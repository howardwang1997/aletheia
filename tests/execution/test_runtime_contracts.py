from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReason,
    BudgetAuthorization,
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    ExecutionCostQuote,
    NodeEnrollmentAuthorityPin,
    NodeEnrollmentAuthorityVerifier,
    NodeExecutionReceipt,
    NodeHealth,
    NodeInventoryResource,
    NodeRuntimeIdentity,
    QualificationAuthorityPin,
    QualificationAuthorityVerifier,
    QualificationVerificationError,
    RuntimeInspectionState,
    TerminalVerificationAuthorityPin,
    TerminalVerificationAuthorityVerifier,
    VerifiedBudgetAuthorizationResolution,
    VerifiedExecutionReceiptResolution,
    VerifiedInputArtifactResolution,
    WorkerNodeAuthorityVerifier,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
    issue_attempt_adoption_receipt,
    issue_engineering_qualification_grant,
    issue_node_inventory_attestation,
    issue_node_execution_receipt,
    issue_runtime_inspection_receipt,
    issue_terminal_verification_attestation,
    issue_worker_node_enrollment,
    qualification_key_id,
    verify_attempt_adoption,
    verify_engineering_qualification,
    verify_node_inventory_attestation,
    verify_node_execution_receipt,
    verify_runtime_for_adoption,
    verify_runtime_for_release_or_retry,
    verify_worker_node_enrollment,
)
from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    ExecutionFailure,
    ExecutionFailureCategory,
    ExecutionIntent,
    ExecutionReceipt,
    ExecutionTerminalState,
    InfrastructureAttempt,
    InputArtifactBinding,
    NetworkPolicy,
    ResourceKind,
    ScientificReplicateSlot,
    canonical_sha256,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, compile_protocol
from aletheia.protocols.capabilities import (
    CapabilityCatalog,
    CapabilityManifestV2,
    FailureCategory as ProtocolFailureCategory,
    FailureDisposition,
    RetryContract,
    RetryMode,
)
from aletheia.protocols.schemas import (
    ProtocolCompilationResult,
    ProtocolIR,
    ProtocolStep,
    WorkOrderDAG,
    WorkOrderNode,
)
from aletheia.protocols.typecheck import expected_capability_audit_policy_sha256

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import fixture_by_name  # noqa: E402

UTC = timezone.utc
PRIVATE_KEY = bytes(range(32))
ENROLLMENT_PRIVATE_KEY = bytes(range(1, 33))
TERMINAL_PRIVATE_KEY = bytes(range(2, 34))
NOW = datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC)
MAXIMUM_INSPECTION_AGE_SECONDS = 600


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _derived_id(prefix: str, label: str) -> str:
    return f"{prefix}_{_digest(label)[:32]}"


def _public_key_hex(private_key: bytes = PRIVATE_KEY) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _slot(work_order: WorkOrderDAG, node: WorkOrderNode) -> ScientificReplicateSlot:
    return ScientificReplicateSlot(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        slot_count=node.scientific_replicate_count,
        slot_index=1,
        replicate_kind=node.replicate_kind,
        preregistration_sha256=node.replicate_preregistration_sha256,
        randomization_seed_sha256=node.replicate_seed_sha256s[0],
        independent_site_required=node.independent_site_required,
    )


def _retryable_compilation() -> tuple[ProtocolCompilationRequest, ProtocolCompilationResult]:
    base = fixture_by_name("grouped_regression").request
    original_manifest = next(
        item
        for item in base.capability_catalog.manifests
        if item.capability_id == "capability.group_records"
    )
    original_failure = original_manifest.failure_modes[0]
    retryable_failure = type(original_failure).model_validate(
        {
            **original_failure.model_dump(mode="python"),
            "category": ProtocolFailureCategory.INFRASTRUCTURE,
            "disposition": FailureDisposition.RETRYABLE,
        }
    )
    retry_contract = RetryContract(
        mode=RetryMode.IDEMPOTENT_NEW_ATTEMPT,
        maximum_attempts_per_scientific_slot=2,
        retryable_failure_ids=(retryable_failure.failure_id,),
        idempotency_rule_sha256=_digest("group-records-idempotency:v1"),
    )
    retryable_manifest = CapabilityManifestV2.model_validate(
        {
            **original_manifest.model_dump(mode="python"),
            "failure_modes": (retryable_failure,),
            "retry": retry_contract,
        }
    )
    updated_steps: list[ProtocolStep] = []
    for step in base.protocol.steps:
        if step.step_id != "step.01_group":
            updated_steps.append(step)
            continue
        bindings = tuple(
            type(binding).model_validate(
                {
                    **binding.model_dump(mode="python"),
                    "capability_manifest_sha256": retryable_manifest.manifest_sha256,
                    "audit_policy_sha256": expected_capability_audit_policy_sha256(
                        retryable_manifest,
                        binding.audit_kind,
                    ),
                }
            )
            for binding in step.capability_requirement.audit_bindings
        )
        requirement = type(step.capability_requirement).model_validate(
            {
                **step.capability_requirement.model_dump(mode="python"),
                "manifest_sha256": retryable_manifest.manifest_sha256,
                "audit_bindings": bindings,
            }
        )
        resource_request = type(step.resource_request).model_validate(
            {
                **step.resource_request.model_dump(mode="python"),
                "max_infrastructure_attempts": 2,
            }
        )
        updated_steps.append(
            ProtocolStep.model_validate(
                {
                    **step.model_dump(mode="python"),
                    "capability_requirement": requirement,
                    "resource_request": resource_request,
                }
            )
        )
    protocol = ProtocolIR.model_validate(
        {**base.protocol.model_dump(mode="python"), "steps": tuple(updated_steps)}
    )
    manifests = tuple(
        sorted(
            (
                retryable_manifest
                if item.capability_id == retryable_manifest.capability_id
                else item
                for item in base.capability_catalog.manifests
            ),
            key=lambda item: item.manifest_sha256,
        )
    )
    request = ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=CapabilityCatalog(manifests=manifests),
        resource_catalog=base.resource_catalog,
        compiler_implementation_sha256=base.compiler_implementation_sha256,
    )
    result = compile_protocol(request)
    assert result.report.accepted, result.report.blockers
    assert result.work_order is not None
    return request, result


def _protocol_input_resolution(
    *,
    request: ProtocolCompilationRequest,
    input_port_id: str,
    resolved_at: datetime,
) -> VerifiedInputArtifactResolution:
    port = next(item for item in request.protocol.data_ports if item.port_id == input_port_id)
    entry = ArtifactManifestEntry(
        expected_artifact_id=_derived_id("art", f"protocol-input:{input_port_id}"),
        artifact_key=input_port_id,
        role=ArtifactRole.RAW_OUTPUT,
        content_sha256=_digest(f"content:{input_port_id}"),
        bytes=128,
        media_type="application/json",
        schema_sha256=port.schema_ref.schema_sha256,
        quarantine_ref=f"quarantine://protocol-input/{input_port_id}",
    )
    producer_attempt_id = _derived_id("iat", f"protocol-input:{input_port_id}")
    manifest = ArtifactManifest(
        intent_sha256=_digest(f"source-intent:{input_port_id}"),
        execution_id=_derived_id("exe", f"source-execution:{input_port_id}"),
        replicate_slot_id=_derived_id("rps", f"source-slot:{input_port_id}"),
        infrastructure_attempt_id=producer_attempt_id,
        entries=(entry,),
        produced_at=NOW - timedelta(hours=2),
    )
    receipt = ArtifactVerifiedReceipt(
        artifact_manifest_sha256=manifest.manifest_sha256,
        producer_attempt_id=producer_attempt_id,
        artifact=entry,
        custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
        verifier_principal_id="principal:artifact-verifier",
        object_store_id="research-cas",
        final_object_ref=f"cas://sha256/{entry.content_sha256}",
        final_object_version="generation-1",
        verified_at=NOW - timedelta(hours=1),
    )
    return VerifiedInputArtifactResolution(
        verified_receipt_sha256=receipt.verified_receipt_sha256,
        verified_receipt=receipt,
        artifact_manifest=manifest,
        content_rehash_sha256=entry.content_sha256,
        content_bytes=entry.bytes,
        resolved_by_principal_id="principal:archive-resolver",
        resolved_at=resolved_at,
    )


def _intent(
    *,
    work_order: WorkOrderDAG,
    node: WorkOrderNode,
    input_bindings: tuple[InputArtifactBinding, ...],
) -> ExecutionIntent:
    slot = _slot(work_order, node)
    return ExecutionIntent(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_sha256=work_order.work_order_sha256,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        capability_id=node.capability_id,
        capability_manifest_sha256=node.capability_manifest_sha256,
        external_action_kind=node.external_action_kind,
        resource_catalog_sha256=work_order.resource_catalog_sha256,
        resource_request=node.resource_request,
        retry_policy=node.retry_policy,
        replicate_slot=slot,
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=input_bindings,
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=NOW,
        deadline=NOW + timedelta(hours=1),
    )


@dataclass(frozen=True)
class QualificationCase:
    request: ProtocolCompilationRequest
    result: ProtocolCompilationResult
    bundle: EngineeringQualificationBundle
    grant: EngineeringQualificationGrant
    pin: QualificationAuthorityPin
    authority_resolver: _AuthorityResolver
    resolution: VerifiedInputArtifactResolution
    observed_at: datetime


class _Resolver:
    def __init__(self, resolutions: tuple[VerifiedInputArtifactResolution, ...]) -> None:
        self._resolutions = {item.verified_receipt_sha256: item for item in resolutions}

    def resolve_verified_input_artifact(
        self, *, verified_receipt_sha256: str, observed_at: datetime
    ) -> VerifiedInputArtifactResolution | None:
        resolution = self._resolutions.get(verified_receipt_sha256)
        if resolution is None:
            return None
        return resolution.model_copy(update={"resolved_at": observed_at})


class _AuthorityResolver:
    def __init__(
        self,
        bundle: EngineeringQualificationBundle,
        *,
        prior_committed_at: datetime | None = None,
    ) -> None:
        self._quote = bundle.cost_quote
        self._budget = bundle.budget_authorization
        self._prior = bundle.prior_execution_receipt
        self._prior_committed_at = prior_committed_at

    def resolve_execution_cost_quote(
        self, *, cost_quote_sha256: str, observed_at: datetime
    ) -> ExecutionCostQuote | None:
        del observed_at
        return self._quote if self._quote.quote_sha256 == cost_quote_sha256 else None

    def resolve_budget_authorization(
        self, *, source_budget_authorization_sha256: str, observed_at: datetime
    ) -> VerifiedBudgetAuthorizationResolution | None:
        if self._budget.source_budget_authorization_sha256 != source_budget_authorization_sha256:
            return None
        return VerifiedBudgetAuthorizationResolution(
            source_budget_authorization_sha256=source_budget_authorization_sha256,
            source_authorization_canonical_bytes_sha256=(source_budget_authorization_sha256),
            source_authorization_policy_sha256=_digest("budget-source-policy:v1"),
            source_authorization_signature_sha256=_digest("budget-source-signature:v1"),
            budget_authorization_sha256=self._budget.authorization_sha256,
            budget_authorization=self._budget,
            resolved_by_principal_id="principal:budget-registry",
            resolved_at=observed_at,
        )

    def resolve_execution_receipt(
        self, *, execution_receipt_sha256: str, observed_at: datetime
    ) -> VerifiedExecutionReceiptResolution | None:
        if self._prior is None:
            return None
        if self._prior.execution_receipt_sha256 != execution_receipt_sha256:
            return None
        return VerifiedExecutionReceiptResolution(
            execution_receipt_sha256=execution_receipt_sha256,
            execution_receipt=self._prior,
            committed_at=self._prior_committed_at or self._prior.verified_at,
            resolved_by_principal_id="principal:execution-receipt-archive",
            resolved_at=observed_at,
        )


def _signed_case(
    *,
    request: ProtocolCompilationRequest,
    result: ProtocolCompilationResult,
    intent: ExecutionIntent,
    resolution: VerifiedInputArtifactResolution,
    observed_at: datetime,
    prior_execution_receipt: ExecutionReceipt | None = None,
    quote_at: datetime = NOW,
    grant_at: datetime = NOW + timedelta(minutes=1),
    grant_expires_at: datetime = NOW + timedelta(minutes=10),
) -> QualificationCase:
    assert result.work_order is not None
    budget = request.protocol.resource_budget
    budget_authorization = BudgetAuthorization(
        quest_id=intent.quest_id,
        protocol_sha256=intent.protocol_sha256,
        work_order_sha256=intent.work_order_sha256,
        resource_budget_sha256=budget.resource_budget_sha256,
        source_budget_authorization_sha256=budget.budget_authorization_sha256,
        currency_code=budget.currency_code,
        maximum_cost_microunits=budget.maximum_cost_microunits,
        deadline=budget.deadline,
        authorized_by_principal_id="principal:budget-authority",
        authorized_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(minutes=30),
    )
    node_manifest_sha256 = _digest("worker-node-manifest")
    quote = ExecutionCostQuote(
        quest_id=intent.quest_id,
        protocol_sha256=intent.protocol_sha256,
        work_order_sha256=intent.work_order_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        accepted_resource_class_ids=intent.resource_request.accepted_resource_class_ids,
        permitted_node_manifest_sha256s=(node_manifest_sha256,),
        selected_node_manifest_sha256=node_manifest_sha256,
        selected_resource_ids=("cpu.socket-0",),
        currency_code=budget.currency_code,
        rate_card_sha256=_digest("rate-card:v1"),
        fixed_charge_microunits=100,
        charge_per_second_microunits=5,
        maximum_lease_seconds=120,
        maximum_charge_microunits=700,
        pricing_policy_sha256=_digest("pricing-policy:v1"),
        quoted_by_principal_id="principal:allocator",
        quoted_at=quote_at,
        expires_at=NOW + timedelta(minutes=20),
    )
    bundle = EngineeringQualificationBundle(
        compilation_request=request,
        compilation_result=result,
        work_order=result.work_order,
        intent=intent,
        prior_execution_receipt=prior_execution_receipt,
        input_artifact_verified_receipt_sha256s=(resolution.verified_receipt_sha256,),
        budget_authorization=budget_authorization,
        cost_quote=quote,
    )
    public_key = _public_key_hex()
    pin = QualificationAuthorityPin(
        policy_sha256=_digest("qualification-policy:v1"),
        principal_id="principal:qualification-authority",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    authority_resolver = _AuthorityResolver(bundle)
    artifact_resolver = _Resolver((resolution,))
    grant = issue_engineering_qualification_grant(
        bundle,
        pin=pin,
        artifact_resolver=artifact_resolver,
        authority_resolver=authority_resolver,
        private_key=PRIVATE_KEY,
        authorized_at=grant_at,
        expires_at=grant_expires_at,
    )
    return QualificationCase(
        request=request,
        result=result,
        bundle=bundle,
        grant=grant,
        pin=pin,
        authority_resolver=authority_resolver,
        resolution=resolution,
        observed_at=observed_at,
    )


def _qualification_case() -> QualificationCase:
    fixture = fixture_by_name("grouped_regression")
    request = fixture.request
    result = compile_protocol(request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.protocol_step_id == "step.01_group")
    observed_at = NOW + timedelta(minutes=5)
    resolution = _protocol_input_resolution(
        request=request,
        input_port_id=node.input_port_ids[0],
        resolved_at=observed_at,
    )
    binding = InputArtifactBinding(
        input_port_id=node.input_port_ids[0],
        source_kind="protocol_input",
        artifact_verified_receipt_sha256=resolution.verified_receipt_sha256,
    )
    intent = _intent(work_order=work_order, node=node, input_bindings=(binding,))
    return _signed_case(
        request=request,
        result=result,
        intent=intent,
        resolution=resolution,
        observed_at=observed_at,
    )


def _intermediate_qualification_case(*, include_producer_lineage: bool) -> QualificationCase:
    fixture = fixture_by_name("grouped_regression")
    request = fixture.request
    result = compile_protocol(request)
    assert result.work_order is not None
    work_order = result.work_order
    producer_node = next(
        item for item in work_order.nodes if item.protocol_step_id == "step.01_group"
    )
    target_node = next(
        item for item in work_order.nodes if item.protocol_step_id == "step.02_estimate"
    )
    target_port_id = target_node.input_port_ids[0]
    producer_input = InputArtifactBinding(
        input_port_id=producer_node.input_port_ids[0],
        source_kind="protocol_input",
        artifact_verified_receipt_sha256=_digest("producer-protocol-input"),
    )
    producer_intent = _intent(
        work_order=work_order,
        node=producer_node,
        input_bindings=(producer_input,),
    )
    producer_intent = ExecutionIntent.model_validate(
        {
            **producer_intent.model_dump(mode="python"),
            "authorized_at": NOW - timedelta(hours=4),
            "deadline": NOW - timedelta(hours=1),
        }
    )
    expected = next(
        item for item in producer_node.expected_artifacts if item.artifact_key == target_port_id
    )
    entry = ArtifactManifestEntry(
        expected_artifact_id=expected.expected_artifact_id,
        artifact_key=expected.artifact_key,
        role=expected.role,
        content_sha256=_digest("intermediate-groups-content"),
        bytes=128,
        media_type=expected.media_type,
        schema_sha256=expected.schema_sha256,
        quarantine_ref="quarantine://producer/intermediate.groups",
    )
    manifest = ArtifactManifest(
        intent_sha256=producer_intent.intent_sha256,
        execution_id=producer_intent.execution_id,
        replicate_slot_id=producer_intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=(
            producer_intent.infrastructure_attempt.infrastructure_attempt_id
        ),
        entries=(entry,),
        produced_at=NOW - timedelta(hours=2),
    )
    verified_receipt = ArtifactVerifiedReceipt(
        artifact_manifest_sha256=manifest.manifest_sha256,
        producer_attempt_id=(producer_intent.infrastructure_attempt.infrastructure_attempt_id),
        artifact=entry,
        custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
        verifier_principal_id="principal:artifact-verifier",
        object_store_id="research-cas",
        final_object_ref=f"cas://sha256/{entry.content_sha256}",
        final_object_version="generation-1",
        verified_at=NOW - timedelta(minutes=90),
    )
    producer_execution_receipt = ExecutionReceipt(
        intent=producer_intent,
        worker_node_manifest_sha256=_digest("producer-node-manifest"),
        node_inventory_sha256=_digest("producer-node-inventory"),
        resource_lease_sha256=_digest("producer-resource-lease"),
        node_execution_receipt_sha256=_digest("producer-node-execution-receipt"),
        started_at=NOW - timedelta(hours=3),
        ended_at=NOW - timedelta(minutes=100),
        observed_at=NOW - timedelta(minutes=80),
        terminal_state=ExecutionTerminalState.ENGINEERING_SUCCEEDED,
        artifact_manifest=manifest,
        artifact_verified_receipts=(verified_receipt,),
        verified_by_principal_id="principal:execution-verifier",
        verified_at=NOW - timedelta(minutes=70),
    )
    observed_at = NOW + timedelta(minutes=5)
    resolution = VerifiedInputArtifactResolution(
        verified_receipt_sha256=verified_receipt.verified_receipt_sha256,
        verified_receipt=verified_receipt,
        artifact_manifest=manifest,
        producer_execution_receipt=(
            producer_execution_receipt if include_producer_lineage else None
        ),
        content_rehash_sha256=entry.content_sha256,
        content_bytes=entry.bytes,
        resolved_by_principal_id="principal:archive-resolver",
        resolved_at=observed_at,
    )
    producer_slot = producer_intent.replicate_slot
    target_binding = InputArtifactBinding(
        input_port_id=target_port_id,
        source_kind="work_order_output",
        artifact_verified_receipt_sha256=verified_receipt.verified_receipt_sha256,
        source_work_order_node_id=producer_node.node_id,
        source_work_order_node_sha256=producer_node.node_sha256,
        source_replicate_slot_id=producer_slot.replicate_slot_id,
        source_slot_index=producer_slot.slot_index,
    )
    target_intent = _intent(
        work_order=work_order,
        node=target_node,
        input_bindings=(target_binding,),
    )
    return _signed_case(
        request=request,
        result=result,
        intent=target_intent,
        resolution=resolution,
        observed_at=observed_at,
    )


def _retry_qualification_case() -> QualificationCase:
    request, result = _retryable_compilation()
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.protocol_step_id == "step.01_group")
    observed_at = NOW + timedelta(minutes=8)
    resolution = _protocol_input_resolution(
        request=request,
        input_port_id=node.input_port_ids[0],
        resolved_at=observed_at,
    )
    binding = InputArtifactBinding(
        input_port_id=node.input_port_ids[0],
        source_kind="protocol_input",
        artifact_verified_receipt_sha256=resolution.verified_receipt_sha256,
    )
    previous_intent = _intent(
        work_order=work_order,
        node=node,
        input_bindings=(binding,),
    )
    retry_rule = previous_intent.retry_policy.retry_rules[0]
    failure = ExecutionFailure(
        category=ExecutionFailureCategory.INFRASTRUCTURE,
        detail_sha256=_digest("confirmed-worker-loss"),
        capability_failure_id=retry_rule.capability_failure_id,
        capability_failure_detection_rule_sha256=retry_rule.detection_rule_sha256,
        retryable_after_confirmed_termination=True,
    )
    prior_receipt = ExecutionReceipt(
        intent=previous_intent,
        worker_node_manifest_sha256=_digest("retry-node-manifest"),
        node_inventory_sha256=_digest("retry-node-inventory"),
        resource_lease_sha256=_digest("retry-resource-lease"),
        node_execution_receipt_sha256=_digest("retry-node-execution-receipt"),
        started_at=NOW + timedelta(minutes=1),
        ended_at=NOW + timedelta(minutes=3),
        observed_at=NOW + timedelta(minutes=3),
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=failure,
        verified_by_principal_id="principal:execution-verifier",
        verified_at=NOW + timedelta(minutes=4),
    )
    next_attempt = InfrastructureAttempt(
        replicate_slot_id=previous_intent.replicate_slot.replicate_slot_id,
        attempt_number=2,
        previous_attempt_id=(previous_intent.infrastructure_attempt.infrastructure_attempt_id),
        prior_confirmed_failure_receipt_sha256=prior_receipt.execution_receipt_sha256,
        prior_failure_category=failure.category,
    )
    next_intent = ExecutionIntent.model_validate(
        {
            **previous_intent.model_dump(mode="python"),
            "infrastructure_attempt": next_attempt,
        }
    )
    return _signed_case(
        request=request,
        result=result,
        intent=next_intent,
        resolution=resolution,
        observed_at=observed_at,
        prior_execution_receipt=prior_receipt,
        quote_at=NOW + timedelta(minutes=5),
        grant_at=NOW + timedelta(minutes=6),
    )


def test_exact_bundle_is_verified_but_never_scientifically_admitted() -> None:
    case = _qualification_case()

    verified = verify_engineering_qualification(
        bundle=case.bundle,
        grant=case.grant,
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=_Resolver((case.resolution,)),
        authority_resolver=case.authority_resolver,
        observed_at=case.observed_at,
    )

    assert verified.intent_sha256 == case.bundle.intent.intent_sha256
    assert verified.input_artifact_verified_receipt_sha256s == (
        case.resolution.verified_receipt_sha256,
    )
    assert verified.qualification_only is True
    assert verified.scientific_admission_allowed is False
    assert case.grant.message.qualification_only is True
    assert case.grant.message.scientific_admission_allowed is False


def test_real_second_attempt_requires_and_verifies_exact_pr3_retry_lineage() -> None:
    case = _retry_qualification_case()

    verified = verify_engineering_qualification(
        bundle=case.bundle,
        grant=case.grant,
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=_Resolver((case.resolution,)),
        authority_resolver=case.authority_resolver,
        observed_at=case.observed_at,
    )

    assert case.bundle.intent.infrastructure_attempt.attempt_number == 2
    assert case.bundle.prior_execution_receipt is not None
    assert (
        verified.prior_execution_receipt_sha256
        == case.bundle.prior_execution_receipt.execution_receipt_sha256
        == case.grant.message.prior_execution_receipt_sha256
    )
    assert case.grant.message.authorized_at > case.bundle.prior_execution_receipt.verified_at
    assert case.grant.message.authorized_at > case.bundle.intent.authorized_at


def _retry_case_with_prior(
    case: QualificationCase,
    prior_receipt: ExecutionReceipt,
    *,
    deadline: datetime | None = None,
) -> QualificationCase:
    previous = prior_receipt.intent
    attempt = InfrastructureAttempt(
        replicate_slot_id=previous.replicate_slot.replicate_slot_id,
        attempt_number=2,
        previous_attempt_id=previous.infrastructure_attempt.infrastructure_attempt_id,
        prior_confirmed_failure_receipt_sha256=prior_receipt.execution_receipt_sha256,
        prior_failure_category=(
            prior_receipt.failure.category if prior_receipt.failure is not None else None
        ),
    )
    payload = {
        **previous.model_dump(mode="python"),
        "infrastructure_attempt": attempt,
    }
    if deadline is not None:
        payload["deadline"] = deadline
    intent = ExecutionIntent.model_validate(payload)
    return _signed_case(
        request=case.request,
        result=case.result,
        intent=intent,
        resolution=case.resolution,
        observed_at=case.observed_at,
        prior_execution_receipt=prior_receipt,
        quote_at=NOW + timedelta(minutes=5),
        grant_at=NOW + timedelta(minutes=6),
    )


def test_retry_rejects_fabricated_hash_nonretryable_failure_and_field_mutation() -> None:
    case = _retry_qualification_case()
    assert case.bundle.prior_execution_receipt is not None
    prior = case.bundle.prior_execution_receipt

    fabricated_attempt = InfrastructureAttempt(
        replicate_slot_id=case.bundle.intent.replicate_slot.replicate_slot_id,
        attempt_number=2,
        previous_attempt_id=prior.intent.infrastructure_attempt.infrastructure_attempt_id,
        prior_confirmed_failure_receipt_sha256=_digest("fabricated-prior-receipt"),
        prior_failure_category=prior.failure.category if prior.failure is not None else None,
    )
    fabricated_intent = ExecutionIntent.model_validate(
        {
            **prior.intent.model_dump(mode="python"),
            "infrastructure_attempt": fabricated_attempt,
        }
    )
    with pytest.raises(ValidationError, match="exact prior ExecutionReceipt"):
        _signed_case(
            request=case.request,
            result=case.result,
            intent=fabricated_intent,
            resolution=case.resolution,
            observed_at=case.observed_at,
            prior_execution_receipt=prior,
            quote_at=NOW + timedelta(minutes=5),
            grant_at=NOW + timedelta(minutes=6),
        )

    assert prior.failure is not None
    nonretryable_failure = prior.failure.model_copy(
        update={"retryable_after_confirmed_termination": False}
    )
    nonretryable_prior = ExecutionReceipt.model_validate(
        {
            **prior.model_dump(mode="python"),
            "failure": nonretryable_failure,
        }
    )
    nonretryable = _retry_case_with_prior(case, nonretryable_prior)
    with pytest.raises(QualificationVerificationError, match="compilation or intent"):
        verify_engineering_qualification(
            bundle=nonretryable.bundle,
            grant=nonretryable.grant,
            authority=QualificationAuthorityVerifier(nonretryable.pin),
            artifact_resolver=_Resolver((nonretryable.resolution,)),
            authority_resolver=nonretryable.authority_resolver,
            observed_at=nonretryable.observed_at,
        )

    mutated = _retry_case_with_prior(
        case,
        prior,
        deadline=case.bundle.intent.deadline - timedelta(minutes=1),
    )
    with pytest.raises(QualificationVerificationError, match="compilation or intent"):
        verify_engineering_qualification(
            bundle=mutated.bundle,
            grant=mutated.grant,
            authority=QualificationAuthorityVerifier(mutated.pin),
            artifact_resolver=_Resolver((mutated.resolution,)),
            authority_resolver=mutated.authority_resolver,
            observed_at=mutated.observed_at,
        )


def test_retry_grant_cannot_predate_prior_terminal_row_commit() -> None:
    case = _retry_qualification_case()
    assert case.bundle.prior_execution_receipt is not None
    impossible_archive = _AuthorityResolver(
        case.bundle,
        prior_committed_at=case.bundle.prior_execution_receipt.verified_at - timedelta(seconds=1),
    )
    with pytest.raises(QualificationVerificationError, match="resolver returned invalid bytes"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=impossible_archive,
            observed_at=case.observed_at,
        )

    late_archive = _AuthorityResolver(
        case.bundle,
        prior_committed_at=NOW + timedelta(minutes=7),
    )

    with pytest.raises(QualificationVerificationError, match="resolver returned invalid bytes"):
        issue_engineering_qualification_grant(
            case.bundle,
            pin=case.pin,
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=late_archive,
            private_key=PRIVATE_KEY,
            authorized_at=case.grant.message.authorized_at,
            expires_at=case.grant.message.expires_at,
        )

    with pytest.raises(QualificationVerificationError, match="predates committed prior"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=late_archive,
            observed_at=case.observed_at,
        )


def test_intermediate_input_requires_exact_successful_producer_lineage() -> None:
    case = _intermediate_qualification_case(include_producer_lineage=True)
    verified = verify_engineering_qualification(
        bundle=case.bundle,
        grant=case.grant,
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=_Resolver((case.resolution,)),
        authority_resolver=case.authority_resolver,
        observed_at=case.observed_at,
    )
    assert verified.intent_sha256 == case.bundle.intent.intent_sha256

    with pytest.raises(QualificationVerificationError, match="producer ExecutionReceipt"):
        _intermediate_qualification_case(include_producer_lineage=False)


def test_grant_rejects_another_bundle_even_when_the_signature_is_valid() -> None:
    case = _qualification_case()
    alternate_quote = ExecutionCostQuote.model_validate(
        {
            **case.bundle.cost_quote.model_dump(mode="python"),
            "selected_resource_ids": ("cpu.socket-1",),
        }
    )
    alternate = EngineeringQualificationBundle(
        compilation_request=case.request,
        compilation_result=case.result,
        work_order=case.bundle.work_order,
        intent=case.bundle.intent,
        input_artifact_verified_receipt_sha256s=(case.resolution.verified_receipt_sha256,),
        budget_authorization=case.bundle.budget_authorization,
        cost_quote=alternate_quote,
    )

    with pytest.raises(QualificationVerificationError, match="rebound"):
        verify_engineering_qualification(
            bundle=alternate,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=_AuthorityResolver(alternate),
            observed_at=case.observed_at,
        )


def test_verification_recompiles_instead_of_trusting_a_signed_compilation() -> None:
    case = _qualification_case()
    forged_request = ProtocolCompilationRequest.model_validate(
        {
            **case.request.model_dump(mode="python"),
            "compiler_implementation_sha256": _digest("forged-compiler"),
        }
    )
    forged_bundle = EngineeringQualificationBundle(
        compilation_request=forged_request,
        compilation_result=case.result,
        work_order=case.bundle.work_order,
        intent=case.bundle.intent,
        input_artifact_verified_receipt_sha256s=(case.resolution.verified_receipt_sha256,),
        budget_authorization=case.bundle.budget_authorization,
        cost_quote=case.bundle.cost_quote,
    )
    forged_grant = issue_engineering_qualification_grant(
        forged_bundle,
        pin=case.pin,
        artifact_resolver=_Resolver((case.resolution,)),
        authority_resolver=_AuthorityResolver(forged_bundle),
        private_key=PRIVATE_KEY,
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(QualificationVerificationError, match="compilation or intent"):
        verify_engineering_qualification(
            bundle=forged_bundle,
            grant=forged_grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=_AuthorityResolver(forged_bundle),
            observed_at=case.observed_at,
        )


def test_verification_rechecks_intent_against_the_frozen_work_order() -> None:
    case = _qualification_case()
    forged_intent = ExecutionIntent.model_validate(
        {
            **case.bundle.intent.model_dump(mode="python"),
            "command_sha256": _digest("forged-command"),
        }
    )
    forged_quote = ExecutionCostQuote.model_validate(
        {
            **case.bundle.cost_quote.model_dump(mode="python"),
            "intent_sha256": forged_intent.intent_sha256,
        }
    )
    forged_bundle = EngineeringQualificationBundle(
        compilation_request=case.request,
        compilation_result=case.result,
        work_order=case.bundle.work_order,
        intent=forged_intent,
        input_artifact_verified_receipt_sha256s=(case.resolution.verified_receipt_sha256,),
        budget_authorization=case.bundle.budget_authorization,
        cost_quote=forged_quote,
    )
    forged_grant = issue_engineering_qualification_grant(
        forged_bundle,
        pin=case.pin,
        artifact_resolver=_Resolver((case.resolution,)),
        authority_resolver=_AuthorityResolver(forged_bundle),
        private_key=PRIVATE_KEY,
        authorized_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(QualificationVerificationError, match="compilation or intent"):
        verify_engineering_qualification(
            bundle=forged_bundle,
            grant=forged_grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=_AuthorityResolver(forged_bundle),
            observed_at=case.observed_at,
        )


def test_networked_intent_cannot_enter_engineering_qualification() -> None:
    case = _qualification_case()
    networked_request = case.bundle.intent.resource_request.model_copy(
        update={"network_policy": NetworkPolicy.ALLOWLIST}
    )
    networked_intent = case.bundle.intent.model_copy(update={"resource_request": networked_request})

    with pytest.raises(ValidationError, match="network-none"):
        EngineeringQualificationBundle(
            compilation_request=case.request,
            compilation_result=case.result,
            work_order=case.bundle.work_order,
            intent=networked_intent,
            input_artifact_verified_receipt_sha256s=(case.resolution.verified_receipt_sha256,),
            budget_authorization=case.bundle.budget_authorization,
            cost_quote=case.bundle.cost_quote,
        )


def test_missing_or_standalone_avr_cannot_cross_input_resolution_boundary() -> None:
    case = _qualification_case()

    with pytest.raises(QualificationVerificationError, match="absent"):
        issue_engineering_qualification_grant(
            case.bundle,
            pin=case.pin,
            artifact_resolver=_Resolver(()),
            authority_resolver=case.authority_resolver,
            private_key=PRIVATE_KEY,
            authorized_at=case.grant.message.authorized_at,
            expires_at=case.grant.message.expires_at,
        )

    with pytest.raises(QualificationVerificationError, match="absent"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver(()),
            authority_resolver=case.authority_resolver,
            observed_at=case.observed_at,
        )

    class _StandaloneAvrResolver:
        def resolve_verified_input_artifact(
            self, *, verified_receipt_sha256: str, observed_at: datetime
        ):
            del observed_at
            assert verified_receipt_sha256 == case.resolution.verified_receipt_sha256
            return case.resolution.verified_receipt

    with pytest.raises(QualificationVerificationError, match="invalid bytes"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_StandaloneAvrResolver(),
            authority_resolver=case.authority_resolver,
            observed_at=case.observed_at,
        )


def test_input_resolution_must_be_fresh_at_allocator_database_time() -> None:
    case = _qualification_case()
    stale = case.resolution.model_copy(
        update={"resolved_at": case.observed_at - timedelta(seconds=1)}
    )

    class _StaleResolver:
        def resolve_verified_input_artifact(
            self, *, verified_receipt_sha256: str, observed_at: datetime
        ) -> VerifiedInputArtifactResolution | None:
            del observed_at
            if verified_receipt_sha256 != stale.verified_receipt_sha256:
                return None
            return stale

    with pytest.raises(QualificationVerificationError, match="freshly resolved"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_StaleResolver(),
            authority_resolver=case.authority_resolver,
            observed_at=case.observed_at,
        )


def test_budget_quote_and_grant_must_all_be_live_at_allocator_time() -> None:
    case = _qualification_case()
    late = NOW + timedelta(minutes=21)

    with pytest.raises(QualificationVerificationError, match="validity|inactive"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=case.authority_resolver,
            observed_at=late,
        )

    with pytest.raises(QualificationVerificationError, match="timezone-aware UTC"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(case.pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=case.authority_resolver,
            observed_at=late.replace(tzinfo=None),
        )


def test_quote_charge_and_lease_window_are_closed() -> None:
    case = _qualification_case()
    payload = case.bundle.cost_quote.model_dump(mode="python")

    with pytest.raises(ValidationError, match="maximum charge"):
        ExecutionCostQuote.model_validate({**payload, "maximum_charge_microunits": 701})

    too_long = ExecutionCostQuote.model_validate(
        {
            **payload,
            "maximum_lease_seconds": 1_801,
            "maximum_charge_microunits": 100 + 5 * 1_801,
        }
    )
    with pytest.raises(ValidationError, match="quoted lease"):
        EngineeringQualificationBundle(
            compilation_request=case.request,
            compilation_result=case.result,
            work_order=case.bundle.work_order,
            intent=case.bundle.intent,
            input_artifact_verified_receipt_sha256s=(case.resolution.verified_receipt_sha256,),
            budget_authorization=case.bundle.budget_authorization,
            cost_quote=too_long,
        )


def test_qualification_flags_are_literal_closed() -> None:
    case = _qualification_case()
    grant_payload = case.grant.model_dump(mode="python")
    grant_payload["message"]["scientific_admission_allowed"] = True

    with pytest.raises(ValidationError):
        EngineeringQualificationGrant.model_validate(grant_payload)


def _worker_manifest() -> WorkerNodeManifest:
    public_key = _public_key_hex()
    return WorkerNodeManifest(
        node_id="node.v100-01",
        site_id="site.lab-a",
        principal_id="principal:worker-v100-01",
        agent_version="1.0.0",
        agent_implementation_sha256=_digest("node-agent:v1"),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="containerd/2",
        sandbox_policy_sha256=_digest("sandbox-policy:v1"),
        resource_class_ids=("rsc_" + _digest("cpu-class")[:32],),
        allowed_data_classifications=("internal",),
        network_policies=(NetworkPolicy.NONE,),
        egress_policy_sha256=_digest("egress:none"),
        node_signing_key_id=qualification_key_id(public_key),
        node_signing_public_key_ed25519_hex=public_key,
        key_valid_from=NOW - timedelta(days=1),
        key_expires_at=NOW + timedelta(days=1),
        frozen_at=NOW - timedelta(hours=1),
    )


def _node_enrollment_pin(*, revoked_at: datetime | None = None) -> NodeEnrollmentAuthorityPin:
    root_public_key = _public_key_hex(ENROLLMENT_PRIVATE_KEY)
    return NodeEnrollmentAuthorityPin(
        policy_sha256=_digest("node-enrollment-policy:v1"),
        principal_id="principal:deployment-node-enrollment",
        key_id=qualification_key_id(root_public_key),
        public_key_ed25519_hex=root_public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        revoked_at=revoked_at,
    )


def _worker_authority(
    manifest: WorkerNodeManifest | None = None,
    *,
    observed_at: datetime = NOW,
) -> WorkerNodeAuthorityVerifier:
    manifest = manifest or _worker_manifest()
    pin = _node_enrollment_pin()
    enrollment = issue_worker_node_enrollment(
        manifest=manifest,
        pin=pin,
        private_key=ENROLLMENT_PRIVATE_KEY,
        issued_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(hours=12),
    )
    return verify_worker_node_enrollment(
        manifest=manifest,
        enrollment=enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(pin),
        expected_manifest_sha256=manifest.manifest_sha256,
        observed_at=observed_at,
    )


def _cpu_inventory() -> NodeInventoryResource:
    return NodeInventoryResource(
        resource_id="cpu.socket-0",
        kind=ResourceKind.CPU,
        resource_class_ids=("rsc_" + _digest("cpu-class")[:32],),
        health=NodeHealth.HEALTHY,
        cpu_cores_total=16,
        cpu_cores_safety_reserve=2,
        cpu_cores_managed_occupied=2,
        cpu_cores_external_occupied=0,
        cpu_cores_allocatable=12,
        memory_bytes_total=1_000,
        memory_bytes_safety_reserve=100,
        memory_bytes_managed_occupied=100,
        memory_bytes_external_occupied=0,
        memory_bytes_allocatable=800,
        scratch_bytes_total=2_000,
        scratch_bytes_safety_reserve=200,
        scratch_bytes_managed_occupied=200,
        scratch_bytes_external_occupied=0,
        scratch_bytes_allocatable=1_600,
        external_process_count=1,
    )


def _runtime_identity(
    *, manifest: WorkerNodeManifest, intent: ExecutionIntent
) -> NodeRuntimeIdentity:
    return NodeRuntimeIdentity(
        node_id=manifest.node_id,
        boot_id="boot.001",
        execution_id=intent.execution_id,
        infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
        runtime_id="container.runtime-001",
        runtime_engine="containerd/2",
        launch_spec_sha256=_digest("launch-spec:v1"),
        sandbox_instance_sha256=_digest("sandbox-instance:001"),
        process_identity_sha256=_digest("pid-starttime-cgroup:001"),
        started_at=NOW + timedelta(minutes=10),
        started_monotonic_ns=1_000,
    )


def _output_manifest(*, intent: ExecutionIntent, produced_at: datetime) -> ArtifactManifest:
    entries = tuple(
        sorted(
            (
                ArtifactManifestEntry(
                    expected_artifact_id=expected.expected_artifact_id,
                    artifact_key=expected.artifact_key,
                    role=expected.role,
                    content_sha256=_digest(f"output:{expected.artifact_key}"),
                    bytes=min(128, expected.max_bytes),
                    media_type=expected.media_type,
                    schema_sha256=expected.schema_sha256,
                    quarantine_ref=f"quarantine://node/{expected.artifact_key}",
                )
                for expected in intent.expected_artifacts
            ),
            key=lambda item: item.artifact_key,
        )
    )
    return ArtifactManifest(
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
        entries=entries,
        produced_at=produced_at,
    )


def test_node_inventory_is_manifest_signed_fresh_and_same_boot_ordered() -> None:
    manifest = _worker_manifest()
    authority = _worker_authority(manifest)
    first = issue_node_inventory_attestation(
        manifest=manifest,
        boot_id="boot.001",
        sequence=1,
        observed_monotonic_ns=200,
        resources=(_cpu_inventory(),),
        collector_implementation_sha256=_digest("collector:v1"),
        collector_output_sha256=_digest("collector-output:1"),
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        private_key=PRIVATE_KEY,
    )
    second = issue_node_inventory_attestation(
        manifest=manifest,
        boot_id="boot.001",
        sequence=2,
        observed_monotonic_ns=300,
        resources=(_cpu_inventory(),),
        collector_implementation_sha256=_digest("collector:v1"),
        collector_output_sha256=_digest("collector-output:2"),
        observed_at=NOW + timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=2),
        private_key=PRIVATE_KEY,
    )

    verified = verify_node_inventory_attestation(
        attestation=second,
        authority=authority,
        expected_manifest_sha256=manifest.manifest_sha256,
        previous_attestation=first,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert verified.node_inventory_sha256 == second.inventory_sha256
    assert verified.sequence == 2
    assert verified.qualification_only is True
    assert verified.scientific_admission_allowed is False


def test_node_inventory_rejects_signature_rollback_staleness_and_wrong_manifest() -> None:
    manifest = _worker_manifest()
    authority = _worker_authority(manifest)
    first = issue_node_inventory_attestation(
        manifest=manifest,
        boot_id="boot.001",
        sequence=1,
        observed_monotonic_ns=200,
        resources=(_cpu_inventory(),),
        collector_implementation_sha256=_digest("collector:v1"),
        collector_output_sha256=_digest("collector-output:1"),
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        private_key=PRIVATE_KEY,
    )
    rollback = issue_node_inventory_attestation(
        manifest=manifest,
        boot_id="boot.001",
        sequence=2,
        observed_monotonic_ns=199,
        resources=(_cpu_inventory(),),
        collector_implementation_sha256=_digest("collector:v1"),
        collector_output_sha256=_digest("collector-output:2"),
        observed_at=NOW + timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=2),
        private_key=PRIVATE_KEY,
    )

    with pytest.raises(QualificationVerificationError, match="monotonic time"):
        verify_node_inventory_attestation(
            attestation=rollback,
            authority=authority,
            expected_manifest_sha256=manifest.manifest_sha256,
            previous_attestation=first,
            observed_at=NOW + timedelta(minutes=1),
        )

    forged = first.model_copy(update={"signature_ed25519_hex": "f" * 128})
    with pytest.raises(QualificationVerificationError, match="signature"):
        verify_node_inventory_attestation(
            attestation=forged,
            authority=authority,
            expected_manifest_sha256=manifest.manifest_sha256,
            observed_at=NOW + timedelta(minutes=1),
        )

    with pytest.raises(QualificationVerificationError, match="not fresh"):
        verify_node_inventory_attestation(
            attestation=first,
            authority=authority,
            expected_manifest_sha256=manifest.manifest_sha256,
            observed_at=NOW + timedelta(minutes=3),
        )

    with pytest.raises(QualificationVerificationError, match="expected manifest"):
        verify_node_inventory_attestation(
            attestation=first,
            authority=authority,
            expected_manifest_sha256=_digest("different-manifest"),
            observed_at=NOW + timedelta(minutes=1),
        )

    rogue_resource = NodeInventoryResource.model_validate(
        {
            **_cpu_inventory().model_dump(mode="python"),
            "resource_class_ids": ("rsc_" + _digest("unapproved-gpu-class")[:32],),
        }
    )
    unsigned_rogue = first.model_copy(
        update={
            "resources": (rogue_resource,),
            "signature_ed25519_hex": "0" * 128,
        }
    )
    rogue_signature = Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY).sign(
        unsigned_rogue.signature_message
    )
    rogue = unsigned_rogue.model_copy(update={"signature_ed25519_hex": rogue_signature.hex()})
    with pytest.raises(QualificationVerificationError, match="resource class"):
        verify_node_inventory_attestation(
            attestation=rogue,
            authority=authority,
            expected_manifest_sha256=manifest.manifest_sha256,
            observed_at=NOW + timedelta(minutes=1),
        )


def test_worker_node_cannot_self_enroll_or_outlive_deployment_root() -> None:
    trusted_manifest = _worker_manifest()
    trusted_pin = _node_enrollment_pin()
    trusted_enrollment = issue_worker_node_enrollment(
        manifest=trusted_manifest,
        pin=trusted_pin,
        private_key=ENROLLMENT_PRIVATE_KEY,
        issued_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(hours=12),
    )
    authority = verify_worker_node_enrollment(
        manifest=trusted_manifest,
        enrollment=trusted_enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(trusted_pin),
        expected_manifest_sha256=trusted_manifest.manifest_sha256,
        observed_at=NOW,
    )
    assert authority.enrollment_authority_pin == trusted_pin

    attacker_key = bytes(reversed(range(32)))
    attacker_public_key = _public_key_hex(attacker_key)
    attacker_manifest = WorkerNodeManifest.model_validate(
        {
            **trusted_manifest.model_dump(mode="python"),
            "node_id": "node.attacker",
            "principal_id": "principal:attacker",
            "node_signing_key_id": qualification_key_id(attacker_public_key),
            "node_signing_public_key_ed25519_hex": attacker_public_key,
        }
    )
    attacker_pin = NodeEnrollmentAuthorityPin(
        policy_sha256=_digest("attacker-enrollment-policy"),
        principal_id="principal:attacker-root",
        key_id=qualification_key_id(attacker_public_key),
        public_key_ed25519_hex=attacker_public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    attacker_enrollment = issue_worker_node_enrollment(
        manifest=attacker_manifest,
        pin=attacker_pin,
        private_key=attacker_key,
        issued_at=NOW - timedelta(minutes=20),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(QualificationVerificationError, match="deployment-pinned root"):
        verify_worker_node_enrollment(
            manifest=attacker_manifest,
            enrollment=attacker_enrollment,
            enrollment_authority=NodeEnrollmentAuthorityVerifier(trusted_pin),
            expected_manifest_sha256=attacker_manifest.manifest_sha256,
            observed_at=NOW,
        )

    revoked_pin = NodeEnrollmentAuthorityPin.model_validate(
        {
            **trusted_pin.model_dump(mode="python"),
            "revoked_at": NOW + timedelta(minutes=1),
        }
    )
    with pytest.raises(QualificationVerificationError, match="outlives its deployment root"):
        verify_worker_node_enrollment(
            manifest=trusted_manifest,
            enrollment=trusted_enrollment,
            enrollment_authority=NodeEnrollmentAuthorityVerifier(revoked_pin),
            expected_manifest_sha256=trusted_manifest.manifest_sha256,
            observed_at=NOW,
        )


def test_qualification_rejects_revoked_pin_and_unregistered_budget_projection() -> None:
    case = _qualification_case()
    revoked_pin = QualificationAuthorityPin.model_validate(
        {
            **case.pin.model_dump(mode="python"),
            "revoked_at": NOW + timedelta(minutes=6),
        }
    )
    with pytest.raises(QualificationVerificationError, match="outlives"):
        verify_engineering_qualification(
            bundle=case.bundle,
            grant=case.grant,
            authority=QualificationAuthorityVerifier(revoked_pin),
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=case.authority_resolver,
            observed_at=case.observed_at,
        )

    lease_cutoff_pin = QualificationAuthorityPin.model_validate(
        {
            **case.pin.model_dump(mode="python"),
            "revoked_at": NOW + timedelta(minutes=2, seconds=30),
        }
    )
    with pytest.raises(QualificationVerificationError, match="cannot fit"):
        issue_engineering_qualification_grant(
            case.bundle,
            pin=lease_cutoff_pin,
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=case.authority_resolver,
            private_key=PRIVATE_KEY,
            authorized_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=2, seconds=30),
        )

    unregistered = _AuthorityResolver(case.bundle)
    unregistered._budget = BudgetAuthorization.model_validate(
        {
            **case.bundle.budget_authorization.model_dump(mode="python"),
            "authorized_by_principal_id": "principal:forged-budget-authority",
        }
    )
    with pytest.raises(QualificationVerificationError, match="budget projection differs"):
        issue_engineering_qualification_grant(
            case.bundle,
            pin=case.pin,
            artifact_resolver=_Resolver((case.resolution,)),
            authority_resolver=unregistered,
            private_key=PRIVATE_KEY,
            authorized_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )


def test_runtime_state_gates_and_typed_adoption_are_exact() -> None:
    case = _qualification_case()
    manifest = _worker_manifest()
    authority = _worker_authority(manifest)
    runtime = _runtime_identity(manifest=manifest, intent=case.bundle.intent)
    old_token = _digest("lease-token:old")
    new_token = _digest("lease-token:new")
    running = issue_runtime_inspection_receipt(
        manifest=manifest,
        runtime_identity=runtime,
        fencing_epoch=1,
        lease_token_sha256=old_token,
        inspection_sequence=1,
        state=RuntimeInspectionState.RUNNING,
        inspection_evidence_sha256=_digest("runtime-inspection:running"),
        inspected_at=NOW + timedelta(minutes=11),
        inspected_monotonic_ns=2_000,
        expires_at=NOW + timedelta(minutes=16),
        private_key=PRIVATE_KEY,
    )
    verified_running = verify_runtime_for_adoption(
        receipt=running,
        authority=authority,
        expected_runtime_identity=runtime,
        expected_fencing_epoch=1,
        expected_lease_token_sha256=old_token,
        maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
        observed_at=NOW + timedelta(minutes=12),
    )
    assert verified_running.state is RuntimeInspectionState.RUNNING

    with pytest.raises(QualificationVerificationError, match="freshness bound"):
        verify_runtime_for_adoption(
            receipt=running,
            authority=authority,
            expected_runtime_identity=runtime,
            expected_fencing_epoch=1,
            expected_lease_token_sha256=old_token,
            maximum_inspection_age_seconds=30,
            observed_at=NOW + timedelta(minutes=12),
        )
    with pytest.raises(QualificationVerificationError, match="positive deployment bound"):
        verify_runtime_for_adoption(
            receipt=running,
            authority=authority,
            expected_runtime_identity=runtime,
            expected_fencing_epoch=1,
            expected_lease_token_sha256=old_token,
            maximum_inspection_age_seconds=0,
            observed_at=NOW + timedelta(minutes=12),
        )

    with pytest.raises(QualificationVerificationError, match="exact pinned runtime"):
        verify_runtime_for_release_or_retry(
            receipt=running,
            authority=authority,
            expected_runtime_identity=runtime,
            expected_fencing_epoch=1,
            expected_lease_token_sha256=old_token,
            maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
            observed_at=NOW + timedelta(minutes=12),
        )

    adoption = issue_attempt_adoption_receipt(
        manifest=manifest,
        runtime_inspection_receipt=running,
        adoption_sequence=1,
        new_fencing_epoch=2,
        new_lease_token_sha256=new_token,
        reason=AttemptAdoptionReason.CONTROL_PLANE_FAILOVER,
        singleton_lock_evidence_sha256=_digest("singleton-lock:evidence"),
        singleton_lock_acquired_monotonic_ns=2_100,
        allocator_principal_id="principal:allocator",
        adopted_at=NOW + timedelta(minutes=12),
        private_key=PRIVATE_KEY,
    )
    verified_adoption = verify_attempt_adoption(
        receipt=adoption,
        authority=authority,
        expected_runtime_identity=runtime,
        expected_previous_fencing_epoch=1,
        expected_previous_lease_token_sha256=old_token,
        expected_new_fencing_epoch=2,
        expected_new_lease_token_sha256=new_token,
        expected_allocator_principal_id="principal:allocator",
        maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
        observed_at=NOW + timedelta(minutes=13),
    )
    assert verified_adoption.new_fencing_epoch == 2
    assert verified_adoption.new_lease_token_sha256 == new_token

    with pytest.raises(ValidationError):
        type(adoption).model_validate(
            {**adoption.model_dump(mode="python"), "reason": "opaque free-form reason"}
        )
    with pytest.raises(QualificationVerificationError, match="failed closed|transition"):
        verify_attempt_adoption(
            receipt=adoption.model_copy(
                update={"new_lease_token_sha256": _digest("forged-new-token")}
            ),
            authority=authority,
            expected_runtime_identity=runtime,
            expected_previous_fencing_epoch=1,
            expected_previous_lease_token_sha256=old_token,
            expected_new_fencing_epoch=2,
            expected_new_lease_token_sha256=new_token,
            expected_allocator_principal_id="principal:allocator",
            maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
            observed_at=NOW + timedelta(minutes=13),
        )


def test_terminated_runtime_and_node_execution_close_manifest_tree_and_fence() -> None:
    case = _qualification_case()
    intent = case.bundle.intent
    manifest = _worker_manifest()
    authority = _worker_authority(manifest)
    runtime = _runtime_identity(manifest=manifest, intent=intent)
    lease_token = _digest("lease-token:terminal")
    termination = issue_runtime_inspection_receipt(
        manifest=manifest,
        runtime_identity=runtime,
        fencing_epoch=2,
        lease_token_sha256=lease_token,
        inspection_sequence=2,
        state=RuntimeInspectionState.TERMINATED,
        inspection_evidence_sha256=_digest("runtime-inspection:terminated"),
        inspected_at=NOW + timedelta(minutes=14),
        inspected_monotonic_ns=4_000,
        expires_at=NOW + timedelta(minutes=19),
        private_key=PRIVATE_KEY,
    )
    verified_termination = verify_runtime_for_release_or_retry(
        receipt=termination,
        authority=authority,
        expected_runtime_identity=runtime,
        expected_fencing_epoch=2,
        expected_lease_token_sha256=lease_token,
        maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
        observed_at=NOW + timedelta(minutes=15),
    )
    assert verified_termination.state is RuntimeInspectionState.TERMINATED
    with pytest.raises(QualificationVerificationError, match="exact pinned runtime"):
        verify_runtime_for_adoption(
            receipt=termination,
            authority=authority,
            expected_runtime_identity=runtime,
            expected_fencing_epoch=2,
            expected_lease_token_sha256=lease_token,
            maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
            observed_at=NOW + timedelta(minutes=15),
        )

    artifact_manifest = _output_manifest(
        intent=intent,
        produced_at=NOW + timedelta(minutes=12),
    )
    inventory_sha256 = _digest("inventory:terminal")
    lease_sha256 = _digest("resource-lease:terminal")
    node_receipt = issue_node_execution_receipt(
        manifest=manifest,
        intent=intent,
        node_inventory_sha256=inventory_sha256,
        resource_lease_sha256=lease_sha256,
        runtime_identity=runtime,
        fencing_epoch=2,
        lease_token_sha256=lease_token,
        ended_at=NOW + timedelta(minutes=13),
        ended_monotonic_ns=3_000,
        exit_code=0,
        artifact_manifest=artifact_manifest,
        termination_inspection_receipt=termination,
        signed_at=NOW + timedelta(minutes=14, seconds=30),
        private_key=PRIVATE_KEY,
    )
    verified = verify_node_execution_receipt(
        receipt=node_receipt,
        authority=authority,
        expected_intent=intent,
        expected_runtime_identity=runtime,
        expected_node_inventory_sha256=inventory_sha256,
        expected_resource_lease_sha256=lease_sha256,
        expected_artifact_manifest=artifact_manifest,
        expected_fencing_epoch=2,
        expected_lease_token_sha256=lease_token,
        maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
        observed_at=NOW + timedelta(minutes=15),
    )
    assert verified.node_execution_receipt_sha256 == node_receipt.node_execution_receipt_sha256
    assert verified.artifact_manifest_sha256 == artifact_manifest.manifest_sha256
    assert verified.output_tree_sha256 == artifact_output_tree_sha256(artifact_manifest)
    assert verified.confirmed_terminated is True

    moved_custody_entry = artifact_manifest.entries[0].model_copy(
        update={"quarantine_ref": "quarantine://attacker/rebound"}
    )
    moved_custody_manifest = ArtifactManifest.model_validate(
        {
            **artifact_manifest.model_dump(mode="python"),
            "entries": (moved_custody_entry, *artifact_manifest.entries[1:]),
        }
    )
    assert artifact_output_tree_sha256(moved_custody_manifest) == artifact_output_tree_sha256(
        artifact_manifest
    )
    with pytest.raises(QualificationVerificationError, match="exact allocator authority"):
        verify_node_execution_receipt(
            receipt=node_receipt,
            authority=authority,
            expected_intent=intent,
            expected_runtime_identity=runtime,
            expected_node_inventory_sha256=inventory_sha256,
            expected_resource_lease_sha256=lease_sha256,
            expected_artifact_manifest=moved_custody_manifest,
            expected_fencing_epoch=2,
            expected_lease_token_sha256=lease_token,
            maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
            observed_at=NOW + timedelta(minutes=15),
        )

    forged_output = node_receipt.model_copy(
        update={"output_tree_sha256": _digest("forged-output-tree")}
    )
    with pytest.raises(QualificationVerificationError, match="exact allocator authority"):
        verify_node_execution_receipt(
            receipt=forged_output,
            authority=authority,
            expected_intent=intent,
            expected_runtime_identity=runtime,
            expected_node_inventory_sha256=inventory_sha256,
            expected_resource_lease_sha256=lease_sha256,
            expected_artifact_manifest=artifact_manifest,
            expected_fencing_epoch=2,
            expected_lease_token_sha256=lease_token,
            maximum_inspection_age_seconds=MAXIMUM_INSPECTION_AGE_SECONDS,
            observed_at=NOW + timedelta(minutes=15),
        )

    assert isinstance(node_receipt, NodeExecutionReceipt)


def test_terminal_disposition_requires_exact_deployment_signed_authority() -> None:
    case = _qualification_case()
    intent = case.bundle.intent
    worker_manifest = _worker_manifest()
    runtime = _runtime_identity(manifest=worker_manifest, intent=intent)
    lease_token = _digest("lease-token:terminal-verification")
    termination = issue_runtime_inspection_receipt(
        manifest=worker_manifest,
        runtime_identity=runtime,
        fencing_epoch=3,
        lease_token_sha256=lease_token,
        inspection_sequence=3,
        state=RuntimeInspectionState.TERMINATED,
        inspection_evidence_sha256=_digest("runtime-inspection:terminal-verification"),
        inspected_at=NOW + timedelta(minutes=14),
        inspected_monotonic_ns=4_000,
        expires_at=NOW + timedelta(minutes=19),
        private_key=PRIVATE_KEY,
    )
    artifact_manifest = _output_manifest(
        intent=intent,
        produced_at=NOW + timedelta(minutes=12),
    )
    inventory_sha256 = _digest("inventory:terminal-verification")
    lease_sha256 = _digest("resource-lease:terminal-verification")
    node_receipt = issue_node_execution_receipt(
        manifest=worker_manifest,
        intent=intent,
        node_inventory_sha256=inventory_sha256,
        resource_lease_sha256=lease_sha256,
        runtime_identity=runtime,
        fencing_epoch=3,
        lease_token_sha256=lease_token,
        ended_at=NOW + timedelta(minutes=13),
        ended_monotonic_ns=3_000,
        exit_code=17,
        artifact_manifest=artifact_manifest,
        termination_inspection_receipt=termination,
        signed_at=NOW + timedelta(minutes=14, seconds=30),
        private_key=PRIVATE_KEY,
    )
    failure = ExecutionFailure(
        category=ExecutionFailureCategory.PROCESS_ERROR,
        detail_sha256=_digest("terminal-process-error"),
    )
    central_receipt = ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=worker_manifest.manifest_sha256,
        node_inventory_sha256=inventory_sha256,
        resource_lease_sha256=lease_sha256,
        node_execution_receipt_sha256=node_receipt.node_execution_receipt_sha256,
        started_at=runtime.started_at,
        ended_at=node_receipt.ended_at,
        observed_at=NOW + timedelta(minutes=14, seconds=45),
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=failure,
        artifact_manifest=artifact_manifest,
        verified_by_principal_id="principal:terminal-verifier",
        verified_at=NOW + timedelta(minutes=15),
    )
    public_key = _public_key_hex(TERMINAL_PRIVATE_KEY)
    pin = TerminalVerificationAuthorityPin(
        policy_sha256=_digest("terminal-verification-policy:v1"),
        principal_id="principal:terminal-verifier",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    attestation = issue_terminal_verification_attestation(
        execution_receipt=central_receipt,
        node_execution_receipt=node_receipt,
        pin=pin,
        private_key=TERMINAL_PRIVATE_KEY,
        expires_at=NOW + timedelta(minutes=20),
    )
    authority = TerminalVerificationAuthorityVerifier(pin)
    verified = authority.verify(
        attestation=attestation,
        execution_receipt=central_receipt,
        node_execution_receipt=node_receipt,
        observed_at=NOW + timedelta(minutes=16),
    )
    assert verified.execution_receipt_sha256 == central_receipt.execution_receipt_sha256
    assert verified.failure_sha256 == canonical_sha256(failure)
    assert verified.verified_by_principal_id == pin.principal_id
    assert verified.scientific_admission_allowed is False

    forged_failure = failure.model_copy(
        update={"detail_sha256": _digest("forged-terminal-disposition")}
    )
    forged_receipt = ExecutionReceipt.model_validate(
        {
            **central_receipt.model_dump(mode="python"),
            "failure": forged_failure,
        }
    )
    with pytest.raises(QualificationVerificationError, match="exact disposition"):
        authority.verify(
            attestation=attestation,
            execution_receipt=forged_receipt,
            node_execution_receipt=node_receipt,
            observed_at=NOW + timedelta(minutes=16),
        )

    forged_attestation = attestation.model_copy(update={"signature_ed25519_hex": "0" * 128})
    with pytest.raises(QualificationVerificationError, match="signature is invalid"):
        authority.verify(
            attestation=forged_attestation,
            execution_receipt=central_receipt,
            node_execution_receipt=node_receipt,
            observed_at=NOW + timedelta(minutes=16),
        )

    with pytest.raises(QualificationVerificationError, match="inactive"):
        authority.verify(
            attestation=attestation,
            execution_receipt=central_receipt,
            node_execution_receipt=node_receipt,
            observed_at=NOW + timedelta(minutes=20),
        )
