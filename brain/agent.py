"""The agentic core — a tool-use loop where the model drives its own research.

Instead of a fixed pipeline (gather → one call → return), the model is given
tools and decides what to investigate: it can pull signals, read news, screen
the whole market, and inspect your portfolio/profile, looping until it has
enough to answer. Every tool call is captured as a "step" so the UI can show
what the brain is actually doing in real time.

This is what makes the system agentic rather than a scripted workflow.
"""
from __future__ import annotations

import json
from typing import Callable, Iterator

from . import llm, profile_store, research_state
from .data.news import get_news
from .data.prices import get_chart, get_signals, screen_universe
from .data.universe import screening_universe
from .engines.discovery import _flavor_ok, _screen_score
from .portfolio import get_portfolio

MAX_STEPS = 12

AGENT_SYSTEM = llm.SYSTEM_PROMPT + """

You are operating in AGENT MODE with tools. Work like a real analyst:
- Investigate before concluding. Pull the data you need — don't guess.
- When asked about the portfolio, read it, then dig into the specific holdings that matter.
- When hunting for ideas, screen the market, then pull signals/news on the standouts.
- When price action matters, fetch a chart and use it in the answer.
- Save only researched, genuinely useful ideas to watchlist memory; do not save every ticker mentioned.
- Chain tools as needed. Be efficient: don't pull data you won't use.
- End with a clear, decision-useful answer grounded in what the tools returned.
- Format final answers as:
  **Read:** one sentence.
  **Evidence:** 2-4 bullets.
  **Action:** 1-4 concrete actions or non-actions.
  **Watch:** optional, only if there is a specific trigger/level/event.
- You proactively surface findings the user didn't explicitly ask for but should know
  (a concentration risk, a holding breaking down, a standout opportunity)."""


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "get_stock_signals",
        "description": "Quantitative signals for one ticker: price, market cap, P/E, "
                       "beta, dividend yield, 1m/3m/6m returns, moving-average position, "
                       "RSI, volatility. Use to evaluate any specific stock.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker, e.g. NVDA"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_stock_news",
        "description": "Recent news headlines for a ticker. Use to understand catalysts "
                       "or what's driving a move.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "screen_market",
        "description": "Run a momentum/trend screen across the ~520-stock universe and return "
                       "the top-ranked candidates. flavor: 'stable' (low volatility), "
                       "'volatile' (high upside/risk), or 'any'. Use to find new ideas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flavor": {"type": "string", "enum": ["stable", "volatile", "any"]},
                "limit": {"type": "integer", "description": "How many candidates to return (max 15)"},
            },
            "required": ["flavor"],
        },
    },
    {
        "name": "get_my_portfolio",
        "description": "The user's current holdings: tickers, quantities, weights, "
                       "unrealized P&L, cash, total value.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_profile",
        "description": "The user's risk personality and preferences. Read this to tailor "
                       "recommendations to who they are.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_research_memory",
        "description": "Persistent watchlist, stored theses, invalidation rules, and alerts "
                       "from prior recommendations. Use this before judging holdings or "
                       "continuing an older investment idea.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stock_chart",
        "description": "Fetch chart data for a ticker and render an inline chart in chat. "
                       "Use this when the user asks about price action, entries, pullbacks, "
                       "momentum, support, or wants to see a graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "span": {"type": "string", "enum": ["1d", "1w", "1m", "3m", "6m", "1y"]},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "save_watchlist_item",
        "description": "Persist a stock idea to the user's watchlist/research memory. "
                       "Use only when an idea should be tracked after analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "reason": {"type": "string"},
                "mode": {"type": "string", "enum": ["stable", "balanced", "volatile"]},
                "max_allocation_pct": {"type": "number"},
            },
            "required": ["ticker", "reason"],
        },
    },
]


def _tool_get_signals(ticker: str) -> str:
    return get_signals(ticker).as_prompt()


def _tool_get_news(ticker: str) -> str:
    hs = get_news(ticker, limit=6)
    if not hs:
        return f"No recent headlines for {ticker}."
    return "\n".join(f"- {h.title} ({h.publisher})" for h in hs)


def _tool_screen(flavor: str = "any", limit: int = 10) -> str:
    limit = max(1, min(int(limit or 10), 15))
    rows = [r for r in screen_universe(screening_universe()) if _flavor_ok(r, flavor)]
    rows.sort(key=_screen_score, reverse=True)
    out = [f"Top {flavor} momentum candidates:"]
    for r in rows[:limit]:
        out.append(f"- {r.ticker}: ${r.price:.2f}, 3m {r.ret_3m_pct:+.0f}% / 6m {r.ret_6m_pct:+.0f}%, "
                   f"RSI {r.rsi_14:.0f}, vol {r.vol_annualized_pct:.0f}%, "
                   f"{'>' if r.above_200d else '<'}200d")
    return "\n".join(out)


def _tool_portfolio() -> str:
    pf = get_portfolio()
    if not pf.holdings:
        return "Portfolio is empty (no holdings entered yet)."
    w = pf.weights()
    lines = [f"Total ${pf.total_value:,.0f}, cash ${pf.cash:,.0f}:"]
    for h in sorted(pf.holdings, key=lambda x: x.market_value, reverse=True):
        upnl = h.unrealized_pct
        lines.append(f"- {h.ticker}: {w.get(h.ticker,0):.1f}% weight, "
                     f"{h.quantity:g} sh @ ${h.current_price:.2f}, "
                     f"unrealized {upnl:+.1f}%" if upnl is not None else
                     f"- {h.ticker}: {w.get(h.ticker,0):.1f}% weight")
    return "\n".join(lines)


def _tool_profile() -> str:
    return profile_store.load_profile().describe()


def _tool_memory() -> str:
    return research_state.summarize_for_prompt()


def _tool_chart(ticker: str, span: str = "3m") -> str:
    return get_chart(ticker, span).summary()


def _tool_save_watchlist(ticker: str, reason: str, mode: str = "balanced",
                         max_allocation_pct: float = 0.0) -> str:
    research_state.save_watch_item(ticker, reason, mode, max_allocation_pct)
    size = f", max allocation {max_allocation_pct:.1f}%" if max_allocation_pct else ""
    return f"Saved {ticker.upper()} to watchlist as {mode}{size}: {reason}"


_DISPATCH: dict[str, Callable[..., str]] = {
    "get_stock_signals": _tool_get_signals,
    "get_stock_news": _tool_get_news,
    "screen_market": _tool_screen,
    "get_my_portfolio": _tool_portfolio,
    "get_my_profile": _tool_profile,
    "get_research_memory": _tool_memory,
    "get_stock_chart": _tool_chart,
    "save_watchlist_item": _tool_save_watchlist,
}


def _execute(name: str, tool_input: dict) -> str:
    fn = _DISPATCH.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(**tool_input)
    except Exception as e:  # noqa: BLE001
        return f"Tool error ({name}): {e}"


# --------------------------------------------------------------------------- #
# Agent loop (streaming) — yields events the UI can render live
# --------------------------------------------------------------------------- #
def run_stream(message: str, history: list[dict] | None = None) -> Iterator[dict]:
    """Run the agent, yielding events:
      {"type":"thinking"}                      — model is reasoning
      {"type":"tool", "name":..., "input":...} — about to investigate
      {"type":"tool_result", "name":..., "summary":...}
      {"type":"answer", "text":...}            — final answer
    """
    client = llm.client()
    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": message})

    for _ in range(MAX_STEPS):
        resp = client.messages.create(
            model=llm.MODEL,
            max_tokens=4000,
            system=[{"type": "text", "text": AGENT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": llm.EFFORT},
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        # surface any interim text the model wrote alongside its tool calls
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                yield {"type": "note", "text": b.text.strip()}

        if resp.stop_reason != "tool_use" or not tool_uses:
            final = "".join(b.text for b in resp.content if b.type == "text")
            yield {"type": "answer", "text": final}
            return

        results = []
        for tu in tool_uses:
            yield {"type": "tool", "name": tu.name, "input": tu.input}
            out = _execute(tu.name, tu.input)
            if tu.name == "get_stock_chart":
                chart = get_chart(tu.input.get("ticker", ""), tu.input.get("span", "3m"))
                yield {"type": "chart", "chart": chart.model_dump()}
            yield {"type": "tool_result", "name": tu.name,
                   "summary": out[:240] + ("…" if len(out) > 240 else "")}
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})

    yield {"type": "answer", "text": "(Reached the step limit — here's what I found above.)"}


def run(message: str, history: list[dict] | None = None) -> dict:
    """Non-streaming convenience wrapper. Returns {answer, steps}."""
    steps, answer = [], ""
    for ev in run_stream(message, history):
        if ev["type"] == "answer":
            answer = ev["text"]
        else:
            steps.append(ev)
    return {"answer": answer, "steps": steps}
