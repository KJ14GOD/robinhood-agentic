"""Manual portfolio source — a local JSON file you edit (or the dashboard
edits for you). Zero credentials, zero ToS concerns. Prices are refreshed
live from yfinance on every read."""
from __future__ import annotations

import json

from .. import config
from ..data.prices import clean_ticker, get_quotes
from ..models import Holding, Portfolio


def load_portfolio(refresh: bool = False) -> Portfolio:
    if not config.HOLDINGS_CACHE.exists():
        return Portfolio(source="manual", sync_message="No manual holdings entered yet.")
    raw = json.loads(config.HOLDINGS_CACHE.read_text())
    holdings = []
    symbols = [clean_ticker(h["ticker"]) for h in raw.get("holdings", [])]
    quotes = get_quotes(symbols, refresh=refresh)
    for h in raw.get("holdings", []):
        ticker = clean_ticker(h["ticker"])
        q = quotes.get(ticker)
        holdings.append(
            Holding(
                ticker=ticker,
                quantity=float(h.get("quantity", 0)),
                avg_cost=float(h.get("avg_cost", 0)),
                current_price=q.price if q else 0.0,
            )
        )
    cash = float(raw.get("cash", 0))
    return Portfolio(holdings=holdings, cash=cash, buying_power=cash, source="manual")


def save_portfolio(holdings: list[dict], cash: float = 0.0) -> Portfolio:
    config.HOLDINGS_CACHE.write_text(
        json.dumps({"holdings": holdings, "cash": cash}, indent=2)
    )
    return load_portfolio(refresh=True)
