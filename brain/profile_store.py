"""Persistence + learning for the user's risk/personality profile.

The profile is the lens every recommendation passes through. It also learns:
when the user accepts or rejects an idea, we nudge the profile so future
recommendations drift toward what they actually like.
"""
from __future__ import annotations

from . import config
from .models import RiskProfile, _now


def load_profile() -> RiskProfile:
    if config.PROFILE_PATH.exists():
        return RiskProfile.model_validate_json(config.PROFILE_PATH.read_text())
    # First run: a sensible neutral default the user will tune.
    profile = RiskProfile()
    save_profile(profile)
    return profile


def save_profile(profile: RiskProfile) -> RiskProfile:
    profile.updated_at = _now()
    config.PROFILE_PATH.write_text(profile.model_dump_json(indent=2))
    return profile


def record_feedback(ticker: str, accepted: bool) -> RiskProfile:
    """Learn from a single accept/reject decision (delegates to the learning
    engine, which captures the stock's characteristics and re-infers tendencies)."""
    from . import profile_learning
    profile = profile_learning.record_feedback(load_profile(), ticker, accepted)
    return save_profile(profile)
