"""The Twin — an autonomous paper fund cloned once from the real account.

At inception it copies the real book exactly (same cash, same positions, same value), then runs
itself: it researches and queues moves, and fills them at live prices during market hours. Fixed
capital — to buy anything it must sell something. The user races their real account against it.

This module is the books + execution (inception, mark-to-market, fills, the You-vs-Twin compare).
The decision-making — what to trade and why — is a separate brain that queues moves here.
"""
from __future__ import annotations

from ..data import market_clock
from ..data.prices import get_quote, get_quotes
from ..db import repository as db_repo
from ..portfolio import get_portfolio


def state() -> dict | None:
    """The fund row (status/cash/inception), or None if it's never been started."""
    return db_repo.load_twin_fund()


def is_running() -> bool:
    f = state()
    return bool(f and f.get("status") == "running")


def inception(mandate_statement: str = "", real_pf=None) -> dict | None:
    """Clone the real account into the fund — ONCE. Same cash + positions at today's prices, so
    fund value == real value at this instant; from here they diverge. No-op (returns current
    state) if already running, so a restart never re-clones and wipes the track record."""
    if is_running():
        return state()
    pf = real_pf if real_pf is not None else get_portfolio()
    db_repo.reset_twin()   # clean slate in case of a prior paused/aborted attempt
    cash = float(pf.cash or 0.0)
    inception_value = float(pf.total_value or 0.0)
    for h in pf.holdings:
        price = float(h.current_price or 0.0)
        if h.quantity and price > 0:
            db_repo.upsert_twin_position(h.ticker, shares=float(h.quantity), avg_cost=price,
                                         thesis="Inherited from your real book at inception.",
                                         horizon="", exit_rule="")
    db_repo.save_twin_fund(status="running", inception_value=inception_value, cash=cash,
                           mandate_statement=mandate_statement)
    db_repo.add_twin_equity_point(value=inception_value, cash=cash,
                                  positions_value=inception_value - cash)
    return state()


def _quote_map(tickers: list[str], refresh: bool = False) -> dict:
    out: dict[str, float] = {}
    uniq = list(dict.fromkeys(t.upper() for t in tickers if t))
    if not uniq:
        return out
    try:
        for t, q in get_quotes(uniq, refresh=refresh).items():
            out[t.upper()] = float(getattr(q, "price", 0.0) or 0.0)
    except Exception:  # noqa: BLE001 — fall through to per-name
        pass
    for t in uniq:
        if out.get(t, 0.0) <= 0:
            try:
                out[t] = float(get_quote(t, refresh=refresh).price or 0.0)
            except Exception:  # noqa: BLE001
                out[t] = 0.0
    return out


def value(refresh: bool = False) -> dict:
    """Mark the fund to market. Returns cash, positions (with live price/value/return), and the
    total. Falls back to avg_cost when a quote is missing so the total never silently drops."""
    f = state()
    if not f:
        return {"started": False, "value": 0.0, "cash": 0.0, "positions_value": 0.0, "positions": []}
    cash = float(f.get("cash") or 0.0)
    poss = db_repo.twin_positions()
    qmap = _quote_map([p["ticker"] for p in poss], refresh=refresh)
    out_positions, pv = [], 0.0
    for p in poss:
        price = qmap.get(p["ticker"], 0.0) or p["avg_cost"]
        mv = p["shares"] * price
        pv += mv
        ret = ((price - p["avg_cost"]) / p["avg_cost"] * 100.0) if p["avg_cost"] > 0 else 0.0
        out_positions.append({**p, "price": price, "market_value": mv, "return_pct": ret})
    out_positions.sort(key=lambda x: x["market_value"], reverse=True)
    return {"started": True, "value": cash + pv, "cash": cash, "positions_value": pv,
            "positions": out_positions, "inception_value": f.get("inception_value", 0.0)}


def snapshot_equity(refresh: bool = False) -> dict:
    """Mark to market and record an equity-curve point. Returns the value dict."""
    v = value(refresh=refresh)
    if v.get("started"):
        db_repo.add_twin_equity_point(value=v["value"], cash=v["cash"], positions_value=v["positions_value"])
    return v


def queue_trade(ticker: str, action: str, shares: float, reasoning: str = "", conviction: int = 0) -> int:
    """Queue an intended move (the decision cycle's output). Fills at the next open."""
    return db_repo.add_twin_trade(ticker.upper(), action.lower(), float(shares),
                                  reasoning=reasoning, conviction=conviction)


def execute_pending(force: bool = False, refresh: bool = True) -> list[dict]:
    """Fill queued orders at live prices — ONLY while the market is open (unless forced, e.g. a
    manual run or a test). Fixed capital: buys are clamped to cash on hand, sells to shares held.
    Updates positions, cash, and trade status; records a post-trade equity point. Returns fills."""
    if not is_running():
        return []
    if not force and not market_clock.is_market_open():
        return []   # off-hours: orders stay queued for the next open
    pending = db_repo.pending_twin_trades()
    if not pending:
        return []
    cash = float((state() or {}).get("cash") or 0.0)
    qmap = _quote_map([t["ticker"] for t in pending], refresh=refresh)
    fills: list[dict] = []
    for t in pending:
        tk, action, want = t["ticker"], t["action"], float(t["shares"] or 0.0)
        price = qmap.get(tk, 0.0)
        if price <= 0 or want <= 0:
            db_repo.fill_twin_trade(t["id"], price=price, value=0.0)   # un-fillable — mark resolved
            continue
        pos = db_repo.get_twin_position(tk)
        if action in ("buy", "add"):
            shares = min(want, cash / price)        # clamp to cash on hand
            if shares <= 1e-9:
                db_repo.fill_twin_trade(t["id"], price=price, value=0.0)   # no cash — nothing bought
                continue
            spend = shares * price
            cash -= spend
            if pos:
                new_sh = pos["shares"] + shares
                new_avg = (pos["shares"] * pos["avg_cost"] + spend) / new_sh if new_sh > 0 else price
                db_repo.upsert_twin_position(tk, shares=new_sh, avg_cost=new_avg)
            else:
                db_repo.upsert_twin_position(tk, shares=shares, avg_cost=price,
                                             thesis=(t.get("reasoning") or "")[:2000])
            db_repo.fill_twin_trade(t["id"], price=price, value=spend)
            fills.append({**t, "filled_shares": shares, "price": price, "value": spend})
        elif action in ("sell", "trim"):
            held = pos["shares"] if pos else 0.0
            shares = min(want, held)                # clamp to shares held (long-only)
            if shares <= 1e-9:
                db_repo.fill_twin_trade(t["id"], price=price, value=0.0)
                continue
            proceeds = shares * price
            cash += proceeds
            left = held - shares
            if left <= 1e-6:
                db_repo.delete_twin_position(tk)
            else:
                db_repo.upsert_twin_position(tk, shares=left, avg_cost=pos["avg_cost"])
            db_repo.fill_twin_trade(t["id"], price=price, value=proceeds)
            fills.append({**t, "filled_shares": shares, "price": price, "value": proceeds})
        else:
            db_repo.fill_twin_trade(t["id"], price=price, value=0.0)
    db_repo.update_twin_cash(cash)
    if fills:
        snapshot_equity(refresh=False)
    return fills


def compare(real_pf=None, refresh: bool = False) -> dict:
    """The race: You vs the Twin since inception. Both started equal at inception_value, so each
    side's return is off that same base (assumes no external deposits/withdrawals — noted in UI)."""
    f = state()
    if not f:
        return {"started": False}
    v = value(refresh=refresh)
    pf = real_pf if real_pf is not None else get_portfolio()
    v0 = float(f.get("inception_value") or 0.0)
    twin_now = v["value"]
    real_now = float(pf.total_value or 0.0)

    def ret(now: float) -> float:
        return ((now - v0) / v0 * 100.0) if v0 > 0 else 0.0

    return {
        "started": True,
        "inception_at": f.get("inception_at", ""),
        "inception_value": v0,
        "twin": {"value": twin_now, "cash": v["cash"], "return_pct": ret(twin_now),
                 "positions": v["positions"]},
        "real": {"value": real_now, "cash": float(pf.cash or 0.0), "return_pct": ret(real_now),
                 "holdings": [{"ticker": h.ticker, "shares": h.quantity, "market_value": h.market_value,
                               "weight": (h.market_value / real_now * 100.0) if real_now > 0 else 0.0}
                              for h in pf.holdings]},
        "edge_pct": ret(twin_now) - ret(real_now),   # how much the Twin is beating you by
        "equity_curve": db_repo.twin_equity_curve(),
        "trades": db_repo.recent_twin_trades(40),
    }
