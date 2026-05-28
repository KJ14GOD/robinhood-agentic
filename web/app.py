"""FastAPI backend for the dashboard.

Run:  uvicorn web.app:app --reload
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

from pathlib import Path

import json

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brain import orchestrator as brain
from brain.models import RiskProfile
from brain.portfolio import manual as manual_pf
from brain import config

app = FastAPI(title="Stock Research Brain")
STATIC = Path(__file__).parent / "static"


# ----- request bodies ----- #
class ChatBody(BaseModel):
    message: str
    history: list[dict] = []


class AnalyzeBody(BaseModel):
    ticker: str


class DiscoverBody(BaseModel):
    flavor: str = "any"
    top_n: int = 5


class FeedbackBody(BaseModel):
    ticker: str
    accepted: bool


class HoldingsBody(BaseModel):
    holdings: list[dict]
    cash: float = 0.0


# ----- API ----- #
@app.get("/api/state")
def state():
    pf = brain.portfolio()
    weights = pf.weights()
    return {
        "source": config.PORTFOLIO_SOURCE,
        "profile": brain.get_profile().model_dump(),
        "portfolio": {
            "total_value": pf.total_value,
            "cash": pf.cash,
            "holdings": [
                {**h.model_dump(), "market_value": h.market_value,
                 "weight": weights.get(h.ticker, 0.0),
                 "unrealized_pct": h.unrealized_pct}
                for h in pf.holdings
            ],
        },
    }


@app.post("/api/profile")
def save_profile(profile: RiskProfile):
    return brain.update_profile(profile).model_dump()


@app.post("/api/holdings")
def save_holdings(body: HoldingsBody):
    if config.PORTFOLIO_SOURCE != "manual":
        return {"error": "Holdings are read from Robinhood; switch PORTFOLIO_SOURCE=manual to edit here."}
    manual_pf.save_portfolio(body.holdings, body.cash)
    return state()


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
    return brain.analyze(body.ticker).model_dump()


@app.post("/api/discover")
def discover(body: DiscoverBody):
    return brain.discover(flavor=body.flavor, top_n=body.top_n).model_dump()


@app.get("/api/digest")
def digest():
    return brain.daily_digest().model_dump()


@app.get("/api/feed")
def feed():
    return brain.feed().model_dump()


@app.get("/api/scoreboard")
def scoreboard():
    return brain.scoreboard()


@app.post("/api/feedback")
def feedback(body: FeedbackBody):
    return brain.feedback(body.ticker, body.accepted).model_dump()


@app.post("/api/learn")
def learn():
    """Re-read the real portfolio into the learned investor signature."""
    return brain.refresh_learning().model_dump()


# ----- static dashboard ----- #
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
