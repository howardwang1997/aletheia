"""Typed materials parser/validator projection and anti-double-counting tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import aletheia.capabilities as c
from aletheia.domains.materials import k3_evidence as k3
from aletheia.domains.materials.capabilities.range_compression_validator import (
    TYPED_RANGE_COMPRESSION_VALIDATOR_PRINCIPAL_SHA256,
    TypedRangeCompressionValidator,
    build_range_compression_validation_policy,
    typed_range_compression_validator_implementation_sha256,
)
from aletheia.domains.materials.capabilities.typed_range_compression import (
    TYPED_RANGE_COMPRESSION_PARSER_PRINCIPAL_SHA256,
    TypedRangeCompressionParser,
    typed_range_compression_parser_implementation_sha256,
)


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    ROOT / "configs/capabilities/materials_band_gap_range_compression_provisional_v2.yaml"
)
PROTOCOL_PATH = ROOT / "configs/materials/k3_band_gap_range_compression_v2.yaml"
BASE = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _manifest():
    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    previous = c.ExperimentCapabilityManifest.model_validate(raw)
    raw["version"] = "2.1.0"
    raw["supersedes_manifest_sha256"] = previous.manifest_sha256
    raw["frozen_at"] = BASE - timedelta(minutes=10)
    parser = raw["roles"][2]
    parser.update(
        {
            "adapter_ref": TypedRangeCompressionParser.adapter_ref,
            "implementation_sha256": (typed_range_compression_parser_implementation_sha256()),
            "principal_sha256": TYPED_RANGE_COMPRESSION_PARSER_PRINCIPAL_SHA256,
            "frozen_at": BASE - timedelta(minutes=11),
        }
    )
    validator = raw["roles"][3]
    validator.update(
        {
            "adapter_ref": TypedRangeCompressionValidator.adapter_ref,
            "implementation_sha256": (typed_range_compression_validator_implementation_sha256()),
            "principal_sha256": TYPED_RANGE_COMPRESSION_VALIDATOR_PRINCIPAL_SHA256,
            "frozen_at": BASE - timedelta(minutes=11),
        }
    )
    return c.ExperimentCapabilityManifest.model_validate(raw)


def _preregistration():
    protocol = k3.MaterialsK3Protocol.model_validate(
        yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    )
    return k3.build_materials_preregistration(
        preregistration_id="typed-range-compression-test-preregistration",
        protocol=protocol,
        preregistered_at=protocol.frozen_at + timedelta(minutes=1),
    )


def _result(preregistration):
    metrics = k3.MaterialsCompressionMetrics(
        unseen_true_sd_ev=2.0,
        unseen_predicted_sd_ev=1.6,
        unseen_compression=0.2,
        control_true_sd_ev=2.0,
        control_predicted_sd_ev=1.64,
        control_compression=0.18,
        unseen_minus_control_delta=0.02,
        delta_ci_lower=-0.04,
        delta_ci_upper=0.08,
        bootstrap_probability_delta_above_zero=0.72,
        unseen_mae_ev=0.5,
        control_mae_ev=0.4,
        bootstrap_resamples=preregistration.protocol.bootstrap.resamples,
        confidence_level=preregistration.protocol.bootstrap.confidence_level,
    )
    return k3.MaterialsExperimentResult(
        dataset=k3.MaterialsDatasetReceipt(
            dataset_ref=preregistration.protocol.dataset_ref,
            composition_column=preregistration.protocol.composition_column,
            target_column=preregistration.protocol.target_column,
            row_count=600,
            feature_count=132,
            chemical_system_count=430,
            logical_rows_sha256=sha("rows"),
            feature_names_sha256=sha("features"),
            feature_matrix_sha256=sha("matrix"),
            target_vector_sha256=sha("targets"),
            chemical_system_vector_sha256=sha("systems"),
            package_versions={"numpy": "test"},
        ),
        split=k3.MaterialsSplitReceipt(
            algorithm=preregistration.protocol.split.algorithm,
            partition_seed=preregistration.protocol.split.partition_seed,
            train_rows=300,
            unseen_test_rows=150,
            within_system_control_rows=150,
            train_chemical_systems=200,
            unseen_chemical_systems=120,
            control_chemical_systems=110,
            train_membership_sha256=sha("train"),
            unseen_membership_sha256=sha("unseen"),
            control_membership_sha256=sha("control"),
        ),
        metrics=metrics,
        outcome_id=k3.MaterialsOutcomeId.GENERIC_SHRINKAGE,
        unseen_predictions_sha256=sha("unseen-predictions"),
        control_predictions_sha256=sha("control-predictions"),
        fitted_model_identity_sha256=sha("model"),
    )


def _run_pipeline(tmp_path, *, purpose=c.ExperimentRunPurpose.MEASUREMENT, parser=None):
    manifest = _manifest()
    preregistration = _preregistration()
    result = _result(preregistration)
    archive = c.CapabilityObservationArchive(tmp_path / "raw")
    receipt = archive.store(
        artifact_id="result",
        payload=result.model_dump_json().encode(),
        media_type="application/json",
        captured_at=BASE + timedelta(minutes=2),
    )
    raw_run = c.build_raw_experiment_run(
        run_id="typed-range-compression-run-001",
        manifest=manifest,
        preregistration_sha256=preregistration.preregistration_sha256,
        input_sha256=sha("input"),
        status=c.ExperimentRunStatus.SUCCEEDED,
        artifacts=(receipt,),
        started_at=BASE + timedelta(minutes=1),
        ended_at=BASE + timedelta(minutes=2),
        run_purpose=purpose,
    )
    parsed = c.parse_capability_observation(
        manifest=manifest,
        raw_run=raw_run,
        archive=archive,
        adapter=parser or TypedRangeCompressionParser(),
        parsed_at=BASE + timedelta(minutes=3),
    )
    pipeline = c.validate_capability_observation(
        manifest=manifest,
        policy=build_range_compression_validation_policy(manifest=manifest, frozen_at=BASE),
        parse_result=parsed,
        archive=archive,
        adapter=TypedRangeCompressionValidator(preregistration=preregistration),
        validated_at=BASE + timedelta(minutes=4),
    )
    return pipeline


def test_real_result_projection_is_validated_negative_with_exact_domain_reparse(tmp_path):
    pipeline = _run_pipeline(tmp_path)
    assert pipeline.disposition is c.CapabilityObservationDisposition.VALIDATED_NEGATIVE
    validation = pipeline.validation
    assert validation is not None
    assert validation.scientific_negative_preserved is True
    assert validation.admissible_for_f9_exploratory_update is True
    assert validation.admissible_for_f9_confirmatory_update is False
    assert validation.domain_report is not None
    assert all(item.passed for item in validation.domain_report.checks)
    measurement = validation.candidate.measurements[0]
    assert measurement.value == 0.02
    assert measurement.unit_ucum == "1"
    assert (measurement.uncertainty.lower, measurement.uncertainty.upper) == (
        -0.04,
        0.08,
    )


def test_exact_reexecution_is_valid_but_cannot_double_count_as_new_f9_evidence(tmp_path):
    pipeline = _run_pipeline(tmp_path, purpose=c.ExperimentRunPurpose.EXACT_REEXECUTION)
    assert pipeline.disposition is c.CapabilityObservationDisposition.VALIDATED_NEGATIVE
    assert pipeline.validation is not None
    assert pipeline.validation.admissible_for_f9_exploratory_update is False
    assert pipeline.validation.admissible_for_f9_confirmatory_update is False


def test_independent_validator_rejects_a_parser_projection_that_changes_the_metric(tmp_path):
    malicious = TypedRangeCompressionParser()

    class ChangedMetricParser:
        adapter_ref = malicious.adapter_ref
        implementation_sha256 = malicious.implementation_sha256
        principal_sha256 = malicious.principal_sha256

        def parse(self, *, raw_run, artifacts):
            payload = malicious.parse(raw_run=raw_run, artifacts=artifacts)
            measurement = payload.measurements[0].model_copy(update={"value": 0.5})
            return payload.model_copy(update={"measurements": (measurement,)})

    pipeline = _run_pipeline(tmp_path, parser=ChangedMetricParser())
    assert pipeline.disposition is c.CapabilityObservationDisposition.REJECTED_INVALID
    assert pipeline.validation is not None
    assert "candidate_projection_mismatch" in pipeline.validation.blockers
    assert pipeline.validation.admissible_for_f9_exploratory_update is False
