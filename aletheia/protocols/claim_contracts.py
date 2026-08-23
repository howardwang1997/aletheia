"""Domain-independent epistemic, observable, and claim contracts.

An epistemic contract states how one action is allowed to reduce uncertainty.  It is a tagged
union rather than a hypothesis-shaped universal schema: characterization, estimation,
qualification, formal derivation, and evidence synthesis are first-class and do not manufacture
dummy null/primary hypotheses.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, Field, model_validator

from aletheia.protocols.base import (
    LOCAL_ID_PATTERN,
    PRINCIPAL_ID_PATTERN,
    PROTOCOL_SCHEMA_VERSION,
    SHA256_PATTERN,
    ProtocolModel,
    canonical_sha256,
    canonical_sha256s,
    canonical_strings,
)


class EpistemicKind(str, Enum):
    HYPOTHESIS_DISCRIMINATION = "hypothesis_discrimination"
    CHARACTERIZATION = "characterization"
    ESTIMATION = "estimation"
    CONSTRAINT_TEST = "constraint_test"
    CAPABILITY_QUALIFICATION = "capability_qualification"
    FORMAL_DERIVATION = "formal_derivation"
    EVIDENCE_SYNTHESIS = "evidence_synthesis"


class EpistemicPurpose(str, Enum):
    CHARACTERIZE = "characterize"
    DISCRIMINATE = "discriminate"
    ESTIMATE_EFFECT = "estimate_effect"
    FALSIFY = "falsify"
    CALIBRATE = "calibrate"
    REPRODUCE = "reproduce"
    MAP_BOUNDARY = "map_boundary"
    SYNTHESIZE = "synthesize"
    ACQUIRE_CAPABILITY = "acquire_capability"
    DERIVE = "derive"


class ObservableValueKind(str, Enum):
    CONTINUOUS = "continuous"
    INTEGER = "integer"
    BINARY = "binary"
    CATEGORICAL = "categorical"
    ORDINAL = "ordinal"
    STRUCTURED = "structured"


class ObservableSpec(ProtocolModel):
    """One measurable construct with exact measurement and calibration requirements."""

    schema_name: Literal["aletheia.observable_spec"] = "aletheia.observable_spec"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    observable_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    construct_definition: str = Field(min_length=1, max_length=4_000)
    value_kind: ObservableValueKind
    unit: str | None = Field(default=None, min_length=1, max_length=128)
    minimum: float | None = None
    maximum: float | None = None
    categories: tuple[str, ...] = Field(default=(), max_length=256)
    uncertainty_model_sha256: str = Field(pattern=SHA256_PATTERN)
    measurement_capability_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=SHA256_PATTERN)
    unit_or_ontology_sha256: str = Field(pattern=SHA256_PATTERN)
    calibration_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    entity_identity_schema_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _observable_is_closed(self) -> "ObservableSpec":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only observable version 1 may omit its revision parent")
        bounds = (self.minimum, self.maximum)
        if any(value is not None and not math.isfinite(value) for value in bounds):
            raise ValueError("observable bounds must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("observable minimum must be below maximum")
        numeric = self.value_kind in {ObservableValueKind.CONTINUOUS, ObservableValueKind.INTEGER}
        if numeric:
            if self.unit is None or self.minimum is None or self.maximum is None or self.categories:
                raise ValueError("numeric observables require unit and range, and no categories")
        elif self.value_kind in {
            ObservableValueKind.BINARY,
            ObservableValueKind.CATEGORICAL,
            ObservableValueKind.ORDINAL,
        }:
            if self.unit is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("discrete observables use categories instead of unit/range")
            canonical_strings(self.categories, "observable categories", required=True)
            if self.value_kind is ObservableValueKind.BINARY and len(self.categories) != 2:
                raise ValueError("binary observables require exactly two categories")
        elif self.unit is not None or self.minimum is not None or self.maximum is not None:
            raise ValueError("structured observables cannot declare scalar unit/range")
        return self

    @property
    def observable_sha256(self) -> str:
        return canonical_sha256(self)


class ClaimKind(str, Enum):
    DESCRIPTIVE = "descriptive"
    COMPARATIVE = "comparative"
    ASSOCIATIONAL = "associational"
    PREDICTIVE = "predictive"
    CAUSAL = "causal"
    MECHANISTIC = "mechanistic"
    CONSTRAINT = "constraint"
    CAPABILITY = "capability"
    FORMAL = "formal"
    SYNTHESIS = "synthesis"


class ClaimStrength(str, Enum):
    EXPLORATORY = "exploratory"
    TENTATIVE = "tentative"
    SUPPORTED = "supported"
    CONFIRMED = "confirmed"


_CLAIM_STRENGTH_RANK = {
    ClaimStrength.EXPLORATORY: 0,
    ClaimStrength.TENTATIVE: 1,
    ClaimStrength.SUPPORTED: 2,
    ClaimStrength.CONFIRMED: 3,
}


class EvidenceModality(str, Enum):
    EMPIRICAL = "empirical"
    COMPUTATIONAL = "computational"
    FORMAL = "formal"
    THEORETICAL = "theoretical"


class ReplicationTier(str, Enum):
    NONE = "none"
    EXACT_REEXECUTION = "exact_reexecution"
    INDEPENDENT_IMPLEMENTATION = "independent_implementation"
    EXTERNAL_INDEPENDENT = "external_independent"


class ClaimAllowance(ProtocolModel):
    kind: ClaimKind
    maximum_strength: ClaimStrength


class ClaimCeiling(ProtocolModel):
    """Per-kind ceiling; unlike a scalar, it cannot conflate incomparable claim families."""

    schema_name: Literal["aletheia.claim_ceiling"] = "aletheia.claim_ceiling"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    allowances: tuple[ClaimAllowance, ...] = Field(min_length=1, max_length=32)
    required_evidence_modalities: tuple[EvidenceModality, ...] = Field(min_length=1, max_length=4)
    required_replication_tier: ReplicationTier
    independent_validation_required: bool = True
    rationale: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def _ceiling_is_canonical(self) -> "ClaimCeiling":
        kinds = tuple(item.kind.value for item in self.allowances)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("claim allowances must have unique kinds in canonical order")
        modalities = tuple(item.value for item in self.required_evidence_modalities)
        if modalities != tuple(sorted(set(modalities))):
            raise ValueError("evidence modalities must be unique and canonical")
        return self

    @property
    def ceiling_sha256(self) -> str:
        return canonical_sha256(self)

    def maximum_strength_for(self, kind: ClaimKind) -> ClaimStrength | None:
        return next(
            (item.maximum_strength for item in self.allowances if item.kind is kind),
            None,
        )


class ClaimContract(ProtocolModel):
    """Pre-observation contract for one atomic, possibly mixed-epistemic statement."""

    schema_name: Literal["aletheia.claim_contract"] = "aletheia.claim_contract"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    claim_contract_id: str = Field(pattern=LOCAL_ID_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    epistemic_kinds: tuple[EpistemicKind, ...] = Field(min_length=1, max_length=7)
    statement: str = Field(min_length=1, max_length=8_000)
    scope_statement: str = Field(min_length=1, max_length=4_000)
    requested_kind: ClaimKind
    requested_strength: ClaimStrength
    ceiling: ClaimCeiling
    decision_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _request_is_below_ceiling(self) -> "ClaimContract":
        kinds = tuple(item.value for item in self.epistemic_kinds)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("claim epistemic kinds must be unique and canonical")
        maximum = self.ceiling.maximum_strength_for(self.requested_kind)
        if maximum is None:
            raise ValueError("requested claim kind is absent from its claim ceiling")
        if _CLAIM_STRENGTH_RANK[self.requested_strength] > _CLAIM_STRENGTH_RANK[maximum]:
            raise ValueError("requested claim strength exceeds its claim ceiling")
        return self

    @property
    def claim_contract_sha256(self) -> str:
        return canonical_sha256(self)


class _EpistemicContractBase(ProtocolModel):
    contract_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    purpose: EpistemicPurpose
    claim_ceiling: ClaimCeiling
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _version_has_exact_parent(self) -> "_EpistemicContractBase":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only epistemic contract version 1 may omit its revision parent")
        return self

    @property
    def contract_sha256(self) -> str:
        return canonical_sha256(self)


class HypothesisDiscriminationContract(_EpistemicContractBase):
    kind: Literal["hypothesis_discrimination"] = "hypothesis_discrimination"
    purpose: Literal[EpistemicPurpose.DISCRIMINATE] = EpistemicPurpose.DISCRIMINATE
    world_model_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    target_hypothesis_sha256s: tuple[str, ...] = Field(min_length=2, max_length=64)
    discrimination_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _targets_are_canonical(self) -> "HypothesisDiscriminationContract":
        canonical_sha256s(
            self.target_hypothesis_sha256s,
            "target hypothesis versions",
            required=True,
        )
        return self


class CharacterizationContract(_EpistemicContractBase):
    kind: Literal["characterization"] = "characterization"
    purpose: Literal[
        EpistemicPurpose.CHARACTERIZE,
        EpistemicPurpose.CALIBRATE,
        EpistemicPurpose.MAP_BOUNDARY,
        EpistemicPurpose.REPRODUCE,
    ]
    target_entity_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    observable_spec_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    coverage_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _targets_are_canonical(self) -> "CharacterizationContract":
        canonical_sha256s(self.target_entity_sha256s, "characterization targets", required=True)
        canonical_sha256s(
            self.observable_spec_sha256s, "characterization observables", required=True
        )
        return self


class EstimationContract(_EpistemicContractBase):
    kind: Literal["estimation"] = "estimation"
    purpose: Literal[EpistemicPurpose.ESTIMATE_EFFECT] = EpistemicPurpose.ESTIMATE_EFFECT
    estimand: str = Field(min_length=1, max_length=4_000)
    target_population_sha256: str = Field(pattern=SHA256_PATTERN)
    observable_spec_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    precision_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _observables_are_canonical(self) -> "EstimationContract":
        canonical_sha256s(self.observable_spec_sha256s, "estimation observables", required=True)
        return self


class ConstraintTestContract(_EpistemicContractBase):
    kind: Literal["constraint_test"] = "constraint_test"
    purpose: Literal[
        EpistemicPurpose.FALSIFY,
        EpistemicPurpose.MAP_BOUNDARY,
        EpistemicPurpose.REPRODUCE,
    ]
    constraint_statement: str = Field(min_length=1, max_length=8_000)
    observable_spec_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    violation_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _observables_are_canonical(self) -> "ConstraintTestContract":
        canonical_sha256s(self.observable_spec_sha256s, "constraint observables", required=True)
        return self


class CapabilityQualificationContract(_EpistemicContractBase):
    kind: Literal["capability_qualification"] = "capability_qualification"
    purpose: Literal[EpistemicPurpose.ACQUIRE_CAPABILITY] = EpistemicPurpose.ACQUIRE_CAPABILITY
    target_capability_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    qualification_rule_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _rules_are_canonical(self) -> "CapabilityQualificationContract":
        canonical_sha256s(self.qualification_rule_sha256s, "qualification rules", required=True)
        return self


class FormalDerivationContract(_EpistemicContractBase):
    kind: Literal["formal_derivation"] = "formal_derivation"
    purpose: Literal[EpistemicPurpose.DERIVE] = EpistemicPurpose.DERIVE
    proposition: str = Field(min_length=1, max_length=8_000)
    axiom_or_assumption_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    proof_obligation_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    proof_checker_capability_manifest_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _formal_inputs_are_canonical(self) -> "FormalDerivationContract":
        canonical_sha256s(self.axiom_or_assumption_sha256s, "formal assumptions", required=True)
        canonical_sha256s(self.proof_obligation_sha256s, "proof obligations", required=True)
        return self


class EvidenceSynthesisContract(_EpistemicContractBase):
    kind: Literal["evidence_synthesis"] = "evidence_synthesis"
    purpose: Literal[EpistemicPurpose.SYNTHESIZE] = EpistemicPurpose.SYNTHESIZE
    target_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=512)
    inclusion_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    exclusion_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    heterogeneity_analysis_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _claims_are_canonical(self) -> "EvidenceSynthesisContract":
        canonical_sha256s(self.target_claim_sha256s, "synthesis claim targets", required=True)
        return self


EpistemicContract: TypeAlias = Annotated[
    HypothesisDiscriminationContract
    | CharacterizationContract
    | EstimationContract
    | ConstraintTestContract
    | CapabilityQualificationContract
    | FormalDerivationContract
    | EvidenceSynthesisContract,
    Field(discriminator="kind"),
]


__all__ = [
    "CapabilityQualificationContract",
    "CharacterizationContract",
    "ClaimAllowance",
    "ClaimCeiling",
    "ClaimContract",
    "ClaimKind",
    "ClaimStrength",
    "ConstraintTestContract",
    "EpistemicContract",
    "EpistemicKind",
    "EpistemicPurpose",
    "EstimationContract",
    "EvidenceModality",
    "EvidenceSynthesisContract",
    "FormalDerivationContract",
    "HypothesisDiscriminationContract",
    "ObservableSpec",
    "ObservableValueKind",
    "ReplicationTier",
]
