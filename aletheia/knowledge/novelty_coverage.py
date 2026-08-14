"""Artifact-derived F8-S5 coverage gated by evaluator-owned novelty calibration."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.claim_extraction import ExtractedAtomicClaimGraphBundle
from aletheia.knowledge.citation_traversal import CitationTraversalCampaign
from aletheia.knowledge.ingestion import CorpusIngestionBundle
from aletheia.knowledge.novelty_calibration import (
    NoveltyCalibrationReport,
    NoveltyCalibrationSplit,
    NoveltyCalibrationVerdict,
)
from aletheia.knowledge.prior_art_matching import PriorArtMatchingResolution
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    ClaimOrigin,
    ContradictionCorrectionReport,
    CoverageDirection,
    CoveragePolicy,
    CoverageReport,
    CoverageRequirement,
    CoverageSignalName,
    CoverageVerdict,
    KnowledgeModel,
    TextAvailability,
)
from aletheia.knowledge.search_coverage import (
    CoverageObservation,
    F8SearchCoverageAssessment,
    build_campaign_search_session,
    build_f8_search_coverage_assessment,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_SEARCH_DERIVED_SIGNALS = {
    CoverageSignalName.QUERY_FAMILY_COVERAGE,
    CoverageSignalName.SOURCE_DIVERSITY,
    CoverageSignalName.CITATION_FRONTIER_SATURATION,
    CoverageSignalName.UNCOVERED_SOURCE_FRACTION,
}
_EXTERNAL_SIGNAL_ORDER = tuple(
    signal for signal in CoverageSignalName if signal not in _SEARCH_DERIVED_SIGNALS
)


class CalibratedNoveltyCoverageAssessment(KnowledgeModel):
    """Self-contained evidence closure for a live novelty coverage decision."""

    schema_version: Literal[1] = 1
    assessment_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    calibration_report: NoveltyCalibrationReport
    ingestion_bundle: CorpusIngestionBundle
    claim_graph_bundle: ExtractedAtomicClaimGraphBundle
    prior_art_resolution: PriorArtMatchingResolution
    correction_report: ContradictionCorrectionReport
    search_assessment: F8SearchCoverageAssessment
    decision_verdict: CoverageVerdict
    decision_blockers: tuple[str, ...]
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _assessment_is_artifact_derived_and_fail_closed(
        self,
    ) -> "CalibratedNoveltyCoverageAssessment":
        _validate_artifact_bindings(
            calibration_report=self.calibration_report,
            ingestion_bundle=self.ingestion_bundle,
            claim_graph_bundle=self.claim_graph_bundle,
            prior_art_resolution=self.prior_art_resolution,
            correction_report=self.correction_report,
            campaign=self.search_assessment.campaign,
            policy=self.search_assessment.report.policy,
        )
        expected_policy = build_calibrated_f8s5_coverage_policy(
            calibration_report=self.calibration_report,
            frozen_at=self.search_assessment.report.policy.frozen_at,
        )
        if self.search_assessment.report.policy != expected_policy:
            raise ValueError("live coverage uses a policy not derived from calibration")
        expected_observations = _derive_external_observations(
            calibration_report=self.calibration_report,
            ingestion_bundle=self.ingestion_bundle,
            claim_graph_bundle=self.claim_graph_bundle,
            prior_art_resolution=self.prior_art_resolution,
            correction_report=self.correction_report,
            campaign=self.search_assessment.campaign,
        )
        if self.search_assessment.external_observations != expected_observations:
            raise ValueError("live coverage observations are not derived from exact artifacts")
        expected_blockers, expected_verdict = _derive_decision(
            calibration_report=self.calibration_report,
            prior_art_resolution=self.prior_art_resolution,
            coverage_report=self.search_assessment.report,
        )
        if (
            self.decision_blockers != expected_blockers
            or self.decision_verdict is not expected_verdict
        ):
            raise ValueError("calibrated coverage decision is not fail-closed and derived")
        if self.search_assessment.report.generated_at != self.generated_at:
            raise ValueError("calibrated coverage and search report times must match")
        if self.generated_at < max(
            self.calibration_report.generated_at,
            self.claim_graph_bundle.built_at,
            self.prior_art_resolution.resolved_at,
            self.correction_report.generated_at,
            self.search_assessment.campaign.ended_at,
        ):
            raise ValueError("calibrated coverage predates one of its evidence artifacts")
        return self

    @property
    def coverage_sha256(self) -> str:
        return content_sha256(self)


class CommittedCalibratedNoveltyCoverage(KnowledgeModel):
    schema_version: Literal[1] = 1
    assessment: CalibratedNoveltyCoverageAssessment
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_assessment(self) -> "CommittedCalibratedNoveltyCoverage":
        payload = canonical_json_bytes(self.assessment)
        if (
            self.ledger.object_sha256 != self.assessment.coverage_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("calibrated coverage ledger does not commit its assessment")
        return self


def build_calibrated_f8s5_coverage_policy(
    *,
    calibration_report: NoveltyCalibrationReport,
    frozen_at: AwareDatetime,
) -> CoveragePolicy:
    if frozen_at < calibration_report.generated_at:
        raise ValueError("live coverage policy cannot freeze before calibration completes")
    calibration = calibration_report.suite.policy
    thresholds = {
        CoverageSignalName.KNOWN_ANSWER_RECALL: (
            calibration.minimum_known_answer_recall_lower_bound
        ),
        CoverageSignalName.SEED_REFERENCE_RECOVERY: (
            calibration.minimum_seed_reference_recovery_lower_bound
        ),
        CoverageSignalName.QUERY_FAMILY_COVERAGE: 1.0,
        CoverageSignalName.SOURCE_DIVERSITY: 1.0,
        CoverageSignalName.CITATION_FRONTIER_SATURATION: 1.0,
        CoverageSignalName.FULL_TEXT_AVAILABILITY: 0.50,
        CoverageSignalName.SOURCE_SPAN_VERIFICATION: 0.95,
        CoverageSignalName.CORRECTION_RETRACTION_CHECK: 1.0,
        CoverageSignalName.PERTURBATION_STABILITY: (
            calibration.minimum_perturbation_stability_lower_bound
        ),
        CoverageSignalName.UNCOVERED_SOURCE_FRACTION: 0.0,
    }
    return CoveragePolicy(
        policy_id=f"f8s5-calibrated-{calibration_report.report_sha256[:20]}",
        requirements=tuple(
            CoverageRequirement(
                signal=signal,
                direction=(
                    CoverageDirection.MAXIMUM
                    if signal is CoverageSignalName.UNCOVERED_SOURCE_FRACTION
                    else CoverageDirection.MINIMUM
                ),
                threshold=thresholds[signal],
                hard=True,
                rationale=(
                    f"F8-S5 calibrated, artifact-derived fail-closed threshold for {signal.value}."
                ),
            )
            for signal in CoverageSignalName
        ),
        minimum_nearest_prior_art=3,
        minimum_independent_reviewers=2,
        frozen_at=frozen_at,
    )


def _temporal_metrics(calibration_report: NoveltyCalibrationReport):
    return next(
        metric
        for metric in calibration_report.metrics
        if metric.split is NoveltyCalibrationSplit.TEMPORAL_HOLDOUT
    )


def _prior_paper_sha256s(
    *,
    claim_graph_bundle: ExtractedAtomicClaimGraphBundle,
    prior_art_resolution: PriorArtMatchingResolution,
) -> tuple[str, ...]:
    prior_claims = {
        claim.claim_sha256: claim
        for claim in claim_graph_bundle.graph.claims
        if claim.origin is ClaimOrigin.PRIOR_ART
    }
    try:
        papers = {
            prior_claims[item.relation.prior_claim_sha256].source_paper_snapshot_sha256
            for item in prior_art_resolution.accepted
        }
    except KeyError as exc:
        raise ValueError(
            "prior-art resolution references a claim outside the reviewed graph"
        ) from exc
    if None in papers or not papers:
        raise ValueError("resolved prior art requires attributable source papers")
    return tuple(sorted(paper for paper in papers if paper is not None))


def _derive_external_observations(
    *,
    calibration_report: NoveltyCalibrationReport,
    ingestion_bundle: CorpusIngestionBundle,
    claim_graph_bundle: ExtractedAtomicClaimGraphBundle,
    prior_art_resolution: PriorArtMatchingResolution,
    correction_report: ContradictionCorrectionReport,
    campaign: CitationTraversalCampaign,
) -> tuple[CoverageObservation, ...]:
    temporal = _temporal_metrics(calibration_report)
    aggregate_session = build_campaign_search_session(campaign)
    protocol = campaign.executions[0].plan.protocol
    search_hits = {
        hit.paper_snapshot_sha256 for query in aggregate_session.queries for hit in query.hits
    }
    seed_papers = set(protocol.seed_paper_snapshot_sha256s)
    recovered_seeds = seed_papers & search_hits

    prior_papers = _prior_paper_sha256s(
        claim_graph_bundle=claim_graph_bundle,
        prior_art_resolution=prior_art_resolution,
    )
    grants = {grant.paper_snapshot_sha256: grant for grant in ingestion_bundle.access_grants}
    full_text_papers = {
        paper_sha256
        for paper_sha256 in prior_papers
        if grants[paper_sha256].text_capability is TextAvailability.FULL_TEXT
    }

    required_spans = {
        span_sha256
        for item in prior_art_resolution.accepted
        for span_sha256 in item.relation.evidence_span_sha256s
    }
    corpus_spans = {span.span_sha256 for span in ingestion_bundle.corpus.spans}
    verified_spans = required_spans & corpus_spans

    candidate_claims = {
        target.candidate_claim_sha256 for target in prior_art_resolution.execution.protocol.targets
    }
    correction_complete = (
        correction_report.correction_retraction_check_complete
        and set(prior_papers).issubset(correction_report.checked_paper_snapshot_sha256s)
        and not (candidate_claims & set(correction_report.unresolved_claim_sha256s))
    )
    observations = {
        CoverageSignalName.KNOWN_ANSWER_RECALL: CoverageObservation(
            signal=CoverageSignalName.KNOWN_ANSWER_RECALL,
            observed=temporal.known_answer_recall.lower_bound,
            numerator=temporal.known_answer_recall.events,
            denominator=temporal.known_answer_recall.total,
            evidence_sha256=content_sha256(
                {
                    "calibration_report_sha256": calibration_report.report_sha256,
                    "split": NoveltyCalibrationSplit.TEMPORAL_HOLDOUT.value,
                    "metric": temporal.known_answer_recall.model_dump(mode="json"),
                }
            ),
            detail="One-sided 95% lower confidence bound on temporal known-answer recall.",
        ),
        CoverageSignalName.SEED_REFERENCE_RECOVERY: CoverageObservation(
            signal=CoverageSignalName.SEED_REFERENCE_RECOVERY,
            observed=len(recovered_seeds) / len(seed_papers),
            numerator=len(recovered_seeds),
            denominator=len(seed_papers),
            evidence_sha256=content_sha256(
                {
                    "search_session_sha256": aggregate_session.session_sha256,
                    "required_seed_paper_sha256s": sorted(seed_papers),
                    "recovered_seed_paper_sha256s": sorted(recovered_seeds),
                }
            ),
            detail="Exact frozen seed papers recovered anywhere in the replayed live campaign.",
        ),
        CoverageSignalName.FULL_TEXT_AVAILABILITY: CoverageObservation(
            signal=CoverageSignalName.FULL_TEXT_AVAILABILITY,
            observed=len(full_text_papers) / len(prior_papers),
            numerator=len(full_text_papers),
            denominator=len(prior_papers),
            evidence_sha256=content_sha256(
                {
                    "ingestion_bundle_sha256": ingestion_bundle.bundle_sha256,
                    "required_prior_paper_sha256s": prior_papers,
                    "full_text_paper_sha256s": sorted(full_text_papers),
                }
            ),
            detail="Licensed full-text grants among distinct resolved nearest-prior papers.",
        ),
        CoverageSignalName.SOURCE_SPAN_VERIFICATION: CoverageObservation(
            signal=CoverageSignalName.SOURCE_SPAN_VERIFICATION,
            observed=len(verified_spans) / len(required_spans),
            numerator=len(verified_spans),
            denominator=len(required_spans),
            evidence_sha256=content_sha256(
                {
                    "claim_graph_bundle_sha256": claim_graph_bundle.bundle_sha256,
                    "required_span_sha256s": sorted(required_spans),
                    "verified_corpus_span_sha256s": sorted(verified_spans),
                }
            ),
            detail="Resolved relation evidence spans present in the immutable ingested corpus.",
        ),
        CoverageSignalName.CORRECTION_RETRACTION_CHECK: CoverageObservation(
            signal=CoverageSignalName.CORRECTION_RETRACTION_CHECK,
            observed=1.0 if correction_complete else 0.0,
            numerator=1 if correction_complete else 0,
            denominator=1,
            evidence_sha256=content_sha256(
                {
                    "correction_report_sha256": correction_report.report_sha256,
                    "required_prior_paper_sha256s": prior_papers,
                    "candidate_claim_sha256s": sorted(candidate_claims),
                }
            ),
            detail="Complete correction/retraction check over every resolved prior paper.",
        ),
        CoverageSignalName.PERTURBATION_STABILITY: CoverageObservation(
            signal=CoverageSignalName.PERTURBATION_STABILITY,
            observed=temporal.perturbation_stability.lower_bound,
            numerator=temporal.perturbation_stability.events,
            denominator=temporal.perturbation_stability.total,
            evidence_sha256=content_sha256(
                {
                    "calibration_report_sha256": calibration_report.report_sha256,
                    "split": NoveltyCalibrationSplit.TEMPORAL_HOLDOUT.value,
                    "metric": temporal.perturbation_stability.model_dump(mode="json"),
                }
            ),
            detail="One-sided 95% lower confidence bound on temporal perturbation stability.",
        ),
    }
    return tuple(observations[signal] for signal in _EXTERNAL_SIGNAL_ORDER)


def _validate_artifact_bindings(
    *,
    calibration_report: NoveltyCalibrationReport,
    ingestion_bundle: CorpusIngestionBundle,
    claim_graph_bundle: ExtractedAtomicClaimGraphBundle,
    prior_art_resolution: PriorArtMatchingResolution,
    correction_report: ContradictionCorrectionReport,
    campaign: CitationTraversalCampaign,
    policy: CoveragePolicy,
) -> None:
    corpus = ingestion_bundle.corpus
    graph = claim_graph_bundle.graph
    matching_protocol = prior_art_resolution.execution.protocol
    search_protocol = campaign.executions[0].plan.protocol
    if (
        claim_graph_bundle.resolution.execution.protocol.ingestion_bundle_sha256
        != ingestion_bundle.bundle_sha256
        or graph.corpus_snapshot_sha256 != corpus.snapshot_sha256
        or matching_protocol.claim_graph_bundle_sha256 != claim_graph_bundle.bundle_sha256
        or matching_protocol.claim_graph_sha256 != graph.graph_sha256
        or matching_protocol.corpus_snapshot_sha256 != corpus.snapshot_sha256
    ):
        raise ValueError("coverage artifacts do not share the exact corpus/claim graph")
    if (
        correction_report.corpus_snapshot_sha256 != corpus.snapshot_sha256
        or correction_report.claim_graph_sha256 != graph.graph_sha256
    ):
        raise ValueError("correction report is bound to another corpus/claim graph")
    candidate_claims = tuple(target.candidate_claim_sha256 for target in matching_protocol.targets)
    if (
        search_protocol.corpus_snapshot_sha256 != corpus.snapshot_sha256
        or search_protocol.cutoff_time != corpus.cutoff_time
        or not set(candidate_claims).issubset(search_protocol.candidate_claim_sha256s)
    ):
        raise ValueError("search campaign is bound to another corpus/candidate/cutoff")
    if not prior_art_resolution.accepted:
        raise ValueError("artifact-derived coverage requires resolved nearest prior art")
    prior_papers = _prior_paper_sha256s(
        claim_graph_bundle=claim_graph_bundle,
        prior_art_resolution=prior_art_resolution,
    )
    corpus_papers = {paper.snapshot_sha256 for paper in corpus.papers}
    if not set(prior_papers).issubset(corpus_papers):
        raise ValueError("resolved prior art cites papers outside the ingested corpus")
    if policy.frozen_at < calibration_report.generated_at:
        raise ValueError("coverage policy predates its calibration report")
    if policy.frozen_at > campaign.started_at:
        raise ValueError("coverage thresholds must freeze before the live search starts")


def _derive_decision(
    *,
    calibration_report: NoveltyCalibrationReport,
    prior_art_resolution: PriorArtMatchingResolution,
    coverage_report: CoverageReport,
) -> tuple[tuple[str, ...], CoverageVerdict]:
    blockers: list[str] = []
    if calibration_report.verdict is not NoveltyCalibrationVerdict.PASS:
        blockers.append("global_calibration_failed")
    counts = {
        target.candidate_claim_sha256: 0
        for target in prior_art_resolution.execution.protocol.targets
    }
    for accepted in prior_art_resolution.accepted:
        counts[accepted.relation.candidate_claim_sha256] += 1
    for claim_sha256, count in counts.items():
        if count < coverage_report.policy.minimum_nearest_prior_art:
            blockers.append(f"nearest_prior_art_below_minimum:{claim_sha256}")
    blockers.extend(
        f"coverage_signal:{signal.value}" for signal in coverage_report.hard_failure_signals
    )
    exact = tuple(blockers)
    return exact, (CoverageVerdict.INSUFFICIENT if exact else CoverageVerdict.SUFFICIENT)


def build_calibrated_novelty_coverage_assessment(
    *,
    assessment_id: str,
    calibration_report: NoveltyCalibrationReport,
    calibration_receipt_key: bytes,
    ingestion_bundle: CorpusIngestionBundle,
    claim_graph_bundle: ExtractedAtomicClaimGraphBundle,
    prior_art_resolution: PriorArtMatchingResolution,
    correction_report: ContradictionCorrectionReport,
    campaign: CitationTraversalCampaign,
    policy_frozen_at: AwareDatetime,
    generated_at: AwareDatetime,
) -> CalibratedNoveltyCoverageAssessment:
    for receipt in calibration_report.trial_receipts:
        receipt.verify(
            key=calibration_receipt_key,
            expected_key_id=calibration_report.evaluator_manifest.receipt_key_id,
        )
    policy = build_calibrated_f8s5_coverage_policy(
        calibration_report=calibration_report,
        frozen_at=policy_frozen_at,
    )
    _validate_artifact_bindings(
        calibration_report=calibration_report,
        ingestion_bundle=ingestion_bundle,
        claim_graph_bundle=claim_graph_bundle,
        prior_art_resolution=prior_art_resolution,
        correction_report=correction_report,
        campaign=campaign,
        policy=policy,
    )
    observations = _derive_external_observations(
        calibration_report=calibration_report,
        ingestion_bundle=ingestion_bundle,
        claim_graph_bundle=claim_graph_bundle,
        prior_art_resolution=prior_art_resolution,
        correction_report=correction_report,
        campaign=campaign,
    )
    search_assessment = build_f8_search_coverage_assessment(
        report_id=f"{assessment_id}-search",
        policy=policy,
        campaign=campaign,
        external_observations=observations,
        generated_at=generated_at,
    )
    blockers, verdict = _derive_decision(
        calibration_report=calibration_report,
        prior_art_resolution=prior_art_resolution,
        coverage_report=search_assessment.report,
    )
    return CalibratedNoveltyCoverageAssessment(
        assessment_id=assessment_id,
        calibration_report=calibration_report,
        ingestion_bundle=ingestion_bundle,
        claim_graph_bundle=claim_graph_bundle,
        prior_art_resolution=prior_art_resolution,
        correction_report=correction_report,
        search_assessment=search_assessment,
        decision_verdict=verdict,
        decision_blockers=blockers,
        generated_at=generated_at,
    )


def commit_calibrated_novelty_coverage(
    *,
    archive: ContentAddressedResponseArchive,
    assessment: CalibratedNoveltyCoverageAssessment,
) -> CommittedCalibratedNoveltyCoverage:
    ledger = archive.store_ledger(
        value=assessment,
        object_sha256=assessment.coverage_sha256,
        archived_at=assessment.generated_at,
    )
    return CommittedCalibratedNoveltyCoverage(
        assessment=assessment,
        ledger=ledger,
    )


def load_calibrated_novelty_coverage(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
    calibration_receipt_key: bytes,
) -> CalibratedNoveltyCoverageAssessment:
    payload = archive.read_ledger(ledger)
    assessment = CalibratedNoveltyCoverageAssessment.model_validate_json(payload)
    if canonical_json_bytes(assessment) != payload:
        raise ValueError("archived calibrated coverage is not canonical JSON")
    if assessment.coverage_sha256 != ledger.object_sha256:
        raise ValueError("archived calibrated coverage changed object identity")
    for receipt in assessment.calibration_report.trial_receipts:
        receipt.verify(
            key=calibration_receipt_key,
            expected_key_id=(assessment.calibration_report.evaluator_manifest.receipt_key_id),
        )
    return assessment


__all__ = [
    "CalibratedNoveltyCoverageAssessment",
    "CommittedCalibratedNoveltyCoverage",
    "build_calibrated_f8s5_coverage_policy",
    "build_calibrated_novelty_coverage_assessment",
    "commit_calibrated_novelty_coverage",
    "load_calibrated_novelty_coverage",
]
