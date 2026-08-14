"""Reviewed F8-S5 novelty decisions and the research-direction gate."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.novelty_calibration import classify_prior_art_relations
from aletheia.knowledge.novelty_coverage import (
    CalibratedNoveltyCoverageAssessment,
)
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    ClaimOrigin,
    ComponentDifference,
    CoverageVerdict,
    KnowledgeModel,
    NoveltyAssessment,
    NoveltyClaimCeiling,
    NoveltyClassification,
    NoveltyPolicy,
    NoveltyReview,
    NoveltyReviewVerdict,
    PriorArtRelation,
    PriorArtRelationType,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_STRONG_CLASSES = {
    NoveltyClassification.NOVEL_COMBINATION,
    NoveltyClassification.NOVEL_METHOD,
    NoveltyClassification.NOVEL_PHENOMENON,
}
_REQUIRED_REVIEW_ROLES = {"domain_expert", "research_librarian"}


class CandidateAuthorshipManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    claim_graph_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    author_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    authorship_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _identities_are_canonical(self) -> "CandidateAuthorshipManifest":
        for values, name in (
            (self.candidate_claim_sha256s, "candidate claims"),
            (self.author_principal_sha256s, "candidate authors"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be unique and sorted")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class NoveltyEvidencePackage(KnowledgeModel):
    schema_version: Literal[1] = 1
    package_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    novelty_policy: NoveltyPolicy
    authorship_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nearest_prior_art_relation_sha256s: tuple[str, ...] = Field(min_length=1)
    derived_classification: NoveltyClassification
    exact_difference_sha256s: tuple[str, ...]
    base_blockers: tuple[str, ...]
    temporal_cutoff: AwareDatetime
    temporal_limitations: str = Field(min_length=1, max_length=4096)
    model_prior_limitations: str = Field(min_length=1, max_length=4096)
    contamination_disclosure: str = Field(min_length=1, max_length=4096)
    assembled_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _package_has_canonical_evidence(self) -> "NoveltyEvidencePackage":
        if len(self.nearest_prior_art_relation_sha256s) != len(
            set(self.nearest_prior_art_relation_sha256s)
        ):
            raise ValueError("novelty evidence package cannot repeat prior-art relations")
        if len(self.exact_difference_sha256s) != len(set(self.exact_difference_sha256s)):
            raise ValueError("novelty evidence package cannot repeat exact differences")
        if any(not blocker.strip() for blocker in self.base_blockers) or len(
            self.base_blockers
        ) != len(set(self.base_blockers)):
            raise ValueError("novelty evidence blockers must be non-blank and unique")
        return self

    @property
    def package_sha256(self) -> str:
        return content_sha256(self)


class CalibratedNoveltyReview(KnowledgeModel):
    schema_version: Literal[1] = 1
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_role: Literal["domain_expert", "methodologist", "research_librarian"]
    candidate_author_excluded: Literal[True] = True
    verdict: NoveltyReviewVerdict
    rationale_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def review_sha256(self) -> str:
        return content_sha256(self)


class ReviewedNoveltyDecision(KnowledgeModel):
    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    coverage: CalibratedNoveltyCoverageAssessment
    authorship_manifest: CandidateAuthorshipManifest
    evidence_package: NoveltyEvidencePackage
    independent_reviews: tuple[CalibratedNoveltyReview, ...]
    assessment: NoveltyAssessment
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _decision_is_rederived_from_reviewed_evidence(self) -> "ReviewedNoveltyDecision":
        _validate_authorship(
            coverage=self.coverage,
            authorship_manifest=self.authorship_manifest,
        )
        expected_package = _build_evidence_package(
            package_id=self.evidence_package.package_id,
            coverage=self.coverage,
            authorship_manifest=self.authorship_manifest,
            candidate_claim_sha256=self.evidence_package.candidate_claim_sha256,
            temporal_limitations=self.evidence_package.temporal_limitations,
            model_prior_limitations=self.evidence_package.model_prior_limitations,
            contamination_disclosure=self.evidence_package.contamination_disclosure,
            assembled_at=self.evidence_package.assembled_at,
        )
        if self.evidence_package != expected_package:
            raise ValueError("novelty evidence package is not derived from exact artifacts")
        _validate_reviews(
            evidence_package=self.evidence_package,
            authorship_manifest=self.authorship_manifest,
            reviews=self.independent_reviews,
        )
        expected_assessment = _derive_novelty_assessment(
            assessment_id=self.assessment.assessment_id,
            coverage=self.coverage,
            authorship_manifest=self.authorship_manifest,
            evidence_package=self.evidence_package,
            reviews=self.independent_reviews,
            assessed_at=self.assessment.assessed_at,
        )
        if self.assessment != expected_assessment:
            raise ValueError("novelty assessment is not derived from exact reviews")
        if self.generated_at != self.assessment.assessed_at:
            raise ValueError("reviewed novelty decision and assessment times must match")
        if self.generated_at < max(
            (
                self.evidence_package.assembled_at,
                *(review.reviewed_at for review in self.independent_reviews),
            )
        ):
            raise ValueError("reviewed novelty decision predates its evidence/reviews")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class ResearchDirectionDisposition(str, Enum):
    ADVANCE_STRONG = "advance_strong_novel_direction"
    ADVANCE_BOUNDED = "advance_bounded_direction"
    REJECT_KNOWN = "reject_known_direction"
    RESEARCH_MORE = "research_more_before_direction"
    BLOCKED_COVERAGE = "blocked_indeterminate_coverage"


class ResearchDirectionGate(KnowledgeModel):
    schema_version: Literal[1] = 1
    gate_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    novelty_decision: ReviewedNoveltyDecision
    disposition: ResearchDirectionDisposition
    experiment_authorized: bool
    maximum_novelty_claim: NoveltyClaimCeiling
    rationale_codes: tuple[str, ...] = Field(min_length=1)
    decided_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _gate_is_mechanical(self) -> "ResearchDirectionGate":
        disposition, authorized, codes = _derive_direction_gate(self.novelty_decision)
        if (
            self.disposition is not disposition
            or self.experiment_authorized != authorized
            or self.rationale_codes != codes
            or self.maximum_novelty_claim
            is not self.novelty_decision.assessment.claim_strength_ceiling
        ):
            raise ValueError("research direction gate is not mechanically derived")
        if self.decided_at < self.novelty_decision.generated_at:
            raise ValueError("research direction gate predates its novelty decision")
        return self

    @property
    def gate_sha256(self) -> str:
        return content_sha256(self)


class CommittedResearchDirectionGate(KnowledgeModel):
    schema_version: Literal[1] = 1
    gate: ResearchDirectionGate
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_gate(self) -> "CommittedResearchDirectionGate":
        payload = canonical_json_bytes(self.gate)
        if (
            self.ledger.object_sha256 != self.gate.gate_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("research direction ledger does not commit its gate")
        return self


def _verify_calibration_receipts(
    *,
    coverage: CalibratedNoveltyCoverageAssessment,
    calibration_receipt_key: bytes,
) -> None:
    calibration = coverage.calibration_report
    for receipt in calibration.trial_receipts:
        receipt.verify(
            key=calibration_receipt_key,
            expected_key_id=calibration.evaluator_manifest.receipt_key_id,
        )


def build_candidate_authorship_manifest(
    *,
    manifest_id: str,
    coverage: CalibratedNoveltyCoverageAssessment,
    candidate_claim_sha256s: tuple[str, ...],
    author_principal_sha256s: tuple[str, ...],
    authorship_evidence_sha256: str,
    frozen_at: AwareDatetime,
) -> CandidateAuthorshipManifest:
    claims = {
        claim.claim_sha256: claim
        for claim in coverage.claim_graph_bundle.graph.claims
        if claim.origin is ClaimOrigin.CANDIDATE
    }
    requested = tuple(sorted(candidate_claim_sha256s))
    if not requested or not set(requested).issubset(claims):
        raise ValueError("authorship manifest references a non-candidate claim")
    artifacts = {claims[claim].candidate_artifact_sha256 for claim in requested}
    if len(artifacts) != 1 or None in artifacts:
        raise ValueError("authorship manifest must cover one exact candidate artifact")
    if frozen_at > coverage.generated_at:
        raise ValueError("candidate authorship must freeze before novelty evidence assembly")
    return CandidateAuthorshipManifest(
        manifest_id=manifest_id,
        claim_graph_bundle_sha256=coverage.claim_graph_bundle.bundle_sha256,
        candidate_artifact_sha256=next(iter(artifacts)),
        candidate_claim_sha256s=requested,
        author_principal_sha256s=tuple(sorted(author_principal_sha256s)),
        authorship_evidence_sha256=authorship_evidence_sha256,
        frozen_at=frozen_at,
    )


def _validate_authorship(
    *,
    coverage: CalibratedNoveltyCoverageAssessment,
    authorship_manifest: CandidateAuthorshipManifest,
) -> None:
    claims = {
        claim.claim_sha256: claim
        for claim in coverage.claim_graph_bundle.graph.claims
        if claim.origin is ClaimOrigin.CANDIDATE
    }
    if (
        authorship_manifest.claim_graph_bundle_sha256 != coverage.claim_graph_bundle.bundle_sha256
        or not set(authorship_manifest.candidate_claim_sha256s).issubset(claims)
        or any(
            claims[claim].candidate_artifact_sha256 != authorship_manifest.candidate_artifact_sha256
            for claim in authorship_manifest.candidate_claim_sha256s
        )
        or authorship_manifest.frozen_at > coverage.generated_at
    ):
        raise ValueError("candidate authorship is bound to another graph/artifact/time")


def build_calibrated_novelty_policy(
    *, coverage: CalibratedNoveltyCoverageAssessment
) -> NoveltyPolicy:
    coverage_policy = coverage.search_assessment.report.policy
    return NoveltyPolicy(
        policy_id=f"f8s5-novelty-{coverage_policy.policy_sha256[:20]}",
        minimum_nearest_prior_art=coverage_policy.minimum_nearest_prior_art,
        minimum_independent_reviewers=coverage_policy.minimum_independent_reviewers,
        frozen_at=coverage_policy.frozen_at,
    )


def _candidate_relations(
    *,
    coverage: CalibratedNoveltyCoverageAssessment,
    candidate_claim_sha256: str,
) -> tuple[PriorArtRelation, ...]:
    relations = tuple(
        item.relation
        for item in coverage.prior_art_resolution.accepted
        if item.relation.candidate_claim_sha256 == candidate_claim_sha256
    )
    if not relations:
        raise ValueError("novelty decision requires resolved prior art for its candidate")
    ranks = [relation.rank for relation in relations]
    if ranks != list(range(1, len(relations) + 1)):
        raise ValueError("candidate prior art must retain contiguous resolved ranks")
    return relations


def _decisive_relation(
    relations: tuple[PriorArtRelation, ...],
) -> PriorArtRelation:
    for relation in relations:
        if relation.relation in {
            PriorArtRelationType.EQUIVALENT,
            PriorArtRelationType.SUBSUMES,
            PriorArtRelationType.SPECIAL_CASE,
        }:
            return relation
    return relations[0]


def _build_evidence_package(
    *,
    package_id: str,
    coverage: CalibratedNoveltyCoverageAssessment,
    authorship_manifest: CandidateAuthorshipManifest,
    candidate_claim_sha256: str,
    temporal_limitations: str,
    model_prior_limitations: str,
    contamination_disclosure: str,
    assembled_at: AwareDatetime,
) -> NoveltyEvidencePackage:
    _validate_authorship(
        coverage=coverage,
        authorship_manifest=authorship_manifest,
    )
    if candidate_claim_sha256 not in authorship_manifest.candidate_claim_sha256s:
        raise ValueError("novelty package candidate is outside the authorship manifest")
    if assembled_at < coverage.generated_at:
        raise ValueError("novelty evidence package predates calibrated coverage")
    relations = _candidate_relations(
        coverage=coverage,
        candidate_claim_sha256=candidate_claim_sha256,
    )
    if coverage.decision_verdict is CoverageVerdict.INSUFFICIENT:
        classification = NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
        differences: tuple[ComponentDifference, ...] = ()
    else:
        classification = classify_prior_art_relations(relations)
        differences = _decisive_relation(relations).differences
    return NoveltyEvidencePackage(
        package_id=package_id,
        coverage_sha256=coverage.coverage_sha256,
        novelty_policy=build_calibrated_novelty_policy(coverage=coverage),
        authorship_manifest_sha256=authorship_manifest.manifest_sha256,
        candidate_claim_sha256=candidate_claim_sha256,
        nearest_prior_art_relation_sha256s=tuple(
            relation.relation_sha256 for relation in relations
        ),
        derived_classification=classification,
        exact_difference_sha256s=tuple(content_sha256(difference) for difference in differences),
        base_blockers=coverage.decision_blockers,
        temporal_cutoff=coverage.ingestion_bundle.corpus.cutoff_time,
        temporal_limitations=temporal_limitations,
        model_prior_limitations=model_prior_limitations,
        contamination_disclosure=contamination_disclosure,
        assembled_at=assembled_at,
    )


def build_novelty_evidence_package(
    *,
    package_id: str,
    coverage: CalibratedNoveltyCoverageAssessment,
    authorship_manifest: CandidateAuthorshipManifest,
    candidate_claim_sha256: str,
    temporal_limitations: str,
    model_prior_limitations: str,
    contamination_disclosure: str,
    assembled_at: AwareDatetime,
) -> NoveltyEvidencePackage:
    return _build_evidence_package(
        package_id=package_id,
        coverage=coverage,
        authorship_manifest=authorship_manifest,
        candidate_claim_sha256=candidate_claim_sha256,
        temporal_limitations=temporal_limitations,
        model_prior_limitations=model_prior_limitations,
        contamination_disclosure=contamination_disclosure,
        assembled_at=assembled_at,
    )


def _validate_reviews(
    *,
    evidence_package: NoveltyEvidencePackage,
    authorship_manifest: CandidateAuthorshipManifest,
    reviews: tuple[CalibratedNoveltyReview, ...],
) -> None:
    expected_order = tuple(sorted(reviews, key=lambda review: review.review_id))
    if reviews != expected_order:
        raise ValueError("independent novelty reviews must use canonical review-ID order")
    ids = [review.review_id for review in reviews]
    principals = [review.reviewer_principal_sha256 for review in reviews]
    receipts = [review.attestation_receipt_sha256 for review in reviews]
    if (
        len(ids) != len(set(ids))
        or len(principals) != len(set(principals))
        or len(receipts) != len(set(receipts))
    ):
        raise ValueError("independent novelty review identities must be unique")
    authors = set(authorship_manifest.author_principal_sha256s)
    if authors.intersection(principals):
        raise ValueError("candidate authors cannot review their novelty decision")
    if any(
        review.evidence_package_sha256 != evidence_package.package_sha256
        or review.reviewed_at < evidence_package.assembled_at
        for review in reviews
    ):
        raise ValueError("independent novelty review is bound to another package/time")


def _review_blockers(
    *,
    evidence_package: NoveltyEvidencePackage,
    reviews: tuple[CalibratedNoveltyReview, ...],
) -> tuple[str, ...]:
    blockers = list(evidence_package.base_blockers)
    if evidence_package.derived_classification is (
        NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
    ):
        return tuple(blockers)
    policy = evidence_package.novelty_policy
    confirmed = [
        review
        for review in reviews
        if review.verdict is NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE
    ]
    if len(confirmed) < policy.minimum_independent_reviewers:
        blockers.append("independent_review_floor_not_met")
    roles = {review.reviewer_role for review in confirmed}
    for role in sorted(_REQUIRED_REVIEW_ROLES - roles):
        blockers.append(f"missing_confirmed_review_role:{role}")
    blockers.extend(
        f"review_request_more_search:{review.review_id}"
        for review in reviews
        if review.verdict is NoveltyReviewVerdict.REQUEST_MORE_SEARCH
    )
    blockers.extend(
        f"review_rejected_classification:{review.review_id}"
        for review in reviews
        if review.verdict is NoveltyReviewVerdict.REJECT_CLASSIFICATION
    )
    return tuple(blockers)


def _legacy_evidence_package_sha256(
    *,
    policy: NoveltyPolicy,
    coverage: CalibratedNoveltyCoverageAssessment,
    candidate_claim_sha256: str,
    relations: tuple[PriorArtRelation, ...],
) -> str:
    return content_sha256(
        {
            "policy_sha256": policy.policy_sha256,
            "corpus_snapshot_sha256": coverage.ingestion_bundle.corpus.snapshot_sha256,
            "search_session_sha256": (coverage.search_assessment.aggregate_session.session_sha256),
            "coverage_report_sha256": coverage.coverage_sha256,
            "claim_graph_sha256": coverage.claim_graph_bundle.graph.graph_sha256,
            "candidate_claim_sha256s": (candidate_claim_sha256,),
            "nearest_prior_art_sha256s": [relation.relation_sha256 for relation in relations],
            "temporal_cutoff": coverage.ingestion_bundle.corpus.cutoff_time.isoformat(),
        }
    )


def _derive_novelty_assessment(
    *,
    assessment_id: str,
    coverage: CalibratedNoveltyCoverageAssessment,
    authorship_manifest: CandidateAuthorshipManifest,
    evidence_package: NoveltyEvidencePackage,
    reviews: tuple[CalibratedNoveltyReview, ...],
    assessed_at: AwareDatetime,
) -> NoveltyAssessment:
    relations = _candidate_relations(
        coverage=coverage,
        candidate_claim_sha256=evidence_package.candidate_claim_sha256,
    )
    decisive = _decisive_relation(relations)
    differences = (
        ()
        if evidence_package.derived_classification
        is NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
        else decisive.differences
    )
    blockers = _review_blockers(
        evidence_package=evidence_package,
        reviews=reviews,
    )
    legacy_package = _legacy_evidence_package_sha256(
        policy=evidence_package.novelty_policy,
        coverage=coverage,
        candidate_claim_sha256=evidence_package.candidate_claim_sha256,
        relations=relations,
    )
    legacy_reviews = tuple(
        NoveltyReview(
            reviewer_principal_sha256=review.reviewer_principal_sha256,
            reviewer_role=review.reviewer_role,
            evidence_package_sha256=legacy_package,
            verdict=review.verdict,
            rationale_sha256=review.rationale_sha256,
            reviewed_at=review.reviewed_at,
        )
        for review in reviews
    )
    classification = evidence_package.derived_classification
    if coverage.decision_verdict is CoverageVerdict.INSUFFICIENT:
        eligible = False
        ceiling = NoveltyClaimCeiling.SPECULATIVE
    else:
        blocking_prior = any(relation.blocks_strong_novelty for relation in relations)
        confirmed = [
            review
            for review in legacy_reviews
            if review.verdict is NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE
        ]
        eligible = (
            classification in _STRONG_CLASSES
            and len(relations) >= evidence_package.novelty_policy.minimum_nearest_prior_art
            and bool(differences)
            and not blockers
            and not blocking_prior
            and len(confirmed) >= evidence_package.novelty_policy.minimum_independent_reviewers
            and len(confirmed) == len(legacy_reviews)
        )
        ceiling = (
            NoveltyClaimCeiling.MODERATE
            if eligible
            else NoveltyClaimCeiling.NONE
            if classification
            in {
                NoveltyClassification.KNOWN_EQUIVALENT,
                NoveltyClassification.KNOWN_SPECIAL_CASE,
            }
            else NoveltyClaimCeiling.WEAK
            if classification
            in {
                NoveltyClassification.INCREMENTAL_EXTENSION,
                NoveltyClassification.CONTRADICTORY_TO_PRIOR,
            }
            else NoveltyClaimCeiling.SPECULATIVE
        )
    return NoveltyAssessment(
        assessment_id=assessment_id,
        policy=evidence_package.novelty_policy,
        corpus_snapshot_sha256=coverage.ingestion_bundle.corpus.snapshot_sha256,
        search_session_sha256=coverage.search_assessment.aggregate_session.session_sha256,
        coverage_report_sha256=coverage.coverage_sha256,
        coverage_verdict=coverage.decision_verdict,
        claim_graph_sha256=coverage.claim_graph_bundle.graph.graph_sha256,
        candidate_claim_sha256s=(evidence_package.candidate_claim_sha256,),
        candidate_author_principal_sha256s=(authorship_manifest.author_principal_sha256s),
        nearest_prior_art=relations,
        classification=classification,
        exact_differences=differences,
        unresolved_blockers=blockers,
        temporal_cutoff=evidence_package.temporal_cutoff,
        temporal_limitations=evidence_package.temporal_limitations,
        model_prior_limitations=evidence_package.model_prior_limitations,
        contamination_disclosure=evidence_package.contamination_disclosure,
        evidence_package_sha256=legacy_package,
        reviews=legacy_reviews,
        strong_novelty_eligible=eligible,
        claim_strength_ceiling=ceiling,
        assessed_at=assessed_at,
    )


def build_reviewed_novelty_decision(
    *,
    decision_id: str,
    assessment_id: str,
    coverage: CalibratedNoveltyCoverageAssessment,
    calibration_receipt_key: bytes,
    authorship_manifest: CandidateAuthorshipManifest,
    evidence_package: NoveltyEvidencePackage,
    independent_reviews: tuple[CalibratedNoveltyReview, ...],
    generated_at: AwareDatetime,
) -> ReviewedNoveltyDecision:
    _verify_calibration_receipts(
        coverage=coverage,
        calibration_receipt_key=calibration_receipt_key,
    )
    _validate_reviews(
        evidence_package=evidence_package,
        authorship_manifest=authorship_manifest,
        reviews=independent_reviews,
    )
    assessment = _derive_novelty_assessment(
        assessment_id=assessment_id,
        coverage=coverage,
        authorship_manifest=authorship_manifest,
        evidence_package=evidence_package,
        reviews=independent_reviews,
        assessed_at=generated_at,
    )
    return ReviewedNoveltyDecision(
        decision_id=decision_id,
        coverage=coverage,
        authorship_manifest=authorship_manifest,
        evidence_package=evidence_package,
        independent_reviews=independent_reviews,
        assessment=assessment,
        generated_at=generated_at,
    )


def _confirmed_review_roles(decision: ReviewedNoveltyDecision) -> set[str]:
    return {
        review.reviewer_role
        for review in decision.independent_reviews
        if review.verdict is NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE
    }


def _derive_direction_gate(
    decision: ReviewedNoveltyDecision,
) -> tuple[ResearchDirectionDisposition, bool, tuple[str, ...]]:
    assessment = decision.assessment
    if assessment.coverage_verdict is CoverageVerdict.INSUFFICIENT:
        return (
            ResearchDirectionDisposition.BLOCKED_COVERAGE,
            False,
            ("coverage_or_calibration_insufficient", *assessment.unresolved_blockers),
        )
    confirmed = [
        review
        for review in decision.independent_reviews
        if review.verdict is NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE
    ]
    review_ready = (
        len(confirmed) >= assessment.policy.minimum_independent_reviewers
        and len(confirmed) == len(decision.independent_reviews)
        and _REQUIRED_REVIEW_ROLES.issubset(_confirmed_review_roles(decision))
    )
    if not review_ready or assessment.unresolved_blockers:
        return (
            ResearchDirectionDisposition.RESEARCH_MORE,
            False,
            ("independent_review_or_search_unresolved", *assessment.unresolved_blockers),
        )
    if assessment.classification in {
        NoveltyClassification.KNOWN_EQUIVALENT,
        NoveltyClassification.KNOWN_SPECIAL_CASE,
    }:
        return (
            ResearchDirectionDisposition.REJECT_KNOWN,
            False,
            ("reviewed_prior_art_blocks_novelty",),
        )
    if assessment.classification in _STRONG_CLASSES:
        if assessment.strong_novelty_eligible:
            return (
                ResearchDirectionDisposition.ADVANCE_STRONG,
                True,
                ("calibrated_strong_novelty_confirmed",),
            )
        return (
            ResearchDirectionDisposition.RESEARCH_MORE,
            False,
            ("strong_novelty_prerequisites_not_met",),
        )
    return (
        ResearchDirectionDisposition.ADVANCE_BOUNDED,
        True,
        ("reviewed_bounded_non_strong_direction",),
    )


def build_research_direction_gate(
    *,
    gate_id: str,
    novelty_decision: ReviewedNoveltyDecision,
    calibration_receipt_key: bytes,
    decided_at: AwareDatetime,
) -> ResearchDirectionGate:
    _verify_calibration_receipts(
        coverage=novelty_decision.coverage,
        calibration_receipt_key=calibration_receipt_key,
    )
    disposition, authorized, codes = _derive_direction_gate(novelty_decision)
    return ResearchDirectionGate(
        gate_id=gate_id,
        novelty_decision=novelty_decision,
        disposition=disposition,
        experiment_authorized=authorized,
        maximum_novelty_claim=novelty_decision.assessment.claim_strength_ceiling,
        rationale_codes=codes,
        decided_at=decided_at,
    )


def commit_research_direction_gate(
    *,
    archive: ContentAddressedResponseArchive,
    gate: ResearchDirectionGate,
) -> CommittedResearchDirectionGate:
    ledger = archive.store_ledger(
        value=gate,
        object_sha256=gate.gate_sha256,
        archived_at=gate.decided_at,
    )
    return CommittedResearchDirectionGate(gate=gate, ledger=ledger)


def load_research_direction_gate(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
    calibration_receipt_key: bytes,
) -> ResearchDirectionGate:
    payload = archive.read_ledger(ledger)
    gate = ResearchDirectionGate.model_validate_json(payload)
    if canonical_json_bytes(gate) != payload:
        raise ValueError("archived research direction gate is not canonical JSON")
    if gate.gate_sha256 != ledger.object_sha256:
        raise ValueError("archived research direction gate changed object identity")
    _verify_calibration_receipts(
        coverage=gate.novelty_decision.coverage,
        calibration_receipt_key=calibration_receipt_key,
    )
    return gate


__all__ = [
    "CalibratedNoveltyReview",
    "CandidateAuthorshipManifest",
    "CommittedResearchDirectionGate",
    "NoveltyEvidencePackage",
    "ResearchDirectionDisposition",
    "ResearchDirectionGate",
    "ReviewedNoveltyDecision",
    "build_calibrated_novelty_policy",
    "build_candidate_authorship_manifest",
    "build_novelty_evidence_package",
    "build_research_direction_gate",
    "build_reviewed_novelty_decision",
    "commit_research_direction_gate",
    "load_research_direction_gate",
]
