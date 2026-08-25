"""Add immutable F9 competing-world-model persistence and K2 compatibility view.

Revision ID: 20260815_0004
Revises: 20260814_0003
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0004"
down_revision: str | None = "20260814_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_TABLES = (
    "epistemic_research_questions",
    "epistemic_hypothesis_versions",
    "epistemic_assumptions",
    "epistemic_predictions",
    "epistemic_belief_states",
    "epistemic_belief_state_members",
    "epistemic_world_model_snapshots",
)


def upgrade() -> None:
    op.create_table(
        "epistemic_research_questions",
        sa.Column("question_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=35), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_question_sha256", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_epistemic_question_version_positive"),
        sa.ForeignKeyConstraint(
            ["parent_question_sha256"], ["epistemic_research_questions.question_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("question_sha256"),
        sa.UniqueConstraint("question_id", "version", name="uq_epistemic_question_version"),
    )
    for column in ("parent_question_sha256", "question_id", "run_id"):
        op.create_index(
            op.f(f"ix_epistemic_research_questions_{column}"),
            "epistemic_research_questions",
            [column],
            unique=False,
        )

    op.create_table(
        "epistemic_hypothesis_versions",
        sa.Column("hypothesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=35), nullable=False),
        sa.Column("question_sha256", sa.String(length=64), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_hypothesis_sha256", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("lifecycle", sa.String(length=24), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_epistemic_hypothesis_version_positive"),
        sa.ForeignKeyConstraint(
            ["parent_hypothesis_sha256"],
            ["epistemic_hypothesis_versions.hypothesis_sha256"],
        ),
        sa.ForeignKeyConstraint(
            ["question_sha256"], ["epistemic_research_questions.question_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("hypothesis_sha256"),
        sa.UniqueConstraint(
            "hypothesis_id", "version", name="uq_epistemic_hypothesis_version"
        ),
    )
    for column in (
        "hypothesis_id",
        "parent_hypothesis_sha256",
        "question_id",
        "question_sha256",
        "run_id",
    ):
        op.create_index(
            op.f(f"ix_epistemic_hypothesis_versions_{column}"),
            "epistemic_hypothesis_versions",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_epistemic_hypothesis_question_role",
        "epistemic_hypothesis_versions",
        ["question_id", "role", "lifecycle"],
        unique=False,
    )

    op.create_table(
        "epistemic_assumptions",
        sa.Column("assumption_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("assumption_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_assumption_sha256", sa.String(length=64), nullable=True),
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("hypothesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=24), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_epistemic_assumption_version_positive"),
        sa.ForeignKeyConstraint(
            ["hypothesis_sha256"], ["epistemic_hypothesis_versions.hypothesis_sha256"]
        ),
        sa.ForeignKeyConstraint(
            ["parent_assumption_sha256"], ["epistemic_assumptions.assumption_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("assumption_sha256"),
        sa.UniqueConstraint(
            "assumption_id", "version", name="uq_epistemic_assumption_version"
        ),
    )
    for column in (
        "assumption_id",
        "hypothesis_id",
        "hypothesis_sha256",
        "parent_assumption_sha256",
        "run_id",
    ):
        op.create_index(
            op.f(f"ix_epistemic_assumptions_{column}"),
            "epistemic_assumptions",
            [column],
            unique=False,
        )

    op.create_table(
        "epistemic_predictions",
        sa.Column("prediction_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("prediction_id", sa.String(length=37), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_prediction_sha256", sa.String(length=64), nullable=True),
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("hypothesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("observable_id", sa.String(length=512), nullable=False),
        sa.Column("direction", sa.String(length=24), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_epistemic_prediction_version_positive"),
        sa.ForeignKeyConstraint(
            ["hypothesis_sha256"], ["epistemic_hypothesis_versions.hypothesis_sha256"]
        ),
        sa.ForeignKeyConstraint(
            ["parent_prediction_sha256"], ["epistemic_predictions.prediction_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("prediction_sha256"),
        sa.UniqueConstraint(
            "prediction_id", "version", name="uq_epistemic_prediction_version"
        ),
    )
    for column in (
        "hypothesis_id",
        "hypothesis_sha256",
        "observable_id",
        "parent_prediction_sha256",
        "prediction_id",
        "run_id",
    ):
        op.create_index(
            op.f(f"ix_epistemic_predictions_{column}"),
            "epistemic_predictions",
            [column],
            unique=False,
        )

    op.create_table(
        "epistemic_belief_states",
        sa.Column("belief_state_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("belief_lineage_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_belief_state_sha256", sa.String(length=64), nullable=True),
        sa.Column("question_id", sa.String(length=35), nullable=False),
        sa.Column("question_sha256", sa.String(length=64), nullable=False),
        sa.Column("update_kind", sa.String(length=32), nullable=False),
        sa.Column("source_observation_receipt_sha256", sa.String(length=64), nullable=True),
        sa.Column("likelihood_model_sha256", sa.String(length=64), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_epistemic_belief_version_positive"),
        sa.ForeignKeyConstraint(
            ["parent_belief_state_sha256"],
            ["epistemic_belief_states.belief_state_sha256"],
        ),
        sa.ForeignKeyConstraint(
            ["question_sha256"], ["epistemic_research_questions.question_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("belief_state_sha256"),
        sa.UniqueConstraint(
            "belief_lineage_id", "version", name="uq_epistemic_belief_lineage_version"
        ),
    )
    for column in (
        "belief_lineage_id",
        "parent_belief_state_sha256",
        "question_id",
        "question_sha256",
        "run_id",
        "source_observation_receipt_sha256",
    ):
        op.create_index(
            op.f(f"ix_epistemic_belief_states_{column}"),
            "epistemic_belief_states",
            [column],
            unique=False,
        )

    op.create_table(
        "epistemic_belief_state_members",
        sa.Column("belief_state_sha256", sa.String(length=64), nullable=False),
        sa.Column("hypothesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "probability >= 0.0 AND probability <= 1.0",
            name="ck_epistemic_belief_member_probability",
        ),
        sa.ForeignKeyConstraint(
            ["belief_state_sha256"], ["epistemic_belief_states.belief_state_sha256"]
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_sha256"], ["epistemic_hypothesis_versions.hypothesis_sha256"]
        ),
        sa.PrimaryKeyConstraint("belief_state_sha256", "hypothesis_sha256"),
        sa.UniqueConstraint(
            "belief_state_sha256",
            "hypothesis_id",
            name="uq_epistemic_belief_member_lineage",
        ),
        sa.UniqueConstraint(
            "belief_state_sha256", "ordinal", name="uq_epistemic_belief_member_order"
        ),
    )
    op.create_index(
        op.f("ix_epistemic_belief_state_members_hypothesis_id"),
        "epistemic_belief_state_members",
        ["hypothesis_id"],
        unique=False,
    )

    op.create_table(
        "epistemic_world_model_snapshots",
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=35), nullable=False),
        sa.Column("question_sha256", sa.String(length=64), nullable=False),
        sa.Column("belief_state_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["belief_state_sha256"], ["epistemic_belief_states.belief_state_sha256"]
        ),
        sa.ForeignKeyConstraint(
            ["question_sha256"], ["epistemic_research_questions.question_sha256"]
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("snapshot_sha256"),
        sa.UniqueConstraint(
            "question_sha256",
            "belief_state_sha256",
            name="uq_epistemic_world_model_question_belief",
        ),
    )
    for column in ("belief_state_sha256", "question_id", "question_sha256", "run_id"):
        op.create_index(
            op.f(f"ix_epistemic_world_model_snapshots_{column}"),
            "epistemic_world_model_snapshots",
            [column],
            unique=False,
        )

    op.execute(
        """
        CREATE VIEW k2_belief_state_compat AS
        SELECT
            id AS legacy_belief_state_id,
            ('k2::' || run_id || '::' || question_key) AS belief_lineage_id,
            run_id,
            question_key,
            alpha,
            beta,
            CASE
                WHEN alpha + beta > 0.0 THEN alpha / (alpha + beta)
                ELSE 0.5
            END AS probability_holds,
            n_updates,
            updated_at,
            'legacy_k2_beta_bernoulli'::varchar(32) AS representation
        FROM belief_states
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_reject_epistemic_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'immutable F9 epistemic row cannot be updated or deleted'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_k2_belief_state_compat_read_only
        INSTEAD OF INSERT OR UPDATE OR DELETE ON k2_belief_state_compat
        FOR EACH ROW EXECUTE FUNCTION aletheia_reject_epistemic_mutation()
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION aletheia_reject_epistemic_mutation()
            """
        )


def downgrade() -> None:
    op.execute("DROP VIEW k2_belief_state_compat")
    for table_name in reversed(IMMUTABLE_TABLES):
        op.drop_table(table_name)
    op.execute("DROP FUNCTION aletheia_reject_epistemic_mutation()")
