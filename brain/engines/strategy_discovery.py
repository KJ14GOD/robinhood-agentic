"""Autonomous strategy discovery for Autopilot.

Theme Scout answers "what areas are alive?" Strategy Discovery answers "what repeatable tactic
should the Twin test there?" It is deterministic and cheap: no manual mission, no LLM spend, and no
new UI surface. The output is persisted strategy experiments that Autopilot can use as grounded
candidates and later score from actual paper-trade review windows.
"""
from __future__ import annotations

from datetime import datetime

from .. import config
from ..data.prices import get_signals_many
from ..db import repository as db_repo


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _market_regime(refresh: bool = False) -> str:
    try:
        sigs = get_signals_many(["SPY", "QQQ"], refresh=refresh)
        rows = [s for s in sigs.values() if getattr(s, "price", 0.0)]
        if not rows:
            return "unknown"
        avg_3m = sum(getattr(s, "ret_3m_pct", 0.0) or 0.0 for s in rows) / len(rows)
        avg_rsi = sum(getattr(s, "rsi_14", 50.0) or 50.0 for s in rows) / len(rows)
        above = sum(1 for s in rows if getattr(s, "above_200d", False))
        if avg_rsi >= 72 and above == len(rows):
            return "risk_on_overextended"
        if above == len(rows) and avg_3m >= 5:
            return "risk_on"
        if above == 0 or avg_3m <= -5:
            return "risk_off"
        if avg_rsi <= 35:
            return "oversold"
        return "mixed"
    except Exception:  # noqa: BLE001
        return "unknown"


def _pick_candidates(candidates: list[dict], tactic: str) -> list[dict]:
    if tactic == "pullback_in_uptrend":
        rows = [c for c in candidates if c.get("above_200d") and c.get("rsi_14", 99) <= 58
                and c.get("ret_3m_pct", 0) >= 0]
    elif tactic == "momentum_continuation":
        rows = [c for c in candidates if c.get("above_50d") and c.get("above_200d")
                and 45 <= c.get("rsi_14", 0) <= 72 and c.get("ret_1m_pct", 0) > 0]
    elif tactic == "valuation_mean_reversion":
        rows = [c for c in candidates if c.get("above_200d") and c.get("rsi_14", 99) <= 38]
    else:
        rows = candidates
    rows = sorted(rows, key=lambda x: float(x.get("score") or 0.0), reverse=True)[:6]
    return [{**c, "strategy_reason": _candidate_reason(c, tactic)} for c in rows]


def _candidate_reason(c: dict, tactic: str) -> str:
    tk = c.get("ticker", "")
    if tactic == "pullback_in_uptrend":
        return f"{tk} is still above the 200d but not overheated; test pullback entry discipline."
    if tactic == "momentum_continuation":
        return f"{tk} has breadth/momentum support without extreme RSI; test continuation."
    if tactic == "valuation_mean_reversion":
        return f"{tk} is oversold inside an intact theme; test mean-reversion setup."
    return c.get("reason", "")


def _strategy_template(theme: dict, tactic: str, regime: str, candidates: list[dict]) -> dict:
    name = theme.get("name") or theme.get("key") or "theme"
    if tactic == "pullback_in_uptrend":
        return {
            "title": f"{name}: pullbacks in intact uptrends",
            "hypothesis": f"In {name}, names above the 200d with cooled RSI can outperform once the theme remains active.",
            "entry_rule": "Candidate above 200d, RSI cooled below 58, theme score remains active.",
            "exit_rule": "Exit or stop testing if price loses 200d, theme score cools, or thesis/event flow breaks.",
            "sizing_note": "Start measured; scale only after judged sector-alpha confirms the setup.",
            "horizon": "1-3 months",
        }
    if tactic == "momentum_continuation":
        return {
            "title": f"{name}: momentum continuation",
            "hypothesis": f"When {name} has broad leadership, high-relative-strength names can keep compounding.",
            "entry_rule": "Candidate above 50d/200d, positive 1m and 3m returns, RSI not above 72.",
            "exit_rule": "Exit or cool the strategy if RSI becomes extreme without follow-through or sector alpha turns negative.",
            "sizing_note": "Use smaller size in overextended tape; avoid chasing RSI >72.",
            "horizon": "1-2 months",
        }
    return {
        "title": f"{name}: oversold mean reversion",
        "hypothesis": f"Theme leaders in {name} can rebound from oversold levels if the broad theme is still intact.",
        "entry_rule": "Candidate above 200d with RSI below 38 and no thesis-break events.",
        "exit_rule": "Retire if oversold entries keep lagging sector or thesis states weaken.",
        "sizing_note": "Small exploratory size until reviews prove the setup.",
        "horizon": "2-6 weeks",
    }


def _score_strategy(theme: dict, tactic: str, candidates: list[dict], regime: str) -> tuple[float, float, list[str], str]:
    if not candidates:
        return 0.0, 0.0, ["No candidates fit the tactic rules."], "cooling"
    theme_score = float(theme.get("score") or 0.0)
    avg_candidate = sum(float(c.get("score") or 0.0) for c in candidates) / len(candidates)
    score = theme_score * 0.55 + avg_candidate * 0.45
    evidence = [
        f"Theme score {theme_score:.0f}/100 with {len(candidates)} candidates fitting {tactic}.",
        "Candidates: " + ", ".join(c.get("ticker", "") for c in candidates[:5]) + ".",
        f"Current tape regime: {regime}.",
    ]
    if tactic == "momentum_continuation" and regime == "risk_on_overextended":
        score -= 12
        evidence.append("Momentum setup penalized because broad tape is already overextended.")
    if tactic == "pullback_in_uptrend" and regime in {"mixed", "oversold"}:
        score += 5
        evidence.append("Pullback setup favored because tape is not euphoric.")
    feedback = db_repo.autonomous_strategy_feedback()
    key_hint = f"{theme.get('key')}:{tactic}:{regime}"
    fb = feedback.get(key_hint, {})
    if fb.get("tested_count", 0):
        adjust = fb.get("avg_sector_alpha", 0.0) * 2.0
        adjust += (fb.get("win_rate", 0.0) - 50.0) * 0.12
        adjust -= fb.get("break_rate", 0.0) * 0.25
        adjust = max(-18.0, min(18.0, adjust))
        score += adjust
        evidence.append(
            f"Strategy feedback: {fb['tested_count']} tested, "
            f"{fb['avg_sector_alpha']:+.1f}% avg sector alpha, {fb['win_rate']:.0f}% worked."
        )
    confidence = min(100.0, max(0.0, 25 + len(candidates) * 7 + float(theme.get("confidence") or 0.0) * 0.35))
    if fb.get("break_rate", 0) >= 34 and fb.get("tested_count", 0) >= 2:
        status = "retired"
    elif score >= 68 and confidence >= 45:
        status = "active"
    elif score >= 45:
        status = "exploring"
    else:
        status = "cooling"
    return min(100.0, max(0.0, score)), confidence, evidence, status


def discover(refresh: bool = False) -> list[dict]:
    regime = _market_regime(refresh=refresh)
    themes = db_repo.autonomous_themes(status="active", limit=10, min_score=45.0)
    out: list[dict] = []
    for theme in themes:
        for tactic in ("pullback_in_uptrend", "momentum_continuation", "valuation_mean_reversion"):
            candidates = _pick_candidates(theme.get("candidates") or [], tactic)
            score, confidence, evidence, status = _score_strategy(theme, tactic, candidates, regime)
            if not candidates:
                continue
            key = f"{theme.get('key')}:{tactic}:{regime}"
            tpl = _strategy_template(theme, tactic, regime, candidates)
            db_repo.upsert_autonomous_strategy(
                key=key,
                title=tpl["title"],
                tactic=tactic,
                horizon=tpl["horizon"],
                theme_key=theme.get("key") or "",
                theme_name=theme.get("name") or "",
                market_regime=regime,
                hypothesis=tpl["hypothesis"],
                entry_rule=tpl["entry_rule"],
                exit_rule=tpl["exit_rule"],
                sizing_note=tpl["sizing_note"],
                score=score,
                confidence=confidence,
                evidence=evidence,
                candidates=candidates,
                status=status,
            )
            out.append({"key": key, "status": status, "score": score, "confidence": confidence,
                        "tactic": tactic, "theme_key": theme.get("key"), "theme_name": theme.get("name"),
                        "market_regime": regime, "candidates": candidates, **tpl, "evidence": evidence})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def run_due(force: bool = False, refresh: bool = False) -> list[dict]:
    if not force:
        recent = db_repo.recent_agent_runs(limit=1, kind="strategy_discovery")
        if recent and recent[0].get("created_at"):
            last = _parse_iso(recent[0]["created_at"])
            if last:
                age = (datetime.now(last.tzinfo) - last).total_seconds() / 3600
                if age < config.STRATEGY_DISCOVERY_HOURS:
                    return []
    strategies = discover(refresh=refresh)
    active = [s for s in strategies if s["status"] == "active"]
    top = active[0] if active else (strategies[0] if strategies else None)
    if top and top["score"] >= 65:
        db_repo.save_research_event(
            event_type="strategy_discovery",
            ticker="",
            severity="info",
            source="strategy_discovery",
            title=f"Strategy Discovery: {top['title']}",
            summary=f"{top['tactic']} during {top['market_regime']}. Score {top['score']:.0f}/100. "
                    + " ".join(top["evidence"][:2]),
        )
    db_repo.save_agent_run(
        query="Autonomous strategy discovery",
        answer=f"{len(active)} active strategy experiments discovered.",
        kind="strategy_discovery",
        steps=[{"type": "strategy_discovery", "strategies": strategies[:10]}],
        tools_used="autonomous_themes,contextual_bandit",
        model="deterministic",
    )
    return strategies
