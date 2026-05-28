"""Portfolio sources. The brain reads your real holdings read-only and never
trades. `get_portfolio()` returns the configured source's holdings, priced.
"""
from __future__ import annotations

import time

from .. import config
from ..models import Portfolio
from . import manual

_PORTFOLIO_CACHE: tuple[float, Portfolio] | None = None


def get_portfolio(refresh: bool = False) -> Portfolio:
    global _PORTFOLIO_CACHE
    if (
        _PORTFOLIO_CACHE
        and not refresh
        and time.time() - _PORTFOLIO_CACHE[0] < config.PORTFOLIO_TTL_SECONDS
    ):
        return _PORTFOLIO_CACHE[1]
    try:
        if config.PORTFOLIO_SOURCE == "robinhood":
            from . import robinhood  # imported lazily so robin_stocks isn't needed for manual
            pf = robinhood.fetch_portfolio(refresh=refresh)
        else:
            pf = manual.load_portfolio(refresh=refresh)
    except Exception as e:  # noqa: BLE001
        pf = Portfolio(
            source=config.PORTFOLIO_SOURCE,
            sync_ok=False,
            sync_message=str(e),
        )
    _PORTFOLIO_CACHE = (time.time(), pf)
    return pf


def clear_portfolio_cache() -> None:
    global _PORTFOLIO_CACHE
    _PORTFOLIO_CACHE = None
