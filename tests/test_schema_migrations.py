"""PF-1: Alembic owns durable schema state and runtime checks fail closed."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from aletheia.db import (
    SchemaCompatibilityError,
    SchemaStatus,
    expected_schema_revision,
    require_schema_current,
)


def test_repository_has_one_expected_alembic_head():
    assert expected_schema_revision() == "20260817_0015"


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
