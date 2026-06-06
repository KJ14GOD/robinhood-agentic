"""Tests for the assistant's control tools — the add/remove plumbing that lets the
user drive the app by chat. Guards the upsert-only persistence bug: removing a
watchlist name or thesis must actually stick across a reload, not reappear.

Uses a throwaway SQLite DB so real data is never touched.
Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain import research_state  # noqa: E402
from brain.models import TradeTicket  # noqa: E402


class WatchlistRemovalTests(unittest.TestCase):
    def _tickers(self):
        return [w.ticker for w in research_state.load_state().watchlist]

    def test_add_then_remove_persists(self):
        research_state.save_watch_item("NVDA", "ai compute", "balanced")
        self.assertIn("NVDA", self._tickers())
        self.assertTrue(research_state.remove_watch_item("NVDA"))
        # the real bug: it must stay gone after a fresh load, not reappear
        self.assertNotIn("NVDA", self._tickers())

    def test_remove_absent_is_false(self):
        self.assertFalse(research_state.remove_watch_item("ZZZZ"))

    def test_set_and_clear_watch_target(self):
        research_state.set_watch_target("AMD", 140.0)
        item = next(w for w in research_state.load_state().watchlist if w.ticker == "AMD")
        self.assertEqual(item.target_entry, 140.0)
        research_state.set_watch_target("AMD", 0)  # clear
        item = next(w for w in research_state.load_state().watchlist if w.ticker == "AMD")
        self.assertEqual(item.target_entry, 0.0)


class ThesisRemovalTests(unittest.TestCase):
    def test_drop_thesis_persists(self):
        research_state.update_from_ticket(TradeTicket(
            ticker="APLD", action="buy", conviction=7, thesis="datacenter buildout",
            catalyst="hyperscaler lease", risks="dilution"))
        self.assertIn("APLD", research_state.load_state().theses)
        self.assertTrue(research_state.remove_thesis("APLD"))
        self.assertNotIn("APLD", research_state.load_state().theses)  # stays gone on reload

    def test_drop_absent_thesis_is_false(self):
        self.assertFalse(research_state.remove_thesis("ZZZZ"))


if __name__ == "__main__":
    unittest.main()
