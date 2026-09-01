from __future__ import annotations

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aletheia.execution.artifact_store as artifact_store_module
from aletheia.execution.artifact_store import (
    ArtifactQuarantineError,
    ArtifactStoreError,
    ArtifactStoreCorruption,
    ArtifactVerificationError,
    LocalArtifactStore,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    ExecutionEffectClass,
    ExecutionIntent,
    ExecutionResourceRequest,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    ExpectedArtifact,
    InfrastructureAttempt,
    ScientificReplicateKind,
    ScientificReplicateSlot,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
QUEST_ID = "qst_" + "a" * 32
RESOURCE_ID = "rsc_" + "b" * 32


def _expected(*, key: str = "raw", max_bytes: int = 1024) -> ExpectedArtifact:
    return ExpectedArtifact(
        artifact_key=key,
        role=ArtifactRole.RAW_OUTPUT,
        media_type="application/octet-stream",
        schema_sha256=H1,
        max_bytes=max_bytes,
        data_classification="research-internal",
        retention_policy_sha256=H2,
    )


def _intent(
    *,
    expected: tuple[ExpectedArtifact, ...] | None = None,
    artifact_quota_bytes: int = 4096,
) -> ExecutionIntent:
    expected = tuple(sorted(expected or (_expected(),), key=lambda item: item.artifact_key))
    slot = ScientificReplicateSlot(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id="work-order.local-artifact-test",
        work_order_node_id="node.local-artifact-test",
        work_order_node_sha256=H1,
        slot_count=1,
        slot_index=1,
        replicate_kind=ScientificReplicateKind.CONFIRMATION,
        preregistration_sha256=H2,
        randomization_seed_sha256=H3,
    )
    return ExecutionIntent(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id=slot.work_order_id,
        work_order_sha256=H4,
        work_order_node_id=slot.work_order_node_id,
        work_order_node_sha256=slot.work_order_node_sha256,
        capability_id="capability.local-test.v1",
        capability_manifest_sha256=H5,
        resource_catalog_sha256=H0,
        resource_request=ExecutionResourceRequest(
            accepted_resource_class_ids=(RESOURCE_ID,),
            cpu_cores=1,
            memory_bytes=1024,
            scratch_bytes=1024,
            wall_time_seconds=60,
            max_infrastructure_attempts=1,
            artifact_quota_bytes=artifact_quota_bytes,
        ),
        retry_policy=ExecutionRetryPolicy(
            mode=ExecutionRetryMode.NEVER,
            maximum_attempts_per_scientific_slot=1,
        ),
        replicate_slot=slot,
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        expected_artifacts=expected,
        environment_sha256=H1,
        command_sha256=H2,
        execution_parameters_sha256=H3,
        effect_class=ExecutionEffectClass.REPLAY_SAFE,
        authorized_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        tmp_path / "custody",
        verifier_principal_id="principal.central-artifact-verifier",
        object_store_id="store.local-test",
    )


def _quarantine(
    store: LocalArtifactStore,
    intent: ExecutionIntent,
    output: Path,
    artifact_paths: dict[str, str] | None = None,
) -> ArtifactManifest:
    return store.quarantine_outputs(
        intent=intent,
        output_root=output,
        artifact_paths=artifact_paths or {"raw": "result.bin"},
        produced_at=NOW + timedelta(seconds=10),
    )


def _cas_path(store: LocalArtifactStore, digest: str) -> Path:
    return store.root / "objects" / "sha256" / digest[:2] / digest


def _quarantine_path(store: LocalArtifactStore, qid: str) -> Path:
    digest = qid.removeprefix("qtn_")
    return store.root / "quarantine" / "objects" / digest[:2] / qid


def test_read_only_store_requires_frozen_layout_and_blocks_mutation(tmp_path: Path) -> None:
    with pytest.raises(ArtifactStoreError, match="must already exist"):
        LocalArtifactStore(tmp_path / "missing", read_only=True)

    writer = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"retained")
    manifest = _quarantine(writer, intent, output)
    receipts = writer.verify_manifest(intent=intent, manifest=manifest)
    for path in writer.root.rglob("*"):
        if path.is_dir():
            path.chmod(0o750)
        elif stat.S_IMODE(path.stat().st_mode) == 0o400:
            path.chmod(0o440)
    writer.root.chmod(0o750)
    reader = LocalArtifactStore(
        writer.root,
        verifier_principal_id=writer.verifier_principal_id,
        object_store_id=writer.object_store_id,
        read_only=True,
    )

    assert reader.read_only is True
    assert (
        stat.S_IMODE(_cas_path(writer, manifest.entries[0].content_sha256).stat().st_mode) == 0o440
    )
    assert reader.load_manifest(manifest_sha256=manifest.manifest_sha256) == manifest
    assert (
        reader.load_verified_receipt(verified_receipt_sha256=receipts[0].verified_receipt_sha256)
        == receipts[0]
    )
    with pytest.raises(ArtifactQuarantineError, match="read-only artifact custody"):
        _quarantine(reader, intent, output)
    with pytest.raises(ArtifactVerificationError, match="read-only artifact custody"):
        reader.verify_manifest(intent=intent, manifest=manifest)


def test_zero_byte_roundtrip_is_opaque_content_addressed_and_reverified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"")

    manifest = _quarantine(store, intent, output)
    receipts = store.verify_manifest(intent=intent, manifest=manifest)

    assert len(receipts) == 1
    entry = manifest.entries[0]
    receipt = receipts[0]
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    assert entry.content_sha256 == empty_sha256 and entry.bytes == 0
    assert entry.quarantine_ref.startswith("qtn_")
    assert "/" not in entry.quarantine_ref and "result.bin" not in entry.quarantine_ref
    assert receipt.final_object_ref == f"cas://sha256/{empty_sha256}"
    assert receipt.final_object_version == f"sha256:{empty_sha256}"
    assert _cas_path(store, empty_sha256).read_bytes() == b""
    assert (
        store.load_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)
        == receipt
    )
    assert (
        store.resolve_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)
        == receipt
    )
    assert store.load_verified_receipt(verified_receipt_sha256="f" * 64) is None


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_quarantine_rejects_symlink_hardlink_and_nonregular_output(
    tmp_path: Path, unsafe_kind: str
) -> None:
    store = _store(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    target = output / "result.bin"
    if unsafe_kind == "symlink":
        target.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)

    with pytest.raises(ArtifactQuarantineError, match="hard-linked|symlink|non-regular"):
        _quarantine(store, _intent(), output)


def test_quarantine_rejects_path_escape_undeclared_and_empty_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"result")
    (tmp_path / "outside.bin").write_bytes(b"outside")

    with pytest.raises(ArtifactQuarantineError, match="escape"):
        _quarantine(store, intent, output, {"raw": "../outside.bin"})

    (output / "undeclared.bin").write_bytes(b"undeclared")
    with pytest.raises(ArtifactQuarantineError, match="undeclared"):
        _quarantine(store, intent, output)
    (output / "undeclared.bin").unlink()
    (output / "empty").mkdir()
    with pytest.raises(ArtifactQuarantineError, match="empty"):
        _quarantine(store, intent, output)


def test_required_output_is_missing_unless_failure_retention_is_explicit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactQuarantineError, match="missing a required artifact"):
        store.quarantine_outputs(
            intent=intent,
            output_root=output,
            artifact_paths={},
            produced_at=NOW + timedelta(seconds=10),
        )

    retained = store.quarantine_outputs(
        intent=intent,
        output_root=output,
        artifact_paths={},
        produced_at=NOW + timedelta(seconds=10),
        allow_partial=True,
    )
    assert retained.entries == ()


def test_quarantine_enforces_per_artifact_and_aggregate_quotas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"12345")
    with pytest.raises(ArtifactQuarantineError, match="per-artifact"):
        _quarantine(store, _intent(expected=(_expected(max_bytes=4),)), output)

    (output / "result.bin").write_bytes(b"123")
    (output / "second.bin").write_bytes(b"456")
    expected = (_expected(key="raw", max_bytes=4), _expected(key="second", max_bytes=4))
    with pytest.raises(ArtifactQuarantineError, match="aggregate"):
        _quarantine(
            store,
            _intent(expected=expected, artifact_quota_bytes=5),
            output,
            {"raw": "result.bin", "second": "second.bin"},
        )


def test_quarantine_detects_same_size_mutation_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    source = output / "result.bin"
    source.write_bytes(b"before")
    original_read = artifact_store_module.os.read
    mutated = False

    def mutate_after_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, count)
        if chunk and not mutated:
            mutated = True
            source.write_bytes(b"after!")
        return chunk

    monkeypatch.setattr(artifact_store_module.os, "read", mutate_after_read)
    with pytest.raises(ArtifactQuarantineError, match="changed while"):
        _quarantine(store, _intent(), output)


def test_quarantine_detects_path_replacement_while_open_inode_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    source = output / "result.bin"
    source.write_bytes(b"before")
    original_read = artifact_store_module.os.read
    replaced = False

    def replace_path_after_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, count)
        if chunk and not replaced:
            replaced = True
            source.rename(output / "renamed.bin")
            source.write_bytes(b"after!")
        return chunk

    monkeypatch.setattr(artifact_store_module.os, "read", replace_path_after_read)
    with pytest.raises(ArtifactQuarantineError, match="changed while"):
        _quarantine(store, _intent(), output)
    assert not tuple((store.root / "quarantine" / "staging").iterdir())


def test_partial_quarantine_write_is_removed_and_retry_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"partial-write-injection")
    original_write = artifact_store_module.os.write
    write_calls = 0

    def fail_after_partial_write(descriptor: int, payload: memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return original_write(descriptor, payload[:3])
        if write_calls == 2:
            raise OSError("injected partial staging write")
        return original_write(descriptor, payload)

    monkeypatch.setattr(artifact_store_module.os, "write", fail_after_partial_write)
    with pytest.raises(ArtifactQuarantineError, match="could not be streamed safely"):
        _quarantine(store, intent, output)
    assert not tuple((store.root / "quarantine" / "staging").iterdir())
    assert not tuple(
        path for path in (store.root / "quarantine" / "objects").rglob("*") if path.is_file()
    )

    monkeypatch.setattr(artifact_store_module.os, "write", original_write)
    assert _quarantine(store, intent, output).entries[0].bytes == len(b"partial-write-injection")


def test_manifest_hash_or_quarantine_identity_tampering_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"trusted bytes")
    manifest = _quarantine(store, intent, output)
    entry = manifest.entries[0]
    tampered_entry = entry.model_copy(update={"content_sha256": "f" * 64})
    tampered = manifest.model_copy(update={"entries": (tampered_entry,)})

    with pytest.raises(ArtifactVerificationError, match="not bound"):
        store.verify_manifest(intent=intent, manifest=tampered)

    quarantine_path = _quarantine_path(store, entry.quarantine_ref)
    quarantine_path.unlink()
    quarantine_path.symlink_to(tmp_path / "outside")
    with pytest.raises(ArtifactVerificationError, match="missing or unsafe"):
        store.verify_manifest(intent=intent, manifest=manifest)


def test_preexisting_corrupt_cas_object_is_never_hidden_by_deduplication(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"expected")
    manifest = _quarantine(store, intent, output)
    digest = manifest.entries[0].content_sha256
    target = _cas_path(store, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"corrupt!")
    target.chmod(0o400)

    with pytest.raises(ArtifactStoreCorruption, match="hash changed|byte count"):
        store.verify_manifest(intent=intent, manifest=manifest)


def test_final_object_tampering_is_detected_when_receipt_is_loaded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"original")
    manifest = _quarantine(store, intent, output)
    receipt = store.verify_manifest(intent=intent, manifest=manifest)[0]
    target = _cas_path(store, manifest.entries[0].content_sha256)
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    target.chmod(0o400)

    with pytest.raises(ArtifactStoreCorruption, match="hash or byte count"):
        store.resolve_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)


def test_writable_custody_object_is_rejected_even_when_bytes_are_unchanged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"immutable")
    manifest = _quarantine(store, intent, output)
    entry = manifest.entries[0]

    quarantine_path = _quarantine_path(store, entry.quarantine_ref)
    quarantine_path.chmod(0o600)
    with pytest.raises(ArtifactVerificationError, match="immutable 0400"):
        store.verify_manifest(intent=intent, manifest=manifest)
    quarantine_path.chmod(0o400)

    receipt = store.verify_manifest(intent=intent, manifest=manifest)[0]
    receipt_path = (
        store.root
        / "receipts"
        / "sha256"
        / receipt.verified_receipt_sha256[:2]
        / f"{receipt.verified_receipt_sha256}.json"
    )
    receipt_path.chmod(0o600)
    with pytest.raises(ArtifactStoreCorruption, match="immutable 0400"):
        store.load_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)
    receipt_path.chmod(0o400)

    final_path = _cas_path(store, entry.content_sha256)
    final_path.chmod(0o600)
    with pytest.raises(ArtifactStoreCorruption, match="immutable 0400"):
        store.load_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)


def test_interrupted_conditional_publish_leaves_no_visible_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"publish me")
    manifest = _quarantine(store, intent, output)
    digest = manifest.entries[0].content_sha256
    original_link = artifact_store_module.os.link

    def fail_final_publish(src: str, dst: str, **kwargs: object) -> None:
        if dst == digest:
            raise OSError("injected CAS publish failure")
        original_link(src, dst, **kwargs)

    monkeypatch.setattr(artifact_store_module.os, "link", fail_final_publish)
    with pytest.raises(ArtifactVerificationError, match="conditionally publish"):
        store.verify_manifest(intent=intent, manifest=manifest)
    target = _cas_path(store, digest)
    assert not target.exists()
    assert not tuple(target.parent.glob("*.tmp"))

    monkeypatch.setattr(artifact_store_module.os, "link", original_link)
    assert (
        store.verify_manifest(intent=intent, manifest=manifest)[0].artifact == (manifest.entries[0])
    )


def test_concurrent_verification_converges_on_one_immutable_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"concurrent")
    manifest = _quarantine(store, intent, output)

    def verify(_: int) -> ArtifactVerifiedReceipt:
        return store.verify_manifest(intent=intent, manifest=manifest)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = tuple(pool.map(verify, range(16)))

    assert len({item.verified_receipt_sha256 for item in receipts}) == 1
    winner = receipts[0]
    assert all(item == winner for item in receipts)
    assert (
        store.load_verified_receipt(verified_receipt_sha256=winner.verified_receipt_sha256)
        == winner
    )


def test_same_content_quarantine_and_verification_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"repeatable")

    first_manifest = _quarantine(store, intent, output)
    second_manifest = _quarantine(store, intent, output)
    first_receipts = store.verify_manifest(intent=intent, manifest=first_manifest)
    second_receipts = store.verify_manifest(intent=intent, manifest=second_manifest)

    assert first_manifest == second_manifest
    assert first_receipts == second_receipts


def test_receipt_sidecar_tampering_is_detected_before_cas_is_trusted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.bin").write_bytes(b"sidecar")
    manifest = _quarantine(store, intent, output)
    receipt = store.verify_manifest(intent=intent, manifest=manifest)[0]
    receipt_path = (
        store.root
        / "receipts"
        / "sha256"
        / receipt.verified_receipt_sha256[:2]
        / f"{receipt.verified_receipt_sha256}.json"
    )
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(b"{}")
    receipt_path.chmod(0o400)

    with pytest.raises(ArtifactStoreCorruption, match="stored artifact receipt"):
        store.load_verified_receipt(verified_receipt_sha256=receipt.verified_receipt_sha256)
