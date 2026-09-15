"""Third-branch verification regressions for the cuprate diagnostic service."""

from types import SimpleNamespace as NS

import pytest

from aletheia.execution import capability_sources as closure
from aletheia.protocols.capabilities import CapabilityManifestV2
from tests.protocols.test_capability_sources import verify


@pytest.mark.parametrize("case", ["run_cuprate_diagnostic"], indirect=True)
def test_cuprate_contract_pins_the_frozen_seed_analysis_lineage(case):
    result = verify(case)
    assert len(result["verified_capabilities"]) == 1
    contract = case.service.contract
    assert contract["adapter_ref"] == (
        "aletheia.execution.cuprate.service:CuprateDiagnosticService.run_cuprate_diagnostic"
    )
    assert contract["service_factory_ref"] == (
        "aletheia.research_controller_cuprate_runtime:build_cuprate_diagnostic_rpc_service"
    )
    assert contract["behavior"]["role"] == "analysis"
    assert contract["behavior"]["side_effect_class"] == "read_only_external"
    assert contract["input_ports"] == [
        {"port_id": "input.dataset_rows", "artifact_kind": "json", "schema": "input"}
    ]
    assert contract["output_ports"] == [
        {
            "port_id": "output.diagnostic_result",
            "artifact_kind": "json",
            "schema": "output",
        }
    ]
    for retained in (
        "execution/cuprate/service.py",
        "execution/cuprate/card_rows.py",
        "execution/cuprate/diagnostics.py",
        "execution/cuprate/doping.py",
        "domains/materials/featurizers.py",
        "research_controller_cuprate_runtime.py",
    ):
        assert retained in contract["source_files"]
    manifest = case.request.capability_catalog.manifests[0]
    assert manifest.runtime.determinism.value == "frozen_seeds"
    assert manifest.runtime.frozen_seeds == (0,)
    assert manifest.operation_id == "operation.run_cuprate_diagnostic"


@pytest.mark.parametrize("case", ["run_cuprate_diagnostic"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "stochastic",
        "deterministic",
        "wrong_seed",
        "archive_input",
        "provider_receipt",
        "reconciliation",
    ],
)
def test_cuprate_branch_rejects_wrong_determinism_or_archive_shape(case, change):
    value = case.request.capability_catalog.manifests[0].model_dump(mode="json")
    step = case.request.protocol.steps[0]
    runtime = next(iter(case.runtime.values()))
    expected = "local service"
    if change == "stochastic":
        value["runtime"]["determinism"] = "declared_stochastic"
    elif change == "deterministic":
        value["runtime"]["determinism"] = "deterministic"
        value["runtime"]["frozen_seeds"] = []
    elif change == "wrong_seed":
        value["runtime"]["frozen_seeds"] = [0, 1]
        expected = "frozen at zero"
    elif change == "archive_input":
        step.archived_observation_input = NS(
            lookup_input_port_id="input.raw_run_lookup",
            envelope_output_port_id="intermediate.raw_run",
            replicate_mapping="same_slot_index",
        )
        expected = "archive input"
    elif change == "provider_receipt":
        step.expected_artifacts = (
            NS(role=NS(value="provider_receipt"), required=True, schema_sha256="0" * 64),
        )
        expected = "write receipt"
    else:
        value["runtime"]["reconciliation_supported"] = True
        expected = "checkpoint or reconciliation"
    manifest = CapabilityManifestV2.model_validate(value)
    with pytest.raises(closure.CapabilitySourceVerificationError, match=expected):
        closure._verify_local_service_sources(case.root, manifest, runtime, step)


@pytest.mark.parametrize("case", ["run_cuprate_diagnostic"], indirect=True)
def test_cuprate_frozen_seed_class_requires_its_seed_tuple(case):
    value = case.request.capability_catalog.manifests[0].model_dump(mode="json")
    value["runtime"]["frozen_seeds"] = []
    with pytest.raises(ValueError):
        CapabilityManifestV2.model_validate(value)
