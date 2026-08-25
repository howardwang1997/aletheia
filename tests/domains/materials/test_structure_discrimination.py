"""Gold and adversarial tests for the F10-S4 matched structure experiment."""

from __future__ import annotations

import hashlib
import importlib.metadata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError
from pymatgen.core import Lattice, Structure

from aletheia.domains.materials.capabilities.structure_discrimination import (
    MatchedEstimatorPolicy,
    StructureAwareExperimentProtocol,
    StructureAwareExperimentResult,
    StructureBootstrapPolicy,
    StructureDatasetContract,
    StructureEvaluationRole,
    StructureExperimentArm,
    StructureGroupSplitPolicy,
    StructureSignalAcceptancePolicy,
    build_structure_aware_experiment_plan,
    run_structure_aware_experiment,
    structure_discrimination_implementation_components,
    structure_discrimination_implementation_sha256,
    verify_structure_aware_experiment,
)
from aletheia.domains.materials.structures import (
    StructureDatasetQualityDisposition,
    StructureGeometryFeaturePolicy,
    StructureQualityPolicy,
    StructureRowDisposition,
    build_structure_feature_matrix,
    build_structure_quality_ledger,
    inspect_structure_row,
)


BASE = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
DATASET_BYTES = b"synthetic structure-aware gold dataset v1"


def sha(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def synthetic_dataset(*, target_offset: float = 0.0) -> pd.DataFrame:
    systems = (
        ("Li", "F"),
        ("Na", "Cl"),
        ("K", "Br"),
        ("Mg", "O"),
        ("Ca", "S"),
        ("Al", "N"),
        ("Ga", "P"),
        ("Zn", "Se"),
        ("Cd", "Te"),
        ("Si", "C"),
        ("B", "N"),
        ("Sr", "O"),
    )
    structures = []
    targets = []
    for system_index, species in enumerate(systems):
        for replicate in range(4):
            lattice_constant = 3.2 + 0.08 * system_index + 0.14 * replicate
            structures.append(
                Structure(
                    Lattice.cubic(lattice_constant),
                    species,
                    ((0.0, 0.0, 0.0), (0.5, 0.5, 0.5)),
                )
            )
            targets.append(70.0 * lattice_constant + 2.5 * system_index + target_offset)
    return pd.DataFrame({"structure": structures, "target": targets})


def protocol(*, rows: int = 48, implementation_sha256: str | None = None):
    return StructureAwareExperimentProtocol(
        protocol_id="synthetic-structure-signal-v1",
        dataset=StructureDatasetContract(
            dataset_ref="fixture://structure-signal",
            artifact_id="synthetic-structure-signal-v1",
            media_type="application/octet-stream",
            expected_file_sha256=sha(DATASET_BYTES),
            expected_row_count=rows,
            structure_column="structure",
            target_column="target",
            target_quantity_kind_id="fixture.phonon_frequency",
            target_unit_ucum="cm-1",
            source_uri="https://example.test/structure-signal",
            license_expression="CC0-1.0",
            license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
            license_evidence_sha256=sha("CC0 gold evidence"),
        ),
        quality_policy=StructureQualityPolicy(
            policy_id="synthetic-structure-quality-v1",
            maximum_sites=16,
        ),
        geometry_feature_policy=StructureGeometryFeaturePolicy(
            policy_id="synthetic-geometry-features-v1",
            radial_bin_edges_angstrom=(0.0, 1.5, 2.5, 3.5, 5.0, 8.0),
        ),
        split_policy=StructureGroupSplitPolicy(
            seed=20260823,
            train_fraction=0.5,
            internal_validation_fraction=0.25,
            locked_holdout_fraction=0.25,
            minimum_rows_per_role=8,
        ),
        estimator_policy=MatchedEstimatorPolicy(
            n_estimators=48,
            max_depth=10,
            min_samples_leaf=1,
            random_state=20260823,
        ),
        bootstrap_policy=StructureBootstrapPolicy(
            resamples=200,
            confidence_level=0.9,
            seed=20260824,
        ),
        acceptance_policy=StructureSignalAcceptancePolicy(
            minimum_relative_mae_improvement=0.01,
        ),
        permutation_seed=20260825,
        implementation_sha256=(
            implementation_sha256 or structure_discrimination_implementation_sha256()
        ),
        required_package_versions={
            name: importlib.metadata.version(name)
            for name in ("matminer", "numpy", "pandas", "pymatgen", "scikit-learn", "spglib")
        },
        frozen_at=BASE,
    )


def plan_and_result(*, target_offset: float = 0.0):
    dataframe = synthetic_dataset(target_offset=target_offset)
    plan = build_structure_aware_experiment_plan(
        plan_id="synthetic-structure-plan-v1",
        protocol=protocol(),
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        prepared_at=BASE + timedelta(minutes=1),
    )
    result = run_structure_aware_experiment(
        plan=plan,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        completed_at=BASE + timedelta(minutes=2),
    )
    return dataframe, plan, result


def test_structure_quality_and_features_distinguish_same_formula_geometry():
    compact = Structure(
        Lattice.cubic(3.5),
        ("Na", "Cl"),
        ((0, 0, 0), (0.5, 0.5, 0.5)),
    )
    expanded = Structure(
        Lattice.cubic(4.5),
        ("Na", "Cl"),
        ((0, 0, 0), (0.5, 0.5, 0.5)),
    )
    quality_policy = protocol(rows=2).quality_policy
    ledger = build_structure_quality_ledger(
        dataset_sha256=sha("two-structures"),
        structures=(compact, expanded),
        policy=quality_policy,
    )
    matrix, receipt = build_structure_feature_matrix(
        structures=(compact, expanded),
        quality_ledger=ledger,
        feature_policy=protocol(rows=2).geometry_feature_policy,
    )
    assert ledger.disposition is StructureDatasetQualityDisposition.ACCEPTED
    assert (
        ledger.rows[0].formula.formula_identity_sha256
        == ledger.rows[1].formula.formula_identity_sha256
    )
    assert ledger.rows[0].canonical_structure_sha256 != ledger.rows[1].canonical_structure_sha256
    assert receipt.row_count == 2
    assert (matrix[0] != matrix[1]).any()


def test_disorder_and_overlapping_sites_fail_the_quality_gate():
    disordered = Structure(
        Lattice.cubic(4.0),
        ({"Na": 0.5, "K": 0.5}, "Cl"),
        ((0, 0, 0), (0.5, 0.5, 0.5)),
    )
    overlap = Structure(
        Lattice.cubic(4.0),
        ("Na", "Cl"),
        ((0, 0, 0), (0, 0, 0)),
    )
    quality = protocol(rows=2).quality_policy
    first = inspect_structure_row(row_position=0, structure=disordered, policy=quality)
    second = inspect_structure_row(row_position=1, structure=overlap, policy=quality)
    assert first.disposition is StructureRowDisposition.REJECTED_QUALITY
    assert "disordered_or_partial_occupancy" in first.quality_flags
    assert second.disposition is StructureRowDisposition.REJECTED_QUALITY
    assert "overlapping_distinct_sites" in second.quality_flags


def test_split_is_target_blind_complete_and_chemical_system_disjoint():
    dataframe = synthetic_dataset()
    shifted = synthetic_dataset(target_offset=10_000)
    first = build_structure_aware_experiment_plan(
        plan_id="target-blind-plan-a",
        protocol=protocol(),
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        prepared_at=BASE + timedelta(minutes=1),
    )
    second = build_structure_aware_experiment_plan(
        plan_id="target-blind-plan-b",
        protocol=protocol(),
        dataframe=shifted,
        dataset_file_bytes=DATASET_BYTES,
        prepared_at=BASE + timedelta(minutes=1),
    )
    assert first.split_receipt == second.split_receipt
    assert first.dataset_receipt.target_vector_sha256 != second.dataset_receipt.target_vector_sha256
    roles_by_group: dict[str, set[StructureEvaluationRole]] = {}
    for assignment in first.split_receipt.assignments:
        roles_by_group.setdefault(assignment.chemical_system_identity_sha256, set()).add(
            assignment.role
        )
    assert all(len(roles) == 1 for roles in roles_by_group.values())


def test_matched_experiment_runs_all_arms_once_and_exactly_replays():
    dataframe, plan, result = plan_and_result()
    assert result.plan_sha256 == plan.plan_sha256
    assert result.matched_capacity.exact_feature_dimension_match is True
    assert (
        result.matched_capacity.aligned_feature_count
        == result.matched_capacity.permuted_control_feature_count
    )
    assert {(item.arm, item.role) for item in result.arm_evaluations} == {
        (arm, role)
        for arm in StructureExperimentArm
        for role in (
            StructureEvaluationRole.INTERNAL_VALIDATION,
            StructureEvaluationRole.LOCKED_HOLDOUT,
        )
    }
    assert all(item.stays_within_role for item in result.permutation_receipts)
    assert result.holdout_is_same_dataset_not_external_replication is True
    verify_structure_aware_experiment(
        plan=plan,
        result=result,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
    )


def test_permuted_control_has_same_capacity_but_real_alignment_improves_gold_target():
    _, _, result = plan_and_result()
    for signal in result.signal_evaluations:
        assert signal.control_minus_aligned_mae > 0
        assert signal.relative_mae_improvement > 0
    composition = {
        item.role: item
        for item in result.arm_evaluations
        if item.arm is StructureExperimentArm.COMPOSITION_ONLY
    }
    aligned = {
        item.role: item
        for item in result.arm_evaluations
        if item.arm is StructureExperimentArm.ALIGNED_STRUCTURE
    }
    assert all(aligned[role].feature_count > composition[role].feature_count for role in aligned)


def test_result_disposition_and_arm_metrics_cannot_be_relabelled():
    _, _, result = plan_and_result()
    payload = result.model_dump(mode="json")
    payload["disposition"] = "no_aligned_structure_advantage"
    with pytest.raises(ValidationError, match="disposition"):
        StructureAwareExperimentResult.model_validate(payload)
    payload = result.model_dump(mode="json")
    payload["signal_evaluations"][0]["aligned_mae"] += 1
    payload["signal_evaluations"][0]["control_minus_aligned_mae"] -= 1
    payload["signal_evaluations"][0]["relative_mae_improvement"] = (
        payload["signal_evaluations"][0]["control_minus_aligned_mae"]
        / payload["signal_evaluations"][0]["permuted_control_mae"]
    )
    with pytest.raises(ValidationError, match="arm metrics"):
        StructureAwareExperimentResult.model_validate(payload)


def test_protocol_rejects_changed_implementation_before_plan_build():
    dataframe = synthetic_dataset()
    with pytest.raises(ValueError, match="another implementation"):
        build_structure_aware_experiment_plan(
            plan_id="wrong-implementation-plan",
            protocol=protocol(implementation_sha256="0" * 64),
            dataframe=dataframe,
            dataset_file_bytes=DATASET_BYTES,
            prepared_at=BASE + timedelta(minutes=1),
        )


def test_implementation_commitment_covers_all_feature_sources():
    assert set(structure_discrimination_implementation_components()) == {
        "featurizers.py",
        "identity.py",
        "structure_discrimination.py",
        "structures.py",
    }


def test_frozen_real_protocol_binds_current_code_environment_and_license_evidence():
    repository = Path(__file__).resolve().parents[3]
    protocol_path = repository / "configs/materials/f10_matbench_phonons_structure_aware_v1.yaml"
    frozen = StructureAwareExperimentProtocol.model_validate(
        yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    )
    evidence_path = repository / "configs/materials/evidence/matbench_phonons_license_v1.json"
    assert frozen.implementation_sha256 == structure_discrimination_implementation_sha256()
    assert (
        frozen.dataset.license_evidence_sha256
        == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    )
    assert frozen.dataset.expected_file_sha256 == (
        "4db551f21ec5f577e6202725f10e34dfc509aa7df3a6bdaac497da7f6dbbb9b3"
    )
