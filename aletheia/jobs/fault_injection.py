"""Deterministic F11 fault-campaign execution and evidence evaluation.

The harness never treats a caught exception as resilience evidence.  A scenario executor must
return a complete observation from the real durable boundary it exercised; this module then
recomputes every invariant and the campaign verdict from that observation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from aletheia.jobs.fault_schemas import (
    CORE_ZERO_METRICS,
    FaultCampaignDisposition,
    FaultCampaignManifest,
    FaultCampaignReport,
    FaultComparator,
    FaultInvariantResult,
    FaultMetric,
    FaultScenarioDisposition,
    FaultScenarioObservation,
    FaultScenarioResult,
    FaultScenarioSpec,
)
from aletheia.reproducibility.manifest import content_sha256

FaultScenarioExecutor = Callable[[FaultScenarioSpec], FaultScenarioObservation]
Clock = Callable[[], datetime]


class FaultCampaignError(RuntimeError):
    """Base error for an invalid or incomplete resilience campaign."""


class FaultCampaignContractError(FaultCampaignError):
    """The manifest, executor matrix, or supplied evidence is structurally incomplete."""


class FaultCampaignInvariantError(FaultCampaignError):
    """A previously materialized report no longer replays to the same verdict."""


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def fault_campaign_order(manifest: FaultCampaignManifest) -> tuple[FaultScenarioSpec, ...]:
    """Return the deterministic, seed-bound execution order for a campaign."""

    manifest = FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
    return tuple(
        sorted(
            manifest.scenarios,
            key=lambda item: (
                content_sha256(
                    {
                        "schema": "aletheia.fault_campaign_order.v1",
                        "seed": manifest.seed,
                        "scenario_id": item.scenario_id,
                    }
                ),
                item.scenario_id,
            ),
        )
    )


def _invariant_passes(
    comparator: FaultComparator,
    *,
    observed: int,
    expected: int,
) -> bool:
    return {
        FaultComparator.EXACT: observed == expected,
        FaultComparator.AT_MOST: observed <= expected,
        FaultComparator.AT_LEAST: observed >= expected,
    }[comparator]


def evaluate_fault_scenario(
    spec: FaultScenarioSpec,
    observation: FaultScenarioObservation,
) -> FaultScenarioResult:
    """Recompute one scenario verdict without trusting executor-supplied pass/fail state."""

    spec = FaultScenarioSpec.model_validate(spec.model_dump(mode="python"))
    observation = FaultScenarioObservation.model_validate(
        observation.model_dump(mode="python")
    )
    blockers: list[str] = []
    if observation.scenario_id != spec.scenario_id:
        raise FaultCampaignContractError(
            f"fault observation {observation.scenario_id!r} does not belong to "
            f"{spec.scenario_id!r}"
        )
    if not observation.injection_confirmed:
        blockers.append("injection:not_confirmed")
    if observation.observed_outcome != spec.expected_outcome:
        observed = (
            observation.observed_outcome.value
            if observation.observed_outcome is not None
            else "none"
        )
        blockers.append(
            f"outcome:mismatch:{observed}/{spec.expected_outcome.value}"
        )

    missing_actions = set(spec.required_recovery_actions) - set(
        observation.recovery_actions
    )
    blockers.extend(
        f"recovery:missing:{item.value}"
        for item in sorted(missing_actions, key=lambda value: value.value)
    )

    observed_by_metric = {item.metric: item for item in observation.metrics}
    expected_by_metric = {item.metric: item for item in spec.expectations}
    missing_metrics = set(expected_by_metric) - set(observed_by_metric)
    extra_metrics = set(observed_by_metric) - set(expected_by_metric)
    blockers.extend(
        f"metrics:missing:{item.value}"
        for item in sorted(missing_metrics, key=lambda value: value.value)
    )
    blockers.extend(
        f"metrics:unexpected:{item.value}"
        for item in sorted(extra_metrics, key=lambda value: value.value)
    )

    referenced_evidence = {
        observation.diagnostic_sha256,
        *(item.evidence_sha256 for item in observation.metrics),
    }
    missing_evidence = referenced_evidence - set(observation.evidence_sha256s)
    blockers.extend(f"evidence:missing:{item}" for item in sorted(missing_evidence))

    elapsed_seconds = (observation.completed_at - observation.started_at).total_seconds()
    if elapsed_seconds > spec.timeout_seconds:
        blockers.append(
            f"harness:timeout_exceeded:{elapsed_seconds:.6f}/{spec.timeout_seconds}"
        )

    invariants: list[FaultInvariantResult] = []
    for metric, expectation in sorted(
        expected_by_metric.items(), key=lambda item: item[0].value
    ):
        observed = observed_by_metric.get(metric)
        if observed is None:
            continue
        passed = _invariant_passes(
            expectation.comparator,
            observed=observed.observed_value,
            expected=expectation.expected_value,
        )
        invariants.append(
            FaultInvariantResult(
                metric=metric,
                comparator=expectation.comparator,
                expected_value=expectation.expected_value,
                observed_value=observed.observed_value,
                evidence_sha256=observed.evidence_sha256,
                passed=passed,
            )
        )
        if not passed:
            blockers.append(
                "invariant:failed:"
                f"{metric.value}:{observed.observed_value}:"
                f"{expectation.comparator.value}:{expectation.expected_value}"
            )

    canonical_blockers = tuple(sorted(set(blockers)))
    disposition = (
        FaultScenarioDisposition.PASSED
        if not canonical_blockers
        else FaultScenarioDisposition.BLOCKED
        if not observation.injection_confirmed
        else FaultScenarioDisposition.FAILED
    )
    return FaultScenarioResult(
        scenario_id=spec.scenario_id,
        spec_sha256=spec.spec_sha256,
        observation=observation,
        invariants=tuple(invariants),
        disposition=disposition,
        blockers=canonical_blockers,
    )


def evaluate_fault_campaign(
    manifest: FaultCampaignManifest,
    observations: Sequence[FaultScenarioObservation],
    *,
    completed_at: datetime,
) -> FaultCampaignReport:
    """Build a complete campaign report from exact manifested observations."""

    manifest = FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
    completed_at = _aware(completed_at, label="fault campaign completion timestamp")
    canonical_observations = tuple(
        FaultScenarioObservation.model_validate(item.model_dump(mode="python"))
        for item in observations
    )
    observed_ids = [item.scenario_id for item in canonical_observations]
    if len(observed_ids) != len(set(observed_ids)):
        raise FaultCampaignContractError("fault campaign observations contain duplicate identities")
    expected_ids = {item.scenario_id for item in manifest.scenarios}
    actual_ids = set(observed_ids)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise FaultCampaignContractError(
            f"fault campaign evidence matrix differs from manifest: missing={missing}, extra={extra}"
        )
    observations_by_id = {item.scenario_id: item for item in canonical_observations}
    results = tuple(
        evaluate_fault_scenario(spec, observations_by_id[spec.scenario_id])
        for spec in manifest.scenarios
    )
    passed_count = sum(
        item.disposition is FaultScenarioDisposition.PASSED for item in results
    )
    failed_count = sum(
        item.disposition is FaultScenarioDisposition.FAILED for item in results
    )
    blocked_count = sum(
        item.disposition is FaultScenarioDisposition.BLOCKED for item in results
    )
    disposition = (
        FaultCampaignDisposition.FAILED
        if failed_count
        else FaultCampaignDisposition.BLOCKED
        if blocked_count
        else FaultCampaignDisposition.PASSED
    )
    core_totals = {
        metric: sum(
            observed.observed_value
            for result in results
            for observed in result.observation.metrics
            if observed.metric is metric
        )
        for metric in CORE_ZERO_METRICS
    }
    return FaultCampaignReport(
        manifest=manifest,
        results=results,
        disposition=disposition,
        scenario_count=len(results),
        passed_count=passed_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        scientific_state_loss_count=core_totals[
            FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT
        ],
        duplicate_scientific_state_count=core_totals[
            FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT
        ],
        duplicate_budget_charge_count=core_totals[
            FaultMetric.DUPLICATE_BUDGET_CHARGE_COUNT
        ],
        duplicate_outward_authorization_count=core_totals[
            FaultMetric.DUPLICATE_OUTWARD_AUTHORIZATION_COUNT
        ],
        unresolved_ambiguity_without_block_count=core_totals[
            FaultMetric.UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT
        ],
        event_state_mismatch_count=core_totals[
            FaultMetric.EVENT_STATE_MISMATCH_COUNT
        ],
        completed_at=completed_at,
    )


def validate_fault_campaign_report(report: FaultCampaignReport) -> FaultCampaignReport:
    """Re-evaluate persisted evidence and reject any caller-authored verdict or aggregate."""

    report = FaultCampaignReport.model_validate(report.model_dump(mode="python"))
    replayed = evaluate_fault_campaign(
        report.manifest,
        tuple(item.observation for item in report.results),
        completed_at=report.completed_at,
    )
    if replayed != report:
        raise FaultCampaignInvariantError(
            f"fault campaign report does not replay exactly: {report.manifest.campaign_id}"
        )
    return replayed


def run_fault_campaign(
    manifest: FaultCampaignManifest,
    executors: Mapping[str, FaultScenarioExecutor],
    *,
    clock: Clock | None = None,
) -> FaultCampaignReport:
    """Execute each real boundary in seeded order and evaluate the returned evidence.

    Executor exceptions intentionally escape: a harness crash is not a blocked or passing
    resilience result.  The caller may rerun the same content-addressed manifest safely.
    """

    manifest = FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
    scenario_ids = {item.scenario_id for item in manifest.scenarios}
    executor_ids = set(executors)
    if executor_ids != scenario_ids:
        raise FaultCampaignContractError(
            "fault campaign executor matrix differs from manifest: "
            f"missing={sorted(scenario_ids - executor_ids)}, "
            f"extra={sorted(executor_ids - scenario_ids)}"
        )
    observations: list[FaultScenarioObservation] = []
    for spec in fault_campaign_order(manifest):
        observation = executors[spec.scenario_id](spec)
        observations.append(
            FaultScenarioObservation.model_validate(observation.model_dump(mode="python"))
        )
    observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    return evaluate_fault_campaign(
        manifest,
        observations,
        completed_at=_aware(observed_at, label="fault campaign harness clock"),
    )


__all__ = [
    "FaultCampaignContractError",
    "FaultCampaignError",
    "FaultCampaignInvariantError",
    "FaultScenarioExecutor",
    "evaluate_fault_campaign",
    "evaluate_fault_scenario",
    "fault_campaign_order",
    "run_fault_campaign",
    "validate_fault_campaign_report",
]
