"""Dataset-level materials identity coverage audit.

This adapter does not invent sample, batch, structure, uncertainty, or protocol
metadata.  It is intended to tell a planner whether a tabular materials dataset can
support a composition benchmark, a measurement audit, or a structure-aware claim.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.domains.materials.identity import LicensedSourceArtifact, normalize_formula
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


class DatasetCoverageStatus(str, Enum):
    ABSENT = "absent"
    COLUMN_COMPLETE = "column_complete"
    COLUMN_PARTIAL = "column_partial"
    DATASET_METADATA_ONLY = "dataset_metadata_only"


class DatasetIdentityAuditDisposition(str, Enum):
    COMPOSITION_BENCHMARK_ONLY = "composition_benchmark_only"
    REJECTED_FORMULA_OR_TARGET_INVALID = "rejected_formula_or_target_invalid"


class MaterialsIdentityColumnMap(FrozenModel):
    schema_name: Literal["aletheia.materials_identity_column_map"] = (
        "aletheia.materials_identity_column_map"
    )
    schema_version: Literal[1] = 1
    formula_column: str = Field(min_length=1, max_length=256)
    property_value_column: str = Field(min_length=1, max_length=256)
    structure_column: str | None = Field(default=None, min_length=1, max_length=256)
    sample_id_column: str | None = Field(default=None, min_length=1, max_length=256)
    batch_id_column: str | None = Field(default=None, min_length=1, max_length=256)
    property_unit_column: str | None = Field(default=None, min_length=1, max_length=256)
    uncertainty_column: str | None = Field(default=None, min_length=1, max_length=256)
    measurement_method_column: str | None = Field(default=None, min_length=1, max_length=256)
    measurement_conditions_column: str | None = Field(default=None, min_length=1, max_length=256)
    row_source_column: str | None = Field(default=None, min_length=1, max_length=256)
    dataset_level_property_unit_ucum: str | None = Field(default=None, pattern=r"^[!-~]{1,64}$")

    @model_validator(mode="after")
    def _columns_are_distinct(self) -> "MaterialsIdentityColumnMap":
        columns = tuple(
            item
            for item in (
                self.formula_column,
                self.property_value_column,
                self.structure_column,
                self.sample_id_column,
                self.batch_id_column,
                self.property_unit_column,
                self.uncertainty_column,
                self.measurement_method_column,
                self.measurement_conditions_column,
                self.row_source_column,
            )
            if item is not None
        )
        if len(columns) != len(set(columns)):
            raise ValueError("materials identity column roles must be distinct")
        return self


class DatasetFieldCoverage(FrozenModel):
    schema_version: Literal[1] = 1
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    status: DatasetCoverageStatus
    column_name: str | None = None
    non_null_rows: int = Field(ge=0)
    total_rows: int = Field(ge=1)
    unique_non_null_values: int = Field(ge=0)

    @model_validator(mode="after")
    def _coverage_counts_match_status(self) -> "DatasetFieldCoverage":
        if self.non_null_rows > self.total_rows or self.unique_non_null_values > self.non_null_rows:
            raise ValueError("dataset field coverage counts are impossible")
        expected = (
            DatasetCoverageStatus.ABSENT
            if self.non_null_rows == 0 and self.column_name is None
            else DatasetCoverageStatus.DATASET_METADATA_ONLY
            if self.column_name is None
            else DatasetCoverageStatus.COLUMN_COMPLETE
            if self.non_null_rows == self.total_rows
            else DatasetCoverageStatus.COLUMN_PARTIAL
        )
        if self.status is not expected:
            raise ValueError("dataset field coverage status is not derived")
        return self


class MaterialsDatasetIdentityAudit(FrozenModel):
    schema_name: Literal["aletheia.materials_dataset_identity_audit"] = (
        "aletheia.materials_dataset_identity_audit"
    )
    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    dataset_ref: str = Field(min_length=1, max_length=512)
    dataset_source: LicensedSourceArtifact
    column_map: MaterialsIdentityColumnMap
    columns: tuple[str, ...] = Field(min_length=1)
    row_count: int = Field(ge=1)
    logical_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_formula_rows: int = Field(ge=0)
    formula_failure_rows: int = Field(ge=0)
    finite_property_rows: int = Field(ge=0)
    unique_formula_identities: int = Field(ge=0)
    unique_chemical_system_identities: int = Field(ge=0)
    composition_collision_groups: int = Field(ge=0)
    maximum_composition_multiplicity: int = Field(ge=0)
    field_coverage: tuple[DatasetFieldCoverage, ...] = Field(min_length=10)
    blockers: tuple[str, ...]
    supported_uses: tuple[str, ...]
    forbidden_interpretations: tuple[str, ...]
    measurement_audit_eligible: Literal[False] = False
    structure_experiment_eligible: Literal[False] = False
    disposition: DatasetIdentityAuditDisposition
    audited_at: AwareDatetime

    @model_validator(mode="after")
    def _summary_is_internally_closed(self) -> "MaterialsDatasetIdentityAudit":
        if self.columns != tuple(sorted(set(self.columns))):
            raise ValueError("dataset columns must be unique and sorted")
        if self.normalized_formula_rows + self.formula_failure_rows != self.row_count:
            raise ValueError("formula success/failure counts must cover the dataset")
        if self.finite_property_rows > self.row_count:
            raise ValueError("finite target count exceeds row count")
        field_ids = tuple(item.field_id for item in self.field_coverage)
        if field_ids != tuple(sorted(set(field_ids))):
            raise ValueError("dataset field coverage must use unique sorted field IDs")
        for values, label in (
            (self.blockers, "blockers"),
            (self.supported_uses, "supported uses"),
            (self.forbidden_interpretations, "forbidden interpretations"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"dataset {label} must be unique and sorted")
        invalid = self.formula_failure_rows > 0 or self.finite_property_rows != self.row_count
        expected = (
            DatasetIdentityAuditDisposition.REJECTED_FORMULA_OR_TARGET_INVALID
            if invalid
            else DatasetIdentityAuditDisposition.COMPOSITION_BENCHMARK_ONLY
        )
        if self.disposition is not expected:
            raise ValueError("dataset identity audit disposition is not derived")
        required_blockers = {
            "batch_identity_absent",
            "measurement_conditions_absent",
            "measurement_method_absent",
            "measurement_uncertainty_absent",
            "row_source_provenance_absent",
            "sample_identity_absent",
            "structure_identity_absent",
        }
        if not required_blockers.issubset(self.blockers):
            raise ValueError("composition-only audit omitted a mandatory identity blocker")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class CompositionTargetCollision(FrozenModel):
    schema_version: Literal[1] = 1
    formula_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_formula: str = Field(min_length=1, max_length=1024)
    row_positions: tuple[int, ...] = Field(min_length=2)
    raw_formulas: tuple[str, ...] = Field(min_length=2)
    property_values: tuple[float, ...] = Field(min_length=2)
    property_minimum: float
    property_maximum: float
    property_range: float = Field(ge=0)
    classification: Literal["unresolved_same_composition"] = "unresolved_same_composition"
    duplicate_or_conflict_determination_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _collision_is_derived(self) -> "CompositionTargetCollision":
        if self.row_positions != tuple(sorted(set(self.row_positions))):
            raise ValueError("composition collision row positions must be unique and sorted")
        if not (len(self.row_positions) == len(self.raw_formulas) == len(self.property_values)):
            raise ValueError("composition collision fields must have equal row cardinality")
        if any(not math.isfinite(value) for value in self.property_values):
            raise ValueError("composition collision property values must be finite")
        if self.property_minimum != min(self.property_values):
            raise ValueError("composition collision minimum is not derived")
        if self.property_maximum != max(self.property_values):
            raise ValueError("composition collision maximum is not derived")
        if not math.isclose(
            self.property_range,
            self.property_maximum - self.property_minimum,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("composition collision range is not derived")
        return self


class MaterialsCompositionCollisionReport(FrozenModel):
    schema_name: Literal["aletheia.materials_composition_collision_report"] = (
        "aletheia.materials_composition_collision_report"
    )
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collisions: tuple[CompositionTargetCollision, ...] = Field(min_length=1)
    collision_group_count: int = Field(ge=1)
    affected_row_count: int = Field(ge=2)
    maximum_property_range: float = Field(ge=0)
    missing_resolution_identities: tuple[Literal["batch", "sample", "structure"], ...]
    disposition: Literal["unresolved_identity_collisions"] = "unresolved_identity_collisions"
    reported_at: AwareDatetime

    @model_validator(mode="after")
    def _report_summary_is_derived(self) -> "MaterialsCompositionCollisionReport":
        identities = tuple(item.formula_identity_sha256 for item in self.collisions)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("composition collisions must have unique sorted identities")
        if self.collision_group_count != len(self.collisions):
            raise ValueError("composition collision group count is not derived")
        if self.affected_row_count != sum(len(item.row_positions) for item in self.collisions):
            raise ValueError("composition collision affected-row count is not derived")
        if self.maximum_property_range != max(item.property_range for item in self.collisions):
            raise ValueError("maximum composition collision range is not derived")
        if self.missing_resolution_identities != ("batch", "sample", "structure"):
            raise ValueError("collision report must retain every missing resolution identity")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


def _not_null(value: Any) -> bool:
    if value is None:
        return False
    try:
        missing = value != value
        if isinstance(missing, bool) and missing:
            return False
    except Exception:
        pass
    return True


def _coverage(
    *,
    field_id: str,
    column_name: str | None,
    dataframe: Any,
    metadata_value: object | None = None,
) -> DatasetFieldCoverage:
    total = len(dataframe)
    if column_name is None:
        present = metadata_value is not None
        return DatasetFieldCoverage(
            field_id=field_id,
            status=(
                DatasetCoverageStatus.DATASET_METADATA_ONLY
                if present
                else DatasetCoverageStatus.ABSENT
            ),
            non_null_rows=total if present else 0,
            total_rows=total,
            unique_non_null_values=1 if present else 0,
        )
    values = [value for value in dataframe[column_name].tolist() if _not_null(value)]
    unique = len({str(value) for value in values})
    return DatasetFieldCoverage(
        field_id=field_id,
        status=(
            DatasetCoverageStatus.COLUMN_COMPLETE
            if len(values) == total
            else DatasetCoverageStatus.COLUMN_PARTIAL
        ),
        column_name=column_name,
        non_null_rows=len(values),
        total_rows=total,
        unique_non_null_values=unique,
    )


def audit_materials_dataframe_identity(
    *,
    audit_id: str,
    dataset_ref: str,
    dataset_source: LicensedSourceArtifact,
    dataframe: Any,
    column_map: MaterialsIdentityColumnMap,
    audited_at: datetime,
) -> MaterialsDatasetIdentityAudit:
    """Audit exact tabular content and refuse to infer absent scientific identities."""

    columns = tuple(sorted(str(item) for item in dataframe.columns))
    required_columns = tuple(
        item
        for item in (
            column_map.formula_column,
            column_map.property_value_column,
            column_map.structure_column,
            column_map.sample_id_column,
            column_map.batch_id_column,
            column_map.property_unit_column,
            column_map.uncertainty_column,
            column_map.measurement_method_column,
            column_map.measurement_conditions_column,
            column_map.row_source_column,
        )
        if item is not None
    )
    missing_columns = sorted(set(required_columns) - set(columns))
    if missing_columns:
        raise ValueError(f"declared materials columns are absent: {missing_columns}")
    if len(dataframe) < 1:
        raise ValueError("materials identity audit requires at least one row")

    logical_rows = []
    formula_failures = 0
    finite_properties = 0
    formula_counts: dict[str, int] = {}
    chemical_systems: set[str] = set()
    for position, (_, row) in enumerate(dataframe.iterrows()):
        raw_formula = row[column_map.formula_column]
        try:
            identity = normalize_formula(str(raw_formula))
            formula_identity = identity.formula_identity_sha256
            chemical_systems.add(identity.chemical_system_identity_sha256)
            formula_counts[formula_identity] = formula_counts.get(formula_identity, 0) + 1
        except Exception:
            formula_failures += 1
            formula_identity = None
        try:
            target = float(row[column_map.property_value_column])
            if math.isfinite(target):
                finite_properties += 1
            else:
                target = None
        except (TypeError, ValueError):
            target = None
        logical_rows.append(
            {
                "position": position,
                "raw_formula": str(raw_formula),
                "formula_identity_sha256": formula_identity,
                "property_value": target,
            }
        )

    coverage = tuple(
        sorted(
            (
                _coverage(
                    field_id="batch_identity",
                    column_name=column_map.batch_id_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="formula_identity",
                    column_name=column_map.formula_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="measurement_conditions",
                    column_name=column_map.measurement_conditions_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="measurement_method",
                    column_name=column_map.measurement_method_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="measurement_uncertainty",
                    column_name=column_map.uncertainty_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="property_unit",
                    column_name=column_map.property_unit_column,
                    dataframe=dataframe,
                    metadata_value=column_map.dataset_level_property_unit_ucum,
                ),
                _coverage(
                    field_id="property_value",
                    column_name=column_map.property_value_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="row_source_provenance",
                    column_name=column_map.row_source_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="sample_identity",
                    column_name=column_map.sample_id_column,
                    dataframe=dataframe,
                ),
                _coverage(
                    field_id="structure_identity",
                    column_name=column_map.structure_column,
                    dataframe=dataframe,
                ),
            ),
            key=lambda item: item.field_id,
        )
    )
    blockers = {
        "batch_identity_absent",
        "measurement_conditions_absent",
        "measurement_method_absent",
        "measurement_uncertainty_absent",
        "row_source_provenance_absent",
        "sample_identity_absent",
        "structure_identity_absent",
    }
    if column_map.property_unit_column is None:
        blockers.add("measurement_unit_not_row_bound")
    if dataset_source.license_expression == "NOASSERTION":
        blockers.add("dataset_license_not_resolved")
    if formula_failures:
        blockers.add("formula_parse_failure")
    if finite_properties != len(dataframe):
        blockers.add("property_value_invalid")
    multiplicities = list(formula_counts.values())
    return MaterialsDatasetIdentityAudit(
        audit_id=audit_id,
        dataset_ref=dataset_ref,
        dataset_source=dataset_source,
        column_map=column_map,
        columns=columns,
        row_count=len(dataframe),
        logical_rows_sha256=content_sha256(logical_rows),
        normalized_formula_rows=len(dataframe) - formula_failures,
        formula_failure_rows=formula_failures,
        finite_property_rows=finite_properties,
        unique_formula_identities=len(formula_counts),
        unique_chemical_system_identities=len(chemical_systems),
        composition_collision_groups=sum(value > 1 for value in multiplicities),
        maximum_composition_multiplicity=max(multiplicities, default=0),
        field_coverage=coverage,
        blockers=tuple(sorted(blockers)),
        supported_uses=("composition_level_predictive_benchmark",),
        forbidden_interpretations=tuple(
            sorted(
                (
                    "batch_or_source_variability_estimated",
                    "composition_row_is_a_physical_sample",
                    "measurement_conflicts_or_repeats_resolved",
                    "sample_disjoint_split_verified",
                    "structure_specific_effect_established",
                )
            )
        ),
        disposition=(
            DatasetIdentityAuditDisposition.REJECTED_FORMULA_OR_TARGET_INVALID
            if formula_failures or finite_properties != len(dataframe)
            else DatasetIdentityAuditDisposition.COMPOSITION_BENCHMARK_ONLY
        ),
        audited_at=audited_at,
    )


def report_composition_collisions(
    *,
    report_id: str,
    source_audit: MaterialsDatasetIdentityAudit,
    dataframe: Any,
    reported_at: datetime,
) -> MaterialsCompositionCollisionReport:
    """Retain normalized-composition collisions without pretending to resolve their cause."""

    replay = audit_materials_dataframe_identity(
        audit_id=source_audit.audit_id,
        dataset_ref=source_audit.dataset_ref,
        dataset_source=source_audit.dataset_source,
        dataframe=dataframe,
        column_map=source_audit.column_map,
        audited_at=source_audit.audited_at,
    )
    if replay != source_audit:
        raise ValueError("composition collision input differs from its source identity audit")
    grouped: dict[str, list[tuple[int, str, str, float]]] = {}
    for position, (_, row) in enumerate(dataframe.iterrows()):
        raw_formula = str(row[source_audit.column_map.formula_column])
        formula = normalize_formula(raw_formula)
        value = float(row[source_audit.column_map.property_value_column])
        grouped.setdefault(formula.formula_identity_sha256, []).append(
            (position, raw_formula, formula.canonical_formula, value)
        )
    collisions = []
    for identity, rows in sorted(grouped.items()):
        if len(rows) < 2:
            continue
        values = tuple(item[3] for item in rows)
        collisions.append(
            CompositionTargetCollision(
                formula_identity_sha256=identity,
                canonical_formula=rows[0][2],
                row_positions=tuple(item[0] for item in rows),
                raw_formulas=tuple(item[1] for item in rows),
                property_values=values,
                property_minimum=min(values),
                property_maximum=max(values),
                property_range=max(values) - min(values),
            )
        )
    if not collisions:
        raise ValueError("source audit contains no composition collisions to report")
    if len(collisions) != source_audit.composition_collision_groups:
        raise ValueError("collision report count differs from source identity audit")
    return MaterialsCompositionCollisionReport(
        report_id=report_id,
        source_audit_sha256=source_audit.audit_sha256,
        logical_rows_sha256=source_audit.logical_rows_sha256,
        collisions=tuple(collisions),
        collision_group_count=len(collisions),
        affected_row_count=sum(len(item.row_positions) for item in collisions),
        maximum_property_range=max(item.property_range for item in collisions),
        missing_resolution_identities=("batch", "sample", "structure"),
        reported_at=reported_at,
    )


__all__ = [
    "CompositionTargetCollision",
    "DatasetCoverageStatus",
    "DatasetFieldCoverage",
    "DatasetIdentityAuditDisposition",
    "MaterialsDatasetIdentityAudit",
    "MaterialsCompositionCollisionReport",
    "MaterialsIdentityColumnMap",
    "audit_materials_dataframe_identity",
    "report_composition_collisions",
]
