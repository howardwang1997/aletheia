"""Opt-in PostgreSQL regression test for the external acceptance chain.

Migration 0036 mirrors the node custody graph for nodeless attempts: seven
append-only twin tables, a closed-schema JSON catalog, a per-transition head
guard delegated from the frozen 0026 trigger, a completeness guard that runs
at COMMIT, and a placement-aware outbox authority trigger.  None of that is
exercised by applying the migration alone, and the allocator suite that will
eventually drive it is gated on the loopback scratch databases.  This test
commits bypassed-writer-shaped rows directly against a fully migrated schema
so the database-level chain is exercised in CI: the honest seven-stage ladder
(reserved → prepared → authorized → launched → challenged → terminated →
terminal, including the compute-release settle tail and the outbox publish)
commits stage by stage, the deadline-expiration branch commits from the
verifying state, and four guard clauses fail closed in isolation — a
non-monotonic authorization head, a rebound preparation pointer, an outbox
row without its authority row, and a mutation of an append-only chain row.

Shas only need internal consistency (column versus column), never real
signatures; times are one deterministic T1-based timeline.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from aletheia.db import expected_schema_revision

UTC = timezone.utc
_T0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(seconds=60)  # reserved_at: every chain time is T1-based
_LEASE_SECONDS = 600
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", None})


@pytest.fixture(scope="module")
def migrated_engine():
    unavailable = (
        pytest.fail if os.environ.get("ALETHEIA_REQUIRE_POSTGRES_TESTS") == "1" else pytest.skip
    )
    raw_url = os.environ.get("ALETHEIA_TEST_MIGRATED_POSTGRES_URL")
    if not raw_url:
        unavailable(
            "external-acceptance-chain regression requires explicit"
            " ALETHEIA_TEST_MIGRATED_POSTGRES_URL"
        )
    url = make_url(raw_url)
    if (
        not url.drivername.startswith("postgresql")
        or url.host not in _LOOPBACK_HOSTS
        or url.database is None
        or not url.database.startswith("aletheia_test")
    ):
        unavailable(
            "external-acceptance-chain regression requires a loopback"
            " aletheia_test* migrated database"
        )
    engine = create_engine(raw_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == expected_schema_revision()
            )
        yield engine
    finally:
        engine.dispose()


def _suffix() -> str:
    return f"{int(datetime.now(UTC).strftime('%H%M%S%f')):032d}"[-32:]


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _sqlstate(integrity_error: IntegrityError) -> str | None:
    original = integrity_error.orig
    return getattr(original, "pgcode", None) or getattr(
        getattr(original, "diag", None), "sqlstate", None
    )


def _chain_documents(suffix: str) -> dict[str, object]:
    """Author every id, sha and document of one honest external chain."""

    def sha(seed: str) -> str:
        return _sha(f"{suffix}:{seed}")

    def sig(seed: str) -> str:
        return sha(seed) + sha(f"{seed}-2")

    quest = f"qst_{suffix}"
    execution = f"exe_{suffix}"
    attempt = f"iat_{suffix}"
    slot = f"sos_{suffix[:29]}"
    class_id = f"rsc_{suffix[:32]}"
    action_kind = "run_cuprate_diagnostic"
    window_end = _T1 + timedelta(hours=2)

    # ---- placement mirror (pattern of the 0035 honest placement seed) -----
    authorization_budget = sha("budget-authorization")
    bundle_sha = sha("bundle")
    quote_sha = sha("quote")
    intent_sha = sha("intent")
    lease_token = sha("lease-token")
    bundle_json = {
        "intent": {
            "execution_id": execution,
            "external_action_kind": action_kind,
            "deadline": _iso(window_end),
            "resource_request": {
                "cpu_cores": 2,
                "memory_bytes": 8_000_000_000,
                "scratch_bytes": 4_000_000_000,
                "exclusive": False,
                "accelerator_count": 0,
                "required_features": ["feature.bridge"],
                "accepted_resource_class_ids": [class_id],
            },
        },
        "cost_quote": {
            "currency_code": "USD",
            "fixed_charge_microunits": 100,
            "charge_per_second_microunits": 5,
            "maximum_lease_seconds": _LEASE_SECONDS,
            "maximum_charge_microunits": 100 + 5 * _LEASE_SECONDS,
            "expires_at": _iso(window_end),
            "selected_external_resource_class_id": class_id,
            "selected_external_resource_class_key": "bridge.acceptance-chain-honest",
            "selected_node_manifest_sha256": None,
            "selected_resource_ids": [],
        },
        "budget_authorization": {"expires_at": _iso(window_end)},
        "compilation_request": {
            "resource_catalog": {
                "resource_classes": [
                    {
                        "class_key": "bridge.acceptance-chain-honest",
                        "kind": "external",
                        "external_action_kinds": [action_kind],
                        "network_policies": ["none"],
                        "cpu_cores": 8,
                        "memory_bytes": 64_000_000_000,
                        "scratch_bytes": 32_000_000_000,
                        "features": ["feature.bridge"],
                    }
                ]
            }
        },
    }
    lease_json = {
        "execution_id": execution,
        "attempt_id": attempt,
        "intent_sha256": intent_sha,
        "external_resource_class_id": class_id,
        "external_resource_class_key": "bridge.acceptance-chain-honest",
        "selected_resource_ids": [],
        "fencing_epoch_at_acquisition": 1,
        "acquired_at": _iso(_T1),
        "hard_deadline": _iso(_T1 + timedelta(seconds=_LEASE_SECONDS)),
    }

    # ---- stage 1: preparation --------------------------------------------
    preparation_sha = sha("preparation")
    bridge_manifest = sha("bridge-manifest")
    prepared_at = _T1 + timedelta(seconds=1)
    preparation_payload = {
        "schema_name": "aletheia.external_runtime_preparation",
        "schema_version": 2,
        "bridge_manifest_sha256": bridge_manifest,
        "execution_id": execution,
        "infrastructure_attempt_id": attempt,
        "intent_sha256": intent_sha,
        "runtime_id": sha("runtime"),
        "runtime_engine": "cuprate-diagnostic",
        "launch_spec_sha256": sha("launch-spec"),
        "workload_executable_sha256": sha("workload-executable"),
        "workload_argv": ["cuprate-diagnostic", "--plan", "plan.json"],
        "runtime_request_sha256": sha("runtime-request"),
        "enforced_placement_sha256": sha("enforced-placement"),
        "input_materialization_receipt_sha256": sha("materialization"),
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "prepared_dispatch_locator_sha256": sha("dispatch-locator"),
        "prepared_at": _iso(prepared_at),
        "prepared_monotonic_ns": 100_000,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- stage 2: launch authorization (CONTROL pin) ----------------------
    request_sha = sha("authorization-request")
    authorization_sha = sha("authorization")
    issued_at = _T1 + timedelta(seconds=3)
    expires_at = _T1 + timedelta(seconds=63)
    lease_expires_at = _T1 + timedelta(seconds=120)
    hard_deadline = _T1 + timedelta(seconds=_LEASE_SECONDS)
    control_policy = sha("control-policy")
    control_principal = "principal:bridge-control"
    control_key = "bridge-control-key-1"
    control_pin = {
        "schema_name": "aletheia.runtime_control_authority_pin",
        "schema_version": 2,
        "policy_sha256": control_policy,
        "principal_id": control_principal,
        "key_id": control_key,
        "public_key_ed25519_hex": sha("control-public-key"),
        "valid_from": _iso(_T1 - timedelta(hours=1)),
        "expires_at": _iso(_T1 + timedelta(hours=1)),
        "revoked_at": None,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    request_json = {
        "schema_name": "aletheia.runtime_launch_authorization_request",
        "schema_version": 2,
        "request_nonce_sha256": sha("request-nonce"),
        "runtime_preparation_sha256": preparation_sha,
        "infrastructure_attempt_id": attempt,
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "pre_runtime_absence_epoch": 0,
        "pre_runtime_absence_receipt_sha256": None,
        "requested_at": _iso(_T1 + timedelta(seconds=2)),
        "requested_monotonic_ns": 105_000,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    authorization_json = {
        "schema_name": "aletheia.external_launch_authorization",
        "schema_version": 2,
        "admission_sha256": sha("admission"),
        "qualification_grant_sha256": sha("grant"),
        "bridge_manifest_sha256": bridge_manifest,
        "execution_id": execution,
        "infrastructure_attempt_id": attempt,
        "intent_sha256": intent_sha,
        "runtime_preparation_sha256": preparation_sha,
        "authorization_request_sha256": request_sha,
        "launch_spec_sha256": sha("launch-spec"),
        "workload_executable_sha256": sha("workload-executable"),
        "workload_argv": ["cuprate-diagnostic", "--plan", "plan.json"],
        "enforced_placement_sha256": sha("enforced-placement"),
        "input_materialization_receipt_sha256": sha("materialization"),
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "lease_expires_at": _iso(lease_expires_at),
        "hard_deadline": _iso(hard_deadline),
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
        "max_launch_delay_ns": 60_000_000_000,
        "runtime_control_policy_sha256": control_policy,
        "authorized_by_principal_id": control_principal,
        "authorization_key_id": control_key,
        "signature_ed25519_hex": sig("authorization-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- stage 3: launch receipt (BRIDGE pin) -----------------------------
    launch_receipt_sha = sha("launch-receipt")
    executor_identity_sha = sha("executor-identity")
    started_at = _T1 + timedelta(seconds=4)
    executor_identity = {
        "schema_name": "aletheia.external_executor_identity",
        "schema_version": 2,
        "execution_id": execution,
        "infrastructure_attempt_id": attempt,
        "runtime_id": sha("runtime"),
        "executor_ref": "bridge://v100ts/cuprate-diagnostic",
        "executor_implementation_sha256": sha("executor-implementation"),
        "invocation_payload_sha256": sha("invocation-payload"),
        "started_at": _iso(started_at),
        "started_monotonic_ns": 110_000,
    }
    observed_at = _T1 + timedelta(seconds=5)
    launch_evidence = {
        "schema_name": "aletheia.external_launch_evidence",
        "schema_version": 2,
        "preparation_sha256": preparation_sha,
        "external_launch_authorization_sha256": authorization_sha,
        "executor_identity": executor_identity,
        "executor_identity_sha256": executor_identity_sha,
        "executor_start_monotonic_lower_bound_ns": 110_000,
        "executor_start_monotonic_upper_bound_exclusive_ns": 112_000,
        "enforced_placement_sha256": sha("enforced-placement"),
        "input_materialization_receipt_sha256": sha("materialization"),
        "enforced_fencing_epoch": 1,
        "enforced_lease_token_sha256": lease_token,
        "launch_evidence_journal_sha256": sha("launch-journal"),
        "observed_at": _iso(observed_at),
        "observed_monotonic_ns": 115_000,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    receipt_signed_at = _T1 + timedelta(seconds=6)
    receipt_accepted_at = _T1 + timedelta(seconds=7)
    launch_receipt_json = {
        "schema_name": "aletheia.external_runtime_launch_receipt",
        "schema_version": 2,
        "bridge_manifest_sha256": bridge_manifest,
        "launch_evidence": launch_evidence,
        "launch_evidence_sha256": sha("launch-evidence"),
        "signed_at": _iso(receipt_signed_at),
        "signing_key_id": "bridge-executor-key-1",
        "signature_ed25519_hex": sig("receipt-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    bridge_pin = {
        "schema_name": "aletheia.runtime_control_authority_pin",
        "schema_version": 2,
        "policy_sha256": sha("bridge-policy"),
        "principal_id": "principal:bridge-executor",
        "key_id": "bridge-executor-key-1",
        "public_key_ed25519_hex": sha("bridge-public-key"),
        "valid_from": _iso(_T1 - timedelta(hours=1)),
        "expires_at": _iso(_T1 + timedelta(hours=1)),
        "revoked_at": None,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- stage 4: termination challenge (CONTROL pin) ---------------------
    challenge_sha = sha("challenge")
    challenge_id = sha("challenge-id")
    challenged_at = _T1 + timedelta(seconds=31)
    challenge_expires_at = _T1 + timedelta(seconds=91)
    artifact_deadline = _T1 + timedelta(seconds=300)
    runtime_ended_at = _T1 + timedelta(seconds=30)
    result_content = sha("result-content")
    resource_lease_sha = sha("lease")
    termination_evidence = {
        "schema_name": "aletheia.external_termination_evidence",
        "schema_version": 2,
        "preparation_sha256": preparation_sha,
        "external_launch_receipt_sha256": launch_receipt_sha,
        "executor_identity_sha256": executor_identity_sha,
        "exit_code": 0,
        "ended_at": _iso(runtime_ended_at),
        "ended_monotonic_ns": 200_000,
        "result_content_sha256": result_content,
        "termination_journal_sha256": sha("termination-journal"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    challenge_json = {
        "schema_name": "aletheia.external_termination_acceptance_challenge",
        "schema_version": 2,
        "challenge_id": challenge_id,
        "attempt_id": attempt,
        "execution_id": execution,
        "intent_sha256": intent_sha,
        "bridge_manifest_sha256": bridge_manifest,
        "runtime_preparation_sha256": preparation_sha,
        "external_runtime_launch_receipt_sha256": launch_receipt_sha,
        "executor_identity_sha256": executor_identity_sha,
        "termination_evidence_sha256": sha("termination-evidence"),
        "result_content_sha256": result_content,
        "resource_lease_sha256": resource_lease_sha,
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "hard_deadline": _iso(hard_deadline),
        "artifact_submission_deadline": _iso(artifact_deadline),
        "challenged_at": _iso(challenged_at),
        "expires_at": _iso(challenge_expires_at),
        "runtime_control_policy_sha256": control_policy,
        "challenged_by_principal_id": control_principal,
        "challenge_key_id": control_key,
        "signature_ed25519_hex": sig("challenge-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- stage 5: termination acceptance + pre-signed expiration ----------
    accepted_termination_sha = sha("accepted-termination")
    termination_receipt_sha = sha("bridge-termination-receipt")
    termination_evidence_sha = sha("termination-evidence")
    proof_signed_at = _T1 + timedelta(seconds=32)
    proof_expires_at = _T1 + timedelta(seconds=92)
    termination_accepted_at = _T1 + timedelta(seconds=33)
    bridge_termination_receipt_json = {
        "schema_name": "aletheia.external_runtime_termination_receipt",
        "schema_version": 2,
        "bridge_manifest_sha256": bridge_manifest,
        "challenge_sha256": challenge_sha,
        "runtime_preparation_sha256": preparation_sha,
        "external_runtime_launch_receipt_sha256": launch_receipt_sha,
        "runtime_launch_authorization_request_sha256": request_sha,
        "external_launch_authorization_sha256": authorization_sha,
        "termination_evidence": termination_evidence,
        "termination_evidence_sha256": termination_evidence_sha,
        "signed_at": _iso(proof_signed_at),
        "expires_at": _iso(proof_expires_at),
        "signing_key_id": "bridge-executor-key-1",
        "signature_ed25519_hex": sig("termination-receipt-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    accepted_termination_json = {
        "schema_name": "aletheia.accepted_external_runtime_termination",
        "schema_version": 2,
        "challenge_sha256": challenge_sha,
        "attempt_id": attempt,
        "runtime_preparation_sha256": preparation_sha,
        "external_runtime_launch_receipt_sha256": launch_receipt_sha,
        "runtime_launch_authorization_request_sha256": request_sha,
        "external_launch_authorization_sha256": authorization_sha,
        "external_runtime_termination_receipt_sha256": termination_receipt_sha,
        "executor_identity_sha256": executor_identity_sha,
        "termination_evidence_sha256": termination_evidence_sha,
        "result_content_sha256": result_content,
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "runtime_ended_at": _iso(runtime_ended_at),
        "exit_code": 0,
        "hard_deadline": _iso(hard_deadline),
        "artifact_submission_deadline": _iso(artifact_deadline),
        "proof_signed_at": _iso(proof_signed_at),
        "proof_expires_at": _iso(proof_expires_at),
        "accepted_at": _iso(termination_accepted_at),
        "billable_ended_at": _iso(runtime_ended_at),
        "runtime_control_policy_sha256": control_policy,
        "accepted_by_principal_id": control_principal,
        "acceptance_key_id": control_key,
        "signature_ed25519_hex": sig("termination-acceptance-signature"),
        "proof_was_fresh": True,
        "compute_release_allowed": True,
        "scientific_admission_allowed": False,
        "qualification_only": True,
    }
    conditional_expiration_sha = sha("conditional-expiration")
    conditional_expiration_json = {
        "schema_name": "aletheia.external_qualification_terminal_deadline_expiration",
        "schema_version": 2,
        "attempt_id": attempt,
        "execution_id": execution,
        "intent_sha256": intent_sha,
        "bridge_manifest_sha256": bridge_manifest,
        "resource_lease_sha256": resource_lease_sha,
        "runtime_preparation_sha256": preparation_sha,
        "runtime_launch_authorization_request_sha256": request_sha,
        "external_launch_authorization_sha256": authorization_sha,
        "external_runtime_launch_receipt_sha256": launch_receipt_sha,
        "external_termination_challenge_sha256": challenge_sha,
        "external_runtime_termination_receipt_sha256": termination_receipt_sha,
        "accepted_external_runtime_termination_sha256": accepted_termination_sha,
        "executor_identity_sha256": executor_identity_sha,
        "termination_evidence_sha256": termination_evidence_sha,
        "result_content_sha256": result_content,
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "runtime_ended_at": _iso(runtime_ended_at),
        "exit_code": 0,
        "hard_deadline": _iso(hard_deadline),
        "artifact_submission_deadline": _iso(artifact_deadline),
        "accepted_runtime_termination_at": _iso(termination_accepted_at),
        "authorized_at": _iso(termination_accepted_at),
        "expired_at": _iso(artifact_deadline),
        "reason": "artifact_submission_deadline_elapsed",
        "disposition": "timeout",
        "retryable": True,
        "conditional_on_terminal_submission_absence": True,
        "database_time_activation_required": True,
        "runtime_control_policy_sha256": control_policy,
        "adjudicated_by_principal_id": control_principal,
        "adjudication_key_id": control_key,
        "signature_ed25519_hex": sig("expiration-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- stage 6: terminal artifacts (acceptance path) --------------------
    accepted_terminal_sha = sha("accepted-terminal")
    terminal_submission_sha = sha("terminal-submission")
    artifact_manifest_sha = sha("artifact-manifest")
    output_tree_sha = sha("output-tree")
    submitted_at = _T1 + timedelta(seconds=60)
    terminal_accepted_at = _T1 + timedelta(seconds=61)
    artifact_receipt_sha = sha("artifact-receipt")
    manifest_entry = {
        "schema_name": "aletheia.artifact_manifest_entry",
        # the frozen runtime v2 catalog pins these artifact schemas at v1
        "schema_version": 1,
        "expected_artifact_id": sha("expected-artifact"),
        "artifact_key": "diagnostic_report",
        "role": "primary_output",
        "content_sha256": sha("artifact-content"),
        "bytes": 2048,
        "media_type": "application/json",
        "schema_sha256": None,
        "quarantine_ref": "quarantine:none",
    }
    artifact_manifest = {
        "schema_name": "aletheia.artifact_manifest",
        "schema_version": 1,
        "intent_sha256": intent_sha,
        "execution_id": execution,
        "replicate_slot_id": slot,
        "infrastructure_attempt_id": attempt,
        "entries": [manifest_entry],
        "produced_at": _iso(runtime_ended_at),
    }
    artifact_receipt = {
        "schema_name": "aletheia.artifact_verified_receipt",
        "schema_version": 1,
        "artifact_manifest_sha256": artifact_manifest_sha,
        "producer_attempt_id": attempt,
        "artifact": manifest_entry,
        "custody_mode": "bridge_custody",
        "verifier_principal_id": control_principal,
        "object_store_id": sha("object-store"),
        "final_object_ref": "bridge://artifacts/diagnostic_report.json",
        "final_object_version": sha("object-version"),
        "custody_receipt_sha256s": [sha("custody-receipt")],
        "verified_at": _iso(_T1 + timedelta(seconds=40)),
    }
    artifact_receipt_sha256s = [artifact_receipt_sha]
    terminal_submission_json = {
        "schema_name": "aletheia.external_qualification_terminal_submission",
        "schema_version": 2,
        "bridge_manifest_sha256": bridge_manifest,
        "intent_sha256": intent_sha,
        "execution_id": execution,
        "attempt_id": attempt,
        "resource_lease_sha256": resource_lease_sha,
        "fencing_epoch": 1,
        "lease_token_sha256": lease_token,
        "accepted_external_runtime_termination_sha256": accepted_termination_sha,
        "artifact_manifest_sha256": artifact_manifest_sha,
        "output_tree_sha256": output_tree_sha,
        "artifact_verified_receipt_sha256s": artifact_receipt_sha256s,
        "disposition": "process_succeeded",
        "submitted_at": _iso(submitted_at),
        "signing_key_id": "bridge-executor-key-1",
        "signature_ed25519_hex": sig("submission-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    accepted_terminal_json = {
        "schema_name": "aletheia.accepted_external_qualification_terminal_submission",
        "schema_version": 2,
        "attempt_id": attempt,
        "bridge_manifest_sha256": bridge_manifest,
        "terminal_submission_sha256": terminal_submission_sha,
        "accepted_external_runtime_termination_sha256": accepted_termination_sha,
        "artifact_manifest_sha256": artifact_manifest_sha,
        "output_tree_sha256": output_tree_sha,
        "artifact_verified_receipt_sha256s": artifact_receipt_sha256s,
        "disposition": "process_succeeded",
        "bridge_submitted_at": _iso(submitted_at),
        "artifact_submission_deadline": _iso(artifact_deadline),
        "accepted_at": _iso(terminal_accepted_at),
        "runtime_control_policy_sha256": control_policy,
        "accepted_by_principal_id": control_principal,
        "acceptance_key_id": control_key,
        "signature_ed25519_hex": sig("terminal-acceptance-signature"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }

    # ---- deterministic settle math ----------------------------------------
    settled_seconds = 33  # acquired_at T1 → settled_at T1+33s
    charged = 100 + 5 * settled_seconds

    return {
        "suffix": suffix,
        "quest": quest,
        "execution": execution,
        "attempt": attempt,
        "slot": slot,
        "class_id": class_id,
        "budget_authorization": authorization_budget,
        "bundle_sha": bundle_sha,
        "quote_sha": quote_sha,
        "intent_sha": intent_sha,
        "lease_token": lease_token,
        "bundle_json": bundle_json,
        "lease_json": lease_json,
        "resource_lease_sha": resource_lease_sha,
        "preparation_sha": preparation_sha,
        "bridge_manifest": bridge_manifest,
        "prepared_at": prepared_at,
        "preparation_payload": preparation_payload,
        "request_sha": request_sha,
        "authorization_sha": authorization_sha,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "lease_expires_at": lease_expires_at,
        "hard_deadline": hard_deadline,
        "control_pin": control_pin,
        "request_json": request_json,
        "authorization_json": authorization_json,
        "launch_receipt_sha": launch_receipt_sha,
        "executor_identity_sha": executor_identity_sha,
        "executor_identity": executor_identity,
        "launch_receipt_json": launch_receipt_json,
        "bridge_pin": bridge_pin,
        "receipt_signed_at": receipt_signed_at,
        "receipt_accepted_at": receipt_accepted_at,
        "challenge_sha": challenge_sha,
        "challenge_id": challenge_id,
        "challenged_at": challenged_at,
        "challenge_expires_at": challenge_expires_at,
        "artifact_deadline": artifact_deadline,
        "runtime_ended_at": runtime_ended_at,
        "result_content": result_content,
        "termination_evidence": termination_evidence,
        "termination_evidence_sha": termination_evidence_sha,
        "challenge_json": challenge_json,
        "accepted_termination_sha": accepted_termination_sha,
        "termination_receipt_sha": termination_receipt_sha,
        "proof_signed_at": proof_signed_at,
        "proof_expires_at": proof_expires_at,
        "termination_accepted_at": termination_accepted_at,
        "bridge_termination_receipt_json": bridge_termination_receipt_json,
        "accepted_termination_json": accepted_termination_json,
        "conditional_expiration_sha": conditional_expiration_sha,
        "conditional_expiration_json": conditional_expiration_json,
        "accepted_terminal_sha": accepted_terminal_sha,
        "terminal_submission_sha": terminal_submission_sha,
        "artifact_manifest_sha": artifact_manifest_sha,
        "output_tree_sha": output_tree_sha,
        "terminal_accepted_at": terminal_accepted_at,
        "terminal_submission_json": terminal_submission_json,
        "accepted_terminal_json": accepted_terminal_json,
        "artifact_manifest": artifact_manifest,
        "artifact_receipt": artifact_receipt,
        "artifact_receipt_sha": artifact_receipt_sha,
        "artifact_receipt_sha256s": artifact_receipt_sha256s,
        "settled_seconds": settled_seconds,
        "charged": charged,
    }


def _js(document: dict[str, object]) -> str:
    return json.dumps(document)


def _seed_placement(engine, docs: dict[str, object]) -> None:
    """Commit the honest external placement every ladder stage builds on."""

    suffix = docs["suffix"]

    def sha(seed: str) -> str:
        return _sha(f"{suffix}:{seed}")

    held = 100 + 5 * _LEASE_SECONDS
    window_end = _T1 + timedelta(hours=2)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_budget_authorizations"
                " (authorization_sha256, quest_id, protocol_sha256, work_order_sha256,"
                "  resource_budget_sha256, source_budget_authorization_sha256,"
                "  currency_code, cap_microunits, authorized_at, expires_at,"
                "  authorized_by_principal_id, payload_sha256, payload_json, registered_at)"
                " VALUES (:a, :quest, :p, :w, :rb, :sb, 'USD', 1000000,"
                "         :authorized, :expires, 'principal:test', :ps,"
                "         CAST(:payload AS jsonb), :authorized)"
            ),
            {
                "a": docs["budget_authorization"],
                "quest": docs["quest"],
                "p": sha("protocol"),
                "w": sha("work-order"),
                "rb": sha("resource-budget"),
                "sb": sha("source-budget"),
                "authorized": _iso(_T0),
                "expires": _iso(_T1 + timedelta(hours=2)),
                "ps": sha("authorization-payload"),
                "payload": json.dumps({"authorization_sha256": docs["budget_authorization"]}),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_budget_heads"
                " (authorization_sha256, currency_code, cap_microunits,"
                "  reserved_microunits, spent_microunits, state_version, updated_at)"
                " VALUES (:a, 'USD', 1000000, :held, 0, 1, :updated)"
            ),
            {"a": docs["budget_authorization"], "held": held, "updated": _iso(_T1)},
        )
        session.execute(
            text(
                "INSERT INTO execution_heads"
                " (execution_id, quest_id, protocol_sha256, work_order_id, work_order_sha256,"
                "  replicate_slot_id, replicate_slot_sha256, last_attempt_number,"
                "  active_attempt_id, state_version, created_at, updated_at)"
                " VALUES (:e, :quest, :p, :wo, :ws, :slot, :slot_sha, 1, :attempt, 1,"
                "         :authorized, :authorized)"
            ),
            {
                "e": docs["execution"],
                "quest": docs["quest"],
                "p": sha("protocol"),
                "wo": f"word_{suffix[:27]}",
                "ws": sha("work-order"),
                "slot": docs["slot"],
                "slot_sha": sha("slot"),
                "attempt": docs["attempt"],
                "authorized": _iso(_T0),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_admissions"
                " (admission_sha256, grant_sha256, bundle_sha256, intent_sha256,"
                "  execution_id, infrastructure_attempt_id, budget_authorization_sha256,"
                "  cost_quote_sha256, authority_policy_sha256, authority_key_id,"
                "  bundle_json, grant_json, verified_receipt_json, verified_at, admitted_at)"
                " VALUES (:a, :grant, :bundle, :intent, :e, :attempt, :auth, :quote,"
                "         :policy, :authority_key, CAST(:bundle_json AS jsonb),"
                "         CAST(:grant_json AS jsonb), CAST('{}' AS jsonb),"
                "         :authorized, :authorized)"
            ),
            {
                "a": sha("admission"),
                "grant": sha("grant"),
                "bundle": docs["bundle_sha"],
                "intent": docs["intent_sha"],
                "e": docs["execution"],
                "attempt": docs["attempt"],
                "auth": docs["budget_authorization"],
                "quote": docs["quote_sha"],
                "policy": sha("policy"),
                "authority_key": sha("authority-key"),
                "bundle_json": _js(docs["bundle_json"]),
                "grant_json": _js({"message": {"expires_at": _iso(window_end)}}),
                "authorized": _iso(_T0),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_attempts"
                " (attempt_id, execution_id, attempt_number, intent_sha256, intent_json,"
                "  admission_sha256, grant_sha256, bundle_sha256, cost_quote_sha256,"
                "  node_id, node_inventory_sha256, external_resource_class_id,"
                "  external_resource_class_key, status, state_version, fencing_epoch,"
                "  lease_token_sha256, adoption_count, last_runtime_inspection_sequence,"
                "  authorized_at, reserved_at, heartbeat_at, lease_expires_at,"
                "  hard_deadline, runtime_launch_authorization_count,"
                "  pre_runtime_absence_count, runtime_termination_challenge_count,"
                "  updated_at)"
                " VALUES (:attempt, :e, 1, :intent, CAST(:intent_json AS jsonb), :a,"
                "         :grant, :bundle, :quote, NULL, NULL, :class_id, :class_key,"
                "         'reserved', 1, 1, :token, 0, 0, :authorized, :reserved,"
                "         :reserved, :lease_expires, :hard, 0, 0, 0, :reserved)"
            ),
            {
                "attempt": docs["attempt"],
                "e": docs["execution"],
                "intent": docs["intent_sha"],
                # the terminal manifest guard binds replicate_slot_id against
                # exactly this embedded slot pointer
                "intent_json": _js(
                    {"infrastructure_attempt": {"replicate_slot_id": docs["slot"]}}
                ),
                "a": sha("admission"),
                "grant": sha("grant"),
                "bundle": docs["bundle_sha"],
                "quote": docs["quote_sha"],
                "class_id": docs["class_id"],
                "class_key": "bridge.acceptance-chain-honest",
                "token": docs["lease_token"],
                "authorized": _iso(_T0),
                "reserved": _iso(_T1),
                # the frozen bundle guard pins attempt.lease_expires_at to the
                # lease row's; the shorter authorization lease_expires_at lives
                # only inside the authorization JSON
                "lease_expires": _iso(docs["hard_deadline"]),
                "hard": _iso(docs["hard_deadline"]),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_resource_leases"
                " (lease_id, attempt_id, node_id, inventory_sha256,"
                "  external_resource_class_id, external_resource_class_key, lease_sha256,"
                "  lease_json, state, fencing_epoch, cpu_cores, memory_bytes,"
                "  scratch_bytes, exclusive, accelerator_count, acquired_at,"
                "  heartbeat_at, lease_expires_at)"
                " VALUES (:lease, :attempt, NULL, NULL, :class_id, :class_key, :lease_sha,"
                "         CAST(:lease_json AS jsonb), 'held', 1, 2, 8000000000,"
                "         4000000000, false, 0, :reserved, :reserved, :hard)"
            ),
            {
                "lease": f"lse_{suffix}",
                "attempt": docs["attempt"],
                "class_id": docs["class_id"],
                "class_key": "bridge.acceptance-chain-honest",
                "lease_sha": docs["resource_lease_sha"],
                "lease_json": _js(docs["lease_json"]),
                "reserved": _iso(_T1),
                "hard": _iso(docs["hard_deadline"]),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_budget_reservations"
                " (reservation_id, authorization_sha256, attempt_id, execution_id,"
                "  cost_quote_sha256, currency_code, fixed_charge_microunits,"
                "  charge_per_second_microunits, maximum_lease_seconds, held_microunits,"
                "  settled_microunits, state, reserved_at)"
                " VALUES (:reservation, :auth, :attempt, :e, :quote, 'USD', 100, 5,"
                "         :seconds, :held, 0, 'held', :reserved)"
            ),
            {
                "reservation": f"rvn_{suffix}",
                "auth": docs["budget_authorization"],
                "attempt": docs["attempt"],
                "e": docs["execution"],
                "quote": docs["quote_sha"],
                "seconds": _LEASE_SECONDS,
                "held": held,
                "reserved": _iso(_T1),
            },
        )
        session.execute(
            text(
                # event_id is a bigint identity column; the sequence mints it
                "INSERT INTO execution_budget_events"
                " (event_sha256, reservation_id, authorization_sha256,"
                "  sequence, previous_event_sha256, event_type, reserved_delta_microunits,"
                "  spent_delta_microunits, payload_sha256, payload_json, recorded_at)"
                " VALUES (:event_sha, :reservation, :auth, 1, NULL, 'reserved',"
                "         :held, 0, :payload_sha, CAST(:payload AS jsonb), :reserved)"
            ),
            {
                "event_sha": sha("budget-event"),
                "reservation": f"rvn_{suffix}",
                "auth": docs["budget_authorization"],
                "held": held,
                "payload_sha": sha("budget-event-payload"),
                "payload": json.dumps(
                    {
                        "reservation_id": f"rvn_{suffix}",
                        "authorization_sha256": docs["budget_authorization"],
                        "sequence": 1,
                        "previous_event_sha256": None,
                        "event_type": "reserved",
                        "reserved_delta_microunits": held,
                        "spent_delta_microunits": 0,
                        "recorded_at": _iso(_T1),
                    }
                ),
                "reserved": _iso(_T1),
            },
        )
        session.commit()


def _stage_prepared(engine, docs: dict[str, object]) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_runtime_preparations"
                " (preparation_sha256, attempt_id, execution_id, intent_sha256,"
                "  bridge_manifest_sha256, fencing_epoch, lease_token_sha256,"
                "  payload_sha256, payload_json, prepared_at, prepared_monotonic_ns,"
                "  recorded_at)"
                " VALUES (:prep, :attempt, :e, :intent, :manifest, 1, :token, :prep,"
                "         CAST(:payload AS jsonb), :prepared, 100000, :recorded)"
            ),
            {
                "prep": docs["preparation_sha"],
                "attempt": docs["attempt"],
                "e": docs["execution"],
                "intent": docs["intent_sha"],
                "manifest": docs["bridge_manifest"],
                "token": docs["lease_token"],
                "payload": _js(docs["preparation_payload"]),
                "prepared": _iso(docs["prepared_at"]),
                "recorded": _iso(docs["prepared_at"]),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET runtime_preparation_sha256 = :prep,"
                "  state_version = 2, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "prep": docs["preparation_sha"],
                "updated": _iso(docs["prepared_at"]),
                "attempt": docs["attempt"],
            },
        )
        session.commit()


def _stage_authorized(engine, docs: dict[str, object]) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_launch_authorizations"
                " (authorization_sha256, attempt_id, preparation_sha256, sequence,"
                "  request_sha256, request_payload_sha256, request_json,"
                "  authorization_payload_sha256, authorization_json,"
                "  runtime_control_pin_sha256, runtime_control_pin_json,"
                "  issued_at, expires_at, recorded_at)"
                " VALUES (:auth, :attempt, :prep, 1, :request, :request,"
                "         CAST(:request_json AS jsonb), :auth,"
                "         CAST(:authorization_json AS jsonb), :pin_sha,"
                "         CAST(:pin_json AS jsonb), :issued, :expires, :issued)"
            ),
            {
                "auth": docs["authorization_sha"],
                "attempt": docs["attempt"],
                "prep": docs["preparation_sha"],
                "request": docs["request_sha"],
                "request_json": _js(docs["request_json"]),
                "authorization_json": _js(docs["authorization_json"]),
                "pin_sha": _sha("control-pin"),
                "pin_json": _js(docs["control_pin"]),
                "issued": _iso(docs["issued_at"]),
                "expires": _iso(docs["expires_at"]),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET status = 'starting',"
                "  runtime_launch_authorization_count = 1,"
                "  latest_runtime_launch_authorization_sha256 = :auth,"
                "  state_version = 3, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "auth": docs["authorization_sha"],
                "updated": _iso(docs["issued_at"]),
                "attempt": docs["attempt"],
            },
        )
        session.commit()


def _stage_launched(engine, docs: dict[str, object]) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_runtime_launch_receipts"
                " (launch_receipt_sha256, attempt_id, preparation_sha256,"
                "  authorization_request_sha256, authorization_sha256,"
                "  executor_identity_sha256, launch_evidence_sha256,"
                "  launch_payload_sha256, launch_receipt_json, bridge_pin_sha256,"
                "  bridge_pin_json, signed_at, accepted_at)"
                " VALUES (:receipt, :attempt, :prep, :request, :auth, :identity,"
                "         :evidence, :receipt, CAST(:receipt_json AS jsonb),"
                "         :pin_sha, CAST(:pin_json AS jsonb), :signed, :accepted)"
            ),
            {
                "receipt": docs["launch_receipt_sha"],
                "attempt": docs["attempt"],
                "prep": docs["preparation_sha"],
                "request": docs["request_sha"],
                "auth": docs["authorization_sha"],
                "identity": docs["executor_identity_sha"],
                "evidence": _sha("launch-evidence"),
                "receipt_json": _js(docs["launch_receipt_json"]),
                "pin_sha": _sha("bridge-pin"),
                "pin_json": _js(docs["bridge_pin"]),
                "signed": _iso(docs["receipt_signed_at"]),
                "accepted": _iso(docs["receipt_accepted_at"]),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET status = 'running',"
                "  runtime_identity_sha256 = :identity,"
                "  runtime_identity_json = CAST(:identity_json AS jsonb),"
                "  state_version = 4, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "identity": docs["executor_identity_sha"],
                "identity_json": _js(docs["executor_identity"]),
                "updated": _iso(docs["receipt_accepted_at"]),
                "attempt": docs["attempt"],
            },
        )
        session.commit()


def _stage_challenged(engine, docs: dict[str, object]) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_termination_challenges"
                " (challenge_sha256, challenge_id, attempt_id, challenge_sequence,"
                "  preparation_sha256, launch_receipt_sha256, executor_identity_sha256,"
                "  termination_evidence_sha256, termination_evidence_json,"
                "  challenge_payload_sha256, challenge_json, runtime_control_pin_sha256,"
                "  runtime_control_pin_json, challenged_at, expires_at)"
                " VALUES (:challenge, :challenge_id, :attempt, 1, :prep, :receipt,"
                "         :identity, :evidence, CAST(:evidence_json AS jsonb),"
                "         :challenge, CAST(:challenge_json AS jsonb), :pin_sha,"
                "         CAST(:pin_json AS jsonb), :challenged, :expires)"
            ),
            {
                "challenge": docs["challenge_sha"],
                "challenge_id": docs["challenge_id"],
                "attempt": docs["attempt"],
                "prep": docs["preparation_sha"],
                "receipt": docs["launch_receipt_sha"],
                "identity": docs["executor_identity_sha"],
                "evidence": docs["termination_evidence_sha"],
                "evidence_json": _js(docs["termination_evidence"]),
                "challenge_json": _js(docs["challenge_json"]),
                "pin_sha": _sha("control-pin"),
                "pin_json": _js(docs["control_pin"]),
                "challenged": _iso(docs["challenged_at"]),
                "expires": _iso(docs["challenge_expires_at"]),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET runtime_termination_challenge_count = 1,"
                "  runtime_termination_challenge_sha256 = :challenge,"
                "  state_version = 5, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "challenge": docs["challenge_sha"],
                "updated": _iso(docs["challenged_at"]),
                "attempt": docs["attempt"],
            },
        )
        session.commit()


def _stage_terminated(engine, docs: dict[str, object]) -> None:
    """Accept the termination and release compute atomically (settle tail)."""
    settled_at = docs["termination_accepted_at"]
    settled_seconds = docs["settled_seconds"]
    charged = docs["charged"]
    held = 100 + 5 * _LEASE_SECONDS
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_runtime_termination_acceptances"
                " (accepted_termination_sha256, attempt_id, challenge_sha256,"
                "  bridge_termination_receipt_sha256, preparation_sha256,"
                "  launch_receipt_sha256, authorization_request_sha256,"
                "  authorization_sha256, executor_identity_sha256,"
                "  termination_evidence_sha256, result_content_sha256, exit_code,"
                "  runtime_ended_at, receipt_payload_sha256,"
                "  bridge_termination_receipt_json, acceptance_payload_sha256,"
                "  accepted_termination_json, conditional_terminal_expiration_sha256,"
                "  conditional_terminal_expiration_payload_sha256,"
                "  conditional_terminal_expiration_json,"
                "  conditional_terminal_expiration_authorized_at,"
                "  conditional_terminal_expiration_expires_at,"
                "  runtime_control_pin_sha256, runtime_control_pin_json, accepted_at)"
                " VALUES (:accepted, :attempt, :challenge, :receipt_sha, :prep,"
                "         :receipt, :request, :auth, :identity, :evidence, :result,"
                "         0, :ended, :receipt_sha,"
                "         CAST(:receipt_json AS jsonb), :accepted,"
                "         CAST(:accepted_json AS jsonb), :conditional, :conditional,"
                "         CAST(:conditional_json AS jsonb), :authorized, :expires,"
                "         :pin_sha, CAST(:pin_json AS jsonb), :accepted_at)"
            ),
            {
                "accepted": docs["accepted_termination_sha"],
                "attempt": docs["attempt"],
                "challenge": docs["challenge_sha"],
                "receipt_sha": docs["termination_receipt_sha"],
                "prep": docs["preparation_sha"],
                "receipt": docs["launch_receipt_sha"],
                "request": docs["request_sha"],
                "auth": docs["authorization_sha"],
                "identity": docs["executor_identity_sha"],
                "evidence": docs["termination_evidence_sha"],
                "result": docs["result_content"],
                "ended": _iso(docs["runtime_ended_at"]),
                "receipt_json": _js(docs["bridge_termination_receipt_json"]),
                "accepted_json": _js(docs["accepted_termination_json"]),
                "conditional": docs["conditional_expiration_sha"],
                "conditional_json": _js(docs["conditional_expiration_json"]),
                "authorized": _iso(docs["termination_accepted_at"]),
                "expires": _iso(docs["artifact_deadline"]),
                "pin_sha": _sha("control-pin"),
                "pin_json": _js(docs["control_pin"]),
                "accepted_at": _iso(docs["termination_accepted_at"]),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET status = 'verifying',"
                "  accepted_runtime_termination_sha256 = :accepted,"
                "  state_version = 6, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "accepted": docs["accepted_termination_sha"],
                "updated": _iso(settled_at),
                "attempt": docs["attempt"],
            },
        )
        session.execute(
            text(
                "UPDATE execution_resource_leases SET state = 'released',"
                "  released_at = :now WHERE attempt_id = :attempt"
            ),
            {"now": _iso(settled_at), "attempt": docs["attempt"]},
        )
        session.execute(
            text(
                "UPDATE execution_budget_reservations SET state = 'settled',"
                "  actual_lease_seconds = :seconds, settled_microunits = :charged,"
                "  settled_at = :now WHERE attempt_id = :attempt"
            ),
            {
                "seconds": settled_seconds,
                "charged": charged,
                "now": _iso(settled_at),
                "attempt": docs["attempt"],
            },
        )
        session.execute(
            text(
                "UPDATE execution_budget_heads SET reserved_microunits = 0,"
                "  spent_microunits = :charged, state_version = 2, updated_at = :now"
                " WHERE authorization_sha256 = :auth"
            ),
            {"charged": charged, "now": _iso(settled_at), "auth": docs["budget_authorization"]},
        )
        suffix = docs["suffix"]

        def sha(seed: str) -> str:
            return _sha(f"{suffix}:{seed}")

        session.execute(
            text(
                "INSERT INTO execution_budget_events"
                " (event_sha256, reservation_id, authorization_sha256, sequence,"
                "  previous_event_sha256, event_type, reserved_delta_microunits,"
                "  spent_delta_microunits, payload_sha256, payload_json, recorded_at)"
                " VALUES (:event_sha, :reservation, :auth, 2, :previous, 'settled',"
                "         :reserved_delta, :charged, :payload_sha,"
                "         CAST(:payload AS jsonb), :now)"
            ),
            {
                "event_sha": sha("budget-settle-event"),
                "reservation": f"rvn_{suffix}",
                "auth": docs["budget_authorization"],
                "previous": sha("budget-event"),
                "reserved_delta": -held,
                "charged": charged,
                "payload_sha": sha("budget-settle-payload"),
                "payload": json.dumps(
                    {
                        "reservation_id": f"rvn_{suffix}",
                        "authorization_sha256": docs["budget_authorization"],
                        "sequence": 2,
                        "previous_event_sha256": sha("budget-event"),
                        "event_type": "settled",
                        "reserved_delta_microunits": -held,
                        "spent_delta_microunits": charged,
                        # the frozen settlement arm mirrors the exact charge
                        # calculation out of these details
                        "details": {
                            "cost_quote_sha256": docs["quote_sha"],
                            "fixed_charge_microunits": 100,
                            "charge_per_second_microunits": 5,
                            "actual_lease_seconds": settled_seconds,
                            "charged_microunits": charged,
                        },
                        "recorded_at": _iso(settled_at),
                    }
                ),
                "now": _iso(settled_at),
            },
        )
        session.commit()


def _stage_terminal(engine, docs: dict[str, object]) -> None:
    """Accept the terminal submission and publish the exact outbox authority."""
    accepted_at = docs["terminal_accepted_at"]
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_qualification_terminal_acceptances"
                " (accepted_terminal_submission_sha256, attempt_id,"
                "  accepted_runtime_termination_sha256, bridge_manifest_sha256,"
                "  terminal_submission_sha256, artifact_manifest_sha256,"
                "  output_tree_sha256, disposition, submission_payload_sha256,"
                "  terminal_submission_json, manifest_payload_sha256,"
                "  artifact_manifest_json, artifact_verified_receipt_sha256s_json,"
                "  artifact_verified_receipts_json, acceptance_payload_sha256,"
                "  accepted_terminal_submission_json, runtime_control_pin_sha256,"
                "  runtime_control_pin_json, accepted_at)"
                " VALUES (:accepted, :attempt, :termination, :manifest_bridge,"
                "         :submission, :artifact_manifest, :output_tree,"
                "         'process_succeeded', :submission,"
                "         CAST(:submission_json AS jsonb), :artifact_manifest,"
                "         CAST(:manifest_json AS jsonb),"
                "         CAST(:receipt_shas AS jsonb),"
                "         CAST(:receipts AS jsonb), :accepted,"
                "         CAST(:accepted_json AS jsonb), :pin_sha,"
                "         CAST(:pin_json AS jsonb), :accepted_at)"
            ),
            {
                "accepted": docs["accepted_terminal_sha"],
                "attempt": docs["attempt"],
                "termination": docs["accepted_termination_sha"],
                "manifest_bridge": docs["bridge_manifest"],
                "submission": docs["terminal_submission_sha"],
                "artifact_manifest": docs["artifact_manifest_sha"],
                "output_tree": docs["output_tree_sha"],
                "submission_json": _js(docs["terminal_submission_json"]),
                "manifest_json": _js(docs["artifact_manifest"]),
                "receipt_shas": json.dumps(docs["artifact_receipt_sha256s"]),
                "receipts": json.dumps([docs["artifact_receipt"]]),
                "accepted_json": _js(docs["accepted_terminal_json"]),
                "pin_sha": _sha("control-pin"),
                "pin_json": _js(docs["control_pin"]),
                "accepted_at": _iso(accepted_at),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_terminal_outbox"
                " (outbox_id, terminal_authority_kind, terminal_authority_sha256,"
                "  accepted_terminal_submission_sha256,"
                "  terminal_deadline_expiration_sha256, execution_id, attempt_id,"
                "  topic, delivery_key, payload_sha256, payload_json, created_at)"
                " VALUES (:outbox_id, 'accepted_terminal_submission', :accepted,"
                "         :accepted, NULL, :e, :attempt,"
                "         'execution.qualification_terminal.v2',"
                "         :delivery_key, :accepted,"
                "         CAST(:payload AS jsonb), :created)"
            ),
            {
                "outbox_id": f"qto_{docs['accepted_terminal_sha']}",
                "accepted": docs["accepted_terminal_sha"],
                "e": docs["execution"],
                "attempt": docs["attempt"],
                "delivery_key": f"execution-v2:{docs['execution']}:{docs['attempt']}",
                "payload": _js(docs["accepted_terminal_json"]),
                "created": _iso(accepted_at),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET status = 'succeeded',"
                "  accepted_terminal_submission_sha256 = :accepted,"
                "  state_version = 7, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "accepted": docs["accepted_terminal_sha"],
                "updated": _iso(accepted_at),
                "attempt": docs["attempt"],
            },
        )
        session.execute(
            text(
                "UPDATE execution_heads SET active_attempt_id = NULL,"
                "  state_version = 2, updated_at = :updated WHERE execution_id = :e"
            ),
            {"updated": _iso(accepted_at), "e": docs["execution"]},
        )
        session.commit()


def _stage_expired(engine, docs: dict[str, object]) -> None:
    """Activate the pre-signed expiration after DB time passes the deadline."""
    activated_at = docs["artifact_deadline"] + timedelta(seconds=10)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_external_qualification_deadline_expirations"
                " (terminal_deadline_expiration_sha256, attempt_id,"
                "  accepted_runtime_termination_sha256, payload_sha256, payload_json,"
                "  runtime_control_pin_sha256, runtime_control_pin_json,"
                "  authorized_at, expired_at, activated_at)"
                " VALUES (:expiration, :attempt, :termination, :expiration,"
                "         CAST(:payload AS jsonb), :pin_sha, CAST(:pin_json AS jsonb),"
                "         :authorized, :expired, :activated)"
            ),
            {
                "expiration": docs["conditional_expiration_sha"],
                "attempt": docs["attempt"],
                "termination": docs["accepted_termination_sha"],
                "payload": _js(docs["conditional_expiration_json"]),
                "pin_sha": _sha("control-pin"),
                "pin_json": _js(docs["control_pin"]),
                "authorized": _iso(docs["termination_accepted_at"]),
                "expired": _iso(docs["artifact_deadline"]),
                "activated": _iso(activated_at),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_terminal_outbox"
                " (outbox_id, terminal_authority_kind, terminal_authority_sha256,"
                "  accepted_terminal_submission_sha256,"
                "  terminal_deadline_expiration_sha256, execution_id, attempt_id,"
                "  topic, delivery_key, payload_sha256, payload_json, created_at)"
                " VALUES (:outbox_id, 'terminal_deadline_expiration', :expiration,"
                "         NULL, :expiration, :e, :attempt,"
                "         'execution.qualification_terminal.v2',"
                "         :delivery_key, :expiration,"
                "         CAST(:payload AS jsonb), :created)"
            ),
            {
                "outbox_id": f"qto_{docs['conditional_expiration_sha']}",
                "expiration": docs["conditional_expiration_sha"],
                "e": docs["execution"],
                "attempt": docs["attempt"],
                "delivery_key": f"execution-v2:{docs['execution']}:{docs['attempt']}",
                "payload": _js(docs["conditional_expiration_json"]),
                "created": _iso(activated_at),
            },
        )
        session.execute(
            text(
                "UPDATE execution_attempts SET status = 'failed',"
                "  terminal_deadline_expiration_sha256 = :expiration,"
                "  state_version = 7, updated_at = :updated WHERE attempt_id = :attempt"
            ),
            {
                "expiration": docs["conditional_expiration_sha"],
                "updated": _iso(activated_at),
                "attempt": docs["attempt"],
            },
        )
        session.execute(
            text(
                "UPDATE execution_heads SET active_attempt_id = NULL,"
                "  state_version = 2, updated_at = :updated WHERE execution_id = :e"
            ),
            {"updated": _iso(activated_at), "e": docs["execution"]},
        )
        session.commit()


def _drive_to(engine, docs: dict[str, object], stage: str) -> None:
    _seed_placement(engine, docs)
    if stage in {"prepared", "authorized", "launched", "challenged", "terminated", "terminal"}:
        _stage_prepared(engine, docs)
    if stage in {"authorized", "launched", "challenged", "terminated", "terminal"}:
        _stage_authorized(engine, docs)
    if stage in {"launched", "challenged", "terminated", "terminal"}:
        _stage_launched(engine, docs)
    if stage in {"challenged", "terminated", "terminal"}:
        _stage_challenged(engine, docs)
    if stage in {"terminated", "terminal"}:
        _stage_terminated(engine, docs)
    if stage == "terminal":
        _stage_terminal(engine, docs)


def test_full_external_chain_commits_through_every_frozen_guard(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _drive_to(migrated_engine, docs, "terminal")
    with Session(migrated_engine) as session:
        row = session.execute(
            text(
                "SELECT a.status, a.state_version, l.state, r.state,"
                "       h.active_attempt_id,"
                "       (SELECT count(*) FROM execution_qualification_terminal_outbox o"
                "         WHERE o.attempt_id = a.attempt_id)"
                "  FROM execution_attempts a"
                "  JOIN execution_resource_leases l ON l.attempt_id = a.attempt_id"
                "  JOIN execution_budget_reservations r ON r.attempt_id = a.attempt_id"
                "  JOIN execution_heads h ON h.execution_id = a.execution_id"
                " WHERE a.attempt_id = :attempt"
            ),
            {"attempt": docs["attempt"]},
        ).one()
        assert row == ("succeeded", 7, "released", "settled", None, 1)
        outbox = session.execute(
            text(
                "SELECT outbox_id, terminal_authority_kind, terminal_authority_sha256,"
                "       payload_sha256, delivery_key"
                "  FROM execution_qualification_terminal_outbox"
                " WHERE attempt_id = :attempt"
            ),
            {"attempt": docs["attempt"]},
        ).one()
        assert outbox == (
            f"qto_{docs['accepted_terminal_sha']}",
            "accepted_terminal_submission",
            docs["accepted_terminal_sha"],
            docs["accepted_terminal_sha"],
            f"execution-v2:{docs['execution']}:{docs['attempt']}",
        )


def test_verifying_stage_holds_without_terminal_authority(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _drive_to(migrated_engine, docs, "terminated")
    with Session(migrated_engine) as session:
        row = session.execute(
            text(
                "SELECT a.status, a.terminal_receipt_sha256,"
                "       (SELECT count(*) FROM execution_qualification_terminal_outbox o"
                "         WHERE o.attempt_id = a.attempt_id),"
                "       (SELECT active_attempt_id FROM execution_heads h"
                "         WHERE h.execution_id = a.execution_id)"
                "  FROM execution_attempts a WHERE a.attempt_id = :attempt"
            ),
            {"attempt": docs["attempt"]},
        ).one()
        assert row == ("verifying", None, 0, docs["attempt"])


def test_deadline_expiration_path_commits_from_the_verifying_state(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _seed_placement(migrated_engine, docs)
    _stage_prepared(migrated_engine, docs)
    _stage_authorized(migrated_engine, docs)
    _stage_launched(migrated_engine, docs)
    _stage_challenged(migrated_engine, docs)
    _stage_terminated(migrated_engine, docs)
    _stage_expired(migrated_engine, docs)
    with Session(migrated_engine) as session:
        row = session.execute(
            text(
                "SELECT a.status,"
                "       (SELECT o.terminal_authority_kind"
                "         FROM execution_qualification_terminal_outbox o"
                "         WHERE o.attempt_id = a.attempt_id),"
                "       (SELECT count(*)"
                "         FROM execution_external_qualification_terminal_acceptances t"
                "         WHERE t.attempt_id = a.attempt_id)"
                "  FROM execution_attempts a WHERE a.attempt_id = :attempt"
            ),
            {"attempt": docs["attempt"]},
        ).one()
        assert row == ("failed", "terminal_deadline_expiration", 0)


def test_non_monotonic_authorization_head_is_rejected(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _seed_placement(migrated_engine, docs)
    _stage_prepared(migrated_engine, docs)
    with Session(migrated_engine) as session:
        with pytest.raises((IntegrityError, OperationalError), match="non-monotonic") as failure:
            session.execute(
                text(
                    "UPDATE execution_attempts SET status = 'starting',"
                    "  runtime_launch_authorization_count = 2,"
                    "  latest_runtime_launch_authorization_sha256 = :auth,"
                    "  state_version = 3, updated_at = :updated"
                    " WHERE attempt_id = :attempt"
                ),
                {
                    "auth": docs["authorization_sha"],
                    "updated": _iso(docs["issued_at"]),
                    "attempt": docs["attempt"],
                },
            )
        assert _sqlstate(failure.value) == "55000"
        session.rollback()
    with Session(migrated_engine) as session:
        count = session.execute(
            text(
                "SELECT runtime_launch_authorization_count FROM execution_attempts"
                " WHERE attempt_id = :attempt"
            ),
            {"attempt": docs["attempt"]},
        ).scalar_one()
        assert count == 0


def test_preparation_pointer_rebind_is_rejected(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _seed_placement(migrated_engine, docs)
    _stage_prepared(migrated_engine, docs)
    with Session(migrated_engine) as session:
        with pytest.raises((IntegrityError, OperationalError), match="preparation pointer is immutable") as failure:
            session.execute(
                text(
                    "UPDATE execution_attempts SET runtime_preparation_sha256 = :other,"
                    "  state_version = 3, updated_at = :updated WHERE attempt_id = :attempt"
                ),
                {
                    "other": _sha(f"{docs['suffix']}:rebound-preparation"),
                    "updated": _iso(docs["issued_at"]),
                    "attempt": docs["attempt"],
                },
            )
        assert _sqlstate(failure.value) == "55000"
        session.rollback()


def test_outbox_without_authority_row_is_rejected(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _seed_placement(migrated_engine, docs)
    ghost = _sha(f"{docs['suffix']}:ghost-authority")
    with Session(migrated_engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_qualification_terminal_outbox"
                " (outbox_id, terminal_authority_kind, terminal_authority_sha256,"
                "  accepted_terminal_submission_sha256,"
                "  terminal_deadline_expiration_sha256, execution_id, attempt_id,"
                "  topic, delivery_key, payload_sha256, payload_json, created_at)"
                " VALUES (:outbox_id, 'accepted_terminal_submission', :ghost, :ghost,"
                "         NULL, :e, :attempt, 'execution.qualification_terminal.v2',"
                "         :delivery_key, :ghost, CAST(:payload AS jsonb), :created)"
            ),
            {
                "outbox_id": f"qto_{ghost}",
                "ghost": ghost,
                "e": docs["execution"],
                "attempt": docs["attempt"],
                "delivery_key": f"execution-v2:{docs['execution']}:{docs['attempt']}",
                # the payload itself must still be the closed schema; only the
                # authority row it points at is missing
                "payload": _js(docs["accepted_terminal_json"]),
                "created": _iso(docs["terminal_accepted_at"]),
            },
        )
        # two deferred guards police the same invariant (the dedicated outbox
        # authority trigger and the completeness guard's outbox arm); either
        # may fire first
        with pytest.raises(
            IntegrityError,
            match="outbox external terminal authority|differs from exact authority",
        ) as failure:
            session.commit()
        assert _sqlstate(failure.value) == "23514"


def test_external_chain_rows_are_append_only(migrated_engine) -> None:
    docs = _chain_documents(_suffix())
    _seed_placement(migrated_engine, docs)
    _stage_prepared(migrated_engine, docs)
    with Session(migrated_engine) as session:
        with pytest.raises(
            (IntegrityError, OperationalError),
            match="execution_external_runtime_preparations is append-only"
        ) as failure:
            session.execute(
                text(
                    "UPDATE execution_external_runtime_preparations"
                    " SET intent_sha256 = :other WHERE attempt_id = :attempt"
                ),
                {
                    "other": _sha(f"{docs['suffix']}:rebound-intent"),
                    "attempt": docs["attempt"],
                },
            )
        assert _sqlstate(failure.value) == "55000"
        session.rollback()
