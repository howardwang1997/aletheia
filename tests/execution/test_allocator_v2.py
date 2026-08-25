"""Focused PostgreSQL regressions for the persisted runtime-v2 allocator lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

import aletheia.execution.allocator as allocator_module
from aletheia.db import Base, engine, session_factory
from aletheia.execution.allocator import (
    AdmissionConflict,
    LeaseAuthorityError,
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
)
from aletheia.execution.node_agent import (
    NodeLocalStateStore,
    NodeRunOutcome,
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedLaunchRegistry,
    PinnedLaunchSpec,
    QualificationNodeAgent,
)
from aletheia.execution.persistence import (
    _ExecutionAttemptRecord,
    _ExecutionBudgetReservationRecord,
    _ExecutionOutboxRecord,
    _ExecutionQualificationTerminalAcceptanceRecord,
    _ExecutionQualificationTerminalDeadlineExpirationRecord,
    _ExecutionQualificationTerminalOutboxRecord,
    _ExecutionResourceLeaseRecord,
    _ExecutionRuntimeFenceRebindRecord,
    _ExecutionRuntimeTerminationAcceptanceRecord,
    _ExecutionTerminalReceiptRecord,
)
from aletheia.execution.postgresql_node_adapter import PostgreSQLNodeAllocatorAdapter
from aletheia.execution.runtime_contracts import (
    QualificationAuthorityVerifier,
    TerminalVerificationAuthorityVerifier,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    OutputQuotaProvisioningReceipt,
    PinnedRuntimeControlVerificationAuthority,
    PinnedInputPath,
    RuntimeControlAuthorityVerifier,
)
from aletheia.execution.terminal_source import VerifiedQualificationTerminalOutboxReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from postgres_test_safety import require_isolated_pr4_postgres  # noqa: E402
from test_allocator import (  # noqa: E402
    TRANSPORT_PRIVATE_KEY,
    _EXECUTION_TABLES,
    _prepared,
)
from test_node_agent import _Clock, _InputMaterializer, _Runtime  # noqa: E402
from test_postgresql_node_adapter import (  # noqa: E402
    _PublishingArtifactStore,
    _RuntimeControlIssuer,
)
from test_runtime_contracts import PRIVATE_KEY, _AuthorityResolver, _digest, _worker_authority  # noqa: E402


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


class _OutputQuotaProvisioner:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def ensure_output_quota(
        self,
        *,
        node_manifest_sha256: str,
        node_id: str,
        boot_id: str,
        execution_id: str,
        attempt_id: str,
        intent_sha256: str,
        output_root: Path,
        output_quota_bytes: int,
        expected_receipt: OutputQuotaProvisioningReceipt | None,
    ) -> OutputQuotaProvisioningReceipt:
        if expected_receipt is not None:
            return expected_receipt
        output_root.chmod(0o700)
        metadata = output_root.lstat()
        return OutputQuotaProvisioningReceipt(
            node_manifest_sha256=node_manifest_sha256,
            node_id=node_id,
            boot_id=boot_id,
            execution_id=execution_id,
            infrastructure_attempt_id=attempt_id,
            intent_sha256=intent_sha256,
            output_root=str(output_root),
            output_quota_bytes=output_quota_bytes,
            output_root_device=metadata.st_dev,
            output_root_inode=metadata.st_ino,
            output_root_owner_uid=metadata.st_uid,
            output_root_owner_gid=metadata.st_gid,
            output_root_mode=stat.S_IMODE(metadata.st_mode),
            mount_id=1,
            mount_parent_id=1,
            block_device_major=os.major(metadata.st_dev),
            block_device_minor=os.minor(metadata.st_dev),
            block_device_capacity_bytes=output_quota_bytes - (output_quota_bytes % 512),
            filesystem_type="ext4",
            filesystem_uuid_sha256=_digest("allocator-v2-output-filesystem"),
            mount_options=("nodev", "noexec", "nosuid", "rw"),
            backing_file_identity_sha256=_digest("allocator-v2-output-backing"),
            provisioner_policy_sha256=_digest("allocator-v2-output-policy"),
            provisioner_principal_id="principal:test-output-quota",
            provisioned_at=self._clock.now(),
        )


def _running_v2(monkeypatch, tmp_path, *, start: bool = True):
    prepared = _prepared(
        monkeypatch,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )
    node_authority = _worker_authority(
        prepared.manifest,
        observed_at=prepared.observed_at,
    )
    issuer = _RuntimeControlIssuer(observed_at=prepared.observed_at)
    output_store = _PublishingArtifactStore(
        tmp_path / "artifact-cas",
        resolver=prepared.artifacts,
    )
    quote = prepared.bundle.cost_quote
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(prepared.case.pin),
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=_AuthorityResolver(prepared.bundle),
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({quote.pricing_policy_sha256}),
            currency_codes=frozenset({quote.currency_code}),
        ),
        node_authorities=(node_authority,),
        node_assignment_transport_pins=(prepared.transport_pin,),
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(
            prepared.terminal_pin
        ),
        allocator_principal_id="principal:allocator",
        runtime_control_issuer=issuer,
        max_inventory_ttl_seconds=30,
        heartbeat_extension_seconds=15,
        artifact_submission_grace_seconds=3600,
    )
    allocator.register_node(prepared.manifest.node_id)
    allocator.append_inventory(prepared.inventory)
    claim = allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    assert claim.lease_token is not None

    state = NodeLocalStateStore(tmp_path / "node-state")
    clock = _Clock(prepared.observed_at)
    adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=prepared.transport_pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        token_custody=state,
        clock=clock,
    )
    intent = prepared.bundle.intent
    node = next(
        item
        for item in prepared.bundle.work_order.nodes
        if item.node_id == intent.work_order_node_id
    )
    artifact_paths = tuple(
        PinnedArtifactPath(
            artifact_key=item.artifact_key,
            relative_path=f"{index:03d}-{item.artifact_key.replace(':', '_')}.bin",
        )
        for index, item in enumerate(intent.expected_artifacts, start=1)
    )
    spec = PinnedLaunchSpec(
        command_sha256=node.command_sha256,
        environment_sha256=node.environment_sha256,
        capability_manifest_sha256=node.capability_manifest_sha256,
        executable_sha256=_digest("allocator-v2-qualified-executable"),
        runtime_engine=prepared.manifest.container_runtime,
        argv=("/opt/aletheia/bin/qualified-group", "--input", "/input/input.json"),
        environment=(PinnedEnvironmentVariable(name="LC_ALL", value="C.UTF-8"),),
        input_paths=(
            PinnedInputPath(
                input_port_id=intent.input_artifact_bindings[0].input_port_id,
                relative_path="input.json",
            ),
        ),
        artifact_paths=artifact_paths,
    )
    runtime = _Runtime(clock)
    agent = QualificationNodeAgent(
        node_authority=node_authority,
        qualification_authority=QualificationAuthorityVerifier(prepared.case.pin),
        runtime_control_authority=issuer.authority_verifier,
        node_signing_private_key=PRIVATE_KEY,
        boot_id="boot.001",
        allocator_principal_id="principal:allocator",
        allocator=adapter,
        runtime=runtime,
        artifact_quarantine=output_store,
        launch_registry=PinnedLaunchRegistry((spec,)),
        state_store=state,
        input_materializer=_InputMaterializer(),
        output_quota_provisioner=_OutputQuotaProvisioner(clock),
        clock=clock,
        artifact_completion_grace_seconds=3600,
    )
    if start:
        assert agent.run_once().outcome is NodeRunOutcome.RUNNING
    return prepared, allocator, adapter, agent, runtime, state, claim


def _verification_only_allocator(
    allocator: PostgreSQLExecutionAllocator,
) -> PostgreSQLExecutionAllocator:
    runtime_authority = PinnedRuntimeControlVerificationAuthority(
        allocator._runtime_control_issuer.authority_pin
    )
    assert not hasattr(runtime_authority, "issue_launch_authorization")
    return PostgreSQLExecutionAllocator(
        authority=allocator._authority,
        artifact_resolver=allocator._artifact_resolver,
        execution_authority_resolver=allocator._execution_authority_resolver,
        pricing_authority=allocator._pricing_authority,
        node_authorities=tuple(allocator._node_authorities.values()),
        node_assignment_transport_pins=tuple(allocator._node_assignment_transport_pins.values()),
        terminal_verification_authority=allocator._terminal_verification_authority,
        allocator_principal_id=allocator._allocator_principal_id,
        runtime_control_authority=runtime_authority,
    )


def test_public_key_only_allocator_can_admit_but_cannot_issue_runtime_controls(
    monkeypatch,
) -> None:
    prepared = _prepared(
        monkeypatch,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )
    node_authority = _worker_authority(
        prepared.manifest,
        observed_at=prepared.observed_at,
    )
    issuer = _RuntimeControlIssuer(observed_at=prepared.observed_at)
    quote = prepared.bundle.cost_quote
    signing_allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(prepared.case.pin),
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=_AuthorityResolver(prepared.bundle),
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({quote.pricing_policy_sha256}),
            currency_codes=frozenset({quote.currency_code}),
        ),
        node_authorities=(node_authority,),
        node_assignment_transport_pins=(prepared.transport_pin,),
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(
            prepared.terminal_pin
        ),
        allocator_principal_id="principal:allocator",
        runtime_control_issuer=issuer,
    )
    signing_allocator.register_node(prepared.manifest.node_id)
    signing_allocator.append_inventory(prepared.inventory)
    verification_only = _verification_only_allocator(signing_allocator)

    claim = verification_only.admit_and_reserve(
        bundle=prepared.bundle,
        grant=prepared.grant,
    )

    assert claim.created is True
    assert verification_only.runtime_control_verification_enabled is True
    assert verification_only.runtime_control_issuance_enabled is False
    with pytest.raises(LeaseAuthorityError, match="without pinned runtime-control custody"):
        verification_only._require_runtime_control_issuer()


def _assert_raw_runtime_v2_mutation_rejected(
    *,
    table: str,
    update_sql: str,
    params: dict[str, object],
    match: str,
) -> None:
    sessions = session_factory()
    trigger = f"trg_{table}_append_only"
    with pytest.raises(DBAPIError, match=match):
        with sessions() as session, session.begin():
            session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
            session.execute(text(update_sql), params)
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_runtime_v2_terminal_acceptance_recovery_and_outbox_are_atomic(
    monkeypatch, tmp_path
) -> None:
    prepared, allocator, adapter, agent, runtime, state, claim = _running_v2(monkeypatch, tmp_path)
    runtime.finish(exit_code=0)
    result = agent.run_once()
    assert result.outcome is NodeRunOutcome.COLLECTED
    assert result.accepted_runtime_termination is not None

    recovery_delivery = allocator.pull_assignment_delivery(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert recovery_delivery is not None
    recovery = recovery_delivery.historical_recovery_grant
    assert recovery is not None
    assert recovery.accepted_runtime_termination_sha256 == (
        result.accepted_runtime_termination.accepted_termination_sha256
    )
    accepted_bundle = state.load_accepted_runtime_termination(attempt_id=claim.snapshot.attempt_id)
    assert accepted_bundle is not None
    challenge, node_receipt, accepted = accepted_bundle
    replayed = allocator.replay_accepted_runtime_termination(
        recovery_grant=recovery,
        challenge=challenge,
        node_runtime_termination_receipt=node_receipt,
        expected_accepted_runtime_termination_sha256=(accepted.accepted_termination_sha256),
    )
    assert replayed == accepted

    sessions = session_factory()
    with sessions() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        assert attempt is not None and attempt.status == "verifying"
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        budget = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        assert resource.state == "released"
        assert budget.state == "settled"
        terminal_row = session.execute(
            select(_ExecutionQualificationTerminalAcceptanceRecord).where(
                _ExecutionQualificationTerminalAcceptanceRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        terminal_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
            terminal_row.accepted_terminal_submission_json
        )
        lineage = allocator.load_verified_qualification_run_lineage(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
            observed_at=terminal_acceptance.accepted_at,
        )
        assert lineage is not None
        assert lineage.qualification_admission_sha256 == claim.snapshot.admission_sha256
        assert lineage.resource_reservation_sha256 == claim.snapshot.resource_lease_sha256
        assert lineage.runtime_launch_sha256 == (
            result.accepted_runtime_termination.node_runtime_launch_receipt_sha256
        )
        assert lineage.accepted_runtime_termination_sha256 == (
            result.accepted_runtime_termination.accepted_termination_sha256
        )
        assert lineage.terminal_acceptance_sha256 == (
            terminal_acceptance.accepted_terminal_submission_sha256
        )
        assert (
            session.scalar(select(func.count()).select_from(_ExecutionTerminalReceiptRecord)) == 0
        )
        assert session.scalar(select(func.count()).select_from(_ExecutionOutboxRecord)) == 0

    late = terminal_acceptance.artifact_submission_deadline + timedelta(seconds=1)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    assert (
        allocator.pull_assignment_delivery(
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )
    pending_one = allocator.pull_pending_qualification_terminal_settlement(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    pending_two = allocator.pull_pending_qualification_terminal_settlement(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert pending_one == terminal_acceptance
    assert pending_two == terminal_acceptance

    settled = allocator.settle_qualification_terminal(terminal_acceptance=pending_one)
    assert settled.snapshot.status == "succeeded"
    assert settled.outbox_id == (f"qto_{terminal_acceptance.accepted_terminal_submission_sha256}")
    replay = allocator.settle_qualification_terminal(terminal_acceptance=terminal_acceptance)
    assert replay.replayed is True and replay.outbox_id == settled.outbox_id
    projection = allocator.load_qualification_terminal_outbox(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
    )
    assert projection is not None
    assert projection.outbox_id == settled.outbox_id
    assert projection.payload == terminal_acceptance
    terminal_reader = VerifiedQualificationTerminalOutboxReader(
        _verification_only_allocator(allocator)
    )
    assert not hasattr(terminal_reader, "admit_and_reserve")
    verified_source = terminal_reader.load_verified_qualification_terminal_source(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
    )
    assert verified_source is not None
    assert verified_source.outbox_id == projection.outbox_id
    assert verified_source.terminal_authority_kind == "accepted_terminal_submission"
    assert verified_source.terminal_authority_sha256 == (
        terminal_acceptance.accepted_terminal_submission_sha256
    )
    assert verified_source.qualification_admission_sha256 == claim.snapshot.admission_sha256
    assert verified_source.resource_reservation_sha256 == claim.snapshot.resource_lease_sha256
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(_ExecutionQualificationTerminalOutboxRecord)
            )
            == 1
        )
        assert allocator.list_qualification_terminal_outbox_in_session(
            session,
            attempt_id_allowlist=(claim.snapshot.attempt_id,),
        ) == (projection,)

    artifact_receipt_sha256 = lineage.artifact_verified_receipts[0].verified_receipt_sha256
    artifact_resolution = prepared.artifacts._resolutions[artifact_receipt_sha256]
    prepared.artifacts._resolutions[artifact_receipt_sha256] = artifact_resolution.model_copy(
        update={"content_rehash_sha256": "f" * 64}
    )
    with pytest.raises(AdmissionConflict, match="fresh custody verification"):
        terminal_reader.load_verified_qualification_terminal_source(
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
        )


def test_terminal_deadline_expiration_is_presigned_and_atomically_activated(
    monkeypatch, tmp_path
) -> None:
    prepared, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )

    def crash_before_artifact_acceptance(**_scope: object) -> None:
        raise SystemExit("crash before terminal artifact acceptance")

    monkeypatch.setattr(
        allocator,
        "accept_terminal_artifacts",
        crash_before_artifact_acceptance,
    )
    runtime.finish(exit_code=0)
    with pytest.raises(SystemExit, match="terminal artifact acceptance"):
        agent.run_once()

    sessions = session_factory()
    with sessions() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        assert attempt is not None
        assert attempt.status == "verifying"
        assert attempt.accepted_runtime_termination_sha256 is not None
        assert attempt.accepted_terminal_submission_sha256 is None
        assert attempt.terminal_deadline_expiration_sha256 is None
        termination = session.execute(
            select(_ExecutionRuntimeTerminationAcceptanceRecord).where(
                _ExecutionRuntimeTerminationAcceptanceRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        assert termination.conditional_terminal_expiration_authorized_at == (
            termination.accepted_at
        )
        deadline = termination.conditional_terminal_expiration_expires_at

    late = deadline + timedelta(microseconds=1)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    original_adjudicate = allocator.adjudicate_expired_qualification_terminal
    committed: dict[str, object] = {}

    def commit_then_crash(**scope: object) -> None:
        committed["result"] = original_adjudicate(**scope)
        raise SystemExit("crash after terminal deadline transaction")

    monkeypatch.setattr(
        allocator,
        "adjudicate_expired_qualification_terminal",
        commit_then_crash,
    )
    with pytest.raises(SystemExit, match="deadline transaction"):
        allocator.adjudicate_expired_qualification_terminal(
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
    first = committed["result"]
    assert first is not None
    assert first.replayed is False
    assert first.snapshot.status == "failed"
    assert first.activated_at == late
    assert first.terminal_authority_kind == "terminal_deadline_expiration"
    assert first.outbox_id == (
        f"qto_{first.terminal_expiration.terminal_deadline_expiration_sha256}"
    )

    monkeypatch.setattr(
        allocator,
        "adjudicate_expired_qualification_terminal",
        original_adjudicate,
    )
    assert (
        allocator.adjudicate_expired_qualification_terminal(
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )
    with sessions() as session:
        attempt = session.get(_ExecutionAttemptRecord, claim.snapshot.attempt_id)
        assert attempt is not None and attempt.status == "failed"
        assert attempt.terminal_deadline_expiration_sha256 == (
            first.terminal_expiration.terminal_deadline_expiration_sha256
        )
        activation = session.get(
            _ExecutionQualificationTerminalDeadlineExpirationRecord,
            first.terminal_expiration.terminal_deadline_expiration_sha256,
        )
        assert activation is not None and activation.activated_at == late
        outbox = session.get(_ExecutionQualificationTerminalOutboxRecord, first.outbox_id)
        assert outbox is not None
        assert outbox.terminal_authority_kind == "terminal_deadline_expiration"
        assert outbox.terminal_authority_sha256 == (
            first.terminal_expiration.terminal_deadline_expiration_sha256
        )
        assert outbox.accepted_terminal_submission_sha256 is None
        assert outbox.terminal_deadline_expiration_sha256 == (
            first.terminal_expiration.terminal_deadline_expiration_sha256
        )
        assert (
            session.scalar(
                select(func.count()).select_from(
                    _ExecutionQualificationTerminalDeadlineExpirationRecord
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(_ExecutionQualificationTerminalOutboxRecord)
            )
            == 1
        )
        projection = allocator.load_qualification_terminal_outbox_in_session(
            session,
            execution_id=claim.snapshot.execution_id,
            attempt_id=claim.snapshot.attempt_id,
        )
        assert projection is not None
        assert projection.outbox_id == first.outbox_id
        assert projection.payload == first.terminal_expiration
    terminal_reader = VerifiedQualificationTerminalOutboxReader(
        _verification_only_allocator(allocator)
    )
    verified_source = terminal_reader.load_verified_qualification_terminal_source(
        execution_id=claim.snapshot.execution_id,
        attempt_id=claim.snapshot.attempt_id,
    )
    assert verified_source is not None
    assert verified_source.outbox_id == first.outbox_id
    assert verified_source.terminal_authority_kind == "terminal_deadline_expiration"
    assert verified_source.terminal_authority_sha256 == (
        first.terminal_expiration.terminal_deadline_expiration_sha256
    )
    assert verified_source.accepted_runtime_termination_sha256 == (
        first.terminal_expiration.accepted_runtime_termination_sha256
    )


def test_runtime_start_exact_commit_replays_after_lease_and_ticket_expiry(
    monkeypatch, tmp_path
) -> None:
    _prepared_case, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch,
        tmp_path,
        start=False,
    )
    original_authorize = allocator.authorize_runtime_start
    committed: dict[str, object] = {}

    def commit_then_crash(**scope: object) -> None:
        committed["scope"] = scope
        committed["result"] = original_authorize(**scope)
        raise SystemExit("crash after durable start authorization")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="durable start authorization"):
        agent.run_once()
    first = committed["result"]
    assert first.replayed is False
    late = claim.snapshot.hard_deadline + timedelta(seconds=1)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)

    replay = original_authorize(**committed["scope"])

    assert replay.replayed is True
    assert replay.launch_authorization == first.launch_authorization
    assert replay.snapshot.status == "starting"


def test_prelaunch_start_commit_recovers_after_deadline_only_to_cleanup(
    monkeypatch, tmp_path
) -> None:
    prepared, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch,
        tmp_path,
        start=False,
    )
    original_authorize = allocator.authorize_runtime_start

    def commit_then_crash(**scope: object) -> None:
        original_authorize(**scope)
        raise SystemExit("crash before saving committed start authority")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="saving committed start authority"):
        agent.run_once()
    monkeypatch.setattr(allocator, "authorize_runtime_start", original_authorize)
    assert runtime.launch_calls == 0

    late = claim.snapshot.hard_deadline + timedelta(seconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    reconciled = allocator.reconcile_expired()
    assert len(reconciled) == 1
    assert reconciled[0].status == "reconciliation_required"

    delivery = allocator.pull_assignment_delivery(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert delivery is not None
    assert delivery.sealed_envelope is None
    assert delivery.historical_recovery_grant is None
    lineage = delivery.historical_pre_runtime_recovery_lineage
    assert lineage is not None
    assert lineage.cleanup_only is True and lineage.launch_allowed is False

    cleaned = agent.run_once()
    assert cleaned.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert runtime.launch_calls == 0
    assert runtime.cleanup_calls == 1
    snapshot = allocator.load_attempt(claim.snapshot.attempt_id)
    assert snapshot is not None and snapshot.status == "cancelled"
    assert snapshot.reconciliation_reason is None
    assert snapshot.resource_lease_sha256 == claim.snapshot.resource_lease_sha256


def test_runtime_v2_admission_rejects_future_runtime_control_pin_atomically(
    monkeypatch,
) -> None:
    prepared = _prepared(
        monkeypatch,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )
    issuer = _RuntimeControlIssuer(observed_at=prepared.observed_at)
    issuer._pin = issuer.authority_pin.model_copy(  # noqa: SLF001
        update={"valid_from": prepared.observed_at + timedelta(seconds=1)}
    )
    issuer._verifier = RuntimeControlAuthorityVerifier(issuer._pin)  # noqa: SLF001
    quote = prepared.bundle.cost_quote
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(prepared.case.pin),
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=_AuthorityResolver(prepared.bundle),
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({quote.pricing_policy_sha256}),
            currency_codes=frozenset({quote.currency_code}),
        ),
        node_authorities=(
            _worker_authority(
                prepared.manifest,
                observed_at=prepared.observed_at,
            ),
        ),
        node_assignment_transport_pins=(prepared.transport_pin,),
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(
            prepared.terminal_pin
        ),
        allocator_principal_id="principal:allocator",
        runtime_control_issuer=issuer,
    )
    allocator.register_node(prepared.manifest.node_id)
    allocator.append_inventory(prepared.inventory)

    with pytest.raises(AdmissionConflict, match="runtime-v2 authority pins"):
        allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)

    sessions = session_factory()
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(_ExecutionAttemptRecord)) == 0


@pytest.mark.parametrize(
    ("artifact_quota_bytes", "must_reject"),
    (
        (1_000_000, True),
        (MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES - 1, True),
        (MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES, False),
        (20_000_000, False),
    ),
)
def test_runtime_v2_admission_enforces_deployable_output_filesystem_minimum(
    monkeypatch,
    artifact_quota_bytes: int,
    must_reject: bool,
) -> None:
    if must_reject:
        prepared = _prepared(monkeypatch)
        request = prepared.bundle.intent.resource_request.model_copy(
            update={"artifact_quota_bytes": artifact_quota_bytes}
        )
        intent = prepared.bundle.intent.model_copy(update={"resource_request": request})
        quote = prepared.bundle.cost_quote.model_copy(
            update={"intent_sha256": intent.intent_sha256}
        )
        bundle = type(prepared.bundle).model_validate(
            {
                **prepared.bundle.model_dump(mode="python"),
                "intent": intent,
                "cost_quote": quote,
            }
        )
        grant = prepared.grant
    else:
        prepared = _prepared(
            monkeypatch,
            artifact_quota_bytes=artifact_quota_bytes,
        )
        bundle = prepared.bundle
        grant = prepared.grant
        quote = bundle.cost_quote
    resolver = _AuthorityResolver(bundle)
    issuer = _RuntimeControlIssuer(observed_at=prepared.observed_at)
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(prepared.case.pin),
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({quote.pricing_policy_sha256}),
            currency_codes=frozenset({quote.currency_code}),
        ),
        node_authorities=(
            _worker_authority(
                prepared.manifest,
                observed_at=prepared.observed_at,
            ),
        ),
        node_assignment_transport_pins=(prepared.transport_pin,),
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(
            prepared.terminal_pin
        ),
        allocator_principal_id="principal:allocator",
        runtime_control_issuer=issuer,
        artifact_submission_grace_seconds=3600,
    )
    allocator.register_node(prepared.manifest.node_id)
    allocator.append_inventory(prepared.inventory)

    if must_reject:
        with pytest.raises(AdmissionConflict, match="deployable filesystem minimum"):
            allocator.admit_and_reserve(bundle=bundle, grant=grant)
        sessions = session_factory()
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(_ExecutionAttemptRecord)) == 0
            assert session.scalar(text("SELECT count(*) FROM execution_heads")) == 0
    else:
        claim = allocator.admit_and_reserve(bundle=bundle, grant=grant)
        assert claim.created is True


def test_runtime_termination_challenge_refresh_is_exact_next_and_immutable(
    monkeypatch, tmp_path
) -> None:
    _prepared_case, allocator, _adapter, agent, runtime, state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    runtime.finish(exit_code=0)

    def crash_after_challenge(**_scope: object) -> None:
        raise SystemExit("crash after durable termination challenge")

    monkeypatch.setattr(
        allocator,
        "accept_runtime_termination",
        crash_after_challenge,
    )
    with pytest.raises(SystemExit, match="durable termination challenge"):
        agent.run_once()

    local = state.load_state(claim.snapshot.attempt_id)
    proof = state.load_runtime_termination_proof(attempt_id=claim.snapshot.attempt_id)
    assert local is not None and local.node_runtime_launch_receipt is not None
    assert proof is not None
    prior_challenge, prior_receipt = proof
    runtime.clock.current = prior_challenge.expires_at
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: runtime.clock.current,
    )
    assert runtime.request is not None and runtime.preparation is not None
    refreshed = runtime.inspect(
        request=runtime.request,
        preparation=runtime.preparation,
        identity=runtime.identity,
    )
    deadline = claim.snapshot.hard_deadline + timedelta(seconds=3600)

    with pytest.raises(LeaseAuthorityError, match="proof windows"):
        allocator.issue_runtime_termination_challenge(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            runtime_preparation=local.runtime_preparation,
            node_runtime_launch_receipt=local.node_runtime_launch_receipt,
            termination_evidence=refreshed,
            inspection_sequence=prior_receipt.inspection_sequence + 2,
            artifact_submission_deadline=deadline,
        )

    mutated = refreshed.model_copy(
        update={
            "exit_code": (refreshed.exit_code or 0) + 1,
            "inspection_evidence_sha256": _digest("mutated-terminal-refresh"),
        }
    )
    with pytest.raises(LeaseAuthorityError, match="immutable engine facts"):
        allocator.issue_runtime_termination_challenge(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            runtime_preparation=local.runtime_preparation,
            node_runtime_launch_receipt=local.node_runtime_launch_receipt,
            termination_evidence=mutated,
            inspection_sequence=prior_receipt.inspection_sequence + 1,
            artifact_submission_deadline=deadline,
        )

    refreshed_commit = allocator.issue_runtime_termination_challenge(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        runtime_preparation=local.runtime_preparation,
        node_runtime_launch_receipt=local.node_runtime_launch_receipt,
        termination_evidence=refreshed,
        inspection_sequence=prior_receipt.inspection_sequence + 1,
        artifact_submission_deadline=deadline,
    )
    assert refreshed_commit.replayed is False
    assert refreshed_commit.challenge.inspection_sequence == 2

    sessions = session_factory()
    with pytest.raises(DBAPIError, match="evidence refresh changed immutable facts"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    "ALTER TABLE execution_runtime_termination_challenges "
                    "DISABLE TRIGGER trg_execution_runtime_termination_challenges_append_only"
                )
            )
            session.execute(
                text(
                    """
                    UPDATE execution_runtime_termination_challenges
                       SET inspection_evidence_json = jsonb_set(
                             inspection_evidence_json, '{exit_code}', '23'::jsonb)
                     WHERE attempt_id = :attempt_id AND inspection_sequence = 2
                    """
                ),
                {"attempt_id": claim.snapshot.attempt_id},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.parametrize("malformed", ({}, None, 7, {"schema_name": "extra", "x": 1}))
def test_runtime_v2_closed_json_validator_rejects_raw_malformed_values(malformed) -> None:
    sessions = session_factory()
    schemas = (
        "aletheia.runtime_preparation",
        "aletheia.runtime_launch_authorization_request",
        "aletheia.runtime_launch_authorization",
        "aletheia.node_runtime_launch_receipt",
        "aletheia.historical_runtime_recovery_grant",
        "aletheia.pre_runtime_absence_receipt",
        "aletheia.pre_runtime_absence_decision_record",
        "aletheia.runtime_fence_rebind_request",
        "aletheia.runtime_fence_rebind_receipt",
        "aletheia.runtime_termination_acceptance_challenge",
        "aletheia.node_runtime_termination_receipt",
        "aletheia.accepted_runtime_termination",
        "aletheia.qualification_terminal_deadline_expiration",
        "aletheia.qualification_terminal_submission",
        "aletheia.accepted_qualification_terminal_submission",
    )
    with sessions() as session:
        for schema in schemas:
            valid = session.execute(
                text(
                    "SELECT aletheia_execution_runtime_v2_json_valid("
                    "CAST(:value AS jsonb), :schema)"
                ),
                {"value": json.dumps(malformed), "schema": schema},
            ).scalar_one()
            assert valid is False


def test_execution_persistence_metadata_creates_in_isolated_schema() -> None:
    require_isolated_pr4_postgres()
    schema = f"pr4b_metadata_{uuid4().hex}"
    execution_tables = tuple(
        table for table in Base.metadata.tables.values() if table.name.startswith("execution_")
    )
    assert len(execution_tables) == 27
    with engine().connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            Base.metadata.create_all(
                connection,
                tables=execution_tables,
                checkfirst=False,
            )
            constraints = dict(
                connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid)
                          FROM pg_constraint
                         WHERE connamespace = CAST(:schema AS regnamespace)
                        """
                    ),
                    {"schema": schema},
                ).all()
            )
            assert (
                "conditional_terminal_expiration_sha256"
                not in constraints["ck_execution_runtime_launch_receipts_hashes"]
            )
            assert (
                "conditional_terminal_expiration_sha256"
                in constraints["ck_execution_runtime_termination_acceptances_hashes"]
            )
        finally:
            transaction.rollback()


@pytest.mark.parametrize(
    "terminal_authority_kind",
    ("accepted_terminal_submission", "not-a-terminal-authority"),
)
def test_terminal_outbox_local_check_rejects_null_or_unknown_authority_kind(
    terminal_authority_kind: str,
) -> None:
    sessions = session_factory()
    authority_sha256 = _digest(f"null-outbox:{terminal_authority_kind}")
    with pytest.raises(
        DBAPIError,
        match="ck_execution_qualification_terminal_outbox_hashes",
    ):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO execution_qualification_terminal_outbox (
                      outbox_id, terminal_authority_kind, terminal_authority_sha256,
                      accepted_terminal_submission_sha256,
                      terminal_deadline_expiration_sha256, execution_id, attempt_id,
                      topic, delivery_key, payload_sha256, payload_json, created_at
                    ) VALUES (
                      :outbox_id, :kind, :authority, NULL, NULL,
                      '00000000-0000-4000-8000-000000000000',
                      'iat_00000000000000000000000000000000',
                      'execution.qualification_terminal.v2',
                      'execution-v2:00000000-0000-4000-8000-000000000000:' ||
                        'iat_00000000000000000000000000000000',
                      :authority, '{}'::jsonb, clock_timestamp()
                    )
                    """
                ),
                {
                    "outbox_id": f"qto_{authority_sha256}",
                    "kind": terminal_authority_kind,
                    "authority": authority_sha256,
                },
            )


def test_raw_terminated_evidence_cannot_claim_absence_fields(monkeypatch, tmp_path) -> None:
    _prepared_case, _allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    runtime.finish(exit_code=0)
    assert agent.run_once().outcome is NodeRunOutcome.COLLECTED
    sessions = session_factory()
    with pytest.raises(DBAPIError, match="runtime termination acceptance JSON"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    "ALTER TABLE execution_runtime_termination_acceptances "
                    "DISABLE TRIGGER trg_execution_runtime_termination_acceptances_append_only"
                )
            )
            session.execute(
                text(
                    """
                    UPDATE execution_runtime_termination_acceptances
                       SET node_termination_receipt_json = jsonb_set(
                             node_termination_receipt_json,
                             '{termination_evidence,prelaunch_absence_epoch}',
                             '1'::jsonb)
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {"attempt_id": claim.snapshot.attempt_id},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_raw_terminal_receipts_must_exactly_cover_manifest_entries(monkeypatch, tmp_path) -> None:
    _prepared_case, _allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    runtime.finish(exit_code=0)
    assert agent.run_once().outcome is NodeRunOutcome.COLLECTED
    sessions = session_factory()
    with pytest.raises(DBAPIError, match="receipt projection is not canonical"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    "ALTER TABLE execution_qualification_terminal_acceptances "
                    "DISABLE TRIGGER "
                    "trg_execution_qualification_terminal_acceptances_append_only"
                )
            )
            session.execute(
                text(
                    """
                    UPDATE execution_qualification_terminal_acceptances
                       SET artifact_verified_receipt_sha256s_json = '[]'::jsonb,
                           artifact_verified_receipts_json = '[]'::jsonb,
                           terminal_submission_json = jsonb_set(
                             terminal_submission_json,
                             '{artifact_verified_receipt_sha256s}', '[]'::jsonb),
                           accepted_terminal_submission_json = jsonb_set(
                             accepted_terminal_submission_json,
                             '{artifact_verified_receipt_sha256s}', '[]'::jsonb)
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {"attempt_id": claim.snapshot.attempt_id},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_runtime_fence_rebind_json_is_exactly_bound_to_adoption(monkeypatch, tmp_path) -> None:
    _prepared_case, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    expired = claim.snapshot.lease_expires_at + timedelta(microseconds=1)
    assert expired < claim.snapshot.hard_deadline
    runtime.clock.current = expired
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: expired)
    reconciled = allocator.reconcile_expired()
    assert reconciled and reconciled[0].status == "reconciliation_required"

    adopted = agent.run_once()
    assert adopted.outcome is NodeRunOutcome.ADOPTED
    assert adopted.runtime_fence_rebind_receipt is not None
    sessions = session_factory()
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(_ExecutionRuntimeFenceRebindRecord))
            == 1
        )

    with pytest.raises(DBAPIError, match="rebind and adoption are not one-to-one"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    "ALTER TABLE execution_runtime_fence_rebinds "
                    "DISABLE TRIGGER trg_execution_runtime_fence_rebinds_append_only"
                )
            )
            session.execute(
                text(
                    """
                    UPDATE execution_runtime_fence_rebinds
                       SET request_json = jsonb_set(
                             request_json,
                             '{expected_runtime_control_journal_sha256}',
                             to_jsonb(CAST(:forged_journal AS text)))
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {
                    "attempt_id": claim.snapshot.attempt_id,
                    "forged_journal": _digest("forged-rebind-journal"),
                },
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    _assert_raw_runtime_v2_mutation_rejected(
        table="execution_runtime_fence_rebinds",
        update_sql="""
          UPDATE execution_runtime_fence_rebinds
             SET accepted_at = rebound_at + interval '61 seconds',
                 receipt_json = jsonb_set(
                   receipt_json, '{signed_at}',
                   to_jsonb(rebound_at + interval '61 seconds'))
           WHERE attempt_id = :attempt_id
        """,
        params={"attempt_id": claim.snapshot.attempt_id},
        match="runtime fence rebind and adoption are not one-to-one",
    )


def test_raw_absence_evidence_cannot_claim_terminal_fields(monkeypatch, tmp_path) -> None:
    _prepared_case, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path, start=False
    )
    original_authorize = allocator.authorize_runtime_start

    def commit_then_crash(**scope: object) -> None:
        original_authorize(**scope)
        raise SystemExit("crash before saving start authority")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="saving start authority"):
        agent.run_once()
    monkeypatch.setattr(allocator, "authorize_runtime_start", original_authorize)
    late = claim.snapshot.hard_deadline + timedelta(seconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    assert allocator.reconcile_expired()
    assert agent.run_once().outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED

    sessions = session_factory()
    with pytest.raises(DBAPIError, match="pre-runtime absence JSON"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    "ALTER TABLE execution_pre_runtime_absence_decisions "
                    "DISABLE TRIGGER trg_execution_pre_runtime_absence_decisions_append_only"
                )
            )
            session.execute(
                text(
                    """
                    UPDATE execution_pre_runtime_absence_decisions
                       SET absence_receipt_json = jsonb_set(
                             absence_receipt_json,
                             '{absence_evidence,exit_code}',
                             '0'::jsonb)
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {"attempt_id": claim.snapshot.attempt_id},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_raw_launch_recovery_and_runtime_pin_windows_are_exact(monkeypatch, tmp_path) -> None:
    _prepared_case, _allocator, _adapter, _agent, _runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    attempt_id = claim.snapshot.attempt_id
    launch_mutations = (
        """
        UPDATE execution_runtime_launch_receipts
           SET recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{hard_deadline}',
                 to_jsonb(recovery_expires_at))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_launch_receipts
           SET recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{issued_at}',
                 to_jsonb(recovery_expires_at))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_launch_receipts
           SET recovery_expires_at =
                 LEAST(
                   (runtime_control_pin_json->>'expires_at')::timestamptz,
                   COALESCE(
                     (runtime_control_pin_json->>'revoked_at')::timestamptz,
                     (runtime_control_pin_json->>'expires_at')::timestamptz)) +
                   interval '1 microsecond',
               recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{recovery_expires_at}',
                 to_jsonb(
                   LEAST(
                     (runtime_control_pin_json->>'expires_at')::timestamptz,
                     COALESCE(
                       (runtime_control_pin_json->>'revoked_at')::timestamptz,
                       (runtime_control_pin_json->>'expires_at')::timestamptz)) +
                     interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
    )
    for update_sql in launch_mutations:
        _assert_raw_runtime_v2_mutation_rejected(
            table="execution_runtime_launch_receipts",
            update_sql=update_sql,
            params={"attempt_id": attempt_id},
            match="runtime launch/recovery authority is incomplete",
        )


def test_raw_prelaunch_authority_rejects_invalid_pin_boundaries_and_quota_hash(
    monkeypatch, tmp_path
) -> None:
    _prepared_case, allocator, _adapter, agent, _runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path, start=False
    )
    original_authorize = allocator.authorize_runtime_start

    def commit_then_crash(**scope: object) -> None:
        original_authorize(**scope)
        raise SystemExit("crash after persisted prelaunch authority")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="persisted prelaunch authority"):
        agent.run_once()
    attempt_id = claim.snapshot.attempt_id

    _assert_raw_runtime_v2_mutation_rejected(
        table="execution_runtime_launch_authorizations",
        update_sql="""
          UPDATE execution_runtime_launch_authorizations
             SET runtime_control_pin_json = jsonb_set(
                   runtime_control_pin_json, '{expires_at}',
                   runtime_control_pin_json->'valid_from')
           WHERE attempt_id = :attempt_id
        """,
        params={"attempt_id": attempt_id},
        match="runtime launch authority JSON is not the closed schema",
    )
    _assert_raw_runtime_v2_mutation_rejected(
        table="execution_runtime_launch_authorizations",
        update_sql="""
          UPDATE execution_runtime_launch_authorizations
             SET runtime_control_pin_json = jsonb_set(
                   runtime_control_pin_json, '{revoked_at}', to_jsonb(issued_at))
           WHERE attempt_id = :attempt_id
        """,
        params={"attempt_id": attempt_id},
        match="runtime launch authorization lineage is rebound",
    )
    _assert_raw_runtime_v2_mutation_rejected(
        table="execution_runtime_preparations",
        update_sql="""
          UPDATE execution_runtime_preparations
             SET payload_json = jsonb_set(
                   payload_json, '{output_quota_provisioning_receipt_sha256}',
                   to_jsonb(CAST('not-a-sha256' AS text)))
           WHERE attempt_id = :attempt_id
        """,
        params={"attempt_id": attempt_id},
        match="runtime preparation differs from exact attempt authority",
    )


def test_raw_absence_receipt_and_decision_time_windows_are_exact(monkeypatch, tmp_path) -> None:
    _prepared_case, allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path, start=False
    )
    original_authorize = allocator.authorize_runtime_start

    def commit_then_crash(**scope: object) -> None:
        original_authorize(**scope)
        raise SystemExit("crash before local start authority save")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="local start authority"):
        agent.run_once()
    monkeypatch.setattr(allocator, "authorize_runtime_start", original_authorize)
    late = claim.snapshot.hard_deadline + timedelta(seconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    assert allocator.reconcile_expired()
    assert agent.run_once().outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    attempt_id = claim.snapshot.attempt_id
    mutations = (
        """
        UPDATE execution_pre_runtime_absence_decisions
           SET absence_receipt_json = jsonb_set(
                 absence_receipt_json, '{absence_evidence,inspected_at}',
                 to_jsonb(
                   (absence_receipt_json->>'signed_at')::timestamptz -
                     interval '61 seconds'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_pre_runtime_absence_decisions
           SET decided_at =
                 (absence_receipt_json->>'signed_at')::timestamptz -
                   interval '1 microsecond',
               decision_json = jsonb_set(
                 decision_json, '{decided_at}',
                 to_jsonb(
                   (absence_receipt_json->>'signed_at')::timestamptz -
                     interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_pre_runtime_absence_decisions
           SET decided_at = (absence_receipt_json->>'expires_at')::timestamptz,
               decision_json = jsonb_set(
                 decision_json, '{decided_at}',
                 absence_receipt_json->'expires_at')
         WHERE attempt_id = :attempt_id
        """,
    )
    for update_sql in mutations:
        _assert_raw_runtime_v2_mutation_rejected(
            table="execution_pre_runtime_absence_decisions",
            update_sql=update_sql,
            params={"attempt_id": attempt_id},
            match="pre-runtime absence decision lacks exact proof/replacement",
        )


def test_raw_challenge_termination_and_terminal_windows_are_exact(monkeypatch, tmp_path) -> None:
    _prepared_case, _allocator, _adapter, agent, runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    runtime.finish(exit_code=0)
    assert agent.run_once().outcome is NodeRunOutcome.COLLECTED
    attempt_id = claim.snapshot.attempt_id

    challenge_mutations = (
        """
        UPDATE execution_runtime_termination_challenges
           SET inspection_evidence_json = jsonb_set(
                 inspection_evidence_json, '{inspected_at}',
                 to_jsonb(challenged_at + interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_challenges
           SET challenge_json = jsonb_set(
                 challenge_json, '{artifact_submission_deadline}',
                 to_jsonb(
                   LEAST(
                     (runtime_control_pin_json->>'expires_at')::timestamptz,
                     COALESCE(
                       (runtime_control_pin_json->>'revoked_at')::timestamptz,
                       (runtime_control_pin_json->>'expires_at')::timestamptz)) +
                     interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_challenges
           SET runtime_control_pin_json = jsonb_set(
                 runtime_control_pin_json, '{revoked_at}', to_jsonb(challenged_at))
         WHERE attempt_id = :attempt_id
        """,
    )
    for update_sql in challenge_mutations:
        _assert_raw_runtime_v2_mutation_rejected(
            table="execution_runtime_termination_challenges",
            update_sql=update_sql,
            params={"attempt_id": attempt_id},
            match="runtime termination challenge differs from launch lineage",
        )

    termination_mutations = (
        """
        UPDATE execution_runtime_termination_acceptances
           SET node_termination_receipt_json = jsonb_set(
                 node_termination_receipt_json, '{signed_at}',
                 to_jsonb(
                   (node_termination_receipt_json->'termination_evidence'->>
                     'inspected_at')::timestamptz + interval '61 seconds')),
               accepted_termination_json = jsonb_set(
                 accepted_termination_json, '{proof_signed_at}',
                 to_jsonb(
                   (node_termination_receipt_json->'termination_evidence'->>
                     'inspected_at')::timestamptz + interval '61 seconds'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET node_termination_receipt_json = jsonb_set(
                 node_termination_receipt_json, '{expires_at}',
                 to_jsonb(
                   (SELECT c.expires_at + interval '1 microsecond'
                      FROM execution_runtime_termination_challenges c
                     WHERE c.challenge_sha256 =
                       execution_runtime_termination_acceptances.challenge_sha256))),
               accepted_termination_json = jsonb_set(
                 accepted_termination_json, '{proof_expires_at}',
                 to_jsonb(
                   (SELECT c.expires_at + interval '1 microsecond'
                      FROM execution_runtime_termination_challenges c
                     WHERE c.challenge_sha256 =
                       execution_runtime_termination_acceptances.challenge_sha256)))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET accepted_termination_json = jsonb_set(
                 accepted_termination_json, '{proof_signed_at}',
                 to_jsonb(
                   (node_termination_receipt_json->>'signed_at')::timestamptz +
                     interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET accepted_termination_json = jsonb_set(
                 accepted_termination_json, '{billable_ended_at}',
                 to_jsonb(accepted_at + interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{admission_sha256}',
                 to_jsonb(CAST(:forged_admission AS text)))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{issued_at}',
                 recovery_grant_json->'recovery_expires_at')
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_runtime_termination_acceptances
           SET recovery_expires_at =
                 LEAST(
                   (runtime_control_pin_json->>'expires_at')::timestamptz,
                   COALESCE(
                     (runtime_control_pin_json->>'revoked_at')::timestamptz,
                     (runtime_control_pin_json->>'expires_at')::timestamptz)) +
                   interval '1 microsecond',
               recovery_grant_json = jsonb_set(
                 recovery_grant_json, '{recovery_expires_at}',
                 to_jsonb(
                   LEAST(
                     (runtime_control_pin_json->>'expires_at')::timestamptz,
                     COALESCE(
                       (runtime_control_pin_json->>'revoked_at')::timestamptz,
                       (runtime_control_pin_json->>'expires_at')::timestamptz)) +
                     interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
    )
    for update_sql in termination_mutations:
        _assert_raw_runtime_v2_mutation_rejected(
            table="execution_runtime_termination_acceptances",
            update_sql=update_sql,
            params={
                "attempt_id": attempt_id,
                "forged_admission": _digest("forged-termination-recovery-admission"),
            },
            match="accepted runtime termination differs from full proof",
        )

    terminal_mutations = (
        """
        UPDATE execution_qualification_terminal_acceptances
           SET terminal_submission_json = jsonb_set(
                 terminal_submission_json, '{submitted_at}',
                 to_jsonb(accepted_at + interval '1 microsecond')),
               accepted_terminal_submission_json = jsonb_set(
                 accepted_terminal_submission_json, '{node_submitted_at}',
                 to_jsonb(accepted_at + interval '1 microsecond'))
         WHERE attempt_id = :attempt_id
        """,
        """
        UPDATE execution_qualification_terminal_acceptances
           SET accepted_at =
                 (accepted_terminal_submission_json->>
                   'artifact_submission_deadline')::timestamptz,
               accepted_terminal_submission_json = jsonb_set(
                 accepted_terminal_submission_json, '{accepted_at}',
                 accepted_terminal_submission_json->'artifact_submission_deadline')
         WHERE attempt_id = :attempt_id
        """,
    )
    for update_sql in terminal_mutations:
        _assert_raw_runtime_v2_mutation_rejected(
            table="execution_qualification_terminal_acceptances",
            update_sql=update_sql,
            params={"attempt_id": attempt_id},
            match="terminal artifact acceptance differs from full proof",
        )


def test_runtime_v2_authority_rows_are_append_only(monkeypatch, tmp_path) -> None:
    _prepared_case, _allocator, _adapter, agent, runtime, _state, _claim = _running_v2(
        monkeypatch, tmp_path
    )
    runtime.finish(exit_code=0)
    assert agent.run_once().outcome is NodeRunOutcome.COLLECTED
    sessions = session_factory()
    table_keys = (
        ("execution_runtime_preparations", "preparation_sha256"),
        ("execution_runtime_launch_authorizations", "authorization_sha256"),
        ("execution_runtime_launch_receipts", "launch_receipt_sha256"),
        ("execution_runtime_termination_challenges", "challenge_sha256"),
        ("execution_runtime_termination_acceptances", "accepted_termination_sha256"),
        (
            "execution_qualification_terminal_acceptances",
            "accepted_terminal_submission_sha256",
        ),
    )
    for table, key in table_keys:
        with pytest.raises(DBAPIError, match="append-only"):
            with sessions() as session, session.begin():
                session.execute(text(f"UPDATE {table} SET {key} = {key}"))


def test_runtime_v2_post_launch_auth_and_absence_heads_cannot_advance_raw(
    monkeypatch, tmp_path
) -> None:
    _prepared_case, _allocator, _adapter, _agent, _runtime, _state, claim = _running_v2(
        monkeypatch, tmp_path
    )
    sessions = session_factory()
    forged_authorization = _digest("raw-post-launch-authorization")
    forged_request = _digest("raw-post-launch-request")
    with pytest.raises(DBAPIError, match="authorization head is non-monotonic"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO execution_runtime_launch_authorizations (
                      authorization_sha256, attempt_id, preparation_sha256, sequence,
                      request_sha256, pre_runtime_absence_epoch,
                      pre_runtime_absence_receipt_sha256, request_payload_sha256,
                      request_json, authorization_payload_sha256, authorization_json,
                      runtime_control_pin_sha256, runtime_control_pin_json,
                      issued_at, expires_at, recorded_at
                    )
                    SELECT :authorization, attempt_id, preparation_sha256, sequence + 1,
                           :request, pre_runtime_absence_epoch,
                           pre_runtime_absence_receipt_sha256, :request,
                           request_json, :authorization, authorization_json,
                           runtime_control_pin_sha256, runtime_control_pin_json,
                           issued_at, expires_at, recorded_at
                      FROM execution_runtime_launch_authorizations
                     WHERE attempt_id = :attempt_id
                     ORDER BY sequence DESC LIMIT 1
                    """
                ),
                {
                    "authorization": forged_authorization,
                    "request": forged_request,
                    "attempt_id": claim.snapshot.attempt_id,
                },
            )
            session.execute(
                text(
                    """
                    UPDATE execution_attempts
                       SET runtime_launch_authorization_count =
                             runtime_launch_authorization_count + 1,
                           latest_runtime_launch_authorization_sha256 = :authorization,
                           state_version = state_version + 1
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {
                    "authorization": forged_authorization,
                    "attempt_id": claim.snapshot.attempt_id,
                },
            )

    forged_decision = _digest("raw-post-launch-absence-decision")
    forged_absence = _digest("raw-post-launch-absence-receipt")
    with pytest.raises(DBAPIError, match="absence head is non-monotonic"):
        with sessions() as session, session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO execution_pre_runtime_absence_decisions (
                      decision_sha256, attempt_id, absence_epoch,
                      absence_receipt_sha256, preparation_sha256,
                      prior_authorization_request_sha256,
                      prior_authorization_sha256, absence_payload_sha256,
                      absence_receipt_json, disposition,
                      replacement_request_sha256, replacement_authorization_sha256,
                      decision_json, runtime_control_pin_sha256,
                      runtime_control_pin_json, decided_at
                    )
                    SELECT :decision, attempt_id, 1, :absence, preparation_sha256,
                           request_sha256, authorization_sha256, :absence,
                           '{}'::jsonb, 'released', NULL, NULL, '{}'::jsonb,
                           runtime_control_pin_sha256, runtime_control_pin_json,
                           recorded_at
                      FROM execution_runtime_launch_authorizations
                     WHERE attempt_id = :attempt_id
                     ORDER BY sequence DESC LIMIT 1
                    """
                ),
                {
                    "decision": forged_decision,
                    "absence": forged_absence,
                    "attempt_id": claim.snapshot.attempt_id,
                },
            )
            session.execute(
                text(
                    """
                    UPDATE execution_attempts
                       SET pre_runtime_absence_count = pre_runtime_absence_count + 1,
                           latest_pre_runtime_absence_receipt_sha256 = :absence,
                           state_version = state_version + 1
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {
                    "absence": forged_absence,
                    "attempt_id": claim.snapshot.attempt_id,
                },
            )


def test_runtime_v2_json_string_array_helper_rejects_noncanonical_values() -> None:
    sessions = session_factory()
    first = _digest("array-first")
    second = _digest("array-second")
    ordered = sorted((first, second))
    cases = (
        (["ok", 7], False, False, False),
        ([first, first], True, True, False),
        (list(reversed(ordered)), True, True, False),
        (ordered, True, True, True),
    )
    with sessions() as session:
        for value, require_sha256, require_canonical, expected in cases:
            actual = session.execute(
                text(
                    "SELECT aletheia_execution_json_string_array("
                    "CAST(:value AS jsonb), :require_sha256, :require_canonical)"
                ),
                {
                    "value": json.dumps(value),
                    "require_sha256": require_sha256,
                    "require_canonical": require_canonical,
                },
            ).scalar_one()
            assert actual is expected
