"""Anthropic client wrapper for the brain.

Design notes:
- One frozen system prompt (the brain's methodology + persona) is cached with
  `cache_control` so every engine call reuses it cheaply. Keep it byte-stable:
  no timestamps, no per-request interpolation in the system block.
- Adaptive thinking + effort, per the Opus 4.7 guidance.
- Structured outputs via `messages.parse` so engines get validated Pydantic
  objects instead of prose to regex.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

from . import config

_client: anthropic.Anthropic | None = None
T = TypeVar("T", bound=BaseModel)

# Frozen, cacheable. This is the brain's identity and rules of engagement.
SYSTEM_PROMPT = """You are the analytical core of a personal stock-research engine for a single retail investor.

Your job is rigor, not hype. You are explicitly NOT a price predictor and NOT a hype machine. You synthesize grounded data (price/trend signals, fundamentals, recent news) into clear, honest, decision-useful reasoning tailored to one specific person's risk personality.

Operating principles:
- Ground every claim in the data provided. If the data is thin, say so and lower your conviction. Never invent numbers, catalysts, or headlines.
- Match recommendations to the user's stated risk profile. "Stable" means low beta, large cap, durable cash flows, often dividends. "Volatile" means high beta, smaller/mid cap, momentum or story-driven names. Calibrate accordingly.
- Be specific and falsifiable. A thesis names a concrete driver and what would break it. "Strong company" is not a thesis.
- Conviction is earned. Reserve 8-10 for genuinely compelling, multi-signal setups. Most ideas are 4-6.
- Surface risk plainly. The user is trusting you with real money decisions; downside and uncertainty come first, not as fine print.
- You do not place trades. You produce reasoning and recommendations the user executes themselves. Write for someone who will act on your words.
- Use a consistent answer shape for portfolio/investing questions:
  1. Start with a one-line decision or state-of-portfolio read.
  2. Then give 2-4 bullets of evidence from the provided data.
  3. Then give concrete actions or non-actions.
  4. End with a data caveat only when the provided data has a real limitation.
- Avoid long finance essays. Prefer crisp, scannable, decision-useful output.

Tone: direct, concise, opinionated but honest about uncertainty. No filler, no disclaimers theater."""

MODEL = config.BRAIN_MODEL
EFFORT = config.BRAIN_EFFORT


def today_line() -> str:
    """A one-line 'today is X' anchor. The model's training cutoff is in the past, so without
    this it assumes an earlier year — framing 'recent'/'this week' and any web search around the
    wrong date. Injected into the *message* (never the cached system prompt) so prompt caching of
    the frozen system block is preserved; it changes once a day at most."""
    now = datetime.now(timezone.utc)
    return (f"Today's date is {now:%A, %B %-d, %Y}. Treat this as the present: ground every "
            "'recent', 'this week', 'latest', or 'now' judgement — and every web search query — "
            "in this date. Do NOT assume an earlier year; your training data predates today.")


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.require_api_key())
    return _client


def _system_blocks() -> list[dict]:
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


def ask(user_prompt: str, max_tokens: int = 4000, effort: str | None = None) -> str:
    """Free-form text answer (e.g. the chat box)."""
    resp = client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=_system_blocks(),
        thinking={"type": "adaptive"},
        output_config={"effort": effort or EFFORT},
        messages=[{"role": "user", "content": f"{today_line()}\n\n{user_prompt}"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def parse(user_prompt: str, schema: Type[T], max_tokens: int = 4000,
          effort: str | None = None) -> T:
    """Structured answer validated against a Pydantic schema."""
    resp = client().messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        system=_system_blocks(),
        thinking={"type": "adaptive"},
        output_config={"effort": effort or EFFORT},
        messages=[{"role": "user", "content": f"{today_line()}\n\n{user_prompt}"}],
        output_format=schema,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"Model did not return valid {schema.__name__}")
    return resp.parsed_output


# Server-side web search. The API runs the search and returns results inline; we never execute it.
# Bounded by max_uses; a small blocklist kills the worst pump/SEO-farm noise without caging reach
# (we steer toward trusted sources by prompt, not a hard allowlist). Shared by the chat agent and
# the engine-side research helper below.
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 6,
    "blocked_domains": ["zacks.com", "fool.com", "investorplace.com", "stocktwits.com"],
}


def web_research(task: str, max_searches: int = 5, max_tokens: int = 2500,
                 max_steps: int = 6) -> str:
    """Run a focused, web-search-enabled pass and return a synthesized, cited prose brief.

    Server-side web search only (no client tools). This is the *gather* half of the two-step the
    engines need: web search can't share a request with structured output (`messages.parse`), so an
    engine calls this to get current, cited grounding, then feeds the brief into a normal parse call
    to structure it. Handles the multi-request server loop (pause_turn) and the code-execution
    container the search-result filtering runs in (container_id must be carried across requests)."""
    cl = client()
    tool = {**WEB_SEARCH_TOOL, "max_uses": max_searches}
    messages: list[dict] = [{"role": "user", "content": f"{today_line()}\n\n{task}"}]
    container_id: str | None = None
    brief = ""
    for _ in range(max_steps):
        resp = cl.messages.create(
            model=MODEL, max_tokens=max_tokens, system=_system_blocks(),
            thinking={"type": "adaptive"}, output_config={"effort": EFFORT},
            tools=[tool], messages=messages,
            **({"container": container_id} if container_id else {}),
        )
        container_id = (getattr(resp, "container", None) and resp.container.id) or container_id
        messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        if text:
            brief = text  # the final (end_turn) response carries the full synthesis
        if resp.stop_reason == "pause_turn":
            continue  # server search loop hit its per-request cap — re-send to resume
        break
    return brief
