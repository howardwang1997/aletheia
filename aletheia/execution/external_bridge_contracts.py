"""External-bridge runtime custody contracts (contradiction #20 remedy).

The nodeless external placement mode (PR #188) reserves attempts with a full
custody lattice — lease token, fencing epoch, hard deadline, external resource
lease — but stops at admission.  The node acceptance chain's vocabulary (node
inventory, device holds, OCI launch evidence, loop-mounted output quota) cannot
express a bridge dispatch, and the DB placement-mode xor pins that separation.

These contracts mirror the v2 runtime-custody chain one-for-one with external
vocabulary:

* the deployment-pinned external bridge authority signs receipts over the
  out-of-band executor (launch, termination, terminal submission), exactly as
  an enrolled node signs its receipts;
* the deployment runtime-control authority signs every allocator-side ticket
  (launch authorization, termination challenge, acceptances, deadline
  expiration), through the same ``RuntimeControlAuthorityVerifier`` pin checks;
* nothing node-shaped is synthesized: there is no inventory attestation, no
  device hold, no transport envelope, and no loop-backed output quota — the
  executor is an in-process pinned service invocation whose outputs quarantine
  into the deployment artifact store before terminal submission.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.runtime_contracts import (
    ExternalBridgeAuthority,
    QualificationAuthorityPin,
    QualificationVerificationError,
)
from aletheia.execution.runtime_v2_contracts import (
    RUNTIME_V2_CONTRACT_SCHEMA_VERSION,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeLaunchAuthorizationRequest,
    _ATTEMPT_ID_PATTERN,
    _EXECUTION_ID_PATTERN,
    _SHA256_PATTERN,
    _SIGNATURE_PATTERN,
    _SYMBOLIC_ID_PATTERN,
    _public_key_hex,
    _require_utc,
    _runtime_control_message,
)
from aletheia.execution.schemas import (
    ExecutionModel,
    canonical_json_bytes,
    canonical_sha256,
)

EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION = RUNTIME_V2_CONTRACT_SCHEMA_VERSION


def _sign(message: bytes, private_key: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(private_key).sign(message).hex()


def verify_external_bridge_signature(
    *,
    authority: ExternalBridgeAuthority,
    signing_key_id: str,
    message: bytes,
    signature_ed25519_hex: str,
    signed_at: datetime,
) -> QualificationAuthorityPin:
    """Verify one bridge receipt signature against the pinned bridge authority.

    Returns the verified pin so callers can bind further pin equality checks.
    """

    pin = authority.bridge_authority_pin
    _require_utc(signed_at, "external bridge receipt signed_at")
    if signing_key_id != pin.key_id or not pin.active_at(signed_at):
        raise QualificationVerificationError(
            "external bridge receipt is not issued by the active pinned authority"
        )
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
            bytes.fromhex(signature_ed25519_hex), message
        )
    except (InvalidSignature, ValueError) as exc:
        raise QualificationVerificationError(
            "external bridge receipt signature is invalid"
        ) from exc
    return pin


class ExternalExecutorIdentity(ExecutionModel):
    """Identity of one out-of-band executor invocation.

    The external executor is an in-process pinned service call, not a
    container: identity binds the resolved factory references and their frozen
    implementation digests plus wall/monotonic start observations, mirroring
    the PID-reuse-safe node identity without synthesizing node fields.
    """

    schema_name: Literal["aletheia.external_executor_identity"] = (
        "aletheia.external_executor_identity"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    executor_ref: str = Field(min_length=1, max_length=256)
    executor_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    invocation_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    started_monotonic_ns: int = Field(ge=0)

    @property
    def executor_identity_sha256(self) -> str:
        return canonical_sha256(self)


class ExternalRuntimePreparation(ExecutionModel):
    """Crash-durable, inert external dispatch metadata created before launch.

    Mirrors ``RuntimePreparation`` minus the node/boot/OCI vocabulary.  There
    is no output-quota provisioning receipt: external outputs quarantine into
    the deployment artifact store and are bound by the terminal submission.
    """

    schema_name: Literal["aletheia.external_runtime_preparation"] = (
        "aletheia.external_runtime_preparation"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    runtime_engine: str = Field(min_length=1, max_length=128)
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_dispatch_locator_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    prepared_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @property
    def preparation_sha256(self) -> str:
        return canonical_sha256(self)

    @model_validator(mode="after")
    def _preparation_is_canonical(self) -> "ExternalRuntimePreparation":
        _require_utc(self.prepared_at, "external runtime preparation prepared_at")
        return self


class ExternalLaunchAuthorization(ExecutionModel):
    """Short-lived DB launch ticket for one external dispatch, after preparation."""

    schema_name: Literal["aletheia.external_launch_authorization"] = (
        "aletheia.external_launch_authorization"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    lease_expires_at: AwareDatetime
    hard_deadline: AwareDatetime
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    max_launch_delay_ns: int = Field(ge=1, le=60_000_000_000)
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authorization_window_is_ordered(self) -> "ExternalLaunchAuthorization":
        _require_utc(self.issued_at, "external launch authorization issued_at")
        if not self.issued_at < self.expires_at <= self.hard_deadline:
            raise ValueError("external launch authorization window is misordered")
        if self.lease_expires_at > self.hard_deadline:
            raise ValueError("external launch authorization lease outlives the hard deadline")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="external_launch_authorization", payload=self.signature_payload
        )

    @property
    def authorization_sha256(self) -> str:
        return canonical_sha256(self)


def issue_external_launch_authorization(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> ExternalLaunchAuthorization:
    """Issue one external DB launch ticket; callers provide the immutable scope."""

    pinned = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
    try:
        unsigned = ExternalLaunchAuthorization(
            **scope,
            runtime_control_policy_sha256=pinned.policy_sha256,
            authorized_by_principal_id=pinned.principal_id,
            authorization_key_id=pinned.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external launch authorization scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pinned.public_key_ed25519_hex
        or not pinned.active_at(unsigned.issued_at)
        or unsigned.expires_at > pinned.active_until
    ):
        raise QualificationVerificationError("external launch authorization signer is inactive")
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalLaunchAuthorization.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_external_launch_authorization(
    *,
    authorization: ExternalLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: ExternalRuntimePreparation,
    authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
    observed_monotonic_ns: int,
) -> None:
    """Verify an external ticket immediately before the executor invocation."""

    _require_utc(observed_at, "external launch authorization observed_at")
    authorization = ExternalLaunchAuthorization.model_validate(
        authorization.model_dump(mode="python")
    )
    preparation = ExternalRuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    if (
        authorization.runtime_preparation_sha256 != preparation.preparation_sha256
        or authorization.authorization_request_sha256 != canonical_sha256(authorization_request)
        or authorization.bridge_manifest_sha256 != preparation.bridge_manifest_sha256
        or authorization.execution_id != preparation.execution_id
        or authorization.infrastructure_attempt_id != preparation.infrastructure_attempt_id
        or authorization.intent_sha256 != preparation.intent_sha256
        or authorization.launch_spec_sha256 != preparation.launch_spec_sha256
        or authorization.workload_executable_sha256 != preparation.workload_executable_sha256
        or authorization.workload_argv != preparation.workload_argv
        or authorization.enforced_placement_sha256 != preparation.enforced_placement_sha256
        or authorization.input_materialization_receipt_sha256
        != preparation.input_materialization_receipt_sha256
        or authorization.fencing_epoch != preparation.fencing_epoch
        or authorization.lease_token_sha256 != preparation.lease_token_sha256
    ):
        raise QualificationVerificationError(
            "external launch authorization differs from exact preparation"
        )
    if authorization_request.runtime_preparation_sha256 != preparation.preparation_sha256:
        raise QualificationVerificationError(
            "external launch authorization request binds another preparation"
        )
    monotonic_age_ns = observed_monotonic_ns - authorization_request.requested_monotonic_ns
    if (
        authorization_request.requested_at > observed_at
        or monotonic_age_ns < 0
        or monotonic_age_ns >= authorization.max_launch_delay_ns
    ):
        raise QualificationVerificationError(
            "external launch authorization is stale against the request clock"
        )
    authority.verify(
        kind="external_launch_authorization",
        payload=authorization.signature_payload,
        signature_ed25519_hex=authorization.signature_ed25519_hex,
        policy_sha256=authorization.runtime_control_policy_sha256,
        principal_id=authorization.authorized_by_principal_id,
        key_id=authorization.authorization_key_id,
        signed_at=authorization.issued_at,
        expires_at=authorization.expires_at,
        observed_at=observed_at,
    )


class ExternalLaunchEvidence(ExecutionModel):
    """Evidence observed only after the pinned executor actually starts work."""

    schema_name: Literal["aletheia.external_launch_evidence"] = "aletheia.external_launch_evidence"
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity: ExternalExecutorIdentity
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_start_monotonic_lower_bound_ns: int = Field(ge=0)
    executor_start_monotonic_upper_bound_exclusive_ns: int = Field(ge=1)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_fencing_epoch: int = Field(ge=1)
    enforced_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_evidence_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _evidence_is_exact_and_ordered(self) -> "ExternalLaunchEvidence":
        _require_utc(self.observed_at, "external launch evidence observed_at")
        if self.executor_identity_sha256 != self.executor_identity.executor_identity_sha256:
            raise ValueError("external launch evidence identity differs from its exact bytes")
        if (
            self.executor_start_monotonic_lower_bound_ns
            >= self.executor_start_monotonic_upper_bound_exclusive_ns
            or self.executor_identity.started_monotonic_ns
            < self.executor_start_monotonic_lower_bound_ns
            or self.executor_identity.started_monotonic_ns
            >= self.executor_start_monotonic_upper_bound_exclusive_ns
            or self.observed_monotonic_ns < self.executor_identity.started_monotonic_ns
            or self.observed_at < self.executor_identity.started_at
        ):
            raise ValueError("external launch evidence start interval or ordering is invalid")
        return self

    @property
    def launch_evidence_sha256(self) -> str:
        return canonical_sha256(self)


class ExternalRuntimeLaunchReceipt(ExecutionModel):
    """Bridge-signed binding from one inert preparation to one started executor."""

    schema_name: Literal["aletheia.external_runtime_launch_receipt"] = (
        "aletheia.external_runtime_launch_receipt"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_evidence: ExternalLaunchEvidence
    launch_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_binds_its_evidence(self) -> "ExternalRuntimeLaunchReceipt":
        _require_utc(self.signed_at, "external runtime launch receipt signed_at")
        if self.launch_evidence_sha256 != self.launch_evidence.launch_evidence_sha256:
            raise ValueError(
                "external runtime launch receipt changed or predates its exact evidence"
            )
        if self.signed_at < self.launch_evidence.observed_at:
            raise ValueError("external runtime launch receipt predates its observation")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def launch_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedExternalRuntimeLaunch(ExecutionModel):
    launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime


def issue_external_runtime_launch_receipt(
    *,
    bridge_pin: QualificationAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> ExternalRuntimeLaunchReceipt:
    """Issue one bridge-signed launch receipt over exact evidence bytes."""

    pinned = QualificationAuthorityPin.model_validate(bridge_pin.model_dump(mode="python"))
    try:
        unsigned = ExternalRuntimeLaunchReceipt(
            **scope, signing_key_id=pinned.key_id, signature_ed25519_hex="0" * 128
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external runtime launch receipt scope is invalid"
        ) from exc
    if _public_key_hex(private_key) != pinned.public_key_ed25519_hex or not pinned.active_at(
        unsigned.signed_at
    ):
        raise QualificationVerificationError("external runtime launch receipt signer is inactive")
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalRuntimeLaunchReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_external_runtime_launch_receipt(
    *,
    receipt: ExternalRuntimeLaunchReceipt,
    authorization: ExternalLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: ExternalRuntimePreparation,
    bridge_authority: ExternalBridgeAuthority,
    runtime_control_authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
    maximum_age_seconds: float,
) -> VerifiedExternalRuntimeLaunch:
    """Fresh-verify one external launch receipt against both pinned authorities.

    The runtime-control ticket and the bridge receipt verify independently: a
    compromised bridge key cannot mint its own launch authorization, and the
    control authority cannot attest engine facts it never observed.
    """

    try:
        receipt = ExternalRuntimeLaunchReceipt.model_validate(receipt.model_dump(mode="python"))
        evidence = receipt.launch_evidence
        verify_external_launch_authorization(
            authorization=authorization,
            authorization_request=authorization_request,
            preparation=preparation,
            authority=runtime_control_authority,
            observed_at=evidence.observed_at,
            observed_monotonic_ns=evidence.observed_monotonic_ns,
        )
        if (
            evidence.preparation_sha256 != preparation.preparation_sha256
            or evidence.external_launch_authorization_sha256 != authorization.authorization_sha256
            or evidence.enforced_fencing_epoch != preparation.fencing_epoch
            or evidence.enforced_lease_token_sha256 != preparation.lease_token_sha256
            or evidence.input_materialization_receipt_sha256
            != preparation.input_materialization_receipt_sha256
            or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
        ):
            raise QualificationVerificationError(
                "external launch evidence does not bind its preparation and ticket"
            )
        if (
            receipt.bridge_manifest_sha256 != bridge_authority.manifest.manifest_sha256
            or preparation.bridge_manifest_sha256 != bridge_authority.manifest.manifest_sha256
        ):
            raise QualificationVerificationError(
                "external launch receipt belongs to another bridge authority"
            )
        age_seconds = (observed_at - receipt.signed_at).total_seconds()
        if age_seconds < 0 or age_seconds > maximum_age_seconds:
            raise QualificationVerificationError(
                "external launch receipt proof is stale for fresh acceptance"
            )
        _require_utc(observed_at, "external launch receipt observed_at")
        verify_external_bridge_signature(
            authority=bridge_authority,
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.signed_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external runtime launch receipt failed closed revalidation"
        ) from exc
    return VerifiedExternalRuntimeLaunch(
        launch_receipt_sha256=receipt.launch_receipt_sha256,
        preparation_sha256=preparation.preparation_sha256,
        executor_identity_sha256=evidence.executor_identity_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        verified_at=receipt.signed_at,
    )


class ExternalTerminationEvidence(ExecutionModel):
    """Terminal observation of one finished executor invocation.

    ``result_content_sha256`` binds the canonical bytes the executor produced
    (the typed result on success, the fail-closed failure record on detected
    failure) so the terminal submission's artifact rehash can be cross-checked
    against what termination actually observed.
    """

    schema_name: Literal["aletheia.external_termination_evidence"] = (
        "aletheia.external_termination_evidence"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    exit_code: int = Field(ge=0, le=255)
    ended_at: AwareDatetime
    ended_monotonic_ns: int = Field(ge=0)
    result_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _termination_is_ordered(self) -> "ExternalTerminationEvidence":
        _require_utc(self.ended_at, "external termination evidence ended_at")
        return self

    @property
    def termination_evidence_sha256(self) -> str:
        return canonical_sha256(self)


class ExternalTerminationAcceptanceChallenge(ExecutionModel):
    """DB-signed, short-lived challenge for one exact external terminal observation."""

    schema_name: Literal["aletheia.external_termination_acceptance_challenge"] = (
        "aletheia.external_termination_acceptance_challenge"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    challenge_id: str = Field(pattern=_SHA256_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    challenged_at: AwareDatetime
    expires_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenged_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    challenge_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _challenge_window_is_ordered(self) -> "ExternalTerminationAcceptanceChallenge":
        _require_utc(self.challenged_at, "external termination challenge challenged_at")
        if not self.challenged_at < self.expires_at <= self.artifact_submission_deadline:
            raise ValueError("external termination challenge window is misordered")
        if self.hard_deadline > self.artifact_submission_deadline:
            raise ValueError("external termination challenge artifact deadline precedes deadline")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="external_termination_acceptance_challenge", payload=self.signature_payload
        )

    @property
    def challenge_sha256(self) -> str:
        return canonical_sha256(self)


def issue_external_termination_acceptance_challenge(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> ExternalTerminationAcceptanceChallenge:
    """Issue one short-lived external termination challenge over exact evidence."""

    pinned = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
    try:
        unsigned = ExternalTerminationAcceptanceChallenge(
            **scope,
            runtime_control_policy_sha256=pinned.policy_sha256,
            challenged_by_principal_id=pinned.principal_id,
            challenge_key_id=pinned.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external termination acceptance challenge scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pinned.public_key_ed25519_hex
        or not pinned.active_at(unsigned.challenged_at)
        or unsigned.expires_at > pinned.active_until
    ):
        raise QualificationVerificationError(
            "external termination acceptance challenge signer is inactive"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalTerminationAcceptanceChallenge.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_external_termination_acceptance_challenge(
    *,
    challenge: ExternalTerminationAcceptanceChallenge,
    preparation: ExternalRuntimePreparation,
    launch_receipt: ExternalRuntimeLaunchReceipt,
    termination_evidence: ExternalTerminationEvidence,
    authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
) -> None:
    """Verify one external challenge against its exact receipt lineage and pin."""

    _require_utc(observed_at, "external termination challenge observed_at")
    challenge = ExternalTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    evidence = ExternalTerminationEvidence.model_validate(
        termination_evidence.model_dump(mode="python")
    )
    if (
        challenge.runtime_preparation_sha256 != preparation.preparation_sha256
        or challenge.external_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
        or challenge.termination_evidence_sha256 != evidence.termination_evidence_sha256
        or challenge.executor_identity_sha256 != evidence.executor_identity_sha256
        or challenge.result_content_sha256 != evidence.result_content_sha256
        or evidence.preparation_sha256 != preparation.preparation_sha256
        or evidence.external_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
        or challenge.intent_sha256 != preparation.intent_sha256
        or challenge.bridge_manifest_sha256 != preparation.bridge_manifest_sha256
        or challenge.fencing_epoch != preparation.fencing_epoch
        or challenge.lease_token_sha256 != preparation.lease_token_sha256
    ):
        raise QualificationVerificationError(
            "external termination challenge does not bind its exact lineage"
        )
    authority.verify(
        kind="external_termination_acceptance_challenge",
        payload=challenge.signature_payload,
        signature_ed25519_hex=challenge.signature_ed25519_hex,
        policy_sha256=challenge.runtime_control_policy_sha256,
        principal_id=challenge.challenged_by_principal_id,
        key_id=challenge.challenge_key_id,
        signed_at=challenge.challenged_at,
        expires_at=challenge.expires_at,
        observed_at=observed_at,
    )


class ExternalRuntimeTerminationReceipt(ExecutionModel):
    """Bridge signature over the complete external terminal evidence and DB challenge."""

    schema_name: Literal["aletheia.external_runtime_termination_receipt"] = (
        "aletheia.external_runtime_termination_receipt"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_evidence: ExternalTerminationEvidence
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    expires_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_binds_its_evidence(self) -> "ExternalRuntimeTerminationReceipt":
        _require_utc(self.signed_at, "external runtime termination receipt signed_at")
        if (
            self.termination_evidence_sha256
            != self.termination_evidence.termination_evidence_sha256
        ):
            raise ValueError(
                "external runtime termination receipt changed or predates its exact evidence"
            )
        if self.signed_at < self.termination_evidence.ended_at or not (
            self.signed_at < self.expires_at
        ):
            raise ValueError("external runtime termination receipt ordering is invalid")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def termination_receipt_sha256(self) -> str:
        return canonical_sha256(self)


def issue_external_runtime_termination_receipt(
    *,
    bridge_pin: QualificationAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> ExternalRuntimeTerminationReceipt:
    """Issue one bridge-signed termination receipt binding the exact DB challenge."""

    pinned = QualificationAuthorityPin.model_validate(bridge_pin.model_dump(mode="python"))
    try:
        unsigned = ExternalRuntimeTerminationReceipt(
            **scope, signing_key_id=pinned.key_id, signature_ed25519_hex="0" * 128
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external runtime termination receipt scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pinned.public_key_ed25519_hex
        or not pinned.active_at(unsigned.signed_at)
        or unsigned.expires_at > pinned.active_until
    ):
        raise QualificationVerificationError(
            "external runtime termination receipt signer is inactive"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalRuntimeTerminationReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


class VerifiedExternalRuntimeTermination(ExecutionModel):
    termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    exit_code: int = Field(ge=0, le=255)
    runtime_ended_at: AwareDatetime
    result_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime


def verify_external_runtime_termination_receipt(
    *,
    receipt: ExternalRuntimeTerminationReceipt,
    challenge: ExternalTerminationAcceptanceChallenge,
    preparation: ExternalRuntimePreparation,
    bridge_authority: ExternalBridgeAuthority,
    observed_at: datetime,
) -> VerifiedExternalRuntimeTermination:
    """Fresh-verify one external termination receipt against its exact challenge."""

    try:
        receipt = ExternalRuntimeTerminationReceipt.model_validate(
            receipt.model_dump(mode="python")
        )
        evidence = receipt.termination_evidence
        if (
            receipt.challenge_sha256 != challenge.challenge_sha256
            or receipt.runtime_preparation_sha256 != preparation.preparation_sha256
            or receipt.external_runtime_launch_receipt_sha256
            != challenge.external_runtime_launch_receipt_sha256
            or receipt.termination_evidence_sha256 != challenge.termination_evidence_sha256
            or receipt.termination_evidence.preparation_sha256 != preparation.preparation_sha256
        ):
            raise QualificationVerificationError(
                "external termination receipt does not bind its exact challenge"
            )
        if (
            receipt.bridge_manifest_sha256 != bridge_authority.manifest.manifest_sha256
            or preparation.bridge_manifest_sha256 != bridge_authority.manifest.manifest_sha256
        ):
            raise QualificationVerificationError(
                "external termination receipt belongs to another bridge authority"
            )
        _require_utc(observed_at, "external termination receipt observed_at")
        if not receipt.signed_at <= observed_at < receipt.expires_at:
            raise QualificationVerificationError(
                "external termination receipt proof is stale for fresh acceptance"
            )
        verify_external_bridge_signature(
            authority=bridge_authority,
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.signed_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external runtime termination receipt failed closed revalidation"
        ) from exc
    return VerifiedExternalRuntimeTermination(
        termination_receipt_sha256=receipt.termination_receipt_sha256,
        challenge_sha256=challenge.challenge_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        exit_code=evidence.exit_code,
        runtime_ended_at=evidence.ended_at,
        result_content_sha256=evidence.result_content_sha256,
        verified_at=receipt.signed_at,
    )


class AcceptedExternalRuntimeTermination(ExecutionModel):
    """Immutable DB acceptance of a fresh external termination proof.

    Deliberately cannot bind an artifact manifest: output quarantine and rehash
    happen after this acceptance releases the lease; the bridge-signed terminal
    submission later binds artifacts to ``accepted_termination_sha256``.
    """

    schema_name: Literal["aletheia.accepted_external_runtime_termination"] = (
        "aletheia.accepted_external_runtime_termination"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_ended_at: AwareDatetime
    exit_code: int = Field(ge=0, le=255)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    proof_signed_at: AwareDatetime
    proof_expires_at: AwareDatetime
    accepted_at: AwareDatetime
    billable_ended_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    acceptance_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    proof_was_fresh: Literal[True] = True
    compute_release_allowed: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    qualification_only: Literal[True] = True

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="accepted_external_runtime_termination", payload=self.signature_payload
        )

    @property
    def accepted_termination_sha256(self) -> str:
        return canonical_sha256(self)


def issue_accepted_external_runtime_termination(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> AcceptedExternalRuntimeTermination:
    """Issue the immutable acceptance of one fresh external termination proof."""

    pinned = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
    try:
        unsigned = AcceptedExternalRuntimeTermination(
            **scope,
            runtime_control_policy_sha256=pinned.policy_sha256,
            accepted_by_principal_id=pinned.principal_id,
            acceptance_key_id=pinned.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "accepted external runtime termination scope is invalid"
        ) from exc
    if _public_key_hex(private_key) != pinned.public_key_ed25519_hex or not pinned.active_at(
        unsigned.accepted_at
    ):
        raise QualificationVerificationError(
            "accepted external runtime termination signer is inactive"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return AcceptedExternalRuntimeTermination.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_accepted_external_runtime_termination(
    *,
    accepted: AcceptedExternalRuntimeTermination,
    receipt: ExternalRuntimeTerminationReceipt,
    challenge: ExternalTerminationAcceptanceChallenge,
    authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Historically verify one accepted external termination at its signing time."""

    try:
        accepted = AcceptedExternalRuntimeTermination.model_validate(
            accepted.model_dump(mode="python")
        )
        if (
            accepted.challenge_sha256 != challenge.challenge_sha256
            or accepted.external_runtime_termination_receipt_sha256
            != receipt.termination_receipt_sha256
            or accepted.exit_code != receipt.termination_evidence.exit_code
            or accepted.runtime_ended_at != receipt.termination_evidence.ended_at
            or accepted.result_content_sha256 != receipt.termination_evidence.result_content_sha256
            or accepted.proof_signed_at != receipt.signed_at
            or accepted.proof_expires_at != receipt.expires_at
        ):
            raise QualificationVerificationError(
                "accepted external runtime termination does not bind its exact proof"
            )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "accepted external runtime termination failed closed revalidation"
        ) from exc
    authority.verify_historical(
        kind="accepted_external_runtime_termination",
        payload=accepted.signature_payload,
        signature_ed25519_hex=accepted.signature_ed25519_hex,
        policy_sha256=accepted.runtime_control_policy_sha256,
        principal_id=accepted.accepted_by_principal_id,
        key_id=accepted.acceptance_key_id,
        signed_at=accepted.accepted_at,
    )


def recompute_external_disposition(
    *,
    exit_code: int,
    deadline_exceeded: bool,
    required_artifacts_present: bool,
) -> Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]:
    """Mechanically recompute the terminal disposition from observed facts."""

    if deadline_exceeded:
        return "timeout"
    if exit_code != 0:
        return "process_failed"
    if not required_artifacts_present:
        return "invalid_output"
    return "process_succeeded"


class ExternalQualificationTerminalSubmission(ExecutionModel):
    """Bridge-signed post-quarantine output provenance bound to accepted termination.

    Signed only after independent CAS rehash of the quarantined outputs, while
    the bridge enrollment key covers the hard deadline plus artifact grace.
    """

    schema_name: Literal["aletheia.external_qualification_terminal_submission"] = (
        "aletheia.external_qualification_terminal_submission"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_external_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...] = ()
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    submitted_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _submission_is_ordered(self) -> "ExternalQualificationTerminalSubmission":
        _require_utc(self.submitted_at, "external terminal submission submitted_at")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def terminal_submission_sha256(self) -> str:
        return canonical_sha256(self)


def issue_external_qualification_terminal_submission(
    *,
    bridge_pin: QualificationAuthorityPin,
    private_key: bytes,
    expected_disposition: (
        Literal["process_succeeded", "process_failed", "invalid_output", "timeout"] | None
    ) = None,
    **scope: object,
) -> ExternalQualificationTerminalSubmission:
    """Issue one bridge-signed terminal submission with mechanical disposition."""

    pinned = QualificationAuthorityPin.model_validate(bridge_pin.model_dump(mode="python"))
    try:
        unsigned = ExternalQualificationTerminalSubmission(
            **scope, signing_key_id=pinned.key_id, signature_ed25519_hex="0" * 128
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external qualification terminal submission scope is invalid"
        ) from exc
    if _public_key_hex(private_key) != pinned.public_key_ed25519_hex or not pinned.active_at(
        unsigned.submitted_at
    ):
        raise QualificationVerificationError(
            "external qualification terminal submission signer is inactive"
        )
    if expected_disposition is not None and unsigned.disposition != expected_disposition:
        raise QualificationVerificationError(
            "external terminal submission disposition differs from observed facts"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalQualificationTerminalSubmission.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


class VerifiedExternalQualificationTerminalSubmission(ExecutionModel):
    terminal_submission_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    verified_at: AwareDatetime


def verify_external_qualification_terminal_submission(
    *,
    submission: ExternalQualificationTerminalSubmission,
    accepted_termination: AcceptedExternalRuntimeTermination,
    bridge_authority: ExternalBridgeAuthority,
    observed_at: datetime,
) -> VerifiedExternalQualificationTerminalSubmission:
    """Fresh-verify one external terminal submission against its acceptance."""

    try:
        submission = ExternalQualificationTerminalSubmission.model_validate(
            submission.model_dump(mode="python")
        )
        if (
            submission.accepted_external_runtime_termination_sha256
            != accepted_termination.accepted_termination_sha256
            or submission.attempt_id != accepted_termination.attempt_id
            or submission.fencing_epoch != accepted_termination.fencing_epoch
            or submission.lease_token_sha256 != accepted_termination.lease_token_sha256
        ):
            raise QualificationVerificationError(
                "external terminal submission does not bind its accepted termination"
            )
        if submission.bridge_manifest_sha256 != bridge_authority.manifest.manifest_sha256:
            raise QualificationVerificationError(
                "external terminal submission belongs to another bridge authority"
            )
        _require_utc(observed_at, "external terminal submission observed_at")
        if not (
            accepted_termination.accepted_at
            <= submission.submitted_at
            < accepted_termination.artifact_submission_deadline
        ):
            raise QualificationVerificationError(
                "external terminal submission misses its acceptance window"
            )
        verify_external_bridge_signature(
            authority=bridge_authority,
            signing_key_id=submission.signing_key_id,
            message=submission.signature_message,
            signature_ed25519_hex=submission.signature_ed25519_hex,
            signed_at=submission.submitted_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external qualification terminal submission failed closed revalidation"
        ) from exc
    return VerifiedExternalQualificationTerminalSubmission(
        terminal_submission_sha256=submission.terminal_submission_sha256,
        accepted_termination_sha256=accepted_termination.accepted_termination_sha256,
        execution_id=submission.execution_id,
        attempt_id=submission.attempt_id,
        disposition=submission.disposition,
        verified_at=submission.submitted_at,
    )


class AcceptedExternalQualificationTerminalSubmission(ExecutionModel):
    """Immutable runtime-control acceptance of bridge-signed artifact provenance.

    This is the qualification-terminal outbox payload for nodeless attempts.
    """

    schema_name: Literal["aletheia.accepted_external_qualification_terminal_submission"] = (
        "aletheia.accepted_external_qualification_terminal_submission"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_submission_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_external_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...]
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    bridge_submitted_at: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    accepted_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    acceptance_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="accepted_external_qualification_terminal_submission",
            payload=self.signature_payload,
        )

    @property
    def terminal_authority_sha256(self) -> str:
        return canonical_sha256(self)


def issue_accepted_external_qualification_terminal_submission(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> AcceptedExternalQualificationTerminalSubmission:
    """Issue the immutable terminal acceptance that the outbox publishes."""

    pinned = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
    try:
        unsigned = AcceptedExternalQualificationTerminalSubmission(
            **scope,
            runtime_control_policy_sha256=pinned.policy_sha256,
            accepted_by_principal_id=pinned.principal_id,
            acceptance_key_id=pinned.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "accepted external qualification terminal submission scope is invalid"
        ) from exc
    if _public_key_hex(private_key) != pinned.public_key_ed25519_hex or not pinned.active_at(
        unsigned.accepted_at
    ):
        raise QualificationVerificationError(
            "accepted external qualification terminal submission signer is inactive"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return AcceptedExternalQualificationTerminalSubmission.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_accepted_external_qualification_terminal_submission(
    *,
    accepted: AcceptedExternalQualificationTerminalSubmission,
    submission: ExternalQualificationTerminalSubmission,
    authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Historically verify one accepted external terminal submission."""

    try:
        accepted = AcceptedExternalQualificationTerminalSubmission.model_validate(
            accepted.model_dump(mode="python")
        )
        if (
            accepted.terminal_submission_sha256 != submission.terminal_submission_sha256
            or accepted.bridge_manifest_sha256 != submission.bridge_manifest_sha256
            or accepted.disposition != submission.disposition
            or accepted.bridge_submitted_at != submission.submitted_at
            or accepted.artifact_manifest_sha256 != submission.artifact_manifest_sha256
            or accepted.output_tree_sha256 != submission.output_tree_sha256
            or accepted.artifact_verified_receipt_sha256s
            != submission.artifact_verified_receipt_sha256s
            or accepted.accepted_external_runtime_termination_sha256
            != submission.accepted_external_runtime_termination_sha256
        ):
            raise QualificationVerificationError(
                "accepted external terminal submission does not bind its exact submission"
            )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "accepted external qualification terminal submission failed closed revalidation"
        ) from exc
    authority.verify_historical(
        kind="accepted_external_qualification_terminal_submission",
        payload=accepted.signature_payload,
        signature_ed25519_hex=accepted.signature_ed25519_hex,
        policy_sha256=accepted.runtime_control_policy_sha256,
        principal_id=accepted.accepted_by_principal_id,
        key_id=accepted.acceptance_key_id,
        signed_at=accepted.accepted_at,
    )


class ExternalQualificationTerminalDeadlineExpiration(ExecutionModel):
    """Pre-signed conditional failure activated by DB time after artifact grace.

    Intentionally contains no artifact manifest or verification-receipt fields:
    their absence is the condition later adjudicated.  Signed while the
    runtime-control pin is live; activates only at the exact artifact deadline
    once the transaction proves no terminal-submission acceptance exists.
    """

    schema_name: Literal["aletheia.external_qualification_terminal_deadline_expiration"] = (
        "aletheia.external_qualification_terminal_deadline_expiration"
    )
    schema_version: Literal[2] = EXTERNAL_BRIDGE_CONTRACT_SCHEMA_VERSION
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_termination_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_runtime_termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_external_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_ended_at: AwareDatetime
    exit_code: int = Field(ge=0, le=255)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    accepted_runtime_termination_at: AwareDatetime
    authorized_at: AwareDatetime
    expired_at: AwareDatetime
    reason: Literal["artifact_submission_deadline_expired"] = "artifact_submission_deadline_expired"
    disposition: Literal["invalid_output"] = "invalid_output"
    retryable: Literal[False] = False
    conditional_on_terminal_submission_absence: Literal[True] = True
    database_time_activation_required: Literal[True] = True
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    adjudicated_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    adjudication_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _expiration_activates_at_its_deadline(
        self,
    ) -> "ExternalQualificationTerminalDeadlineExpiration":
        _require_utc(self.expired_at, "external terminal deadline expiration expired_at")
        if self.expired_at != self.artifact_submission_deadline:
            raise ValueError(
                "external terminal deadline expiration must activate at the artifact deadline"
            )
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="external_qualification_terminal_deadline_expiration",
            payload=self.signature_payload,
        )

    @property
    def expiration_sha256(self) -> str:
        return canonical_sha256(self)


def issue_external_qualification_terminal_deadline_expiration(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> ExternalQualificationTerminalDeadlineExpiration:
    """Pre-sign one conditional external terminal failure for later activation."""

    pinned = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))
    try:
        unsigned = ExternalQualificationTerminalDeadlineExpiration(
            **scope,
            runtime_control_policy_sha256=pinned.policy_sha256,
            adjudicated_by_principal_id=pinned.principal_id,
            adjudication_key_id=pinned.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external qualification terminal deadline expiration scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pinned.public_key_ed25519_hex
        or not pinned.active_at(unsigned.authorized_at)
        or unsigned.expired_at > pinned.active_until
    ):
        raise QualificationVerificationError(
            "external qualification terminal deadline expiration signer is inactive"
        )
    signature = _sign(unsigned.signature_message, private_key)
    return ExternalQualificationTerminalDeadlineExpiration.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature}).model_dump(mode="python")
    )


def verify_external_qualification_terminal_deadline_expiration(
    *,
    expiration: ExternalQualificationTerminalDeadlineExpiration,
    accepted_termination: AcceptedExternalRuntimeTermination,
    authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Historically verify one external deadline expiration at its signing time."""

    try:
        expiration = ExternalQualificationTerminalDeadlineExpiration.model_validate(
            expiration.model_dump(mode="python")
        )
        if (
            expiration.accepted_external_runtime_termination_sha256
            != accepted_termination.accepted_termination_sha256
            or expiration.artifact_submission_deadline
            != accepted_termination.artifact_submission_deadline
            or expiration.exit_code != accepted_termination.exit_code
            or expiration.runtime_ended_at != accepted_termination.runtime_ended_at
        ):
            raise QualificationVerificationError(
                "external terminal deadline expiration does not bind its acceptance"
            )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "external qualification terminal deadline expiration failed closed revalidation"
        ) from exc
    authority.verify_historical(
        kind="external_qualification_terminal_deadline_expiration",
        payload=expiration.signature_payload,
        signature_ed25519_hex=expiration.signature_ed25519_hex,
        policy_sha256=expiration.runtime_control_policy_sha256,
        principal_id=expiration.adjudicated_by_principal_id,
        key_id=expiration.adjudication_key_id,
        signed_at=expiration.authorized_at,
    )
