"""Eval layer — error analysis on the brain's own reasoning.

The cost of shipping features went to zero; knowing whether the brain is actually
*good* did not. The Scorecard measures market outcome (did the call beat SPY) — slow,
noisy, maturity-gated. This measures the other thing: did the agent *do the job well* —
grounded, sourced, falsifiable, profile-aware — judgeable the instant a trace is produced.

Phase 1 (here) is human error-analysis: read a real trace, tag what failed. Those tags
accrete into a domain-specific failure taxonomy — the eval suite a generic benchmark can't
give you. Phase 2 (later) turns the taxonomy into an automated LLM-as-judge score.
"""
from __future__ import annotations

# Seed failure taxonomy for a stock-research agent. Extensible: a label can carry any
# tag, and new tags surface in the summary, so the taxonomy grows from real review.
SEED_FAILURE_MODES: list[dict] = [
    {"id": "hallucinated_fact", "label": "Hallucinated fact",
     "desc": "Stated a number or fact not supported by any source."},
    {"id": "weak_grounding", "label": "Weak grounding",
     "desc": "A load-bearing claim isn't backed by a cited source."},
    {"id": "source_mismatch", "label": "Source mismatch",
     "desc": "Cited a source that doesn't actually support the claim."},
    {"id": "missed_catalyst", "label": "Missed catalyst",
     "desc": "Overlooked an important or obvious catalyst."},
    {"id": "not_falsifiable", "label": "Not falsifiable",
     "desc": "Thesis has no real invalidation condition — can't be proven wrong."},
    {"id": "overconfident", "label": "Overconfident",
     "desc": "Conviction is too high for the strength of the evidence."},
    {"id": "ignored_profile", "label": "Ignored profile",
     "desc": "Didn't respect the user's risk appetite / horizon / personality."},
    {"id": "stale_data", "label": "Stale data",
     "desc": "Relied on outdated information when current would change the call."},
    {"id": "contradiction", "label": "Contradiction",
     "desc": "Contradicts an earlier call or the stored thesis without acknowledging it."},
    {"id": "tool_misuse", "label": "Tool misuse",
     "desc": "Didn't search/dig when it should have, or used the wrong tool."},
    {"id": "vague", "label": "Vague / hedged",
     "desc": "Non-actionable, hand-wavy, or hedged into uselessness."},
    {"id": "generic", "label": "Generic",
     "desc": "Could've been written about any stock — no specific, differentiated insight."},
]

VERDICTS = ["good", "mixed", "flawed"]

_SEED_IDS = {m["id"] for m in SEED_FAILURE_MODES}
_LABELS = {m["id"]: m["label"] for m in SEED_FAILURE_MODES}


def taxonomy() -> list[dict]:
    """The seed failure modes the review UI offers as checkboxes."""
    return list(SEED_FAILURE_MODES)


def normalize_tag(tag: str) -> str:
    """A free-typed failure mode becomes a stable id (lowercase, underscored), so user
    additions accrete into the taxonomy alongside the seed set instead of fragmenting."""
    return "_".join((tag or "").strip().lower().split())[:40]


def pretty(tag: str) -> str:
    """Human label for a tag id — the seed's label, or a title-cased fallback."""
    return _LABELS.get(tag) or tag.replace("_", " ").capitalize()
