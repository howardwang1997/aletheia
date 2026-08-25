from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from pydantic import ValidationError

from aletheia.execution.schemas import StaticResourceClass
from aletheia.protocols.capabilities import (
    CapabilityManifestV2,
    EpistemicKind,
    QualificationStatus,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest
from aletheia.protocols.schemas import (
    CompatibilityAuditReceipt,
    CompatibilityDimension,
    DataRole,
    PreauthorizationVisibility,
    ProtocolBlockerCode,
    ProtocolIR,
)
from aletheia.protocols.typecheck import (
    ProtocolCheckRequest,
    expected_compatibility_audit_policy_sha256,
    typecheck_protocol,
)
from aletheia.protocols.world_models import WorldModelSnapshotV2

from fixtures import accepted_protocol_fixtures, digest, fixture_by_name


def _payload(name: str = "grouped_regression") -> dict:
    return fixture_by_name(name).request.model_dump(mode="python")


def _validated(payload: dict) -> ProtocolCompilationRequest:
    return ProtocolCompilationRequest.model_validate(payload)


def _check_request(request: ProtocolCompilationRequest) -> ProtocolCheckRequest:
    return ProtocolCheckRequest(
        protocol=request.protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
    )


def _codes(request: ProtocolCompilationRequest) -> set[ProtocolBlockerCode]:
    return {item.code for item in typecheck_protocol(_check_request(request)).blockers}


def _replace_selected_manifest(
    payload: dict,
    step_index: int,
    mutate: Callable[[dict], None],
) -> CapabilityManifestV2:
    requirement = payload["protocol"]["steps"][step_index]["capability_requirement"]
    old_sha256 = requirement["manifest_sha256"]
    manifest_payloads = list(payload["capability_catalog"]["manifests"])
    selected = next(
        item for item in manifest_payloads if item["capability_id"] == requirement["capability_id"]
    )
    assert CapabilityManifestV2.model_validate(selected).manifest_sha256 == old_sha256
    mutate(selected)
    replacement = CapabilityManifestV2.model_validate(selected)
    manifests = [
        replacement
        if CapabilityManifestV2.model_validate(item).manifest_sha256 == old_sha256
        else CapabilityManifestV2.model_validate(item)
        for item in manifest_payloads
    ]
    payload["capability_catalog"]["manifests"] = tuple(
        item.model_dump(mode="python")
        for item in sorted(manifests, key=lambda value: value.manifest_sha256)
    )
    requirement["operation_id"] = replacement.operation_id
    requirement["capability_id"] = replacement.capability_id
    requirement["semantic_version"] = replacement.semantic_version
    requirement["manifest_sha256"] = replacement.manifest_sha256
    return replacement


def test_all_canonical_fixtures_typecheck_without_live_capacity_state() -> None:
    assert "available" not in StaticResourceClass.model_fields
    assert "busy" not in StaticResourceClass.model_fields
    for fixture in accepted_protocol_fixtures():
        report = typecheck_protocol(_check_request(fixture.request))
        assert report.accepted, (fixture.name, report.blockers)


def test_missing_observable_is_a_typed_blocker() -> None:
    payload = _payload()
    payload["protocol"]["epistemic_contract"]["observable_spec_sha256s"] = (
        digest("missing-observable"),
    )

    assert ProtocolBlockerCode.OBSERVABLE_MISSING in _codes(_validated(payload))


def test_observable_without_a_producer_step_output_binding_is_rejected() -> None:
    payload = _payload()
    payload["protocol"]["observable_output_bindings"] = ()

    assert ProtocolBlockerCode.OBSERVABLE_MISSING in _codes(_validated(payload))


def test_missing_bidirectional_prediction_is_a_typed_blocker() -> None:
    payload = _payload("structural_intervention_simulation")
    world_model_payload = payload["protocol"]["world_model"]
    world_model_payload["predictions"] = world_model_payload["predictions"][:1]
    world_model = WorldModelSnapshotV2.model_validate(world_model_payload)
    payload["protocol"]["world_model"] = world_model.model_dump(mode="python")
    payload["protocol"]["epistemic_contract"]["world_model_snapshot_sha256"] = (
        world_model.world_model_sha256
    )

    assert ProtocolBlockerCode.HYPOTHESIS_PREDICTION_MISSING in _codes(_validated(payload))


def test_discriminating_predictions_must_bind_this_method_and_outcome_space() -> None:
    payload = _payload("structural_intervention_simulation")
    world_model_payload = payload["protocol"]["world_model"]
    for prediction in world_model_payload["predictions"]:
        prediction["measurement_protocol_sha256"] = digest("another-method")
        prediction["outcome_space_sha256"] = digest("another-outcome-space")
    world_model = WorldModelSnapshotV2.model_validate(world_model_payload)
    payload["protocol"]["world_model"] = world_model.model_dump(mode="python")
    payload["protocol"]["epistemic_contract"]["world_model_snapshot_sha256"] = (
        world_model.world_model_sha256
    )

    assert ProtocolBlockerCode.HYPOTHESIS_PREDICTION_MISSING in _codes(_validated(payload))


def test_visible_confirmation_data_is_rejected_as_a_leak() -> None:
    payload = _payload("external_measurement")
    confirmation_port = next(
        item
        for item in payload["protocol"]["data_ports"]
        if item["data_role"] == DataRole.CONFIRMATION
    )
    confirmation_port["preauthorization_visibility"] = PreauthorizationVisibility.VISIBLE

    assert ProtocolBlockerCode.DATA_ROLE_CONFLICT in _codes(_validated(payload))


def test_post_observation_analysis_edit_is_not_preregistered() -> None:
    payload = _payload()
    payload["protocol"]["analysis_plan"]["frozen_before_observation"] = False

    assert ProtocolBlockerCode.ANALYSIS_NOT_PREREGISTERED in _codes(_validated(payload))


def test_dependency_cycle_is_rejected() -> None:
    payload = _payload()
    payload["protocol"]["steps"][0]["depends_on_step_ids"] = ("step.03_validate",)

    assert ProtocolBlockerCode.DAG_CYCLE in _codes(_validated(payload))


def test_undeclared_step_port_is_rejected() -> None:
    payload = _payload()
    inputs = payload["protocol"]["steps"][0]["input_port_ids"]
    payload["protocol"]["steps"][0]["input_port_ids"] = tuple(sorted((*inputs, "input.undeclared")))

    assert ProtocolBlockerCode.PORT_UNBOUND in _codes(_validated(payload))


def test_protocol_cannot_make_a_declared_output_artifact_optional() -> None:
    payload = _payload()
    payload["protocol"]["steps"][0]["expected_artifacts"][0]["required"] = False

    assert ProtocolBlockerCode.PORT_UNBOUND in _codes(_validated(payload))


def test_executor_validator_group_conflict_is_rejected() -> None:
    payload = _payload()
    payload["protocol"]["independence"]["validator_group_id"] = payload["protocol"]["independence"][
        "executor_group_id"
    ]

    assert ProtocolBlockerCode.INDEPENDENCE_CONFLICT in _codes(_validated(payload))


def test_absent_capability_is_rejected() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    payload["capability_catalog"]["manifests"] = tuple(
        item
        for item in payload["capability_catalog"]["manifests"]
        if item["capability_id"] != requirement["capability_id"]
    )

    assert ProtocolBlockerCode.CAPABILITY_UNAVAILABLE in _codes(_validated(payload))


def test_unpinned_ambiguous_capability_is_rejected() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    selected_payload = next(
        item
        for item in payload["capability_catalog"]["manifests"]
        if item["capability_id"] == requirement["capability_id"]
    )
    duplicate_payload = dict(selected_payload)
    duplicate_payload["capability_id"] = "capability.alternate_validator"
    duplicate = CapabilityManifestV2.model_validate(duplicate_payload)
    manifests = [
        *(
            CapabilityManifestV2.model_validate(item)
            for item in payload["capability_catalog"]["manifests"]
        ),
        duplicate,
    ]
    payload["capability_catalog"]["manifests"] = tuple(
        item.model_dump(mode="python")
        for item in sorted(manifests, key=lambda value: value.manifest_sha256)
    )
    requirement["capability_id"] = None
    requirement["semantic_version"] = None
    requirement["manifest_sha256"] = None

    assert ProtocolBlockerCode.CAPABILITY_AMBIGUOUS in _codes(_validated(payload))


def test_suspended_capability_is_not_qualified() -> None:
    payload = _payload()

    def suspend(manifest: dict) -> None:
        manifest["qualification"]["status"] = QualificationStatus.SUSPENDED

    _replace_selected_manifest(payload, -1, suspend)

    assert ProtocolBlockerCode.CAPABILITY_NOT_QUALIFIED in _codes(_validated(payload))


def test_missing_capability_audit_is_rejected() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    requirement["audit_bindings"] = tuple(
        item for item in requirement["audit_bindings"] if item["audit_kind"] != "runtime"
    )

    assert ProtocolBlockerCode.CAPABILITY_AUDIT_MISSING in _codes(_validated(payload))


def test_capability_auditor_cannot_be_an_execution_principal() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    requirement["audit_bindings"][0]["auditor_principal_id"] = payload["protocol"]["independence"][
        "executor_principal_ids"
    ][0]

    assert ProtocolBlockerCode.CAPABILITY_AUDIT_MISSING in _codes(_validated(payload))


def test_capability_auditor_cannot_grant_the_same_capability_qualification() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    manifest = next(
        item
        for item in payload["capability_catalog"]["manifests"]
        if item["capability_id"] == requirement["capability_id"]
    )
    requirement["audit_bindings"][0]["auditor_principal_id"] = manifest["qualification"][
        "qualified_by_principal_id"
    ]

    assert ProtocolBlockerCode.CAPABILITY_AUDIT_MISSING in _codes(_validated(payload))


def test_capability_epistemic_applicability_mismatch_is_rejected() -> None:
    payload = _payload()

    def change_applicability(manifest: dict) -> None:
        manifest["applicability"]["epistemic_kinds"] = (EpistemicKind.CHARACTERIZATION,)

    _replace_selected_manifest(payload, -1, change_applicability)

    assert ProtocolBlockerCode.CAPABILITY_APPLICABILITY_MISMATCH in _codes(_validated(payload))


def test_step_must_bind_every_non_optional_capability_port() -> None:
    payload = _payload()

    def add_required_output(manifest: dict) -> None:
        additional = dict(manifest["output_ports"][0])
        additional["port_id"] = "output.unbound_required"
        manifest["output_ports"] = tuple(
            sorted((*manifest["output_ports"], additional), key=lambda item: item["port_id"])
        )

    _replace_selected_manifest(payload, -1, add_required_output)

    assert ProtocolBlockerCode.PORT_UNBOUND in _codes(_validated(payload))


def test_step_must_bind_every_non_optional_capability_input() -> None:
    payload = _payload()

    def add_required_input(manifest: dict) -> None:
        additional = dict(manifest["input_ports"][0])
        additional["port_id"] = "input.unbound_required"
        manifest["input_ports"] = tuple(
            sorted((*manifest["input_ports"], additional), key=lambda item: item["port_id"])
        )

    _replace_selected_manifest(payload, -1, add_required_input)

    assert ProtocolBlockerCode.PORT_UNBOUND in _codes(_validated(payload))


def test_static_resource_shape_mismatch_is_rejected() -> None:
    payload = _payload()
    payload["protocol"]["steps"][0]["resource_request"]["cpu_cores"] = 1_000_000

    assert ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_internal_runtime_cannot_be_scheduled_on_an_external_action_resource() -> None:
    payload = _payload()
    resource_payload = payload["resource_catalog"]["resource_classes"][0]
    old_resource = StaticResourceClass.model_validate(resource_payload)
    resource_payload["kind"] = "external"
    resource_payload["external_action_kinds"] = ("external.unrelated_action",)
    external_resource = StaticResourceClass.model_validate(resource_payload)
    payload["resource_catalog"]["resource_classes"] = (external_resource.model_dump(mode="python"),)
    for step in payload["protocol"]["steps"]:
        accepted = step["resource_request"]["accepted_resource_class_ids"]
        step["resource_request"]["accepted_resource_class_ids"] = tuple(
            external_resource.resource_class_id if item == old_resource.resource_class_id else item
            for item in accepted
        )

    assert ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_step_environment_must_match_the_selected_capability() -> None:
    payload = _payload()
    payload["protocol"]["steps"][0]["environment_sha256"] = digest("untrusted-environment")

    assert ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_read_only_external_capability_requires_an_external_runtime_and_action() -> None:
    payload = _payload()
    requirement = payload["protocol"]["steps"][-1]["capability_requirement"]
    manifest = next(
        item
        for item in payload["capability_catalog"]["manifests"]
        if item["capability_id"] == requirement["capability_id"]
    )
    manifest["side_effect_class"] = "read_only_external"

    with pytest.raises(ValidationError, match="external runtime"):
        CapabilityManifestV2.model_validate(manifest)


def test_read_only_external_runtime_requires_a_matching_external_resource() -> None:
    payload = _payload()

    def make_external_read(manifest: dict) -> None:
        manifest["side_effect_class"] = "read_only_external"
        manifest["runtime"]["runtime_kind"] = "external_service"
        manifest["external_action_kind"] = "external.read"

    _replace_selected_manifest(payload, -1, make_external_read)

    assert ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_one_time_external_step_cannot_request_multiple_infrastructure_attempts() -> None:
    payload = _payload("external_measurement")
    step = payload["protocol"]["steps"][0]
    step["resource_request"]["max_infrastructure_attempts"] = 2

    assert ProtocolBlockerCode.RETRY_POLICY_INCOMPATIBLE in _codes(_validated(payload))


def test_one_time_external_manifest_cannot_advertise_retryable_attempts() -> None:
    payload = _payload("external_measurement")
    requirement = payload["protocol"]["steps"][0]["capability_requirement"]
    manifest = next(
        item
        for item in payload["capability_catalog"]["manifests"]
        if item["capability_id"] == requirement["capability_id"]
    )
    failure_id = manifest["failure_modes"][0]["failure_id"]
    manifest["failure_modes"][0]["category"] = "infrastructure"
    manifest["failure_modes"][0]["disposition"] = "retryable"
    manifest["retry"] = {
        "mode": "idempotent_new_attempt",
        "maximum_attempts_per_scientific_slot": 2,
        "retryable_failure_ids": (failure_id,),
        "idempotency_rule_sha256": digest("one-time-idempotency-rule"),
        "reconciliation_rule_sha256": None,
        "checkpoint_schema": None,
        "best_of_n_forbidden": True,
    }

    with pytest.raises(ValidationError, match="one-time external"):
        CapabilityManifestV2.model_validate(manifest)


def test_expected_artifact_bounds_must_fit_the_step_quota() -> None:
    payload = _payload()
    step = payload["protocol"]["steps"][0]
    step["expected_artifacts"][0]["max_bytes"] = (
        step["resource_request"]["artifact_quota_bytes"] + 1
    )

    assert ProtocolBlockerCode.RESOURCE_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_capability_artifact_kind_cannot_masquerade_as_another_kind() -> None:
    payload = _payload()
    protocol_port = next(
        item for item in payload["protocol"]["data_ports"] if item["port_id"] == "output.estimate"
    )
    protocol_port["artifact_kind"] = "text"

    assert ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_identity_lineage_cannot_use_an_output_schema_as_its_input_schema() -> None:
    payload = _payload()
    input_port = next(
        item for item in payload["protocol"]["data_ports"] if item["direction"] == "input"
    )
    input_port["identity_schema_sha256"] = digest("actual-input-identity-schema")

    assert ProtocolBlockerCode.IDENTITY_LINEAGE_OPEN in _codes(_validated(payload))


def test_selected_capability_claim_ceiling_rejects_overclaim() -> None:
    payload = _payload()

    def lower_ceiling(manifest: dict) -> None:
        manifest["claim_ceiling"]["allowances"][0]["maximum_strength"] = "exploratory"

    _replace_selected_manifest(payload, -1, lower_ceiling)

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_claim_cannot_list_an_unimplemented_epistemic_kind() -> None:
    payload = _payload()
    payload["protocol"]["claim_contract"]["epistemic_kinds"] = (
        "characterization",
        "estimation",
    )

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_protocol_cannot_weaken_selected_manifest_replication_requirement() -> None:
    payload = _payload()

    def require_external_replication(manifest: dict) -> None:
        manifest["claim_ceiling"]["required_replication_tier"] = "external_independent"

    _replace_selected_manifest(payload, -1, require_external_replication)

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_protocol_cannot_omit_a_selected_manifest_evidence_modality() -> None:
    payload = _payload()

    def require_formal_evidence(manifest: dict) -> None:
        manifest["claim_ceiling"]["required_evidence_modalities"] = (
            "computational",
            "formal",
        )

    _replace_selected_manifest(payload, -1, require_formal_evidence)

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_protocol_cannot_disable_manifest_required_independent_validation() -> None:
    payload = _payload()
    for contract in (
        payload["protocol"]["claim_contract"],
        payload["protocol"]["epistemic_contract"],
    ):
        contract["ceiling" if "ceiling" in contract else "claim_ceiling"][
            "independent_validation_required"
        ] = False

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_epistemic_and_claim_ceilings_cannot_disagree_on_replication() -> None:
    payload = _payload()
    payload["protocol"]["epistemic_contract"]["claim_ceiling"]["required_replication_tier"] = (
        "external_independent"
    )

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_analysis_endpoints_must_be_declared_observables() -> None:
    payload = _payload()
    payload["protocol"]["analysis_plan"]["primary_endpoint_sha256s"] = (
        digest("undeclared-analysis-endpoint"),
    )

    assert ProtocolBlockerCode.OBSERVABLE_MISSING in _codes(_validated(payload))


def test_control_cannot_claim_coverage_without_any_bound_signal() -> None:
    payload = _payload()
    control = payload["protocol"]["controls"][0]
    control["input_port_ids"] = ()
    control["observable_spec_sha256s"] = ()

    assert ProtocolBlockerCode.CONTROL_COVERAGE_MISSING in _codes(_validated(payload))


def test_every_scientific_contract_must_be_implemented_by_a_step() -> None:
    payload = _payload()
    step = next(item for item in payload["protocol"]["steps"] if item["contract_bindings"])
    step["contract_bindings"] = step["contract_bindings"][1:]

    assert ProtocolBlockerCode.PARAMETER_HASH_UNCOVERED in _codes(_validated(payload))


def test_every_caller_parameter_must_bind_an_execution_step() -> None:
    payload = _payload()
    for step in payload["protocol"]["steps"]:
        step["caller_parameter_ids"] = ()

    assert ProtocolBlockerCode.PARAMETER_HASH_UNCOVERED in _codes(_validated(payload))


def test_caller_parameter_cannot_be_bound_only_to_the_validator() -> None:
    payload = _payload()
    parameter_ids = tuple(
        item["parameter_id"] for item in payload["protocol"]["caller_parameter_bindings"]
    )
    for step in payload["protocol"]["steps"]:
        step["caller_parameter_ids"] = (
            parameter_ids if step["role"] == "independent_validator" else ()
        )

    assert ProtocolBlockerCode.PARAMETER_HASH_UNCOVERED in _codes(_validated(payload))


def _schema_compatibility_request(*, receipt_direction: str) -> ProtocolCompilationRequest:
    payload = _payload()
    port_id = "output.estimate"
    protocol_port = next(
        item for item in payload["protocol"]["data_ports"] if item["port_id"] == port_id
    )
    original_schema_sha256 = protocol_port["schema_ref"]["schema_sha256"]
    replacement_schema_sha256 = digest("output-estimate-schema-v2")
    protocol_port["schema_ref"]["schema_sha256"] = replacement_schema_sha256
    producer = next(
        item for item in payload["protocol"]["steps"] if port_id in item["output_port_ids"]
    )
    expected_artifact = next(
        item for item in producer["expected_artifacts"] if item["artifact_key"] == port_id
    )
    expected_artifact["schema_sha256"] = replacement_schema_sha256
    if receipt_direction == "correct":
        source, target = original_schema_sha256, replacement_schema_sha256
    else:
        source, target = replacement_schema_sha256, original_schema_sha256
    receipt = CompatibilityAuditReceipt(
        dimension=CompatibilityDimension.JSON_SCHEMA,
        source_sha256=source,
        target_sha256=target,
        audit_policy_sha256=expected_compatibility_audit_policy_sha256(
            dimension=CompatibilityDimension.JSON_SCHEMA,
            source_sha256=source,
            target_sha256=target,
        ),
        evidence_sha256s=(digest("schema-compatibility-evidence"),),
        audited_by_principal_id="principal:compatibility_auditor",
        audited_at=payload["protocol"]["authored_at"],
    )
    payload["protocol"]["compatibility_audit_receipts"] = (receipt.model_dump(mode="python"),)
    return _validated(payload)


def test_compatibility_receipt_is_direction_bound() -> None:
    wrong_codes = _codes(_schema_compatibility_request(receipt_direction="reversed"))
    correct_codes = _codes(_schema_compatibility_request(receipt_direction="correct"))

    assert ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE in wrong_codes
    assert ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE not in correct_codes


def test_post_freeze_compatibility_audit_cannot_retroactively_authorize_a_port() -> None:
    request = _schema_compatibility_request(receipt_direction="correct")
    payload = request.model_dump(mode="python")
    payload["protocol"]["compatibility_audit_receipts"][0]["audited_at"] = payload["protocol"][
        "authored_at"
    ] + timedelta(seconds=1)

    assert ProtocolBlockerCode.SCOPE_MISMATCH in _codes(_validated(payload))


def test_protocol_author_cannot_self_issue_a_compatibility_audit() -> None:
    request = _schema_compatibility_request(receipt_direction="correct")
    payload = request.model_dump(mode="python")
    payload["protocol"]["compatibility_audit_receipts"][0]["audited_by_principal_id"] = payload[
        "protocol"
    ]["authored_by_principal_id"]

    assert ProtocolBlockerCode.PORT_SCHEMA_INCOMPATIBLE in _codes(_validated(payload))


def test_protocol_cannot_predate_a_scientific_subcontract() -> None:
    payload = _payload()
    payload["protocol"]["objective"]["authored_at"] = payload["protocol"][
        "authored_at"
    ] + timedelta(seconds=1)

    assert ProtocolBlockerCode.SCOPE_MISMATCH in _codes(_validated(payload))


def _set_protocol_replication_tier(payload: dict, tier: str) -> None:
    payload["protocol"]["claim_contract"]["ceiling"]["required_replication_tier"] = tier
    payload["protocol"]["epistemic_contract"]["claim_ceiling"]["required_replication_tier"] = tier


def _set_step_replicate_count(step: dict, count: int) -> None:
    step["scientific_replicate_count"] = count
    step["replicate_seed_sha256s"] = tuple(
        digest(f"{step['step_id']}:replicate-seed:{index}") for index in range(1, count + 1)
    )


def test_pipeline_step_count_cannot_masquerade_as_exact_replication() -> None:
    payload = _payload()
    _set_protocol_replication_tier(payload, "exact_reexecution")

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_parser_repetition_cannot_masquerade_as_scientific_replication() -> None:
    payload = _payload()
    _set_protocol_replication_tier(payload, "exact_reexecution")
    parser = next(
        item for item in payload["protocol"]["steps"] if item["role"] == "observation_parser"
    )
    _set_step_replicate_count(parser, 2)
    payload["protocol"]["resource_budget"]["maximum_total_artifact_bytes"] = 10**15

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_intermediate_data_flow_cannot_select_one_of_multiple_producer_slots() -> None:
    payload = _payload()
    producer = payload["protocol"]["steps"][0]
    _set_step_replicate_count(producer, 2)
    payload["protocol"]["resource_budget"]["maximum_total_artifact_bytes"] = 10**15

    assert ProtocolBlockerCode.PORT_UNBOUND in _codes(_validated(payload))


def test_independent_validator_must_consume_the_claim_observable_dataflow() -> None:
    payload = _payload()
    validator = next(
        item for item in payload["protocol"]["steps"] if item["role"] == "independent_validator"
    )
    protocol_input = next(
        item["port_id"]
        for item in payload["protocol"]["data_ports"]
        if item["direction"] == "input"
    )
    validator["input_port_ids"] = (protocol_input,)

    assert ProtocolBlockerCode.INDEPENDENCE_CONFLICT in _codes(_validated(payload))


def test_validator_cannot_consume_a_different_output_from_the_observable_producer() -> None:
    payload = _payload()
    observable_binding = payload["protocol"]["observable_output_bindings"][0]
    producer = next(
        item
        for item in payload["protocol"]["steps"]
        if item["step_id"] == observable_binding["producer_step_id"]
    )
    observable_binding["output_port_id"] = next(
        port_id
        for port_id in producer["output_port_ids"]
        if port_id != observable_binding["output_port_id"]
    )

    assert ProtocolBlockerCode.INDEPENDENCE_CONFLICT in _codes(_validated(payload))


def test_every_scientific_executor_can_preregister_exact_reexecution_slots() -> None:
    payload = _payload("structural_intervention_simulation")
    _set_protocol_replication_tier(payload, "exact_reexecution")
    for step in payload["protocol"]["steps"]:
        _set_step_replicate_count(step, 2)
    payload["protocol"]["resource_budget"]["maximum_total_artifact_bytes"] = 10**15
    interim = _validated(payload)
    epistemic_sha256 = interim.protocol.epistemic_contract.contract_sha256
    for step in payload["protocol"]["steps"]:
        for binding in step["contract_bindings"]:
            if binding["contract_kind"] == "epistemic":
                binding["contract_sha256"] = epistemic_sha256

    assert typecheck_protocol(_check_request(_validated(payload))).accepted


@pytest.mark.parametrize(
    "tier",
    ["independent_implementation", "external_independent"],
)
def test_slot_count_cannot_prove_independent_replication(tier: str) -> None:
    payload = _payload()
    _set_protocol_replication_tier(payload, tier)
    for step in payload["protocol"]["steps"]:
        _set_step_replicate_count(step, 10)
    payload["protocol"]["resource_budget"]["maximum_total_artifact_bytes"] = 10**15

    assert ProtocolBlockerCode.UNSUPPORTED_CLAIM in _codes(_validated(payload))


def test_typechecker_revalidates_model_construct_tampering() -> None:
    request = fixture_by_name("grouped_regression").request
    invalid_protocol = ProtocolIR.model_construct(
        **{
            **request.protocol.__dict__,
            "version": 2,
            "revision_parent_sha256": None,
        }
    )
    constructed = ProtocolCheckRequest.model_construct(
        protocol=invalid_protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
    )

    with pytest.raises(ValidationError, match="version 1"):
        typecheck_protocol(constructed)
