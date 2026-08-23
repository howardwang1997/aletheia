from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
from pydantic import TypeAdapter, ValidationError

from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    ActionRejectedPayload,
    ActionSupersededPayload,
    ActivateDirective,
    BacktrackDirective,
    CharterActivatedPayload,
    CharterRevisedPayload,
    ContinueCommittedPayload,
    ContinueDirective,
    EvidenceKind,
    EvidenceRef,
    EventType,
    ForkDirective,
    KernelModel,
    KernelObjectEnvelope,
    KernelObjectKind,
    KernelObjectRef,
    Opportunity,
    OpportunityKind,
    OpportunityRecordedPayload,
    PauseDirective,
    ProblemAdmittedPayload,
    QuestionAdmittedPayload,
    QuestionKind,
    RefineDirective,
    RejectedAlternative,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchEvent,
    ResearchProblemVersion,
    ResearchQuestionVersion,
    StopDirective,
    StopReason,
    TransitionDecision,
    TransitionDirective,
    canonical_json_bytes,
    canonical_sha256,
)

QUEST_ID = "qst_11111111111111111111111111111111"
OTHER_QUEST_ID = "qst_22222222222222222222222222222222"
ROOT_BRANCH_ID = "rbr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHILD_BRANCH_ID = "rbr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER_BRANCH_ID = "rbr_cccccccccccccccccccccccccccccccc"
NOW = datetime(2026, 8, 23, 10, 30, tzinfo=timezone.utc)


def _sha(character: str) -> str:
    return character * 64


def _ref(
    kind: KernelObjectKind,
    object_id: str,
    digest: str,
    *,
    quest_id: str = QUEST_ID,
) -> KernelObjectRef:
    return KernelObjectRef(
        object_kind=kind,
        object_id=object_id,
        object_sha256=digest,
        quest_id=quest_id,
    )


def _charter_ref(*, quest_id: str = QUEST_ID) -> KernelObjectRef:
    return _ref(KernelObjectKind.CHARTER, "charter:main", _sha("1"), quest_id=quest_id)


def _problem_ref(*, quest_id: str = QUEST_ID) -> KernelObjectRef:
    return _ref(KernelObjectKind.PROBLEM, "problem:main", _sha("2"), quest_id=quest_id)


def _question_ref(*, quest_id: str = QUEST_ID) -> KernelObjectRef:
    return _ref(KernelObjectKind.QUESTION, "question:main", _sha("3"), quest_id=quest_id)


def _action_ref(
    suffix: str = "main",
    digest: str = _sha("4"),
    *,
    quest_id: str = QUEST_ID,
) -> KernelObjectRef:
    return _ref(KernelObjectKind.ACTION, f"action:{suffix}", digest, quest_id=quest_id)


def _evidence(
    digest: str = _sha("5"),
    *,
    kind: EvidenceKind = EvidenceKind.POSITIVE,
    object_id: str | None = "evidence:main",
) -> EvidenceRef:
    return EvidenceRef(kind=kind, object_sha256=digest, object_id=object_id)


def _with_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    return {**base, **overrides}


def make_charter(**overrides: Any) -> ResearchCharterVersion:
    data = {
        "quest_id": QUEST_ID,
        "charter_id": "charter:main",
        "version": 1,
        "mission": "Discover reliable, valuable knowledge.",
        "value_boundaries": ("beneficial", "truthful"),
        "included_scopes": ("chemistry", "physics"),
        "excluded_scopes": ("human-subjects",),
        "allowed_action_classes": ("analysis", "simulation"),
        "safety_policy_sha256": _sha("1"),
        "ethics_policy_sha256": _sha("2"),
        "license_policy_sha256": _sha("3"),
        "privacy_policy_sha256": _sha("4"),
        "egress_policy_sha256": _sha("5"),
        "budget_policy_sha256": _sha("6"),
        "approval_policy_sha256": _sha("7"),
        "publication_policy_sha256": _sha("8"),
        "amendment_principal_ids": ("human:alice", "human:bob"),
        "emergency_stop_principal_ids": ("human:alice",),
        "authorized_by_principal_id": "human:alice",
        "authority_receipt_sha256": _sha("9"),
        "authorized_at": NOW,
    }
    return ResearchCharterVersion(**_with_overrides(data, overrides))


def make_opportunity(**overrides: Any) -> Opportunity:
    data = {
        "opportunity_id": "opportunity:main",
        "quest_id": QUEST_ID,
        "charter_ref": _charter_ref(),
        "kind": OpportunityKind.KNOWLEDGE_GAP,
        "statement": "A consequential uncertainty remains unresolved.",
        "evidence_refs": (_evidence(),),
        "recorded_by_principal_id": "agent:scout",
        "recorded_at": NOW,
    }
    return Opportunity(**_with_overrides(data, overrides))


def make_problem(**overrides: Any) -> ResearchProblemVersion:
    data = {
        "problem_id": "problem:main",
        "quest_id": QUEST_ID,
        "charter_ref": _charter_ref(),
        "version": 1,
        "title": "Resolve a consequential uncertainty",
        "statement": "The available evidence does not distinguish the live explanations.",
        "scope": "A bounded scientific domain.",
        "importance_rationale": "The answer changes a consequential scientific decision.",
        "unknowns": ("causal mechanism", "effect magnitude"),
        "opportunity_refs": (),
        "evidence_refs": (),
        "semantic_delta": "Initial problem admission.",
        "authored_by_principal_id": "agent:problem-framer",
        "authored_at": NOW,
    }
    return ResearchProblemVersion(**_with_overrides(data, overrides))


def make_question(**overrides: Any) -> ResearchQuestionVersion:
    data = {
        "question_id": "question:main",
        "quest_id": QUEST_ID,
        "charter_ref": _charter_ref(),
        "problem_ref": _problem_ref(),
        "version": 1,
        "kind": QuestionKind.CAUSAL,
        "statement": "Does the proposed mechanism cause the observed response?",
        "scope": "The preregistered intervention and population.",
        "answer_space": ("negative", "positive", "unresolved"),
        "scientific_value": "Discriminates explanations and changes the next action.",
        "falsifiability": "A calibrated null response falsifies the effect claim.",
        "evidence_refs": (),
        "semantic_delta": "Initial question admission.",
        "authored_by_principal_id": "agent:question-framer",
        "authored_at": NOW,
    }
    return ResearchQuestionVersion(**_with_overrides(data, overrides))


def make_action(**overrides: Any) -> ResearchActionProposal:
    data = {
        "action_id": "action:main",
        "quest_id": QUEST_ID,
        "charter_ref": _charter_ref(),
        "question_ref": _question_ref(),
        "basis_tail_event_sha256": _sha("a"),
        "kind": ActionKind.DISCRIMINATE,
        "epistemic_purpose": "Discriminate the two remaining causal explanations.",
        "candidate_outcomes": ("negative", "positive", "unresolved"),
        "evidence_refs": (),
        "cost_receipt_sha256": _sha("b"),
        "risk_receipt_sha256": _sha("c"),
        "alternative_action_refs": (),
        "requested_authority_class": "compute:bounded",
        "proposed_by_principal_id": "agent:planner",
        "proposed_at": NOW,
    }
    return ResearchActionProposal(**_with_overrides(data, overrides))


def make_transition(**overrides: Any) -> TransitionDecision:
    data = {
        "transition_id": "transition:main",
        "quest_id": QUEST_ID,
        "charter_ref": _charter_ref(),
        "source_graph_sha256": _sha("d"),
        "selected_action_ref": _action_ref(),
        "directive": ContinueDirective(branch_id=ROOT_BRANCH_ID),
        "evidence_refs": (),
        "evidence_event_sha256s": (),
        "rejected_alternatives": (),
        "budget_receipt_sha256": _sha("b"),
        "risk_receipt_sha256": _sha("c"),
        "policy_receipt_sha256": _sha("e"),
        "reason_codes": ("expected-information-gain",),
        "rationale": "The selected action has positive marginal information value.",
        "decided_by_principal_id": "agent:controller",
        "decided_at": NOW,
    }
    return TransitionDecision(**_with_overrides(data, overrides))


def make_genesis_event(**overrides: Any) -> ResearchEvent:
    payload = CharterActivatedPayload(charter_ref=_charter_ref(), root_branch_id=ROOT_BRANCH_ID)
    data = {
        "quest_id": QUEST_ID,
        "sequence": 1,
        "parent_event_sha256": None,
        "event_type": EventType.CHARTER_ACTIVATED,
        "payload": payload,
        "command_sha256": _sha("a"),
        "principal_id": "human:alice",
        "authorization_receipt_sha256": _sha("9"),
        "committed_at": NOW,
    }
    return ResearchEvent(**_with_overrides(data, overrides))


def _all_kernel_model_types() -> set[type[KernelModel]]:
    pending = list(KernelModel.__subclasses__())
    found: set[type[KernelModel]] = set()
    while pending:
        model_type = pending.pop()
        if model_type not in found:
            found.add(model_type)
            pending.extend(model_type.__subclasses__())
    return found


def test_all_kernel_models_are_frozen_and_closed_world() -> None:
    model_types = _all_kernel_model_types()
    assert model_types
    assert all(model_type.model_config.get("frozen") is True for model_type in model_types)
    assert all(model_type.model_config.get("extra") == "forbid" for model_type in model_types)

    charter = make_charter()
    with pytest.raises(ValidationError, match="frozen"):
        charter.mission = "Mutated authority"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchCharterVersion.model_validate({**charter.model_dump(), "run_id": "legacy-run"})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (make_charter, "authorized_at"),
        (make_opportunity, "recorded_at"),
        (make_problem, "authored_at"),
        (make_question, "authored_at"),
        (make_action, "proposed_at"),
        (make_transition, "decided_at"),
        (make_genesis_event, "committed_at"),
    ],
)
def test_authoritative_timestamps_must_be_utc(
    factory: Callable[..., KernelModel], field_name: str
) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        factory(**{field_name: NOW.astimezone(timezone(timedelta(hours=12)))})
    with pytest.raises(ValidationError):
        factory(**{field_name: NOW.replace(tzinfo=None)})


def test_charter_expiry_must_be_utc_and_follow_authorization() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        make_charter(expires_at=NOW.astimezone(timezone(timedelta(hours=-4))))
    with pytest.raises(ValidationError, match="charter expiry must follow authorization"):
        make_charter(expires_at=NOW)
    assert make_charter(expires_at=NOW + timedelta(days=1)).expires_at == NOW + timedelta(days=1)


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (make_charter, "value_boundaries"),
        (make_charter, "included_scopes"),
        (make_charter, "excluded_scopes"),
        (make_charter, "allowed_action_classes"),
        (make_charter, "amendment_principal_ids"),
        (make_charter, "emergency_stop_principal_ids"),
        (make_problem, "unknowns"),
        (make_question, "answer_space"),
        (make_action, "candidate_outcomes"),
        (make_transition, "reason_codes"),
    ],
)
def test_string_sets_reject_duplicates_and_noncanonical_order(
    factory: Callable[..., KernelModel], field_name: str
) -> None:
    with pytest.raises(ValidationError, match="unique and canonically ordered"):
        factory(**{field_name: ("zeta", "alpha")})
    with pytest.raises(ValidationError, match="unique and canonically ordered"):
        factory(**{field_name: ("alpha", "alpha")})
    with pytest.raises(ValidationError, match="nonempty canonical strings"):
        factory(**{field_name: (" alpha",)})


def test_charter_included_and_excluded_scopes_are_disjoint() -> None:
    with pytest.raises(ValidationError, match="must be disjoint"):
        make_charter(included_scopes=("chemistry",), excluded_scopes=("chemistry",))


def test_reference_sets_are_unique_and_canonically_ordered() -> None:
    evidence_a = _evidence(_sha("1"), object_id="evidence:a")
    evidence_b = _evidence(_sha("2"), object_id="evidence:b")
    opportunity_a = _ref(KernelObjectKind.OPPORTUNITY, "opportunity:a", _sha("1"))
    opportunity_b = _ref(KernelObjectKind.OPPORTUNITY, "opportunity:b", _sha("2"))
    action_a = _action_ref("a", _sha("1"))
    action_b = _action_ref("b", _sha("2"))
    rejected_a = RejectedAlternative(action_ref=action_a, reason_codes=("lower-value",))
    rejected_b = RejectedAlternative(action_ref=action_b, reason_codes=("higher-risk",))

    with pytest.raises(ValidationError, match="opportunity evidence"):
        make_opportunity(evidence_refs=(evidence_b, evidence_a))
    with pytest.raises(ValidationError, match="opportunity references"):
        make_problem(opportunity_refs=(opportunity_b, opportunity_a))
    with pytest.raises(ValidationError, match="alternative actions"):
        make_action(alternative_action_refs=(action_b, action_a))
    with pytest.raises(ValidationError, match="rejected alternatives"):
        make_transition(rejected_alternatives=(rejected_b, rejected_a))

    with pytest.raises(ValidationError, match="opportunity evidence"):
        make_opportunity(evidence_refs=(evidence_a, evidence_a))
    with pytest.raises(ValidationError, match="opportunity references"):
        make_problem(opportunity_refs=(opportunity_a, opportunity_a))
    with pytest.raises(ValidationError, match="alternative actions"):
        make_action(alternative_action_refs=(action_a, action_a))
    with pytest.raises(ValidationError, match="rejected alternatives"):
        make_transition(rejected_alternatives=(rejected_a, rejected_a))


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (make_problem, "evidence_refs"),
        (make_question, "evidence_refs"),
        (make_action, "evidence_refs"),
        (make_transition, "evidence_refs"),
    ],
)
def test_all_evidence_sets_reject_duplicates(
    factory: Callable[..., KernelModel], field_name: str
) -> None:
    evidence = _evidence()
    earlier = _evidence(_sha("1"), object_id="evidence:earlier")
    later = _evidence(_sha("2"), object_id="evidence:later")
    with pytest.raises(ValidationError, match="evidence"):
        factory(**{field_name: (evidence, evidence)})
    with pytest.raises(ValidationError, match="evidence"):
        factory(**{field_name: (later, earlier)})


def test_directive_and_payload_string_sets_are_canonical() -> None:
    with pytest.raises(ValidationError, match="child_branch_ids"):
        ForkDirective(
            source_branch_id=ROOT_BRANCH_ID,
            child_branch_ids=(OTHER_BRANCH_ID, CHILD_BRANCH_ID),
        )
    with pytest.raises(ValidationError, match="reason_codes"):
        RejectedAlternative(action_ref=_action_ref(), reason_codes=("zeta", "alpha"))
    with pytest.raises(ValidationError, match="reopen_conditions"):
        StopDirective(
            branch_id=ROOT_BRANCH_ID,
            stop_reason=StopReason.CAPABILITY_UNAVAILABLE,
            reopen_conditions=("new-tool", "new-tool"),
        )
    with pytest.raises(ValidationError, match="reason_codes"):
        ActionRejectedPayload(
            action_id="action:main",
            branch_id=ROOT_BRANCH_ID,
            reason_codes=("policy", "budget", "budget"),
        )
    with pytest.raises(ValidationError, match="reason_codes"):
        ActionSupersededPayload(
            action_id="action:main",
            branch_id=ROOT_BRANCH_ID,
            reason_codes=("zeta", "alpha"),
        )


def test_unresolved_evidence_in_stop_directive_is_unique() -> None:
    evidence = _evidence()
    with pytest.raises(ValidationError, match="unresolved"):
        StopDirective(
            branch_id=ROOT_BRANCH_ID,
            stop_reason=StopReason.CURRENTLY_INDISTINGUISHABLE,
            unresolved_refs=(evidence, evidence),
        )
    with pytest.raises(ValidationError, match="unresolved"):
        StopDirective(
            branch_id=ROOT_BRANCH_ID,
            stop_reason=StopReason.CURRENTLY_INDISTINGUISHABLE,
            unresolved_refs=(
                _evidence(_sha("2"), object_id="evidence:later"),
                _evidence(_sha("1"), object_id="evidence:earlier"),
            ),
        )


def test_transition_evidence_event_hashes_are_unique_and_ordered() -> None:
    with pytest.raises(ValidationError, match="evidence_event_sha256s"):
        make_transition(evidence_event_sha256s=(_sha("2"), _sha("1")))
    with pytest.raises(ValidationError, match="evidence_event_sha256s"):
        make_transition(evidence_event_sha256s=(_sha("1"), _sha("1")))


@pytest.mark.parametrize(
    ("factory", "version", "parent", "fork", "valid"),
    [
        (make_problem, 1, None, None, True),
        (make_problem, 1, _sha("1"), None, False),
        (make_problem, 1, None, _sha("2"), True),
        (make_problem, 2, _sha("1"), None, True),
        (make_problem, 2, None, None, False),
        (make_problem, 2, _sha("1"), _sha("2"), False),
        (make_question, 1, None, None, True),
        (make_question, 1, _sha("1"), None, False),
        (make_question, 1, None, _sha("2"), True),
        (make_question, 2, _sha("1"), None, True),
        (make_question, 2, None, None, False),
        (make_question, 2, _sha("1"), _sha("2"), False),
    ],
)
def test_problem_and_question_lineage_rules(
    factory: Callable[..., KernelModel],
    version: int,
    parent: str | None,
    fork: str | None,
    valid: bool,
) -> None:
    fork_field = (
        "forked_from_problem_sha256" if factory is make_problem else "forked_from_question_sha256"
    )
    kwargs = {"version": version, "revision_parent_sha256": parent, fork_field: fork}
    if valid:
        assert factory(**kwargs).version == version
    else:
        with pytest.raises(ValidationError):
            factory(**kwargs)


def test_charter_lineage_requires_exactly_one_parent_after_version_one() -> None:
    assert make_charter(version=1, revision_parent_sha256=None).version == 1
    assert make_charter(version=2, revision_parent_sha256=_sha("1")).version == 2
    with pytest.raises(ValidationError, match="only charter version 1"):
        make_charter(version=1, revision_parent_sha256=_sha("1"))
    with pytest.raises(ValidationError, match="only charter version 1"):
        make_charter(version=2, revision_parent_sha256=None)


def test_research_question_has_no_legacy_run_identity() -> None:
    assert "run_id" not in ResearchQuestionVersion.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchQuestionVersion.model_validate(
            {**make_question().model_dump(), "run_id": "legacy-run-id"}
        )


@pytest.mark.parametrize(
    "build_invalid",
    [
        lambda: make_opportunity(charter_ref=_problem_ref()),
        lambda: make_opportunity(charter_ref=_charter_ref(quest_id=OTHER_QUEST_ID)),
        lambda: make_problem(charter_ref=_problem_ref()),
        lambda: make_problem(charter_ref=_charter_ref(quest_id=OTHER_QUEST_ID)),
        lambda: make_problem(
            opportunity_refs=(_ref(KernelObjectKind.PROBLEM, "problem:other", _sha("1")),)
        ),
        lambda: make_problem(
            opportunity_refs=(
                _ref(
                    KernelObjectKind.OPPORTUNITY,
                    "opportunity:other",
                    _sha("1"),
                    quest_id=OTHER_QUEST_ID,
                ),
            )
        ),
        lambda: make_question(charter_ref=_problem_ref()),
        lambda: make_question(problem_ref=_question_ref()),
        lambda: make_question(problem_ref=_problem_ref(quest_id=OTHER_QUEST_ID)),
        lambda: make_action(charter_ref=_problem_ref()),
        lambda: make_action(question_ref=_problem_ref()),
        lambda: make_action(question_ref=_question_ref(quest_id=OTHER_QUEST_ID)),
        lambda: make_action(
            alternative_action_refs=(_ref(KernelObjectKind.QUESTION, "question:other", _sha("1")),)
        ),
        lambda: make_action(
            alternative_action_refs=(_action_ref("other", _sha("1"), quest_id=OTHER_QUEST_ID),)
        ),
        lambda: make_transition(charter_ref=_problem_ref()),
        lambda: make_transition(selected_action_ref=_question_ref()),
        lambda: make_transition(selected_action_ref=_action_ref(quest_id=OTHER_QUEST_ID)),
        lambda: make_transition(
            rejected_alternatives=(
                RejectedAlternative(
                    action_ref=_action_ref("other", _sha("1"), quest_id=OTHER_QUEST_ID),
                    reason_codes=("lower-value",),
                ),
            )
        ),
    ],
)
def test_object_references_enforce_kind_and_quest_scope(
    build_invalid: Callable[[], KernelModel],
) -> None:
    with pytest.raises(ValidationError):
        build_invalid()


@pytest.mark.parametrize(
    "payload",
    [
        CharterActivatedPayload(charter_ref=_problem_ref(), root_branch_id=ROOT_BRANCH_ID),
        CharterRevisedPayload(charter_ref=_charter_ref(quest_id=OTHER_QUEST_ID)),
        OpportunityRecordedPayload(opportunity_ref=_problem_ref(), branch_id=ROOT_BRANCH_ID),
        ProblemAdmittedPayload(
            problem_ref=_problem_ref(quest_id=OTHER_QUEST_ID), branch_id=ROOT_BRANCH_ID
        ),
        QuestionAdmittedPayload(question_ref=_problem_ref(), branch_id=ROOT_BRANCH_ID),
        ActionProposedPayload(action_ref=_question_ref(), branch_id=ROOT_BRANCH_ID),
    ],
)
def test_event_reference_payloads_enforce_kind_and_quest_scope(payload: KernelModel) -> None:
    event_type = EventType(payload.kind)
    sequence = 1 if event_type is EventType.CHARTER_ACTIVATED else 2
    parent = None if sequence == 1 else _sha("f")
    with pytest.raises(ValidationError):
        make_genesis_event(
            sequence=sequence,
            parent_event_sha256=parent,
            event_type=event_type,
            payload=payload,
        )


def test_kernel_object_refs_hashes_and_envelopes_are_content_bound() -> None:
    objects = (make_charter(), make_opportunity(), make_problem(), make_question(), make_action())
    expected_kinds = (
        KernelObjectKind.CHARTER,
        KernelObjectKind.OPPORTUNITY,
        KernelObjectKind.PROBLEM,
        KernelObjectKind.QUESTION,
        KernelObjectKind.ACTION,
    )
    for payload, expected_kind in zip(objects, expected_kinds, strict=True):
        assert payload.object_ref.object_kind is expected_kind
        assert payload.object_ref.object_sha256 == canonical_sha256(payload)
        assert payload.object_ref.catalog_key == payload.object_sha256
        assert (
            KernelObjectEnvelope(object_ref=payload.object_ref, payload=payload).payload == payload
        )

        tampered_ref = payload.object_ref.model_copy(update={"object_sha256": _sha("f")})
        with pytest.raises(ValidationError, match="does not match"):
            KernelObjectEnvelope(object_ref=tampered_ref, payload=payload)

    original = make_question()
    tampered = make_question(statement="Does a different mechanism cause the observed response?")
    assert original.object_sha256 != tampered.object_sha256
    with pytest.raises(ValidationError, match="does not match"):
        KernelObjectEnvelope(object_ref=original.object_ref, payload=tampered)


def test_canonical_hash_is_stable_across_dictionary_key_order_and_omitted_none() -> None:
    first = {"z": 3, "nested": {"b": 2, "a": 1}, "none": None}
    second = {"nested": {"a": 1, "b": 2}, "z": 3}
    assert canonical_json_bytes(first) == b'{"nested":{"a":1,"b":2},"z":3}'
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_sha256(first) == canonical_sha256(second)


@pytest.mark.parametrize("value", [float("nan"), {"x": float("nan")}, [float("nan")]])
def test_canonical_json_rejects_nan(value: object) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json_bytes(value)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ({"kind": "continue", "branch_id": ROOT_BRANCH_ID}, ContinueDirective),
        ({"kind": "activate", "branch_id": ROOT_BRANCH_ID}, ActivateDirective),
        (
            {
                "kind": "refine",
                "source_branch_id": ROOT_BRANCH_ID,
                "child_branch_id": CHILD_BRANCH_ID,
            },
            RefineDirective,
        ),
        (
            {
                "kind": "fork",
                "source_branch_id": ROOT_BRANCH_ID,
                "child_branch_ids": (CHILD_BRANCH_ID, OTHER_BRANCH_ID),
            },
            ForkDirective,
        ),
        (
            {
                "kind": "backtrack",
                "source_branch_id": ROOT_BRANCH_ID,
                "target_branch_id": CHILD_BRANCH_ID,
                "target_event_sha256": _sha("f"),
                "new_branch_id": OTHER_BRANCH_ID,
            },
            BacktrackDirective,
        ),
        ({"kind": "pause", "branch_id": ROOT_BRANCH_ID}, PauseDirective),
        (
            {
                "kind": "stop",
                "branch_id": ROOT_BRANCH_ID,
                "stop_reason": "budget_exhausted",
            },
            StopDirective,
        ),
    ],
)
def test_transition_directive_is_discriminated_by_kind(
    raw: dict[str, object], expected_type: type[KernelModel]
) -> None:
    parsed = TypeAdapter(TransitionDirective).validate_python(raw)
    assert isinstance(parsed, expected_type)


def test_transition_directive_rejects_unknown_or_cross_variant_fields() -> None:
    adapter = TypeAdapter(TransitionDirective)
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        adapter.validate_python({"kind": "teleport", "branch_id": ROOT_BRANCH_ID})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python(
            {
                "kind": "continue",
                "branch_id": ROOT_BRANCH_ID,
                "child_branch_id": CHILD_BRANCH_ID,
            }
        )


def test_transition_directives_reject_degenerate_branch_relations() -> None:
    with pytest.raises(ValidationError, match="distinct child"):
        RefineDirective(source_branch_id=ROOT_BRANCH_ID, child_branch_id=ROOT_BRANCH_ID)
    with pytest.raises(ValidationError, match="distinct from their source"):
        ForkDirective(
            source_branch_id=ROOT_BRANCH_ID,
            child_branch_ids=(ROOT_BRANCH_ID, CHILD_BRANCH_ID),
        )
    with pytest.raises(ValidationError, match="must be distinct"):
        BacktrackDirective(
            source_branch_id=ROOT_BRANCH_ID,
            target_branch_id=CHILD_BRANCH_ID,
            target_event_sha256=_sha("f"),
            new_branch_id=CHILD_BRANCH_ID,
        )


def test_event_envelope_requires_matching_type_and_typed_payload() -> None:
    event = make_genesis_event()
    assert isinstance(event.payload, CharterActivatedPayload)
    assert event.event_type is EventType.CHARTER_ACTIVATED

    parsed = ResearchEvent.model_validate(event.model_dump(mode="json"))
    assert isinstance(parsed.payload, CharterActivatedPayload)
    assert parsed.event_sha256 == event.event_sha256
    assert parsed.event_id == f"rke_{event.event_sha256[:32]}"

    with pytest.raises(ValidationError, match="event_type must match"):
        make_genesis_event(event_type=EventType.CHARTER_REVISED)


@pytest.mark.parametrize(
    ("sequence", "parent", "event_type", "payload"),
    [
        (1, _sha("f"), EventType.CHARTER_ACTIVATED, None),
        (
            2,
            None,
            EventType.OPPORTUNITY_RECORDED,
            OpportunityRecordedPayload(
                opportunity_ref=_ref(KernelObjectKind.OPPORTUNITY, "opportunity:main", _sha("1")),
                branch_id=ROOT_BRANCH_ID,
            ),
        ),
        (
            1,
            None,
            EventType.OPPORTUNITY_RECORDED,
            OpportunityRecordedPayload(
                opportunity_ref=_ref(KernelObjectKind.OPPORTUNITY, "opportunity:main", _sha("1")),
                branch_id=ROOT_BRANCH_ID,
            ),
        ),
    ],
)
def test_event_sequence_parent_and_genesis_rules(
    sequence: int,
    parent: str | None,
    event_type: EventType,
    payload: KernelModel | None,
) -> None:
    kwargs: dict[str, object] = {
        "sequence": sequence,
        "parent_event_sha256": parent,
        "event_type": event_type,
    }
    if payload is not None:
        kwargs["payload"] = payload
    with pytest.raises(ValidationError):
        make_genesis_event(**kwargs)


def test_committed_transition_event_binds_event_quest_and_directive_kind() -> None:
    stop_decision = make_transition(
        directive=StopDirective(
            branch_id=ROOT_BRANCH_ID,
            stop_reason=StopReason.BUDGET_EXHAUSTED,
        )
    )
    with pytest.raises(ValidationError):
        make_genesis_event(
            sequence=2,
            parent_event_sha256=_sha("f"),
            event_type=EventType.CONTINUE_COMMITTED,
            payload=ContinueCommittedPayload(decision=stop_decision),
        )

    other_quest_decision = make_transition(
        quest_id=OTHER_QUEST_ID,
        charter_ref=_charter_ref(quest_id=OTHER_QUEST_ID),
        selected_action_ref=_action_ref(quest_id=OTHER_QUEST_ID),
    )
    with pytest.raises(ValidationError):
        make_genesis_event(
            sequence=2,
            parent_event_sha256=_sha("f"),
            event_type=EventType.CONTINUE_COMMITTED,
            payload=ContinueCommittedPayload(decision=other_quest_decision),
        )


def test_valid_non_genesis_event_is_content_addressed() -> None:
    payload = OpportunityRecordedPayload(
        opportunity_ref=_ref(KernelObjectKind.OPPORTUNITY, "opportunity:main", _sha("1")),
        branch_id=ROOT_BRANCH_ID,
    )
    event = make_genesis_event(
        sequence=2,
        parent_event_sha256=_sha("f"),
        event_type=EventType.OPPORTUNITY_RECORDED,
        payload=payload,
    )
    assert event.sequence == 2
    assert event.parent_event_sha256 == _sha("f")
    assert event.event_sha256 == canonical_sha256(event)


def test_action_lifecycle_payloads_are_closed_and_typed() -> None:
    authorized = ActionAuthorizedPayload(action_id="action:main", branch_id=ROOT_BRANCH_ID)
    proposed = ActionProposedPayload(action_ref=_action_ref(), branch_id=ROOT_BRANCH_ID)
    assert authorized.kind == EventType.ACTION_AUTHORIZED.value
    assert proposed.action_ref.object_kind is KernelObjectKind.ACTION
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ActionAuthorizedPayload(
            action_id="action:main",
            branch_id=ROOT_BRANCH_ID,
            receipt_sha256=_sha("1"),
        )
