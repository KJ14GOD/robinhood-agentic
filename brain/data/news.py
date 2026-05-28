"""Free news via RSS (Yahoo Finance per-ticker feeds + Google News fallback).

Returns recent headlines the LLM synthesizes. No paid API; swap in Finnhub/
Polygon news later behind this same `get_news` signature.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote_plus

import feedparser

from .. import config


@dataclass
class Headline:
    title: str
    publisher: str
    published: str
    link: str


def _yahoo_feed(ticker: str) -> str:
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"


def _google_feed(query: str) -> str:
    q = quote_plus(f"{query} stock")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


_NEWS_CACHE: dict[str, tuple[float, list[Headline]]] = {}


def clear_news_cache(tickers: list[str] | None = None) -> None:
    if tickers is None:
        _NEWS_CACHE.clear()
        return
    for t in tickers:
        _NEWS_CACHE.pop(t.upper().strip(), None)


def get_news(ticker: str, limit: int = 6, refresh: bool = False) -> list[Headline]:
    ticker = ticker.upper().strip()
    hit = _NEWS_CACHE.get(ticker)
    if hit and not refresh and time.time() - hit[0] < config.NEWS_TTL_SECONDS:
        return hit[1][:limit]
    out: list[Headline] = []
    for url in (_yahoo_feed(ticker), _google_feed(ticker)):
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit]:
                out.append(
                    Headline(
                        title=getattr(entry, "title", ""),
                        publisher=getattr(getattr(entry, "source", None), "title", "")
                        or getattr(entry, "publisher", "")
                        or "news",
                        published=getattr(entry, "published", ""),
                        link=getattr(entry, "link", ""),
                    )
                )
            if out:
                break
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.1)
    _NEWS_CACHE[ticker] = (time.time(), out)
    return out[:limit]


def headlines_as_prompt(ticker: str, limit: int = 6, refresh: bool = False) -> str:
    hs = get_news(ticker, limit, refresh=refresh)
    if not hs:
        return f"{ticker}: no recent headlines found."
    lines = [f"Recent {ticker} headlines:"]
    for h in hs:
        lines.append(f"- {h.title} ({h.publisher})")
    return "\n".join(lines)
