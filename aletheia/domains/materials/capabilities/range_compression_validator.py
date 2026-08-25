"""Independent typed validator for the materials range-compression capability."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from aletheia.capabilities.observations import (
    CandidateCapabilityObservation,
    ObservationCondition,
    RawExperimentRun,
    ScientificOutcomeClass,
    UncertaintyKind,
)
from aletheia.capabilities.validators import (
    CapabilityObservationValidationPolicy,
    DomainValidationCheck,
    DomainValidationPayload,
    QuantityUnitContract,
)
from aletheia.capabilities.schemas import ExperimentCapabilityManifest
from aletheia.domains.materials.k3_evidence import (
    MaterialsExperimentResult,
    MaterialsOutcomeId,
    MaterialsPreregistration,
    classify_materials_outcome,
)
from aletheia.reproducibility.manifest import content_sha256


TYPED_RANGE_COMPRESSION_VALIDATOR_PRINCIPAL_SHA256 = (
    "f636f6bd880a4fbe4c5040f366e684fc776d5079556cccae13101515fc03ed68"
)


def typed_range_compression_validator_implementation_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def build_range_compression_validation_policy(
    *,
    manifest: ExperimentCapabilityManifest,
    frozen_at: datetime,
) -> CapabilityObservationValidationPolicy:
    return CapabilityObservationValidationPolicy(
        policy_id="materials-range-compression-typed-observation-v1",
        capability_manifest_sha256=manifest.manifest_sha256,
        unit_contracts=(
            QuantityUnitContract(
                quantity_kind_id="prediction_range_compression_difference",
                canonical_ucum_code="1",
                allowed_ucum_codes=("1",),
                conversion_policy_sha256=hashlib.sha256(
                    b"dimensionless-compression-difference-literal-v1"
                ).hexdigest(),
            ),
            QuantityUnitContract(
                quantity_kind_id="random_seed",
                canonical_ucum_code="1",
                allowed_ucum_codes=("1",),
                conversion_policy_sha256=hashlib.sha256(
                    b"dimensionless-random-seed-literal-v1"
                ).hexdigest(),
            ),
        ),
        required_condition_ids=("dataset", "model", "partition-seed"),
        minimum_sample_count=100,
        frozen_at=frozen_at,
    )


def _outcome_class(outcome: MaterialsOutcomeId) -> ScientificOutcomeClass:
    if outcome is MaterialsOutcomeId.UNSEEN_SPECIFIC:
        return ScientificOutcomeClass.POSITIVE
    if outcome in {
        MaterialsOutcomeId.GENERIC_SHRINKAGE,
        MaterialsOutcomeId.NO_MATERIAL_COMPRESSION,
    }:
        return ScientificOutcomeClass.NEGATIVE
    return ScientificOutcomeClass.INCONCLUSIVE


def _check(
    *, check_id: str, passed: bool, failure_code: str, evidence: object
) -> DomainValidationCheck:
    return DomainValidationCheck(
        check_id=check_id,
        passed=passed,
        failure_code=None if passed else failure_code,
        evidence_sha256s=(content_sha256({"check": check_id, "evidence": evidence}),),
    )


class TypedRangeCompressionValidator:
    """Reparse the raw result and independently compare its frozen typed projection."""

    adapter_ref = (
        "aletheia.domains.materials.capabilities.range_compression_validator:"
        "TypedRangeCompressionValidator"
    )
    principal_sha256 = TYPED_RANGE_COMPRESSION_VALIDATOR_PRINCIPAL_SHA256

    def __init__(self, *, preregistration: MaterialsPreregistration) -> None:
        self.preregistration = preregistration

    @property
    def implementation_sha256(self) -> str:
        return typed_range_compression_validator_implementation_sha256()

    def validate(
        self,
        *,
        candidate: CandidateCapabilityObservation,
        raw_run: RawExperimentRun,
        artifacts: Mapping[str, bytes],
    ) -> DomainValidationPayload:
        if "result" not in artifacts:
            raise ValueError("validator cannot find the complete result artifact")
        result = MaterialsExperimentResult.model_validate_json(artifacts["result"])
        protocol = self.preregistration.protocol
        metrics = result.metrics
        measurement = candidate.measurements[0] if len(candidate.measurements) == 1 else None
        conditions = (
            {item.condition_id: item for item in candidate.context.conditions}
            if candidate.context is not None
            else {}
        )
        expected_conditions = {
            "dataset": ObservationCondition(
                condition_id="dataset",
                categorical_value=result.dataset.dataset_ref,
            ),
            "model": ObservationCondition(
                condition_id="model",
                categorical_value=result.fitted_model_identity_sha256,
            ),
            "partition-seed": ObservationCondition(
                condition_id="partition-seed",
                quantity_kind_id="random_seed",
                numeric_value=float(result.split.partition_seed),
                unit_ucum="1",
            ),
        }
        protocol_bound = (
            raw_run.preregistration_sha256 == self.preregistration.preregistration_sha256
            and result.dataset.dataset_ref == protocol.dataset_ref
            and result.dataset.composition_column == protocol.composition_column
            and result.dataset.target_column == protocol.target_column
            and result.split.algorithm == protocol.split.algorithm
            and result.split.partition_seed == protocol.split.partition_seed
            and result.metrics.bootstrap_resamples == protocol.bootstrap.resamples
            and result.metrics.confidence_level == protocol.bootstrap.confidence_level
        )
        classified = classify_materials_outcome(metrics=result.metrics, rule=protocol.outcome_rule)
        outcome_exact = (
            result.outcome_id is classified
            and candidate.scientific_outcome is _outcome_class(result.outcome_id)
            and f"frozen_outcome:{result.outcome_id.value}" in candidate.parser_warnings
        )
        measurement_exact = (
            measurement is not None
            and measurement.measurement_id == "unseen-minus-control-compression"
            and measurement.quantity_kind_id == "prediction_range_compression_difference"
            and measurement.value == metrics.unseen_minus_control_delta
            and measurement.unit_ucum == "1"
            and measurement.uncertainty.kind is UncertaintyKind.CONFIDENCE_INTERVAL
            and measurement.uncertainty.lower == metrics.delta_ci_lower
            and measurement.uncertainty.upper == metrics.delta_ci_upper
            and measurement.uncertainty.coverage_probability == metrics.confidence_level
            and measurement.uncertainty.method_sha256
            == hashlib.sha256(b"chemical_system_cluster_bootstrap_v1").hexdigest()
            and measurement.sample_count
            == min(
                result.split.unseen_chemical_systems,
                result.split.control_chemical_systems,
            )
            and measurement.raw_artifact_ids == ("result",)
            and conditions == expected_conditions
            and candidate.context is not None
            and candidate.context.measurement_method_id == "materials.band_gap.range_compression.v2"
            and candidate.context.batch_id == result.dataset.logical_rows_sha256
            and "computational_model_diagnostic_not_physical_measurement"
            in candidate.parser_warnings
        )
        sample_valid = (
            result.split.unseen_chemical_systems >= 100
            and result.split.control_chemical_systems >= 100
        )
        checks = tuple(
            sorted(
                (
                    _check(
                        check_id="candidate_projection_exact",
                        passed=measurement_exact,
                        failure_code="candidate_projection_mismatch",
                        evidence={
                            "candidate": candidate.candidate_sha256,
                            "result": result.result_sha256,
                        },
                    ),
                    _check(
                        check_id="minimum_sample_rule",
                        passed=sample_valid,
                        failure_code="minimum_chemical_system_count_not_met",
                        evidence=result.split.model_dump(mode="json"),
                    ),
                    _check(
                        check_id="outcome_classification",
                        passed=outcome_exact,
                        failure_code="outcome_projection_mismatch",
                        evidence={
                            "raw": result.outcome_id.value,
                            "classified": classified.value,
                            "candidate": candidate.scientific_outcome.value,
                        },
                    ),
                    _check(
                        check_id="protocol_binding",
                        passed=protocol_bound,
                        failure_code="raw_result_protocol_mismatch",
                        evidence={
                            "preregistration": (self.preregistration.preregistration_sha256),
                            "run_preregistration": raw_run.preregistration_sha256,
                            "result": result.result_sha256,
                        },
                    ),
                    _check(
                        check_id="raw_result_schema",
                        passed=True,
                        failure_code="raw_result_schema_invalid",
                        evidence=result.result_sha256,
                    ),
                ),
                key=lambda item: item.check_id,
            )
        )
        return DomainValidationPayload(
            checks=checks,
            protocol_adherence_verified=protocol_bound and outcome_exact,
            measurement_identity_verified=measurement_exact,
        )


__all__ = [
    "TYPED_RANGE_COMPRESSION_VALIDATOR_PRINCIPAL_SHA256",
    "TypedRangeCompressionValidator",
    "build_range_compression_validation_policy",
    "typed_range_compression_validator_implementation_sha256",
]
