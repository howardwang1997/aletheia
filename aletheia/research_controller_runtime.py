"""Deployment-pinned outer runtime for the durable Research Kernel controller.

The protected :mod:`aletheia.research_controller` package deliberately owns no process loader and
does not import the legacy engineering queue.  This outer module is the reviewed composition seam:
one process performs exactly one operational role, while a byte-pinned deployment factory supplies
the role's concrete database, execution, recovery, and step-authority adapters.

Runtime cycle receipts are monitoring evidence only.  They never authorize a Research Kernel
command, validate an observation, admit a scientific slot, or turn a task result into a claim.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.durable_tasks.contracts import RecoveryReceipt, TaskOutcome
from aletheia.jobs.queue import DurableTaskQueue
from aletheia.jobs.worker import DurableWorker
from aletheia.migration.dynamic_loader import load_guarded_source_bytes
from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    ControllerModel,
    ResearchControllerManifest,
)
from aletheia.research_controller.dispatcher import (
    ControllerDispatchReceipt,
    ExecutionTerminalOutboxDispatcher,
    QualificationTerminalOutboxPort,
    ResearchKernelOutboxPort,
    ResearchKernelOutboxDispatcher,
)
from aletheia.research_controller.redrive import (
    ControllerDeliveryReconciler,
    ControllerDeliveryReconciliationReceipt,
)
from aletheia.research_controller.service import ResearchControllerService
from aletheia.research_controller.worker import research_controller_task_handler
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_MODULE_PATTERN = r"^aletheia(?:[.][A-Za-z_][A-Za-z0-9_]*)+$"
_ATTRIBUTE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_MAX_PINNED_FILE_BYTES = 4 * 1024 * 1024


class ResearchControllerRuntimeError(RuntimeError):
    """A deployment pin, component factory, or runtime result failed closed."""


class ResearchControllerRuntimeRole(str, Enum):
    """Exactly one independently supervised responsibility per process."""

    KERNEL_DISPATCHER = "kernel_dispatcher"
    TERMINAL_DISPATCHER = "terminal_dispatcher"
    WORKER = "worker"
    DELIVERY_RECONCILER = "delivery_reconciler"


def _canonical_absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not path.is_absolute()
        or str(path) != os.path.normpath(value)
    ):
        raise ValueError(f"{label} must be one canonical absolute path")
    return path


class ResearchControllerRuntimeDeployment(ControllerModel):
    """One byte-pinned process role and its deployment-owned composition entry point."""

    schema_name: Literal["aletheia.research_controller_runtime_deployment"] = (
        "aletheia.research_controller_runtime_deployment"
    )
    schema_version: Literal[1] = 1
    runtime_id: str | None = Field(default=None, pattern=r"^rtr_[0-9a-f]{32}$")
    role: ResearchControllerRuntimeRole
    controller_manifest_path: str
    controller_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewed_code_root: str
    composition_factory_module: str = Field(pattern=_MODULE_PATTERN)
    composition_factory_attribute: str = Field(pattern=_ATTRIBUTE_PATTERN)
    composition_factory_source_path: str
    composition_factory_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    composition_config_path: str
    composition_config_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    idle_seconds: float = Field(default=1.0, ge=0.05, le=3_600.0)
    prepared_at: AwareDatetime
    one_role_per_process: Literal[True] = True
    unpinned_dynamic_loading_allowed: Literal[False] = False
    legacy_optimize_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _paths_and_identity_are_canonical(self) -> "ResearchControllerRuntimeDeployment":
        controller = _canonical_absolute_path(
            self.controller_manifest_path,
            label="controller manifest path",
        )
        code_root = _canonical_absolute_path(self.reviewed_code_root, label="reviewed code root")
        factory = _canonical_absolute_path(
            self.composition_factory_source_path,
            label="composition factory source path",
        )
        config = _canonical_absolute_path(
            self.composition_config_path,
            label="composition config path",
        )
        try:
            factory.relative_to(code_root)
        except ValueError as exc:
            raise ValueError("composition factory must be inside the reviewed code root") from exc
        if len({controller, factory, config}) != 3:
            raise ValueError("controller manifest, factory source, and config must be distinct")
        expected = f"rtr_{self.deployment_sha256[:32]}"
        if self.runtime_id is not None and self.runtime_id != expected:
            raise ValueError("runtime id differs from its deployment manifest")
        object.__setattr__(self, "runtime_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"runtime_id"}))


class ResearchControllerRuntimeStartupReceipt(ControllerModel):
    """Process-local startup/recovery observation; never scientific authority."""

    schema_name: Literal["aletheia.research_controller_runtime_startup_receipt"] = (
        "aletheia.research_controller_runtime_startup_receipt"
    )
    schema_version: Literal[1] = 1
    runtime_id: str = Field(pattern=r"^rtr_[0-9a-f]{32}$")
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: ResearchControllerRuntimeRole
    recovered_task_ids: tuple[str, ...]
    terminalized_task_ids: tuple[str, ...]
    dependency_failed_task_ids: tuple[str, ...]
    started_at: AwareDatetime
    scientific_authority: Literal[False] = False

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchControllerRuntimeCycleReceipt(ControllerModel):
    """One hashed operational cycle suitable for structured service logs."""

    schema_name: Literal["aletheia.research_controller_runtime_cycle_receipt"] = (
        "aletheia.research_controller_runtime_cycle_receipt"
    )
    schema_version: Literal[1] = 1
    runtime_id: str = Field(pattern=r"^rtr_[0-9a-f]{32}$")
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: ResearchControllerRuntimeRole
    cycle_number: int = Field(ge=1)
    work_performed: bool
    result_kind: Literal[
        "kernel_dispatch",
        "terminal_dispatch",
        "controller_task",
        "controller_task_idle",
        "delivery_reconciliation",
    ]
    result_payload: dict[str, object]
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    scientific_authority: Literal[False] = False

    @model_validator(mode="after")
    def _result_is_exact_and_role_typed(self) -> "ResearchControllerRuntimeCycleReceipt":
        expected_kind = {
            ResearchControllerRuntimeRole.KERNEL_DISPATCHER: {"kernel_dispatch"},
            ResearchControllerRuntimeRole.TERMINAL_DISPATCHER: {"terminal_dispatch"},
            ResearchControllerRuntimeRole.WORKER: {
                "controller_task",
                "controller_task_idle",
            },
            ResearchControllerRuntimeRole.DELIVERY_RECONCILER: {"delivery_reconciliation"},
        }[self.role]
        if self.result_kind not in expected_kind:
            raise ValueError("runtime result kind differs from its process role")
        if self.finished_at < self.started_at:
            raise ValueError("runtime cycle finished before it started")
        expected_sha256 = canonical_sha256(
            {
                "result_kind": self.result_kind,
                "result_payload": self.result_payload,
            }
        )
        if self.result_sha256 != expected_sha256:
            raise ValueError("runtime cycle result hash differs from its payload")
        if self.result_kind == "controller_task_idle" and (
            self.work_performed or self.result_payload
        ):
            raise ValueError("an idle controller worker cannot report work or a result payload")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True)
class ResearchControllerRuntimeDependencies:
    """Exact objects returned by a pinned deployment factory.

    Only the dependency needed by ``deployment.role`` may be populated.  Keeping the container
    closed prevents a dispatcher process from silently receiving a step executor or vice versa.
    """

    queue: object
    kernel_store: ResearchKernelOutboxPort | None = None
    terminal_outbox: QualificationTerminalOutboxPort | None = None
    service: ResearchControllerService | None = None


def research_controller_durable_worker(
    *,
    manifest: ResearchControllerManifest,
    service: ResearchControllerService,
    queue: DurableTaskQueue | None = None,
) -> DurableWorker:
    """Pin the legacy engineering worker to one deployment-owned controller manifest."""

    worker_id = f"research-controller:{manifest.controller_id}"
    return DurableWorker(
        worker_id=worker_id,
        worker_manifest_sha256=manifest.worker_manifest_sha256,
        handlers={
            CONTROLLER_TASK_TYPE: research_controller_task_handler(
                manifest=manifest,
                service=service,
            )
        },
        queue=queue,
    )


def _fresh_pinned_bytes(
    path_value: str | Path,
    expected_sha256: str,
    *,
    label: str,
) -> bytes:
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ResearchControllerRuntimeError(f"{label} is unavailable") from exc
    if resolved != path:
        raise ResearchControllerRuntimeError(f"{label} path traverses a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResearchControllerRuntimeError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ResearchControllerRuntimeError(f"{label} must be a regular file")
        if not 0 < before.st_size <= _MAX_PINNED_FILE_BYTES:
            raise ResearchControllerRuntimeError(f"{label} exceeds its byte bound")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_PINNED_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PINNED_FILE_BYTES:
                raise ResearchControllerRuntimeError(f"{label} exceeds its byte bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or total != before.st_size
    ):
        raise ResearchControllerRuntimeError(f"{label} changed while it was read")
    payload = b"".join(chunks)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ResearchControllerRuntimeError(f"{label} differs from its deployment pin")
    return payload


def load_research_controller_runtime_deployment(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> ResearchControllerRuntimeDeployment:
    """Fresh-read one exact runtime deployment manifest from an external byte pin."""

    try:
        payload = _fresh_pinned_bytes(
            path,
            expected_file_sha256,
            label="research-controller runtime deployment manifest",
        )
        return ResearchControllerRuntimeDeployment.model_validate_json(payload)
    except ResearchControllerRuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise ResearchControllerRuntimeError(
            "research-controller runtime deployment manifest is invalid"
        ) from exc


def _load_controller_manifest(
    deployment: ResearchControllerRuntimeDeployment,
) -> ResearchControllerManifest:
    try:
        payload = _fresh_pinned_bytes(
            deployment.controller_manifest_path,
            deployment.controller_manifest_file_sha256,
            label="research-controller manifest",
        )
        manifest = ResearchControllerManifest.model_validate_json(payload)
    except ResearchControllerRuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise ResearchControllerRuntimeError("research-controller manifest is invalid") from exc
    if manifest.manifest_sha256 != deployment.controller_manifest_sha256:
        raise ResearchControllerRuntimeError(
            "research-controller manifest differs from the runtime deployment"
        )
    return manifest


def _load_runtime_dependencies(
    *,
    deployment: ResearchControllerRuntimeDeployment,
    controller_manifest: ResearchControllerManifest,
) -> ResearchControllerRuntimeDependencies:
    code_root = Path(deployment.reviewed_code_root)
    source_path = Path(deployment.composition_factory_source_path)
    try:
        resolved_root = code_root.resolve(strict=True)
        resolved_source = source_path.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ResearchControllerRuntimeError(
            "composition factory escaped the reviewed code root"
        ) from exc
    if resolved_root != code_root or resolved_source != source_path:
        raise ResearchControllerRuntimeError(
            "reviewed code root or composition factory traverses a symlink"
        )
    source_before = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="research-controller composition factory",
    )
    config_bytes = _fresh_pinned_bytes(
        deployment.composition_config_path,
        deployment.composition_config_file_sha256,
        label="research-controller composition config",
    )
    try:
        module = load_guarded_source_bytes(
            deployment.composition_factory_module,
            source_path,
            source_before,
        )
        factory = vars(module).get(deployment.composition_factory_attribute)
    except Exception as exc:
        raise ResearchControllerRuntimeError(
            "research-controller composition factory could not be loaded"
        ) from exc
    source_after = _fresh_pinned_bytes(
        source_path,
        deployment.composition_factory_source_sha256,
        label="research-controller composition factory",
    )
    if source_before != source_after or not callable(factory):
        raise ResearchControllerRuntimeError(
            "research-controller composition factory changed or is not callable"
        )
    try:
        dependencies = factory(
            deployment=deployment,
            controller_manifest=controller_manifest,
            configuration_bytes=config_bytes,
        )
    except Exception as exc:
        raise ResearchControllerRuntimeError(
            "research-controller composition factory failed"
        ) from exc
    if type(dependencies) is not ResearchControllerRuntimeDependencies:
        raise ResearchControllerRuntimeError(
            "composition factory returned an unsupported dependency container"
        )
    return dependencies


def _require_methods(value: object, methods: tuple[str, ...], *, label: str) -> None:
    if value is None or not all(callable(getattr(value, name, None)) for name in methods):
        raise ResearchControllerRuntimeError(f"{label} does not implement its exact runtime port")


def _validate_role_dependencies(
    deployment: ResearchControllerRuntimeDeployment,
    dependencies: ResearchControllerRuntimeDependencies,
) -> None:
    role = deployment.role
    _require_methods(
        dependencies.queue,
        ("enqueue_in_session", "get_in_session"),
        label="durable task queue",
    )
    if getattr(dependencies.queue, "principal", None) != deployment.process_principal_id:
        raise ResearchControllerRuntimeError(
            "durable task queue principal differs from the deployment process principal"
        )
    expected = {
        ResearchControllerRuntimeRole.KERNEL_DISPATCHER: (True, False, False),
        ResearchControllerRuntimeRole.TERMINAL_DISPATCHER: (False, True, False),
        ResearchControllerRuntimeRole.WORKER: (False, False, True),
        ResearchControllerRuntimeRole.DELIVERY_RECONCILER: (False, False, False),
    }[role]
    observed = (
        dependencies.kernel_store is not None,
        dependencies.terminal_outbox is not None,
        dependencies.service is not None,
    )
    if observed != expected:
        raise ResearchControllerRuntimeError(
            "runtime factory supplied missing or cross-role privileged dependencies"
        )
    if role is ResearchControllerRuntimeRole.KERNEL_DISPATCHER:
        _require_methods(
            dependencies.kernel_store,
            ("list_pending_outbox_in_session", "mark_outbox_published_in_session"),
            label="Research Kernel outbox",
        )
    elif role is ResearchControllerRuntimeRole.TERMINAL_DISPATCHER:
        _require_methods(
            dependencies.terminal_outbox,
            ("load_qualification_terminal_outbox_in_session",),
            label="qualification terminal outbox",
        )
    elif role is ResearchControllerRuntimeRole.WORKER:
        _require_methods(
            dependencies.queue,
            ("claim", "heartbeat", "fail", "complete", "recover_expired"),
            label="durable worker queue",
        )
        if type(dependencies.service) is not ResearchControllerService:
            raise ResearchControllerRuntimeError(
                "controller worker requires the exact stateless controller service"
            )


class ResearchControllerRuntime:
    """One independently supervised process role with fail-fast invariant handling."""

    def __init__(
        self,
        *,
        deployment: ResearchControllerRuntimeDeployment,
        controller_manifest: ResearchControllerManifest,
        component: object,
        queue: object,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.deployment = deployment
        self.controller_manifest = controller_manifest
        self._component = component
        self._queue = queue
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._startup_receipt: ResearchControllerRuntimeStartupReceipt | None = None
        self._cycle_number = 0

    async def start(self) -> ResearchControllerRuntimeStartupReceipt:
        if self._startup_receipt is not None:
            return self._startup_receipt
        recovery: RecoveryReceipt | None = None
        if self.deployment.role is ResearchControllerRuntimeRole.WORKER:
            _require_methods(self._queue, ("recover_expired",), label="durable worker queue")
            recovery = await asyncio.to_thread(self._queue.recover_expired)
            if type(recovery) is not RecoveryReceipt:
                raise ResearchControllerRuntimeError(
                    "durable worker startup returned an invalid recovery receipt"
                )
        self._startup_receipt = ResearchControllerRuntimeStartupReceipt(
            runtime_id=self.deployment.runtime_id,
            deployment_sha256=self.deployment.deployment_sha256,
            controller_id=self.controller_manifest.controller_id,
            controller_manifest_sha256=self.controller_manifest.manifest_sha256,
            role=self.deployment.role,
            recovered_task_ids=() if recovery is None else recovery.recovered_task_ids,
            terminalized_task_ids=() if recovery is None else recovery.terminalized_task_ids,
            dependency_failed_task_ids=(
                () if recovery is None else recovery.dependency_failed_task_ids
            ),
            started_at=self._clock(),
        )
        return self._startup_receipt

    async def run_once(self) -> ResearchControllerRuntimeCycleReceipt:
        await self.start()
        started_at = self._clock()
        role = self.deployment.role
        if role is ResearchControllerRuntimeRole.KERNEL_DISPATCHER:
            result = await asyncio.to_thread(self._component.dispatch_once)
            if type(result) is not ControllerDispatchReceipt:
                raise ResearchControllerRuntimeError(
                    "Kernel dispatcher returned an invalid receipt"
                )
            result_kind = "kernel_dispatch"
            payload = result.model_dump(mode="json")
            work_performed = bool(result.delivered_outbox_sha256s)
        elif role is ResearchControllerRuntimeRole.TERMINAL_DISPATCHER:
            result = await asyncio.to_thread(self._component.dispatch_once)
            if type(result) is not ControllerDispatchReceipt:
                raise ResearchControllerRuntimeError(
                    "terminal dispatcher returned an invalid receipt"
                )
            result_kind = "terminal_dispatch"
            payload = result.model_dump(mode="json")
            work_performed = bool(result.delivered_outbox_sha256s)
        elif role is ResearchControllerRuntimeRole.DELIVERY_RECONCILER:
            result = await asyncio.to_thread(self._component.reconcile_once)
            if type(result) is not ControllerDeliveryReconciliationReceipt:
                raise ResearchControllerRuntimeError(
                    "delivery reconciler returned an invalid receipt"
                )
            result_kind = "delivery_reconciliation"
            payload = result.model_dump(mode="json")
            work_performed = any(
                (
                    result.redriven_attempt_sha256s,
                    result.successor_attempt_sha256s,
                    result.dead_letter_resolution_sha256s,
                    result.terminal_resolution_sha256s,
                )
            )
        else:
            outcome = await self._component.run_once()
            if outcome is None:
                result_kind = "controller_task_idle"
                payload = {}
                work_performed = False
            elif type(outcome) is TaskOutcome:
                result_kind = "controller_task"
                payload = {
                    "task_id": outcome.task.task_id,
                    "task_status": outcome.task.status.value,
                    "attempt_id": outcome.attempt.attempt_id,
                    "terminal_category": (
                        None
                        if outcome.attempt.terminal_category is None
                        else outcome.attempt.terminal_category.value
                    ),
                    "result_sha256": outcome.attempt.result_sha256,
                    "replayed": outcome.replayed,
                }
                work_performed = True
            else:
                raise ResearchControllerRuntimeError(
                    "controller worker returned an invalid task outcome"
                )
        finished_at = self._clock()
        self._cycle_number += 1
        result_sha256 = canonical_sha256(
            {
                "result_kind": result_kind,
                "result_payload": payload,
            }
        )
        return ResearchControllerRuntimeCycleReceipt(
            runtime_id=self.deployment.runtime_id,
            deployment_sha256=self.deployment.deployment_sha256,
            controller_id=self.controller_manifest.controller_id,
            controller_manifest_sha256=self.controller_manifest.manifest_sha256,
            role=role,
            cycle_number=self._cycle_number,
            work_performed=work_performed,
            result_kind=result_kind,
            result_payload=payload,
            result_sha256=result_sha256,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def run_forever(
        self,
        *,
        stop: asyncio.Event,
        emit: Callable[[ResearchControllerRuntimeCycleReceipt], None] | None = None,
    ) -> None:
        await self.start()
        while not stop.is_set():
            receipt = await self.run_once()
            if emit is not None:
                emitted = emit(receipt)
                if inspect.isawaitable(emitted):
                    raise ResearchControllerRuntimeError(
                        "runtime receipt emitter must be synchronous"
                    )
            if receipt.work_performed:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.deployment.idle_seconds,
                )
            except TimeoutError:
                pass


def build_research_controller_runtime(
    deployment: ResearchControllerRuntimeDeployment,
) -> ResearchControllerRuntime:
    """Load exact deployment inputs and compose one role without unused authorities."""

    deployment = ResearchControllerRuntimeDeployment.model_validate(
        deployment.model_dump(mode="python")
    )
    controller_manifest = _load_controller_manifest(deployment)
    dependencies = _load_runtime_dependencies(
        deployment=deployment,
        controller_manifest=controller_manifest,
    )
    _validate_role_dependencies(deployment, dependencies)
    if deployment.role is ResearchControllerRuntimeRole.KERNEL_DISPATCHER:
        component = ResearchKernelOutboxDispatcher(
            kernel_store=dependencies.kernel_store,
            manifest=controller_manifest,
            queue=dependencies.queue,
        )
    elif deployment.role is ResearchControllerRuntimeRole.TERMINAL_DISPATCHER:
        component = ExecutionTerminalOutboxDispatcher(
            terminal_outbox=dependencies.terminal_outbox,
            manifest=controller_manifest,
            queue=dependencies.queue,
        )
    elif deployment.role is ResearchControllerRuntimeRole.DELIVERY_RECONCILER:
        component = ControllerDeliveryReconciler(
            manifest=controller_manifest,
            queue=dependencies.queue,
        )
    else:
        component = research_controller_durable_worker(
            manifest=controller_manifest,
            service=dependencies.service,
            queue=dependencies.queue,
        )
    return ResearchControllerRuntime(
        deployment=deployment,
        controller_manifest=controller_manifest,
        component=component,
        queue=dependencies.queue,
    )


__all__ = [
    "ResearchControllerRuntime",
    "ResearchControllerRuntimeCycleReceipt",
    "ResearchControllerRuntimeDependencies",
    "ResearchControllerRuntimeDeployment",
    "ResearchControllerRuntimeError",
    "ResearchControllerRuntimeRole",
    "ResearchControllerRuntimeStartupReceipt",
    "build_research_controller_runtime",
    "load_research_controller_runtime_deployment",
    "research_controller_durable_worker",
]
