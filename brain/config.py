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

# --- Robinhood (read-only) ---
RH_USERNAME = os.environ.get("RH_USERNAME", "")
RH_PASSWORD = os.environ.get("RH_PASSWORD", "")
RH_MFA = os.environ.get("RH_MFA", "")

# --- Persistence paths ---
PROFILE_PATH = DATA_DIR / "profile.json"
SHADOW_PATH = DATA_DIR / "shadow_ledger.jsonl"
HOLDINGS_CACHE = DATA_DIR / "holdings_manual.json"
DIGEST_DIR = DATA_DIR / "digests"
DIGEST_DIR.mkdir(exist_ok=True)


def require_api_key() -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return ANTHROPIC_API_KEY
