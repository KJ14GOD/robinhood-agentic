"""The discovery universe — the pool the screener scans.

Loads the full S&P 500 (bundled in sp500.json) plus a curated set of
high-interest names that aren't in the index (recent IPOs, story stocks,
international ADRs, ETFs, and common retail/AI/defense/energy names). A local
data_store/universe_extra.json file can add more symbols without code changes.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent
_ROOT = _DIR.parents[1]
_CUSTOM = _ROOT / "data_store" / "universe_extra.json"

# Popular names not always in the S&P 500 that users care about.
_EXTRAS: list[str] = [
    "PLTR", "COIN", "RBLX", "SOFI", "RIVN", "HOOD", "SNAP", "PINS", "DKNG",
    "ARM", "SMCI", "MSTR", "U", "AFRM", "RDDT", "DASH", "TSM", "ASML", "SHOP",
    "NU", "GRAB", "ROKU", "TTD", "ZS", "DDOG", "NET", "MDB", "CRWD", "SNOW",
    # AI / chips / infrastructure
    "NVDA", "AMD", "AVGO", "MRVL", "MU", "LRCX", "KLAC", "AMAT", "TER", "ENTG",
    "MPWR", "ON", "WOLF", "ALAB", "CRDO", "AEHR", "COHR", "LITE", "CIEN", "CLS",
    "VRT", "ETN", "PWR", "EME", "FIX", "APLD", "IREN", "CORZ", "HUT", "CLSK",
    # aerospace / defense / space / drones
    "LMT", "NOC", "RTX", "GD", "HII", "LHX", "TXT", "KTOS", "AVAV", "ACHR",
    "JOBY", "RKLB", "ASTS", "SPIR", "BKSY", "ONDS",
    # fintech / crypto / consumer platforms
    "PYPL", "SQ", "UPST", "LC", "BILL", "TOST", "MELI", "SE", "BABA", "JD",
    "PDD", "BIDU", "LI", "NIO", "XPEV",
    # software / security / data
    "S", "PATH", "AI", "SOUN", "BBAI", "GTLB", "CFLT", "ESTC", "HCP", "IOT",
    "APP", "DUOL", "TOOL", "DOCN", "FROG", "NCNO", "TENB", "OKTA", "PANW",
    # biotech / healthcare story names
    "HIMS", "TMDX", "RXRX", "SDGR", "CRSP", "NTLA", "BEAM", "EDIT", "VERV",
    "VKTX", "ALT", "IONQ", "QBTS", "RGTI",
    # energy / materials / uranium / nuclear
    "CEG", "VST", "TLN", "OKLO", "SMR", "CCJ", "UEC", "UUUU", "NXE", "LEU",
    "MP", "ALB", "LAC", "LIT", "URA", "URNM",
    # broad ETFs / factor proxies Autopilot may use as liquid exposure
    "SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "SMH", "SOXX", "XLK", "XLF",
    "XLI", "XLE", "XLY", "XLV", "XLC", "XLU", "XLP", "XLB", "XLRE",
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
def _custom_extras() -> list[str]:
    """Optional local extension: data_store/universe_extra.json as ["TICKER", ...]."""
    if not _CUSTOM.exists():
        return []
    try:
        raw = json.loads(_CUSTOM.read_text())
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, dict):
        raw = raw.get("tickers", [])
    return [str(t).upper().strip() for t in (raw or []) if str(t).strip()]


@lru_cache(maxsize=1)
def full_universe() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in _sp500() + _EXTRAS + _custom_extras():
        u = t.upper().strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def screening_universe(exclude: list[str] | None = None) -> list[str]:
    ex = {t.upper() for t in (exclude or [])}
    return [t for t in full_universe() if t not in ex]
