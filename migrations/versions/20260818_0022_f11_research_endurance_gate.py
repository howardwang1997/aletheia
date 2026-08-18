"""Add append-only, real-time-safe F11 research endurance evidence.

Revision ID: 20260818_0022
Revises: 20260818_0021
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0022"
down_revision: str | None = "20260818_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_endurance_gates",
        sa.Column("gate_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("evidence_class", sa.String(length=32), nullable=False),
        sa.Column("required_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("checkpoint_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("maximum_checkpoint_gap_seconds", sa.Integer(), nullable=False),
        sa.Column("frozen_quest_spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("initial_graph_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_budget_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_data_role_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("prerequisite_fault_campaign_id", sa.String(length=96), nullable=False),
        sa.Column("prerequisite_fault_report_sha256", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("started_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_class IN ('accelerated_engineering','real_time_72h')",
            name="ck_research_endurance_gates_evidence_class",
        ),
        sa.CheckConstraint(
            "required_duration_seconds > 0 AND checkpoint_interval_seconds > 0 "
            "AND maximum_checkpoint_gap_seconds >= checkpoint_interval_seconds",
            name="ck_research_endurance_gates_timing",
        ),
        sa.CheckConstraint(
            "evidence_class <> 'real_time_72h' OR required_duration_seconds >= 259200",
            name="ck_research_endurance_gates_real_duration",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(
            ["prerequisite_fault_campaign_id"],
            ["fault_injection_campaigns.campaign_id"],
        ),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("gate_id"),
        sa.UniqueConstraint("command_id", name="uq_research_endurance_gates_command"),
    )
    for column in (
        "command_id",
        "created_at",
        "evidence_class",
        "manifest_sha256",
        "prerequisite_fault_campaign_id",
        "quest_id",
        "started_at",
        "started_by",
    ):
        op.create_index(
            op.f(f"ix_research_endurance_gates_{column}"),
            "research_endurance_gates",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_endurance_gates_quest_started",
        "research_endurance_gates",
        ["quest_id", "started_at"],
        unique=False,
    )

    op.create_table(
        "research_endurance_checkpoints",
        sa.Column("checkpoint_id", sa.String(length=96), nullable=False),
        sa.Column("gate_id", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("parent_sha256", sa.String(length=64), nullable=False),
        sa.Column("observation_sha256", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "checkpoint_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("reproduction_count", sa.Integer(), nullable=False),
        sa.Column("process_kill_count", sa.Integer(), nullable=False),
        sa.Column("provider_interruption_count", sa.Integer(), nullable=False),
        sa.Column("structural_pivot_count", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reproduction_count >= 0 AND process_kill_count >= 0 "
            "AND provider_interruption_count >= 0 AND structural_pivot_count >= 0",
            name="ck_research_endurance_checkpoints_evidence_counts",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_research_endurance_checkpoints_sequence",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(["gate_id"], ["research_endurance_gates.gate_id"]),
        sa.PrimaryKeyConstraint("checkpoint_id"),
        sa.UniqueConstraint(
            "checkpoint_sha256",
            name="uq_research_endurance_checkpoints_sha256",
        ),
        sa.UniqueConstraint(
            "gate_id",
            "sequence",
            name="uq_research_endurance_checkpoints_gate_sequence",
        ),
        sa.UniqueConstraint(
            "command_id",
            name="uq_research_endurance_checkpoints_command",
        ),
    )
    for column in (
        "checkpoint_sha256",
        "command_id",
        "created_at",
        "created_by",
        "gate_id",
        "observation_sha256",
        "observed_at",
    ):
        op.create_index(
            op.f(f"ix_research_endurance_checkpoints_{column}"),
            "research_endurance_checkpoints",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_endurance_checkpoints_gate_observed",
        "research_endurance_checkpoints",
        ["gate_id", "observed_at"],
        unique=False,
    )

    op.create_table(
        "research_endurance_reports",
        sa.Column("gate_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=96), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("evidence_class", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False),
        sa.Column("checkpoint_count", sa.Integer(), nullable=False),
        sa.Column("negative_result_count", sa.Integer(), nullable=False),
        sa.Column("reproduction_count", sa.Integer(), nullable=False),
        sa.Column("process_kill_count", sa.Integer(), nullable=False),
        sa.Column("provider_interruption_count", sa.Integer(), nullable=False),
        sa.Column("structural_pivot_count", sa.Integer(), nullable=False),
        sa.Column("portfolio_epoch_count", sa.Integer(), nullable=False),
        sa.Column("real_72h_passed", sa.Boolean(), nullable=False),
        sa.Column("eligible_for_f11_scientific_exit_review", sa.Boolean(), nullable=False),
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
            name="ck_research_endurance_reports_disposition",
        ),
        sa.CheckConstraint(
            "evidence_class IN ('accelerated_engineering','real_time_72h')",
            name="ck_research_endurance_reports_evidence_class",
        ),
        sa.CheckConstraint(
            "elapsed_seconds >= 0 AND checkpoint_count >= 0 "
            "AND negative_result_count >= 0 AND reproduction_count >= 0 "
            "AND process_kill_count >= 0 AND provider_interruption_count >= 0 "
            "AND structural_pivot_count >= 0 AND portfolio_epoch_count >= 0",
            name="ck_research_endurance_reports_counts",
        ),
        sa.CheckConstraint(
            "real_72h_passed IS FALSE OR "
            "(evidence_class = 'real_time_72h' AND disposition = 'passed' "
            "AND elapsed_seconds >= 259200)",
            name="ck_research_endurance_reports_real_verdict",
        ),
        sa.CheckConstraint(
            "eligible_for_f11_scientific_exit_review = real_72h_passed",
            name="ck_research_endurance_reports_exit_review",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["scientific_commands.command_id"]),
        sa.ForeignKeyConstraint(["gate_id"], ["research_endurance_gates.gate_id"]),
        sa.ForeignKeyConstraint(["quest_id"], ["research_graph_nodes.node_id"]),
        sa.PrimaryKeyConstraint("gate_id"),
        sa.UniqueConstraint("command_id", name="uq_research_endurance_reports_command"),
    )
    for column in (
        "command_id",
        "completed_at",
        "created_at",
        "created_by",
        "disposition",
        "evidence_class",
        "quest_id",
        "report_sha256",
    ):
        op.create_index(
            op.f(f"ix_research_endurance_reports_{column}"),
            "research_endurance_reports",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_research_endurance_reports_quest_completed",
        "research_endurance_reports",
        ["quest_id", "completed_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_research_endurance_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'research endurance ledger rows are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "research_endurance_gates",
        "research_endurance_checkpoints",
        "research_endurance_reports",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_research_endurance_append_only()
            """
        )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_endurance_gate()
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
          quest_state text;
          quest_spec text;
          fault_quest text;
          fault_report text;
          fault_disposition text;
        BEGIN
          SELECT node_type, current_state, spec_sha256
            INTO quest_type, quest_state, quest_spec
          FROM research_graph_nodes WHERE node_id = NEW.quest_id FOR UPDATE;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 principal, created_at, input_json
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 command_principal, command_created_at, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          SELECT quest_id, report_sha256, disposition
            INTO fault_quest, fault_report, fault_disposition
          FROM fault_injection_campaigns
          WHERE campaign_id = NEW.prerequisite_fault_campaign_id;
          IF quest_type IS DISTINCT FROM 'quest'
             OR quest_state IS DISTINCT FROM 'active'
             OR quest_spec IS DISTINCT FROM NEW.frozen_quest_spec_sha256
             OR command_status IS DISTINCT FROM 'applying'
             OR command_type IS DISTINCT FROM 'research_endurance.mutation'
             OR aggregate_type IS DISTINCT FROM 'research_endurance'
             OR aggregate_id IS DISTINCT FROM NEW.gate_id
             OR command_principal IS DISTINCT FROM NEW.started_by
             OR command_created_at IS DISTINCT FROM NEW.created_at
             OR command_input->>'operation' IS DISTINCT FROM 'start'
             OR command_input->>'gate_id' IS DISTINCT FROM NEW.gate_id
             OR command_input->>'manifest_sha256' IS DISTINCT FROM NEW.manifest_sha256
             OR command_input->'manifest' IS DISTINCT FROM NEW.manifest_json
             OR NEW.manifest_json->>'gate_id' IS DISTINCT FROM NEW.gate_id
             OR NEW.manifest_json->>'quest_id' IS DISTINCT FROM NEW.quest_id
             OR NEW.manifest_json->>'evidence_class' IS DISTINCT FROM NEW.evidence_class
             OR NEW.manifest_json->>'frozen_quest_spec_sha256'
                  IS DISTINCT FROM NEW.frozen_quest_spec_sha256
             OR NEW.manifest_json->>'initial_graph_sha256'
                  IS DISTINCT FROM NEW.initial_graph_sha256
             OR NEW.manifest_json->>'frozen_budget_manifest_sha256'
                  IS DISTINCT FROM NEW.frozen_budget_manifest_sha256
             OR NEW.manifest_json->>'frozen_data_role_manifest_sha256'
                  IS DISTINCT FROM NEW.frozen_data_role_manifest_sha256
             OR NEW.manifest_json->>'prerequisite_fault_campaign_id'
                  IS DISTINCT FROM NEW.prerequisite_fault_campaign_id
             OR NEW.manifest_json->>'prerequisite_fault_report_sha256'
                  IS DISTINCT FROM NEW.prerequisite_fault_report_sha256
             OR (NEW.manifest_json->>'required_duration_seconds')::integer
                  IS DISTINCT FROM NEW.required_duration_seconds
             OR (NEW.manifest_json->>'checkpoint_interval_seconds')::integer
                  IS DISTINCT FROM NEW.checkpoint_interval_seconds
             OR (NEW.manifest_json->>'maximum_checkpoint_gap_seconds')::integer
                  IS DISTINCT FROM NEW.maximum_checkpoint_gap_seconds
             OR fault_quest IS DISTINCT FROM NEW.quest_id
             OR fault_report IS DISTINCT FROM NEW.prerequisite_fault_report_sha256
             OR fault_disposition IS DISTINCT FROM 'passed' THEN
            RAISE EXCEPTION 'research endurance gate is outside its frozen command/sources';
          END IF;
          IF EXISTS (
               SELECT 1 FROM research_endurance_gates prior
               WHERE prior.quest_id = NEW.quest_id
                 AND prior.gate_id <> NEW.gate_id
                 AND NOT EXISTS (
                   SELECT 1 FROM research_endurance_reports report
                   WHERE report.gate_id = prior.gate_id
                 )
             ) THEN
            RAISE EXCEPTION 'Quest already has an unfinished research endurance gate';
          END IF;
          IF NEW.started_at > clock_timestamp() + interval '5 seconds' THEN
            RAISE EXCEPTION 'research endurance gate starts in the future';
          END IF;
          IF NEW.evidence_class = 'real_time_72h'
             AND abs(extract(epoch FROM (clock_timestamp() - NEW.started_at))) > 5 THEN
            RAISE EXCEPTION 'real-time research endurance start must use the database clock';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_endurance_gate_guard
        BEFORE INSERT ON research_endurance_gates
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_endurance_gate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_endurance_checkpoint()
        RETURNS trigger AS $$
        DECLARE
          gate_start timestamptz;
          gate_class text;
          manifest_hash text;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_principal text;
          command_created_at timestamptz;
          command_input jsonb;
          expected_sequence integer;
          expected_parent text;
          previous_observed timestamptz;
        BEGIN
          SELECT started_at, evidence_class, manifest_sha256
            INTO gate_start, gate_class, manifest_hash
          FROM research_endurance_gates WHERE gate_id = NEW.gate_id FOR UPDATE;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 principal, created_at, input_json
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 command_principal, command_created_at, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          SELECT sequence + 1, checkpoint_sha256, observed_at
            INTO expected_sequence, expected_parent, previous_observed
          FROM research_endurance_checkpoints
          WHERE gate_id = NEW.gate_id ORDER BY sequence DESC LIMIT 1;
          IF expected_sequence IS NULL THEN
            expected_sequence := 1;
            expected_parent := manifest_hash;
            previous_observed := gate_start;
          END IF;
          IF gate_start IS NULL
             OR EXISTS (
               SELECT 1 FROM research_endurance_reports WHERE gate_id = NEW.gate_id
             )
             OR command_status IS DISTINCT FROM 'applying'
             OR command_type IS DISTINCT FROM 'research_endurance.mutation'
             OR aggregate_type IS DISTINCT FROM 'research_endurance'
             OR aggregate_id IS DISTINCT FROM NEW.gate_id
             OR command_principal IS DISTINCT FROM NEW.created_by
             OR command_created_at IS DISTINCT FROM NEW.created_at
             OR command_input->>'operation' IS DISTINCT FROM 'checkpoint'
             OR command_input->>'gate_id' IS DISTINCT FROM NEW.gate_id
             OR command_input->'evidence'
                  IS DISTINCT FROM NEW.checkpoint_json->'evidence'
             OR NEW.sequence IS DISTINCT FROM expected_sequence
             OR NEW.parent_sha256 IS DISTINCT FROM expected_parent
             OR NEW.observed_at <= previous_observed
             OR NEW.checkpoint_json->>'checkpoint_id' IS DISTINCT FROM NEW.checkpoint_id
             OR NEW.checkpoint_json->>'gate_id' IS DISTINCT FROM NEW.gate_id
             OR (NEW.checkpoint_json->>'sequence')::integer IS DISTINCT FROM NEW.sequence
             OR NEW.checkpoint_json->>'parent_sha256' IS DISTINCT FROM NEW.parent_sha256
             OR NEW.checkpoint_json->>'checkpoint_sha256' IS DISTINCT FROM NEW.checkpoint_sha256
             OR NEW.checkpoint_json #>> '{observation,observation_sha256}'
                  IS DISTINCT FROM NEW.observation_sha256
             OR (NEW.checkpoint_json #>> '{observation,observed_at}')::timestamptz
                  IS DISTINCT FROM NEW.observed_at
             OR jsonb_array_length(NEW.checkpoint_json #> '{evidence,reproductions}')
                  IS DISTINCT FROM NEW.reproduction_count
             OR (SELECT count(*)::integer
                   FROM jsonb_array_elements(
                     NEW.checkpoint_json #> '{evidence,interruptions}'
                   ) evidence
                   WHERE evidence->>'kind' = 'process_kill')
                  IS DISTINCT FROM NEW.process_kill_count
             OR (SELECT count(*)::integer
                   FROM jsonb_array_elements(
                     NEW.checkpoint_json #> '{evidence,interruptions}'
                   ) evidence
                   WHERE evidence->>'kind' = 'provider_transport')
                  IS DISTINCT FROM NEW.provider_interruption_count
             OR jsonb_array_length(NEW.checkpoint_json #> '{evidence,structural_pivots}')
                  IS DISTINCT FROM NEW.structural_pivot_count THEN
            RAISE EXCEPTION 'research endurance checkpoint is outside its command/chain';
          END IF;
          IF gate_class = 'real_time_72h'
             AND abs(extract(epoch FROM (clock_timestamp() - NEW.observed_at))) > 5 THEN
            RAISE EXCEPTION 'real-time endurance checkpoint must use the database clock';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_endurance_checkpoint_guard
        BEFORE INSERT ON research_endurance_checkpoints
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_endurance_checkpoint()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_endurance_report()
        RETURNS trigger AS $$
        DECLARE
          gate_quest text;
          gate_class text;
          gate_start timestamptz;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_principal text;
          command_created_at timestamptz;
          command_input jsonb;
        BEGIN
          SELECT quest_id, evidence_class, started_at
            INTO gate_quest, gate_class, gate_start
          FROM research_endurance_gates WHERE gate_id = NEW.gate_id FOR UPDATE;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 principal, created_at, input_json
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 command_principal, command_created_at, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF gate_quest IS DISTINCT FROM NEW.quest_id
             OR gate_class IS DISTINCT FROM NEW.evidence_class
             OR command_status IS DISTINCT FROM 'applying'
             OR command_type IS DISTINCT FROM 'research_endurance.mutation'
             OR aggregate_type IS DISTINCT FROM 'research_endurance'
             OR aggregate_id IS DISTINCT FROM NEW.gate_id
             OR command_principal IS DISTINCT FROM NEW.created_by
             OR command_created_at IS DISTINCT FROM NEW.created_at
             OR command_input->>'operation' IS DISTINCT FROM 'finalize'
             OR command_input->>'gate_id' IS DISTINCT FROM NEW.gate_id
             OR COALESCE(command_input->'efficiency', 'null'::jsonb)
                  IS DISTINCT FROM COALESCE(NEW.report_json->'efficiency', 'null'::jsonb)
             OR NEW.report_json #>> '{manifest,gate_id}' IS DISTINCT FROM NEW.gate_id
             OR NEW.report_json #>> '{manifest,quest_id}' IS DISTINCT FROM NEW.quest_id
             OR NEW.report_json->>'report_sha256' IS DISTINCT FROM NEW.report_sha256
             OR NEW.report_json->>'disposition' IS DISTINCT FROM NEW.disposition
             OR (NEW.report_json->>'elapsed_seconds')::integer
                  IS DISTINCT FROM NEW.elapsed_seconds
             OR (NEW.report_json->>'checkpoint_count')::integer
                  IS DISTINCT FROM NEW.checkpoint_count
             OR (NEW.report_json->>'negative_result_count')::integer
                  IS DISTINCT FROM NEW.negative_result_count
             OR (NEW.report_json->>'reproduction_count')::integer
                  IS DISTINCT FROM NEW.reproduction_count
             OR (NEW.report_json->>'process_kill_count')::integer
                  IS DISTINCT FROM NEW.process_kill_count
             OR (NEW.report_json->>'provider_interruption_count')::integer
                  IS DISTINCT FROM NEW.provider_interruption_count
             OR (NEW.report_json->>'structural_pivot_count')::integer
                  IS DISTINCT FROM NEW.structural_pivot_count
             OR (NEW.report_json->>'portfolio_epoch_count')::integer
                  IS DISTINCT FROM NEW.portfolio_epoch_count
             OR (NEW.report_json->>'real_72h_passed')::boolean
                  IS DISTINCT FROM NEW.real_72h_passed
             OR (NEW.report_json->>'eligible_for_f11_scientific_exit_review')::boolean
                  IS DISTINCT FROM NEW.eligible_for_f11_scientific_exit_review
             OR (NEW.report_json->>'completed_at')::timestamptz
                  IS DISTINCT FROM NEW.completed_at
             OR NEW.elapsed_seconds IS DISTINCT FROM
                  floor(extract(epoch FROM (NEW.completed_at - gate_start)))::integer THEN
            RAISE EXCEPTION 'research endurance report is outside its command/gate';
          END IF;
          IF gate_class = 'real_time_72h'
             AND abs(extract(epoch FROM (clock_timestamp() - NEW.completed_at))) > 5 THEN
            RAISE EXCEPTION 'real-time endurance report must use the database clock';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_endurance_report_guard
        BEFORE INSERT ON research_endurance_reports
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_endurance_report()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_research_endurance_report_guard ON research_endurance_reports"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_endurance_report()")
    op.execute(
        "DROP TRIGGER trg_research_endurance_checkpoint_guard "
        "ON research_endurance_checkpoints"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_endurance_checkpoint()")
    op.execute(
        "DROP TRIGGER trg_research_endurance_gate_guard ON research_endurance_gates"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_endurance_gate()")
    for table in reversed(
        (
            "research_endurance_gates",
            "research_endurance_checkpoints",
            "research_endurance_reports",
        )
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION aletheia_research_endurance_append_only()")

    op.drop_index(
        "ix_research_endurance_reports_quest_completed",
        table_name="research_endurance_reports",
    )
    for column in reversed(
        (
            "command_id",
            "completed_at",
            "created_at",
            "created_by",
            "disposition",
            "evidence_class",
            "quest_id",
            "report_sha256",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_endurance_reports_{column}"),
            table_name="research_endurance_reports",
        )
    op.drop_table("research_endurance_reports")

    op.drop_index(
        "ix_research_endurance_checkpoints_gate_observed",
        table_name="research_endurance_checkpoints",
    )
    for column in reversed(
        (
            "checkpoint_sha256",
            "command_id",
            "created_at",
            "created_by",
            "gate_id",
            "observation_sha256",
            "observed_at",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_endurance_checkpoints_{column}"),
            table_name="research_endurance_checkpoints",
        )
    op.drop_table("research_endurance_checkpoints")

    op.drop_index(
        "ix_research_endurance_gates_quest_started",
        table_name="research_endurance_gates",
    )
    for column in reversed(
        (
            "command_id",
            "created_at",
            "evidence_class",
            "manifest_sha256",
            "prerequisite_fault_campaign_id",
            "quest_id",
            "started_at",
            "started_by",
        )
    ):
        op.drop_index(
            op.f(f"ix_research_endurance_gates_{column}"),
            table_name="research_endurance_gates",
        )
    op.drop_table("research_endurance_gates")
