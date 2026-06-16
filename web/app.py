"""FastAPI backend for the dashboard.

Run:  uvicorn web.app:app --reload
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
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
_BRAIN = {
    "started": None,
    "cycle_started": None,
    "last_seen": None,
    "last_ok": None,
    "last_error": None,
    "current_step": None,
    "current_step_started": None,
    "cycle_count": 0,
    "steps": {},
}

# Hard ceilings per brain step. The loop is sequential, so without deadlines one stalled
# network/LLM call freezes every step below it. These budgets are intentionally not one-size-fits-all:
# deterministic scans stay tight, while agentic research and Autopilot get enough room to finish.
_DEFAULT_STEP_TIMEOUT_SECONDS = 180
_STEP_TIMEOUTS_SECONDS = {
    "sentiment": 60,
    "catalysts": 60,
    "memory": 420,
    "missions": 300,
    "autoresearch": 900,
    "structural_risk": 300,
    "mandate_review": 300,
    "mandate_drift": 300,
    "theme_scout": 240,
    "strategy_discovery": 240,
    "twin_review": 180,
    "twin_decision": 420,
    "twin_fill": 180,
    "twin_snapshot": 180,
    "judge": 360,
    "feed": 240,
}

# The canonical ordered brain pipeline. Lifted to module scope so the timeline UI can show all
# steps (with labels, in order) even before they have run this cycle — pending/running/done/failed.
_BRAIN_STEPS = [
    ("sentiment", brain.ingest_sentiment, "Sentiment ingest"),
    ("catalysts", brain.ingest_catalysts, "Catalyst radar"),
    ("memory", brain.revisit_memory, "Living memory re-judge"),
    ("missions", brain.run_due_missions, "Strategy missions"),
    ("autoresearch", brain.run_autoresearch, "Autonomous deep research"),
    ("structural_risk", brain.run_structural_risk, "Structural risk"),
    ("mandate_review", brain.run_mandate_review, "Mandate review"),
    ("mandate_drift", brain.run_mandate_drift, "Mandate drift check"),
    ("theme_scout", brain.run_theme_scout, "Theme scout"),
    ("strategy_discovery", brain.run_strategy_discovery, "Strategy discovery"),
    ("twin_review", brain.twin_review_windows, "Autopilot review windows"),
    ("twin_decision", brain.run_twin_decision, "Autopilot decision"),
    ("twin_fill", brain.twin_execute_pending, "Autopilot fill orders"),
    ("twin_snapshot", brain.twin_snapshot, "Autopilot equity snapshot"),
    ("judge", brain.judge_recent_traces, "Self-grading sweep"),
    ("feed", brain.prewarm_feed, "Feed pre-warm"),
]
_BRAIN_STEP_LABELS = {key: label for key, _fn, label in _BRAIN_STEPS}


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


def _iso_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _step_summary(result) -> str:
    """A short, human description of what a step actually did — shown when you open
    a timeline row. Names the tickers when the step returns rows so 'updated' becomes
    'AAPL, NVDA re-judged' etc."""
    if isinstance(result, bool):
        return "ran" if result else "skipped (gated / nothing to do)"
    if isinstance(result, list):
        n = len(result)
        if not n:
            return "ran — no items this cycle"
        tickers = [str(r.get("ticker")) for r in result
                   if isinstance(r, dict) and r.get("ticker")]
        if tickers:
            shown = ", ".join(tickers[:6])
            return f"{n} item{'s' if n != 1 else ''}: {shown}" + (" …" if n > 6 else "")
        return f"{n} item{'s' if n != 1 else ''}"
    if isinstance(result, dict):
        return "updated"
    if result is None:
        return "no-op"
    return "ok"


def _mark_brain_step(key: str, ok: bool, result=None, error: str = "") -> None:
    now = time.time()
    started = _BRAIN.get("current_step_started") or now
    _BRAIN["last_seen"] = now
    _BRAIN["current_step"] = None
    _BRAIN["current_step_started"] = None
    _BRAIN["steps"][key] = {
        "at": now,
        "started": started,
        "duration": round(now - started, 2),
        "ok": ok,
        "label": _BRAIN_STEP_LABELS.get(key, key.replace("_", " ")),
        "result": _step_summary(result) if ok else "failed",
        "error": error[:240],
    }


async def _run_brain_step(key: str, fn):
    timeout = _STEP_TIMEOUTS_SECONDS.get(key, _DEFAULT_STEP_TIMEOUT_SECONDS)
    try:
        _BRAIN["current_step"] = key
        _BRAIN["current_step_started"] = time.time()
        _BRAIN["last_seen"] = _BRAIN["current_step_started"]
        result = await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
        _mark_brain_step(key, True, result=result)
        return result
    except (asyncio.TimeoutError, TimeoutError):
        _mark_brain_step(
            key, False,
            error=f"timed out after {timeout}s — step exceeded its deadline, "
                  "loop moved on (likely a stalled network/LLM call)")
        raise
    except Exception as e:  # noqa: BLE001
        _mark_brain_step(key, False, error=str(e))
        raise


def _worker_health() -> dict:
    if config.AUTO_REFRESH_SECONDS <= 0:
        return {"status": "disabled", "label": "worker disabled", "stale": False}
    started = _BRAIN["started"]
    last_ok = _BRAIN["last_ok"]
    last_seen = _BRAIN.get("last_seen")
    current_step = _BRAIN.get("current_step")
    current_step_started = _BRAIN.get("current_step_started")
    ref = last_seen or last_ok or started
    if not ref:
        return {"status": "starting", "label": "starting", "stale": False}
    age = time.time() - ref
    stale_after = max(config.BRAIN_LOOP_SECONDS * 3, config.BRAIN_LOOP_SECONDS + 300)
    if not last_ok and age < stale_after:
        return {"status": "starting", "label": "starting", "stale": False,
                "started_at": _iso_ts(started)}
    current_age = time.time() - current_step_started if current_step_started else 0
    stale = age > stale_after
    working = bool(current_step and current_age <= stale_after)
    return {
        "status": "working" if working else ("stale" if stale else "running"),
        "label": f"working: {str(current_step).replace('_', ' ')}" if working
                 else ("needs attention" if stale else "running"),
        "stale": stale and not working,
        "started_at": _iso_ts(started),
        "cycle_started_at": _iso_ts(_BRAIN["cycle_started"]),
        "last_ok_at": _iso_ts(last_ok),
        "last_seen_at": _iso_ts(last_seen),
        "current_step": current_step or "",
        "current_step_started_at": _iso_ts(current_step_started),
        "last_error": _BRAIN["last_error"] or "",
        "cycle_count": _BRAIN["cycle_count"],
    }


def _brain_timeline() -> list[dict]:
    """Every brain step in pipeline order with its live state this cycle:
    ok / failed / running / pending. This is the scend.ai-style timeline the UI
    renders — click a row to see what that step did (or why it failed)."""
    cycle_started = _BRAIN.get("cycle_started") or 0
    current = _BRAIN.get("current_step")
    cur_started = _BRAIN.get("current_step_started")
    rows: list[dict] = []
    for key, _fn, label in _BRAIN_STEPS:
        rec = _BRAIN["steps"].get(key)
        ran_this_cycle = bool(rec and (rec.get("at") or 0) >= cycle_started)
        if key == current:
            status = "running"
        elif ran_this_cycle:
            status = "ok" if rec.get("ok") else "failed"
        else:
            status = "pending"
        row = {"key": key, "label": label, "status": status}
        if rec:
            row.update({
                "at": _iso_ts(rec.get("at")),
                "started_at": _iso_ts(rec.get("started")),
                "duration": rec.get("duration"),
                "result": rec.get("result", ""),
                "error": rec.get("error", ""),
                "from_prev_cycle": not ran_this_cycle and status != "running",
            })
        if status == "running" and cur_started:
            row["started_at"] = _iso_ts(cur_started)
            row["elapsed"] = round(time.time() - cur_started, 1)
        rows.append(row)
    return rows


def _autopilot_ops(payload: dict) -> dict:
    """Operational status for the local Autopilot worker. This is deliberately process-scoped:
    it tells the UI whether THIS running uvicorn process is actually ticking."""
    now = datetime.now(timezone.utc)
    trace = payload.get("decision_trace") or {}
    last_decision = _parse_iso(trace.get("created_at"))
    next_due = (last_decision + timedelta(hours=config.TWIN_DECIDE_HOURS)) if last_decision else now
    pending = payload.get("pending") or {}
    pending_count = int(pending.get("count") or 0)
    trades = payload.get("trades") or []

    if pending_count:
        decision_status = "waiting for fill"
        decision_detail = "orders are already queued; new decisions wait until that batch resolves"
    elif not last_decision:
        decision_status = "due now"
        decision_detail = "no Autopilot decision has been recorded yet"
    elif now >= next_due:
        decision_status = "due now"
        decision_detail = "cadence has elapsed; next brain tick can think again"
    else:
        decision_status = "scheduled"
        decision_detail = "inside the decision cooldown"

    if not trades:
        if pending_count:
            history_note = "Orders are queued but not filled yet, so History will show pending moves once the queue is written and filled/canceled states update."
        elif last_decision:
            history_note = "No moves yet because the latest Autopilot decision held the cloned book or queued nothing. After-hours only blocks fills, not History rows."
        else:
            history_note = "No moves yet because Autopilot has not made a recorded trade decision since this twin was started."
    else:
        history_note = ""

    steps = []
    for key, row in sorted((_BRAIN.get("steps") or {}).items(),
                           key=lambda kv: kv[1].get("at") or 0, reverse=True)[:8]:
        steps.append({"key": key, "at": _iso_ts(row.get("at")), "ok": row.get("ok", False),
                      "result": row.get("result", ""), "error": row.get("error", "")})

    worker = _worker_health()
    catchup = "ready"
    if worker.get("status") == "starting":
        catchup = "startup catch-up is beginning"
    elif decision_status == "due now" and not pending_count:
        catchup = "Autopilot is due; the next brain tick will attempt a decision"
    elif pending_count:
        catchup = "catch-up paused because an order batch is already queued"

    return {
        "worker": worker,
        "live_data": _refresh_health(),
        "decision": {
            "status": decision_status,
            "detail": decision_detail,
            "cadence_hours": config.TWIN_DECIDE_HOURS,
            "last_decision_at": last_decision.isoformat() if last_decision else "",
            "next_due_at": next_due.isoformat() if next_due else "",
            "due_now": now >= next_due and not pending_count,
        },
        "startup": {"server_started_at": _iso_ts(_BRAIN["started"]), "catchup": catchup},
        "history_note": history_note,
        "steps": steps,
        "timeline": _brain_timeline(),
    }


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
    payload = brain.twin_compare(refresh=refresh)
    if isinstance(payload, dict) and payload.get("started"):
        payload["ops"] = _autopilot_ops(payload)
    return payload


@app.get("/api/twin/ops")
def twin_ops():
    """Raw local worker diagnostics for Autopilot/background-loop debugging."""
    payload = brain.twin_compare(refresh=False)
    if isinstance(payload, dict) and payload.get("started"):
        return _autopilot_ops(payload)
    return {"worker": _worker_health(), "live_data": _refresh_health(), "started": False}


@app.post("/api/twin/start")
def twin_start():
    """Clone your real account into the Twin and start it (one-time)."""
    return {"twin": brain.twin_start()}


@app.post("/api/twin/decide")
def twin_decide():
    """Force an Autopilot decision cycle now (the tab's 'run a cycle' button)."""
    payload = brain.twin_decide_now()
    if isinstance(payload, dict) and payload.get("started"):
        payload["ops"] = _autopilot_ops(payload)
    return payload


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
        _BRAIN["cycle_started"] = time.time()
        _BRAIN["cycle_count"] = int(_BRAIN.get("cycle_count") or 0) + 1
        errors = []
        for key, fn, label in _BRAIN_STEPS:
            try:
                await _run_brain_step(key, fn)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{key}: {e}")
                logger.warning("%s failed: %s", label, e)
        _BRAIN["last_ok"] = time.time()
        _BRAIN["last_error"] = "; ".join(errors)[:500] if errors else None
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
        _BRAIN["started"] = time.time()
        asyncio.create_task(_refresh_loop())   # fast: live data only
        asyncio.create_task(_brain_loop())     # slow: LLM brain work, decoupled
    if config.AUTO_BRIEFINGS:
        asyncio.create_task(_briefing_loop())


# ----- static dashboard ----- #
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
