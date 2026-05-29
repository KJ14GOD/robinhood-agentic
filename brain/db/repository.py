from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from ..models import Briefing, Holding, Portfolio, PriceAlert, ResearchState, Thesis, WatchItem
from .models import (
    BriefingRecord,
    PortfolioSnapshot,
    PositionSnapshot,
    PriceAlertRecord,
    QuoteSnapshot,
    ResearchEventRecord,
    ThesisRecord,
    TickerResearchRecord,
    WatchlistItemRecord,
)
from .session import db_session, init_db

_READY = False


def _ensure_ready() -> bool:
    global _READY
    if _READY:
        return True
    try:
        init_db()
        _READY = True
        return True
    except Exception:
        return False


def _parse_dt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def save_portfolio_snapshot(pf: Portfolio) -> None:
    if not _ensure_ready() or not pf.holdings:
        return
    weights = pf.weights()
    try:
        with db_session() as session:
            snap = PortfolioSnapshot(
                source=pf.source,
                total_value=pf.total_value,
                cash=pf.cash,
                buying_power=pf.buying_power,
                reported_equity=pf.reported_equity,
                pricing_source=pf.pricing_source,
                pricing_warning=pf.pricing_warning,
                sync_ok=pf.sync_ok,
                sync_message=pf.sync_message,
                captured_at=_parse_dt(pf.as_of),
            )
            session.add(snap)
            session.flush()
            for h in pf.holdings:
                session.add(
                    PositionSnapshot(
                        snapshot_id=snap.id,
                        ticker=h.ticker,
                        quantity=h.quantity,
                        avg_cost=h.avg_cost,
                        current_price=h.current_price,
                        market_value=h.market_value,
                        weight=weights.get(h.ticker, 0.0),
                    )
                )
                session.add(
                    QuoteSnapshot(
                        ticker=h.ticker,
                        price=h.current_price,
                        source=pf.pricing_source or pf.source,
                        captured_at=_parse_dt(pf.as_of),
                    )
                )
    except Exception:
        return


def latest_portfolio_snapshot(source: str = "") -> Portfolio | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            stmt = select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.captured_at), desc(PortfolioSnapshot.id))
            if source:
                stmt = stmt.where(PortfolioSnapshot.source == source)
            snap = session.execute(stmt.limit(1)).scalars().first()
            if not snap:
                return None
            holdings = [
                Holding(
                    ticker=p.ticker,
                    quantity=p.quantity,
                    avg_cost=p.avg_cost,
                    current_price=p.current_price,
                )
                for p in snap.positions
            ]
            return Portfolio(
                holdings=holdings,
                cash=snap.cash,
                buying_power=snap.buying_power,
                reported_equity=snap.reported_equity,
                pricing_source=snap.pricing_source,
                pricing_warning=snap.pricing_warning,
                source=snap.source,
                sync_ok=snap.sync_ok,
                sync_message=snap.sync_message,
                as_of=snap.captured_at.isoformat(),
            )
    except Exception:
        return None


def portfolio_equity_history(span: str = "3m", source: str = "") -> list[tuple[str, float]]:
    if not _ensure_ready():
        return []
    days = {
        "1d": 1,
        "1m": 31,
        "3m": 93,
        "6m": 186,
        "1y": 366,
    }.get(span, 93)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with db_session() as session:
            stmt = (
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.captured_at >= cutoff)
                .order_by(PortfolioSnapshot.captured_at)
            )
            if source:
                stmt = stmt.where(PortfolioSnapshot.source == source)
            rows = session.execute(stmt).scalars().all()
            return [(r.captured_at.isoformat(), r.total_value) for r in rows if r.total_value > 0]
    except Exception:
        return []


def load_research_state() -> ResearchState | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            watch_rows = session.execute(
                select(WatchlistItemRecord).order_by(WatchlistItemRecord.updated_at)
            ).scalars().all()
            thesis_rows = session.execute(select(ThesisRecord)).scalars().all()
            alert_rows = session.execute(
                select(PriceAlertRecord).order_by(PriceAlertRecord.created_at)
            ).scalars().all()
            briefing_rows = session.execute(
                select(BriefingRecord).order_by(BriefingRecord.created_at)
            ).scalars().all()
            return ResearchState(
                watchlist=[
                    WatchItem(
                        ticker=r.ticker,
                        reason=r.reason,
                        mode=r.mode if r.mode in {"stable", "balanced", "volatile"} else "balanced",
                        target_entry=r.target_entry,
                        max_allocation_pct=r.max_allocation_pct,
                        added_at=r.added_at.isoformat(),
                        updated_at=r.updated_at.isoformat(),
                    )
                    for r in watch_rows
                ],
                theses={
                    r.ticker: Thesis(
                        ticker=r.ticker,
                        thesis=r.thesis,
                        status=r.status if r.status in {"active", "review", "broken", "archived"} else "active",
                        strengthens=json.loads(r.strengthens_json or "[]"),
                        weakens=json.loads(r.weakens_json or "[]"),
                        invalidation=r.invalidation,
                        last_decision=r.last_decision,
                        updated_at=r.updated_at.isoformat(),
                    )
                    for r in thesis_rows
                },
                alerts=[
                    PriceAlert(
                        ticker=r.ticker,
                        kind=r.kind if r.kind in {"below", "above", "review"} else "review",
                        threshold=r.threshold,
                        note=r.note,
                        active=r.active,
                        triggered_at=r.triggered_at.isoformat() if r.triggered_at else "",
                        created_at=r.created_at.isoformat(),
                    )
                    for r in alert_rows
                ],
                briefings=[
                    Briefing(
                        id=r.id,
                        kind=r.kind if r.kind in {"morning", "evening", "manual"} else "manual",
                        title=r.title,
                        summary=r.summary,
                        bullets=json.loads(r.bullets_json or "[]"),
                        actions=json.loads(r.actions_json or "[]"),
                        created_at=r.created_at.isoformat(),
                    )
                    for r in briefing_rows
                ],
            )
    except Exception:
        return None


def save_research_state(state: ResearchState) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            for item in state.watchlist:
                row = session.execute(
                    select(WatchlistItemRecord).where(WatchlistItemRecord.ticker == item.ticker)
                ).scalars().first()
                if not row:
                    row = WatchlistItemRecord(ticker=item.ticker)
                    session.add(row)
                row.reason = item.reason
                row.mode = item.mode
                row.target_entry = item.target_entry
                row.max_allocation_pct = item.max_allocation_pct
                row.added_at = _parse_dt(item.added_at)
                row.updated_at = _parse_dt(item.updated_at)

            for thesis in state.theses.values():
                row = session.execute(
                    select(ThesisRecord).where(ThesisRecord.ticker == thesis.ticker)
                ).scalars().first()
                if not row:
                    row = ThesisRecord(ticker=thesis.ticker)
                    session.add(row)
                row.thesis = thesis.thesis
                row.status = thesis.status
                row.strengthens_json = json.dumps(thesis.strengthens)
                row.weakens_json = json.dumps(thesis.weakens)
                row.invalidation = thesis.invalidation
                row.last_decision = thesis.last_decision
                row.updated_at = _parse_dt(thesis.updated_at)

            for briefing in state.briefings:
                row = session.get(BriefingRecord, briefing.id)
                if not row:
                    row = BriefingRecord(id=briefing.id)
                    session.add(row)
                row.kind = briefing.kind
                row.title = briefing.title
                row.summary = briefing.summary
                row.bullets_json = json.dumps(briefing.bullets)
                row.actions_json = json.dumps(briefing.actions)
                row.created_at = _parse_dt(briefing.created_at)

            for alert in state.alerts:
                row = session.execute(
                    select(PriceAlertRecord)
                    .where(PriceAlertRecord.ticker == alert.ticker)
                    .where(PriceAlertRecord.kind == alert.kind)
                    .where(PriceAlertRecord.threshold == alert.threshold)
                ).scalars().first()
                if not row:
                    row = PriceAlertRecord(ticker=alert.ticker)
                    session.add(row)
                row.kind = alert.kind
                row.threshold = alert.threshold
                row.note = alert.note
                row.active = alert.active
                row.triggered_at = _parse_dt(alert.triggered_at) if alert.triggered_at else None
                row.created_at = _parse_dt(alert.created_at)
    except Exception:
        return


def save_research_event(
    event_type: str,
    title: str,
    summary: str = "",
    ticker: str = "",
    severity: str = "info",
    source: str = "",
) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            session.add(
                ResearchEventRecord(
                    ticker=ticker.upper(),
                    event_type=event_type,
                    severity=severity,
                    title=title,
                    summary=summary,
                    source=source,
                )
            )
    except Exception:
        return


def event_exists_recent(event_type: str, ticker: str, within_hours: float = 12.0) -> bool:
    """Dedup guard for the monitor loop: has this exact (event_type, ticker) event
    already fired inside the cooldown window? Keeps the loop from re-logging the
    same standing condition (e.g. 'over concentration line') every cycle."""
    if not _ensure_ready():
        return False
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        with db_session() as session:
            stmt = (
                select(ResearchEventRecord.id)
                .where(ResearchEventRecord.event_type == event_type)
                .where(ResearchEventRecord.ticker == ticker.upper())
                .where(ResearchEventRecord.created_at >= cutoff)
                .limit(1)
            )
            return session.execute(stmt).first() is not None
    except Exception:
        return False


def recent_events(limit: int = 50, within_hours: float | None = None,
                  event_types: list[str] | None = None) -> list[dict]:
    """Read back the persisted event stream for the Today surface, newest first."""
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            stmt = select(ResearchEventRecord).order_by(desc(ResearchEventRecord.created_at))
            if within_hours is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
                stmt = stmt.where(ResearchEventRecord.created_at >= cutoff)
            if event_types:
                stmt = stmt.where(ResearchEventRecord.event_type.in_(event_types))
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return [
                {
                    "id": r.id,
                    "ticker": r.ticker,
                    "event_type": r.event_type,
                    "severity": r.severity,
                    "title": r.title,
                    "summary": r.summary,
                    "source": r.source,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
    except Exception:
        return []


def upsert_ticker_research(
    ticker: str,
    action_label: str,
    thesis: str,
    confidence: float = 0.0,
    bull_case: str = "",
    bear_case: str = "",
    risks: str = "",
    source: str = "",
) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(TickerResearchRecord, ticker.upper())
            if not row:
                row = TickerResearchRecord(ticker=ticker.upper())
                session.add(row)
            row.action_label = action_label
            row.confidence = confidence
            row.thesis = thesis
            row.bull_case = bull_case
            row.bear_case = bear_case
            row.risks = risks
            row.source = source
            row.refreshed_at = datetime.now(timezone.utc)
    except Exception:
        return


def get_ticker_research(ticker: str) -> dict | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            row = session.get(TickerResearchRecord, ticker.upper())
            if not row:
                return None
            return {
                "ticker": row.ticker,
                "action_label": row.action_label,
                "confidence": row.confidence,
                "thesis": row.thesis,
                "bull_case": row.bull_case,
                "bear_case": row.bear_case,
                "risks": row.risks,
                "source": row.source,
                "refreshed_at": row.refreshed_at.isoformat(),
            }
    except Exception:
        return None
