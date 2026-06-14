"""Judge engine — the brain grading its own reasoning (eval layer, Phase 2).

Phase 1 built the failure taxonomy and a human-labeling surface. This is the payoff: an
LLM-as-judge that scores every reasoning trace against that SAME taxonomy the instant it's
produced — a verdict, a 0-100 quality score, which failure modes fired, and a per-claim
grounding check (is each load-bearing claim actually backed by a cited source?). Unlike the
outcome Scorecard (slow, market-gated) this is the *process* eval: did the agent do the job
well — grounded, sourced, falsifiable, profile-aware — judgeable now.

The agentic half is the gate: when a fresh recommendation is judged FLAWED on a load-bearing
failure mode, the brain repairs it once — fed the judge's specific criticism — before it ever
reaches the user. The agent is held to the user's own quality bar in real time. Best-effort
throughout: a judging or repair hiccup degrades to shipping the original call, never blocks it.
"""
from __future__ import annotations

import re

from .. import config, evals, llm
from ..db import repository as db_repo
from ..models import JudgeAssessment, RiskProfile, TradeTicket


def _evidence_block(evidence_text: str, sources: list[dict] | None) -> str:
    parts: list[str] = []
    if evidence_text and evidence_text.strip():
        parts.append("EVIDENCE THE CALL READ (its grounding):\n" + evidence_text.strip()[:6000])
    cited = "\n".join(f"- {s.get('title') or s.get('url')}" for s in (sources or []) if s.get("url"))
    if cited:
        parts.append("CITED SOURCES:\n" + cited[:2000])
    if not parts:
        parts.append("EVIDENCE: none captured — judge grounding against the quantitative signals the call "
                     "cites, and flag weak_grounding if load-bearing claims aren't supported.")
    return "\n\n".join(parts)


def _judge_prompt(kind: str, ticker: str, call_block: str, signals_prompt: str,
                  evidence_text: str, sources: list[dict] | None,
                  mandate_block: str, profile: RiskProfile) -> str:
    return f"""You are the QUALITY JUDGE for this research engine. Score the reasoning below against the
user's own failure taxonomy. You judge the PROCESS, not the market outcome: did the agent do the
job well — grounded, sourced, falsifiable, and true to THIS user — regardless of whether the trade
eventually wins or loses.

{mandate_block or ''}

INVESTOR PROFILE (the call must respect this):
{profile.describe()}

THE CALL BEING JUDGED ({kind} on {ticker}):
{call_block}

QUANTITATIVE SIGNALS available to the call:
{signals_prompt}

{_evidence_block(evidence_text, sources)}

FAILURE TAXONOMY — score against exactly these modes (return the ids):
{evals.taxonomy_prompt()}

Judge rigorously and specifically:
- Identify the load-bearing claims (the facts, numbers, and named drivers the call rests on) and
  check each against the evidence ACTUALLY provided. An unsupported load-bearing claim is
  weak_grounding; an invented figure is hallucinated_fact; a source that doesn't say what's
  claimed is source_mismatch.
- A thesis with no concrete invalidation is not_falsifiable. Conviction out of line with the
  strength of the evidence is overconfident. A call that ignores the profile or mandate is
  ignored_profile.
- Be calibrated. Reserve a score of 85+ for genuinely rigorous, well-sourced, falsifiable calls.
  Most decent calls land 60-80. Only flag failure modes that actually apply — do not pad the list.
Return your verdict, the score, the failure-mode ids that apply, the grounding checks (the
load-bearing claims and whether each is supported), a short rationale, and — if it's not 'good' —
the single most important fix."""


def _ticket_block(t: TradeTicket) -> str:
    return (f"ACTION: {t.action.upper()} ({t.decision_label}) at conviction {t.conviction}/10, "
            f"suggested size {t.suggested_size_pct:.0f}% of portfolio\n"
            f"THESIS: {t.thesis}\nCATALYST: {t.catalyst}\nRISKS / INVALIDATION: {t.risks}\n"
            f"FITS PROFILE BECAUSE: {t.fits_profile_because}")


def assess_ticket(ticket: TradeTicket, profile: RiskProfile, signals_prompt: str = "",
                  evidence_text: str = "", sources: list[dict] | None = None,
                  mandate_block: str = "") -> JudgeAssessment | None:
    """Score one recommendation against the taxonomy. Returns None on any failure (best-effort)."""
    if not config.JUDGE_ENABLED:
        return None
    try:
        return llm.parse(
            _judge_prompt("analyst recommendation", ticket.ticker, _ticket_block(ticket),
                          signals_prompt or "(not provided)", evidence_text, sources, mandate_block, profile),
            JudgeAssessment, max_tokens=1600, effort=config.JUDGE_EFFORT)
    except Exception:  # noqa: BLE001
        return None


def assess_text(kind: str, ticker: str, call_block: str, profile: RiskProfile,
                signals_prompt: str = "", evidence_text: str = "", sources: list[dict] | None = None,
                mandate_block: str = "") -> JudgeAssessment | None:
    """Score an arbitrary rendered trace (deep report, re-judge) — used by the background sweep."""
    if not config.JUDGE_ENABLED or not (call_block or "").strip():
        return None
    try:
        return llm.parse(
            _judge_prompt(kind, ticker, call_block, signals_prompt or "(not provided)",
                          evidence_text, sources, mandate_block, profile),
            JudgeAssessment, max_tokens=1600, effort=config.JUDGE_EFFORT)
    except Exception:  # noqa: BLE001
        return None


def _repair_prompt(ticket: TradeTicket, a: JudgeAssessment, signals_prompt: str,
                   evidence_text: str, sources: list[dict] | None,
                   mandate_block: str, profile: RiskProfile) -> str:
    failed = ", ".join(evals.pretty(t) for t in a.failure_modes) or "quality below the bar"
    return f"""Your draft recommendation for {ticket.ticker} did not clear the quality bar — a reviewer
flagged it. Repair it: fix exactly what's broken, keep what's sound, and do NOT inflate conviction
to compensate. If the honest answer is a lower-conviction or a 'watch' call, make that call.

{mandate_block or ''}

INVESTOR PROFILE:
{profile.describe()}

YOUR DRAFT:
{_ticket_block(ticket)}

REVIEWER VERDICT: {a.verdict} ({a.score}/100)
FAILED ON: {failed}
WHY: {a.rationale}
MOST IMPORTANT FIX: {a.fix}

QUANTITATIVE SIGNALS (reason from these, do not invent):
{signals_prompt or '(not provided)'}

{_evidence_block(evidence_text, sources)}

Produce the corrected recommendation for the SAME ticker: ground every load-bearing claim in the
evidence above (or drop the claim), make the thesis falsifiable with a concrete invalidation, and
set conviction to match the real strength of the evidence."""


def repair_ticket(ticket: TradeTicket, a: JudgeAssessment, profile: RiskProfile,
                  signals_prompt: str = "", evidence_text: str = "", sources: list[dict] | None = None,
                  mandate_block: str = "") -> TradeTicket | None:
    """One targeted repair pass fed the judge's specific criticism. None on failure."""
    try:
        fixed = llm.parse(
            _repair_prompt(ticket, a, signals_prompt, evidence_text, sources, mandate_block, profile),
            TradeTicket, max_tokens=2200)
        fixed.ticker = ticket.ticker
        return fixed
    except Exception:  # noqa: BLE001
        return None


def gate_ticket(ticket: TradeTicket, profile: RiskProfile, signals_prompt: str = "",
                evidence_text: str = "", sources: list[dict] | None = None,
                mandate_block: str = "") -> tuple[TradeTicket, JudgeAssessment | None, bool]:
    """Judge a fresh recommendation and, if it's flawed on a load-bearing mode, repair it once
    before it ships. Returns (final_ticket, final_assessment, revised). Always returns a usable
    ticket — judging or repair never blocks the call."""
    a = assess_ticket(ticket, profile, signals_prompt, evidence_text, sources, mandate_block)
    if a is None:
        return ticket, None, False
    if config.SELF_CRITIQUE and a.verdict == "flawed" and evals.is_load_bearing(a.failure_modes):
        fixed = repair_ticket(ticket, a, profile, signals_prompt, evidence_text, sources, mandate_block)
        if fixed is not None:
            a2 = assess_ticket(fixed, profile, signals_prompt, evidence_text, sources, mandate_block)
            # Keep the repair only if the judge re-scores it no worse than the original.
            if a2 is not None and a2.score >= a.score:
                return fixed, a2, True
    return ticket, a, False


def record(run_id: str | None, kind: str, ticker: str,
           assessment: JudgeAssessment | None, revised: bool = False) -> None:
    """Persist a judgement against the trace's agent_run id (best-effort)."""
    if not run_id or assessment is None:
        return
    try:
        db_repo.save_eval_judgement(
            run_id=run_id, kind=kind, ticker=ticker, verdict=assessment.verdict,
            score=assessment.score, failure_modes=assessment.failure_modes,
            grounding=[g.model_dump() for g in assessment.grounding],
            rationale=assessment.rationale, fix=assessment.fix, revised=revised, model=llm.MODEL)
    except Exception:  # noqa: BLE001
        pass


# --- reconstruct a judge-able block from a stored trace (the background sweep) --------------- #
def _trace_ticker(run: dict) -> str:
    step = (run.get("steps") or [{}])[0]
    t = step.get("ticker") or (step.get("report") or {}).get("ticker")
    if t:
        return str(t).upper()
    q = run.get("query") or ""
    rest = re.sub(r"^(analyze|re-?judge|deep research)\W*", "", q, flags=re.I)
    return (rest.split() or [""])[0].upper()


def block_from_trace(run: dict) -> tuple[str, str]:
    """Reconstruct a judge-able call block + ticker from a stored agent_run (for the sweep)."""
    kind = run.get("kind")
    step = (run.get("steps") or [{}])[0]
    tk = _trace_ticker(run)
    if kind == "analyst":
        block = (f"ACTION: {str(step.get('action', '')).upper()} ({step.get('label', '')}) at "
                 f"conviction {step.get('conviction', '?')}/10\nTHESIS: {step.get('thesis', '')}\n"
                 f"CATALYST: {step.get('catalyst', '')}\nRISKS: {step.get('risks', '')}")
    elif kind == "deep_research":
        rep = step.get("report") or {}
        block = (f"ACTION: {str(rep.get('action', '')).upper()} ({rep.get('verdict', '')}) at "
                 f"conviction {rep.get('conviction', '?')}/10\nTHESIS: {rep.get('thesis', '')}\n"
                 f"CATALYST: {rep.get('catalyst', '')}\nINVALIDATION: {rep.get('invalidation', '')}\n"
                 f"BULL: {' | '.join(rep.get('bull_case', []))}\nBEAR: {' | '.join(rep.get('bear_case', []))}")
    elif kind == "rejudge":
        block = (f"STATUS: {step.get('status', '')} ({step.get('label', '')})\n"
                 f"TRIGGER: {step.get('trigger', '')}\nVERDICT: {step.get('reason', '')}")
    else:
        block = (run.get("answer") or "")[:2000]
    return block.strip(), tk


def sources_from_trace(run: dict) -> list[dict]:
    step = (run.get("steps") or [{}])[0]
    return step.get("sources") or (step.get("report") or {}).get("sources") or []
