"""Add the fenced local execution, resource, and budget authority.

Revision ID: 20260825_0024
Revises: 20260824_0023
Create Date: 2026-08-25

The legacy jobs queue remains transport only.  This schema is the sole authority for admission,
budget holds, resource/device leases, fencing, reconciliation, and terminal execution receipts.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260825_0024"
down_revision: str | None = "20260824_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _indexes(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=False)


def upgrade() -> None:
    op.create_table(
        "execution_nodes",
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("node_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("node_authority_pin_sha256", sa.String(64), nullable=False),
        sa.Column("node_authority_pin_json", JSONB, nullable=False),
        sa.Column("node_enrollment_sha256", sa.String(64), nullable=False),
        sa.Column("node_enrollment_json", JSONB, nullable=False),
        sa.Column("node_principal_id", sa.String(128), nullable=False),
        sa.Column("site_id", sa.String(192), nullable=False),
        sa.Column("manifest_json", JSONB, nullable=False),
        sa.Column("boot_id", sa.String(192), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("current_inventory_sha256", sa.String(64), nullable=True),
        sa.Column("current_inventory_sequence", sa.BigInteger(), nullable=True),
        sa.Column("reserved_cpu_cores", sa.Integer(), nullable=False),
        sa.Column("reserved_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_scratch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("exclusive_lease_id", sa.String(96), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('active','draining','retired')", name="ck_execution_nodes_state"
        ),
        sa.CheckConstraint(
            "state_version >= 1 AND reserved_cpu_cores >= 0 AND reserved_memory_bytes >= 0 "
            "AND reserved_scratch_bytes >= 0",
            name="ck_execution_nodes_capacity_head",
        ),
        sa.CheckConstraint(
            "node_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "node_authority_pin_sha256 ~ '^[0-9a-f]{64}$' AND "
            "node_enrollment_sha256 ~ '^[0-9a-f]{64}$' AND "
            "(current_inventory_sha256 IS NULL OR current_inventory_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_execution_nodes_hashes",
        ),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint("current_inventory_sha256"),
        sa.UniqueConstraint("node_enrollment_sha256"),
        sa.UniqueConstraint("exclusive_lease_id"),
    )
    _indexes("execution_nodes", "node_principal_id", "registered_at", "state")

    op.create_table(
        "execution_inventory_attestations",
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("node_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("boot_id", sa.String(192), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_monotonic_ns", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("scratch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("allocatable_cpu_cores", sa.Integer(), nullable=False),
        sa.Column("allocatable_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("allocatable_scratch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("managed_cpu_cores", sa.Integer(), nullable=False),
        sa.Column("managed_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("managed_scratch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("external_occupancy", sa.Boolean(), nullable=False),
        sa.Column("external_occupancy_sha256", sa.String(64), nullable=True),
        sa.Column("resource_class_ids_json", JSONB, nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("attested_by_principal_id", sa.String(128), nullable=False),
        sa.Column("signing_key_id", sa.String(64), nullable=False),
        sa.Column("signature_ed25519_hex", sa.String(128), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_execution_inventory_sequence"),
        sa.CheckConstraint(
            "cpu_cores >= 1 AND memory_bytes >= 1 AND scratch_bytes >= 1 "
            "AND allocatable_cpu_cores >= 0 AND allocatable_memory_bytes >= 0 "
            "AND allocatable_scratch_bytes >= 0 AND managed_cpu_cores >= 0 "
            "AND managed_memory_bytes >= 0 AND managed_scratch_bytes >= 0 "
            "AND allocatable_cpu_cores <= cpu_cores "
            "AND allocatable_memory_bytes <= memory_bytes "
            "AND allocatable_scratch_bytes <= scratch_bytes",
            name="ck_execution_inventory_capacity",
        ),
        sa.CheckConstraint(
            "observed_monotonic_ns >= 0 AND observed_at <= received_at AND received_at < valid_until",
            name="ck_execution_inventory_time",
        ),
        sa.CheckConstraint(
            "inventory_sha256 ~ '^[0-9a-f]{64}$' AND node_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_inventory_hashes",
        ),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.PrimaryKeyConstraint("inventory_sha256"),
        sa.UniqueConstraint(
            "node_id", "boot_id", "sequence", name="uq_execution_inventory_node_boot_sequence"
        ),
        sa.UniqueConstraint(
            "node_id",
            "boot_id",
            "observed_monotonic_ns",
            name="uq_execution_inventory_node_boot_monotonic",
        ),
        sa.UniqueConstraint("node_id", "inventory_sha256", name="uq_execution_inventory_node_hash"),
    )
    _indexes("execution_inventory_attestations", "node_id", "received_at", "valid_until")

    op.create_table(
        "execution_inventory_devices",
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(192), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("hardware_uuid", sa.String(192), nullable=False),
        sa.Column("resource_class_ids_json", JSONB, nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("total_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("safety_reserve_bytes", sa.BigInteger(), nullable=False),
        sa.Column("managed_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("allocatable_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("compute_capability", sa.String(16), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("external_occupancy", sa.Boolean(), nullable=False),
        sa.Column("external_occupancy_sha256", sa.String(64), nullable=True),
        sa.Column("features_json", JSONB, nullable=False),
        sa.CheckConstraint(
            "total_memory_bytes >= 1 AND safety_reserve_bytes >= 0 "
            "AND managed_memory_bytes >= 0 AND allocatable_memory_bytes >= 0 "
            "AND safety_reserve_bytes + managed_memory_bytes + "
            "allocatable_memory_bytes <= total_memory_bytes",
            name="ck_execution_inventory_devices_memory",
        ),
        sa.CheckConstraint(
            "external_occupancy = (external_occupancy_sha256 IS NOT NULL)",
            name="ck_execution_inventory_devices_external_pair",
        ),
        sa.CheckConstraint(
            "compute_capability ~ '^[0-9]+[.][0-9]+$'",
            name="ck_execution_inventory_devices_compute_capability",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_sha256"],
            ["execution_inventory_attestations.inventory_sha256"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.PrimaryKeyConstraint("inventory_sha256", "device_id"),
        sa.UniqueConstraint(
            "inventory_sha256", "hardware_uuid", name="uq_execution_inventory_devices_hardware"
        ),
    )
    _indexes("execution_inventory_devices", "model", "node_id")

    op.create_table(
        "execution_device_heads",
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(192), nullable=False),
        sa.Column("hardware_uuid", sa.String(192), nullable=False),
        sa.Column("current_inventory_sha256", sa.String(64), nullable=False),
        sa.Column("fencing_counter", sa.BigInteger(), nullable=False),
        sa.Column("active_device_lease_id", sa.String(96), nullable=True),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "fencing_counter >= 0 AND state_version >= 1", name="ck_execution_device_heads_versions"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.ForeignKeyConstraint(
            ["current_inventory_sha256", "device_id"],
            [
                "execution_inventory_devices.inventory_sha256",
                "execution_inventory_devices.device_id",
            ],
            name="fk_execution_device_heads_current_inventory",
        ),
        sa.PrimaryKeyConstraint("node_id", "device_id"),
        sa.UniqueConstraint("node_id", "hardware_uuid", name="uq_execution_device_heads_hardware"),
        sa.UniqueConstraint(
            "active_device_lease_id", name="uq_execution_device_heads_active_lease"
        ),
    )

    op.create_table(
        "execution_qualification_admissions",
        sa.Column("admission_sha256", sa.String(64), nullable=False),
        sa.Column("grant_sha256", sa.String(64), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("infrastructure_attempt_id", sa.String(36), nullable=False),
        sa.Column("budget_authorization_sha256", sa.String(64), nullable=False),
        sa.Column("cost_quote_sha256", sa.String(64), nullable=False),
        sa.Column("authority_policy_sha256", sa.String(64), nullable=False),
        sa.Column("authority_key_id", sa.String(64), nullable=False),
        sa.Column("bundle_json", JSONB, nullable=False),
        sa.Column("grant_json", JSONB, nullable=False),
        sa.Column("verified_receipt_json", JSONB, nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "admission_sha256 ~ '^[0-9a-f]{64}$' AND grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND bundle_sha256 ~ '^[0-9a-f]{64}$' AND intent_sha256 ~ '^[0-9a-f]{64}$' "
            "AND budget_authorization_sha256 ~ '^[0-9a-f]{64}$' "
            "AND cost_quote_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authority_policy_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authority_key_id ~ '^[0-9a-f]{64}$'",
            name="ck_execution_qualification_admissions_hashes",
        ),
        sa.PrimaryKeyConstraint("admission_sha256"),
        sa.UniqueConstraint(
            "admission_sha256",
            "infrastructure_attempt_id",
            name="uq_execution_qualification_admissions_attempt_binding",
        ),
        sa.UniqueConstraint("grant_sha256", name="uq_execution_qualification_admissions_grant"),
        sa.UniqueConstraint("bundle_sha256", name="uq_execution_qualification_admissions_bundle"),
    )
    _indexes(
        "execution_qualification_admissions",
        "intent_sha256",
        "execution_id",
        "infrastructure_attempt_id",
        "admitted_at",
    )

    op.create_table(
        "execution_budget_authorizations",
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("quest_id", sa.String(36), nullable=False),
        sa.Column("protocol_sha256", sa.String(64), nullable=False),
        sa.Column("work_order_sha256", sa.String(64), nullable=False),
        sa.Column("resource_budget_sha256", sa.String(64), nullable=False),
        sa.Column("source_budget_authorization_sha256", sa.String(64), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("cap_microunits", sa.BigInteger(), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_by_principal_id", sa.String(128), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cap_microunits >= 0 AND authorized_at < expires_at",
            name="ck_execution_budget_authorizations_cap_time",
        ),
        sa.CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'", name="ck_execution_budget_authorizations_currency"
        ),
        sa.CheckConstraint(
            "authorization_sha256 ~ '^[0-9a-f]{64}$' AND resource_budget_sha256 ~ '^[0-9a-f]{64}$' "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_budget_authorizations_hashes",
        ),
        sa.PrimaryKeyConstraint("authorization_sha256"),
        sa.UniqueConstraint(
            "quest_id",
            "resource_budget_sha256",
            "authorization_sha256",
            name="uq_execution_budget_authorizations_scope",
        ),
    )
    _indexes(
        "execution_budget_authorizations",
        "quest_id",
        "resource_budget_sha256",
        "expires_at",
        "registered_at",
    )

    op.create_table(
        "execution_budget_heads",
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("cap_microunits", sa.BigInteger(), nullable=False),
        sa.Column("reserved_microunits", sa.BigInteger(), nullable=False),
        sa.Column("spent_microunits", sa.BigInteger(), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "reserved_microunits >= 0 AND spent_microunits >= 0 "
            "AND reserved_microunits + spent_microunits <= cap_microunits AND state_version >= 1",
            name="ck_execution_budget_heads_balance",
        ),
        sa.CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'", name="ck_execution_budget_heads_currency"
        ),
        sa.ForeignKeyConstraint(
            ["authorization_sha256"], ["execution_budget_authorizations.authorization_sha256"]
        ),
        sa.PrimaryKeyConstraint("authorization_sha256"),
    )

    op.create_table(
        "execution_heads",
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("quest_id", sa.String(36), nullable=False),
        sa.Column("protocol_sha256", sa.String(64), nullable=False),
        sa.Column("work_order_id", sa.String(35), nullable=False),
        sa.Column("work_order_sha256", sa.String(64), nullable=False),
        sa.Column("replicate_slot_id", sa.String(36), nullable=False),
        sa.Column("replicate_slot_sha256", sa.String(64), nullable=False),
        sa.Column("last_attempt_number", sa.Integer(), nullable=False),
        sa.Column("active_attempt_id", sa.String(36), nullable=True),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "last_attempt_number >= 0 AND state_version >= 1", name="ck_execution_heads_versions"
        ),
        sa.CheckConstraint(
            "protocol_sha256 ~ '^[0-9a-f]{64}$' AND work_order_sha256 ~ '^[0-9a-f]{64}$' "
            "AND replicate_slot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_heads_hashes",
        ),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.UniqueConstraint("replicate_slot_id"),
        sa.UniqueConstraint("active_attempt_id"),
    )
    _indexes("execution_heads", "quest_id", "created_at")

    _create_attempt_and_lease_tables()
    _install_execution_guards()


def _create_attempt_and_lease_tables() -> None:
    """Create tables that depend on every immutable admission/capacity head."""
    op.create_table(
        "execution_attempts",
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("intent_json", JSONB, nullable=False),
        sa.Column("admission_sha256", sa.String(64), nullable=False),
        sa.Column("grant_sha256", sa.String(64), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("cost_quote_sha256", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("node_inventory_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_token_sha256", sa.String(64), nullable=False),
        sa.Column("adoption_count", sa.Integer(), nullable=False),
        sa.Column("latest_adoption_sha256", sa.String(64), nullable=True),
        sa.Column("last_runtime_inspection_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_runtime_inspection_sha256", sa.String(64), nullable=True),
        sa.Column("last_runtime_inspected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_runtime_inspected_monotonic_ns", sa.BigInteger(), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconciliation_reason", sa.String(64), nullable=True),
        sa.Column("runtime_identity_sha256", sa.String(64), nullable=True),
        sa.Column("runtime_identity_json", JSONB, nullable=True),
        sa.Column("terminal_receipt_sha256", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1 AND adoption_count >= 0 "
            "AND last_runtime_inspection_sequence >= 0 "
            "AND state_version >= 1 AND fencing_epoch >= 1",
            name="ck_execution_attempts_versions",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','starting','running','reconciliation_required','terminated',"
            "'verifying','succeeded','failed','cancelled')",
            name="ck_execution_attempts_status",
        ),
        sa.CheckConstraint(
            "authorized_at <= reserved_at AND reserved_at < hard_deadline "
            "AND reserved_at < lease_expires_at AND lease_expires_at <= hard_deadline",
            name="ck_execution_attempts_time",
        ),
        sa.CheckConstraint(
            "intent_sha256 ~ '^[0-9a-f]{64}$' AND admission_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grant_sha256 ~ '^[0-9a-f]{64}$' AND bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND cost_quote_sha256 ~ '^[0-9a-f]{64}$' AND lease_token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND node_inventory_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (latest_adoption_sha256 IS NULL OR latest_adoption_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (last_runtime_inspection_sha256 IS NULL OR "
            "last_runtime_inspection_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (runtime_identity_sha256 IS NULL OR runtime_identity_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (terminal_receipt_sha256 IS NULL OR terminal_receipt_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_execution_attempts_hashes",
        ),
        sa.CheckConstraint(
            "(runtime_identity_sha256 IS NULL) = (runtime_identity_json IS NULL)",
            name="ck_execution_attempts_runtime_identity_pair",
        ),
        sa.CheckConstraint(
            "(last_runtime_inspection_sequence = 0) = "
            "(last_runtime_inspection_sha256 IS NULL) AND "
            "(last_runtime_inspection_sequence = 0) = "
            "(last_runtime_inspected_at IS NULL) AND "
            "(last_runtime_inspection_sequence = 0) = "
            "(last_runtime_inspected_monotonic_ns IS NULL) AND "
            "(last_runtime_inspected_monotonic_ns IS NULL OR "
            "last_runtime_inspected_monotonic_ns >= 0)",
            name="ck_execution_attempts_inspection_tuple",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["execution_heads.execution_id"]),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.ForeignKeyConstraint(
            ["node_inventory_sha256"], ["execution_inventory_attestations.inventory_sha256"]
        ),
        sa.ForeignKeyConstraint(
            ["admission_sha256", "attempt_id"],
            [
                "execution_qualification_admissions.admission_sha256",
                "execution_qualification_admissions.infrastructure_attempt_id",
            ],
            name="fk_execution_attempts_admission_attempt",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("execution_id", "attempt_number", name="uq_execution_attempts_number"),
        sa.UniqueConstraint(
            "attempt_id", "execution_id", name="uq_execution_attempts_attempt_execution"
        ),
        sa.UniqueConstraint("intent_sha256", name="uq_execution_attempts_intent"),
        sa.UniqueConstraint("latest_adoption_sha256"),
        sa.UniqueConstraint("terminal_receipt_sha256"),
    )
    _indexes(
        "execution_attempts",
        "execution_id",
        "node_id",
        "node_inventory_sha256",
        "status",
        "reserved_at",
        "lease_expires_at",
    )
    op.create_index(
        "uq_execution_attempts_active_execution",
        "execution_attempts",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('reserved','starting','running','reconciliation_required','terminated','verifying')"
        ),
    )

    op.create_table(
        "execution_attempt_adoptions",
        sa.Column("adoption_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("new_fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("previous_lease_token_sha256", sa.String(64), nullable=False),
        sa.Column("new_lease_token_sha256", sa.String(64), nullable=False),
        sa.Column("runtime_identity_sha256", sa.String(64), nullable=False),
        sa.Column("reason_sha256", sa.String(64), nullable=False),
        sa.Column("adopted_by_principal_id", sa.String(128), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1 AND previous_fencing_epoch >= 1 "
            "AND new_fencing_epoch = previous_fencing_epoch + 1",
            name="ck_execution_attempt_adoptions_fence",
        ),
        sa.CheckConstraint(
            "adoption_sha256 ~ '^[0-9a-f]{64}$' "
            "AND previous_lease_token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND new_lease_token_sha256 ~ '^[0-9a-f]{64}$' "
            "AND runtime_identity_sha256 ~ '^[0-9a-f]{64}$' "
            "AND reason_sha256 ~ '^[0-9a-f]{64}$' AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_attempt_adoptions_hashes",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.attempt_id"]),
        sa.PrimaryKeyConstraint("adoption_sha256"),
        sa.UniqueConstraint(
            "attempt_id", "sequence", name="uq_execution_attempt_adoptions_sequence"
        ),
        sa.UniqueConstraint(
            "attempt_id", "new_fencing_epoch", name="uq_execution_attempt_adoptions_fence"
        ),
    )
    _indexes("execution_attempt_adoptions", "attempt_id", "adopted_at")

    op.create_table(
        "execution_resource_leases",
        sa.Column("lease_id", sa.String(96), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("lease_sha256", sa.String(64), nullable=False),
        sa.Column("lease_json", JSONB, nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("scratch_bytes", sa.BigInteger(), nullable=False),
        sa.Column("exclusive", sa.Boolean(), nullable=False),
        sa.Column("accelerator_count", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "cpu_cores >= 1 AND memory_bytes >= 1 AND scratch_bytes >= 1 "
            "AND accelerator_count BETWEEN 0 AND 1 AND fencing_epoch >= 1",
            name="ck_execution_resource_leases_capacity",
        ),
        sa.CheckConstraint(
            "state IN ('held','reconciliation_required','released')",
            name="ck_execution_resource_leases_state",
        ),
        sa.CheckConstraint(
            "acquired_at < lease_expires_at AND (released_at IS NULL OR released_at >= acquired_at)",
            name="ck_execution_resource_leases_time",
        ),
        sa.CheckConstraint(
            "inventory_sha256 ~ '^[0-9a-f]{64}$' AND lease_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_resource_leases_hashes",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.attempt_id"]),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.ForeignKeyConstraint(
            ["inventory_sha256"], ["execution_inventory_attestations.inventory_sha256"]
        ),
        sa.PrimaryKeyConstraint("lease_id"),
        sa.UniqueConstraint("attempt_id", name="uq_execution_resource_leases_attempt"),
        sa.UniqueConstraint("lease_sha256"),
    )
    _indexes(
        "execution_resource_leases",
        "attempt_id",
        "node_id",
        "state",
        "acquired_at",
        "lease_expires_at",
    )

    op.create_table(
        "execution_device_leases",
        sa.Column("device_lease_id", sa.String(96), nullable=False),
        sa.Column("resource_lease_id", sa.String(96), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(192), nullable=False),
        sa.Column("hardware_uuid", sa.String(192), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("requested_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('held','reconciliation_required','released')",
            name="ck_execution_device_leases_state",
        ),
        sa.CheckConstraint(
            "fencing_epoch >= 1 AND requested_memory_bytes >= 1",
            name="ck_execution_device_leases_fence_memory",
        ),
        sa.ForeignKeyConstraint(["resource_lease_id"], ["execution_resource_leases.lease_id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.attempt_id"]),
        sa.ForeignKeyConstraint(["node_id"], ["execution_nodes.node_id"]),
        sa.PrimaryKeyConstraint("device_lease_id"),
        sa.UniqueConstraint(
            "resource_lease_id", "device_id", name="uq_execution_device_leases_claim"
        ),
    )
    _indexes(
        "execution_device_leases",
        "resource_lease_id",
        "attempt_id",
        "node_id",
        "device_id",
        "state",
    )
    op.create_index(
        "uq_execution_device_leases_active_device",
        "execution_device_leases",
        ["node_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('held','reconciliation_required')"),
    )

    op.create_table(
        "execution_budget_reservations",
        sa.Column("reservation_id", sa.String(96), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("cost_quote_sha256", sa.String(64), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("fixed_charge_microunits", sa.BigInteger(), nullable=False),
        sa.Column("charge_per_second_microunits", sa.BigInteger(), nullable=False),
        sa.Column("maximum_lease_seconds", sa.BigInteger(), nullable=False),
        sa.Column("actual_lease_seconds", sa.BigInteger(), nullable=True),
        sa.Column("held_microunits", sa.BigInteger(), nullable=False),
        sa.Column("settled_microunits", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "fixed_charge_microunits >= 0 AND charge_per_second_microunits >= 0 "
            "AND maximum_lease_seconds >= 1 "
            "AND held_microunits = fixed_charge_microunits + "
            "(charge_per_second_microunits * maximum_lease_seconds) "
            "AND settled_microunits >= 0 AND settled_microunits <= held_microunits",
            name="ck_execution_budget_reservations_amounts",
        ),
        sa.CheckConstraint(
            "(state IN ('held','reconciliation_required') AND actual_lease_seconds IS NULL "
            " AND settled_microunits = 0 AND settled_at IS NULL) OR "
            "(state = 'settled' AND actual_lease_seconds IS NOT NULL "
            " AND actual_lease_seconds BETWEEN 0 AND maximum_lease_seconds "
            " AND settled_microunits = fixed_charge_microunits + "
            "     (charge_per_second_microunits * actual_lease_seconds) "
            " AND settled_at IS NOT NULL AND settled_at >= reserved_at) OR "
            "(state = 'released' AND actual_lease_seconds IS NULL "
            " AND settled_microunits = 0 AND settled_at IS NOT NULL "
            " AND settled_at >= reserved_at)",
            name="ck_execution_budget_reservations_settlement",
        ),
        sa.CheckConstraint(
            "state IN ('held','reconciliation_required','settled','released')",
            name="ck_execution_budget_reservations_state",
        ),
        sa.CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'", name="ck_execution_budget_reservations_currency"
        ),
        sa.CheckConstraint(
            "cost_quote_sha256 ~ '^[0-9a-f]{64}$'", name="ck_execution_budget_reservations_quote"
        ),
        sa.ForeignKeyConstraint(
            ["authorization_sha256"], ["execution_budget_heads.authorization_sha256"]
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.attempt_id"]),
        sa.ForeignKeyConstraint(["execution_id"], ["execution_heads.execution_id"]),
        sa.PrimaryKeyConstraint("reservation_id"),
        sa.UniqueConstraint("attempt_id", name="uq_execution_budget_reservations_attempt"),
        sa.UniqueConstraint(
            "authorization_sha256",
            "cost_quote_sha256",
            name="uq_execution_budget_reservations_quote",
        ),
    )
    _indexes(
        "execution_budget_reservations",
        "authorization_sha256",
        "attempt_id",
        "execution_id",
        "state",
        "reserved_at",
    )

    op.create_table(
        "execution_budget_events",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_sha256", sa.String(64), nullable=False),
        sa.Column("reservation_id", sa.String(96), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_sha256", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("reserved_delta_microunits", sa.BigInteger(), nullable=False),
        sa.Column("spent_delta_microunits", sa.BigInteger(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_execution_budget_events_sequence"),
        sa.CheckConstraint(
            "event_type IN ('reserved','reconciliation_required','adopted','settled','released')",
            name="ck_execution_budget_events_type",
        ),
        sa.CheckConstraint(
            "event_sha256 ~ '^[0-9a-f]{64}$' AND payload_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (previous_event_sha256 IS NULL OR previous_event_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_execution_budget_events_hashes",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND previous_event_sha256 IS NULL) OR (sequence > 1 AND previous_event_sha256 IS NOT NULL)",
            name="ck_execution_budget_events_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["execution_budget_reservations.reservation_id"]
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "reservation_id", "sequence", name="uq_execution_budget_events_sequence"
        ),
        sa.UniqueConstraint("event_sha256", name="uq_execution_budget_events_hash"),
    )
    _indexes(
        "execution_budget_events",
        "reservation_id",
        "authorization_sha256",
        "event_type",
        "recorded_at",
    )

    op.create_table(
        "execution_terminal_receipts",
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("resource_lease_sha256", sa.String(64), nullable=False),
        sa.Column("node_execution_receipt_sha256", sa.String(64), nullable=False),
        sa.Column("node_execution_receipt_json", JSONB, nullable=False),
        sa.Column("terminal_verification_attestation_sha256", sa.String(64), nullable=False),
        sa.Column("terminal_verification_attestation_json", JSONB, nullable=False),
        sa.Column("terminal_verification_authority_pin_sha256", sa.String(64), nullable=False),
        sa.Column("terminal_verification_authority_pin_json", JSONB, nullable=False),
        sa.Column("terminal_verification_policy_sha256", sa.String(64), nullable=False),
        sa.Column("terminal_verification_key_id", sa.String(64), nullable=False),
        sa.Column("terminal_state", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("artifact_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_manifest_json", JSONB, nullable=False),
        sa.Column("artifact_verified_receipt_sha256s_json", JSONB, nullable=False),
        sa.Column("committed_by_principal_id", sa.String(128), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "terminal_state IN ('engineering_succeeded','execution_failed','cancelled')",
            name="ck_execution_terminal_receipts_state",
        ),
        sa.CheckConstraint(
            "receipt_sha256 ~ '^[0-9a-f]{64}$' AND intent_sha256 ~ '^[0-9a-f]{64}$' "
            "AND resource_lease_sha256 ~ '^[0-9a-f]{64}$' "
            "AND node_execution_receipt_sha256 ~ '^[0-9a-f]{64}$' "
            "AND terminal_verification_attestation_sha256 ~ '^[0-9a-f]{64}$' "
            "AND terminal_verification_authority_pin_sha256 ~ '^[0-9a-f]{64}$' "
            "AND terminal_verification_policy_sha256 ~ '^[0-9a-f]{64}$' "
            "AND terminal_verification_key_id ~ '^[0-9a-f]{64}$' "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_terminal_receipts_hashes",
        ),
        sa.CheckConstraint(
            "terminal_verification_attestation_json->>'signature_ed25519_hex' "
            "~ '^[0-9a-f]{128}$' AND "
            "terminal_verification_authority_pin_json->>'public_key_ed25519_hex' "
            "~ '^[0-9a-f]{64}$'",
            name="ck_execution_terminal_receipts_verification_keys",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "execution_id"],
            ["execution_attempts.attempt_id", "execution_attempts.execution_id"],
            name="fk_execution_terminal_receipts_attempt_execution",
        ),
        sa.PrimaryKeyConstraint("receipt_sha256"),
        sa.UniqueConstraint("attempt_id", name="uq_execution_terminal_receipts_attempt"),
        sa.UniqueConstraint("node_execution_receipt_sha256"),
        sa.UniqueConstraint(
            "terminal_verification_attestation_sha256",
            name="uq_execution_terminal_receipts_verification_attestation",
        ),
    )
    _indexes(
        "execution_terminal_receipts",
        "attempt_id",
        "execution_id",
        "terminal_state",
        "committed_at",
    )

    op.create_table(
        "execution_outbox",
        sa.Column("outbox_id", sa.String(96), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("topic", sa.String(96), nullable=False),
        sa.Column("delivery_key", sa.String(192), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','published') AND publish_attempts >= 0",
            name="ck_execution_outbox_status",
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_execution_outbox_payload_hash"
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND published_at IS NULL) OR (status = 'published' AND published_at IS NOT NULL)",
            name="ck_execution_outbox_publish_time",
        ),
        sa.ForeignKeyConstraint(["receipt_sha256"], ["execution_terminal_receipts.receipt_sha256"]),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("receipt_sha256", name="uq_execution_outbox_receipt"),
        sa.UniqueConstraint("delivery_key", name="uq_execution_outbox_delivery_key"),
    )
    _indexes(
        "execution_outbox", "receipt_sha256", "execution_id", "attempt_id", "status", "created_at"
    )


def _install_execution_guards() -> None:
    """Install immutable, monotonic-transition, and deferred completeness guards."""
    op.execute(
        """
        CREATE FUNCTION aletheia_execution_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in (
        "execution_inventory_attestations",
        "execution_inventory_devices",
        "execution_qualification_admissions",
        "execution_budget_authorizations",
        "execution_attempt_adoptions",
        "execution_budget_events",
        "execution_terminal_receipts",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_execution_reject_mutation()
            """
        )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_node() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'execution node head is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.node_id, NEW.node_manifest_sha256, NEW.node_authority_pin_sha256,
              NEW.node_authority_pin_json, NEW.node_enrollment_sha256,
              NEW.node_enrollment_json, NEW.node_principal_id,
              NEW.site_id, NEW.manifest_json, NEW.registered_at)
             IS DISTINCT FROM
             (OLD.node_id, OLD.node_manifest_sha256, OLD.node_authority_pin_sha256,
              OLD.node_authority_pin_json, OLD.node_enrollment_sha256,
              OLD.node_enrollment_json, OLD.node_principal_id,
              OLD.site_id, OLD.manifest_json, OLD.registered_at) THEN
            RAISE EXCEPTION 'execution node identity is immutable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.boot_id, NEW.current_inventory_sha256, NEW.current_inventory_sequence)
             IS DISTINCT FROM
             (OLD.boot_id, OLD.current_inventory_sha256, OLD.current_inventory_sequence) THEN
            IF NEW.current_inventory_sha256 IS NULL THEN
              RAISE EXCEPTION 'execution node inventory pointer cannot roll back to null'
                USING ERRCODE = '55000';
            END IF;
            IF OLD.current_inventory_sha256 IS NOT NULL AND NOT EXISTS (
              SELECT 1
                FROM execution_inventory_attestations prior,
                     execution_inventory_attestations current
               WHERE prior.inventory_sha256 = OLD.current_inventory_sha256
                 AND current.inventory_sha256 = NEW.current_inventory_sha256
                 AND current.node_id = NEW.node_id
                 AND current.received_at >= prior.received_at
                 AND current.observed_at > prior.observed_at
                 AND (
                   (current.boot_id = prior.boot_id
                    AND current.sequence > prior.sequence
                    AND current.observed_monotonic_ns > prior.observed_monotonic_ns)
                   OR
                   (current.boot_id <> prior.boot_id AND NOT EXISTS (
                     SELECT 1 FROM execution_resource_leases l
                      WHERE l.node_id = NEW.node_id
                        AND l.state IN ('held','reconciliation_required')
                   ))
                 )
            ) THEN
              RAISE EXCEPTION 'execution node inventory pointer is regressive or reboot-unsafe'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'execution node state_version must advance exactly once'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'execution node database clock moved backward'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.state = 'retired' OR
             (OLD.state = 'draining' AND NEW.state NOT IN ('draining','retired')) OR
             (OLD.state = 'active' AND NEW.state NOT IN ('active','draining','retired')) THEN
            RAISE EXCEPTION 'invalid execution node transition' USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_nodes_guard
        BEFORE UPDATE OR DELETE ON execution_nodes
        FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_node();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_execution_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'execution head is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.execution_id, NEW.quest_id, NEW.protocol_sha256, NEW.work_order_id,
              NEW.work_order_sha256, NEW.replicate_slot_id, NEW.replicate_slot_sha256,
              NEW.created_at) IS DISTINCT FROM
             (OLD.execution_id, OLD.quest_id, OLD.protocol_sha256, OLD.work_order_id,
              OLD.work_order_sha256, OLD.replicate_slot_id, OLD.replicate_slot_sha256,
              OLD.created_at) THEN
            RAISE EXCEPTION 'execution head scope is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 OR
             NEW.last_attempt_number < OLD.last_attempt_number OR
             NEW.last_attempt_number > OLD.last_attempt_number + 1 THEN
            RAISE EXCEPTION 'execution head version/attempt progression is invalid'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'execution head database clock moved backward'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_heads_guard
          BEFORE UPDATE OR DELETE ON execution_heads
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_execution_head();

        CREATE FUNCTION aletheia_execution_guard_budget_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'budget head is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.authorization_sha256, NEW.currency_code, NEW.cap_microunits)
             IS DISTINCT FROM
             (OLD.authorization_sha256, OLD.currency_code, OLD.cap_microunits) THEN
            RAISE EXCEPTION 'budget head authorization/cap is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'budget head state_version must advance exactly once'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'budget head database clock moved backward'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_budget_heads_guard
          BEFORE UPDATE OR DELETE ON execution_budget_heads
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_budget_head();

        CREATE FUNCTION aletheia_execution_guard_device_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'device fencing head is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.node_id, NEW.device_id, NEW.hardware_uuid) IS DISTINCT FROM
             (OLD.node_id, OLD.device_id, OLD.hardware_uuid) THEN
            RAISE EXCEPTION 'device head physical identity is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 OR
             NEW.fencing_counter < OLD.fencing_counter OR
             (OLD.active_device_lease_id IS NULL AND
              NEW.active_device_lease_id IS NULL AND
              NEW.fencing_counter <> OLD.fencing_counter) OR
             (OLD.active_device_lease_id IS NULL AND
              NEW.active_device_lease_id IS NOT NULL AND
              NEW.fencing_counter <= OLD.fencing_counter) OR
             (OLD.active_device_lease_id IS NOT NULL AND
              NEW.active_device_lease_id IS NULL AND
              NEW.fencing_counter <> OLD.fencing_counter) OR
             (OLD.active_device_lease_id IS NOT NULL AND
              NEW.active_device_lease_id IS NOT NULL AND
              NEW.active_device_lease_id <> OLD.active_device_lease_id) OR
             (NEW.active_device_lease_id IS NOT DISTINCT FROM OLD.active_device_lease_id AND
              NEW.fencing_counter NOT IN
                (OLD.fencing_counter, OLD.fencing_counter + 1)) THEN
            RAISE EXCEPTION 'device head version/fence progression is invalid'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'device head database clock moved backward'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_device_heads_guard
          BEFORE UPDATE OR DELETE ON execution_device_heads
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_device_head();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_attempt() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE allowed boolean; adopting boolean; finalizing boolean; terminalizing boolean;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'execution attempt is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.attempt_id, NEW.execution_id, NEW.attempt_number, NEW.intent_sha256,
              NEW.intent_json, NEW.admission_sha256, NEW.grant_sha256, NEW.bundle_sha256,
              NEW.cost_quote_sha256, NEW.node_id, NEW.node_inventory_sha256,
              NEW.authorized_at, NEW.reserved_at,
              NEW.hard_deadline)
             IS DISTINCT FROM
             (OLD.attempt_id, OLD.execution_id, OLD.attempt_number, OLD.intent_sha256,
              OLD.intent_json, OLD.admission_sha256, OLD.grant_sha256, OLD.bundle_sha256,
              OLD.cost_quote_sha256, OLD.node_id, OLD.node_inventory_sha256,
              OLD.authorized_at, OLD.reserved_at,
              OLD.hard_deadline) THEN
            RAISE EXCEPTION 'execution attempt authority is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'execution attempt state_version must advance exactly once'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at OR NEW.heartbeat_at < OLD.heartbeat_at OR
             NEW.lease_expires_at < OLD.lease_expires_at OR
             NEW.lease_expires_at > NEW.hard_deadline THEN
            RAISE EXCEPTION 'attempt clock/heartbeat/lease expiry is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          adopting := NEW.fencing_epoch = OLD.fencing_epoch + 1
            AND NEW.adoption_count = OLD.adoption_count + 1
            AND NEW.latest_adoption_sha256 IS NOT NULL
            AND NEW.lease_token_sha256 <> OLD.lease_token_sha256
            AND NEW.status = 'running'
            AND EXISTS (
              SELECT 1 FROM execution_attempt_adoptions d
               WHERE d.adoption_sha256 = NEW.latest_adoption_sha256
                 AND d.attempt_id = OLD.attempt_id
                 AND d.sequence = NEW.adoption_count
                 AND d.previous_fencing_epoch = OLD.fencing_epoch
                 AND d.new_fencing_epoch = NEW.fencing_epoch
                 AND d.previous_lease_token_sha256 = OLD.lease_token_sha256
                 AND d.new_lease_token_sha256 = NEW.lease_token_sha256
                 AND d.runtime_identity_sha256 = NEW.runtime_identity_sha256
            );
          finalizing := OLD.status = 'reconciliation_required'
            AND NEW.status IN ('succeeded','failed','cancelled')
            AND NEW.terminal_receipt_sha256 IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM execution_terminal_receipts r
               WHERE r.receipt_sha256 = NEW.terminal_receipt_sha256
                 AND r.attempt_id = NEW.attempt_id
            );
          terminalizing := NEW.status IN ('succeeded','failed','cancelled')
            AND NEW.terminal_receipt_sha256 IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM execution_terminal_receipts r
               WHERE r.receipt_sha256 = NEW.terminal_receipt_sha256
                 AND r.attempt_id = NEW.attempt_id
            );
          IF NOT adopting AND
             (NEW.fencing_epoch, NEW.lease_token_sha256, NEW.adoption_count,
              NEW.latest_adoption_sha256) IS DISTINCT FROM
             (OLD.fencing_epoch, OLD.lease_token_sha256, OLD.adoption_count,
              OLD.latest_adoption_sha256) THEN
            RAISE EXCEPTION 'fence/token rotation requires an exact adoption receipt'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NOT NULL AND
             (NEW.runtime_identity_sha256, NEW.runtime_identity_json) IS DISTINCT FROM
             (OLD.runtime_identity_sha256, OLD.runtime_identity_json) THEN
            RAISE EXCEPTION 'runtime identity is immutable once bound' USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NULL AND NEW.runtime_identity_sha256 IS NOT NULL AND
             NOT (OLD.status = 'reserved' AND NEW.status = 'starting') THEN
            RAISE EXCEPTION 'runtime identity may bind only while starting a reservation'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NULL AND NEW.runtime_identity_sha256 IS NOT NULL AND
             (NEW.runtime_identity_json->>'started_at')::timestamptz NOT BETWEEN
               NEW.reserved_at AND NEW.updated_at THEN
            RAISE EXCEPTION 'runtime start is outside its reservation/DB observation order'
              USING ERRCODE = '55000';
          END IF;
          IF (NEW.last_runtime_inspection_sequence, NEW.last_runtime_inspection_sha256,
              NEW.last_runtime_inspected_at, NEW.last_runtime_inspected_monotonic_ns)
             IS DISTINCT FROM
             (OLD.last_runtime_inspection_sequence, OLD.last_runtime_inspection_sha256,
              OLD.last_runtime_inspected_at, OLD.last_runtime_inspected_monotonic_ns) AND
             (NEW.last_runtime_inspection_sequence <= OLD.last_runtime_inspection_sequence OR
              NEW.last_runtime_inspected_at IS NULL OR
              NEW.last_runtime_inspected_monotonic_ns IS NULL OR
              (OLD.last_runtime_inspected_at IS NOT NULL AND
               NEW.last_runtime_inspected_at <= OLD.last_runtime_inspected_at) OR
              (OLD.last_runtime_inspected_monotonic_ns IS NOT NULL AND
               NEW.last_runtime_inspected_monotonic_ns <=
                 OLD.last_runtime_inspected_monotonic_ns) OR
              NOT (adopting OR terminalizing)) THEN
            RAISE EXCEPTION 'runtime inspection order may advance only by signed adoption/exit'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.status IN ('starting','running','terminated','verifying',
                            'succeeded','failed','cancelled') AND
             NEW.runtime_identity_sha256 IS NULL THEN
            RAISE EXCEPTION 'launched/terminal attempt requires exact runtime identity'
              USING ERRCODE = '55000';
          END IF;
          allowed := NEW.status = OLD.status OR
            (OLD.status = 'reserved' AND NEW.status IN
              ('starting','reconciliation_required')) OR
            (OLD.status = 'starting' AND NEW.status IN
              ('running','reconciliation_required')) OR
            (OLD.status = 'running' AND NEW.status IN
              ('terminated','verifying','reconciliation_required')) OR
            (OLD.status = 'terminated' AND NEW.status IN
              ('verifying','reconciliation_required')) OR
            (OLD.status = 'verifying' AND NEW.status IN
              ('reconciliation_required')) OR
            (OLD.status IN ('running','terminated','verifying') AND terminalizing) OR
            (OLD.status = 'reconciliation_required' AND (adopting OR finalizing));
          IF NOT allowed THEN
            RAISE EXCEPTION 'invalid execution attempt transition % -> %', OLD.status, NEW.status
              USING ERRCODE = '55000';
          END IF;
          IF OLD.terminal_receipt_sha256 IS NOT NULL AND
             NEW.terminal_receipt_sha256 IS DISTINCT FROM OLD.terminal_receipt_sha256 THEN
            RAISE EXCEPTION 'terminal receipt identity is immutable' USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_attempts_guard
        BEFORE UPDATE OR DELETE ON execution_attempts
        FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_attempt();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_lease_state() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          adopting boolean := false;
          finalizing boolean := false;
          new_fence bigint := (to_jsonb(NEW)->>'fencing_epoch')::bigint;
          old_fence bigint := (to_jsonb(OLD)->>'fencing_epoch')::bigint;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION '% is not deletable', TG_TABLE_NAME USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_resource_leases' THEN
            IF (to_jsonb(NEW) - ARRAY['state','fencing_epoch','heartbeat_at','lease_expires_at','released_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','fencing_epoch','heartbeat_at','lease_expires_at','released_at']) THEN
              RAISE EXCEPTION 'resource lease identity is immutable' USING ERRCODE = '55000';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_device_leases' THEN
            IF (to_jsonb(NEW) - ARRAY['state','fencing_epoch','released_at']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','fencing_epoch','released_at']) THEN
              RAISE EXCEPTION 'device lease identity is immutable' USING ERRCODE = '55000';
            END IF;
          ELSE
            IF (to_jsonb(NEW) - ARRAY['state','actual_lease_seconds',
                                      'settled_microunits','settled_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','actual_lease_seconds',
                                      'settled_microunits','settled_at']) THEN
              RAISE EXCEPTION 'budget reservation identity is immutable' USING ERRCODE = '55000';
            END IF;
            adopting := OLD.state = 'reconciliation_required' AND NEW.state = 'held'
              AND EXISTS (
                SELECT 1 FROM execution_attempts a
                 JOIN execution_attempt_adoptions d
                   ON d.adoption_sha256 = a.latest_adoption_sha256
                 WHERE a.attempt_id = NEW.attempt_id AND a.status = 'running'
              );
          END IF;
          IF new_fence IS DISTINCT FROM old_fence THEN
            adopting := new_fence = old_fence + 1 AND EXISTS (
              SELECT 1 FROM execution_attempt_adoptions d
               WHERE d.attempt_id = (to_jsonb(NEW)->>'attempt_id')
                 AND d.previous_fencing_epoch = old_fence
                 AND d.new_fencing_epoch = new_fence
            );
            IF NOT adopting THEN
              RAISE EXCEPTION 'lease fence rotation lacks exact adoption receipt'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          finalizing := OLD.state = 'reconciliation_required'
            AND NEW.state IN ('released','settled')
            AND EXISTS (
              SELECT 1 FROM execution_terminal_receipts r
               WHERE r.attempt_id = (to_jsonb(NEW)->>'attempt_id')
            );
          IF NOT (
            NEW.state = OLD.state OR
            (OLD.state = 'held' AND NEW.state IN ('reconciliation_required','released','settled')) OR
            (OLD.state = 'reconciliation_required' AND NEW.state = 'held' AND adopting) OR
            finalizing
          ) THEN
            RAISE EXCEPTION 'invalid % state transition % -> %', TG_TABLE_NAME, OLD.state, NEW.state
              USING ERRCODE = '55000';
          END IF;
          IF OLD.state IN ('released','settled') AND NEW.state <> OLD.state THEN
            RAISE EXCEPTION '% terminal/reconciliation state is sticky', TG_TABLE_NAME
              USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_budget_reservations' AND
             OLD.state IN ('released','settled') AND
             (to_jsonb(NEW)->'actual_lease_seconds', to_jsonb(NEW)->'settled_microunits',
              to_jsonb(NEW)->'settled_at') IS DISTINCT FROM
             (to_jsonb(OLD)->'actual_lease_seconds', to_jsonb(OLD)->'settled_microunits',
              to_jsonb(OLD)->'settled_at') THEN
            RAISE EXCEPTION 'terminal budget settlement is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_resource_leases' THEN
            IF NEW.heartbeat_at < OLD.heartbeat_at OR
               NEW.lease_expires_at < OLD.lease_expires_at OR
               NEW.lease_expires_at > (SELECT hard_deadline FROM execution_attempts
                                        WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'resource lease heartbeat/expiry is non-monotonic or past deadline'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_resource_leases_guard
          BEFORE UPDATE OR DELETE ON execution_resource_leases
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_lease_state();
        CREATE TRIGGER trg_execution_device_leases_guard
          BEFORE UPDATE OR DELETE ON execution_device_leases
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_lease_state();
        CREATE TRIGGER trg_execution_budget_reservations_guard
          BEFORE UPDATE OR DELETE ON execution_budget_reservations
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_lease_state();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_budget_event_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE prior_hash text;
        BEGIN
          IF NEW.sequence = 1 THEN
            IF EXISTS (SELECT 1 FROM execution_budget_events e
                       WHERE e.reservation_id = NEW.reservation_id) THEN
              RAISE EXCEPTION 'budget event chain already exists' USING ERRCODE = '23505';
            END IF;
          ELSE
            SELECT event_sha256 INTO prior_hash
              FROM execution_budget_events
             WHERE reservation_id = NEW.reservation_id AND sequence = NEW.sequence - 1;
            IF prior_hash IS NULL OR prior_hash <> NEW.previous_event_sha256 THEN
              RAISE EXCEPTION 'budget event chain predecessor mismatch' USING ERRCODE = '55000';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_budget_events_chain
          BEFORE INSERT ON execution_budget_events
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_budget_event_chain();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_guard_outbox() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'execution outbox is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.outbox_id, NEW.receipt_sha256, NEW.execution_id, NEW.attempt_id,
              NEW.topic, NEW.delivery_key, NEW.payload_sha256, NEW.payload_json, NEW.created_at)
             IS DISTINCT FROM
             (OLD.outbox_id, OLD.receipt_sha256, OLD.execution_id, OLD.attempt_id,
              OLD.topic, OLD.delivery_key, OLD.payload_sha256, OLD.payload_json, OLD.created_at) THEN
            RAISE EXCEPTION 'execution outbox payload is immutable' USING ERRCODE = '55000';
          END IF;
          IF OLD.status = 'published' OR NEW.publish_attempts < OLD.publish_attempts OR
             NOT (NEW.status = OLD.status OR
                  (OLD.status = 'pending' AND NEW.status = 'published')) THEN
            RAISE EXCEPTION 'invalid execution outbox publication transition' USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_outbox_guard
          BEFORE UPDATE OR DELETE ON execution_outbox
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_outbox();
        """
    )

    _install_deferred_execution_guards()


def _install_deferred_execution_guards() -> None:
    """Close multi-row authority bundles at transaction commit."""
    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_current_inventory() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          target_node text := NEW.node_id;
          node_row execution_nodes%ROWTYPE;
        BEGIN
          SELECT * INTO node_row FROM execution_nodes WHERE node_id = target_node;
          IF NOT FOUND THEN RETURN NULL; END IF;
          IF node_row.current_inventory_sha256 IS NULL THEN
            IF node_row.current_inventory_sequence IS NOT NULL OR node_row.boot_id IS NOT NULL THEN
              RAISE EXCEPTION 'empty node inventory pointer is not a null tuple'
                USING ERRCODE = '23514';
            END IF;
          ELSIF NOT EXISTS (
            SELECT 1 FROM execution_inventory_attestations i
             WHERE i.inventory_sha256 = node_row.current_inventory_sha256
               AND i.node_id = node_row.node_id
               AND i.node_manifest_sha256 = node_row.node_manifest_sha256
               AND i.boot_id = node_row.boot_id
               AND i.sequence = node_row.current_inventory_sequence
               AND (SELECT count(*)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu') = 1
               AND i.cpu_cores = (SELECT sum((r.value->>'cpu_cores_total')::bigint)
                  FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                 WHERE r.value->>'kind' = 'cpu')
               AND i.memory_bytes = (SELECT sum((r.value->>'memory_bytes_total')::bigint)
                  FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                 WHERE r.value->>'kind' = 'cpu')
               AND i.scratch_bytes = (SELECT sum((r.value->>'scratch_bytes_total')::bigint)
                  FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                 WHERE r.value->>'kind' = 'cpu')
               AND i.allocatable_cpu_cores =
                   (SELECT sum((r.value->>'cpu_cores_allocatable')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.allocatable_memory_bytes =
                   (SELECT sum((r.value->>'memory_bytes_allocatable')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.allocatable_scratch_bytes =
                   (SELECT sum((r.value->>'scratch_bytes_allocatable')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.managed_cpu_cores =
                   (SELECT sum((r.value->>'cpu_cores_managed_occupied')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.managed_memory_bytes =
                   (SELECT sum((r.value->>'memory_bytes_managed_occupied')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.managed_scratch_bytes =
                   (SELECT sum((r.value->>'scratch_bytes_managed_occupied')::bigint)
                      FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'kind' = 'cpu')
               AND i.managed_cpu_cores = COALESCE((
                     SELECT sum(l.cpu_cores) FROM execution_resource_leases l
                      WHERE l.node_id = i.node_id AND l.acquired_at < i.received_at
                        AND (l.released_at IS NULL OR l.released_at > i.received_at)
                   ), 0)
               AND i.managed_memory_bytes = COALESCE((
                     SELECT sum(l.memory_bytes) FROM execution_resource_leases l
                      WHERE l.node_id = i.node_id AND l.acquired_at < i.received_at
                        AND (l.released_at IS NULL OR l.released_at > i.received_at)
                   ), 0)
               AND i.managed_scratch_bytes = COALESCE((
                     SELECT sum(l.scratch_bytes) FROM execution_resource_leases l
                      WHERE l.node_id = i.node_id AND l.acquired_at < i.received_at
                        AND (l.released_at IS NULL OR l.released_at > i.received_at)
                   ), 0)
          ) THEN
            RAISE EXCEPTION 'node current inventory pointer is cross-node or inconsistent'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_node_inventory_consistent
          AFTER INSERT OR UPDATE ON execution_nodes
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_current_inventory();
        CREATE CONSTRAINT TRIGGER trg_execution_inventory_node_consistent
          AFTER INSERT ON execution_inventory_attestations
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_current_inventory();
        CREATE CONSTRAINT TRIGGER trg_execution_resource_inventory_baseline_consistent
          AFTER INSERT OR UPDATE ON execution_resource_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_current_inventory();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_execution_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          max_attempt integer;
          expected_active text;
          head_row execution_heads%ROWTYPE;
        BEGIN
          SELECT * INTO head_row FROM execution_heads WHERE execution_id = NEW.execution_id;
          IF NOT FOUND THEN RETURN NULL; END IF;
          SELECT COALESCE(max(attempt_number),0),
                 max(attempt_id) FILTER (
                   WHERE status IN ('reserved','starting','running','reconciliation_required',
                                    'terminated','verifying')
                 )
            INTO max_attempt, expected_active
            FROM execution_attempts WHERE execution_id = NEW.execution_id;
          IF head_row.last_attempt_number <> max_attempt OR
             head_row.active_attempt_id IS DISTINCT FROM expected_active THEN
            RAISE EXCEPTION 'execution head differs from durable attempt lineage'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_head_consistent
          AFTER INSERT OR UPDATE ON execution_heads
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_execution_head();
        CREATE CONSTRAINT TRIGGER trg_execution_attempt_head_consistent
          AFTER INSERT OR UPDATE ON execution_attempts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_execution_head();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_attempt_bundle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          attempt_row execution_attempts%ROWTYPE;
          lease_row execution_resource_leases%ROWTYPE;
          reservation_row execution_budget_reservations%ROWTYPE;
          device_count bigint;
          head_attempt text;
        BEGIN
          SELECT * INTO attempt_row FROM execution_attempts WHERE attempt_id = NEW.attempt_id;
          IF NOT FOUND THEN RETURN NULL; END IF;

          IF NOT EXISTS (
            SELECT 1 FROM execution_qualification_admissions a
             WHERE a.admission_sha256 = attempt_row.admission_sha256
               AND a.infrastructure_attempt_id = attempt_row.attempt_id
               AND a.execution_id = attempt_row.execution_id
               AND a.intent_sha256 = attempt_row.intent_sha256
               AND a.grant_sha256 = attempt_row.grant_sha256
               AND a.bundle_sha256 = attempt_row.bundle_sha256
               AND a.cost_quote_sha256 = attempt_row.cost_quote_sha256
          ) THEN
            RAISE EXCEPTION 'attempt lacks its exact immutable qualification admission'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO lease_row FROM execution_resource_leases
           WHERE attempt_id = attempt_row.attempt_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'attempt lacks its resource lease' USING ERRCODE = '23514';
          END IF;
          IF lease_row.node_id <> attempt_row.node_id OR
             lease_row.inventory_sha256 <> attempt_row.node_inventory_sha256 OR
             lease_row.fencing_epoch <> attempt_row.fencing_epoch OR
             lease_row.lease_expires_at <> attempt_row.lease_expires_at THEN
            RAISE EXCEPTION 'attempt and resource lease authority fields diverge'
              USING ERRCODE = '23514';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM execution_qualification_admissions q
             WHERE q.admission_sha256 = attempt_row.admission_sha256
               AND lease_row.cpu_cores =
                   (q.bundle_json->'intent'->'resource_request'->>'cpu_cores')::integer
               AND lease_row.memory_bytes =
                   (q.bundle_json->'intent'->'resource_request'->>'memory_bytes')::bigint
               AND lease_row.scratch_bytes =
                   (q.bundle_json->'intent'->'resource_request'->>'scratch_bytes')::bigint
               AND lease_row.exclusive =
                   (q.bundle_json->'intent'->'resource_request'->>'exclusive')::boolean
               AND lease_row.accelerator_count =
                   (q.bundle_json->'intent'->'resource_request'
                    ->>'accelerator_count')::integer
               AND lease_row.lease_json->>'execution_id' = attempt_row.execution_id
               AND lease_row.lease_json->>'attempt_id' = attempt_row.attempt_id
               AND lease_row.lease_json->>'intent_sha256' = attempt_row.intent_sha256
               AND lease_row.lease_json->>'node_id' = attempt_row.node_id
               AND lease_row.lease_json->>'inventory_sha256' =
                   attempt_row.node_inventory_sha256
               AND lease_row.lease_json->'selected_resource_ids' =
                   q.bundle_json->'cost_quote'->'selected_resource_ids'
               AND (lease_row.lease_json->>'fencing_epoch_at_acquisition')::bigint =
                   attempt_row.fencing_epoch - attempt_row.adoption_count
               AND (lease_row.lease_json->>'acquired_at')::timestamptz =
                   lease_row.acquired_at
               AND (lease_row.lease_json->>'hard_deadline')::timestamptz =
                   attempt_row.hard_deadline
          ) THEN
            RAISE EXCEPTION 'resource lease differs from exact intent/quote payload'
              USING ERRCODE = '23514';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM execution_inventory_attestations i
              JOIN execution_nodes n ON n.node_id = attempt_row.node_id
              JOIN execution_qualification_admissions q
                ON q.admission_sha256 = attempt_row.admission_sha256
             WHERE i.inventory_sha256 = attempt_row.node_inventory_sha256
               AND i.node_id = attempt_row.node_id
               AND i.node_manifest_sha256 = n.node_manifest_sha256
               AND n.node_manifest_sha256 =
                   (q.bundle_json->'cost_quote'->>'selected_node_manifest_sha256')
               AND attempt_row.reserved_at >= i.received_at
               AND attempt_row.reserved_at < i.valid_until
               AND q.bundle_json->'intent'->'resource_request'->>'network_policy' = 'none'
               AND n.manifest_json->'network_policies' @> '["none"]'::jsonb
               AND q.bundle_json->'intent'->'resource_request'->>'data_locality' <>
                   'region_pinned'
               AND (
                 q.bundle_json->'intent'->'resource_request'->>'data_locality' = 'any'
                 OR (
                   q.bundle_json->'intent'->'resource_request'->>'data_locality' =
                     'site_pinned'
                   AND q.bundle_json->'intent'->'resource_request'->'locality_labels'
                       @> to_jsonb(ARRAY[n.site_id])
                 )
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(
                          q.bundle_json->'intent'->'expected_artifacts'
                        ) artifact(value)
                  WHERE NOT n.manifest_json->'allowed_data_classifications'
                            @> to_jsonb(ARRAY[artifact.value->>'data_classification'])
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(
                          q.bundle_json->'intent'->'input_artifact_bindings'
                        ) binding(value)
                  WHERE NOT EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(
                             q.bundle_json->'compilation_request'->'protocol'->'data_ports'
                           ) port(value)
                     WHERE port.value->>'port_id' = binding.value->>'input_port_id'
                       AND n.manifest_json->'allowed_data_classifications'
                           @> to_jsonb(ARRAY[port.value->>'data_classification'])
                  )
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements_text(
                          q.bundle_json->'cost_quote'->'selected_resource_ids'
                        ) selected(resource_id)
                  WHERE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(i.payload_json->'resources') r(value)
                     WHERE r.value->>'resource_id' = selected.resource_id
                       AND r.value->>'health' = 'healthy'
                       AND COALESCE((r.value->>'external_process_count')::bigint, 0) = 0
                       AND COALESCE(
                             (r.value->>'memory_bytes_external_occupied')::bigint, 0
                           ) = 0
                       AND COALESCE(
                             (r.value->>'scratch_bytes_external_occupied')::bigint, 0
                           ) = 0
                       AND COALESCE(
                             (r.value->>'accelerator_memory_bytes_external_occupied')::bigint,
                             0
                           ) = 0
                       AND EXISTS (
                         SELECT 1
                           FROM jsonb_array_elements_text(
                                  r.value->'resource_class_ids'
                                ) live(class_id)
                           JOIN jsonb_array_elements_text(
                                  q.bundle_json->'intent'->'resource_request'
                                    ->'accepted_resource_class_ids'
                                ) accepted(class_id)
                             ON accepted.class_id = live.class_id
                       )
                  )
               )
               AND (SELECT count(*)
                      FROM jsonb_array_elements_text(
                             q.bundle_json->'cost_quote'->'selected_resource_ids'
                           ) selected(resource_id)
                      JOIN LATERAL jsonb_array_elements(i.payload_json->'resources') r(value)
                        ON r.value->>'resource_id' = selected.resource_id
                     WHERE r.value->>'kind' = 'cpu') = 1
               AND (SELECT count(*)
                      FROM jsonb_array_elements_text(
                             q.bundle_json->'cost_quote'->'selected_resource_ids'
                           ) selected(resource_id)
                      JOIN LATERAL jsonb_array_elements(i.payload_json->'resources') r(value)
                        ON r.value->>'resource_id' = selected.resource_id
                     WHERE r.value->>'kind' = 'accelerator') =
                   lease_row.accelerator_count
               AND lease_row.cpu_cores <= (
                 SELECT (r.value->>'cpu_cores_allocatable')::bigint +
                        (r.value->>'cpu_cores_managed_occupied')::bigint
                   FROM jsonb_array_elements_text(
                          q.bundle_json->'cost_quote'->'selected_resource_ids'
                        ) selected(resource_id)
                   JOIN LATERAL jsonb_array_elements(i.payload_json->'resources') r(value)
                     ON r.value->>'resource_id' = selected.resource_id
                  WHERE r.value->>'kind' = 'cpu'
               )
               AND lease_row.memory_bytes <= (
                 SELECT (r.value->>'memory_bytes_allocatable')::bigint +
                        (r.value->>'memory_bytes_managed_occupied')::bigint
                   FROM jsonb_array_elements_text(
                          q.bundle_json->'cost_quote'->'selected_resource_ids'
                        ) selected(resource_id)
                   JOIN LATERAL jsonb_array_elements(i.payload_json->'resources') r(value)
                     ON r.value->>'resource_id' = selected.resource_id
                  WHERE r.value->>'kind' = 'cpu'
               )
               AND lease_row.scratch_bytes <= (
                 SELECT (r.value->>'scratch_bytes_allocatable')::bigint +
                        (r.value->>'scratch_bytes_managed_occupied')::bigint
                   FROM jsonb_array_elements_text(
                          q.bundle_json->'cost_quote'->'selected_resource_ids'
                        ) selected(resource_id)
                   JOIN LATERAL jsonb_array_elements(i.payload_json->'resources') r(value)
                     ON r.value->>'resource_id' = selected.resource_id
                  WHERE r.value->>'kind' = 'cpu'
               )
               AND (attempt_row.runtime_identity_json IS NULL OR (
                    attempt_row.runtime_identity_json->>'node_id' = attempt_row.node_id
                    AND attempt_row.runtime_identity_json->>'boot_id' = i.boot_id
                    AND attempt_row.runtime_identity_json->>'execution_id' =
                        attempt_row.execution_id
                    AND attempt_row.runtime_identity_json
                        ->>'infrastructure_attempt_id' = attempt_row.attempt_id
                    AND (attempt_row.runtime_identity_json
                         ->>'started_at')::timestamptz >= attempt_row.reserved_at
                    AND (attempt_row.runtime_identity_json
                         ->>'started_at')::timestamptz <= attempt_row.updated_at
                    AND (attempt_row.runtime_identity_json
                         ->>'started_monotonic_ns')::bigint >= i.observed_monotonic_ns
               ))
          ) THEN
            RAISE EXCEPTION 'attempt inventory/node differs from exact admitted placement'
              USING ERRCODE = '23514';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM execution_qualification_admissions q
             JOIN execution_nodes n ON n.node_id = attempt_row.node_id
             WHERE q.admission_sha256 = attempt_row.admission_sha256
               AND attempt_row.hard_deadline = attempt_row.reserved_at +
                   make_interval(secs =>
                     (q.bundle_json->'cost_quote'->>'maximum_lease_seconds')::integer)
               AND attempt_row.hard_deadline <=
                   (q.bundle_json->'intent'->>'deadline')::timestamptz
               AND attempt_row.hard_deadline <=
                   (q.bundle_json->'cost_quote'->>'expires_at')::timestamptz
               AND attempt_row.hard_deadline <=
                   (q.bundle_json->'budget_authorization'->>'expires_at')::timestamptz
               AND attempt_row.hard_deadline <=
                   (q.grant_json->'message'->>'expires_at')::timestamptz
               AND attempt_row.hard_deadline <=
                   (n.node_enrollment_json->'message'->>'expires_at')::timestamptz
               AND attempt_row.hard_deadline <=
                   (n.node_authority_pin_json->>'expires_at')::timestamptz
               AND (n.node_authority_pin_json->>'revoked_at' IS NULL OR
                    attempt_row.hard_deadline <=
                    (n.node_authority_pin_json->>'revoked_at')::timestamptz)
               AND attempt_row.hard_deadline <=
                   (n.manifest_json->>'key_expires_at')::timestamptz
               AND (n.manifest_json->>'key_revoked_at' IS NULL OR
                    attempt_row.hard_deadline <=
                    (n.manifest_json->>'key_revoked_at')::timestamptz)
          ) THEN
            RAISE EXCEPTION 'attempt lease deadline exceeds its exact grant/quote/budget window'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO reservation_row FROM execution_budget_reservations
           WHERE attempt_id = attempt_row.attempt_id;
          IF NOT FOUND OR NOT EXISTS (
            SELECT 1 FROM execution_budget_events e
             WHERE e.reservation_id = reservation_row.reservation_id
               AND e.authorization_sha256 = reservation_row.authorization_sha256
               AND e.sequence = 1
               AND e.event_type = 'reserved'
               AND e.reserved_delta_microunits = reservation_row.held_microunits
               AND e.spent_delta_microunits = 0
          ) THEN
            RAISE EXCEPTION 'attempt lacks its budget reservation/event' USING ERRCODE = '23514';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM execution_budget_reservations r
             JOIN execution_qualification_admissions q
               ON q.admission_sha256 = attempt_row.admission_sha256
             WHERE r.attempt_id = attempt_row.attempt_id
               AND r.execution_id = attempt_row.execution_id
               AND r.cost_quote_sha256 = attempt_row.cost_quote_sha256
               AND r.authorization_sha256 = q.budget_authorization_sha256
               AND r.currency_code = q.bundle_json->'cost_quote'->>'currency_code'
               AND r.fixed_charge_microunits =
                   (q.bundle_json->'cost_quote'->>'fixed_charge_microunits')::bigint
               AND r.charge_per_second_microunits =
                   (q.bundle_json->'cost_quote'->>'charge_per_second_microunits')::bigint
               AND r.maximum_lease_seconds =
                   (q.bundle_json->'cost_quote'->>'maximum_lease_seconds')::bigint
               AND r.held_microunits =
                   (q.bundle_json->'cost_quote'->>'maximum_charge_microunits')::bigint
          ) THEN
            RAISE EXCEPTION 'budget reservation differs from exact admitted quote'
              USING ERRCODE = '23514';
          END IF;
          SELECT count(*) INTO device_count FROM execution_device_leases
           WHERE attempt_id = attempt_row.attempt_id;
          IF device_count <> lease_row.accelerator_count THEN
            RAISE EXCEPTION 'attempt device lease count differs from exact resource lease'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_device_leases d
             WHERE d.attempt_id = attempt_row.attempt_id
               AND (d.resource_lease_id <> lease_row.lease_id
                    OR d.node_id <> attempt_row.node_id
                    OR d.fencing_epoch <> attempt_row.fencing_epoch)
          ) THEN
            RAISE EXCEPTION 'device lease differs from exact attempt/resource fence'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_device_leases d
             LEFT JOIN execution_inventory_devices i
               ON i.inventory_sha256 = attempt_row.node_inventory_sha256
              AND i.node_id = attempt_row.node_id
              AND i.device_id = d.device_id
              AND i.hardware_uuid = d.hardware_uuid
             LEFT JOIN execution_device_heads h
               ON h.node_id = d.node_id AND h.device_id = d.device_id
             WHERE d.attempt_id = attempt_row.attempt_id
               AND (i.device_id IS NULL OR NOT i.healthy OR i.external_occupancy
                    OR i.managed_memory_bytes <> 0
                    OR d.requested_memory_bytes > i.allocatable_memory_bytes
                    OR h.device_id IS NULL
                    OR h.hardware_uuid <> d.hardware_uuid)
          ) OR EXISTS (
            SELECT 1
              FROM jsonb_array_elements_text(
                     (SELECT q.bundle_json->'cost_quote'->'selected_resource_ids'
                        FROM execution_qualification_admissions q
                       WHERE q.admission_sha256 = attempt_row.admission_sha256)
                   ) selected(resource_id)
              JOIN execution_inventory_devices i
                ON i.inventory_sha256 = attempt_row.node_inventory_sha256
               AND i.device_id = selected.resource_id
             WHERE NOT EXISTS (
               SELECT 1 FROM execution_device_leases d
                WHERE d.attempt_id = attempt_row.attempt_id
                  AND d.device_id = selected.resource_id
             )
          ) OR EXISTS (
            SELECT 1 FROM execution_device_leases d
             WHERE d.attempt_id = attempt_row.attempt_id
               AND NOT EXISTS (
                 SELECT 1
                   FROM execution_qualification_admissions q,
                        jsonb_array_elements_text(
                          q.bundle_json->'cost_quote'->'selected_resource_ids'
                        ) selected(resource_id)
                  WHERE q.admission_sha256 = attempt_row.admission_sha256
                    AND selected.resource_id = d.device_id
               )
          ) THEN
            RAISE EXCEPTION 'device leases differ from exact inventory/quote placement'
              USING ERRCODE = '23514';
          END IF;
          SELECT active_attempt_id INTO head_attempt FROM execution_heads
           WHERE execution_id = attempt_row.execution_id;

          IF attempt_row.status = 'reconciliation_required' THEN
            IF lease_row.state <> 'reconciliation_required' OR
               reservation_row.state <> 'reconciliation_required' OR
               EXISTS (SELECT 1 FROM execution_device_leases d
                        WHERE d.attempt_id = attempt_row.attempt_id
                          AND d.state <> 'reconciliation_required') OR
               head_attempt IS DISTINCT FROM attempt_row.attempt_id OR
               attempt_row.terminal_receipt_sha256 IS NOT NULL THEN
              RAISE EXCEPTION 'reconciliation must retain every resource and authority hold'
                USING ERRCODE = '23514';
            END IF;
          ELSIF attempt_row.status IN ('succeeded','failed','cancelled') THEN
            IF lease_row.state <> 'released' OR reservation_row.state NOT IN ('settled','released') OR
               EXISTS (SELECT 1 FROM execution_device_leases d
                        WHERE d.attempt_id = attempt_row.attempt_id AND d.state <> 'released') OR
               head_attempt IS NOT NULL OR attempt_row.terminal_receipt_sha256 IS NULL OR
               NOT EXISTS (
                 SELECT 1 FROM execution_terminal_receipts r
                  JOIN execution_outbox o ON o.receipt_sha256 = r.receipt_sha256
                  WHERE r.attempt_id = attempt_row.attempt_id
                    AND r.receipt_sha256 = attempt_row.terminal_receipt_sha256
               ) THEN
              RAISE EXCEPTION 'terminal attempt lacks atomic receipt/outbox or released holds'
                USING ERRCODE = '23514';
            END IF;
            IF reservation_row.state = 'settled' AND NOT EXISTS (
              SELECT 1 FROM execution_budget_events e
               WHERE e.reservation_id = reservation_row.reservation_id
                 AND e.sequence = (SELECT max(x.sequence) FROM execution_budget_events x
                                    WHERE x.reservation_id = reservation_row.reservation_id)
                 AND e.event_type = 'settled'
                 AND e.reserved_delta_microunits = -reservation_row.held_microunits
                 AND e.spent_delta_microunits = reservation_row.settled_microunits
                 AND (e.payload_json->'details'->>'cost_quote_sha256') =
                     reservation_row.cost_quote_sha256
                 AND (e.payload_json->'details'->>'fixed_charge_microunits')::bigint =
                     reservation_row.fixed_charge_microunits
                 AND (e.payload_json->'details'->>'charge_per_second_microunits')::bigint =
                     reservation_row.charge_per_second_microunits
                 AND (e.payload_json->'details'->>'actual_lease_seconds')::bigint =
                     reservation_row.actual_lease_seconds
                 AND (e.payload_json->'details'->>'charged_microunits')::bigint =
                     reservation_row.settled_microunits
            ) THEN
              RAISE EXCEPTION 'terminal budget settlement lacks exact calculation event'
                USING ERRCODE = '23514';
            END IF;
          ELSE
            IF lease_row.state <> 'held' OR reservation_row.state <> 'held' OR
               EXISTS (SELECT 1 FROM execution_device_leases d
                        WHERE d.attempt_id = attempt_row.attempt_id AND d.state <> 'held') OR
               head_attempt IS DISTINCT FROM attempt_row.attempt_id OR
               attempt_row.terminal_receipt_sha256 IS NOT NULL THEN
              RAISE EXCEPTION 'active attempt authority bundle is incomplete'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_attempt_bundle_complete
          AFTER INSERT OR UPDATE ON execution_attempts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_attempt_bundle();
        CREATE CONSTRAINT TRIGGER trg_execution_resource_attempt_bundle_complete
          AFTER INSERT OR UPDATE ON execution_resource_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_attempt_bundle();
        CREATE CONSTRAINT TRIGGER trg_execution_device_attempt_bundle_complete
          AFTER INSERT OR UPDATE ON execution_device_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_attempt_bundle();
        CREATE CONSTRAINT TRIGGER trg_execution_budget_attempt_bundle_complete
          AFTER INSERT OR UPDATE ON execution_budget_reservations
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_attempt_bundle();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_adoption() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM execution_attempts a
             WHERE a.attempt_id = NEW.attempt_id
               AND a.latest_adoption_sha256 = NEW.adoption_sha256
               AND a.adoption_count = NEW.sequence
               AND a.fencing_epoch = NEW.new_fencing_epoch
               AND a.lease_token_sha256 = NEW.new_lease_token_sha256
               AND a.runtime_identity_sha256 = NEW.runtime_identity_sha256
               AND a.last_runtime_inspection_sequence =
                   (NEW.payload_json->'runtime_inspection_receipt'
                    ->>'inspection_sequence')::bigint
               AND a.last_runtime_inspection_sha256 =
                   (NEW.payload_json->>'runtime_inspection_receipt_sha256')
               AND a.last_runtime_inspected_at =
                   (NEW.payload_json->'runtime_inspection_receipt'
                    ->>'inspected_at')::timestamptz
               AND a.last_runtime_inspected_monotonic_ns =
                   (NEW.payload_json->'runtime_inspection_receipt'
                    ->>'inspected_monotonic_ns')::bigint
               AND a.status = 'running'
          ) THEN
            RAISE EXCEPTION 'adoption receipt must atomically rotate its exact attempt fence/token'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_adoption_complete
          AFTER INSERT ON execution_attempt_adoptions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_adoption();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_terminal_outbox() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM execution_attempts a
             JOIN execution_heads h ON h.execution_id = a.execution_id
             JOIN execution_resource_leases l ON l.attempt_id = a.attempt_id
             JOIN execution_budget_reservations b ON b.attempt_id = a.attempt_id
             JOIN execution_qualification_admissions q
               ON q.admission_sha256 = a.admission_sha256
             JOIN execution_outbox o ON o.receipt_sha256 = NEW.receipt_sha256
             WHERE a.attempt_id = NEW.attempt_id
               AND a.execution_id = NEW.execution_id
               AND a.intent_sha256 = NEW.intent_sha256
               AND a.terminal_receipt_sha256 = NEW.receipt_sha256
               AND l.lease_sha256 = NEW.resource_lease_sha256
               AND NEW.payload_sha256 = NEW.receipt_sha256
               AND NEW.node_execution_receipt_sha256 =
                   (NEW.payload_json->>'node_execution_receipt_sha256')
               AND NEW.payload_json->'intent' = a.intent_json
               AND (NEW.payload_json->>'resource_lease_sha256') =
                   NEW.resource_lease_sha256
               AND (NEW.payload_json->>'terminal_state') = NEW.terminal_state
               AND (NEW.payload_json->>'verified_by_principal_id') =
                   NEW.committed_by_principal_id
               AND NEW.committed_at >=
                   (NEW.payload_json->>'verified_at')::timestamptz
               AND NEW.terminal_verification_attestation_json->>'schema_name' =
                   'aletheia.terminal_verification_attestation'
               AND NEW.terminal_verification_attestation_json->'message'->>'schema_name' =
                   'aletheia.terminal_verification_attestation_message'
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'execution_receipt_sha256' = NEW.receipt_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'node_execution_receipt_sha256' =
                   NEW.node_execution_receipt_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'intent_sha256' = NEW.intent_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'execution_id' = NEW.execution_id
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'infrastructure_attempt_id' = NEW.attempt_id
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'worker_node_manifest_sha256' =
                   NEW.payload_json->>'worker_node_manifest_sha256'
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'node_inventory_sha256' = a.node_inventory_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'resource_lease_sha256' = NEW.resource_lease_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'artifact_manifest_sha256' = NEW.artifact_manifest_sha256
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'terminal_state' = NEW.terminal_state
               AND NEW.terminal_verification_attestation_json->'message'
                   ->>'verified_by_principal_id' = NEW.committed_by_principal_id
               AND (NEW.terminal_verification_attestation_json->'message'
                    ->>'verified_at')::timestamptz =
                   (NEW.payload_json->>'verified_at')::timestamptz
               AND (NEW.terminal_verification_attestation_json->'message'
                    ->>'expires_at')::timestamptz > NEW.committed_at
               AND (NEW.terminal_verification_attestation_json->'message'
                    ->>'qualification_only')::boolean
               AND NOT (NEW.terminal_verification_attestation_json->'message'
                        ->>'scientific_admission_allowed')::boolean
               AND NEW.terminal_verification_attestation_json->'message'
                   ->'artifact_verified_receipt_sha256s' = COALESCE(
                 (SELECT jsonb_agg(item.value ORDER BY item.value)
                    FROM jsonb_array_elements_text(
                           NEW.artifact_verified_receipt_sha256s_json
                         ) item(value)),
                 '[]'::jsonb
               )
               AND NEW.terminal_verification_policy_sha256 =
                   NEW.terminal_verification_attestation_json->'message'
                     ->>'terminal_verification_policy_sha256'
               AND NEW.terminal_verification_key_id =
                   NEW.terminal_verification_attestation_json->'message'
                     ->>'verification_key_id'
               AND NEW.terminal_verification_authority_pin_json->>'policy_sha256' =
                   NEW.terminal_verification_policy_sha256
               AND NEW.terminal_verification_authority_pin_json->>'key_id' =
                   NEW.terminal_verification_key_id
               AND NEW.terminal_verification_authority_pin_json->>'principal_id' =
                   NEW.committed_by_principal_id
               AND (NEW.terminal_verification_authority_pin_json
                    ->>'valid_from')::timestamptz <= NEW.committed_at
               AND NEW.committed_at < LEAST(
                 (NEW.terminal_verification_authority_pin_json
                  ->>'expires_at')::timestamptz,
                 COALESCE(
                   (NEW.terminal_verification_authority_pin_json
                    ->>'revoked_at')::timestamptz,
                   (NEW.terminal_verification_authority_pin_json
                    ->>'expires_at')::timestamptz
                 )
               )
               AND NEW.artifact_manifest_json = NEW.payload_json->'artifact_manifest'
               AND NEW.artifact_verified_receipt_sha256s_json = COALESCE(
                 (SELECT jsonb_agg(item.value->>'verified_receipt_sha256'
                                   ORDER BY item.ordinality)
                    FROM jsonb_array_elements(
                           NEW.payload_json->'artifact_verified_receipts'
                         ) WITH ORDINALITY AS item(value, ordinality)),
                 '[]'::jsonb
               )
               AND COALESCE(
                 (SELECT jsonb_agg(item.value->'artifact'->>'artifact_key'
                                   ORDER BY item.ordinality)
                    FROM jsonb_array_elements(
                           NEW.payload_json->'artifact_verified_receipts'
                         ) WITH ORDINALITY AS item(value, ordinality)),
                 '[]'::jsonb
               ) = COALESCE(
                 (SELECT jsonb_agg(item.value->>'artifact_key' ORDER BY item.ordinality)
                    FROM jsonb_array_elements(NEW.artifact_manifest_json->'entries')
                         WITH ORDINALITY AS item(value, ordinality)),
                 '[]'::jsonb
               )
               AND NEW.artifact_manifest_json->>'intent_sha256' = NEW.intent_sha256
               AND NEW.artifact_manifest_json->>'execution_id' = NEW.execution_id
               AND NEW.artifact_manifest_json->>'replicate_slot_id' =
                   h.replicate_slot_id
               AND NEW.artifact_manifest_json->>'infrastructure_attempt_id' = NEW.attempt_id
               AND (NEW.artifact_manifest_json->>'produced_at')::timestamptz >=
                   (NEW.node_execution_receipt_json->>'started_at')::timestamptz
               AND (NEW.artifact_manifest_json->>'produced_at')::timestamptz <=
                   (NEW.payload_json->>'verified_at')::timestamptz
               AND jsonb_array_length(NEW.artifact_manifest_json->'entries') =
                   (SELECT count(DISTINCT item.value->>'artifact_key')
                      FROM jsonb_array_elements(
                             NEW.artifact_manifest_json->'entries'
                           ) item(value))
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(
                          NEW.payload_json->'artifact_verified_receipts'
                        ) verified(value)
                  WHERE verified.value->>'artifact_manifest_sha256' IS DISTINCT FROM
                        NEW.artifact_manifest_sha256
                     OR verified.value->>'producer_attempt_id' IS DISTINCT FROM NEW.attempt_id
                     OR NOT EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(
                                NEW.artifact_manifest_json->'entries'
                              ) actual(value)
                        WHERE actual.value = verified.value->'artifact'
                     )
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(
                          NEW.artifact_manifest_json->'entries'
                        ) actual(value)
                  WHERE NOT EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(
                             q.bundle_json->'intent'->'expected_artifacts'
                           ) expected(value)
                     WHERE expected.value->>'artifact_key' =
                           actual.value->>'artifact_key'
                       AND expected.value->>'role' = actual.value->>'role'
                       AND expected.value->>'media_type' = actual.value->>'media_type'
                       AND expected.value->'schema_sha256' IS NOT DISTINCT FROM
                           actual.value->'schema_sha256'
                       AND (actual.value->>'bytes')::bigint <=
                           (expected.value->>'max_bytes')::bigint
                  )
               )
               AND COALESCE(
                     (SELECT sum((item.value->>'bytes')::bigint)
                        FROM jsonb_array_elements(
                               NEW.artifact_manifest_json->'entries'
                             ) item(value)),
                     0
                   ) <=
                   (q.bundle_json->'intent'->'resource_request'
                    ->>'artifact_quota_bytes')::bigint
               AND (NEW.node_execution_receipt_json->>'artifact_manifest_sha256') =
                   NEW.artifact_manifest_sha256
               AND (NEW.node_execution_receipt_json->>'intent_sha256') = NEW.intent_sha256
               AND (NEW.node_execution_receipt_json->>'execution_id') = NEW.execution_id
               AND (NEW.node_execution_receipt_json->>'infrastructure_attempt_id') = NEW.attempt_id
               AND (NEW.node_execution_receipt_json->>'resource_lease_sha256') =
                   NEW.resource_lease_sha256
               AND (NEW.node_execution_receipt_json->>'node_inventory_sha256') =
                   a.node_inventory_sha256
               AND (NEW.node_execution_receipt_json->>'node_manifest_sha256') =
                   (NEW.payload_json->>'worker_node_manifest_sha256')
               AND (NEW.node_execution_receipt_json->>'runtime_identity_sha256') =
                   a.runtime_identity_sha256
               AND NEW.node_execution_receipt_json->'runtime_identity' =
                   a.runtime_identity_json
               AND (NEW.node_execution_receipt_json->>'started_monotonic_ns')::bigint =
                   (a.runtime_identity_json->>'started_monotonic_ns')::bigint
               AND NEW.node_execution_receipt_json->'termination_inspection_receipt'
                   ->'runtime_identity' = a.runtime_identity_json
               AND NEW.node_execution_receipt_json->'termination_inspection_receipt'
                   ->>'runtime_identity_sha256' = a.runtime_identity_sha256
               AND (NEW.node_execution_receipt_json->>'started_at') =
                   (NEW.payload_json->>'started_at')
               AND (NEW.node_execution_receipt_json->>'ended_at') =
                   (NEW.payload_json->>'ended_at')
               AND CASE
                 WHEN (NEW.node_execution_receipt_json->>'exit_code')::integer = 0
                      AND (NEW.node_execution_receipt_json->>'ended_at')::timestamptz <=
                          a.hard_deadline
                      AND NOT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements(
                                 q.bundle_json->'intent'->'expected_artifacts'
                               ) expected(value)
                         WHERE (expected.value->>'required')::boolean
                           AND NOT EXISTS (
                             SELECT 1 FROM jsonb_array_elements(
                               NEW.artifact_manifest_json->'entries'
                             ) actual(value)
                              WHERE actual.value->>'artifact_key' =
                                    expected.value->>'artifact_key'
                           )
                      )
                   THEN NEW.terminal_state = 'engineering_succeeded'
                 WHEN (NEW.node_execution_receipt_json->>'exit_code')::integer = 0
                      AND (NEW.node_execution_receipt_json->>'ended_at')::timestamptz >
                          a.hard_deadline
                   THEN NEW.terminal_state = 'execution_failed'
                        AND NEW.payload_json->'failure'->>'category' = 'timeout'
                 WHEN (NEW.node_execution_receipt_json->>'exit_code')::integer = 0
                   THEN NEW.terminal_state = 'execution_failed'
                        AND NEW.payload_json->'failure'->>'category' = 'invalid_output'
                 ELSE NEW.terminal_state <> 'engineering_succeeded'
               END
               AND (NEW.node_execution_receipt_json->>'started_at')::timestamptz >=
                   a.reserved_at
               AND (NEW.terminal_state <> 'engineering_succeeded' OR
                    (NEW.node_execution_receipt_json->>'ended_at')::timestamptz <=
                    a.hard_deadline)
               AND NEW.committed_at >= l.acquired_at
               AND b.actual_lease_seconds = least(
                   b.maximum_lease_seconds,
                   ceil(extract(epoch FROM (NEW.committed_at - l.acquired_at)))::bigint
               )
               AND b.settled_microunits = b.fixed_charge_microunits +
                   (b.charge_per_second_microunits * b.actual_lease_seconds)
               AND (NEW.node_execution_receipt_json->>'fencing_epoch')::bigint = a.fencing_epoch
               AND (NEW.node_execution_receipt_json->>'lease_token_sha256') =
                   a.lease_token_sha256
               AND a.last_runtime_inspection_sequence =
                   (NEW.node_execution_receipt_json->'termination_inspection_receipt'
                    ->>'inspection_sequence')::bigint
               AND a.last_runtime_inspection_sha256 =
                   (NEW.node_execution_receipt_json
                    ->>'termination_inspection_receipt_sha256')
               AND a.last_runtime_inspected_at =
                   (NEW.node_execution_receipt_json->'termination_inspection_receipt'
                    ->>'inspected_at')::timestamptz
               AND a.last_runtime_inspected_monotonic_ns =
                   (NEW.node_execution_receipt_json->'termination_inspection_receipt'
                    ->>'inspected_monotonic_ns')::bigint
               AND a.status = CASE NEW.terminal_state
                 WHEN 'engineering_succeeded' THEN 'succeeded'
                 WHEN 'execution_failed' THEN 'failed'
                 WHEN 'cancelled' THEN 'cancelled'
               END
               AND o.execution_id = NEW.execution_id
               AND o.attempt_id = NEW.attempt_id
               AND o.topic = 'execution.terminal.v1'
               AND o.delivery_key = 'execution:' || NEW.execution_id || ':' || NEW.attempt_id
               AND o.payload_sha256 = NEW.payload_sha256
               AND o.payload_json = NEW.payload_json
          ) THEN
            RAISE EXCEPTION 'terminal receipt and outbox must commit with terminal attempt'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_terminal_outbox_complete
          AFTER INSERT ON execution_terminal_receipts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_terminal_outbox();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_budget_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          auth_hash text := COALESCE(NEW.authorization_sha256, OLD.authorization_sha256);
          expected_reserved bigint;
          expected_spent bigint;
          head_reserved bigint;
          head_spent bigint;
          head_cap bigint;
          authority_cap bigint;
          head_currency text;
          authority_currency text;
        BEGIN
          SELECT COALESCE(sum(CASE WHEN state IN ('held','reconciliation_required')
                                  THEN held_microunits ELSE 0 END), 0),
                 COALESCE(sum(CASE WHEN state = 'settled' THEN settled_microunits ELSE 0 END), 0)
            INTO expected_reserved, expected_spent
            FROM execution_budget_reservations WHERE authorization_sha256 = auth_hash;
          SELECT reserved_microunits, spent_microunits, cap_microunits, currency_code
            INTO head_reserved, head_spent, head_cap, head_currency
            FROM execution_budget_heads WHERE authorization_sha256 = auth_hash;
          SELECT cap_microunits, currency_code INTO authority_cap, authority_currency
            FROM execution_budget_authorizations WHERE authorization_sha256 = auth_hash;
          IF head_reserved IS DISTINCT FROM expected_reserved OR
             head_spent IS DISTINCT FROM expected_spent OR
             head_cap IS DISTINCT FROM authority_cap OR
             head_currency IS DISTINCT FROM authority_currency THEN
            RAISE EXCEPTION 'budget head differs from durable reservations' USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_budget_head_consistent
          AFTER INSERT OR UPDATE ON execution_budget_heads
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_budget_head();
        CREATE CONSTRAINT TRIGGER trg_execution_budget_reservation_consistent
          AFTER INSERT OR UPDATE ON execution_budget_reservations
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_budget_head();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_budget_events() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          target_reservation text;
          reservation_row execution_budget_reservations%ROWTYPE;
          attempt_row execution_attempts%ROWTYPE;
          event_count bigint;
          reserved_count bigint;
          reconciliation_count bigint;
          adoption_count bigint;
          settled_count bigint;
          released_count bigint;
          reserved_sum bigint;
          spent_sum bigint;
          last_type text;
          expected_reserved bigint;
          expected_spent bigint;
          expected_last_type text;
        BEGIN
          IF TG_TABLE_NAME = 'execution_attempts' THEN
            SELECT reservation_id INTO target_reservation
              FROM execution_budget_reservations
             WHERE attempt_id = NEW.attempt_id;
          ELSE
            target_reservation := to_jsonb(NEW)->>'reservation_id';
          END IF;
          IF target_reservation IS NULL THEN RETURN NULL; END IF;
          SELECT * INTO reservation_row FROM execution_budget_reservations
           WHERE reservation_id = target_reservation;
          IF NOT FOUND THEN RETURN NULL; END IF;
          SELECT * INTO attempt_row FROM execution_attempts
           WHERE attempt_id = reservation_row.attempt_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'budget event ledger lacks its exact attempt'
              USING ERRCODE = '23514';
          END IF;

          SELECT count(*), count(*) FILTER (WHERE event_type = 'reserved'),
                 count(*) FILTER (WHERE event_type = 'reconciliation_required'),
                 count(*) FILTER (WHERE event_type = 'adopted'),
                 count(*) FILTER (WHERE event_type = 'settled'),
                 count(*) FILTER (WHERE event_type = 'released'),
                 COALESCE(sum(reserved_delta_microunits), 0),
                 COALESCE(sum(spent_delta_microunits), 0)
            INTO event_count, reserved_count, reconciliation_count, adoption_count,
                 settled_count, released_count, reserved_sum, spent_sum
            FROM execution_budget_events WHERE reservation_id = target_reservation;
          SELECT event_type INTO last_type FROM execution_budget_events
           WHERE reservation_id = target_reservation ORDER BY sequence DESC LIMIT 1;
          expected_reserved := CASE
            WHEN reservation_row.state IN ('held','reconciliation_required')
              THEN reservation_row.held_microunits ELSE 0 END;
          expected_spent := CASE WHEN reservation_row.state = 'settled'
            THEN reservation_row.settled_microunits ELSE 0 END;
          expected_last_type := CASE reservation_row.state
            WHEN 'held' THEN CASE WHEN attempt_row.adoption_count = 0
                               THEN 'reserved' ELSE 'adopted' END
            WHEN 'reconciliation_required' THEN 'reconciliation_required'
            WHEN 'settled' THEN 'settled'
            WHEN 'released' THEN 'released'
          END;

          IF event_count = 0 OR reserved_count <> 1 OR adoption_count <>
             attempt_row.adoption_count OR
             reconciliation_count > adoption_count + 1 OR settled_count > 1 OR
             released_count > 1 OR settled_count + released_count > 1 OR
             EXISTS (
               SELECT 1 FROM execution_budget_events e
                WHERE e.reservation_id = target_reservation
                  AND (e.authorization_sha256 <> reservation_row.authorization_sha256
                       OR e.sequence < 1
                       OR e.payload_json->>'reservation_id' IS DISTINCT FROM e.reservation_id
                       OR e.payload_json->>'authorization_sha256' IS DISTINCT FROM
                          e.authorization_sha256
                       OR (e.payload_json->>'sequence')::bigint IS DISTINCT FROM e.sequence
                       OR e.payload_json->>'previous_event_sha256' IS DISTINCT FROM
                          e.previous_event_sha256
                       OR e.payload_json->>'event_type' IS DISTINCT FROM e.event_type
                       OR (e.payload_json->>'reserved_delta_microunits')::bigint IS DISTINCT FROM
                          e.reserved_delta_microunits
                       OR (e.payload_json->>'spent_delta_microunits')::bigint IS DISTINCT FROM
                          e.spent_delta_microunits
                       OR (e.payload_json->>'recorded_at')::timestamptz IS DISTINCT FROM
                          e.recorded_at
                       OR e.recorded_at < reservation_row.reserved_at)
             ) OR EXISTS (
               SELECT 1 FROM execution_budget_events e
                LEFT JOIN execution_budget_events p
                  ON p.reservation_id = e.reservation_id AND p.sequence = e.sequence - 1
               WHERE e.reservation_id = target_reservation
                 AND ((e.sequence = 1 AND
                       (e.event_type <> 'reserved' OR e.previous_event_sha256 IS NOT NULL OR
                        e.reserved_delta_microunits <> reservation_row.held_microunits OR
                        e.spent_delta_microunits <> 0 OR
                        e.recorded_at <> reservation_row.reserved_at))
                      OR (e.sequence > 1 AND
                          (p.event_sha256 IS NULL OR
                           e.previous_event_sha256 <> p.event_sha256 OR
                           e.recorded_at < p.recorded_at))
                      OR (e.event_type = 'reconciliation_required' AND
                          (e.sequence = 1 OR p.event_type NOT IN ('reserved','adopted') OR
                           e.reserved_delta_microunits <> 0 OR
                           e.spent_delta_microunits <> 0))
                      OR (e.event_type = 'adopted' AND
                          (p.event_type NOT IN
                             ('reserved','adopted','reconciliation_required') OR
                           e.reserved_delta_microunits <> 0 OR
                           e.spent_delta_microunits <> 0))
                      OR (e.event_type = 'settled' AND
                          (e.reserved_delta_microunits <> -reservation_row.held_microunits OR
                           e.spent_delta_microunits <> reservation_row.settled_microunits OR
                           e.recorded_at <> reservation_row.settled_at))
                      OR (e.event_type = 'released' AND
                          (e.reserved_delta_microunits <> -reservation_row.held_microunits OR
                           e.spent_delta_microunits <> 0 OR
                           e.recorded_at <> reservation_row.settled_at)))
             ) OR event_count <> (SELECT max(sequence) FROM execution_budget_events
                                   WHERE reservation_id = target_reservation) OR
             reserved_sum <> expected_reserved OR spent_sum <> expected_spent OR
             last_type <> expected_last_type THEN
            RAISE EXCEPTION 'budget event ledger differs from reservation/attempt authority'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_budget_event_ledger_complete
          AFTER INSERT ON execution_budget_events
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_budget_events();
        CREATE CONSTRAINT TRIGGER trg_execution_budget_reservation_event_ledger_complete
          AFTER INSERT OR UPDATE ON execution_budget_reservations
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_budget_events();
        CREATE CONSTRAINT TRIGGER trg_execution_attempt_budget_event_ledger_complete
          AFTER INSERT OR UPDATE ON execution_attempts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_budget_events();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_node_capacity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          target_node text := COALESCE(NEW.node_id, OLD.node_id);
          expected_cpu bigint;
          expected_memory bigint;
          expected_scratch bigint;
          expected_exclusive text;
          active_count bigint;
          exclusive_count bigint;
          actual_cpu bigint;
          actual_memory bigint;
          actual_scratch bigint;
          actual_exclusive text;
          capacity_cpu bigint;
          capacity_memory bigint;
          capacity_scratch bigint;
        BEGIN
          SELECT COALESCE(sum(cpu_cores),0), COALESCE(sum(memory_bytes),0),
                 COALESCE(sum(scratch_bytes),0),
                 max(lease_id) FILTER (WHERE exclusive), count(*),
                 count(*) FILTER (WHERE exclusive)
            INTO expected_cpu, expected_memory, expected_scratch, expected_exclusive,
                 active_count, exclusive_count
            FROM execution_resource_leases
           WHERE node_id = target_node AND state IN ('held','reconciliation_required');
          SELECT reserved_cpu_cores, reserved_memory_bytes, reserved_scratch_bytes,
                 exclusive_lease_id
            INTO actual_cpu, actual_memory, actual_scratch, actual_exclusive
            FROM execution_nodes WHERE node_id = target_node;
          SELECT i.allocatable_cpu_cores + i.managed_cpu_cores,
                 i.allocatable_memory_bytes + i.managed_memory_bytes,
                 i.allocatable_scratch_bytes + i.managed_scratch_bytes
            INTO capacity_cpu, capacity_memory, capacity_scratch
            FROM execution_nodes n
            LEFT JOIN execution_inventory_attestations i
              ON i.inventory_sha256 = n.current_inventory_sha256
             AND i.node_id = n.node_id
           WHERE n.node_id = target_node;
          IF (actual_cpu,actual_memory,actual_scratch,actual_exclusive) IS DISTINCT FROM
             (expected_cpu,expected_memory,expected_scratch,expected_exclusive) THEN
            RAISE EXCEPTION 'node capacity head differs from active/reconciliation leases'
              USING ERRCODE = '23514';
          END IF;
          IF exclusive_count > 1 OR (exclusive_count = 1 AND active_count <> 1) THEN
            RAISE EXCEPTION 'exclusive lease cannot coexist with another retained lease'
              USING ERRCODE = '23514';
          END IF;
          IF (capacity_cpu IS NULL AND active_count <> 0) OR
             actual_cpu > COALESCE(capacity_cpu, 0) OR
             actual_memory > COALESCE(capacity_memory, 0) OR
             actual_scratch > COALESCE(capacity_scratch, 0) THEN
            RAISE EXCEPTION 'retained resource leases exceed current signed inventory capacity'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_node_capacity_consistent
          AFTER INSERT OR UPDATE ON execution_nodes
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_node_capacity();
        CREATE CONSTRAINT TRIGGER trg_execution_resource_capacity_consistent
          AFTER INSERT OR UPDATE ON execution_resource_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_node_capacity();
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_device_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          target_node text := COALESCE(NEW.node_id, OLD.node_id);
          target_device text := COALESCE(NEW.device_id, OLD.device_id);
          active_id text;
          fence bigint;
          lease_id text;
          lease_fence bigint;
        BEGIN
          SELECT active_device_lease_id, fencing_counter INTO active_id, fence
            FROM execution_device_heads
           WHERE node_id = target_node AND device_id = target_device;
          SELECT device_lease_id, fencing_epoch INTO lease_id, lease_fence
            FROM execution_device_leases
           WHERE node_id = target_node AND device_id = target_device
             AND state IN ('held','reconciliation_required');
          IF NOT EXISTS (
            SELECT 1 FROM execution_device_heads h
              JOIN execution_inventory_devices i
                ON i.inventory_sha256 = h.current_inventory_sha256
               AND i.device_id = h.device_id
              JOIN execution_inventory_attestations a
                ON a.inventory_sha256 = i.inventory_sha256
              CROSS JOIN LATERAL jsonb_array_elements(a.payload_json->'resources') r(value)
             WHERE h.node_id = target_node AND h.device_id = target_device
               AND i.node_id = h.node_id AND i.hardware_uuid = h.hardware_uuid
               AND r.value->>'resource_id' = i.device_id
               AND r.value->>'kind' = 'accelerator'
               AND r.value->>'accelerator_uuid' = i.hardware_uuid
               AND r.value->>'accelerator_model' = i.model
               AND r.value->'resource_class_ids' = i.resource_class_ids_json
               AND (r.value->>'accelerator_memory_bytes_total')::bigint =
                   i.total_memory_bytes
               AND (r.value->>'accelerator_memory_bytes_safety_reserve')::bigint =
                   i.safety_reserve_bytes
               AND (r.value->>'accelerator_memory_bytes_managed_occupied')::bigint =
                   i.managed_memory_bytes
               AND (r.value->>'accelerator_memory_bytes_allocatable')::bigint =
                   i.allocatable_memory_bytes
               AND r.value->>'accelerator_compute_capability' = i.compute_capability
               AND (r.value->>'health' = 'healthy') = i.healthy
               AND i.external_occupancy = (
                    (r.value->>'external_process_count')::bigint > 0 OR
                    (r.value->>'accelerator_memory_bytes_external_occupied')::bigint > 0
               )
               AND r.value->'features' = i.features_json
               AND (i.managed_memory_bytes = 0 OR EXISTS (
                    SELECT 1 FROM execution_device_leases d
                     WHERE d.node_id = i.node_id AND d.device_id = i.device_id
                       AND d.hardware_uuid = i.hardware_uuid
                       AND d.acquired_at < a.received_at
                       AND (d.released_at IS NULL OR d.released_at > a.received_at)
               ))
          ) THEN
            RAISE EXCEPTION 'device head current inventory is cross-node or inconsistent'
              USING ERRCODE = '23514';
          END IF;
          IF active_id IS DISTINCT FROM lease_id OR
             (lease_id IS NOT NULL AND fence <> lease_fence) THEN
            RAISE EXCEPTION 'device fencing head differs from active/reconciliation lease'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER trg_execution_device_head_consistent
          AFTER INSERT OR UPDATE ON execution_device_heads
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_device_head();
        CREATE CONSTRAINT TRIGGER trg_execution_device_lease_consistent
          AFTER INSERT OR UPDATE ON execution_device_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_device_head();
        CREATE CONSTRAINT TRIGGER trg_execution_inventory_device_consistent
          AFTER INSERT ON execution_inventory_devices
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_device_head();
        """
    )


def downgrade() -> None:
    op.drop_table("execution_outbox")
    op.drop_table("execution_terminal_receipts")
    op.drop_table("execution_budget_events")
    op.drop_table("execution_budget_reservations")
    op.drop_table("execution_device_leases")
    op.drop_table("execution_resource_leases")
    op.execute("DROP TABLE IF EXISTS execution_attempt_adoptions")
    op.drop_table("execution_attempts")
    op.drop_table("execution_heads")
    op.drop_table("execution_budget_heads")
    op.drop_table("execution_budget_authorizations")
    op.drop_table("execution_qualification_admissions")
    op.drop_table("execution_device_heads")
    op.drop_table("execution_inventory_devices")
    op.drop_table("execution_inventory_attestations")
    op.drop_table("execution_nodes")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_device_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_node_capacity()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_budget_events()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_budget_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_terminal_outbox()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_adoption()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_attempt_bundle()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_execution_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_current_inventory()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_outbox()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_budget_event_chain()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_lease_state()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_attempt()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_device_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_budget_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_execution_head()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_node()")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_reject_mutation()")
