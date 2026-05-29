"""Discovery engine — find stocks the user likely hasn't seen.

  1. Batched quantitative screen over the FULL universe (~520 names, one
     download) ranks by momentum / trend structure / RSI.
  2. Full fundamentals are pulled only for the top shortlist.
  3. The LLM writes the thesis and ranks the finalists against the user's
     profile and a requested risk flavor.

The screen finds them; the model explains and personalizes them.
"""
from __future__ import annotations

from ..data.prices import ScreenRow, get_signals_many
from ..data.universe import screening_universe
from ..models import DiscoveryResult, RiskProfile, TradeTicket
from ..data import prices
from .. import shadow
from .. import llm, research_state


def _screen_score(r: ScreenRow) -> float:
    """Momentum + trend-structure score. Pure arithmetic, no model."""
    if r.price <= 0:
        return -1e9
    s = r.ret_3m_pct * 1.0 + r.ret_6m_pct * 0.5
    s += 15 if r.above_50d else -10
    s += 15 if r.above_200d else -10
    if 50 <= r.rsi_14 <= 70:
        s += 10
    elif r.rsi_14 > 80:
        s -= 15
    return s


def _flavor_ok(r: ScreenRow, flavor: str) -> bool:
    # Fast pre-filter using volatility as a beta proxy (beta needs the slow call).
    if flavor == "stable":
        return r.vol_annualized_pct <= 35
    if flavor == "volatile":
        return r.vol_annualized_pct >= 45
    return True


def discover(profile: RiskProfile, flavor: str = "any", top_n: int = 5,
             exclude: list[str] | None = None) -> DiscoveryResult:
    """flavor: 'stable' | 'volatile' | 'any'."""
    universe = screening_universe(exclude=exclude)
    rows = [r for r in prices.screen_universe(universe) if _flavor_ok(r, flavor)]
    rows.sort(key=_screen_score, reverse=True)
    shortlist = rows[:12]  # the model picks from these
    if not shortlist:
        return DiscoveryResult(ideas=[])

    # Enrich the shortlist with full fundamentals (sector, beta, P/E, div).
    enriched_map = get_signals_many([r.ticker for r in shortlist])
    enriched = [enriched_map.get(r.ticker) for r in shortlist]
    rows_txt = "\n".join(f"- {s.as_prompt()}" for s in enriched if s and s.price > 0)

    prompt = f"""From the screened candidates below, pick the {top_n} most compelling ideas for this investor.

INVESTOR PROFILE:
{profile.describe()}

REQUESTED FLAVOR: {flavor} (stable=lower risk/beta, volatile=higher risk/upside, any=best overall)

SCREENED CANDIDATES (top of a {len(universe)}-stock momentum/trend screen — grounded data):
{rows_txt}

For each pick: the hook (why it's interesting now), a one-line summary of what the screen
flagged, a stable/moderate/volatile tag, and honest conviction (1-10). Favor ideas that
genuinely fit this person and that they likely wouldn't have surfaced themselves.
Return at most {top_n}."""

    result = llm.parse(prompt, DiscoveryResult, max_tokens=3000)
    result.ideas = result.ideas[:top_n]
    research_state.add_discovery_ideas(result.ideas)

    # Log each new idea to shadow so the track record covers discovery, not just
    # the analyst. Dedup so re-running discovery doesn't double-log the same name.
    for idea in result.ideas:
        if shadow.has_open(idea.ticker):
            continue
        shadow.log_recommendation(
            TradeTicket(
                ticker=idea.ticker, action="buy", conviction=idea.conviction,
                thesis=idea.why_now, catalyst=idea.signal_summary,
                risks=f"Risk flavor: {idea.risk_flavor}.",
            ),
            source="discovery",
        )
    return result
