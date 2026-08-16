"""Add F11 durable tasks and resumable durable event identities.

Revision ID: 20260816_0006
Revises: 20260815_0005
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0006"
down_revision: str | None = "20260815_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("event_key", sa.String(length=128), nullable=True))
    op.add_column("events", sa.Column("event_sha256", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_events_key_has_sha256",
        "events",
        "event_key IS NULL OR event_sha256 IS NOT NULL",
    )
    op.create_unique_constraint("uq_events_event_key", "events", ["event_key"])

    op.create_table(
        "durable_tasks",
        sa.Column("task_id", sa.String(length=96), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("task_type", sa.String(length=96), nullable=False),
        sa.Column("inputs_sha256", sa.String(length=64), nullable=False),
        sa.Column("inputs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("retry_policy_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_attempt_id", sa.String(length=32), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token_sha256", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_artifact_id", sa.Text(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("terminal_category", sa.String(length=40), nullable=True),
        sa.Column("terminal_detail_sha256", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint("attempt_count >= 0", name="ck_durable_tasks_attempt_count"),
        sa.CheckConstraint("state_version >= 1", name="ck_durable_tasks_state_version"),
        sa.CheckConstraint(
            "status IN ('blocked','queued','leased','retry_wait','succeeded','failed','cancelled')",
            name="ck_durable_tasks_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_durable_tasks_idempotency_key"),
    )
    for column in (
        "active_attempt_id",
        "available_at",
        "created_at",
        "lease_expires_at",
        "lease_owner",
        "owner",
        "run_id",
        "status",
        "task_type",
        "terminal_category",
    ):
        op.create_index(
            op.f(f"ix_durable_tasks_{column}"),
            "durable_tasks",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_durable_tasks_claim",
        "durable_tasks",
        ["status", "available_at", "priority", "created_at"],
        unique=False,
    )

    op.create_table(
        "durable_task_dependencies",
        sa.Column("task_id", sa.String(length=96), nullable=False),
        sa.Column("dependency_task_id", sa.String(length=96), nullable=False),
        sa.CheckConstraint("task_id <> dependency_task_id", name="ck_durable_task_dependency_self"),
        sa.ForeignKeyConstraint(
            ["dependency_task_id"],
            ["durable_tasks.task_id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["durable_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "dependency_task_id"),
    )
    op.create_index(
        op.f("ix_durable_task_dependencies_dependency_task_id"),
        "durable_task_dependencies",
        ["dependency_task_id"],
        unique=False,
    )

    op.create_table(
        "durable_task_attempts",
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=96), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("worker_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("lease_token_sha256", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_category", sa.String(length=40), nullable=True),
        sa.Column("terminal_detail_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "partial_artifact_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("logs_artifact_id", sa.Text(), nullable=True),
        sa.Column("result_artifact_id", sa.Text(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.CheckConstraint("attempt_number >= 1", name="ck_durable_task_attempt_number"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["durable_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_durable_task_attempt_number"),
    )
    for column in (
        "ended_at",
        "lease_expires_at",
        "started_at",
        "task_id",
        "terminal_category",
        "worker_id",
    ):
        op.create_index(
            op.f(f"ix_durable_task_attempts_{column}"),
            "durable_task_attempts",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_durable_task_attempts_active",
        "durable_task_attempts",
        ["task_id", "ended_at"],
        unique=False,
    )

    op.create_table(
        "durable_queue_audits",
        sa.Column("audit_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("audit_type", sa.String(length=48), nullable=False),
        sa.Column("principal", sa.String(length=128), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        op.f("ix_durable_queue_audits_audit_type"),
        "durable_queue_audits",
        ["audit_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_durable_queue_audits_created_at"),
        "durable_queue_audits",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("durable_queue_audits")
    op.drop_table("durable_task_attempts")
    op.drop_table("durable_task_dependencies")
    op.drop_table("durable_tasks")
    op.drop_constraint("uq_events_event_key", "events", type_="unique")
    op.drop_constraint("ck_events_key_has_sha256", "events", type_="check")
    op.drop_column("events", "event_sha256")
    op.drop_column("events", "event_key")
