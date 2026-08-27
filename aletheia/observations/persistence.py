"""SQLAlchemy records for the durable scientific-observation bridge.

The records in this module are append-only authority material.  Operational lease and retry
state remains in :mod:`aletheia.jobs`; authoritative Quest state remains in the Research Kernel.
The store adapter writes these rows through a caller-owned transaction so a bridge receipt and its
kernel event can be committed (or rolled back) together.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aletheia.db import Base


# PostgreSQL receives JSONB in the audited migration.  The portable variant keeps the contract
# helpers executable in isolated SQLite tests without weakening the production DDL.
_JSON = JSON().with_variant(JSONB, "postgresql")
_HASH_SQL = "length({name}) = 64 AND lower({name}) = {name}"
_QUEST_SQL = "length(quest_id) = 36 AND substr(quest_id, 1, 4) = 'qst_'"
_SLOT_SQL = "length(scientific_slot_id) = 36 AND substr(scientific_slot_id, 1, 4) = 'sos_'"


def _hashes(*names: str) -> str:
    return " AND ".join(_HASH_SQL.format(name=name) for name in names)


def _postgresql_json_check(expression: str, *, name: str) -> CheckConstraint:
    """Mirror PostgreSQL JSONB authority checks without breaking portable SQLite fixtures."""

    return CheckConstraint(expression, name=name).ddl_if(dialect="postgresql")


class ResearchControllerRegistrationRecord(Base):
    """One durable controller identity for one Quest."""

    __tablename__ = "research_controller_registrations"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "registration_sha256",
                "controller_manifest_sha256",
                "launch_request_sha256",
            ),
            name="ck_rc_reg_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rc_reg_quest"),
        CheckConstraint(
            "length(registration_id) = 36 AND substr(registration_id, 1, 4) = 'rcr_'",
            name="ck_rc_reg_id",
        ),
        CheckConstraint(
            "controller_kind = 'research.controller.v1'",
            name="ck_rc_reg_kind",
        ),
        _postgresql_json_check(
            "jsonb_typeof(registration_json) = 'object' AND "
            "registration_json->>'schema_name' = "
            "'aletheia.research_controller_registration' AND "
            "registration_json->>'registration_id' = registration_id AND "
            "registration_json->>'controller_id' = controller_id AND "
            "registration_json->>'controller_manifest_sha256' = "
            "controller_manifest_sha256 AND "
            "registration_json->>'controller_principal_id' = controller_principal_id AND "
            "registration_json->>'registered_by_principal_id' = "
            "registered_by_principal_id AND "
            "registration_json #>> '{launch_request,quest_id}' = quest_id AND "
            "(registration_json->>'registered_at')::timestamptz = registered_at",
            name="ck_rc_reg_json",
        ),
        UniqueConstraint("quest_id", name="uq_rc_reg_quest"),
        UniqueConstraint("registration_id", name="uq_rc_reg_id"),
        UniqueConstraint("launch_request_sha256", name="uq_rc_reg_launch_request"),
        UniqueConstraint(
            "registration_sha256",
            "registration_id",
            "quest_id",
            name="uq_rc_reg_exact_quest",
        ),
        UniqueConstraint(
            "registration_sha256",
            "registration_id",
            "quest_id",
            "launch_request_sha256",
            name="uq_rc_reg_exact_launch",
        ),
        Index("ix_rc_reg_registered_at", "registered_at"),
        Index("ix_rc_reg_controller", "controller_id"),
    )

    registration_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    registration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quest_id: Mapped[str] = mapped_column(
        ForeignKey("research_quest_streams.quest_id"), nullable=False
    )
    controller_id: Mapped[str] = mapped_column(String(128), nullable=False)
    controller_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    controller_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    controller_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    registered_by_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    launch_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registration_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchControllerDeliveryRecord(Base):
    """Exactly one durable-task wakeup for one exact Research Kernel outbox item."""

    __tablename__ = "research_controller_deliveries"
    __table_args__ = (
        CheckConstraint(_hashes("delivery_sha256", "source_sha256"), name="ck_rc_delivery_hashes"),
        CheckConstraint(_QUEST_SQL, name="ck_rc_delivery_quest"),
        CheckConstraint(
            "source_kind IN ('launch','kernel_outbox','execution_terminal_outbox')",
            name="ck_rc_delivery_source_kind",
        ),
        CheckConstraint(
            "(source_kind = 'launch' AND source_key = registration_id "
            "AND launch_request_sha256 = source_sha256 "
            "AND source_stream_version IS NULL AND execution_id IS NULL "
            "AND attempt_id IS NULL) OR "
            "(source_kind = 'kernel_outbox' AND launch_request_sha256 IS NULL "
            "AND source_stream_version >= 1 AND execution_id IS NULL "
            "AND attempt_id IS NULL) OR "
            "(source_kind = 'execution_terminal_outbox' "
            "AND launch_request_sha256 IS NULL AND source_stream_version IS NULL "
            "AND execution_id IS NOT NULL AND attempt_id IS NOT NULL)",
            name="ck_rc_delivery_source_shape",
        ),
        _postgresql_json_check(
            "jsonb_typeof(delivery_json) = 'object' AND "
            "delivery_json->>'schema_name' = 'aletheia.research_controller_delivery' AND "
            "delivery_json->>'registration_sha256' = registration_sha256 AND "
            "delivery_json #>> '{wakeup,registration_id}' = registration_id AND "
            "delivery_json #>> '{wakeup,quest_id}' = quest_id AND "
            "delivery_json #>> '{wakeup,source_kind}' = source_kind AND "
            "delivery_json #>> '{wakeup,source_key}' = source_key AND "
            "delivery_json #>> '{wakeup,source_sha256}' = source_sha256 AND "
            "delivery_json->>'task_id' = task_id",
            name="ck_rc_delivery_json",
        ),
        UniqueConstraint("source_kind", "source_key", name="uq_rc_delivery_source_key"),
        UniqueConstraint("source_kind", "source_sha256", name="uq_rc_delivery_source_hash"),
        UniqueConstraint("task_id", name="uq_rc_delivery_task"),
        UniqueConstraint(
            "delivery_sha256",
            "quest_id",
            name="uq_rc_delivery_exact_quest",
        ),
        ForeignKeyConstraint(
            ["registration_sha256", "registration_id", "quest_id"],
            [
                "research_controller_registrations.registration_sha256",
                "research_controller_registrations.registration_id",
                "research_controller_registrations.quest_id",
            ],
            name="fk_rc_delivery_registration",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["source_key", "quest_id", "source_stream_version", "source_sha256"],
            [
                "research_kernel_outbox.outbox_id",
                "research_kernel_outbox.quest_id",
                "research_kernel_outbox.sequence",
                "research_kernel_outbox.event_sha256",
            ],
            name="fk_rc_delivery_exact_source",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "registration_sha256",
                "registration_id",
                "quest_id",
                "launch_request_sha256",
            ],
            [
                "research_controller_registrations.registration_sha256",
                "research_controller_registrations.registration_id",
                "research_controller_registrations.quest_id",
                "research_controller_registrations.launch_request_sha256",
            ],
            name="fk_rc_delivery_launch",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["source_key", "execution_id", "attempt_id", "source_sha256"],
            [
                "execution_qualification_terminal_outbox.outbox_id",
                "execution_qualification_terminal_outbox.execution_id",
                "execution_qualification_terminal_outbox.attempt_id",
                "execution_qualification_terminal_outbox.terminal_authority_sha256",
            ],
            name="fk_rc_delivery_execution_terminal",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rc_delivery_registration", "registration_sha256", "delivered_at"),
    )

    delivery_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    registration_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(192), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_stream_version: Mapped[int | None] = mapped_column(BigInteger)
    launch_request_sha256: Mapped[str | None] = mapped_column(String(64))
    execution_id: Mapped[str | None] = mapped_column(String(36))
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(ForeignKey("durable_tasks.task_id"), nullable=False)
    delivery_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchControllerDeliveryAttemptRecord(Base):
    """Append-only task generation for one immutable controller delivery."""

    __tablename__ = "research_controller_delivery_attempts"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "attempt_sha256",
                "delivery_sha256",
                "wakeup_sha256",
                "controller_manifest_sha256",
                "task_request_sha256",
            ),
            name="ck_rcda_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rcda_quest"),
        CheckConstraint("generation >= 0 AND generation <= 1024", name="ck_rcda_generation"),
        CheckConstraint(
            "kind IN ('initial','failure_redrive','completed_successor')",
            name="ck_rcda_kind",
        ),
        CheckConstraint(
            "(generation = 0 AND kind = 'initial' AND supersedes_task_id IS NULL "
            "AND predecessor_status IS NULL AND predecessor_terminal_category IS NULL "
            "AND predecessor_terminal_detail_sha256 IS NULL "
            "AND predecessor_result_sha256 IS NULL "
            "AND predecessor_tick_receipt_sha256 IS NULL) OR "
            "(generation > 0 AND kind = 'failure_redrive' "
            "AND supersedes_task_id IS NOT NULL AND predecessor_status = 'failed' "
            "AND predecessor_terminal_category IS NOT NULL "
            "AND predecessor_terminal_detail_sha256 IS NOT NULL "
            "AND predecessor_result_sha256 IS NULL "
            "AND predecessor_tick_receipt_sha256 IS NULL) OR "
            "(generation > 0 AND kind = 'completed_successor' "
            "AND supersedes_task_id IS NOT NULL AND predecessor_status = 'succeeded' "
            "AND predecessor_terminal_category = 'success' "
            "AND predecessor_terminal_detail_sha256 IS NULL "
            "AND predecessor_result_sha256 IS NOT NULL "
            "AND predecessor_tick_receipt_sha256 IS NOT NULL)",
            name="ck_rcda_predecessor_shape",
        ),
        _postgresql_json_check(
            "jsonb_typeof(attempt_json) = 'object' AND "
            "attempt_json->>'schema_name' = "
            "'aletheia.research_controller_delivery_attempt' AND "
            "attempt_json->>'delivery_sha256' = delivery_sha256 AND "
            "attempt_json->>'quest_id' = quest_id AND "
            "attempt_json->>'wakeup_sha256' = wakeup_sha256 AND "
            "attempt_json->>'controller_manifest_sha256' = controller_manifest_sha256 AND "
            "(attempt_json->>'generation')::bigint = generation AND "
            "attempt_json->>'kind' = kind AND attempt_json->>'task_id' = task_id AND "
            "attempt_json->>'task_request_sha256' = task_request_sha256 AND "
            "attempt_json->>'supersedes_task_id' IS NOT DISTINCT FROM supersedes_task_id AND "
            "attempt_json->>'predecessor_status' IS NOT DISTINCT FROM predecessor_status AND "
            "attempt_json->>'predecessor_terminal_category' IS NOT DISTINCT FROM "
            "predecessor_terminal_category AND "
            "attempt_json->>'predecessor_terminal_detail_sha256' IS NOT DISTINCT FROM "
            "predecessor_terminal_detail_sha256 AND "
            "attempt_json->>'predecessor_result_sha256' IS NOT DISTINCT FROM "
            "predecessor_result_sha256 AND "
            "attempt_json->>'predecessor_tick_receipt_sha256' IS NOT DISTINCT FROM "
            "predecessor_tick_receipt_sha256 AND "
            "(attempt_json->>'recorded_at')::timestamptz = recorded_at",
            name="ck_rcda_json",
        ),
        CheckConstraint("task_id <> supersedes_task_id", name="ck_rcda_distinct_tasks"),
        UniqueConstraint(
            "delivery_sha256",
            "generation",
            name="uq_rcda_delivery_generation",
        ),
        UniqueConstraint("task_id", name="uq_rcda_task"),
        UniqueConstraint("supersedes_task_id", name="uq_rcda_supersedes_task"),
        UniqueConstraint(
            "attempt_sha256",
            "delivery_sha256",
            "generation",
            "task_id",
            name="uq_rcda_exact_attempt",
        ),
        ForeignKeyConstraint(
            ["delivery_sha256", "quest_id"],
            [
                "research_controller_deliveries.delivery_sha256",
                "research_controller_deliveries.quest_id",
            ],
            name="fk_rcda_delivery",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rcda_delivery_generation", "delivery_sha256", "generation"),
        Index("ix_rcda_quest_recorded", "quest_id", "recorded_at"),
    )

    attempt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    wakeup_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    controller_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("durable_tasks.task_id"), nullable=False)
    task_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_task_id: Mapped[str | None] = mapped_column(ForeignKey("durable_tasks.task_id"))
    predecessor_status: Mapped[str | None] = mapped_column(String(16))
    predecessor_terminal_category: Mapped[str | None] = mapped_column(String(40))
    predecessor_terminal_detail_sha256: Mapped[str | None] = mapped_column(String(64))
    predecessor_result_sha256: Mapped[str | None] = mapped_column(String(64))
    predecessor_tick_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    attempt_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchControllerDeliveryResolutionRecord(Base):
    """Observable terminal disposition that permanently settles one delivery generation."""

    __tablename__ = "research_controller_delivery_resolutions"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "resolution_sha256",
                "delivery_sha256",
                "latest_attempt_sha256",
                "controller_manifest_sha256",
            ),
            name="ck_rcdr_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rcdr_quest"),
        CheckConstraint(
            "exhausted_generation >= 0 AND exhausted_generation <= 1024 "
            "AND max_delivery_generation >= 0 AND max_delivery_generation <= 1024 "
            "AND exhausted_generation <= max_delivery_generation",
            name="ck_rcdr_generation",
        ),
        CheckConstraint(
            "disposition IN ('awaiting_authority','awaiting_external_result','blocked',"
            "'authoritative_source_committed','dead_letter')",
            name="ck_rcdr_disposition",
        ),
        CheckConstraint(
            "(disposition = 'dead_letter' AND dead_letter_reason IN "
            "('generation_limit_exhausted','invalid_succeeded_result','task_cancelled')) OR "
            "(disposition <> 'dead_letter' AND dead_letter_reason IS NULL)",
            name="ck_rcdr_dead_letter_reason",
        ),
        CheckConstraint(
            "(terminal_task_status IN ('failed','cancelled') "
            "AND terminal_detail_sha256 IS NOT NULL "
            "AND terminal_result_sha256 IS NULL) OR "
            "(terminal_task_status = 'succeeded' AND terminal_category = 'success' "
            "AND terminal_detail_sha256 IS NULL AND terminal_result_sha256 IS NOT NULL)",
            name="ck_rcdr_terminal_shape",
        ),
        CheckConstraint(
            "(disposition = 'dead_letter') OR "
            "(terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NOT NULL "
            "AND step_disposition IS NOT NULL "
            "AND signed_kernel_command_committed IS NOT NULL "
            "AND independent_observation_admission_committed IS NOT NULL)",
            name="ck_rcdr_verified_success",
        ),
        CheckConstraint(
            "(disposition = 'awaiting_authority' AND step_disposition = 'awaiting_authority' "
            "AND signed_kernel_command_committed = false "
            "AND independent_observation_admission_committed = false) OR "
            "(disposition = 'awaiting_external_result' "
            "AND step_disposition = 'awaiting_external_result' "
            "AND signed_kernel_command_committed = false "
            "AND independent_observation_admission_committed = false) OR "
            "(disposition = 'blocked' AND step_disposition = 'blocked' "
            "AND signed_kernel_command_committed = false "
            "AND independent_observation_admission_committed = false) OR "
            "(disposition = 'authoritative_source_committed' "
            "AND step_disposition = 'completed' "
            "AND signed_kernel_command_committed = true) OR "
            "(disposition = 'dead_letter')",
            name="ck_rcdr_resolution_shape",
        ),
        CheckConstraint(
            "(dead_letter_reason = 'generation_limit_exhausted' "
            "AND exhausted_generation = max_delivery_generation AND "
            "((terminal_task_status = 'failed' AND tick_receipt_sha256 IS NULL "
            "AND step_disposition IS NULL) OR "
            "(terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NOT NULL "
            "AND step_disposition = 'completed' "
            "AND signed_kernel_command_committed = false "
            "AND independent_observation_admission_committed = false))) OR "
            "(dead_letter_reason = 'invalid_succeeded_result' "
            "AND terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NULL "
            "AND step_disposition IS NULL AND signed_kernel_command_committed IS NULL "
            "AND independent_observation_admission_committed IS NULL) OR "
            "(dead_letter_reason = 'task_cancelled' "
            "AND terminal_task_status = 'cancelled' AND terminal_category = 'cancelled' "
            "AND tick_receipt_sha256 IS NULL AND step_disposition IS NULL "
            "AND signed_kernel_command_committed IS NULL "
            "AND independent_observation_admission_committed IS NULL) OR "
            "dead_letter_reason IS NULL",
            name="ck_rcdr_reason_shape",
        ),
        _postgresql_json_check(
            "jsonb_typeof(resolution_json) = 'object' AND "
            "resolution_json->>'schema_name' = "
            "'aletheia.research_controller_delivery_resolution' AND "
            "resolution_json->>'delivery_sha256' = delivery_sha256 AND "
            "resolution_json->>'quest_id' = quest_id AND "
            "resolution_json->>'latest_attempt_sha256' = latest_attempt_sha256 AND "
            "(resolution_json->>'exhausted_generation')::bigint = exhausted_generation AND "
            "(resolution_json->>'max_delivery_generation')::bigint = max_delivery_generation AND "
            "resolution_json->>'terminal_task_id' = terminal_task_id AND "
            "resolution_json->>'terminal_task_status' = terminal_task_status AND "
            "resolution_json->>'terminal_category' = terminal_category AND "
            "resolution_json->>'terminal_detail_sha256' IS NOT DISTINCT FROM "
            "terminal_detail_sha256 AND "
            "resolution_json->>'terminal_result_sha256' IS NOT DISTINCT FROM "
            "terminal_result_sha256 AND "
            "resolution_json->>'tick_receipt_sha256' IS NOT DISTINCT FROM "
            "tick_receipt_sha256 AND "
            "resolution_json->>'step_disposition' IS NOT DISTINCT FROM step_disposition AND "
            "(resolution_json->>'signed_kernel_command_committed')::boolean "
            "IS NOT DISTINCT FROM signed_kernel_command_committed AND "
            "(resolution_json->>'independent_observation_admission_committed')::boolean "
            "IS NOT DISTINCT FROM independent_observation_admission_committed AND "
            "resolution_json->>'controller_manifest_sha256' = controller_manifest_sha256 AND "
            "resolution_json->>'disposition' = disposition AND "
            "resolution_json->>'dead_letter_reason' IS NOT DISTINCT FROM dead_letter_reason AND "
            "(resolution_json->>'resolved_at')::timestamptz = resolved_at",
            name="ck_rcdr_json",
        ),
        UniqueConstraint("delivery_sha256", name="uq_rcdr_delivery"),
        UniqueConstraint("terminal_task_id", name="uq_rcdr_terminal_task"),
        ForeignKeyConstraint(
            [
                "latest_attempt_sha256",
                "delivery_sha256",
                "exhausted_generation",
                "terminal_task_id",
            ],
            [
                "research_controller_delivery_attempts.attempt_sha256",
                "research_controller_delivery_attempts.delivery_sha256",
                "research_controller_delivery_attempts.generation",
                "research_controller_delivery_attempts.task_id",
            ],
            name="fk_rcdr_latest_attempt",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rcdr_quest_resolved", "quest_id", "resolved_at"),
    )

    resolution_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    latest_attempt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    exhausted_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_delivery_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    terminal_task_id: Mapped[str] = mapped_column(
        ForeignKey("durable_tasks.task_id"), nullable=False
    )
    terminal_task_status: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal_category: Mapped[str] = mapped_column(String(40), nullable=False)
    terminal_detail_sha256: Mapped[str | None] = mapped_column(String(64))
    terminal_result_sha256: Mapped[str | None] = mapped_column(String(64))
    tick_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    step_disposition: Mapped[str | None] = mapped_column(String(32))
    signed_kernel_command_committed: Mapped[bool | None] = mapped_column()
    independent_observation_admission_committed: Mapped[bool | None] = mapped_column()
    controller_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    dead_letter_reason: Mapped[str | None] = mapped_column(String(40))
    resolution_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchProtocolCompilationRecord(Base):
    """Recoverable exact compiler input/result with an explicit protocol revision chain."""

    __tablename__ = "research_protocol_compilations"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "compilation_sha256",
                "action_sha256",
                "protocol_sha256",
                "request_sha256",
                "result_sha256",
                "receipt_sha256",
            ),
            name="ck_rpc_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rpc_quest"),
        CheckConstraint(
            "protocol_version >= 1 AND "
            "((protocol_version = 1 AND revision_parent_version IS NULL "
            "AND revision_parent_sha256 IS NULL) OR "
            "(protocol_version > 1 AND revision_parent_version = protocol_version - 1 "
            "AND revision_parent_sha256 IS NOT NULL))",
            name="ck_rpc_revision",
        ),
        _postgresql_json_check(
            "jsonb_typeof(request_json) = 'object' AND "
            "jsonb_typeof(result_json) = 'object' AND "
            "request_json #>> '{protocol,protocol_id}' = protocol_id AND "
            "(request_json #>> '{protocol,version}')::bigint = protocol_version AND "
            "request_json #>> '{protocol,revision_parent_sha256}' "
            "IS NOT DISTINCT FROM revision_parent_sha256 AND "
            "result_json #>> '{receipt,protocol_sha256}' = protocol_sha256",
            name="ck_rpc_json",
        ),
        UniqueConstraint("action_sha256", name="uq_rpc_action"),
        UniqueConstraint("request_sha256", name="uq_rpc_request"),
        UniqueConstraint("result_sha256", name="uq_rpc_result"),
        UniqueConstraint("receipt_sha256", name="uq_rpc_receipt"),
        UniqueConstraint(
            "quest_id",
            "protocol_id",
            "protocol_version",
            name="uq_rpc_protocol_version",
        ),
        UniqueConstraint(
            "quest_id",
            "protocol_id",
            "protocol_version",
            "protocol_sha256",
            name="uq_rpc_protocol_identity",
        ),
        ForeignKeyConstraint(
            [
                "quest_id",
                "protocol_id",
                "revision_parent_version",
                "revision_parent_sha256",
            ],
            [
                "research_protocol_compilations.quest_id",
                "research_protocol_compilations.protocol_id",
                "research_protocol_compilations.protocol_version",
                "research_protocol_compilations.protocol_sha256",
            ],
            name="fk_rpc_revision_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rpc_quest_registered", "quest_id", "registered_at"),
    )

    compilation_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(
        ForeignKey("research_quest_streams.quest_id"), nullable=False
    )
    action_sha256: Mapped[str] = mapped_column(
        ForeignKey("research_kernel_objects.object_sha256"), nullable=False
    )
    protocol_id: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_parent_version: Mapped[int | None] = mapped_column(BigInteger)
    revision_parent_sha256: Mapped[str | None] = mapped_column(String(64))
    protocol_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchScientificExecutionAuthorizationRecord(Base):
    """Registered scientific eligibility for one exact action/protocol execution slot.

    ``execution_id`` and ``attempt_id`` deliberately have no early foreign key to
    ``execution_attempts``: the SEA must commit before PR-4 admission creates that attempt.  The
    later raw-run custody adapter resolves and re-verifies the exact completed attempt lineage.
    """

    __tablename__ = "research_scientific_execution_authorizations"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "authorization_sha256",
                "action_sha256",
                "source_event_sha256",
                "qualification_bundle_sha256",
                "qualification_grant_sha256",
            ),
            name="ck_rsea_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rsea_quest"),
        CheckConstraint(_SLOT_SQL, name="ck_rsea_slot"),
        CheckConstraint(
            "source_event_type = 'action_authorized' AND source_event_sequence >= 1",
            name="ck_rsea_source_event",
        ),
        CheckConstraint(
            "length(execution_id) = 36 AND substr(execution_id, 1, 4) = 'exe_' "
            "AND length(attempt_id) = 36 AND substr(attempt_id, 1, 4) = 'iat_'",
            name="ck_rsea_execution",
        ),
        CheckConstraint(
            "authorized_at <= registered_at AND registered_at < expires_at "
            "AND expires_at < observation_admission_deadline",
            name="ck_rsea_time",
        ),
        _postgresql_json_check(
            "jsonb_typeof(authorization_json) = 'object' AND "
            "authorization_json->>'schema_name' = "
            "'aletheia.scientific_execution_authorization' AND "
            "authorization_json #>> '{message,scientific_slot_id}' = "
            "scientific_slot_id AND "
            "authorization_json #>> "
            "'{message,action_protocol_binding,action,quest_id}' = quest_id AND "
            "authorization_json #>> "
            "'{message,qualification_bundle,intent,execution_id}' = execution_id AND "
            "authorization_json #>> "
            "'{message,qualification_bundle,intent,infrastructure_attempt,"
            "infrastructure_attempt_id}' = attempt_id",
            name="ck_rsea_json",
        ),
        UniqueConstraint("scientific_slot_id", name="uq_rsea_slot"),
        UniqueConstraint("execution_id", name="uq_rsea_execution"),
        UniqueConstraint("attempt_id", name="uq_rsea_attempt"),
        UniqueConstraint("source_event_sha256", name="uq_rsea_source_event"),
        UniqueConstraint("qualification_bundle_sha256", name="uq_rsea_bundle"),
        UniqueConstraint("qualification_grant_sha256", name="uq_rsea_grant"),
        UniqueConstraint(
            "authorization_sha256",
            "quest_id",
            "scientific_slot_id",
            name="uq_rsea_exact_scope",
        ),
        ForeignKeyConstraint(
            ["quest_id", "source_event_sequence", "source_event_sha256", "source_event_type"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
                "research_kernel_events.event_type",
            ],
            name="fk_rsea_source_event",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rsea_quest_registered", "quest_id", "registered_at"),
    )

    authorization_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scientific_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action_sha256: Mapped[str] = mapped_column(
        ForeignKey("research_kernel_objects.object_sha256"), nullable=False
    )
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_event_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    qualification_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    qualification_grant_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_admission_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchObservationIssuanceChallengeRecord(Base):
    """Short-lived DB-issued anti-replay challenge for validation or admission."""

    __tablename__ = "research_observation_issuance_challenges"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "challenge_sha256",
                "authorization_sha256",
                "nonce_sha256",
                "database_authority_policy_sha256",
                "issuance_key_id",
            ),
            name="ck_roic_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_roic_quest"),
        CheckConstraint(_SLOT_SQL, name="ck_roic_slot"),
        CheckConstraint(
            "purpose IN ('validation','admission')",
            name="ck_roic_purpose",
        ),
        CheckConstraint(
            "(purpose = 'validation' AND raw_run_sha256 IS NOT NULL "
            "AND committed_validation_receipt_sha256 IS NULL "
            "AND validation_receipt_sha256 IS NULL) OR "
            "(purpose = 'admission' AND raw_run_sha256 IS NULL "
            "AND committed_validation_receipt_sha256 IS NOT NULL "
            "AND validation_receipt_sha256 IS NOT NULL)",
            name="ck_roic_source_shape",
        ),
        CheckConstraint(
            "issued_at <= recorded_at AND recorded_at < expires_at "
            "AND expires_at <= observation_admission_deadline",
            name="ck_roic_time",
        ),
        _postgresql_json_check(
            "jsonb_typeof(challenge_json) = 'object' AND "
            "challenge_json #>> '{message,scientific_slot_id}' = scientific_slot_id AND "
            "challenge_json #>> '{message,nonce_sha256}' = nonce_sha256 AND "
            "challenge_json #>> '{message,row_scope}' = row_scope",
            name="ck_roic_json",
        ),
        UniqueConstraint("nonce_sha256", name="uq_roic_nonce"),
        UniqueConstraint(
            "challenge_sha256",
            "quest_id",
            "scientific_slot_id",
            "authorization_sha256",
            "raw_run_sha256",
            name="uq_roic_validation_source",
        ),
        UniqueConstraint(
            "challenge_sha256",
            "quest_id",
            "scientific_slot_id",
            "authorization_sha256",
            "committed_validation_receipt_sha256",
            "validation_receipt_sha256",
            name="uq_roic_admission_source",
        ),
        ForeignKeyConstraint(
            ["authorization_sha256", "quest_id", "scientific_slot_id"],
            [
                "research_scientific_execution_authorizations.authorization_sha256",
                "research_scientific_execution_authorizations.quest_id",
                "research_scientific_execution_authorizations.scientific_slot_id",
            ],
            name="fk_roic_authorization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "committed_validation_receipt_sha256",
                "validation_receipt_sha256",
                "quest_id",
                "scientific_slot_id",
                "authorization_sha256",
            ],
            [
                "research_observation_validation_receipts.committed_receipt_sha256",
                "research_observation_validation_receipts.validation_receipt_sha256",
                "research_observation_validation_receipts.quest_id",
                "research_observation_validation_receipts.scientific_slot_id",
                "research_observation_validation_receipts.authorization_sha256",
            ],
            name="fk_roic_admission_validation",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_roic_expiry", "purpose", "expires_at"),
    )

    challenge_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scientific_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    authorization_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_run_sha256: Mapped[str | None] = mapped_column(String(64))
    committed_validation_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    database_authority_policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_by_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    issuance_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    challenge_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_admission_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchObservationValidationReceiptRecord(Base):
    """One independently signed and DB-committed validation result per Phase-1 slot."""

    __tablename__ = "research_observation_validation_receipts"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "committed_receipt_sha256",
                "validation_receipt_sha256",
                "authorization_sha256",
                "qualification_admission_sha256",
                "raw_run_sha256",
                "issuance_challenge_sha256",
            ),
            name="ck_rovr_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rovr_quest"),
        CheckConstraint(_SLOT_SQL, name="ck_rovr_slot"),
        CheckConstraint(
            "disposition IN ('validated_confirmation','rejected_scientific','blocked_execution')",
            name="ck_rovr_disposition",
        ),
        CheckConstraint(
            "(disposition = 'validated_confirmation' "
            "AND outcome IN ('positive','negative','inconclusive') "
            "AND scientific_observation_sha256 IS NOT NULL) OR "
            "(disposition <> 'validated_confirmation' AND outcome IS NULL "
            "AND scientific_observation_sha256 IS NULL)",
            name="ck_rovr_outcome",
        ),
        CheckConstraint(
            "validated_at <= registered_at AND registered_at <= committed_at",
            name="ck_rovr_time",
        ),
        _postgresql_json_check(
            "jsonb_typeof(committed_receipt_json) = 'object' AND "
            "committed_receipt_json->>'schema_name' = "
            "'aletheia.committed_observation_validation_receipt' AND "
            "committed_receipt_json #>> '{message,validation_receipt_sha256}' = "
            "validation_receipt_sha256 AND "
            "committed_receipt_json #>> '{message,issuance_challenge_sha256}' = "
            "issuance_challenge_sha256",
            name="ck_rovr_json",
        ),
        UniqueConstraint("validation_receipt_sha256", name="uq_rovr_receipt"),
        UniqueConstraint("scientific_slot_id", name="uq_rovr_slot"),
        UniqueConstraint("raw_run_sha256", name="uq_rovr_raw_run"),
        UniqueConstraint("issuance_challenge_sha256", name="uq_rovr_challenge"),
        UniqueConstraint("scientific_observation_sha256", name="uq_rovr_observation"),
        UniqueConstraint(
            "committed_receipt_sha256",
            "validation_receipt_sha256",
            "quest_id",
            "scientific_slot_id",
            "authorization_sha256",
            name="uq_rovr_exact_receipt",
        ),
        UniqueConstraint(
            "committed_receipt_sha256",
            "validation_receipt_sha256",
            "quest_id",
            "scientific_slot_id",
            "authorization_sha256",
            "scientific_observation_sha256",
            name="uq_rovr_exact_observation",
        ),
        ForeignKeyConstraint(
            ["authorization_sha256", "quest_id", "scientific_slot_id"],
            [
                "research_scientific_execution_authorizations.authorization_sha256",
                "research_scientific_execution_authorizations.quest_id",
                "research_scientific_execution_authorizations.scientific_slot_id",
            ],
            name="fk_rovr_authorization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "issuance_challenge_sha256",
                "quest_id",
                "scientific_slot_id",
                "authorization_sha256",
                "raw_run_sha256",
            ],
            [
                "research_observation_issuance_challenges.challenge_sha256",
                "research_observation_issuance_challenges.quest_id",
                "research_observation_issuance_challenges.scientific_slot_id",
                "research_observation_issuance_challenges.authorization_sha256",
                "research_observation_issuance_challenges.raw_run_sha256",
            ],
            name="fk_rovr_exact_challenge",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rovr_quest_committed", "quest_id", "committed_at"),
    )

    committed_receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    validation_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scientific_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    authorization_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    qualification_admission_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_qualification_admissions.admission_sha256"), nullable=False
    )
    raw_run_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issuance_challenge_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_campaign_sha256: Mapped[str | None] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16))
    scientific_observation_sha256: Mapped[str | None] = mapped_column(String(64))
    committed_receipt_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchObservationAdmissionRecord(Base):
    """Final Phase-1 slot CAS, optionally bound to its incorporation kernel event."""

    __tablename__ = "research_observation_admissions"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "committed_admission_sha256",
                "decision_sha256",
                "authorization_sha256",
                "committed_validation_receipt_sha256",
                "validation_receipt_sha256",
                "issuance_challenge_sha256",
            ),
            name="ck_roa_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_roa_quest"),
        CheckConstraint(_SLOT_SQL, name="ck_roa_slot"),
        CheckConstraint("disposition = 'admitted'", name="ck_roa_disposition"),
        CheckConstraint(
            "admitted_observation_sha256 IS NOT NULL "
            "AND incorporated_event_sequence IS NOT NULL "
            "AND incorporated_event_sha256 IS NOT NULL "
            "AND incorporated_event_type = 'observation_incorporated'",
            name="ck_roa_incorporation",
        ),
        CheckConstraint(
            "registered_at <= committed_at",
            name="ck_roa_time",
        ),
        _postgresql_json_check(
            "jsonb_typeof(admission_json) = 'object' AND "
            "admission_json->>'schema_name' = "
            "'aletheia.committed_observation_admission' AND "
            "admission_json #>> '{message,decision_sha256}' = decision_sha256 AND "
            "admission_json #>> '{message,committed_validation_receipt_sha256}' = "
            "committed_validation_receipt_sha256 AND "
            "admission_json #>> "
            "'{message,exact_registered_validation_receipt_sha256}' = "
            "validation_receipt_sha256 AND "
            "admission_json #>> '{message,issuance_challenge_sha256}' = "
            "issuance_challenge_sha256",
            name="ck_roa_json",
        ),
        UniqueConstraint("scientific_slot_id", name="uq_roa_phase1_slot"),
        UniqueConstraint("decision_sha256", name="uq_roa_decision"),
        UniqueConstraint(
            "committed_validation_receipt_sha256",
            name="uq_roa_validation",
        ),
        UniqueConstraint("issuance_challenge_sha256", name="uq_roa_challenge"),
        UniqueConstraint("admitted_observation_sha256", name="uq_roa_observation"),
        UniqueConstraint("incorporated_event_sha256", name="uq_roa_event"),
        UniqueConstraint(
            "committed_admission_sha256",
            "quest_id",
            "scientific_slot_id",
            "admitted_observation_sha256",
            name="uq_roa_exact_observation",
        ),
        ForeignKeyConstraint(
            ["authorization_sha256", "quest_id", "scientific_slot_id"],
            [
                "research_scientific_execution_authorizations.authorization_sha256",
                "research_scientific_execution_authorizations.quest_id",
                "research_scientific_execution_authorizations.scientific_slot_id",
            ],
            name="fk_roa_authorization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "committed_validation_receipt_sha256",
                "validation_receipt_sha256",
                "quest_id",
                "scientific_slot_id",
                "authorization_sha256",
            ],
            [
                "research_observation_validation_receipts.committed_receipt_sha256",
                "research_observation_validation_receipts.validation_receipt_sha256",
                "research_observation_validation_receipts.quest_id",
                "research_observation_validation_receipts.scientific_slot_id",
                "research_observation_validation_receipts.authorization_sha256",
            ],
            name="fk_roa_exact_validation",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "committed_validation_receipt_sha256",
                "validation_receipt_sha256",
                "quest_id",
                "scientific_slot_id",
                "authorization_sha256",
                "admitted_observation_sha256",
            ],
            [
                "research_observation_validation_receipts.committed_receipt_sha256",
                "research_observation_validation_receipts.validation_receipt_sha256",
                "research_observation_validation_receipts.quest_id",
                "research_observation_validation_receipts.scientific_slot_id",
                "research_observation_validation_receipts.authorization_sha256",
                "research_observation_validation_receipts.scientific_observation_sha256",
            ],
            name="fk_roa_exact_observation",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "issuance_challenge_sha256",
                "quest_id",
                "scientific_slot_id",
                "authorization_sha256",
                "committed_validation_receipt_sha256",
                "validation_receipt_sha256",
            ],
            [
                "research_observation_issuance_challenges.challenge_sha256",
                "research_observation_issuance_challenges.quest_id",
                "research_observation_issuance_challenges.scientific_slot_id",
                "research_observation_issuance_challenges.authorization_sha256",
                "research_observation_issuance_challenges.committed_validation_receipt_sha256",
                "research_observation_issuance_challenges.validation_receipt_sha256",
            ],
            name="fk_roa_exact_challenge",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "quest_id",
                "incorporated_event_sequence",
                "incorporated_event_sha256",
                "incorporated_event_type",
            ],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
                "research_kernel_events.event_type",
            ],
            name="fk_roa_incorporated_event",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_roa_quest_committed", "quest_id", "committed_at"),
    )

    committed_admission_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scientific_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    authorization_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_validation_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issuance_challenge_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    admitted_observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    admission_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    incorporated_event_sequence: Mapped[int | None] = mapped_column(BigInteger)
    incorporated_event_sha256: Mapped[str | None] = mapped_column(String(64))
    incorporated_event_type: Mapped[str | None] = mapped_column(String(64))


class ResearchContinuationReceiptRecord(Base):
    """Recoverable graph-scoped continuation derived from one admitted observation."""

    __tablename__ = "research_continuation_receipts"
    __table_args__ = (
        CheckConstraint(
            _hashes(
                "receipt_sha256",
                "action_sha256",
                "world_model_snapshot_sha256",
                "observation_projection_sha256",
                "scientific_observation_sha256",
                "committed_admission_sha256",
            ),
            name="ck_rcr_hashes",
        ),
        CheckConstraint(_QUEST_SQL, name="ck_rcr_quest"),
        CheckConstraint(_SLOT_SQL, name="ck_rcr_slot"),
        CheckConstraint(
            "disposition IN ('ready','redesign_observable','hypothesis_set_fork_required')",
            name="ck_rcr_disposition",
        ),
        _postgresql_json_check(
            "jsonb_typeof(receipt_json) = 'object' AND "
            "receipt_json->>'schema_name' = "
            "'aletheia.graph_scoped_continuation_receipt' AND "
            "receipt_json->>'scientific_slot_id' = scientific_slot_id AND "
            "receipt_json->>'world_model_snapshot_sha256' = "
            "world_model_snapshot_sha256 AND "
            "receipt_json->>'observation_projection_sha256' = "
            "observation_projection_sha256 AND "
            "receipt_json->>'disposition' = disposition",
            name="ck_rcr_json",
        ),
        UniqueConstraint("scientific_slot_id", name="uq_rcr_slot"),
        UniqueConstraint("action_sha256", name="uq_rcr_action"),
        UniqueConstraint("observation_projection_sha256", name="uq_rcr_projection"),
        ForeignKeyConstraint(
            [
                "committed_admission_sha256",
                "quest_id",
                "scientific_slot_id",
                "scientific_observation_sha256",
            ],
            [
                "research_observation_admissions.committed_admission_sha256",
                "research_observation_admissions.quest_id",
                "research_observation_admissions.scientific_slot_id",
                "research_observation_admissions.admitted_observation_sha256",
            ],
            name="fk_rcr_exact_admission",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_rcr_quest_recorded", "quest_id", "recorded_at"),
    )

    receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action_sha256: Mapped[str] = mapped_column(
        ForeignKey("research_kernel_objects.object_sha256"), nullable=False
    )
    scientific_slot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    world_model_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scientific_observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_admission_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(48), nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


OBSERVATION_PERSISTENCE_TABLES = (
    ResearchControllerRegistrationRecord.__table__,
    ResearchControllerDeliveryRecord.__table__,
    ResearchControllerDeliveryAttemptRecord.__table__,
    ResearchControllerDeliveryResolutionRecord.__table__,
    ResearchProtocolCompilationRecord.__table__,
    ResearchScientificExecutionAuthorizationRecord.__table__,
    ResearchObservationIssuanceChallengeRecord.__table__,
    ResearchObservationValidationReceiptRecord.__table__,
    ResearchObservationAdmissionRecord.__table__,
    ResearchContinuationReceiptRecord.__table__,
)
