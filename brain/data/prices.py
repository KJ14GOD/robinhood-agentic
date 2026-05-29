"""Prices, fundamentals, and trend signals via yfinance (free).

Everything quantitative the brain reasons over enters here. The LLM is never
asked to recall or predict prices — it only interprets these grounded numbers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from .. import config
from ..models import ChartPoint, Holding, StockChart

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


import time as _time

_QUOTE_CACHE: dict[str, tuple[float, Quote]] = {}
_SIGNAL_CACHE: dict[str, tuple[float, TrendSignals]] = {}
_SCREEN_CACHE: dict[str, tuple[float, list]] = {}
_CHART_CACHE: dict[str, tuple[float, StockChart]] = {}


def _fresh(hit: tuple[float, object] | None, ttl: int) -> bool:
    return bool(hit and _time.time() - hit[0] < ttl)


def clear_caches(tickers: list[str] | None = None, include_signals: bool = True) -> None:
    """Force the next read to hit the data providers.

    Use this when the user clicks refresh, when the dashboard background loop
    runs, or after Robinhood positions change.

    `include_signals=False` keeps the trend-signal and universe-screen caches
    warm. Those are *daily* indicators (200d MA, RSI, momentum), so the 2-minute
    refresh loop should not wipe them every cycle — doing so forces a full
    yfinance re-download per holding every 2 min for data that only moves once a
    day. Live quotes/charts are still cleared so dashboard prices stay current.
    """
    if tickers is None:
        _QUOTE_CACHE.clear()
        _CHART_CACHE.clear()
        if include_signals:
            _SIGNAL_CACHE.clear()
            _SCREEN_CACHE.clear()
        return
    cleaned = {clean_ticker(t) for t in tickers}
    for t in cleaned:
        _QUOTE_CACHE.pop(t, None)
        _SIGNAL_CACHE.pop(t, None)
        _CHART_CACHE.pop(t, None)


def get_quote(ticker: str, refresh: bool = False) -> Quote:
    ticker = clean_ticker(ticker)
    if not ticker:
        return Quote(ticker=ticker, ok=False, error="empty symbol")
    hit = _QUOTE_CACHE.get(ticker)
    if not refresh and _fresh(hit, config.QUOTE_TTL_SECONDS):
        return hit[1]
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
        q = Quote(ticker=ticker, price=price, name=name, ok=price > 0)
    except Exception as e:  # noqa: BLE001
        q = Quote(ticker=ticker, ok=False, error=str(e))
    _QUOTE_CACHE[ticker] = (_time.time(), q)
    return q


def get_quotes(tickers: list[str], refresh: bool = False,
               max_workers: int = 8) -> dict[str, Quote]:
    """Fetch many quotes concurrently while preserving the single-ticker cache."""
    cleaned = [clean_ticker(t) for t in tickers if clean_ticker(t)]
    if not cleaned:
        return {}
    out: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(cleaned))) as pool:
        futures = {pool.submit(get_quote, t, refresh): t for t in cleaned}
        for fut in as_completed(futures):
            q = fut.result()
            out[q.ticker] = q
    return out


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

def screen_universe(tickers: list[str], refresh: bool = False) -> list[ScreenRow]:
    """Download a year of closes for the whole list in ONE request and compute
    momentum/trend signals in memory. This is what makes a 500-name screen fast.
    Cached for 30 minutes so repeat discovery calls are instant.
    """
    if not tickers:
        return []
    key = ",".join(sorted(tickers))
    hit = _SCREEN_CACHE.get(key)
    if not refresh and _fresh(hit, config.SCREEN_TTL_SECONDS):
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


def get_signals(ticker: str, refresh: bool = False) -> TrendSignals:
    ticker = clean_ticker(ticker)
    sig = TrendSignals(ticker=ticker)
    if not ticker:
        sig.notes.append("empty symbol")
        return sig
    hit = _SIGNAL_CACHE.get(ticker)
    if not refresh and _fresh(hit, config.SIGNAL_TTL_SECONDS):
        return hit[1]
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
    _SIGNAL_CACHE[ticker] = (_time.time(), sig)
    return sig


def get_signals_many(tickers: list[str], refresh: bool = False,
                     max_workers: int = 8) -> dict[str, TrendSignals]:
    cleaned = [clean_ticker(t) for t in tickers if clean_ticker(t)]
    if not cleaned:
        return {}
    out: dict[str, TrendSignals] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(cleaned))) as pool:
        futures = {pool.submit(get_signals, t, refresh): t for t in cleaned}
        for fut in as_completed(futures):
            sig = fut.result()
            out[sig.ticker] = sig
    return out


def get_chart(ticker: str, span: str = "3m", refresh: bool = False) -> StockChart:
    ticker = clean_ticker(ticker)
    span = span if span in {"1d", "1w", "1m", "3m", "6m", "1y"} else "3m"
    if not ticker:
        return StockChart(ticker="", span=span)  # type: ignore[arg-type]
    cache_key = f"{ticker}:{span}"
    hit = _CHART_CACHE.get(cache_key)
    if not refresh and _fresh(hit, config.QUOTE_TTL_SECONDS):
        return hit[1]
    period, interval = {
        "1d": ("1d", "5m"),
        "1w": ("5d", "15m"),
        "1m": ("1mo", "1d"),
        "3m": ("3mo", "1d"),
        "6m": ("6mo", "1d"),
        "1y": ("1y", "1wk"),
    }[span]
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    except Exception:  # noqa: BLE001
        hist = pd.DataFrame()
    points: list[ChartPoint] = []
    if not hist.empty:
        closes = hist["Close"].dropna()
        for idx, close in closes.items():
            points.append(ChartPoint(at=idx.isoformat(), close=float(close)))
    latest = points[-1].close if points else 0.0
    first = points[0].close if points else 0.0
    ret = ((latest - first) / first * 100.0) if first else 0.0
    chart = StockChart(ticker=ticker, span=span, points=points, latest=latest, return_pct=ret)
    _CHART_CACHE[cache_key] = (_time.time(), chart)
    return chart


def get_portfolio_chart(
    holdings: list[Holding],
    cash: float = 0.0,
    span: str = "3m",
    refresh: bool = False,
    target_latest: float = 0.0,
) -> StockChart:
    """Reconstruct an approximate portfolio value chart from current quantities.

    This is not a broker statement history. It answers the product question the
    UI needs right now: "how would the current book have moved across this
    window?" A real DB-backed equity curve should replace this once snapshots
    are persisted.
    """
    span = span if span in {"1d", "1w", "1m", "3m", "6m", "1y"} else "3m"
    active = [h for h in holdings if h.quantity > 0]
    if not active:
        return StockChart(ticker="PORTFOLIO", span=span, source="portfolio holdings")

    period, interval = {
        "1d": ("1d", "5m"),
        "1w": ("5d", "15m"),
        "1m": ("1mo", "1d"),
        "3m": ("3mo", "1d"),
        "6m": ("6mo", "1d"),
        "1y": ("1y", "1wk"),
    }[span]
    key = (
        "PORTFOLIO:"
        + span
        + ":"
        + ",".join(f"{h.ticker}:{h.quantity}" for h in active)
        + f":{target_latest:.2f}"
    )
    hit = _CHART_CACHE.get(key)
    if not refresh and _fresh(hit, config.QUOTE_TTL_SECONDS):
        return hit[1]

    tickers = [h.ticker for h in active]
    qty = {h.ticker: h.quantity for h in active}
    frames = []
    try:
        hist = yf.download(
            tickers,
            period=period,
            interval=interval,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception:  # noqa: BLE001
        hist = pd.DataFrame()
    if not hist.empty:
        if isinstance(hist.columns, pd.MultiIndex):
            closes = hist["Close"] if "Close" in hist.columns.get_level_values(0) else pd.DataFrame()
            for ticker in tickers:
                if ticker in closes:
                    series = closes[ticker].dropna()
                    if not series.empty:
                        frames.append(series.rename(ticker) * qty[ticker])
        elif "Close" in hist:
            ticker = tickers[0]
            series = hist["Close"].dropna()
            if not series.empty:
                frames.append(series.rename(ticker) * qty[ticker])

    if not frames:
        chart = StockChart(ticker="PORTFOLIO", span=span, source="portfolio holdings")
        _CHART_CACHE[key] = (_time.time(), chart)
        return chart

    values = pd.concat(frames, axis=1).ffill().dropna(how="all").sum(axis=1) + cash
    latest_raw = float(values.iloc[-1]) if not values.empty else 0.0
    if target_latest > 0 and latest_raw > 0:
        values = values * (target_latest / latest_raw)
    points = [
        ChartPoint(at=idx.isoformat(), close=float(value))
        for idx, value in values.items()
        if pd.notna(value)
    ]
    latest = points[-1].close if points else 0.0
    first = points[0].close if points else 0.0
    ret = ((latest - first) / first * 100.0) if first else 0.0
    chart = StockChart(
        ticker="PORTFOLIO",
        span=span,
        points=points,
        latest=latest,
        return_pct=ret,
        source="current holdings reconstruction, anchored to broker equity",
    )
    _CHART_CACHE[key] = (_time.time(), chart)
    return chart
