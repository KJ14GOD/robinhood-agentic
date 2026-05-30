"""Tests for the Living Memory engine (brain/engines/memory.py).

`trigger_reason` is pure. The full `revisit_theses` flow is exercised with the
LLM judge and the signals fetch stubbed out — so it runs offline, with no tokens
— against a throwaway DB + research-state file (the real store is never touched).

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP.name}")

from brain import config, research_state  # noqa: E402
from brain.engines import memory  # noqa: E402
from brain.data.prices import TrendSignals  # noqa: E402
from brain.models import (  # noqa: E402
    Portfolio, Holding, RiskProfile, ResearchState, Thesis, ThesisVerdict,
)


def tearDownModule():
    # See test_events_repo: avoid mid-run unlink of a possibly-shared DB. OS cleans /tmp.
    pass


class TriggerReasonTests(unittest.TestCase):
    def test_drawdown_triggers(self):
        h = Holding(ticker="X", quantity=1, avg_cost=100, current_price=80)  # -20%
        self.assertIsNotNone(memory.trigger_reason(h, None))

    def test_below_200d_triggers(self):
        h = Holding(ticker="X", quantity=1, avg_cost=100, current_price=101)
        sig = TrendSignals(ticker="X", price=101, above_200d=False, rsi_14=50)
        self.assertIn("200-day", memory.trigger_reason(h, sig))

    def test_oversold_triggers(self):
        h = Holding(ticker="X", quantity=1, avg_cost=100, current_price=101)
        sig = TrendSignals(ticker="X", price=101, above_200d=True, rsi_14=25)
        self.assertIn("oversold", memory.trigger_reason(h, sig))

    def test_healthy_position_no_trigger(self):
        h = Holding(ticker="X", quantity=1, avg_cost=100, current_price=105)  # +5%
        sig = TrendSignals(ticker="X", price=105, above_200d=True, rsi_14=55)
        self.assertIsNone(memory.trigger_reason(h, sig))


class RevisitFlowTests(unittest.TestCase):
    def setUp(self):
        # Isolate research-state to a temp file so we never touch the real store.
        self._statefile = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._statefile.close()
        self._orig_path = config.RESEARCH_STATE_PATH
        config.RESEARCH_STATE_PATH = Path(self._statefile.name)

        # Stub the network + LLM: no signals fetch, judge returns "broken".
        self._orig_signals = memory.get_signals_many
        self._orig_judge = memory._judge
        memory.get_signals_many = lambda tickers, **k: {}
        memory._judge = lambda thesis, holding, sig, trigger: ThesisVerdict(
            status="broken", decision_label="EXIT REVIEW",
            reason="Data-center growth disappointed — matches the stated invalidation.")

    def tearDown(self):
        memory.get_signals_many = self._orig_signals
        memory._judge = self._orig_judge
        config.RESEARCH_STATE_PATH = self._orig_path
        try:
            os.unlink(self._statefile.name)
        except OSError:
            pass

    def _seed(self, ticker, status="active"):
        research_state.save_state(ResearchState(theses={
            ticker: Thesis(ticker=ticker, thesis="AI demand keeps compounding",
                           invalidation="data-center growth disappoints", status=status),
        }))

    def _pf(self, ticker):
        # down 20% from cost -> drawdown trigger fires without needing signals.
        return Portfolio(holdings=[Holding(ticker=ticker, quantity=1, avg_cost=100, current_price=80)])

    def test_triggered_thesis_flips_and_logs_event(self):
        tkr = "MEMA"  # unique per test so cooldown events don't collide
        self._seed(tkr)
        changed = memory.revisit_theses(self._pf(tkr), RiskProfile())
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["from"], "active")
        self.assertEqual(changed[0]["to"], "broken")

        self.assertEqual(research_state.load_state().theses[tkr].status, "broken")
        from brain.db import repository as repo
        evs = [e for e in repo.recent_events(limit=50) if e["ticker"] == tkr]
        self.assertTrue(any(e["event_type"] == "thesis_broken" for e in evs))

    def test_cooldown_blocks_second_judgement(self):
        tkr = "MEMB"
        self._seed(tkr)
        first = memory.revisit_theses(self._pf(tkr), RiskProfile())
        self.assertEqual(len(first), 1)
        # reset the thesis to active so the ONLY thing that can block is the cooldown event
        self._seed(tkr, status="active")
        second = memory.revisit_theses(self._pf(tkr), RiskProfile())
        self.assertEqual(second, [])

    def test_unheld_thesis_is_skipped(self):
        tkr = "MEMC"
        self._seed(tkr)
        empty_pf = Portfolio(holdings=[Holding(ticker="OTHER", quantity=1, avg_cost=10, current_price=5)])
        self.assertEqual(memory.revisit_theses(empty_pf, RiskProfile()), [])


if __name__ == "__main__":
    unittest.main()
