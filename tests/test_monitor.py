"""Unit tests for the deterministic monitor engine (brain/engines/monitor.py).

Pure logic, no network/DB — exercises `detect()` directly. Run with:
    .venv/bin/python -m unittest discover -s tests
"""
import unittest

from brain.models import Portfolio, Holding, RiskProfile, ResearchState, Thesis, WatchItem
from brain.data.prices import TrendSignals
from brain.engines import monitor


def _sig(ticker, price=100.0, above_200d=True, rsi_14=50.0):
    return TrendSignals(ticker=ticker, price=price, above_200d=above_200d, rsi_14=rsi_14)


def _types(events, ticker=None):
    return {e["event_type"] for e in events if ticker is None or e["ticker"] == ticker}


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.profile = RiskProfile(max_single_position_pct=15.0)

    def test_calm_book_is_silent(self):
        pf = Portfolio(holdings=[Holding(ticker=f"S{i}", quantity=1, avg_cost=100, current_price=105)
                                 for i in range(10)], cash=0)
        sig = {f"S{i}": _sig(f"S{i}", price=105) for i in range(10)}
        self.assertEqual(monitor.detect(pf, self.profile, ResearchState(), sig), [])

    def test_concentration_fires_over_ceiling_only(self):
        pf = Portfolio(holdings=[
            Holding(ticker="BIG", quantity=1, avg_cost=100, current_price=100),   # ~83%
            Holding(ticker="SM1", quantity=1, avg_cost=10, current_price=10),
            Holding(ticker="SM2", quantity=1, avg_cost=10, current_price=10),
        ], cash=0)
        sig = {t: _sig(t) for t in ("BIG", "SM1", "SM2")}
        ev = monitor.detect(pf, self.profile, ResearchState(), sig)
        self.assertIn("concentration", _types(ev, "BIG"))
        self.assertNotIn("concentration", _types(ev, "SM1"))

    def test_drawdown_and_gain_thresholds(self):
        pf = Portfolio(holdings=[
            Holding(ticker="DN", quantity=1, avg_cost=100, current_price=80),   # -20% -> drawdown
            Holding(ticker="UP", quantity=1, avg_cost=100, current_price=140),  # +40% -> big_gain
            Holding(ticker="MID", quantity=1, avg_cost=100, current_price=105), # +5% -> neither
        ], cash=0)
        sig = {t: _sig(t) for t in ("DN", "UP", "MID")}
        ev = monitor.detect(pf, self.profile, ResearchState(), sig)
        self.assertIn("drawdown", _types(ev, "DN"))
        self.assertIn("big_gain", _types(ev, "UP"))
        self.assertFalse({"drawdown", "big_gain"} & _types(ev, "MID"))

    def test_trend_and_rsi(self):
        pf = Portfolio(holdings=[
            Holding(ticker="WEAK", quantity=1, avg_cost=100, current_price=100),
            Holding(ticker="HOT", quantity=1, avg_cost=100, current_price=100),
            Holding(ticker="COLD", quantity=1, avg_cost=100, current_price=100),
        ], cash=0)
        sig = {
            "WEAK": _sig("WEAK", above_200d=False, rsi_14=55),
            "HOT": _sig("HOT", above_200d=True, rsi_14=75),
            "COLD": _sig("COLD", above_200d=True, rsi_14=22),
        }
        ev = monitor.detect(pf, self.profile, ResearchState(), sig)
        self.assertIn("below_200d", _types(ev, "WEAK"))
        self.assertIn("overbought", _types(ev, "HOT"))
        self.assertIn("oversold", _types(ev, "COLD"))

    def test_rsi_zero_does_not_trip_oversold(self):
        # rsi_14 == 0 means "no data", not oversold — guard against false alerts.
        pf = Portfolio(holdings=[Holding(ticker="ND", quantity=1, avg_cost=100, current_price=100)], cash=0)
        ev = monitor.detect(pf, self.profile, ResearchState(), {"ND": _sig("ND", rsi_14=0.0)})
        self.assertNotIn("oversold", _types(ev, "ND"))

    def test_thesis_review_and_broken(self):
        pf = Portfolio(holdings=[
            Holding(ticker="REV", quantity=1, avg_cost=100, current_price=100),
            Holding(ticker="BRK", quantity=1, avg_cost=100, current_price=100),
            Holding(ticker="OK", quantity=1, avg_cost=100, current_price=100),
        ], cash=0)
        mem = ResearchState(theses={
            "REV": Thesis(ticker="REV", status="review", invalidation="x"),
            "BRK": Thesis(ticker="BRK", status="broken", invalidation="y"),
            "OK": Thesis(ticker="OK", status="active"),
        })
        sig = {t: _sig(t) for t in ("REV", "BRK", "OK")}
        # High ceiling so equal-weight concentration noise doesn't mask the thesis logic.
        profile = RiskProfile(max_single_position_pct=80.0)
        ev = monitor.detect(pf, profile, mem, sig)
        self.assertIn("thesis_review", _types(ev, "REV"))
        self.assertIn("thesis_broken", _types(ev, "BRK"))
        self.assertNotIn("thesis_review", _types(ev, "OK"))
        self.assertNotIn("thesis_broken", _types(ev, "OK"))  # active thesis -> no event

    def test_watchlist_target_hit_only_when_unheld_and_cheap(self):
        pf = Portfolio(holdings=[Holding(ticker="OWNED", quantity=1, avg_cost=10, current_price=10)], cash=0)
        mem = ResearchState(watchlist=[
            WatchItem(ticker="CHEAP", target_entry=20.0, reason="ai"),   # price 18 <= 20 -> hit
            WatchItem(ticker="PRICEY", target_entry=20.0, reason="x"),   # price 25 > 20 -> no
            WatchItem(ticker="OWNED", target_entry=999.0, reason="held"),  # held -> skip
        ])
        sig = {
            "OWNED": _sig("OWNED", price=10),
            "CHEAP": _sig("CHEAP", price=18),
            "PRICEY": _sig("PRICEY", price=25),
        }
        ev = monitor.detect(pf, self.profile, mem, sig)
        self.assertIn("target_hit", _types(ev, "CHEAP"))
        self.assertNotIn("target_hit", _types(ev, "PRICEY"))
        self.assertNotIn("target_hit", _types(ev, "OWNED"))

    def test_event_dicts_have_required_save_kwargs(self):
        pf = Portfolio(holdings=[Holding(ticker="DN", quantity=1, avg_cost=100, current_price=80)], cash=0)
        ev = monitor.detect(pf, self.profile, ResearchState(), {"DN": _sig("DN", above_200d=False)})
        self.assertTrue(ev)
        for e in ev:
            self.assertEqual(set(e), {"event_type", "ticker", "severity", "title", "summary", "source"})
            self.assertIn(e["severity"], {"info", "warn", "alert"})

    def test_earnings_soon_fires_in_window(self):
        from datetime import date, timedelta
        pf = Portfolio(holdings=[Holding(ticker="NVDA", quantity=1, avg_cost=100, current_price=100)], cash=0)
        sig = {"NVDA": _sig("NVDA", price=100, above_200d=True, rsi_14=55)}
        ev = monitor.detect(pf, self.profile, ResearchState(), sig,
                            {"NVDA": date.today() + timedelta(days=3)})
        es = [e for e in ev if e["event_type"] == "earnings_soon"]
        self.assertEqual(len(es), 1)
        self.assertIn("3 days", es[0]["title"])

    def test_earnings_far_off_is_silent(self):
        from datetime import date, timedelta
        pf = Portfolio(holdings=[Holding(ticker="NVDA", quantity=1, avg_cost=100, current_price=100)], cash=0)
        sig = {"NVDA": _sig("NVDA", price=100, above_200d=True, rsi_14=55)}
        ev = monitor.detect(pf, self.profile, ResearchState(), sig,
                            {"NVDA": date.today() + timedelta(days=30)})
        self.assertEqual([e for e in ev if e["event_type"] == "earnings_soon"], [])

    def test_research_stale_fires_for_unheld_aged_thesis(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        mem = ResearchState(theses={"OLD": Thesis(ticker="OLD", status="active", updated_at=old)})
        pf = Portfolio(holdings=[Holding(ticker="HELD", quantity=1, avg_cost=10, current_price=10)], cash=0)
        ev = monitor.detect(pf, self.profile, mem, {"HELD": _sig("HELD")})
        self.assertIn("research_stale", _types(ev, "OLD"))

    def test_research_stale_skips_held_and_fresh(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        mem = ResearchState(theses={
            "HELDOLD": Thesis(ticker="HELDOLD", status="active", updated_at=old),
            "UNHELDNEW": Thesis(ticker="UNHELDNEW", status="active", updated_at=fresh),
        })
        pf = Portfolio(holdings=[Holding(ticker="HELDOLD", quantity=1, avg_cost=10, current_price=10)], cash=0)
        ev = monitor.detect(pf, RiskProfile(max_single_position_pct=80.0), mem, {"HELDOLD": _sig("HELDOLD")})
        self.assertNotIn("research_stale", _types(ev, "HELDOLD"))    # held -> the scheduled revisit handles it
        self.assertNotIn("research_stale", _types(ev, "UNHELDNEW"))  # fresh -> not stale


if __name__ == "__main__":
    unittest.main()
