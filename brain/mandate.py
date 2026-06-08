"""Mandate engine — the standing goal the agent works toward.

This is the cockpit's core: the user states what they're trying to do in plain words
("long-term growth, hold 1+ year, moderate risk, no meme stocks"); the brain reads it
back, stores it, and aligns every recommendation to it. The `review` pass is the agentic
advisor read — how the portfolio stands against the mandate, and the few moves that serve
it — in plain language, grounded in the actual book.
"""
from __future__ import annotations

from . import llm
from .db import repository as db_repo
from .models import Mandate, MandateExtract, PlanReview, Portfolio, RiskProfile
from .data.prices import get_signals_many


def get_mandate() -> Mandate:
    return db_repo.load_mandate()


def mandate_prompt() -> str:
    """The standing-instruction block for any recommendation prompt. Empty if unset, so
    callers drop it in unconditionally."""
    try:
        return get_mandate().describe()
    except Exception:  # noqa: BLE001
        return ""


def _extract_prompt(text: str) -> str:
    return f"""The user just described their investing goal in their own words. Read it and extract
the structured intent — don't invent anything they didn't say; leave a field empty if it's unclear.

USER'S GOAL: {text}

Pull out the time horizon, risk tolerance, and style if implied, plus any sectors/themes to favor
or avoid. Then write ONE plain sentence confirming the goal back to them, in your words."""


def set_mandate(text: str) -> Mandate:
    """Store the user's goal, with the brain's structured reading of it. The statement is the
    source of truth; the parse is best-effort (a failure still saves the raw statement)."""
    text = (text or "").strip()
    if not text:
        return get_mandate()
    try:
        ext = llm.parse(_extract_prompt(text), MandateExtract, max_tokens=800)
    except Exception:  # noqa: BLE001 — never lose the user's words to a parse failure
        ext = MandateExtract(horizon="", risk="", style="", favor=[], avoid=[],
                             summary=text[:200])
    m = Mandate(statement=text, horizon=ext.horizon, risk=ext.risk, style=ext.style,
                favor=ext.favor, avoid=ext.avoid, summary=ext.summary)
    db_repo.save_mandate(m)
    return m


def _holdings_block(pf: Portfolio) -> str:
    if not pf.holdings:
        return "The portfolio currently holds no positions (cash only)."
    weights = pf.weights()
    sigs = get_signals_many([h.ticker for h in pf.holdings])
    lines = []
    for h in pf.holdings:
        s = sigs.get(h.ticker)
        w = weights.get(h.ticker, 0.0)
        up = h.unrealized_pct
        ups = f"{up:+.0f}% from cost" if up is not None else "cost basis n/a"
        trend = ""
        if s and s.price > 0:
            trend = f", {'above' if s.above_200d else 'below'} 200d, RSI {s.rsi_14:.0f}, 3m {s.ret_3m_pct:+.0f}%"
            if s.pe and s.pe > 0:
                trend += f", P/E {s.pe:.0f}"
        lines.append(f"- {h.ticker}: {w:.0f}% of book, {ups}{trend}")  # weights() is already a percentage
    return "CURRENT HOLDINGS:\n" + "\n".join(lines)


def _review_prompt(m: Mandate, profile: RiskProfile, pf: Portfolio, holdings: str) -> str:
    return f"""You are the user's investing advisor. Judge their portfolio against their stated mandate
and give a short, plain-language read plus the few moves that would better serve the mandate.

{m.describe()}

INVESTOR PROFILE (secondary to the mandate): {profile.describe()}

PORTFOLIO: total value ${pf.total_value:,.0f}, cash ${pf.cash:,.0f}.
{holdings}

Assess: how well does this book fit the mandate *right now*? Then give 1-3 concrete moves that would
serve the mandate better — trimming a name that doesn't fit, holding a core one, or the kind of
exposure that's missing (name a ticker only if you're confident). Be specific and grounded in the
holdings above; don't pad the list — if the book already fits well, say so and give zero or one move.
Speak plainly, like an advisor talking to a person, not a research report."""


def review(pf: Portfolio, profile: RiskProfile) -> PlanReview | None:
    """The advisor read: portfolio vs mandate → alignment + a few grounded moves. None if no
    mandate is set yet."""
    m = get_mandate()
    if not m.is_set():
        return None
    try:
        return llm.parse(_review_prompt(m, profile, pf, _holdings_block(pf)), PlanReview, max_tokens=1500)
    except Exception:  # noqa: BLE001
        return None
