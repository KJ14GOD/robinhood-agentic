"""Tests for the drift-triggered mandate plan — the pure drift detector plus the
baseline -> fire -> cooldown flow. Offline: portfolio, mandate, and the review LLM call are
stubbed; a throwaway SQLite DB backs the durable baseline + event cooldown.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from sqlalchemy import delete  # noqa: E402

from brain import orchestrator as o  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.db.models import MandatePlanStateRecord, ResearchEventRecord  # noqa: E402
from brain.db.session import db_session  # noqa: E402
from brain.models import Holding, Mandate, Portfolio  # noqa: E402


def _pf(mv: dict) -> Portfolio:
    """A portfolio whose weights come straight from the given {ticker: market_value} map."""
    return Portfolio(holdings=[Holding(ticker=t, quantity=v, avg_cost=1.0, current_price=1.0)
                               for t, v in mv.items()])


class DriftDetectorTests(unittest.TestCase):
    TH = 12

    def test_no_change_no_drift(self):
        sig = [["NVDA", 50], ["VOO", 50]]
        material, reason = o._mandate_drift(sig, sig, self.TH)
        self.assertFalse(material)
        self.assertEqual(reason, "")

    def test_weight_shift_over_threshold(self):
        material, reason = o._mandate_drift([["NVDA", 50], ["VOO", 50]],
                                            [["NVDA", 62], ["VOO", 38]], self.TH)
        self.assertTrue(material)
        self.assertIn("NVDA 50->62%", reason)

    def test_small_shift_under_threshold(self):
        material, _ = o._mandate_drift([["NVDA", 50], ["VOO", 50]],
                                       [["NVDA", 58], ["VOO", 42]], self.TH)
        self.assertFalse(material)   # 8pp < 12pp

    def test_new_position_of_real_size(self):
        material, reason = o._mandate_drift([["NVDA", 92]], [["NVDA", 92], ["AVGO", 8]], self.TH)
        self.assertTrue(material)
        self.assertIn("new AVGO", reason)

    def test_tiny_new_position_ignored(self):
        material, _ = o._mandate_drift([["NVDA", 96]], [["NVDA", 96], ["X", 4]], self.TH)
        self.assertFalse(material)   # 4% below the real-size floor

    def test_exited_position(self):
        material, reason = o._mandate_drift([["NVDA", 88], ["TSLA", 12]], [["NVDA", 100]], self.TH)
        self.assertTrue(material)
        self.assertIn("exited TSLA", reason)

    def test_none_baseline_treats_all_as_new(self):
        # With no baseline every position looks new — which is why run_mandate_drift guards
        # `prev is None` and baselines instead of calling this. Documenting that contract.
        material, _ = o._mandate_drift(None, [["NVDA", 100]], self.TH)
        self.assertTrue(material)


class DriftFlowTests(unittest.TestCase):
    def setUp(self):
        repo.judge_summary()  # force _ensure_ready / create_all
        with db_session() as s:
            s.execute(delete(ResearchEventRecord))
            s.execute(delete(MandatePlanStateRecord))
        self._gm, self._gp, self._mr = o._mandate.get_mandate, o.get_portfolio, o.mandate_review
        o._mandate.get_mandate = lambda: Mandate(statement="long-term growth, hold 1y+")
        o.mandate_review = lambda force=False: {"alignment": "Book leans heavy on one name.",
                                                 "moves": [{"ticker": "NVDA", "action": "trim"}]}
        self._pf_now = _pf({"NVDA": 50, "VOO": 50})
        o.get_portfolio = lambda: self._pf_now

    def tearDown(self):
        o._mandate.get_mandate, o.get_portfolio, o.mandate_review = self._gm, self._gp, self._mr

    def _events(self):
        return repo.recent_events(event_types=["mandate_plan"])

    def test_first_run_baselines_without_firing(self):
        fired = o.run_mandate_drift()
        self.assertFalse(fired)
        self.assertEqual(len(self._events()), 0)
        self.assertEqual(repo.load_mandate_plan_sig(), [["NVDA", 50], ["VOO", 50]])

    def test_material_drift_fires_then_cools_down(self):
        o.run_mandate_drift()                       # baseline 50/50
        self._pf_now = _pf({"NVDA": 80, "VOO": 20})  # +30pp on NVDA
        fired = o.run_mandate_drift()
        self.assertTrue(fired)
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertIn("moved", evs[0]["title"].lower())
        self.assertIn("NVDA", evs[0]["summary"])
        self.assertEqual(repo.load_mandate_plan_sig(), [["NVDA", 80], ["VOO", 20]])

        # Drift again within the cooldown window: must NOT fire, but absorbs the new shape.
        self._pf_now = _pf({"NVDA": 95, "VOO": 5})
        fired2 = o.run_mandate_drift()
        self.assertFalse(fired2)
        self.assertEqual(len(self._events()), 1)     # still just the one
        self.assertEqual(repo.load_mandate_plan_sig(), [["NVDA", 95], ["VOO", 5]])

    def test_no_drift_does_not_fire(self):
        o.run_mandate_drift()                        # baseline
        fired = o.run_mandate_drift()                # same book
        self.assertFalse(fired)
        self.assertEqual(len(self._events()), 0)

    def test_no_mandate_is_noop(self):
        o._mandate.get_mandate = lambda: Mandate()   # unset
        self.assertFalse(o.run_mandate_drift())
        self.assertIsNone(repo.load_mandate_plan_sig())


if __name__ == "__main__":
    unittest.main()
