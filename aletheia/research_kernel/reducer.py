"""Pure, deterministic reduction of committed research-kernel events.

The reducer is deliberately ignorant of persistence, models, schedulers, domains, and execution.
Object bytes live outside the event stream (ultimately in CAS), so callers must supply the frozen
object catalog used by an event.  A reference is admitted only after its type, quest scope, and
content hash have been recomputed from that catalog.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import Field, ValidationError

from aletheia.research_kernel.schemas import (
    ActionKind,
    EvidenceRef,
    KernelModel,
    KernelObject,
    KernelObjectKind,
    KernelObjectRef,
    ResearchEvent,
    StopDirective,
    StopReason,
    TransitionDecision,
    canonical_json_bytes,
    canonical_sha256,
    emergency_halt_action_ref,
)

REDUCER_VERSION = 1


class InvalidTransitionError(ValueError):
    """A committed event cannot legally follow the supplied graph state."""


class BranchLifecycle(str, Enum):
    """Lifecycle of a branch, independent of any fixed scientific stage order."""

    ADMITTED = "admitted"
    ACTIVE = "active"
    PAUSED = "paused"
    SUPERSEDED = "superseded"
    BACKTRACKED = "backtracked"
    STOPPED = "stopped"


class ActionLifecycle(str, Enum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class BranchSnapshot(KernelModel):
    branch_id: str
    parent_branch_id: str | None = None
    origin_event_sha256: str
    head_event_sha256: str
    lifecycle: BranchLifecycle = BranchLifecycle.ADMITTED
    problem_refs: tuple[KernelObjectRef, ...] = ()
    question_refs: tuple[KernelObjectRef, ...] = ()
    action_ids: tuple[str, ...] = ()
    backtrack_target_event_sha256: str | None = None


class BranchCheckpoint(KernelModel):
    """Historical branch projection at one committed event, retained for honest backtracking."""

    branch_id: str
    stream_version: int = Field(ge=1)
    event_sha256: str
    problem_refs: tuple[KernelObjectRef, ...] = ()
    question_refs: tuple[KernelObjectRef, ...] = ()


class ObjectAdmission(KernelModel):
    object_ref: KernelObjectRef
    branch_id: str
    admitted_event_sha256: str


class ActionSnapshot(KernelModel):
    action_ref: KernelObjectRef
    branch_id: str
    kind: ActionKind
    lifecycle: ActionLifecycle
    proposed_event_sha256: str
    decided_event_sha256: str | None = None


class ResearchStateGraph(KernelModel):
    """Canonical projection rebuilt exclusively from a quest's committed event stream."""

    snapshot_schema_version: Literal[1] = 1
    reducer_version: Literal[1] = REDUCER_VERSION
    quest_id: str | None = None
    stream_version: int = Field(default=0, ge=0)
    tail_event_sha256: str | None = None
    event_ids: tuple[str, ...] = ()
    event_sha256s: tuple[str, ...] = ()
    charter_ref: KernelObjectRef | None = None
    charter_history: tuple[KernelObjectRef, ...] = ()
    branches: tuple[BranchSnapshot, ...] = ()
    branch_checkpoints: tuple[BranchCheckpoint, ...] = ()
    opportunities: tuple[ObjectAdmission, ...] = ()
    problems: tuple[ObjectAdmission, ...] = ()
    questions: tuple[ObjectAdmission, ...] = ()
    actions: tuple[ActionSnapshot, ...] = ()
    transition_decisions: tuple[TransitionDecision, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    terminal: bool = False
    terminal_event_sha256: str | None = None

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


def empty_state() -> ResearchStateGraph:
    """Return the one canonical empty graph state."""

    return ResearchStateGraph()


def _value(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _object_id(ref: KernelObjectRef) -> str:
    return ref.object_id


def _event_type(event: ResearchEvent) -> str:
    return _value(event.event_type)


def _event_hash(event: ResearchEvent) -> str:
    return event.event_sha256


def _branches(state: ResearchStateGraph) -> dict[str, BranchSnapshot]:
    return {branch.branch_id: branch for branch in state.branches}


def _actions(state: ResearchStateGraph) -> dict[str, ActionSnapshot]:
    return {_object_id(action.action_ref): action for action in state.actions}


def _ref_key(ref: KernelObjectRef) -> tuple[str, str]:
    return (ref.object_id, ref.object_sha256)


def _admissions(state: ResearchStateGraph) -> dict[tuple[str, str], ObjectAdmission]:
    return {
        _ref_key(admission.object_ref): admission
        for admission in (*state.opportunities, *state.problems, *state.questions)
    }


def _evidence_key(ref: EvidenceRef) -> tuple[str, str, str]:
    return (ref.kind.value, ref.object_sha256, ref.object_id or "")


def _merge_evidence(
    existing: Iterable[EvidenceRef], added: Iterable[EvidenceRef]
) -> tuple[EvidenceRef, ...]:
    by_key = {_evidence_key(ref): ref for ref in (*tuple(existing), *tuple(added))}
    return tuple(by_key[key] for key in sorted(by_key))


def _sorted(values: Iterable[KernelModel], key) -> tuple[Any, ...]:
    return tuple(sorted(values, key=key))


def _assert_active(branches: Mapping[str, BranchSnapshot], branch_id: str) -> BranchSnapshot:
    branch = branches.get(branch_id)
    if branch is None:
        raise InvalidTransitionError(f"unknown branch: {branch_id}")
    if branch.lifecycle is not BranchLifecycle.ACTIVE:
        raise InvalidTransitionError(f"branch {branch_id} is {branch.lifecycle.value}, not active")
    return branch


def _assert_nonterminal(branches: Mapping[str, BranchSnapshot], branch_id: str) -> BranchSnapshot:
    branch = branches.get(branch_id)
    if branch is None:
        raise InvalidTransitionError(f"unknown branch: {branch_id}")
    if branch.lifecycle not in {
        BranchLifecycle.ADMITTED,
        BranchLifecycle.ACTIVE,
        BranchLifecycle.PAUSED,
    }:
        raise InvalidTransitionError(f"branch {branch_id} is terminal: {branch.lifecycle.value}")
    return branch


def _assert_branch_acyclic(branches: Mapping[str, BranchSnapshot]) -> None:
    for branch_id in branches:
        seen: set[str] = set()
        current: str | None = branch_id
        while current is not None:
            if current in seen:
                raise InvalidTransitionError("branch lineage contains a cycle")
            seen.add(current)
            branch = branches.get(current)
            if branch is None:
                raise InvalidTransitionError(f"branch parent does not exist: {current}")
            current = branch.parent_branch_id


def _visible_from_branch(
    branches: Mapping[str, BranchSnapshot], admitted_branch_id: str, branch_id: str
) -> bool:
    current: str | None = branch_id
    while current is not None:
        if current == admitted_branch_id:
            return True
        current_branch = branches.get(current)
        current = current_branch.parent_branch_id if current_branch is not None else None
    return False


def _catalog_object(
    ref: KernelObjectRef,
    objects: Mapping[str, KernelObject],
    *,
    quest_id: str,
    expected_kind: KernelObjectKind,
) -> KernelObject:
    object_id = _object_id(ref)
    # Stable lineage ids may have many immutable versions.  Catalog lookup therefore uses the
    # version's content identity, never the stable object id.
    obj = objects.get(ref.object_sha256)
    if obj is None:
        raise InvalidTransitionError(f"object catalog is missing {object_id}@{ref.object_sha256}")
    try:
        # ``model_copy`` and ``model_construct`` intentionally bypass Pydantic validation.  The
        # reducer is an authority boundary, so never trust an in-memory model merely because its
        # Python type looks right.
        obj = type(obj).model_validate(obj.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise InvalidTransitionError(f"catalog object is invalid: {object_id}") from exc
    actual_ref = obj.object_ref
    if actual_ref != ref:
        raise InvalidTransitionError(f"object reference does not match catalog bytes: {object_id}")
    if actual_ref.quest_id != quest_id:
        raise InvalidTransitionError(f"cross-quest object reference: {object_id}")
    if actual_ref.object_kind is not expected_kind:
        raise InvalidTransitionError(
            f"object {object_id} has kind {actual_ref.object_kind.value}, expected {expected_kind.value}"
        )
    return obj


def _validate_envelope(state: ResearchStateGraph, event: ResearchEvent) -> None:
    if event.reducer_version != REDUCER_VERSION:
        raise InvalidTransitionError(
            f"unsupported reducer version: {event.reducer_version}; expected {REDUCER_VERSION}"
        )
    expected_sequence = state.stream_version + 1
    if event.sequence != expected_sequence:
        raise InvalidTransitionError(
            f"non-contiguous event sequence: got {event.sequence}, expected {expected_sequence}"
        )
    if event.parent_event_sha256 != state.tail_event_sha256:
        raise InvalidTransitionError("event parent does not match the current stream tail")
    if event.event_id in state.event_ids:
        raise InvalidTransitionError(f"duplicate event id: {event.event_id}")
    if _event_hash(event) in state.event_sha256s:
        raise InvalidTransitionError(f"duplicate event hash: {_event_hash(event)}")
    if state.quest_id is not None and event.quest_id != state.quest_id:
        raise InvalidTransitionError(
            f"event quest {event.quest_id} does not match graph quest {state.quest_id}"
        )
    if state.terminal:
        raise InvalidTransitionError("no event may follow a terminal research graph")


def _replace_branch(
    branches: dict[str, BranchSnapshot], branch: BranchSnapshot
) -> dict[str, BranchSnapshot]:
    result = dict(branches)
    result[branch.branch_id] = branch
    return result


def _with_branch_head(branch: BranchSnapshot, event_sha256: str) -> BranchSnapshot:
    return branch.model_copy(update={"head_event_sha256": event_sha256})


def _payload_ref(payload: Any, name: str) -> KernelObjectRef:
    ref = getattr(payload, name)
    if not isinstance(ref, KernelObjectRef):
        raise InvalidTransitionError(f"{name} is not a kernel object reference")
    return ref


def _finalize(
    state: ResearchStateGraph,
    event: ResearchEvent,
    *,
    branches: Mapping[str, BranchSnapshot],
    problems: Iterable[ObjectAdmission] | None = None,
    questions: Iterable[ObjectAdmission] | None = None,
    actions: Iterable[ActionSnapshot] | None = None,
    decisions: Iterable[TransitionDecision] | None = None,
    charter_ref: KernelObjectRef | None = None,
    charter_history: Iterable[KernelObjectRef] | None = None,
    opportunities: Iterable[ObjectAdmission] | None = None,
    evidence_refs: Iterable[EvidenceRef] | None = None,
) -> ResearchStateGraph:
    _assert_branch_acyclic(branches)
    event_sha256 = _event_hash(event)
    checkpoints = list(state.branch_checkpoints)
    checkpoint_keys = {(item.branch_id, item.event_sha256) for item in checkpoints}
    for branch in branches.values():
        key = (branch.branch_id, event_sha256)
        if branch.head_event_sha256 == event_sha256 and key not in checkpoint_keys:
            checkpoints.append(
                BranchCheckpoint(
                    branch_id=branch.branch_id,
                    stream_version=event.sequence,
                    event_sha256=event_sha256,
                    problem_refs=branch.problem_refs,
                    question_refs=branch.question_refs,
                )
            )
    terminal = bool(branches) and not any(
        branch.lifecycle
        in {BranchLifecycle.ADMITTED, BranchLifecycle.ACTIVE, BranchLifecycle.PAUSED}
        for branch in branches.values()
    )
    result = ResearchStateGraph(
        quest_id=event.quest_id,
        stream_version=event.sequence,
        tail_event_sha256=event_sha256,
        event_ids=(*state.event_ids, event.event_id),
        event_sha256s=(*state.event_sha256s, event_sha256),
        charter_ref=charter_ref if charter_ref is not None else state.charter_ref,
        charter_history=tuple(
            charter_history if charter_history is not None else state.charter_history
        ),
        branches=_sorted(branches.values(), lambda branch: branch.branch_id),
        branch_checkpoints=_sorted(
            checkpoints, lambda checkpoint: (checkpoint.stream_version, checkpoint.branch_id)
        ),
        opportunities=_sorted(
            opportunities if opportunities is not None else state.opportunities,
            lambda admission: (
                _object_id(admission.object_ref),
                admission.object_ref.object_sha256,
            ),
        ),
        problems=_sorted(
            problems if problems is not None else state.problems,
            lambda admission: _object_id(admission.object_ref),
        ),
        questions=_sorted(
            questions if questions is not None else state.questions,
            lambda admission: _object_id(admission.object_ref),
        ),
        actions=_sorted(
            actions if actions is not None else state.actions,
            lambda action: _object_id(action.action_ref),
        ),
        transition_decisions=tuple(
            decisions if decisions is not None else state.transition_decisions
        ),
        evidence_refs=tuple(evidence_refs if evidence_refs is not None else state.evidence_refs),
        terminal=terminal,
        terminal_event_sha256=event_sha256 if terminal else None,
    )
    assert_graph_invariants(result)
    return result


def _reduce_charter(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    if state.stream_version != 0 or state.charter_ref is not None:
        raise InvalidTransitionError("a charter may only be activated by the genesis event")
    payload = event.payload
    charter_ref = _payload_ref(payload, "charter_ref")
    _catalog_object(
        charter_ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=KernelObjectKind.CHARTER,
    )
    branch_id = payload.root_branch_id
    event_sha256 = _event_hash(event)
    branch = BranchSnapshot(
        branch_id=branch_id,
        origin_event_sha256=event_sha256,
        head_event_sha256=event_sha256,
        lifecycle=BranchLifecycle.ACTIVE,
    )
    return _finalize(
        state,
        event,
        branches={branch_id: branch},
        charter_ref=charter_ref,
        charter_history=(charter_ref,),
    )


def _reduce_charter_revision(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    if state.charter_ref is None:
        raise InvalidTransitionError("cannot revise a missing charter")
    ref = _payload_ref(event.payload, "charter_ref")
    revised = _catalog_object(
        ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=KernelObjectKind.CHARTER,
    )
    current = _catalog_object(
        state.charter_ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=KernelObjectKind.CHARTER,
    )
    if revised.charter_id != current.charter_id:
        raise InvalidTransitionError("charter revision cannot change the charter lineage id")
    if revised.version != current.version + 1:
        raise InvalidTransitionError("charter revision version is not contiguous")
    if revised.revision_parent_sha256 != state.charter_ref.object_sha256:
        raise InvalidTransitionError("charter revision does not bind the current charter bytes")
    return _finalize(
        state,
        event,
        branches=_branches(state),
        charter_ref=ref,
        charter_history=(*state.charter_history, ref),
    )


def _require_current_charter(state: ResearchStateGraph, obj: KernelObject) -> None:
    if state.charter_ref is None or getattr(obj, "charter_ref", None) != state.charter_ref:
        raise InvalidTransitionError("object does not bind the graph's current charter version")


def _reduce_opportunity(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    payload = event.payload
    branches = _branches(state)
    branch = _assert_active(branches, payload.branch_id)
    ref = _payload_ref(payload, "opportunity_ref")
    if _ref_key(ref) in _admissions(state):
        raise InvalidTransitionError(
            f"duplicate opportunity version: {ref.object_id}@{ref.object_sha256}"
        )
    if any(item.object_ref.object_id == ref.object_id for item in state.opportunities):
        raise InvalidTransitionError(f"duplicate opportunity id: {ref.object_id}")
    obj = _catalog_object(
        ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=KernelObjectKind.OPPORTUNITY,
    )
    _require_current_charter(state, obj)
    event_sha256 = _event_hash(event)
    admission = ObjectAdmission(
        object_ref=ref,
        branch_id=branch.branch_id,
        admitted_event_sha256=event_sha256,
    )
    branches = _replace_branch(branches, _with_branch_head(branch, event_sha256))
    return _finalize(
        state,
        event,
        branches=branches,
        opportunities=(*state.opportunities, admission),
        evidence_refs=_merge_evidence(state.evidence_refs, obj.evidence_refs),
    )


def _latest_lineage_admission(
    state: ResearchStateGraph, admissions: Iterable[ObjectAdmission], object_id: str
) -> ObjectAdmission | None:
    matches = [item for item in admissions if item.object_ref.object_id == object_id]
    if not matches:
        return None
    order = {event_sha256: index for index, event_sha256 in enumerate(state.event_sha256s)}
    return max(matches, key=lambda item: order[item.admitted_event_sha256])


def _validate_version_lineage(
    state: ResearchStateGraph,
    branches: Mapping[str, BranchSnapshot],
    branch: BranchSnapshot,
    obj: KernelObject,
    objects: Mapping[str, KernelObject],
    admissions: tuple[ObjectAdmission, ...],
    *,
    fork_field: str,
) -> None:
    latest = _latest_lineage_admission(state, admissions, obj.object_ref.object_id)
    forked_from_sha256 = getattr(obj, fork_field)
    if obj.version == 1:
        if latest is not None:
            raise InvalidTransitionError("lineage version 1 cannot reuse an admitted object id")
        if forked_from_sha256 is None:
            return
        fork_parent = next(
            (item for item in admissions if item.object_ref.object_sha256 == forked_from_sha256),
            None,
        )
        if fork_parent is None or not _visible_from_branch(
            branches, fork_parent.branch_id, branch.branch_id
        ):
            raise InvalidTransitionError("fork lineage parent is not an exact visible version")
        if fork_parent.object_ref.object_id == obj.object_ref.object_id:
            raise InvalidTransitionError("fork must begin a new stable lineage id")
        return
    if latest is None:
        raise InvalidTransitionError("revised object has no admitted lineage parent")
    if not _visible_from_branch(branches, latest.branch_id, branch.branch_id):
        raise InvalidTransitionError("revision parent is not visible from this branch")
    if obj.revision_parent_sha256 != latest.object_ref.object_sha256:
        raise InvalidTransitionError("revision does not bind the one current lineage head")
    parent = _catalog_object(
        latest.object_ref,
        objects,
        quest_id=obj.quest_id,
        expected_kind=obj.object_ref.object_kind,
    )
    if obj.version != parent.version + 1:
        raise InvalidTransitionError("object revision version is not contiguous")


def _reduce_admission(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
    *,
    ref_name: str,
    expected_kind: KernelObjectKind,
) -> ResearchStateGraph:
    payload = event.payload
    branches = _branches(state)
    branch = _assert_active(branches, payload.branch_id)
    ref = _payload_ref(payload, ref_name)
    object_id = _object_id(ref)
    if _ref_key(ref) in _admissions(state) or object_id in _actions(state):
        raise InvalidTransitionError(f"duplicate object version: {object_id}@{ref.object_sha256}")
    obj = _catalog_object(
        ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=expected_kind,
    )
    _require_current_charter(state, obj)
    lineage_admissions = (
        state.problems if expected_kind is KernelObjectKind.PROBLEM else state.questions
    )
    _validate_version_lineage(
        state,
        branches,
        branch,
        obj,
        objects,
        lineage_admissions,
        fork_field=(
            "forked_from_problem_sha256"
            if expected_kind is KernelObjectKind.PROBLEM
            else "forked_from_question_sha256"
        ),
    )
    if expected_kind is KernelObjectKind.PROBLEM:
        for opportunity_ref in obj.opportunity_refs:
            opportunity = _admissions(state).get(_ref_key(opportunity_ref))
            if opportunity is None or not _visible_from_branch(
                branches, opportunity.branch_id, branch.branch_id
            ):
                raise InvalidTransitionError(
                    f"problem {object_id} references a non-admitted opportunity"
                )
    if expected_kind is KernelObjectKind.QUESTION:
        problem = _admissions(state).get(_ref_key(obj.problem_ref))
        if (
            problem is None
            or problem.object_ref != obj.problem_ref
            or not _visible_from_branch(branches, problem.branch_id, branch.branch_id)
        ):
            raise InvalidTransitionError(
                f"question {object_id} does not reference an exact visible problem version"
            )
    event_sha256 = _event_hash(event)
    admission = ObjectAdmission(
        object_ref=ref,
        branch_id=branch.branch_id,
        admitted_event_sha256=event_sha256,
    )
    branch_update: dict[str, Any] = {"head_event_sha256": event_sha256}
    if expected_kind is KernelObjectKind.PROBLEM:
        branch_update["problem_refs"] = (*branch.problem_refs, ref)
        problems = (*state.problems, admission)
        questions = state.questions
    else:
        branch_update["question_refs"] = (*branch.question_refs, ref)
        problems = state.problems
        questions = (*state.questions, admission)
    branches = _replace_branch(branches, branch.model_copy(update=branch_update))
    return _finalize(
        state,
        event,
        branches=branches,
        problems=problems,
        questions=questions,
        evidence_refs=_merge_evidence(state.evidence_refs, obj.evidence_refs),
    )


def _reduce_action_proposed(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    payload = event.payload
    branches = _branches(state)
    branch = _assert_nonterminal(branches, payload.branch_id)
    ref = _payload_ref(payload, "action_ref")
    action_id = _object_id(ref)
    if action_id in _actions(state) or any(
        admitted_id == action_id for admitted_id, _ in _admissions(state)
    ):
        raise InvalidTransitionError(f"duplicate object id: {action_id}")
    obj = _catalog_object(
        ref,
        objects,
        quest_id=event.quest_id,
        expected_kind=KernelObjectKind.ACTION,
    )
    _require_current_charter(state, obj)
    if obj.basis_tail_event_sha256 != state.tail_event_sha256:
        raise InvalidTransitionError("action proposal was formed from a stale graph tail")
    question = _admissions(state).get(_ref_key(obj.question_ref))
    if (
        question is None
        or question.object_ref != obj.question_ref
        or not _visible_from_branch(branches, question.branch_id, branch.branch_id)
    ):
        raise InvalidTransitionError(
            f"action {action_id} does not reference an exact visible question version"
        )
    event_sha256 = _event_hash(event)
    action = ActionSnapshot(
        action_ref=ref,
        branch_id=branch.branch_id,
        kind=obj.kind,
        lifecycle=ActionLifecycle.PROPOSED,
        proposed_event_sha256=event_sha256,
    )
    branches = _replace_branch(
        branches,
        branch.model_copy(
            update={
                "head_event_sha256": event_sha256,
                "action_ids": (*branch.action_ids, action_id),
            }
        ),
    )
    for alternative_ref in obj.alternative_action_refs:
        alternative = _actions(state).get(alternative_ref.object_id)
        if alternative is None or alternative.action_ref != alternative_ref:
            raise InvalidTransitionError("action references an unknown alternative action version")
    return _finalize(
        state,
        event,
        branches=branches,
        actions=(*state.actions, action),
        evidence_refs=_merge_evidence(state.evidence_refs, obj.evidence_refs),
    )


def _reduce_action_decision(
    state: ResearchStateGraph,
    event: ResearchEvent,
    *,
    lifecycle: ActionLifecycle,
) -> ResearchStateGraph:
    payload = event.payload
    branches = _branches(state)
    branch = _assert_nonterminal(branches, payload.branch_id)
    action_id = payload.action_id
    actions = _actions(state)
    action = actions.get(action_id)
    if action is None:
        raise InvalidTransitionError(f"unknown action: {action_id}")
    if action.branch_id != branch.branch_id:
        raise InvalidTransitionError(f"action {action_id} belongs to another branch")
    if action.lifecycle is not ActionLifecycle.PROPOSED:
        raise InvalidTransitionError(f"action {action_id} is already {action.lifecycle.value}")
    event_sha256 = _event_hash(event)
    actions[action_id] = action.model_copy(
        update={"lifecycle": lifecycle, "decided_event_sha256": event_sha256}
    )
    branches = _replace_branch(branches, _with_branch_head(branch, event_sha256))
    return _finalize(state, event, branches=branches, actions=actions.values())


def _directive_branch_id(decision: TransitionDecision) -> str:
    directive = decision.directive
    return getattr(directive, "branch_id", getattr(directive, "source_branch_id", ""))


def _validate_decision_context(state: ResearchStateGraph, decision: TransitionDecision) -> None:
    if decision.source_graph_sha256 != state.snapshot_sha256:
        raise InvalidTransitionError("transition decision was made from a different graph snapshot")
    if decision.quest_id != state.quest_id or decision.charter_ref != state.charter_ref:
        raise InvalidTransitionError("transition decision has the wrong quest or charter authority")
    if any(
        existing.transition_id == decision.transition_id for existing in state.transition_decisions
    ):
        raise InvalidTransitionError(f"duplicate transition id: {decision.transition_id}")
    known_events = set(state.event_sha256s)
    missing = set(decision.evidence_event_sha256s) - known_events
    if missing:
        raise InvalidTransitionError(
            f"transition decision references unknown evidence events: {sorted(missing)}"
        )


def _validate_decision(state: ResearchStateGraph, decision: TransitionDecision) -> ActionSnapshot:
    _validate_decision_context(state, decision)
    action = _actions(state).get(decision.selected_action_ref.object_id)
    if action is None or action.action_ref != decision.selected_action_ref:
        raise InvalidTransitionError("transition selected an unknown action version")
    if action.lifecycle is not ActionLifecycle.PROPOSED:
        raise InvalidTransitionError("transition selected an action that is no longer proposed")
    if action.branch_id != _directive_branch_id(decision):
        raise InvalidTransitionError("transition action and directive belong to different branches")
    expected_action_kinds = {
        "continue": ActionKind.CONTINUE,
        "activate": ActionKind.ACTIVATE,
        "refine": ActionKind.REFINE,
        "fork": ActionKind.FORK,
        "backtrack": ActionKind.BACKTRACK,
        "pause": ActionKind.PAUSE,
        "stop": ActionKind.STOP,
    }
    expected_kind = expected_action_kinds.get(decision.directive.kind)
    if expected_kind is not None and action.kind is not expected_kind:
        raise InvalidTransitionError(
            f"{decision.directive.kind} decision selected a {action.kind.value} action"
        )
    for alternative in decision.rejected_alternatives:
        candidate = _actions(state).get(alternative.action_ref.object_id)
        if candidate is None or candidate.action_ref != alternative.action_ref:
            raise InvalidTransitionError("transition rejects an unknown alternative action")
        if candidate.action_ref == decision.selected_action_ref:
            raise InvalidTransitionError("selected action cannot also be a rejected alternative")
    return action


def _applied_actions(
    state: ResearchStateGraph,
    decision: TransitionDecision,
    event_sha256: str,
) -> tuple[ActionSnapshot, ...]:
    action = _validate_decision(state, decision)
    actions = _actions(state)
    actions[action.action_ref.object_id] = action.model_copy(
        update={
            "lifecycle": ActionLifecycle.APPLIED,
            "decided_event_sha256": event_sha256,
        }
    )
    return tuple(actions.values())


def _decision_finalize(
    state: ResearchStateGraph,
    event: ResearchEvent,
    branches: Mapping[str, BranchSnapshot],
    decision: TransitionDecision,
) -> ResearchStateGraph:
    event_sha256 = _event_hash(event)
    directive_evidence = getattr(decision.directive, "unresolved_refs", ())
    return _finalize(
        state,
        event,
        branches=branches,
        actions=_applied_actions(state, decision, event_sha256),
        decisions=(*state.transition_decisions, decision),
        evidence_refs=_merge_evidence(
            state.evidence_refs, (*decision.evidence_refs, *directive_evidence)
        ),
    )


def _reduce_fork(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    payload = event.payload
    _validate_decision(state, payload.decision)
    directive = payload.decision.directive
    branches = _branches(state)
    source = _assert_active(branches, directive.source_branch_id)
    child_ids = tuple(directive.child_branch_ids)
    if len(child_ids) < 2 or len(set(child_ids)) != len(child_ids):
        raise InvalidTransitionError("a fork requires at least two unique child branches")
    duplicates = set(child_ids) & set(branches)
    if duplicates:
        raise InvalidTransitionError(f"fork branch ids already exist: {sorted(duplicates)}")
    event_sha256 = _event_hash(event)
    branches[source.branch_id] = source.model_copy(update={"head_event_sha256": event_sha256})
    for branch_id in child_ids:
        branches[branch_id] = BranchSnapshot(
            branch_id=branch_id,
            parent_branch_id=source.branch_id,
            origin_event_sha256=event_sha256,
            head_event_sha256=event_sha256,
            problem_refs=source.problem_refs,
            question_refs=source.question_refs,
        )
    return _decision_finalize(state, event, branches, payload.decision)


def _reduce_continue(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    decision = event.payload.decision
    _validate_decision(state, decision)
    branches = _branches(state)
    branch = _assert_active(branches, decision.directive.branch_id)
    branches = _replace_branch(branches, _with_branch_head(branch, _event_hash(event)))
    return _decision_finalize(state, event, branches, decision)


def _reduce_activate(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    decision = event.payload.decision
    _validate_decision(state, decision)
    branches = _branches(state)
    branch = branches.get(decision.directive.branch_id)
    if branch is None:
        raise InvalidTransitionError(f"unknown branch: {decision.directive.branch_id}")
    if branch.lifecycle not in {BranchLifecycle.ADMITTED, BranchLifecycle.PAUSED}:
        raise InvalidTransitionError(
            f"only admitted or paused branches may activate, got {branch.lifecycle.value}"
        )
    branches[branch.branch_id] = branch.model_copy(
        update={
            "head_event_sha256": _event_hash(event),
            "lifecycle": BranchLifecycle.ACTIVE,
        }
    )
    return _decision_finalize(state, event, branches, decision)


def _reduce_refine(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    decision = event.payload.decision
    _validate_decision(state, decision)
    directive = decision.directive
    branches = _branches(state)
    source = _assert_active(branches, directive.source_branch_id)
    if directive.child_branch_id in branches:
        raise InvalidTransitionError(
            f"refine branch id already exists: {directive.child_branch_id}"
        )
    event_sha256 = _event_hash(event)
    branches[source.branch_id] = source.model_copy(
        update={
            "head_event_sha256": event_sha256,
            "lifecycle": BranchLifecycle.SUPERSEDED,
        }
    )
    branches[directive.child_branch_id] = BranchSnapshot(
        branch_id=directive.child_branch_id,
        parent_branch_id=source.branch_id,
        origin_event_sha256=event_sha256,
        head_event_sha256=event_sha256,
        lifecycle=BranchLifecycle.ACTIVE,
        problem_refs=source.problem_refs,
        question_refs=source.question_refs,
    )
    return _decision_finalize(state, event, branches, decision)


def _reduce_pause(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    decision = event.payload.decision
    _validate_decision(state, decision)
    branches = _branches(state)
    branch = _assert_active(branches, decision.directive.branch_id)
    branches[branch.branch_id] = branch.model_copy(
        update={
            "head_event_sha256": _event_hash(event),
            "lifecycle": BranchLifecycle.PAUSED,
        }
    )
    return _decision_finalize(state, event, branches, decision)


def _is_branch_ancestor(
    branches: Mapping[str, BranchSnapshot], ancestor_id: str, branch_id: str
) -> bool:
    current = branches.get(branch_id)
    while current is not None and current.parent_branch_id is not None:
        if current.parent_branch_id == ancestor_id:
            return True
        current = branches.get(current.parent_branch_id)
    return False


def _reduce_backtrack(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    payload = event.payload
    _validate_decision(state, payload.decision)
    directive = payload.decision.directive
    branches = _branches(state)
    source = _assert_active(branches, directive.source_branch_id)
    target = branches.get(directive.target_branch_id)
    if target is None:
        raise InvalidTransitionError(f"unknown backtrack target: {directive.target_branch_id}")
    if target.branch_id == source.branch_id or not _is_branch_ancestor(
        branches, target.branch_id, source.branch_id
    ):
        raise InvalidTransitionError("backtrack target must be a strict ancestor branch")
    target_checkpoint = next(
        (
            checkpoint
            for checkpoint in state.branch_checkpoints
            if checkpoint.branch_id == target.branch_id
            and checkpoint.event_sha256 == directive.target_event_sha256
        ),
        None,
    )
    if target_checkpoint is None:
        raise InvalidTransitionError("backtrack target is not a checkpoint on the target branch")
    source_origin = next(
        (
            checkpoint
            for checkpoint in state.branch_checkpoints
            if checkpoint.branch_id == source.branch_id
            and checkpoint.event_sha256 == source.origin_event_sha256
        ),
        None,
    )
    if source_origin is None or target_checkpoint.stream_version > source_origin.stream_version:
        raise InvalidTransitionError(
            "backtrack target was not causally available when the source branch began"
        )
    if directive.target_event_sha256 == source.head_event_sha256:
        raise InvalidTransitionError("backtrack target must be a strict historical checkpoint")
    if directive.new_branch_id in branches:
        raise InvalidTransitionError(
            f"backtrack branch id already exists: {directive.new_branch_id}"
        )
    event_sha256 = _event_hash(event)
    branches[source.branch_id] = source.model_copy(
        update={"head_event_sha256": event_sha256, "lifecycle": BranchLifecycle.BACKTRACKED}
    )
    branches[directive.new_branch_id] = BranchSnapshot(
        branch_id=directive.new_branch_id,
        parent_branch_id=target.branch_id,
        origin_event_sha256=event_sha256,
        head_event_sha256=event_sha256,
        lifecycle=BranchLifecycle.ACTIVE,
        problem_refs=target_checkpoint.problem_refs,
        question_refs=target_checkpoint.question_refs,
        backtrack_target_event_sha256=directive.target_event_sha256,
    )
    return _decision_finalize(state, event, branches, payload.decision)


def _reduce_stop(state: ResearchStateGraph, event: ResearchEvent) -> ResearchStateGraph:
    payload = event.payload
    directive = payload.decision.directive
    if not isinstance(directive, StopDirective):
        raise InvalidTransitionError("stop event requires a typed stop directive")
    if directive.stop_reason is StopReason.EMERGENCY_STOP:
        _validate_decision_context(state, payload.decision)
        if state.charter_ref is None or payload.decision.selected_action_ref != (
            emergency_halt_action_ref(
                quest_id=event.quest_id,
                charter_ref=state.charter_ref,
            )
        ):
            raise InvalidTransitionError(
                "emergency stop lacks its deterministic global-halt authority marker"
            )
        if payload.decision.rejected_alternatives:
            raise InvalidTransitionError(
                "emergency stop cannot carry action-selection alternatives"
            )
        branches = _branches(state)
        if directive.branch_id not in branches:
            raise InvalidTransitionError(
                f"unknown emergency-stop initiating branch: {directive.branch_id}"
            )
        event_sha256 = _event_hash(event)
        for branch_id, branch in tuple(branches.items()):
            if branch.lifecycle in {
                BranchLifecycle.ADMITTED,
                BranchLifecycle.ACTIVE,
                BranchLifecycle.PAUSED,
            }:
                branches[branch_id] = branch.model_copy(
                    update={
                        "head_event_sha256": event_sha256,
                        "lifecycle": BranchLifecycle.STOPPED,
                    }
                )
        return _finalize(
            state,
            event,
            branches=branches,
            decisions=(*state.transition_decisions, payload.decision),
            evidence_refs=_merge_evidence(
                state.evidence_refs,
                (*payload.decision.evidence_refs, *directive.unresolved_refs),
            ),
        )

    _validate_decision(state, payload.decision)
    branches = _branches(state)
    branch = _assert_nonterminal(branches, directive.branch_id)
    if not payload.decision.evidence_refs and not payload.decision.evidence_event_sha256s:
        raise InvalidTransitionError("stop requires at least one exact evidence reference")
    if not directive.reopen_conditions and not directive.unresolved_refs:
        raise InvalidTransitionError(
            "stop must preserve reopen conditions or explicit unresolved evidence"
        )
    event_sha256 = _event_hash(event)
    branches[branch.branch_id] = branch.model_copy(
        update={"head_event_sha256": event_sha256, "lifecycle": BranchLifecycle.STOPPED}
    )
    return _decision_finalize(state, event, branches, payload.decision)


def reduce_event(
    state: ResearchStateGraph,
    event: ResearchEvent,
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    """Apply one committed event without mutating ``state`` or ``objects``."""

    try:
        state = ResearchStateGraph.model_validate(state.model_dump(mode="python"))
        event = ResearchEvent.model_validate(event.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise InvalidTransitionError("state or event failed authoritative revalidation") from exc
    assert_graph_invariants(state)
    _validate_envelope(state, event)
    event_type = _event_type(event)
    if state.stream_version == 0 and event_type != "charter_activated":
        raise InvalidTransitionError("the genesis event must activate a research charter")
    if event_type == "charter_activated":
        return _reduce_charter(state, event, objects)
    if state.charter_ref is None:
        raise InvalidTransitionError("research state has no active charter")
    if event_type == "charter_revised":
        return _reduce_charter_revision(state, event, objects)
    if event_type == "opportunity_recorded":
        return _reduce_opportunity(state, event, objects)
    if event_type == "problem_admitted":
        return _reduce_admission(
            state,
            event,
            objects,
            ref_name="problem_ref",
            expected_kind=KernelObjectKind.PROBLEM,
        )
    if event_type == "question_admitted":
        return _reduce_admission(
            state,
            event,
            objects,
            ref_name="question_ref",
            expected_kind=KernelObjectKind.QUESTION,
        )
    if event_type == "action_proposed":
        return _reduce_action_proposed(state, event, objects)
    if event_type == "action_authorized":
        return _reduce_action_decision(state, event, lifecycle=ActionLifecycle.AUTHORIZED)
    if event_type == "action_rejected":
        return _reduce_action_decision(state, event, lifecycle=ActionLifecycle.REJECTED)
    if event_type == "action_superseded":
        return _reduce_action_decision(state, event, lifecycle=ActionLifecycle.SUPERSEDED)
    if event_type == "continue_committed":
        return _reduce_continue(state, event)
    if event_type == "activate_committed":
        return _reduce_activate(state, event)
    if event_type == "refine_committed":
        return _reduce_refine(state, event)
    if event_type == "fork_committed":
        return _reduce_fork(state, event)
    if event_type == "backtrack_committed":
        return _reduce_backtrack(state, event)
    if event_type == "pause_committed":
        return _reduce_pause(state, event)
    if event_type == "stop_committed":
        return _reduce_stop(state, event)
    raise InvalidTransitionError(f"unsupported event type: {event_type}")


def replay(
    events: Iterable[ResearchEvent],
    objects: Mapping[str, KernelObject],
) -> ResearchStateGraph:
    """Replay a complete quest event stream into one byte-stable graph snapshot."""

    state = empty_state()
    for event in events:
        state = reduce_event(state, event, objects)
    return state


def assert_graph_invariants(state: ResearchStateGraph) -> None:
    """Validate invariants that must also hold for deserialized snapshots."""

    if state.stream_version != len(state.event_ids):
        raise InvalidTransitionError("stream version does not match event id count")
    if state.stream_version != len(state.event_sha256s):
        raise InvalidTransitionError("stream version does not match event hash count")
    if len(state.event_ids) != len(set(state.event_ids)):
        raise InvalidTransitionError("snapshot contains duplicate event ids")
    if len(state.event_sha256s) != len(set(state.event_sha256s)):
        raise InvalidTransitionError("snapshot contains duplicate event hashes")
    if state.tail_event_sha256 != (state.event_sha256s[-1] if state.event_sha256s else None):
        raise InvalidTransitionError("snapshot tail does not match its event hashes")
    if state.stream_version == 0:
        if (
            state.quest_id is not None
            or state.charter_ref is not None
            or state.charter_history
            or state.branches
            or state.opportunities
            or state.problems
            or state.questions
            or state.actions
            or state.transition_decisions
            or state.evidence_refs
        ):
            raise InvalidTransitionError("empty stream cannot contain research state")
        if state.terminal or state.terminal_event_sha256 is not None:
            raise InvalidTransitionError("empty stream cannot be terminal")
        return
    if state.quest_id is None or state.charter_ref is None or not state.branches:
        raise InvalidTransitionError("nonempty stream lacks quest, charter, or root branch")
    if state.charter_ref.quest_id != state.quest_id:
        raise InvalidTransitionError("snapshot charter belongs to another quest")
    if not state.charter_history or state.charter_history[-1] != state.charter_ref:
        raise InvalidTransitionError("snapshot current charter is not its retained history tail")
    if len({_ref_key(ref) for ref in state.charter_history}) != len(state.charter_history):
        raise InvalidTransitionError("snapshot contains a duplicate charter version")
    if any(
        ref.quest_id != state.quest_id or ref.object_kind is not KernelObjectKind.CHARTER
        for ref in state.charter_history
    ):
        raise InvalidTransitionError("snapshot charter history crosses quest or object kind")
    branches = _branches(state)
    if len(branches) != len(state.branches):
        raise InvalidTransitionError("snapshot contains duplicate branch ids")
    _assert_branch_acyclic(branches)
    checkpoint_keys = [
        (checkpoint.branch_id, checkpoint.event_sha256) for checkpoint in state.branch_checkpoints
    ]
    if len(checkpoint_keys) != len(set(checkpoint_keys)):
        raise InvalidTransitionError("snapshot contains duplicate branch checkpoints")
    for checkpoint in state.branch_checkpoints:
        if checkpoint.branch_id not in branches:
            raise InvalidTransitionError("checkpoint references an unknown branch")
        if checkpoint.event_sha256 not in state.event_sha256s:
            raise InvalidTransitionError("checkpoint references an unknown event")
        if checkpoint.stream_version > state.stream_version:
            raise InvalidTransitionError("checkpoint is newer than the graph stream")
        if state.event_sha256s[checkpoint.stream_version - 1] != checkpoint.event_sha256:
            raise InvalidTransitionError("checkpoint version does not identify its exact event")
    for branch in state.branches:
        if not any(
            checkpoint.branch_id == branch.branch_id
            and checkpoint.event_sha256 == branch.head_event_sha256
            for checkpoint in state.branch_checkpoints
        ):
            raise InvalidTransitionError("branch head lacks an exact historical checkpoint")
    expected_terminal = not any(
        branch.lifecycle
        in {BranchLifecycle.ADMITTED, BranchLifecycle.ACTIVE, BranchLifecycle.PAUSED}
        for branch in state.branches
    )
    if state.terminal != expected_terminal:
        raise InvalidTransitionError("snapshot terminal flag disagrees with active branches")
    expected_terminal_event = state.tail_event_sha256 if state.terminal else None
    if state.terminal_event_sha256 != expected_terminal_event:
        raise InvalidTransitionError("snapshot terminal event is inconsistent")
    if state.terminal and (
        not state.transition_decisions or state.transition_decisions[-1].directive.kind != "stop"
    ):
        raise InvalidTransitionError("only an explicit stop may terminalize a graph")
    version_keys = [
        _ref_key(admission.object_ref)
        for admission in (*state.opportunities, *state.problems, *state.questions)
    ] + [_ref_key(action.action_ref) for action in state.actions]
    if len(version_keys) != len(set(version_keys)):
        raise InvalidTransitionError("snapshot contains duplicate admitted object versions")
    if len([item for item in state.opportunities]) != len(
        {item.object_ref.object_id for item in state.opportunities}
    ):
        raise InvalidTransitionError("snapshot contains duplicate opportunity ids")
    for admissions, expected_kind in (
        (state.opportunities, KernelObjectKind.OPPORTUNITY),
        (state.problems, KernelObjectKind.PROBLEM),
        (state.questions, KernelObjectKind.QUESTION),
    ):
        for admission in admissions:
            if admission.object_ref.object_kind is not expected_kind:
                raise InvalidTransitionError("admission is stored in the wrong object projection")
    for admission in (*state.opportunities, *state.problems, *state.questions):
        if admission.branch_id not in branches:
            raise InvalidTransitionError("admitted object references an unknown branch")
        if admission.object_ref.quest_id != state.quest_id:
            raise InvalidTransitionError("admitted object belongs to another quest")
        if admission.admitted_event_sha256 not in state.event_sha256s:
            raise InvalidTransitionError("admission references an unknown event")
    actions = _actions(state)
    if len(actions) != len(state.actions):
        raise InvalidTransitionError("snapshot contains duplicate action ids")
    for action in state.actions:
        if action.branch_id not in branches:
            raise InvalidTransitionError("action references an unknown branch")
        if action.action_ref.quest_id != state.quest_id:
            raise InvalidTransitionError("action belongs to another quest")
        if action.action_ref.object_kind is not KernelObjectKind.ACTION:
            raise InvalidTransitionError("action projection contains a non-action reference")
        if action.proposed_event_sha256 not in state.event_sha256s:
            raise InvalidTransitionError("action proposal references an unknown event")
        if (action.lifecycle is ActionLifecycle.PROPOSED) != (action.decided_event_sha256 is None):
            raise InvalidTransitionError("action lifecycle disagrees with its decision event")
        if (
            action.decided_event_sha256 is not None
            and action.decided_event_sha256 not in state.event_sha256s
        ):
            raise InvalidTransitionError("action decision references an unknown event")
    admission_refs = {
        _ref_key(item.object_ref)
        for item in (*state.opportunities, *state.problems, *state.questions)
    }
    for checkpoint in state.branch_checkpoints:
        if any(_ref_key(ref) not in admission_refs for ref in checkpoint.problem_refs):
            raise InvalidTransitionError("checkpoint references a non-admitted problem version")
        if any(_ref_key(ref) not in admission_refs for ref in checkpoint.question_refs):
            raise InvalidTransitionError("checkpoint references a non-admitted question version")
    for branch in state.branches:
        if any(
            ref.object_kind is not KernelObjectKind.PROBLEM or _ref_key(ref) not in admission_refs
            for ref in branch.problem_refs
        ):
            raise InvalidTransitionError("branch references a non-admitted problem version")
        if any(
            ref.object_kind is not KernelObjectKind.QUESTION or _ref_key(ref) not in admission_refs
            for ref in branch.question_refs
        ):
            raise InvalidTransitionError("branch references a non-admitted question version")
        if len(branch.action_ids) != len(set(branch.action_ids)):
            raise InvalidTransitionError("branch contains duplicate action ids")
        for action_id in branch.action_ids:
            action = actions.get(action_id)
            if action is None or action.branch_id != branch.branch_id:
                raise InvalidTransitionError("branch references an action from another branch")
    for action in state.actions:
        if action.action_ref.object_id not in branches[action.branch_id].action_ids:
            raise InvalidTransitionError("action is absent from its owning branch")
    transition_ids = [decision.transition_id for decision in state.transition_decisions]
    if len(transition_ids) != len(set(transition_ids)):
        raise InvalidTransitionError("snapshot contains duplicate transition ids")
    applied_action_refs: set[tuple[str, str]] = set()
    for decision_index, decision in enumerate(state.transition_decisions):
        if decision.quest_id != state.quest_id or decision.charter_ref not in state.charter_history:
            raise InvalidTransitionError("transition decision crosses quest or charter history")
        if (
            isinstance(decision.directive, StopDirective)
            and decision.directive.stop_reason is StopReason.EMERGENCY_STOP
        ):
            expected_marker = emergency_halt_action_ref(
                quest_id=state.quest_id,
                charter_ref=decision.charter_ref,
            )
            if decision.selected_action_ref != expected_marker:
                raise InvalidTransitionError(
                    "emergency stop lacks its deterministic global-halt authority marker"
                )
            if decision.rejected_alternatives:
                raise InvalidTransitionError(
                    "emergency stop cannot carry action-selection alternatives"
                )
            if not state.terminal or decision_index != len(state.transition_decisions) - 1:
                raise InvalidTransitionError(
                    "emergency stop must be the terminal transition decision"
                )
            if any(ref not in state.event_sha256s for ref in decision.evidence_event_sha256s):
                raise InvalidTransitionError("transition decision cites an unknown event")
            continue
        action = actions.get(decision.selected_action_ref.object_id)
        if action is None or action.action_ref != decision.selected_action_ref:
            raise InvalidTransitionError("transition decision selected an unknown action")
        if action.lifecycle is not ActionLifecycle.APPLIED:
            raise InvalidTransitionError("transition decision action is not applied")
        applied_action_refs.add(_ref_key(action.action_ref))
        if any(ref not in state.event_sha256s for ref in decision.evidence_event_sha256s):
            raise InvalidTransitionError("transition decision cites an unknown event")
        for alternative in decision.rejected_alternatives:
            candidate = actions.get(alternative.action_ref.object_id)
            if candidate is None or candidate.action_ref != alternative.action_ref:
                raise InvalidTransitionError("transition rejected an unknown alternative")
    for action in state.actions:
        if (
            action.lifecycle is ActionLifecycle.APPLIED
            and _ref_key(action.action_ref) not in applied_action_refs
        ):
            raise InvalidTransitionError("applied action has no retained transition decision")
    if state.evidence_refs != _merge_evidence((), state.evidence_refs):
        raise InvalidTransitionError("snapshot evidence references are not canonical and unique")


def canonical_state_bytes(state: ResearchStateGraph) -> bytes:
    """Return the canonical bytes used for cross-process replay comparison."""

    assert_graph_invariants(state)
    return canonical_json_bytes(state)


def canonical_state_sha256(state: ResearchStateGraph) -> str:
    """Return the content identity of a valid research graph snapshot."""

    assert_graph_invariants(state)
    return canonical_sha256(state)
