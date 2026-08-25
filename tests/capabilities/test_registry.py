"""F10-S1 capability manifest, registry, persistence, and exact-planner tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import aletheia.capabilities as c
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from aletheia.reproducibility.manifest import content_sha256


BASE = datetime(2026, 8, 15, tzinfo=timezone.utc)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _schema(name: str):
    return c.schema_descriptor(
        schema_id=f"test.{name}",
        version="1.0.0",
        json_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )


def _roles():
    return tuple(
        c.CapabilityRoleBinding(
            role=role,
            adapter_ref=f"tests.capabilities.adapters:{role.value}",
            implementation_sha256=sha(f"implementation:{role.value}"),
            principal_sha256=sha(f"principal:{role.value}"),
            runtime=(
                c.CapabilityRuntime.DETERMINISTIC
                if role in {c.CapabilityRole.PLANNER, c.CapabilityRole.VALIDATOR}
                else c.CapabilityRuntime.SANDBOXED_CODE
            ),
            boundary=(
                c.CapabilityBoundary.NO_EXECUTION
                if role is c.CapabilityRole.PLANNER
                else c.CapabilityBoundary.HARD_SANDBOX
            ),
            allowed_tools=() if role is not c.CapabilityRole.EXECUTOR else ("cpu",),
            agent_authored=(role is c.CapabilityRole.EXECUTOR),
            frozen_at=BASE,
        )
        for role in (
            c.CapabilityRole.PLANNER,
            c.CapabilityRole.EXECUTOR,
            c.CapabilityRole.OBSERVATION_PARSER,
            c.CapabilityRole.VALIDATOR,
        )
    )


def _registration():
    return c.CapabilityRegistrationEvidence(
        reference_fixtures_sha256=sha("reference"),
        adversarial_fixtures_sha256=sha("adversarial"),
        positive_control_receipt_sha256=sha("positive"),
        negative_control_receipt_sha256=sha("negative"),
        independent_recomputation_receipt_sha256=sha("recompute"),
        reproduction_policy_evidence_sha256=sha("reproduction"),
        safety_review_sha256=sha("safety"),
        domain_review_receipt_sha256=sha("domain-review"),
        domain_reviewer_principal_sha256=sha("domain-reviewer"),
        promotion_auditor_principal_sha256=sha("promotion-auditor"),
        reviewed_at=BASE + timedelta(hours=1),
    )


def manifest(
    *,
    capability_id: str = "materials.range_compression",
    version: str = "1.0.0",
    lifecycle: c.CapabilityLifecycle = c.CapabilityLifecycle.REGISTERED,
    supersedes: str | None = None,
    input_schema=None,
    safety: c.SafetyClass = c.SafetyClass.LOW_RISK_COMPUTE,
):
    registered = lifecycle is not c.CapabilityLifecycle.PROVISIONAL
    return c.ExperimentCapabilityManifest(
        capability_id=capability_id,
        version=version,
        domain="materials",
        lifecycle=lifecycle,
        scientific_question_ids=("materials.band_gap.range_compression",),
        claim_types_supported=(
            c.CapabilityClaimType.DESCRIPTIVE,
            c.CapabilityClaimType.PREDICTIVE,
        ),
        maximum_evidence_level=(
            c.CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL
            if registered
            else c.CapabilityEvidenceLevel.EXPLORATORY
        ),
        input_schema=input_schema or _schema("input"),
        output_schema=_schema("output"),
        accepted_data_modalities=("composition_property_table",),
        required_metadata=("composition", "property_unit", "target"),
        units_and_ontologies=("eV", "pymatgen.chemical_system"),
        action_type=c.CapabilityActionType.COMPUTATIONAL_EXPERIMENT,
        roles=_roles(),
        preregistration_schema=_schema("preregistration"),
        controls_required=(
            c.CapabilityControlRequirement(
                control_id="control.negative",
                kind=c.ControlKind.NEGATIVE,
                description="represented-system negative control",
            ),
            c.CapabilityControlRequirement(
                control_id="control.positive",
                kind=c.ControlKind.POSITIVE,
                description="synthetic known-compression positive control",
            ),
        ),
        assumptions=(
            c.CapabilityAssumption(
                assumption_id="assumption.measurement",
                statement="Band-gap labels share eV semantics.",
                violation_consequence="Spread comparison is uninterpretable.",
                test_or_monitor="Require and validate the unit metadata.",
            ),
        ),
        known_failure_modes=(
            c.CapabilityFailureMode(
                failure_id="failure.partition_instability",
                description="Outcome changes across frozen partitions.",
                detection="Registered multi-partition replication.",
                disposition="exploratory_only",
            ),
        ),
        minimum_sample_rule=c.CapabilityMinimumSampleRule(
            sampling_unit="chemical_system",
            minimum_count=100,
            power_or_precision_rule="At least 100 systems in each comparison arm.",
            rule_sha256=sha("minimum-sample-rule"),
        ),
        resources=c.CapabilityResourceEstimate(
            estimated_cost_usd=1.0,
            estimated_wall_time_seconds=120,
            cpu_seconds=120,
            memory_mb=2048,
            gpu_seconds=0,
        ),
        nondeterminism_policy=c.CapabilityNondeterminismPolicy(
            mode="frozen_seeds",
            frozen_seeds=(20260816, 20260817),
            aggregation_rule="Retain every seed and aggregate all preregistered slots.",
            stopping_rule="Run every frozen slot; no early stop.",
        ),
        reproduction_policy=c.CapabilityReproductionPolicy(
            minimum_exact_reexecutions=2,
            independent_implementation_required=False,
            independent_dataset_required=False,
            metric_tolerance=1e-12,
        ),
        safety_class=safety,
        approval_class=c.ApprovalClass.OPERATOR,
        license_egress_policy=c.CapabilityLicenseEgressPolicy(
            allowed_data_classes=("public_benchmark",),
            network_egress="none",
            raw_data_retention="required",
            license_evidence_sha256=sha("license"),
        ),
        supersedes_manifest_sha256=supersedes,
        registration_evidence=_registration() if registered else None,
        frozen_at=BASE + timedelta(hours=2),
    )


def query(**updates):
    payload = {
        "query_id": "materials-range-compression-query",
        "domain": "materials",
        "scientific_question_id": "materials.band_gap.range_compression",
        "claim_type": c.CapabilityClaimType.PREDICTIVE,
        "minimum_evidence_level": c.CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL,
        "available_data_modalities": ("composition_property_table",),
        "available_metadata": ("composition", "property_unit", "target"),
        "maximum_safety_class": c.SafetyClass.LOW_RISK_COMPUTE,
        "allowed_approval_classes": (c.ApprovalClass.OPERATOR,),
    }
    payload.update(updates)
    return c.CapabilityPlanningQuery(**payload)


def test_manifest_enforces_roles_registration_controls_and_schema_hashes():
    registered = manifest()
    assert registered.manifest_sha256 == content_sha256(registered)

    raw = registered.model_dump(mode="python")
    roles = list(raw["roles"])
    roles[3]["principal_sha256"] = roles[1]["principal_sha256"]
    raw["roles"] = tuple(roles)
    with pytest.raises(ValidationError, match="distinct principals"):
        c.ExperimentCapabilityManifest.model_validate(raw)

    raw = registered.model_dump(mode="python")
    raw["registration_evidence"] = None
    with pytest.raises(ValidationError, match="promotion evidence"):
        c.ExperimentCapabilityManifest.model_validate(raw)

    descriptor = registered.input_schema.model_dump(mode="python")
    descriptor["json_schema_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="schema hash"):
        c.CapabilitySchemaDescriptor.model_validate(descriptor)


def test_provisional_capability_is_exploratory_only_and_cannot_claim_mechanism():
    provisional = manifest(lifecycle=c.CapabilityLifecycle.PROVISIONAL)
    assert provisional.maximum_evidence_level is c.CapabilityEvidenceLevel.EXPLORATORY
    raw = provisional.model_dump(mode="python")
    raw["claim_types_supported"] = (c.CapabilityClaimType.MECHANISM_CANDIDATE,)
    with pytest.raises(ValidationError, match="strong causal"):
        c.ExperimentCapabilityManifest.model_validate(raw)


def test_registry_is_versioned_append_only_and_rejects_silent_breaking_changes():
    first = manifest()
    successor = manifest(version="1.1.0", supersedes=first.manifest_sha256)
    snapshot = c.build_capability_registry_snapshot(
        registry_id="materials-capabilities-v1",
        manifests=(successor, first),
        created_at=BASE + timedelta(hours=3),
    )
    assert snapshot.manifests == (first, successor)
    assert c.CapabilityRegistry(snapshot).get(first.capability_id) == successor

    changed_schema = c.schema_descriptor(
        schema_id="test.input",
        version="1.0.0",
        json_schema={
            "type": "object",
            "properties": {"changed": {"type": "string"}},
            "required": ["changed"],
            "additionalProperties": False,
        },
    )
    incompatible = manifest(
        version="1.2.0",
        supersedes=successor.manifest_sha256,
        input_schema=changed_schema,
    )
    with pytest.raises(c.IncompatibleCapabilityVersion, match="major version"):
        c.build_capability_registry_snapshot(
            registry_id="materials-capabilities-invalid",
            manifests=(first, successor, incompatible),
            created_at=BASE + timedelta(hours=3),
        )


def test_registry_physically_reloads_content_addressed_snapshot(tmp_path):
    snapshot = c.build_capability_registry_snapshot(
        registry_id="materials-capabilities-archive",
        manifests=(manifest(),),
        created_at=BASE + timedelta(hours=3),
    )
    archive = ContentAddressedResponseArchive(tmp_path / "registry")
    committed = c.commit_capability_registry(
        archive=archive,
        snapshot=snapshot,
        committed_at=BASE + timedelta(hours=4),
    )
    assert c.load_capability_registry(archive=archive, committed=committed) == snapshot


def test_planner_uses_exact_question_metadata_and_no_fuzzy_fallback():
    selected = manifest()
    decoy = manifest(capability_id="materials.unrelated")
    snapshot = c.build_capability_registry_snapshot(
        registry_id="materials-planner-registry",
        manifests=(selected, decoy),
        created_at=BASE + timedelta(hours=3),
    )
    plan = c.plan_capability(snapshot=snapshot, query=query())
    assert plan.disposition is c.CapabilityPlanDisposition.SELECTED
    assert plan.selected_manifest == selected
    assert plan.query.observation_access == "none"

    unsupported = c.plan_capability(
        snapshot=snapshot,
        query=query(scientific_question_id="materials.band_gap.range_compress"),
    )
    assert unsupported.disposition is c.CapabilityPlanDisposition.UNSUPPORTED
    assert unsupported.selected_manifest is None

    missing_metadata = c.plan_capability(
        snapshot=snapshot,
        query=query(available_metadata=("composition", "target")),
    )
    assert missing_metadata.disposition is c.CapabilityPlanDisposition.UNSUPPORTED
    assert any(
        "required_metadata_missing:property_unit" in audit.blockers
        for audit in missing_metadata.candidate_audits
    )


def test_exact_unknown_or_provisional_capability_fails_closed():
    provisional = manifest(lifecycle=c.CapabilityLifecycle.PROVISIONAL)
    snapshot = c.build_capability_registry_snapshot(
        registry_id="provisional-registry",
        manifests=(provisional,),
        created_at=BASE + timedelta(hours=3),
    )
    registry = c.CapabilityRegistry(snapshot)
    with pytest.raises(c.UnsupportedCapability, match="provisional"):
        registry.get(provisional.capability_id)
    assert registry.get(provisional.capability_id, allow_provisional=True) == provisional
    with pytest.raises(c.UnsupportedCapability, match="unsupported capability ID"):
        registry.get("materials.range-compress")
