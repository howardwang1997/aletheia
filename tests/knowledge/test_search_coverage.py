from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s2_fixtures import (
    StepClock,
    build_adapters,
    build_citation_policy,
    build_search_plan,
    sha,
)
from .test_schema_spike import _time


_DERIVED = {
    k.CoverageSignalName.QUERY_FAMILY_COVERAGE,
    k.CoverageSignalName.SOURCE_DIVERSITY,
    k.CoverageSignalName.CITATION_FRONTIER_SATURATION,
    k.CoverageSignalName.UNCOVERED_SOURCE_FRACTION,
}


def _observations(*, override: dict[k.CoverageSignalName, float] | None = None):
    override = override or {}
    return tuple(
        k.CoverageObservation(
            signal=signal,
            observed=override.get(signal, 1.0),
            numerator=(0 if override.get(signal) == 0.0 else 1),
            denominator=1,
            evidence_sha256=sha(f"external-coverage:{signal.value}"),
            detail=f"Synthetic independent measurement for {signal.value}.",
        )
        for signal in k.CoverageSignalName
        if signal not in _DERIVED
    )


async def _campaign(
    tmp_path,
    *,
    fail_initial_query: bool = False,
    fail_derived_round: bool = False,
):
    citation_policy = build_citation_policy(consecutive_saturated_rounds=1)
    plan = build_search_plan(
        citation_traversal_policy_sha256=citation_policy.policy_sha256
    )
    adapters = build_adapters(plan)
    neighbor = sha("coverage-citation-neighbor")
    graph: dict[tuple[str, k.QueryFamily], tuple[str, ...]] = {}
    for seed in plan.protocol.seed_paper_snapshot_sha256s:
        graph[(seed, k.QueryFamily.CITATION_BACKWARD)] = (neighbor,)
        graph[(seed, k.QueryFamily.CITATION_FORWARD)] = ()
    graph[(neighbor, k.QueryFamily.CITATION_BACKWARD)] = ()
    graph[(neighbor, k.QueryFamily.CITATION_FORWARD)] = ()
    for adapter in adapters.values():
        adapter.citation_graph = graph
        if fail_derived_round:
            adapter.fail_citation_rounds.add(1)
    if fail_initial_query:
        target = next(
            query
            for query in plan.queries
            if query.family is k.QueryFamily.QUEST
        )
        adapters[target.source_id].fetch_errors[target.logical_query_id] = k.CircuitOpenError()
    executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    )
    initial = await executor.execute(plan=plan, execution_id="f8s2-coverage-initial")
    campaign = await k.run_citation_traversal(
        campaign_id="f8s2-coverage-campaign",
        policy=citation_policy,
        initial_execution=initial,
        executor=executor,
    )
    return campaign


@pytest.mark.asyncio
async def test_closed_campaign_builds_sufficient_aggregate_coverage(tmp_path) -> None:
    campaign = await _campaign(tmp_path)
    policy = k.build_default_f8s2_coverage_policy(
        frozen_at=_time("2024-12-29T00:00:00Z")
    )
    assessment = k.build_f8_search_coverage_assessment(
        report_id="f8s2-coverage-pass",
        policy=policy,
        campaign=campaign,
        external_observations=_observations(),
        generated_at=_time("2025-01-01T00:00:00Z"),
    )

    assert assessment.report.verdict is k.CoverageVerdict.SUFFICIENT
    assert not assessment.report.hard_failure_signals
    by_signal = {result.signal: result for result in assessment.report.signals}
    for signal in _DERIVED:
        assert by_signal[signal].status is k.CoverageSignalStatus.PASS
    assert len(assessment.aggregate_session.queries) == sum(
        len(execution.session.queries) for execution in campaign.executions
    )
    assert assessment == k.F8SearchCoverageAssessment.model_validate_json(
        assessment.model_dump_json()
    )
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    committed = k.commit_f8_search_coverage_assessment(
        archive=archive,
        assessment=assessment,
        archived_at=_time("2025-01-01T00:00:01Z"),
    )
    assert (
        k.load_f8_search_coverage_assessment(
            archive=archive, ledger=committed.ledger
        )
        == assessment
    )


@pytest.mark.asyncio
async def test_initial_query_failure_forces_four_search_coverage_failures(tmp_path) -> None:
    campaign = await _campaign(tmp_path, fail_initial_query=True)
    policy = k.build_default_f8s2_coverage_policy(
        frozen_at=_time("2024-12-29T00:00:00Z")
    )
    assessment = k.build_f8_search_coverage_assessment(
        report_id="f8s2-initial-failure",
        policy=policy,
        campaign=campaign,
        external_observations=_observations(),
        generated_at=_time("2025-01-01T00:00:00Z"),
    )
    assert assessment.report.verdict is k.CoverageVerdict.INSUFFICIENT
    assert set(assessment.report.hard_failure_signals) == _DERIVED


@pytest.mark.asyncio
async def test_derived_citation_failure_alone_blocks_saturation_signal(tmp_path) -> None:
    campaign = await _campaign(tmp_path, fail_derived_round=True)
    assessment = k.build_f8_search_coverage_assessment(
        report_id="f8s2-derived-failure",
        policy=k.build_default_f8s2_coverage_policy(
            frozen_at=_time("2024-12-29T00:00:00Z")
        ),
        campaign=campaign,
        external_observations=_observations(),
        generated_at=_time("2025-01-01T00:00:00Z"),
    )
    assert assessment.report.verdict is k.CoverageVerdict.INSUFFICIENT
    assert assessment.report.hard_failure_signals == (
        k.CoverageSignalName.CITATION_FRONTIER_SATURATION,
    )


@pytest.mark.asyncio
async def test_external_measurement_failure_remains_a_hard_gate(tmp_path) -> None:
    campaign = await _campaign(tmp_path)
    failed_signal = k.CoverageSignalName.KNOWN_ANSWER_RECALL
    assessment = k.build_f8_search_coverage_assessment(
        report_id="f8s2-external-failure",
        policy=k.build_default_f8s2_coverage_policy(
            frozen_at=_time("2024-12-29T00:00:00Z")
        ),
        campaign=campaign,
        external_observations=_observations(override={failed_signal: 0.0}),
        generated_at=_time("2025-01-01T00:00:00Z"),
    )
    assert assessment.report.hard_failure_signals == (failed_signal,)
    assert assessment.report.verdict is k.CoverageVerdict.INSUFFICIENT


@pytest.mark.asyncio
async def test_permissive_search_thresholds_are_rejected_not_exploited(tmp_path) -> None:
    campaign = await _campaign(tmp_path, fail_initial_query=True)
    policy = k.build_default_f8s2_coverage_policy(
        frozen_at=_time("2024-12-29T00:00:00Z")
    )
    requirements = tuple(
        k.CoverageRequirement.model_validate(
            {
                **requirement.model_dump(mode="python"),
                "threshold": (
                    0.5
                    if requirement.signal is k.CoverageSignalName.QUERY_FAMILY_COVERAGE
                    else requirement.threshold
                ),
            }
        )
        for requirement in policy.requirements
    )
    permissive = k.CoveragePolicy.model_validate(
        {**policy.model_dump(mode="python"), "requirements": requirements}
    )
    with pytest.raises(ValueError, match="hard fail-closed threshold 1.0"):
        k.build_f8_search_coverage_assessment(
            report_id="f8s2-permissive-policy",
            policy=permissive,
            campaign=campaign,
            external_observations=_observations(),
            generated_at=_time("2025-01-01T00:00:00Z"),
        )


def test_callers_cannot_override_search_derived_signals() -> None:
    with pytest.raises(ValidationError, match="cannot be supplied by callers"):
        k.CoverageObservation(
            signal="citation_frontier_saturation",
            observed=1.0,
            numerator=1,
            denominator=1,
            evidence_sha256=sha("forged-saturation"),
            detail="Caller tries to bypass the committed citation campaign.",
        )
