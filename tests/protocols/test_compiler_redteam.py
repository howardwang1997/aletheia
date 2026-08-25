from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from aletheia.execution.schemas import (
    ExecutionEffectClass,
    ExecutionIntent,
    InputArtifactBinding,
    InfrastructureAttempt,
    ScientificReplicateSlot,
)
from aletheia.protocols.compiler import (
    CompilationVerificationError,
    ExecutionIntentBindingError,
    ProtocolCompilationRequest,
    compile_protocol,
    verify_compilation,
    verify_execution_intent_binding,
)
from aletheia.protocols.schemas import (
    ProtocolBlockerCode,
    ProtocolCompilationResult,
    WorkOrderDAG,
)

from fixtures import accepted_protocol_fixtures, digest, fixture_by_name


def test_canonical_compilation_is_deterministic_and_dependency_exact() -> None:
    for fixture in accepted_protocol_fixtures():
        before = fixture.request.model_dump(mode="python")
        first = compile_protocol(fixture.request)
        second = compile_protocol(fixture.request)

        assert first == second
        assert first.work_order is not None
        assert first.report.accepted
        assert fixture.request.model_dump(mode="python") == before
        verify_compilation(fixture.request, first)

        nodes = {item.protocol_step_id: item for item in first.work_order.nodes}
        protocol_steps = {item.step_id: item for item in fixture.request.protocol.steps}
        manifests = {
            item.manifest_sha256: item for item in fixture.request.capability_catalog.manifests
        }
        parameter_bindings = {
            item.parameter_id: item for item in fixture.request.protocol.caller_parameter_bindings
        }
        for step_id, dependency_step_ids in fixture.expected_dependency_steps:
            manifest = manifests[protocol_steps[step_id].capability_requirement.manifest_sha256]
            assert nodes[step_id].role is protocol_steps[step_id].role
            assert nodes[step_id].capability_id == manifest.capability_id
            assert nodes[step_id].capability_manifest_sha256 == manifest.manifest_sha256
            assert nodes[step_id].external_action_kind == manifest.external_action_kind
            assert nodes[step_id].input_port_ids == protocol_steps[step_id].input_port_ids
            assert nodes[step_id].output_port_ids == protocol_steps[step_id].output_port_ids
            assert nodes[step_id].resource_request == protocol_steps[step_id].resource_request
            assert nodes[step_id].expected_artifacts == protocol_steps[step_id].expected_artifacts
            assert nodes[step_id].contract_bindings == protocol_steps[step_id].contract_bindings
            assert nodes[step_id].caller_parameter_bindings == tuple(
                parameter_bindings[item] for item in protocol_steps[step_id].caller_parameter_ids
            )
            assert nodes[step_id].observable_output_bindings == tuple(
                item
                for item in fixture.request.protocol.observable_output_bindings
                if item.producer_step_id == step_id
            )
            assert (
                nodes[step_id].operation_batch_size == protocol_steps[step_id].operation_batch_size
            )
            assert nodes[step_id].replicate_kind is protocol_steps[step_id].replicate_kind
            assert nodes[step_id].replicate_preregistration_sha256 == (
                protocol_steps[step_id].replicate_preregistration_sha256
            )
            assert nodes[step_id].replicate_seed_sha256s == (
                protocol_steps[step_id].replicate_seed_sha256s
            )
            assert nodes[step_id].independent_site_required == (
                protocol_steps[step_id].independent_site_required
            )
            assert nodes[step_id].scientific_replicate_count == (
                protocol_steps[step_id].scientific_replicate_count
            )
            assert nodes[step_id].execution_parameters_sha256 == (
                protocol_steps[step_id].execution_parameters_sha256
            )
            assert nodes[step_id].environment_sha256 == protocol_steps[step_id].environment_sha256
            assert nodes[step_id].dependency_node_ids == tuple(
                sorted(nodes[item].node_id for item in dependency_step_ids)
            )
        assert first.work_order.resource_budget_sha256 == (
            fixture.request.protocol.resource_budget.resource_budget_sha256
        )


def test_physical_action_compiles_as_one_time_not_replay_safe() -> None:
    fixture = fixture_by_name("external_measurement")
    result = compile_protocol(fixture.request)
    assert result.work_order is not None
    nodes = {item.protocol_step_id: item for item in result.work_order.nodes}

    assert nodes["step.01_acquire"].effect_class is ExecutionEffectClass.ONE_TIME_EXTERNAL
    assert nodes["step.02_parse"].effect_class is ExecutionEffectClass.REPLAY_SAFE
    assert nodes["step.03_validate"].effect_class is ExecutionEffectClass.REPLAY_SAFE


def test_blocked_protocol_emits_no_work_order_and_exact_blocker_receipt() -> None:
    fixture = fixture_by_name("grouped_regression")
    payload = fixture.request.model_dump(mode="python")
    payload["protocol"]["analysis_plan"]["frozen_before_observation"] = False
    request = ProtocolCompilationRequest.model_validate(payload)

    result = compile_protocol(request)

    assert result.work_order is None
    assert not result.report.accepted
    assert {item.code for item in result.report.blockers} >= {
        ProtocolBlockerCode.ANALYSIS_NOT_PREREGISTERED
    }
    assert result.receipt.work_order_sha256 is None
    assert result.receipt.blocker_sha256s == tuple(
        sorted(item.blocker_sha256 for item in result.report.blockers)
    )
    verify_compilation(request, result)


def test_verifier_rejects_receipt_hash_tampering() -> None:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    tampered_receipt = result.receipt.model_copy(
        update={"compiler_implementation_sha256": digest("forged-compiler")}
    )
    internally_well_formed_forgery = ProtocolCompilationResult(
        report=result.report,
        work_order=result.work_order,
        receipt=tampered_receipt,
    )

    with pytest.raises(CompilationVerificationError, match="canonical result"):
        verify_compilation(fixture.request, internally_well_formed_forgery)


def test_verifier_rejects_coherently_rehashed_work_order_tampering() -> None:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    assert result.work_order is not None
    original_node = result.work_order.nodes[0]
    tampered_node = original_node.model_copy(
        update={"execution_parameters_sha256": digest("forged-execution-parameters")}
    )
    tampered_nodes = tuple(
        sorted(
            (tampered_node, *result.work_order.nodes[1:]),
            key=lambda item: item.node_id,
        )
    )
    tampered_work_order = result.work_order.model_copy(update={"nodes": tampered_nodes})
    rebound_receipt = result.receipt.model_copy(
        update={"work_order_sha256": tampered_work_order.work_order_sha256}
    )
    internally_well_formed_forgery = ProtocolCompilationResult(
        report=result.report,
        work_order=tampered_work_order,
        receipt=rebound_receipt,
    )

    with pytest.raises(CompilationVerificationError, match="canonical result"):
        verify_compilation(fixture.request, internally_well_formed_forgery)


def test_compiler_revalidates_model_construct_request() -> None:
    request = fixture_by_name("grouped_regression").request
    constructed = ProtocolCompilationRequest.model_construct(
        protocol=request.protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
        compiler_implementation_sha256="not-a-sha256",
    )

    with pytest.raises(ValidationError, match="pattern"):
        compile_protocol(constructed)


def test_verifier_revalidates_model_construct_result() -> None:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    unbound_receipt = result.receipt.model_copy(
        update={"protocol_sha256": digest("another-protocol")}
    )
    constructed = ProtocolCompilationResult.model_construct(
        report=result.report,
        work_order=result.work_order,
        receipt=unbound_receipt,
    )

    with pytest.raises(CompilationVerificationError, match="failed revalidation"):
        verify_compilation(fixture.request, constructed)


def _bound_execution_intent() -> tuple[WorkOrderDAG, ExecutionIntent]:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(
        item for item in work_order.nodes if item.effect_class is ExecutionEffectClass.REPLAY_SAFE
    )
    slot = ScientificReplicateSlot(
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
    attempt = InfrastructureAttempt(
        replicate_slot_id=slot.replicate_slot_id,
        attempt_number=1,
    )
    input_bindings = []
    for input_port_id in node.input_port_ids:
        producers = tuple(
            item for item in work_order.nodes if input_port_id in item.output_port_ids
        )
        if not producers:
            input_bindings.append(
                InputArtifactBinding(
                    input_port_id=input_port_id,
                    source_kind="protocol_input",
                    artifact_verified_receipt_sha256=digest(f"protocol-input:{input_port_id}"),
                )
            )
            continue
        producer = producers[0]
        producer_slot = ScientificReplicateSlot(
            quest_id=work_order.quest_id,
            protocol_sha256=work_order.protocol_sha256,
            work_order_id=work_order.work_order_id,
            work_order_node_id=producer.node_id,
            work_order_node_sha256=producer.node_sha256,
            slot_count=producer.scientific_replicate_count,
            slot_index=1,
            replicate_kind=producer.replicate_kind,
            preregistration_sha256=producer.replicate_preregistration_sha256,
            randomization_seed_sha256=producer.replicate_seed_sha256s[0],
            independent_site_required=producer.independent_site_required,
        )
        input_bindings.append(
            InputArtifactBinding(
                input_port_id=input_port_id,
                source_kind="work_order_output",
                artifact_verified_receipt_sha256=digest(f"intermediate-input:{input_port_id}"),
                source_work_order_node_id=producer.node_id,
                source_work_order_node_sha256=producer.node_sha256,
                source_replicate_slot_id=producer_slot.replicate_slot_id,
                source_slot_index=1,
            )
        )
    intent = ExecutionIntent(
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
        infrastructure_attempt=attempt,
        input_artifact_bindings=tuple(sorted(input_bindings, key=lambda item: item.input_port_id)),
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=fixture.request.protocol.authored_at,
        deadline=fixture.request.protocol.authored_at + timedelta(hours=1),
    )
    verify_execution_intent_binding(work_order, intent)
    return work_order, intent


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("capability_id", "capability.forged"),
        ("capability_manifest_sha256", digest("forged-capability-manifest")),
        ("external_action_kind", "external.forged"),
        ("resource_catalog_sha256", digest("forged-resource-catalog")),
        ("environment_sha256", digest("forged-environment")),
        ("execution_parameters_sha256", digest("forged-execution-parameters")),
    ),
)
def test_execution_intent_cannot_mutate_a_work_order_field(
    field: str,
    forged_value: str,
) -> None:
    work_order, intent = _bound_execution_intent()
    tampered = intent.model_copy(update={field: forged_value})

    with pytest.raises(
        ExecutionIntentBindingError, match="revalidation|changed a frozen WorkOrder"
    ):
        verify_execution_intent_binding(work_order, tampered)


def test_execution_intent_cannot_mutate_a_work_order_replicate_seed() -> None:
    work_order, intent = _bound_execution_intent()
    tampered_slot = intent.replicate_slot.model_copy(
        update={"randomization_seed_sha256": digest("forged-replicate-seed")}
    )
    tampered = intent.model_copy(update={"replicate_slot": tampered_slot})

    with pytest.raises(
        ExecutionIntentBindingError, match="revalidation|changed a frozen WorkOrder"
    ):
        verify_execution_intent_binding(work_order, tampered)
