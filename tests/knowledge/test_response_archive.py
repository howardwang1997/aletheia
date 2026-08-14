from __future__ import annotations

import hashlib
import json
import stat

import pytest

import aletheia.knowledge as k
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256
from .f8s2_fixtures import build_search_plan, sha
from .test_schema_spike import _time


def _safe_payload(query_id: str = "query") -> bytes:
    return json.dumps(
        {
            "items": [
                {
                    "id": "record-1",
                    "title": "Metadata-only result",
                    "authors": ["Fixture Author"],
                    "year": 2024,
                }
            ],
            "next_cursor": None,
            "query_id": query_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _store(archive: k.ContentAddressedResponseArchive, payload: bytes):
    plan = build_search_plan()
    query = plan.queries[0]
    manifest = next(item for item in plan.adapters if item.source_id == query.source_id)
    return archive.store_response(
        payload=payload,
        media_type="application/json",
        manifest=manifest,
        query=query,
        request_sha256=sha("request"),
        received_at=_time("2024-12-30T00:00:00Z"),
    )


def test_response_archive_is_content_addressed_idempotent_and_read_only(tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "archive")
    payload = _safe_payload()
    first = _store(archive, payload)
    second = _store(archive, payload)

    assert first.relative_path == second.relative_path
    assert first.response_sha256 == hashlib.sha256(payload).hexdigest()
    assert archive.read_response(first) == payload
    target = archive.root / first.relative_path
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert not target.is_symlink()


@pytest.mark.parametrize(
    "payload, media_type",
    [
        (b'{"items":[],"abstract":"copyrighted text"}', "application/json"),
        (
            b'<feed xmlns="http://www.w3.org/2005/Atom"><summary>text</summary></feed>',
            "application/atom+xml",
        ),
    ],
)
def test_response_archive_rejects_text_bearing_fields(
    tmp_path, payload: bytes, media_type: str
) -> None:
    plan = build_search_plan()
    query = plan.queries[0]
    manifest = next(item for item in plan.adapters if item.source_id == query.source_id)
    if media_type != "application/json":
        raw = manifest.model_dump(mode="python")
        raw["media_types"] = ("application/json", media_type)
        manifest = k.ProviderAdapterManifest.model_validate(raw)
        query_raw = query.model_dump(mode="python")
        query_raw["adapter_manifest_sha256"] = manifest.manifest_sha256
        query = k.PlannedSearchQuery.model_validate(query_raw)
    archive = k.ContentAddressedResponseArchive(tmp_path / "archive")
    with pytest.raises(k.ResponsePolicyViolation, match="forbidden text-bearing fields"):
        archive.store_response(
            payload=payload,
            media_type=media_type,
            manifest=manifest,
            query=query,
            request_sha256=sha("forbidden-request"),
            received_at=_time("2024-12-30T00:00:00Z"),
        )
    assert not list((archive.root / "responses").rglob("*.response")) if (
        archive.root / "responses"
    ).exists() else True


def test_response_archive_detects_tampering_and_target_symlink(tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "archive")
    receipt = _store(archive, _safe_payload())
    target = archive.root / receipt.relative_path
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    with pytest.raises(k.ResponseArchiveCorruption, match="byte count changed"):
        archive.read_response(receipt)

    second_root = tmp_path / "second"
    second = k.ContentAddressedResponseArchive(second_root)
    payload = _safe_payload("symlink")
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"responses/{digest[:2]}/{digest[2:4]}/{digest}.response"
    link = second.root / relative
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    link.symlink_to(outside)
    with pytest.raises(k.ResponseArchiveCorruption, match="missing or unsafe"):
        _store(second, payload)


def test_response_archive_refuses_symlink_root_and_oversized_object(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(k.ResponseArchiveError, match="root cannot be a symlink"):
        k.ContentAddressedResponseArchive(linked)

    archive = k.ContentAddressedResponseArchive(tmp_path / "tiny", max_object_bytes=32)
    with pytest.raises(k.ResponseArchiveError, match="byte limit"):
        _store(archive, _safe_payload())


def test_canonical_ledger_round_trip_binds_object_identity(tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "archive")
    value = {"schema_version": 1, "failures": [], "state": "complete"}
    object_sha256 = content_sha256(value)
    receipt = archive.store_ledger(
        value=value,
        object_sha256=object_sha256,
        archived_at=_time("2024-12-30T00:00:00Z"),
    )
    assert archive.read_ledger(receipt) == canonical_json_bytes(value)
    assert receipt.object_sha256 == object_sha256
    assert receipt.ledger_sha256 == hashlib.sha256(canonical_json_bytes(value)).hexdigest()
