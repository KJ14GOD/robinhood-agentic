"""Deep research mode — the heavy, cited, self-critiqued analysis.

This is the counterpart to the fast one-shot analyst: it plans, pulls multiple
angles, writes an explicit bull AND bear case, then runs a second pass that
argues *against* its own draft before settling a final call. The result updates
the stored thesis (so living memory benefits), logs to the shadow track record
as its own source, and writes the full trace to `agent_runs` as an audit trail.

It is for important decisions, not every click — two model calls per run.
"""
from __future__ import annotations

from .. import llm, research_state, shadow
from ..data.news import headlines_as_prompt
from ..data.prices import clean_ticker, get_chart, get_signals
from ..db import repository as db_repo
from ..models import DeepResearchCritique, DeepResearchDraft, RiskProfile, TradeTicket, _now


def _draft_prompt(ticker, profile, signals, news, chart_summary, prior) -> str:
    return f"""Run a deep, falsifiable research pass on {ticker} for this investor. Plan your
inquiry, then build BOTH sides honestly before concluding.

INVESTOR PROFILE:
{profile.describe()}

{prior}

QUANTITATIVE SIGNALS (grounded — reason from these, cite them, do not invent numbers):
{signals.as_prompt()}

PRICE ACTION: {chart_summary}

{news}

Produce: a short research plan (the specific questions you're answering), the strongest
grounded bull case and bear case, the specific evidence you actually used, a falsifiable
thesis, the catalyst and rough timing, the invalidation (what would break it), a suggested
size as % of portfolio, an action (buy/add/hold/trim/sell/watch), and honest conviction.
Be willing to conclude the boring or negative answer."""


def _critique_prompt(ticker, draft: DeepResearchDraft) -> str:
    return f"""You just drafted this call on {ticker}. Now be your own toughest critic before it ships.

DRAFT CALL: {draft.action.upper()} at conviction {draft.conviction}/10.
THESIS: {draft.thesis}
BULL: {' | '.join(draft.bull_case)}
BEAR: {' | '.join(draft.bear_case)}

Attack it: where is the draft weakest, what could be wrong, and what is the strongest version
of the OPPOSITE call? Then decide whether the original call still holds. Give your final action
and conviction — change them if the criticism warrants it, keep them if it doesn't — and one
line on where you landed and why. Intellectual honesty over consistency."""


def _render_text(report: dict) -> str:
    """A plain-text rendering stored as the agent_run answer (the audit trail)."""
    def block(label, items):
        return f"{label}:\n" + "\n".join(f"  - {x}" for x in items) if items else ""
    parts = [
        f"DEEP RESEARCH — {report['ticker']}: {report['verdict']} (conviction {report['conviction']}/10)",
        report.get("note", ""),
        block("Plan", report.get("plan", [])),
        block("Bull", report.get("bull_case", [])),
        block("Bear", report.get("bear_case", [])),
        block("Evidence", report.get("evidence", [])),
        block("Self-critique", report.get("critique", [])),
        f"Thesis: {report.get('thesis','')}",
        f"Breaks if: {report.get('invalidation','')}",
    ]
    return "\n\n".join(p for p in parts if p)


def run(ticker: str, profile: RiskProfile) -> dict:
    """Deep dive on one ticker. Returns the structured report and persists it."""
    ticker = clean_ticker(ticker) or ticker.upper().strip()
    signals = get_signals(ticker)
    news = headlines_as_prompt(ticker, limit=8)
    chart_summary = get_chart(ticker, "6m").summary()

    state = research_state.load_state()
    prior_thesis = state.theses.get(ticker)
    prior = (f"PRIOR STORED THESIS (build on or challenge it): {prior_thesis.thesis}\n"
             f"PRIOR INVALIDATION: {prior_thesis.invalidation}"
             if prior_thesis else "No prior thesis on file — this is the first deep look.")

    draft = llm.parse(_draft_prompt(ticker, profile, signals, news, chart_summary, prior),
                      DeepResearchDraft, max_tokens=3500)
    critique = llm.parse(_critique_prompt(ticker, draft), DeepResearchCritique, max_tokens=2000)

    # The final, post-critique call becomes a normal ticket so it flows through the
    # same memory + shadow plumbing as every other recommendation.
    ticket = TradeTicket(
        ticker=ticker, action=critique.final_action, conviction=critique.final_conviction,
        thesis=draft.thesis, catalyst=draft.catalyst, risks=draft.risks,
        suggested_size_pct=draft.suggested_size_pct,
    )
    research_state.update_from_ticket(ticket)  # updates the stored thesis + watchlist + event
    try:
        if not shadow.has_open(ticker, source="deep_research"):
            shadow.log_recommendation(ticket, source="deep_research", profile=profile, signals=signals)
    except Exception:  # noqa: BLE001 — never let the track-record write break the report
        pass

    changed = (critique.final_action != draft.action) or (critique.final_conviction != draft.conviction)
    report = {
        "ticker": ticker,
        "plan": draft.plan,
        "bull_case": draft.bull_case,
        "bear_case": draft.bear_case,
        "evidence": draft.evidence,
        "critique": critique.critique,
        "verdict": ticket.decision_label,
        "action": ticket.action,
        "conviction": ticket.conviction,
        "thesis": draft.thesis,
        "catalyst": draft.catalyst,
        "invalidation": draft.risks,
        "changed": changed,
        "note": critique.note,
        "as_of": _now(),
    }

    try:
        db_repo.save_agent_run(
            query=f"Deep research: {ticker}",
            answer=_render_text(report),
            kind="deep_research",
            # Store the whole structured report so the UI can re-open the exact
            # card later from the audit trail (the readable text lives in answer).
            steps=[{"type": "report", "report": report}],
            tools_used="get_stock_signals,get_stock_news,get_stock_chart",
            model=llm.MODEL,
        )
    except Exception:  # noqa: BLE001
        pass

    return report
