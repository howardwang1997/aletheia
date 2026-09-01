"""Deployment-pinned runtime-control signing authority for qualification execution.

The allocator needs one narrow online signer for launch, recovery, termination, and terminal
deadline decisions.  Keeping that signer behind :class:`RuntimeControlIssuancePort` prevents the
node agent and OCI runtime from receiving the raw key or a generic signing callback.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    HistoricalRuntimeRecoveryGrant,
    QualificationTerminalDeadlineExpiration,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeTerminationAcceptanceChallenge,
    issue_accepted_qualification_terminal_submission,
    issue_accepted_runtime_termination,
    issue_historical_runtime_recovery_grant,
    issue_qualification_terminal_deadline_expiration,
    issue_runtime_launch_authorization,
    issue_runtime_termination_acceptance_challenge,
)


class PinnedRuntimeControlIssuanceAuthority:
    """Exact Ed25519 key custody implementing only the runtime-control issuance port."""

    def __init__(self, *, pin: RuntimeControlAuthorityPin, private_key: bytes) -> None:
        self._pin = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
        key = bytes(private_key)
        try:
            public_key = (
                Ed25519PrivateKey.from_private_bytes(key)
                .public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except ValueError as exc:
            raise ValueError(
                "runtime-control private key must contain exactly 32 raw bytes"
            ) from exc
        if public_key != self._pin.public_key_ed25519_hex:
            raise ValueError("runtime-control private key differs from its deployment pin")
        self._private_key = key
        self._verifier = RuntimeControlAuthorityVerifier(self._pin)

    @property
    def authority_pin(self) -> RuntimeControlAuthorityPin:
        return self._pin

    @property
    def authority_verifier(self) -> RuntimeControlAuthorityVerifier:
        return self._verifier

    def issue_launch_authorization(
        self,
        *,
        authorization_request,
        preparation,
        admission_sha256,
        qualification_grant_sha256,
        lease_expires_at,
        hard_deadline,
        issued_at,
        expires_at,
        max_launch_delay_ns,
    ) -> RuntimeLaunchAuthorization:
        return issue_runtime_launch_authorization(
            pin=self._pin,
            private_key=self._private_key,
            admission_sha256=admission_sha256,
            qualification_grant_sha256=qualification_grant_sha256,
            node_manifest_sha256=preparation.node_manifest_sha256,
            node_id=preparation.node_id,
            boot_id=preparation.boot_id,
            execution_id=preparation.execution_id,
            infrastructure_attempt_id=preparation.infrastructure_attempt_id,
            intent_sha256=preparation.intent_sha256,
            runtime_preparation_sha256=preparation.preparation_sha256,
            authorization_request_sha256=authorization_request.request_sha256,
            launch_spec_sha256=preparation.launch_spec_sha256,
            oci_config_sha256=preparation.oci_config_sha256,
            workload_executable_sha256=preparation.workload_executable_sha256,
            workload_argv=preparation.workload_argv,
            enforced_placement_sha256=preparation.enforced_placement_sha256,
            input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
            fencing_epoch=preparation.fencing_epoch,
            lease_token_sha256=preparation.lease_token_sha256,
            lease_expires_at=lease_expires_at,
            hard_deadline=hard_deadline,
            issued_at=issued_at,
            expires_at=expires_at,
            max_launch_delay_ns=max_launch_delay_ns,
        )

    def issue_historical_recovery(self, **scope) -> HistoricalRuntimeRecoveryGrant:
        return issue_historical_runtime_recovery_grant(
            pin=self._pin,
            private_key=self._private_key,
            **scope,
        )

    def issue_termination_challenge(
        self,
        *,
        preparation,
        launch_receipt,
        termination_evidence,
        inspection_sequence,
        node_inventory_sha256,
        resource_lease_sha256,
        fencing_epoch,
        lease_token_sha256,
        hard_deadline,
        artifact_submission_deadline,
        challenged_at,
        expires_at,
    ) -> RuntimeTerminationAcceptanceChallenge:
        evidence = RuntimeInspectionEvidence.model_validate(
            termination_evidence.model_dump(mode="python")
        )
        return issue_runtime_termination_acceptance_challenge(
            pin=self._pin,
            private_key=self._private_key,
            attempt_id=preparation.infrastructure_attempt_id,
            execution_id=preparation.execution_id,
            intent_sha256=preparation.intent_sha256,
            node_manifest_sha256=preparation.node_manifest_sha256,
            runtime_preparation_sha256=preparation.preparation_sha256,
            node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
            runtime_identity_sha256=launch_receipt.launch_evidence.runtime_identity_sha256,
            runtime_inspection_evidence_sha256=evidence.inspection_sha256,
            inspection_sequence=inspection_sequence,
            node_inventory_sha256=node_inventory_sha256,
            resource_lease_sha256=resource_lease_sha256,
            fencing_epoch=fencing_epoch,
            lease_token_sha256=lease_token_sha256,
            hard_deadline=hard_deadline,
            artifact_submission_deadline=artifact_submission_deadline,
            challenged_at=challenged_at,
            expires_at=expires_at,
        )

    def issue_accepted_termination(self, **scope) -> AcceptedRuntimeTermination:
        return issue_accepted_runtime_termination(
            pin=self._pin,
            private_key=self._private_key,
            runtime_authority=self._verifier,
            **scope,
        )

    def issue_terminal_submission_acceptance(
        self, **scope
    ) -> AcceptedQualificationTerminalSubmission:
        return issue_accepted_qualification_terminal_submission(
            pin=self._pin,
            private_key=self._private_key,
            runtime_authority=self._verifier,
            **scope,
        )

    def issue_terminal_deadline_expiration(
        self, **scope
    ) -> QualificationTerminalDeadlineExpiration:
        return issue_qualification_terminal_deadline_expiration(
            pin=self._pin,
            private_key=self._private_key,
            runtime_authority=self._verifier,
            **scope,
        )


__all__ = ["PinnedRuntimeControlIssuanceAuthority"]
