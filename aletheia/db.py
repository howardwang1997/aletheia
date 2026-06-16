"""SQLAlchemy engine/session wiring.

Phase 0 uses a synchronous engine; async callers (FastAPI, the event bus) write
through ``asyncio.to_thread`` so we keep one simple session model everywhere.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from aletheia.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def engine():
    global _engine, _SessionFactory
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error."""
    sess = session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def create_all() -> None:
    """Create tables directly (Phase 0 convenience; Alembic owns this later)."""
    from sqlalchemy import text

    import aletheia.memory.ledger  # noqa: F401  (register models)

    eng = engine()
    # pgvector's Vector column (memory_chunks.embedding) needs the extension first.
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(eng)
    # create_all() only CREATES missing tables; it never ALTERs existing ones. Add columns
    # introduced after a table already existed in a live DB (idempotent; no-op once present).
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)"))
        conn.execute(text("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)"))
        conn.execute(text("ALTER TABLE data_assets ADD COLUMN IF NOT EXISTS composition_column VARCHAR(128)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_claims_dedup_key ON claims (dedup_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_experiments_dedup_key ON experiments (dedup_key)"))
