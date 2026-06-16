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
# Cadence ceiling for the heavy LLM brain loop (decoupled from the fast price refresh
# so a long deep dive can't stall live data). The engines self-gate; this is just a floor.
BRAIN_LOOP_SECONDS = int(os.environ.get("BRAIN_LOOP_SECONDS", "180"))
# How often the agent proactively re-reads your portfolio against your mandate and pings
# you a fresh plan (the "comes to you" cadence). One LLM call per period.
MANDATE_REVIEW_DAYS = int(os.environ.get("MANDATE_REVIEW_DAYS", "7"))
# Drift-triggered plan: also re-plan when the book moves materially off its last-planned shape
# between the weekly checks (a per-name weight move ≥ this many points, a new/exited position).
# Its own cooldown so a busy week can't spam you.
MANDATE_DRIFT_PCT = int(os.environ.get("MANDATE_DRIFT_PCT", "12"))
MANDATE_DRIFT_COOLDOWN_HOURS = float(os.environ.get("MANDATE_DRIFT_COOLDOWN_HOURS", "24"))

# --- Autopilot (the Twin) decision brain ---
# The autonomous paper fund runs a decision cycle on this cadence (cost control on the LLM think —
# the Twin still sets its own trade count, we just don't re-think more often than this).
TWIN_ENABLED = os.environ.get("TWIN_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
TWIN_DECIDE_HOURS = float(os.environ.get("TWIN_DECIDE_HOURS", "4"))
THEME_SCOUT_HOURS = float(os.environ.get("THEME_SCOUT_HOURS", "6"))
STRATEGY_DISCOVERY_HOURS = float(os.environ.get("STRATEGY_DISCOVERY_HOURS", "6"))
TWIN_PREFLIGHT_BUY_MAX_UP_PCT = float(os.environ.get("TWIN_PREFLIGHT_BUY_MAX_UP_PCT", "4"))
TWIN_PREFLIGHT_SELL_MAX_DOWN_PCT = float(os.environ.get("TWIN_PREFLIGHT_SELL_MAX_DOWN_PCT", "8"))
AUTO_BRIEFINGS = os.environ.get("AUTO_BRIEFINGS", "true").lower() in {"1", "true", "yes", "on"}
# Autonomous deep research: let the brain run unprompted deep dives on high-signal triggers
# (a thesis breaking/under review, a mission name promoted to BUY) and drop the report into the
# ping feed. Conservative + cooldowned in the engine; this is just the on/off switch.
AUTO_DEEP_RESEARCH = os.environ.get("AUTO_DEEP_RESEARCH", "true").lower() in {"1", "true", "yes", "on"}
MORNING_BRIEF_TIME = os.environ.get("MORNING_BRIEF_TIME", "06:30")
EVENING_BRIEF_TIME = os.environ.get("EVENING_BRIEF_TIME", "16:30")

# --- Eval layer / self-grading (Phase 2: LLM-as-judge) ---
# The process eval: auto-score every reasoning trace against the failure taxonomy the instant
# it's produced (judgeable now, unlike the time-gated outcome Scorecard). JUDGE_ENABLED is the
# master switch. SELF_CRITIQUE additionally lets a fresh recommendation the judge flags as flawed
# on a load-bearing mode repair itself ONCE before it reaches the user — the agentic loop.
JUDGE_ENABLED = os.environ.get("JUDGE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SELF_CRITIQUE = os.environ.get("SELF_CRITIQUE", "true").lower() in {"1", "true", "yes", "on"}
JUDGE_EFFORT = os.environ.get("JUDGE_EFFORT", "medium")  # focused pass; cheaper than the main brain
JUDGE_SWEEP_MAX = int(os.environ.get("JUDGE_SWEEP_MAX", "4"))  # max unscored traces auto-judged per brain cycle

# --- Robinhood (read-only) ---
RH_USERNAME = os.environ.get("RH_USERNAME", "")
RH_PASSWORD = os.environ.get("RH_PASSWORD", "")
RH_MFA = os.environ.get("RH_MFA", "")

# --- Social sentiment (free, no-auth: StockTwits mood + ApeWisdom/Reddit buzz) ---
# Secondary, contextual signal. Set SENTIMENT_ENABLED=false to turn it off.
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SENTIMENT_TTL_SECONDS = int(os.environ.get("SENTIMENT_TTL_SECONDS", "1800"))  # 30 min cache + ingest gate
SENTIMENT_BUZZ_PCT = int(os.environ.get("SENTIMENT_BUZZ_PCT", "50"))   # mention spike % that pings
SENTIMENT_BUZZ_MIN = int(os.environ.get("SENTIMENT_BUZZ_MIN", "25"))   # min absolute mentions to count

# --- Catalyst radar (Finnhub structured company news) ---
# A real, timestamped news feed so the brain proactively surfaces fresh catalysts on
# your names. Fully quarantined + gated on the key: with no key it's a clean no-op.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB_ENABLED = os.environ.get("FINNHUB_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
FINNHUB_TTL_SECONDS = int(os.environ.get("FINNHUB_TTL_SECONDS", "900"))      # 15 min cache + scan gate
FINNHUB_FRESH_HOURS = int(os.environ.get("FINNHUB_FRESH_HOURS", "6"))        # only ping news newer than this
FINNHUB_COOLDOWN_HOURS = float(os.environ.get("FINNHUB_COOLDOWN_HOURS", "6"))  # max one catalyst ping/name per window

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
