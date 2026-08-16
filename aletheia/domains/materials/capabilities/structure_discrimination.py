"""Precommitted matched-capacity structure-aware reference experiment for F10-S4."""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.domains.materials.featurizers import magpie_features
from aletheia.domains.materials.identity import LicensedSourceArtifact
from aletheia.domains.materials.structures import (
    StructureDatasetQualityDisposition,
    StructureDatasetQualityLedger,
    StructureFeatureMatrixReceipt,
    StructureGeometryFeaturePolicy,
    StructureQualityPolicy,
    build_structure_feature_matrix,
    build_structure_quality_ledger,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REQUIRED_PACKAGE_NAMES = frozenset(
    {"matminer", "numpy", "pandas", "pymatgen", "scikit-learn", "spglib"}
)


class StructureEvaluationRole(str, Enum):
    INTERNAL_VALIDATION = "internal_validation"
    LOCKED_HOLDOUT = "locked_holdout"
    TRAIN = "train"


class StructureExperimentArm(str, Enum):
    ALIGNED_STRUCTURE = "aligned_structure"
    COMPOSITION_ONLY = "composition_only"
    PERMUTED_STRUCTURE_CONTROL = "permuted_structure_control"


class StructureSignalDisposition(str, Enum):
    NO_ALIGNED_STRUCTURE_ADVANTAGE = "no_aligned_structure_advantage"
    ROBUST_ALIGNED_STRUCTURE_SIGNAL = "robust_aligned_structure_signal"
    SUGGESTIVE_NOT_ROBUST = "suggestive_not_robust"


class StructureDatasetContract(FrozenModel):
    schema_name: Literal["aletheia.structure_dataset_contract"] = (
        "aletheia.structure_dataset_contract"
    )
    schema_version: Literal[1] = 1
    dataset_ref: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    media_type: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-zA-Z0-9][a-zA-Z0-9.+-]{0,126}$")
    expected_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_row_count: int = Field(ge=1)
    structure_column: str = Field(min_length=1, max_length=256)
    target_column: str = Field(min_length=1, max_length=256)
    target_quantity_kind_id: str = Field(min_length=1, max_length=256)
    target_unit_ucum: str = Field(pattern=r"^[!-~]{1,64}$")
    source_uri: str = Field(min_length=1, max_length=2048)
    license_expression: str = Field(min_length=1, max_length=512)
    license_uri: str = Field(min_length=1, max_length=2048)
    license_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _columns_and_uris_are_valid(self) -> "StructureDatasetContract":
        if self.structure_column == self.target_column:
            raise ValueError("structure and target columns must differ")
        if ":" not in self.source_uri or ":" not in self.license_uri:
            raise ValueError("structure dataset source and licence must be absolute URIs")
        return self


class StructureGroupSplitPolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_group_split_policy"] = (
        "aletheia.structure_group_split_policy"
    )
    schema_version: Literal[1] = 1
    algorithm: Literal["chemical_system_balanced_hash_v1"] = "chemical_system_balanced_hash_v1"
    seed: int = Field(ge=0, le=2**32 - 1)
    train_fraction: float = Field(gt=0, lt=1)
    internal_validation_fraction: float = Field(gt=0, lt=1)
    locked_holdout_fraction: float = Field(gt=0, lt=1)
    minimum_rows_per_role: int = Field(default=40, ge=1)
    group_identity: Literal["chemical_system"] = "chemical_system"
    target_blind_assignment: Literal[True] = True

    @model_validator(mode="after")
    def _fractions_cover_one(self) -> "StructureGroupSplitPolicy":
        if not math.isclose(
            self.train_fraction + self.internal_validation_fraction + self.locked_holdout_fraction,
            1.0,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("structure split fractions must sum to one")
        return self


class MatchedEstimatorPolicy(FrozenModel):
    schema_name: Literal["aletheia.matched_structure_estimator_policy"] = (
        "aletheia.matched_structure_estimator_policy"
    )
    schema_version: Literal[1] = 1
    estimator: Literal["sklearn.random_forest_regressor"] = "sklearn.random_forest_regressor"
    n_estimators: int = Field(default=256, ge=8, le=4096)
    max_depth: int | None = Field(default=18, ge=2, le=128)
    min_samples_leaf: int = Field(default=2, ge=1, le=1024)
    max_features: float = Field(default=1.0, gt=0, le=1)
    random_state: int = Field(ge=0, le=2**32 - 1)
    n_jobs: Literal[1] = 1
    hyperparameter_tuning_forbidden: Literal[True] = True
    fit_once_per_arm: Literal[True] = True

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class StructureBootstrapPolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_cluster_bootstrap_policy"] = (
        "aletheia.structure_cluster_bootstrap_policy"
    )
    schema_version: Literal[1] = 1
    resamples: int = Field(ge=100, le=100_000)
    confidence_level: float = Field(gt=0.5, lt=1)
    seed: int = Field(ge=0, le=2**32 - 1)
    cluster_identity: Literal["chemical_system"] = "chemical_system"


class StructureSignalAcceptancePolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_signal_acceptance_policy"] = (
        "aletheia.structure_signal_acceptance_policy"
    )
    schema_version: Literal[1] = 1
    minimum_relative_mae_improvement: float = Field(ge=0, le=1)
    require_positive_cluster_ci_internal: Literal[True] = True
    require_positive_cluster_ci_holdout: Literal[True] = True
    require_both_roles: Literal[True] = True
    mechanism_or_causal_claim_forbidden: Literal[True] = True


class StructureAwareExperimentProtocol(FrozenModel):
    schema_name: Literal["aletheia.structure_aware_experiment_protocol"] = (
        "aletheia.structure_aware_experiment_protocol"
    )
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    dataset: StructureDatasetContract
    quality_policy: StructureQualityPolicy
    geometry_feature_policy: StructureGeometryFeaturePolicy
    split_policy: StructureGroupSplitPolicy
    estimator_policy: MatchedEstimatorPolicy
    bootstrap_policy: StructureBootstrapPolicy
    acceptance_policy: StructureSignalAcceptancePolicy
    composition_featurizer: Literal["matminer.magpie.v1"] = "matminer.magpie.v1"
    structure_control: Literal["within_role_row_permutation"] = "within_role_row_permutation"
    permutation_seed: int = Field(ge=0, le=2**32 - 1)
    implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    required_package_versions: dict[str, str] = Field(min_length=6, max_length=6)
    public_retrospective_dataset: Literal[True] = True
    independent_external_dataset_claim_forbidden: Literal[True] = True
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _environment_contract_is_closed(self) -> "StructureAwareExperimentProtocol":
        if set(self.required_package_versions) != _REQUIRED_PACKAGE_NAMES:
            raise ValueError("structure protocol package-version contract is incomplete")
        if any(not value.strip() for value in self.required_package_versions.values()):
            raise ValueError("structure protocol package versions cannot be blank")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class StructureDatasetReceipt(FrozenModel):
    schema_name: Literal["aletheia.structure_dataset_receipt"] = (
        "aletheia.structure_dataset_receipt"
    )
    schema_version: Literal[1] = 1
    source: LicensedSourceArtifact
    dataset_ref: str
    row_count: int = Field(ge=1)
    logical_rows_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_vector_sha256: str = Field(pattern=_SHA256_PATTERN)
    formula_vector_sha256: str = Field(pattern=_SHA256_PATTERN)
    structure_vector_sha256: str = Field(pattern=_SHA256_PATTERN)
    package_versions: dict[str, str]

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class CompositionFeatureMatrixReceipt(FrozenModel):
    schema_name: Literal["aletheia.composition_feature_matrix_receipt"] = (
        "aletheia.composition_feature_matrix_receipt"
    )
    schema_version: Literal[1] = 1
    row_count: int = Field(ge=1)
    feature_count: int = Field(ge=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    feature_names_sha256: str = Field(pattern=_SHA256_PATTERN)
    matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    featurizer: Literal["matminer.magpie.v1"] = "matminer.magpie.v1"

    @model_validator(mode="after")
    def _names_are_closed(self) -> "CompositionFeatureMatrixReceipt":
        if self.feature_count != len(self.feature_names):
            raise ValueError("composition feature count differs from names")
        if self.feature_names != tuple(dict.fromkeys(self.feature_names)):
            raise ValueError("composition feature names must be unique and ordered")
        if self.feature_names_sha256 != content_sha256(list(self.feature_names)):
            raise ValueError("composition feature names hash is invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class StructureSplitAssignment(FrozenModel):
    schema_version: Literal[1] = 1
    row_position: int = Field(ge=0)
    row_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    chemical_system_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: StructureEvaluationRole


class StructureSplitReceipt(FrozenModel):
    schema_name: Literal["aletheia.structure_split_receipt"] = "aletheia.structure_split_receipt"
    schema_version: Literal[1] = 1
    policy: StructureGroupSplitPolicy
    assignments: tuple[StructureSplitAssignment, ...] = Field(min_length=3)
    train_rows: int = Field(ge=1)
    internal_validation_rows: int = Field(ge=1)
    locked_holdout_rows: int = Field(ge=1)
    train_groups: int = Field(ge=1)
    internal_validation_groups: int = Field(ge=1)
    locked_holdout_groups: int = Field(ge=1)
    membership_sha256: str = Field(pattern=_SHA256_PATTERN)
    group_disjoint: Literal[True] = True

    @model_validator(mode="after")
    def _split_is_complete_and_group_disjoint(self) -> "StructureSplitReceipt":
        positions = tuple(item.row_position for item in self.assignments)
        if positions != tuple(range(len(self.assignments))):
            raise ValueError("structure split assignments must cover ordered row positions")
        role_rows = {
            role: sum(item.role is role for item in self.assignments)
            for role in StructureEvaluationRole
        }
        if (
            self.train_rows != role_rows[StructureEvaluationRole.TRAIN]
            or self.internal_validation_rows
            != role_rows[StructureEvaluationRole.INTERNAL_VALIDATION]
            or self.locked_holdout_rows != role_rows[StructureEvaluationRole.LOCKED_HOLDOUT]
        ):
            raise ValueError("structure split row counts are not derived")
        group_roles: dict[str, set[StructureEvaluationRole]] = {}
        for item in self.assignments:
            group_roles.setdefault(item.chemical_system_identity_sha256, set()).add(item.role)
        if any(len(roles) != 1 for roles in group_roles.values()):
            raise ValueError("chemical system crosses structure evaluation roles")
        role_groups = {
            role: sum(next(iter(roles)) is role for roles in group_roles.values())
            for role in StructureEvaluationRole
        }
        if (
            self.train_groups != role_groups[StructureEvaluationRole.TRAIN]
            or self.internal_validation_groups
            != role_groups[StructureEvaluationRole.INTERNAL_VALIDATION]
            or self.locked_holdout_groups != role_groups[StructureEvaluationRole.LOCKED_HOLDOUT]
        ):
            raise ValueError("structure split group counts are not derived")
        expected_membership = content_sha256(
            {
                "policy": self.policy.model_dump(mode="json"),
                "assignments": [item.model_dump(mode="json") for item in self.assignments],
            }
        )
        if self.membership_sha256 != expected_membership:
            raise ValueError("structure split membership hash is invalid")
        minimum = self.policy.minimum_rows_per_role
        if min(role_rows.values()) < minimum:
            raise ValueError("structure split role is sample-starved")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class StructureAwareExperimentPlan(FrozenModel):
    schema_name: Literal["aletheia.structure_aware_experiment_plan"] = (
        "aletheia.structure_aware_experiment_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    protocol: StructureAwareExperimentProtocol
    dataset_receipt: StructureDatasetReceipt
    quality_ledger: StructureDatasetQualityLedger
    split_receipt: StructureSplitReceipt
    composition_features: CompositionFeatureMatrixReceipt
    structure_features: StructureFeatureMatrixReceipt
    prepared_at: AwareDatetime
    model_fit_count_at_freeze: Literal[0] = 0
    target_driven_split_forbidden: Literal[True] = True
    state: Literal["frozen_before_model_fit"] = "frozen_before_model_fit"

    @model_validator(mode="after")
    def _plan_is_exactly_bound(self) -> "StructureAwareExperimentPlan":
        if self.protocol.frozen_at >= self.prepared_at:
            raise ValueError("structure experiment plan must follow protocol freeze")
        if self.protocol.implementation_sha256 != structure_discrimination_implementation_sha256():
            raise ValueError("structure experiment implementation differs from frozen protocol")
        if (
            self.dataset_receipt.source.sha256 != self.protocol.dataset.expected_file_sha256
            or self.dataset_receipt.row_count != self.protocol.dataset.expected_row_count
            or self.dataset_receipt.package_versions != self.protocol.required_package_versions
            or self.quality_ledger.dataset_sha256 != self.dataset_receipt.source.sha256
            or self.structure_features.quality_ledger_sha256 != self.quality_ledger.ledger_sha256
            or self.structure_features.feature_policy_sha256
            != self.protocol.geometry_feature_policy.policy_sha256
        ):
            raise ValueError("structure experiment plan changed dataset or feature lineage")
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


class PermutationRoleReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    role: StructureEvaluationRole
    row_count: int = Field(ge=1)
    source_positions_sha256: str = Field(pattern=_SHA256_PATTERN)
    permuted_positions_sha256: str = Field(pattern=_SHA256_PATTERN)
    stays_within_role: Literal[True] = True


class MatchedCapacityReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    aligned_feature_count: int = Field(ge=1)
    permuted_control_feature_count: int = Field(ge=1)
    train_rows_each: int = Field(ge=1)
    estimator_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    fit_count_aligned: Literal[1] = 1
    fit_count_permuted_control: Literal[1] = 1
    exact_feature_dimension_match: Literal[True] = True
    exact_estimator_budget_match: Literal[True] = True

    @model_validator(mode="after")
    def _dimensions_match(self) -> "MatchedCapacityReceipt":
        if self.aligned_feature_count != self.permuted_control_feature_count:
            raise ValueError("aligned and permuted structure arms changed model input capacity")
        return self


class StructureArmEvaluation(FrozenModel):
    schema_version: Literal[1] = 1
    arm: StructureExperimentArm
    role: Literal[
        StructureEvaluationRole.INTERNAL_VALIDATION,
        StructureEvaluationRole.LOCKED_HOLDOUT,
    ]
    row_count: int = Field(ge=1)
    feature_count: int = Field(ge=1)
    mae: float = Field(ge=0)
    rmse: float = Field(ge=0)
    predictions_sha256: str = Field(pattern=_SHA256_PATTERN)
    absolute_errors_sha256: str = Field(pattern=_SHA256_PATTERN)


class StructureSignalEvaluation(FrozenModel):
    schema_version: Literal[1] = 1
    role: Literal[
        StructureEvaluationRole.INTERNAL_VALIDATION,
        StructureEvaluationRole.LOCKED_HOLDOUT,
    ]
    aligned_mae: float = Field(ge=0)
    permuted_control_mae: float = Field(ge=0)
    control_minus_aligned_mae: float
    relative_mae_improvement: float
    cluster_ci_lower: float
    cluster_ci_upper: float
    bootstrap_probability_improvement: float = Field(ge=0, le=1)
    bootstrap_distribution_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _deltas_are_derived(self) -> "StructureSignalEvaluation":
        delta = self.permuted_control_mae - self.aligned_mae
        if not math.isclose(self.control_minus_aligned_mae, delta, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("structure signal MAE delta is not derived")
        relative = delta / self.permuted_control_mae if self.permuted_control_mae else 0.0
        if not math.isclose(self.relative_mae_improvement, relative, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("structure signal relative improvement is not derived")
        if self.cluster_ci_lower > self.cluster_ci_upper:
            raise ValueError("structure signal confidence interval is reversed")
        return self


class StructureAwareExperimentResult(FrozenModel):
    schema_name: Literal["aletheia.structure_aware_experiment_result"] = (
        "aletheia.structure_aware_experiment_result"
    )
    schema_version: Literal[1] = 1
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    acceptance_policy: StructureSignalAcceptancePolicy
    permutation_receipts: tuple[PermutationRoleReceipt, ...] = Field(min_length=3, max_length=3)
    matched_capacity: MatchedCapacityReceipt
    arm_evaluations: tuple[StructureArmEvaluation, ...] = Field(min_length=6, max_length=6)
    signal_evaluations: tuple[StructureSignalEvaluation, ...] = Field(min_length=2, max_length=2)
    disposition: StructureSignalDisposition
    completed_at: AwareDatetime
    all_preregistered_arms_retained: Literal[True] = True
    holdout_is_same_dataset_not_external_replication: Literal[True] = True
    causal_or_mechanism_claim_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _result_is_complete_and_disposition_derived(self) -> "StructureAwareExperimentResult":
        permutation_roles = tuple(item.role.value for item in self.permutation_receipts)
        if permutation_roles != tuple(sorted(set(permutation_roles))):
            raise ValueError("permutation receipts must cover unique sorted roles")
        evaluation_keys = tuple((item.arm.value, item.role.value) for item in self.arm_evaluations)
        if evaluation_keys != tuple(sorted(set(evaluation_keys))):
            raise ValueError("arm evaluations must cover unique sorted arm/role pairs")
        expected_keys = {
            (arm.value, role.value)
            for arm in StructureExperimentArm
            for role in (
                StructureEvaluationRole.INTERNAL_VALIDATION,
                StructureEvaluationRole.LOCKED_HOLDOUT,
            )
        }
        if set(evaluation_keys) != expected_keys:
            raise ValueError("structure experiment omitted a preregistered arm or evaluation role")
        signal_roles = tuple(item.role.value for item in self.signal_evaluations)
        if signal_roles != tuple(sorted(set(signal_roles))):
            raise ValueError("signal evaluations must cover unique sorted roles")
        evaluations = {(item.arm, item.role): item for item in self.arm_evaluations}
        for signal in self.signal_evaluations:
            aligned = evaluations[(StructureExperimentArm.ALIGNED_STRUCTURE, signal.role)]
            control = evaluations[(StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL, signal.role)]
            if signal.aligned_mae != aligned.mae or signal.permuted_control_mae != control.mae:
                raise ValueError("structure signal evaluation changed its arm metrics")
        expected_disposition = _derive_disposition(
            signals=self.signal_evaluations,
            policy=self.acceptance_policy,
        )
        if self.disposition is not expected_disposition:
            raise ValueError("structure signal disposition is not mechanically derived")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


def structure_discrimination_implementation_components() -> dict[str, str]:
    """Hash every repository source file that can change an experiment matrix or result."""

    materials_directory = Path(__file__).resolve().parents[1]
    paths = {
        "structure_discrimination.py": Path(__file__).resolve(),
        "featurizers.py": materials_directory / "featurizers.py",
        "identity.py": materials_directory / "identity.py",
        "structures.py": materials_directory / "structures.py",
    }
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(paths.items())
    }


def structure_discrimination_implementation_sha256() -> str:
    return content_sha256(structure_discrimination_implementation_components())


def _current_package_versions() -> dict[str, str]:
    return {name: importlib.metadata.version(name) for name in sorted(_REQUIRED_PACKAGE_NAMES)}


def _matrix_sha256(matrix: Any) -> str:
    import numpy as np

    contiguous = np.ascontiguousarray(matrix, dtype=np.float64)
    return content_sha256(
        {
            "shape": list(contiguous.shape),
            "dtype": str(contiguous.dtype),
            "bytes_sha256": hashlib.sha256(contiguous.tobytes()).hexdigest(),
        }
    )


def _composition_features(
    quality_ledger: StructureDatasetQualityLedger,
) -> tuple[Any, CompositionFeatureMatrixReceipt]:
    import numpy as np
    import pandas as pd

    frame = pd.DataFrame(
        {"composition": [item.formula.canonical_formula for item in quality_ledger.rows]}
    )
    features, names, aligned = magpie_features(frame, "composition")
    if len(aligned) != len(frame) or tuple(aligned.index) != tuple(frame.index):
        raise ValueError("Magpie dropped or reordered a structure experiment row")
    matrix = np.ascontiguousarray(features.to_numpy(dtype=np.float64), dtype=np.float64)
    if not np.isfinite(matrix).all():
        raise ValueError("composition featurizer produced nonfinite values")
    feature_names = tuple(str(item) for item in names)
    receipt = CompositionFeatureMatrixReceipt(
        row_count=matrix.shape[0],
        feature_count=matrix.shape[1],
        feature_names=feature_names,
        feature_names_sha256=content_sha256(list(feature_names)),
        matrix_sha256=_matrix_sha256(matrix),
    )
    return matrix, receipt


def _build_split(
    *,
    quality_ledger: StructureDatasetQualityLedger,
    policy: StructureGroupSplitPolicy,
) -> StructureSplitReceipt:
    grouped: dict[str, list[int]] = {}
    for row in quality_ledger.rows:
        grouped.setdefault(row.formula.chemical_system_identity_sha256, []).append(row.row_position)
    roles = (
        StructureEvaluationRole.INTERNAL_VALIDATION,
        StructureEvaluationRole.LOCKED_HOLDOUT,
        StructureEvaluationRole.TRAIN,
    )
    fractions = {
        StructureEvaluationRole.TRAIN: policy.train_fraction,
        StructureEvaluationRole.INTERNAL_VALIDATION: policy.internal_validation_fraction,
        StructureEvaluationRole.LOCKED_HOLDOUT: policy.locked_holdout_fraction,
    }
    targets = {role: max(1.0, len(quality_ledger.rows) * fractions[role]) for role in roles}
    assigned: dict[StructureEvaluationRole, list[int]] = {role: [] for role in roles}

    def salted(identity: str) -> str:
        return hashlib.sha256(f"{policy.seed}:{identity}".encode()).hexdigest()

    for identity in sorted(grouped, key=lambda item: (-len(grouped[item]), salted(item))):
        deficits = {role: (targets[role] - len(assigned[role])) / targets[role] for role in roles}
        role = max(
            roles,
            key=lambda item: (
                deficits[item],
                hashlib.sha256(f"{policy.seed}:{identity}:{item.value}".encode()).hexdigest(),
            ),
        )
        assigned[role].extend(grouped[identity])
    role_by_position = {
        position: role for role, positions in assigned.items() for position in positions
    }
    assignments = tuple(
        StructureSplitAssignment(
            row_position=row.row_position,
            row_receipt_sha256=row.row_receipt_sha256,
            chemical_system_identity_sha256=row.formula.chemical_system_identity_sha256,
            role=role_by_position[row.row_position],
        )
        for row in quality_ledger.rows
    )
    row_counts = {role: len(assigned[role]) for role in roles}
    group_counts = {
        role: len(
            {item.chemical_system_identity_sha256 for item in assignments if item.role is role}
        )
        for role in roles
    }
    membership = content_sha256(
        {
            "policy": policy.model_dump(mode="json"),
            "assignments": [item.model_dump(mode="json") for item in assignments],
        }
    )
    return StructureSplitReceipt(
        policy=policy,
        assignments=assignments,
        train_rows=row_counts[StructureEvaluationRole.TRAIN],
        internal_validation_rows=row_counts[StructureEvaluationRole.INTERNAL_VALIDATION],
        locked_holdout_rows=row_counts[StructureEvaluationRole.LOCKED_HOLDOUT],
        train_groups=group_counts[StructureEvaluationRole.TRAIN],
        internal_validation_groups=group_counts[StructureEvaluationRole.INTERNAL_VALIDATION],
        locked_holdout_groups=group_counts[StructureEvaluationRole.LOCKED_HOLDOUT],
        membership_sha256=membership,
    )


def _dataset_receipt(
    *,
    protocol: StructureAwareExperimentProtocol,
    source: LicensedSourceArtifact,
    quality_ledger: StructureDatasetQualityLedger,
    targets: Any,
) -> StructureDatasetReceipt:
    import numpy as np

    target_array = np.ascontiguousarray(targets, dtype=np.float64)
    logical_rows = [
        {
            "row_position": row.row_position,
            "source_row_sha256": row.source_row_sha256,
            "formula_identity_sha256": row.formula.formula_identity_sha256,
            "canonical_structure_sha256": row.canonical_structure_sha256,
            "target": float(target_array[row.row_position]),
        }
        for row in quality_ledger.rows
    ]
    return StructureDatasetReceipt(
        source=source,
        dataset_ref=protocol.dataset.dataset_ref,
        row_count=len(quality_ledger.rows),
        logical_rows_sha256=content_sha256(logical_rows),
        target_vector_sha256=_matrix_sha256(target_array.reshape(-1, 1)),
        formula_vector_sha256=content_sha256(
            [item.formula.formula_identity_sha256 for item in quality_ledger.rows]
        ),
        structure_vector_sha256=content_sha256(
            [item.canonical_structure_sha256 for item in quality_ledger.rows]
        ),
        package_versions=_current_package_versions(),
    )


def _prepare_components(
    *,
    protocol: StructureAwareExperimentProtocol,
    dataframe: Any,
    dataset_file_bytes: bytes,
    prepared_at: datetime,
) -> tuple[
    StructureDatasetReceipt,
    StructureDatasetQualityLedger,
    StructureSplitReceipt,
    CompositionFeatureMatrixReceipt,
    StructureFeatureMatrixReceipt,
    Any,
    Any,
    Any,
]:
    import numpy as np

    if protocol.implementation_sha256 != structure_discrimination_implementation_sha256():
        raise ValueError("structure experiment protocol froze another implementation")
    if protocol.required_package_versions != _current_package_versions():
        raise ValueError("structure experiment package versions differ from frozen protocol")
    if hashlib.sha256(dataset_file_bytes).hexdigest() != protocol.dataset.expected_file_sha256:
        raise ValueError("structure dataset file differs from frozen hash")
    if len(dataframe) != protocol.dataset.expected_row_count:
        raise ValueError("structure dataset row count differs from frozen contract")
    expected_columns = {protocol.dataset.structure_column, protocol.dataset.target_column}
    if not expected_columns.issubset({str(item) for item in dataframe.columns}):
        raise ValueError("structure dataset is missing a frozen column")
    structures = tuple(dataframe[protocol.dataset.structure_column].tolist())
    targets = np.ascontiguousarray(
        dataframe[protocol.dataset.target_column].to_numpy(dtype=np.float64), dtype=np.float64
    )
    if not np.isfinite(targets).all():
        raise ValueError("structure experiment target contains nonfinite values")
    source = LicensedSourceArtifact(
        artifact_id=protocol.dataset.artifact_id,
        sha256=protocol.dataset.expected_file_sha256,
        bytes=len(dataset_file_bytes),
        media_type=protocol.dataset.media_type,
        source_uri=protocol.dataset.source_uri,
        license_expression=protocol.dataset.license_expression,
        license_uri=protocol.dataset.license_uri,
        license_evidence_sha256=protocol.dataset.license_evidence_sha256,
        retrieved_at=prepared_at,
    )
    source.verify_bytes(dataset_file_bytes)
    quality = build_structure_quality_ledger(
        dataset_sha256=source.sha256,
        structures=structures,
        policy=protocol.quality_policy,
    )
    if quality.disposition is not StructureDatasetQualityDisposition.ACCEPTED:
        raise ValueError("structure dataset failed the frozen all-row quality gate")
    split = _build_split(quality_ledger=quality, policy=protocol.split_policy)
    composition_matrix, composition_receipt = _composition_features(quality)
    structure_matrix, structure_receipt = build_structure_feature_matrix(
        structures=structures,
        quality_ledger=quality,
        feature_policy=protocol.geometry_feature_policy,
    )
    receipt = _dataset_receipt(
        protocol=protocol,
        source=source,
        quality_ledger=quality,
        targets=targets,
    )
    return (
        receipt,
        quality,
        split,
        composition_receipt,
        structure_receipt,
        composition_matrix,
        structure_matrix,
        targets,
    )


def build_structure_aware_experiment_plan(
    *,
    plan_id: str,
    protocol: StructureAwareExperimentProtocol,
    dataframe: Any,
    dataset_file_bytes: bytes,
    prepared_at: datetime,
) -> StructureAwareExperimentPlan:
    components = _prepare_components(
        protocol=protocol,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        prepared_at=prepared_at,
    )
    receipt, quality, split, composition_receipt, structure_receipt = components[:5]
    return StructureAwareExperimentPlan(
        plan_id=plan_id,
        protocol=protocol,
        dataset_receipt=receipt,
        quality_ledger=quality,
        split_receipt=split,
        composition_features=composition_receipt,
        structure_features=structure_receipt,
        prepared_at=prepared_at,
    )


def _role_indices(split: StructureSplitReceipt, role: StructureEvaluationRole) -> Any:
    import numpy as np

    return np.asarray(
        [item.row_position for item in split.assignments if item.role is role], dtype=np.int64
    )


def _permuted_structure_matrix(
    *,
    structure_matrix: Any,
    split: StructureSplitReceipt,
    seed: int,
) -> tuple[Any, tuple[PermutationRoleReceipt, ...]]:
    import numpy as np

    output = np.empty_like(structure_matrix)
    receipts = []
    for offset, role in enumerate(sorted(StructureEvaluationRole, key=lambda item: item.value)):
        indices = _role_indices(split, role)
        rng = np.random.default_rng(seed + offset)
        permuted = rng.permutation(indices)
        output[indices] = structure_matrix[permuted]
        receipts.append(
            PermutationRoleReceipt(
                role=role,
                row_count=len(indices),
                source_positions_sha256=content_sha256(indices.tolist()),
                permuted_positions_sha256=content_sha256(permuted.tolist()),
            )
        )
    return output, tuple(receipts)


def _fit_predict(
    *,
    matrix: Any,
    targets: Any,
    train_indices: Any,
    evaluation_indices: dict[StructureEvaluationRole, Any],
    policy: MatchedEstimatorPolicy,
) -> dict[StructureEvaluationRole, Any]:
    from sklearn.ensemble import RandomForestRegressor

    model = RandomForestRegressor(
        n_estimators=policy.n_estimators,
        max_depth=policy.max_depth,
        min_samples_leaf=policy.min_samples_leaf,
        max_features=policy.max_features,
        random_state=policy.random_state,
        n_jobs=policy.n_jobs,
    )
    model.fit(matrix[train_indices], targets[train_indices])
    return {role: model.predict(matrix[indices]) for role, indices in evaluation_indices.items()}


def _arm_evaluation(
    *,
    arm: StructureExperimentArm,
    role: StructureEvaluationRole,
    targets: Any,
    indices: Any,
    predictions: Any,
    feature_count: int,
) -> StructureArmEvaluation:
    import numpy as np

    actual = np.ascontiguousarray(targets[indices], dtype=np.float64)
    predicted = np.ascontiguousarray(predictions, dtype=np.float64)
    errors = np.ascontiguousarray(np.abs(actual - predicted), dtype=np.float64)
    return StructureArmEvaluation(
        arm=arm,
        role=role,
        row_count=len(indices),
        feature_count=feature_count,
        mae=float(errors.mean()),
        rmse=float(np.sqrt(np.mean((actual - predicted) ** 2))),
        predictions_sha256=_matrix_sha256(predicted.reshape(-1, 1)),
        absolute_errors_sha256=_matrix_sha256(errors.reshape(-1, 1)),
    )


def _cluster_bootstrap_signal(
    *,
    role: StructureEvaluationRole,
    targets: Any,
    indices: Any,
    aligned_predictions: Any,
    control_predictions: Any,
    quality: StructureDatasetQualityLedger,
    policy: StructureBootstrapPolicy,
) -> StructureSignalEvaluation:
    import numpy as np

    actual = np.asarray(targets[indices], dtype=np.float64)
    aligned_errors = np.abs(actual - np.asarray(aligned_predictions, dtype=np.float64))
    control_errors = np.abs(actual - np.asarray(control_predictions, dtype=np.float64))
    deltas = control_errors - aligned_errors
    groups: dict[str, list[int]] = {}
    for local_position, row_position in enumerate(indices.tolist()):
        identity = quality.rows[row_position].formula.chemical_system_identity_sha256
        groups.setdefault(identity, []).append(local_position)
    group_ids = sorted(groups)
    rng = np.random.default_rng(
        policy.seed + (0 if role is StructureEvaluationRole.INTERNAL_VALIDATION else 1)
    )
    distribution = np.empty(policy.resamples, dtype=np.float64)
    for iteration in range(policy.resamples):
        sampled = rng.integers(0, len(group_ids), size=len(group_ids))
        selected = [index for group_index in sampled for index in groups[group_ids[group_index]]]
        distribution[iteration] = float(deltas[selected].mean())
    alpha = 1.0 - policy.confidence_level
    lower, upper = np.quantile(distribution, [alpha / 2, 1 - alpha / 2])
    aligned_mae = float(aligned_errors.mean())
    control_mae = float(control_errors.mean())
    delta = control_mae - aligned_mae
    return StructureSignalEvaluation(
        role=role,
        aligned_mae=aligned_mae,
        permuted_control_mae=control_mae,
        control_minus_aligned_mae=delta,
        relative_mae_improvement=delta / control_mae if control_mae else 0.0,
        cluster_ci_lower=float(lower),
        cluster_ci_upper=float(upper),
        bootstrap_probability_improvement=float(np.mean(distribution > 0)),
        bootstrap_distribution_sha256=_matrix_sha256(distribution.reshape(-1, 1)),
    )


def _derive_disposition(
    *,
    signals: tuple[StructureSignalEvaluation, ...],
    policy: StructureSignalAcceptancePolicy,
) -> StructureSignalDisposition:
    robust = all(
        item.cluster_ci_lower > 0
        and item.relative_mae_improvement >= policy.minimum_relative_mae_improvement
        for item in signals
    )
    if robust:
        return StructureSignalDisposition.ROBUST_ALIGNED_STRUCTURE_SIGNAL
    if all(item.control_minus_aligned_mae > 0 for item in signals):
        return StructureSignalDisposition.SUGGESTIVE_NOT_ROBUST
    return StructureSignalDisposition.NO_ALIGNED_STRUCTURE_ADVANTAGE


def run_structure_aware_experiment(
    *,
    plan: StructureAwareExperimentPlan,
    dataframe: Any,
    dataset_file_bytes: bytes,
    completed_at: datetime,
) -> StructureAwareExperimentResult:
    """Rebuild all inputs, execute every frozen arm once, and retain both evaluation roles."""

    import numpy as np

    if completed_at <= plan.prepared_at:
        raise ValueError("structure experiment completion must follow plan freeze")
    components = _prepare_components(
        protocol=plan.protocol,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        prepared_at=plan.prepared_at,
    )
    receipt, quality, split, composition_receipt, structure_receipt = components[:5]
    if (
        receipt != plan.dataset_receipt
        or quality != plan.quality_ledger
        or split != plan.split_receipt
        or composition_receipt != plan.composition_features
        or structure_receipt != plan.structure_features
    ):
        raise ValueError("structure experiment inputs differ from frozen plan")
    composition_matrix, structure_matrix, targets = components[5:]
    permuted_structure, permutation_receipts = _permuted_structure_matrix(
        structure_matrix=structure_matrix,
        split=split,
        seed=plan.protocol.permutation_seed,
    )
    matrices = {
        StructureExperimentArm.COMPOSITION_ONLY: composition_matrix,
        StructureExperimentArm.ALIGNED_STRUCTURE: np.ascontiguousarray(
            np.hstack((composition_matrix, structure_matrix)), dtype=np.float64
        ),
        StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL: np.ascontiguousarray(
            np.hstack((composition_matrix, permuted_structure)), dtype=np.float64
        ),
    }
    train_indices = _role_indices(split, StructureEvaluationRole.TRAIN)
    evaluation_indices = {
        role: _role_indices(split, role)
        for role in (
            StructureEvaluationRole.INTERNAL_VALIDATION,
            StructureEvaluationRole.LOCKED_HOLDOUT,
        )
    }
    predictions = {
        arm: _fit_predict(
            matrix=matrix,
            targets=targets,
            train_indices=train_indices,
            evaluation_indices=evaluation_indices,
            policy=plan.protocol.estimator_policy,
        )
        for arm, matrix in matrices.items()
    }
    arm_evaluations = tuple(
        sorted(
            (
                _arm_evaluation(
                    arm=arm,
                    role=role,
                    targets=targets,
                    indices=evaluation_indices[role],
                    predictions=predictions[arm][role],
                    feature_count=matrices[arm].shape[1],
                )
                for arm in StructureExperimentArm
                for role in (
                    StructureEvaluationRole.INTERNAL_VALIDATION,
                    StructureEvaluationRole.LOCKED_HOLDOUT,
                )
            ),
            key=lambda item: (item.arm.value, item.role.value),
        )
    )
    signals = tuple(
        sorted(
            (
                _cluster_bootstrap_signal(
                    role=role,
                    targets=targets,
                    indices=evaluation_indices[role],
                    aligned_predictions=predictions[StructureExperimentArm.ALIGNED_STRUCTURE][role],
                    control_predictions=predictions[
                        StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL
                    ][role],
                    quality=quality,
                    policy=plan.protocol.bootstrap_policy,
                )
                for role in (
                    StructureEvaluationRole.INTERNAL_VALIDATION,
                    StructureEvaluationRole.LOCKED_HOLDOUT,
                )
            ),
            key=lambda item: item.role.value,
        )
    )
    aligned_count = matrices[StructureExperimentArm.ALIGNED_STRUCTURE].shape[1]
    control_count = matrices[StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL].shape[1]
    return StructureAwareExperimentResult(
        plan_sha256=plan.plan_sha256,
        acceptance_policy=plan.protocol.acceptance_policy,
        permutation_receipts=permutation_receipts,
        matched_capacity=MatchedCapacityReceipt(
            aligned_feature_count=aligned_count,
            permuted_control_feature_count=control_count,
            train_rows_each=len(train_indices),
            estimator_policy_sha256=plan.protocol.estimator_policy.policy_sha256,
        ),
        arm_evaluations=arm_evaluations,
        signal_evaluations=signals,
        disposition=_derive_disposition(
            signals=signals,
            policy=plan.protocol.acceptance_policy,
        ),
        completed_at=completed_at,
    )


def verify_structure_aware_experiment(
    *,
    plan: StructureAwareExperimentPlan,
    result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
) -> None:
    if result.plan_sha256 != plan.plan_sha256:
        raise ValueError("structure experiment result is bound to another plan")
    expected = run_structure_aware_experiment(
        plan=plan,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        completed_at=result.completed_at,
    )
    if expected != result:
        raise ValueError("structure experiment result differs from exact physical recomputation")


__all__ = [
    "CompositionFeatureMatrixReceipt",
    "MatchedCapacityReceipt",
    "MatchedEstimatorPolicy",
    "PermutationRoleReceipt",
    "StructureArmEvaluation",
    "StructureAwareExperimentPlan",
    "StructureAwareExperimentProtocol",
    "StructureAwareExperimentResult",
    "StructureBootstrapPolicy",
    "StructureDatasetContract",
    "StructureDatasetReceipt",
    "StructureEvaluationRole",
    "StructureExperimentArm",
    "StructureGroupSplitPolicy",
    "StructureSignalAcceptancePolicy",
    "StructureSignalDisposition",
    "StructureSignalEvaluation",
    "StructureSplitAssignment",
    "StructureSplitReceipt",
    "build_structure_aware_experiment_plan",
    "run_structure_aware_experiment",
    "structure_discrimination_implementation_components",
    "structure_discrimination_implementation_sha256",
    "verify_structure_aware_experiment",
]
