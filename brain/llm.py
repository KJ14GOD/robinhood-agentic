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

Tone: direct, concise, opinionated but honest about uncertainty. No filler, no disclaimers theater."""

MODEL = config.BRAIN_MODEL
EFFORT = config.BRAIN_EFFORT


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
        messages=[{"role": "user", "content": user_prompt}],
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
        messages=[{"role": "user", "content": user_prompt}],
        output_format=schema,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"Model did not return valid {schema.__name__}")
    return resp.parsed_output
