"""Profile learning — make the risk profile actually evolve.

Two evidence sources, both turned into transparent, logged adjustments:

  1. FEEDBACK — every 👍/👎 is stored with the stock's beta/sector/dividend/flavor.
     From the accumulated events we infer real tendencies: do you gravitate to
     high-beta growth? dividend payers? particular sectors? and nudge the profile.

  2. HOLDINGS — your actual book is the strongest signal of who you are. We read
     its weighted beta, sector tilt, and dividend exposure into a one-line
     "investor signature" the brain uses on every recommendation.

Everything is deterministic and logged — each change records *why*, so you can
see (and override) how the brain's read of you is evolving.
"""
from __future__ import annotations

from collections import Counter

from .data.prices import get_signals
from .models import FeedbackEvent, Portfolio, RiskAppetite, RiskProfile, _now

_MIN_EVENTS = 3          # don't infer from too little signal
_DIV_THRESHOLD = 1.5     # % yield that counts as "a dividend payer"


def _flavor(beta: float, vol: float) -> str:
    if beta and beta <= 1.1 and vol <= 35:
        return "stable"
    if beta >= 1.2 or vol >= 45:
        return "volatile"
    return "moderate"


def _log(profile: RiskProfile, msg: str) -> None:
    stamped = f"{_now()[:10]} · {msg}"
    profile.learning_log = ([stamped] + profile.learning_log)[:25]


# --------------------------------------------------------------------------- #
# 1. Feedback → event with characteristics, then re-infer tendencies
# --------------------------------------------------------------------------- #
def record_feedback(profile: RiskProfile, ticker: str, accepted: bool) -> RiskProfile:
    ticker = ticker.upper()
    sig = get_signals(ticker)
    profile.feedback_events.append(FeedbackEvent(
        ticker=ticker, accepted=accepted, beta=sig.beta, sector=sig.sector,
        flavor=_flavor(sig.beta, sig.vol_annualized_pct), dividend_yield=sig.dividend_yield,
    ))
    profile.feedback_events = profile.feedback_events[-100:]
    return _infer_from_feedback(profile)


def _infer_from_feedback(profile: RiskProfile) -> RiskProfile:
    acc = [e for e in profile.feedback_events if e.accepted]
    rej = [e for e in profile.feedback_events if not e.accepted]
    if len(acc) < _MIN_EVENTS:
        return profile

    # --- volatility tendency → appetite nudge ---
    betas = [e.beta for e in acc if e.beta > 0]
    if betas:
        avg_beta = sum(betas) / len(betas)
        if avg_beta >= 1.3 and profile.appetite != RiskAppetite.aggressive:
            profile.appetite = RiskAppetite.aggressive
            _log(profile, f"Nudged appetite → aggressive (your accepted ideas average beta {avg_beta:.2f}).")
        elif avg_beta <= 0.9 and profile.appetite != RiskAppetite.conservative:
            profile.appetite = RiskAppetite.conservative
            _log(profile, f"Nudged appetite → conservative (your accepted ideas average beta {avg_beta:.2f}).")

    # --- sector preferences ---
    fav = [s for s, _ in Counter(e.sector for e in acc if e.sector).most_common(2)]
    for s in fav:
        if s and s not in profile.favor_sectors:
            profile.favor_sectors.append(s)
            _log(profile, f"Added '{s}' to favored sectors (you've liked multiple names there).")
    rej_sectors = Counter(e.sector for e in rej if e.sector)
    acc_sectors = {e.sector for e in acc}
    for s, n in rej_sectors.items():
        if n >= 2 and s not in acc_sectors and s not in profile.avoid_sectors:
            profile.avoid_sectors.append(s)
            _log(profile, f"Added '{s}' to avoided sectors (you've passed on it {n}×).")

    # --- dividend preference ---
    payers = sum(1 for e in acc if e.dividend_yield >= _DIV_THRESHOLD)
    if payers / len(acc) >= 0.6 and not profile.prefers_dividends:
        profile.prefers_dividends = True
        _log(profile, f"Set dividend preference on ({payers}/{len(acc)} of your likes pay a real yield).")
    return profile


# --------------------------------------------------------------------------- #
# 2. Holdings → investor signature
# --------------------------------------------------------------------------- #
def learn_from_holdings(profile: RiskProfile, pf: Portfolio) -> RiskProfile:
    if not pf.holdings or pf.total_value <= 0:
        return profile
    weights = pf.weights()
    wbeta_num, wbeta_den, div_w, sectors = 0.0, 0.0, 0.0, Counter()
    for h in pf.holdings:
        sig = get_signals(h.ticker)
        w = weights.get(h.ticker, 0)
        if sig.beta > 0:
            wbeta_num += sig.beta * w
            wbeta_den += w
        if sig.dividend_yield >= _DIV_THRESHOLD:
            div_w += w
        if sig.sector:
            sectors[sig.sector] += w

    wbeta = wbeta_num / wbeta_den if wbeta_den else 0.0
    top = [s for s, _ in sectors.most_common(2)]
    style = "aggressive/high-beta" if wbeta >= 1.3 else "defensive/low-beta" if wbeta and wbeta <= 0.9 else "balanced"
    sig_parts = [f"actual book runs ~{wbeta:.2f} beta ({style})"]
    if top:
        sig_parts.append(f"concentrated in {', '.join(top)}")
    if div_w >= 40:
        sig_parts.append(f"{div_w:.0f}% in dividend payers")
    new_sig = "; ".join(sig_parts) + "."
    if new_sig != profile.investor_signature:
        profile.investor_signature = new_sig
        _log(profile, f"Updated signature from your holdings: {new_sig}")
    return profile
