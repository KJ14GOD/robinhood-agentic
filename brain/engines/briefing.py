"""Morning/evening briefing engine.

Briefings are saved into research memory so the dashboard has an inbox of
what the assistant found over time, not just a transient feed.
"""
from __future__ import annotations

import uuid
from typing import Literal

from .. import llm, research_state
from ..data.news import headlines_as_prompt
from ..data.prices import get_signals_many
from ..models import Briefing, Portfolio, RiskProfile


class BriefingDraft(Briefing):
    id: str = "draft"


def generate(kind: Literal["morning", "evening", "manual"],
             pf: Portfolio, profile: RiskProfile) -> Briefing:
    weights = pf.weights()
    memory = research_state.load_state()
    signals = get_signals_many([h.ticker for h in pf.holdings])
    holding_lines = []
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True):
        sig = signals.get(h.ticker)
        thesis = memory.theses.get(h.ticker)
        holding_lines.append(
            f"{h.ticker}: {weights.get(h.ticker, 0):.1f}% weight, "
            f"value ${h.market_value:,.0f}, price ${h.current_price:.2f}, "
            f"unrealized {h.unrealized_pct:+.1f}%."
            if h.unrealized_pct is not None else
            f"{h.ticker}: {weights.get(h.ticker, 0):.1f}% weight, value ${h.market_value:,.0f}."
        )
        if sig:
            holding_lines.append("  " + sig.as_prompt())
        if thesis:
            holding_lines.append(f"  thesis: {thesis.thesis} invalidation: {thesis.invalidation}")
        holding_lines.append("  " + headlines_as_prompt(h.ticker, limit=2).replace("\n", "\n  "))

    watch_lines = [
        f"- {item.ticker} ({item.mode}): {item.reason}"
        for item in memory.watchlist[-12:]
    ]

    prompt = f"""Create a {kind} portfolio briefing for this investor.

INVESTOR PROFILE:
{profile.describe()}

PORTFOLIO:
total ${pf.total_value:,.0f}, cash ${pf.cash:,.0f}, source {pf.source}
{chr(10).join(holding_lines) or 'No holdings.'}

WATCHLIST / MEMORY:
{chr(10).join(watch_lines) or 'No watchlist items yet.'}

Return a concise briefing with:
- title
- 2-3 sentence summary
- 4-7 bullets ordered by importance
- 2-5 concrete actions or non-actions for today/tomorrow

Make it decision-useful. Mention if "do nothing" is the right action."""

    draft = llm.parse(prompt, BriefingDraft, max_tokens=2500)
    briefing = Briefing(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        title=draft.title,
        summary=draft.summary,
        bullets=draft.bullets,
        actions=draft.actions,
    )
    research_state.add_briefing(briefing)
    return briefing
