"""Establish the audited Aletheia schema baseline.

Revision ID: 20260813_0001
Revises: None
Create Date: 2026-08-13

This revision executes the checked-in frozen SQL snapshot. Future revisions must use explicit
Alembic operations; this file must never import mutable ORM metadata.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision: str = "20260813_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASELINE_SQL = Path(__file__).with_name("20260813_0001_schema_baseline.sql")


def upgrade() -> None:
    sql = BASELINE_SQL.read_text(encoding="utf-8")
    connection = op.get_bind()
    for statement in sql.split(";"):
        if statement.strip():
            connection.execute(text(statement))


def downgrade() -> None:
    # Destructive baseline downgrade is deliberately unsupported. Production evidence tables must
    # be restored from a verified backup rather than bulk-dropped by an application command.
    raise RuntimeError(
        "baseline downgrade is irreversible; restore a verified database backup instead"
    )
