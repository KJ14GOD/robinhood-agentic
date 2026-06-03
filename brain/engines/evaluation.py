"""Evaluation engine — the scorecard that answers 'is the brain actually good?'

Reads the marked-to-market shadow ledger and grades the brain's recommendations
along every cut the product needs to earn trust: overall win rate / return /
benchmark-relative alpha, calibration by conviction bucket, and performance by
source engine, action label, and risk mode — plus a short, grounded read of what
the brain is good and bad at.

Everything here is deterministic: no LLM call. The numbers are the argument, and
benchmark-relative alpha is only counted over trades that actually carry an
anchor, so un-gradeable (legacy) records can't flatter or punish the score.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import shadow
from ..models import ShadowTrade

# A call has to clear this many days before it counts toward the trusted track
# record. It's a noise floor, not a proof bar: a position marked minutes after
# entry tells us nothing, so younger calls are reported separately as "forming"
# and kept out of the headline win rate / alpha.
MATURE_DAYS = 5


def _fmt(v: float) -> str:
    return f"{v:+.1f}%"


def _age_days(t: ShadowTrade) -> float:
    """Calendar days a call has been alive, from entry to now."""
    try:
        dt = datetime.fromisoformat((t.entry_at or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _bucket(conviction: int) -> str:
    if conviction >= 7:
        return "high"
    if conviction >= 4:
        return "medium"
    return "low"


def _agg(trades: list[ShadowTrade]) -> dict:
    """Core stats for any set of trades — overall or one slice of a cut."""
    n = len(trades)
    if not n:
        return {"count": 0, "win_rate": 0.0, "avg_return_pct": 0.0,
                "avg_alpha_pct": 0.0, "benchmarked": 0, "beat_bench_rate": 0.0}
    rets = [t.return_pct() for t in trades]
    wins = sum(1 for r in rets if r > 0)
    benched = [t for t in trades if t.has_benchmark()]
    alphas = [t.alpha_pct() for t in benched]
    beats = sum(1 for a in alphas if a > 0)
    return {
        "count": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_return_pct": round(sum(rets) / n, 2),
        "avg_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else 0.0,
        "benchmarked": len(benched),
        "beat_bench_rate": round(beats / len(alphas) * 100, 1) if alphas else 0.0,
    }


def _group(trades: list[ShadowTrade], keyfn) -> list[dict]:
    groups: dict[str, list[ShadowTrade]] = {}
    for t in trades:
        groups.setdefault(keyfn(t) or "—", []).append(t)
    rows = [{"key": k, **_agg(v)} for k, v in groups.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


_BUCKET_ORDER = {"high": 0, "medium": 1, "low": 2}


def _calibration(trades: list[ShadowTrade]) -> list[dict]:
    rows = _group(trades, lambda t: _bucket(t.conviction))
    rows.sort(key=lambda r: _BUCKET_ORDER.get(r["key"], 9))
    return rows


def _narrative(headline: dict, forming: dict, by_bucket: list[dict], by_source: list[dict]) -> list[str]:
    """A few honest, grounded sentences on what the brain is good and bad at —
    derived straight from the cuts, never invented. Maturity comes first: the read
    leads with how young the book is before it says anything about edge."""
    out: list[str] = []
    n = headline["count"]                       # matured calls only
    total = headline.get("total", n)
    forming_n = headline.get("forming", 0)
    med = headline.get("median_age_days", 0.0)
    bar = headline.get("mature_days", MATURE_DAYS)
    if total == 0:
        return ["No recommendations logged yet — analyze a stock, run discovery, or "
                "let the assistant make a call to start the track record."]
    if n == 0:
        prov = forming.get("avg_return_pct", 0.0)
        return [f"No call has cleared the {bar}-day maturity bar yet — all {forming_n} are still "
                f"forming (median age {med:.1f}d). The {_fmt(prov)} so far is mark-to-market noise, "
                "not a track record; the scorecard fills in as calls age."]
    if n < 5:
        out.append(f"Only {n} call{'s' if n != 1 else ''} past the {bar}-day bar — early signal, "
                   "not proof. The record needs depth before it's trustworthy.")

    hi = next((r for r in by_bucket if r["key"] == "high" and r["count"]), None)
    lo = next((r for r in by_bucket if r["key"] == "low" and r["count"]), None)
    if hi and lo:
        if hi["avg_return_pct"] > lo["avg_return_pct"]:
            out.append(f"Conviction is calibrated: high-conviction calls average "
                       f"{_fmt(hi['avg_return_pct'])} vs {_fmt(lo['avg_return_pct'])} for low-conviction.")
        else:
            out.append(f"Conviction is inverted: high-conviction calls ({_fmt(hi['avg_return_pct'])}) "
                       f"aren't beating low-conviction ones ({_fmt(lo['avg_return_pct'])}) — the brain's "
                       "confidence isn't earning its keep yet.")

    sized = [r for r in by_source if r["count"] >= 2]
    if sized:
        best = max(sized, key=lambda r: r["avg_return_pct"])
        worst = min(sized, key=lambda r: r["avg_return_pct"])
        out.append(f"Strongest engine: {best['key']} at {_fmt(best['avg_return_pct'])} over {best['count']} calls.")
        if worst["key"] != best["key"] and worst["avg_return_pct"] < 0:
            out.append(f"Weakest: {worst['key']} is underwater at {_fmt(worst['avg_return_pct'])} over {worst['count']}.")

    if headline["benchmarked"]:
        verb = "beating" if headline["avg_alpha_pct"] > 0 else "lagging"
        out.append(f"Against SPY, the book is {verb} the market by {_fmt(abs(headline['avg_alpha_pct']))} "
                   f"on average across {headline['benchmarked']} anchored call"
                   f"{'s' if headline['benchmarked'] != 1 else ''}.")
    if forming_n:
        out.append(f"{forming_n} more call{'s' if forming_n != 1 else ''} still forming under the "
                   f"{bar}-day bar — not yet counted above.")
    return out[:5]


def _row(t: ShadowTrade) -> dict:
    age = _age_days(t)
    return {
        "id": t.id,
        "ticker": t.ticker,
        "action": t.action,
        "decision_label": t.decision_label,
        "conviction": t.conviction,
        "source": t.source,
        "sector": t.sector,
        "entry_at": t.entry_at,
        "entry_price": t.entry_price,
        "last_price": t.last_price,
        "return_pct": round(t.return_pct(), 2),
        "alpha_pct": round(t.alpha_pct(), 2) if t.has_benchmark() else None,
        "benchmarked": t.has_benchmark(),
        "age_days": round(age, 1),
        "mature": age >= MATURE_DAYS,
    }


def scorecard(refresh: bool = False) -> dict:
    """The full scorecard — the bottleneck layer between 'interesting assistant'
    and 'something you'd trust with real money.'

    The trusted numbers (headline, calibration, attribution) are computed over
    *matured* calls only — anything younger than MATURE_DAYS is reported as
    "forming" so a few days of fresh marks can't masquerade as a track record."""
    trades = shadow.mark_to_market(refresh=refresh)
    ages = {t.id: _age_days(t) for t in trades}
    mature = [t for t in trades if ages[t.id] >= MATURE_DAYS]
    forming = [t for t in trades if ages[t.id] < MATURE_DAYS]

    headline = _agg(mature)
    headline.update({
        "total": len(trades),
        "matured": len(mature),
        "forming": len(forming),
        "median_age_days": round(_median(list(ages.values())), 1),
        "mature_days": MATURE_DAYS,
    })
    forming_summary = _agg(forming)
    forming_summary["median_age_days"] = round(_median([ages[t.id] for t in forming]), 1)

    by_bucket = _calibration(mature)
    by_source = _group(mature, lambda t: t.source)

    # Leaderboard is ranked by alpha (excess vs SPY), over matured calls that
    # actually carry a benchmark anchor — "which calls beat the market, and by
    # how much," not raw return.
    benched = [t for t in mature if t.has_benchmark()]
    ranked = sorted(benched, key=lambda t: t.alpha_pct(), reverse=True)
    best = ranked[:3]
    best_ids = {t.id for t in best}
    worst = [t for t in reversed(ranked) if t.id not in best_ids][:3]

    return {
        "headline": headline,
        "forming": forming_summary,
        "calibration": by_bucket,
        "by_source": by_source,
        "by_label": _group(mature, lambda t: t.decision_label),
        "by_mode": _group(mature, lambda t: t.risk_mode),
        "narrative": _narrative(headline, forming_summary, by_bucket, by_source),
        "best": [_row(t) for t in best],
        "worst": [_row(t) for t in worst],
        "trades": [_row(t) for t in sorted(trades, key=lambda x: x.entry_at, reverse=True)],
    }
