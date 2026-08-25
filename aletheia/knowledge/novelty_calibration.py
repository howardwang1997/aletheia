"""Evaluator-owned F8-S5 calibration for recall, temporal false novelty, and stability."""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.prior_art_matching import PriorArtMatchingResolution
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    DifferenceComponent,
    KnowledgeModel,
    NoveltyClassification,
    PriorArtRelation,
    PriorArtRelationType,
    SearchSession,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_ONE_SIDED_95_Z = 1.6448536269514722
_STRONG_CLASSES = {
    NoveltyClassification.NOVEL_COMBINATION,
    NoveltyClassification.NOVEL_METHOD,
    NoveltyClassification.NOVEL_PHENOMENON,
}
_DIFFERENCE_ORDER = tuple(DifferenceComponent)


def _require_receipt_key(key: bytes) -> None:
    if len(key) < 32:
        raise ValueError("calibration receipt key must contain at least 32 bytes")


class NoveltyCalibrationSplit(str, Enum):
    VALIDATION = "validation"
    TEMPORAL_HOLDOUT = "temporal_holdout"


class NoveltyPerturbationKind(str, Enum):
    BASE = "base"
    CLAIM_PARAPHRASE = "claim_paraphrase"
    QUERY_SYNONYM = "query_synonym"
    ENTITY_ALIAS = "entity_alias"
    CONDITION_REORDER = "condition_reorder"


class CalibrationTrialOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class NoveltyCalibrationVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class CalibrationFailureSignal(str, Enum):
    TRIAL_ERROR = "trial_error"
    KNOWN_ANSWER_RECALL = "known_answer_recall"
    SEED_REFERENCE_RECOVERY = "seed_reference_recovery"
    CLASSIFICATION_ACCURACY = "classification_accuracy"
    FALSE_STRONG_NOVELTY = "false_strong_novelty"
    MISSED_STRONG_NOVELTY = "missed_strong_novelty"
    PERTURBATION_STABILITY = "perturbation_stability"
    NEAREST_PRIOR_MRR = "nearest_prior_mrr"


class NoveltyCalibrationPolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    confidence_level: Literal[0.95] = 0.95
    statistical_method: Literal["one_sided_wilson_95"] = "one_sided_wilson_95"
    minimum_validation_cases: int = Field(default=40, ge=40, le=100_000)
    minimum_temporal_holdout_cases: int = Field(default=40, ge=40, le=100_000)
    minimum_known_non_novel_cases_per_split: int = Field(default=30, ge=30)
    minimum_strong_novel_cases_per_split: int = Field(default=10, ge=10)
    minimum_semantics_preserving_variants: int = Field(default=3, ge=3, le=20)
    minimum_known_answer_recall_lower_bound: float = Field(default=0.80, ge=0.80, le=1.0)
    minimum_seed_reference_recovery_lower_bound: float = Field(default=0.80, ge=0.80, le=1.0)
    minimum_classification_accuracy_lower_bound: float = Field(default=0.75, ge=0.70, le=1.0)
    maximum_false_strong_novelty_upper_bound: float = Field(default=0.10, ge=0.0, le=0.10)
    maximum_missed_strong_novelty_upper_bound: float = Field(default=0.25, ge=0.0, le=0.25)
    minimum_perturbation_stability_lower_bound: float = Field(default=0.90, ge=0.85, le=1.0)
    minimum_nearest_prior_mrr: float = Field(default=0.80, ge=0.75, le=1.0)
    threshold_application: Literal["validation_and_holdout_both_must_pass"] = (
        "validation_and_holdout_both_must_pass"
    )
    author_exclusion_required: Literal[True] = True
    holdout_labels_evaluator_only: Literal[True] = True
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _sample_composition_is_possible(self) -> "NoveltyCalibrationPolicy":
        required = (
            self.minimum_known_non_novel_cases_per_split + self.minimum_strong_novel_cases_per_split
        )
        if self.minimum_validation_cases < required:
            raise ValueError("validation case floor cannot satisfy label composition")
        if self.minimum_temporal_holdout_cases < required:
            raise ValueError("temporal holdout floor cannot satisfy label composition")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class CalibrationEvaluatorManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    evaluator_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    evaluator_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_view_parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _evaluator_is_exact_and_unprivileged(self) -> "CalibrationEvaluatorManifest":
        if self.classification_policy_sha256 != NOVELTY_CLASSIFICATION_POLICY_SHA256:
            raise ValueError("calibration evaluator uses another novelty classification policy")
        if self.tool_names:
            raise ValueError("calibration evaluator cannot receive tool authority")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class NoveltyCalibrationVariant(KnowledgeModel):
    schema_version: Literal[1] = 1
    variant_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    kind: NoveltyPerturbationKind
    semantics_preserving: Literal[True] = True
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    perturbation_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def variant_sha256(self) -> str:
        return content_sha256(self)


class NoveltyCalibrationCase(KnowledgeModel):
    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    split: NoveltyCalibrationSplit
    domain: str = Field(min_length=1, max_length=256)
    temporal_cutoff: AwareDatetime
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_author_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    variants: tuple[NoveltyCalibrationVariant, ...] = Field(min_length=3)
    input_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _case_has_canonical_semantics_preserving_variants(self) -> "NoveltyCalibrationCase":
        authors = self.candidate_author_principal_sha256s
        if authors != tuple(sorted(set(authors))):
            raise ValueError("calibration candidate authors must be unique and sorted")
        if self.variants[0].kind is not NoveltyPerturbationKind.BASE:
            raise ValueError("calibration case must put its base variant first")
        if any(variant.kind is NoveltyPerturbationKind.BASE for variant in self.variants[1:]):
            raise ValueError("calibration case can contain only one base variant")
        ids = [variant.variant_id for variant in self.variants]
        hashes = [variant.variant_sha256 for variant in self.variants]
        claims = [variant.candidate_claim_sha256 for variant in self.variants]
        if (
            len(ids) != len(set(ids))
            or len(hashes) != len(set(hashes))
            or len(claims) != len(set(claims))
        ):
            raise ValueError("calibration variants require unique IDs, contents, and claims")
        if self.frozen_at < self.temporal_cutoff:
            raise ValueError("calibration case cannot freeze before its temporal cutoff")
        return self

    @property
    def case_sha256(self) -> str:
        return content_sha256(self)


class NoveltyCalibrationLabel(KnowledgeModel):
    schema_version: Literal[1] = 1
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_prior_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    expected_nearest_prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_seed_paper_sha256s: tuple[str, ...] = Field(min_length=1)
    expected_classification: NoveltyClassification
    expert_adjudicator_principal_sha256s: tuple[str, ...] = Field(min_length=2)
    adjudication_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    labeled_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _label_is_canonical_and_decisive(self) -> "NoveltyCalibrationLabel":
        if self.expected_classification is (NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE):
            raise ValueError("calibration labels cannot be coverage-indeterminate")
        if self.expected_prior_claim_sha256s != tuple(
            sorted(set(self.expected_prior_claim_sha256s))
        ):
            raise ValueError("known prior claims must be unique and sorted")
        if self.expected_nearest_prior_claim_sha256 not in self.expected_prior_claim_sha256s:
            raise ValueError("expected nearest prior must belong to known answers")
        if self.expected_seed_paper_sha256s != tuple(sorted(set(self.expected_seed_paper_sha256s))):
            raise ValueError("expected seed papers must be unique and sorted")
        adjudicators = self.expert_adjudicator_principal_sha256s
        if adjudicators != tuple(sorted(set(adjudicators))):
            raise ValueError("calibration adjudicators must be unique and sorted")
        return self

    @property
    def label_sha256(self) -> str:
        return content_sha256(self)


class NoveltyCalibrationSuite(KnowledgeModel):
    schema_version: Literal[1] = 1
    suite_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    policy: NoveltyCalibrationPolicy
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[NoveltyCalibrationCase, ...]
    labels_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    holdout_custody_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_at: AwareDatetime
    state: Literal["sealed"] = "sealed"

    @model_validator(mode="after")
    def _suite_has_two_frozen_nonoverlapping_splits(self) -> "NoveltyCalibrationSuite":
        expected_order = tuple(
            sorted(
                self.cases,
                key=lambda case: (
                    tuple(NoveltyCalibrationSplit).index(case.split),
                    case.case_id,
                ),
            )
        )
        if self.cases != expected_order:
            raise ValueError("calibration cases must use canonical split/case order")
        ids = [case.case_id for case in self.cases]
        hashes = [case.case_sha256 for case in self.cases]
        inputs = [case.input_evidence_sha256 for case in self.cases]
        if (
            len(ids) != len(set(ids))
            or len(hashes) != len(set(hashes))
            or len(inputs) != len(set(inputs))
        ):
            raise ValueError("calibration cases require unique IDs, contents, and inputs")
        by_split = {
            split: [case for case in self.cases if case.split is split]
            for split in NoveltyCalibrationSplit
        }
        if (
            len(by_split[NoveltyCalibrationSplit.VALIDATION]) < self.policy.minimum_validation_cases
            or len(by_split[NoveltyCalibrationSplit.TEMPORAL_HOLDOUT])
            < self.policy.minimum_temporal_holdout_cases
        ):
            raise ValueError("calibration suite does not meet frozen split sample floors")
        if any(
            len(case.variants) < self.policy.minimum_semantics_preserving_variants
            for case in self.cases
        ):
            raise ValueError("calibration case has too few frozen perturbation variants")
        validation_cutoffs = [
            case.temporal_cutoff for case in by_split[NoveltyCalibrationSplit.VALIDATION]
        ]
        holdout_cutoffs = [
            case.temporal_cutoff for case in by_split[NoveltyCalibrationSplit.TEMPORAL_HOLDOUT]
        ]
        if max(validation_cutoffs) >= min(holdout_cutoffs):
            raise ValueError("temporal holdout cutoffs must follow every validation cutoff")
        if self.sealed_at < max(
            self.policy.frozen_at,
            *(case.frozen_at for case in self.cases),
        ):
            raise ValueError("calibration suite sealed before its inputs")
        return self

    @property
    def suite_sha256(self) -> str:
        return content_sha256(self)


class CalibrationRelationView(KnowledgeModel):
    schema_version: Literal[1] = 1
    rank: int = Field(ge=1, le=100_000)
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: PriorArtRelationType
    difference_components: tuple[DifferenceComponent, ...]
    relation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _difference_shape_matches_relation(self) -> "CalibrationRelationView":
        if self.difference_components != tuple(
            sorted(set(self.difference_components), key=_DIFFERENCE_ORDER.index)
        ):
            raise ValueError("calibration difference components must be canonical")
        equivalent = self.relation is PriorArtRelationType.EQUIVALENT
        if equivalent == bool(self.difference_components):
            raise ValueError("calibration relation/difference shape is invalid")
        return self


def _classify_relation_views(
    relations: tuple[CalibrationRelationView, ...],
) -> NoveltyClassification:
    for relation in relations:
        if relation.relation is PriorArtRelationType.EQUIVALENT:
            return NoveltyClassification.KNOWN_EQUIVALENT
        if relation.relation in {
            PriorArtRelationType.SUBSUMES,
            PriorArtRelationType.SPECIAL_CASE,
        }:
            return NoveltyClassification.KNOWN_SPECIAL_CASE
    top = relations[0]
    if top.relation is PriorArtRelationType.CONTRADICTION:
        return NoveltyClassification.CONTRADICTORY_TO_PRIOR
    if top.relation is PriorArtRelationType.COMBINATION:
        return NoveltyClassification.NOVEL_COMBINATION
    if DifferenceComponent.METHOD in top.difference_components:
        return NoveltyClassification.NOVEL_METHOD
    if set(top.difference_components) & {
        DifferenceComponent.RELATION,
        DifferenceComponent.OBJECT,
        DifferenceComponent.EFFECT,
    }:
        return NoveltyClassification.NOVEL_PHENOMENON
    return NoveltyClassification.INCREMENTAL_EXTENSION


NOVELTY_CLASSIFICATION_POLICY_SHA256 = content_sha256(
    {
        "policy": "f8s5-relation-view-classification-v1",
        "blocking_precedence": [
            PriorArtRelationType.EQUIVALENT.value,
            PriorArtRelationType.SUBSUMES.value,
            PriorArtRelationType.SPECIAL_CASE.value,
        ],
        "top_relation_then_components": {
            "contradiction": NoveltyClassification.CONTRADICTORY_TO_PRIOR.value,
            "combination": NoveltyClassification.NOVEL_COMBINATION.value,
            "method_component": NoveltyClassification.NOVEL_METHOD.value,
            "relation_object_effect_component": (NoveltyClassification.NOVEL_PHENOMENON.value),
            "fallback": NoveltyClassification.INCREMENTAL_EXTENSION.value,
        },
    }
)


class CalibrationTrialPayload(KnowledgeModel):
    schema_version: Literal[1] = 1
    trial_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: NoveltyCalibrationSplit
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_art_resolution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: CalibrationTrialOutcome
    relations: tuple[CalibrationRelationView, ...] = ()
    predicted_classification: NoveltyClassification | None = None
    search_hit_paper_sha256s: tuple[str, ...] = ()
    failure_class: str | None = Field(default=None, max_length=256)
    failure_detail_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_is_complete_or_explicit_error(self) -> "CalibrationTrialPayload":
        ranks = [relation.rank for relation in self.relations]
        prior_hashes = [relation.prior_claim_sha256 for relation in self.relations]
        if ranks and ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("calibration relation ranks must be contiguous")
        if len(prior_hashes) != len(set(prior_hashes)):
            raise ValueError("calibration trial cannot repeat a prior claim")
        if self.search_hit_paper_sha256s != tuple(sorted(set(self.search_hit_paper_sha256s))):
            raise ValueError("calibration search-hit identities must be unique and sorted")
        if self.outcome is CalibrationTrialOutcome.SUCCESS:
            if (
                not self.relations
                or self.failure_class is not None
                or self.failure_detail_sha256 is not None
                or self.predicted_classification is not _classify_relation_views(self.relations)
            ):
                raise ValueError("successful calibration trial has incomplete derivation")
        elif (
            self.relations
            or self.predicted_classification is not None
            or self.search_hit_paper_sha256s
            or not self.failure_class
            or self.failure_detail_sha256 is None
        ):
            raise ValueError("failed calibration trial requires hashed failure and no judgment")
        return self

    @property
    def payload_sha256(self) -> str:
        return content_sha256(self)


class SignedCalibrationTrialReceipt(KnowledgeModel):
    schema_version: Literal[1] = 1
    payload: CalibrationTrialPayload
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _message(payload: CalibrationTrialPayload, key_id: str) -> bytes:
        return canonical_json_bytes(
            {
                "purpose": "f8s5-novelty-calibration-trial-v1",
                "key_id": key_id,
                "payload": payload.model_dump(mode="json", exclude_none=True),
            }
        )

    @classmethod
    def sign(
        cls, *, payload: CalibrationTrialPayload, key_id: str, key: bytes
    ) -> "SignedCalibrationTrialReceipt":
        _require_receipt_key(key)
        signature = hmac.new(
            key,
            cls._message(payload, key_id),
            hashlib.sha256,
        ).hexdigest()
        return cls(payload=payload, key_id=key_id, hmac_sha256=signature)

    def verify(self, *, key: bytes, expected_key_id: str) -> None:
        _require_receipt_key(key)
        if self.key_id != expected_key_id:
            raise ValueError("calibration trial receipt uses another evaluator key")
        expected = hmac.new(
            key,
            self._message(self.payload, self.key_id),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, self.hmac_sha256):
            raise ValueError("calibration trial receipt signature is invalid")

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _wilson_bounds(events: int, total: int) -> tuple[float, float]:
    if total < 1 or not 0 <= events <= total:
        raise ValueError("Wilson interval requires 0 <= events <= total")
    probability = events / total
    z_squared = _ONE_SIDED_95_Z**2
    denominator = 1.0 + z_squared / total
    center = (probability + z_squared / (2.0 * total)) / denominator
    margin = (
        _ONE_SIDED_95_Z
        * math.sqrt(probability * (1.0 - probability) / total + z_squared / (4.0 * total**2))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


class BinomialRate(KnowledgeModel):
    schema_version: Literal[1] = 1
    events: int = Field(ge=0)
    total: int = Field(ge=1)
    estimate: float = Field(ge=0.0, le=1.0)
    lower_bound: float = Field(ge=0.0, le=1.0)
    upper_bound: float = Field(ge=0.0, le=1.0)
    confidence_level: Literal[0.95] = 0.95
    method: Literal["one_sided_wilson_95"] = "one_sided_wilson_95"

    @model_validator(mode="after")
    def _statistics_are_derived(self) -> "BinomialRate":
        if self.events > self.total:
            raise ValueError("binomial events cannot exceed total")
        expected_lower, expected_upper = _wilson_bounds(self.events, self.total)
        if (
            self.estimate != self.events / self.total
            or self.lower_bound != expected_lower
            or self.upper_bound != expected_upper
        ):
            raise ValueError("binomial rate and confidence bounds are not derived")
        return self


def _rate(events: int, total: int) -> BinomialRate:
    lower, upper = _wilson_bounds(events, total)
    return BinomialRate(
        events=events,
        total=total,
        estimate=events / total,
        lower_bound=lower,
        upper_bound=upper,
    )


class ReciprocalRankMetric(KnowledgeModel):
    schema_version: Literal[1] = 1
    case_count: int = Field(ge=1)
    reciprocal_rank_sum: float = Field(ge=0.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _mean_is_derived(self) -> "ReciprocalRankMetric":
        if self.mean_reciprocal_rank != self.reciprocal_rank_sum / self.case_count:
            raise ValueError("mean reciprocal rank is not derived")
        return self


class CalibrationSplitMetrics(KnowledgeModel):
    schema_version: Literal[1] = 1
    split: NoveltyCalibrationSplit
    cases: int = Field(ge=1)
    trials: int = Field(ge=1)
    failed_trials: int = Field(ge=0)
    known_answer_recall: BinomialRate
    seed_reference_recovery: BinomialRate
    classification_accuracy: BinomialRate
    false_strong_novelty: BinomialRate
    missed_strong_novelty: BinomialRate
    perturbation_stability: BinomialRate
    nearest_prior_mrr: ReciprocalRankMetric


class CalibrationFailure(KnowledgeModel):
    schema_version: Literal[1] = 1
    split: NoveltyCalibrationSplit
    signal: CalibrationFailureSignal


class NoveltyCalibrationReport(KnowledgeModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    suite: NoveltyCalibrationSuite
    evaluator_manifest: CalibrationEvaluatorManifest
    labels: tuple[NoveltyCalibrationLabel, ...]
    trial_receipts: tuple[SignedCalibrationTrialReceipt, ...]
    metrics: tuple[CalibrationSplitMetrics, ...] = Field(min_length=2, max_length=2)
    failures: tuple[CalibrationFailure, ...]
    verdict: NoveltyCalibrationVerdict
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _report_is_completely_derived(self) -> "NoveltyCalibrationReport":
        _validate_calibration_report(self)
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class CommittedNoveltyCalibrationReport(KnowledgeModel):
    schema_version: Literal[1] = 1
    report: NoveltyCalibrationReport
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_report(self) -> "CommittedNoveltyCalibrationReport":
        payload = canonical_json_bytes(self.report)
        if (
            self.ledger.object_sha256 != self.report.report_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("novelty calibration ledger does not commit its report")
        return self


def _labels_commitment(labels: tuple[NoveltyCalibrationLabel, ...]) -> str:
    return content_sha256(
        {
            "policy": "f8s5-evaluator-only-label-commitment-v1",
            "label_sha256s": [label.label_sha256 for label in labels],
        }
    )


def _validate_labels(
    *,
    suite: NoveltyCalibrationSuite,
    labels: tuple[NoveltyCalibrationLabel, ...],
) -> None:
    if [label.case_sha256 for label in labels] != [case.case_sha256 for case in suite.cases]:
        raise ValueError("calibration labels must exactly preserve suite case order")
    if len({label.label_sha256 for label in labels}) != len(labels):
        raise ValueError("calibration labels must be unique")
    if _labels_commitment(labels) != suite.labels_commitment_sha256:
        raise ValueError("calibration labels differ from the sealed commitment")
    non_novel_counts = {split: 0 for split in NoveltyCalibrationSplit}
    strong_counts = {split: 0 for split in NoveltyCalibrationSplit}
    for case, label in zip(suite.cases, labels, strict=True):
        if set(case.candidate_author_principal_sha256s) & set(
            label.expert_adjudicator_principal_sha256s
        ):
            raise ValueError("candidate authors cannot adjudicate calibration novelty labels")
        if label.labeled_at > suite.sealed_at:
            raise ValueError("calibration label was not frozen before suite sealing")
        if label.expected_classification in _STRONG_CLASSES:
            strong_counts[case.split] += 1
        else:
            non_novel_counts[case.split] += 1
    for split in NoveltyCalibrationSplit:
        if (
            non_novel_counts[split] < suite.policy.minimum_known_non_novel_cases_per_split
            or strong_counts[split] < suite.policy.minimum_strong_novel_cases_per_split
        ):
            raise ValueError("calibration labels do not satisfy frozen class composition")


def build_novelty_calibration_suite(
    *,
    suite_id: str,
    policy: NoveltyCalibrationPolicy,
    system_manifest_sha256: str,
    cases: tuple[NoveltyCalibrationCase, ...],
    labels: tuple[NoveltyCalibrationLabel, ...],
    holdout_custody_manifest_sha256: str,
    sealed_at: AwareDatetime,
) -> NoveltyCalibrationSuite:
    suite = NoveltyCalibrationSuite(
        suite_id=suite_id,
        policy=policy,
        system_manifest_sha256=system_manifest_sha256,
        cases=cases,
        labels_commitment_sha256=_labels_commitment(labels),
        holdout_custody_manifest_sha256=holdout_custody_manifest_sha256,
        sealed_at=sealed_at,
    )
    _validate_labels(suite=suite, labels=labels)
    return suite


def _relation_view(relation: PriorArtRelation) -> CalibrationRelationView:
    return CalibrationRelationView(
        rank=relation.rank,
        prior_claim_sha256=relation.prior_claim_sha256,
        relation=relation.relation,
        difference_components=tuple(
            sorted(
                {difference.component for difference in relation.differences},
                key=_DIFFERENCE_ORDER.index,
            )
        ),
        relation_sha256=relation.relation_sha256,
    )


def classify_prior_art_relations(
    relations: tuple[PriorArtRelation, ...],
) -> NoveltyClassification:
    """Apply the frozen calibration classifier to exact reviewed prior-art relations."""
    if not relations:
        raise ValueError("novelty classification requires at least one prior-art relation")
    ranks = [relation.rank for relation in relations]
    if ranks != list(range(1, len(relations) + 1)):
        raise ValueError("novelty classification requires contiguous ranked prior art")
    return _classify_relation_views(tuple(_relation_view(item) for item in relations))


def issue_calibration_trial_receipt(
    *,
    suite: NoveltyCalibrationSuite,
    case: NoveltyCalibrationCase,
    variant: NoveltyCalibrationVariant,
    resolution: PriorArtMatchingResolution,
    search_session: SearchSession,
    evaluator_manifest: CalibrationEvaluatorManifest,
    receipt_key: bytes,
    completed_at: AwareDatetime,
) -> SignedCalibrationTrialReceipt:
    if case not in suite.cases or variant not in case.variants:
        raise ValueError("calibration trial case/variant is outside the sealed suite")
    if evaluator_manifest.frozen_at > suite.sealed_at:
        raise ValueError("calibration evaluator must freeze before suite sealing")
    matching_protocol = resolution.execution.protocol
    if (
        matching_protocol.claim_graph_bundle_sha256 != variant.graph_bundle_sha256
        or matching_protocol.corpus_snapshot_sha256 != case.corpus_snapshot_sha256
        or matching_protocol.matcher_manifest.manifest_sha256 != suite.system_manifest_sha256
    ):
        raise ValueError("calibration matching resolution is bound to another input/system")
    if (
        search_session.protocol_sha256 != variant.search_protocol_sha256
        or search_session.corpus_snapshot_sha256 != case.corpus_snapshot_sha256
    ):
        raise ValueError("calibration search session is bound to another input")
    if resolution.execution.failures or not resolution.accepted:
        raise ValueError("successful calibration receipt requires a resolved matching result")
    relations = tuple(_relation_view(item.relation) for item in resolution.accepted)
    if any(
        item.relation.candidate_claim_sha256 != variant.candidate_claim_sha256
        for item in resolution.accepted
    ):
        raise ValueError("calibration resolution relation belongs to another variant claim")
    if completed_at < max(resolution.resolved_at, search_session.ended_at):
        raise ValueError("calibration trial cannot complete before its evidence")
    hit_hashes = tuple(
        sorted(
            {hit.paper_snapshot_sha256 for query in search_session.queries for hit in query.hits}
        )
    )
    payload = CalibrationTrialPayload(
        trial_id=f"trial:{case.case_id}:{variant.variant_id}",
        case_sha256=case.case_sha256,
        variant_sha256=variant.variant_sha256,
        split=case.split,
        system_manifest_sha256=suite.system_manifest_sha256,
        evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
        candidate_claim_sha256=variant.candidate_claim_sha256,
        prior_art_resolution_sha256=resolution.resolution_sha256,
        search_session_sha256=search_session.session_sha256,
        outcome=CalibrationTrialOutcome.SUCCESS,
        relations=relations,
        predicted_classification=_classify_relation_views(relations),
        search_hit_paper_sha256s=hit_hashes,
        completed_at=completed_at,
    )
    return SignedCalibrationTrialReceipt.sign(
        payload=payload,
        key_id=evaluator_manifest.receipt_key_id,
        key=receipt_key,
    )


def issue_failed_calibration_trial_receipt(
    *,
    suite: NoveltyCalibrationSuite,
    case: NoveltyCalibrationCase,
    variant: NoveltyCalibrationVariant,
    evaluator_manifest: CalibrationEvaluatorManifest,
    receipt_key: bytes,
    failure: Exception,
    completed_at: AwareDatetime,
) -> SignedCalibrationTrialReceipt:
    if case not in suite.cases or variant not in case.variants:
        raise ValueError("failed calibration trial is outside the sealed suite")
    if evaluator_manifest.frozen_at > suite.sealed_at:
        raise ValueError("calibration evaluator must freeze before suite sealing")
    if completed_at < suite.sealed_at:
        raise ValueError("failed calibration trial cannot predate suite sealing")
    failure_class = f"{type(failure).__module__}.{type(failure).__qualname__}"[:256]
    failure_detail = content_sha256(
        {
            "failure_class": failure_class,
            "message_sha256": hashlib.sha256(
                str(failure).encode("utf-8", errors="replace")
            ).hexdigest(),
        }
    )
    payload = CalibrationTrialPayload(
        trial_id=f"trial:{case.case_id}:{variant.variant_id}",
        case_sha256=case.case_sha256,
        variant_sha256=variant.variant_sha256,
        split=case.split,
        system_manifest_sha256=suite.system_manifest_sha256,
        evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
        candidate_claim_sha256=variant.candidate_claim_sha256,
        prior_art_resolution_sha256=content_sha256(
            {"missing_resolution_for_variant": variant.variant_sha256}
        ),
        search_session_sha256=content_sha256(
            {"missing_search_for_variant": variant.variant_sha256}
        ),
        outcome=CalibrationTrialOutcome.ERROR,
        failure_class=failure_class,
        failure_detail_sha256=failure_detail,
        completed_at=completed_at,
    )
    return SignedCalibrationTrialReceipt.sign(
        payload=payload,
        key_id=evaluator_manifest.receipt_key_id,
        key=receipt_key,
    )


def _derive_split_metrics(
    *,
    split: NoveltyCalibrationSplit,
    suite: NoveltyCalibrationSuite,
    labels_by_case: Mapping[str, NoveltyCalibrationLabel],
    payloads_by_variant: Mapping[str, CalibrationTrialPayload],
) -> CalibrationSplitMetrics:
    cases = [case for case in suite.cases if case.split is split]
    known_events = 0
    known_total = 0
    seed_events = 0
    seed_total = 0
    classification_events = 0
    false_strong_events = 0
    false_strong_total = 0
    missed_strong_events = 0
    missed_strong_total = 0
    stability_events = 0
    stability_total = 0
    reciprocal_rank_sum = 0.0
    failed_trials = 0

    for case in cases:
        label = labels_by_case[case.case_sha256]
        base = payloads_by_variant[case.variants[0].variant_sha256]
        base_success = base.outcome is CalibrationTrialOutcome.SUCCESS
        ranked = tuple(item.prior_claim_sha256 for item in base.relations)
        recovered = set(ranked) if base_success else set()
        known_total += len(label.expected_prior_claim_sha256s)
        known_events += len(recovered & set(label.expected_prior_claim_sha256s))
        seed_total += len(label.expected_seed_paper_sha256s)
        seed_events += len(
            set(base.search_hit_paper_sha256s) & set(label.expected_seed_paper_sha256s)
        )
        predicted = base.predicted_classification if base_success else None
        classification_events += int(predicted is label.expected_classification)
        expected_strong = label.expected_classification in _STRONG_CLASSES
        predicted_strong = predicted in _STRONG_CLASSES if predicted is not None else False
        if expected_strong:
            missed_strong_total += 1
            missed_strong_events += int(not predicted_strong)
        else:
            false_strong_total += 1
            false_strong_events += int(predicted_strong)
        if label.expected_nearest_prior_claim_sha256 in ranked:
            reciprocal_rank_sum += 1.0 / (
                ranked.index(label.expected_nearest_prior_claim_sha256) + 1
            )
        for variant in case.variants[1:]:
            payload = payloads_by_variant[variant.variant_sha256]
            stability_total += 1
            stability_events += int(
                base_success
                and payload.outcome is CalibrationTrialOutcome.SUCCESS
                and payload.predicted_classification is base.predicted_classification
                and tuple(item.prior_claim_sha256 for item in payload.relations) == ranked
            )
        failed_trials += sum(
            payloads_by_variant[variant.variant_sha256].outcome is CalibrationTrialOutcome.ERROR
            for variant in case.variants
        )

    return CalibrationSplitMetrics(
        split=split,
        cases=len(cases),
        trials=sum(len(case.variants) for case in cases),
        failed_trials=failed_trials,
        known_answer_recall=_rate(known_events, known_total),
        seed_reference_recovery=_rate(seed_events, seed_total),
        classification_accuracy=_rate(classification_events, len(cases)),
        false_strong_novelty=_rate(false_strong_events, false_strong_total),
        missed_strong_novelty=_rate(missed_strong_events, missed_strong_total),
        perturbation_stability=_rate(stability_events, stability_total),
        nearest_prior_mrr=ReciprocalRankMetric(
            case_count=len(cases),
            reciprocal_rank_sum=reciprocal_rank_sum,
            mean_reciprocal_rank=reciprocal_rank_sum / len(cases),
        ),
    )


def _derive_failures(
    *,
    policy: NoveltyCalibrationPolicy,
    metrics: tuple[CalibrationSplitMetrics, ...],
) -> tuple[CalibrationFailure, ...]:
    failures: list[CalibrationFailure] = []
    for item in metrics:
        checks = (
            (
                CalibrationFailureSignal.TRIAL_ERROR,
                item.failed_trials > 0,
            ),
            (
                CalibrationFailureSignal.KNOWN_ANSWER_RECALL,
                item.known_answer_recall.lower_bound
                < policy.minimum_known_answer_recall_lower_bound,
            ),
            (
                CalibrationFailureSignal.SEED_REFERENCE_RECOVERY,
                item.seed_reference_recovery.lower_bound
                < policy.minimum_seed_reference_recovery_lower_bound,
            ),
            (
                CalibrationFailureSignal.CLASSIFICATION_ACCURACY,
                item.classification_accuracy.lower_bound
                < policy.minimum_classification_accuracy_lower_bound,
            ),
            (
                CalibrationFailureSignal.FALSE_STRONG_NOVELTY,
                item.false_strong_novelty.upper_bound
                > policy.maximum_false_strong_novelty_upper_bound,
            ),
            (
                CalibrationFailureSignal.MISSED_STRONG_NOVELTY,
                item.missed_strong_novelty.upper_bound
                > policy.maximum_missed_strong_novelty_upper_bound,
            ),
            (
                CalibrationFailureSignal.PERTURBATION_STABILITY,
                item.perturbation_stability.lower_bound
                < policy.minimum_perturbation_stability_lower_bound,
            ),
            (
                CalibrationFailureSignal.NEAREST_PRIOR_MRR,
                item.nearest_prior_mrr.mean_reciprocal_rank < policy.minimum_nearest_prior_mrr,
            ),
        )
        failures.extend(
            CalibrationFailure(split=item.split, signal=signal)
            for signal, failed in checks
            if failed
        )
    return tuple(failures)


def _derive_report_parts(
    *,
    suite: NoveltyCalibrationSuite,
    labels: tuple[NoveltyCalibrationLabel, ...],
    receipts: tuple[SignedCalibrationTrialReceipt, ...],
) -> tuple[tuple[CalibrationSplitMetrics, ...], tuple[CalibrationFailure, ...]]:
    labels_by_case = {label.case_sha256: label for label in labels}
    payloads_by_variant = {receipt.payload.variant_sha256: receipt.payload for receipt in receipts}
    metrics = tuple(
        _derive_split_metrics(
            split=split,
            suite=suite,
            labels_by_case=labels_by_case,
            payloads_by_variant=payloads_by_variant,
        )
        for split in NoveltyCalibrationSplit
    )
    return metrics, _derive_failures(policy=suite.policy, metrics=metrics)


def _validate_trial_bindings(
    *,
    suite: NoveltyCalibrationSuite,
    evaluator_manifest: CalibrationEvaluatorManifest,
    trial_receipts: tuple[SignedCalibrationTrialReceipt, ...],
) -> None:
    if evaluator_manifest.frozen_at > suite.sealed_at:
        raise ValueError("calibration evaluator must freeze before suite sealing")
    expected_pairs = [(case, variant) for case in suite.cases for variant in case.variants]
    if len(trial_receipts) != len(expected_pairs):
        raise ValueError("calibration report must include every case variant exactly once")
    trial_ids: set[str] = set()
    receipt_hashes: set[str] = set()
    resolution_hashes: set[str] = set()
    search_hashes: set[str] = set()
    for (case, variant), receipt in zip(expected_pairs, trial_receipts, strict=True):
        payload = receipt.payload
        if (
            payload.case_sha256 != case.case_sha256
            or payload.variant_sha256 != variant.variant_sha256
            or payload.split is not case.split
            or payload.candidate_claim_sha256 != variant.candidate_claim_sha256
            or payload.system_manifest_sha256 != suite.system_manifest_sha256
            or payload.evaluator_manifest_sha256 != evaluator_manifest.manifest_sha256
            or receipt.key_id != evaluator_manifest.receipt_key_id
        ):
            raise ValueError("calibration trial differs from sealed case/variant order")
        if payload.completed_at < suite.sealed_at:
            raise ValueError("calibration trial predates suite sealing")
        trial_ids.add(payload.trial_id)
        receipt_hashes.add(receipt.receipt_sha256)
        resolution_hashes.add(payload.prior_art_resolution_sha256)
        search_hashes.add(payload.search_session_sha256)
    expected_count = len(expected_pairs)
    if not all(
        len(identities) == expected_count
        for identities in (
            trial_ids,
            receipt_hashes,
            resolution_hashes,
            search_hashes,
        )
    ):
        raise ValueError("calibration trials cannot reuse IDs, receipts, or evidence artifacts")


def _validate_calibration_report(report: NoveltyCalibrationReport) -> None:
    suite = report.suite
    _validate_labels(suite=suite, labels=report.labels)
    _validate_trial_bindings(
        suite=suite,
        evaluator_manifest=report.evaluator_manifest,
        trial_receipts=report.trial_receipts,
    )
    expected_metrics, expected_failures = _derive_report_parts(
        suite=suite,
        labels=report.labels,
        receipts=report.trial_receipts,
    )
    if report.metrics != expected_metrics or report.failures != expected_failures:
        raise ValueError("calibration metrics/failures are not derived from exact trials")
    expected_verdict = (
        NoveltyCalibrationVerdict.FAIL if report.failures else NoveltyCalibrationVerdict.PASS
    )
    if report.verdict is not expected_verdict:
        raise ValueError("calibration verdict is not derived")
    if report.generated_at < max(receipt.payload.completed_at for receipt in report.trial_receipts):
        raise ValueError("calibration report predates a trial")


def build_novelty_calibration_report(
    *,
    report_id: str,
    suite: NoveltyCalibrationSuite,
    evaluator_manifest: CalibrationEvaluatorManifest,
    labels: tuple[NoveltyCalibrationLabel, ...],
    trial_receipts: tuple[SignedCalibrationTrialReceipt, ...],
    receipt_key: bytes,
    generated_at: AwareDatetime,
) -> NoveltyCalibrationReport:
    _validate_labels(suite=suite, labels=labels)
    _validate_trial_bindings(
        suite=suite,
        evaluator_manifest=evaluator_manifest,
        trial_receipts=trial_receipts,
    )
    for receipt in trial_receipts:
        receipt.verify(
            key=receipt_key,
            expected_key_id=evaluator_manifest.receipt_key_id,
        )
    metrics, failures = _derive_report_parts(
        suite=suite,
        labels=labels,
        receipts=trial_receipts,
    )
    return NoveltyCalibrationReport(
        report_id=report_id,
        suite=suite,
        evaluator_manifest=evaluator_manifest,
        labels=labels,
        trial_receipts=trial_receipts,
        metrics=metrics,
        failures=failures,
        verdict=(NoveltyCalibrationVerdict.FAIL if failures else NoveltyCalibrationVerdict.PASS),
        generated_at=generated_at,
    )


def commit_novelty_calibration_report(
    *,
    archive: ContentAddressedResponseArchive,
    report: NoveltyCalibrationReport,
) -> CommittedNoveltyCalibrationReport:
    ledger = archive.store_ledger(
        value=report,
        object_sha256=report.report_sha256,
        archived_at=report.generated_at,
    )
    return CommittedNoveltyCalibrationReport(report=report, ledger=ledger)


def load_novelty_calibration_report(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
    receipt_key: bytes,
) -> NoveltyCalibrationReport:
    payload = archive.read_ledger(ledger)
    report = NoveltyCalibrationReport.model_validate_json(payload)
    if canonical_json_bytes(report) != payload:
        raise ValueError("archived novelty calibration report is not canonical JSON")
    if report.report_sha256 != ledger.object_sha256:
        raise ValueError("archived novelty calibration report changed object identity")
    for receipt in report.trial_receipts:
        receipt.verify(
            key=receipt_key,
            expected_key_id=report.evaluator_manifest.receipt_key_id,
        )
    return report


__all__ = [
    "NOVELTY_CLASSIFICATION_POLICY_SHA256",
    "BinomialRate",
    "CalibrationEvaluatorManifest",
    "CalibrationFailure",
    "CalibrationFailureSignal",
    "CalibrationRelationView",
    "CalibrationSplitMetrics",
    "CalibrationTrialOutcome",
    "CalibrationTrialPayload",
    "CommittedNoveltyCalibrationReport",
    "NoveltyCalibrationCase",
    "NoveltyCalibrationLabel",
    "NoveltyCalibrationPolicy",
    "NoveltyCalibrationReport",
    "NoveltyCalibrationSplit",
    "NoveltyCalibrationSuite",
    "NoveltyCalibrationVariant",
    "NoveltyCalibrationVerdict",
    "NoveltyPerturbationKind",
    "ReciprocalRankMetric",
    "SignedCalibrationTrialReceipt",
    "build_novelty_calibration_report",
    "build_novelty_calibration_suite",
    "classify_prior_art_relations",
    "commit_novelty_calibration_report",
    "issue_calibration_trial_receipt",
    "issue_failed_calibration_trial_receipt",
    "load_novelty_calibration_report",
]
