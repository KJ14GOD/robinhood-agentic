"""Tests for the Twin engine — inception clone, mark-to-market, fills under fixed capital, and
the You-vs-Twin compare. Offline: quotes and the real portfolio are stubbed; a throwaway DB
backs the fund's books. `execute_pending(force=True)` bypasses the market-hours gate.

Run with: .venv/bin/python -m unittest discover -s tests
"""
import os
import tempfile
import types
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from brain.db import repository as repo  # noqa: E402
from brain.engines import twin  # noqa: E402
from brain.models import Holding, Portfolio  # noqa: E402

QUOTES: dict[str, float] = {}


def _fake_quotes(tickers, refresh=False):
    return {t.upper(): types.SimpleNamespace(price=QUOTES.get(t.upper(), 0.0)) for t in tickers}


def _fake_quote(t, refresh=False):
    return types.SimpleNamespace(price=QUOTES.get(t.upper(), 0.0))


def _pf(cash, holdings):
    """holdings: {ticker: (shares, price)}."""
    return Portfolio(cash=cash, holdings=[Holding(ticker=t, quantity=s, avg_cost=p, current_price=p)
                                          for t, (s, p) in holdings.items()])


class TwinTests(unittest.TestCase):
    def setUp(self):
        repo.reset_twin()
        self._gq, self._gqs = twin.get_quote, twin.get_quotes
        twin.get_quote, twin.get_quotes = _fake_quote, _fake_quotes
        QUOTES.clear()
        QUOTES.update({"NVDA": 100.0, "VOO": 200.0})
        # inception: $2,000 cash + 10 NVDA @100 + 5 VOO @200 = $4,000
        self.real = _pf(2000.0, {"NVDA": (10, 100.0), "VOO": (5, 200.0)})

    def tearDown(self):
        twin.get_quote, twin.get_quotes = self._gq, self._gqs

    def test_inception_clones_the_book(self):
        twin.inception(real_pf=self.real)
        f = twin.state()
        self.assertEqual(f["status"], "running")
        self.assertEqual(f["cash"], 2000.0)
        self.assertEqual(f["inception_value"], 4000.0)
        v = twin.value()
        self.assertAlmostEqual(v["value"], 4000.0)
        poss = {p["ticker"]: p for p in v["positions"]}
        self.assertAlmostEqual(poss["NVDA"]["shares"], 10.0)
        self.assertAlmostEqual(poss["NVDA"]["avg_cost"], 100.0)   # cost = inception price

    def test_inception_is_once_only(self):
        twin.inception(real_pf=self.real)
        # a second call with a different book must NOT re-clone
        twin.inception(real_pf=_pf(9999.0, {"AAPL": (1, 1.0)}))
        f = twin.state()
        self.assertEqual(f["cash"], 2000.0)
        self.assertEqual(f["inception_value"], 4000.0)

    def test_mark_to_market_moves_with_price(self):
        twin.inception(real_pf=self.real)
        QUOTES["NVDA"] = 120.0   # +20%
        v = twin.value(refresh=True)
        self.assertAlmostEqual(v["value"], 2000 + 10 * 120 + 5 * 200)   # 4,200
        nvda = next(p for p in v["positions"] if p["ticker"] == "NVDA")
        self.assertAlmostEqual(nvda["return_pct"], 20.0)

    def test_buy_clamped_to_cash_and_updates_avg(self):
        twin.inception(real_pf=self.real)         # cash 2,000
        QUOTES["NVDA"] = 200.0
        twin.queue_trade("NVDA", "buy", 5, reasoning="add on strength", conviction=7)
        fills = twin.execute_pending(force=True)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["filled_shares"], 5.0)        # 5 * 200 = 1,000 <= cash
        self.assertAlmostEqual(twin.state()["cash"], 1000.0)
        pos = repo.get_twin_position("NVDA")
        self.assertAlmostEqual(pos["shares"], 15.0)
        self.assertAlmostEqual(pos["avg_cost"], (10 * 100 + 5 * 200) / 15)   # 133.33

    def test_buy_beyond_cash_is_clamped(self):
        twin.inception(real_pf=self.real)         # cash 2,000
        QUOTES["NVDA"] = 100.0
        twin.queue_trade("NVDA", "buy", 1000)     # would cost 100k; only 2k cash
        twin.execute_pending(force=True)
        self.assertAlmostEqual(twin.state()["cash"], 0.0)
        self.assertAlmostEqual(repo.get_twin_position("NVDA")["shares"], 30.0)   # 10 + 20 afforded

    def test_sell_raises_cash_and_can_close(self):
        twin.inception(real_pf=self.real)
        twin.queue_trade("VOO", "sell", 5)        # close the whole VOO position @200
        twin.execute_pending(force=True)
        self.assertAlmostEqual(twin.state()["cash"], 2000 + 1000)   # +5*200
        self.assertIsNone(repo.get_twin_position("VOO"))

    def test_off_hours_does_not_fill(self):
        twin.inception(real_pf=self.real)
        twin.queue_trade("NVDA", "buy", 1)
        # not forced + (almost certainly) market closed during the test run → stays queued
        if not twin.market_clock.is_market_open():
            self.assertEqual(twin.execute_pending(force=False), [])
            self.assertEqual(len(repo.pending_twin_trades()), 1)

    def test_compare_edge(self):
        twin.inception(real_pf=self.real)          # both start at 4,000
        QUOTES["NVDA"] = 140.0                      # twin: 2000 + 10*140 + 5*200 = 4,400 (+10%)
        real_now = _pf(2000.0, {"NVDA": (10, 120.0), "VOO": (5, 200.0)})   # real: 2000+1200+1000 = 4,200 (+5%)
        c = twin.compare(real_pf=real_now, refresh=True)
        self.assertAlmostEqual(c["twin"]["return_pct"], 10.0)
        self.assertAlmostEqual(c["real"]["return_pct"], 5.0)
        self.assertAlmostEqual(c["edge_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
