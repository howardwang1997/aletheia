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
from pathlib import Path

from aletheia.execution.qualification_service_contracts import (
    QualificationServiceDeploymentManifestV1,
    QualificationServiceExitReceipt,
    QualificationServiceHandlerSet,
    QualificationServiceOperationHandler,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    QualificationServiceStartupReceipt,
    qualification_service_process_config_binding_sha256,
    qualification_service_role_operation,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes
from aletheia.migration.dynamic_loader import load_guarded_source_bytes

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_PINNED_FILE_BYTES = 16 * 1024 * 1024


class QualificationServiceProcessError(RuntimeError):
    """A deployment, filesystem, identity, factory, or handler invariant failed closed."""


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
    expected_operation = qualification_service_role_operation(role)
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
    "qualification_service_process_config_binding_sha256",
    "run_qualification_service_cli",
]
