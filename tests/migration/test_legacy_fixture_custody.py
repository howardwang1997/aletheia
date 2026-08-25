from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "legacy" / "v1"

EXPECTED_TOP_LEVEL_KEYS = {
    "endurance/bundle-receipt.json": {
        "authoritative_objects",
        "authoritative_source_projections",
        "capture",
        "derived_objects",
        "gate_id",
        "schema",
        "schema_version",
    },
    "endurance/checkpoint-identities.json": {
        "checkpoint_chain_sha256",
        "checkpoint_count",
        "checkpoints",
        "gate_id",
        "manifest_sha256",
        "projection",
        "report_sha256",
        "schema",
        "schema_version",
        "source_authority",
        "terminal_commands",
    },
    "endurance/derived-interpretation.json": {
        "interpretation",
        "mutates_source",
        "schema",
        "schema_version",
        "source_binding",
        "status",
        "supersedes",
    },
    "endurance/manifest.json": {
        "autonomous_allocation_enabled",
        "checkpoint_interval_seconds",
        "environment_manifest_sha256",
        "evidence_class",
        "frozen_budget_manifest_sha256",
        "frozen_data_role_manifest_sha256",
        "frozen_quest_spec_sha256",
        "frozen_question_sha256s",
        "gate_id",
        "gate_key",
        "harness_code_sha256",
        "initial_campaign_ids",
        "initial_graph_sha256",
        "maximum_checkpoint_gap_seconds",
        "minimum_efficiency_improvement_ppm",
        "minimum_negative_results",
        "minimum_portfolio_epochs",
        "minimum_process_kills",
        "minimum_provider_interruptions",
        "minimum_reproductions",
        "minimum_structural_pivots",
        "outward_actions_allowed",
        "prerequisite_fault_campaign_id",
        "prerequisite_fault_report_sha256",
        "quest_id",
        "required_duration_seconds",
        "schema_version",
    },
    "endurance/report.json": {
        "autonomous_allocation_enabled",
        "blockers",
        "checkpoint_chain_sha256",
        "checkpoint_count",
        "completed_at",
        "disposition",
        "efficiency",
        "elapsed_seconds",
        "eligible_for_f11_scientific_exit_review",
        "final_portfolio",
        "manifest",
        "maximum_observed_gap_seconds",
        "negative_result_count",
        "portfolio_epoch_count",
        "process_kill_count",
        "provider_interruption_count",
        "real_72h_passed",
        "report_sha256",
        "reproduction_count",
        "started_at",
        "structural_pivot_count",
    },
    "golden_contract.v1.json": {
        "availability_semantics",
        "cases",
        "contract_id",
        "contract_status",
        "outcome_coverage",
        "purpose",
        "schema_name",
        "schema_version",
        "source_identities",
        "use_policy",
    },
    "origin-assurance.v1.json": {
        "assurance_scope",
        "objects",
        "policy",
        "schema_name",
        "schema_version",
    },
    "run_projection_sources.v1.json": {
        "capture",
        "cases",
        "database_schema_revision",
        "exporter",
        "schema_name",
        "schema_version",
        "source_authority",
    },
    "run_projections.v1.json": {
        "bundle_sha256",
        "capture_policy",
        "cases",
        "database_schema_revision",
        "schema_name",
        "schema_version",
        "source_authority",
    },
    "snapshot/import-receipt.json": {
        "claim_ceiling",
        "data_role",
        "import_key",
        "import_mode",
        "imported_at",
        "imported_by",
        "importer_code_sha256",
        "legacy_mutation_propagates",
        "live_refresh_allowed",
        "object_count",
        "payload_authority",
        "receipt_id",
        "receipt_sha256",
        "schema_name",
        "schema_version",
        "scientific_admission_allowed",
        "snapshot_id",
        "snapshot_sha256",
        "target_scope_id",
        "total_bytes",
        "training_use_allowed",
        "verification_status",
    },
    "snapshot/manifest.json": {
        "exporter_code_sha256",
        "exporter_entrypoint",
        "exporter_entrypoint_sha256",
        "exporter_execution_assurance",
        "exporter_git_commit",
        "exporter_git_tree",
        "exporter_identity_scheme",
        "freezer_identity",
        "legacy_mutation_propagates",
        "live_refresh_allowed",
        "object_count",
        "objects",
        "payload_authority",
        "redaction_manifest_sha256",
        "schema_name",
        "schema_version",
        "snapshot_id",
        "snapshot_sha256",
        "source_scope",
        "source_system",
        "source_version",
        "total_bytes",
    },
    "snapshot/redaction-manifest.v1.json": {
        "claim_ceiling",
        "data_class",
        "live_refresh_allowed",
        "objects",
        "review_status",
        "schema_name",
        "schema_version",
        "scientific_admission_allowed",
        "training_use_allowed",
    },
    "snapshot/request.json": {
        "exporter_code_sha256",
        "exporter_entrypoint",
        "exporter_entrypoint_sha256",
        "exporter_execution_assurance",
        "exporter_git_commit",
        "exporter_git_tree",
        "exporter_identity_scheme",
        "objects",
        "redaction_manifest_sha256",
        "schema_name",
        "schema_version",
        "source_scope",
        "source_system",
        "source_version",
    },
    "snapshot/store/bindings/5b002fec7845491f65f4c776e0957eea595f6fc62437689fe9aaaaef0a761f0a.json": {
        "schema_name",
        "schema_version",
        "snapshot_id",
        "snapshot_sha256",
        "source_scope",
        "source_system",
        "source_version",
    },
    "snapshot/store/manifests/134ee8f705cafb3f361719ec6429f6fe86e2a8f42feda40d3715bc722d044ecc.json": {
        "exporter_code_sha256",
        "exporter_entrypoint",
        "exporter_entrypoint_sha256",
        "exporter_execution_assurance",
        "exporter_git_commit",
        "exporter_git_tree",
        "exporter_identity_scheme",
        "freezer_identity",
        "legacy_mutation_propagates",
        "live_refresh_allowed",
        "object_count",
        "objects",
        "payload_authority",
        "redaction_manifest_sha256",
        "schema_name",
        "schema_version",
        "snapshot_id",
        "snapshot_sha256",
        "source_scope",
        "source_system",
        "source_version",
        "total_bytes",
    },
}
EXPECTED_TOP_LEVEL_KEYS.update(
    {
        "snapshot/store/objects/sha256/bb/"
        "bb1c3273e82969ae1d9ed4d3ccf593ee4a277914492cb9fb8a7232090cf55624": set(
            EXPECTED_TOP_LEVEL_KEYS["run_projections.v1.json"]
        ),
        "snapshot/store/objects/sha256/f4/"
        "f4cc6bb4873744a4fae4eb6d243929ddd86c96891f6ccab8cb9825a6bf3c6c49": set(
            EXPECTED_TOP_LEVEL_KEYS["endurance/report.json"]
        ),
    }
)
EXPECTED_NON_JSON_FILES = {
    "snapshot/exporter.py": "c00d87ac4ac9b352114b4deab7546edc8366c2ff85f7a0489ae1bf84ad0801f8",
}

_FORBIDDEN_KEYS = re.compile(
    r"(?:^|_)(?:"
    r"api_?key|authorization|access_token|refresh_token|password|passwd|credentials?|"
    r"database_(?:url|dsn)|dsn|private_key|secret|raw_payload|payload_json|transcript"
    r")(?:$|_)",
    re.IGNORECASE,
)
_FORBIDDEN_VALUES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"(?:^|[\s'\"])/(?:Users|home|root)/"),
    re.compile(r"(?i)\b[A-Z]:\\Users\\"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])"),
)


def _walk(value: Any, pointer: str = "$") -> Iterator[tuple[str, Any]]:
    yield pointer, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{pointer}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{pointer}[{index}]")


def test_fixture_file_set_and_top_level_schemas_are_fail_closed() -> None:
    descendants = list(FIXTURE_ROOT.rglob("*"))
    assert not [path for path in descendants if path.is_symlink()]
    actual = {path.relative_to(FIXTURE_ROOT).as_posix() for path in descendants if path.is_file()}
    assert actual == set(EXPECTED_TOP_LEVEL_KEYS) | set(EXPECTED_NON_JSON_FILES)

    for relative_path, expected_keys in EXPECTED_TOP_LEVEL_KEYS.items():
        payload = json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))
        assert isinstance(payload, dict), relative_path
        assert set(payload) == expected_keys, relative_path

    for relative_path, expected_sha256 in EXPECTED_NON_JSON_FILES.items():
        data = (FIXTURE_ROOT / relative_path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_sha256, relative_path


def test_all_legacy_fixtures_reject_private_keys_endpoints_and_operator_identifiers() -> None:
    for relative_path in EXPECTED_TOP_LEVEL_KEYS:
        payload = json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))
        for pointer, value in _walk(payload):
            if isinstance(value, dict):
                offending_keys = [key for key in value if _FORBIDDEN_KEYS.search(key)]
                assert not offending_keys, (relative_path, pointer, offending_keys)
            elif isinstance(value, str):
                assert not any(pattern.search(value) for pattern in _FORBIDDEN_VALUES), (
                    relative_path,
                    pointer,
                )

    for relative_path in EXPECTED_NON_JSON_FILES:
        text = (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")
        assert not any(pattern.search(text) for pattern in _FORBIDDEN_VALUES), relative_path


def test_origin_assurance_sidecar_binds_every_strong_historical_source_label() -> None:
    payload = json.loads((FIXTURE_ROOT / "origin-assurance.v1.json").read_text())
    assert payload["schema_name"] == "aletheia.legacy_origin_assurance"
    assert payload["assurance_scope"] == "historical_external_capture_origin"
    assert payload["policy"] == {
        "declared_source_authority_is_cryptographic_provenance": False,
        "fixture_integrity_is_independently_verifiable": True,
        "scientific_admission_allowed": False,
    }
    objects = payload["objects"]
    assert [item["path"] for item in objects] == [
        "endurance/bundle-receipt.json",
        "endurance/checkpoint-identities.json",
        "run_projection_sources.v1.json",
        "run_projections.v1.json",
    ]
    for item in objects:
        assert set(item) == {
            "declared_source_authority",
            "origin_evidence_limit",
            "path",
            "sha256",
            "source_origin_assurance",
        }
        assert item["source_origin_assurance"] == "operator_attested"
        assert isinstance(item["origin_evidence_limit"], str)
        assert item["origin_evidence_limit"].strip()
        fixture_path = FIXTURE_ROOT / item["path"]
        assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == item["sha256"]
        source = json.loads(fixture_path.read_text())
        declared = source.get("source_authority") or source.get("capture", {}).get(
            "source_authority"
        )
        assert item["declared_source_authority"] == declared
