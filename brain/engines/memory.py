"""Living Memory engine — make stored theses react instead of just sitting there.

When a held position trips a price/trend trigger (big drawdown, below its
200-day, oversold), this re-judges that ticker's *stored thesis against its own
invalidation condition* with a single gated LLM call, then moves the thesis
forward (active → review → broken) and logs a high-signal event.

Gating is the whole point: the LLM only runs when (1) you still hold the name,
(2) there's a concrete price/trend reason to look, and (3) we haven't already
re-judged it in the last day. So on a calm book it spends nothing. It reuses the
signals the monitor already cached this cycle, so it adds no extra data load.
"""
from __future__ import annotations

from ..data.news import headlines_as_prompt
from ..data.prices import TrendSignals, get_signals_many
from ..models import Holding, Portfolio, RiskProfile, ThesisVerdict, _now
from .. import llm, research_state
from ..db import repository as db_repo
from . import monitor

TRIGGER_COOLDOWN_HOURS = 24.0

# Which verdict maps to which event type / severity in the "What changed" feed.
_OUTCOME = {
    "broken": ("thesis_broken", "alert"),
    "review": ("thesis_review", "warn"),
    "active": ("thesis_affirmed", "info"),
}
_COOLDOWN_TYPES = [etype for etype, _ in _OUTCOME.values()]


def trigger_reason(holding: Holding, sig: TrendSignals | None) -> str | None:
    """Cheap pre-check (no LLM): is there a concrete reason to re-examine this
    thesis right now? Returns a short description, or None to skip."""
    reasons: list[str] = []
    upnl = holding.unrealized_pct
    if upnl is not None and upnl <= -monitor.BIG_DRAWDOWN_PCT:
        reasons.append(f"down {upnl:.0f}% from cost")
    if sig and sig.price > 0 and not sig.above_200d:
        reasons.append("trading below its 200-day average")
    if sig and 0 < sig.rsi_14 <= monitor.RSI_OVERSOLD:
        reasons.append(f"oversold (RSI {sig.rsi_14:.0f})")
    return "; ".join(reasons) or None


def _judge(thesis, holding: Holding, sig: TrendSignals | None, trigger: str) -> ThesisVerdict | None:
    """The one gated LLM call: is the thesis still intact, or did its invalidation
    condition actually trigger?"""
    news = headlines_as_prompt(thesis.ticker, limit=5)
    upnl = holding.unrealized_pct
    prompt = f"""Re-judge this stored investment thesis against what just changed. Be conservative.

TICKER: {thesis.ticker}
STORED THESIS: {thesis.thesis or 'none on file'}
INVALIDATION CONDITION (what would prove it wrong): {thesis.invalidation or 'not specified'}
STRENGTHENS IT: {', '.join(thesis.strengthens) or 'n/a'}
WEAKENS IT: {', '.join(thesis.weakens) or 'n/a'}

WHAT TRIGGERED THIS REVIEW: {trigger}
POSITION: unrealized {upnl:+.0f}% from cost.{'' if upnl is not None else ' (cost basis unknown)'}
CURRENT SIGNALS: {sig.as_prompt() if sig else 'n/a'}
{news}

Has the invalidation condition ACTUALLY been met, or is this normal volatility?
- status 'broken' ONLY if the evidence clearly matches the stated invalidation.
- status 'review' if the thesis is at genuine risk and warrants a human look.
- status 'active' if it still holds and this is noise.
Give the matching action label and ONE grounded sentence citing the specific evidence."""
    try:
        return llm.parse(prompt, ThesisVerdict, max_tokens=1200)
    except Exception:
        return None


def revisit_theses(pf: Portfolio, profile: RiskProfile) -> list[dict]:
    """Re-judge triggered, still-held, not-recently-checked theses. Persists status
    changes + events. Returns a summary of what moved."""
    state = research_state.load_state()
    if not state.theses or not pf.holdings:
        return []

    held = {h.ticker: h for h in pf.holdings}
    candidates = [t for t in state.theses.values()
                  if t.status in ("active", "review") and t.ticker in held]
    if not candidates:
        return []

    signals = get_signals_many([t.ticker for t in candidates])
    changed: list[dict] = []
    dirty = False

    for thesis in candidates:
        holding = held[thesis.ticker]
        trigger = trigger_reason(holding, signals.get(thesis.ticker))
        if not trigger:
            continue
        if db_repo.event_exists_recent(_COOLDOWN_TYPES, thesis.ticker, TRIGGER_COOLDOWN_HOURS):
            continue  # already re-judged this name within the cooldown — don't burn a call

        verdict = _judge(thesis, holding, signals.get(thesis.ticker), trigger)
        if verdict is None:
            continue

        prev = thesis.status
        thesis.status = verdict.status
        thesis.last_decision = verdict.decision_label
        thesis.updated_at = _now()
        # Accumulate the evidence ledger so the thesis genuinely *evolves* over time
        # instead of being a static copy of the latest analysis. An affirmed verdict
        # strengthens the thesis; a review/broken one is a pressure against it.
        note = f"{_now()[:10]}: {verdict.reason}"
        if verdict.status == "active":
            thesis.strengthens = (thesis.strengthens + [note])[-6:]
        else:
            thesis.weakens = (thesis.weakens + [note])[-6:]
        dirty = True

        etype, severity = _OUTCOME[verdict.status]
        db_repo.save_research_event(
            event_type=etype, ticker=thesis.ticker, severity=severity,
            title=f"{thesis.ticker} thesis → {verdict.status} ({verdict.decision_label})",
            summary=verdict.reason, source="memory")
        changed.append({"ticker": thesis.ticker, "from": prev, "to": verdict.status,
                        "label": verdict.decision_label, "reason": verdict.reason})

    if dirty:
        research_state.save_state(state)
    return changed
