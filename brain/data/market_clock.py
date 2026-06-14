"""US equity market clock — when the Twin can actually trade.

The autonomous paper fund only fills orders while the market is open (regular session,
9:30-16:00 ET, weekdays, minus NYSE holidays). Off-hours it researches and queues moves
that fill at the next open. No external market-calendar dependency: regular hours via
zoneinfo (DST handled automatically) and a hardcoded NYSE full-holiday set (extend yearly).
Early-close half-days are treated as normal sessions — immaterial for a daily-cadence sim.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)

# NYSE full-closure holidays. Hardcoded (no market-calendar dep) — extend each year.
_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def now_et() -> datetime:
    return datetime.now(_ET)


def _et(dt: datetime | None) -> datetime:
    if dt is None:
        return now_et()
    return dt.astimezone(_ET) if dt.tzinfo else dt.replace(tzinfo=_ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def is_market_open(dt: datetime | None = None) -> bool:
    """Is the regular US session open at this instant?"""
    d = _et(dt)
    return is_trading_day(d.date()) and OPEN <= d.time() < CLOSE


def session_phase(dt: datetime | None = None) -> str:
    """'open' = the Twin can fill orders; 'closed' = research + queue for the next open."""
    return "open" if is_market_open(dt) else "closed"


def next_open(dt: datetime | None = None) -> datetime:
    """The next instant the market opens (today's open if we're before it on a trading day)."""
    d = _et(dt)
    if is_trading_day(d.date()) and d.time() < OPEN:
        return datetime.combine(d.date(), OPEN, _ET)
    nd = d.date() + timedelta(days=1)
    while not is_trading_day(nd):
        nd += timedelta(days=1)
    return datetime.combine(nd, OPEN, _ET)
