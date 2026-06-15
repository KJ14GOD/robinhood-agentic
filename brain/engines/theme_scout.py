"""Autonomous Theme Scout.

This is not a user mission. It is Signal's own market-radar loop: scan the broad tradable universe,
detect which themes have breadth/momentum/event support, persist them, and feed high-score
candidates to Autopilot. It is deterministic and cheap; no LLM spend is needed to form a first-pass
research agenda.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .. import config
from ..data.prices import ScreenRow, screen_universe
from ..data.universe import screening_universe
from ..db import repository as db_repo


@dataclass(frozen=True)
class ThemeRule:
    key: str
    name: str
    tickers: tuple[str, ...]


THEMES: tuple[ThemeRule, ...] = (
    ThemeRule("ai_compute", "AI compute and semiconductors",
              ("NVDA", "AMD", "AVGO", "MRVL", "MU", "SMCI", "ARM", "TSM", "ASML",
               "LRCX", "KLAC", "AMAT", "MPWR", "ALAB", "CRDO", "COHR", "LITE", "CIEN")),
    ThemeRule("ai_power_infra", "AI power and data-center infrastructure",
              ("VRT", "ETN", "PWR", "EME", "FIX", "APLD", "IREN", "CORZ", "HUT", "CLSK",
               "CEG", "VST", "TLN", "OKLO", "SMR")),
    ThemeRule("defense_space", "Defense, space, and autonomy",
              ("LMT", "NOC", "RTX", "GD", "HII", "LHX", "TXT", "KTOS", "AVAV", "RKLB",
               "ASTS", "SPIR", "BKSY", "ONDS", "ACHR", "JOBY")),
    ThemeRule("fintech_crypto", "Fintech and crypto rails",
              ("HOOD", "COIN", "MSTR", "SOFI", "SQ", "PYPL", "AFRM", "UPST", "LC", "BILL",
               "TOST", "NU")),
    ThemeRule("software_security", "Software, data, and security",
              ("CRWD", "PANW", "ZS", "DDOG", "NET", "MDB", "SNOW", "GTLB", "CFLT", "ESTC",
               "HCP", "IOT", "OKTA", "APP")),
    ThemeRule("biotech_platforms", "Biotech platforms and medicine",
              ("HIMS", "TMDX", "RXRX", "SDGR", "CRSP", "NTLA", "BEAM", "EDIT", "VERV",
               "VKTX", "ALT")),
    ThemeRule("nuclear_uranium", "Nuclear, uranium, and grid scarcity",
              ("CEG", "VST", "TLN", "OKLO", "SMR", "CCJ", "UEC", "UUUU", "NXE", "LEU",
               "URA", "URNM")),
    ThemeRule("quantum_nextgen", "Quantum and frontier compute",
              ("IONQ", "QBTS", "RGTI", "IBM", "GOOGL", "MSFT")),
)


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _candidate_score(r: ScreenRow, event_hits: int = 0) -> float:
    score = 0.0
    score += r.ret_1m_pct * 0.7 + r.ret_3m_pct * 0.55 + r.ret_6m_pct * 0.25
    score += 12 if r.above_50d else -8
    score += 14 if r.above_200d else -10
    if 35 <= r.rsi_14 <= 72:
        score += 8
    elif r.rsi_14 > 82:
        score -= 16
    score -= max(0.0, r.vol_annualized_pct - 80.0) * 0.08
    score += min(event_hits, 4) * 5
    return score


def _event_hits() -> dict[str, int]:
    hits: dict[str, int] = {}
    for e in db_repo.recent_events(limit=160, within_hours=120.0):
        t = (e.get("ticker") or "").upper()
        if t:
            hits[t] = hits.get(t, 0) + 1
    return hits


def _theme_score(rows: list[ScreenRow], hits: dict[str, int]) -> tuple[float, float, list[str]]:
    if not rows:
        return 0.0, 0.0, ["No liquid names with usable signal data."]
    ranked = sorted(rows, key=lambda r: _candidate_score(r, hits.get(r.ticker, 0)), reverse=True)
    leaders = ranked[: min(5, len(ranked))]
    breadth_50 = sum(1 for r in rows if r.above_50d) / len(rows)
    breadth_200 = sum(1 for r in rows if r.above_200d) / len(rows)
    pos_3m = sum(1 for r in rows if r.ret_3m_pct > 0) / len(rows)
    avg_1m = sum(r.ret_1m_pct for r in leaders) / len(leaders)
    avg_3m = sum(r.ret_3m_pct for r in leaders) / len(leaders)
    ev = sum(hits.get(r.ticker, 0) for r in rows)
    score = 20 + avg_1m * 0.7 + avg_3m * 0.45 + breadth_50 * 18 + breadth_200 * 14 + pos_3m * 14
    score += min(ev, 8) * 3
    confidence = min(100.0, max(0.0, 30 + len(rows) * 3 + breadth_50 * 25 + min(ev, 6) * 4))
    evidence = [
        f"{len(rows)} names scanned; {breadth_50:.0%} above 50d, {breadth_200:.0%} above 200d.",
        f"Leaders average {avg_1m:+.1f}% over 1m and {avg_3m:+.1f}% over 3m.",
    ]
    if ev:
        evidence.append(f"{ev} recent portfolio/market events mention names in this theme.")
    evidence.append("Top names: " + ", ".join(r.ticker for r in leaders[:5]) + ".")
    return min(100.0, max(0.0, score)), confidence, evidence


def _pack_candidate(rule: ThemeRule, r: ScreenRow, hits: int) -> dict:
    score = _candidate_score(r, hits)
    why = (f"{rule.name}: 1m {r.ret_1m_pct:+.0f}%, 3m {r.ret_3m_pct:+.0f}%, "
           f"{'above' if r.above_200d else 'below'} 200d, RSI {r.rsi_14:.0f}")
    if hits:
        why += f", {hits} recent event mention{'s' if hits != 1 else ''}"
    return {
        "ticker": r.ticker,
        "score": round(score, 2),
        "reason": why,
        "ret_1m_pct": r.ret_1m_pct,
        "ret_3m_pct": r.ret_3m_pct,
        "ret_6m_pct": r.ret_6m_pct,
        "rsi_14": r.rsi_14,
        "vol_annualized_pct": r.vol_annualized_pct,
        "above_50d": r.above_50d,
        "above_200d": r.above_200d,
    }


def scan(refresh: bool = False) -> list[dict]:
    tickers = sorted({t for rule in THEMES for t in rule.tickers})
    rows = {r.ticker.upper(): r for r in screen_universe([t for t in screening_universe() if t in tickers], refresh=refresh)}
    hits = _event_hits()
    feedback = db_repo.autonomous_theme_feedback()
    out: list[dict] = []
    for rule in THEMES:
        theme_rows = [rows[t] for t in rule.tickers if t in rows]
        score, confidence, evidence = _theme_score(theme_rows, hits)
        fb = feedback.get(rule.key, {})
        if fb.get("tested_count", 0):
            adjust = fb.get("avg_sector_alpha", 0.0) * 2.0
            adjust += (fb.get("win_rate", 0.0) - 50.0) * 0.15
            adjust -= fb.get("break_rate", 0.0) * 0.25
            adjust = max(-15.0, min(15.0, adjust))
            score = min(100.0, max(0.0, score + adjust))
            evidence.append(
                f"Autopilot feedback: {fb['tested_count']} tested, "
                f"{fb['avg_sector_alpha']:+.1f}% avg sector alpha, {fb['win_rate']:.0f}% worked."
            )
        candidates = sorted(
            [_pack_candidate(rule, r, hits.get(r.ticker, 0)) for r in theme_rows],
            key=lambda c: c["score"], reverse=True,
        )[:8]
        status = "active" if score >= 45 and candidates else "cooling"
        db_repo.upsert_autonomous_theme(rule.key, rule.name, score, confidence, evidence,
                                        candidates, status=status)
        out.append({"key": rule.key, "name": rule.name, "status": status, "score": score,
                    "confidence": confidence, "evidence": evidence, "candidates": candidates})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def run_due(force: bool = False, refresh: bool = False) -> list[dict]:
    if not force:
        recent = db_repo.recent_agent_runs(limit=1, kind="theme_scout")
        if recent and recent[0].get("created_at"):
            last = _parse_iso(recent[0]["created_at"])
            if last:
                age = (datetime.now(last.tzinfo) - last).total_seconds() / 3600
                if age < config.THEME_SCOUT_HOURS:
                    return []
    themes = scan(refresh=refresh)
    active = [t for t in themes if t["status"] == "active"]
    top = active[0] if active else (themes[0] if themes else None)
    if top and top["score"] >= 60:
        db_repo.save_research_event(
            event_type="theme_scout",
            ticker="",
            severity="info",
            source="theme_scout",
            title=f"Theme Scout: {top['name']}",
            summary=f"Score {top['score']:.0f}/100. " + " ".join(top["evidence"][:2]),
        )
    db_repo.save_agent_run(
        query="Autonomous theme scout",
        answer=f"{len(active)} active themes discovered.",
        kind="theme_scout",
        steps=[{"type": "theme_scout", "themes": themes[:8]}],
        tools_used="screen_universe,recent_events",
        model="deterministic",
    )
    return themes
