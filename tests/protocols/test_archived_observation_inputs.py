"""Static protocol examples only; fixture manifests confer no runtime authority."""

import pytest
from pydantic import ValidationError

from aletheia.protocols.capabilities import ArtifactKind, RuntimeKind, SideEffectClass
from aletheia.protocols.claim_contracts import (
    ClaimKind,
    ClaimStrength,
    EpistemicKind,
    EvidenceModality,
)
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    _execution_command_sha256,
    compile_protocol,
    verify_compilation,
)
from aletheia.protocols.schemas import ProtocolActionCategory, ProtocolPortDirection as Direction
from aletheia.protocols.schemas import ProtocolStepRole as Role, WorkOrderDAG
from tests.protocols.fixtures import (
    _PortSpec,
    _StepPlan,
    _build_fixture,
    _cpu_resource,
    _resource_request,
)


def archive_request():
    cpu = _cpu_resource("resource.cpu.archive-example")
    service = type(cpu).model_validate(
        {
            **cpu.model_dump(mode="python"),
            "class_key": "resource.external.archive-example",
            "kind": "external",
            "external_action_kinds": ("load_raw_run",),
        }
    )
    service_request = type(_resource_request(cpu)).model_validate(
        {
            **_resource_request(cpu).model_dump(mode="python"),
            "accepted_resource_class_ids": (service.resource_class_id,),
        }
    )
    ports = (
        _PortSpec("input.payload", Direction.INPUT, ArtifactKind.JSON),
        _PortSpec("input.raw_run_lookup", Direction.INPUT, ArtifactKind.JSON),
        _PortSpec("intermediate.digest", Direction.INTERMEDIATE, ArtifactKind.TEXT),
        _PortSpec("intermediate.raw_run", Direction.INTERMEDIATE, ArtifactKind.RECEIPT),
        _PortSpec("output.lineage", Direction.OUTPUT, ArtifactKind.RECEIPT),
        _PortSpec("output.validation", Direction.OUTPUT, ArtifactKind.JSON),
    )
    plans = (
        _StepPlan(
            "step.01_execute",
            "capability.digest",
            "operation.digest",
            ("input.payload",),
            ("intermediate.digest",),
            (),
            _resource_request(cpu),
            Role.SCIENTIFIC_EXECUTOR,
        ),
        _StepPlan(
            "step.02_read",
            "capability.load_raw_run",
            "operation.load_raw_run",
            ("input.raw_run_lookup",),
            ("intermediate.raw_run",),
            ("step.01_execute",),
            service_request,
            Role.OBSERVATION_PARSER,
            runtime_kind=RuntimeKind.EXTERNAL_SERVICE,
            side_effect_class=SideEffectClass.READ_ONLY_EXTERNAL,
            external_action_kind="load_raw_run",
            adapter_ref="aletheia.observations.adapters:PostgreSQLRawRunEnvelopeSourceAdapter.load_raw_run",
        ),
        _StepPlan(
            "step.03_validate",
            "capability.validate",
            "operation.validate",
            ("intermediate.raw_run",),
            ("output.lineage", "output.validation"),
            ("step.02_read",),
            _resource_request(cpu),
            Role.INDEPENDENT_VALIDATOR,
        ),
    )
    fixture = _build_fixture(
        name="archive_example",
        identity="4",
        action_category=ProtocolActionCategory.COMPUTATIONAL_EXPERIMENT,
        epistemic_kind=EpistemicKind.CHARACTERIZATION,
        claim_kind=ClaimKind.DESCRIPTIVE,
        claim_strength=ClaimStrength.SUPPORTED,
        evidence_modality=EvidenceModality.COMPUTATIONAL,
        port_specs=ports,
        step_plans=plans,
        resources=(cpu, service),
        measurement_step_id="step.01_execute",
        epistemic_shape="characterization",
    )
    payload = fixture.request.model_dump(mode="python")
    payload["protocol"]["steps"][1]["archived_observation_input"] = {
        "observable_output_binding": payload["protocol"]["observable_output_bindings"][0],
        "lookup_input_port_id": "input.raw_run_lookup",
        "envelope_output_port_id": "intermediate.raw_run",
    }
    return ProtocolCompilationRequest.model_validate(payload)


def test_registered_archive_flow_compiles_and_roundtrips():
    request = archive_request()
    result = compile_protocol(request)
    assert result.report.accepted, result.report.blockers
    verify_compilation(request, result)
    reader = next(n for n in result.work_order.nodes if n.protocol_step_id == "step.02_read")
    assert reader.archived_observation_input == request.protocol.steps[1].archived_observation_input
    assert reader.input_port_ids == ("input.raw_run_lookup",)
    assert (
        WorkOrderDAG.model_validate_json(result.work_order.model_dump_json()) == result.work_order
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "producer",
        "observable",
        "artifact",
        "lookup",
        "envelope",
        "dependency",
        "replicate_count",
        "ordinary_service",
    ],
)
def test_archive_flow_rejects_rebound_or_unproved_edges(change):
    request = archive_request()
    payload = request.model_dump(mode="python")
    reader = payload["protocol"]["steps"][1]
    binding = reader["archived_observation_input"]
    if change == "missing":
        reader["archived_observation_input"] = None
    elif change in {"producer", "observable", "artifact"}:
        field, value = {
            "producer": ("producer_step_id", "step.03_validate"),
            "observable": ("observable_spec_sha256", "a" * 64),
            "artifact": ("output_port_id", "output.validation"),
        }[change]
        binding["observable_output_binding"][field] = value
    elif change == "lookup":
        binding["lookup_input_port_id"] = "input.payload"
    elif change == "envelope":
        binding["envelope_output_port_id"] = "output.lineage"
    elif change == "dependency":
        reader["depends_on_step_ids"] = ()
    elif change == "replicate_count":
        reader["scientific_replicate_count"] = 2
        reader["replicate_seed_sha256s"] *= 2
    else:
        reader["capability_requirement"] = payload["protocol"]["steps"][2]["capability_requirement"]
    result = compile_protocol(ProtocolCompilationRequest.model_validate(payload))
    assert not result.report.accepted
    assert result.work_order is None


def test_archive_binding_cannot_be_retargeted_inside_compiled_work_order():
    result = compile_protocol(archive_request())
    payload = result.work_order.model_dump(mode="python")
    reader = next(n for n in payload["nodes"] if n["protocol_step_id"] == "step.02_read")
    reader["archived_observation_input"]["observable_output_binding"]["output_port_id"] = (
        "output.validation"
    )
    with pytest.raises(ValidationError, match="exact producer"):
        WorkOrderDAG.model_validate(payload)


def test_unsupported_slot_mapping_is_not_an_archive_read():
    payload = archive_request().model_dump(mode="python")
    payload["protocol"]["steps"][1]["archived_observation_input"]["replicate_mapping"] = "any_slot"
    with pytest.raises(ValidationError, match="same_slot_index"):
        ProtocolCompilationRequest.model_validate(payload)


def test_archive_binding_is_part_of_the_exact_execution_command():
    request = archive_request()
    step = request.protocol.steps[1]
    manifest = request.capability_catalog.get_exact(step.capability_requirement.manifest_sha256)
    before = _execution_command_sha256(step=step, manifest=manifest)
    payload = step.model_dump(mode="python")
    payload["archived_observation_input"]["observable_output_binding"]["output_port_id"] = (
        "output.other"
    )
    changed = type(step).model_validate(payload)
    assert _execution_command_sha256(step=changed, manifest=manifest) != before
