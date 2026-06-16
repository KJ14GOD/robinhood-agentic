"""Tests for autonomous Autopilot strategy discovery.

Theme Scout finds active areas. Strategy Discovery turns those into concrete tactic/regime
experiments that Autopilot can test without the user creating a mission.
"""
import os
import tempfile
import types
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from sqlalchemy import delete  # noqa: E402

from brain.db import repository as repo  # noqa: E402
from brain.db.models import AutonomousStrategyRecord, AutonomousThemeRecord  # noqa: E402
from brain.engines import strategy_discovery, twin  # noqa: E402
from brain.models import RiskProfile, TwinDecision, TwinMove  # noqa: E402


def _candidate(ticker, score=70, rsi=50):
    return {
        "ticker": ticker,
        "score": score,
        "reason": f"{ticker} active in theme",
        "ret_1m_pct": 8,
        "ret_3m_pct": 24,
        "ret_6m_pct": 35,
        "rsi_14": rsi,
        "vol_annualized_pct": 45,
        "above_50d": True,
        "above_200d": True,
    }


class StrategyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        repo.init_db()
        with repo.db_session() as s:
            s.execute(delete(AutonomousStrategyRecord))
            s.execute(delete(AutonomousThemeRecord))
        self._sd_gsm = strategy_discovery.get_signals_many
        self._tw_gsm = twin.get_signals_many
        self._tw_gs = twin.get_signals
        self._tw_screen = twin.screen_universe
        self._tw_universe = twin.screening_universe
        self._tw_state = twin.research_state.load_state
        strategy_discovery.get_signals_many = lambda names, **k: {
            t.upper(): types.SimpleNamespace(price=100, ret_3m_pct=0.0, rsi_14=50.0, above_200d=True)
            for t in names
        }
        twin.get_signals_many = strategy_discovery.get_signals_many
        twin.get_signals = lambda t, refresh=False: types.SimpleNamespace(sector="Technology")
        twin.screen_universe = lambda tickers, refresh=False: []
        twin.screening_universe = lambda exclude=None: []
        twin.research_state.load_state = lambda: types.SimpleNamespace(watchlist=[], theses={})
        repo.upsert_autonomous_theme(
            "ai_power_infra", "AI power and data-center infrastructure",
            score=72, confidence=80,
            evidence=["theme active"],
            candidates=[_candidate("VRT", 75, 48), _candidate("ETN", 68, 54), _candidate("APLD", 64, 70)],
            status="active",
        )

    def tearDown(self):
        strategy_discovery.get_signals_many = self._sd_gsm
        twin.get_signals_many = self._tw_gsm
        twin.get_signals = self._tw_gs
        twin.screen_universe = self._tw_screen
        twin.screening_universe = self._tw_universe
        twin.research_state.load_state = self._tw_state

    def test_strategy_discovery_persists_experiments(self):
        strategies = strategy_discovery.run_due(force=True)
        rows = repo.autonomous_strategies(min_score=40)

        self.assertTrue(strategies)
        self.assertTrue(any(s["tactic"] == "pullback_in_uptrend" for s in rows))
        top = rows[0]
        self.assertIn(top["status"], {"active", "exploring"})
        self.assertIn("hypothesis", top)
        self.assertIn("VRT", {c["ticker"] for c in top["candidates"]})

    def test_strategy_experiments_feed_autopilot_and_get_attributed(self):
        strategy_discovery.run_due(force=True)

        universe = twin._candidate_universe(set())
        self.assertIn("VRT", universe)
        self.assertIn("strategy experiment", universe["VRT"])

        decision = TwinDecision(summary="Test strategy.", moves=[
            TwinMove(ticker="VRT", action="buy", usd=100, reasoning="strategy candidate",
                     conviction=7, tactic="theme_exposure"),
        ])
        adjusted, notes, rejected = twin._critic(
            decision,
            {"cash": 1000.0, "positions": [], "value": 1000.0},
            RiskProfile(),
            universe,
        )

        self.assertFalse(rejected)
        self.assertTrue(adjusted.moves[0].source_strategy_key)
        self.assertIn("Critic aligned tactic", next(iter(notes.values())))


if __name__ == "__main__":
    unittest.main()
