"""The brain's public API — what the dashboard and CLI call.

Ties together profile + portfolio + data + engines + shadow ledger. This is the
single import surface for everything above the engine layer.
"""
from __future__ import annotations

from typing import Iterator

from . import agent, llm, profile_store, shadow
from .engines import analyst, discovery, findings, guardian
from .models import DiscoveryResult, GuardianDigest, Portfolio, RiskProfile, TradeTicket
from .portfolio import get_portfolio


# --- profile ---------------------------------------------------------------- #
def get_profile() -> RiskProfile:
    return profile_store.load_profile()


def update_profile(profile: RiskProfile) -> RiskProfile:
    return profile_store.save_profile(profile)


def feedback(ticker: str, accepted: bool) -> RiskProfile:
    profile_store.record_feedback(ticker, accepted)
    return refresh_learning()


def refresh_learning() -> RiskProfile:
    """Re-read the user's actual holdings into the investor signature. Called
    after feedback and exposed so the UI can trigger a learning refresh."""
    from . import profile_learning
    profile = profile_learning.learn_from_holdings(profile_store.load_profile(), get_portfolio())
    return profile_store.save_profile(profile)


# --- portfolio -------------------------------------------------------------- #
def portfolio() -> Portfolio:
    return get_portfolio()


# --- engines ---------------------------------------------------------------- #
def analyze(ticker: str) -> TradeTicket:
    return analyst.analyze(ticker, get_profile())


def discover(flavor: str = "any", top_n: int = 5) -> DiscoveryResult:
    pf = get_portfolio()
    held = [h.ticker for h in pf.holdings]
    return discovery.discover(get_profile(), flavor=flavor, top_n=top_n, exclude=held)


def daily_digest() -> GuardianDigest:
    return guardian.run_guardian(get_portfolio(), get_profile())


def feed():
    """Proactive findings for the always-on feed."""
    return findings.scan(get_portfolio(), get_profile())


# --- shadow mode ------------------------------------------------------------ #
def scoreboard() -> dict:
    return shadow.scoreboard()


# --- agentic chat ----------------------------------------------------------- #
def chat(message: str, history: list[dict] | None = None) -> dict:
    """Agentic Q&A. The model drives its own research via tools and remembers
    the conversation. Returns {answer, steps} where steps is the trace of what
    the brain did."""
    return agent.run(message, history=history)


def chat_stream(message: str, history: list[dict] | None = None) -> Iterator[dict]:
    """Streaming variant — yields each step as it happens so the UI can render
    the brain's thinking live (tool calls, interim notes, final answer)."""
    yield from agent.run_stream(message, history=history)
