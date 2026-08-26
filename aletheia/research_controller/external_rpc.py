"""Pinned, receipt-authenticated Unix RPC clients for controller step services.

The controller worker owns no scientific signing key.  Each facade below exposes one narrow port
used by a concrete step adapter, while the shared transport verifies Linux peer credentials,
socket custody, canonical request/response bytes, and an Ed25519 service receipt before returning a
typed result.  A transport receipt authenticates the deployed service; it never replaces the
domain signatures and database/Kernel checks performed by the adapters themselves.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import struct
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.execution_registration import (
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
from aletheia.research_controller.action_proposals import (
    ActionProposalBlocked,
    SubmittedActionProposal,
)
from aletheia.research_controller.continuation_step import ContinuationAssessmentUnavailable
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.step_executor import ControllerStepAuthorityBinding
from aletheia.research_controller.protocol_compilation_step import (
    ProtocolCompilationUnavailable,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_PUBLIC_KEY_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_RPC_KEY_PATTERN = r"^rpck_[0-9a-f]{32}$"
_RPC_SERVICE_PATTERN = r"^rpcs_[0-9a-f]{32}$"
_RPC_REQUEST_PATTERN = r"^rpcq_[0-9a-f]{32}$"
_RPC_RESPONSE_PATTERN = r"^rpcr_[0-9a-f]{32}$"
_BlockerCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")]


class ControllerWorkerRPCError(RuntimeError):
    """The RPC endpoint, transport receipt, or typed result failed closed."""


class ControllerWorkerRPCBlocked(ControllerWorkerRPCError):
    """A signed endpoint returned one canonical non-retryable blocker set."""

    def __init__(self, blocker_codes: tuple[str, ...]) -> None:
        self.blocker_codes = blocker_codes
        super().__init__(",".join(blocker_codes))


class ControllerWorkerRPCOperation(str, Enum):
    """Closed operation vocabulary; there is no generic model callback."""

    MATERIALIZE_ACTION_PROPOSAL = "materialize_action_proposal"
    COMPILE_PROTOCOL = "compile_protocol"
    ISSUE_EXECUTION_AUTHORIZATION = "issue_execution_authorization"
    REGISTER_EXECUTION = "register_execution"
    LOAD_RAW_RUN = "load_raw_run"
    PREPARE_VALIDATION_CAMPAIGN = "prepare_validation_campaign"
    ISSUE_VALIDATION_CHALLENGE = "issue_validation_challenge"
    ISSUE_VALIDATION_RECEIPT = "issue_validation_receipt"
    COMMIT_VALIDATION = "commit_validation"
    LOAD_COMMITTED_VALIDATION = "load_committed_validation"
    ISSUE_ADMISSION_CHALLENGE = "issue_admission_challenge"
    ISSUE_ADMISSION_DECISION = "issue_admission_decision"
    COMMIT_AND_INCORPORATE = "commit_and_incorporate"
    DERIVE_CONTINUATION = "derive_continuation"


def _canonical_absolute_socket_path(value: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not candidate.is_absolute()
        or value != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError("controller worker RPC socket path must be canonical and absolute")
    return candidate


def controller_worker_rpc_key_id(public_key_ed25519_hex: str) -> str:
    """Derive the immutable transport-receipt key identity."""

    try:
        public_key = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("RPC receipt public key must be hexadecimal") from exc
    if len(public_key) != 32:
        raise ValueError("RPC receipt public key must contain 32 raw bytes")
    return f"rpck_{hashlib.sha256(public_key).hexdigest()[:32]}"


class ControllerWorkerRPCServicePin(ControllerModel):
    """Exact Unix endpoint, peer, receipt key, and authority closure for one service."""

    schema_name: Literal["aletheia.controller_worker_rpc_service_pin"] = (
        "aletheia.controller_worker_rpc_service_pin"
    )
    schema_version: Literal[1] = 1
    service_id: str | None = Field(default=None, pattern=_RPC_SERVICE_PATTERN)
    service_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    service_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    operations: tuple[ControllerWorkerRPCOperation, ...] = Field(min_length=1, max_length=14)
    authority_binding_sha256s: tuple[str, ...] = Field(max_length=8)
    socket_path: str
    socket_owner_uid: int = Field(ge=0)
    socket_group_gid: int = Field(ge=0)
    socket_mode: int = Field(ge=0, le=0o777)
    peer_uid: int = Field(ge=0)
    peer_gid: int = Field(ge=0)
    receipt_key_id: str = Field(pattern=_RPC_KEY_PATTERN)
    receipt_public_key_ed25519_hex: str = Field(pattern=_PUBLIC_KEY_PATTERN)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    connect_timeout_seconds: float = Field(ge=0.05, le=120.0)
    max_request_bytes: int = Field(ge=1_024, le=16 * 1024 * 1024)
    max_response_bytes: int = Field(ge=1_024, le=16 * 1024 * 1024)
    linux_peer_credentials_required: Literal[True] = True
    private_key_loaded_in_worker: Literal[False] = False

    @model_validator(mode="after")
    def _pin_is_canonical(self) -> "ControllerWorkerRPCServicePin":
        _canonical_absolute_socket_path(self.socket_path)
        if self.socket_mode not in {0o600, 0o660}:
            raise ValueError("controller worker RPC socket mode must be 0600 or 0660")
        if self.operations != tuple(sorted(set(self.operations), key=lambda item: item.value)):
            raise ValueError("controller worker RPC operations must be unique and canonical")
        if self.authority_binding_sha256s != tuple(sorted(set(self.authority_binding_sha256s))):
            raise ValueError("RPC authority binding hashes must be unique and canonical")
        if self.receipt_key_id != controller_worker_rpc_key_id(self.receipt_public_key_ed25519_hex):
            raise ValueError("RPC receipt key id differs from its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("RPC service receipt-key interval is empty")
        expected_id = f"rpcs_{self.pin_sha256[:32]}"
        if self.service_id is not None and self.service_id != expected_id:
            raise ValueError("RPC service id differs from its pin")
        object.__setattr__(self, "service_id", expected_id)
        return self

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"service_id"}))


class ControllerWorkerRPCRequest(ControllerModel):
    """Deterministic, idempotent request envelope bound to one pinned service."""

    schema_name: Literal["aletheia.controller_worker_rpc_request"] = (
        "aletheia.controller_worker_rpc_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=_RPC_REQUEST_PATTERN)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    worker_process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    service_id: str = Field(pattern=_RPC_SERVICE_PATTERN)
    service_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    operation: ControllerWorkerRPCOperation
    payload: dict[str, object]

    @model_validator(mode="after")
    def _request_identity_is_exact(self) -> "ControllerWorkerRPCRequest":
        expected = f"rpcq_{self.request_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected:
            raise ValueError("controller worker RPC request id differs from its payload")
        object.__setattr__(self, "request_id", expected)
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))


class ControllerWorkerRPCResponse(ControllerModel):
    """Service-signed response over the exact request and canonical result."""

    schema_name: Literal["aletheia.controller_worker_rpc_response"] = (
        "aletheia.controller_worker_rpc_response"
    )
    schema_version: Literal[1] = 1
    response_id: str | None = Field(default=None, pattern=_RPC_RESPONSE_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_id: str = Field(pattern=_RPC_SERVICE_PATTERN)
    service_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    service_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    operation: ControllerWorkerRPCOperation
    disposition: Literal["succeeded", "blocked"] = "succeeded"
    result: dict[str, object] | None = None
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    blocker_codes: tuple[_BlockerCode, ...] = Field(default=(), max_length=64)
    signed_at: AwareDatetime
    receipt_key_id: str = Field(pattern=_RPC_KEY_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _response_identity_is_exact(self) -> "ControllerWorkerRPCResponse":
        if self.disposition == "succeeded":
            if (
                self.result is None
                or self.result_sha256 != canonical_sha256(self.result)
                or self.blocker_codes
            ):
                raise ValueError("successful RPC response has invalid result custody")
        elif (
            self.result is not None
            or self.result_sha256 is not None
            or not self.blocker_codes
            or self.blocker_codes != tuple(sorted(set(self.blocker_codes)))
        ):
            raise ValueError("blocked RPC response has invalid blocker custody")
        expected = f"rpcr_{self.response_sha256[:32]}"
        if self.response_id is not None and self.response_id != expected:
            raise ValueError("controller worker RPC response id differs from its payload")
        object.__setattr__(self, "response_id", expected)
        return self

    @property
    def signing_bytes(self) -> bytes:
        return canonical_json_bytes(
            self.model_dump(
                mode="json",
                exclude={"response_id", "signature_ed25519_hex"},
            )
        )

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256(self.signing_bytes).hexdigest()


class ControllerWorkerRPCTransport(Protocol):
    """Exchange one canonical request for one canonical response."""

    def exchange(self, pin: ControllerWorkerRPCServicePin, request_bytes: bytes) -> bytes: ...


class LinuxUnixSocketRPCTransport:
    """One-request Unix stream transport with exact socket and Linux peer checks."""

    @staticmethod
    def _socket_identity(pin: ControllerWorkerRPCServicePin) -> tuple[int, ...]:
        path = _canonical_absolute_socket_path(pin.socket_path)
        try:
            if path.resolve(strict=True) != path:
                raise ControllerWorkerRPCError("RPC socket path traverses a symlink")
            observed = os.lstat(path)
        except OSError as exc:
            raise ControllerWorkerRPCError("RPC socket is unavailable") from exc
        if (
            not stat.S_ISSOCK(observed.st_mode)
            or observed.st_uid != pin.socket_owner_uid
            or observed.st_gid != pin.socket_group_gid
            or stat.S_IMODE(observed.st_mode) != pin.socket_mode
        ):
            raise ControllerWorkerRPCError("RPC socket custody differs from its deployment pin")
        return (
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            observed.st_gid,
            stat.S_IMODE(observed.st_mode),
        )

    @staticmethod
    def _verify_peer(connection: socket.socket, pin: ControllerWorkerRPCServicePin) -> None:
        if not pin.linux_peer_credentials_required:
            return
        if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
            raise ControllerWorkerRPCError("Linux SO_PEERCRED is required by the RPC pin")
        try:
            raw = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, gid = struct.unpack("3i", raw)
        except (OSError, struct.error) as exc:
            raise ControllerWorkerRPCError("RPC peer credentials could not be observed") from exc
        if uid != pin.peer_uid or gid != pin.peer_gid:
            raise ControllerWorkerRPCError("RPC peer credentials differ from the deployment pin")

    def exchange(self, pin: ControllerWorkerRPCServicePin, request_bytes: bytes) -> bytes:
        before = self._socket_identity(pin)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(pin.connect_timeout_seconds)
        try:
            connection.connect(pin.socket_path)
            self._verify_peer(connection, pin)
            connection.sendall(request_bytes + b"\n")
            connection.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = connection.recv(min(65_536, pin.max_response_bytes + 2 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > pin.max_response_bytes + 1:
                    raise ControllerWorkerRPCError("RPC response exceeds its byte bound")
        except (OSError, TimeoutError) as exc:
            raise ControllerWorkerRPCError("RPC transport failed closed") from exc
        finally:
            connection.close()
        if before != self._socket_identity(pin):
            raise ControllerWorkerRPCError("RPC socket identity changed during the exchange")
        framed = b"".join(chunks)
        if not framed.endswith(b"\n") or b"\n" in framed[:-1]:
            raise ControllerWorkerRPCError("RPC response is not one canonical frame")
        response = framed[:-1]
        if not response:
            raise ControllerWorkerRPCError("RPC response is empty")
        return response


_ResultModel = TypeVar("_ResultModel", bound=ControllerModel)


class ControllerWorkerRPCClient:
    """Verify one endpoint's identity and return only a requested closed result model."""

    def __init__(
        self,
        *,
        pin: ControllerWorkerRPCServicePin,
        controller_id: str,
        controller_manifest_sha256: str,
        worker_process_principal_id: str,
        transport: ControllerWorkerRPCTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.pin = ControllerWorkerRPCServicePin.model_validate(pin.model_dump(mode="python"))
        self._controller_id = controller_id
        self._controller_manifest_sha256 = controller_manifest_sha256
        self._worker_process_principal_id = worker_process_principal_id
        self._transport = transport or LinuxUnixSocketRPCTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def call(
        self,
        operation: ControllerWorkerRPCOperation,
        *,
        payload: dict[str, object],
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        if operation not in self.pin.operations:
            raise ControllerWorkerRPCError("RPC operation is absent from the service pin")
        request = ControllerWorkerRPCRequest(
            controller_id=self._controller_id,
            controller_manifest_sha256=self._controller_manifest_sha256,
            worker_process_principal_id=self._worker_process_principal_id,
            service_id=self.pin.service_id,
            service_pin_sha256=self.pin.pin_sha256,
            operation=operation,
            payload=payload,
        )
        try:
            request_bytes = canonical_json_bytes(request)
            if len(request_bytes) > self.pin.max_request_bytes:
                raise ControllerWorkerRPCError("RPC request exceeds its byte bound")
            response_bytes = self._transport.exchange(self.pin, request_bytes)
            response = ControllerWorkerRPCResponse.model_validate_json(response_bytes)
            if canonical_json_bytes(response) != response_bytes:
                raise ControllerWorkerRPCError("RPC response bytes are not canonical JSON")
            now = self._clock()
            if (
                response.request_sha256 != request.request_sha256
                or response.operation is not operation
                or response.service_id != self.pin.service_id
                or response.service_principal_id != self.pin.service_principal_id
                or response.service_manifest_sha256 != self.pin.service_manifest_sha256
                or response.service_policy_sha256 != self.pin.service_policy_sha256
                or response.receipt_key_id != self.pin.receipt_key_id
                or not self.pin.valid_from <= response.signed_at < self.pin.expires_at
                or not self.pin.valid_from <= now < self.pin.expires_at
                or response.signed_at > now
            ):
                raise ControllerWorkerRPCError("RPC response differs from its service/request pin")
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(self.pin.receipt_public_key_ed25519_hex)
            ).verify(bytes.fromhex(response.signature_ed25519_hex), response.signing_bytes)
            if response.disposition == "blocked":
                raise ControllerWorkerRPCBlocked(response.blocker_codes)
            if response.result is None or response.result_sha256 is None:  # pragma: no cover
                raise ControllerWorkerRPCError("successful RPC response lost its result")
            result = result_type.model_validate(response.result)
            if canonical_sha256(result) != response.result_sha256:
                raise ControllerWorkerRPCError("typed RPC result differs from its signed hash")
            return result
        except ControllerWorkerRPCError:
            raise
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise ControllerWorkerRPCError("RPC response verification failed closed") from exc


def _binding(
    value: ControllerStepAuthorityBinding,
    *,
    pin: ControllerWorkerRPCServicePin,
) -> ControllerStepAuthorityBinding:
    frozen = ControllerStepAuthorityBinding.model_validate(value.model_dump(mode="python"))
    if frozen.binding_sha256 not in pin.authority_binding_sha256s:
        raise ValueError("RPC service pin does not contain its step authority binding")
    return frozen


def _json(value: ControllerModel) -> dict[str, object]:
    return value.model_dump(mode="json")


class RPCActionProposalMaterialization:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def materialize_and_submit(self, *, wakeup, projection, plan) -> SubmittedActionProposal:
        try:
            return self._client.call(
                ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,
                payload={
                    "wakeup": _json(wakeup),
                    "projection": _json(projection),
                    "plan": _json(plan),
                },
                result_type=SubmittedActionProposal,
            )
        except ControllerWorkerRPCBlocked as exc:
            raise ActionProposalBlocked(exc.blocker_codes) from exc


class RPCProtocolCompilationMaterialization:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def compile_and_register(self, *, wakeup, projection, plan) -> ProtocolCompilationWrite:
        try:
            return self._client.call(
                ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                payload={
                    "wakeup": _json(wakeup),
                    "projection": _json(projection),
                    "plan": _json(plan),
                },
                result_type=ProtocolCompilationWrite,
            )
        except ControllerWorkerRPCBlocked as exc:
            raise ProtocolCompilationUnavailable(exc.blocker_codes) from exc


class RPCScientificExecutionAuthorizationIssuer:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def issue_scientific_execution_authorization(
        self, *, wakeup, projection, plan
    ) -> ScientificExecutionAuthorization:
        return self._client.call(
            ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,
            payload={"wakeup": _json(wakeup), "projection": _json(projection), "plan": _json(plan)},
            result_type=ScientificExecutionAuthorization,
        )


class RPCScientificExecutionRegistrar:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def register_and_reserve(
        self, authorization: ScientificExecutionAuthorization
    ) -> AtomicScientificExecutionRegistrationReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.REGISTER_EXECUTION,
            payload={"authorization": _json(authorization)},
            result_type=AtomicScientificExecutionRegistrationReceipt,
        )


class RPCRawRunEnvelopeSource:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def load_raw_run(self, *, quest_id, action_sha256, scientific_slot_id) -> RawRunEnvelope:
        return self._client.call(
            ControllerWorkerRPCOperation.LOAD_RAW_RUN,
            payload={
                "quest_id": quest_id,
                "action_sha256": action_sha256,
                "scientific_slot_id": scientific_slot_id,
            },
            result_type=RawRunEnvelope,
        )


class _ValidationCampaignResult(ControllerModel):
    validation_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class RPCIndependentObservationValidator:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def prepare_validation_campaign(self, *, raw_run: RawRunEnvelope) -> str | None:
        result = self._client.call(
            ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
            payload={"raw_run": _json(raw_run)},
            result_type=_ValidationCampaignResult,
        )
        return result.validation_campaign_sha256

    def issue_validation_receipt(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
        issuance_challenge: ValidationIssuanceChallenge,
    ) -> ObservationValidationReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
            payload={
                "raw_run": _json(raw_run),
                "validation_campaign_sha256": validation_campaign_sha256,
                "issuance_challenge": _json(issuance_challenge),
            },
            result_type=ObservationValidationReceipt,
        )


class RPCDatabaseObservationBridge:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def issue_validation_challenge(
        self, *, raw_run: RawRunEnvelope, validation_campaign_sha256: str | None
    ) -> ValidationChallengeRegistrationReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
            payload={
                "raw_run": _json(raw_run),
                "validation_campaign_sha256": validation_campaign_sha256,
            },
            result_type=ValidationChallengeRegistrationReceipt,
        )

    def commit_validation(self, receipt: ObservationValidationReceipt) -> ValidationCommitReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.COMMIT_VALIDATION,
            payload={"receipt": _json(receipt)},
            result_type=ValidationCommitReceipt,
        )

    def issue_admission_challenge(
        self, committed_validation: CommittedObservationValidationReceipt
    ) -> AdmissionChallengeRegistrationReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
            payload={"committed_validation": _json(committed_validation)},
            result_type=AdmissionChallengeRegistrationReceipt,
        )


class RPCCommittedValidationSource:
    def __init__(
        self,
        client: ControllerWorkerRPCClient,
        bindings: tuple[ControllerStepAuthorityBinding, ...],
    ):
        self._client = client
        self.authority_bindings = tuple(_binding(item, pin=client.pin) for item in bindings)

    def load_committed_validation(
        self, *, quest_id, action_sha256, scientific_slot_id
    ) -> CommittedObservationValidationReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION,
            payload={
                "quest_id": quest_id,
                "action_sha256": action_sha256,
                "scientific_slot_id": scientific_slot_id,
            },
            result_type=CommittedObservationValidationReceipt,
        )


class RPCIndependentObservationAdmission:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def issue_admission_decision(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
        issuance_challenge: AdmissionIssuanceChallenge,
    ) -> ObservationAdmissionDecision:
        return self._client.call(
            ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,
            payload={
                "committed_validation": _json(committed_validation),
                "issuance_challenge": _json(issuance_challenge),
            },
            result_type=ObservationAdmissionDecision,
        )


class RPCAtomicObservationAdmission:
    def __init__(
        self,
        client: ControllerWorkerRPCClient,
        *,
        database_binding: ControllerStepAuthorityBinding,
        kernel_binding: ControllerStepAuthorityBinding,
        admission_binding: ControllerStepAuthorityBinding,
    ) -> None:
        self._client = client
        self.database_authority_binding = _binding(database_binding, pin=client.pin)
        self.kernel_authority_binding = _binding(kernel_binding, pin=client.pin)
        self.admission_authority_binding = _binding(admission_binding, pin=client.pin)

    def commit_and_incorporate(
        self, decision: ObservationAdmissionDecision
    ) -> AtomicObservationAdmissionReceipt:
        return self._client.call(
            ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,
            payload={"decision": _json(decision)},
            result_type=AtomicObservationAdmissionReceipt,
        )


class RPCContinuationMaterialization:
    def __init__(self, client: ControllerWorkerRPCClient, binding: ControllerStepAuthorityBinding):
        self._client = client
        self.authority_binding = _binding(binding, pin=client.pin)

    def derive_and_register(self, *, wakeup, projection, plan) -> ContinuationReceiptWrite:
        try:
            return self._client.call(
                ControllerWorkerRPCOperation.DERIVE_CONTINUATION,
                payload={
                    "wakeup": _json(wakeup),
                    "projection": _json(projection),
                    "plan": _json(plan),
                },
                result_type=ContinuationReceiptWrite,
            )
        except ControllerWorkerRPCBlocked as exc:
            raise ContinuationAssessmentUnavailable(exc.blocker_codes) from exc


__all__ = [
    "ControllerWorkerRPCClient",
    "ControllerWorkerRPCBlocked",
    "ControllerWorkerRPCError",
    "ControllerWorkerRPCOperation",
    "ControllerWorkerRPCRequest",
    "ControllerWorkerRPCResponse",
    "ControllerWorkerRPCServicePin",
    "ControllerWorkerRPCTransport",
    "LinuxUnixSocketRPCTransport",
    "RPCActionProposalMaterialization",
    "RPCAtomicObservationAdmission",
    "RPCCommittedValidationSource",
    "RPCContinuationMaterialization",
    "RPCDatabaseObservationBridge",
    "RPCIndependentObservationAdmission",
    "RPCIndependentObservationValidator",
    "RPCProtocolCompilationMaterialization",
    "RPCRawRunEnvelopeSource",
    "RPCScientificExecutionAuthorizationIssuer",
    "RPCScientificExecutionRegistrar",
    "controller_worker_rpc_key_id",
]
