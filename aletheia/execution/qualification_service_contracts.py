"""Pure contracts for the guarded qualification service process boundary."""

from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MODULE_PATTERN = r"^aletheia(?:[.][A-Za-z_][A-Za-z0-9_]*)+$"
_ATTRIBUTE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


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


def qualification_service_role_operation(role: QualificationServiceRole) -> str:
    """Return the sole operation admitted for one service role."""

    return _ROLE_OPERATION[role]


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


def qualification_service_process_config_binding_sha256(
    deployment: QualificationServiceProcessDeploymentV1,
) -> str:
    """Hash the process fields a config may bind without a config-hash self-reference."""

    process = QualificationServiceProcessDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    return canonical_sha256(
        {
            "schema": "aletheia.qualification_service_process_config_binding",
            "schema_version": 1,
            "process": process.model_dump(
                mode="json",
                exclude={"process_id", "composition_config_file_sha256"},
            ),
        }
    )


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


__all__ = [
    "QualificationServiceDeploymentManifestV1",
    "QualificationServiceExitReceipt",
    "QualificationServiceHandlerSet",
    "QualificationServiceOperationHandler",
    "QualificationServiceProcessDeploymentV1",
    "QualificationServiceRole",
    "QualificationServiceStartupReceipt",
    "qualification_service_process_config_binding_sha256",
    "qualification_service_role_operation",
]
