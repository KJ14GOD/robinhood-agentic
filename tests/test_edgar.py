"""Unit tests for the SEC EDGAR primary-source layer (brain/data/edgar.py).

All HTTP is mocked — no live SEC calls. Covers ticker→CIK mapping, recent filings + URL
construction, XBRL financial-fact extraction, filing-text cleaning/anchoring, the prompt
formatters, and graceful failure on an unmapped ticker.
"""
import unittest

from brain.data import edgar

_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

_SUBS = {"filings": {"recent": {
    "form": ["8-K", "10-Q", "10-K"],
    "filingDate": ["2026-05-01", "2026-04-15", "2026-02-01"],
    "accessionNumber": ["0000320193-26-000050", "0000320193-26-000040", "0000320193-26-000010"],
    "primaryDocument": ["a8k.htm", "a10q.htm", "a10k.htm"],
    "primaryDocDescription": ["Current report", "Quarterly report", "Annual report"],
}}}

_FACTS = {"entityName": "Apple Inc.", "facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        {"end": "2024-09-30", "val": 383000000000, "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2024-11-01"},
        {"end": "2025-09-30", "val": 400000000000, "form": "10-K", "fy": 2025, "fp": "FY", "filed": "2025-11-01"},
    ]}},
    "NetIncomeLoss": {"units": {"USD": [
        {"end": "2025-09-30", "val": 95000000000, "form": "10-K", "fy": 2025, "fp": "FY", "filed": "2025-11-01"},
    ]}},
}}}

_10K_HTML = ("<html><head><style>x{}</style></head><body><h1>Apple 10-K</h1>"
             "<p>Cover page boilerplate.</p><p>Item 1A. Risk Factors</p>"
             "<p>We face intense competition and supply-chain concentration.</p>"
             "<script>tracking()</script></body></html>")


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _fake_get(url, headers=None, timeout=None):
    if "company_tickers.json" in url:
        return _Resp(200, _TICKERS)
    if "submissions/CIK" in url:
        return _Resp(200, _SUBS)
    if "companyfacts" in url:
        return _Resp(200, _FACTS)
    if "Archives/edgar/data" in url:
        return _Resp(200, text=_10K_HTML)
    return _Resp(404)


class EdgarTests(unittest.TestCase):
    def setUp(self):
        edgar._cache.clear()
        edgar._ticker_map = None
        edgar.requests.get = _fake_get

    def test_cik_lookup(self):
        self.assertEqual(edgar._cik_for("AAPL"), "0000320193")
        self.assertIsNone(edgar._cik_for("NOPE"))

    def test_recent_filings_and_url(self):
        rows = edgar.recent_filings("AAPL", limit=3)
        self.assertEqual([r["form"] for r in rows], ["8-K", "10-Q", "10-K"])
        k10 = rows[2]
        self.assertEqual(k10["date"], "2026-02-01")
        # accession dashes stripped, integer CIK in the Archives path
        self.assertEqual(k10["url"],
                         "https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/a10k.htm")

    def test_financial_facts_extraction(self):
        data = edgar.financial_facts("AAPL")
        self.assertEqual(data["entity"], "Apple Inc.")
        labels = [m["label"] for m in data["metrics"]]
        self.assertIn("Revenue", labels)
        self.assertIn("Net income", labels)
        rev = next(m for m in data["metrics"] if m["label"] == "Revenue")
        # newest first, one point per period end
        self.assertEqual(rev["points"][0]["end"], "2025-09-30")
        self.assertEqual(rev["points"][0]["val"], 400000000000)

    def test_facts_prompt_formatting(self):
        out = edgar.facts_as_prompt("AAPL")
        self.assertIn("Apple Inc.", out)
        self.assertIn("$400.00B", out)

    def test_filing_text_anchors_on_risk_factors_and_strips_html(self):
        out = edgar.filing_text("AAPL", form="10-K")
        self.assertEqual(out["form"], "10-K")
        self.assertTrue(out["text"].lower().startswith("risk factors"))
        self.assertIn("intense competition", out["text"])
        self.assertNotIn("tracking()", out["text"])   # script stripped
        self.assertNotIn("<p>", out["text"])           # tags stripped

    def test_graceful_failure_on_unmapped_ticker(self):
        self.assertEqual(edgar.recent_filings("NOPE"), [])
        self.assertEqual(edgar.financial_facts("NOPE"), {})
        self.assertEqual(edgar.filing_text("NOPE"), {})
        self.assertIn("No EDGAR", edgar.facts_as_prompt("NOPE"))


if __name__ == "__main__":
    unittest.main()
