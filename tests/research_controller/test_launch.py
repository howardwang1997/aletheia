from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from aletheia.jobs.contracts import RetryPolicy
from aletheia.observations.store import ControllerRegistrationWrite
from aletheia.research_controller.contracts import (
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchReceipt,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
)
from aletheia.research_controller.launch import (
    ControllerLaunchConflict,
    ResearchControllerLauncher,
)
from aletheia.research_controller.persistence import PostgreSQLControllerLaunchAdapter
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.reducer import ResearchStateGraph
from aletheia.research_kernel.schemas import (
    CharterActivatedPayload,
    CharterRevisedPayload,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ResearchEvent,
)
from aletheia.research_store.store import ResearchReplayAudit

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _audit() -> ResearchReplayAudit:
    quest_id = "qst_" + "a" * 32
    charter_ref = KernelObjectRef(
        object_kind=KernelObjectKind.CHARTER,
        object_id="charter:controller-launch",
        object_sha256="b" * 64,
        quest_id=quest_id,
    )
    event = ResearchEvent(
        quest_id=quest_id,
        sequence=1,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter_ref,
            root_branch_id="rbr_" + "c" * 32,
        ),
        command_sha256="d" * 64,
        principal_id="human:commissioner",
        authorization_receipt_sha256="e" * 64,
        committed_at=NOW,
    )
    state = ResearchStateGraph(
        quest_id=quest_id,
        stream_version=1,
        tail_event_sha256=event.event_sha256,
        event_ids=(event.event_id,),
        event_sha256s=(event.event_sha256,),
        charter_ref=charter_ref,
        charter_history=(charter_ref,),
    )
    return ResearchReplayAudit(
        quest_id=quest_id,
        scope_binding=ResearchScopeBinding(quest_id=quest_id, program_id="prg_" + "f" * 32),
        events=(event,),
        state=state,
        verified_snapshot_sha256s=(state.snapshot_sha256,),
    )


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


class _Kernel:
    def __init__(self, audit):
        self.audit_value = audit
        self.calls = 0

    def audit(self, quest_id):
        self.calls += 1
        assert quest_id == self.audit_value.quest_id
        return self.audit_value


class _Persistence:
    def __init__(self):
        self.calls = []

    def register_launch(self, *, request, manifest, registered_by_principal_id):
        self.calls.append((request, manifest, registered_by_principal_id))
        registration = ResearchControllerRegistration(
            registration_id=request.registration_id,
            launch_request=request,
            controller_id=manifest.controller_id,
            controller_manifest_sha256=manifest.manifest_sha256,
            controller_principal_id=manifest.controller_key,
            registered_by_principal_id=registered_by_principal_id,
            registered_at=NOW,
        )
        wakeup = ControllerWakeup(
            registration_id=registration.registration_id,
            quest_id=request.quest_id,
            source_kind=ControllerWakeupKind.LAUNCH,
            source_key=request.registration_id,
            source_sha256=request.request_sha256,
        )
        return ResearchControllerLaunchReceipt(
            registration=registration,
            wakeup=wakeup,
            durable_task_id="task-rctl-" + "7" * 32,
            created=True,
        )


def _request(audit, **changes):
    values = {
        "program_id": audit.scope_binding.program_id,
        "quest_id": audit.quest_id,
        "idempotency_key": "launch:one",
        "expected_stream_version": len(audit.events),
        "expected_tail_event_sha256": audit.events[-1].event_sha256,
        "expected_snapshot_sha256": audit.state.snapshot_sha256,
    }
    values.update(changes)
    return ResearchControllerLaunchRequest(**values)


def test_launch_audits_exact_head_before_writing() -> None:
    audit = _audit()
    kernel = _Kernel(audit)
    persistence = _Persistence()
    launcher = ResearchControllerLauncher(
        kernel_store=kernel, manifest=_manifest(), persistence=persistence
    )
    receipt = launcher.launch(_request(audit), registered_by_principal_id="owner:local")
    assert kernel.calls == 1
    assert len(persistence.calls) == 1
    assert (
        receipt.registration.launch_request.expected_snapshot_sha256 == audit.state.snapshot_sha256
    )
    assert receipt.registration.scientific_checkpoint_created is False


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"expected_stream_version": 99}, "stream version"),
        ({"expected_tail_event_sha256": "8" * 64}, "tail event"),
        ({"expected_snapshot_sha256": "9" * 64}, "snapshot"),
        ({"program_id": "prg_" + "a" * 32}, "another Program"),
    ),
)
def test_stale_or_cross_scope_launch_never_reaches_persistence(change, message) -> None:
    audit = _audit()
    persistence = _Persistence()
    launcher = ResearchControllerLauncher(
        kernel_store=_Kernel(audit), manifest=_manifest(), persistence=persistence
    )
    with pytest.raises(ControllerLaunchConflict, match=message):
        launcher.launch(_request(audit, **change), registered_by_principal_id="owner:local")
    assert persistence.calls == []


def test_launch_rejects_internally_inconsistent_audit_projection() -> None:
    audit = _audit()
    inconsistent = audit.model_copy(update={"verified_snapshot_sha256s": ()})
    persistence = _Persistence()
    launcher = ResearchControllerLauncher(
        kernel_store=_Kernel(inconsistent), manifest=_manifest(), persistence=persistence
    )

    with pytest.raises(ControllerLaunchConflict, match="verified custody"):
        launcher.launch(_request(audit), registered_by_principal_id="owner:local")

    assert persistence.calls == []


def test_postgresql_adapter_reaudits_under_the_registration_transaction(
    monkeypatch,
) -> None:
    first = _audit()
    session = object()

    class _TransactionalKernel:
        def __init__(self) -> None:
            self.calls = []

        def audit_in_session(self, supplied_session, quest_id):
            self.calls.append((supplied_session, quest_id))
            return first

    class _Queue:
        def enqueue_in_session(self, *_args, **_kwargs):
            raise AssertionError("a stale locked audit must fail before enqueue")

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr("aletheia.research_controller.persistence.session_scope", _scope)
    kernel = _TransactionalKernel()
    adapter = PostgreSQLControllerLaunchAdapter(kernel_store=kernel, queue=_Queue())

    with pytest.raises(ControllerLaunchConflict, match="stream version is stale"):
        adapter.register_launch(
            request=_request(first, expected_stream_version=len(first.events) + 1),
            manifest=_manifest(),
            registered_by_principal_id="owner:local",
        )

    assert kernel.calls == [(session, first.quest_id)]


def test_launch_rejects_head_that_moves_between_outer_audit_and_registration(
    monkeypatch,
) -> None:
    first = _audit()
    revised_charter = first.state.charter_ref.model_copy(update={"object_sha256": "a" * 64})
    revision = ResearchEvent(
        quest_id=first.quest_id,
        sequence=2,
        parent_event_sha256=first.events[-1].event_sha256,
        event_type=EventType.CHARTER_REVISED,
        payload=CharterRevisedPayload(charter_ref=revised_charter),
        command_sha256="9" * 64,
        principal_id="human:commissioner",
        authorization_receipt_sha256="8" * 64,
        committed_at=NOW,
    )
    moved_state = ResearchStateGraph(
        quest_id=first.quest_id,
        stream_version=2,
        tail_event_sha256=revision.event_sha256,
        event_ids=(first.events[0].event_id, revision.event_id),
        event_sha256s=(first.events[0].event_sha256, revision.event_sha256),
        charter_ref=revised_charter,
        charter_history=(first.state.charter_ref, revised_charter),
    )
    moved = ResearchReplayAudit(
        quest_id=first.quest_id,
        scope_binding=first.scope_binding,
        events=(first.events[0], revision),
        state=moved_state,
        verified_snapshot_sha256s=(
            first.state.snapshot_sha256,
            moved_state.snapshot_sha256,
        ),
    )
    session = object()

    class _RacingKernel:
        def audit(self, quest_id):
            assert quest_id == first.quest_id
            return first

        def audit_in_session(self, supplied_session, quest_id):
            assert supplied_session is session
            assert quest_id == first.quest_id
            return moved

    class _Queue:
        def enqueue_in_session(self, *_args, **_kwargs):
            raise AssertionError("the moved head must fail before enqueue")

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr("aletheia.research_controller.persistence.session_scope", _scope)
    kernel = _RacingKernel()
    adapter = PostgreSQLControllerLaunchAdapter(kernel_store=kernel, queue=_Queue())
    launcher = ResearchControllerLauncher(
        kernel_store=kernel,
        manifest=_manifest(),
        persistence=adapter,
    )

    with pytest.raises(ControllerLaunchConflict, match="stream version is stale"):
        launcher.launch(
            _request(first),
            registered_by_principal_id="owner:local",
        )


def test_postgresql_adapter_rejects_exact_request_replay_by_another_principal(
    monkeypatch,
) -> None:
    audit = _audit()
    request = _request(audit)
    manifest = _manifest()
    registration = ResearchControllerRegistration(
        registration_id=request.registration_id,
        launch_request=request,
        controller_id=manifest.controller_id,
        controller_manifest_sha256=manifest.manifest_sha256,
        controller_principal_id=manifest.controller_key,
        registered_by_principal_id="owner:original",
        registered_at=NOW,
    )
    write = ControllerRegistrationWrite.from_contract(registration)

    class _TransactionalKernel:
        def audit_in_session(self, _session, _quest_id):
            return audit

    class _Queue:
        def enqueue_in_session(self, *_args, **_kwargs):
            raise AssertionError("a cross-principal replay must fail before enqueue")

    @contextmanager
    def _scope():
        yield object()

    monkeypatch.setattr("aletheia.research_controller.persistence.session_scope", _scope)
    monkeypatch.setattr(
        "aletheia.research_controller.persistence.get_controller_registration_by_launch_request",
        lambda *_args, **_kwargs: write,
    )
    monkeypatch.setattr(
        "aletheia.research_controller.persistence.get_controller_registration_by_quest",
        lambda *_args, **_kwargs: write,
    )
    adapter = PostgreSQLControllerLaunchAdapter(kernel_store=_TransactionalKernel(), queue=_Queue())

    with pytest.raises(ControllerLaunchConflict, match="exact retry differs"):
        adapter.register_launch(
            request=request,
            manifest=manifest,
            registered_by_principal_id="operator:other",
        )
