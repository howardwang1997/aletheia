from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

import aletheia.execution.qualification_node_service as node_service
import aletheia.execution.oci_deployment as oci_deployment
from aletheia.execution.assignment_contracts import node_transport_key_id
from aletheia.execution.node_agent import NodeRunOutcome, NodeRunResult, PinnedLaunchSpec
from aletheia.execution.oci_deployment import (
    OCIOutputQuotaError,
    LoopbackOutputQuotaProvisionerClient,
    LoopbackOutputQuotaProvisioningService,
    PinnedOCIImageLayout,
)
from aletheia.execution.postgresql_node_adapter import QualificationExecutionWorker
from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
from aletheia.execution.qualification_node_service import (
    QualificationNodeCompositionError,
    QualificationNodeMutableRootPinV1,
    QualificationNodePrivateKeyPinV1,
    QualificationNodeServiceConfigV1,
    QualificationNodeWorkerLoop,
    compose_node_service,
)
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_control_issuance import PinnedRuntimeControlIssuanceAuthority
from aletheia.execution.runtime_v2_contracts import RuntimeControlAuthorityPin
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.execution.terminal_runtime import TerminalNodeAuthorityConfig

_EXECUTION_TESTS = Path(__file__).resolve().parent
_CONTROLLER_TESTS = _EXECUTION_TESTS.parent / "research_controller"
for value in (_EXECUTION_TESTS, _CONTROLLER_TESTS):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from test_allocator import TRANSPORT_PRIVATE_KEY  # noqa: E402
from test_oci_runtime import _policy, _spec as _launch_spec  # noqa: E402
from test_qualification_deployment import _observation, _spec as _deployment_spec  # noqa: E402
from test_runtime_contracts import PRIVATE_KEY, _worker_authority  # noqa: E402
from test_terminal_runtime import _config as _terminal_config  # noqa: E402

RUNTIME_PRIVATE_KEY = hashlib.sha256(b"qualification-node-runtime-control").digest()


def _public_key(private_key: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _key_pin(
    *,
    role: str,
    algorithm: str,
    path: str,
    private_key: bytes,
    key_id: str,
    uid: int,
    gid: int,
) -> QualificationNodePrivateKeyPinV1:
    return QualificationNodePrivateKeyPinV1(
        role=role,
        algorithm=algorithm,
        path=path,
        file_sha256=hashlib.sha256(private_key).hexdigest(),
        key_id=key_id,
        owner_uid=uid,
        owner_gid=gid,
        parent_chain_sha256="e" * 64,
    )


def _root_pin(
    *,
    purpose: str,
    path: str,
    device: int,
    inode: int,
    uid: int,
    gid: int,
    parent_chain_sha256: str,
) -> QualificationNodeMutableRootPinV1:
    return QualificationNodeMutableRootPinV1(
        purpose=purpose,
        path=path,
        device=device,
        inode=inode,
        owner_uid=uid,
        owner_gid=gid,
        parent_chain_sha256=parent_chain_sha256,
    )


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    terminal_root = tmp_path / "terminal"
    terminal_root.mkdir()
    terminal, _controller_manifest = _terminal_config(monkeypatch, terminal_root)
    original_node_authority = terminal.node_authorities[0]
    manifest = original_node_authority.manifest.model_validate(
        original_node_authority.manifest.model_copy(
            update={"container_runtime": "docker"}
        ).model_dump(mode="python")
    )
    verified_node = _worker_authority(manifest, observed_at=terminal.prepared_at)
    transport_pin = original_node_authority.assignment_transport_pin.model_validate(
        original_node_authority.assignment_transport_pin.model_copy(
            update={"node_manifest_sha256": manifest.manifest_sha256}
        ).model_dump(mode="python")
    )
    node_authority = TerminalNodeAuthorityConfig(
        manifest=manifest,
        enrollment=verified_node.enrollment,
        enrollment_authority_pin=verified_node.enrollment_authority_pin,
        assignment_transport_pin=transport_pin,
    )
    deployment_spec = _deployment_spec()
    observation = _observation(deployment_spec)
    uid = observation.quota_deployment.allowed_client_uid
    gid = observation.quota_deployment.allowed_client_gid
    launch: PinnedLaunchSpec = _launch_spec()
    base_policy = _policy(tmp_path / "policy", launch)
    policy = base_policy.model_validate(
        base_policy.model_copy(
            update={
                "oci_platform": manifest.oci_platform,
                "sandbox_policy_sha256": manifest.sandbox_policy_sha256,
                "workload_uid": uid,
                "workload_gid": gid,
            }
        ).model_dump(mode="python")
    )
    runtime_public = _public_key(RUNTIME_PRIVATE_KEY)
    runtime_pin = RuntimeControlAuthorityPin(
        policy_sha256=terminal.runtime_control_authority_pin.policy_sha256,
        principal_id=terminal.runtime_control_authority_pin.principal_id,
        key_id=qualification_key_id(runtime_public),
        public_key_ed25519_hex=runtime_public,
        valid_from=terminal.prepared_at - timedelta(days=1),
        expires_at=terminal.prepared_at + timedelta(days=1),
    )
    custody = QualificationPreAdmissionCustodyConfig(
        artifact_store_root=terminal.artifact_store_root,
        artifact_verifier_principal_id=terminal.artifact_verifier_principal_id,
        artifact_object_store_id=terminal.artifact_object_store_id,
        artifact_max_object_bytes=terminal.artifact_max_object_bytes,
        authority_registry_root=terminal.authority_registry_root,
        authority_registry_filesystem_pin=terminal.authority_registry_filesystem_pin,
        pricing_authority_pin=terminal.pricing_authority_pin,
        source_budget_authority_pin=terminal.source_budget_authority_pin,
        qualification_authority_pin=terminal.qualification_authority_pin,
        terminal_verification_authority_pin=terminal.terminal_verification_authority_pin,
        input_resolver_principal_id=terminal.input_resolver_principal_id,
        prepared_at=terminal.prepared_at,
    )
    watchdog = observation.watchdog_deployment.model_copy(
        update={
            "policy_sha256": policy.policy_sha256,
            "allowed_client_uid": uid,
            "allowed_client_gid": gid,
        }
    )
    observed_roots = {item.purpose: item for item in observation.custody_roots}
    layout_root = (tmp_path / "image-layout").resolve()
    process = QualificationServiceProcessDeploymentV1(
        deployment_id=deployment_spec.deployment_id,
        role=QualificationServiceRole.NODE,
        operation="run",
        process_uid=uid,
        process_gid=gid,
        worker_poll_milliseconds=deployment_spec.worker_poll_milliseconds,
        reviewed_code_root="/opt/aletheia/release",
        composition_factory_module="aletheia.execution.qualification_node_composition",
        composition_factory_attribute="compose_node_service",
        composition_factory_source_path=(
            "/opt/aletheia/release/aletheia/execution/qualification_node_composition.py"
        ),
        composition_factory_source_sha256="a" * 64,
        composition_factory_owner_uid=0,
        composition_factory_owner_gid=0,
        composition_factory_mode=0o444,
        composition_config_path="/etc/aletheia/services/node.json",
        composition_config_file_sha256="0" * 64,
        composition_config_owner_uid=0,
        composition_config_owner_gid=0,
        composition_config_mode=0o440,
    )
    config = QualificationNodeServiceConfigV1(
        deployment_id=process.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(process),
        database_url_sha256="d" * 64,
        schema_revision="20260829_0028",
        postgresql_role="aletheia_execution_allocator",
        qualification_custody=custody,
        runtime_control_authority_pin=runtime_pin,
        node_authority=node_authority,
        allowed_rate_card_sha256s=terminal.allowed_rate_card_sha256s,
        allowed_currency_codes=terminal.allowed_currency_codes,
        allocator_principal_id="principal:qualification-node-allocator",
        input_materializer_principal_id="principal:qualification-node-input-materializer",
        node_signing_key=_key_pin(
            role="node_signing",
            algorithm="ed25519",
            path="/etc/aletheia/keys/node-signing.key",
            private_key=PRIVATE_KEY,
            key_id=manifest.node_signing_key_id,
            uid=uid,
            gid=gid,
        ),
        assignment_transport_key=_key_pin(
            role="assignment_transport",
            algorithm="x25519",
            path="/etc/aletheia/keys/node-assignment.key",
            private_key=TRANSPORT_PRIVATE_KEY,
            key_id=node_transport_key_id(
                node_authority.assignment_transport_pin.public_key_x25519_hex
            ),
            uid=uid,
            gid=gid,
        ),
        runtime_control_key=_key_pin(
            role="runtime_control",
            algorithm="ed25519",
            path="/etc/aletheia/keys/runtime-control.key",
            private_key=RUNTIME_PRIVATE_KEY,
            key_id=runtime_pin.key_id,
            uid=uid,
            gid=gid,
        ),
        artifact_store_root_pin=_root_pin(
            purpose="artifact_store",
            path=custody.artifact_store_root,
            device=observed_roots["artifact_store"].device,
            inode=observed_roots["artifact_store"].inode,
            uid=uid,
            gid=gid,
            parent_chain_sha256=observed_roots["artifact_store"].parent_chain_sha256,
        ),
        node_state_root_pin=_root_pin(
            purpose="node_state",
            path=deployment_spec.node_state_root,
            device=observed_roots["node_state"].device,
            inode=observed_roots["node_state"].inode,
            uid=uid,
            gid=gid,
            parent_chain_sha256=observed_roots["node_state"].parent_chain_sha256,
        ),
        input_materialization_journal_root_pin=_root_pin(
            purpose="input_materialization_journal",
            path=deployment_spec.input_materialization_journal_root,
            device=observed_roots["input_materialization_journal"].device,
            inode=observed_roots["input_materialization_journal"].inode,
            uid=uid,
            gid=gid,
            parent_chain_sha256=observed_roots["input_materialization_journal"].parent_chain_sha256,
        ),
        runtime_journal_root_pin=_root_pin(
            purpose="runtime_journal",
            path=deployment_spec.runtime_journal_root,
            device=watchdog.journal_root_device,
            inode=watchdog.journal_root_inode,
            uid=uid,
            gid=gid,
            parent_chain_sha256=watchdog.journal_root_parent_chain_sha256,
        ),
        oci_policy=policy,
        image_layout=PinnedOCIImageLayout(
            policy_sha256=policy.policy_sha256,
            layout_root=str(layout_root),
            layout_root_device=1,
            layout_root_inode=2,
            layout_parent_chain_sha256="f" * 64,
            reviewed_launch_gate_executable_sha256=policy.launch_gate_executable_sha256,
            reviewed_launch_gate_protocol_sha256=policy.launch_gate_protocol_sha256,
        ),
        quota_deployment=observation.quota_deployment,
        watchdog_deployment=watchdog,
        launch_specs=(launch,),
        prepared_at=terminal.prepared_at,
    )
    payload = canonical_json_bytes(config)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        process.model_copy(
            update={
                "process_id": None,
                "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ).model_dump(mode="python")
    )
    return process, config


def test_node_config_closes_three_keys_authorities_and_cpu_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _fixture(monkeypatch, tmp_path)

    assert config.cpu_only is True
    assert config.launch_specs[0].launch_spec_sha256 == config.oci_policy.launch_spec_sha256
    assert (
        len(
            {
                config.node_signing_key.key_id,
                config.assignment_transport_key.key_id,
                config.runtime_control_key.key_id,
            }
        )
        == 3
    )
    payload = config.model_dump(mode="python")
    payload["runtime_control_key"] = config.runtime_control_key.model_copy(
        update={"path": config.node_signing_key.path}
    )
    with pytest.raises(ValidationError, match="files and bytes must be distinct"):
        QualificationNodeServiceConfigV1.model_validate(payload)

    payload = config.model_dump(mode="python")
    payload["launch_specs"] = (
        config.launch_specs[0].model_copy(update={"command_sha256": "1" * 64}),
    )
    with pytest.raises(ValidationError, match="launch registry differs"):
        QualificationNodeServiceConfigV1.model_validate(payload)


def test_node_factory_binds_canonical_config_database_and_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, config = _fixture(monkeypatch, tmp_path)
    calls: list[int] = []

    class FakeLoop:
        def __init__(self, worker) -> None:
            assert worker == "worker"

        def run(self, *, poll_milliseconds: int) -> None:
            calls.append(poll_milliseconds)

    monkeypatch.setattr(node_service, "_compose_worker", lambda _config: "worker")
    monkeypatch.setattr(node_service, "QualificationNodeWorkerLoop", FakeLoop)
    monkeypatch.setattr(
        node_service,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://node"),
    )
    monkeypatch.setattr(node_service, "expected_schema_revision", lambda: config.schema_revision)
    monkeypatch.setattr(node_service, "_verify_live_database_binding", lambda _config: None)
    config = config.model_copy(
        update={
            "database_url_sha256": hashlib.sha256(b"postgresql://node").hexdigest(),
        }
    )
    payload = canonical_json_bytes(config)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        process.model_copy(
            update={
                "process_id": None,
                "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ).model_dump(mode="python")
    )

    handlers = compose_node_service(deployment=process, configuration_bytes=payload)
    handlers.handler(poll_milliseconds=process.worker_poll_milliseconds)

    assert handlers.role is QualificationServiceRole.NODE
    assert calls == [process.worker_poll_milliseconds]
    with pytest.raises(QualificationNodeCompositionError, match="another poll"):
        handlers.handler(poll_milliseconds=999)
    with pytest.raises(QualificationNodeCompositionError, match="not canonical"):
        compose_node_service(deployment=process, configuration_bytes=payload + b"\n")


def test_live_database_binding_requires_exact_role_and_single_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _fixture(monkeypatch, tmp_path)

    class Result:
        def __init__(self, row) -> None:
            self.row = row

        def one(self):
            return self.row

    class Session:
        def __init__(self, row) -> None:
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result(self.row)

    monkeypatch.setattr(
        node_service,
        "session_factory",
        lambda: lambda: Session((config.postgresql_role, 1, config.schema_revision)),
    )
    node_service._verify_live_database_binding(config)  # noqa: SLF001

    monkeypatch.setattr(
        node_service,
        "session_factory",
        lambda: lambda: Session(("wrong_role", 1, config.schema_revision)),
    )
    with pytest.raises(QualificationNodeCompositionError, match="role or live schema"):
        node_service._verify_live_database_binding(config)  # noqa: SLF001

    monkeypatch.setattr(
        node_service,
        "session_factory",
        lambda: lambda: Session((config.postgresql_role, 2, config.schema_revision)),
    )
    with pytest.raises(QualificationNodeCompositionError, match="role or live schema"):
        node_service._verify_live_database_binding(config)  # noqa: SLF001


def test_private_key_loader_freshly_binds_bytes_metadata_and_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = (tmp_path / "node.key").resolve()
    key_path.write_bytes(PRIVATE_KEY)
    key_path.chmod(0o400)
    metadata = key_path.stat()
    pin = _key_pin(
        role="node_signing",
        algorithm="ed25519",
        path=str(key_path),
        private_key=PRIVATE_KEY,
        key_id=qualification_key_id(_public_key(PRIVATE_KEY)),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
    )
    monkeypatch.setattr(node_service, "host_parent_chain_sha256", lambda _path: "e" * 64)

    assert node_service._fresh_private_key(pin) == PRIVATE_KEY  # noqa: SLF001

    key_path.chmod(0o600)
    with pytest.raises(QualificationNodeCompositionError, match="custody differs"):
        node_service._fresh_private_key(pin)  # noqa: SLF001


def test_mutable_root_loader_rejects_inode_or_mode_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "node-state").resolve()
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    metadata = root.stat()
    pin = _root_pin(
        purpose="node_state",
        path=str(root),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        parent_chain_sha256="e" * 64,
    )
    monkeypatch.setattr(node_service, "host_parent_chain_sha256", lambda _path: "e" * 64)

    assert node_service._verify_mutable_root(pin) == root  # noqa: SLF001

    root.chmod(0o750)
    with pytest.raises(QualificationNodeCompositionError, match="custody differs"):
        node_service._verify_mutable_root(pin)  # noqa: SLF001


def test_runtime_control_issuer_rejects_key_rebinding_and_exposes_narrow_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _fixture(monkeypatch, tmp_path)
    issuer = PinnedRuntimeControlIssuanceAuthority(
        pin=config.runtime_control_authority_pin,
        private_key=RUNTIME_PRIVATE_KEY,
    )

    assert issuer.authority_pin == config.runtime_control_authority_pin
    assert issuer.authority_verifier.pin == config.runtime_control_authority_pin
    assert not hasattr(issuer, "private_key")
    with pytest.raises(ValueError, match="differs"):
        PinnedRuntimeControlIssuanceAuthority(
            pin=config.runtime_control_authority_pin,
            private_key=b"x" * 32,
        )


def test_worker_loop_stops_only_at_tick_boundary() -> None:
    class FakeWorker(QualificationExecutionWorker):
        def __init__(self) -> None:
            self.calls = 0
            self.loop: QualificationNodeWorkerLoop | None = None

        def tick(self):
            self.calls += 1
            assert self.loop is not None
            self.loop.stop()
            return NodeRunResult(outcome=NodeRunOutcome.IDLE)

    worker = FakeWorker()
    loop = QualificationNodeWorkerLoop(worker)
    worker.loop = loop

    loop.run(poll_milliseconds=50)

    assert worker.calls == 1
    with pytest.raises(QualificationNodeCompositionError, match="outside"):
        QualificationNodeWorkerLoop(worker).run(poll_milliseconds=49)


def test_worker_loop_reports_once_and_backs_off_identical_reconciliation(capsys) -> None:
    diagnostic = "a" * 64

    class FakeStop:
        def __init__(self) -> None:
            self.stopped = False
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, seconds: float) -> None:
            self.waits.append(seconds)

    class FakeWorker(QualificationExecutionWorker):
        def __init__(self) -> None:
            self.calls = 0
            self.loop: QualificationNodeWorkerLoop | None = None

        def tick(self) -> NodeRunResult:
            self.calls += 1
            assert self.loop is not None
            if self.calls == 5:
                self.loop.stop()
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id="iat_" + "1" * 32,
                reconciliation_reason="runtime termination challenge lost exact fence authority",
                allocator_rejection_sha256=diagnostic,
            )

    worker = FakeWorker()
    loop = QualificationNodeWorkerLoop(worker)
    stop = FakeStop()
    loop._stop = stop  # noqa: SLF001 - deterministic wait-port fixture
    worker.loop = loop

    loop.run(poll_milliseconds=50)

    assert worker.calls == 5
    assert stop.waits == [0.05, 0.1, 0.2, 0.4, 0.8]
    emitted = capsys.readouterr().out.splitlines()
    assert len(emitted) == 1
    receipt = json.loads(emitted[0])
    assert receipt == {
        "allocator_rejection_sha256": diagnostic,
        "attempt_id": "iat_" + "1" * 32,
        "qualification_only": True,
        "reason": "runtime termination challenge lost exact fence authority",
        "retry_backoff_max_milliseconds": 30_000,
        "schema_name": "aletheia.qualification_node_reconciliation_status",
        "schema_version": 1,
        "scientific_admission_allowed": False,
    }


def test_quota_client_requests_independent_root_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _fixture(monkeypatch, tmp_path)
    client = LoopbackOutputQuotaProvisionerClient(config.quota_deployment)
    requests: list[dict[str, object]] = []
    evidence = "9" * 64

    def request(payload):
        requests.append(dict(payload))
        return (
            {
                "schema": "aletheia.loopback_output_quota_verification_response.v1",
                "deployment_sha256": config.quota_deployment.deployment_sha256,
                "service_pid": 81,
                "evidence_sha256": evidence,
            },
            81,
        )

    monkeypatch.setattr(client, "_request", request)

    assert (
        client.verify_enforced_quota(
            output_root=Path("/var/lib/aletheia/qualification/workspaces/attempt/output"),
            output_quota_bytes=1024,
            execution_id="exe_" + "1" * 32,
            infrastructure_attempt_id="iat_" + "2" * 32,
            runtime_id="runtime:test",
            expected_evidence_sha256=evidence,
        )
        == evidence
    )
    assert requests == [
        {
            "operation": "verify",
            "output_root": "/var/lib/aletheia/qualification/workspaces/attempt/output",
            "output_quota_bytes": 1024,
            "execution_id": "exe_" + "1" * 32,
            "infrastructure_attempt_id": "iat_" + "2" * 32,
            "runtime_id": "runtime:test",
            "expected_evidence_sha256": evidence,
        }
    ]

    monkeypatch.setattr(
        client,
        "_request",
        lambda _payload: (
            {
                "schema": "aletheia.loopback_output_quota_verification_response.v1",
                "deployment_sha256": config.quota_deployment.deployment_sha256,
                "service_pid": 81,
                "evidence_sha256": evidence,
                "untrusted": True,
            },
            81,
        ),
    )
    with pytest.raises(OCIOutputQuotaError, match="differs"):
        client.verify_enforced_quota(
            output_root=Path("/var/lib/aletheia/qualification/workspaces/attempt/output"),
            output_quota_bytes=1024,
            execution_id="exe_" + "1" * 32,
            infrastructure_attempt_id="iat_" + "2" * 32,
            runtime_id="runtime:test",
            expected_evidence_sha256=evidence,
        )


def test_quota_root_service_dispatches_only_exact_verification_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(oci_deployment.socket, "SO_PEERCRED", 17, raising=False)
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = config.quota_deployment  # noqa: SLF001
    evidence = "8" * 64
    calls: list[dict[str, object]] = []

    class Verifier:
        def verify_enforced_quota(self, **scope):
            calls.append(scope)
            return evidence

    service._verification_controller = Verifier()  # noqa: SLF001
    request = {
        "operation": "verify",
        "output_root": "/var/lib/aletheia/qualification/workspaces/attempt/output",
        "output_quota_bytes": 1024,
        "execution_id": "exe_" + "1" * 32,
        "infrastructure_attempt_id": "iat_" + "2" * 32,
        "runtime_id": "runtime:test",
        "expected_evidence_sha256": evidence,
    }
    monkeypatch.setattr(service, "_receive_line", lambda _connection: canonical_json_bytes(request))

    class Connection:
        sent = b""

        def getsockopt(self, *_args):
            return (
                (1).to_bytes(4, sys.byteorder, signed=True)
                + config.quota_deployment.allowed_client_uid.to_bytes(4, sys.byteorder, signed=True)
                + config.quota_deployment.allowed_client_gid.to_bytes(4, sys.byteorder, signed=True)
            )

        def sendall(self, payload: bytes) -> None:
            self.sent = payload

    connection = Connection()
    service._serve_connection(connection)  # noqa: SLF001

    response = json.loads(connection.sent)
    assert response["evidence_sha256"] == evidence
    assert calls[0]["runtime_id"] == "runtime:test"

    request["extra"] = "forbidden"
    with pytest.raises(OCIOutputQuotaError, match="fields are not exact"):
        service._serve_connection(Connection())  # noqa: SLF001
