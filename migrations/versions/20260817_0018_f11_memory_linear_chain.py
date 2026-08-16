"""Enforce one linear compaction chain per scope/task.

Revision ID: 20260817_0018
Revises: 20260817_0017
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260817_0018"
down_revision: str | None = "20260817_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_research_memory_compactions_root",
        "research_memory_compactions",
        ["scope_node_id", "task_key"],
        unique=True,
        postgresql_where=sa.text("parent_compaction_id IS NULL"),
    )
    op.create_index(
        "uq_research_memory_compactions_parent",
        "research_memory_compactions",
        ["parent_compaction_id"],
        unique=True,
        postgresql_where=sa.text("parent_compaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_research_memory_compactions_parent",
        table_name="research_memory_compactions",
        postgresql_where=sa.text("parent_compaction_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_research_memory_compactions_root",
        table_name="research_memory_compactions",
        postgresql_where=sa.text("parent_compaction_id IS NULL"),
    )
