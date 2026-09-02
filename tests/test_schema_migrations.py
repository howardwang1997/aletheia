"""PF-1: Alembic owns durable schema state and runtime checks fail closed."""

from __future__ import annotations

import runpy
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
from aletheia.execution.qualification_deployment import (
    EXPECTED_EXECUTION_SCHEMA_REVISION,
)
from aletheia.schema_migrations import require_schema_exact


def test_repository_has_one_expected_alembic_head():
    assert expected_schema_revision() == "20260903_0032"


def test_qualification_deployment_pins_the_repository_alembic_head():
    assert EXPECTED_EXECUTION_SCHEMA_REVISION == expected_schema_revision()


def test_real_time_endurance_uses_exact_transaction_clock_guards():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260828_0029_realtime_endurance_transaction_clock.py"
    )
    migration = runpy.run_path(str(migration_path))
    expected_guards = (
        ("aletheia_validate_research_endurance_gate", "started_at"),
        ("aletheia_validate_research_endurance_checkpoint", "observed_at"),
        ("aletheia_validate_research_endurance_report", "completed_at"),
    )
    assert migration["_GUARDS"] == expected_guards
    for _, timestamp_field in expected_guards:
        assert (
            migration["_old_guard"](timestamp_field)
            == f"abs(extract(epoch FROM (clock_timestamp() - NEW.{timestamp_field}))) > 5"
        )
        assert (
            migration["_transaction_guard"](timestamp_field)
            == f"NEW.{timestamp_field} IS DISTINCT FROM transaction_timestamp()"
        )


def test_runtime_v2_deferred_validator_uses_frozen_owner_authority():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260829_0030_execution_runtime_trigger_authority.py"
    )
    migration = runpy.run_path(str(migration_path))
    assert migration["_FUNCTION_IDENTITY"] == "public.aletheia_execution_check_runtime_v2_attempt()"
    assert migration["_SAFE_SEARCH_PATH"] == "search_path=pg_catalog, public"


def test_prelaunch_lease_contraction_requires_exact_runtime_authority():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260831_0031_prelaunch_lease_contraction.py"
    )
    migration = runpy.run_path(str(migration_path))
    attempt_guard = migration["_ATTEMPT_CONTRACTION_GUARD"]
    resource_guard = migration["_RESOURCE_CONTRACTION_GUARD"]
    assert "OLD.status = 'reserved'" in attempt_guard
    assert "NEW.status = 'starting'" in attempt_guard
    assert "execution_runtime_launch_authorizations" in attempt_guard
    assert "authorization_json->>'lease_expires_at'" in attempt_guard
    assert "attempt_row.lease_expires_at = NEW.lease_expires_at" in resource_guard
    assert "launch_row.authorization_sha256" in resource_guard


def test_attempt_scoped_cleanup_migration_keeps_legacy_shape_and_release_only_guard():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260903_0032_attempt_scoped_cleanup_recovery.py"
    )
    migration = runpy.run_path(str(migration_path))
    receipt_shape = migration["_RECOVERY_RECEIPT_SHAPE"]
    decision_guard = migration["_RECOVERY_RUNTIME_PIN_TIME_GUARD"]
    json_guard = migration["_RECOVERY_ABSENCE_JSON_GUARD"]
    assert "CASE WHEN value ? 'cleanup_recovery_authority'" in receipt_shape
    assert 'ELSE\n                \'{"schema_name":"string"' in receipt_shape
    assert "d.disposition IS DISTINCT FROM 'released'" in decision_guard
    assert "d.replacement_request_sha256 IS NOT NULL" in decision_guard
    assert "(d.absence_receipt_json->>'signed_at')::timestamptz <" in decision_guard
    assert "(d.absence_receipt_json->>'expires_at')::timestamptz > LEAST(" in decision_guard
    assert "runtime_launch_authorization_sha256" in json_guard
    assert "cleanup_absence_epoch" in json_guard

    runtime_v2_source = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260827_0026_runtime_v2_lifecycle.py"
    ).read_text()
    rewritten = runtime_v2_source
    for _function_name, old, new, _label in migration["_upgrade_pairs"]():
        assert rewritten.count(old) == 1
        rewritten = rewritten.replace(old, new)
        assert new in rewritten
    for _function_name, old, new, _label in reversed(migration["_upgrade_pairs"]()):
        assert rewritten.count(new) == 1
        rewritten = rewritten.replace(new, old)
    assert rewritten == runtime_v2_source


def test_arl1_replicate_campaign_replaces_single_sea_per_action_constraint():
    from sqlalchemy import Index, UniqueConstraint

    from aletheia.observations.persistence import (
        ResearchScientificExecutionAuthorizationRecord,
    )

    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260828_0028_arl1_replicate_campaign.py"
    ).read_text()
    constraints = ResearchScientificExecutionAuthorizationRecord.__table__.constraints
    indexes = ResearchScientificExecutionAuthorizationRecord.__table__.indexes
    assert "uq_rsea_source_event" not in {
        item.name for item in constraints if isinstance(item, UniqueConstraint)
    }
    assert "ix_rsea_quest_source_event" in {
        item.name for item in indexes if isinstance(item, Index)
    }
    assert '"uq_rsea_source_event"' in migration
    assert '"ix_rsea_quest_source_event"' in migration
    assert "HAVING count(*) > 1" in migration


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


def test_pr5_json_authority_checks_match_migration_and_orm_metadata():
    from sqlalchemy import CheckConstraint
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    from aletheia.observations.persistence import (
        ResearchContinuationReceiptRecord,
        ResearchControllerDeliveryAttemptRecord,
        ResearchControllerDeliveryRecord,
        ResearchControllerDeliveryResolutionRecord,
        ResearchControllerRegistrationRecord,
        ResearchObservationAdmissionRecord,
        ResearchObservationIssuanceChallengeRecord,
        ResearchObservationValidationReceiptRecord,
        ResearchProtocolCompilationRecord,
        ResearchScientificExecutionAuthorizationRecord,
    )

    expected = {
        ResearchControllerRegistrationRecord: "ck_rc_reg_json",
        ResearchControllerDeliveryRecord: "ck_rc_delivery_json",
        ResearchControllerDeliveryAttemptRecord: "ck_rcda_json",
        ResearchControllerDeliveryResolutionRecord: "ck_rcdr_json",
        ResearchProtocolCompilationRecord: "ck_rpc_json",
        ResearchScientificExecutionAuthorizationRecord: "ck_rsea_json",
        ResearchObservationIssuanceChallengeRecord: "ck_roic_json",
        ResearchObservationValidationReceiptRecord: "ck_rovr_json",
        ResearchObservationAdmissionRecord: "ck_roa_json",
        ResearchContinuationReceiptRecord: "ck_rcr_json",
    }
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260828_0027_scientific_controller_persistence.py"
    ).read_text()

    for record_type, constraint_name in expected.items():
        matching = {
            constraint.name
            for constraint in record_type.__table__.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name == constraint_name
        }
        assert matching == {constraint_name}
        assert f"CONSTRAINT {constraint_name} CHECK" in str(
            CreateTable(record_type.__table__).compile(dialect=postgresql.dialect())
        )
        assert constraint_name not in str(
            CreateTable(record_type.__table__).compile(dialect=sqlite.dialect())
        )
        assert f"CONSTRAINT {constraint_name} CHECK" in migration


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
