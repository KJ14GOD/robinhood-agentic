"""Integration tests for the event stream: repository persistence + dedup +
reader, and the orchestrator's Today ranking. Uses a throwaway SQLite DB so the
real data store is never touched.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

# Point the whole stack at a temp DB BEFORE importing brain (config reads env at import).
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.db import repository as repo  # noqa: E402
from brain import orchestrator  # noqa: E402


def tearDownModule():
    # Intentionally do NOT unlink mid-run: when the whole suite runs in one
    # process all modules share this DB, and deleting it here would pull the rug
    # out from under later modules. The OS cleans up /tmp.
    pass


class EventRepoTests(unittest.TestCase):
    def test_save_read_dedup(self):
        repo.save_research_event(event_type="concentration", ticker="APLD", severity="warn",
                                 title="APLD is 31% of your portfolio", summary="over line", source="monitor")
        repo.save_research_event(event_type="drawdown", ticker="NVDA", severity="alert",
                                 title="NVDA down 35%", summary="check", source="monitor")

        evs = repo.recent_events(limit=10)
        # at least the two we just wrote, newest first
        tickers = [e["ticker"] for e in evs]
        self.assertIn("APLD", tickers)
        self.assertIn("NVDA", tickers)

        # dedup window logic
        self.assertTrue(repo.event_exists_recent("concentration", "APLD", within_hours=12))
        self.assertFalse(repo.event_exists_recent("overbought", "APLD", within_hours=12))
        # a zero-hour window puts the cutoff in the future -> nothing counts as recent
        self.assertFalse(repo.event_exists_recent("concentration", "APLD", within_hours=0))

    def test_event_type_filter(self):
        repo.save_research_event(event_type="oversold", ticker="KO", severity="info",
                                 title="KO oversold", summary="", source="monitor")
        only = repo.recent_events(limit=50, event_types=["oversold"])
        self.assertTrue(only)
        self.assertTrue(all(e["event_type"] == "oversold" for e in only))

    def test_today_events_ranks_alert_first(self):
        # alert should sort ahead of warn/info regardless of write order
        out = orchestrator.today_events(limit=50)
        sevs = [e["severity"] for e in out["events"]]
        if "alert" in sevs and "info" in sevs:
            self.assertLess(sevs.index("alert"), sevs.index("info"))
        self.assertEqual(out["count"], len(out["events"]))


if __name__ == "__main__":
    unittest.main()
