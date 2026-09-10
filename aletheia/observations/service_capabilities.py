"""Retained contracts for the two bounded observation service operations.

These describe engineering I/O and persistence. Deployment, custody and signed capability
audits remain separate requirements; constructing a contract never qualifies a service.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aletheia.research_kernel.schemas import canonical_sha256


@dataclass(frozen=True)
class ServiceCapabilitySources:
    contract: dict
    implementation_path: Path
    source_paths: dict[str, Path]
    source_bytes: dict[str, bytes]
    schemas: dict[str, dict]


def local_service_capability_sources(
    operation: str, *, read_bytes: Callable[[Path], bytes] = Path.read_bytes
) -> ServiceCapabilitySources:
    """Derive the exact supported service contract from its actual models and source files."""
    import hashlib

    from aletheia.observations.f9_v2_validation import CommittedF9V2ValidationCampaign
    from aletheia.observations.scientific_bridge import RawRunEnvelope
    from aletheia.research_controller.external_rpc import RawRunLoadResult, ValidationCampaignResult
    from aletheia.research_controller.external_rpc_server import (
        RawRunRPCPayload,
        ScientificSlotLookupRPCPayload,
    )

    root = Path(__file__).resolve().parents[1]
    common = (
        "observations/service_capabilities.py",
        "protocols/schemas.py",
        "protocols/compiler.py",
        "protocols/typecheck.py",
        "observations/scientific_bridge.py",
        "research_controller/external_rpc.py",
        "research_controller/external_rpc_server.py",
    )
    if operation == "load_raw_run":
        implementation = "observations/adapters.py"
        factory = "research_controller_raw_run_source_runtime.py"
        adapter = (
            "aletheia.observations.adapters:PostgreSQLRawRunEnvelopeSourceAdapter.load_raw_run"
        )
        factory_ref = (
            "aletheia.research_controller_raw_run_source_runtime:build_raw_run_source_rpc_service"
        )
        sources = (*common, implementation, factory)
        schemas = {
            "input": ScientificSlotLookupRPCPayload.model_json_schema(),
            "output": RawRunEnvelope.model_json_schema(),
            "wire_request": ScientificSlotLookupRPCPayload.model_json_schema(),
            "wire_response": RawRunLoadResult.model_json_schema(),
        }
        inputs = [{"port_id": "input.raw_run_lookup", "artifact_kind": "json", "schema": "input"}]
        outputs = [
            {"port_id": "intermediate.raw_run", "artifact_kind": "receipt", "schema": "output"}
        ]
        behavior = {
            "side_effect_class": "read_only_external",
            "role": "observation_parser",
            "invocation": {"keyword_arguments_from": "input.raw_run_lookup"},
            "archive_input": {
                "contract": "ProtocolStep.archived_observation_input",
                "source": "registered ScientificExecutionAuthorization.scientific_observation_artifact_binding",
                "slot_mapping": "same_slot_index",
                "custody": "Load the producer slot's verified terminal material from its signed registration; preserve its observation artifact binding in the returned envelope.",
            },
            "return_value": "intermediate.raw_run",
            "pending": "Signed terminal-material-pending status becomes RawRunEnvelopePending; no envelope is emitted.",
            "ready": "Verify the preregistered quest, action, slot and terminal artifacts before returning the envelope.",
            "persistence": "Read registered authorization and immutable terminal material; no domain writes.",
            "replay": "Reload and reverify the exact registered slot and retained terminal material.",
            "durable_artifacts": [],
        }
    elif operation == "prepare_validation_campaign":
        implementation = "observations/f9_v2_validation.py"
        factory = "research_controller_f9_v2_validation_runtime.py"
        adapter = "aletheia.observations.f9_v2_validation:F9V2IndependentValidationService.prepare_validation_campaign"
        factory_ref = "aletheia.research_controller_f9_v2_validation_runtime:build_f9_v2_validation_rpc_service"
        sources = (*common, implementation, factory, "observations/f9_v2_assessor.py")
        result_schema = ValidationCampaignResult.model_json_schema()
        schemas = {
            "input": RawRunEnvelope.model_json_schema(),
            "output": result_schema["properties"]["validation_campaign_sha256"],
            "wire_request": RawRunRPCPayload.model_json_schema(),
            "wire_response": result_schema,
            "committed_campaign": CommittedF9V2ValidationCampaign.model_json_schema(),
        }
        inputs = [
            {"port_id": "intermediate.raw_run", "artifact_kind": "receipt", "schema": "input"}
        ]
        outputs = [
            {
                "port_id": "output.validation_campaign_sha256",
                "artifact_kind": "json",
                "schema": "output",
            }
        ]
        behavior = {
            "side_effect_class": "durable_write",
            "role": "independent_validator",
            "invocation": {"keyword_arguments": {"raw_run": "intermediate.raw_run"}},
            "return_value": "output.validation_campaign_sha256",
            "null_result": "A non-successful terminal process returns null without publishing a campaign.",
            "persistence": "Verify the raw run, assess against the frozen exact-content catalog, sign and atomically publish one committed campaign.",
            "replay": "Load and verify the committed campaign for the exact raw run before assessing or signing; return its original campaign digest.",
            "clock": "The service supplies verification, assessment and commit times; first publication is not a pure function of raw_run.",
            "durable_artifacts": [
                {
                    "lookup": "returned campaign digest",
                    "schema": "committed_campaign",
                    "source": "validation_archive",
                }
            ],
        }
    else:
        raise ValueError("unsupported local capability service operation")

    paths = {name: root / name for name in sorted(sources)}
    payloads = {name: read_bytes(path) for name, path in paths.items()}
    schema_hashes = {name: canonical_sha256(value) for name, value in schemas.items()}
    contract = {
        "schema_name": "aletheia.local_observation_service_capability_contract",
        "schema_version": 1,
        "operation": operation,
        "runtime_kind": "external_service",
        "adapter_ref": adapter,
        "service_factory_ref": factory_ref,
        "transport": "unix_domain_socket",
        "network_egress": "none",
        "operation_batch_size": 1,
        "input_ports": inputs,
        "output_ports": outputs,
        "schema_sources": schema_hashes,
        "source_files": {
            name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()
        },
        "behavior": behavior,
        "scientific_authority": False,
    }
    return ServiceCapabilitySources(contract, root / implementation, paths, payloads, schemas)
