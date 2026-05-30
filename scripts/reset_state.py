"""Reset the brain to a first-run state — wipe the database and local state.

What it does:
  1. Drops EVERY physical table in the database (including any orphan tables left
     by removed features), then recreates the current schema empty.
  2. Deletes the data_store JSON files that load_state/profile/shadow fall back
     to — otherwise they'd immediately re-seed the freshly-wiped database.

This is irreversible — there is no backup. Your live Robinhood account is NOT
touched: holdings repopulate from the broker on the next refresh. Everything the
brain *accumulated* (theses, watchlist, events, briefings, shadow trades, learned
profile) starts blank.

Usage:
    python -m scripts.reset_state         # dry run — shows what it would clear
    python -m scripts.reset_state --yes   # actually wipe
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import MetaData  # noqa: E402

from brain import config  # noqa: E402
from brain.db.session import engine, init_db  # noqa: E402

STATE_FILES = [
    config.PROFILE_PATH,
    config.SHADOW_PATH,
    config.HOLDINGS_CACHE,
    config.RESEARCH_STATE_PATH,
    config.PORTFOLIO_SNAPSHOT_PATH,
]


def _physical_tables() -> list[str]:
    meta = MetaData()
    meta.reflect(bind=engine)
    return sorted(meta.tables.keys())


def main(do_it: bool) -> None:
    tables = _physical_tables()
    files = [p for p in STATE_FILES if p.exists()]
    digests = list(config.DIGEST_DIR.glob("*.json")) if config.DIGEST_DIR.exists() else []

    print(f"Database : {engine.url.render_as_string(hide_password=True)}")
    print(f"Tables to DROP ({len(tables)}): {', '.join(tables) or '(none)'}")
    print(f"Files to DELETE: {', '.join(p.name for p in files) or '(none)'}"
          + (f" + {len(digests)} digest(s)" if digests else ""))

    if not do_it:
        print("\nDry run — nothing changed. Re-run with --yes to actually wipe.")
        return

    meta = MetaData()
    meta.reflect(bind=engine)
    meta.drop_all(bind=engine)
    init_db()
    print(f"Dropped {len(tables)} table(s); recreated empty schema.")

    for p in files + digests:
        p.unlink()
    print(f"Deleted {len(files)} state file(s) and {len(digests)} digest(s).")
    print("\nDone — restart the app for a clean first-run state.")


if __name__ == "__main__":
    main("--yes" in sys.argv)
