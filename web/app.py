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

app = FastAPI(title="Signal Research Engine")
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


class ReconcileBody(BaseModel):
    trade_id: str
    mode: str  # "replace" | "keep"


class EvalLabelBody(BaseModel):
    run_id: str
    kind: str = ""
    ticker: str = ""
    verdict: str = ""              # good | mixed | flawed
    failure_modes: list[str] = []
    note: str = ""


class MandateBody(BaseModel):
    statement: str = ""


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
    brain.save_chat_message("user", body.message)
    out = brain.chat(body.message, history=body.history)
    brain.save_chat_message("assistant", (out or {}).get("answer", ""))
    return out


@app.post("/api/chat/stream")
def chat_stream(body: ChatBody):
    """Server-Sent Events: streams each step (tool call, note, answer) as the
    agent works, so the UI can render its reasoning live. Both turns are persisted
    so the Home conversation survives reloads."""
    def gen():
        brain.save_chat_message("user", body.message)
        try:
            for ev in brain.chat_stream(body.message, history=body.history):
                if ev.get("type") == "answer":
                    brain.save_chat_message("assistant", ev.get("text", ""))
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type':'error','text':str(e)})}\n\n"
        yield "data: {\"type\":\"done\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/history")
def chat_history(limit: int = Query(80, ge=1, le=200)):
    """The persisted Home conversation, oldest first (chat order)."""
    return {"messages": brain.chat_history(limit=limit)}


def _analyst_sources(ticker: str) -> list[dict]:
    """Sources for the card — from the unified evidence store (everything the brain has
    gathered on this name: web research + catalysts), deduped, most-recent first."""
    return brain.evidence(ticker, limit=12)


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    if not body.refresh:
        cached = brain.cached_analysis(body.ticker)
        if cached:
            cached["sources"] = _analyst_sources(body.ticker)
            return cached
    out = brain.analyze(body.ticker).model_dump()
    out["cached"] = False
    out["sources"] = _analyst_sources(body.ticker)
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


@app.post("/api/shadow/reconcile")
def shadow_reconcile(body: ReconcileBody):
    """Resolve a duplicate shadow re-call on demand — 'replace' closes the older
    open call(s) for that name, 'keep' leaves both. Never blocks; runs when you choose."""
    return brain.reconcile_duplicate(body.trade_id, body.mode)


@app.get("/api/mandate")
def mandate_get():
    """The standing mandate + the cached advisor plan (no LLM spend unless reviewed)."""
    return {"mandate": brain.get_mandate(), "review": brain.mandate_review(force=False)}


@app.post("/api/mandate")
def mandate_set(body: MandateBody):
    """Set/replace the user's goal (the brain reads it back + structures it)."""
    return {"mandate": brain.set_mandate(body.statement)}


@app.post("/api/mandate/review")
def mandate_review():
    """Generate the plain-language advisor read of the portfolio against the mandate."""
    return {"review": brain.mandate_review(force=True)}


@app.get("/api/twin")
def twin_get(refresh: bool = False):
    """You vs the Twin since inception (or {started:false} if it hasn't been launched yet)."""
    return brain.twin_compare(refresh=refresh)


@app.post("/api/twin/start")
def twin_start():
    """Clone your real account into the Twin and start it (one-time)."""
    return {"twin": brain.twin_start()}


@app.post("/api/twin/decide")
def twin_decide():
    """Force an Autopilot decision cycle now (the tab's 'run a cycle' button)."""
    return brain.twin_decide_now()


@app.post("/api/twin/reset")
def twin_reset():
    """Wipe Autopilot (fund + positions + trades + equity) so it can be re-cloned fresh."""
    brain.twin_reset()
    return {"started": False}


@app.get("/api/evals")
def evals_overview(limit: int = Query(30, ge=1, le=100), kind: str | None = None):
    """The eval worklist + the emerging suite: reviewable traces (with any label) plus the
    taxonomy and failure-mode frequencies."""
    return {
        "traces": brain.eval_traces(limit=limit, kind=kind),
        "taxonomy": brain.eval_taxonomy(),
        "summary": brain.eval_summary(),   # your labels (ground truth)
        "judge": brain.judge_summary(),    # the auto-judge's continuous score
    }


@app.post("/api/evals/label")
def evals_label(body: EvalLabelBody):
    """Persist a human error-analysis label on one trace."""
    ok = brain.save_eval_label(body.run_id, body.kind, body.ticker, body.verdict,
                               body.failure_modes, body.note)
    return {"ok": ok, "summary": brain.eval_summary()}


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
    """FAST loop — keep broker/price/shadow data warm without spending LLM tokens.

    Deliberately tiny: only the cheap, no-LLM work that the live UI depends on. The
    expensive, multi-minute brain work lives in its own loop (`_brain_loop`) so a long
    deep dive can never block the price refresh and make the data look stale."""
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
        await asyncio.sleep(config.AUTO_REFRESH_SECONDS)


async def _brain_loop() -> None:
    """SLOW loop — the expensive, LLM-spending brain work, fully decoupled from the
    live-data refresh. Each step is gated/cooldowned in its own engine (a calm book
    spends nothing) and guarded so one failure can't stall the rest. Because this runs
    independently, a multi-minute deep dive here never freezes prices or trips the
    'updater looks down' banner — that was the bug. Cadence ceiling only; the gates do
    the real throttling."""
    while True:
        # Social sentiment: ping when a name's Reddit chatter spikes. One cheap call,
        # gated + fully quarantined (no-op when disabled).
        try:
            await asyncio.to_thread(brain.ingest_sentiment)
        except Exception as e:  # noqa: BLE001
            logger.warning("sentiment ingest failed: %s", e)
        # Catalyst radar: surface fresh structured company news on the user's names.
        # Cheap HTTP scan, gated + cooldowned, fully quarantined (no-op with no key).
        try:
            await asyncio.to_thread(brain.ingest_catalysts)
        except Exception as e:  # noqa: BLE001
            logger.warning("catalyst ingest failed: %s", e)
        # Living memory: re-judge triggered theses. Gated, so usually a no-op.
        try:
            await asyncio.to_thread(brain.revisit_memory)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory revisit failed: %s", e)
        # Strategy missions: re-run any whose daily cadence lapsed. Gated per mission.
        try:
            await asyncio.to_thread(brain.run_due_missions)
        except Exception as e:  # noqa: BLE001
            logger.warning("mission run failed: %s", e)
        # Autonomous deep research: dive names that just hit a high-signal trigger and
        # drop the report into the ping feed. Heavily gated + cooldowned in the engine.
        try:
            await asyncio.to_thread(brain.run_autoresearch)
        except Exception as e:  # noqa: BLE001
            logger.warning("autoresearch failed: %s", e)
        # Structural (portfolio-level) risk read + autonomous concentration ping.
        try:
            await asyncio.to_thread(brain.run_structural_risk)
        except Exception as e:  # noqa: BLE001
            logger.warning("structural risk failed: %s", e)
        # Proactive mandate plan: re-read the book against the user's goal on its cadence and
        # ping a fresh plan — the agent coming to you. Gated to once/period, no-op without a mandate.
        try:
            await asyncio.to_thread(brain.run_mandate_review)
        except Exception as e:  # noqa: BLE001
            logger.warning("mandate review failed: %s", e)
        # Drift-triggered plan: fire off-cadence when the book moves materially off its last-planned
        # shape (a big weight shift, a new/exited position), not just on the weekly clock.
        try:
            await asyncio.to_thread(brain.run_mandate_drift)
        except Exception as e:  # noqa: BLE001
            logger.warning("mandate drift check failed: %s", e)
        # Autonomous theme scout: Signal forms its own research agenda from broad-market leadership
        # and recent events. Autopilot reads these themes as grounded candidate sources.
        try:
            await asyncio.to_thread(brain.run_theme_scout)
        except Exception as e:  # noqa: BLE001
            logger.warning("theme scout failed: %s", e)
        # Autopilot (the Twin): its autonomous think (gated to TWIN_DECIDE_HOURS), then fill any
        # queued orders during market hours, then record an equity point so the race line stays live.
        try:
            await asyncio.to_thread(brain.twin_review_windows)
            await asyncio.to_thread(brain.run_twin_decision)
            await asyncio.to_thread(brain.twin_execute_pending)
            await asyncio.to_thread(brain.twin_snapshot)
        except Exception as e:  # noqa: BLE001
            logger.warning("twin tick failed: %s", e)
        # Self-grading sweep: auto-score any recent reasoning trace the inline gate didn't reach
        # (mainly the autonomous re-judge path). Bounded per cycle + gated; a calm system no-ops.
        try:
            await asyncio.to_thread(brain.judge_recent_traces)
        except Exception as e:  # noqa: BLE001
            logger.warning("judge sweep failed: %s", e)
        # Pre-warm the curated findings feed so it's ready when the user opens the tab.
        try:
            await asyncio.to_thread(brain.prewarm_feed)
        except Exception as e:  # noqa: BLE001
            logger.warning("feed pre-warm failed: %s", e)
        await asyncio.sleep(config.BRAIN_LOOP_SECONDS)


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
        asyncio.create_task(_refresh_loop())   # fast: live data only
        asyncio.create_task(_brain_loop())     # slow: LLM brain work, decoupled
    if config.AUTO_BRIEFINGS:
        asyncio.create_task(_briefing_loop())


# ----- static dashboard ----- #
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
