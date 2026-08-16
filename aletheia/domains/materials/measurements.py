"""Materials measurement audit with conservative, identity-aware pooling.

The audit converts only units and conditions frozen by policy, retains failed and
duplicate records, separates incompatible condition strata, excludes conflicting
same-sample repeats, and reports unavailable variability instead of manufacturing a
noise estimate from insufficient data.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.observations import (
    MeasuredQuantity,
    ObservationContext,
    UncertaintyKind,
)
from aletheia.domains.materials.identity import (
    LicensedSourceArtifact,
    MaterialIdentityLevel,
    MaterialRecordIdentity,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class MaterialMeasurementStatus(str, Enum):
    FAILED = "failed"
    INVALIDATED = "invalidated"
    PARTIAL = "partial"
    SUCCEEDED = "succeeded"


class ConditionValueKind(str, Enum):
    CATEGORICAL = "categorical"
    QUANTITATIVE = "quantitative"


class MeasurementAuditDisposition(str, Enum):
    CLEAN = "clean"
    REJECTED_NO_ELIGIBLE_MEASUREMENTS = "rejected_no_eligible_measurements"
    REQUIRES_REVIEW_CONFLICT = "requires_review_conflict"
    VALID_CONDITION_STRATIFIED = "valid_condition_stratified"
    VALID_WITH_EXCLUSIONS = "valid_with_exclusions"


class VariabilityLevel(str, Enum):
    BETWEEN_BATCH = "between_batch"
    BETWEEN_SOURCE = "between_source"
    WITHIN_SAMPLE = "within_sample"


class EstimateAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class LinearUnitConversion(FrozenModel):
    schema_version: Literal[1] = 1
    source_ucum_code: str = Field(pattern=r"^[!-~]{1,64}$")
    canonical_ucum_code: str = Field(pattern=r"^[!-~]{1,64}$")
    scale: float
    offset: float = 0.0

    @model_validator(mode="after")
    def _conversion_is_finite(self) -> "LinearUnitConversion":
        if not math.isfinite(self.scale) or self.scale == 0 or not math.isfinite(self.offset):
            raise ValueError("unit conversion scale and offset must be finite with nonzero scale")
        return self

    def convert(self, value: float) -> float:
        return value * self.scale + self.offset


class MeasurementConditionContract(FrozenModel):
    schema_version: Literal[1] = 1
    condition_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    value_kind: ConditionValueKind
    quantity_kind_id: str | None = Field(default=None, min_length=1, max_length=256)
    canonical_ucum_code: str | None = Field(default=None, pattern=r"^[!-~]{1,64}$")
    unit_conversions: tuple[LinearUnitConversion, ...] = ()
    compatibility_tolerance: float | None = Field(default=None, ge=0)
    allowed_categories: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _contract_matches_value_kind(self) -> "MeasurementConditionContract":
        if self.value_kind is ConditionValueKind.QUANTITATIVE:
            if (
                self.quantity_kind_id is None
                or self.canonical_ucum_code is None
                or not self.unit_conversions
                or self.compatibility_tolerance is None
                or self.allowed_categories
            ):
                raise ValueError("quantitative condition contract is incomplete")
            sources = tuple(item.source_ucum_code for item in self.unit_conversions)
            if sources != tuple(sorted(set(sources))):
                raise ValueError("condition unit conversions must have unique sorted sources")
            if any(
                item.canonical_ucum_code != self.canonical_ucum_code
                for item in self.unit_conversions
            ):
                raise ValueError("condition conversions must target the canonical unit")
            canonical = next(
                (
                    item
                    for item in self.unit_conversions
                    if item.source_ucum_code == self.canonical_ucum_code
                ),
                None,
            )
            if canonical is None or canonical.scale != 1 or canonical.offset != 0:
                raise ValueError("condition canonical unit requires an identity conversion")
        elif (
            self.quantity_kind_id is not None
            or self.canonical_ucum_code is not None
            or self.unit_conversions
            or self.compatibility_tolerance is not None
            or not self.allowed_categories
        ):
            raise ValueError("categorical condition contract has quantitative fields or no values")
        if self.allowed_categories != tuple(sorted(set(self.allowed_categories))):
            raise ValueError("allowed condition categories must be unique and sorted")
        return self


class MaterialMeasurementAuditPolicy(FrozenModel):
    schema_name: Literal["aletheia.material_measurement_audit_policy"] = (
        "aletheia.material_measurement_audit_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    property_id: str = Field(min_length=1, max_length=256)
    quantity_kind_id: str = Field(min_length=1, max_length=256)
    canonical_ucum_code: str = Field(pattern=r"^[!-~]{1,64}$")
    quantity_unit_conversions: tuple[LinearUnitConversion, ...] = Field(min_length=1)
    required_conditions: tuple[MeasurementConditionContract, ...] = Field(min_length=1)
    allowed_measurement_method_ids: tuple[str, ...] = Field(min_length=1)
    required_identity_levels: tuple[MaterialIdentityLevel, ...] = Field(min_length=1)
    pooling_identity_level: Literal[
        MaterialIdentityLevel.FORMULA,
        MaterialIdentityLevel.STRUCTURE,
    ]
    allow_not_quantified_uncertainty: bool = False
    conflict_absolute_tolerance: float = Field(ge=0)
    conflict_combined_standard_uncertainty_multiplier: float = Field(default=3.0, gt=0)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _policy_sets_and_units_are_canonical(self) -> "MaterialMeasurementAuditPolicy":
        source_units = tuple(item.source_ucum_code for item in self.quantity_unit_conversions)
        if source_units != tuple(sorted(set(source_units))):
            raise ValueError("quantity unit conversions must have unique sorted sources")
        if any(
            item.canonical_ucum_code != self.canonical_ucum_code
            for item in self.quantity_unit_conversions
        ):
            raise ValueError("quantity conversions must target the canonical unit")
        canonical = next(
            (
                item
                for item in self.quantity_unit_conversions
                if item.source_ucum_code == self.canonical_ucum_code
            ),
            None,
        )
        if canonical is None or canonical.scale != 1 or canonical.offset != 0:
            raise ValueError("quantity canonical unit requires an identity conversion")
        condition_ids = tuple(item.condition_id for item in self.required_conditions)
        if condition_ids != tuple(sorted(set(condition_ids))):
            raise ValueError("required conditions must have unique sorted IDs")
        if self.allowed_measurement_method_ids != tuple(
            sorted(set(self.allowed_measurement_method_ids))
        ):
            raise ValueError("measurement methods must be unique and sorted")
        expected_levels = tuple(sorted(set(self.required_identity_levels), key=lambda x: x.value))
        if self.required_identity_levels != expected_levels:
            raise ValueError("required material identity levels must be unique and sorted")
        if self.pooling_identity_level not in self.required_identity_levels:
            raise ValueError("pooling identity must be a required material identity level")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class MaterialMeasurementRecord(FrozenModel):
    schema_name: Literal["aletheia.material_measurement_record"] = (
        "aletheia.material_measurement_record"
    )
    schema_version: Literal[1] = 1
    measurement_record_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    material: MaterialRecordIdentity
    property_id: str = Field(min_length=1, max_length=256)
    source_group_id: str = Field(min_length=1, max_length=512)
    measurement_source: LicensedSourceArtifact
    status: MaterialMeasurementStatus
    quantity: MeasuredQuantity | None = None
    context: ObservationContext | None = None
    failure_code: str | None = Field(default=None, min_length=1, max_length=256)
    measured_at: AwareDatetime

    @model_validator(mode="after")
    def _record_retains_status_and_identity(self) -> "MaterialMeasurementRecord":
        if self.source_group_id != self.source_group_id.strip():
            raise ValueError("measurement source group must be canonical")
        if self.status is MaterialMeasurementStatus.SUCCEEDED:
            if self.quantity is None or self.context is None or self.failure_code is not None:
                raise ValueError("successful measurement requires quantity/context and no failure")
            if self.measurement_source.artifact_id not in self.quantity.raw_artifact_ids:
                raise ValueError(
                    "measurement quantity must bind its exact licensed source artifact"
                )
            sample_id = self.material.sample.sample_id if self.material.sample is not None else None
            batch_id = self.material.batch.batch_id if self.material.batch is not None else None
            if self.context.sample_id != sample_id or self.context.batch_id != batch_id:
                raise ValueError("measurement context changed sample or batch identity")
        elif self.quantity is not None or self.context is not None or self.failure_code is None:
            raise ValueError("non-success measurement retains failure but cannot expose a value")
        return self

    @property
    def measurement_record_sha256(self) -> str:
        return content_sha256(self)


class CanonicalConditionValue(FrozenModel):
    schema_version: Literal[1] = 1
    condition_id: str
    numeric_value: float | None = None
    unit_ucum: str | None = None
    categorical_value: str | None = None

    @model_validator(mode="after")
    def _has_one_value(self) -> "CanonicalConditionValue":
        numeric = self.numeric_value is not None
        categorical = self.categorical_value is not None
        if numeric == categorical:
            raise ValueError("canonical condition requires exactly one value kind")
        if numeric:
            if self.unit_ucum is None or not math.isfinite(self.numeric_value):  # type: ignore[arg-type]
                raise ValueError("canonical quantitative condition is incomplete")
        elif self.unit_ucum is not None:
            raise ValueError("canonical categorical condition cannot have a unit")
        return self


class CanonicalMeasurementProjection(FrozenModel):
    schema_version: Literal[1] = 1
    measurement_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_record_id: str
    pooling_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    formula_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    structure_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    sample_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    batch_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_group_id: str
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)
    value: float
    unit_ucum: str
    standard_uncertainty: float | None = Field(default=None, ge=0)
    conditions: tuple[CanonicalConditionValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _projection_is_canonical(self) -> "CanonicalMeasurementProjection":
        if not math.isfinite(self.value):
            raise ValueError("canonical measurement value must be finite")
        ids = tuple(item.condition_id for item in self.conditions)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("canonical conditions must have unique sorted IDs")
        return self


class MeasurementExclusion(FrozenModel):
    schema_version: Literal[1] = 1
    measurement_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reasons_are_canonical(self) -> "MeasurementExclusion":
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("measurement exclusion reasons must be unique and sorted")
        return self


class ExactDuplicateMeasurement(FrozenModel):
    schema_version: Literal[1] = 1
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)
    retained_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    excluded_record_sha256s: tuple[str, ...] = Field(min_length=1)


class ConflictingProvenanceProjection(FrozenModel):
    schema_version: Literal[1] = 1
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_record_sha256s: tuple[str, ...] = Field(min_length=2)
    projection_sha256s: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _identities_are_canonical(self) -> "ConflictingProvenanceProjection":
        if self.measurement_record_sha256s != tuple(sorted(set(self.measurement_record_sha256s))):
            raise ValueError("provenance-conflict records must be unique and sorted")
        if self.projection_sha256s != tuple(sorted(set(self.projection_sha256s))):
            raise ValueError("provenance conflict requires distinct sorted projections")
        return self


class SameSampleRepeat(FrozenModel):
    schema_version: Literal[1] = 1
    sample_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_record_sha256s: tuple[str, ...] = Field(min_length=2)


class IncompatibleMeasurementConditions(FrozenModel):
    schema_version: Literal[1] = 1
    left_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    right_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    incompatible_condition_ids: tuple[str, ...] = Field(min_length=1)


class ConflictingSampleMeasurements(FrozenModel):
    schema_version: Literal[1] = 1
    sample_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    left_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    right_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    absolute_difference: float = Field(gt=0)
    conflict_threshold: float = Field(ge=0)


class VariabilityEstimate(FrozenModel):
    schema_version: Literal[1] = 1
    level: VariabilityLevel
    condition_stratum_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_record_sha256s: tuple[str, ...]
    measurement_count: int = Field(ge=0)
    group_count: int = Field(ge=0)
    availability: EstimateAvailability
    standard_deviation: float | None = Field(default=None, ge=0)
    variance: float | None = Field(default=None, ge=0)
    median_standard_uncertainty: float | None = Field(default=None, ge=0)
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _availability_shape_is_closed(self) -> "VariabilityEstimate":
        if self.measurement_count != len(self.measurement_record_sha256s):
            raise ValueError("variability measurement count is not derived")
        if self.measurement_record_sha256s != tuple(sorted(set(self.measurement_record_sha256s))):
            raise ValueError("variability record identities must be unique and sorted")
        if self.availability is EstimateAvailability.AVAILABLE:
            if (
                self.standard_deviation is None
                or self.variance is None
                or self.unavailable_reason is not None
                or self.group_count < 1
            ):
                raise ValueError("available variability estimate is incomplete")
            if not math.isclose(
                self.variance,
                self.standard_deviation**2,
                rel_tol=1e-12,
                abs_tol=1e-15,
            ):
                raise ValueError("variability variance and standard deviation disagree")
        elif (
            self.standard_deviation is not None
            or self.variance is not None
            or self.unavailable_reason is None
        ):
            raise ValueError("unavailable variability estimate must state only its reason")
        return self


class _DerivedMeasurementAudit(FrozenModel):
    projections: tuple[CanonicalMeasurementProjection, ...]
    exclusions: tuple[MeasurementExclusion, ...]
    exact_duplicates: tuple[ExactDuplicateMeasurement, ...]
    provenance_conflicts: tuple[ConflictingProvenanceProjection, ...]
    same_sample_repeats: tuple[SameSampleRepeat, ...]
    incompatible_conditions: tuple[IncompatibleMeasurementConditions, ...]
    conflicts: tuple[ConflictingSampleMeasurements, ...]
    eligible_record_sha256s: tuple[str, ...]
    variability: tuple[VariabilityEstimate, ...]
    disposition: MeasurementAuditDisposition


class MaterialMeasurementAudit(FrozenModel):
    schema_name: Literal["aletheia.material_measurement_audit"] = (
        "aletheia.material_measurement_audit"
    )
    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    policy: MaterialMeasurementAuditPolicy
    records: tuple[MaterialMeasurementRecord, ...] = Field(min_length=1)
    projections: tuple[CanonicalMeasurementProjection, ...]
    exclusions: tuple[MeasurementExclusion, ...]
    exact_duplicates: tuple[ExactDuplicateMeasurement, ...]
    provenance_conflicts: tuple[ConflictingProvenanceProjection, ...]
    same_sample_repeats: tuple[SameSampleRepeat, ...]
    incompatible_conditions: tuple[IncompatibleMeasurementConditions, ...]
    conflicts: tuple[ConflictingSampleMeasurements, ...]
    eligible_record_sha256s: tuple[str, ...]
    variability: tuple[VariabilityEstimate, ...] = Field(min_length=3)
    disposition: MeasurementAuditDisposition
    audited_at: AwareDatetime

    @model_validator(mode="after")
    def _audit_is_mechanically_replayed(self) -> "MaterialMeasurementAudit":
        record_hashes = tuple(item.measurement_record_sha256 for item in self.records)
        if record_hashes != tuple(sorted(set(record_hashes))):
            raise ValueError("measurement audit records must have unique sorted identities")
        derived = _derive_measurement_audit(records=self.records, policy=self.policy)
        for field_name in _DerivedMeasurementAudit.model_fields:
            if getattr(self, field_name) != getattr(derived, field_name):
                raise ValueError(f"measurement audit changed derived field {field_name}")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


def _find_conversion(
    conversions: tuple[LinearUnitConversion, ...], unit: str
) -> LinearUnitConversion | None:
    return next((item for item in conversions if item.source_ucum_code == unit), None)


def _standard_uncertainty(quantity: MeasuredQuantity, scale: float) -> float | None:
    uncertainty = quantity.uncertainty
    if uncertainty.kind is UncertaintyKind.STANDARD:
        return abs(scale) * uncertainty.value  # type: ignore[operator]
    if uncertainty.kind is UncertaintyKind.EXPANDED:
        return abs(scale) * uncertainty.value / uncertainty.coverage_factor  # type: ignore[operator]
    return None


def _canonical_conditions(
    context: ObservationContext,
    policy: MaterialMeasurementAuditPolicy,
) -> tuple[tuple[CanonicalConditionValue, ...] | None, tuple[str, ...]]:
    observed = {item.condition_id: item for item in context.conditions}
    output: list[CanonicalConditionValue] = []
    reasons: set[str] = set()
    for contract in policy.required_conditions:
        condition = observed.get(contract.condition_id)
        if condition is None:
            reasons.add(f"condition_missing:{contract.condition_id}")
            continue
        if contract.value_kind is ConditionValueKind.CATEGORICAL:
            if (
                condition.categorical_value is None
                or condition.categorical_value not in contract.allowed_categories
            ):
                reasons.add(f"condition_invalid:{contract.condition_id}")
                continue
            output.append(
                CanonicalConditionValue(
                    condition_id=contract.condition_id,
                    categorical_value=condition.categorical_value,
                )
            )
            continue
        if (
            condition.numeric_value is None
            or condition.quantity_kind_id != contract.quantity_kind_id
            or condition.unit_ucum is None
        ):
            reasons.add(f"condition_invalid:{contract.condition_id}")
            continue
        conversion = _find_conversion(contract.unit_conversions, condition.unit_ucum)
        if conversion is None:
            reasons.add(f"condition_unit_unsupported:{contract.condition_id}")
            continue
        output.append(
            CanonicalConditionValue(
                condition_id=contract.condition_id,
                numeric_value=conversion.convert(condition.numeric_value),
                unit_ucum=contract.canonical_ucum_code,
            )
        )
    if reasons:
        return None, tuple(sorted(reasons))
    return tuple(sorted(output, key=lambda item: item.condition_id)), ()


def _project_record(
    record: MaterialMeasurementRecord,
    policy: MaterialMeasurementAuditPolicy,
) -> tuple[CanonicalMeasurementProjection | None, tuple[str, ...]]:
    reasons: set[str] = set()
    if record.status is not MaterialMeasurementStatus.SUCCEEDED:
        reasons.add(f"measurement_status:{record.status.value}")
        return None, tuple(sorted(reasons))
    if record.property_id != policy.property_id:
        reasons.add("property_mismatch")
    for level in policy.required_identity_levels:
        if record.material.identity_at(level) is None:
            reasons.add(f"identity_missing:{level.value}")
    quantity = record.quantity
    context = record.context
    if quantity is None or context is None:  # guarded by MaterialMeasurementRecord
        reasons.add("measurement_payload_missing")
        return None, tuple(sorted(reasons))
    if quantity.quantity_kind_id != policy.quantity_kind_id:
        reasons.add("quantity_kind_mismatch")
    conversion = _find_conversion(policy.quantity_unit_conversions, quantity.unit_ucum)
    if conversion is None:
        reasons.add("quantity_unit_unsupported")
    if (
        quantity.uncertainty.kind is UncertaintyKind.NOT_QUANTIFIED
        and not policy.allow_not_quantified_uncertainty
    ):
        reasons.add("uncertainty_not_quantified")
    if context.measurement_method_id not in policy.allowed_measurement_method_ids:
        reasons.add("measurement_method_unsupported")
    conditions, condition_reasons = _canonical_conditions(context, policy)
    reasons.update(condition_reasons)
    pooling_identity = record.material.identity_at(policy.pooling_identity_level)
    if pooling_identity is None:
        reasons.add(f"identity_missing:{policy.pooling_identity_level.value}")
    if reasons or conversion is None or conditions is None or pooling_identity is None:
        return None, tuple(sorted(reasons))
    sample = record.material.identity_at(MaterialIdentityLevel.SAMPLE)
    batch = record.material.identity_at(MaterialIdentityLevel.BATCH)
    structure = record.material.identity_at(MaterialIdentityLevel.STRUCTURE)
    provenance = content_sha256(
        {
            "measurement_source_receipt_sha256": record.measurement_source.receipt_sha256,
            "measurement_id": quantity.measurement_id,
            "raw_artifact_ids": list(quantity.raw_artifact_ids),
            "sample_identity_sha256": sample,
            "batch_identity_sha256": batch,
        }
    )
    value = conversion.convert(quantity.value)
    if not math.isfinite(value):
        return None, ("converted_value_nonfinite",)
    return (
        CanonicalMeasurementProjection(
            measurement_record_sha256=record.measurement_record_sha256,
            measurement_record_id=record.measurement_record_id,
            pooling_identity_sha256=pooling_identity,
            formula_identity_sha256=record.material.formula.formula_identity_sha256,
            structure_identity_sha256=structure,
            sample_identity_sha256=sample,
            batch_identity_sha256=batch,
            source_group_id=record.source_group_id,
            provenance_sha256=provenance,
            value=value,
            unit_ucum=policy.canonical_ucum_code,
            standard_uncertainty=_standard_uncertainty(quantity, conversion.scale),
            conditions=conditions,
        ),
        (),
    )


def _condition_incompatibilities(
    left: CanonicalMeasurementProjection,
    right: CanonicalMeasurementProjection,
    policy: MaterialMeasurementAuditPolicy,
) -> tuple[str, ...]:
    left_conditions = {item.condition_id: item for item in left.conditions}
    right_conditions = {item.condition_id: item for item in right.conditions}
    incompatible = []
    for contract in policy.required_conditions:
        a = left_conditions[contract.condition_id]
        b = right_conditions[contract.condition_id]
        if contract.value_kind is ConditionValueKind.CATEGORICAL:
            if a.categorical_value != b.categorical_value:
                incompatible.append(contract.condition_id)
        elif abs(a.numeric_value - b.numeric_value) > contract.compatibility_tolerance:  # type: ignore[operator]
            incompatible.append(contract.condition_id)
    return tuple(incompatible)


def _condition_strata(
    projections: tuple[CanonicalMeasurementProjection, ...],
    policy: MaterialMeasurementAuditPolicy,
) -> list[list[CanonicalMeasurementProjection]]:
    """Deterministic conservative clique partition; every pooled pair is compatible."""

    strata: list[list[CanonicalMeasurementProjection]] = []
    for projection in projections:
        for stratum in strata:
            if projection.pooling_identity_sha256 == stratum[0].pooling_identity_sha256 and all(
                not _condition_incompatibilities(projection, member, policy) for member in stratum
            ):
                stratum.append(projection)
                break
        else:
            strata.append([projection])
    return strata


def _median_uncertainty(projections: list[CanonicalMeasurementProjection]) -> float | None:
    values = [
        item.standard_uncertainty for item in projections if item.standard_uncertainty is not None
    ]
    return None if not values else float(statistics.median(values))


def _projection_content_sha256(projection: CanonicalMeasurementProjection) -> str:
    """Scientific projection under one provenance key; retry/display IDs are excluded."""

    return content_sha256(
        {
            "pooling_identity_sha256": projection.pooling_identity_sha256,
            "formula_identity_sha256": projection.formula_identity_sha256,
            "structure_identity_sha256": projection.structure_identity_sha256,
            "sample_identity_sha256": projection.sample_identity_sha256,
            "batch_identity_sha256": projection.batch_identity_sha256,
            "source_group_id": projection.source_group_id,
            "value": projection.value,
            "unit_ucum": projection.unit_ucum,
            "standard_uncertainty": projection.standard_uncertainty,
            "conditions": [item.model_dump(mode="json") for item in projection.conditions],
        }
    )


def _variability_for_stratum(
    projections: list[CanonicalMeasurementProjection],
) -> tuple[VariabilityEstimate, ...]:
    record_hashes = tuple(sorted(item.measurement_record_sha256 for item in projections))
    stratum_hash = content_sha256(
        {
            "pooling_identity_sha256": projections[0].pooling_identity_sha256,
            "measurement_record_sha256s": list(record_hashes),
        }
    )
    noise = _median_uncertainty(projections)
    estimates: list[VariabilityEstimate] = []

    sample_groups: dict[str, list[float]] = {}
    for item in projections:
        if item.sample_identity_sha256 is not None:
            sample_groups.setdefault(item.sample_identity_sha256, []).append(item.value)
    repeated = [values for values in sample_groups.values() if len(values) >= 2]
    degrees_freedom = sum(len(values) - 1 for values in repeated)
    if degrees_freedom:
        sum_squares = sum(
            sum((value - statistics.fmean(values)) ** 2 for value in values) for values in repeated
        )
        variance = sum_squares / degrees_freedom
        estimates.append(
            VariabilityEstimate(
                level=VariabilityLevel.WITHIN_SAMPLE,
                condition_stratum_sha256=stratum_hash,
                measurement_record_sha256s=record_hashes,
                measurement_count=len(record_hashes),
                group_count=len(repeated),
                availability=EstimateAvailability.AVAILABLE,
                standard_deviation=math.sqrt(variance),
                variance=variance,
                median_standard_uncertainty=noise,
            )
        )
    else:
        estimates.append(
            VariabilityEstimate(
                level=VariabilityLevel.WITHIN_SAMPLE,
                condition_stratum_sha256=stratum_hash,
                measurement_record_sha256s=record_hashes,
                measurement_count=len(record_hashes),
                group_count=len(sample_groups),
                availability=EstimateAvailability.UNAVAILABLE,
                median_standard_uncertainty=noise,
                unavailable_reason="fewer_than_two_compatible_measurements_per_sample",
            )
        )

    for level, attribute, reason in (
        (
            VariabilityLevel.BETWEEN_BATCH,
            "batch_identity_sha256",
            "fewer_than_two_batches",
        ),
        (VariabilityLevel.BETWEEN_SOURCE, "source_group_id", "fewer_than_two_sources"),
    ):
        grouped: dict[str, list[CanonicalMeasurementProjection]] = {}
        for item in projections:
            identity = getattr(item, attribute)
            if identity is not None:
                grouped.setdefault(identity, []).append(item)
        means = []
        for _, members in sorted(grouped.items()):
            by_sample: dict[str, list[float]] = {}
            for member in members:
                sample = member.sample_identity_sha256 or member.measurement_record_sha256
                by_sample.setdefault(sample, []).append(member.value)
            sample_means = [statistics.fmean(values) for _, values in sorted(by_sample.items())]
            means.append(statistics.fmean(sample_means))
        if len(means) >= 2:
            standard_deviation = float(statistics.stdev(means))
            estimates.append(
                VariabilityEstimate(
                    level=level,
                    condition_stratum_sha256=stratum_hash,
                    measurement_record_sha256s=record_hashes,
                    measurement_count=len(record_hashes),
                    group_count=len(means),
                    availability=EstimateAvailability.AVAILABLE,
                    standard_deviation=standard_deviation,
                    variance=standard_deviation**2,
                    median_standard_uncertainty=noise,
                )
            )
        else:
            estimates.append(
                VariabilityEstimate(
                    level=level,
                    condition_stratum_sha256=stratum_hash,
                    measurement_record_sha256s=record_hashes,
                    measurement_count=len(record_hashes),
                    group_count=len(means),
                    availability=EstimateAvailability.UNAVAILABLE,
                    median_standard_uncertainty=noise,
                    unavailable_reason=reason,
                )
            )
    return tuple(estimates)


def _empty_variability(policy: MaterialMeasurementAuditPolicy) -> tuple[VariabilityEstimate, ...]:
    stratum_hash = content_sha256(
        {"policy_sha256": policy.policy_sha256, "eligible_measurements": []}
    )
    return tuple(
        VariabilityEstimate(
            level=level,
            condition_stratum_sha256=stratum_hash,
            measurement_record_sha256s=(),
            measurement_count=0,
            group_count=0,
            availability=EstimateAvailability.UNAVAILABLE,
            unavailable_reason="no_eligible_measurements",
        )
        for level in sorted(VariabilityLevel, key=lambda item: item.value)
    )


def _derive_measurement_audit(
    *,
    records: tuple[MaterialMeasurementRecord, ...],
    policy: MaterialMeasurementAuditPolicy,
) -> _DerivedMeasurementAudit:
    exclusions: dict[str, set[str]] = {}
    projections: list[CanonicalMeasurementProjection] = []
    for record in records:
        projection, reasons = _project_record(record, policy)
        if projection is None:
            exclusions.setdefault(record.measurement_record_sha256, set()).update(reasons)
        else:
            projections.append(projection)
    projections.sort(key=lambda item: item.measurement_record_sha256)

    by_provenance: dict[str, list[CanonicalMeasurementProjection]] = {}
    for projection in projections:
        by_provenance.setdefault(projection.provenance_sha256, []).append(projection)
    duplicates: list[ExactDuplicateMeasurement] = []
    provenance_conflicts: list[ConflictingProvenanceProjection] = []
    duplicate_excluded: set[str] = set()
    for provenance, members in sorted(by_provenance.items()):
        if len(members) < 2:
            continue
        hashes = sorted(item.measurement_record_sha256 for item in members)
        projection_hashes = tuple(sorted({_projection_content_sha256(item) for item in members}))
        if len(projection_hashes) > 1:
            provenance_conflicts.append(
                ConflictingProvenanceProjection(
                    provenance_sha256=provenance,
                    measurement_record_sha256s=tuple(hashes),
                    projection_sha256s=projection_hashes,
                )
            )
            for record_hash in hashes:
                exclusions.setdefault(record_hash, set()).add(
                    "conflicting_projection_for_same_provenance"
                )
                duplicate_excluded.add(record_hash)
            continue
        excluded = tuple(hashes[1:])
        duplicates.append(
            ExactDuplicateMeasurement(
                provenance_sha256=provenance,
                retained_record_sha256=hashes[0],
                excluded_record_sha256s=excluded,
            )
        )
        for record_hash in excluded:
            exclusions.setdefault(record_hash, set()).add("exact_duplicate")
            duplicate_excluded.add(record_hash)

    nonduplicates = tuple(
        item for item in projections if item.measurement_record_sha256 not in duplicate_excluded
    )
    incompatibilities: list[IncompatibleMeasurementConditions] = []
    conflicts: list[ConflictingSampleMeasurements] = []
    for index, left in enumerate(nonduplicates):
        for right in nonduplicates[index + 1 :]:
            if left.pooling_identity_sha256 != right.pooling_identity_sha256:
                continue
            incompatible = _condition_incompatibilities(left, right, policy)
            if incompatible:
                incompatibilities.append(
                    IncompatibleMeasurementConditions(
                        left_record_sha256=left.measurement_record_sha256,
                        right_record_sha256=right.measurement_record_sha256,
                        incompatible_condition_ids=incompatible,
                    )
                )
                continue
            if (
                left.sample_identity_sha256 is None
                or left.sample_identity_sha256 != right.sample_identity_sha256
            ):
                continue
            combined_uncertainty = 0.0
            if left.standard_uncertainty is not None and right.standard_uncertainty is not None:
                combined_uncertainty = math.hypot(
                    left.standard_uncertainty, right.standard_uncertainty
                )
            threshold = max(
                policy.conflict_absolute_tolerance,
                policy.conflict_combined_standard_uncertainty_multiplier * combined_uncertainty,
            )
            difference = abs(left.value - right.value)
            if difference > threshold:
                conflicts.append(
                    ConflictingSampleMeasurements(
                        sample_identity_sha256=left.sample_identity_sha256,
                        left_record_sha256=left.measurement_record_sha256,
                        right_record_sha256=right.measurement_record_sha256,
                        absolute_difference=difference,
                        conflict_threshold=threshold,
                    )
                )
                exclusions.setdefault(left.measurement_record_sha256, set()).add(
                    "conflicting_same_sample_repeat"
                )
                exclusions.setdefault(right.measurement_record_sha256, set()).add(
                    "conflicting_same_sample_repeat"
                )

    repeats: list[SameSampleRepeat] = []
    by_sample: dict[str, list[str]] = {}
    for projection in nonduplicates:
        if projection.sample_identity_sha256 is not None:
            by_sample.setdefault(projection.sample_identity_sha256, []).append(
                projection.measurement_record_sha256
            )
    for sample, hashes in sorted(by_sample.items()):
        if len(hashes) >= 2:
            repeats.append(
                SameSampleRepeat(
                    sample_identity_sha256=sample,
                    measurement_record_sha256s=tuple(sorted(hashes)),
                )
            )

    eligible = tuple(
        item for item in nonduplicates if item.measurement_record_sha256 not in exclusions
    )
    eligible_hashes = tuple(item.measurement_record_sha256 for item in eligible)
    variability = (
        tuple(
            estimate
            for stratum in _condition_strata(eligible, policy)
            for estimate in _variability_for_stratum(stratum)
        )
        if eligible
        else _empty_variability(policy)
    )
    if not eligible:
        disposition = MeasurementAuditDisposition.REJECTED_NO_ELIGIBLE_MEASUREMENTS
    elif provenance_conflicts or conflicts:
        disposition = MeasurementAuditDisposition.REQUIRES_REVIEW_CONFLICT
    elif exclusions:
        disposition = MeasurementAuditDisposition.VALID_WITH_EXCLUSIONS
    elif incompatibilities:
        disposition = MeasurementAuditDisposition.VALID_CONDITION_STRATIFIED
    else:
        disposition = MeasurementAuditDisposition.CLEAN
    exclusion_models = tuple(
        MeasurementExclusion(
            measurement_record_sha256=record_hash,
            reason_codes=tuple(sorted(reasons)),
        )
        for record_hash, reasons in sorted(exclusions.items())
    )
    return _DerivedMeasurementAudit(
        projections=tuple(projections),
        exclusions=exclusion_models,
        exact_duplicates=tuple(duplicates),
        provenance_conflicts=tuple(provenance_conflicts),
        same_sample_repeats=tuple(repeats),
        incompatible_conditions=tuple(incompatibilities),
        conflicts=tuple(conflicts),
        eligible_record_sha256s=eligible_hashes,
        variability=variability,
        disposition=disposition,
    )


def audit_material_measurements(
    *,
    audit_id: str,
    policy: MaterialMeasurementAuditPolicy,
    records: tuple[MaterialMeasurementRecord, ...],
    audited_at: datetime,
) -> MaterialMeasurementAudit:
    """Replayable audit; records are sorted and every derived finding is recomputed."""

    ordered = tuple(sorted(records, key=lambda item: item.measurement_record_sha256))
    if len({item.measurement_record_sha256 for item in ordered}) != len(ordered):
        raise ValueError("identical measurement records cannot be submitted twice")
    derived = _derive_measurement_audit(records=ordered, policy=policy)
    return MaterialMeasurementAudit(
        audit_id=audit_id,
        policy=policy,
        records=ordered,
        projections=derived.projections,
        exclusions=derived.exclusions,
        exact_duplicates=derived.exact_duplicates,
        provenance_conflicts=derived.provenance_conflicts,
        same_sample_repeats=derived.same_sample_repeats,
        incompatible_conditions=derived.incompatible_conditions,
        conflicts=derived.conflicts,
        eligible_record_sha256s=derived.eligible_record_sha256s,
        variability=derived.variability,
        disposition=derived.disposition,
        audited_at=audited_at,
    )


__all__ = [
    "CanonicalConditionValue",
    "CanonicalMeasurementProjection",
    "ConditionValueKind",
    "ConflictingSampleMeasurements",
    "ConflictingProvenanceProjection",
    "EstimateAvailability",
    "ExactDuplicateMeasurement",
    "IncompatibleMeasurementConditions",
    "LinearUnitConversion",
    "MaterialMeasurementAudit",
    "MaterialMeasurementAuditPolicy",
    "MaterialMeasurementRecord",
    "MaterialMeasurementStatus",
    "MeasurementAuditDisposition",
    "MeasurementConditionContract",
    "MeasurementExclusion",
    "SameSampleRepeat",
    "VariabilityEstimate",
    "VariabilityLevel",
    "audit_material_measurements",
]
