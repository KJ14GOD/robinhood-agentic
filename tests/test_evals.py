"""Tests for the eval layer — labeling brain traces and the emerging failure taxonomy.
Uses a throwaway SQLite DB so real data is never touched.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain import evals  # noqa: E402
from brain.db import repository as repo  # noqa: E402


class TaxonomyTests(unittest.TestCase):
    def test_seed_taxonomy_present(self):
        ids = {m["id"] for m in evals.taxonomy()}
        self.assertIn("weak_grounding", ids)
        self.assertIn("not_falsifiable", ids)

    def test_normalize_and_pretty(self):
        self.assertEqual(evals.normalize_tag("Weak Grounding"), "weak_grounding")
        self.assertEqual(evals.normalize_tag("  anchored on PRICE  "), "anchored_on_price")
        self.assertEqual(evals.pretty("weak_grounding"), "Weak grounding")     # seed label
        self.assertEqual(evals.pretty("anchored_on_price"), "Anchored on price")  # fallback


class EvalLabelTests(unittest.TestCase):
    def test_label_upsert_and_summary(self):
        repo.save_eval_label("runA", "analyst", "RKLB", "flawed",
                             ["weak_grounding", "overconfident"], "no source for the +168% claim")
        repo.save_eval_label("runB", "rejudge", "APLD", "good", [], "")
        s = repo.eval_summary()
        self.assertEqual(s["labeled"], 2)
        self.assertEqual(s["verdicts"].get("flawed"), 1)
        self.assertEqual(s["verdicts"].get("good"), 1)
        top = {f["tag"] for f in s["failure_counts"]}
        self.assertEqual(top, {"weak_grounding", "overconfident"})

        # re-labeling the same run updates in place (one label per run), not a dup
        repo.save_eval_label("runA", "analyst", "RKLB", "mixed", ["weak_grounding"], "revised")
        s2 = repo.eval_summary()
        self.assertEqual(s2["labeled"], 2)                       # still 2 runs, not 3
        self.assertEqual(s2["verdicts"].get("flawed", 0), 0)     # flawed -> mixed
        self.assertEqual(s2["verdicts"].get("mixed"), 1)

    def test_labels_by_run(self):
        repo.save_eval_label("runC", "deep_research", "NVDA", "good", ["generic"], "fine")
        got = repo.eval_labels_by_run(["runC", "missing"])
        self.assertIn("runC", got)
        self.assertEqual(got["runC"]["verdict"], "good")
        self.assertEqual(got["runC"]["failure_modes"], ["generic"])
        self.assertNotIn("missing", got)


if __name__ == "__main__":
    unittest.main()
