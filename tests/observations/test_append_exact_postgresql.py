"""Opt-in PostgreSQL regression test for the append-once ``created`` flag.

SQLAlchemy 2.0 ORM-enabled ``INSERT ... ON CONFLICT DO NOTHING`` cannot report
``rowcount`` under psycopg3 (it returns ``-1``, "unknown"), so deriving
``AppendReceipt.created`` from ``rowcount`` made every first append report
``created=False`` on PostgreSQL.  The first live ARL-1 observation admission
(generation 20260907m, release 72615ff) failed closed on exactly this: the
atomic admission coordinator treats a first-insert ``created=False`` as an
unexpected replay of one of its authorities.  SQLite reports ``rowcount``
reliably, which is why the portable fixtures never caught it.

This test runs the shared append seam against a real PostgreSQL engine and is
skipped unless an isolated throwaway database is explicitly provided.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from aletheia.research_kernel.schemas import canonical_sha256

# Register the real referenced tables in Base.metadata so SQLAlchemy can resolve
# every FK while emitting only the isolated observation tables (same trick as the
# portable SQLite harness in persistence_test_support).
import aletheia.execution.persistence  # noqa: F401
import aletheia.jobs.persistence  # noqa: F401
import aletheia.research_store.persistence  # noqa: F401
from aletheia.observations.persistence import OBSERVATION_PERSISTENCE_TABLES
from aletheia.observations.store import (
    ControllerRegistrationWrite,
    get_controller_registration_by_launch_request,
    register_controller,
)
from persistence_test_support import _PARENT_STUBS

UTC = timezone.utc

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", None})


def _isolated_postgres_engine():
    raw_url = os.environ.get("ALETHEIA_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip(
            "append-exact PostgreSQL regression requires explicit ALETHEIA_TEST_POSTGRES_URL"
        )
    url = make_url(raw_url)
    if (
        not url.drivername.startswith("postgresql")
        or url.host not in _LOOPBACK_HOSTS
        or url.database is None
        or not url.database.startswith("aletheia_test")
    ):
        pytest.skip("append-exact PostgreSQL regression requires loopback aletheia_test* database")
    return create_engine(raw_url, future=True)


def test_append_exact_first_append_reports_created_on_postgresql() -> None:
    engine = _isolated_postgres_engine()
    with engine.begin() as connection:
        for statement in _PARENT_STUBS:
            connection.execute(
                text(statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))
            )
    OBSERVATION_PERSISTENCE_TABLES[0].metadata.create_all(
        bind=engine, tables=OBSERVATION_PERSISTENCE_TABLES, checkfirst=True
    )

    marker = datetime.now(UTC).strftime("%H%M%S%f")
    quest = "qst_" + f"{int(marker):032d}"[-32:]
    registered_at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    payload = {
        "schema_name": "aletheia.research_controller_registration",
        "schema_version": 1,
        "registration_id": "rcr_" + f"{int(marker):032d}"[-32:],
        "controller_id": "rctl_" + f"{int(marker):032d}"[-32:],
        "controller_manifest_sha256": "4" * 64,
        "controller_principal_id": "controller:worker",
        "registered_by_principal_id": "controller:launcher",
        "launch_request": {"quest_id": quest},
        "registered_at": "2026-09-08T12:00:00Z",
    }
    write = ControllerRegistrationWrite(
        registration_sha256=canonical_sha256(payload),
        registration_id=payload["registration_id"],
        quest_id=quest,
        controller_id=payload["controller_id"],
        controller_manifest_sha256="4" * 64,
        controller_principal_id="controller:worker",
        registered_by_principal_id="controller:launcher",
        launch_request_sha256=canonical_sha256({"quest_id": quest, "marker": marker}),
        registration_json=payload,
        registered_at=registered_at,
    )

    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_quest_streams (quest_id) VALUES (:quest)"),
            {"quest": quest},
        )

        first = register_controller(session, write)
        assert first.created, "first append through psycopg3 must report created=True"

        replay = register_controller(session, write)
        assert not replay.created, "exact replay must report created=False"

        session.commit()

    with Session(engine) as session:
        persisted = get_controller_registration_by_launch_request(
            session, launch_request_sha256=write.launch_request_sha256
        )
    assert persisted == write
