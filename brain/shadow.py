"""Shadow mode — the honesty layer.

Every recommendation the brain makes is logged here as a timestamped paper
trade. We mark them to market over time and compute a real track record, so
you can find out whether the brain is good *before* real money rides on it.

Stored as append-only JSONL so the history is never silently rewritten.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from . import config
from .data.prices import get_quote
from .models import ShadowTrade, TradeTicket, _now


def _read_all() -> list[ShadowTrade]:
    if not config.SHADOW_PATH.exists():
        return []
    out: list[ShadowTrade] = []
    for line in config.SHADOW_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(ShadowTrade.model_validate_json(line))
    return out


def _write_all(trades: list[ShadowTrade]) -> None:
    config.SHADOW_PATH.write_text(
        "\n".join(t.model_dump_json() for t in trades) + ("\n" if trades else "")
    )


def log_recommendation(ticket: TradeTicket, source: str = "analyst") -> ShadowTrade:
    """Snapshot a recommendation as a paper trade at the current price."""
    price = get_quote(ticket.ticker).price
    trade = ShadowTrade(
        id=uuid.uuid4().hex[:12],
        ticker=ticket.ticker.upper(),
        action=ticket.action,
        conviction=ticket.conviction,
        thesis=ticket.thesis,
        entry_price=price,
        last_price=price,
        last_at=_now(),
        source=source,
    )
    with config.SHADOW_PATH.open("a") as f:
        f.write(trade.model_dump_json() + "\n")
    return trade


def mark_to_market(refresh: bool = False) -> list[ShadowTrade]:
    """Refresh last_price on all open trades. Call before reporting."""
    trades = _read_all()
    for t in trades:
        if not t.closed:
            t.last_price = get_quote(t.ticker, refresh=refresh).price
            t.last_at = _now()
    _write_all(trades)
    return trades


def set_user_executed(trade_id: str, executed: bool) -> None:
    trades = _read_all()
    for t in trades:
        if t.id == trade_id:
            t.user_executed = executed
    _write_all(trades)


def scoreboard(refresh: bool = False) -> dict:
    """The track record. This is the number that earns (or loses) trust."""
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
            {**t.model_dump(), "return_pct": round(t.return_pct(), 2)}
            for t in sorted(trades, key=lambda x: x.entry_at, reverse=True)
        ],
    }
