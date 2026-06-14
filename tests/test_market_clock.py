"""Tests for the US market clock — the Twin only trades during the regular session."""
import os
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.data import market_clock as mc  # noqa: E402

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


class MarketClockTests(unittest.TestCase):
    def test_weekday_midsession_open(self):
        self.assertTrue(mc.is_market_open(et(2026, 6, 17, 11, 0)))   # Wed 11:00 ET
        self.assertEqual(mc.session_phase(et(2026, 6, 17, 11, 0)), "open")

    def test_before_open_and_after_close_closed(self):
        self.assertFalse(mc.is_market_open(et(2026, 6, 17, 9, 0)))   # 09:00 < 09:30
        self.assertFalse(mc.is_market_open(et(2026, 6, 17, 16, 0)))  # 16:00 is closed (half-open interval)
        self.assertFalse(mc.is_market_open(et(2026, 6, 17, 20, 0)))

    def test_weekend_closed(self):
        self.assertFalse(mc.is_market_open(et(2026, 6, 20, 12, 0)))  # Saturday
        self.assertFalse(mc.is_market_open(et(2026, 6, 21, 12, 0)))  # Sunday

    def test_holiday_closed(self):
        self.assertFalse(mc.is_market_open(et(2026, 12, 25, 12, 0)))  # Christmas
        self.assertFalse(mc.is_trading_day(date(2026, 6, 19)))        # Juneteenth

    def test_next_open_skips_weekend(self):
        # Friday after close -> Monday 09:30
        nxt = mc.next_open(et(2026, 6, 19, 17, 0))   # Fri 17:00
        self.assertEqual((nxt.month, nxt.day, nxt.hour, nxt.minute), (6, 22, 9, 30))

    def test_next_open_same_day_before_bell(self):
        nxt = mc.next_open(et(2026, 6, 17, 8, 0))     # Wed 08:00 -> same day 09:30
        self.assertEqual((nxt.month, nxt.day, nxt.hour, nxt.minute), (6, 17, 9, 30))

    def test_next_open_skips_holiday(self):
        # Christmas 2026 is Fri 12/25; eve after close -> Monday 12/28 (12/26-27 weekend)
        nxt = mc.next_open(et(2026, 12, 24, 17, 0))
        self.assertEqual((nxt.month, nxt.day), (12, 28))


if __name__ == "__main__":
    unittest.main()
