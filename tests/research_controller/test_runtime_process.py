from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.durable_tasks.contracts import RecoveryReceipt, RetryPolicy
from aletheia.research_controller.contracts import ResearchControllerManifest
from aletheia.research_controller.dispatcher import ControllerDispatchReceipt
from aletheia.research_controller.redrive import ControllerDeliveryReconciliationReceipt
from aletheia.research_controller_postgresql_runtime import build_postgresql_runtime
from aletheia.research_controller_runtime import (
    ResearchControllerRuntime,
    ResearchControllerRuntimeCycleReceipt,
    ResearchControllerRuntimeDeployment,
    ResearchControllerRuntimeError,
    ResearchControllerRuntimeRole,
    build_research_controller_runtime,
    load_research_controller_runtime_deployment,
)
from aletheia.research_kernel.schemas import canonical_sha256
from aletheia.research_store.store import PostgreSQLResearchKernelOutbox

NOW = datetime(2026, 8, 25, 3, 0, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _postgres_config(role: str, principal: str) -> bytes:
    return (
        json.dumps(
            {
                "schema_name": "aletheia.research_controller_postgresql_runtime_config",
                "schema_version": 1,
                "role": role,
                "process_principal_id": principal,
                "database_url_sha256": hashlib.sha256(
                    get_settings().database_url.encode()
                ).hexdigest(),
                "schema_revision": expected_schema_revision(),
                "scientific_authority": False,
                "kernel_command_authority": False,
                "observation_admission_authority": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="principal.controller.production",
        controller_code_sha256=_sha("controller-code"),
        controller_policy_sha256=_sha("controller-policy"),
        capability_catalog_sha256=_sha("capability-catalog"),
        protocol_registry_policy_sha256=_sha("protocol-policy"),
        scientific_bridge_policy_sha256=_sha("bridge-policy"),
        worker_manifest_sha256=_sha("worker-manifest"),
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        prepared_at=NOW,
    )


def _deployment(
    role: ResearchControllerRuntimeRole,
    **updates: object,
) -> ResearchControllerRuntimeDeployment:
    payload: dict[str, object] = {
        "role": role,
        "controller_manifest_path": "/opt/aletheia/controller/manifest.json",
        "controller_manifest_file_sha256": _sha("controller-manifest-file"),
        "controller_manifest_sha256": _manifest().manifest_sha256,
        "reviewed_code_root": "/opt/aletheia/release",
        "composition_factory_module": "aletheia.production.controller_composition",
        "composition_factory_attribute": "build_runtime",
        "composition_factory_source_path": (
            "/opt/aletheia/release/aletheia/production/controller_composition.py"
        ),
        "composition_factory_source_sha256": _sha("factory-source"),
        "composition_config_path": "/etc/aletheia/controller/runtime.json",
        "composition_config_file_sha256": _sha("factory-config"),
        "process_principal_id": f"principal.controller.{role.value}",
        "prepared_at": NOW,
    }
    payload.update(updates)
    return ResearchControllerRuntimeDeployment.model_validate(payload)


class _Clock:
    def __init__(self) -> None:
        self._next = NOW

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(seconds=1)
        return value


class _RecoveryQueue:
    def __init__(self) -> None:
        self.calls = 0

    def recover_expired(self) -> RecoveryReceipt:
        self.calls += 1
        return RecoveryReceipt(
            recovered_task_ids=("task_recovered",),
            terminalized_task_ids=("task_terminal",),
            dependency_failed_task_ids=(),
            recovered_at=NOW,
        )


class _IdleWorker:
    async def run_once(self):
        return None


class _Dispatcher:
    def dispatch_once(self) -> ControllerDispatchReceipt:
        return ControllerDispatchReceipt(
            delivered_outbox_sha256s=(_sha("delivered"),),
            concurrency_deferred_quest_ids=(),
            registered_quest_count=1,
        )


class _Reconciler:
    def reconcile_once(self) -> ControllerDeliveryReconciliationReceipt:
        return ControllerDeliveryReconciliationReceipt(
            redriven_attempt_sha256s=(_sha("redriven"),),
            successor_attempt_sha256s=(),
            dead_letter_resolution_sha256s=(),
            terminal_resolution_sha256s=(),
            concurrency_deferred_delivery_sha256s=(),
            inspected_delivery_count=1,
        )


def test_runtime_deployment_is_closed_and_self_identifying() -> None:
    deployment = _deployment(ResearchControllerRuntimeRole.KERNEL_DISPATCHER)
    assert deployment.runtime_id == f"rtr_{deployment.deployment_sha256[:32]}"
    assert deployment.one_role_per_process is True
    assert deployment.direct_kernel_mutation_allowed is False
    assert deployment.direct_observation_admission_allowed is False
    with pytest.raises(ValidationError, match="inside the reviewed code root"):
        _deployment(
            ResearchControllerRuntimeRole.KERNEL_DISPATCHER,
            composition_factory_source_path="/srv/unreviewed/factory.py",
        )
    with pytest.raises(ValidationError, match="distinct"):
        _deployment(
            ResearchControllerRuntimeRole.KERNEL_DISPATCHER,
            composition_config_path="/opt/aletheia/controller/manifest.json",
        )


@pytest.mark.asyncio
async def test_worker_start_recovers_once_and_idle_cycle_is_non_authoritative() -> None:
    queue = _RecoveryQueue()
    runtime = ResearchControllerRuntime(
        deployment=_deployment(ResearchControllerRuntimeRole.WORKER),
        controller_manifest=_manifest(),
        component=_IdleWorker(),
        queue=queue,
        clock=_Clock(),
    )
    startup = await runtime.start()
    assert startup.recovered_task_ids == ("task_recovered",)
    assert startup.terminalized_task_ids == ("task_terminal",)
    assert startup.scientific_authority is False
    assert await runtime.start() == startup
    assert queue.calls == 1

    cycle = await runtime.run_once()
    assert cycle.result_kind == "controller_task_idle"
    assert cycle.work_performed is False
    assert cycle.result_payload == {}
    assert cycle.scientific_authority is False
    assert cycle.finished_at > cycle.started_at
    assert (
        ResearchControllerRuntimeCycleReceipt.model_validate(cycle.model_dump(mode="python"))
        == cycle
    )
    with pytest.raises(ValidationError, match="result hash"):
        ResearchControllerRuntimeCycleReceipt.model_validate(
            {
                **cycle.model_dump(mode="python"),
                "result_sha256": _sha("rebound-result"),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "component", "kind"),
    (
        (
            ResearchControllerRuntimeRole.KERNEL_DISPATCHER,
            _Dispatcher(),
            "kernel_dispatch",
        ),
        (
            ResearchControllerRuntimeRole.TERMINAL_DISPATCHER,
            _Dispatcher(),
            "terminal_dispatch",
        ),
        (
            ResearchControllerRuntimeRole.DELIVERY_RECONCILER,
            _Reconciler(),
            "delivery_reconciliation",
        ),
    ),
)
async def test_operational_roles_emit_typed_hashed_work_receipts(
    role: ResearchControllerRuntimeRole,
    component: object,
    kind: str,
) -> None:
    runtime = ResearchControllerRuntime(
        deployment=_deployment(role),
        controller_manifest=_manifest(),
        component=component,
        queue=object(),
        clock=_Clock(),
    )
    receipt = await runtime.run_once()
    assert receipt.role is role
    assert receipt.result_kind == kind
    assert receipt.work_performed is True
    assert receipt.cycle_number == 1
    assert receipt.result_sha256 == canonical_sha256(
        {"result_kind": kind, "result_payload": receipt.result_payload}
    )


def test_runtime_deployment_file_is_fresh_read_and_externally_pinned(tmp_path: Path) -> None:
    deployment = _deployment(ResearchControllerRuntimeRole.DELIVERY_RECONCILER)
    path = tmp_path / "runtime.json"
    path.write_text(deployment.model_dump_json(), encoding="utf-8")
    loaded = load_research_controller_runtime_deployment(
        path,
        expected_file_sha256=_file_sha(path),
    )
    assert loaded == deployment
    with pytest.raises(ResearchControllerRuntimeError, match="deployment pin"):
        load_research_controller_runtime_deployment(
            path,
            expected_file_sha256=_sha("wrong"),
        )


@pytest.mark.asyncio
async def test_pinned_factory_builds_only_its_declared_role(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    factory = (root / "aletheia/research_controller_postgresql_runtime.py").resolve()
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(_manifest().model_dump_json(), encoding="utf-8")
    config_path = tmp_path / "composition.json"
    principal = "principal.controller.delivery_reconciler"
    config_path.write_bytes(_postgres_config("delivery_reconciler", principal))
    deployment = _deployment(
        ResearchControllerRuntimeRole.DELIVERY_RECONCILER,
        controller_manifest_path=str(controller_path),
        controller_manifest_file_sha256=_file_sha(controller_path),
        reviewed_code_root=str(root),
        composition_factory_module="aletheia.research_controller_postgresql_runtime",
        composition_factory_attribute="build_postgresql_runtime",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=_file_sha(factory),
        composition_config_path=str(config_path),
        composition_config_file_sha256=_file_sha(config_path),
    )
    runtime = build_research_controller_runtime(deployment)
    startup = await runtime.start()
    assert startup.role is ResearchControllerRuntimeRole.DELIVERY_RECONCILER
    assert startup.recovered_task_ids == ()

    rebound = ResearchControllerRuntimeDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "role": ResearchControllerRuntimeRole.WORKER,
        }
    )
    with pytest.raises(ResearchControllerRuntimeError, match="factory failed"):
        build_research_controller_runtime(rebound)

    config_path.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ResearchControllerRuntimeError, match="composition config.*pin"):
        build_research_controller_runtime(deployment)


def test_postgresql_factory_minimizes_kernel_dispatch_authority() -> None:
    principal = "principal.controller.kernel_dispatcher"
    deployment = _deployment(
        ResearchControllerRuntimeRole.KERNEL_DISPATCHER,
        process_principal_id=principal,
    )
    dependencies = build_postgresql_runtime(
        deployment=deployment,
        controller_manifest=_manifest(),
        configuration_bytes=_postgres_config("kernel_dispatcher", principal),
    )

    assert isinstance(dependencies.kernel_store, PostgreSQLResearchKernelOutbox)
    assert dependencies.terminal_outbox is None
    assert dependencies.service is None
    assert dependencies.queue.principal == principal
    assert not hasattr(dependencies.kernel_store, "commit")
    assert not hasattr(dependencies.kernel_store, "audit")

    duplicate = _postgres_config("kernel_dispatcher", principal).replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_postgresql_runtime(
            deployment=deployment,
            controller_manifest=_manifest(),
            configuration_bytes=duplicate,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    "ALETHEIA_DATABASE_URL" not in os.environ,
    reason="destructive isolated PostgreSQL runtime test requires an explicit database",
)
async def test_pinned_postgresql_roles_run_against_current_schema(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    factory = (root / "aletheia/research_controller_postgresql_runtime.py").resolve()
    controller_path = tmp_path / "controller.json"
    controller_path.write_text(_manifest().model_dump_json(), encoding="utf-8")

    for role in (
        ResearchControllerRuntimeRole.KERNEL_DISPATCHER,
        ResearchControllerRuntimeRole.DELIVERY_RECONCILER,
    ):
        principal = f"principal.controller.{role.value}"
        config_path = tmp_path / f"{role.value}.json"
        config_path.write_bytes(_postgres_config(role.value, principal))
        deployment = _deployment(
            role,
            controller_manifest_path=str(controller_path),
            controller_manifest_file_sha256=_file_sha(controller_path),
            reviewed_code_root=str(root),
            composition_factory_module="aletheia.research_controller_postgresql_runtime",
            composition_factory_attribute="build_postgresql_runtime",
            composition_factory_source_path=str(factory),
            composition_factory_source_sha256=_file_sha(factory),
            composition_config_path=str(config_path),
            composition_config_file_sha256=_file_sha(config_path),
            process_principal_id=principal,
        )
        cycle = await build_research_controller_runtime(deployment).run_once()
        assert cycle.work_performed is False
        if role is ResearchControllerRuntimeRole.KERNEL_DISPATCHER:
            assert cycle.result_kind == "kernel_dispatch"
            assert cycle.result_payload["registered_quest_count"] == 0
        else:
            assert cycle.result_kind == "delivery_reconciliation"
            assert cycle.result_payload["inspected_delivery_count"] == 0
