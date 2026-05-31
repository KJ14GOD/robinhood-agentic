from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .. import config
from .base import Base


def _database_url() -> str:
    if config.DATABASE_URL.startswith("postgresql://"):
        return config.DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return config.DATABASE_URL


DATABASE_URL = _database_url()
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    # Import model module so SQLAlchemy registers table metadata.
    from . import models as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


# `create_all` only creates missing *tables* — it never adds a newly-introduced
# column to a table that already exists. On Postgres (the deployed DB) we apply
# additive columns idempotently here so a schema bump lands without a manual
# migration. SQLite (tests/dev) always builds the full table via create_all, so
# this is a no-op there.
_ADDITIVE_COLUMNS = [
    "ALTER TABLE missions ADD COLUMN IF NOT EXISTS last_seeded_at TIMESTAMP WITH TIME ZONE",
]


def _ensure_columns() -> None:
    if not DATABASE_URL.startswith("postgresql"):
        return
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            for stmt in _ADDITIVE_COLUMNS:
                conn.execute(text(stmt))
    except Exception:
        return


@contextmanager
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
