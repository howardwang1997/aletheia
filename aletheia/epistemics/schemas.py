"""Immutable F9 competing-world-model contracts.

The stable ``*_id`` fields identify scientific lineages.  Content SHA-256 fields identify one
frozen version inside a lineage.  A changed statement, assumption, prediction, or probability
therefore creates a child version; it never edits the object that was available to an earlier
experiment.

This module is deliberately pure: no database, scheduler, model provider, or observation reader.
F9-S1 establishes representation and version semantics only.  Later slices own generation,
identification review, prediction receipts, experiment selection, and belief updates.
"""

from __future__ import annotations

import math
import uuid
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_QUESTION_ID_PATTERN = r"^rq_[0-9a-f]{32}$"
_HYPOTHESIS_ID_PATTERN = r"^hyp_[0-9a-f]{32}$"
_ASSUMPTION_ID_PATTERN = r"^asm_[0-9a-f]{32}$"
_PREDICTION_ID_PATTERN = r"^pred_[0-9a-f]{32}$"
_BELIEF_ID_PATTERN = r"^blf_[0-9a-f]{32}$"


class EpistemicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchQuestionKind(str, Enum):
    MECHANISM = "mechanism"
    CAUSAL_EFFECT = "causal_effect"
    DESCRIPTIVE = "descriptive"
    PREDICTIVE = "predictive"


class HypothesisRole(str, Enum):
    NULL = "null"
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"


class HypothesisLifecycle(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    NARROWED = "narrowed"
    RETIRED = "retired"


class AssumptionKind(str, Enum):
    STRUCTURAL = "structural"
    IDENTIFICATION = "identification"
    MEASUREMENT = "measurement"
    STATISTICAL = "statistical"
    SCOPE = "scope"


class AssumptionDisposition(str, Enum):
    UNRESOLVED = "unresolved"
    ACCEPTED = "accepted"
    VIOLATED = "violated"
    REJECTED = "rejected"


class PredictionDirection(str, Enum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NO_CHANGE = "no_change"
    DISTRIBUTIONAL = "distributional"
    QUALITATIVE = "qualitative"


class BeliefUpdateKind(str, Enum):
    PRIOR = "prior"
    VALIDATED_OBSERVATION = "validated_observation"
    HYPOTHESIS_REVISION = "hypothesis_revision"


def new_research_question_id() -> str:
    return f"rq_{uuid.uuid4().hex}"


def new_hypothesis_id() -> str:
    return f"hyp_{uuid.uuid4().hex}"


def new_assumption_id() -> str:
    return f"asm_{uuid.uuid4().hex}"


def new_prediction_id() -> str:
    return f"pred_{uuid.uuid4().hex}"


def new_belief_lineage_id() -> str:
    return f"blf_{uuid.uuid4().hex}"


def _validate_parent(version: int, parent_sha256: str | None, label: str) -> None:
    if version == 1 and parent_sha256 is not None:
        raise ValueError(f"initial {label} version cannot have a parent")
    if version > 1 and parent_sha256 is None:
        raise ValueError(f"revised {label} version requires its exact parent SHA-256")


class ResearchQuestion(EpistemicModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    question_id: str = Field(pattern=_QUESTION_ID_PATTERN)
    version: int = Field(ge=1)
    parent_question_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    kind: ResearchQuestionKind
    statement: str = Field(min_length=1, max_length=8192)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _version_is_linked(self) -> "ResearchQuestion":
        _validate_parent(self.version, self.parent_question_sha256, "research-question")
        if not self.statement.strip():
            raise ValueError("research-question statement cannot be blank")
        return self

    @property
    def question_sha256(self) -> str:
        return content_sha256(self)


class HypothesisVersion(EpistemicModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    question_id: str = Field(pattern=_QUESTION_ID_PATTERN)
    question_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    version: int = Field(ge=1)
    parent_hypothesis_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    role: HypothesisRole
    lifecycle: HypothesisLifecycle = HypothesisLifecycle.PROPOSED
    statement: str = Field(min_length=1, max_length=8192)
    mechanism: str | None = Field(default=None, max_length=16384)
    rationale_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _version_is_linked(self) -> "HypothesisVersion":
        _validate_parent(self.version, self.parent_hypothesis_sha256, "hypothesis")
        if not self.statement.strip():
            raise ValueError("hypothesis statement cannot be blank")
        if self.mechanism is not None and not self.mechanism.strip():
            raise ValueError("hypothesis mechanism cannot be blank")
        if self.role is not HypothesisRole.NULL and self.mechanism is None:
            raise ValueError("primary and alternative hypotheses require an explicit mechanism")
        return self

    @property
    def hypothesis_sha256(self) -> str:
        return content_sha256(self)


class Assumption(EpistemicModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    assumption_id: str = Field(pattern=_ASSUMPTION_ID_PATTERN)
    version: int = Field(ge=1)
    parent_assumption_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: AssumptionKind
    statement: str = Field(min_length=1, max_length=8192)
    risk_if_violated: str = Field(min_length=1, max_length=8192)
    disposition: AssumptionDisposition = AssumptionDisposition.UNRESOLVED
    review_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _version_and_review_are_linked(self) -> "Assumption":
        _validate_parent(self.version, self.parent_assumption_sha256, "assumption")
        if not self.statement.strip() or not self.risk_if_violated.strip():
            raise ValueError("assumption statement and violation risk cannot be blank")
        reviewed = self.disposition is not AssumptionDisposition.UNRESOLVED
        if reviewed != (self.review_receipt_sha256 is not None):
            raise ValueError("resolved assumptions require a review receipt, and only then")
        return self

    @property
    def assumption_sha256(self) -> str:
        return content_sha256(self)


class Prediction(EpistemicModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    prediction_id: str = Field(pattern=_PREDICTION_ID_PATTERN)
    version: int = Field(ge=1)
    parent_prediction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    observable_id: str = Field(min_length=1, max_length=512)
    outcome_space: tuple[str, ...] = Field(min_length=2, max_length=128)
    expected_outcome: str = Field(min_length=1, max_length=2048)
    direction: PredictionDirection
    discriminates_from_hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=63)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _prediction_is_discriminating(self) -> "Prediction":
        _validate_parent(self.version, self.parent_prediction_sha256, "prediction")
        outcomes = tuple(item.strip() for item in self.outcome_space)
        if any(not item for item in outcomes) or len(outcomes) != len(set(outcomes)):
            raise ValueError("prediction outcome space must contain unique non-blank labels")
        if self.expected_outcome not in outcomes:
            raise ValueError("prediction expected outcome must be a member of its outcome space")
        alternatives = self.discriminates_from_hypothesis_ids
        if len(alternatives) != len(set(alternatives)):
            raise ValueError("prediction discrimination targets must be unique")
        if self.hypothesis_id in alternatives:
            raise ValueError("prediction cannot discriminate a hypothesis from itself")
        return self

    @property
    def prediction_sha256(self) -> str:
        return content_sha256(self)


class HypothesisBelief(EpistemicModel):
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    probability: float = Field(ge=0.0, le=1.0)


class BeliefState(EpistemicModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    belief_lineage_id: str = Field(pattern=_BELIEF_ID_PATTERN)
    version: int = Field(ge=1)
    parent_belief_state_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    question_id: str = Field(pattern=_QUESTION_ID_PATTERN)
    question_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypotheses: tuple[HypothesisBelief, ...] = Field(min_length=2, max_length=64)
    update_kind: BeliefUpdateKind
    source_observation_receipt_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    likelihood_model_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _state_is_normalized_and_attributable(self) -> "BeliefState":
        _validate_parent(self.version, self.parent_belief_state_sha256, "belief-state")
        identities = [item.hypothesis_id for item in self.hypotheses]
        version_hashes = [item.hypothesis_version_sha256 for item in self.hypotheses]
        if len(identities) != len(set(identities)):
            raise ValueError("belief state must contain one entry per hypothesis lineage")
        if len(version_hashes) != len(set(version_hashes)):
            raise ValueError("belief state cannot reuse a hypothesis version")
        if identities != sorted(identities):
            raise ValueError("belief-state hypotheses must use canonical hypothesis-id order")
        if not math.isclose(
            math.fsum(item.probability for item in self.hypotheses),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("belief-state probabilities must sum to one")
        observed = self.update_kind is BeliefUpdateKind.VALIDATED_OBSERVATION
        has_observation = self.source_observation_receipt_sha256 is not None
        has_likelihood = self.likelihood_model_sha256 is not None
        if (observed and not (has_observation and has_likelihood)) or (
            not observed and (has_observation or has_likelihood)
        ):
            raise ValueError(
                "validated-observation belief states require observation and likelihood receipts, "
                "and only then"
            )
        return self

    @property
    def belief_state_sha256(self) -> str:
        return content_sha256(self)


class WorldModelSnapshot(EpistemicModel):
    """One internally closed competing-hypothesis state for a research question."""

    schema_version: Literal[1] = 1
    question: ResearchQuestion
    hypotheses: tuple[HypothesisVersion, ...] = Field(min_length=3, max_length=64)
    assumptions: tuple[Assumption, ...] = Field(min_length=3)
    predictions: tuple[Prediction, ...] = Field(min_length=3)
    belief_state: BeliefState
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _snapshot_is_closed(self) -> "WorldModelSnapshot":
        hypotheses_by_id = {item.hypothesis_id: item for item in self.hypotheses}
        if len(hypotheses_by_id) != len(self.hypotheses):
            raise ValueError("world model must contain one current version per hypothesis lineage")
        if [item.hypothesis_id for item in self.hypotheses] != sorted(hypotheses_by_id):
            raise ValueError("world-model hypotheses must use canonical hypothesis-id order")
        roles = [item.role for item in self.hypotheses]
        if roles.count(HypothesisRole.NULL) != 1 or roles.count(HypothesisRole.PRIMARY) != 1:
            raise ValueError("world model requires exactly one null and one primary hypothesis")
        if roles.count(HypothesisRole.ALTERNATIVE) < 1:
            raise ValueError("world model requires at least one credible alternative hypothesis")

        assumption_order = [(item.assumption_id, item.version) for item in self.assumptions]
        if len(assumption_order) != len(set(assumption_order)):
            raise ValueError("world model cannot reuse an assumption lineage/version")
        if assumption_order != sorted(assumption_order):
            raise ValueError("world-model assumptions must use canonical lineage/version order")
        prediction_order = [(item.prediction_id, item.version) for item in self.predictions]
        if len(prediction_order) != len(set(prediction_order)):
            raise ValueError("world model cannot reuse a prediction lineage/version")
        if prediction_order != sorted(prediction_order):
            raise ValueError("world-model predictions must use canonical lineage/version order")

        for hypothesis in self.hypotheses:
            if hypothesis.run_id != self.question.run_id:
                raise ValueError("world-model hypothesis run does not match its question")
            if hypothesis.question_id != self.question.question_id:
                raise ValueError("world-model hypothesis question lineage does not match")
            if hypothesis.question_version_sha256 != self.question.question_sha256:
                raise ValueError("world-model hypothesis is not bound to the exact question version")

        assumption_hypotheses: set[str] = set()
        for assumption in self.assumptions:
            hypothesis = hypotheses_by_id.get(assumption.hypothesis_id)
            if hypothesis is None:
                raise ValueError("assumption references a hypothesis outside the world model")
            if assumption.run_id != self.question.run_id:
                raise ValueError("assumption run does not match the world model")
            if assumption.hypothesis_version_sha256 != hypothesis.hypothesis_sha256:
                raise ValueError("assumption is not bound to the exact hypothesis version")
            assumption_hypotheses.add(assumption.hypothesis_id)
        if assumption_hypotheses != set(hypotheses_by_id):
            raise ValueError("every hypothesis requires at least one explicit assumption")

        prediction_hypotheses: set[str] = set()
        for prediction in self.predictions:
            hypothesis = hypotheses_by_id.get(prediction.hypothesis_id)
            if hypothesis is None:
                raise ValueError("prediction references a hypothesis outside the world model")
            if prediction.run_id != self.question.run_id:
                raise ValueError("prediction run does not match the world model")
            if prediction.hypothesis_version_sha256 != hypothesis.hypothesis_sha256:
                raise ValueError("prediction is not bound to the exact hypothesis version")
            if not set(prediction.discriminates_from_hypothesis_ids).issubset(hypotheses_by_id):
                raise ValueError("prediction discrimination target is outside the world model")
            prediction_hypotheses.add(prediction.hypothesis_id)
        if prediction_hypotheses != set(hypotheses_by_id):
            raise ValueError("every hypothesis requires at least one discriminating prediction")

        belief = self.belief_state
        if belief.run_id != self.question.run_id or belief.question_id != self.question.question_id:
            raise ValueError("belief state run/question lineage does not match the world model")
        if belief.question_version_sha256 != self.question.question_sha256:
            raise ValueError("belief state is not bound to the exact question version")
        belief_bindings = {
            item.hypothesis_id: item.hypothesis_version_sha256 for item in belief.hypotheses
        }
        expected_bindings = {
            item.hypothesis_id: item.hypothesis_sha256 for item in self.hypotheses
        }
        if belief_bindings != expected_bindings:
            raise ValueError("belief state does not cover the exact world-model hypothesis versions")

        child_times = (
            self.question.frozen_at,
            *(item.frozen_at for item in self.hypotheses),
            *(item.frozen_at for item in self.assumptions),
            *(item.frozen_at for item in self.predictions),
            belief.frozen_at,
        )
        if any(moment > self.frozen_at for moment in child_times):
            raise ValueError("world-model snapshot cannot predate a member")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return content_sha256(self)


class LegacyK2BeliefView(EpistemicModel):
    """Read-only projection of the mutable K2 Beta row; not a migrated F9 posterior."""

    legacy_belief_state_id: str = Field(min_length=1, max_length=32)
    belief_lineage_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    question_key: str = Field(min_length=1, max_length=96)
    alpha: float = Field(gt=0.0)
    beta: float = Field(gt=0.0)
    probability_holds: float = Field(ge=0.0, le=1.0)
    n_updates: int = Field(ge=0)
    updated_at: AwareDatetime
    representation: Literal["legacy_k2_beta_bernoulli"] = "legacy_k2_beta_bernoulli"

    @model_validator(mode="after")
    def _mean_matches_beta_parameters(self) -> "LegacyK2BeliefView":
        expected = self.alpha / (self.alpha + self.beta)
        if not math.isclose(self.probability_holds, expected, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("legacy K2 compatibility probability does not match alpha/(alpha+beta)")
        if self.belief_lineage_id != f"k2::{self.run_id}::{self.question_key}":
            raise ValueError("legacy K2 compatibility lineage identity is not canonical")
        return self
