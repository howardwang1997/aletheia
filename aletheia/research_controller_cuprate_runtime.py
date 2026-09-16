"""Guarded-loader factory for the read-only cuprate diagnostic RPC service."""

from __future__ import annotations


def build_cuprate_diagnostic_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``RUN_CUPRATE_DIAGNOSTIC`` with public computation only."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from pathlib import Path
    from typing import Literal

    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        CuprateDiagnosticRPCPayload,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    class CuprateDiagnosticRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.cuprate_diagnostic_rpc_service_config"] = (
            "aletheia.cuprate_diagnostic_rpc_service_config"
        )
        schema_version: Literal[1] = 1
        controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
        controller_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        worker_process_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
        service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
        service_pin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        dataset_csv_path: str
        dataset_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        source_implementation_source_path: str
        source_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        private_domain_signing_key_loaded: Literal[False] = False
        database_access_allowed: Literal[False] = False
        execution_mutation_allowed: Literal[False] = False
        validation_allowed: Literal[False] = False
        direct_observation_admission_allowed: Literal[False] = False
        direct_kernel_mutation_allowed: Literal[False] = False
        network_egress_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _paths_are_canonical(self):
            dataset = Path(self.dataset_csv_path)
            source = Path(self.source_implementation_source_path)
            for label, path in (("dataset", dataset), ("source", source)):
                value = str(path)
                if (
                    not value
                    or "\x00" in value
                    or not path.is_absolute()
                    or value != os.path.normpath(value)
                    or value == "/"
                ):
                    raise ValueError(
                        f"cuprate diagnostic {label} path must be canonical and absolute"
                    )
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate cuprate diagnostic RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(path: Path, *, expected_sha256: str, label: str) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError(f"{label} traverses a symlink")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or not 0 < before.st_size <= 16 * 1024 * 1024
                ):
                    raise ValueError(f"{label} has unsafe file custody")
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError(f"{label} ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if os.read(descriptor, 1) or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(f"{label} changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(f"{label} differs from its byte pin")
        return payload

    def staged_dataset_custody(path: Path) -> None:
        """The staged CSV must be a read-only regular file outside code custody."""

        info = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o222
        ):
            raise ValueError("cuprate diagnostic dataset path has unsafe custody")

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = CuprateDiagnosticRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("cuprate diagnostic RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("cuprate diagnostic RPC config is not canonical JSON")

    pin = deployment.service_pin
    if (
        pin.operations != (ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC,)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or pin.service_principal_id == config.worker_process_principal_id
    ):
        raise ValueError("cuprate diagnostic RPC config differs from deployment")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.source_implementation_source_path)
    from aletheia.execution.cuprate import service as service_module

    expected_module_path = Path(service_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("cuprate diagnostic implementation escaped reviewed source") from exc
    if implementation_path != expected_module_path:
        raise ValueError("cuprate diagnostic implementation resolved another module")

    dataset_path = Path(config.dataset_csv_path)
    if dataset_path == reviewed_root or reviewed_root in dataset_path.parents:
        raise ValueError("cuprate diagnostic dataset lives inside reviewed code custody")
    staged_dataset_custody(dataset_path)

    before = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.source_implementation_source_sha256,
        label="cuprate diagnostic implementation",
    )
    service = service_module.CuprateDiagnosticService(dataset_csv_path=dataset_path)

    def run_cuprate_diagnostic(payload):
        if type(payload) is not CuprateDiagnosticRPCPayload:
            raise TypeError("cuprate diagnostic RPC handler received another payload type")
        if payload.expected_content_sha256 != config.dataset_content_sha256:
            raise ValueError("cuprate diagnostic request targets another dataset content")
        return service.run_cuprate_diagnostic(payload)

    after = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.source_implementation_source_sha256,
        label="cuprate diagnostic implementation",
    )
    if before != after:
        raise ValueError("cuprate diagnostic implementation changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC,
                handler=run_cuprate_diagnostic,
            ),
        ),
    )


__all__ = ["build_cuprate_diagnostic_rpc_service"]
