from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.input_materializer import (
    InputMaterializationError,
    LocalCASInputMaterializer,
)
from aletheia.execution.runtime_v2_contracts import (
    InputMaterializationReceipt,
    PinnedInputPath,
)
from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionEffectClass,
    ExecutionIntent,
    ExecutionResourceRequest,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    ExpectedArtifact,
    InfrastructureAttempt,
    InputArtifactBinding,
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
INPUT_PORT = "input.raw"
INPUT_BYTES = b"verified immutable input\x00with bytes"


class _Clock:
    def now(self) -> datetime:
        return NOW


def _producer_intent() -> ExecutionIntent:
    expected = ExpectedArtifact(
        artifact_key="raw",
        role=ArtifactRole.RAW_OUTPUT,
        media_type="application/octet-stream",
        schema_sha256=H1,
        max_bytes=4096,
        data_classification="research-internal",
        retention_policy_sha256=H2,
    )
    slot = ScientificReplicateSlot(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id="work-order.input-materializer-test",
        work_order_node_id="node.input-materializer-test",
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
        capability_id="capability.input-materializer-test.v1",
        capability_manifest_sha256=H5,
        resource_catalog_sha256=H0,
        resource_request=ExecutionResourceRequest(
            accepted_resource_class_ids=(RESOURCE_ID,),
            cpu_cores=1,
            memory_bytes=1024,
            scratch_bytes=1024,
            wall_time_seconds=60,
            max_infrastructure_attempts=1,
            artifact_quota_bytes=4096,
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
        expected_artifacts=(expected,),
        environment_sha256=H1,
        command_sha256=H2,
        execution_parameters_sha256=H3,
        effect_class=ExecutionEffectClass.REPLAY_SAFE,
        authorized_at=NOW - timedelta(minutes=5),
        deadline=NOW + timedelta(minutes=5),
    )


def _case(
    tmp_path: Path,
) -> tuple[LocalArtifactStore, ExecutionIntent, str, LocalCASInputMaterializer, Path]:
    producer = _producer_intent()
    output = tmp_path / "producer-output"
    output.mkdir()
    (output / "result.bin").write_bytes(INPUT_BYTES)
    store = LocalArtifactStore(
        tmp_path / "custody",
        verifier_principal_id="principal.input-materializer-verifier",
        object_store_id="store.input-materializer-test",
    )
    manifest = store.quarantine_outputs(
        intent=producer,
        output_root=output,
        artifact_paths={"raw": "result.bin"},
        produced_at=NOW - timedelta(minutes=1),
    )
    verified = store.verify_manifest(intent=producer, manifest=manifest)[0]
    binding = InputArtifactBinding(
        input_port_id=INPUT_PORT,
        source_kind="protocol_input",
        artifact_verified_receipt_sha256=verified.verified_receipt_sha256,
    )
    payload = producer.model_dump(mode="python")
    payload["input_artifact_bindings"] = (binding,)
    consumer = ExecutionIntent.model_validate(payload)
    materializer = LocalCASInputMaterializer(
        artifact_store=store,
        journal_root=tmp_path / "materializer-journal",
        path_pins=(PinnedInputPath(input_port_id=INPUT_PORT, relative_path="dataset/input.bin"),),
        materializer_principal_id="principal:input-materializer",
        clock=_Clock(),
    )
    destination = tmp_path / "attempt-input"
    destination.mkdir(mode=0o700)
    return store, consumer, verified.verified_receipt_sha256, materializer, destination


def _cas_path(store: LocalArtifactStore) -> Path:
    digest = hashlib.sha256(INPUT_BYTES).hexdigest()
    return store.root / "objects" / "sha256" / digest[:2] / digest


def test_stream_rehash_atomic_publish_and_typed_exact_replay(tmp_path: Path) -> None:
    store, intent, verified_sha256, materializer, destination = _case(tmp_path)

    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)

    assert isinstance(receipt, InputMaterializationReceipt)
    assert receipt.intent_sha256 == intent.intent_sha256
    assert receipt.materializer_principal_id == "principal:input-materializer"
    assert len(receipt.entries) == 1
    entry = receipt.entries[0]
    assert entry.input_port_id == INPUT_PORT
    assert entry.verified_receipt_sha256 == verified_sha256
    assert entry.relative_path == "dataset/input.bin"
    assert entry.content_sha256 == hashlib.sha256(INPUT_BYTES).hexdigest()
    assert entry.content_bytes == len(INPUT_BYTES)
    assert entry.read_only is True
    staged = destination / entry.relative_path
    assert staged.read_bytes() == INPUT_BYTES
    assert stat.S_IMODE(staged.stat().st_mode) == 0o400
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o500
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")) == [
        "dataset",
        "dataset/input.bin",
    ]
    assert os.stat(_cas_path(store)).st_ino != os.stat(staged).st_ino

    replay = materializer.ensure_verified_inputs(intent=intent, destination=destination)
    assert replay == receipt
    assert replay.materialization_receipt_sha256 == receipt.materialization_receipt_sha256
    assert (
        materializer.ensure_verified_inputs_sha256(intent=intent, destination=destination)
        == receipt.materialization_receipt_sha256
    )
    assert materializer.load_receipt(intent=intent, destination=destination) == receipt


def test_partial_copy_is_rehashed_and_completed_after_interruption(tmp_path: Path) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    partial_parent = destination / "dataset"
    partial_parent.mkdir(mode=0o700)
    partial = partial_parent / "input.bin"
    partial.write_bytes(INPUT_BYTES)
    partial.chmod(0o400)

    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)

    assert receipt.entries[0].staged_file_identity_sha256
    assert partial.read_bytes() == INPUT_BYTES
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500


@pytest.mark.parametrize("crash_point", ["before-link", "after-link-before-unlink"])
def test_random_temp_crash_residue_is_exactly_recovered(
    tmp_path: Path,
    crash_point: str,
) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    parent = destination / "dataset"
    parent.mkdir(mode=0o700)
    digest = hashlib.sha256(INPUT_BYTES).hexdigest()
    temporary = parent / f".aletheia-input-{digest}.{'a' * 32}.tmp"
    temporary.write_bytes(INPUT_BYTES)
    temporary.chmod(0o400)
    target = parent / "input.bin"
    if crash_point == "after-link-before-unlink":
        os.link(temporary, target)
        assert temporary.stat().st_nlink == 2

    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)

    assert not temporary.exists()
    assert target.read_bytes() == INPUT_BYTES
    assert target.stat().st_nlink == 1
    assert receipt.entries[0].relative_path == "dataset/input.bin"
    assert materializer.load_receipt(intent=intent, destination=destination) == receipt


def test_post_seal_receipt_replay_detects_redundant_chmod_ctime_change(
    tmp_path: Path,
) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)
    staged = destination / receipt.entries[0].relative_path
    before_ctime = staged.stat().st_ctime_ns

    staged.chmod(0o400)

    assert staged.stat().st_ctime_ns != before_ctime
    with pytest.raises(InputMaterializationError, match="identities"):
        materializer.load_receipt(intent=intent, destination=destination)


def test_receipt_prelink_orphan_is_removed_before_materialization(tmp_path: Path) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
    temporary = tmp_path / "materializer-journal" / f".{attempt_id}.input.json.{'b' * 32}.tmp"
    temporary.write_bytes(b"partial receipt bytes")
    temporary.chmod(0o600)

    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)

    assert not temporary.exists()
    assert materializer.load_receipt(intent=intent, destination=destination) == receipt


def test_receipt_postlink_preunlink_is_exactly_recovered(tmp_path: Path) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)
    attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
    receipt_path = tmp_path / "materializer-journal" / f"{attempt_id}.input.json"
    temporary = receipt_path.with_name(f".{receipt_path.name}.{'c' * 32}.tmp")
    os.link(receipt_path, temporary)
    assert receipt_path.stat().st_nlink == 2

    replay = materializer.ensure_verified_inputs(intent=intent, destination=destination)

    assert replay == receipt
    assert not temporary.exists()
    assert receipt_path.stat().st_nlink == 1


@pytest.mark.parametrize("unsafe_kind", ["extra", "symlink", "wrong-bytes", "writable"])
def test_destination_tree_tampering_fails_closed(tmp_path: Path, unsafe_kind: str) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    if unsafe_kind == "extra":
        (destination / "undeclared.bin").write_bytes(b"extra")
    elif unsafe_kind == "symlink":
        (tmp_path / "outside.bin").write_bytes(INPUT_BYTES)
        (destination / "dataset").mkdir()
        (destination / "dataset" / "input.bin").symlink_to(tmp_path / "outside.bin")
    else:
        (destination / "dataset").mkdir()
        staged = destination / "dataset" / "input.bin"
        staged.write_bytes(b"wrong" if unsafe_kind == "wrong-bytes" else INPUT_BYTES)
        staged.chmod(0o400 if unsafe_kind == "wrong-bytes" else 0o600)

    with pytest.raises(InputMaterializationError):
        materializer.ensure_verified_inputs(intent=intent, destination=destination)


def test_fresh_cas_and_staged_identity_revalidation_detects_mutation(tmp_path: Path) -> None:
    store, intent, _, materializer, destination = _case(tmp_path)
    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)
    cas = _cas_path(store)
    cas.chmod(0o600)
    cas.write_bytes(b"X" * len(INPUT_BYTES))
    cas.chmod(0o400)

    with pytest.raises(InputMaterializationError, match="fresh CAS|custody"):
        materializer.load_receipt(intent=intent, destination=destination)

    cas.chmod(0o600)
    cas.write_bytes(INPUT_BYTES)
    cas.chmod(0o400)
    staged = destination / receipt.entries[0].relative_path
    staged.chmod(0o600)
    staged.write_bytes(b"Y" * len(INPUT_BYTES))
    staged.chmod(0o400)
    with pytest.raises(InputMaterializationError, match="differs|identities"):
        materializer.load_receipt(intent=intent, destination=destination)


def test_port_path_set_is_exact_and_journal_is_outside_mount(tmp_path: Path) -> None:
    store, intent, _, _, destination = _case(tmp_path)
    missing = LocalCASInputMaterializer(
        artifact_store=store,
        journal_root=tmp_path / "missing-pin-journal",
        path_pins=(),
        clock=_Clock(),
    )
    with pytest.raises(InputMaterializationError, match="pins differ"):
        missing.ensure_verified_inputs(intent=intent, destination=destination)

    extra = LocalCASInputMaterializer(
        artifact_store=store,
        journal_root=tmp_path / "extra-pin-journal",
        path_pins=(
            PinnedInputPath(input_port_id="input.extra", relative_path="extra.bin"),
            PinnedInputPath(input_port_id=INPUT_PORT, relative_path="dataset/input.bin"),
        ),
        clock=_Clock(),
    )
    with pytest.raises(InputMaterializationError, match="pins differ"):
        extra.ensure_verified_inputs(intent=intent, destination=destination)


def test_receipt_custody_tampering_is_not_regenerated(tmp_path: Path) -> None:
    _, intent, _, materializer, destination = _case(tmp_path)
    receipt = materializer.ensure_verified_inputs(intent=intent, destination=destination)
    journal = tmp_path / "materializer-journal"
    receipt_path = journal / (
        f"{intent.infrastructure_attempt.infrastructure_attempt_id}.input.json"
    )
    assert receipt_path.read_bytes()
    receipt_path.chmod(0o600)

    with pytest.raises(InputMaterializationError, match="metadata"):
        materializer.ensure_verified_inputs(intent=intent, destination=destination)
    assert receipt.materialization_receipt_sha256
