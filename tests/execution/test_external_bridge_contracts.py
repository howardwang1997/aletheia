from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.external_bridge_contracts import (
    AcceptedExternalQualificationTerminalSubmission,
    AcceptedExternalRuntimeTermination,
    ExternalExecutorIdentity,
    ExternalLaunchAuthorization,
    ExternalLaunchEvidence,
    ExternalQualificationTerminalDeadlineExpiration,
    ExternalQualificationTerminalSubmission,
    ExternalRuntimeLaunchReceipt,
    ExternalRuntimePreparation,
    ExternalRuntimeTerminationReceipt,
    ExternalTerminationAcceptanceChallenge,
    ExternalTerminationEvidence,
    issue_accepted_external_qualification_terminal_submission,
    issue_accepted_external_runtime_termination,
    issue_external_launch_authorization,
    issue_external_qualification_terminal_deadline_expiration,
    issue_external_qualification_terminal_submission,
    issue_external_runtime_launch_receipt,
    issue_external_runtime_termination_receipt,
    issue_external_termination_acceptance_challenge,
    recompute_external_disposition,
    verify_accepted_external_qualification_terminal_submission,
    verify_accepted_external_runtime_termination,
    verify_external_launch_authorization,
    verify_external_qualification_terminal_deadline_expiration,
    verify_external_qualification_terminal_submission,
    verify_external_runtime_launch_receipt,
    verify_external_runtime_termination_receipt,
    verify_external_termination_acceptance_challenge,
)
from aletheia.execution.runtime_contracts import (
    ExternalBridgeAuthority,
    NetworkPolicy,
    QualificationAuthorityPin,
    QualificationVerificationError,
    WorkerNodeManifest,
    qualification_key_id,
)
from aletheia.execution.runtime_v2_contracts import (
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeLaunchAuthorizationRequest,
)
from aletheia.execution.schemas import canonical_sha256

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _public_key_hex(private_key: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw().hex()


RUNTIME_CONTROL_PRIVATE_KEY = bytes(range(1, 33))
BRIDGE_PRIVATE_KEY = bytes(range(33, 65))
OTHER_BRIDGE_PRIVATE_KEY = bytes(range(65, 97))


def _runtime_control_pin() -> RuntimeControlAuthorityPin:
    public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    return RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-control-policy:v1"),
        principal_id="principal:runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=4),
    )


def _bridge_pin(private_key: bytes = BRIDGE_PRIVATE_KEY) -> QualificationAuthorityPin:
    public_key = _public_key_hex(private_key)
    return QualificationAuthorityPin(
        policy_sha256=_digest("bridge-authority-policy:v1"),
        principal_id="principal:external-bridge",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=4),
    )


def _bridge_authority(
    *, private_key: bytes = BRIDGE_PRIVATE_KEY
) -> tuple[ExternalBridgeAuthority, str]:
    public_key = _public_key_hex(OTHER_BRIDGE_PRIVATE_KEY)
    manifest = WorkerNodeManifest(
        node_id="node.bridge-external-01",
        site_id="site.lab-a",
        principal_id="principal:bridge-external-01",
        agent_version="1.0.0",
        agent_implementation_sha256=_digest("bridge-agent:v1"),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="host-process",
        sandbox_policy_sha256=_digest("bridge-sandbox-policy:v1"),
        resource_class_ids=("rsc_" + _digest("bridge-class")[:32],),
        allowed_data_classifications=("internal",),
        network_policies=(NetworkPolicy.NONE,),
        egress_policy_sha256=_digest("bridge-egress:none"),
        node_signing_key_id=qualification_key_id(public_key),
        node_signing_public_key_ed25519_hex=public_key,
        key_valid_from=NOW - timedelta(days=1),
        key_expires_at=NOW + timedelta(days=1),
        frozen_at=NOW - timedelta(hours=1),
    )
    return (
        ExternalBridgeAuthority(manifest=manifest, bridge_authority_pin=_bridge_pin(private_key)),
        manifest.manifest_sha256,
    )


EXECUTION_ID = "exe_" + "1" * 32
ATTEMPT_ID = "iat_" + "2" * 32


def _preparation(bridge_manifest_sha256: str) -> ExternalRuntimePreparation:
    return ExternalRuntimePreparation(
        bridge_manifest_sha256=bridge_manifest_sha256,
        execution_id=EXECUTION_ID,
        infrastructure_attempt_id=ATTEMPT_ID,
        intent_sha256=_digest("intent"),
        runtime_id="runtime.external-dispatch-01",
        runtime_engine="in-process-python",
        launch_spec_sha256=_digest("launch-spec"),
        workload_executable_sha256=_digest("workload"),
        workload_argv=(
            "/root/miniconda3/envs/arl2-cuprate/bin/python",
            "-m",
            "aletheia.execution.cuprate.service",
        ),
        runtime_request_sha256=_digest("runtime-request"),
        enforced_placement_sha256=_digest("placement"),
        input_materialization_receipt_sha256=_digest("input-materialization"),
        fencing_epoch=1,
        lease_token_sha256=_digest("lease-token"),
        prepared_dispatch_locator_sha256=_digest("dispatch-locator"),
        prepared_at=NOW - timedelta(seconds=30),
        prepared_monotonic_ns=10_000,
    )


def _authorization_request(preparation: ExternalRuntimePreparation):
    return RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("request-nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        requested_at=preparation.prepared_at + timedelta(seconds=1),
        requested_monotonic_ns=10_500,
    )


def _authorization(
    preparation: ExternalRuntimePreparation,
    request: RuntimeLaunchAuthorizationRequest,
    pin: RuntimeControlAuthorityPin,
) -> ExternalLaunchAuthorization:
    return issue_external_launch_authorization(
        pin=pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=_digest("admission"),
        qualification_grant_sha256=_digest("grant"),
        bridge_manifest_sha256=preparation.bridge_manifest_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        intent_sha256=preparation.intent_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        authorization_request_sha256=canonical_sha256(request),
        launch_spec_sha256=preparation.launch_spec_sha256,
        workload_executable_sha256=preparation.workload_executable_sha256,
        workload_argv=preparation.workload_argv,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=preparation.input_materialization_receipt_sha256,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        lease_expires_at=NOW + timedelta(seconds=15),
        hard_deadline=NOW + timedelta(hours=1),
        issued_at=NOW - timedelta(seconds=25),
        expires_at=NOW + timedelta(seconds=35),
        max_launch_delay_ns=60_000_000_000,
    )


def _executor_identity() -> ExternalExecutorIdentity:
    return ExternalExecutorIdentity(
        execution_id=EXECUTION_ID,
        infrastructure_attempt_id=ATTEMPT_ID,
        runtime_id="runtime.external-dispatch-01",
        executor_ref="aletheia.execution.cuprate.service:CuprateDiagnosticService.run_cuprate_diagnostic",
        executor_implementation_sha256=_digest("cuprate-service"),
        invocation_payload_sha256=_digest("invocation-payload"),
        started_at=NOW - timedelta(seconds=20),
        started_monotonic_ns=12_000,
    )


def _launch_evidence(
    preparation: ExternalRuntimePreparation,
    authorization: ExternalLaunchAuthorization,
    identity: ExternalExecutorIdentity,
) -> ExternalLaunchEvidence:
    return ExternalLaunchEvidence(
        preparation_sha256=preparation.preparation_sha256,
        external_launch_authorization_sha256=authorization.authorization_sha256,
        executor_identity=identity,
        executor_identity_sha256=identity.executor_identity_sha256,
        executor_start_monotonic_lower_bound_ns=11_800,
        executor_start_monotonic_upper_bound_exclusive_ns=12_500,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=preparation.input_materialization_receipt_sha256,
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        launch_evidence_journal_sha256=_digest("launch-journal"),
        observed_at=NOW - timedelta(seconds=19),
        observed_monotonic_ns=13_000,
    )


def _launch_receipt(
    authority_manifest_sha256: str,
    evidence: ExternalLaunchEvidence,
    *,
    private_key: bytes = BRIDGE_PRIVATE_KEY,
) -> ExternalRuntimeLaunchReceipt:
    return issue_external_runtime_launch_receipt(
        bridge_pin=_bridge_pin(private_key),
        private_key=private_key,
        bridge_manifest_sha256=authority_manifest_sha256,
        launch_evidence=evidence,
        launch_evidence_sha256=evidence.launch_evidence_sha256,
        signed_at=NOW - timedelta(seconds=18),
    )


def _termination_evidence(
    preparation: ExternalRuntimePreparation,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    identity: ExternalExecutorIdentity,
    *,
    result_sha: str | None = None,
) -> ExternalTerminationEvidence:
    return ExternalTerminationEvidence(
        preparation_sha256=preparation.preparation_sha256,
        external_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        executor_identity_sha256=identity.executor_identity_sha256,
        exit_code=0,
        ended_at=NOW - timedelta(seconds=5),
        ended_monotonic_ns=90_000,
        result_content_sha256=result_sha or _digest("cuprate-result"),
        termination_journal_sha256=_digest("termination-journal"),
    )


def _challenge(
    preparation: ExternalRuntimePreparation,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    evidence: ExternalTerminationEvidence,
    *,
    resource_lease_sha256: str | None = None,
) -> ExternalTerminationAcceptanceChallenge:
    return issue_external_termination_acceptance_challenge(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        challenge_id=_digest("challenge-id"),
        attempt_id=preparation.infrastructure_attempt_id,
        execution_id=preparation.execution_id,
        intent_sha256=preparation.intent_sha256,
        bridge_manifest_sha256=preparation.bridge_manifest_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        executor_identity_sha256=evidence.executor_identity_sha256,
        termination_evidence_sha256=evidence.termination_evidence_sha256,
        result_content_sha256=evidence.result_content_sha256,
        resource_lease_sha256=resource_lease_sha256 or _digest("resource-lease"),
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        hard_deadline=NOW + timedelta(hours=1),
        artifact_submission_deadline=NOW + timedelta(hours=2),
        challenged_at=NOW - timedelta(seconds=4),
        expires_at=NOW + timedelta(seconds=56),
    )


def _termination_receipt(
    preparation: ExternalRuntimePreparation,
    authorization: ExternalLaunchAuthorization,
    request: RuntimeLaunchAuthorizationRequest,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    challenge: ExternalTerminationAcceptanceChallenge,
    evidence: ExternalTerminationEvidence,
    *,
    private_key: bytes = BRIDGE_PRIVATE_KEY,
) -> ExternalRuntimeTerminationReceipt:
    return issue_external_runtime_termination_receipt(
        bridge_pin=_bridge_pin(private_key),
        private_key=private_key,
        bridge_manifest_sha256=preparation.bridge_manifest_sha256,
        challenge_sha256=challenge.challenge_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_launch_authorization_request_sha256=canonical_sha256(request),
        external_launch_authorization_sha256=authorization.authorization_sha256,
        termination_evidence=evidence,
        termination_evidence_sha256=evidence.termination_evidence_sha256,
        signed_at=NOW - timedelta(seconds=3),
        expires_at=NOW + timedelta(seconds=57),
    )


def _accepted_termination(
    preparation: ExternalRuntimePreparation,
    authorization: ExternalLaunchAuthorization,
    request: RuntimeLaunchAuthorizationRequest,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    challenge: ExternalTerminationAcceptanceChallenge,
    termination_receipt: ExternalRuntimeTerminationReceipt,
    evidence: ExternalTerminationEvidence,
) -> AcceptedExternalRuntimeTermination:
    return issue_accepted_external_runtime_termination(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        challenge_sha256=challenge.challenge_sha256,
        attempt_id=preparation.infrastructure_attempt_id,
        runtime_preparation_sha256=preparation.preparation_sha256,
        external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_launch_authorization_request_sha256=canonical_sha256(request),
        external_launch_authorization_sha256=authorization.authorization_sha256,
        external_runtime_termination_receipt_sha256=termination_receipt.termination_receipt_sha256,
        executor_identity_sha256=evidence.executor_identity_sha256,
        termination_evidence_sha256=evidence.termination_evidence_sha256,
        result_content_sha256=evidence.result_content_sha256,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        runtime_ended_at=evidence.ended_at,
        exit_code=evidence.exit_code,
        hard_deadline=NOW + timedelta(hours=1),
        artifact_submission_deadline=NOW + timedelta(hours=2),
        proof_signed_at=termination_receipt.signed_at,
        proof_expires_at=termination_receipt.expires_at,
        accepted_at=NOW - timedelta(seconds=2),
        billable_ended_at=evidence.ended_at,
    )


def _submission(
    preparation: ExternalRuntimePreparation,
    accepted: AcceptedExternalRuntimeTermination,
    *,
    disposition: str = "process_succeeded",
    expected_disposition: str | None = "process_succeeded",
    submitted_at_delta: timedelta = timedelta(seconds=-1),
) -> ExternalQualificationTerminalSubmission:
    return issue_external_qualification_terminal_submission(
        bridge_pin=_bridge_pin(),
        private_key=BRIDGE_PRIVATE_KEY,
        expected_disposition=expected_disposition,  # type: ignore[arg-type]
        bridge_manifest_sha256=preparation.bridge_manifest_sha256,
        intent_sha256=preparation.intent_sha256,
        execution_id=preparation.execution_id,
        attempt_id=preparation.infrastructure_attempt_id,
        resource_lease_sha256=_digest("resource-lease"),
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        accepted_external_runtime_termination_sha256=accepted.accepted_termination_sha256,
        artifact_manifest_sha256=_digest("artifact-manifest"),
        output_tree_sha256=_digest("output-tree"),
        artifact_verified_receipt_sha256s=(_digest("avr-1"),),
        disposition=disposition,  # type: ignore[arg-type]
        submitted_at=NOW + submitted_at_delta,
    )


def _accepted_submission(
    submission: ExternalQualificationTerminalSubmission,
    accepted_termination: AcceptedExternalRuntimeTermination,
) -> AcceptedExternalQualificationTerminalSubmission:
    return issue_accepted_external_qualification_terminal_submission(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        attempt_id=submission.attempt_id,
        bridge_manifest_sha256=submission.bridge_manifest_sha256,
        terminal_submission_sha256=submission.terminal_submission_sha256,
        accepted_external_runtime_termination_sha256=(
            accepted_termination.accepted_termination_sha256
        ),
        artifact_manifest_sha256=submission.artifact_manifest_sha256,
        output_tree_sha256=submission.output_tree_sha256,
        artifact_verified_receipt_sha256s=submission.artifact_verified_receipt_sha256s,
        disposition=submission.disposition,
        bridge_submitted_at=submission.submitted_at,
        artifact_submission_deadline=accepted_termination.artifact_submission_deadline,
        accepted_at=NOW,
    )


def _expiration(
    preparation: ExternalRuntimePreparation,
    request: RuntimeLaunchAuthorizationRequest,
    authorization: ExternalLaunchAuthorization,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    challenge: ExternalTerminationAcceptanceChallenge,
    termination_receipt: ExternalRuntimeTerminationReceipt,
    accepted_termination: AcceptedExternalRuntimeTermination,
) -> ExternalQualificationTerminalDeadlineExpiration:
    return issue_external_qualification_terminal_deadline_expiration(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        attempt_id=preparation.infrastructure_attempt_id,
        execution_id=preparation.execution_id,
        intent_sha256=preparation.intent_sha256,
        bridge_manifest_sha256=preparation.bridge_manifest_sha256,
        resource_lease_sha256=_digest("resource-lease"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_request_sha256=canonical_sha256(request),
        external_launch_authorization_sha256=authorization.authorization_sha256,
        external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        external_termination_challenge_sha256=challenge.challenge_sha256,
        external_runtime_termination_receipt_sha256=termination_receipt.termination_receipt_sha256,
        accepted_external_runtime_termination_sha256=accepted_termination.accepted_termination_sha256,
        executor_identity_sha256=accepted_termination.executor_identity_sha256,
        termination_evidence_sha256=accepted_termination.termination_evidence_sha256,
        result_content_sha256=accepted_termination.result_content_sha256,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        runtime_ended_at=accepted_termination.runtime_ended_at,
        exit_code=accepted_termination.exit_code,
        hard_deadline=accepted_termination.hard_deadline,
        artifact_submission_deadline=accepted_termination.artifact_submission_deadline,
        accepted_runtime_termination_at=accepted_termination.accepted_at,
        authorized_at=accepted_termination.accepted_at,
        expired_at=accepted_termination.artifact_submission_deadline,
    )


class _Chain:
    """The complete external custody chain built from one bridge authority."""

    def __init__(self) -> None:
        authority, manifest_sha256 = _bridge_authority()
        self.authority: ExternalBridgeAuthority = authority
        self.manifest_sha256 = manifest_sha256
        self.rc_verifier = RuntimeControlAuthorityVerifier(_runtime_control_pin())
        self.preparation = _preparation(manifest_sha256)
        self.request = _authorization_request(self.preparation)
        self.authorization = _authorization(self.preparation, self.request, _runtime_control_pin())
        self.identity = _executor_identity()
        self.launch_evidence = _launch_evidence(self.preparation, self.authorization, self.identity)
        self.launch_receipt = _launch_receipt(manifest_sha256, self.launch_evidence)
        self.termination_evidence = _termination_evidence(
            self.preparation, self.launch_receipt, self.identity
        )
        self.challenge = _challenge(
            self.preparation, self.launch_receipt, self.termination_evidence
        )
        self.termination_receipt = _termination_receipt(
            self.preparation,
            self.authorization,
            self.request,
            self.launch_receipt,
            self.challenge,
            self.termination_evidence,
        )
        self.accepted_termination = _accepted_termination(
            self.preparation,
            self.authorization,
            self.request,
            self.launch_receipt,
            self.challenge,
            self.termination_receipt,
            self.termination_evidence,
        )
        self.submission = _submission(self.preparation, self.accepted_termination)
        self.accepted_submission = _accepted_submission(self.submission, self.accepted_termination)
        self.expiration = _expiration(
            self.preparation,
            self.request,
            self.authorization,
            self.launch_receipt,
            self.challenge,
            self.termination_receipt,
            self.accepted_termination,
        )


def test_full_external_chain_roundtrip() -> None:
    chain = _Chain()

    verify_external_launch_authorization(
        authorization=chain.authorization,
        authorization_request=chain.request,
        preparation=chain.preparation,
        authority=chain.rc_verifier,
        observed_at=NOW - timedelta(seconds=19),
        observed_monotonic_ns=12_800,
    )
    verified_launch = verify_external_runtime_launch_receipt(
        receipt=chain.launch_receipt,
        authorization=chain.authorization,
        authorization_request=chain.request,
        preparation=chain.preparation,
        bridge_authority=chain.authority,
        runtime_control_authority=chain.rc_verifier,
        observed_at=NOW - timedelta(seconds=17),
        maximum_age_seconds=60.0,
    )
    assert verified_launch.launch_receipt_sha256 == chain.launch_receipt.launch_receipt_sha256

    verify_external_termination_acceptance_challenge(
        challenge=chain.challenge,
        preparation=chain.preparation,
        launch_receipt=chain.launch_receipt,
        termination_evidence=chain.termination_evidence,
        authority=chain.rc_verifier,
        observed_at=NOW - timedelta(seconds=3),
    )
    verified_termination = verify_external_runtime_termination_receipt(
        receipt=chain.termination_receipt,
        challenge=chain.challenge,
        preparation=chain.preparation,
        bridge_authority=chain.authority,
        observed_at=NOW - timedelta(seconds=2),
    )
    assert (
        verified_termination.termination_receipt_sha256
        == chain.termination_receipt.termination_receipt_sha256
    )

    verify_accepted_external_runtime_termination(
        accepted=chain.accepted_termination,
        receipt=chain.termination_receipt,
        challenge=chain.challenge,
        authority=chain.rc_verifier,
    )
    verified_submission = verify_external_qualification_terminal_submission(
        submission=chain.submission,
        accepted_termination=chain.accepted_termination,
        bridge_authority=chain.authority,
        observed_at=NOW,
    )
    assert verified_submission.disposition == "process_succeeded"
    verify_accepted_external_qualification_terminal_submission(
        accepted=chain.accepted_submission,
        submission=chain.submission,
        authority=chain.rc_verifier,
    )
    verify_external_qualification_terminal_deadline_expiration(
        expiration=chain.expiration,
        accepted_termination=chain.accepted_termination,
        authority=chain.rc_verifier,
    )


def test_canonical_digests_are_deterministic() -> None:
    first = _Chain()
    second = _Chain()
    assert first.authorization.authorization_sha256 == second.authorization.authorization_sha256
    assert first.launch_receipt.launch_receipt_sha256 == second.launch_receipt.launch_receipt_sha256
    assert (
        first.accepted_submission.terminal_authority_sha256
        == second.accepted_submission.terminal_authority_sha256
    )


def test_launch_authorization_rejects_rebound_preparation() -> None:
    chain = _Chain()
    tampered = chain.authorization.model_copy(
        update={"runtime_preparation_sha256": _digest("other-preparation")}
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_launch_authorization(
            authorization=tampered,
            authorization_request=chain.request,
            preparation=chain.preparation,
            authority=chain.rc_verifier,
            observed_at=NOW - timedelta(seconds=19),
            observed_monotonic_ns=12_800,
        )


def test_launch_authorization_rejects_stale_request_clock() -> None:
    chain = _Chain()
    with pytest.raises(QualificationVerificationError):
        verify_external_launch_authorization(
            authorization=chain.authorization,
            authorization_request=chain.request,
            preparation=chain.preparation,
            authority=chain.rc_verifier,
            observed_at=NOW + timedelta(seconds=5),
            observed_monotonic_ns=90_000_000_000,
        )


def test_launch_authorization_rejects_foreign_runtime_control_key() -> None:
    chain = _Chain()
    foreign_key = Ed25519PrivateKey.generate()
    foreign_public = foreign_key.public_key().public_bytes_raw().hex()
    foreign_pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-control-policy:v1"),
        principal_id="principal:runtime-control",
        key_id=qualification_key_id(foreign_public),
        public_key_ed25519_hex=foreign_public,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=4),
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_launch_authorization(
            authorization=chain.authorization,
            authorization_request=chain.request,
            preparation=chain.preparation,
            authority=RuntimeControlAuthorityVerifier(foreign_pin),
            observed_at=NOW - timedelta(seconds=19),
            observed_monotonic_ns=12_800,
        )


def test_launch_receipt_rejects_other_bridge_key() -> None:
    chain = _Chain()
    wrong_key_receipt = _launch_receipt(
        chain.manifest_sha256,
        chain.launch_evidence,
        private_key=OTHER_BRIDGE_PRIVATE_KEY,
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_runtime_launch_receipt(
            receipt=wrong_key_receipt,
            authorization=chain.authorization,
            authorization_request=chain.request,
            preparation=chain.preparation,
            bridge_authority=chain.authority,
            runtime_control_authority=chain.rc_verifier,
            observed_at=NOW - timedelta(seconds=17),
            maximum_age_seconds=60.0,
        )


def test_launch_receipt_rejects_other_bridge_manifest() -> None:
    chain = _Chain()
    # signed by the right key, but bound to a foreign manifest digest: the
    # signature verifies, the manifest binding must fail
    foreign_manifest_receipt = _launch_receipt(
        _digest("foreign-bridge-manifest"), chain.launch_evidence
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_runtime_launch_receipt(
            receipt=foreign_manifest_receipt,
            authorization=chain.authorization,
            authorization_request=chain.request,
            preparation=chain.preparation,
            bridge_authority=chain.authority,
            runtime_control_authority=chain.rc_verifier,
            observed_at=NOW - timedelta(seconds=17),
            maximum_age_seconds=60.0,
        )


def test_launch_receipt_rejects_stale_proof() -> None:
    chain = _Chain()
    with pytest.raises(QualificationVerificationError):
        verify_external_runtime_launch_receipt(
            receipt=chain.launch_receipt,
            authorization=chain.authorization,
            authorization_request=chain.request,
            preparation=chain.preparation,
            bridge_authority=chain.authority,
            runtime_control_authority=chain.rc_verifier,
            observed_at=NOW + timedelta(seconds=30),
            maximum_age_seconds=10.0,
        )


def test_launch_evidence_rejects_identity_digest_mismatch() -> None:
    chain = _Chain()
    scope = chain.launch_evidence.model_dump(mode="python")
    scope["executor_identity_sha256"] = _digest("tampered-identity")
    with pytest.raises(ValidationError):
        ExternalLaunchEvidence(**scope)


def test_challenge_rejects_rebound_evidence() -> None:
    chain = _Chain()
    other_evidence = _termination_evidence(
        chain.preparation,
        chain.launch_receipt,
        chain.identity,
        result_sha=_digest("another-result"),
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_termination_acceptance_challenge(
            challenge=chain.challenge,
            preparation=chain.preparation,
            launch_receipt=chain.launch_receipt,
            termination_evidence=other_evidence,
            authority=chain.rc_verifier,
            observed_at=NOW - timedelta(seconds=3),
        )


def test_termination_receipt_rejects_foreign_challenge() -> None:
    chain = _Chain()
    other_evidence = _termination_evidence(
        chain.preparation,
        chain.launch_receipt,
        chain.identity,
        result_sha=_digest("another-result"),
    )
    other_challenge = _challenge(chain.preparation, chain.launch_receipt, other_evidence)
    with pytest.raises(QualificationVerificationError):
        verify_external_runtime_termination_receipt(
            receipt=chain.termination_receipt,
            challenge=other_challenge,
            preparation=chain.preparation,
            bridge_authority=chain.authority,
            observed_at=NOW - timedelta(seconds=2),
        )


def test_termination_receipt_rejects_expired_proof() -> None:
    chain = _Chain()
    with pytest.raises(QualificationVerificationError):
        verify_external_runtime_termination_receipt(
            receipt=chain.termination_receipt,
            challenge=chain.challenge,
            preparation=chain.preparation,
            bridge_authority=chain.authority,
            observed_at=NOW + timedelta(seconds=120),
        )


def test_accepted_termination_rejects_rebound_receipt() -> None:
    chain = _Chain()
    tampered = chain.accepted_termination.model_copy(
        update={"external_runtime_termination_receipt_sha256": _digest("other-receipt")}
    )
    with pytest.raises(QualificationVerificationError):
        verify_accepted_external_runtime_termination(
            accepted=tampered,
            receipt=chain.termination_receipt,
            challenge=chain.challenge,
            authority=chain.rc_verifier,
        )


def test_submission_rejects_observed_disposition_mismatch() -> None:
    chain = _Chain()
    with pytest.raises(QualificationVerificationError):
        _submission(
            chain.preparation,
            chain.accepted_termination,
            disposition="process_failed",
            expected_disposition="process_succeeded",
        )


def test_submission_rejects_window_miss() -> None:
    chain = _Chain()
    late = _submission(
        chain.preparation,
        chain.accepted_termination,
        submitted_at_delta=timedelta(hours=3),
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_qualification_terminal_submission(
            submission=late,
            accepted_termination=chain.accepted_termination,
            bridge_authority=chain.authority,
            observed_at=NOW + timedelta(hours=3),
        )


def test_accepted_submission_rejects_rebound_fields() -> None:
    chain = _Chain()
    tampered = chain.accepted_submission.model_copy(
        update={"artifact_manifest_sha256": _digest("other-manifest")}
    )
    with pytest.raises(QualificationVerificationError):
        verify_accepted_external_qualification_terminal_submission(
            accepted=tampered,
            submission=chain.submission,
            authority=chain.rc_verifier,
        )


def test_deadline_expiration_must_activate_at_artifact_deadline() -> None:
    chain = _Chain()
    scope = chain.expiration.model_dump(mode="python")
    scope["expired_at"] = chain.accepted_termination.artifact_submission_deadline + timedelta(
        seconds=1
    )
    with pytest.raises(ValidationError):
        ExternalQualificationTerminalDeadlineExpiration(**scope)


def test_deadline_expiration_rejects_rebound_acceptance() -> None:
    chain = _Chain()
    tampered = chain.expiration.model_copy(
        update={"accepted_external_runtime_termination_sha256": _digest("other-acceptance")}
    )
    with pytest.raises(QualificationVerificationError):
        verify_external_qualification_terminal_deadline_expiration(
            expiration=tampered,
            accepted_termination=chain.accepted_termination,
            authority=chain.rc_verifier,
        )


def test_recompute_external_disposition_covers_all_four_outcomes() -> None:
    assert (
        recompute_external_disposition(
            exit_code=0, deadline_exceeded=False, required_artifacts_present=True
        )
        == "process_succeeded"
    )
    assert (
        recompute_external_disposition(
            exit_code=1, deadline_exceeded=False, required_artifacts_present=True
        )
        == "process_failed"
    )
    assert (
        recompute_external_disposition(
            exit_code=0, deadline_exceeded=False, required_artifacts_present=False
        )
        == "invalid_output"
    )
    assert (
        recompute_external_disposition(
            exit_code=0, deadline_exceeded=True, required_artifacts_present=True
        )
        == "timeout"
    )


def test_signature_patterns_reject_non_hex() -> None:
    chain = _Chain()
    scope = chain.launch_receipt.model_dump(mode="python")
    scope["signature_ed25519_hex"] = "z" * 128
    with pytest.raises(ValidationError):
        ExternalRuntimeLaunchReceipt(**scope)
