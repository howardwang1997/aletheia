"""Add transactional scientific commands and one-time external action receipts.

Revision ID: 20260817_0010
Revises: 20260816_0009
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0010"
down_revision: str | None = "20260816_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scientific_commands",
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("command_type", sa.String(length=96), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=192), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("source_event_key", sa.String(length=128), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "input_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("principal", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "event_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("output_event_key", sa.String(length=128), nullable=False),
        sa.Column("output_event_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('applying','committed')",
            name="ck_scientific_commands_status",
        ),
        sa.ForeignKeyConstraint(["output_event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_scientific_commands_idempotency_key",
        ),
        sa.UniqueConstraint(
            "source_event_key",
            name="uq_scientific_commands_source_event_key",
        ),
        sa.UniqueConstraint(
            "output_event_key",
            name="uq_scientific_commands_output_event_key",
        ),
        sa.UniqueConstraint(
            "output_event_id",
            name="uq_scientific_commands_output_event_id",
        ),
    )
    for column in (
        "aggregate_id",
        "aggregate_type",
        "command_type",
        "committed_at",
        "created_at",
        "output_event_id",
        "principal",
        "run_id",
        "source_event_key",
        "status",
    ):
        op.create_index(
            op.f(f"ix_scientific_commands_{column}"),
            "scientific_commands",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_scientific_commands_aggregate",
        "scientific_commands",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )

    op.create_table(
        "one_time_external_actions",
        sa.Column("action_id", sa.String(length=96), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=96), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "request_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("principal", sa.String(length=128), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("claim_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("claim_owner", sa.String(length=128), nullable=False),
        sa.Column("execution_token_sha256", sa.String(length=64), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconcile_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('claimed','reconciliation_required','completed')",
            name="ck_one_time_external_actions_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_one_time_external_actions_state_version",
        ),
        sa.ForeignKeyConstraint(["last_event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint(
            "last_event_id",
            name="uq_one_time_external_actions_last_event",
        ),
        sa.UniqueConstraint(
            "provider_idempotency_key",
            name="uq_one_time_external_actions_provider_key",
        ),
        sa.UniqueConstraint("scope_key", name="uq_one_time_external_actions_scope_key"),
    )
    for column in (
        "action_type",
        "claim_owner",
        "claimed_at",
        "completed_at",
        "created_at",
        "last_event_id",
        "principal",
        "receipt_sha256",
        "reconcile_after",
        "run_id",
        "status",
    ):
        op.create_index(
            op.f(f"ix_one_time_external_actions_{column}"),
            "one_time_external_actions",
            [column],
            unique=False,
        )

    op.create_table(
        "external_action_receipts",
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=96), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("outcome_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "event_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("completion_event_key", sa.String(length=128), nullable=False),
        sa.Column("completion_event_id", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["one_time_external_actions.action_id"],
        ),
        sa.ForeignKeyConstraint(["completion_event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("receipt_sha256"),
        sa.UniqueConstraint("action_id", name="uq_external_action_receipts_action"),
        sa.UniqueConstraint(
            "completion_event_id",
            name="uq_external_action_receipts_event_id",
        ),
        sa.UniqueConstraint(
            "completion_event_key",
            name="uq_external_action_receipts_event_key",
        ),
    )
    for column in ("action_id", "completed_at", "completion_event_id", "outcome_sha256"):
        op.create_index(
            op.f(f"ix_external_action_receipts_{column}"),
            "external_action_receipts",
            [column],
            unique=False,
        )

    op.add_column(
        "artifacts",
        sa.Column("scientific_command_id", sa.String(length=96), nullable=True),
    )
    op.add_column("artifacts", sa.Column("commit_ordinal", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_artifacts_scientific_command_id_scientific_commands",
        "artifacts",
        "scientific_commands",
        ["scientific_command_id"],
        ["command_id"],
    )
    op.create_check_constraint(
        "ck_artifacts_scientific_commit_pair",
        "artifacts",
        "(scientific_command_id IS NULL AND commit_ordinal IS NULL) OR "
        "(scientific_command_id IS NOT NULL AND commit_ordinal IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_artifacts_scientific_commit_ordinal",
        "artifacts",
        ["scientific_command_id", "commit_ordinal"],
    )
    op.create_index(
        op.f("ix_artifacts_scientific_command_id"),
        "artifacts",
        ["scientific_command_id"],
        unique=False,
    )

    op.add_column(
        "decisions",
        sa.Column("scientific_command_id", sa.String(length=96), nullable=True),
    )
    op.create_foreign_key(
        "fk_decisions_scientific_command_id_scientific_commands",
        "decisions",
        "scientific_commands",
        ["scientific_command_id"],
        ["command_id"],
    )
    op.create_unique_constraint(
        "uq_decisions_scientific_command_id",
        "decisions",
        ["scientific_command_id"],
    )
    op.create_index(
        op.f("ix_decisions_scientific_command_id"),
        "decisions",
        ["scientific_command_id"],
        unique=False,
    )

    op.add_column(
        "campaign_split_ledgers",
        sa.Column("final_action_id", sa.String(length=96), nullable=True),
    )
    op.add_column(
        "campaign_split_ledgers",
        sa.Column("final_action_receipt_sha256", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaign_split_ledgers_final_action_id",
        "campaign_split_ledgers",
        "one_time_external_actions",
        ["final_action_id"],
        ["action_id"],
    )
    op.create_unique_constraint(
        "uq_campaign_split_ledgers_final_action_id",
        "campaign_split_ledgers",
        ["final_action_id"],
    )
    op.create_index(
        op.f("ix_campaign_split_ledgers_final_action_id"),
        "campaign_split_ledgers",
        ["final_action_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_split_ledgers_final_action_receipt_sha256"),
        "campaign_split_ledgers",
        ["final_action_receipt_sha256"],
        unique=False,
    )

    op.add_column(
        "external_validation_ledgers",
        sa.Column("action_id", sa.String(length=96), nullable=True),
    )
    op.add_column(
        "external_validation_ledgers",
        sa.Column("action_receipt_sha256", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_external_validation_ledgers_action_id",
        "external_validation_ledgers",
        "one_time_external_actions",
        ["action_id"],
        ["action_id"],
    )
    op.create_unique_constraint(
        "uq_external_validation_ledgers_action_id",
        "external_validation_ledgers",
        ["action_id"],
    )
    op.create_index(
        op.f("ix_external_validation_ledgers_action_id"),
        "external_validation_ledgers",
        ["action_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_validation_ledgers_action_receipt_sha256"),
        "external_validation_ledgers",
        ["action_receipt_sha256"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_reject_immutable_f11_row()
        RETURNS trigger AS $$
        BEGIN
          IF TG_TABLE_NAME = 'scientific_commands' THEN
            IF OLD.status <> 'committed' THEN
              IF TG_OP = 'DELETE' THEN
                RETURN OLD;
              END IF;
              RETURN NEW;
            END IF;
          END IF;
          RAISE EXCEPTION 'immutable F11 scientific receipt cannot be mutated';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scientific_commands_immutable
        BEFORE UPDATE OR DELETE ON scientific_commands
        FOR EACH ROW EXECUTE FUNCTION aletheia_reject_immutable_f11_row()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_external_action_receipts_immutable
        BEFORE UPDATE OR DELETE ON external_action_receipts
        FOR EACH ROW EXECUTE FUNCTION aletheia_reject_immutable_f11_row()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_external_action_receipts_immutable ON external_action_receipts")
    op.execute("DROP TRIGGER trg_scientific_commands_immutable ON scientific_commands")
    op.execute("DROP FUNCTION aletheia_reject_immutable_f11_row()")

    op.drop_index(
        op.f("ix_external_validation_ledgers_action_receipt_sha256"),
        table_name="external_validation_ledgers",
    )
    op.drop_index(
        op.f("ix_external_validation_ledgers_action_id"),
        table_name="external_validation_ledgers",
    )
    op.drop_constraint(
        "uq_external_validation_ledgers_action_id",
        "external_validation_ledgers",
        type_="unique",
    )
    op.drop_constraint(
        "fk_external_validation_ledgers_action_id",
        "external_validation_ledgers",
        type_="foreignkey",
    )
    op.drop_column("external_validation_ledgers", "action_receipt_sha256")
    op.drop_column("external_validation_ledgers", "action_id")

    op.drop_index(
        op.f("ix_campaign_split_ledgers_final_action_receipt_sha256"),
        table_name="campaign_split_ledgers",
    )
    op.drop_index(
        op.f("ix_campaign_split_ledgers_final_action_id"),
        table_name="campaign_split_ledgers",
    )
    op.drop_constraint(
        "uq_campaign_split_ledgers_final_action_id",
        "campaign_split_ledgers",
        type_="unique",
    )
    op.drop_constraint(
        "fk_campaign_split_ledgers_final_action_id",
        "campaign_split_ledgers",
        type_="foreignkey",
    )
    op.drop_column("campaign_split_ledgers", "final_action_receipt_sha256")
    op.drop_column("campaign_split_ledgers", "final_action_id")

    op.drop_index(op.f("ix_decisions_scientific_command_id"), table_name="decisions")
    op.drop_constraint(
        "uq_decisions_scientific_command_id",
        "decisions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_decisions_scientific_command_id_scientific_commands",
        "decisions",
        type_="foreignkey",
    )
    op.drop_column("decisions", "scientific_command_id")

    op.drop_index(op.f("ix_artifacts_scientific_command_id"), table_name="artifacts")
    op.drop_constraint(
        "uq_artifacts_scientific_commit_ordinal",
        "artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "ck_artifacts_scientific_commit_pair",
        "artifacts",
        type_="check",
    )
    op.drop_constraint(
        "fk_artifacts_scientific_command_id_scientific_commands",
        "artifacts",
        type_="foreignkey",
    )
    op.drop_column("artifacts", "commit_ordinal")
    op.drop_column("artifacts", "scientific_command_id")

    op.drop_table("external_action_receipts")
    op.drop_table("one_time_external_actions")
    op.drop_table("scientific_commands")
