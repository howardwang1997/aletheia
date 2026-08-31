from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

import aletheia.execution.allocator as allocator_module
from aletheia.db import session_factory
from aletheia.execution.allocator import (
    AdmissionConflict,
    LeaseAuthorityError,
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
    QualificationAssignmentDelivery,
    ReservationSnapshot,
    RuntimeLaunchCommit,
    RuntimeProofReplayRejected,
    RuntimeProofReplayRejectionCode,
    RuntimeStartCommit,
)
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.assignment_contracts import (
    NodeAssignmentTransportPin,
    QualificationAssignmentSecret,
    node_transport_key_id,
    seal_qualification_assignment,
    x25519_public_key_hex,
)
from aletheia.execution.node_agent import (
    AssignmentRejected,
    LocalStateError,
    NodeLeaseRejected,
    NodeLocalStateStore,
    NodeAllocatorPort,
    NodeProofReplayRejected,
    NodeProofReplayRejectionCode,
    NodeReservation,
    NodeRunOutcome,
    NodeRunResult,
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedLaunchRegistry,
    PinnedLaunchSpec,
    QualificationNodeAgent,
    RuntimeRejected,
)
from aletheia.execution.postgresql_node_adapter import (
    PostgreSQLNodeAllocatorAdapter,
    QualificationExecutionWorker,
)
from aletheia.execution.runtime_contracts import (
    QualificationAuthorityVerifier,
    TerminalVerificationAuthorityVerifier,
    VerifiedInputArtifactResolution,
    qualification_key_id,
)
from aletheia.execution.runtime_v2_contracts import (
    HistoricalPreRuntimeRecoveryLineage,
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    PinnedInputPath,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeLaunchAuthorizationRequest,
    RuntimePreparation,
    issue_accepted_qualification_terminal_submission,
    issue_accepted_runtime_termination,
    issue_historical_runtime_recovery_grant,
    issue_qualification_terminal_deadline_expiration,
    issue_runtime_launch_authorization,
    issue_runtime_termination_acceptance_challenge,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_allocator import (  # noqa: E402
    TRANSPORT_PRIVATE_KEY as ALLOCATOR_TRANSPORT_PRIVATE_KEY,
    _prepared,
)
from test_runtime_contracts import (  # noqa: E402
    PRIVATE_KEY,
    _AuthorityResolver,
    _digest,
    _qualification_case,
    _worker_authority,
)
from test_node_agent import _Clock as _NodeClock  # noqa: E402
from test_node_agent import (  # noqa: E402
    _Harness,
    _InputMaterializer,
    _OutputQuotaProvisioner,
    _Runtime,
)
from test_node_agent import _runtime_control_pin  # noqa: E402
from test_node_agent import (  # noqa: E402
    RUNTIME_CONTROL_PRIVATE_KEY as NODE_RUNTIME_CONTROL_PRIVATE_KEY,
)

UTC = timezone.utc
TRANSPORT_PRIVATE_KEY = bytes.fromhex("91" * 32)
RUNTIME_CONTROL_PRIVATE_KEY = bytes.fromhex("92" * 32)
RAW_TOKEN = "A" * 43
_ADAPTER_DATABASE_NAME = "aletheia_pr4b_adapter_final_20260824_01"


@dataclass(frozen=True)
class _Clock:
    observed_at: datetime

    def now(self) -> datetime:
        return self.observed_at


class _DeliveryAllocator:
    def __init__(self, delivery: object | None) -> None:
        self.delivery = delivery
        self.pulls: list[tuple[str, str]] = []

    def pull_assignment_delivery(self, *, node_id: str, node_manifest_sha256: str):
        self.pulls.append((node_id, node_manifest_sha256))
        return self.delivery


class _RuntimeControlIssuer:
    """Test key custody implementing the public runtime-control issuance port."""

    def __init__(self, *, observed_at: datetime) -> None:
        public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
        self._pin = RuntimeControlAuthorityPin(
            policy_sha256=_digest("adapter-integration-runtime-control-policy"),
            principal_id="principal:adapter-integration-runtime-control",
            key_id=qualification_key_id(public_key),
            public_key_ed25519_hex=public_key,
            valid_from=observed_at - timedelta(days=1),
            expires_at=observed_at + timedelta(days=1),
        )
        self._verifier = RuntimeControlAuthorityVerifier(self._pin)

    @property
    def authority_pin(self) -> RuntimeControlAuthorityPin:
        return self._pin

    @property
    def authority_verifier(self) -> RuntimeControlAuthorityVerifier:
        return self._verifier

    def issue_launch_authorization(
        self,
        *,
        authorization_request,
        preparation,
        admission_sha256,
        qualification_grant_sha256,
        lease_expires_at,
        hard_deadline,
        issued_at,
        expires_at,
        max_launch_delay_ns,
    ):
        return issue_runtime_launch_authorization(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            admission_sha256=admission_sha256,
            qualification_grant_sha256=qualification_grant_sha256,
            node_manifest_sha256=preparation.node_manifest_sha256,
            node_id=preparation.node_id,
            boot_id=preparation.boot_id,
            execution_id=preparation.execution_id,
            infrastructure_attempt_id=preparation.infrastructure_attempt_id,
            intent_sha256=preparation.intent_sha256,
            runtime_preparation_sha256=preparation.preparation_sha256,
            authorization_request_sha256=authorization_request.request_sha256,
            launch_spec_sha256=preparation.launch_spec_sha256,
            oci_config_sha256=preparation.oci_config_sha256,
            workload_executable_sha256=preparation.workload_executable_sha256,
            workload_argv=preparation.workload_argv,
            enforced_placement_sha256=preparation.enforced_placement_sha256,
            input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
            fencing_epoch=preparation.fencing_epoch,
            lease_token_sha256=preparation.lease_token_sha256,
            lease_expires_at=lease_expires_at,
            hard_deadline=hard_deadline,
            issued_at=issued_at,
            expires_at=expires_at,
            max_launch_delay_ns=max_launch_delay_ns,
        )

    def issue_historical_recovery(self, **scope):
        return issue_historical_runtime_recovery_grant(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            **scope,
        )

    def issue_termination_challenge(
        self,
        *,
        preparation,
        launch_receipt,
        termination_evidence,
        inspection_sequence,
        node_inventory_sha256,
        resource_lease_sha256,
        fencing_epoch,
        lease_token_sha256,
        hard_deadline,
        artifact_submission_deadline,
        challenged_at,
        expires_at,
    ):
        return issue_runtime_termination_acceptance_challenge(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            attempt_id=preparation.infrastructure_attempt_id,
            execution_id=preparation.execution_id,
            intent_sha256=preparation.intent_sha256,
            node_manifest_sha256=preparation.node_manifest_sha256,
            runtime_preparation_sha256=preparation.preparation_sha256,
            node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
            runtime_identity_sha256=(launch_receipt.launch_evidence.runtime_identity_sha256),
            runtime_inspection_evidence_sha256=termination_evidence.inspection_sha256,
            inspection_sequence=inspection_sequence,
            node_inventory_sha256=node_inventory_sha256,
            resource_lease_sha256=resource_lease_sha256,
            fencing_epoch=fencing_epoch,
            lease_token_sha256=lease_token_sha256,
            hard_deadline=hard_deadline,
            artifact_submission_deadline=artifact_submission_deadline,
            challenged_at=challenged_at,
            expires_at=expires_at,
        )

    def issue_accepted_termination(self, **scope):
        return issue_accepted_runtime_termination(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            runtime_authority=self._verifier,
            **scope,
        )

    def issue_terminal_submission_acceptance(self, **scope):
        return issue_accepted_qualification_terminal_submission(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            runtime_authority=self._verifier,
            **scope,
        )

    def issue_terminal_deadline_expiration(self, **scope):
        return issue_qualification_terminal_deadline_expiration(
            pin=self._pin,
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            runtime_authority=self._verifier,
            **scope,
        )


class _PublishingArtifactStore:
    """Use real filesystem custody and publish only its verified public DTOs to the resolver."""

    def __init__(self, root: Path, *, resolver) -> None:
        self._store = LocalArtifactStore(root)
        self._resolver = resolver

    def quarantine_outputs(self, **scope):
        return self._store.quarantine_outputs(**scope)

    def verify_manifest(self, **scope):
        manifest = scope["manifest"]
        receipts = self._store.verify_manifest(**scope)
        self._resolver.add_manifest(manifest)
        for receipt in receipts:
            self._resolver.add_resolution(
                VerifiedInputArtifactResolution(
                    verified_receipt_sha256=receipt.verified_receipt_sha256,
                    verified_receipt=receipt,
                    artifact_manifest=manifest,
                    producer_execution_receipt=None,
                    content_rehash_sha256=receipt.artifact.content_sha256,
                    content_bytes=receipt.artifact.bytes,
                    custody_reverified=True,
                    resolved_by_principal_id="principal:adapter-artifact-resolver",
                    resolved_at=receipt.verified_at,
                )
            )
        return receipts


@pytest.fixture
def _isolated_adapter_database():
    database_url = os.environ.get("ALETHEIA_DATABASE_URL", "")
    try:
        parsed_url = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        parsed_url = None
    if (
        parsed_url is None
        or parsed_url.get_backend_name() != "postgresql"
        or parsed_url.database != _ADAPTER_DATABASE_NAME
        or parsed_url.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.skip("requires the explicitly isolated PR4b adapter PostgreSQL database")
    sessions = session_factory()
    truncate = text(
        "TRUNCATE execution_nodes, execution_heads, execution_budget_authorizations, "
        "execution_qualification_admissions RESTART IDENTITY CASCADE"
    )
    with sessions() as session, session.begin():
        session.execute(truncate)
    yield
    with sessions() as session, session.begin():
        session.execute(truncate)


def _public_key_hex(private_key: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _fixture(tmp_path):
    case = _qualification_case()
    intent = case.bundle.intent
    observed_at = case.observed_at
    node_id = "node.qualification-adapter-01"
    node_manifest_sha256 = _digest("adapter-node-manifest")
    public_key = x25519_public_key_hex(TRANSPORT_PRIVATE_KEY)
    pin = NodeAssignmentTransportPin(
        node_id=node_id,
        node_manifest_sha256=node_manifest_sha256,
        transport_policy_sha256=_digest("adapter-transport-policy"),
        transport_principal_id="principal:adapter-transport",
        transport_key_id=node_transport_key_id(public_key),
        public_key_x25519_hex=public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )
    hard_deadline = observed_at + timedelta(minutes=1)
    token_sha256 = hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest()
    snapshot = ReservationSnapshot(
        execution_id=intent.execution_id,
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        attempt_number=intent.infrastructure_attempt.attempt_number,
        intent_sha256=intent.intent_sha256,
        admission_sha256=_digest("adapter-admission"),
        grant_sha256=case.grant.grant_sha256,
        bundle_sha256=case.bundle.bundle_sha256,
        node_id=node_id,
        node_inventory_sha256=_digest("adapter-inventory"),
        status="reserved",
        state_version=1,
        fencing_epoch=1,
        lease_token_sha256=token_sha256,
        resource_lease_sha256=_digest("adapter-resource-lease"),
        selected_resource_ids=case.bundle.cost_quote.selected_resource_ids,
        cpu_cores=intent.resource_request.cpu_cores,
        memory_bytes=intent.resource_request.memory_bytes,
        scratch_bytes=intent.resource_request.scratch_bytes,
        exclusive=intent.resource_request.exclusive,
        device_leases=(),
        budget_authorization_sha256=case.bundle.budget_authorization.authorization_sha256,
        cost_quote_sha256=case.bundle.cost_quote.quote_sha256,
        currency_code=case.bundle.cost_quote.currency_code,
        held_microunits=case.bundle.cost_quote.maximum_charge_microunits,
        reserved_at=observed_at - timedelta(seconds=1),
        lease_expires_at=observed_at + timedelta(seconds=30),
        hard_deadline=hard_deadline,
        reconciliation_reason=None,
    )
    secret = QualificationAssignmentSecret(
        infrastructure_attempt_id=snapshot.attempt_id,
        admission_sha256=snapshot.admission_sha256,
        grant_sha256=snapshot.grant_sha256,
        bundle_sha256=snapshot.bundle_sha256,
        node_id=node_id,
        node_manifest_sha256=node_manifest_sha256,
        resource_lease_sha256=snapshot.resource_lease_sha256,
        fencing_epoch=snapshot.fencing_epoch,
        lease_token=RAW_TOKEN,
        lease_token_sha256=token_sha256,
        issued_at=observed_at - timedelta(seconds=1),
        expires_at=hard_deadline,
    )
    envelope = seal_qualification_assignment(secret=secret, transport_pin=pin)
    delivery = QualificationAssignmentDelivery(
        bundle=case.bundle,
        grant=case.grant,
        snapshot=snapshot,
        sealed_envelope=envelope,
        historical_recovery_grant=None,
    )
    allocator = _DeliveryAllocator(delivery)
    custody = NodeLocalStateStore(tmp_path / "node-state")
    adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        token_custody=custody,
        clock=_Clock(observed_at),
    )
    return case, snapshot, pin, allocator, custody, adapter


def _real_v2_composition(monkeypatch, tmp_path, *, start: bool = True):
    # Runtime import avoids the intentional test-helper cycle: allocator-v2 imports this module's
    # public composition issuer/store, while these adapter tests reuse its frozen PG harness.
    from test_allocator_v2 import _running_v2  # noqa: PLC0415

    return _running_v2(monkeypatch, tmp_path, start=start)


def test_initial_delivery_decrypts_directly_into_durable_token_custody(tmp_path) -> None:
    case, snapshot, pin, allocator, custody, adapter = _fixture(tmp_path)

    assignment = adapter.pull_qualification_assignment(
        node_id=pin.node_id,
        node_manifest_sha256=pin.node_manifest_sha256,
    )

    assert assignment is not None
    assert assignment.intent == case.bundle.intent
    assert assignment.work_order_node.node_id == case.bundle.intent.work_order_node_id
    assert assignment.qualification_grant == case.grant
    assert isinstance(assignment.reservation, NodeReservation)
    assert assignment.reservation.attempt_id == snapshot.attempt_id
    assert assignment.lease_token is None
    assert assignment.historical_recovery_grant is None
    assert (
        custody.load_token(
            attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            expected_sha256=snapshot.lease_token_sha256,
        )
        == RAW_TOKEN
    )
    assert allocator.pulls == [(pin.node_id, pin.node_manifest_sha256)]

    # Ciphertext replay is idempotent and still never exposes the raw token in the DTO.
    replay = adapter.pull_qualification_assignment(
        node_id=pin.node_id,
        node_manifest_sha256=pin.node_manifest_sha256,
    )
    assert replay == assignment
    assert RAW_TOKEN not in repr(replay)


def test_assignment_pull_rejects_rebound_scope_before_token_custody(tmp_path) -> None:
    _case, snapshot, pin, allocator, custody, adapter = _fixture(tmp_path)
    assert isinstance(allocator.delivery, QualificationAssignmentDelivery)
    allocator.delivery = replace(
        allocator.delivery,
        snapshot=replace(snapshot, resource_lease_sha256=_digest("rebound-resource-lease")),
    )

    with pytest.raises(AssignmentRejected, match="differs from allocator reservation"):
        adapter.pull_qualification_assignment(
            node_id=pin.node_id,
            node_manifest_sha256=pin.node_manifest_sha256,
        )
    with pytest.raises(LocalStateError):
        custody.load_token(
            attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            expected_sha256=snapshot.lease_token_sha256,
        )


def test_recovery_delivery_requires_existing_token_and_never_reopens_envelope(tmp_path) -> None:
    case, snapshot, pin, allocator, custody, adapter = _fixture(tmp_path)
    custody.save_token(
        attempt_id=snapshot.attempt_id,
        fencing_epoch=snapshot.fencing_epoch,
        token=RAW_TOKEN,
    )
    public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    runtime_pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("adapter-runtime-control-policy"),
        principal_id="principal:adapter-runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=case.observed_at - timedelta(days=1),
        expires_at=case.observed_at + timedelta(days=1),
    )
    recovery = issue_historical_runtime_recovery_grant(
        pin=runtime_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=snapshot.admission_sha256,
        qualification_grant_sha256=snapshot.grant_sha256,
        intent_sha256=snapshot.intent_sha256,
        execution_id=snapshot.execution_id,
        infrastructure_attempt_id=snapshot.attempt_id,
        runtime_preparation_sha256=_digest("adapter-runtime-preparation"),
        node_runtime_launch_receipt_sha256=_digest("adapter-launch-receipt"),
        accepted_runtime_termination_sha256=None,
        admitted_at=case.grant.message.authorized_at,
        hard_deadline=snapshot.hard_deadline,
        issued_at=case.observed_at,
        recovery_expires_at=case.observed_at + timedelta(hours=1),
    )
    allocator.delivery = QualificationAssignmentDelivery(
        bundle=case.bundle,
        grant=case.grant,
        snapshot=replace(snapshot, status="reconciliation_required"),
        sealed_envelope=None,
        historical_recovery_grant=recovery,
    )

    assignment = adapter.pull_qualification_assignment(
        node_id=pin.node_id,
        node_manifest_sha256=pin.node_manifest_sha256,
    )

    assert assignment is not None
    assert assignment.lease_token is None
    assert assignment.historical_recovery_grant == recovery

    empty_custody = NodeLocalStateStore(tmp_path / "empty-node-state")
    empty_adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        token_custody=empty_custody,
        clock=_Clock(case.observed_at),
    )
    with pytest.raises(LocalStateError):
        empty_adapter.pull_qualification_assignment(
            node_id=pin.node_id,
            node_manifest_sha256=pin.node_manifest_sha256,
        )


def test_pre_runtime_delivery_projects_historical_cleanup_lineage_without_token(
    tmp_path,
) -> None:
    case, snapshot, pin, allocator, custody, _adapter = _fixture(tmp_path)
    custody.save_token(
        attempt_id=snapshot.attempt_id,
        fencing_epoch=snapshot.fencing_epoch,
        token=RAW_TOKEN,
    )
    preparation = RuntimePreparation(
        node_manifest_sha256=pin.node_manifest_sha256,
        node_id=snapshot.node_id,
        boot_id="boot.adapter-01",
        execution_id=snapshot.execution_id,
        infrastructure_attempt_id=snapshot.attempt_id,
        intent_sha256=snapshot.intent_sha256,
        runtime_id="qual.adapter-prelaunch",
        runtime_engine="containerd/2",
        launch_spec_sha256=_digest("adapter-prelaunch-spec"),
        workload_executable_sha256=_digest("adapter-prelaunch-executable"),
        workload_argv=("/opt/aletheia/bin/qualified",),
        runtime_request_sha256=_digest("adapter-prelaunch-request"),
        enforced_placement_sha256=_digest("adapter-prelaunch-placement"),
        input_materialization_receipt_sha256=_digest("adapter-prelaunch-inputs"),
        output_quota_provisioning_receipt_sha256=_digest("adapter-prelaunch-output-quota"),
        fencing_epoch=snapshot.fencing_epoch,
        lease_token_sha256=snapshot.lease_token_sha256,
        prepared_runtime_locator_sha256=_digest("adapter-prelaunch-locator"),
        oci_config_sha256=_digest("adapter-prelaunch-oci"),
        prepared_at=case.observed_at - timedelta(seconds=4),
        prepared_monotonic_ns=1,
    )
    request = RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("adapter-prelaunch-nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=snapshot.attempt_id,
        fencing_epoch=snapshot.fencing_epoch,
        lease_token_sha256=snapshot.lease_token_sha256,
        requested_at=case.observed_at - timedelta(seconds=3),
        requested_monotonic_ns=2,
    )
    runtime_public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    runtime_pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("adapter-prelaunch-control-policy"),
        principal_id="principal:adapter-prelaunch-control",
        key_id=qualification_key_id(runtime_public_key),
        public_key_ed25519_hex=runtime_public_key,
        valid_from=case.observed_at - timedelta(days=1),
        expires_at=case.observed_at + timedelta(days=1),
    )
    authorization = issue_runtime_launch_authorization(
        pin=runtime_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=snapshot.admission_sha256,
        qualification_grant_sha256=snapshot.grant_sha256,
        node_manifest_sha256=preparation.node_manifest_sha256,
        node_id=preparation.node_id,
        boot_id=preparation.boot_id,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        intent_sha256=preparation.intent_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        authorization_request_sha256=request.request_sha256,
        launch_spec_sha256=preparation.launch_spec_sha256,
        oci_config_sha256=preparation.oci_config_sha256,
        workload_executable_sha256=preparation.workload_executable_sha256,
        workload_argv=preparation.workload_argv,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        fencing_epoch=snapshot.fencing_epoch,
        lease_token_sha256=snapshot.lease_token_sha256,
        lease_expires_at=snapshot.lease_expires_at,
        hard_deadline=snapshot.hard_deadline,
        issued_at=case.observed_at - timedelta(seconds=2),
        expires_at=case.observed_at + timedelta(seconds=1),
        max_launch_delay_ns=5_000_000_000,
    )
    lineage = HistoricalPreRuntimeRecoveryLineage(
        runtime_preparation=preparation,
        runtime_launch_authorization_request=request,
        runtime_launch_authorization=authorization,
    )
    allocator.delivery = QualificationAssignmentDelivery(
        bundle=case.bundle,
        grant=case.grant,
        snapshot=replace(snapshot, status="reconciliation_required"),
        historical_pre_runtime_recovery_lineage=lineage,
    )
    adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        token_custody=custody,
        clock=_Clock(snapshot.hard_deadline + timedelta(minutes=1)),
    )

    assignment = adapter.pull_qualification_assignment(
        node_id=pin.node_id,
        node_manifest_sha256=pin.node_manifest_sha256,
    )

    assert assignment is not None
    assert assignment.lease_token is None
    assert assignment.historical_recovery_grant is None
    assert assignment.historical_pre_runtime_recovery_lineage == lineage
    assert RAW_TOKEN not in repr(assignment)


def test_reservation_projection_drops_budget_fields_and_preserves_exact_placement(tmp_path) -> None:
    _case, snapshot, _pin, _allocator, _custody, _adapter = _fixture(tmp_path)

    projected = PostgreSQLNodeAllocatorAdapter.project_reservation(snapshot)

    assert projected.selected_resource_ids == snapshot.selected_resource_ids
    assert projected.lease_token_sha256 == snapshot.lease_token_sha256
    assert not hasattr(projected, "held_microunits")
    assert not hasattr(projected, "budget_authorization_sha256")


def test_adapter_method_surface_matches_the_frozen_node_allocator_port() -> None:
    methods = (
        "pull_qualification_assignment",
        "start_attempt",
        "mark_running",
        "heartbeat",
        "retain_reconciliation",
        "resolve_pre_runtime_absence",
        "adopt_attempt",
        "challenge_runtime_termination",
        "accept_runtime_termination",
        "replay_accepted_runtime_termination",
        "submit_terminal_artifacts",
    )
    for name in methods:
        protocol = inspect.signature(getattr(NodeAllocatorPort, name))
        concrete = inspect.signature(getattr(PostgreSQLNodeAllocatorAdapter, name))
        assert tuple(protocol.parameters) == tuple(concrete.parameters)
        assert tuple(item.kind for item in protocol.parameters.values()) == tuple(
            item.kind for item in concrete.parameters.values()
        )


def test_heartbeat_uses_public_allocator_method_and_maps_lease_rejection(tmp_path) -> None:
    _case, snapshot, _pin, allocator, _custody, adapter = _fixture(tmp_path)
    calls: list[tuple[str, str, int]] = []

    def heartbeat(*, attempt_id: str, lease_token: str, fencing_epoch: int):
        calls.append((attempt_id, lease_token, fencing_epoch))
        return SimpleNamespace(snapshot=replace(snapshot, status="running"))

    allocator.heartbeat = heartbeat
    projected = adapter.heartbeat(
        attempt_id=snapshot.attempt_id,
        lease_token=RAW_TOKEN,
        fencing_epoch=snapshot.fencing_epoch,
    )
    assert projected.status == "running"
    assert calls == [(snapshot.attempt_id, RAW_TOKEN, snapshot.fencing_epoch)]

    def reject(**_kwargs):
        raise LeaseAuthorityError("must-not-reflect-raw-token")

    allocator.heartbeat = reject
    with pytest.raises(NodeLeaseRejected, match="allocator rejected heartbeat authority") as caught:
        adapter.heartbeat(
            attempt_id=snapshot.attempt_id,
            lease_token=RAW_TOKEN,
            fencing_epoch=snapshot.fencing_epoch,
        )
    assert "must-not-reflect" not in str(caught.value)
    assert (
        caught.value.allocator_rejection_sha256
        == hashlib.sha256(
            b"ALETHEIA_NODE_ALLOCATOR_REJECTION_V1\x00"
            b"heartbeat\x00"
            b"LeaseAuthorityError\x00"
            b"must-not-reflect-raw-token"
        ).hexdigest()
    )


def test_only_the_exact_allocator_proof_code_becomes_refresh_authority(tmp_path) -> None:
    _case, _snapshot, _pin, _allocator, _custody, adapter = _fixture(tmp_path)

    def reject_terminal():
        raise RuntimeProofReplayRejected(
            RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
        )

    with pytest.raises(NodeProofReplayRejected) as refreshable:
        adapter._lease_call(
            "runtime termination challenge",
            reject_terminal,
            proof_replay_code=(
                RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
            ),
        )
    assert refreshable.value.code is (
        NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
    )

    with pytest.raises(NodeLeaseRejected):
        adapter._lease_call(
            "pre-runtime absence resolution",
            reject_terminal,
            proof_replay_code=(
                RuntimeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED
            ),
        )


def test_start_and_launch_translate_only_v2_public_commits(tmp_path) -> None:
    _case, base, _pin, allocator, _custody, adapter = _fixture(tmp_path / "adapter")
    harness = _Harness(tmp_path / "node")
    running = harness.agent.run_once()
    assert running.runtime_preparation is not None
    assert running.node_runtime_launch_receipt is not None
    assert harness.allocator._start_request is not None
    assert harness.allocator._start_authorization is not None
    assert harness.runtime.request is not None
    assert (
        running.runtime_preparation.output_quota_provisioning_receipt_sha256
        == harness.runtime.request.output_quota_provisioning_receipt.provisioning_receipt_sha256
    )
    reservation = harness.allocator.current
    snapshot = replace(
        base,
        execution_id=reservation.execution_id,
        attempt_id=reservation.attempt_id,
        intent_sha256=reservation.intent_sha256,
        admission_sha256=reservation.admission_sha256,
        grant_sha256=reservation.grant_sha256,
        node_id=reservation.node_id,
        node_inventory_sha256=reservation.node_inventory_sha256,
        status="starting",
        fencing_epoch=reservation.fencing_epoch,
        lease_token_sha256=reservation.lease_token_sha256,
        resource_lease_sha256=reservation.resource_lease_sha256,
        selected_resource_ids=reservation.selected_resource_ids,
        cpu_cores=reservation.cpu_cores,
        memory_bytes=reservation.memory_bytes,
        scratch_bytes=reservation.scratch_bytes,
        exclusive=reservation.exclusive,
        device_leases=(),
        lease_expires_at=reservation.lease_expires_at,
        hard_deadline=reservation.hard_deadline,
    )
    calls: list[str] = []
    start_scopes: list[dict[str, object]] = []

    def authorize_runtime_start(**scope):
        calls.append("authorize_runtime_start")
        start_scopes.append(scope)
        return RuntimeStartCommit(
            snapshot=snapshot,
            launch_authorization=(harness.allocator._start_authorization.launch_authorization),
            replayed=False,
        )

    runtime_public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    runtime_pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("adapter-runtime-control-policy"),
        principal_id="principal:adapter-runtime-control",
        key_id=qualification_key_id(runtime_public_key),
        public_key_ed25519_hex=runtime_public_key,
        valid_from=harness.clock.now() - timedelta(days=1),
        expires_at=harness.clock.now() + timedelta(days=1),
    )
    recovery = issue_historical_runtime_recovery_grant(
        pin=runtime_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=snapshot.admission_sha256,
        qualification_grant_sha256=snapshot.grant_sha256,
        intent_sha256=snapshot.intent_sha256,
        execution_id=snapshot.execution_id,
        infrastructure_attempt_id=snapshot.attempt_id,
        runtime_preparation_sha256=running.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            running.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        accepted_runtime_termination_sha256=None,
        admitted_at=harness.case.grant.message.authorized_at,
        hard_deadline=snapshot.hard_deadline,
        issued_at=harness.clock.now(),
        recovery_expires_at=runtime_pin.active_until,
    )

    def accept_runtime_launch(**_kwargs):
        calls.append("accept_runtime_launch")
        return RuntimeLaunchCommit(
            snapshot=replace(snapshot, status="running"),
            historical_recovery_grant=recovery,
            replayed=False,
        )

    allocator.authorize_runtime_start = authorize_runtime_start
    allocator.accept_runtime_launch = accept_runtime_launch
    start = adapter.start_attempt(
        attempt_id=snapshot.attempt_id,
        lease_token=harness.raw_token,
        fencing_epoch=snapshot.fencing_epoch,
        runtime_preparation=running.runtime_preparation,
        launch_authorization_request=harness.allocator._start_request,
    )
    launched = adapter.mark_running(
        attempt_id=snapshot.attempt_id,
        lease_token=harness.raw_token,
        fencing_epoch=snapshot.fencing_epoch,
        node_runtime_launch_receipt=running.node_runtime_launch_receipt,
    )

    assert start.reservation.status == "starting"
    assert start.launch_authorization == (
        harness.allocator._start_authorization.launch_authorization
    )
    assert start.replayed is False
    assert launched.status == "running"
    assert calls == ["authorize_runtime_start", "accept_runtime_launch"]
    assert start_scopes == [
        {
            "attempt_id": snapshot.attempt_id,
            "lease_token": harness.raw_token,
            "fencing_epoch": snapshot.fencing_epoch,
            "runtime_preparation": running.runtime_preparation,
            "launch_authorization_request": harness.allocator._start_request,
        }
    ]

    harness.clock.current = snapshot.lease_expires_at + timedelta(seconds=1)
    historical_snapshot = replace(snapshot, status="reconciliation_required")

    def replay_historical_start(**_kwargs):
        return RuntimeStartCommit(
            snapshot=historical_snapshot,
            launch_authorization=(harness.allocator._start_authorization.launch_authorization),
            replayed=True,
        )

    allocator.authorize_runtime_start = replay_historical_start
    historical_start = adapter.start_attempt(
        attempt_id=snapshot.attempt_id,
        lease_token=harness.raw_token,
        fencing_epoch=snapshot.fencing_epoch,
        runtime_preparation=running.runtime_preparation,
        launch_authorization_request=harness.allocator._start_request,
    )
    assert historical_start.reservation.status == "reconciliation_required"
    assert historical_start.reservation.lease_expires_at < harness.clock.now()
    assert historical_start.launch_authorization == (
        harness.allocator._start_authorization.launch_authorization
    )
    assert historical_start.replayed is True

    rebound_quota_preparation = running.runtime_preparation.model_copy(
        update={
            "output_quota_provisioning_receipt_sha256": _digest("rebound-output-quota-provisioning")
        }
    )
    with pytest.raises(RuntimeRejected, match="changed exact authority"):
        adapter.start_attempt(
            attempt_id=snapshot.attempt_id,
            lease_token=harness.raw_token,
            fencing_epoch=snapshot.fencing_epoch,
            runtime_preparation=rebound_quota_preparation,
            launch_authorization_request=harness.allocator._start_request,
        )


def test_accepted_termination_replay_uses_only_historical_existing_commit(tmp_path) -> None:
    harness = _Harness(tmp_path / "node")
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())
    accepted = collected.accepted_runtime_termination
    challenge = collected.runtime_termination_challenge
    receipt = collected.node_runtime_termination_receipt
    assert accepted is not None and challenge is not None and receipt is not None

    issuer = _RuntimeControlIssuer(observed_at=accepted.accepted_at)
    recovery = issuer.issue_historical_recovery(
        admission_sha256=harness.reservation.admission_sha256,
        qualification_grant_sha256=harness.reservation.grant_sha256,
        intent_sha256=harness.intent.intent_sha256,
        execution_id=harness.intent.execution_id,
        infrastructure_attempt_id=accepted.attempt_id,
        runtime_preparation_sha256=accepted.runtime_preparation_sha256,
        node_runtime_launch_receipt_sha256=(accepted.node_runtime_launch_receipt_sha256),
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        admitted_at=accepted.hard_deadline - timedelta(hours=1),
        hard_deadline=accepted.hard_deadline,
        issued_at=accepted.accepted_at,
        recovery_expires_at=accepted.artifact_submission_deadline,
    )
    _case, _snapshot, _pin, allocator, _custody, adapter = _fixture(tmp_path / "adapter")
    calls: list[dict[str, object]] = []

    def replay(**scope):
        calls.append(scope)
        return accepted

    allocator.replay_accepted_runtime_termination = replay
    replayed = adapter.replay_accepted_runtime_termination(
        recovery_grant=recovery,
        challenge=challenge,
        node_runtime_termination_receipt=receipt,
        expected_accepted_runtime_termination_sha256=(accepted.accepted_termination_sha256),
    )

    assert replayed == accepted
    assert calls == [
        {
            "recovery_grant": recovery,
            "challenge": challenge,
            "node_runtime_termination_receipt": receipt,
            "expected_accepted_runtime_termination_sha256": (accepted.accepted_termination_sha256),
        }
    ]


def test_constructor_and_pull_fail_closed_on_key_identity_and_non_utc_clock(tmp_path) -> None:
    _case, _snapshot, pin, allocator, custody, _adapter = _fixture(tmp_path)
    with pytest.raises(ValueError, match="differs from its deployment pin"):
        PostgreSQLNodeAllocatorAdapter(
            allocator=allocator,
            transport_pin=pin,
            node_transport_private_key=bytes.fromhex("93" * 32),
            token_custody=custody,
        )
    adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        token_custody=custody,
        clock=_Clock(datetime(2026, 8, 24, tzinfo=timezone(timedelta(hours=12)))),
    )
    with pytest.raises(AssignmentRejected, match="timezone-aware UTC"):
        adapter.pull_qualification_assignment(
            node_id=pin.node_id,
            node_manifest_sha256=pin.node_manifest_sha256,
        )


def test_assignment_conflict_is_translated_without_allocator_detail(tmp_path) -> None:
    _case, _snapshot, pin, allocator, _custody, adapter = _fixture(tmp_path)

    def reject(**_kwargs):
        raise AdmissionConflict("must-not-reflect-storage-detail")

    allocator.pull_assignment_delivery = reject
    with pytest.raises(
        AssignmentRejected, match="allocator rejected the pinned assignment delivery"
    ) as caught:
        adapter.pull_qualification_assignment(
            node_id=pin.node_id,
            node_manifest_sha256=pin.node_manifest_sha256,
        )
    assert "storage-detail" not in str(caught.value)


def test_worker_never_settles_a_non_collected_node_tick(tmp_path, monkeypatch) -> None:
    harness = _Harness(tmp_path / "node")
    prepared = _prepared(
        monkeypatch,
        artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )

    def forbidden_settlement(**_scope):
        raise AssertionError("non-collected node tick reached settlement")

    monkeypatch.setattr(
        prepared.allocator,
        "settle_qualification_terminal",
        forbidden_settlement,
    )
    monkeypatch.setattr(
        prepared.allocator,
        "pull_pending_qualification_terminal_settlement",
        lambda **_scope: None,
        raising=False,
    )
    monkeypatch.setattr(
        prepared.allocator,
        "adjudicate_expired_qualification_terminal",
        lambda **_scope: None,
        raising=False,
    )
    worker = QualificationExecutionWorker(
        agent=harness.agent,
        allocator=prepared.allocator,
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_id=harness.authority.manifest.node_id,
        node_manifest_sha256=harness.authority.manifest.manifest_sha256,
    )

    result = worker.tick()

    assert result.outcome is NodeRunOutcome.RUNNING


def test_worker_adjudicates_signed_terminal_deadline_before_other_work(
    tmp_path, monkeypatch
) -> None:
    harness = _Harness(tmp_path / "node")
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())
    accepted = collected.accepted_runtime_termination
    preparation = harness.allocator._runtime_preparation
    authorization_request = harness.allocator._start_request
    start = harness.allocator._start_authorization
    launch_receipt = harness.allocator.last_launch_receipt
    challenge = harness.allocator.last_challenge
    node_termination = harness.allocator.last_node_termination
    assert accepted is not None and preparation is not None
    assert authorization_request is not None and start is not None
    assert launch_receipt is not None and challenge is not None
    assert node_termination is not None
    expiration = issue_qualification_terminal_deadline_expiration(
        pin=_runtime_control_pin(),
        private_key=NODE_RUNTIME_CONTROL_PRIVATE_KEY,
        intent=harness.intent,
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=authorization_request,
        launch_authorization=start.launch_authorization,
        expected_node_inventory_sha256=harness.reservation.node_inventory_sha256,
        expected_resource_lease_sha256=harness.reservation.resource_lease_sha256,
        node_authority=harness.authority,
        runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
    )
    prepared = _prepared(monkeypatch)
    _case, base, _pin, _allocator, _custody, _adapter = _fixture(tmp_path / "adapter")
    snapshot = replace(
        base,
        execution_id=expiration.execution_id,
        attempt_id=expiration.attempt_id,
        intent_sha256=expiration.intent_sha256,
        node_id=expiration.node_id,
        node_inventory_sha256=expiration.node_inventory_sha256,
        resource_lease_sha256=expiration.resource_lease_sha256,
        status="failed",
        fencing_epoch=expiration.fencing_epoch,
        lease_token_sha256=expiration.lease_token_sha256,
        hard_deadline=expiration.hard_deadline,
        lease_expires_at=harness.reservation.lease_expires_at,
    )
    calls: list[str] = []

    commit = SimpleNamespace(
        snapshot=snapshot,
        terminal_expiration=expiration,
        activated_at=expiration.expired_at + timedelta(seconds=1),
        outbox_id=f"qto_{expiration.terminal_deadline_expiration_sha256}",
        terminal_authority_kind="terminal_deadline_expiration",
        replayed=False,
    )

    def adjudicate(**_scope):
        calls.append("deadline")
        return commit

    monkeypatch.setattr(
        prepared.allocator,
        "adjudicate_expired_qualification_terminal",
        adjudicate,
        raising=False,
    )
    monkeypatch.setattr(
        prepared.allocator,
        "pull_pending_qualification_terminal_settlement",
        lambda **_scope: calls.append("pending"),
        raising=False,
    )
    monkeypatch.setattr(
        harness.agent,
        "run_once",
        lambda: calls.append("agent") or NodeRunResult(outcome=NodeRunOutcome.IDLE),
    )
    worker = QualificationExecutionWorker(
        agent=harness.agent,
        allocator=prepared.allocator,
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_id=harness.authority.manifest.node_id,
        node_manifest_sha256=harness.authority.manifest.manifest_sha256,
    )

    result = worker.tick()

    assert result.outcome is NodeRunOutcome.IDLE
    assert calls == ["deadline", "pending", "agent"]

    valid_commit = vars(commit)
    forged_projections = (
        {"outbox_id": f"qto_{_digest('forged-expiration-authority')}"},
        {"terminal_authority_kind": "accepted_terminal_submission"},
        {"snapshot": replace(snapshot, status="verifying")},
    )
    for forged_projection in forged_projections:
        commit = SimpleNamespace(**(valid_commit | forged_projection))
        with pytest.raises(RuntimeRejected, match="changed exact terminal authority"):
            worker.tick()


def test_worker_finalizes_signed_db_pending_acceptance_before_one_agent_tick(
    tmp_path, monkeypatch
) -> None:
    harness = _Harness(tmp_path / "node")
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())
    acceptance = collected.accepted_terminal_submission
    assert acceptance is not None
    monkeypatch.setattr(
        harness.agent,
        "run_once",
        lambda: NodeRunResult(outcome=NodeRunOutcome.IDLE),
    )

    prepared = _prepared(monkeypatch)
    _case, base, _pin, _allocator, _custody, _adapter = _fixture(tmp_path / "adapter")
    pulls: list[tuple[str, str]] = []
    settled: list[object] = []
    settlement_commit_overrides: dict[str, object] = {}

    def pull_pending(*, node_id, node_manifest_sha256):
        pulls.append((node_id, node_manifest_sha256))
        return acceptance

    def settle(*, terminal_acceptance):
        settled.append(terminal_acceptance)
        commit_values: dict[str, object] = {
            "snapshot": replace(
                base,
                attempt_id=terminal_acceptance.attempt_id,
                node_id=harness.authority.manifest.node_id,
                status="succeeded",
            ),
            "outbox_id": (f"qto_{terminal_acceptance.accepted_terminal_submission_sha256}"),
            "terminal_authority_kind": "accepted_terminal_submission",
            "replayed": False,
        }
        commit_values.update(settlement_commit_overrides)
        return SimpleNamespace(**commit_values)

    monkeypatch.setattr(
        prepared.allocator,
        "pull_pending_qualification_terminal_settlement",
        pull_pending,
        raising=False,
    )
    monkeypatch.setattr(
        prepared.allocator,
        "adjudicate_expired_qualification_terminal",
        lambda **_scope: None,
        raising=False,
    )
    monkeypatch.setattr(prepared.allocator, "settle_qualification_terminal", settle)
    worker = QualificationExecutionWorker(
        agent=harness.agent,
        allocator=prepared.allocator,
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_id=harness.authority.manifest.node_id,
        node_manifest_sha256=harness.authority.manifest.manifest_sha256,
    )

    result = worker.tick()

    assert result.outcome is NodeRunOutcome.IDLE
    assert pulls == [
        (
            harness.authority.manifest.node_id,
            harness.authority.manifest.manifest_sha256,
        )
    ]
    assert settled == [acceptance]

    forged_projections = (
        {"outbox_id": f"qto_{_digest('forged-terminal-acceptance')}"},
        {"terminal_authority_kind": "terminal_deadline_expiration"},
        {
            "snapshot": replace(
                base,
                attempt_id=acceptance.attempt_id,
                node_id=harness.authority.manifest.node_id,
                status="verifying",
            )
        },
        {
            "snapshot": replace(
                base,
                attempt_id=acceptance.attempt_id,
                node_id="node.forged-terminal-settlement",
                status="succeeded",
            )
        },
    )
    for forged_projection in forged_projections:
        settlement_commit_overrides.update(forged_projection)
        with pytest.raises(RuntimeRejected, match="changed exact acceptance"):
            worker.tick()
        settlement_commit_overrides.clear()

    settled.clear()
    foreign_manifest_sha256 = _digest("foreign-node-manifest")
    foreign_worker = QualificationExecutionWorker(
        agent=harness.agent,
        allocator=prepared.allocator,
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_id="node.foreign",
        node_manifest_sha256=foreign_manifest_sha256,
    )
    with pytest.raises(RuntimeRejected, match="another node manifest"):
        foreign_worker.tick()
    assert pulls[-1] == ("node.foreign", foreign_manifest_sha256)
    assert settled == []


def test_worker_settles_from_signed_acceptance_not_other_result_fields(
    tmp_path, monkeypatch
) -> None:
    harness = _Harness(tmp_path / "node")
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())
    acceptance = collected.accepted_terminal_submission
    assert acceptance is not None
    forged_result = replace(collected, attempt_id="attempt_forged_result_field")
    monkeypatch.setattr(harness.agent, "run_once", lambda: forged_result)

    prepared = _prepared(monkeypatch)
    _case, base, _pin, _allocator, _custody, _adapter = _fixture(tmp_path / "adapter")
    settled: list[object] = []

    def settle(*, terminal_acceptance):
        settled.append(terminal_acceptance)
        return SimpleNamespace(
            snapshot=replace(
                base,
                attempt_id=terminal_acceptance.attempt_id,
                node_id=harness.authority.manifest.node_id,
                status="succeeded",
            ),
            outbox_id=(f"qto_{terminal_acceptance.accepted_terminal_submission_sha256}"),
            terminal_authority_kind="accepted_terminal_submission",
            replayed=False,
        )

    monkeypatch.setattr(prepared.allocator, "settle_qualification_terminal", settle)
    monkeypatch.setattr(
        prepared.allocator,
        "pull_pending_qualification_terminal_settlement",
        lambda **_scope: None,
        raising=False,
    )
    monkeypatch.setattr(
        prepared.allocator,
        "adjudicate_expired_qualification_terminal",
        lambda **_scope: None,
        raising=False,
    )
    worker = QualificationExecutionWorker(
        agent=harness.agent,
        allocator=prepared.allocator,
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_id=harness.authority.manifest.node_id,
        node_manifest_sha256=harness.authority.manifest.manifest_sha256,
    )

    result = worker.tick()

    assert result is forged_result
    assert settled == [acceptance]

    forged_acceptance = acceptance.model_copy(update={"signature_ed25519_hex": "0" * 128})
    monkeypatch.setattr(
        harness.agent,
        "run_once",
        lambda: replace(
            forged_result,
            accepted_terminal_submission=forged_acceptance,
        ),
    )
    settled.clear()
    with pytest.raises(RuntimeRejected, match="authenticated terminal acceptance"):
        worker.tick()
    assert settled == []


@pytest.mark.parametrize("settlement_crash", ("before", "after"))
def test_real_postgresql_worker_recovers_settlement_crash_without_duplicate_outbox(
    tmp_path, monkeypatch, _isolated_adapter_database, settlement_crash
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
    )
    allocator.register_node(prepared.manifest.node_id)
    allocator.append_inventory(prepared.inventory)
    claim = allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    assert claim.lease_token is not None

    state = NodeLocalStateStore(tmp_path / "node-state")
    clock = _NodeClock(prepared.observed_at)
    adapter = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=prepared.transport_pin,
        node_transport_private_key=ALLOCATOR_TRANSPORT_PRIVATE_KEY,
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
        executable_sha256=_digest("adapter-integration-qualified-executable"),
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
        output_quota_provisioner=_OutputQuotaProvisioner(),
        artifact_quarantine=_PublishingArtifactStore(
            tmp_path / "artifact-cas",
            resolver=prepared.artifacts,
        ),
        launch_registry=PinnedLaunchRegistry((spec,)),
        state_store=state,
        input_materializer=_InputMaterializer(),
        clock=clock,
    )

    running = agent.run_once()

    assert running.outcome is NodeRunOutcome.RUNNING
    assert running.node_runtime_launch_receipt is not None
    assert runtime.launch_calls == 1
    persisted = allocator.load_attempt(claim.snapshot.attempt_id)
    assert persisted is not None and persisted.status == "running"
    assert (
        state.load_token(
            attempt_id=claim.snapshot.attempt_id,
            fencing_epoch=claim.snapshot.fencing_epoch,
            expected_sha256=claim.snapshot.lease_token_sha256,
        )
        == claim.lease_token
    )

    recovery = adapter.pull_qualification_assignment(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert recovery is not None
    assert recovery.lease_token is None
    assert recovery.historical_recovery_grant is not None
    replay = agent.run_once()
    assert replay.outcome is NodeRunOutcome.RUNNING
    assert runtime.launch_calls == 1

    runtime.finish(exit_code=0)
    original_accept_termination = adapter.accept_runtime_termination
    accepted_before_local_save: list[object] = []

    def crash_after_acceptance_commit(**scope):
        accepted = original_accept_termination(**scope)
        accepted_before_local_save.append(accepted)
        raise SystemExit("crash after accepted termination DB commit")

    monkeypatch.setattr(
        adapter,
        "accept_runtime_termination",
        crash_after_acceptance_commit,
    )
    with pytest.raises(SystemExit, match="accepted termination DB commit"):
        agent.run_once()
    assert len(accepted_before_local_save) == 1
    accepted_commit = accepted_before_local_save[0]
    verifying = allocator.load_attempt(claim.snapshot.attempt_id)
    assert verifying is not None and verifying.status == "verifying"
    recovery_after_acceptance = adapter.pull_qualification_assignment(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert recovery_after_acceptance is not None
    assert recovery_after_acceptance.historical_recovery_grant is not None
    assert (
        recovery_after_acceptance.historical_recovery_grant.accepted_runtime_termination_sha256
        == accepted_commit.accepted_termination_sha256
    )
    monkeypatch.setattr(
        adapter,
        "accept_runtime_termination",
        original_accept_termination,
    )
    original_replay_termination = allocator.replay_accepted_runtime_termination
    accepted_replays: list[dict[str, object]] = []

    def observed_accepted_replay(**scope):
        accepted_replays.append(scope)
        return original_replay_termination(**scope)

    monkeypatch.setattr(
        allocator,
        "replay_accepted_runtime_termination",
        observed_accepted_replay,
    )
    worker = QualificationExecutionWorker(
        agent=agent,
        allocator=allocator,
        runtime_control_authority=issuer.authority_verifier,
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    original_settle = allocator.settle_qualification_terminal
    settlement_acceptances: list[object] = []

    def interrupted_settle(*, terminal_acceptance):
        settlement_acceptances.append(terminal_acceptance)
        if settlement_crash == "before":
            raise SystemExit("crash before terminal settlement")
        original_settle(terminal_acceptance=terminal_acceptance)
        raise SystemExit("crash after terminal settlement")

    monkeypatch.setattr(
        allocator,
        "settle_qualification_terminal",
        interrupted_settle,
    )
    with pytest.raises(SystemExit, match=f"crash {settlement_crash} terminal settlement"):
        worker.tick()
    assert len(accepted_replays) == 1
    assert len(settlement_acceptances) == 1
    after_crash = allocator.load_attempt(claim.snapshot.attempt_id)
    assert after_crash is not None

    sessions = session_factory()

    def outbox_count() -> int:
        with sessions() as session:
            return int(
                session.execute(
                    text(
                        "SELECT count(*) FROM execution_qualification_terminal_outbox "
                        "WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": claim.snapshot.attempt_id},
                ).scalar_one()
            )

    if settlement_crash == "before":
        assert after_crash.status == "verifying"
        assert outbox_count() == 0
    else:
        assert after_crash.status == "succeeded"
        assert outbox_count() == 1

    monkeypatch.setattr(
        allocator,
        "settle_qualification_terminal",
        original_settle,
    )
    terminal_acceptance = settlement_acceptances[0]
    late = terminal_acceptance.artifact_submission_deadline + timedelta(seconds=1)
    clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    assert (
        adapter.pull_qualification_assignment(
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )
    recovered_tick = worker.tick()
    assert recovered_tick.outcome is NodeRunOutcome.IDLE
    settled = allocator.load_attempt(claim.snapshot.attempt_id)
    assert settled is not None and settled.status == "succeeded"
    assert outbox_count() == 1
    replayed_settlement = original_settle(terminal_acceptance=settlement_acceptances[0])
    assert replayed_settlement.replayed is True
    assert replayed_settlement.snapshot.status == "succeeded"
    assert outbox_count() == 1


def test_real_postgresql_worker_deadline_commit_return_crash_is_terminal_and_foreign_safe(
    tmp_path, monkeypatch, _isolated_adapter_database
) -> None:
    prepared, allocator, _adapter, agent, runtime, _state, claim = _real_v2_composition(
        monkeypatch, tmp_path
    )

    def crash_before_terminal_artifacts(**_scope):
        raise SystemExit("crash before terminal artifact acceptance")

    monkeypatch.setattr(
        allocator,
        "accept_terminal_artifacts",
        crash_before_terminal_artifacts,
    )
    runtime.finish(exit_code=0)
    with pytest.raises(SystemExit, match="terminal artifact acceptance"):
        agent.run_once()

    sessions = session_factory()
    with sessions() as session:
        deadline = session.execute(
            text(
                "SELECT conditional_terminal_expiration_expires_at "
                "FROM execution_runtime_termination_acceptances "
                "WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": claim.snapshot.attempt_id},
        ).scalar_one()
    late = deadline + timedelta(microseconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    monkeypatch.setattr(
        agent,
        "run_once",
        lambda: NodeRunResult(outcome=NodeRunOutcome.IDLE),
    )
    verifier = _RuntimeControlIssuer(observed_at=prepared.observed_at).authority_verifier

    foreign_worker = QualificationExecutionWorker(
        agent=agent,
        allocator=allocator,
        runtime_control_authority=verifier,
        node_id="node.foreign-adapter",
        node_manifest_sha256=_digest("foreign-adapter-manifest"),
    )
    with pytest.raises(RuntimeRejected, match="terminal deadline adjudication"):
        foreign_worker.tick()
    with sessions() as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM execution_qualification_terminal_outbox "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": claim.snapshot.attempt_id},
            ).scalar_one()
            == 0
        )

    worker = QualificationExecutionWorker(
        agent=agent,
        allocator=allocator,
        runtime_control_authority=verifier,
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    original_adjudicate = allocator.adjudicate_expired_qualification_terminal
    committed: list[object] = []

    def commit_then_crash(**scope):
        committed.append(original_adjudicate(**scope))
        raise SystemExit("crash after deadline adjudication commit")

    monkeypatch.setattr(
        allocator,
        "adjudicate_expired_qualification_terminal",
        commit_then_crash,
    )
    with pytest.raises(SystemExit, match="deadline adjudication commit"):
        worker.tick()
    assert len(committed) == 1 and committed[0] is not None
    terminal_commit = committed[0]
    assert terminal_commit.snapshot.status == "failed"
    assert terminal_commit.terminal_authority_kind == "terminal_deadline_expiration"
    assert terminal_commit.outbox_id == (
        f"qto_{terminal_commit.terminal_expiration.terminal_deadline_expiration_sha256}"
    )

    monkeypatch.setattr(
        allocator,
        "adjudicate_expired_qualification_terminal",
        original_adjudicate,
    )
    assert worker.tick().outcome is NodeRunOutcome.IDLE
    with sessions() as session:
        status, expiration_sha256 = session.execute(
            text(
                "SELECT status, terminal_deadline_expiration_sha256 "
                "FROM execution_attempts WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": claim.snapshot.attempt_id},
        ).one()
        active_attempt_id = session.execute(
            text(
                "SELECT active_attempt_id FROM execution_heads WHERE execution_id = :execution_id"
            ),
            {"execution_id": claim.snapshot.execution_id},
        ).scalar_one()
        outbox_rows = session.execute(
            text(
                "SELECT terminal_authority_kind, terminal_authority_sha256, outbox_id "
                "FROM execution_qualification_terminal_outbox "
                "WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": claim.snapshot.attempt_id},
        ).all()
    assert status == "failed"
    assert expiration_sha256 == (
        terminal_commit.terminal_expiration.terminal_deadline_expiration_sha256
    )
    assert active_attempt_id is None
    assert outbox_rows == [
        (
            "terminal_deadline_expiration",
            expiration_sha256,
            f"qto_{expiration_sha256}",
        )
    ]


def test_real_postgresql_concurrent_deadline_workers_emit_one_exact_outbox(
    tmp_path, monkeypatch, _isolated_adapter_database
) -> None:
    prepared, allocator, _adapter, agent, runtime, _state, claim = _real_v2_composition(
        monkeypatch, tmp_path
    )

    def crash_before_terminal_artifacts(**_scope):
        raise SystemExit("crash before concurrent deadline finalization")

    monkeypatch.setattr(
        allocator,
        "accept_terminal_artifacts",
        crash_before_terminal_artifacts,
    )
    runtime.finish(exit_code=1)
    with pytest.raises(SystemExit, match="concurrent deadline finalization"):
        agent.run_once()

    sessions = session_factory()
    with sessions() as session:
        deadline = session.execute(
            text(
                "SELECT conditional_terminal_expiration_expires_at "
                "FROM execution_runtime_termination_acceptances "
                "WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": claim.snapshot.attempt_id},
        ).scalar_one()
    late = deadline + timedelta(microseconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    monkeypatch.setattr(
        agent,
        "run_once",
        lambda: NodeRunResult(outcome=NodeRunOutcome.IDLE),
    )
    verifier = _RuntimeControlIssuer(observed_at=prepared.observed_at).authority_verifier
    workers = tuple(
        QualificationExecutionWorker(
            agent=agent,
            allocator=allocator,
            runtime_control_authority=verifier,
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        for _ in range(2)
    )
    original_adjudicate = allocator.adjudicate_expired_qualification_terminal
    observed_commits: list[object | None] = []

    def observed_adjudication(**scope):
        commit = original_adjudicate(**scope)
        observed_commits.append(commit)
        return commit

    monkeypatch.setattr(
        allocator,
        "adjudicate_expired_qualification_terminal",
        observed_adjudication,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda worker: worker.tick(), workers))

    assert all(result.outcome is NodeRunOutcome.IDLE for result in results)
    returned_commits = tuple(item for item in observed_commits if item is not None)
    assert returned_commits
    assert sum(not item.replayed for item in returned_commits) == 1
    authority_sha256s = {
        item.terminal_expiration.terminal_deadline_expiration_sha256 for item in returned_commits
    }
    assert len(authority_sha256s) == 1
    authority_sha256 = authority_sha256s.pop()
    assert all(
        item.terminal_authority_kind == "terminal_deadline_expiration"
        and item.outbox_id == f"qto_{authority_sha256}"
        for item in returned_commits
    )
    with sessions() as session:
        outbox_rows = session.execute(
            text(
                "SELECT terminal_authority_kind, terminal_authority_sha256, outbox_id "
                "FROM execution_qualification_terminal_outbox "
                "WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": claim.snapshot.attempt_id},
        ).all()
    assert outbox_rows == [
        (
            "terminal_deadline_expiration",
            authority_sha256,
            f"qto_{authority_sha256}",
        )
    ]


def test_real_postgresql_prelaunch_recovery_after_hard_deadline_preserves_quota_and_never_launches(
    tmp_path, monkeypatch, _isolated_adapter_database
) -> None:
    prepared, allocator, adapter, agent, runtime, state, claim = _real_v2_composition(
        monkeypatch, tmp_path, start=False
    )
    original_authorize = allocator.authorize_runtime_start

    def commit_then_crash(**scope):
        original_authorize(**scope)
        raise SystemExit("crash after durable prelaunch authorization")

    monkeypatch.setattr(allocator, "authorize_runtime_start", commit_then_crash)
    with pytest.raises(SystemExit, match="durable prelaunch authorization"):
        agent.run_once()
    assert runtime.launch_calls == 0
    quota_receipt = state.load_output_quota_provisioning(attempt_id=claim.snapshot.attempt_id)
    assert quota_receipt is not None
    assert quota_receipt.output_quota_bytes == (
        prepared.bundle.intent.resource_request.artifact_quota_bytes
    )

    monkeypatch.setattr(allocator, "authorize_runtime_start", original_authorize)
    late = claim.snapshot.hard_deadline + timedelta(seconds=1)
    runtime.clock.current = late
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: late)
    reconciled = allocator.reconcile_expired()
    assert len(reconciled) == 1
    assert reconciled[0].status == "reconciliation_required"

    assignment = adapter.pull_qualification_assignment(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert assignment is not None
    assert assignment.lease_token is None
    assert assignment.historical_recovery_grant is None
    lineage = assignment.historical_pre_runtime_recovery_lineage
    assert lineage is not None
    assert lineage.cleanup_only is True and lineage.launch_allowed is False
    assert (
        lineage.runtime_preparation.output_quota_provisioning_receipt_sha256
        == quota_receipt.provisioning_receipt_sha256
    )

    cleaned = agent.run_once()
    assert cleaned.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert runtime.launch_calls == 0
    assert runtime.cleanup_calls == 1
    terminal = allocator.load_attempt(claim.snapshot.attempt_id)
    assert terminal is not None and terminal.status == "cancelled"
    assert terminal.reconciliation_reason is None
