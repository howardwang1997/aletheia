"""Bind exact retry intent and outcome to each durable attempt.

Revision ID: 20260816_0007
Revises: 20260816_0006
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260816_0007"
down_revision: str | None = "20260816_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "durable_task_attempts",
        sa.Column("retry_requested", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "durable_task_attempts",
        sa.Column("retry_scheduled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("durable_task_attempts", "retry_scheduled")
    op.drop_column("durable_task_attempts", "retry_requested")
