"""Frozen macOS launchd supervision for a run-once endurance controller.

The external scheduler is deliberately unable to start or finalize a gate.  Before an operator
starts the database clock, launchd may invoke ``run_supervisor_cycle`` repeatedly; those cycles
verify the frozen deployment and return ``waiting_for_explicit_start`` without mutating science.
Once the exact gate is live, the same invocation delegates one decision to the advisory-locked
controller.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.db import REPO_ROOT
from aletheia.programs.endurance_controller import (
    EnduranceControllerError,
    EnduranceControllerManifest,
    EnduranceControllerPreflight,
    EnduranceControllerTick,
    controller_status,
    preflight_endurance_controller,
    run_controller_tick,
    verify_endurance_controller_code_identity,
)
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SUPERVISOR_ID_PATTERN = r"^edsup_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_ENVIRONMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_LAUNCHD_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_LAUNCHD_DOMAIN_PATTERN = re.compile(r"^gui/[1-9][0-9]*$")


class EnduranceSupervisorError(RuntimeError):
    """The frozen external-supervisor deployment is invalid or unavailable."""


class EnduranceSupervisorConflict(EnduranceSupervisorError):
    """Live deployment bytes differ from the frozen supervisor identity."""


class EnduranceSupervisorCycleAction(str, Enum):
    WAITING_FOR_EXPLICIT_START = "waiting_for_explicit_start"
    CONTROLLER_TICK = "controller_tick"
    TERMINAL_NOOP = "terminal_noop"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path.resolve(strict=False)


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{label} must be a safe repository-relative path")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EnduranceSupervisorRuntime(_FrozenModel):
    conda_executable: str
    conda_sha256: str = Field(pattern=_SHA256_PATTERN)
    conda_environment: str = Field(pattern=_ENVIRONMENT_PATTERN)
    environment_prefix: str
    python_executable: str
    python_sha256: str = Field(pattern=_SHA256_PATTERN)
    python_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _absolute_runtime(self) -> "EnduranceSupervisorRuntime":
        conda = _absolute_path(self.conda_executable, label="Conda executable")
        prefix = _absolute_path(self.environment_prefix, label="Conda environment prefix")
        python = _absolute_path(self.python_executable, label="Python executable")
        try:
            python.relative_to(prefix)
        except ValueError as exc:
            raise ValueError("Python executable is outside the frozen Conda environment") from exc
        if prefix.name != self.conda_environment:
            raise ValueError("Conda environment name differs from its frozen prefix")
        object.__setattr__(self, "conda_executable", str(conda))
        object.__setattr__(self, "environment_prefix", str(prefix))
        object.__setattr__(self, "python_executable", str(python))
        return self


class EnduranceSupervisorManifest(_FrozenModel):
    schema_version: Literal[1] = 1
    supervisor_id: str | None = Field(default=None, pattern=_SUPERVISOR_ID_PATTERN)
    supervisor_key: str = Field(pattern=_IDENTITY_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    repository_root: str
    controller_manifest_path: str
    supervisor_manifest_path: str
    launchd_plist_path: str
    stdout_log_path: str
    stderr_log_path: str
    launchd_label: str
    launchd_domain: str
    poll_seconds: int = Field(ge=5, le=60 * 60)
    runtime: EnduranceSupervisorRuntime
    prepared_at: AwareDatetime
    platform: Literal["macos_launchd"] = "macos_launchd"
    run_at_load: Literal[True] = True
    automatic_start: Literal[False] = False
    automatic_finalization: Literal[False] = False

    @model_validator(mode="after")
    def _closed_deployment(self) -> "EnduranceSupervisorManifest":
        root = _absolute_path(self.repository_root, label="repository root")
        relative_fields = (
            "controller_manifest_path",
            "supervisor_manifest_path",
            "launchd_plist_path",
            "stdout_log_path",
            "stderr_log_path",
        )
        values = {
            field: _safe_relative(getattr(self, field), label=field).as_posix()
            for field in relative_fields
        }
        if len(set(values.values())) != len(values):
            raise ValueError("supervisor deployment paths must be distinct")
        if _LAUNCHD_LABEL_PATTERN.fullmatch(self.launchd_label) is None:
            raise ValueError("invalid launchd label")
        if _LAUNCHD_DOMAIN_PATTERN.fullmatch(self.launchd_domain) is None:
            raise ValueError("launchd domain must be gui/<positive uid>")
        object.__setattr__(self, "repository_root", str(root))
        for field, value in values.items():
            object.__setattr__(self, field, value)
        expected = f"edsup_{self.manifest_sha256[:32]}"
        if self.supervisor_id is not None and self.supervisor_id != expected:
            raise ValueError("supervisor ID differs from its manifest")
        object.__setattr__(self, "supervisor_id", expected)
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"supervisor_id"}))


class EnduranceSupervisorPreflight(_FrozenModel):
    supervisor_id: str = Field(pattern=_SUPERVISOR_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    database_observed_at: AwareDatetime
    eligible_for_explicit_start: bool
    blockers: tuple[str, ...]
    controller_preflight: EnduranceControllerPreflight | None
    controller_identity_verified: bool
    runtime_verified: bool
    deployment_files_verified: bool
    launchd_job_loaded: bool
    automatic_start: Literal[False] = False
    automatic_finalization: Literal[False] = False

    @model_validator(mode="after")
    def _derived_verdict(self) -> "EnduranceSupervisorPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("supervisor preflight blockers must be canonical")
        expected = (
            not blockers
            and self.controller_preflight is not None
            and self.controller_preflight.eligible_to_start
            and self.controller_identity_verified
            and self.runtime_verified
            and self.deployment_files_verified
            and self.launchd_job_loaded
        )
        if self.eligible_for_explicit_start != expected:
            raise ValueError("supervisor preflight verdict differs from blockers")
        return self


class EnduranceSupervisorCycle(_FrozenModel):
    supervisor_id: str = Field(pattern=_SUPERVISOR_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    action: EnduranceSupervisorCycleAction
    database_observed_at: AwareDatetime
    gate_state: Literal["not_started", "running", "terminal"]
    checkpoint_count: int = Field(ge=0)
    pending_envelope_ids: tuple[str, ...]
    controller_tick: EnduranceControllerTick | None = None
    automatic_start: Literal[False] = False
    automatic_finalization: Literal[False] = False

    @model_validator(mode="after")
    def _action_shape(self) -> "EnduranceSupervisorCycle":
        expected_tick = self.action is EnduranceSupervisorCycleAction.CONTROLLER_TICK
        if (self.controller_tick is not None) != expected_tick:
            raise ValueError("only a controller-tick supervisor cycle may contain a tick")
        expected_action = {
            "not_started": EnduranceSupervisorCycleAction.WAITING_FOR_EXPLICIT_START,
            "running": EnduranceSupervisorCycleAction.CONTROLLER_TICK,
            "terminal": EnduranceSupervisorCycleAction.TERMINAL_NOOP,
        }[self.gate_state]
        if self.action is not expected_action:
            raise ValueError("supervisor action differs from the durable gate state")
        return self


def capture_supervisor_runtime(
    *,
    conda_executable: Path,
    conda_environment: str,
) -> EnduranceSupervisorRuntime:
    conda = conda_executable.resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    if not conda.is_file() or not os.access(conda, os.X_OK):
        raise EnduranceSupervisorConflict("Conda executable is not executable")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise EnduranceSupervisorConflict("current Conda Python is not executable")
    return EnduranceSupervisorRuntime(
        conda_executable=str(conda),
        conda_sha256=_sha256_file(conda),
        conda_environment=conda_environment,
        environment_prefix=str(prefix),
        python_executable=str(python),
        python_sha256=_sha256_file(python),
        python_version=platform.python_version(),
    )


def _resolve_repository_path(manifest: EnduranceSupervisorManifest, relative: str) -> Path:
    root = Path(manifest.repository_root).resolve(strict=True)
    safe = _safe_relative(relative, label="supervisor artifact")
    path = (root / Path(*safe.parts)).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - safe relative paths cannot escape
        raise EnduranceSupervisorConflict("supervisor artifact escaped repository") from exc
    return path


def prepare_endurance_supervisor_manifest(
    controller: EnduranceControllerManifest,
    *,
    controller_manifest_path: Path,
    supervisor_manifest_path: Path,
    launchd_plist_path: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
    supervisor_key: str,
    launchd_label: str,
    launchd_domain: str,
    runtime: EnduranceSupervisorRuntime,
    repository_root: Path = REPO_ROOT,
    prepared_at: datetime | None = None,
) -> EnduranceSupervisorManifest:
    root = repository_root.resolve(strict=True)
    controller = EnduranceControllerManifest.model_validate(controller.model_dump(mode="python"))
    if controller.controller_id is None or controller.gate_manifest.gate_id is None:
        raise EnduranceSupervisorConflict("controller/gate identity is incomplete")
    controller_path = controller_manifest_path.resolve(strict=True)
    try:
        controller_relative = controller_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise EnduranceSupervisorConflict("controller manifest escaped repository") from exc
    persisted = EnduranceControllerManifest.model_validate_json(controller_path.read_bytes())
    if persisted != controller:
        raise EnduranceSupervisorConflict("controller manifest file differs from supplied identity")

    def relative_output(path: Path, label: str) -> str:
        resolved = path.resolve(strict=False)
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise EnduranceSupervisorConflict(f"{label} escaped repository") from exc

    return EnduranceSupervisorManifest(
        supervisor_key=supervisor_key,
        controller_id=controller.controller_id,
        controller_manifest_sha256=controller.manifest_sha256,
        controller_manifest_file_sha256=_sha256_file(controller_path),
        gate_id=controller.gate_manifest.gate_id,
        repository_root=str(root),
        controller_manifest_path=controller_relative,
        supervisor_manifest_path=relative_output(supervisor_manifest_path, "supervisor manifest"),
        launchd_plist_path=relative_output(launchd_plist_path, "launchd plist"),
        stdout_log_path=relative_output(stdout_log_path, "stdout log"),
        stderr_log_path=relative_output(stderr_log_path, "stderr log"),
        launchd_label=launchd_label,
        launchd_domain=launchd_domain,
        poll_seconds=controller.supervisor_poll_seconds,
        runtime=runtime,
        prepared_at=prepared_at or datetime.now(timezone.utc),
    )


def render_endurance_launchd_plist(manifest: EnduranceSupervisorManifest) -> bytes:
    manifest = EnduranceSupervisorManifest.model_validate(manifest.model_dump(mode="python"))
    root = Path(manifest.repository_root)
    supervisor_path = _resolve_repository_path(manifest, manifest.supervisor_manifest_path)
    script_path = root / "scripts" / "run_endurance_supervisor.py"
    payload: dict[str, Any] = {
        "Label": manifest.launchd_label,
        "ProgramArguments": [
            manifest.runtime.conda_executable,
            "run",
            "--no-capture-output",
            "-n",
            manifest.runtime.conda_environment,
            "python",
            str(script_path),
            "cycle",
            str(supervisor_path),
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StartInterval": manifest.poll_seconds,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(_resolve_repository_path(manifest, manifest.stdout_log_path)),
        "StandardErrorPath": str(_resolve_repository_path(manifest, manifest.stderr_log_path)),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "Umask": 0o077,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def load_supervisor_controller(
    manifest: EnduranceSupervisorManifest,
) -> EnduranceControllerManifest:
    path = _resolve_repository_path(manifest, manifest.controller_manifest_path)
    if not path.is_file() or _sha256_file(path) != manifest.controller_manifest_file_sha256:
        raise EnduranceSupervisorConflict("controller manifest file bytes changed")
    controller = EnduranceControllerManifest.model_validate_json(path.read_bytes())
    if (
        controller.controller_id != manifest.controller_id
        or controller.manifest_sha256 != manifest.controller_manifest_sha256
        or controller.gate_manifest.gate_id != manifest.gate_id
        or controller.supervisor_poll_seconds != manifest.poll_seconds
    ):
        raise EnduranceSupervisorConflict("controller manifest binding changed")
    return controller


def verify_endurance_supervisor_runtime(runtime: EnduranceSupervisorRuntime) -> None:
    runtime = EnduranceSupervisorRuntime.model_validate(runtime.model_dump(mode="python"))
    for label, value, expected in (
        ("Conda", runtime.conda_executable, runtime.conda_sha256),
        ("Python", runtime.python_executable, runtime.python_sha256),
    ):
        path = Path(value)
        if not path.is_file() or not os.access(path, os.X_OK) or _sha256_file(path) != expected:
            raise EnduranceSupervisorConflict(f"{label} runtime identity changed")
    if (
        Path(sys.executable).resolve(strict=True) != Path(runtime.python_executable)
        or Path(sys.prefix).resolve(strict=True) != Path(runtime.environment_prefix)
        or platform.python_version() != runtime.python_version
    ):
        raise EnduranceSupervisorConflict("supervisor is running outside the frozen Conda Python")


def verify_endurance_supervisor_files(
    manifest: EnduranceSupervisorManifest,
) -> EnduranceControllerManifest:
    manifest = EnduranceSupervisorManifest.model_validate(manifest.model_dump(mode="python"))
    if Path(manifest.repository_root).resolve(strict=True) != REPO_ROOT.resolve(strict=True):
        raise EnduranceSupervisorConflict("supervisor is running from another repository root")
    controller = load_supervisor_controller(manifest)
    verify_endurance_controller_code_identity(
        controller.code_identity,
        repository_root=Path(manifest.repository_root),
    )
    verify_endurance_supervisor_runtime(manifest.runtime)
    plist_path = _resolve_repository_path(manifest, manifest.launchd_plist_path)
    expected_plist = render_endurance_launchd_plist(manifest)
    if not plist_path.is_file() or plist_path.read_bytes() != expected_plist:
        raise EnduranceSupervisorConflict("launchd plist bytes differ from frozen deployment")
    manifest_path = _resolve_repository_path(manifest, manifest.supervisor_manifest_path)
    if not manifest_path.is_file():
        raise EnduranceSupervisorConflict("supervisor manifest file is missing")
    persisted = EnduranceSupervisorManifest.model_validate_json(manifest_path.read_bytes())
    if persisted != manifest:
        raise EnduranceSupervisorConflict("supervisor manifest file differs from live identity")
    return controller


def launchd_job_loaded(manifest: EnduranceSupervisorManifest) -> bool:
    result = subprocess.run(
        ("launchctl", "print", f"{manifest.launchd_domain}/{manifest.launchd_label}"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    root = Path(manifest.repository_root)
    required = (
        manifest.runtime.conda_executable,
        str(root / "scripts" / "run_endurance_supervisor.py"),
        str(_resolve_repository_path(manifest, manifest.supervisor_manifest_path)),
        str(_resolve_repository_path(manifest, manifest.stdout_log_path)),
        str(_resolve_repository_path(manifest, manifest.stderr_log_path)),
        f"run interval = {manifest.poll_seconds} seconds",
    )
    return all(value in result.stdout for value in required)


def preflight_endurance_supervisor(
    manifest: EnduranceSupervisorManifest,
    *,
    loaded_probe: Callable[[EnduranceSupervisorManifest], bool] = launchd_job_loaded,
) -> EnduranceSupervisorPreflight:
    manifest = EnduranceSupervisorManifest.model_validate(manifest.model_dump(mode="python"))
    blockers: list[str] = []
    controller: EnduranceControllerManifest | None = None
    controller_report: EnduranceControllerPreflight | None = None
    controller_ok = runtime_ok = files_ok = False
    try:
        controller = load_supervisor_controller(manifest)
        verify_endurance_controller_code_identity(
            controller.code_identity,
            repository_root=Path(manifest.repository_root),
        )
        controller_ok = True
    except (EnduranceSupervisorError, EnduranceControllerError, OSError, ValueError):
        blockers.append("controller:identity_or_manifest_changed")
    try:
        verify_endurance_supervisor_runtime(manifest.runtime)
        runtime_ok = True
    except (EnduranceSupervisorError, OSError, ValueError):
        blockers.append("runtime:conda_or_python_changed")
    try:
        plist_path = _resolve_repository_path(manifest, manifest.launchd_plist_path)
        manifest_path = _resolve_repository_path(manifest, manifest.supervisor_manifest_path)
        if plist_path.read_bytes() != render_endurance_launchd_plist(manifest):
            raise EnduranceSupervisorConflict("launchd plist changed")
        if EnduranceSupervisorManifest.model_validate_json(manifest_path.read_bytes()) != manifest:
            raise EnduranceSupervisorConflict("supervisor manifest changed")
        files_ok = True
    except (EnduranceSupervisorError, OSError, ValueError):
        blockers.append("deployment:manifest_or_plist_changed")
    loaded = False
    try:
        loaded = bool(loaded_probe(manifest))
    except OSError:
        loaded = False
    if not loaded:
        blockers.append("launchd:job_not_loaded")
    if controller is not None and controller_ok:
        try:
            controller_report = preflight_endurance_controller(
                controller,
                repository_root=Path(manifest.repository_root),
                artifact_root=Path(manifest.repository_root),
            )
            blockers.extend(f"controller:{item}" for item in controller_report.blockers)
        except (EnduranceControllerError, OSError, ValueError):
            blockers.append("controller:preflight_failed")
    observed = (
        controller_report.database_observed_at
        if controller_report is not None
        else datetime.now(timezone.utc)
    )
    canonical = tuple(sorted(set(blockers)))
    assert manifest.supervisor_id is not None
    return EnduranceSupervisorPreflight(
        supervisor_id=manifest.supervisor_id,
        controller_id=manifest.controller_id,
        gate_id=manifest.gate_id,
        database_observed_at=observed,
        eligible_for_explicit_start=(
            not canonical
            and controller_report is not None
            and controller_report.eligible_to_start
            and controller_ok
            and runtime_ok
            and files_ok
            and loaded
        ),
        blockers=canonical,
        controller_preflight=controller_report,
        controller_identity_verified=controller_ok,
        runtime_verified=runtime_ok,
        deployment_files_verified=files_ok,
        launchd_job_loaded=loaded,
    )


def run_endurance_supervisor_cycle(
    manifest: EnduranceSupervisorManifest,
    *,
    now: datetime | None = None,
) -> EnduranceSupervisorCycle:
    manifest = EnduranceSupervisorManifest.model_validate(manifest.model_dump(mode="python"))
    controller = verify_endurance_supervisor_files(manifest)
    status = controller_status(
        controller,
        artifact_root=Path(manifest.repository_root),
        now=now,
    )
    state = str(status["state"])
    assert state in {"not_started", "running", "terminal"}
    tick: EnduranceControllerTick | None = None
    action = {
        "not_started": EnduranceSupervisorCycleAction.WAITING_FOR_EXPLICIT_START,
        "running": EnduranceSupervisorCycleAction.CONTROLLER_TICK,
        "terminal": EnduranceSupervisorCycleAction.TERMINAL_NOOP,
    }[state]
    if state == "running":
        tick = run_controller_tick(
            controller,
            repository_root=Path(manifest.repository_root),
            artifact_root=Path(manifest.repository_root),
            now=now,
        )
        status = controller_status(
            controller,
            artifact_root=Path(manifest.repository_root),
            now=now,
        )
    observed_at = datetime.fromisoformat(str(status["database_observed_at"]))
    pending = tuple(sorted(str(item) for item in status["pending_envelope_ids"]))
    assert manifest.supervisor_id is not None
    return EnduranceSupervisorCycle(
        supervisor_id=manifest.supervisor_id,
        controller_id=manifest.controller_id,
        gate_id=manifest.gate_id,
        action=action,
        database_observed_at=observed_at,
        gate_state=state,
        checkpoint_count=int(status["checkpoint_count"]),
        pending_envelope_ids=pending,
        controller_tick=tick,
    )


__all__ = [
    "EnduranceSupervisorConflict",
    "EnduranceSupervisorCycle",
    "EnduranceSupervisorCycleAction",
    "EnduranceSupervisorError",
    "EnduranceSupervisorManifest",
    "EnduranceSupervisorPreflight",
    "EnduranceSupervisorRuntime",
    "capture_supervisor_runtime",
    "launchd_job_loaded",
    "load_supervisor_controller",
    "preflight_endurance_supervisor",
    "prepare_endurance_supervisor_manifest",
    "render_endurance_launchd_plist",
    "run_endurance_supervisor_cycle",
    "verify_endurance_supervisor_files",
    "verify_endurance_supervisor_runtime",
]
