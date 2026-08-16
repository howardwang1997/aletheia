"""F10 provisional capability and frozen multi-partition replication tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from aletheia.capabilities import (
    CapabilityEvidenceLevel,
    CapabilityPlanDisposition,
    CapabilityPlanningQuery,
    ExperimentCapabilityManifest,
    build_capability_registry_snapshot,
    plan_capability,
)
from aletheia.domains.materials import k3_evidence as k3
from aletheia.domains.materials.capabilities.range_compression import (
    parse_range_compression_observation,
)
from aletheia.domains.materials.capabilities.replication import (
    MaterialsReplicationPattern,
    assemble_materials_replication_bundle,
    assemble_materials_replication_slot_evidence,
    build_materials_replication_plan,
    derive_materials_replication_aggregation,
    verify_materials_replication_bundle,
)


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_V1_PATH = (
    ROOT / "configs/capabilities/materials_band_gap_range_compression_provisional_v1.yaml"
)
MANIFEST_PATH = (
    ROOT / "configs/capabilities/materials_band_gap_range_compression_provisional_v2.yaml"
)
QUERY_PATH = (
    ROOT / "configs/capabilities/queries/materials_band_gap_range_compression_exploratory_v2.yaml"
)
PROTOCOL_PATH = ROOT / "configs/materials/k3_band_gap_range_compression_v2.yaml"
BASE = datetime(2026, 8, 15, 6, tzinfo=timezone.utc)
MEASUREMENT_KEY = b"m" * 48
VALIDATION_KEY = b"v" * 48


def _manifest() -> ExperimentCapabilityManifest:
    return ExperimentCapabilityManifest.model_validate(
        yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    )


def _manifest_v1() -> ExperimentCapabilityManifest:
    return ExperimentCapabilityManifest.model_validate(
        yaml.safe_load(MANIFEST_V1_PATH.read_text(encoding="utf-8"))
    )


def _plan():
    manifest = _manifest()
    registry = build_capability_registry_snapshot(
        registry_id="materials-replication-test-registry",
        manifests=(_manifest_v1(), manifest),
        created_at=BASE,
    )
    protocol = k3.MaterialsK3Protocol.model_validate(
        yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    )
    return build_materials_replication_plan(
        plan_id="materials-range-compression-replication-test",
        manifest=manifest,
        registry=registry,
        base_protocol=protocol,
        protocol_frozen_at=BASE + timedelta(minutes=1),
        preregistered_at=BASE + timedelta(minutes=2),
        frozen_at=BASE + timedelta(minutes=3),
    )


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _result(commitment, *, unseen_specific: bool):
    protocol = commitment.preregistration.protocol
    if unseen_specific:
        unseen_predicted_sd = 1.4
        control_predicted_sd = 1.8
        ci = (0.08, 0.31)
        probability = 0.99
        outcome = k3.MaterialsOutcomeId.UNSEEN_SPECIFIC
    else:
        unseen_predicted_sd = 1.6
        control_predicted_sd = 1.64
        ci = (-0.04, 0.08)
        probability = 0.72
        outcome = k3.MaterialsOutcomeId.GENERIC_SHRINKAGE
    unseen_compression = 1 - unseen_predicted_sd / 2
    control_compression = 1 - control_predicted_sd / 2
    metrics = k3.MaterialsCompressionMetrics(
        unseen_true_sd_ev=2.0,
        unseen_predicted_sd_ev=unseen_predicted_sd,
        unseen_compression=unseen_compression,
        control_true_sd_ev=2.0,
        control_predicted_sd_ev=control_predicted_sd,
        control_compression=control_compression,
        unseen_minus_control_delta=unseen_compression - control_compression,
        delta_ci_lower=ci[0],
        delta_ci_upper=ci[1],
        bootstrap_probability_delta_above_zero=probability,
        unseen_mae_ev=0.55,
        control_mae_ev=0.35,
        bootstrap_resamples=protocol.bootstrap.resamples,
        confidence_level=protocol.bootstrap.confidence_level,
    )
    seed = commitment.seed
    return k3.MaterialsExperimentResult(
        dataset=k3.MaterialsDatasetReceipt(
            dataset_ref=protocol.dataset_ref,
            composition_column=protocol.composition_column,
            target_column=protocol.target_column,
            row_count=100,
            feature_count=132,
            chemical_system_count=80,
            logical_rows_sha256=_sha(f"rows:{seed}"),
            feature_names_sha256=_sha("features"),
            feature_matrix_sha256=_sha(f"matrix:{seed}"),
            target_vector_sha256=_sha("targets"),
            chemical_system_vector_sha256=_sha("systems"),
            package_versions={"numpy": "test"},
        ),
        split=k3.MaterialsSplitReceipt(
            algorithm=protocol.split.algorithm,
            partition_seed=protocol.split.partition_seed,
            train_rows=70,
            unseen_test_rows=20,
            within_system_control_rows=10,
            train_chemical_systems=60,
            unseen_chemical_systems=20,
            control_chemical_systems=10,
            train_membership_sha256=_sha(f"train:{seed}"),
            unseen_membership_sha256=_sha(f"unseen:{seed}"),
            control_membership_sha256=_sha(f"control:{seed}"),
        ),
        metrics=metrics,
        outcome_id=outcome,
        unseen_predictions_sha256=_sha(f"unseen-predictions:{seed}"),
        control_predictions_sha256=_sha(f"control-predictions:{seed}"),
        fitted_model_identity_sha256=_sha(f"model:{seed}"),
    )


def _slot_evidence(plan, commitment, *, unseen_specific: bool):
    preregistration = commitment.preregistration
    policy = preregistration.protocol.evidence_policy
    started_at = plan.frozen_at + timedelta(minutes=commitment.ordinal)
    observation = k3.MaterialsObservation(
        observation_id=f"{commitment.slot_id}.observation",
        preregistration_sha256=preregistration.preregistration_sha256,
        protocol_sha256=preregistration.protocol_sha256,
        selected_candidate_id=preregistration.selected_candidate_id,
        implementation_sha256=preregistration.implementation_sha256,
        result=_result(commitment, unseen_specific=unseen_specific),
        measurement_principal_sha256=policy.measurement_principal_sha256,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
    )
    signed_observation = k3.SignedMaterialsObservation.issue(
        observation,
        key_id=policy.measurement_key_id,
        key=MEASUREMENT_KEY,
    )
    validations = []
    for index in (1, 2):
        receipt = k3.MaterialsValidationReceipt(
            validation_id=f"{commitment.slot_id}.validation-{index}",
            preregistration_sha256=preregistration.preregistration_sha256,
            observation_envelope_sha256=signed_observation.envelope_sha256,
            recomputed_result_sha256=observation.result.result_sha256,
            implementation_sha256=preregistration.implementation_sha256,
            validation_principal_sha256=policy.validation_principal_sha256,
            validated_at=observation.ended_at + timedelta(seconds=index),
        )
        validations.append(
            k3.SignedMaterialsValidation.issue(
                receipt,
                key_id=policy.validation_key_id,
                key=VALIDATION_KEY,
            )
        )
    update = k3.derive_materials_belief_update(
        preregistration=preregistration,
        signed_observation=signed_observation,
        signed_validation=validations[-1],
        observation_key=MEASUREMENT_KEY,
        validation_key=VALIDATION_KEY,
        updated_at=validations[-1].receipt.validated_at + timedelta(seconds=1),
    )
    return assemble_materials_replication_slot_evidence(
        commitment=commitment,
        signed_observation=signed_observation,
        exact_reexecutions=tuple(validations),
        update=update,
        required_exact_reexecutions=2,
        completed_at=update.updated_at + timedelta(seconds=1),
    )


def test_real_provisional_manifest_selects_only_for_exploratory_queries():
    manifest = _manifest()
    snapshot = build_capability_registry_snapshot(
        registry_id="materials-provisional-query-test",
        manifests=(_manifest_v1(), manifest),
        created_at=BASE,
    )
    raw = yaml.safe_load(QUERY_PATH.read_text(encoding="utf-8"))
    exploratory = CapabilityPlanningQuery.model_validate(raw)
    selected = plan_capability(snapshot=snapshot, query=exploratory)
    assert selected.disposition is CapabilityPlanDisposition.SELECTED
    assert selected.selected_manifest == manifest
    assert selected.reason_codes == ("exact_provisional_capability_selected",)

    raw.update(
        {
            "query_id": "materials-range-compression-confirmatory-negative-control",
            "minimum_evidence_level": CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL.value,
            "allow_provisional": False,
        }
    )
    rejected = plan_capability(snapshot=snapshot, query=CapabilityPlanningQuery.model_validate(raw))
    assert rejected.disposition is CapabilityPlanDisposition.UNSUPPORTED
    assert rejected.candidate_audits[0].blockers == (
        "capability_not_registered",
        "evidence_level_insufficient",
    )


def test_replication_plan_freezes_every_seed_and_rejects_slot_tampering():
    plan = _plan()
    assert tuple(slot.seed for slot in plan.slots) == (
        20260818,
        20260819,
        20260820,
        20260821,
        20260822,
    )
    assert all(
        slot.preregistration.preregistered_at == plan.preregistered_at for slot in plan.slots
    )
    assert all(
        slot.preregistration.protocol.bootstrap.seed == slot.seed * 100 + 1 for slot in plan.slots
    )
    raw = plan.model_dump(mode="python")
    slots = list(raw["slots"])
    slots[0]["seed"] += 1
    raw["slots"] = tuple(slots)
    with pytest.raises(ValidationError, match="capability seed commitment"):
        type(plan).model_validate(raw)


def test_v2_contract_schemas_validate_the_actual_preregistration_plan_and_result():
    plan = _plan()
    manifest = plan.capability_manifest
    commitment = plan.slots[0]
    result = _result(commitment, unseen_specific=True)
    jsonschema.validate(
        commitment.preregistration.model_dump(mode="json"),
        manifest.input_schema.json_schema,
    )
    jsonschema.validate(result.model_dump(mode="json"), manifest.output_schema.json_schema)
    jsonschema.validate(plan.model_dump(mode="json"), manifest.preregistration_schema.json_schema)

    old_manifest = _manifest_v1()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result.model_dump(mode="json"), old_manifest.output_schema.json_schema)


def test_four_of_five_consensus_keeps_all_results_without_joint_bayesian_update():
    plan = _plan()
    evidence = tuple(
        _slot_evidence(plan, commitment, unseen_specific=index < 4)
        for index, commitment in enumerate(plan.slots)
    )
    aggregated_at = max(item.completed_at for item in evidence) + timedelta(seconds=1)
    aggregation = derive_materials_replication_aggregation(
        plan=plan, evidence=evidence, aggregated_at=aggregated_at
    )
    bundle = assemble_materials_replication_bundle(
        plan=plan,
        evidence=evidence,
        aggregation=aggregation,
        assembled_at=aggregated_at + timedelta(seconds=1),
    )
    assert aggregation.pattern is MaterialsReplicationPattern.CONSENSUS_UNSEEN_SPECIFIC
    assert aggregation.outcome_counts[k3.MaterialsOutcomeId.UNSEEN_SPECIFIC] == 4
    assert aggregation.outcome_counts[k3.MaterialsOutcomeId.GENERIC_SHRINKAGE] == 1
    assert aggregation.all_slots_included is True
    assert aggregation.joint_bayesian_update_performed is False
    assert aggregation.supports_capability_promotion is False
    assert len(aggregation.evidence_sha256es) == 5
    assert type(bundle).model_validate_json(bundle.model_dump_json()) == bundle
    verify_materials_replication_bundle(
        bundle=bundle,
        observation_key=MEASUREMENT_KEY,
        validation_key=VALIDATION_KEY,
    )

    with pytest.raises(ValueError, match="every slot"):
        derive_materials_replication_aggregation(plan=plan, evidence=evidence[:-1])


def test_observation_parser_and_signature_checks_fail_closed():
    plan = _plan()
    evidence = _slot_evidence(plan, plan.slots[0], unseen_specific=True)
    parsed = parse_range_compression_observation(
        evidence.signed_observation.observation.result.model_dump(mode="python")
    )
    assert parsed == evidence.signed_observation.observation.result
    unknown = parsed.model_dump(mode="python")
    unknown["dropped_failure"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_range_compression_observation(unknown)
    forged = evidence.signed_observation.model_copy(update={"hmac_sha256": "0" * 64})
    with pytest.raises(ValueError, match="observation signature"):
        forged.verify(
            key=MEASUREMENT_KEY,
            expected_key_id=(
                plan.slots[0].preregistration.protocol.evidence_policy.measurement_key_id
            ),
        )
