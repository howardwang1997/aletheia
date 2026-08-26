"""Guarded one-role-per-process runtime for qualification-only execution services.

The portable PR-4 deployment renderer names five Python runners.  This module gives those
entrypoints one closed process boundary without installing files, starting systemd, or creating
scientific authority.  An out-of-band SHA-256 pins the canonical deployment manifest before any
composition factory is loaded; the factory source and its role-specific configuration are then
fresh-read and pinned independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256
from aletheia.migration.dynamic_loader import load_guarded_source_bytes

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MODULE_PATTERN = r"^aletheia(?:[.][A-Za-z_][A-Za-z0-9_]*)+$"
_ATTRIBUTE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_MAX_PINNED_FILE_BYTES = 16 * 1024 * 1024


class QualificationServiceProcessError(RuntimeError):
    """A deployment, filesystem, identity, factory, or handler invariant failed closed."""


class QualificationServiceRole(str, Enum):
    """Closed set of processes rendered by ``QualificationDeploymentSpecV1``."""

    WORKSPACE = "workspace"
    QUOTA = "quota"
    WATCHDOG = "watchdog"
    NODE = "node"
    OUTBOX = "outbox"


_ROLE_ORDER = (
    QualificationServiceRole.WORKSPACE,
    QualificationServiceRole.QUOTA,
    QualificationServiceRole.WATCHDOG,
    QualificationServiceRole.NODE,
    QualificationServiceRole.OUTBOX,
)
_ROLE_OPERATION: dict[QualificationServiceRole, str] = {
    QualificationServiceRole.WORKSPACE: "ensure-shared-workspace",
    QualificationServiceRole.QUOTA: "serve",
    QualificationServiceRole.WATCHDOG: "serve",
    QualificationServiceRole.NODE: "run",
    QualificationServiceRole.OUTBOX: "run",
}
_ROOT_SERVICE_ROLES = frozenset(
    {
        QualificationServiceRole.WORKSPACE,
        QualificationServiceRole.QUOTA,
        QualificationServiceRole.WATCHDOG,
    }
)


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


class QualificationServiceProcessDeploymentV1(ExecutionModel):
    """Exact source, configuration, identity, and operation for one service process."""

    schema_name: Literal["aletheia.qualification_service_process_deployment"] = (
        "aletheia.qualification_service_process_deployment"
    )
    schema_version: Literal[1] = 1
    process_id: str | None = Field(default=None, pattern=r"^qsp_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    role: QualificationServiceRole
    operation: Literal["ensure-shared-workspace", "serve", "run"]
    process_uid: int = Field(ge=0, le=2**31 - 1)
    process_gid: int = Field(ge=0, le=2**31 - 1)
    worker_poll_milliseconds: int | None = Field(default=None, ge=50, le=60_000)
    reviewed_code_root: str
    composition_factory_module: str = Field(pattern=_MODULE_PATTERN)
    composition_factory_attribute: str = Field(pattern=_ATTRIBUTE_PATTERN)
    composition_factory_source_path: str
    composition_factory_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    composition_factory_owner_uid: int = Field(ge=0, le=2**31 - 1)
    composition_factory_owner_gid: int = Field(ge=0, le=2**31 - 1)
    composition_factory_mode: int = Field(ge=0, le=0o777)
    composition_config_path: str
    composition_config_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    composition_config_owner_uid: int = Field(ge=0, le=2**31 - 1)
    composition_config_owner_gid: int = Field(ge=0, le=2**31 - 1)
    composition_config_mode: int = Field(ge=0, le=0o777)
    one_service_per_process: Literal[True] = True
    automatic_installation: Literal[False] = False
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_is_closed(self) -> "QualificationServiceProcessDeploymentV1":
        code_root = _canonical_absolute_path(self.reviewed_code_root, label="reviewed code root")
        source = _canonical_absolute_path(
            self.composition_factory_source_path,
            label="composition factory source",
        )
        config = _canonical_absolute_path(
            self.composition_config_path,
            label="composition config",
        )
        try:
            source_relative = source.relative_to(code_root)
        except ValueError as exc:
            raise ValueError("composition factory escaped the reviewed code root") from exc
        expected_relative = Path(*self.composition_factory_module.split(".")).with_suffix(".py")
        if source_relative != expected_relative:
            raise ValueError("composition factory module does not match its reviewed source path")
        if config == source or config == code_root or code_root in config.parents:
            raise ValueError("composition config must be separate from reviewed source")
        if self.operation != _ROLE_OPERATION[self.role]:
            raise ValueError("qualification service operation differs from its exact role")
        if self.role in _ROOT_SERVICE_ROLES:
            if (self.process_uid, self.process_gid) != (0, 0):
                raise ValueError("privileged qualification services must run as root:root")
        elif self.process_uid == 0 or self.process_gid == 0:
            raise ValueError("node and outbox services must use non-root primary identities")
        if (self.role is QualificationServiceRole.NODE) != (
            self.worker_poll_milliseconds is not None
        ):
            raise ValueError("only the node service may bind a worker poll interval")
        for label, mode in (
            ("composition factory", self.composition_factory_mode),
            ("composition config", self.composition_config_mode),
        ):
            if mode & 0o222 or not mode & 0o444:
                raise ValueError(f"{label} must be pinned read-only and readable")
        expected_id = f"qsp_{self.identity_sha256[:32]}"
        if self.process_id is not None and self.process_id != expected_id:
            raise ValueError("qualification service process id is not derived")
        object.__setattr__(self, "process_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"process_id"}))


class QualificationServiceDeploymentManifestV1(ExecutionModel):
    """Canonical five-process manifest consumed by every rendered runner."""

    schema_name: Literal["aletheia.qualification_service_deployment_manifest"] = (
        "aletheia.qualification_service_deployment_manifest"
    )
    schema_version: Literal[1] = 1
    manifest_id: str | None = Field(default=None, pattern=r"^qsm_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    processes: tuple[QualificationServiceProcessDeploymentV1, ...] = Field(
        min_length=5,
        max_length=5,
    )
    prepared_at: AwareDatetime
    one_service_per_process: Literal[True] = True
    automatic_installation: Literal[False] = False
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_exhaustive(self) -> "QualificationServiceDeploymentManifestV1":
        if tuple(item.role for item in self.processes) != _ROLE_ORDER:
            raise ValueError("qualification service manifest must contain all roles canonically")
        if any(item.deployment_id != self.deployment_id for item in self.processes):
            raise ValueError("qualification service process belongs to another deployment")
        if len({item.process_id for item in self.processes}) != len(self.processes):
            raise ValueError("qualification service process identities must be unique")
        if len(
            {
                (item.composition_factory_module, item.composition_factory_attribute)
                for item in self.processes
            }
        ) != len(self.processes):
            raise ValueError("qualification service factory identities must be unique")
        if len({item.composition_config_path for item in self.processes}) != len(self.processes):
            raise ValueError("qualification service configurations must be role-specific")
        node = self.process_for(QualificationServiceRole.NODE)
        outbox = self.process_for(QualificationServiceRole.OUTBOX)
        if node.process_uid == outbox.process_uid or node.process_gid == outbox.process_gid:
            raise ValueError("node and outbox process identities must be distinct")
        expected_id = f"qsm_{self.identity_sha256[:32]}"
        if self.manifest_id is not None and self.manifest_id != expected_id:
            raise ValueError("qualification service manifest id is not derived")
        object.__setattr__(self, "manifest_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"manifest_id"}))

    @property
    def file_sha256(self) -> str:
        """Digest of the only accepted canonical manifest bytes."""

        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()

    def process_for(
        self,
        role: QualificationServiceRole,
    ) -> QualificationServiceProcessDeploymentV1:
        return next(item for item in self.processes if item.role is role)


class QualificationServiceStartupReceipt(ExecutionModel):
    """Operational evidence that one exact process passed its startup boundary."""

    schema_name: Literal["aletheia.qualification_service_startup_receipt"] = (
        "aletheia.qualification_service_startup_receipt"
    )
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^qsm_[0-9a-f]{32}$")
    manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_id: str = Field(pattern=r"^qsp_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    role: QualificationServiceRole
    operation: Literal["ensure-shared-workspace", "serve", "run"]
    process_uid: int = Field(ge=0, le=2**31 - 1)
    process_gid: int = Field(ge=0, le=2**31 - 1)
    started_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationServiceExitReceipt(ExecutionModel):
    """Operational evidence that a handler returned normally, never a campaign verdict."""

    schema_name: Literal["aletheia.qualification_service_exit_receipt"] = (
        "aletheia.qualification_service_exit_receipt"
    )
    schema_version: Literal[1] = 1
    startup_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_id: str = Field(pattern=r"^qsp_[0-9a-f]{32}$")
    role: QualificationServiceRole
    operation: Literal["ensure-shared-workspace", "serve", "run"]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    disposition: Literal["returned"] = "returned"
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @model_validator(mode="after")
    def _times_are_ordered(self) -> "QualificationServiceExitReceipt":
        if self.finished_at < self.started_at:
            raise ValueError("qualification service finished before it started")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationServiceOperationHandler(Protocol):
    def __call__(self, *, poll_milliseconds: int | None) -> None: ...


class QualificationServiceHandlerSet:
    """One exact callable for one role and operation; it grants no other dispatch surface."""

    def __init__(
        self,
        *,
        role: QualificationServiceRole,
        operation: str,
        handler: QualificationServiceOperationHandler,
    ) -> None:
        if operation != _ROLE_OPERATION[role]:
            raise ValueError("qualification handler operation differs from its role")
        if not callable(handler):
            raise TypeError("qualification service handler is not callable")
        self.role = role
        self.operation = operation
        self.handler = handler


def _fresh_pinned_bytes(
    path_value: str | Path,
    expected_sha256: str,
    *,
    label: str,
    expected_owner_uid: int | None = None,
    expected_owner_gid: int | None = None,
    expected_mode: int | None = None,
    maximum_bytes: int = _MAX_PINNED_FILE_BYTES,
) -> bytes:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path:
            raise QualificationServiceProcessError(f"{label} path traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise QualificationServiceProcessError(f"{label} cannot be opened safely") from exc
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
            raise QualificationServiceProcessError(f"{label} custody differs from its pin")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise QualificationServiceProcessError(f"{label} exceeds its byte bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or total != before.st_size:
        raise QualificationServiceProcessError(f"{label} changed while read")
    payload = b"".join(chunks)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise QualificationServiceProcessError(f"{label} differs from its byte pin")
    return payload


def _strict_json(payload: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        duplicates = [
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        ]
        if duplicates:
            raise ValueError(f"duplicate JSON keys: {duplicates}")
        return dict(pairs)

    def invalid_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        payload,
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
    )


def load_qualification_service_deployment_manifest(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> QualificationServiceDeploymentManifestV1:
    """Fresh-read one canonical manifest from an out-of-band SHA-256 pin."""

    if not isinstance(expected_file_sha256, str) or not re.fullmatch(
        _SHA256_PATTERN, expected_file_sha256
    ):
        raise QualificationServiceProcessError("qualification manifest digest is not SHA-256")
    payload = _fresh_pinned_bytes(
        path,
        expected_file_sha256,
        label="qualification service deployment manifest",
    )
    try:
        manifest = QualificationServiceDeploymentManifestV1.model_validate(_strict_json(payload))
    except (TypeError, ValueError) as exc:
        raise QualificationServiceProcessError(
            "qualification service deployment manifest is invalid"
        ) from exc
    if payload != canonical_json_bytes(manifest):
        raise QualificationServiceProcessError(
            "qualification service deployment manifest is not canonical JSON"
        )
    return manifest


def _load_handler_set(
    deployment: QualificationServiceProcessDeploymentV1,
) -> QualificationServiceHandlerSet:
    code_root = Path(deployment.reviewed_code_root)
    source_path = Path(deployment.composition_factory_source_path)
    try:
        if (
            code_root.resolve(strict=True) != code_root
            or source_path.resolve(strict=True) != source_path
        ):
            raise QualificationServiceProcessError(
                "qualification composition source traverses a symlink"
            )
        source_path.relative_to(code_root)
    except (OSError, ValueError) as exc:
        raise QualificationServiceProcessError(
            "qualification composition factory escaped reviewed source"
        ) from exc
    source_before = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="qualification composition factory",
        expected_owner_uid=deployment.composition_factory_owner_uid,
        expected_owner_gid=deployment.composition_factory_owner_gid,
        expected_mode=deployment.composition_factory_mode,
    )
    configuration = _fresh_pinned_bytes(
        deployment.composition_config_path,
        deployment.composition_config_file_sha256,
        label="qualification composition config",
        expected_owner_uid=deployment.composition_config_owner_uid,
        expected_owner_gid=deployment.composition_config_owner_gid,
        expected_mode=deployment.composition_config_mode,
    )
    try:
        module = load_guarded_source_bytes(
            deployment.composition_factory_module,
            source_path,
            source_before,
        )
        factory = vars(module).get(deployment.composition_factory_attribute)
    except Exception as exc:
        raise QualificationServiceProcessError(
            "qualification composition factory could not load"
        ) from exc
    source_after = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="qualification composition factory",
        expected_owner_uid=deployment.composition_factory_owner_uid,
        expected_owner_gid=deployment.composition_factory_owner_gid,
        expected_mode=deployment.composition_factory_mode,
    )
    if source_before != source_after or not callable(factory):
        raise QualificationServiceProcessError(
            "qualification composition factory changed or is not callable"
        )
    try:
        handlers = factory(deployment=deployment, configuration_bytes=configuration)
    except Exception as exc:
        raise QualificationServiceProcessError("qualification composition factory failed") from exc
    if type(handlers) is not QualificationServiceHandlerSet:
        raise QualificationServiceProcessError(
            "qualification factory returned another handler container"
        )
    if handlers.role is not deployment.role or handlers.operation != deployment.operation:
        raise QualificationServiceProcessError(
            "qualification factory handler differs from its deployment"
        )
    return handlers


class QualificationServiceRuntime:
    """Run one exact service operation after live Linux identity and source verification."""

    def __init__(
        self,
        *,
        manifest: QualificationServiceDeploymentManifestV1,
        deployment: QualificationServiceProcessDeploymentV1,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manifest = manifest
        self.deployment = deployment
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._handlers: QualificationServiceHandlerSet | None = None
        self._startup_receipt: QualificationServiceStartupReceipt | None = None

    def start(self) -> QualificationServiceStartupReceipt:
        if self._startup_receipt is not None:
            return self._startup_receipt
        if not sys.platform.startswith("linux"):
            raise QualificationServiceProcessError("qualification service requires Linux")
        if (
            os.geteuid() != self.deployment.process_uid
            or os.getegid() != self.deployment.process_gid
        ):
            raise QualificationServiceProcessError(
                "qualification service UID/GID differ from deployment"
            )
        self._handlers = _load_handler_set(self.deployment)
        self._startup_receipt = QualificationServiceStartupReceipt(
            manifest_id=self.manifest.manifest_id,
            manifest_file_sha256=self.manifest.file_sha256,
            process_id=self.deployment.process_id,
            deployment_id=self.deployment.deployment_id,
            role=self.deployment.role,
            operation=self.deployment.operation,
            process_uid=self.deployment.process_uid,
            process_gid=self.deployment.process_gid,
            started_at=self._clock(),
        )
        return self._startup_receipt

    def run(self) -> QualificationServiceExitReceipt:
        startup = self.start()
        assert self._handlers is not None
        try:
            result = self._handlers.handler(
                poll_milliseconds=self.deployment.worker_poll_milliseconds
            )
        except Exception as exc:
            raise QualificationServiceProcessError("qualification service handler failed") from exc
        if result is not None:
            raise QualificationServiceProcessError(
                "qualification service handler returned an unauthorized value"
            )
        return QualificationServiceExitReceipt(
            startup_receipt_sha256=startup.receipt_sha256,
            process_id=self.deployment.process_id,
            role=self.deployment.role,
            operation=self.deployment.operation,
            started_at=startup.started_at,
            finished_at=self._clock(),
        )


def build_qualification_service_runtime(
    manifest: QualificationServiceDeploymentManifestV1,
    *,
    role: QualificationServiceRole,
    clock: Callable[[], datetime] | None = None,
) -> QualificationServiceRuntime:
    """Build an unstarted runtime for exactly one manifest role."""

    frozen = QualificationServiceDeploymentManifestV1.model_validate(
        manifest.model_dump(mode="python")
    )
    return QualificationServiceRuntime(
        manifest=frozen,
        deployment=frozen.process_for(role),
        clock=clock,
    )


def _emit_receipt(receipt: ExecutionModel) -> None:
    print(
        json.dumps(receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def run_qualification_service_cli(
    *,
    role: QualificationServiceRole,
    argv: Sequence[str] | None = None,
) -> int:
    """Parse the frozen systemd interface and run only the entrypoint's compiled-in role."""

    parser = argparse.ArgumentParser(
        description=f"Run the qualification-only {role.value} service process."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("operation")
    parser.add_argument("--poll-milliseconds", type=int)
    args = parser.parse_args(argv)
    expected_operation = _ROLE_OPERATION[role]
    if args.operation != expected_operation:
        parser.error(f"{role.value} runner requires operation {expected_operation!r}")
    manifest = load_qualification_service_deployment_manifest(
        args.manifest,
        expected_file_sha256=args.manifest_sha256,
    )
    deployment = manifest.process_for(role)
    if args.poll_milliseconds != deployment.worker_poll_milliseconds:
        parser.error("poll interval differs from the exact role deployment")
    runtime = build_qualification_service_runtime(manifest, role=role)
    _emit_receipt(runtime.start())
    _emit_receipt(runtime.run())
    return 0


__all__ = [
    "QualificationServiceDeploymentManifestV1",
    "QualificationServiceExitReceipt",
    "QualificationServiceHandlerSet",
    "QualificationServiceOperationHandler",
    "QualificationServiceProcessDeploymentV1",
    "QualificationServiceProcessError",
    "QualificationServiceRole",
    "QualificationServiceRuntime",
    "QualificationServiceStartupReceipt",
    "build_qualification_service_runtime",
    "load_qualification_service_deployment_manifest",
    "run_qualification_service_cli",
]
