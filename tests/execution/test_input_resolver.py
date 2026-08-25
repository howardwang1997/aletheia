from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.input_resolver import (
    InputArtifactResolutionError,
    LocalVerifiedInputArtifactResolver,
)
from aletheia.execution.ports import ArchivedExecutionTerminalReceipt
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    ExecutionEffectClass,
    ExecutionFailure,
    ExecutionFailureCategory,
    ExecutionIntent,
    ExecutionReceipt,
    ExecutionResourceRequest,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    ExecutionTerminalState,
    ExpectedArtifact,
    InfrastructureAttempt,
    ScientificReplicateKind,
    ScientificReplicateSlot,
    canonical_sha256,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
QUEST_ID = "qst_" + "a" * 32
RESOURCE_ID = "rsc_" + "b" * 32


class _Archive:
    def __init__(self, rows: tuple[ArchivedExecutionTerminalReceipt, ...] = ()) -> None:
        self.rows = rows
        self.requested_attempt_id: str | None = None

    def list_terminal_receipts_for_attempt(
        self, *, infrastructure_attempt_id: str
    ) -> tuple[ArchivedExecutionTerminalReceipt, ...]:
        self.requested_attempt_id = infrastructure_attempt_id
        return self.rows


def _intent(now: datetime) -> ExecutionIntent:
    expected = ExpectedArtifact(
        artifact_key="raw",
        role=ArtifactRole.RAW_OUTPUT,
        media_type="application/octet-stream",
        schema_sha256=H1,
        max_bytes=1024,
        data_classification="research-internal",
        retention_policy_sha256=H2,
    )
    slot = ScientificReplicateSlot(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id="work-order.input-resolution-test",
        work_order_node_id="node.input-resolution-test",
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
        capability_id="capability.input-resolution-test.v1",
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
        authorized_at=now,
        deadline=now + timedelta(hours=1),
    )


def _custody(
    tmp_path: Path,
) -> tuple[
    LocalArtifactStore,
    ExecutionIntent,
    ArtifactManifest,
    ArtifactVerifiedReceipt,
]:
    now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    intent = _intent(now)
    output = tmp_path / "output"
    output.mkdir(parents=True)
    (output / "result.bin").write_bytes(b"immutable input bytes")
    store = LocalArtifactStore(
        tmp_path / "custody",
        verifier_principal_id="principal.input-artifact-verifier",
        object_store_id="store.input-resolution-test",
    )
    manifest = store.quarantine_outputs(
        intent=intent,
        output_root=output,
        artifact_paths={"raw": "result.bin"},
        produced_at=now + timedelta(minutes=1),
    )
    verified = store.verify_manifest(intent=intent, manifest=manifest)[0]
    return store, intent, manifest, verified


def _execution_receipt(
    *,
    intent: ExecutionIntent,
    manifest: ArtifactManifest,
    verified: ArtifactVerifiedReceipt,
    succeeded: bool = True,
) -> ExecutionReceipt:
    terminal_state = (
        ExecutionTerminalState.ENGINEERING_SUCCEEDED
        if succeeded
        else ExecutionTerminalState.EXECUTION_FAILED
    )
    failure = None
    if not succeeded:
        failure = ExecutionFailure(
            category=ExecutionFailureCategory.INFRASTRUCTURE,
            detail_sha256=H4,
        )
    node_receipt_json = _node_receipt_json(intent)
    return ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=H0,
        node_inventory_sha256=H1,
        resource_lease_sha256=H2,
        node_execution_receipt_sha256=canonical_sha256(node_receipt_json),
        started_at=intent.authorized_at,
        ended_at=verified.verified_at,
        observed_at=verified.verified_at,
        terminal_state=terminal_state,
        failure=failure,
        artifact_manifest=manifest,
        artifact_verified_receipts=(verified,),
        verified_by_principal_id="execution-receipt-verifier",
        verified_at=verified.verified_at + timedelta(seconds=1),
    )


def _node_receipt_json(intent: ExecutionIntent) -> dict[str, object]:
    return {
        "schema_name": "aletheia.test_node_execution_receipt",
        "intent_sha256": intent.intent_sha256,
        "execution_id": intent.execution_id,
        "infrastructure_attempt_id": (intent.infrastructure_attempt.infrastructure_attempt_id),
    }


def _row(receipt: ExecutionReceipt) -> ArchivedExecutionTerminalReceipt:
    manifest = receipt.artifact_manifest
    assert manifest is not None
    policy_sha256 = H4
    key_id = H5
    principal_id = receipt.verified_by_principal_id
    node_receipt_json = _node_receipt_json(receipt.intent)
    terminal_pin_json = {
        "policy_sha256": policy_sha256,
        "key_id": key_id,
        "principal_id": principal_id,
    }
    terminal_attestation_json = {
        "signature_ed25519_hex": "0" * 128,
        "message": {
            "execution_receipt_sha256": receipt.execution_receipt_sha256,
            "node_execution_receipt_sha256": receipt.node_execution_receipt_sha256,
            "terminal_state": receipt.terminal_state.value,
            "terminal_verification_policy_sha256": policy_sha256,
            "verification_key_id": key_id,
            "verified_by_principal_id": principal_id,
        },
    }
    return ArchivedExecutionTerminalReceipt(
        receipt_sha256=receipt.execution_receipt_sha256,
        attempt_id=receipt.intent.infrastructure_attempt.infrastructure_attempt_id,
        execution_id=receipt.intent.execution_id,
        intent_sha256=receipt.intent.intent_sha256,
        resource_lease_sha256=receipt.resource_lease_sha256,
        terminal_state=receipt.terminal_state,
        payload_sha256=receipt.execution_receipt_sha256,
        receipt=receipt,
        node_execution_receipt_sha256=receipt.node_execution_receipt_sha256,
        node_execution_receipt_json=node_receipt_json,
        terminal_verification_attestation_sha256=canonical_sha256(terminal_attestation_json),
        terminal_verification_attestation_json=terminal_attestation_json,
        terminal_verification_authority_pin_sha256=canonical_sha256(terminal_pin_json),
        terminal_verification_authority_pin_json=terminal_pin_json,
        terminal_verification_policy_sha256=policy_sha256,
        terminal_verification_key_id=key_id,
        committed_by_principal_id=principal_id,
        artifact_manifest_sha256=manifest.manifest_sha256,
        artifact_verified_receipt_sha256s=tuple(
            item.verified_receipt_sha256 for item in receipt.artifact_verified_receipts
        ),
        committed_at=receipt.verified_at,
    )


def _resolver(store: LocalArtifactStore, archive: _Archive) -> LocalVerifiedInputArtifactResolver:
    return LocalVerifiedInputArtifactResolver(
        artifact_store=store,
        terminal_receipt_archive=archive,
        resolver_principal_id="principal.input-artifact-resolver",
    )


def test_archived_protocol_input_resolves_without_claiming_a_producer(tmp_path: Path) -> None:
    store, intent, manifest, verified = _custody(tmp_path)
    archive = _Archive()
    observed_at = verified.verified_at + timedelta(seconds=2)

    resolution = _resolver(store, archive).resolve_verified_input_artifact(
        verified_receipt_sha256=verified.verified_receipt_sha256,
        observed_at=observed_at,
    )

    assert resolution is not None
    assert resolution.verified_receipt == verified
    assert resolution.artifact_manifest == manifest
    assert resolution.producer_execution_receipt is None
    assert resolution.resolved_at == observed_at
    assert archive.requested_attempt_id == (intent.infrastructure_attempt.infrastructure_attempt_id)


def test_work_order_output_resolves_exact_successful_terminal_row(tmp_path: Path) -> None:
    store, intent, manifest, verified = _custody(tmp_path)
    producer = _execution_receipt(intent=intent, manifest=manifest, verified=verified)
    observed_at = producer.verified_at + timedelta(seconds=1)

    resolution = _resolver(store, _Archive((_row(producer),))).resolve_verified_input_artifact(
        verified_receipt_sha256=verified.verified_receipt_sha256,
        observed_at=observed_at,
    )

    assert resolution is not None
    assert resolution.producer_execution_receipt == producer
    assert resolution.content_rehash_sha256 == verified.artifact.content_sha256
    assert resolution.content_bytes == verified.artifact.bytes


def test_complete_manifest_can_be_freshly_resolved_without_an_avr(tmp_path: Path) -> None:
    store, _intent_value, manifest, verified = _custody(tmp_path)
    resolver = _resolver(store, _Archive())
    observed_at = verified.verified_at + timedelta(seconds=1)

    assert (
        resolver.resolve_artifact_manifest(
            manifest_sha256=manifest.manifest_sha256,
            observed_at=observed_at,
        )
        == manifest
    )
    assert (
        resolver.resolve_artifact_manifest(
            manifest_sha256=H5,
            observed_at=observed_at,
        )
        is None
    )


def test_manifest_resolution_rejects_non_utc_time_and_cas_tamper(tmp_path: Path) -> None:
    store, _intent_value, manifest, verified = _custody(tmp_path)
    resolver = _resolver(store, _Archive())

    with pytest.raises(InputArtifactResolutionError, match="timezone-aware UTC"):
        resolver.resolve_artifact_manifest(
            manifest_sha256=manifest.manifest_sha256,
            observed_at=datetime.now(),
        )

    cas_path = (
        store.root
        / "objects"
        / "sha256"
        / verified.artifact.content_sha256[:2]
        / verified.artifact.content_sha256
    )
    cas_path.chmod(0o600)
    cas_path.write_bytes(b"tampered manifest object")
    cas_path.chmod(0o400)
    with pytest.raises(InputArtifactResolutionError, match="fresh local revalidation"):
        resolver.resolve_artifact_manifest(
            manifest_sha256=manifest.manifest_sha256,
            observed_at=verified.verified_at + timedelta(seconds=1),
        )


def test_missing_or_tampered_manifest_sidecar_fails_closed(tmp_path: Path) -> None:
    store, _intent_value, manifest, verified = _custody(tmp_path)
    manifest_path = (
        store.root
        / "manifests"
        / "sha256"
        / manifest.manifest_sha256[:2]
        / f"{manifest.manifest_sha256}.json"
    )
    manifest_path.unlink()

    with pytest.raises(InputArtifactResolutionError, match="manifest sidecar"):
        _resolver(store, _Archive()).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=verified.verified_at + timedelta(seconds=1),
        )

    manifest_path.write_bytes(b"{}")
    manifest_path.chmod(0o400)
    with pytest.raises(InputArtifactResolutionError, match="fresh local revalidation"):
        _resolver(store, _Archive()).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=verified.verified_at + timedelta(seconds=1),
        )


def test_receipt_sidecar_or_cas_tampering_fails_before_resolution(tmp_path: Path) -> None:
    store, _intent_value, _manifest, verified = _custody(tmp_path)
    receipt_path = (
        store.root
        / "receipts"
        / "sha256"
        / verified.verified_receipt_sha256[:2]
        / f"{verified.verified_receipt_sha256}.json"
    )
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(b"{}")
    receipt_path.chmod(0o400)
    observed_at = verified.verified_at + timedelta(seconds=1)
    with pytest.raises(InputArtifactResolutionError, match="fresh local revalidation"):
        _resolver(store, _Archive()).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=observed_at,
        )

    store, _intent_value, _manifest, verified = _custody(tmp_path / "cas-case")
    cas_path = (
        store.root
        / "objects"
        / "sha256"
        / verified.artifact.content_sha256[:2]
        / verified.artifact.content_sha256
    )
    cas_path.chmod(0o600)
    cas_path.write_bytes(b"tampered input bytes")
    cas_path.chmod(0o400)
    with pytest.raises(InputArtifactResolutionError, match="fresh local revalidation"):
        _resolver(store, _Archive()).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=verified.verified_at + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"payload_sha256": H5},
        {"resource_lease_sha256": H5},
        {"node_execution_receipt_sha256": H5},
        {"terminal_verification_attestation_sha256": H5},
        {"terminal_verification_authority_pin_sha256": H5},
        {"terminal_verification_policy_sha256": H0},
        {"terminal_verification_key_id": H0},
        {"committed_by_principal_id": "principal.forged-terminal-verifier"},
        {"artifact_manifest_sha256": H5},
        {"artifact_verified_receipt_sha256s": (H5,)},
        {"attempt_id": "iat_" + "f" * 32},
        {"terminal_state": ExecutionTerminalState.EXECUTION_FAILED},
    ],
)
def test_inconsistent_terminal_archive_row_fails_closed(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    store, intent, manifest, verified = _custody(tmp_path)
    producer = _execution_receipt(intent=intent, manifest=manifest, verified=verified)
    inconsistent = replace(_row(producer), **mutation)

    with pytest.raises(InputArtifactResolutionError, match="exact immutable successful"):
        _resolver(store, _Archive((inconsistent,))).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=producer.verified_at + timedelta(seconds=1),
        )


def test_failed_or_duplicate_producer_lineage_fails_closed(tmp_path: Path) -> None:
    store, intent, manifest, verified = _custody(tmp_path)
    failed = _execution_receipt(
        intent=intent,
        manifest=manifest,
        verified=verified,
        succeeded=False,
    )
    observed_at = failed.verified_at + timedelta(seconds=1)

    with pytest.raises(InputArtifactResolutionError, match="exact immutable successful"):
        _resolver(store, _Archive((_row(failed),))).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=observed_at,
        )

    success = _execution_receipt(intent=intent, manifest=manifest, verified=verified)
    row = _row(success)
    with pytest.raises(InputArtifactResolutionError, match="ambiguous"):
        _resolver(store, _Archive((row, row))).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=success.verified_at + timedelta(seconds=1),
        )


def test_producer_receipt_must_precede_database_commit_time(tmp_path: Path) -> None:
    store, intent, manifest, verified = _custody(tmp_path)
    producer = _execution_receipt(intent=intent, manifest=manifest, verified=verified)
    backdated_commit = replace(
        _row(producer),
        committed_at=producer.verified_at - timedelta(microseconds=1),
    )

    with pytest.raises(InputArtifactResolutionError, match="exact immutable successful"):
        _resolver(store, _Archive((backdated_commit,))).resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=producer.verified_at + timedelta(seconds=1),
        )


def test_resolution_requires_allocator_utc_observation_and_current_avr(tmp_path: Path) -> None:
    store, _intent_value, _manifest, verified = _custody(tmp_path)
    resolver = _resolver(store, _Archive())

    with pytest.raises(InputArtifactResolutionError, match="timezone-aware UTC"):
        resolver.resolve_verified_input_artifact(
            verified_receipt_sha256=verified.verified_receipt_sha256,
            observed_at=datetime.now(),
        )
    assert (
        resolver.resolve_verified_input_artifact(
            verified_receipt_sha256=H5,
            observed_at=verified.verified_at + timedelta(seconds=1),
        )
        is None
    )
