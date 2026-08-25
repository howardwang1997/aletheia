from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aletheia.programs.endurance_schemas import (
    EnduranceGateDisposition,
    EnduranceGateManifest,
    EnduranceGateReport,
)
from aletheia.reproducibility.manifest import content_sha256


_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "legacy" / "v1" / "endurance"
_GATE_ID = "edg_720570e0ac6b219638b5c14f3ff1b2bc"
_MANIFEST_SHA256 = "720570e0ac6b219638b5c14f3ff1b2bc42f45bb6c28362f16cc20dfda4e7f8de"
_REPORT_SHA256 = "20bc3e61474ccb39e217f2528f80b5f084c5c56c1ff2312000ffeb5e33cab56c"
_CHAIN_SHA256 = "e038e168917637f2afb77d9a4c275d0295835a7ea31f0c536a29748acd133dcc"
_IDENTITY_PROJECTION_SHA256 = "fddea56a11c2e72c305d0894722a5a14c2f780c2347a5770baae51cbb8e28ac0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_FROZEN_FILE_SHA256S = {
    "bundle-receipt.json": "0ad97cb8d90cf0d45e06671020248cd22b584acfb7a7c58335b5d4093bdb2e7e",
    "checkpoint-identities.json": (
        "c0edd03bd15dd17e89b250662d8e01e69e4b83151144539dea55f7594c26e587"
    ),
    "derived-interpretation.json": (
        "6fee2801e87c659d6380314d70ee2ec9bee9bc7483ae96e9eacd3b40eb6b7fb1"
    ),
    "manifest.json": "c8686519a6aac67636cf78cb47b9380997e42ba3a593cc973bd6eb1f19782a27",
    "report.json": "f4cc6bb4873744a4fae4eb6d243929ddd86c96891f6ccab8cb9825a6bf3c6c49",
}


def _read_json(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _aware_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
    return parsed


def _assert_durable_reference(reference: dict[str, Any], event_type: str) -> None:
    assert set(reference) == {
        "command_id",
        "event_sha256",
        "event_type",
        "output_event_id",
        "output_event_key",
    }
    command_id = reference["command_id"]
    assert re.fullmatch(r"scmd_[0-9a-f]{32}", command_id)
    assert reference["event_type"] == event_type
    assert reference["output_event_key"] == f"scientific-command:{command_id}"
    assert isinstance(reference["output_event_id"], int)
    assert reference["output_event_id"] > 0
    assert _SHA256.fullmatch(reference["event_sha256"])


def _assert_checkpoint_projection(
    projection: dict[str, Any],
    manifest: EnduranceGateManifest,
    report: EnduranceGateReport,
) -> None:
    assert set(projection) == {
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
    }
    assert projection["schema"] == "aletheia.legacy.endurance_checkpoint_identities"
    assert projection["schema_version"] == 1
    assert projection["source_authority"] == "validating_postgresql_store"
    assert projection["projection"] == "identities_and_durable_references_only"
    assert projection["gate_id"] == manifest.gate_id == _GATE_ID
    assert projection["manifest_sha256"] == manifest.manifest_sha256 == _MANIFEST_SHA256
    assert projection["report_sha256"] == report.report_sha256 == _REPORT_SHA256

    terminal = projection["terminal_commands"]
    assert set(terminal) == {"finalize", "start"}
    _assert_durable_reference(terminal["start"], "research_endurance_started")
    _assert_durable_reference(terminal["finalize"], "research_endurance_finalized")

    checkpoints = projection["checkpoints"]
    assert projection["checkpoint_count"] == report.checkpoint_count == len(checkpoints) == 73
    checkpoint_fields = {
        "checkpoint_id",
        "checkpoint_sha256",
        "command_id",
        "event_sha256",
        "event_type",
        "observation_sha256",
        "observed_at",
        "output_event_id",
        "output_event_key",
        "parent_sha256",
        "process_kill_count",
        "provider_interruption_count",
        "reproduction_count",
        "sequence",
        "structural_pivot_count",
    }
    expected_parent = manifest.manifest_sha256
    observed_times: list[datetime] = []
    checkpoint_hashes: list[str] = []
    command_ids: set[str] = set()
    event_keys: set[str] = set()
    for expected_sequence, checkpoint in enumerate(checkpoints, start=1):
        assert set(checkpoint) == checkpoint_fields
        assert checkpoint["sequence"] == expected_sequence
        assert checkpoint["parent_sha256"] == expected_parent
        assert _SHA256.fullmatch(checkpoint["checkpoint_sha256"])
        assert _SHA256.fullmatch(checkpoint["observation_sha256"])
        assert checkpoint["checkpoint_id"] == (f"edc_{checkpoint['checkpoint_sha256'][:32]}")

        reference = {
            key: checkpoint[key]
            for key in (
                "command_id",
                "event_sha256",
                "event_type",
                "output_event_id",
                "output_event_key",
            )
        }
        _assert_durable_reference(reference, "research_endurance_checkpointed")
        assert checkpoint["command_id"] not in command_ids
        assert checkpoint["output_event_key"] not in event_keys
        command_ids.add(checkpoint["command_id"])
        event_keys.add(checkpoint["output_event_key"])

        for count_field in (
            "process_kill_count",
            "provider_interruption_count",
            "reproduction_count",
            "structural_pivot_count",
        ):
            assert isinstance(checkpoint[count_field], int)
            assert checkpoint[count_field] >= 0
        observed_times.append(_aware_timestamp(checkpoint["observed_at"]))
        checkpoint_hashes.append(checkpoint["checkpoint_sha256"])
        expected_parent = checkpoint["checkpoint_sha256"]

    assert observed_times == sorted(set(observed_times))
    points = (report.started_at, *observed_times, report.completed_at)
    gaps = [math.ceil((right - left).total_seconds()) for left, right in zip(points, points[1:])]
    assert max(gaps) == report.maximum_observed_gap_seconds == 3837
    assert sum(item["reproduction_count"] for item in checkpoints) == report.reproduction_count
    assert sum(item["process_kill_count"] for item in checkpoints) == report.process_kill_count
    assert sum(item["provider_interruption_count"] for item in checkpoints) == (
        report.provider_interruption_count
    )
    assert sum(item["structural_pivot_count"] for item in checkpoints) == (
        report.structural_pivot_count
    )

    recalculated_chain = content_sha256(
        {
            "schema": "aletheia.research_endurance_checkpoint_chain.v1",
            "manifest_sha256": manifest.manifest_sha256,
            "checkpoint_sha256s": tuple(checkpoint_hashes),
        }
    )
    assert recalculated_chain == projection["checkpoint_chain_sha256"]
    assert recalculated_chain == report.checkpoint_chain_sha256 == _CHAIN_SHA256


def test_authoritative_manifest_and_report_recalculate_to_frozen_identities() -> None:
    manifest_payload = _read_json("manifest.json")
    report_payload = _read_json("report.json")
    manifest = EnduranceGateManifest.model_validate(manifest_payload)
    report = EnduranceGateReport.model_validate(report_payload)

    assert manifest.manifest_sha256 == _MANIFEST_SHA256
    assert manifest.gate_id == _GATE_ID
    assert report.report_sha256 == _REPORT_SHA256
    assert report.manifest == manifest
    assert report.disposition is EnduranceGateDisposition.BLOCKED
    assert report.blockers == ("structural_pivots:minimum_not_met:0/1",)
    assert report.checkpoint_chain_sha256 == _CHAIN_SHA256
    assert report.checkpoint_count == 73
    assert report.elapsed_seconds == 268393
    assert report.negative_result_count == 1
    assert report.reproduction_count == 1
    assert report.process_kill_count == 1
    assert report.provider_interruption_count == 1
    assert report.structural_pivot_count == 0
    assert report.portfolio_epoch_count == 1
    assert report.real_72h_passed is False
    assert report.eligible_for_f11_scientific_exit_review is False


def test_checkpoint_projection_recalculates_chain_and_durable_references() -> None:
    manifest = EnduranceGateManifest.model_validate(_read_json("manifest.json"))
    report = EnduranceGateReport.model_validate(_read_json("report.json"))
    projection = _read_json("checkpoint-identities.json")

    _assert_checkpoint_projection(projection, manifest, report)
    assert content_sha256(projection) == _IDENTITY_PROJECTION_SHA256


def test_checkpoint_projection_detects_content_and_link_tampering() -> None:
    manifest = EnduranceGateManifest.model_validate(_read_json("manifest.json"))
    report = EnduranceGateReport.model_validate(_read_json("report.json"))
    projection = _read_json("checkpoint-identities.json")

    broken_link = copy.deepcopy(projection)
    broken_link["checkpoints"][20]["parent_sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        _assert_checkpoint_projection(broken_link, manifest, report)

    changed_identity = copy.deepcopy(projection)
    changed_identity["checkpoints"][20]["checkpoint_sha256"] = "1" * 64
    with pytest.raises(AssertionError):
        _assert_checkpoint_projection(changed_identity, manifest, report)

    changed_report = _read_json("report.json")
    changed_report["elapsed_seconds"] += 1
    with pytest.raises(ValidationError):
        EnduranceGateReport.model_validate(changed_report)


def test_derived_interpretation_is_separate_source_bound_and_non_superseding() -> None:
    manifest = EnduranceGateManifest.model_validate(_read_json("manifest.json"))
    report = EnduranceGateReport.model_validate(_read_json("report.json"))
    projection = _read_json("checkpoint-identities.json")
    derived = _read_json("derived-interpretation.json")

    assert set(derived) == {
        "interpretation",
        "mutates_source",
        "schema",
        "schema_version",
        "source_binding",
        "status",
        "supersedes",
    }
    assert derived["schema"] == "aletheia.legacy.endurance_derived_interpretation"
    assert derived["schema_version"] == 1
    assert derived["status"] == "derived_non_authoritative"
    assert derived["supersedes"] is False
    assert derived["mutates_source"] is False
    assert "manifest" not in derived
    assert "report" not in derived
    assert derived["source_binding"] == {
        "checkpoint_chain_sha256": report.checkpoint_chain_sha256,
        "checkpoint_count": report.checkpoint_count,
        "checkpoint_identity_projection_sha256": content_sha256(projection),
        "gate_id": manifest.gate_id,
        "manifest_sha256": manifest.manifest_sha256,
        "report_sha256": report.report_sha256,
    }
    interpretation = derived["interpretation"]
    assert interpretation["terminal_disposition"] == report.disposition.value
    assert interpretation["scientific_exit_eligible"] is (
        report.eligible_for_f11_scientific_exit_review
    )
    assert report.blockers[0] in interpretation["claims"][-1]


def test_bundle_receipt_and_tracked_bytes_are_immutable() -> None:
    assert set(path.name for path in _FIXTURE_ROOT.iterdir()) == set(_FROZEN_FILE_SHA256S)
    for name, expected_sha256 in _FROZEN_FILE_SHA256S.items():
        assert _file_sha256(_FIXTURE_ROOT / name) == expected_sha256

    receipt = _read_json("bundle-receipt.json")
    assert set(receipt) == {
        "authoritative_objects",
        "authoritative_source_projections",
        "capture",
        "derived_objects",
        "gate_id",
        "schema",
        "schema_version",
    }
    assert receipt["schema"] == "aletheia.legacy.endurance_evidence_bundle_receipt"
    assert receipt["schema_version"] == 1
    assert receipt["gate_id"] == _GATE_ID
    assert receipt["capture"]["read_only"] is True
    assert receipt["capture"]["source_authority"] == "validating_postgresql_store"

    authoritative = receipt["authoritative_objects"]
    source_projections = receipt["authoritative_source_projections"]
    derived = receipt["derived_objects"]
    assert {item["file"] for item in authoritative} == {"manifest.json", "report.json"}
    assert len(source_projections) == 1
    checkpoint_projection = source_projections[0]
    assert checkpoint_projection == {
        "authority_limit": (
            "identities_and_durable_references_only_not_original_checkpoint_objects"
        ),
        "byte_length": (_FIXTURE_ROOT / "checkpoint-identities.json").stat().st_size,
        "can_recalculate_original_checkpoint_sha256": False,
        "classification": "authoritative_source_projection",
        "file": "checkpoint-identities.json",
        "file_sha256": _FROZEN_FILE_SHA256S["checkpoint-identities.json"],
        "identity_algorithm": "content_sha256",
        "object_sha256": _IDENTITY_PROJECTION_SHA256,
        "source_payloads_included": False,
    }
    assert "checkpoint-identities.json" not in {item["file"] for item in authoritative}

    objects = authoritative + source_projections + derived
    assert {item["file"] for item in objects} == set(_FROZEN_FILE_SHA256S) - {"bundle-receipt.json"}
    for item in objects:
        path = _FIXTURE_ROOT / item["file"]
        assert path.stat().st_size == item["byte_length"]
        assert _file_sha256(path) == item["file_sha256"]
        algorithm = item["identity_algorithm"]
        assert algorithm in {
            "EnduranceGateManifest.manifest_sha256",
            "EnduranceGateReport.report_sha256",
            "content_sha256",
        }
        if algorithm == "content_sha256":
            assert content_sha256(_read_json(item["file"])) == item["object_sha256"]

    receipt_by_file = {item["file"]: item for item in objects}
    manifest = EnduranceGateManifest.model_validate(_read_json("manifest.json"))
    report = EnduranceGateReport.model_validate(_read_json("report.json"))
    assert receipt_by_file["manifest.json"] == {
        "byte_length": (_FIXTURE_ROOT / "manifest.json").stat().st_size,
        "file": "manifest.json",
        "file_sha256": _FROZEN_FILE_SHA256S["manifest.json"],
        "identity_algorithm": "EnduranceGateManifest.manifest_sha256",
        "object_sha256": manifest.manifest_sha256,
    }
    assert receipt_by_file["report.json"] == {
        "byte_length": (_FIXTURE_ROOT / "report.json").stat().st_size,
        "file": "report.json",
        "file_sha256": _FROZEN_FILE_SHA256S["report.json"],
        "identity_algorithm": "EnduranceGateReport.report_sha256",
        "object_sha256": report.report_sha256,
    }


def test_bundle_contains_no_raw_operational_or_private_material() -> None:
    prohibited_keys = {
        "absolute_path",
        "api_key",
        "credential",
        "database_url",
        "password",
        "payload_json",
        "raw_payload",
        "secret",
        "transcript",
    }

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    for name in _FROZEN_FILE_SHA256S:
        payload = _read_json(name)
        assert not (keys(payload) & prohibited_keys)
        serialized = json.dumps(payload, sort_keys=True)
        assert "/Users/" not in serialized
        assert "postgresql://" not in serialized
        assert "postgresql+" not in serialized
