"""Autonomous deep research — the brain decides what to dig into, unprompted.

This is what makes the always-on navigator feel alive: instead of waiting for you to click
"Deep research", the brain watches its own event stream and, when a name hits a high-signal
"re-underwrite it now" moment, runs the full agentic deep dive on its own and drops the finished
report into the ping feed.

Gating is everything here, because a deep dive is the single most expensive thing the brain does
(an agentic loop with web search + EDGAR + multiple model calls). So:
  - Only genuine re-underwrite triggers fire it: a thesis breaking, a thesis under review, or a
    mission name promoted to BUY. Routine signals (RSI, small moves) do NOT.
  - Per-ticker cooldown (DB-timestamp based, survives restarts) — no re-diving the same name for
    days.
  - A hard per-cycle cap so a backlog drains one dive at a time, never a token storm.

A calm book spends nothing.
"""
from __future__ import annotations

from ..db import repository as db_repo
from . import deep_research

# How a deep dive is surfaced + cooldowned. The completed dive logs a `deep_dive` event, which
# both shows up in the ping feed AND serves as the per-ticker cooldown marker.
AUTO_DIVE_COOLDOWN_HOURS = 72.0   # don't auto-dive the same name more than ~once every 3 days
AUTO_DIVE_LOOKBACK_HOURS = 3.0    # only act on triggers fired this recently
AUTO_DIVE_MAX_PER_CYCLE = 1       # at most this many dives per background cycle

# Event types that justify an unprompted deep dive, with a priority (higher = dive first).
# mission_update only counts when severity == "warn" (i.e. a promotion to BUY), not routine relabels.
_TRIGGERS = {"thesis_broken": 3, "thesis_review": 2, "mission_update": 1}


def _candidates() -> list[tuple[int, str, str]]:
    """Recent high-signal triggers → [(priority, ticker, reason)], highest priority first,
    one entry per ticker (its strongest trigger)."""
    best: dict[str, tuple[int, str]] = {}
    for e in db_repo.recent_events(limit=80, within_hours=AUTO_DIVE_LOOKBACK_HOURS):
        etype = e.get("event_type")
        ticker = e.get("ticker")
        if not ticker or etype not in _TRIGGERS:
            continue
        if etype == "mission_update" and e.get("severity") != "warn":
            continue  # only BUY promotions, not routine relabels
        pr = _TRIGGERS[etype]
        if ticker not in best or pr > best[ticker][0]:
            best[ticker] = (pr, e.get("title") or etype)
    ranked = [(pr, tk, reason) for tk, (pr, reason) in best.items()]
    ranked.sort(reverse=True)
    return ranked


def run_due_dives(profile, max_per_cycle: int = AUTO_DIVE_MAX_PER_CYCLE) -> list[dict]:
    """Run autonomous deep dives on triggered, not-recently-dived names. Persists each as a
    `deep_dive` event (the ping) and returns a summary of what ran."""
    done: list[dict] = []
    for _pr, ticker, reason in _candidates():
        if len(done) >= max_per_cycle:
            break
        if db_repo.event_exists_recent("deep_dive", ticker, within_hours=AUTO_DIVE_COOLDOWN_HOURS):
            continue  # dived this name recently — leave it
        try:
            report = deep_research.run(ticker, profile)
        except Exception:  # noqa: BLE001 — one bad dive shouldn't stop the cycle
            continue

        action = (report.get("action") or "").lower()
        verdict = report.get("verdict") or action.upper()
        # An actionable verdict is worth interrupting you for (warn → also a browser ping);
        # a hold/watch dive lands quietly in the feed.
        severity = "warn" if action in ("buy", "add", "sell", "trim") else "info"
        summary = (f"Auto-researched after {reason}. "
                   f"{report.get('note') or report.get('thesis') or ''}").strip()[:240]
        db_repo.save_research_event(
            event_type="deep_dive", ticker=ticker, severity=severity,
            title=f"{ticker} deep dive → {verdict} (conviction {report.get('conviction', '?')}/10)",
            summary=summary, source="autoresearch")
        done.append({"ticker": ticker, "verdict": verdict,
                     "conviction": report.get("conviction"), "trigger": reason})
    return done
