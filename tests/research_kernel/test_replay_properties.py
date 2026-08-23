"""Property-based acceptance tests for deterministic research-kernel replay."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from aletheia.research_kernel.reducer import (
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
    ActionRejectedPayload,
    ActivateCommittedPayload,
    ActivateDirective,
    BacktrackCommittedPayload,
    BacktrackDirective,
    CharterActivatedPayload,
    EventType,
    EvidenceKind,
    EvidenceRef,
    ForkCommittedPayload,
    ForkDirective,
    KernelObject,
    ProblemAdmittedPayload,
    QuestionKind,
    QuestionAdmittedPayload,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchEvent,
    ResearchProblemVersion,
    ResearchQuestionVersion,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
)

_BASE_TIME = datetime(2026, 8, 23, tzinfo=timezone.utc)
_PROPERTY_SETTINGS = settings(
    max_examples=36,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, seed: int, label: str) -> str:
    return f"{prefix}_{_sha256(f'{seed}:{label}')[:32]}"


def _event(
    events: list[ResearchEvent],
    *,
    quest_id: str,
    event_type: EventType,
    payload: object,
    seed: int,
) -> ResearchEvent:
    sequence = len(events) + 1
    return ResearchEvent(
        quest_id=quest_id,
        sequence=sequence,
        parent_event_sha256=events[-1].event_sha256 if events else None,
        event_type=event_type,
        payload=payload,
        command_sha256=_sha256(f"{seed}:command:{sequence}"),
        principal_id="property:test-harness",
        authorization_receipt_sha256=_sha256(f"{seed}:authority:{sequence}"),
        committed_at=_BASE_TIME + timedelta(seconds=seed * 100 + sequence),
    )


def _transition_decision(
    *,
    seed: int,
    label: str,
    quest_id: str,
    charter: ResearchCharterVersion,
    action: ResearchActionProposal,
    state: ResearchStateGraph,
    directive: object,
    events: list[ResearchEvent],
) -> TransitionDecision:
    return TransitionDecision(
        transition_id=_stable_id("transition", seed, label),
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        source_graph_sha256=state.snapshot_sha256,
        selected_action_ref=action.object_ref,
        directive=directive,
        evidence_refs=(
            EvidenceRef(
                kind=EvidenceKind.INCONCLUSIVE,
                object_sha256=_sha256(f"{seed}:{label}:evidence"),
            ),
        ),
        evidence_event_sha256s=tuple(sorted({events[-1].event_sha256, events[-2].event_sha256})),
        budget_receipt_sha256=_sha256(f"{seed}:{label}:budget"),
        risk_receipt_sha256=_sha256(f"{seed}:{label}:risk"),
        policy_receipt_sha256=_sha256(f"{seed}:{label}:policy"),
        reason_codes=("property_generated",),
        rationale=f"property-generated {label} decision",
        decided_by_principal_id="property:decision",
        decided_at=_BASE_TIME + timedelta(seconds=seed * 100 + len(events) + 1),
    )


@dataclass(frozen=True)
class _StreamCase:
    events: tuple[ResearchEvent, ...]
    objects: dict[str, KernelObject]


def _build_stream(seed: int, topology: str, reject_action: bool) -> _StreamCase:
    quest_id = _stable_id("qst", seed, "quest")
    root_branch_id = _stable_id("rbr", seed, "root")
    events: list[ResearchEvent] = []
    objects: dict[str, KernelObject] = {}
    state = empty_state()

    charter = ResearchCharterVersion(
        quest_id=quest_id,
        charter_id=_stable_id("charter", seed, "charter"),
        version=1,
        mission=f"bounded property mission {seed}",
        value_boundaries=("scientific_integrity",),
        included_scopes=("bounded_research",),
        excluded_scopes=("unbounded_outward_action",),
        allowed_action_classes=("analysis", "transition"),
        safety_policy_sha256=_sha256(f"{seed}:safety"),
        ethics_policy_sha256=_sha256(f"{seed}:ethics"),
        license_policy_sha256=_sha256(f"{seed}:license"),
        privacy_policy_sha256=_sha256(f"{seed}:privacy"),
        egress_policy_sha256=_sha256(f"{seed}:egress"),
        budget_policy_sha256=_sha256(f"{seed}:budget"),
        approval_policy_sha256=_sha256(f"{seed}:approval"),
        publication_policy_sha256=_sha256(f"{seed}:publication"),
        amendment_principal_ids=("human:owner",),
        emergency_stop_principal_ids=("human:owner",),
        authorized_by_principal_id="human:owner",
        authority_receipt_sha256=_sha256(f"{seed}:charter-authority"),
        authorized_at=_BASE_TIME + timedelta(seconds=seed * 100),
    )
    objects[charter.object_sha256] = charter
    event = _event(
        events,
        quest_id=quest_id,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter.object_ref,
            root_branch_id=root_branch_id,
        ),
        seed=seed,
    )
    events.append(event)
    state = reduce_event(state, event, objects)

    problem = ResearchProblemVersion(
        problem_id=_stable_id("problem", seed, "problem"),
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        version=1,
        title=f"property problem {seed}",
        statement="Which bounded uncertainty should be reduced?",
        scope="synthetic replay acceptance",
        importance_rationale="exercises the authoritative replay contract",
        unknowns=("bounded_unknown",),
        semantic_delta="initial problem version",
        authored_by_principal_id="fixture:problem-author",
        authored_at=_BASE_TIME + timedelta(seconds=seed * 100 + 2),
    )
    objects[problem.object_sha256] = problem
    event = _event(
        events,
        quest_id=quest_id,
        event_type=EventType.PROBLEM_ADMITTED,
        payload=ProblemAdmittedPayload(
            problem_ref=problem.object_ref,
            branch_id=root_branch_id,
        ),
        seed=seed,
    )
    events.append(event)
    state = reduce_event(state, event, objects)

    question = ResearchQuestionVersion(
        question_id=_stable_id("question", seed, "question"),
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.COMPARATIVE,
        statement="Do the two frozen outcomes differ?",
        scope="synthetic replay acceptance",
        answer_space=("different", "indistinguishable"),
        scientific_value="tests graph-scoped question custody",
        falsifiability="the frozen observation can be inconclusive",
        semantic_delta="initial question version",
        authored_by_principal_id="fixture:question-author",
        authored_at=_BASE_TIME + timedelta(seconds=seed * 100 + 3),
    )
    objects[question.object_sha256] = question
    event = _event(
        events,
        quest_id=quest_id,
        event_type=EventType.QUESTION_ADMITTED,
        payload=QuestionAdmittedPayload(
            question_ref=question.object_ref,
            branch_id=root_branch_id,
        ),
        seed=seed,
    )
    events.append(event)
    state = reduce_event(state, event, objects)

    primary_action_kind = (
        ActionKind.STOP
        if topology == "stop"
        else ActionKind.FORK
        if topology in {"fork", "fork_backtrack"}
        else ActionKind.CONTINUE
    )
    action = ResearchActionProposal(
        action_id=_stable_id("action", seed, "action"),
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=events[-1].event_sha256,
        kind=primary_action_kind,
        epistemic_purpose="exercise a deterministic branch transition",
        candidate_outcomes=("different", "indistinguishable"),
        evidence_refs=(
            EvidenceRef(
                kind=EvidenceKind.NEGATIVE,
                object_sha256=_sha256(f"{seed}:negative-evidence"),
            ),
        ),
        cost_receipt_sha256=_sha256(f"{seed}:cost"),
        risk_receipt_sha256=_sha256(f"{seed}:action-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id="fixture:action-proposer",
        proposed_at=_BASE_TIME + timedelta(seconds=seed * 100 + 4),
    )
    objects[action.object_sha256] = action
    event = _event(
        events,
        quest_id=quest_id,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=action.object_ref,
            branch_id=root_branch_id,
        ),
        seed=seed,
    )
    events.append(event)
    state = reduce_event(state, event, objects)

    # No-execution branch transitions atomically authorize and apply their selected proposal.  An
    # explicit action decision is therefore emitted only for a non-transitioning linear case.
    if topology == "linear":
        if reject_action:
            event_type = EventType.ACTION_REJECTED
            payload = ActionRejectedPayload(
                action_id=action.action_id,
                branch_id=root_branch_id,
                reason_codes=("not_selected",),
            )
        else:
            event_type = EventType.ACTION_AUTHORIZED
            payload = ActionAuthorizedPayload(action_id=action.action_id, branch_id=root_branch_id)
        event = _event(
            events,
            quest_id=quest_id,
            event_type=event_type,
            payload=payload,
            seed=seed,
        )
        events.append(event)
        state = reduce_event(state, event, objects)

    if topology == "stop":
        decision = _transition_decision(
            seed=seed,
            label="stop",
            quest_id=quest_id,
            charter=charter,
            action=action,
            state=state,
            directive=StopDirective(
                branch_id=root_branch_id,
                stop_reason=StopReason.CURRENTLY_INDISTINGUISHABLE,
                reopen_conditions=("new_discriminating_capability",),
                unresolved_refs=(
                    EvidenceRef(
                        kind=EvidenceKind.INCONCLUSIVE,
                        object_sha256=_sha256(f"{seed}:stop-unresolved"),
                    ),
                ),
            ),
            events=events,
        )
        event = _event(
            events,
            quest_id=quest_id,
            event_type=EventType.STOP_COMMITTED,
            payload=StopCommittedPayload(decision=decision),
            seed=seed,
        )
        events.append(event)
        reduce_event(state, event, objects)
    elif topology in {"fork", "fork_backtrack"}:
        child_branch_ids = tuple(
            sorted(
                (
                    _stable_id("rbr", seed, "fork-a"),
                    _stable_id("rbr", seed, "fork-b"),
                )
            )
        )
        decision = _transition_decision(
            seed=seed,
            label="fork",
            quest_id=quest_id,
            charter=charter,
            action=action,
            state=state,
            directive=ForkDirective(
                source_branch_id=root_branch_id,
                child_branch_ids=child_branch_ids,
            ),
            events=events,
        )
        event = _event(
            events,
            quest_id=quest_id,
            event_type=EventType.FORK_COMMITTED,
            payload=ForkCommittedPayload(decision=decision),
            seed=seed,
        )
        events.append(event)
        state = reduce_event(state, event, objects)

        if topology == "fork_backtrack":
            activate_action = ResearchActionProposal(
                action_id=_stable_id("action", seed, "activate-action"),
                quest_id=quest_id,
                charter_ref=charter.object_ref,
                question_ref=question.object_ref,
                basis_tail_event_sha256=events[-1].event_sha256,
                kind=ActionKind.ACTIVATE,
                epistemic_purpose="activate an admitted child before further work",
                candidate_outcomes=("activate_child", "remain_admitted"),
                evidence_refs=(
                    EvidenceRef(
                        kind=EvidenceKind.INCONCLUSIVE,
                        object_sha256=_sha256(f"{seed}:activate-evidence"),
                    ),
                ),
                cost_receipt_sha256=_sha256(f"{seed}:activate-cost"),
                risk_receipt_sha256=_sha256(f"{seed}:activate-risk"),
                requested_authority_class="transition",
                proposed_by_principal_id="fixture:action-proposer",
                proposed_at=_BASE_TIME + timedelta(seconds=seed * 100 + len(events) + 1),
            )
            objects[activate_action.object_sha256] = activate_action
            event = _event(
                events,
                quest_id=quest_id,
                event_type=EventType.ACTION_PROPOSED,
                payload=ActionProposedPayload(
                    action_ref=activate_action.object_ref,
                    branch_id=child_branch_ids[0],
                ),
                seed=seed,
            )
            events.append(event)
            state = reduce_event(state, event, objects)
            decision = _transition_decision(
                seed=seed,
                label="activate",
                quest_id=quest_id,
                charter=charter,
                action=activate_action,
                state=state,
                directive=ActivateDirective(branch_id=child_branch_ids[0]),
                events=events,
            )
            event = _event(
                events,
                quest_id=quest_id,
                event_type=EventType.ACTIVATE_COMMITTED,
                payload=ActivateCommittedPayload(decision=decision),
                seed=seed,
            )
            events.append(event)
            state = reduce_event(state, event, objects)

            backtrack_action = ResearchActionProposal(
                action_id=_stable_id("action", seed, "backtrack-action"),
                quest_id=quest_id,
                charter_ref=charter.object_ref,
                question_ref=question.object_ref,
                basis_tail_event_sha256=events[-1].event_sha256,
                kind=ActionKind.BACKTRACK,
                epistemic_purpose="return to a strict historical branch checkpoint",
                candidate_outcomes=("new_branch_from_ancestor",),
                evidence_refs=(
                    EvidenceRef(
                        kind=EvidenceKind.INCONCLUSIVE,
                        object_sha256=_sha256(f"{seed}:backtrack-evidence"),
                    ),
                ),
                cost_receipt_sha256=_sha256(f"{seed}:backtrack-cost"),
                risk_receipt_sha256=_sha256(f"{seed}:backtrack-risk"),
                requested_authority_class="transition",
                proposed_by_principal_id="fixture:action-proposer",
                proposed_at=_BASE_TIME + timedelta(seconds=seed * 100 + len(events) + 1),
            )
            objects[backtrack_action.object_sha256] = backtrack_action
            event = _event(
                events,
                quest_id=quest_id,
                event_type=EventType.ACTION_PROPOSED,
                payload=ActionProposedPayload(
                    action_ref=backtrack_action.object_ref,
                    branch_id=child_branch_ids[0],
                ),
                seed=seed,
            )
            events.append(event)
            state = reduce_event(state, event, objects)
            decision = _transition_decision(
                seed=seed,
                label="backtrack",
                quest_id=quest_id,
                charter=charter,
                action=backtrack_action,
                state=state,
                directive=BacktrackDirective(
                    source_branch_id=child_branch_ids[0],
                    target_branch_id=root_branch_id,
                    target_event_sha256=events[2].event_sha256,
                    new_branch_id=_stable_id("rbr", seed, "backtrack-child"),
                ),
                events=events,
            )
            event = _event(
                events,
                quest_id=quest_id,
                event_type=EventType.BACKTRACK_COMMITTED,
                payload=BacktrackCommittedPayload(decision=decision),
                seed=seed,
            )
            events.append(event)
            reduce_event(state, event, objects)

    return _StreamCase(events=tuple(events), objects=objects)


@st.composite
def _stream_cases(draw: st.DrawFn) -> _StreamCase:
    seed = draw(st.integers(min_value=0, max_value=10_000))
    topology = draw(st.sampled_from(("linear", "stop", "fork", "fork_backtrack")))
    reject_action = draw(st.booleans())
    return _build_stream(seed, topology, reject_action)


@_PROPERTY_SETTINGS
@given(case=_stream_cases(), split_hint=st.integers(min_value=0, max_value=16))
def test_full_replay_equals_prefix_plus_incremental_suffix(
    case: _StreamCase,
    split_hint: int,
) -> None:
    split = min(split_hint, len(case.events))
    complete = replay(case.events, case.objects)
    incremental = replay(case.events[:split], case.objects)
    for event in case.events[split:]:
        incremental = reduce_event(incremental, event, case.objects)

    assert incremental == complete
    assert canonical_state_bytes(incremental) == canonical_state_bytes(complete)
    assert canonical_state_sha256(incremental) == canonical_state_sha256(complete)


@_PROPERTY_SETTINGS
@given(case=_stream_cases())
def test_pydantic_json_roundtrip_preserves_canonical_snapshot(case: _StreamCase) -> None:
    expected = replay(case.events, case.objects)
    events = tuple(
        ResearchEvent.model_validate_json(event.model_dump_json()) for event in case.events
    )
    objects = {
        object_id: type(obj).model_validate_json(obj.model_dump_json())
        for object_id, obj in case.objects.items()
    }
    actual = replay(events, objects)
    restored_snapshot = ResearchStateGraph.model_validate_json(actual.model_dump_json())

    assert actual == expected
    assert canonical_state_bytes(restored_snapshot) == canonical_state_bytes(expected)
    assert canonical_state_sha256(restored_snapshot) == expected.snapshot_sha256
    assert hashlib.sha256(expected.canonical_bytes()).hexdigest() == expected.snapshot_sha256


@_PROPERTY_SETTINGS
@given(case=_stream_cases(), event_hint=st.integers(min_value=1, max_value=16))
def test_replay_rejects_parent_tampering(case: _StreamCase, event_hint: int) -> None:
    event_index = min(event_hint, len(case.events) - 1)
    prefix = replay(case.events[:event_index], case.objects)
    event = case.events[event_index]
    tampered = event.model_copy(
        update={"parent_event_sha256": _sha256(f"tampered-parent:{event.parent_event_sha256}")}
    )

    with pytest.raises(InvalidTransitionError):
        reduce_event(prefix, tampered, case.objects)


@_PROPERTY_SETTINGS
@given(case=_stream_cases(), event_hint=st.integers(min_value=1, max_value=16))
def test_replay_rejects_stream_version_tampering(case: _StreamCase, event_hint: int) -> None:
    event_index = min(event_hint, len(case.events) - 1)
    prefix = replay(case.events[:event_index], case.objects)
    event = case.events[event_index]
    tampered = event.model_copy(update={"sequence": event.sequence + 1})

    with pytest.raises(InvalidTransitionError):
        reduce_event(prefix, tampered, case.objects)


@_PROPERTY_SETTINGS
@given(case=_stream_cases(), admission_hint=st.integers(min_value=0, max_value=3))
def test_replay_rejects_object_reference_hash_tampering(
    case: _StreamCase,
    admission_hint: int,
) -> None:
    event_index = admission_hint
    event = case.events[event_index]
    reference_field = (
        "charter_ref",
        "problem_ref",
        "question_ref",
        "action_ref",
    )[event_index]
    ref = getattr(event.payload, reference_field)
    tampered_ref = ref.model_copy(
        update={"object_sha256": _sha256(f"tampered-ref:{ref.object_sha256}")}
    )
    tampered_payload = event.payload.model_copy(update={reference_field: tampered_ref})
    tampered_event = event.model_copy(update={"payload": tampered_payload})
    prefix = replay(case.events[:event_index], case.objects)

    with pytest.raises(InvalidTransitionError):
        reduce_event(prefix, tampered_event, case.objects)


@_PROPERTY_SETTINGS
@given(case=_stream_cases(), admission_hint=st.integers(min_value=0, max_value=3))
def test_replay_rejects_tampered_object_catalog(
    case: _StreamCase,
    admission_hint: int,
) -> None:
    event_index = admission_hint
    object_sha256 = (
        case.events[0].payload.charter_ref.object_sha256,
        case.events[1].payload.problem_ref.object_sha256,
        case.events[2].payload.question_ref.object_sha256,
        case.events[3].payload.action_ref.object_sha256,
    )[event_index]
    catalog = dict(case.objects)
    obj = catalog[object_sha256]
    mutable_text_field = {
        "charter": "mission",
        "problem": "statement",
        "question": "statement",
        "action": "epistemic_purpose",
    }[obj.object_ref.object_kind.value]
    catalog[object_sha256] = obj.model_copy(
        update={mutable_text_field: f"tampered {getattr(obj, mutable_text_field)}"}
    )
    prefix = replay(case.events[:event_index], case.objects)

    with pytest.raises(InvalidTransitionError):
        reduce_event(prefix, case.events[event_index], catalog)
