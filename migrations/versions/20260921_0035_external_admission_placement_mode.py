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
CHECKs widen so a NULL inventory sha passes.  The frozen 0024/0025
constraint triggers are rewritten in the same migration: the attempt-bundle
function keeps every node-mode guard verbatim behind
``attempt_row.node_id IS NOT NULL`` and gains external-mode mirrors (lease
payload/class agreement against the admission bundle, the frozen-catalog
class profile, and the grant/quote/budget deadline window; the catalog
mirror joins only on fields ``model_dump`` serializes — the derived
``resource_class_id`` never serializes, so the attempt's class id ties to
the catalog through the quote selection and the intent's accepted ids),
the assignment
envelope requirement and the node-capacity head check skip nodeless rows,
and the lease/attempt authority-field comparison moves to
``IS DISTINCT FROM`` so NULL placement fields compare exactly.  The
runtime ORM in ``aletheia/execution/persistence.py`` changes identically in
the same commit.  No data changes: existing rows are all node-mode and
satisfy the new constraints unchanged.
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
_WIDE_INVENTORY = f"(node_inventory_sha256 IS NULL OR node_inventory_sha256 {_SHA256_SQL})"


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
    _rewrite_placement_authority_triggers(relaxed=True)
    op.execute(
        "ALTER TABLE execution_attempts"
        " ALTER COLUMN node_id DROP NOT NULL,"
        " ALTER COLUMN node_inventory_sha256 DROP NOT NULL,"
        " ADD COLUMN IF NOT EXISTS external_resource_class_id VARCHAR(128)"
    )
    op.execute("ALTER TABLE execution_attempts DROP CONSTRAINT ck_execution_attempts_hashes")
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
        "ALTER TABLE execution_resource_leases DROP CONSTRAINT ck_execution_resource_leases_hashes"
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
    _rewrite_placement_authority_triggers(relaxed=False)
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
    op.execute("ALTER TABLE execution_resource_leases DROP COLUMN external_resource_class_id")
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


_ENVELOPE_NODELESS = (
    "          SELECT * INTO attempt_row FROM execution_attempts"
    " WHERE attempt_id = target_attempt;\n"
    "          IF NOT FOUND THEN\n"
    "            RETURN NULL;\n"
    "          END IF;\n"
)
_ENVELOPE_NODELESS_RELAXED = (
    _ENVELOPE_NODELESS + "          IF attempt_row.node_id IS NULL THEN\n"
    "            RETURN NULL;\n"
    "          END IF;\n"
)
_CAPACITY_HEAD = (
    "        BEGIN\n          SELECT COALESCE(sum(cpu_cores),0), COALESCE(sum(memory_bytes),0),\n"
)
_CAPACITY_HEAD_RELAXED = (
    "        BEGIN\n"
    "          IF target_node IS NULL THEN RETURN NULL; END IF;\n"
    "          SELECT COALESCE(sum(cpu_cores),0), COALESCE(sum(memory_bytes),0),\n"
)
_AUTHORITY_FIELDS = (
    "          IF lease_row.node_id <> attempt_row.node_id OR\n"
    "             lease_row.inventory_sha256 <> attempt_row.node_inventory_sha256 OR\n"
)
_AUTHORITY_FIELDS_RELAXED = (
    "          IF lease_row.node_id IS DISTINCT FROM attempt_row.node_id OR\n"
    "             lease_row.inventory_sha256 IS DISTINCT FROM"
    " attempt_row.node_inventory_sha256 OR\n"
    "             lease_row.external_resource_class_id IS DISTINCT FROM\n"
    "               attempt_row.external_resource_class_id OR\n"
)
_GUARD_TWO_HEAD = (
    "          IF NOT EXISTS (\n"
    "            SELECT 1 FROM execution_qualification_admissions q\n"
    "             WHERE q.admission_sha256 = attempt_row.admission_sha256\n"
    "               AND lease_row.cpu_cores =\n"
)
_GUARD_TWO_HEAD_RELAXED = (
    "          IF attempt_row.node_id IS NOT NULL AND NOT EXISTS (\n"
    "            SELECT 1 FROM execution_qualification_admissions q\n"
    "             WHERE q.admission_sha256 = attempt_row.admission_sha256\n"
    "               AND lease_row.cpu_cores =\n"
)
_GUARD_THREE_HEAD = (
    "          IF NOT EXISTS (\n            SELECT 1 FROM execution_inventory_attestations i\n"
)
_GUARD_THREE_HEAD_RELAXED = (
    "          IF attempt_row.node_id IS NOT NULL AND NOT EXISTS (\n"
    "            SELECT 1 FROM execution_inventory_attestations i\n"
)
_GUARD_FOUR_HEAD = (
    "          IF NOT EXISTS (\n"
    "            SELECT 1 FROM execution_qualification_admissions q\n"
    "             JOIN execution_nodes n ON n.node_id = attempt_row.node_id\n"
)
_GUARD_FOUR_HEAD_RELAXED = (
    "          IF attempt_row.node_id IS NOT NULL AND NOT EXISTS (\n"
    "            SELECT 1 FROM execution_qualification_admissions q\n"
    "             JOIN execution_nodes n ON n.node_id = attempt_row.node_id\n"
)
_DEVICE_COUNT_ANCHOR = "          SELECT count(*) INTO device_count FROM execution_device_leases\n"
_EXTERNAL_LEASE_GUARD = """
          IF attempt_row.node_id IS NULL AND NOT EXISTS (
            SELECT 1 FROM execution_qualification_admissions q
             WHERE q.admission_sha256 = attempt_row.admission_sha256
               AND lease_row.external_resource_class_id =
                   attempt_row.external_resource_class_id
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
               AND lease_row.lease_json->>'external_resource_class_id' =
                   attempt_row.external_resource_class_id
               AND lease_row.lease_json->'selected_resource_ids' =
                   q.bundle_json->'cost_quote'->'selected_resource_ids'
               AND (lease_row.lease_json->>'fencing_epoch_at_acquisition')::bigint =
                   attempt_row.fencing_epoch - attempt_row.adoption_count
               AND (lease_row.lease_json->>'acquired_at')::timestamptz =
                   lease_row.acquired_at
               AND (lease_row.lease_json->>'hard_deadline')::timestamptz =
                   attempt_row.hard_deadline
               AND q.bundle_json->'cost_quote'->>'selected_external_resource_class_id' =
                   attempt_row.external_resource_class_id
               AND q.bundle_json->'cost_quote'->>'selected_node_manifest_sha256' IS NULL
               AND q.bundle_json->'intent'->>'external_action_kind' IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'external resource lease differs from exact intent/quote payload'
              USING ERRCODE = '23514';
          END IF;
          IF attempt_row.node_id IS NULL AND NOT EXISTS (
            SELECT 1
              FROM execution_qualification_admissions q,
                   jsonb_array_elements(
                     q.bundle_json->'compilation_request'->'resource_catalog'
                       ->'resource_classes'
                   ) cls(value)
             WHERE q.admission_sha256 = attempt_row.admission_sha256
               AND cls.value->>'kind' = 'external'
               AND q.bundle_json->'intent'->>'external_action_kind' = ANY (
                   SELECT jsonb_array_elements_text(cls.value->'external_action_kinds'))
               AND cls.value->'network_policies' @> '["none"]'::jsonb
               AND (cls.value->>'cpu_cores')::integer >=
                   (q.bundle_json->'intent'->'resource_request'->>'cpu_cores')::integer
               AND (cls.value->>'memory_bytes')::bigint >=
                   (q.bundle_json->'intent'->'resource_request'->>'memory_bytes')::bigint
               AND (cls.value->>'scratch_bytes')::bigint >=
                   (q.bundle_json->'intent'->'resource_request'->>'scratch_bytes')::bigint
               AND NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements_text(
                          q.bundle_json->'intent'->'resource_request'
                            ->'required_features'
                        ) req(feature)
                  WHERE NOT (cls.value->'features' @> to_jsonb(ARRAY[req.feature]))
               )
               AND attempt_row.external_resource_class_id = ANY (
                   SELECT jsonb_array_elements_text(
                     q.bundle_json->'intent'->'resource_request'
                       ->'accepted_resource_class_ids'
                   ))
          ) THEN
            RAISE EXCEPTION 'external placement class is absent from the frozen catalog'
              USING ERRCODE = '23514';
          END IF;
          IF attempt_row.node_id IS NULL AND NOT EXISTS (
            SELECT 1 FROM execution_qualification_admissions q
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
          ) THEN
            RAISE EXCEPTION
              'external attempt lease deadline exceeds its exact grant/quote/budget window'
              USING ERRCODE = '23514';
          END IF;
"""
_DEVICE_COUNT_ANCHOR_RELAXED = _EXTERNAL_LEASE_GUARD + _DEVICE_COUNT_ANCHOR


def _rewrite_placement_authority_triggers(*, relaxed: bool) -> None:
    """Rewrite the frozen 0024/0025 placement triggers for placement mode.

    Every node-mode guard is kept verbatim; each edit either prefixes a guard
    with ``attempt_row.node_id IS NOT NULL``, switches the lease/attempt field
    comparison to NULL-exact ``IS DISTINCT FROM``, or inserts external-mode
    mirrors.  ``relaxed=False`` applies the exact reverse edits so downgrade
    restores the frozen bodies.
    """

    plans = (
        (
            "aletheia_execution_check_assignment_envelope()",
            "0025 assignment envelope attempt fetch",
            (_ENVELOPE_NODELESS, _ENVELOPE_NODELESS_RELAXED),
        ),
        (
            "aletheia_execution_check_node_capacity()",
            "0024 node capacity head baseline",
            (_CAPACITY_HEAD, _CAPACITY_HEAD_RELAXED),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0024 attempt/lease authority fields",
            (_AUTHORITY_FIELDS, _AUTHORITY_FIELDS_RELAXED),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0024 attempt lease-payload guard",
            (_GUARD_TWO_HEAD, _GUARD_TWO_HEAD_RELAXED),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0024 attempt inventory placement guard",
            (_GUARD_THREE_HEAD, _GUARD_THREE_HEAD_RELAXED),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0024 attempt deadline window guard",
            (_GUARD_FOUR_HEAD, _GUARD_FOUR_HEAD_RELAXED),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0024 attempt device-count anchor",
            (_DEVICE_COUNT_ANCHOR, _DEVICE_COUNT_ANCHOR_RELAXED),
        ),
    )
    for index, (function_name, label, (needle, replacement)) in enumerate(plans):
        old, new = (needle, replacement) if relaxed else (replacement, needle)
        op.execute(
            f"""
            DO $migration$
            DECLARE
              definition text;
            BEGIN
              SELECT pg_get_functiondef('{function_name}'::regprocedure) INTO definition;
              IF position($needle${old}$needle$ IN definition) = 0 THEN
                RAISE EXCEPTION '{label} is not the expected frozen form';
              END IF;
              EXECUTE replace(
                definition, $needle${old}$needle$, $needle${new}$needle$
              );
            END;
            $migration$;
            """
        )
