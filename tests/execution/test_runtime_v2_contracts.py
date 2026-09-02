from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.runtime_contracts import (
    NodeEnrollmentAuthorityPin,
    NodeEnrollmentAuthorityVerifier,
    NodeRuntimeIdentity,
    QualificationVerificationError,
    RuntimeInspectionState,
    WorkerNodeManifest,
    issue_worker_node_enrollment,
    qualification_key_id,
    verify_worker_node_enrollment,
)
from aletheia.execution.runtime_control_issuance import PinnedRuntimeControlIssuanceAuthority
from aletheia.execution.runtime_v2_contracts import (
    AcceptedRuntimeTermination,
    AttemptScopedPreRuntimeCleanupAuthorityPin,
    AttemptScopedPreRuntimeCleanupAuthorityVerifier,
    InputMaterializationEntry,
    InputMaterializationReceipt,
    QualificationTerminalSubmission,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    PinnedInputPath,
    RuntimeFenceRebindEvidence,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimeLaunchEvidence,
    RuntimePreparation,
    issue_accepted_runtime_termination,
    issue_attempt_scoped_pre_runtime_cleanup_receipt,
    issue_historical_runtime_recovery_grant,
    issue_node_runtime_launch_receipt,
    issue_node_runtime_termination_receipt,
    issue_pre_runtime_absence_receipt,
    issue_runtime_fence_rebind_receipt,
    issue_runtime_launch_authorization,
    issue_runtime_termination_acceptance_challenge,
    validate_pre_runtime_absence_evidence_refresh,
    validate_runtime_fence_rebind_evidence,
    validate_runtime_terminal_evidence_refresh,
    verify_accepted_runtime_termination,
    verify_historical_runtime_recovery_grant,
    verify_node_runtime_launch_receipt,
    verify_node_runtime_termination_receipt,
    verify_node_runtime_termination_receipt_historical,
    verify_pre_runtime_absence_receipt,
    verify_runtime_fence_rebind_receipt,
    verify_runtime_launch_authorization,
    _artifact_verified_receipt_sha256s,
)
from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    NetworkPolicy,
    canonical_sha256,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
NODE_PRIVATE_KEY = bytes(range(32))
ENROLLMENT_PRIVATE_KEY = bytes(range(1, 33))
RUNTIME_CONTROL_PRIVATE_KEY = bytes(range(2, 34))


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _public_key_hex(private_key: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _manifest() -> WorkerNodeManifest:
    public_key = _public_key_hex(NODE_PRIVATE_KEY)
    return WorkerNodeManifest(
        node_id="node:runtime-v2",
        site_id="site:runtime-v2",
        principal_id="principal:runtime-v2-node",
        agent_version="2.0.0",
        agent_implementation_sha256=_digest("runtime-v2-agent"),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="docker/29",
        sandbox_policy_sha256=_digest("runtime-v2-sandbox-policy"),
        resource_class_ids=("rsc_" + "1" * 32,),
        allowed_data_classifications=("public",),
        network_policies=(NetworkPolicy.NONE,),
        egress_policy_sha256=_digest("runtime-v2-egress"),
        node_signing_key_id=qualification_key_id(public_key),
        node_signing_public_key_ed25519_hex=public_key,
        key_valid_from=NOW - timedelta(hours=1),
        key_expires_at=NOW + timedelta(hours=2),
        frozen_at=NOW - timedelta(minutes=50),
    )


def _authority():
    manifest = _manifest()
    root_public = _public_key_hex(ENROLLMENT_PRIVATE_KEY)
    pin = NodeEnrollmentAuthorityPin(
        policy_sha256=_digest("runtime-v2-enrollment-policy"),
        principal_id="principal:runtime-v2-enrollment-root",
        key_id=qualification_key_id(root_public),
        public_key_ed25519_hex=root_public,
        valid_from=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=3),
    )
    enrollment = issue_worker_node_enrollment(
        manifest=manifest,
        pin=pin,
        private_key=ENROLLMENT_PRIVATE_KEY,
        issued_at=NOW - timedelta(minutes=45),
        expires_at=NOW + timedelta(hours=1),
    )
    return verify_worker_node_enrollment(
        manifest=manifest,
        enrollment=enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(pin),
        expected_manifest_sha256=manifest.manifest_sha256,
        observed_at=NOW,
    )


def _runtime_control_pin() -> RuntimeControlAuthorityPin:
    public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    return RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-v2-control-policy"),
        principal_id="principal:runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=3),
    )


def _launch_authority(
    preparation: RuntimePreparation,
    *,
    requested_at: datetime = NOW + timedelta(seconds=2),
    requested_monotonic_ns: int = 2_000,
    issued_at: datetime = NOW + timedelta(seconds=2),
    max_launch_delay_ns: int = 10_000,
) -> tuple[RuntimeLaunchAuthorizationRequest, RuntimeLaunchAuthorization]:
    request = RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("runtime-v2-launch-nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        requested_at=requested_at,
        requested_monotonic_ns=requested_monotonic_ns,
    )
    pin = _runtime_control_pin()
    authorization = issue_runtime_launch_authorization(
        pin=pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=_digest("runtime-v2-admission"),
        qualification_grant_sha256=_digest("runtime-v2-qualification-grant"),
        node_manifest_sha256=preparation.node_manifest_sha256,
        node_id=preparation.node_id,
        boot_id=preparation.boot_id,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        intent_sha256=preparation.intent_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        authorization_request_sha256=request.request_sha256,
        launch_spec_sha256=preparation.launch_spec_sha256,
        oci_config_sha256=preparation.oci_config_sha256,
        workload_executable_sha256=preparation.workload_executable_sha256,
        workload_argv=preparation.workload_argv,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        lease_expires_at=NOW + timedelta(minutes=1),
        hard_deadline=NOW + timedelta(minutes=5),
        issued_at=issued_at,
        expires_at=NOW + timedelta(seconds=20),
        max_launch_delay_ns=max_launch_delay_ns,
    )
    return request, authorization


def _materialization() -> InputMaterializationReceipt:
    entry = InputMaterializationEntry(
        input_port_id="port:input",
        verified_receipt_sha256=_digest("runtime-v2-avr"),
        content_sha256=_digest("runtime-v2-input-content"),
        content_bytes=17,
        relative_path="inputs/request.json",
        staged_file_identity_sha256=_digest("runtime-v2-staged-file"),
    )
    return InputMaterializationReceipt(
        intent_sha256=_digest("runtime-v2-intent"),
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        entries=(entry,),
        staged_root_identity_sha256=_digest("runtime-v2-staged-root"),
        materializer_principal_id="principal:input-materializer",
        materialized_at=NOW,
    )


def _preparation() -> RuntimePreparation:
    manifest = _manifest()
    materialization = _materialization()
    return RuntimePreparation(
        node_manifest_sha256=manifest.manifest_sha256,
        node_id=manifest.node_id,
        boot_id="boot:runtime-v2",
        execution_id=materialization.execution_id,
        infrastructure_attempt_id=materialization.infrastructure_attempt_id,
        intent_sha256=materialization.intent_sha256,
        runtime_id="runtime:qualification-v2",
        runtime_engine=manifest.container_runtime,
        launch_spec_sha256=_digest("runtime-v2-launch-spec"),
        workload_executable_sha256=_digest("runtime-v2-workload-executable"),
        workload_argv=("/opt/aletheia/bin/qualification", "--run"),
        runtime_request_sha256=_digest("runtime-v2-request"),
        enforced_placement_sha256=_digest("runtime-v2-placement"),
        input_materialization_receipt_sha256=(materialization.materialization_receipt_sha256),
        output_quota_provisioning_receipt_sha256=_digest("runtime-v2-output-quota"),
        fencing_epoch=4,
        lease_token_sha256=_digest("runtime-v2-token-old"),
        prepared_runtime_locator_sha256=_digest("runtime-v2-locator"),
        oci_config_sha256=_digest("runtime-v2-oci-config"),
        prepared_at=NOW + timedelta(seconds=1),
        prepared_monotonic_ns=1_000,
    )


def _identity(preparation: RuntimePreparation) -> NodeRuntimeIdentity:
    return NodeRuntimeIdentity(
        node_id=preparation.node_id,
        boot_id=preparation.boot_id,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        runtime_id=preparation.runtime_id,
        runtime_engine=preparation.runtime_engine,
        launch_spec_sha256=preparation.launch_spec_sha256,
        sandbox_instance_sha256=_digest("runtime-v2-container-id"),
        process_identity_sha256=_digest("runtime-v2-process-start"),
        started_at=NOW + timedelta(seconds=3),
        started_monotonic_ns=3_000,
    )


def _launch_evidence(
    preparation: RuntimePreparation, authorization: RuntimeLaunchAuthorization
) -> RuntimeLaunchEvidence:
    identity = _identity(preparation)
    return RuntimeLaunchEvidence(
        preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        runtime_identity=identity,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        engine_start_monotonic_lower_bound_ns=identity.started_monotonic_ns,
        engine_start_monotonic_upper_bound_exclusive_ns=(identity.started_monotonic_ns + 1),
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        engine_launch_journal_sha256=_digest("runtime-v2-launch-journal"),
        launch_evidence_sha256=_digest("runtime-v2-engine-launch-inspection"),
        observed_at=NOW + timedelta(seconds=4),
        observed_monotonic_ns=4_000,
    )


def test_input_materialization_and_pinned_paths_are_closed_and_canonical() -> None:
    receipt = _materialization()
    assert receipt.entries[0].relative_path == "inputs/request.json"
    assert len(receipt.materialization_receipt_sha256) == 64
    assert receipt.qualification_only is True
    assert receipt.scientific_admission_allowed is False

    with pytest.raises(ValidationError, match="canonical relative"):
        PinnedInputPath(input_port_id="port:input", relative_path="../escape")
    with pytest.raises(ValidationError, match="unique and canonical"):
        InputMaterializationReceipt.model_validate(
            {
                **receipt.model_dump(mode="python"),
                "entries": (receipt.entries[0], receipt.entries[0]),
            }
        )


def test_artifact_verification_receipts_follow_exact_manifest_key_order() -> None:
    entries = tuple(
        ArtifactManifestEntry(
            expected_artifact_id=f"art_{index:032x}",
            artifact_key=key,
            role=ArtifactRole.RAW_OUTPUT,
            content_sha256=_digest(f"artifact-content:{key}"),
            bytes=index,
            media_type="application/octet-stream",
            quarantine_ref=f"quarantine/{key}",
        )
        for index, key in enumerate(("artifact:a", "artifact:b"), start=1)
    )
    manifest = ArtifactManifest(
        intent_sha256=_digest("ordered-artifact-intent"),
        execution_id="exe_" + "2" * 32,
        replicate_slot_id="rps_" + "3" * 32,
        infrastructure_attempt_id="iat_" + "4" * 32,
        entries=entries,
        produced_at=NOW,
    )
    receipts = tuple(
        ArtifactVerifiedReceipt(
            artifact_manifest_sha256=manifest.manifest_sha256,
            producer_attempt_id=manifest.infrastructure_attempt_id,
            artifact=entry,
            custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
            verifier_principal_id="principal:artifact-verifier",
            object_store_id="store:qualification",
            final_object_ref=f"objects/{entry.content_sha256}",
            final_object_version="1",
            verified_at=NOW,
        )
        for entry in entries
    )

    assert _artifact_verified_receipt_sha256s(
        manifest=manifest,
        artifact_verified_receipts=receipts,
    ) == tuple(sorted(item.verified_receipt_sha256 for item in receipts))
    with pytest.raises(QualificationVerificationError, match="artifact-key order"):
        _artifact_verified_receipt_sha256s(
            manifest=manifest,
            artifact_verified_receipts=tuple(reversed(receipts)),
        )


def test_preparation_contains_no_started_or_process_identity_fields() -> None:
    preparation = _preparation()
    payload = preparation.model_dump(mode="json")
    assert "started_at" not in payload
    assert "process_identity_sha256" not in payload
    assert "runtime_identity" not in payload
    with pytest.raises(ValidationError, match="Extra inputs"):
        RuntimePreparation.model_validate({**payload, "started_at": NOW})


def test_actual_launch_receipt_is_signed_only_after_real_identity() -> None:
    authority = _authority()
    preparation = _preparation()
    authorization_request, authorization = _launch_authority(preparation)
    runtime_authority = RuntimeControlAuthorityVerifier(_runtime_control_pin())
    evidence = _launch_evidence(preparation, authorization)
    receipt = issue_node_runtime_launch_receipt(
        manifest=authority.manifest,
        preparation=preparation,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        launch_evidence=evidence,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=5),
        private_key=NODE_PRIVATE_KEY,
    )
    verified = verify_node_runtime_launch_receipt(
        receipt=receipt,
        preparation=preparation,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        authority=authority,
        runtime_authority=runtime_authority,
        observed_at=NOW + timedelta(seconds=6),
        maximum_age_seconds=10,
    )
    assert verified.runtime_identity_sha256 == evidence.runtime_identity_sha256
    assert verified.preparation_sha256 == preparation.preparation_sha256

    forged = receipt.model_copy(
        update={
            "launch_evidence": evidence.model_copy(
                update={"input_materialization_receipt_sha256": _digest("forged-input")}
            )
        }
    )
    with pytest.raises((ValidationError, ValueError)):
        verify_node_runtime_launch_receipt(
            receipt=forged,
            preparation=preparation,
            launch_authorization_request=authorization_request,
            launch_authorization=authorization,
            authority=authority,
            runtime_authority=runtime_authority,
            observed_at=NOW + timedelta(seconds=6),
            maximum_age_seconds=10,
        )


def test_proc_tick_interval_overlapping_request_is_conservatively_rejected() -> None:
    preparation = _preparation()
    request, authorization = _launch_authority(
        preparation,
        requested_monotonic_ns=2_500,
        max_launch_delay_ns=10_000,
    )
    tick_lower_identity = _identity(preparation).model_copy(update={"started_monotonic_ns": 2_000})
    ambiguous = _launch_evidence(preparation, authorization).model_copy(
        update={
            "runtime_identity": tick_lower_identity,
            "runtime_identity_sha256": tick_lower_identity.runtime_identity_sha256,
            "engine_start_monotonic_lower_bound_ns": 2_000,
            "engine_start_monotonic_upper_bound_exclusive_ns": 3_000,
        }
    )

    with pytest.raises(QualificationVerificationError, match="actual runtime start"):
        issue_node_runtime_launch_receipt(
            manifest=_authority().manifest,
            preparation=preparation,
            launch_authorization_request=request,
            launch_authorization=authorization,
            launch_evidence=ambiguous,
            runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            signed_at=NOW + timedelta(seconds=5),
            private_key=NODE_PRIVATE_KEY,
        )


def test_launch_ticket_orders_prepare_request_issue_and_monotonic_mutation_gate() -> None:
    preparation = _preparation()
    request, authorization = _launch_authority(preparation)
    verifier = RuntimeControlAuthorityVerifier(_runtime_control_pin())
    verify_runtime_launch_authorization(
        authorization=authorization,
        authorization_request=request,
        preparation=preparation,
        authority=verifier,
        observed_at=NOW + timedelta(seconds=3),
        observed_monotonic_ns=3_000,
    )
    with pytest.raises(QualificationVerificationError, match="exact preparation"):
        verify_runtime_launch_authorization(
            authorization=authorization.model_copy(
                update={"workload_argv": ("/opt/aletheia/bin/other", "--unsafe")}
            ),
            authorization_request=request,
            preparation=preparation,
            authority=verifier,
            observed_at=NOW + timedelta(seconds=3),
            observed_monotonic_ns=3_000,
        )
    with pytest.raises(QualificationVerificationError, match="exact preparation"):
        verify_runtime_launch_authorization(
            authorization=authorization,
            authorization_request=request,
            preparation=preparation,
            authority=verifier,
            observed_at=NOW + timedelta(seconds=3),
            observed_monotonic_ns=request.requested_monotonic_ns + 10_000,
        )

    request_after_issue, issued_too_early = _launch_authority(
        preparation,
        requested_at=NOW + timedelta(seconds=2),
        issued_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(QualificationVerificationError, match="exact preparation"):
        verify_runtime_launch_authorization(
            authorization=issued_too_early,
            authorization_request=request_after_issue,
            preparation=preparation,
            authority=verifier,
            observed_at=NOW + timedelta(seconds=3),
            observed_monotonic_ns=3_000,
        )

    _, issued_before_prepare = _launch_authority(
        preparation,
        requested_at=NOW,
        issued_at=NOW,
    )
    with pytest.raises(QualificationVerificationError, match="exact preparation"):
        verify_runtime_launch_authorization(
            authorization=issued_before_prepare,
            authorization_request=RuntimeLaunchAuthorizationRequest(
                request_nonce_sha256=_digest("runtime-v2-launch-nonce"),
                runtime_preparation_sha256=preparation.preparation_sha256,
                infrastructure_attempt_id=preparation.infrastructure_attempt_id,
                fencing_epoch=preparation.fencing_epoch,
                lease_token_sha256=preparation.lease_token_sha256,
                requested_at=NOW,
                requested_monotonic_ns=2_000,
            ),
            preparation=preparation,
            authority=verifier,
            observed_at=NOW + timedelta(seconds=1),
            observed_monotonic_ns=3_000,
        )


def test_inspection_absence_is_prelaunch_only_and_terminal_requires_journal() -> None:
    preparation = _preparation()
    base = dict(
        preparation_sha256=preparation.preparation_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("runtime-v2-inspection"),
        runtime_control_journal_sha256=_digest("runtime-v2-control-journal"),
        inspected_at=NOW + timedelta(seconds=5),
        inspected_monotonic_ns=5_000,
    )
    absent = RuntimeInspectionEvidence(
        **base,
        state=RuntimeInspectionState.ABSENT,
        prelaunch_absence_journal_sha256=_digest("runtime-v2-never-started-journal"),
        prelaunch_absence_epoch=1,
    )
    assert absent.runtime_identity is None
    absence_receipt = issue_pre_runtime_absence_receipt(
        manifest=_authority().manifest,
        preparation=preparation,
        absence_evidence=absent,
        signed_at=NOW + timedelta(seconds=6),
        expires_at=NOW + timedelta(seconds=10),
        private_key=NODE_PRIVATE_KEY,
    )
    verified_absence = verify_pre_runtime_absence_receipt(
        receipt=absence_receipt,
        preparation=preparation,
        authority=_authority(),
        observed_at=NOW + timedelta(seconds=7),
        maximum_age_seconds=5,
    )
    assert verified_absence.preparation_sha256 == preparation.preparation_sha256

    with pytest.raises(ValidationError, match="prelaunch absence"):
        RuntimeInspectionEvidence(**base, state=RuntimeInspectionState.ABSENT)
    with pytest.raises(ValidationError, match="engine terminal journal"):
        RuntimeInspectionEvidence(
            **base,
            state=RuntimeInspectionState.TERMINATED,
            runtime_identity=_identity(preparation),
            runtime_identity_sha256=_identity(preparation).runtime_identity_sha256,
            exit_code=0,
            ended_at=NOW + timedelta(seconds=4),
            ended_monotonic_ns=4_000,
        )

    unknown = RuntimeInspectionEvidence(
        **base,
        state=RuntimeInspectionState.UNKNOWN,
        runtime_identity=_identity(preparation),
        runtime_identity_sha256=_identity(preparation).runtime_identity_sha256,
    )
    assert unknown.state is RuntimeInspectionState.UNKNOWN


def test_cleaned_absence_advances_epoch_and_binds_prior_signed_ticket() -> None:
    preparation = _preparation()
    request, authorization = _launch_authority(preparation)
    runtime_authority = RuntimeControlAuthorityVerifier(_runtime_control_pin())
    evidence = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.ABSENT,
        preparation_sha256=preparation.preparation_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("cleaned-absence-inspection"),
        runtime_control_journal_sha256=_digest("cleaned-absence-control"),
        prelaunch_absence_journal_sha256=_digest("cleaned-absence-journal"),
        prelaunch_absence_epoch=1,
        prelaunch_authorization_request_sha256=request.request_sha256,
        prelaunch_authorization_sha256=authorization.authorization_sha256,
        inspected_at=NOW + timedelta(seconds=5),
        inspected_monotonic_ns=5_000,
    )
    receipt = issue_pre_runtime_absence_receipt(
        manifest=_authority().manifest,
        preparation=preparation,
        absence_evidence=evidence,
        signed_at=NOW + timedelta(seconds=6),
        expires_at=NOW + timedelta(seconds=10),
        private_key=NODE_PRIVATE_KEY,
        launch_authorization_request=request,
        launch_authorization=authorization,
        runtime_authority=runtime_authority,
    )
    assert (
        verify_pre_runtime_absence_receipt(
            receipt=receipt,
            preparation=preparation,
            authority=_authority(),
            observed_at=NOW + timedelta(seconds=7),
            maximum_age_seconds=5,
            launch_authorization_request=request,
            launch_authorization=authorization,
            runtime_authority=runtime_authority,
        ).absence_evidence_sha256
        == evidence.inspection_sha256
    )

    wrong_epoch = evidence.model_copy(update={"prelaunch_absence_epoch": 2})
    with pytest.raises(QualificationVerificationError, match="authorization epoch"):
        issue_pre_runtime_absence_receipt(
            manifest=_authority().manifest,
            preparation=preparation,
            absence_evidence=wrong_epoch,
            signed_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(seconds=10),
            private_key=NODE_PRIVATE_KEY,
            launch_authorization_request=request,
            launch_authorization=authorization,
            runtime_authority=runtime_authority,
        )


def test_attempt_scoped_cleanup_receipt_is_exact_short_lived_and_externally_pinned() -> None:
    preparation = _preparation()
    request, authorization = _launch_authority(preparation)
    runtime_authority = RuntimeControlAuthorityVerifier(_runtime_control_pin())
    recovery_private_key = hashlib.sha256(b"attempt-scoped-cleanup").digest()
    recovery_public_key = _public_key_hex(recovery_private_key)
    pin = AttemptScopedPreRuntimeCleanupAuthorityPin(
        policy_sha256=_digest("attempt-scoped-cleanup-policy"),
        principal_id="principal:attempt-scoped-cleanup",
        key_id=qualification_key_id(recovery_public_key),
        public_key_ed25519_hex=recovery_public_key,
        source_node_id=preparation.node_id,
        source_node_manifest_sha256=preparation.node_manifest_sha256,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        runtime_preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        cleanup_absence_epoch=1,
        watchdog_deployment_sha256=_digest("attempt-scoped-cleanup-watchdog"),
        valid_from=NOW + timedelta(seconds=4),
        expires_at=NOW + timedelta(minutes=10),
    )
    evidence = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.ABSENT,
        preparation_sha256=preparation.preparation_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("attempt-scoped-cleanup-inspection"),
        runtime_control_journal_sha256=_digest("attempt-scoped-cleanup-control"),
        prelaunch_absence_journal_sha256=_digest("attempt-scoped-cleanup-journal"),
        prelaunch_absence_epoch=1,
        prelaunch_authorization_request_sha256=request.request_sha256,
        prelaunch_authorization_sha256=authorization.authorization_sha256,
        inspected_at=NOW + timedelta(seconds=5),
        inspected_monotonic_ns=5_000,
    )
    receipt = issue_attempt_scoped_pre_runtime_cleanup_receipt(
        authority_pin=pin,
        preparation=preparation,
        absence_evidence=evidence,
        signed_at=NOW + timedelta(seconds=6),
        expires_at=NOW + timedelta(seconds=10),
        private_key=recovery_private_key,
        launch_authorization_request=request,
        launch_authorization=authorization,
        runtime_authority=runtime_authority,
    )

    verified = verify_pre_runtime_absence_receipt(
        receipt=receipt,
        preparation=preparation,
        authority=_authority(),
        observed_at=NOW + timedelta(seconds=7),
        maximum_age_seconds=5,
        launch_authorization_request=request,
        launch_authorization=authorization,
        runtime_authority=runtime_authority,
        cleanup_recovery_authority=(AttemptScopedPreRuntimeCleanupAuthorityVerifier(pin)),
    )

    assert verified.cleanup_recovery_authority_sha256 == pin.authority_sha256
    assert receipt.signing_key_id != _authority().manifest.node_signing_key_id
    with pytest.raises(QualificationVerificationError, match="deployment pin"):
        verify_pre_runtime_absence_receipt(
            receipt=receipt,
            preparation=preparation,
            authority=_authority(),
            observed_at=NOW + timedelta(seconds=7),
            maximum_age_seconds=5,
            launch_authorization_request=request,
            launch_authorization=authorization,
            runtime_authority=runtime_authority,
        )
    with pytest.raises(ValidationError, match="at most one hour"):
        AttemptScopedPreRuntimeCleanupAuthorityPin.model_validate(
            pin.model_copy(
                update={"expires_at": pin.valid_from + timedelta(hours=1, microseconds=1)}
            ).model_dump(mode="python")
        )


def test_fence_rebind_evidence_and_node_signature_bind_exact_cas() -> None:
    authority = _authority()
    preparation = _preparation()
    identity = _identity(preparation)
    request = RuntimeFenceRebindRequest(
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        previous_fencing_epoch=preparation.fencing_epoch,
        previous_lease_token_sha256=preparation.lease_token_sha256,
        new_fencing_epoch=preparation.fencing_epoch + 1,
        new_lease_token_sha256=_digest("runtime-v2-token-new"),
        rebind_sequence=1,
        expected_runtime_control_journal_sha256=_digest("runtime-v2-control-old"),
        requested_at=NOW + timedelta(seconds=7),
        requested_monotonic_ns=7_000,
    )
    evidence = RuntimeFenceRebindEvidence(
        request_sha256=request.request_sha256,
        preparation_sha256=request.preparation_sha256,
        runtime_identity_sha256=request.runtime_identity_sha256,
        previous_fencing_epoch=request.previous_fencing_epoch,
        previous_lease_token_sha256=request.previous_lease_token_sha256,
        new_fencing_epoch=request.new_fencing_epoch,
        new_lease_token_sha256=request.new_lease_token_sha256,
        rebind_sequence=request.rebind_sequence,
        previous_runtime_control_journal_sha256=(request.expected_runtime_control_journal_sha256),
        new_runtime_control_journal_sha256=_digest("runtime-v2-control-new"),
        rebind_evidence_sha256=_digest("runtime-v2-rebind-engine-evidence"),
        rebound_at=NOW + timedelta(seconds=8),
        rebound_monotonic_ns=8_000,
    )
    validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
    receipt = issue_runtime_fence_rebind_receipt(
        manifest=authority.manifest,
        request=request,
        evidence=evidence,
        signed_at=NOW + timedelta(seconds=9),
        private_key=NODE_PRIVATE_KEY,
    )
    verified = verify_runtime_fence_rebind_receipt(
        receipt=receipt,
        request=request,
        authority=authority,
        observed_at=NOW + timedelta(seconds=10),
        maximum_age_seconds=10,
    )
    assert verified.new_fencing_epoch == preparation.fencing_epoch + 1

    different = request.model_copy(
        update={"new_lease_token_sha256": _digest("different-new-token")}
    )
    with pytest.raises(ValueError, match="differs from its exact CAS request"):
        validate_runtime_fence_rebind_evidence(request=different, evidence=evidence)


def test_fresh_signature_cannot_refresh_old_launch_absence_or_rebind_fact() -> None:
    node_authority = _authority()
    preparation = _preparation()
    authorization_request, authorization = _launch_authority(preparation)
    runtime_authority = RuntimeControlAuthorityVerifier(_runtime_control_pin())
    launch_evidence = _launch_evidence(preparation, authorization)
    late_launch_receipt = issue_node_runtime_launch_receipt(
        manifest=node_authority.manifest,
        preparation=preparation,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        launch_evidence=launch_evidence,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=15),
        private_key=NODE_PRIVATE_KEY,
    )
    with pytest.raises(QualificationVerificationError, match="stale"):
        verify_node_runtime_launch_receipt(
            receipt=late_launch_receipt,
            preparation=preparation,
            launch_authorization_request=authorization_request,
            launch_authorization=authorization,
            authority=node_authority,
            runtime_authority=runtime_authority,
            observed_at=NOW + timedelta(seconds=16),
            maximum_age_seconds=5,
        )

    delayed_request, delayed_authorization = _launch_authority(
        preparation,
        max_launch_delay_ns=20_000,
    )
    delayed_observation = _launch_evidence(preparation, delayed_authorization).model_copy(
        update={
            "observed_at": NOW + timedelta(seconds=15),
            "observed_monotonic_ns": 15_000,
        }
    )
    delayed_receipt = issue_node_runtime_launch_receipt(
        manifest=node_authority.manifest,
        preparation=preparation,
        launch_authorization_request=delayed_request,
        launch_authorization=delayed_authorization,
        launch_evidence=delayed_observation,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=15),
        private_key=NODE_PRIVATE_KEY,
    )
    # A post-start/pre-journal crash may make the observation later than the ticket.  The fresh
    # DB observation remains acceptable because the actual engine start is still in-window.
    assert (
        verify_node_runtime_launch_receipt(
            receipt=delayed_receipt,
            preparation=preparation,
            launch_authorization_request=delayed_request,
            launch_authorization=delayed_authorization,
            authority=node_authority,
            runtime_authority=runtime_authority,
            observed_at=NOW + timedelta(seconds=16),
            maximum_age_seconds=5,
        ).runtime_identity_sha256
        == delayed_observation.runtime_identity_sha256
    )

    out_of_window_identity = _identity(preparation).model_copy(
        update={
            "started_at": NOW + timedelta(seconds=21),
            "started_monotonic_ns": 30_001,
        }
    )
    out_of_window_evidence = _launch_evidence(preparation, delayed_authorization).model_copy(
        update={
            "runtime_identity": out_of_window_identity,
            "runtime_identity_sha256": out_of_window_identity.runtime_identity_sha256,
            "engine_start_monotonic_lower_bound_ns": 30_001,
            "engine_start_monotonic_upper_bound_exclusive_ns": 30_002,
            "observed_at": NOW + timedelta(seconds=22),
            "observed_monotonic_ns": 31_000,
        }
    )
    with pytest.raises(QualificationVerificationError, match="actual runtime start"):
        issue_node_runtime_launch_receipt(
            manifest=node_authority.manifest,
            preparation=preparation,
            launch_authorization_request=delayed_request,
            launch_authorization=delayed_authorization,
            launch_evidence=out_of_window_evidence,
            runtime_authority=runtime_authority,
            signed_at=NOW + timedelta(seconds=22),
            private_key=NODE_PRIVATE_KEY,
        )

    absence = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.ABSENT,
        preparation_sha256=preparation.preparation_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("old-absence-inspection"),
        runtime_control_journal_sha256=_digest("old-absence-control"),
        prelaunch_absence_journal_sha256=_digest("old-absence-journal"),
        prelaunch_absence_epoch=1,
        inspected_at=NOW + timedelta(seconds=5),
        inspected_monotonic_ns=5_000,
    )
    late_absence = issue_pre_runtime_absence_receipt(
        manifest=node_authority.manifest,
        preparation=preparation,
        absence_evidence=absence,
        signed_at=NOW + timedelta(seconds=15),
        expires_at=NOW + timedelta(seconds=18),
        private_key=NODE_PRIVATE_KEY,
    )
    with pytest.raises(QualificationVerificationError, match="stale"):
        verify_pre_runtime_absence_receipt(
            receipt=late_absence,
            preparation=preparation,
            authority=node_authority,
            observed_at=NOW + timedelta(seconds=16),
            maximum_age_seconds=5,
        )

    rebind_request = RuntimeFenceRebindRequest(
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity_sha256=_identity(preparation).runtime_identity_sha256,
        previous_fencing_epoch=preparation.fencing_epoch,
        previous_lease_token_sha256=preparation.lease_token_sha256,
        new_fencing_epoch=preparation.fencing_epoch + 1,
        new_lease_token_sha256=_digest("late-rebind-token"),
        rebind_sequence=1,
        expected_runtime_control_journal_sha256=_digest("late-rebind-old-control"),
        requested_at=NOW + timedelta(seconds=7),
        requested_monotonic_ns=7_000,
    )
    rebind_evidence = RuntimeFenceRebindEvidence(
        request_sha256=rebind_request.request_sha256,
        preparation_sha256=rebind_request.preparation_sha256,
        runtime_identity_sha256=rebind_request.runtime_identity_sha256,
        previous_fencing_epoch=rebind_request.previous_fencing_epoch,
        previous_lease_token_sha256=rebind_request.previous_lease_token_sha256,
        new_fencing_epoch=rebind_request.new_fencing_epoch,
        new_lease_token_sha256=rebind_request.new_lease_token_sha256,
        rebind_sequence=1,
        previous_runtime_control_journal_sha256=(
            rebind_request.expected_runtime_control_journal_sha256
        ),
        new_runtime_control_journal_sha256=_digest("late-rebind-new-control"),
        rebind_evidence_sha256=_digest("late-rebind-evidence"),
        rebound_at=NOW + timedelta(seconds=8),
        rebound_monotonic_ns=8_000,
    )
    late_rebind = issue_runtime_fence_rebind_receipt(
        manifest=node_authority.manifest,
        request=rebind_request,
        evidence=rebind_evidence,
        signed_at=NOW + timedelta(seconds=15),
        private_key=NODE_PRIVATE_KEY,
    )
    with pytest.raises(QualificationVerificationError, match="stale"):
        verify_runtime_fence_rebind_receipt(
            receipt=late_rebind,
            request=rebind_request,
            authority=node_authority,
            observed_at=NOW + timedelta(seconds=16),
            maximum_age_seconds=5,
        )


def test_historical_recovery_is_explicitly_nonlaunching() -> None:
    preparation = _preparation()
    _, authorization = _launch_authority(preparation)
    launch = _launch_evidence(preparation, authorization)
    pin = _runtime_control_pin()
    grant = issue_historical_runtime_recovery_grant(
        pin=pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=_digest("runtime-v2-admission"),
        qualification_grant_sha256=_digest("runtime-v2-qualification-grant"),
        intent_sha256=preparation.intent_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        runtime_preparation_sha256=preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=launch.evidence_sha256,
        admitted_at=NOW,
        hard_deadline=NOW + timedelta(minutes=5),
        issued_at=NOW + timedelta(minutes=6),
        recovery_expires_at=NOW + timedelta(minutes=10),
    )
    verify_historical_runtime_recovery_grant(
        grant=grant,
        authority=RuntimeControlAuthorityVerifier(pin),
        observed_at=NOW + timedelta(minutes=7),
    )
    assert grant.recovery_only is True
    assert grant.launch_allowed is False
    with pytest.raises(ValidationError):
        type(grant).model_validate({**grant.model_dump(mode="python"), "launch_allowed": True})


def test_fresh_termination_acceptance_precedes_artifact_and_node_receipt_work() -> None:
    preparation = _preparation()
    node_authority = _authority()
    control_pin = _runtime_control_pin()
    runtime_authority = RuntimeControlAuthorityVerifier(control_pin)
    authorization_request, authorization = _launch_authority(preparation)
    launch_evidence = _launch_evidence(preparation, authorization)
    launch_receipt = issue_node_runtime_launch_receipt(
        manifest=node_authority.manifest,
        preparation=preparation,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        launch_evidence=launch_evidence,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=5),
        private_key=NODE_PRIVATE_KEY,
    )
    identity = launch_evidence.runtime_identity
    terminal_evidence = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.TERMINATED,
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity=identity,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("runtime-v2-terminal-inspection"),
        runtime_control_journal_sha256=_digest("runtime-v2-control-journal"),
        engine_terminal_journal_sha256=_digest("runtime-v2-terminal-journal"),
        inspected_at=NOW + timedelta(seconds=8),
        inspected_monotonic_ns=8_000,
        exit_code=0,
        ended_at=NOW + timedelta(seconds=7),
        ended_monotonic_ns=7_000,
    )
    challenge = issue_runtime_termination_acceptance_challenge(
        pin=control_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        attempt_id=preparation.infrastructure_attempt_id,
        execution_id=preparation.execution_id,
        intent_sha256=preparation.intent_sha256,
        node_manifest_sha256=preparation.node_manifest_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        runtime_inspection_evidence_sha256=terminal_evidence.inspection_sha256,
        inspection_sequence=1,
        node_inventory_sha256=_digest("runtime-v2-inventory"),
        resource_lease_sha256=_digest("runtime-v2-resource-lease"),
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        hard_deadline=NOW + timedelta(minutes=5),
        artifact_submission_deadline=NOW + timedelta(hours=1),
        challenged_at=NOW + timedelta(seconds=9),
        expires_at=NOW + timedelta(seconds=30),
    )
    concrete_challenge = PinnedRuntimeControlIssuanceAuthority(
        pin=control_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
    ).issue_termination_challenge(
        preparation=preparation,
        launch_receipt=launch_receipt,
        termination_evidence=terminal_evidence,
        inspection_sequence=1,
        node_inventory_sha256=_digest("runtime-v2-inventory"),
        resource_lease_sha256=_digest("runtime-v2-resource-lease"),
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        hard_deadline=NOW + timedelta(minutes=5),
        artifact_submission_deadline=NOW + timedelta(hours=1),
        challenged_at=NOW + timedelta(seconds=9),
        expires_at=NOW + timedelta(seconds=30),
    )
    assert concrete_challenge == challenge
    assert challenge.challenge_id == canonical_sha256(
        challenge.model_dump(mode="json", exclude={"challenge_id", "signature_ed25519_hex"})
    )
    node_termination = issue_node_runtime_termination_receipt(
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        termination_evidence=terminal_evidence,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=10),
        expires_at=NOW + timedelta(seconds=25),
        private_key=NODE_PRIVATE_KEY,
    )
    verified = verify_node_runtime_termination_receipt(
        receipt=node_termination,
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        observed_at=NOW + timedelta(seconds=11),
        maximum_age_seconds=10,
    )
    assert verified.termination_evidence_sha256 == terminal_evidence.inspection_sha256
    verify_node_runtime_termination_receipt_historical(
        receipt=node_termination,
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    with pytest.raises(QualificationVerificationError):
        verify_node_runtime_termination_receipt_historical(
            receipt=node_termination.model_copy(update={"signature_ed25519_hex": "0" * 128}),
            challenge=challenge,
            preparation=preparation,
            launch_receipt=launch_receipt,
            launch_authorization_request=authorization_request,
            launch_authorization=authorization,
            node_authority=node_authority,
            runtime_authority=runtime_authority,
        )
    late_challenge = issue_runtime_termination_acceptance_challenge(
        pin=control_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        attempt_id=challenge.attempt_id,
        execution_id=challenge.execution_id,
        intent_sha256=challenge.intent_sha256,
        node_manifest_sha256=challenge.node_manifest_sha256,
        runtime_preparation_sha256=challenge.runtime_preparation_sha256,
        node_runtime_launch_receipt_sha256=(challenge.node_runtime_launch_receipt_sha256),
        runtime_identity_sha256=challenge.runtime_identity_sha256,
        runtime_inspection_evidence_sha256=(challenge.runtime_inspection_evidence_sha256),
        inspection_sequence=2,
        node_inventory_sha256=challenge.node_inventory_sha256,
        resource_lease_sha256=challenge.resource_lease_sha256,
        fencing_epoch=challenge.fencing_epoch,
        lease_token_sha256=challenge.lease_token_sha256,
        hard_deadline=challenge.hard_deadline,
        artifact_submission_deadline=challenge.artifact_submission_deadline,
        challenged_at=NOW + timedelta(seconds=14),
        expires_at=NOW + timedelta(seconds=20),
    )
    late_node_termination = issue_node_runtime_termination_receipt(
        challenge=late_challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        termination_evidence=terminal_evidence,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        signed_at=NOW + timedelta(seconds=15),
        expires_at=NOW + timedelta(seconds=19),
        private_key=NODE_PRIVATE_KEY,
    )
    with pytest.raises(QualificationVerificationError, match="stale"):
        verify_node_runtime_termination_receipt(
            receipt=late_node_termination,
            challenge=late_challenge,
            preparation=preparation,
            launch_receipt=launch_receipt,
            launch_authorization_request=authorization_request,
            launch_authorization=authorization,
            node_authority=node_authority,
            runtime_authority=runtime_authority,
            observed_at=NOW + timedelta(seconds=16),
            maximum_age_seconds=5,
        )
    accepted = issue_accepted_runtime_termination(
        pin=control_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        challenge=challenge,
        node_termination_receipt=node_termination,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        accepted_at=NOW + timedelta(seconds=12),
        billable_ended_at=NOW + timedelta(seconds=12),
        maximum_proof_age_seconds=10,
    )
    # Historical verification remains valid after the short node/challenge window and therefore
    # cannot be stranded by a long artifact quarantine.
    verify_accepted_runtime_termination(
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    with pytest.raises(QualificationVerificationError):
        verify_accepted_runtime_termination(
            accepted=accepted,
            challenge=challenge,
            node_termination_receipt=node_termination,
            preparation=preparation,
            launch_receipt=launch_receipt,
            launch_authorization_request=authorization_request,
            launch_authorization=authorization.model_copy(
                update={"admission_sha256": _digest("forged-launch-admission")}
            ),
            node_authority=node_authority,
            runtime_authority=runtime_authority,
        )
    assert accepted.compute_release_allowed is True
    assert accepted.scientific_admission_allowed is False
    assert "node_execution_receipt_sha256" not in AcceptedRuntimeTermination.model_fields
    assert "artifact_manifest_sha256" not in AcceptedRuntimeTermination.model_fields
    assert "node_execution_receipt_sha256" not in QualificationTerminalSubmission.model_fields
    assert "accepted_runtime_termination_sha256" in (QualificationTerminalSubmission.model_fields)
    assert "artifact_verified_receipt_sha256s" in (QualificationTerminalSubmission.model_fields)

    with pytest.raises(ValidationError, match="not fresh"):
        AcceptedRuntimeTermination.model_validate(
            {
                **accepted.model_dump(mode="python"),
                "accepted_at": accepted.proof_expires_at,
            }
        )


def test_inspection_refresh_advances_only_observation_fields() -> None:
    preparation = _preparation()
    identity = _identity(preparation)
    terminal = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.TERMINATED,
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity=identity,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("terminal-inspection-generation-1"),
        runtime_control_journal_sha256=_digest("terminal-control-journal"),
        engine_terminal_journal_sha256=_digest("terminal-engine-journal"),
        inspected_at=NOW + timedelta(seconds=8),
        inspected_monotonic_ns=8_000,
        exit_code=0,
        ended_at=NOW + timedelta(seconds=7),
        ended_monotonic_ns=7_000,
    )
    refreshed_terminal = terminal.model_copy(
        update={
            "inspection_evidence_sha256": _digest("terminal-inspection-generation-2"),
            "inspected_at": NOW + timedelta(seconds=20),
            "inspected_monotonic_ns": 20_000,
        }
    )
    validate_runtime_terminal_evidence_refresh(
        previous=terminal,
        refreshed=refreshed_terminal,
    )
    with pytest.raises(QualificationVerificationError, match="immutable engine facts"):
        validate_runtime_terminal_evidence_refresh(
            previous=terminal,
            refreshed=refreshed_terminal.model_copy(update={"exit_code": 1}),
        )

    absence = RuntimeInspectionEvidence(
        state=RuntimeInspectionState.ABSENT,
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity=None,
        runtime_identity_sha256=None,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        inspection_evidence_sha256=_digest("absence-inspection-generation-1"),
        runtime_control_journal_sha256=_digest("absence-control-journal"),
        prelaunch_absence_journal_sha256=_digest("absence-tombstone"),
        prelaunch_absence_epoch=1,
        inspected_at=NOW + timedelta(seconds=8),
        inspected_monotonic_ns=8_000,
    )
    refreshed_absence = absence.model_copy(
        update={
            "inspection_evidence_sha256": _digest("absence-inspection-generation-2"),
            "inspected_at": NOW + timedelta(seconds=20),
            "inspected_monotonic_ns": 20_000,
        }
    )
    validate_pre_runtime_absence_evidence_refresh(
        previous=absence,
        refreshed=refreshed_absence,
    )
    with pytest.raises(QualificationVerificationError, match="immutable engine facts"):
        validate_pre_runtime_absence_evidence_refresh(
            previous=absence,
            refreshed=refreshed_absence.model_copy(
                update={"prelaunch_absence_journal_sha256": _digest("other-tombstone")}
            ),
        )
