"""Portfolio sources. The brain reads your real holdings read-only and never
trades. `get_portfolio()` returns the configured source's holdings, priced.
"""
from __future__ import annotations

import json
import time

from .. import config
from ..db import repository as db_repo
from ..models import Portfolio
from . import manual

_PORTFOLIO_CACHE: tuple[float, Portfolio] | None = None


def _load_snapshot() -> Portfolio | None:
    try:
        if not config.PORTFOLIO_SNAPSHOT_PATH.exists():
            return None
        raw = json.loads(config.PORTFOLIO_SNAPSHOT_PATH.read_text())
        return Portfolio.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None


def _save_snapshot(pf: Portfolio) -> None:
    try:
        config.PORTFOLIO_SNAPSHOT_PATH.write_text(pf.model_dump_json(indent=2))
    except Exception:  # noqa: BLE001
        pass


def get_portfolio(refresh: bool = False) -> Portfolio:
    global _PORTFOLIO_CACHE
    if (
        _PORTFOLIO_CACHE
        and not refresh
        and time.time() - _PORTFOLIO_CACHE[0] < config.PORTFOLIO_TTL_SECONDS
    ):
        return _PORTFOLIO_CACHE[1]
    if not refresh:
        db_snap = db_repo.latest_portfolio_snapshot(config.PORTFOLIO_SOURCE)
        if db_snap:
            _PORTFOLIO_CACHE = (time.time(), db_snap)
            return db_snap
        snap = _load_snapshot()
        if snap:
            _PORTFOLIO_CACHE = (time.time(), snap)
            return snap
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
    if pf.sync_ok and pf.holdings:
        _save_snapshot(pf)
        db_repo.save_portfolio_snapshot(pf)
    return pf


def clear_portfolio_cache() -> None:
    global _PORTFOLIO_CACHE
    _PORTFOLIO_CACHE = None
