"""Admit one closed attempt-scoped cleanup authority after source-node expiry.

Revision ID: 20260903_0032
Revises: 20260831_0031
Create Date: 2026-09-03

The existing absence rows remain the only durable authority.  This revision changes no table and
does not relax ordinary node signatures.  It extends the closed JSON validator with a second,
explicit shape and requires that shape to bind one historical launch authorization, one source
node/manifest, one absence epoch, one independently pinned key, and a release-only decision.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260903_0032"
down_revision: str | None = "20260831_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_FUNCTION = "aletheia_execution_runtime_v2_json_valid"
_ATTEMPT_FUNCTION = "aletheia_execution_check_runtime_v2_attempt"

_LEGACY_RECEIPT_SHAPE = """            WHEN 'aletheia.pre_runtime_absence_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","preparation":"object",' ||
              '"preparation_sha256":"string","absence_evidence":"object",' ||
              '"absence_evidence_sha256":"string","signed_at":"string",' ||
              '"expires_at":"string","signing_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'"""

_RECOVERY_RECEIPT_SHAPE = """            WHEN 'aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"policy_sha256":"string","principal_id":"string","key_id":"string",' ||
              '"public_key_ed25519_hex":"string","source_node_id":"string",' ||
              '"source_node_manifest_sha256":"string",' ||
              '"infrastructure_attempt_id":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"runtime_launch_authorization_sha256":"string",' ||
              '"cleanup_absence_epoch":"number","watchdog_deployment_sha256":"string",' ||
              '"valid_from":"string","expires_at":"string","revoked_at":"null|string",' ||
              '"cleanup_only":"boolean","launch_allowed":"boolean",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.pre_runtime_absence_receipt' THEN
              CASE WHEN value ? 'cleanup_recovery_authority' OR
                             value ? 'cleanup_recovery_authority_sha256' THEN
                '{"schema_name":"string","schema_version":"number",' ||
                '"node_manifest_sha256":"string","preparation":"object",' ||
                '"preparation_sha256":"string","absence_evidence":"object",' ||
                '"absence_evidence_sha256":"string","signed_at":"string",' ||
                '"expires_at":"string","signing_key_id":"string",' ||
                '"signature_ed25519_hex":"string",' ||
                '"cleanup_recovery_authority":"object",' ||
                '"cleanup_recovery_authority_sha256":"string",' ||
                '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
              ELSE
                '{"schema_name":"string","schema_version":"number",' ||
                '"node_manifest_sha256":"string","preparation":"object",' ||
                '"preparation_sha256":"string","absence_evidence":"object",' ||
                '"absence_evidence_sha256":"string","signed_at":"string",' ||
                '"expires_at":"string","signing_key_id":"string",' ||
                '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
                '"scientific_admission_allowed":"boolean"}'
              END"""

_LEGACY_DECISION_SHAPE = """            WHEN 'aletheia.pre_runtime_absence_decision_record' THEN
              '{"schema_name":"string","schema_version":"number","attempt_id":"string",' ||
              '"absence_epoch":"number","absence_receipt_sha256":"string",' ||
              '"preparation_sha256":"string",' ||
              '"prior_authorization_request_sha256":"null|string",' ||
              '"prior_authorization_sha256":"null|string","disposition":"string",' ||
              '"replacement_request_sha256":"null|string",' ||
              '"replacement_authorization_sha256":"null|string","decided_at":"string",' ||
              '"runtime_control_pin_sha256":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'"""

_RECOVERY_DECISION_SHAPE = """            WHEN 'aletheia.pre_runtime_absence_decision_record' THEN
              CASE WHEN value ? 'pre_runtime_cleanup_recovery_authority_sha256' THEN
                '{"schema_name":"string","schema_version":"number","attempt_id":"string",' ||
                '"absence_epoch":"number","absence_receipt_sha256":"string",' ||
                '"preparation_sha256":"string",' ||
                '"prior_authorization_request_sha256":"null|string",' ||
                '"prior_authorization_sha256":"null|string","disposition":"string",' ||
                '"replacement_request_sha256":"null|string",' ||
                '"replacement_authorization_sha256":"null|string","decided_at":"string",' ||
                '"runtime_control_pin_sha256":"string",' ||
                '"pre_runtime_cleanup_recovery_authority_sha256":"string",' ||
                '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
              ELSE
                '{"schema_name":"string","schema_version":"number","attempt_id":"string",' ||
                '"absence_epoch":"number","absence_receipt_sha256":"string",' ||
                '"preparation_sha256":"string",' ||
                '"prior_authorization_request_sha256":"null|string",' ||
                '"prior_authorization_sha256":"null|string","disposition":"string",' ||
                '"replacement_request_sha256":"null|string",' ||
                '"replacement_authorization_sha256":"null|string","decided_at":"string",' ||
                '"runtime_control_pin_sha256":"string","qualification_only":"boolean",' ||
                '"scientific_admission_allowed":"boolean"}'
              END"""

_LEGACY_SCHEMA_VERSION = """                'aletheia.node_runtime_identity',
                'aletheia.artifact_manifest',"""
_RECOVERY_SCHEMA_VERSION = """                'aletheia.node_runtime_identity',
                'aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin',
                'aletheia.artifact_manifest',"""

_LEGACY_PIN_VALIDATION = (
    """          IF expected_schema = 'aletheia.runtime_control_authority_pin' AND NOT ("""
)
_RECOVERY_PIN_VALIDATION = """          IF expected_schema =
             'aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin' AND NOT (
               value->>'cleanup_only' IS NOT DISTINCT FROM 'true' AND
               value->>'launch_allowed' IS NOT DISTINCT FROM 'false' AND
               value->>'qualification_only' IS NOT DISTINCT FROM 'true' AND
               value->>'scientific_admission_allowed' IS NOT DISTINCT FROM 'false' AND
               (value->>'valid_from')::timestamptz <
                 (value->>'expires_at')::timestamptz AND
               (value->>'expires_at')::timestamptz -
                 (value->>'valid_from')::timestamptz <= interval '1 hour' AND
               (
                 jsonb_typeof(value->'revoked_at') = 'null' OR
                 (value->>'revoked_at')::timestamptz BETWEEN
                   (value->>'valid_from')::timestamptz AND
                   (value->>'expires_at')::timestamptz
               )
             ) THEN
            RETURN false;
          END IF;
          IF expected_schema = 'aletheia.runtime_control_authority_pin' AND NOT ("""

_LEGACY_ABSENCE_JSON_GUARD = """               NOT aletheia_execution_runtime_v2_json_valid(
                 d.decision_json, 'aletheia.pre_runtime_absence_decision_record') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin'))"""

_RECOVERY_ABSENCE_JSON_GUARD = """               NOT aletheia_execution_runtime_v2_json_valid(
                 d.decision_json, 'aletheia.pre_runtime_absence_decision_record') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin') OR
               ((d.absence_receipt_json ? 'cleanup_recovery_authority') IS DISTINCT FROM
                (d.absence_receipt_json ? 'cleanup_recovery_authority_sha256')) OR
               ((d.absence_receipt_json ? 'cleanup_recovery_authority') IS DISTINCT FROM
                (d.decision_json ?
                  'pre_runtime_cleanup_recovery_authority_sha256')) OR
               (d.absence_receipt_json ? 'cleanup_recovery_authority' AND (
                 NOT aletheia_execution_runtime_v2_json_valid(
                   d.absence_receipt_json->'cleanup_recovery_authority',
                   'aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin') OR
                 d.absence_receipt_json->>'cleanup_recovery_authority_sha256' IS DISTINCT FROM
                   d.decision_json->>'pre_runtime_cleanup_recovery_authority_sha256' OR
                 d.absence_receipt_json->>'signing_key_id' IS DISTINCT FROM
                   d.absence_receipt_json->'cleanup_recovery_authority'->>'key_id' OR
                 d.absence_receipt_json->>'node_manifest_sha256' IS DISTINCT FROM
                   d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'source_node_manifest_sha256' OR
                 d.absence_receipt_json->'preparation'->>'node_id' IS DISTINCT FROM
                   d.absence_receipt_json->'cleanup_recovery_authority'->>'source_node_id' OR
                 d.absence_receipt_json->'preparation'->>'infrastructure_attempt_id'
                   IS DISTINCT FROM d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'infrastructure_attempt_id' OR
                 d.absence_receipt_json->>'preparation_sha256' IS DISTINCT FROM
                   d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'runtime_preparation_sha256' OR
                 d.absence_receipt_json->'absence_evidence'->>'prelaunch_authorization_sha256'
                   IS DISTINCT FROM d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'runtime_launch_authorization_sha256' OR
                 (d.absence_receipt_json->'absence_evidence'->>
                   'prelaunch_absence_epoch')::integer IS DISTINCT FROM
                   (d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'cleanup_absence_epoch')::integer
               )))"""

_LEGACY_RUNTIME_PIN_TIME_GUARD = """               d.runtime_control_pin_json->>'policy_sha256' IS NULL OR
               d.decided_at <
                 (d.runtime_control_pin_json->>'valid_from')::timestamptz OR
               d.decided_at >= LEAST(
                 (d.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (d.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (d.runtime_control_pin_json->>'expires_at')::timestamptz)) OR"""

_RECOVERY_RUNTIME_PIN_TIME_GUARD = """               d.runtime_control_pin_json->>'policy_sha256' IS NULL OR
               (NOT (d.absence_receipt_json ? 'cleanup_recovery_authority') AND (
                 d.decided_at <
                   (d.runtime_control_pin_json->>'valid_from')::timestamptz OR
                 d.decided_at >= LEAST(
                   (d.runtime_control_pin_json->>'expires_at')::timestamptz,
                   COALESCE(
                   (d.runtime_control_pin_json->>'revoked_at')::timestamptz,
                     (d.runtime_control_pin_json->>'expires_at')::timestamptz)))) OR
               (d.absence_receipt_json ? 'cleanup_recovery_authority' AND (
                 d.disposition IS DISTINCT FROM 'released' OR
                 d.replacement_request_sha256 IS NOT NULL OR
                 d.replacement_authorization_sha256 IS NOT NULL OR
                 d.prior_authorization_sha256 IS DISTINCT FROM
                   d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'runtime_launch_authorization_sha256' OR
                 (d.absence_receipt_json->>'signed_at')::timestamptz <
                   (d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'valid_from')::timestamptz OR
                 (d.absence_receipt_json->>'signed_at')::timestamptz >= LEAST(
                   (d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'expires_at')::timestamptz,
                   COALESCE(
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'revoked_at')::timestamptz,
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'expires_at')::timestamptz)) OR
                 (d.absence_receipt_json->>'expires_at')::timestamptz > LEAST(
                   (d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'expires_at')::timestamptz,
                   COALESCE(
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'revoked_at')::timestamptz,
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'expires_at')::timestamptz)) OR
                 d.decided_at < (d.absence_receipt_json->'cleanup_recovery_authority'->>
                   'valid_from')::timestamptz OR
                 d.decided_at >= LEAST(
                   (d.absence_receipt_json->'cleanup_recovery_authority'->>
                     'expires_at')::timestamptz,
                   COALESCE(
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'revoked_at')::timestamptz,
                     (d.absence_receipt_json->'cleanup_recovery_authority'->>
                       'expires_at')::timestamptz)))) OR"""


def _replace_fragment(*, function_name: str, old: str, new: str, label: str) -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE
          definition text;
          rewritten text;
        BEGIN
          SELECT pg_get_functiondef(to_regprocedure('public.{function_name}(jsonb,text)'))
            INTO definition
           WHERE '{function_name}' = '{_JSON_FUNCTION}';
          IF definition IS NULL THEN
            SELECT pg_get_functiondef(to_regprocedure('public.{function_name}()'))
              INTO definition;
          END IF;
          IF definition IS NULL OR position($old${old}$old$ in definition) = 0 THEN
            RAISE EXCEPTION '0032 found unexpected {label} definition';
          END IF;
          rewritten := replace(definition, $old${old}$old$, $new${new}$new$);
          IF rewritten = definition THEN
            RAISE EXCEPTION '0032 did not replace exactly the expected {label} fragment';
          END IF;
          EXECUTE rewritten;
        END;
        $migration$;
        """
    )


def _upgrade_pairs() -> tuple[tuple[str, str, str, str], ...]:
    return (
        (_JSON_FUNCTION, _LEGACY_RECEIPT_SHAPE, _RECOVERY_RECEIPT_SHAPE, "receipt shape"),
        (_JSON_FUNCTION, _LEGACY_DECISION_SHAPE, _RECOVERY_DECISION_SHAPE, "decision shape"),
        (_JSON_FUNCTION, _LEGACY_SCHEMA_VERSION, _RECOVERY_SCHEMA_VERSION, "schema version"),
        (_JSON_FUNCTION, _LEGACY_PIN_VALIDATION, _RECOVERY_PIN_VALIDATION, "pin validation"),
        (
            _ATTEMPT_FUNCTION,
            _LEGACY_ABSENCE_JSON_GUARD,
            _RECOVERY_ABSENCE_JSON_GUARD,
            "absence JSON guard",
        ),
        (
            _ATTEMPT_FUNCTION,
            _LEGACY_RUNTIME_PIN_TIME_GUARD,
            _RECOVERY_RUNTIME_PIN_TIME_GUARD,
            "runtime pin time guard",
        ),
    )


def upgrade() -> None:
    for function_name, old, new, label in _upgrade_pairs():
        _replace_fragment(function_name=function_name, old=old, new=new, label=label)


def downgrade() -> None:
    for function_name, old, new, label in reversed(_upgrade_pairs()):
        _replace_fragment(function_name=function_name, old=new, new=old, label=label)
