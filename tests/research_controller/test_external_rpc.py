from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCBlocked,
    ControllerWorkerRPCClient,
    ControllerWorkerRPCError,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCRequest,
    ControllerWorkerRPCResponse,
    ControllerWorkerRPCServicePin,
    RPCActionProposalMaterialization,
    RPCAtomicObservationAdmission,
    RPCCommittedValidationSource,
    RPCContinuationMaterialization,
    RPCDatabaseObservationBridge,
    RPCIndependentObservationAdmission,
    RPCIndependentObservationValidator,
    RPCProtocolCompilationMaterialization,
    RPCRawRunEnvelopeSource,
    RPCScientificExecutionAuthorizationIssuer,
    RPCScientificExecutionRegistrar,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.protocol_compilation_step import ProtocolCompilationUnavailable
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

NOW = datetime(2026, 8, 26, 4, 0, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _Result(ControllerModel):
    schema_name: str = "test.controller_worker_rpc_result"
    value: str


def _private_key(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())


def _pin(
    *,
    private_key: Ed25519PrivateKey,
    operation: ControllerWorkerRPCOperation = ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
    authority_binding_sha256s: tuple[str, ...] = (_sha("authority-binding"),),
) -> ControllerWorkerRPCServicePin:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return ControllerWorkerRPCServicePin(
        service_principal_id="principal.rpc.protocol",
        service_manifest_sha256=_sha("rpc-manifest"),
        service_policy_sha256=_sha("rpc-policy"),
        operations=(operation,),
        authority_binding_sha256s=authority_binding_sha256s,
        socket_path="/run/aletheia/protocol.sock",
        socket_owner_uid=7101,
        socket_group_gid=7100,
        socket_mode=0o660,
        peer_uid=7101,
        peer_gid=7100,
        receipt_key_id=controller_worker_rpc_key_id(public_key.hex()),
        receipt_public_key_ed25519_hex=public_key.hex(),
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=64 * 1024,
        max_response_bytes=64 * 1024,
    )


class _SignedTransport:
    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        result: _Result,
        mutate: str | None = None,
    ) -> None:
        self._private_key = private_key
        self._result = result
        self._mutate = mutate
        self.requests: list[ControllerWorkerRPCRequest] = []

    def exchange(self, pin, request_bytes):
        request = ControllerWorkerRPCRequest.model_validate_json(request_bytes)
        assert canonical_json_bytes(request) == request_bytes
        self.requests.append(request)
        payload = {
            "request_sha256": request.request_sha256,
            "service_id": pin.service_id,
            "service_principal_id": pin.service_principal_id,
            "service_manifest_sha256": pin.service_manifest_sha256,
            "service_policy_sha256": pin.service_policy_sha256,
            "operation": request.operation,
            "result": self._result.model_dump(mode="json"),
            "result_sha256": canonical_sha256(self._result),
            "signed_at": NOW,
            "receipt_key_id": pin.receipt_key_id,
            "signature_ed25519_hex": "0" * 128,
        }
        if self._mutate == "blocked":
            payload.update(
                {
                    "disposition": "blocked",
                    "result": None,
                    "result_sha256": None,
                    "blocker_codes": ("provider:unavailable",),
                }
            )
        if self._mutate == "request":
            payload["request_sha256"] = _sha("another-request")
        unsigned = ControllerWorkerRPCResponse.model_validate(payload)
        payload["signature_ed25519_hex"] = self._private_key.sign(unsigned.signing_bytes).hex()
        response = ControllerWorkerRPCResponse.model_validate(payload)
        encoded = canonical_json_bytes(response)
        if self._mutate == "signature":
            encoded = encoded.replace(
                response.signature_ed25519_hex.encode(),
                ("f" * 128).encode(),
            )
        if self._mutate == "noncanonical":
            encoded = b" " + encoded
        return encoded


def test_rpc_client_binds_canonical_request_and_signed_typed_result() -> None:
    private_key = _private_key("rpc-receipt")
    result = _Result(value="accepted")
    transport = _SignedTransport(private_key=private_key, result=result)
    pin = _pin(private_key=private_key)
    client = ControllerWorkerRPCClient(
        pin=pin,
        controller_id=f"rctl_{_sha('controller')[:32]}",
        controller_manifest_sha256=_sha("controller"),
        worker_process_principal_id="principal.controller.worker",
        transport=transport,
        clock=lambda: NOW,
    )

    first = client.call(
        ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload={"action_sha256": _sha("action")},
        result_type=_Result,
    )
    second = client.call(
        ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload={"action_sha256": _sha("action")},
        result_type=_Result,
    )

    assert first == second == result
    assert transport.requests[0] == transport.requests[1]
    assert transport.requests[0].request_sha256 == transport.requests[1].request_sha256
    assert pin.private_key_loaded_in_worker is False
    assert pin.linux_peer_credentials_required is True


@pytest.mark.parametrize("mutation", ("request", "signature", "noncanonical"))
def test_rpc_client_rejects_rebinding_tamper_and_noncanonical_bytes(mutation: str) -> None:
    private_key = _private_key("rpc-receipt")
    client = ControllerWorkerRPCClient(
        pin=_pin(private_key=private_key),
        controller_id=f"rctl_{_sha('controller')[:32]}",
        controller_manifest_sha256=_sha("controller"),
        worker_process_principal_id="principal.controller.worker",
        transport=_SignedTransport(
            private_key=private_key,
            result=_Result(value="accepted"),
            mutate=mutation,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(ControllerWorkerRPCError):
        client.call(
            ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            payload={"action_sha256": _sha("action")},
            result_type=_Result,
        )


def test_rpc_pin_is_closed_and_cryptographically_self_identifying() -> None:
    private_key = _private_key("rpc-receipt")
    pin = _pin(private_key=private_key)
    assert pin.service_id == f"rpcs_{pin.pin_sha256[:32]}"

    with pytest.raises(ValidationError, match="key id"):
        ControllerWorkerRPCServicePin.model_validate(
            {
                **pin.model_dump(mode="python", exclude={"service_id"}),
                "receipt_key_id": "rpck_" + "0" * 32,
            }
        )
    with pytest.raises(ValidationError, match="0600 or 0660"):
        ControllerWorkerRPCServicePin.model_validate(
            {**pin.model_dump(mode="python", exclude={"service_id"}), "socket_mode": 0o666}
        )
    with pytest.raises(ValidationError, match="extra"):
        ControllerWorkerRPCServicePin.model_validate(
            {**pin.model_dump(mode="python"), "private_key_ed25519_hex": "0" * 64}
        )


def test_rpc_client_rejects_oversize_request_before_transport() -> None:
    private_key = _private_key("rpc-receipt")
    transport = _SignedTransport(private_key=private_key, result=_Result(value="unused"))
    original = _pin(private_key=private_key)
    pin = ControllerWorkerRPCServicePin.model_validate(
        {
            **original.model_dump(mode="python", exclude={"service_id"}),
            "max_request_bytes": 1_024,
        }
    )
    client = ControllerWorkerRPCClient(
        pin=pin,
        controller_id=f"rctl_{_sha('controller')[:32]}",
        controller_manifest_sha256=_sha("controller"),
        worker_process_principal_id="principal.controller.worker",
        transport=transport,
        clock=lambda: NOW,
    )

    with pytest.raises(ControllerWorkerRPCError, match="request exceeds"):
        client.call(
            ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            payload={"oversize": "x" * 2_000},
            result_type=_Result,
        )
    assert transport.requests == []


def test_rpc_client_preserves_signed_non_retryable_blockers() -> None:
    private_key = _private_key("rpc-receipt")
    client = ControllerWorkerRPCClient(
        pin=_pin(private_key=private_key),
        controller_id=f"rctl_{_sha('controller')[:32]}",
        controller_manifest_sha256=_sha("controller"),
        worker_process_principal_id="principal.controller.worker",
        transport=_SignedTransport(
            private_key=private_key,
            result=_Result(value="unused"),
            mutate="blocked",
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(ControllerWorkerRPCBlocked) as caught:
        client.call(
            ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            payload={"action_sha256": _sha("action")},
            result_type=_Result,
        )
    assert caught.value.blocker_codes == ("provider:unavailable",)


def test_protocol_rpc_facade_maps_signed_blocker_to_step_disposition() -> None:
    private_key = _private_key("rpc-receipt")
    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.PROTOCOL_COMPILATION,
        principal_id="principal.rpc.protocol",
        policy_sha256=_sha("rpc-policy"),
        service_manifest_sha256=_sha("rpc-manifest"),
        externally_deployed=True,
    )
    client = ControllerWorkerRPCClient(
        pin=_pin(
            private_key=private_key,
            authority_binding_sha256s=(binding.binding_sha256,),
        ),
        controller_id=f"rctl_{_sha('controller')[:32]}",
        controller_manifest_sha256=_sha("controller"),
        worker_process_principal_id="principal.controller.worker",
        transport=_SignedTransport(
            private_key=private_key,
            result=_Result(value="unused"),
            mutate="blocked",
        ),
        clock=lambda: NOW,
    )
    facade = RPCProtocolCompilationMaterialization(client, binding)
    tick = _Result(value="tick")

    with pytest.raises(ProtocolCompilationUnavailable) as caught:
        facade.compile_and_register(wakeup=tick, projection=tick, plan=tick)
    assert caught.value.blocker_codes == ("provider:unavailable",)


class _StopRPC(Exception):
    pass


class _RecordingClient:
    def __init__(self, *bindings: ControllerStepAuthorityBinding) -> None:
        self.pin = SimpleNamespace(
            authority_binding_sha256s=tuple(sorted(binding.binding_sha256 for binding in bindings))
        )
        self.operations: list[ControllerWorkerRPCOperation] = []

    def call(self, operation, **_kwargs):
        self.operations.append(operation)
        raise _StopRPC


def _authority(role: ControllerStepAuthorityRole) -> ControllerStepAuthorityBinding:
    signed = role in {
        ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    }
    return ControllerStepAuthorityBinding(
        role=role,
        principal_id=f"principal.{role.value}",
        key_id=f"key.{role.value}" if signed else None,
        policy_sha256=_sha(f"{role.value}:policy"),
        service_manifest_sha256=_sha(f"{role.value}:manifest"),
        externally_deployed=True,
    )


def test_rpc_facades_cover_each_closed_operation_without_a_catch_all() -> None:
    role = ControllerStepAuthorityRole
    action = _authority(role.ACTION_PROPOSAL)
    compiler = _authority(role.PROTOCOL_COMPILATION)
    execution = _authority(role.EXECUTION_AUTHORIZATION)
    database = _authority(role.DATABASE_ATTESTATION)
    validator = _authority(role.INDEPENDENT_VALIDATION)
    admission = _authority(role.INDEPENDENT_ADMISSION)
    kernel = _authority(role.KERNEL_COMMAND)
    continuation = _authority(role.CONTINUATION_ASSESSMENT)
    tick = _Result(value="tick")
    calls = (
        (
            RPCActionProposalMaterialization(_RecordingClient(action), action),
            "materialize_and_submit",
            {"wakeup": tick, "projection": tick, "plan": tick},
        ),
        (
            RPCProtocolCompilationMaterialization(_RecordingClient(compiler), compiler),
            "compile_and_register",
            {"wakeup": tick, "projection": tick, "plan": tick},
        ),
        (
            RPCScientificExecutionAuthorizationIssuer(_RecordingClient(execution), execution),
            "issue_scientific_execution_authorization",
            {"wakeup": tick, "projection": tick, "plan": tick},
        ),
        (
            RPCScientificExecutionRegistrar(_RecordingClient(execution), execution),
            "register_and_reserve",
            {"authorization": tick},
        ),
        (
            RPCRawRunEnvelopeSource(_RecordingClient(execution), execution),
            "load_raw_run",
            {
                "quest_id": "qst_" + "1" * 32,
                "action_sha256": _sha("action"),
                "scientific_slot_id": "sos_" + "2" * 32,
            },
        ),
        (
            RPCIndependentObservationValidator(_RecordingClient(validator), validator),
            "prepare_validation_campaign",
            {"raw_run": tick},
        ),
        (
            RPCIndependentObservationValidator(_RecordingClient(validator), validator),
            "issue_validation_receipt",
            {
                "raw_run": tick,
                "validation_campaign_sha256": _sha("campaign"),
                "issuance_challenge": tick,
            },
        ),
        (
            RPCDatabaseObservationBridge(_RecordingClient(database), database),
            "issue_validation_challenge",
            {"raw_run": tick, "validation_campaign_sha256": _sha("campaign")},
        ),
        (
            RPCDatabaseObservationBridge(_RecordingClient(database), database),
            "commit_validation",
            {"receipt": tick},
        ),
        (
            RPCDatabaseObservationBridge(_RecordingClient(database), database),
            "issue_admission_challenge",
            {"committed_validation": tick},
        ),
        (
            RPCCommittedValidationSource(
                _RecordingClient(database, validator), (database, validator)
            ),
            "load_committed_validation",
            {
                "quest_id": "qst_" + "1" * 32,
                "action_sha256": _sha("action"),
                "scientific_slot_id": "sos_" + "2" * 32,
            },
        ),
        (
            RPCIndependentObservationAdmission(_RecordingClient(admission), admission),
            "issue_admission_decision",
            {"committed_validation": tick, "issuance_challenge": tick},
        ),
        (
            RPCAtomicObservationAdmission(
                _RecordingClient(database, admission, kernel),
                database_binding=database,
                admission_binding=admission,
                kernel_binding=kernel,
            ),
            "commit_and_incorporate",
            {"decision": tick},
        ),
        (
            RPCContinuationMaterialization(_RecordingClient(continuation), continuation),
            "derive_and_register",
            {"wakeup": tick, "projection": tick, "plan": tick},
        ),
    )

    observed: list[ControllerWorkerRPCOperation] = []
    for facade, method_name, kwargs in calls:
        with pytest.raises(_StopRPC):
            getattr(facade, method_name)(**kwargs)
        observed.extend(facade._client.operations)

    assert len(observed) == len(ControllerWorkerRPCOperation)
    assert frozenset(observed) == frozenset(ControllerWorkerRPCOperation)
