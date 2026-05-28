"""Shared domain models — the vocabulary the whole brain speaks in.

These Pydantic models double as the JSON-schema contract for the LLM's
structured outputs, so engines get typed, validated results instead of
free-form prose to parse.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


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
    as_of: str = Field(default_factory=_now)

    @property
    def total_value(self) -> float:
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


class PortfolioInsight(BaseModel):
    """One item in the daily guardian digest."""
    ticker: str
    headline: str
    sentiment: Literal["positive", "neutral", "negative"]
    detail: str
    suggested_action: Optional[TradeTicket] = None


class GuardianDigest(BaseModel):
    summary: str = Field(description="2-3 sentence state-of-the-portfolio.")
    insights: list[PortfolioInsight]
    concentration_flags: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A single proactive item for the always-on findings feed."""
    kind: Literal["opportunity", "risk", "news", "concentration"]
    ticker: str = ""
    headline: str = Field(description="One punchy line — what the user should know.")
    detail: str = Field(description="The grounded specifics behind it, 1-2 sentences.")


class FindingsFeed(BaseModel):
    findings: list[Finding]


# --------------------------------------------------------------------------- #
# Shadow mode
# --------------------------------------------------------------------------- #
class ShadowTrade(BaseModel):
    """A logged paper trade. Every recommendation becomes one of these so we
    can measure whether the brain is actually any good before risking money."""
    id: str
    ticker: str
    action: Action
    conviction: int
    thesis: str
    entry_price: float
    entry_at: str = Field(default_factory=_now)
    source: str = "analyst"           # which engine produced it
    # outcome (filled in later by mark-to-market)
    last_price: float = 0.0
    last_at: str = ""
    closed: bool = False
    user_executed: Optional[bool] = None   # did the user act on it?

    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        sign = -1.0 if self.action in ("sell", "trim") else 1.0
        return sign * (self.last_price - self.entry_price) / self.entry_price * 100.0
