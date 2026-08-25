from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionEffectClass,
    ExecutionIntent,
    InfrastructureAttempt,
    InputArtifactBinding,
    ScientificReplicateSlot,
)
from aletheia.protocols.base import canonical_json_bytes
from aletheia.protocols.compiler import (
    ExecutionIntentBindingError,
    ProtocolCompilationRequest,
    compile_protocol,
    verify_compilation,
    verify_execution_intent_binding,
)
from aletheia.protocols.schemas import ProtocolCompilationResult, WorkOrderDAG, WorkOrderNode
from fixtures import ProtocolFixture, accepted_protocol_fixtures, digest, fixture_by_name

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "fixture",
    accepted_protocol_fixtures(),
    ids=lambda item: item.name,
)
def test_domain_neutral_protocols_compile_through_one_canonical_path(
    fixture: ProtocolFixture,
) -> None:
    result = compile_protocol(fixture.request)

    assert result.report.accepted
    assert result.report.blockers == ()
    assert result.work_order is not None
    verify_compilation(fixture.request, result)

    work_order = result.work_order
    assert work_order.protocol_sha256 == fixture.request.protocol.protocol_sha256
    assert (
        work_order.resource_budget_sha256
        == fixture.request.protocol.resource_budget.resource_budget_sha256
    )
    assert work_order.nodes == tuple(sorted(work_order.nodes, key=lambda item: item.node_id))
    assert (
        work_order.work_order_sha256 == hashlib.sha256(canonical_json_bytes(work_order)).hexdigest()
    )
    assert result.receipt.work_order_sha256 == work_order.work_order_sha256

    node_id_by_step = {item.protocol_step_id: item.node_id for item in work_order.nodes}
    actual_dependencies = tuple(
        (
            step_id,
            tuple(sorted(node_id_by_step[dependency] for dependency in dependency_step_ids)),
        )
        for step_id, dependency_step_ids in fixture.expected_dependency_steps
    )
    compiled_dependencies = tuple(
        (
            step_id,
            next(
                item.dependency_node_ids
                for item in work_order.nodes
                if item.protocol_step_id == step_id
            ),
        )
        for step_id, _ in fixture.expected_dependency_steps
    )
    assert compiled_dependencies == actual_dependencies

    step_by_id = {item.step_id: item for item in fixture.request.protocol.steps}
    for node in work_order.nodes:
        step = step_by_id[node.protocol_step_id]
        assert (
            tuple(
                item.artifact_key
                for item in node.expected_artifacts
                if item.role is ArtifactRole.RAW_OUTPUT
            )
            == step.output_port_ids
        )


def test_fixtures_cover_linear_branched_and_external_protocol_shapes() -> None:
    results = {
        fixture.name: compile_protocol(fixture.request) for fixture in accepted_protocol_fixtures()
    }

    grouped = results["grouped_regression"].work_order
    intervention = results["structural_intervention_simulation"].work_order
    external = results["external_measurement"].work_order
    assert grouped is not None and intervention is not None and external is not None

    assert len(grouped.nodes) == 3
    assert len(intervention.nodes) == 4
    assert len(external.nodes) == 3
    assert max(len(item.dependency_node_ids) for item in grouped.nodes) == 1
    assert max(len(item.dependency_node_ids) for item in intervention.nodes) == 2
    assert {item.effect_class for item in grouped.nodes} == {ExecutionEffectClass.REPLAY_SAFE}
    assert {item.effect_class for item in intervention.nodes} == {ExecutionEffectClass.REPLAY_SAFE}
    assert {item.effect_class for item in external.nodes} == {
        ExecutionEffectClass.REPLAY_SAFE,
        ExecutionEffectClass.ONE_TIME_EXTERNAL,
    }


@pytest.mark.parametrize(
    "fixture",
    accepted_protocol_fixtures(),
    ids=lambda item: item.name,
)
def test_compilation_survives_fresh_process_json_roundtrip(
    fixture: ProtocolFixture,
) -> None:
    local_result = compile_protocol(fixture.request)
    script = """
import sys

from aletheia.protocols.compiler import ProtocolCompilationRequest, compile_protocol

request = ProtocolCompilationRequest.model_validate_json(sys.stdin.buffer.read())
sys.stdout.write(compile_protocol(request).model_dump_json())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPOSITORY_ROOT,
        input=fixture.request.model_dump_json(),
        text=True,
        capture_output=True,
        check=True,
    )
    fresh_result = ProtocolCompilationResult.model_validate_json(completed.stdout)

    assert canonical_json_bytes(fresh_result) == canonical_json_bytes(local_result)
    assert fresh_result.work_order is not None
    assert local_result.work_order is not None
    assert fresh_result.work_order.work_order_sha256 == local_result.work_order.work_order_sha256
    verify_compilation(fixture.request, fresh_result)


def _replicate_slot(
    work_order: WorkOrderDAG,
    node: WorkOrderNode,
    *,
    slot_index: int = 1,
) -> ScientificReplicateSlot:
    return ScientificReplicateSlot(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        slot_count=node.scientific_replicate_count,
        slot_index=slot_index,
        replicate_kind=node.replicate_kind,
        preregistration_sha256=node.replicate_preregistration_sha256,
        randomization_seed_sha256=node.replicate_seed_sha256s[slot_index - 1],
        independent_site_required=node.independent_site_required,
    )


def _input_bindings(
    work_order: WorkOrderDAG,
    node: WorkOrderNode,
    *,
    slot_index: int = 1,
) -> tuple[InputArtifactBinding, ...]:
    bindings: list[InputArtifactBinding] = []
    for input_port_id in node.input_port_ids:
        producers = tuple(
            candidate
            for candidate in work_order.nodes
            if input_port_id in candidate.output_port_ids
        )
        if not producers:
            bindings.append(
                InputArtifactBinding(
                    input_port_id=input_port_id,
                    source_kind="protocol_input",
                    artifact_verified_receipt_sha256=digest(
                        f"verified-protocol-input:{input_port_id}"
                    ),
                )
            )
            continue
        assert len(producers) == 1
        producer = producers[0]
        producer_slot = _replicate_slot(work_order, producer, slot_index=slot_index)
        bindings.append(
            InputArtifactBinding(
                input_port_id=input_port_id,
                source_kind="work_order_output",
                artifact_verified_receipt_sha256=digest(
                    f"verified-work-order-output:{input_port_id}"
                ),
                source_work_order_node_id=producer.node_id,
                source_work_order_node_sha256=producer.node_sha256,
                source_replicate_slot_id=producer_slot.replicate_slot_id,
                source_slot_index=producer_slot.slot_index,
            )
        )
    return tuple(sorted(bindings, key=lambda item: item.input_port_id))


def _bound_execution_intent(
    *,
    step_id: str,
) -> tuple[WorkOrderDAG, WorkOrderNode, ExecutionIntent]:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.protocol_step_id == step_id)
    slot = _replicate_slot(work_order, node)
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
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=_input_bindings(work_order, node),
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=fixture.request.protocol.authored_at,
        deadline=fixture.request.protocol.authored_at + timedelta(hours=1),
    )
    return work_order, node, intent


def test_execution_intent_binds_the_exact_compiled_command() -> None:
    work_order, node, intent = _bound_execution_intent(step_id="step.01_group")

    assert intent.command_sha256 == node.command_sha256
    verify_execution_intent_binding(work_order, intent)

    mutated = intent.model_copy(update={"command_sha256": digest("another-command")})
    with pytest.raises(ExecutionIntentBindingError, match="changed a frozen WorkOrder"):
        verify_execution_intent_binding(work_order, mutated)


@pytest.mark.parametrize(
    ("step_id", "expected_source_kind"),
    (
        ("step.01_group", "protocol_input"),
        ("step.02_estimate", "work_order_output"),
        ("step.03_validate", "work_order_output"),
    ),
)
def test_execution_intent_uses_typed_protocol_and_upstream_input_bindings(
    step_id: str,
    expected_source_kind: str,
) -> None:
    work_order, node, intent = _bound_execution_intent(step_id=step_id)

    assert tuple(item.input_port_id for item in intent.input_artifact_bindings) == (
        node.input_port_ids
    )
    assert {item.source_kind for item in intent.input_artifact_bindings} == {expected_source_kind}
    verify_execution_intent_binding(work_order, intent)


def test_execution_intent_rejects_untyped_or_inexact_upstream_inputs() -> None:
    work_order, node, intent = _bound_execution_intent(step_id="step.02_estimate")
    assert len(intent.input_artifact_bindings) == 1
    exact = intent.input_artifact_bindings[0]

    empty = intent.model_copy(update={"input_artifact_bindings": ()})
    with pytest.raises(ExecutionIntentBindingError, match="exactly one verified receipt"):
        verify_execution_intent_binding(work_order, empty)

    arbitrary_untyped = intent.model_copy(
        update={"input_artifact_bindings": (digest("arbitrary-untyped-receipt"),)}
    )
    with pytest.raises(ExecutionIntentBindingError, match="closed-model revalidation"):
        verify_execution_intent_binding(work_order, arbitrary_untyped)

    wrong_producer_node = next(
        candidate
        for candidate in work_order.nodes
        if candidate.node_id not in node.dependency_node_ids and candidate.node_id != node.node_id
    )
    wrong_producer = exact.model_copy(
        update={
            "source_work_order_node_id": wrong_producer_node.node_id,
            "source_work_order_node_sha256": wrong_producer_node.node_sha256,
        }
    )
    with pytest.raises(ExecutionIntentBindingError, match="exact dependency producer"):
        verify_execution_intent_binding(
            work_order,
            intent.model_copy(update={"input_artifact_bindings": (wrong_producer,)}),
        )

    wrong_slot = exact.model_copy(update={"source_replicate_slot_id": "rps_" + "f" * 32})
    with pytest.raises(ExecutionIntentBindingError, match="another producer replicate slot"):
        verify_execution_intent_binding(
            work_order,
            intent.model_copy(update={"input_artifact_bindings": (wrong_slot,)}),
        )


def test_execution_intent_verifier_revalidates_attempt_slot_identity() -> None:
    work_order, _, intent = _bound_execution_intent(step_id="step.01_group")
    wrong_attempt = InfrastructureAttempt(
        replicate_slot_id="rps_" + "f" * 32,
        attempt_number=1,
    )
    forged = intent.model_copy(update={"infrastructure_attempt": wrong_attempt})

    with pytest.raises(ExecutionIntentBindingError, match="closed-model revalidation"):
        verify_execution_intent_binding(work_order, forged)


def test_execution_intent_cannot_select_a_different_producer_replicate() -> None:
    fixture = fixture_by_name("structural_intervention_simulation")
    payload = fixture.request.model_dump(mode="python")
    for step in payload["protocol"]["steps"]:
        step["scientific_replicate_count"] = 2
        step["replicate_seed_sha256s"] = (
            digest(f"{step['step_id']}:seed:1"),
            digest(f"{step['step_id']}:seed:2"),
        )
    payload["protocol"]["resource_budget"]["maximum_total_artifact_bytes"] = 10**15
    request = ProtocolCompilationRequest.model_validate(payload)
    result = compile_protocol(request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.protocol_step_id == "step.03_compare")
    slot = _replicate_slot(work_order, node, slot_index=2)
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
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=_input_bindings(
            work_order,
            node,
            slot_index=2,
        ),
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=request.protocol.authored_at,
        deadline=request.protocol.authored_at + timedelta(hours=1),
    )
    verify_execution_intent_binding(work_order, intent)

    exact = intent.input_artifact_bindings[0]
    producer = next(
        item for item in work_order.nodes if item.node_id == exact.source_work_order_node_id
    )
    wrong_source_slot = _replicate_slot(work_order, producer, slot_index=1)
    wrong_binding = exact.model_copy(
        update={
            "source_slot_index": 1,
            "source_replicate_slot_id": wrong_source_slot.replicate_slot_id,
        }
    )
    forged_bindings = (wrong_binding, *intent.input_artifact_bindings[1:])
    forged = intent.model_copy(update={"input_artifact_bindings": forged_bindings})

    with pytest.raises(ExecutionIntentBindingError, match="exact dependency producer"):
        verify_execution_intent_binding(work_order, forged)
