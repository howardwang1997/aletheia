"""SQLAlchemy records for the Postgres-backed durable task queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aletheia.db import Base


class DurableTaskRecord(Base):
    __tablename__ = "durable_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('blocked','queued','leased','retry_wait','succeeded','failed','cancelled')",
            name="ck_durable_tasks_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_durable_tasks_attempt_count"),
        CheckConstraint("state_version >= 1", name="ck_durable_tasks_state_version"),
        UniqueConstraint("idempotency_key", name="uq_durable_tasks_idempotency_key"),
        Index(
            "ix_durable_tasks_claim",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
        Index(
            "uq_durable_tasks_active_concurrency_key",
            "concurrency_key",
            unique=True,
            postgresql_where=text(
                "concurrency_key IS NOT NULL AND "
                "status IN ('blocked','queued','leased','retry_wait')"
            ),
        ),
    )

    task_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(96), index=True)
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    concurrency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    retry_policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active_attempt_id: Mapped[str | None] = mapped_column(String(32), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_token_sha256: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    result_artifact_id: Mapped[str | None] = mapped_column(Text)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    terminal_category: Mapped[str | None] = mapped_column(String(40), index=True)
    terminal_detail_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DurableTaskDependencyRecord(Base):
    __tablename__ = "durable_task_dependencies"
    __table_args__ = (
        CheckConstraint("task_id <> dependency_task_id", name="ck_durable_task_dependency_self"),
    )

    task_id: Mapped[str] = mapped_column(
        ForeignKey("durable_tasks.task_id", ondelete="CASCADE"), primary_key=True
    )
    dependency_task_id: Mapped[str] = mapped_column(
        ForeignKey("durable_tasks.task_id"), primary_key=True, index=True
    )


class DurableTaskAttemptRecord(Base):
    __tablename__ = "durable_task_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="ck_durable_task_attempt_number"),
        UniqueConstraint("task_id", "attempt_number", name="uq_durable_task_attempt_number"),
        Index("ix_durable_task_attempts_active", "task_id", "ended_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("durable_tasks.task_id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    worker_manifest_sha256: Mapped[str] = mapped_column(String(64))
    lease_token_sha256: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    terminal_category: Mapped[str | None] = mapped_column(String(40), index=True)
    terminal_detail_sha256: Mapped[str | None] = mapped_column(String(64))
    retry_requested: Mapped[bool | None] = mapped_column(Boolean)
    retry_scheduled: Mapped[bool | None] = mapped_column(Boolean)
    partial_artifact_ids_json: Mapped[list[str]] = mapped_column(JSONB)
    logs_artifact_id: Mapped[str | None] = mapped_column(Text)
    result_artifact_id: Mapped[str | None] = mapped_column(Text)
    result_sha256: Mapped[str | None] = mapped_column(String(64))


class DurableQueueAuditRecord(Base):
    """Append-only recovery summary for operator-visible restart/fault audits."""

    __tablename__ = "durable_queue_audits"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_type: Mapped[str] = mapped_column(String(48), index=True)
    principal: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ScientificCommandRecord(Base):
    """One committed, replay-safe scientific mutation and its durable event projection.

    Rows are inserted as ``applying`` inside the mutation transaction and become visible only
    after they have a result and keyed event.  PostgreSQL therefore acts as the transactional
    outbox: a committed scientific state can never be observed without its matching event.
    """

    __tablename__ = "scientific_commands"
    __table_args__ = (
        CheckConstraint(
            "status IN ('applying','committed')",
            name="ck_scientific_commands_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_scientific_commands_idempotency_key"),
        UniqueConstraint("source_event_key", name="uq_scientific_commands_source_event_key"),
        UniqueConstraint("output_event_key", name="uq_scientific_commands_output_event_key"),
        UniqueConstraint("output_event_id", name="uq_scientific_commands_output_event_id"),
        Index("ix_scientific_commands_aggregate", "aggregate_type", "aggregate_id"),
    )

    command_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    command_type: Mapped[str] = mapped_column(String(96), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(192), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    source_event_key: Mapped[str | None] = mapped_column(String(128), index=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    principal: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    event_type: Mapped[str] = mapped_column(String(64))
    event_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_event_key: Mapped[str] = mapped_column(String(128))
    output_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class OneTimeExternalActionRecord(Base):
    """Durable intent for an outward or information-revealing action.

    A claimed action is never automatically claimed again.  An expired claim moves to
    ``reconciliation_required`` so a crash after an unknowable outward effect cannot cause a
    second effect.
    """

    __tablename__ = "one_time_external_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('claimed','reconciliation_required','completed')",
            name="ck_one_time_external_actions_status",
        ),
        CheckConstraint(
            "state_version >= 1",
            name="ck_one_time_external_actions_state_version",
        ),
        UniqueConstraint("scope_key", name="uq_one_time_external_actions_scope_key"),
        UniqueConstraint(
            "provider_idempotency_key",
            name="uq_one_time_external_actions_provider_key",
        ),
        UniqueConstraint("last_event_id", name="uq_one_time_external_actions_last_event"),
    )

    action_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(96), index=True)
    scope_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    principal: Mapped[str] = mapped_column(String(128), index=True)
    provider_idempotency_key: Mapped[str] = mapped_column(String(128))
    claim_ttl_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    state_version: Mapped[int] = mapped_column(Integer)
    claim_owner: Mapped[str] = mapped_column(String(128), index=True)
    execution_token_sha256: Mapped[str] = mapped_column(String(64))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reconcile_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    last_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ExternalActionReceiptRecord(Base):
    """Immutable proof of the one accepted outcome for an external action intent."""

    __tablename__ = "external_action_receipts"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_external_action_receipts_action"),
        UniqueConstraint(
            "action_id",
            "receipt_sha256",
            name="uq_external_action_receipts_action_receipt",
        ),
        UniqueConstraint("completion_event_key", name="uq_external_action_receipts_event_key"),
        UniqueConstraint("completion_event_id", name="uq_external_action_receipts_event_id"),
    )

    receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("one_time_external_actions.action_id"), index=True
    )
    request_sha256: Mapped[str] = mapped_column(String(64))
    outcome_sha256: Mapped[str] = mapped_column(String(64), index=True)
    provider_receipt_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    completion_event_key: Mapped[str] = mapped_column(String(128))
    completion_event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
