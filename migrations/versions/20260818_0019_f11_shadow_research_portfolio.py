"""Add the shadow-only research portfolio ledger.

Revision ID: 20260818_0019
Revises: 20260817_0018
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0019"
down_revision: str | None = "20260817_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_portfolio_slates",
        sa.Column("slate_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("memory_context_receipt_id", sa.String(length=96), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposal_sha256", sa.String(length=64), nullable=False),
        sa.Column("assessment_batch_sha256", sa.String(length=64), nullable=False),
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "spec_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("graph_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "graph_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("budget_state_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "budget_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["scientific_commands.command_id"],
        ),
        sa.ForeignKeyConstraint(
            ["memory_context_receipt_id"],
            ["research_memory_context_receipts.context_receipt_id"],
        ),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("slate_id"),
        sa.UniqueConstraint("command_id", name="uq_research_portfolio_slates_command"),
    )
    for column in (
        "assessment_batch_sha256",
        "budget_state_sha256",
        "command_id",
        "created_at",
        "created_by",
        "graph_sha256",
        "memory_context_receipt_id",
        "policy_sha256",
        "proposal_sha256",
        "quest_id",
        "spec_sha256",
    ):
        op.create_index(
            op.f(f"ix_research_portfolio_slates_{column}"),
            "research_portfolio_slates",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_portfolio_slates_quest_created",
        "research_portfolio_slates",
        ["quest_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "research_portfolio_candidates",
        sa.Column("slate_id", sa.String(length=96), nullable=False),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("program_id", sa.String(length=96), nullable=False),
        sa.Column("family_id", sa.String(length=96), nullable=True),
        sa.Column("action_type", sa.String(length=48), nullable=False),
        sa.Column("target_node_id", sa.String(length=96), nullable=False),
        sa.Column("action_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "action_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("assessment_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "assessment_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_type IN ('advance_campaign','discriminating_experiment','replication',"
            "'mechanism_test','acquire_data','repair_capability','start_campaign',"
            "'pause_program','stop_program')",
            name="ck_research_portfolio_candidates_action_type",
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["research_scientific_families.family_id"],
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["research_graph_nodes.node_id"],
        ),
        sa.ForeignKeyConstraint(
            ["slate_id"],
            ["research_portfolio_slates.slate_id"],
            name="fk_research_portfolio_candidates_slate",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["research_graph_nodes.node_id"],
        ),
        sa.PrimaryKeyConstraint("slate_id", "candidate_id"),
        sa.UniqueConstraint(
            "slate_id",
            "action_sha256",
            name="uq_research_portfolio_candidates_slate_action",
        ),
    )
    for column in (
        "action_sha256",
        "action_type",
        "assessment_sha256",
        "family_id",
        "program_id",
        "target_node_id",
    ):
        op.create_index(
            op.f(f"ix_research_portfolio_candidates_{column}"),
            "research_portfolio_candidates",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_portfolio_candidates_program",
        "research_portfolio_candidates",
        ["program_id", "slate_id"],
        unique=False,
    )

    op.create_table(
        "research_portfolio_human_plans",
        sa.Column("human_plan_id", sa.String(length=96), nullable=False),
        sa.Column("slate_id", sa.String(length=96), nullable=False),
        sa.Column("plan_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "plan_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["scientific_commands.command_id"],
        ),
        sa.ForeignKeyConstraint(
            ["slate_id"],
            ["research_portfolio_slates.slate_id"],
        ),
        sa.PrimaryKeyConstraint("human_plan_id"),
        sa.UniqueConstraint("command_id", name="uq_research_portfolio_human_plans_command"),
        sa.UniqueConstraint("slate_id", name="uq_research_portfolio_human_plans_slate"),
    )
    for column in ("command_id", "created_at", "created_by", "plan_sha256", "slate_id"):
        op.create_index(
            op.f(f"ix_research_portfolio_human_plans_{column}"),
            "research_portfolio_human_plans",
            [column],
            unique=False,
        )

    op.create_table(
        "research_portfolio_epochs",
        sa.Column("epoch_id", sa.String(length=96), nullable=False),
        sa.Column("slate_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("human_plan_id", sa.String(length=96), nullable=False),
        sa.Column("score_count", sa.Integer(), nullable=False),
        sa.Column("decision_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "decision_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("comparison_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "comparison_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("epoch_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "shadow_only",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "actions_enqueued",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "score_count >= 2",
            name="ck_research_portfolio_epochs_score_count",
        ),
        sa.CheckConstraint(
            "shadow_only IS TRUE AND actions_enqueued IS FALSE",
            name="ck_research_portfolio_epochs_shadow_only",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["scientific_commands.command_id"],
        ),
        sa.ForeignKeyConstraint(
            ["human_plan_id"],
            ["research_portfolio_human_plans.human_plan_id"],
        ),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.ForeignKeyConstraint(
            ["slate_id"],
            ["research_portfolio_slates.slate_id"],
        ),
        sa.PrimaryKeyConstraint("epoch_id"),
        sa.UniqueConstraint("command_id", name="uq_research_portfolio_epochs_command"),
        sa.UniqueConstraint("human_plan_id", name="uq_research_portfolio_epochs_human_plan"),
        sa.UniqueConstraint("slate_id", name="uq_research_portfolio_epochs_slate"),
    )
    for column in (
        "command_id",
        "comparison_sha256",
        "created_at",
        "created_by",
        "decision_sha256",
        "epoch_sha256",
        "evaluated_at",
        "human_plan_id",
        "quest_id",
        "slate_id",
    ):
        op.create_index(
            op.f(f"ix_research_portfolio_epochs_{column}"),
            "research_portfolio_epochs",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_portfolio_epochs_quest_evaluated",
        "research_portfolio_epochs",
        ["quest_id", "evaluated_at"],
        unique=False,
    )

    op.create_table(
        "research_portfolio_scores",
        sa.Column("epoch_id", sa.String(length=96), nullable=False),
        sa.Column("slate_id", sa.String(length=96), nullable=False),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("score_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "score_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("feasible", sa.Boolean(), nullable=False),
        sa.Column("base_utility_microscore", sa.BigInteger(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.CheckConstraint("rank >= 1", name="ck_research_portfolio_scores_rank"),
        sa.ForeignKeyConstraint(
            ["epoch_id"],
            ["research_portfolio_epochs.epoch_id"],
        ),
        sa.ForeignKeyConstraint(
            ["slate_id", "candidate_id"],
            [
                "research_portfolio_candidates.slate_id",
                "research_portfolio_candidates.candidate_id",
            ],
            name="fk_research_portfolio_scores_candidate",
        ),
        sa.PrimaryKeyConstraint("epoch_id", "candidate_id"),
        sa.UniqueConstraint(
            "epoch_id",
            "rank",
            name="uq_research_portfolio_scores_epoch_rank",
        ),
    )
    for column in ("feasible", "score_sha256", "selected"):
        op.create_index(
            op.f(f"ix_research_portfolio_scores_{column}"),
            "research_portfolio_scores",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_portfolio_scores_epoch_selected",
        "research_portfolio_scores",
        ["epoch_id", "selected"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_portfolio_scores_epoch_selected",
        table_name="research_portfolio_scores",
    )
    for column in reversed(("feasible", "score_sha256", "selected")):
        op.drop_index(
            op.f(f"ix_research_portfolio_scores_{column}"),
            table_name="research_portfolio_scores",
        )
    op.drop_table("research_portfolio_scores")

    op.drop_index(
        "ix_research_portfolio_epochs_quest_evaluated",
        table_name="research_portfolio_epochs",
    )
    for column in reversed(
        (
            "command_id",
            "comparison_sha256",
            "created_at",
            "created_by",
            "decision_sha256",
            "epoch_sha256",
            "evaluated_at",
            "human_plan_id",
            "quest_id",
            "slate_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_portfolio_epochs_{column}"),
            table_name="research_portfolio_epochs",
        )
    op.drop_table("research_portfolio_epochs")

    for column in reversed(("command_id", "created_at", "created_by", "plan_sha256", "slate_id")):
        op.drop_index(
            op.f(f"ix_research_portfolio_human_plans_{column}"),
            table_name="research_portfolio_human_plans",
        )
    op.drop_table("research_portfolio_human_plans")

    op.drop_index(
        "ix_research_portfolio_candidates_program",
        table_name="research_portfolio_candidates",
    )
    for column in reversed(
        (
            "action_sha256",
            "action_type",
            "assessment_sha256",
            "family_id",
            "program_id",
            "target_node_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_portfolio_candidates_{column}"),
            table_name="research_portfolio_candidates",
        )
    op.drop_table("research_portfolio_candidates")

    op.drop_index(
        "ix_research_portfolio_slates_quest_created",
        table_name="research_portfolio_slates",
    )
    for column in reversed(
        (
            "assessment_batch_sha256",
            "budget_state_sha256",
            "command_id",
            "created_at",
            "created_by",
            "graph_sha256",
            "memory_context_receipt_id",
            "policy_sha256",
            "proposal_sha256",
            "quest_id",
            "spec_sha256",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_portfolio_slates_{column}"),
            table_name="research_portfolio_slates",
        )
    op.drop_table("research_portfolio_slates")
