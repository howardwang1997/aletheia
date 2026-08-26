from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import aletheia.qualification_installer as installer
from aletheia.execution import qualification_deployment as deployment
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.qualification_service_runtime import (
    QualificationServiceDeploymentManifestV1,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
)
from .test_qualification_deployment import _sha, _spec

NOW = datetime(2026, 8, 27, 2, 3, 4, tzinfo=timezone.utc)
ROLES = (
    QualificationServiceRole.WORKSPACE,
    QualificationServiceRole.QUOTA,
    QualificationServiceRole.WATCHDOG,
    QualificationServiceRole.NODE,
    QualificationServiceRole.OUTBOX,
)
OPERATIONS = {
    QualificationServiceRole.WORKSPACE: "ensure-shared-workspace",
    QualificationServiceRole.QUOTA: "serve",
    QualificationServiceRole.WATCHDOG: "serve",
    QualificationServiceRole.NODE: "run",
    QualificationServiceRole.OUTBOX: "run",
}


def _replace_process(
    process: QualificationServiceProcessDeploymentV1,
    **updates: object,
) -> QualificationServiceProcessDeploymentV1:
    return QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            **updates,
        }
    )


def _replace_manifest_process(
    manifest: QualificationServiceDeploymentManifestV1,
    role: QualificationServiceRole,
    **updates: object,
) -> QualificationServiceDeploymentManifestV1:
    processes = tuple(
        _replace_process(process, **updates) if process.role is role else process
        for process in manifest.processes
    )
    return QualificationServiceDeploymentManifestV1.model_validate(
        {
            **manifest.model_dump(mode="python", exclude={"manifest_id"}),
            "processes": processes,
        }
    )


def _spec_with_manifest(
    spec: deployment.QualificationDeploymentSpecV1,
    manifest: QualificationServiceDeploymentManifestV1,
) -> deployment.QualificationDeploymentSpecV1:
    return _spec(
        reviewed_code_tree=spec.reviewed_code_tree,
        agent_implementation_sha256=spec.agent_implementation_sha256,
        deployment_manifest_sha256=manifest.file_sha256,
    )


def _request() -> installer.QualificationInstallationRequestV1:
    base = _spec()
    additional_entries = tuple(
        deployment.QualificationReviewedCodeFile(
            relative_path=(f"aletheia/execution/qualification_{role.value}_composition.py"),
            reviewed_sha256=_sha(f"factory:{role.value}"),
            byte_length=1000 + index,
            expected_mode=0o444,
        )
        for index, role in enumerate(ROLES)
    )
    entries = tuple(
        sorted(
            (*base.reviewed_code_tree.entries, *additional_entries),
            key=lambda item: item.relative_path,
        )
    )
    tree = deployment.QualificationReviewedCodeTree(
        root_path=base.reviewed_code_tree.root_path,
        expected_root_mode=base.reviewed_code_tree.expected_root_mode,
        directories=base.reviewed_code_tree.directories,
        entries=entries,
        manifest_sha256=deployment.reviewed_code_tree_manifest_sha256(
            root_path=base.reviewed_code_tree.root_path,
            expected_root_mode=base.reviewed_code_tree.expected_root_mode,
            directories=base.reviewed_code_tree.directories,
            entries=entries,
        ),
    )
    runners = (
        base.expected_workspace_runner,
        base.expected_quota_runner,
        base.expected_watchdog_runner,
        base.expected_node_runner,
        base.expected_outbox_runner,
    )
    agent_sha256 = deployment.qualification_agent_implementation_sha256(
        reviewed_code_tree=tree,
        reviewed_python_environment=base.reviewed_python_environment,
        expected_python_executable=base.expected_python_executable,
        expected_runners=runners,
        expected_service_modules=(
            base.expected_quota_service_module,
            base.expected_watchdog_service_module,
        ),
        expected_python_import_paths=base.expected_python_import_paths,
    )
    provisional_spec = _spec(
        reviewed_code_tree=tree,
        agent_implementation_sha256=agent_sha256,
    )
    entries_by_path = {entry.relative_path: entry for entry in entries}
    processes: list[QualificationServiceProcessDeploymentV1] = []
    for role in ROLES:
        relative = f"aletheia/execution/qualification_{role.value}_composition.py"
        entry = entries_by_path[relative]
        root_role = role in {
            QualificationServiceRole.WORKSPACE,
            QualificationServiceRole.QUOTA,
            QualificationServiceRole.WATCHDOG,
        }
        process_uid = (
            0
            if root_role
            else provisional_spec.node_uid
            if role is QualificationServiceRole.NODE
            else provisional_spec.outbox_uid
        )
        process_gid = (
            0
            if root_role
            else provisional_spec.node_gid
            if role is QualificationServiceRole.NODE
            else provisional_spec.outbox_gid
        )
        processes.append(
            QualificationServiceProcessDeploymentV1(
                deployment_id=provisional_spec.deployment_id,
                role=role,
                operation=OPERATIONS[role],
                process_uid=process_uid,
                process_gid=process_gid,
                worker_poll_milliseconds=(
                    provisional_spec.worker_poll_milliseconds
                    if role is QualificationServiceRole.NODE
                    else None
                ),
                reviewed_code_root=provisional_spec.code_root,
                composition_factory_module=(
                    f"aletheia.execution.qualification_{role.value}_composition"
                ),
                composition_factory_attribute=f"build_{role.value}_service",
                composition_factory_source_path=(f"{provisional_spec.code_root}/{relative}"),
                composition_factory_source_sha256=entry.reviewed_sha256,
                composition_factory_owner_uid=entry.expected_owner_uid,
                composition_factory_owner_gid=entry.expected_owner_gid,
                composition_factory_mode=entry.expected_mode,
                composition_config_path=f"/etc/aletheia/services/{role.value}.json",
                composition_config_file_sha256=_sha(f"config:{role.value}"),
                composition_config_owner_uid=process_uid,
                composition_config_owner_gid=process_gid,
                composition_config_mode=0o400,
            )
        )
    manifest = QualificationServiceDeploymentManifestV1(
        deployment_id=provisional_spec.deployment_id,
        processes=tuple(processes),
        prepared_at=NOW,
    )
    spec = _spec_with_manifest(provisional_spec, manifest)
    return installer.QualificationInstallationRequestV1(
        deployment_spec=spec,
        service_manifest=manifest,
        journal_root="/var/lib/aletheia/qualification-installer",
        systemctl_executable=deployment.QualificationExpectedRootExecutable(
            path="/usr/bin/systemctl",
            reviewed_sha256=_sha("systemctl"),
            expected_mode=0o555,
        ),
        requested_at=NOW,
    )


class _FakeHost:
    def __init__(self) -> None:
        self.journals: dict[str, bytes] = {}
        self.targets: dict[str, installer.QualificationInstalledFileObservation] = {}
        self.target_payloads: dict[str, bytes] = {}
        self.loaded = False
        self.fail_inputs = False
        self.fail_quiescence = False
        self.daemon_reload_calls = 0
        self.lock_calls = 0
        self.verify_calls = 0

    def assert_linux_root(self) -> None:
        return None

    @contextmanager
    def lock(self):
        self.lock_calls += 1
        yield

    def verify_pinned_inputs(self) -> None:
        self.verify_calls += 1
        if self.fail_inputs:
            raise installer.QualificationInstallationError("pinned input failed")

    def observe_systemd(self, unit_names):
        if self.fail_quiescence:
            raise installer.QualificationInstallationError("unit is enabled")
        return installer.QualificationSystemdQuiescenceObservation(
            units=tuple(
                installer.QualificationSystemdUnitState(
                    unit_name=name,
                    load_state="loaded" if self.loaded else "not-found",
                    unit_file_state="disabled" if self.loaded else "not-found",
                )
                for name in unit_names
            ),
            observed_at=NOW,
        )

    def read_journal(self, path: Path) -> bytes | None:
        return self.journals.get(str(path))

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        key = str(path)
        existing = self.journals.get(key)
        if existing is not None and existing != payload:
            raise installer.QualificationInstallationError("journal exact retry differs")
        self.journals[key] = payload

    def publish_artifact(self, artifact, payload):
        existing = self.targets.get(artifact.target_path)
        if existing is not None:
            if (
                existing.content_sha256 != artifact.content_sha256
                or existing.byte_length != artifact.byte_length
                or existing.owner_uid != artifact.owner_uid
                or existing.owner_gid != artifact.owner_gid
                or existing.mode != artifact.mode
                or self.target_payloads[artifact.target_path] != payload
            ):
                raise installer.QualificationInstallationError("variant target")
            return existing
        observed = installer.QualificationInstalledFileObservation(
            path=artifact.target_path,
            content_sha256=artifact.content_sha256,
            byte_length=artifact.byte_length,
            owner_uid=artifact.owner_uid,
            owner_gid=artifact.owner_gid,
            mode=artifact.mode,
            device=7,
            inode=1000 + artifact.ordinal,
            link_count=1,
        )
        self.targets[artifact.target_path] = observed
        self.target_payloads[artifact.target_path] = payload
        return observed

    def observe_artifact(self, artifact):
        try:
            return self.targets[artifact.target_path]
        except KeyError as exc:
            raise installer.QualificationInstallationError("missing target") from exc

    def daemon_reload(self) -> str:
        self.daemon_reload_calls += 1
        self.loaded = True
        return _sha("daemon-reload")


def _clock():
    counter = 0

    def now() -> datetime:
        nonlocal counter
        counter += 1
        return NOW + timedelta(seconds=counter)

    return now


def test_request_and_plan_bind_exact_disabled_installation() -> None:
    request = _request()
    plan = installer.build_qualification_installation_plan(request)
    assert request.request_id == f"qir_{request.identity_sha256[:32]}"
    assert request.file_sha256 == hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    assert plan.plan_id == f"qip_{plan.identity_sha256[:32]}"
    assert tuple(item.ordinal for item in plan.artifacts) == tuple(range(6))
    assert plan.artifacts[0].target_path == request.deployment_spec.deployment_manifest_path
    assert tuple(item.unit_name for item in plan.artifacts[1:]) == tuple(
        unit.unit_name for unit in deployment.render_systemd_units(request.deployment_spec)
    )
    assert plan.postgresql_acl_applied is False
    assert plan.services_enabled is False
    assert plan.services_started is False
    assert plan.deployment_qualified is False


def test_request_rejects_process_identity_source_config_and_journal_rebinds() -> None:
    request = _request()
    manifest = _replace_manifest_process(
        request.service_manifest,
        QualificationServiceRole.NODE,
        process_uid=request.deployment_spec.node_uid + 10,
    )
    with pytest.raises(ValidationError, match="process identity differs"):
        installer.QualificationInstallationRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "service_manifest": manifest,
                "deployment_spec": _spec_with_manifest(request.deployment_spec, manifest),
            }
        )

    node = request.service_manifest.process_for(QualificationServiceRole.NODE)
    manifest = _replace_manifest_process(
        request.service_manifest,
        QualificationServiceRole.NODE,
        composition_factory_source_path=(
            f"{request.deployment_spec.code_root}/aletheia/execution/unreviewed.py"
        ),
        composition_factory_module="aletheia.execution.unreviewed",
        composition_factory_source_sha256=_sha("unreviewed"),
    )
    with pytest.raises(ValidationError, match="not an exact reviewed"):
        installer.QualificationInstallationRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "service_manifest": manifest,
                "deployment_spec": _spec_with_manifest(request.deployment_spec, manifest),
            }
        )

    manifest = _replace_manifest_process(
        request.service_manifest,
        QualificationServiceRole.NODE,
        composition_config_owner_uid=0,
        composition_config_owner_gid=0,
        composition_config_mode=0o400,
    )
    with pytest.raises(ValidationError, match="cannot read"):
        installer.QualificationInstallationRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "service_manifest": manifest,
                "deployment_spec": _spec_with_manifest(request.deployment_spec, manifest),
            }
        )
    assert node.process_uid != 0

    with pytest.raises(ValidationError, match="journal overlaps"):
        installer.QualificationInstallationRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "journal_root": request.deployment_spec.node_state_root,
            }
        )


def test_install_happy_path_and_exact_retry_are_idempotent() -> None:
    request = _request()
    host = _FakeHost()
    receipt = installer.install_qualification_service_files(
        request,
        host,
        clock=_clock(),
    )
    assert receipt.receipt_id == f"qix_{receipt.identity_sha256[:32]}"
    assert len(receipt.artifact_completions) == 6
    assert len(host.targets) == 6
    assert host.daemon_reload_calls == 1
    assert host.loaded is True
    assert receipt.postgresql_acl_applied is False
    assert receipt.services_enabled is False
    assert receipt.services_started is False
    assert receipt.deployment_qualified is False
    assert len(host.journals) == 17

    retried = installer.install_qualification_service_files(
        request,
        host,
        clock=_clock(),
    )
    assert retried == receipt
    assert host.daemon_reload_calls == 1
    assert len(host.targets) == 6


@pytest.mark.parametrize("ordinal", tuple(range(6)))
def test_crash_after_each_artifact_publish_resumes_exactly_once(ordinal: int) -> None:
    request = _request()
    host = _FakeHost()
    clock = _clock()
    crashed = False

    def fault(phase: str) -> None:
        nonlocal crashed
        if phase == f"after_artifact_publish:{ordinal}" and not crashed:
            crashed = True
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        installer.install_qualification_service_files(
            request,
            host,
            clock=clock,
            fault=fault,
        )
    receipt = installer.install_qualification_service_files(
        request,
        host,
        clock=clock,
    )
    assert tuple(item.artifact_ordinal for item in receipt.artifact_completions) == tuple(range(6))
    assert len(host.targets) == 6
    assert host.daemon_reload_calls == 1


def test_crash_after_durable_daemon_reload_marker_does_not_reinvoke() -> None:
    request = _request()
    host = _FakeHost()
    clock = _clock()
    crashed = False

    def fault(phase: str) -> None:
        nonlocal crashed
        if phase == "after_daemon_reload" and not crashed:
            crashed = True
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError):
        installer.install_qualification_service_files(
            request,
            host,
            clock=clock,
            fault=fault,
        )
    assert host.daemon_reload_calls == 1
    installer.install_qualification_service_files(request, host, clock=clock)
    assert host.daemon_reload_calls == 1


@pytest.mark.parametrize("journal_kind", ("artifact", "daemon-reload"))
def test_resume_rejects_noncanonical_journal_chronology(journal_kind: str) -> None:
    request = _request()
    host = _FakeHost()
    clock = _clock()
    crashed = False

    def fault(phase: str) -> None:
        nonlocal crashed
        target_phase = (
            "after_artifact_completion:1" if journal_kind == "artifact" else "after_daemon_reload"
        )
        if phase == target_phase and not crashed:
            crashed = True
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        installer.install_qualification_service_files(
            request,
            host,
            clock=clock,
            fault=fault,
        )

    request_root = next(Path(key).parent for key in host.journals if key.endswith("/request.json"))
    journal_name = (
        "artifact-1.completed.json" if journal_kind == "artifact" else "daemon-reload.json"
    )
    journal_path = str(request_root / journal_name)
    timestamp_field = "installed_at" if journal_kind == "artifact" else "reloaded_at"
    model_type = (
        installer.QualificationInstallationArtifactCompletion
        if journal_kind == "artifact"
        else installer.QualificationDaemonReloadReceipt
    )
    document = model_type.model_validate_json(host.journals[journal_path]).model_copy(
        update={timestamp_field: NOW - timedelta(seconds=1)}
    )
    host.journals[journal_path] = canonical_json_bytes(document)

    expected = (
        "artifact completion timestamps are not canonical"
        if journal_kind == "artifact"
        else "daemon reload timestamp precedes artifact completion"
    )
    with pytest.raises(installer.QualificationInstallationError, match=expected):
        installer.install_qualification_service_files(request, host, clock=clock)


def test_active_request_rejects_variant_installation() -> None:
    request = _request()
    host = _FakeHost()
    installer.install_qualification_service_files(request, host, clock=_clock())
    variant = installer.QualificationInstallationRequestV1.model_validate(
        {
            **request.model_dump(mode="python", exclude={"request_id"}),
            "requested_at": NOW + timedelta(seconds=1),
        }
    )
    assert variant.request_id != request.request_id
    with pytest.raises(installer.QualificationInstallationError, match="journal exact retry"):
        installer.install_qualification_service_files(variant, host, clock=_clock())
    assert host.daemon_reload_calls == 1


def test_variant_target_and_completed_inode_drift_fail_closed() -> None:
    request = _request()
    plan = installer.build_qualification_installation_plan(request)
    host = _FakeHost()
    first = plan.artifacts[0]
    host.targets[first.target_path] = installer.QualificationInstalledFileObservation(
        path=first.target_path,
        content_sha256=_sha("variant"),
        byte_length=first.byte_length,
        owner_uid=0,
        owner_gid=0,
        mode=first.mode,
        device=7,
        inode=999,
        link_count=1,
    )
    host.target_payloads[first.target_path] = b"variant"
    with pytest.raises(installer.QualificationInstallationError, match="variant target"):
        installer.install_qualification_service_files(request, host, clock=_clock())

    host = _FakeHost()
    installer.install_qualification_service_files(request, host, clock=_clock())
    observed = host.targets[first.target_path]
    host.targets[first.target_path] = observed.model_copy(update={"inode": observed.inode + 1})
    with pytest.raises(installer.QualificationInstallationError, match="changed after"):
        installer.install_qualification_service_files(request, host, clock=_clock())


@pytest.mark.parametrize("failure", ("inputs", "quiescence"))
def test_preflight_failure_writes_no_journal_or_target(failure: str) -> None:
    host = _FakeHost()
    if failure == "inputs":
        host.fail_inputs = True
    else:
        host.fail_quiescence = True
    with pytest.raises(installer.QualificationInstallationError):
        installer.install_qualification_service_files(_request(), host, clock=_clock())
    assert host.journals == {}
    assert host.targets == {}
    assert host.daemon_reload_calls == 0


def test_request_loader_and_cli_plan_are_canonical_and_non_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _request()
    path = (tmp_path / "request.json").resolve()
    path.write_bytes(canonical_json_bytes(request))
    loaded = installer.load_qualification_installation_request(
        path,
        expected_file_sha256=request.file_sha256,
    )
    assert loaded == request
    assert (
        installer.run_qualification_installer_cli(
            (
                "--request",
                str(path),
                "--request-sha256",
                request.file_sha256,
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["schema_name"] == "aletheia.qualification_installation_plan"

    with pytest.raises(installer.QualificationInstallationError, match="byte digest"):
        installer.load_qualification_installation_request(
            path,
            expected_file_sha256="0" * 64,
        )
    noncanonical = (tmp_path / "pretty.json").resolve()
    noncanonical.write_text(
        json.dumps(request.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    with pytest.raises(installer.QualificationInstallationError, match="not canonical"):
        installer.load_qualification_installation_request(
            noncanonical,
            expected_file_sha256=hashlib.sha256(noncanonical.read_bytes()).hexdigest(),
        )


def test_cli_apply_requires_exact_opt_in_before_constructing_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    path = (tmp_path / "request.json").resolve()
    path.write_bytes(canonical_json_bytes(request))
    calls = 0

    def forbidden_host(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("host must not be constructed")

    monkeypatch.setattr(installer, "LinuxQualificationInstallationHost", forbidden_host)
    with pytest.raises(SystemExit):
        installer.run_qualification_installer_cli(
            (
                "--request",
                str(path),
                "--request-sha256",
                request.file_sha256,
                "--apply",
            )
        )
    assert calls == 0


def test_concrete_host_refuses_non_linux_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = installer.LinuxQualificationInstallationHost(_request())
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    with pytest.raises(installer.QualificationInstallationError, match="requires Linux"):
        host.assert_linux_root()


def test_concrete_atomic_publish_is_no_overwrite_and_exact_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    host = installer.LinuxQualificationInstallationHost(request)
    monkeypatch.setattr(host, "_assert_root_parent_chain", lambda _path: None)
    target = (tmp_path / "installed.conf").resolve()
    payload = b"exact bytes\n"
    digest = hashlib.sha256(payload).hexdigest()
    first = host._publish_exact(
        target=target,
        payload=payload,
        digest=digest,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        mode=0o444,
        ordinal=1,
    )
    second = host._publish_exact(
        target=target,
        payload=payload,
        digest=digest,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        mode=0o444,
        ordinal=1,
    )
    assert second == first
    target.chmod(0o644)
    target.write_bytes(b"variant\n")
    target.chmod(0o444)
    with pytest.raises(installer.QualificationInstallationError, match="variant custody"):
        host._publish_exact(
            target=target,
            payload=payload,
            digest=digest,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            mode=0o444,
            ordinal=1,
        )


def test_systemd_observer_normalizes_absent_unit_and_rejects_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = installer.LinuxQualificationInstallationHost(_request())
    missing = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout="LoadState=not-found\nActiveState=inactive\nUnitFileState=\n",
        stderr="",
    )
    monkeypatch.setattr(host, "_systemctl", lambda *args: missing)
    names = tuple(
        item.unit_name for item in deployment.render_systemd_units(host.request.deployment_spec)
    )
    observed = host.observe_systemd(names)
    assert all(item.unit_file_state == "not-found" for item in observed.units)

    active = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout="LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
        stderr="",
    )
    monkeypatch.setattr(host, "_systemctl", lambda *args: active)
    with pytest.raises(installer.QualificationInstallationError, match="active, enabled"):
        host.observe_systemd(names)


def test_host_observation_and_systemd_scope_cannot_be_rebound() -> None:
    request = _request()

    class ReboundArtifactHost(_FakeHost):
        def publish_artifact(self, artifact, payload):
            observed = super().publish_artifact(artifact, payload)
            return observed.model_copy(update={"path": f"{artifact.target_path}.other"})

    with pytest.raises(installer.QualificationInstallationError, match="differs from"):
        installer.install_qualification_service_files(
            request,
            ReboundArtifactHost(),
            clock=_clock(),
        )

    class ReboundSystemdHost(_FakeHost):
        def observe_systemd(self, unit_names):
            observed = super().observe_systemd(unit_names)
            units = list(observed.units)
            units[0] = units[0].model_copy(update={"unit_name": "another.service"})
            return observed.model_copy(
                update={"units": tuple(sorted(units, key=lambda x: x.unit_name))}
            )

    with pytest.raises(installer.QualificationInstallationError, match="exact units"):
        installer.install_qualification_service_files(
            request,
            ReboundSystemdHost(),
            clock=_clock(),
        )


def test_installer_clock_rollback_fails_before_later_completion() -> None:
    values = iter((NOW + timedelta(seconds=2), NOW + timedelta(seconds=1)))
    host = _FakeHost()
    with pytest.raises(installer.QualificationInstallationError, match="clock moved backwards"):
        installer.install_qualification_service_files(
            _request(),
            host,
            clock=lambda: next(values),
        )
    assert len(host.targets) == 2
    assert host.daemon_reload_calls == 0


def test_concrete_missing_journal_read_is_non_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = installer.LinuxQualificationInstallationHost(_request())
    monkeypatch.setattr(host, "_prepare_journal_parent", lambda _path: None)
    assert host.read_journal((tmp_path / "missing.json").resolve()) is None
