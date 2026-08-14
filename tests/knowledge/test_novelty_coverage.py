from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s4_fixtures import build_review
from .f8s5_fixtures import (
    RECEIPT_KEY,
    build_f8s5_live_fixture,
)
from .test_schema_spike import _time


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    return asyncio.run(build_f8s5_live_fixture(tmp_path_factory.mktemp("f8s5-live")))


def _revalidate(model_type, model, **updates):
    raw = model.model_dump(mode="python")
    raw.update(updates)
    return model_type.model_validate(raw)


def _rebuild_coverage(fixture, **updates):
    arguments = {
        "assessment_id": "f8s5-rebuilt-calibrated-coverage",
        "calibration_report": fixture["report"],
        "calibration_receipt_key": RECEIPT_KEY,
        "ingestion_bundle": fixture["ingestion_bundle"],
        "claim_graph_bundle": fixture["graph_bundle"],
        "prior_art_resolution": fixture["prior_art_resolution"],
        "correction_report": fixture["correction_report"],
        "campaign": fixture["campaign"],
        "policy_frozen_at": _time("2025-08-02T00:00:00Z"),
        "generated_at": _time("2025-08-06T00:00:00Z"),
    }
    arguments.update(updates)
    return k.build_calibrated_novelty_coverage_assessment(**arguments)


def test_live_coverage_is_sufficient_only_from_bound_artifacts(fixture) -> None:
    coverage = fixture["coverage"]
    observations = {item.signal: item for item in coverage.search_assessment.external_observations}

    assert coverage.decision_verdict is k.CoverageVerdict.SUFFICIENT
    assert coverage.decision_blockers == ()
    assert coverage.search_assessment.report.verdict is k.CoverageVerdict.SUFFICIENT
    assert observations[k.CoverageSignalName.KNOWN_ANSWER_RECALL].observed < 1.0
    assert observations[k.CoverageSignalName.KNOWN_ANSWER_RECALL].observed > 0.80
    assert observations[k.CoverageSignalName.SEED_REFERENCE_RECOVERY].observed == 1.0
    assert observations[k.CoverageSignalName.FULL_TEXT_AVAILABILITY].observed == 1.0
    assert observations[k.CoverageSignalName.SOURCE_SPAN_VERIFICATION].observed == 1.0
    assert observations[k.CoverageSignalName.CORRECTION_RETRACTION_CHECK].observed == 1.0
    assert observations[k.CoverageSignalName.PERTURBATION_STABILITY].observed > 0.90


def test_live_policy_thresholds_are_derived_from_calibration(fixture) -> None:
    policy = fixture["coverage"].search_assessment.report.policy
    thresholds = {requirement.signal: requirement.threshold for requirement in policy.requirements}
    calibration_policy = fixture["policy"]

    assert thresholds[k.CoverageSignalName.KNOWN_ANSWER_RECALL] == (
        calibration_policy.minimum_known_answer_recall_lower_bound
    )
    assert thresholds[k.CoverageSignalName.SEED_REFERENCE_RECOVERY] == (
        calibration_policy.minimum_seed_reference_recovery_lower_bound
    )
    assert thresholds[k.CoverageSignalName.PERTURBATION_STABILITY] == (
        calibration_policy.minimum_perturbation_stability_lower_bound
    )
    assert policy.minimum_nearest_prior_art == 3
    assert policy.minimum_independent_reviewers == 2


def test_caller_cannot_substitute_an_external_coverage_number(fixture) -> None:
    coverage = fixture["coverage"]
    raw = coverage.model_dump(mode="python")
    search_raw = coverage.search_assessment.model_dump(mode="python")
    observations = list(coverage.search_assessment.external_observations)
    observations[0] = _revalidate(
        k.CoverageObservation,
        observations[0],
        observed=1.0,
        evidence_sha256=sha("caller-supplied-perfect-recall"),
    )
    search_raw["external_observations"] = tuple(observations)
    raw["search_assessment"] = search_raw

    with pytest.raises(ValidationError, match="not derived from exact artifacts"):
        k.CalibratedNoveltyCoverageAssessment.model_validate(raw)


def test_failed_global_calibration_blocks_an_otherwise_complete_live_search(fixture) -> None:
    receipts = list(fixture["receipts"])
    case = fixture["cases"][0]
    receipts[1] = k.issue_failed_calibration_trial_receipt(
        suite=fixture["suite"],
        case=case,
        variant=case.variants[1],
        evaluator_manifest=fixture["evaluator_manifest"],
        receipt_key=RECEIPT_KEY,
        failure=RuntimeError("calibration trial failed"),
        completed_at=_time("2025-07-02T00:00:00Z"),
    )
    failed_calibration = k.build_novelty_calibration_report(
        report_id="f8s5-failed-global-calibration",
        suite=fixture["suite"],
        evaluator_manifest=fixture["evaluator_manifest"],
        labels=fixture["labels"],
        trial_receipts=tuple(receipts),
        receipt_key=RECEIPT_KEY,
        generated_at=_time("2025-07-04T00:00:00Z"),
    )

    coverage = _rebuild_coverage(
        fixture,
        calibration_report=failed_calibration,
    )

    assert coverage.search_assessment.report.verdict is k.CoverageVerdict.SUFFICIENT
    assert coverage.decision_verdict is k.CoverageVerdict.INSUFFICIENT
    assert coverage.decision_blockers == ("global_calibration_failed",)


def test_incomplete_correction_check_becomes_a_hard_coverage_failure(fixture) -> None:
    incomplete = _revalidate(
        k.ContradictionCorrectionReport,
        fixture["correction_report"],
        correction_retraction_check_complete=False,
    )

    coverage = _rebuild_coverage(fixture, correction_report=incomplete)

    assert coverage.decision_verdict is k.CoverageVerdict.INSUFFICIENT
    assert k.CoverageSignalName.CORRECTION_RETRACTION_CHECK in (
        coverage.search_assessment.report.hard_failure_signals
    )
    assert coverage.decision_blockers == ("coverage_signal:correction_retraction_check",)


def test_nearest_prior_art_floor_is_an_explicit_decision_blocker(fixture) -> None:
    execution = fixture["prior_art_execution"]
    review_task = execution.review_queue.tasks[0]
    candidate = next(
        item
        for item in execution.relation_candidates
        if item.relation_candidate_sha256 == review_task.relation_candidate_sha256
    )
    rejected = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(
            build_review(
                execution=execution,
                candidate=candidate,
                decision="reject",
            ),
        ),
        resolution_id="resolution:f8s5:below-prior-floor",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )

    coverage = _rebuild_coverage(fixture, prior_art_resolution=rejected)

    assert len(rejected.accepted) == 2
    assert coverage.search_assessment.report.verdict is k.CoverageVerdict.SUFFICIENT
    assert coverage.decision_verdict is k.CoverageVerdict.INSUFFICIENT
    assert coverage.decision_blockers[0].startswith("nearest_prior_art_below_minimum:")


def test_cross_corpus_correction_report_is_rejected(fixture) -> None:
    wrong = _revalidate(
        k.ContradictionCorrectionReport,
        fixture["correction_report"],
        corpus_snapshot_sha256=sha("another-corpus"),
    )

    with pytest.raises(ValueError, match="bound to another corpus/claim graph"):
        _rebuild_coverage(fixture, correction_report=wrong)


def test_actual_prior_art_resolution_issues_a_bound_calibration_receipt(fixture) -> None:
    cases = list(fixture["cases"])
    labels = list(fixture["labels"])
    original_case = cases[0]
    variants = list(original_case.variants)
    variants[0] = _revalidate(
        k.NoveltyCalibrationVariant,
        variants[0],
        candidate_claim_sha256=fixture["prior_fixture"]["candidate"].claim_sha256,
        graph_bundle_sha256=fixture["graph_bundle"].bundle_sha256,
        search_protocol_sha256=fixture["search_protocol"].protocol_sha256,
    )
    cases[0] = _revalidate(
        k.NoveltyCalibrationCase,
        original_case,
        temporal_cutoff=fixture["ingestion_bundle"].corpus.cutoff_time,
        corpus_snapshot_sha256=fixture["ingestion_bundle"].corpus.snapshot_sha256,
        variants=tuple(variants),
        frozen_at=_time("2025-08-01T00:00:00Z"),
    )
    labels[0] = _revalidate(
        k.NoveltyCalibrationLabel,
        labels[0],
        case_sha256=cases[0].case_sha256,
    )
    suite = k.build_novelty_calibration_suite(
        suite_id="f8s5-integrated-calibration-suite",
        policy=fixture["policy"],
        system_manifest_sha256=(
            fixture["prior_art_resolution"].execution.protocol.matcher_manifest.manifest_sha256
        ),
        cases=tuple(cases),
        labels=tuple(labels),
        holdout_custody_manifest_sha256=sha("integrated-holdout-custody"),
        sealed_at=_time("2025-08-02T00:00:00Z"),
    )
    search_session = k.build_campaign_search_session(fixture["campaign"])

    receipt = k.issue_calibration_trial_receipt(
        suite=suite,
        case=cases[0],
        variant=variants[0],
        resolution=fixture["prior_art_resolution"],
        search_session=search_session,
        evaluator_manifest=fixture["evaluator_manifest"],
        receipt_key=RECEIPT_KEY,
        completed_at=_time("2025-08-04T00:00:00Z"),
    )

    assert receipt.payload.prior_art_resolution_sha256 == (
        fixture["prior_art_resolution"].resolution_sha256
    )
    assert receipt.payload.search_session_sha256 == search_session.session_sha256
    assert receipt.payload.predicted_classification is (
        k.NoveltyClassification.INCREMENTAL_EXTENSION
    )
    receipt.verify(
        key=RECEIPT_KEY,
        expected_key_id=fixture["evaluator_manifest"].receipt_key_id,
    )


def test_calibrated_coverage_archive_round_trip_rechecks_calibration_key(fixture, tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "coverage-archive")
    committed = k.commit_calibrated_novelty_coverage(
        archive=archive,
        assessment=fixture["coverage"],
    )
    loaded = k.load_calibrated_novelty_coverage(
        archive=archive,
        ledger=committed.ledger,
        calibration_receipt_key=RECEIPT_KEY,
    )

    assert loaded == fixture["coverage"]
    with pytest.raises(ValueError, match="signature is invalid"):
        k.load_calibrated_novelty_coverage(
            archive=archive,
            ledger=committed.ledger,
            calibration_receipt_key=bytes.fromhex(sha("wrong-coverage-key")),
        )
