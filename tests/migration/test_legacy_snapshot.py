from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.migration.legacy import (
    LegacyDataClass,
    LegacyDataRole,
    LegacyFreezeRequest,
    LegacyFreezerIdentity,
    LegacyFreezerSourceFile,
    LegacyImportReceipt,
    LegacyObjectRole,
    LegacySnapshotInput,
    build_legacy_import_receipt,
    freeze_legacy_snapshot,
    legacy_exporter_code_sha256,
    verify_legacy_snapshot,
)

ZERO_SHA = "0" * 64
EXPORTER_COMMIT = "a" * 40
EXPORTER_TREE = "b" * 40
EXPORTER_ENTRYPOINT = "exporter.py"
EXPORTER_ENTRYPOINT_SHA = "c" * 64
EXPORTER_SHA = legacy_exporter_code_sha256(
    commit=EXPORTER_COMMIT,
    tree=EXPORTER_TREE,
    entrypoint=EXPORTER_ENTRYPOINT,
    entrypoint_sha256=EXPORTER_ENTRYPOINT_SHA,
)


def _request(*objects: LegacySnapshotInput) -> LegacyFreezeRequest:
    return LegacyFreezeRequest(
        source_system="legacy-test",
        source_scope="run/example",
        source_version="git/0123456789abcdef",
        redaction_manifest_sha256=ZERO_SHA,
        exporter_git_commit=EXPORTER_COMMIT,
        exporter_git_tree=EXPORTER_TREE,
        exporter_entrypoint=EXPORTER_ENTRYPOINT,
        exporter_entrypoint_sha256=EXPORTER_ENTRYPOINT_SHA,
        exporter_code_sha256=EXPORTER_SHA,
        objects=objects,
    )


def _input(logical_name: str, relative_path: str) -> LegacySnapshotInput:
    return LegacySnapshotInput(
        logical_name=logical_name,
        source_relative_path=relative_path,
        role=LegacyObjectRole.GOLDEN_CONTRACT,
        media_type="application/json",
        data_class=LegacyDataClass.DEV_FIXTURE,
    )


def test_snapshot_is_content_addressed_and_not_a_live_view(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "contract.json"
    source_file.write_text('{"status":"blocked"}\n', encoding="utf-8")
    store = tmp_path / "store"

    manifest = freeze_legacy_snapshot(
        _request(_input("terminal-contract", "contract.json")),
        source_root=source,
        snapshot_store=store,
    )
    frozen_object = store / manifest.objects[0].cas_relative_uri
    frozen_bytes = frozen_object.read_bytes()
    source_file.write_text('{"status":"passed"}\n', encoding="utf-8")

    verify_legacy_snapshot(manifest, snapshot_store=store)
    assert frozen_object.read_bytes() == frozen_bytes
    assert manifest.live_refresh_allowed is False
    assert manifest.legacy_mutation_propagates is False
    assert manifest.snapshot_id == f"lgs_{manifest.snapshot_sha256[:32]}"
    assert manifest.exporter_execution_assurance == "operator_attested"
    assert manifest.freezer_identity.execution_assurance == (
        "runtime_source_bytes_hashed_before_freeze"
    )
    assert manifest.freezer_identity.entrypoint == "aletheia/migration/legacy.py"
    stored_manifest = json.loads(
        (store / "manifests" / f"{manifest.snapshot_sha256}.json").read_text(encoding="utf-8")
    )
    assert stored_manifest["snapshot_sha256"] == manifest.snapshot_sha256


def test_snapshot_verification_detects_cas_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    store = tmp_path / "store"
    manifest = freeze_legacy_snapshot(
        _request(_input("contract", "contract.json")),
        source_root=source,
        snapshot_store=store,
    )
    target = store / manifest.objects[0].cas_relative_uri
    target.chmod(0o640)
    target.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed verification"):
        verify_legacy_snapshot(manifest, snapshot_store=store)


def test_declared_source_version_cannot_be_silently_rebound(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "contract.json"
    source_file.write_text('{"version":1}\n', encoding="utf-8")
    store = tmp_path / "store"
    request = _request(_input("contract", "contract.json"))
    freeze_legacy_snapshot(request, source_root=source, snapshot_store=store)
    source_file.write_text('{"version":2}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="collision or corruption"):
        freeze_legacy_snapshot(request, source_root=source, snapshot_store=store)


def test_same_request_retry_and_concurrent_first_writers_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text('{"version":1}\n', encoding="utf-8")
    store = tmp_path / "store"
    request = _request(_input("contract", "contract.json"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        manifests = tuple(
            executor.map(
                lambda _: freeze_legacy_snapshot(
                    request,
                    source_root=source,
                    snapshot_store=store,
                ),
                range(2),
            )
        )

    assert manifests[0] == manifests[1]
    assert freeze_legacy_snapshot(request, source_root=source, snapshot_store=store) == manifests[0]
    assert len(tuple((store / "objects" / "sha256").glob("*/*"))) == 1
    assert len(tuple((store / "manifests").glob("*.json"))) == 1
    assert len(tuple((store / "bindings").glob("*.json"))) == 1


def test_snapshot_verification_detects_version_binding_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    store = tmp_path / "store"
    manifest = freeze_legacy_snapshot(
        _request(_input("contract", "contract.json")),
        source_root=source,
        snapshot_store=store,
    )
    binding = next((store / "bindings").glob("*.json"))
    binding.chmod(0o640)
    binding.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="collision or corruption"):
        verify_legacy_snapshot(manifest, snapshot_store=store)


def test_freeze_revalidates_and_rejects_tampered_freezer_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    request = _request(_input("contract", "contract.json"))
    first = freeze_legacy_snapshot(
        request,
        source_root=source,
        snapshot_store=tmp_path / "first-store",
    )
    tampered = first.freezer_identity.model_copy(update={"code_sha256": "f" * 64})

    with pytest.raises(ValidationError, match="freezer code hash"):
        freeze_legacy_snapshot(
            request,
            source_root=source,
            snapshot_store=tmp_path / "second-store",
            freezer_identity=tampered,
        )

    forged = LegacyFreezerIdentity(
        entrypoint="not-real.py",
        source_files=(
            LegacyFreezerSourceFile(
                relative_path="not-real.py",
                sha256="f" * 64,
                size_bytes=123,
            ),
        ),
    )
    with pytest.raises(FileNotFoundError):
        freeze_legacy_snapshot(
            request,
            source_root=source,
            snapshot_store=tmp_path / "third-store",
            freezer_identity=forged,
            freezer_source_root=tmp_path,
        )


def test_explicit_freezer_identity_requires_source_reverification(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    first = freeze_legacy_snapshot(
        _request(_input("contract", "contract.json")),
        source_root=source,
        snapshot_store=tmp_path / "first-store",
    )

    with pytest.raises(ValueError, match="requires freezer_source_root"):
        freeze_legacy_snapshot(
            _request(_input("contract", "contract.json")),
            source_root=source,
            snapshot_store=tmp_path / "second-store",
            freezer_identity=first.freezer_identity,
        )


def test_receipt_cannot_admit_legacy_payload_as_scientific_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    manifest = freeze_legacy_snapshot(
        _request(_input("contract", "contract.json")),
        source_root=source,
        snapshot_store=tmp_path / "store",
    )

    receipt = build_legacy_import_receipt(
        manifest,
        snapshot_store=tmp_path / "store",
        target_scope_id="quest/example",
        imported_by="migration/operator",
        importer_code_sha256="2" * 64,
        imported_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        data_role=LegacyDataRole.COMPATIBILITY_ONLY,
    )

    assert receipt.import_key is not None and receipt.import_key.startswith("lgk_")
    assert receipt.receipt_id == f"lgi_{receipt.receipt_sha256[:32]}"
    assert receipt.claim_ceiling == "engineering_regression_only"
    assert receipt.scientific_admission_allowed is False
    assert receipt.training_use_allowed is False
    assert receipt.live_refresh_allowed is False


def test_receipt_builder_rehashes_store_and_rejects_snapshot_id_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    store = tmp_path / "store"
    manifest = freeze_legacy_snapshot(
        _request(_input("contract", "contract.json")),
        source_root=source,
        snapshot_store=store,
    )
    target = store / manifest.objects[0].cas_relative_uri
    target.chmod(0o640)
    target.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed verification"):
        build_legacy_import_receipt(
            manifest,
            snapshot_store=store,
            target_scope_id="quest/example",
            imported_by="migration/operator",
            importer_code_sha256="2" * 64,
            imported_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )

    receipt_payload = {
        "snapshot_id": "lgs_" + "f" * 32,
        "snapshot_sha256": manifest.snapshot_sha256,
        "target_scope_id": "quest/example",
        "imported_by": "migration/operator",
        "importer_code_sha256": "2" * 64,
        "imported_at": datetime(2026, 8, 23, tzinfo=timezone.utc),
        "object_count": manifest.object_count,
        "total_bytes": manifest.total_bytes,
    }
    with pytest.raises(ValidationError, match="snapshot ID does not match"):
        LegacyImportReceipt.model_validate(receipt_payload)


def test_snapshot_rejects_traversal_noncanonical_order_and_obvious_credentials(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="normalized relative path"):
        _input("escape", "../secret.json")
    with pytest.raises(ValidationError, match="canonical order"):
        _request(_input("z", "z.json"), _input("a", "a.json"))

    source = tmp_path / "source"
    source.mkdir()
    (source / "id_rsa").write_text("not-even-a-real-key", encoding="utf-8")
    with pytest.raises(ValueError, match="credential-like path"):
        freeze_legacy_snapshot(
            _request(_input("credential", "id_rsa")),
            source_root=source,
            snapshot_store=tmp_path / "store",
        )


def test_snapshot_rejects_symlink_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (source / "linked.json").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes its root|symlinks"):
        freeze_legacy_snapshot(
            _request(_input("linked", "linked.json")),
            source_root=source,
            snapshot_store=tmp_path / "store",
        )


def test_snapshot_rejects_symlinked_store_components(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside-store"
    outside.mkdir()
    (store / "objects").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="traverses a symlink"):
        freeze_legacy_snapshot(
            _request(_input("contract", "contract.json")),
            source_root=source,
            snapshot_store=store,
        )
