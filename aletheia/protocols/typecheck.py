"""Fail-closed structural type checker for Scientific Protocol IR v1."""

from __future__ import annotations

from collections.abc import Iterable

from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionEffectClass,
    NetworkPolicy,
    ResourceKind,
    StaticResourceCatalog,
    StaticResourceClass,
)
from aletheia.protocols.base import ProtocolModel, canonical_sha256
from aletheia.protocols.capabilities import (
    ArtifactKind,
    CapabilityCatalog,
    CapabilityManifestV2,
    CalibrationMode,
    NetworkEgressMode,
    PortDirection,
    PortMultiplicity,
    QualificationStatus,
    RetryMode,
    RuntimeKind,
)
from aletheia.protocols.claim_contracts import (
    CharacterizationContract,
    ClaimStrength,
    ConstraintTestContract,
    EstimationContract,
    HypothesisDiscriminationContract,
    ReplicationTier,
)
from aletheia.protocols.schemas import (
    CapabilityAuditKind,
    CompatibilityDimension,
    ControlFailureClass,
    DataRole,
    PreauthorizationVisibility,
    ProtocolBlocker,
    ProtocolBlockerCode,
    ProtocolCheckReport,
    ProtocolIR,
    ProtocolContractKind,
    ProtocolPortDirection,
    ProtocolStepRole,
    caller_parameter_manifest_sha256,
)


class ProtocolCheckRequest(ProtocolModel):
    protocol: ProtocolIR
    capability_catalog: CapabilityCatalog
    resource_catalog: StaticResourceCatalog


_STRENGTH_RANK = {
    ClaimStrength.EXPLORATORY: 0,
    ClaimStrength.TENTATIVE: 1,
    ClaimStrength.SUPPORTED: 2,
    ClaimStrength.CONFIRMED: 3,
}

_REQUIRED_CONTROL_FAILURES = {
    ControlFailureClass.EMPTY_INPUT,
    ControlFailureClass.DATA_LEAKAGE,
    ControlFailureClass.DEGENERACY,
    ControlFailureClass.DRIFT,
}

_REQUIRED_CAPABILITY_AUDITS = {
    CapabilityAuditKind.APPLICABILITY,
    CapabilityAuditKind.FAILURE_MODES,
    CapabilityAuditKind.SAMPLE_FLOOR,
    CapabilityAuditKind.RUNTIME,
    CapabilityAuditKind.SAFETY,
    CapabilityAuditKind.LICENSE_EGRESS,
}

_REPLICATION_RANK = {
    ReplicationTier.NONE: 0,
    ReplicationTier.EXACT_REEXECUTION: 1,
    ReplicationTier.INDEPENDENT_IMPLEMENTATION: 2,
    ReplicationTier.EXTERNAL_INDEPENDENT: 3,
}


def _blocker(
    code: ProtocolBlockerCode,
    *,
    location: str,
    subject_id: str,
    detail: str,
    evidence: Iterable[str] = (),
) -> ProtocolBlocker:
    return ProtocolBlocker(
        code=code,
        location=location,
        subject_id=subject_id,
        detail=detail,
        evidence_sha256s=tuple(sorted(set(evidence))),
    )


def _compatibility_exists(
    protocol: ProtocolIR,
    *,
    dimension: CompatibilityDimension,
    source_sha256: str,
    target_sha256: str,
) -> bool:
    if source_sha256 == target_sha256:
        return True
    return any(
        item.dimension is dimension
        and item.source_sha256 == source_sha256
        and item.target_sha256 == target_sha256
        and item.audit_policy_sha256
        == expected_compatibility_audit_policy_sha256(
            dimension=dimension,
            source_sha256=source_sha256,
            target_sha256=target_sha256,
        )
        for item in protocol.compatibility_audit_receipts
    )


def expected_compatibility_audit_policy_sha256(
    *,
    dimension: CompatibilityDimension,
    source_sha256: str,
    target_sha256: str,
) -> str:
    """Derive the exact directional relation a compatibility audit must evaluate."""

    return canonical_sha256(
        {
            "schema_name": "aletheia.protocol_compatibility_audit_policy",
            "schema_version": 1,
            "dimension": dimension.value,
            "source_sha256": source_sha256,
            "target_sha256": target_sha256,
        }
    )


def expected_capability_audit_policy_sha256(
    manifest: CapabilityManifestV2,
    audit_kind: CapabilityAuditKind,
) -> str:
    """Derive the exact reviewed contract each typed capability audit must evaluate."""

    material = {
        CapabilityAuditKind.APPLICABILITY: manifest.applicability,
        CapabilityAuditKind.FAILURE_MODES: [
            item.model_dump(mode="json", exclude_none=True) for item in manifest.failure_modes
        ],
        CapabilityAuditKind.SAMPLE_FLOOR: {
            "minimum_batch_size": manifest.applicability.minimum_batch_size,
            "maximum_batch_size": manifest.applicability.maximum_batch_size,
        },
        CapabilityAuditKind.RUNTIME: manifest.runtime,
        CapabilityAuditKind.CALIBRATION: manifest.calibration,
        CapabilityAuditKind.SAFETY: manifest.safety,
        CapabilityAuditKind.LICENSE_EGRESS: manifest.license_egress,
    }[audit_kind]
    return canonical_sha256(material)


def _resolve_requirement(
    protocol: ProtocolIR,
    step_index: int,
    catalog: CapabilityCatalog,
) -> tuple[CapabilityManifestV2 | None, tuple[ProtocolBlocker, ...]]:
    step = protocol.steps[step_index]
    requirement = step.capability_requirement
    candidates = tuple(
        manifest
        for manifest in catalog.manifests
        if manifest.operation_id == requirement.operation_id
        and (
            requirement.capability_id is None or manifest.capability_id == requirement.capability_id
        )
        and (
            requirement.semantic_version is None
            or manifest.semantic_version == requirement.semantic_version
        )
        and (
            requirement.manifest_sha256 is None
            or manifest.manifest_sha256 == requirement.manifest_sha256
        )
    )
    location = f"steps[{step_index}].capability_requirement"
    if not candidates:
        return None, (
            _blocker(
                ProtocolBlockerCode.CAPABILITY_UNAVAILABLE,
                location=location,
                subject_id=requirement.requirement_id,
                detail="no frozen capability manifest satisfies the exact selector",
            ),
        )
    if len(candidates) > 1:
        return None, (
            _blocker(
                ProtocolBlockerCode.CAPABILITY_AMBIGUOUS,
                location=location,
                subject_id=requirement.requirement_id,
                detail="capability selector has multiple matches and must be pinned",
                evidence=(item.manifest_sha256 for item in candidates),
            ),
        )
    return candidates[0], ()


def resolve_protocol_capabilities(
    request: ProtocolCheckRequest,
) -> tuple[CapabilityManifestV2 | None, ...]:
    """Resolve the same exact/unique static selection used by the checker and compiler."""

    return tuple(
        _resolve_requirement(request.protocol, index, request.capability_catalog)[0]
        for index in range(len(request.protocol.steps))
    )


def _resource_class_matches(
    resource: StaticResourceClass,
    step_request,
) -> bool:
    if resource.resource_class_id not in step_request.accepted_resource_class_ids:
        return False
    if (
        resource.cpu_cores < step_request.cpu_cores
        or resource.memory_bytes < step_request.memory_bytes
        or resource.scratch_bytes < step_request.scratch_bytes
        or resource.accelerator_count < step_request.accelerator_count
        or step_request.network_policy not in resource.network_policies
        or not set(step_request.required_features).issubset(resource.features)
        or (step_request.exclusive and not resource.supports_exclusive)
        or (step_request.preemptible and not resource.supports_preemption)
        or (
            step_request.checkpoint_interval_seconds is not None
            and not resource.supports_checkpointing
        )
        or not set(step_request.locality_labels).issubset(resource.locality_labels)
    ):
        return False
    if step_request.accelerator_count:
        if resource.kind is not ResourceKind.ACCELERATOR:
            return False
        if resource.accelerator_model not in step_request.allowed_accelerator_models:
            return False
        if (resource.accelerator_memory_bytes or 0) < (
            step_request.minimum_accelerator_memory_bytes or 0
        ):
            return False
        requested = tuple(
            int(part) for part in (step_request.minimum_compute_capability or "0.0").split(".")
        )
        available = tuple(
            int(part) for part in (resource.accelerator_compute_capability or "0.0").split(".")
        )
        if available < requested:
            return False
    return True


def _check_scope_and_epistemics(protocol: ProtocolIR) -> list[ProtocolBlocker]:
    blockers: list[ProtocolBlocker] = []
    subject = protocol.protocol_id
    scope_hash = protocol.graph_scope.graph_scope_sha256
    authored_values = (
        protocol.objective,
        protocol.design_space,
        protocol.method,
        protocol.epistemic_contract,
        *protocol.observables,
        *protocol.compatibility_audit_receipts,
    )
    authored_times = tuple(
        getattr(item, "authored_at", getattr(item, "audited_at", None)) for item in authored_values
    )
    if protocol.world_model is not None:
        authored_times += (protocol.world_model.authored_at,)
    if any(item is not None and item > protocol.authored_at for item in authored_times):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.SCOPE_MISMATCH,
                location="authored_at",
                subject_id=subject,
                detail="protocol cannot freeze scientific objects authored in its future",
            )
        )
    if protocol.objective.graph_scope_sha256 != scope_hash:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.OBJECTIVE_BINDING_MISMATCH,
                location="objective",
                subject_id=subject,
                detail="objective is not bound to the protocol graph scope",
            )
        )
    scope_bound_items = (
        ("design_space", protocol.design_space.graph_scope_sha256),
        ("method", protocol.method.graph_scope_sha256),
        ("epistemic_contract", protocol.epistemic_contract.graph_scope_sha256),
        ("claim_contract", protocol.claim_contract.graph_scope_sha256),
        *(("observables", item.graph_scope_sha256) for item in protocol.observables),
    )
    if protocol.world_model is not None:
        scope_bound_items += (("world_model", protocol.world_model.graph_scope.graph_scope_sha256),)
    for location, item_scope_hash in scope_bound_items:
        if item_scope_hash != scope_hash:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.SCOPE_MISMATCH,
                    location=location,
                    subject_id=subject,
                    detail="protocol components must bind the same exact graph scope",
                    evidence=(scope_hash, item_scope_hash),
                )
            )
    if tuple(item.value for item in protocol.claim_contract.epistemic_kinds) != (
        protocol.epistemic_contract.kind,
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.UNSUPPORTED_CLAIM,
                location="claim_contract.epistemic_kinds",
                subject_id=protocol.claim_contract.claim_contract_id,
                detail="ProtocolIR v1 requires one exact claim/epistemic inference kind",
            )
        )

    compatibility_authorities = {
        protocol.authored_by_principal_id,
        *protocol.independence.executor_principal_ids,
        *protocol.independence.parser_principal_ids,
        *protocol.independence.validator_principal_ids,
        *protocol.independence.claim_approver_principal_ids,
    }
    for receipt in protocol.compatibility_audit_receipts:
        expected_policy = expected_compatibility_audit_policy_sha256(
            dimension=receipt.dimension,
            source_sha256=receipt.source_sha256,
            target_sha256=receipt.target_sha256,
        )
        if (
            receipt.audit_policy_sha256 != expected_policy
            or receipt.audited_by_principal_id in compatibility_authorities
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE,
                    location="compatibility_audit_receipts",
                    subject_id=protocol.protocol_id,
                    detail=(
                        "compatibility audits require the exact directional policy and an "
                        "independent auditor"
                    ),
                    evidence=(receipt.receipt_sha256,),
                )
            )
    if (
        protocol.epistemic_contract.claim_ceiling.ceiling_sha256
        != protocol.claim_contract.ceiling.ceiling_sha256
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.UNSUPPORTED_CLAIM,
                location="claim_contract.ceiling",
                subject_id=protocol.claim_contract.claim_contract_id,
                detail="claim and epistemic contracts must bind the same exact claim ceiling",
                evidence=(
                    protocol.epistemic_contract.claim_ceiling.ceiling_sha256,
                    protocol.claim_contract.ceiling.ceiling_sha256,
                ),
            )
        )

    contract = protocol.epistemic_contract
    observable_hashes = {item.observable_sha256 for item in protocol.observables}
    referenced_observables: tuple[str, ...] = ()
    if isinstance(
        contract,
        (CharacterizationContract, EstimationContract, ConstraintTestContract),
    ):
        referenced_observables = contract.observable_spec_sha256s
    for missing in sorted(set(referenced_observables) - observable_hashes):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.OBSERVABLE_MISSING,
                location="epistemic_contract.observable_spec_sha256s",
                subject_id=contract.contract_id,
                detail="epistemic contract references an observable absent from the protocol",
                evidence=(missing,),
            )
        )

    if isinstance(contract, HypothesisDiscriminationContract):
        if protocol.world_model is None:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.WORLD_MODEL_MISSING,
                    location="world_model",
                    subject_id=contract.contract_id,
                    detail="hypothesis discrimination requires an exact world-model snapshot",
                )
            )
        elif protocol.world_model.world_model_sha256 != contract.world_model_snapshot_sha256:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.WORLD_MODEL_MISMATCH,
                    location="world_model",
                    subject_id=contract.contract_id,
                    detail="world-model bytes do not match the epistemic contract binding",
                    evidence=(
                        contract.world_model_snapshot_sha256,
                        protocol.world_model.world_model_sha256,
                    ),
                )
            )
        else:
            try:
                protocol.world_model.assert_hypothesis_discrimination(
                    contract.target_hypothesis_sha256s
                )
            except ValueError as exc:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.HYPOTHESIS_PREDICTION_MISSING,
                        location="world_model.predictions",
                        subject_id=contract.contract_id,
                        detail=str(exc),
                        evidence=contract.target_hypothesis_sha256s,
                    )
                )
    if protocol.world_model is not None:
        for prediction in protocol.world_model.predictions:
            if prediction.observable_spec_sha256 not in observable_hashes:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.OBSERVABLE_MISSING,
                        location="world_model.predictions",
                        subject_id=prediction.prediction_id,
                        detail="world-model prediction references an undeclared observable",
                        evidence=(prediction.observable_spec_sha256,),
                    )
                )
            if (
                prediction.measurement_protocol_sha256 != protocol.method.method_contract_sha256
                or prediction.outcome_space_sha256 != protocol.analysis_plan.outcome_space_sha256
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.HYPOTHESIS_PREDICTION_MISSING,
                        location="world_model.predictions",
                        subject_id=prediction.prediction_id,
                        detail=(
                            "predictions must bind this protocol's exact method contract and "
                            "preregistered analysis outcome space"
                        ),
                        evidence=(prediction.prediction_sha256,),
                    )
                )
    return blockers


def _check_data_controls_and_lineage(protocol: ProtocolIR) -> list[ProtocolBlocker]:
    blockers: list[ProtocolBlocker] = []
    ports = {item.port_id: item for item in protocol.data_ports}
    observables = {item.observable_sha256 for item in protocol.observables}

    for port in protocol.data_ports:
        if (
            port.data_role
            in {
                DataRole.CONFIRMATION,
                DataRole.REPLICATION,
                DataRole.PRIVATE_VALIDATION,
            }
            and port.preauthorization_visibility is not PreauthorizationVisibility.HIDDEN
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.DATA_ROLE_CONFLICT,
                    location="data_ports",
                    subject_id=port.port_id,
                    detail="confirmation/private/replication data must be hidden before authorization",
                )
            )

    caught = {failure for control in protocol.controls for failure in control.catches}
    missing_controls = _REQUIRED_CONTROL_FAILURES - caught
    if missing_controls:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.CONTROL_COVERAGE_MISSING,
                location="controls",
                subject_id=protocol.protocol_id,
                detail="controls do not cover empty input, leakage, degeneracy, and drift",
                evidence=(canonical_sha256(item.value) for item in missing_controls),
            )
        )
    for control in protocol.controls:
        if (
            (not control.input_port_ids and not control.observable_spec_sha256s)
            or not set(control.input_port_ids).issubset(ports)
            or not set(control.observable_spec_sha256s).issubset(observables)
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CONTROL_COVERAGE_MISSING,
                    location="controls",
                    subject_id=control.control_id,
                    detail=(
                        "every control must bind at least one declared input port or observable"
                    ),
                )
            )

    endpoint_hashes = set(protocol.analysis_plan.primary_endpoint_sha256s) | set(
        protocol.analysis_plan.secondary_endpoint_sha256s
    )
    missing_endpoints = endpoint_hashes - observables
    if missing_endpoints:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.OBSERVABLE_MISSING,
                location="analysis_plan.primary_endpoint_sha256s",
                subject_id=protocol.protocol_id,
                detail="analysis endpoints must bind exact observables declared by the protocol",
                evidence=missing_endpoints,
            )
        )

    lineage = protocol.identity_lineage
    input_identity_hashes = {
        item.identity_schema_sha256
        for item in protocol.data_ports
        if item.direction is ProtocolPortDirection.INPUT
    }
    output_identity_hashes = {
        item.identity_schema_sha256
        for item in protocol.data_ports
        if item.direction in {ProtocolPortDirection.INTERMEDIATE, ProtocolPortDirection.OUTPUT}
    }
    lineage_port = ports.get(lineage.lineage_artifact_port_id)
    if (
        lineage_port is None
        or lineage_port.direction
        not in {
            ProtocolPortDirection.INTERMEDIATE,
            ProtocolPortDirection.OUTPUT,
        }
        or lineage_port.artifact_kind is not ArtifactKind.RECEIPT
        or set(lineage.input_identity_schema_sha256s) != input_identity_hashes
        or set(lineage.output_identity_schema_sha256s) != output_identity_hashes
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.IDENTITY_LINEAGE_OPEN,
                location="identity_lineage",
                subject_id=protocol.protocol_id,
                detail="sample/specimen identity lineage is not closed over declared ports",
            )
        )

    if not protocol.analysis_plan.frozen_before_observation:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.ANALYSIS_NOT_PREREGISTERED,
                location="analysis_plan",
                subject_id=protocol.protocol_id,
                detail="analysis, exclusion, multiplicity, and stopping must be frozen pre-observation",
            )
        )

    total_artifact_quota = sum(
        step.resource_request.artifact_quota_bytes * step.scientific_replicate_count
        for step in protocol.steps
    )
    permitted_retention = set(protocol.resource_budget.permitted_retention_policy_sha256s)
    declared_retention = {
        artifact.retention_policy_sha256
        for step in protocol.steps
        for artifact in step.expected_artifacts
    }
    artifact_quota_exceeded = any(
        sum(artifact.max_bytes for artifact in step.expected_artifacts)
        > step.resource_request.artifact_quota_bytes
        for step in protocol.steps
    )
    if (
        protocol.resource_budget.deadline <= protocol.authored_at
        or total_artifact_quota > protocol.resource_budget.maximum_total_artifact_bytes
        or artifact_quota_exceeded
        or not declared_retention.issubset(permitted_retention)
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE,
                location="resource_budget",
                subject_id=protocol.protocol_id,
                detail="deadline, artifact quota, or retention exceeds the frozen resource envelope",
            )
        )

    groups = (
        protocol.independence.executor_group_id,
        protocol.independence.parser_group_id,
        protocol.independence.validator_group_id,
        protocol.independence.claim_approver_group_id,
    )
    principal_sets = (
        set(protocol.independence.executor_principal_ids),
        set(protocol.independence.parser_principal_ids),
        set(protocol.independence.validator_principal_ids),
        set(protocol.independence.claim_approver_principal_ids),
    )
    principals_are_disjoint = sum(len(item) for item in principal_sets) == len(
        set().union(*principal_sets)
    )
    required_roles = {
        ProtocolStepRole.SCIENTIFIC_EXECUTOR,
        ProtocolStepRole.OBSERVATION_PARSER,
        ProtocolStepRole.INDEPENDENT_VALIDATOR,
    }
    if (
        len(set(groups)) != len(groups)
        or not principals_are_disjoint
        or not required_roles.issubset({item.role for item in protocol.steps})
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.INDEPENDENCE_CONFLICT,
                location="independence",
                subject_id=protocol.protocol_id,
                detail="executor, parser, validator, and claim approver must be independent",
            )
        )
    mutable = {item.factor_id for item in protocol.design_space.factors if item.caller_mutable}
    bindings = {item.parameter_id for item in protocol.caller_parameter_bindings}
    executor_step_parameters = {
        item
        for step in protocol.steps
        if step.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR
        for item in step.caller_parameter_ids
    }
    expected_manifest = caller_parameter_manifest_sha256(protocol.caller_parameter_bindings)
    if (
        mutable != bindings
        or bindings != executor_step_parameters
        or any(not set(step.caller_parameter_ids).issubset(bindings) for step in protocol.steps)
        or any(
            step.caller_parameter_ids and step.role is not ProtocolStepRole.SCIENTIFIC_EXECUTOR
            for step in protocol.steps
        )
        or protocol.caller_parameter_manifest_sha256 != expected_manifest
    ):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.PARAMETER_HASH_UNCOVERED,
                location="caller_parameter_bindings",
                subject_id=protocol.protocol_id,
                detail=(
                    "every caller-mutable design factor must appear in the exact parameter hash "
                    "and at least one explicit step binding"
                ),
            )
        )

    required_contracts = {
        (ProtocolContractKind.DESIGN_SPACE, protocol.design_space.design_space_sha256),
        (ProtocolContractKind.METHOD, protocol.method.method_sha256),
        (ProtocolContractKind.EPISTEMIC, protocol.epistemic_contract.contract_sha256),
        *((ProtocolContractKind.CONTROL, canonical_sha256(item)) for item in protocol.controls),
        (ProtocolContractKind.ANALYSIS, canonical_sha256(protocol.analysis_plan)),
    }
    actual_contracts = {
        (binding.contract_kind, binding.contract_sha256)
        for step in protocol.steps
        for binding in step.contract_bindings
    }
    allowed_roles = {
        ProtocolContractKind.DESIGN_SPACE: {ProtocolStepRole.SCIENTIFIC_EXECUTOR},
        ProtocolContractKind.METHOD: {ProtocolStepRole.SCIENTIFIC_EXECUTOR},
        ProtocolContractKind.EPISTEMIC: {ProtocolStepRole.SCIENTIFIC_EXECUTOR},
        ProtocolContractKind.CONTROL: {
            ProtocolStepRole.CONTROL,
            ProtocolStepRole.INDEPENDENT_VALIDATOR,
        },
        ProtocolContractKind.ANALYSIS: {
            ProtocolStepRole.ANALYSIS,
            ProtocolStepRole.INDEPENDENT_VALIDATOR,
        },
    }
    role_mismatch = any(
        step.role not in allowed_roles[binding.contract_kind]
        for step in protocol.steps
        for binding in step.contract_bindings
    )
    if actual_contracts != required_contracts or role_mismatch:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.PARAMETER_HASH_UNCOVERED,
                location="steps.contract_bindings",
                subject_id=protocol.protocol_id,
                detail=(
                    "work steps must cover exactly every design, method, epistemic, control, "
                    "and analysis contract under an eligible role"
                ),
            )
        )
    return blockers


def _check_dag(protocol: ProtocolIR) -> list[ProtocolBlocker]:
    blockers: list[ProtocolBlocker] = []
    steps = {item.step_id: item for item in protocol.steps}
    ports = {item.port_id: item for item in protocol.data_ports}
    producers: dict[str, str] = {}

    for index, step in enumerate(protocol.steps):
        expected_artifacts = {item.artifact_key: item for item in step.expected_artifacts}
        raw_output_keys = {
            item.artifact_key
            for item in step.expected_artifacts
            if item.role is ArtifactRole.RAW_OUTPUT
        }
        unknown_dependencies = set(step.depends_on_step_ids) - steps.keys()
        unknown_ports = (set(step.input_port_ids) | set(step.output_port_ids)) - ports.keys()
        if unknown_dependencies or unknown_ports or step.step_id in step.depends_on_step_ids:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_UNBOUND,
                    location=f"steps[{index}]",
                    subject_id=step.step_id,
                    detail="step references an unknown/self dependency or undeclared port",
                )
            )
        if raw_output_keys != set(step.output_port_ids):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_UNBOUND,
                    location=f"steps[{index}].expected_artifacts",
                    subject_id=step.step_id,
                    detail="raw-output artifacts must bind exactly every declared output port",
                )
            )
        for port_id in step.output_port_ids:
            if port_id in producers:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location=f"steps[{index}].output_port_ids",
                        subject_id=step.step_id,
                        detail="protocol port has multiple producers",
                    )
                )
            producers[port_id] = step.step_id
            port = ports.get(port_id)
            if port is not None and port.direction is ProtocolPortDirection.INPUT:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location=f"steps[{index}].output_port_ids",
                        subject_id=step.step_id,
                        detail="step cannot produce a protocol input port",
                    )
                )
            artifact = expected_artifacts.get(port_id)
            if port is not None and (
                artifact is None
                or artifact.schema_sha256 != port.schema_ref.schema_sha256
                or artifact.data_classification != port.data_classification.value
                or not artifact.required
                or artifact.role is not ArtifactRole.RAW_OUTPUT
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location=f"steps[{index}].expected_artifacts",
                        subject_id=port_id,
                        detail=(
                            "each output port requires one mandatory raw-output expectation with "
                            "the same key, schema, and classification"
                        ),
                    )
                )
        for port_id in step.input_port_ids:
            port = ports.get(port_id)
            if port is not None and port.direction is ProtocolPortDirection.OUTPUT:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location=f"steps[{index}].input_port_ids",
                        subject_id=step.step_id,
                        detail="final output port cannot be consumed as an input",
                    )
                )

    for port in protocol.data_ports:
        if (
            port.direction
            in {
                ProtocolPortDirection.INTERMEDIATE,
                ProtocolPortDirection.OUTPUT,
            }
            and port.port_id not in producers
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_UNBOUND,
                    location="data_ports",
                    subject_id=port.port_id,
                    detail="non-input protocol port has no producer",
                )
            )
    for step in protocol.steps:
        for port_id in step.input_port_ids:
            producer = producers.get(port_id)
            if producer is not None and producer not in step.depends_on_step_ids:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location="steps",
                        subject_id=step.step_id,
                        detail="consumer does not depend on the step producing its input port",
                    )
                )
            if (
                producer is not None
                and steps[producer].scientific_replicate_count != step.scientific_replicate_count
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location="steps",
                        subject_id=step.step_id,
                        detail=(
                            "v1 intermediate data flow requires one-to-one producer and consumer "
                            "replicate slots"
                        ),
                    )
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return False
        if step_id in visited:
            return True
        visiting.add(step_id)
        for dependency in steps[step_id].depends_on_step_ids:
            if dependency in steps and not visit(dependency):
                return False
        visiting.remove(step_id)
        visited.add(step_id)
        return True

    if any(not visit(step_id) for step_id in steps if step_id not in visited):
        blockers.append(
            _blocker(
                ProtocolBlockerCode.DAG_CYCLE,
                location="steps",
                subject_id=protocol.protocol_id,
                detail="protocol step dependency graph contains a cycle",
            )
        )

    if protocol.claim_contract.ceiling.independent_validation_required:
        required_validator_contracts = {
            ProtocolContractKind.CONTROL,
            ProtocolContractKind.ANALYSIS,
        }

        consumers: dict[str, set[str]] = {}
        for candidate_step in protocol.steps:
            for port_id in candidate_step.input_port_ids:
                consumers.setdefault(port_id, set()).add(candidate_step.step_id)

        def receives_data_from_port(step_id: str, source_port_id: str) -> bool:
            pending = list(consumers.get(source_port_id, ()))
            seen: set[str] = set()
            while pending:
                candidate = pending.pop()
                if candidate == step_id:
                    return True
                if candidate in seen or candidate not in steps:
                    continue
                seen.add(candidate)
                pending.extend(
                    consumer
                    for port_id in steps[candidate].output_port_ids
                    for consumer in consumers.get(port_id, ())
                )
            return False

        for observable_binding in protocol.observable_output_bindings:
            validators = tuple(
                step
                for step in protocol.steps
                if step.role is ProtocolStepRole.INDEPENDENT_VALIDATOR
                and receives_data_from_port(
                    step.step_id,
                    observable_binding.output_port_id,
                )
                and required_validator_contracts.issubset(
                    {item.contract_kind for item in step.contract_bindings}
                )
            )
            if not validators:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.INDEPENDENCE_CONFLICT,
                        location="observable_output_bindings",
                        subject_id=observable_binding.producer_step_id,
                        detail=(
                            "every claim-supporting observable must flow to an independent "
                            "validator that implements the frozen control and analysis contracts"
                        ),
                    )
                )
    return blockers


def _check_capability_and_resource(
    request: ProtocolCheckRequest,
) -> tuple[list[ProtocolBlocker], tuple[CapabilityManifestV2 | None, ...]]:
    protocol = request.protocol
    blockers: list[ProtocolBlocker] = []
    resolved: list[CapabilityManifestV2 | None] = []
    protocol_ports = {item.port_id: item for item in protocol.data_ports}
    claim = protocol.claim_contract

    for index, step in enumerate(protocol.steps):
        manifest, resolution_blockers = _resolve_requirement(
            protocol, index, request.capability_catalog
        )
        resolved.append(manifest)
        blockers.extend(resolution_blockers)
        resource_matches = tuple(
            item
            for item in request.resource_catalog.resource_classes
            if _resource_class_matches(item, step.resource_request)
        )
        if not resource_matches:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE,
                    location=f"steps[{index}].resource_request",
                    subject_id=step.step_id,
                    detail="no static resource class satisfies the frozen structural request",
                    evidence=(
                        canonical_sha256(item)
                        for item in step.resource_request.accepted_resource_class_ids
                    ),
                )
            )
        egress_mode = manifest.license_egress.network_egress if manifest is not None else None
        expected_network = {
            NetworkEgressMode.NONE: NetworkPolicy.NONE,
            NetworkEgressMode.ALLOWLISTED: NetworkPolicy.ALLOWLIST,
            NetworkEgressMode.SITE_MANAGED: NetworkPolicy.AUTHENTICATED_EXTERNAL,
        }.get(egress_mode)
        if manifest is not None and (
            step.resource_request.network_policy is not expected_network
            or (
                egress_mode is NetworkEgressMode.ALLOWLISTED
                and step.resource_request.egress_allowlist_sha256
                != canonical_sha256(manifest.license_egress.allowlisted_destination_ids)
            )
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_EGRESS_INCOMPATIBLE,
                    location=f"steps[{index}].resource_request",
                    subject_id=step.step_id,
                    detail="resource network policy does not implement capability egress policy",
                )
            )
        if manifest is None:
            continue
        if step.environment_sha256 != manifest.runtime.environment_sha256:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE,
                    location=f"steps[{index}].environment_sha256",
                    subject_id=step.step_id,
                    detail="step environment must equal the selected capability runtime environment",
                    evidence=(step.environment_sha256, manifest.runtime.environment_sha256),
                )
            )
        manifest_authorities = {
            manifest.frozen_by_principal_id,
            manifest.principal.executor_principal_id,
            manifest.qualification.qualified_by_principal_id,
        }
        for receipt in protocol.compatibility_audit_receipts:
            if receipt.audited_by_principal_id in manifest_authorities:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE,
                        location="compatibility_audit_receipts",
                        subject_id=step.step_id,
                        detail=(
                            "compatibility auditor must be separate from every selected "
                            "capability author, executor, and qualifier"
                        ),
                        evidence=(receipt.receipt_sha256, manifest.manifest_sha256),
                    )
                )
        required_inputs = {
            item.port_id
            for item in manifest.input_ports
            if item.multiplicity is not PortMultiplicity.OPTIONAL
        }
        required_outputs = {
            item.port_id
            for item in manifest.output_ports
            if item.multiplicity is not PortMultiplicity.OPTIONAL
        }
        if not required_inputs.issubset(step.input_port_ids) or not required_outputs.issubset(
            step.output_port_ids
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_UNBOUND,
                    location=f"steps[{index}]",
                    subject_id=step.step_id,
                    detail="step omits a non-optional selected capability input or output port",
                )
            )
        external_runtime = manifest.runtime.runtime_kind in {
            RuntimeKind.EXTERNAL_SERVICE,
            RuntimeKind.PHYSICAL_SITE,
            RuntimeKind.HUMAN_PROCEDURE,
        }
        resource_runtime_mismatch = (
            external_runtime
            and any(
                item.kind is not ResourceKind.EXTERNAL
                or manifest.external_action_kind not in item.external_action_kinds
                for item in resource_matches
            )
        ) or (
            not external_runtime
            and any(item.kind is ResourceKind.EXTERNAL for item in resource_matches)
        )
        if resource_runtime_mismatch:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE,
                    location=f"steps[{index}].resource_request",
                    subject_id=step.step_id,
                    detail=(
                        "external/physical runtime requires a static external resource that "
                        "supports its exact action kind"
                    ),
                )
            )
        external_effect = manifest.execution_effect_class is not ExecutionEffectClass.REPLAY_SAFE
        provider_receipts = tuple(
            item for item in step.expected_artifacts if item.role is ArtifactRole.PROVIDER_RECEIPT
        )
        provider_contract_valid = len(provider_receipts) == 1 and provider_receipts[0].required
        if external_effect != provider_contract_valid:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_UNBOUND,
                    location=f"steps[{index}].expected_artifacts",
                    subject_id=step.step_id,
                    detail=(
                        "external mutations require exactly one provider receipt; replay-safe "
                        "steps require none"
                    ),
                )
            )
        role_principals = {
            ProtocolStepRole.SCIENTIFIC_EXECUTOR: protocol.independence.executor_principal_ids,
            ProtocolStepRole.OBSERVATION_PARSER: protocol.independence.parser_principal_ids,
            ProtocolStepRole.INDEPENDENT_VALIDATOR: protocol.independence.validator_principal_ids,
            ProtocolStepRole.CONTROL: protocol.independence.executor_principal_ids,
            ProtocolStepRole.ANALYSIS: protocol.independence.executor_principal_ids,
            ProtocolStepRole.CALIBRATION: protocol.independence.executor_principal_ids,
        }
        role_group = {
            ProtocolStepRole.SCIENTIFIC_EXECUTOR: protocol.independence.executor_group_id,
            ProtocolStepRole.OBSERVATION_PARSER: protocol.independence.parser_group_id,
            ProtocolStepRole.INDEPENDENT_VALIDATOR: protocol.independence.validator_group_id,
            ProtocolStepRole.CONTROL: protocol.independence.executor_group_id,
            ProtocolStepRole.ANALYSIS: protocol.independence.executor_group_id,
            ProtocolStepRole.CALIBRATION: protocol.independence.executor_group_id,
        }[step.role]
        other_groups = {
            protocol.independence.executor_group_id,
            protocol.independence.parser_group_id,
            protocol.independence.validator_group_id,
            protocol.independence.claim_approver_group_id,
        } - {role_group}
        if manifest.principal.executor_principal_id not in role_principals[
            step.role
        ] or not other_groups.issubset(manifest.principal.required_independence_groups):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.INDEPENDENCE_CONFLICT,
                    location=f"steps[{index}].capability_requirement",
                    subject_id=step.step_id,
                    detail=(
                        "selected capability principal/independence groups do not match the "
                        "executor/parser/validator separation contract"
                    ),
                    evidence=(manifest.manifest_sha256,),
                )
            )
        requirement = step.capability_requirement
        qualification = manifest.qualification.status
        if (
            requirement.minimum_qualification_status is not QualificationStatus.QUALIFIED
            or qualification is not QualificationStatus.QUALIFIED
            or manifest.qualification.qualified_at is None
            or manifest.qualification.qualified_at > protocol.authored_at
            or (
                manifest.qualification.expires_at is not None
                and manifest.qualification.expires_at <= protocol.authored_at
            )
            or manifest.frozen_at > protocol.authored_at
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CAPABILITY_NOT_QUALIFIED,
                    location=f"steps[{index}].capability_requirement",
                    subject_id=requirement.requirement_id,
                    detail="selected capability does not satisfy the minimum qualification status",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        if (
            protocol.epistemic_contract.kind
            not in {item.value for item in manifest.applicability.epistemic_kinds}
            or not set(manifest.applicability.required_condition_sha256s).issubset(
                requirement.required_condition_sha256s
            )
            or set(manifest.applicability.excluded_condition_sha256s)
            & set(requirement.required_condition_sha256s)
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CAPABILITY_APPLICABILITY_MISMATCH,
                    location=f"steps[{index}].capability_requirement",
                    subject_id=requirement.requirement_id,
                    detail="capability applicability contract does not cover this protocol",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        audit_by_kind = {item.audit_kind: item for item in requirement.audit_bindings}
        required_audit_kinds = set(_REQUIRED_CAPABILITY_AUDITS)
        if manifest.calibration.mode is not CalibrationMode.NOT_APPLICABLE:
            required_audit_kinds.add(CapabilityAuditKind.CALIBRATION)
        authority_principals = {
            protocol.authored_by_principal_id,
            manifest.frozen_by_principal_id,
            manifest.principal.executor_principal_id,
            *protocol.independence.executor_principal_ids,
            *protocol.independence.parser_principal_ids,
            *protocol.independence.validator_principal_ids,
            *protocol.independence.claim_approver_principal_ids,
        }
        if manifest.qualification.qualified_by_principal_id is not None:
            authority_principals.add(manifest.qualification.qualified_by_principal_id)
        audit_bindings_valid = required_audit_kinds.issubset(audit_by_kind) and all(
            item.capability_manifest_sha256 == manifest.manifest_sha256
            and item.receipt_sha256 in manifest.qualification.evidence_receipt_sha256s
            and item.audit_policy_sha256
            == expected_capability_audit_policy_sha256(manifest, item.audit_kind)
            and manifest.qualification.qualified_at is not None
            and item.valid_from
            <= manifest.qualification.qualified_at
            <= manifest.frozen_at
            <= protocol.authored_at
            and (item.expires_at is None or item.expires_at > protocol.authored_at)
            and item.auditor_principal_id not in authority_principals
            for item in requirement.audit_bindings
        )
        if not audit_bindings_valid:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CAPABILITY_AUDIT_MISSING,
                    location=f"steps[{index}].capability_requirement",
                    subject_id=requirement.requirement_id,
                    detail=(
                        "typed applicability/failure/sample/runtime/safety/license/calibration "
                        "audits are incomplete, stale, or bound to another manifest"
                    ),
                    evidence=(item.receipt_sha256 for item in requirement.audit_bindings),
                )
            )
        if not (
            manifest.applicability.minimum_batch_size
            <= step.operation_batch_size
            <= (manifest.applicability.maximum_batch_size or step.operation_batch_size)
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CAPABILITY_APPLICABILITY_MISMATCH,
                    location=f"steps[{index}].operation_batch_size",
                    subject_id=step.step_id,
                    detail="per-operation batch size violates capability sample bounds",
                )
            )
        if any(
            item.retention_policy_sha256 != manifest.license_egress.retention_policy_sha256
            for item in step.expected_artifacts
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.PORT_LICENSE_INCOMPATIBLE,
                    location=f"steps[{index}].expected_artifacts",
                    subject_id=step.step_id,
                    detail="artifact retention must equal the selected capability policy",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        if (
            step.resource_request.wall_time_seconds > manifest.runtime.maximum_wall_time_seconds
            or step.resource_request.max_infrastructure_attempts
            > manifest.retry.maximum_attempts_per_scientific_slot
            or (
                step.resource_request.checkpoint_interval_seconds is not None
                and manifest.retry.mode is not RetryMode.CHECKPOINT_RESUME
            )
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.RETRY_POLICY_INCOMPATIBLE,
                    location=f"steps[{index}].resource_request",
                    subject_id=step.step_id,
                    detail="resource/retry/checkpoint envelope exceeds the capability contract",
                )
            )

        maximum = manifest.claim_ceiling.maximum_strength_for(claim.requested_kind)
        if maximum is None or _STRENGTH_RANK[claim.requested_strength] > _STRENGTH_RANK[maximum]:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.UNSUPPORTED_CLAIM,
                    location="claim_contract",
                    subject_id=claim.claim_contract_id,
                    detail="selected capability cannot support the requested claim ceiling",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        protocol_ceiling = claim.ceiling
        manifest_ceiling = manifest.claim_ceiling
        if (
            not set(manifest_ceiling.required_evidence_modalities).issubset(
                protocol_ceiling.required_evidence_modalities
            )
            or _REPLICATION_RANK[protocol_ceiling.required_replication_tier]
            < _REPLICATION_RANK[manifest_ceiling.required_replication_tier]
            or (
                manifest_ceiling.independent_validation_required
                and not protocol_ceiling.independent_validation_required
            )
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.UNSUPPORTED_CLAIM,
                    location="claim_contract.ceiling",
                    subject_id=step.step_id,
                    detail=(
                        "protocol evidence, replication, and validation requirements cannot be "
                        "weaker than a selected capability claim ceiling"
                    ),
                    evidence=(manifest.manifest_sha256,),
                )
            )

        capability_inputs = {item.port_id: item for item in manifest.input_ports}
        capability_outputs = {item.port_id: item for item in manifest.output_ports}
        for port_id, direction in (
            *((item, PortDirection.INPUT) for item in step.input_port_ids),
            *((item, PortDirection.OUTPUT) for item in step.output_port_ids),
        ):
            protocol_port = protocol_ports.get(port_id)
            capability_port = (
                capability_inputs.get(port_id)
                if direction is PortDirection.INPUT
                else capability_outputs.get(port_id)
            )
            if protocol_port is None or capability_port is None:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_UNBOUND,
                        location=f"steps[{index}]",
                        subject_id=step.step_id,
                        detail="step port is absent from the selected capability manifest",
                    )
                )
                continue
            if capability_port.artifact_kind is not protocol_port.artifact_kind:
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE,
                        location=f"steps[{index}]",
                        subject_id=port_id,
                        detail="capability and protocol ports declare different artifact kinds",
                    )
                )
            if (
                direction is PortDirection.INPUT
                and protocol_port.data_classification
                not in manifest.license_egress.permitted_input_classes
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_CLASSIFICATION_INCOMPATIBLE,
                        location=f"steps[{index}]",
                        subject_id=port_id,
                        detail="capability license policy forbids the input data classification",
                    )
                )
            source_schema, target_schema = (
                (protocol_port.schema_ref.schema_sha256, capability_port.schema_ref.schema_sha256)
                if direction is PortDirection.INPUT
                else (
                    capability_port.schema_ref.schema_sha256,
                    protocol_port.schema_ref.schema_sha256,
                )
            )
            if not _compatibility_exists(
                protocol,
                dimension=CompatibilityDimension.JSON_SCHEMA,
                source_sha256=source_schema,
                target_sha256=target_schema,
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE,
                        location=f"steps[{index}]",
                        subject_id=port_id,
                        detail="port JSON schemas are neither identical nor explicitly audited",
                        evidence=(source_schema, target_schema),
                    )
                )
            capability_unit = canonical_sha256(capability_port.unit_or_ontology_refs)
            if not _compatibility_exists(
                protocol,
                dimension=CompatibilityDimension.UNIT_OR_ONTOLOGY,
                source_sha256=(
                    protocol_port.unit_or_ontology_sha256
                    if direction is PortDirection.INPUT
                    else capability_unit
                ),
                target_sha256=(
                    capability_unit
                    if direction is PortDirection.INPUT
                    else protocol_port.unit_or_ontology_sha256
                ),
            ):
                blockers.append(
                    _blocker(
                        ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE,
                        location=f"steps[{index}]",
                        subject_id=port_id,
                        detail="port unit/ontology contracts are incompatible",
                    )
                )
            if capability_port.data_classification is not protocol_port.data_classification:
                source = canonical_sha256(
                    (
                        protocol_port.data_classification.value
                        if direction is PortDirection.INPUT
                        else capability_port.data_classification.value
                    )
                )
                target = canonical_sha256(
                    (
                        capability_port.data_classification.value
                        if direction is PortDirection.INPUT
                        else protocol_port.data_classification.value
                    )
                )
                if not _compatibility_exists(
                    protocol,
                    dimension=CompatibilityDimension.DATA_CLASSIFICATION,
                    source_sha256=source,
                    target_sha256=target,
                ):
                    blockers.append(
                        _blocker(
                            ProtocolBlockerCode.PORT_CLASSIFICATION_INCOMPATIBLE,
                            location=f"steps[{index}]",
                            subject_id=port_id,
                            detail="port data classifications are incompatible",
                        )
                    )
            for dimension, code, protocol_hash, capability_hash in (
                (
                    CompatibilityDimension.LICENSE,
                    ProtocolBlockerCode.PORT_LICENSE_INCOMPATIBLE,
                    protocol_port.license_policy_sha256,
                    manifest.license_egress.license_policy_sha256,
                ),
                (
                    CompatibilityDimension.EGRESS,
                    ProtocolBlockerCode.PORT_EGRESS_INCOMPATIBLE,
                    protocol_port.egress_policy_sha256,
                    manifest.license_egress.egress_policy_sha256,
                ),
            ):
                source, target = (
                    (protocol_hash, capability_hash)
                    if direction is PortDirection.INPUT
                    else (capability_hash, protocol_hash)
                )
                if not _compatibility_exists(
                    protocol,
                    dimension=dimension,
                    source_sha256=source,
                    target_sha256=target,
                ):
                    blockers.append(
                        _blocker(
                            code,
                            location=f"steps[{index}]",
                            subject_id=port_id,
                            detail=f"port {dimension.value} contracts are incompatible",
                        )
                    )
    return blockers, tuple(resolved)


def _check_observable_capabilities(
    request: ProtocolCheckRequest,
) -> list[ProtocolBlocker]:
    blockers: list[ProtocolBlocker] = []
    protocol = request.protocol
    bindings = {item.observable_spec_sha256: item for item in protocol.observable_output_bindings}
    observable_hashes = {item.observable_sha256 for item in protocol.observables}
    if set(bindings) != observable_hashes:
        blockers.append(
            _blocker(
                ProtocolBlockerCode.OBSERVABLE_MISSING,
                location="observable_output_bindings",
                subject_id=protocol.protocol_id,
                detail="every observable requires exactly one producer-step/output-port binding",
            )
        )
    steps = {item.step_id: (index, item) for index, item in enumerate(protocol.steps)}
    ports = {item.port_id: item for item in protocol.data_ports}
    for observable in request.protocol.observables:
        try:
            manifest = request.capability_catalog.get_exact(
                observable.measurement_capability_manifest_sha256
            )
        except LookupError:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.OBSERVABLE_CAPABILITY_MISMATCH,
                    location="observables",
                    subject_id=observable.observable_id,
                    detail="observable measurement capability is absent from the frozen catalog",
                    evidence=(observable.measurement_capability_manifest_sha256,),
                )
            )
            continue
        binding = bindings.get(observable.observable_sha256)
        step_entry = steps.get(binding.producer_step_id) if binding is not None else None
        selected_manifest: CapabilityManifestV2 | None = None
        if step_entry is not None:
            step_index, step = step_entry
            selected_manifest, _ = _resolve_requirement(
                protocol,
                step_index,
                request.capability_catalog,
            )
            port = ports.get(binding.output_port_id) if binding is not None else None
            if (
                binding.output_port_id not in step.output_port_ids
                or port is None
                or port.direction is ProtocolPortDirection.INPUT
                or port.identity_schema_sha256 != observable.entity_identity_schema_sha256
                or port.schema_ref.schema_sha256 != observable.output_schema_sha256
                or port.unit_or_ontology_sha256 != observable.unit_or_ontology_sha256
            ):
                selected_manifest = None
        if (
            selected_manifest is None
            or selected_manifest.manifest_sha256 != manifest.manifest_sha256
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.OBSERVABLE_CAPABILITY_MISMATCH,
                    location="observable_output_bindings",
                    subject_id=observable.observable_id,
                    detail=(
                        "observable must bind an output of a step that selects its exact "
                        "measurement capability, schema, unit/ontology, and entity identity"
                    ),
                    evidence=(observable.measurement_capability_manifest_sha256,),
                )
            )
        if manifest.qualification.status is not QualificationStatus.QUALIFIED:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.OBSERVABLE_CAPABILITY_MISMATCH,
                    location="observables",
                    subject_id=observable.observable_id,
                    detail="observable measurement capability is not qualified",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        elif (
            manifest.qualification.qualified_at is None
            or manifest.qualification.qualified_at > request.protocol.authored_at
            or (
                manifest.qualification.expires_at is not None
                and manifest.qualification.expires_at <= request.protocol.authored_at
            )
            or manifest.frozen_at > request.protocol.authored_at
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CALIBRATION_UNCOVERED,
                    location="observables",
                    subject_id=observable.observable_id,
                    detail="measurement qualification is not valid at protocol freeze time",
                    evidence=(manifest.manifest_sha256,),
                )
            )
        expected_calibration = canonical_sha256(manifest.calibration)
        if observable.calibration_contract_sha256 != expected_calibration:
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CALIBRATION_UNCOVERED,
                    location="observables",
                    subject_id=observable.observable_id,
                    detail="observable does not bind the capability calibration contract",
                    evidence=(observable.calibration_contract_sha256, expected_calibration),
                )
            )
        if (
            manifest.calibration.mode is not CalibrationMode.NOT_APPLICABLE
            and manifest.calibration.calibration_receipt_schema is None
        ):
            blockers.append(
                _blocker(
                    ProtocolBlockerCode.CALIBRATION_UNCOVERED,
                    location="observables",
                    subject_id=observable.observable_id,
                    detail="calibrated measurement capability lacks a receipt schema",
                )
            )
    return blockers


def _check_replication(protocol: ProtocolIR) -> list[ProtocolBlocker]:
    tier = protocol.claim_contract.ceiling.required_replication_tier
    if tier is ReplicationTier.NONE:
        return []
    if tier in {
        ReplicationTier.INDEPENDENT_IMPLEMENTATION,
        ReplicationTier.EXTERNAL_INDEPENDENT,
    }:
        return [
            _blocker(
                ProtocolBlockerCode.UNSUPPORTED_CLAIM,
                location="claim_contract.ceiling.required_replication_tier",
                subject_id=protocol.claim_contract.claim_contract_id,
                detail=(
                    "v1 cannot prove implementation/principal/site independence from a slot "
                    "count; an explicit future replication assignment contract is required"
                ),
            )
        ]
    # Without an explicit claim-to-step assignment contract, every scientific executor branch
    # that may support the claim must preregister the exact-reexecution slots. Parser, control,
    # and validator multiplicity cannot masquerade as scientific replication.
    scientific_steps = tuple(
        item for item in protocol.steps if item.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR
    )
    if scientific_steps and all(item.scientific_replicate_count >= 2 for item in scientific_steps):
        return []
    return [
        _blocker(
            ProtocolBlockerCode.UNSUPPORTED_CLAIM,
            location="claim_contract.ceiling.required_replication_tier",
            subject_id=protocol.claim_contract.claim_contract_id,
            detail="protocol does not preregister enough scientific replicate slots",
        )
    ]


def typecheck_protocol(request: ProtocolCheckRequest) -> ProtocolCheckReport:
    """Return every deterministic structural blocker; never inspect live resource availability."""

    # Revalidate caller-created/model_construct values before relying on their invariants.
    request = ProtocolCheckRequest.model_validate(request.model_dump(mode="python"))
    blockers: list[ProtocolBlocker] = []
    blockers.extend(_check_scope_and_epistemics(request.protocol))
    blockers.extend(_check_data_controls_and_lineage(request.protocol))
    blockers.extend(_check_dag(request.protocol))
    capability_blockers, _ = _check_capability_and_resource(request)
    blockers.extend(capability_blockers)
    blockers.extend(_check_observable_capabilities(request))
    blockers.extend(_check_replication(request.protocol))
    canonical = tuple(
        sorted(
            {item.blocker_sha256: item for item in blockers}.values(),
            key=lambda item: (item.code.value, item.location, item.subject_id, item.blocker_sha256),
        )
    )
    return ProtocolCheckReport(
        protocol_sha256=request.protocol.protocol_sha256,
        capability_catalog_sha256=request.capability_catalog.catalog_sha256,
        resource_catalog_sha256=request.resource_catalog.catalog_sha256,
        blockers=canonical,
    )


__all__ = [
    "ProtocolCheckRequest",
    "expected_capability_audit_policy_sha256",
    "expected_compatibility_audit_policy_sha256",
    "resolve_protocol_capabilities",
    "typecheck_protocol",
]
