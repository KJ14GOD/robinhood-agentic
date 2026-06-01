"""The brain's public API — what the dashboard and CLI call.

Ties together profile + portfolio + data + engines + shadow ledger. This is the
single import surface for everything above the engine layer.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterator

from . import agent, llm, profile_store, research_state, shadow
from .data.news import clear_news_cache
from .data.prices import clear_caches, get_chart, get_portfolio_chart, get_quote
from .data import robinhood_charts
from .db import repository as db_repo
from .engines import analyst, autoresearch, briefing, discovery, evaluation, findings, memory, missions, monitor
from .engines import deep_research as _deep_research
from . import config
from .models import Briefing, ChartPoint, DiscoveryResult, Mission, Portfolio, ResearchState, RiskProfile, StockChart, TradeTicket
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
    clear_caches(include_signals=False)  # keep daily signal/screen caches warm; only live prices need clearing each cycle
    clear_news_cache()
    return get_portfolio(refresh=True)


def init_database() -> None:
    from .db.session import init_db

    init_db()


def get_research_state() -> ResearchState:
    return research_state.load_state()


def set_watch_target(ticker: str, target_entry: float) -> ResearchState:
    """Set/clear the entry-price alert on a watchlist name; returns the new state."""
    return research_state.set_watch_target(ticker, target_entry)


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
        "catalyst": row["bull_case"] or "Cached research — hit Re-run analyst or Deep research for a fresh catalyst.",
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


def deep_research(ticker: str) -> dict:
    """Heavy, cited, self-critiqued deep dive on one ticker. Updates the stored
    thesis, logs to the track record, and writes an audit trail to agent_runs."""
    return _deep_research.run(ticker, get_profile())


# Findings is an LLM pass, so it's cached and signature-gated: we only spend a
# call when the holdings mix or the logged events actually changed (or the TTL
# lapses). The background loop pre-warms it, so the feed is ready the instant the
# tab opens and a calm book costs almost nothing.
_FEED_CACHE: dict = {"feed": None, "at": 0.0, "sig": None}
_FEED_TTL = 3 * 3600.0  # upper bound on staleness even when nothing changed


def _feed_signature(pf: Portfolio) -> tuple:
    holdings_key = tuple(sorted((h.ticker, round(h.market_value)) for h in pf.holdings))
    latest = db_repo.recent_events(limit=1)
    return (holdings_key, latest[0].get("id") if latest else None)


def feed(force: bool = False):
    """Curated findings for the always-on feed — a ranked view over the persisted
    event stream (+ light news/opportunity enrichment). Cached so repeated tab
    opens are instant and only a real change triggers a fresh LLM curation."""
    pf = get_portfolio()
    sig = _feed_signature(pf)
    now = time.time()
    cached = _FEED_CACHE["feed"]
    if (not force and cached is not None and _FEED_CACHE["sig"] == sig
            and now - _FEED_CACHE["at"] < _FEED_TTL):
        return cached
    result = findings.scan(pf, get_profile())
    _FEED_CACHE.update(feed=result, at=now, sig=sig)
    return result


def prewarm_feed() -> None:
    """Background pre-warm: refresh the findings cache if it's gone stale, so the
    feed is already there when the user arrives. No-op cost when nothing changed."""
    if get_portfolio().holdings:
        feed()


def run_monitors() -> list[dict]:
    """Cheap deterministic event scan over the live portfolio. Persists new,
    deduped events to the DB. Called by the background loop — no LLM, no tokens."""
    return monitor.run_monitors(get_portfolio(), get_profile())


def revisit_memory() -> list[dict]:
    """Living memory: re-judge triggered theses against their invalidation and move
    them forward. Gated (only fires on a real trigger, once/day/name), so it spends
    nothing on a calm book. Returns the theses that changed."""
    return memory.revisit_theses(get_portfolio(), get_profile())


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


def scorecard(refresh: bool = False) -> dict:
    """The evaluation layer: graded recommendations — calibration, attribution,
    and benchmark-relative scoring. This is what proves whether the brain has edge."""
    return evaluation.scorecard(refresh=refresh)


def agent_runs(limit: int = 20, kind: str | None = None) -> list[dict]:
    """The audit trail: recent agent loops with their tool traces. Reads the DB."""
    return db_repo.recent_agent_runs(limit=limit, kind=kind)


# --- strategy missions ------------------------------------------------------ #
def list_missions(status: str | None = None) -> list[Mission]:
    return db_repo.all_missions(status=status)


def create_mission(title: str, mode: str = "any") -> Mission:
    """Seed and classify a new standing theme tracker."""
    return missions.create_mission(title, mode, get_profile())


def run_mission(mission_id: str, force: bool = True) -> Mission | None:
    m = db_repo.get_mission(mission_id)
    if not m:
        return None
    return missions.run_mission(m, get_profile(), force=force)


def set_mission_status(mission_id: str, status: str) -> Mission | None:
    return db_repo.set_mission_status(mission_id, status)


def delete_mission(mission_id: str) -> None:
    db_repo.delete_mission(mission_id)


def run_due_missions() -> list[dict]:
    """Background entry point: re-run active missions whose cadence has lapsed."""
    return missions.run_due_missions(get_profile())


def run_autoresearch() -> list[dict]:
    """Background entry point: autonomously deep-dive names that just hit a high-signal trigger
    (thesis broke/under review, mission name promoted to BUY) and drop the report into the ping
    feed. Conservative + cooldowned in the engine; the whole thing is gated off by config."""
    if not config.AUTO_DEEP_RESEARCH:
        return []
    return autoresearch.run_due_dives(get_profile())


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
