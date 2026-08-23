"""Pure, versioned contracts for the authoritative research-state graph.

The kernel owns scientific state transitions, not object bytes.  Events therefore carry typed
content references while callers supply immutable objects from content-addressed custody during
replay.  This module intentionally depends only on the standard library and Pydantic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

KERNEL_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
REDUCER_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_OBJECT_ID_PATTERN = r"^[a-z][a-z0-9_:/.-]{2,127}$"
_BRANCH_ID_PATTERN = r"^rbr_[0-9a-f]{32}$"
_PRINCIPAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"


def _without_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one kernel value with the repository's canonical JSON v1 rules."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        _without_none(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_strings(
    values: tuple[str, ...], label: str, *, required: bool = False
) -> tuple[str, ...]:
    if required and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not value or value != value.strip() for value in values):
        raise ValueError(f"{label} must contain nonempty canonical strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


def _canonical_evidence(values: tuple[EvidenceRef, ...], label: str) -> tuple[EvidenceRef, ...]:
    expected = tuple(
        sorted(
            set(values),
            key=lambda item: (item.kind.value, item.object_sha256, item.object_id or ""),
        )
    )
    if values != expected:
        raise ValueError(f"{label} must be unique and canonical")
    return values


class KernelModel(BaseModel):
    """Immutable, closed-world base model for every PR-1 authority contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _datetimes_are_canonical_utc(self) -> "KernelModel":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


class KernelObjectKind(str, Enum):
    CHARTER = "charter"
    OPPORTUNITY = "opportunity"
    PROBLEM = "problem"
    QUESTION = "question"
    ACTION = "action"


class EvidenceKind(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    INCONCLUSIVE = "inconclusive"
    OBJECTION = "objection"
    CONTRADICTION = "contradiction"
    POLICY = "policy"
    BUDGET = "budget"
    RISK = "risk"


class OpportunityKind(str, Enum):
    LITERATURE_CONTRADICTION = "literature_contradiction"
    REPRODUCIBLE_ANOMALY = "reproducible_anomaly"
    KNOWLEDGE_GAP = "knowledge_gap"
    MEASUREMENT_GAP = "measurement_gap"
    CAPABILITY_CHANGE = "capability_change"
    REPLICATION_DEBT = "replication_debt"
    EXTERNAL_NEED = "external_need"


class QuestionKind(str, Enum):
    DESCRIPTIVE = "descriptive"
    COMPARATIVE = "comparative"
    CAUSAL = "causal"
    MECHANISTIC = "mechanistic"
    ESTIMATION = "estimation"
    CONSTRAINT = "constraint"
    FORMAL = "formal"


class ActionKind(str, Enum):
    CONTINUE = "continue"
    ACTIVATE = "activate"
    CHARACTERIZE = "characterize"
    DISCRIMINATE = "discriminate"
    ESTIMATE_EFFECT = "estimate_effect"
    FALSIFY = "falsify"
    CALIBRATE = "calibrate"
    REPRODUCE = "reproduce"
    MAP_BOUNDARY = "map_boundary"
    SYNTHESIZE = "synthesize"
    ACQUIRE_CAPABILITY = "acquire_capability"
    REFINE = "refine"
    FORK = "fork"
    BACKTRACK = "backtrack"
    PAUSE = "pause"
    STOP = "stop"


class StopReason(str, Enum):
    EVIDENCE_THRESHOLD_REACHED = "evidence_threshold_reached"
    CURRENTLY_INDISTINGUISHABLE = "currently_indistinguishable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    INSUFFICIENT_NOVELTY_OR_VALUE = "insufficient_novelty_or_value"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LOW_MARGINAL_INFORMATION_VALUE = "low_marginal_information_value"
    RISK_BOUNDARY = "risk_boundary"
    ETHICS_BOUNDARY = "ethics_boundary"
    LICENSE_BOUNDARY = "license_boundary"
    RELIABLE_NEGATIVE_RESULT = "reliable_negative_result"
    EXTERNAL_REPLICATION_REQUIRED = "external_replication_required"
    HUMAN_VALUE_JUDGMENT_REQUIRED = "human_value_judgment_required"
    EMERGENCY_STOP = "emergency_stop"


class KernelObjectRef(KernelModel):
    object_kind: KernelObjectKind
    object_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    object_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)

    @property
    def catalog_key(self) -> str:
        return self.object_sha256


ResearchObjectRef = KernelObjectRef


def emergency_halt_action_ref(*, quest_id: str, charter_ref: KernelObjectRef) -> KernelObjectRef:
    """Return the deterministic virtual authority marker for a global emergency halt.

    The marker is not an admitted scientific action and therefore has no CAS payload. It occupies
    the existing v1 ``selected_action_ref`` field so emergency halt semantics can remain wire
    compatible without depending on an ordinary-role action proposal.
    """

    if charter_ref.quest_id != quest_id or charter_ref.object_kind is not KernelObjectKind.CHARTER:
        raise ValueError("emergency halt marker requires the active Quest charter")
    digest = canonical_sha256(
        {
            "schema_name": "aletheia.emergency_halt_authority",
            "schema_version": 1,
            "quest_id": quest_id,
            "charter_sha256": charter_ref.object_sha256,
        }
    )
    return KernelObjectRef(
        object_kind=KernelObjectKind.ACTION,
        object_id="action:emergency-halt",
        object_sha256=digest,
        quest_id=quest_id,
    )


class EvidenceRef(KernelModel):
    kind: EvidenceKind
    object_sha256: str = Field(pattern=_SHA256_PATTERN)
    object_id: str | None = Field(default=None, pattern=_OBJECT_ID_PATTERN)


class ResearchCharterVersion(KernelModel):
    schema_name: Literal["aletheia.research_charter"] = "aletheia.research_charter"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    mission: str = Field(min_length=1, max_length=8_000)
    value_boundaries: tuple[str, ...] = Field(min_length=1, max_length=64)
    included_scopes: tuple[str, ...] = Field(min_length=1, max_length=128)
    excluded_scopes: tuple[str, ...] = Field(default=(), max_length=128)
    allowed_action_classes: tuple[str, ...] = Field(min_length=1, max_length=64)
    safety_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    ethics_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    license_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    privacy_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    egress_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    budget_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    publication_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    amendment_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    emergency_stop_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    authorized_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    # External charter provenance, not the cryptographic per-command authorization receipt.
    authority_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _charter_is_canonical(self) -> "ResearchCharterVersion":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only charter version 1 may omit revision_parent_sha256")
        _canonical_strings(self.value_boundaries, "value_boundaries", required=True)
        _canonical_strings(self.included_scopes, "included_scopes", required=True)
        _canonical_strings(self.excluded_scopes, "excluded_scopes")
        _canonical_strings(self.allowed_action_classes, "allowed_action_classes", required=True)
        _canonical_strings(self.amendment_principal_ids, "amendment_principal_ids", required=True)
        _canonical_strings(
            self.emergency_stop_principal_ids,
            "emergency_stop_principal_ids",
            required=True,
        )
        if set(self.included_scopes) & set(self.excluded_scopes):
            raise ValueError("included and excluded charter scopes must be disjoint")
        if self.expires_at is not None and self.expires_at <= self.authorized_at:
            raise ValueError("charter expiry must follow authorization")
        return self

    @property
    def object_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def object_ref(self) -> KernelObjectRef:
        return KernelObjectRef(
            object_kind=KernelObjectKind.CHARTER,
            object_id=self.charter_id,
            object_sha256=self.object_sha256,
            quest_id=self.quest_id,
        )


class Opportunity(KernelModel):
    schema_name: Literal["aletheia.research_opportunity"] = "aletheia.research_opportunity"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    opportunity_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_ref: KernelObjectRef
    kind: OpportunityKind
    statement: str = Field(min_length=1, max_length=8_000)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=128)
    recorded_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _opportunity_scope_is_exact(self) -> "Opportunity":
        if (
            self.charter_ref.object_kind is not KernelObjectKind.CHARTER
            or self.charter_ref.quest_id != self.quest_id
        ):
            raise ValueError("opportunity charter reference must belong to its quest")
        _canonical_evidence(self.evidence_refs, "opportunity evidence")
        return self

    @property
    def object_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def object_ref(self) -> KernelObjectRef:
        return KernelObjectRef(
            object_kind=KernelObjectKind.OPPORTUNITY,
            object_id=self.opportunity_id,
            object_sha256=self.object_sha256,
            quest_id=self.quest_id,
        )


class ResearchProblemVersion(KernelModel):
    schema_name: Literal["aletheia.research_problem"] = "aletheia.research_problem"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    problem_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_ref: KernelObjectRef
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    forked_from_problem_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    title: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=8_000)
    scope: str = Field(min_length=1, max_length=4_000)
    importance_rationale: str = Field(min_length=1, max_length=4_000)
    unknowns: tuple[str, ...] = Field(min_length=1, max_length=128)
    opportunity_refs: tuple[KernelObjectRef, ...] = Field(default=(), max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _problem_lineage_is_explicit(self) -> "ResearchProblemVersion":
        if self.version == 1 and self.revision_parent_sha256 is not None:
            raise ValueError("problem version 1 cannot have a revision parent")
        if self.version > 1 and self.revision_parent_sha256 is None:
            raise ValueError("revised problem must identify its revision parent")
        if self.version > 1 and self.forked_from_problem_sha256 is not None:
            raise ValueError("a problem revision cannot also begin a fork lineage")
        if (
            self.charter_ref.object_kind is not KernelObjectKind.CHARTER
            or self.charter_ref.quest_id != self.quest_id
        ):
            raise ValueError("problem charter reference must belong to its quest")
        _canonical_strings(self.unknowns, "unknowns", required=True)
        if self.opportunity_refs != tuple(
            sorted(
                set(self.opportunity_refs),
                key=lambda item: (item.object_id, item.object_sha256),
            )
        ):
            raise ValueError("problem opportunity references must be unique and canonical")
        if any(
            ref.quest_id != self.quest_id or ref.object_kind is not KernelObjectKind.OPPORTUNITY
            for ref in self.opportunity_refs
        ):
            raise ValueError("problem opportunities must belong to the same quest")
        _canonical_evidence(self.evidence_refs, "problem evidence")
        return self

    @property
    def object_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def object_ref(self) -> KernelObjectRef:
        return KernelObjectRef(
            object_kind=KernelObjectKind.PROBLEM,
            object_id=self.problem_id,
            object_sha256=self.object_sha256,
            quest_id=self.quest_id,
        )


class ResearchQuestionVersion(KernelModel):
    schema_name: Literal["aletheia.research_question"] = "aletheia.research_question"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    question_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_ref: KernelObjectRef
    problem_ref: KernelObjectRef
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    forked_from_question_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    kind: QuestionKind
    statement: str = Field(min_length=1, max_length=8_000)
    scope: str = Field(min_length=1, max_length=4_000)
    answer_space: tuple[str, ...] = Field(min_length=1, max_length=128)
    scientific_value: str = Field(min_length=1, max_length=4_000)
    falsifiability: str = Field(min_length=1, max_length=4_000)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _question_scope_is_exact(self) -> "ResearchQuestionVersion":
        if self.version == 1 and self.revision_parent_sha256 is not None:
            raise ValueError("question version 1 cannot have a revision parent")
        if self.version > 1 and self.revision_parent_sha256 is None:
            raise ValueError("revised question must identify its revision parent")
        if self.version > 1 and self.forked_from_question_sha256 is not None:
            raise ValueError("a question revision cannot also begin a fork lineage")
        for ref, kind, label in (
            (self.charter_ref, KernelObjectKind.CHARTER, "charter"),
            (self.problem_ref, KernelObjectKind.PROBLEM, "problem"),
        ):
            if ref.object_kind is not kind or ref.quest_id != self.quest_id:
                raise ValueError(f"question {label} reference must belong to its quest")
        _canonical_strings(self.answer_space, "answer_space", required=True)
        _canonical_evidence(self.evidence_refs, "question evidence")
        return self

    @property
    def object_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def object_ref(self) -> KernelObjectRef:
        return KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id=self.question_id,
            object_sha256=self.object_sha256,
            quest_id=self.quest_id,
        )


class ResearchActionProposal(KernelModel):
    schema_name: Literal["aletheia.research_action_proposal"] = "aletheia.research_action_proposal"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    action_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_ref: KernelObjectRef
    question_ref: KernelObjectRef
    basis_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: ActionKind
    epistemic_purpose: str = Field(min_length=1, max_length=4_000)
    candidate_outcomes: tuple[str, ...] = Field(min_length=1, max_length=128)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)
    cost_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    risk_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    alternative_action_refs: tuple[KernelObjectRef, ...] = Field(default=(), max_length=64)
    requested_authority_class: str = Field(pattern=_OBJECT_ID_PATTERN)
    proposed_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    proposed_at: AwareDatetime

    @model_validator(mode="after")
    def _action_scope_is_exact(self) -> "ResearchActionProposal":
        for ref, kind, label in (
            (self.charter_ref, KernelObjectKind.CHARTER, "charter"),
            (self.question_ref, KernelObjectKind.QUESTION, "question"),
        ):
            if ref.object_kind is not kind or ref.quest_id != self.quest_id:
                raise ValueError(f"action {label} reference must belong to its quest")
        _canonical_strings(self.candidate_outcomes, "candidate_outcomes", required=True)
        if self.alternative_action_refs != tuple(
            sorted(
                set(self.alternative_action_refs),
                key=lambda item: (item.object_id, item.object_sha256),
            )
        ):
            raise ValueError("alternative actions must be unique and canonical")
        if any(
            ref.object_kind is not KernelObjectKind.ACTION or ref.quest_id != self.quest_id
            for ref in self.alternative_action_refs
        ):
            raise ValueError("alternative actions must belong to the same quest")
        _canonical_evidence(self.evidence_refs, "action evidence")
        return self

    @property
    def object_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def object_ref(self) -> KernelObjectRef:
        return KernelObjectRef(
            object_kind=KernelObjectKind.ACTION,
            object_id=self.action_id,
            object_sha256=self.object_sha256,
            quest_id=self.quest_id,
        )


KernelObject: TypeAlias = (
    ResearchCharterVersion
    | Opportunity
    | ResearchProblemVersion
    | ResearchQuestionVersion
    | ResearchActionProposal
)


class KernelObjectEnvelope(KernelModel):
    object_ref: KernelObjectRef
    payload: KernelObject

    @model_validator(mode="after")
    def _reference_matches_payload(self) -> "KernelObjectEnvelope":
        if self.payload.object_ref != self.object_ref:
            raise ValueError("object envelope reference does not match its payload")
        return self


ResearchObjectEnvelope = KernelObjectEnvelope


class RejectedAlternative(KernelModel):
    action_ref: KernelObjectRef
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _alternative_is_canonical(self) -> "RejectedAlternative":
        if self.action_ref.object_kind is not KernelObjectKind.ACTION:
            raise ValueError("rejected alternative must reference an action")
        _canonical_strings(self.reason_codes, "reason_codes", required=True)
        return self


class ContinueDirective(KernelModel):
    kind: Literal["continue"] = "continue"
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class ActivateDirective(KernelModel):
    kind: Literal["activate"] = "activate"
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class RefineDirective(KernelModel):
    kind: Literal["refine"] = "refine"
    source_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    child_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)

    @model_validator(mode="after")
    def _child_is_new(self) -> "RefineDirective":
        if self.source_branch_id == self.child_branch_id:
            raise ValueError("refine must create a distinct child branch")
        return self


class ForkDirective(KernelModel):
    kind: Literal["fork"] = "fork"
    source_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    child_branch_ids: tuple[str, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def _children_are_canonical(self) -> "ForkDirective":
        _canonical_strings(self.child_branch_ids, "child_branch_ids", required=True)
        if self.source_branch_id in self.child_branch_ids:
            raise ValueError("fork children must be distinct from their source")
        return self


class BacktrackDirective(KernelModel):
    kind: Literal["backtrack"] = "backtrack"
    source_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    target_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    target_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)

    @model_validator(mode="after")
    def _branches_are_distinct(self) -> "BacktrackDirective":
        if len({self.source_branch_id, self.target_branch_id, self.new_branch_id}) != 3:
            raise ValueError("backtrack source, strict ancestor, and child must be distinct")
        return self


class PauseDirective(KernelModel):
    kind: Literal["pause"] = "pause"
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class StopDirective(KernelModel):
    kind: Literal["stop"] = "stop"
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    stop_reason: StopReason
    reopen_conditions: tuple[str, ...] = Field(default=(), max_length=64)
    unresolved_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def _stop_details_are_canonical(self) -> "StopDirective":
        _canonical_strings(self.reopen_conditions, "reopen_conditions")
        _canonical_evidence(self.unresolved_refs, "unresolved evidence")
        return self


TransitionDirective: TypeAlias = Annotated[
    ContinueDirective
    | ActivateDirective
    | RefineDirective
    | ForkDirective
    | BacktrackDirective
    | PauseDirective
    | StopDirective,
    Field(discriminator="kind"),
]


class TransitionDecision(KernelModel):
    schema_name: Literal["aletheia.transition_decision"] = "aletheia.transition_decision"
    schema_version: Literal[1] = KERNEL_SCHEMA_VERSION
    transition_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    charter_ref: KernelObjectRef
    source_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_action_ref: KernelObjectRef
    directive: TransitionDirective
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)
    evidence_event_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    rejected_alternatives: tuple[RejectedAlternative, ...] = Field(default=(), max_length=64)
    budget_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    risk_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1, max_length=8_000)
    decided_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _decision_is_canonical(self) -> "TransitionDecision":
        for ref, kind, label in (
            (self.charter_ref, KernelObjectKind.CHARTER, "charter"),
            (self.selected_action_ref, KernelObjectKind.ACTION, "selected action"),
        ):
            if ref.object_kind is not kind or ref.quest_id != self.quest_id:
                raise ValueError(f"transition {label} reference must belong to its quest")
        _canonical_strings(self.evidence_event_sha256s, "evidence_event_sha256s")
        _canonical_strings(self.reason_codes, "reason_codes", required=True)
        if self.rejected_alternatives != tuple(
            sorted(
                set(self.rejected_alternatives),
                key=lambda item: item.action_ref.object_sha256,
            )
        ):
            raise ValueError("rejected alternatives must be unique and canonical")
        if any(item.action_ref.quest_id != self.quest_id for item in self.rejected_alternatives):
            raise ValueError("rejected alternatives must belong to the transition quest")
        _canonical_evidence(self.evidence_refs, "transition evidence")
        return self

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self)


class EventType(str, Enum):
    CHARTER_ACTIVATED = "charter_activated"
    CHARTER_REVISED = "charter_revised"
    OPPORTUNITY_RECORDED = "opportunity_recorded"
    PROBLEM_ADMITTED = "problem_admitted"
    QUESTION_ADMITTED = "question_admitted"
    ACTION_PROPOSED = "action_proposed"
    ACTION_AUTHORIZED = "action_authorized"
    ACTION_REJECTED = "action_rejected"
    ACTION_SUPERSEDED = "action_superseded"
    CONTINUE_COMMITTED = "continue_committed"
    ACTIVATE_COMMITTED = "activate_committed"
    REFINE_COMMITTED = "refine_committed"
    FORK_COMMITTED = "fork_committed"
    BACKTRACK_COMMITTED = "backtrack_committed"
    PAUSE_COMMITTED = "pause_committed"
    STOP_COMMITTED = "stop_committed"


class CharterActivatedPayload(KernelModel):
    kind: Literal["charter_activated"] = "charter_activated"
    charter_ref: KernelObjectRef
    root_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class CharterRevisedPayload(KernelModel):
    kind: Literal["charter_revised"] = "charter_revised"
    charter_ref: KernelObjectRef


class OpportunityRecordedPayload(KernelModel):
    kind: Literal["opportunity_recorded"] = "opportunity_recorded"
    opportunity_ref: KernelObjectRef
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class ProblemAdmittedPayload(KernelModel):
    kind: Literal["problem_admitted"] = "problem_admitted"
    problem_ref: KernelObjectRef
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class QuestionAdmittedPayload(KernelModel):
    kind: Literal["question_admitted"] = "question_admitted"
    question_ref: KernelObjectRef
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class ActionProposedPayload(KernelModel):
    kind: Literal["action_proposed"] = "action_proposed"
    action_ref: KernelObjectRef
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class ActionAuthorizedPayload(KernelModel):
    kind: Literal["action_authorized"] = "action_authorized"
    action_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)


class ActionRejectedPayload(KernelModel):
    kind: Literal["action_rejected"] = "action_rejected"
    action_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _reasons_are_canonical(self) -> "ActionRejectedPayload":
        _canonical_strings(self.reason_codes, "reason_codes", required=True)
        return self


class ActionSupersededPayload(KernelModel):
    kind: Literal["action_superseded"] = "action_superseded"
    action_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _reasons_are_canonical(self) -> "ActionSupersededPayload":
        _canonical_strings(self.reason_codes, "reason_codes", required=True)
        return self


class ContinueCommittedPayload(KernelModel):
    kind: Literal["continue_committed"] = "continue_committed"
    decision: TransitionDecision


class ActivateCommittedPayload(KernelModel):
    kind: Literal["activate_committed"] = "activate_committed"
    decision: TransitionDecision


class RefineCommittedPayload(KernelModel):
    kind: Literal["refine_committed"] = "refine_committed"
    decision: TransitionDecision


class ForkCommittedPayload(KernelModel):
    kind: Literal["fork_committed"] = "fork_committed"
    decision: TransitionDecision


class BacktrackCommittedPayload(KernelModel):
    kind: Literal["backtrack_committed"] = "backtrack_committed"
    decision: TransitionDecision


class PauseCommittedPayload(KernelModel):
    kind: Literal["pause_committed"] = "pause_committed"
    decision: TransitionDecision


class StopCommittedPayload(KernelModel):
    kind: Literal["stop_committed"] = "stop_committed"
    decision: TransitionDecision


EventPayload: TypeAlias = Annotated[
    CharterActivatedPayload
    | CharterRevisedPayload
    | OpportunityRecordedPayload
    | ProblemAdmittedPayload
    | QuestionAdmittedPayload
    | ActionProposedPayload
    | ActionAuthorizedPayload
    | ActionRejectedPayload
    | ActionSupersededPayload
    | ContinueCommittedPayload
    | ActivateCommittedPayload
    | RefineCommittedPayload
    | ForkCommittedPayload
    | BacktrackCommittedPayload
    | PauseCommittedPayload
    | StopCommittedPayload,
    Field(discriminator="kind"),
]


class ResearchEvent(KernelModel):
    schema_name: Literal["aletheia.research_event"] = "aletheia.research_event"
    event_schema_version: Literal[1] = EVENT_SCHEMA_VERSION
    reducer_version: Literal[1] = REDUCER_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    sequence: int = Field(ge=1)
    parent_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    event_type: EventType
    payload: EventPayload
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    authorization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _event_envelope_is_consistent(self) -> "ResearchEvent":
        if (self.sequence == 1) != (self.parent_event_sha256 is None):
            raise ValueError("only the genesis event may omit parent_event_sha256")
        if self.event_type.value != self.payload.kind:
            raise ValueError("event_type must match the typed payload kind")
        if self.sequence == 1 and self.event_type is not EventType.CHARTER_ACTIVATED:
            raise ValueError("the genesis event must activate a charter")
        referenced_object: tuple[KernelObjectRef, KernelObjectKind] | None = None
        if isinstance(self.payload, (CharterActivatedPayload, CharterRevisedPayload)):
            referenced_object = (self.payload.charter_ref, KernelObjectKind.CHARTER)
        elif isinstance(self.payload, OpportunityRecordedPayload):
            referenced_object = (self.payload.opportunity_ref, KernelObjectKind.OPPORTUNITY)
        elif isinstance(self.payload, ProblemAdmittedPayload):
            referenced_object = (self.payload.problem_ref, KernelObjectKind.PROBLEM)
        elif isinstance(self.payload, QuestionAdmittedPayload):
            referenced_object = (self.payload.question_ref, KernelObjectKind.QUESTION)
        elif isinstance(self.payload, ActionProposedPayload):
            referenced_object = (self.payload.action_ref, KernelObjectKind.ACTION)
        if referenced_object is not None:
            ref, expected_kind = referenced_object
            if ref.object_kind is not expected_kind or ref.quest_id != self.quest_id:
                raise ValueError("event object reference has the wrong kind or quest scope")

        transition_payload_types = (
            ContinueCommittedPayload,
            ActivateCommittedPayload,
            RefineCommittedPayload,
            ForkCommittedPayload,
            BacktrackCommittedPayload,
            PauseCommittedPayload,
            StopCommittedPayload,
        )
        if isinstance(self.payload, transition_payload_types):
            expected_directive = self.payload.kind.removesuffix("_committed")
            if (
                self.payload.decision.quest_id != self.quest_id
                or self.payload.decision.directive.kind != expected_directive
            ):
                raise ValueError(
                    "committed transition must bind the event quest and directive kind"
                )
        return self

    @property
    def event_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def event_id(self) -> str:
        return f"rke_{self.event_sha256[:32]}"
