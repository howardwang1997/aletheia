"""Graph-scoped F9 v2 world-model values.

Unlike the legacy F9 snapshot, v2 does not require every research action to look like a three-way
null/primary/alternative test.  A snapshot may hold one or more evolving hypotheses, optional
global or hypothesis-specific assumptions, optional predictions, and optional normalized belief.
The stricter bidirectional same-protocol discrimination check is an explicit operation used only
by a ``HypothesisDiscriminationContract``.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.protocols.base import (
    PRINCIPAL_ID_PATTERN,
    SHA256_PATTERN,
    ProtocolModel,
    ProtocolScope,
    canonical_sha256,
    canonical_sha256s,
    canonical_strings,
)

_HYPOTHESIS_ID_PATTERN = r"^hyp_[0-9a-f]{32}$"
_ASSUMPTION_ID_PATTERN = r"^asm_[0-9a-f]{32}$"
_PREDICTION_ID_PATTERN = r"^pred_[0-9a-f]{32}$"
_WORLD_MODEL_ID_PATTERN = r"^wm_[0-9a-f]{32}$"
_BELIEF_ID_PATTERN = r"^blf_[0-9a-f]{32}$"


def _validate_revision(*, version: int, revision_parent_sha256: str | None, label: str) -> None:
    if (version == 1) != (revision_parent_sha256 is None):
        raise ValueError(f"only {label} version 1 may omit its revision parent")


class HypothesisLifecycle(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    NARROWED = "narrowed"
    RETIRED = "retired"


class HypothesisVersionV2(ProtocolModel):
    schema_name: Literal["aletheia.hypothesis"] = "aletheia.hypothesis"
    schema_version: Literal[2] = 2
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    derived_from_hypothesis_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    lifecycle: HypothesisLifecycle
    statement: str = Field(min_length=1, max_length=8_000)
    explanatory_model: str | None = Field(default=None, min_length=1, max_length=16_000)
    rationale_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _lineage_is_explicit(self) -> "HypothesisVersionV2":
        _validate_revision(
            version=self.version,
            revision_parent_sha256=self.revision_parent_sha256,
            label="hypothesis",
        )
        canonical_sha256s(
            self.derived_from_hypothesis_sha256s,
            "derived hypothesis versions",
        )
        if self.revision_parent_sha256 in self.derived_from_hypothesis_sha256s:
            raise ValueError("revision parent cannot be repeated as a derivation source")
        return self

    @property
    def hypothesis_sha256(self) -> str:
        return canonical_sha256(self)


class AssumptionScope(str, Enum):
    GLOBAL = "global"
    HYPOTHESIS = "hypothesis"


class AssumptionDisposition(str, Enum):
    UNRESOLVED = "unresolved"
    ACCEPTED = "accepted"
    VIOLATED = "violated"
    REJECTED = "rejected"


class AssumptionVersionV2(ProtocolModel):
    schema_name: Literal["aletheia.world_model_assumption"] = "aletheia.world_model_assumption"
    schema_version: Literal[2] = 2
    assumption_id: str = Field(pattern=_ASSUMPTION_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    derived_from_assumption_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    scope: AssumptionScope
    applies_to_hypothesis_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    statement: str = Field(min_length=1, max_length=8_000)
    violation_consequence: str = Field(min_length=1, max_length=8_000)
    test_or_monitor_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    disposition: AssumptionDisposition = AssumptionDisposition.UNRESOLVED
    disposition_receipt_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _assumption_is_closed(self) -> "AssumptionVersionV2":
        _validate_revision(
            version=self.version,
            revision_parent_sha256=self.revision_parent_sha256,
            label="assumption",
        )
        canonical_sha256s(
            self.derived_from_assumption_sha256s,
            "derived assumption versions",
        )
        canonical_sha256s(
            self.applies_to_hypothesis_sha256s,
            "assumption hypothesis bindings",
        )
        if (self.scope is AssumptionScope.GLOBAL) != (not self.applies_to_hypothesis_sha256s):
            raise ValueError("only global assumptions may omit hypothesis bindings")
        resolved = self.disposition is not AssumptionDisposition.UNRESOLVED
        if resolved != (self.disposition_receipt_sha256 is not None):
            raise ValueError("resolved assumption requires a disposition receipt, and only then")
        return self

    @property
    def assumption_sha256(self) -> str:
        return canonical_sha256(self)


class PredictionVersionV2(ProtocolModel):
    schema_name: Literal["aletheia.prediction"] = "aletheia.prediction"
    schema_version: Literal[2] = 2
    prediction_id: str = Field(pattern=_PREDICTION_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    derived_from_prediction_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis_sha256: str = Field(pattern=SHA256_PATTERN)
    observable_spec_sha256: str = Field(pattern=SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    outcome_space_sha256: str = Field(pattern=SHA256_PATTERN)
    predicted_outcome_sha256: str = Field(pattern=SHA256_PATTERN)
    discriminates_from_hypothesis_sha256s: tuple[str, ...] = Field(default=(), max_length=63)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _prediction_is_closed(self) -> "PredictionVersionV2":
        _validate_revision(
            version=self.version,
            revision_parent_sha256=self.revision_parent_sha256,
            label="prediction",
        )
        canonical_sha256s(
            self.derived_from_prediction_sha256s,
            "derived prediction versions",
        )
        canonical_sha256s(
            self.discriminates_from_hypothesis_sha256s,
            "prediction discrimination targets",
        )
        if self.hypothesis_sha256 in self.discriminates_from_hypothesis_sha256s:
            raise ValueError("prediction cannot discriminate its hypothesis from itself")
        return self

    @property
    def prediction_sha256(self) -> str:
        return canonical_sha256(self)


class BeliefUpdateBasis(str, Enum):
    PRIOR = "prior"
    VALIDATED_OBSERVATION = "validated_observation"
    MODEL_REVISION = "model_revision"
    QUALITATIVE = "qualitative"


class HypothesisBeliefV2(ProtocolModel):
    hypothesis_sha256: str = Field(pattern=SHA256_PATTERN)
    probability: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _probability_is_finite(self) -> "HypothesisBeliefV2":
        if not math.isfinite(self.probability):
            raise ValueError("hypothesis probability must be finite")
        return self


class BeliefStateVersionV2(ProtocolModel):
    schema_name: Literal["aletheia.belief_state"] = "aletheia.belief_state"
    schema_version: Literal[2] = 2
    belief_id: str = Field(pattern=_BELIEF_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis_beliefs: tuple[HypothesisBeliefV2, ...] = Field(min_length=1, max_length=64)
    update_basis: BeliefUpdateBasis
    source_observation_receipt_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    update_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _belief_is_closed(self) -> "BeliefStateVersionV2":
        _validate_revision(
            version=self.version,
            revision_parent_sha256=self.revision_parent_sha256,
            label="belief state",
        )
        hashes = tuple(item.hypothesis_sha256 for item in self.hypothesis_beliefs)
        if hashes != tuple(sorted(set(hashes))):
            raise ValueError("belief hypotheses must be unique and canonical")
        if not math.isclose(
            math.fsum(item.probability for item in self.hypothesis_beliefs),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("hypothesis probabilities must sum to one")
        observed = self.update_basis is BeliefUpdateBasis.VALIDATED_OBSERVATION
        if observed != (self.source_observation_receipt_sha256 is not None):
            raise ValueError("only observation updates bind an observation receipt")
        return self

    @property
    def belief_state_sha256(self) -> str:
        return canonical_sha256(self)


class WorldModelSnapshotV2(ProtocolModel):
    """One exact, graph-scoped scientific model without a universal hypothesis shape."""

    schema_name: Literal["aletheia.world_model_snapshot"] = "aletheia.world_model_snapshot"
    schema_version: Literal[2] = 2
    graph_scope: ProtocolScope
    world_model_id: str = Field(pattern=_WORLD_MODEL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    derived_from_snapshot_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    hypotheses: tuple[HypothesisVersionV2, ...] = Field(min_length=1, max_length=64)
    assumptions: tuple[AssumptionVersionV2, ...] = Field(default=(), max_length=1024)
    predictions: tuple[PredictionVersionV2, ...] = Field(default=(), max_length=1024)
    belief_state: BeliefStateVersionV2 | None = None
    causal_structure_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    model_limitations: tuple[str, ...] = Field(min_length=1, max_length=128)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _snapshot_is_closed(self) -> "WorldModelSnapshotV2":
        _validate_revision(
            version=self.version,
            revision_parent_sha256=self.revision_parent_sha256,
            label="world-model snapshot",
        )
        canonical_sha256s(
            self.derived_from_snapshot_sha256s,
            "derived world-model snapshots",
        )
        canonical_strings(self.model_limitations, "world-model limitations", required=True)
        members = (*self.hypotheses, *self.assumptions, *self.predictions)
        if self.belief_state is not None:
            members += (self.belief_state,)
        if any(item.authored_at > self.authored_at for item in members):
            raise ValueError("world-model snapshot cannot predate one of its members")
        scope_hash = self.graph_scope.graph_scope_sha256

        hypotheses_by_hash = {item.hypothesis_sha256: item for item in self.hypotheses}
        hypothesis_order = tuple(
            (item.hypothesis_id, item.version, item.hypothesis_sha256) for item in self.hypotheses
        )
        if hypothesis_order != tuple(sorted(set(hypothesis_order))):
            raise ValueError("world-model hypotheses must be unique and canonical")
        lineage_ids = tuple(item.hypothesis_id for item in self.hypotheses)
        if len(lineage_ids) != len(set(lineage_ids)):
            raise ValueError("world model must contain one current version per hypothesis lineage")
        if any(item.graph_scope_sha256 != scope_hash for item in self.hypotheses):
            raise ValueError("world-model hypothesis escaped its exact graph scope")

        assumption_order = tuple(
            (item.assumption_id, item.version, item.assumption_sha256) for item in self.assumptions
        )
        if assumption_order != tuple(sorted(set(assumption_order))):
            raise ValueError("world-model assumptions must be unique and canonical")
        if any(item.graph_scope_sha256 != scope_hash for item in self.assumptions):
            raise ValueError("world-model assumption escaped its exact graph scope")
        for assumption in self.assumptions:
            if not set(assumption.applies_to_hypothesis_sha256s).issubset(hypotheses_by_hash):
                raise ValueError("assumption references a hypothesis outside the snapshot")

        prediction_order = tuple(
            (item.prediction_id, item.version, item.prediction_sha256) for item in self.predictions
        )
        if prediction_order != tuple(sorted(set(prediction_order))):
            raise ValueError("world-model predictions must be unique and canonical")
        if any(item.graph_scope_sha256 != scope_hash for item in self.predictions):
            raise ValueError("world-model prediction escaped its exact graph scope")
        for prediction in self.predictions:
            if prediction.hypothesis_sha256 not in hypotheses_by_hash:
                raise ValueError("prediction references a hypothesis outside the snapshot")
            if not set(prediction.discriminates_from_hypothesis_sha256s).issubset(
                hypotheses_by_hash
            ):
                raise ValueError("prediction discrimination target is outside the snapshot")

        if self.belief_state is not None:
            if self.belief_state.graph_scope_sha256 != scope_hash:
                raise ValueError("world-model belief state escaped its exact graph scope")
            belief_hashes = {
                item.hypothesis_sha256 for item in self.belief_state.hypothesis_beliefs
            }
            if belief_hashes != set(hypotheses_by_hash):
                raise ValueError("belief state must cover the exact current hypothesis versions")
        return self

    @property
    def world_model_sha256(self) -> str:
        return canonical_sha256(self)

    def assert_hypothesis_discrimination(self, target_hypothesis_sha256s: tuple[str, ...]) -> None:
        """Fail unless every target pair has bidirectional, same-protocol differing predictions."""

        canonical_sha256s(
            target_hypothesis_sha256s,
            "hypothesis-discrimination targets",
            required=True,
        )
        if len(target_hypothesis_sha256s) < 2:
            raise ValueError("hypothesis discrimination requires at least two targets")
        hypothesis_hashes = {item.hypothesis_sha256 for item in self.hypotheses}
        if not set(target_hypothesis_sha256s).issubset(hypothesis_hashes):
            raise ValueError("hypothesis-discrimination target is outside the world model")

        for left_index, left in enumerate(target_hypothesis_sha256s):
            for right in target_hypothesis_sha256s[left_index + 1 :]:
                left_predictions = tuple(
                    item
                    for item in self.predictions
                    if item.hypothesis_sha256 == left
                    and right in item.discriminates_from_hypothesis_sha256s
                )
                right_predictions = tuple(
                    item
                    for item in self.predictions
                    if item.hypothesis_sha256 == right
                    and left in item.discriminates_from_hypothesis_sha256s
                )
                distinguishable = any(
                    (
                        left_item.observable_spec_sha256,
                        left_item.measurement_protocol_sha256,
                        left_item.outcome_space_sha256,
                    )
                    == (
                        right_item.observable_spec_sha256,
                        right_item.measurement_protocol_sha256,
                        right_item.outcome_space_sha256,
                    )
                    and left_item.predicted_outcome_sha256 != right_item.predicted_outcome_sha256
                    for left_item in left_predictions
                    for right_item in right_predictions
                )
                if not distinguishable:
                    raise ValueError(
                        "hypothesis pair lacks bidirectional same-protocol differing predictions"
                    )


class WorldModelBinding(ProtocolModel):
    """Exact lightweight binding used by protocols without duplicating a snapshot payload."""

    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)


__all__ = [
    "AssumptionDisposition",
    "AssumptionScope",
    "AssumptionVersionV2",
    "BeliefStateVersionV2",
    "BeliefUpdateBasis",
    "HypothesisBeliefV2",
    "HypothesisLifecycle",
    "HypothesisVersionV2",
    "PredictionVersionV2",
    "WorldModelBinding",
    "WorldModelSnapshotV2",
]
