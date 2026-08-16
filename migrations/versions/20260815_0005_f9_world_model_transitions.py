"""Add atomic F9 world-model transition persistence.

Revision ID: 20260815_0005
Revises: 20260815_0004
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0005"
down_revision: str | None = "20260815_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "epistemic_world_model_transitions",
        sa.Column("transition_sha256", sa.String(length=64), nullable=False),
        sa.Column("transition_id", sa.String(length=192), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=35), nullable=False),
        sa.Column("belief_lineage_id", sa.String(length=36), nullable=False),
        sa.Column("source_update_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("posterior_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("next_round_snapshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("disposition", sa.String(length=48), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["next_round_snapshot_sha256"],
            ["epistemic_world_model_snapshots.snapshot_sha256"],
        ),
        sa.ForeignKeyConstraint(
            ["posterior_snapshot_sha256"],
            ["epistemic_world_model_snapshots.snapshot_sha256"],
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(
            ["source_snapshot_sha256"],
            ["epistemic_world_model_snapshots.snapshot_sha256"],
        ),
        sa.PrimaryKeyConstraint("transition_sha256"),
        sa.UniqueConstraint(
            "source_update_receipt_sha256",
            name="uq_epistemic_world_model_transition_update",
        ),
        sa.UniqueConstraint("transition_id", name="uq_epistemic_world_model_transition_id"),
    )
    for column in (
        "belief_lineage_id",
        "disposition",
        "next_round_snapshot_sha256",
        "posterior_snapshot_sha256",
        "question_id",
        "run_id",
        "source_snapshot_sha256",
        "source_update_receipt_sha256",
        "transition_id",
    ):
        op.create_index(
            op.f(f"ix_epistemic_world_model_transitions_{column}"),
            "epistemic_world_model_transitions",
            [column],
            unique=False,
        )
    op.execute(
        """
        CREATE TRIGGER trg_epistemic_world_model_transitions_immutable
        BEFORE UPDATE OR DELETE ON epistemic_world_model_transitions
        FOR EACH ROW EXECUTE FUNCTION aletheia_reject_epistemic_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("epistemic_world_model_transitions")
