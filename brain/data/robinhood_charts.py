from __future__ import annotations

from datetime import datetime, timedelta, timezone

import robin_stocks.robinhood as rh

from .. import config
from ..models import ChartPoint, StockChart
from ..portfolio import robinhood as rh_portfolio

_CACHE: dict[str, tuple[float, StockChart]] = {}


def _ttl(span: str) -> int:
    return {
        "1d": 20,
        "1w": 45,
        "1m": 120,
        "3m": 300,
        "6m": 600,
        "1y": 900,
    }.get(span, 300)


def _span_args(span: str) -> tuple[str, str, str]:
    span = span if span in {"1d", "1w", "1m", "3m", "6m", "1y"} else "3m"
    return {
        "1d": ("5minute", "day", "extended"),
        "1w": ("10minute", "week", "regular"),
        "1m": ("hour", "month", "regular"),
        "3m": ("day", "3month", "regular"),
        "6m": ("day", "year", "regular"),
        "1y": ("week", "year", "regular"),
    }[span]


def _clean_price(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _points(rows: list[dict], price_keys: tuple[str, ...]) -> list[ChartPoint]:
    points: list[ChartPoint] = []
    for row in rows or []:
        at = row.get("begins_at") or row.get("start_time") or row.get("date") or ""
        price = 0.0
        for key in price_keys:
            price = _clean_price(row.get(key))
            if price > 0:
                break
        if at and price > 0:
            points.append(ChartPoint(at=at, close=price))
    return points


def _chart(ticker: str, span: str, points: list[ChartPoint], source: str) -> StockChart:
    if span == "6m":
        cutoff = datetime.now(timezone.utc) - timedelta(days=186)
        filtered = []
        for point in points:
            try:
                at = datetime.fromisoformat(point.at.replace("Z", "+00:00"))
            except ValueError:
                at = None
            if at is None or at >= cutoff:
                filtered.append(point)
        points = filtered or points
    latest = points[-1].close if points else 0.0
    first = points[0].close if points else 0.0
    ret = ((latest - first) / first * 100.0) if first else 0.0
    return StockChart(ticker=ticker, span=span, points=points, latest=latest, return_pct=ret, source=source)


def _fresh(key: str, span: str) -> StockChart | None:
    import time

    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _ttl(span):
        return hit[1]
    return None


def _store(key: str, chart: StockChart) -> StockChart:
    import time

    _CACHE[key] = (time.time(), chart)
    return chart


def get_stock_chart(ticker: str, span: str = "3m", refresh: bool = False) -> StockChart:
    if config.PORTFOLIO_SOURCE != "robinhood":
        return StockChart(ticker=ticker, span=span)  # type: ignore[arg-type]
    key = f"stock:{ticker.upper()}:{span}"
    cached = None if refresh else _fresh(key, span)
    if cached:
        return cached
    rh_portfolio._login()
    interval, rh_span, bounds = _span_args(span)
    rows = rh.stocks.get_stock_historicals(ticker, interval=interval, span=rh_span, bounds=bounds)
    if not isinstance(rows, list) or rows == [None]:
        rows = []
    points = _points(rows, ("close_price", "adjusted_close_equity", "close_equity"))
    return _store(key, _chart(ticker.upper(), span, points, "Robinhood historicals"))


def get_portfolio_chart(span: str = "3m", refresh: bool = False) -> StockChart:
    if config.PORTFOLIO_SOURCE != "robinhood":
        return StockChart(ticker="PORTFOLIO", span=span)  # type: ignore[arg-type]
    key = f"portfolio:{span}"
    cached = None if refresh else _fresh(key, span)
    if cached:
        return cached
    rh_portfolio._login()
    interval, rh_span, bounds = _span_args(span)
    rows = rh.account.get_historical_portfolio(interval=interval, span=rh_span, bounds=bounds)
    if not isinstance(rows, list) or rows == [None]:
        rows = []
    points = _points(
        rows,
        (
            "adjusted_close_equity",
            "close_equity",
            "equity",
            "market_value",
            "close_price",
        ),
    )
    return _store(key, _chart("PORTFOLIO", span, points, "Robinhood portfolio historicals"))
