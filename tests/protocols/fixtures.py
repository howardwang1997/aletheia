"""Domain-neutral accepted fixtures for the Scientific Protocol IR compiler.

The fixtures intentionally exercise different scientific shapes through the same compiler:

* a linear, deterministic grouped-estimation pipeline;
* a branched structural-intervention simulation with hypothesis discrimination; and
* a one-time external measurement followed by deterministic parsing.

They are value-only test data.  No adapter named by a manifest is imported or executed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aletheia.execution.schemas import (
    ArtifactRole,
    DataLocality,
    ExecutionResourceRequest,
    ExpectedArtifact,
    NetworkPolicy,
    ResourceKind,
    ScientificReplicateKind,
    StaticResourceCatalog,
    StaticResourceClass,
)
from aletheia.protocols.base import JsonSchemaRef, ProtocolScope, canonical_sha256
from aletheia.protocols.capabilities import (
    ApplicabilityContract,
    ArtifactKind,
    CalibrationContract,
    CalibrationMode,
    CapabilityCatalog,
    CapabilityManifestV2,
    CapabilityPort,
    DataClassification,
    DeterminismClass,
    FailureCategory,
    FailureDisposition,
    FailureMode,
    LicenseEgressContract,
    NetworkEgressMode,
    PortDirection,
    PrincipalContract,
    PrincipalKind,
    QualificationContract,
    QualificationStatus,
    RetryContract,
    RetryMode,
    RuntimeContract,
    RuntimeKind,
    SafetyClass,
    SafetyContract,
    SideEffectClass,
)
from aletheia.protocols.claim_contracts import (
    CharacterizationContract,
    ClaimAllowance,
    ClaimCeiling,
    ClaimContract,
    ClaimKind,
    ClaimStrength,
    EpistemicKind,
    EpistemicPurpose,
    EstimationContract,
    EvidenceModality,
    HypothesisDiscriminationContract,
    ObservableSpec,
    ObservableValueKind,
    ReplicationTier,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest
from aletheia.protocols.schemas import (
    AnalysisPlan,
    CallerParameterBinding,
    CapabilityAuditBinding,
    CapabilityAuditKind,
    CapabilityRequirement,
    ControlFailureClass,
    ControlSpec,
    DataRole,
    DesignFactor,
    DesignSpaceVersion,
    IdentityLineageContract,
    IndependenceContract,
    MethodVersion,
    ObjectiveContractVersion,
    ObservableOutputBinding,
    PreauthorizationVisibility,
    ProtocolActionCategory,
    ProtocolContractKind,
    ProtocolDataPort,
    ProtocolIR,
    ProtocolPortDirection,
    ProtocolStep,
    ProtocolStepRole,
    ResourceBudgetContract,
    StepContractBinding,
    caller_parameter_manifest_sha256,
)
from aletheia.protocols.typecheck import expected_capability_audit_policy_sha256
from aletheia.protocols.world_models import (
    HypothesisLifecycle,
    HypothesisVersionV2,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.schemas import KernelObjectKind, KernelObjectRef

_NOW = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
_PRINCIPAL = "principal:fixture_author"
_LICENSE_POLICY = hashlib.sha256(b"fixture-license-policy").hexdigest()
_EGRESS_POLICY = hashlib.sha256(b"fixture-egress-policy").hexdigest()
_RETENTION_POLICY = hashlib.sha256(b"fixture-retention-policy").hexdigest()
_UNIT_REFS = ("ontology.domain-neutral.quantity.v1",)
_UNIT_SHA256 = canonical_sha256(_UNIT_REFS)


def digest(label: str) -> str:
    """Return a readable fixture label's stable SHA-256 identity."""

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProtocolFixture:
    name: str
    request: ProtocolCompilationRequest
    expected_dependency_steps: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class _PortSpec:
    port_id: str
    direction: ProtocolPortDirection
    artifact_kind: ArtifactKind
    data_role: DataRole = DataRole.EXPLORATION
    visibility: PreauthorizationVisibility = PreauthorizationVisibility.VISIBLE


@dataclass(frozen=True)
class _StepPlan:
    step_id: str
    capability_id: str
    operation_id: str
    input_port_ids: tuple[str, ...]
    output_port_ids: tuple[str, ...]
    depends_on_step_ids: tuple[str, ...]
    resource_request: ExecutionResourceRequest
    role: ProtocolStepRole
    runtime_kind: RuntimeKind = RuntimeKind.DETERMINISTIC_FUNCTION
    determinism: DeterminismClass = DeterminismClass.DETERMINISTIC
    frozen_seeds: tuple[int, ...] = ()
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    principal_kind: PrincipalKind = PrincipalKind.SERVICE
    physical_hazard: bool = False
    calibrated: bool = False


def _schema_ref(port_id: str) -> JsonSchemaRef:
    return JsonSchemaRef(
        schema_id=f"schema.{port_id}",
        semantic_version="1.0.0",
        schema_sha256=digest(f"schema:{port_id}"),
    )


def _scope(identity: str) -> ProtocolScope:
    quest_id = f"qst_{identity * 32}"
    program_id = f"prg_{identity * 32}"
    campaign_id = f"cmp_{identity * 32}"
    return ProtocolScope(
        scope_binding=ResearchScopeBinding(
            quest_id=quest_id,
            program_id=program_id,
            campaign_id=campaign_id,
        ),
        scope_node_id=campaign_id,
        branch_id=f"rbr_{identity * 32}",
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id=f"question:{identity}",
            object_sha256=digest(f"question:{identity}:v1"),
            quest_id=quest_id,
        ),
        graph_snapshot_sha256=digest(f"graph:{identity}:v1"),
    )


def _claim_ceiling(
    claim_kind: ClaimKind,
    strength: ClaimStrength,
    modality: EvidenceModality,
) -> ClaimCeiling:
    return ClaimCeiling(
        allowances=(ClaimAllowance(kind=claim_kind, maximum_strength=strength),),
        required_evidence_modalities=(modality,),
        required_replication_tier=ReplicationTier.NONE,
        independent_validation_required=True,
        rationale="The frozen design supports only this explicitly bounded claim family.",
    )


def _cpu_resource(class_key: str) -> StaticResourceClass:
    return StaticResourceClass(
        class_key=class_key,
        kind=ResourceKind.CPU,
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="oci-v1",
        cpu_cores=8,
        memory_bytes=16 * 1024**3,
        scratch_bytes=64 * 1024**3,
        features=("deterministic-runtime",),
        network_policies=(NetworkPolicy.NONE,),
        supports_exclusive=True,
    )


def _accelerator_resource() -> StaticResourceClass:
    return StaticResourceClass(
        class_key="resource.accelerator.generic",
        kind=ResourceKind.ACCELERATOR,
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="oci-v1",
        cpu_cores=16,
        memory_bytes=64 * 1024**3,
        scratch_bytes=128 * 1024**3,
        accelerator_model="generic-accelerator-v1",
        accelerator_count=1,
        accelerator_memory_bytes=16 * 1024**3,
        accelerator_compute_capability="7.0",
        features=("deterministic-runtime", "frozen-seed-runtime"),
        network_policies=(NetworkPolicy.NONE,),
        supports_exclusive=True,
        supports_checkpointing=True,
    )


def _external_resource() -> StaticResourceClass:
    return StaticResourceClass(
        class_key="resource.external.measurement-site",
        kind=ResourceKind.EXTERNAL,
        cpu_architecture="site-managed",
        oci_platform="site-managed/v1",
        container_runtime="site-managed",
        cpu_cores=1,
        memory_bytes=1024**3,
        scratch_bytes=4 * 1024**3,
        network_policies=(NetworkPolicy.AUTHENTICATED_EXTERNAL,),
        locality_labels=("site.measurement",),
        external_action_kinds=("measurement.acquire",),
        supports_exclusive=True,
    )


def _resource_request(
    resource: StaticResourceClass,
    *,
    wall_time_seconds: int = 120,
) -> ExecutionResourceRequest:
    if resource.kind is ResourceKind.ACCELERATOR:
        return ExecutionResourceRequest(
            accepted_resource_class_ids=(resource.resource_class_id,),
            cpu_cores=8,
            memory_bytes=16 * 1024**3,
            scratch_bytes=16 * 1024**3,
            wall_time_seconds=wall_time_seconds,
            accelerator_count=1,
            allowed_accelerator_models=(resource.accelerator_model or "",),
            minimum_accelerator_memory_bytes=8 * 1024**3,
            minimum_compute_capability="7.0",
            required_features=("frozen-seed-runtime",),
            network_policy=NetworkPolicy.NONE,
            artifact_quota_bytes=8 * 1024**2,
        )
    if resource.kind is ResourceKind.EXTERNAL:
        return ExecutionResourceRequest(
            accepted_resource_class_ids=(resource.resource_class_id,),
            cpu_cores=1,
            memory_bytes=512 * 1024**2,
            scratch_bytes=1024**3,
            wall_time_seconds=wall_time_seconds,
            required_features=(),
            data_locality=DataLocality.SITE_PINNED,
            locality_labels=("site.measurement",),
            network_policy=NetworkPolicy.AUTHENTICATED_EXTERNAL,
            artifact_quota_bytes=16 * 1024**2,
        )
    return ExecutionResourceRequest(
        accepted_resource_class_ids=(resource.resource_class_id,),
        cpu_cores=2,
        memory_bytes=2 * 1024**3,
        scratch_bytes=4 * 1024**3,
        wall_time_seconds=wall_time_seconds,
        required_features=("deterministic-runtime",),
        network_policy=NetworkPolicy.NONE,
        artifact_quota_bytes=8 * 1024**2,
    )


def _protocol_port(spec: _PortSpec, *, identity_sha256: str) -> ProtocolDataPort:
    return ProtocolDataPort(
        port_id=spec.port_id,
        direction=spec.direction,
        schema_ref=_schema_ref(spec.port_id),
        artifact_kind=spec.artifact_kind,
        data_classification=DataClassification.INTERNAL,
        data_role=spec.data_role,
        preauthorization_visibility=spec.visibility,
        license_policy_sha256=_LICENSE_POLICY,
        egress_policy_sha256=_EGRESS_POLICY,
        identity_schema_sha256=identity_sha256,
        unit_or_ontology_sha256=_UNIT_SHA256,
    )


def _capability_port(spec: _PortSpec, direction: PortDirection) -> CapabilityPort:
    return CapabilityPort(
        port_id=spec.port_id,
        direction=direction,
        schema_ref=_schema_ref(spec.port_id),
        artifact_kind=spec.artifact_kind,
        data_classification=DataClassification.INTERNAL,
        unit_or_ontology_refs=_UNIT_REFS,
        identity_lineage_required=True,
        description=f"Typed domain-neutral port {spec.port_id}.",
    )


def _manifest(
    plan: _StepPlan,
    *,
    ports: dict[str, _PortSpec],
    epistemic_kind: EpistemicKind,
    claim_ceiling: ClaimCeiling,
) -> CapabilityManifestV2:
    audit_kinds = tuple(
        sorted(
            (
                CapabilityAuditKind.APPLICABILITY,
                CapabilityAuditKind.FAILURE_MODES,
                CapabilityAuditKind.SAMPLE_FLOOR,
                CapabilityAuditKind.RUNTIME,
                CapabilityAuditKind.SAFETY,
                CapabilityAuditKind.LICENSE_EGRESS,
                *((CapabilityAuditKind.CALIBRATION,) if plan.calibrated else ()),
            ),
            key=lambda item: item.value,
        )
    )
    audit_sha256s = tuple(
        sorted(
            digest(f"qualification-audit:{plan.capability_id}:{kind.value}") for kind in audit_kinds
        )
    )
    calibration = (
        CalibrationContract(
            mode=CalibrationMode.REFERENCE_STANDARD,
            calibration_receipt_schema=_schema_ref(f"{plan.capability_id}.calibration"),
            maximum_age_seconds=86_400,
            operating_envelope_sha256=digest(f"operating-envelope:{plan.capability_id}"),
        )
        if plan.calibrated
        else CalibrationContract(mode=CalibrationMode.NOT_APPLICABLE)
    )
    safety = (
        SafetyContract(
            safety_class=SafetyClass.PHYSICAL_HAZARD,
            hazard_sha256s=(digest(f"hazard:{plan.capability_id}"),),
            approval_policy_sha256=digest(f"safety-policy:{plan.capability_id}"),
            interlock_receipt_schema=_schema_ref(f"{plan.capability_id}.interlock"),
            emergency_stop_required=True,
        )
        if plan.physical_hazard
        else SafetyContract(
            safety_class=SafetyClass.CONTROLLED_COMPUTE,
            approval_policy_sha256=digest(f"safety-policy:{plan.capability_id}"),
        )
    )
    role_principal = {
        ProtocolStepRole.SCIENTIFIC_EXECUTOR: "principal:fixture_executor",
        ProtocolStepRole.OBSERVATION_PARSER: "principal:fixture_parser",
        ProtocolStepRole.INDEPENDENT_VALIDATOR: "principal:fixture_validator",
        ProtocolStepRole.CONTROL: "principal:fixture_executor",
        ProtocolStepRole.ANALYSIS: "principal:fixture_executor",
        ProtocolStepRole.CALIBRATION: "principal:fixture_executor",
    }[plan.role]
    role_group = {
        ProtocolStepRole.SCIENTIFIC_EXECUTOR: "group.executor",
        ProtocolStepRole.OBSERVATION_PARSER: "group.parser",
        ProtocolStepRole.INDEPENDENT_VALIDATOR: "group.validator",
        ProtocolStepRole.CONTROL: "group.executor",
        ProtocolStepRole.ANALYSIS: "group.executor",
        ProtocolStepRole.CALIBRATION: "group.executor",
    }[plan.role]
    other_groups = tuple(
        sorted(
            {
                "group.claim-approver",
                "group.executor",
                "group.parser",
                "group.validator",
            }
            - {role_group}
        )
    )
    return CapabilityManifestV2(
        capability_id=plan.capability_id,
        semantic_version="2.0.0",
        operation_id=plan.operation_id,
        external_action_kind=(
            "measurement.acquire"
            if plan.runtime_kind
            in {
                RuntimeKind.EXTERNAL_SERVICE,
                RuntimeKind.PHYSICAL_SITE,
                RuntimeKind.HUMAN_PROCEDURE,
            }
            else None
        ),
        title=f"Atomic operation {plan.operation_id}",
        description="One frozen atomic operation used by a domain-neutral protocol fixture.",
        input_ports=tuple(
            _capability_port(ports[item], PortDirection.INPUT) for item in plan.input_port_ids
        ),
        output_ports=tuple(
            _capability_port(ports[item], PortDirection.OUTPUT) for item in plan.output_port_ids
        ),
        side_effect_class=plan.side_effect_class,
        principal=PrincipalContract(
            executor_principal_id=role_principal,
            principal_kind=plan.principal_kind,
            authority_policy_sha256=digest(f"authority:{plan.capability_id}"),
            credential_class="credential.fixture",
            required_independence_groups=other_groups,
        ),
        runtime=RuntimeContract(
            runtime_kind=plan.runtime_kind,
            adapter_ref=f"fixture_adapters:{plan.operation_id.replace('.', '_')}",
            implementation_sha256=digest(f"implementation:{plan.capability_id}"),
            environment_sha256=digest(f"environment:{plan.capability_id}"),
            determinism=plan.determinism,
            frozen_seeds=plan.frozen_seeds,
            maximum_wall_time_seconds=3_600,
            checkpoint_supported=False,
            reconciliation_supported=False,
        ),
        applicability=ApplicabilityContract(
            epistemic_kinds=(epistemic_kind,),
            domain_tags=("domain-neutral",),
            minimum_batch_size=1,
            maximum_batch_size=16,
        ),
        calibration=calibration,
        failure_modes=(
            FailureMode(
                failure_id=f"failure.{plan.step_id}.terminal",
                category=FailureCategory.INVALID_OUTPUT,
                description="Output violates its frozen schema or integrity contract.",
                detection_rule_sha256=digest(f"failure-rule:{plan.capability_id}"),
                disposition=FailureDisposition.TERMINAL,
            ),
        ),
        retry=RetryContract(mode=RetryMode.NEVER, maximum_attempts_per_scientific_slot=1),
        safety=safety,
        license_egress=LicenseEgressContract(
            license_policy_sha256=_LICENSE_POLICY,
            permitted_input_classes=(DataClassification.INTERNAL,),
            output_license_ids=("LicenseRef-Fixture",),
            network_egress=(
                NetworkEgressMode.SITE_MANAGED
                if plan.runtime_kind is RuntimeKind.PHYSICAL_SITE
                else NetworkEgressMode.NONE
            ),
            egress_policy_sha256=_EGRESS_POLICY,
            retention_policy_sha256=_RETENTION_POLICY,
        ),
        qualification=QualificationContract(
            status=QualificationStatus.QUALIFIED,
            qualification_rule_sha256=digest(f"qualification-rule:{plan.capability_id}"),
            evidence_receipt_sha256s=audit_sha256s,
            qualified_by_principal_id="principal:fixture_qualifier",
            qualified_at=_NOW,
        ),
        claim_ceiling=claim_ceiling,
        frozen_by_principal_id="principal:fixture_capability_author",
        frozen_at=_NOW,
    )


def _audit_bindings(manifest: CapabilityManifestV2) -> tuple[CapabilityAuditBinding, ...]:
    kinds = tuple(
        sorted(
            (
                CapabilityAuditKind.APPLICABILITY,
                CapabilityAuditKind.FAILURE_MODES,
                CapabilityAuditKind.SAMPLE_FLOOR,
                CapabilityAuditKind.RUNTIME,
                CapabilityAuditKind.SAFETY,
                CapabilityAuditKind.LICENSE_EGRESS,
                *(
                    (CapabilityAuditKind.CALIBRATION,)
                    if manifest.calibration.mode is not CalibrationMode.NOT_APPLICABLE
                    else ()
                ),
            ),
            key=lambda item: item.value,
        )
    )
    return tuple(
        CapabilityAuditBinding(
            audit_kind=kind,
            capability_manifest_sha256=manifest.manifest_sha256,
            receipt_sha256=digest(f"qualification-audit:{manifest.capability_id}:{kind.value}"),
            audit_policy_sha256=expected_capability_audit_policy_sha256(manifest, kind),
            auditor_principal_id="principal:fixture_auditor",
            valid_from=_NOW,
            expires_at=_NOW + timedelta(days=30),
        )
        for kind in kinds
    )


def _expected_artifact(spec: _PortSpec) -> ExpectedArtifact:
    media_type = {
        ArtifactKind.TABLE: "application/vnd.apache.arrow.file",
        ArtifactKind.JSON: "application/json",
        ArtifactKind.RECEIPT: "application/json",
        ArtifactKind.MEASUREMENT: "application/octet-stream",
        ArtifactKind.MODEL: "application/octet-stream",
    }.get(spec.artifact_kind, "application/octet-stream")
    return ExpectedArtifact(
        artifact_key=spec.port_id,
        role=ArtifactRole.RAW_OUTPUT,
        media_type=media_type,
        schema_sha256=_schema_ref(spec.port_id).schema_sha256,
        max_bytes=4 * 1024**2,
        data_classification=DataClassification.INTERNAL.value,
        retention_policy_sha256=_RETENTION_POLICY,
    )


def _provider_receipt_artifact(step_id: str) -> ExpectedArtifact:
    return ExpectedArtifact(
        artifact_key=f"operational.provider_receipt.{step_id}",
        role=ArtifactRole.PROVIDER_RECEIPT,
        media_type="application/json",
        schema_sha256=digest(f"provider-receipt-schema:{step_id}"),
        max_bytes=64 * 1024,
        data_classification=DataClassification.INTERNAL.value,
        retention_policy_sha256=_RETENTION_POLICY,
    )


def _build_fixture(
    *,
    name: str,
    identity: str,
    action_category: ProtocolActionCategory,
    epistemic_kind: EpistemicKind,
    claim_kind: ClaimKind,
    claim_strength: ClaimStrength,
    evidence_modality: EvidenceModality,
    port_specs: tuple[_PortSpec, ...],
    step_plans: tuple[_StepPlan, ...],
    resources: tuple[StaticResourceClass, ...],
    measurement_step_id: str,
    epistemic_shape: str,
) -> ProtocolFixture:
    scope = _scope(identity)
    scope_sha256 = scope.graph_scope_sha256
    claim_ceiling = _claim_ceiling(claim_kind, claim_strength, evidence_modality)
    ports_by_id = {item.port_id: item for item in port_specs}
    manifests_by_step = {
        plan.step_id: _manifest(
            plan,
            ports=ports_by_id,
            epistemic_kind=epistemic_kind,
            claim_ceiling=claim_ceiling,
        )
        for plan in step_plans
    }
    measurement_manifest = manifests_by_step[measurement_step_id]
    measurement_plan = next(item for item in step_plans if item.step_id == measurement_step_id)
    measurement_output_port_id = next(
        item
        for item in measurement_plan.output_port_ids
        if ports_by_id[item].artifact_kind is not ArtifactKind.RECEIPT
    )
    observable = ObservableSpec(
        observable_id=f"observable.{name}",
        version=1,
        graph_scope_sha256=scope_sha256,
        construct_definition="The preregistered response quantity for the scoped entities.",
        value_kind=ObservableValueKind.CONTINUOUS,
        unit="standardized_unit",
        minimum=-1_000_000.0,
        maximum=1_000_000.0,
        uncertainty_model_sha256=digest(f"uncertainty:{name}"),
        measurement_capability_manifest_sha256=measurement_manifest.manifest_sha256,
        output_schema_sha256=_schema_ref(measurement_output_port_id).schema_sha256,
        unit_or_ontology_sha256=_UNIT_SHA256,
        calibration_contract_sha256=canonical_sha256(measurement_manifest.calibration),
        entity_identity_schema_sha256=digest(f"identity:{name}"),
        semantic_delta="Initial frozen observable contract.",
        authored_by_principal_id=_PRINCIPAL,
        authored_at=_NOW,
    )
    method = MethodVersion(
        method_id=f"method.{name}",
        version=1,
        graph_scope_sha256=scope_sha256,
        method_family="method.domain-neutral",
        method_contract_sha256=digest(f"method-contract:{name}"),
        limitation_sha256s=(digest(f"method-limitation:{name}"),),
        semantic_delta="Initial method version.",
        authored_by_principal_id=_PRINCIPAL,
        authored_at=_NOW,
    )
    analysis_plan = AnalysisPlan(
        primary_endpoint_sha256s=(observable.observable_sha256,),
        estimator_or_likelihood_sha256=digest(f"estimator:{name}"),
        sample_size_or_precision_rule_sha256=digest(f"sample-size:{name}"),
        missingness_rule_sha256=digest(f"missingness:{name}"),
        exclusion_rule_sha256=digest(f"exclusion:{name}"),
        multiplicity_rule_sha256=digest(f"multiplicity:{name}"),
        stopping_rule_sha256=digest(f"stopping:{name}"),
        futility_rule_sha256=digest(f"futility:{name}"),
        positive_decision_rule_sha256=digest(f"positive:{name}"),
        negative_decision_rule_sha256=digest(f"negative:{name}"),
        inconclusive_decision_rule_sha256=digest(f"inconclusive:{name}"),
        robustness_analysis_sha256s=(digest(f"robustness:{name}"),),
        frozen_before_observation=True,
        preregistration_seal_sha256=digest(f"preregistration:{name}"),
    )

    world_model: WorldModelSnapshotV2 | None = None
    if epistemic_shape == "estimation":
        epistemic_contract = EstimationContract(
            contract_id=f"contract.{name}",
            version=1,
            graph_scope_sha256=scope_sha256,
            claim_ceiling=claim_ceiling,
            semantic_delta="Initial frozen estimation contract.",
            authored_by_principal_id=_PRINCIPAL,
            authored_at=_NOW,
            estimand="The preregistered group contrast over the scoped population.",
            target_population_sha256=digest(f"population:{name}"),
            observable_spec_sha256s=(observable.observable_sha256,),
            precision_rule_sha256=digest(f"precision:{name}"),
        )
    elif epistemic_shape == "characterization":
        epistemic_contract = CharacterizationContract(
            contract_id=f"contract.{name}",
            version=1,
            graph_scope_sha256=scope_sha256,
            purpose=EpistemicPurpose.CHARACTERIZE,
            claim_ceiling=claim_ceiling,
            semantic_delta="Initial frozen characterization contract.",
            authored_by_principal_id=_PRINCIPAL,
            authored_at=_NOW,
            target_entity_sha256s=(digest(f"target-entity:{name}"),),
            observable_spec_sha256s=(observable.observable_sha256,),
            coverage_rule_sha256=digest(f"coverage:{name}"),
        )
    elif epistemic_shape == "discrimination":
        hypotheses = tuple(
            HypothesisVersionV2(
                hypothesis_id=f"hyp_{token * 32}",
                version=1,
                graph_scope_sha256=scope_sha256,
                lifecycle=HypothesisLifecycle.ACTIVE,
                statement=f"Structural account {token} predicts a distinct frozen response.",
                explanatory_model=f"Domain-neutral structural account {token}.",
                rationale_sha256=digest(f"hypothesis-rationale:{name}:{token}"),
                semantic_delta="Initial hypothesis version.",
                authored_by_principal_id=_PRINCIPAL,
                authored_at=_NOW,
            )
            for token in ("a", "b")
        )
        target_hashes = tuple(sorted(item.hypothesis_sha256 for item in hypotheses))
        predictions = tuple(
            PredictionVersionV2(
                prediction_id=f"pred_{token * 32}",
                version=1,
                graph_scope_sha256=scope_sha256,
                hypothesis_sha256=hypothesis.hypothesis_sha256,
                observable_spec_sha256=observable.observable_sha256,
                measurement_protocol_sha256=method.method_contract_sha256,
                outcome_space_sha256=analysis_plan.outcome_space_sha256,
                predicted_outcome_sha256=digest(f"predicted-outcome:{name}:{token}"),
                discriminates_from_hypothesis_sha256s=(hypotheses[1 - index].hypothesis_sha256,),
                semantic_delta="Initial discriminating prediction.",
                authored_by_principal_id=_PRINCIPAL,
                authored_at=_NOW,
            )
            for index, (token, hypothesis) in enumerate(zip(("a", "b"), hypotheses, strict=True))
        )
        world_model = WorldModelSnapshotV2(
            graph_scope=scope,
            world_model_id=f"wm_{identity * 32}",
            version=1,
            hypotheses=hypotheses,
            predictions=predictions,
            causal_structure_sha256=digest(f"causal-structure:{name}"),
            model_limitations=("Only the preregistered intervention and response are compared.",),
            semantic_delta="Initial frozen discrimination snapshot.",
            authored_by_principal_id=_PRINCIPAL,
            authored_at=_NOW,
        )
        epistemic_contract = HypothesisDiscriminationContract(
            contract_id=f"contract.{name}",
            version=1,
            graph_scope_sha256=scope_sha256,
            claim_ceiling=claim_ceiling,
            semantic_delta="Initial frozen discrimination contract.",
            authored_by_principal_id=_PRINCIPAL,
            authored_at=_NOW,
            world_model_snapshot_sha256=world_model.world_model_sha256,
            target_hypothesis_sha256s=target_hashes,
            discrimination_rule_sha256=digest(f"discrimination:{name}"),
        )
    else:  # pragma: no cover - fixture authoring guard
        raise ValueError(f"unknown epistemic fixture shape: {epistemic_shape}")

    identity_sha256 = digest(f"identity:{name}")
    data_ports = tuple(
        sorted(
            (_protocol_port(item, identity_sha256=identity_sha256) for item in port_specs),
            key=lambda item: item.port_id,
        )
    )
    factors = (
        DesignFactor(
            factor_id=f"factor.{name}",
            factor_kind="intervention" if epistemic_shape == "discrimination" else "covariate",
            value_schema=_schema_ref(f"factor.{name}"),
            assignment_rule_sha256=digest(f"assignment:{name}"),
            caller_mutable=True,
        ),
    )
    bindings = (
        CallerParameterBinding(
            parameter_id=factors[0].factor_id,
            value_sha256=digest(f"factor-value:{name}"),
        ),
    )
    design_space = DesignSpaceVersion(
        design_space_id=f"design.{name}",
        version=1,
        graph_scope_sha256=scope_sha256,
        population_sha256=digest(f"population:{name}"),
        sampling_unit_schema_sha256=digest(f"sampling-unit:{name}"),
        specimen_genealogy_sha256=digest(f"genealogy:{name}"),
        factors=factors,
        randomization_rule_sha256=digest(f"randomization:{name}"),
        allocation_rule_sha256=digest(f"allocation:{name}"),
        blocking_rule_sha256=digest(f"blocking:{name}"),
        semantic_delta="Initial design-space version.",
        authored_by_principal_id=_PRINCIPAL,
        authored_at=_NOW,
    )
    control = ControlSpec(
        control_id=f"control.{name}",
        catches=tuple(sorted(ControlFailureClass, key=lambda item: item.value)),
        input_port_ids=(
            next(
                item.port_id for item in data_ports if item.direction is ProtocolPortDirection.INPUT
            ),
        ),
        observable_spec_sha256s=(observable.observable_sha256,),
        decision_rule_sha256=digest(f"control-decision:{name}"),
    )
    design_bindings = tuple(
        sorted(
            (
                StepContractBinding(
                    contract_kind=ProtocolContractKind.DESIGN_SPACE,
                    contract_sha256=design_space.design_space_sha256,
                ),
                StepContractBinding(
                    contract_kind=ProtocolContractKind.METHOD,
                    contract_sha256=method.method_sha256,
                ),
                StepContractBinding(
                    contract_kind=ProtocolContractKind.EPISTEMIC,
                    contract_sha256=epistemic_contract.contract_sha256,
                ),
            ),
            key=lambda item: f"{item.contract_kind.value}:{item.contract_sha256}",
        )
    )
    analysis_bindings = tuple(
        sorted(
            (
                StepContractBinding(
                    contract_kind=ProtocolContractKind.CONTROL,
                    contract_sha256=canonical_sha256(control),
                ),
                StepContractBinding(
                    contract_kind=ProtocolContractKind.ANALYSIS,
                    contract_sha256=canonical_sha256(analysis_plan),
                ),
            ),
            key=lambda item: f"{item.contract_kind.value}:{item.contract_sha256}",
        )
    )
    steps = tuple(
        ProtocolStep(
            step_id=plan.step_id,
            role=plan.role,
            capability_requirement=CapabilityRequirement(
                requirement_id=f"requirement.{plan.step_id}",
                operation_id=plan.operation_id,
                capability_id=manifests_by_step[plan.step_id].capability_id,
                semantic_version=manifests_by_step[plan.step_id].semantic_version,
                manifest_sha256=manifests_by_step[plan.step_id].manifest_sha256,
                audit_bindings=_audit_bindings(manifests_by_step[plan.step_id]),
            ),
            depends_on_step_ids=plan.depends_on_step_ids,
            input_port_ids=plan.input_port_ids,
            output_port_ids=plan.output_port_ids,
            resource_request=plan.resource_request,
            expected_artifacts=tuple(
                sorted(
                    (
                        *(_expected_artifact(ports_by_id[item]) for item in plan.output_port_ids),
                        *(
                            (_provider_receipt_artifact(plan.step_id),)
                            if plan.side_effect_class
                            in {
                                SideEffectClass.DURABLE_WRITE,
                                SideEffectClass.EXTERNAL_MUTATION,
                                SideEffectClass.PHYSICAL_ACTION,
                            }
                            else ()
                        ),
                    ),
                    key=lambda item: item.artifact_key,
                )
            ),
            contract_bindings=(
                design_bindings
                if plan.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR
                else analysis_bindings
                if plan.role is ProtocolStepRole.INDEPENDENT_VALIDATOR
                else ()
            ),
            caller_parameter_ids=(
                tuple(item.parameter_id for item in bindings)
                if plan.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR
                else ()
            ),
            operation_batch_size=1,
            replicate_kind=ScientificReplicateKind.PRIMARY,
            replicate_preregistration_sha256=digest(
                f"replicate-preregistration:{name}:{plan.step_id}"
            ),
            replicate_seed_sha256s=(digest(f"replicate-seed:{name}:{plan.step_id}:1"),),
            scientific_replicate_count=1,
            execution_parameters_sha256=digest(f"execution-parameters:{name}:{plan.step_id}"),
            environment_sha256=manifests_by_step[plan.step_id].runtime.environment_sha256,
        )
        for plan in step_plans
    )
    output_identity_hashes = tuple(
        sorted(
            {
                item.identity_schema_sha256
                for item in data_ports
                if item.direction
                in {ProtocolPortDirection.INTERMEDIATE, ProtocolPortDirection.OUTPUT}
            }
        )
    )
    input_identity_hashes = tuple(
        sorted(
            {
                item.identity_schema_sha256
                for item in data_ports
                if item.direction is ProtocolPortDirection.INPUT
            }
        )
    )
    lineage_port_id = next(item.port_id for item in data_ports if item.port_id.endswith("lineage"))
    artifact_budget = sum(
        step.resource_request.artifact_quota_bytes * step.scientific_replicate_count
        for step in steps
    )
    protocol = ProtocolIR(
        protocol_id=f"protocol.{name}",
        version=1,
        graph_scope=scope,
        objective=ObjectiveContractVersion(
            objective_id=f"objective.{name}",
            version=1,
            graph_scope_sha256=scope_sha256,
            action_category=action_category,
            objective=f"Resolve the frozen domain-neutral objective for {name}.",
            candidate_outcome_sha256s=(digest(f"candidate-outcome:{name}"),),
            value_receipt_sha256=digest(f"value-receipt:{name}"),
            semantic_delta="Initial objective version.",
            authored_by_principal_id=_PRINCIPAL,
            authored_at=_NOW,
        ),
        design_space=design_space,
        method=method,
        epistemic_contract=epistemic_contract,
        world_model=world_model,
        observables=(observable,),
        observable_output_bindings=(
            ObservableOutputBinding(
                observable_spec_sha256=observable.observable_sha256,
                producer_step_id=measurement_step_id,
                output_port_id=measurement_output_port_id,
            ),
        ),
        data_ports=data_ports,
        identity_lineage=IdentityLineageContract(
            input_identity_schema_sha256s=input_identity_hashes,
            output_identity_schema_sha256s=output_identity_hashes,
            genealogy_rule_sha256=digest(f"genealogy-rule:{name}"),
            lineage_artifact_port_id=lineage_port_id,
        ),
        controls=(control,),
        analysis_plan=analysis_plan,
        independence=IndependenceContract(
            executor_group_id="group.executor",
            parser_group_id="group.parser",
            validator_group_id="group.validator",
            claim_approver_group_id="group.claim-approver",
            executor_principal_ids=("principal:fixture_executor",),
            parser_principal_ids=("principal:fixture_parser",),
            validator_principal_ids=("principal:fixture_validator",),
            claim_approver_principal_ids=("principal:fixture_claim_approver",),
            policy_sha256=digest(f"independence:{name}"),
        ),
        resource_budget=ResourceBudgetContract(
            currency_code="USD",
            maximum_cost_microunits=100_000_000,
            maximum_total_artifact_bytes=artifact_budget,
            deadline=_NOW + timedelta(days=7),
            budget_authorization_sha256=digest(f"budget-authorization:{name}"),
            checkpoint_policy_sha256=digest(f"checkpoint-policy:{name}"),
            permitted_retention_policy_sha256s=(_RETENTION_POLICY,),
        ),
        steps=steps,
        claim_contract=ClaimContract(
            claim_contract_id=f"claim.{name}",
            graph_scope_sha256=scope_sha256,
            epistemic_kinds=(epistemic_kind,),
            statement=f"The preregistered evidence supports the bounded {claim_kind.value} claim.",
            scope_statement="Only the frozen population, intervention, method, and operating envelope.",
            requested_kind=claim_kind,
            requested_strength=claim_strength,
            ceiling=claim_ceiling,
            decision_rule_sha256=digest(f"claim-decision:{name}"),
        ),
        caller_parameter_bindings=bindings,
        caller_parameter_manifest_sha256=caller_parameter_manifest_sha256(bindings),
        authored_by_principal_id=_PRINCIPAL,
        authored_at=_NOW,
    )
    capability_catalog = CapabilityCatalog(
        manifests=tuple(sorted(manifests_by_step.values(), key=lambda item: item.manifest_sha256))
    )
    resource_catalog = StaticResourceCatalog(
        catalog_key=f"catalog.{name}",
        resource_classes=tuple(sorted(resources, key=lambda item: item.resource_class_id)),
    )
    request = ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=capability_catalog,
        resource_catalog=resource_catalog,
        compiler_implementation_sha256=digest("scientific-protocol-compiler:v1"),
    )
    return ProtocolFixture(
        name=name,
        request=request,
        expected_dependency_steps=tuple(
            (item.step_id, item.depends_on_step_ids) for item in step_plans
        ),
    )


def _grouped_regression_fixture() -> ProtocolFixture:
    cpu = _cpu_resource("resource.cpu.grouped-analysis")
    ports = (
        _PortSpec("input.records", ProtocolPortDirection.INPUT, ArtifactKind.TABLE),
        _PortSpec("intermediate.groups", ProtocolPortDirection.INTERMEDIATE, ArtifactKind.TABLE),
        _PortSpec("intermediate.estimate", ProtocolPortDirection.INTERMEDIATE, ArtifactKind.JSON),
        _PortSpec("output.estimate", ProtocolPortDirection.OUTPUT, ArtifactKind.JSON),
        _PortSpec("output.lineage", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
        _PortSpec("output.validation", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
    )
    plans = (
        _StepPlan(
            "step.01_group",
            "capability.group_records",
            "operation.group_records",
            ("input.records",),
            ("intermediate.groups",),
            (),
            _resource_request(cpu),
            ProtocolStepRole.OBSERVATION_PARSER,
        ),
        _StepPlan(
            "step.02_estimate",
            "capability.estimate_group_contrast",
            "operation.estimate_group_contrast",
            ("intermediate.groups",),
            ("intermediate.estimate", "output.lineage"),
            ("step.01_group",),
            _resource_request(cpu),
            ProtocolStepRole.SCIENTIFIC_EXECUTOR,
        ),
        _StepPlan(
            "step.03_validate",
            "capability.validate_group_estimate",
            "operation.validate_group_estimate",
            ("intermediate.estimate",),
            ("output.estimate", "output.validation"),
            ("step.02_estimate",),
            _resource_request(cpu),
            ProtocolStepRole.INDEPENDENT_VALIDATOR,
        ),
    )
    return _build_fixture(
        name="grouped_regression",
        identity="1",
        action_category=ProtocolActionCategory.DETERMINISTIC_ANALYSIS,
        epistemic_kind=EpistemicKind.ESTIMATION,
        claim_kind=ClaimKind.ASSOCIATIONAL,
        claim_strength=ClaimStrength.TENTATIVE,
        evidence_modality=EvidenceModality.COMPUTATIONAL,
        port_specs=ports,
        step_plans=plans,
        resources=(cpu,),
        measurement_step_id="step.02_estimate",
        epistemic_shape="estimation",
    )


def _structural_intervention_fixture() -> ProtocolFixture:
    accelerator = _accelerator_resource()
    cpu = _cpu_resource("resource.cpu.simulation-analysis")
    ports = (
        _PortSpec("input.structure", ProtocolPortDirection.INPUT, ArtifactKind.MODEL),
        _PortSpec("intermediate.baseline", ProtocolPortDirection.INTERMEDIATE, ArtifactKind.MODEL),
        _PortSpec(
            "intermediate.intervention",
            ProtocolPortDirection.INTERMEDIATE,
            ArtifactKind.MODEL,
        ),
        _PortSpec("intermediate.comparison", ProtocolPortDirection.INTERMEDIATE, ArtifactKind.JSON),
        _PortSpec("output.comparison", ProtocolPortDirection.OUTPUT, ArtifactKind.JSON),
        _PortSpec("output.lineage", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
        _PortSpec("output.validation", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
    )
    plans = (
        _StepPlan(
            "step.01_baseline",
            "capability.simulate_baseline",
            "operation.simulate_baseline",
            ("input.structure",),
            ("intermediate.baseline",),
            (),
            _resource_request(accelerator, wall_time_seconds=300),
            ProtocolStepRole.SCIENTIFIC_EXECUTOR,
            runtime_kind=RuntimeKind.DIGEST_PINNED_CONTAINER,
            determinism=DeterminismClass.FROZEN_SEEDS,
            frozen_seeds=(17,),
            side_effect_class=SideEffectClass.EPHEMERAL_WRITE,
        ),
        _StepPlan(
            "step.02_intervene",
            "capability.simulate_intervention",
            "operation.simulate_intervention",
            ("input.structure",),
            ("intermediate.intervention",),
            (),
            _resource_request(accelerator, wall_time_seconds=300),
            ProtocolStepRole.SCIENTIFIC_EXECUTOR,
            runtime_kind=RuntimeKind.DIGEST_PINNED_CONTAINER,
            determinism=DeterminismClass.FROZEN_SEEDS,
            frozen_seeds=(17,),
            side_effect_class=SideEffectClass.EPHEMERAL_WRITE,
        ),
        _StepPlan(
            "step.03_compare",
            "capability.compare_structural_responses",
            "operation.compare_structural_responses",
            ("intermediate.baseline", "intermediate.intervention"),
            ("intermediate.comparison", "output.lineage"),
            ("step.01_baseline", "step.02_intervene"),
            _resource_request(cpu),
            ProtocolStepRole.OBSERVATION_PARSER,
        ),
        _StepPlan(
            "step.04_validate",
            "capability.validate_structural_comparison",
            "operation.validate_structural_comparison",
            ("intermediate.comparison",),
            ("output.comparison", "output.validation"),
            ("step.03_compare",),
            _resource_request(cpu),
            ProtocolStepRole.INDEPENDENT_VALIDATOR,
        ),
    )
    return _build_fixture(
        name="structural_intervention_simulation",
        identity="2",
        action_category=ProtocolActionCategory.STRUCTURAL_INTERVENTION,
        epistemic_kind=EpistemicKind.HYPOTHESIS_DISCRIMINATION,
        claim_kind=ClaimKind.PREDICTIVE,
        claim_strength=ClaimStrength.TENTATIVE,
        evidence_modality=EvidenceModality.COMPUTATIONAL,
        port_specs=ports,
        step_plans=plans,
        resources=(accelerator, cpu),
        measurement_step_id="step.03_compare",
        epistemic_shape="discrimination",
    )


def _external_measurement_fixture() -> ProtocolFixture:
    external = _external_resource()
    cpu = _cpu_resource("resource.cpu.measurement-parser")
    hidden = {
        "data_role": DataRole.CONFIRMATION,
        "visibility": PreauthorizationVisibility.HIDDEN,
    }
    ports = (
        _PortSpec("input.entity", ProtocolPortDirection.INPUT, ArtifactKind.SAMPLE, **hidden),
        _PortSpec(
            "intermediate.raw_measurement",
            ProtocolPortDirection.INTERMEDIATE,
            ArtifactKind.MEASUREMENT,
            **hidden,
        ),
        _PortSpec(
            "intermediate.parsed_measurement",
            ProtocolPortDirection.INTERMEDIATE,
            ArtifactKind.JSON,
            **hidden,
        ),
        _PortSpec("output.lineage", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT, **hidden),
        _PortSpec("output.measurement", ProtocolPortDirection.OUTPUT, ArtifactKind.JSON, **hidden),
        _PortSpec(
            "output.validation", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT, **hidden
        ),
    )
    plans = (
        _StepPlan(
            "step.01_acquire",
            "capability.acquire_external_measurement",
            "operation.acquire_external_measurement",
            ("input.entity",),
            ("intermediate.raw_measurement",),
            (),
            _resource_request(external, wall_time_seconds=600),
            ProtocolStepRole.SCIENTIFIC_EXECUTOR,
            runtime_kind=RuntimeKind.PHYSICAL_SITE,
            side_effect_class=SideEffectClass.PHYSICAL_ACTION,
            principal_kind=PrincipalKind.INSTRUMENT,
            physical_hazard=True,
            calibrated=True,
        ),
        _StepPlan(
            "step.02_parse",
            "capability.parse_external_measurement",
            "operation.parse_external_measurement",
            ("intermediate.raw_measurement",),
            ("intermediate.parsed_measurement", "output.lineage"),
            ("step.01_acquire",),
            _resource_request(cpu),
            ProtocolStepRole.OBSERVATION_PARSER,
        ),
        _StepPlan(
            "step.03_validate",
            "capability.validate_external_measurement",
            "operation.validate_external_measurement",
            ("intermediate.parsed_measurement",),
            ("output.measurement", "output.validation"),
            ("step.02_parse",),
            _resource_request(cpu),
            ProtocolStepRole.INDEPENDENT_VALIDATOR,
        ),
    )
    return _build_fixture(
        name="external_measurement",
        identity="3",
        action_category=ProtocolActionCategory.EXTERNAL_MEASUREMENT_REQUEST,
        epistemic_kind=EpistemicKind.CHARACTERIZATION,
        claim_kind=ClaimKind.DESCRIPTIVE,
        claim_strength=ClaimStrength.SUPPORTED,
        evidence_modality=EvidenceModality.EMPIRICAL,
        port_specs=ports,
        step_plans=plans,
        resources=(external, cpu),
        measurement_step_id="step.01_acquire",
        epistemic_shape="characterization",
    )


def accepted_protocol_fixtures() -> tuple[ProtocolFixture, ...]:
    """Build all accepted compiler fixtures in stable name order."""

    return (
        _external_measurement_fixture(),
        _grouped_regression_fixture(),
        _structural_intervention_fixture(),
    )


def fixture_by_name(name: str) -> ProtocolFixture:
    """Return one accepted fixture by its stable public name."""

    try:
        return next(item for item in accepted_protocol_fixtures() if item.name == name)
    except StopIteration as exc:
        raise KeyError(name) from exc


__all__ = [
    "ProtocolFixture",
    "accepted_protocol_fixtures",
    "digest",
    "fixture_by_name",
]
