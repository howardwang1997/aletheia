"""Deterministic Scientific Protocol IR compiler.

Compilation is a pure projection over frozen protocol/capability/resource values.  It emits a
WorkOrder DAG or exact typed blockers; it never checks live capacity, allocates resources, executes
work, admits observations, or mutates research authority.
"""

from __future__ import annotations

from pydantic import Field

from aletheia.execution.schemas import (
    CapabilityFailureCategory,
    ExecutionIntent,
    ExecutionRetryDisposition,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    ExecutionRetryRule,
    ScientificReplicateSlot,
    StaticResourceCatalog,
)
from aletheia.protocols.base import (
    SHA256_PATTERN,
    ProtocolModel,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.protocols.capabilities import (
    CapabilityCatalog,
    CapabilityManifestV2,
)
from aletheia.protocols.schemas import (
    CompilationReceipt,
    ProtocolCompilationResult,
    ProtocolIR,
    ProtocolStep,
    WorkOrderDAG,
    WorkOrderNode,
)
from aletheia.protocols.typecheck import (
    ProtocolCheckRequest,
    resolve_protocol_capabilities,
    typecheck_protocol,
)


class ProtocolCompilationRequest(ProtocolModel):
    protocol: ProtocolIR
    capability_catalog: CapabilityCatalog
    resource_catalog: StaticResourceCatalog
    compiler_implementation_sha256: str = Field(pattern=SHA256_PATTERN)


class CompilationVerificationError(ValueError):
    """A supplied compilation is not the canonical result for its frozen inputs."""


class ExecutionIntentBindingError(ValueError):
    """An execution intent is not the exact lowering of its WorkOrder node."""


def _node_id(*, protocol_sha256: str, step_id: str, manifest_sha256: str) -> str:
    digest = canonical_sha256(
        {
            "schema_name": "aletheia.work_order_node_identity",
            "schema_version": 1,
            "protocol_sha256": protocol_sha256,
            "step_id": step_id,
            "capability_manifest_sha256": manifest_sha256,
        }
    )
    return f"node.{digest[:32]}"


def _execution_command_sha256(*, step: ProtocolStep, manifest: CapabilityManifestV2) -> str:
    """Derive the exact logical command specification without materializing shell code."""

    return canonical_sha256(
        {
            "schema_name": "aletheia.execution_command_spec",
            "schema_version": 1,
            "operation_id": manifest.operation_id,
            "adapter_ref": manifest.runtime.adapter_ref,
            "implementation_sha256": manifest.runtime.implementation_sha256,
            "environment_sha256": manifest.runtime.environment_sha256,
            "capability_manifest_sha256": manifest.manifest_sha256,
            "execution_parameters_sha256": step.execution_parameters_sha256,
            "input_port_ids": step.input_port_ids,
            "output_port_ids": step.output_port_ids,
        }
    )


def _execution_retry_policy(manifest: CapabilityManifestV2) -> ExecutionRetryPolicy:
    retryable_ids = set(manifest.retry.retryable_failure_ids)
    rules = tuple(
        sorted(
            (
                ExecutionRetryRule(
                    capability_failure_id=item.failure_id,
                    capability_failure_category=CapabilityFailureCategory(item.category.value),
                    detection_rule_sha256=item.detection_rule_sha256,
                    disposition=ExecutionRetryDisposition(item.disposition.value),
                )
                for item in manifest.failure_modes
                if item.failure_id in retryable_ids
            ),
            key=lambda item: item.capability_failure_id,
        )
    )
    return ExecutionRetryPolicy(
        mode=ExecutionRetryMode(manifest.retry.mode.value),
        maximum_attempts_per_scientific_slot=(manifest.retry.maximum_attempts_per_scientific_slot),
        retry_rules=rules,
        idempotency_rule_sha256=manifest.retry.idempotency_rule_sha256,
        reconciliation_rule_sha256=manifest.retry.reconciliation_rule_sha256,
        checkpoint_schema_sha256=(
            canonical_sha256(manifest.retry.checkpoint_schema)
            if manifest.retry.checkpoint_schema is not None
            else None
        ),
    )


def _build_work_order(
    request: ProtocolCompilationRequest,
    manifests: tuple[CapabilityManifestV2, ...],
) -> WorkOrderDAG:
    protocol = request.protocol
    parameter_bindings = {item.parameter_id: item for item in protocol.caller_parameter_bindings}
    observable_bindings_by_step = {
        step.step_id: tuple(
            item
            for item in protocol.observable_output_bindings
            if item.producer_step_id == step.step_id
        )
        for step in protocol.steps
    }
    node_ids = {
        step.step_id: _node_id(
            protocol_sha256=protocol.protocol_sha256,
            step_id=step.step_id,
            manifest_sha256=manifest.manifest_sha256,
        )
        for step, manifest in zip(protocol.steps, manifests, strict=True)
    }
    nodes = tuple(
        sorted(
            (
                WorkOrderNode(
                    node_id=node_ids[step.step_id],
                    protocol_step_id=step.step_id,
                    role=step.role,
                    capability_id=manifest.capability_id,
                    capability_manifest_sha256=manifest.manifest_sha256,
                    command_sha256=_execution_command_sha256(step=step, manifest=manifest),
                    external_action_kind=manifest.external_action_kind,
                    effect_class=manifest.execution_effect_class,
                    dependency_node_ids=tuple(
                        sorted(node_ids[item] for item in step.depends_on_step_ids)
                    ),
                    input_port_ids=step.input_port_ids,
                    output_port_ids=step.output_port_ids,
                    resource_request=step.resource_request,
                    retry_policy=_execution_retry_policy(manifest),
                    expected_artifacts=step.expected_artifacts,
                    contract_bindings=step.contract_bindings,
                    observable_output_bindings=observable_bindings_by_step[step.step_id],
                    caller_parameter_bindings=tuple(
                        parameter_bindings[item] for item in step.caller_parameter_ids
                    ),
                    operation_batch_size=step.operation_batch_size,
                    replicate_kind=step.replicate_kind,
                    replicate_preregistration_sha256=step.replicate_preregistration_sha256,
                    replicate_seed_sha256s=step.replicate_seed_sha256s,
                    independent_site_required=step.independent_site_required,
                    scientific_replicate_count=step.scientific_replicate_count,
                    execution_parameters_sha256=step.execution_parameters_sha256,
                    environment_sha256=step.environment_sha256,
                )
                for step, manifest in zip(protocol.steps, manifests, strict=True)
            ),
            key=lambda item: item.node_id,
        )
    )
    return WorkOrderDAG(
        quest_id=protocol.graph_scope.scope_binding.quest_id,
        graph_scope_sha256=protocol.graph_scope.graph_scope_sha256,
        protocol_sha256=protocol.protocol_sha256,
        capability_catalog_sha256=request.capability_catalog.catalog_sha256,
        resource_catalog_sha256=request.resource_catalog.catalog_sha256,
        resource_budget_sha256=protocol.resource_budget.resource_budget_sha256,
        nodes=nodes,
    )


def compile_protocol(request: ProtocolCompilationRequest) -> ProtocolCompilationResult:
    """Compile one frozen protocol to a canonical DAG or deterministic blocker receipt."""

    request = ProtocolCompilationRequest.model_validate(request.model_dump(mode="python"))
    check_request = ProtocolCheckRequest(
        protocol=request.protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
    )
    report = typecheck_protocol(check_request)
    work_order: WorkOrderDAG | None = None
    if report.accepted:
        resolved = resolve_protocol_capabilities(check_request)
        if any(item is None for item in resolved):
            raise RuntimeError("accepted typecheck did not resolve every capability")
        work_order = _build_work_order(
            request,
            tuple(item for item in resolved if item is not None),
        )
    receipt = CompilationReceipt(
        protocol_sha256=request.protocol.protocol_sha256,
        typecheck_report_sha256=report.report_sha256,
        compiler_implementation_sha256=request.compiler_implementation_sha256,
        capability_catalog_sha256=request.capability_catalog.catalog_sha256,
        resource_catalog_sha256=request.resource_catalog.catalog_sha256,
        work_order_sha256=work_order.work_order_sha256 if work_order else None,
        blocker_sha256s=tuple(sorted(item.blocker_sha256 for item in report.blockers)),
    )
    return ProtocolCompilationResult(report=report, work_order=work_order, receipt=receipt)


def verify_compilation(
    request: ProtocolCompilationRequest,
    result: ProtocolCompilationResult,
) -> None:
    """Recompute and byte-compare a compilation, including all transitive identities."""

    try:
        validated_request = ProtocolCompilationRequest.model_validate(
            request.model_dump(mode="python")
        )
        validated_result = ProtocolCompilationResult.model_validate(
            result.model_dump(mode="python")
        )
    except (TypeError, ValueError) as exc:
        raise CompilationVerificationError("compilation contracts failed revalidation") from exc
    expected = compile_protocol(validated_request)
    if canonical_json_bytes(validated_result) != canonical_json_bytes(expected):
        raise CompilationVerificationError(
            "compilation differs from the canonical result for its frozen inputs"
        )


def verify_execution_intent_binding(
    work_order: WorkOrderDAG,
    intent: ExecutionIntent,
) -> None:
    """Fail closed unless an intent preserves its exact node and replicate-slot contract."""

    try:
        validated_work_order = WorkOrderDAG.model_validate(
            work_order.model_dump(mode="python", warnings="none")
        )
        validated_intent = ExecutionIntent.model_validate(
            intent.model_dump(mode="python", warnings="none")
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionIntentBindingError(
            "execution intent or WorkOrder failed closed-model revalidation"
        ) from exc

    work_order = validated_work_order
    intent = validated_intent
    nodes = tuple(item for item in work_order.nodes if item.node_id == intent.work_order_node_id)
    if len(nodes) != 1:
        raise ExecutionIntentBindingError("execution intent node does not resolve exactly once")
    node = nodes[0]
    slot = intent.replicate_slot
    slot_index = slot.slot_index - 1
    seed_matches = 0 <= slot_index < len(node.replicate_seed_sha256s) and (
        slot.randomization_seed_sha256 == node.replicate_seed_sha256s[slot_index]
    )
    checks = (
        intent.quest_id == work_order.quest_id,
        intent.protocol_sha256 == work_order.protocol_sha256,
        intent.work_order_id == work_order.work_order_id,
        intent.work_order_sha256 == work_order.work_order_sha256,
        intent.work_order_node_sha256 == node.node_sha256,
        intent.capability_id == node.capability_id,
        intent.capability_manifest_sha256 == node.capability_manifest_sha256,
        intent.command_sha256 == node.command_sha256,
        intent.external_action_kind == node.external_action_kind,
        intent.resource_catalog_sha256 == work_order.resource_catalog_sha256,
        intent.resource_request == node.resource_request,
        intent.retry_policy == node.retry_policy,
        intent.expected_artifacts == node.expected_artifacts,
        intent.environment_sha256 == node.environment_sha256,
        intent.execution_parameters_sha256 == node.execution_parameters_sha256,
        intent.effect_class == node.effect_class,
        slot.quest_id == work_order.quest_id,
        slot.protocol_sha256 == work_order.protocol_sha256,
        slot.work_order_id == work_order.work_order_id,
        slot.work_order_node_id == node.node_id,
        slot.work_order_node_sha256 == node.node_sha256,
        slot.slot_count == node.scientific_replicate_count,
        slot.replicate_kind == node.replicate_kind,
        slot.preregistration_sha256 == node.replicate_preregistration_sha256,
        seed_matches,
        slot.independent_site_required == node.independent_site_required,
    )
    if not all(checks):
        raise ExecutionIntentBindingError(
            "execution intent changed a frozen WorkOrder node or replicate-slot field"
        )

    bindings = {item.input_port_id: item for item in intent.input_artifact_bindings}
    if set(bindings) != set(node.input_port_ids):
        raise ExecutionIntentBindingError(
            "execution intent must bind exactly one verified receipt to every node input port"
        )
    for input_port_id, binding in bindings.items():
        producers = tuple(
            item for item in work_order.nodes if input_port_id in item.output_port_ids
        )
        if not producers:
            if binding.source_kind != "protocol_input":
                raise ExecutionIntentBindingError("protocol input claimed a WorkOrder producer")
            continue
        if len(producers) != 1:
            raise ExecutionIntentBindingError("WorkOrder input has ambiguous producers")
        producer = producers[0]
        if (
            binding.source_kind != "work_order_output"
            or binding.source_work_order_node_id != producer.node_id
            or binding.source_work_order_node_sha256 != producer.node_sha256
            or producer.node_id not in node.dependency_node_ids
            or binding.source_slot_index is None
            or binding.source_slot_index != slot.slot_index
        ):
            raise ExecutionIntentBindingError(
                "intermediate input is not bound to its exact dependency producer"
            )
        try:
            source_slot = ScientificReplicateSlot(
                quest_id=work_order.quest_id,
                protocol_sha256=work_order.protocol_sha256,
                work_order_id=work_order.work_order_id,
                work_order_node_id=producer.node_id,
                work_order_node_sha256=producer.node_sha256,
                slot_count=producer.scientific_replicate_count,
                slot_index=binding.source_slot_index,
                replicate_kind=producer.replicate_kind,
                preregistration_sha256=producer.replicate_preregistration_sha256,
                randomization_seed_sha256=producer.replicate_seed_sha256s[
                    binding.source_slot_index - 1
                ],
                independent_site_required=producer.independent_site_required,
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ExecutionIntentBindingError(
                "intermediate input names an invalid producer replicate slot"
            ) from exc
        if binding.source_replicate_slot_id != source_slot.replicate_slot_id:
            raise ExecutionIntentBindingError(
                "intermediate input receipt belongs to another producer replicate slot"
            )


__all__ = [
    "CompilationVerificationError",
    "ExecutionIntentBindingError",
    "ProtocolCompilationRequest",
    "compile_protocol",
    "verify_compilation",
    "verify_execution_intent_binding",
]
