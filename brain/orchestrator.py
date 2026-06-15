"""The brain's public API — what the dashboard and CLI call.

Ties together profile + portfolio + data + engines + shadow ledger. This is the
single import surface for everything above the engine layer.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Iterator

from . import agent, llm, mandate as _mandate, profile_store, research_state, shadow
from .data.news import clear_news_cache
from .data.prices import clear_caches, get_chart, get_portfolio_chart, get_quote
from .data import robinhood_charts
from .db import repository as db_repo
from .engines import analyst, autoresearch, briefing, discovery, evaluation, findings, judge, memory, missions, monitor, twin
from .engines import deep_research as _deep_research
from .engines import structural_risk as _structural_risk
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


# Structural risk is an LLM clustering pass — gated like findings, but on a coarser signature
# (the set of holdings + weights rounded to whole %), since cluster membership only shifts when
# allocations actually change, not on every tick.
_RISK_CACHE: dict = {"result": None, "at": 0.0, "sig": None}
_RISK_TTL = 6 * 3600.0


def _risk_signature(pf: Portfolio) -> tuple:
    w = pf.weights()
    return tuple(sorted((h.ticker, round(w.get(h.ticker, 0.0))) for h in pf.holdings))


def structural_risk(force: bool = False):
    """Portfolio-level correlated-bet read. Cached + signature-gated so it only re-clusters when
    the allocation actually shifts (or the TTL lapses) — a calm book costs nothing."""
    pf = get_portfolio()
    sig = _risk_signature(pf)
    now = time.time()
    cached = _RISK_CACHE["result"]
    if (not force and cached is not None and _RISK_CACHE["sig"] == sig
            and now - _RISK_CACHE["at"] < _RISK_TTL):
        return cached
    result = _structural_risk.analyze(pf, get_profile(), research_state.load_state().theses)
    _RISK_CACHE.update(result=result, at=now, sig=sig)
    return result


def run_structural_risk():
    """Background entry: compute the structural read (cached) and drop a cooldowned ping if the
    book is concentrated. No-op cost when allocation is unchanged."""
    if not get_portfolio().holdings:
        return None
    result = structural_risk()
    _structural_risk.maybe_alert(result)
    return result


# ---------- social sentiment (quarantined, secondary signal) ----------
_SENT_CACHE: dict = {"at": 0.0}
_CAT_CACHE: dict = {"at": 0.0}


def _social_universe() -> set[str]:
    """The names worth listening for: everything held, watched, or tracked in a mission."""
    uni: set[str] = set()
    try:
        uni.update(h.ticker.upper() for h in get_portfolio().holdings)
    except Exception:
        pass
    try:
        state = research_state.load_state()
        uni.update(w.ticker.upper() for w in state.watchlist)
        uni.update(t.upper() for t in state.theses.keys())
    except Exception:
        pass
    try:
        for m in list_missions(status="active"):
            uni.update(c.ticker.upper() for c in m.candidates)
    except Exception:
        pass
    return {t for t in uni if t}


def ingest_sentiment() -> int:
    """Check social buzz on the user's names and ping when a name's Reddit mentions
    spike. One cheap ApeWisdom call (cached), gated to SENTIMENT_TTL_SECONDS, fully
    quarantined. Per-ticker StockTwits mood is fetched on demand by the analysis
    prompt, not here. Returns the number of buzz events fired."""
    from .data import sentiment
    if not sentiment.available():
        return 0
    now = time.time()
    if now - _SENT_CACHE["at"] < config.SENTIMENT_TTL_SECONDS:
        return 0
    _SENT_CACHE["at"] = now
    universe = _social_universe()
    if not universe:
        return 0
    trending = sentiment.apewisdom_map()
    fired = 0
    for tk in universe:
        x = trending.get(tk)
        if not x:
            continue
        mentions = int(x.get("mentions", 0) or 0)
        prev = int(x.get("mentions_24h_ago", 0) or 0)
        if not prev:
            continue
        delta = round((mentions - prev) / prev * 100)
        if delta >= config.SENTIMENT_BUZZ_PCT and mentions >= config.SENTIMENT_BUZZ_MIN:
            if not db_repo.event_exists_recent("social_buzz", tk, 12.0):
                db_repo.save_research_event(
                    event_type="social_buzz", ticker=tk, severity="info",
                    title=f"{tk} buzzing on Reddit — mentions {delta:+d}%",
                    summary=f"{mentions} mentions today vs {prev} yesterday — social chatter "
                            "spiking. A lead to look at, not a signal on its own.",
                    source="sentiment")
                fired += 1
    return fired


def _owned_or_watched() -> set[str]:
    """Names you hold or watch — catalysts on these ping loudly (warn); research-only
    names (theses, missions) log quietly (info)."""
    s: set[str] = set()
    try:
        s.update(h.ticker.upper() for h in get_portfolio().holdings)
    except Exception:
        pass
    try:
        s.update(w.ticker.upper() for w in research_state.load_state().watchlist)
    except Exception:
        pass
    return s


_PAREN_TICKER = re.compile(r"\(([A-Z]{1,5})\)")


def _about_another_company(headline: str, tk: str) -> bool:
    """Finnhub tags any article that mentions a symbol, so a headline can really be about
    a *different* company. When the headline names tickers in parens (e.g. 'Reddit (RDDT)')
    and our ticker isn't among them, it's about that other name — skip it as off-target."""
    found = {m.upper() for m in _PAREN_TICKER.findall(headline or "")}
    return bool(found) and tk.upper() not in found


def ingest_catalysts() -> int:
    """Catalyst radar: scan the user's names for fresh company news (Finnhub) and ping
    when something material just landed. One cheap HTTP call per name (cached), gated to
    FINNHUB_TTL_SECONDS, fully quarantined (no-op with no key). Per-name cooldown keeps
    it from spamming; off-target headlines (about a different company) and headlines already
    surfaced for another name this scan are filtered out. Returns the number of pings fired."""
    from .data import catalysts
    if not catalysts.available():
        return 0
    now = time.time()
    if now - _CAT_CACHE["at"] < config.FINNHUB_TTL_SECONDS:
        return 0
    _CAT_CACHE["at"] = now
    universe = _social_universe()
    if not universe:
        return 0
    loud = _owned_or_watched()
    seen_headlines: set[str] = set()  # dedup the same article across names this scan
    fired = 0
    for tk in universe:
        if db_repo.event_exists_recent("catalyst", tk, config.FINNHUB_COOLDOWN_HOURS):
            continue  # already surfaced a catalyst for this name recently
        # first fresh item that's actually about THIS name and not already used
        c = None
        for cand in catalysts.fresh_items(tk, config.FINNHUB_FRESH_HOURS):
            key = (cand.headline or "").strip().lower()
            if not key or key in seen_headlines or _about_another_company(cand.headline, tk):
                continue
            c = cand
            break
        if not c:
            continue
        seen_headlines.add((c.headline or "").strip().lower())
        age = int(c.age_hours)
        when = "just now" if age < 1 else f"{age}h ago"
        src = f" · {c.source}" if c.source else ""
        db_repo.save_research_event(
            event_type="catalyst", ticker=tk,
            severity="warn" if tk in loud else "info",
            title=f"{tk}: {c.headline}"[:160],
            summary=f"{(c.summary or 'Fresh news on a name you track.')[:200]} ({when}{src}) {c.url}".strip(),
            source="catalysts")
        if c.url:  # also fold into the unified evidence store
            db_repo.record_evidence(tk, [{"url": c.url, "title": c.headline,
                                          "source": c.source, "snippet": c.summary}],
                                    kind="catalyst", engine="catalysts")
        fired += 1
    return fired


def run_monitors() -> list[dict]:
    """Cheap deterministic event scan over the live portfolio. Persists new,
    deduped events to the DB. Called by the background loop — no LLM, no tokens."""
    return monitor.run_monitors(get_portfolio(), get_profile())


def revisit_memory() -> list[dict]:
    """Living memory: re-judge triggered theses against their invalidation and move
    them forward. Gated (only fires on a real trigger, once/day/name), so it spends
    nothing on a calm book. Returns the theses that changed."""
    return memory.revisit_theses(get_portfolio(), get_profile())


# Events whose ping/activity row corresponds to a reasoning trace the judge has scored — so the
# feed can deep-link straight to the judge's read of it. (deep_dive -> the autonomous deep dive;
# thesis_* -> the living-memory re-judgement that produced the event.)
_EVENT_TRACE_KIND = {
    "deep_dive": "deep_research",
    "thesis_broken": "rejudge", "thesis_review": "rejudge",
    "thesis_active": "rejudge", "thesis_affirmed": "rejudge",
}


def _attach_judgements(events: list[dict]) -> None:
    """Hang the judge's read onto each event whose trace it scored (matched by ticker + kind).
    One batched query; best-effort, so a miss just leaves the event un-linked."""
    linked = {e["ticker"].upper() for e in events
              if e.get("ticker") and e.get("event_type") in _EVENT_TRACE_KIND}
    if not linked:
        return
    jmap = db_repo.judgements_for_tickers(list(linked))
    if not jmap:
        return
    for e in events:
        kind = _EVENT_TRACE_KIND.get(e.get("event_type"))
        if kind and e.get("ticker"):
            j = jmap.get((e["ticker"].upper(), kind))
            if j:
                e["judgement"] = j


def today_events(limit: int = 40, within_hours: float = 72.0) -> dict:
    """The persisted event stream for the Today surface, newest first, ranked so
    the loudest (alert) items lead. Reads the DB — does not trigger a scan."""
    rank = {"alert": 0, "warn": 1, "info": 2}
    events = db_repo.recent_events(limit=limit, within_hours=within_hours)
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)   # newest first
    events.sort(key=lambda e: rank.get(e.get("severity"), 3))          # stable: alert → warn → info
    _attach_judgements(events)
    return {"events": events, "count": len(events)}


# --- shadow mode ------------------------------------------------------------ #
def scoreboard(refresh: bool = False) -> dict:
    return shadow.scoreboard(refresh=refresh)


def reconcile_duplicate(trade_id: str, mode: str) -> dict:
    """Resolve a duplicate shadow re-call ('replace' the older or 'keep' both)."""
    return shadow.reconcile_duplicate(trade_id, mode)


def scorecard(refresh: bool = False) -> dict:
    """The evaluation layer: graded recommendations — calibration, attribution,
    and benchmark-relative scoring. This is what proves whether the brain has edge."""
    return evaluation.scorecard(refresh=refresh)


def agent_runs(limit: int = 20, kind: str | None = None) -> list[dict]:
    """The audit trail: recent agent loops with their tool traces. Reads the DB."""
    return db_repo.recent_agent_runs(limit=limit, kind=kind)


def evidence(ticker: str, limit: int = 30) -> list[dict]:
    """The unified evidence the brain has gathered on a ticker (web + catalysts), deduped."""
    return db_repo.evidence_for(ticker, limit=limit)


# --- eval layer (error analysis on the brain's own traces) ------------------ #
def eval_taxonomy() -> list[dict]:
    from . import evals
    return evals.taxonomy()


def eval_traces(limit: int = 30, kind: str | None = None) -> list[dict]:
    """Recent reviewable brain traces (analyst / re-judge / deep research) with any label
    already attached — the worklist for error analysis."""
    runs = db_repo.recent_agent_runs(limit=limit, kind=kind)
    if not kind:  # default to the reasoning products, not chat
        runs = [r for r in runs if r.get("kind") in ("analyst", "rejudge", "deep_research")]
    ids = [r["id"] for r in runs]
    labels = db_repo.eval_labels_by_run(ids)            # human ground truth
    judgements = db_repo.eval_judgements_by_run(ids)    # the auto-judge's read
    for r in runs:
        r["label"] = labels.get(r["id"])
        r["judgement"] = judgements.get(r["id"])
    return runs


def save_eval_label(run_id: str, kind: str, ticker: str, verdict: str,
                    failure_modes: list[str], note: str = "") -> bool:
    from . import evals
    tags = [evals.normalize_tag(t) for t in (failure_modes or []) if t and t.strip()]
    return db_repo.save_eval_label(run_id, kind, ticker, verdict, tags, note)


def eval_summary() -> dict:
    """The emerging eval suite — labeled count, verdict split, ranked failure modes (with
    human-readable labels)."""
    from . import evals
    s = db_repo.eval_summary()
    for row in s.get("failure_counts", []):
        row["label"] = evals.pretty(row["tag"])
    return s


def judge_summary() -> dict:
    """The auto eval suite — the LLM-as-judge's continuous quality score (verdict split, avg
    score, ranked failure modes, # self-revised, agreement with your labels). Mirrors
    eval_summary but for the machine judgements."""
    from . import evals
    s = db_repo.judge_summary()
    for row in s.get("failure_counts", []):
        row["label"] = evals.pretty(row["tag"])
    return s


def judge_recent_traces(limit: int | None = None) -> int:
    """Background sweep: auto-score recent reasoning traces the inline gate didn't (mainly the
    autonomous re-judge path, and anything produced while judging was off). Bounded per cycle,
    best-effort. Returns how many it judged this pass."""
    if not config.JUDGE_ENABLED:
        return 0
    pending = db_repo.unjudged_run_ids(limit=limit or config.JUDGE_SWEEP_MAX)
    if not pending:
        return 0
    profile = get_profile()
    mandate_block = _mandate.mandate_prompt()
    n = 0
    for r in pending:
        block, ticker = judge.block_from_trace(r)
        if not block:
            continue
        a = judge.assess_text(
            r["kind"], ticker, block, profile,
            signals_prompt="(historical trace — judge from the captured reasoning and cited evidence)",
            evidence_text=r.get("answer", ""), sources=judge.sources_from_trace(r),
            mandate_block=mandate_block)
        if a is not None:
            step = (r.get("steps") or [{}])[0]
            judge.record(r["id"], r["kind"], ticker, a, revised=bool(step.get("revised")))
            n += 1
    return n


# --- mandate (the standing goal — the agentic cockpit) ---------------------- #
_MANDATE_REVIEW: dict = {"sig": None, "review": None}


def get_mandate() -> dict:
    return _mandate.get_mandate().model_dump()


def set_mandate(text: str) -> dict:
    m = _mandate.set_mandate(text)
    _MANDATE_REVIEW["sig"] = None   # a new goal invalidates the cached plan
    return m.model_dump()


def mandate_review(force: bool = False) -> dict | None:
    """The advisor read of the portfolio against the mandate. Cached on (mandate + holdings)
    so it only re-spends when the goal or the book actually changes."""
    m = _mandate.get_mandate()
    if not m.is_set():
        return None
    pf = get_portfolio()
    sig = (m.updated_at, _risk_signature(pf))
    if not force and _MANDATE_REVIEW["review"] is not None and _MANDATE_REVIEW["sig"] == sig:
        return _MANDATE_REVIEW["review"]
    review = _mandate.review(pf, get_profile())
    out = review.model_dump() if review else None
    _MANDATE_REVIEW.update(sig=sig, review=out)
    return out


def _plan_signature(pf: Portfolio) -> list:
    """The holdings/weights shape drift is measured against — same basis as the risk signature,
    as a JSON-friendly list of [ticker, weight_pct]."""
    return [[tk, w] for tk, w in _risk_signature(pf)]


def _mandate_drift(old: list | None, new: list, threshold: int) -> tuple[bool, str]:
    """Did the book move materially off its last-planned shape? Material = a new or exited position
    of real size, or any name's weight moving >= threshold points. Returns (is_material, reason)."""
    od = {t: w for t, w in (old or [])}
    nd = {t: w for t, w in (new or [])}
    floor = max(5, threshold // 2)   # what counts as a "real size" new/exited position
    changes: list[str] = []
    for tk, w in nd.items():
        ow = od.get(tk)
        if ow is None:
            if w >= floor:
                changes.append(f"new {tk} {w:.0f}%")
        elif abs(w - ow) >= threshold:
            changes.append(f"{tk} {ow:.0f}->{w:.0f}%")
    for tk, w in od.items():
        if tk not in nd and w >= floor:
            changes.append(f"exited {tk} ({w:.0f}%)")
    return (bool(changes), "; ".join(changes[:4]))


def _fire_mandate_plan(review: dict, drift_reason: str = "", sig: list | None = None) -> None:
    """Build + drop the mandate-plan ping, and (re)set the drift baseline so the next drift is
    measured from here. Shared by the weekly review and the drift trigger."""
    moves = review.get("moves") or []
    align = (review.get("alignment") or "").strip()
    lead = f"Allocation shift: {drift_reason}. " if drift_reason else ""
    if drift_reason:
        title = "Your book moved — plan update"
    elif moves:
        title = f"Your plan — {len(moves)} move{'s' if len(moves) != 1 else ''} to consider"
    else:
        title = "Your plan — on track"
    if moves:
        movetxt = "; ".join(f"{mv['ticker']} {mv['action']}" for mv in moves[:3])
        summary = f"{lead}{align} → {movetxt}."
    else:
        summary = f"{lead}{align or 'Your portfolio still fits your mandate; nothing to change.'}"
    db_repo.save_research_event(event_type="mandate_plan", ticker="", severity="warn",
                                title=title, summary=summary[:300], source="mandate")
    if sig is not None:
        db_repo.save_mandate_plan_sig(sig)


def run_mandate_review() -> bool:
    """Proactive plan: on the mandate cadence, re-read the portfolio against the mandate and
    drop a plan into the ping rail — the agent coming to you, unprompted. Gated by a cooldown
    event so it fires at most once per period; a no-op when no mandate is set. One LLM call."""
    m = _mandate.get_mandate()
    if not m.is_set():
        return False
    if db_repo.event_exists_recent("mandate_plan", "", within_hours=config.MANDATE_REVIEW_DAYS * 24):
        return False
    review = mandate_review(force=True)   # fresh read, also warms the card cache
    if not review:
        return False
    _fire_mandate_plan(review, sig=_plan_signature(get_portfolio()))
    return True


def run_mandate_drift() -> bool:
    """Off-cadence plan: fire when the book moves materially off its last-planned shape between the
    weekly checks. Durable baseline (survives restarts); its own cooldown so a busy week can't spam.
    A no-op when there's no mandate, no holdings, or no real drift."""
    m = _mandate.get_mandate()
    if not m.is_set():
        return False
    pf = get_portfolio()
    if not pf.holdings:
        return False
    cur = _plan_signature(pf)
    prev = db_repo.load_mandate_plan_sig()
    if prev is None:
        db_repo.save_mandate_plan_sig(cur)   # first run baselines only — never fires
        return False
    material, reason = _mandate_drift(prev, cur, config.MANDATE_DRIFT_PCT)
    if not material:
        return False
    if db_repo.event_exists_recent("mandate_plan", "", within_hours=config.MANDATE_DRIFT_COOLDOWN_HOURS):
        db_repo.save_mandate_plan_sig(cur)   # absorb the drift so we don't re-fire the same shift
        return False
    review = mandate_review(force=True)
    if not review:
        return False
    _fire_mandate_plan(review, drift_reason=reason, sig=cur)
    return True


# --- the Twin (autonomous paper fund cloned from your real book) ------------- #
def twin_state() -> dict | None:
    return twin.state()


def twin_start() -> dict | None:
    """Clone the real account into the Twin (once) and launch it on your current mandate."""
    return twin.inception(mandate_statement=_mandate.get_mandate().statement)


def twin_compare(refresh: bool = False) -> dict:
    """You vs the Twin since inception — values, returns, the edge, holdings, equity curve, trades."""
    return twin.compare(refresh=refresh)


def twin_execute_pending() -> list[dict]:
    """Fill any queued Twin orders (no-op off-hours). Called by the autonomous loop later."""
    return twin.execute_pending()


def twin_snapshot() -> dict | None:
    """Record an equity-curve point for the Twin so the race line lives between trades."""
    return twin.snapshot_equity() if twin.is_running() else None


def twin_review_due() -> list[dict]:
    """Review matured Autopilot fills and write deterministic policy lessons."""
    return twin.review_due_trades() if twin.is_running() else []


def twin_review_windows() -> list[dict]:
    """Mature self-review: grade due evaluation windows vs SPY + sector, read the thesis state,
    and apply long-horizon grace. Feeds the richer per-tactic policy memory."""
    return twin.review_windows() if twin.is_running() else []


def run_twin_decision() -> bool:
    """Gated entry for the autonomous decision cycle (cost control on the LLM think — the Twin sets
    its own trade count, we just don't re-think more than every TWIN_DECIDE_HOURS). Emits an
    Autopilot event when it actually traded so the move shows in the feed."""
    if not config.TWIN_ENABLED or not twin.is_running():
        return False
    recent = db_repo.recent_agent_runs(limit=1, kind="twin_decision")
    if recent and recent[0].get("created_at"):
        try:
            last = datetime.fromisoformat(recent[0]["created_at"])
            if (datetime.now(last.tzinfo) - last).total_seconds() < config.TWIN_DECIDE_HOURS * 3600:
                return False
        except Exception:  # noqa: BLE001
            pass
    decision = twin.decide(get_profile())
    if not decision:
        return False
    moves = [m for m in decision.moves if m.action != "hold" and (m.usd or 0) > 0]
    if moves:
        movetxt = "; ".join(f"{m.action} {m.ticker}" for m in moves)[:200]
        db_repo.save_research_event(
            event_type="twin_decision", ticker="", severity="info", source="autopilot",
            title="Autopilot rebalanced",
            summary=(decision.summary + (f" — {movetxt}" if movetxt else ""))[:300])
    return True


def twin_decide_now() -> dict:
    """Force a decision cycle right now (the Autopilot tab's 'run a cycle' button), fill anything
    fillable, and return the refreshed race."""
    if not twin.is_running():
        return {"started": False}
    twin.decide(get_profile())
    twin.execute_pending()   # fills now if the market's open; otherwise the moves stay queued
    return twin.compare(refresh=True)


def twin_reset() -> None:
    db_repo.reset_twin()


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


def chat_history(limit: int = 80) -> list[dict]:
    """The persisted Home conversation (user + assistant turns), oldest first."""
    return db_repo.recent_chat_messages(limit=limit)


def save_chat_message(role: str, content: str) -> None:
    """Persist one turn of the Home conversation (best-effort)."""
    db_repo.save_chat_message(role, content)
