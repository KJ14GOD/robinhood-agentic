"""Agentic researcher — the multi-step investigation behind deep research.

Instead of one search-and-summarize pass, this runs a real research loop: the model plans its
questions, then investigates with tools — searching the live web AND reading primary SEC filings —
reflecting between steps, following leads, and converging only when the question is actually
answered. It returns a cited, corroborated research *dossier* (prose), which the deep-research
engine then synthesizes into a structured, self-critiqued call.

Two things make it frontier-grade rather than a glorified summary:
  1. Primary sources. It reads the actual 10-K/10-Q/8-K and reported XBRL financials via EDGAR,
     not just news *about* them.
  2. Epistemic honesty (corroboration/confidence). The system prompt forces it to cross-check
     load-bearing claims across independent sources, tag each conclusion with a confidence, and
     explicitly surface where sources disagree or evidence is thin — rather than sounding sure.

It mixes a server-side tool (web_search, resolved inline by the API) with client-side tools
(EDGAR + quant), so the loop handles both, plus the pause_turn / code-execution-container
mechanics that web search's result-filtering needs.
"""
from __future__ import annotations

from typing import Callable

from .. import llm
from ..data import edgar
from ..data.prices import clean_ticker, get_chart, get_signals

MAX_STEPS = 12

RESEARCH_SYSTEM = llm.SYSTEM_PROMPT + """

You are operating as a DEEP RESEARCH analyst with tools. This is the heavy, careful pass — act
like an analyst who will be graded on being right, not on sounding confident.

Method:
- Start by planning the few specific questions that actually decide this call.
- Investigate them with the tools. Go to PRIMARY SOURCES: read the company's SEC filings
  (read_sec_filing) and reported financials (get_company_financials), not just news about them.
  Use web_search for current events, the latest quarter, analyst views, catalysts, and risks.
- Follow leads. If an IPO, deal, or macro/policy event matters, trace its ripple to the names it
  actually affects (including ones the investor doesn't hold).
- CORROBORATE load-bearing claims across independent sources before you rely on them. When sources
  DISAGREE, say so explicitly and don't paper over it. Tag each key conclusion with a confidence
  (high / medium / low) and name what would raise it.
- Be honest about gaps: if the evidence is thin or stale, say that plainly.

Do NOT issue a final buy/sell/size here — a separate synthesis step does that. Your job is the
grounded, cited, corroborated evidence base it will reason over.

End your turn with a written RESEARCH DOSSIER in this shape:
- Questions: the specific questions you set out to answer.
- Findings: the key findings, each with its source(s) and a confidence tag.
- Primary sources: what the filings/financials actually showed (with figures).
- Disagreements & open questions: where sources conflict or evidence is thin.
- Narrative vs fundamentals: how the current story squares (or not) with the reported numbers."""


TOOLS = [
    llm.WEB_SEARCH_TOOL,
    {
        "name": "get_company_filings",
        "description": "List a company's recent SEC filings (form type, date, link). 8-K = material "
                       "event, 10-Q = quarterly, 10-K = annual. Use to see what was officially filed and when.",
        "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
    },
    {
        "name": "get_company_financials",
        "description": "The company's actual reported financials from SEC XBRL (revenue, net income, "
                       "margins, EPS, assets, equity) over recent periods. Primary-source numbers — prefer "
                       "these over figures quoted in articles.",
        "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
    },
    {
        "name": "read_sec_filing",
        "description": "Read a cleaned text excerpt of the company's most recent filing of a given form "
                       "(10-K, 10-Q, or 8-K) — anchored on Risk Factors / MD&A for periodic reports. Use to "
                       "read what the company itself actually disclosed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "form": {"type": "string", "enum": ["10-K", "10-Q", "8-K"]},
            },
            "required": ["ticker", "form"],
        },
    },
    {
        "name": "get_stock_signals",
        "description": "Quantitative signals for a ticker: price, market cap, P/E, beta, returns, "
                       "moving-average position, RSI, volatility.",
        "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
    },
    {
        "name": "get_stock_chart",
        "description": "Price-action summary for a ticker over a span (1m/3m/6m/1y).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "span": {"type": "string"}},
            "required": ["ticker"],
        },
    },
]


def _exec(name: str, inp: dict) -> str:
    try:
        tkr = clean_ticker(inp.get("ticker", "")) or inp.get("ticker", "")
        if name == "get_company_filings":
            return edgar.filings_as_prompt(tkr)
        if name == "get_company_financials":
            return edgar.facts_as_prompt(tkr)
        if name == "read_sec_filing":
            return edgar.filing_text_as_prompt(tkr, form=inp.get("form", "10-K"))
        if name == "get_stock_signals":
            return get_signals(tkr).as_prompt()
        if name == "get_stock_chart":
            return get_chart(tkr, inp.get("span", "6m")).summary()
        return f"Unknown tool: {name}"
    except Exception as e:  # noqa: BLE001 — a tool failure is a finding, not a crash
        return f"Tool error ({name}): {e}"


def _kickoff(ticker: str, profile, prior_thesis) -> str:
    prior = ""
    if prior_thesis:
        prior = (f"\n\nPRIOR STORED THESIS (test it, don't assume it): \"{prior_thesis.thesis}\"; "
                 f"it breaks if: \"{prior_thesis.invalidation}\".")
    return (f"Investigate {clean_ticker(ticker)} for this investor and build the evidence dossier.\n\n"
            f"INVESTOR: {profile.describe()}{prior}\n\n"
            f"Plan your questions, then investigate with the tools — read the primary SEC filings, "
            f"pull the reported financials, and search for the current picture. Corroborate, tag "
            f"confidence, and flag disagreements. Finish with the dossier.")


def investigate(ticker: str, profile, prior_thesis=None, max_steps: int = MAX_STEPS) -> dict:
    """Run the agentic investigation. Returns {"dossier", "trace", "tools_used"}."""
    cl = llm.client()
    system = [{"type": "text", "text": RESEARCH_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict] = [{"role": "user", "content": f"{llm.today_line()}\n\n{_kickoff(ticker, profile, prior_thesis)}"}]
    container_id: str | None = None
    trace: list[dict] = []
    tools_used: list[str] = []
    last_text = ""

    for _ in range(max_steps):
        resp = cl.messages.create(
            model=llm.MODEL, max_tokens=4000, system=system,
            thinking={"type": "adaptive"}, output_config={"effort": llm.EFFORT},
            tools=TOOLS, messages=messages,
            **({"container": container_id} if container_id else {}),
        )
        container_id = (getattr(resp, "container", None) and resp.container.id) or container_id
        messages.append({"role": "assistant", "content": resp.content})

        client_uses = [b for b in resp.content if b.type == "tool_use"]
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                last_text = "".join(x.text for x in resp.content if x.type == "text")
                trace.append({"type": "note", "text": b.text.strip()[:300]})
            elif b.type == "server_tool_use" and b.name == "web_search":
                tools_used.append("web_search")
                trace.append({"type": "tool", "name": "web_search", "input": dict(b.input or {})})
            elif b.type == "web_search_tool_result":
                hits = getattr(b, "content", None)
                hits = hits if isinstance(hits, list) else []
                trace.append({"type": "tool_result", "name": "web_search",
                              "summary": f"{len(hits)} result{'s' if len(hits) != 1 else ''}"})

        if resp.stop_reason == "pause_turn":
            continue  # server search loop hit its per-request cap — resume
        if resp.stop_reason != "tool_use" or not client_uses:
            return {"dossier": last_text, "trace": trace, "tools_used": sorted(set(tools_used))}

        results = []
        for tu in client_uses:
            tools_used.append(tu.name)
            trace.append({"type": "tool", "name": tu.name, "input": dict(tu.input or {})})
            out = _exec(tu.name, tu.input or {})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            trace.append({"type": "tool_result", "name": tu.name, "summary": out[:200]})
        messages.append({"role": "user", "content": results})

    # Exhausted the step budget mid-investigation — force a final dossier with no tools offered.
    try:
        messages.append({"role": "user", "content": "Stop investigating now and write your final research dossier from what you have."})
        resp = cl.messages.create(
            model=llm.MODEL, max_tokens=4000, system=system,
            thinking={"type": "adaptive"}, output_config={"effort": llm.EFFORT},
            messages=messages, **({"container": container_id} if container_id else {}),
        )
        final = "".join(b.text for b in resp.content if b.type == "text")
        if final:
            last_text = final
    except Exception:  # noqa: BLE001
        pass
    return {"dossier": last_text, "trace": trace, "tools_used": sorted(set(tools_used))}


_EXEC: Callable = _exec  # exported for tests/patching
