from __future__ import annotations

import hashlib
import importlib.metadata
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from pydantic import ValidationError
from pymatgen.core import Lattice, Structure

from aletheia.domains.materials.capabilities.structure_discrimination import (
    MatchedEstimatorPolicy,
    StructureAwareExperimentProtocol,
    StructureBootstrapPolicy,
    StructureDatasetContract,
    StructureEvaluationRole,
    StructureGroupSplitPolicy,
    StructureSignalAcceptancePolicy,
    StructureSignalDisposition,
    build_structure_aware_experiment_plan,
    run_structure_aware_experiment,
    structure_discrimination_implementation_sha256,
)
from aletheia.domains.materials.phonon_reproduction import (
    IndependentExtraTreesPolicy,
    PhononIndependentReplayProtocol,
    PhononIndependentReplayResult,
    PhononReplayArtifact,
    PhononReproductionConflict,
    capture_phonon_replay_code_identity,
    run_phonon_independent_replay,
    verify_phonon_independent_replay,
)
from aletheia.domains.materials.structures import (
    StructureGeometryFeaturePolicy,
    StructureQualityPolicy,
)

BASE = datetime(2026, 8, 18, 8, tzinfo=timezone.utc)
DATASET_BYTES = b"implementation-diverse phonon replay fixture v1"
PACKAGES = ("matminer", "numpy", "pandas", "pymatgen", "scikit-learn", "spglib")


def _sha(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def _dataset() -> pd.DataFrame:
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
            targets.append(70.0 * lattice_constant + 2.5 * system_index)
    return pd.DataFrame({"structure": structures, "target": targets})


def _source_protocol() -> StructureAwareExperimentProtocol:
    return StructureAwareExperimentProtocol(
        protocol_id="phonon-replay-source-fixture-v1",
        dataset=StructureDatasetContract(
            dataset_ref="fixture://phonon-replay",
            artifact_id="phonon-replay-fixture-v1",
            media_type="application/octet-stream",
            expected_file_sha256=_sha(DATASET_BYTES),
            expected_row_count=48,
            structure_column="structure",
            target_column="target",
            target_quantity_kind_id="fixture.phonon_frequency",
            target_unit_ucum="cm-1",
            source_uri="https://example.test/phonon-replay",
            license_expression="CC0-1.0",
            license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
            license_evidence_sha256=_sha("fixture-license"),
        ),
        quality_policy=StructureQualityPolicy(
            policy_id="phonon-replay-quality-v1",
            maximum_sites=16,
        ),
        geometry_feature_policy=StructureGeometryFeaturePolicy(
            policy_id="phonon-replay-geometry-v1",
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
        implementation_sha256=structure_discrimination_implementation_sha256(),
        required_package_versions={name: importlib.metadata.version(name) for name in PACKAGES},
        frozen_at=BASE,
    )


def _fixture():
    dataframe = _dataset()
    plan = build_structure_aware_experiment_plan(
        plan_id="phonon-replay-source-plan-v1",
        protocol=_source_protocol(),
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        prepared_at=BASE + timedelta(minutes=1),
    )
    source_result = run_structure_aware_experiment(
        plan=plan,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        completed_at=BASE + timedelta(minutes=2),
    )
    protocol = PhononIndependentReplayProtocol(
        quest_id="qst_" + "1" * 32,
        gate_id="edg_" + "2" * 32,
        gate_manifest_sha256=_sha("gate"),
        controller_id="edctl_" + "3" * 32,
        controller_manifest_sha256=_sha("controller"),
        commissioning_id="pcm_" + "4" * 32,
        commissioning_manifest_sha256=_sha("commissioning"),
        original_campaign_id="cmp_" + "5" * 32,
        reproduction_campaign_id="cmp_" + "6" * 32,
        dataset=PhononReplayArtifact(
            relative_path="fixtures/phonons.json.gz",
            file_sha256=_sha(DATASET_BYTES),
        ),
        source_plan=PhononReplayArtifact(
            relative_path="fixtures/plan.json",
            file_sha256=_sha("plan-file"),
            content_sha256=plan.plan_sha256,
        ),
        source_result=PhononReplayArtifact(
            relative_path="fixtures/result.json",
            file_sha256=_sha("result-file"),
            content_sha256=source_result.result_sha256,
        ),
        source_split_membership_sha256=plan.split_receipt.membership_sha256,
        source_composition_matrix_sha256=plan.composition_features.matrix_sha256,
        source_structure_matrix_sha256=plan.structure_features.matrix_sha256,
        estimator_policy=IndependentExtraTreesPolicy(
            n_estimators=64,
            max_depth=10,
            min_samples_leaf=1,
            random_state=20260829,
        ),
        permutation_seed=20260830,
        bootstrap_seed=20260831,
        bootstrap_resamples=200,
        confidence_level=0.9,
        minimum_relative_mae_improvement=0.01,
        required_package_versions={name: importlib.metadata.version(name) for name in PACKAGES},
        code_identity=capture_phonon_replay_code_identity(require_committed=False),
        prepared_at=BASE + timedelta(minutes=3),
        execution_class="engineering",
    )
    return dataframe, plan, source_result, protocol


def test_independent_replay_reconstructs_inputs_uses_distinct_estimator_and_exactly_replays() -> (
    None
):
    dataframe, plan, source_result, protocol = _fixture()
    result = run_phonon_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        completed_at=BASE + timedelta(minutes=4),
    )
    assert protocol.estimator_policy.estimator == "sklearn.extra_trees_regressor"
    assert plan.protocol.estimator_policy.estimator == "sklearn.random_forest_regressor"
    assert result.composition_matrix_sha256 == plan.composition_features.matrix_sha256
    assert result.structure_matrix_sha256 == plan.structure_features.matrix_sha256
    assert result.matched_capacity.exact_estimator_budget_match is True
    assert result.disposition is StructureSignalDisposition.ROBUST_ALIGNED_STRUCTURE_SIGNAL
    assert {item.role for item in result.signal_evaluations} == {
        StructureEvaluationRole.INTERNAL_VALIDATION,
        StructureEvaluationRole.LOCKED_HOLDOUT,
    }
    verify_phonon_independent_replay(
        protocol=protocol,
        result=result,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
    )


def test_replay_rejects_target_drift_and_result_relabelling() -> None:
    dataframe, plan, source_result, protocol = _fixture()
    changed = dataframe.copy()
    changed.loc[0, "target"] += 1
    with pytest.raises(PhononReproductionConflict, match="target vector"):
        run_phonon_independent_replay(
            protocol=protocol,
            plan=plan,
            source_result=source_result,
            dataframe=changed,
            dataset_file_bytes=DATASET_BYTES,
            completed_at=BASE + timedelta(minutes=4),
        )

    result = run_phonon_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=DATASET_BYTES,
        completed_at=BASE + timedelta(minutes=4),
    )
    payload = result.model_dump(mode="json")
    payload["disposition"] = "no_aligned_structure_advantage"
    with pytest.raises(ValidationError, match="disposition"):
        PhononIndependentReplayResult.model_validate(payload)


def test_protocol_rejects_campaign_alias_and_unsafe_artifact_path() -> None:
    _, _, _, protocol = _fixture()
    with pytest.raises(ValidationError, match="distinct Campaign"):
        PhononIndependentReplayProtocol.model_validate(
            protocol.model_dump(
                mode="python",
                exclude={"protocol_id"},
            )
            | {"reproduction_campaign_id": protocol.original_campaign_id}
        )
    with pytest.raises(ValidationError, match="safe relative"):
        PhononReplayArtifact(
            relative_path="../escaped.json",
            file_sha256=_sha("escaped"),
        )
    with pytest.raises(ValidationError, match="committed code provenance"):
        PhononIndependentReplayProtocol.model_validate(
            protocol.model_dump(mode="python", exclude={"protocol_id"})
            | {"execution_class": "production"}
        )
