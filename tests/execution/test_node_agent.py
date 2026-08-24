from __future__ import annotations

import hashlib
import stat
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.node_agent import (
    AssignmentRejected,
    LocalStateError,
    NodeLeaseRejected,
    NodeLocalStateStore,
    NodeReservation,
    NodeRunOutcome,
    NodeTerminalDisposition,
    OutputCollectionRejected,
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedLaunchRegistry,
    PinnedLaunchSpec,
    QualificationAssignment,
    QualificationNodeAgent,
    ReservedDeviceBinding,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeRejected,
)
from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReceipt,
    EngineeringQualificationGrant,
    EngineeringQualificationGrantMessage,
    NodeEnrollmentAuthorityPin,
    NodeEnrollmentAuthorityVerifier,
    NodeExecutionReceipt,
    NodeRuntimeIdentity,
    QualificationAuthorityPin,
    QualificationAuthorityVerifier,
    RuntimeInspectionReceipt,
    RuntimeInspectionState,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
    issue_worker_node_enrollment,
    qualification_key_id,
    verify_worker_node_enrollment,
)
from aletheia.execution.schemas import (
    ExecutionIntent,
    InfrastructureAttempt,
    InputArtifactBinding,
    NetworkPolicy,
    ScientificReplicateSlot,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.protocols.compiler import compile_protocol

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import fixture_by_name  # noqa: E402

UTC = timezone.utc
PRIVATE_KEY = bytes(range(32))
ENROLLMENT_PRIVATE_KEY = bytes(range(1, 33))
NOW = datetime(2026, 8, 24, 1, 7, 3, tzinfo=UTC)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


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


@dataclass(frozen=True)
class QualificationCase:
    intent: ExecutionIntent
    work_order_node: object
    grant: EngineeringQualificationGrant
    pin: QualificationAuthorityPin
    observed_at: datetime


def _qualification_case() -> QualificationCase:
    fixture = fixture_by_name("grouped_regression")
    request = fixture.request
    result = compile_protocol(request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.protocol_step_id == "step.01_group")
    slot = ScientificReplicateSlot(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        slot_count=node.scientific_replicate_count,
        slot_index=1,
        replicate_kind=node.replicate_kind,
        preregistration_sha256=node.replicate_preregistration_sha256,
        randomization_seed_sha256=node.replicate_seed_sha256s[0],
        independent_site_required=node.independent_site_required,
    )
    binding = InputArtifactBinding(
        input_port_id=node.input_port_ids[0],
        source_kind="protocol_input",
        artifact_verified_receipt_sha256=_digest("verified-protocol-input"),
    )
    intent = ExecutionIntent(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_sha256=work_order.work_order_sha256,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        capability_id=node.capability_id,
        capability_manifest_sha256=node.capability_manifest_sha256,
        resource_catalog_sha256=work_order.resource_catalog_sha256,
        resource_request=node.resource_request,
        retry_policy=node.retry_policy,
        replicate_slot=slot,
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=(binding,),
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=NOW - timedelta(minutes=5),
        deadline=NOW + timedelta(hours=1),
    )
    public_key = _public_key_hex(PRIVATE_KEY)
    pin = QualificationAuthorityPin(
        policy_sha256=_digest("qualification-policy"),
        principal_id="principal:qualification-authority",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    message = EngineeringQualificationGrantMessage(
        bundle_sha256=_digest("qualification-bundle"),
        quest_id=intent.quest_id,
        graph_scope_sha256=request.protocol.graph_scope.graph_scope_sha256,
        protocol_sha256=intent.protocol_sha256,
        compilation_request_sha256=canonical_sha256(request),
        compilation_result_sha256=canonical_sha256(result),
        compilation_receipt_sha256=result.receipt.receipt_sha256,
        work_order_id=work_order.work_order_id,
        work_order_sha256=work_order.work_order_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        input_artifact_verified_receipt_sha256s=(binding.artifact_verified_receipt_sha256,),
        budget_authorization_sha256=_digest("budget-authorization"),
        cost_quote_sha256=_digest("cost-quote"),
        qualification_authority_policy_sha256=pin.policy_sha256,
        authorized_by_principal_id=pin.principal_id,
        authorization_key_id=pin.key_id,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=20),
    )
    grant = EngineeringQualificationGrant(
        message=message,
        signature_ed25519_hex=Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY)
        .sign(message.message_bytes)
        .hex(),
    )
    return QualificationCase(
        intent=intent,
        work_order_node=node,
        grant=grant,
        pin=pin,
        observed_at=NOW,
    )


def _worker_authority(*, observed_at: datetime, enrollment_expires_at: datetime | None = None):
    public_key = _public_key_hex(PRIVATE_KEY)
    manifest = WorkerNodeManifest(
        node_id="node.qualification-01",
        site_id="site.local",
        principal_id="principal:qualification-node",
        agent_version="1.0.0",
        agent_implementation_sha256=_digest("node-agent"),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="containerd/2",
        sandbox_policy_sha256=_digest("sandbox-policy"),
        resource_class_ids=("rsc_" + _digest("resource-class")[:32],),
        allowed_data_classifications=("internal",),
        network_policies=(NetworkPolicy.NONE,),
        egress_policy_sha256=_digest("egress-none"),
        node_signing_key_id=qualification_key_id(public_key),
        node_signing_public_key_ed25519_hex=public_key,
        key_valid_from=NOW - timedelta(days=1),
        key_expires_at=NOW + timedelta(days=1),
        frozen_at=NOW - timedelta(hours=1),
    )
    enrollment_public_key = _public_key_hex(ENROLLMENT_PRIVATE_KEY)
    pin = NodeEnrollmentAuthorityPin(
        policy_sha256=_digest("node-enrollment-policy"),
        principal_id="principal:node-enrollment",
        key_id=qualification_key_id(enrollment_public_key),
        public_key_ed25519_hex=enrollment_public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )
    enrollment = issue_worker_node_enrollment(
        manifest=manifest,
        pin=pin,
        private_key=ENROLLMENT_PRIVATE_KEY,
        issued_at=NOW - timedelta(minutes=30),
        expires_at=enrollment_expires_at or NOW + timedelta(hours=12),
    )
    return verify_worker_node_enrollment(
        manifest=manifest,
        enrollment=enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(pin),
        expected_manifest_sha256=manifest.manifest_sha256,
        observed_at=observed_at,
    )


class _Clock:
    def __init__(self, now: datetime, monotonic_ns: int = 10_000) -> None:
        self.current = now
        self.monotonic = monotonic_ns

    def now(self) -> datetime:
        return self.current

    def monotonic_ns(self) -> int:
        self.monotonic += 10
        return self.monotonic


class _InputMaterializer:
    def __init__(self) -> None:
        self.calls = 0
        self.crash_after_write = False

    def ensure_verified_inputs(self, *, intent, destination: Path) -> str:
        assert intent.input_artifact_bindings
        self.calls += 1
        expected = b'{"verified":true}'
        target = destination / "input.json"
        if target.exists():
            assert target.is_file()
            assert target.read_bytes() == expected
        else:
            assert not any(destination.iterdir())
            target.write_bytes(expected)
        materialization_sha256 = canonical_sha256(
            {
                "schema": "test.verified_input_materialization.v1",
                "intent_sha256": intent.intent_sha256,
                "input_sha256": hashlib.sha256(expected).hexdigest(),
            }
        )
        if self.crash_after_write:
            self.crash_after_write = False
            raise SystemExit("crash after verified input materialization")
        return materialization_sha256


class _Runtime:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.state = RuntimeInspectionState.ABSENT
        self.prepare_calls = 0
        self.launch_calls = 0
        self.identity: NodeRuntimeIdentity | None = None
        self.request: RuntimeLaunchRequest | None = None
        self.exit_code: int | None = None
        self.ended_at: datetime | None = None
        self.ended_monotonic_ns: int | None = None
        self.crash_after_launch = False
        self.crash_before_runtime_call = False
        self.crash_after_prepare = False
        self.prepared_observation_state: RuntimeInspectionState | None = None
        self.forge_placement_evidence = False
        self.observation_age_seconds = 0

    def prepare(self, *, request: RuntimeLaunchRequest) -> NodeRuntimeIdentity:
        self.prepare_calls += 1
        if self.request is not None:
            assert request == self.request
        self.request = request
        if self.identity is None:
            self.identity = NodeRuntimeIdentity(
                node_id=request.node_id,
                boot_id=request.boot_id,
                execution_id=request.execution_id,
                infrastructure_attempt_id=request.attempt_id,
                runtime_id=request.runtime_id,
                runtime_engine=request.spec.runtime_engine,
                launch_spec_sha256=request.spec.launch_spec_sha256,
                sandbox_instance_sha256=_digest(f"sandbox:{request.runtime_id}"),
                process_identity_sha256=_digest(f"process:{request.runtime_id}"),
                started_at=self.clock.now(),
                started_monotonic_ns=self.clock.monotonic_ns(),
            )
        identity = self.identity
        if self.prepared_observation_state is not None:
            self.state = self.prepared_observation_state
            if self.state is RuntimeInspectionState.TERMINATED:
                self.exit_code = 0
                self.ended_at = self.clock.now()
                self.ended_monotonic_ns = self.clock.monotonic_ns()
        if self.crash_after_prepare:
            self.crash_after_prepare = False
            raise SystemExit("crash after idempotent runtime preparation")
        return identity

    def inspect(
        self, *, request: RuntimeLaunchRequest, identity: NodeRuntimeIdentity
    ) -> RuntimeObservation:
        assert request.runtime_id == identity.runtime_id
        return RuntimeObservation(
            state=self.state,
            runtime_identity=identity,
            enforced_placement_sha256=(
                _digest("forged-placement")
                if self.forge_placement_evidence
                else request.enforced_placement_sha256
            ),
            enforced_fencing_epoch=request.fencing_epoch,
            inspection_evidence_sha256=_digest(
                f"inspect:{request.runtime_id}:{self.state.value}:{self.clock.monotonic}"
            ),
            inspected_at=self.clock.now() - timedelta(seconds=self.observation_age_seconds),
            inspected_monotonic_ns=max(
                identity.started_monotonic_ns,
                self.clock.monotonic_ns() - self.observation_age_seconds * 1_000_000_000,
            ),
            exit_code=self.exit_code,
            ended_at=self.ended_at,
            ended_monotonic_ns=self.ended_monotonic_ns,
        )

    def ensure_started(
        self, *, request: RuntimeLaunchRequest, identity: NodeRuntimeIdentity
    ) -> None:
        assert request.runtime_id == identity.runtime_id
        self.request = request
        if self.crash_before_runtime_call:
            self.crash_before_runtime_call = False
            raise SystemExit("crash in committed launch gap")
        self.launch_calls += 1
        self.state = RuntimeInspectionState.RUNNING
        if self.crash_after_launch:
            self.crash_after_launch = False
            raise SystemExit("node process crashed after idempotent runtime launch")

    def finish(
        self,
        *,
        exit_code: int,
        write_declared: bool = True,
        write_extra: bool = False,
    ) -> None:
        assert self.request is not None
        if write_declared:
            for item in self.request.spec.artifact_paths:
                target = self.request.output_root / item.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"result:{item.artifact_key}".encode())
        if write_extra:
            (self.request.output_root / "undeclared.bin").write_bytes(b"undeclared")
        self.state = RuntimeInspectionState.TERMINATED
        self.exit_code = exit_code
        self.ended_at = self.clock.now()
        self.ended_monotonic_ns = self.clock.monotonic_ns()

    def unknown(self) -> None:
        self.state = RuntimeInspectionState.UNKNOWN
        self.exit_code = None
        self.ended_at = None
        self.ended_monotonic_ns = None


class _Allocator:
    def __init__(self, assignment: QualificationAssignment) -> None:
        self.assignment = assignment
        self.current = assignment.reservation
        self.calls: list[str] = []
        self.start_crash = False
        self.crash_after_mark_running = False
        self.crash_after_mark_terminated = False
        self.crash_before_mark_verifying = False
        self.crash_after_adoption = False
        self.crash_before_adoption_commit = False
        self.reject_heartbeat = False
        self.reconcile_on: set[str] = set()
        self.retained = 0
        self.last_inspection: RuntimeInspectionReceipt | None = None
        self.last_adoption: AttemptAdoptionReceipt | None = None
        self.last_node_receipt: NodeExecutionReceipt | None = None
        self.last_disposition: NodeTerminalDisposition | None = None

    def pull_qualification_assignment(self, *, node_id: str, node_manifest_sha256: str):
        assert node_id == self.current.node_id
        assert len(node_manifest_sha256) == 64
        self.calls.append("pull")
        return replace(self.assignment, reservation=self.current)

    def start_attempt(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_identity: NodeRuntimeIdentity,
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("start")
        if self.start_crash:
            self.start_crash = False
            raise SystemExit("allocator crashed before launch authorization committed")
        assert runtime_identity.infrastructure_attempt_id == attempt_id
        self.current = replace(
            self.current,
            status=("reconciliation_required" if "start" in self.reconcile_on else "starting"),
        )
        return self.current

    def mark_running(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("running")
        self.current = replace(
            self.current,
            status=(
                "reconciliation_required" if "mark_running" in self.reconcile_on else "running"
            ),
        )
        if self.crash_after_mark_running:
            self.crash_after_mark_running = False
            raise SystemExit("crash after allocator running commit")
        return self.current

    def heartbeat(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("heartbeat")
        if self.reject_heartbeat:
            raise NodeLeaseRejected("stale fence")
        if "heartbeat" in self.reconcile_on:
            self.current = replace(self.current, status="reconciliation_required")
        return self.current

    def retain_reconciliation(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
        reason: str,
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        assert reason
        self.calls.append("retain")
        self.retained += 1
        self.last_inspection = inspection_receipt
        self.current = replace(self.current, status="reconciliation_required")
        return self.current

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
    ) -> NodeReservation:
        self._authority(
            receipt.infrastructure_attempt_id, previous_lease_token, previous_fencing_epoch
        )
        assert hashlib.sha256(new_lease_token.encode()).hexdigest() == (
            receipt.new_lease_token_sha256
        )
        self.calls.append("adopt")
        self.last_adoption = receipt
        if self.crash_before_adoption_commit:
            self.crash_before_adoption_commit = False
            raise SystemExit("crash before allocator adoption commit")
        self.current = replace(
            self.current,
            status=("reconciliation_required" if "adopt" in self.reconcile_on else "running"),
            fencing_epoch=receipt.new_fencing_epoch,
            lease_token_sha256=receipt.new_lease_token_sha256,
            device_leases=tuple(
                replace(item, fencing_epoch=receipt.new_fencing_epoch)
                for item in self.current.device_leases
            ),
        )
        if self.crash_after_adoption:
            self.crash_after_adoption = False
            raise SystemExit("crash after allocator adoption commit")
        return self.current

    def mark_terminated(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        assert inspection_receipt.state in {
            RuntimeInspectionState.TERMINATED,
            RuntimeInspectionState.ABSENT,
        }
        self.calls.append("terminated")
        self.last_inspection = inspection_receipt
        self.current = replace(
            self.current,
            status=(
                "reconciliation_required"
                if "mark_terminated" in self.reconcile_on
                else "terminated"
            ),
        )
        if self.crash_after_mark_terminated:
            self.crash_after_mark_terminated = False
            raise SystemExit("crash after allocator termination commit")
        return self.current

    def mark_verifying(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_execution_receipt: NodeExecutionReceipt,
        disposition: NodeTerminalDisposition,
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("verifying")
        if self.crash_before_mark_verifying:
            self.crash_before_mark_verifying = False
            raise SystemExit("crash before allocator verification commit")
        self.last_node_receipt = node_execution_receipt
        self.last_disposition = disposition
        self.current = replace(
            self.current,
            status=(
                "reconciliation_required" if "mark_verifying" in self.reconcile_on else "verifying"
            ),
        )
        return self.current

    def _authority(self, attempt_id: str, token: str, fence: int) -> None:
        assert attempt_id == self.current.attempt_id
        if (
            fence != self.current.fencing_epoch
            or hashlib.sha256(token.encode()).hexdigest() != self.current.lease_token_sha256
        ):
            raise NodeLeaseRejected("stale token or fence")


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        case: QualificationCase | None = None,
        *,
        worker_enrollment_expires_at: datetime | None = None,
    ) -> None:
        self.case = case or _qualification_case()
        self.intent = self.case.intent
        self.node = self.case.work_order_node
        self.authority = _worker_authority(
            observed_at=self.case.observed_at,
            enrollment_expires_at=worker_enrollment_expires_at,
        )
        self.clock = _Clock(self.case.observed_at)
        self.raw_token = "T" * 43
        token_sha256 = hashlib.sha256(self.raw_token.encode()).hexdigest()
        self.reservation = NodeReservation(
            execution_id=self.intent.execution_id,
            attempt_id=self.intent.infrastructure_attempt.infrastructure_attempt_id,
            intent_sha256=self.intent.intent_sha256,
            admission_sha256=_digest("admission"),
            grant_sha256=self.case.grant.grant_sha256,
            node_id=self.authority.manifest.node_id,
            node_inventory_sha256=_digest("inventory"),
            resource_lease_sha256=_digest("lease"),
            selected_resource_ids=("cpu.socket-0",),
            cpu_cores=self.intent.resource_request.cpu_cores,
            memory_bytes=self.intent.resource_request.memory_bytes,
            scratch_bytes=self.intent.resource_request.scratch_bytes,
            exclusive=self.intent.resource_request.exclusive,
            device_leases=(),
            status="reserved",
            fencing_epoch=1,
            lease_token_sha256=token_sha256,
            lease_expires_at=self.clock.now() + timedelta(minutes=10),
            hard_deadline=self.clock.now() + timedelta(minutes=15),
        )
        artifact_paths = tuple(
            PinnedArtifactPath(
                artifact_key=item.artifact_key,
                relative_path=f"{index:03d}-{item.artifact_key.replace(':', '_')}.bin",
            )
            for index, item in enumerate(self.intent.expected_artifacts, start=1)
        )
        self.spec = PinnedLaunchSpec(
            command_sha256=self.node.command_sha256,
            environment_sha256=self.node.environment_sha256,
            capability_manifest_sha256=self.node.capability_manifest_sha256,
            executable_sha256=_digest("qualified-executable"),
            runtime_engine=self.authority.manifest.container_runtime,
            argv=("/opt/aletheia/bin/qualified-group", "--input", "/input/input.json"),
            environment=(PinnedEnvironmentVariable(name="LC_ALL", value="C.UTF-8"),),
            artifact_paths=artifact_paths,
        )
        self.assignment = QualificationAssignment(
            intent=self.intent,
            work_order_node=self.node,
            qualification_grant=self.case.grant,
            reservation=self.reservation,
            lease_token=self.raw_token,
        )
        self.allocator = _Allocator(self.assignment)
        self.runtime = _Runtime(self.clock)
        self.materializer = _InputMaterializer()
        self.state = NodeLocalStateStore(tmp_path / "node-state")
        self.artifacts = LocalArtifactStore(tmp_path / "artifact-cas")
        self.agent = QualificationNodeAgent(
            node_authority=self.authority,
            qualification_authority=QualificationAuthorityVerifier(self.case.pin),
            node_signing_private_key=PRIVATE_KEY,
            boot_id="boot.qualification-001",
            allocator_principal_id="principal:allocator",
            allocator=self.allocator,
            runtime=self.runtime,
            artifact_quarantine=self.artifacts,
            launch_registry=PinnedLaunchRegistry((self.spec,)),
            state_store=self.state,
            input_materializer=self.materializer,
            clock=self.clock,
        )

    def replay_assignment(self, *, status: str | None = None) -> QualificationAssignment:
        reservation = self.allocator.current
        if status is not None:
            reservation = replace(reservation, status=status)
            self.allocator.current = reservation
        return replace(self.assignment, reservation=reservation, lease_token=None)


def test_pinned_launch_spec_rejects_shell_host_env_secrets_network_and_mount_escape() -> None:
    base = dict(
        command_sha256=_digest("command"),
        environment_sha256=_digest("environment"),
        capability_manifest_sha256=_digest("capability"),
        executable_sha256=_digest("executable"),
        runtime_engine="containerd/2",
        argv=("/opt/bin/qualified",),
        artifact_paths=(PinnedArtifactPath(artifact_key="raw", relative_path="result.bin"),),
    )
    with pytest.raises(ValidationError, match="non-shell"):
        PinnedLaunchSpec(**{**base, "argv": ("/bin/sh", "-c", "echo unsafe")})
    with pytest.raises(ValidationError, match="absolute"):
        PinnedLaunchSpec(**{**base, "argv": ("qualified --unsafe",)})
    with pytest.raises(ValidationError, match="credentials"):
        PinnedLaunchSpec(
            **{
                **base,
                "environment": (
                    PinnedEnvironmentVariable(name="DATABASE_URL", value="postgres://db"),
                ),
            }
        )
    with pytest.raises(ValidationError):
        PinnedLaunchSpec(**{**base, "inherit_host_environment": True})
    with pytest.raises(ValidationError):
        PinnedLaunchSpec(**{**base, "network_policy": NetworkPolicy.ALLOWLIST})
    with pytest.raises(ValidationError, match="Extra inputs"):
        PinnedLaunchSpec(**{**base, "mounts": ("/var/run/docker.sock",)})


def test_happy_replay_safe_run_persists_private_state_and_collects_exact_signed_tree(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)

    running = harness.agent.run_once()
    assert running.outcome is NodeRunOutcome.RUNNING
    assert harness.runtime.launch_calls == 1
    assert harness.materializer.calls == 1

    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())

    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert collected.artifact_manifest is not None
    assert collected.node_execution_receipt is not None
    assert collected.node_execution_receipt.exit_code == 0
    assert collected.node_execution_receipt.confirmed_terminated is True
    assert collected.node_execution_receipt.qualification_only is True
    assert collected.node_execution_receipt.scientific_admission_allowed is False
    assert collected.terminal_disposition is NodeTerminalDisposition.PROCESS_SUCCEEDED
    assert collected.node_execution_receipt.output_tree_sha256 == artifact_output_tree_sha256(
        collected.artifact_manifest
    )
    assert harness.runtime.launch_calls == 1
    assert harness.allocator.calls[-2:] == ["terminated", "verifying"]

    raw = harness.raw_token.encode()
    assert raw not in canonical_json_bytes(collected.node_execution_receipt)
    assert all(
        raw not in path.read_bytes()
        for directory in (
            "adoptions",
            "attempts",
            "locks",
            "preparations",
            "results",
            "terminations",
        )
        for path in (harness.state.root / directory).iterdir()
    )
    for directory in (
        "adoptions",
        "attempts",
        "tokens",
        "locks",
        "preparations",
        "results",
        "terminations",
    ):
        for path in (harness.state.root / directory).iterdir():
            metadata = path.lstat()
            assert stat.S_ISREG(metadata.st_mode)
            assert metadata.st_nlink == 1
            assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert harness.runtime.request is not None
    assert harness.runtime.request.spec.inherit_host_environment is False
    assert harness.runtime.request.spec.network_policy is NetworkPolicy.NONE
    assert harness.runtime.request.input_root != harness.runtime.request.output_root
    assert stat.S_IMODE(harness.runtime.request.input_root.stat().st_mode) == 0o500
    assert (
        harness.runtime.request.resource_lease_sha256 == harness.reservation.resource_lease_sha256
    )
    assert (
        harness.runtime.request.node_inventory_sha256 == harness.reservation.node_inventory_sha256
    )
    assert harness.runtime.request.selected_resource_ids == ("cpu.socket-0",)
    assert harness.runtime.request.cpu_cores == harness.intent.resource_request.cpu_cores
    assert harness.runtime.request.memory_bytes == harness.intent.resource_request.memory_bytes
    assert harness.runtime.request.scratch_bytes == harness.intent.resource_request.scratch_bytes
    assert harness.runtime.request.fencing_epoch == 1
    assert harness.raw_token not in repr(harness.runtime.request)

    replayed = harness.agent.run_assignment(harness.replay_assignment())
    assert replayed.node_execution_receipt == collected.node_execution_receipt
    assert harness.runtime.launch_calls == 1


def test_crash_before_allocator_start_retries_but_post_launch_recovery_never_duplicates(
    tmp_path: Path,
) -> None:
    before = _Harness(tmp_path / "before")
    before.allocator.start_crash = True
    with pytest.raises(SystemExit, match="before launch"):
        before.agent.run_once()
    assert before.runtime.launch_calls == 0
    recovered = before.agent.run_assignment(before.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.RUNNING
    assert before.runtime.launch_calls == 1

    after = _Harness(tmp_path / "after")
    after.runtime.crash_after_launch = True
    with pytest.raises(SystemExit, match="after idempotent"):
        after.agent.run_once()
    assert after.runtime.launch_calls == 1
    recovered_after = after.agent.run_assignment(after.replay_assignment())
    assert recovered_after.outcome is NodeRunOutcome.RUNNING
    assert after.runtime.launch_calls == 1


def test_preparation_recovery_is_exact_after_input_or_runtime_metadata_crash(
    tmp_path: Path,
) -> None:
    input_crash = _Harness(tmp_path / "input")
    input_crash.materializer.crash_after_write = True
    with pytest.raises(SystemExit, match="input materialization"):
        input_crash.agent.run_once()
    assert input_crash.state.load_state(input_crash.reservation.attempt_id) is None
    recovered_input = input_crash.agent.run_assignment(input_crash.replay_assignment())
    assert recovered_input.outcome is NodeRunOutcome.RUNNING
    assert input_crash.materializer.calls == 2
    assert input_crash.runtime.prepare_calls == 1
    assert input_crash.runtime.launch_calls == 1

    prepare_crash = _Harness(tmp_path / "prepare")
    prepare_crash.runtime.crash_after_prepare = True
    with pytest.raises(SystemExit, match="runtime preparation"):
        prepare_crash.agent.run_once()
    prepared_identity = prepare_crash.runtime.identity
    assert prepared_identity is not None
    assert prepare_crash.state.load_state(prepare_crash.reservation.attempt_id) is None
    recovered_prepare = prepare_crash.agent.run_assignment(prepare_crash.replay_assignment())
    assert recovered_prepare.outcome is NodeRunOutcome.RUNNING
    assert recovered_prepare.runtime_identity == prepared_identity
    assert prepare_crash.materializer.calls == 2
    assert prepare_crash.runtime.prepare_calls == 2
    assert prepare_crash.runtime.launch_calls == 1


def test_crash_after_mark_running_commit_advances_local_phase_without_replaying_start(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.crash_after_mark_running = True

    with pytest.raises(SystemExit, match="running commit"):
        harness.agent.run_once()

    assert harness.allocator.current.status == "running"
    assert harness.runtime.launch_calls == 1
    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.RUNNING
    assert harness.allocator.calls.count("start") == 1
    assert harness.allocator.calls.count("running") == 1
    assert harness.runtime.launch_calls == 1


def test_allocator_reconciliation_responses_never_authorize_or_continue_workload(
    tmp_path: Path,
) -> None:
    start = _Harness(tmp_path / "start")
    start.allocator.reconcile_on.add("start")
    start_result = start.agent.run_once()
    assert start_result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert start.runtime.launch_calls == 0

    mark_running = _Harness(tmp_path / "mark-running")
    mark_running.allocator.reconcile_on.add("mark_running")
    running_result = mark_running.agent.run_once()
    assert running_result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert mark_running.runtime.launch_calls == 1
    assert mark_running.state.load_state(mark_running.reservation.attempt_id) is not None
    assert not mark_running.state.load_state(mark_running.reservation.attempt_id).running_confirmed
    mark_running.allocator.reconcile_on.clear()
    recovered_running = mark_running.agent.run_assignment(mark_running.replay_assignment())
    assert recovered_running.outcome is NodeRunOutcome.ADOPTED
    assert recovered_running.runtime_identity == running_result.runtime_identity
    assert mark_running.runtime.launch_calls == 1

    heartbeat = _Harness(tmp_path / "heartbeat")
    heartbeat.allocator.reconcile_on.add("heartbeat")
    heartbeat_result = heartbeat.agent.run_once()
    assert heartbeat_result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert heartbeat.runtime.launch_calls == 1
    assert heartbeat.allocator.current.status == "reconciliation_required"

    terminated = _Harness(tmp_path / "launch-committed-terminated")
    terminated.allocator.reconcile_on.add("mark_running")
    assert terminated.agent.run_once().outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    terminated.allocator.reconcile_on.clear()
    terminated.runtime.finish(exit_code=0)
    collected = terminated.agent.run_assignment(terminated.replay_assignment())
    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert terminated.allocator.calls.count("running") == 1
    assert terminated.allocator.calls.count("terminated") == 1


def test_terminal_and_adoption_mutations_validate_normal_reconciliation_responses(
    tmp_path: Path,
) -> None:
    terminated = _Harness(tmp_path / "terminated")
    assert terminated.agent.run_once().outcome is NodeRunOutcome.RUNNING
    terminated.runtime.finish(exit_code=0)
    terminated.allocator.reconcile_on.add("mark_terminated")
    stopped = terminated.agent.run_assignment(terminated.replay_assignment())
    assert stopped.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert stopped.node_execution_receipt is None
    assert terminated.allocator.last_node_receipt is None

    verifying = _Harness(tmp_path / "verifying")
    assert verifying.agent.run_once().outcome is NodeRunOutcome.RUNNING
    verifying.runtime.finish(exit_code=0)
    verifying.allocator.reconcile_on.add("mark_verifying")
    central_hold = verifying.agent.run_assignment(verifying.replay_assignment())
    assert central_hold.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert central_hold.node_execution_receipt is not None
    assert central_hold.artifact_manifest is not None
    assert verifying.allocator.current.status == "reconciliation_required"
    verifying.allocator.reconcile_on.clear()
    closed = verifying.agent.run_assignment(verifying.replay_assignment())
    assert closed.outcome is NodeRunOutcome.COLLECTED
    assert closed.node_execution_receipt == central_hold.node_execution_receipt
    assert verifying.allocator.calls.count("verifying") == 2

    adoption = _Harness(tmp_path / "adoption")
    assert adoption.agent.run_once().outcome is NodeRunOutcome.RUNNING
    adoption.allocator.reconcile_on.add("adopt")
    rejected_adoption = adoption.agent.run_assignment(
        adoption.replay_assignment(status="reconciliation_required")
    )
    assert rejected_adoption.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert rejected_adoption.adoption_receipt is not None
    local = adoption.state.load_state(adoption.reservation.attempt_id)
    assert local is not None
    assert local.fencing_epoch == 2
    assert local.adoption_sequence == 1
    assert adoption.allocator.current.fencing_epoch == 2
    adoption.allocator.reconcile_on.clear()
    resumed = adoption.agent.run_assignment(adoption.replay_assignment())
    assert resumed.outcome is NodeRunOutcome.ADOPTED
    assert adoption.allocator.current.fencing_epoch == 3


def test_committed_launch_gap_absence_is_exact_failure_and_never_relaunched(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.crash_before_runtime_call = True
    with pytest.raises(SystemExit, match="committed launch gap"):
        harness.agent.run_once()
    assert harness.runtime.launch_calls == 0

    collected = harness.agent.run_assignment(harness.replay_assignment())
    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert collected.node_execution_receipt is not None
    assert collected.node_execution_receipt.exit_code == 255
    assert collected.terminal_disposition is NodeTerminalDisposition.PROCESS_FAILED
    assert collected.inspection_receipt is not None
    assert collected.inspection_receipt.state is RuntimeInspectionState.ABSENT
    assert collected.artifact_manifest is not None
    assert collected.artifact_manifest.entries == ()
    assert harness.runtime.launch_calls == 0
    assert harness.allocator.retained == 0


@pytest.mark.parametrize(
    "rogue_state",
    [RuntimeInspectionState.RUNNING, RuntimeInspectionState.TERMINATED],
)
def test_prepared_runtime_observation_is_never_retroactively_authorized_or_signed(
    tmp_path: Path, rogue_state: RuntimeInspectionState
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.prepared_observation_state = rogue_state

    result = harness.agent.run_once()

    assert result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert result.inspection_receipt is None
    assert result.node_execution_receipt is None
    assert "start" not in harness.allocator.calls
    assert "running" not in harness.allocator.calls
    assert "terminated" not in harness.allocator.calls
    assert "verifying" not in harness.allocator.calls


def test_stale_fence_and_unknown_runtime_retain_holds_without_duplicate(tmp_path: Path) -> None:
    stale = _Harness(tmp_path / "stale")
    assert stale.agent.run_once().outcome is NodeRunOutcome.RUNNING
    stale.allocator.reject_heartbeat = True
    result = stale.agent.run_assignment(stale.replay_assignment())
    assert result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert stale.runtime.launch_calls == 1
    assert "terminated" not in stale.allocator.calls
    assert "verifying" not in stale.allocator.calls

    unknown = _Harness(tmp_path / "unknown")
    assert unknown.agent.run_once().outcome is NodeRunOutcome.RUNNING
    unknown.runtime.unknown()
    result = unknown.agent.run_assignment(unknown.replay_assignment())
    assert result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert result.inspection_receipt is not None
    assert result.inspection_receipt.state is RuntimeInspectionState.UNKNOWN
    assert unknown.allocator.retained == 1
    assert unknown.runtime.launch_calls == 1
    assert "terminated" not in unknown.allocator.calls


def test_same_attempt_adoption_requires_running_inspection_and_rotates_exact_token_fence(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING

    adopted = harness.agent.run_assignment(
        harness.replay_assignment(status="reconciliation_required")
    )

    assert adopted.outcome is NodeRunOutcome.ADOPTED
    assert adopted.inspection_receipt is not None
    assert adopted.inspection_receipt.state is RuntimeInspectionState.RUNNING
    assert adopted.adoption_receipt is harness.allocator.last_adoption
    assert adopted.adoption_receipt is not None
    assert adopted.adoption_receipt.previous_fencing_epoch == 1
    assert adopted.adoption_receipt.new_fencing_epoch == 2
    assert adopted.adoption_receipt.runtime_identity_sha256 == (
        adopted.runtime_identity.runtime_identity_sha256
    )
    assert harness.raw_token.encode() not in canonical_json_bytes(adopted.adoption_receipt)
    assert harness.runtime.launch_calls == 1


def test_adoption_commit_crash_rolls_forward_exact_pending_journal_without_remint(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.allocator.crash_after_adoption = True

    with pytest.raises(SystemExit, match="adoption commit"):
        harness.agent.run_assignment(harness.replay_assignment(status="reconciliation_required"))

    journal_files = tuple((harness.state.root / "adoptions").iterdir())
    assert len(journal_files) == 1
    journal_payload = journal_files[0].read_bytes()
    assert harness.raw_token.encode() not in journal_payload
    assert harness.allocator.current.fencing_epoch == 2
    committed_receipt = harness.allocator.last_adoption
    assert committed_receipt is not None

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.ADOPTED
    assert recovered.adoption_receipt == committed_receipt
    assert harness.allocator.calls.count("adopt") == 1
    assert len(tuple((harness.state.root / "adoptions").iterdir())) == 1
    assert harness.runtime.launch_calls == 1


def test_adoption_token_to_journal_crash_reuses_immutable_rotation_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    original_save = harness.state.save_pending_adoption
    crash_next = True

    def crash_once(**kwargs) -> None:
        nonlocal crash_next
        if crash_next:
            crash_next = False
            raise SystemExit("crash between rotation token and adoption journal")
        original_save(**kwargs)

    monkeypatch.setattr(harness.state, "save_pending_adoption", crash_once)
    with pytest.raises(SystemExit, match="rotation token"):
        harness.agent.run_assignment(harness.replay_assignment(status="reconciliation_required"))

    orphan_token = harness.state.load_existing_token(
        attempt_id=harness.reservation.attempt_id,
        fencing_epoch=2,
    )
    assert orphan_token is not None
    assert tuple((harness.state.root / "adoptions").iterdir()) == ()
    assert harness.allocator.current.fencing_epoch == 1

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.ADOPTED
    assert recovered.adoption_receipt is not None
    assert (
        recovered.adoption_receipt.new_lease_token_sha256
        == hashlib.sha256(orphan_token.encode()).hexdigest()
    )
    assert harness.allocator.calls.count("adopt") == 1
    assert harness.runtime.launch_calls == 1


def test_expired_uncommitted_adoption_supersedes_only_proof_not_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.allocator.crash_before_adoption_commit = True

    with pytest.raises(SystemExit, match="before allocator adoption commit"):
        harness.agent.run_assignment(harness.replay_assignment(status="reconciliation_required"))

    stale_receipt = harness.allocator.last_adoption
    assert stale_receipt is not None
    assert harness.allocator.current.fencing_epoch == 1
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    original_save = harness.state.save_pending_adoption
    crash_next = True

    def crash_once(**kwargs) -> None:
        nonlocal crash_next
        if crash_next:
            crash_next = False
            raise SystemExit("crash between refreshed proof and supersession journal")
        original_save(**kwargs)

    monkeypatch.setattr(harness.state, "save_pending_adoption", crash_once)
    with pytest.raises(SystemExit, match="supersession journal"):
        harness.agent.run_assignment(harness.replay_assignment())

    adopted = harness.agent.run_assignment(harness.replay_assignment())
    assert adopted.outcome is NodeRunOutcome.ADOPTED
    assert adopted.adoption_receipt is not None
    assert adopted.adoption_receipt != stale_receipt
    assert adopted.adoption_receipt.adoption_sequence == stale_receipt.adoption_sequence
    assert adopted.adoption_receipt.new_fencing_epoch == stale_receipt.new_fencing_epoch
    assert adopted.adoption_receipt.new_lease_token_sha256 == (stale_receipt.new_lease_token_sha256)
    assert (
        adopted.adoption_receipt.runtime_inspection_receipt.inspection_sequence
        > stale_receipt.runtime_inspection_receipt.inspection_sequence
    )
    assert len(tuple((harness.state.root / "adoptions").iterdir())) == 2
    assert harness.allocator.current.fencing_epoch == 2
    assert harness.allocator.calls.count("adopt") == 2
    assert harness.runtime.launch_calls == 1


def test_failure_and_absent_terminal_outcomes_emit_exact_empty_or_partial_manifest(
    tmp_path: Path,
) -> None:
    failed = _Harness(tmp_path / "failed")
    assert failed.agent.run_once().outcome is NodeRunOutcome.RUNNING
    failed.runtime.finish(exit_code=17, write_declared=False)
    result = failed.agent.run_assignment(failed.replay_assignment())
    assert result.outcome is NodeRunOutcome.COLLECTED
    assert result.artifact_manifest is not None
    assert result.artifact_manifest.entries == ()
    assert result.node_execution_receipt is not None
    assert result.node_execution_receipt.exit_code == 17
    assert result.terminal_disposition is NodeTerminalDisposition.PROCESS_FAILED
    assert result.node_execution_receipt.artifact_manifest_sha256 == (
        result.artifact_manifest.manifest_sha256
    )

    absent = _Harness(tmp_path / "absent")
    assert absent.agent.run_once().outcome is NodeRunOutcome.RUNNING
    absent.runtime.state = RuntimeInspectionState.ABSENT
    result = absent.agent.run_assignment(absent.replay_assignment())
    assert result.outcome is NodeRunOutcome.COLLECTED
    assert result.artifact_manifest is not None
    assert result.artifact_manifest.entries == ()
    assert result.node_execution_receipt is not None
    assert result.node_execution_receipt.exit_code == 255
    assert result.terminal_disposition is NodeTerminalDisposition.PROCESS_FAILED
    assert result.inspection_receipt is not None
    assert result.inspection_receipt.state is RuntimeInspectionState.ABSENT


def test_termination_commit_crash_reuses_exact_journal_without_new_transition(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_mark_terminated = True

    with pytest.raises(SystemExit, match="termination commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    committed_inspection = harness.allocator.last_inspection
    assert committed_inspection is not None
    assert harness.allocator.current.status == "terminated"
    assert len(tuple((harness.state.root / "terminations").iterdir())) == 1

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.inspection_receipt == committed_inspection
    assert harness.allocator.calls.count("terminated") == 1
    assert harness.allocator.calls.count("verifying") == 1


def test_expired_uncommitted_terminal_result_refreshes_stopped_proof_generation(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_before_mark_verifying = True

    with pytest.raises(SystemExit, match="verification commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    old_receipt = harness.state.load_terminal_result(
        harness.reservation.attempt_id,
        inspection_sequence=harness.state.load_state(
            harness.reservation.attempt_id
        ).inspection_sequence,
    )[1]
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    refreshed = harness.agent.run_assignment(harness.replay_assignment())
    assert refreshed.outcome is NodeRunOutcome.COLLECTED
    assert refreshed.node_execution_receipt is not None
    assert refreshed.node_execution_receipt != old_receipt
    assert refreshed.node_execution_receipt.artifact_manifest_sha256 == (
        old_receipt.artifact_manifest_sha256
    )
    assert (
        refreshed.inspection_receipt.inspection_sequence
        > old_receipt.termination_inspection_receipt.inspection_sequence
    )
    assert len(tuple((harness.state.root / "results").iterdir())) == 2


def test_exit_zero_missing_required_and_post_deadline_are_bounded_typed_failures(
    tmp_path: Path,
) -> None:
    missing = _Harness(tmp_path / "missing")
    assert missing.agent.run_once().outcome is NodeRunOutcome.RUNNING
    missing.runtime.finish(exit_code=0, write_declared=False)
    invalid = missing.agent.run_assignment(missing.replay_assignment())
    assert invalid.outcome is NodeRunOutcome.COLLECTED
    assert invalid.terminal_disposition is NodeTerminalDisposition.INVALID_OUTPUT
    assert invalid.node_execution_receipt is not None
    assert invalid.node_execution_receipt.exit_code == 0
    assert invalid.artifact_manifest is not None
    assert invalid.artifact_manifest.entries == ()

    late = _Harness(tmp_path / "late")
    assert late.agent.run_once().outcome is NodeRunOutcome.RUNNING
    late.clock.current += timedelta(minutes=16)
    late.clock.monotonic += 16 * 60 * 1_000_000_000
    late.runtime.finish(exit_code=0)
    timed_out = late.agent.run_assignment(late.replay_assignment(status="reconciliation_required"))
    assert timed_out.outcome is NodeRunOutcome.COLLECTED
    assert timed_out.terminal_disposition is NodeTerminalDisposition.TIMEOUT
    assert timed_out.node_execution_receipt is not None
    assert timed_out.node_execution_receipt.exit_code == 0
    assert timed_out.artifact_manifest is not None
    assert timed_out.artifact_manifest.entries


def test_output_tree_mismatch_fails_closed_after_stop_without_terminal_receipt(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0, write_extra=True)

    with pytest.raises(OutputCollectionRejected, match="exact declared tree"):
        harness.agent.run_assignment(harness.replay_assignment())

    assert harness.allocator.last_node_receipt is None
    assert "verifying" not in harness.allocator.calls
    assert harness.runtime.launch_calls == 1


def test_missing_initial_raw_token_cannot_prepare_runtime_or_forge_absent_release(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    missing = replace(harness.assignment, lease_token=None)

    with pytest.raises(LocalStateError, match="missing"):
        harness.agent.run_assignment(missing)

    assert harness.runtime.prepare_calls == 0
    assert harness.runtime.launch_calls == 0
    assert "terminated" not in harness.allocator.calls
    assert "retain" not in harness.allocator.calls


def test_singleton_flock_prevents_same_attempt_double_agent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    attempt_id = harness.reservation.attempt_id

    with harness.state.attempt_lock(
        attempt_id=attempt_id, monotonic_ns=harness.clock.monotonic_ns()
    ) as held:
        assert held is not None
        result = harness.agent.run_assignment(harness.assignment)

    assert result.outcome is NodeRunOutcome.LOCKED_BY_PEER
    assert harness.runtime.prepare_calls == 0
    assert harness.runtime.launch_calls == 0


def test_assignment_mutation_or_unpinned_launch_binding_is_rejected_before_runtime(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    forged_intent = harness.intent.model_copy(update={"command_sha256": _digest("forged")})
    forged = replace(harness.assignment, intent=forged_intent)

    with pytest.raises(AssignmentRejected):
        harness.agent.run_assignment(forged)
    assert harness.runtime.prepare_calls == 0

    other_spec = harness.spec.model_copy(update={"command_sha256": _digest("not-selected")})
    agent = QualificationNodeAgent(
        node_authority=harness.authority,
        qualification_authority=QualificationAuthorityVerifier(harness.case.pin),
        node_signing_private_key=PRIVATE_KEY,
        boot_id="boot.qualification-001",
        allocator_principal_id="principal:allocator",
        allocator=harness.allocator,
        runtime=harness.runtime,
        artifact_quarantine=harness.artifacts,
        launch_registry=PinnedLaunchRegistry((other_spec,)),
        state_store=harness.state,
        input_materializer=harness.materializer,
        clock=harness.clock,
    )
    with pytest.raises(AssignmentRejected, match="no launch pin"):
        agent.run_assignment(harness.assignment)


def test_resource_projection_and_runtime_enforcement_evidence_fail_closed(tmp_path: Path) -> None:
    divergent = _Harness(tmp_path / "divergent")
    forged_reservation = replace(
        divergent.reservation,
        cpu_cores=divergent.reservation.cpu_cores + 1,
    )
    with pytest.raises(AssignmentRejected, match="placement projection"):
        divergent.agent.run_assignment(
            replace(divergent.assignment, reservation=forged_reservation)
        )
    assert divergent.runtime.prepare_calls == 0

    device = ReservedDeviceBinding(
        device_id="gpu.0",
        hardware_uuid="GPU-deadbeef",
        fencing_epoch=1,
        requested_memory_bytes=1024,
    )
    unexpected_device = replace(
        divergent.reservation,
        selected_resource_ids=("cpu.socket-0", "gpu.0"),
        device_leases=(device,),
    )
    with pytest.raises(AssignmentRejected, match="placement projection"):
        divergent.agent.run_assignment(replace(divergent.assignment, reservation=unexpected_device))

    forged_runtime = _Harness(tmp_path / "runtime")
    forged_runtime.runtime.forge_placement_evidence = True
    with pytest.raises(RuntimeRejected, match="placement/fence"):
        forged_runtime.agent.run_once()
    assert "start" not in forged_runtime.allocator.calls
    assert forged_runtime.runtime.launch_calls == 0


def test_stale_inspection_and_expired_node_pin_cannot_create_terminal_release(
    tmp_path: Path,
) -> None:
    stale_running = _Harness(tmp_path / "stale-running")
    assert stale_running.agent.run_once().outcome is NodeRunOutcome.RUNNING
    heartbeat_count = stale_running.allocator.calls.count("heartbeat")
    stale_running.clock.current += timedelta(seconds=20)
    stale_running.clock.monotonic += 20 * 1_000_000_000
    stale_running.runtime.observation_age_seconds = 11
    with pytest.raises(RuntimeRejected, match="contemporaneous"):
        stale_running.agent.run_assignment(stale_running.replay_assignment())
    assert stale_running.allocator.calls.count("heartbeat") == heartbeat_count

    stale = _Harness(tmp_path / "stale")
    assert stale.agent.run_once().outcome is NodeRunOutcome.RUNNING
    stale.runtime.unknown()
    stale.clock.current += timedelta(seconds=20)
    stale.clock.monotonic += 20 * 1_000_000_000
    stale.runtime.observation_age_seconds = 11
    with pytest.raises(RuntimeRejected, match="contemporaneous"):
        stale.agent.run_assignment(stale.replay_assignment())
    assert "terminated" not in stale.allocator.calls
    assert "verifying" not in stale.allocator.calls

    expired = _Harness(
        tmp_path / "expired",
        worker_enrollment_expires_at=NOW + timedelta(minutes=2),
    )
    assert expired.agent.run_once().outcome is NodeRunOutcome.RUNNING
    expired.clock.current = NOW + timedelta(minutes=3)
    expired.clock.monotonic += 3 * 60 * 1_000_000_000
    expired.runtime.finish(exit_code=0)
    with pytest.raises(RuntimeRejected, match="cannot cover"):
        expired.agent.run_assignment(expired.replay_assignment())
    assert "terminated" not in expired.allocator.calls
    assert "verifying" not in expired.allocator.calls
