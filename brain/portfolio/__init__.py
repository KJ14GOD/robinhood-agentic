"""Portfolio sources. The brain reads your real holdings read-only and never
trades. `get_portfolio()` returns the configured source's holdings, priced.
"""
from __future__ import annotations

from .. import config
from ..models import Portfolio
from . import manual


def get_portfolio() -> Portfolio:
    if config.PORTFOLIO_SOURCE == "robinhood":
        from . import robinhood  # imported lazily so robin_stocks isn't needed for manual
        return robinhood.fetch_portfolio()
    return manual.load_portfolio()
