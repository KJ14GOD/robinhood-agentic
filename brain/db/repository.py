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
                   conviction: int = 0, status: str = "pending") -> int:
    if not _ensure_ready():
        return 0
    try:
        with db_session() as session:
            r = TwinTradeRecord(ticker=ticker.upper(), action=action, shares=shares,
                                reasoning=reasoning[:4000], conviction=conviction, status=status,
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
            rows = session.execute(select(TwinTradeRecord).where(
                TwinTradeRecord.status == "pending").order_by(TwinTradeRecord.decided_at)).scalars().all()
            return [{"id": r.id, "ticker": r.ticker, "action": r.action, "shares": r.shares,
                     "reasoning": r.reasoning, "conviction": r.conviction} for r in rows]
    except Exception:
        return []


def fill_twin_trade(trade_id: int, price: float, value: float) -> None:
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            r = session.get(TwinTradeRecord, trade_id)
            if r:
                r.status = "filled"
                r.price = price
                r.value = value
                r.filled_at = datetime.now(timezone.utc)
    except Exception:
        return


def recent_twin_trades(limit: int = 60) -> list[dict]:
    if not _ensure_ready():
        return []
    try:
        with db_session() as session:
            rows = session.execute(select(TwinTradeRecord).order_by(
                desc(TwinTradeRecord.decided_at)).limit(limit)).scalars().all()
            return [{"id": r.id, "ticker": r.ticker, "action": r.action, "shares": r.shares,
                     "price": r.price, "value": r.value, "reasoning": r.reasoning,
                     "conviction": r.conviction, "status": r.status,
                     "decided_at": r.decided_at.isoformat() if r.decided_at else "",
                     "filled_at": r.filled_at.isoformat() if r.filled_at else ""} for r in rows]
    except Exception:
        return []


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


def reset_twin() -> None:
    """Wipe the whole Twin (fund + positions + trades + equity) — for a clean re-inception."""
    if not _ensure_ready():
        return
    try:
        with db_session() as session:
            for model in (TwinEquityRecord, TwinTradeRecord, TwinPositionRecord, TwinFundRecord):
                session.execute(delete(model))
    except Exception:
        return
