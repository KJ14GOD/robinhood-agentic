"""Analyst engine — on-demand deep dive on any ticker.

Grounds the LLM in live signals + news, filters through the risk profile, and
returns a falsifiable TradeTicket. Logs the result to shadow mode.
"""
from __future__ import annotations

from ..data.news import headlines_as_prompt
from ..data.prices import get_signals
from ..data import sentiment
from ..models import RiskProfile, TradeTicket
from .. import llm, research_state, shadow


def analyze(ticker: str, profile: RiskProfile, log_shadow: bool = True) -> TradeTicket:
    ticker = ticker.upper().strip()
    signals = get_signals(ticker)
    news = headlines_as_prompt(ticker)
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
{social}
{social_guidance}

Decide the right action (buy / add / hold / trim / sell / watch) for THIS investor given
their risk profile. Give a falsifiable thesis, the concrete catalyst and rough timing,
the risks that would break it, a suggested position size as % of portfolio, and one line
on why it fits (or how to size it to fit) their personality. Be honest about conviction."""

    ticket = llm.parse(prompt, TradeTicket, max_tokens=2500)
    ticket.ticker = ticker
    if log_shadow:
        shadow.log_recommendation(ticket, source="analyst", profile=profile, signals=signals)
    research_state.update_from_ticket(ticket)
    return ticket
