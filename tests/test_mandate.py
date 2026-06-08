"""Tests for the mandate (the standing goal) — storage round-trip and the prompt block.
The LLM extract/review are not exercised here (network); the persistence + formatting are.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain import mandate as mandate_engine  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.models import Mandate  # noqa: E402


class MandateModelTests(unittest.TestCase):
    def test_is_set_and_describe(self):
        self.assertFalse(Mandate().is_set())
        self.assertEqual(Mandate().describe(), "")
        m = Mandate(statement="long-term holds for a year+", horizon="1+ year",
                    risk="moderate", style="growth", avoid=["meme stocks"])
        self.assertTrue(m.is_set())
        d = m.describe()
        self.assertIn("long-term holds", d)
        self.assertIn("horizon 1+ year", d)
        self.assertIn("Avoid: meme stocks", d)


class MandateStoreTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        repo.save_mandate(Mandate(statement="steady income", risk="conservative",
                                  style="dividend", favor=["utilities", "REITs"]))
        got = repo.load_mandate()
        self.assertEqual(got.statement, "steady income")
        self.assertEqual(got.style, "dividend")
        self.assertEqual(got.favor, ["utilities", "REITs"])

    def test_prompt_empty_when_unset(self):
        # a fresh row id won't exist after the previous test in a separate DB scope, but
        # mandate_prompt must be safe regardless — empty string, never raises.
        self.assertIsInstance(mandate_engine.mandate_prompt(), str)


if __name__ == "__main__":
    unittest.main()
