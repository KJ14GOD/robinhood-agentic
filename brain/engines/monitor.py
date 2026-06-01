"""Monitor engine — the always-on, no-LLM event detector.

This is the proactive heartbeat the product is built around: cheap deterministic
checks over data we already compute every refresh (snapshots + signals), turned
into structured `research_events`. It runs on the background loop, so the app
surfaces what changed *without* being clicked and *without* spending tokens.

`detect()` is pure (no IO) so it can be unit-tested; `run_monitors()` wraps it
with dedup + persistence so a standing condition isn't re-logged every cycle.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..data.prices import TrendSignals, get_earnings_date, get_signals_many
from ..models import Portfolio, RiskProfile
from .. import research_state
from ..db import repository as db_repo

# Thresholds — deliberately conservative so the feed stays signal, not noise.
BIG_DRAWDOWN_PCT = 15.0   # position this far below cost → worth a thesis check
BIG_GAIN_PCT = 30.0       # position this far above cost → trim-or-let-it-run prompt
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
EARNINGS_SOON_DAYS = 7    # flag a holding's earnings this many days out
STALE_AFTER_DAYS = 21     # a thesis untouched this long is "stale research"
MONITOR_COOLDOWN_HOURS = 12.0   # don't re-log the same (type, ticker) within this window
EARNINGS_COOLDOWN_HOURS = 48.0  # earnings reminders linger; re-log them less often
STALE_COOLDOWN_HOURS = 168.0    # a stale-research nudge should recur ~weekly, not daily


def days_old(iso: str) -> float | None:
    """Age in days of an ISO timestamp, or None if unparseable. Shared by the
    monitor (stale-research events) and living memory (scheduled revisits)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return None


def detect(pf: Portfolio, profile: RiskProfile, memory,
           signals: dict[str, TrendSignals], earnings: dict | None = None) -> list[dict]:
    """Pure detector pass: portfolio + signals + memory (+ earnings dates) → list
    of event dicts.

    Each event dict matches `save_research_event` kwargs
    (event_type, ticker, severity, title, summary, source). `earnings` maps
    ticker → date (or None); it's passed in so this stays pure/testable.
    """
    events: list[dict] = []
    earnings = earnings or {}
    today = date.today()
    weights = pf.weights()
    held = {h.ticker for h in pf.holdings}
    ceiling = profile.max_single_position_pct or 15.0

    for h in pf.holdings:
        w = weights.get(h.ticker, 0.0)
        sig = signals.get(h.ticker)
        upnl = h.unrealized_pct

        if w > ceiling:
            events.append(dict(
                event_type="concentration", ticker=h.ticker, severity="warn",
                title=f"{h.ticker} is {w:.0f}% of your portfolio",
                summary=f"Above your {ceiling:.0f}% single-position comfort line — concentration risk. Consider trimming.",
                source="monitor"))

        if upnl is not None:
            if upnl <= -BIG_DRAWDOWN_PCT:
                events.append(dict(
                    event_type="drawdown", ticker=h.ticker, severity="alert",
                    title=f"{h.ticker} down {upnl:.0f}% from your cost",
                    summary=f"Unrealized {upnl:+.0f}%. Worth re-checking the thesis or your stop.",
                    source="monitor"))
            elif upnl >= BIG_GAIN_PCT:
                events.append(dict(
                    event_type="big_gain", ticker=h.ticker, severity="info",
                    title=f"{h.ticker} up {upnl:.0f}% from your cost",
                    summary=f"Unrealized {upnl:+.0f}%. Decide: trim into strength or let the winner run.",
                    source="monitor"))

        if sig and sig.price > 0:
            if not sig.above_200d:
                events.append(dict(
                    event_type="below_200d", ticker=h.ticker, severity="warn",
                    title=f"{h.ticker} is below its 200-day average",
                    summary=f"Price ${sig.price:.2f} sits under the 200d MA — the longer-term trend is weakening.",
                    source="monitor"))
            if sig.rsi_14 >= RSI_OVERBOUGHT:
                events.append(dict(
                    event_type="overbought", ticker=h.ticker, severity="info",
                    title=f"{h.ticker} looks overbought (RSI {sig.rsi_14:.0f})",
                    summary="Upside momentum is stretched; near-term pullback risk.",
                    source="monitor"))
            elif 0 < sig.rsi_14 <= RSI_OVERSOLD:
                events.append(dict(
                    event_type="oversold", ticker=h.ticker, severity="info",
                    title=f"{h.ticker} looks oversold (RSI {sig.rsi_14:.0f})",
                    summary="Downside momentum is stretched; watch for a bounce or a falling knife.",
                    source="monitor"))

        thesis = memory.theses.get(h.ticker)
        if thesis and thesis.status in ("review", "broken"):
            sev = "alert" if thesis.status == "broken" else "warn"
            events.append(dict(
                event_type=f"thesis_{thesis.status}", ticker=h.ticker, severity=sev,
                title=f"{h.ticker} thesis marked {thesis.status}",
                summary=(thesis.invalidation or thesis.thesis or "Revisit this stored thesis.")[:200],
                source="monitor"))

        ed = earnings.get(h.ticker)
        if ed is not None:
            delta = (ed - today).days
            if 0 <= delta <= EARNINGS_SOON_DAYS:
                when = "today" if delta == 0 else f"in {delta} day{'s' if delta != 1 else ''}"
                events.append(dict(
                    event_type="earnings_soon", ticker=h.ticker,
                    severity="warn" if delta <= 2 else "info",
                    title=f"{h.ticker} reports earnings {when}",
                    summary="Earnings can revalue the thesis fast — review the position and your stop before the print.",
                    source="monitor"))

    # Watchlist target hits — names you don't own yet that just got cheap enough.
    for item in memory.watchlist:
        if item.ticker in held or item.target_entry <= 0:
            continue
        sig = signals.get(item.ticker)
        if sig and sig.price > 0 and sig.price <= item.target_entry:
            events.append(dict(
                event_type="target_hit", ticker=item.ticker, severity="warn",
                title=f"{item.ticker} hit your ${item.target_entry:.2f} entry target",
                summary=f"Now ${sig.price:.2f}, at or below your watchlist target. {item.reason}".strip()[:200],
                source="monitor"))

    # Stale research — tracked theses that have aged out with no fresh look.
    # Held names are deliberately skipped: living memory re-judges those on a
    # schedule, so flagging them here too would just double up.
    for thesis in getattr(memory, "theses", {}).values():
        if thesis.ticker in held or thesis.status not in ("active", "review"):
            continue
        age = days_old(thesis.updated_at)
        if age is not None and age >= STALE_AFTER_DAYS:
            events.append(dict(
                event_type="research_stale", ticker=thesis.ticker, severity="info",
                title=f"{thesis.ticker} research is {int(age)} days old",
                summary="No fresh look in a while — re-run the analyst or deep research to confirm it still holds.",
                source="monitor"))

    return events


def run_monitors(pf: Portfolio, profile: RiskProfile) -> list[dict]:
    """Detect → dedup → persist. Returns the events actually written this cycle."""
    if not pf.holdings:
        return []
    memory = research_state.load_state()
    held = {h.ticker for h in pf.holdings}
    watch_tickers = [w.ticker for w in memory.watchlist if w.target_entry > 0 and w.ticker not in held]
    signals = get_signals_many([h.ticker for h in pf.holdings] + watch_tickers)
    earnings = {h.ticker: get_earnings_date(h.ticker) for h in pf.holdings}

    cooldowns = {"earnings_soon": EARNINGS_COOLDOWN_HOURS, "research_stale": STALE_COOLDOWN_HOURS}
    saved: list[dict] = []
    for ev in detect(pf, profile, memory, signals, earnings):
        cooldown = cooldowns.get(ev["event_type"], MONITOR_COOLDOWN_HOURS)
        if db_repo.event_exists_recent(ev["event_type"], ev["ticker"], within_hours=cooldown):
            continue
        db_repo.save_research_event(**ev)
        saved.append(ev)
    return saved
