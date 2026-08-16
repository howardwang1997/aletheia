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
