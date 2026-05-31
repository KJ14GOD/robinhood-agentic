from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    total_value: Mapped[float] = mapped_column(Float, default=0.0)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    buying_power: Mapped[float] = mapped_column(Float, default=0.0)
    reported_equity: Mapped[float] = mapped_column(Float, default=0.0)
    pricing_source: Mapped[str] = mapped_column(Text, default="")
    pricing_warning: Mapped[str] = mapped_column(Text, default="")
    sync_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_message: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    positions: Mapped[list["PositionSnapshot"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)

    snapshot: Mapped[PortfolioSnapshot] = relationship(back_populates="positions")


class ThesisRecord(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    thesis: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    strengthens_json: Mapped[str] = mapped_column(Text, default="[]")
    weakens_json: Mapped[str] = mapped_column(Text, default="[]")
    invalidation: Mapped[str] = mapped_column(Text, default="")
    last_decision: Mapped[str] = mapped_column(String(40), default="WATCHLIST", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistItemRecord(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(40), default="balanced", index=True)
    target_entry: Mapped[float] = mapped_column(Float, default=0.0)
    max_allocation_pct: Mapped[float] = mapped_column(Float, default=0.0)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BriefingRecord(Base):
    __tablename__ = "briefings"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="manual", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    bullets_json: Mapped[str] = mapped_column(Text, default="[]")
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ResearchEventRecord(Base):
    __tablename__ = "research_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), default="", index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(40), default="info", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TickerResearchRecord(Base):
    __tablename__ = "ticker_research"

    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    action_label: Mapped[str] = mapped_column(String(40), default="WATCHLIST", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    thesis: Mapped[str] = mapped_column(Text, default="")
    bull_case: Mapped[str] = mapped_column(Text, default="")
    bear_case: Mapped[str] = mapped_column(Text, default="")
    risks: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ShadowTradeRecord(Base):
    """A logged recommendation, marked to market over time. This is the raw
    material of the evaluation layer — every field exists so a recommendation can
    later be graded by conviction, action label, source engine, risk mode, the
    signals that justified it, and performance versus market/sector benchmarks.

    Benchmark and sector-ETF anchor prices are captured AT ENTRY on purpose: they
    cannot be reconstructed after the fact, so without them benchmark-relative
    scoring of past recommendations is impossible."""

    __tablename__ = "shadow_trades"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    action: Mapped[str] = mapped_column(String(20), default="buy")
    decision_label: Mapped[str] = mapped_column(String(40), default="WATCHLIST", index=True)
    conviction: Mapped[int] = mapped_column(Integer, default=0)
    risk_mode: Mapped[str] = mapped_column(String(40), default="", index=True)
    flavor: Mapped[str] = mapped_column(String(40), default="")
    sector: Mapped[str] = mapped_column(String(80), default="")
    thesis: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(60), default="analyst", index=True)

    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    entry_signals_json: Mapped[str] = mapped_column(Text, default="{}")

    # Benchmark anchors captured at entry (market + the stock's sector ETF).
    bench_symbol: Mapped[str] = mapped_column(String(20), default="SPY")
    bench_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    sector_etf: Mapped[str] = mapped_column(String(20), default="")
    sector_etf_entry_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Outcome, filled in by mark-to-market.
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    bench_last_price: Mapped[float] = mapped_column(Float, default=0.0)
    sector_etf_last_price: Mapped[float] = mapped_column(Float, default=0.0)

    closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    close_reason: Mapped[str] = mapped_column(String(120), default="")
    user_executed: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)


class AgentRunRecord(Base):
    """Audit trail for an agent loop (chat, deep research, mission run, briefing).

    Persisting the trace is what lets the system answer 'why did the brain say
    that?' after the fact, and is where deep-research mode will write its plan,
    sources, and self-critique."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="chat", index=True)
    query: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    tools_used: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MissionRecord(Base):
    """A standing research mission. The brain keeps its roster of candidates
    current and re-labels them on a gated cadence, reporting changes on its own."""

    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    theme: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(20), default="any")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    candidates: Mapped[list["MissionCandidateRecord"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MissionCandidateRecord(Base):
    __tablename__ = "mission_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    label: Mapped[str] = mapped_column(String(20), default="WATCH")
    conviction: Mapped[int] = mapped_column(Integer, default=5)
    reason: Mapped[str] = mapped_column(Text, default="")
    sector: Mapped[str] = mapped_column(String(80), default="")
    signals_json: Mapped[str] = mapped_column(Text, default="{}")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    mission: Mapped[MissionRecord] = relationship(back_populates="candidates")


Index("ix_position_snapshots_ticker_snapshot", PositionSnapshot.ticker, PositionSnapshot.snapshot_id)
Index("ix_research_events_ticker_created", ResearchEventRecord.ticker, ResearchEventRecord.created_at)
Index("ix_shadow_trades_source_closed", ShadowTradeRecord.source, ShadowTradeRecord.closed)
Index("ix_mission_candidates_mission_ticker", MissionCandidateRecord.mission_id, MissionCandidateRecord.ticker)
