"""Safe operational helpers around the Alembic schema baseline."""

from __future__ import annotations

from dataclasses import dataclass

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, MetaData, inspect
from sqlalchemy.engine import Connection

import aletheia.memory.ledger  # noqa: F401  (register every ORM table)
import aletheia.knowledge.persistence  # noqa: F401  (register F8 immutable tables)
import aletheia.epistemics.persistence  # noqa: F401  (register F9 world-model tables)
import aletheia.execution.persistence  # noqa: F401  (register local execution tables)
import aletheia.jobs.persistence  # noqa: F401  (register F11 durable queue tables)
import aletheia.observations.persistence  # noqa: F401  (register PR-5 bridge tables)
import aletheia.programs.persistence  # noqa: F401  (register F11 scientific graph tables)
import aletheia.research_store.persistence  # noqa: F401  (register kernel-store tables)
from aletheia.db import (
    Base,
    SchemaCompatibilityError,
    SchemaStatus,
    alembic_config,
    engine,
    require_schema_current,
)

LEGACY_BASELINE_REVISION = "20260813_0001"
POST_BASELINE_TABLES = frozenset(
    {
        "run_manifests",
        "knowledge_access_policies",
        "knowledge_corpus_sources",
        "knowledge_paper_snapshots",
        "knowledge_paper_text_identities",
        "knowledge_source_spans",
        "knowledge_publication_updates",
        "knowledge_content_access_grants",
        "knowledge_provider_receipts",
        "knowledge_corpus_snapshots",
        "knowledge_ingestion_bundles",
        "knowledge_corpus_source_members",
        "knowledge_corpus_paper_members",
        "knowledge_corpus_span_members",
        "knowledge_corpus_update_members",
        "knowledge_bundle_grant_members",
        "knowledge_bundle_receipt_members",
        "epistemic_research_questions",
        "epistemic_hypothesis_versions",
        "epistemic_assumptions",
        "epistemic_predictions",
        "epistemic_belief_states",
        "epistemic_belief_state_members",
        "epistemic_world_model_snapshots",
        "epistemic_world_model_transitions",
        "execution_nodes",
        "execution_inventory_attestations",
        "execution_inventory_devices",
        "execution_device_heads",
        "execution_qualification_admissions",
        "execution_budget_authorizations",
        "execution_budget_heads",
        "execution_heads",
        "execution_attempts",
        "execution_assignment_envelopes",
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
        "execution_attempt_adoptions",
        "execution_external_runtime_preparations",
        "execution_external_launch_authorizations",
        "execution_external_runtime_launch_receipts",
        "execution_external_termination_challenges",
        "execution_external_runtime_termination_acceptances",
        "execution_external_qualification_terminal_acceptances",
        "execution_external_qualification_deadline_expirations",
        "execution_resource_leases",
        "execution_device_leases",
        "execution_budget_reservations",
        "execution_budget_events",
        "execution_terminal_receipts",
        "execution_outbox",
        "durable_tasks",
        "durable_task_dependencies",
        "durable_task_attempts",
        "durable_queue_audits",
        "scientific_commands",
        "one_time_external_actions",
        "external_action_receipts",
        "fault_injection_campaigns",
        "research_graph_nodes",
        "research_graph_transitions",
        "research_scientific_families",
        "research_campaign_families",
        "research_graph_dependencies",
        "research_program_questions",
        "research_campaign_runs",
        "research_campaign_experiments",
        "research_data_role_allocations",
        "research_budget_allocations",
        "research_memory_facts",
        "research_memory_task_bindings",
        "research_memory_compactions",
        "research_memory_compaction_members",
        "research_memory_context_receipts",
        "research_portfolio_slates",
        "research_portfolio_candidates",
        "research_portfolio_human_plans",
        "research_portfolio_epochs",
        "research_portfolio_scores",
        "research_endurance_gates",
        "research_endurance_checkpoints",
        "research_endurance_reports",
        "research_quest_authorities",
        "research_quest_streams",
        "research_kernel_objects",
        "research_kernel_command_receipts",
        "research_kernel_events",
        "research_kernel_snapshots",
        "research_kernel_outbox",
        "research_controller_registrations",
        "research_controller_deliveries",
        "research_controller_delivery_attempts",
        "research_controller_delivery_resolutions",
        "research_protocol_compilations",
        "research_scientific_execution_authorizations",
        "research_observation_issuance_challenges",
        "research_observation_validation_receipts",
        "research_observation_admissions",
        "research_continuation_receipts",
    }
)
POST_BASELINE_COLUMNS = frozenset(
    {
        ("events", "event_key"),
        ("events", "event_sha256"),
        ("artifacts", "scientific_command_id"),
        ("artifacts", "commit_ordinal"),
        ("decisions", "scientific_command_id"),
        ("campaign_split_ledgers", "final_action_id"),
        ("campaign_split_ledgers", "final_action_receipt_sha256"),
        ("external_validation_ledgers", "action_id"),
        ("external_validation_ledgers", "action_receipt_sha256"),
        ("budget_events", "research_budget_allocation_id"),
        ("hypothesis_attempts", "research_family_id"),
    }
)
POST_BASELINE_CONSTRAINTS = frozenset(
    {
        "ck_events_key_has_sha256",
        "uq_events_event_key",
        "ck_artifacts_scientific_commit_pair",
        "uq_artifacts_scientific_commit_ordinal",
        "uq_decisions_scientific_command_id",
        "uq_campaign_split_ledgers_final_action_id",
        "uq_external_validation_ledgers_action_id",
        "uq_rke_scoped_typed_event",
        "uq_rko_exact_controller_source",
        "uq_exec_qto_exact_controller_source",
    }
)


@dataclass(frozen=True)
class BaselineAdoptionReceipt:
    revision: str
    table_count: int
    schema_diff_count: int


def _plain_column_names(index: object) -> list[str] | None:
    """Ordered column names when every index term is a plain column, else None.

    Sort-order and function wrappers (``col.desc()``, ``func.lower(col)``)
    keep the column in ``Index.columns`` while changing the compiled text
    the PostgreSQL comparator diffs, so agreement between two shapes is
    provable through names only when every term is a plain column.
    """
    terms = list(getattr(index, "expressions", ()) or ())
    if not terms or not all(isinstance(term, Column) for term in terms):
        return None
    names = [getattr(term, "name", None) for term in terms]
    if None in names or len(set(names)) != len(names):
        return None
    return names


def _index_rides_excluded_columns(
    index: object, compare_to: object, exclude_columns: frozenset[tuple[str, str]]
) -> bool:
    """True only when an index diff is post-baseline drift the column exclusion covers.

    An ORM index declared over post-baseline columns rides along with those
    columns: the legacy database has neither.  Without a same-named database
    index (alembic's added path) the index is wholesale post-baseline drift
    and rides whenever ``Index.columns`` resolves fully to excluded plain
    columns -- ``Index.columns`` silently drops ``text()`` expression terms,
    so those keep their diff.  With a same-named database index (the changed
    path, where alembic passes only the metadata side) the shapes must agree
    on every axis the comparators diff: ordered column names, the unique
    flag, per-expression compiled text, and set dialect options such as
    ``postgresql_nulls_not_distinct``.  Name-based agreement cannot prove
    compiled text or options, so any wrapper term or set option on either
    side keeps the diff and adoption refuses.
    """
    table_name = getattr(getattr(index, "table", None), "name", None)
    columns = list(getattr(index, "columns", ()) or ())
    names = [getattr(column, "name", None) for column in columns]
    if not columns or None in names or len(set(names)) != len(names):
        return False
    expressions = getattr(index, "expressions", None)
    if expressions is not None and len(expressions) != len(columns):
        return False
    if not all((table_name, column) in exclude_columns for column in names):
        return False
    if compare_to is None:
        return True
    compared = _plain_column_names(compare_to)
    index_options = dict(getattr(index, "dialect_kwargs", None) or {})
    compared_options = dict(getattr(compare_to, "dialect_kwargs", None) or {})
    return (
        compared is not None
        and compared == _plain_column_names(index)
        and bool(getattr(compare_to, "unique", False)) == bool(getattr(index, "unique", False))
        and not any(value is not None for value in index_options.values())
        and not any(value is not None for value in compared_options.values())
    )


def schema_diffs(
    connection: Connection,
    *,
    exclude_tables: frozenset[str] = frozenset(),
    exclude_columns: frozenset[tuple[str, str]] = frozenset(),
    exclude_constraints: frozenset[str] = frozenset(),
    metadata: MetaData | None = None,
) -> list[object]:
    """Return Alembic's structural diff between the connected schema and ORM metadata.

    ``metadata`` defaults to the ORM Base; tests pass a small controlled
    MetaData to exercise the include_object channels directly.
    """

    def include_object(object_, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
        # The name-matched table, column, and constraint exclusions ride
        # the metadata side only -- no database counterpart (compare_to is
        # None, not the reflected object); a database table, column, or
        # constraint wearing an excluded name is drift and keeps its diff,
        # whatever its shape.  The column-driven channels differ: the
        # index branch also rides a changed pair that agrees on every
        # compared axis, and the foreign-key branch never reads
        # compare_to, so a changed pair keeps its reflected remove_fk
        # half.
        if type_ == "table" and name in exclude_tables and compare_to is None and not reflected:
            return False
        if type_ == "column" and compare_to is None and not reflected:
            table_name = getattr(getattr(object_, "table", None), "name", None)
            if (table_name, name) in exclude_columns:
                return False
        if (
            type_ == "index"
            and not reflected
            and _index_rides_excluded_columns(object_, compare_to, exclude_columns)
        ):
            return False
        # A metadata foreign key over post-baseline columns rides with those
        # columns (the legacy database has neither); a database-only foreign
        # key is extra drift and keeps its diff (reflected side).
        if type_ == "foreign_key_constraint" and not reflected:
            table_name = getattr(getattr(object_, "table", None), "name", None)
            constrained = [
                getattr(column, "name", None) for column in getattr(object_, "columns", ()) or ()
            ]
            if (
                constrained
                and None not in constrained
                and all((table_name, column) in exclude_columns for column in constrained)
            ):
                return False
        if (
            type_ in {"unique_constraint", "check_constraint"}
            and name in exclude_constraints
            and compare_to is None
            and not reflected
        ):
            return False
        return True

    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": False,
            "include_object": include_object,
        },
    )
    return list(compare_metadata(context, Base.metadata if metadata is None else metadata))


def require_schema_exact(connection: Connection | None = None) -> SchemaStatus:
    """Fail closed unless both the revision and ORM-managed structure match this build.

    An Alembic stamp is only a claim about migration history. It is not proof that every table,
    column, index, foreign key and ORM-managed constraint exists. Deployment entry points use this
    stronger check so a manually stamped, partially restored or otherwise drifted database cannot
    report ready and then execute against a different authority graph.

    This function intentionally lives at the deployment/migration layer. Keeping the import out of
    :mod:`aletheia.db` prevents protected authority packages from gaining a reverse dependency on
    every persistence module registered for Alembic comparison.
    """

    owned = connection is None
    conn = engine().connect() if owned else connection
    assert conn is not None
    try:
        status = require_schema_current(conn)
        diffs = schema_diffs(conn)
        if not diffs:
            return status
        preview = "; ".join(repr(diff)[:240] for diff in diffs[:5])
        raise SchemaCompatibilityError(
            "database revision is current but its structure differs from this Aletheia build; "
            "restore a verified backup or rebuild and migrate a fresh database "
            f"({len(diffs)} differences: {preview})"
        )
    finally:
        if owned:
            conn.close()


def adopt_existing_baseline() -> BaselineAdoptionReceipt:
    """Stamp a legacy create_all database only after exact structural comparison.

    This never creates, alters, or repairs application tables. An empty database must use
    ``alembic upgrade head`` instead; a partial/changed database requires a reviewed migration.
    """
    with engine().connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" in tables:
            raise SchemaCompatibilityError("database is already managed by Alembic")
        application_tables = tables - {"alembic_version"}
        if not application_tables:
            raise SchemaCompatibilityError(
                "database is empty; use `conda run -n aletheia alembic upgrade head`"
            )
        diffs = schema_diffs(
            connection,
            exclude_tables=POST_BASELINE_TABLES,
            exclude_columns=POST_BASELINE_COLUMNS,
            exclude_constraints=POST_BASELINE_CONSTRAINTS,
        )
        if diffs:
            preview = "; ".join(repr(diff) for diff in diffs[:5])
            raise SchemaCompatibilityError(
                "legacy schema does not exactly match the audited baseline; refusing to stamp "
                f"({len(diffs)} differences: {preview})"
            )

    cfg = alembic_config()
    command.stamp(cfg, LEGACY_BASELINE_REVISION)
    return BaselineAdoptionReceipt(
        revision=LEGACY_BASELINE_REVISION,
        table_count=len(application_tables),
        schema_diff_count=0,
    )
