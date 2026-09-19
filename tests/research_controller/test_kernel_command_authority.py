from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.research_controller.action_proposals import materialize_action_proposal
from aletheia.research_controller.contracts import (
    TRANSITION_COMMIT_PENDING_BLOCKER,
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    plan_recovery_tick,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
)
from aletheia.research_controller.kernel_authority import (
    ControllerKernelPolicyAssignment,
    ExactActionKernelAuthority,
    ExactAdmissionKernelAuthority,
    ExactTransitionKernelAuthority,
    KernelCommandAuthorityError,
    continuation_receipt_evidence_ref,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    DedicatedControllerStepExecutor,
)
from aletheia.research_kernel.commands import (
    ResearchCommandProposal,
    ResearchScopeBinding,
    required_authorization_role,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationKey,
    ResearchAuthorizationRole,
    ed25519_key_id,
    ed25519_public_key_hex,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    EventType,
    EvidenceKind,
    EvidenceRef,
    ForkCommittedPayload,
    ForkDirective,
    ObservationIncorporatedPayload,
    ResearchEvent,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
)

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (_TESTS / "research_controller", _TESTS / "research_kernel"):
    if str(_fixture_dir) not in sys.path:
        sys.path.insert(0, str(_fixture_dir))

from test_action_proposals import (  # noqa: E402
    BRANCH_ID,
    NOW,
    QUEST_ID,
    SLOT_ID,
    _binding as _proposal_binding,
    _draft,
    _request,
    _sha,
)
from test_commands import (  # noqa: E402
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
    _role_key as command_role_key,
)
from test_step_executor import (  # noqa: E402
    WORKER_PROCESS_PRINCIPAL,
    _adapter_set,
    _adapters,
    _controller_manifest,
)


def _assignment_fixture(
    role: ResearchAuthorizationRole = ResearchAuthorizationRole.ORDINARY,
) -> tuple[object, object, ControllerKernelPolicyAssignment]:
    trust_root, policy = command_authority(quest_id=QUEST_ID)
    key = command_role_key(policy, role)
    assignment = ControllerKernelPolicyAssignment(
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        authorization_policy=policy,
    )
    return trust_root, key, assignment


def _action_authority(
    role: ResearchAuthorizationRole = ResearchAuthorizationRole.ORDINARY,
) -> ExactActionKernelAuthority:
    trust_root, key, assignment = _assignment_fixture(role)
    return ExactActionKernelAuthority(
        trust_root=trust_root,
        assignments=(assignment,),
        authorization_key_id=key.key_id,
        private_key=COMMAND_PRIVATE_KEYS[role],
    )


def _transition_authority(
    role: ResearchAuthorizationRole = ResearchAuthorizationRole.ORDINARY,
) -> ExactTransitionKernelAuthority:
    trust_root, key, assignment = _assignment_fixture(role)
    return ExactTransitionKernelAuthority(
        trust_root=trust_root,
        assignments=(assignment,),
        authorization_key_id=key.key_id,
        private_key=COMMAND_PRIVATE_KEYS[role],
    )


# The admission signer's second ORDINARY key speaks with the ACTION_PROPOSAL
# binding principal itself (contradiction #10 remedy a): the bridge requires
# the ACTION_PROPOSED principal to equal the proposer while the paired
# ACTION_AUTHORIZED stays with the separate action authority.
_ADMISSION_PRIVATE = b"\x25" * 32
_ADMISSION_PRINCIPAL = "service:action-proposal"


def _admission_extra_key() -> ResearchAuthorizationKey:
    public = ed25519_public_key_hex(_ADMISSION_PRIVATE)
    return ResearchAuthorizationKey(
        key_id=ed25519_key_id(public),
        principal_id=_ADMISSION_PRINCIPAL,
        role=ResearchAuthorizationRole.ORDINARY,
        public_key_ed25519_hex=public,
        valid_from=datetime(2026, 8, 23, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )


def _admission_authority() -> ExactAdmissionKernelAuthority:
    trust_root, policy = command_authority(
        quest_id=QUEST_ID, extra_keys=(_admission_extra_key(),)
    )
    key = next(
        item for item in policy.keys if item.principal_id == _ADMISSION_PRINCIPAL
    )
    assignment = ControllerKernelPolicyAssignment(
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        authorization_policy=policy,
    )
    return ExactAdmissionKernelAuthority(
        trust_root=trust_root,
        assignments=(assignment,),
        authorization_key_id=key.key_id,
        private_key=_ADMISSION_PRIVATE,
    )


def _submission(step: ControllerStep):
    request = _request(step)
    return materialize_action_proposal(
        request=request,
        draft=_draft(request),
        authority_binding=_proposal_binding(),
        submitted_at=NOW + timedelta(seconds=2),
    )


def _authorization_proposal(
    submission,
    *,
    expected_stream_version: int,
    expected_tail_event_sha256: str,
) -> ResearchCommandProposal:
    action_ref = submission.action.object_ref
    return ResearchCommandProposal(
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        expected_stream_version=expected_stream_version,
        expected_tail_event_sha256=expected_tail_event_sha256,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=action_ref.object_id,
            branch_id=submission.target_branch_id,
        ),
        proposed_by_principal_id="service:action-proposal",
        proposed_at=submission.submitted_at,
    )


def _receipt() -> ContinuationReceipt:
    return ContinuationReceipt(
        world_model_snapshot_sha256=_sha("world-model"),
        observation_projection_sha256=_sha("observation-projection"),
        scientific_slot_id=SLOT_ID,
        assessments=(),
        disposition=ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED,
        reason_codes=("hypothesis_set_fork_required",),
        proposed_action_kind=ActionKind.FORK,
    )


def _incorporated_event(*, action_id: str, slot_id: str = SLOT_ID) -> ResearchEvent:
    return ResearchEvent(
        quest_id=QUEST_ID,
        sequence=2,
        parent_event_sha256=_sha("parent-event"),
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=ObservationIncorporatedPayload(
            branch_id=BRANCH_ID,
            action_id=action_id,
            scientific_slot_id=slot_id,
            committed_admission_sha256=_sha("admission"),
            scientific_observation_sha256=_sha("observation"),
            outcome="positive",
            source_world_model_sha256=_sha("world-model"),
        ),
        command_sha256=_sha("command"),
        principal_id="agent:operator",
        authorization_receipt_sha256=_sha("authorization"),
        committed_at=NOW,
    )


def _fork_decision(submission, receipt, event) -> TransitionDecision:
    return TransitionDecision(
        transition_id="transition:fork-followup",
        quest_id=QUEST_ID,
        charter_ref=submission.action.charter_ref,
        source_graph_sha256=_sha("source-graph"),
        selected_action_ref=submission.action.object_ref,
        directive=ForkDirective(
            source_branch_id=BRANCH_ID,
            child_branch_ids=("rbr_" + "a" * 32, "rbr_" + "b" * 32),
        ),
        evidence_refs=(continuation_receipt_evidence_ref(receipt),),
        evidence_event_sha256s=(event.event_sha256,),
        budget_receipt_sha256=_sha("budget"),
        risk_receipt_sha256=_sha("risk"),
        policy_receipt_sha256=_sha("policy"),
        reason_codes=("hypothesis_set_fork_required",),
        rationale="The frozen hypothesis set cannot explain the admitted observation.",
        decided_by_principal_id="service:continuation-assessment",
        decided_at=submission.submitted_at,
    )


def _transition_proposal(submission, decision) -> ResearchCommandProposal:
    return ResearchCommandProposal(
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        expected_stream_version=submission.request.expected_stream_version,
        expected_tail_event_sha256=submission.request.expected_tail_event_sha256,
        event_type=EventType.FORK_COMMITTED,
        payload=ForkCommittedPayload(decision=decision),
        proposed_by_principal_id="service:action-proposal",
        proposed_at=submission.submitted_at,
    )


def _live_pins(submission) -> tuple[int, str]:
    """Head pins the authorization names after the admission commit lands."""

    return (
        submission.request.expected_stream_version + 1,
        "b" * 64,
    )


def test_action_authority_signs_only_the_exact_submitted_action() -> None:
    authority = _action_authority()
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    version, tail = _live_pins(submission)
    proposal = _authorization_proposal(
        submission,
        expected_stream_version=version,
        expected_tail_event_sha256=tail,
    )
    idempotency = f"action:{submission.action.object_sha256}"
    source = f"action-proposal:{submission.action.object_sha256}"

    command = authority.authorize_action(
        proposal=proposal,
        submitted=submission,
        idempotency_key=idempotency,
        source_event_key=source,
    )

    assert command.proposal_sha256 == proposal.proposal_sha256
    assert command.idempotency_key == idempotency
    assert command.principal_id == authority.principal_id
    assert command.authorized_at == proposal.proposed_at


def test_action_authority_rejects_rebound_proposals() -> None:
    authority = _action_authority()
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    proposal = _authorization_proposal(
        submission,
        expected_stream_version=submission.request.expected_stream_version + 1,
        expected_tail_event_sha256="b" * 64,
    )
    idempotency = f"action:{submission.action.object_sha256}"
    source = f"action-proposal:{submission.action.object_sha256}"

    rebound_payload = proposal.payload.model_copy(update={"action_id": "action:rebound"})
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_action(
            proposal=proposal.model_copy(update={"payload": rebound_payload}),
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key=source,
        )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_action(
            proposal=proposal,
            submitted=submission,
            idempotency_key="action:" + "0" * 64,
            source_event_key=source,
        )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_action(
            proposal=proposal,
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key="action-proposal:" + "0" * 64,
        )
    with pytest.raises(KernelCommandAuthorityError, match="escaped"):
        authority.authorize_action(
            proposal=proposal.model_copy(
                update={
                    "quest_id": "qst_" + "f" * 32,
                    "scope_binding": ResearchScopeBinding(quest_id="qst_" + "f" * 32),
                }
            ),
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key=source,
        )
    with pytest.raises(KernelCommandAuthorityError, match="own proposal"):
        authority.authorize_action(
            proposal=proposal.model_copy(
                update={"proposed_by_principal_id": authority.principal_id}
            ),
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key=source,
        )


def test_admission_authority_signs_the_submission_carried_admission_command() -> None:
    authority = _admission_authority()
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    proposal = submission.command_proposal
    idempotency = f"action-proposed:{submission.action.object_sha256}"
    source = f"action-proposed:{submission.action.object_sha256}"

    command = authority.authorize_admission(
        proposal=proposal,
        submitted=submission,
        idempotency_key=idempotency,
        source_event_key=source,
    )

    assert command.proposal_sha256 == proposal.proposal_sha256
    assert command.idempotency_key == idempotency
    assert command.principal_id == authority.principal_id
    # the bridge's :454 clause — the ACTION_PROPOSED event's principal IS the
    # action's proposer — holds by construction on the signed command
    assert command.principal_id == submission.proposed_by_principal_id


def test_admission_authority_rejects_a_rebound_admission_command() -> None:
    authority = _admission_authority()
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    idempotency = f"action-proposed:{submission.action.object_sha256}"
    source = f"action-proposed:{submission.action.object_sha256}"

    # any drift from the byte-exact command the submission carries — here a
    # re-pinned stream head — must be refused: the real store admits the
    # action through this exact event, so a rebound admission would admit an
    # action the Quest never saw authored
    rebound = submission.command_proposal.model_copy(
        update={
            "expected_stream_version": (
                submission.command_proposal.expected_stream_version + 1
            )
        }
    )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_admission(
            proposal=rebound,
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key=source,
        )
    # the admission command under the authorization's idempotency identity is
    # the exact wiring mistake the RPC dispatch could make
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_admission(
            proposal=submission.command_proposal,
            submitted=submission,
            idempotency_key=f"action:{submission.action.object_sha256}",
            source_event_key=source,
        )
    # and so is the admission under the authorization's source identity: the
    # store's receipt lookup matches persisted rows by either key, so a shared
    # source_event_key would collide the two commands of one exchange
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_admission(
            proposal=submission.command_proposal,
            submitted=submission,
            idempotency_key=idempotency,
            source_event_key=f"action-proposal:{submission.action.object_sha256}",
        )
    # an ACTION_AUTHORIZED proposal under the admission operation is the same
    # dispatch mistake on the event side: the admission authority signs
    # ACTION_PROPOSED only
    version, tail = _live_pins(submission)
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_admission(
            proposal=_authorization_proposal(
                submission,
                expected_stream_version=version,
                expected_tail_event_sha256=tail,
            ),
            submitted=submission,
            idempotency_key=f"action:{submission.action.object_sha256}",
            source_event_key=f"action-proposal:{submission.action.object_sha256}",
        )


def test_admission_authority_signs_only_its_own_principal_s_proposals() -> None:
    authority = _admission_authority()
    # a submission whose proposer is another principal is not this authority's
    # contract, even when every other exact-proposal clause holds
    request = _request(ControllerStep.PROPOSE_ACTION)
    foreign_binding = _proposal_binding().model_copy(
        update={"principal_id": "service:another-proposer"}
    )
    submission = materialize_action_proposal(
        request=request,
        draft=_draft(request),
        authority_binding=foreign_binding,
        submitted_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(KernelCommandAuthorityError, match="own principal"):
        authority.authorize_admission(
            proposal=submission.command_proposal,
            submitted=submission,
            idempotency_key=f"action-proposed:{submission.action.object_sha256}",
            source_event_key=f"action-proposed:{submission.action.object_sha256}",
        )


def test_action_authority_refuses_the_submission_carried_admission_command() -> None:
    authority = _action_authority()
    submission = _submission(ControllerStep.PROPOSE_ACTION)

    # the ACTION_PROPOSED admission belongs to the admission authority holding
    # the proposer's own key; this authority signing it would collapse the
    # bridge's proposer/authorizer separation, so the event-type clause
    # refuses it by construction
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_action(
            proposal=submission.command_proposal,
            submitted=submission,
            idempotency_key=f"action-proposed:{submission.action.object_sha256}",
            source_event_key=f"action-proposed:{submission.action.object_sha256}",
        )


def test_command_authorities_fail_closed_on_rebound_configuration() -> None:
    trust_root, key, assignment = _assignment_fixture()

    with pytest.raises(KernelCommandAuthorityError, match="private key"):
        ExactActionKernelAuthority(
            trust_root=trust_root,
            assignments=(assignment,),
            authorization_key_id=key.key_id,
            private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.AMENDMENT],
        )
    with pytest.raises(KernelCommandAuthorityError, match="unique.*canonical"):
        ExactTransitionKernelAuthority(
            trust_root=trust_root,
            assignments=(assignment, assignment),
            authorization_key_id=key.key_id,
            private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        )
    for build in (_action_authority, _transition_authority):
        with pytest.raises(KernelCommandAuthorityError, match="ordinary Kernel key"):
            build(role=ResearchAuthorizationRole.EMERGENCY)
    admission = _admission_authority()
    with pytest.raises(KernelCommandAuthorityError, match="private key"):
        ExactAdmissionKernelAuthority(
            trust_root=admission.trust_root,
            assignments=admission.assignments,
            authorization_key_id=admission.authorization_key_id,
            private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.AMENDMENT],
        )


def test_transition_authority_signs_only_the_receipt_forced_fork() -> None:
    authority = _transition_authority()
    submission = _submission(ControllerStep.PROPOSE_FOLLOWUP)
    receipt = _receipt()
    event = _incorporated_event(action_id=submission.action.action_id)
    decision = _fork_decision(submission, receipt, event)
    proposal = _transition_proposal(submission, decision)
    idempotency = f"transition:{decision.decision_sha256}"
    source = f"continuation:{receipt.receipt_sha256}"

    command = authority.authorize_transition(
        proposal=proposal,
        submitted=submission,
        receipt=receipt,
        incorporated_event=event,
        idempotency_key=idempotency,
        source_event_key=source,
    )

    assert command.proposal_sha256 == proposal.proposal_sha256
    assert command.idempotency_key == idempotency
    assert command.principal_id == authority.principal_id


def test_transition_authority_rejects_rebound_continuations() -> None:
    authority = _transition_authority()
    submission = _submission(ControllerStep.PROPOSE_FOLLOWUP)
    receipt = _receipt()
    event = _incorporated_event(action_id=submission.action.action_id)
    decision = _fork_decision(submission, receipt, event)
    proposal = _transition_proposal(submission, decision)
    idempotency = f"transition:{decision.decision_sha256}"
    source = f"continuation:{receipt.receipt_sha256}"

    uncited = _transition_proposal(
        submission, decision.model_copy(update={"evidence_refs": ()})
    )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_transition(
            proposal=uncited,
            submitted=submission,
            receipt=receipt,
            incorporated_event=event,
            idempotency_key=f"transition:{uncited.payload.decision.decision_sha256}",
            source_event_key=source,
        )
    wrong_slot = _incorporated_event(
        action_id=submission.action.action_id, slot_id="sos_" + "f" * 32
    )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_transition(
            proposal=proposal,
            submitted=submission,
            receipt=receipt,
            incorporated_event=wrong_slot,
            idempotency_key=idempotency,
            source_event_key=source,
        )
    stopped = decision.model_copy(
        update={
            "directive": StopDirective(
                branch_id=BRANCH_ID, stop_reason=StopReason.EVIDENCE_THRESHOLD_REACHED
            )
        }
    )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_transition(
            proposal=_transition_proposal(submission, stopped),
            submitted=submission,
            receipt=receipt,
            incorporated_event=event,
            idempotency_key=f"transition:{stopped.decision_sha256}",
            source_event_key=source,
        )
    with pytest.raises(KernelCommandAuthorityError, match="rebound"):
        authority.authorize_transition(
            proposal=proposal,
            submitted=submission,
            receipt=receipt,
            incorporated_event=event,
            idempotency_key="transition:" + "0" * 64,
            source_event_key=source,
        )
    other_quest = "qst_" + "f" * 32
    foreign_decision = decision.model_copy(
        update={
            "quest_id": other_quest,
            "charter_ref": decision.charter_ref.model_copy(update={"quest_id": other_quest}),
            "selected_action_ref": decision.selected_action_ref.model_copy(
                update={"quest_id": other_quest}
            ),
        }
    )
    foreign_proposal = _transition_proposal(submission, decision).model_copy(
        update={
            "quest_id": other_quest,
            "scope_binding": ResearchScopeBinding(quest_id=other_quest),
            "payload": ForkCommittedPayload(decision=foreign_decision),
        }
    )
    with pytest.raises(KernelCommandAuthorityError, match="escaped"):
        authority.authorize_transition(
            proposal=foreign_proposal,
            submitted=submission,
            receipt=receipt,
            incorporated_event=event,
            idempotency_key=f"transition:{foreign_decision.decision_sha256}",
            source_event_key=source,
        )


def test_emergency_stop_stays_off_the_ordinary_transition_key() -> None:
    submission = _submission(ControllerStep.PROPOSE_FOLLOWUP)
    base = _fork_decision(
        submission,
        _receipt(),
        _incorporated_event(action_id=submission.action.action_id),
    )
    ordinary = StopCommittedPayload(
        decision=base.model_copy(
            update={
                "directive": StopDirective(
                    branch_id=BRANCH_ID, stop_reason=StopReason.EVIDENCE_THRESHOLD_REACHED
                )
            }
        )
    )
    emergency = StopCommittedPayload(
        decision=base.model_copy(
            update={
                "directive": StopDirective(
                    branch_id=BRANCH_ID, stop_reason=StopReason.EMERGENCY_STOP
                )
            }
        )
    )

    assert (
        required_authorization_role(event_type=EventType.STOP_COMMITTED, payload=ordinary)
        is ResearchAuthorizationRole.ORDINARY
    )
    assert (
        required_authorization_role(event_type=EventType.STOP_COMMITTED, payload=emergency)
        is ResearchAuthorizationRole.EMERGENCY
    )
    with pytest.raises(KernelCommandAuthorityError, match="ordinary Kernel key"):
        _transition_authority(role=ResearchAuthorizationRole.EMERGENCY)


def _pending_values() -> dict[str, object]:
    return {
        "quest_id": QUEST_ID,
        "action_sha256": _sha("transition-action"),
        "scientific_slot_id": None,
        "audited_stream_version": 7,
        "audited_tail_event_sha256": _sha("tail"),
        "audited_snapshot_sha256": _sha("snapshot"),
        "action_authorized": True,
        "authorized_action_transition_pending": True,
        "compilation_disposition": CompilationDisposition.MISSING,
        "scientific_execution_authorization_registered": False,
        "execution_terminal_observed": False,
        "validation_committed": False,
        "admission_committed": False,
        "observation_incorporated": False,
        "continuation_committed": False,
        "blocker_codes": (),
    }


def test_transition_pending_projection_parks_in_typed_blocked_wait() -> None:
    projection = ControllerRecoveryProjection.model_validate(_pending_values())
    plan = plan_recovery_tick(projection)

    assert plan.step is ControllerStep.BLOCKED
    assert plan.blocker_codes == (TRANSITION_COMMIT_PENDING_BLOCKER,)


def test_transition_pending_wait_requires_an_isolated_authorized_action() -> None:
    with pytest.raises(ValidationError, match="requires an authorized action"):
        ControllerRecoveryProjection.model_validate(
            {**_pending_values(), "action_authorized": False}
        )
    with pytest.raises(ValidationError, match="execution receipts"):
        ControllerRecoveryProjection.model_validate(
            {**_pending_values(), "compilation_disposition": CompilationDisposition.ACCEPTED}
        )


def test_unset_transition_flag_keeps_legacy_projection_hashes() -> None:
    values = {
        key: item
        for key, item in _pending_values().items()
        if key != "authorized_action_transition_pending"
    }
    legacy = ControllerRecoveryProjection.model_validate(values)
    explicit = ControllerRecoveryProjection.model_validate(
        {**values, "authorized_action_transition_pending": None}
    )

    assert legacy.authorized_action_transition_pending is None
    assert legacy.projection_sha256 == explicit.projection_sha256


def _command_binding(
    role: ControllerStepAuthorityRole,
    *,
    principal_id: str | None = None,
) -> ControllerStepAuthorityBinding:
    return ControllerStepAuthorityBinding(
        role=role,
        principal_id=principal_id or f"authority:{role.value}",
        key_id=f"key:{role.value}",
        policy_sha256=_sha(f"policy:{role.value}"),
        service_manifest_sha256=_sha(f"service:{role.value}"),
        externally_deployed=True,
    )


def test_executor_enrolls_command_authorities_beside_adapter_closures() -> None:
    adapters = _adapters()
    executor = DedicatedControllerStepExecutor(
        controller_manifest=_controller_manifest(),
        worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
        manifest=_adapter_set(adapters),
        adapters=adapters,
    )
    assert executor.command_authority_bindings == ()

    bindings = (
        _command_binding(ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND),
        _command_binding(ControllerStepAuthorityRole.TRANSITION_KERNEL_COMMAND),
    )
    enrolled = DedicatedControllerStepExecutor(
        controller_manifest=_controller_manifest(),
        worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
        manifest=_adapter_set(adapters),
        adapters=adapters,
        command_authority_bindings=bindings,
    )
    assert enrolled.command_authority_bindings == bindings

    with pytest.raises(ValueError, match="distinct principals"):
        DedicatedControllerStepExecutor(
            controller_manifest=_controller_manifest(),
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=_adapter_set(adapters),
            adapters=adapters,
            command_authority_bindings=(
                _command_binding(
                    ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND,
                    principal_id="authority:kernel_command",
                ),
                _command_binding(ControllerStepAuthorityRole.TRANSITION_KERNEL_COMMAND),
            ),
        )
    with pytest.raises(ValueError, match="cannot be signed scientific authorities"):
        DedicatedControllerStepExecutor(
            controller_manifest=_controller_manifest(),
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=_adapter_set(adapters),
            adapters=adapters,
            command_authority_bindings=(
                _command_binding(
                    ControllerStepAuthorityRole.TRANSITION_KERNEL_COMMAND,
                    principal_id=WORKER_PROCESS_PRINCIPAL,
                ),
            ),
        )
    with pytest.raises(ValueError, match="only accept external command roles"):
        DedicatedControllerStepExecutor(
            controller_manifest=_controller_manifest(),
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=_adapter_set(adapters),
            adapters=adapters,
            command_authority_bindings=(
                _command_binding(ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION),
            ),
        )
    with pytest.raises(ValueError, match="rebound"):
        DedicatedControllerStepExecutor(
            controller_manifest=_controller_manifest(),
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=_adapter_set(adapters),
            adapters=adapters,
            command_authority_bindings=(
                _command_binding(ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND),
                _command_binding(
                    ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND,
                    principal_id="authority:action-kernel-two",
                ),
            ),
        )


def test_continuation_receipt_citation_is_exact() -> None:
    receipt = _receipt()

    assert continuation_receipt_evidence_ref(receipt) == EvidenceRef(
        kind=EvidenceKind.POLICY,
        object_sha256=receipt.receipt_sha256,
        object_id=f"continuation:{receipt.receipt_sha256[:32]}",
    )
