"""Tests for strategy missions: seeding a roster, gated classification, candidate
persistence, and mission_update events — all with the LLM and market data mocked,
so the test is deterministic and offline. Uses a throwaway SQLite DB.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.data.prices import TrendSignals  # noqa: E402
from brain.db import repository as repo  # noqa: E402
from brain.engines import missions  # noqa: E402
from brain.models import (  # noqa: E402
    MissionClassification, MissionRoster, MissionSeed, MissionSeedItem, RiskProfile,
)

_CLASSIFY_CALLS = 0
SEED_TICKERS = [("LMT", "prime contractor"), ("RTX", "missiles + engines")]


def _fake_parse(prompt, schema, max_tokens=4000, **kw):
    global _CLASSIFY_CALLS
    if schema is MissionSeed:
        return MissionSeed(theme="Defense primes",
                           candidates=[MissionSeedItem(ticker=t, why=w) for t, w in SEED_TICKERS])
    if schema is MissionRoster:
        _CLASSIFY_CALLS += 1
        return MissionRoster(items=[
            MissionClassification(ticker="LMT", label="BUY", conviction=8, reason="breakout on budget news"),
            MissionClassification(ticker="RTX", label="WATCH", conviction=5, reason="range-bound"),
        ])
    raise AssertionError(f"unexpected schema {schema}")


def _fake_signals_many(tickers, refresh=False, **kw):
    return {t: TrendSignals(ticker=t, price=100.0, sector="Industrials", rsi_14=55.0) for t in tickers}


_WEB_CALLS = 0


def _fake_web_research(task, **kw):
    """Stub the live web-search 'gather' step so seeding stays offline + deterministic."""
    global _WEB_CALLS
    _WEB_CALLS += 1
    return "Live, on-theme names (stubbed brief): LMT, RTX."


class MissionTests(unittest.TestCase):
    def setUp(self):
        global _CLASSIFY_CALLS, SEED_TICKERS, _WEB_CALLS
        _CLASSIFY_CALLS = 0
        _WEB_CALLS = 0
        SEED_TICKERS = [("LMT", "prime contractor"), ("RTX", "missiles + engines")]
        missions.llm.parse = _fake_parse
        missions.llm.web_research = _fake_web_research
        missions.get_signals_many = _fake_signals_many
        self.profile = RiskProfile()

    def test_create_seeds_classifies_and_persists(self):
        m = missions.create_mission("track defense stocks", "any", self.profile)
        self.assertGreaterEqual(_WEB_CALLS, 1)  # seeding now searches the live web first
        self.assertEqual(m.theme, "Defense primes")
        labels = {c.ticker: c.label for c in m.candidates}
        self.assertEqual(labels.get("LMT"), "BUY")
        self.assertEqual(labels.get("RTX"), "WATCH")
        self.assertTrue(m.last_classified_at)

        # persisted and reloadable
        reloaded = repo.get_mission(m.id)
        self.assertIsNotNone(reloaded)
        self.assertEqual({c.ticker for c in reloaded.candidates}, {"LMT", "RTX"})
        lmt = next(c for c in reloaded.candidates if c.ticker == "LMT")
        self.assertEqual(lmt.label, "BUY")
        self.assertEqual(lmt.signals.get("sector"), "Industrials")

    def test_seed_falls_back_when_web_search_fails(self):
        """A web-search failure must not block seeding — it degrades to the model's
        own knowledge (the parse step) rather than raising."""
        def boom(task, **kw):
            raise RuntimeError("search down")
        missions.llm.web_research = boom
        m = missions.create_mission("track defense stocks", "any", self.profile)
        self.assertEqual({c.ticker for c in m.candidates}, {"LMT", "RTX"})

    def test_buy_promotion_emits_event(self):
        m = missions.create_mission("track defense stocks", "any", self.profile)
        ev = repo.recent_events(limit=50, event_types=["mission_update"])
        hit = [e for e in ev if e["ticker"] == "LMT"]
        self.assertTrue(hit, "expected a mission_update event for LMT promoted to BUY")
        self.assertEqual(hit[0]["source"], "mission")

    def test_classify_is_gated(self):
        m = missions.create_mission("track defense stocks", "any", self.profile)
        calls_after_create = _CLASSIFY_CALLS
        # immediate re-run without force is within the cooldown -> no new LLM classify
        missions.run_mission(m, self.profile, force=False)
        self.assertEqual(_CLASSIFY_CALLS, calls_after_create)
        # forcing bypasses the gate
        missions.run_mission(m, self.profile, force=True)
        self.assertEqual(_CLASSIFY_CALLS, calls_after_create + 1)

    def test_run_due_skips_fresh_missions(self):
        missions.create_mission("track defense stocks", "any", self.profile)
        before = _CLASSIFY_CALLS
        ran = missions.run_due_missions(self.profile)  # all just classified -> none due
        self.assertEqual(ran, [])
        self.assertEqual(_CLASSIFY_CALLS, before)

    def test_reseed_adds_new_names_and_keeps_existing(self):
        global SEED_TICKERS
        m = missions.create_mission("track defense stocks", "any", self.profile)
        self.assertEqual({c.ticker for c in m.candidates}, {"LMT", "RTX"})
        self.assertTrue(m.last_seeded_at)
        lmt_first_seen = next(c.first_seen for c in m.candidates if c.ticker == "LMT")

        # a later re-screen of the theme surfaces a genuinely new name
        SEED_TICKERS = [("LMT", "prime"), ("RTX", "missiles"), ("NOC", "new prime")]
        m2 = missions.reseed_mission(m, self.profile)

        tickers = {c.ticker for c in m2.candidates}
        self.assertEqual(tickers, {"LMT", "RTX", "NOC"})  # additive — nothing dropped
        # existing name keeps its accumulated history
        self.assertEqual(next(c.first_seen for c in m2.candidates if c.ticker == "LMT"), lmt_first_seen)
        # and a real "added to the roster" event fired for the new name
        ev = repo.recent_events(limit=50, event_types=["mission_update"])
        self.assertTrue(any(e["ticker"] == "NOC" and "added" in e["title"].lower() for e in ev))

    def test_status_and_delete(self):
        m = missions.create_mission("track defense stocks", "any", self.profile)
        repo.set_mission_status(m.id, "paused")
        self.assertEqual(repo.get_mission(m.id).status, "paused")
        repo.delete_mission(m.id)
        self.assertIsNone(repo.get_mission(m.id))


if __name__ == "__main__":
    unittest.main()
