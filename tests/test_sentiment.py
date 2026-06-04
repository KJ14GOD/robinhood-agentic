"""Tests for the social-sentiment layer: the prompt formatter and its best-effort
contract. Network is never touched — `get_sentiment` is stubbed.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import unittest

from brain.data import sentiment


class SentimentPromptTests(unittest.TestCase):
    def tearDown(self):
        # restore the real implementation so other tests/modules aren't affected
        import importlib
        importlib.reload(sentiment)

    def test_formats_both_axes(self):
        sentiment.get_sentiment = lambda t: {
            "ticker": t, "bullish_pct": 81, "tagged": 21,
            "mentions": 96, "mentions_prev": 80, "mention_delta_pct": 20, "rank": 16,
        }
        line = sentiment.sentiment_prompt("RKLB")
        self.assertIn("81% bullish", line)
        self.assertIn("mentions 96", line)
        self.assertIn("+20% vs yesterday", line)
        self.assertIn("not fact", line.lower())  # labeled secondary

    def test_empty_when_no_data(self):
        sentiment.get_sentiment = lambda t: None
        self.assertEqual(sentiment.sentiment_prompt("X"), "")

    def test_best_effort_swallows_errors(self):
        def boom(t):
            raise RuntimeError("source down")
        sentiment.get_sentiment = boom
        self.assertEqual(sentiment.sentiment_prompt("X"), "")  # never raises


if __name__ == "__main__":
    unittest.main()
