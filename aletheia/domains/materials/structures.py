"""Structure quality gates and species-blind geometry features for F10-S4."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from aletheia.domains.materials.identity import FormulaIdentity, normalize_formula
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


class StructureRowDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_QUALITY = "rejected_quality"


class StructureDatasetQualityDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_ROWS = "rejected_rows"


class StructureQualityPolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_quality_policy"] = "aletheia.structure_quality_policy"
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    require_ordered: Literal[True] = True
    minimum_sites: int = Field(default=1, ge=1)
    maximum_sites: int = Field(default=256, ge=1, le=100_000)
    minimum_volume_per_atom_angstrom3: float = Field(default=1.0, gt=0)
    maximum_volume_per_atom_angstrom3: float = Field(default=1_000.0, gt=0)
    minimum_distinct_site_distance_angstrom: float = Field(default=0.25, gt=0)
    maximum_lattice_condition_number: float = Field(default=100.0, gt=1)
    symprec_angstrom: float = Field(default=1e-3, gt=0, le=0.1)
    angle_tolerance_degree: float = Field(default=5.0, gt=0, le=20)
    coordinate_decimals: int = Field(default=10, ge=6, le=14)
    lattice_decimals: int = Field(default=10, ge=6, le=14)
    standardized_cell: Literal["primitive_standard"] = "primitive_standard"

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> "StructureQualityPolicy":
        if self.maximum_sites < self.minimum_sites:
            raise ValueError("maximum sites must be at least minimum sites")
        if self.maximum_volume_per_atom_angstrom3 <= self.minimum_volume_per_atom_angstrom3:
            raise ValueError("structure volume bounds are not ordered")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class StructureRowReceipt(FrozenModel):
    schema_name: Literal["aletheia.structure_row_receipt"] = "aletheia.structure_row_receipt"
    schema_version: Literal[1] = 1
    row_position: int = Field(ge=0)
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formula: FormulaIdentity
    canonical_structure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    site_count: int = Field(ge=1)
    standardized_site_count: int = Field(ge=1)
    ordered: bool
    volume_per_atom_angstrom3: float = Field(gt=0)
    minimum_distinct_site_distance_angstrom: float | None = Field(default=None, ge=0)
    lattice_condition_number: float = Field(gt=0)
    space_group_number: int = Field(ge=1, le=230)
    quality_flags: tuple[str, ...]
    disposition: StructureRowDisposition

    @model_validator(mode="after")
    def _disposition_is_derived(self) -> "StructureRowReceipt":
        if self.quality_flags != tuple(sorted(set(self.quality_flags))):
            raise ValueError("structure row quality flags must be unique and sorted")
        expected = (
            StructureRowDisposition.REJECTED_QUALITY
            if self.quality_flags
            else StructureRowDisposition.ACCEPTED
        )
        if self.disposition is not expected:
            raise ValueError("structure row disposition must be derived from quality flags")
        numeric = (
            self.volume_per_atom_angstrom3,
            self.lattice_condition_number,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("structure row quality metrics must be finite")
        return self

    @property
    def row_receipt_sha256(self) -> str:
        return content_sha256(self)


class StructureDatasetQualityLedger(FrozenModel):
    schema_name: Literal["aletheia.structure_dataset_quality_ledger"] = (
        "aletheia.structure_dataset_quality_ledger"
    )
    schema_version: Literal[1] = 1
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: StructureQualityPolicy
    rows: tuple[StructureRowReceipt, ...] = Field(min_length=1)
    accepted_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    row_membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: StructureDatasetQualityDisposition

    @model_validator(mode="after")
    def _summary_is_derived(self) -> "StructureDatasetQualityLedger":
        positions = tuple(item.row_position for item in self.rows)
        if positions != tuple(range(len(self.rows))):
            raise ValueError("structure quality rows must be a complete ordered position ledger")
        accepted = sum(item.disposition is StructureRowDisposition.ACCEPTED for item in self.rows)
        rejected = len(self.rows) - accepted
        if self.accepted_rows != accepted or self.rejected_rows != rejected:
            raise ValueError("structure quality row counts are not derived")
        expected_membership = content_sha256(
            {
                "dataset_sha256": self.dataset_sha256,
                "policy_sha256": self.policy.policy_sha256,
                "row_receipt_sha256s": [item.row_receipt_sha256 for item in self.rows],
            }
        )
        if self.row_membership_sha256 != expected_membership:
            raise ValueError("structure quality membership hash is invalid")
        expected_disposition = (
            StructureDatasetQualityDisposition.REJECTED_ROWS
            if rejected
            else StructureDatasetQualityDisposition.ACCEPTED
        )
        if self.disposition is not expected_disposition:
            raise ValueError("structure dataset quality disposition is not derived")
        return self

    @property
    def ledger_sha256(self) -> str:
        return content_sha256(self)


class StructureGeometryFeaturePolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_geometry_feature_policy"] = (
        "aletheia.structure_geometry_feature_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    radial_bin_edges_angstrom: tuple[float, ...] = Field(min_length=3, max_length=65)
    include_symmetry: Literal[True] = True
    species_blind: Literal[True] = True
    standardization: Literal["primitive_standard"] = "primitive_standard"

    @model_validator(mode="after")
    def _radial_edges_are_canonical(self) -> "StructureGeometryFeaturePolicy":
        if self.radial_bin_edges_angstrom != tuple(sorted(set(self.radial_bin_edges_angstrom))):
            raise ValueError("radial bin edges must be unique and sorted")
        if self.radial_bin_edges_angstrom[0] != 0:
            raise ValueError("radial histogram must start at zero angstrom")
        if any(not math.isfinite(value) or value < 0 for value in self.radial_bin_edges_angstrom):
            raise ValueError("radial bin edges must be finite and nonnegative")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class StructureFeatureMatrixReceipt(FrozenModel):
    schema_name: Literal["aletheia.structure_feature_matrix_receipt"] = (
        "aletheia.structure_feature_matrix_receipt"
    )
    schema_version: Literal[1] = 1
    quality_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=1)
    feature_count: int = Field(ge=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    feature_names_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _feature_names_are_closed(self) -> "StructureFeatureMatrixReceipt":
        if self.feature_count != len(self.feature_names):
            raise ValueError("structure feature count differs from names")
        if self.feature_names != tuple(dict.fromkeys(self.feature_names)):
            raise ValueError("structure feature names must be unique and ordered")
        if self.feature_names_sha256 != content_sha256(list(self.feature_names)):
            raise ValueError("structure feature-name hash is invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _rounded(value: float, decimals: int) -> float:
    result = round(float(value), decimals)
    return 0.0 if result == -0.0 else result


def _standardize(structure: Any, policy: StructureQualityPolicy) -> tuple[Any, int]:
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    analyzer = SpacegroupAnalyzer(
        structure,
        symprec=policy.symprec_angstrom,
        angle_tolerance=policy.angle_tolerance_degree,
    )
    standardized = analyzer.get_primitive_standard_structure(
        international_monoclinic=True,
        keep_site_properties=False,
    )
    canonical_analyzer = SpacegroupAnalyzer(
        standardized,
        symprec=policy.symprec_angstrom,
        angle_tolerance=policy.angle_tolerance_degree,
    )
    return standardized, canonical_analyzer.get_space_group_number()


def inspect_structure_row(
    *,
    row_position: int,
    structure: Any,
    policy: StructureQualityPolicy,
) -> StructureRowReceipt:
    """Derive exact row identity and fail-closed structure quality findings."""

    import numpy as np

    source_row_hash = content_sha256(structure.as_dict())
    formula = normalize_formula(structure.composition.element_composition.formula.replace(" ", ""))
    site_count = len(structure)
    volume_per_atom = float(structure.volume / site_count)
    lattice_condition = float(np.linalg.cond(np.asarray(structure.lattice.matrix, dtype=float)))
    distances = np.asarray(structure.distance_matrix, dtype=float)
    off_diagonal = distances[~np.eye(site_count, dtype=bool)]
    minimum_distance = float(off_diagonal.min()) if off_diagonal.size else None
    flags: set[str] = set()
    if policy.require_ordered and not structure.is_ordered:
        flags.add("disordered_or_partial_occupancy")
    if not policy.minimum_sites <= site_count <= policy.maximum_sites:
        flags.add("site_count_out_of_range")
    if not (
        math.isfinite(volume_per_atom)
        and policy.minimum_volume_per_atom_angstrom3
        <= volume_per_atom
        <= policy.maximum_volume_per_atom_angstrom3
    ):
        flags.add("volume_per_atom_out_of_range")
    if not math.isfinite(lattice_condition) or (
        lattice_condition > policy.maximum_lattice_condition_number
    ):
        flags.add("ill_conditioned_lattice")
    if (
        minimum_distance is not None
        and minimum_distance < policy.minimum_distinct_site_distance_angstrom
    ):
        flags.add("overlapping_distinct_sites")
    try:
        standardized, space_group_number = _standardize(structure, policy)
    except Exception:
        standardized = structure
        space_group_number = 1
        flags.add("symmetry_standardization_failed")
    sites = []
    for site in standardized:
        species = tuple(
            sorted(
                (str(specie), _rounded(occupancy, policy.coordinate_decimals))
                for specie, occupancy in site.species.items()
            )
        )
        coordinates = tuple(
            _rounded(float(value) % 1.0, policy.coordinate_decimals) for value in site.frac_coords
        )
        coordinates = tuple(0.0 if abs(value - 1.0) < 10**-12 else value for value in coordinates)
        sites.append({"species": species, "fractional_coordinates": coordinates})
    sites.sort(key=lambda item: (item["species"], item["fractional_coordinates"]))
    canonical_hash = content_sha256(
        {
            "quality_policy_sha256": policy.policy_sha256,
            "lattice_matrix": [
                [_rounded(value, policy.lattice_decimals) for value in row]
                for row in standardized.lattice.matrix
            ],
            "space_group_number": space_group_number,
            "sites": sites,
        }
    )
    quality_flags = tuple(sorted(flags))
    return StructureRowReceipt(
        row_position=row_position,
        source_row_sha256=source_row_hash,
        formula=formula,
        canonical_structure_sha256=canonical_hash,
        site_count=site_count,
        standardized_site_count=len(standardized),
        ordered=bool(structure.is_ordered),
        volume_per_atom_angstrom3=volume_per_atom,
        minimum_distinct_site_distance_angstrom=minimum_distance,
        lattice_condition_number=lattice_condition,
        space_group_number=space_group_number,
        quality_flags=quality_flags,
        disposition=(
            StructureRowDisposition.REJECTED_QUALITY
            if quality_flags
            else StructureRowDisposition.ACCEPTED
        ),
    )


def build_structure_quality_ledger(
    *,
    dataset_sha256: str,
    structures: tuple[Any, ...],
    policy: StructureQualityPolicy,
) -> StructureDatasetQualityLedger:
    rows = tuple(
        inspect_structure_row(row_position=index, structure=structure, policy=policy)
        for index, structure in enumerate(structures)
    )
    accepted = sum(item.disposition is StructureRowDisposition.ACCEPTED for item in rows)
    rejected = len(rows) - accepted
    membership = content_sha256(
        {
            "dataset_sha256": dataset_sha256,
            "policy_sha256": policy.policy_sha256,
            "row_receipt_sha256s": [item.row_receipt_sha256 for item in rows],
        }
    )
    return StructureDatasetQualityLedger(
        dataset_sha256=dataset_sha256,
        policy=policy,
        rows=rows,
        accepted_rows=accepted,
        rejected_rows=rejected,
        row_membership_sha256=membership,
        disposition=(
            StructureDatasetQualityDisposition.REJECTED_ROWS
            if rejected
            else StructureDatasetQualityDisposition.ACCEPTED
        ),
    )


def structure_geometry_feature_names(
    policy: StructureGeometryFeaturePolicy,
) -> tuple[str, ...]:
    bins = tuple(
        f"rdf_count_per_atom_{left:g}_{right:g}_angstrom"
        for left, right in zip(
            policy.radial_bin_edges_angstrom[:-1],
            policy.radial_bin_edges_angstrom[1:],
            strict=True,
        )
    )
    return (
        "volume_per_atom_angstrom3",
        "lattice_b_over_a",
        "lattice_c_over_a",
        "lattice_alpha_degree",
        "lattice_beta_degree",
        "lattice_gamma_degree",
        "space_group_number",
        "nearest_neighbor_min_angstrom",
        "nearest_neighbor_mean_angstrom",
        "nearest_neighbor_std_angstrom",
        "nearest_neighbor_max_angstrom",
        *tuple(f"crystal_system_{item}" for item in range(1, 8)),
        *bins,
    )


def extract_structure_geometry_features(
    *,
    structure: Any,
    quality_policy: StructureQualityPolicy,
    feature_policy: StructureGeometryFeaturePolicy,
) -> tuple[float, ...]:
    """Extract fixed-size species-blind geometry from the standard primitive cell."""

    import numpy as np
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    standardized, space_group_number = _standardize(structure, quality_policy)
    analyzer = SpacegroupAnalyzer(
        standardized,
        symprec=quality_policy.symprec_angstrom,
        angle_tolerance=quality_policy.angle_tolerance_degree,
    )
    crystal_system_index = {
        "triclinic": 1,
        "monoclinic": 2,
        "orthorhombic": 3,
        "tetragonal": 4,
        "trigonal": 5,
        "hexagonal": 6,
        "cubic": 7,
    }[analyzer.get_crystal_system()]
    maximum_radius = feature_policy.radial_bin_edges_angstrom[-1]
    neighbor_distances = [
        float(neighbor.nn_distance)
        for site in standardized
        for neighbor in standardized.get_neighbors(site, maximum_radius)
        if neighbor.nn_distance > 1e-12
    ]
    if not neighbor_distances:
        raise ValueError("standardized structure has no periodic neighbor within radial policy")
    distances = np.asarray(neighbor_distances, dtype=float)
    histogram, _ = np.histogram(
        distances,
        bins=np.asarray(feature_policy.radial_bin_edges_angstrom, dtype=float),
    )
    a = float(standardized.lattice.a)
    features = (
        float(standardized.volume / len(standardized)),
        float(standardized.lattice.b / a),
        float(standardized.lattice.c / a),
        float(standardized.lattice.alpha),
        float(standardized.lattice.beta),
        float(standardized.lattice.gamma),
        float(space_group_number),
        float(distances.min()),
        float(distances.mean()),
        float(distances.std(ddof=0)),
        float(distances.max()),
        *(1.0 if index == crystal_system_index else 0.0 for index in range(1, 8)),
        *(float(value) / len(standardized) for value in histogram.tolist()),
    )
    if len(features) != len(structure_geometry_feature_names(feature_policy)) or any(
        not math.isfinite(value) for value in features
    ):
        raise ValueError("structure geometry featurization produced invalid output")
    return features


def build_structure_feature_matrix(
    *,
    structures: tuple[Any, ...],
    quality_ledger: StructureDatasetQualityLedger,
    feature_policy: StructureGeometryFeaturePolicy,
) -> tuple[Any, StructureFeatureMatrixReceipt]:
    """Build and content-hash a matrix only after the complete quality ledger passes."""

    import numpy as np

    if quality_ledger.disposition is not StructureDatasetQualityDisposition.ACCEPTED:
        raise ValueError("structure feature extraction requires an all-accepted quality ledger")
    if len(structures) != len(quality_ledger.rows):
        raise ValueError("structure feature rows differ from quality ledger")
    matrix = np.ascontiguousarray(
        [
            extract_structure_geometry_features(
                structure=structure,
                quality_policy=quality_ledger.policy,
                feature_policy=feature_policy,
            )
            for structure in structures
        ],
        dtype=np.float64,
    )
    names = structure_geometry_feature_names(feature_policy)
    receipt = StructureFeatureMatrixReceipt(
        quality_ledger_sha256=quality_ledger.ledger_sha256,
        feature_policy_sha256=feature_policy.policy_sha256,
        row_count=matrix.shape[0],
        feature_count=matrix.shape[1],
        feature_names=names,
        feature_names_sha256=content_sha256(list(names)),
        matrix_sha256=content_sha256(
            {
                "shape": list(matrix.shape),
                "dtype": str(matrix.dtype),
                "bytes_sha256": __import__("hashlib").sha256(matrix.tobytes()).hexdigest(),
            }
        ),
    )
    return matrix, receipt


__all__ = [
    "StructureDatasetQualityDisposition",
    "StructureDatasetQualityLedger",
    "StructureFeatureMatrixReceipt",
    "StructureGeometryFeaturePolicy",
    "StructureQualityPolicy",
    "StructureRowDisposition",
    "StructureRowReceipt",
    "build_structure_feature_matrix",
    "build_structure_quality_ledger",
    "extract_structure_geometry_features",
    "inspect_structure_row",
    "structure_geometry_feature_names",
]
