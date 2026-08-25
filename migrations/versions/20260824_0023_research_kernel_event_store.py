"""Add the authoritative PR-2 research-kernel event store.

Revision ID: 20260824_0023
Revises: 20260818_0022
Create Date: 2026-08-24

Quest stream content remains isolated from the legacy Program graph.  A tiny immutable namespace
claim is shared by both stores solely to prevent the same ``qst_*`` identity from acquiring two
scientific authorities.  The immutable compatibility scope on each new stream is supplied by the
pure kernel command contract; Program/Campaign binding cannot be changed by advancing a stream.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260824_0023"
down_revision: str | None = "20260818_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_quest_authorities",
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("authority_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_quest_authorities_quest_id",
        ),
        sa.CheckConstraint(
            "authority_kind IN ('legacy_program_graph','research_kernel_v1')",
            name="ck_research_quest_authorities_kind",
        ),
        sa.PrimaryKeyConstraint("quest_id"),
    )
    op.create_index(
        "ix_research_quest_authorities_kind",
        "research_quest_authorities",
        ["authority_kind"],
        unique=False,
    )
    # Block concurrent legacy Quest inserts from the backfill until the legacy claim trigger is
    # installed later in this same transactional migration.  Without this lock, a Quest inserted
    # between the SELECT and CREATE TRIGGER could remain unclaimed and later acquire kernel
    # authority under the same identity.
    op.execute("LOCK TABLE research_graph_nodes IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        INSERT INTO research_quest_authorities (quest_id, authority_kind)
        SELECT node_id, 'legacy_program_graph'
        FROM research_graph_nodes
        WHERE node_type = 'quest'
        ON CONFLICT (quest_id) DO NOTHING
        """
    )

    op.create_table(
        "research_quest_streams",
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("program_id", sa.String(length=36), nullable=True),
        sa.Column("campaign_id", sa.String(length=36), nullable=True),
        sa.Column("scope_binding_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_trust_root_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "authorization_policy_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("stream_version", sa.BigInteger(), nullable=False),
        sa.Column("tail_event_sha256", sa.String(length=64), nullable=True),
        sa.Column("reducer_version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_quest_streams_quest_id",
        ),
        sa.CheckConstraint(
            "stream_version >= 0",
            name="ck_research_quest_streams_version",
        ),
        sa.CheckConstraint(
            "reducer_version >= 1",
            name="ck_research_quest_streams_reducer_version",
        ),
        sa.CheckConstraint(
            "scope_binding_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_quest_streams_scope_binding_sha256",
        ),
        sa.CheckConstraint(
            "authorization_policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_quest_streams_authorization_policy_sha256",
        ),
        sa.CheckConstraint(
            "authorization_trust_root_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_quest_streams_authorization_trust_root_sha256",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "(program_id IS NULL OR program_id ~ '^prg_[0-9a-f]{32}$') AND "
            "(campaign_id IS NULL OR campaign_id ~ '^cmp_[0-9a-f]{32}$') AND "
            "(campaign_id IS NULL OR program_id IS NOT NULL)",
            name="ck_research_quest_streams_scope_binding",
        ),
        sa.CheckConstraint(
            "(stream_version = 0 AND tail_event_sha256 IS NULL) OR "
            "(stream_version >= 1 AND tail_event_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_research_quest_streams_tail",
        ),
        sa.PrimaryKeyConstraint("quest_id"),
        sa.UniqueConstraint(
            "scope_binding_sha256",
            name="uq_research_quest_streams_scope_binding_sha256",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            name="uq_research_quest_streams_scoped_binding",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            "authorization_policy_sha256",
            name="uq_research_quest_streams_authority_binding",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "scope_binding_sha256",
            "authorization_trust_root_sha256",
            "authorization_policy_sha256",
            name="uq_research_quest_streams_trusted_authority_binding",
        ),
    )
    op.create_index(
        op.f("ix_research_quest_streams_program_id"),
        "research_quest_streams",
        ["program_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_quest_streams_campaign_id"),
        "research_quest_streams",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_quest_streams_updated_at"),
        "research_quest_streams",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "research_kernel_objects",
        sa.Column("object_sha256", sa.String(length=64), nullable=False),
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("object_kind", sa.String(length=24), nullable=False),
        sa.Column("object_id", sa.String(length=128), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.Column("object_schema_name", sa.String(length=128), nullable=False),
        sa.Column("object_schema_version", sa.Integer(), nullable=False),
        sa.Column("canonicalization", sa.String(length=48), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("object_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "object_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_objects_sha256",
        ),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_kernel_objects_quest_id",
        ),
        sa.CheckConstraint(
            "object_id ~ '^[a-z][a-z0-9_:/.-]{2,127}$'",
            name="ck_research_kernel_objects_object_id",
        ),
        sa.CheckConstraint(
            "object_kind IN ('charter','opportunity','problem','question','action')",
            name="ck_research_kernel_objects_kind",
        ),
        sa.CheckConstraint(
            "object_version >= 1 AND object_schema_version >= 1 AND object_size_bytes > 0",
            name="ck_research_kernel_objects_versions_size",
        ),
        sa.CheckConstraint(
            "storage_key = 'sha256/' || substring(object_sha256 from 1 for 2) || '/' "
            "|| object_sha256",
            name="ck_research_kernel_objects_storage_key",
        ),
        sa.ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_objects_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("object_sha256"),
        sa.UniqueConstraint(
            "quest_id",
            "object_kind",
            "object_id",
            "object_version",
            name="uq_research_kernel_objects_logical_version",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "object_sha256",
            "object_kind",
            "object_id",
            name="uq_research_kernel_objects_scoped_identity",
        ),
    )
    op.create_index(
        op.f("ix_research_kernel_objects_quest_id"),
        "research_kernel_objects",
        ["quest_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_kernel_objects_object_kind"),
        "research_kernel_objects",
        ["object_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_kernel_objects_registered_at"),
        "research_kernel_objects",
        ["registered_at"],
        unique=False,
    )

    op.create_table(
        "research_kernel_command_receipts",
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=192), nullable=False),
        sa.Column("source_event_key", sa.String(length=192), nullable=True),
        sa.Column("command_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "command_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("scope_binding_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_trust_root_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_stream_version", sa.BigInteger(), nullable=False),
        sa.Column("expected_tail_event_sha256", sa.String(length=64), nullable=True),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("authorization_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_stream_version", sa.BigInteger(), nullable=False),
        sa.Column("result_event_sha256", sa.String(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_kernel_command_receipts_quest_id",
        ),
        sa.CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_command_sha256",
        ),
        sa.CheckConstraint(
            "authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_authorization_sha256",
        ),
        sa.CheckConstraint(
            "scope_binding_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_scope_binding_sha256",
        ),
        sa.CheckConstraint(
            "authorization_policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_authorization_policy_sha256",
        ),
        sa.CheckConstraint(
            "authorization_trust_root_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_trust_root_sha256",
        ),
        sa.CheckConstraint(
            "principal_id ~ '^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$'",
            name="ck_research_kernel_command_receipts_principal_id",
        ),
        sa.CheckConstraint(
            "command_id ~ '^rkc_[0-9a-f]{32}$' AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' AND "
            "(source_event_key IS NULL OR "
            "source_event_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')",
            name="ck_research_kernel_command_receipts_keys",
        ),
        sa.CheckConstraint(
            "expected_stream_version >= 0 AND result_stream_version = expected_stream_version + 1",
            name="ck_research_kernel_command_receipts_versions",
        ),
        sa.CheckConstraint(
            "(expected_stream_version = 0 AND expected_tail_event_sha256 IS NULL) OR "
            "(expected_stream_version >= 1 AND "
            "expected_tail_event_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_research_kernel_command_receipts_expected_tail",
        ),
        sa.CheckConstraint(
            "result_event_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_command_receipts_event_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(command_json) = 'object'",
            name="ck_research_kernel_command_receipts_json",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "submitted_at <= committed_at",
            name="ck_research_kernel_command_receipts_times",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint(
            "quest_id",
            "idempotency_key",
            name="uq_research_kernel_command_receipts_idempotency",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "command_sha256",
            name="uq_research_kernel_command_receipts_command_sha256",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "command_id",
            name="uq_research_kernel_command_receipts_scoped_command",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "command_id",
            "expected_stream_version",
            "expected_tail_event_sha256",
            name="uq_research_kernel_command_receipts_expected_parent",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "result_stream_version",
            "result_event_sha256",
            name="uq_research_kernel_command_receipts_result_event",
        ),
        sa.UniqueConstraint(
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
    )
    op.create_index(
        op.f("ix_research_kernel_command_receipts_quest_id"),
        "research_kernel_command_receipts",
        ["quest_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_kernel_command_receipts_principal_id"),
        "research_kernel_command_receipts",
        ["principal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_kernel_command_receipts_submitted_at"),
        "research_kernel_command_receipts",
        ["submitted_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_kernel_command_receipts_committed_at"),
        "research_kernel_command_receipts",
        ["committed_at"],
        unique=False,
    )
    op.create_index(
        "uq_research_kernel_command_receipts_source_event",
        "research_kernel_command_receipts",
        ["quest_id", "source_event_key"],
        unique=True,
        postgresql_where=sa.text("source_event_key IS NOT NULL"),
    )

    op.create_table(
        "research_kernel_events",
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("parent_sequence", sa.BigInteger(), nullable=True),
        sa.Column("parent_event_sha256", sa.String(length=64), nullable=True),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("reducer_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "event_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("command_id", sa.String(length=96), nullable=False),
        sa.Column("command_sha256", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("authorization_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("admitted_object_sha256", sa.String(length=64), nullable=True),
        sa.Column("admitted_object_kind", sa.String(length=24), nullable=True),
        sa.Column("admitted_object_id", sa.String(length=128), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_events_sha256",
        ),
        sa.CheckConstraint(
            "event_id = 'rke_' || substring(event_sha256 from 1 for 32)",
            name="ck_research_kernel_events_event_id",
        ),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_kernel_events_quest_id",
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND event_schema_version >= 1 AND reducer_version >= 1",
            name="ck_research_kernel_events_versions",
        ),
        sa.CheckConstraint(
            "event_type IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed','action_authorized',"
            "'action_rejected','action_superseded','continue_committed','activate_committed',"
            "'refine_committed','fork_committed','backtrack_committed','pause_committed',"
            "'stop_committed')",
            name="ck_research_kernel_events_type",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND parent_sequence IS NULL AND parent_event_sha256 IS NULL) OR "
            "(sequence > 1 AND parent_sequence = sequence - 1 AND "
            "parent_event_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_research_kernel_events_parent",
        ),
        sa.CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$' AND "
            "authorization_receipt_sha256 ~ '^[0-9a-f]{64}$' AND "
            "principal_id ~ '^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$'",
            name="ck_research_kernel_events_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(event_json) = 'object'",
            name="ck_research_kernel_events_json",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "((admitted_object_sha256 IS NULL)::integer + "
            "(admitted_object_kind IS NULL)::integer + "
            "(admitted_object_id IS NULL)::integer) IN (0, 3)",
            name="ck_research_kernel_events_admitted_object_complete",
        ),
        sa.CheckConstraint(
            "(event_type IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed') "
            "AND admitted_object_sha256 IS NOT NULL) OR "
            "(event_type NOT IN ('charter_activated','charter_revised','opportunity_recorded',"
            "'problem_admitted','question_admitted','action_proposed') "
            "AND admitted_object_sha256 IS NULL)",
            name="ck_research_kernel_events_admitted_object_event",
        ),
        sa.CheckConstraint(
            "(event_type IN ('charter_activated','charter_revised') "
            "AND admitted_object_kind = 'charter') OR "
            "(event_type = 'opportunity_recorded' AND admitted_object_kind = 'opportunity') OR "
            "(event_type = 'problem_admitted' AND admitted_object_kind = 'problem') OR "
            "(event_type = 'question_admitted' AND admitted_object_kind = 'question') OR "
            "(event_type = 'action_proposed' AND admitted_object_kind = 'action') OR "
            "admitted_object_kind IS NULL",
            name="ck_research_kernel_events_admitted_object_kind",
        ),
        sa.ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_events_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("event_sha256"),
        sa.UniqueConstraint("event_id", name="uq_research_kernel_events_event_id"),
        sa.UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_events_quest_sequence",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "sequence",
            "event_sha256",
            name="uq_research_kernel_events_scoped_sequence_sha256",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "event_sha256",
            name="uq_research_kernel_events_scoped_sha256",
        ),
        sa.UniqueConstraint("command_id", name="uq_research_kernel_events_command"),
    )
    op.create_index(
        "ix_research_kernel_events_committed_at",
        "research_kernel_events",
        ["committed_at"],
        unique=False,
    )
    op.create_index(
        "ix_research_kernel_events_type",
        "research_kernel_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "uq_research_kernel_events_admitted_object",
        "research_kernel_events",
        ["quest_id", "admitted_object_sha256"],
        unique=True,
        postgresql_where=sa.text("admitted_object_sha256 IS NOT NULL"),
    )

    op.create_table(
        "research_kernel_snapshots",
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("tail_event_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("reducer_version", sa.Integer(), nullable=False),
        sa.Column("canonicalization", sa.String(length=48), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("snapshot_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$' AND tail_event_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_snapshots_hashes",
        ),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$'",
            name="ck_research_kernel_snapshots_quest_id",
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND snapshot_schema_version >= 1 AND reducer_version >= 1 "
            "AND snapshot_size_bytes > 0",
            name="ck_research_kernel_snapshots_versions_size",
        ),
        sa.CheckConstraint(
            "storage_key = 'sha256/' || substring(snapshot_sha256 from 1 for 2) || '/' "
            "|| snapshot_sha256",
            name="ck_research_kernel_snapshots_storage_key",
        ),
        sa.ForeignKeyConstraint(
            ["quest_id"],
            ["research_quest_streams.quest_id"],
            name="fk_research_kernel_snapshots_quest",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("snapshot_sha256"),
        sa.UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_snapshots_quest_sequence",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "snapshot_sha256",
            name="uq_research_kernel_snapshots_scoped_sha256",
        ),
    )
    op.create_index(
        "ix_research_kernel_snapshots_created_at",
        "research_kernel_snapshots",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "research_kernel_outbox",
        sa.Column("outbox_id", sa.String(length=96), nullable=False),
        sa.Column("quest_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("delivery_key", sa.String(length=192), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quest_id ~ '^qst_[0-9a-f]{32}$' "
            "AND event_sha256 ~ '^[0-9a-f]{64}$' "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_kernel_outbox_hashes_scope",
        ),
        sa.CheckConstraint(
            "payload_sha256 = event_sha256",
            name="ck_research_kernel_outbox_event_payload",
        ),
        sa.CheckConstraint(
            "outbox_id = 'rko_' || substring(event_sha256 from 1 for 32)",
            name="ck_research_kernel_outbox_id",
        ),
        sa.CheckConstraint(
            "topic = 'research_kernel.event.v1' AND "
            "delivery_key = quest_id || ':' || sequence::text",
            name="ck_research_kernel_outbox_routing",
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND delivery_attempts >= 0",
            name="ck_research_kernel_outbox_counts",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending','delivering','published')",
            name="ck_research_kernel_outbox_status",
        ),
        sa.CheckConstraint(
            "(delivery_status = 'published' AND published_at IS NOT NULL) OR "
            "(delivery_status <> 'published' AND published_at IS NULL)",
            name="ck_research_kernel_outbox_published_at",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint(
            "event_sha256",
            name="uq_research_kernel_outbox_event",
        ),
        sa.UniqueConstraint(
            "quest_id",
            "sequence",
            name="uq_research_kernel_outbox_quest_sequence",
        ),
        sa.UniqueConstraint(
            "topic",
            "delivery_key",
            name="uq_research_kernel_outbox_delivery_key",
        ),
    )
    op.create_index(
        op.f("ix_research_kernel_outbox_available_at"),
        "research_kernel_outbox",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        "ix_research_kernel_outbox_delivery",
        "research_kernel_outbox",
        ["delivery_status", "available_at", "created_at"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_research_quest_streams_tail_event",
        "research_quest_streams",
        "research_kernel_events",
        ["quest_id", "stream_version", "tail_event_sha256"],
        ["quest_id", "sequence", "event_sha256"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_research_kernel_command_receipts_expected_parent",
        "research_kernel_command_receipts",
        "research_kernel_events",
        ["quest_id", "expected_stream_version", "expected_tail_event_sha256"],
        ["quest_id", "sequence", "event_sha256"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_research_kernel_command_receipts_result_event",
        "research_kernel_command_receipts",
        "research_kernel_events",
        ["quest_id", "result_stream_version", "result_event_sha256"],
        ["quest_id", "sequence", "event_sha256"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_claim_research_quest_authority()
        RETURNS trigger AS $$
        DECLARE
          desired_authority text;
          observed_authority text;
        BEGIN
          IF TG_TABLE_NAME = 'research_graph_nodes' THEN
            IF NEW.node_type <> 'quest' THEN
              RETURN NEW;
            END IF;
            desired_authority := 'legacy_program_graph';
          ELSE
            desired_authority := 'research_kernel_v1';
          END IF;

          INSERT INTO research_quest_authorities (quest_id, authority_kind)
          VALUES (NEW.quest_id, desired_authority)
          ON CONFLICT (quest_id) DO NOTHING;

          SELECT authority_kind INTO observed_authority
          FROM research_quest_authorities
          WHERE quest_id = NEW.quest_id
          FOR UPDATE;
          IF observed_authority IS DISTINCT FROM desired_authority THEN
            RAISE EXCEPTION
              'Quest % is already claimed by % authority, not %',
              NEW.quest_id, observed_authority, desired_authority;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_kernel_quest_authority_claim
        BEFORE INSERT ON research_quest_streams
        FOR EACH ROW EXECUTE FUNCTION aletheia_claim_research_quest_authority()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_legacy_program_quest_authority_claim
        BEFORE INSERT ON research_graph_nodes
        FOR EACH ROW EXECUTE FUNCTION aletheia_claim_research_quest_authority()
        """
    )
    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_quest_authority()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.authority_kind = 'legacy_program_graph' THEN
            IF NOT EXISTS (
              SELECT 1 FROM research_graph_nodes
              WHERE node_id = NEW.quest_id
                AND quest_id = NEW.quest_id
                AND node_type = 'quest'
            ) THEN
              RAISE EXCEPTION 'legacy Quest authority has no exact legacy Quest root';
            END IF;
          ELSIF NOT EXISTS (
            SELECT 1 FROM research_quest_streams WHERE quest_id = NEW.quest_id
          ) THEN
            RAISE EXCEPTION 'kernel Quest authority has no exact kernel Quest stream';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_quest_authority_binding
        AFTER INSERT ON research_quest_authorities
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_quest_authority()
        """
    )
    op.execute(
        """
        CREATE FUNCTION aletheia_research_quest_authority_immutable()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'research Quest authority claims are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_quest_authority_immutable
        BEFORE UPDATE OR DELETE ON research_quest_authorities
        FOR EACH ROW EXECUTE FUNCTION aletheia_research_quest_authority_immutable()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_command_scope()
        RETURNS trigger AS $$
        DECLARE
          stream_program_id text;
          stream_campaign_id text;
          stream_binding_sha256 text;
          stream_authorization_trust_root_sha256 text;
          stream_authorization_policy_sha256 text;
        BEGIN
          SELECT program_id, campaign_id, scope_binding_sha256,
                 authorization_trust_root_sha256, authorization_policy_sha256
            INTO stream_program_id, stream_campaign_id, stream_binding_sha256,
                 stream_authorization_trust_root_sha256,
                 stream_authorization_policy_sha256
          FROM research_quest_streams WHERE quest_id = NEW.quest_id;
          IF NOT FOUND
             OR NEW.scope_binding_sha256 IS DISTINCT FROM stream_binding_sha256
             OR NEW.authorization_trust_root_sha256
                  IS DISTINCT FROM stream_authorization_trust_root_sha256
             OR NEW.authorization_policy_sha256
                  IS DISTINCT FROM stream_authorization_policy_sha256
             OR NEW.command_json #>> '{scope_binding,quest_id}' IS DISTINCT FROM NEW.quest_id
             OR NEW.command_json #>> '{scope_binding,program_id}'
                  IS DISTINCT FROM stream_program_id
             OR NEW.command_json #>> '{scope_binding,campaign_id}'
                  IS DISTINCT FROM stream_campaign_id THEN
            RAISE EXCEPTION 'research command scope differs from its immutable Quest binding';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_kernel_command_scope
        AFTER INSERT ON research_kernel_command_receipts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_command_scope()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_event_bundle()
        RETURNS trigger AS $$
        DECLARE
          current_version bigint;
          current_tail text;
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM research_kernel_outbox
            WHERE quest_id = NEW.quest_id
              AND sequence = NEW.sequence
              AND event_sha256 = NEW.event_sha256
              AND payload_sha256 = NEW.event_sha256
          ) THEN
            RAISE EXCEPTION 'research event has no exact transactional outbox intent';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM research_kernel_snapshots
            WHERE quest_id = NEW.quest_id
              AND sequence = NEW.sequence
              AND tail_event_sha256 = NEW.event_sha256
          ) THEN
            RAISE EXCEPTION 'research event has no exact replay snapshot metadata';
          END IF;
          SELECT stream_version, tail_event_sha256 INTO current_version, current_tail
          FROM research_quest_streams WHERE quest_id = NEW.quest_id;
          IF NOT FOUND OR current_version < NEW.sequence
             OR (current_version = NEW.sequence
                 AND current_tail IS DISTINCT FROM NEW.event_sha256) THEN
            RAISE EXCEPTION 'research event is not reachable from its committed stream head';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_kernel_event_bundle_complete
        AFTER INSERT ON research_kernel_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_event_bundle()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_object_admission()
        RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM research_kernel_events
            WHERE quest_id = NEW.quest_id
              AND admitted_object_sha256 = NEW.object_sha256
              AND admitted_object_kind = NEW.object_kind
              AND admitted_object_id = NEW.object_id
          ) THEN
            RAISE EXCEPTION 'research kernel object has no exact admitting event';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_kernel_object_admission_complete
        AFTER INSERT ON research_kernel_objects
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_object_admission()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_research_kernel_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "research_kernel_objects",
        "research_kernel_command_receipts",
        "research_kernel_events",
        "research_kernel_snapshots",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_research_kernel_append_only()
            """
        )

    op.execute(
        """
        CREATE FUNCTION aletheia_research_quest_stream_guard()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'research Quest streams cannot be deleted';
          END IF;
          IF NEW.quest_id IS DISTINCT FROM OLD.quest_id
             OR NEW.program_id IS DISTINCT FROM OLD.program_id
             OR NEW.campaign_id IS DISTINCT FROM OLD.campaign_id
             OR NEW.scope_binding_sha256 IS DISTINCT FROM OLD.scope_binding_sha256
             OR NEW.authorization_trust_root_sha256
                  IS DISTINCT FROM OLD.authorization_trust_root_sha256
             OR NEW.authorization_policy_sha256
                  IS DISTINCT FROM OLD.authorization_policy_sha256
             OR NEW.authorization_policy_json IS DISTINCT FROM OLD.authorization_policy_json
             OR NEW.reducer_version IS DISTINCT FROM OLD.reducer_version
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'research Quest scope, authorization, and reducer identities are immutable';
          END IF;
          IF NEW.stream_version <> OLD.stream_version + 1 THEN
            RAISE EXCEPTION 'research Quest stream head must advance exactly one version';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'research Quest stream updated_at cannot move backward';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_quest_streams_guard
        BEFORE UPDATE OR DELETE ON research_quest_streams
        FOR EACH ROW EXECUTE FUNCTION aletheia_research_quest_stream_guard()
        """
    )
    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_stream_complete()
        RETURNS trigger AS $$
        DECLARE
          current_version bigint;
          current_tail text;
        BEGIN
          SELECT stream_version, tail_event_sha256 INTO current_version, current_tail
          FROM research_quest_streams WHERE quest_id = NEW.quest_id;
          IF NOT FOUND OR current_version < 1 OR current_tail IS NULL THEN
            RAISE EXCEPTION 'committed research Quest stream must contain a genesis event';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_quest_streams_complete
        AFTER INSERT OR UPDATE ON research_quest_streams
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_stream_complete()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_research_kernel_outbox_guard()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'research kernel outbox intents cannot be deleted';
          END IF;
          IF NEW.outbox_id IS DISTINCT FROM OLD.outbox_id
             OR NEW.quest_id IS DISTINCT FROM OLD.quest_id
             OR NEW.sequence IS DISTINCT FROM OLD.sequence
             OR NEW.event_sha256 IS DISTINCT FROM OLD.event_sha256
             OR NEW.topic IS DISTINCT FROM OLD.topic
             OR NEW.delivery_key IS DISTINCT FROM OLD.delivery_key
             OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'research kernel outbox event identity is immutable';
          END IF;
          IF NEW.delivery_attempts < OLD.delivery_attempts THEN
            RAISE EXCEPTION 'research kernel outbox attempts cannot decrease';
          END IF;
          IF OLD.delivery_status = 'published' AND NEW.delivery_status <> 'published' THEN
            RAISE EXCEPTION 'published research kernel outbox rows cannot be reopened';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_kernel_outbox_guard
        BEFORE UPDATE OR DELETE ON research_kernel_outbox
        FOR EACH ROW EXECUTE FUNCTION aletheia_research_kernel_outbox_guard()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_research_quest_authority_immutable ON research_quest_authorities")
    op.execute("DROP FUNCTION aletheia_research_quest_authority_immutable()")
    op.execute("DROP TRIGGER trg_research_quest_authority_binding ON research_quest_authorities")
    op.execute("DROP FUNCTION aletheia_validate_research_quest_authority()")
    op.execute("DROP TRIGGER trg_legacy_program_quest_authority_claim ON research_graph_nodes")
    op.execute("DROP TRIGGER trg_research_kernel_quest_authority_claim ON research_quest_streams")
    op.execute("DROP FUNCTION aletheia_claim_research_quest_authority()")
    op.execute(
        "DROP TRIGGER trg_research_kernel_object_admission_complete ON research_kernel_objects"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_object_admission()")
    op.execute("DROP TRIGGER trg_research_kernel_event_bundle_complete ON research_kernel_events")
    op.execute("DROP FUNCTION aletheia_validate_research_event_bundle()")
    op.execute("DROP TRIGGER trg_research_kernel_command_scope ON research_kernel_command_receipts")
    op.execute("DROP FUNCTION aletheia_validate_research_command_scope()")
    op.execute("DROP TRIGGER trg_research_kernel_outbox_guard ON research_kernel_outbox")
    op.execute("DROP FUNCTION aletheia_research_kernel_outbox_guard()")
    op.execute("DROP TRIGGER trg_research_quest_streams_complete ON research_quest_streams")
    op.execute("DROP FUNCTION aletheia_validate_research_stream_complete()")
    op.execute("DROP TRIGGER trg_research_quest_streams_guard ON research_quest_streams")
    op.execute("DROP FUNCTION aletheia_research_quest_stream_guard()")
    for table in (
        "research_kernel_snapshots",
        "research_kernel_events",
        "research_kernel_command_receipts",
        "research_kernel_objects",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION aletheia_research_kernel_append_only()")

    op.drop_constraint(
        "fk_research_kernel_command_receipts_result_event",
        "research_kernel_command_receipts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_research_kernel_command_receipts_expected_parent",
        "research_kernel_command_receipts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_research_quest_streams_tail_event",
        "research_quest_streams",
        type_="foreignkey",
    )
    op.drop_table("research_kernel_outbox")
    op.drop_table("research_kernel_snapshots")
    op.drop_table("research_kernel_events")
    op.drop_table("research_kernel_command_receipts")
    op.drop_table("research_kernel_objects")
    op.drop_table("research_quest_streams")
    op.drop_table("research_quest_authorities")
