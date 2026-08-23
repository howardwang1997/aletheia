from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aletheia.research_kernel.schemas import ResearchCharterVersion, canonical_json_bytes
from aletheia.research_store.cas import (
    FilesystemResearchArchive,
    ResearchArchiveCorruption,
    ResearchArchiveError,
)

_H = "a" * 64


def _charter() -> ResearchCharterVersion:
    return ResearchCharterVersion(
        quest_id="qst_" + "1" * 32,
        charter_id="charter:cas-test",
        version=1,
        mission="Test durable content custody",
        value_boundaries=("honesty",),
        included_scopes=("fixture",),
        allowed_action_classes=("characterize",),
        safety_policy_sha256=_H,
        ethics_policy_sha256=_H,
        license_policy_sha256=_H,
        privacy_policy_sha256=_H,
        egress_policy_sha256=_H,
        budget_policy_sha256=_H,
        approval_policy_sha256=_H,
        publication_policy_sha256=_H,
        amendment_principal_ids=("human:owner",),
        emergency_stop_principal_ids=("human:owner",),
        authorized_by_principal_id="human:owner",
        authority_receipt_sha256=_H,
        authorized_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def test_object_roundtrip_and_exact_retry_are_content_addressed(tmp_path: Path) -> None:
    archive = FilesystemResearchArchive(tmp_path / "cas")
    charter = _charter()

    first = archive.archive_object(charter)
    second = archive.archive_object(charter)
    loaded = archive.load_object(charter.object_ref)

    assert first == second == loaded.metadata
    assert loaded.payload == charter
    assert first.storage_key == f"sha256/{charter.object_sha256[:2]}/{charter.object_sha256}"
    target = archive.root / first.storage_key
    assert target.read_bytes() == canonical_json_bytes(charter)
    assert target.stat().st_mode & 0o777 == 0o400


def test_object_load_rejects_corruption_and_cross_identity(tmp_path: Path) -> None:
    archive = FilesystemResearchArchive(tmp_path / "cas")
    charter = _charter()
    metadata = archive.archive_object(charter)
    target = archive.root / metadata.storage_key
    target.chmod(0o600)
    target.write_bytes(b"{}")

    with pytest.raises(ResearchArchiveCorruption, match="hash changed"):
        archive.load_object(charter.object_ref)


def test_archive_rejects_symlink_root_and_symlink_object(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ResearchArchiveError, match="root cannot be a symlink"):
        FilesystemResearchArchive(linked_root)

    archive = FilesystemResearchArchive(tmp_path / "cas")
    charter = _charter()
    metadata = archive.archive_object(charter)
    target = archive.root / metadata.storage_key
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ResearchArchiveCorruption, match="missing or unsafe"):
        archive.load_object(charter.object_ref)


def test_snapshot_roundtrip_rehashes_declared_identity(tmp_path: Path) -> None:
    archive = FilesystemResearchArchive(tmp_path / "cas")
    payload = b'{"snapshot_schema_version":1}'
    digest = hashlib.sha256(payload).hexdigest()

    metadata = archive.archive_snapshot(
        quest_id="qst_" + "2" * 32,
        stream_version=3,
        snapshot_sha256=digest,
        payload=payload,
    )

    assert archive.load_snapshot(metadata) == payload
    with pytest.raises(ResearchArchiveError, match="declared content hash"):
        archive.archive_snapshot(
            quest_id=metadata.quest_id,
            stream_version=metadata.stream_version,
            snapshot_sha256="0" * 64,
            payload=payload,
        )


def test_failed_publish_never_exposes_a_partial_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = FilesystemResearchArchive(tmp_path / "cas")
    charter = _charter()
    target = archive.root / f"sha256/{charter.object_sha256[:2]}/{charter.object_sha256}"
    original_link = os.link

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise OSError("simulated publish interruption")

    monkeypatch.setattr(os, "link", fail_publish)
    with pytest.raises(ResearchArchiveError, match="publish staged bytes"):
        archive.archive_object(charter)
    assert not target.exists()

    monkeypatch.setattr(os, "link", original_link)
    metadata = archive.archive_object(charter)
    assert archive.load_object(charter.object_ref).metadata == metadata
