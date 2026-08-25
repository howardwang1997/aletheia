"""Prevent duplicate active work in an explicit mutual-exclusion scope.

Revision ID: 20260816_0008
Revises: 20260816_0007
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260816_0008"
down_revision: str | None = "20260816_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "durable_tasks",
        sa.Column("concurrency_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f("ix_durable_tasks_concurrency_key"),
        "durable_tasks",
        ["concurrency_key"],
        unique=False,
    )
    op.create_index(
        "uq_durable_tasks_active_concurrency_key",
        "durable_tasks",
        ["concurrency_key"],
        unique=True,
        postgresql_where=sa.text(
            "concurrency_key IS NOT NULL AND status IN ('blocked','queued','leased','retry_wait')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_durable_tasks_active_concurrency_key", table_name="durable_tasks")
    op.drop_index(op.f("ix_durable_tasks_concurrency_key"), table_name="durable_tasks")
    op.drop_column("durable_tasks", "concurrency_key")
