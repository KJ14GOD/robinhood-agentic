"""Tests for deep research mode: the draft -> self-critique flow settles the final
call, updates the stored thesis, logs to the shadow track record under its own
source, and writes an audit trail to agent_runs. LLM and market data are mocked.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

import brain.config as bconfig  # noqa: E402

bconfig.SHADOW_PATH = Path(_TMP.name + ".no-ledger.jsonl")  # keep the real ledger untouched

from brain import shadow  # noqa: E402
from brain.data.prices import Quote, TrendSignals  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.engines import deep_research as dr  # noqa: E402
from brain.models import DeepResearchCritique, DeepResearchDraft, RiskProfile  # noqa: E402


class _Chart:
    def summary(self):
        return "NVDA 6m chart: +20% across 26 points."


def _fake_signals(ticker, refresh=False):
    return TrendSignals(ticker=ticker, price=100.0, sector="Technology", rsi_14=60.0)


def _fake_news(ticker, limit=8):
    return "Recent headlines:\n- AI demand stays strong (Reuters)"


def _fake_chart(ticker, span="6m", refresh=False):
    return _Chart()


def _fake_quote(ticker, refresh=False):
    return Quote(ticker=ticker, price=100.0)


def _fake_parse(prompt, schema, max_tokens=4000, **kw):
    if schema is DeepResearchDraft:
        return DeepResearchDraft(
            plan=["demand durability?", "valuation vs growth?"],
            bull_case=["category leader"], bear_case=["priced for perfection"],
            evidence=["RSI 60, +20% over 6m"], thesis="AI compute leader",
            catalyst="next earnings", risks="data-center demand stalls",
            action="buy", conviction=7, suggested_size_pct=4.0)
    if schema is DeepResearchCritique:
        return DeepResearchCritique(
            critique=["bull leans on momentum more than fundamentals"],
            holds_up=True, final_action="hold", final_conviction=6,
            note="trimmed to HOLD after weighing valuation")
    raise AssertionError(f"unexpected schema {schema}")


class DeepResearchTests(unittest.TestCase):
    def setUp(self):
        dr.llm.parse = _fake_parse
        dr.get_signals = _fake_signals
        dr.headlines_as_prompt = _fake_news
        dr.get_chart = _fake_chart
        shadow.get_quote = _fake_quote
        self.profile = RiskProfile()

    def test_report_reflects_self_critique(self):
        rep = dr.run("NVDA", self.profile)
        for k in ("ticker", "plan", "bull_case", "bear_case", "evidence", "critique",
                  "verdict", "action", "conviction", "thesis", "invalidation", "changed"):
            self.assertIn(k, rep)
        # the final call comes from the critique pass (HOLD/6), not the draft (buy/7)
        self.assertEqual(rep["action"], "hold")
        self.assertEqual(rep["conviction"], 6)
        self.assertEqual(rep["verdict"], "HOLD")
        self.assertTrue(rep["changed"])

    def test_updates_thesis_logs_shadow_and_audits(self):
        dr.run("NVDA", self.profile)

        # stored thesis / ticker research updated (written straight to the DB)
        tr = repo.get_ticker_research("NVDA")
        self.assertIsNotNone(tr)
        self.assertEqual(tr["action_label"], "HOLD")
        self.assertEqual(tr["thesis"], "AI compute leader")

        # logged to the shadow track record under its own source
        self.assertIn("NVDA", repo.open_shadow_tickers("deep_research"))

        # audit trail persisted
        runs = repo.recent_agent_runs(limit=5, kind="deep_research")
        self.assertTrue(runs)
        self.assertEqual(runs[0]["tools_used"], "get_stock_signals,get_stock_news,get_stock_chart")
        self.assertIn("DEEP RESEARCH", runs[0]["answer"])


if __name__ == "__main__":
    unittest.main()
