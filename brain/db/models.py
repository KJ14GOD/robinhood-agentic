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


class EvidenceItemRecord(Base):
    """A single piece of sourced evidence the brain gathered on a ticker — one row per
    source (web article, catalyst, filing). Deduped by (ticker, url) so the same source
    surfaced by different engines collapses to one row, with last_seen refreshed. This
    is the unified, reusable evidence store: instead of citations being trapped inside
    each run's JSON, the whole app can answer 'what does the brain know about X, and from
    where' from one place."""

    __tablename__ = "evidence_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), default="", index=True)
    url: Mapped[str] = mapped_column(String(700), default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(160), default="")   # publisher / domain
    snippet: Mapped[str] = mapped_column(Text, default="")          # optional excerpt/summary
    kind: Mapped[str] = mapped_column(String(40), default="web", index=True)   # web | catalyst | filing
    engine: Mapped[str] = mapped_column(String(40), default="", index=True)    # analyst | rejudge | catalysts | deep_research
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (Index("ix_evidence_ticker_url", "ticker", "url"),)


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


class MandateRecord(Base):
    """The user's standing investing mandate (single row). Stored in the DB so it persists
    in production like everything else — it's the agent's standing instruction."""

    __tablename__ = "mandates"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="default")
    statement: Mapped[str] = mapped_column(Text, default="")
    horizon: Mapped[str] = mapped_column(String(60), default="")
    risk: Mapped[str] = mapped_column(String(60), default="")
    style: Mapped[str] = mapped_column(String(60), default="")
    favor_json: Mapped[str] = mapped_column(Text, default="[]")
    avoid_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MandatePlanStateRecord(Base):
    """Durable baseline for drift detection: the holdings/weights signature as of the last mandate
    plan we sent. Lets the drift-triggered plan fire when the book moves materially off its
    last-planned shape — and survive restarts (an in-memory baseline would silently re-baseline and
    miss real drift). Single row (id='default'). Additive table — create_all picks it up, no ALTER."""

    __tablename__ = "mandate_plan_state"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="default")
    signature_json: Mapped[str] = mapped_column(Text, default="[]")   # [[ticker, weight_pct], ...]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalLabelRecord(Base):
    """A human error-analysis label on one brain trace (an agent_run). This is the raw
    material of the eval suite: reading real outputs and writing down what failed builds
    a domain-specific failure taxonomy that generic benchmarks can't capture. One label
    per run (upsert by run_id) — the latest judgement wins."""

    __tablename__ = "eval_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)   # the agent_run being judged
    kind: Mapped[str] = mapped_column(String(40), default="", index=True)   # analyst | rejudge | deep_research...
    ticker: Mapped[str] = mapped_column(String(20), default="", index=True)
    verdict: Mapped[str] = mapped_column(String(20), default="", index=True)  # good | mixed | flawed
    failure_modes_json: Mapped[str] = mapped_column(Text, default="[]")       # taxonomy tags
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EvalJudgementRecord(Base):
    """The LLM-as-judge's automatic score for one trace (an agent_run) — Phase 2 of the eval
    layer. Kept in a SEPARATE table from the human `eval_labels` (the ground truth) so the two
    never collide: create_all picks this up with no migration, and judge-vs-human agreement is
    computable where both exist. One judgement per run (upsert by run_id) — the latest wins."""

    __tablename__ = "eval_judgements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True, unique=True)  # the agent_run judged
    kind: Mapped[str] = mapped_column(String(40), default="", index=True)     # analyst | rejudge | deep_research...
    ticker: Mapped[str] = mapped_column(String(20), default="", index=True)
    verdict: Mapped[str] = mapped_column(String(20), default="", index=True)  # good | mixed | flawed
    score: Mapped[int] = mapped_column(Integer, default=0)                    # 0-100 quality
    failure_modes_json: Mapped[str] = mapped_column(Text, default="[]")       # taxonomy tags the judge flagged
    grounding_json: Mapped[str] = mapped_column(Text, default="[]")           # per-claim grounding checks
    rationale: Mapped[str] = mapped_column(Text, default="")
    fix: Mapped[str] = mapped_column(Text, default="")
    revised: Mapped[bool] = mapped_column(Boolean, default=False)             # did the call self-revise before shipping?
    model: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ChatMessageRecord(Base):
    """One turn of the Home conversation (user or assistant), persisted so the
    chat-first home survives reloads. Only the final text is stored — the full
    tool trace of each answer already lives in agent_runs."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
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


# --------------------------------------------------------------------------- #
# The Twin — an autonomous paper fund cloned once from the real account, that
# then manages itself so the user can race it against their real performance.
# All additive tables (create_all picks them up; no migration).
# --------------------------------------------------------------------------- #
class TwinFundRecord(Base):
    """The Twin fund itself — single row (id='default'). Cloned from the real book at inception
    (fixed capital: this cash + the positions are all it ever gets), then runs autonomously."""

    __tablename__ = "twin_fund"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="default")
    status: Mapped[str] = mapped_column(String(20), default="", index=True)   # "" unset | running | paused
    inception_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    inception_value: Mapped[float] = mapped_column(Float, default=0.0)         # $ value at the clone
    cash: Mapped[float] = mapped_column(Float, default=0.0)                    # uninvested cash on hand
    mandate_statement: Mapped[str] = mapped_column(Text, default="")          # the plan it was launched to pursue
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TwinPositionRecord(Base):
    """One open Twin holding — shares PLUS the Twin's own intent on it (it authors and revises the
    thesis/horizon/exit itself; that's what makes the holding a decision, not just a number)."""

    __tablename__ = "twin_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    thesis: Mapped[str] = mapped_column(Text, default="")        # why the Twin holds it
    horizon: Mapped[str] = mapped_column(String(60), default="")  # its intended hold (free text: "core", "swing"...)
    exit_rule: Mapped[str] = mapped_column(Text, default="")     # what would make it sell
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TwinTradeRecord(Base):
    """One Twin decision. Queued off-hours (status 'pending'), filled at the next open at the live
    price (status 'filled'). The reasoning is stored so each trade is auditable + judge-scorable."""

    __tablename__ = "twin_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    action: Mapped[str] = mapped_column(String(10), default="")   # buy | add | trim | sell
    shares: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)       # fill price (0 until filled)
    value: Mapped[float] = mapped_column(Float, default=0.0)       # shares * fill price
    reasoning: Mapped[str] = mapped_column(Text, default="")
    conviction: Mapped[int] = mapped_column(Integer, default=0)
    critic_note: Mapped[str] = mapped_column(Text, default="")
    tactic: Mapped[str] = mapped_column(String(60), default="", index=True)
    horizon: Mapped[str] = mapped_column(String(80), default="")
    thesis: Mapped[str] = mapped_column(Text, default="")
    exit_rule: Mapped[str] = mapped_column(Text, default="")
    review_after_days: Mapped[int] = mapped_column(Integer, default=7)
    bench_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    bench_last_price: Mapped[float] = mapped_column(Float, default=0.0)
    review_status: Mapped[str] = mapped_column(String(20), default="", index=True)
    review_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    review_alpha_pct: Mapped[float] = mapped_column(Float, default=0.0)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | filled | canceled
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TwinEquityRecord(Base):
    """A point on the Twin's equity curve — the line you race your real account against."""

    __tablename__ = "twin_equity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)            # cash + positions marked to market
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    positions_value: Mapped[float] = mapped_column(Float, default=0.0)
