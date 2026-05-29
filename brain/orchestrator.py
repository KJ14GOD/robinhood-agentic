"""The brain's public API — what the dashboard and CLI call.

Ties together profile + portfolio + data + engines + shadow ledger. This is the
single import surface for everything above the engine layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterator

from . import agent, llm, profile_store, research_state, shadow
from .data.news import clear_news_cache
from .data.prices import clear_caches, get_chart, get_portfolio_chart, get_quote
from .data import robinhood_charts
from .db import repository as db_repo
from .engines import analyst, briefing, discovery, findings, guardian, monitor
from .models import Briefing, ChartPoint, DiscoveryResult, GuardianDigest, Portfolio, ResearchState, RiskProfile, StockChart, TradeTicket
from .portfolio import clear_portfolio_cache, get_portfolio


# --- profile ---------------------------------------------------------------- #
def get_profile() -> RiskProfile:
    return profile_store.load_profile()


def update_profile(profile: RiskProfile) -> RiskProfile:
    return profile_store.save_profile(profile)


def feedback(ticker: str, accepted: bool) -> RiskProfile:
    profile_store.record_feedback(ticker, accepted)
    return refresh_learning()


def refresh_learning() -> RiskProfile:
    """Re-read the user's actual holdings into the investor signature. Called
    after feedback and exposed so the UI can trigger a learning refresh."""
    from . import profile_learning
    profile = profile_learning.learn_from_holdings(profile_store.load_profile(), get_portfolio(refresh=True))
    return profile_store.save_profile(profile)


# --- portfolio -------------------------------------------------------------- #
def portfolio(refresh: bool = False) -> Portfolio:
    return get_portfolio(refresh=refresh)


def refresh_live_state() -> Portfolio:
    """Force a read-through of market data and broker/manual portfolio state."""
    clear_portfolio_cache()
    clear_caches()
    clear_news_cache()
    return get_portfolio(refresh=True)


def init_database() -> None:
    from .db.session import init_db

    init_db()


def get_research_state() -> ResearchState:
    return research_state.load_state()


def create_briefing(kind: str = "manual") -> Briefing:
    if kind not in {"morning", "evening", "manual"}:
        kind = "manual"
    return briefing.generate(kind, get_portfolio(refresh=True), get_profile())


def _anchor_to_now(chart: StockChart, current_price: float) -> StockChart:
    """Extend a chart so its rightmost point is the live price at this moment.

    Charts arrive from two providers (Robinhood for stocks, yfinance for the
    portfolio reconstruction) and at coarse intervals (weekly bars for 1Y,
    daily for 6M). That left charts ending at inconsistent wall-clock times and
    stopping days short of today. Pinning a live "now" point to every chart —
    the way brokerages draw the line out to the current quote — keeps them
    consistent and current regardless of source or interval.
    """
    if current_price <= 0 or not chart.points:
        return chart
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    points = list(chart.points) + [ChartPoint(at=now, close=current_price)]
    first = points[0].close
    ret = ((current_price - first) / first * 100.0) if first else 0.0
    return chart.model_copy(update={"points": points, "latest": current_price, "return_pct": ret})


def stock_chart(ticker: str, span: str = "3m", refresh: bool = False) -> StockChart:
    rh_chart = robinhood_charts.get_stock_chart(ticker, span=span, refresh=refresh)
    chart = rh_chart if rh_chart.points else get_chart(ticker, span=span, refresh=refresh)
    # Anchor to the *warm* quote, never a forced re-fetch. Forcing a live quote on
    # every chart poll hammered the quote API; when it rate-limited it returned 0,
    # so _anchor_to_now skipped the live point and the line froze at the last bar.
    # The warm quote is kept fresh by the background refresh loop instead.
    quote = get_quote(ticker)
    return _anchor_to_now(chart, quote.price if quote.ok else 0.0)


def portfolio_chart(span: str = "3m", refresh: bool = False) -> StockChart:
    # Use the warm portfolio snapshot for the anchor — same value the header shows,
    # no extra broker round-trip — so the chart tip can't diverge from total value
    # and can't be silently zeroed by a rate-limited refresh. Only the historical
    # body honours `refresh`.
    pf = get_portfolio()
    rh_chart = robinhood_charts.get_portfolio_chart(span=span, refresh=refresh)
    if rh_chart.points:
        chart = rh_chart
    else:
        chart = get_portfolio_chart(
            pf.holdings,
            cash=pf.cash,
            span=span,
            refresh=refresh,
            target_latest=pf.total_value,
        )
    return _anchor_to_now(chart, pf.total_value)


# --- engines ---------------------------------------------------------------- #
def analyze(ticker: str) -> TradeTicket:
    return analyst.analyze(ticker, get_profile())


def cached_analysis(ticker: str) -> dict | None:
    row = db_repo.get_ticker_research(ticker)
    if not row:
        return None
    label = row["action_label"]
    action = {
        "BUY CANDIDATE": "buy",
        "WATCHLIST": "watch",
        "WAIT FOR PULLBACK": "watch",
        "HOLD": "hold",
        "TRIM": "trim",
        "EXIT REVIEW": "sell",
        "REJECT": "watch",
        "DO NOTHING": "hold",
    }.get(label, "watch")
    return {
        "ticker": row["ticker"],
        "action": action,
        "decision_label": label,
        "conviction": max(1, min(10, int(round(row["confidence"] or 1)))),
        "thesis": row["thesis"],
        "catalyst": row["bull_case"] or "Cached research. Run Deep refresh for a fresh catalyst check.",
        "risks": row["risks"] or row["bear_case"],
        "suggested_size_pct": 0.0,
        "fits_profile_because": "",
        "cached": True,
        "source": row["source"],
        "refreshed_at": row["refreshed_at"],
    }


def discover(flavor: str = "any", top_n: int = 5) -> DiscoveryResult:
    pf = get_portfolio()
    held = [h.ticker for h in pf.holdings]
    return discovery.discover(get_profile(), flavor=flavor, top_n=top_n, exclude=held)


def daily_digest() -> GuardianDigest:
    return guardian.run_guardian(get_portfolio(), get_profile())


def feed():
    """Proactive findings for the always-on feed."""
    return findings.scan(get_portfolio(), get_profile())


def run_monitors() -> list[dict]:
    """Cheap deterministic event scan over the live portfolio. Persists new,
    deduped events to the DB. Called by the background loop — no LLM, no tokens."""
    return monitor.run_monitors(get_portfolio(), get_profile())


def today_events(limit: int = 40, within_hours: float = 72.0) -> dict:
    """The persisted event stream for the Today surface, newest first, ranked so
    the loudest (alert) items lead. Reads the DB — does not trigger a scan."""
    rank = {"alert": 0, "warn": 1, "info": 2}
    events = db_repo.recent_events(limit=limit, within_hours=within_hours)
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)   # newest first
    events.sort(key=lambda e: rank.get(e.get("severity"), 3))          # stable: alert → warn → info
    return {"events": events, "count": len(events)}


# --- shadow mode ------------------------------------------------------------ #
def scoreboard(refresh: bool = False) -> dict:
    return shadow.scoreboard(refresh=refresh)


# --- agentic chat ----------------------------------------------------------- #
def chat(message: str, history: list[dict] | None = None) -> dict:
    """Agentic Q&A. The model drives its own research via tools and remembers
    the conversation. Returns {answer, steps} where steps is the trace of what
    the brain did."""
    return agent.run(message, history=history)


def chat_stream(message: str, history: list[dict] | None = None) -> Iterator[dict]:
    """Streaming variant — yields each step as it happens so the UI can render
    the brain's thinking live (tool calls, interim notes, final answer)."""
    yield from agent.run_stream(message, history=history)
