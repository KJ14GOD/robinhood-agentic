"""Tests for the agentic researcher loop (brain/engines/researcher.py).

The Anthropic client is mocked with a scripted sequence of responses so we can exercise the loop
deterministically: it executes a client-side EDGAR tool, handles a server-side web_search +
pause_turn resume (carrying the code-execution container id forward), and returns the final
dossier with a faithful trace. No network.
"""
import unittest

from brain.engines import researcher
from brain.models import RiskProfile


class _Block:
    def __init__(self, type, text="", name="", input=None, id="", content=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input or {}
        self.id = id
        self.content = content if content is not None else []


class _Container:
    def __init__(self, cid):
        self.id = cid


class _Resp:
    def __init__(self, content, stop_reason, container=None):
        self.content = content
        self.stop_reason = stop_reason
        self.container = container


class _Messages:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script[len(self.calls) - 1]


class _Client:
    def __init__(self, script):
        self.messages = _Messages(script)


# Captured at import (collection) time, before any other test patches the module global.
_REAL_INVESTIGATE = researcher.investigate


class ResearcherTests(unittest.TestCase):
    def setUp(self):
        researcher.investigate = _REAL_INVESTIGATE  # undo any cross-test monkeypatch
        researcher.edgar.facts_as_prompt = lambda t: "Reported revenue $60B, net income $30B (SEC XBRL)."

    def test_full_investigation_loop(self):
        script = [
            # 1) model reads primary financials (client tool)
            _Resp([_Block("text", "Planning: check reported financials first."),
                   _Block("tool_use", name="get_company_financials", input={"ticker": "NVDA"}, id="t1")],
                  stop_reason="tool_use", container=_Container("cid-1")),
            # 2) model runs a web search (server tool) and the API pauses mid-loop
            _Resp([_Block("server_tool_use", name="web_search", input={"query": "NVDA data center demand 2026"}),
                   _Block("web_search_tool_result", input={})],
                  stop_reason="pause_turn"),
            # 3) resumes and writes the dossier
            _Resp([_Block("text", "RESEARCH DOSSIER\nFindings: demand strong (Reuters, high confidence). "
                                  "Disagreements: one outlet flags bubble risk.")],
                  stop_reason="end_turn"),
        ]
        client = _Client(script)
        researcher.llm.client = lambda: client

        out = researcher.investigate("NVDA", RiskProfile())

        # the final dossier is returned
        self.assertIn("RESEARCH DOSSIER", out["dossier"])
        self.assertIn("Disagreements", out["dossier"])
        # both the client EDGAR tool and the server web search were used
        self.assertIn("get_company_financials", out["tools_used"])
        self.assertIn("web_search", out["tools_used"])
        # the EDGAR tool actually executed and its result is in the trace
        results = [s for s in out["trace"] if s["type"] == "tool_result" and s["name"] == "get_company_financials"]
        self.assertTrue(results and "revenue" in results[0]["summary"].lower())

    def test_web_search_container_carried_forward(self):
        # First response opens a container; the next request must reference it (pause_turn resume).
        script = [
            _Resp([_Block("server_tool_use", name="web_search", input={"query": "q"})],
                  stop_reason="pause_turn", container=_Container("cid-X")),
            _Resp([_Block("text", "DOSSIER: done.")], stop_reason="end_turn"),
        ]
        client = _Client(script)
        researcher.llm.client = lambda: client

        researcher.investigate("NVDA", RiskProfile())

        # second create() call must carry container="cid-X"
        self.assertGreaterEqual(len(client.messages.calls), 2)
        self.assertEqual(client.messages.calls[1].get("container"), "cid-X")


if __name__ == "__main__":
    unittest.main()
