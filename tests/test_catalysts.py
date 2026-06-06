"""Tests for the catalyst radar's prompt formatter and best-effort contract.
Network is never touched — `get_company_news` is stubbed.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timezone, timedelta

from brain.data import catalysts
from brain.data.catalysts import Catalyst


def _cat(headline, hours_ago, source="Reuters"):
    return Catalyst(
        ticker="RKLB", headline=headline, summary="body", source=source,
        url="https://example.com/x", category="company news",
        dt=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


class CatalystPromptTests(unittest.TestCase):
    def tearDown(self):
        import importlib
        importlib.reload(catalysts)

    def test_prompt_lists_dated_headlines(self):
        catalysts.get_company_news = lambda t, days=14: [
            _cat("Rocket Lab wins $90M Space Force award", 3),
            _cat("Neutron first flight slips to Q3", 30)]
        line = catalysts.catalysts_prompt("RKLB")
        self.assertIn("Space Force", line)
        self.assertIn("Neutron", line)
        self.assertIn("RECENT CATALYSTS", line)

    def test_empty_when_no_news(self):
        catalysts.get_company_news = lambda t, days=14: []
        self.assertEqual(catalysts.catalysts_prompt("RKLB"), "")

    def test_latest_fresh_respects_window(self):
        catalysts.get_company_news = lambda t, days=7: [
            _cat("brand new", 2), _cat("stale", 50)]
        self.assertIsNotNone(catalysts.latest_fresh("RKLB", within_hours=6))   # 2h < 6h
        # newest item is 2h old; a 1h window should find nothing fresh
        self.assertIsNone(catalysts.latest_fresh("RKLB", within_hours=1))

    def test_best_effort_swallows_errors(self):
        def boom(t, days=14):
            raise RuntimeError("source down")
        catalysts.get_company_news = boom
        # the formatter reads get_company_news directly; guard against a raise
        try:
            out = catalysts.catalysts_prompt("RKLB")
        except Exception:
            out = "RAISED"
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
