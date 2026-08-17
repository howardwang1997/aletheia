"""Add append-only F11 resilience fault-injection campaign evidence.

Revision ID: 20260818_0021
Revises: 20260818_0020
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0021"
down_revision: str | None = "20260818_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fault_injection_campaigns",
        sa.Column("campaign_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("scientific_state_loss_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_scientific_state_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_budget_charge_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_outward_authorization_count", sa.Integer(), nullable=False),
        sa.Column(
            "unresolved_ambiguity_without_block_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("event_state_mismatch_count", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "disposition IN ('passed','failed','blocked')",
            name="ck_fault_injection_campaigns_disposition",
        ),
        sa.CheckConstraint(
            "scenario_count >= 10 AND passed_count >= 0 AND failed_count >= 0 "
            "AND blocked_count >= 0 AND "
            "scenario_count = passed_count + failed_count + blocked_count",
            name="ck_fault_injection_campaigns_counts",
        ),
        sa.CheckConstraint(
            "(disposition = 'passed' AND passed_count = scenario_count "
            "AND failed_count = 0 AND blocked_count = 0) OR "
            "(disposition = 'failed' AND failed_count > 0) OR "
            "(disposition = 'blocked' AND failed_count = 0 AND blocked_count > 0)",
            name="ck_fault_injection_campaigns_verdict",
        ),
        sa.CheckConstraint(
            "scientific_state_loss_count >= 0 "
            "AND duplicate_scientific_state_count >= 0 "
            "AND duplicate_budget_charge_count >= 0 "
            "AND duplicate_outward_authorization_count >= 0 "
            "AND unresolved_ambiguity_without_block_count >= 0 "
            "AND event_state_mismatch_count >= 0",
            name="ck_fault_injection_campaigns_core_counts",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["scientific_commands.command_id"],
        ),
        sa.ForeignKeyConstraint(
            ["quest_id"],
            ["research_graph_nodes.node_id"],
        ),
        sa.PrimaryKeyConstraint("campaign_id"),
        sa.UniqueConstraint(
            "command_id",
            name="uq_fault_injection_campaigns_command",
        ),
    )
    for column in (
        "command_id",
        "completed_at",
        "created_at",
        "created_by",
        "disposition",
        "manifest_sha256",
        "quest_id",
        "report_sha256",
    ):
        op.create_index(
            op.f(f"ix_fault_injection_campaigns_{column}"),
            "fault_injection_campaigns",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_fault_injection_campaigns_quest_completed",
        "fault_injection_campaigns",
        ["quest_id", "completed_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_fault_injection_campaign_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'fault injection campaign rows are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fault_injection_campaigns_append_only
        BEFORE UPDATE OR DELETE ON fault_injection_campaigns
        FOR EACH ROW EXECUTE FUNCTION aletheia_fault_injection_campaign_append_only()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_fault_injection_campaign()
        RETURNS trigger AS $$
        DECLARE
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_principal text;
          command_created_at timestamptz;
          command_input jsonb;
          quest_type text;
        BEGIN
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type,
                 scientific_commands.aggregate_id, principal, created_at, input_json
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 command_principal, command_created_at, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'resilience_fault_campaign.commit'
             OR aggregate_type <> 'fault_campaign'
             OR aggregate_id IS DISTINCT FROM NEW.campaign_id
             OR command_principal IS DISTINCT FROM NEW.created_by
             OR command_created_at IS DISTINCT FROM NEW.created_at
             OR command_input->>'operation' <> 'commit_campaign'
             OR command_input->>'kind' <> 'fault_injection_campaign'
             OR command_input->>'campaign_id' IS DISTINCT FROM NEW.campaign_id
             OR command_input->>'manifest_sha256' IS DISTINCT FROM NEW.manifest_sha256
             OR command_input->>'report_sha256' IS DISTINCT FROM NEW.report_sha256
             OR command_input->>'disposition' IS DISTINCT FROM NEW.disposition
             OR (command_input->>'scenario_count')::integer IS DISTINCT FROM NEW.scenario_count
             OR (command_input->>'passed_count')::integer IS DISTINCT FROM NEW.passed_count
             OR (command_input->>'failed_count')::integer IS DISTINCT FROM NEW.failed_count
             OR (command_input->>'blocked_count')::integer IS DISTINCT FROM NEW.blocked_count
             OR (command_input->>'scientific_state_loss_count')::integer
                  IS DISTINCT FROM NEW.scientific_state_loss_count
             OR (command_input->>'duplicate_scientific_state_count')::integer
                  IS DISTINCT FROM NEW.duplicate_scientific_state_count
             OR (command_input->>'duplicate_budget_charge_count')::integer
                  IS DISTINCT FROM NEW.duplicate_budget_charge_count
             OR (command_input->>'duplicate_outward_authorization_count')::integer
                  IS DISTINCT FROM NEW.duplicate_outward_authorization_count
             OR (command_input->>'unresolved_ambiguity_without_block_count')::integer
                  IS DISTINCT FROM NEW.unresolved_ambiguity_without_block_count
             OR (command_input->>'event_state_mismatch_count')::integer
                  IS DISTINCT FROM NEW.event_state_mismatch_count THEN
            RAISE EXCEPTION 'fault injection campaign is outside its applying command';
          END IF;

          IF NEW.report_json #>> '{manifest,campaign_id}'
                  IS DISTINCT FROM NEW.campaign_id
             OR NEW.report_json #>> '{manifest,quest_id}'
                  IS DISTINCT FROM NEW.quest_id
             OR NEW.report_json->>'report_sha256' IS DISTINCT FROM NEW.report_sha256
             OR NEW.report_json->>'disposition' IS DISTINCT FROM NEW.disposition
             OR (NEW.report_json->>'scenario_count')::integer
                  IS DISTINCT FROM NEW.scenario_count
             OR (NEW.report_json->>'passed_count')::integer
                  IS DISTINCT FROM NEW.passed_count
             OR (NEW.report_json->>'failed_count')::integer
                  IS DISTINCT FROM NEW.failed_count
             OR (NEW.report_json->>'blocked_count')::integer
                  IS DISTINCT FROM NEW.blocked_count
             OR (NEW.report_json->>'scientific_state_loss_count')::integer
                  IS DISTINCT FROM NEW.scientific_state_loss_count
             OR (NEW.report_json->>'duplicate_scientific_state_count')::integer
                  IS DISTINCT FROM NEW.duplicate_scientific_state_count
             OR (NEW.report_json->>'duplicate_budget_charge_count')::integer
                  IS DISTINCT FROM NEW.duplicate_budget_charge_count
             OR (NEW.report_json->>'duplicate_outward_authorization_count')::integer
                  IS DISTINCT FROM NEW.duplicate_outward_authorization_count
             OR (NEW.report_json->>'unresolved_ambiguity_without_block_count')::integer
                  IS DISTINCT FROM NEW.unresolved_ambiguity_without_block_count
             OR (NEW.report_json->>'event_state_mismatch_count')::integer
                  IS DISTINCT FROM NEW.event_state_mismatch_count
             OR (NEW.report_json->>'completed_at')::timestamptz
                  IS DISTINCT FROM NEW.completed_at
             OR jsonb_array_length(NEW.report_json->'results')
                  IS DISTINCT FROM NEW.scenario_count THEN
            RAISE EXCEPTION 'fault injection campaign report bindings are inconsistent';
          END IF;

          IF NEW.created_at < NEW.completed_at THEN
            RAISE EXCEPTION 'fault injection campaign commit predates its evidence';
          END IF;
          IF NEW.disposition = 'passed' AND (
               NEW.scientific_state_loss_count <> 0
               OR NEW.duplicate_scientific_state_count <> 0
               OR NEW.duplicate_budget_charge_count <> 0
               OR NEW.duplicate_outward_authorization_count <> 0
               OR NEW.unresolved_ambiguity_without_block_count <> 0
               OR NEW.event_state_mismatch_count <> 0
             ) THEN
            RAISE EXCEPTION 'passing fault campaign has a nonzero core invariant';
          END IF;
          IF NEW.quest_id IS NOT NULL THEN
            SELECT node_type INTO quest_type
            FROM research_graph_nodes WHERE node_id = NEW.quest_id;
            IF NOT FOUND OR quest_type <> 'quest' THEN
              RAISE EXCEPTION 'fault injection campaign is scoped to a non-Quest node';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fault_injection_campaign_guard
        BEFORE INSERT ON fault_injection_campaigns
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_fault_injection_campaign()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_fault_injection_campaign_guard ON fault_injection_campaigns"
    )
    op.execute("DROP FUNCTION aletheia_validate_fault_injection_campaign()")
    op.execute(
        "DROP TRIGGER trg_fault_injection_campaigns_append_only "
        "ON fault_injection_campaigns"
    )
    op.execute("DROP FUNCTION aletheia_fault_injection_campaign_append_only()")
    op.drop_index(
        "ix_fault_injection_campaigns_quest_completed",
        table_name="fault_injection_campaigns",
    )
    for column in reversed(
        (
            "command_id",
            "completed_at",
            "created_at",
            "created_by",
            "disposition",
            "manifest_sha256",
            "quest_id",
            "report_sha256",
        )
    ):
        op.drop_index(
            op.f(f"ix_fault_injection_campaigns_{column}"),
            table_name="fault_injection_campaigns",
        )
    op.drop_table("fault_injection_campaigns")
