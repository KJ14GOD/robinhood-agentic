"""Tests for the autonomous Theme Scout.

The scout is Signal's own research agenda: it discovers themes from market data and feeds high-score
candidates to Autopilot without the user creating a mission.
"""
import os
import tempfile
import types
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from brain.data.prices import ScreenRow  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.db.models import TwinTradeReviewRecord  # noqa: E402
from brain.engines import theme_scout, twin  # noqa: E402
from brain.models import TwinMove  # noqa: E402


def _row(ticker, one=12, three=35, six=45, rsi=58):
    return ScreenRow(ticker=ticker, price=100, ret_1m_pct=one, ret_3m_pct=three,
                     ret_6m_pct=six, above_50d=True, above_200d=True,
                     rsi_14=rsi, vol_annualized_pct=45)


class ThemeScoutTests(unittest.TestCase):
    def setUp(self):
        repo.reset_twin()
        self._screen = theme_scout.screen_universe
        self._twin_screen = twin.screen_universe
        self._twin_universe = twin.screening_universe
        self._state = twin.research_state.load_state
        rows = {
            "VRT": _row("VRT", 18, 48, 60),
            "ETN": _row("ETN", 9, 24, 32),
            "APLD": _row("APLD", 22, 55, 80, rsi=70),
            "NVDA": _row("NVDA", 3, 9, 20),
        }
        theme_scout.screen_universe = lambda tickers, refresh=False: [rows[t] for t in tickers if t in rows]
        twin.screen_universe = lambda tickers, refresh=False: []
        twin.screening_universe = lambda exclude=None: []
        twin.research_state.load_state = lambda: types.SimpleNamespace(watchlist=[], theses={})

    def tearDown(self):
        theme_scout.screen_universe = self._screen
        twin.screen_universe = self._twin_screen
        twin.screening_universe = self._twin_universe
        twin.research_state.load_state = self._state

    def test_scout_persists_autonomous_theme_and_feeds_autopilot(self):
        themes = theme_scout.run_due(force=True)
        active = repo.autonomous_themes(status="active", min_score=45)

        self.assertTrue(any(t["key"] == "ai_power_infra" for t in themes))
        power = next(t for t in active if t["key"] == "ai_power_infra")
        self.assertGreaterEqual(power["score"], 45)
        self.assertIn("VRT", {c["ticker"] for c in power["candidates"]})

        universe = twin._candidate_universe(set())
        self.assertIn("VRT", universe)
        self.assertTrue(any("autonomous theme" in src for src in universe.values()))

    def test_scout_cadence_skips_when_fresh(self):
        theme_scout.run_due(force=True)
        self.assertEqual(theme_scout.run_due(force=False), [])

    def test_theme_attribution_flows_into_feedback(self):
        theme_scout.run_due(force=True)
        decision = twin.TwinDecision(summary="Test the theme.", moves=[
            TwinMove(ticker="VRT", action="buy", usd=100, reasoning="theme test",
                     conviction=7, tactic="theme_exposure", thesis="AI infra demand",
                     horizon="swing", exit_rule="theme breaks"),
        ])
        profile = types.SimpleNamespace(max_single_position_pct=100)
        v = {"cash": 1000.0, "positions": [], "value": 1000.0}
        universe = twin._candidate_universe(set())
        adjusted, _, rejected = twin._critic(decision, v, profile, universe)
        self.assertFalse(rejected)
        self.assertEqual(adjusted.moves[0].source_theme_key, "ai_power_infra")

        twin._apply(adjusted)
        trade = repo.pending_twin_trades()[0]
        self.assertEqual(trade["source_theme_key"], "ai_power_infra")
        repo.schedule_twin_reviews(
            trade["id"], "VRT", "buy", "theme_exposure", "swing",
            entry_price=100.0, bench_entry=100.0, sector_symbol="XLK", sector_entry=100.0,
            windows=[("1m", 30, True)], source_theme_key=trade["source_theme_key"],
            source_theme_name=trade["source_theme_name"])
        with repo.db_session() as s:
            r = s.query(TwinTradeReviewRecord).first()
            rid = r.id
            r.due_at = r.created_at
        repo.save_twin_review_window(rid, price=110.0, bench_last=102.0, sector_last=104.0,
                                     return_pct=10.0, spy_alpha_pct=8.0, sector_alpha_pct=6.0,
                                     drawdown_pct=-2.0, thesis_state="active", verdict="worked",
                                     note="worked")

        fb = repo.autonomous_theme_feedback()
        self.assertEqual(fb["ai_power_infra"]["tested_count"], 1)
        self.assertAlmostEqual(fb["ai_power_infra"]["avg_sector_alpha"], 6.0)


if __name__ == "__main__":
    unittest.main()
