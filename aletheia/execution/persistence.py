"""Private PostgreSQL records for the local execution authority.

The public mutation surface lives in :mod:`aletheia.execution.allocator`.  These records are
deliberately private: callers must not manufacture leases, budget events, or terminal receipts by
writing ORM rows directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class _ExecutionNodeRecord(Base):
    __tablename__ = "execution_nodes"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','draining','retired')",
            name="ck_execution_nodes_state",
        ),
        CheckConstraint(
            "state_version >= 1 AND reserved_cpu_cores >= 0 "
            "AND reserved_memory_bytes >= 0 AND reserved_scratch_bytes >= 0",
            name="ck_execution_nodes_capacity_head",
        ),
        CheckConstraint(
            f"node_manifest_sha256 {_SHA256_SQL} AND "
            f"node_authority_pin_sha256 {_SHA256_SQL} AND "
            f"node_enrollment_sha256 {_SHA256_SQL} AND "
            f"(current_inventory_sha256 IS NULL OR current_inventory_sha256 {_SHA256_SQL})",
            name="ck_execution_nodes_hashes",
        ),
    )

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_manifest_sha256: Mapped[str] = mapped_column(String(64))
    node_authority_pin_sha256: Mapped[str] = mapped_column(String(64))
    node_authority_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    node_enrollment_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    node_enrollment_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    node_principal_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str] = mapped_column(String(192))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    boot_id: Mapped[str | None] = mapped_column(String(192))
    state: Mapped[str] = mapped_column(String(16), index=True)
    state_version: Mapped[int] = mapped_column(BigInteger, default=1)
    current_inventory_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    current_inventory_sequence: Mapped[int | None] = mapped_column(BigInteger)
    reserved_cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    reserved_memory_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_scratch_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    exclusive_lease_id: Mapped[str | None] = mapped_column(String(96), unique=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _ExecutionInventoryAttestationRecord(Base):
    __tablename__ = "execution_inventory_attestations"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_execution_inventory_sequence"),
        CheckConstraint(
            "cpu_cores >= 1 AND memory_bytes >= 1 AND scratch_bytes >= 1 "
            "AND allocatable_cpu_cores >= 0 AND allocatable_memory_bytes >= 0 "
            "AND allocatable_scratch_bytes >= 0 AND managed_cpu_cores >= 0 "
            "AND managed_memory_bytes >= 0 AND managed_scratch_bytes >= 0 "
            "AND allocatable_cpu_cores <= cpu_cores "
            "AND allocatable_memory_bytes <= memory_bytes "
            "AND allocatable_scratch_bytes <= scratch_bytes",
            name="ck_execution_inventory_capacity",
        ),
        CheckConstraint(
            "observed_monotonic_ns >= 0 AND observed_at <= received_at "
            "AND received_at < valid_until",
            name="ck_execution_inventory_time",
        ),
        CheckConstraint(
            f"inventory_sha256 {_SHA256_SQL} AND node_manifest_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL}",
            name="ck_execution_inventory_hashes",
        ),
        UniqueConstraint(
            "node_id",
            "boot_id",
            "sequence",
            name="uq_execution_inventory_node_boot_sequence",
        ),
        UniqueConstraint(
            "node_id",
            "boot_id",
            "observed_monotonic_ns",
            name="uq_execution_inventory_node_boot_monotonic",
        ),
        UniqueConstraint(
            "node_id",
            "inventory_sha256",
            name="uq_execution_inventory_node_hash",
        ),
    )

    inventory_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    node_manifest_sha256: Mapped[str] = mapped_column(String(64))
    boot_id: Mapped[str] = mapped_column(String(192))
    sequence: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_monotonic_ns: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cpu_cores: Mapped[int] = mapped_column(Integer)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    scratch_bytes: Mapped[int] = mapped_column(BigInteger)
    allocatable_cpu_cores: Mapped[int] = mapped_column(Integer)
    allocatable_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    allocatable_scratch_bytes: Mapped[int] = mapped_column(BigInteger)
    managed_cpu_cores: Mapped[int] = mapped_column(Integer)
    managed_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    managed_scratch_bytes: Mapped[int] = mapped_column(BigInteger)
    external_occupancy: Mapped[bool] = mapped_column(Boolean)
    external_occupancy_sha256: Mapped[str | None] = mapped_column(String(64))
    resource_class_ids_json: Mapped[list[str]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attested_by_principal_id: Mapped[str] = mapped_column(String(128))
    signing_key_id: Mapped[str] = mapped_column(String(64))
    signature_ed25519_hex: Mapped[str] = mapped_column(String(128))


class _ExecutionInventoryDeviceRecord(Base):
    __tablename__ = "execution_inventory_devices"
    __table_args__ = (
        CheckConstraint(
            "total_memory_bytes >= 1 AND safety_reserve_bytes >= 0 "
            "AND managed_memory_bytes >= 0 AND allocatable_memory_bytes >= 0 "
            "AND safety_reserve_bytes + managed_memory_bytes + "
            "allocatable_memory_bytes <= total_memory_bytes",
            name="ck_execution_inventory_devices_memory",
        ),
        CheckConstraint(
            "external_occupancy = (external_occupancy_sha256 IS NOT NULL)",
            name="ck_execution_inventory_devices_external_pair",
        ),
        CheckConstraint(
            "compute_capability ~ '^[0-9]+[.][0-9]+$'",
            name="ck_execution_inventory_devices_compute_capability",
        ),
        UniqueConstraint(
            "inventory_sha256",
            "hardware_uuid",
            name="uq_execution_inventory_devices_hardware",
        ),
    )

    inventory_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_inventory_attestations.inventory_sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    device_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    hardware_uuid: Mapped[str] = mapped_column(String(192))
    resource_class_ids_json: Mapped[list[str]] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128), index=True)
    total_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    safety_reserve_bytes: Mapped[int] = mapped_column(BigInteger)
    managed_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    allocatable_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    compute_capability: Mapped[str] = mapped_column(String(16))
    healthy: Mapped[bool] = mapped_column(Boolean)
    external_occupancy: Mapped[bool] = mapped_column(Boolean)
    external_occupancy_sha256: Mapped[str | None] = mapped_column(String(64))
    features_json: Mapped[list[str]] = mapped_column(JSONB)


class _ExecutionDeviceHeadRecord(Base):
    """Mutable fencing head for a physical accelerator across inventory refreshes."""

    __tablename__ = "execution_device_heads"
    __table_args__ = (
        CheckConstraint(
            "fencing_counter >= 0 AND state_version >= 1",
            name="ck_execution_device_heads_versions",
        ),
        ForeignKeyConstraint(
            ["current_inventory_sha256", "device_id"],
            [
                "execution_inventory_devices.inventory_sha256",
                "execution_inventory_devices.device_id",
            ],
            name="fk_execution_device_heads_current_inventory",
        ),
        UniqueConstraint("node_id", "hardware_uuid", name="uq_execution_device_heads_hardware"),
        UniqueConstraint("active_device_lease_id", name="uq_execution_device_heads_active_lease"),
    )

    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    hardware_uuid: Mapped[str] = mapped_column(String(192))
    current_inventory_sha256: Mapped[str] = mapped_column(String(64))
    fencing_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    active_device_lease_id: Mapped[str | None] = mapped_column(String(96))
    state_version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _ExecutionQualificationAdmissionRecord(Base):
    """Immutable, independently re-verifiable authority material retained at admission."""

    __tablename__ = "execution_qualification_admissions"
    __table_args__ = (
        CheckConstraint(
            f"admission_sha256 {_SHA256_SQL} AND grant_sha256 {_SHA256_SQL} "
            f"AND bundle_sha256 {_SHA256_SQL} AND intent_sha256 {_SHA256_SQL} "
            f"AND budget_authorization_sha256 {_SHA256_SQL} "
            f"AND cost_quote_sha256 {_SHA256_SQL} AND authority_policy_sha256 {_SHA256_SQL} "
            f"AND authority_key_id {_SHA256_SQL}",
            name="ck_execution_qualification_admissions_hashes",
        ),
        UniqueConstraint(
            "admission_sha256",
            "infrastructure_attempt_id",
            name="uq_execution_qualification_admissions_attempt_binding",
        ),
        UniqueConstraint("grant_sha256", name="uq_execution_qualification_admissions_grant"),
        UniqueConstraint("bundle_sha256", name="uq_execution_qualification_admissions_bundle"),
    )

    admission_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_sha256: Mapped[str] = mapped_column(String(64))
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    intent_sha256: Mapped[str] = mapped_column(String(64), index=True)
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    infrastructure_attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    budget_authorization_sha256: Mapped[str] = mapped_column(String(64))
    cost_quote_sha256: Mapped[str] = mapped_column(String(64))
    authority_policy_sha256: Mapped[str] = mapped_column(String(64))
    authority_key_id: Mapped[str] = mapped_column(String(64))
    bundle_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    grant_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verified_receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionBudgetAuthorizationRecord(Base):
    __tablename__ = "execution_budget_authorizations"
    __table_args__ = (
        CheckConstraint(
            "cap_microunits >= 0 AND authorized_at < expires_at",
            name="ck_execution_budget_authorizations_cap_time",
        ),
        CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'",
            name="ck_execution_budget_authorizations_currency",
        ),
        CheckConstraint(
            f"authorization_sha256 {_SHA256_SQL} AND resource_budget_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL}",
            name="ck_execution_budget_authorizations_hashes",
        ),
        UniqueConstraint(
            "quest_id",
            "resource_budget_sha256",
            "authorization_sha256",
            name="uq_execution_budget_authorizations_scope",
        ),
    )

    authorization_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), index=True)
    protocol_sha256: Mapped[str] = mapped_column(String(64))
    work_order_sha256: Mapped[str] = mapped_column(String(64))
    resource_budget_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_budget_authorization_sha256: Mapped[str] = mapped_column(String(64))
    currency_code: Mapped[str] = mapped_column(String(3))
    cap_microunits: Mapped[int] = mapped_column(BigInteger)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    authorized_by_principal_id: Mapped[str] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class _ExecutionBudgetHeadRecord(Base):
    __tablename__ = "execution_budget_heads"
    __table_args__ = (
        CheckConstraint(
            "reserved_microunits >= 0 AND spent_microunits >= 0 "
            "AND reserved_microunits + spent_microunits <= cap_microunits "
            "AND state_version >= 1",
            name="ck_execution_budget_heads_balance",
        ),
        CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'",
            name="ck_execution_budget_heads_currency",
        ),
    )

    authorization_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_budget_authorizations.authorization_sha256"), primary_key=True
    )
    currency_code: Mapped[str] = mapped_column(String(3))
    cap_microunits: Mapped[int] = mapped_column(BigInteger)
    reserved_microunits: Mapped[int] = mapped_column(BigInteger, default=0)
    spent_microunits: Mapped[int] = mapped_column(BigInteger, default=0)
    state_version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _ExecutionHeadRecord(Base):
    __tablename__ = "execution_heads"
    __table_args__ = (
        CheckConstraint(
            "last_attempt_number >= 0 AND state_version >= 1",
            name="ck_execution_heads_versions",
        ),
        CheckConstraint(
            f"protocol_sha256 {_SHA256_SQL} AND work_order_sha256 {_SHA256_SQL} "
            f"AND replicate_slot_sha256 {_SHA256_SQL}",
            name="ck_execution_heads_hashes",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quest_id: Mapped[str] = mapped_column(String(36), index=True)
    protocol_sha256: Mapped[str] = mapped_column(String(64))
    work_order_id: Mapped[str] = mapped_column(String(35))
    work_order_sha256: Mapped[str] = mapped_column(String(64))
    replicate_slot_id: Mapped[str] = mapped_column(String(36), unique=True)
    replicate_slot_sha256: Mapped[str] = mapped_column(String(64))
    last_attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    active_attempt_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    state_version: Mapped[int] = mapped_column(BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _ExecutionAttemptRecord(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint(
            "attempt_number >= 1 AND adoption_count >= 0 "
            "AND last_runtime_inspection_sequence >= 0 "
            "AND runtime_launch_authorization_count >= 0 "
            "AND pre_runtime_absence_count >= 0 "
            "AND runtime_termination_challenge_count >= 0 "
            "AND state_version >= 1 AND fencing_epoch >= 1",
            name="ck_execution_attempts_versions",
        ),
        CheckConstraint(
            "status IN ('reserved','starting','running','reconciliation_required',"
            "'terminated','verifying','succeeded','failed','cancelled')",
            name="ck_execution_attempts_status",
        ),
        CheckConstraint(
            "authorized_at <= reserved_at AND reserved_at < hard_deadline "
            "AND reserved_at < lease_expires_at AND lease_expires_at <= hard_deadline",
            name="ck_execution_attempts_time",
        ),
        CheckConstraint(
            f"intent_sha256 {_SHA256_SQL} AND admission_sha256 {_SHA256_SQL} "
            f"AND grant_sha256 {_SHA256_SQL} AND bundle_sha256 {_SHA256_SQL} "
            f"AND cost_quote_sha256 {_SHA256_SQL} AND lease_token_sha256 {_SHA256_SQL} "
            f"AND node_inventory_sha256 {_SHA256_SQL} "
            f"AND (latest_adoption_sha256 IS NULL OR latest_adoption_sha256 {_SHA256_SQL}) "
            f"AND (last_runtime_inspection_sha256 IS NULL OR "
            f"last_runtime_inspection_sha256 {_SHA256_SQL}) "
            f"AND (runtime_identity_sha256 IS NULL OR runtime_identity_sha256 {_SHA256_SQL}) "
            f"AND (terminal_receipt_sha256 IS NULL OR terminal_receipt_sha256 {_SHA256_SQL}) "
            f"AND (runtime_preparation_sha256 IS NULL OR "
            f"runtime_preparation_sha256 {_SHA256_SQL}) "
            f"AND (latest_runtime_launch_authorization_sha256 IS NULL OR "
            f"latest_runtime_launch_authorization_sha256 {_SHA256_SQL}) "
            f"AND (latest_pre_runtime_absence_receipt_sha256 IS NULL OR "
            f"latest_pre_runtime_absence_receipt_sha256 {_SHA256_SQL}) "
            f"AND (node_runtime_launch_receipt_sha256 IS NULL OR "
            f"node_runtime_launch_receipt_sha256 {_SHA256_SQL}) "
            f"AND (runtime_termination_challenge_sha256 IS NULL OR "
            f"runtime_termination_challenge_sha256 {_SHA256_SQL}) "
            f"AND (accepted_runtime_termination_sha256 IS NULL OR "
            f"accepted_runtime_termination_sha256 {_SHA256_SQL}) "
            f"AND (accepted_terminal_submission_sha256 IS NULL OR "
            f"accepted_terminal_submission_sha256 {_SHA256_SQL}) "
            f"AND (terminal_deadline_expiration_sha256 IS NULL OR "
            f"terminal_deadline_expiration_sha256 {_SHA256_SQL})",
            name="ck_execution_attempts_hashes",
        ),
        CheckConstraint(
            "accepted_terminal_submission_sha256 IS NULL OR "
            "terminal_deadline_expiration_sha256 IS NULL",
            name="ck_execution_attempts_v2_terminal_authority",
        ),
        CheckConstraint(
            "(runtime_launch_authorization_count = 0) = "
            "(latest_runtime_launch_authorization_sha256 IS NULL) AND "
            "(pre_runtime_absence_count = 0) = "
            "(latest_pre_runtime_absence_receipt_sha256 IS NULL) AND "
            "(runtime_termination_challenge_count = 0) = "
            "(runtime_termination_challenge_sha256 IS NULL)",
            name="ck_execution_attempts_v2_counts",
        ),
        CheckConstraint(
            "(runtime_identity_sha256 IS NULL) = (runtime_identity_json IS NULL)",
            name="ck_execution_attempts_runtime_identity_pair",
        ),
        CheckConstraint(
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
        UniqueConstraint(
            "execution_id",
            "attempt_number",
            name="uq_execution_attempts_number",
        ),
        UniqueConstraint(
            "attempt_id", "execution_id", name="uq_execution_attempts_attempt_execution"
        ),
        ForeignKeyConstraint(
            ["admission_sha256", "attempt_id"],
            [
                "execution_qualification_admissions.admission_sha256",
                "execution_qualification_admissions.infrastructure_attempt_id",
            ],
            name="fk_execution_attempts_admission_attempt",
        ),
        UniqueConstraint("intent_sha256", name="uq_execution_attempts_intent"),
        Index(
            "uq_execution_attempts_active_execution",
            "execution_id",
            unique=True,
            postgresql_where=text(
                "status IN ('reserved','starting','running','reconciliation_required',"
                "'terminated','verifying')"
            ),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_heads.execution_id"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    intent_sha256: Mapped[str] = mapped_column(String(64))
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    admission_sha256: Mapped[str] = mapped_column(String(64))
    grant_sha256: Mapped[str] = mapped_column(String(64))
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    cost_quote_sha256: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    node_inventory_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_inventory_attestations.inventory_sha256"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    state_version: Mapped[int] = mapped_column(BigInteger)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    lease_token_sha256: Mapped[str] = mapped_column(String(64))
    adoption_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_adoption_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    last_runtime_inspection_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    last_runtime_inspection_sha256: Mapped[str | None] = mapped_column(String(64))
    last_runtime_inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_runtime_inspected_monotonic_ns: Mapped[int | None] = mapped_column(BigInteger)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    hard_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reconciliation_reason: Mapped[str | None] = mapped_column(String(64))
    runtime_identity_sha256: Mapped[str | None] = mapped_column(String(64))
    runtime_identity_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    terminal_receipt_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    runtime_preparation_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    runtime_launch_authorization_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_runtime_launch_authorization_sha256: Mapped[str | None] = mapped_column(
        String(64), unique=True
    )
    pre_runtime_absence_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_pre_runtime_absence_receipt_sha256: Mapped[str | None] = mapped_column(
        String(64), unique=True
    )
    node_runtime_launch_receipt_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    runtime_termination_challenge_count: Mapped[int] = mapped_column(Integer, default=0)
    runtime_termination_challenge_sha256: Mapped[str | None] = mapped_column(
        String(64), unique=True
    )
    accepted_runtime_termination_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    accepted_terminal_submission_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    terminal_deadline_expiration_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class _ExecutionAssignmentEnvelopeRecord(Base):
    """Immutable node-encrypted delivery for one exact admitted infrastructure attempt."""

    __tablename__ = "execution_assignment_envelopes"
    __table_args__ = (
        CheckConstraint(
            "initial_fencing_epoch >= 1 AND issued_at < expires_at",
            name="ck_execution_assignment_envelopes_fence_time",
        ),
        CheckConstraint(
            f"assignment_envelope_sha256 {_SHA256_SQL} "
            f"AND assignment_secret_sha256 {_SHA256_SQL} "
            f"AND admission_sha256 {_SHA256_SQL} AND grant_sha256 {_SHA256_SQL} "
            f"AND bundle_sha256 {_SHA256_SQL} AND node_manifest_sha256 {_SHA256_SQL} "
            f"AND resource_lease_sha256 {_SHA256_SQL} AND lease_token_sha256 {_SHA256_SQL} "
            f"AND transport_pin_sha256 {_SHA256_SQL} AND transport_key_id {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL}",
            name="ck_execution_assignment_envelopes_hashes",
        ),
        ForeignKeyConstraint(
            ["admission_sha256", "attempt_id"],
            [
                "execution_qualification_admissions.admission_sha256",
                "execution_qualification_admissions.infrastructure_attempt_id",
            ],
            name="fk_execution_assignment_envelopes_admission_attempt",
        ),
        UniqueConstraint("attempt_id"),
        UniqueConstraint(
            "admission_sha256",
            name="uq_execution_assignment_envelopes_admission",
        ),
    )

    assignment_envelope_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_secret_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    admission_sha256: Mapped[str] = mapped_column(String(64), index=True)
    grant_sha256: Mapped[str] = mapped_column(String(64))
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    node_manifest_sha256: Mapped[str] = mapped_column(String(64))
    resource_lease_sha256: Mapped[str] = mapped_column(String(64))
    initial_fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    lease_token_sha256: Mapped[str] = mapped_column(String(64))
    transport_pin_sha256: Mapped[str] = mapped_column(String(64))
    transport_key_id: Mapped[str] = mapped_column(String(64), index=True)
    transport_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class _ExecutionRuntimePreparationRecord(Base):
    """One inert, crash-durable runtime preparation for an admitted attempt."""

    __tablename__ = "execution_runtime_preparations"
    __table_args__ = (
        CheckConstraint(
            "fencing_epoch >= 1 AND prepared_monotonic_ns >= 0",
            name="ck_execution_runtime_preparations_order",
        ),
        CheckConstraint(
            f"preparation_sha256 {_SHA256_SQL} AND intent_sha256 {_SHA256_SQL} "
            f"AND node_manifest_sha256 {_SHA256_SQL} AND lease_token_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_preparations_hashes",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "execution_id"],
            ["execution_attempts.attempt_id", "execution_attempts.execution_id"],
            name="fk_execution_runtime_preparations_attempt",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_runtime_preparations_attempt"),
    )

    preparation_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    intent_sha256: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    node_manifest_sha256: Mapped[str] = mapped_column(String(64))
    boot_id: Mapped[str] = mapped_column(String(192))
    fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    lease_token_sha256: Mapped[str] = mapped_column(String(64))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_monotonic_ns: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionRuntimeLaunchAuthorizationRecord(Base):
    """Append-only request plus the exact runtime-control signed launch ticket."""

    __tablename__ = "execution_runtime_launch_authorizations"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1 AND pre_runtime_absence_epoch >= 0 AND issued_at < expires_at",
            name="ck_execution_runtime_launch_authorizations_order",
        ),
        CheckConstraint(
            "(pre_runtime_absence_epoch = 0) = (pre_runtime_absence_receipt_sha256 IS NULL)",
            name="ck_execution_runtime_launch_authorizations_absence",
        ),
        CheckConstraint(
            f"authorization_sha256 {_SHA256_SQL} AND request_sha256 {_SHA256_SQL} "
            f"AND preparation_sha256 {_SHA256_SQL} "
            f"AND (pre_runtime_absence_receipt_sha256 IS NULL OR "
            f"pre_runtime_absence_receipt_sha256 {_SHA256_SQL}) "
            f"AND request_payload_sha256 {_SHA256_SQL} "
            f"AND authorization_payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_launch_authorizations_hashes",
        ),
        UniqueConstraint(
            "attempt_id", "sequence", name="uq_execution_runtime_launch_authorizations_sequence"
        ),
        UniqueConstraint("request_sha256"),
    )

    authorization_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    request_sha256: Mapped[str] = mapped_column(String(64))
    pre_runtime_absence_epoch: Mapped[int] = mapped_column(Integer)
    pre_runtime_absence_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    request_payload_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    authorization_payload_sha256: Mapped[str] = mapped_column(String(64))
    authorization_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionRuntimeLaunchReceiptRecord(Base):
    """One node-signed real launch plus non-launching historical recovery authority."""

    __tablename__ = "execution_runtime_launch_receipts"
    __table_args__ = (
        CheckConstraint(
            f"launch_receipt_sha256 {_SHA256_SQL} AND preparation_sha256 {_SHA256_SQL} "
            f"AND authorization_request_sha256 {_SHA256_SQL} "
            f"AND authorization_sha256 {_SHA256_SQL} AND runtime_identity_sha256 {_SHA256_SQL} "
            f"AND launch_payload_sha256 {_SHA256_SQL} "
            f"AND recovery_grant_sha256 {_SHA256_SQL} "
            f"AND recovery_payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_launch_receipts_hashes",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_runtime_launch_receipts_attempt"),
        UniqueConstraint("runtime_identity_sha256"),
        UniqueConstraint("recovery_grant_sha256"),
    )

    launch_receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256")
    )
    authorization_request_sha256: Mapped[str] = mapped_column(String(64))
    authorization_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_launch_authorizations.authorization_sha256")
    )
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64))
    launch_payload_sha256: Mapped[str] = mapped_column(String(64))
    launch_receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recovery_grant_sha256: Mapped[str] = mapped_column(String(64))
    recovery_payload_sha256: Mapped[str] = mapped_column(String(64))
    recovery_grant_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recovery_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionPreRuntimeAbsenceDecisionRecord(Base):
    """Fresh node absence proof and the allocator's exact release/reauthorize decision."""

    __tablename__ = "execution_pre_runtime_absence_decisions"
    __table_args__ = (
        CheckConstraint(
            "absence_epoch >= 1 AND disposition IN ('released','reauthorized')",
            name="ck_execution_pre_runtime_absence_decisions_shape",
        ),
        CheckConstraint(
            "(disposition = 'released' AND replacement_request_sha256 IS NULL "
            "AND replacement_authorization_sha256 IS NULL) OR "
            "(disposition = 'reauthorized' AND replacement_request_sha256 IS NOT NULL "
            "AND replacement_authorization_sha256 IS NOT NULL)",
            name="ck_execution_pre_runtime_absence_decisions_replacement",
        ),
        CheckConstraint(
            "(prior_authorization_request_sha256 IS NULL) = (prior_authorization_sha256 IS NULL)",
            name="ck_execution_pre_runtime_absence_decisions_prior_pair",
        ),
        CheckConstraint(
            f"decision_sha256 {_SHA256_SQL} AND absence_receipt_sha256 {_SHA256_SQL} "
            f"AND preparation_sha256 {_SHA256_SQL} "
            f"AND (prior_authorization_request_sha256 IS NULL OR "
            f"prior_authorization_request_sha256 {_SHA256_SQL}) "
            f"AND (prior_authorization_sha256 IS NULL OR "
            f"prior_authorization_sha256 {_SHA256_SQL}) "
            f"AND absence_payload_sha256 {_SHA256_SQL} "
            f"AND (replacement_request_sha256 IS NULL OR "
            f"replacement_request_sha256 {_SHA256_SQL}) "
            f"AND (replacement_authorization_sha256 IS NULL OR "
            f"replacement_authorization_sha256 {_SHA256_SQL}) "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_pre_runtime_absence_decisions_hashes",
        ),
        UniqueConstraint(
            "attempt_id", "absence_epoch", name="uq_execution_pre_runtime_absence_epoch"
        ),
        UniqueConstraint("absence_receipt_sha256"),
    )

    decision_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    absence_epoch: Mapped[int] = mapped_column(Integer)
    absence_receipt_sha256: Mapped[str] = mapped_column(String(64))
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256")
    )
    prior_authorization_request_sha256: Mapped[str | None] = mapped_column(String(64))
    prior_authorization_sha256: Mapped[str | None] = mapped_column(String(64))
    absence_payload_sha256: Mapped[str] = mapped_column(String(64))
    absence_receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    disposition: Mapped[str] = mapped_column(String(16), index=True)
    replacement_request_sha256: Mapped[str | None] = mapped_column(String(64))
    replacement_authorization_sha256: Mapped[str | None] = mapped_column(String(64))
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionRuntimeFenceRebindRecord(Base):
    """Full request/evidence/node receipt paired one-to-one with an adoption."""

    __tablename__ = "execution_runtime_fence_rebinds"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1 AND new_fencing_epoch = previous_fencing_epoch + 1",
            name="ck_execution_runtime_fence_rebinds_sequence",
        ),
        CheckConstraint(
            f"rebind_receipt_sha256 {_SHA256_SQL} AND request_sha256 {_SHA256_SQL} "
            f"AND evidence_sha256 {_SHA256_SQL} AND adoption_sha256 {_SHA256_SQL} "
            f"AND preparation_sha256 {_SHA256_SQL} AND runtime_identity_sha256 {_SHA256_SQL} "
            f"AND previous_lease_token_sha256 {_SHA256_SQL} "
            f"AND new_lease_token_sha256 {_SHA256_SQL} "
            f"AND request_payload_sha256 {_SHA256_SQL} "
            f"AND receipt_payload_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_fence_rebinds_hashes",
        ),
        UniqueConstraint("attempt_id", "sequence", name="uq_execution_runtime_rebind_sequence"),
        UniqueConstraint("adoption_sha256"),
        UniqueConstraint("request_sha256"),
        UniqueConstraint("evidence_sha256"),
    )

    rebind_receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    adoption_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_attempt_adoptions.adoption_sha256"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    request_sha256: Mapped[str] = mapped_column(String(64))
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256")
    )
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64))
    previous_fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    new_fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    previous_lease_token_sha256: Mapped[str] = mapped_column(String(64))
    new_lease_token_sha256: Mapped[str] = mapped_column(String(64))
    request_payload_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    receipt_payload_sha256: Mapped[str] = mapped_column(String(64))
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    rebound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionRuntimeTerminationChallengeRecord(Base):
    __tablename__ = "execution_runtime_termination_challenges"
    __table_args__ = (
        CheckConstraint(
            "inspection_sequence >= 1 AND challenged_at < expires_at",
            name="ck_execution_runtime_termination_challenges_order",
        ),
        CheckConstraint(
            f"challenge_sha256 {_SHA256_SQL} AND challenge_id {_SHA256_SQL} "
            f"AND preparation_sha256 {_SHA256_SQL} AND launch_receipt_sha256 {_SHA256_SQL} "
            f"AND runtime_identity_sha256 {_SHA256_SQL} "
            f"AND inspection_evidence_sha256 {_SHA256_SQL} "
            f"AND challenge_payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_termination_challenges_hashes",
        ),
        UniqueConstraint(
            "attempt_id",
            "inspection_sequence",
            name="uq_execution_runtime_termination_challenge_sequence",
        ),
        UniqueConstraint("challenge_id"),
    )

    challenge_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(String(64))
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256")
    )
    launch_receipt_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_launch_receipts.launch_receipt_sha256")
    )
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64))
    inspection_evidence_sha256: Mapped[str] = mapped_column(String(64))
    inspection_evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inspection_sequence: Mapped[int] = mapped_column(BigInteger)
    challenge_payload_sha256: Mapped[str] = mapped_column(String(64))
    challenge_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    challenged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionRuntimeTerminationAcceptanceRecord(Base):
    __tablename__ = "execution_runtime_termination_acceptances"
    __table_args__ = (
        CheckConstraint(
            "inspection_sequence >= 1 AND runtime_ended_at <= accepted_at",
            name="ck_execution_runtime_termination_acceptances_order",
        ),
        CheckConstraint(
            f"accepted_termination_sha256 {_SHA256_SQL} AND challenge_sha256 {_SHA256_SQL} "
            f"AND node_termination_receipt_sha256 {_SHA256_SQL} "
            f"AND preparation_sha256 {_SHA256_SQL} AND launch_receipt_sha256 {_SHA256_SQL} "
            f"AND runtime_identity_sha256 {_SHA256_SQL} "
            f"AND termination_evidence_sha256 {_SHA256_SQL} "
            f"AND node_receipt_payload_sha256 {_SHA256_SQL} "
            f"AND acceptance_payload_sha256 {_SHA256_SQL} "
            f"AND recovery_grant_sha256 {_SHA256_SQL} "
            f"AND recovery_payload_sha256 {_SHA256_SQL} "
            f"AND conditional_terminal_expiration_sha256 {_SHA256_SQL} "
            f"AND conditional_terminal_expiration_payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_runtime_termination_acceptances_hashes",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_runtime_termination_acceptance_attempt"),
        UniqueConstraint("challenge_sha256"),
        UniqueConstraint("node_termination_receipt_sha256"),
        UniqueConstraint("recovery_grant_sha256"),
        UniqueConstraint("conditional_terminal_expiration_sha256"),
    )

    accepted_termination_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    challenge_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_termination_challenges.challenge_sha256")
    )
    node_termination_receipt_sha256: Mapped[str] = mapped_column(String(64))
    preparation_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_preparations.preparation_sha256")
    )
    launch_receipt_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_launch_receipts.launch_receipt_sha256")
    )
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64))
    termination_evidence_sha256: Mapped[str] = mapped_column(String(64))
    inspection_sequence: Mapped[int] = mapped_column(BigInteger)
    node_receipt_payload_sha256: Mapped[str] = mapped_column(String(64))
    node_termination_receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    acceptance_payload_sha256: Mapped[str] = mapped_column(String(64))
    accepted_termination_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recovery_grant_sha256: Mapped[str] = mapped_column(String(64))
    recovery_payload_sha256: Mapped[str] = mapped_column(String(64))
    recovery_grant_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recovery_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    conditional_terminal_expiration_sha256: Mapped[str] = mapped_column(String(64))
    conditional_terminal_expiration_payload_sha256: Mapped[str] = mapped_column(String(64))
    conditional_terminal_expiration_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    conditional_terminal_expiration_authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )
    conditional_terminal_expiration_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionQualificationTerminalDeadlineExpirationRecord(Base):
    """Activated pre-signed failure after DB time proves artifact grace elapsed."""

    __tablename__ = "execution_qualification_terminal_deadline_expirations"
    __table_args__ = (
        CheckConstraint(
            "authorized_at < expired_at AND expired_at <= activated_at",
            name="ck_execution_qualification_terminal_deadline_expirations_order",
        ),
        CheckConstraint(
            f"terminal_deadline_expiration_sha256 {_SHA256_SQL} "
            f"AND accepted_runtime_termination_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_qualification_terminal_deadline_expirations_hashes",
        ),
        UniqueConstraint(
            "attempt_id",
            name="uq_execution_qualification_terminal_deadline_expiration_attempt",
        ),
        UniqueConstraint("accepted_runtime_termination_sha256"),
    )

    terminal_deadline_expiration_sha256: Mapped[str] = mapped_column(
        ForeignKey(
            "execution_runtime_termination_acceptances.conditional_terminal_expiration_sha256"
        ),
        primary_key=True,
    )
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    accepted_runtime_termination_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_termination_acceptances.accepted_termination_sha256")
    )
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionQualificationTerminalAcceptanceRecord(Base):
    __tablename__ = "execution_qualification_terminal_acceptances"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('process_succeeded','process_failed','invalid_output','timeout')",
            name="ck_execution_qualification_terminal_acceptances_disposition",
        ),
        CheckConstraint(
            f"accepted_terminal_submission_sha256 {_SHA256_SQL} "
            f"AND accepted_runtime_termination_sha256 {_SHA256_SQL} "
            f"AND terminal_submission_sha256 {_SHA256_SQL} "
            f"AND artifact_manifest_sha256 {_SHA256_SQL} AND output_tree_sha256 {_SHA256_SQL} "
            f"AND submission_payload_sha256 {_SHA256_SQL} "
            f"AND manifest_payload_sha256 {_SHA256_SQL} "
            f"AND acceptance_payload_sha256 {_SHA256_SQL} "
            f"AND runtime_control_pin_sha256 {_SHA256_SQL}",
            name="ck_execution_qualification_terminal_acceptances_hashes",
        ),
        UniqueConstraint(
            "attempt_id", name="uq_execution_qualification_terminal_acceptance_attempt"
        ),
        UniqueConstraint("accepted_runtime_termination_sha256"),
        UniqueConstraint("terminal_submission_sha256"),
    )

    accepted_terminal_submission_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    accepted_runtime_termination_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_runtime_termination_acceptances.accepted_termination_sha256")
    )
    terminal_submission_sha256: Mapped[str] = mapped_column(String(64))
    artifact_manifest_sha256: Mapped[str] = mapped_column(String(64))
    output_tree_sha256: Mapped[str] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(32), index=True)
    submission_payload_sha256: Mapped[str] = mapped_column(String(64))
    terminal_submission_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_payload_sha256: Mapped[str] = mapped_column(String(64))
    artifact_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    artifact_verified_receipt_sha256s_json: Mapped[list[str]] = mapped_column(JSONB)
    artifact_verified_receipts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    acceptance_payload_sha256: Mapped[str] = mapped_column(String(64))
    accepted_terminal_submission_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_control_pin_sha256: Mapped[str] = mapped_column(String(64))
    runtime_control_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionQualificationTerminalOutboxRecord(Base):
    """Immutable v2 publication intent; it never aliases the v1 receipt outbox."""

    __tablename__ = "execution_qualification_terminal_outbox"
    __table_args__ = (
        CheckConstraint(
            "outbox_id = 'qto_' || terminal_authority_sha256 "
            f"AND terminal_authority_sha256 {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL} "
            "AND payload_sha256 = terminal_authority_sha256 "
            "AND ((terminal_authority_kind = 'accepted_terminal_submission' "
            "AND accepted_terminal_submission_sha256 IS NOT NULL "
            "AND accepted_terminal_submission_sha256 = terminal_authority_sha256 "
            "AND terminal_deadline_expiration_sha256 IS NULL) OR "
            "(terminal_authority_kind = 'terminal_deadline_expiration' "
            "AND terminal_deadline_expiration_sha256 IS NOT NULL "
            "AND terminal_deadline_expiration_sha256 = terminal_authority_sha256 "
            "AND accepted_terminal_submission_sha256 IS NULL)) "
            "AND topic = 'execution.qualification_terminal.v2' "
            "AND delivery_key = "
            "'execution-v2:' || execution_id || ':' || attempt_id",
            name="ck_execution_qualification_terminal_outbox_hashes",
        ),
        UniqueConstraint("terminal_authority_sha256"),
        UniqueConstraint("accepted_terminal_submission_sha256"),
        UniqueConstraint("terminal_deadline_expiration_sha256"),
        UniqueConstraint("attempt_id"),
        UniqueConstraint("delivery_key"),
        UniqueConstraint(
            "outbox_id",
            "execution_id",
            "attempt_id",
            "terminal_authority_sha256",
            name="uq_exec_qto_exact_controller_source",
        ),
    )

    outbox_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    terminal_authority_kind: Mapped[str] = mapped_column(String(48))
    terminal_authority_sha256: Mapped[str] = mapped_column(String(64), index=True)
    accepted_terminal_submission_sha256: Mapped[str | None] = mapped_column(
        ForeignKey(
            "execution_qualification_terminal_acceptances.accepted_terminal_submission_sha256"
        ),
        index=True,
    )
    terminal_deadline_expiration_sha256: Mapped[str | None] = mapped_column(
        ForeignKey(
            "execution_qualification_terminal_deadline_expirations."
            "terminal_deadline_expiration_sha256"
        ),
        index=True,
    )
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    topic: Mapped[str] = mapped_column(String(96))
    delivery_key: Mapped[str] = mapped_column(String(192))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionAttemptAdoptionRecord(Base):
    """Append-only receipt that authorizes one same-attempt fence/token rotation."""

    __tablename__ = "execution_attempt_adoptions"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1 AND previous_fencing_epoch >= 1 "
            "AND new_fencing_epoch = previous_fencing_epoch + 1",
            name="ck_execution_attempt_adoptions_fence",
        ),
        CheckConstraint(
            f"adoption_sha256 {_SHA256_SQL} AND previous_lease_token_sha256 {_SHA256_SQL} "
            f"AND new_lease_token_sha256 {_SHA256_SQL} AND runtime_identity_sha256 {_SHA256_SQL} "
            f"AND reason_sha256 {_SHA256_SQL} AND payload_sha256 {_SHA256_SQL}",
            name="ck_execution_attempt_adoptions_hashes",
        ),
        UniqueConstraint("attempt_id", "sequence", name="uq_execution_attempt_adoptions_sequence"),
        UniqueConstraint(
            "attempt_id", "new_fencing_epoch", name="uq_execution_attempt_adoptions_fence"
        ),
    )

    adoption_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    previous_fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    new_fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    previous_lease_token_sha256: Mapped[str] = mapped_column(String(64))
    new_lease_token_sha256: Mapped[str] = mapped_column(String(64))
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64))
    reason_sha256: Mapped[str] = mapped_column(String(64))
    adopted_by_principal_id: Mapped[str] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionResourceLeaseRecord(Base):
    __tablename__ = "execution_resource_leases"
    __table_args__ = (
        CheckConstraint(
            "cpu_cores >= 1 AND memory_bytes >= 1 AND scratch_bytes >= 1 "
            "AND accelerator_count BETWEEN 0 AND 1 AND fencing_epoch >= 1",
            name="ck_execution_resource_leases_capacity",
        ),
        CheckConstraint(
            "state IN ('held','reconciliation_required','released')",
            name="ck_execution_resource_leases_state",
        ),
        CheckConstraint(
            "acquired_at < lease_expires_at AND "
            "(released_at IS NULL OR released_at >= acquired_at)",
            name="ck_execution_resource_leases_time",
        ),
        CheckConstraint(
            f"inventory_sha256 {_SHA256_SQL} AND lease_sha256 {_SHA256_SQL}",
            name="ck_execution_resource_leases_hashes",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_resource_leases_attempt"),
    )

    lease_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    inventory_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_inventory_attestations.inventory_sha256")
    )
    lease_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    lease_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(32), index=True)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    cpu_cores: Mapped[int] = mapped_column(Integer)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    scratch_bytes: Mapped[int] = mapped_column(BigInteger)
    exclusive: Mapped[bool] = mapped_column(Boolean)
    accelerator_count: Mapped[int] = mapped_column(Integer, default=0)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _ExecutionDeviceLeaseRecord(Base):
    __tablename__ = "execution_device_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('held','reconciliation_required','released')",
            name="ck_execution_device_leases_state",
        ),
        CheckConstraint(
            "fencing_epoch >= 1 AND requested_memory_bytes >= 1",
            name="ck_execution_device_leases_fence_memory",
        ),
        UniqueConstraint(
            "resource_lease_id",
            "device_id",
            name="uq_execution_device_leases_claim",
        ),
        Index(
            "uq_execution_device_leases_active_device",
            "node_id",
            "device_id",
            unique=True,
            postgresql_where=text("state IN ('held','reconciliation_required')"),
        ),
    )

    device_lease_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    resource_lease_id: Mapped[str] = mapped_column(
        ForeignKey("execution_resource_leases.lease_id"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("execution_nodes.node_id"), index=True)
    device_id: Mapped[str] = mapped_column(String(192), index=True)
    hardware_uuid: Mapped[str] = mapped_column(String(192))
    state: Mapped[str] = mapped_column(String(32), index=True)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger)
    requested_memory_bytes: Mapped[int] = mapped_column(BigInteger)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _ExecutionBudgetReservationRecord(Base):
    __tablename__ = "execution_budget_reservations"
    __table_args__ = (
        CheckConstraint(
            "fixed_charge_microunits >= 0 AND charge_per_second_microunits >= 0 "
            "AND maximum_lease_seconds >= 1 "
            "AND held_microunits = fixed_charge_microunits + "
            "(charge_per_second_microunits * maximum_lease_seconds) "
            "AND settled_microunits >= 0 AND settled_microunits <= held_microunits",
            name="ck_execution_budget_reservations_amounts",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "state IN ('held','reconciliation_required','settled','released')",
            name="ck_execution_budget_reservations_state",
        ),
        CheckConstraint(
            "currency_code ~ '^[A-Z]{3}$'",
            name="ck_execution_budget_reservations_currency",
        ),
        CheckConstraint(
            f"cost_quote_sha256 {_SHA256_SQL}",
            name="ck_execution_budget_reservations_quote",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_budget_reservations_attempt"),
        UniqueConstraint(
            "authorization_sha256",
            "cost_quote_sha256",
            name="uq_execution_budget_reservations_quote",
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    authorization_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_budget_heads.authorization_sha256"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.attempt_id"), index=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_heads.execution_id"), index=True
    )
    cost_quote_sha256: Mapped[str] = mapped_column(String(64))
    currency_code: Mapped[str] = mapped_column(String(3))
    fixed_charge_microunits: Mapped[int] = mapped_column(BigInteger)
    charge_per_second_microunits: Mapped[int] = mapped_column(BigInteger)
    maximum_lease_seconds: Mapped[int] = mapped_column(BigInteger)
    actual_lease_seconds: Mapped[int | None] = mapped_column(BigInteger)
    held_microunits: Mapped[int] = mapped_column(BigInteger)
    settled_microunits: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(32), index=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _ExecutionBudgetEventRecord(Base):
    __tablename__ = "execution_budget_events"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_execution_budget_events_sequence"),
        CheckConstraint(
            "event_type IN ('reserved','reconciliation_required','adopted','settled','released')",
            name="ck_execution_budget_events_type",
        ),
        CheckConstraint(
            f"event_sha256 {_SHA256_SQL} AND payload_sha256 {_SHA256_SQL} "
            f"AND (previous_event_sha256 IS NULL OR previous_event_sha256 {_SHA256_SQL})",
            name="ck_execution_budget_events_hashes",
        ),
        CheckConstraint(
            "(sequence = 1 AND previous_event_sha256 IS NULL) OR "
            "(sequence > 1 AND previous_event_sha256 IS NOT NULL)",
            name="ck_execution_budget_events_chain_shape",
        ),
        UniqueConstraint(
            "reservation_id",
            "sequence",
            name="uq_execution_budget_events_sequence",
        ),
        UniqueConstraint("event_sha256", name="uq_execution_budget_events_hash"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_sha256: Mapped[str] = mapped_column(String(64))
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("execution_budget_reservations.reservation_id"), index=True
    )
    authorization_sha256: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    previous_event_sha256: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    reserved_delta_microunits: Mapped[int] = mapped_column(BigInteger)
    spent_delta_microunits: Mapped[int] = mapped_column(BigInteger)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionTerminalReceiptRecord(Base):
    __tablename__ = "execution_terminal_receipts"
    __table_args__ = (
        CheckConstraint(
            "terminal_state IN ('engineering_succeeded','execution_failed','cancelled')",
            name="ck_execution_terminal_receipts_state",
        ),
        CheckConstraint(
            f"receipt_sha256 {_SHA256_SQL} AND intent_sha256 {_SHA256_SQL} "
            f"AND resource_lease_sha256 {_SHA256_SQL} "
            f"AND node_execution_receipt_sha256 {_SHA256_SQL} "
            f"AND terminal_verification_attestation_sha256 {_SHA256_SQL} "
            f"AND terminal_verification_authority_pin_sha256 {_SHA256_SQL} "
            f"AND terminal_verification_policy_sha256 {_SHA256_SQL} "
            f"AND terminal_verification_key_id {_SHA256_SQL} "
            f"AND payload_sha256 {_SHA256_SQL} "
            f"AND artifact_manifest_sha256 {_SHA256_SQL}",
            name="ck_execution_terminal_receipts_hashes",
        ),
        CheckConstraint(
            "terminal_verification_attestation_json->>'signature_ed25519_hex' "
            "~ '^[0-9a-f]{128}$' AND "
            "terminal_verification_authority_pin_json->>'public_key_ed25519_hex' "
            "~ '^[0-9a-f]{64}$'",
            name="ck_execution_terminal_receipts_verification_keys",
        ),
        UniqueConstraint("attempt_id", name="uq_execution_terminal_receipts_attempt"),
        UniqueConstraint(
            "terminal_verification_attestation_sha256",
            name="uq_execution_terminal_receipts_verification_attestation",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "execution_id"],
            ["execution_attempts.attempt_id", "execution_attempts.execution_id"],
            name="fk_execution_terminal_receipts_attempt_execution",
        ),
    )

    receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    intent_sha256: Mapped[str] = mapped_column(String(64))
    resource_lease_sha256: Mapped[str] = mapped_column(String(64))
    node_execution_receipt_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    node_execution_receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    terminal_verification_attestation_sha256: Mapped[str] = mapped_column(String(64))
    terminal_verification_attestation_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    terminal_verification_authority_pin_sha256: Mapped[str] = mapped_column(String(64))
    terminal_verification_authority_pin_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    terminal_verification_policy_sha256: Mapped[str] = mapped_column(String(64))
    terminal_verification_key_id: Mapped[str] = mapped_column(String(64))
    terminal_state: Mapped[str] = mapped_column(String(32), index=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    artifact_manifest_sha256: Mapped[str] = mapped_column(String(64))
    artifact_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    artifact_verified_receipt_sha256s_json: Mapped[list[str]] = mapped_column(JSONB)
    committed_by_principal_id: Mapped[str] = mapped_column(String(128))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _ExecutionOutboxRecord(Base):
    __tablename__ = "execution_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','published') AND publish_attempts >= 0",
            name="ck_execution_outbox_status",
        ),
        CheckConstraint(
            f"payload_sha256 {_SHA256_SQL}",
            name="ck_execution_outbox_payload_hash",
        ),
        CheckConstraint(
            "(status = 'pending' AND published_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL)",
            name="ck_execution_outbox_publish_time",
        ),
        UniqueConstraint("receipt_sha256", name="uq_execution_outbox_receipt"),
        UniqueConstraint("delivery_key", name="uq_execution_outbox_delivery_key"),
    )

    outbox_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    receipt_sha256: Mapped[str] = mapped_column(
        ForeignKey("execution_terminal_receipts.receipt_sha256"), index=True
    )
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    topic: Mapped[str] = mapped_column(String(96))
    delivery_key: Mapped[str] = mapped_column(String(192))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), index=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__: list[str] = []
