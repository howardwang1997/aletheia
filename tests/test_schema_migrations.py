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
from aletheia.schema_migrations import require_schema_exact


def test_repository_has_one_expected_alembic_head():
    assert expected_schema_revision() == "20260828_0027"


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


def test_qualification_assignments_are_encrypted_and_relationally_complete():
    source = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260826_0025_sealed_execution_assignments.py"
    ).read_text()
    assert "execution_assignment_envelopes" in source
    assert "0025 requires an empty PR-4a attempt store" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "initial assignment differs from first adoption lineage" in source
    assert "lease_token" in source
    assert "raw_token" not in source


def test_runtime_v2_tables_are_excluded_from_legacy_baseline_parity():
    from aletheia.schema_migrations import POST_BASELINE_TABLES

    assert {
        "execution_runtime_preparations",
        "execution_runtime_launch_authorizations",
        "execution_runtime_launch_receipts",
        "execution_pre_runtime_absence_decisions",
        "execution_runtime_fence_rebinds",
        "execution_runtime_termination_challenges",
        "execution_runtime_termination_acceptances",
        "execution_qualification_terminal_deadline_expirations",
        "execution_qualification_terminal_acceptances",
        "execution_qualification_terminal_outbox",
    } <= POST_BASELINE_TABLES


def test_pr5_exact_source_constraints_match_migration_and_orm_metadata():
    from aletheia.execution.persistence import (
        _ExecutionQualificationTerminalOutboxRecord,
    )
    from aletheia.research_store.persistence import (
        ResearchKernelEventRecord,
        ResearchKernelOutboxRecord,
    )
    from aletheia.schema_migrations import POST_BASELINE_CONSTRAINTS

    expected = {
        "uq_rke_scoped_typed_event",
        "uq_rko_exact_controller_source",
        "uq_exec_qto_exact_controller_source",
    }
    metadata_names = {
        constraint.name
        for record_type in (
            ResearchKernelEventRecord,
            ResearchKernelOutboxRecord,
            _ExecutionQualificationTerminalOutboxRecord,
        )
        for constraint in record_type.__table__.constraints
    }
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260828_0027_scientific_controller_persistence.py"
    ).read_text()
    assert expected <= metadata_names
    assert expected <= POST_BASELINE_CONSTRAINTS
    assert all(name in migration for name in expected)


def test_alembic_environment_registers_pr5_orm_metadata():
    source = (Path(__file__).parents[1] / "migrations" / "env.py").read_text()
    assert "import aletheia.observations.persistence" in source


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


def test_exact_schema_requires_current_revision_and_zero_structural_diffs(monkeypatch):
    current = SchemaStatus("r2", "r2", True)
    connection = MagicMock()
    monkeypatch.setattr("aletheia.schema_migrations.require_schema_current", lambda conn: current)
    monkeypatch.setattr("aletheia.schema_migrations.schema_diffs", lambda conn: [])

    assert require_schema_exact(connection) is current


def test_exact_schema_rejects_a_current_but_structurally_drifted_database(monkeypatch):
    current = SchemaStatus("r2", "r2", True)
    connection = MagicMock()
    monkeypatch.setattr("aletheia.schema_migrations.require_schema_current", lambda conn: current)
    monkeypatch.setattr(
        "aletheia.schema_migrations.schema_diffs",
        lambda conn: [("add_table", "missing_authority")],
    )

    with pytest.raises(
        SchemaCompatibilityError,
        match="revision is current but its structure differs",
    ):
        require_schema_exact(connection)


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
    assert source.count("require_schema_exact()") == 2
    assert "require_schema_current" not in source
    assert "create_all()" not in source


def test_durable_runtime_entry_points_require_exact_schema_structure():
    root = Path(__file__).parents[1]
    entry_points = (
        "durable_tasks.py",
        "durable_worker.py",
        "manage_knowledge_corpus.py",
        "research_graph.py",
        "research_memory.py",
        "research_portfolio.py",
        "run_research_controller_runtime.py",
        "scientific_transactions.py",
    )
    for name in entry_points:
        source = (root / "scripts" / name).read_text()
        assert "require_schema_exact" in source, name
        assert "require_schema_current" not in source, name


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
