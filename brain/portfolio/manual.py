"""Manual portfolio source — a local JSON file you edit (or the dashboard
edits for you). Zero credentials, zero ToS concerns. Prices are refreshed
live from yfinance on every read."""
from __future__ import annotations

import json

from .. import config
from ..data.prices import get_quote
from ..models import Holding, Portfolio


def load_portfolio() -> Portfolio:
    if not config.HOLDINGS_CACHE.exists():
        return Portfolio()
    raw = json.loads(config.HOLDINGS_CACHE.read_text())
    holdings = []
    for h in raw.get("holdings", []):
        q = get_quote(h["ticker"])
        holdings.append(
            Holding(
                ticker=h["ticker"].upper(),
                quantity=float(h.get("quantity", 0)),
                avg_cost=float(h.get("avg_cost", 0)),
                current_price=q.price,
            )
        )
    return Portfolio(holdings=holdings, cash=float(raw.get("cash", 0)))


def save_portfolio(holdings: list[dict], cash: float = 0.0) -> Portfolio:
    config.HOLDINGS_CACHE.write_text(
        json.dumps({"holdings": holdings, "cash": cash}, indent=2)
    )
    return load_portfolio()
