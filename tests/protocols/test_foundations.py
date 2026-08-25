from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from aletheia.protocols import (
    ApplicabilityContract,
    ArtifactKind,
    CalibrationContract,
    CalibrationMode,
    CapabilityCatalog,
    CapabilityManifestV2,
    CapabilityPort,
    CharacterizationContract,
    ClaimAllowance,
    ClaimCeiling,
    ClaimContract,
    ClaimKind,
    ClaimStrength,
    DataClassification,
    DeterminismClass,
    EpistemicContract,
    EpistemicKind,
    EpistemicPurpose,
    EvidenceModality,
    FailureCategory,
    FailureDisposition,
    FailureMode,
    HypothesisDiscriminationContract,
    HypothesisLifecycle,
    HypothesisVersionV2,
    JsonSchemaRef,
    LicenseEgressContract,
    NetworkEgressMode,
    ObservableSpec,
    ObservableValueKind,
    PortDirection,
    PrincipalContract,
    PrincipalKind,
    ProtocolScope,
    QualificationContract,
    QualificationStatus,
    ReplicationTier,
    RetryContract,
    RetryMode,
    RuntimeContract,
    RuntimeKind,
    SafetyClass,
    SafetyContract,
    SideEffectClass,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.schemas import KernelObjectKind, KernelObjectRef

_NOW = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
_QUEST_ID = "qst_" + "1" * 32
_PROGRAM_ID = "prg_" + "2" * 32
_CAMPAIGN_ID = "cmp_" + "3" * 32
_BRANCH_ID = "rbr_" + "4" * 32
_PRINCIPAL_ID = "principal:researcher"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _scope(*, campaign: bool = True) -> ProtocolScope:
    binding = ResearchScopeBinding(
        quest_id=_QUEST_ID,
        program_id=_PROGRAM_ID,
        campaign_id=_CAMPAIGN_ID if campaign else None,
    )
    return ProtocolScope(
        scope_binding=binding,
        scope_node_id=_CAMPAIGN_ID if campaign else _PROGRAM_ID,
        branch_id=_BRANCH_ID,
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id="question:main",
            object_sha256=_digest("question-v3"),
            quest_id=_QUEST_ID,
        ),
        graph_snapshot_sha256=_digest("research-graph-snapshot"),
    )


def _ceiling() -> ClaimCeiling:
    return ClaimCeiling(
        allowances=(
            ClaimAllowance(kind=ClaimKind.CAUSAL, maximum_strength=ClaimStrength.TENTATIVE),
            ClaimAllowance(
                kind=ClaimKind.DESCRIPTIVE,
                maximum_strength=ClaimStrength.CONFIRMED,
            ),
        ),
        required_evidence_modalities=(EvidenceModality.EMPIRICAL,),
        required_replication_tier=ReplicationTier.EXACT_REEXECUTION,
        rationale="Causal language remains tentative until independent external confirmation.",
    )


def _hypothesis(*, suffix: str, scope_sha256: str, derived: tuple[str, ...] = ()):
    return HypothesisVersionV2(
        hypothesis_id="hyp_" + suffix * 32,
        version=1,
        derived_from_hypothesis_sha256s=derived,
        graph_scope_sha256=scope_sha256,
        lifecycle=HypothesisLifecycle.ACTIVE,
        statement=f"Hypothesis {suffix}",
        explanatory_model=f"Model {suffix}",
        rationale_sha256=_digest(f"rationale-{suffix}"),
        semantic_delta="Initial version in this lineage.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
    )


def _schema_ref(label: str) -> JsonSchemaRef:
    return JsonSchemaRef(
        schema_id=f"schema.{label}",
        semantic_version="1.0.0",
        schema_sha256=_digest(f"schema-{label}"),
    )


def _manifest(*, title: str = "Atomic parser") -> CapabilityManifestV2:
    return CapabilityManifestV2(
        capability_id="capability.atomic_parser",
        semantic_version="2.0.0",
        operation_id="operation.parse_measurement",
        title=title,
        description="Parse exactly one immutable raw measurement bundle.",
        input_ports=(
            CapabilityPort(
                port_id="input.raw_measurement",
                direction=PortDirection.INPUT,
                schema_ref=_schema_ref("raw_measurement"),
                artifact_kind=ArtifactKind.MEASUREMENT,
                data_classification=DataClassification.INTERNAL,
                description="Raw measurement bundle.",
            ),
        ),
        output_ports=(
            CapabilityPort(
                port_id="output.parsed_measurement",
                direction=PortDirection.OUTPUT,
                schema_ref=_schema_ref("parsed_measurement"),
                artifact_kind=ArtifactKind.JSON,
                data_classification=DataClassification.INTERNAL,
                description="Typed parsed values.",
            ),
        ),
        side_effect_class=SideEffectClass.NONE,
        principal=PrincipalContract(
            executor_principal_id="principal:parser",
            principal_kind=PrincipalKind.SERVICE,
            authority_policy_sha256=_digest("principal-policy"),
            credential_class="credential.workload_identity",
            required_independence_groups=("execution",),
        ),
        runtime=RuntimeContract(
            runtime_kind=RuntimeKind.DETERMINISTIC_FUNCTION,
            adapter_ref="aletheia.adapters:parse_measurement",
            implementation_sha256=_digest("implementation"),
            environment_sha256=_digest("environment"),
            determinism=DeterminismClass.DETERMINISTIC,
            maximum_wall_time_seconds=60,
            checkpoint_supported=False,
            reconciliation_supported=False,
        ),
        applicability=ApplicabilityContract(
            epistemic_kinds=(
                EpistemicKind.CHARACTERIZATION,
                EpistemicKind.ESTIMATION,
            ),
            domain_tags=("domain_agnostic",),
        ),
        calibration=CalibrationContract(mode=CalibrationMode.NOT_APPLICABLE),
        failure_modes=(
            FailureMode(
                failure_id="failure.invalid_input",
                category=FailureCategory.INVALID_OUTPUT,
                description="Input does not match its frozen schema.",
                detection_rule_sha256=_digest("invalid-input-rule"),
                disposition=FailureDisposition.TERMINAL,
            ),
        ),
        retry=RetryContract(
            mode=RetryMode.NEVER,
            maximum_attempts_per_scientific_slot=1,
        ),
        safety=SafetyContract(
            safety_class=SafetyClass.LOW_RISK_COMPUTE,
            approval_policy_sha256=_digest("safety-policy"),
        ),
        license_egress=LicenseEgressContract(
            license_policy_sha256=_digest("license-policy"),
            permitted_input_classes=(DataClassification.INTERNAL,),
            output_license_ids=("LicenseRef-Proprietary",),
            network_egress=NetworkEgressMode.NONE,
            egress_policy_sha256=_digest("egress-policy"),
            retention_policy_sha256=_digest("retention-policy"),
        ),
        qualification=QualificationContract(
            status=QualificationStatus.PROVISIONAL,
            qualification_rule_sha256=_digest("qualification-rule"),
        ),
        claim_ceiling=_ceiling(),
        frozen_by_principal_id="principal:capability_author",
        frozen_at=_NOW,
    )


def test_protocol_scope_reuses_kernel_question_and_most_specific_node() -> None:
    scope = _scope()
    assert scope.scope_binding.campaign_id == scope.scope_node_id
    assert scope.question_ref.object_kind is KernelObjectKind.QUESTION
    assert ProtocolScope.model_validate_json(scope.model_dump_json()) == scope
    assert len(scope.graph_scope_sha256) == 64

    payload = scope.model_dump(mode="python")
    payload["scope_node_id"] = _PROGRAM_ID
    with pytest.raises(ValidationError, match="most-specific"):
        ProtocolScope.model_validate(payload)

    payload = scope.model_dump(mode="python")
    payload["question_ref"]["object_kind"] = KernelObjectKind.PROBLEM
    with pytest.raises(ValidationError, match="exact kernel question"):
        ProtocolScope.model_validate(payload)


def test_observable_requires_operationalized_numeric_measurement() -> None:
    scope = _scope()
    observable = ObservableSpec(
        observable_id="observable.temperature",
        version=1,
        graph_scope_sha256=scope.graph_scope_sha256,
        construct_definition="Specimen temperature at the preregistered sampling instant.",
        value_kind=ObservableValueKind.CONTINUOUS,
        unit="K",
        minimum=0.0,
        maximum=2_000.0,
        uncertainty_model_sha256=_digest("uncertainty"),
        measurement_capability_manifest_sha256=_digest("measurement-capability"),
        output_schema_sha256=_digest("measurement-output-schema"),
        unit_or_ontology_sha256=_digest("kelvin-ontology"),
        calibration_contract_sha256=_digest("calibration"),
        entity_identity_schema_sha256=_digest("entity-identity"),
        semantic_delta="Initial version.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
    )
    assert observable.observable_sha256 == observable.observable_sha256

    payload = observable.model_dump(mode="python")
    payload["maximum"] = None
    with pytest.raises(ValidationError, match="require unit and range"):
        ObservableSpec.model_validate(payload)


def test_epistemic_union_does_not_force_world_model_on_other_kinds() -> None:
    scope_sha256 = _scope().graph_scope_sha256
    characterization = CharacterizationContract(
        contract_id="contract.characterize",
        version=1,
        graph_scope_sha256=scope_sha256,
        purpose=EpistemicPurpose.CHARACTERIZE,
        claim_ceiling=_ceiling(),
        semantic_delta="Initial characterization contract.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
        target_entity_sha256s=(_digest("entity"),),
        observable_spec_sha256s=(_digest("observable"),),
        coverage_rule_sha256=_digest("coverage"),
    )
    adapter = TypeAdapter(EpistemicContract)
    assert isinstance(
        adapter.validate_python(characterization.model_dump()), CharacterizationContract
    )

    extra = characterization.model_dump(mode="python")
    extra["world_model_snapshot_sha256"] = _digest("invented-world-model")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python(extra)

    discrimination = HypothesisDiscriminationContract(
        contract_id="contract.discriminate",
        version=1,
        graph_scope_sha256=scope_sha256,
        claim_ceiling=_ceiling(),
        semantic_delta="Initial discrimination contract.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
        world_model_snapshot_sha256=_digest("world-model"),
        target_hypothesis_sha256s=tuple(sorted((_digest("h1"), _digest("h2")))),
        discrimination_rule_sha256=_digest("discrimination-rule"),
    )
    assert isinstance(
        adapter.validate_python(discrimination.model_dump()),
        HypothesisDiscriminationContract,
    )


def test_claim_ceiling_is_per_kind_and_rejects_overclaim() -> None:
    scope_sha256 = _scope().graph_scope_sha256
    contract = ClaimContract(
        claim_contract_id="claim.temperature",
        graph_scope_sha256=scope_sha256,
        epistemic_kinds=(
            EpistemicKind.CHARACTERIZATION,
            EpistemicKind.ESTIMATION,
        ),
        statement="The population mean temperature is within the preregistered interval.",
        scope_statement="Only the measured population and operating envelope.",
        requested_kind=ClaimKind.DESCRIPTIVE,
        requested_strength=ClaimStrength.SUPPORTED,
        ceiling=_ceiling(),
        decision_rule_sha256=_digest("claim-decision"),
    )
    assert contract.ceiling.maximum_strength_for(ClaimKind.CAUSAL) is ClaimStrength.TENTATIVE

    payload = contract.model_dump(mode="python")
    payload["requested_kind"] = ClaimKind.CAUSAL
    payload["requested_strength"] = ClaimStrength.CONFIRMED
    with pytest.raises(ValidationError, match="exceeds"):
        ClaimContract.model_validate(payload)


def test_hash_collections_reject_canonical_looking_non_hashes() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        HypothesisDiscriminationContract(
            contract_id="contract.bad_hash",
            version=1,
            graph_scope_sha256=_scope().graph_scope_sha256,
            claim_ceiling=_ceiling(),
            semantic_delta="Initial discrimination contract.",
            authored_by_principal_id=_PRINCIPAL_ID,
            authored_at=_NOW,
            world_model_snapshot_sha256=_digest("world-model"),
            target_hypothesis_sha256s=("not-a-hash-1", "not-a-hash-2"),
            discrimination_rule_sha256=_digest("discrimination-rule"),
        )


def test_world_model_v2_supports_flexible_lineage_without_forced_discrimination() -> None:
    scope = _scope()
    source = _digest("source-hypothesis-in-another-lineage")
    hypothesis = _hypothesis(
        suffix="a",
        scope_sha256=scope.graph_scope_sha256,
        derived=(source,),
    )
    snapshot = WorldModelSnapshotV2(
        graph_scope=scope,
        world_model_id="wm_" + "b" * 32,
        version=1,
        derived_from_snapshot_sha256s=(_digest("source-snapshot"),),
        hypotheses=(hypothesis,),
        model_limitations=("No discrimination claim is made by this snapshot.",),
        semantic_delta="Forked one characterization-relevant model lineage.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
    )
    assert snapshot.predictions == ()
    assert snapshot.belief_state is None
    assert len(snapshot.world_model_sha256) == 64


def test_discrimination_check_is_explicit_bidirectional_and_same_protocol() -> None:
    scope = _scope()
    left = _hypothesis(suffix="a", scope_sha256=scope.graph_scope_sha256)
    right = _hypothesis(suffix="b", scope_sha256=scope.graph_scope_sha256)
    targets = tuple(sorted((left.hypothesis_sha256, right.hypothesis_sha256)))
    common = {
        "graph_scope_sha256": scope.graph_scope_sha256,
        "observable_spec_sha256": _digest("observable"),
        "measurement_protocol_sha256": _digest("measurement-protocol"),
        "outcome_space_sha256": _digest("outcome-space"),
        "semantic_delta": "Initial prediction.",
        "authored_by_principal_id": _PRINCIPAL_ID,
        "authored_at": _NOW,
    }
    predictions = (
        PredictionVersionV2(
            prediction_id="pred_" + "a" * 32,
            version=1,
            hypothesis_sha256=left.hypothesis_sha256,
            predicted_outcome_sha256=_digest("left-outcome"),
            discriminates_from_hypothesis_sha256s=(right.hypothesis_sha256,),
            **common,
        ),
        PredictionVersionV2(
            prediction_id="pred_" + "b" * 32,
            version=1,
            hypothesis_sha256=right.hypothesis_sha256,
            predicted_outcome_sha256=_digest("right-outcome"),
            discriminates_from_hypothesis_sha256s=(left.hypothesis_sha256,),
            **common,
        ),
    )
    snapshot = WorldModelSnapshotV2(
        graph_scope=scope,
        world_model_id="wm_" + "c" * 32,
        version=1,
        hypotheses=(left, right),
        predictions=predictions,
        model_limitations=("Only the frozen outcome space is discriminating.",),
        semantic_delta="Initial discrimination snapshot.",
        authored_by_principal_id=_PRINCIPAL_ID,
        authored_at=_NOW,
    )
    snapshot.assert_hypothesis_discrimination(targets)

    incomplete = snapshot.model_copy(update={"predictions": predictions[:1]})
    with pytest.raises(ValueError, match="bidirectional"):
        incomplete.assert_hypothesis_discrimination(targets)


def test_atomic_capability_manifest_and_catalog_are_exact() -> None:
    manifest = _manifest()
    catalog = CapabilityCatalog(manifests=(manifest,))
    assert catalog.get_exact(manifest.manifest_sha256) == manifest
    assert (
        catalog.resolve_exact(
            capability_id=manifest.capability_id,
            semantic_version=manifest.semantic_version,
            manifest_sha256=manifest.manifest_sha256,
        )
        == manifest
    )
    with pytest.raises(LookupError, match="identity/version"):
        catalog.resolve_exact(
            capability_id=manifest.capability_id,
            semantic_version="2.0.1",
            manifest_sha256=manifest.manifest_sha256,
        )
    with pytest.raises(LookupError, match="exactly once"):
        catalog.get_exact(_digest("unknown-manifest"))

    second = _manifest(title="Same identity, different bytes")
    ordered = tuple(sorted((manifest, second), key=lambda item: item.manifest_sha256))
    with pytest.raises(ValidationError, match="duplicate capability/version"):
        CapabilityCatalog(manifests=ordered)


def test_capability_retry_and_port_contracts_fail_closed() -> None:
    manifest = _manifest()
    payload = manifest.model_dump(mode="python")
    payload["output_ports"][0]["direction"] = PortDirection.INPUT
    with pytest.raises(ValidationError, match="wrong direction"):
        CapabilityManifestV2.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["retry"] = RetryContract(
        mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
        maximum_attempts_per_scientific_slot=2,
        retryable_failure_ids=("failure.timeout",),
        idempotency_rule_sha256=_digest("idempotency-rule"),
    )
    with pytest.raises(ValidationError, match="undeclared failure"):
        CapabilityManifestV2.model_validate(payload)

    with pytest.raises(ValidationError, match="less than or equal to 100"):
        RetryContract(
            mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
            maximum_attempts_per_scientific_slot=101,
            retryable_failure_ids=("failure.timeout",),
            idempotency_rule_sha256=_digest("idempotency-rule"),
        )

    with pytest.raises(ValidationError, match="only reconcile-before-retry"):
        RetryContract(
            mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
            maximum_attempts_per_scientific_slot=2,
            retryable_failure_ids=("failure.timeout",),
            idempotency_rule_sha256=_digest("idempotency-rule"),
            reconciliation_rule_sha256=_digest("stray-reconciliation-rule"),
        )

    payload = manifest.model_dump(mode="python")
    failure_id = payload["failure_modes"][0]["failure_id"]
    payload["failure_modes"][0]["category"] = FailureCategory.INFRASTRUCTURE
    payload["failure_modes"][0]["disposition"] = FailureDisposition.RECONCILIATION_REQUIRED
    payload["retry"] = RetryContract(
        mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
        maximum_attempts_per_scientific_slot=2,
        retryable_failure_ids=(failure_id,),
        idempotency_rule_sha256=_digest("idempotency-rule"),
    )
    with pytest.raises(ValidationError, match="reconciliation-required"):
        CapabilityManifestV2.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    failure_id = payload["failure_modes"][0]["failure_id"]
    payload["failure_modes"][0]["category"] = FailureCategory.SAFETY
    payload["failure_modes"][0]["disposition"] = FailureDisposition.RETRYABLE
    payload["retry"] = RetryContract(
        mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
        maximum_attempts_per_scientific_slot=2,
        retryable_failure_ids=(failure_id,),
        idempotency_rule_sha256=_digest("idempotency-rule"),
    )
    with pytest.raises(ValidationError, match="only infrastructure or execution"):
        CapabilityManifestV2.model_validate(payload)

    with pytest.raises(ValidationError, match="provisional"):
        QualificationContract(
            status=QualificationStatus.PROVISIONAL,
            qualification_rule_sha256=_digest("qualification-rule"),
            evidence_receipt_sha256s=(_digest("premature-evidence"),),
        )
