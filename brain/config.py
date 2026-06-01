"""Central configuration, loaded from environment (.env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data_store"
DATA_DIR.mkdir(exist_ok=True)

# --- LLM ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "claude-opus-4-7")
BRAIN_EFFORT = os.environ.get("BRAIN_EFFORT", "high")  # low | medium | high | max

# --- Portfolio ---
PORTFOLIO_SOURCE = os.environ.get("PORTFOLIO_SOURCE", "manual").lower()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'brain.db'}")
PORTFOLIO_TTL_SECONDS = int(os.environ.get("PORTFOLIO_TTL_SECONDS", "30"))
QUOTE_TTL_SECONDS = int(os.environ.get("QUOTE_TTL_SECONDS", "60"))
SIGNAL_TTL_SECONDS = int(os.environ.get("SIGNAL_TTL_SECONDS", "900"))
SCREEN_TTL_SECONDS = int(os.environ.get("SCREEN_TTL_SECONDS", "1800"))
NEWS_TTL_SECONDS = int(os.environ.get("NEWS_TTL_SECONDS", "900"))
AUTO_REFRESH_SECONDS = int(os.environ.get("AUTO_REFRESH_SECONDS", "120"))
AUTO_BRIEFINGS = os.environ.get("AUTO_BRIEFINGS", "true").lower() in {"1", "true", "yes", "on"}
# Autonomous deep research: let the brain run unprompted deep dives on high-signal triggers
# (a thesis breaking/under review, a mission name promoted to BUY) and drop the report into the
# ping feed. Conservative + cooldowned in the engine; this is just the on/off switch.
AUTO_DEEP_RESEARCH = os.environ.get("AUTO_DEEP_RESEARCH", "true").lower() in {"1", "true", "yes", "on"}
MORNING_BRIEF_TIME = os.environ.get("MORNING_BRIEF_TIME", "06:30")
EVENING_BRIEF_TIME = os.environ.get("EVENING_BRIEF_TIME", "16:30")

# --- Robinhood (read-only) ---
RH_USERNAME = os.environ.get("RH_USERNAME", "")
RH_PASSWORD = os.environ.get("RH_PASSWORD", "")
RH_MFA = os.environ.get("RH_MFA", "")

# --- Persistence paths ---
PROFILE_PATH = DATA_DIR / "profile.json"
SHADOW_PATH = DATA_DIR / "shadow_ledger.jsonl"
HOLDINGS_CACHE = DATA_DIR / "holdings_manual.json"
RESEARCH_STATE_PATH = DATA_DIR / "research_state.json"
PORTFOLIO_SNAPSHOT_PATH = DATA_DIR / "portfolio_snapshot.json"
DIGEST_DIR = DATA_DIR / "digests"
DIGEST_DIR.mkdir(exist_ok=True)


def require_api_key() -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return ANTHROPIC_API_KEY
