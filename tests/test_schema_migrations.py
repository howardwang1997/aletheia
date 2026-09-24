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
    assert expected_schema_revision() == "20260924_0037"


def test_orm_constraint_names_fit_postgres_namedatalen():
    """Contradiction #21: >63-char constraint names render unrepresentable.

    PostgreSQL silently truncates identifiers past NAMEDATALEN (63) in DDL,
    but SQLAlchemy raises IdentifierError client-side when the preparer
    formats such a name during Alembic comparison, so ``require_schema_exact``
    can never pass. Convention-generated names (``conv`` objects, including
    every over-long index name in current metadata) are exempt: the dialect
    truncates those deterministically with a hash suffix on both sides of
    the comparison. Explicit string names — constraints and indexes alike —
    get no such treatment and must stay within 63 characters.
    """

    from sqlalchemy.sql.elements import conv

    from aletheia.db import Base

    explicit_names = [
        (table.name, named.name)
        for table in Base.metadata.tables.values()
        for named in (*table.constraints, *table.indexes)
        if not isinstance(named.name, conv) and named.name and len(named.name) > 63
    ]
    assert explicit_names == []


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


def test_0036_external_acceptance_tables_are_excluded_from_legacy_baseline_parity():
    from aletheia.schema_migrations import POST_BASELINE_TABLES

    assert {
        "execution_external_runtime_preparations",
        "execution_external_launch_authorizations",
        "execution_external_runtime_launch_receipts",
        "execution_external_termination_challenges",
        "execution_external_runtime_termination_acceptances",
        "execution_external_qualification_terminal_acceptances",
        "execution_external_qualification_deadline_expirations",
    } <= POST_BASELINE_TABLES


def _migration_drift(source):
    """One migration source's (created tables, added (table, column) pairs,
    unresolvable tracked-DDL descriptions) for the parity drift guard.

    Consumed by the corpus walk below and by the synthetic fail-closed
    test, so both exercise the same extraction code.
    """
    import ast
    import re

    ddl_keywords = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE"}

    def added_columns_in(statement):
        return [
            name
            for name in re.findall(
                r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
                statement,
                re.IGNORECASE,
            )
            if name.upper() not in ddl_keywords
        ]

    def tracked_ddl(sql):
        return bool(re.search(r"\bCREATE\s+TABLE\b", sql, re.IGNORECASE)) or bool(
            added_columns_in(sql)
        )

    def string_tuples(tree):
        flat = {}
        nested = {}
        scalars = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            scalars[target.id] = node.value.value
                continue
            if not (isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List))):
                continue
            elements = node.value.elts
            strings = [
                element.value
                for element in elements
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            rows = None
            if elements and all(isinstance(element, (ast.Tuple, ast.List)) for element in elements):
                rows = []
                for element in elements:
                    inner = [
                        part.value
                        for part in element.elts
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    ]
                    if len(inner) != len(element.elts):
                        rows = None
                        break
                    rows.append(inner)
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if len(strings) == len(elements):
                    flat.setdefault(target.id, set()).update(strings)
                if rows is not None:
                    nested[target.id] = rows
        resolved = dict(flat)
        for node in ast.walk(tree):
            # for a, b, ... in nested_tuple_of_string_tuples: position-wise
            # resolution -- 0003 builds its six membership tables this way.
            if not (isinstance(node, ast.For) and isinstance(node.iter, ast.Name)):
                continue
            rows = nested.get(node.iter.id)
            if rows is None or not isinstance(node.target, ast.Tuple):
                continue
            for position, target in enumerate(node.target.elts):
                if not (isinstance(target, ast.Name) and all(len(row) > position for row in rows)):
                    continue
                resolved.setdefault(target.id, set()).update(row[position] for row in rows)
        return resolved, scalars

    tree = ast.parse(source)
    tuples, scalars = string_tuples(tree)
    created = set()
    added = set()
    unresolvable = []
    execute_sql = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "op"):
            continue
        if node.func.attr == "create_table":
            argument = node.args[0] if node.args else None
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                created.add(argument.value)
            elif isinstance(argument, ast.Name) and argument.id in tuples:
                created |= tuples[argument.id]
            else:
                unresolvable.append("op.create_table argument")
        if node.func.attr == "add_column":
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            table_argument = node.args[0] if node.args else keywords.get("table_name")
            column_argument = node.args[1] if len(node.args) > 1 else keywords.get("column")
            table = (
                table_argument.value
                if isinstance(table_argument, ast.Constant)
                and isinstance(table_argument.value, str)
                else None
            )
            column = None
            if (
                isinstance(column_argument, ast.Call)
                and isinstance(column_argument.func, ast.Attribute)
                and column_argument.func.attr == "Column"
                and column_argument.args
                and isinstance(column_argument.args[0], ast.Constant)
                and isinstance(column_argument.args[0].value, str)
            ):
                column = column_argument.args[0].value
            if table is not None and column is not None:
                added.add((table, column))
            else:
                unresolvable.append("op.add_column argument")
        if node.func.attr == "execute" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                execute_sql.append(argument.value)
            elif isinstance(argument, ast.Name) and argument.id in scalars:
                execute_sql.append(scalars[argument.id])
            elif isinstance(argument, ast.JoinedStr):
                literal = " ".join(
                    part.value
                    for part in argument.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                if not literal.strip():
                    # a pure interpolation (f"{ddl}") hides everything the
                    # guard tracks: same verdict as an unseen variable
                    unresolvable.append("op.execute f-string argument")
                elif tracked_ddl(literal):
                    unresolvable.append("op.execute f-string DDL")
            else:
                unresolvable.append("op.execute argument")
    for sql in execute_sql:
        for statement in re.sub(r"\s+", " ", sql).split(";"):
            if "CREATE TABLE" in statement.upper():
                match = re.search(
                    r"CREATE TABLE (?:IF NOT EXISTS )?((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)",
                    statement,
                    re.IGNORECASE,
                )
                if match and "." not in match.group(1):
                    created.add(match.group(1))
                else:
                    unresolvable.append("CREATE TABLE statement")
            table_match = re.search(
                r"ALTER TABLE (?:IF EXISTS )?((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)",
                statement,
                re.IGNORECASE,
            )
            statement_columns = added_columns_in(statement)
            if statement_columns:
                if table_match and "." not in table_match.group(1):
                    for column in statement_columns:
                        added.add((table_match.group(1), column))
                else:
                    unresolvable.append("ALTER TABLE ADD statement")
            elif re.search(
                r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[\"'`(\d]",
                statement,
                re.IGNORECASE,
            ):
                # a tracked ADD whose name is quoted or otherwise not a
                # bare identifier: unresolvable, never a silent pass
                unresolvable.append("ALTER TABLE ADD statement")
    return created, added, unresolvable


def test_every_table_created_after_the_legacy_baseline_is_excluded_from_parity():
    """Guard the legacy-baseline parity exclusions against silent drift.

    0036 wrote its CREATE TABLE statements as raw SQL and 0003 built six
    tables from a loop variable, so the frozenset drifted silently and
    adopt_existing_baseline would refuse to stamp any legacy database.
    Every table a non-baseline migration creates -- op.create_table with
    a literal or loop-variable name resolved against module tuples, or
    CREATE TABLE in a string op.execute -- must already be excluded, and
    every column a migration adds to a compared table (op.add_column or
    ALTER TABLE ... ADD, with or without the COLUMN keyword) must be
    column-excluded.  Tracked DDL the guard cannot resolve -- f-string or
    variable-built names, pure-interpolation f-strings, quoted or
    schema-qualified identifiers -- fails the guard instead of passing
    silently (pinned synthetically in
    test_walking_guard_fails_closed_on_unresolvable_ddl).  Two escapes
    remain by design: DDL the guard does not track at all (CREATE INDEX,
    ADD CONSTRAINT, table-level RENAME TO), and a mixed f-string whose
    literal parts show no tracked DDL -- an interpolated name inside
    otherwise-untracked literal SQL is invisible to it.
    """
    import re

    from aletheia.schema_migrations import (
        LEGACY_BASELINE_REVISION,
        POST_BASELINE_COLUMNS,
        POST_BASELINE_TABLES,
    )

    versions = sorted((Path(__file__).parents[1] / "migrations" / "versions").glob("*.py"))
    assert versions, "migration versions directory is empty"

    for path in versions:
        source = path.read_text()
        revision = re.search(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)", source, re.M).group(1)
        if revision == LEGACY_BASELINE_REVISION:
            continue
        created, added, unresolvable = _migration_drift(source)
        assert not unresolvable, [f"{path.name}: {entry}" for entry in unresolvable]
        assert created <= POST_BASELINE_TABLES, (
            f"{path.name} creates tables missing from POST_BASELINE_TABLES: "
            f"{sorted(created - POST_BASELINE_TABLES)}"
        )
        uncovered = {
            (table, column) for table, column in added if table not in POST_BASELINE_TABLES
        } - set(POST_BASELINE_COLUMNS)
        assert not uncovered, (
            f"{path.name} adds columns to compared tables without a "
            f"POST_BASELINE_COLUMNS entry: {sorted(uncovered)}"
        )


def test_schema_diffs_excludes_indexes_over_excluded_columns():
    """The index channel of the parity diff: an ORM index declared over a
    post-baseline column rides with that column's exclusion.  Without the
    index branch, adopt_existing_baseline has refused every real legacy
    database since the first indexed post-baseline column (0010)."""
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

    from aletheia.schema_migrations import schema_diffs

    metadata = MetaData()
    Table(
        "zz_baseline_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("probe_extra", String(64), index=True),
    )
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE zz_baseline_probe (id INTEGER NOT NULL PRIMARY KEY)"))
        connection.commit()
        excluded = schema_diffs(
            connection,
            exclude_columns=frozenset({("zz_baseline_probe", "probe_extra")}),
            metadata=metadata,
        )
        kept = schema_diffs(connection, exclude_columns=frozenset(), metadata=metadata)
    assert excluded == []
    assert kept  # the same index still diffs without the column exclusion

    # Round 2: a same-named database index with a different shape must keep
    # its diff even though the metadata side is all-excluded -- alembic's
    # changed-index path passes only the metadata side to include_object.
    drifted = MetaData()
    Table(
        "zz_baseline_probe",
        drifted,
        Column("id", Integer, primary_key=True),
        Column("probe_extra", String(64), index=True),
    )
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE zz_baseline_probe (id INTEGER NOT NULL PRIMARY KEY)"))
        connection.execute(
            text("CREATE INDEX ix_zz_baseline_probe_probe_extra ON zz_baseline_probe (id)")
        )
        connection.commit()
        changed = schema_diffs(
            connection,
            exclude_columns=frozenset({("zz_baseline_probe", "probe_extra")}),
            metadata=drifted,
        )
    assert any(diff[0] in {"add_index", "remove_index"} for diff in changed)


def test_index_exclusion_requires_resolvable_shape_and_matching_database_index():
    """Round-2 pins for the index ride-along decision: expression terms,
    compared columns, and a same-named differently-shaped database index
    all keep the diff -- adoption must refuse, not stamp over drift."""
    from sqlalchemy import Column, Index, Integer, MetaData, String, Table, text

    from aletheia.schema_migrations import _index_rides_excluded_columns

    metadata = MetaData()
    probe = Table(
        "zz_baseline_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("probe_extra", String(64)),
    )
    excluded = frozenset({("zz_baseline_probe", "probe_extra")})

    assert _index_rides_excluded_columns(Index("ix_plain", probe.c.probe_extra), None, excluded)
    assert not _index_rides_excluded_columns(
        Index("ix_mixed", probe.c.probe_extra, probe.c.id), None, excluded
    )
    # Index.columns drops expression terms: the shape is unresolvable.
    assert not _index_rides_excluded_columns(
        Index("ix_expression", probe.c.probe_extra, text("lower(id)")), None, excluded
    )

    database_side = MetaData()
    database_probe = Table(
        "zz_baseline_probe",
        database_side,
        Column("id", Integer, primary_key=True),
        Column("probe_extra", String(64)),
    )
    drifted = Index("ix_db_drifted", database_probe.c.id)
    assert not _index_rides_excluded_columns(
        Index("ix_db_drifted", probe.c.probe_extra), drifted, excluded
    )
    matching = Index("ix_db_matching", database_probe.c.probe_extra)
    assert _index_rides_excluded_columns(
        Index("ix_db_matching", probe.c.probe_extra), matching, excluded
    )
    # Round 3: the comparators diff the unique flag and db-side expression
    # terms too, so identical column names alone must not ride.
    assert not _index_rides_excluded_columns(
        Index("ix_db_unique", probe.c.probe_extra),
        Index("ix_db_unique", database_probe.c.probe_extra, unique=True),
        excluded,
    )
    assert _index_rides_excluded_columns(
        Index("ix_db_unique_match", probe.c.probe_extra, unique=True),
        Index("ix_db_unique_match", database_probe.c.probe_extra, unique=True),
        excluded,
    )
    db_expression = Index("ix_db_expression", database_probe.c.probe_extra)
    db_expression.expressions = [
        database_probe.c.probe_extra,
        text("lower(id)"),
    ]
    assert not _index_rides_excluded_columns(
        Index("ix_db_expression", probe.c.probe_extra), db_expression, excluded
    )


def test_walking_guard_fails_closed_on_unresolvable_ddl():
    """Round-2/3 pins for the drift-guard extraction: tracked DDL whose names
    the guard cannot resolve fails it instead of passing silently, and
    consecutive semicolon-free op.execute statements attribute their ADD
    COLUMNs to their own tables (0035's chunk-merge failure mode)."""
    source = (
        "def upgrade():\n"
        "    op.execute(f'CREATE TABLE {name} (id int)')\n"
        "    op.execute('CREATE TABLE \"Quoted\" (id int)')\n"
        "    op.execute('CREATE TABLE public.qualified (id int)')\n"
        "    op.execute(f'ALTER TABLE {t} ADD COLUMN c int')\n"
        '    op.execute(\'ALTER TABLE "q" ADD COLUMN "c" int\')\n'
        "    op.execute('ALTER TABLE events ADD pg_short_form text')\n"
        "    op.execute('ALTER TABLE execution_attempts ADD COLUMN a text')\n"
        "    op.execute('ALTER TABLE execution_resource_leases ADD COLUMN a text')\n"
        "    op.execute('ALTER TABLE public.leases ADD COLUMN a text')\n"
        "    op.execute(f'{ddl}')\n"
        "    op.add_column('events', column=sa.Column('kw_form', sa.Text()))\n"
    )
    created, added, unresolvable = _migration_drift(source)
    assert created == set()
    assert added == {
        ("events", "pg_short_form"),  # PostgreSQL ADD without the COLUMN keyword
        ("execution_attempts", "a"),
        ("execution_resource_leases", "a"),  # own table, not the chunk's first
        ("events", "kw_form"),  # keyword-form op.add_column resolves
    }
    assert sorted(unresolvable) == [
        "ALTER TABLE ADD statement",  # quoted table and column names
        "ALTER TABLE ADD statement",  # schema-qualified table
        "CREATE TABLE statement",  # quoted identifier
        "CREATE TABLE statement",  # schema-qualified
        "op.execute f-string DDL",  # f-string CREATE TABLE
        "op.execute f-string DDL",  # f-string ALTER TABLE ADD COLUMN
        "op.execute f-string argument",  # pure interpolation hides everything
    ]


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
