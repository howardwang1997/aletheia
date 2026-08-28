"""Synthetic contract and adversarial tests for the F10-S6 campaign template."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import aletheia.capabilities as c
import aletheia.epistemics as e
from aletheia.domains.materials.mechanistic_campaign import (
    ConfirmationIndependenceKind,
    FreshConfirmationReservation,
    MechanisticCampaignDisposition,
    MechanisticCampaignEvidenceBundle,
    MechanisticCampaignProtocol,
    MechanisticClaimCeiling,
    MechanisticCapabilityQualification,
    MechanisticDecisionPolicy,
    MechanisticEvidenceRole,
    MechanisticExperimentFamily,
    MechanisticExperimentSlot,
    MechanisticSlotEvidence,
    OutcomeMappingManifest,
    build_mechanistic_campaign_protocol,
    build_mechanistic_campaign_readiness_audit,
    evaluate_mechanistic_campaign,
)
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from epistemics.f9s2_fixtures import build_f9s2_fixture
from epistemics.f9s3_fixtures import build_f9s3_fixture
from epistemics.f9s4_fixtures import build_f9s4_fixture
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture


ROOT = Path(__file__).resolve().parents[3]
BASE_MANIFEST = (
    ROOT / "configs/capabilities/materials_band_gap_range_compression_provisional_v2.yaml"
)
CURRENT_AUDIT = ROOT / "configs/materials/f10_mechanistic_campaign_readiness_audit_v1.json"


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _role(manifest: c.ExperimentCapabilityManifest, role: c.CapabilityRole):
    return next(item for item in manifest.roles if item.role is role)


@pytest.fixture(scope="module")
def f9_chain(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f10s6-strong-direction"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypothesis_parts = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f10s6:synthetic-hypotheses",
            direction_gate=hypothesis_parts["gate"],
            policy=hypothesis_parts["policy"],
            request=hypothesis_parts["request"],
            generator=hypothesis_parts["generator"],
            deduplicator=hypothesis_parts["deduplicator"],
            clock=hypothesis_parts["clock"],
        )
    )
    causal_parts = build_f9s3_fixture(hypothesis_campaign)
    causal_campaign = asyncio.run(
        e.run_causal_identification_audit(
            campaign_id="campaign:f10s6:synthetic-causal",
            source_campaign=causal_parts["source_campaign"],
            policy=causal_parts["policy"],
            request=causal_parts["request"],
            author=causal_parts["author"],
            reviewer=causal_parts["reviewer"],
            clock=causal_parts["clock"],
        )
    )
    predictions = []
    for suffix in ("structure", "simulation"):
        parts = build_f9s4_fixture(
            causal_campaign,
            experiment_id=f"experiment.f10s6.{suffix}.v1",
        )
        predictions.append(
            asyncio.run(
                e.run_prediction_commitment(
                    campaign_id=f"campaign:f10s6:prediction:{suffix}",
                    source_causal_campaign=parts["source_campaign"],
                    policy=parts["policy"],
                    request=parts["request"],
                    author=parts["author"],
                    calibration_evaluator_manifest=parts["evaluator_manifest"],
                    clock=parts["clock"],
                )
            )
        )
    assert causal_campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert causal_campaign.claim_ceiling is e.CausalClaimCeiling.CAUSAL_CANDIDATE
    assert all(item.disposition is e.PredictionCommitmentDisposition.READY for item in predictions)
    return causal_campaign, tuple(predictions)


def _registered_manifest(
    *,
    family: MechanisticExperimentFamily,
    frozen_at: datetime,
    identity_label: str | None = None,
) -> c.ExperimentCapabilityManifest:
    raw = yaml.safe_load(BASE_MANIFEST.read_text(encoding="utf-8"))
    label = identity_label or family.value.replace("c2_", "").replace("c4_", "")
    raw.update(
        {
            "capability_id": f"materials.synthetic.{label}",
            "version": "1.0.0",
            "lifecycle": "registered",
            "maximum_evidence_level": (
                "independent_computational"
                if family is MechanisticExperimentFamily.SIMULATION
                else "confirmatory_internal"
            ),
            "action_type": (
                "simulation"
                if family is MechanisticExperimentFamily.SIMULATION
                else "computational_experiment"
            ),
            "claim_types_supported": (
                ["descriptive", "mechanism_candidate", "predictive"]
                if family is MechanisticExperimentFamily.SIMULATION
                else ["descriptive", "predictive"]
            ),
            "scientific_question_ids": ["rq_f10s6_synthetic_mechanism"],
            "supersedes_manifest_sha256": None,
            "frozen_at": frozen_at,
        }
    )
    role_frozen_at = frozen_at - timedelta(minutes=2)
    for role in raw["roles"]:
        role_name = role["role"]
        role.update(
            {
                "adapter_ref": f"tests.domains.materials.synthetic:{label}_{role_name}",
                "implementation_sha256": sha(f"f10s6:{label}:{role_name}:implementation"),
                "principal_sha256": sha(f"f10s6:{label}:{role_name}:principal"),
                "agent_authored": False,
                "frozen_at": role_frozen_at,
            }
        )
    raw["registration_evidence"] = {
        "reference_fixtures_sha256": sha(f"f10s6:{label}:reference"),
        "adversarial_fixtures_sha256": sha(f"f10s6:{label}:adversarial"),
        "positive_control_receipt_sha256": sha(f"f10s6:{label}:positive"),
        "negative_control_receipt_sha256": sha(f"f10s6:{label}:negative"),
        "independent_recomputation_receipt_sha256": sha(f"f10s6:{label}:recomputation"),
        "reproduction_policy_evidence_sha256": sha(f"f10s6:{label}:reproduction"),
        "safety_review_sha256": sha(f"f10s6:{label}:safety"),
        "domain_review_receipt_sha256": sha(f"f10s6:{label}:domain-review"),
        "domain_reviewer_principal_sha256": sha(f"f10s6:{label}:domain-reviewer"),
        "promotion_auditor_principal_sha256": sha(f"f10s6:{label}:promotion-auditor"),
        "reviewed_at": frozen_at - timedelta(minutes=1),
    }
    return c.ExperimentCapabilityManifest.model_validate(raw)


def _as_provisional(
    manifest: c.ExperimentCapabilityManifest,
) -> c.ExperimentCapabilityManifest:
    raw = manifest.model_dump(mode="python")
    raw.update(
        {
            "lifecycle": c.CapabilityLifecycle.PROVISIONAL,
            "maximum_evidence_level": c.CapabilityEvidenceLevel.EXPLORATORY,
            "claim_types_supported": (
                c.CapabilityClaimType.DESCRIPTIVE,
                c.CapabilityClaimType.PREDICTIVE,
            ),
            "registration_evidence": None,
        }
    )
    return c.ExperimentCapabilityManifest.model_validate(raw)


def _family_qualification(
    *,
    manifest: c.ExperimentCapabilityManifest,
    family: MechanisticExperimentFamily,
    frozen_at: datetime,
) -> MechanisticCapabilityQualification:
    label = manifest.capability_id.replace(".", "-")
    return MechanisticCapabilityQualification(
        qualification_id=f"qualification.{label}",
        capability_manifest_sha256=manifest.manifest_sha256,
        family=family,
        expected_action=manifest.action_type,
        qualification_evidence_sha256=sha(f"f10s6:{label}:family-qualification"),
        domain_reviewer_principal_sha256=sha(f"f10s6:{label}:family-qualification-reviewer"),
        frozen_at=frozen_at,
    )


@dataclass(frozen=True)
class CampaignFixture:
    protocol: MechanisticCampaignProtocol
    registry: c.CapabilityRegistrySnapshot
    family_qualifications: tuple[MechanisticCapabilityQualification, ...]
    structure_manifest: c.ExperimentCapabilityManifest
    simulation_manifest: c.ExperimentCapabilityManifest


def _campaign_fixture(
    f9_chain,
    *,
    registered: bool = True,
    minimum_probability_margin: float = 0.4,
    same_family: bool = False,
) -> CampaignFixture:
    causal_campaign, predictions = f9_chain
    anchor = max(item.generated_at for item in predictions) + timedelta(hours=1)
    structure_manifest = _registered_manifest(
        family=MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION,
        frozen_at=anchor,
    )
    simulation_family = (
        MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION
        if same_family
        else MechanisticExperimentFamily.SIMULATION
    )
    simulation_manifest = _registered_manifest(
        family=simulation_family,
        frozen_at=anchor,
        identity_label=("structure_replication" if same_family else None),
    )
    if not registered:
        structure_manifest = _as_provisional(structure_manifest)
        simulation_manifest = _as_provisional(simulation_manifest)

    registry = c.build_capability_registry_snapshot(
        registry_id="materials-f10s6-synthetic-campaign-registry-v1",
        manifests=(structure_manifest, simulation_manifest),
        created_at=anchor + timedelta(seconds=30),
    )
    family_qualifications = (
        _family_qualification(
            manifest=structure_manifest,
            family=MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION,
            frozen_at=anchor + timedelta(seconds=45),
        ),
        _family_qualification(
            manifest=simulation_manifest,
            family=simulation_family,
            frozen_at=anchor + timedelta(seconds=45),
        ),
    )

    policy = MechanisticDecisionPolicy(
        policy_id="f10s6-concordant-robust-winner-v1",
        minimum_probability_margin=minimum_probability_margin,
        frozen_at=anchor + timedelta(minutes=1),
    )
    planned_at = anchor + timedelta(minutes=3)
    structure_executor = _role(structure_manifest, c.CapabilityRole.EXECUTOR)
    simulation_executor = _role(simulation_manifest, c.CapabilityRole.EXECUTOR)
    structure_slot = MechanisticExperimentSlot(
        slot_id="slot.c2.structure",
        family=MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION,
        evidence_role=MechanisticEvidenceRole.INTERNAL_DISCRIMINATION,
        capability_manifest=structure_manifest,
        family_qualification=family_qualifications[0],
        prediction_campaign_sha256=predictions[0].campaign_sha256,
        input_identity_sha256=sha("f10s6:structure:input"),
        data_identity_sha256=sha("f10s6:structure:internal-dataset"),
        implementation_identity_sha256=structure_executor.implementation_sha256,
        maximum_cost_usd=10.0,
        maximum_wall_time_seconds=600,
        planned_at=planned_at,
    )
    reservation = FreshConfirmationReservation(
        reservation_id="reservation.f10s6.fresh-independent-simulation",
        independence_kind=ConfirmationIndependenceKind.INDEPENDENT_IMPLEMENTATION,
        data_identity_sha256=sha("f10s6:simulation:fresh-dataset"),
        implementation_identity_sha256=simulation_executor.implementation_sha256,
        custody_principal_sha256=sha("f10s6:fresh:custody-principal"),
        reserved_at=anchor + timedelta(minutes=2),
    )
    simulation_slot = MechanisticExperimentSlot(
        slot_id="slot.c4.simulation",
        family=simulation_family,
        evidence_role=MechanisticEvidenceRole.FRESH_CONFIRMATION,
        capability_manifest=simulation_manifest,
        family_qualification=family_qualifications[1],
        prediction_campaign_sha256=predictions[1].campaign_sha256,
        input_identity_sha256=sha("f10s6:simulation:input"),
        data_identity_sha256=reservation.data_identity_sha256,
        implementation_identity_sha256=reservation.implementation_identity_sha256,
        maximum_cost_usd=25.0,
        maximum_wall_time_seconds=1200,
        confirmation_reservation=reservation,
        planned_at=planned_at,
    )
    protocol = build_mechanistic_campaign_protocol(
        protocol_id="f10s6-synthetic-mechanistic-campaign-v1",
        capability_registry=registry,
        causal_campaign=causal_campaign,
        prediction_campaigns=predictions,
        slots=(structure_slot, simulation_slot),
        policy=policy,
        evaluator_principal_sha256=sha("f10s6:campaign:evaluator"),
        frozen_at=anchor + timedelta(minutes=4),
    )
    return CampaignFixture(
        protocol=protocol,
        registry=registry,
        family_qualifications=family_qualifications,
        structure_manifest=structure_manifest,
        simulation_manifest=simulation_manifest,
    )


class _Parser:
    def __init__(self, manifest: c.ExperimentCapabilityManifest) -> None:
        binding = _role(manifest, c.CapabilityRole.OBSERVATION_PARSER)
        self.adapter_ref = binding.adapter_ref
        self.implementation_sha256 = binding.implementation_sha256
        self.principal_sha256 = binding.principal_sha256

    def parse(self, *, raw_run, artifacts):
        assert set(artifacts) == {"result"}
        return c.ParsedObservationPayload(
            scientific_outcome=c.ScientificOutcomeClass.POSITIVE,
            measurements=(
                c.MeasuredQuantity(
                    measurement_id="mechanistic-outcome-score",
                    quantity_kind_id="mechanistic_outcome_score",
                    value=0.8,
                    unit_ucum="1",
                    uncertainty=c.MeasurementUncertainty(
                        kind=c.UncertaintyKind.CONFIDENCE_INTERVAL,
                        lower=0.7,
                        upper=0.9,
                        coverage_probability=0.95,
                        method_sha256=sha("f10s6:bootstrap"),
                    ),
                    sample_count=40,
                    raw_artifact_ids=("result",),
                ),
            ),
            context=c.ObservationContext(
                measurement_method_id="f10s6-frozen-outcome-method-v1",
                conditions=(
                    c.ObservationCondition(
                        condition_id="experiment_context",
                        categorical_value="preregistered",
                    ),
                ),
                sample_id=f"sample:{raw_run.run_id}",
            ),
        )


class _Validator:
    def __init__(self, manifest: c.ExperimentCapabilityManifest) -> None:
        binding = _role(manifest, c.CapabilityRole.VALIDATOR)
        self.adapter_ref = binding.adapter_ref
        self.implementation_sha256 = binding.implementation_sha256
        self.principal_sha256 = binding.principal_sha256

    def validate(self, *, candidate, raw_run, artifacts):
        assert candidate.raw_run_sha256 == raw_run.run_sha256
        assert set(artifacts) == {"result"}
        return c.DomainValidationPayload(
            checks=(
                c.DomainValidationCheck(
                    check_id="control.negative",
                    passed=True,
                    evidence_sha256s=(sha("f10s6:negative-control"),),
                ),
                c.DomainValidationCheck(
                    check_id="schema.exact",
                    passed=True,
                    evidence_sha256s=(sha("f10s6:schema-check"),),
                ),
            ),
            protocol_adherence_verified=True,
            measurement_identity_verified=True,
        )


def _slot_evidence(
    tmp_path: Path,
    *,
    protocol: MechanisticCampaignProtocol,
    slot: MechanisticExperimentSlot,
    observed_outcome_bin_id: str,
    tag: str,
    correct_preregistration: bool = True,
    correct_mapping_schema: bool = True,
) -> MechanisticSlotEvidence:
    source_opened_at = protocol.frozen_at + timedelta(minutes=1)
    raw_archive = c.CapabilityObservationArchive(tmp_path / tag / "raw")
    artifact = raw_archive.store(
        artifact_id="result",
        payload=json.dumps(
            {"observed_outcome_bin_id": observed_outcome_bin_id}, sort_keys=True
        ).encode(),
        media_type="application/json",
        captured_at=source_opened_at + timedelta(minutes=1),
    )
    run = c.build_raw_experiment_run(
        run_id=f"{slot.slot_id}.{tag}",
        manifest=slot.capability_manifest,
        preregistration_sha256=(
            protocol.protocol_sha256
            if correct_preregistration
            else sha(f"{tag}:wrong-preregistration")
        ),
        input_sha256=slot.input_identity_sha256,
        status=c.ExperimentRunStatus.SUCCEEDED,
        artifacts=(artifact,),
        started_at=source_opened_at,
        ended_at=source_opened_at + timedelta(minutes=1),
        exit_code=0,
    )
    parsed = c.parse_capability_observation(
        manifest=slot.capability_manifest,
        raw_run=run,
        archive=raw_archive,
        adapter=_Parser(slot.capability_manifest),
        parsed_at=source_opened_at + timedelta(minutes=2),
    )
    validation_policy = c.CapabilityObservationValidationPolicy(
        policy_id=f"f10s6-observation-policy-{tag}",
        capability_manifest_sha256=slot.capability_manifest.manifest_sha256,
        unit_contracts=(
            c.QuantityUnitContract(
                quantity_kind_id="mechanistic_outcome_score",
                canonical_ucum_code="1",
                allowed_ucum_codes=("1",),
                conversion_policy_sha256=sha("f10s6:dimensionless-unit-policy"),
            ),
        ),
        required_condition_ids=("experiment_context",),
        minimum_sample_count=30,
        frozen_at=protocol.frozen_at + timedelta(seconds=30),
    )
    result = c.validate_capability_observation(
        manifest=slot.capability_manifest,
        policy=validation_policy,
        parse_result=parsed,
        archive=raw_archive,
        adapter=_Validator(slot.capability_manifest),
        validated_at=source_opened_at + timedelta(minutes=3),
    )
    committed = c.commit_capability_observation_pipeline(
        archive=ContentAddressedResponseArchive(tmp_path / tag / "ledger"),
        result=result,
        committed_at=source_opened_at + timedelta(minutes=4),
    )
    prediction = next(
        item
        for item in protocol.prediction_campaigns
        if item.campaign_sha256 == slot.prediction_campaign_sha256
    )
    mapping = OutcomeMappingManifest(
        mapper_id=f"f10s6-outcome-mapper-{tag}",
        prediction_campaign_sha256=prediction.campaign_sha256,
        outcome_schema_sha256=(
            prediction.request.outcome_schema.outcome_schema_sha256
            if correct_mapping_schema
            else sha(f"{tag}:wrong-outcome-schema")
        ),
        adapter_ref="tests.domains.materials.synthetic:map_outcome",
        implementation_sha256=sha(f"f10s6:{tag}:mapping-implementation"),
        principal_sha256=sha(f"f10s6:{tag}:mapping-principal"),
        frozen_at=protocol.frozen_at + timedelta(seconds=45),
    )
    return MechanisticSlotEvidence(
        slot_id=slot.slot_id,
        pipeline=committed,
        observed_outcome_bin_id=observed_outcome_bin_id,
        mapping_manifest=mapping,
        mapping_evidence_sha256=sha(f"f10s6:{tag}:mapping-evidence"),
        source_opened_at=source_opened_at,
        mapped_at=source_opened_at + timedelta(minutes=5),
        independence_attestation_sha256=(
            sha(f"f10s6:{tag}:independence-attestation")
            if slot.evidence_role is MechanisticEvidenceRole.FRESH_CONFIRMATION
            else None
        ),
    )


def _evidences(
    tmp_path: Path,
    fixture: CampaignFixture,
    *,
    outcomes: tuple[str, str] = ("primary_pattern", "primary_pattern"),
    first_preregistration_valid: bool = True,
    first_mapping_schema_valid: bool = True,
) -> tuple[MechanisticSlotEvidence, ...]:
    return tuple(
        _slot_evidence(
            tmp_path,
            protocol=fixture.protocol,
            slot=slot,
            observed_outcome_bin_id=outcome,
            tag=f"evidence-{index}",
            correct_preregistration=first_preregistration_valid or index != 0,
            correct_mapping_schema=first_mapping_schema_valid or index != 0,
        )
        for index, (slot, outcome) in enumerate(zip(fixture.protocol.slots, outcomes, strict=True))
    )


def test_registered_two_family_protocol_is_release_eligible(f9_chain):
    fixture = _campaign_fixture(f9_chain)
    protocol = fixture.protocol
    assert protocol.execution_authorized is True
    assert protocol.mechanism_release_eligible is True
    assert not protocol.execution_blockers
    assert not protocol.mechanism_release_blockers
    assert {slot.family for slot in protocol.slots} == {
        MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION,
        MechanisticExperimentFamily.SIMULATION,
    }
    assert protocol.direction_gate_sha256 == (
        protocol.causal_campaign.source_campaign.direction_gate.gate_sha256
    )


def test_protocol_rejects_manifest_absent_from_frozen_registry(f9_chain):
    fixture = _campaign_fixture(f9_chain)
    incomplete_registry = c.build_capability_registry_snapshot(
        registry_id="materials-f10s6-incomplete-registry-v1",
        manifests=(fixture.structure_manifest,),
        created_at=fixture.registry.created_at,
    )
    protocol = fixture.protocol
    with pytest.raises(ValidationError, match="absent from the frozen registry"):
        build_mechanistic_campaign_protocol(
            protocol_id=protocol.protocol_id,
            capability_registry=incomplete_registry,
            causal_campaign=protocol.causal_campaign,
            prediction_campaigns=protocol.prediction_campaigns,
            slots=protocol.slots,
            policy=protocol.policy,
            evaluator_principal_sha256=protocol.evaluator_principal_sha256,
            frozen_at=protocol.frozen_at,
        )


def test_provisional_capabilities_are_executable_but_release_blocked(f9_chain):
    protocol = _campaign_fixture(f9_chain, registered=False).protocol
    assert protocol.execution_authorized is True
    assert protocol.mechanism_release_eligible is False
    assert "mechanism_capable_claim_type_missing" in protocol.mechanism_release_blockers
    assert any(
        item.startswith("capability_not_registered:")
        for item in protocol.mechanism_release_blockers
    )


def test_same_family_campaign_is_not_execution_authorized(f9_chain):
    protocol = _campaign_fixture(f9_chain, same_family=True).protocol
    assert protocol.execution_authorized is False
    assert "fewer_than_two_experiment_families" in protocol.execution_blockers
    assert "no_intervention_or_simulation_family" in protocol.execution_blockers


def test_concordant_registered_evidence_supports_mechanism_candidate(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(tmp_path, fixture),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is (
        MechanisticCampaignDisposition.MECHANISM_CANDIDATE_SUPPORTED
    )
    assert bundle.decision.claim_ceiling is MechanisticClaimCeiling.MECHANISM_CANDIDATE
    assert bundle.decision.supported_hypothesis_role is e.HypothesisRole.PRIMARY
    assert bundle.decision.joint_posterior_computed is False
    assert all(item.evidence_valid for item in bundle.slot_assessments)
    assert all(item.passes_discrimination_rule for item in bundle.slot_assessments)


def test_concordant_provisional_evidence_is_bounded_pattern_only(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain, registered=False)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(tmp_path, fixture),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is (MechanisticCampaignDisposition.BOUNDED_PATTERN_SUPPORTED)
    assert bundle.decision.claim_ceiling is MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
    assert any(
        item.startswith("observation_not_confirmatory:")
        for item in bundle.decision.release_blockers
    )


def test_valid_but_low_margin_evidence_is_inconclusive_not_invalid(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain, minimum_probability_margin=0.5)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(tmp_path, fixture),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is MechanisticCampaignDisposition.INCONCLUSIVE
    assert all(item.evidence_valid for item in bundle.slot_assessments)
    assert all(not item.passes_discrimination_rule for item in bundle.slot_assessments)
    assert all(not item.blockers for item in bundle.slot_assessments)
    assert all(
        item.discrimination_failures == ("winner_probability_margin_below_minimum",)
        for item in bundle.slot_assessments
    )


def test_different_robust_winners_are_conflicting_evidence(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(
            tmp_path,
            fixture,
            outcomes=("primary_pattern", "alternative_pattern"),
        ),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is MechanisticCampaignDisposition.CONFLICTING_EVIDENCE
    assert bundle.decision.supported_hypothesis_id is None
    assert all(item.passes_discrimination_rule for item in bundle.slot_assessments)


def test_preregistration_mismatch_is_invalid_evidence(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(
            tmp_path,
            fixture,
            first_preregistration_valid=False,
        ),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is MechanisticCampaignDisposition.INVALID_EVIDENCE
    assert bundle.slot_assessments[0].blockers == ("preregistration_mismatch",)


def test_outcome_mapper_schema_rebinding_is_invalid_evidence(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(
            tmp_path,
            fixture,
            first_mapping_schema_valid=False,
        ),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    assert bundle.decision.disposition is MechanisticCampaignDisposition.INVALID_EVIDENCE
    assert bundle.slot_assessments[0].blockers == ("outcome_mapper_schema_mismatch",)


def test_bundle_rejects_derived_decision_tampering(tmp_path, f9_chain):
    fixture = _campaign_fixture(f9_chain)
    bundle = evaluate_mechanistic_campaign(
        protocol=fixture.protocol,
        slot_evidences=_evidences(tmp_path, fixture),
        evaluated_at=fixture.protocol.frozen_at + timedelta(minutes=10),
    )
    raw = bundle.model_dump(mode="python")
    raw["decision"]["disposition"] = MechanisticCampaignDisposition.INCONCLUSIVE
    with pytest.raises(ValidationError, match="decision is not derived"):
        MechanisticCampaignEvidenceBundle.model_validate(raw)


def test_current_registry_readiness_is_explicitly_blocked(
    materials_registry_v4: c.CapabilityRegistrySnapshot,
):
    registry = materials_registry_v4
    audit = build_mechanistic_campaign_readiness_audit(
        audit_id="materials-f10s6-current-readiness-v1",
        registry=registry,
        audited_at=datetime(2026, 8, 15, 16, 30, tzinfo=timezone.utc),
    )
    assert audit.engineering_template_available is True
    assert audit.execution_ready is False
    assert audit.scientific_release_ready is False
    assert {item.family for item in audit.available_capabilities} == {None}
    assert not audit.family_qualifications
    assert "production_f8_direction_missing" in audit.blockers
    assert "fewer_than_two_registered_confirmatory_families" in audit.blockers
    assert "fresh_confirmation_reservation_missing" in audit.blockers
    frozen = build_mechanistic_campaign_readiness_audit(
        audit_id=audit.audit_id,
        registry=registry,
        audited_at=audit.audited_at,
    )
    assert frozen == audit
    assert type(audit).model_validate_json(CURRENT_AUDIT.read_text(encoding="utf-8")) == audit
    assert audit.audit_sha256 == (
        "d7fe32533ad2ea9853c35a56555d816f27b489e532a47cf6a29a10c7a89d003b"
    )


def test_registered_readiness_inputs_clear_every_gate(f9_chain):
    fixture = _campaign_fixture(f9_chain)
    fresh_slot = next(
        item
        for item in fixture.protocol.slots
        if item.evidence_role is MechanisticEvidenceRole.FRESH_CONFIRMATION
    )
    assert fresh_slot.confirmation_reservation is not None
    audit = build_mechanistic_campaign_readiness_audit(
        audit_id="materials-f10s6-synthetic-ready-audit-v1",
        registry=fixture.registry,
        family_qualifications=fixture.family_qualifications,
        production_direction_gate_sha256=fixture.protocol.direction_gate_sha256,
        ready_hypothesis_campaign_sha256=fixture.protocol.hypothesis_campaign_sha256,
        ready_causal_campaign_sha256=fixture.protocol.causal_campaign_sha256,
        fresh_confirmation_reservation_sha256=(
            fresh_slot.confirmation_reservation.reservation_sha256
        ),
        independent_confirmation_kind=ConfirmationIndependenceKind.INDEPENDENT_IMPLEMENTATION,
        audited_at=fixture.protocol.frozen_at + timedelta(minutes=1),
    )
    assert audit.execution_ready is True
    assert audit.scientific_release_ready is True
    assert not audit.blockers
