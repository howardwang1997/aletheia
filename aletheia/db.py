"""SQLAlchemy engine/session wiring and the fail-closed schema contract.

Phase 0 uses a synchronous engine; async callers (FastAPI, the event bus) write
through ``asyncio.to_thread`` so we keep one simple session model everywhere.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from aletheia.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def engine() -> Engine:
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


@dataclass(frozen=True)
class SchemaStatus:
    """Observed database revision relative to the code's single Alembic head."""

    current_revision: str | None
    expected_revision: str
    has_application_tables: bool

    @property
    def is_current(self) -> bool:
        return self.current_revision == self.expected_revision


class SchemaCompatibilityError(RuntimeError):
    """Raised when runtime code and the database schema are not exactly compatible."""


def alembic_config() -> Config:
    if not ALEMBIC_INI.is_file():
        raise SchemaCompatibilityError(f"missing Alembic config: {ALEMBIC_INI}")
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    return cfg


def expected_schema_revision() -> str:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise SchemaCompatibilityError(
            f"expected exactly one Alembic head, found {len(heads)}: {heads}"
        )
    return heads[0]


def schema_status(connection: Connection | None = None) -> SchemaStatus:
    """Read schema state without mutating it."""
    owned = connection is None
    conn = connection or engine().connect()
    try:
        tables = set(inspect(conn).get_table_names())
        current = MigrationContext.configure(conn).get_current_revision()
        return SchemaStatus(
            current_revision=current,
            expected_revision=expected_schema_revision(),
            has_application_tables=bool(tables - {"alembic_version"}),
        )
    finally:
        if owned:
            conn.close()


def require_schema_current(connection: Connection | None = None) -> SchemaStatus:
    """Fail closed unless the database is stamped at the code's exact Alembic head."""
    status = schema_status(connection)
    if status.is_current:
        return status
    if status.current_revision is None and status.has_application_tables:
        action = (
            "pre-Alembic schema detected; back it up, then run "
            "`conda run -n aletheia python scripts/adopt_schema_baseline.py`"
        )
    elif status.current_revision is None:
        action = "empty database; run `conda run -n aletheia alembic upgrade head`"
    else:
        known_revisions = {
            item.revision for item in ScriptDirectory.from_config(alembic_config()).walk_revisions()
        }
        if status.current_revision in known_revisions:
            action = "run `conda run -n aletheia alembic upgrade head` with a verified backup"
        else:
            action = (
                "database revision is newer or unknown to this build; deploy matching code or "
                "restore a compatible verified backup"
            )
    raise SchemaCompatibilityError(
        "database schema is not compatible with this Aletheia build "
        f"(current={status.current_revision!r}, expected={status.expected_revision!r}); {action}"
    )


def create_all() -> None:
    """Backward-compatible test/dev entry point that delegates to Alembic.

    Existing tests historically call ``create_all``. Keeping the name avoids a flag-day rewrite,
    but it no longer calls SQLAlchemy ``create_all`` or executes ad-hoc DDL. An unversioned legacy
    database still fails closed and requires the explicit audited adoption command.
    """
    from alembic import command

    status = schema_status()
    if status.is_current:
        return
    if status.current_revision is None and status.has_application_tables:
        require_schema_current()  # raises with the reviewed legacy-adoption instruction
    command.upgrade(alembic_config(), "head")
    require_schema_current()
