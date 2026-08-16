"""Typed F10-S2 parser for complete range-compression executor results."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from aletheia.capabilities.observations import (
    ExperimentRunStatus,
    MeasuredQuantity,
    MeasurementUncertainty,
    ObservationCondition,
    ObservationContext,
    ParsedObservationPayload,
    RawExperimentRun,
    ScientificOutcomeClass,
    UncertaintyKind,
)
from aletheia.domains.materials.k3_evidence import (
    MaterialsExperimentResult,
    MaterialsOutcomeId,
)


TYPED_RANGE_COMPRESSION_PARSER_PRINCIPAL_SHA256 = (
    "cf63a23d943e5818f825ba4e4d070c8f04f391cff3969250fb623c330e37d8f5"
)


def typed_range_compression_parser_implementation_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _scientific_outcome(outcome: MaterialsOutcomeId) -> ScientificOutcomeClass:
    if outcome is MaterialsOutcomeId.UNSEEN_SPECIFIC:
        return ScientificOutcomeClass.POSITIVE
    if outcome in {
        MaterialsOutcomeId.GENERIC_SHRINKAGE,
        MaterialsOutcomeId.NO_MATERIAL_COMPRESSION,
    }:
        return ScientificOutcomeClass.NEGATIVE
    return ScientificOutcomeClass.INCONCLUSIVE


class TypedRangeCompressionParser:
    """Parse one exact raw result without deciding scientific validity."""

    adapter_ref = (
        "aletheia.domains.materials.capabilities.typed_range_compression:"
        "TypedRangeCompressionParser"
    )
    principal_sha256 = TYPED_RANGE_COMPRESSION_PARSER_PRINCIPAL_SHA256

    @property
    def implementation_sha256(self) -> str:
        return typed_range_compression_parser_implementation_sha256()

    def parse(
        self, *, raw_run: RawExperimentRun, artifacts: Mapping[str, bytes]
    ) -> ParsedObservationPayload:
        if raw_run.status is not ExperimentRunStatus.SUCCEEDED:
            return ParsedObservationPayload(
                scientific_outcome=ScientificOutcomeClass.NOT_EVALUABLE,
                execution_failure_acknowledged=True,
                parser_warnings=("executor_failure_retained_without_scientific_outcome",),
            )
        if "result" not in artifacts:
            raise ValueError("range-compression run lacks the complete result artifact")
        result = MaterialsExperimentResult.model_validate_json(artifacts["result"])
        metrics = result.metrics
        measurement = MeasuredQuantity(
            measurement_id="unseen-minus-control-compression",
            quantity_kind_id="prediction_range_compression_difference",
            value=metrics.unseen_minus_control_delta,
            unit_ucum="1",
            uncertainty=MeasurementUncertainty(
                kind=UncertaintyKind.CONFIDENCE_INTERVAL,
                lower=metrics.delta_ci_lower,
                upper=metrics.delta_ci_upper,
                coverage_probability=metrics.confidence_level,
                method_sha256=hashlib.sha256(b"chemical_system_cluster_bootstrap_v1").hexdigest(),
            ),
            sample_count=min(
                result.split.unseen_chemical_systems,
                result.split.control_chemical_systems,
            ),
            raw_artifact_ids=("result",),
        )
        context = ObservationContext(
            measurement_method_id="materials.band_gap.range_compression.v2",
            conditions=(
                ObservationCondition(
                    condition_id="dataset",
                    categorical_value=result.dataset.dataset_ref,
                ),
                ObservationCondition(
                    condition_id="model",
                    categorical_value=result.fitted_model_identity_sha256,
                ),
                ObservationCondition(
                    condition_id="partition-seed",
                    quantity_kind_id="random_seed",
                    numeric_value=float(result.split.partition_seed),
                    unit_ucum="1",
                ),
            ),
            batch_id=result.dataset.logical_rows_sha256,
        )
        return ParsedObservationPayload(
            scientific_outcome=_scientific_outcome(result.outcome_id),
            measurements=(measurement,),
            context=context,
            parser_warnings=(
                "computational_model_diagnostic_not_physical_measurement",
                f"frozen_outcome:{result.outcome_id.value}",
            ),
        )


__all__ = [
    "TYPED_RANGE_COMPRESSION_PARSER_PRINCIPAL_SHA256",
    "TypedRangeCompressionParser",
    "typed_range_compression_parser_implementation_sha256",
]
