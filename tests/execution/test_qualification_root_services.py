from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

import aletheia.execution.oci_deployment as oci_deployment
import aletheia.execution.qualification_root_services as root_services
import aletheia.qualification_service_runtime as service_runtime
from aletheia.execution.qualification_deployment import (
    QualificationLinuxDeploymentObservation,
)
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.qualification_service_runtime import (
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.qualification_quota_composition import build_quota_service
from aletheia.execution.qualification_watchdog_composition import build_watchdog_service
from aletheia.execution.qualification_workspace_composition import build_workspace_service

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_oci_runtime import _policy as _oci_policy  # noqa: E402
from test_qualification_deployment import _observation, _spec  # noqa: E402


def _process(role: QualificationServiceRole) -> QualificationServiceProcessDeploymentV1:
    module = f"aletheia.execution.qualification_{role.value}_composition"
    return QualificationServiceProcessDeploymentV1(
        deployment_id="qualification:prod",
        role=role,
        operation={
            QualificationServiceRole.WORKSPACE: "ensure-shared-workspace",
            QualificationServiceRole.QUOTA: "serve",
            QualificationServiceRole.WATCHDOG: "serve",
        }[role],
        process_uid=0,
        process_gid=0,
        reviewed_code_root="/opt/aletheia/release",
        composition_factory_module=module,
        composition_factory_attribute=f"build_{role.value}_service",
        composition_factory_source_path=(f"/opt/aletheia/release/{module.replace('.', '/')}.py"),
        composition_factory_source_sha256="a" * 64,
        composition_factory_owner_uid=0,
        composition_factory_owner_gid=0,
        composition_factory_mode=0o444,
        composition_config_path=f"/etc/aletheia/services/{role.value}.json",
        composition_config_file_sha256="b" * 64,
        composition_config_owner_uid=0,
        composition_config_owner_gid=0,
        composition_config_mode=0o400,
    )


def _workspace_deployment(
    observation: QualificationLinuxDeploymentObservation,
) -> oci_deployment.SharedOutputWorkspaceDeploymentPin:
    spec = _spec()
    custody = {item.purpose: item for item in observation.custody_roots}["workspace_source"]
    modules = {item.path: item for item in observation.service_module_files}
    service_module = modules[spec.expected_quota_service_module.path]
    return oci_deployment.SharedOutputWorkspaceDeploymentPin(
        deployment_id=f"{spec.deployment_id}:workspace",
        systemd_unit_name=spec.workspace_unit_name,
        service_executable=observation.python_executable,
        mount=observation.quota_deployment.mount,
        service_module_sha256=service_module.sha256,
        service_module_device=service_module.device,
        service_module_inode=service_module.inode,
        service_module_mode=service_module.mode,
        service_module_parent_chain_sha256=service_module.parent_chain_sha256,
        source_root=spec.workspace_source_root,
        source_root_device=custody.device,
        source_root_inode=custody.inode,
        source_root_owner_gid=spec.node_gid,
        source_root_parent_chain_sha256=custody.parent_chain_sha256,
        target_root=spec.output_workspace_root,
        target_underlay_device=41,
        target_underlay_inode=501,
        target_parent_chain_sha256=observation.output_workspace_root.parent_chain_sha256,
    )


def _configs(tmp_path: Path):
    spec = _spec()
    observation = _observation(spec)
    workspace_process = _process(QualificationServiceRole.WORKSPACE)
    quota_process = _process(QualificationServiceRole.QUOTA)
    watchdog_process = _process(QualificationServiceRole.WATCHDOG)
    policy = _oci_policy(tmp_path).model_copy(
        update={"workload_uid": spec.node_uid, "workload_gid": spec.node_gid}
    )
    watchdog = observation.watchdog_deployment.model_copy(
        update={
            "policy_sha256": policy.policy_sha256,
            "allowed_client_uid": policy.workload_uid,
            "allowed_client_gid": policy.workload_gid,
        }
    )
    return (
        workspace_process,
        root_services.QualificationWorkspaceServiceConfigV1(
            deployment_id=workspace_process.deployment_id,
            process_config_binding_sha256=qualification_service_process_config_binding_sha256(
                workspace_process
            ),
            workspace_deployment=_workspace_deployment(observation),
        ),
        quota_process,
        root_services.QualificationQuotaServiceConfigV2(
            deployment_id=quota_process.deployment_id,
            process_config_binding_sha256=qualification_service_process_config_binding_sha256(
                quota_process
            ),
            oci_policy=policy,
            runtime_journal_root=spec.runtime_journal_root,
            quota_deployment=observation.quota_deployment,
        ),
        watchdog_process,
        root_services.QualificationWatchdogServiceConfigV1(
            deployment_id=watchdog_process.deployment_id,
            process_config_binding_sha256=qualification_service_process_config_binding_sha256(
                watchdog_process
            ),
            oci_policy=policy,
            watchdog_deployment=watchdog,
        ),
    )


@pytest.mark.parametrize(
    ("role", "factory_name", "service_name", "method_name", "pair_index"),
    (
        (QualificationServiceRole.WORKSPACE, "workspace", "workspace", "ensure", 0),
        (QualificationServiceRole.QUOTA, "quota", "quota", "serve", 2),
        (QualificationServiceRole.WATCHDOG, "watchdog", "watchdog", "serve", 4),
    ),
)
def test_root_factories_bind_exact_process_and_one_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: QualificationServiceRole,
    factory_name: str,
    service_name: str,
    method_name: str,
    pair_index: int,
) -> None:
    values = _configs(tmp_path)
    process = values[pair_index]
    config = values[pair_index + 1]
    calls: list[str] = []

    class FakeService:
        def __init__(self, *args, **kwargs) -> None:
            assert args or kwargs

        def ensure_shared_workspace(self) -> None:
            calls.append("ensure")

        def serve_forever(self) -> None:
            calls.append("serve")

    monkeypatch.setattr(
        root_services,
        {
            "workspace": "SharedOutputWorkspaceService",
            "quota": "LoopbackOutputQuotaProvisioningService",
            "watchdog": "DurableDeadlineWatchdogService",
        }[service_name],
        FakeService,
    )
    factory: Callable[..., object] = {
        "workspace": build_workspace_service,
        "quota": build_quota_service,
        "watchdog": build_watchdog_service,
    }[factory_name]
    handlers = factory(deployment=process, configuration_bytes=canonical_json_bytes(config))
    assert handlers.role is role
    assert handlers.operation == process.operation
    handlers.handler(poll_milliseconds=None)
    assert calls == [method_name]


def test_root_factory_rejects_noncanonical_duplicate_and_rebound_config(tmp_path: Path) -> None:
    process, config, *_rest = _configs(tmp_path)
    payload = canonical_json_bytes(config)
    with pytest.raises(
        root_services.QualificationRootServiceCompositionError,
        match="not canonical",
    ):
        build_workspace_service(deployment=process, configuration_bytes=payload + b"\n")
    duplicate = payload.replace(
        b"{",
        b'{"schema_version":1,',
        1,
    )
    with pytest.raises(
        root_services.QualificationRootServiceCompositionError,
        match="invalid",
    ):
        build_workspace_service(deployment=process, configuration_bytes=duplicate)
    variant = config.model_copy(update={"process_config_binding_sha256": "f" * 64})
    with pytest.raises(
        root_services.QualificationRootServiceCompositionError,
        match="differs",
    ):
        build_workspace_service(
            deployment=process,
            configuration_bytes=canonical_json_bytes(variant),
        )


def test_process_config_binding_has_no_config_digest_self_reference() -> None:
    process = _process(QualificationServiceRole.WORKSPACE)
    binding = qualification_service_process_config_binding_sha256(process)
    assert binding == "cb8daae90ab1edf7d88183ec52c5c95e85742bb32848761c8c2c7eb7b145c4bb"
    replaced = QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            "composition_config_file_sha256": "c" * 64,
        }
    )
    assert replaced.identity_sha256 != process.identity_sha256
    assert qualification_service_process_config_binding_sha256(replaced) == binding
    rebound = QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            "composition_config_path": "/etc/aletheia/services/variant.json",
        }
    )
    assert qualification_service_process_config_binding_sha256(rebound) != binding


def test_root_service_deployments_do_not_self_pin_future_systemd_inodes(
    tmp_path: Path,
) -> None:
    values = _configs(tmp_path)
    deployments = (
        values[1].workspace_deployment,
        values[3].quota_deployment,
        values[5].watchdog_deployment,
    )
    assert all(item.schema_version == 2 for item in deployments)
    assert all("systemd_unit" not in type(item).model_fields for item in deployments)
    for item in deployments:
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            type(item).model_validate(
                {
                    **item.model_dump(mode="python"),
                    "systemd_unit": {"path": f"/etc/systemd/system/{item.systemd_unit_name}"},
                }
            )


def test_guarded_loader_accepts_final_config_hash_without_self_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = (tmp_path / "release").resolve()
    source = release / "aletheia/execution/qualification_workspace_composition.py"
    source.parent.mkdir(parents=True)
    installed_source = Path("aletheia/execution/qualification_workspace_composition.py").resolve()
    source.write_bytes(installed_source.read_bytes())
    source.chmod(0o444)
    config_path = (tmp_path / "config/workspace.json").resolve()
    config_path.parent.mkdir(parents=True)
    source_metadata = source.stat()
    prototype = QualificationServiceProcessDeploymentV1(
        deployment_id="qualification:prod",
        role=QualificationServiceRole.WORKSPACE,
        operation="ensure-shared-workspace",
        process_uid=0,
        process_gid=0,
        reviewed_code_root=str(release),
        composition_factory_module="aletheia.execution.qualification_workspace_composition",
        composition_factory_attribute="build_workspace_service",
        composition_factory_source_path=str(source),
        composition_factory_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        composition_factory_owner_uid=source_metadata.st_uid,
        composition_factory_owner_gid=source_metadata.st_gid,
        composition_factory_mode=0o444,
        composition_config_path=str(config_path),
        composition_config_file_sha256="0" * 64,
        composition_config_owner_uid=os.geteuid(),
        composition_config_owner_gid=os.getegid(),
        composition_config_mode=0o400,
    )
    _old_process, old_config, *_rest = _configs(tmp_path / "pins")
    config = root_services.QualificationWorkspaceServiceConfigV1(
        deployment_id=prototype.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(
            prototype
        ),
        workspace_deployment=old_config.workspace_deployment,
    )
    payload = canonical_json_bytes(config)
    config_path.write_bytes(payload)
    config_path.chmod(0o400)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        {
            **prototype.model_dump(mode="python", exclude={"process_id"}),
            "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )

    class FakeWorkspace:
        def __init__(self, deployment) -> None:
            assert deployment == config.workspace_deployment

        def ensure_shared_workspace(self) -> None:
            return None

    monkeypatch.setattr(root_services, "SharedOutputWorkspaceService", FakeWorkspace)
    handlers = service_runtime._load_handler_set(process)  # noqa: SLF001
    assert handlers.role is QualificationServiceRole.WORKSPACE


def test_workspace_pin_rejects_overlapping_roots(tmp_path: Path) -> None:
    _process_value, config, *_rest = _configs(tmp_path)
    raw = config.workspace_deployment.model_dump(mode="python")
    raw["target_root"] = f"{config.workspace_deployment.source_root}/nested"
    with pytest.raises(ValueError, match="overlap"):
        oci_deployment.SharedOutputWorkspaceDeploymentPin.model_validate(raw)


def test_workspace_mountinfo_parser_and_shared_marker_are_closed() -> None:
    parsed = oci_deployment.SharedOutputWorkspaceService._parse_mountinfo(  # noqa: SLF001
        "401 1 8:2 / /srv/aletheia/output rw,nosuid shared:77 - ext4 /dev/sda2 rw"
    )
    assert parsed["mountpoint"] == "/srv/aletheia/output"
    assert oci_deployment.SharedOutputWorkspaceService._is_shared(parsed)  # noqa: SLF001
    assert not oci_deployment.SharedOutputWorkspaceService._is_shared(  # noqa: SLF001
        {**parsed, "optional_fields": ("master:12",)}
    )
    with pytest.raises(oci_deployment.OCISharedWorkspaceError, match="unparseable"):
        oci_deployment.SharedOutputWorkspaceService._parse_mountinfo("malformed")  # noqa: SLF001


def test_workspace_bound_target_rechecks_exact_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process_value, config, *_rest = _configs(tmp_path)
    deployment = config.workspace_deployment
    service = oci_deployment.SharedOutputWorkspaceService(deployment)
    source_metadata = SimpleNamespace(
        st_mode=stat.S_IFDIR | deployment.source_root_mode,
        st_dev=deployment.source_root_device,
        st_ino=deployment.source_root_inode,
        st_uid=0,
        st_gid=deployment.source_root_owner_gid,
    )
    target_metadata = SimpleNamespace(**vars(source_metadata))
    metadata = {
        deployment.source_root: source_metadata,
        deployment.target_root: target_metadata,
    }
    parent_hashes = {
        deployment.source_root: deployment.source_root_parent_chain_sha256,
        deployment.target_root: deployment.target_parent_chain_sha256,
    }
    monkeypatch.setattr(Path, "lstat", lambda path: metadata[str(path)])
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(
        oci_deployment,
        "host_parent_chain_sha256",
        lambda path: parent_hashes[str(path)],
    )
    mount = {
        "mountpoint": deployment.target_root,
        "mount_options": frozenset({"rw"}),
        "optional_fields": ("shared:99",),
        "major": os.major(source_metadata.st_dev),
        "minor": os.minor(source_metadata.st_dev),
    }
    service._verify_bound_target(  # noqa: SLF001
        source=Path(deployment.source_root),
        target=Path(deployment.target_root),
        mount=mount,
    )
    metadata[deployment.source_root] = SimpleNamespace(
        **{**vars(source_metadata), "st_ino": source_metadata.st_ino + 1}
    )
    with pytest.raises(oci_deployment.OCISharedWorkspaceError, match="exact source bind"):
        service._verify_bound_target(  # noqa: SLF001
            source=Path(deployment.source_root),
            target=Path(deployment.target_root),
            mount=mount,
        )


def test_workspace_one_shot_binds_then_promotes_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, config, *_rest = _configs(tmp_path)
    del process
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    workspace = config.workspace_deployment.model_copy(
        update={"source_root": str(source), "target_root": str(target)}
    )
    service = oci_deployment.SharedOutputWorkspaceService(workspace)
    nonshared = {
        "mountpoint": str(target),
        "mount_options": frozenset({"rw"}),
        "optional_fields": (),
    }
    shared = {**nonshared, "optional_fields": ("shared:99",)}
    observed = iter((None, nonshared, shared))
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_require_root_systemd_service", lambda: None)
    monkeypatch.setattr(oci_deployment, "_verify_pinned_directory", lambda *a, **k: None)
    monkeypatch.setattr(service, "_find_target_mount", lambda _target: next(observed))
    monkeypatch.setattr(service, "_verify_bound_target", lambda **_scope: None)
    monkeypatch.setattr(service, "_run_mount", commands.append)
    service.ensure_shared_workspace()
    assert commands == [
        ("--bind", str(source), str(target)),
        ("--make-shared", str(target)),
    ]

    recovery = oci_deployment.SharedOutputWorkspaceService(workspace)
    recovered = iter((nonshared, shared))
    commands.clear()
    monkeypatch.setattr(recovery, "_require_root_systemd_service", lambda: None)
    monkeypatch.setattr(recovery, "_find_target_mount", lambda _target: next(recovered))
    monkeypatch.setattr(recovery, "_verify_bound_target", lambda **_scope: None)
    monkeypatch.setattr(recovery, "_run_mount", commands.append)
    recovery.ensure_shared_workspace()
    assert commands == [("--make-shared", str(target))]


def test_workspace_first_bind_rejects_nonempty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process_value, config, *_rest = _configs(tmp_path)
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / "unexpected").write_text("occupied", encoding="utf-8")
    workspace = config.workspace_deployment.model_copy(
        update={"source_root": str(source), "target_root": str(target)}
    )
    service = oci_deployment.SharedOutputWorkspaceService(workspace)
    monkeypatch.setattr(service, "_require_root_systemd_service", lambda: None)
    monkeypatch.setattr(oci_deployment, "_verify_pinned_directory", lambda *a, **k: None)
    monkeypatch.setattr(service, "_find_target_mount", lambda _target: None)
    with pytest.raises(oci_deployment.OCISharedWorkspaceError, match="must be empty"):
        service.ensure_shared_workspace()
