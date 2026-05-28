"""Prices, fundamentals, and trend signals via yfinance (free).

Everything quantitative the brain reasons over enters here. The LLM is never
asked to recall or predict prices — it only interprets these grounded numbers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache

import pandas as pd
import yfinance as yf

# Quiet yfinance's noisy "possibly delisted" stderr logging — we handle empty
# data gracefully (delisted/acquired holdings fall back to the broker's price).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class Quote:
    ticker: str
    price: float = 0.0
    name: str = ""
    ok: bool = True
    error: str = ""


@dataclass
class TrendSignals:
    """Deterministic technical/fundamental signals for one ticker."""
    ticker: str
    price: float = 0.0
    name: str = ""
    sector: str = ""
    market_cap: float = 0.0
    pe: float = 0.0
    beta: float = 0.0
    dividend_yield: float = 0.0
    ret_1m_pct: float = 0.0
    ret_3m_pct: float = 0.0
    ret_6m_pct: float = 0.0
    above_50d: bool = False
    above_200d: bool = False
    vol_annualized_pct: float = 0.0
    rsi_14: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        return (
            f"{self.ticker} ({self.name}, {self.sector or 'n/a'}): "
            f"price ${self.price:.2f}, mktcap ${self.market_cap/1e9:.1f}B, "
            f"P/E {self.pe:.1f}, beta {self.beta:.2f}, "
            f"div yield {self.dividend_yield:.2f}%, "
            f"returns 1m {self.ret_1m_pct:+.1f}% / 3m {self.ret_3m_pct:+.1f}% / 6m {self.ret_6m_pct:+.1f}%, "
            f"{'above' if self.above_50d else 'below'} 50d MA, "
            f"{'above' if self.above_200d else 'below'} 200d MA, "
            f"RSI(14) {self.rsi_14:.0f}, annualized vol {self.vol_annualized_pct:.0f}%."
        )


def clean_ticker(ticker: str) -> str:
    """Normalize a symbol for yfinance.

    Robinhood/feeds sometimes emit symbols yfinance can't price: a trailing
    `^` (corporate-action/placeholder marker) or a `.` class separator
    (BRK.B). Strip the marker and map `.`→`-` so we get a usable symbol.
    """
    t = (ticker or "").upper().strip()
    t = t.split("^")[0]          # VERV^ -> VERV
    t = t.replace(".", "-")      # BRK.B -> BRK-B
    return t


@lru_cache(maxsize=512)
def get_quote(ticker: str) -> Quote:
    ticker = clean_ticker(ticker)
    if not ticker:
        return Quote(ticker=ticker, ok=False, error="empty symbol")
    try:
        t = yf.Ticker(ticker)
        fast = t.fast_info
        price = float(fast.get("last_price") or fast.get("lastPrice") or 0.0)
        if not price:
            hist = t.history(period="5d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
        name = ""
        try:
            name = t.info.get("shortName", "") or ""
        except Exception:
            pass
        return Quote(ticker=ticker, price=price, name=name, ok=price > 0)
    except Exception as e:  # noqa: BLE001
        return Quote(ticker=ticker, ok=False, error=str(e))


def _rsi(closes, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass
class ScreenRow:
    """Lightweight, fast signals computed from batched price history only —
    no per-ticker fundamentals call. Used to rank a large universe quickly."""
    ticker: str
    price: float = 0.0
    ret_1m_pct: float = 0.0
    ret_3m_pct: float = 0.0
    ret_6m_pct: float = 0.0
    above_50d: bool = False
    above_200d: bool = False
    rsi_14: float = 0.0
    vol_annualized_pct: float = 0.0


import time as _time

_SCREEN_CACHE: dict[str, tuple[float, list]] = {}
_SCREEN_TTL = 1800  # 30 min — intraday momentum doesn't shift faster than this matters


def screen_universe(tickers: list[str]) -> list[ScreenRow]:
    """Download a year of closes for the whole list in ONE request and compute
    momentum/trend signals in memory. This is what makes a 500-name screen fast.
    Cached for 30 minutes so repeat discovery calls are instant.
    """
    if not tickers:
        return []
    key = ",".join(sorted(tickers))
    hit = _SCREEN_CACHE.get(key)
    if hit and _time.time() - hit[0] < _SCREEN_TTL:
        return hit[1]
    data = yf.download(
        tickers, period="1y", interval="1d", auto_adjust=True,
        group_by="ticker", threads=True, progress=False,
    )
    rows: list[ScreenRow] = []
    for t in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                closes = data[t]["Close"].dropna().tolist() if t in data.columns.levels[0] else []
            else:  # single ticker case
                closes = data["Close"].dropna().tolist()
        except Exception:  # noqa: BLE001
            closes = []
        if len(closes) < 30:
            continue
        row = ScreenRow(ticker=t, price=float(closes[-1]))

        def ret(days: int) -> float:
            return (closes[-1] - closes[-days]) / closes[-days] * 100.0 if len(closes) > days else 0.0

        row.ret_1m_pct = ret(21)
        row.ret_3m_pct = ret(63)
        row.ret_6m_pct = ret(126)
        row.above_50d = closes[-1] > sum(closes[-50:]) / min(50, len(closes))
        row.above_200d = closes[-1] > sum(closes[-200:]) / min(200, len(closes))
        row.rsi_14 = _rsi(closes)
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            row.vol_annualized_pct = math.sqrt(var) * math.sqrt(252) * 100.0
        rows.append(row)
    _SCREEN_CACHE[key] = (_time.time(), rows)
    return rows


def get_signals(ticker: str) -> TrendSignals:
    ticker = clean_ticker(ticker)
    sig = TrendSignals(ticker=ticker)
    if not ticker:
        sig.notes.append("empty symbol")
        return sig
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty:
            sig.notes.append("no price history")
            return sig
        closes = list(hist["Close"].dropna())
        sig.price = float(closes[-1])

        def ret(days: int) -> float:
            if len(closes) > days:
                return (closes[-1] - closes[-days]) / closes[-days] * 100.0
            return 0.0

        sig.ret_1m_pct = ret(21)
        sig.ret_3m_pct = ret(63)
        sig.ret_6m_pct = ret(126)
        ma50 = sum(closes[-50:]) / min(50, len(closes))
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        sig.above_50d = sig.price > ma50
        sig.above_200d = sig.price > ma200
        sig.rsi_14 = _rsi(closes)

        # annualized volatility from daily log returns
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            sig.vol_annualized_pct = math.sqrt(var) * math.sqrt(252) * 100.0

        info = {}
        try:
            info = t.info
        except Exception:
            pass
        sig.name = info.get("shortName", "") or ""
        sig.sector = info.get("sector", "") or ""
        sig.market_cap = float(info.get("marketCap") or 0.0)
        sig.pe = float(info.get("trailingPE") or 0.0)
        sig.beta = float(info.get("beta") or 0.0)
        # yfinance >=0.2.50 returns dividendYield already as a percent (e.g. 0.41
        # for 0.41%). Older builds returned a fraction (0.0041). Only scale up the
        # clearly-fractional case so both formats land in percent terms.
        dy = float(info.get("dividendYield") or 0.0)
        sig.dividend_yield = dy * 100.0 if 0 < dy < 0.05 else dy
    except Exception as e:  # noqa: BLE001
        sig.notes.append(f"signal error: {e}")
    return sig
