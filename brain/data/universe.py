"""The discovery universe — the pool the screener scans.

Loads the full S&P 500 (bundled in sp500.json) plus a curated set of
high-interest names that aren't in the index (recent IPOs, story stocks).
That's ~520 tickers, screened in a single batched download.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent

# Popular names not always in the S&P 500 that users care about.
_EXTRAS: list[str] = [
    "PLTR", "COIN", "RBLX", "SOFI", "RIVN", "HOOD", "SNAP", "PINS", "DKNG",
    "ARM", "SMCI", "MSTR", "U", "AFRM", "RDDT", "DASH", "TSM", "ASML", "SHOP",
    "NU", "GRAB", "ROKU", "TTD", "ZS", "DDOG", "NET", "MDB", "CRWD", "SNOW",
]


@lru_cache(maxsize=1)
def _sp500() -> list[str]:
    f = _DIR / "sp500.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            pass
    return []


@lru_cache(maxsize=1)
def full_universe() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in _sp500() + _EXTRAS:
        u = t.upper().strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def screening_universe(exclude: list[str] | None = None) -> list[str]:
    ex = {t.upper() for t in (exclude or [])}
    return [t for t in full_universe() if t not in ex]
