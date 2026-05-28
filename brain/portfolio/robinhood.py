"""READ-ONLY Robinhood source via robin_stocks.

This logs into your *real* account and reads positions only. It NEVER calls
any order-placement function — there are none imported here. This path is
against Robinhood's ToS and stores a session token locally; use it knowingly.
If you'd rather not, set PORTFOLIO_SOURCE=manual.
"""
from __future__ import annotations

import robin_stocks.robinhood as rh

from .. import config
from ..data.prices import clean_ticker, get_quote
from ..models import Holding, Portfolio

_logged_in = False


def _login() -> None:
    global _logged_in
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


def fetch_portfolio() -> Portfolio:
    """Read holdings + buying power. Read-only — no orders, ever."""
    _login()
    positions = rh.account.build_holdings()  # {ticker: {quantity, average_buy_price, ...}}
    holdings = []
    for ticker, data in positions.items():
        qty = float(data.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        sym = clean_ticker(ticker)
        q = get_quote(sym)
        # fall back to RH's own price when yfinance can't quote the symbol
        rh_price = float(data.get("price", 0) or 0)
        holdings.append(
            Holding(
                ticker=sym,
                quantity=qty,
                avg_cost=float(data.get("average_buy_price", 0) or 0),
                current_price=q.price or rh_price,
            )
        )
    cash = 0.0
    try:
        profile = rh.profiles.load_account_profile()
        cash = float(profile.get("buying_power", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return Portfolio(holdings=holdings, cash=cash)
