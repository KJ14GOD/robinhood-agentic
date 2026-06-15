"""Tests for the Twin engine — inception clone, mark-to-market, fills under fixed capital, and
the You-vs-Twin compare. Offline: quotes and the real portfolio are stubbed; a throwaway DB
backs the fund's books. `execute_pending(force=True)` bypasses the market-hours gate.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from sqlalchemy import select  # noqa: E402

from brain.db import repository as repo  # noqa: E402
from brain.db.models import TwinTradeRecord, TwinTradeReviewRecord  # noqa: E402
from brain.engines import twin  # noqa: E402
from brain.models import Holding, Portfolio, RiskProfile, TwinDecision, TwinMove  # noqa: E402

QUOTES: dict[str, float] = {}


def _fake_quotes(tickers, refresh=False):
    return {t.upper(): types.SimpleNamespace(price=QUOTES.get(t.upper(), 0.0)) for t in tickers}


def _fake_quote(t, refresh=False):
    return types.SimpleNamespace(price=QUOTES.get(t.upper(), 0.0))


def _pf(cash, holdings):
    """holdings: {ticker: (shares, price)}."""
    return Portfolio(cash=cash, holdings=[Holding(ticker=t, quantity=s, avg_cost=p, current_price=p)
                                          for t, (s, p) in holdings.items()])


class TwinTests(unittest.TestCase):
    def setUp(self):
        repo.reset_twin()
        self._gq, self._gqs = twin.get_quote, twin.get_quotes
        twin.get_quote, twin.get_quotes = _fake_quote, _fake_quotes
        QUOTES.clear()
        QUOTES.update({"NVDA": 100.0, "VOO": 200.0, "SPY": 100.0})
        # inception: $2,000 cash + 10 NVDA @100 + 5 VOO @200 = $4,000
        self.real = _pf(2000.0, {"NVDA": (10, 100.0), "VOO": (5, 200.0)})

    def tearDown(self):
        twin.get_quote, twin.get_quotes = self._gq, self._gqs

    def test_inception_clones_the_book(self):
        twin.inception(real_pf=self.real)
        f = twin.state()
        self.assertEqual(f["status"], "running")
        self.assertEqual(f["cash"], 2000.0)
        self.assertEqual(f["inception_value"], 4000.0)
        v = twin.value()
        self.assertAlmostEqual(v["value"], 4000.0)
        poss = {p["ticker"]: p for p in v["positions"]}
        self.assertAlmostEqual(poss["NVDA"]["shares"], 10.0)
        self.assertAlmostEqual(poss["NVDA"]["avg_cost"], 100.0)   # cost = inception price

    def test_inception_is_once_only(self):
        twin.inception(real_pf=self.real)
        # a second call with a different book must NOT re-clone
        twin.inception(real_pf=_pf(9999.0, {"AAPL": (1, 1.0)}))
        f = twin.state()
        self.assertEqual(f["cash"], 2000.0)
        self.assertEqual(f["inception_value"], 4000.0)

    def test_mark_to_market_moves_with_price(self):
        twin.inception(real_pf=self.real)
        QUOTES["NVDA"] = 120.0   # +20%
        v = twin.value(refresh=True)
        self.assertAlmostEqual(v["value"], 2000 + 10 * 120 + 5 * 200)   # 4,200
        nvda = next(p for p in v["positions"] if p["ticker"] == "NVDA")
        self.assertAlmostEqual(nvda["return_pct"], 20.0)

    def test_buy_clamped_to_cash_and_updates_avg(self):
        twin.inception(real_pf=self.real)         # cash 2,000
        QUOTES["NVDA"] = 200.0
        twin.queue_trade("NVDA", "buy", 5, reasoning="add on strength", conviction=7)
        fills = twin.execute_pending(force=True)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["filled_shares"], 5.0)        # 5 * 200 = 1,000 <= cash
        self.assertAlmostEqual(twin.state()["cash"], 1000.0)
        pos = repo.get_twin_position("NVDA")
        self.assertAlmostEqual(pos["shares"], 15.0)
        self.assertAlmostEqual(pos["avg_cost"], (10 * 100 + 5 * 200) / 15)   # 133.33

    def test_buy_beyond_cash_is_clamped(self):
        twin.inception(real_pf=self.real)         # cash 2,000
        QUOTES["NVDA"] = 100.0
        twin.queue_trade("NVDA", "buy", 1000)     # would cost 100k; only 2k cash
        twin.execute_pending(force=True)
        self.assertAlmostEqual(twin.state()["cash"], 0.0)
        self.assertAlmostEqual(repo.get_twin_position("NVDA")["shares"], 30.0)   # 10 + 20 afforded

    def test_sell_raises_cash_and_can_close(self):
        twin.inception(real_pf=self.real)
        twin.queue_trade("VOO", "sell", 5)        # close the whole VOO position @200
        twin.execute_pending(force=True)
        self.assertAlmostEqual(twin.state()["cash"], 2000 + 1000)   # +5*200
        self.assertIsNone(repo.get_twin_position("VOO"))

    def test_off_hours_does_not_fill(self):
        twin.inception(real_pf=self.real)
        twin.queue_trade("NVDA", "buy", 1)
        # not forced + (almost certainly) market closed during the test run → stays queued
        if not twin.market_clock.is_market_open():
            self.assertEqual(twin.execute_pending(force=False), [])
            self.assertEqual(len(repo.pending_twin_trades()), 1)

    def test_compare_edge(self):
        twin.inception(real_pf=self.real)          # both start at 4,000
        QUOTES["NVDA"] = 140.0                      # twin: 2000 + 10*140 + 5*200 = 4,400 (+10%)
        real_now = _pf(2000.0, {"NVDA": (10, 120.0), "VOO": (5, 200.0)})   # real: 2000+1200+1000 = 4,200 (+5%)
        c = twin.compare(real_pf=real_now, refresh=True)
        self.assertAlmostEqual(c["twin"]["return_pct"], 10.0)
        self.assertAlmostEqual(c["real"]["return_pct"], 5.0)
        self.assertAlmostEqual(c["edge_pct"], 5.0)
        self.assertAlmostEqual(c["real"]["marked_value"], 4400.0)

    def test_compare_keeps_broker_value_and_exposes_marked_real_value(self):
        twin.inception(real_pf=self.real)
        QUOTES["NVDA"] = 80.0
        QUOTES["VOO"] = 190.0
        real_now = _pf(2000.0, {"NVDA": (10, 999.0), "VOO": (5, 999.0)})
        c = twin.compare(real_pf=real_now, refresh=True)
        self.assertAlmostEqual(c["twin"]["value"], 2000 + 10 * 80 + 5 * 190)
        self.assertAlmostEqual(c["real"]["value"], 2000 + 10 * 999 + 5 * 999)
        self.assertAlmostEqual(c["real"]["marked_value"], c["twin"]["value"])

    def _decide_with(self, dec, candidates=None, profile=None):
        """Run a decision cycle with the model + signals stubbed to a fixed decision."""
        gsm, lp, cu = twin.get_signals_many, twin.llm.parse, twin._candidate_universe
        twin.get_signals_many = lambda names, **k: {}
        twin.llm.parse = lambda *a, **k: dec
        twin._candidate_universe = lambda held: candidates if candidates is not None else {"GOOGL": "test candidate"}
        try:
            return twin.decide(profile or RiskProfile())
        finally:
            twin.get_signals_many, twin.llm.parse, twin._candidate_universe = gsm, lp, cu

    def test_decide_queues_sells_first_and_sets_intent(self):
        twin.inception(real_pf=self.real)   # cash 2,000; NVDA 10@100; VOO 5@200
        QUOTES["GOOGL"] = 100.0
        out = self._decide_with(TwinDecision(summary="Rotate a little.", moves=[
            TwinMove(ticker="GOOGL", action="buy", usd=400, reasoning="open a core AI name",
                     conviction=7, tactic="theme_exposure", thesis="durable", horizon="core",
                     exit_rule="thesis breaks", review_after_days=30),
            TwinMove(ticker="NVDA", action="trim", usd=500, reasoning="take some profit",
                     conviction=6, tactic="risk_reduction", thesis="still core, smaller",
                     horizon="core", exit_rule="loses 200d", review_after_days=14),
        ]))
        self.assertEqual(out.summary, "Rotate a little.")
        pending = repo.pending_twin_trades()
        self.assertEqual(len(pending), 2)
        self.assertEqual((pending[0]["action"], pending[0]["ticker"]), ("trim", "NVDA"))  # sell-side first
        self.assertEqual(pending[1]["action"], "buy")
        self.assertEqual(pending[0]["tactic"], "risk_reduction")
        self.assertEqual(pending[1]["review_after_days"], 30)
        self.assertEqual(repo.get_twin_position("NVDA")["horizon"], "core")   # intent refreshed on held name
        self.assertTrue(repo.recent_agent_runs(limit=3, kind="twin_decision"))  # cadence/audit marker

    def test_decide_skips_when_orders_are_already_pending(self):
        twin.inception(real_pf=self.real)
        twin.queue_trade("NVDA", "buy", 1, reasoning="already queued")
        called = {"n": 0}
        lp = twin.llm.parse

        def _parse(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("LLM should not run with pending orders")

        twin.llm.parse = _parse
        try:
            self.assertIsNone(twin.decide(RiskProfile()))
        finally:
            twin.llm.parse = lp
        self.assertEqual(called["n"], 0)
        self.assertEqual(len(repo.pending_twin_trades()), 1)

    def test_critic_rejects_ungrounded_buy(self):
        twin.inception(real_pf=self.real)
        self._decide_with(TwinDecision(summary="Try an ungrounded name.", moves=[
            TwinMove(ticker="FAKE", action="buy", usd=100, reasoning="model invented it",
                     conviction=5, tactic="theme_exposure"),
        ]), candidates={})
        trades = repo.recent_twin_trades(5)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "canceled")
        self.assertIn("not in the grounded candidate universe", trades[0]["critic_note"])
        self.assertEqual(repo.pending_twin_trades(), [])

    def test_critic_scales_buys_to_fixed_capital(self):
        twin.inception(real_pf=self.real)   # cash 2,000
        self._decide_with(TwinDecision(summary="Overspend.", moves=[
            TwinMove(ticker="GOOGL", action="buy", usd=1800, reasoning="buy one",
                     conviction=6, tactic="theme_exposure"),
            TwinMove(ticker="MSFT", action="buy", usd=1200, reasoning="buy two",
                     conviction=6, tactic="theme_exposure"),
        ]), candidates={"GOOGL": "test", "MSFT": "test"}, profile=RiskProfile(max_single_position_pct=100))
        pending = repo.pending_twin_trades()
        self.assertEqual(len(pending), 2)
        self.assertAlmostEqual(sum(t["value"] for t in pending), 2000.0, places=2)
        self.assertTrue(all("fixed capital" in t["critic_note"] for t in pending))

    def test_critic_does_not_cap_position_size(self):
        # No single-position cap — Autopilot sizes its own concentration; only fixed capital limits it.
        twin.inception(real_pf=self.real)   # $4,000 book, $2,000 cash
        self._decide_with(TwinDecision(summary="Concentrate.", moves=[
            TwinMove(ticker="GOOGL", action="buy", usd=2000, reasoning="high-conviction concentrate",
                     conviction=8, tactic="theme_exposure"),
        ]), candidates={"GOOGL": "test"}, profile=RiskProfile(max_single_position_pct=15))
        pending = repo.pending_twin_trades()
        self.assertEqual(len(pending), 1)
        self.assertAlmostEqual(pending[0]["value"], 2000.0, places=2)  # full size — not capped to 15%

    def test_stale_pending_batches_are_canceled_before_fill(self):
        twin.inception(real_pf=self.real)
        repo.add_twin_trade("NVDA", "buy", 0.0, usd=100.0, reasoning="older batch")
        repo.add_twin_trade("VOO", "buy", 0.0, usd=200.0, reasoning="latest batch")
        with repo.db_session() as session:
            rows = session.execute(select(TwinTradeRecord).order_by(TwinTradeRecord.id)).scalars().all()
            rows[-2].decided_at = datetime.now(timezone.utc) - timedelta(hours=4)
            rows[-1].decided_at = datetime.now(timezone.utc)

        fills = twin.execute_pending(force=True)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["ticker"], "VOO")
        trades = {t["ticker"]: t for t in repo.recent_twin_trades(5)}
        self.assertEqual(trades["NVDA"]["status"], "canceled")
        self.assertEqual(trades["VOO"]["status"], "filled")

    def test_review_due_trades_scores_filled_tactic(self):
        twin.inception(real_pf=self.real)
        repo.add_twin_trade("NVDA", "buy", 0.0, usd=500.0, reasoning="pullback setup",
                            tactic="pullback_in_uptrend", horizon="swing", review_after_days=1)
        twin.execute_pending(force=True)
        with repo.db_session() as session:
            row = session.execute(select(TwinTradeRecord).where(TwinTradeRecord.ticker == "NVDA")).scalars().first()
            row.filled_at = datetime.now(timezone.utc) - timedelta(days=2)
        QUOTES["NVDA"] = 110.0
        QUOTES["SPY"] = 102.0
        reviewed = twin.review_due_trades(refresh=True)
        self.assertEqual(len(reviewed), 1)
        trade = repo.recent_twin_trades(1)[0]
        self.assertEqual(trade["review_status"], "reviewed")
        self.assertAlmostEqual(trade["review_return_pct"], 10.0, places=1)
        self.assertAlmostEqual(trade["review_alpha_pct"], 8.0, places=1)
        self.assertIn("pullback_in_uptrend", twin._policy_memory())

    def test_candidate_universe_includes_broad_screen_names(self):
        old_screen, old_universe = twin.screen_universe, twin.screening_universe
        try:
            twin.screening_universe = lambda exclude=None: ["AAA", "BBB", "NVDA"]
            twin.screen_universe = lambda tickers, refresh=False: [
                twin.ScreenRow(ticker="AAA", price=10, ret_3m_pct=20, ret_6m_pct=30,
                               above_50d=True, above_200d=True, rsi_14=60, vol_annualized_pct=25),
                twin.ScreenRow(ticker="BBB", price=20, ret_3m_pct=5, ret_6m_pct=6,
                               above_50d=True, above_200d=True, rsi_14=55, vol_annualized_pct=20),
            ]
            uni = twin._candidate_universe({"NVDA"})
        finally:
            twin.screen_universe, twin.screening_universe = old_screen, old_universe
        self.assertIn("AAA", uni)
        self.assertIn("broad screen", uni["AAA"])
        self.assertNotIn("NVDA", uni)

    def test_decide_then_execute_changes_book(self):
        twin.inception(real_pf=self.real)
        QUOTES["GOOGL"] = 100.0
        self._decide_with(TwinDecision(summary="Open GOOGL from the NVDA trim.", moves=[
            TwinMove(ticker="NVDA", action="trim", usd=400, reasoning="trim", conviction=6),
            TwinMove(ticker="GOOGL", action="buy", usd=400, reasoning="open", conviction=7),
        ]))
        fills = twin.execute_pending(force=True)
        self.assertEqual(len(fills), 2)
        self.assertIsNotNone(repo.get_twin_position("GOOGL"))   # opened from the freed cash
        self.assertAlmostEqual(repo.get_twin_position("NVDA")["shares"], 6.0)  # 10 - 4 trimmed
        self.assertAlmostEqual(twin.state()["cash"], 2000.0)    # +400 trim, -400 buy = net flat
        # the filled share count is written back to the trade record (History shows real shares)
        self.assertTrue(all(t["shares"] > 0 for t in repo.recent_twin_trades(5) if t["status"] == "filled"))

    def test_decide_holds_when_no_moves(self):
        twin.inception(real_pf=self.real)
        self._decide_with(TwinDecision(summary="Nothing worth a trade.", moves=[]))
        self.assertEqual(repo.pending_twin_trades(), [])

    # --- mature multi-window, thesis-aware self-review ---
    def test_windows_for_horizon(self):
        long_w = {w: j for w, _, j in twin._windows_for("multi-year core", "long_term_compounder")}
        self.assertTrue(long_w.get("3m"))           # 3m is a JUDGED window for a long-term hold
        self.assertFalse(long_w.get("1w", True))    # 1w is monitoring-only (grace)
        hygiene = [w for w, _, _ in twin._windows_for("core", "rebalance")]
        self.assertEqual(hygiene, ["1w"])           # hygiene tactic: one quick check

    def _backdate_due(self):
        with repo.db_session() as s:
            for r in s.execute(select(TwinTradeReviewRecord)).scalars().all():
                r.due_at = datetime.now(timezone.utc) - timedelta(days=1)

    def _review_with(self, state, dd_normal, reason, drawdown=-10.0):
        a, dd = twin._assess_thesis, twin._drawdown_since
        twin._assess_thesis = lambda *args, **k: (state, dd_normal, reason)
        twin._drawdown_since = lambda *args, **k: drawdown
        try:
            return twin.review_windows(refresh=True)
        finally:
            twin._assess_thesis, twin._drawdown_since = a, dd

    def test_review_grace_intact_on_normal_dip(self):
        # Long-term hold, down 8%, but SPY/sector down too and thesis active → NOT a failure.
        twin.inception(real_pf=self.real)
        repo.schedule_twin_reviews(1, "NVDA", "buy", "long_term_compounder", "multi-year",
                                   entry_price=100.0, bench_entry=100.0, sector_symbol="SMH",
                                   sector_entry=100.0, windows=[("1w", 7, False)])
        self._backdate_due()
        QUOTES.update({"NVDA": 92.0, "SPY": 96.0, "SMH": 93.0})
        done = self._review_with("active", True, "no invalidation found")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["verdict"], "intact")    # monitoring window + active thesis
        self.assertNotEqual(done[0]["verdict"], "failed")

    def test_review_failed_on_thesis_break(self):
        twin.inception(real_pf=self.real)
        repo.schedule_twin_reviews(1, "NVDA", "buy", "long_term_compounder", "multi-year",
                                   entry_price=100.0, bench_entry=100.0, sector_symbol="SMH",
                                   sector_entry=100.0, windows=[("3m", 90, True)])
        self._backdate_due()
        QUOTES.update({"NVDA": 92.0, "SPY": 100.0, "SMH": 100.0})
        done = self._review_with("broken", False, "guidance cut in the core segment")
        self.assertEqual(done[0]["verdict"], "failed")    # thesis broke → real failure

    def test_review_worked_at_judged_window_beats_sector(self):
        twin.inception(real_pf=self.real)
        repo.schedule_twin_reviews(1, "NVDA", "buy", "momentum_continuation", "swing",
                                   entry_price=100.0, bench_entry=100.0, sector_symbol="SMH",
                                   sector_entry=100.0, windows=[("1m", 30, True)])
        self._backdate_due()
        QUOTES.update({"NVDA": 110.0, "SPY": 102.0, "SMH": 104.0})   # +10 vs sector +4 → sector alpha +6
        done = self._review_with("active", True, "thesis on track")
        self.assertEqual(done[0]["verdict"], "worked")
        self.assertAlmostEqual(done[0]["sector_alpha"], 6.0, places=1)


if __name__ == "__main__":
    unittest.main()
