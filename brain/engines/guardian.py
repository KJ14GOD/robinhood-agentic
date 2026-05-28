"""Guardian engine — always-on watch over the real portfolio.

Pulls signals + news for every holding, computes concentration, and asks the
LLM for a state-of-the-portfolio digest: what's good, what's bad, what to do.
Designed to run on a schedule (see scheduler note in README) and on demand.
"""
from __future__ import annotations

import json

from ..data.news import headlines_as_prompt
from ..data.prices import get_signals_many
from ..models import GuardianDigest, Portfolio, RiskProfile
from .. import config, llm, research_state
from ..models import _now


def _concentration_lines(pf: Portfolio, profile: RiskProfile) -> list[str]:
    flags = []
    for tkr, w in pf.weights().items():
        if w > profile.max_single_position_pct:
            flags.append(f"{tkr} is {w:.0f}% of the portfolio (over your {profile.max_single_position_pct:.0f}% comfort line)")
    return flags


def run_guardian(pf: Portfolio, profile: RiskProfile, save: bool = True) -> GuardianDigest:
    if not pf.holdings:
        return GuardianDigest(summary="No holdings to watch yet. Add positions to begin monitoring.",
                              insights=[])

    weights = pf.weights()
    blocks = []
    memory = research_state.load_state()
    signals = get_signals_many([h.ticker for h in pf.holdings])
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True):
        sig = signals.get(h.ticker)
        upnl = h.unrealized_pct
        blocks.append(
            f"{h.ticker}: weight {weights.get(h.ticker, 0):.1f}%, "
            f"unrealized {upnl:+.1f}% " if upnl is not None else f"{h.ticker}: weight {weights.get(h.ticker,0):.1f}%, "
        )
        blocks.append("  " + sig.as_prompt() if sig else "  no signal data")
        if h.ticker in memory.theses:
            thesis = memory.theses[h.ticker]
            blocks.append(f"  stored thesis: {thesis.thesis} invalidation: {thesis.invalidation}")
        blocks.append("  " + headlines_as_prompt(h.ticker, limit=4).replace("\n", "\n  "))

    conc = _concentration_lines(pf, profile)
    prompt = f"""Give this investor a state-of-the-portfolio briefing.

INVESTOR PROFILE:
{profile.describe()}

PORTFOLIO (total ${pf.total_value:,.0f}, cash ${pf.cash:,.0f}):
{chr(10).join(blocks)}

CONCENTRATION (mechanical check): {('; '.join(conc)) if conc else 'within comfort limits'}

Write a 2-3 sentence overall summary, then one insight per holding worth flagging
(positive/neutral/negative, with a concrete detail grounded in the data above). Where a
position clearly warrants action for THIS investor, attach a suggested trade (action,
conviction, thesis, catalyst, risks, size, profile-fit). Don't manufacture action where
holding is the right call. List any concentration flags."""

    digest = llm.parse(prompt, GuardianDigest, max_tokens=4000)
    if not digest.concentration_flags:
        digest.concentration_flags = conc
    if save:
        path = config.DIGEST_DIR / f"digest_{_now()[:10]}.json"
        path.write_text(json.dumps(digest.model_dump(), indent=2))
    return digest
