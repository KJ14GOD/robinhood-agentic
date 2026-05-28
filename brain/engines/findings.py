"""Findings engine — the always-on feed.

Proactively surfaces what the user should know *right now*: holdings breaking
down or running hot, concentration drift, fresh news on positions, and a couple
of standout market opportunities they don't own. Grounded in batched signals +
news, then distilled into a typed feed by one structured LLM call.
"""
from __future__ import annotations

from ..data.news import get_news
from ..data.prices import get_signals_many, screen_universe
from ..data.universe import screening_universe
from ..models import FindingsFeed, Portfolio, RiskProfile
from .. import llm, research_state
from ..engines.discovery import _screen_score


def scan(pf: Portfolio, profile: RiskProfile, max_findings: int = 6) -> FindingsFeed:
    weights = pf.weights()
    held = [h.ticker for h in pf.holdings]

    # --- grounded inputs ---------------------------------------------------- #
    holding_lines = []
    conc_lines = []
    memory = research_state.load_state()
    signals = get_signals_many([h.ticker for h in pf.holdings])
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True):
        sig = signals.get(h.ticker)
        w = weights.get(h.ticker, 0)
        upnl = h.unrealized_pct
        sig_line = sig.as_prompt() if sig else "no signal data"
        thesis = memory.theses.get(h.ticker)
        thesis_line = f" Stored thesis: {thesis.thesis} Invalidation: {thesis.invalidation}" if thesis else ""
        holding_lines.append(
            f"{h.ticker}: {w:.0f}% weight, unrealized {upnl:+.0f}%. {sig_line}{thesis_line}"
            if upnl is not None else f"{h.ticker}: {w:.0f}% weight. {sig_line}{thesis_line}"
        )
        if w > profile.max_single_position_pct:
            conc_lines.append(f"{h.ticker} is {w:.0f}% of the book (over the {profile.max_single_position_pct:.0f}% comfort line).")

    # fresh news for the top 3 positions only (bounds latency)
    news_lines = []
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True)[:3]:
        for hl in get_news(h.ticker, limit=2):
            news_lines.append(f"{h.ticker}: {hl.title}")

    # a couple of market standouts the user does NOT own
    market = [r for r in screen_universe(screening_universe(exclude=held))]
    market.sort(key=_screen_score, reverse=True)
    opp_lines = [
        f"{r.ticker}: ${r.price:.0f}, 3m {r.ret_3m_pct:+.0f}%/6m {r.ret_6m_pct:+.0f}%, RSI {r.rsi_14:.0f}"
        for r in market[:4]
    ]
    watch_lines = [
        f"{item.ticker} ({item.mode}): {item.reason}"
        for item in memory.watchlist[-8:]
        if item.ticker not in held
    ]

    if not pf.holdings and not opp_lines:
        return FindingsFeed(findings=[])

    prompt = f"""Produce the {max_findings} most important findings for this investor's feed right now.

PROFILE: {profile.describe()}

HOLDINGS (grounded signals):
{chr(10).join(holding_lines) or 'none'}

CONCENTRATION (mechanical): {' '.join(conc_lines) or 'within limits'}

RECENT NEWS ON POSITIONS:
{chr(10).join(news_lines) or 'none'}

MARKET STANDOUTS NOT OWNED (top of momentum screen):
{chr(10).join(opp_lines) or 'none'}

PERSISTENT WATCHLIST:
{chr(10).join(watch_lines) or 'none'}

Surface a mix of: real risks (a holding breaking down, concentration), notable news, and
genuine opportunities. Use the stored thesis/watchlist context when it changes what matters.
Each finding: kind (opportunity/risk/news/concentration), ticker, a punchy headline, and
grounded detail. Only include findings that actually matter to THIS investor — quality over
filling the quota. Most important first."""

    feed = llm.parse(prompt, FindingsFeed, max_tokens=2500)
    feed.findings = feed.findings[:max_findings]
    return feed
