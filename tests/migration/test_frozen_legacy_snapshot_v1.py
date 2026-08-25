from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aletheia.migration.legacy import (
    LegacyFreezeRequest,
    LegacyImportReceipt,
    LegacySnapshotManifest,
    verify_legacy_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = ROOT / "tests/fixtures/legacy/v1"
SNAPSHOT_ROOT = LEGACY_ROOT / "snapshot"
STORE = SNAPSHOT_ROOT / "store"
EXPECTED_FILE_SHA256 = {
    "exporter.py": "c00d87ac4ac9b352114b4deab7546edc8366c2ff85f7a0489ae1bf84ad0801f8",
    "import-receipt.json": "311558860c64a0dee86e662b40e2175978d27fcb364f1dcc9c3844461456b3e8",
    "manifest.json": "bbc257707979c0277be618b9b032f6970b9a67d4c53e2479a64d7ad3cae8eaf5",
    "redaction-manifest.v1.json": (
        "71e24e7a127004526e855b2d9fc24398f95d809301db33377dc8f30a98763031"
    ),
    "request.json": "453373662ed9891cbc1019ff73d65bcb6eb9223f0215710cc120845c933ea359",
    "store/bindings/5b002fec7845491f65f4c776e0957eea595f6fc62437689fe9aaaaef0a761f0a.json": (
        "4eb2818510c60b97199b81a2b0b089059baf7d4b3f351bf5711d9f39bbaa9689"
    ),
    "store/manifests/134ee8f705cafb3f361719ec6429f6fe86e2a8f42feda40d3715bc722d044ecc.json": (
        "f33d30b20eb455c0866f97f59f9b1ec10b3cdf234f6d8e659e9d07e7857fa361"
    ),
    "store/objects/sha256/bb/bb1c3273e82969ae1d9ed4d3ccf593ee4a277914492cb9fb8a7232090cf55624": (
        "bb1c3273e82969ae1d9ed4d3ccf593ee4a277914492cb9fb8a7232090cf55624"
    ),
    "store/objects/sha256/f4/f4cc6bb4873744a4fae4eb6d243929ddd86c96891f6ccab8cb9825a6bf3c6c49": (
        "f4cc6bb4873744a4fae4eb6d243929ddd86c96891f6ccab8cb9825a6bf3c6c49"
    ),
}


def _json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_tracked_snapshot_bundle_file_set_and_bytes_are_frozen() -> None:
    actual = {
        path.relative_to(SNAPSHOT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SNAPSHOT_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual == EXPECTED_FILE_SHA256


def test_selected_legacy_records_are_real_cas_objects_with_one_version_binding() -> None:
    request = LegacyFreezeRequest.model_validate(_json(SNAPSHOT_ROOT / "request.json"))
    manifest = LegacySnapshotManifest.model_validate(_json(SNAPSHOT_ROOT / "manifest.json"))

    assert (request.source_system, request.source_scope, request.source_version) == (
        manifest.source_system,
        manifest.source_scope,
        manifest.source_version,
    )
    assert request.redaction_manifest_sha256 == manifest.redaction_manifest_sha256
    assert request.exporter_code_sha256 == manifest.exporter_code_sha256
    assert tuple(item.logical_name for item in request.objects) == tuple(
        item.logical_name for item in manifest.objects
    )
    for requested, frozen in zip(request.objects, manifest.objects, strict=True):
        assert requested.source_relative_path == frozen.source_relative_path
        assert requested.role == frozen.role
        assert requested.media_type == frozen.media_type
        assert requested.data_class == frozen.data_class
    assert manifest.snapshot_id == "lgs_134ee8f705cafb3f361719ec6429f6fe"
    assert manifest.snapshot_sha256 == (
        "134ee8f705cafb3f361719ec6429f6fe86e2a8f42feda40d3715bc722d044ecc"
    )
    verify_legacy_snapshot(manifest, snapshot_store=STORE)

    source_by_path = {
        "endurance/report.json": LEGACY_ROOT / "endurance/report.json",
        "run_projections.v1.json": LEGACY_ROOT / "run_projections.v1.json",
    }
    for item in manifest.objects:
        assert (STORE / item.cas_relative_uri).read_bytes() == source_by_path[
            item.source_relative_path
        ].read_bytes()


def test_snapshot_redaction_exporter_and_import_receipt_are_bound_and_non_authoritative() -> None:
    request = LegacyFreezeRequest.model_validate(_json(SNAPSHOT_ROOT / "request.json"))
    manifest = LegacySnapshotManifest.model_validate(_json(SNAPSHOT_ROOT / "manifest.json"))
    receipt = LegacyImportReceipt.model_validate(_json(SNAPSHOT_ROOT / "import-receipt.json"))
    redaction = _json(SNAPSHOT_ROOT / "redaction-manifest.v1.json")

    assert hashlib.sha256((SNAPSHOT_ROOT / "exporter.py").read_bytes()).hexdigest() == (
        manifest.exporter_entrypoint_sha256
    )
    assert (
        hashlib.sha256((SNAPSHOT_ROOT / "redaction-manifest.v1.json").read_bytes()).hexdigest()
        == manifest.redaction_manifest_sha256
    )
    assert redaction["review_status"] == "operator_reviewed"
    assert redaction["data_class"] == "internal_sanitized"
    redacted_objects = redaction["objects"]
    assert isinstance(redacted_objects, list) and redacted_objects
    assert {item["source_relative_path"] for item in redacted_objects} == {
        item.source_relative_path for item in request.objects
    }
    for item in redacted_objects:
        assert set(item) == {"source_relative_path", "retained", "excluded"}
        assert item["retained"] and item["excluded"]
    assert redaction["scientific_admission_allowed"] is False
    assert redaction["training_use_allowed"] is False
    assert redaction["live_refresh_allowed"] is False
    for source in manifest.freezer_identity.source_files:
        current_bytes = (ROOT / source.relative_path).read_bytes()
        assert len(current_bytes) == source.size_bytes
        assert hashlib.sha256(current_bytes).hexdigest() == source.sha256
    legacy_freezer_source = next(
        item
        for item in manifest.freezer_identity.source_files
        if item.relative_path == "aletheia/migration/legacy.py"
    )
    assert receipt.importer_code_sha256 == legacy_freezer_source.sha256
    assert receipt.snapshot_id == manifest.snapshot_id
    assert receipt.snapshot_sha256 == manifest.snapshot_sha256
    assert receipt.object_count == manifest.object_count
    assert receipt.total_bytes == manifest.total_bytes
    assert receipt.claim_ceiling == "engineering_regression_only"
    assert receipt.scientific_admission_allowed is False
    assert receipt.training_use_allowed is False
    assert receipt.live_refresh_allowed is False
    assert receipt.legacy_mutation_propagates is False
