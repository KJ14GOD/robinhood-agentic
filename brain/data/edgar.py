"""SEC EDGAR — free, authoritative primary-source data for the deep researcher.

This is the "read the actual document" layer that separates frontier-grade analysis from
summarizing news *about* a company. Three things the researcher can pull, all free, no API key
(SEC only asks for a descriptive User-Agent):

  - `recent_filings(ticker)` — what the company has officially filed and when (8-K = material
    event, 10-Q/10-K = periodic) with a link to each primary document.
  - `financial_facts(ticker)` — the actual reported XBRL numbers (revenue, net income, etc.)
    over recent periods. Structured, clean, no HTML parsing.
  - `filing_text(ticker, form)` — a cleaned text excerpt of the latest filing of a given form,
    for reading what an 8-K or 10-K actually said. Best-effort (filings are large HTML).

Everything degrades gracefully: a network hiccup or an unmapped ticker returns empty, never raises
into the caller. Light TTL caching keeps us well under SEC's rate limits.
"""
from __future__ import annotations

import html
import os
import re
import time
from typing import Any

import requests

from .prices import clean_ticker

# SEC asks API users to identify themselves. Override via env for a real contact if you scale up.
_UA = os.environ.get("EDGAR_USER_AGENT", "robinhood-agentic research tool (personal use)")
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}
_TIMEOUT = 12

_TICKER_TTL = 86400          # ticker→CIK map changes rarely
_DATA_TTL = 6 * 3600         # submissions / facts — a few hours is plenty
_TEXT_TTL = 24 * 3600        # a specific filing's text never changes; cache hard

_cache: dict[str, tuple[float, Any]] = {}
_ticker_map: tuple[float, dict[str, str]] | None = None

# The XBRL concepts worth surfacing, in display order. SEC reports revenue under two common tags.
_CONCEPTS = [
    ("Revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]),
    ("Net income", ["NetIncomeLoss"]),
    ("Operating income", ["OperatingIncomeLoss"]),
    ("Gross profit", ["GrossProfit"]),
    ("Diluted EPS", ["EarningsPerShareDiluted"]),
    ("Total assets", ["Assets"]),
    ("Total liabilities", ["Liabilities"]),
    ("Shareholders' equity", ["StockholdersEquity"]),
    ("Cash & equivalents", ["CashAndCashEquivalentsAtCarryingValue"]),
]


def _fresh(key: str, ttl: int) -> Any | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _get_json(url: str, ttl: int) -> Any | None:
    cached = _fresh(url, ttl)
    if cached is not None:
        return cached
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        _cache[url] = (time.time(), data)
        return data
    except Exception:  # noqa: BLE001 — EDGAR is best-effort; never raise into the researcher
        return None


def _cik_for(ticker: str) -> str | None:
    """Zero-padded 10-digit CIK for a ticker, or None if EDGAR doesn't list it."""
    global _ticker_map
    t = clean_ticker(ticker)
    if not _ticker_map or time.time() - _ticker_map[0] >= _TICKER_TTL:
        data = _get_json("https://www.sec.gov/files/company_tickers.json", _TICKER_TTL)
        mapping: dict[str, str] = {}
        if isinstance(data, dict):
            for row in data.values():
                try:
                    mapping[str(row["ticker"]).upper()] = f"{int(row['cik_str']):010d}"
                except (KeyError, ValueError, TypeError):
                    continue
        _ticker_map = (time.time(), mapping)
    return _ticker_map[1].get(t)


def recent_filings(ticker: str, limit: int = 8) -> list[dict]:
    """Recent official filings, newest first: form type, date, description, and a doc link."""
    cik = _cik_for(ticker)
    if not cik:
        return []
    data = _get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", _DATA_TTL)
    try:
        recent = data["filings"]["recent"]
    except (TypeError, KeyError):
        return []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    descs = recent.get("primaryDocDescription", [])
    cik_int = int(cik)
    out: list[dict] = []
    for i in range(min(len(forms), len(dates), len(accns))):
        accn_nodash = accns[i].replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{doc}"
               if doc else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")
        out.append({
            "form": forms[i],
            "date": dates[i],
            "description": (descs[i] if i < len(descs) else "") or "",
            "url": url,
        })
        if len(out) >= limit:
            break
    return out


def _latest_values(facts: dict, tags: list[str], n: int = 4) -> list[dict]:
    """Most recent `n` reported values for the first matching XBRL tag, newest first."""
    gaap = (facts.get("facts", {}) or {}).get("us-gaap", {}) or {}
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        units = node.get("units", {}) or {}
        series = units.get("USD") or units.get("USD/shares") or next(iter(units.values()), [])
        rows = [r for r in series if r.get("end") and r.get("val") is not None]
        # Prefer one value per period end; keep the latest-filed for each end date.
        by_end: dict[str, dict] = {}
        for r in sorted(rows, key=lambda x: (x.get("end", ""), x.get("filed", ""))):
            by_end[r["end"]] = r
        ordered = sorted(by_end.values(), key=lambda x: x["end"], reverse=True)
        if ordered:
            return ordered[:n]
    return []


def financial_facts(ticker: str) -> dict:
    """Key reported financials over recent periods. Returns {} if unavailable.

    Shape: {"ticker", "entity", "metrics": [{"label","tag","points":[{"end","val","form","fy","fp"}]}]}.
    """
    cik = _cik_for(ticker)
    if not cik:
        return {}
    facts = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", _DATA_TTL)
    if not isinstance(facts, dict) or "facts" not in facts:
        return {}
    metrics = []
    for label, tags in _CONCEPTS:
        pts = _latest_values(facts, tags)
        if not pts:
            continue
        metrics.append({
            "label": label,
            "points": [{"end": p["end"], "val": p["val"], "form": p.get("form", ""),
                        "fy": p.get("fy"), "fp": p.get("fp")} for p in pts],
        })
    if not metrics:
        return {}
    return {"ticker": clean_ticker(ticker), "entity": facts.get("entityName", ""), "metrics": metrics}


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/tr|/h[1-6])\s*/?>", "\n", raw)
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = _WS_RE.sub(" ", text)
    return _NL_RE.sub("\n\n", text).strip()


def filing_text(ticker: str, form: str = "10-K", max_chars: int = 16000) -> dict:
    """Cleaned text excerpt of the most recent filing of `form`. Best-effort; {} if unavailable.

    For 10-K/10-Q it tries to anchor on the Risk Factors / MD&A section (the analytically dense
    part) before falling back to the document head. Returns {"form","date","url","text"}.
    """
    target = form.upper().replace(" ", "")
    match = next((f for f in recent_filings(ticker, limit=40)
                  if f["form"].upper().replace(" ", "") == target), None)
    if not match or not match["url"].endswith((".htm", ".html", ".txt")):
        return {}
    cached = _fresh("text:" + match["url"], _TEXT_TTL)
    if cached is not None:
        return cached
    try:
        r = requests.get(match["url"], headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return {}
        text = _strip_html(r.text)
    except Exception:  # noqa: BLE001
        return {}
    # Anchor on the meaty section when present, else take the head.
    low = text.lower()
    anchor = -1
    for marker in ("risk factors", "management's discussion", "management’s discussion"):
        anchor = low.find(marker)
        if anchor != -1:
            break
    excerpt = (text[anchor:anchor + max_chars] if anchor != -1 else text[:max_chars]).strip()
    out = {"form": match["form"], "date": match["date"], "url": match["url"], "text": excerpt}
    _cache["text:" + match["url"]] = (time.time(), out)
    return out


# --------------------------------------------------------------------------- #
# Prompt formatters — what the researcher's tools actually return to the model
# --------------------------------------------------------------------------- #
def _money(v: float | int) -> str:
    a = abs(v)
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= scale:
            return f"${v / scale:.2f}{unit}"
    return f"${v:,.2f}"


def filings_as_prompt(ticker: str, limit: int = 8) -> str:
    rows = recent_filings(ticker, limit=limit)
    if not rows:
        return f"No EDGAR filings found for {ticker}."
    lines = [f"Recent SEC filings for {clean_ticker(ticker)} (newest first):"]
    for f in rows:
        d = f" — {f['description']}" if f["description"] else ""
        lines.append(f"- {f['date']} {f['form']}{d} | {f['url']}")
    return "\n".join(lines)


def facts_as_prompt(ticker: str) -> str:
    data = financial_facts(ticker)
    if not data:
        return f"No EDGAR financial facts found for {ticker}."
    lines = [f"Reported financials for {data['entity'] or clean_ticker(ticker)} (from SEC XBRL):"]
    for m in data["metrics"]:
        pts = m["points"]
        rendered = ", ".join(
            f"{p['end']} ({p.get('fp') or p.get('form') or ''}): "
            + (f"{p['val']:.2f}" if m["label"] == "Diluted EPS" else _money(p["val"]))
            for p in pts)
        lines.append(f"- {m['label']}: {rendered}")
    return "\n".join(lines)


def filing_text_as_prompt(ticker: str, form: str = "10-K", max_chars: int = 16000) -> str:
    out = filing_text(ticker, form=form, max_chars=max_chars)
    if not out:
        return f"Could not retrieve a recent {form} filing text for {ticker}."
    return (f"Excerpt from {clean_ticker(ticker)}'s {out['form']} filed {out['date']} "
            f"({out['url']}):\n\n{out['text']}")
