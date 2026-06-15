from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, desc, func, select

from ..models import (
    Briefing, Holding, Mandate, Mission, MissionCandidate, Portfolio, ResearchState,
    ShadowTrade, Thesis, WatchItem,
)
from .models import (
    AgentRunRecord,
    AutonomousThemeRecord,
    BriefingRecord,
    ChatMessageRecord,
    EvalJudgementRecord,
    EvalLabelRecord,
    EvidenceItemRecord,
    MandatePlanStateRecord,
    MandateRecord,
    MissionCandidateRecord,
    MissionRecord,
    PortfolioSnapshot,
    PositionSnapshot,
    ResearchEventRecord,
    ShadowTradeRecord,
    ThesisRecord,
    TickerResearchRecord,
    TwinEquityRecord,
    TwinFundRecord,
    TwinPositionRecord,
    TwinTradeRecord,
    TwinTradeReviewRecord,
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


def delete_watchlist_item(ticker: str) -> bool:
    """Hard-delete a watchlist row. `save_research_state` is upsert-only, so removing a
    name from the state isn't enough — the row must be deleted explicitly or it reappears
    on the next load. Returns True if a row was removed."""
    if not _ensure_ready() or not ticker:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                select(WatchlistItemRecord).where(WatchlistItemRecord.ticker == ticker.upper())
            ).scalars().first()
            if not row:
                return False
            session.delete(row)
            return True
    except Exception:
        return False


def delete_thesis(ticker: str) -> bool:
    """Hard-delete a stored thesis row (same upsert-only caveat as the watchlist)."""
    if not _ensure_ready() or not ticker:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                select(ThesisRecord).where(ThesisRecord.ticker == ticker.upper())
            ).scalars().first()
            if not row:
                return False
            session.delete(row)
            return True
    except Exception:
        return False


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


def save_chat_message(role: str, content: str) -> None:
    """Persist one turn of the Home conversation. Best-effort — chat must keep
    working even if the DB write fails."""
    if not _ensure_ready() or not (content or "").strip():
        return
    try:
        with db_session() as session:
            session.add(ChatMessageRecord(role=role, content=content))
    except Exception:
        return


def recent_chat_messages(limit: int = 80, within_hours: float = 24.0 * 7) -> list[dict]:
    """The persisted Home conversation, oldest first (chat order)."""
    if not _ensure_ready():
        return []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        with db_session() as session:
            stmt = (
                select(ChatMessageRecord)
                .where(ChatMessageRecord.created_at >= cutoff)
                .order_by(desc(ChatMessageRecord.created_at))
                .limit(limit)
            )
            rows = list(session.execute(stmt).scalars().all())
        rows.reverse()
        return [
            {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]
    except Exception:
        return []


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


# --- mandate (the user's standing goal) ------------------------------------- #
def load_mandate() -> Mandate:
    if not _ensure_ready():
        return Mandate()
    try:
        with db_session() as session:
            row = session.get(MandateRecord, "default")
            if not row:
                return Mandate()
            return Mandate(
                statement=row.statement, horizon=row.horizon, risk=row.risk, style=row.style,
                favor=json.loads(row.favor_json or "[]"), avoid=json.loads(row.avoid_json or "[]"),
                summary=row.summary,
                updated_at=row.updated_at.isoformat() if row.updated_at else "",
            )
    except Exception:
        return Mandate()


def save_mandate(m: Mandate) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(MandateRecord, "default")
            if not row:
                row = MandateRecord(id="default")
                session.add(row)
            row.statement = m.statement
            row.horizon = m.horizon
            row.risk = m.risk
            row.style = m.style
            row.favor_json = json.dumps(m.favor or [])
            row.avoid_json = json.dumps(m.avoid or [])
            row.summary = m.summary
            row.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


def load_mandate_plan_sig() -> list | None:
    """The holdings/weights signature as of the last plan we sent. None if never set (so the
    drift check baselines on first run instead of firing)."""
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            row = session.get(MandatePlanStateRecord, "default")
            return json.loads(row.signature_json or "[]") if row else None
    except Exception:
        return None


def save_mandate_plan_sig(sig: list) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            row = session.get(MandatePlanStateRecord, "default")
            if not row:
                row = MandatePlanStateRecord(id="default")
                session.add(row)
            row.signature_json = json.dumps(sig or [])
            row.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


# --- eval labels (error analysis on brain traces) --------------------------- #
def save_eval_label(run_id: str, kind: str, ticker: str, verdict: str,
                    failure_modes: list[str], note: str = "") -> bool:
    """Upsert the human label on one trace (one label per run_id; latest wins)."""
    if not _ensure_ready() or not run_id:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                select(EvalLabelRecord).where(EvalLabelRecord.run_id == run_id).limit(1)
            ).scalars().first()
            if row is None:
                row = EvalLabelRecord(run_id=run_id, created_at=datetime.now(timezone.utc))
                session.add(row)
            row.kind = kind or row.kind or ""
            row.ticker = (ticker or row.ticker or "").upper()
            row.verdict = verdict or ""
            row.failure_modes_json = json.dumps(failure_modes or [])
            row.note = (note or "")[:4000]
            row.updated_at = datetime.now(timezone.utc)
        return True
    except Exception:
        return False


def eval_labels_by_run(run_ids: list[str]) -> dict[str, dict]:
    """Existing labels for a set of runs, keyed by run_id — so the review list can show
    what's already been judged."""
    if not _ensure_ready() or not run_ids:
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                select(EvalLabelRecord).where(EvalLabelRecord.run_id.in_(list(run_ids)))
            ).scalars().all()
            return {r.run_id: {"verdict": r.verdict,
                               "failure_modes": json.loads(r.failure_modes_json or "[]"),
                               "note": r.note} for r in rows}
    except Exception:
        return {}


def eval_summary() -> dict:
    """The emerging eval suite: how many traces labeled, verdict split, and failure-mode
    frequencies (the taxonomy, ranked) — i.e. what the brain fails at, and how often."""
    if not _ensure_ready():
        return {"labeled": 0, "verdicts": {}, "failure_counts": []}
    try:
        with db_session() as session:
            rows = session.execute(select(EvalLabelRecord)).scalars().all()
        verdicts: dict[str, int] = {}
        fails: dict[str, int] = {}
        for r in rows:
            verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
            for tag in json.loads(r.failure_modes_json or "[]"):
                fails[tag] = fails.get(tag, 0) + 1
        ranked = sorted(({"tag": k, "count": v} for k, v in fails.items()),
                        key=lambda x: x["count"], reverse=True)
        return {"labeled": len(rows), "verdicts": verdicts, "failure_counts": ranked}
    except Exception:
        return {"labeled": 0, "verdicts": {}, "failure_counts": []}


# --- eval judgements (the LLM-as-judge auto-score; eval Phase 2) ------------ #
def save_eval_judgement(run_id: str, kind: str, ticker: str, verdict: str, score: int,
                        failure_modes: list[str], grounding: list[dict], rationale: str,
                        fix: str = "", revised: bool = False, model: str = "") -> bool:
    """Upsert the machine judge's score on one trace (one per run_id; latest wins)."""
    if not _ensure_ready() or not run_id:
        return False
    try:
        with db_session() as session:
            row = session.execute(
                select(EvalJudgementRecord).where(EvalJudgementRecord.run_id == run_id).limit(1)
            ).scalars().first()
            if row is None:
                row = EvalJudgementRecord(run_id=run_id, created_at=datetime.now(timezone.utc))
                session.add(row)
            row.kind = kind or row.kind or ""
            row.ticker = (ticker or row.ticker or "").upper()
            row.verdict = verdict or ""
            row.score = int(score or 0)
            row.failure_modes_json = json.dumps(failure_modes or [])
            row.grounding_json = json.dumps(grounding or [])
            row.rationale = (rationale or "")[:4000]
            row.fix = (fix or "")[:2000]
            row.revised = bool(revised)
            row.model = model or ""
            row.updated_at = datetime.now(timezone.utc)
        return True
    except Exception:
        return False


def eval_judgements_by_run(run_ids: list[str]) -> dict[str, dict]:
    """Machine judgements for a set of runs, keyed by run_id — so the worklist can show the
    judge's read next to (and against) the human label."""
    if not _ensure_ready() or not run_ids:
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                select(EvalJudgementRecord).where(EvalJudgementRecord.run_id.in_(list(run_ids)))
            ).scalars().all()
            return {r.run_id: {"verdict": r.verdict, "score": r.score,
                               "failure_modes": json.loads(r.failure_modes_json or "[]"),
                               "grounding": json.loads(r.grounding_json or "[]"),
                               "rationale": r.rationale, "fix": r.fix, "revised": r.revised}
                    for r in rows}
    except Exception:
        return {}


def unjudged_run_ids(limit: int = 20,
                     kinds: tuple[str, ...] = ("analyst", "rejudge", "deep_research")) -> list[dict]:
    """Recent reasoning traces with no machine judgement yet — the background sweep's worklist."""
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            judged = set(session.execute(select(EvalJudgementRecord.run_id)).scalars().all())
            rows = session.execute(
                select(AgentRunRecord).where(AgentRunRecord.kind.in_(list(kinds)))
                .order_by(desc(AgentRunRecord.created_at)).limit(limit * 4)
            ).scalars().all()
            out: list[dict] = []
            for r in rows:
                if r.id in judged:
                    continue
                out.append({"id": r.id, "kind": r.kind, "query": r.query,
                            "answer": r.answer, "steps": json.loads(r.steps_json or "[]")})
                if len(out) >= limit:
                    break
            return out
    except Exception:
        return []


def judge_summary() -> dict:
    """The auto eval suite: traces scored, verdict split, avg score, ranked failure modes, how
    many self-revised, and the judge's agreement with human labels where both exist (the eval of
    the eval — tells you whether to trust the auto-score)."""
    empty = {"judged": 0, "verdicts": {}, "avg_score": 0, "failure_counts": [],
             "revised": 0, "agreement": None, "agreement_n": 0}
    if not _ensure_ready():
        return empty
    try:
        with db_session() as session:
            jrows = session.execute(select(EvalJudgementRecord)).scalars().all()
            human = {r.run_id: r.verdict for r in session.execute(select(EvalLabelRecord)).scalars().all()}
        verdicts: dict[str, int] = {}
        fails: dict[str, int] = {}
        scores: list[int] = []
        revised = agree = overlap = 0
        for r in jrows:
            verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
            scores.append(r.score or 0)
            if r.revised:
                revised += 1
            for tag in json.loads(r.failure_modes_json or "[]"):
                fails[tag] = fails.get(tag, 0) + 1
            if r.run_id in human and r.verdict:
                overlap += 1
                if human[r.run_id] == r.verdict:
                    agree += 1
        ranked = sorted(({"tag": k, "count": v} for k, v in fails.items()),
                        key=lambda x: x["count"], reverse=True)
        return {"judged": len(jrows), "verdicts": verdicts,
                "avg_score": round(sum(scores) / len(scores)) if scores else 0,
                "failure_counts": ranked, "revised": revised,
                "agreement": (round(agree / overlap * 100) if overlap else None),
                "agreement_n": overlap}
    except Exception:
        return empty


def judgements_for_tickers(tickers: list[str]) -> dict:
    """Latest machine judgement per (ticker, kind) for a set of tickers — so the ping/activity
    feed can deep-link an event to the judge's read of the trace behind it. Keyed by (TICKER, kind)."""
    if not _ensure_ready() or not tickers:
        return {}
    ups = [t.upper() for t in tickers if t]
    if not ups:
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                select(EvalJudgementRecord).where(EvalJudgementRecord.ticker.in_(ups))
                .order_by(desc(EvalJudgementRecord.created_at))
            ).scalars().all()
        out: dict = {}
        for r in rows:
            key = (r.ticker, r.kind)
            if key in out:
                continue  # rows are newest-first, so the first per key is the latest
            out[key] = {"run_id": r.run_id, "kind": r.kind, "verdict": r.verdict, "score": r.score,
                        "failure_modes": json.loads(r.failure_modes_json or "[]"),
                        "grounding": json.loads(r.grounding_json or "[]"),
                        "rationale": r.rationale, "fix": r.fix, "revised": r.revised,
                        "created_at": r.created_at.isoformat() if r.created_at else ""}
        return out
    except Exception:
        return {}


# --- evidence store (unified, reusable sources) ----------------------------- #
def record_evidence(ticker: str, items: list[dict], kind: str = "web", engine: str = "") -> int:
    """Upsert sourced evidence for a ticker. `items` are {url, title, source?, snippet?}.
    Deduped on (ticker, url): a source seen again just refreshes last_seen/title rather
    than duplicating. Best-effort; returns how many rows were written or refreshed."""
    if not _ensure_ready() or not ticker or not items:
        return 0
    tk = ticker.upper().strip()
    n = 0
    try:
        with db_session() as session:
            for it in items:
                url = (it.get("url") or "").strip()
                if not url:
                    continue
                row = session.execute(
                    select(EvidenceItemRecord)
                    .where(EvidenceItemRecord.ticker == tk)
                    .where(EvidenceItemRecord.url == url)
                    .limit(1)
                ).scalars().first()
                if row is None:
                    row = EvidenceItemRecord(ticker=tk, url=url, first_seen=datetime.now(timezone.utc))
                    session.add(row)
                row.title = (it.get("title") or row.title or url)[:1000]
                row.source = (it.get("source") or row.source or "")[:160]
                if it.get("snippet"):
                    row.snippet = it["snippet"][:2000]
                row.kind = kind or row.kind or "web"
                row.engine = engine or row.engine or ""
                row.last_seen = datetime.now(timezone.utc)
                n += 1
    except Exception:
        return 0
    return n


def evidence_for(ticker: str, limit: int = 30) -> list[dict]:
    """All evidence gathered on a ticker, most-recently-seen first."""
    if not _ensure_ready() or not ticker:
        return []
    try:
        with db_session() as session:
            rows = session.execute(
                select(EvidenceItemRecord)
                .where(EvidenceItemRecord.ticker == ticker.upper().strip())
                .order_by(desc(EvidenceItemRecord.last_seen))
                .limit(limit)
            ).scalars().all()
            return [
                {
                    "url": r.url, "title": r.title, "source": r.source,
                    "snippet": r.snippet, "kind": r.kind, "engine": r.engine,
                    "last_seen": r.last_seen.isoformat() if r.last_seen else "",
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


# --- autonomous theme scout ------------------------------------------------- #
def upsert_autonomous_theme(key: str, name: str, score: float, confidence: float,
                            evidence: list[str], candidates: list[dict],
                            status: str = "active", source: str = "theme_scout") -> None:
    """Persist a theme Signal discovered on its own. Candidates are stored as JSON because they are
    a ranked snapshot from the scout, not user-managed entities."""
    if not _ensure_ready() or not key:
        return
    now = datetime.now(timezone.utc)
    try:
        with db_session() as session:
            row = session.get(AutonomousThemeRecord, key)
            if not row:
                row = AutonomousThemeRecord(key=key, discovered_at=now)
                session.add(row)
            row.name = name
            row.score = float(score or 0.0)
            row.confidence = float(confidence or 0.0)
            row.status = status if status in {"active", "cooling", "archived"} else "active"
            row.evidence_json = json.dumps(evidence or [])[:20000]
            row.candidates_json = json.dumps(candidates or [])[:60000]
            row.source = source
            row.updated_at = now
            row.last_seen_at = now
    except Exception:
        return


def autonomous_themes(status: str | None = None, limit: int = 20, min_score: float = 0.0) -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            stmt = select(AutonomousThemeRecord).order_by(desc(AutonomousThemeRecord.score),
                                                          desc(AutonomousThemeRecord.updated_at))
            if status:
                stmt = stmt.where(AutonomousThemeRecord.status == status)
            if min_score:
                stmt = stmt.where(AutonomousThemeRecord.score >= min_score)
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return [{
                "key": r.key,
                "name": r.name,
                "status": r.status,
                "score": r.score,
                "confidence": r.confidence,
                "evidence": json.loads(r.evidence_json or "[]"),
                "candidates": json.loads(r.candidates_json or "[]"),
                "source": r.source,
                "discovered_at": r.discovered_at.isoformat() if r.discovered_at else "",
                "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else "",
            } for r in rows]
    except Exception:
        return []


def autonomous_theme_feedback() -> dict[str, dict]:
    """Reviewed Autopilot outcomes grouped by autonomous theme attribution."""
    if not _ensure_ready():
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.status == "done")
                .where(TwinTradeReviewRecord.judged.is_(True))
                .where(TwinTradeReviewRecord.source_theme_key != "")
            ).scalars().all()
        groups: dict[str, list[TwinTradeReviewRecord]] = {}
        names: dict[str, str] = {}
        for r in rows:
            groups.setdefault(r.source_theme_key, []).append(r)
            names[r.source_theme_key] = r.source_theme_name or r.source_theme_key
        out: dict[str, dict] = {}
        for key, rs in groups.items():
            n = len(rs)
            out[key] = {
                "key": key,
                "name": names.get(key, key),
                "tested_count": n,
                "avg_return": sum(x.return_pct for x in rs) / n if n else 0.0,
                "avg_spy_alpha": sum(x.spy_alpha_pct for x in rs) / n if n else 0.0,
                "avg_sector_alpha": sum(x.sector_alpha_pct for x in rs) / n if n else 0.0,
                "win_rate": sum(1 for x in rs if x.verdict == "worked") / n * 100.0 if n else 0.0,
                "break_rate": sum(1 for x in rs if x.thesis_state == "broken") / n * 100.0 if n else 0.0,
            }
        return out
    except Exception:
        return {}


# --- the Twin (autonomous paper fund) --------------------------------------- #
def load_twin_fund() -> dict | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            r = session.get(TwinFundRecord, "default")
            if not r:
                return None
            return {"status": r.status, "cash": r.cash, "inception_value": r.inception_value,
                    "inception_at": r.inception_at.isoformat() if r.inception_at else "",
                    "mandate_statement": r.mandate_statement}
    except Exception:
        return None


def save_twin_fund(status: str, inception_value: float, cash: float,
                   mandate_statement: str = "") -> None:
    """Create/replace the fund row at inception. Sets inception_at to now."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinFundRecord, "default")
            if not r:
                r = TwinFundRecord(id="default")
                session.add(r)
            r.status = status
            r.inception_at = datetime.now(timezone.utc)
            r.inception_value = inception_value
            r.cash = cash
            r.mandate_statement = mandate_statement
            r.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


def update_twin_cash(cash: float) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinFundRecord, "default")
            if r:
                r.cash = cash
                r.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


def twin_positions() -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(select(TwinPositionRecord).order_by(TwinPositionRecord.ticker)).scalars().all()
            return [{"ticker": r.ticker, "shares": r.shares, "avg_cost": r.avg_cost,
                     "thesis": r.thesis, "horizon": r.horizon, "exit_rule": r.exit_rule,
                     "opened_at": r.opened_at.isoformat() if r.opened_at else ""} for r in rows]
    except Exception:
        return []


def get_twin_position(ticker: str) -> dict | None:
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            r = session.execute(select(TwinPositionRecord).where(
                TwinPositionRecord.ticker == ticker.upper()).limit(1)).scalars().first()
            if not r:
                return None
            return {"ticker": r.ticker, "shares": r.shares, "avg_cost": r.avg_cost,
                    "thesis": r.thesis, "horizon": r.horizon, "exit_rule": r.exit_rule}
    except Exception:
        return None


def upsert_twin_position(ticker: str, shares: float, avg_cost: float,
                         thesis: str | None = None, horizon: str | None = None,
                         exit_rule: str | None = None) -> None:
    """Set a position's shares/avg_cost (and intent fields when provided). Intent fields left as
    None keep their existing value, so a fill can update shares without wiping the thesis."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.execute(select(TwinPositionRecord).where(
                TwinPositionRecord.ticker == ticker.upper()).limit(1)).scalars().first()
            if not r:
                r = TwinPositionRecord(ticker=ticker.upper(), opened_at=datetime.now(timezone.utc))
                session.add(r)
            r.shares = shares
            r.avg_cost = avg_cost
            if thesis is not None:
                r.thesis = thesis
            if horizon is not None:
                r.horizon = horizon
            if exit_rule is not None:
                r.exit_rule = exit_rule
            r.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


def set_twin_intent(ticker: str, thesis: str | None = None, horizon: str | None = None,
                    exit_rule: str | None = None) -> None:
    """Update only the Twin's intent fields on a position it already holds (no-op otherwise) — used
    when the decision cycle re-judges a name without changing its share count."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.execute(select(TwinPositionRecord).where(
                TwinPositionRecord.ticker == ticker.upper()).limit(1)).scalars().first()
            if not r:
                return
            if thesis is not None:
                r.thesis = thesis
            if horizon is not None:
                r.horizon = horizon
            if exit_rule is not None:
                r.exit_rule = exit_rule
            r.updated_at = datetime.now(timezone.utc)
    except Exception:
        return


def delete_twin_position(ticker: str) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.execute(select(TwinPositionRecord).where(
                TwinPositionRecord.ticker == ticker.upper()).limit(1)).scalars().first()
            if r:
                session.delete(r)
    except Exception:
        return


def add_twin_trade(ticker: str, action: str, shares: float, reasoning: str = "",
                   conviction: int = 0, status: str = "pending", usd: float = 0.0,
                   tactic: str = "", horizon: str = "", thesis: str = "",
                   exit_rule: str = "", review_after_days: int = 7,
                   critic_note: str = "", source_theme_key: str = "",
                   source_theme_name: str = "", plan_step: int = 0,
                   depends_on: list[str] | None = None,
                   decision_price: float = 0.0, market_regime: str = "") -> int:
    """Queue a trade. A decision-cycle order is sized in dollars (`usd`, stashed in `value` until
    fill); a direct share order passes `shares` with usd=0. Shares are (re)computed at fill price."""
    if not _ensure_ready():
        return 0
    try:
        with db_session() as session:
            r = TwinTradeRecord(ticker=ticker.upper(), action=action, shares=shares, value=usd,
                                decision_price=decision_price,
                                reasoning=reasoning[:4000], conviction=conviction, status=status,
                                critic_note=critic_note[:4000],
                                tactic=tactic[:60], source_theme_key=source_theme_key[:80],
                                source_theme_name=source_theme_name[:4000],
                                market_regime=market_regime[:40],
                                plan_step=int(plan_step or 0),
                                depends_on_json=json.dumps(depends_on or [])[:4000],
                                horizon=horizon[:80], thesis=thesis[:4000],
                                exit_rule=exit_rule[:4000], review_after_days=review_after_days,
                                decided_at=datetime.now(timezone.utc))
            session.add(r)
            session.flush()
            return r.id
    except Exception:
        return 0


def pending_twin_trades() -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(select(TwinTradeRecord).where(TwinTradeRecord.status == "pending")
                                   .order_by(TwinTradeRecord.decided_at, TwinTradeRecord.id)).scalars().all()
            return [{"id": r.id, "ticker": r.ticker, "action": r.action, "shares": r.shares,
                     "value": r.value, "decision_price": r.decision_price,
                     "reasoning": r.reasoning, "conviction": r.conviction,
                     "critic_note": r.critic_note, "preflight_note": r.preflight_note,
                     "tactic": r.tactic, "source_theme_key": r.source_theme_key,
                     "source_theme_name": r.source_theme_name,
                     "market_regime": r.market_regime,
                     "plan_step": r.plan_step, "depends_on": json.loads(r.depends_on_json or "[]"),
                     "horizon": r.horizon, "thesis": r.thesis,
                     "exit_rule": r.exit_rule, "review_after_days": r.review_after_days,
                     "status": r.status,
                     "decided_at": r.decided_at.isoformat() if r.decided_at else ""} for r in rows]
    except Exception:
        return []


def cancel_twin_trades(trade_ids: list[int], reason: str | dict[int, str] = "") -> int:
    """Mark queued Twin orders canceled. Used when an off-hours decision is superseded by a newer
    queued batch, so the next market open cannot fill stale duplicate orders."""
    if not _ensure_ready() or not trade_ids:
        return 0
    try:
        with db_session() as session:
            rows = session.execute(select(TwinTradeRecord).where(TwinTradeRecord.id.in_(trade_ids))).scalars().all()
            n = 0
            for r in rows:
                if r.status == "pending":
                    r.status = "canceled"
                    note = reason.get(r.id, "") if isinstance(reason, dict) else reason
                    if note:
                        r.preflight_note = note[:4000]
                    n += 1
            return n
    except Exception:
        return 0


def resize_twin_trade(trade_id: int, usd: float, note: str = "") -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinTradeRecord, trade_id)
            if not r or r.status != "pending":
                return
            r.value = max(0.0, float(usd or 0.0))
            if note:
                r.preflight_note = note[:4000]
    except Exception:
        return


def fill_twin_trade(trade_id: int, price: float, value: float, shares: float | None = None,
                    bench_entry_price: float = 0.0) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinTradeRecord, trade_id)
            if r:
                r.status = "filled"
                r.price = price
                r.value = value
                if shares is not None:
                    r.shares = shares   # the actual filled share count (dollar orders start at 0)
                if bench_entry_price > 0:
                    r.bench_entry_price = bench_entry_price
                    r.bench_last_price = bench_entry_price
                r.filled_at = datetime.now(timezone.utc)
    except Exception:
        return


def recent_twin_trades(limit: int = 60) -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(select(TwinTradeRecord).order_by(
                desc(TwinTradeRecord.decided_at), desc(TwinTradeRecord.id)).limit(limit)).scalars().all()
            return [{"id": r.id, "ticker": r.ticker, "action": r.action, "shares": r.shares,
                     "price": r.price, "decision_price": r.decision_price, "value": r.value,
                     "reasoning": r.reasoning,
                     "conviction": r.conviction, "critic_note": r.critic_note,
                     "preflight_note": r.preflight_note,
                     "tactic": r.tactic, "source_theme_key": r.source_theme_key,
                     "source_theme_name": r.source_theme_name,
                     "market_regime": r.market_regime,
                     "plan_step": r.plan_step, "depends_on": json.loads(r.depends_on_json or "[]"),
                     "horizon": r.horizon,
                     "thesis": r.thesis, "exit_rule": r.exit_rule,
                     "review_after_days": r.review_after_days,
                     "bench_entry_price": r.bench_entry_price,
                     "bench_last_price": r.bench_last_price,
                     "review_status": r.review_status,
                     "review_return_pct": r.review_return_pct,
                     "review_alpha_pct": r.review_alpha_pct,
                     "review_note": r.review_note,
                     "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else "",
                     "status": r.status,
                     "decided_at": r.decided_at.isoformat() if r.decided_at else "",
                     "filled_at": r.filled_at.isoformat() if r.filled_at else ""} for r in rows]
    except Exception:
        return []


def due_twin_review_trades(limit: int = 50) -> list[dict]:
    """Filled Twin trades whose configured review window has elapsed."""
    if not _ensure_ready():
        return []
    now = datetime.now(timezone.utc)
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeRecord)
                .where(TwinTradeRecord.status == "filled")
                .where(TwinTradeRecord.review_status != "reviewed")
                .order_by(TwinTradeRecord.filled_at, TwinTradeRecord.id)
                .limit(limit)
            ).scalars().all()
            out = []
            for r in rows:
                if not r.filled_at:
                    continue
                filled = r.filled_at if r.filled_at.tzinfo else r.filled_at.replace(tzinfo=timezone.utc)
                if now - filled < timedelta(days=max(1, int(r.review_after_days or 7))):
                    continue
                out.append({"id": r.id, "ticker": r.ticker, "action": r.action, "shares": r.shares,
                            "price": r.price, "value": r.value, "reasoning": r.reasoning,
                            "conviction": r.conviction, "tactic": r.tactic,
                            "source_theme_key": r.source_theme_key,
                            "source_theme_name": r.source_theme_name,
                            "market_regime": r.market_regime, "horizon": r.horizon,
                            "review_after_days": r.review_after_days,
                            "bench_entry_price": r.bench_entry_price,
                            "filled_at": r.filled_at.isoformat() if r.filled_at else ""})
            return out
    except Exception:
        return []


def save_twin_review(trade_id: int, last_price: float, bench_last_price: float,
                     return_pct: float, alpha_pct: float, note: str) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinTradeRecord, trade_id)
            if not r:
                return
            r.bench_last_price = bench_last_price
            r.review_return_pct = return_pct
            r.review_alpha_pct = alpha_pct
            r.review_note = note[:4000]
            r.review_status = "reviewed"
            r.reviewed_at = datetime.now(timezone.utc)
    except Exception:
        return


def schedule_twin_reviews(trade_id: int, ticker: str, action: str, tactic: str, horizon: str,
                          entry_price: float, bench_entry: float, sector_symbol: str,
                          sector_entry: float, windows: list, source_theme_key: str = "",
                          source_theme_name: str = "", market_regime: str = "") -> None:
    """Queue the horizon-appropriate evaluation windows for a just-filled trade.
    `windows` is a list of (window, days, judged)."""
    if not _ensure_ready() or not windows:
        return
    try:
        now = datetime.now(timezone.utc)
        with db_session() as session:
            for window, days, judged in windows:
                session.add(TwinTradeReviewRecord(
                    trade_id=trade_id, ticker=ticker.upper(), action=action, tactic=tactic or "",
                    source_theme_key=source_theme_key or "", source_theme_name=source_theme_name or "",
                    market_regime=market_regime or "",
                    horizon=horizon or "", window=window, judged=bool(judged),
                    due_at=now + timedelta(days=int(days)), status="pending",
                    entry_price=entry_price, bench_entry=bench_entry,
                    sector_symbol=sector_symbol or "", sector_entry=sector_entry, created_at=now))
    except Exception:
        return


def due_twin_reviews(limit: int = 40) -> list[dict]:
    """Pending review windows whose due date has passed."""
    if not _ensure_ready():
        return []
    try:
        now = datetime.now(timezone.utc)
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.status == "pending")
                .where(TwinTradeReviewRecord.due_at <= now)
                .order_by(TwinTradeReviewRecord.due_at).limit(limit)).scalars().all()
            return [{"id": r.id, "trade_id": r.trade_id, "ticker": r.ticker, "action": r.action,
                     "tactic": r.tactic, "source_theme_key": r.source_theme_key,
                     "source_theme_name": r.source_theme_name,
                     "market_regime": r.market_regime,
                     "horizon": r.horizon, "window": r.window, "judged": r.judged,
                     "entry_price": r.entry_price, "bench_entry": r.bench_entry,
                     "sector_symbol": r.sector_symbol, "sector_entry": r.sector_entry} for r in rows]
    except Exception:
        return []


def save_twin_review_window(review_id: int, price: float, bench_last: float, sector_last: float,
                            return_pct: float, spy_alpha_pct: float, sector_alpha_pct: float,
                            drawdown_pct: float, thesis_state: str, verdict: str, note: str) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinTradeReviewRecord, review_id)
            if not r:
                return
            r.price = price
            r.bench_last = bench_last
            r.sector_last = sector_last
            r.return_pct = return_pct
            r.spy_alpha_pct = spy_alpha_pct
            r.sector_alpha_pct = sector_alpha_pct
            r.drawdown_pct = drawdown_pct
            r.thesis_state = thesis_state or ""
            r.verdict = verdict or ""
            r.note = (note or "")[:4000]
            r.status = "done"
            r.reviewed_at = datetime.now(timezone.utc)
    except Exception:
        return


def twin_reviews_for_trades(trade_ids: list[int]) -> dict:
    """Review windows per trade id (newest-window last) — for the History detail."""
    if not _ensure_ready() or not trade_ids:
        return {}
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.trade_id.in_(list(trade_ids)))
                .order_by(TwinTradeReviewRecord.trade_id, TwinTradeReviewRecord.due_at)).scalars().all()
        out: dict = {}
        for r in rows:
            out.setdefault(r.trade_id, []).append({
                "window": r.window, "judged": r.judged, "status": r.status,
                "return_pct": r.return_pct, "spy_alpha_pct": r.spy_alpha_pct,
                "sector_alpha_pct": r.sector_alpha_pct, "drawdown_pct": r.drawdown_pct,
                "thesis_state": r.thesis_state, "verdict": r.verdict, "note": r.note,
                "sector_symbol": r.sector_symbol, "source_theme_key": r.source_theme_key,
                "source_theme_name": r.source_theme_name,
                "market_regime": r.market_regime,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else ""})
        return out
    except Exception:
        return {}


def twin_window_policy() -> list[dict]:
    """Done, JUDGED review windows aggregated per tactic — the mature policy memory."""
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.status == "done")
                .where(TwinTradeReviewRecord.judged.is_(True))).scalars().all()
        groups: dict = {}
        for r in rows:
            groups.setdefault(r.tactic or r.action or "trade", []).append(r)
        out = []
        for tactic, rs in groups.items():
            n = len(rs)
            if not n:
                continue
            out.append({
                "tactic": tactic, "count": n,
                "avg_spy_alpha": sum(x.spy_alpha_pct for x in rs) / n,
                "avg_sector_alpha": sum(x.sector_alpha_pct for x in rs) / n,
                "win_rate": sum(1 for x in rs if x.verdict == "worked") / n * 100.0,
                "break_rate": sum(1 for x in rs if x.thesis_state == "broken") / n * 100.0,
            })
        return out
    except Exception:
        return []


def twin_contextual_bandit() -> dict:
    """Contextual bandit priors from judged Autopilot reviews.

    Rewards are not simulated. They come from the authoritative review-window ledger:
    sector/SPY alpha, plus thesis-state penalties/bonuses. The policy groups outcomes by tactic,
    sector, autonomous theme, market regime, and combined contexts so Autopilot can learn that a
    tactic works in one context but not another.
    """
    empty = {"arms": [], "by_key": {}, "top": [], "bottom": []}
    if not _ensure_ready():
        return empty
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.status == "done")
                .where(TwinTradeReviewRecord.judged.is_(True))
            ).scalars().all()
        if not rows:
            return empty

        def reward(r: TwinTradeReviewRecord) -> float:
            base = r.sector_alpha_pct if r.sector_symbol else r.spy_alpha_pct
            if r.verdict == "worked":
                base += 1.0
            elif r.verdict in {"lagged", "weak"}:
                base -= 1.0
            elif r.verdict == "failed":
                base -= 4.0
            if r.thesis_state == "stronger":
                base += 2.0
            elif r.thesis_state == "weakening":
                base -= 2.0
            elif r.thesis_state == "broken":
                base -= 5.0
            return base

        groups: dict[str, dict] = {}

        def add(kind: str, key: str, label: str, r: TwinTradeReviewRecord) -> None:
            if not key:
                return
            full_key = f"{kind}:{key}"
            g = groups.setdefault(full_key, {"kind": kind, "key": key, "label": label, "rows": []})
            g["rows"].append(r)

        for r in rows:
            tactic = r.tactic or r.action or "trade"
            sector = r.sector_symbol or "market"
            theme = r.source_theme_key or ""
            regime = r.market_regime or "unknown"
            add("tactic", tactic, tactic, r)
            add("sector", sector, sector, r)
            add("regime", regime, regime, r)
            add("tactic_sector", f"{tactic}|{sector}", f"{tactic} in {sector}", r)
            add("tactic_regime", f"{tactic}|{regime}", f"{tactic} during {regime}", r)
            add("sector_regime", f"{sector}|{regime}", f"{sector} during {regime}", r)
            if theme:
                add("theme", theme, r.source_theme_name or theme, r)
                add("tactic_theme", f"{tactic}|{theme}", f"{tactic} in {r.source_theme_name or theme}", r)
                add("theme_regime", f"{theme}|{regime}", f"{r.source_theme_name or theme} during {regime}", r)

        arms = []
        for full_key, g in groups.items():
            rs = g.pop("rows")
            n = len(rs)
            rewards = [reward(r) for r in rs]
            avg_reward = sum(rewards) / n if n else 0.0
            wins = sum(1 for r in rs if r.verdict == "worked")
            breaks = sum(1 for r in rs if r.thesis_state == "broken")
            weak = sum(1 for r in rs if r.thesis_state == "weakening")
            confidence = min(0.95, n / (n + 4.0))
            if n < 2:
                stance = "explore"
            elif avg_reward > 2 and confidence >= 0.33 and breaks / n < 0.34:
                stance = "lean_in"
            elif avg_reward < -3 or breaks / n >= 0.34:
                stance = "avoid"
            elif avg_reward < -1:
                stance = "size_down"
            else:
                stance = "neutral"
            arms.append({
                **g,
                "id": full_key,
                "count": n,
                "avg_reward": avg_reward,
                "avg_return": sum(r.return_pct for r in rs) / n if n else 0.0,
                "avg_spy_alpha": sum(r.spy_alpha_pct for r in rs) / n if n else 0.0,
                "avg_sector_alpha": sum(r.sector_alpha_pct for r in rs) / n if n else 0.0,
                "win_rate": wins / n * 100.0 if n else 0.0,
                "break_rate": breaks / n * 100.0 if n else 0.0,
                "weak_rate": weak / n * 100.0 if n else 0.0,
                "confidence": confidence,
                "stance": stance,
            })
        arms.sort(key=lambda x: (x["confidence"], x["avg_reward"], x["count"]), reverse=True)
        by_key = {a["id"]: a for a in arms}
        top = sorted([a for a in arms if a["count"] >= 2], key=lambda x: x["avg_reward"], reverse=True)[:8]
        bottom = sorted([a for a in arms if a["count"] >= 2], key=lambda x: x["avg_reward"])[:8]
        return {"arms": arms, "by_key": by_key, "top": top, "bottom": bottom}
    except Exception:
        return empty


def twin_lesson_book() -> dict:
    """Aggregated Autopilot lessons from the authoritative review-window ledger."""
    if not _ensure_ready():
        return {"tactics": [], "sectors": [], "themes": [], "recent": []}
    try:
        with db_session() as session:
            rows = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.status == "done")
                .order_by(desc(TwinTradeReviewRecord.reviewed_at))
            ).scalars().all()

        judged = [r for r in rows if r.judged]

        def pack_group(key: str, rs: list[TwinTradeReviewRecord]) -> dict:
            n = len(rs)
            worked = sum(1 for x in rs if x.verdict == "worked")
            broken = sum(1 for x in rs if x.thesis_state == "broken")
            weak = sum(1 for x in rs if x.thesis_state == "weakening")
            normal = sum(1 for x in rs if x.verdict in ("intact", "monitor") or
                         (x.return_pct < 0 and x.sector_alpha_pct >= -2 and x.thesis_state == "active"))
            return {
                "key": key,
                "count": n,
                "avg_return": sum(x.return_pct for x in rs) / n if n else 0.0,
                "avg_spy_alpha": sum(x.spy_alpha_pct for x in rs) / n if n else 0.0,
                "avg_sector_alpha": sum(x.sector_alpha_pct for x in rs) / n if n else 0.0,
                "win_rate": worked / n * 100.0 if n else 0.0,
                "break_rate": broken / n * 100.0 if n else 0.0,
                "weak_count": weak,
                "normal_drawdowns": normal,
            }

        tactic_groups: dict[str, list[TwinTradeReviewRecord]] = {}
        sector_groups: dict[str, list[TwinTradeReviewRecord]] = {}
        context_groups: dict[tuple[str, str], list[TwinTradeReviewRecord]] = {}
        for r in judged:
            tactic = r.tactic or r.action or "trade"
            sector = r.sector_symbol or "market"
            tactic_groups.setdefault(tactic, []).append(r)
            sector_groups.setdefault(sector, []).append(r)
            context_groups.setdefault((tactic, sector), []).append(r)

        tactics = [pack_group(k, v) for k, v in tactic_groups.items()]
        tactics.sort(key=lambda x: (x["count"], x["avg_sector_alpha"]), reverse=True)

        sectors = []
        for sector, rs in sector_groups.items():
            row = pack_group(sector, rs)
            best = None
            for (tactic, sec), crs in context_groups.items():
                if sec != sector or not crs:
                    continue
                cand = pack_group(tactic, crs)
                if best is None or (cand["avg_sector_alpha"], cand["count"]) > (best["avg_sector_alpha"], best["count"]):
                    best = cand
            row["best_tactic"] = best["key"] if best else ""
            sectors.append(row)
        sectors.sort(key=lambda x: (x["count"], x["avg_sector_alpha"]), reverse=True)

        recent = [{
            "ticker": r.ticker, "action": r.action, "tactic": r.tactic, "horizon": r.horizon,
            "window": r.window, "judged": r.judged, "sector_symbol": r.sector_symbol,
            "return_pct": r.return_pct, "spy_alpha_pct": r.spy_alpha_pct,
            "sector_alpha_pct": r.sector_alpha_pct, "drawdown_pct": r.drawdown_pct,
            "thesis_state": r.thesis_state, "verdict": r.verdict, "note": r.note,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else "",
        } for r in rows[:12]]

        feedback = autonomous_theme_feedback()
        themes = []
        for th in autonomous_themes(status="active", limit=8, min_score=45.0):
            fb = feedback.get(th["key"], {})
            stance = "testing"
            if fb.get("tested_count", 0) >= 2:
                if fb.get("break_rate", 0) >= 34:
                    stance = "back off"
                elif fb.get("avg_sector_alpha", 0) > 1 and fb.get("win_rate", 0) >= 50:
                    stance = "lean in"
                elif fb.get("avg_sector_alpha", 0) < -1:
                    stance = "cooling"
                else:
                    stance = "mixed"
            themes.append({**th, **fb, "stance": stance})

        return {"tactics": tactics, "sectors": sectors, "themes": themes, "recent": recent}
    except Exception:
        return {"tactics": [], "sectors": [], "themes": [], "recent": []}


def latest_twin_review(ticker: str) -> dict | None:
    """Most recent done review window for a ticker — feeds the held-position health read."""
    if not _ensure_ready():
        return None
    try:
        with db_session() as session:
            r = session.execute(
                select(TwinTradeReviewRecord)
                .where(TwinTradeReviewRecord.ticker == ticker.upper())
                .where(TwinTradeReviewRecord.status == "done")
                .order_by(desc(TwinTradeReviewRecord.reviewed_at)).limit(1)).scalars().first()
            if not r:
                return None
            return {"window": r.window, "return_pct": r.return_pct,
                    "sector_alpha_pct": r.sector_alpha_pct, "drawdown_pct": r.drawdown_pct,
                    "thesis_state": r.thesis_state, "verdict": r.verdict, "note": r.note,
                    "sector_symbol": r.sector_symbol}
    except Exception:
        return None


def add_twin_equity_point(value: float, cash: float, positions_value: float) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            session.add(TwinEquityRecord(value=value, cash=cash, positions_value=positions_value,
                                         at=datetime.now(timezone.utc)))
    except Exception:
        return


def twin_equity_curve(limit: int = 400) -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(select(TwinEquityRecord).order_by(
                desc(TwinEquityRecord.at)).limit(limit)).scalars().all()
            rows = list(reversed(rows))   # oldest -> newest for charting
            return [{"at": r.at.isoformat() if r.at else "", "value": r.value,
                     "cash": r.cash, "positions_value": r.positions_value} for r in rows]
    except Exception:
        return []


def real_equity_curve(source: str, since_iso: str | None = None, limit: int = 400) -> list[dict]:
    """The real account's value over time (from portfolio_snapshots) — the 'you' line in the
    You-vs-Autopilot race. Filtered to since inception so both lines share a start."""
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            stmt = select(PortfolioSnapshot)
            if source:
                stmt = stmt.where(PortfolioSnapshot.source == source)
            if since_iso:
                try:
                    stmt = stmt.where(PortfolioSnapshot.captured_at >= datetime.fromisoformat(since_iso))
                except Exception:
                    pass
            rows = session.execute(stmt.order_by(desc(PortfolioSnapshot.captured_at)).limit(limit)).scalars().all()
            rows = list(reversed(rows))   # oldest -> newest for charting
            return [{"at": r.captured_at.isoformat() if r.captured_at else "", "value": r.total_value}
                    for r in rows]
    except Exception:
        return []


def reset_twin() -> None:
    """Wipe the whole Twin (fund + positions + trades + equity) — for a clean re-inception."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            for model in (TwinTradeReviewRecord, TwinEquityRecord, TwinTradeRecord,
                          TwinPositionRecord, TwinFundRecord):
                session.execute(delete(model))
    except Exception:
        return
