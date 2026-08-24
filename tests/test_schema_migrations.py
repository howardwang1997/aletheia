"""PF-1: Alembic owns durable schema state and runtime checks fail closed."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aletheia.db import (
    SchemaCompatibilityError,
    SchemaStatus,
    expected_schema_revision,
    require_schema_current,
)


def test_repository_has_one_expected_alembic_head():
    assert expected_schema_revision() == "20260825_0024"


def test_local_execution_foundation_is_fenced_and_not_the_legacy_queue():
    source = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260825_0024_local_execution_foundation.py"
    ).read_text()
    assert "execution_qualification_admissions" in source
    assert "execution_device_heads" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "reconciliation must retain every resource and authority hold" in source
    assert "jobs_tasks" not in source


def test_research_authority_backfill_is_closed_against_concurrent_legacy_inserts():
    source = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260824_0023_research_kernel_event_store.py"
    ).read_text()
    lock = source.index("LOCK TABLE research_graph_nodes IN SHARE ROW EXCLUSIVE MODE")
    backfill = source.index("INSERT INTO research_quest_authorities")
    legacy_trigger = source.index("CREATE TRIGGER trg_legacy_program_quest_authority_claim")
    assert lock < backfill < legacy_trigger


def test_current_schema_is_accepted(monkeypatch):
    current = SchemaStatus("r2", "r2", True)
    monkeypatch.setattr("aletheia.db.schema_status", lambda _connection=None: current)
    assert require_schema_current() is current


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (SchemaStatus(None, "r2", False), "empty database"),
        (SchemaStatus(None, "r2", True), "pre-Alembic schema"),
        (SchemaStatus("20260814_0003", "20260815_0004", True), "alembic upgrade head"),
        (SchemaStatus("future", "r2", True), "newer or unknown"),
    ],
)
def test_incompatible_schema_fails_closed(monkeypatch, status, message):
    monkeypatch.setattr("aletheia.db.schema_status", lambda _connection=None: status)
    with pytest.raises(SchemaCompatibilityError, match=message):
        require_schema_current()


def test_application_startup_checks_but_never_creates_tables():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "aletheia" / "api" / "main.py").read_text()
    assert "require_schema_current()" in source
    assert "create_all()" not in source


def test_legacy_create_all_name_delegates_to_alembic(monkeypatch):
    import aletheia.db as module

    calls = []
    monkeypatch.setattr(module, "schema_status", lambda: SchemaStatus(None, "20260815_0004", False))
    monkeypatch.setattr(module, "alembic_config", lambda: "config")
    monkeypatch.setattr(module, "require_schema_current", lambda: calls.append("checked"))
    monkeypatch.setattr("alembic.command.upgrade", lambda cfg, rev: calls.append((cfg, rev)))
    module.create_all()
    assert calls == [("config", "head"), "checked"]


def test_legacy_adoption_rejects_empty_database(monkeypatch):
    from aletheia.schema_migrations import adopt_existing_baseline

    connection = MagicMock()

    @contextmanager
    def connected():
        yield connection

    monkeypatch.setattr("aletheia.schema_migrations.engine", lambda: MagicMock(connect=connected))
    monkeypatch.setattr(
        "aletheia.schema_migrations.inspect", lambda _conn: MagicMock(get_table_names=lambda: [])
    )
    with pytest.raises(SchemaCompatibilityError, match="database is empty"):
        adopt_existing_baseline()


def test_legacy_adoption_rejects_schema_drift(monkeypatch):
    from aletheia.schema_migrations import adopt_existing_baseline

    connection = MagicMock()

    @contextmanager
    def connected():
        yield connection

    monkeypatch.setattr("aletheia.schema_migrations.engine", lambda: MagicMock(connect=connected))
    monkeypatch.setattr(
        "aletheia.schema_migrations.inspect",
        lambda _conn: MagicMock(get_table_names=lambda: ["runs"]),
    )
    monkeypatch.setattr(
        "aletheia.schema_migrations.schema_diffs", lambda *_args, **_kwargs: [("add_table", "x")]
    )
    with pytest.raises(SchemaCompatibilityError, match="refusing to stamp"):
        adopt_existing_baseline()
