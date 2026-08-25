"""Add receipt-backed scientific memory compaction tables.

Revision ID: 20260817_0016
Revises: 20260817_0015
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0016"
down_revision: str | None = "20260817_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_memory_facts",
        sa.Column("fact_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("scope_node_id", sa.String(length=96), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_refs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fact_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('goal','hypothesis','assumption','prediction','observation','evidence',"
            "'decision','method','result','negative_result','contradiction','limitation',"
            "'failed_hypothesis','open_question','safety_boundary')",
            name="ck_research_memory_facts_kind",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.ForeignKeyConstraint(["scope_node_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("command_id", name="uq_research_memory_facts_command"),
    )
    for column in (
        "command_id",
        "created_at",
        "created_by",
        "fact_sha256",
        "kind",
        "quest_id",
        "scope_node_id",
    ):
        op.create_index(
            op.f(f"ix_research_memory_facts_{column}"),
            "research_memory_facts",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_memory_facts_scope_kind",
        "research_memory_facts",
        ["scope_node_id", "kind"],
        unique=False,
    )

    op.create_table(
        "research_memory_task_bindings",
        sa.Column("fact_id", sa.String(length=96), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("context_role", sa.String(length=24), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.CheckConstraint(
            "context_role IN ('required','supporting')",
            name="ck_research_memory_task_bindings_role",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(["fact_id"], ["research_memory_facts.fact_id"]),
        sa.PrimaryKeyConstraint("fact_id", "task_key"),
    )
    for column in ("command_id", "context_role", "task_key"):
        op.create_index(
            op.f(f"ix_research_memory_task_bindings_{column}"),
            "research_memory_task_bindings",
            [column],
            unique=False,
        )

    op.create_table(
        "research_memory_compactions",
        sa.Column("compaction_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("scope_node_id", sa.String(length=96), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("parent_compaction_id", sa.String(length=96), nullable=True),
        sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("exact_count", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_sha256", sa.String(length=64), nullable=False),
        sa.Column("producer_provider", sa.String(length=64), nullable=False),
        sa.Column("producer_model", sa.String(length=256), nullable=False),
        sa.Column("producer_prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("producer_draft_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_relative_path", sa.Text(), nullable=False),
        sa.Column("artifact_object_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exact_count >= 0 AND exact_count <= source_count",
            name="ck_research_memory_compactions_exact_count",
        ),
        sa.CheckConstraint("source_count > 0", name="ck_research_memory_compactions_source_count"),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(
            ["parent_compaction_id"], ["research_memory_compactions.compaction_id"]
        ),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.ForeignKeyConstraint(["scope_node_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("compaction_id"),
        sa.UniqueConstraint("command_id", name="uq_research_memory_compactions_command"),
    )
    for column in (
        "artifact_receipt_sha256",
        "artifact_sha256",
        "command_id",
        "created_at",
        "parent_compaction_id",
        "quest_id",
        "scope_node_id",
        "source_manifest_sha256",
        "task_key",
    ):
        op.create_index(
            op.f(f"ix_research_memory_compactions_{column}"),
            "research_memory_compactions",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_memory_compactions_scope_task",
        "research_memory_compactions",
        ["scope_node_id", "task_key", "created_at"],
        unique=False,
    )

    op.create_table(
        "research_memory_compaction_members",
        sa.Column("compaction_id", sa.String(length=96), nullable=False),
        sa.Column("fact_id", sa.String(length=96), nullable=False),
        sa.Column("fact_sha256", sa.String(length=64), nullable=False),
        sa.Column("fact_kind", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "disposition IN ('summary','exact_required','exact_non_droppable')",
            name="ck_research_memory_compaction_members_disposition",
        ),
        sa.ForeignKeyConstraint(["compaction_id"], ["research_memory_compactions.compaction_id"]),
        sa.ForeignKeyConstraint(["fact_id"], ["research_memory_facts.fact_id"]),
        sa.PrimaryKeyConstraint("compaction_id", "fact_id"),
    )
    op.create_index(
        op.f("ix_research_memory_compaction_members_disposition"),
        "research_memory_compaction_members",
        ["disposition"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_memory_compaction_members_fact_id"),
        "research_memory_compaction_members",
        ["fact_id"],
        unique=False,
    )

    op.create_table(
        "research_memory_context_receipts",
        sa.Column("context_receipt_id", sa.String(length=96), nullable=False),
        sa.Column("compaction_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("scope_node_id", sa.String(length=96), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("consumer_provider", sa.String(length=64), nullable=False),
        sa.Column("consumer_model", sa.String(length=256), nullable=False),
        sa.Column("max_chars", sa.Integer(), nullable=False),
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("selected_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("max_chars >= 512", name="ck_research_memory_context_max_chars"),
        sa.CheckConstraint(
            "prompt_chars > 0 AND prompt_chars <= max_chars",
            name="ck_research_memory_context_prompt_chars",
        ),
        sa.ForeignKeyConstraint(["compaction_id"], ["research_memory_compactions.compaction_id"]),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.ForeignKeyConstraint(["scope_node_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("context_receipt_id"),
        sa.UniqueConstraint("command_id", name="uq_research_memory_context_command"),
    )
    for column in (
        "command_id",
        "compaction_id",
        "context_sha256",
        "created_at",
        "quest_id",
        "scope_node_id",
        "task_key",
    ):
        op.create_index(
            op.f(f"ix_research_memory_context_receipts_{column}"),
            "research_memory_context_receipts",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_memory_context_scope_task",
        "research_memory_context_receipts",
        ["scope_node_id", "task_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_memory_context_scope_task",
        table_name="research_memory_context_receipts",
    )
    for column in reversed(
        (
            "command_id",
            "compaction_id",
            "context_sha256",
            "created_at",
            "quest_id",
            "scope_node_id",
            "task_key",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_memory_context_receipts_{column}"),
            table_name="research_memory_context_receipts",
        )
    op.drop_table("research_memory_context_receipts")
    op.drop_index(
        op.f("ix_research_memory_compaction_members_fact_id"),
        table_name="research_memory_compaction_members",
    )
    op.drop_index(
        op.f("ix_research_memory_compaction_members_disposition"),
        table_name="research_memory_compaction_members",
    )
    op.drop_table("research_memory_compaction_members")
    op.drop_index(
        "ix_research_memory_compactions_scope_task",
        table_name="research_memory_compactions",
    )
    for column in reversed(
        (
            "artifact_receipt_sha256",
            "artifact_sha256",
            "command_id",
            "created_at",
            "parent_compaction_id",
            "quest_id",
            "scope_node_id",
            "source_manifest_sha256",
            "task_key",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_memory_compactions_{column}"),
            table_name="research_memory_compactions",
        )
    op.drop_table("research_memory_compactions")
    for column in reversed(("command_id", "context_role", "task_key")):
        op.drop_index(
            op.f(f"ix_research_memory_task_bindings_{column}"),
            table_name="research_memory_task_bindings",
        )
    op.drop_table("research_memory_task_bindings")
    op.drop_index("ix_research_memory_facts_scope_kind", table_name="research_memory_facts")
    for column in reversed(
        (
            "command_id",
            "created_at",
            "created_by",
            "fact_sha256",
            "kind",
            "quest_id",
            "scope_node_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_memory_facts_{column}"),
            table_name="research_memory_facts",
        )
    op.drop_table("research_memory_facts")
