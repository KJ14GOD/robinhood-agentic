"""Tests for autonomous deep research (brain/engines/autoresearch.py).

Verifies the gating that keeps this expensive engine safe: only high-signal triggers fire a dive
(thesis broke/under review, mission promoted to BUY — not routine signals), the highest-priority
name goes first, the per-cycle cap holds, each completed dive logs a `deep_dive` ping event, and
the per-ticker cooldown blocks a repeat. `deep_research.run` is mocked — no LLM, no network.
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.db import repository as db_repo  # noqa: E402
from brain.db.session import db_session  # noqa: E402
from brain.db.models import ResearchEventRecord  # noqa: E402
from brain.engines import autoresearch  # noqa: E402
from brain.models import RiskProfile  # noqa: E402


def _seed(event_type, ticker, severity="info"):
    db_repo.save_research_event(event_type=event_type, ticker=ticker, severity=severity,
                                title=f"{ticker} {event_type}", summary="x", source="test")


# Captured at import (collection) time so tearDown can undo our monkeypatch and not leak the fake
# into other test modules (e.g. test_deep_research, which exercises the real run()).
_REAL_DR_RUN = autoresearch.deep_research.run


class AutoResearchTests(unittest.TestCase):
    def setUp(self):
        db_repo.recent_events(limit=1)        # force DB init / table creation
        with db_session() as s:               # clean event slate per test
            s.query(ResearchEventRecord).delete()
        self.calls = []

        def _fake_run(ticker, profile):
            self.calls.append(ticker)
            return {"action": "buy", "verdict": "BUY", "conviction": 7,
                    "note": f"auto note {ticker}", "thesis": "t"}

        autoresearch.deep_research.run = _fake_run
        self.profile = RiskProfile()

    def tearDown(self):
        autoresearch.deep_research.run = _REAL_DR_RUN

    def test_highest_priority_dives_first_and_cap_holds(self):
        _seed("thesis_review", "REV", "warn")     # priority 2
        _seed("mission_update", "BUYNAME", "warn")  # priority 1 (BUY promotion)
        _seed("thesis_broken", "BRK", "alert")    # priority 3 — should go first

        done = autoresearch.run_due_dives(self.profile, max_per_cycle=1)

        self.assertEqual(self.calls, ["BRK"])           # only the top-priority name, cap respected
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["ticker"], "BRK")
        # the dive logged a deep_dive ping event, actionable verdict → warn
        self.assertTrue(db_repo.event_exists_recent("deep_dive", "BRK", within_hours=72.0))

    def test_non_triggers_are_ignored(self):
        _seed("overbought", "OB", "info")          # routine signal — never dives
        _seed("mission_update", "RELABEL", "info")  # mission relabel, not a BUY — never dives

        done = autoresearch.run_due_dives(self.profile, max_per_cycle=5)

        self.assertEqual(self.calls, [])
        self.assertEqual(done, [])

    def test_cooldown_blocks_repeat_dive(self):
        _seed("thesis_broken", "BRK", "alert")
        autoresearch.run_due_dives(self.profile, max_per_cycle=1)   # dives BRK, logs deep_dive
        self.calls.clear()

        # trigger still present, but the deep_dive cooldown should now block a repeat
        again = autoresearch.run_due_dives(self.profile, max_per_cycle=1)
        self.assertEqual(self.calls, [])
        self.assertEqual(again, [])

    def test_drains_backlog_across_cycles_within_cap(self):
        _seed("thesis_broken", "AAA", "alert")
        _seed("thesis_broken", "BBB", "alert")
        # cap = 1, two triggered names → exactly one dive this cycle
        done = autoresearch.run_due_dives(self.profile, max_per_cycle=1)
        self.assertEqual(len(done), 1)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
