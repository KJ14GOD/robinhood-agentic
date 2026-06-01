"""Structural risk — portfolio-level reasoning the per-ticker monitors can't do.

Per-stock signals miss the way books actually blow up: owning the same bet several times without
realizing it. Five names can each look like a reasonable position while 60% of the portfolio quietly
rides one driver (say, AI data-center capex) — so if that one thesis cracks, the whole cluster falls
together. This engine clusters the holdings by their *shared underlying driver* and surfaces the
combined exposure, the single condition that would hit the whole cluster, and the biggest hidden risk.

Design: the model does the *judgement* (which names share a driver); the code does the *math*
(summing the real portfolio weights of each cluster). We never trust the model's arithmetic — only
its clustering — keeping the numbers grounded.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .. import llm
from ..db import repository as db_repo
from ..models import Portfolio, RiskCluster, RiskProfile, StructuralRisk, _now

CONCENTRATION_ALERT_PCT = 40.0   # a cluster this big → "concentrated" + an autonomous ping
ALERT_COOLDOWN_HOURS = 24.0      # don't re-ping the same structural warning more than daily


class _Cluster(BaseModel):
    label: str
    driver: str = ""
    breaks_if: str = ""
    tickers: list[str] = Field(default_factory=list)


class _ClusterPlan(BaseModel):
    clusters: list[_Cluster] = Field(default_factory=list)
    note: str = ""


def _prompt(pf: Portfolio, profile: RiskProfile, theses: dict) -> str:
    weights = pf.weights()
    rows = []
    for h in sorted(pf.holdings, key=lambda x: weights.get(x.ticker, 0.0), reverse=True):
        th = theses.get(h.ticker)
        drv = f" — thesis: {th.thesis}" if th and getattr(th, "thesis", "") else ""
        rows.append(f"- {h.ticker}: {weights.get(h.ticker, 0.0):.1f}% of portfolio{drv}")
    return f"""Analyze this portfolio for STRUCTURAL risk — not per-stock risk, but bets the investor is
making more than once without realizing it. Group the holdings into clusters that share one underlying
driver/factor/theme (e.g. "AI data-center capex", "rate-sensitive / long-duration", "consumer
spending", "one mega-cap's ecosystem", "energy prices"). Assign a holding to the cluster that captures
its DOMINANT driver. Only form a cluster where there's a real shared dependency — leave genuinely
independent names out rather than forcing them in.

For each cluster give: a short label, the shared driver, the single condition that would hit the whole
cluster at once (breaks_if), and the tickers in it. Do NOT compute weights or percentages — just assign
the tickers; the system computes exposure from real portfolio weights.

INVESTOR: {profile.describe()}

HOLDINGS:
{chr(10).join(rows)}

Return clusters (most significant first) and an optional one-line note on the book's overall shape."""


def analyze(pf: Portfolio, profile: RiskProfile, theses: dict | None = None) -> StructuralRisk:
    """Cluster holdings by shared driver and compute each cluster's real combined weight."""
    if not pf.holdings:
        return StructuralRisk(headline="No holdings to analyze yet.", as_of=_now())
    theses = theses or {}
    weights = pf.weights()
    try:
        plan = llm.parse(_prompt(pf, profile, theses), _ClusterPlan, max_tokens=1500)
    except Exception:  # noqa: BLE001 — never let the risk read break the page
        return StructuralRisk(headline="Structural read unavailable right now.", as_of=_now())

    clusters: list[RiskCluster] = []
    for c in plan.clusters:
        tickers = [t.upper() for t in c.tickers if t.upper() in weights]
        if not tickers:
            continue
        clusters.append(RiskCluster(
            label=c.label, driver=c.driver, breaks_if=c.breaks_if, tickers=tickers,
            weight_pct=round(sum(weights.get(t, 0.0) for t in tickers), 1)))

    # Keep clusters that are a genuine multi-name bet, or a single name big enough to be structural.
    clusters = [c for c in clusters if len(c.tickers) >= 2 or c.weight_pct >= CONCENTRATION_ALERT_PCT]
    clusters.sort(key=lambda c: c.weight_pct, reverse=True)

    top = clusters[0] if clusters else None
    concentrated = bool(top and top.weight_pct >= CONCENTRATION_ALERT_PCT)
    if concentrated:
        headline = f"{top.weight_pct:.0f}% of your book is one bet: {top.label}."
    elif top:
        headline = f"Most concentrated driver: {top.label} at {top.weight_pct:.0f}% — reasonably spread for now."
    else:
        headline = "No major structural concentration — your bets are genuinely spread."
    return StructuralRisk(headline=headline, concentrated=concentrated,
                          clusters=clusters[:4], note=plan.note, as_of=_now())


def maybe_alert(result: StructuralRisk) -> bool:
    """If the book is concentrated, drop one cooldowned ping into the event feed. Returns whether
    it fired. Portfolio-level, so the event carries no ticker (cooldown keyed on the empty ticker)."""
    if not result.concentrated or not result.clusters:
        return False
    if db_repo.event_exists_recent("structural_risk", "", within_hours=ALERT_COOLDOWN_HOURS):
        return False
    top = result.clusters[0]
    db_repo.save_research_event(
        event_type="structural_risk", ticker="", severity="warn", source="risk",
        title=result.headline,
        summary=(f"{', '.join(top.tickers)} all ride {top.driver or top.label}. "
                 f"Breaks if {top.breaks_if or 'the shared driver turns'}.").strip()[:240])
    return True
