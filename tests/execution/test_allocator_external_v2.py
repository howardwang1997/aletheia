"""Focused PostgreSQL regressions for the external bridge runtime-v2 lifecycle.

The 0036 chain regression proves the database guards against bypassed writers;
these tests drive the allocator's eight public external lifecycle methods
end to end with real CONTROL/BRIDGE ed25519 custody — the honest ladder
(reserved → starting → running → verifying → succeeded, including the
compute-release settle tail and the outbox publish), exact replays at every
stage, the typed expired-challenge rejection, deadline adjudication, and the
writer-side fail-closed negatives.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from sqlalchemy import select, text

import aletheia.execution.allocator as allocator_module
from aletheia.db import session_factory
from aletheia.execution.allocator import (
    AdmissionConflict,
    LeaseAuthorityError,
    RuntimeProofReplayRejected,
    RuntimeProofReplayRejectionCode,
)
from aletheia.execution.external_bridge_contracts import (
    AcceptedExternalQualificationTerminalSubmission,
    ExternalExecutorIdentity,
    ExternalLaunchEvidence,
    ExternalQualificationTerminalDeadlineExpiration,
    ExternalRuntimePreparation,
    ExternalTerminationEvidence,
    issue_external_qualification_terminal_submission,
    issue_external_runtime_launch_receipt,
    issue_external_runtime_termination_receipt,
)
from aletheia.execution.persistence import (
    _ExecutionAttemptRecord,
    _ExecutionBudgetEventRecord,
    _ExecutionBudgetHeadRecord,
    _ExecutionBudgetReservationRecord,
    _ExecutionExternalRuntimeTerminationAcceptanceRecord,
    _ExecutionQualificationTerminalOutboxRecord,
    _ExecutionResourceLeaseRecord,
)
from aletheia.execution.runtime_control_issuance import (
    PinnedRuntimeControlIssuanceAuthority,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_v2_contracts import (
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    RuntimeControlAuthorityPin,
    RuntimeLaunchAuthorizationRequest,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactVerifiedReceipt,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from postgres_test_safety import require_isolated_pr4_postgres  # noqa: E402
from test_allocator import (  # noqa: E402
    BRIDGE_PRIVATE_KEY,
    _EXECUTION_TABLES,
    _prepared,
)
from test_runtime_contracts import _digest, _public_key_hex  # noqa: E402

CONTROL_PRIVATE_KEY = bytes.fromhex("37" * 32)


@pytest.fixture(autouse=True)
def _clean_execution_tables() -> Iterator[None]:
    require_isolated_pr4_postgres()
    sessions = session_factory()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))
    yield
    require_isolated_pr4_postgres()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))


def _issuer() -> PinnedRuntimeControlIssuanceAuthority:
    """One wide-window CONTROL signer; expiry behaviour is tested per window field."""

    public_key = _public_key_hex(CONTROL_PRIVATE_KEY)
    pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("external-v2-runtime-control-policy"),
        principal_id="principal:external-v2-runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    return PinnedRuntimeControlIssuanceAuthority(pin=pin, private_key=CONTROL_PRIVATE_KEY)


def _clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: when)


def _admit(
    monkeypatch,
    *,
    artifact_quota_bytes: int = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
):
    """Admit one external attempt; a distinct quota admits a distinct bundle."""

    issuer = _issuer()
    prepared = _prepared(
        monkeypatch,
        external=True,
        runtime_control_issuer=issuer,
        artifact_quota_bytes=artifact_quota_bytes,
    )
    claim = prepared.allocator.admit_and_reserve(
        bundle=prepared.bundle, grant=prepared.grant
    )
    assert claim.created is True and claim.lease_token is not None
    assert claim.snapshot.node_id is None
    assert claim.snapshot.external_resource_class_id is not None
    return prepared, issuer, claim


def _preparation(prepared, claim, *, prepared_at: datetime) -> ExternalRuntimePreparation:
    return ExternalRuntimePreparation(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        execution_id=claim.snapshot.execution_id,
        infrastructure_attempt_id=claim.snapshot.attempt_id,
        intent_sha256=claim.snapshot.intent_sha256,
        runtime_id=_digest("external-v2-runtime"),
        runtime_engine=prepared.manifest.container_runtime,
        launch_spec_sha256=_digest("external-v2-launch-spec"),
        workload_executable_sha256=_digest("external-v2-executable"),
        workload_argv=("cuprate-diagnostic", "--plan", "plan.json"),
        runtime_request_sha256=_digest("external-v2-runtime-request"),
        enforced_placement_sha256=_digest("external-v2-enforced-placement"),
        input_materialization_receipt_sha256=_digest("external-v2-materialization"),
        fencing_epoch=claim.snapshot.fencing_epoch,
        lease_token_sha256=claim.snapshot.lease_token_sha256,
        prepared_dispatch_locator_sha256=_digest("external-v2-dispatch-locator"),
        prepared_at=prepared_at,
        prepared_monotonic_ns=100_000,
    )


def _launch_request(
    preparation: ExternalRuntimePreparation, claim, *, requested_at: datetime
) -> RuntimeLaunchAuthorizationRequest:
    return RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("external-v2-request-nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=claim.snapshot.attempt_id,
        fencing_epoch=claim.snapshot.fencing_epoch,
        lease_token_sha256=claim.snapshot.lease_token_sha256,
        pre_runtime_absence_epoch=0,
        pre_runtime_absence_receipt_sha256=None,
        requested_at=requested_at,
        requested_monotonic_ns=105_000,
    )


def _launch_receipt(
    prepared,
    authorization,
    preparation: ExternalRuntimePreparation,
    *,
    started_at: datetime,
    observed_at: datetime,
    signed_at: datetime,
):
    identity = ExternalExecutorIdentity(
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        runtime_id=preparation.runtime_id,
        executor_ref="bridge://v100ts/cuprate-diagnostic",
        executor_implementation_sha256=_digest("external-v2-executor-implementation"),
        invocation_payload_sha256=_digest("external-v2-invocation-payload"),
        started_at=started_at,
        started_monotonic_ns=110_000,
    )
    evidence = ExternalLaunchEvidence(
        preparation_sha256=preparation.preparation_sha256,
        external_launch_authorization_sha256=authorization.authorization_sha256,
        executor_identity=identity,
        executor_identity_sha256=identity.executor_identity_sha256,
        executor_start_monotonic_lower_bound_ns=110_000,
        executor_start_monotonic_upper_bound_exclusive_ns=112_000,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(
            preparation.input_materialization_receipt_sha256
        ),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        launch_evidence_journal_sha256=_digest("external-v2-launch-journal"),
        observed_at=observed_at,
        observed_monotonic_ns=115_000,
    )
    receipt = issue_external_runtime_launch_receipt(
        bridge_pin=prepared.bridge_pin,
        private_key=BRIDGE_PRIVATE_KEY,
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        launch_evidence=evidence,
        launch_evidence_sha256=evidence.launch_evidence_sha256,
        signed_at=signed_at,
    )
    return receipt, identity, evidence


def _termination_evidence(
    preparation: ExternalRuntimePreparation,
    identity: ExternalExecutorIdentity,
    launch_receipt,
    *,
    ended_at: datetime,
    exit_code: int = 0,
) -> ExternalTerminationEvidence:
    return ExternalTerminationEvidence(
        preparation_sha256=preparation.preparation_sha256,
        external_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        executor_identity_sha256=identity.executor_identity_sha256,
        exit_code=exit_code,
        ended_at=ended_at,
        ended_monotonic_ns=200_000,
        result_content_sha256=_digest("external-v2-result-content"),
        termination_journal_sha256=_digest("external-v2-termination-journal"),
    )


def _termination_receipt(
    prepared,
    challenge,
    evidence: ExternalTerminationEvidence,
    preparation: ExternalRuntimePreparation,
    launch_receipt,
    request,
    authorization,
    *,
    signed_at: datetime,
    expires_at: datetime,
):
    return issue_external_runtime_termination_receipt(
        bridge_pin=prepared.bridge_pin,
        private_key=BRIDGE_PRIVATE_KEY,
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        challenge_sha256=challenge.challenge_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_launch_authorization_request_sha256=request.request_sha256,
        external_launch_authorization_sha256=authorization.authorization_sha256,
        termination_evidence=evidence,
        termination_evidence_sha256=evidence.termination_evidence_sha256,
        signed_at=signed_at,
        expires_at=expires_at,
    )


def _terminal_artifacts(
    prepared,
    claim,
    accepted,
    *,
    produced_at: datetime,
    verified_at: datetime,
    disposition: str = "process_succeeded",
    manifest_sha_override: str | None = None,
):
    with session_factory()() as session:
        attempt_row = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        assert attempt_row is not None
        slot = attempt_row.intent_json["infrastructure_attempt"]["replicate_slot_id"]
    entry = ArtifactManifestEntry(
        expected_artifact_id=f"art_{_digest('external-v2-expected-artifact')[:32]}",
        artifact_key="diagnostic_report",
        role="raw_output",
        content_sha256=_digest("external-v2-artifact-content"),
        bytes=2048,
        media_type="application/json",
        schema_sha256=None,
        quarantine_ref="quarantine:none",
    )
    manifest = ArtifactManifest(
        intent_sha256=claim.snapshot.intent_sha256,
        execution_id=claim.snapshot.execution_id,
        replicate_slot_id=slot,
        infrastructure_attempt_id=claim.snapshot.attempt_id,
        entries=(entry,),
        produced_at=produced_at,
    )
    receipt = ArtifactVerifiedReceipt(
        artifact_manifest_sha256=manifest.manifest_sha256,
        producer_attempt_id=claim.snapshot.attempt_id,
        artifact=entry,
        custody_mode="central_rehash",
        verifier_principal_id="principal:external-bridge",
        object_store_id=_digest("external-v2-object-store"),
        final_object_ref="bridge://artifacts/diagnostic_report.json",
        final_object_version=_digest("external-v2-object-version"),
        custody_receipt_sha256s=(_digest("external-v2-custody-receipt"),),
        verified_at=verified_at,
    )
    submission = issue_external_qualification_terminal_submission(
        bridge_pin=prepared.bridge_pin,
        private_key=BRIDGE_PRIVATE_KEY,
        expected_disposition=None if disposition is None else disposition,
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        intent_sha256=claim.snapshot.intent_sha256,
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
        resource_lease_sha256=claim.snapshot.resource_lease_sha256,
        fencing_epoch=claim.snapshot.fencing_epoch,
        lease_token_sha256=claim.snapshot.lease_token_sha256,
        accepted_external_runtime_termination_sha256=(
            accepted.accepted_termination_sha256
        ),
        artifact_manifest_sha256=(
            manifest.manifest_sha256
            if manifest_sha_override is None
            else manifest_sha_override
        ),
        output_tree_sha256=_digest("external-v2-output-tree"),
        artifact_verified_receipt_sha256s=(receipt.verified_receipt_sha256,),
        disposition=disposition,
        submitted_at=verified_at + timedelta(seconds=5),
    )
    return submission, manifest, (receipt,)


def _drive_to_launched(
    monkeypatch, *, artifact_quota_bytes: int = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
):
    """Admit, authorize, and launch; return every artifact the later stages need."""

    prepared, _issuer_, claim = _admit(
        monkeypatch, artifact_quota_bytes=artifact_quota_bytes
    )
    t0 = prepared.observed_at
    preparation = _preparation(prepared, claim, prepared_at=t0 + timedelta(seconds=1))
    request = _launch_request(preparation, claim, requested_at=t0 + timedelta(seconds=2))
    _clock(monkeypatch, t0 + timedelta(seconds=3))
    start = prepared.allocator.authorize_external_runtime_start(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        runtime_preparation=preparation,
        launch_authorization_request=request,
    )
    assert start.replayed is False
    launch_receipt, identity, _evidence = _launch_receipt(
        prepared,
        start.launch_authorization,
        preparation,
        started_at=t0 + timedelta(seconds=4),
        observed_at=t0 + timedelta(seconds=5),
        signed_at=t0 + timedelta(seconds=6),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=7))
    launch = prepared.allocator.accept_external_runtime_launch(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        launch_receipt=launch_receipt,
    )
    assert launch.replayed is False
    assert launch.snapshot.status == "running"
    return prepared, claim, preparation, request, start, launch_receipt, identity, t0


def _drive_to_verifying(
    monkeypatch,
    *,
    exit_code: int = 0,
    artifact_quota_bytes: int = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
):
    """Drive through termination acceptance; the settle tail releases compute."""

    (
        prepared,
        claim,
        preparation,
        request,
        start,
        launch_receipt,
        identity,
        t0,
    ) = _drive_to_launched(
        monkeypatch, artifact_quota_bytes=artifact_quota_bytes
    )
    evidence = _termination_evidence(
        preparation,
        identity,
        launch_receipt,
        ended_at=t0 + timedelta(seconds=20),
        exit_code=exit_code,
    )
    _clock(monkeypatch, t0 + timedelta(seconds=30))
    challenged = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=evidence,
    )
    assert challenged.replayed is False
    termination_receipt = _termination_receipt(
        prepared,
        challenged.challenge,
        evidence,
        preparation,
        launch_receipt,
        request,
        start.launch_authorization,
        signed_at=t0 + timedelta(seconds=31),
        expires_at=t0 + timedelta(seconds=91),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=32))
    termination = prepared.allocator.accept_external_runtime_termination(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_receipt=termination_receipt,
    )
    assert termination.replayed is False
    assert termination.snapshot.status == "verifying"
    return (
        prepared,
        claim,
        preparation,
        challenged,
        evidence,
        termination,
        t0,
    )


def test_external_ladder_commits_and_replays_exactly(monkeypatch) -> None:
    (
        prepared,
        claim,
        preparation,
        request,
        start,
        launch_receipt,
        identity,
        t0,
    ) = _drive_to_launched(monkeypatch)

    # authorize replay is exact: same request returns the stored ticket
    _clock(monkeypatch, t0 + timedelta(seconds=4))
    replay_start = prepared.allocator.authorize_external_runtime_start(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        runtime_preparation=preparation,
        launch_authorization_request=request,
    )
    assert replay_start.replayed is True
    assert (
        replay_start.launch_authorization.authorization_sha256
        == start.launch_authorization.authorization_sha256
    )

    # launch replay is exact
    replay_launch = prepared.allocator.accept_external_runtime_launch(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        launch_receipt=launch_receipt,
    )
    assert replay_launch.replayed is True

    evidence = _termination_evidence(
        preparation,
        identity,
        launch_receipt,
        ended_at=t0 + timedelta(seconds=20),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=30))
    challenged = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=evidence,
    )
    assert challenged.replayed is False
    replay_challenge = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=evidence,
    )
    assert replay_challenge.replayed is True
    assert (
        replay_challenge.challenge.challenge_sha256
        == challenged.challenge.challenge_sha256
    )

    termination_receipt = _termination_receipt(
        prepared,
        challenged.challenge,
        evidence,
        preparation,
        launch_receipt,
        request,
        start.launch_authorization,
        signed_at=t0 + timedelta(seconds=31),
        expires_at=t0 + timedelta(seconds=91),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=32))
    termination = prepared.allocator.accept_external_runtime_termination(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_receipt=termination_receipt,
    )
    assert termination.replayed is False

    # the settle tail released compute at exactly the billable duration
    with session_factory()() as session:
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id
                == claim.snapshot.attempt_id
            )
        ).scalar_one()
        lease = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == claim.snapshot.attempt_id
            )
        ).scalar_one()
        head = session.get(
            _ExecutionBudgetHeadRecord, claim.snapshot.budget_authorization_sha256
        )
        assert reservation.state == "settled"
        assert reservation.actual_lease_seconds == 20
        assert (
            reservation.settled_microunits
            == reservation.fixed_charge_microunits
            + 20 * reservation.charge_per_second_microunits
        )
        assert termination.charged_microunits == reservation.settled_microunits
        assert lease.state == "released"
        assert head.reserved_microunits == 0
        assert head.spent_microunits == reservation.settled_microunits
        event = session.execute(
            select(_ExecutionBudgetEventRecord)
            .where(
                _ExecutionBudgetEventRecord.reservation_id
                == reservation.reservation_id,
                _ExecutionBudgetEventRecord.event_type == "settled",
            )
            .order_by(_ExecutionBudgetEventRecord.sequence.desc())
            .limit(1)
        ).scalar_one()
        assert event.payload_json["details"] == {
            "cost_quote_sha256": reservation.cost_quote_sha256,
            "fixed_charge_microunits": reservation.fixed_charge_microunits,
            "charge_per_second_microunits": reservation.charge_per_second_microunits,
            "actual_lease_seconds": 20,
            "charged_microunits": reservation.settled_microunits,
        }

    replay_termination = prepared.allocator.accept_external_runtime_termination(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_receipt=termination_receipt,
    )
    assert replay_termination.replayed is True
    assert replay_termination.charged_microunits == termination.charged_microunits

    submission, manifest, receipts = _terminal_artifacts(
        prepared,
        claim,
        termination.accepted_termination,
        produced_at=t0 + timedelta(seconds=20),
        verified_at=t0 + timedelta(seconds=35),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=41))
    artifacts = prepared.allocator.accept_external_terminal_artifacts(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        terminal_submission=submission,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
    )
    assert artifacts.replayed is False
    assert artifacts.snapshot.status == "verifying"
    replay_artifacts = prepared.allocator.accept_external_terminal_artifacts(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        terminal_submission=submission,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
    )
    assert replay_artifacts.replayed is True

    _clock(monkeypatch, t0 + timedelta(seconds=42))
    pending = prepared.allocator.pull_pending_external_qualification_terminal_settlement(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert pending is not None
    assert (
        pending.terminal_authority_sha256
        == artifacts.terminal_acceptance.terminal_authority_sha256
    )
    settled = prepared.allocator.settle_external_qualification_terminal(
        terminal_acceptance=pending,
    )
    assert settled.replayed is False
    assert settled.snapshot.status == "succeeded"

    with session_factory()() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        outbox = session.execute(
            select(_ExecutionQualificationTerminalOutboxRecord).where(
                _ExecutionQualificationTerminalOutboxRecord.attempt_id
                == claim.snapshot.attempt_id
            )
        ).scalar_one()
        assert attempt.status == "succeeded"
        assert attempt.terminal_deadline_expiration_sha256 is None
        assert outbox.terminal_authority_kind == "accepted_terminal_submission"
        assert outbox.terminal_authority_sha256 == pending.terminal_authority_sha256

    replay_settle = prepared.allocator.settle_external_qualification_terminal(
        terminal_acceptance=pending,
    )
    assert replay_settle.replayed is True
    assert (
        prepared.allocator.pull_pending_external_qualification_terminal_settlement(
            bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )


def test_external_verified_readers_replay_the_settled_lineage(monkeypatch) -> None:
    (
        prepared,
        claim,
        _preparation,
        _challenged,
        _evidence,
        termination,
        t0,
    ) = _drive_to_verifying(monkeypatch)

    # a live attempt without terminal artifacts is typed-pending, never a
    # partial export
    assert (
        prepared.allocator.load_verified_external_qualification_run_lineage(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
            observed_at=t0 + timedelta(seconds=45),
        )
        is None
    )
    assert (
        prepared.allocator.load_verified_qualification_terminal_source(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
        )
        is None
    )

    submission, manifest, receipts = _terminal_artifacts(
        prepared,
        claim,
        termination.accepted_termination,
        produced_at=t0 + timedelta(seconds=20),
        verified_at=t0 + timedelta(seconds=35),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=41))
    prepared.allocator.accept_external_terminal_artifacts(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        terminal_submission=submission,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
    )
    _clock(monkeypatch, t0 + timedelta(seconds=42))
    pending = prepared.allocator.pull_pending_external_qualification_terminal_settlement(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert pending is not None
    settled = prepared.allocator.settle_external_qualification_terminal(
        terminal_acceptance=pending,
    )
    assert settled.snapshot.status == "succeeded"

    observed = t0 + timedelta(seconds=60)
    lineage = prepared.allocator.load_verified_external_qualification_run_lineage(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
        observed_at=observed,
    )
    assert lineage is not None
    assert lineage.intent_sha256 == claim.snapshot.intent_sha256
    assert lineage.terminal_acceptance_sha256 == pending.terminal_authority_sha256
    assert lineage.terminal_submission_sha256 == submission.terminal_submission_sha256
    assert (
        lineage.accepted_runtime_termination_sha256
        == termination.accepted_termination.accepted_termination_sha256
    )
    assert lineage.bridge_manifest_sha256 == prepared.manifest.manifest_sha256
    assert lineage.artifact_manifest == manifest
    assert lineage.artifact_verified_receipts == receipts
    again = prepared.allocator.load_verified_external_qualification_run_lineage(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
        observed_at=observed,
    )
    assert again is not None and again.lineage_sha256 == lineage.lineage_sha256

    material = prepared.allocator.load_verified_external_qualification_raw_run_material(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
        observed_at=observed,
    )
    assert material is not None
    assert material.accepted_terminal_submission == pending
    assert material.terminal_submission == submission
    assert material.verified_at == observed

    source = prepared.allocator.load_verified_qualification_terminal_source(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
    )
    assert source is not None
    assert source.terminal_authority_kind == "accepted_terminal_submission"
    assert source.terminal_authority_sha256 == pending.terminal_authority_sha256
    replayed_lineage = prepared.allocator.load_verified_external_qualification_run_lineage(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
        observed_at=source.verified_at,
    )
    assert replayed_lineage is not None
    assert source.lineage_evidence_sha256 == replayed_lineage.lineage_sha256

    items = prepared.allocator.list_qualification_terminal_outbox(
        attempt_id_allowlist=(claim.snapshot.attempt_id,)
    )
    assert len(items) == 1
    assert isinstance(items[0].payload, AcceptedExternalQualificationTerminalSubmission)
    assert items[0].payload == pending


def test_external_terminal_source_covers_the_deadline_path(monkeypatch) -> None:
    (
        prepared,
        claim,
        _preparation,
        challenged,
        _evidence,
        termination,
        _t0,
    ) = _drive_to_verifying(monkeypatch)
    deadline = challenged.challenge.artifact_submission_deadline
    _clock(monkeypatch, deadline + timedelta(seconds=10))
    adjudicated = prepared.allocator.adjudicate_expired_external_qualification_terminal(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert adjudicated is not None and adjudicated.replayed is False

    source = prepared.allocator.load_verified_qualification_terminal_source(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
    )
    assert source is not None
    assert source.terminal_authority_kind == "terminal_deadline_expiration"
    assert (
        source.terminal_authority_sha256
        == adjudicated.terminal_expiration.expiration_sha256
    )
    assert (
        source.accepted_runtime_termination_sha256
        == termination.accepted_termination.accepted_termination_sha256
    )
    assert source.outbox_id == adjudicated.outbox_id

    items = prepared.allocator.list_qualification_terminal_outbox(
        attempt_id_allowlist=(claim.snapshot.attempt_id,)
    )
    assert len(items) == 1
    assert isinstance(items[0].payload, ExternalQualificationTerminalDeadlineExpiration)
    assert (
        items[0].payload.expiration_sha256
        == adjudicated.terminal_expiration.expiration_sha256
    )

    # a deadline-failed attempt has no terminal acceptance row: run lineage and
    # raw-run material fail closed instead of exporting partial custody
    after = deadline + timedelta(seconds=11)
    with pytest.raises(AdmissionConflict, match="durable lineage is incomplete"):
        prepared.allocator.load_verified_external_qualification_run_lineage(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
            observed_at=after,
        )
    with pytest.raises(AdmissionConflict, match="durable lineage is incomplete"):
        prepared.allocator.load_verified_external_qualification_raw_run_material(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
            observed_at=after,
        )


def test_external_authorize_rejects_foreign_authority_and_reauthorization(
    monkeypatch,
) -> None:
    prepared, _issuer_, claim = _admit(monkeypatch)
    t0 = prepared.observed_at
    preparation = _preparation(prepared, claim, prepared_at=t0 + timedelta(seconds=1))

    # a request under a foreign fencing epoch is refused before any insert
    foreign = RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("external-v2-request-nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=claim.snapshot.attempt_id,
        fencing_epoch=claim.snapshot.fencing_epoch + 1,
        lease_token_sha256=claim.snapshot.lease_token_sha256,
        pre_runtime_absence_epoch=0,
        pre_runtime_absence_receipt_sha256=None,
        requested_at=t0 + timedelta(seconds=2),
        requested_monotonic_ns=105_000,
    )
    _clock(monkeypatch, t0 + timedelta(seconds=3))
    with pytest.raises(LeaseAuthorityError, match="differs from locked attempt"):
        prepared.allocator.authorize_external_runtime_start(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            runtime_preparation=preparation,
            launch_authorization_request=foreign,
        )

    request = _launch_request(preparation, claim, requested_at=t0 + timedelta(seconds=2))
    start = prepared.allocator.authorize_external_runtime_start(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        runtime_preparation=preparation,
        launch_authorization_request=request,
    )
    launch_receipt, _identity, _evidence = _launch_receipt(
        prepared,
        start.launch_authorization,
        preparation,
        started_at=t0 + timedelta(seconds=4),
        observed_at=t0 + timedelta(seconds=5),
        signed_at=t0 + timedelta(seconds=6),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=7))
    prepared.allocator.accept_external_runtime_launch(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        launch_receipt=launch_receipt,
    )
    # a second authorization after the accepted launch is refused (same
    # preparation, a fresh request under an advanced clock)
    second_request = _launch_request(
        preparation, claim, requested_at=t0 + timedelta(seconds=9)
    )
    _clock(monkeypatch, t0 + timedelta(seconds=10))
    with pytest.raises(
        LeaseAuthorityError, match="cannot receive another external launch authorization"
    ):
        prepared.allocator.authorize_external_runtime_start(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            runtime_preparation=preparation,
            launch_authorization_request=second_request,
        )

    # the preparation pointer is one-shot: a rebound preparation for the same
    # attempt is refused even before any receipt exists
    prepared2, _issuer2, claim2 = _admit(
        monkeypatch,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1024,
    )
    t2 = prepared2.observed_at
    preparation2 = _preparation(prepared2, claim2, prepared_at=t2 + timedelta(seconds=1))
    request2 = _launch_request(
        preparation2, claim2, requested_at=t2 + timedelta(seconds=2)
    )
    _clock(monkeypatch, t2 + timedelta(seconds=3))
    prepared2.allocator.authorize_external_runtime_start(
        attempt_id=claim2.snapshot.attempt_id,
        lease_token=claim2.lease_token,
        fencing_epoch=claim2.snapshot.fencing_epoch,
        runtime_preparation=preparation2,
        launch_authorization_request=request2,
    )
    rebound = preparation2.model_copy(
        update={"prepared_dispatch_locator_sha256": _digest("external-v2-locator-3")}
    )
    rebound_request = _launch_request(
        rebound, claim2, requested_at=t2 + timedelta(seconds=4)
    )
    _clock(monkeypatch, t2 + timedelta(seconds=5))
    with pytest.raises(LeaseAuthorityError, match="preparation identity is rebound"):
        prepared2.allocator.authorize_external_runtime_start(
            attempt_id=claim2.snapshot.attempt_id,
            lease_token=claim2.lease_token,
            fencing_epoch=claim2.snapshot.fencing_epoch,
            runtime_preparation=rebound,
            launch_authorization_request=rebound_request,
        )


def test_external_challenge_generations_are_serialized(monkeypatch) -> None:
    (
        prepared,
        claim,
        preparation,
        _request,
        _start,
        launch_receipt,
        identity,
        t0,
    ) = _drive_to_launched(monkeypatch)
    evidence = _termination_evidence(
        preparation,
        identity,
        launch_receipt,
        ended_at=t0 + timedelta(seconds=20),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=30))
    first = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=evidence,
    )
    # a different observation while the first generation is live is refused
    second_evidence = evidence.model_copy(update={"ended_monotonic_ns": 201_000})
    with pytest.raises(LeaseAuthorityError, match="still live or accepted"):
        prepared.allocator.issue_external_termination_challenge(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            termination_evidence=second_evidence,
        )

    # after expiry with no acceptance, replaying the same observation is the
    # typed rejection, never a silent second generation
    _clock(monkeypatch, first.challenge.expires_at + timedelta(seconds=1))
    with pytest.raises(RuntimeProofReplayRejected) as failure:
        prepared.allocator.issue_external_termination_challenge(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            termination_evidence=evidence,
        )
    assert (
        failure.value.code
        is RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
    )
    # a fresh generation after the old one expired unaccepted is allowed
    third = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=second_evidence,
    )
    assert third.replayed is False
    assert third.challenge.challenge_sha256 != first.challenge.challenge_sha256


def test_external_termination_receipt_must_bind_its_challenge(monkeypatch) -> None:
    (
        prepared,
        claim,
        preparation,
        request,
        start,
        launch_receipt,
        identity,
        t0,
    ) = _drive_to_launched(monkeypatch)
    evidence = _termination_evidence(
        preparation,
        identity,
        launch_receipt,
        ended_at=t0 + timedelta(seconds=20),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=30))
    challenged = prepared.allocator.issue_external_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        termination_evidence=evidence,
    )
    # honest bytes for every lineage field, but the evidence itself is a
    # different terminal observation than the challenged one
    divergent = evidence.model_copy(update={"ended_monotonic_ns": 250_000})
    forged = _termination_receipt(
        prepared,
        challenged.challenge,
        divergent,
        preparation,
        launch_receipt,
        request,
        start.launch_authorization,
        signed_at=t0 + timedelta(seconds=31),
        expires_at=t0 + timedelta(seconds=91),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=32))
    with pytest.raises(LeaseAuthorityError, match="stale or invalid"):
        prepared.allocator.accept_external_runtime_termination(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            termination_receipt=forged,
        )
    with session_factory()() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        assert attempt.status == "running"
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id
                == claim.snapshot.attempt_id
            )
        ).scalar_one()
        assert reservation.state == "held"


def test_external_terminal_artifacts_fail_closed(monkeypatch) -> None:
    (
        prepared,
        claim,
        _preparation,
        _challenged,
        _evidence,
        termination,
        t0,
    ) = _drive_to_verifying(monkeypatch)

    # the manifest sha the submission names is not the manifest presented
    submission, manifest, receipts = _terminal_artifacts(
        prepared,
        claim,
        termination.accepted_termination,
        produced_at=t0 + timedelta(seconds=20),
        verified_at=t0 + timedelta(seconds=35),
        manifest_sha_override=_digest("external-v2-foreign-manifest"),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=41))
    with pytest.raises(
        LeaseAuthorityError, match="differ from the accepted termination"
    ):
        prepared.allocator.accept_external_terminal_artifacts(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            terminal_submission=submission,
            artifact_manifest=manifest,
            artifact_verified_receipts=receipts,
        )

    # a disposition that contradicts the observed exit code is refused
    (
        prepared3,
        claim3,
        _preparation3,
        _challenged3,
        evidence3,
        termination3,
        _t3,
    ) = _drive_to_verifying(
        monkeypatch,
        exit_code=1,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1024,
    )
    lying, manifest3, receipts3 = _terminal_artifacts(
        prepared3,
        claim3,
        termination3.accepted_termination,
        produced_at=evidence3.ended_at,
        verified_at=t0 + timedelta(seconds=35),
        disposition="process_succeeded",
    )
    _clock(monkeypatch, t0 + timedelta(seconds=41))
    with pytest.raises(
        LeaseAuthorityError, match="disposition differs from recomputed outcome"
    ):
        prepared3.allocator.accept_external_terminal_artifacts(
            attempt_id=claim3.snapshot.attempt_id,
            lease_token=claim3.lease_token,
            fencing_epoch=claim3.snapshot.fencing_epoch,
            terminal_submission=lying,
            artifact_manifest=manifest3,
            artifact_verified_receipts=receipts3,
        )
    with session_factory()() as session:
        acceptance = session.execute(
            select(_ExecutionExternalRuntimeTerminationAcceptanceRecord).where(
                _ExecutionExternalRuntimeTerminationAcceptanceRecord.attempt_id
                == claim3.snapshot.attempt_id
            )
        ).scalar_one()
        assert acceptance is not None  # termination acceptance persists...
        pointer = session.get(_ExecutionAttemptRecord, claim3.snapshot.attempt_id)
        assert pointer.accepted_terminal_submission_sha256 is None  # ...artifact acceptance does not


def test_external_settlement_rejects_foreign_acceptance(monkeypatch) -> None:
    (
        prepared,
        claim,
        _preparation,
        _challenged,
        _evidence,
        termination,
        t0,
    ) = _drive_to_verifying(monkeypatch)
    submission, manifest, receipts = _terminal_artifacts(
        prepared,
        claim,
        termination.accepted_termination,
        produced_at=t0 + timedelta(seconds=20),
        verified_at=t0 + timedelta(seconds=35),
    )
    _clock(monkeypatch, t0 + timedelta(seconds=41))
    artifacts = prepared.allocator.accept_external_terminal_artifacts(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        terminal_submission=submission,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
    )
    foreign = artifacts.terminal_acceptance.model_copy(
        update={"bridge_submitted_at": t0 + timedelta(seconds=39)}
    )
    with pytest.raises(LeaseAuthorityError, match="settlement is rebound"):
        prepared.allocator.settle_external_qualification_terminal(
            terminal_acceptance=foreign,
        )
    with session_factory()() as session:
        outbox = session.execute(
            select(_ExecutionQualificationTerminalOutboxRecord).where(
                _ExecutionQualificationTerminalOutboxRecord.attempt_id
                == claim.snapshot.attempt_id
            )
        ).scalar_one_or_none()
        assert outbox is None


def test_external_deadline_adjudication_is_presigned_and_atomic(monkeypatch) -> None:
    (
        prepared,
        claim,
        _preparation,
        challenged,
        _evidence,
        termination,
        _t0,
    ) = _drive_to_verifying(monkeypatch)

    deadline = challenged.challenge.artifact_submission_deadline
    # before the deadline nothing activates
    _clock(monkeypatch, deadline - timedelta(seconds=1))
    assert (
        prepared.allocator.adjudicate_expired_external_qualification_terminal(
            bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )

    _clock(monkeypatch, deadline + timedelta(seconds=10))
    adjudicated = prepared.allocator.adjudicate_expired_external_qualification_terminal(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert adjudicated is not None and adjudicated.replayed is False
    assert adjudicated.snapshot.status == "failed"
    assert (
        adjudicated.terminal_expiration.expiration_sha256
        == termination.terminal_expiration.expiration_sha256
    )

    with session_factory()() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        outbox = session.execute(
            select(_ExecutionQualificationTerminalOutboxRecord).where(
                _ExecutionQualificationTerminalOutboxRecord.attempt_id
                == claim.snapshot.attempt_id
            )
        ).scalar_one()
        assert attempt.status == "failed"
        assert (
            attempt.terminal_deadline_expiration_sha256
            == termination.terminal_expiration.expiration_sha256
        )
        assert outbox.terminal_authority_kind == "terminal_deadline_expiration"

    # the activated attempt leaves the candidate set; the durable rows ARE the
    # idempotency — a second sweep finds nothing to adjudicate
    replay = prepared.allocator.adjudicate_expired_external_qualification_terminal(
        bridge_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert replay is None
    assert (
        prepared.allocator.pull_pending_external_qualification_terminal_settlement(
            bridge_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )

    # an unregistered manifest cannot adjudicate or pull anything
    foreign_manifest = _digest("external-v2-unregistered-manifest")
    with pytest.raises(AdmissionConflict, match="no registered bridge authority"):
        prepared.allocator.adjudicate_expired_external_qualification_terminal(
            bridge_manifest_sha256=foreign_manifest,
        )
    with pytest.raises(AdmissionConflict, match="no registered bridge authority"):
        prepared.allocator.pull_pending_external_qualification_terminal_settlement(
            bridge_manifest_sha256=foreign_manifest,
        )
