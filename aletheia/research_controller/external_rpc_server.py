"""Fail-closed server side of the controller worker's operation-closed RPC protocol.

The worker-side client proves the endpoint, peer, receipt, and typed result.  This module closes the
other half of that boundary: one externally supervised service accepts only its frozen controller,
worker, operation set, and canonical payload models, then signs a transport receipt with a key that
never enters the worker.  The receipt authenticates transport provenance only; domain services
remain responsible for durable first-writer custody and every scientific/database signature.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, Field

from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionCampaignRegistrationReceipt,
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.observations.scientific_bridge import (
    AdmissionIssuanceChallenge,
    CommittedObservationValidationReceipt,
    ObservationAdmissionDecision,
    ObservationValidationReceipt,
    RawRunEnvelope,
    ScientificExecutionAuthorization,
    ValidationIssuanceChallenge,
)
from aletheia.observations.service import (
    AdmissionChallengeRegistrationReceipt,
    ValidationChallengeRegistrationReceipt,
    ValidationCommitReceipt,
)
from aletheia.observations.store import ContinuationReceiptWrite, ProtocolCompilationWrite
from aletheia.research_controller.action_proposals import SubmittedActionProposal
from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerTickPlan,
    ControllerWakeup,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCRequest,
    ControllerWorkerRPCResponse,
    ControllerWorkerRPCServicePin,
    RawRunLoadResult,
    ValidationCampaignResult,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BLOCKER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"
_CONTROLLER_PATTERN = r"^rctl_[0-9a-f]{32}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"


class ControllerWorkerRPCServerError(RuntimeError):
    """A request, handler result, or receipt-signing invariant failed closed."""


class ControllerWorkerRPCRequestRejected(ControllerWorkerRPCServerError):
    """Untrusted request bytes did not match the deployed service boundary."""


class ControllerWorkerRPCServiceBlocked(ControllerWorkerRPCServerError):
    """A domain service produced one canonical, non-retryable blocker set."""

    def __init__(self, blocker_codes: tuple[str, ...]) -> None:
        if (
            not blocker_codes
            or len(blocker_codes) > 64
            or blocker_codes != tuple(sorted(set(blocker_codes)))
            or any(re.fullmatch(_BLOCKER_PATTERN, item) is None for item in blocker_codes)
        ):
            raise ValueError("RPC service blockers must be nonempty and canonical")
        self.blocker_codes = blocker_codes
        super().__init__(",".join(blocker_codes))


class ControllerTickRPCPayload(ControllerModel):
    wakeup: ControllerWakeup
    projection: ControllerRecoveryProjection
    plan: ControllerTickPlan


class ScientificExecutionRegistrationRPCPayload(ControllerModel):
    authorization: ScientificExecutionAuthorization


class ScientificExecutionCampaignRegistrationRPCPayload(ControllerModel):
    authorizations: tuple[ScientificExecutionAuthorization, ...] = Field(
        min_length=2,
        max_length=100,
    )


class ScientificSlotLookupRPCPayload(ControllerModel):
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")


class RawRunRPCPayload(ControllerModel):
    raw_run: RawRunEnvelope


class ValidationReceiptIssuanceRPCPayload(ControllerModel):
    raw_run: RawRunEnvelope
    validation_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    issuance_challenge: ValidationIssuanceChallenge


class ValidationChallengeIssuanceRPCPayload(ControllerModel):
    raw_run: RawRunEnvelope
    validation_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class ValidationCommitRPCPayload(ControllerModel):
    receipt: ObservationValidationReceipt


class AdmissionChallengeIssuanceRPCPayload(ControllerModel):
    committed_validation: CommittedObservationValidationReceipt


class AdmissionDecisionIssuanceRPCPayload(ControllerModel):
    committed_validation: CommittedObservationValidationReceipt
    issuance_challenge: AdmissionIssuanceChallenge


class AdmissionCommitRPCPayload(ControllerModel):
    decision: ObservationAdmissionDecision


_PayloadModel = type[ControllerModel]
_ResultModel = type[BaseModel]

_OPERATION_PAYLOAD_MODELS: dict[ControllerWorkerRPCOperation, _PayloadModel] = {
    ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL: ControllerTickRPCPayload,
    ControllerWorkerRPCOperation.COMPILE_PROTOCOL: ControllerTickRPCPayload,
    ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION: ControllerTickRPCPayload,
    ControllerWorkerRPCOperation.REGISTER_EXECUTION: ScientificExecutionRegistrationRPCPayload,
    ControllerWorkerRPCOperation.REGISTER_EXECUTION_CAMPAIGN: (
        ScientificExecutionCampaignRegistrationRPCPayload
    ),
    ControllerWorkerRPCOperation.LOAD_RAW_RUN: ScientificSlotLookupRPCPayload,
    ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN: RawRunRPCPayload,
    ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE: (
        ValidationChallengeIssuanceRPCPayload
    ),
    ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT: ValidationReceiptIssuanceRPCPayload,
    ControllerWorkerRPCOperation.COMMIT_VALIDATION: ValidationCommitRPCPayload,
    ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION: ScientificSlotLookupRPCPayload,
    ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE: (AdmissionChallengeIssuanceRPCPayload),
    ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION: AdmissionDecisionIssuanceRPCPayload,
    ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE: AdmissionCommitRPCPayload,
    ControllerWorkerRPCOperation.DERIVE_CONTINUATION: ControllerTickRPCPayload,
}

_OPERATION_RESULT_MODELS: dict[ControllerWorkerRPCOperation, _ResultModel] = {
    ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL: SubmittedActionProposal,
    ControllerWorkerRPCOperation.COMPILE_PROTOCOL: ProtocolCompilationWrite,
    ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION: ScientificExecutionAuthorization,
    ControllerWorkerRPCOperation.REGISTER_EXECUTION: (AtomicScientificExecutionRegistrationReceipt),
    ControllerWorkerRPCOperation.REGISTER_EXECUTION_CAMPAIGN: (
        AtomicScientificExecutionCampaignRegistrationReceipt
    ),
    ControllerWorkerRPCOperation.LOAD_RAW_RUN: RawRunLoadResult,
    ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN: ValidationCampaignResult,
    ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE: (
        ValidationChallengeRegistrationReceipt
    ),
    ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT: ObservationValidationReceipt,
    ControllerWorkerRPCOperation.COMMIT_VALIDATION: ValidationCommitReceipt,
    ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION: (CommittedObservationValidationReceipt),
    ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE: (AdmissionChallengeRegistrationReceipt),
    ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION: ObservationAdmissionDecision,
    ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE: AtomicObservationAdmissionReceipt,
    ControllerWorkerRPCOperation.DERIVE_CONTINUATION: ContinuationReceiptWrite,
}

_SIGNED_BLOCKER_OPERATIONS = frozenset(
    {
        ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,
        ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        ControllerWorkerRPCOperation.DERIVE_CONTINUATION,
    }
)

if (
    frozenset(_OPERATION_PAYLOAD_MODELS) != frozenset(ControllerWorkerRPCOperation)
    or frozenset(_OPERATION_RESULT_MODELS) != frozenset(ControllerWorkerRPCOperation)
    or not _SIGNED_BLOCKER_OPERATIONS.issubset(_OPERATION_PAYLOAD_MODELS)
):  # pragma: no cover - import-time closed-world invariant
    raise RuntimeError("controller worker RPC server operation contracts are not exhaustive")


class ControllerWorkerRPCOperationHandler(Protocol):
    """One operation-specific domain entry point supplied by a pinned service factory."""

    def __call__(self, payload: ControllerModel) -> BaseModel: ...


@dataclass(frozen=True)
class ControllerWorkerRPCHandlerBinding:
    operation: ControllerWorkerRPCOperation
    handler: ControllerWorkerRPCOperationHandler


class ControllerWorkerRPCHandlerSet:
    """Exhaustive operation-to-handler partition for exactly one service process."""

    def __init__(
        self,
        *,
        operations: tuple[ControllerWorkerRPCOperation, ...],
        bindings: tuple[ControllerWorkerRPCHandlerBinding, ...],
    ) -> None:
        expected = tuple(sorted(set(operations), key=lambda item: item.value))
        observed = tuple(sorted((item.operation for item in bindings), key=lambda item: item.value))
        if operations != expected or observed != expected or len(observed) != len(bindings):
            raise ValueError("RPC server handlers must exactly cover the canonical operation set")
        if any(not callable(item.handler) for item in bindings):
            raise TypeError("RPC server handler is not callable")
        self._handlers = {item.operation: item.handler for item in bindings}
        self.operations = expected

    def handler_for(
        self, operation: ControllerWorkerRPCOperation
    ) -> ControllerWorkerRPCOperationHandler:
        try:
            return self._handlers[operation]
        except KeyError as exc:  # pragma: no cover - constructor proves exhaustiveness
            raise ControllerWorkerRPCServerError("RPC operation lacks its exact handler") from exc


class ControllerWorkerRPCService:
    """Validate, dispatch, and receipt-sign one canonical request frame."""

    def __init__(
        self,
        *,
        pin: ControllerWorkerRPCServicePin,
        controller_id: str,
        controller_manifest_sha256: str,
        worker_process_principal_id: str,
        handlers: ControllerWorkerRPCHandlerSet,
        receipt_private_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            frozen_pin = ControllerWorkerRPCServicePin.model_validate(pin.model_dump(mode="python"))
            private_key = Ed25519PrivateKey.from_private_bytes(receipt_private_key)
            public_hex = (
                private_key.public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ControllerWorkerRPCServerError(
                "RPC service pin or receipt key is invalid"
            ) from exc
        if (
            handlers.operations != frozen_pin.operations
            or public_hex != frozen_pin.receipt_public_key_ed25519_hex
        ):
            raise ControllerWorkerRPCServerError(
                "RPC handlers or receipt key differ from the service pin"
            )
        if (
            re.fullmatch(_CONTROLLER_PATTERN, controller_id) is None
            or re.fullmatch(_SHA256_PATTERN, controller_manifest_sha256) is None
            or re.fullmatch(_PRINCIPAL_PATTERN, worker_process_principal_id) is None
        ):
            raise ControllerWorkerRPCServerError("RPC service deployment identity is invalid")
        self.pin = frozen_pin
        self._controller_id = controller_id
        self._controller_manifest_sha256 = controller_manifest_sha256
        self._worker_process_principal_id = worker_process_principal_id
        self._handlers = handlers
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _load_request(self, request_bytes: bytes) -> ControllerWorkerRPCRequest:
        if not request_bytes or len(request_bytes) > self.pin.max_request_bytes:
            raise ControllerWorkerRPCRequestRejected("RPC request violates its byte bound")
        try:
            request = ControllerWorkerRPCRequest.model_validate_json(request_bytes)
        except (TypeError, ValueError) as exc:
            raise ControllerWorkerRPCRequestRejected("RPC request is invalid") from exc
        if canonical_json_bytes(request) != request_bytes:
            raise ControllerWorkerRPCRequestRejected("RPC request is not canonical JSON")
        if (
            request.controller_id != self._controller_id
            or request.controller_manifest_sha256 != self._controller_manifest_sha256
            or request.worker_process_principal_id != self._worker_process_principal_id
            or request.service_id != self.pin.service_id
            or request.service_pin_sha256 != self.pin.pin_sha256
            or request.operation not in self.pin.operations
        ):
            raise ControllerWorkerRPCRequestRejected("RPC request differs from its deployment pin")
        return request

    @staticmethod
    def _load_payload(request: ControllerWorkerRPCRequest) -> ControllerModel:
        try:
            payload = _OPERATION_PAYLOAD_MODELS[request.operation].model_validate(request.payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerWorkerRPCRequestRejected("RPC operation payload is invalid") from exc
        if canonical_json_bytes(payload) != canonical_json_bytes(request.payload):
            raise ControllerWorkerRPCRequestRejected("RPC operation payload is not canonical")
        return payload

    def _sign_response(
        self,
        *,
        request: ControllerWorkerRPCRequest,
        result: BaseModel | None = None,
        blocker_codes: tuple[str, ...] = (),
    ) -> ControllerWorkerRPCResponse:
        signed_at = self._clock()
        if (
            signed_at.tzinfo is None
            or signed_at.utcoffset() is None
            or not self.pin.valid_from <= signed_at < self.pin.expires_at
        ):
            raise ControllerWorkerRPCServerError(
                "RPC service clock is outside the receipt-key interval"
            )
        disposition: Literal["succeeded", "blocked"] = "blocked" if blocker_codes else "succeeded"
        result_json = None if result is None else result.model_dump(mode="json")
        payload = {
            "request_sha256": request.request_sha256,
            "service_id": self.pin.service_id,
            "service_principal_id": self.pin.service_principal_id,
            "service_manifest_sha256": self.pin.service_manifest_sha256,
            "service_policy_sha256": self.pin.service_policy_sha256,
            "operation": request.operation,
            "disposition": disposition,
            "result": result_json,
            "result_sha256": None if result is None else canonical_sha256(result),
            "blocker_codes": blocker_codes,
            "signed_at": signed_at,
            "receipt_key_id": self.pin.receipt_key_id,
            "signature_ed25519_hex": "0" * 128,
        }
        try:
            unsigned = ControllerWorkerRPCResponse.model_validate(payload)
            payload["signature_ed25519_hex"] = self._private_key.sign(unsigned.signing_bytes).hex()
            return ControllerWorkerRPCResponse.model_validate(payload)
        except (TypeError, ValueError) as exc:  # pragma: no cover - closed construction
            raise ControllerWorkerRPCServerError("RPC response could not be signed") from exc

    def _encode_response(self, response: ControllerWorkerRPCResponse) -> bytes:
        encoded = canonical_json_bytes(response)
        if len(encoded) > self.pin.max_response_bytes:
            raise ControllerWorkerRPCServerError("RPC response exceeds its byte bound")
        return encoded

    def handle(self, request_bytes: bytes) -> bytes:
        """Return canonical response bytes, or reject without a signed response."""

        request = self._load_request(request_bytes)
        payload = self._load_payload(request)
        try:
            result = self._handlers.handler_for(request.operation)(payload)
        except ControllerWorkerRPCServiceBlocked as exc:
            if request.operation not in _SIGNED_BLOCKER_OPERATIONS:
                raise ControllerWorkerRPCServerError(
                    "RPC operation cannot convert a service failure into a signed blocker"
                ) from exc
            response = self._sign_response(request=request, blocker_codes=exc.blocker_codes)
            return self._encode_response(response)
        if inspect.isawaitable(result):
            raise ControllerWorkerRPCServerError("RPC service handler must be synchronous")
        expected_type = _OPERATION_RESULT_MODELS[request.operation]
        if type(result) is not expected_type:
            raise ControllerWorkerRPCServerError("RPC service returned another result type")
        try:
            frozen = expected_type.model_validate(result.model_dump(mode="python"))
        except (
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:  # pragma: no cover - exact type above
            raise ControllerWorkerRPCServerError("RPC service returned an invalid result") from exc
        if frozen != result:
            raise ControllerWorkerRPCServerError("RPC service result changed during validation")
        return self._encode_response(self._sign_response(request=request, result=frozen))


__all__ = [
    "AdmissionChallengeIssuanceRPCPayload",
    "AdmissionCommitRPCPayload",
    "AdmissionDecisionIssuanceRPCPayload",
    "ControllerTickRPCPayload",
    "ControllerWorkerRPCHandlerBinding",
    "ControllerWorkerRPCHandlerSet",
    "ControllerWorkerRPCOperationHandler",
    "ControllerWorkerRPCRequestRejected",
    "ControllerWorkerRPCServerError",
    "ControllerWorkerRPCService",
    "ControllerWorkerRPCServiceBlocked",
    "RawRunRPCPayload",
    "ScientificExecutionRegistrationRPCPayload",
    "ScientificExecutionCampaignRegistrationRPCPayload",
    "ScientificSlotLookupRPCPayload",
    "ValidationChallengeIssuanceRPCPayload",
    "ValidationCommitRPCPayload",
    "ValidationReceiptIssuanceRPCPayload",
]
