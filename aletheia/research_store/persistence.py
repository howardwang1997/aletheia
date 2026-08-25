"""SQLAlchemy metadata for the authoritative research event store.

The pure contracts and reducer live in :mod:`aletheia.research_kernel`.  This module is a
PostgreSQL adapter: it records immutable event/command authority and content-addressed object or
snapshot *metadata*.  Object and snapshot payload bytes remain in the CAS named by ``storage_key``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aletheia.db import Base

_SHA256_SQL = "~ '^[0-9a-f]{64}$'"
_QUEST_ID_SQL = "~ '^qst_[0-9a-f]{32}$'"
_OBJECT_ID_SQL = "~ '^[a-z][a-z0-9_:/.-]{2,127}$'"
_PRINCIPAL_ID_SQL = "~ '^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$'"


class ResearchQuestAuthorityRecord(Base):
    """Immutable namespace claim preventing legacy/new authority for one Quest id."""

    __tablename__ = "research_quest_authorities"
    __table_args__ = (
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_quest_authorities_quest_id",
        ),
        CheckConstraint(
            "authority_kind IN ('legacy_program_graph','research_kernel_v1')",
            name="ck_research_quest_authorities_kind",
        ),
        Index("ix_research_quest_authorities_kind", "authority_kind"),
    )

    quest_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    authority_kind: Mapped[str] = mapped_column(String(32))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchQuestStreamRecord(Base):
    """Mutable compare-and-swap head for exactly one Quest event stream."""

    __tablename__ = "research_quest_streams"
    __table_args__ = (
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_quest_streams_quest_id",
        ),
        CheckConstraint(
            "stream_version >= 0",
            name="ck_research_quest_streams_version",
        ),
        CheckConstraint(
            "reducer_version >= 1",
            name="ck_research_quest_streams_reducer_version",
        ),
        CheckConstraint(
            f"scope_binding_sha256 {_SHA256_SQL}",
            name="ck_research_quest_streams_scope_binding_sha256",
        ),
        CheckConstraint(
            f"authorization_policy_sha256 {_SHA256_SQL}",
            name="ck_research_quest_streams_authorization_policy_sha256",
        ),
        CheckConstraint(
            f"authorization_trust_root_sha256 {_SHA256_SQL}",
            name="ck_research_quest_streams_authorization_trust_root_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(authorization_policy_json) = 'object' AND "
            "authorization_policy_json->>'schema_name' IS NOT DISTINCT FROM "
            "'aletheia.research_authorization_policy' AND "
            "(authorization_policy_json->>'schema_version')::integer "
            "IS NOT DISTINCT FROM 1 AND "
            "authorization_policy_json->>'quest_id' IS NOT DISTINCT FROM quest_id AND "
            "authorization_policy_json->>'trust_root_sha256' "
            "IS NOT DISTINCT FROM authorization_trust_root_sha256",
            name="ck_research_quest_streams_authorization_policy_json",
        ),
        CheckConstraint(
            "(program_id IS NULL OR program_id ~ '^prg_[0-9a-f]{32}$') AND "
            "(campaign_id IS NULL OR campaign_id ~ '^cmp_[0-9a-f]{32}$') AND "
            "(campaign_id IS NULL OR program_id IS NOT NULL)",
            name="ck_research_quest_streams_scope_binding",
        ),
        CheckConstraint(
            "(stream_version = 0 AND tail_event_sha256 IS NULL) OR "
            f"(stream_version >= 1 AND tail_event_sha256 {_SHA256_SQL})",
            name="ck_research_quest_streams_tail",
        ),
        UniqueConstraint(
            "scope_binding_sha256",
            name="uq_research_quest_streams_scope_binding_sha256",
        ),
        UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            name="uq_research_quest_streams_scoped_binding",
        ),
        UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            "authorization_policy_sha256",
            name="uq_research_quest_streams_authority_binding",
        ),
        UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            "authorization_trust_root_sha256",
            "authorization_policy_sha256",
            name="uq_research_quest_streams_trusted_authority_binding",
        ),
        ForeignKeyConstraint(
            ["quest_id", "stream_version", "tail_event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_quest_streams_tail_event",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    quest_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    program_id: Mapped[str | None] = mapped_column(String(36), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    scope_binding_sha256: Mapped[str] = mapped_column(String(64))
    authorization_trust_root_sha256: Mapped[str] = mapped_column(String(64))
    authorization_policy_sha256: Mapped[str] = mapped_column(String(64))
    authorization_policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    stream_version: Mapped[int] = mapped_column(BigInteger, default=0)
    tail_event_sha256: Mapped[str | None] = mapped_column(String(64))
    reducer_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchKernelObjectRecord(Base):
    """Immutable CAS index for one typed kernel object; never stores object payload bytes."""

    __tablename__ = "research_kernel_objects"
    __table_args__ = (
        CheckConstraint(
            f"object_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_objects_sha256",
        ),
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_kernel_objects_quest_id",
        ),
        CheckConstraint(
            f"object_id {_OBJECT_ID_SQL}",
            name="ck_research_kernel_objects_object_id",
        ),
        CheckConstraint(
            "object_kind IN ('charter','opportunity','problem','question','action')",
            name="ck_research_kernel_objects_kind",
        ),
        CheckConstraint(
            "object_version >= 1 AND object_schema_version >= 1 AND object_size_bytes > 0",
            name="ck_research_kernel_objects_versions_size",
        ),
        CheckConstraint(
            "storage_key = 'sha256/' || substring(object_sha256 from 1 for 2) || '/' "
            "|| object_sha256",
            name="ck_research_kernel_objects_storage_key",
        ),
        UniqueConstraint(
            "quest_id",
            "object_kind",
            "object_id",
            "object_version",
            name="uq_research_kernel_objects_logical_version",
        ),
        UniqueConstraint(
            "quest_id",
            "object_sha256",
            "object_kind",
            "object_id",
            name="uq_research_kernel_objects_scoped_identity",
        ),
        ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_objects_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    object_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), index=True)
    object_kind: Mapped[str] = mapped_column(String(24), index=True)
    object_id: Mapped[str] = mapped_column(String(128))
    object_version: Mapped[int] = mapped_column(Integer)
    object_schema_name: Mapped[str] = mapped_column(String(128))
    object_schema_version: Mapped[int] = mapped_column(Integer)
    canonicalization: Mapped[str] = mapped_column(String(48))
    media_type: Mapped[str] = mapped_column(String(128))
    object_size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(80))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchKernelCommandReceiptRecord(Base):
    """Immutable idempotency receipt for one committed command transaction."""

    __tablename__ = "research_kernel_command_receipts"
    __table_args__ = (
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_kernel_command_receipts_quest_id",
        ),
        CheckConstraint(
            f"command_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_command_sha256",
        ),
        CheckConstraint(
            f"authorization_receipt_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_authorization_sha256",
        ),
        CheckConstraint(
            f"scope_binding_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_scope_binding_sha256",
        ),
        CheckConstraint(
            f"authorization_policy_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_authorization_policy_sha256",
        ),
        CheckConstraint(
            f"authorization_trust_root_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_trust_root_sha256",
        ),
        CheckConstraint(
            f"principal_id {_PRINCIPAL_ID_SQL}",
            name="ck_research_kernel_command_receipts_principal_id",
        ),
        CheckConstraint(
            "command_id ~ '^rkc_[0-9a-f]{32}$' AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' AND "
            "(source_event_key IS NULL OR "
            "source_event_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')",
            name="ck_research_kernel_command_receipts_keys",
        ),
        CheckConstraint(
            "expected_stream_version >= 0 AND result_stream_version = expected_stream_version + 1",
            name="ck_research_kernel_command_receipts_versions",
        ),
        CheckConstraint(
            "(expected_stream_version = 0 AND expected_tail_event_sha256 IS NULL) OR "
            "(expected_stream_version >= 1 AND "
            f"expected_tail_event_sha256 {_SHA256_SQL})",
            name="ck_research_kernel_command_receipts_expected_tail",
        ),
        CheckConstraint(
            f"result_event_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_command_receipts_event_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(command_json) = 'object'",
            name="ck_research_kernel_command_receipts_json",
        ),
        CheckConstraint(
            "command_json->>'quest_id' IS NOT DISTINCT FROM quest_id "
            "AND (command_json->>'expected_stream_version')::bigint "
            "IS NOT DISTINCT FROM expected_stream_version "
            "AND command_json->>'idempotency_key' IS NOT DISTINCT FROM idempotency_key "
            "AND command_json->>'source_event_key' IS NOT DISTINCT FROM source_event_key "
            "AND command_json->>'expected_tail_event_sha256' "
            "IS NOT DISTINCT FROM expected_tail_event_sha256 "
            "AND command_json->>'principal_id' IS NOT DISTINCT FROM principal_id "
            "AND command_json->>'authorization_receipt_sha256' IS NOT DISTINCT FROM "
            "authorization_receipt_sha256 "
            "AND command_json->>'authorization_policy_sha256' IS NOT DISTINCT FROM "
            "authorization_policy_sha256 "
            "AND command_json->>'authorization_trust_root_sha256' IS NOT DISTINCT FROM "
            "authorization_trust_root_sha256 "
            "AND command_json #>> '{scope_binding,quest_id}' IS NOT DISTINCT FROM quest_id "
            "AND (command_json->>'authorized_at')::timestamptz "
            "IS NOT DISTINCT FROM submitted_at",
            name="ck_research_kernel_command_receipts_json_bindings",
        ),
        CheckConstraint(
            "submitted_at <= committed_at",
            name="ck_research_kernel_command_receipts_times",
        ),
        UniqueConstraint(
            "quest_id",
            "idempotency_key",
            name="uq_research_kernel_command_receipts_idempotency",
        ),
        UniqueConstraint(
            "quest_id",
            "command_sha256",
            name="uq_research_kernel_command_receipts_command_sha256",
        ),
        UniqueConstraint(
            "quest_id",
            "command_id",
            name="uq_research_kernel_command_receipts_scoped_command",
        ),
        UniqueConstraint(
            "quest_id",
            "command_id",
            "expected_stream_version",
            "expected_tail_event_sha256",
            name="uq_research_kernel_command_receipts_expected_parent",
        ),
        UniqueConstraint(
            "quest_id",
            "result_stream_version",
            "result_event_sha256",
            name="uq_research_kernel_command_receipts_result_event",
        ),
        UniqueConstraint(
            "quest_id",
            "command_id",
            "command_sha256",
            "principal_id",
            "authorization_receipt_sha256",
            "result_stream_version",
            "result_event_sha256",
            "committed_at",
            name="uq_research_kernel_command_receipts_event_authority",
        ),
        ForeignKeyConstraint(
            [
                "quest_id",
                "scope_binding_sha256",
                "authorization_trust_root_sha256",
                "authorization_policy_sha256",
            ],
            [
                "research_quest_streams.quest_id",
                "research_quest_streams.scope_binding_sha256",
                "research_quest_streams.authorization_trust_root_sha256",
                "research_quest_streams.authorization_policy_sha256",
            ],
            name="fk_research_kernel_command_receipts_trusted_authority_binding",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["quest_id", "expected_stream_version", "expected_tail_event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_kernel_command_receipts_expected_parent",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["quest_id", "result_stream_version", "result_event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_kernel_command_receipts_result_event",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index(
            "uq_research_kernel_command_receipts_source_event",
            "quest_id",
            "source_event_key",
            unique=True,
            postgresql_where=text("source_event_key IS NOT NULL"),
        ),
    )

    command_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(192))
    source_event_key: Mapped[str | None] = mapped_column(String(192))
    command_sha256: Mapped[str] = mapped_column(String(64))
    command_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    scope_binding_sha256: Mapped[str] = mapped_column(String(64))
    authorization_trust_root_sha256: Mapped[str] = mapped_column(String(64))
    authorization_policy_sha256: Mapped[str] = mapped_column(String(64))
    expected_stream_version: Mapped[int] = mapped_column(BigInteger)
    expected_tail_event_sha256: Mapped[str | None] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(String(128), index=True)
    authorization_receipt_sha256: Mapped[str] = mapped_column(String(64))
    result_stream_version: Mapped[int] = mapped_column(BigInteger)
    result_event_sha256: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ResearchKernelEventRecord(Base):
    """One immutable, typed event in a Quest-local linear hash chain."""

    __tablename__ = "research_kernel_events"
    __table_args__ = (
        CheckConstraint(
            f"event_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_events_sha256",
        ),
        CheckConstraint(
            "event_id = 'rke_' || substring(event_sha256 from 1 for 32)",
            name="ck_research_kernel_events_event_id",
        ),
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_kernel_events_quest_id",
        ),
        CheckConstraint(
            "sequence >= 1 AND event_schema_version >= 1 AND reducer_version >= 1",
            name="ck_research_kernel_events_versions",
        ),
        CheckConstraint(
            "event_type IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed','action_authorized',"
            "'action_rejected','action_superseded','observation_incorporated',"
            "'continue_committed','activate_committed',"
            "'refine_committed','fork_committed','backtrack_committed','pause_committed',"
            "'stop_committed')",
            name="ck_research_kernel_events_type",
        ),
        CheckConstraint(
            "(sequence = 1 AND parent_sequence IS NULL AND parent_event_sha256 IS NULL) OR "
            "(sequence > 1 AND parent_sequence = sequence - 1 AND "
            f"parent_event_sha256 {_SHA256_SQL})",
            name="ck_research_kernel_events_parent",
        ),
        CheckConstraint(
            f"command_sha256 {_SHA256_SQL} AND "
            f"authorization_receipt_sha256 {_SHA256_SQL} AND "
            f"principal_id {_PRINCIPAL_ID_SQL}",
            name="ck_research_kernel_events_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(event_json) = 'object'",
            name="ck_research_kernel_events_json",
        ),
        CheckConstraint(
            "event_json->>'quest_id' IS NOT DISTINCT FROM quest_id "
            "AND (event_json->>'sequence')::bigint IS NOT DISTINCT FROM sequence "
            "AND event_json->>'parent_event_sha256' IS NOT DISTINCT FROM parent_event_sha256 "
            "AND event_json->>'event_type' IS NOT DISTINCT FROM event_type "
            "AND (event_json->>'event_schema_version')::integer "
            "IS NOT DISTINCT FROM event_schema_version "
            "AND (event_json->>'reducer_version')::integer "
            "IS NOT DISTINCT FROM reducer_version "
            "AND event_json->>'command_sha256' IS NOT DISTINCT FROM command_sha256 "
            "AND event_json->>'principal_id' IS NOT DISTINCT FROM principal_id "
            "AND event_json->>'authorization_receipt_sha256' IS NOT DISTINCT FROM "
            "authorization_receipt_sha256 "
            "AND (event_json->>'committed_at')::timestamptz "
            "IS NOT DISTINCT FROM committed_at",
            name="ck_research_kernel_events_json_bindings",
        ),
        CheckConstraint(
            "COALESCE(event_json #>> '{payload,charter_ref,object_sha256}', "
            "event_json #>> '{payload,opportunity_ref,object_sha256}', "
            "event_json #>> '{payload,problem_ref,object_sha256}', "
            "event_json #>> '{payload,question_ref,object_sha256}', "
            "event_json #>> '{payload,action_ref,object_sha256}') "
            "IS NOT DISTINCT FROM admitted_object_sha256 AND "
            "COALESCE(event_json #>> '{payload,charter_ref,object_kind}', "
            "event_json #>> '{payload,opportunity_ref,object_kind}', "
            "event_json #>> '{payload,problem_ref,object_kind}', "
            "event_json #>> '{payload,question_ref,object_kind}', "
            "event_json #>> '{payload,action_ref,object_kind}') "
            "IS NOT DISTINCT FROM admitted_object_kind AND "
            "COALESCE(event_json #>> '{payload,charter_ref,object_id}', "
            "event_json #>> '{payload,opportunity_ref,object_id}', "
            "event_json #>> '{payload,problem_ref,object_id}', "
            "event_json #>> '{payload,question_ref,object_id}', "
            "event_json #>> '{payload,action_ref,object_id}') "
            "IS NOT DISTINCT FROM admitted_object_id",
            name="ck_research_kernel_events_json_admitted_object",
        ),
        CheckConstraint(
            "((admitted_object_sha256 IS NULL)::integer + "
            "(admitted_object_kind IS NULL)::integer + "
            "(admitted_object_id IS NULL)::integer) IN (0, 3)",
            name="ck_research_kernel_events_admitted_object_complete",
        ),
        CheckConstraint(
            "(event_type IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed') "
            "AND admitted_object_sha256 IS NOT NULL) OR "
            "(event_type NOT IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed') "
            "AND admitted_object_sha256 IS NULL)",
            name="ck_research_kernel_events_admitted_object_event",
        ),
        CheckConstraint(
            "(event_type IN ('charter_activated','charter_revised') "
            "AND admitted_object_kind = 'charter') OR "
            "(event_type = 'opportunity_recorded' AND admitted_object_kind = 'opportunity') OR "
            "(event_type = 'problem_admitted' AND admitted_object_kind = 'problem') OR "
            "(event_type = 'question_admitted' AND admitted_object_kind = 'question') OR "
            "(event_type = 'action_proposed' AND admitted_object_kind = 'action') OR "
            "admitted_object_kind IS NULL",
            name="ck_research_kernel_events_admitted_object_kind",
        ),
        UniqueConstraint("event_id", name="uq_research_kernel_events_event_id"),
        UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_events_quest_sequence",
        ),
        UniqueConstraint(
            "quest_id",
            "sequence",
            "event_sha256",
            name="uq_research_kernel_events_scoped_sequence_sha256",
        ),
        UniqueConstraint(
            "quest_id",
            "sequence",
            "event_sha256",
            "event_type",
            name="uq_rke_scoped_typed_event",
        ),
        UniqueConstraint(
            "quest_id",
            "event_sha256",
            name="uq_research_kernel_events_scoped_sha256",
        ),
        UniqueConstraint("command_id", name="uq_research_kernel_events_command"),
        ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_events_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["quest_id", "parent_sequence", "parent_event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_kernel_events_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "quest_id",
                "command_id",
                "command_sha256",
                "principal_id",
                "authorization_receipt_sha256",
                "sequence",
                "event_sha256",
                "committed_at",
            ],
            [
                "research_kernel_command_receipts.quest_id",
                "research_kernel_command_receipts.command_id",
                "research_kernel_command_receipts.command_sha256",
                "research_kernel_command_receipts.principal_id",
                "research_kernel_command_receipts.authorization_receipt_sha256",
                "research_kernel_command_receipts.result_stream_version",
                "research_kernel_command_receipts.result_event_sha256",
                "research_kernel_command_receipts.committed_at",
            ],
            name="fk_research_kernel_events_command",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["quest_id", "command_id", "parent_sequence", "parent_event_sha256"],
            [
                "research_kernel_command_receipts.quest_id",
                "research_kernel_command_receipts.command_id",
                "research_kernel_command_receipts.expected_stream_version",
                "research_kernel_command_receipts.expected_tail_event_sha256",
            ],
            name="fk_research_kernel_events_command_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "quest_id",
                "admitted_object_sha256",
                "admitted_object_kind",
                "admitted_object_id",
            ],
            [
                "research_kernel_objects.quest_id",
                "research_kernel_objects.object_sha256",
                "research_kernel_objects.object_kind",
                "research_kernel_objects.object_id",
            ],
            name="fk_research_kernel_events_admitted_object",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_research_kernel_events_committed_at", "committed_at"),
        Index("ix_research_kernel_events_type", "event_type"),
        Index(
            "uq_research_kernel_events_admitted_object",
            "quest_id",
            "admitted_object_sha256",
            unique=True,
            postgresql_where=text("admitted_object_sha256 IS NOT NULL"),
        ),
    )

    event_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36))
    quest_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(BigInteger)
    parent_sequence: Mapped[int | None] = mapped_column(BigInteger)
    parent_event_sha256: Mapped[str | None] = mapped_column(String(64))
    event_schema_version: Mapped[int] = mapped_column(Integer)
    reducer_version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(String(96))
    command_sha256: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(String(128))
    authorization_receipt_sha256: Mapped[str] = mapped_column(String(64))
    admitted_object_sha256: Mapped[str | None] = mapped_column(String(64))
    admitted_object_kind: Mapped[str | None] = mapped_column(String(24))
    admitted_object_id: Mapped[str | None] = mapped_column(String(128))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchKernelSnapshotRecord(Base):
    """Immutable CAS metadata for a replay-derived graph snapshot, never snapshot bytes."""

    __tablename__ = "research_kernel_snapshots"
    __table_args__ = (
        CheckConstraint(
            f"snapshot_sha256 {_SHA256_SQL} AND tail_event_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_snapshots_hashes",
        ),
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL}",
            name="ck_research_kernel_snapshots_quest_id",
        ),
        CheckConstraint(
            "sequence >= 1 AND snapshot_schema_version >= 1 AND reducer_version >= 1 "
            "AND snapshot_size_bytes > 0",
            name="ck_research_kernel_snapshots_versions_size",
        ),
        CheckConstraint(
            "storage_key = 'sha256/' || substring(snapshot_sha256 from 1 for 2) || '/' "
            "|| snapshot_sha256",
            name="ck_research_kernel_snapshots_storage_key",
        ),
        UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_snapshots_quest_sequence",
        ),
        UniqueConstraint(
            "quest_id",
            "snapshot_sha256",
            name="uq_research_kernel_snapshots_scoped_sha256",
        ),
        ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_snapshots_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["quest_id", "sequence", "tail_event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_kernel_snapshots_tail_event",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_research_kernel_snapshots_created_at", "created_at"),
    )

    snapshot_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(BigInteger)
    tail_event_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_schema_version: Mapped[int] = mapped_column(Integer)
    reducer_version: Mapped[int] = mapped_column(Integer)
    canonicalization: Mapped[str] = mapped_column(String(48))
    media_type: Mapped[str] = mapped_column(String(128))
    snapshot_size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchKernelOutboxRecord(Base):
    """Transactional event-delivery intent with mutable at-least-once delivery state."""

    __tablename__ = "research_kernel_outbox"
    __table_args__ = (
        CheckConstraint(
            f"quest_id {_QUEST_ID_SQL} AND event_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL}",
            name="ck_research_kernel_outbox_hashes_scope",
        ),
        CheckConstraint(
            "payload_sha256 = event_sha256",
            name="ck_research_kernel_outbox_event_payload",
        ),
        CheckConstraint(
            "outbox_id = 'rko_' || substring(event_sha256 from 1 for 32)",
            name="ck_research_kernel_outbox_id",
        ),
        CheckConstraint(
            "topic = 'research_kernel.event.v1' AND "
            "delivery_key = quest_id || ':' || sequence::text",
            name="ck_research_kernel_outbox_routing",
        ),
        CheckConstraint(
            "sequence >= 1 AND delivery_attempts >= 0",
            name="ck_research_kernel_outbox_counts",
        ),
        CheckConstraint(
            "delivery_status IN ('pending','delivering','published')",
            name="ck_research_kernel_outbox_status",
        ),
        CheckConstraint(
            "(delivery_status = 'published' AND published_at IS NOT NULL) OR "
            "(delivery_status <> 'published' AND published_at IS NULL)",
            name="ck_research_kernel_outbox_published_at",
        ),
        UniqueConstraint("event_sha256", name="uq_research_kernel_outbox_event"),
        UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_outbox_quest_sequence",
        ),
        UniqueConstraint(
            "outbox_id",
            "quest_id",
            "sequence",
            "event_sha256",
            name="uq_rko_exact_controller_source",
        ),
        UniqueConstraint(
            "topic",
            "delivery_key",
            name="uq_research_kernel_outbox_delivery_key",
        ),
        ForeignKeyConstraint(
            ["quest_id", "sequence", "event_sha256"],
            [
                "research_kernel_events.quest_id",
                "research_kernel_events.sequence",
                "research_kernel_events.event_sha256",
            ],
            name="fk_research_kernel_outbox_event",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_research_kernel_outbox_delivery",
            "delivery_status",
            "available_at",
            "created_at",
        ),
    )

    outbox_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_sha256: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(128))
    delivery_key: Mapped[str] = mapped_column(String(192))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
