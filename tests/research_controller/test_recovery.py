from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

from aletheia.execution.allocator import (
    QualificationTerminalOutboxItem,
    VerifiedQualificationTerminalSource,
)
from aletheia.jobs.contracts import RetryPolicy
from aletheia.observations.store import (
    ContinuationReceiptWrite,
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    ObservationAdmissionWrite,
    ObservationValidationReceiptWrite,
    ProtocolCompilationWrite,
    ScientificExecutionAuthorizationWrite,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    ObservationAdmissionDisposition,
)
from aletheia.protocols.compiler import compile_protocol
from aletheia.research_controller import recovery as recovery_module
from aletheia.research_controller.contracts import (
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_task_spec,
    plan_recovery_tick,
)
from aletheia.research_controller.continuation import (
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    derive_continuation_v2,
)
from aletheia.research_controller.recovery import (
    ControllerRecoveryError,
    PostgreSQLControllerRecoveryAdapter,
)
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    ResearchStateGraph,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    ActionRejectedPayload,
    CharterActivatedPayload,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ObservationIncorporatedPayload,
    ResearchActionProposal,
    ResearchEvent,
)
from aletheia.research_store.store import ResearchReplayAudit

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
_OBSERVATION_FIXTURES = Path(__file__).resolve().parents[1] / "observations"
_CONTROLLER_FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
sys.path.insert(0, str(_OBSERVATION_FIXTURES))
sys.path.insert(0, str(_CONTROLLER_FIXTURES))
from fixtures import fixture_by_name  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    _blocked_receipt,
    _bridge_case,
    _commit_admission,
    _commit_validation,
    _issue_admission_decision,
    _raw_run,
    _validated_receipt,
)
from test_vertical_cut import _f9_enriched_grouped_fixture  # noqa: E402

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
QUEST_ID = "qst_" + "1" * 32
BRANCH_ID = "rbr_" + "1" * 32


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:recovery-v1",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        prepared_at=NOW,
    )


def _audit(*, with_authorized_action: bool) -> ResearchReplayAudit:
    protocol_scope = fixture_by_name("grouped_regression").request.protocol.graph_scope
    scope = ResearchScopeBinding.model_validate(protocol_scope.scope_binding)
    assert scope.quest_id == QUEST_ID
    assert protocol_scope.branch_id == BRANCH_ID
    charter_ref = KernelObjectRef(
        object_kind=KernelObjectKind.CHARTER,
        object_id="charter:recovery",
        object_sha256="7" * 64,
        quest_id=QUEST_ID,
    )
    genesis = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=1,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter_ref,
            root_branch_id=BRANCH_ID,
        ),
        command_sha256="8" * 64,
        principal_id="human:commissioner",
        authorization_receipt_sha256="9" * 64,
        committed_at=NOW,
    )
    if not with_authorized_action:
        state = ResearchStateGraph(
            quest_id=QUEST_ID,
            stream_version=1,
            tail_event_sha256=genesis.event_sha256,
            event_ids=(genesis.event_id,),
            event_sha256s=(genesis.event_sha256,),
            charter_ref=charter_ref,
            charter_history=(charter_ref,),
        )
        return ResearchReplayAudit(
            quest_id=QUEST_ID,
            scope_binding=scope,
            events=(genesis,),
            state=state,
            verified_snapshot_sha256s=(state.snapshot_sha256,),
        )

    question_ref = KernelObjectRef(
        object_kind=KernelObjectKind.QUESTION,
        object_id="question:recovery",
        object_sha256="a" * 64,
        quest_id=QUEST_ID,
    )
    action = ResearchActionProposal(
        action_id="action:recovery-measurement",
        quest_id=QUEST_ID,
        charter_ref=charter_ref,
        question_ref=question_ref,
        basis_tail_event_sha256=genesis.event_sha256,
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="Recover the exact authorized measurement action.",
        candidate_outcomes=("negative", "positive"),
        cost_receipt_sha256="b" * 64,
        risk_receipt_sha256="c" * 64,
        requested_authority_class="scientific-measurement",
        proposed_by_principal_id="model:planner",
        proposed_at=NOW + timedelta(seconds=1),
    )
    proposed = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=2,
        parent_event_sha256=genesis.event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(action_ref=action.object_ref, branch_id=BRANCH_ID),
        command_sha256="d" * 64,
        principal_id=action.proposed_by_principal_id,
        authorization_receipt_sha256="e" * 64,
        committed_at=NOW + timedelta(seconds=2),
    )
    authorized = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=3,
        parent_event_sha256=proposed.event_sha256,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(action_id=action.action_id, branch_id=BRANCH_ID),
        command_sha256="f" * 64,
        principal_id="human:action-authorizer",
        authorization_receipt_sha256="0" * 64,
        committed_at=NOW + timedelta(seconds=3),
    )
    state = ResearchStateGraph(
        quest_id=QUEST_ID,
        stream_version=3,
        tail_event_sha256=authorized.event_sha256,
        event_ids=(genesis.event_id, proposed.event_id, authorized.event_id),
        event_sha256s=(
            genesis.event_sha256,
            proposed.event_sha256,
            authorized.event_sha256,
        ),
        charter_ref=charter_ref,
        charter_history=(charter_ref,),
        actions=(
            ActionSnapshot(
                action_ref=action.object_ref,
                branch_id=BRANCH_ID,
                kind=action.kind,
                lifecycle=ActionLifecycle.AUTHORIZED,
                proposed_event_sha256=proposed.event_sha256,
                decided_event_sha256=authorized.event_sha256,
            ),
        ),
    )
    return ResearchReplayAudit(
        quest_id=QUEST_ID,
        scope_binding=scope,
        events=(genesis, proposed, authorized),
        state=state,
        verified_snapshot_sha256s=("1" * 64, "2" * 64, state.snapshot_sha256),
    )


def _registration(audit: ResearchReplayAudit) -> ResearchControllerRegistration:
    request = ResearchControllerLaunchRequest(
        program_id=audit.scope_binding.program_id,
        quest_id=audit.quest_id,
        idempotency_key="launch:recovery",
        expected_stream_version=audit.state.stream_version,
        expected_tail_event_sha256=audit.state.tail_event_sha256,
        expected_snapshot_sha256=audit.state.snapshot_sha256,
    )
    manifest = _manifest()
    return ResearchControllerRegistration(
        registration_id=request.registration_id,
        launch_request=request,
        controller_id=manifest.controller_id,
        controller_manifest_sha256=manifest.manifest_sha256,
        controller_principal_id=manifest.controller_key,
        registered_by_principal_id="owner:local",
        registered_at=NOW,
    )


def _wakeup(registration: ResearchControllerRegistration) -> ControllerWakeup:
    return ControllerWakeup(
        registration_id=registration.registration_id,
        quest_id=QUEST_ID,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key=registration.registration_id,
        source_sha256=registration.launch_request.request_sha256,
    )


class _Kernel:
    def __init__(self, audit: ResearchReplayAudit) -> None:
        self.audit = audit

    def audit_in_session(self, _session, quest_id, *, expected_scope_binding=None):
        assert quest_id == self.audit.quest_id
        assert expected_scope_binding is None
        return self.audit


class _Terminal:
    def load_verified_qualification_terminal_source(self, *, execution_id, attempt_id):
        raise AssertionError((execution_id, attempt_id))

    def load_qualification_terminal_outbox_in_session(self, _session, *, execution_id, attempt_id):
        raise AssertionError((execution_id, attempt_id))


class _TerminalItem:
    def __init__(
        self,
        item: QualificationTerminalOutboxItem,
        authorization: ScientificExecutionAuthorizationWrite,
    ) -> None:
        self.item = item
        self.authorization = authorization

    def load_verified_qualification_terminal_source(self, *, execution_id, attempt_id):
        assert (execution_id, attempt_id) == (
            self.item.execution_id,
            self.item.attempt_id,
        )
        return VerifiedQualificationTerminalSource(
            execution_id=execution_id,
            attempt_id=attempt_id,
            intent_sha256="1" * 64,
            qualification_bundle_sha256=self.authorization.qualification_bundle_sha256,
            qualification_grant_sha256=self.authorization.qualification_grant_sha256,
            qualification_admission_sha256="4" * 64,
            qualification_admitted_at=self.item.created_at,
            resource_reservation_sha256="5" * 64,
            resource_reserved_at=self.item.created_at,
            runtime_launch_sha256="6" * 64,
            runtime_launched_at=self.item.created_at,
            accepted_runtime_termination_sha256="7" * 64,
            outbox_id=self.item.outbox_id,
            terminal_authority_kind=self.item.terminal_authority_kind,
            terminal_authority_sha256=self.item.terminal_authority_sha256,
            payload_sha256=self.item.payload_sha256,
            outbox_created_at=self.item.created_at,
            lineage_evidence_sha256="8" * 64,
            verified_at=self.item.created_at,
        )

    def load_qualification_terminal_outbox_in_session(self, _session, *, execution_id, attempt_id):
        assert (execution_id, attempt_id) == (
            self.item.execution_id,
            self.item.attempt_id,
        )
        return self.item


@contextmanager
def _session_scope():
    yield object()


def _common_store_mocks(
    monkeypatch: pytest.MonkeyPatch,
    registration: ResearchControllerRegistration,
    *,
    wakeup: ControllerWakeup | None = None,
) -> None:
    delivered_wakeup = wakeup or _wakeup(registration)
    delivery = ControllerDeliveryWrite.from_contract(
        registration_sha256=registration.registration_sha256,
        wakeup=delivered_wakeup,
        task_id=controller_task_spec(
            manifest=_manifest(),
            wakeup=delivered_wakeup,
        ).task_id,
        delivered_at=NOW,
    )
    monkeypatch.setattr(recovery_module, "session_scope", _session_scope)
    monkeypatch.setattr(
        recovery_module,
        "get_controller_registration_by_quest",
        lambda _session, _quest_id: ControllerRegistrationWrite.from_contract(registration),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_controller_delivery_by_source",
        lambda _session, **_kwargs: delivery,
    )
    monkeypatch.setattr(
        recovery_module,
        "list_scientific_execution_authorizations",
        lambda _session, *, quest_id: (),
    )


def test_restart_rebuilds_missing_action_from_ledger_without_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(with_authorized_action=False)
    registration = _registration(audit)
    _common_store_mocks(monkeypatch, registration)

    first = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_Terminal(),
        manifest=_manifest(),
    ).load(_wakeup(registration))
    restarted = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_Terminal(),
        manifest=_manifest(),
    ).load(_wakeup(registration))

    assert restarted == first
    assert plan_recovery_tick(restarted).step is ControllerStep.PROPOSE_ACTION


def test_restart_recovers_accepted_compile_before_slot_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(with_authorized_action=True)
    registration = _registration(audit)
    _common_store_mocks(monkeypatch, registration)
    request = fixture_by_name("grouped_regression").request
    result = compile_protocol(request)
    assert result.report.accepted
    compilation = ProtocolCompilationWrite.from_contract(
        quest_id=QUEST_ID,
        action_sha256=audit.state.actions[0].action_ref.object_sha256,
        request=request,
        result=result,
        registered_at=NOW,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: compilation,
    )

    projection = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_Terminal(),
        manifest=_manifest(),
    ).load(_wakeup(registration))

    assert projection.action_authorized is True
    assert projection.scientific_slot_id is None
    assert plan_recovery_tick(projection).step is ControllerStep.REGISTER_EXECUTION


def test_recovery_rejects_rebound_kernel_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(with_authorized_action=False)
    registration = _registration(audit)
    wakeup = ControllerWakeup(
        registration_id=registration.registration_id,
        quest_id=QUEST_ID,
        source_kind=ControllerWakeupKind.KERNEL_OUTBOX,
        source_key="rko_" + "f" * 32,
        source_sha256=audit.events[0].event_sha256,
        source_stream_version=1,
    )
    _common_store_mocks(monkeypatch, registration, wakeup=wakeup)

    with pytest.raises(ControllerRecoveryError, match="absent from the audited"):
        PostgreSQLControllerRecoveryAdapter(
            kernel_store=_Kernel(audit),
            terminal_outbox=_Terminal(),
            manifest=_manifest(),
        ).load(wakeup)


def test_recovery_requires_the_exact_durable_delivery_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(with_authorized_action=False)
    registration = _registration(audit)
    _common_store_mocks(monkeypatch, registration)
    monkeypatch.setattr(
        recovery_module,
        "get_controller_delivery_by_source",
        lambda _session, **_kwargs: None,
    )

    with pytest.raises(ControllerRecoveryError, match="durable delivery receipt"):
        PostgreSQLControllerRecoveryAdapter(
            kernel_store=_Kernel(audit),
            terminal_outbox=_Terminal(),
            manifest=_manifest(),
        ).load(_wakeup(registration))


def test_recovery_recomputes_compilation_instead_of_trusting_receipt_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(with_authorized_action=True)
    registration = _registration(audit)
    _common_store_mocks(monkeypatch, registration)
    request = fixture_by_name("grouped_regression").request
    foreign_request = request.model_copy(update={"compiler_implementation_sha256": "f" * 64})
    foreign_result = compile_protocol(foreign_request)
    internally_hashed_but_noncanonical = ProtocolCompilationWrite.from_contract(
        quest_id=QUEST_ID,
        action_sha256=audit.state.actions[0].action_ref.object_sha256,
        request=request,
        result=foreign_result,
        registered_at=NOW,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: internally_hashed_but_noncanonical,
    )

    with pytest.raises(ControllerRecoveryError, match="compilation is invalid"):
        PostgreSQLControllerRecoveryAdapter(
            kernel_store=_Kernel(audit),
            terminal_outbox=_Terminal(),
            manifest=_manifest(),
        ).load(_wakeup(registration))


def test_latest_rejected_action_does_not_hide_an_older_eligible_action() -> None:
    audit = _audit(with_authorized_action=True)
    current = audit.state.actions[0]
    rejected_ref = KernelObjectRef(
        object_kind=KernelObjectKind.ACTION,
        object_id="action:recovery-rejected",
        object_sha256="6" * 64,
        quest_id=QUEST_ID,
    )
    proposed = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=4,
        parent_event_sha256=audit.events[-1].event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(action_ref=rejected_ref, branch_id=BRANCH_ID),
        command_sha256="5" * 64,
        principal_id="model:planner",
        authorization_receipt_sha256="4" * 64,
        committed_at=NOW + timedelta(seconds=4),
    )
    rejected = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=5,
        parent_event_sha256=proposed.event_sha256,
        event_type=EventType.ACTION_REJECTED,
        payload=ActionRejectedPayload(
            action_id=rejected_ref.object_id,
            branch_id=BRANCH_ID,
            reason_codes=("not_selected",),
        ),
        command_sha256="3" * 64,
        principal_id="human:action-authorizer",
        authorization_receipt_sha256="2" * 64,
        committed_at=NOW + timedelta(seconds=5),
    )
    rejected_snapshot = ActionSnapshot(
        action_ref=rejected_ref,
        branch_id=BRANCH_ID,
        kind=ActionKind.DISCRIMINATE,
        lifecycle=ActionLifecycle.REJECTED,
        proposed_event_sha256=proposed.event_sha256,
        decided_event_sha256=rejected.event_sha256,
    )
    state = audit.state.model_copy(
        update={
            "stream_version": 5,
            "tail_event_sha256": rejected.event_sha256,
            "event_ids": (*audit.state.event_ids, proposed.event_id, rejected.event_id),
            "event_sha256s": (
                *audit.state.event_sha256s,
                proposed.event_sha256,
                rejected.event_sha256,
            ),
            "actions": (current, rejected_snapshot),
        }
    )
    changed = audit.model_copy(
        update={"events": (*audit.events, proposed, rejected), "state": state}
    )

    assert recovery_module._latest_action(changed) == current


def test_latest_structural_transition_is_a_barrier_to_older_continuations() -> None:
    audit = _audit(with_authorized_action=True)
    structural_ref = KernelObjectRef(
        object_kind=KernelObjectKind.ACTION,
        object_id="action:recovery-fork-transition",
        object_sha256="7" * 64,
        quest_id=QUEST_ID,
    )
    proposed = ResearchEvent(
        quest_id=QUEST_ID,
        sequence=4,
        parent_event_sha256=audit.events[-1].event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(action_ref=structural_ref, branch_id=BRANCH_ID),
        command_sha256="6" * 64,
        principal_id="model:planner",
        authorization_receipt_sha256="5" * 64,
        committed_at=NOW + timedelta(seconds=4),
    )
    structural = ActionSnapshot(
        action_ref=structural_ref,
        branch_id=BRANCH_ID,
        kind=ActionKind.FORK,
        lifecycle=ActionLifecycle.APPLIED,
        proposed_event_sha256=proposed.event_sha256,
        decided_event_sha256="4" * 64,
        observation_evidence_ref=None,
    )
    changed = audit.model_copy(
        update={
            "events": (*audit.events, proposed),
            "state": audit.state.model_copy(update={"actions": (*audit.state.actions, structural)}),
        }
    )

    assert recovery_module._latest_action(changed) is None


def test_terminal_wakeup_recovers_its_exact_action_not_a_newer_ledger_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    binding = case.binding
    proposed = binding.action_proposed_event
    authorized = binding.action_authorized_event
    old_snapshot = ActionSnapshot(
        action_ref=binding.action.object_ref,
        branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        kind=binding.action.kind,
        lifecycle=ActionLifecycle.AUTHORIZED,
        proposed_event_sha256=proposed.event_sha256,
        decided_event_sha256=authorized.event_sha256,
    )
    newer_ref = KernelObjectRef(
        object_kind=KernelObjectKind.ACTION,
        object_id="action:newer-ledger-action",
        object_sha256="1" * 64,
        quest_id=binding.action.quest_id,
    )
    newer_event = ResearchEvent(
        quest_id=binding.action.quest_id,
        sequence=authorized.sequence + 1,
        parent_event_sha256=authorized.event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=newer_ref,
            branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        ),
        command_sha256="2" * 64,
        principal_id="model:newer-planner",
        authorization_receipt_sha256="3" * 64,
        committed_at=authorized.committed_at + timedelta(seconds=1),
    )
    newer_snapshot = ActionSnapshot(
        action_ref=newer_ref,
        branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        kind=ActionKind.DISCRIMINATE,
        lifecycle=ActionLifecycle.PROPOSED,
        proposed_event_sha256=newer_event.event_sha256,
    )
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=newer_event.sequence,
        tail_event_sha256=newer_event.event_sha256,
        event_ids=(proposed.event_id, authorized.event_id, newer_event.event_id),
        event_sha256s=(
            proposed.event_sha256,
            authorized.event_sha256,
            newer_event.event_sha256,
        ),
        charter_ref=binding.action.charter_ref,
        charter_history=(binding.action.charter_ref,),
        actions=(old_snapshot, newer_snapshot),
    )
    audit = ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        events=(proposed, authorized, newer_event),
        state=state,
        verified_snapshot_sha256s=(state.snapshot_sha256,),
    )
    registration = _registration(audit)
    raw_run = _raw_run(case)
    accepted_terminal = raw_run.accepted_terminal_submission
    intent = case.authorization.message.qualification_bundle.intent
    terminal = QualificationTerminalOutboxItem(
        outbox_id=f"qto_{accepted_terminal.accepted_terminal_submission_sha256}",
        terminal_authority_kind="accepted_terminal_submission",
        terminal_authority_sha256=(accepted_terminal.accepted_terminal_submission_sha256),
        execution_id=intent.execution_id,
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        payload=accepted_terminal,
        payload_sha256=accepted_terminal.accepted_terminal_submission_sha256,
        created_at=raw_run.assembled_at,
    )
    wakeup = ControllerWakeup(
        registration_id=registration.registration_id,
        quest_id=audit.quest_id,
        source_kind=ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX,
        source_key=terminal.outbox_id,
        source_sha256=terminal.terminal_authority_sha256,
    )
    delivery = ControllerDeliveryWrite.from_contract(
        registration_sha256=registration.registration_sha256,
        wakeup=wakeup,
        task_id=controller_task_spec(manifest=_manifest(), wakeup=wakeup).task_id,
        delivered_at=raw_run.assembled_at,
        execution_id=terminal.execution_id,
        attempt_id=terminal.attempt_id,
    )
    compilation = ProtocolCompilationWrite.from_contract(
        quest_id=audit.quest_id,
        action_sha256=binding.action.object_sha256,
        request=binding.compilation_request,
        result=binding.compilation_result,
        registered_at=NOW,
    )
    authorization = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at,
    )

    monkeypatch.setattr(recovery_module, "session_scope", _session_scope)
    monkeypatch.setattr(
        recovery_module,
        "get_controller_registration_by_quest",
        lambda _session, _quest_id: ControllerRegistrationWrite.from_contract(registration),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_controller_delivery_by_source",
        lambda _session, **_kwargs: delivery,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: compilation,
    )
    monkeypatch.setattr(
        recovery_module,
        "list_scientific_execution_authorizations",
        lambda _session, **_kwargs: (authorization,),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_validation_receipt_by_slot",
        lambda _session, **_kwargs: None,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_admission_by_slot",
        lambda _session, **_kwargs: None,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_continuation_receipt_by_slot",
        lambda _session, **_kwargs: None,
    )

    projection = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_TerminalItem(terminal, authorization),
        manifest=_manifest(),
    ).load(wakeup)

    assert projection.action_sha256 == binding.action.object_sha256
    assert projection.action_sha256 != newer_ref.object_sha256
    assert projection.execution_terminal_observed is True
    assert plan_recovery_tick(projection).step is ControllerStep.COMMIT_VALIDATION


@pytest.mark.parametrize(
    ("terminal_disposition", "expected_blocker"),
    (
        ("process_failed", "observation_validation_blocked_execution"),
        ("timeout", "observation_validation_blocked_execution"),
        ("scientific_rejected", "observation_validation_rejected_scientific"),
    ),
)
def test_nonconfirmation_validation_blocks_instead_of_looping_admission(
    monkeypatch: pytest.MonkeyPatch,
    terminal_disposition: str,
    expected_blocker: str,
) -> None:
    case = _bridge_case()
    binding = case.binding
    proposed = binding.action_proposed_event
    authorized = binding.action_authorized_event
    action_snapshot = ActionSnapshot(
        action_ref=binding.action.object_ref,
        branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        kind=binding.action.kind,
        lifecycle=ActionLifecycle.AUTHORIZED,
        proposed_event_sha256=proposed.event_sha256,
        decided_event_sha256=authorized.event_sha256,
    )
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=authorized.sequence,
        tail_event_sha256=authorized.event_sha256,
        event_ids=(proposed.event_id, authorized.event_id),
        event_sha256s=(proposed.event_sha256, authorized.event_sha256),
        charter_ref=binding.action.charter_ref,
        charter_history=(binding.action.charter_ref,),
        actions=(action_snapshot,),
    )
    audit = ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        events=(proposed, authorized),
        state=state,
        verified_snapshot_sha256s=(state.snapshot_sha256,),
    )
    registration = _registration(audit)
    wakeup = _wakeup(registration)
    delivery = ControllerDeliveryWrite.from_contract(
        registration_sha256=registration.registration_sha256,
        wakeup=wakeup,
        task_id=controller_task_spec(manifest=_manifest(), wakeup=wakeup).task_id,
        delivered_at=NOW,
    )
    compilation = ProtocolCompilationWrite.from_contract(
        quest_id=audit.quest_id,
        action_sha256=binding.action.object_sha256,
        request=binding.compilation_request,
        result=binding.compilation_result,
        registered_at=NOW,
    )
    authorization = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at,
    )
    receipt = (
        _validated_receipt(
            case,
            disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
            blocker_codes=("validation_campaign:scientific_rejection",),
        )
        if terminal_disposition == "scientific_rejected"
        else _blocked_receipt(case, terminal_disposition)
    )
    committed_validation = _commit_validation(case, receipt)
    validation = ObservationValidationReceiptWrite.from_contract(
        committed_validation,
        quest_id=audit.quest_id,
    )
    raw_run = receipt.message.raw_run
    accepted_terminal = raw_run.accepted_terminal_submission
    terminal = QualificationTerminalOutboxItem(
        outbox_id=f"qto_{accepted_terminal.accepted_terminal_submission_sha256}",
        terminal_authority_kind="accepted_terminal_submission",
        terminal_authority_sha256=(accepted_terminal.accepted_terminal_submission_sha256),
        execution_id=authorization.execution_id,
        attempt_id=authorization.attempt_id,
        payload=accepted_terminal,
        payload_sha256=accepted_terminal.accepted_terminal_submission_sha256,
        created_at=raw_run.assembled_at,
    )

    monkeypatch.setattr(recovery_module, "session_scope", _session_scope)
    monkeypatch.setattr(
        recovery_module,
        "get_controller_registration_by_quest",
        lambda _session, _quest_id: ControllerRegistrationWrite.from_contract(registration),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_controller_delivery_by_source",
        lambda _session, **_kwargs: delivery,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: compilation,
    )
    monkeypatch.setattr(
        recovery_module,
        "list_scientific_execution_authorizations",
        lambda _session, **_kwargs: (authorization,),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_validation_receipt_by_slot",
        lambda _session, **_kwargs: validation,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_admission_by_slot",
        lambda _session, **_kwargs: None,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_continuation_receipt_by_slot",
        lambda _session, **_kwargs: None,
    )

    projection = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_TerminalItem(terminal, authorization),
        manifest=_manifest(),
    ).load(wakeup)

    assert projection.validation_committed is True
    assert projection.admission_committed is False
    assert projection.blocker_codes == (expected_blocker,)
    assert plan_recovery_tick(projection).step is ControllerStep.BLOCKED


def test_restart_rebuilds_complete_admitted_continuation_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enriched = _f9_enriched_grouped_fixture()
    original_fixture_by_name = fixture_by_name

    def enriched_fixture(name: str):
        if name == "grouped_regression":
            return enriched
        return original_fixture_by_name(name)

    monkeypatch.setattr("test_runtime_contracts.fixture_by_name", enriched_fixture)
    case = _bridge_case()
    binding = case.binding
    validation = _validated_receipt(case, outcome_bin_id="outcome.negative")
    decision, committed_validation = _issue_admission_decision(
        case,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    committed_admission = _commit_admission(case, decision)
    world_model = binding.compilation_request.protocol.world_model
    assert world_model is not None
    payload = ObservationIncorporatedPayload(
        branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        action_id=binding.action.action_id,
        scientific_slot_id=decision.message.scientific_slot_id,
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        scientific_observation_sha256=decision.message.admitted_observation_sha256,
        outcome=validation.message.outcome.value,
        source_world_model_sha256=world_model.world_model_sha256,
    )
    observation_event = ResearchEvent(
        quest_id=binding.action.quest_id,
        sequence=binding.action_authorized_event.sequence + 1,
        parent_event_sha256=binding.action_authorized_event.event_sha256,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=payload,
        command_sha256="1" * 64,
        principal_id="kernel:observation-authorizer",
        authorization_receipt_sha256="2" * 64,
        committed_at=binding.action_authorized_event.committed_at + timedelta(seconds=1),
    )
    action_snapshot = ActionSnapshot(
        action_ref=binding.action.object_ref,
        branch_id=payload.branch_id,
        kind=binding.action.kind,
        lifecycle=ActionLifecycle.APPLIED,
        proposed_event_sha256=binding.action_proposed_event.event_sha256,
        decided_event_sha256=observation_event.event_sha256,
        observation_evidence_ref=payload.evidence_ref,
    )
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=observation_event.sequence,
        tail_event_sha256=observation_event.event_sha256,
        event_ids=(
            binding.action_proposed_event.event_id,
            binding.action_authorized_event.event_id,
            observation_event.event_id,
        ),
        event_sha256s=(
            binding.action_proposed_event.event_sha256,
            binding.action_authorized_event.event_sha256,
            observation_event.event_sha256,
        ),
        charter_ref=binding.action.charter_ref,
        charter_history=(binding.action.charter_ref,),
        actions=(action_snapshot,),
        evidence_refs=(payload.evidence_ref,),
    )
    audit = ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        events=(
            binding.action_proposed_event,
            binding.action_authorized_event,
            observation_event,
        ),
        state=state,
        verified_snapshot_sha256s=("3" * 64, "4" * 64, state.snapshot_sha256),
    )
    registration = _registration(audit)
    wakeup = _wakeup(registration)
    delivery = ControllerDeliveryWrite.from_contract(
        registration_sha256=registration.registration_sha256,
        wakeup=wakeup,
        task_id=controller_task_spec(manifest=_manifest(), wakeup=wakeup).task_id,
        delivered_at=NOW,
    )
    compilation = ProtocolCompilationWrite.from_contract(
        quest_id=audit.quest_id,
        action_sha256=binding.action.object_sha256,
        request=binding.compilation_request,
        result=binding.compilation_result,
        registered_at=NOW,
    )
    authorization = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at,
    )
    validation_write = ObservationValidationReceiptWrite.from_contract(
        committed_validation,
        quest_id=audit.quest_id,
    )
    admission_write = ObservationAdmissionWrite.from_contract(
        committed_admission,
        quest_id=audit.quest_id,
        incorporated_event_sequence=observation_event.sequence,
        incorporated_event_sha256=observation_event.event_sha256,
        incorporated_event_type=EventType.OBSERVATION_INCORPORATED.value,
    )
    predictions = tuple(sorted(world_model.predictions, key=lambda item: item.hypothesis_sha256))
    artifact_binding = case.authorization.message.scientific_observation_artifact_binding
    observation = ScientificObservationProjection(
        scientific_slot_id=payload.scientific_slot_id,
        committed_admission_sha256=payload.committed_admission_sha256,
        scientific_observation_sha256=payload.scientific_observation_sha256,
        source_world_model_sha256=payload.source_world_model_sha256,
        outcome=validation.message.outcome,
        observable_spec_sha256=artifact_binding.observable.observable_sha256,
        measurement_protocol_sha256=(
            binding.compilation_request.protocol.method.method_contract_sha256
        ),
        outcome_space_sha256=(
            binding.compilation_request.protocol.analysis_plan.outcome_space_sha256
        ),
        observed_outcome_sha256="5" * 64,
    )
    assessments = tuple(
        HypothesisPredictionAssessment(
            hypothesis_sha256=item.hypothesis_sha256,
            prediction_sha256=item.prediction_sha256,
            prediction_fit=PredictionFit.OUT_OF_SUPPORT,
            fit_rule_sha256="6" * 64,
            assessment_artifact_sha256=(f"{index + 7:x}" * 64)[:64],
        )
        for index, item in enumerate(predictions)
    )
    continuation = derive_continuation_v2(
        world_model=world_model,
        observation=observation,
        assessments=assessments,
    )
    continuation_write = ContinuationReceiptWrite.from_contract(
        continuation,
        quest_id=audit.quest_id,
        action_sha256=binding.action.object_sha256,
        observation=observation,
        recorded_at=NOW + timedelta(hours=1),
    )
    raw_run = validation.message.raw_run
    terminal = QualificationTerminalOutboxItem(
        outbox_id=(
            f"qto_{raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256}"
        ),
        terminal_authority_kind="accepted_terminal_submission",
        terminal_authority_sha256=(
            raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256
        ),
        execution_id=authorization.execution_id,
        attempt_id=authorization.attempt_id,
        payload=raw_run.accepted_terminal_submission,
        payload_sha256=(raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256),
        created_at=raw_run.assembled_at,
    )

    monkeypatch.setattr(recovery_module, "session_scope", _session_scope)
    monkeypatch.setattr(
        recovery_module,
        "get_controller_registration_by_quest",
        lambda _session, _quest_id: ControllerRegistrationWrite.from_contract(registration),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_controller_delivery_by_source",
        lambda _session, **_kwargs: delivery,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: compilation,
    )
    monkeypatch.setattr(
        recovery_module,
        "list_scientific_execution_authorizations",
        lambda _session, **_kwargs: (authorization,),
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_validation_receipt_by_slot",
        lambda _session, **_kwargs: validation_write,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_observation_admission_by_slot",
        lambda _session, **_kwargs: admission_write,
    )
    monkeypatch.setattr(
        recovery_module,
        "get_continuation_receipt_by_slot",
        lambda _session, **_kwargs: continuation_write,
    )

    first = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_TerminalItem(terminal, authorization),
        manifest=_manifest(),
    ).load(wakeup)
    restarted = PostgreSQLControllerRecoveryAdapter(
        kernel_store=_Kernel(audit),
        terminal_outbox=_TerminalItem(terminal, authorization),
        manifest=_manifest(),
    ).load(wakeup)

    assert restarted == first
    assert restarted.observation_incorporated is True
    assert restarted.continuation_committed is True
    assert plan_recovery_tick(restarted).step is ControllerStep.PROPOSE_FOLLOWUP
