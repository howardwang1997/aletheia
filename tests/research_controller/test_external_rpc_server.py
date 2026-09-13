from __future__ import annotations

import hashlib
import os
import shutil
import socket
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.observations.store import ProtocolCompilationWrite
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCBlocked,
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCRequest,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    ControllerTickRPCPayload,
    ControllerWorkerRPCHandlerBinding,
    ControllerWorkerRPCHandlerSet,
    ControllerWorkerRPCRequestRejected,
    ControllerWorkerRPCServerError,
    ControllerWorkerRPCService,
    ControllerWorkerRPCServiceBlocked,
)
from aletheia.research_controller_rpc_runtime import (
    ControllerWorkerRPCProcessError,
    ControllerWorkerRPCServerCycleReceipt,
    ControllerWorkerRPCServerDeployment,
    ControllerWorkerRPCServerStartupReceipt,
    _ensure_runtime_socket_parent,
    build_controller_worker_rpc_server_runtime,
    load_controller_worker_rpc_server_deployment,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"rpc-server-receipt").digest())


def _pin(
    *,
    socket_path: str = "/run/aletheia/compiler.sock",
    process_uid: int = 7101,
    process_gid: int = 7100,
) -> ControllerWorkerRPCServicePin:
    public_key = (
        _private_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return ControllerWorkerRPCServicePin(
        service_principal_id="principal.rpc.compiler",
        service_manifest_sha256=_sha("compiler-service-manifest"),
        service_policy_sha256=_sha("compiler-service-policy"),
        operations=(ControllerWorkerRPCOperation.COMPILE_PROTOCOL,),
        authority_binding_sha256s=(_sha("compiler-authority-binding"),),
        socket_path=socket_path,
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(public_key.hex()),
        receipt_public_key_ed25519_hex=public_key.hex(),
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=256 * 1024,
        max_response_bytes=256 * 1024,
    )


def _tick_payload() -> ControllerTickRPCPayload:
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id="qst_" + "2" * 32,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:rpc-server",
        source_sha256=_sha("launch-source"),
    )
    projection = ControllerRecoveryProjection(
        quest_id=wakeup.quest_id,
        action_sha256=_sha("action"),
        scientific_slot_id=None,
        audited_stream_version=3,
        audited_tail_event_sha256=_sha("tail"),
        audited_snapshot_sha256=_sha("snapshot"),
        action_authorized=True,
        compilation_disposition=CompilationDisposition.MISSING,
        scientific_execution_authorization_registered=False,
        execution_terminal_observed=False,
        validation_committed=False,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )
    return ControllerTickRPCPayload(
        wakeup=wakeup,
        projection=projection,
        plan=plan_recovery_tick(projection),
    )


def _compilation_write() -> ProtocolCompilationWrite:
    protocol = {"protocol_id": "protocol.rpc", "version": 1}
    request_json = {"protocol": protocol, "catalog": {"sha256": _sha("catalog")}}
    receipt = {"status": "accepted", "work_order_sha256": _sha("work-order")}
    result_json = {"receipt": receipt, "accepted": True}
    request_sha256 = canonical_sha256(request_json)
    result_sha256 = canonical_sha256(result_json)
    receipt_sha256 = canonical_sha256(receipt)
    identity = canonical_sha256(
        {
            "schema_name": "aletheia.protocol_compilation_registration_identity",
            "schema_version": 1,
            "quest_id": _tick_payload().wakeup.quest_id,
            "action_sha256": _tick_payload().projection.action_sha256,
            "request_sha256": request_sha256,
            "result_sha256": result_sha256,
            "receipt_sha256": receipt_sha256,
        }
    )
    return ProtocolCompilationWrite(
        compilation_sha256=identity,
        quest_id=_tick_payload().wakeup.quest_id,
        action_sha256=_tick_payload().projection.action_sha256,
        protocol_id="protocol.rpc",
        protocol_version=1,
        protocol_sha256=canonical_sha256(protocol),
        request_sha256=request_sha256,
        result_sha256=result_sha256,
        receipt_sha256=receipt_sha256,
        request_json=request_json,
        result_json=result_json,
        registered_at=NOW,
    )


class _DirectTransport:
    def __init__(self, service: ControllerWorkerRPCService) -> None:
        self.service = service
        self.request_bytes: list[bytes] = []

    def exchange(self, _pin, request_bytes):
        self.request_bytes.append(request_bytes)
        return self.service.handle(request_bytes)


def _service(handler) -> tuple[ControllerWorkerRPCServicePin, ControllerWorkerRPCService]:
    pin = _pin()
    handlers = ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                handler=handler,
            ),
        ),
    )
    service = ControllerWorkerRPCService(
        pin=pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        handlers=handlers,
        receipt_private_key=_private_key().private_bytes_raw(),
        clock=lambda: NOW,
    )
    return pin, service


def test_server_and_client_round_trip_exact_typed_result_and_receipt() -> None:
    result = _compilation_write()

    def handle(payload):
        assert type(payload) is ControllerTickRPCPayload
        assert payload == _tick_payload()
        return result

    pin, service = _service(handle)
    transport = _DirectTransport(service)
    client = ControllerWorkerRPCClient(
        pin=pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        transport=transport,
        clock=lambda: NOW,
    )
    payload = _tick_payload()

    observed = client.call(
        ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload=payload.model_dump(mode="json"),
        result_type=ProtocolCompilationWrite,
    )

    assert observed == result
    assert (
        canonical_json_bytes(
            ControllerWorkerRPCRequest.model_validate_json(transport.request_bytes[0])
        )
        == transport.request_bytes[0]
    )


def test_server_rejects_noncanonical_unknown_and_rebound_requests_before_handler() -> None:
    called = False

    def handle(_payload):
        nonlocal called
        called = True
        return _compilation_write()

    pin, service = _service(handle)
    payload = _tick_payload().model_dump(mode="json")
    request = ControllerWorkerRPCRequest(
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        service_id=pin.service_id,
        service_pin_sha256=pin.pin_sha256,
        operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload=payload,
    )

    with pytest.raises(ControllerWorkerRPCRequestRejected, match="canonical"):
        service.handle(b" " + canonical_json_bytes(request))
    rebound = ControllerWorkerRPCRequest.model_validate(
        {
            **request.model_dump(mode="python", exclude={"request_id"}),
            "controller_manifest_sha256": _sha("other"),
        }
    )
    with pytest.raises(ControllerWorkerRPCRequestRejected, match="deployment pin"):
        service.handle(canonical_json_bytes(rebound))
    unknown_payload = ControllerWorkerRPCRequest(
        **{
            **request.model_dump(mode="python", exclude={"request_id"}),
            "payload": {**payload, "unexpected": True},
        }
    )
    with pytest.raises(ControllerWorkerRPCRequestRejected, match="payload"):
        service.handle(canonical_json_bytes(unknown_payload))
    assert called is False


def test_server_only_signs_blockers_for_the_three_typed_operations() -> None:
    pin, service = _service(
        lambda _payload: (_ for _ in ()).throw(
            ControllerWorkerRPCServiceBlocked(("compiler:observable_missing",))
        )
    )
    client = ControllerWorkerRPCClient(
        pin=pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        transport=_DirectTransport(service),
        clock=lambda: NOW,
    )
    with pytest.raises(ControllerWorkerRPCBlocked) as caught:
        client.call(
            ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            payload=_tick_payload().model_dump(mode="json"),
            result_type=ProtocolCompilationWrite,
        )
    assert caught.value.blocker_codes == ("compiler:observable_missing",)

    wrong_pin = ControllerWorkerRPCServicePin.model_validate(
        {
            **pin.model_dump(mode="python", exclude={"service_id"}),
            "operations": (ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,),
        }
    )
    wrong_handlers = ControllerWorkerRPCHandlerSet(
        operations=wrong_pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,
                handler=lambda _payload: (_ for _ in ()).throw(
                    ControllerWorkerRPCServiceBlocked(("authority:unavailable",))
                ),
            ),
        ),
    )
    wrong_service = ControllerWorkerRPCService(
        pin=wrong_pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        handlers=wrong_handlers,
        receipt_private_key=_private_key().private_bytes_raw(),
        clock=lambda: NOW,
    )
    wrong_request = ControllerWorkerRPCRequest(
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        service_id=wrong_pin.service_id,
        service_pin_sha256=wrong_pin.pin_sha256,
        operation=ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,
        payload=_tick_payload().model_dump(mode="json"),
    )
    with pytest.raises(ControllerWorkerRPCServerError, match="cannot convert"):
        wrong_service.handle(canonical_json_bytes(wrong_request))


def test_server_rejects_wrong_result_type_key_and_handler_partition() -> None:
    _pin_value, service = _service(lambda _payload: _tick_payload())
    request_payload = _tick_payload().model_dump(mode="json")
    pin = service.pin
    request = ControllerWorkerRPCRequest(
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        service_id=pin.service_id,
        service_pin_sha256=pin.pin_sha256,
        operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload=request_payload,
    )
    with pytest.raises(ControllerWorkerRPCServerError, match="another result type"):
        service.handle(canonical_json_bytes(request))

    with pytest.raises(ControllerWorkerRPCServerError, match="receipt key"):
        ControllerWorkerRPCService(
            pin=pin,
            controller_id="rctl_" + "3" * 32,
            controller_manifest_sha256=_sha("controller-manifest"),
            worker_process_principal_id="principal.controller.worker",
            handlers=ControllerWorkerRPCHandlerSet(
                operations=pin.operations,
                bindings=(
                    ControllerWorkerRPCHandlerBinding(
                        operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                        handler=lambda _payload: _compilation_write(),
                    ),
                ),
            ),
            receipt_private_key=hashlib.sha256(b"another-key").digest(),
        )
    with pytest.raises(ValueError, match="exactly cover"):
        ControllerWorkerRPCHandlerSet(operations=pin.operations, bindings=())


def test_server_refuses_expired_receipt_key_and_oversize_response() -> None:
    pin = _pin()
    handlers = ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                handler=lambda _payload: _compilation_write(),
            ),
        ),
    )
    expired = ControllerWorkerRPCService(
        pin=pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        handlers=handlers,
        receipt_private_key=_private_key().private_bytes_raw(),
        clock=lambda: NOW + timedelta(hours=2),
    )
    request = ControllerWorkerRPCRequest(
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        service_id=pin.service_id,
        service_pin_sha256=pin.pin_sha256,
        operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload=_tick_payload().model_dump(mode="json"),
    )
    with pytest.raises(ControllerWorkerRPCServerError, match="key interval"):
        expired.handle(canonical_json_bytes(request))

    bounded_pin = ControllerWorkerRPCServicePin.model_validate(
        {
            **pin.model_dump(mode="python", exclude={"service_id"}),
            "max_response_bytes": 1_024,
        }
    )
    bounded = ControllerWorkerRPCService(
        pin=bounded_pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        handlers=handlers,
        receipt_private_key=_private_key().private_bytes_raw(),
        clock=lambda: NOW,
    )
    bounded_request = ControllerWorkerRPCRequest(
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        service_id=bounded_pin.service_id,
        service_pin_sha256=bounded_pin.pin_sha256,
        operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
        payload=_tick_payload().model_dump(mode="json"),
    )
    with pytest.raises(ControllerWorkerRPCServerError, match="response exceeds"):
        bounded.handle(canonical_json_bytes(bounded_request))


def _runtime_fixture(tmp_path: Path) -> tuple[ControllerWorkerRPCServerDeployment, Path]:
    code_root = tmp_path / "release"
    config_root = tmp_path / "config"
    secret_root = tmp_path / "secrets"
    for path in (code_root, config_root, secret_root):
        path.mkdir()
    # Mirror the deployed run tree: a root-commissioned generation root shared
    # by every service uid, with one service-owned leaf below it.  pytest tmp
    # dirs are 0700, so only the modes set here matter.  Component names stay
    # short: bound paths must fit the kernel's unix socket name limit under
    # CI's long basetemp.
    generation_root = tmp_path / "run" / "g"
    socket_root = generation_root / "c"
    generation_root.mkdir(mode=0o750, parents=True)
    generation_root.chmod(0o750)
    socket_root.mkdir(mode=0o750)
    socket_root.chmod(0o750)
    # Directories under a sticky basetemp (e.g. /tmp) inherit a foreign group;
    # the deployment pins the process gid, so pin the group here too.
    os.chown(generation_root, -1, os.getegid())
    os.chown(socket_root, -1, os.getegid())
    source = code_root / "rpc_factory.py"
    config = config_root / "rpc_config.json"
    key_path = secret_root / "receipt.key"
    socket_path = socket_root / "s"
    result = _compilation_write()
    config.write_bytes(canonical_json_bytes({"result": result.model_dump(mode="json")}))
    source.write_text(
        """def build_rpc_service(*, deployment, configuration_bytes):
    import json
    from aletheia.observations.store import ProtocolCompilationWrite
    from aletheia.research_controller.external_rpc_server import (
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
    )

    result = ProtocolCompilationWrite.model_validate(json.loads(configuration_bytes)[\"result\"])
    return ControllerWorkerRPCHandlerSet(
        operations=deployment.service_pin.operations,
        bindings=(ControllerWorkerRPCHandlerBinding(
            operation=deployment.service_pin.operations[0],
            handler=lambda _payload: result,
        ),),
    )
""",
        encoding="utf-8",
    )
    key_path.write_bytes(_private_key().private_bytes_raw())
    key_path.chmod(0o400)
    parent = socket_root.stat()
    pin = _pin(
        socket_path=str(socket_path),
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
    )
    deployment = ControllerWorkerRPCServerDeployment(
        service_pin=pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        worker_peer_uid=os.geteuid() + 1,
        worker_peer_gid=os.getegid(),
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        socket_parent_path=str(socket_root),
        socket_parent_owner_uid=parent.st_uid,
        socket_parent_owner_gid=parent.st_gid,
        socket_parent_mode=parent.st_mode & 0o777,
        socket_parent_device_id=parent.st_dev,
        socket_parent_inode=parent.st_ino,
        receipt_private_key_path=str(key_path),
        receipt_private_key_sha256=hashlib.sha256(key_path.read_bytes()).hexdigest(),
        reviewed_code_root=str(code_root),
        composition_factory_module="aletheia.test_rpc_factory",
        composition_factory_attribute="build_rpc_service",
        composition_factory_source_path=str(source),
        composition_factory_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        composition_config_path=str(config),
        composition_config_file_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        prepared_at=NOW,
    )
    manifest_path = tmp_path / "deployment.json"
    manifest_path.write_bytes(canonical_json_bytes(deployment))
    return deployment, manifest_path


def test_runtime_loads_exact_factory_and_private_key_without_starting_socket(
    tmp_path: Path,
) -> None:
    deployment, manifest_path = _runtime_fixture(tmp_path)
    loaded = load_controller_worker_rpc_server_deployment(
        manifest_path,
        expected_file_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    runtime = build_controller_worker_rpc_server_runtime(loaded, clock=lambda: NOW)

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()
    assert deployment.private_key_loaded_in_worker is False
    assert deployment.transport_receipt_grants_scientific_authority is False


def test_runtime_rejects_key_custody_source_drift_and_unsafe_identity(tmp_path: Path) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    Path(deployment.receipt_private_key_path).chmod(0o600)
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody"):
        build_controller_worker_rpc_server_runtime(deployment)

    Path(deployment.receipt_private_key_path).chmod(0o400)
    Path(deployment.composition_factory_source_path).write_text("changed\n", encoding="utf-8")
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(deployment)

    with pytest.raises(ValidationError, match="distinct UIDs"):
        ControllerWorkerRPCServerDeployment.model_validate(
            {
                **deployment.model_dump(mode="python", exclude={"runtime_id"}),
                "worker_peer_uid": deployment.process_uid,
            }
        )


def test_non_linux_runtime_fails_before_binding_or_claiming_host_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    monkeypatch.setattr("aletheia.research_controller_rpc_runtime.sys.platform", "darwin")

    with pytest.raises(ControllerWorkerRPCProcessError, match="Linux SO_PEERCRED"):
        runtime.start()
    assert not Path(deployment.service_pin.socket_path).exists()


def test_client_does_not_accept_unsigned_server_failures() -> None:
    _pin_value, service = _service(
        lambda _payload: (_ for _ in ()).throw(RuntimeError("domain store unavailable"))
    )
    with pytest.raises(RuntimeError, match="domain store unavailable"):
        service.handle(
            canonical_json_bytes(
                ControllerWorkerRPCRequest(
                    controller_id="rctl_" + "3" * 32,
                    controller_manifest_sha256=_sha("controller-manifest"),
                    worker_process_principal_id="principal.controller.worker",
                    service_id=service.pin.service_id,
                    service_pin_sha256=service.pin.pin_sha256,
                    operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                    payload=_tick_payload().model_dump(mode="json"),
                )
            )
        )
    client = ControllerWorkerRPCClient(
        pin=service.pin,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller-manifest"),
        worker_process_principal_id="principal.controller.worker",
        transport=_DirectTransport(service),
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="domain store unavailable"):
        client.call(
            ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            payload=_tick_payload().model_dump(mode="json"),
            result_type=ProtocolCompilationWrite,
        )


def test_server_deployment_contains_no_inline_private_key() -> None:
    forbidden = {
        "receipt_private_key",
        "receipt_private_key_hex",
        "private_key_ed25519_hex",
    }
    assert forbidden.isdisjoint(ControllerWorkerRPCServerDeployment.model_fields)
    assert forbidden.isdisjoint(ControllerWorkerRPCServicePin.model_fields)


def test_operational_receipts_reject_partial_or_noncanonical_evidence() -> None:
    pin = _pin()
    with pytest.raises(ValidationError, match="unique and canonical"):
        ControllerWorkerRPCServerStartupReceipt(
            runtime_id="rpcsrv_" + "1" * 32,
            deployment_sha256=_sha("deployment"),
            service_id=pin.service_id,
            receipt_key_id=pin.receipt_key_id,
            operations=(
                ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
            ),
            socket_device_id=1,
            socket_inode=2,
            started_at=NOW,
        )
    with pytest.raises(ValidationError, match="inconsistent evidence"):
        ControllerWorkerRPCServerCycleReceipt(
            runtime_id="rpcsrv_" + "1" * 32,
            deployment_sha256=_sha("deployment"),
            service_id=pin.service_id,
            cycle_number=1,
            disposition="rejected",
            request_id="rpcq_" + "2" * 32,
            rejection_code="request_rejected",
            started_at=NOW,
            finished_at=NOW,
        )

    valid = ControllerWorkerRPCServerStartupReceipt(
        runtime_id="rpcsrv_" + "1" * 32,
        deployment_sha256=_sha("deployment"),
        service_id=pin.service_id,
        receipt_key_id=pin.receipt_key_id,
        operations=(ControllerWorkerRPCOperation.COMPILE_PROTOCOL,),
        socket_device_id=1,
        socket_inode=2,
        boot_id="c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
        started_at=NOW,
    )
    assert valid.boot_id == "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
    payload = valid.model_dump(mode="json")
    del payload["boot_id"]
    legacy = ControllerWorkerRPCServerStartupReceipt.model_validate(payload)
    assert legacy.boot_id is None
    with pytest.raises(ValidationError, match="boot_id"):
        ControllerWorkerRPCServerStartupReceipt.model_validate(
            payload | {"boot_id": "not-a-uuid"}
        )


def _force_linux_peer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aletheia.research_controller_rpc_runtime.sys.platform", "linux")
    monkeypatch.setattr(socket, "SO_PEERCRED", 1, raising=False)


def test_ensure_runtime_socket_parent_recreates_boot_missing_leaf(tmp_path: Path) -> None:
    generation_root = (tmp_path / "run" / "aletheia-scientific").resolve()
    parent = generation_root / "compiler"
    generation_root.mkdir(mode=0o750, parents=True)
    generation_root.chmod(0o750)
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)

    def ensure() -> tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int]]:
        return _ensure_runtime_socket_parent(
            parent,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            mode=0o750,
        )

    marker = generation_root / "commissioned"
    marker.write_bytes(b"root-commissioned")
    root_before = generation_root.stat()
    intact = ensure()
    intact_inode = parent.stat().st_ino
    assert intact == (
        (parent.stat().st_dev, intact_inode, os.geteuid(), os.getegid(), 0o750),
        (
            root_before.st_dev,
            root_before.st_ino,
            root_before.st_uid,
            root_before.st_gid,
            0o750,
        ),
    )
    ensure()
    assert parent.stat().st_ino == intact_inode

    # A reboot keeps the provisioned generation root and loses only the
    # service-owned leaf; the leaf returns with its pinned custody while the
    # shared root keeps its identity and its commissioned contents.
    shutil.rmtree(parent)
    ensure()
    recreated = parent.stat()
    assert stat.S_ISDIR(recreated.st_mode)
    assert (recreated.st_uid, recreated.st_gid, stat.S_IMODE(recreated.st_mode)) == (
        os.geteuid(),
        os.getegid(),
        0o750,
    )
    assert generation_root.stat().st_ino == root_before.st_ino
    assert marker.read_bytes() == b"root-commissioned"
    # The recreated inode stays stable across further startups; it is never
    # compared to the pre-reboot inode because CI filesystems reuse inodes.
    ensure()
    assert parent.stat().st_ino == recreated.st_ino

    # The generation root is provisioner-owned: when a reboot takes it too,
    # recreation fails closed naming the provisioner and mints no leaf.
    shutil.rmtree(generation_root)
    with pytest.raises(ControllerWorkerRPCProcessError, match="generation root is absent"):
        ensure()
    assert not parent.exists()


def test_ensure_runtime_socket_parent_fails_closed_on_foreign_custody(tmp_path: Path) -> None:
    parent = (tmp_path / "run").resolve()
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)

    def ensure() -> tuple[int, int, int, int, int]:
        return _ensure_runtime_socket_parent(
            parent,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            mode=0o750,
        )

    parent.chmod(0o711)
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        ensure()

    parent.chmod(0o750)
    decoy = (tmp_path / "decoy").resolve()
    decoy.mkdir(mode=0o750)
    parent.rmdir()
    parent.symlink_to(decoy, target_is_directory=True)
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        ensure()

    parent.unlink()
    parent.write_bytes(b"")
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        ensure()


def test_ensure_runtime_socket_parent_rejects_foreign_generation_root(tmp_path: Path) -> None:
    generation_root = (tmp_path / "run" / "aletheia-scientific").resolve()
    parent = generation_root / "compiler"

    def ensure() -> tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int]]:
        return _ensure_runtime_socket_parent(
            parent,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            mode=0o750,
        )

    generation_root.mkdir(mode=0o750, parents=True)
    generation_root.chmod(0o750)
    generation_root.chmod(0o757)
    with pytest.raises(
        ControllerWorkerRPCProcessError,
        match="generation root custody is not a private directory",
    ):
        ensure()
    assert not parent.exists()

    generation_root.chmod(0o750)
    shutil.rmtree(generation_root)
    generation_root.write_bytes(b"")
    with pytest.raises(
        ControllerWorkerRPCProcessError,
        match="generation root custody is not a private directory",
    ):
        ensure()
    assert not parent.exists()

    generation_root.unlink()
    decoy = (tmp_path / "decoy-root").resolve()
    decoy.mkdir(mode=0o750)
    decoy.chmod(0o750)
    generation_root.symlink_to(decoy, target_is_directory=True)
    with pytest.raises(
        ControllerWorkerRPCProcessError,
        match="generation root custody is not a private directory",
    ):
        ensure()
    assert not parent.exists()
    # The recreate must never be attempted through the symlink.
    assert not (decoy / "compiler").exists()


def test_runtime_start_recreates_missing_socket_parent_after_reboot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    _force_linux_peer_credentials(monkeypatch)
    socket_parent = Path(deployment.socket_parent_path)
    socket_path = Path(deployment.service_pin.socket_path)

    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    runtime.start()
    assert stat.S_ISSOCK(socket_path.stat().st_mode)
    runtime.close()
    assert not os.path.lexists(socket_path)

    # The reboot wipes the service-owned leaf while the provisioned generation
    # root keeps the shared identity the deployment pin cannot carry across
    # boots; startup must recover the leaf only.
    shutil.rmtree(socket_parent)
    ensure_calls = []

    def _recording_ensure(path, *, owner_uid, owner_gid, mode):
        ensure_calls.append((path, owner_uid, owner_gid, mode))
        return _ensure_runtime_socket_parent(
            path, owner_uid=owner_uid, owner_gid=owner_gid, mode=mode
        )

    monkeypatch.setattr(
        "aletheia.research_controller_rpc_runtime._ensure_runtime_socket_parent",
        _recording_ensure,
    )
    monkeypatch.setattr(
        "aletheia.research_controller_rpc_runtime._boot_id",
        lambda: "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
    )
    rebound = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    receipt = rebound.start()
    assert receipt.boot_id == "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
    recreated = socket_parent.stat()
    generation_root = socket_parent.parent.stat()
    assert stat.S_ISDIR(recreated.st_mode)
    assert (recreated.st_uid, recreated.st_gid, stat.S_IMODE(recreated.st_mode)) == (
        deployment.socket_parent_owner_uid,
        deployment.socket_parent_owner_gid,
        0o750,
    )
    assert stat.S_ISSOCK(socket_path.stat().st_mode)
    assert ensure_calls == [
        (socket_parent, os.geteuid(), os.getegid(), 0o750),
    ]
    assert rebound._socket_parent_identity == (  # noqa: SLF001
        (
            recreated.st_dev,
            recreated.st_ino,
            deployment.socket_parent_owner_uid,
            deployment.socket_parent_owner_gid,
            0o750,
        ),
        (
            generation_root.st_dev,
            generation_root.st_ino,
            generation_root.st_uid,
            generation_root.st_gid,
            0o750,
        ),
    )
    rebound.close()


def test_runtime_start_fails_closed_on_foreign_socket_parent_custody(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    _force_linux_peer_credentials(monkeypatch)
    socket_parent = Path(deployment.socket_parent_path)
    socket_path = Path(deployment.service_pin.socket_path)
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)

    socket_parent.chmod(0o711)
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        runtime.start()
    assert not os.path.lexists(socket_path)

    socket_parent.chmod(0o750)
    decoy = (tmp_path / "decoy-parent").resolve()
    decoy.mkdir(mode=0o750)
    socket_parent.rmdir()
    socket_parent.symlink_to(decoy, target_is_directory=True)
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        runtime.start()
    assert not os.path.lexists(socket_path)


def test_runtime_start_fails_closed_when_generation_root_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    _force_linux_peer_credentials(monkeypatch)
    socket_path = Path(deployment.service_pin.socket_path)
    shutil.rmtree(tmp_path / "run")

    # A reboot without the tmpfiles.d tree leaves no generation root to serve
    # under: startup fails closed naming the provisioner, before any bind.
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    with pytest.raises(ControllerWorkerRPCProcessError, match="generation root is absent"):
        runtime.start()
    assert not os.path.lexists(socket_path)


def test_runtime_parent_checks_stay_strict_after_startup_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    _force_linux_peer_credentials(monkeypatch)
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    runtime.start()
    observed = Path(deployment.socket_parent_path).stat()
    generation_root = Path(deployment.socket_parent_path).parent.stat()
    assert runtime._socket_parent_identity == (  # noqa: SLF001
        (
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            observed.st_gid,
            0o750,
        ),
        (
            generation_root.st_dev,
            generation_root.st_ino,
            generation_root.st_uid,
            generation_root.st_gid,
            0o750,
        ),
    )

    def _refuse(*_args, **_kwargs):
        raise AssertionError("per-cycle checks must not recreate the socket parent")

    monkeypatch.setattr(
        "aletheia.research_controller_rpc_runtime._ensure_runtime_socket_parent",
        _refuse,
    )
    assert runtime._parent_identity() == runtime._socket_parent_identity  # noqa: SLF001

    # A replaced leaf directory with identical custody is still foreign: the
    # anchor keeps the per-cycle comparison on this boot's exact identity.
    runtime._socket_parent_identity = (  # noqa: SLF001
        (
            observed.st_dev,
            observed.st_ino + 1,
            observed.st_uid,
            observed.st_gid,
            0o750,
        ),
        (
            generation_root.st_dev,
            generation_root.st_ino,
            generation_root.st_uid,
            generation_root.st_gid,
            0o750,
        ),
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        runtime._parent_identity()  # noqa: SLF001
    runtime.close()


def test_parent_identity_anchors_generation_root_per_cycle(tmp_path: Path) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    socket_parent = Path(deployment.socket_parent_path)
    generation_root_path = socket_parent.parent
    leaf = socket_parent.stat()
    generation_root = generation_root_path.stat()
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    runtime._socket_parent_identity = (  # noqa: SLF001
        (
            leaf.st_dev,
            leaf.st_ino,
            leaf.st_uid,
            leaf.st_gid,
            0o750,
        ),
        (
            generation_root.st_dev,
            generation_root.st_ino,
            generation_root.st_uid,
            generation_root.st_gid,
            0o750,
        ),
    )

    # A world-open generation root fails every cycle on every service.
    generation_root_path.chmod(0o757)
    with pytest.raises(
        ControllerWorkerRPCProcessError,
        match="generation root custody is not a private directory",
    ):
        runtime._parent_identity()  # noqa: SLF001

    # A renamed-and-recreated generation root with identical custody is still
    # foreign: the anchor keeps the per-cycle comparison on this boot's exact
    # shared identity, not just its mode.
    generation_root_path.chmod(0o750)
    runtime._socket_parent_identity = (  # noqa: SLF001
        (
            leaf.st_dev,
            leaf.st_ino,
            leaf.st_uid,
            leaf.st_gid,
            0o750,
        ),
        (
            generation_root.st_dev,
            generation_root.st_ino + 1,
            generation_root.st_uid,
            generation_root.st_gid,
            0o750,
        ),
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="custody differs from its pin"):
        runtime._parent_identity()  # noqa: SLF001

    shutil.rmtree(socket_parent)
    with pytest.raises(ControllerWorkerRPCProcessError, match="RPC socket parent is unavailable"):
        runtime._parent_identity()  # noqa: SLF001


def test_serve_once_after_close_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _manifest_path = _runtime_fixture(tmp_path)
    _force_linux_peer_credentials(monkeypatch)
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)
    runtime.start()
    runtime.close()

    # close() nulls the listener while keeping the startup receipt, so
    # serve_once's start() early-returns; the cycle must fail closed instead
    # of tripping an assertion.
    with pytest.raises(
        ControllerWorkerRPCProcessError, match="closed before this serve cycle"
    ):
        runtime.serve_once()
