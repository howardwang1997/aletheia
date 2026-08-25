"""Add immutable Run Manifest v1 ledger records.

Revision ID: 20260813_0002
Revises: 20260813_0001
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0002"
down_revision: str | None = "20260813_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_manifests",
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("parent_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "frozen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("manifest_sha256"),
        sa.UniqueConstraint("run_id", name="uq_run_manifest_run"),
    )
    op.create_index(
        op.f("ix_run_manifests_parent_manifest_sha256"),
        "run_manifests",
        ["parent_manifest_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_run_manifests_run_id"), "run_manifests", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_run_manifests_run_id"), table_name="run_manifests")
    op.drop_index(
        op.f("ix_run_manifests_parent_manifest_sha256"), table_name="run_manifests"
    )
    op.drop_table("run_manifests")
