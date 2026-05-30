"""Findings engine — the curated top of the always-on feed.

Findings is a *view*, not a second scan. The deterministic monitor and the
living-memory engine already write everything they observe to the persisted
event stream (the Activity log). This engine reads that same record back and
makes ONE structured LLM pass to rank, de-duplicate, and phrase the handful of
things that matter most for this investor right now — plus a couple of market
standouts they don't own, for proactive idea surfacing.

Because the holdings facts come from the same events the Activity tab shows,
the two surfaces can no longer tell contradictory stories about the portfolio.
This also folds in what the standalone Guardian digest used to do: the
per-holding state-of-the-book read is now just the curation of those events.
"""
from __future__ import annotations

from ..data.news import get_news
from ..data.prices import screen_universe
from ..data.universe import screening_universe
from ..db import repository as db_repo
from ..models import FindingsFeed, Portfolio, RiskProfile
from .. import llm, research_state
from ..engines.discovery import _screen_score


def scan(pf: Portfolio, profile: RiskProfile, max_findings: int = 6) -> FindingsFeed:
    weights = pf.weights()
    held = [h.ticker for h in pf.holdings]
    memory = research_state.load_state()

    # --- the brain's own logged record (Activity) is the backbone -------------- #
    # Findings ranks and translates what's already been observed; it does not
    # re-derive the world independently, so it stays consistent with Activity.
    events = db_repo.recent_events(limit=30, within_hours=72.0)
    event_lines = [
        f"[{e.get('severity', 'info')}] "
        f"{(e.get('ticker') + ': ') if e.get('ticker') else ''}"
        f"{e.get('title', '')}. {e.get('summary', '')}".strip()
        for e in events
    ]

    # --- holdings framing (no IO): weights, P&L, and the stored thesis --------- #
    holding_lines = []
    conc_lines = []
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True):
        w = weights.get(h.ticker, 0)
        upnl = h.unrealized_pct
        thesis = memory.theses.get(h.ticker)
        thesis_line = f" Stored thesis ({thesis.status}): {thesis.thesis}" if thesis else ""
        holding_lines.append(
            f"{h.ticker}: {w:.0f}% weight, unrealized {upnl:+.0f}%.{thesis_line}"
            if upnl is not None else f"{h.ticker}: {w:.0f}% weight.{thesis_line}"
        )
        if w > profile.max_single_position_pct:
            conc_lines.append(f"{h.ticker} is {w:.0f}% of the book (over the {profile.max_single_position_pct:.0f}% comfort line).")

    # --- light news enrichment (events don't carry headlines) ------------------ #
    news_lines = []
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True)[:3]:
        for hl in get_news(h.ticker, limit=2):
            news_lines.append(f"{h.ticker}: {hl.title}")

    # --- a couple of market standouts the user does NOT own -------------------- #
    market = [r for r in screen_universe(screening_universe(exclude=held))]
    market.sort(key=_screen_score, reverse=True)
    opp_lines = [
        f"{r.ticker}: ${r.price:.0f}, 3m {r.ret_3m_pct:+.0f}%/6m {r.ret_6m_pct:+.0f}%, RSI {r.rsi_14:.0f}"
        for r in market[:4]
    ]

    if not events and not pf.holdings and not opp_lines:
        return FindingsFeed(findings=[])

    prompt = f"""Curate the {max_findings} most important findings for this investor's feed right now.

PROFILE: {profile.describe()}

LOGGED OBSERVATIONS (the brain's own monitor + memory record — these are the
facts already shown in the Activity log; treat them as ground truth and base
your risk / concentration / thesis findings on them):
{chr(10).join(event_lines) or 'nothing logged in the last 72h'}

CURRENT HOLDINGS (weights, P&L, stored thesis):
{chr(10).join(holding_lines) or 'none'}

CONCENTRATION (mechanical): {' '.join(conc_lines) or 'within limits'}

RECENT NEWS ON POSITIONS:
{chr(10).join(news_lines) or 'none'}

MARKET STANDOUTS NOT OWNED (top of momentum screen — use for opportunity findings):
{chr(10).join(opp_lines) or 'none'}

Rank what genuinely matters most to THIS investor and write each as a finding:
kind (opportunity/risk/news/concentration), ticker, a punchy headline, and grounded
detail. Ground every finding in the observations and data above — do not invent
conditions that aren't supported. Quality over filling the quota; most important first."""

    feed = llm.parse(prompt, FindingsFeed, max_tokens=2500)
    feed.findings = feed.findings[:max_findings]
    return feed
