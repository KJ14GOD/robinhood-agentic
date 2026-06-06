"""Analyst engine — on-demand deep dive on any ticker.

Grounds the LLM in live signals + a live web pass (current news substance, catalysts,
guidance, analyst takes), filters through the risk profile, and returns a falsifiable
TradeTicket. Logs the result to shadow mode and persists its cited evidence to the audit
trail. Two-step because web search can't share a request with structured output: gather
a cited brief, then parse it into the ticket — with today's RSS headlines as a fallback
so a search hiccup degrades gracefully instead of skipping the grounding.
"""
from __future__ import annotations

from ..data.news import headlines_as_prompt
from ..data.prices import get_signals
from ..data import sentiment, catalysts
from ..db import repository as db_repo
from ..models import RiskProfile, TradeTicket
from .. import llm, research_state, shadow


def _research_task(ticker: str) -> str:
    return f"""Gather what's actually happening with {ticker} right now, for an investment decision.

Pull the substance (not just headlines) of: recent company/sector news, any near-term catalyst
(earnings dates, product/launch, regulatory), latest guidance and analyst reactions, and anything
that materially changed the setup in the last few weeks. Report concrete, cited facts — figures and
dates where you have them. Don't render a buy/sell verdict; just surface what matters."""


def analyze(ticker: str, profile: RiskProfile, log_shadow: bool = True) -> TradeTicket:
    ticker = ticker.upper().strip()
    signals = get_signals(ticker)
    try:
        brief, sources = llm.web_research(_research_task(ticker), max_searches=4, return_sources=True)
    except Exception:  # noqa: BLE001 — degrade to RSS rather than skip grounding
        brief, sources = "", []
    news = (f"LIVE WEB RESEARCH (current, cited):\n{brief.strip()}"
            if brief.strip() else headlines_as_prompt(ticker))
    cat = catalysts.catalysts_prompt(ticker)  # structured, dated recent news (empty if none)
    social = sentiment.sentiment_prompt(ticker)
    social_guidance = ("""
When the social read is relevant, work it into your reasoning explicitly (one clause is
enough) — e.g. lopsided bullishness after a big run is a crowded long / contrarian caution,
a mention spike flags a catalyst or a pump to check. Treat it as crowd positioning, never as
fact, and only mention it when it actually bears on the call. Don't force it.""" if social else "")

    prompt = f"""Produce a recommendation for {ticker} for this investor.

INVESTOR PROFILE:
{profile.describe()}

QUANTITATIVE SIGNALS (grounded — reason from these, don't invent):
{signals.as_prompt()}

{news}
{cat}
{social}
{social_guidance}

Decide the right action (buy / add / hold / trim / sell / watch) for THIS investor given
their risk profile. Give a falsifiable thesis, the concrete catalyst and rough timing,
the risks that would break it, a suggested position size as % of portfolio, and one line
on why it fits (or how to size it to fit) their personality. Be honest about conviction."""

    ticket = llm.parse(prompt, TradeTicket, max_tokens=2500)
    ticket.ticker = ticker
    # Persist the cited evidence this call read, so the analysis is auditable later
    # (same trail as re-judge / deep research). Best-effort; never blocks the result.
    if brief.strip():
        try:
            db_repo.save_agent_run(
                query=f"Analyze {ticker}",
                answer=brief,
                kind="analyst",
                steps=[{"type": "analyst", "ticker": ticker, "sources": sources,
                        "action": ticket.action, "label": ticket.decision_label}],
                tools_used="web_search", model=llm.MODEL,
            )
            db_repo.record_evidence(ticker, sources, kind="web", engine="analyst")
        except Exception:  # noqa: BLE001
            pass
    if log_shadow:
        shadow.log_recommendation(ticket, source="analyst", profile=profile, signals=signals)
    research_state.update_from_ticket(ticket)
    return ticket
