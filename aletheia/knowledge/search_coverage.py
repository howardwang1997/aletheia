"""Convert committed F8-S2 search/citation evidence into fail-closed coverage reports."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.citation_traversal import CitationTraversalCampaign
from aletheia.knowledge.response_archive import (
    ArchivedSearchLedger,
    ContentAddressedResponseArchive,
    ResponseArchiveCorruption,
)
from aletheia.knowledge.schemas import (
    CoverageDirection,
    CoveragePolicy,
    CoverageReport,
    CoverageRequirement,
    CoverageSignalName,
    CoverageSignalResult,
    CoverageSignalStatus,
    CoverageVerdict,
    KnowledgeModel,
    QueryOutcome,
    SearchSession,
    SearchStoppingReason,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_DERIVED_SIGNALS = {
    CoverageSignalName.QUERY_FAMILY_COVERAGE,
    CoverageSignalName.SOURCE_DIVERSITY,
    CoverageSignalName.CITATION_FRONTIER_SATURATION,
    CoverageSignalName.UNCOVERED_SOURCE_FRACTION,
}
_EXTERNAL_SIGNALS = set(CoverageSignalName) - _DERIVED_SIGNALS


class CoverageObservation(KnowledgeModel):
    """An independently measured signal not derivable from the search ledger itself."""

    schema_version: Literal[1] = 1
    signal: CoverageSignalName
    observed: float
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _observation_is_external_and_attributable(self) -> "CoverageObservation":
        if self.signal not in _EXTERNAL_SIGNALS:
            raise ValueError("search-derived coverage signals cannot be supplied by callers")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("coverage numerator and denominator must appear together")
        if self.numerator is not None and self.numerator > self.denominator:
            raise ValueError("coverage numerator cannot exceed denominator")
        return self


class F8SearchCoverageAssessment(KnowledgeModel):
    schema_version: Literal[1] = 1
    campaign: CitationTraversalCampaign
    aggregate_session: SearchSession
    external_observations: tuple[CoverageObservation, ...]
    report: CoverageReport
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _assessment_binds_all_evidence(self) -> "F8SearchCoverageAssessment":
        if self.report.search_session_sha256 != self.aggregate_session.session_sha256:
            raise ValueError("coverage report is bound to a different aggregate search session")
        if (
            self.report.corpus_snapshot_sha256
            != self.campaign.executions[0].plan.protocol.corpus_snapshot_sha256
        ):
            raise ValueError("coverage report is bound to a different corpus snapshot")
        signals = [item.signal for item in self.external_observations]
        if len(signals) != len(set(signals)) or set(signals) != _EXTERNAL_SIGNALS:
            raise ValueError("coverage assessment must include every external signal exactly once")
        return self

    @property
    def assessment_sha256(self) -> str:
        return content_sha256(self)


class CommittedF8SearchCoverageAssessment(KnowledgeModel):
    schema_version: Literal[1] = 1
    assessment: F8SearchCoverageAssessment
    ledger: ArchivedSearchLedger

    @model_validator(mode="after")
    def _ledger_commits_the_assessment(self) -> "CommittedF8SearchCoverageAssessment":
        payload = canonical_json_bytes(self.assessment)
        if (
            self.ledger.object_sha256 != self.assessment.assessment_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("coverage ledger does not commit the exact assessment")
        return self


def build_default_f8s2_coverage_policy(*, frozen_at: AwareDatetime) -> CoveragePolicy:
    thresholds = {
        CoverageSignalName.KNOWN_ANSWER_RECALL: 0.80,
        CoverageSignalName.SEED_REFERENCE_RECOVERY: 0.80,
        CoverageSignalName.QUERY_FAMILY_COVERAGE: 1.0,
        CoverageSignalName.SOURCE_DIVERSITY: 1.0,
        CoverageSignalName.CITATION_FRONTIER_SATURATION: 1.0,
        CoverageSignalName.FULL_TEXT_AVAILABILITY: 0.50,
        CoverageSignalName.SOURCE_SPAN_VERIFICATION: 0.95,
        CoverageSignalName.CORRECTION_RETRACTION_CHECK: 1.0,
        CoverageSignalName.PERTURBATION_STABILITY: 0.90,
        CoverageSignalName.UNCOVERED_SOURCE_FRACTION: 0.0,
    }
    requirements = tuple(
        CoverageRequirement(
            signal=signal,
            direction=(
                CoverageDirection.MAXIMUM
                if signal is CoverageSignalName.UNCOVERED_SOURCE_FRACTION
                else CoverageDirection.MINIMUM
            ),
            threshold=thresholds[signal],
            hard=True,
            rationale=f"F8-S2 fail-closed threshold for {signal.value}.",
        )
        for signal in CoverageSignalName
    )
    return CoveragePolicy(
        policy_id="f8s2-search-coverage-policy-v1",
        requirements=requirements,
        minimum_nearest_prior_art=3,
        minimum_independent_reviewers=2,
        frozen_at=frozen_at,
    )


def _validate_fail_closed_policy(policy: CoveragePolicy) -> None:
    requirements = {item.signal: item for item in policy.requirements}
    exact = {
        CoverageSignalName.QUERY_FAMILY_COVERAGE: (
            CoverageDirection.MINIMUM,
            1.0,
        ),
        CoverageSignalName.SOURCE_DIVERSITY: (CoverageDirection.MINIMUM, 1.0),
        CoverageSignalName.CITATION_FRONTIER_SATURATION: (
            CoverageDirection.MINIMUM,
            1.0,
        ),
        CoverageSignalName.UNCOVERED_SOURCE_FRACTION: (
            CoverageDirection.MAXIMUM,
            0.0,
        ),
    }
    for signal, (direction, threshold) in exact.items():
        requirement = requirements[signal]
        if (
            not requirement.hard
            or requirement.direction is not direction
            or requirement.threshold != threshold
        ):
            raise ValueError(
                f"F8-S2 requires hard fail-closed threshold {threshold} for {signal.value}"
            )


def build_campaign_search_session(campaign: CitationTraversalCampaign) -> SearchSession:
    queries = tuple(
        query
        for execution in campaign.executions
        for query in execution.session.queries
    )
    response_hashes: list[str] = []
    for query in queries:
        if query.response_sha256 is not None and query.response_sha256 not in response_hashes:
            response_hashes.append(query.response_sha256)
    protocol = campaign.executions[0].plan.protocol
    return SearchSession(
        session_id=f"f8s2-campaign-{campaign.campaign_sha256[:24]}",
        protocol_sha256=protocol.protocol_sha256,
        corpus_snapshot_sha256=protocol.corpus_snapshot_sha256,
        queries=queries,
        started_at=campaign.started_at,
        ended_at=campaign.ended_at,
        stopping_reason=campaign.stopping_reason,
        stopping_evidence_sha256=content_sha256(
            {
                "citation_campaign_sha256": campaign.campaign_sha256,
                "execution_sha256s": [
                    execution.execution_sha256 for execution in campaign.executions
                ],
                "replay_audit_sha256s": [
                    audit.audit_sha256 for audit in campaign.replay_audits
                ],
            }
        ),
        replay_cache_sha256s=tuple(response_hashes),
    )


def _logical_query_closure(execution) -> dict[str, bool]:
    grouped: dict[str, list] = defaultdict(list)
    for receipt in execution.page_receipts:
        grouped[receipt.logical_query_sha256].append(receipt)
    return {
        logical_sha256: (
            receipts[-1].outcome is QueryOutcome.SUCCESS
            and receipts[-1].terminal is True
            and all(receipt.outcome is QueryOutcome.SUCCESS for receipt in receipts)
        )
        for logical_sha256, receipts in grouped.items()
    }


def _derived_results(
    *, campaign: CitationTraversalCampaign, policy: CoveragePolicy
) -> dict[CoverageSignalName, CoverageSignalResult]:
    initial = campaign.executions[0]
    closure = _logical_query_closure(initial)
    required_families = tuple(initial.plan.protocol.required_query_families)
    family_passes = 0
    for family in required_families:
        family_queries = [
            query for query in initial.plan.queries if query.family is family
        ]
        if family_queries and all(
            closure.get(query.logical_query_sha256, False) for query in family_queries
        ):
            family_passes += 1
    source_passes = 0
    for source_id in initial.plan.protocol.planned_source_ids:
        source_queries = [
            query for query in initial.plan.queries if query.source_id == source_id
        ]
        if source_queries and all(
            closure.get(query.logical_query_sha256, False) for query in source_queries
        ):
            source_passes += 1
    source_total = len(initial.plan.protocol.planned_source_ids)
    family_total = len(required_families)
    citation_complete = campaign.coverage_disposition == "eligible" and campaign.stopping_reason in {
        SearchStoppingReason.SATURATION,
        SearchStoppingReason.SOURCE_EXHAUSTED,
    }
    values = {
        CoverageSignalName.QUERY_FAMILY_COVERAGE: (
            family_passes / family_total,
            family_passes,
            family_total,
            content_sha256(
                {
                    "initial_execution_sha256": initial.execution_sha256,
                    "family_closure": {
                        family.value: all(
                            closure.get(query.logical_query_sha256, False)
                            for query in initial.plan.queries
                            if query.family is family
                        )
                        for family in required_families
                    },
                }
            ),
            "Fraction of required query families complete on every planned capable source.",
        ),
        CoverageSignalName.SOURCE_DIVERSITY: (
            source_passes / source_total,
            source_passes,
            source_total,
            content_sha256(
                {
                    "initial_execution_sha256": initial.execution_sha256,
                    "complete_source_ids": [
                        source_id
                        for source_id in initial.plan.protocol.planned_source_ids
                        if all(
                            closure.get(query.logical_query_sha256, False)
                            for query in initial.plan.queries
                            if query.source_id == source_id
                        )
                    ],
                }
            ),
            "Fraction of planned sources whose complete initial query set terminated successfully.",
        ),
        CoverageSignalName.CITATION_FRONTIER_SATURATION: (
            1.0 if citation_complete else 0.0,
            1 if citation_complete else 0,
            1,
            campaign.campaign_sha256,
            "One only when replayed two-direction traversal reached saturation or source exhaustion.",
        ),
        CoverageSignalName.UNCOVERED_SOURCE_FRACTION: (
            (source_total - source_passes) / source_total,
            source_total - source_passes,
            source_total,
            content_sha256(
                {
                    "initial_execution_sha256": initial.execution_sha256,
                    "uncovered_source_count": source_total - source_passes,
                }
            ),
            "Fraction of planned sources with any incomplete initial logical query.",
        ),
    }
    requirements = {item.signal: item for item in policy.requirements}
    results: dict[CoverageSignalName, CoverageSignalResult] = {}
    for signal, (observed, numerator, denominator, evidence, detail) in values.items():
        requirement = requirements[signal]
        passed = (
            observed >= requirement.threshold
            if requirement.direction is CoverageDirection.MINIMUM
            else observed <= requirement.threshold
        )
        results[signal] = CoverageSignalResult(
            signal=signal,
            observed=observed,
            numerator=numerator,
            denominator=denominator,
            status=(CoverageSignalStatus.PASS if passed else CoverageSignalStatus.FAIL),
            evidence_sha256=evidence,
            detail=detail,
        )
    return results


def build_f8_search_coverage_assessment(
    *,
    report_id: str,
    policy: CoveragePolicy,
    campaign: CitationTraversalCampaign,
    external_observations: tuple[CoverageObservation, ...],
    generated_at: AwareDatetime,
) -> F8SearchCoverageAssessment:
    _validate_fail_closed_policy(policy)
    external_by_signal = {item.signal: item for item in external_observations}
    if len(external_by_signal) != len(external_observations) or set(
        external_by_signal
    ) != _EXTERNAL_SIGNALS:
        raise ValueError("every external coverage signal must be supplied exactly once")
    requirements = {item.signal: item for item in policy.requirements}
    results = _derived_results(campaign=campaign, policy=policy)
    for signal, observation in external_by_signal.items():
        requirement = requirements[signal]
        passed = (
            observation.observed >= requirement.threshold
            if requirement.direction is CoverageDirection.MINIMUM
            else observation.observed <= requirement.threshold
        )
        results[signal] = CoverageSignalResult(
            signal=signal,
            observed=observation.observed,
            numerator=observation.numerator,
            denominator=observation.denominator,
            status=(CoverageSignalStatus.PASS if passed else CoverageSignalStatus.FAIL),
            evidence_sha256=observation.evidence_sha256,
            detail=observation.detail,
        )
    ordered_results = tuple(results[signal] for signal in CoverageSignalName)
    hard_failures = tuple(
        sorted(
            (
                requirement.signal
                for requirement in policy.requirements
                if requirement.hard
                and results[requirement.signal].status is not CoverageSignalStatus.PASS
            ),
            key=lambda signal: signal.value,
        )
    )
    aggregate_session = build_campaign_search_session(campaign)
    report = CoverageReport(
        report_id=report_id,
        policy=policy,
        corpus_snapshot_sha256=aggregate_session.corpus_snapshot_sha256,
        search_session_sha256=aggregate_session.session_sha256,
        signals=ordered_results,
        verdict=(
            CoverageVerdict.INSUFFICIENT
            if hard_failures
            else CoverageVerdict.SUFFICIENT
        ),
        hard_failure_signals=hard_failures,
        generated_at=generated_at,
    )
    return F8SearchCoverageAssessment(
        campaign=campaign,
        aggregate_session=aggregate_session,
        external_observations=external_observations,
        report=report,
    )


def commit_f8_search_coverage_assessment(
    *,
    archive: ContentAddressedResponseArchive,
    assessment: F8SearchCoverageAssessment,
    archived_at: AwareDatetime,
) -> CommittedF8SearchCoverageAssessment:
    ledger = archive.store_ledger(
        value=assessment,
        object_sha256=assessment.assessment_sha256,
        archived_at=archived_at,
    )
    return CommittedF8SearchCoverageAssessment(assessment=assessment, ledger=ledger)


def load_f8_search_coverage_assessment(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedSearchLedger
) -> F8SearchCoverageAssessment:
    payload = archive.read_ledger(ledger)
    assessment = F8SearchCoverageAssessment.model_validate_json(payload)
    if assessment.assessment_sha256 != ledger.object_sha256:
        raise ResponseArchiveCorruption("coverage ledger object identity changed")
    if canonical_json_bytes(assessment) != payload:
        raise ResponseArchiveCorruption("coverage ledger is not canonical assessment JSON")
    return assessment


__all__ = [
    "CoverageObservation",
    "CommittedF8SearchCoverageAssessment",
    "F8SearchCoverageAssessment",
    "build_campaign_search_session",
    "build_default_f8s2_coverage_policy",
    "build_f8_search_coverage_assessment",
    "commit_f8_search_coverage_assessment",
    "load_f8_search_coverage_assessment",
]
