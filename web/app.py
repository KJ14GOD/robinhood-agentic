"""FastAPI backend for the dashboard.

Run:  uvicorn web.app:app --reload
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

import json

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brain import orchestrator as brain
from brain.models import RiskProfile
from brain.portfolio import manual as manual_pf
from brain import config

app = FastAPI(title="Stock Research Brain")
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger("brain.refresh")

# Heartbeat for the background refresh loop. The dashboard's data freshness
# hinges on this loop running — if it dies, the read-through cache happily
# serves stale snapshots with no error. So we track when it last succeeded
# and surface a "stale" flag the dashboard turns into a visible banner.
_REFRESH = {"last_ok": None, "last_error": None, "started": None}


def _refresh_health() -> dict:
    """Is live data actually being refreshed? stale=True means the loop has
    gone quiet for several cycles (crashed or stuck), so on-screen numbers
    may be old."""
    if config.AUTO_REFRESH_SECONDS <= 0:
        return {"stale": False, "message": ""}  # auto-refresh intentionally off
    ref = _REFRESH["last_ok"] or _REFRESH["started"]
    if ref is None:
        return {"stale": False, "message": ""}  # not started yet
    age = time.time() - ref
    if age <= config.AUTO_REFRESH_SECONDS * 3:  # one missed cycle is noise
        return {"stale": False, "message": ""}
    mins = max(1, int(age // 60))
    if _REFRESH["last_ok"]:
        msg = f"Live data hasn't refreshed in {mins} min — background updater looks down"
    else:
        msg = f"Live data has never refreshed since startup ({mins} min) — background updater looks down"
    if _REFRESH["last_error"]:
        msg += f" ({_REFRESH['last_error']})"
    return {"stale": True, "message": msg}


# ----- request bodies ----- #
class ChatBody(BaseModel):
    message: str
    history: list[dict] = []


class AnalyzeBody(BaseModel):
    ticker: str
    refresh: bool = False


class DiscoverBody(BaseModel):
    flavor: str = "any"
    top_n: int = 5


class FeedbackBody(BaseModel):
    ticker: str
    accepted: bool


class HoldingsBody(BaseModel):
    holdings: list[dict]
    cash: float = 0.0


class BriefingBody(BaseModel):
    kind: str = "manual"


class WatchTargetBody(BaseModel):
    ticker: str
    target_entry: float = 0.0


class MissionBody(BaseModel):
    title: str
    mode: str = "any"


class MissionStatusBody(BaseModel):
    status: str


class DeepResearchBody(BaseModel):
    ticker: str


# ----- API ----- #
def _state(pf=None):
    pf = pf or brain.portfolio()
    weights = pf.weights()
    return {
        "source": pf.source or config.PORTFOLIO_SOURCE,
        "as_of": pf.as_of,
        "sync_ok": pf.sync_ok,
        "sync_message": pf.sync_message,
        "refresh": _refresh_health(),
        "profile": brain.get_profile().model_dump(),
        "research": brain.get_research_state().model_dump(),
        "portfolio": {
            "total_value": pf.total_value,
            "cash": pf.cash,
            "buying_power": pf.buying_power,
            "reported_equity": pf.reported_equity,
            "pricing_source": pf.pricing_source,
            "pricing_warning": pf.pricing_warning,
            "as_of": pf.as_of,
            "holdings": [
                {**h.model_dump(), "market_value": h.market_value,
                 "weight": weights.get(h.ticker, 0.0),
                 "unrealized_pct": h.unrealized_pct}
                for h in pf.holdings
            ],
        },
    }


@app.get("/api/state")
def state():
    return _state()


@app.post("/api/refresh")
def refresh():
    pf = brain.refresh_live_state()
    return _state(pf)


@app.post("/api/profile")
def save_profile(profile: RiskProfile):
    return brain.update_profile(profile).model_dump()


@app.post("/api/holdings")
def save_holdings(body: HoldingsBody):
    if config.PORTFOLIO_SOURCE != "manual":
        return {"error": "Holdings are read from Robinhood; switch PORTFOLIO_SOURCE=manual to edit here."}
    manual_pf.save_portfolio(body.holdings, body.cash)
    return refresh()


@app.post("/api/chat")
def chat(body: ChatBody):
    """Non-streaming agentic chat. Returns {answer, steps} once complete."""
    return brain.chat(body.message, history=body.history)


@app.post("/api/chat/stream")
def chat_stream(body: ChatBody):
    """Server-Sent Events: streams each step (tool call, note, answer) as the
    agent works, so the UI can render its reasoning live."""
    def gen():
        try:
            for ev in brain.chat_stream(body.message, history=body.history):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type':'error','text':str(e)})}\n\n"
        yield "data: {\"type\":\"done\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    if not body.refresh:
        cached = brain.cached_analysis(body.ticker)
        if cached:
            return cached
    out = brain.analyze(body.ticker).model_dump()
    out["cached"] = False
    return out


@app.post("/api/deep_research")
def deep_research(body: DeepResearchBody):
    """Heavy, cited, self-critiqued deep dive on one ticker (two model calls)."""
    if not body.ticker.strip():
        return {"error": "Provide a ticker to research."}
    return brain.deep_research(body.ticker)


@app.post("/api/discover")
def discover(body: DiscoverBody):
    return brain.discover(flavor=body.flavor, top_n=body.top_n).model_dump()


@app.get("/api/feed")
def feed():
    return brain.feed().model_dump()


@app.get("/api/events")
def events(limit: int = Query(40, ge=1, le=200)):
    """Persisted, deduped event stream from the deterministic monitor loop."""
    return brain.today_events(limit=limit)


@app.post("/api/briefing")
def briefing(body: BriefingBody):
    return brain.create_briefing(body.kind).model_dump()


@app.post("/api/watch/target")
def watch_target(body: WatchTargetBody):
    """Set/clear the entry-price alert on a watchlist name. Returns the new research state."""
    return brain.set_watch_target(body.ticker, body.target_entry).model_dump()


@app.get("/api/chart/{ticker}")
def chart(
    ticker: str,
    span: str = Query("3m", pattern="^(1d|1w|1m|3m|6m|1y)$"),
    refresh: bool = False,
):
    if ticker.lower() in {"portfolio", "total", "account"}:
        return brain.portfolio_chart(span=span, refresh=refresh).model_dump()
    return brain.stock_chart(ticker, span=span, refresh=refresh).model_dump()


@app.get("/api/scoreboard")
def scoreboard():
    return brain.scoreboard()


@app.get("/api/scorecard")
def scorecard(refresh: bool = False):
    """The evaluation layer: calibration, attribution, and benchmark-relative scoring.
    `refresh` re-marks open trades against fresh quotes (used by the live auto-refresh)."""
    return brain.scorecard(refresh=refresh)


@app.get("/api/structural_risk")
def structural_risk(refresh: bool = False):
    """Portfolio-level structural read: correlated-bet clusters and the biggest hidden risk."""
    return brain.structural_risk(force=refresh).model_dump()


@app.get("/api/agent_runs")
def agent_runs(limit: int = Query(20, ge=1, le=100), kind: str | None = None):
    """The audit trail of agentic loops (chat, deep research). Reads the DB."""
    return {"runs": brain.agent_runs(limit=limit, kind=kind)}


@app.get("/api/missions")
def missions_list():
    return {"missions": [m.model_dump() for m in brain.list_missions()]}


@app.post("/api/missions")
def mission_create(body: MissionBody):
    """Seed a new standing theme tracker (LLM maps theme -> roster, then classifies)."""
    if not body.title.strip():
        return {"error": "A mission needs a theme, e.g. 'track defense stocks'."}
    return brain.create_mission(body.title, body.mode).model_dump()


@app.post("/api/missions/{mission_id}/run")
def mission_run(mission_id: str):
    m = brain.run_mission(mission_id, force=True)
    return m.model_dump() if m else {"error": "Mission not found."}


@app.post("/api/missions/{mission_id}/status")
def mission_status(mission_id: str, body: MissionStatusBody):
    m = brain.set_mission_status(mission_id, body.status)
    return m.model_dump() if m else {"error": "Mission not found or bad status."}


@app.delete("/api/missions/{mission_id}")
def mission_delete(mission_id: str):
    brain.delete_mission(mission_id)
    return {"ok": True}


@app.post("/api/feedback")
def feedback(body: FeedbackBody):
    return brain.feedback(body.ticker, body.accepted).model_dump()


@app.post("/api/learn")
def learn():
    """Re-read the real portfolio into the learned investor signature."""
    return brain.refresh_learning().model_dump()


async def _refresh_loop() -> None:
    """Keep broker/price/shadow data warm without spending LLM tokens."""
    while True:
        try:
            await asyncio.to_thread(brain.refresh_live_state)
            await asyncio.to_thread(brain.scoreboard, True)
            _REFRESH["last_ok"] = time.time()
            _REFRESH["last_error"] = None
        except Exception as e:  # noqa: BLE001
            _REFRESH["last_error"] = str(e)
            logger.warning("background refresh failed: %s", e)
        # Monitors run in their own guard: a detector failure must not mark the
        # whole live refresh as stale (prices already updated above).
        try:
            await asyncio.to_thread(brain.run_monitors)  # cheap, no-LLM event scan
        except Exception as e:  # noqa: BLE001
            logger.warning("monitor scan failed: %s", e)
        # Social sentiment: ping when a name's Reddit chatter spikes. One cheap call,
        # gated + fully quarantined (no-op when disabled), so it can't disturb the rest.
        try:
            await asyncio.to_thread(brain.ingest_sentiment)
        except Exception as e:  # noqa: BLE001
            logger.warning("sentiment ingest failed: %s", e)
        # Living memory: re-judge triggered theses. Gated, so usually a no-op; its
        # own guard so an LLM hiccup can't disturb price refresh or the monitor.
        try:
            await asyncio.to_thread(brain.revisit_memory)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory revisit failed: %s", e)
        # Strategy missions: re-run any whose daily cadence lapsed. Gated per
        # mission, so a calm set spends nothing; its own guard for the same reason.
        try:
            await asyncio.to_thread(brain.run_due_missions)
        except Exception as e:  # noqa: BLE001
            logger.warning("mission run failed: %s", e)
        # Autonomous deep research: dive names that just hit a high-signal trigger (logged by the
        # steps above this cycle) and drop the report into the ping feed. Heavily gated + cooldowned
        # in the engine, so a calm book spends nothing; its own guard so a dive can't disturb the rest.
        try:
            await asyncio.to_thread(brain.run_autoresearch)
        except Exception as e:  # noqa: BLE001
            logger.warning("autoresearch failed: %s", e)
        # Structural (portfolio-level) risk read + autonomous concentration ping. Gated like the
        # feed on the allocation signature, so a steady book spends nothing.
        try:
            await asyncio.to_thread(brain.run_structural_risk)
        except Exception as e:  # noqa: BLE001
            logger.warning("structural risk failed: %s", e)
        # Pre-warm the curated findings feed (over the freshly-logged events) so
        # it's ready the moment the user opens the tab. Cached + signature-gated,
        # so a calm book with no new events recomputes only when the TTL lapses.
        try:
            await asyncio.to_thread(brain.prewarm_feed)
        except Exception as e:  # noqa: BLE001
            logger.warning("feed pre-warm failed: %s", e)
        await asyncio.sleep(config.AUTO_REFRESH_SECONDS)


def _briefing_exists_today(kind: str) -> bool:
    today = datetime.now().date()
    state = brain.get_research_state()
    for b in state.briefings:
        if b.kind != kind:
            continue
        try:
            created = datetime.fromisoformat(b.created_at).astimezone().date()
        except ValueError:
            created = None
        if created == today:
            return True
    return False


async def _briefing_loop() -> None:
    """Create one morning/evening briefing per day while the app is running."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now().strftime("%H:%M")
        try:
            if now >= config.MORNING_BRIEF_TIME and not _briefing_exists_today("morning"):
                await asyncio.to_thread(brain.create_briefing, "morning")
            if now >= config.EVENING_BRIEF_TIME and not _briefing_exists_today("evening"):
                await asyncio.to_thread(brain.create_briefing, "evening")
        except Exception:
            pass


@app.on_event("startup")
async def start_background_refresh() -> None:
    try:
        brain.init_database()
    except Exception:
        pass
    if config.AUTO_REFRESH_SECONDS > 0:
        _REFRESH["started"] = time.time()
        asyncio.create_task(_refresh_loop())
    if config.AUTO_BRIEFINGS:
        asyncio.create_task(_briefing_loop())


# ----- static dashboard ----- #
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
