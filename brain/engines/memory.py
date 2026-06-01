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

import re

from ..data.news import get_news, headlines_as_prompt
from ..data.prices import TrendSignals, get_earnings_date, get_signals_many
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


# Common words to ignore when matching a thesis's drivers against headlines.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "will", "its",
    "are", "has", "have", "but", "not", "you", "your", "their", "would", "could",
    "than", "then", "over", "under", "above", "below", "more", "less", "been",
    "they", "them", "was", "were", "which", "what", "when", "while", "about",
}


def _thesis_terms(thesis) -> set[str]:
    """The thesis's named drivers as lowercase keywords — drawn from its
    invalidation condition and accumulated strengthens/weakens (not the prose),
    so a news match is about a *specific* driver, not any generic word."""
    text = " ".join([thesis.invalidation or ""] + list(thesis.strengthens) + list(thesis.weakens))
    words = re.findall(r"[a-z][a-z0-9\-]{3,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _news_trigger(thesis) -> str | None:
    """Fires when a recent headline mentions one of the thesis's named drivers."""
    terms = _thesis_terms(thesis)
    if not terms:
        return None
    try:
        for h in get_news(thesis.ticker, limit=6):
            title = (h.title or "").lower()
            if any(term in title for term in terms):
                return f'recent news may bear on the thesis: "{h.title.strip()}"'[:180]
    except Exception:  # noqa: BLE001 — news is best-effort; never block a revisit
        return None
    return None


def _earnings_trigger(ticker: str) -> str | None:
    """Fires when earnings are within the soon-window or just passed."""
    from datetime import date

    ed = get_earnings_date(ticker)
    if ed is None:
        return None
    delta = (ed - date.today()).days
    if delta == 0:
        return "reports earnings today"
    if 0 < delta <= monitor.EARNINGS_SOON_DAYS:
        return f"reports earnings in {delta} day{'s' if delta != 1 else ''}"
    if -3 <= delta < 0:
        return "just reported earnings"
    return None


def _stale_trigger(thesis) -> str | None:
    """Fires when a thesis has aged out with no recent look — a scheduled re-check
    even on a calm book, so stored views don't silently rot. Self-resetting: a
    re-judgement stamps updated_at, so it won't fire again for another window."""
    age = monitor.days_old(thesis.updated_at)
    if age is not None and age >= monitor.STALE_AFTER_DAYS:
        return f"research is {int(age)} days old (scheduled re-check)"
    return None


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
        sig = signals.get(thesis.ticker)
        # Re-examine on price/trend OR an earnings window OR news that hits a
        # named driver — fundamentals now trigger a revisit, not just price.
        reasons = [r for r in (
            trigger_reason(holding, sig),
            _earnings_trigger(thesis.ticker),
            _news_trigger(thesis),
            _stale_trigger(thesis),
        ) if r]
        if not reasons:
            continue
        if db_repo.event_exists_recent(_COOLDOWN_TYPES, thesis.ticker, TRIGGER_COOLDOWN_HOURS):
            continue  # already re-judged this name within the cooldown — don't burn a call

        verdict = _judge(thesis, holding, sig, "; ".join(reasons))
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
