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
from . import judge


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

    from .. import mandate
    mandate_block = mandate.mandate_prompt()

    prompt = f"""Produce a recommendation for {ticker} for this investor.

{mandate_block}

INVESTOR PROFILE:
{profile.describe()}

QUANTITATIVE SIGNALS (grounded — reason from these, don't invent):
{signals.as_prompt()}

{news}
{cat}
{social}
{social_guidance}

GROUNDING DISCIPLINE (you are graded on this): every load-bearing claim — a number, a fact, a
named catalyst — must be supported by the evidence above (the live web research, the quantitative
signals, or the recent catalysts). If the evidence doesn't support a claim, do not assert it: drop
it, or mark it explicitly as an assumption and lower your conviction. Never state a figure, date,
or catalyst that isn't in the provided evidence. A thesis resting on uncited assertions is a weak
thesis — price it at low conviction.

Decide the right action (buy / add / hold / trim / sell / watch) for THIS investor given
their risk profile{' and especially their mandate above' if mandate_block else ''}. Give a falsifiable thesis, the concrete
catalyst and rough timing, the risks that would break it, a suggested position size as % of
portfolio, and one line on why it fits (or how to size it to fit) their goal. Be honest about conviction."""

    ticket = llm.parse(prompt, TradeTicket, max_tokens=2500)
    ticket.ticker = ticker

    # Self-grading gate (eval Phase 2): judge the call against the user's own failure taxonomy
    # and, if it's flawed on a load-bearing mode, let it repair itself ONCE before it ships. The
    # (possibly revised) ticket is what flows on into shadow + memory + the user's hands.
    signals_prompt = signals.as_prompt()
    ticket, assessment, revised = judge.gate_ticket(
        ticket, profile, signals_prompt=signals_prompt,
        evidence_text=brief, sources=sources, mandate_block=mandate_block)

    # Persist the trace + cited evidence so the analysis is auditable later (same trail as
    # re-judge / deep research). Always write the run so the judge's score has a trace to attach
    # to — even on a degraded (no-web) call. Best-effort; never blocks the result.
    run_id = None
    try:
        run_id = db_repo.save_agent_run(
            query=f"Analyze {ticker}",
            answer=brief,
            kind="analyst",
            steps=[{"type": "analyst", "ticker": ticker, "sources": sources,
                    "action": ticket.action, "label": ticket.decision_label,
                    "conviction": ticket.conviction, "thesis": ticket.thesis,
                    "catalyst": ticket.catalyst, "risks": ticket.risks, "revised": revised}],
            tools_used="web_search" if brief.strip() else "", model=llm.MODEL,
        )
        if sources:
            db_repo.record_evidence(ticker, sources, kind="web", engine="analyst")
    except Exception:  # noqa: BLE001
        pass
    judge.record(run_id, "analyst", ticker, assessment, revised)

    if log_shadow:
        shadow.log_recommendation(ticket, source="analyst", profile=profile, signals=signals)
    research_state.update_from_ticket(ticket)
    return ticket
