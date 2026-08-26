"""One-service-per-process Linux runtime for controller worker RPC authorities.

This outer composition boundary may load service-owned domain keys through a byte-pinned factory.
It is deliberately separate from the keyless controller worker runtime.  The transport receipt key
is loaded from one exact 0400 file, the Unix socket and both peers are deployment-pinned, and a
service process can expose only the operations listed in its immutable worker-side pin.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import struct
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.migration.dynamic_loader import load_guarded_source_bytes
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCRequest,
    ControllerWorkerRPCServicePin,
)
from aletheia.research_controller.external_rpc_server import (
    ControllerWorkerRPCHandlerSet,
    ControllerWorkerRPCRequestRejected,
    ControllerWorkerRPCServerError,
    ControllerWorkerRPCService,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_MODULE_PATTERN = r"^aletheia(?:[.][A-Za-z_][A-Za-z0-9_]*)+$"
_ATTRIBUTE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_MAX_PINNED_FILE_BYTES = 16 * 1024 * 1024


class ControllerWorkerRPCProcessError(RuntimeError):
    """A deployment, filesystem, socket, peer, factory, or service invariant failed closed."""


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


class ControllerWorkerRPCServerDeployment(ControllerModel):
    """Exact OS, key, source, and client binding for one external RPC service process."""

    schema_name: Literal["aletheia.controller_worker_rpc_server_deployment"] = (
        "aletheia.controller_worker_rpc_server_deployment"
    )
    schema_version: Literal[1] = 1
    runtime_id: str | None = Field(default=None, pattern=r"^rpcsrv_[0-9a-f]{32}$")
    service_pin: ControllerWorkerRPCServicePin
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    worker_process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    worker_peer_uid: int = Field(ge=1, le=2**31 - 1)
    worker_peer_gid: int = Field(ge=1, le=2**31 - 1)
    process_uid: int = Field(ge=1, le=2**31 - 1)
    process_gid: int = Field(ge=1, le=2**31 - 1)
    socket_parent_path: str
    socket_parent_owner_uid: int = Field(ge=0, le=2**31 - 1)
    socket_parent_owner_gid: int = Field(ge=0, le=2**31 - 1)
    socket_parent_mode: int = Field(ge=0, le=0o777)
    socket_parent_device_id: int = Field(ge=0)
    socket_parent_inode: int = Field(ge=1)
    receipt_private_key_path: str
    receipt_private_key_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewed_code_root: str
    composition_factory_module: str = Field(pattern=_MODULE_PATTERN)
    composition_factory_attribute: str = Field(pattern=_ATTRIBUTE_PATTERN)
    composition_factory_source_path: str
    composition_factory_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    composition_config_path: str
    composition_config_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    accept_timeout_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    one_service_per_process: Literal[True] = True
    linux_peer_credentials_required: Literal[True] = True
    receipt_key_mode: Literal[0o400] = 0o400
    private_key_loaded_in_worker: Literal[False] = False
    transport_receipt_grants_scientific_authority: Literal[False] = False
    unpinned_dynamic_loading_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_is_closed(self) -> "ControllerWorkerRPCServerDeployment":
        socket_path = _canonical_absolute_path(
            self.service_pin.socket_path, label="RPC socket path"
        )
        parent = _canonical_absolute_path(self.socket_parent_path, label="RPC socket parent")
        key = _canonical_absolute_path(
            self.receipt_private_key_path, label="RPC receipt private key"
        )
        code_root = _canonical_absolute_path(self.reviewed_code_root, label="reviewed code root")
        factory = _canonical_absolute_path(
            self.composition_factory_source_path, label="composition factory source"
        )
        config = _canonical_absolute_path(self.composition_config_path, label="composition config")
        if socket_path.parent != parent:
            raise ValueError("RPC socket does not belong to its pinned parent")
        try:
            factory.relative_to(code_root)
        except ValueError as exc:
            raise ValueError("RPC composition factory escaped the reviewed code root") from exc
        try:
            key.relative_to(code_root)
        except ValueError:
            pass
        else:
            raise ValueError("RPC receipt private key cannot live inside reviewed source")
        if len({socket_path, parent, key, factory, config}) != 5:
            raise ValueError("RPC deployment paths must be distinct")
        if (
            self.process_uid != self.service_pin.peer_uid
            or self.process_gid != self.service_pin.peer_gid
            or self.process_uid != self.service_pin.socket_owner_uid
            or self.process_gid != self.service_pin.socket_group_gid
        ):
            raise ValueError("RPC process and socket identity differ from the worker service pin")
        if self.worker_peer_uid == self.process_uid:
            raise ValueError("RPC worker and service must use distinct UIDs")
        if (
            self.service_pin.socket_mode != 0o660
            or self.worker_peer_gid != self.service_pin.socket_group_gid
        ):
            raise ValueError("RPC worker requires the exact shared socket group and 0660 mode")
        if self.worker_process_principal_id == self.service_pin.service_principal_id:
            raise ValueError("RPC worker and service principals must differ")
        if (
            self.socket_parent_owner_uid != self.process_uid
            or self.socket_parent_owner_gid != self.process_gid
            or not self.socket_parent_mode & 0o010
            or self.socket_parent_mode & 0o007
        ):
            raise ValueError(
                "RPC socket parent must be service-owned, shared-group traversable, and private"
            )
        if (
            self.prepared_at < self.service_pin.valid_from
            or self.prepared_at >= self.service_pin.expires_at
        ):
            raise ValueError("RPC deployment is outside the receipt-key validity interval")
        expected = f"rpcsrv_{self.deployment_sha256[:32]}"
        if self.runtime_id is not None and self.runtime_id != expected:
            raise ValueError("RPC server runtime id differs from its deployment")
        object.__setattr__(self, "runtime_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"runtime_id"}))


class ControllerWorkerRPCServerStartupReceipt(ControllerModel):
    """Non-scientific evidence that one exact socket and operation set started."""

    schema_name: Literal["aletheia.controller_worker_rpc_server_startup_receipt"] = (
        "aletheia.controller_worker_rpc_server_startup_receipt"
    )
    schema_version: Literal[1] = 1
    runtime_id: str = Field(pattern=r"^rpcsrv_[0-9a-f]{32}$")
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
    receipt_key_id: str = Field(pattern=r"^rpck_[0-9a-f]{32}$")
    operations: tuple[ControllerWorkerRPCOperation, ...] = Field(min_length=1, max_length=14)
    socket_device_id: int = Field(ge=0)
    socket_inode: int = Field(ge=1)
    started_at: AwareDatetime
    transport_receipt_grants_scientific_authority: Literal[False] = False

    @model_validator(mode="after")
    def _operations_are_canonical(self) -> "ControllerWorkerRPCServerStartupReceipt":
        if self.operations != tuple(sorted(set(self.operations), key=lambda item: item.value)):
            raise ValueError("RPC server startup operations must be unique and canonical")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerWorkerRPCServerCycleReceipt(ControllerModel):
    """Bounded operational log for one accepted or rejected connection."""

    schema_name: Literal["aletheia.controller_worker_rpc_server_cycle_receipt"] = (
        "aletheia.controller_worker_rpc_server_cycle_receipt"
    )
    schema_version: Literal[1] = 1
    runtime_id: str = Field(pattern=r"^rpcsrv_[0-9a-f]{32}$")
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
    cycle_number: int = Field(ge=1)
    disposition: Literal["responded", "rejected"]
    request_id: str | None = Field(default=None, pattern=r"^rpcq_[0-9a-f]{32}$")
    operation: ControllerWorkerRPCOperation | None = None
    response_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    rejection_code: (
        Literal[
            "peer_credentials_mismatch",
            "request_frame_invalid",
            "request_rejected",
        ]
        | None
    ) = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    transport_receipt_grants_scientific_authority: Literal[False] = False

    @model_validator(mode="after")
    def _cycle_is_exact(self) -> "ControllerWorkerRPCServerCycleReceipt":
        responded = self.disposition == "responded"
        response_evidence = (self.request_id, self.operation, self.response_sha256)
        if (
            responded
            and (not all(value is not None for value in response_evidence) or self.rejection_code)
        ) or (
            not responded
            and (any(value is not None for value in response_evidence) or not self.rejection_code)
        ):
            raise ValueError("RPC server cycle disposition has inconsistent evidence")
        if self.finished_at < self.started_at:
            raise ValueError("RPC server cycle finished before it started")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


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
            raise ControllerWorkerRPCProcessError(f"{label} path traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ControllerWorkerRPCProcessError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= maximum_bytes
            or (expected_owner_uid is not None and before.st_uid != expected_owner_uid)
            or (expected_owner_gid is not None and before.st_gid != expected_owner_gid)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
            or before.st_nlink != 1
        ):
            raise ControllerWorkerRPCProcessError(f"{label} custody differs from its pin")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise ControllerWorkerRPCProcessError(f"{label} exceeds its byte bound")
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
        raise ControllerWorkerRPCProcessError(f"{label} changed while read")
    payload = b"".join(chunks)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ControllerWorkerRPCProcessError(f"{label} differs from its byte pin")
    return payload


def load_controller_worker_rpc_server_deployment(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> ControllerWorkerRPCServerDeployment:
    """Fresh-read one exact external-service deployment from an out-of-band digest."""

    payload = _fresh_pinned_bytes(
        path,
        expected_file_sha256,
        label="controller worker RPC server deployment",
    )
    try:
        return ControllerWorkerRPCServerDeployment.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise ControllerWorkerRPCProcessError("RPC server deployment is invalid") from exc


def _load_handlers(
    deployment: ControllerWorkerRPCServerDeployment,
) -> ControllerWorkerRPCHandlerSet:
    code_root = Path(deployment.reviewed_code_root)
    source_path = Path(deployment.composition_factory_source_path)
    try:
        if (
            code_root.resolve(strict=True) != code_root
            or source_path.resolve(strict=True) != source_path
        ):
            raise ControllerWorkerRPCProcessError("RPC reviewed source traverses a symlink")
        source_path.relative_to(code_root)
    except (OSError, ValueError) as exc:
        raise ControllerWorkerRPCProcessError(
            "RPC composition factory escaped reviewed source"
        ) from exc
    before = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="RPC composition factory",
    )
    config = _fresh_pinned_bytes(
        deployment.composition_config_path,
        deployment.composition_config_file_sha256,
        label="RPC composition config",
    )
    try:
        module = load_guarded_source_bytes(
            deployment.composition_factory_module,
            source_path,
            before,
        )
        factory = vars(module).get(deployment.composition_factory_attribute)
    except Exception as exc:
        raise ControllerWorkerRPCProcessError("RPC composition factory could not load") from exc
    after = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="RPC composition factory",
    )
    if before != after or not callable(factory):
        raise ControllerWorkerRPCProcessError("RPC composition factory changed or is not callable")
    try:
        handlers = factory(deployment=deployment, configuration_bytes=config)
    except Exception as exc:
        raise ControllerWorkerRPCProcessError("RPC composition factory failed") from exc
    if type(handlers) is not ControllerWorkerRPCHandlerSet:
        raise ControllerWorkerRPCProcessError("RPC factory returned another handler container")
    if handlers.operations != deployment.service_pin.operations:
        raise ControllerWorkerRPCProcessError("RPC factory operation set differs from deployment")
    return handlers


class ControllerWorkerRPCServerRuntime:
    """Own one Linux Unix listener and process one request per bounded cycle."""

    def __init__(
        self,
        *,
        deployment: ControllerWorkerRPCServerDeployment,
        service: ControllerWorkerRPCService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.deployment = deployment
        self._service = service
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._listener: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._startup_receipt: ControllerWorkerRPCServerStartupReceipt | None = None
        self._cycle_number = 0

    def _parent_identity(self) -> tuple[int, int, int, int, int]:
        path = Path(self.deployment.socket_parent_path)
        try:
            if path.resolve(strict=True) != path:
                raise ControllerWorkerRPCProcessError("RPC socket parent traverses a symlink")
            observed = os.lstat(path)
        except OSError as exc:
            raise ControllerWorkerRPCProcessError("RPC socket parent is unavailable") from exc
        identity = (
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            observed.st_gid,
            stat.S_IMODE(observed.st_mode),
        )
        expected = (
            self.deployment.socket_parent_device_id,
            self.deployment.socket_parent_inode,
            self.deployment.socket_parent_owner_uid,
            self.deployment.socket_parent_owner_gid,
            self.deployment.socket_parent_mode,
        )
        if not stat.S_ISDIR(observed.st_mode) or identity != expected:
            raise ControllerWorkerRPCProcessError("RPC socket parent custody differs from its pin")
        return identity

    def _live_socket_identity(self) -> tuple[int, int]:
        path = Path(self.deployment.service_pin.socket_path)
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise ControllerWorkerRPCProcessError("RPC socket disappeared") from exc
        if (
            not stat.S_ISSOCK(observed.st_mode)
            or observed.st_uid != self.deployment.service_pin.socket_owner_uid
            or observed.st_gid != self.deployment.service_pin.socket_group_gid
            or stat.S_IMODE(observed.st_mode) != self.deployment.service_pin.socket_mode
        ):
            raise ControllerWorkerRPCProcessError("RPC socket custody differs from its pin")
        return observed.st_dev, observed.st_ino

    def start(self) -> ControllerWorkerRPCServerStartupReceipt:
        if self._startup_receipt is not None:
            return self._startup_receipt
        if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
            raise ControllerWorkerRPCProcessError("RPC server requires Linux SO_PEERCRED")
        if (
            os.geteuid() != self.deployment.process_uid
            or os.getegid() != self.deployment.process_gid
        ):
            raise ControllerWorkerRPCProcessError("RPC process UID/GID differ from deployment")
        parent_before = self._parent_identity()
        socket_path = Path(self.deployment.service_pin.socket_path)
        if os.path.lexists(socket_path):
            raise ControllerWorkerRPCProcessError("RPC socket path already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o777)
        try:
            listener.bind(str(socket_path))
        except OSError as exc:
            listener.close()
            raise ControllerWorkerRPCProcessError("RPC socket could not bind") from exc
        finally:
            os.umask(previous_umask)
        try:
            os.chmod(socket_path, self.deployment.service_pin.socket_mode)
            listener.listen(16)
            listener.settimeout(self.deployment.accept_timeout_seconds)
            identity = self._live_socket_identity()
            if parent_before != self._parent_identity():
                raise ControllerWorkerRPCProcessError("RPC socket parent changed during bind")
        except Exception:
            listener.close()
            try:
                socket_path.unlink()
            except OSError:
                pass
            raise
        self._listener = listener
        self._socket_identity = identity
        self._startup_receipt = ControllerWorkerRPCServerStartupReceipt(
            runtime_id=self.deployment.runtime_id,
            deployment_sha256=self.deployment.deployment_sha256,
            service_id=self.deployment.service_pin.service_id,
            receipt_key_id=self.deployment.service_pin.receipt_key_id,
            operations=self.deployment.service_pin.operations,
            socket_device_id=identity[0],
            socket_inode=identity[1],
            started_at=self._clock(),
        )
        return self._startup_receipt

    def _verify_client(self, connection: socket.socket) -> None:
        try:
            raw = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, gid = struct.unpack("3i", raw)
        except (OSError, struct.error) as exc:
            raise ControllerWorkerRPCRequestRejected(
                "RPC client credentials could not be observed"
            ) from exc
        if (uid, gid) != (self.deployment.worker_peer_uid, self.deployment.worker_peer_gid):
            raise ControllerWorkerRPCRequestRejected("RPC client credentials differ")

    def _read_request_frame(self, connection: socket.socket) -> bytes:
        connection.settimeout(self.deployment.service_pin.connect_timeout_seconds)
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = connection.recv(
                    min(65_536, self.deployment.service_pin.max_request_bytes + 2 - total)
                )
            except (OSError, TimeoutError) as exc:
                raise ControllerWorkerRPCRequestRejected("RPC request read failed") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > self.deployment.service_pin.max_request_bytes + 1:
                raise ControllerWorkerRPCRequestRejected("RPC request frame exceeds its bound")
        framed = b"".join(chunks)
        if not framed.endswith(b"\n") or b"\n" in framed[:-1] or len(framed) <= 1:
            raise ControllerWorkerRPCRequestRejected("RPC request is not one canonical frame")
        return framed[:-1]

    def serve_once(self) -> ControllerWorkerRPCServerCycleReceipt | None:
        self.start()
        assert self._listener is not None and self._socket_identity is not None
        self._parent_identity()
        if self._live_socket_identity() != self._socket_identity:
            raise ControllerWorkerRPCProcessError("RPC socket changed between cycles")
        try:
            connection, _address = self._listener.accept()
        except TimeoutError:
            return None
        except OSError as exc:
            raise ControllerWorkerRPCProcessError("RPC listener accept failed") from exc
        started_at = self._clock()
        rejection_code: str | None = None
        request = None
        response = None
        try:
            try:
                self._verify_client(connection)
            except ControllerWorkerRPCRequestRejected:
                rejection_code = "peer_credentials_mismatch"
                raise
            try:
                request_bytes = self._read_request_frame(connection)
            except ControllerWorkerRPCRequestRejected:
                rejection_code = "request_frame_invalid"
                raise
            try:
                response = self._service.handle(request_bytes)
                request = ControllerWorkerRPCRequest.model_validate_json(request_bytes)
            except ControllerWorkerRPCRequestRejected:
                rejection_code = "request_rejected"
                raise
            connection.sendall(response + b"\n")
            connection.shutdown(socket.SHUT_WR)
        except ControllerWorkerRPCRequestRejected:
            pass
        except (OSError, TimeoutError) as exc:
            raise ControllerWorkerRPCProcessError("RPC response transport failed") from exc
        finally:
            connection.close()
        self._cycle_number += 1
        finished_at = self._clock()
        self._parent_identity()
        if self._live_socket_identity() != self._socket_identity:
            raise ControllerWorkerRPCProcessError("RPC socket changed during a request cycle")
        if response is None or request is None:
            return ControllerWorkerRPCServerCycleReceipt(
                runtime_id=self.deployment.runtime_id,
                deployment_sha256=self.deployment.deployment_sha256,
                service_id=self.deployment.service_pin.service_id,
                cycle_number=self._cycle_number,
                disposition="rejected",
                rejection_code=rejection_code or "request_rejected",
                started_at=started_at,
                finished_at=finished_at,
            )
        return ControllerWorkerRPCServerCycleReceipt(
            runtime_id=self.deployment.runtime_id,
            deployment_sha256=self.deployment.deployment_sha256,
            service_id=self.deployment.service_pin.service_id,
            cycle_number=self._cycle_number,
            disposition="responded",
            request_id=request.request_id,
            operation=request.operation,
            response_sha256=hashlib.sha256(response).hexdigest(),
            started_at=started_at,
            finished_at=finished_at,
        )

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._socket_identity is None:
            return
        socket_path = Path(self.deployment.service_pin.socket_path)
        try:
            if self._live_socket_identity() == self._socket_identity:
                socket_path.unlink()
        except ControllerWorkerRPCProcessError:
            pass
        self._socket_identity = None


def build_controller_worker_rpc_server_runtime(
    deployment: ControllerWorkerRPCServerDeployment,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ControllerWorkerRPCServerRuntime:
    """Load exact factory/key bytes and build one unstarted external service runtime."""

    deployment = ControllerWorkerRPCServerDeployment.model_validate(
        deployment.model_dump(mode="python")
    )
    handlers = _load_handlers(deployment)
    private_key = _fresh_pinned_bytes(
        deployment.receipt_private_key_path,
        deployment.receipt_private_key_sha256,
        label="RPC receipt private key",
        expected_owner_uid=deployment.process_uid,
        expected_owner_gid=deployment.process_gid,
        expected_mode=deployment.receipt_key_mode,
        maximum_bytes=32,
    )
    if len(private_key) != 32:
        raise ControllerWorkerRPCProcessError("RPC receipt private key must contain 32 raw bytes")
    try:
        service = ControllerWorkerRPCService(
            pin=deployment.service_pin,
            controller_id=deployment.controller_id,
            controller_manifest_sha256=deployment.controller_manifest_sha256,
            worker_process_principal_id=deployment.worker_process_principal_id,
            handlers=handlers,
            receipt_private_key=private_key,
            clock=clock,
        )
    except ControllerWorkerRPCServerError as exc:
        raise ControllerWorkerRPCProcessError("RPC service composition failed") from exc
    return ControllerWorkerRPCServerRuntime(
        deployment=deployment,
        service=service,
        clock=clock,
    )


__all__ = [
    "ControllerWorkerRPCProcessError",
    "ControllerWorkerRPCServerCycleReceipt",
    "ControllerWorkerRPCServerDeployment",
    "ControllerWorkerRPCServerRuntime",
    "ControllerWorkerRPCServerStartupReceipt",
    "build_controller_worker_rpc_server_runtime",
    "load_controller_worker_rpc_server_deployment",
]
