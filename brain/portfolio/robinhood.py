"""READ-ONLY Robinhood source via robin_stocks.

This logs into your *real* account and reads positions only. It NEVER calls
any order-placement function — there are none imported here. This path is
against Robinhood's ToS and stores a session token locally; use it knowingly.
If you'd rather not, set PORTFOLIO_SOURCE=manual.
"""
from __future__ import annotations

import threading

import robin_stocks.robinhood as rh
from robin_stocks.robinhood.helper import request_get
from robin_stocks.robinhood.urls import historicals_url

from .. import config
from ..data.prices import clean_ticker, get_quotes
from ..models import Holding, Portfolio

_logged_in = False
# Serialize login. The app calls _login() from many threads at once (chart requests,
# brain/refresh loops). Without this lock they all race past the `_logged_in` check while
# none has finished, and each starts a full Robinhood device-approval challenge — a
# thundering herd that hammers the push endpoint and trips a 429. The lock lets exactly
# one thread log in while the rest wait and reuse the session.
_login_lock = threading.Lock()


def _login() -> None:
    global _logged_in
    if _logged_in:
        return
    with _login_lock:
        # Re-check inside the lock: a thread that waited here may have logged in already.
        if _logged_in:
            return
        if not (config.RH_USERNAME and config.RH_PASSWORD):
            raise RuntimeError(
                "PORTFOLIO_SOURCE=robinhood but RH_USERNAME/RH_PASSWORD are not set in .env"
            )
        kwargs = {"username": config.RH_USERNAME, "password": config.RH_PASSWORD,
                  "store_session": True}
        if config.RH_MFA:
            import pyotp  # optional dep; only needed for automated TOTP
            kwargs["mfa_code"] = pyotp.TOTP(config.RH_MFA).now()
        rh.login(**kwargs)
        _logged_in = True


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _reported_equity(profile: dict) -> float:
    """Match Robinhood's displayed account value before reconstructing locally."""
    for key in (
        "extended_hours_equity",
        "extended_hours_portfolio_equity",
        "equity",
        "portfolio_equity",
        "market_value",
    ):
        value = _as_float(profile.get(key))
        if value > 0:
            return value
    return 0.0


def _historical_24h_prices(symbols: list[str]) -> dict[str, float]:
    """Best available unofficial approximation of Robinhood's 24-hour chart feed.

    Robinhood's web UI may use a newer ATS overnight feed that is not exposed by
    robin_stocks quote/profile helpers. The 24_7 historicals endpoint is closer
    than regular quotes when it returns data, but it can still lag the web UI.
    """
    if not symbols:
        return {}
    data = request_get(
        historicals_url(),
        "results",
        {
            "symbols": ",".join(symbols),
            "interval": "5minute",
            "span": "day",
            "bounds": "24_7",
        },
    )
    out: dict[str, float] = {}
    for item in data or []:
        symbol = item.get("symbol")
        points = item.get("historicals") or []
        if not symbol or not points:
            continue
        price = _as_float(points[-1].get("close_price"))
        if price > 0:
            out[clean_ticker(symbol)] = price
    return out


def fetch_portfolio(refresh: bool = False) -> Portfolio:
    """Read holdings + buying power. Read-only — no orders, ever."""
    _login()
    account_profile = {}
    portfolio_profile = {}
    try:
        account_profile = rh.profiles.load_account_profile() or {}
    except Exception:  # noqa: BLE001
        pass
    try:
        portfolio_profile = rh.profiles.load_portfolio_profile() or {}
    except Exception:  # noqa: BLE001
        pass

    positions = rh.account.build_holdings()  # {ticker: {quantity, average_buy_price, ...}}
    symbols = [
        clean_ticker(ticker)
        for ticker, data in positions.items()
        if float(data.get("quantity", 0) or 0) > 0
    ]
    quotes = get_quotes(symbols, refresh=refresh)
    historical_24h = _historical_24h_prices(symbols)
    holdings = []
    for ticker, data in positions.items():
        qty = float(data.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        sym = clean_ticker(ticker)
        q = quotes.get(sym)
        # Robinhood should be the source of truth for the Robinhood dashboard.
        # yfinance is only a fallback when RH cannot quote a symbol.
        rh_price = _as_float(data.get("price"))
        chart_price = historical_24h.get(sym, 0.0)
        holdings.append(
            Holding(
                ticker=sym,
                quantity=qty,
                avg_cost=_as_float(data.get("average_buy_price")),
                current_price=chart_price or rh_price or (q.price if q else 0.0),
            )
        )
    cash = _as_float(account_profile.get("portfolio_cash")) or _as_float(account_profile.get("cash"))
    buying_power = _as_float(account_profile.get("buying_power"))
    reported = _reported_equity(portfolio_profile) or sum(h.market_value for h in holdings) + cash
    return Portfolio(
        holdings=holdings,
        cash=cash,
        buying_power=buying_power,
        reported_equity=reported,
        pricing_source="Robinhood API 24_7 historicals / portfolio profile",
        pricing_warning=(
            "Robinhood web may show a different overnight value because its "
            "24 Hour Market ATS feed is not fully exposed by robin_stocks."
        ),
        source="robinhood",
    )
