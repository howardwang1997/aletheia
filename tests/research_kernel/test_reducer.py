from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    BranchLifecycle,
    InvalidTransitionError,
    ResearchStateGraph,
    canonical_state_bytes,
    canonical_state_sha256,
    empty_state,
    reduce_event,
    replay,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    ActivateCommittedPayload,
    ActivateDirective,
    BacktrackCommittedPayload,
    BacktrackDirective,
    CharterActivatedPayload,
    CharterRevisedPayload,
    ContinueCommittedPayload,
    ContinueDirective,
    EventType,
    EvidenceKind,
    EvidenceRef,
    ForkCommittedPayload,
    ForkDirective,
    KernelObject,
    Opportunity,
    OpportunityKind,
    OpportunityRecordedPayload,
    PauseCommittedPayload,
    PauseDirective,
    ProblemAdmittedPayload,
    QuestionAdmittedPayload,
    QuestionKind,
    RefineCommittedPayload,
    RefineDirective,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchEvent,
    ResearchProblemVersion,
    ResearchQuestionVersion,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
    emergency_halt_action_ref,
)

NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _id(prefix: str, label: str) -> str:
    return f"{prefix}_{_sha(label)[:32]}"


def _charter(
    quest_id: str, *, version: int = 1, parent: str | None = None
) -> ResearchCharterVersion:
    return ResearchCharterVersion(
        quest_id=quest_id,
        charter_id="charter_primary",
        version=version,
        revision_parent_sha256=parent,
        mission=f"truthful autonomous research charter v{version}",
        value_boundaries=("scientific_integrity",),
        included_scopes=("bounded_research",),
        excluded_scopes=("unreviewed_external_action",),
        allowed_action_classes=("analysis", "transition"),
        safety_policy_sha256=_sha(f"safety:{version}"),
        ethics_policy_sha256=_sha(f"ethics:{version}"),
        license_policy_sha256=_sha(f"license:{version}"),
        privacy_policy_sha256=_sha(f"privacy:{version}"),
        egress_policy_sha256=_sha(f"egress:{version}"),
        budget_policy_sha256=_sha(f"budget:{version}"),
        approval_policy_sha256=_sha(f"approval:{version}"),
        publication_policy_sha256=_sha(f"publication:{version}"),
        amendment_principal_ids=("human:owner",),
        emergency_stop_principal_ids=("human:owner",),
        authorized_by_principal_id="human:owner",
        authority_receipt_sha256=_sha(f"authority:{version}"),
        authorized_at=NOW + timedelta(seconds=version),
    )


@dataclass
class Scenario:
    quest_id: str = field(default_factory=lambda: _id("qst", "reducer-quest"))
    root_branch_id: str = field(default_factory=lambda: _id("rbr", "root"))
    events: list[ResearchEvent] = field(default_factory=list)
    objects: dict[str, KernelObject] = field(default_factory=dict)
    state: ResearchStateGraph = field(default_factory=empty_state)
    charter: ResearchCharterVersion | None = None

    def add_object(self, obj: KernelObject) -> None:
        self.objects[obj.object_sha256] = obj

    def commit(self, event_type: EventType, payload: Any) -> ResearchEvent:
        sequence = len(self.events) + 1
        event = ResearchEvent(
            quest_id=self.quest_id,
            sequence=sequence,
            parent_event_sha256=self.events[-1].event_sha256 if self.events else None,
            event_type=event_type,
            payload=payload,
            command_sha256=_sha(f"command:{sequence}:{event_type.value}"),
            principal_id="test:kernel",
            authorization_receipt_sha256=_sha(f"authorization:{sequence}"),
            committed_at=NOW + timedelta(minutes=sequence),
        )
        old_state = self.state
        self.state = reduce_event(old_state, event, self.objects)
        assert old_state.stream_version == sequence - 1
        self.events.append(event)
        return event


def _started() -> Scenario:
    case = Scenario()
    case.charter = _charter(case.quest_id)
    case.add_object(case.charter)
    case.commit(
        EventType.CHARTER_ACTIVATED,
        CharterActivatedPayload(
            charter_ref=case.charter.object_ref,
            root_branch_id=case.root_branch_id,
        ),
    )
    return case


def _admit_problem_and_question(
    case: Scenario, *, branch_id: str | None = None
) -> tuple[Opportunity, ResearchProblemVersion, ResearchQuestionVersion]:
    assert case.charter is not None
    branch_id = branch_id or case.root_branch_id
    opportunity = Opportunity(
        opportunity_id=_id("opportunity", f"opportunity:{len(case.events)}"),
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        kind=OpportunityKind.KNOWLEDGE_GAP,
        statement="A bounded contradiction remains unresolved.",
        evidence_refs=(
            EvidenceRef(kind=EvidenceKind.CONTRADICTION, object_sha256=_sha("contradiction")),
        ),
        recorded_by_principal_id="test:opportunity",
        recorded_at=NOW + timedelta(minutes=len(case.events) + 1),
    )
    case.add_object(opportunity)
    case.commit(
        EventType.OPPORTUNITY_RECORDED,
        OpportunityRecordedPayload(
            opportunity_ref=opportunity.object_ref,
            branch_id=branch_id,
        ),
    )
    problem = ResearchProblemVersion(
        problem_id="problem_primary",
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        version=1,
        title="Bounded mechanism problem",
        statement="Which explanation accounts for the contradiction?",
        scope="fixture scope",
        importance_rationale="The explanations imply different observations.",
        unknowns=("mechanism",),
        opportunity_refs=(opportunity.object_ref,),
        evidence_refs=(EvidenceRef(kind=EvidenceKind.NEGATIVE, object_sha256=_sha("negative")),),
        semantic_delta="initial version",
        authored_by_principal_id="test:problem",
        authored_at=NOW + timedelta(minutes=len(case.events) + 1),
    )
    case.add_object(problem)
    case.commit(
        EventType.PROBLEM_ADMITTED,
        ProblemAdmittedPayload(problem_ref=problem.object_ref, branch_id=branch_id),
    )
    question = ResearchQuestionVersion(
        question_id="question_primary",
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.MECHANISTIC,
        statement="Which of the competing mechanisms is consistent?",
        scope="fixture scope",
        answer_space=("mechanism_a", "mechanism_b"),
        scientific_value="A discriminating result resolves the contradiction.",
        falsifiability="Either mechanism can be contradicted.",
        semantic_delta="initial version",
        authored_by_principal_id="test:question",
        authored_at=NOW + timedelta(minutes=len(case.events) + 1),
    )
    case.add_object(question)
    case.commit(
        EventType.QUESTION_ADMITTED,
        QuestionAdmittedPayload(question_ref=question.object_ref, branch_id=branch_id),
    )
    return opportunity, problem, question


def _propose(
    case: Scenario,
    question: ResearchQuestionVersion,
    branch_id: str,
    kind: ActionKind,
    label: str,
) -> ResearchActionProposal:
    assert case.charter is not None and case.state.tail_event_sha256 is not None
    action = ResearchActionProposal(
        action_id=_id("action", label),
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=case.state.tail_event_sha256,
        kind=kind,
        epistemic_purpose=f"apply {kind.value} without hiding prior evidence",
        candidate_outcomes=("advance", "do_not_advance"),
        evidence_refs=(
            EvidenceRef(kind=EvidenceKind.INCONCLUSIVE, object_sha256=_sha(f"evidence:{label}")),
        ),
        cost_receipt_sha256=_sha(f"cost:{label}"),
        risk_receipt_sha256=_sha(f"risk:{label}"),
        requested_authority_class="transition",
        proposed_by_principal_id="test:proposal",
        proposed_at=NOW + timedelta(minutes=len(case.events) + 1),
    )
    case.add_object(action)
    case.commit(
        EventType.ACTION_PROPOSED,
        ActionProposedPayload(action_ref=action.object_ref, branch_id=branch_id),
    )
    return action


def _decision(
    case: Scenario,
    action: ResearchActionProposal,
    directive: Any,
    label: str,
    *,
    evidence: bool = True,
) -> TransitionDecision:
    assert case.charter is not None
    return TransitionDecision(
        transition_id=_id("transition", label),
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        source_graph_sha256=case.state.snapshot_sha256,
        selected_action_ref=action.object_ref,
        directive=directive,
        evidence_refs=(
            (EvidenceRef(kind=EvidenceKind.INCONCLUSIVE, object_sha256=_sha(f"decision:{label}")),)
            if evidence
            else ()
        ),
        evidence_event_sha256s=(case.events[-1].event_sha256,) if evidence else (),
        budget_receipt_sha256=_sha(f"decision-budget:{label}"),
        risk_receipt_sha256=_sha(f"decision-risk:{label}"),
        policy_receipt_sha256=_sha(f"decision-policy:{label}"),
        reason_codes=("evidence_bound",),
        rationale=f"typed {label} transition",
        decided_by_principal_id="test:decision",
        decided_at=NOW + timedelta(minutes=len(case.events) + 1),
    )


def _branch(case: Scenario, branch_id: str):
    return next(branch for branch in case.state.branches if branch.branch_id == branch_id)


def test_catalog_admission_charter_revision_and_lineage_heads_are_exact() -> None:
    case = _started()
    charter_v1 = case.charter
    assert charter_v1 is not None
    charter_v2 = _charter(case.quest_id, version=2, parent=charter_v1.object_sha256)
    case.add_object(charter_v2)
    case.commit(
        EventType.CHARTER_REVISED,
        CharterRevisedPayload(charter_ref=charter_v2.object_ref),
    )
    case.charter = charter_v2

    opportunity, problem_v1, question = _admit_problem_and_question(case)
    assert case.state.charter_history == (charter_v1.object_ref, charter_v2.object_ref)
    assert {item.kind for item in case.state.evidence_refs} == {
        EvidenceKind.CONTRADICTION,
        EvidenceKind.NEGATIVE,
    }

    problem_v2 = problem_v1.model_copy(
        update={
            "version": 2,
            "revision_parent_sha256": problem_v1.object_sha256,
            "semantic_delta": "narrowed after exact negative evidence",
            "authored_at": NOW + timedelta(hours=1),
        }
    )
    problem_v2 = ResearchProblemVersion.model_validate(problem_v2.model_dump())
    case.add_object(problem_v2)
    case.commit(
        EventType.PROBLEM_ADMITTED,
        ProblemAdmittedPayload(
            problem_ref=problem_v2.object_ref,
            branch_id=case.root_branch_id,
        ),
    )

    competing_v2 = problem_v1.model_copy(
        update={
            "version": 2,
            "revision_parent_sha256": problem_v1.object_sha256,
            "semantic_delta": "competing edit from a stale lineage head",
            "authored_at": NOW + timedelta(hours=2),
        }
    )
    competing_v2 = ResearchProblemVersion.model_validate(competing_v2.model_dump())
    case.add_object(competing_v2)
    event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.PROBLEM_ADMITTED,
        payload=ProblemAdmittedPayload(
            problem_ref=competing_v2.object_ref,
            branch_id=case.root_branch_id,
        ),
        command_sha256=_sha("stale-revision-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("stale-revision-auth"),
        committed_at=NOW + timedelta(hours=3),
    )
    with pytest.raises(InvalidTransitionError, match="current lineage head"):
        reduce_event(case.state, event, case.objects)

    # The exact question remains tied to problem v1; history is retained, never silently rebound.
    assert question.problem_ref == problem_v1.object_ref
    assert opportunity.object_ref in problem_v1.opportunity_refs


def test_fork_activate_backtrack_preserves_a_strict_checkpoint() -> None:
    case = _started()
    _, _, question = _admit_problem_and_question(case)
    target_event_sha256 = case.events[-1].event_sha256
    fork_action = _propose(case, question, case.root_branch_id, ActionKind.FORK, "fork")
    children = tuple(sorted((_id("rbr", "child-a"), _id("rbr", "child-b"))))
    case.commit(
        EventType.FORK_COMMITTED,
        ForkCommittedPayload(
            decision=_decision(
                case,
                fork_action,
                ForkDirective(source_branch_id=case.root_branch_id, child_branch_ids=children),
                "fork",
            )
        ),
    )
    assert _branch(case, case.root_branch_id).lifecycle is BranchLifecycle.ACTIVE
    assert {_branch(case, child).lifecycle for child in children} == {BranchLifecycle.ADMITTED}

    activate_action = _propose(case, question, children[0], ActionKind.ACTIVATE, "activate-child")
    case.commit(
        EventType.ACTIVATE_COMMITTED,
        ActivateCommittedPayload(
            decision=_decision(
                case,
                activate_action,
                ActivateDirective(branch_id=children[0]),
                "activate-child",
            )
        ),
    )
    backtrack_action = _propose(case, question, children[0], ActionKind.BACKTRACK, "backtrack")
    new_branch = _id("rbr", "backtracked-child")
    case.commit(
        EventType.BACKTRACK_COMMITTED,
        BacktrackCommittedPayload(
            decision=_decision(
                case,
                backtrack_action,
                BacktrackDirective(
                    source_branch_id=children[0],
                    target_branch_id=case.root_branch_id,
                    target_event_sha256=target_event_sha256,
                    new_branch_id=new_branch,
                ),
                "backtrack",
            )
        ),
    )
    assert _branch(case, children[0]).lifecycle is BranchLifecycle.BACKTRACKED
    assert _branch(case, new_branch).lifecycle is BranchLifecycle.ACTIVE
    checkpoint = next(
        item
        for item in case.state.branch_checkpoints
        if item.branch_id == case.root_branch_id and item.event_sha256 == target_event_sha256
    )
    assert _branch(case, new_branch).problem_refs == checkpoint.problem_refs
    assert _branch(case, new_branch).question_refs == checkpoint.question_refs

    bad_action = _propose(case, question, new_branch, ActionKind.BACKTRACK, "bad-backtrack")
    invalid = _decision(
        case,
        bad_action,
        BacktrackDirective(
            source_branch_id=new_branch,
            target_branch_id=children[1],
            target_event_sha256=_branch(case, children[1]).head_event_sha256,
            new_branch_id=_id("rbr", "bad-child"),
        ),
        "bad-backtrack",
    )
    event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.BACKTRACK_COMMITTED,
        payload=BacktrackCommittedPayload(decision=invalid),
        command_sha256=_sha("bad-backtrack-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("bad-backtrack-auth"),
        committed_at=NOW + timedelta(hours=4),
    )
    with pytest.raises(InvalidTransitionError):
        reduce_event(case.state, event, case.objects)


def test_continue_pause_activate_refine_and_terminal_stop_have_no_stage_order() -> None:
    case = _started()
    _, _, question = _admit_problem_and_question(case)

    continue_action = _propose(case, question, case.root_branch_id, ActionKind.CONTINUE, "continue")
    case.commit(
        EventType.CONTINUE_COMMITTED,
        ContinueCommittedPayload(
            decision=_decision(
                case,
                continue_action,
                ContinueDirective(branch_id=case.root_branch_id),
                "continue",
            )
        ),
    )
    pause_action = _propose(case, question, case.root_branch_id, ActionKind.PAUSE, "pause")
    case.commit(
        EventType.PAUSE_COMMITTED,
        PauseCommittedPayload(
            decision=_decision(
                case,
                pause_action,
                PauseDirective(branch_id=case.root_branch_id),
                "pause",
            )
        ),
    )
    assert _branch(case, case.root_branch_id).lifecycle is BranchLifecycle.PAUSED

    activate_action = _propose(
        case, question, case.root_branch_id, ActionKind.ACTIVATE, "reactivate"
    )
    case.commit(
        EventType.ACTIVATE_COMMITTED,
        ActivateCommittedPayload(
            decision=_decision(
                case,
                activate_action,
                ActivateDirective(branch_id=case.root_branch_id),
                "reactivate",
            )
        ),
    )
    refine_action = _propose(case, question, case.root_branch_id, ActionKind.REFINE, "refine")
    refined_branch = _id("rbr", "refined")
    case.commit(
        EventType.REFINE_COMMITTED,
        RefineCommittedPayload(
            decision=_decision(
                case,
                refine_action,
                RefineDirective(
                    source_branch_id=case.root_branch_id,
                    child_branch_id=refined_branch,
                ),
                "refine",
            )
        ),
    )
    assert _branch(case, case.root_branch_id).lifecycle is BranchLifecycle.SUPERSEDED
    assert _branch(case, refined_branch).lifecycle is BranchLifecycle.ACTIVE

    stop_action = _propose(case, question, refined_branch, ActionKind.STOP, "stop")
    case.commit(
        EventType.STOP_COMMITTED,
        StopCommittedPayload(
            decision=_decision(
                case,
                stop_action,
                StopDirective(
                    branch_id=refined_branch,
                    stop_reason=StopReason.CURRENTLY_INDISTINGUISHABLE,
                    reopen_conditions=("new_measurement",),
                    unresolved_refs=(
                        EvidenceRef(
                            kind=EvidenceKind.INCONCLUSIVE,
                            object_sha256=_sha("unresolved"),
                        ),
                    ),
                ),
                "stop",
            )
        ),
    )
    assert case.state.terminal
    assert case.state.terminal_event_sha256 == case.events[-1].event_sha256
    assert {item.kind for item in case.state.evidence_refs} >= {
        EvidenceKind.CONTRADICTION,
        EvidenceKind.NEGATIVE,
        EvidenceKind.INCONCLUSIVE,
    }

    later_action = ResearchActionProposal(
        **{
            **stop_action.model_dump(),
            "action_id": _id("action", "after-terminal"),
            "basis_tail_event_sha256": case.state.tail_event_sha256,
            "proposed_at": NOW + timedelta(days=1),
        }
    )
    case.add_object(later_action)
    event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=later_action.object_ref,
            branch_id=refined_branch,
        ),
        command_sha256=_sha("terminal-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("terminal-auth"),
        committed_at=NOW + timedelta(days=1),
    )
    with pytest.raises(InvalidTransitionError, match="terminal"):
        reduce_event(case.state, event, case.objects)


def test_emergency_halt_needs_no_action_and_stops_every_live_branch() -> None:
    case = _started()
    _, _, question = _admit_problem_and_question(case)
    fork_action = _propose(case, question, case.root_branch_id, ActionKind.FORK, "emergency-fork")
    child_branches = tuple(sorted((_id("rbr", "emergency-a"), _id("rbr", "emergency-b"))))
    case.commit(
        EventType.FORK_COMMITTED,
        ForkCommittedPayload(
            decision=_decision(
                case,
                fork_action,
                ForkDirective(
                    source_branch_id=case.root_branch_id,
                    child_branch_ids=child_branches,
                ),
                "emergency-fork",
            )
        ),
    )
    assert {
        branch.lifecycle
        for branch in case.state.branches
        if branch.branch_id in {case.root_branch_id, *child_branches}
    } == {BranchLifecycle.ACTIVE, BranchLifecycle.ADMITTED}

    assert case.charter is not None
    halt_marker = emergency_halt_action_ref(
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
    )
    decision = TransitionDecision(
        transition_id=_id("transition", "emergency-global-halt"),
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,
        source_graph_sha256=case.state.snapshot_sha256,
        selected_action_ref=halt_marker,
        directive=StopDirective(
            branch_id=child_branches[0],
            stop_reason=StopReason.EMERGENCY_STOP,
        ),
        budget_receipt_sha256=_sha("emergency-budget"),
        risk_receipt_sha256=_sha("emergency-risk"),
        policy_receipt_sha256=_sha("emergency-policy"),
        reason_codes=("emergency_authority",),
        rationale="Globally halt every live branch without ordinary action authority.",
        decided_by_principal_id="test:emergency",
        decided_at=NOW + timedelta(hours=1),
    )
    case.commit(
        EventType.STOP_COMMITTED,
        StopCommittedPayload(decision=decision),
    )

    assert case.state.terminal
    assert all(
        branch.lifecycle
        not in {BranchLifecycle.ADMITTED, BranchLifecycle.ACTIVE, BranchLifecycle.PAUSED}
        for branch in case.state.branches
    )
    assert {
        branch.lifecycle
        for branch in case.state.branches
        if branch.branch_id in {case.root_branch_id, *child_branches}
    } == {BranchLifecycle.STOPPED}
    assert not any(action.action_ref == halt_marker for action in case.state.actions)
    assert replay(case.events, case.objects) == case.state

    later_event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.STOP_COMMITTED,
        payload=StopCommittedPayload(decision=decision),
        command_sha256=_sha("post-emergency-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("post-emergency-auth"),
        committed_at=NOW + timedelta(hours=2),
    )
    with pytest.raises(InvalidTransitionError, match="terminal"):
        reduce_event(case.state, later_event, case.objects)


def test_stop_event_rejects_a_non_stop_directive() -> None:
    case = _started()
    _, _, question = _admit_problem_and_question(case)
    action = _propose(case, question, case.root_branch_id, ActionKind.CONTINUE, "wrong-stop")
    with pytest.raises(ValueError, match="directive kind"):
        ResearchEvent(
            quest_id=case.quest_id,
            sequence=len(case.events) + 1,
            parent_event_sha256=case.events[-1].event_sha256,
            event_type=EventType.STOP_COMMITTED,
            payload=StopCommittedPayload(
                decision=_decision(
                    case,
                    action,
                    ContinueDirective(branch_id=case.root_branch_id),
                    "wrong-stop",
                )
            ),
            command_sha256=_sha("wrong-stop-command"),
            principal_id="test:kernel",
            authorization_receipt_sha256=_sha("wrong-stop-auth"),
            committed_at=NOW + timedelta(hours=1),
        )


def test_transition_rejects_authorized_action_stale_graph_and_empty_stop_basis() -> None:
    case = _started()
    _, _, question = _admit_problem_and_question(case)
    action = _propose(case, question, case.root_branch_id, ActionKind.STOP, "authorized-stop")
    case.commit(
        EventType.ACTION_AUTHORIZED,
        ActionAuthorizedPayload(action_id=action.action_id, branch_id=case.root_branch_id),
    )
    assert (
        next(item for item in case.state.actions if item.action_ref == action.object_ref).lifecycle
        is ActionLifecycle.AUTHORIZED
    )

    decision = _decision(
        case,
        action,
        StopDirective(
            branch_id=case.root_branch_id,
            stop_reason=StopReason.BUDGET_EXHAUSTED,
            reopen_conditions=("new_budget",),
        ),
        "authorized-stop",
    )
    event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.STOP_COMMITTED,
        payload=StopCommittedPayload(decision=decision),
        command_sha256=_sha("authorized-stop-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("authorized-stop-auth"),
        committed_at=NOW + timedelta(days=2),
    )
    with pytest.raises(InvalidTransitionError, match="no longer proposed"):
        reduce_event(case.state, event, case.objects)

    fresh = _started()
    _, _, fresh_question = _admit_problem_and_question(fresh)
    fresh_action = _propose(
        fresh, fresh_question, fresh.root_branch_id, ActionKind.STOP, "empty-stop"
    )
    empty_decision = _decision(
        fresh,
        fresh_action,
        StopDirective(
            branch_id=fresh.root_branch_id,
            stop_reason=StopReason.LOW_MARGINAL_INFORMATION_VALUE,
        ),
        "empty-stop",
        evidence=False,
    )
    empty_event = ResearchEvent(
        quest_id=fresh.quest_id,
        sequence=len(fresh.events) + 1,
        parent_event_sha256=fresh.events[-1].event_sha256,
        event_type=EventType.STOP_COMMITTED,
        payload=StopCommittedPayload(decision=empty_decision),
        command_sha256=_sha("empty-stop-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("empty-stop-auth"),
        committed_at=NOW + timedelta(days=3),
    )
    with pytest.raises(InvalidTransitionError, match="evidence"):
        reduce_event(fresh.state, empty_event, fresh.objects)

    wrong_kind = _propose(
        fresh,
        fresh_question,
        fresh.root_branch_id,
        ActionKind.DISCRIMINATE,
        "wrong-continue-kind",
    )
    wrong_decision = _decision(
        fresh,
        wrong_kind,
        ContinueDirective(branch_id=fresh.root_branch_id),
        "wrong-continue-kind",
    )
    wrong_event = ResearchEvent(
        quest_id=fresh.quest_id,
        sequence=len(fresh.events) + 1,
        parent_event_sha256=fresh.events[-1].event_sha256,
        event_type=EventType.CONTINUE_COMMITTED,
        payload=ContinueCommittedPayload(decision=wrong_decision),
        command_sha256=_sha("wrong-kind-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("wrong-kind-auth"),
        committed_at=NOW + timedelta(days=3, minutes=1),
    )
    with pytest.raises(InvalidTransitionError, match="continue decision selected"):
        reduce_event(fresh.state, wrong_event, fresh.objects)


def test_snapshot_hash_is_canonical_and_tampered_state_fails_closed() -> None:
    case = _started()
    _admit_problem_and_question(case)
    rebuilt = replay(case.events, case.objects)
    assert rebuilt == case.state
    assert canonical_state_bytes(rebuilt) == rebuilt.canonical_bytes()
    assert canonical_state_sha256(rebuilt) == rebuilt.snapshot_sha256
    assert replay(case.events, dict(reversed(tuple(case.objects.items())))) == rebuilt

    root = _branch(case, case.root_branch_id)
    cyclic = root.model_copy(update={"parent_branch_id": root.branch_id})
    tampered = case.state.model_copy(update={"branches": (cyclic,)})
    action = ResearchActionProposal(
        action_id=_id("action", "cycle-probe"),
        quest_id=case.quest_id,
        charter_ref=case.charter.object_ref,  # type: ignore[union-attr]
        question_ref=case.state.questions[-1].object_ref,
        basis_tail_event_sha256=case.state.tail_event_sha256,
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="probe tampered state",
        candidate_outcomes=("rejected",),
        cost_receipt_sha256=_sha("cycle-cost"),
        risk_receipt_sha256=_sha("cycle-risk"),
        requested_authority_class="transition",
        proposed_by_principal_id="test:proposal",
        proposed_at=NOW + timedelta(days=4),
    )
    case.add_object(action)
    event = ResearchEvent(
        quest_id=case.quest_id,
        sequence=len(case.events) + 1,
        parent_event_sha256=case.events[-1].event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=action.object_ref,
            branch_id=case.root_branch_id,
        ),
        command_sha256=_sha("cycle-command"),
        principal_id="test:kernel",
        authorization_receipt_sha256=_sha("cycle-auth"),
        committed_at=NOW + timedelta(days=4),
    )
    with pytest.raises(InvalidTransitionError, match="cycle"):
        reduce_event(tampered, event, case.objects)


def test_replay_is_byte_identical_in_a_fresh_process() -> None:
    case = _started()
    _admit_problem_and_question(case)
    expected = replay(case.events, case.objects)
    payload = {
        "events": [event.model_dump(mode="json") for event in case.events],
        "objects": [
            {
                "kind": obj.object_ref.object_kind.value,
                "payload": obj.model_dump(mode="json"),
            }
            for obj in case.objects.values()
        ],
    }
    script = """
import json, sys
from aletheia.research_kernel.reducer import canonical_state_bytes, canonical_state_sha256, replay
from aletheia.research_kernel.schemas import (
    Opportunity, ResearchActionProposal, ResearchCharterVersion, ResearchEvent,
    ResearchProblemVersion, ResearchQuestionVersion,
)
classes = {
    "charter": ResearchCharterVersion,
    "opportunity": Opportunity,
    "problem": ResearchProblemVersion,
    "question": ResearchQuestionVersion,
    "action": ResearchActionProposal,
}
raw = json.load(sys.stdin)
events = tuple(ResearchEvent.model_validate(item) for item in raw["events"])
objects = {}
for item in raw["objects"]:
    obj = classes[item["kind"]].model_validate(item["payload"])
    objects[obj.object_sha256] = obj
state = replay(events, objects)
json.dump({"bytes": canonical_state_bytes(state).decode(), "sha256": canonical_state_sha256(state)}, sys.stdout)
"""
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "937"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=environment,
    )
    actual = json.loads(completed.stdout)
    assert actual == {
        "bytes": canonical_state_bytes(expected).decode(),
        "sha256": canonical_state_sha256(expected),
    }
