from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from aletheia.reproducibility.manifest import content_sha256

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/legacy/v1/run_projections.v1.json"
SOURCE_FIXTURE = ROOT / "tests/fixtures/legacy/v1/run_projection_sources.v1.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_FILE_SHA256 = "bb1c3273e82969ae1d9ed4d3ccf593ee4a277914492cb9fb8a7232090cf55624"
EXPECTED_BUNDLE_SHA256 = "ef86995b3a07a2a05137d124c7e1696ac5262556125da443a6cfdb2be13653fd"
EXPECTED_SOURCE_FILE_SHA256 = "e1b5ec4847301e6299b5e87a76bc1cffba5aebd32a6a54cb44d59824909232f0"
EXPECTED_EXPORTER_FILE_SHA256 = "421b49bd9a5aa4e0ee5e9be3c85d8d7c84d6c7fe87ad94347af124e64c4a932a"


def _bundle() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sources() -> dict[str, Any]:
    return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_legacy_projections_are_content_addressed() -> None:
    assert _file_sha256(FIXTURE) == EXPECTED_FILE_SHA256
    bundle = _bundle()
    expected_bundle_sha = bundle.pop("bundle_sha256")
    assert expected_bundle_sha == EXPECTED_BUNDLE_SHA256
    assert content_sha256(bundle) == expected_bundle_sha

    run_ids: set[str] = set()
    for case in bundle["cases"]:
        expected_case_sha = case.pop("case_projection_sha256")
        assert content_sha256(case) == expected_case_sha
        assert SHA256.fullmatch(case["event_projection"]["event_type_sequence_sha256"])
        assert SHA256.fullmatch(case["artifact_projection"]["artifact_tree_sha256"])
        assert case["run_id"] not in run_ids
        run_ids.add(case["run_id"])


def test_sanitized_sources_recalculate_every_projection_summary_offline() -> None:
    assert _file_sha256(SOURCE_FIXTURE) == EXPECTED_SOURCE_FILE_SHA256
    sources = _sources()
    assert set(sources) == {
        "capture",
        "cases",
        "database_schema_revision",
        "exporter",
        "schema_name",
        "schema_version",
        "source_authority",
    }
    assert sources["schema_name"] == "aletheia.legacy_run_projection_sources"
    assert sources["schema_version"] == 1
    assert sources["source_authority"] == ("validating_postgresql_store_and_local_workspace")
    assert sources["capture"] == {
        "artifact_blob_content_included": False,
        "artifact_hashing": "streamed_opaque_bytes_sha256_without_deserialization",
        "event_fields_included": [
            "type",
            "event_key_presence",
            "event_sha256_presence",
        ],
        "event_payloads_included": False,
        "exported_on": "2026-08-23",
        "read_only": True,
    }

    exporter = sources["exporter"]
    assert set(exporter) == {"path", "sha256"}
    assert exporter == {
        "path": "scripts/export_legacy_run_projections.py",
        "sha256": EXPECTED_EXPORTER_FILE_SHA256,
    }
    exporter_path = (ROOT / exporter["path"]).resolve(strict=True)
    assert exporter_path.is_relative_to(ROOT.resolve())
    assert _file_sha256(exporter_path) == exporter["sha256"]

    projection = _bundle()
    assert sources["database_schema_revision"] == projection["database_schema_revision"]
    assert sources["source_authority"] == projection["source_authority"]
    source_cases = {case["run_id"]: case for case in sources["cases"]}
    projection_cases = {case["run_id"]: case for case in projection["cases"]}
    assert len(source_cases) == len(sources["cases"])
    assert set(source_cases) == set(projection_cases)

    allowed_roles = {
        "opaque_model_blob",
        "plot",
        "rendered_report",
        "structured_result",
    }
    for run_id, case in projection_cases.items():
        source = source_cases[run_id]
        assert set(source) == {
            "artifact_source",
            "domain",
            "event_source",
            "run_id",
            "terminal_status",
        }
        assert source["domain"] == case["domain"]
        assert source["terminal_status"] == case["terminal_status"]

        event_source = source["event_source"]
        assert set(event_source) == {
            "event_key_present_count",
            "event_sha256_present_count",
            "event_types",
            "ordering",
        }
        assert event_source["ordering"] == "events.id_ascending"
        assert event_source["event_key_present_count"] == 0
        assert event_source["event_sha256_present_count"] == 0
        event_types = event_source["event_types"]
        assert event_types
        assert all(isinstance(event_type, str) and event_type for event_type in event_types)
        events = case["event_projection"]
        assert len(event_types) == events["event_count"]
        assert dict(Counter(event_types)) == events["event_type_counts"]
        assert event_types[0] == events["first_event_type"]
        assert event_types[-1] == events["last_event_type"]
        assert (
            content_sha256({"event_types": event_types}) == (events["event_type_sequence_sha256"])
        )
        assert events["all_source_rows_unkeyed_unhashed"] is True

        artifact_source = source["artifact_source"]
        assert set(artifact_source) == {"excluded_patterns", "objects"}
        artifacts = case["artifact_projection"]
        assert artifact_source["excluded_patterns"] == artifacts["excluded_patterns"]
        objects = artifact_source["objects"]
        paths: list[str] = []
        for item in objects:
            assert set(item) == {"relative_path", "role", "sha256", "size_bytes"}
            relative = PurePosixPath(item["relative_path"])
            assert not relative.is_absolute()
            assert ".." not in relative.parts
            assert relative.as_posix() == item["relative_path"]
            assert item["role"] in allowed_roles
            assert isinstance(item["size_bytes"], int) and item["size_bytes"] >= 0
            assert SHA256.fullmatch(item["sha256"])
            paths.append(item["relative_path"])
        assert paths == sorted(set(paths))
        assert len(objects) == artifacts["artifact_count"]
        assert Counter(item["role"] for item in objects) == artifacts["artifact_role_counts"]
        assert sum(item["size_bytes"] for item in objects) == artifacts["total_bytes"]
        assert content_sha256({"objects": objects}) == artifacts["artifact_tree_sha256"]


def test_projection_covers_distinct_domains_and_negative_outcomes() -> None:
    cases = _bundle()["cases"]
    outcomes = Counter((case["domain"], case["terminal_status"]) for case in cases)

    assert outcomes[("materials", "completed")] >= 1
    assert outcomes[("molecules", "completed")] >= 1
    assert outcomes[("molecules", "results_rejected")] >= 1
    assert outcomes[("rag", "completed")] >= 1
    assert outcomes[("rag", "results_rejected")] >= 1


def test_projection_preserves_legacy_identity_limits_and_excludes_unsafe_payloads() -> None:
    bundle = _bundle()
    assert bundle["capture_policy"] == {
        "artifact_payloads_included": False,
        "claim_ceiling": "engineering_regression_only",
        "event_payloads_included": False,
        "live_refresh": False,
        "refresh_policy": "publish_new_explicit_version",
        "scientific_admission": False,
        "training_use": False,
    }
    for case in bundle["cases"]:
        events = case["event_projection"]
        assert events["legacy_identity_class"] == "unkeyed_unhashed"
        assert events["all_source_rows_unkeyed_unhashed"] is True
        assert sum(events["event_type_counts"].values()) == events["event_count"]
        artifacts = case["artifact_projection"]
        assert sum(artifacts["artifact_role_counts"].values()) == artifacts["artifact_count"]
        assert artifacts["payload_bytes_in_fixture"] is False
        assert artifacts["opaque_blobs_are_never_deserialized"] is True
        excluded = "\n".join(artifacts["excluded_patterns"])
        for unsafe in ("payload.json", "job.log", "transcript", "__pycache__", "*.pyc"):
            assert unsafe in excluded


def test_projection_is_offline_and_does_not_require_mutable_source_workspaces() -> None:
    # The migration gate consumes the tracked projection.  Live workspaces/DB are deliberately not
    # consulted here; a refresh is a separately reviewed fixture version.
    assert FIXTURE.is_file()
    assert "workspaces/" not in FIXTURE.read_text(encoding="utf-8")
