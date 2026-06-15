"""The Twin — an autonomous paper fund cloned once from the real account.

At inception it copies the real book exactly (same cash, same positions, same value), then runs
itself: it researches and queues moves, and fills them at live prices during market hours. Fixed
capital — to buy anything it must sell something. The user races their real account against it.

This module is the books + execution (inception, mark-to-market, fills, the You-vs-Twin compare).
The decision-making — what to trade and why — is a separate brain that queues moves here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import config, llm, research_state
from ..data import market_clock
from ..data.prices import (ScreenRow, get_chart, get_quote, get_quotes, get_signals,
                           get_signals_many, screen_universe, sector_etf)
from ..data.universe import screening_universe
from ..db import repository as db_repo
from ..models import TwinDecision, TwinThesisReview
from ..portfolio import get_portfolio

_VALID_TACTICS = {
    "rebalance",
    "risk_reduction",
    "momentum_continuation",
    "pullback_in_uptrend",
    "valuation_mean_reversion",
    "catalyst_trade",
    "long_term_compounder",
    "theme_exposure",
    "defensive_rotation",
    "liquidity_cleanup",
}
_MIN_ORDER_USD = 1.0
_HYGIENE_TACTICS = {"rebalance", "risk_reduction", "liquidity_cleanup", "defensive_rotation"}
_REVIEW_WINDOW_DAYS = {"1d": 1, "1w": 7, "1m": 30, "3m": 90, "6m": 180}
_WINDOW_SPAN = {"1d": "5d", "1w": "1m", "1m": "3m", "3m": "6m", "6m": "1y"}


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


def _mark_real_portfolio(pf, refresh: bool = False) -> dict:
    """Mark the real portfolio with the same quote source used for the Twin.

    Robinhood's reported equity can include a different after-hours/24h mark than our quote feed.
    The race needs apples-to-apples pricing, so the compare view values both books from the same
    quote map and falls back to Robinhood's holding price only when a quote is missing.
    """
    cash = float(pf.cash or 0.0)
    qmap = _quote_map([h.ticker for h in pf.holdings], refresh=refresh)
    holdings, positions_value = [], 0.0
    for h in pf.holdings:
        price = qmap.get(h.ticker.upper(), 0.0) or float(h.current_price or 0.0)
        mv = float(h.quantity or 0.0) * price
        positions_value += mv
        holdings.append({"ticker": h.ticker, "shares": h.quantity, "price": price, "market_value": mv})
    total = cash + positions_value
    for h in holdings:
        h["weight"] = (h["market_value"] / total * 100.0) if total > 0 else 0.0
    return {"value": total, "cash": cash, "positions_value": positions_value, "holdings": holdings}


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


def _pending_time(row: dict) -> datetime:
    try:
        dt = datetime.fromisoformat(row.get("decided_at", ""))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _current_pending_trades() -> list[dict]:
    """Return the pending batch that should remain live.

    If the app queued multiple off-hours decision cycles before the market opened, the older
    batches are stale. Keep the latest cluster of pending orders and cancel the rest so the next
    market open cannot execute duplicate rebalances.
    """
    pending = db_repo.pending_twin_trades()
    if len(pending) <= 1:
        return pending
    rows = sorted(pending, key=lambda r: (_pending_time(r), int(r.get("id") or 0)))
    batches: list[list[dict]] = []
    current: list[dict] = []
    last_ts: datetime | None = None
    for row in rows:
        ts = _pending_time(row)
        if current and last_ts and (ts - last_ts).total_seconds() > 120:
            batches.append(current)
            current = []
        current.append(row)
        last_ts = ts
    if current:
        batches.append(current)
    if len(batches) <= 1:
        return rows
    stale_ids = [int(r["id"]) for batch in batches[:-1] for r in batch if r.get("id")]
    db_repo.cancel_twin_trades(stale_ids)
    return batches[-1]


def execute_pending(force: bool = False, refresh: bool = True) -> list[dict]:
    """Fill queued orders at live prices — ONLY while the market is open (unless forced, e.g. a
    manual run or a test). Orders carry a DOLLAR intent (`value`) and are priced HERE, at fill, so
    a transient quote failure just leaves the order queued to retry next tick rather than losing it.
    Fixed capital: buys clamp to cash on hand, sells to shares held (long-only). Returns the fills."""
    if not is_running():
        return []
    if not force and not market_clock.is_market_open():
        return []   # off-hours: orders stay queued for the next open
    pending = _current_pending_trades()
    if not pending:
        return []
    cash = float((state() or {}).get("cash") or 0.0)
    qmap = _quote_map([t["ticker"] for t in pending], refresh=refresh)
    bench_price = get_quote("SPY", refresh=refresh).price or 0.0
    fills: list[dict] = []
    for t in pending:
        tk, action = t["ticker"], t["action"]
        usd = float(t.get("value") or 0.0)          # dollar intent (0 for a direct share order)
        price = qmap.get(tk, 0.0)
        if price <= 0:
            continue   # couldn't price it right now — leave queued, retry on a later tick
        want = (usd / price) if usd > 0 else float(t.get("shares") or 0.0)
        if want <= 1e-9:
            db_repo.fill_twin_trade(t["id"], price=price, value=0.0)   # nothing to do — resolve
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
            db_repo.fill_twin_trade(t["id"], price=price, value=spend, shares=shares,
                                    bench_entry_price=bench_price)
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
            db_repo.fill_twin_trade(t["id"], price=price, value=proceeds, shares=shares,
                                    bench_entry_price=bench_price)
            fills.append({**t, "filled_shares": shares, "price": price, "value": proceeds})
        else:
            db_repo.fill_twin_trade(t["id"], price=price, value=0.0)
    db_repo.update_twin_cash(cash)
    if fills:
        snapshot_equity(refresh=False)
        for f in fills:
            _schedule_fill_reviews(f, bench_price)
    return fills


# --------------------------------------------------------------------------- #
# Mature self-review (Stage 3) — multi-window, sector-relative, thesis-aware
# --------------------------------------------------------------------------- #
def _sector_anchor(ticker: str, refresh: bool = False) -> tuple[str, float]:
    """The stock's sector ETF symbol + its current price, for sector-relative alpha. Best-effort."""
    try:
        sym = sector_etf(get_signals(ticker, refresh=refresh).sector or "")
        if not sym:
            return ("", 0.0)
        return (sym, float(get_quote(sym, refresh=refresh).price or 0.0))
    except Exception:  # noqa: BLE001
        return ("", 0.0)


def _windows_for(horizon: str | None, tactic: str | None) -> list[tuple[str, int, bool]]:
    """The evaluation windows to schedule for a trade, by horizon/tactic. (window, days, judged) —
    judged=False windows are MONITORING ONLY (track price + watch for thesis-break), not scored."""
    h, tac = f"{horizon or ''}".lower(), f"{tactic or ''}".lower()
    if tac in _HYGIENE_TACTICS:
        plan = [("1w", True)]   # hygiene: one quick check, scored as 'executed' not alpha-judged
    elif tac == "long_term_compounder" or any(x in h for x in ("core", "long", "multi", "compound", "year")):
        plan = [("1w", False), ("1m", False), ("3m", True), ("6m", True)]
    elif "swing" in h:
        plan = [("1d", False), ("1w", False), ("1m", True)]
    elif tac == "catalyst_trade" or any(x in h for x in ("trade", "catalyst", "day")):
        plan = [("1d", False), ("1w", True)]
    else:
        plan = [("1w", False), ("1m", True), ("3m", True)]
    return [(w, _REVIEW_WINDOW_DAYS[w], judged) for (w, judged) in plan]


def _schedule_fill_reviews(fill: dict, bench_price: float) -> None:
    """At fill, schedule the trade's evaluation windows + capture the SPY and sector anchors."""
    try:
        if fill.get("action") not in ("buy", "add", "sell", "trim"):
            return
        sym, sec_price = _sector_anchor(fill["ticker"])
        db_repo.schedule_twin_reviews(
            trade_id=fill["id"], ticker=fill["ticker"], action=fill["action"],
            tactic=fill.get("tactic") or "", horizon=fill.get("horizon") or "",
            entry_price=float(fill.get("price") or 0.0), bench_entry=float(bench_price or 0.0),
            sector_symbol=sym, sector_entry=sec_price,
            windows=_windows_for(fill.get("horizon"), fill.get("tactic")))
    except Exception:  # noqa: BLE001
        return


def _drawdown_since(ticker: str, window: str, entry: float, refresh: bool = False) -> float:
    """Worst close-to-entry dip over the window (negative %). Best-effort; 0 if no chart."""
    if entry <= 0:
        return 0.0
    try:
        chart = get_chart(ticker, _WINDOW_SPAN.get(window, "3m"), refresh=refresh)
        closes = [p.close for p in (chart.points or []) if getattr(p, "close", 0)]
        if not closes:
            return 0.0
        return (min(closes) - entry) / entry * 100.0
    except Exception:  # noqa: BLE001
        return 0.0


def _thesis_prompt(d, thesis, exit_rule, stock_pct, spy_pct, sec_pct, drawdown, events) -> str:
    return f"""You are reviewing one of Autopilot's open paper trades to decide whether its THESIS still
holds — independent of short-term price. A drawdown alone is NOT a thesis break: if the market and the
stock's sector are down too and no invalidation fired, the thesis is still 'active'.

TRADE: {d['action'].upper()} {d['ticker']} · tactic {d.get('tactic')} · horizon {d.get('horizon')} · window {d['window']}
THESIS: {thesis}
EXIT RULE (what would invalidate it): {exit_rule or '(none recorded)'}

PRICE ACTION since entry: {d['ticker']} {stock_pct:+.1f}%, SPY {spy_pct:+.1f}%, sector ETF {sec_pct:+.1f}%, worst dip {drawdown:+.1f}%.

RECENT NEWS / EVENTS:
{events or '(nothing notable)'}

Decide the thesis state (active / weakening / broken / stronger), whether the drawdown is normal for
this horizon and sector, and one grounded sentence. Mark 'broken' ONLY if the exit rule clearly fired
or the original reason is now wrong — never just because the price is down."""


def _assess_thesis(d, ctx, stock_pct, spy_pct, sec_pct, drawdown) -> tuple[str, bool, str]:
    tac = (d.get("tactic") or "").lower()
    if d["window"] == "1d" or tac in _HYGIENE_TACTICS:
        return ("active", True, "Monitoring window — price tracked, no thesis call yet.")
    thesis = (ctx.get("thesis") or "").strip()
    exit_rule = (ctx.get("exit_rule") or "").strip()
    if not thesis or not config.TWIN_ENABLED:
        return ("active", abs(stock_pct) <= max(8.0, abs(sec_pct) + 4), "No stored thesis to test.")
    try:
        r = llm.parse(_thesis_prompt(d, thesis, exit_rule, stock_pct, spy_pct, sec_pct, drawdown,
                                     _events_block([d["ticker"]])),
                      TwinThesisReview, max_tokens=600, effort="low")
        return (r.state, r.drawdown_normal, r.reason)
    except Exception:  # noqa: BLE001
        return ("active", True, "Thesis check unavailable; treated as intact (no invalidation found).")


def _grace_verdict(d, tac: str, state: str, dd_normal: bool, sector_alpha: float) -> str:
    if tac in _HYGIENE_TACTICS:
        return "executed"
    if state == "broken":
        return "failed"
    if state == "weakening":
        return "weak"
    # thesis active/stronger: only SCORE at a judged window; earlier windows are intact-monitoring
    if d.get("judged"):
        return "worked" if sector_alpha > 0 else "lagged"
    return "intact"


def _review_note(d, ret, spy_alpha, sector_alpha, sec_sym, state, verdict, reason) -> str:
    sec = f", vs {sec_sym} {sector_alpha:+.1f}%" if sec_sym else ""
    return (f"{d['window']} {verdict}: {d['ticker']} {ret:+.1f}% (alpha vs SPY {spy_alpha:+.1f}%{sec}); "
            f"thesis {state} — {reason}")


def review_windows(refresh: bool = False) -> list[dict]:
    """Mature self-review: grade each DUE window against SPY AND the sector ETF, read the thesis
    state, and apply long-horizon grace so a normal drawdown on an intact thesis isn't called a
    failure. Writes per-window results; aggregates feed policy memory. Best-effort."""
    if not config.TWIN_ENABLED:
        return []
    due = db_repo.due_twin_reviews()
    if not due:
        return []
    names = {d["ticker"] for d in due} | {d["sector_symbol"] for d in due if d.get("sector_symbol")}
    qmap = _quote_map(list(names) + ["SPY"], refresh=refresh)
    spy_last = qmap.get("SPY", 0.0)
    ctx = {t["id"]: t for t in db_repo.recent_twin_trades(400)}
    done: list[dict] = []
    for d in due:
        entry, last = float(d.get("entry_price") or 0.0), qmap.get(d["ticker"], 0.0)
        if entry <= 0 or last <= 0:
            continue
        sign = -1.0 if d["action"] in ("sell", "trim") else 1.0
        stock_pct = (last - entry) / entry * 100.0
        ret = sign * stock_pct
        spy_entry = float(d.get("bench_entry") or 0.0)
        spy_pct = ((spy_last - spy_entry) / spy_entry * 100.0) if spy_entry > 0 and spy_last > 0 else 0.0
        spy_alpha = sign * (stock_pct - spy_pct) if spy_entry > 0 and spy_last > 0 else ret
        sec_sym, sec_entry = d.get("sector_symbol") or "", float(d.get("sector_entry") or 0.0)
        sec_last = qmap.get(sec_sym, 0.0)
        sec_pct = ((sec_last - sec_entry) / sec_entry * 100.0) if sec_entry > 0 and sec_last > 0 else 0.0
        sector_alpha = sign * (stock_pct - sec_pct) if sec_entry > 0 and sec_last > 0 else 0.0
        drawdown = _drawdown_since(d["ticker"], d["window"], entry, refresh=refresh)
        state, dd_normal, reason = _assess_thesis(d, ctx.get(d["trade_id"], {}), stock_pct, spy_pct, sec_pct, drawdown)
        verdict = _grace_verdict(d, (d.get("tactic") or "").lower(), state, dd_normal, sector_alpha)
        note = _review_note(d, ret, spy_alpha, sector_alpha, sec_sym, state, verdict, reason)
        db_repo.save_twin_review_window(
            d["id"], price=last, bench_last=spy_last, sector_last=sec_last, return_pct=ret,
            spy_alpha_pct=spy_alpha, sector_alpha_pct=sector_alpha, drawdown_pct=drawdown,
            thesis_state=state, verdict=verdict, note=note)
        done.append({**d, "return_pct": ret, "spy_alpha": spy_alpha, "sector_alpha": sector_alpha,
                     "thesis_state": state, "verdict": verdict, "note": note})
    return done


def position_health(ticker: str) -> dict:
    """Held-position health read from its latest review window — separates 'normal drawdown, hold'
    from 'thesis weakening/broken'. Empty dict if no review yet."""
    r = db_repo.latest_twin_review(ticker)
    if not r:
        return {}
    state, sec_a, ret = r.get("thesis_state") or "", r.get("sector_alpha_pct") or 0.0, r.get("return_pct") or 0.0
    if state == "broken":
        label = "thesis broken — review"
    elif state == "weakening":
        label = "thesis weakening — watch"
    elif state == "stronger":
        label = "thesis strengthening"
    elif ret < 0 and sec_a >= -2:
        label = "normal drawdown — hold"
    else:
        label = "thesis intact"
    return {"state": state, "label": label, "window": r.get("window"),
            "return_pct": ret, "sector_alpha_pct": sec_a,
            "sector_symbol": r.get("sector_symbol") or "", "note": r.get("note") or ""}


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
    real_mark = _mark_real_portfolio(pf, refresh=refresh)
    real_now = float(pf.total_value or 0.0)
    pending = _current_pending_trades()
    pending_buy = sum(float(t.get("value") or 0.0) for t in pending
                      if t.get("action") in ("buy", "add"))
    pending_sell = sum(float(t.get("value") or 0.0) for t in pending
                       if t.get("action") in ("sell", "trim"))
    phase = market_clock.session_phase()

    def ret(now: float) -> float:
        return ((now - v0) / v0 * 100.0) if v0 > 0 else 0.0

    return {
        "started": True,
        "inception_at": f.get("inception_at", ""),
        "inception_value": v0,
        "twin": {"value": twin_now, "cash": v["cash"], "return_pct": ret(twin_now),
                 "positions": v["positions"]},
        "real": {"value": real_now, "cash": float(pf.cash or 0.0), "return_pct": ret(real_now),
                 "positions_value": real_mark["positions_value"],
                 "holdings": [{"ticker": h.ticker, "shares": h.quantity, "market_value": h.market_value,
                               "weight": (h.market_value / real_now * 100.0) if real_now > 0 else 0.0}
                              for h in pf.holdings],
                 "marked_value": real_mark["value"],
                 "pricing_note": "Displayed balance uses broker reported equity; marked_value uses the Autopilot quote source."},
        "edge_pct": ret(twin_now) - ret(real_now),   # how much the Twin is beating you by
        "market": {"phase": phase, "is_open": phase == "open",
                   "next_open": market_clock.next_open().isoformat() if phase != "open" else ""},
        "pending": {"count": len(pending), "buy_value": pending_buy, "sell_value": pending_sell,
                    "gross_value": pending_buy + pending_sell,
                    "oldest_decided_at": pending[0].get("decided_at", "") if pending else ""},
        "valuation": {"starting_value": v0, "marked_value": twin_now,
                      "marked_at": market_clock.now_et().isoformat(),
                      "pending_applied": False},
        "equity_curve": db_repo.twin_equity_curve(),
        "real_equity_curve": db_repo.real_equity_curve(getattr(pf, "source", "") or "",
                                                       since_iso=f.get("inception_at")),
        "trades": db_repo.recent_twin_trades(200),
    }


# --------------------------------------------------------------------------- #
# The decision brain — Autopilot deciding its own trades (Stage 2)
# --------------------------------------------------------------------------- #
def _screen_score(r: ScreenRow) -> float:
    if r.price <= 0:
        return -1e9
    s = r.ret_3m_pct * 1.0 + r.ret_6m_pct * 0.5
    s += 15 if r.above_50d else -10
    s += 15 if r.above_200d else -10
    if 45 <= r.rsi_14 <= 72:
        s += 8
    elif r.rsi_14 > 82:
        s -= 18
    return s


def _candidate_universe(held: set[str]) -> dict:
    """Grounded names Autopilot may buy beyond its book.

    This is intentionally broader than the user's current catalog: memory/mission names plus a
    ranked broad-market screen. The LLM still gets a finite grounded list, not permission to invent
    tickers.
    """
    uni: dict[str, str] = {}
    try:
        st = research_state.load_state()
        for w in st.watchlist:
            uni.setdefault(w.ticker.upper(), f"watchlist: {(w.reason or 'tracked')[:80]}")
        for tk, th in st.theses.items():
            if th.status in ("active", "review"):
                uni.setdefault(tk.upper(), f"thesis ({th.status}): {th.thesis[:80]}")
    except Exception:  # noqa: BLE001
        pass
    try:
        for m in db_repo.all_missions(status="active"):
            for c in m.candidates:
                if c.label == "REJECT":
                    continue
                uni.setdefault(c.ticker.upper(), f"mission {m.title} / {c.label}: {c.reason[:90]}")
    except Exception:  # noqa: BLE001
        pass
    try:
        for e in db_repo.recent_events(limit=80, within_hours=96):
            tk = (e.get("ticker") or "").upper()
            if tk and tk not in held:
                uni.setdefault(tk, f"recent {e.get('event_type') or 'event'}: {(e.get('title') or '')[:90]}")
    except Exception:  # noqa: BLE001
        pass
    try:
        exclude = list(held | set(uni))
        rows = screen_universe(screening_universe(exclude=exclude))
        rows.sort(key=_screen_score, reverse=True)
        for r in rows[:40]:
            uni.setdefault(
                r.ticker.upper(),
                f"broad screen: 3m {r.ret_3m_pct:+.0f}%, 6m {r.ret_6m_pct:+.0f}%, "
                f"RSI {r.rsi_14:.0f}, vol {r.vol_annualized_pct:.0f}%",
            )
    except Exception:  # noqa: BLE001
        pass
    for t in held:
        uni.pop(t, None)   # held names live in the book block, not the candidate list
    return dict(list(uni.items())[:60])


def _sig_line(t: str, s) -> str:
    bits = [f"${s.price:.2f}"]
    if getattr(s, "ret_3m_pct", None) is not None:
        bits.append(f"3m {s.ret_3m_pct:+.0f}%")
    bits.append(f"{'>' if getattr(s, 'above_200d', False) else '<'}200d")
    if getattr(s, "rsi_14", 0):
        bits.append(f"RSI {s.rsi_14:.0f}")
    if getattr(s, "pe", 0) and s.pe > 0:
        bits.append(f"P/E {s.pe:.0f}")
    return f"- {t}: " + ", ".join(bits)


def _signals_block(names: list[str]) -> str:
    if not names:
        return "(no names)"
    try:
        sigs = get_signals_many(names)
    except Exception:  # noqa: BLE001
        return "(signals unavailable this cycle)"
    out = [_sig_line(t, sigs[t]) for t in names if sigs.get(t) and getattr(sigs[t], "price", 0)]
    return "\n".join(out) or "(signals unavailable this cycle)"


def _events_block(names: list[str]) -> str:
    nset = {n.upper() for n in names}
    try:
        evs = db_repo.recent_events(limit=50, within_hours=72)
    except Exception:  # noqa: BLE001
        return ""
    lines = [f"- {e['ticker']}: {e['title']}" for e in evs
             if e.get("ticker", "").upper() in nset and e.get("title")]
    return "\n".join(dict.fromkeys(lines))[:1400]


def review_due_trades(refresh: bool = False) -> list[dict]:
    """Stage 3 self-review: score filled Twin trades after their review window matures.

    This is deterministic policy learning, not an LLM judgement. It records whether each tactic
    produced benchmark-relative alpha at the declared horizon, then future decisions read the
    aggregate policy memory.
    """
    due = db_repo.due_twin_review_trades()
    if not due:
        return []
    qmap = _quote_map([t["ticker"] for t in due] + ["SPY"], refresh=refresh)
    bench_last = qmap.get("SPY", 0.0)
    reviewed = []
    for t in due:
        entry = float(t.get("price") or 0.0)
        last = qmap.get(t["ticker"], 0.0)
        if entry <= 0 or last <= 0:
            continue
        sign = -1.0 if t.get("action") in ("sell", "trim") else 1.0
        stock_pct = (last - entry) / entry * 100.0
        bench_entry = float(t.get("bench_entry_price") or 0.0)
        bench_pct = ((bench_last - bench_entry) / bench_entry * 100.0) if bench_entry > 0 and bench_last > 0 else 0.0
        ret = sign * stock_pct
        alpha = sign * (stock_pct - bench_pct) if bench_entry > 0 and bench_last > 0 else ret
        verdict = "worked" if alpha > 0 else "lagged"
        tactic = t.get("tactic") or t.get("action") or "trade"
        note = (f"{tactic} {verdict}: {t['ticker']} returned {ret:+.1f}% "
                f"vs SPY-adjusted alpha {alpha:+.1f}% after {t.get('review_after_days') or 7}d.")
        db_repo.save_twin_review(t["id"], last_price=last, bench_last_price=bench_last,
                                 return_pct=ret, alpha_pct=alpha, note=note)
        reviewed.append({**t, "last_price": last, "return_pct": ret, "alpha_pct": alpha, "note": note})
    return reviewed


def _policy_memory() -> str:
    stats = _policy_stats()
    if not stats:
        return "No reviewed Autopilot trades yet. Be conservative, explicit, and gather evidence."
    lines = []
    for tactic, st in sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:8]:
        sec = st.get("avg_sector_alpha")
        br = st.get("break_rate")
        extra = (f", sector alpha {sec:+.1f}%" if sec is not None else "")
        extra += (f", thesis-break {br:.0f}%" if br is not None else "")
        ok = st["avg_alpha"] > 1 and st["win_rate"] >= 50 and (br is None or br < 34)
        instruction = "lean in modestly" if ok else "size down or demand better evidence"
        lines.append(f"- {tactic}: {st['count']} reviewed, avg alpha {st['avg_alpha']:+.1f}%{extra}, "
                     f"win rate {st['win_rate']:.0f}% — {instruction}.")
    return "\n".join(lines)


def _policy_stats() -> dict[str, dict]:
    """Per-tactic learned stats. Prefers the mature multi-window reviews (sector-relative alpha +
    thesis-break rate); falls back to the legacy single-window trade reviews when none exist yet."""
    wins = db_repo.twin_window_policy()
    if wins:
        return {w["tactic"]: {
            "count": w["count"],
            "avg_alpha": w["avg_sector_alpha"] if w["avg_sector_alpha"] else w["avg_spy_alpha"],
            "avg_spy_alpha": w["avg_spy_alpha"], "avg_sector_alpha": w["avg_sector_alpha"],
            "win_rate": w["win_rate"], "break_rate": w["break_rate"]} for w in wins}
    rows = [r for r in db_repo.recent_twin_trades(500)
            if r.get("status") == "filled" and r.get("review_status") == "reviewed"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("tactic") or r.get("action") or "trade", []).append(r)
    out: dict[str, dict] = {}
    for tactic, rs in groups.items():
        alphas = [float(r.get("review_alpha_pct") or 0.0) for r in rs]
        if not alphas:
            continue
        out[tactic] = {"count": len(alphas), "avg_alpha": sum(alphas) / len(alphas),
                       "win_rate": sum(1 for a in alphas if a > 0) / len(alphas) * 100.0}
    return out


def _move_key(m, idx: int) -> str:
    return f"{idx}:{m.ticker.upper()}:{m.action}"


def _default_review_days(m) -> int:
    text = f"{m.horizon} {m.tactic}".lower()
    if any(x in text for x in ("core", "multi-year", "long_term", "compounder", "12 months")):
        return 90
    if any(x in text for x in ("3-12", "months")):
        return 45
    if "swing" in text or "1-4" in text:
        return 14
    if "trade" in text or "catalyst" in text:
        return 5
    return max(1, int(m.review_after_days or 7))


def _critic(decision: TwinDecision, v: dict, profile, universe: dict) -> tuple[TwinDecision, dict[str, str], list[tuple]]:
    """Deterministic risk/capital governor between LLM proposal and queued orders."""
    held = {p["ticker"].upper(): p for p in v.get("positions", [])}
    allowed = set(universe) | set(held)
    policy = _policy_stats()
    accepted: list[tuple[int, object]] = []
    rejected: list[tuple[object, str]] = []
    notes: dict[str, str] = {}

    for idx, m in enumerate(decision.moves):
        m.ticker = m.ticker.upper().strip()
        m.action = m.action.lower()
        m.usd = max(0.0, float(m.usd or 0.0))
        m.tactic = (m.tactic or "unspecified").strip().lower().replace(" ", "_")
        if m.tactic not in _VALID_TACTICS:
            notes[_move_key(m, idx)] = f"Critic normalized unrecognized tactic '{m.tactic}' to theme_exposure."
            m.tactic = "theme_exposure"
        if m.review_after_days == 7:
            m.review_after_days = _default_review_days(m)

        if m.action == "hold":
            accepted.append((idx, m))
            continue
        if m.usd < _MIN_ORDER_USD:
            rejected.append((m, "Rejected by critic: dollar size was too small to queue."))
            continue
        if m.action in ("buy", "add") and m.ticker not in allowed:
            rejected.append((m, "Rejected by critic: ticker was not in the grounded candidate universe."))
            continue
        if m.action in ("sell", "trim") and m.ticker not in held:
            rejected.append((m, "Rejected by critic: Autopilot does not own shares to sell/trim."))
            continue
        if m.action in ("sell", "trim"):
            max_sell = float(held[m.ticker].get("market_value") or 0.0)
            if m.usd > max_sell:
                notes[_move_key(m, idx)] = f"Critic capped sell/trim from ${m.usd:,.0f} to owned value ${max_sell:,.0f}."
                m.usd = max_sell
        if m.action in ("buy", "add"):
            # No single-position cap — Autopilot decides its own concentration on purpose (that's the
            # whole point). The only hard limit on a buy is fixed capital, enforced below. Policy
            # memory still nudges sizing on tactics with a weak reviewed track record.
            st = policy.get(m.tactic)
            if st and st["count"] >= 3 and st["avg_alpha"] < -6 and st["win_rate"] < 34:
                rejected.append((m, f"Rejected by critic: tactic {m.tactic} has weak reviewed results ({st['avg_alpha']:+.1f}% avg alpha)."))
                continue
            if st and st["count"] >= 2 and st["avg_alpha"] < -2:
                old = m.usd
                m.usd *= 0.5
                prior = notes.get(_move_key(m, idx), "")
                notes[_move_key(m, idx)] = (prior + " " if prior else "") + (
                    f"Critic halved size from ${old:,.0f} to ${m.usd:,.0f}; tactic {m.tactic} has weak reviewed results."
                )
        if m.usd < _MIN_ORDER_USD and m.action != "hold":
            rejected.append((m, "Rejected by critic: adjusted size fell below minimum order."))
            continue
        accepted.append((idx, m))

    sell_cash = sum(m.usd for _, m in accepted if m.action in ("sell", "trim"))
    available = float(v.get("cash") or 0.0) + sell_cash
    buys = [(idx, m) for idx, m in accepted if m.action in ("buy", "add")]
    total_buy = sum(m.usd for _, m in buys)
    if total_buy > available > 0:
        scale = available / total_buy
        for idx, m in buys:
            old = m.usd
            m.usd *= scale
            prior = notes.get(_move_key(m, idx), "")
            notes[_move_key(m, idx)] = (prior + " " if prior else "") + (
                f"Critic scaled buy/add from ${old:,.0f} to ${m.usd:,.0f} to respect fixed capital."
            )
    elif total_buy > available:
        for _, m in buys:
            rejected.append((m, "Rejected by critic: no cash or sale proceeds available to fund buy/add."))
        accepted = [(idx, m) for idx, m in accepted if m.action not in ("buy", "add")]

    final_moves = [m for _, m in accepted if m.action == "hold" or m.usd >= _MIN_ORDER_USD]
    summary = decision.summary
    if rejected or notes:
        summary = (summary + f" Critic adjusted {len(notes)} move(s), rejected {len(rejected)}.").strip()
    return TwinDecision(summary=summary, moves=final_moves), notes, rejected


def _book_block(v: dict) -> str:
    lines = [f"Total ${v['value']:,.0f} · cash ${v['cash']:,.0f} · {len(v['positions'])} positions"]
    for p in v["positions"]:
        intent = []
        if p.get("thesis"):
            intent.append(f"thesis: {p['thesis'][:120]}")
        if p.get("horizon"):
            intent.append(f"horizon: {p['horizon']}")
        if p.get("exit_rule"):
            intent.append(f"exit: {p['exit_rule'][:80]}")
        lines.append(f"- {p['ticker']}: {p['shares']:.2f} sh, ${p['market_value']:,.0f} "
                     f"({p['return_pct']:+.0f}% since you bought){' · ' + '; '.join(intent) if intent else ''}")
    return "\n".join(lines)


def _decide_prompt(v: dict, mandate_block: str, profile, universe: dict,
                   sig_block: str, events_block: str, policy_block: str) -> str:
    cand = "\n".join(f"- {t}: {ctx}" for t, ctx in universe.items()) or "(none beyond your book)"
    return f"""You are AUTOPILOT — an autonomous fund manager running a real paper portfolio. You decide the
trades and nobody approves them. Your job: pursue the mandate below and beat the user's real account.
FIXED CAPITAL — to buy you must use cash already in the Twin or sell/trim something first. You may
not invent deposits, assume extra buying power, or spend money that is not shown. Long-only (no shorts).

{mandate_block or 'No mandate is set — manage prudently toward steady long-term growth and capital preservation.'}

INVESTOR PROFILE (secondary to the mandate): {profile.describe()}

YOUR BOOK:
{_book_block(v)}

CANDIDATES you may buy (grounded from memory, missions, and broad market screening — choose from
this list only; never invented tickers):
{cand}

QUANTITATIVE SIGNALS:
{sig_block}

RECENT CATALYSTS / EVENTS:
{events_block or '(nothing notable)'}

AUTOPILOT POLICY MEMORY (reviewed paper trades only):
{policy_block}

Decide your moves for this cycle. You set the pace — trade as much or as little as warranted, including
nothing at all. Size each move in DOLLARS. Sells/trims free up cash for buys; never spend more cash than
you have (sell first if you must). Take profit or cut losers per your own exit rules; don't churn for its
own sake. Position sizing and concentration are YOUR call — you may concentrate in a high-conviction
name beyond the profile's comfort cap if you judge it worth the risk; treat that cap as a preference,
not a hard limit. For every move, classify the tactic, horizon, review_after_days, updated thesis, and exit
rule. Ground every move in the signals, policy memory, and mandate — this is real performance you'll
be judged on."""


def _apply(decision: TwinDecision, critic_notes: dict[str, str] | None = None,
           rejected: list[tuple] | None = None) -> list[dict]:
    """Queue the decision's moves as DOLLAR orders (sells/trims first so they fund buys within the
    same fill pass) and refresh per-position intent on names already held. No pricing here — orders
    are priced at fill, so a transient quote failure never loses a decision. Returns queued moves."""
    order = {"sell": 0, "trim": 1, "add": 2, "buy": 3, "hold": 9}
    queued = []
    critic_notes = critic_notes or {}
    for m, note in (rejected or []):
        if (m.usd or 0) > 0:
            db_repo.add_twin_trade(m.ticker, m.action, 0.0, usd=float(m.usd),
                                   reasoning=m.reasoning or m.thesis, conviction=m.conviction,
                                   status="canceled", tactic=m.tactic, horizon=m.horizon,
                                   thesis=m.thesis, exit_rule=m.exit_rule,
                                   review_after_days=m.review_after_days, critic_note=note)
    for idx, m in sorted(enumerate(decision.moves), key=lambda x: order.get(x[1].action, 5)):
        db_repo.set_twin_intent(m.ticker, thesis=m.thesis or None, horizon=m.horizon or None,
                                exit_rule=m.exit_rule or None)
        if m.action == "hold" or (m.usd or 0) <= 0:
            continue
        db_repo.add_twin_trade(m.ticker, m.action, 0.0, usd=float(m.usd),
                               reasoning=m.reasoning or m.thesis, conviction=m.conviction,
                               tactic=m.tactic, horizon=m.horizon, thesis=m.thesis,
                               exit_rule=m.exit_rule, review_after_days=m.review_after_days,
                               critic_note=critic_notes.get(_move_key(m, idx), ""))
        queued.append({"ticker": m.ticker.upper(), "action": m.action, "usd": m.usd})
    return queued


def decide(profile) -> TwinDecision | None:
    """Autopilot's autonomous decision cycle: read the book + mandate + signals, decide moves, queue
    them (they fill at the next open via execute_pending), and persist the decision as an audit trail
    + cadence marker. Best-effort — a model hiccup means it simply holds this cycle."""
    if not config.TWIN_ENABLED or not is_running():
        return None
    review_due_trades(refresh=False)
    if _current_pending_trades():
        return None
    v = value(refresh=True)
    held = {p["ticker"] for p in v["positions"]}
    universe = _candidate_universe(held)
    names = (list(held) + [t for t in universe if t not in held])[:50]
    from .. import mandate as _mandate
    try:
        raw = llm.parse(
            _decide_prompt(v, _mandate.mandate_prompt(), profile, universe,
                           _signals_block(names), _events_block(names), _policy_memory()),
            TwinDecision, max_tokens=2400)
    except Exception:  # noqa: BLE001
        return None
    decision, critic_notes, rejected = _critic(raw, v, profile, universe)
    _apply(decision, critic_notes=critic_notes, rejected=rejected)
    try:
        db_repo.save_agent_run(
            query="Autopilot decision cycle", answer=decision.summary, kind="twin_decision",
            steps=[{"type": "twin_decision", "summary": decision.summary,
                    "original_moves": [m.model_dump() for m in raw.moves],
                    "moves": [m.model_dump() for m in decision.moves],
                    "critic_notes": critic_notes,
                    "rejected": [{"ticker": m.ticker, "action": m.action, "usd": m.usd, "reason": note}
                                 for m, note in rejected]}],
            tools_used="", model=llm.MODEL)
    except Exception:  # noqa: BLE001
        pass
    return decision
