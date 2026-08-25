"""F9-S10 real-materials alternatives/experiment/update evidence-chain tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from aletheia.domains.materials import k3_evidence as k3


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_PATH = ROOT / "configs/materials/k3_band_gap_range_compression_v2.yaml"
MEASUREMENT_KEY = b"materials-measurement-test-key-0001"
VALIDATION_KEY = b"materials-validation-test-key-00001"


def _protocol() -> k3.MaterialsK3Protocol:
    return k3.MaterialsK3Protocol.model_validate(
        yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    )


def _preregistration() -> k3.MaterialsPreregistration:
    protocol = _protocol()
    return k3.build_materials_preregistration(
        preregistration_id="materials-k3-test-preregistration",
        protocol=protocol,
        preregistered_at=protocol.frozen_at + timedelta(minutes=1),
    )


def _result(preregistration: k3.MaterialsPreregistration) -> k3.MaterialsExperimentResult:
    metrics = k3.MaterialsCompressionMetrics(
        unseen_true_sd_ev=2.0,
        unseen_predicted_sd_ev=1.4,
        unseen_compression=0.3,
        control_true_sd_ev=2.0,
        control_predicted_sd_ev=1.8,
        control_compression=0.1,
        unseen_minus_control_delta=0.2,
        delta_ci_lower=0.08,
        delta_ci_upper=0.31,
        bootstrap_probability_delta_above_zero=0.99,
        unseen_mae_ev=0.55,
        control_mae_ev=0.35,
        bootstrap_resamples=preregistration.protocol.bootstrap.resamples,
        confidence_level=preregistration.protocol.bootstrap.confidence_level,
    )
    return k3.MaterialsExperimentResult(
        dataset=k3.MaterialsDatasetReceipt(
            dataset_ref=preregistration.protocol.dataset_ref,
            composition_column=preregistration.protocol.composition_column,
            target_column=preregistration.protocol.target_column,
            row_count=100,
            feature_count=132,
            chemical_system_count=80,
            logical_rows_sha256="1" * 64,
            feature_names_sha256="2" * 64,
            feature_matrix_sha256="3" * 64,
            target_vector_sha256="4" * 64,
            chemical_system_vector_sha256="5" * 64,
            package_versions={"numpy": "test"},
        ),
        split=k3.MaterialsSplitReceipt(
            algorithm=preregistration.protocol.split.algorithm,
            partition_seed=preregistration.protocol.split.partition_seed,
            train_rows=70,
            unseen_test_rows=20,
            within_system_control_rows=10,
            train_chemical_systems=60,
            unseen_chemical_systems=20,
            control_chemical_systems=10,
            train_membership_sha256="6" * 64,
            unseen_membership_sha256="7" * 64,
            control_membership_sha256="8" * 64,
        ),
        metrics=metrics,
        outcome_id=k3.MaterialsOutcomeId.UNSEEN_SPECIFIC,
        unseen_predictions_sha256="9" * 64,
        control_predictions_sha256="a" * 64,
        fitted_model_identity_sha256="b" * 64,
    )


def _signed_chain():
    preregistration = _preregistration()
    start = preregistration.preregistered_at + timedelta(minutes=1)
    observation = k3.MaterialsObservation(
        observation_id="materials-k3-test-observation",
        preregistration_sha256=preregistration.preregistration_sha256,
        protocol_sha256=preregistration.protocol_sha256,
        selected_candidate_id=preregistration.selected_candidate_id,
        implementation_sha256=preregistration.implementation_sha256,
        result=_result(preregistration),
        measurement_principal_sha256=(
            preregistration.protocol.evidence_policy.measurement_principal_sha256
        ),
        started_at=start,
        ended_at=start + timedelta(minutes=1),
    )
    signed_observation = k3.SignedMaterialsObservation.issue(
        observation,
        key_id=preregistration.protocol.evidence_policy.measurement_key_id,
        key=MEASUREMENT_KEY,
    )
    receipt = k3.MaterialsValidationReceipt(
        validation_id="materials-k3-test-validation",
        preregistration_sha256=preregistration.preregistration_sha256,
        observation_envelope_sha256=signed_observation.envelope_sha256,
        recomputed_result_sha256=observation.result.result_sha256,
        implementation_sha256=preregistration.implementation_sha256,
        validation_principal_sha256=(
            preregistration.protocol.evidence_policy.validation_principal_sha256
        ),
        validated_at=observation.ended_at + timedelta(minutes=1),
    )
    signed_validation = k3.SignedMaterialsValidation.issue(
        receipt,
        key_id=preregistration.protocol.evidence_policy.validation_key_id,
        key=VALIDATION_KEY,
    )
    update = k3.derive_materials_belief_update(
        preregistration=preregistration,
        signed_observation=signed_observation,
        signed_validation=signed_validation,
        observation_key=MEASUREMENT_KEY,
        validation_key=VALIDATION_KEY,
        updated_at=receipt.validated_at + timedelta(minutes=1),
    )
    decision = k3.derive_materials_scientific_decision(
        update=update, decided_at=update.updated_at + timedelta(minutes=1)
    )
    bundle = k3.assemble_materials_evidence_bundle(
        preregistration=preregistration,
        signed_observation=signed_observation,
        signed_validation=signed_validation,
        update=update,
        decision=decision,
        assembled_at=decision.decided_at + timedelta(minutes=1),
    )
    return preregistration, signed_observation, signed_validation, update, decision, bundle


def test_protocol_selects_discriminating_control_and_preregistration_never_loads_data(
    monkeypatch,
):
    protocol = _protocol()
    audits = k3.derive_materials_candidate_audits(protocol)
    assert audits[0].candidate_id == "candidate.unseen_vs_seen_control"
    assert audits[0].selected is True
    assert audits[0].expected_information_gain_nats > 100 * audits[1].expected_information_gain_nats

    monkeypatch.setattr(
        k3, "_dataset_inputs", lambda _protocol: (_ for _ in ()).throw(AssertionError)
    )
    preregistration = k3.build_materials_preregistration(
        preregistration_id="materials-k3-observation-blind",
        protocol=protocol,
        preregistered_at=protocol.frozen_at + timedelta(minutes=1),
    )
    assert preregistration.observation_access_during_selection == "none"
    assert preregistration.selected_candidate_id == audits[0].candidate_id


def test_separately_signed_validation_drives_robust_bayesian_contraction():
    _prereg, _observation, _validation, update, decision, bundle = _signed_chain()
    nominal = next(item for item in update.scenario_posteriors if item.scenario_id == "nominal")
    posterior = {item.hypothesis_id: item.posterior_probability for item in nominal.probabilities}
    assert posterior["h1_unseen_system_extrapolation"] == pytest.approx(0.8235294118)
    assert update.nominal_winner_hypothesis_ids == ("h1_unseen_system_extrapolation",)
    assert update.winner_stable_across_sensitivity is True
    assert update.minimum_effective_count_contraction >= 0.10
    assert update.hypothesis_space_contracted is True
    assert update.mechanism_claim_disposition == "withheld_observational_model_diagnostic"
    assert {item.action for item in update.revisions} == {
        k3.MaterialsRevisionAction.RETAIN,
        k3.MaterialsRevisionAction.NARROW,
    }
    h0_revision = next(
        item for item in update.revisions if item.hypothesis_id == "h0_no_material_compression"
    )
    assert h0_revision.action is k3.MaterialsRevisionAction.NARROW
    assert h0_revision.rationale_code == "nonwinner_retained_under_likelihood_sensitivity"
    assert decision.disposition is k3.MaterialsChainDisposition.QUALIFIED_COMPLETE
    assert decision.formal_prospective_evidence is False
    assert decision.formal_external_replication is False
    k3.verify_materials_evidence_bundle(
        bundle=bundle,
        observation_key=MEASUREMENT_KEY,
        validation_key=VALIDATION_KEY,
    )


def test_forged_measurement_and_validation_fail_closed():
    _prereg, observation, validation, _update, _decision, bundle = _signed_chain()
    forged_observation = observation.model_copy(update={"hmac_sha256": "0" * 64})
    with pytest.raises(ValueError, match="observation signature"):
        forged_observation.verify(
            key=MEASUREMENT_KEY,
            expected_key_id=bundle.preregistration.protocol.evidence_policy.measurement_key_id,
        )
    forged_validation = validation.model_copy(update={"hmac_sha256": "0" * 64})
    with pytest.raises(ValueError, match="validation signature"):
        forged_validation.verify(
            key=VALIDATION_KEY,
            expected_key_id=bundle.preregistration.protocol.evidence_policy.validation_key_id,
        )


def test_validator_physically_recomputes_exact_result(monkeypatch):
    preregistration, observation, _validation, _update, _decision, _bundle = _signed_chain()
    calls = []

    def recompute(_preregistration):
        calls.append(_preregistration.preregistration_sha256)
        return observation.observation.result

    monkeypatch.setattr(k3, "run_materials_experiment", recompute)
    receipt = k3.validate_materials_observation(
        preregistration=preregistration,
        signed_observation=observation,
        observation_key=MEASUREMENT_KEY,
        validation_key=VALIDATION_KEY,
        validated_at=observation.observation.ended_at + timedelta(minutes=2),
    )
    assert calls == [preregistration.preregistration_sha256]
    assert receipt.receipt.physical_recomputation_performed is True
    assert receipt.receipt.exact_result_match is True


def test_protocol_cli_inspection_is_observation_blind():
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/real_k3_materials_e2e.py"),
            "inspect",
            "--protocol",
            str(PROTOCOL_PATH),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["observation_access"] == "none"
    assert payload["candidate_audits"][0]["selected"] is True
    assert payload["formal_prospective_evidence"] is False
