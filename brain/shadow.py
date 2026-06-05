"""Shadow mode — the honesty layer (DB-backed).

Every recommendation the brain makes is logged here as a timestamped paper
trade, enriched with the decision label, conviction, source engine, the risk
mode and signals that justified it, and market + sector benchmark anchors
captured *at entry*. We mark them to market over time and compute a real track
record, so you can find out whether the brain is good *before* real money rides
on it.

Storage is the database (`shadow_trades`). The legacy append-only JSONL ledger
is migrated in once on first use so no history is lost.

The benchmark/sector anchors are the reason this is DB-backed and captured at
entry: benchmark-relative scoring of a *past* recommendation is impossible to
reconstruct after the fact, so the anchor price must be stored the moment the
call is made.
"""
from __future__ import annotations

import uuid
from typing import Optional

from . import config
from .data.prices import get_quote, sector_etf
from .db import repository as db_repo
from .models import RiskProfile, ShadowTrade, TradeTicket, _now

_MIGRATED = False

# Keys we snapshot off a TrendSignals so a logged idea stays attributable later
# (which signals were behind it: momentum, valuation, trend, RSI, volatility).
_SIGNAL_KEYS = (
    "price", "sector", "beta", "pe", "dividend_yield",
    "ret_1m_pct", "ret_3m_pct", "ret_6m_pct",
    "above_50d", "above_200d", "rsi_14", "vol_annualized_pct",
)


def _migrate_jsonl_once() -> None:
    """Import the legacy JSONL ledger into the DB once, then retire the file.

    Old records lack the new fields (benchmark anchors, decision label, etc.);
    they validate fine with defaults, so historical trades survive but are simply
    not benchmark-gradeable — which is exactly why we now capture anchors going
    forward."""
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    path = config.SHADOW_PATH
    try:
        if not path.exists() or db_repo.shadow_trade_count() > 0:
            return
        legacy: list[ShadowTrade] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                legacy.append(ShadowTrade.model_validate_json(line))
            except Exception:  # noqa: BLE001
                continue
        if legacy:
            db_repo.save_shadow_trades(legacy)
        path.rename(path.with_suffix(".jsonl.migrated"))
    except Exception:  # noqa: BLE001
        return


def _signals_snapshot(signals) -> dict:
    """Compact dict of the signals that justified a call. Accepts a TrendSignals
    dataclass (preferred) or a plain dict; returns {} when nothing was given."""
    if signals is None:
        return {}
    get = signals.get if isinstance(signals, dict) else (lambda k: getattr(signals, k, None))
    out: dict = {}
    for k in _SIGNAL_KEYS:
        v = get(k)
        if v is not None:
            out[k] = v
    return out


def has_open(ticker: str, source: Optional[str] = None) -> bool:
    """Is there already an open (un-closed) paper trade for this ticker? Used to
    keep engines from logging the same idea on every run."""
    _migrate_jsonl_once()
    return ticker.upper() in db_repo.open_shadow_tickers(source)


def log_recommendation(
    ticket: TradeTicket,
    source: str = "analyst",
    profile: Optional[RiskProfile] = None,
    flavor: str = "",
    signals=None,
) -> ShadowTrade:
    """Snapshot a recommendation as a paper trade at the current price, anchored
    to the market (SPY) and the stock's sector ETF so it can be graded later.

    Duplicates are kept on purpose (a re-call at a new price is real information) but
    are flagged at read-time in the scorecard so they're easy to spot and reconcile."""
    _migrate_jsonl_once()
    # Was this name already tracked before this call? If so, the new row is a re-call —
    # we still log it (its own price + SPY anchor are captured below, permanently), and
    # drop a non-blocking Activity ping so you can reconcile it whenever, never a modal.
    already_tracked = has_open(ticket.ticker)
    price = get_quote(ticket.ticker).price

    sector = ""
    if signals is not None:
        sector = (signals.get("sector") if isinstance(signals, dict)
                  else getattr(signals, "sector", "")) or ""
    entry_signals = _signals_snapshot(signals)

    bench_price = get_quote("SPY").price
    etf = sector_etf(sector)
    etf_price = get_quote(etf).price if etf else 0.0

    trade = ShadowTrade(
        id=uuid.uuid4().hex[:12],
        ticker=ticket.ticker.upper(),
        action=ticket.action,
        decision_label=ticket.decision_label,
        conviction=ticket.conviction,
        thesis=ticket.thesis,
        entry_price=price,
        last_price=price,
        last_at=_now(),
        source=source,
        risk_mode=(profile.appetite.value if profile else ""),
        flavor=flavor,
        sector=sector,
        entry_signals=entry_signals,
        bench_symbol="SPY",
        bench_entry_price=bench_price,
        bench_last_price=bench_price,
        sector_etf=etf,
        sector_etf_entry_price=etf_price,
        sector_etf_last_price=etf_price,
    )
    db_repo.save_shadow_trade(trade)
    # Non-blocking awareness of a re-call. Cooldowned so re-analyzing a name many times
    # in a day doesn't spam the rail; the underlying trades are all still logged + marked.
    if already_tracked and not db_repo.event_exists_recent("shadow_dup", ticket.ticker, 6.0):
        db_repo.save_research_event(
            event_type="shadow_dup", ticker=ticket.ticker.upper(), severity="info",
            title=f"{ticket.ticker.upper()} re-called at ${price:.2f} — already in shadow",
            summary=(f"Logged a fresh {source} paper trade at ${price:.2f} "
                     f"(SPY ${bench_price:.2f}) on top of an existing call. Both are kept and "
                     "the new one is marked 'repeat' in Shadow — reconcile there whenever, no rush."),
            source="shadow")
    return trade


def mark_to_market(refresh: bool = False) -> list[ShadowTrade]:
    """Refresh last_price (and the benchmark anchors) on all open trades. Call
    before reporting. Quotes are deduped to one lookup per unique symbol."""
    _migrate_jsonl_once()
    trades = db_repo.all_shadow_trades()
    if not trades:
        return []

    symbols: set[str] = set()
    for t in trades:
        if t.closed:
            continue
        symbols.add(t.ticker)
        symbols.add(t.bench_symbol or "SPY")
        if t.sector_etf:
            symbols.add(t.sector_etf)
    quotes = {s: get_quote(s, refresh=refresh).price for s in symbols}

    dirty: list[ShadowTrade] = []
    for t in trades:
        if t.closed:
            continue
        if quotes.get(t.ticker, 0.0) > 0:
            t.last_price = quotes[t.ticker]
        if quotes.get(t.bench_symbol or "SPY", 0.0) > 0:
            t.bench_last_price = quotes[t.bench_symbol or "SPY"]
        if t.sector_etf and quotes.get(t.sector_etf, 0.0) > 0:
            t.sector_etf_last_price = quotes[t.sector_etf]
        t.last_at = _now()
        dirty.append(t)

    if dirty:
        db_repo.save_shadow_trades(dirty)
    return trades


def set_user_executed(trade_id: str, executed: bool) -> None:
    _migrate_jsonl_once()
    for t in db_repo.all_shadow_trades():
        if t.id == trade_id:
            t.user_executed = executed
            db_repo.save_shadow_trade(t)
            return


def reconcile_duplicate(trade_id: str, mode: str) -> dict:
    """Resolve a duplicate re-call, on your schedule (never a blocking prompt).

    `trade_id` is the newer re-call (the row marked 'repeat'). mode:
      - 'replace' — this new call supersedes the prior one(s): close the older open
        trades for the same ticker, keep the new one. Their captured price + SPY anchor
        are preserved on the closed rows, so the history stays intact.
      - 'keep'    — keep both; just clear the 'repeat'-driven nudge (no-op on data)."""
    _migrate_jsonl_once()
    trades = db_repo.all_shadow_trades()
    new = next((t for t in trades if t.id == trade_id), None)
    if new is None:
        return {"ok": False, "error": "trade not found"}
    closed = 0
    if mode == "replace":
        older = [t for t in trades if t.ticker == new.ticker and not t.closed
                 and t.id != new.id and t.entry_at < new.entry_at]
        for t in older:
            t.closed = True
            t.closed_at = _now()
            t.close_reason = f"superseded by later {new.source} re-call"
        if older:
            db_repo.save_shadow_trades(older)
            closed = len(older)
    return {"ok": True, "closed": closed, "ticker": new.ticker, "mode": mode}


def scoreboard(refresh: bool = False) -> dict:
    """The track record. This is the number that earns (or loses) trust.

    Output keys are kept stable for the current UI; the full scorecard
    (calibration, attribution, benchmark-relative breakdowns) is built on top of
    the now-richer per-trade records in the evaluation layer."""
    trades = mark_to_market(refresh=refresh)
    if not trades:
        return {"count": 0, "win_rate": 0.0, "avg_return_pct": 0.0, "trades": []}
    returns = [t.return_pct() for t in trades]
    wins = sum(1 for r in returns if r > 0)
    return {
        "count": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_return_pct": round(sum(returns) / len(returns), 2),
        "best": round(max(returns), 2),
        "worst": round(min(returns), 2),
        "trades": [
            {
                **t.model_dump(),
                "return_pct": round(t.return_pct(), 2),
                "alpha_pct": round(t.alpha_pct(), 2),
            }
            for t in sorted(trades, key=lambda x: x.entry_at, reverse=True)
        ],
    }
