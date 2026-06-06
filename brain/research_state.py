"""Persistent research memory: theses, watchlist, and alerts.

This is the layer that turns one-off recommendations into a portfolio brain
with memory. Engines can update it after producing recommendations; the
dashboard and agent can read it to keep context across sessions.
"""
from __future__ import annotations

from . import config
from .db import repository as db_repo
from .models import Briefing, ResearchState, StockIdea, Thesis, TradeTicket, WatchItem, _now


def load_state() -> ResearchState:
    db_state = db_repo.load_research_state()
    if db_state and (db_state.watchlist or db_state.theses or db_state.briefings):
        return db_state
    if config.RESEARCH_STATE_PATH.exists():
        state = ResearchState.model_validate_json(config.RESEARCH_STATE_PATH.read_text())
        db_repo.save_research_state(state)
        return state
    state = ResearchState()
    save_state(state)
    return state


def save_state(state: ResearchState) -> ResearchState:
    state.updated_at = _now()
    config.RESEARCH_STATE_PATH.write_text(state.model_dump_json(indent=2))
    db_repo.save_research_state(state)
    return state


def add_briefing(briefing: Briefing) -> ResearchState:
    state = load_state()
    state.briefings.append(briefing)
    state.briefings = state.briefings[-50:]
    db_repo.save_research_event(
        event_type="briefing",
        severity="info",
        title=briefing.title,
        summary=briefing.summary,
        source=f"briefing:{briefing.kind}",
    )
    return save_state(state)


def _mode_from_size(size: float) -> str:
    if 0 < size <= 3:
        return "volatile"
    if size >= 8:
        return "stable"
    return "balanced"


def update_from_ticket(ticket: TradeTicket) -> ResearchState:
    state = load_state()
    ticker = ticket.ticker.upper()
    existing = state.theses.get(ticker)
    state.theses[ticker] = Thesis(
        ticker=ticker,
        thesis=ticket.thesis,
        status="review" if ticket.action in ("sell", "trim") else "active",
        strengthens=existing.strengthens if existing else [],
        weakens=existing.weakens if existing else [],
        invalidation=ticket.risks,
        last_decision=ticket.decision_label,
    )
    db_repo.upsert_ticker_research(
        ticker=ticker,
        action_label=ticket.decision_label,
        thesis=ticket.thesis,
        confidence=float(ticket.conviction),
        bull_case=ticket.catalyst,
        risks=ticket.risks,
        source="analyze",
    )
    db_repo.save_research_event(
        event_type="ticker_research",
        severity="info" if ticket.action in ("hold", "watch") else "action",
        ticker=ticker,
        title=f"{ticker} {ticket.decision_label}",
        summary=ticket.thesis,
        source="analyze",
    )
    if ticket.action in ("buy", "add", "watch"):
        upsert_watch_item(
            ticker=ticker,
            reason=_watch_reason(ticket),
            mode=_mode_from_size(ticket.suggested_size_pct),
            max_allocation_pct=ticket.suggested_size_pct,
            state=state,
        )
    return save_state(state)


def _watch_reason(ticket: TradeTicket) -> str:
    """Short *why-track* line for the watchlist — the forward catalyst we're
    waiting on, NOT the full thesis (that lives on the Thesis record). Keeps the
    three research tables meaning three different things."""
    catalyst = (ticket.catalyst or "").strip()
    if catalyst:
        return catalyst[:200]
    return f"Tracking after {ticket.decision_label.lower()} call."


def add_discovery_ideas(ideas: list[StockIdea]) -> ResearchState:
    state = load_state()
    for idea in ideas:
        upsert_watch_item(
            ticker=idea.ticker.upper(),
            reason=idea.why_now,
            mode="volatile" if idea.risk_flavor == "volatile" else
            "stable" if idea.risk_flavor == "stable" else "balanced",
            state=state,
        )
        db_repo.save_research_event(
            event_type="discovery",
            severity="info",
            ticker=idea.ticker.upper(),
            title=f"{idea.ticker.upper()} added from discovery",
            summary=idea.why_now,
            source="discover",
        )
    return save_state(state)


def upsert_watch_item(ticker: str, reason: str = "", mode: str = "balanced",
                      max_allocation_pct: float = 0.0,
                      state: ResearchState | None = None) -> ResearchState:
    state = state or load_state()
    ticker = ticker.upper()
    for item in state.watchlist:
        if item.ticker == ticker:
            item.reason = reason or item.reason
            item.mode = mode if mode in ("stable", "balanced", "volatile") else item.mode
            item.max_allocation_pct = max_allocation_pct or item.max_allocation_pct
            item.updated_at = _now()
            return state
    state.watchlist.append(WatchItem(
        ticker=ticker,
        reason=reason,
        mode=mode if mode in ("stable", "balanced", "volatile") else "balanced",
        max_allocation_pct=max_allocation_pct,
    ))
    state.watchlist = state.watchlist[-100:]
    return state


def save_watch_item(ticker: str, reason: str = "", mode: str = "balanced",
                    max_allocation_pct: float = 0.0) -> ResearchState:
    state = upsert_watch_item(ticker, reason, mode, max_allocation_pct)
    return save_state(state)


def set_watch_target(ticker: str, target_entry: float) -> ResearchState:
    """Set (or clear, with 0) the entry-price alert on a watchlist name. The
    monitor pings when an unheld watchlist ticker trades at/below this price."""
    state = load_state()
    ticker = ticker.upper()
    target = max(0.0, float(target_entry or 0.0))
    for item in state.watchlist:
        if item.ticker == ticker:
            item.target_entry = target
            item.updated_at = _now()
            return save_state(state)
    # Not tracked yet — add a minimal row that carries the alert.
    state.watchlist.append(WatchItem(ticker=ticker, target_entry=target,
                                     reason="Entry-price watch."))
    state.watchlist = state.watchlist[-100:]
    return save_state(state)


def remove_watch_item(ticker: str) -> bool:
    """Drop a name from the watchlist. Returns True if it was there. Saving alone is
    upsert-only, so we also delete the DB row explicitly or it would reappear on reload."""
    state = load_state()
    ticker = ticker.upper()
    before = len(state.watchlist)
    state.watchlist = [w for w in state.watchlist if w.ticker != ticker]
    save_state(state)
    removed_db = db_repo.delete_watchlist_item(ticker)
    return removed_db or len(state.watchlist) != before


def remove_thesis(ticker: str) -> bool:
    """Drop a stored thesis on a name (stops it being re-judged). Returns True if found."""
    state = load_state()
    ticker = ticker.upper()
    existed = ticker in state.theses
    state.theses.pop(ticker, None)
    save_state(state)
    removed_db = db_repo.delete_thesis(ticker)
    return removed_db or existed


def summarize_for_prompt(max_items: int = 20) -> str:
    state = load_state()
    lines: list[str] = []
    if state.watchlist:
        lines.append("Watchlist:")
        for item in state.watchlist[-max_items:]:
            size = f", max {item.max_allocation_pct:.1f}%" if item.max_allocation_pct else ""
            lines.append(f"- {item.ticker} ({item.mode}{size}): {item.reason}")
    if state.theses:
        lines.append("Stored theses:")
        for thesis in list(state.theses.values())[-max_items:]:
            lines.append(
                f"- {thesis.ticker}: {thesis.last_decision}, {thesis.status}. "
                f"Thesis: {thesis.thesis} Invalidation: {thesis.invalidation}"
            )
    return "\n".join(lines) or "No persistent research memory yet."
