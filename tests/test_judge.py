"""Tests for the eval layer Phase 2 — the LLM-as-judge and the self-revision gate.

Offline: the model call (`llm.parse`) is stubbed with scripted structured outputs, and a
throwaway SQLite DB backs the persistence so real data is never touched. The stub is installed
in setUp and restored in tearDown so it can't leak into other test modules.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")  # import-safe; the model call is stubbed

from sqlalchemy import delete  # noqa: E402

from brain import config, evals, llm  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.db.models import EvalJudgementRecord, EvalLabelRecord  # noqa: E402
from brain.db.session import db_session  # noqa: E402
from brain.engines import judge  # noqa: E402
from brain.models import GroundingCheck, JudgeAssessment, RiskProfile, TradeTicket  # noqa: E402


def _ticket(**kw) -> TradeTicket:
    base = dict(ticker="NVDA", action="buy", conviction=8,
                thesis="Data-center demand keeps compounding.",
                catalyst="Earnings in three weeks.", risks="A capex air-pocket.",
                suggested_size_pct=5.0)
    base.update(kw)
    return TradeTicket(**base)


def _assess(verdict="good", score=85, modes=None, fix="") -> JudgeAssessment:
    return JudgeAssessment(verdict=verdict, score=score, failure_modes=modes or [],
                           grounding=[GroundingCheck(claim="demand", supported=True, note="cited")],
                           rationale="Reads fine.", fix=fix)


class StubLLM:
    """Installs a scripted llm.parse that returns queued objects in call order."""
    def __init__(self, responses):
        self._seq = iter(responses)
        self.calls = 0
        self._orig = llm.parse

    def __enter__(self):
        def _fake(prompt, schema, max_tokens=4000, effort=None):
            self.calls += 1
            return next(self._seq)
        llm.parse = _fake
        return self

    def __exit__(self, *exc):
        llm.parse = self._orig


class TaxonomyGateHelperTests(unittest.TestCase):
    def test_load_bearing_split(self):
        self.assertTrue(evals.is_load_bearing(["weak_grounding"]))
        self.assertTrue(evals.is_load_bearing(["Not Falsifiable"]))   # normalized
        self.assertFalse(evals.is_load_bearing(["vague", "generic"]))
        self.assertFalse(evals.is_load_bearing([]))

    def test_taxonomy_prompt_lists_modes(self):
        p = evals.taxonomy_prompt()
        self.assertIn("weak_grounding:", p)
        self.assertIn("not_falsifiable:", p)


class GateTests(unittest.TestCase):
    def setUp(self):
        self._je, self._sc = config.JUDGE_ENABLED, config.SELF_CRITIQUE
        config.JUDGE_ENABLED, config.SELF_CRITIQUE = True, True

    def tearDown(self):
        config.JUDGE_ENABLED, config.SELF_CRITIQUE = self._je, self._sc

    def test_good_call_is_not_revised(self):
        with StubLLM([_assess("good", 88)]) as s:
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertFalse(revised)
        self.assertEqual(a.verdict, "good")
        self.assertEqual(s.calls, 1)  # judged once, no repair

    def test_flawed_load_bearing_self_revises(self):
        flawed = _assess("flawed", 35, ["weak_grounding"], fix="Cite the demand number.")
        fixed = _ticket(conviction=6, thesis="Demand cited from the 10-Q.")
        better = _assess("mixed", 72)
        with StubLLM([flawed, fixed, better]) as s:
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertTrue(revised)
        self.assertEqual(t.conviction, 6)          # the repaired ticket shipped
        self.assertEqual(a.score, 72)              # re-scored
        self.assertEqual(s.calls, 3)               # judge, repair, re-judge

    def test_soft_flaw_does_not_gate(self):
        with StubLLM([_assess("flawed", 40, ["vague", "generic"])]) as s:
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertFalse(revised)                  # not a load-bearing failure mode
        self.assertEqual(s.calls, 1)

    def test_revision_discarded_if_worse(self):
        flawed = _assess("flawed", 35, ["overconfident"])
        fixed = _ticket(conviction=4)
        worse = _assess("flawed", 20, ["overconfident"])
        with StubLLM([flawed, fixed, worse]):
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertFalse(revised)                  # re-judge worse → keep original
        self.assertEqual(t.conviction, 8)          # original ticket
        self.assertEqual(a.score, 35)

    def test_self_critique_off_means_score_only(self):
        config.SELF_CRITIQUE = False
        with StubLLM([_assess("flawed", 30, ["weak_grounding"])]) as s:
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertFalse(revised)
        self.assertEqual(s.calls, 1)               # judged, never repaired

    def test_judge_disabled_passes_through(self):
        config.JUDGE_ENABLED = False
        with StubLLM([_assess("good")]) as s:
            t, a, revised = judge.gate_ticket(_ticket(), RiskProfile())
        self.assertIsNone(a)
        self.assertFalse(revised)
        self.assertEqual(s.calls, 0)               # no model call at all


class TraceReconstructionTests(unittest.TestCase):
    def test_block_from_analyst_trace(self):
        run = {"kind": "analyst", "query": "Analyze NVDA",
               "steps": [{"ticker": "NVDA", "action": "buy", "label": "BUY CANDIDATE",
                          "conviction": 7, "thesis": "Compounding demand.", "risks": "Capex."}]}
        block, tk = judge.block_from_trace(run)
        self.assertEqual(tk, "NVDA")
        self.assertIn("Compounding demand.", block)
        self.assertIn("BUY", block.upper())

    def test_block_from_deep_research_trace(self):
        run = {"kind": "deep_research", "query": "Deep research: AMD",
               "steps": [{"report": {"ticker": "AMD", "action": "hold", "verdict": "HOLD",
                                     "conviction": 5, "thesis": "Fairly valued.",
                                     "bull_case": ["share gains"], "bear_case": ["margin"]}}]}
        block, tk = judge.block_from_trace(run)
        self.assertEqual(tk, "AMD")
        self.assertIn("Fairly valued.", block)
        self.assertIn("share gains", block)

    def test_block_ticker_falls_back_to_query(self):
        run = {"kind": "rejudge", "query": "Re-judge TSLA", "steps": [{"reason": "Still holds."}]}
        block, tk = judge.block_from_trace(run)
        self.assertEqual(tk, "TSLA")
        self.assertIn("Still holds.", block)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        repo.judge_summary()  # forces _ensure_ready / create_all
        with db_session() as s:
            s.execute(delete(EvalJudgementRecord))
            s.execute(delete(EvalLabelRecord))

    def test_record_roundtrip_and_summary(self):
        judge.record("run-aaa", "analyst", "NVDA", _assess("good", 90), revised=False)
        judge.record("run-bbb", "deep_research", "AMD", _assess("flawed", 40, ["weak_grounding"]), revised=True)
        got = repo.eval_judgements_by_run(["run-aaa", "run-bbb"])
        self.assertEqual(got["run-aaa"]["score"], 90)
        self.assertTrue(got["run-bbb"]["revised"])
        self.assertIn("weak_grounding", got["run-bbb"]["failure_modes"])

        s = repo.judge_summary()
        self.assertEqual(s["judged"], 2)
        self.assertEqual(s["verdicts"].get("good"), 1)
        self.assertEqual(s["avg_score"], 65)        # (90 + 40) / 2
        self.assertEqual(s["revised"], 1)
        self.assertEqual(s["failure_counts"][0]["tag"], "weak_grounding")

    def test_upsert_latest_judgement_wins(self):
        judge.record("run-ccc", "analyst", "NVDA", _assess("flawed", 30), revised=False)
        judge.record("run-ccc", "analyst", "NVDA", _assess("good", 80), revised=True)
        got = repo.eval_judgements_by_run(["run-ccc"])
        self.assertEqual(got["run-ccc"]["verdict"], "good")
        self.assertEqual(got["run-ccc"]["score"], 80)
        self.assertEqual(repo.judge_summary()["judged"], 1)   # one row, not two

    def test_agreement_with_human_labels(self):
        # Two traces: judge agrees with the human on one, disagrees on the other → 50%.
        judge.record("run-1", "analyst", "NVDA", _assess("good", 85), revised=False)
        judge.record("run-2", "analyst", "AMD", _assess("good", 80), revised=False)
        repo.save_eval_label("run-1", "analyst", "NVDA", "good", [], "agree")
        repo.save_eval_label("run-2", "analyst", "AMD", "flawed", ["overconfident"], "disagree")
        s = repo.judge_summary()
        self.assertEqual(s["agreement_n"], 2)
        self.assertEqual(s["agreement"], 50)

    def test_unjudged_run_ids_excludes_scored(self):
        repo.save_agent_run(query="Analyze X", kind="analyst", run_id="ar-1",
                            steps=[{"ticker": "X", "thesis": "t"}])
        repo.save_agent_run(query="Analyze Y", kind="analyst", run_id="ar-2",
                            steps=[{"ticker": "Y", "thesis": "t"}])
        judge.record("ar-1", "analyst", "X", _assess("good"), revised=False)
        pending = repo.unjudged_run_ids(limit=10)
        ids = {p["id"] for p in pending}
        self.assertIn("ar-2", ids)
        self.assertNotIn("ar-1", ids)   # already judged


if __name__ == "__main__":
    unittest.main()
