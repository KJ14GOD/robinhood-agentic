"""Strategy missions — standing theme trackers that work without being asked.

A mission ("track defense stocks", "find stable AI exposure") keeps a live roster
of names for a theme, monitors them cheaply every cycle, and on a gated daily
cadence re-labels each one BUY / WATCH / WAIT / REJECT through the user's profile.
Material changes (a new name, a promotion to BUY) are emitted as `mission_update`
events into the Today/Activity feed, so the brain reports back on its own.

LLM spend is deliberately bounded: the roster is *seeded* with a live web search +
structure step when a mission is created and re-screened on a slow (~weekly)
cadence, and *re-classified* on a daily cooldown — never on every background cycle.
The monitoring in between is deterministic and free.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import llm
from ..data.prices import TrendSignals, clean_ticker, get_signals_many
from ..db import repository as db_repo
from ..models import (
    Mission, MissionCandidate, MissionRoster, MissionSeed, RiskProfile, _now,
)

CLASSIFY_COOLDOWN_HOURS = 20.0   # gated daily cadence for the LLM re-label
RESEED_COOLDOWN_HOURS = 168.0    # ~weekly: re-screen the theme for genuinely new names
MAX_ROSTER = 15

_SIGNAL_KEYS = (
    "price", "sector", "beta", "pe", "dividend_yield",
    "ret_1m_pct", "ret_3m_pct", "ret_6m_pct",
    "above_50d", "above_200d", "rsi_14", "vol_annualized_pct",
)


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return 1e9


def _sig_snapshot(sig: TrendSignals | None) -> dict:
    if sig is None:
        return {}
    return {k: getattr(sig, k) for k in _SIGNAL_KEYS if getattr(sig, k, None) is not None}


def _seed_research_task(title: str, mode: str, profile: RiskProfile) -> str:
    return f"""Using current information from the live web, find the real, liquid, US-listed (NYSE/Nasdaq) stocks that genuinely best fit this investing theme today: "{title}".

Judge fit on the merits as of now. Include whichever names truly belong — established or newer — and don't restrict yourself to the familiar names from memory if the live picture has moved on. Equally, don't reach for obscure or just-listed names for novelty's sake: best fit wins.

REQUESTED RISK FLAVOR: {mode} (stable=lower risk/beta, volatile=higher risk/upside, any=best fit)
INVESTOR CONTEXT: {profile.describe()}

Return up to {MAX_ROSTER} of the most on-theme, investable names, each with its ticker and one line on why it fits the theme right now (cite what you found). Quality over a long list."""


def _seed_roster(title: str, mode: str, profile: RiskProfile) -> MissionSeed:
    """Build the roster as a two-step so re-seed can surface names the model's own
    memory can't — newer or post-cutoff names the live picture now favors. First
    search the live web for the names that best fit the theme today, then structure
    that cited brief into the roster. Best-effort search: if it fails we fall back to the model's
    knowledge so a web hiccup never blocks seeding (structured output can't share a
    request with web search, hence the two calls)."""
    try:
        brief = llm.web_research(_seed_research_task(title, mode, profile), max_searches=5)
    except Exception:  # noqa: BLE001 — degrade to memory-only rather than fail the seed
        brief = ""
    grounding = (f"\n\nLIVE WEB RESEARCH (current and cited — prefer these names and reflect "
                 f"what is happening now):\n{brief.strip()}" if brief.strip() else "")

    prompt = f"""The user wants a standing research mission: "{title}".

Map this theme to a focused roster of {MAX_ROSTER} or fewer real, liquid, US-listed
tickers that genuinely fit it. Prefer the most on-theme, investable names over a
long list — quality over quantity.

REQUESTED RISK FLAVOR: {mode} (stable=lower risk/beta, volatile=higher risk/upside, any=best fit)
INVESTOR: {profile.describe()}{grounding}

Only include tickers you are confident trade on US exchanges (NYSE/Nasdaq). For each,
give the ticker and one line on why it fits the theme. Return a normalized short name
for the theme too."""
    return llm.parse(prompt, MissionSeed, max_tokens=1500)


def _classify(mission: Mission, signals: dict[str, TrendSignals], profile: RiskProfile) -> MissionRoster:
    rows = [f"- {s.as_prompt()}" for s in signals.values() if s and s.price > 0]
    prompt = f"""Mission: "{mission.title}" (theme: {mission.theme or mission.title}).

For each candidate below, decide a label for THIS investor and the mission's risk flavor:
- BUY: compelling to act on now.
- WATCH: on-theme, worth tracking, no action yet.
- WAIT: a good name at the wrong price/time — wait for a pullback or catalyst.
- REJECT: no longer fits the theme or this investor; drop it.

RISK FLAVOR: {mission.mode}
INVESTOR: {profile.describe()}

CANDIDATES (grounded signals — reason from these, don't invent):
{chr(10).join(rows) or '(no live signals available)'}

Give each ticker a label, an honest conviction (1-10), and one grounded sentence citing
the signal or the fit. Be willing to REJECT names that have drifted off-theme."""
    return llm.parse(prompt, MissionRoster, max_tokens=2500)


def _emit(mission: Mission, cand: MissionCandidate, what: str) -> None:
    """Surface a notable mission change to the Today/Activity feed (deduped)."""
    if db_repo.event_exists_recent("mission_update", cand.ticker, within_hours=CLASSIFY_COOLDOWN_HOURS):
        return
    severity = "warn" if cand.label == "BUY" else "info"
    db_repo.save_research_event(
        event_type="mission_update", ticker=cand.ticker, severity=severity,
        title=f"{cand.ticker} {what} · {mission.title}",
        summary=cand.reason, source="mission")


def create_mission(title: str, mode: str, profile: RiskProfile) -> Mission:
    """Seed a new mission's roster from the theme, then classify it once so it's
    useful the moment it's opened."""
    title = (title or "").strip()
    if mode not in {"stable", "balanced", "volatile", "any"}:
        mode = "any"
    seed = _seed_roster(title, mode, profile)

    seen: set[str] = set()
    candidates: list[MissionCandidate] = []
    for item in seed.candidates[:MAX_ROSTER]:
        tkr = clean_ticker(item.ticker)
        if not tkr or tkr in seen:
            continue
        seen.add(tkr)
        candidates.append(MissionCandidate(ticker=tkr, reason=item.why))

    mission = Mission(id=uuid.uuid4().hex[:12], title=title,
                      theme=seed.theme or title, mode=mode, candidates=candidates)
    mission.last_seeded_at = _now()  # the roster was just screened; don't re-seed for a week
    db_repo.save_mission(mission)
    return run_mission(mission, profile, force=True)


def run_mission(mission: Mission, profile: RiskProfile, force: bool = False) -> Mission:
    """Monitor the roster and (gated) re-classify it, emitting events on changes."""
    if not force and _hours_since(mission.last_classified_at) < CLASSIFY_COOLDOWN_HOURS:
        return mission  # too soon since the last LLM pass — stay cheap
    tickers = [c.ticker for c in mission.candidates]
    if not tickers:
        return mission

    signals = get_signals_many(tickers)
    roster = _classify(mission, signals, profile)
    verdicts = {item.ticker.upper(): item for item in roster.items}
    previous = {c.ticker: c for c in mission.candidates}

    new_candidates: list[MissionCandidate] = []
    for tkr in tickers:
        v = verdicts.get(tkr)
        prev = previous.get(tkr)
        sig = signals.get(tkr)
        cand = MissionCandidate(
            ticker=tkr,
            label=(v.label if v else (prev.label if prev else "WATCH")),
            conviction=(v.conviction if v else (prev.conviction if prev else 5)),
            reason=(v.reason if v else (prev.reason if prev else "")),
            sector=(getattr(sig, "sector", "") if sig else (prev.sector if prev else "")),
            signals=_sig_snapshot(sig) or (prev.signals if prev else {}),
            first_seen=(prev.first_seen if prev else _now()),
            updated_at=_now(),
        )
        new_candidates.append(cand)
        # report a name promoted into BUY. (New names are surfaced by reseed_mission,
        # which is where the roster actually grows — here it never does.)
        if cand.label == "BUY" and (prev is None or prev.label != "BUY"):
            _emit(mission, cand, "promoted to BUY")

    mission.candidates = new_candidates
    mission.last_classified_at = _now()
    mission.last_run_at = _now()
    mission.updated_at = _now()
    db_repo.save_mission(mission)
    return mission


def _cap_roster(roster: list[MissionCandidate]) -> list[MissionCandidate]:
    """Keep the roster bounded. When over the cap, shed names the brain already
    labeled REJECT (lowest conviction first), then lowest-conviction overall —
    never silently dropping a live high-conviction name."""
    if len(roster) <= MAX_ROSTER:
        return roster
    # sort so the most-droppable sit first: REJECTs before others, low conviction first
    droppable_first = sorted(roster, key=lambda c: (0 if c.label == "REJECT" else 1, c.conviction))
    return droppable_first[len(roster) - MAX_ROSTER:]


def reseed_mission(mission: Mission, profile: RiskProfile) -> Mission:
    """Re-screen the theme and merge genuinely new names into the roster, keeping
    existing names and their accumulated history. Emits a real 'added to the
    roster' event per new name. This is the only place a roster grows."""
    seed = _seed_roster(mission.title, mission.mode, profile)
    existing = {c.ticker: c for c in mission.candidates}
    added: list[MissionCandidate] = []
    for item in seed.candidates[:MAX_ROSTER]:
        tkr = clean_ticker(item.ticker)
        if not tkr or tkr in existing:
            continue
        cand = MissionCandidate(ticker=tkr, reason=item.why)  # WATCH until the next classify
        existing[tkr] = cand
        added.append(cand)

    if added:
        roster = _cap_roster(list(existing.values()))
        mission.candidates = roster
        mission.updated_at = _now()
        kept = {c.ticker for c in roster}
        for cand in added:
            if cand.ticker in kept:
                _emit(mission, cand, "added to the roster")

    mission.last_seeded_at = _now()
    db_repo.save_mission(mission)
    return mission


def run_due_missions(profile: RiskProfile) -> list[dict]:
    """Background entry point: for each active mission, re-screen for new names if
    the (slow) reseed cadence lapsed, then re-classify if the (daily) classify
    cadence lapsed. Both gated, so a calm set of missions spends nothing."""
    ran: list[dict] = []
    for mission in db_repo.all_missions(status="active"):
        did = False
        try:
            if _hours_since(mission.last_seeded_at) >= RESEED_COOLDOWN_HOURS:
                mission = reseed_mission(mission, profile)
                did = True
            if _hours_since(mission.last_classified_at) >= CLASSIFY_COOLDOWN_HOURS:
                run_mission(mission, profile, force=False)
                did = True
        except Exception:  # noqa: BLE001 — one bad mission must not stall the rest
            continue
        if did:
            ran.append({"id": mission.id, "title": mission.title})
    return ran
