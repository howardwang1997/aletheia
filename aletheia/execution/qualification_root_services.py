"""Production composition for the three privileged qualification-only services.

The guarded runner already freezes each config file and factory source before invoking these
builders.  This module adds the semantic binding: canonical config bytes name the exact process
identity and one closed root-service deployment.  None of the services loads database credentials,
private signing keys, or scientific-admission authority.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import Field, model_validator

from aletheia.execution.oci_deployment import (
    DeploymentPinnedOCIPolicy,
    DurableDeadlineWatchdogService,
    LoopbackOutputQuotaProvisioningService,
    LoopbackQuotaProvisionerDeploymentPin,
    SharedOutputWorkspaceDeploymentPin,
    SharedOutputWorkspaceService,
    SystemdWatchdogDeploymentPin,
)
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceHandlerSet,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"


class QualificationRootServiceCompositionError(RuntimeError):
    """A root-service config, process binding, or handler invocation failed closed."""


class QualificationWorkspaceServiceConfigV1(ExecutionModel):
    """Canonical factory config for the workspace one-shot."""

    schema_name: Literal["aletheia.qualification_workspace_service_config"] = (
        "aletheia.qualification_workspace_service_config"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    process_config_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    workspace_deployment: SharedOutputWorkspaceDeploymentPin
    private_signing_keys_loaded: Literal[False] = False
    database_credentials_loaded: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _workspace_is_scoped(self) -> "QualificationWorkspaceServiceConfigV1":
        if self.workspace_deployment.deployment_id != f"{self.deployment_id}:workspace":
            raise ValueError("workspace service config belongs to another deployment")
        return self


class QualificationQuotaServiceConfigV2(ExecutionModel):
    """Canonical factory config for the loopback quota daemon."""

    schema_name: Literal["aletheia.qualification_quota_service_config"] = (
        "aletheia.qualification_quota_service_config"
    )
    schema_version: Literal[2] = 2
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    process_config_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_policy: DeploymentPinnedOCIPolicy
    runtime_journal_root: str
    quota_deployment: LoopbackQuotaProvisionerDeploymentPin
    private_signing_keys_loaded: Literal[False] = False
    database_credentials_loaded: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _quota_is_scoped(self) -> "QualificationQuotaServiceConfigV2":
        if (
            self.quota_deployment.deployment_id != f"{self.deployment_id}:quota"
            or self.oci_policy.workload_uid != self.quota_deployment.allowed_client_uid
            or self.oci_policy.workload_gid != self.quota_deployment.allowed_client_gid
        ):
            raise ValueError("quota service config belongs to another deployment")
        candidate = Path(self.runtime_journal_root)
        if (
            not candidate.is_absolute()
            or str(candidate) != os.path.normpath(self.runtime_journal_root)
            or self.runtime_journal_root == "/"
        ):
            raise ValueError("quota runtime journal root must be canonical and absolute")
        return self


class QualificationWatchdogServiceConfigV1(ExecutionModel):
    """Canonical factory config for the independent deadline watchdog."""

    schema_name: Literal["aletheia.qualification_watchdog_service_config"] = (
        "aletheia.qualification_watchdog_service_config"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    process_config_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_policy: DeploymentPinnedOCIPolicy
    watchdog_deployment: SystemdWatchdogDeploymentPin
    private_signing_keys_loaded: Literal[False] = False
    database_credentials_loaded: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _watchdog_is_scoped(self) -> "QualificationWatchdogServiceConfigV1":
        if (
            self.watchdog_deployment.deployment_id != f"{self.deployment_id}:watchdog"
            or self.watchdog_deployment.policy_sha256 != self.oci_policy.policy_sha256
        ):
            raise ValueError("watchdog service config belongs to another deployment or policy")
        return self


_ConfigT = TypeVar(
    "_ConfigT",
    QualificationWorkspaceServiceConfigV1,
    QualificationQuotaServiceConfigV2,
    QualificationWatchdogServiceConfigV1,
)


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate qualification root-service config keys: {duplicates}")
    return dict(pairs)


def _load_config(configuration_bytes: bytes, model: type[_ConfigT]) -> _ConfigT:
    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=_unique_object)
        config = model.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationRootServiceCompositionError(
            "qualification root-service config is invalid"
        ) from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise QualificationRootServiceCompositionError(
            "qualification root-service config is not canonical JSON"
        )
    return config


def _bind_process(
    deployment: QualificationServiceProcessDeploymentV1,
    *,
    role: QualificationServiceRole,
    config_deployment_id: str,
    process_config_binding_sha256: str,
) -> QualificationServiceProcessDeploymentV1:
    try:
        process = QualificationServiceProcessDeploymentV1.model_validate(
            deployment.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationRootServiceCompositionError(
            "qualification process deployment is invalid"
        ) from exc
    if (
        process.role is not role
        or process.deployment_id != config_deployment_id
        or qualification_service_process_config_binding_sha256(process)
        != process_config_binding_sha256
        or (process.process_uid, process.process_gid) != (0, 0)
        or process.worker_poll_milliseconds is not None
    ):
        raise QualificationRootServiceCompositionError(
            "qualification root-service config differs from its process deployment"
        )
    return process


def _handler_set(
    *,
    process: QualificationServiceProcessDeploymentV1,
    operation,
) -> QualificationServiceHandlerSet:
    def handler(*, poll_milliseconds: int | None) -> None:
        if poll_milliseconds is not None:
            raise QualificationRootServiceCompositionError(
                "root-service handler received a worker poll interval"
            )
        operation()

    return QualificationServiceHandlerSet(
        role=process.role,
        operation=process.operation,
        handler=handler,
    )


def compose_workspace_service(
    *,
    deployment: QualificationServiceProcessDeploymentV1,
    configuration_bytes: bytes,
) -> QualificationServiceHandlerSet:
    """Compose the exact crash-recoverable workspace one-shot."""

    config = _load_config(configuration_bytes, QualificationWorkspaceServiceConfigV1)
    process = _bind_process(
        deployment,
        role=QualificationServiceRole.WORKSPACE,
        config_deployment_id=config.deployment_id,
        process_config_binding_sha256=config.process_config_binding_sha256,
    )
    service = SharedOutputWorkspaceService(config.workspace_deployment)
    return _handler_set(process=process, operation=service.ensure_shared_workspace)


def compose_quota_service(
    *,
    deployment: QualificationServiceProcessDeploymentV1,
    configuration_bytes: bytes,
) -> QualificationServiceHandlerSet:
    """Compose the existing root-only loopback quota daemon."""

    config = _load_config(configuration_bytes, QualificationQuotaServiceConfigV2)
    process = _bind_process(
        deployment,
        role=QualificationServiceRole.QUOTA,
        config_deployment_id=config.deployment_id,
        process_config_binding_sha256=config.process_config_binding_sha256,
    )
    service = LoopbackOutputQuotaProvisioningService(
        config.quota_deployment,
        verification_policy=config.oci_policy,
        runtime_journal_root=Path(config.runtime_journal_root),
    )
    return _handler_set(process=process, operation=service.serve_forever)


def compose_watchdog_service(
    *,
    deployment: QualificationServiceProcessDeploymentV1,
    configuration_bytes: bytes,
) -> QualificationServiceHandlerSet:
    """Compose the existing independently supervised deadline watchdog."""

    config = _load_config(configuration_bytes, QualificationWatchdogServiceConfigV1)
    process = _bind_process(
        deployment,
        role=QualificationServiceRole.WATCHDOG,
        config_deployment_id=config.deployment_id,
        process_config_binding_sha256=config.process_config_binding_sha256,
    )
    service = DurableDeadlineWatchdogService(
        policy=config.oci_policy,
        deployment=config.watchdog_deployment,
    )
    return _handler_set(process=process, operation=service.serve_forever)


__all__ = [
    "QualificationQuotaServiceConfigV2",
    "QualificationRootServiceCompositionError",
    "QualificationWatchdogServiceConfigV1",
    "QualificationWorkspaceServiceConfigV1",
    "compose_quota_service",
    "compose_watchdog_service",
    "compose_workspace_service",
]
