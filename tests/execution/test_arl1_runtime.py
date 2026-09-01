from __future__ import annotations

import hashlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.arl1_campaign import (
    ARL1ProtocolCampaignPending,
    ARL1ProtocolCampaignRequestV1,
    ARL1ProtocolCampaignService,
)
from aletheia.arl1_runtime import (
    ARL1CampaignRPCServiceSetV1,
    ARL1CampaignRuntimeConfigV1,
    ARL1CampaignRuntimeDeploymentV1,
    ARL1EvidenceArchiveRuntimeConfigV1,
    ARL1RuntimeError,
    compose_arl1_campaign_service,
    execute_arl1_campaign_deployment,
    load_arl1_campaign_runtime_deployment,
    load_arl1_campaign_runtime_inputs,
)
from aletheia.arl1_verifier import LocalARL1EvidenceArchive
from aletheia.observations.scientific_bridge import VerifiedExecutionAuthorityProjection
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
)
from aletheia.research_controller_rpc_runtime import ControllerWorkerRPCServerDeployment
from aletheia.research_controller.step_executor import ControllerStepAuthorityRole
from aletheia.research_kernel.schemas import canonical_json_bytes

_CONTROLLER_TESTS = Path(__file__).resolve().parents[1] / "research_controller"
_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
for _test_root in (_CONTROLLER_TESTS, _OBSERVATION_TESTS):
    if str(_test_root) not in sys.path:
        sys.path.insert(0, str(_test_root))

from test_scientific_bridge import _replicate_bridge_cases  # noqa: E402
from test_worker_composition import _worker_config  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _standalone_campaign_request() -> ARL1ProtocolCampaignRequestV1:
    cases = _replicate_bridge_cases()
    first = cases[0].authorization.message
    binding = first.action_protocol_binding
    return ARL1ProtocolCampaignRequestV1(
        domain_scope="bounded_grouped_regression",
        modality_scope="cpu_computational",
        compilation_request=binding.compilation_request,
        compilation_result=binding.compilation_result,
        work_order_node_id=binding.work_order_node.node_id,
        authorizations=tuple(case.authorization for case in cases),
        primary_scientific_slot_id=first.scientific_slot_id,
        requested_at=first.authorized_at,
    )


def _campaign_only_pin(pin: ControllerWorkerRPCServicePin) -> ControllerWorkerRPCServicePin:
    return ControllerWorkerRPCServicePin.model_validate(
        {
            **pin.model_dump(mode="python", exclude={"service_id"}),
            "operations": (ControllerWorkerRPCOperation.REGISTER_EXECUTION_CAMPAIGN,),
        }
    )


def _external_server_pin(
    pin: ControllerWorkerRPCServicePin,
    *,
    server_uid: int,
) -> ControllerWorkerRPCServicePin:
    return ControllerWorkerRPCServicePin.model_validate(
        {
            **pin.model_dump(mode="python", exclude={"service_id"}),
            "socket_owner_uid": server_uid,
            "peer_uid": server_uid,
        }
    )


def _runtime_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    worker, controller_manifest = _worker_config(monkeypatch, tmp_path)
    bindings = {
        binding.role: binding
        for adapter in worker.adapter_set_manifest.adapters
        for binding in adapter.authorities
    }
    server_uid = os.geteuid() + 1
    services = ARL1CampaignRPCServiceSetV1(
        execution_registration=_external_server_pin(
            _campaign_only_pin(worker.rpc_services.execution_registration),
            server_uid=server_uid,
        ),
        raw_run_source=_external_server_pin(
            worker.rpc_services.raw_run_source,
            server_uid=server_uid,
        ),
        database_observation=_external_server_pin(
            worker.rpc_services.database_observation,
            server_uid=server_uid,
        ),
        independent_validation=_external_server_pin(
            worker.rpc_services.independent_validation,
            server_uid=server_uid,
        ),
        independent_admission=_external_server_pin(
            worker.rpc_services.independent_admission,
            server_uid=server_uid,
        ),
        atomic_admission=_external_server_pin(
            worker.rpc_services.atomic_admission,
            server_uid=server_uid,
        ),
    )
    archive_root = tmp_path / "arl1-runtime-archive"
    archive_root.mkdir(mode=0o700)
    archive_root.chmod(0o700)
    archive_metadata = archive_root.stat()
    repository_root = Path(__file__).resolve().parents[2]
    campaign_source = (repository_root / "aletheia/arl1_campaign.py").resolve()
    verifier_source = (repository_root / "aletheia/arl1_verifier.py").resolve()
    config = ARL1CampaignRuntimeConfigV1(
        process_principal_id=worker.process_principal_id,
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        controller_id=worker.controller_id,
        controller_manifest_sha256=worker.controller_manifest_sha256,
        database_url_sha256=worker.database_url_sha256,
        schema_revision=worker.schema_revision,
        authority_bindings=tuple(
            bindings[role]
            for role in sorted(
                (
                    ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
                    ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                    ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
                    ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                    ControllerStepAuthorityRole.KERNEL_COMMAND,
                ),
                key=lambda item: item.value,
            )
        ),
        rpc_services=services,
        kernel_reader=worker.kernel_reader,
        qualification_reader=worker.terminal_reader,
        allocator_authority=VerifiedExecutionAuthorityProjection(
            principal_id=worker.terminal_reader.allocator_principal_id,
            key_id=_sha("allocator-runtime-key"),
            policy_sha256=_sha("allocator-runtime-policy"),
        ),
        artifact_authority=VerifiedExecutionAuthorityProjection(
            principal_id=worker.terminal_reader.artifact_verifier_principal_id,
            key_id=_sha("artifact-runtime-key"),
            policy_sha256=_sha("artifact-runtime-policy"),
        ),
        evidence_archive=ARL1EvidenceArchiveRuntimeConfigV1(
            root=str(archive_root.resolve()),
            owner_uid=archive_metadata.st_uid,
            group_gid=archive_metadata.st_gid,
            device_id=archive_metadata.st_dev,
            inode=archive_metadata.st_ino,
            directory_mode=0o700,
            object_mode=0o400,
            max_object_bytes=64 * 1024**2,
        ),
        campaign_implementation_source_path=str(campaign_source),
        campaign_implementation_source_sha256=hashlib.sha256(
            campaign_source.read_bytes()
        ).hexdigest(),
        verifier_implementation_source_path=str(verifier_source),
        verifier_implementation_source_sha256=hashlib.sha256(
            verifier_source.read_bytes()
        ).hexdigest(),
        prepared_at=worker.prepared_at,
    )
    return config, controller_manifest


class _Custody:
    def verify_raw_run_custody(self, **_kwargs):  # pragma: no cover - compose only
        raise AssertionError("composition must not read custody before campaign execution")


class _Kernel:
    def audit(self, *_args, **_kwargs):  # pragma: no cover - compose only
        raise AssertionError("composition must not audit before campaign execution")


def test_keyless_runtime_composes_only_campaign_rpc_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    archive = LocalARL1EvidenceArchive(Path(config.evidence_archive.root))

    service = compose_arl1_campaign_service(
        config,
        raw_run_custody=_Custody(),
        kernel_store=_Kernel(),
        archive=archive,
    )

    assert type(service) is ARL1ProtocolCampaignService
    assert config.private_signing_key_loaded is False
    assert config.autonomous_research_design_allowed is False
    observed = {
        operation for _name, pin in config.rpc_services.named_pins for operation in pin.operations
    }
    assert ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL not in observed
    assert ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION not in observed
    assert ControllerWorkerRPCOperation.DERIVE_CONTINUATION not in observed


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {"peer_uid": os.geteuid(), "socket_owner_uid": os.geteuid()},
            "UID-separated",
        ),
        (
            {
                "peer_gid": os.getegid() + 1,
                "socket_group_gid": os.getegid() + 1,
            },
            "campaign socket GID",
        ),
        ({"socket_mode": 0o600}, "campaign socket GID"),
    ),
)
def test_runtime_rejects_unreachable_or_same_uid_rpc_server_peer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    updates: dict[str, int],
    message: str,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    payload = config.model_dump(mode="python", exclude={"configuration_id"})
    pin = payload["rpc_services"]["raw_run_source"]
    pin.pop("service_id", None)
    pin.update(updates)

    with pytest.raises(ValueError, match=message):
        ARL1CampaignRuntimeConfigV1.model_validate(payload)


def test_runtime_peer_identity_can_form_one_service_per_process_deployment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    pin = config.rpc_services.raw_run_source
    repository_root = Path(__file__).resolve().parents[2]
    factory = (repository_root / "aletheia/research_controller_raw_run_source_runtime.py").resolve()
    socket_parent = Path(pin.socket_path).parent
    socket_parent_metadata = socket_parent.stat()

    deployment = ControllerWorkerRPCServerDeployment(
        service_pin=pin,
        controller_id=config.controller_id,
        controller_manifest_sha256=config.controller_manifest_sha256,
        worker_process_principal_id=config.process_principal_id,
        worker_peer_uid=config.process_uid,
        worker_peer_gid=config.process_gid,
        process_uid=pin.peer_uid,
        process_gid=pin.peer_gid,
        socket_parent_path=str(socket_parent),
        socket_parent_owner_uid=pin.peer_uid,
        socket_parent_owner_gid=pin.peer_gid,
        socket_parent_mode=0o710,
        socket_parent_device_id=socket_parent_metadata.st_dev,
        socket_parent_inode=socket_parent_metadata.st_ino,
        receipt_private_key_path=str((tmp_path / "raw-run-receipt.key").resolve()),
        receipt_private_key_sha256=_sha("raw-run-receipt-key"),
        reviewed_code_root=str(repository_root),
        composition_factory_module="aletheia.research_controller_raw_run_source_runtime",
        composition_factory_attribute="build_raw_run_source_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str((tmp_path / "raw-run-config.json").resolve()),
        composition_config_file_sha256=_sha("raw-run-config"),
        prepared_at=config.prepared_at,
    )

    assert deployment.process_uid == pin.peer_uid
    assert deployment.worker_peer_uid == config.process_uid
    assert deployment.worker_peer_uid != deployment.process_uid
    assert deployment.worker_peer_gid == deployment.process_gid == config.process_gid


def test_runtime_manifest_freshly_binds_config_and_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    request = _standalone_campaign_request()
    config_path = (tmp_path / "arl1-runtime.json").resolve()
    request_path = (tmp_path / "arl1-request.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    request_path.write_bytes(canonical_json_bytes(request))
    deployment = ARL1CampaignRuntimeDeploymentV1(
        configuration_path=str(config_path),
        configuration_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        configuration_sha256=config.configuration_sha256,
        request_path=str(request_path),
        request_file_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
        request_sha256=request.request_sha256,
        process_principal_id=config.process_principal_id,
        process_uid=config.process_uid,
        process_gid=config.process_gid,
        prepared_at=config.prepared_at,
    )
    deployment_path = (tmp_path / "arl1-deployment.json").resolve()
    deployment_path.write_bytes(canonical_json_bytes(deployment))
    deployment_sha256 = hashlib.sha256(deployment_path.read_bytes()).hexdigest()

    loaded = load_arl1_campaign_runtime_deployment(
        deployment_path,
        expected_file_sha256=deployment_sha256,
    )
    loaded_config, loaded_request = load_arl1_campaign_runtime_inputs(loaded)

    assert loaded == deployment
    assert loaded_config == config
    assert loaded_request == request

    request_path.write_bytes(request_path.read_bytes() + b"\n")
    with pytest.raises(ARL1RuntimeError, match="byte pin"):
        load_arl1_campaign_runtime_inputs(loaded)


def test_runtime_refuses_non_linux_execution_before_opening_authority_ports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    deployment = ARL1CampaignRuntimeDeploymentV1(
        configuration_path=str((tmp_path / "missing-config.json").resolve()),
        configuration_file_sha256="1" * 64,
        configuration_sha256=config.configuration_sha256,
        request_path=str((tmp_path / "missing-request.json").resolve()),
        request_file_sha256="2" * 64,
        request_sha256="3" * 64,
        process_principal_id=config.process_principal_id,
        process_uid=config.process_uid,
        process_gid=config.process_gid,
        prepared_at=config.prepared_at,
    )
    monkeypatch.setattr("aletheia.arl1_runtime.sys.platform", "darwin")

    with pytest.raises(ARL1RuntimeError, match="requires Linux"):
        execute_arl1_campaign_deployment(deployment)


def test_programmatic_runtime_requires_exact_schema_before_loading_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    deployment = ARL1CampaignRuntimeDeploymentV1(
        configuration_path=str((tmp_path / "missing-config.json").resolve()),
        configuration_file_sha256="1" * 64,
        configuration_sha256=config.configuration_sha256,
        request_path=str((tmp_path / "missing-request.json").resolve()),
        request_file_sha256="2" * 64,
        request_sha256="3" * 64,
        process_principal_id=config.process_principal_id,
        process_uid=config.process_uid,
        process_gid=config.process_gid,
        prepared_at=config.prepared_at,
    )
    monkeypatch.setattr("aletheia.arl1_runtime.sys.platform", "linux")
    monkeypatch.setattr("aletheia.arl1_runtime.os.geteuid", lambda: deployment.process_uid)
    monkeypatch.setattr("aletheia.arl1_runtime.os.getegid", lambda: deployment.process_gid)

    def reject_schema():
        raise RuntimeError("schema drift sentinel")

    monkeypatch.setattr("aletheia.arl1_runtime.require_schema_exact", reject_schema)

    with pytest.raises(RuntimeError, match="schema drift sentinel"):
        execute_arl1_campaign_deployment(deployment)


def test_linux_runtime_boundedly_retries_only_typed_terminal_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    request = _standalone_campaign_request()
    deployment = ARL1CampaignRuntimeDeploymentV1(
        configuration_path=str((tmp_path / "runtime.json").resolve()),
        configuration_file_sha256="1" * 64,
        configuration_sha256=config.configuration_sha256,
        request_path=str((tmp_path / "request.json").resolve()),
        request_file_sha256="2" * 64,
        request_sha256=request.request_sha256,
        process_principal_id=config.process_principal_id,
        process_uid=config.process_uid,
        process_gid=config.process_gid,
        prepared_at=config.prepared_at,
    )
    expected = object()

    class _PendingService:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, observed_request):
            assert observed_request == request
            self.calls += 1
            if self.calls <= 2:
                raise ARL1ProtocolCampaignPending(
                    scientific_slot_id=request.primary_scientific_slot_id,
                    pending_code="raw_run:terminal_material_pending",
                    retry_after_milliseconds=250,
                )
            return expected

    service = _PendingService()
    observed_at = [request.requested_at + timedelta(seconds=1)]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        observed_at[0] += timedelta(seconds=seconds)

    monkeypatch.setattr("aletheia.arl1_runtime.sys.platform", "linux")
    monkeypatch.setattr("aletheia.arl1_runtime.os.geteuid", lambda: deployment.process_uid)
    monkeypatch.setattr("aletheia.arl1_runtime.os.getegid", lambda: deployment.process_gid)
    monkeypatch.setattr("aletheia.arl1_runtime.require_schema_exact", lambda: None)
    monkeypatch.setattr(
        "aletheia.arl1_runtime.load_arl1_campaign_runtime_inputs",
        lambda _deployment: (config, request),
    )
    monkeypatch.setattr(
        "aletheia.arl1_runtime.compose_arl1_campaign_service",
        lambda _config, *, clock: service,
    )

    result = execute_arl1_campaign_deployment(
        deployment,
        clock=lambda: observed_at[0],
        sleeper=sleep,
    )

    assert result is expected
    assert service.calls == 3
    assert sleeps == [0.25, 0.25]


def test_linux_runtime_stops_pending_retries_at_signed_admission_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    request = _standalone_campaign_request()
    deployment = ARL1CampaignRuntimeDeploymentV1(
        configuration_path=str((tmp_path / "runtime.json").resolve()),
        configuration_file_sha256="1" * 64,
        configuration_sha256=config.configuration_sha256,
        request_path=str((tmp_path / "request.json").resolve()),
        request_file_sha256="2" * 64,
        request_sha256=request.request_sha256,
        process_principal_id=config.process_principal_id,
        process_uid=config.process_uid,
        process_gid=config.process_gid,
        prepared_at=config.prepared_at,
    )

    class _PendingService:
        def execute(self, _request):
            raise ARL1ProtocolCampaignPending(
                scientific_slot_id=request.primary_scientific_slot_id,
                pending_code="raw_run:terminal_material_pending",
                retry_after_milliseconds=250,
            )

    monkeypatch.setattr("aletheia.arl1_runtime.sys.platform", "linux")
    monkeypatch.setattr("aletheia.arl1_runtime.os.geteuid", lambda: deployment.process_uid)
    monkeypatch.setattr("aletheia.arl1_runtime.os.getegid", lambda: deployment.process_gid)
    monkeypatch.setattr("aletheia.arl1_runtime.require_schema_exact", lambda: None)
    monkeypatch.setattr(
        "aletheia.arl1_runtime.load_arl1_campaign_runtime_inputs",
        lambda _deployment: (config, request),
    )
    monkeypatch.setattr(
        "aletheia.arl1_runtime.compose_arl1_campaign_service",
        lambda _config, *, clock: _PendingService(),
    )

    with pytest.raises(ARL1RuntimeError, match="admission deadline"):
        execute_arl1_campaign_deployment(
            deployment,
            clock=lambda: request.authorizations[0].message.observation_admission_deadline,
            sleeper=lambda _seconds: pytest.fail("deadline must prevent sleeping"),
        )
