"""Catalyst radar — structured company news via Finnhub.

A read-only, quarantined "what just happened on this name" layer. Unlike the RSS
headlines (thin, untimestamped) this returns real items with a timestamp, source,
url, and category — so the brain can (1) ping you when a *fresh* catalyst lands and
(2) ground its analysis in precise, recent, cited news.

Everything here is best-effort and isolated: any failure (no key, rate limit, bad
response) returns empty and the brain simply sees no catalysts. It must never raise
into the rest of the system. Gated on the API key: with no key, `available()` is
False and every call is a clean no-op.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .. import config

_URL = "https://finnhub.io/api/v1/company-news"
_UA = "signal-research (personal portfolio research)"

# ticker -> (fetched_at, items). In-memory TTL cache; no DB, no history needed.
_cache: dict[str, tuple[float, list]] = {}


@dataclass
class Catalyst:
    ticker: str
    headline: str
    summary: str
    source: str
    url: str
    category: str
    dt: datetime          # publish time (UTC)

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.dt).total_seconds() / 3600.0


def available() -> bool:
    return bool(config.FINNHUB_ENABLED and config.FINNHUB_API_KEY)


def _ttl() -> float:
    return float(config.FINNHUB_TTL_SECONDS)


def get_company_news(ticker: str, days: int = 7) -> list[Catalyst]:
    """Recent company news for one ticker, newest first. Best-effort; [] on any failure."""
    if not available() or not ticker:
        return []
    ticker = ticker.upper()
    hit = _cache.get(ticker)
    if hit and time.time() - hit[0] < _ttl():
        return hit[1]
    now = datetime.now(timezone.utc)
    frm = (now.timestamp() - days * 86400)
    params = {
        "symbol": ticker,
        "from": datetime.fromtimestamp(frm, timezone.utc).strftime("%Y-%m-%d"),
        "to": now.strftime("%Y-%m-%d"),
        "token": config.FINNHUB_API_KEY,
    }
    try:
        r = httpx.get(_URL, params=params, headers={"User-Agent": _UA}, timeout=8.0)
        if r.status_code != 200:
            return _cache.get(ticker, (0.0, []))[1]
        out: list[Catalyst] = []
        for x in (r.json() or []):
            ts = x.get("datetime")
            if not ts:
                continue
            out.append(Catalyst(
                ticker=ticker,
                headline=(x.get("headline") or "").strip(),
                summary=(x.get("summary") or "").strip(),
                source=(x.get("source") or "").strip(),
                url=(x.get("url") or "").strip(),
                category=(x.get("category") or "").strip(),
                dt=datetime.fromtimestamp(int(ts), timezone.utc),
            ))
        out.sort(key=lambda c: c.dt, reverse=True)
        _cache[ticker] = (time.time(), out)
        return out
    except Exception:
        return _cache.get(ticker, (0.0, []))[1]


def fresh_items(ticker: str, within_hours: float) -> list[Catalyst]:
    """All catalysts newer than `within_hours`, newest first (the list is sorted, so we
    stop at the first stale one)."""
    out: list[Catalyst] = []
    for c in get_company_news(ticker):
        if c.age_hours > within_hours:
            break
        if c.headline:
            out.append(c)
    return out


def latest_fresh(ticker: str, within_hours: float) -> Catalyst | None:
    """The single most recent catalyst newer than `within_hours`, or None."""
    items = fresh_items(ticker, within_hours)
    return items[0] if items else None


def catalysts_prompt(ticker: str, limit: int = 5, days: int = 14) -> str:
    """Compact, timestamped recent-catalyst block for an analysis prompt. Empty when
    there's nothing, so callers drop it in unconditionally. Never raises."""
    try:
        items = [c for c in get_company_news(ticker, days=days) if c.headline][:limit]
    except Exception:
        return ""
    if not items:
        return ""
    lines = []
    for c in items:
        when = c.dt.strftime("%b %d")
        src = f" ({c.source})" if c.source else ""
        lines.append(f"- {when}{src}: {c.headline}")
    return "RECENT CATALYSTS (structured news feed, dated):\n" + "\n".join(lines)
