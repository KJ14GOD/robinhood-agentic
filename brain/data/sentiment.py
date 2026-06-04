"""Social sentiment — a read-only, quarantined "what's the crowd doing" layer.

Two free, no-auth sources, combined per ticker:
  - StockTwits: bull/bear ratio from recently tagged messages (the *mood*).
  - ApeWisdom:  Reddit mention volume + 24h change (the *buzz*).

Everything here is best-effort and isolated: any failure returns empty and the
brain simply sees no sentiment. It must never raise into the rest of the system,
and it is never load-bearing — a secondary, contextual signal only. Both are real
public APIs (built to be called), so unlike scraping reddit.com directly they
don't bot-block us.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from .. import config

_UA = "signal-research (personal portfolio research)"
_ST = "https://api.stocktwits.com/api/2/streams/symbol/{t}.json"
_AW = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"

# In-memory TTL caches. No DB, no history needed — ApeWisdom carries its own 24h
# delta, and StockTwits is a point-in-time mood read.
_st_cache: dict = {}                 # ticker -> (fetched_at, payload)
_aw_cache: dict = {"at": 0.0, "map": {}}


def available() -> bool:
    return config.SENTIMENT_ENABLED


def _ttl() -> float:
    return float(config.SENTIMENT_TTL_SECONDS)


def _stocktwits(ticker: str) -> dict | None:
    """Bull/bear from recently tagged StockTwits messages. None on any failure."""
    hit = _st_cache.get(ticker)
    if hit and time.time() - hit[0] < _ttl():
        return hit[1]
    try:
        r = httpx.get(_ST.format(t=ticker), headers={"User-Agent": _UA}, timeout=8.0)
        if r.status_code != 200:
            return None
        bull = bear = 0
        for m in r.json().get("messages", []) or []:
            basic = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if basic == "Bullish":
                bull += 1
            elif basic == "Bearish":
                bear += 1
        tagged = bull + bear
        out = {"bullish_pct": round(bull / tagged * 100) if tagged else None, "tagged": tagged}
        _st_cache[ticker] = (time.time(), out)
        return out
    except Exception:
        return None


def apewisdom_map() -> dict:
    """One call returns the top trending tickers (Reddit mentions + 24h-ago count);
    cache the whole map. Returns the last good map (or {}) on failure."""
    if _aw_cache["map"] and time.time() - _aw_cache["at"] < _ttl():
        return _aw_cache["map"]
    try:
        r = httpx.get(_AW, headers={"User-Agent": _UA}, timeout=8.0)
        if r.status_code != 200:
            return _aw_cache["map"]
        m = {}
        for x in r.json().get("results", []) or []:
            tk = (x.get("ticker") or "").upper()
            if tk:
                m[tk] = x
        _aw_cache.update(at=time.time(), map=m)
        return m
    except Exception:
        return _aw_cache["map"]


def get_sentiment(ticker: str) -> dict | None:
    """Combined social read for one ticker, or None if nothing came back. Best-effort."""
    if not available() or not ticker:
        return None
    ticker = ticker.upper()
    st = _stocktwits(ticker)
    aw = apewisdom_map().get(ticker)
    if not st and not aw:
        return None
    mentions = prev = delta = rank = None
    if aw:
        mentions = int(aw.get("mentions", 0) or 0)
        prev = int(aw.get("mentions_24h_ago", 0) or 0)
        rank = aw.get("rank")
        delta = round((mentions - prev) / prev * 100) if prev else None
    return {
        "ticker": ticker,
        "bullish_pct": (st or {}).get("bullish_pct"),
        "tagged": (st or {}).get("tagged", 0),
        "mentions": mentions,
        "mentions_prev": prev,
        "mention_delta_pct": delta,
        "rank": rank,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def sentiment_prompt(ticker: str) -> str:
    """One-line social read for an analysis prompt. Empty when there's nothing, so
    callers drop it in unconditionally. Labeled as secondary context, never fact."""
    try:
        s = get_sentiment(ticker)
    except Exception:
        return ""
    if not s:
        return ""
    parts = []
    if s.get("bullish_pct") is not None and s.get("tagged"):
        parts.append(f"StockTwits {s['bullish_pct']}% bullish ({s['tagged']} tagged)")
    if s.get("mentions") is not None:
        d = s.get("mention_delta_pct")
        trend = f", {d:+d}% vs yesterday" if isinstance(d, int) else ""
        parts.append(f"Reddit mentions {s['mentions']}{trend}")
    if not parts:
        return ""
    return "SOCIAL SENTIMENT (secondary crowd context, not fact): " + "; ".join(parts) + "."
