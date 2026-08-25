from __future__ import annotations

import sys
from pathlib import Path

from aletheia.execution.schemas import NetworkPolicy, ResourceKind, StaticResourceClass
from aletheia.legacy_evaluation.capability import (
    EVAL_ARTIFACT_KEY,
    INVOCATION_PORT_ID,
    MODEL_ARTIFACT_KEY,
    RAW_RESULT_ARTIFACT_KEY,
    TABLE_PORT_ID,
    legacy_evaluation_expected_artifacts,
)
from aletheia.legacy_evaluation.contracts import LegacyEvaluationHarnessManifest
from aletheia.protocols.base import canonical_sha256
from aletheia.protocols.capabilities import (
    ArtifactKind,
    CapabilityCatalog,
    CapabilityManifestV2,
    CapabilityPort,
    DeterminismClass,
    PortDirection,
    RuntimeKind,
    SideEffectClass,
)
from aletheia.protocols.claim_contracts import (
    ClaimKind,
    ClaimStrength,
    EpistemicKind,
    EvidenceModality,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, compile_protocol
from aletheia.protocols.schemas import (
    CapabilityRequirement,
    ProtocolContractKind,
    ProtocolActionCategory,
    ProtocolPortDirection,
    ProtocolStepRole,
    StepContractBinding,
)

from conftest import NOW, LegacyEvaluationCase

_fixture_dir = Path(__file__).resolve().parents[1] / "protocols"
if str(_fixture_dir) not in sys.path:
    sys.path.insert(0, str(_fixture_dir))

from fixtures import _PortSpec, _StepPlan, _audit_bindings, _build_fixture, _resource_request  # noqa: E402


def _compiler_request(
    manifest: CapabilityManifestV2,
    harness: LegacyEvaluationHarnessManifest,
) -> ProtocolCompilationRequest:
    cpu = StaticResourceClass(
        class_key="resource.cpu.pr6-legacy-evaluation",
        kind=ResourceKind.CPU,
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="oci-v1",
        cpu_cores=8,
        memory_bytes=16 * 1024**3,
        scratch_bytes=64 * 1024**3,
        features=("deterministic-runtime", "frozen-seed-runtime"),
        network_policies=(NetworkPolicy.NONE,),
        supports_exclusive=True,
    )
    leaf_request = _resource_request(cpu, wall_time_seconds=3_600).model_copy(
        update={"artifact_quota_bytes": 64 * 1024**2}
    )
    validator_request = _resource_request(cpu)
    port_specs = (
        _PortSpec("input.records", ProtocolPortDirection.INPUT, ArtifactKind.TABLE),
        _PortSpec(INVOCATION_PORT_ID, ProtocolPortDirection.INPUT, ArtifactKind.JSON),
        _PortSpec(TABLE_PORT_ID, ProtocolPortDirection.INTERMEDIATE, ArtifactKind.TABLE),
        _PortSpec(EVAL_ARTIFACT_KEY, ProtocolPortDirection.INTERMEDIATE, ArtifactKind.JSON),
        _PortSpec(MODEL_ARTIFACT_KEY, ProtocolPortDirection.OUTPUT, ArtifactKind.MODEL),
        _PortSpec(RAW_RESULT_ARTIFACT_KEY, ProtocolPortDirection.INTERMEDIATE, ArtifactKind.JSON),
        _PortSpec("output.lineage", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
        _PortSpec("output.validation", ProtocolPortDirection.OUTPUT, ArtifactKind.RECEIPT),
    )
    plans = (
        _StepPlan(
            step_id="step.00_parse",
            capability_id="capability.parse_legacy_table",
            operation_id="operation.parse_legacy_table",
            input_port_ids=("input.records",),
            output_port_ids=(TABLE_PORT_ID,),
            depends_on_step_ids=(),
            resource_request=validator_request,
            role=ProtocolStepRole.OBSERVATION_PARSER,
        ),
        _StepPlan(
            step_id="step.01_legacy_evaluate",
            capability_id=manifest.capability_id,
            operation_id=manifest.operation_id,
            input_port_ids=(INVOCATION_PORT_ID, TABLE_PORT_ID),
            output_port_ids=(
                EVAL_ARTIFACT_KEY,
                MODEL_ARTIFACT_KEY,
                RAW_RESULT_ARTIFACT_KEY,
            ),
            depends_on_step_ids=("step.00_parse",),
            resource_request=leaf_request,
            role=ProtocolStepRole.SCIENTIFIC_EXECUTOR,
            runtime_kind=RuntimeKind.DIGEST_PINNED_CONTAINER,
            determinism=DeterminismClass.FROZEN_SEEDS,
            frozen_seeds=(0,),
            side_effect_class=SideEffectClass.EPHEMERAL_WRITE,
        ),
        _StepPlan(
            step_id="step.02_validate",
            capability_id="capability.validate_legacy_evaluation",
            operation_id="operation.validate_legacy_evaluation",
            input_port_ids=(EVAL_ARTIFACT_KEY, RAW_RESULT_ARTIFACT_KEY),
            output_port_ids=("output.lineage", "output.validation"),
            depends_on_step_ids=("step.01_legacy_evaluate",),
            resource_request=validator_request,
            role=ProtocolStepRole.INDEPENDENT_VALIDATOR,
        ),
    )
    fixture = _build_fixture(
        name="pr6_legacy_evaluation",
        identity="6",
        action_category=ProtocolActionCategory.DETERMINISTIC_ANALYSIS,
        epistemic_kind=EpistemicKind.CHARACTERIZATION,
        claim_kind=ClaimKind.DESCRIPTIVE,
        claim_strength=ClaimStrength.EXPLORATORY,
        evidence_modality=EvidenceModality.COMPUTATIONAL,
        port_specs=port_specs,
        step_plans=plans,
        resources=(cpu,),
        measurement_step_id="step.01_legacy_evaluate",
        epistemic_shape="characterization",
    )
    protocol = fixture.request.protocol.model_copy(update={"authored_at": NOW})
    manifest_ports = {
        item.port_id: item for item in (*manifest.input_ports, *manifest.output_ports)
    }
    generic_parser = next(
        item
        for item in fixture.request.capability_catalog.manifests
        if item.capability_id == "capability.parse_legacy_table"
    )
    generic_validator = next(
        item
        for item in fixture.request.capability_catalog.manifests
        if item.capability_id == "capability.validate_legacy_evaluation"
    )
    table_output = CapabilityPort.model_validate(
        manifest_ports[TABLE_PORT_ID]
        .model_copy(
            update={
                "direction": PortDirection.OUTPUT,
                "description": "Verified local CSV table projection.",
            }
        )
        .model_dump(mode="python")
    )
    parser_manifest = CapabilityManifestV2.model_validate(
        generic_parser.model_copy(
            update={
                "output_ports": (table_output,),
                "license_egress": manifest.license_egress,
            }
        ).model_dump(mode="python")
    )
    raw_result_input = CapabilityPort.model_validate(
        manifest_ports[RAW_RESULT_ARTIFACT_KEY]
        .model_copy(
            update={
                "direction": PortDirection.INPUT,
                "description": "Independent structural validation input.",
            }
        )
        .model_dump(mode="python")
    )
    eval_input = CapabilityPort.model_validate(
        manifest_ports[EVAL_ARTIFACT_KEY]
        .model_copy(
            update={
                "direction": PortDirection.INPUT,
                "description": "Exact independently parsed evaluation record.",
            }
        )
        .model_dump(mode="python")
    )
    validator_manifest = CapabilityManifestV2.model_validate(
        generic_validator.model_copy(
            update={
                "input_ports": tuple(
                    sorted((eval_input, raw_result_input), key=lambda item: item.port_id)
                ),
                "license_egress": manifest.license_egress,
            }
        ).model_dump(mode="python")
    )
    data_ports = tuple(
        item.model_copy(
            update={
                "schema_ref": manifest_ports[item.port_id].schema_ref,
                "artifact_kind": manifest_ports[item.port_id].artifact_kind,
                "data_classification": manifest_ports[item.port_id].data_classification,
                "license_policy_sha256": manifest.license_egress.license_policy_sha256,
                "egress_policy_sha256": manifest.license_egress.egress_policy_sha256,
                "unit_or_ontology_sha256": canonical_sha256(
                    manifest_ports[item.port_id].unit_or_ontology_refs
                ),
            }
        )
        if item.port_id in manifest_ports
        else item.model_copy(
            update={
                "license_policy_sha256": manifest.license_egress.license_policy_sha256,
                "egress_policy_sha256": manifest.license_egress.egress_policy_sha256,
            }
        )
        for item in protocol.data_ports
    )
    leaf = next(item for item in protocol.steps if item.step_id == "step.01_legacy_evaluate")
    leaf = leaf.model_copy(
        update={
            "capability_requirement": CapabilityRequirement(
                requirement_id=leaf.capability_requirement.requirement_id,
                operation_id=manifest.operation_id,
                capability_id=manifest.capability_id,
                semantic_version=manifest.semantic_version,
                manifest_sha256=manifest.manifest_sha256,
                required_condition_sha256s=(harness.manifest_sha256,),
                audit_bindings=_audit_bindings(manifest),
            ),
            "expected_artifacts": legacy_evaluation_expected_artifacts(
                harness=harness,
                retention_policy_sha256=manifest.license_egress.retention_policy_sha256,
            ),
            "environment_sha256": manifest.runtime.environment_sha256,
        }
    )
    parser = next(item for item in protocol.steps if item.step_id == "step.00_parse")
    parser_expected = tuple(
        item.model_copy(
            update={
                "schema_sha256": manifest_ports[TABLE_PORT_ID].schema_ref.schema_sha256,
                "retention_policy_sha256": manifest.license_egress.retention_policy_sha256,
            }
        )
        for item in parser.expected_artifacts
    )
    parser = parser.model_copy(
        update={
            "capability_requirement": CapabilityRequirement(
                requirement_id=parser.capability_requirement.requirement_id,
                operation_id=parser_manifest.operation_id,
                capability_id=parser_manifest.capability_id,
                semantic_version=parser_manifest.semantic_version,
                manifest_sha256=parser_manifest.manifest_sha256,
                audit_bindings=_audit_bindings(parser_manifest),
            ),
            "environment_sha256": parser_manifest.runtime.environment_sha256,
            "expected_artifacts": parser_expected,
        }
    )
    validator = next(item for item in protocol.steps if item.step_id == "step.02_validate")
    validator = validator.model_copy(
        update={
            "capability_requirement": CapabilityRequirement(
                requirement_id=validator.capability_requirement.requirement_id,
                operation_id=validator_manifest.operation_id,
                capability_id=validator_manifest.capability_id,
                semantic_version=validator_manifest.semantic_version,
                manifest_sha256=validator_manifest.manifest_sha256,
                audit_bindings=_audit_bindings(validator_manifest),
            ),
            "environment_sha256": validator_manifest.runtime.environment_sha256,
            "expected_artifacts": tuple(
                item.model_copy(
                    update={
                        "retention_policy_sha256": (manifest.license_egress.retention_policy_sha256)
                    }
                )
                for item in validator.expected_artifacts
            ),
        }
    )
    steps = tuple(
        parser
        if item.step_id == "step.00_parse"
        else leaf
        if item.step_id == "step.01_legacy_evaluate"
        else validator
        if item.step_id == "step.02_validate"
        else item
        for item in protocol.steps
    )
    observable = protocol.observables[0].model_copy(
        update={
            "measurement_capability_manifest_sha256": manifest.manifest_sha256,
            "output_schema_sha256": manifest_ports[EVAL_ARTIFACT_KEY].schema_ref.schema_sha256,
            "unit_or_ontology_sha256": canonical_sha256(
                manifest_ports[EVAL_ARTIFACT_KEY].unit_or_ontology_refs
            ),
            "calibration_contract_sha256": canonical_sha256(manifest.calibration),
        }
    )
    output_bindings = (
        protocol.observable_output_bindings[0].model_copy(
            update={"observable_spec_sha256": observable.observable_sha256}
        ),
    )
    analysis_plan = protocol.analysis_plan.model_copy(
        update={"primary_endpoint_sha256s": (observable.observable_sha256,)}
    )
    epistemic_contract = protocol.epistemic_contract.model_copy(
        update={"observable_spec_sha256s": (observable.observable_sha256,)}
    )
    control = protocol.controls[0].model_copy(
        update={"observable_spec_sha256s": (observable.observable_sha256,)}
    )
    rebound_steps = []
    for step in steps:
        bindings = []
        for binding in step.contract_bindings:
            replacement = binding.contract_sha256
            if binding.contract_kind is ProtocolContractKind.EPISTEMIC:
                replacement = epistemic_contract.contract_sha256
            elif binding.contract_kind is ProtocolContractKind.CONTROL:
                replacement = canonical_sha256(control)
            elif binding.contract_kind is ProtocolContractKind.ANALYSIS:
                replacement = canonical_sha256(analysis_plan)
            bindings.append(
                StepContractBinding(
                    contract_kind=binding.contract_kind,
                    contract_sha256=replacement,
                )
            )
        rebound_steps.append(step.model_copy(update={"contract_bindings": tuple(bindings)}))
    resource_budget = protocol.resource_budget.model_copy(
        update={
            "permitted_retention_policy_sha256s": tuple(
                sorted(
                    {
                        *protocol.resource_budget.permitted_retention_policy_sha256s,
                        manifest.license_egress.retention_policy_sha256,
                    }
                )
            )
        }
    )
    independence = protocol.independence.model_copy(
        update={"executor_principal_ids": (manifest.principal.executor_principal_id,)}
    )
    protocol = protocol.model_copy(
        update={
            "analysis_plan": analysis_plan,
            "controls": (control,),
            "data_ports": data_ports,
            "epistemic_contract": epistemic_contract,
            "independence": independence,
            "observable_output_bindings": output_bindings,
            "observables": (observable,),
            "resource_budget": resource_budget,
            "steps": tuple(rebound_steps),
        }
    )
    other_manifests = tuple(
        item
        for item in fixture.request.capability_catalog.manifests
        if item.capability_id
        not in {
            manifest.capability_id,
            parser_manifest.capability_id,
            validator_manifest.capability_id,
        }
    )
    catalog = CapabilityCatalog(
        manifests=tuple(
            sorted(
                (*other_manifests, manifest, parser_manifest, validator_manifest),
                key=lambda item: item.manifest_sha256,
            )
        )
    )
    return ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=catalog,
        resource_catalog=fixture.request.resource_catalog,
        compiler_implementation_sha256=fixture.request.compiler_implementation_sha256,
    )


def test_standard_compiler_lowers_the_leaf_without_domain_special_cases(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    request = _compiler_request(
        legacy_evaluation_case.manifest,
        legacy_evaluation_case.harness,
    )
    result = compile_protocol(request)

    assert result.report.accepted is True
    assert result.work_order is not None
    node = next(
        item
        for item in result.work_order.nodes
        if item.capability_id == legacy_evaluation_case.manifest.capability_id
    )
    assert node.capability_manifest_sha256 == legacy_evaluation_case.manifest.manifest_sha256
    assert node.input_port_ids == (INVOCATION_PORT_ID, TABLE_PORT_ID)
    assert node.expected_artifacts == legacy_evaluation_case.intent.expected_artifacts
    assert not result.report.blockers
