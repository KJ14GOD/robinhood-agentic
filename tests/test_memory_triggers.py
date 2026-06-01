"""Unit tests for the news + earnings thesis triggers in living memory.

These cover the cheap pre-checks that decide whether a stored thesis is worth a
(gated) LLM re-judgement: a news headline hitting a named driver, or earnings
landing in the soon-window. LLM and data sources are mocked.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.data.news import Headline  # noqa: E402
from brain.engines import memory  # noqa: E402
from brain.models import Thesis  # noqa: E402


def _thesis():
    return Thesis(ticker="APLD", thesis="generic prose about the company",
                  invalidation="dilution via equity issuance at elevated prices",
                  strengthens=["second hyperscaler lease signed"], weakens=[])


class TriggerTests(unittest.TestCase):
    def test_thesis_terms_pulls_drivers_not_stopwords(self):
        terms = memory._thesis_terms(_thesis())
        self.assertIn("lease", terms)
        self.assertIn("equity", terms)
        self.assertIn("hyperscaler", terms)
        self.assertNotIn("the", terms)
        self.assertNotIn("via", terms)

    def test_news_trigger_fires_on_driver_match(self):
        memory.get_news = lambda ticker, limit=6: [
            Headline(title="APLD signs second hyperscaler lease", publisher="Reuters", published="", link="")]
        self.assertIsNotNone(memory._news_trigger(_thesis()))

    def test_news_trigger_silent_without_match(self):
        memory.get_news = lambda ticker, limit=6: [
            Headline(title="Markets drift on light holiday volume", publisher="AP", published="", link="")]
        self.assertIsNone(memory._news_trigger(_thesis()))

    def test_earnings_trigger_window(self):
        memory.get_earnings_date = lambda t, refresh=False: date.today() + timedelta(days=2)
        self.assertIn("2 days", memory._earnings_trigger("NVDA"))

        memory.get_earnings_date = lambda t, refresh=False: date.today()
        self.assertEqual(memory._earnings_trigger("NVDA"), "reports earnings today")

        memory.get_earnings_date = lambda t, refresh=False: date.today() + timedelta(days=30)
        self.assertIsNone(memory._earnings_trigger("NVDA"))

        memory.get_earnings_date = lambda t, refresh=False: None
        self.assertIsNone(memory._earnings_trigger("NVDA"))

    def test_stale_trigger(self):
        from datetime import datetime, timezone, timedelta
        old = Thesis(ticker="X", updated_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
        fresh = Thesis(ticker="X", updated_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.assertIsNotNone(memory._stale_trigger(old))
        self.assertIsNone(memory._stale_trigger(fresh))


if __name__ == "__main__":
    unittest.main()
