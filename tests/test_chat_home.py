"""Tests for the chat-first Home: persisted conversation + the agent's set_mandate tool.
Uses a throwaway SQLite DB so real data is never touched.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain import agent  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.models import Mandate  # noqa: E402


class ChatPersistenceTests(unittest.TestCase):
    def test_save_and_read_roundtrip_in_chat_order(self):
        repo.save_chat_message("user", "what should I do with ONDS?")
        repo.save_chat_message("assistant", "Trim it — 18% of the book is too much.")
        msgs = repo.recent_chat_messages(limit=10)
        self.assertGreaterEqual(len(msgs), 2)
        self.assertEqual(msgs[-2]["role"], "user")
        self.assertEqual(msgs[-1]["role"], "assistant")
        self.assertIn("Trim it", msgs[-1]["content"])
        self.assertTrue(msgs[-1]["created_at"])          # ISO stamp for stream ordering
        # oldest first (chat order)
        self.assertLessEqual(msgs[0]["created_at"], msgs[-1]["created_at"])

    def test_blank_messages_are_not_persisted(self):
        before = len(repo.recent_chat_messages(limit=200))
        repo.save_chat_message("assistant", "   ")
        self.assertEqual(len(repo.recent_chat_messages(limit=200)), before)


class SetMandateToolTests(unittest.TestCase):
    def test_tool_is_registered(self):
        names = {t["name"] for t in agent.TOOLS if isinstance(t, dict) and "name" in t}
        self.assertIn("set_mandate", names)
        self.assertIn("set_mandate", agent._DISPATCH)

    def test_dispatch_sets_mandate(self):
        fake = Mandate(statement="long-term holds, a year plus", summary="Long-term, low churn")
        with mock.patch("brain.mandate.set_mandate", return_value=fake) as m:
            out = agent._execute("set_mandate", {"statement": "long-term holds, a year plus"})
        m.assert_called_once_with("long-term holds, a year plus")
        self.assertIn("Long-term, low churn", out)

    def test_dispatch_rejects_empty_goal(self):
        out = agent._execute("set_mandate", {"statement": "  "})
        self.assertIn("nothing set", out.lower())


if __name__ == "__main__":
    unittest.main()
