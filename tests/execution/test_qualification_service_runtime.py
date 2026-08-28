from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import aletheia.qualification_python_bootstrap as python_bootstrap
import aletheia.qualification_service_runtime as runtime
from aletheia.execution.schemas import canonical_json_bytes

NOW = datetime(2026, 8, 27, 1, 2, 3, tzinfo=timezone.utc)
ROLE_ORDER = (
    runtime.QualificationServiceRole.WORKSPACE,
    runtime.QualificationServiceRole.QUOTA,
    runtime.QualificationServiceRole.WATCHDOG,
    runtime.QualificationServiceRole.NODE,
    runtime.QualificationServiceRole.OUTBOX,
)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_process(
    process: runtime.QualificationServiceProcessDeploymentV1,
    **updates: object,
) -> runtime.QualificationServiceProcessDeploymentV1:
    return runtime.QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            **updates,
        }
    )


def _factory_source(attribute: str) -> str:
    return f"""from __future__ import annotations

import json
from pathlib import Path

from aletheia.qualification_service_runtime import QualificationServiceHandlerSet


def {attribute}(*, deployment, configuration_bytes):
    configuration = json.loads(configuration_bytes)

    def handler(*, poll_milliseconds):
        Path(configuration["marker_path"]).write_text(
            json.dumps(
                {{
                    "operation": deployment.operation,
                    "poll_milliseconds": poll_milliseconds,
                    "role": deployment.role.value,
                }},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    return QualificationServiceHandlerSet(
        role=deployment.role,
        operation=deployment.operation,
        handler=handler,
    )
"""


def _manifest(tmp_path: Path) -> tuple[runtime.QualificationServiceDeploymentManifestV1, Path]:
    root = (tmp_path / "release").resolve()
    config_root = (tmp_path / "configuration").resolve()
    processes: list[runtime.QualificationServiceProcessDeploymentV1] = []
    for index, role in enumerate(ROLE_ORDER):
        module = f"aletheia.execution.qualification_{role.value}_composition"
        attribute = f"build_{role.value.replace('-', '_')}_service"
        source = root / Path(*module.split(".")).with_suffix(".py")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_factory_source(attribute), encoding="utf-8")
        source.chmod(0o444)
        config = config_root / f"{role.value}.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {"marker_path": str((tmp_path / f"{role.value}.marker").resolve())},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        config.chmod(0o444)
        source_stat = source.stat()
        config_stat = config.stat()
        root_service = role in {
            runtime.QualificationServiceRole.WORKSPACE,
            runtime.QualificationServiceRole.QUOTA,
            runtime.QualificationServiceRole.WATCHDOG,
        }
        processes.append(
            runtime.QualificationServiceProcessDeploymentV1(
                deployment_id="deployment:qualification-local",
                role=role,
                operation={
                    runtime.QualificationServiceRole.WORKSPACE: "ensure-shared-workspace",
                    runtime.QualificationServiceRole.QUOTA: "serve",
                    runtime.QualificationServiceRole.WATCHDOG: "serve",
                    runtime.QualificationServiceRole.NODE: "run",
                    runtime.QualificationServiceRole.OUTBOX: "run",
                }[role],
                process_uid=0 if root_service else 1000 + index,
                process_gid=0 if root_service else 2000 + index,
                worker_poll_milliseconds=(
                    250 if role is runtime.QualificationServiceRole.NODE else None
                ),
                reviewed_code_root=str(root),
                composition_factory_module=module,
                composition_factory_attribute=attribute,
                composition_factory_source_path=str(source),
                composition_factory_source_sha256=_file_sha(source),
                composition_factory_owner_uid=source_stat.st_uid,
                composition_factory_owner_gid=source_stat.st_gid,
                composition_factory_mode=0o444,
                composition_config_path=str(config),
                composition_config_file_sha256=_file_sha(config),
                composition_config_owner_uid=config_stat.st_uid,
                composition_config_owner_gid=config_stat.st_gid,
                composition_config_mode=0o444,
            )
        )
    manifest = runtime.QualificationServiceDeploymentManifestV1(
        deployment_id="deployment:qualification-local",
        processes=tuple(processes),
        prepared_at=NOW,
    )
    path = (tmp_path / "qualification-services.json").resolve()
    path.write_bytes(canonical_json_bytes(manifest))
    path.chmod(0o444)
    return manifest, path


def test_manifest_is_derived_exhaustive_and_canonical(tmp_path: Path) -> None:
    manifest, path = _manifest(tmp_path)
    assert manifest.manifest_id == f"qsm_{manifest.identity_sha256[:32]}"
    assert manifest.file_sha256 == _file_sha(path)
    assert tuple(process.role for process in manifest.processes) == ROLE_ORDER
    assert all(
        process.process_id == f"qsp_{process.identity_sha256[:32]}"
        for process in manifest.processes
    )
    assert manifest.qualification_only is True
    assert manifest.scientific_admission_allowed is False
    assert manifest.automatic_installation is False
    assert manifest.automatic_start is False


def test_manifest_rejects_missing_reordered_or_rebound_roles(tmp_path: Path) -> None:
    manifest, _path = _manifest(tmp_path)
    values = manifest.model_dump(mode="python", exclude={"manifest_id"})
    with pytest.raises(ValidationError, match="all roles canonically"):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**values, "processes": tuple(reversed(manifest.processes))}
        )
    with pytest.raises(ValidationError):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**values, "processes": manifest.processes[:-1]}
        )
    rebound = _replace_process(manifest.processes[-1], deployment_id="deployment:another")
    with pytest.raises(ValidationError, match="another deployment"):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**values, "processes": (*manifest.processes[:-1], rebound)}
        )


def test_process_rejects_wrong_operation_path_mode_and_identity(tmp_path: Path) -> None:
    manifest, _path = _manifest(tmp_path)
    node = manifest.process_for(runtime.QualificationServiceRole.NODE)
    values = node.model_dump(mode="python", exclude={"process_id"})
    with pytest.raises(ValidationError, match="operation differs"):
        runtime.QualificationServiceProcessDeploymentV1.model_validate(
            {**values, "operation": "serve"}
        )
    with pytest.raises(ValidationError, match="module does not match"):
        runtime.QualificationServiceProcessDeploymentV1.model_validate(
            {**values, "composition_factory_source_path": str(tmp_path / "release/wrong.py")}
        )
    with pytest.raises(ValidationError, match="read-only"):
        runtime.QualificationServiceProcessDeploymentV1.model_validate(
            {**values, "composition_config_mode": 0o644}
        )
    with pytest.raises(ValidationError, match="non-root"):
        runtime.QualificationServiceProcessDeploymentV1.model_validate({**values, "process_uid": 0})


def test_manifest_rejects_shared_config_and_node_outbox_identity(tmp_path: Path) -> None:
    manifest, _path = _manifest(tmp_path)
    values = manifest.model_dump(mode="python", exclude={"manifest_id"})
    outbox = manifest.process_for(runtime.QualificationServiceRole.OUTBOX)
    node = manifest.process_for(runtime.QualificationServiceRole.NODE)
    shared_config = _replace_process(
        outbox,
        composition_config_path=node.composition_config_path,
    )
    with pytest.raises(ValidationError, match="configurations must be role-specific"):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**values, "processes": (*manifest.processes[:-1], shared_config)}
        )
    shared_identity = _replace_process(
        outbox,
        process_uid=node.process_uid,
        process_gid=node.process_gid,
    )
    with pytest.raises(ValidationError, match="identities must be distinct"):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**values, "processes": (*manifest.processes[:-1], shared_identity)}
        )


def test_loader_requires_exact_digest_unique_keys_and_canonical_json(tmp_path: Path) -> None:
    manifest, path = _manifest(tmp_path)
    loaded = runtime.load_qualification_service_deployment_manifest(
        path,
        expected_file_sha256=manifest.file_sha256,
    )
    assert loaded == manifest
    with pytest.raises(runtime.QualificationServiceProcessError, match="byte pin"):
        runtime.load_qualification_service_deployment_manifest(
            path,
            expected_file_sha256="0" * 64,
        )

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    with pytest.raises(runtime.QualificationServiceProcessError, match="not canonical"):
        runtime.load_qualification_service_deployment_manifest(
            noncanonical.resolve(),
            expected_file_sha256=_file_sha(noncanonical),
        )

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_name":"one","schema_name":"two"}', encoding="utf-8")
    with pytest.raises(runtime.QualificationServiceProcessError, match="manifest is invalid"):
        runtime.load_qualification_service_deployment_manifest(
            duplicate.resolve(),
            expected_file_sha256=_file_sha(duplicate),
        )


@pytest.mark.parametrize("role", ROLE_ORDER)
def test_runtime_dispatches_only_the_exact_role_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: runtime.QualificationServiceRole,
) -> None:
    manifest, _path = _manifest(tmp_path)
    process = manifest.process_for(role)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: process.process_uid)
    monkeypatch.setattr(runtime.os, "getegid", lambda: process.process_gid)
    clock_values = iter((NOW, NOW + timedelta(seconds=1)))
    service = runtime.build_qualification_service_runtime(
        manifest,
        role=role,
        clock=lambda: next(clock_values),
    )
    startup = service.start()
    assert service.start() is startup
    receipt = service.run()
    assert receipt.startup_receipt_sha256 == startup.receipt_sha256
    assert receipt.deployment_qualified is False
    marker = tmp_path / f"{role.value}.marker"
    observed = json.loads(marker.read_text(encoding="utf-8"))
    assert observed == {
        "operation": process.operation,
        "poll_milliseconds": process.worker_poll_milliseconds,
        "role": role.value,
    }


def test_runtime_checks_linux_identity_before_loading_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, _path = _manifest(tmp_path)
    process = manifest.process_for(runtime.QualificationServiceRole.NODE)
    calls = 0

    def forbidden_loader(_deployment):
        nonlocal calls
        calls += 1
        raise AssertionError("factory must not load")

    monkeypatch.setattr(runtime, "_load_handler_set", forbidden_loader)
    service = runtime.build_qualification_service_runtime(
        manifest,
        role=runtime.QualificationServiceRole.NODE,
    )
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    with pytest.raises(runtime.QualificationServiceProcessError, match="requires Linux"):
        service.start()
    assert calls == 0

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: process.process_uid + 1)
    monkeypatch.setattr(runtime.os, "getegid", lambda: process.process_gid)
    with pytest.raises(runtime.QualificationServiceProcessError, match="UID/GID"):
        service.start()
    assert calls == 0


@pytest.mark.parametrize("target", ("source", "config"))
def test_runtime_rejects_factory_or_config_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
) -> None:
    manifest, _path = _manifest(tmp_path)
    process = manifest.process_for(runtime.QualificationServiceRole.NODE)
    changed = Path(
        process.composition_factory_source_path
        if target == "source"
        else process.composition_config_path
    )
    changed.chmod(0o644)
    changed.write_bytes(changed.read_bytes() + b"\n")
    changed.chmod(0o444)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: process.process_uid)
    monkeypatch.setattr(runtime.os, "getegid", lambda: process.process_gid)
    service = runtime.build_qualification_service_runtime(
        manifest,
        role=runtime.QualificationServiceRole.NODE,
    )
    with pytest.raises(runtime.QualificationServiceProcessError, match="byte pin"):
        service.start()


def test_cli_requires_exact_role_operation_poll_and_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, path = _manifest(tmp_path)
    process = manifest.process_for(runtime.QualificationServiceRole.NODE)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: process.process_uid)
    monkeypatch.setattr(runtime.os, "getegid", lambda: process.process_gid)
    assert (
        runtime.run_qualification_service_cli(
            role=runtime.QualificationServiceRole.NODE,
            argv=(
                "--manifest",
                str(path),
                "--manifest-sha256",
                manifest.file_sha256,
                "run",
                "--poll-milliseconds",
                "250",
            ),
        )
        == 0
    )
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["schema_name"] for item in output] == [
        "aletheia.qualification_service_startup_receipt",
        "aletheia.qualification_service_exit_receipt",
    ]
    with pytest.raises(SystemExit):
        runtime.run_qualification_service_cli(
            role=runtime.QualificationServiceRole.NODE,
            argv=(
                "--manifest",
                str(path),
                "--manifest-sha256",
                manifest.file_sha256,
                "serve",
                "--poll-milliseconds",
                "250",
            ),
        )
    with pytest.raises(SystemExit):
        runtime.run_qualification_service_cli(
            role=runtime.QualificationServiceRole.NODE,
            argv=(
                "--manifest",
                str(path),
                "--manifest-sha256",
                manifest.file_sha256,
                "run",
                "--poll-milliseconds",
                "251",
            ),
        )


@pytest.mark.parametrize(
    ("script_name", "expected_role"),
    (
        ("run-workspace.py", runtime.QualificationServiceRole.WORKSPACE),
        ("run-quota.py", runtime.QualificationServiceRole.QUOTA),
        ("run-watchdog.py", runtime.QualificationServiceRole.WATCHDOG),
        ("run-node.py", runtime.QualificationServiceRole.NODE),
        ("run-outbox.py", runtime.QualificationServiceRole.OUTBOX),
    ),
)
def test_thin_runner_compiles_in_exactly_one_role(
    monkeypatch: pytest.MonkeyPatch,
    script_name: str,
    expected_role: runtime.QualificationServiceRole,
) -> None:
    observed: list[runtime.QualificationServiceRole] = []

    def fake_cli(*, role, argv=None):
        del argv
        observed.append(role)
        return 17

    monkeypatch.setattr(runtime, "run_qualification_service_cli", fake_cli)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / script_name))
    assert namespace["main"]() == 17
    assert observed == [expected_role]


def test_no_site_runner_appends_reviewed_packages_after_stdlib() -> None:
    import pydantic

    repository_root = Path(__file__).resolve().parents[2]
    site_packages = Path(pydantic.__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHOME": sys.prefix,
            "PYTHONPATH": str(repository_root),
            python_bootstrap.QUALIFICATION_SITE_PACKAGES_ENV: str(site_packages),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    completed = subprocess.run(
        (
            sys.executable,
            "-S",
            "-s",
            "-P",
            "-c",
            (
                "import sys; "
                "from aletheia.qualification_python_bootstrap import "
                "activate_reviewed_site_packages; "
                "activated = activate_reviewed_site_packages(); "
                "import dataclasses, pydantic, sqlalchemy; "
                "assert sys.path[-1] == activated; "
                "assert dataclasses.__file__.startswith(sys.prefix)"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    environment["PYTHONPATH"] = f"{repository_root}:{site_packages}"
    injected = subprocess.run(
        (
            sys.executable,
            "-S",
            "-s",
            "-P",
            "-c",
            (
                "from aletheia.qualification_python_bootstrap import "
                "activate_reviewed_site_packages; activate_reviewed_site_packages()"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert injected.returncode != 0
    assert "injected before reviewed bootstrap" in injected.stderr


def test_handler_set_rejects_wrong_operation_or_noncallable() -> None:
    with pytest.raises(ValueError, match="differs from its role"):
        runtime.QualificationServiceHandlerSet(
            role=runtime.QualificationServiceRole.OUTBOX,
            operation="serve",
            handler=lambda *, poll_milliseconds: None,
        )
    with pytest.raises(TypeError, match="not callable"):
        runtime.QualificationServiceHandlerSet(
            role=runtime.QualificationServiceRole.OUTBOX,
            operation="run",
            handler=None,  # type: ignore[arg-type]
        )


def test_handler_cannot_return_an_authority_bearing_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, _path = _manifest(tmp_path)
    process = manifest.process_for(runtime.QualificationServiceRole.NODE)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: process.process_uid)
    monkeypatch.setattr(runtime.os, "getegid", lambda: process.process_gid)
    monkeypatch.setattr(
        runtime,
        "_load_handler_set",
        lambda _deployment: runtime.QualificationServiceHandlerSet(
            role=runtime.QualificationServiceRole.NODE,
            operation="run",
            handler=lambda *, poll_milliseconds: {"scientific_result": True},
        ),
    )
    service = runtime.build_qualification_service_runtime(
        manifest,
        role=runtime.QualificationServiceRole.NODE,
    )
    with pytest.raises(runtime.QualificationServiceProcessError, match="unauthorized value"):
        service.run()


def test_manifest_rejects_stale_caller_authored_ids(tmp_path: Path) -> None:
    manifest, _path = _manifest(tmp_path)
    with pytest.raises(ValidationError, match="manifest id is not derived"):
        runtime.QualificationServiceDeploymentManifestV1.model_validate(
            {**manifest.model_dump(mode="python"), "manifest_id": f"qsm_{'0' * 32}"}
        )
    node = manifest.process_for(runtime.QualificationServiceRole.NODE)
    with pytest.raises(ValidationError, match="process id is not derived"):
        runtime.QualificationServiceProcessDeploymentV1.model_validate(
            {**node.model_dump(mode="python"), "process_id": f"qsp_{'0' * 32}"}
        )
