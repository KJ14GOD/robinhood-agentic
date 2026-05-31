"""Shared domain models — the vocabulary the whole brain speaks in.

These Pydantic models double as the JSON-schema contract for the LLM's
structured outputs, so engines get typed, validated results instead of
free-form prose to parse.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Risk / personality
# --------------------------------------------------------------------------- #
class RiskAppetite(str, Enum):
    conservative = "conservative"
    balanced = "balanced"
    aggressive = "aggressive"


class Horizon(str, Enum):
    short = "short"      # weeks–months, trades around catalysts
    medium = "medium"    # 6–18 months
    long = "long"        # multi-year, buy-and-hold


class FeedbackEvent(BaseModel):
    """A single accept/reject decision, captured WITH the stock's characteristics
    so the brain can infer *patterns* (e.g. 'you keep liking high-beta growth'),
    not just remember ticker symbols."""
    ticker: str
    accepted: bool
    beta: float = 0.0
    sector: str = ""
    flavor: str = ""               # stable | moderate | volatile
    dividend_yield: float = 0.0
    at: str = Field(default_factory=_now)


class RiskProfile(BaseModel):
    """Who the user is as an investor. Every recommendation is filtered through
    this. The user sets the base fields; the brain *learns* and nudges them over
    time from feedback + actual holdings, logging every change transparently."""
    appetite: RiskAppetite = RiskAppetite.balanced
    horizon: Horizon = Horizon.medium
    max_single_position_pct: float = 15.0   # comfort ceiling for one name
    prefers_dividends: bool = False
    avoid_sectors: list[str] = Field(default_factory=list)
    favor_sectors: list[str] = Field(default_factory=list)
    notes: str = ""                          # free-text self-description
    # --- learned state ---
    feedback_events: list[FeedbackEvent] = Field(default_factory=list)
    investor_signature: str = ""             # brain's derived read of who you are
    learning_log: list[str] = Field(default_factory=list)  # why it changed, newest first
    updated_at: str = Field(default_factory=_now)

    # convenience views over feedback_events
    @property
    def accepted_tickers(self) -> list[str]:
        return [e.ticker for e in self.feedback_events if e.accepted]

    @property
    def rejected_tickers(self) -> list[str]:
        return [e.ticker for e in self.feedback_events if not e.accepted]

    def describe(self) -> str:
        """Compact natural-language summary fed to the LLM."""
        parts = [
            f"Risk appetite: {self.appetite.value}.",
            f"Time horizon: {self.horizon.value}.",
            f"Max comfortable single-position size: {self.max_single_position_pct:.0f}% of portfolio.",
            f"{'Prefers' if self.prefers_dividends else 'Indifferent to'} dividend income.",
        ]
        if self.favor_sectors:
            parts.append(f"Leans toward: {', '.join(self.favor_sectors)}.")
        if self.avoid_sectors:
            parts.append(f"Avoids: {', '.join(self.avoid_sectors)}.")
        if self.notes:
            parts.append(f"Self-described: {self.notes}")
        if self.investor_signature:
            parts.append(f"Learned read of this investor: {self.investor_signature}")
        acc, rej = self.accepted_tickers, self.rejected_tickers
        if acc:
            parts.append(f"Has liked: {', '.join(acc[-10:])}.")
        if rej:
            parts.append(f"Has passed on: {', '.join(rej[-10:])}.")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #
class Holding(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float = 0.0
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pct(self) -> Optional[float]:
        if self.avg_cost <= 0:
            return None
        return (self.current_price - self.avg_cost) / self.avg_cost * 100.0


class Portfolio(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    cash: float = 0.0
    buying_power: float = 0.0
    reported_equity: float = 0.0
    pricing_source: str = ""
    pricing_warning: str = ""
    source: str = ""
    sync_ok: bool = True
    sync_message: str = ""
    as_of: str = Field(default_factory=_now)

    @property
    def total_value(self) -> float:
        if self.reported_equity > 0:
            return self.reported_equity
        return self.cash + sum(h.market_value for h in self.holdings)

    def weights(self) -> dict[str, float]:
        tv = self.total_value
        if tv <= 0:
            return {}
        return {h.ticker: h.market_value / tv * 100.0 for h in self.holdings}


# --------------------------------------------------------------------------- #
# Recommendations & discovery (LLM structured outputs)
# --------------------------------------------------------------------------- #
Action = Literal["buy", "sell", "trim", "add", "hold", "watch"]
DecisionLabel = Literal[
    "BUY CANDIDATE",
    "WATCHLIST",
    "WAIT FOR PULLBACK",
    "HOLD",
    "TRIM",
    "EXIT REVIEW",
    "REJECT",
    "DO NOTHING",
]


class TradeTicket(BaseModel):
    """A fully-reasoned, ready-to-act recommendation. The user executes it
    manually — the brain never places the order."""
    ticker: str
    action: Action
    conviction: int = Field(ge=1, le=10, description="1=weak, 10=table-pounding")
    thesis: str = Field(description="Why, in 2-4 sentences. The actual reasoning.")
    catalyst: str = Field(description="What could make this move, and when.")
    risks: str = Field(description="What breaks the thesis.")
    suggested_size_pct: float = Field(
        default=0.0, description="Suggested position size as % of portfolio."
    )
    fits_profile_because: str = Field(
        default="", description="How this matches the user's risk personality."
    )

    @computed_field
    @property
    def decision_label(self) -> DecisionLabel:
        return {
            "buy": "BUY CANDIDATE",
            "add": "BUY CANDIDATE",
            "watch": "WATCHLIST",
            "hold": "HOLD",
            "trim": "TRIM",
            "sell": "EXIT REVIEW",
        }.get(self.action, "DO NOTHING")  # type: ignore[return-value]


class StockIdea(BaseModel):
    """A discovery-engine find: something the user likely hasn't seen."""
    ticker: str
    name: str = ""
    why_now: str = Field(description="The hook — why it's interesting right now.")
    signal_summary: str = Field(description="What the quantitative screen flagged.")
    risk_flavor: Literal["stable", "moderate", "volatile"] = "moderate"
    conviction: int = Field(ge=1, le=10)


class DiscoveryResult(BaseModel):
    ideas: list[StockIdea]


class Finding(BaseModel):
    """A single proactive item for the always-on findings feed."""
    kind: Literal["opportunity", "risk", "news", "concentration"]
    ticker: str = ""
    headline: str = Field(description="One punchy line — what the user should know.")
    detail: str = Field(description="The grounded specifics behind it, 1-2 sentences.")


class FindingsFeed(BaseModel):
    findings: list[Finding]


class ChartPoint(BaseModel):
    at: str
    close: float


class StockChart(BaseModel):
    ticker: str
    span: Literal["1d", "1w", "1m", "3m", "6m", "1y"] = "3m"
    points: list[ChartPoint] = Field(default_factory=list)
    latest: float = 0.0
    return_pct: float = 0.0
    source: str = "yfinance"

    def summary(self) -> str:
        if not self.points:
            return f"{self.ticker}: no chart data found."
        return (
            f"{self.ticker} {self.span} chart: latest ${self.latest:.2f}, "
            f"return {self.return_pct:+.1f}% across {len(self.points)} points."
        )


# --------------------------------------------------------------------------- #
# Persistent research memory
# --------------------------------------------------------------------------- #
class Thesis(BaseModel):
    ticker: str
    thesis: str = ""
    status: Literal["active", "review", "broken", "archived"] = "active"
    strengthens: list[str] = Field(default_factory=list)
    weakens: list[str] = Field(default_factory=list)
    invalidation: str = ""
    last_decision: DecisionLabel = "WATCHLIST"
    updated_at: str = Field(default_factory=_now)


class ThesisVerdict(BaseModel):
    """The living-memory engine's re-judgement of a stored thesis after an event
    trips its invalidation condition. Drives the thesis status forward."""
    status: Literal["active", "review", "broken"] = Field(
        description="'broken' only if the invalidation condition is clearly met; "
                    "'review' if at risk and worth a human look; 'active' if it still holds.")
    decision_label: DecisionLabel = "HOLD"
    reason: str = Field(description="ONE grounded sentence citing the specific evidence.")


class WatchItem(BaseModel):
    ticker: str
    reason: str = ""
    mode: Literal["stable", "balanced", "volatile"] = "balanced"
    target_entry: float = 0.0
    max_allocation_pct: float = 0.0
    added_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class Briefing(BaseModel):
    id: str
    kind: Literal["morning", "evening", "manual"] = "manual"
    title: str
    summary: str
    bullets: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)


class ResearchState(BaseModel):
    watchlist: list[WatchItem] = Field(default_factory=list)
    theses: dict[str, Thesis] = Field(default_factory=dict)
    briefings: list[Briefing] = Field(default_factory=list)
    updated_at: str = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Shadow mode
# --------------------------------------------------------------------------- #
class ShadowTrade(BaseModel):
    """A logged paper trade. Every recommendation becomes one of these so we can
    measure whether the brain is actually any good before risking money.

    The record carries everything the evaluation layer needs to grade it later:
    the decision label and conviction, the engine that produced it, the risk mode
    and signals that justified it, and market/sector benchmark anchors captured at
    entry so returns can be measured *relative* to what holding the index would
    have done."""
    id: str
    ticker: str
    action: Action
    decision_label: DecisionLabel = "WATCHLIST"
    conviction: int
    thesis: str
    entry_price: float
    entry_at: str = Field(default_factory=_now)
    source: str = "analyst"           # which engine produced it
    risk_mode: str = ""               # the user's risk appetite at log time
    flavor: str = ""                  # stable | moderate | volatile (discovery)
    sector: str = ""
    entry_signals: dict = Field(default_factory=dict)  # the signals snapshot that justified it
    # benchmark anchors captured AT ENTRY (cannot be reconstructed later)
    bench_symbol: str = "SPY"
    bench_entry_price: float = 0.0
    sector_etf: str = ""
    sector_etf_entry_price: float = 0.0
    # outcome (filled in later by mark-to-market)
    last_price: float = 0.0
    last_at: str = ""
    bench_last_price: float = 0.0
    sector_etf_last_price: float = 0.0
    closed: bool = False
    closed_at: str = ""
    close_reason: str = ""
    user_executed: Optional[bool] = None   # did the user act on it?

    def _sign(self) -> float:
        # A sell/trim call is "right" when the name falls, so its return is the
        # negative of the price change. Everything else is a long-style call.
        return -1.0 if self.action in ("sell", "trim") else 1.0

    @staticmethod
    def _pct(entry: float, last: float) -> float:
        if entry <= 0 or last <= 0:
            return 0.0
        return (last - entry) / entry * 100.0

    def stock_change_pct(self) -> float:
        """Raw, unsigned price change of the name since entry."""
        return self._pct(self.entry_price, self.last_price)

    def bench_change_pct(self) -> float:
        return self._pct(self.bench_entry_price, self.bench_last_price)

    def sector_change_pct(self) -> float:
        return self._pct(self.sector_etf_entry_price, self.sector_etf_last_price)

    def return_pct(self) -> float:
        """Signed return of the recommendation in absolute terms."""
        return self._sign() * self.stock_change_pct()

    def alpha_pct(self) -> float:
        """Signed excess return versus the market benchmark (SPY). This is the
        number that says whether the call beat simply holding the index. Returns
        0.0 when no anchor was captured (e.g. legacy trades) so un-gradeable
        records don't pollute benchmark-relative stats."""
        if self.bench_entry_price <= 0:
            return 0.0
        return self._sign() * (self.stock_change_pct() - self.bench_change_pct())

    def has_benchmark(self) -> bool:
        """Whether this trade carries a usable market anchor — the evaluation
        layer uses this to compute alpha only over gradeable trades."""
        return self.bench_entry_price > 0

    def sector_alpha_pct(self) -> float:
        """Signed excess return versus the stock's own sector ETF."""
        if self.sector_etf_entry_price <= 0:
            return 0.0
        return self._sign() * (self.stock_change_pct() - self.sector_change_pct())


# --------------------------------------------------------------------------- #
# Strategy missions — standing theme trackers that work without being asked
# --------------------------------------------------------------------------- #
MissionLabel = Literal["BUY", "WATCH", "WAIT", "REJECT"]


class MissionCandidate(BaseModel):
    """One name a mission is tracking, with the brain's current verdict on it."""
    ticker: str
    label: MissionLabel = "WATCH"
    conviction: int = Field(default=5, ge=1, le=10)
    reason: str = ""
    sector: str = ""
    signals: dict = Field(default_factory=dict)  # snapshot behind the verdict
    first_seen: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class Mission(BaseModel):
    """A persistent research mission: 'track defense stocks', 'find stable AI
    exposure'. The brain keeps the roster current and re-labels it on its own."""
    id: str
    title: str                                  # the user's instruction, verbatim
    theme: str = ""                             # normalized short name of the theme
    mode: Literal["stable", "balanced", "volatile", "any"] = "any"
    status: Literal["active", "paused", "archived"] = "active"
    candidates: list[MissionCandidate] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    last_run_at: str = ""
    last_classified_at: str = ""
    last_seeded_at: str = ""           # when the roster was last (re)screened for new names


# --- LLM structured outputs for the mission engine --- #
class MissionSeedItem(BaseModel):
    ticker: str
    why: str = Field(default="", description="One line on why it fits the theme.")


class MissionSeed(BaseModel):
    """The roster the brain proposes when a mission is created."""
    theme: str = Field(description="A short, normalized name for the theme being tracked.")
    candidates: list[MissionSeedItem] = Field(
        description="Real, US-listed tickers that genuinely fit the theme (prefer the most on-theme names; 15 max).")


class MissionClassification(BaseModel):
    ticker: str
    label: MissionLabel
    conviction: int = Field(ge=1, le=10)
    reason: str = Field(description="One grounded sentence citing the signal or fit.")


class MissionRoster(BaseModel):
    """The brain's per-candidate verdicts on a mission's roster."""
    items: list[MissionClassification]


# --------------------------------------------------------------------------- #
# Deep research mode — the heavy, cited, self-critiqued analysis
# --------------------------------------------------------------------------- #
class DeepResearchDraft(BaseModel):
    """The first pass: plan, both cases, evidence, and an initial call."""
    plan: list[str] = Field(description="3-5 specific questions or angles this research sets out to answer.")
    bull_case: list[str] = Field(description="The strongest grounded points FOR.")
    bear_case: list[str] = Field(description="The strongest grounded points AGAINST.")
    evidence: list[str] = Field(description="Specific facts actually cited from the signals/news/chart provided.")
    thesis: str = Field(description="The core thesis in 2-4 sentences.")
    catalyst: str = Field(description="What could make it move, and roughly when.")
    risks: str = Field(description="What would break the thesis (its invalidation condition).")
    action: Action
    conviction: int = Field(ge=1, le=10)
    suggested_size_pct: float = Field(default=0.0, description="Suggested position size as % of portfolio.")


class DeepResearchCritique(BaseModel):
    """The self-critique pass: argue against the draft, then settle the final call."""
    critique: list[str] = Field(description="Honest self-criticism: the weakest links in the draft, what could be wrong, the steelman of the opposite call.")
    holds_up: bool = Field(description="Does the draft's call still stand after this criticism?")
    final_action: Action
    final_conviction: int = Field(ge=1, le=10)
    note: str = Field(description="One line on the final stance after self-criticism.")
