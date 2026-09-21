"""External bridge admission: attempts/leases gain a placement mode.

Revision ID: 20260921_0035
Revises: 20260915_0034
Create Date: 2026-09-21

Contradiction #15: engineering qualification had no way to admit an
external-bridge execution because every attempt and resource lease was
node-bound by construction.  The allocator now admits external-mode cost
quotes (one frozen EXTERNAL resource class, deployment-pinned bridge
authority) without local node inventory: ``execution_attempts`` and
``execution_resource_leases`` relax ``node_id`` and the inventory sha to
NULLABLE, gain ``external_resource_class_id``, and a placement-mode CHECK
keeps the two modes exclusive at the database level.  The sha-pattern
CHECKs widen so a NULL inventory sha passes.  The runtime ORM in
``aletheia/execution/persistence.py`` changes identically in the same
commit.  No data changes: existing rows are all node-mode and satisfy the
new constraints unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260921_0035"
down_revision: str | None = "20260915_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256_SQL = "~ '^[0-9a-f]{64}$'"
_NARROW_INVENTORY = f"node_inventory_sha256 {_SHA256_SQL}"
_WIDE_INVENTORY = (
    f"(node_inventory_sha256 IS NULL OR node_inventory_sha256 {_SHA256_SQL})"
)


def _attempt_hashes(inventory_clause: str) -> str:
    optional = (
        "latest_adoption_sha256",
        "last_runtime_inspection_sha256",
        "runtime_identity_sha256",
        "terminal_receipt_sha256",
        "runtime_preparation_sha256",
        "latest_runtime_launch_authorization_sha256",
        "latest_pre_runtime_absence_receipt_sha256",
        "node_runtime_launch_receipt_sha256",
        "runtime_termination_challenge_sha256",
        "accepted_runtime_termination_sha256",
        "accepted_terminal_submission_sha256",
        "terminal_deadline_expiration_sha256",
    )
    required = (
        "intent_sha256",
        "admission_sha256",
        "grant_sha256",
        "bundle_sha256",
        "cost_quote_sha256",
        "lease_token_sha256",
    )
    clauses = [f"{item} {_SHA256_SQL}" for item in required]
    clauses.append(inventory_clause)
    clauses.extend(f"({item} IS NULL OR {item} {_SHA256_SQL})" for item in optional)
    return " AND ".join(clauses)
_PLACEMENT_MODE = (
    "((node_id IS NULL) = (external_resource_class_id IS NOT NULL)) "
    "AND ((node_id IS NULL) = (node_inventory_sha256 IS NULL))"
)
_LEASE_PLACEMENT_MODE = (
    "((node_id IS NULL) = (external_resource_class_id IS NOT NULL)) "
    "AND ((node_id IS NULL) = (inventory_sha256 IS NULL))"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE execution_attempts"
        " ALTER COLUMN node_id DROP NOT NULL,"
        " ALTER COLUMN node_inventory_sha256 DROP NOT NULL,"
        " ADD COLUMN IF NOT EXISTS external_resource_class_id VARCHAR(128)"
    )
    op.execute(
        "ALTER TABLE execution_attempts DROP CONSTRAINT ck_execution_attempts_hashes"
    )
    op.execute(
        "ALTER TABLE execution_attempts"
        " ADD CONSTRAINT ck_execution_attempts_hashes"
        f" CHECK ({_attempt_hashes(_WIDE_INVENTORY)})"
    )
    op.execute(
        "ALTER TABLE execution_attempts"
        " ADD CONSTRAINT ck_execution_attempts_placement_mode"
        f" CHECK ({_PLACEMENT_MODE})"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " ALTER COLUMN node_id DROP NOT NULL,"
        " ALTER COLUMN inventory_sha256 DROP NOT NULL,"
        " ADD COLUMN IF NOT EXISTS external_resource_class_id VARCHAR(128)"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " DROP CONSTRAINT ck_execution_resource_leases_hashes"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " ADD CONSTRAINT ck_execution_resource_leases_hashes"
        " CHECK ((inventory_sha256 IS NULL OR inventory_sha256 "
        f"{_SHA256_SQL}) AND lease_sha256 {_SHA256_SQL})"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " ADD CONSTRAINT ck_execution_resource_leases_placement_mode"
        f" CHECK ({_LEASE_PLACEMENT_MODE})"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " DROP CONSTRAINT ck_execution_resource_leases_placement_mode,"
        " DROP CONSTRAINT ck_execution_resource_leases_hashes"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " ADD CONSTRAINT ck_execution_resource_leases_hashes"
        " CHECK (inventory_sha256 "
        f"{_SHA256_SQL} AND lease_sha256 {_SHA256_SQL})"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases DROP COLUMN external_resource_class_id"
    )
    op.execute(
        "ALTER TABLE execution_resource_leases"
        " ALTER COLUMN node_id SET NOT NULL,"
        " ALTER COLUMN inventory_sha256 SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE execution_attempts"
        " DROP CONSTRAINT ck_execution_attempts_placement_mode,"
        " DROP CONSTRAINT ck_execution_attempts_hashes"
    )
    op.execute(
        "ALTER TABLE execution_attempts"
        " ADD CONSTRAINT ck_execution_attempts_hashes"
        f" CHECK ({_attempt_hashes(_NARROW_INVENTORY)})"
    )
    op.execute("ALTER TABLE execution_attempts DROP COLUMN external_resource_class_id")
    op.execute(
        "ALTER TABLE execution_attempts"
        " ALTER COLUMN node_id SET NOT NULL,"
        " ALTER COLUMN node_inventory_sha256 SET NOT NULL"
    )
