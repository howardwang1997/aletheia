"""Content-addressed material identity and multi-level split auditing.

Formula, crystal structure, synthesis batch, and physical sample are deliberately
different identity levels.  In particular, a formula never stands in for a sample
or a batch.  Builders in this module derive canonical identities; callers cannot
assert that a split is clean without the overlap and missing-identity checks being
recomputed by the frozen models.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import warnings
from datetime import datetime
from enum import Enum
from fractions import Fraction
from functools import reduce
from math import gcd
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class MaterialIdentityLevel(str, Enum):
    BATCH = "batch"
    CHEMICAL_SYSTEM = "chemical_system"
    FORMULA = "formula"
    RECORD = "record"
    SAMPLE = "sample"
    STRUCTURE = "structure"


class StructureQualityDisposition(str, Enum):
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"


class SplitAuditDisposition(str, Enum):
    CLEAN = "clean"
    REJECTED_IDENTITY_LEAKAGE = "rejected_identity_leakage"


class LicensedSourceArtifact(FrozenModel):
    """Exact source bytes plus explicit licence evidence.

    The model records, but does not remotely resolve, the source and licence URI.
    ``license_evidence_sha256`` binds the licence text or registry record that was
    actually inspected instead of trusting a free-form licence label alone.
    """

    schema_name: Literal["aletheia.material_source_artifact"] = "aletheia.material_source_artifact"
    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    sha256: str = Field(pattern=_SHA256_PATTERN)
    bytes: int = Field(ge=1, le=4 * 1024 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=256)
    source_uri: str = Field(min_length=1, max_length=2048)
    license_expression: str = Field(min_length=1, max_length=512)
    license_uri: str = Field(min_length=1, max_length=2048)
    license_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieved_at: AwareDatetime

    @model_validator(mode="after")
    def _strings_are_canonical(self) -> "LicensedSourceArtifact":
        values = (
            self.media_type,
            self.source_uri,
            self.license_expression,
            self.license_uri,
        )
        if any(value != value.strip() or "\n" in value or "\r" in value for value in values):
            raise ValueError("source and licence strings must be canonical single-line values")
        if ":" not in self.source_uri or ":" not in self.license_uri:
            raise ValueError("source and licence references must be absolute URIs")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)

    def verify_bytes(self, payload: bytes) -> None:
        if len(payload) != self.bytes or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ValueError("source artifact bytes do not match their immutable receipt")


class FormulaNormalizationPolicy(FrozenModel):
    schema_name: Literal["aletheia.formula_normalization_policy"] = (
        "aletheia.formula_normalization_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: Literal["pymatgen-element-integer-ratio-v1"] = "pymatgen-element-integer-ratio-v1"
    parser_package: Literal["pymatgen"] = "pymatgen"
    parser_version: str = Field(min_length=1, max_length=64)
    max_denominator: int = Field(default=10_000, ge=1, le=1_000_000)
    amount_tolerance: float = Field(default=1e-10, gt=0, le=1e-4)
    species_policy: Literal["element_only"] = "element_only"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class CanonicalElementAmount(FrozenModel):
    schema_version: Literal[1] = 1
    element: str = Field(pattern=r"^[A-Z][a-z]?$")
    integer_amount: int = Field(ge=1, le=10**12)


class FormulaIdentity(FrozenModel):
    schema_name: Literal["aletheia.formula_identity"] = "aletheia.formula_identity"
    schema_version: Literal[1] = 1
    input_formula: str = Field(min_length=1, max_length=1024)
    canonical_formula: str = Field(min_length=1, max_length=1024)
    chemical_system: str = Field(pattern=r"^[A-Z][A-Za-z]*(?:-[A-Z][A-Za-z]*)*$")
    composition: tuple[CanonicalElementAmount, ...] = Field(min_length=1)
    normalization_policy: FormulaNormalizationPolicy
    maximum_amount_error: float = Field(ge=0)

    @model_validator(mode="after")
    def _canonical_fields_are_derived(self) -> "FormulaIdentity":
        symbols = tuple(item.element for item in self.composition)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("canonical formula elements must be unique and sorted")
        amounts = tuple(item.integer_amount for item in self.composition)
        if reduce(gcd, amounts) != 1:
            raise ValueError("canonical formula amounts must be reduced to coprime integers")
        if self.chemical_system != "-".join(symbols):
            raise ValueError("chemical system must be derived from canonical elements")
        expected = "".join(
            item.element + (str(item.integer_amount) if item.integer_amount != 1 else "")
            for item in self.composition
        )
        if self.canonical_formula != expected:
            raise ValueError("canonical formula must be derived from element amounts")
        if self.maximum_amount_error > self.normalization_policy.amount_tolerance:
            raise ValueError("formula cannot be represented within the normalization tolerance")
        return self

    @property
    def formula_identity_sha256(self) -> str:
        """Representation-independent formula identity (raw input is excluded)."""

        return content_sha256(
            {
                "schema_name": self.schema_name,
                "schema_version": self.schema_version,
                "composition": [item.model_dump(mode="json") for item in self.composition],
                "normalization_policy_sha256": self.normalization_policy.policy_sha256,
            }
        )

    @property
    def chemical_system_identity_sha256(self) -> str:
        return content_sha256(
            {
                "identity_level": MaterialIdentityLevel.CHEMICAL_SYSTEM.value,
                "elements": [item.element for item in self.composition],
                "normalization_policy_sha256": self.normalization_policy.policy_sha256,
            }
        )

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class StructureNormalizationPolicy(FrozenModel):
    schema_name: Literal["aletheia.structure_normalization_policy"] = (
        "aletheia.structure_normalization_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: Literal["pymatgen-cif-conventional-standard-v1"] = (
        "pymatgen-cif-conventional-standard-v1"
    )
    parser_package: Literal["pymatgen"] = "pymatgen"
    parser_version: str = Field(min_length=1, max_length=64)
    symprec_angstrom: float = Field(default=1e-3, gt=0, le=0.1)
    angle_tolerance_degree: float = Field(default=5.0, gt=0, le=20)
    coordinate_decimals: int = Field(default=10, ge=6, le=14)
    lattice_decimals: int = Field(default=10, ge=6, le=14)
    canonical_cell: Literal["conventional_standard"] = "conventional_standard"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class StructureIdentity(FrozenModel):
    schema_name: Literal["aletheia.structure_identity"] = "aletheia.structure_identity"
    schema_version: Literal[1] = 1
    source: LicensedSourceArtifact
    formula: FormulaIdentity
    normalization_policy: StructureNormalizationPolicy
    canonical_structure_sha256: str = Field(pattern=_SHA256_PATTERN)
    site_count: int = Field(ge=1)
    ordered: bool
    space_group_symbol: str = Field(min_length=1, max_length=64)
    space_group_number: int = Field(ge=1, le=230)
    volume_per_atom_angstrom3: float = Field(gt=0)
    quality_flags: tuple[str, ...] = ()
    quality_disposition: StructureQualityDisposition

    @model_validator(mode="after")
    def _quality_is_derived(self) -> "StructureIdentity":
        if self.quality_flags != tuple(sorted(set(self.quality_flags))):
            raise ValueError("structure quality flags must be unique and sorted")
        expected = (
            StructureQualityDisposition.NEEDS_REVIEW
            if self.quality_flags
            else StructureQualityDisposition.ACCEPTED
        )
        if self.quality_disposition is not expected:
            raise ValueError("structure quality disposition must be derived from quality flags")
        if not math.isfinite(self.volume_per_atom_angstrom3):
            raise ValueError("structure volume per atom must be finite")
        return self

    @property
    def structure_identity_sha256(self) -> str:
        """Normalized structure identity; exact source bytes remain separately bound."""

        return content_sha256(
            {
                "schema_name": self.schema_name,
                "schema_version": self.schema_version,
                "formula_identity_sha256": self.formula.formula_identity_sha256,
                "canonical_structure_sha256": self.canonical_structure_sha256,
                "normalization_policy_sha256": self.normalization_policy.policy_sha256,
            }
        )

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class SynthesisBatchIdentity(FrozenModel):
    schema_name: Literal["aletheia.synthesis_batch_identity"] = "aletheia.synthesis_batch_identity"
    schema_version: Literal[1] = 1
    batch_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    issuer: str = Field(min_length=1, max_length=256)
    formula_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    structure_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    synthesis_record: LicensedSourceArtifact
    parent_batch_identity_sha256s: tuple[str, ...] = ()
    produced_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _batch_fields_are_canonical(self) -> "SynthesisBatchIdentity":
        if self.issuer != self.issuer.strip():
            raise ValueError("batch issuer must be canonical")
        if self.parent_batch_identity_sha256s != tuple(
            sorted(set(self.parent_batch_identity_sha256s))
        ):
            raise ValueError("parent batch identities must be unique and sorted")
        return self

    @property
    def batch_identity_sha256(self) -> str:
        return content_sha256(self)


class SampleIdentity(FrozenModel):
    schema_name: Literal["aletheia.sample_identity"] = "aletheia.sample_identity"
    schema_version: Literal[1] = 1
    sample_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    issuer: str = Field(min_length=1, max_length=256)
    batch_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    sample_record: LicensedSourceArtifact
    parent_sample_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _sample_fields_are_canonical(self) -> "SampleIdentity":
        if self.issuer != self.issuer.strip():
            raise ValueError("sample issuer must be canonical")
        return self

    @property
    def sample_identity_sha256(self) -> str:
        return content_sha256(self)


class MaterialRecordIdentity(FrozenModel):
    schema_name: Literal["aletheia.material_record_identity"] = "aletheia.material_record_identity"
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    source: LicensedSourceArtifact
    formula: FormulaIdentity
    structure: StructureIdentity | None = None
    batch: SynthesisBatchIdentity | None = None
    sample: SampleIdentity | None = None
    declared_missing_levels: tuple[MaterialIdentityLevel, ...] = ()

    @model_validator(mode="after")
    def _lineage_is_closed_and_missingness_is_explicit(self) -> "MaterialRecordIdentity":
        if self.structure is not None and (
            self.structure.formula.formula_identity_sha256 != self.formula.formula_identity_sha256
        ):
            raise ValueError("structure and material record formula identities differ")
        if self.batch is not None:
            if self.batch.formula_identity_sha256 != self.formula.formula_identity_sha256:
                raise ValueError("batch and material record formula identities differ")
            expected_structure = (
                self.structure.structure_identity_sha256 if self.structure is not None else None
            )
            if self.batch.structure_identity_sha256 != expected_structure:
                raise ValueError("batch and material record structure identities differ")
        if self.sample is not None:
            if self.batch is None:
                raise ValueError("sample identity requires its explicit synthesis batch")
            if self.sample.batch_identity_sha256 != self.batch.batch_identity_sha256:
                raise ValueError("sample identity is bound to another synthesis batch")
        expected_missing = tuple(
            sorted(
                (
                    level
                    for level, value in (
                        (MaterialIdentityLevel.STRUCTURE, self.structure),
                        (MaterialIdentityLevel.BATCH, self.batch),
                        (MaterialIdentityLevel.SAMPLE, self.sample),
                    )
                    if value is None
                ),
                key=lambda item: item.value,
            )
        )
        if self.declared_missing_levels != expected_missing:
            raise ValueError("missing material identity levels must be declared exactly")
        return self

    @property
    def record_identity_sha256(self) -> str:
        return content_sha256(self)

    def identity_at(self, level: MaterialIdentityLevel) -> str | None:
        if level is MaterialIdentityLevel.CHEMICAL_SYSTEM:
            return self.formula.chemical_system_identity_sha256
        if level is MaterialIdentityLevel.FORMULA:
            return self.formula.formula_identity_sha256
        if level is MaterialIdentityLevel.STRUCTURE:
            return None if self.structure is None else self.structure.structure_identity_sha256
        if level is MaterialIdentityLevel.BATCH:
            return None if self.batch is None else self.batch.batch_identity_sha256
        if level is MaterialIdentityLevel.SAMPLE:
            return None if self.sample is None else self.sample.sample_identity_sha256
        return self.record_identity_sha256


class MaterialSplitPolicy(FrozenModel):
    schema_name: Literal["aletheia.material_split_policy"] = "aletheia.material_split_policy"
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    required_identity_levels: tuple[MaterialIdentityLevel, ...] = Field(min_length=1)
    allowed_splits: tuple[str, ...] = Field(min_length=2)
    frozen_at: AwareDatetime
    missing_identity_policy: Literal["reject"] = "reject"
    cross_split_overlap_policy: Literal["reject"] = "reject"

    @model_validator(mode="after")
    def _sets_are_canonical(self) -> "MaterialSplitPolicy":
        expected_levels = tuple(sorted(set(self.required_identity_levels), key=lambda x: x.value))
        if self.required_identity_levels != expected_levels:
            raise ValueError("required identity levels must be unique and sorted")
        if self.allowed_splits != tuple(sorted(set(self.allowed_splits))):
            raise ValueError("allowed split labels must be unique and sorted")
        if MaterialIdentityLevel.RECORD not in self.required_identity_levels:
            raise ValueError("record identity must always be isolated across splits")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class MaterialSplitAssignment(FrozenModel):
    schema_version: Literal[1] = 1
    assignment_id: str = Field(pattern=r"^assignment-[0-9]{6}$")
    split: str = Field(min_length=1, max_length=128)
    record_id: str
    record_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    chemical_system_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    formula_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    structure_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    batch_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    sample_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    def identity_at(self, level: MaterialIdentityLevel) -> str | None:
        return {
            MaterialIdentityLevel.CHEMICAL_SYSTEM: self.chemical_system_identity_sha256,
            MaterialIdentityLevel.FORMULA: self.formula_identity_sha256,
            MaterialIdentityLevel.STRUCTURE: self.structure_identity_sha256,
            MaterialIdentityLevel.BATCH: self.batch_identity_sha256,
            MaterialIdentityLevel.SAMPLE: self.sample_identity_sha256,
            MaterialIdentityLevel.RECORD: self.record_identity_sha256,
        }[level]


class MissingSplitIdentity(FrozenModel):
    schema_version: Literal[1] = 1
    assignment_id: str
    split: str
    record_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    identity_level: MaterialIdentityLevel


class CrossSplitIdentityOverlap(FrozenModel):
    schema_version: Literal[1] = 1
    identity_level: MaterialIdentityLevel
    identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    splits: tuple[str, ...] = Field(min_length=2)
    record_identity_sha256s: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sets_are_canonical(self) -> "CrossSplitIdentityOverlap":
        if self.splits != tuple(sorted(set(self.splits))):
            raise ValueError("overlap split labels must be unique and sorted")
        if self.record_identity_sha256s != tuple(sorted(set(self.record_identity_sha256s))):
            raise ValueError("overlap record identities must be unique and sorted")
        return self


def _derive_split_findings(
    assignments: tuple[MaterialSplitAssignment, ...],
    policy: MaterialSplitPolicy,
) -> tuple[tuple[MissingSplitIdentity, ...], tuple[CrossSplitIdentityOverlap, ...]]:
    missing: list[MissingSplitIdentity] = []
    overlaps: list[CrossSplitIdentityOverlap] = []
    for level in policy.required_identity_levels:
        grouped: dict[str, list[MaterialSplitAssignment]] = {}
        for assignment in assignments:
            identity = assignment.identity_at(level)
            if identity is None:
                missing.append(
                    MissingSplitIdentity(
                        assignment_id=assignment.assignment_id,
                        split=assignment.split,
                        record_identity_sha256=assignment.record_identity_sha256,
                        identity_level=level,
                    )
                )
            else:
                grouped.setdefault(identity, []).append(assignment)
        for identity, members in grouped.items():
            splits = tuple(sorted({item.split for item in members}))
            if len(splits) > 1:
                overlaps.append(
                    CrossSplitIdentityOverlap(
                        identity_level=level,
                        identity_sha256=identity,
                        splits=splits,
                        record_identity_sha256s=tuple(
                            sorted({item.record_identity_sha256 for item in members})
                        ),
                    )
                )
    missing.sort(key=lambda item: (item.identity_level.value, item.assignment_id))
    overlaps.sort(key=lambda item: (item.identity_level.value, item.identity_sha256))
    return tuple(missing), tuple(overlaps)


class MaterialSplitLedger(FrozenModel):
    schema_name: Literal["aletheia.material_split_ledger"] = "aletheia.material_split_ledger"
    schema_version: Literal[1] = 1
    ledger_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    dataset_source: LicensedSourceArtifact
    policy: MaterialSplitPolicy
    assignments: tuple[MaterialSplitAssignment, ...] = Field(min_length=2)
    missing_identities: tuple[MissingSplitIdentity, ...]
    cross_split_overlaps: tuple[CrossSplitIdentityOverlap, ...]
    membership_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: SplitAuditDisposition
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _audit_is_recomputed(self) -> "MaterialSplitLedger":
        assignment_ids = tuple(item.assignment_id for item in self.assignments)
        if assignment_ids != tuple(sorted(set(assignment_ids))):
            raise ValueError("split assignments must have unique sorted assignment IDs")
        if any(item.split not in self.policy.allowed_splits for item in self.assignments):
            raise ValueError("split assignment uses a label outside the frozen policy")
        represented = {item.split for item in self.assignments}
        if len(represented) < 2:
            raise ValueError("material split ledger must represent at least two splits")
        missing, overlaps = _derive_split_findings(self.assignments, self.policy)
        if self.missing_identities != missing or self.cross_split_overlaps != overlaps:
            raise ValueError("material split findings were not mechanically derived")
        expected_membership = content_sha256(
            {
                "policy_sha256": self.policy.policy_sha256,
                "assignments": [item.model_dump(mode="json") for item in self.assignments],
            }
        )
        if self.membership_sha256 != expected_membership:
            raise ValueError("material split membership hash is invalid")
        expected_disposition = (
            SplitAuditDisposition.REJECTED_IDENTITY_LEAKAGE
            if missing or overlaps
            else SplitAuditDisposition.CLEAN
        )
        if self.disposition is not expected_disposition:
            raise ValueError("split disposition must be derived from identity findings")
        return self

    @property
    def ledger_sha256(self) -> str:
        return content_sha256(self)


def default_formula_normalization_policy() -> FormulaNormalizationPolicy:
    return FormulaNormalizationPolicy(parser_version=importlib.metadata.version("pymatgen"))


def default_structure_normalization_policy() -> StructureNormalizationPolicy:
    return StructureNormalizationPolicy(parser_version=importlib.metadata.version("pymatgen"))


def normalize_formula(
    formula: str,
    *,
    policy: FormulaNormalizationPolicy | None = None,
) -> FormulaIdentity:
    """Parse an elemental formula into a reduced, exact integer-ratio identity."""

    from pymatgen.core import Composition

    policy = policy or default_formula_normalization_policy()
    if not formula or formula != formula.strip():
        raise ValueError("formula must be a non-empty canonical string")
    try:
        composition = Composition(formula).element_composition
    except Exception as error:
        raise ValueError("formula cannot be parsed as an elemental composition") from error
    amounts = composition.get_el_amt_dict()
    if not amounts or any(not math.isfinite(value) or value <= 0 for value in amounts.values()):
        raise ValueError("formula must contain finite positive element amounts")
    fractions = {
        symbol: Fraction(str(value)).limit_denominator(policy.max_denominator)
        for symbol, value in amounts.items()
    }
    common_denominator = math.lcm(*(item.denominator for item in fractions.values()))
    integers = {
        symbol: item.numerator * (common_denominator // item.denominator)
        for symbol, item in fractions.items()
    }
    divisor = reduce(gcd, integers.values())
    integers = {symbol: amount // divisor for symbol, amount in integers.items()}
    scale_candidates = [amounts[symbol] / integers[symbol] for symbol in integers]
    scale = sum(scale_candidates) / len(scale_candidates)
    maximum_error = max(abs(amounts[symbol] - scale * integers[symbol]) for symbol in integers)
    canonical = tuple(
        CanonicalElementAmount(element=symbol, integer_amount=integers[symbol])
        for symbol in sorted(integers)
    )
    canonical_formula = "".join(
        item.element + (str(item.integer_amount) if item.integer_amount != 1 else "")
        for item in canonical
    )
    return FormulaIdentity(
        input_formula=formula,
        canonical_formula=canonical_formula,
        chemical_system="-".join(item.element for item in canonical),
        composition=canonical,
        normalization_policy=policy,
        maximum_amount_error=maximum_error,
    )


def _rounded(value: float, decimals: int) -> float:
    rounded = round(float(value), decimals)
    return 0.0 if rounded == -0.0 else rounded


def build_structure_identity_from_cif(
    *,
    cif_bytes: bytes,
    source: LicensedSourceArtifact,
    expected_formula: FormulaIdentity | None = None,
    formula_policy: FormulaNormalizationPolicy | None = None,
    structure_policy: StructureNormalizationPolicy | None = None,
) -> StructureIdentity:
    """Parse and standardize exactly one CIF structure into a stable local identity.

    This is a versioned local canonicalization contract, not a claim that arbitrary
    crystallographic files have one globally universal byte representation.
    """

    from pymatgen.io.cif import CifParser
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    source.verify_bytes(cif_bytes)
    formula_policy = formula_policy or default_formula_normalization_policy()
    structure_policy = structure_policy or default_structure_normalization_policy()
    if structure_policy.parser_version != formula_policy.parser_version:
        raise ValueError("formula and structure parsers must use the same pymatgen version")
    try:
        cif_text = cif_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CIF artifact is not valid UTF-8") from error
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            parser = CifParser.from_str(cif_text, check_cif=True)
            structures = parser.parse_structures(primitive=False, on_error="raise")
        except Exception as error:
            raise ValueError("CIF artifact cannot be parsed into a structure") from error
    if len(structures) != 1:
        raise ValueError("one structure identity requires exactly one CIF data structure")
    structure = structures[0]
    formula = normalize_formula(
        structure.composition.element_composition.formula.replace(" ", ""),
        policy=formula_policy,
    )
    if expected_formula is not None and (
        formula.formula_identity_sha256 != expected_formula.formula_identity_sha256
    ):
        raise ValueError("CIF composition differs from the expected formula identity")
    analyzer = SpacegroupAnalyzer(
        structure,
        symprec=structure_policy.symprec_angstrom,
        angle_tolerance=structure_policy.angle_tolerance_degree,
    )
    try:
        standardized = analyzer.get_conventional_standard_structure(
            international_monoclinic=True,
            keep_site_properties=False,
        )
        canonical_analyzer = SpacegroupAnalyzer(
            standardized,
            symprec=structure_policy.symprec_angstrom,
            angle_tolerance=structure_policy.angle_tolerance_degree,
        )
        symbol = canonical_analyzer.get_space_group_symbol()
        number = canonical_analyzer.get_space_group_number()
    except Exception as error:
        raise ValueError("CIF structure cannot be symmetry-standardized") from error
    sites = []
    for site in standardized:
        species = tuple(
            sorted(
                (str(specie), _rounded(occupancy, structure_policy.coordinate_decimals))
                for specie, occupancy in site.species.items()
            )
        )
        coordinates = tuple(
            _rounded(float(value) % 1.0, structure_policy.coordinate_decimals)
            for value in site.frac_coords
        )
        coordinates = tuple(0.0 if abs(value - 1.0) < 10**-12 else value for value in coordinates)
        sites.append({"species": species, "fractional_coordinates": coordinates})
    sites.sort(key=lambda item: (item["species"], item["fractional_coordinates"]))
    lattice = {
        "a": _rounded(standardized.lattice.a, structure_policy.lattice_decimals),
        "b": _rounded(standardized.lattice.b, structure_policy.lattice_decimals),
        "c": _rounded(standardized.lattice.c, structure_policy.lattice_decimals),
        "alpha": _rounded(standardized.lattice.alpha, structure_policy.lattice_decimals),
        "beta": _rounded(standardized.lattice.beta, structure_policy.lattice_decimals),
        "gamma": _rounded(standardized.lattice.gamma, structure_policy.lattice_decimals),
    }
    canonical_hash = content_sha256(
        {
            "normalization_policy_sha256": structure_policy.policy_sha256,
            "space_group_number": number,
            "lattice": lattice,
            "sites": sites,
        }
    )
    flags = set()
    if caught:
        flags.add("cif_parser_warning")
    if not structure.is_ordered:
        flags.add("disordered_or_partial_occupancy")
    volume_per_atom = float(standardized.volume / len(standardized))
    if volume_per_atom < 1.0 or volume_per_atom > 1_000.0:
        flags.add("implausible_volume_per_atom")
    quality_flags = tuple(sorted(flags))
    return StructureIdentity(
        source=source,
        formula=formula,
        normalization_policy=structure_policy,
        canonical_structure_sha256=canonical_hash,
        site_count=len(standardized),
        ordered=bool(structure.is_ordered),
        space_group_symbol=symbol,
        space_group_number=number,
        volume_per_atom_angstrom3=volume_per_atom,
        quality_flags=quality_flags,
        quality_disposition=(
            StructureQualityDisposition.NEEDS_REVIEW
            if quality_flags
            else StructureQualityDisposition.ACCEPTED
        ),
    )


def build_material_split_ledger(
    *,
    ledger_id: str,
    dataset_source: LicensedSourceArtifact,
    policy: MaterialSplitPolicy,
    records: tuple[tuple[str, MaterialRecordIdentity], ...],
    created_at: datetime,
) -> MaterialSplitLedger:
    """Create a content-addressed split audit across every required identity level."""

    if any(split not in policy.allowed_splits for split, _ in records):
        raise ValueError("material record uses a split outside the frozen policy")
    ordered = sorted(records, key=lambda item: (item[0], item[1].record_identity_sha256))
    assignments = tuple(
        MaterialSplitAssignment(
            assignment_id=f"assignment-{index:06d}",
            split=split,
            record_id=record.record_id,
            record_identity_sha256=record.record_identity_sha256,
            chemical_system_identity_sha256=(record.formula.chemical_system_identity_sha256),
            formula_identity_sha256=record.formula.formula_identity_sha256,
            structure_identity_sha256=record.identity_at(MaterialIdentityLevel.STRUCTURE),
            batch_identity_sha256=record.identity_at(MaterialIdentityLevel.BATCH),
            sample_identity_sha256=record.identity_at(MaterialIdentityLevel.SAMPLE),
        )
        for index, (split, record) in enumerate(ordered, start=1)
    )
    missing, overlaps = _derive_split_findings(assignments, policy)
    membership = content_sha256(
        {
            "policy_sha256": policy.policy_sha256,
            "assignments": [item.model_dump(mode="json") for item in assignments],
        }
    )
    return MaterialSplitLedger(
        ledger_id=ledger_id,
        dataset_source=dataset_source,
        policy=policy,
        assignments=assignments,
        missing_identities=missing,
        cross_split_overlaps=overlaps,
        membership_sha256=membership,
        disposition=(
            SplitAuditDisposition.REJECTED_IDENTITY_LEAKAGE
            if missing or overlaps
            else SplitAuditDisposition.CLEAN
        ),
        created_at=created_at,
    )


__all__ = [
    "CanonicalElementAmount",
    "CrossSplitIdentityOverlap",
    "FormulaIdentity",
    "FormulaNormalizationPolicy",
    "LicensedSourceArtifact",
    "MaterialIdentityLevel",
    "MaterialRecordIdentity",
    "MaterialSplitAssignment",
    "MaterialSplitLedger",
    "MaterialSplitPolicy",
    "MissingSplitIdentity",
    "SampleIdentity",
    "SplitAuditDisposition",
    "StructureIdentity",
    "StructureNormalizationPolicy",
    "StructureQualityDisposition",
    "SynthesisBatchIdentity",
    "build_material_split_ledger",
    "build_structure_identity_from_cif",
    "default_formula_normalization_policy",
    "default_structure_normalization_policy",
    "normalize_formula",
]
