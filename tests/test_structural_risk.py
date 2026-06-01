"""Tests for the structural-risk engine (brain/engines/structural_risk.py).

The model does the clustering (mocked here); the code must compute each cluster's combined weight
from the *real* portfolio weights, drop forced single-name clusters, set the concentrated flag and
headline off a threshold, and emit one cooldowned portfolio-level ping when concentrated. No LLM.
"""
import os
import tempfile
import unittest

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

from brain.db import repository as db_repo  # noqa: E402
from brain.db.session import db_session  # noqa: E402
from brain.db.models import ResearchEventRecord  # noqa: E402
from brain.engines import structural_risk as sr  # noqa: E402
from brain.engines.structural_risk import _Cluster, _ClusterPlan  # noqa: E402
from brain.models import Holding, Portfolio, RiskProfile  # noqa: E402

_REAL_PARSE = sr.llm.parse  # captured at collection time for restore


def _pf():
    # weights: NVDA 50, AVGO 20, VRT 10 (all "AI capex" = 80%), KO 20 (independent)
    return Portfolio(holdings=[
        Holding(ticker="NVDA", quantity=1, avg_cost=50, current_price=50, sector="Technology"),
        Holding(ticker="AVGO", quantity=1, avg_cost=20, current_price=20, sector="Technology"),
        Holding(ticker="VRT", quantity=1, avg_cost=10, current_price=10, sector="Industrials"),
        Holding(ticker="KO", quantity=1, avg_cost=20, current_price=20, sector="Consumer Staples"),
    ], cash=0)


def _plan():
    return _ClusterPlan(clusters=[
        _Cluster(label="AI data-center capex", driver="hyperscaler capex",
                 breaks_if="capex guidance gets cut", tickers=["NVDA", "AVGO", "VRT"]),
        _Cluster(label="Consumer staples", driver="consumer demand", tickers=["KO"]),
    ], note="tech-heavy book")


class StructuralRiskTests(unittest.TestCase):
    def setUp(self):
        db_repo.recent_events(limit=1)               # force DB init
        with db_session() as s:
            s.query(ResearchEventRecord).delete()
        sr.llm.parse = lambda *a, **k: _plan()
        self.profile = RiskProfile()

    def tearDown(self):
        sr.llm.parse = _REAL_PARSE

    def test_clusters_weighted_from_real_portfolio_and_concentration_flagged(self):
        res = sr.analyze(_pf(), self.profile)
        # the single-name independent cluster (KO, 20%) is dropped; AI cluster kept
        self.assertEqual(len(res.clusters), 1)
        top = res.clusters[0]
        self.assertEqual(top.label, "AI data-center capex")
        self.assertEqual(set(top.tickers), {"NVDA", "AVGO", "VRT"})
        self.assertEqual(top.weight_pct, 80.0)        # computed by code, not the model
        self.assertTrue(res.concentrated)             # 80% >= 40% threshold
        self.assertIn("80%", res.headline)

    def test_calm_book_not_concentrated(self):
        sr.llm.parse = lambda *a, **k: _ClusterPlan(clusters=[
            _Cluster(label="AI", driver="ai", tickers=["NVDA", "AVGO"]),  # 70% here...
        ], note="")
        # ...but spread the book so no cluster is large: equal-weight 5 names
        pf = Portfolio(holdings=[Holding(ticker=t, quantity=1, avg_cost=10, current_price=10)
                                 for t in ("NVDA", "AVGO", "KO", "JPM", "XOM")], cash=0)
        # NVDA+AVGO = 40% exactly; make it 2 of 5 = 40% -> borderline. Use 6 names for <40.
        pf.holdings.append(Holding(ticker="PG", quantity=1, avg_cost=10, current_price=10))
        res = sr.analyze(pf, self.profile)
        self.assertFalse(res.concentrated)            # 2/6 = 33% < 40
        self.assertIn("spread", res.headline.lower() + " ")

    def test_alert_fires_once_then_cooldown(self):
        res = sr.analyze(_pf(), self.profile)
        self.assertTrue(sr.maybe_alert(res))          # first time → ping fires
        self.assertTrue(db_repo.event_exists_recent("structural_risk", "", within_hours=24.0))
        self.assertFalse(sr.maybe_alert(res))         # cooldown → no repeat

    def test_no_alert_when_not_concentrated(self):
        from brain.models import StructuralRisk
        self.assertFalse(sr.maybe_alert(StructuralRisk(headline="spread", concentrated=False)))


if __name__ == "__main__":
    unittest.main()
