"""Observation recovery against an untouched, fully migrated PostgreSQL schema."""

from contextlib import contextmanager
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aletheia import schema_migrations
from aletheia.db import expected_schema_revision
from aletheia.observations.coordinator import (
    CommittedAdmissionNotLoaded,
    PostgreSQLAtomicObservationAdmissionCoordinator,
)


@pytest.fixture
def migrated_engine():
    unavailable = pytest.fail if os.environ.get("ALETHEIA_REQUIRE_POSTGRES_TESTS") == "1" else pytest.skip
    raw_url = os.environ.get("ALETHEIA_TEST_MIGRATED_POSTGRES_URL")
    if not raw_url:
        unavailable("requires explicit ALETHEIA_TEST_MIGRATED_POSTGRES_URL")
    url = make_url(raw_url)
    if (
        not url.drivername.startswith("postgresql")
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not url.database
        or not url.database.startswith("aletheia_test")
    ):
        unavailable("requires a loopback aletheia_test* migrated database")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == expected_schema_revision()
        yield engine
    finally:
        engine.dispose()


def test_full_migration_has_exact_schema_and_both_deferred_incorporation_guards(
    migrated_engine, monkeypatch,
):
    monkeypatch.setattr(schema_migrations, "engine", lambda: migrated_engine)
    schema_migrations.require_schema_exact()
    with migrated_engine.connect() as connection:
        guards = connection.execute(text("""
            SELECT tgname, tgdeferrable, tginitdeferred, tgenabled
            FROM pg_trigger
            WHERE tgname IN (
                'trg_roa_incorporation_complete', 'trg_rke_observation_incorporation_complete'
            ) ORDER BY tgname
        """)).all()
        assert len(guards) == 2
        assert all(deferred and initially and enabled == "O" for _, deferred, initially, enabled in guards)
        definition = connection.scalar(text(
            "SELECT pg_get_functiondef('public.aletheia_observation_incorporation_complete()'::regprocedure)"
        ))
        assert definition.count(".source_world_model_sha256") == 2
        assert "protocol,world_model,world_model_sha256" not in definition


def test_empty_admission_recovery_uses_only_reader_privileges(migrated_engine):
    # A distinct role has no INSERT/UPDATE/DELETE grants, signing port or clock.
    role = "aletheia_test_reader_" + uuid4().hex
    with migrated_engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
        connection.exec_driver_sql(f"GRANT SELECT ON research_observation_admissions TO {role}")

    @contextmanager
    def read_session():
        with Session(migrated_engine) as session, session.begin():
            session.execute(text(f"SET LOCAL ROLE {role}"))
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session

    def forbidden_clock(_session):
        raise AssertionError("empty recovery must not issue any timed authority")

    try:
        coordinator = PostgreSQLAtomicObservationAdmissionCoordinator(
            kernel_store=None, kernel_authority=None, verification=None,
            controller_principal_id="test:read-only-recovery",
            session_scope_factory=read_session, database_clock=forbidden_clock,
        )
        with pytest.raises(CommittedAdmissionNotLoaded):
            coordinator.load_committed_admission(
                quest_id="qst_" + "1" * 32, action_sha256="2" * 64,
                scientific_slot_id="sos_" + "3" * 32,
            )
        with pytest.raises(DBAPIError) as denied:
            with read_session() as session:
                session.execute(text("DELETE FROM research_observation_admissions WHERE false"))
        assert denied.value.orig.sqlstate in {"25006", "42501"}
    finally:
        with migrated_engine.begin() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")
