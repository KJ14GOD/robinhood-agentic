from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select

from ..models import (
    Briefing, Holding, Mission, MissionCandidate, Portfolio, ResearchState,
    ShadowTrade, Thesis, WatchItem,
)
from .models import (
    AgentRunRecord,
    BriefingRecord,
    MissionCandidateRecord,
    MissionRecord,
    PortfolioSnapshot,
    PositionSnapshot,
    ResearchEventRecord,
    ShadowTradeRecord,
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


def event_exists_recent(event_type, ticker: str, within_hours: float = 12.0) -> bool:
    """Dedup/cooldown guard: has an event of this type (or any of these types) for
    this ticker fired inside the window? `event_type` may be a str or a list — the
    monitor uses one type, the living-memory engine passes several outcome types so
    it won't re-judge the same name more than once a day."""
    if not _ensure_ready():
        return False
    types = [event_type] if isinstance(event_type, str) else list(event_type)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        with db_session() as session:
            stmt = (
                select(ResearchEventRecord.id)
                .where(ResearchEventRecord.event_type.in_(types))
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


# --- shadow trades (the evaluation layer's raw material) -------------------- #
def _record_to_trade(r: ShadowTradeRecord) -> ShadowTrade:
    return ShadowTrade(
        id=r.id,
        ticker=r.ticker,
        action=r.action,
        decision_label=r.decision_label,
        conviction=r.conviction,
        thesis=r.thesis,
        entry_price=r.entry_price,
        entry_at=r.entry_at.isoformat() if r.entry_at else "",
        source=r.source,
        risk_mode=r.risk_mode,
        flavor=r.flavor,
        sector=r.sector,
        entry_signals=json.loads(r.entry_signals_json or "{}"),
        bench_symbol=r.bench_symbol,
        bench_entry_price=r.bench_entry_price,
        sector_etf=r.sector_etf,
        sector_etf_entry_price=r.sector_etf_entry_price,
        last_price=r.last_price,
        last_at=r.last_at.isoformat() if r.last_at else "",
        bench_last_price=r.bench_last_price,
        sector_etf_last_price=r.sector_etf_last_price,
        closed=r.closed,
        closed_at=r.closed_at.isoformat() if r.closed_at else "",
        close_reason=r.close_reason,
        user_executed=r.user_executed,
    )


def _apply_trade(r: ShadowTradeRecord, t: ShadowTrade) -> None:
    r.ticker = t.ticker.upper()
    r.action = t.action
    r.decision_label = t.decision_label
    r.conviction = t.conviction
    r.risk_mode = t.risk_mode
    r.flavor = t.flavor
    r.sector = t.sector
    r.thesis = t.thesis
    r.source = t.source
    r.entry_price = t.entry_price
    r.entry_at = _parse_dt(t.entry_at) if t.entry_at else datetime.now(timezone.utc)
    r.entry_signals_json = json.dumps(t.entry_signals or {})
    r.bench_symbol = t.bench_symbol
    r.bench_entry_price = t.bench_entry_price
    r.sector_etf = t.sector_etf
    r.sector_etf_entry_price = t.sector_etf_entry_price
    r.last_price = t.last_price
    r.last_at = _parse_dt(t.last_at) if t.last_at else None
    r.bench_last_price = t.bench_last_price
    r.sector_etf_last_price = t.sector_etf_last_price
    r.closed = t.closed
    r.closed_at = _parse_dt(t.closed_at) if t.closed_at else None
    r.close_reason = t.close_reason
    r.user_executed = t.user_executed


def save_shadow_trade(trade: ShadowTrade) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(ShadowTradeRecord, trade.id)
            if not row:
                row = ShadowTradeRecord(id=trade.id)
                session.add(row)
            _apply_trade(row, trade)
    except Exception:
        return


def save_shadow_trades(trades: list[ShadowTrade]) -> None:
    """Upsert many trades in one transaction — used by mark-to-market."""
    if not _ensure_ready() or not trades:
        return
    try:
        with db_session() as session:
            for trade in trades:
                row = session.get(ShadowTradeRecord, trade.id)
                if not row:
                    row = ShadowTradeRecord(id=trade.id)
                    session.add(row)
                _apply_trade(row, trade)
    except Exception:
        return


def delete_shadow_trades(ids: list[str]) -> int:
    """Hard-delete paper trades by id. Used when a duplicate re-call replaces the older
    call(s) outright. Returns how many rows were removed."""
    if not _ensure_ready() or not ids:
        return 0
    try:
        with db_session() as session:
            n = 0
            for tid in ids:
                row = session.get(ShadowTradeRecord, tid)
                if row:
                    session.delete(row)
                    n += 1
            return n
    except Exception:
        return 0


def all_shadow_trades() -> list[ShadowTrade]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(
                select(ShadowTradeRecord).order_by(desc(ShadowTradeRecord.entry_at))
            ).scalars().all()
            return [_record_to_trade(r) for r in rows]
    except Exception:
        return []


def shadow_trade_count() -> int:
    if not _ensure_ready():
        return 0
    try:
        with db_session() as session:
            return int(session.execute(select(func.count(ShadowTradeRecord.id))).scalar() or 0)
    except Exception:
        return 0


def open_shadow_tickers(source: str | None = None) -> set[str]:
    """Tickers with an un-closed paper trade — used so engines don't double-log
    the same open idea every run."""
    if not _ensure_ready():
        return set()
    try:
        with db_session() as session:
            stmt = select(ShadowTradeRecord.ticker).where(ShadowTradeRecord.closed.is_(False))
            if source is not None:
                stmt = stmt.where(ShadowTradeRecord.source == source)
            return {t.upper() for t in session.execute(stmt).scalars().all()}
    except Exception:
        return set()


# --- agent runs (audit trail) ----------------------------------------------- #
def save_agent_run(
    query: str,
    answer: str = "",
    kind: str = "chat",
    steps: list[dict] | None = None,
    tools_used: str = "",
    model: str = "",
    run_id: str | None = None,
) -> str:
    """Persist one agent loop's trace. Returns the run id (generated if absent)."""
    rid = run_id or uuid.uuid4().hex[:12]
    if not _ensure_ready():
        return rid
    try:
        with db_session() as session:
            session.add(
                AgentRunRecord(
                    id=rid,
                    kind=kind,
                    query=query[:8000],
                    answer=answer[:20000],
                    steps_json=json.dumps(steps or [])[:200000],
                    tools_used=tools_used[:255],
                    model=model,
                )
            )
    except Exception:
        return rid
    return rid


def recent_agent_runs(limit: int = 20, kind: str | None = None) -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            stmt = select(AgentRunRecord).order_by(desc(AgentRunRecord.created_at))
            if kind:
                stmt = stmt.where(AgentRunRecord.kind == kind)
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "query": r.query,
                    "answer": r.answer,
                    "steps": json.loads(r.steps_json or "[]"),
                    "tools_used": r.tools_used,
                    "model": r.model,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
    except Exception:
        return []


# --- strategy missions ------------------------------------------------------ #
def _candidate_to_model(r: MissionCandidateRecord) -> MissionCandidate:
    return MissionCandidate(
        ticker=r.ticker,
        label=r.label if r.label in {"BUY", "WATCH", "WAIT", "REJECT"} else "WATCH",
        conviction=max(1, min(10, r.conviction or 5)),
        reason=r.reason,
        sector=r.sector,
        signals=json.loads(r.signals_json or "{}"),
        first_seen=r.first_seen.isoformat() if r.first_seen else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


def _mission_to_model(r: MissionRecord) -> Mission:
    return Mission(
        id=r.id,
        title=r.title,
        theme=r.theme,
        mode=r.mode if r.mode in {"stable", "balanced", "volatile", "any"} else "any",
        status=r.status if r.status in {"active", "paused", "archived"} else "active",
        candidates=[_candidate_to_model(c) for c in sorted(r.candidates, key=lambda x: x.ticker)],
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
        last_run_at=r.last_run_at.isoformat() if r.last_run_at else "",
        last_classified_at=r.last_classified_at.isoformat() if r.last_classified_at else "",
        last_seeded_at=r.last_seeded_at.isoformat() if r.last_seeded_at else "",
    )


def save_mission(mission: Mission) -> None:
    """Upsert a mission and replace its candidate roster. The engine supplies the
    correct first_seen for carried-over names, so a full replace is safe."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(MissionRecord, mission.id)
            if not row:
                row = MissionRecord(id=mission.id)
                session.add(row)
            row.title = mission.title
            row.theme = mission.theme
            row.mode = mission.mode
            row.status = mission.status
            row.created_at = _parse_dt(mission.created_at) if mission.created_at else datetime.now(timezone.utc)
            row.updated_at = _parse_dt(mission.updated_at) if mission.updated_at else datetime.now(timezone.utc)
            row.last_run_at = _parse_dt(mission.last_run_at) if mission.last_run_at else None
            row.last_classified_at = _parse_dt(mission.last_classified_at) if mission.last_classified_at else None
            row.last_seeded_at = _parse_dt(mission.last_seeded_at) if mission.last_seeded_at else None
            session.flush()
            # rebuild the roster
            for c in list(row.candidates):
                session.delete(c)
            session.flush()
            for c in mission.candidates:
                session.add(MissionCandidateRecord(
                    mission_id=mission.id,
                    ticker=c.ticker.upper(),
                    label=c.label,
                    conviction=c.conviction,
                    reason=c.reason,
                    sector=c.sector,
                    signals_json=json.dumps(c.signals or {}),
                    first_seen=_parse_dt(c.first_seen) if c.first_seen else datetime.now(timezone.utc),
                    updated_at=_parse_dt(c.updated_at) if c.updated_at else datetime.now(timezone.utc),
                ))
    except Exception:
        return


def all_missions(status: str | None = None) -> list[Mission]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            stmt = select(MissionRecord).order_by(desc(MissionRecord.updated_at))
            if status:
                stmt = stmt.where(MissionRecord.status == status)
            return [_mission_to_model(r) for r in session.execute(stmt).scalars().all()]
    except Exception:
        return []


def get_mission(mission_id: str) -> Mission | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            row = session.get(MissionRecord, mission_id)
            return _mission_to_model(row) if row else None
    except Exception:
        return None


def set_mission_status(mission_id: str, status: str) -> Mission | None:
    if not _ensure_ready() or status not in {"active", "paused", "archived"}:
        return None
    try:
        with db_session() as session:
            row = session.get(MissionRecord, mission_id)
            if not row:
                return None
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
            session.flush()
            return _mission_to_model(row)
    except Exception:
        return None


def delete_mission(mission_id: str) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(MissionRecord, mission_id)
            if row:
                session.delete(row)
    except Exception:
        return
