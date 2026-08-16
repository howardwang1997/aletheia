"""Safe operational helpers around the Alembic schema baseline."""

from __future__ import annotations

from dataclasses import dataclass

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

import aletheia.memory.ledger  # noqa: F401  (register every ORM table)
import aletheia.knowledge.persistence  # noqa: F401  (register F8 immutable tables)
import aletheia.epistemics.persistence  # noqa: F401  (register F9 world-model tables)
import aletheia.jobs.persistence  # noqa: F401  (register F11 durable queue tables)
import aletheia.programs.persistence  # noqa: F401  (register F11 scientific graph tables)
from aletheia.db import Base, SchemaCompatibilityError, alembic_config, engine

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
        "durable_tasks",
        "durable_task_dependencies",
        "durable_task_attempts",
        "durable_queue_audits",
        "scientific_commands",
        "one_time_external_actions",
        "external_action_receipts",
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
    }
)


@dataclass(frozen=True)
class BaselineAdoptionReceipt:
    revision: str
    table_count: int
    schema_diff_count: int


def schema_diffs(
    connection: Connection,
    *,
    exclude_tables: frozenset[str] = frozenset(),
    exclude_columns: frozenset[tuple[str, str]] = frozenset(),
    exclude_constraints: frozenset[str] = frozenset(),
) -> list[object]:
    """Return Alembic's structural diff between the connected schema and ORM metadata."""

    def include_object(
        object_, name: str | None, type_: str, _reflected: bool, _compare_to
    ) -> bool:
        if type_ == "table" and name in exclude_tables:
            return False
        if type_ == "column":
            table_name = getattr(getattr(object_, "table", None), "name", None)
            if (table_name, name) in exclude_columns:
                return False
        if type_ in {"unique_constraint", "check_constraint"} and name in exclude_constraints:
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
    return list(compare_metadata(context, Base.metadata))


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
