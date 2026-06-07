"""Tests for the DB-backed shadow ledger (the evaluation layer's raw material):
benchmark + sector anchoring at entry, signals snapshot, mark-to-market and the
alpha math, dedup, scoreboard shape, agent_runs persistence, and legacy-record
compatibility.

Uses a throwaway SQLite DB and a temp SHADOW_PATH so neither the real data store
nor the real JSONL ledger is ever touched.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

import brain.config as bconfig  # noqa: E402

# Point the JSONL migration at a path that does not exist, so tests never read
# or rename the real ledger.
bconfig.SHADOW_PATH = Path(_TMP.name + ".no-such-ledger.jsonl")

from brain import shadow  # noqa: E402
from brain.data.prices import Quote, TrendSignals  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.models import RiskAppetite, RiskProfile, ShadowTrade, TradeTicket  # noqa: E402

# Deterministic fake quotes — symbol -> price. Patched in over the network call.
PRICES: dict[str, float] = {}


def _fake_quote(ticker: str, refresh: bool = False) -> Quote:
    return Quote(ticker=ticker, price=PRICES.get(ticker.upper(), 0.0))


def _ticket(ticker: str, action: str = "buy", conviction: int = 7) -> TradeTicket:
    return TradeTicket(ticker=ticker, action=action, conviction=conviction,
                       thesis="thesis", catalyst="catalyst", risks="risks")


class ShadowDBTests(unittest.TestCase):
    def setUp(self):
        shadow.get_quote = _fake_quote  # cut the network for every shadow quote lookup
        PRICES.clear()
        PRICES.update({"SPY": 500.0, "XLK": 200.0, "XLP": 70.0,
                       "NVDA": 100.0, "AMD": 100.0, "KO": 60.0})

    def test_log_captures_benchmark_and_signals(self):
        sig = TrendSignals(ticker="NVDA", price=100.0, sector="Technology",
                           beta=1.8, rsi_14=62.0, ret_3m_pct=12.0, above_200d=True)
        profile = RiskProfile(appetite=RiskAppetite.aggressive)
        trade = shadow.log_recommendation(_ticket("NVDA", "buy", 8),
                                          source="analyst", profile=profile, signals=sig)

        self.assertEqual(trade.decision_label, "BUY CANDIDATE")
        self.assertEqual(trade.risk_mode, "aggressive")
        self.assertEqual(trade.bench_symbol, "SPY")
        self.assertEqual(trade.bench_entry_price, 500.0)
        self.assertEqual(trade.sector_etf, "XLK")
        self.assertEqual(trade.sector_etf_entry_price, 200.0)
        self.assertEqual(trade.entry_signals.get("beta"), 1.8)

        # round-trips through the DB with the snapshot intact
        got = next(t for t in repo.all_shadow_trades() if t.id == trade.id)
        self.assertEqual(got.sector_etf, "XLK")
        self.assertEqual(got.entry_signals.get("rsi_14"), 62.0)
        self.assertEqual(got.decision_label, "BUY CANDIDATE")

    def test_mark_to_market_and_alpha(self):
        sig = TrendSignals(ticker="AMD", price=100.0, sector="Technology")
        trade = shadow.log_recommendation(_ticket("AMD", "buy", 7), source="analyst", signals=sig)

        # AMD +20%, SPY +5%, XLK +5%  ->  alpha +15, sector alpha +15
        PRICES.update({"AMD": 120.0, "SPY": 525.0, "XLK": 210.0})
        marked = shadow.mark_to_market(refresh=True)
        m = next(t for t in marked if t.id == trade.id)

        self.assertAlmostEqual(m.return_pct(), 20.0, places=1)
        self.assertAlmostEqual(m.bench_change_pct(), 5.0, places=1)
        self.assertAlmostEqual(m.alpha_pct(), 15.0, places=1)
        self.assertAlmostEqual(m.sector_alpha_pct(), 15.0, places=1)

    def test_sell_call_inverts_sign(self):
        # A 'trim' is right when the name falls: a -10% move is a +10% return.
        trade = shadow.log_recommendation(_ticket("KO", "trim", 5), source="analyst")
        PRICES["KO"] = 54.0  # -10%
        marked = shadow.mark_to_market(refresh=True)
        m = next(t for t in marked if t.id == trade.id)
        self.assertAlmostEqual(m.return_pct(), 10.0, places=1)

    def test_has_open_dedup(self):
        self.assertFalse(shadow.has_open("MSFT", source="analyst"))
        PRICES["MSFT"] = 400.0
        shadow.log_recommendation(_ticket("MSFT"), source="analyst")
        self.assertTrue(shadow.has_open("MSFT"))
        self.assertTrue(shadow.has_open("MSFT", source="analyst"))
        self.assertFalse(shadow.has_open("MSFT", source="discovery"))

    def test_scoreboard_keeps_keys(self):
        PRICES["TSLA"] = 250.0
        shadow.log_recommendation(_ticket("TSLA"), source="analyst")
        board = shadow.scoreboard(refresh=True)
        for key in ("count", "win_rate", "avg_return_pct", "trades"):
            self.assertIn(key, board)
        self.assertGreaterEqual(board["count"], 1)
        self.assertIn("alpha_pct", board["trades"][0])

    def test_agent_runs_persist(self):
        rid = repo.save_agent_run(
            query="what's my biggest risk?",
            answer="Concentration in APLD.",
            steps=[{"type": "tool", "name": "get_my_portfolio"}],
            tools_used="get_my_portfolio",
            model="claude-test",
        )
        runs = repo.recent_agent_runs(limit=5)
        got = next(r for r in runs if r["id"] == rid)
        self.assertEqual(got["tools_used"], "get_my_portfolio")
        self.assertEqual(got["steps"][0]["name"], "get_my_portfolio")

    def test_legacy_record_parses_with_defaults(self):
        # A pre-migration JSONL record (no benchmark/label fields) must still load.
        old = ('{"id":"legacy123","ticker":"AAPL","action":"buy","conviction":6,'
               '"thesis":"old","entry_price":150.0,'
               '"entry_at":"2026-05-01T00:00:00+00:00","source":"analyst",'
               '"last_price":165.0,"last_at":"2026-05-10T00:00:00+00:00",'
               '"closed":false,"user_executed":null}')
        t = ShadowTrade.model_validate_json(old)
        self.assertEqual(t.ticker, "AAPL")
        self.assertEqual(t.decision_label, "WATCHLIST")   # default
        self.assertEqual(t.bench_entry_price, 0.0)          # default
        self.assertAlmostEqual(t.return_pct(), 10.0, places=1)
        self.assertEqual(t.alpha_pct(), 0.0)                # no anchor -> not gradeable


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        shadow.get_quote = _fake_quote
        PRICES.clear()
        PRICES.update({"SPY": 500.0, "XLK": 200.0})

    def test_scorecard_structure_and_cuts(self):
        from datetime import datetime, timedelta, timezone
        from brain.engines import evaluation
        # Backdate past the maturity bar so these count toward the trusted headline
        # and populate the cuts (which are computed over matured calls only).
        old = (datetime.now(timezone.utc) - timedelta(days=evaluation.MATURE_DAYS + 5)).isoformat()
        for tkr, conv in [("EVALA", 9), ("EVALB", 2)]:
            PRICES[tkr] = 100.0
            tr = shadow.log_recommendation(
                _ticket(tkr, "buy", conv), source="discovery",
                signals=TrendSignals(ticker=tkr, sector="Technology"))
            tr.entry_at = old
            repo.save_shadow_trade(tr)
        card = evaluation.scorecard(refresh=True)

        for key in ("headline", "forming", "calibration", "by_source", "by_label",
                    "by_mode", "narrative", "best", "worst", "themes", "trades"):
            self.assertIn(key, card)
        h = card["headline"]
        self.assertGreaterEqual(h["matured"], 2)
        self.assertEqual(h["count"], h["matured"])          # headline grades matured only
        self.assertGreaterEqual(h["total"], 2)
        self.assertIn("median_age_days", h)
        buckets = {r["key"] for r in card["calibration"]}
        self.assertTrue({"high", "low"} <= buckets)         # both matured buckets present
        self.assertTrue(any(r["key"] == "discovery" for r in card["by_source"]))
        self.assertTrue(card["narrative"] and isinstance(card["narrative"][0], str))
        self.assertTrue(all("age_days" in r and "mature" in r for r in card["trades"]))

    def test_fresh_calls_are_forming_not_graded(self):
        """A just-logged call is too young to grade: it lands in 'forming', carries
        mature=False, and never inflates the matured headline."""
        from brain.engines import evaluation
        PRICES["FRESHA"] = 100.0
        shadow.log_recommendation(
            _ticket("FRESHA", "buy", 7), source="analyst",
            signals=TrendSignals(ticker="FRESHA", sector="Technology"))
        card = evaluation.scorecard(refresh=True)
        self.assertGreaterEqual(card["headline"]["forming"], 1)
        row = next(r for r in card["trades"] if r["ticker"] == "FRESHA")
        self.assertFalse(row["mature"])
        self.assertLess(row["age_days"], evaluation.MATURE_DAYS)

    def test_theme_signal_sector_then_mission(self):
        """Themes group by GICS sector by default, but a name in a mission roster is
        attributed to that mission (the actionable theme), which takes precedence."""
        from datetime import datetime, timedelta, timezone
        from brain.engines import evaluation
        from brain.models import Mission, MissionCandidate

        old = (datetime.now(timezone.utc) - timedelta(days=evaluation.MATURE_DAYS + 3)).isoformat()
        PRICES["THEMEA"] = 100.0
        tr = shadow.log_recommendation(
            _ticket("THEMEA", "buy", 8), source="discovery",
            signals=TrendSignals(ticker="THEMEA", sector="Energy"))
        tr.entry_at = old
        repo.save_shadow_trade(tr)

        themes = {t["theme"]: t for t in evaluation.scorecard(refresh=True)["themes"]}
        self.assertIn("Energy", themes)                       # sector fallback
        self.assertEqual(themes["Energy"]["kind"], "sector")

        # Now put THEMEA in a mission — it should re-attribute to the mission theme.
        repo.save_mission(Mission(id="m1", title="nuclear power", mode="any", status="active",
                                  candidates=[MissionCandidate(ticker="THEMEA", label="BUY")]))
        themes2 = {t["theme"]: t for t in evaluation.scorecard(refresh=True)["themes"]}
        self.assertIn("nuclear power", themes2)
        self.assertEqual(themes2["nuclear power"]["kind"], "mission")
        self.assertNotIn("Energy", themes2)                   # mission supersedes the sector

    def test_agg_and_bucket_math(self):
        from brain.engines import evaluation as ev
        self.assertEqual(ev._bucket(9), "high")
        self.assertEqual(ev._bucket(5), "medium")
        self.assertEqual(ev._bucket(1), "low")

        def mk(last: float) -> ShadowTrade:
            return ShadowTrade(id=f"x{last}", ticker="ZZ", action="buy", conviction=8,
                               thesis="t", entry_price=100.0, last_price=last,
                               bench_entry_price=500.0, bench_last_price=525.0)  # SPY +5%

        agg = ev._agg([mk(110.0), mk(90.0)])  # returns +10% / -10%
        self.assertEqual(agg["count"], 2)
        self.assertEqual(agg["win_rate"], 50.0)
        self.assertAlmostEqual(agg["avg_return_pct"], 0.0, places=1)
        self.assertEqual(agg["benchmarked"], 2)
        # alpha: (+10-5) and (-10-5) -> avg -5
        self.assertAlmostEqual(agg["avg_alpha_pct"], -5.0, places=1)


if __name__ == "__main__":
    unittest.main()
