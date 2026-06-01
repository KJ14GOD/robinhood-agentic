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

from . import llm, profile_store, research_state, shadow
from .data.news import get_news
from .data.prices import get_chart, get_signals, screen_universe
from .data.universe import screening_universe
from .db import repository as db_repo
from .engines.discovery import _flavor_ok, _screen_score
from .models import TradeTicket
from .portfolio import get_portfolio

MAX_STEPS = 12

AGENT_SYSTEM = llm.SYSTEM_PROMPT + """

You are operating in AGENT MODE with tools. Work like a real analyst:
- Investigate before concluding. Pull the data you need — don't guess.
- When asked about the portfolio, read it, then dig into the specific holdings that matter.
- When hunting for ideas, screen the market, then pull signals/news on the standouts.
- When price action matters, fetch a chart and use it in the answer.
- Save only researched, genuinely useful ideas to watchlist memory; do not save every ticker mentioned.
- When you reach a real, decision-useful call on a ticker (a buy/add/hold/trim/sell with a conviction level), log it once with log_recommendation so the brain builds an honest, measurable track record. Never log passing mentions or hypotheticals.
- Chain tools as needed. Be efficient: don't pull data you won't use.
- End with a clear, decision-useful answer grounded in what the tools returned.
- Format final answers as:
  **Read:** one sentence.
  **Evidence:** 2-4 bullets.
  **Action:** 1-4 concrete actions or non-actions.
  **Watch:** optional, only if there is a specific trigger/level/event.
- You proactively surface findings the user didn't explicitly ask for but should know
  (a concentration risk, a holding breaking down, a standout opportunity).

USING WEB SEARCH:
- The quantitative tools (signals, screen, chart) give you numbers; web_search gives you the
  live story. Use web_search whenever the answer depends on what's happening *now* — recent
  news, an earnings reaction, a policy/Fed/Trump comment, an IPO and its ripple to related
  names, a sector catalyst, or anything time-sensitive. Don't answer from memory when current
  information would change the call; search first.
- Weight sources by trust, and say which tier a claim rests on:
  TIER 1 (treat as ground truth): SEC filings / company IR / official releases.
  TIER 2 (reputable reporting): Reuters, Bloomberg, WSJ, FT, CNBC, Barron's, AP.
  TIER 3 (sentiment/color only, never a standalone fact): blogs, forums, Seeking Alpha, smaller outlets.
- Never treat a single headline or post as fact. Corroborate, and cite the source for any
  claim that moves your conclusion so it can be checked."""

# Server-side web search. The API runs the search and returns results inline; we never execute
# it. Bounded by max_uses; a small blocklist kills the worst pump/SEO-farm noise without caging
# reach (we steer toward trusted sources by prompt, not a hard allowlist — see AGENT_SYSTEM).
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 6,
    "blocked_domains": ["zacks.com", "fool.com", "investorplace.com", "stocktwits.com"],
}


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
        "description": "Persistent watchlist, stored theses, and invalidation rules "
                       "from prior recommendations. Use this before judging holdings or "
                       "continuing an older investment idea.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_activity",
        "description": "The brain's own logged observations from the always-on monitor and "
                       "memory engine (the Activity feed): concentration, drawdowns, RSI/trend "
                       "signals, thesis status changes, target hits. Read this to ground answers "
                       "in what the brain has already noticed, rather than re-deriving it.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many recent events (max 40)"}},
        },
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
    {
        "name": "log_recommendation",
        "description": "Record a concrete recommendation to the shadow track record so it can be "
                       "graded later against the market and its sector. Use ONLY when you've reached "
                       "a real, decision-useful call on a ticker (a buy/add/hold/trim/sell with a "
                       "conviction level) — never for passing mentions or hypotheticals. This is how "
                       "the brain builds an honest, measurable record of whether its calls actually work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "action": {"type": "string", "enum": ["buy", "add", "hold", "trim", "sell", "watch"]},
                "conviction": {"type": "integer", "description": "1=weak, 10=table-pounding"},
                "thesis": {"type": "string", "description": "The actual reasoning, 1-3 sentences."},
                "catalyst": {"type": "string", "description": "What could make it move, and roughly when."},
                "risks": {"type": "string", "description": "What would break the thesis."},
            },
            "required": ["ticker", "action", "conviction", "thesis"],
        },
    },
    WEB_SEARCH_TOOL,
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


def _tool_activity(limit: int = 15) -> str:
    evs = db_repo.recent_events(limit=max(1, min(int(limit or 15), 40)), within_hours=168.0)
    if not evs:
        return "No logged activity in the last week."
    lines = ["Recent brain-logged activity (newest first):"]
    for e in evs:
        tk = (e.get("ticker") + " ") if e.get("ticker") else ""
        lines.append(f"- [{e.get('severity', 'info')}] {tk}{e.get('title', '')}. {e.get('summary', '')}".rstrip())
    return "\n".join(lines)


def _tool_chart(ticker: str, span: str = "3m") -> str:
    return get_chart(ticker, span).summary()


def _tool_save_watchlist(ticker: str, reason: str, mode: str = "balanced",
                         max_allocation_pct: float = 0.0) -> str:
    research_state.save_watch_item(ticker, reason, mode, max_allocation_pct)
    size = f", max allocation {max_allocation_pct:.1f}%" if max_allocation_pct else ""
    return f"Saved {ticker.upper()} to watchlist as {mode}{size}: {reason}"


def _tool_log_recommendation(ticker: str, action: str, conviction: int, thesis: str,
                             catalyst: str = "", risks: str = "") -> str:
    tkr = ticker.upper().strip()
    if shadow.has_open(tkr, source="assistant"):
        return (f"{tkr} already has an open recommendation in the track record — "
                "leaving it; not double-logging.")
    ticket = TradeTicket(
        ticker=tkr, action=action, conviction=int(conviction),
        thesis=thesis, catalyst=catalyst or "", risks=risks or "",
    )
    signals = get_signals(tkr)
    trade = shadow.log_recommendation(
        ticket, source="assistant", profile=profile_store.load_profile(), signals=signals,
    )
    return (f"Logged {tkr} as {ticket.decision_label} (conviction {ticket.conviction}) to the "
            f"shadow track record at ${trade.entry_price:.2f}, anchored to SPY"
            f"{(' and ' + trade.sector_etf) if trade.sector_etf else ''}.")


_DISPATCH: dict[str, Callable[..., str]] = {
    "get_stock_signals": _tool_get_signals,
    "get_stock_news": _tool_get_news,
    "screen_market": _tool_screen,
    "get_my_portfolio": _tool_portfolio,
    "get_my_profile": _tool_profile,
    "get_research_memory": _tool_memory,
    "get_recent_activity": _tool_activity,
    "get_stock_chart": _tool_chart,
    "save_watchlist_item": _tool_save_watchlist,
    "log_recommendation": _tool_log_recommendation,
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
    # Anchor "now" so the model frames recency and web-search queries around today, not its
    # training-era assumption of an earlier year. Goes in the message, not the cached system block.
    messages.append({"role": "user", "content": f"{llm.today_line()}\n\n{message}"})

    # Accumulate a compact, faithful trace so the whole loop is persisted to
    # agent_runs as an audit trail (what the brain looked at, and why it answered).
    trace: list[dict] = []
    tools_used: list[str] = []

    def _persist(answer: str) -> None:
        try:
            db_repo.save_agent_run(
                query=message, answer=answer, kind="chat",
                steps=trace, tools_used=",".join(dict.fromkeys(tools_used)),
                model=llm.MODEL,
            )
        except Exception:  # noqa: BLE001 — the audit trail must never break the answer
            pass

    # web_search_20260209 filters results via a server-side code-execution container. When the
    # search loop spans more than one request (pause_turn), the follow-up must reference that same
    # container, or the API 400s with "container_id is required". Carry it across iterations.
    container_id: str | None = None

    for _ in range(MAX_STEPS):
        resp = client.messages.create(
            model=llm.MODEL,
            max_tokens=4000,
            system=[{"type": "text", "text": AGENT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": llm.EFFORT},
            tools=TOOLS,
            messages=messages,
            **({"container": container_id} if container_id else {}),
        )
        container_id = getattr(resp, "container", None) and resp.container.id or container_id
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        # surface interim text + server-side web searches (the search runs on the API and its
        # results are already inline in this same response — we only narrate it to the UI).
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                trace.append({"type": "note", "text": b.text.strip()})
                yield {"type": "note", "text": b.text.strip()}
            elif b.type == "server_tool_use" and b.name == "web_search":
                query = (b.input or {}).get("query", "")
                tools_used.append("web_search")
                trace.append({"type": "tool", "name": "web_search", "input": {"query": query}})
                yield {"type": "tool", "name": "web_search", "input": {"query": query}}
            elif b.type == "web_search_tool_result":
                hits = b.content if isinstance(b.content, list) else []
                summary = f"{len(hits)} result{'s' if len(hits) != 1 else ''}" + (
                    f": {hits[0].title}" if hits and getattr(hits[0], "title", None) else "")
                trace.append({"type": "tool_result", "name": "web_search", "summary": summary})
                yield {"type": "tool_result", "name": "web_search", "summary": summary}

        # The server-side search loop paused at its iteration cap — re-send to let it resume
        # (the trailing server_tool_use block in the appended assistant content signals resume).
        if resp.stop_reason == "pause_turn":
            continue

        if resp.stop_reason != "tool_use" or not tool_uses:
            final = "".join(b.text for b in resp.content if b.type == "text")
            _persist(final)
            yield {"type": "answer", "text": final}
            return

        results = []
        for tu in tool_uses:
            tools_used.append(tu.name)
            trace.append({"type": "tool", "name": tu.name, "input": tu.input})
            yield {"type": "tool", "name": tu.name, "input": tu.input}
            out = _execute(tu.name, tu.input)
            if tu.name == "get_stock_chart":
                chart = get_chart(tu.input.get("ticker", ""), tu.input.get("span", "3m"))
                yield {"type": "chart", "chart": chart.model_dump()}
            summary = out[:240] + ("…" if len(out) > 240 else "")
            trace.append({"type": "tool_result", "name": tu.name, "summary": summary})
            yield {"type": "tool_result", "name": tu.name, "summary": summary}
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})

    limit_msg = "(Reached the step limit — here's what I found above.)"
    _persist(limit_msg)
    yield {"type": "answer", "text": limit_msg}


def run(message: str, history: list[dict] | None = None) -> dict:
    """Non-streaming convenience wrapper. Returns {answer, steps}."""
    steps, answer = [], ""
    for ev in run_stream(message, history):
        if ev["type"] == "answer":
            answer = ev["text"]
        else:
            steps.append(ev)
    return {"answer": answer, "steps": steps}
