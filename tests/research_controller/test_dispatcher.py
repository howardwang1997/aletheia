from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aletheia.jobs.contracts import RetryPolicy
from aletheia.jobs.queue import TaskConcurrencyConflict
from aletheia.observations.store import ControllerDeliveryWrite, ControllerRegistrationWrite
from aletheia.research_controller.contracts import (
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_task_spec,
)
from aletheia.research_controller.dispatcher import (
    ExecutionTerminalOutboxDispatcher,
    ResearchKernelOutboxDispatcher,
)
from aletheia.research_store.store import ResearchKernelOutboxItem

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:local-v1",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(max_attempts=3, lease_seconds=60, heartbeat_interval_seconds=10),
        prepared_at=NOW,
    )


def _registration() -> ControllerRegistrationWrite:
    request = ResearchControllerLaunchRequest(
        program_id="prg_" + "7" * 32,
        quest_id="qst_" + "8" * 32,
        idempotency_key="launch:dispatcher",
        expected_stream_version=1,
        expected_tail_event_sha256="9" * 64,
        expected_snapshot_sha256="a" * 64,
    )
    registration = ResearchControllerRegistration(
        registration_id=request.registration_id,
        launch_request=request,
        controller_id=_manifest().controller_id,
        controller_manifest_sha256=_manifest().manifest_sha256,
        controller_principal_id=_manifest().controller_key,
        registered_by_principal_id="http-user:owner",
        registered_at=NOW,
    )
    return ControllerRegistrationWrite.from_contract(registration)


def _outbox() -> ResearchKernelOutboxItem:
    event_sha256 = "b" * 64
    quest_id = _registration().quest_id
    return ResearchKernelOutboxItem(
        outbox_id=f"rko_{event_sha256[:32]}",
        quest_id=quest_id,
        sequence=2,
        event_sha256=event_sha256,
        delivery_key=f"{quest_id}:2",
        payload_sha256=event_sha256,
        delivery_status="pending",
        delivery_attempts=0,
        available_at=NOW,
        created_at=NOW,
    )


class _Session:
    def scalar(self, _statement):
        return NOW


class _Kernel:
    def __init__(self):
        self.published = []

    def list_pending_outbox_in_session(self, _session, **kwargs):
        assert kwargs["registered_quest_ids"] == (_registration().quest_id,)
        return (_outbox(),)

    def mark_outbox_published_in_session(self, _session, item):
        self.published.append(item.event_sha256)


class _Queue:
    def __init__(self, *, conflict: bool = False):
        self.conflict = conflict
        self.specs = []

    def enqueue_in_session(self, _session, spec):
        self.specs.append(spec)
        if self.conflict:
            raise TaskConcurrencyConflict("busy", existing_task_id="task-existing")
        return SimpleNamespace(
            task=SimpleNamespace(task_id=spec.task_id, created_at=NOW), created=True
        )


def _patch(monkeypatch, recorded):
    @contextmanager
    def scope():
        yield _Session()

    monkeypatch.setattr("aletheia.research_controller.dispatcher.session_scope", scope)
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.list_controller_registrations",
        lambda _session: (_registration(),),
    )
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.get_controller_delivery_by_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.record_controller_delivery",
        lambda _session, write: recorded.append(write),
    )
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.record_controller_delivery_attempt",
        lambda _session, write: recorded.append(write),
    )


def test_kernel_outbox_delivery_enqueues_records_and_publishes_atomically(monkeypatch) -> None:
    recorded = []
    _patch(monkeypatch, recorded)
    kernel = _Kernel()
    queue = _Queue()
    receipt = ResearchKernelOutboxDispatcher(
        kernel_store=kernel, manifest=_manifest(), queue=queue
    ).dispatch_once()
    assert receipt.delivered_outbox_sha256s == ("b" * 64,)
    assert receipt.concurrency_deferred_quest_ids == ()
    assert recorded[0].source_kind == "kernel_outbox"
    assert recorded[0].source_stream_version == 2
    assert recorded[1].generation == 0
    assert recorded[1].task_id == queue.specs[0].task_id
    assert kernel.published == ["b" * 64]


def test_active_quest_task_leaves_exact_outbox_pending(monkeypatch) -> None:
    recorded = []
    _patch(monkeypatch, recorded)
    kernel = _Kernel()
    receipt = ResearchKernelOutboxDispatcher(
        kernel_store=kernel, manifest=_manifest(), queue=_Queue(conflict=True)
    ).dispatch_once()
    assert receipt.delivered_outbox_sha256s == ()
    assert receipt.concurrency_deferred_quest_ids == (_registration().quest_id,)
    assert recorded == []
    assert kernel.published == []


def test_execution_terminal_outbox_wakes_owning_quest_once(monkeypatch) -> None:
    recorded = []
    _patch(monkeypatch, recorded)
    authorization = SimpleNamespace(
        quest_id=_registration().quest_id,
        execution_id="exe_" + "c" * 32,
        attempt_id="iat_" + "d" * 32,
    )
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.list_scientific_execution_authorizations",
        lambda _session: (authorization,),
    )
    item = SimpleNamespace(
        outbox_id="qto_" + "e" * 64,
        terminal_authority_sha256="e" * 64,
        execution_id=authorization.execution_id,
        attempt_id=authorization.attempt_id,
    )

    class _Terminal:
        def load_qualification_terminal_outbox_in_session(self, _session, **kwargs):
            assert kwargs == {
                "execution_id": authorization.execution_id,
                "attempt_id": authorization.attempt_id,
            }
            return item

    receipt = ExecutionTerminalOutboxDispatcher(
        terminal_outbox=_Terminal(), manifest=_manifest(), queue=_Queue()
    ).dispatch_once()
    assert receipt.delivered_outbox_sha256s == ("e" * 64,)
    assert recorded[0].source_kind == "execution_terminal_outbox"
    assert recorded[0].execution_id == authorization.execution_id
    assert recorded[0].attempt_id == authorization.attempt_id
    assert recorded[0].delivered_at == NOW
    assert recorded[1].generation == 0


def test_execution_terminal_existing_delivery_is_audited_before_skip(monkeypatch) -> None:
    recorded = []
    _patch(monkeypatch, recorded)
    authorization = SimpleNamespace(
        quest_id=_registration().quest_id,
        execution_id="exe_" + "c" * 32,
        attempt_id="iat_" + "d" * 32,
    )
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.list_scientific_execution_authorizations",
        lambda _session: (authorization,),
    )
    item = SimpleNamespace(
        outbox_id="qto_" + "e" * 64,
        terminal_authority_sha256="e" * 64,
        execution_id=authorization.execution_id,
        attempt_id=authorization.attempt_id,
    )

    class _Terminal:
        def load_qualification_terminal_outbox_in_session(self, _session, **_kwargs):
            return item

    wakeup = ControllerWakeup(
        registration_id=_registration().registration_id,
        quest_id=_registration().quest_id,
        source_kind=ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX,
        source_key=item.outbox_id,
        source_sha256=item.terminal_authority_sha256,
    )
    task_id = controller_task_spec(manifest=_manifest(), wakeup=wakeup).task_id
    exact = ControllerDeliveryWrite.from_contract(
        registration_sha256=_registration().registration_sha256,
        wakeup=wakeup,
        task_id=task_id,
        delivered_at=NOW,
        execution_id=item.execution_id,
        attempt_id=item.attempt_id,
    )
    rebound = exact.model_copy(update={"task_id": "task-rctl-" + "f" * 32})
    monkeypatch.setattr(
        "aletheia.research_controller.dispatcher.get_controller_delivery_by_source",
        lambda *_args, **_kwargs: rebound,
    )

    with pytest.raises(ValueError, match="immutable source"):
        ExecutionTerminalOutboxDispatcher(
            terminal_outbox=_Terminal(), manifest=_manifest(), queue=_Queue()
        ).dispatch_once()

    assert recorded == []
