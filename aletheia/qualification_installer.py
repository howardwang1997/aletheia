"""Opt-in, crash-replayable installation of disabled qualification service files.

This outer deployment boundary installs only the exact service manifest and five systemd unit
files.  It never creates principals, applies PostgreSQL ACLs, enables or starts a service, or grants
scientific authority.  Each mutation is preceded by an immutable intent and followed by an
immutable completion record under one root-owned journal.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.qualification_deployment import (
    QualificationDeploymentSpecV1,
    QualificationExpectedRootExecutable,
    render_postgresql_acl,
    render_systemd_units,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256
from aletheia.qualification_service_runtime import (
    QualificationServiceDeploymentManifestV1,
    QualificationServiceRole,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MAX_INSTALL_FILE_BYTES = 16 * 1024 * 1024
_OPT_IN_CONFIRMATION = "INSTALL_QUALIFICATION_ONLY_DISABLED"


class QualificationInstallationError(RuntimeError):
    """The installation request, host, journal, or exact file state failed closed."""


def _canonical_absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not path.is_absolute()
        or str(path) != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be one canonical absolute path")
    return path


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _process_can_read(
    *,
    process_uid: int,
    process_gid: int,
    owner_uid: int,
    owner_gid: int,
    mode: int,
) -> bool:
    return bool(
        (process_uid == owner_uid and mode & 0o400)
        or (process_gid == owner_gid and mode & 0o040)
        or mode & 0o004
    )


class QualificationInstallationRequestV1(ExecutionModel):
    """Immutable root-operator request for one disabled file installation."""

    schema_name: Literal["aletheia.qualification_installation_request"] = (
        "aletheia.qualification_installation_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=r"^qir_[0-9a-f]{32}$")
    deployment_spec: QualificationDeploymentSpecV1
    service_manifest: QualificationServiceDeploymentManifestV1
    journal_root: str
    systemctl_executable: QualificationExpectedRootExecutable
    requested_at: AwareDatetime
    opt_in_confirmation: Literal["INSTALL_QUALIFICATION_ONLY_DISABLED"] = _OPT_IN_CONFIRMATION
    install_service_manifest: Literal[True] = True
    install_systemd_units: Literal[True] = True
    daemon_reload_allowed: Literal[True] = True
    create_principals_allowed: Literal[False] = False
    apply_postgresql_acl: Literal[False] = False
    enable_services: Literal[False] = False
    start_services: Literal[False] = False
    automatic_installation: Literal[False] = False
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_closed(self) -> "QualificationInstallationRequestV1":
        spec = self.deployment_spec
        manifest = self.service_manifest
        journal_root = _canonical_absolute_path(self.journal_root, label="installer journal root")
        systemctl = _canonical_absolute_path(
            self.systemctl_executable.path,
            label="systemctl executable",
        )
        if (
            manifest.deployment_id != spec.deployment_id
            or manifest.file_sha256 != spec.deployment_manifest_sha256
            or self.requested_at < manifest.prepared_at
        ):
            raise ValueError("service manifest differs from the portable deployment spec")
        expected_identities = {
            QualificationServiceRole.WORKSPACE: (0, 0, None),
            QualificationServiceRole.QUOTA: (0, 0, None),
            QualificationServiceRole.WATCHDOG: (0, 0, None),
            QualificationServiceRole.NODE: (
                spec.node_uid,
                spec.node_gid,
                spec.worker_poll_milliseconds,
            ),
            QualificationServiceRole.OUTBOX: (spec.outbox_uid, spec.outbox_gid, None),
        }
        reviewed_entries = {entry.relative_path: entry for entry in spec.reviewed_code_tree.entries}
        config_paths: list[Path] = []
        for process in manifest.processes:
            if (
                process.reviewed_code_root != spec.code_root
                or (
                    process.process_uid,
                    process.process_gid,
                    process.worker_poll_milliseconds,
                )
                != expected_identities[process.role]
            ):
                raise ValueError("service process identity differs from the deployment spec")
            source = Path(process.composition_factory_source_path)
            relative = str(source.relative_to(spec.code_root))
            reviewed = reviewed_entries.get(relative)
            if (
                reviewed is None
                or process.composition_factory_source_sha256 != reviewed.reviewed_sha256
                or process.composition_factory_owner_uid != reviewed.expected_owner_uid
                or process.composition_factory_owner_gid != reviewed.expected_owner_gid
                or process.composition_factory_mode != reviewed.expected_mode
            ):
                raise ValueError("service factory is not an exact reviewed code-tree entry")
            if not _process_can_read(
                process_uid=process.process_uid,
                process_gid=process.process_gid,
                owner_uid=process.composition_config_owner_uid,
                owner_gid=process.composition_config_owner_gid,
                mode=process.composition_config_mode,
            ):
                raise ValueError("service process cannot read its exact composition config")
            config_paths.append(Path(process.composition_config_path))
        guarded_roots = tuple(
            Path(value)
            for value in (
                spec.code_root,
                spec.reviewed_python_environment.root_path,
                spec.systemd_unit_root,
                spec.workspace_source_root,
                spec.output_workspace_root,
                spec.quota_backing_root,
                spec.quota_state_root,
                spec.watchdog_state_root,
                spec.runtime_journal_root,
                spec.node_state_root,
                spec.artifact_store_root,
                spec.input_materialization_journal_root,
                spec.authority_registry_root,
                spec.oci_layout_root,
                spec.outbox_spool_root,
            )
        )
        if any(_paths_overlap(config, root) for config in config_paths for root in guarded_roots):
            raise ValueError("service composition config overlaps a guarded deployment root")
        protected_paths = (
            Path(spec.deployment_manifest_path),
            systemctl,
            *config_paths,
            *(Path(unit.path) for unit in render_systemd_units(spec)),
        )
        if any(
            _paths_overlap(left, right)
            for index, left in enumerate(protected_paths)
            for right in protected_paths[index + 1 :]
        ):
            raise ValueError("installer inputs and targets must not overlap")
        if any(_paths_overlap(journal_root, path) for path in (*protected_paths, *guarded_roots)):
            raise ValueError("installer journal overlaps an installed or pinned file")
        if any(_paths_overlap(systemctl, root) for root in guarded_roots):
            raise ValueError("systemctl executable overlaps a guarded deployment root")
        expected_id = f"qir_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected_id:
            raise ValueError("qualification installation request id is not derived")
        object.__setattr__(self, "request_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def file_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


class QualificationInstallationArtifactV1(ExecutionModel):
    """One exact immutable target published by the disabled installer."""

    schema_name: Literal["aletheia.qualification_installation_artifact"] = (
        "aletheia.qualification_installation_artifact"
    )
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=5)
    artifact_kind: Literal["service_manifest", "systemd_unit"]
    unit_name: str | None = None
    target_path: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1, le=_MAX_INSTALL_FILE_BYTES)
    owner_uid: Literal[0] = 0
    owner_gid: Literal[0] = 0
    mode: int = Field(ge=0, le=0o7777)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _artifact_is_closed(self) -> "QualificationInstallationArtifactV1":
        path = _canonical_absolute_path(self.target_path, label="installation artifact")
        is_unit = self.artifact_kind == "systemd_unit"
        if is_unit != (self.unit_name is not None):
            raise ValueError("installation artifact kind and unit identity differ")
        if self.unit_name is not None and path.name != self.unit_name:
            raise ValueError("systemd installation artifact path differs from its unit")
        if self.mode & 0o7133 or self.mode & 0o404 != 0o404:
            raise ValueError("installation artifact must be root-controlled and read-only")
        return self

    @property
    def artifact_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationInstallationPlanV1(ExecutionModel):
    """Deterministic six-file plan; planning itself mutates nothing."""

    schema_name: Literal["aletheia.qualification_installation_plan"] = (
        "aletheia.qualification_installation_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str | None = Field(default=None, pattern=r"^qip_[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_systemd_units_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_postgresql_acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifacts: tuple[QualificationInstallationArtifactV1, ...] = Field(
        min_length=6,
        max_length=6,
    )
    postgresql_acl_applied: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_canonical(self) -> "QualificationInstallationPlanV1":
        if tuple(item.ordinal for item in self.artifacts) != tuple(range(6)):
            raise ValueError("installation artifacts must have exhaustive canonical ordinals")
        if self.artifacts[0].artifact_kind != "service_manifest" or any(
            item.artifact_kind != "systemd_unit" for item in self.artifacts[1:]
        ):
            raise ValueError("installation plan must contain one manifest then five units")
        if tuple(item.unit_name for item in self.artifacts[1:]) != tuple(
            sorted(item.unit_name for item in self.artifacts[1:])
        ):
            raise ValueError("systemd installation artifacts must be canonically ordered")
        if len({item.target_path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("installation artifact targets must be unique")
        expected_id = f"qip_{self.identity_sha256[:32]}"
        if self.plan_id is not None and self.plan_id != expected_id:
            raise ValueError("qualification installation plan id is not derived")
        object.__setattr__(self, "plan_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"}))

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationInstalledFileObservation(ExecutionModel):
    """Fresh local observation of one installed regular file."""

    path: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1, le=_MAX_INSTALL_FILE_BYTES)
    owner_uid: int = Field(ge=0, le=2**31 - 1)
    owner_gid: int = Field(ge=0, le=2**31 - 1)
    mode: int = Field(ge=0, le=0o7777)
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    link_count: Literal[1] = 1
    file_type: Literal["regular"] = "regular"
    symlink: Literal[False] = False

    @model_validator(mode="after")
    def _path_is_canonical(self) -> "QualificationInstalledFileObservation":
        _canonical_absolute_path(self.path, label="installed file observation")
        return self

    @property
    def observation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationSystemdUnitState(ExecutionModel):
    unit_name: str
    load_state: Literal["loaded", "not-found"]
    active_state: Literal["inactive"] = "inactive"
    unit_file_state: Literal["disabled", "not-found"]


class QualificationSystemdQuiescenceObservation(ExecutionModel):
    schema_name: Literal["aletheia.qualification_systemd_quiescence"] = (
        "aletheia.qualification_systemd_quiescence"
    )
    schema_version: Literal[1] = 1
    units: tuple[QualificationSystemdUnitState, ...] = Field(min_length=5, max_length=5)
    observed_at: AwareDatetime
    all_inactive: Literal[True] = True
    all_disabled_or_absent: Literal[True] = True

    @model_validator(mode="after")
    def _units_are_canonical(self) -> "QualificationSystemdQuiescenceObservation":
        names = tuple(item.unit_name for item in self.units)
        if names != tuple(sorted(set(names))):
            raise ValueError("systemd quiescence units must be unique and canonical")
        return self

    @property
    def observation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationInstallationActiveRequest(ExecutionModel):
    schema_name: Literal["aletheia.qualification_installation_active_request"] = (
        "aletheia.qualification_installation_active_request"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qip_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    automatic_start: Literal[False] = False


class QualificationInstallationArtifactIntent(ExecutionModel):
    schema_name: Literal["aletheia.qualification_installation_artifact_intent"] = (
        "aletheia.qualification_installation_artifact_intent"
    )
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact: QualificationInstallationArtifactV1

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationInstallationArtifactCompletion(ExecutionModel):
    schema_name: Literal["aletheia.qualification_installation_artifact_completion"] = (
        "aletheia.qualification_installation_artifact_completion"
    )
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_ordinal: int = Field(ge=0, le=5)
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_file: QualificationInstalledFileObservation
    installed_at: AwareDatetime

    @property
    def completion_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationDaemonReloadReceipt(ExecutionModel):
    schema_name: Literal["aletheia.qualification_daemon_reload_receipt"] = (
        "aletheia.qualification_daemon_reload_receipt"
    )
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    systemctl_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    invocation_sha256: str = Field(pattern=_SHA256_PATTERN)
    reloaded_at: AwareDatetime
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationInstallationReceiptV1(ExecutionModel):
    """Final operational receipt for exact files left disabled and inactive."""

    schema_name: Literal["aletheia.qualification_installation_receipt"] = (
        "aletheia.qualification_installation_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^qix_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    request_id: str = Field(pattern=r"^qir_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qip_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_completions: tuple[QualificationInstallationArtifactCompletion, ...] = Field(
        min_length=6,
        max_length=6,
    )
    daemon_reload_receipt: QualificationDaemonReloadReceipt
    quiescence_before_sha256: str = Field(pattern=_SHA256_PATTERN)
    quiescence_after_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    create_principals_performed: Literal[False] = False
    postgresql_acl_applied: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_complete(self) -> "QualificationInstallationReceiptV1":
        if tuple(item.artifact_ordinal for item in self.artifact_completions) != tuple(range(6)):
            raise ValueError("installation receipt must cover all artifacts canonically")
        completion_times = tuple(item.installed_at for item in self.artifact_completions)
        if completion_times != tuple(sorted(completion_times)):
            raise ValueError("artifact completion timestamps are not canonical")
        if any(
            item.request_id != self.request_id
            or item.plan_sha256 != self.plan_sha256
            or item.installed_at > self.completed_at
            for item in self.artifact_completions
        ):
            raise ValueError("artifact completion differs from installation receipt")
        if (
            self.daemon_reload_receipt.request_id != self.request_id
            or self.daemon_reload_receipt.plan_sha256 != self.plan_sha256
            or self.daemon_reload_receipt.reloaded_at < completion_times[-1]
            or self.daemon_reload_receipt.reloaded_at > self.completed_at
        ):
            raise ValueError("daemon reload receipt differs from installation completion")
        expected_id = f"qix_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("qualification installation receipt id is not derived")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def _artifact_payloads(
    request: QualificationInstallationRequestV1,
) -> tuple[tuple[QualificationInstallationArtifactV1, bytes], ...]:
    spec = request.deployment_spec
    manifest_bytes = canonical_json_bytes(request.service_manifest)
    units = render_systemd_units(spec)
    items: list[tuple[QualificationInstallationArtifactV1, bytes]] = [
        (
            QualificationInstallationArtifactV1(
                ordinal=0,
                artifact_kind="service_manifest",
                target_path=spec.deployment_manifest_path,
                content_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                byte_length=len(manifest_bytes),
                owner_uid=spec.expected_deployment_manifest.expected_owner_uid,
                owner_gid=spec.expected_deployment_manifest.expected_owner_gid,
                mode=spec.expected_deployment_manifest.expected_mode,
            ),
            manifest_bytes,
        )
    ]
    for ordinal, unit in enumerate(units, start=1):
        payload = unit.content.encode("utf-8")
        items.append(
            (
                QualificationInstallationArtifactV1(
                    ordinal=ordinal,
                    artifact_kind="systemd_unit",
                    unit_name=unit.unit_name,
                    target_path=unit.path,
                    content_sha256=unit.content_sha256,
                    byte_length=len(payload),
                    owner_uid=unit.owner_uid,
                    owner_gid=unit.owner_gid,
                    mode=unit.mode,
                ),
                payload,
            )
        )
    return tuple(items)


def build_qualification_installation_plan(
    request: QualificationInstallationRequestV1,
) -> QualificationInstallationPlanV1:
    """Build the exact disabled installation plan without reading or mutating a host."""

    request = QualificationInstallationRequestV1.model_validate(request.model_dump(mode="python"))
    units = render_systemd_units(request.deployment_spec)
    return QualificationInstallationPlanV1(
        request_id=request.request_id,
        request_sha256=canonical_sha256(request),
        deployment_id=request.deployment_spec.deployment_id,
        spec_sha256=request.deployment_spec.spec_sha256,
        service_manifest_sha256=request.service_manifest.file_sha256,
        rendered_systemd_units_sha256=canonical_sha256(units),
        rendered_postgresql_acl_sha256=hashlib.sha256(
            render_postgresql_acl(request.deployment_spec)
        ).hexdigest(),
        artifacts=tuple(item for item, _payload in _artifact_payloads(request)),
    )


class QualificationInstallationHostPort(Protocol):
    """Narrow privileged host operations required by the disabled installer."""

    def assert_linux_root(self) -> None: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def verify_pinned_inputs(self) -> None: ...

    def observe_systemd(
        self,
        unit_names: tuple[str, ...],
    ) -> QualificationSystemdQuiescenceObservation: ...

    def read_journal(self, path: Path) -> bytes | None: ...

    def write_journal_once(self, path: Path, payload: bytes) -> None: ...

    def publish_artifact(
        self,
        artifact: QualificationInstallationArtifactV1,
        payload: bytes,
    ) -> QualificationInstalledFileObservation: ...

    def observe_artifact(
        self,
        artifact: QualificationInstallationArtifactV1,
    ) -> QualificationInstalledFileObservation: ...

    def daemon_reload(self) -> str: ...


def _journal_paths(
    request: QualificationInstallationRequestV1,
) -> tuple[Path, Path]:
    root = Path(request.journal_root)
    deployment_key = hashlib.sha256(request.deployment_spec.deployment_id.encode()).hexdigest()[:32]
    return root / f"active-{deployment_key}.json", root / request.request_id


def _validated_journal_model(
    payload: bytes | None,
    model_type: type[ExecutionModel],
    *,
    label: str,
) -> ExecutionModel | None:
    if payload is None:
        return None
    try:
        value = model_type.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise QualificationInstallationError(f"{label} journal is invalid") from exc
    if payload != canonical_json_bytes(value):
        raise QualificationInstallationError(f"{label} journal is not canonical")
    return value


def _observation_matches_artifact(
    observation: QualificationInstalledFileObservation,
    artifact: QualificationInstallationArtifactV1,
) -> bool:
    return (
        observation.path == artifact.target_path
        and observation.content_sha256 == artifact.content_sha256
        and observation.byte_length == artifact.byte_length
        and observation.owner_uid == artifact.owner_uid
        and observation.owner_gid == artifact.owner_gid
        and observation.mode == artifact.mode
    )


def _quiescence_matches_units(
    observation: QualificationSystemdQuiescenceObservation,
    unit_names: tuple[str, ...],
    *,
    require_loaded: bool,
) -> bool:
    return tuple(item.unit_name for item in observation.units) == unit_names and (
        not require_loaded or all(item.load_state == "loaded" for item in observation.units)
    )


def install_qualification_service_files(
    request: QualificationInstallationRequestV1,
    host: QualificationInstallationHostPort,
    *,
    clock: Callable[[], datetime] | None = None,
    fault: Callable[[str], None] | None = None,
) -> QualificationInstallationReceiptV1:
    """Install or resume the exact six files while leaving every unit disabled and inactive."""

    request = QualificationInstallationRequestV1.model_validate(request.model_dump(mode="python"))
    plan = build_qualification_installation_plan(request)
    payloads = dict(
        (artifact.ordinal, payload) for artifact, payload in _artifact_payloads(request)
    )
    now = clock or (lambda: datetime.now(timezone.utc))
    last_timestamp = request.requested_at

    def monitored_now() -> datetime:
        nonlocal last_timestamp
        observed = now()
        if observed < last_timestamp:
            raise QualificationInstallationError("installer clock moved backwards")
        last_timestamp = observed
        return observed

    inject = fault or (lambda _phase: None)
    unit_names = tuple(item.unit_name for item in plan.artifacts[1:] if item.unit_name is not None)
    active_path, request_root = _journal_paths(request)
    request_sha256 = canonical_sha256(request)
    active = QualificationInstallationActiveRequest(
        deployment_id=plan.deployment_id,
        request_id=request.request_id,
        request_sha256=request_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )

    host.assert_linux_root()
    with host.lock():
        host.verify_pinned_inputs()
        quiescence_before = host.observe_systemd(unit_names)
        if not _quiescence_matches_units(
            quiescence_before,
            unit_names,
            require_loaded=False,
        ):
            raise QualificationInstallationError(
                "systemd pre-installation observation differs from exact units"
            )
        host.write_journal_once(active_path, canonical_json_bytes(active))
        host.write_journal_once(request_root / "request.json", canonical_json_bytes(request))
        host.write_journal_once(request_root / "plan.json", canonical_json_bytes(plan))
        inject("after_journal_initialized")

        existing_receipt = _validated_journal_model(
            host.read_journal(request_root / "receipt.json"),
            QualificationInstallationReceiptV1,
            label="installation receipt",
        )
        if existing_receipt is not None:
            assert isinstance(existing_receipt, QualificationInstallationReceiptV1)
            if (
                existing_receipt.deployment_id != plan.deployment_id
                or existing_receipt.request_sha256 != request_sha256
                or existing_receipt.plan_sha256 != plan.plan_sha256
                or existing_receipt.request_id != request.request_id
                or existing_receipt.plan_id != plan.plan_id
            ):
                raise QualificationInstallationError(
                    "existing installation receipt differs from exact retry"
                )
            for artifact, completion in zip(
                plan.artifacts,
                existing_receipt.artifact_completions,
                strict=True,
            ):
                intent = QualificationInstallationArtifactIntent(
                    request_id=request.request_id,
                    plan_sha256=plan.plan_sha256,
                    artifact=artifact,
                )
                observed = host.observe_artifact(artifact)
                if (
                    completion.request_id != request.request_id
                    or completion.plan_sha256 != plan.plan_sha256
                    or completion.artifact_ordinal != artifact.ordinal
                    or completion.artifact_sha256 != artifact.artifact_sha256
                    or completion.intent_sha256 != intent.intent_sha256
                    or not _observation_matches_artifact(
                        completion.installed_file,
                        artifact,
                    )
                    or observed != completion.installed_file
                ):
                    raise QualificationInstallationError(
                        "installed artifact changed after completed installation"
                    )
                if completion.installed_at < last_timestamp:
                    raise QualificationInstallationError(
                        "installation receipt timestamps are not canonical"
                    )
                last_timestamp = completion.installed_at
            if existing_receipt.daemon_reload_receipt.reloaded_at < last_timestamp:
                raise QualificationInstallationError(
                    "installation receipt timestamps are not canonical"
                )
            after = host.observe_systemd(unit_names)
            if not _quiescence_matches_units(after, unit_names, require_loaded=True):
                raise QualificationInstallationError(
                    "completed installation units are not loaded and quiescent"
                )
            return existing_receipt

        completions: list[QualificationInstallationArtifactCompletion] = []
        for artifact in plan.artifacts:
            intent = QualificationInstallationArtifactIntent(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                artifact=artifact,
            )
            intent_path = request_root / f"artifact-{artifact.ordinal}.intent.json"
            completion_path = request_root / f"artifact-{artifact.ordinal}.completed.json"
            host.write_journal_once(intent_path, canonical_json_bytes(intent))
            stored_completion = _validated_journal_model(
                host.read_journal(completion_path),
                QualificationInstallationArtifactCompletion,
                label=f"artifact {artifact.ordinal} completion",
            )
            if stored_completion is None:
                observed = host.publish_artifact(artifact, payloads[artifact.ordinal])
                if not _observation_matches_artifact(observed, artifact):
                    raise QualificationInstallationError(
                        "host publication observation differs from installation artifact"
                    )
                inject(f"after_artifact_publish:{artifact.ordinal}")
                completion = QualificationInstallationArtifactCompletion(
                    request_id=request.request_id,
                    plan_sha256=plan.plan_sha256,
                    artifact_ordinal=artifact.ordinal,
                    artifact_sha256=artifact.artifact_sha256,
                    intent_sha256=intent.intent_sha256,
                    installed_file=observed,
                    installed_at=monitored_now(),
                )
                host.write_journal_once(completion_path, canonical_json_bytes(completion))
                inject(f"after_artifact_completion:{artifact.ordinal}")
            else:
                assert isinstance(
                    stored_completion,
                    QualificationInstallationArtifactCompletion,
                )
                completion = stored_completion
                if completion.installed_at < last_timestamp:
                    raise QualificationInstallationError(
                        "artifact completion timestamps are not canonical"
                    )
                last_timestamp = completion.installed_at
                if (
                    completion.request_id != request.request_id
                    or completion.plan_sha256 != plan.plan_sha256
                    or completion.artifact_ordinal != artifact.ordinal
                    or completion.artifact_sha256 != artifact.artifact_sha256
                    or completion.intent_sha256 != intent.intent_sha256
                    or not _observation_matches_artifact(
                        completion.installed_file,
                        artifact,
                    )
                    or host.observe_artifact(artifact) != completion.installed_file
                ):
                    raise QualificationInstallationError(
                        "artifact completion differs from exact retry"
                    )
            completions.append(completion)

        reload_path = request_root / "daemon-reload.json"
        stored_reload = _validated_journal_model(
            host.read_journal(reload_path),
            QualificationDaemonReloadReceipt,
            label="daemon reload",
        )
        if stored_reload is None:
            invocation_sha256 = host.daemon_reload()
            reload_receipt = QualificationDaemonReloadReceipt(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                systemctl_executable_sha256=(request.systemctl_executable.reviewed_sha256),
                invocation_sha256=invocation_sha256,
                reloaded_at=monitored_now(),
            )
            host.write_journal_once(reload_path, canonical_json_bytes(reload_receipt))
        else:
            assert isinstance(stored_reload, QualificationDaemonReloadReceipt)
            reload_receipt = stored_reload
            if reload_receipt.reloaded_at < last_timestamp:
                raise QualificationInstallationError(
                    "daemon reload timestamp precedes artifact completion"
                )
            last_timestamp = reload_receipt.reloaded_at
            if (
                reload_receipt.request_id != request.request_id
                or reload_receipt.plan_sha256 != plan.plan_sha256
                or reload_receipt.systemctl_executable_sha256
                != request.systemctl_executable.reviewed_sha256
            ):
                raise QualificationInstallationError(
                    "daemon reload journal differs from exact retry"
                )
        inject("after_daemon_reload")
        quiescence_after = host.observe_systemd(unit_names)
        if not _quiescence_matches_units(
            quiescence_after,
            unit_names,
            require_loaded=True,
        ):
            raise QualificationInstallationError(
                "installed systemd units are not loaded and quiescent"
            )
        receipt = QualificationInstallationReceiptV1(
            deployment_id=plan.deployment_id,
            request_id=request.request_id,
            request_sha256=request_sha256,
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            artifact_completions=tuple(completions),
            daemon_reload_receipt=reload_receipt,
            quiescence_before_sha256=quiescence_before.observation_sha256,
            quiescence_after_sha256=quiescence_after.observation_sha256,
            completed_at=monitored_now(),
        )
        host.write_journal_once(request_root / "receipt.json", canonical_json_bytes(receipt))
        inject("after_receipt")
        return receipt


def _fresh_file(
    path_value: str | Path,
    *,
    expected_sha256: str | None,
    expected_owner_uid: int | None = None,
    expected_owner_gid: int | None = None,
    expected_mode: int | None = None,
    maximum_bytes: int = _MAX_INSTALL_FILE_BYTES,
    preserve_missing: bool = False,
) -> tuple[bytes, QualificationInstalledFileObservation]:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path:
            raise QualificationInstallationError("pinned file path traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError as exc:
        if preserve_missing:
            raise
        raise QualificationInstallationError("pinned file is missing") from exc
    except OSError as exc:
        raise QualificationInstallationError("pinned file cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= maximum_bytes
            or before.st_nlink != 1
            or (expected_owner_uid is not None and before.st_uid != expected_owner_uid)
            or (expected_owner_gid is not None and before.st_gid != expected_owner_gid)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
        ):
            raise QualificationInstallationError("pinned file custody differs")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise QualificationInstallationError("pinned file exceeds its byte bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or total != before.st_size:
        raise QualificationInstallationError("pinned file changed while read")
    payload = b"".join(chunks)
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise QualificationInstallationError("pinned file differs from its byte digest")
    return payload, QualificationInstalledFileObservation(
        path=str(path),
        content_sha256=digest,
        byte_length=len(payload),
        owner_uid=before.st_uid,
        owner_gid=before.st_gid,
        mode=stat.S_IMODE(before.st_mode),
        device=before.st_dev,
        inode=before.st_ino,
        link_count=before.st_nlink,
    )


class LinuxQualificationInstallationHost:
    """Concrete root/Linux host adapter for the disabled file installer."""

    def __init__(self, request: QualificationInstallationRequestV1) -> None:
        self.request = QualificationInstallationRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        self._journal_root = Path(self.request.journal_root)
        self._active_path, self._request_root = _journal_paths(self.request)

    def assert_linux_root(self) -> None:
        if not sys.platform.startswith("linux"):
            raise QualificationInstallationError("qualification installer requires Linux")
        if os.geteuid() != 0 or os.getegid() != 0:
            raise QualificationInstallationError("qualification installer requires root:root")
        self._assert_directory(
            self._journal_root,
            owner_uid=0,
            owner_gid=0,
            mode=0o700,
            label="installer journal root",
        )

    @staticmethod
    def _assert_directory(
        path: Path,
        *,
        owner_uid: int,
        owner_gid: int,
        mode: int,
        label: str,
    ) -> None:
        try:
            if path.resolve(strict=True) != path:
                raise QualificationInstallationError(f"{label} traverses a symlink")
            observed = os.lstat(path)
        except OSError as exc:
            raise QualificationInstallationError(f"{label} is unavailable") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != owner_uid
            or observed.st_gid != owner_gid
            or stat.S_IMODE(observed.st_mode) != mode
        ):
            raise QualificationInstallationError(f"{label} custody differs")

    @staticmethod
    def _assert_root_parent_chain(path: Path) -> None:
        current = path
        while True:
            try:
                observed = os.lstat(current)
            except OSError as exc:
                raise QualificationInstallationError(
                    "installation target parent is unavailable"
                ) from exc
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != 0
                or observed.st_gid != 0
                or stat.S_IMODE(observed.st_mode) & 0o022
            ):
                raise QualificationInstallationError(
                    "installation target parent chain is not root-controlled"
                )
            if current == Path("/"):
                return
            current = current.parent

    @contextmanager
    def lock(self) -> Iterator[None]:
        lock_path = self._journal_root / "installer.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise QualificationInstallationError("installer lock custody differs")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def verify_pinned_inputs(self) -> None:
        spec = self.request.deployment_spec
        entries = {entry.relative_path: entry for entry in spec.reviewed_code_tree.entries}
        seen: dict[str, tuple[str, int, int, int]] = {}
        pins: list[tuple[str, str, int, int, int]] = [
            (
                self.request.systemctl_executable.path,
                self.request.systemctl_executable.reviewed_sha256,
                self.request.systemctl_executable.expected_owner_uid,
                self.request.systemctl_executable.expected_owner_gid,
                self.request.systemctl_executable.expected_mode,
            )
        ]
        for process in self.request.service_manifest.processes:
            entry = entries[
                str(Path(process.composition_factory_source_path).relative_to(spec.code_root))
            ]
            pins.extend(
                (
                    (
                        process.composition_factory_source_path,
                        entry.reviewed_sha256,
                        entry.expected_owner_uid,
                        entry.expected_owner_gid,
                        entry.expected_mode,
                    ),
                    (
                        process.composition_config_path,
                        process.composition_config_file_sha256,
                        process.composition_config_owner_uid,
                        process.composition_config_owner_gid,
                        process.composition_config_mode,
                    ),
                )
            )
        for path, digest, owner_uid, owner_gid, mode in pins:
            identity = (digest, owner_uid, owner_gid, mode)
            if path in seen and seen[path] != identity:
                raise QualificationInstallationError("pinned installer input has variant custody")
            seen[path] = identity
        for path, (digest, owner_uid, owner_gid, mode) in sorted(seen.items()):
            _fresh_file(
                path,
                expected_sha256=digest,
                expected_owner_uid=owner_uid,
                expected_owner_gid=owner_gid,
                expected_mode=mode,
            )

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        executable = self.request.systemctl_executable
        _fresh_file(
            executable.path,
            expected_sha256=executable.reviewed_sha256,
            expected_owner_uid=executable.expected_owner_uid,
            expected_owner_gid=executable.expected_owner_gid,
            expected_mode=executable.expected_mode,
        )
        try:
            return subprocess.run(
                [executable.path, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise QualificationInstallationError("pinned systemctl invocation failed") from exc

    def observe_systemd(
        self,
        unit_names: tuple[str, ...],
    ) -> QualificationSystemdQuiescenceObservation:
        states: list[QualificationSystemdUnitState] = []
        for unit_name in unit_names:
            result = self._systemctl(
                "show",
                unit_name,
                "--property=LoadState,ActiveState,UnitFileState",
            )
            if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
                raise QualificationInstallationError("systemd unit state could not be observed")
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in values:
                    raise QualificationInstallationError("systemd returned duplicate state")
                values[key] = value
            try:
                load_state = values["LoadState"]
                unit_file_state = values["UnitFileState"]
                if load_state == "not-found" and not unit_file_state:
                    unit_file_state = "not-found"
                states.append(
                    QualificationSystemdUnitState(
                        unit_name=unit_name,
                        load_state=load_state,
                        active_state=values["ActiveState"],
                        unit_file_state=unit_file_state,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise QualificationInstallationError(
                    "systemd unit is active, enabled, failed, or ambiguous"
                ) from exc
        return QualificationSystemdQuiescenceObservation(
            units=tuple(states),
            observed_at=datetime.now(timezone.utc),
        )

    def _prepare_journal_parent(self, path: Path) -> None:
        if path == self._journal_root:
            return
        if path != self._request_root:
            raise QualificationInstallationError("journal write escaped the exact request root")
        try:
            os.mkdir(path, 0o700)
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise QualificationInstallationError(
                "request journal root could not be created"
            ) from exc
        self._assert_directory(
            path,
            owner_uid=0,
            owner_gid=0,
            mode=0o700,
            label="request journal root",
        )

    def read_journal(self, path: Path) -> bytes | None:
        self._prepare_journal_parent(path.parent)
        try:
            return _fresh_file(
                path,
                expected_sha256=None,
                expected_owner_uid=0,
                expected_owner_gid=0,
                expected_mode=0o400,
                preserve_missing=True,
            )[0]
        except FileNotFoundError:
            return None
        except OSError as exc:
            if not path.exists():
                return None
            raise QualificationInstallationError("journal file could not be read") from exc

    def _remove_stale_staging(self, target: Path, *, ordinal: int, target_mode: int) -> None:
        prefix = f".aletheia-{self.request.request_id}-{ordinal}-"
        try:
            entries = tuple(target.parent.iterdir())
        except OSError as exc:
            raise QualificationInstallationError(
                "installation target parent cannot be listed"
            ) from exc
        removed = False
        for candidate in entries:
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(".tmp"):
                continue
            observed = os.lstat(candidate)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != 0
                or observed.st_gid != 0
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) not in {0o600, target_mode}
            ):
                raise QualificationInstallationError("stale installer staging custody differs")
            candidate.unlink()
            removed = True
        if removed:
            descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _publish_exact(
        self,
        *,
        target: Path,
        payload: bytes,
        digest: str,
        owner_uid: int,
        owner_gid: int,
        mode: int,
        ordinal: int,
    ) -> QualificationInstalledFileObservation:
        self._assert_root_parent_chain(target.parent)
        try:
            return _fresh_file(
                target,
                expected_sha256=digest,
                expected_owner_uid=owner_uid,
                expected_owner_gid=owner_gid,
                expected_mode=mode,
            )[1]
        except QualificationInstallationError:
            if os.path.lexists(target):
                raise QualificationInstallationError(
                    "installation target already exists with variant custody"
                )
        self._remove_stale_staging(target, ordinal=ordinal, target_mode=mode)
        staging = target.parent / (
            f".aletheia-{self.request.request_id}-{ordinal}-{secrets.token_hex(8)}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise QualificationInstallationError("installer staging write made no progress")
                offset += written
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if os.path.lexists(target):
                raise QualificationInstallationError(
                    "installation target appeared during atomic publication"
                )
            os.replace(staging, target)
            parent_descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
            raise
        return _fresh_file(
            target,
            expected_sha256=digest,
            expected_owner_uid=owner_uid,
            expected_owner_gid=owner_gid,
            expected_mode=mode,
        )[1]

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        self._prepare_journal_parent(path.parent)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            existing, _observation = _fresh_file(
                path,
                expected_sha256=digest,
                expected_owner_uid=0,
                expected_owner_gid=0,
                expected_mode=0o400,
            )
        except QualificationInstallationError:
            if os.path.lexists(path):
                raise QualificationInstallationError("journal exact retry differs")
        else:
            if existing != payload:
                raise QualificationInstallationError("journal exact retry bytes differ")
            return
        ordinal = int(hashlib.sha256(path.name.encode()).hexdigest()[:8], 16)
        self._publish_exact(
            target=path,
            payload=payload,
            digest=digest,
            owner_uid=0,
            owner_gid=0,
            mode=0o400,
            ordinal=ordinal,
        )

    def publish_artifact(
        self,
        artifact: QualificationInstallationArtifactV1,
        payload: bytes,
    ) -> QualificationInstalledFileObservation:
        if len(payload) != artifact.byte_length or hashlib.sha256(payload).hexdigest() != (
            artifact.content_sha256
        ):
            raise QualificationInstallationError("installation payload differs from its plan")
        return self._publish_exact(
            target=Path(artifact.target_path),
            payload=payload,
            digest=artifact.content_sha256,
            owner_uid=artifact.owner_uid,
            owner_gid=artifact.owner_gid,
            mode=artifact.mode,
            ordinal=artifact.ordinal,
        )

    def observe_artifact(
        self,
        artifact: QualificationInstallationArtifactV1,
    ) -> QualificationInstalledFileObservation:
        return _fresh_file(
            artifact.target_path,
            expected_sha256=artifact.content_sha256,
            expected_owner_uid=artifact.owner_uid,
            expected_owner_gid=artifact.owner_gid,
            expected_mode=artifact.mode,
        )[1]

    def daemon_reload(self) -> str:
        result = self._systemctl("daemon-reload")
        if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
            raise QualificationInstallationError("systemctl daemon-reload failed")
        return canonical_sha256(
            {
                "schema": "aletheia.qualification_daemon_reload_invocation",
                "schema_version": 1,
                "systemctl_executable_sha256": (self.request.systemctl_executable.reviewed_sha256),
                "argv": (self.request.systemctl_executable.path, "daemon-reload"),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )


def load_qualification_installation_request(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> QualificationInstallationRequestV1:
    """Load one canonical request from an out-of-band digest without host mutation."""

    payload, _observation = _fresh_file(path, expected_sha256=expected_file_sha256)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload, object_pairs_hook=unique_object)
        request = QualificationInstallationRequestV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationInstallationError("installation request is invalid") from exc
    if payload != canonical_json_bytes(request):
        raise QualificationInstallationError("installation request is not canonical JSON")
    return request


def _emit(value: ExecutionModel) -> None:
    print(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def run_qualification_installer_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge")
    args = parser.parse_args(argv)
    request = load_qualification_installation_request(
        args.request,
        expected_file_sha256=args.request_sha256,
    )
    if not args.apply:
        _emit(build_qualification_installation_plan(request))
        return 0
    if args.acknowledge != request.opt_in_confirmation:
        parser.error("--apply requires --acknowledge INSTALL_QUALIFICATION_ONLY_DISABLED")
    receipt = install_qualification_service_files(
        request,
        LinuxQualificationInstallationHost(request),
    )
    _emit(receipt)
    return 0


__all__ = [
    "LinuxQualificationInstallationHost",
    "QualificationDaemonReloadReceipt",
    "QualificationInstallationArtifactCompletion",
    "QualificationInstallationArtifactIntent",
    "QualificationInstallationArtifactV1",
    "QualificationInstallationError",
    "QualificationInstallationHostPort",
    "QualificationInstallationPlanV1",
    "QualificationInstallationReceiptV1",
    "QualificationInstallationRequestV1",
    "QualificationInstalledFileObservation",
    "QualificationSystemdQuiescenceObservation",
    "QualificationSystemdUnitState",
    "build_qualification_installation_plan",
    "install_qualification_service_files",
    "load_qualification_installation_request",
    "run_qualification_installer_cli",
]
