from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s5_fixtures import (
    RECEIPT_KEY,
    build_f8s5_fixture,
    resign_receipt,
)
from .test_schema_spike import _time


@pytest.fixture(scope="module")
def fixture():
    return build_f8s5_fixture()


def _build_report(fixture, receipts, *, labels=None):
    return k.build_novelty_calibration_report(
        report_id="f8s5-mutated-calibration-report",
        suite=fixture["suite"],
        evaluator_manifest=fixture["evaluator_manifest"],
        labels=tuple(labels or fixture["labels"]),
        trial_receipts=tuple(receipts),
        receipt_key=RECEIPT_KEY,
        generated_at=_time("2025-07-04T00:00:00Z"),
    )


def _method_relation(receipt, identity: str) -> k.CalibrationRelationView:
    original = receipt.payload.relations[0]
    return k.CalibrationRelationView(
        rank=1,
        prior_claim_sha256=original.prior_claim_sha256,
        relation=k.PriorArtRelationType.EXTENSION,
        difference_components=(k.DifferenceComponent.METHOD,),
        relation_sha256=sha(f"{identity}:method-relation"),
    )


def test_perfect_validation_and_temporal_holdout_pass_with_confidence_bounds(fixture) -> None:
    report = fixture["report"]

    assert report.verdict is k.NoveltyCalibrationVerdict.PASS
    assert report.failures == ()
    assert tuple(metric.split for metric in report.metrics) == tuple(k.NoveltyCalibrationSplit)
    for metric in report.metrics:
        assert metric.cases == 40
        assert metric.trials == 120
        assert metric.known_answer_recall.estimate == 1.0
        assert metric.known_answer_recall.lower_bound > 0.80
        assert metric.false_strong_novelty.estimate == 0.0
        assert metric.false_strong_novelty.upper_bound < 0.10
        assert metric.missed_strong_novelty.upper_bound < 0.25
        assert metric.perturbation_stability.lower_bound > 0.90
        assert metric.nearest_prior_mrr.mean_reciprocal_rank == 1.0


def test_any_explicit_trial_error_fail_closes_the_report(fixture) -> None:
    receipts = list(fixture["receipts"])
    case = fixture["cases"][0]
    variant = case.variants[1]
    failed = k.issue_failed_calibration_trial_receipt(
        suite=fixture["suite"],
        case=case,
        variant=variant,
        evaluator_manifest=fixture["evaluator_manifest"],
        receipt_key=RECEIPT_KEY,
        failure=RuntimeError("secret provider response must not enter the ledger"),
        completed_at=_time("2025-07-02T00:00:00Z"),
    )
    receipts[1] = failed

    report = _build_report(fixture, receipts)

    assert report.verdict is k.NoveltyCalibrationVerdict.FAIL
    assert (
        k.CalibrationFailure(
            split=k.NoveltyCalibrationSplit.VALIDATION,
            signal=k.CalibrationFailureSignal.TRIAL_ERROR,
        )
        in report.failures
    )
    assert "secret provider response" not in failed.model_dump_json()


def test_temporal_false_strong_novelty_fails_even_when_validation_passes(fixture) -> None:
    receipts = list(fixture["receipts"])
    first_holdout = 40 * 3
    for index in range(first_holdout, first_holdout + 3):
        relation = _method_relation(receipts[index], f"false-strong:{index}")
        receipts[index] = resign_receipt(
            receipts[index],
            relations=(relation,),
            predicted_classification=k.NoveltyClassification.NOVEL_METHOD,
        )

    report = _build_report(fixture, receipts)

    assert report.verdict is k.NoveltyCalibrationVerdict.FAIL
    assert report.metrics[0].false_strong_novelty.events == 0
    assert report.metrics[1].false_strong_novelty.events == 1
    assert report.metrics[1].false_strong_novelty.upper_bound > 0.10
    assert (
        k.CalibrationFailure(
            split=k.NoveltyCalibrationSplit.TEMPORAL_HOLDOUT,
            signal=k.CalibrationFailureSignal.FALSE_STRONG_NOVELTY,
        )
        in report.failures
    )


def test_perturbation_instability_is_measured_independently_of_base_accuracy(fixture) -> None:
    receipts = list(fixture["receipts"])
    for case_index in range(5):
        index = case_index * 3 + 1
        relation = _method_relation(receipts[index], f"unstable:{index}")
        receipts[index] = resign_receipt(
            receipts[index],
            relations=(relation,),
            predicted_classification=k.NoveltyClassification.NOVEL_METHOD,
        )

    report = _build_report(fixture, receipts)
    validation = report.metrics[0]

    assert validation.classification_accuracy.estimate == 1.0
    assert validation.perturbation_stability.events == 75
    assert validation.perturbation_stability.lower_bound < 0.90
    assert (
        k.CalibrationFailure(
            split=k.NoveltyCalibrationSplit.VALIDATION,
            signal=k.CalibrationFailureSignal.PERTURBATION_STABILITY,
        )
        in report.failures
    )


def test_missing_receipt_is_rejected_before_metric_derivation(fixture) -> None:
    with pytest.raises(ValueError, match="every case variant exactly once"):
        _build_report(fixture, fixture["receipts"][:-1])


def test_receipt_order_is_bound_to_sealed_case_variant_order(fixture) -> None:
    receipts = list(fixture["receipts"])
    receipts[0], receipts[1] = receipts[1], receipts[0]

    with pytest.raises(ValueError, match="differs from sealed case/variant order"):
        _build_report(fixture, receipts)


def test_resolution_and_search_evidence_cannot_be_reused_across_trials(fixture) -> None:
    receipts = list(fixture["receipts"])
    receipts[1] = resign_receipt(
        receipts[1],
        prior_art_resolution_sha256=receipts[0].payload.prior_art_resolution_sha256,
        search_session_sha256=receipts[0].payload.search_session_sha256,
    )

    with pytest.raises(ValueError, match="cannot reuse IDs, receipts, or evidence"):
        _build_report(fixture, receipts)


def test_invalid_hmac_is_rejected(fixture) -> None:
    receipts = list(fixture["receipts"])
    raw = receipts[0].model_dump(mode="python")
    raw["hmac_sha256"] = sha("forged-hmac")
    receipts[0] = k.SignedCalibrationTrialReceipt.model_validate(raw)

    with pytest.raises(ValueError, match="signature is invalid"):
        _build_report(fixture, receipts)


def test_sealed_label_commitment_prevents_label_substitution(fixture) -> None:
    labels = list(fixture["labels"])
    raw = labels[0].model_dump(mode="python")
    raw["expected_seed_paper_sha256s"] = (sha("substituted-seed"),)
    labels[0] = k.NoveltyCalibrationLabel.model_validate(raw)

    with pytest.raises(ValueError, match="sealed commitment"):
        _build_report(fixture, fixture["receipts"], labels=labels)


def test_report_metrics_and_verdict_cannot_be_forged(fixture) -> None:
    report = fixture["report"]
    raw = report.model_dump(mode="python")
    raw["metrics"] = (report.metrics[1], report.metrics[0])
    raw["verdict"] = k.NoveltyCalibrationVerdict.FAIL

    with pytest.raises(ValidationError, match="metrics/failures are not derived"):
        k.NoveltyCalibrationReport.model_validate(raw)


def test_calibration_report_archive_round_trip_reverifies_receipts(fixture, tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "calibration-archive")
    committed = k.commit_novelty_calibration_report(
        archive=archive,
        report=fixture["report"],
    )

    loaded = k.load_novelty_calibration_report(
        archive=archive,
        ledger=committed.ledger,
        receipt_key=RECEIPT_KEY,
    )

    assert loaded == fixture["report"]
    assert committed.ledger.object_sha256 == fixture["report"].report_sha256


def test_archive_load_rejects_the_wrong_evaluator_key(fixture, tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "wrong-key-archive")
    committed = k.commit_novelty_calibration_report(
        archive=archive,
        report=fixture["report"],
    )

    with pytest.raises(ValueError, match="signature is invalid"):
        k.load_novelty_calibration_report(
            archive=archive,
            ledger=committed.ledger,
            receipt_key=bytes.fromhex(sha("another-32-byte-key")),
        )
