from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.artifact_store import LocalArtifactStore
import aletheia.execution.oci_deployment as oci_deployment_module
from aletheia.execution.oci_deployment import (
    LoopbackOutputQuotaProvisioningService,
    PreinstalledOutputWorkspaceRootPin,
    _QuotaFilesystemFormatted,
    _QuotaLoopAttachment,
)
from aletheia.execution.node_agent import (
    AssignmentRejected,
    AttemptPhase,
    LocalStateError,
    NodeLeaseRejected,
    NodeLocalStateStore,
    NodeProofReplayRejected,
    NodeProofReplayRejectionCode,
    NodeReservation,
    NodeRunOutcome,
    NodeTerminalDisposition,
    OutputCollectionRejected,
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedLaunchRegistry,
    PinnedLaunchSpec,
    PreRuntimeAbsenceDecision,
    PreRuntimeAbsenceDisposition,
    QualificationAssignment,
    QualificationNodeAgent,
    ReservedDeviceBinding,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeRejected,
    RuntimeStartAuthorization,
    TerminalArtifactCommit,
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
    QualificationVerificationError,
    RuntimeInspectionReceipt,
    RuntimeInspectionState,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
    issue_worker_node_enrollment,
    qualification_key_id,
    verify_worker_node_enrollment,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    HistoricalPreRuntimeRecoveryLineage,
    InputMaterializationEntry,
    InputMaterializationReceipt,
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    NodeRuntimeLaunchReceipt,
    NodeRuntimeTerminationReceipt,
    OutputQuotaProvisioningReceipt,
    PinnedOutputWorkspaceRoot,
    PinnedInputPath,
    PreRuntimeAbsenceReceipt,
    QualificationTerminalDeadlineExpiration,
    QualificationTerminalSubmission,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeFenceRebindEvidence,
    RuntimeFenceRebindReceipt,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimeLaunchEvidence,
    RuntimePreparation,
    RuntimeTerminationAcceptanceChallenge,
    issue_accepted_runtime_termination,
    issue_accepted_qualification_terminal_submission,
    issue_historical_runtime_recovery_grant,
    issue_qualification_terminal_deadline_expiration,
    issue_qualification_terminal_submission,
    issue_runtime_launch_authorization,
    issue_runtime_termination_acceptance_challenge,
    validate_runtime_fence_rebind_evidence,
    verify_pre_runtime_absence_receipt,
    verify_qualification_terminal_deadline_expiration,
    verify_qualification_terminal_submission,
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
RUNTIME_CONTROL_PRIVATE_KEY = bytes(range(2, 34))
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


def _runtime_control_pin() -> RuntimeControlAuthorityPin:
    public_key = _public_key_hex(RUNTIME_CONTROL_PRIVATE_KEY)
    return RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-control-policy"),
        principal_id="principal:runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )


@dataclass(frozen=True)
class QualificationCase:
    intent: ExecutionIntent
    work_order_node: object
    grant: EngineeringQualificationGrant
    pin: QualificationAuthorityPin
    observed_at: datetime


def _qualification_case(*, artifact_quota_bytes: int | None = None) -> QualificationCase:
    fixture = fixture_by_name("grouped_regression")
    request = fixture.request
    if artifact_quota_bytes is not None:
        protocol = request.protocol
        first = protocol.steps[0]
        delta = artifact_quota_bytes - first.resource_request.artifact_quota_bytes
        first = first.model_copy(
            update={
                "resource_request": first.resource_request.model_copy(
                    update={"artifact_quota_bytes": artifact_quota_bytes}
                )
            }
        )
        protocol = protocol.model_copy(
            update={
                "steps": (first, *protocol.steps[1:]),
                "resource_budget": protocol.resource_budget.model_copy(
                    update={
                        "maximum_total_artifact_bytes": (
                            protocol.resource_budget.maximum_total_artifact_bytes + delta
                        )
                    }
                ),
            }
        )
        request = request.model_copy(update={"protocol": protocol})
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

    def ensure_verified_inputs(self, *, intent, destination: Path) -> InputMaterializationReceipt:
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
        if stat.S_IMODE(target.stat().st_mode) != 0o400:
            target.chmod(0o400)
        if stat.S_IMODE(destination.stat().st_mode) != 0o500:
            destination.chmod(0o500)
        target_identity = target.stat()
        root_identity = destination.stat()
        content_sha256 = hashlib.sha256(expected).hexdigest()
        receipt = InputMaterializationReceipt(
            intent_sha256=intent.intent_sha256,
            execution_id=intent.execution_id,
            infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
            entries=(
                InputMaterializationEntry(
                    input_port_id=intent.input_artifact_bindings[0].input_port_id,
                    verified_receipt_sha256=(
                        intent.input_artifact_bindings[0].artifact_verified_receipt_sha256
                    ),
                    content_sha256=content_sha256,
                    content_bytes=len(expected),
                    relative_path="input.json",
                    staged_file_identity_sha256=canonical_sha256(
                        {
                            "schema": "test.staged_file.v2",
                            "content_sha256": content_sha256,
                            "relative_path": "input.json",
                            "ctime_ns": target_identity.st_ctime_ns,
                            "mode": stat.S_IMODE(target_identity.st_mode),
                        }
                    ),
                ),
            ),
            staged_root_identity_sha256=canonical_sha256(
                {
                    "schema": "test.staged_root.v2",
                    "intent_sha256": intent.intent_sha256,
                    "content_sha256": content_sha256,
                    "ctime_ns": root_identity.st_ctime_ns,
                    "mode": stat.S_IMODE(root_identity.st_mode),
                }
            ),
            materializer_principal_id="principal:test-input-materializer",
            materialized_at=NOW,
        )
        if self.crash_after_write:
            self.crash_after_write = False
            raise SystemExit("crash after verified input materialization")
        return receipt


class _OutputQuotaProvisioner:
    """Bounded test fake: returns exact current root identity and never mounts."""

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        if expected_receipt is not None:
            assert expected_receipt.output_root == str(output_root)
            assert expected_receipt.output_quota_bytes == output_quota_bytes
            return expected_receipt
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
            mount_id=1,
            mount_parent_id=1,
            block_device_major=os.major(metadata.st_dev),
            block_device_minor=os.minor(metadata.st_dev),
            block_device_capacity_bytes=output_quota_bytes,
            filesystem_type="ext4",
            filesystem_uuid_sha256=_digest("test-output-filesystem"),
            mount_options=("nodev", "noexec", "nosuid", "rw"),
            backing_file_identity_sha256=_digest(f"test-output-backing:{attempt_id}"),
            provisioner_policy_sha256=_digest("test-output-provisioner-policy"),
            provisioner_principal_id="principal:test-output-provisioner",
            provisioned_at=NOW,
        )


class _Runtime:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.state = RuntimeInspectionState.ABSENT
        self.prepare_calls = 0
        self.launch_calls = 0
        self.rebind_calls = 0
        self.preparation: RuntimePreparation | None = None
        self.identity: NodeRuntimeIdentity | None = None
        self.request: RuntimeLaunchRequest | None = None
        self.exit_code: int | None = None
        self.ended_at: datetime | None = None
        self.ended_monotonic_ns: int | None = None
        self.crash_after_launch = False
        self.crash_before_runtime_call = False
        self.crash_after_create = False
        self.quick_exit_before_identity = False
        self.cleanup_calls = 0
        self.cleanup_mutations = 0
        self.created_without_start = False
        self.crash_after_prepare = False
        self.prepared_observation_state: RuntimeInspectionState | None = None
        self.forge_placement_evidence = False
        self.observation_age_seconds = 0
        self.runtime_control_journal_sha256 = _digest("runtime-control:absent")
        self.engine_launch_journal_sha256 = _digest("engine-launch")
        self.current_fencing_epoch = 1
        self.current_lease_token_sha256: str | None = None
        self.prelaunch_absence_epoch = 1
        self.prelaunch_authorization_request_sha256: str | None = None
        self.prelaunch_authorization_sha256: str | None = None

    def _identity(self, request: RuntimeLaunchRequest) -> NodeRuntimeIdentity:
        return NodeRuntimeIdentity(
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

    def prepare(self, *, request: RuntimeLaunchRequest) -> RuntimePreparation:
        self.prepare_calls += 1
        if self.request is not None:
            assert request == self.request
        self.request = request
        self.current_fencing_epoch = request.fencing_epoch
        self.current_lease_token_sha256 = request.lease_token_sha256
        if self.preparation is None:
            self.preparation = RuntimePreparation(
                node_manifest_sha256=request.node_manifest_sha256,
                node_id=request.node_id,
                boot_id=request.boot_id,
                execution_id=request.execution_id,
                infrastructure_attempt_id=request.attempt_id,
                intent_sha256=request.intent_sha256,
                runtime_id=request.runtime_id,
                runtime_engine=request.spec.runtime_engine,
                launch_spec_sha256=request.spec.launch_spec_sha256,
                workload_executable_sha256=request.spec.executable_sha256,
                workload_argv=request.spec.argv,
                runtime_request_sha256=request.runtime_request_sha256,
                enforced_placement_sha256=request.enforced_placement_sha256,
                input_materialization_receipt_sha256=(
                    request.input_materialization_receipt.materialization_receipt_sha256
                ),
                output_quota_provisioning_receipt_sha256=(
                    request.output_quota_provisioning_receipt.provisioning_receipt_sha256
                ),
                fencing_epoch=request.fencing_epoch,
                lease_token_sha256=request.lease_token_sha256,
                prepared_runtime_locator_sha256=_digest(f"prepared:{request.runtime_id}"),
                oci_config_sha256=_digest(f"oci:{request.runtime_id}"),
                prepared_at=self.clock.now(),
                prepared_monotonic_ns=self.clock.monotonic_ns(),
            )
        if self.prepared_observation_state is not None:
            self.state = self.prepared_observation_state
            self.identity = self._identity(request)
            if self.state is RuntimeInspectionState.TERMINATED:
                self.exit_code = 0
                self.ended_at = self.clock.now()
                self.ended_monotonic_ns = self.clock.monotonic_ns()
        if self.crash_after_prepare:
            self.crash_after_prepare = False
            raise SystemExit("crash after idempotent runtime preparation")
        return self.preparation

    def inspect(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity | None,
    ) -> RuntimeObservation:
        assert self.preparation == preparation
        if identity is not None:
            assert request.runtime_id == identity.runtime_id
        observed_identity = None if self.state is RuntimeInspectionState.ABSENT else self.identity
        terminal = self.state is RuntimeInspectionState.TERMINATED
        return RuntimeObservation(
            state=self.state,
            preparation_sha256=preparation.preparation_sha256,
            runtime_identity=observed_identity,
            runtime_identity_sha256=(
                observed_identity.runtime_identity_sha256 if observed_identity is not None else None
            ),
            enforced_placement_sha256=(
                _digest("forged-placement")
                if self.forge_placement_evidence
                else request.enforced_placement_sha256
            ),
            input_materialization_receipt_sha256=(
                request.input_materialization_receipt.materialization_receipt_sha256
            ),
            enforced_fencing_epoch=self.current_fencing_epoch,
            enforced_lease_token_sha256=self.current_lease_token_sha256,
            inspection_evidence_sha256=_digest(
                f"inspect:{request.runtime_id}:{self.state.value}:{self.clock.monotonic}"
            ),
            runtime_control_journal_sha256=self.runtime_control_journal_sha256,
            prelaunch_absence_journal_sha256=(
                _digest(f"absence:{request.runtime_id}:{self.prelaunch_absence_epoch}")
                if self.state is RuntimeInspectionState.ABSENT
                else None
            ),
            prelaunch_absence_epoch=(
                self.prelaunch_absence_epoch
                if self.state is RuntimeInspectionState.ABSENT
                else None
            ),
            prelaunch_authorization_request_sha256=(
                self.prelaunch_authorization_request_sha256
                if self.state is RuntimeInspectionState.ABSENT
                else None
            ),
            prelaunch_authorization_sha256=(
                self.prelaunch_authorization_sha256
                if self.state is RuntimeInspectionState.ABSENT
                else None
            ),
            engine_terminal_journal_sha256=(
                _digest(f"terminal:{request.runtime_id}:{self.exit_code}") if terminal else None
            ),
            inspected_at=self.clock.now() - timedelta(seconds=self.observation_age_seconds),
            inspected_monotonic_ns=max(
                observed_identity.started_monotonic_ns if observed_identity is not None else 0,
                self.clock.monotonic_ns() - self.observation_age_seconds * 1_000_000_000,
            ),
            exit_code=self.exit_code,
            ended_at=self.ended_at,
            ended_monotonic_ns=self.ended_monotonic_ns,
        )

    def ensure_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> RuntimeLaunchEvidence:
        assert preparation == self.preparation
        assert authorization.authorization_request_sha256 == (authorization_request.request_sha256)
        assert authorization.runtime_preparation_sha256 == preparation.preparation_sha256
        assert (pre_runtime_absence_receipt is None) == (
            authorization_request.pre_runtime_absence_epoch == 0
        )
        self.request = request
        if self.crash_before_runtime_call:
            self.crash_before_runtime_call = False
            raise SystemExit("crash in committed launch gap")
        if self.crash_after_create:
            self.crash_after_create = False
            self.created_without_start = True
            self.state = RuntimeInspectionState.UNKNOWN
            raise SystemExit("node crashed after engine create before start")
        if self.quick_exit_before_identity:
            self.quick_exit_before_identity = False
            self.state = RuntimeInspectionState.UNKNOWN
            raise SystemExit("workload exited before exact launch identity")
        if self.identity is None:
            self.launch_calls += 1
            self.identity = self._identity(request)
            self.runtime_control_journal_sha256 = _digest(f"runtime-control:{request.runtime_id}:1")
        self.state = RuntimeInspectionState.RUNNING
        if self.crash_after_launch:
            self.crash_after_launch = False
            raise SystemExit("node process crashed after idempotent runtime launch")
        assert self.identity is not None
        return self._launch_evidence(
            request=request,
            preparation=preparation,
            authorization=authorization,
        )

    def _launch_evidence(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization: RuntimeLaunchAuthorization,
    ) -> RuntimeLaunchEvidence:
        assert self.identity is not None
        return RuntimeLaunchEvidence(
            preparation_sha256=preparation.preparation_sha256,
            runtime_launch_authorization_sha256=authorization.authorization_sha256,
            runtime_identity=self.identity,
            runtime_identity_sha256=self.identity.runtime_identity_sha256,
            engine_start_monotonic_lower_bound_ns=self.identity.started_monotonic_ns,
            engine_start_monotonic_upper_bound_exclusive_ns=(
                self.identity.started_monotonic_ns + 1
            ),
            enforced_placement_sha256=request.enforced_placement_sha256,
            input_materialization_receipt_sha256=(
                request.input_materialization_receipt.materialization_receipt_sha256
            ),
            enforced_fencing_epoch=request.fencing_epoch,
            enforced_lease_token_sha256=request.lease_token_sha256,
            engine_launch_journal_sha256=self.engine_launch_journal_sha256,
            launch_evidence_sha256=_digest(f"launch-evidence:{request.runtime_id}"),
            observed_at=self.clock.now(),
            observed_monotonic_ns=self.clock.monotonic_ns(),
        )

    def recover_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> RuntimeLaunchEvidence | None:
        assert (pre_runtime_absence_receipt is None) == (
            authorization_request.pre_runtime_absence_epoch == 0
        )
        if self.identity is None:
            return None
        return self._launch_evidence(
            request=request,
            preparation=preparation,
            authorization=authorization,
        )

    def cleanup_never_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> RuntimeObservation:
        self.cleanup_calls += 1
        if self.identity is not None or (
            self.state is RuntimeInspectionState.UNKNOWN and not self.created_without_start
        ):
            self.state = RuntimeInspectionState.UNKNOWN
            return self.inspect(request=request, preparation=preparation, identity=None)
        if self.created_without_start:
            self.cleanup_mutations += 1
        self.created_without_start = False
        self.prelaunch_absence_epoch = authorization_request.pre_runtime_absence_epoch + 1
        self.prelaunch_authorization_request_sha256 = authorization_request.request_sha256
        self.prelaunch_authorization_sha256 = authorization.authorization_sha256
        self.state = RuntimeInspectionState.ABSENT
        return self.inspect(request=request, preparation=preparation, identity=None)

    def rebind_fence(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity,
    ) -> RuntimeFenceRebindEvidence:
        assert preparation == self.preparation
        assert identity == self.identity
        assert request.previous_fencing_epoch == self.current_fencing_epoch
        assert request.previous_lease_token_sha256 == self.current_lease_token_sha256
        assert (
            request.expected_runtime_control_journal_sha256 == self.runtime_control_journal_sha256
        )
        self.rebind_calls += 1
        previous_journal = self.runtime_control_journal_sha256
        self.current_fencing_epoch = request.new_fencing_epoch
        self.current_lease_token_sha256 = request.new_lease_token_sha256
        self.runtime_control_journal_sha256 = _digest(
            f"runtime-control:{identity.runtime_id}:{request.new_fencing_epoch}"
        )
        return RuntimeFenceRebindEvidence(
            request_sha256=request.request_sha256,
            preparation_sha256=request.preparation_sha256,
            runtime_identity_sha256=request.runtime_identity_sha256,
            previous_fencing_epoch=request.previous_fencing_epoch,
            previous_lease_token_sha256=request.previous_lease_token_sha256,
            new_fencing_epoch=request.new_fencing_epoch,
            new_lease_token_sha256=request.new_lease_token_sha256,
            rebind_sequence=request.rebind_sequence,
            previous_runtime_control_journal_sha256=previous_journal,
            new_runtime_control_journal_sha256=self.runtime_control_journal_sha256,
            rebind_evidence_sha256=_digest(
                f"rebind:{identity.runtime_id}:{request.rebind_sequence}"
            ),
            rebound_at=self.clock.now(),
            rebound_monotonic_ns=self.clock.monotonic_ns(),
        )

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
    def __init__(
        self, assignment: QualificationAssignment, *, node_authority, clock: _Clock
    ) -> None:
        self.assignment = assignment
        self.node_authority = node_authority
        self.clock = clock
        self.current = assignment.reservation
        self.calls: list[str] = []
        self.start_crash = False
        self.crash_after_start_commit = False
        self.start_return_delay_ns = 0
        self._start_request: RuntimeLaunchAuthorizationRequest | None = None
        self._start_authorization: RuntimeStartAuthorization | None = None
        self.crash_after_mark_running = False
        self.crash_after_mark_terminated = False
        self.crash_after_challenge_commit = False
        self.crash_after_absence_commit = False
        self.crash_before_mark_verifying = False
        self.crash_after_terminal_acceptance_commit = False
        self.crash_after_adoption = False
        self.crash_before_adoption_commit = False
        self.reject_heartbeat = False
        self.reconcile_on: set[str] = set()
        self.retained = 0
        self.last_inspection: RuntimeInspectionReceipt | None = None
        self.last_adoption: AttemptAdoptionReceipt | None = None
        self.last_launch_receipt: NodeRuntimeLaunchReceipt | None = None
        self.last_challenge: RuntimeTerminationAcceptanceChallenge | None = None
        self.challenge_history: dict[int, RuntimeTerminationAcceptanceChallenge] = {}
        self.last_node_termination: NodeRuntimeTerminationReceipt | None = None
        self.last_accepted: AcceptedRuntimeTermination | None = None
        self.accepted_replay_calls = 0
        self.last_submission: QualificationTerminalSubmission | None = None
        self.last_terminal_acceptance: AcceptedQualificationTerminalSubmission | None = None
        self.last_absence_receipt = None
        self.last_absence_request: RuntimeLaunchAuthorizationRequest | None = None
        self.last_absence_decision: PreRuntimeAbsenceDecision | None = None
        self.last_artifact_verified_receipts = ()
        self._runtime_preparation: RuntimePreparation | None = None
        self.last_rebind_request: RuntimeFenceRebindRequest | None = None
        self.last_rebind_receipt: RuntimeFenceRebindReceipt | None = None
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
        runtime_preparation: RuntimePreparation,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> RuntimeStartAuthorization:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("start")
        if self._start_authorization is not None:
            assert self._start_request == launch_authorization_request
            assert (
                self._start_authorization.launch_authorization.runtime_preparation_sha256
                == runtime_preparation.preparation_sha256
            )
            return replace(self._start_authorization, replayed=True)
        if self.start_crash:
            self.start_crash = False
            raise SystemExit("allocator crashed before launch authorization committed")
        assert runtime_preparation.infrastructure_attempt_id == attempt_id
        self.current = replace(
            self.current,
            status=("reconciliation_required" if "start" in self.reconcile_on else "starting"),
        )
        authorization = issue_runtime_launch_authorization(
            pin=_runtime_control_pin(),
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            admission_sha256=self.current.admission_sha256,
            qualification_grant_sha256=self.current.grant_sha256,
            node_manifest_sha256=runtime_preparation.node_manifest_sha256,
            node_id=runtime_preparation.node_id,
            boot_id=runtime_preparation.boot_id,
            execution_id=runtime_preparation.execution_id,
            infrastructure_attempt_id=runtime_preparation.infrastructure_attempt_id,
            intent_sha256=runtime_preparation.intent_sha256,
            runtime_preparation_sha256=runtime_preparation.preparation_sha256,
            authorization_request_sha256=launch_authorization_request.request_sha256,
            launch_spec_sha256=runtime_preparation.launch_spec_sha256,
            oci_config_sha256=runtime_preparation.oci_config_sha256,
            workload_executable_sha256=runtime_preparation.workload_executable_sha256,
            workload_argv=runtime_preparation.workload_argv,
            enforced_placement_sha256=runtime_preparation.enforced_placement_sha256,
            input_materialization_receipt_sha256=(
                runtime_preparation.input_materialization_receipt_sha256
            ),
            fencing_epoch=fencing_epoch,
            lease_token_sha256=self.current.lease_token_sha256,
            lease_expires_at=self.current.lease_expires_at,
            hard_deadline=self.current.hard_deadline,
            issued_at=launch_authorization_request.requested_at,
            expires_at=min(
                launch_authorization_request.requested_at + timedelta(seconds=5),
                self.current.lease_expires_at,
            ),
            max_launch_delay_ns=5_000_000_000,
        )
        self._start_request = launch_authorization_request
        self._start_authorization = RuntimeStartAuthorization(
            reservation=self.current,
            launch_authorization=authorization,
        )
        if self.crash_after_start_commit:
            self.crash_after_start_commit = False
            raise SystemExit("crash after allocator start commit")
        self.clock.monotonic += self.start_return_delay_ns
        return self._start_authorization

    def mark_running(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
    ) -> NodeReservation:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("running")
        assert (
            node_runtime_launch_receipt.launch_evidence.runtime_identity.infrastructure_attempt_id
            == attempt_id
        )
        self.last_launch_receipt = node_runtime_launch_receipt
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

    def resolve_pre_runtime_absence(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        absence_receipt,
        replacement_launch_authorization_request: (RuntimeLaunchAuthorizationRequest | None),
    ) -> PreRuntimeAbsenceDecision:
        self._authority(attempt_id, lease_token, fencing_epoch)
        self.calls.append("absence")
        if self.last_absence_decision is not None:
            assert self.last_absence_receipt == absence_receipt
            assert self.last_absence_request == replacement_launch_authorization_request
            return self.last_absence_decision
        if not self.clock.now() < absence_receipt.expires_at:
            raise NodeProofReplayRejected(
                NodeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED,
                "exact absence receipt is stale and has no committed decision",
            )
        cleaned_launch = (
            absence_receipt.absence_evidence.prelaunch_authorization_request_sha256 is not None
        )
        prior_request = self._start_request if cleaned_launch else None
        prior_authorization = (
            self._start_authorization.launch_authorization
            if cleaned_launch and self._start_authorization is not None
            else None
        )
        verify_pre_runtime_absence_receipt(
            receipt=absence_receipt,
            preparation=runtime_preparation,
            authority=self.node_authority,
            observed_at=self.clock.now(),
            maximum_age_seconds=10,
            launch_authorization_request=prior_request,
            launch_authorization=prior_authorization,
            runtime_authority=(
                RuntimeControlAuthorityVerifier(_runtime_control_pin()) if cleaned_launch else None
            ),
        )
        self.last_absence_receipt = absence_receipt
        self.last_absence_request = replacement_launch_authorization_request
        if (
            self.clock.now() >= self.current.hard_deadline
            or replacement_launch_authorization_request is None
        ):
            self.current = replace(self.current, status="cancelled")
            decision = PreRuntimeAbsenceDecision(
                reservation=self.current,
                disposition=PreRuntimeAbsenceDisposition.RELEASED,
                pre_runtime_absence_receipt_sha256=(absence_receipt.absence_receipt_sha256),
            )
        else:
            assert replacement_launch_authorization_request is not None
            self.current = replace(self.current, status="starting")
            authorization = issue_runtime_launch_authorization(
                pin=_runtime_control_pin(),
                private_key=RUNTIME_CONTROL_PRIVATE_KEY,
                admission_sha256=self.current.admission_sha256,
                qualification_grant_sha256=self.current.grant_sha256,
                node_manifest_sha256=runtime_preparation.node_manifest_sha256,
                node_id=runtime_preparation.node_id,
                boot_id=runtime_preparation.boot_id,
                execution_id=runtime_preparation.execution_id,
                infrastructure_attempt_id=runtime_preparation.infrastructure_attempt_id,
                intent_sha256=runtime_preparation.intent_sha256,
                runtime_preparation_sha256=runtime_preparation.preparation_sha256,
                authorization_request_sha256=(
                    replacement_launch_authorization_request.request_sha256
                ),
                launch_spec_sha256=runtime_preparation.launch_spec_sha256,
                oci_config_sha256=runtime_preparation.oci_config_sha256,
                workload_executable_sha256=runtime_preparation.workload_executable_sha256,
                workload_argv=runtime_preparation.workload_argv,
                enforced_placement_sha256=runtime_preparation.enforced_placement_sha256,
                input_materialization_receipt_sha256=(
                    runtime_preparation.input_materialization_receipt_sha256
                ),
                fencing_epoch=fencing_epoch,
                lease_token_sha256=self.current.lease_token_sha256,
                lease_expires_at=self.current.lease_expires_at,
                hard_deadline=self.current.hard_deadline,
                issued_at=replacement_launch_authorization_request.requested_at,
                expires_at=min(
                    replacement_launch_authorization_request.requested_at + timedelta(seconds=5),
                    self.current.lease_expires_at,
                ),
                max_launch_delay_ns=5_000_000_000,
            )
            decision = PreRuntimeAbsenceDecision(
                reservation=self.current,
                disposition=PreRuntimeAbsenceDisposition.REAUTHORIZED,
                pre_runtime_absence_receipt_sha256=(absence_receipt.absence_receipt_sha256),
                replacement_launch_authorization_request=(replacement_launch_authorization_request),
                replacement_launch_authorization=authorization,
            )
            self._start_request = replacement_launch_authorization_request
            self._start_authorization = RuntimeStartAuthorization(
                reservation=self.current,
                launch_authorization=authorization,
            )
        self.last_absence_decision = decision
        if self.crash_after_absence_commit:
            self.crash_after_absence_commit = False
            raise SystemExit("crash after allocator absence commit")
        return decision

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
        runtime_fence_rebind_request: RuntimeFenceRebindRequest,
        runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt,
    ) -> NodeReservation:
        self._authority(
            receipt.infrastructure_attempt_id, previous_lease_token, previous_fencing_epoch
        )
        assert hashlib.sha256(new_lease_token.encode()).hexdigest() == (
            receipt.new_lease_token_sha256
        )
        self.calls.append("adopt")
        self.last_adoption = receipt
        validate_runtime_fence_rebind_evidence(
            request=runtime_fence_rebind_request,
            evidence=runtime_fence_rebind_receipt.evidence,
        )
        assert (
            runtime_fence_rebind_request.request_sha256
            == runtime_fence_rebind_receipt.evidence.request_sha256
        )
        self.last_rebind_request = runtime_fence_rebind_request
        assert runtime_fence_rebind_receipt.evidence.new_fencing_epoch == receipt.new_fencing_epoch
        assert (
            runtime_fence_rebind_receipt.evidence.new_lease_token_sha256
            == receipt.new_lease_token_sha256
        )
        self.last_rebind_receipt = runtime_fence_rebind_receipt
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
        assert inspection_receipt.state is RuntimeInspectionState.TERMINATED
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

    def challenge_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
        termination_evidence: RuntimeInspectionEvidence,
        inspection_sequence: int,
        artifact_submission_deadline: datetime,
    ) -> RuntimeTerminationAcceptanceChallenge:
        self._authority(attempt_id, lease_token, fencing_epoch)
        assert termination_evidence.state is RuntimeInspectionState.TERMINATED
        existing_challenge = self.challenge_history.get(inspection_sequence)
        if existing_challenge is not None:
            assert existing_challenge.runtime_preparation_sha256 == (
                runtime_preparation.preparation_sha256
            )
            assert existing_challenge.node_runtime_launch_receipt_sha256 == (
                node_runtime_launch_receipt.launch_receipt_sha256
            )
            assert existing_challenge.runtime_inspection_evidence_sha256 == (
                termination_evidence.inspection_sha256
            )
            if self.last_accepted is None and not self.clock.now() < existing_challenge.expires_at:
                raise NodeProofReplayRejected(
                    NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED,
                    "exact termination challenge expired without acceptance",
                )
            self.last_challenge = existing_challenge
            return existing_challenge
        if self.challenge_history:
            assert inspection_sequence == max(self.challenge_history) + 1
        self._runtime_preparation = runtime_preparation
        self.last_challenge = issue_runtime_termination_acceptance_challenge(
            pin=_runtime_control_pin(),
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            attempt_id=attempt_id,
            execution_id=runtime_preparation.execution_id,
            intent_sha256=runtime_preparation.intent_sha256,
            node_manifest_sha256=runtime_preparation.node_manifest_sha256,
            runtime_preparation_sha256=runtime_preparation.preparation_sha256,
            node_runtime_launch_receipt_sha256=(node_runtime_launch_receipt.launch_receipt_sha256),
            runtime_identity_sha256=(
                node_runtime_launch_receipt.launch_evidence.runtime_identity_sha256
            ),
            runtime_inspection_evidence_sha256=termination_evidence.inspection_sha256,
            inspection_sequence=inspection_sequence,
            node_inventory_sha256=self.current.node_inventory_sha256,
            resource_lease_sha256=self.current.resource_lease_sha256,
            fencing_epoch=fencing_epoch,
            lease_token_sha256=self.current.lease_token_sha256,
            hard_deadline=self.current.hard_deadline,
            artifact_submission_deadline=artifact_submission_deadline,
            challenged_at=termination_evidence.inspected_at,
            expires_at=min(
                termination_evidence.inspected_at + timedelta(seconds=10),
                _runtime_control_pin().active_until,
            ),
        )
        self.challenge_history[inspection_sequence] = self.last_challenge
        if self.crash_after_challenge_commit:
            self.crash_after_challenge_commit = False
            raise SystemExit("crash after allocator challenge commit")
        return self.last_challenge

    def accept_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
    ) -> AcceptedRuntimeTermination:
        self._authority(attempt_id, lease_token, fencing_epoch)
        if self.last_accepted is not None:
            assert self.last_challenge == challenge
            assert self.last_node_termination == node_runtime_termination_receipt
            return self.last_accepted
        if not self.clock.now() < challenge.expires_at:
            raise NodeProofReplayRejected(
                NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED,
                "exact termination challenge expired without acceptance",
            )
        if "mark_terminated" in self.reconcile_on:
            self.current = replace(self.current, status="reconciliation_required")
            raise NodeLeaseRejected("central termination acceptance held")
        assert self.last_challenge == challenge
        assert self._runtime_preparation is not None
        assert self.last_launch_receipt is not None
        assert self._start_request is not None
        assert self._start_authorization is not None
        self.calls.append("terminated")
        self.last_node_termination = node_runtime_termination_receipt
        self.last_accepted = issue_accepted_runtime_termination(
            pin=_runtime_control_pin(),
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            challenge=challenge,
            node_termination_receipt=node_runtime_termination_receipt,
            preparation=self._runtime_preparation,
            launch_receipt=self.last_launch_receipt,
            launch_authorization_request=self._start_request,
            launch_authorization=self._start_authorization.launch_authorization,
            node_authority=self.node_authority,
            runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            accepted_at=node_runtime_termination_receipt.signed_at,
            billable_ended_at=node_runtime_termination_receipt.signed_at,
            maximum_proof_age_seconds=10,
        )
        self.current = replace(self.current, status="terminated")
        if self.crash_after_mark_terminated:
            self.crash_after_mark_terminated = False
            raise SystemExit("crash after allocator termination commit")
        return self.last_accepted

    def replay_accepted_runtime_termination(
        self,
        *,
        recovery_grant,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
        expected_accepted_runtime_termination_sha256: str,
    ) -> AcceptedRuntimeTermination:
        self.accepted_replay_calls += 1
        assert self.last_accepted is not None
        assert self.last_challenge == challenge
        assert self.last_node_termination == node_runtime_termination_receipt
        assert recovery_grant.accepted_runtime_termination_sha256 == (
            expected_accepted_runtime_termination_sha256
        )
        assert self.last_accepted.accepted_termination_sha256 == (
            expected_accepted_runtime_termination_sha256
        )
        return self.last_accepted

    def submit_terminal_artifacts(
        self,
        *,
        accepted_termination: AcceptedRuntimeTermination,
        terminal_submission: QualificationTerminalSubmission,
        artifact_manifest,
        artifact_verified_receipts,
        disposition: NodeTerminalDisposition,
    ) -> TerminalArtifactCommit:
        assert accepted_termination == self.last_accepted
        assert terminal_submission.accepted_runtime_termination_sha256 == (
            accepted_termination.accepted_termination_sha256
        )
        assert terminal_submission.artifact_manifest_sha256 == artifact_manifest.manifest_sha256
        assert terminal_submission.disposition == disposition.value
        assert self.last_challenge is not None
        assert self.last_node_termination is not None
        assert self._runtime_preparation is not None
        assert self.last_launch_receipt is not None
        assert self._start_request is not None
        assert self._start_authorization is not None
        verify_qualification_terminal_submission(
            submission=terminal_submission,
            intent=self.assignment.intent,
            accepted=accepted_termination,
            challenge=self.last_challenge,
            node_termination_receipt=self.last_node_termination,
            preparation=self._runtime_preparation,
            launch_receipt=self.last_launch_receipt,
            launch_authorization_request=self._start_request,
            launch_authorization=self._start_authorization.launch_authorization,
            artifact_manifest=artifact_manifest,
            artifact_verified_receipts=tuple(artifact_verified_receipts),
            expected_node_inventory_sha256=self.current.node_inventory_sha256,
            expected_resource_lease_sha256=self.current.resource_lease_sha256,
            node_authority=self.node_authority,
            runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            verified_at=max(self.clock.now(), terminal_submission.submitted_at),
        )
        if self.last_terminal_acceptance is not None:
            assert self.last_submission == terminal_submission
            assert self.last_artifact_verified_receipts == tuple(artifact_verified_receipts)
            self.calls.append("verifying")
            self.current = replace(
                self.current,
                status=(
                    "reconciliation_required"
                    if "mark_verifying" in self.reconcile_on
                    else "verifying"
                ),
            )
            return TerminalArtifactCommit(
                reservation=self.current,
                terminal_acceptance=self.last_terminal_acceptance,
            )
        self.calls.append("verifying")
        if self.crash_before_mark_verifying:
            self.crash_before_mark_verifying = False
            raise SystemExit("crash before allocator verification commit")
        self.last_submission = terminal_submission
        self.last_artifact_verified_receipts = tuple(artifact_verified_receipts)
        self.last_disposition = disposition
        self.last_terminal_acceptance = issue_accepted_qualification_terminal_submission(
            pin=_runtime_control_pin(),
            private_key=RUNTIME_CONTROL_PRIVATE_KEY,
            submission=terminal_submission,
            intent=self.assignment.intent,
            accepted=accepted_termination,
            challenge=self.last_challenge,
            node_termination_receipt=self.last_node_termination,
            preparation=self._runtime_preparation,
            launch_receipt=self.last_launch_receipt,
            launch_authorization_request=self._start_request,
            launch_authorization=self._start_authorization.launch_authorization,
            artifact_manifest=artifact_manifest,
            artifact_verified_receipts=tuple(artifact_verified_receipts),
            expected_node_inventory_sha256=self.current.node_inventory_sha256,
            expected_resource_lease_sha256=self.current.resource_lease_sha256,
            node_authority=self.node_authority,
            runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            accepted_at=max(self.clock.now(), terminal_submission.submitted_at),
        )
        self.current = replace(
            self.current,
            status=(
                "reconciliation_required" if "mark_verifying" in self.reconcile_on else "verifying"
            ),
        )
        if self.crash_after_terminal_acceptance_commit:
            self.crash_after_terminal_acceptance_commit = False
            raise SystemExit("crash after final terminal acceptance commit")
        return TerminalArtifactCommit(
            reservation=self.current,
            terminal_acceptance=self.last_terminal_acceptance,
        )

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
            input_paths=(
                PinnedInputPath(
                    input_port_id=self.intent.input_artifact_bindings[0].input_port_id,
                    relative_path="input.json",
                ),
            ),
            artifact_paths=artifact_paths,
        )
        self.assignment = QualificationAssignment(
            intent=self.intent,
            work_order_node=self.node,
            qualification_grant=self.case.grant,
            reservation=self.reservation,
            lease_token=self.raw_token,
        )
        self.allocator = _Allocator(
            self.assignment,
            node_authority=self.authority,
            clock=self.clock,
        )
        self.runtime = _Runtime(self.clock)
        self.materializer = _InputMaterializer()
        self.output_quota_provisioner = _OutputQuotaProvisioner()
        self.state = NodeLocalStateStore(tmp_path / "node-state")
        self.artifacts = LocalArtifactStore(tmp_path / "artifact-cas")
        self.agent = QualificationNodeAgent(
            node_authority=self.authority,
            qualification_authority=QualificationAuthorityVerifier(self.case.pin),
            runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            node_signing_private_key=PRIVATE_KEY,
            boot_id="boot.qualification-001",
            allocator_principal_id="principal:allocator",
            allocator=self.allocator,
            runtime=self.runtime,
            output_quota_provisioner=self.output_quota_provisioner,
            artifact_quarantine=self.artifacts,
            launch_registry=PinnedLaunchRegistry((self.spec,)),
            state_store=self.state,
            input_materializer=self.materializer,
            clock=self.clock,
            artifact_completion_grace_seconds=4 * 60 * 60,
        )

    def replay_assignment(self, *, status: str | None = None) -> QualificationAssignment:
        reservation = self.allocator.current
        if status is not None:
            reservation = replace(reservation, status=status)
            self.allocator.current = reservation
        return replace(self.assignment, reservation=reservation, lease_token=None)


@pytest.mark.parametrize(
    "relationship", ["equal", "state-under-workspace", "workspace-under-state"]
)
def test_private_state_and_pinned_shared_workspace_cannot_overlap_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relationship: str,
) -> None:
    if relationship == "equal":
        workspace = tmp_path / "shared"
        workspace.mkdir(mode=0o700)
        state_root = workspace
    elif relationship == "state-under-workspace":
        workspace = tmp_path / "shared"
        workspace.mkdir(mode=0o700)
        state_root = workspace / "private-state"
    else:
        state_root = tmp_path / "private-state"
        state_root.mkdir(mode=0o711)
        workspace = state_root / "shared"
        workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    metadata = workspace.lstat()
    pin = PinnedOutputWorkspaceRoot(
        path=str(workspace),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mount_id=7,
        owner_gid=os.getegid(),
        parent_chain_sha256="0" * 64,
    )
    monkeypatch.setattr(
        NodeLocalStateStore,
        "_verify_output_workspace_root",
        classmethod(lambda cls, supplied: None),
    )

    observed_paths = {workspace}
    if state_root.exists():
        observed_paths.add(state_root)
    before = {
        path: (
            path.lstat().st_dev,
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_gid,
            path.lstat().st_mtime_ns,
            tuple(sorted(child.name for child in path.iterdir())),
        )
        for path in observed_paths
    }

    with pytest.raises(LocalStateError, match="custody roots overlap"):
        NodeLocalStateStore(state_root, output_workspace_root_pin=pin)

    assert not (workspace / "absences").exists()
    assert {
        path: (
            path.lstat().st_dev,
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_gid,
            path.lstat().st_mtime_ns,
            tuple(sorted(child.name for child in path.iterdir())),
        )
        for path in observed_paths
    } == before


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
    assert running.runtime_preparation is not None
    assert running.node_runtime_launch_receipt is harness.allocator.last_launch_receipt
    assert running.node_runtime_launch_receipt is not None
    assert (
        running.node_runtime_launch_receipt.launch_evidence.preparation_sha256
        == running.runtime_preparation.preparation_sha256
    )
    assert running.runtime_identity == (
        running.node_runtime_launch_receipt.launch_evidence.runtime_identity
    )
    assert harness.runtime.launch_calls == 1
    assert harness.materializer.calls == 1

    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())

    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert collected.artifact_manifest is not None
    assert collected.node_runtime_termination_receipt is not None
    assert collected.accepted_runtime_termination is not None
    assert collected.accepted_terminal_submission is not None
    assert collected.terminal_submission is not None
    assert collected.node_runtime_termination_receipt.termination_evidence.exit_code == 0
    assert collected.accepted_runtime_termination.compute_release_allowed is True
    assert collected.accepted_runtime_termination.scientific_admission_allowed is False
    assert collected.terminal_disposition is NodeTerminalDisposition.PROCESS_SUCCEEDED
    assert collected.terminal_submission.output_tree_sha256 == (
        artifact_output_tree_sha256(collected.artifact_manifest)
    )
    assert collected.terminal_submission.signature_ed25519_hex != "0" * 128
    assert collected.terminal_submission.artifact_verified_receipt_sha256s == tuple(
        sorted(item.verified_receipt_sha256 for item in collected.artifact_verified_receipts)
    )
    assert (
        collected.accepted_terminal_submission.terminal_submission_sha256
        == collected.terminal_submission.terminal_submission_sha256
    )
    assert harness.runtime.launch_calls == 1
    assert harness.allocator.calls[-2:] == ["terminated", "verifying"]

    raw = harness.raw_token.encode()
    assert raw not in canonical_json_bytes(collected.accepted_runtime_termination)
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
    assert replayed.accepted_runtime_termination == collected.accepted_runtime_termination
    assert replayed.terminal_submission == collected.terminal_submission
    assert harness.runtime.launch_calls == 1


@pytest.mark.parametrize(
    ("crash_phase", "mounts_before_restart"),
    [
        ("attempt-root-chowned-before-mode", 0),
        ("attempt-root-moded-before-fsync", 0),
        ("quota-mount-command-returned", 1),
    ],
)
def test_pinned_workspace_quota_service_recovers_composed_node_cold_start_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
    mounts_before_restart: int,
) -> None:
    """Recover the same privileged generation through real Node workspace ordering.

    The virtual UID shim is limited to the two root-owned directory identities because this
    test runs without privilege.  Directory descriptors, modes, inodes, durable service records,
    the Node store, input materialization, and the service's mount/chown code are all real.
    """

    if os.getegid() == 0:
        pytest.skip("the unprivileged root-custody phase test needs a non-root dedicated group")
    case = _qualification_case(artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1)
    harness = _Harness(tmp_path / "harness", case=case)
    workspace_root = tmp_path / "output-workspaces"
    workspace_root.mkdir(mode=0o1730)
    workspace_root.chmod(0o1730)
    state_root = tmp_path / "quota-state"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    backing_root = tmp_path / "quota-backing"
    backing_root.mkdir(mode=0o700)
    backing_root.chmod(0o700)
    workspace_metadata = workspace_root.lstat()
    workspace_pin = PinnedOutputWorkspaceRoot(
        path=str(workspace_root),
        device=workspace_metadata.st_dev,
        inode=workspace_metadata.st_ino,
        mount_id=9,
        owner_gid=os.getegid(),
        parent_chain_sha256="0" * 64,
    )
    preinstalled_workspace_pin = PreinstalledOutputWorkspaceRootPin(
        path=workspace_pin.path,
        device=workspace_pin.device,
        inode=workspace_pin.inode,
        owner_gid=workspace_pin.owner_gid,
        parent_chain_sha256=workspace_pin.parent_chain_sha256,
    )
    monkeypatch.setattr(
        NodeLocalStateStore,
        "_verify_output_workspace_root",
        classmethod(lambda cls, pin: None),
    )

    attempt_id = harness.reservation.attempt_id
    attempt_key = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    attempt_root = workspace_root / attempt_key
    output_root = attempt_root / "output"
    virtual_root_owned = False
    mounted = False
    mount_calls = 0
    original_fstat = os.fstat
    original_lstat = os.lstat
    original_stat = os.stat
    original_fchown = os.fchown

    def _with_owner(metadata: os.stat_result, *, uid: int, gid: int) -> os.stat_result:
        fields = list(metadata)
        fields[4] = uid
        fields[5] = gid
        return os.stat_result(fields)

    def _is_attempt(metadata: os.stat_result) -> bool:
        try:
            actual = original_lstat(attempt_root)
        except FileNotFoundError:
            return False
        return metadata.st_dev == actual.st_dev and metadata.st_ino == actual.st_ino

    def _virtual_fstat(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        if (
            metadata.st_dev == workspace_metadata.st_dev
            and metadata.st_ino == workspace_metadata.st_ino
        ):
            return _with_owner(metadata, uid=0, gid=os.getegid())
        if virtual_root_owned and _is_attempt(metadata):
            return _with_owner(metadata, uid=0, gid=os.getegid())
        return metadata

    def _virtual_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        metadata = original_lstat(path, *args, **kwargs)
        if virtual_root_owned and not kwargs.get("dir_fd") and Path(path) == attempt_root:
            return _with_owner(metadata, uid=0, gid=os.getegid())
        return metadata

    def _virtual_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        metadata = original_stat(path, *args, **kwargs)
        if virtual_root_owned and not kwargs.get("dir_fd") and Path(path) == attempt_root:
            return _with_owner(metadata, uid=0, gid=os.getegid())
        return metadata

    def _virtual_fchown(descriptor: int, uid: int, gid: int) -> None:
        nonlocal virtual_root_owned
        metadata = original_fstat(descriptor)
        if _is_attempt(metadata):
            assert uid == 0
            assert gid == os.getegid()
            virtual_root_owned = True
            return
        original_fchown(descriptor, uid, gid)

    monkeypatch.setattr(os, "fstat", _virtual_fstat)
    monkeypatch.setattr(os, "lstat", _virtual_lstat)
    monkeypatch.setattr(os, "stat", _virtual_stat)
    monkeypatch.setattr(os, "fchown", _virtual_fchown)
    monkeypatch.setattr(oci_deployment_module, "host_parent_chain_sha256", lambda path: "0" * 64)

    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        deployment_sha256="d" * 64,
        state_root=str(state_root),
        backing_root=str(backing_root),
        workspace_root=str(workspace_root),
        workspace_root_pin=preinstalled_workspace_pin,
        allowed_client_uid=os.geteuid(),
        allowed_client_gid=os.getegid(),
        filesystem_type="ext4",
        mount=object(),
        provisioner_policy_sha256="e" * 64,
        provisioner_principal_id="principal:quota-composed-test",
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_current_boot_id", lambda: "service-boot")

    def _find_mount(path: Path):  # type: ignore[no-untyped-def]
        if path == workspace_root:
            return {"mount_id": 9}
        if path != output_root or not mounted:
            return None
        return {
            "mount_id": 41,
            "mount_parent_id": 32,
            "major": 7,
            "minor": 7,
            "mountpoint": str(output_root),
            "mount_options": frozenset({"rw", "nosuid", "nodev", "noexec", "noatime"}),
            "fstype": "ext4",
            "source": "/dev/loop7",
            "super_options": frozenset({"rw"}),
        }

    monkeypatch.setattr(service, "_find_mount", _find_mount)
    monkeypatch.setattr(
        oci_deployment_module,
        "_observe_live_output_workspace_root",
        lambda expected: workspace_pin,
    )

    def _ensure_loop_attachment(
        intent, *, backing_identity: str, generation_root: Path
    ) -> _QuotaLoopAttachment:  # type: ignore[no-untyped-def]
        path = generation_root / "loop-attached.json"
        if path.exists():
            loaded = service._load_root_model(path, _QuotaLoopAttachment)  # noqa: SLF001
            assert isinstance(loaded, _QuotaLoopAttachment)
            assert loaded.intent_record_sha256 == intent.intent_record_sha256
            assert loaded.backing_file_identity_sha256 == backing_identity
            return loaded
        record = _QuotaLoopAttachment(
            deployment_sha256=service._deployment.deployment_sha256,  # noqa: SLF001
            intent_record_sha256=intent.intent_record_sha256,
            loop_device="/dev/loop7",
            backing_file_identity_sha256=backing_identity,
            attached_at=harness.clock.now(),
        )
        service._publish_root_model(path, record)  # noqa: SLF001
        return record

    def _ensure_formatted(
        intent, *, attachment: _QuotaLoopAttachment, generation_root: Path
    ) -> _QuotaFilesystemFormatted:  # type: ignore[no-untyped-def]
        path = generation_root / "filesystem-formatted.json"
        if path.exists():
            loaded = service._load_root_model(path, _QuotaFilesystemFormatted)  # noqa: SLF001
            assert isinstance(loaded, _QuotaFilesystemFormatted)
            assert loaded.attachment_record_sha256 == attachment.attachment_record_sha256
            return loaded
        record = _QuotaFilesystemFormatted(
            deployment_sha256=service._deployment.deployment_sha256,  # noqa: SLF001
            attachment_record_sha256=attachment.attachment_record_sha256,
            filesystem_uuid=intent.filesystem_uuid,
            filesystem_uuid_sha256=hashlib.sha256(
                intent.filesystem_uuid.encode("ascii")
            ).hexdigest(),
            formatted_at=harness.clock.now(),
        )
        service._publish_root_model(path, record)  # noqa: SLF001
        return record

    monkeypatch.setattr(service, "_ensure_loop_attachment", _ensure_loop_attachment)
    monkeypatch.setattr(service, "_ensure_formatted", _ensure_formatted)
    monkeypatch.setattr(service, "_verify_live_receipt", lambda receipt: None)

    def _mount_command(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mounted, mount_calls
        assert not mounted
        mounted = True
        mount_calls += 1
        output_root.chmod(0o755)
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(service, "_run_pinned", _mount_command)

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            del tz
            return harness.clock.now()

    monkeypatch.setattr(oci_deployment_module, "datetime", _FixedDateTime)

    class _ServiceClient:
        output_workspace_root_pin = workspace_pin
        minimum_output_quota_bytes = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES

        def __init__(self) -> None:
            self.calls = 0

        def ensure_output_quota(self, **request):  # type: ignore[no-untyped-def]
            self.calls += 1
            expected = request.pop("expected_receipt")
            return service.ensure(
                {
                    **request,
                    "output_root": str(request["output_root"]),
                    "expected_receipt": (
                        expected.model_dump(mode="json") if expected is not None else "none"
                    ),
                }
            )

    client = _ServiceClient()
    private_state_root = tmp_path / "node-private-state"

    def _new_store() -> NodeLocalStateStore:
        return NodeLocalStateStore(
            private_state_root,
            output_workspace_root_pin=workspace_pin,
        )

    def _new_agent(state: NodeLocalStateStore) -> QualificationNodeAgent:
        return QualificationNodeAgent(
            node_authority=harness.authority,
            qualification_authority=QualificationAuthorityVerifier(harness.case.pin),
            runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            node_signing_private_key=PRIVATE_KEY,
            boot_id="boot.qualification-001",
            allocator_principal_id="principal:allocator",
            allocator=harness.allocator,
            runtime=harness.runtime,
            output_quota_provisioner=client,
            artifact_quarantine=harness.artifacts,
            launch_registry=PinnedLaunchRegistry((harness.spec,)),
            state_store=state,
            input_materializer=harness.materializer,
            clock=harness.clock,
            artifact_completion_grace_seconds=4 * 60 * 60,
        )

    class _PowerLoss(BaseException):
        pass

    crashed = False

    def _crash_once(phase: str, path: Path) -> None:
        nonlocal crashed
        if phase == crash_phase and not crashed:
            crashed = True
            raise _PowerLoss(f"power loss at {phase}")

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash_once)
    first_store = _new_store()
    with pytest.raises(_PowerLoss, match=crash_phase):
        _new_agent(first_store).run_once()
    assert crashed is True
    assert virtual_root_owned is True
    assert stat.S_IMODE(original_lstat(attempt_root).st_mode) == (
        0o700 if crash_phase == "attempt-root-chowned-before-mode" else 0o710
    )
    assert mount_calls == mounts_before_restart

    recovered_store = _new_store()
    running = _new_agent(recovered_store).run_once()
    assert running.outcome is NodeRunOutcome.RUNNING
    assert mount_calls == 1
    assert stat.S_IMODE(original_lstat(attempt_root / "input").st_mode) == 0o500
    receipt_path = state_root / attempt_key / "receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    quota_receipt = recovered_store.load_output_quota_provisioning(attempt_id=attempt_id)
    assert quota_receipt is not None
    assert receipt_bytes == canonical_json_bytes(quota_receipt)
    assert quota_receipt.output_quota_bytes == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1
    assert quota_receipt.block_device_capacity_bytes == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    intent_record = service._load_root_model(  # noqa: SLF001
        state_root / attempt_key / "intent.json",
        oci_deployment_module._QuotaProvisioningIntent,  # noqa: SLF001
    )
    assert intent_record.output_quota_bytes == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1
    assert intent_record.output_quota_bytes == quota_receipt.output_quota_bytes
    assert intent_record.block_device_capacity_bytes == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    assert harness.runtime.request is not None
    assert harness.runtime.request.output_quota_bytes == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES + 1
    assert (
        harness.runtime.request.output_quota_provisioning_receipt.block_device_capacity_bytes
        == MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    )

    # A second cold Node restart must accept the sealed 0500 input phase, reopen the same
    # privileged receipt, and recover the already-running runtime without remounting.
    final_store = _new_store()
    replay = _new_agent(final_store).run_assignment(harness.replay_assignment(status="running"))
    assert replay.outcome is NodeRunOutcome.RUNNING
    assert mount_calls == 1
    assert receipt_path.read_bytes() == receipt_bytes


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


def test_post_start_pre_receipt_crash_recovers_after_ticket_expiry_from_actual_start(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.crash_after_launch = True
    with pytest.raises(SystemExit, match="after idempotent"):
        harness.agent.run_once()
    identity = harness.runtime.identity
    assert identity is not None
    harness.clock.current += timedelta(seconds=6)
    harness.clock.monotonic += 6_000_000_000

    recovered = harness.agent.run_assignment(harness.replay_assignment())

    assert recovered.outcome is NodeRunOutcome.RUNNING
    assert recovered.runtime_identity == identity
    assert recovered.node_runtime_launch_receipt is not None
    assert recovered.node_runtime_launch_receipt.launch_evidence.observed_at > identity.started_at
    assert harness.runtime.launch_calls == 1


def test_created_never_started_cleanup_advances_epoch_before_replacement_launch(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.crash_after_create = True
    with pytest.raises(SystemExit, match="after engine create"):
        harness.agent.run_once()
    harness.clock.current += timedelta(seconds=6)
    harness.clock.monotonic += 6_000_000_000
    harness.allocator.crash_after_absence_commit = True

    with pytest.raises(SystemExit, match="allocator absence commit"):
        harness.agent.run_assignment(harness.replay_assignment())
    cleaned = harness.agent.run_assignment(harness.replay_assignment())

    assert cleaned.outcome is NodeRunOutcome.PRE_RUNTIME_REAUTHORIZED
    receipt = cleaned.pre_runtime_absence_receipt
    assert receipt is not None
    assert receipt.absence_evidence.prelaunch_absence_epoch == 1
    assert receipt.absence_evidence.prelaunch_authorization_request_sha256 is not None
    assert receipt.absence_evidence.prelaunch_authorization_sha256 is not None
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None and state.runtime_launch_authorization_request is not None
    assert state.runtime_launch_authorization_request.pre_runtime_absence_epoch == 1
    assert (
        state.runtime_launch_authorization_request.pre_runtime_absence_receipt_sha256
        == receipt.absence_receipt_sha256
    )
    assert harness.runtime.cleanup_calls == 2
    assert harness.runtime.cleanup_mutations == 1

    running = harness.agent.run_assignment(harness.replay_assignment())
    assert running.outcome is NodeRunOutcome.RUNNING
    assert harness.runtime.launch_calls == 1


def test_quick_exit_before_exact_identity_remains_unknown_and_cannot_become_absence(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.quick_exit_before_identity = True
    with pytest.raises(SystemExit, match="before exact launch identity"):
        harness.agent.run_once()
    harness.clock.current += timedelta(seconds=6)
    harness.clock.monotonic += 6_000_000_000

    retained = harness.agent.run_assignment(harness.replay_assignment())

    assert retained.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert retained.pre_runtime_absence_receipt is None
    assert retained.runtime_identity is None
    assert harness.runtime.cleanup_calls == 1
    assert "absence" not in harness.allocator.calls


def test_delayed_launch_ticket_return_fails_before_runtime_launch_call(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.start_return_delay_ns = 5_000_000_001

    with pytest.raises(RuntimeRejected, match="authorization is invalid or stale"):
        harness.agent.run_once()

    assert harness.runtime.launch_calls == 0
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.phase.value == "start_requested"


def test_crash_after_db_start_commit_replays_durable_exact_nonce_and_ticket(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.crash_after_start_commit = True

    with pytest.raises(SystemExit, match="allocator start commit"):
        harness.agent.run_once()

    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.phase.value == "start_requested"
    request = state.runtime_launch_authorization_request
    assert request is not None
    assert harness.runtime.launch_calls == 0

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.RUNNING
    recovered_state = harness.state.load_state(harness.reservation.attempt_id)
    assert recovered_state is not None
    assert recovered_state.runtime_launch_authorization_request == request
    assert recovered_state.runtime_launch_authorization == (
        harness.allocator._start_authorization.launch_authorization
    )
    assert harness.runtime.launch_calls == 1


@pytest.mark.parametrize(
    ("acceptance_requires_adoption", "expected_outcome"),
    (
        (False, NodeRunOutcome.RUNNING),
        (True, NodeRunOutcome.ADOPTED),
    ),
)
def test_historical_pre_runtime_delivery_resubmits_exact_local_launch_receipt(
    monkeypatch,
    tmp_path: Path,
    acceptance_requires_adoption: bool,
    expected_outcome: NodeRunOutcome,
) -> None:
    harness = _Harness(tmp_path)
    original_mark_running = harness.allocator.mark_running

    def crash_before_allocator_acceptance(**_scope: object) -> NodeReservation:
        raise SystemExit("crash before allocator launch acceptance")

    monkeypatch.setattr(
        harness.allocator,
        "mark_running",
        crash_before_allocator_acceptance,
    )
    with pytest.raises(SystemExit, match="allocator launch acceptance"):
        harness.agent.run_once()

    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.phase is AttemptPhase.LAUNCH_COMMITTED
    assert state.launch_committed is True
    assert state.running_confirmed is False
    assert state.runtime_identity is not None
    assert state.node_runtime_launch_receipt is not None
    assert harness.allocator.current.status == "starting"
    assert harness.runtime.launch_calls == 1

    monkeypatch.setattr(harness.allocator, "mark_running", original_mark_running)
    if acceptance_requires_adoption:
        harness.allocator.reconcile_on.add("mark_running")
    lineage = HistoricalPreRuntimeRecoveryLineage(
        runtime_preparation=state.runtime_preparation,
        runtime_launch_authorization_request=state.runtime_launch_authorization_request,
        runtime_launch_authorization=state.runtime_launch_authorization,
    )
    recovery_assignment = replace(
        harness.assignment,
        reservation=harness.allocator.current,
        lease_token=None,
        historical_pre_runtime_recovery_lineage=lineage,
    )

    recovered = harness.agent.run_assignment(recovery_assignment)

    assert recovered.outcome is expected_outcome
    assert harness.runtime.launch_calls == 1
    assert harness.allocator.last_launch_receipt == state.node_runtime_launch_receipt
    assert harness.allocator.calls.count("running") == 1
    assert harness.allocator.calls.count("adopt") == int(acceptance_requires_adoption)
    persisted = harness.state.load_state(harness.reservation.attempt_id)
    assert persisted is not None
    assert persisted.running_confirmed is True
    assert persisted.phase is AttemptPhase.RUNNING


def test_historical_pre_runtime_recovery_after_hard_deadline_only_cleans_and_releases(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.crash_after_start_commit = True

    with pytest.raises(SystemExit, match="allocator start commit"):
        harness.agent.run_once()

    state = harness.state.load_state(harness.reservation.attempt_id)
    start = harness.allocator._start_authorization
    assert state is not None and state.phase.value == "start_requested"
    assert state.runtime_launch_authorization_request is not None
    assert start is not None
    assert harness.runtime.launch_calls == 0

    harness.clock.current = harness.reservation.hard_deadline + timedelta(minutes=1)
    harness.clock.monotonic += 16 * 60 * 1_000_000_000
    recovered_reservation = replace(
        harness.allocator.current,
        status="reconciliation_required",
    )
    harness.allocator.current = recovered_reservation
    lineage = HistoricalPreRuntimeRecoveryLineage(
        runtime_preparation=state.runtime_preparation,
        runtime_launch_authorization_request=(state.runtime_launch_authorization_request),
        runtime_launch_authorization=start.launch_authorization,
    )
    recovery_assignment = replace(
        harness.assignment,
        reservation=recovered_reservation,
        lease_token=None,
        historical_pre_runtime_recovery_lineage=lineage,
    )

    recovered = harness.agent.run_assignment(recovery_assignment)

    assert recovered.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert harness.runtime.launch_calls == 0
    assert harness.runtime.cleanup_calls == 1
    assert harness.allocator.calls.count("start") == 1
    assert harness.allocator.calls.count("absence") == 1
    persisted = harness.state.load_state(harness.reservation.attempt_id)
    assert persisted is not None
    assert persisted.runtime_launch_authorization == start.launch_authorization
    assert persisted.phase.value == "pre_runtime_released"


def test_historical_pre_runtime_recovery_resumes_cleanup_from_reconciliation_state(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.crash_after_start_commit = True

    with pytest.raises(SystemExit, match="allocator start commit"):
        harness.agent.run_once()

    state = harness.state.load_state(harness.reservation.attempt_id)
    start = harness.allocator._start_authorization
    assert state is not None and start is not None
    assert state.phase is AttemptPhase.START_REQUESTED
    # A failed never-started cleanup can durably retain either side of the local launch
    # commit.  This reproduces the pre-commit side while preserving the exact DB ticket.
    state = replace(
        state,
        phase=AttemptPhase.RECONCILIATION_REQUIRED,
        runtime_launch_authorization=start.launch_authorization,
    )
    harness.state.save_state(state)
    harness.clock.current = harness.reservation.hard_deadline + timedelta(minutes=1)
    harness.clock.monotonic += 16 * 60 * 1_000_000_000
    recovered_reservation = replace(
        harness.allocator.current,
        status="reconciliation_required",
    )
    harness.allocator.current = recovered_reservation
    lineage = HistoricalPreRuntimeRecoveryLineage(
        runtime_preparation=state.runtime_preparation,
        runtime_launch_authorization_request=state.runtime_launch_authorization_request,
        runtime_launch_authorization=start.launch_authorization,
    )

    recovered = harness.agent.run_assignment(
        replace(
            harness.assignment,
            reservation=recovered_reservation,
            lease_token=None,
            historical_pre_runtime_recovery_lineage=lineage,
        )
    )

    assert recovered.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert harness.runtime.launch_calls == 0
    assert harness.runtime.cleanup_calls == 1
    assert harness.allocator.calls.count("absence") == 1


def test_historical_pre_runtime_cleanup_covers_committed_local_launch_gap(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.crash_before_runtime_call = True

    with pytest.raises(SystemExit, match="committed launch gap"):
        harness.agent.run_once()

    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None and state.phase.value == "launch_committed"
    assert state.runtime_launch_authorization_request is not None
    assert state.runtime_launch_authorization is not None
    assert state.runtime_identity is None
    assert harness.runtime.launch_calls == 0

    # The first cleanup attempt may fail after the local launch commit and persist a
    # reconciliation hold.  Historical recovery must remain cleanup-only from that state.
    state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
    harness.state.save_state(state)

    harness.clock.current = harness.reservation.hard_deadline + timedelta(minutes=1)
    harness.clock.monotonic += 16 * 60 * 1_000_000_000
    recovered_reservation = replace(
        harness.allocator.current,
        status="reconciliation_required",
    )
    harness.allocator.current = recovered_reservation
    lineage = HistoricalPreRuntimeRecoveryLineage(
        runtime_preparation=state.runtime_preparation,
        runtime_launch_authorization_request=(state.runtime_launch_authorization_request),
        runtime_launch_authorization=state.runtime_launch_authorization,
    )

    recovered = harness.agent.run_assignment(
        replace(
            harness.assignment,
            reservation=recovered_reservation,
            lease_token=None,
            historical_pre_runtime_recovery_lineage=lineage,
        )
    )

    assert recovered.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert harness.runtime.launch_calls == 0
    assert harness.runtime.cleanup_calls == 1
    assert harness.allocator.calls.count("absence") == 1


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
    prepared = prepare_crash.runtime.preparation
    assert prepared is not None
    assert prepare_crash.runtime.identity is None
    assert prepare_crash.state.load_state(prepare_crash.reservation.attempt_id) is None
    recovered_prepare = prepare_crash.agent.run_assignment(prepare_crash.replay_assignment())
    assert recovered_prepare.outcome is NodeRunOutcome.RUNNING
    assert recovered_prepare.runtime_preparation == prepared
    assert recovered_prepare.runtime_identity is not None
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
    assert central_hold.accepted_runtime_termination is not None
    assert central_hold.terminal_submission is not None
    assert central_hold.artifact_manifest is not None
    assert verifying.allocator.current.status == "reconciliation_required"
    verifying.allocator.reconcile_on.clear()
    closed = verifying.agent.run_assignment(verifying.replay_assignment())
    assert closed.outcome is NodeRunOutcome.COLLECTED
    assert closed.accepted_runtime_termination == central_hold.accepted_runtime_termination
    assert closed.terminal_submission == central_hold.terminal_submission
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


def test_fresh_launch_gap_replays_idempotent_start_without_forging_absent_exit(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.crash_before_runtime_call = True
    with pytest.raises(SystemExit, match="committed launch gap"):
        harness.agent.run_once()
    assert harness.runtime.launch_calls == 0

    running = harness.agent.run_assignment(harness.replay_assignment())
    assert running.outcome is NodeRunOutcome.RUNNING
    assert running.node_runtime_launch_receipt is not None
    assert running.node_execution_receipt is None
    assert running.pre_runtime_absence_receipt is None
    assert harness.runtime.launch_calls == 1
    assert harness.allocator.retained == 0


def test_prelaunch_absence_is_never_projected_as_process_exit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.allocator.start_crash = True
    with pytest.raises(SystemExit, match="before launch"):
        harness.agent.run_once()

    recovered = harness.agent.run_assignment(
        harness.replay_assignment(status="reconciliation_required")
    )
    assert recovered.outcome is NodeRunOutcome.RUNNING
    assert recovered.pre_runtime_absence_receipt is None
    assert recovered.runtime_identity is not None
    assert recovered.node_execution_receipt is None
    # START_REQUESTED already has a durable nonce; recovery replays it instead of minting a
    # replacement absence ticket.
    assert harness.allocator.calls.count("start") == 2
    assert "terminated" not in harness.allocator.calls


def test_pre_runtime_absence_db_commit_crash_replays_exact_proof_and_replacement_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    original_save = harness.state.save_state
    crash_once = True

    def crash_after_prepared(state) -> None:
        nonlocal crash_once
        original_save(state)
        if crash_once and state.phase.value == "prepared":
            crash_once = False
            raise SystemExit("crash after durable inert preparation")

    monkeypatch.setattr(harness.state, "save_state", crash_after_prepared)
    with pytest.raises(SystemExit, match="inert preparation"):
        harness.agent.run_once()
    monkeypatch.setattr(harness.state, "save_state", original_save)

    harness.allocator.crash_after_absence_commit = True
    with pytest.raises(SystemExit, match="allocator absence commit"):
        harness.agent.run_assignment(harness.replay_assignment(status="reconciliation_required"))
    pending = harness.state.load_pre_runtime_absence_request(
        attempt_id=harness.reservation.attempt_id,
        absence_epoch=1,
    )
    assert pending is not None
    receipt, request = pending
    assert request is not None
    assert request.pre_runtime_absence_receipt_sha256 == receipt.absence_receipt_sha256
    committed = harness.allocator.last_absence_decision
    assert committed is not None
    assert committed.disposition is PreRuntimeAbsenceDisposition.REAUTHORIZED

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.PRE_RUNTIME_REAUTHORIZED
    assert recovered.pre_runtime_absence_receipt == receipt
    assert harness.allocator.calls.count("absence") == 2
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.runtime_launch_authorization_request == request
    assert state.runtime_launch_authorization == (committed.replacement_launch_authorization)
    running = harness.agent.run_assignment(harness.replay_assignment())
    assert running.outcome is NodeRunOutcome.RUNNING
    assert harness.runtime.launch_calls == 1
    assert "terminated" not in harness.allocator.calls


def test_expired_uncommitted_absence_crash_refreshes_same_tombstone_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    original_save_state = harness.state.save_state
    crash_after_prepared = True

    def save_prepared_then_crash(state) -> None:
        nonlocal crash_after_prepared
        original_save_state(state)
        if crash_after_prepared and state.phase.value == "prepared":
            crash_after_prepared = False
            raise SystemExit("crash after inert preparation")

    monkeypatch.setattr(harness.state, "save_state", save_prepared_then_crash)
    with pytest.raises(SystemExit, match="inert preparation"):
        harness.agent.run_once()
    monkeypatch.setattr(harness.state, "save_state", original_save_state)

    original_save_absence = harness.state.save_pre_runtime_absence_request
    crash_after_local_absence = True

    def save_absence_then_crash(**kwargs) -> None:
        nonlocal crash_after_local_absence
        original_save_absence(**kwargs)
        if crash_after_local_absence:
            crash_after_local_absence = False
            raise SystemExit("crash after local absence proof")

    monkeypatch.setattr(
        harness.state,
        "save_pre_runtime_absence_request",
        save_absence_then_crash,
    )
    with pytest.raises(SystemExit, match="local absence proof"):
        harness.agent.run_assignment(harness.replay_assignment(status="reconciliation_required"))
    monkeypatch.setattr(
        harness.state,
        "save_pre_runtime_absence_request",
        original_save_absence,
    )
    old_generation = harness.state.load_latest_pre_runtime_absence_generation(
        attempt_id=harness.reservation.attempt_id,
        absence_epoch=1,
    )
    assert old_generation is not None and old_generation.generation == 1
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    recovered = harness.agent.run_assignment(
        harness.replay_assignment(status="reconciliation_required")
    )
    assert recovered.outcome is NodeRunOutcome.PRE_RUNTIME_REAUTHORIZED
    refreshed = harness.state.load_latest_pre_runtime_absence_generation(
        attempt_id=harness.reservation.attempt_id,
        absence_epoch=1,
    )
    assert refreshed is not None and refreshed.generation == 2
    assert refreshed.supersedes_absence_receipt_sha256 == (
        old_generation.receipt.absence_receipt_sha256
    )
    assert refreshed.receipt.absence_evidence.prelaunch_absence_epoch == 1
    assert refreshed.receipt.absence_evidence.prelaunch_absence_journal_sha256 == (
        old_generation.receipt.absence_evidence.prelaunch_absence_journal_sha256
    )
    assert refreshed.receipt.absence_evidence.inspected_at > (
        old_generation.receipt.absence_evidence.inspected_at
    )
    assert refreshed.replacement_request is not None
    assert refreshed.replacement_request.pre_runtime_absence_receipt_sha256 == (
        refreshed.receipt.absence_receipt_sha256
    )
    assert harness.allocator.calls.count("absence") == 2
    assert len(tuple((harness.state.root / "absences").iterdir())) == 2


def test_expired_pre_runtime_absence_atomically_releases_without_retry_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    original_save = harness.state.save_state
    crash_once = True

    def crash_after_prepared(state) -> None:
        nonlocal crash_once
        original_save(state)
        if crash_once and state.phase.value == "prepared":
            crash_once = False
            raise SystemExit("crash after durable inert preparation")

    monkeypatch.setattr(harness.state, "save_state", crash_after_prepared)
    with pytest.raises(SystemExit, match="inert preparation"):
        harness.agent.run_once()
    monkeypatch.setattr(harness.state, "save_state", original_save)
    harness.clock.current += timedelta(minutes=16)
    harness.clock.monotonic += 16 * 60 * 1_000_000_000

    released = harness.agent.run_assignment(
        harness.replay_assignment(status="reconciliation_required")
    )
    assert released.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED
    assert released.pre_runtime_absence_receipt is not None
    pending = harness.state.load_pre_runtime_absence_request(
        attempt_id=harness.reservation.attempt_id,
        absence_epoch=1,
    )
    assert pending is not None and pending[1] is None
    assert harness.allocator.current.status == "cancelled"
    assert harness.allocator.last_absence_decision is not None
    assert (
        harness.allocator.last_absence_decision.disposition is PreRuntimeAbsenceDisposition.RELEASED
    )
    assert harness.runtime.launch_calls == 0
    assert "start" not in harness.allocator.calls
    assert "terminated" not in harness.allocator.calls


def test_historical_recovery_grant_never_authorizes_prepare_or_launch(
    tmp_path: Path,
) -> None:
    active = _Harness(tmp_path / "active-grant")
    assert active.agent.run_once().outcome is NodeRunOutcome.RUNNING
    active_state = active.state.load_state(active.reservation.attempt_id)
    assert active_state is not None
    assert active_state.node_runtime_launch_receipt is not None
    active_reservation = replace(active.allocator.current, status="reconciliation_required")
    active.allocator.current = active_reservation
    active_recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=active_reservation.admission_sha256,
        qualification_grant_sha256=active.case.grant.grant_sha256,
        intent_sha256=active.intent.intent_sha256,
        execution_id=active.intent.execution_id,
        infrastructure_attempt_id=active_reservation.attempt_id,
        runtime_preparation_sha256=active_state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            active_state.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        admitted_at=NOW,
        hard_deadline=active_reservation.hard_deadline,
        issued_at=active.clock.now(),
        recovery_expires_at=NOW + timedelta(minutes=30),
    )
    recovery_assignment = replace(
        active.assignment,
        reservation=active_reservation,
        lease_token=None,
        historical_recovery_grant=active_recovery,
    )
    assert active.agent._validate_assignment(recovery_assignment)[-1] is False
    tampered_signature = ("0" if active_recovery.signature_ed25519_hex[0] != "0" else "1") + (
        active_recovery.signature_ed25519_hex[1:]
    )
    with pytest.raises(AssignmentRejected, match="previously valid qualification"):
        active.agent.run_assignment(
            replace(
                recovery_assignment,
                historical_recovery_grant=active_recovery.model_copy(
                    update={"signature_ed25519_hex": tampered_signature}
                ),
            )
        )
    assert active.runtime.launch_calls == 1

    missing = _Harness(tmp_path / "missing")
    missing.clock.current += timedelta(minutes=21)
    missing.clock.monotonic += 21 * 60 * 1_000_000_000
    missing_reservation = replace(missing.reservation, status="reconciliation_required")
    missing.allocator.current = missing_reservation
    missing_recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=missing_reservation.admission_sha256,
        qualification_grant_sha256=missing.case.grant.grant_sha256,
        intent_sha256=missing.intent.intent_sha256,
        execution_id=missing.intent.execution_id,
        infrastructure_attempt_id=missing_reservation.attempt_id,
        runtime_preparation_sha256=_digest("missing-preparation"),
        node_runtime_launch_receipt_sha256=_digest("missing-launch"),
        admitted_at=NOW,
        hard_deadline=missing_reservation.hard_deadline,
        issued_at=missing.clock.now(),
        recovery_expires_at=NOW + timedelta(minutes=30),
    )
    missing_workspace = (
        missing.state.workspace_root
        / hashlib.sha256(missing_reservation.attempt_id.encode("utf-8")).hexdigest()
    )
    token_files_before = tuple(sorted((missing.state.root / "tokens").iterdir()))
    preparation_files_before = tuple(sorted((missing.state.root / "preparations").iterdir()))
    quota_calls_before = missing.output_quota_provisioner.calls
    materialization_calls_before = missing.materializer.calls
    assert not missing_workspace.exists()
    with pytest.raises(AssignmentRejected, match="cannot prepare or launch"):
        missing.agent.run_assignment(
            replace(
                missing.assignment,
                reservation=missing_reservation,
                historical_recovery_grant=missing_recovery,
            )
        )
    assert not missing_workspace.exists()
    assert tuple(sorted((missing.state.root / "tokens").iterdir())) == token_files_before
    assert (
        tuple(sorted((missing.state.root / "preparations").iterdir())) == preparation_files_before
    )
    assert missing.output_quota_provisioner.calls == quota_calls_before
    assert missing.materializer.calls == materialization_calls_before
    assert missing.runtime.prepare_calls == 0
    assert missing.runtime.launch_calls == 0

    launched = _Harness(tmp_path / "launched")
    assert launched.agent.run_once().outcome is NodeRunOutcome.RUNNING
    state = launched.state.load_state(launched.reservation.attempt_id)
    assert state is not None
    assert state.node_runtime_launch_receipt is not None
    launched.clock.current += timedelta(minutes=21)
    launched.clock.monotonic += 21 * 60 * 1_000_000_000
    launched_reservation = replace(launched.allocator.current, status="reconciliation_required")
    launched.allocator.current = launched_reservation
    recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=launched_reservation.admission_sha256,
        qualification_grant_sha256=launched.case.grant.grant_sha256,
        intent_sha256=launched.intent.intent_sha256,
        execution_id=launched.intent.execution_id,
        infrastructure_attempt_id=launched_reservation.attempt_id,
        runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            state.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        admitted_at=NOW,
        hard_deadline=launched_reservation.hard_deadline,
        issued_at=launched.clock.now(),
        recovery_expires_at=NOW + timedelta(minutes=30),
    )
    wrong_recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=launched_reservation.admission_sha256,
        qualification_grant_sha256=launched.case.grant.grant_sha256,
        intent_sha256=launched.intent.intent_sha256,
        execution_id=launched.intent.execution_id,
        infrastructure_attempt_id=launched_reservation.attempt_id,
        runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=_digest("wrong-launch"),
        admitted_at=NOW,
        hard_deadline=launched_reservation.hard_deadline,
        issued_at=launched.clock.now(),
        recovery_expires_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(AssignmentRejected, match="differs from durable launched lineage"):
        launched.agent.run_assignment(
            replace(
                launched.assignment,
                reservation=launched_reservation,
                lease_token=None,
                historical_recovery_grant=wrong_recovery,
            )
        )
    result = launched.agent.run_assignment(
        replace(
            launched.assignment,
            reservation=launched_reservation,
            lease_token=None,
            historical_recovery_grant=recovery,
        )
    )
    assert result.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert launched.runtime.launch_calls == 1
    assert launched.runtime.rebind_calls == 0


def test_historical_recovery_collects_existing_runtime_after_live_lease_expires(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.node_runtime_launch_receipt is not None
    harness.runtime.finish(exit_code=0)

    reservation = replace(harness.allocator.current, status="running")
    harness.allocator.current = reservation
    harness.clock.current = reservation.lease_expires_at + timedelta(milliseconds=141)
    harness.clock.monotonic += int(timedelta(minutes=10, milliseconds=141).total_seconds() * 1e9)
    with pytest.raises(AssignmentRejected, match="exact allocator/grant authority"):
        harness.agent.run_assignment(replace(harness.assignment, reservation=reservation))
    recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=reservation.admission_sha256,
        qualification_grant_sha256=harness.case.grant.grant_sha256,
        intent_sha256=harness.intent.intent_sha256,
        execution_id=harness.intent.execution_id,
        infrastructure_attempt_id=reservation.attempt_id,
        runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            state.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        admitted_at=NOW,
        hard_deadline=reservation.hard_deadline,
        issued_at=harness.clock.now(),
        recovery_expires_at=reservation.hard_deadline + timedelta(hours=4),
    )
    assignment = replace(
        harness.assignment,
        reservation=reservation,
        lease_token=None,
        historical_recovery_grant=recovery,
    )

    result = harness.agent.run_assignment(assignment)

    assert result.outcome is NodeRunOutcome.COLLECTED
    assert result.terminal_disposition is NodeTerminalDisposition.PROCESS_SUCCEEDED
    assert result.accepted_runtime_termination is not None
    assert harness.runtime.launch_calls == 1
    assert harness.runtime.rebind_calls == 0


def test_historical_recovery_replays_db_accepted_hash_after_local_save_crash(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_mark_terminated = True

    with pytest.raises(SystemExit, match="termination commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    accepted = harness.allocator.last_accepted
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert accepted is not None and state is not None
    assert state.node_runtime_launch_receipt is not None
    assert (
        harness.state.load_accepted_runtime_termination(attempt_id=harness.reservation.attempt_id)
        is None
    )
    assert (
        harness.state.load_runtime_termination_proof(
            attempt_id=harness.reservation.attempt_id,
            inspection_sequence=state.inspection_sequence,
        )
        is not None
    )
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000
    recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=harness.allocator.current.admission_sha256,
        qualification_grant_sha256=harness.case.grant.grant_sha256,
        intent_sha256=state.intent_sha256,
        execution_id=state.execution_id,
        infrastructure_attempt_id=state.attempt_id,
        runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            state.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        admitted_at=NOW,
        hard_deadline=harness.allocator.current.hard_deadline,
        issued_at=harness.clock.now(),
        recovery_expires_at=accepted.artifact_submission_deadline,
    )
    recovery_assignment = replace(
        harness.assignment,
        reservation=harness.allocator.current,
        lease_token=None,
        historical_recovery_grant=recovery,
    )

    recovered = harness.agent.run_assignment(recovery_assignment)
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.accepted_runtime_termination == accepted
    assert harness.allocator.accepted_replay_calls == 1
    assert harness.allocator.calls.count("terminated") == 1
    local_accepted = harness.state.load_accepted_runtime_termination(
        attempt_id=harness.reservation.attempt_id
    )
    assert local_accepted is not None and local_accepted[2] == accepted


def test_historical_recovery_accepted_hash_without_local_proof_fails_closed(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None and state.node_runtime_launch_receipt is not None
    reservation = replace(harness.allocator.current, status="reconciliation_required")
    harness.allocator.current = reservation
    recovery = issue_historical_runtime_recovery_grant(
        pin=_runtime_control_pin(),
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
        admission_sha256=reservation.admission_sha256,
        qualification_grant_sha256=harness.case.grant.grant_sha256,
        intent_sha256=state.intent_sha256,
        execution_id=state.execution_id,
        infrastructure_attempt_id=state.attempt_id,
        runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=(
            state.node_runtime_launch_receipt.launch_receipt_sha256
        ),
        accepted_runtime_termination_sha256=_digest("missing-accepted-termination"),
        admitted_at=NOW,
        hard_deadline=reservation.hard_deadline,
        issued_at=harness.clock.now(),
        recovery_expires_at=reservation.hard_deadline + timedelta(hours=4),
    )
    with pytest.raises(AssignmentRejected, match="complete persisted proof"):
        harness.agent.run_assignment(
            replace(
                harness.assignment,
                reservation=reservation,
                lease_token=None,
                historical_recovery_grant=recovery,
            )
        )
    assert harness.allocator.accepted_replay_calls == 0


@pytest.mark.parametrize(
    "rogue_state",
    [RuntimeInspectionState.RUNNING, RuntimeInspectionState.TERMINATED],
)
def test_prepared_runtime_observation_is_never_retroactively_authorized_or_signed(
    tmp_path: Path, rogue_state: RuntimeInspectionState
) -> None:
    harness = _Harness(tmp_path)
    harness.runtime.prepared_observation_state = rogue_state

    with pytest.raises(RuntimeRejected, match="changed the exact runtime identity"):
        harness.agent.run_once()
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
    assert adopted.runtime_fence_rebind_receipt is harness.allocator.last_rebind_receipt
    assert adopted.runtime_fence_rebind_receipt is not None
    assert harness.allocator.last_rebind_request is not None
    assert (
        harness.allocator.last_rebind_request.request_sha256
        == adopted.runtime_fence_rebind_receipt.evidence.request_sha256
    )
    assert adopted.adoption_receipt.previous_fencing_epoch == 1
    assert adopted.adoption_receipt.new_fencing_epoch == 2
    assert adopted.adoption_receipt.runtime_identity_sha256 == (
        adopted.runtime_identity.runtime_identity_sha256
    )
    assert harness.raw_token.encode() not in canonical_json_bytes(adopted.adoption_receipt)
    assert harness.raw_token.encode() not in canonical_json_bytes(
        adopted.runtime_fence_rebind_receipt
    )
    assert adopted.runtime_fence_rebind_receipt.evidence.previous_fencing_epoch == 1
    assert adopted.runtime_fence_rebind_receipt.evidence.new_fencing_epoch == 2
    assert harness.runtime.launch_calls == 1
    assert harness.runtime.rebind_calls == 1


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


def test_expired_adoption_after_runtime_rebind_fails_closed_without_second_cas(
    tmp_path: Path,
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

    held = harness.agent.run_assignment(harness.replay_assignment())
    assert held.outcome is NodeRunOutcome.RECONCILIATION_REQUIRED
    assert len(tuple((harness.state.root / "adoptions").iterdir())) == 1
    assert len(tuple((harness.state.root / "rebinds").iterdir())) == 1
    assert harness.allocator.current.fencing_epoch == 1
    assert harness.runtime.current_fencing_epoch == 2
    assert harness.runtime.rebind_calls == 1
    assert harness.allocator.calls.count("adopt") == 1
    assert harness.runtime.launch_calls == 1


def test_failure_is_collectable_but_postlaunch_absence_cannot_forge_exit(
    tmp_path: Path,
) -> None:
    failed = _Harness(tmp_path / "failed")
    assert failed.agent.run_once().outcome is NodeRunOutcome.RUNNING
    failed.runtime.finish(exit_code=17, write_declared=False)
    result = failed.agent.run_assignment(failed.replay_assignment())
    assert result.outcome is NodeRunOutcome.COLLECTED
    assert result.artifact_manifest is not None
    assert result.artifact_manifest.entries == ()
    assert result.node_runtime_termination_receipt is not None
    assert result.node_runtime_termination_receipt.termination_evidence.exit_code == 17
    assert result.terminal_disposition is NodeTerminalDisposition.PROCESS_FAILED
    assert result.terminal_submission is not None
    assert result.terminal_submission.artifact_manifest_sha256 == (
        result.artifact_manifest.manifest_sha256
    )

    absent = _Harness(tmp_path / "absent")
    assert absent.agent.run_once().outcome is NodeRunOutcome.RUNNING
    absent.runtime.state = RuntimeInspectionState.ABSENT
    with pytest.raises(RuntimeRejected, match="changed the exact runtime identity"):
        absent.agent.run_assignment(absent.replay_assignment())
    assert absent.allocator.last_inspection is None
    assert "terminated" not in absent.allocator.calls
    assert "verifying" not in absent.allocator.calls


def test_termination_commit_crash_reuses_exact_journal_without_new_transition(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_mark_terminated = True

    with pytest.raises(SystemExit, match="termination commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    committed_proof = harness.allocator.last_node_termination
    committed_acceptance = harness.allocator.last_accepted
    assert committed_proof is not None
    assert committed_acceptance is not None
    assert harness.allocator.current.status == "terminated"
    assert len(tuple((harness.state.root / "terminations").iterdir())) == 2

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.node_runtime_termination_receipt == committed_proof
    assert recovered.accepted_runtime_termination == committed_acceptance
    assert harness.allocator.calls.count("terminated") == 1
    assert harness.allocator.calls.count("verifying") == 1


def test_challenge_commit_crash_replays_byte_exact_durable_terminal_evidence(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_challenge_commit = True

    with pytest.raises(SystemExit, match="challenge commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    pending = harness.state.load_runtime_termination_evidence(
        attempt_id=harness.reservation.attempt_id
    )
    challenge = harness.allocator.last_challenge
    assert pending is not None and challenge is not None
    sequence, evidence = pending
    assert challenge.inspection_sequence == sequence
    assert challenge.runtime_inspection_evidence_sha256 == evidence.inspection_sha256

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.runtime_termination_challenge == challenge
    assert recovered.node_runtime_termination_receipt is not None
    assert recovered.node_runtime_termination_receipt.termination_evidence == evidence


def test_expired_challenge_commit_crash_refreshes_append_only_terminal_generation(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_challenge_commit = True

    with pytest.raises(SystemExit, match="challenge commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    old_challenge = harness.allocator.last_challenge
    old_pending = harness.state.load_runtime_termination_evidence(
        attempt_id=harness.reservation.attempt_id
    )
    assert old_challenge is not None and old_pending is not None
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.runtime_termination_challenge is not None
    assert recovered.runtime_termination_challenge.inspection_sequence == (
        old_challenge.inspection_sequence + 1
    )
    assert recovered.runtime_termination_challenge != old_challenge
    new_pending = harness.state.load_runtime_termination_evidence(
        attempt_id=harness.reservation.attempt_id,
        inspection_sequence=recovered.runtime_termination_challenge.inspection_sequence,
    )
    assert new_pending is not None
    assert new_pending[1].engine_terminal_journal_sha256 == (
        old_pending[1].engine_terminal_journal_sha256
    )
    assert new_pending[1].ended_at == old_pending[1].ended_at
    assert new_pending[1].inspected_at > old_pending[1].inspected_at
    assert len(harness.allocator.challenge_history) == 2
    evidence_files = tuple((harness.state.root / "terminations").glob("*.evidence.json"))
    assert len(evidence_files) == 2


def test_expired_unaccepted_local_terminal_proof_is_reinspected_before_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    original_accept = harness.allocator.accept_runtime_termination
    crash_once = True

    def crash_before_accept(**kwargs):
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise SystemExit("crash after durable local termination proof")
        return original_accept(**kwargs)

    monkeypatch.setattr(harness.allocator, "accept_runtime_termination", crash_before_accept)
    with pytest.raises(SystemExit, match="durable local termination proof"):
        harness.agent.run_assignment(harness.replay_assignment())
    monkeypatch.setattr(harness.allocator, "accept_runtime_termination", original_accept)
    old_proof = harness.state.load_runtime_termination_proof(
        attempt_id=harness.reservation.attempt_id
    )
    assert old_proof is not None
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.node_runtime_termination_receipt is not None
    assert recovered.node_runtime_termination_receipt.inspection_sequence == (
        old_proof[1].inspection_sequence + 1
    )
    assert recovered.node_runtime_termination_receipt.termination_evidence.ended_at == (
        old_proof[1].termination_evidence.ended_at
    )
    proof_files = tuple((harness.state.root / "terminations").glob("*.proof.json"))
    assert len(proof_files) == 2


def test_expired_node_proof_does_not_strand_accepted_post_quarantine_submission(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_before_mark_verifying = True

    with pytest.raises(SystemExit, match="verification commit"):
        harness.agent.run_assignment(harness.replay_assignment())

    accepted = harness.allocator.last_accepted
    proof = harness.allocator.last_node_termination
    assert accepted is not None and proof is not None
    submission = harness.state.load_terminal_submission_result(
        attempt_id=harness.reservation.attempt_id,
        accepted=accepted,
    )[1]
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    refreshed = harness.agent.run_assignment(harness.replay_assignment())
    assert refreshed.outcome is NodeRunOutcome.COLLECTED
    assert refreshed.accepted_runtime_termination == accepted
    assert refreshed.node_runtime_termination_receipt == proof
    assert refreshed.terminal_submission == submission
    assert len(tuple((harness.state.root / "results").iterdir())) == 1


def test_final_terminal_acceptance_db_commit_crash_replays_historical_chain(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    harness.allocator.crash_after_terminal_acceptance_commit = True

    with pytest.raises(SystemExit, match="final terminal acceptance commit"):
        harness.agent.run_assignment(harness.replay_assignment())
    terminal_acceptance = harness.allocator.last_terminal_acceptance
    assert terminal_acceptance is not None
    assert harness.allocator.current.status == "verifying"
    harness.clock.current += timedelta(seconds=11)
    harness.clock.monotonic += 11 * 1_000_000_000

    recovered = harness.agent.run_assignment(harness.replay_assignment())
    assert recovered.outcome is NodeRunOutcome.COLLECTED
    assert recovered.accepted_terminal_submission == terminal_acceptance
    assert recovered.terminal_submission == harness.allocator.last_submission
    assert recovered.node_execution_receipt is None
    assert harness.allocator.calls.count("verifying") == 2


def test_artifact_verifier_host_time_ahead_does_not_order_node_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    original_verify = harness.artifacts.verify_manifest

    def verify_with_future_host_time(**kwargs):
        return tuple(
            receipt.model_copy(update={"verified_at": harness.clock.now() + timedelta(hours=1)})
            for receipt in original_verify(**kwargs)
        )

    monkeypatch.setattr(harness.artifacts, "verify_manifest", verify_with_future_host_time)
    collected = harness.agent.run_assignment(harness.replay_assignment())

    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert collected.terminal_submission is not None
    assert collected.artifact_verified_receipts
    assert all(
        receipt.verified_at > collected.terminal_submission.submitted_at
        for receipt in collected.artifact_verified_receipts
    )
    assert collected.terminal_submission.submitted_at == harness.clock.now()


def test_future_dated_terminal_submission_is_rejected_before_its_signed_time(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.agent.run_once().outcome is NodeRunOutcome.RUNNING
    harness.runtime.finish(exit_code=0)
    collected = harness.agent.run_assignment(harness.replay_assignment())
    assert collected.outcome is NodeRunOutcome.COLLECTED
    assert collected.runtime_termination_challenge is not None
    assert collected.node_runtime_termination_receipt is not None
    assert collected.accepted_runtime_termination is not None
    assert collected.artifact_manifest is not None
    assert collected.terminal_submission is not None
    state = harness.state.load_state(harness.reservation.attempt_id)
    assert state is not None
    assert state.node_runtime_launch_receipt is not None
    assert state.runtime_launch_authorization_request is not None
    assert state.runtime_launch_authorization is not None
    future_signed_at = collected.terminal_submission.submitted_at + timedelta(minutes=1)
    future_submission = issue_qualification_terminal_submission(
        node_authority=harness.authority,
        runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        private_key=PRIVATE_KEY,
        intent=harness.intent,
        accepted=collected.accepted_runtime_termination,
        challenge=collected.runtime_termination_challenge,
        node_termination_receipt=collected.node_runtime_termination_receipt,
        preparation=state.runtime_preparation,
        launch_receipt=state.node_runtime_launch_receipt,
        launch_authorization_request=state.runtime_launch_authorization_request,
        launch_authorization=state.runtime_launch_authorization,
        node_inventory_sha256=harness.reservation.node_inventory_sha256,
        resource_lease_sha256=harness.reservation.resource_lease_sha256,
        artifact_manifest=collected.artifact_manifest,
        artifact_verified_receipts=collected.artifact_verified_receipts,
        disposition=collected.terminal_submission.disposition,
        submitted_at=future_signed_at,
    )
    with pytest.raises(QualificationVerificationError, match="verified artifacts"):
        verify_qualification_terminal_submission(
            submission=future_submission,
            intent=harness.intent,
            accepted=collected.accepted_runtime_termination,
            challenge=collected.runtime_termination_challenge,
            node_termination_receipt=collected.node_runtime_termination_receipt,
            preparation=state.runtime_preparation,
            launch_receipt=state.node_runtime_launch_receipt,
            launch_authorization_request=state.runtime_launch_authorization_request,
            launch_authorization=state.runtime_launch_authorization,
            artifact_manifest=collected.artifact_manifest,
            artifact_verified_receipts=collected.artifact_verified_receipts,
            expected_node_inventory_sha256=harness.reservation.node_inventory_sha256,
            expected_resource_lease_sha256=harness.reservation.resource_lease_sha256,
            node_authority=harness.authority,
            runtime_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
            verified_at=future_signed_at - timedelta(seconds=1),
        )


def test_exit_zero_missing_required_and_post_deadline_are_bounded_typed_failures(
    tmp_path: Path,
) -> None:
    missing = _Harness(tmp_path / "missing")
    assert missing.agent.run_once().outcome is NodeRunOutcome.RUNNING
    missing.runtime.finish(exit_code=0, write_declared=False)
    invalid = missing.agent.run_assignment(missing.replay_assignment())
    assert invalid.outcome is NodeRunOutcome.COLLECTED
    assert invalid.terminal_disposition is NodeTerminalDisposition.INVALID_OUTPUT
    assert invalid.node_runtime_termination_receipt is not None
    assert invalid.node_runtime_termination_receipt.termination_evidence.exit_code == 0
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
    assert timed_out.node_runtime_termination_receipt is not None
    assert timed_out.node_runtime_termination_receipt.termination_evidence.exit_code == 0
    assert timed_out.artifact_manifest is not None
    assert timed_out.artifact_manifest.entries


def test_signed_terminal_deadline_expiration_binds_full_chain_without_artifact_claims(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
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
    assert accepted is not None
    assert preparation is not None
    assert authorization_request is not None and start is not None
    assert launch_receipt is not None and challenge is not None
    assert node_termination is not None
    deadline_pin = _runtime_control_pin().model_copy(
        update={"expires_at": accepted.artifact_submission_deadline}
    )
    expiration = issue_qualification_terminal_deadline_expiration(
        pin=deadline_pin,
        private_key=RUNTIME_CONTROL_PRIVATE_KEY,
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
        runtime_authority=RuntimeControlAuthorityVerifier(deadline_pin),
    )

    assert expiration.disposition == "invalid_output"
    assert expiration.retryable is False
    assert expiration.accepted_runtime_termination_sha256 == (accepted.accepted_termination_sha256)
    assert expiration.authorized_at == accepted.accepted_at
    assert expiration.expired_at == accepted.artifact_submission_deadline
    assert "artifact_manifest_sha256" not in (QualificationTerminalDeadlineExpiration.model_fields)
    assert "artifact_verified_receipt_sha256s" not in (
        QualificationTerminalDeadlineExpiration.model_fields
    )
    assert (
        verify_qualification_terminal_deadline_expiration(
            expiration=expiration,
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
            runtime_authority=RuntimeControlAuthorityVerifier(deadline_pin),
        )
        == expiration
    )

    with pytest.raises(QualificationVerificationError):
        verify_qualification_terminal_deadline_expiration(
            expiration=expiration.model_copy(
                update={
                    "accepted_runtime_termination_sha256": _digest("forged-terminal-expiration")
                }
            ),
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
            runtime_authority=RuntimeControlAuthorityVerifier(deadline_pin),
        )


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
        runtime_control_authority=RuntimeControlAuthorityVerifier(_runtime_control_pin()),
        node_signing_private_key=PRIVATE_KEY,
        boot_id="boot.qualification-001",
        allocator_principal_id="principal:allocator",
        allocator=harness.allocator,
        runtime=harness.runtime,
        output_quota_provisioner=harness.output_quota_provisioner,
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
    with pytest.raises(AssignmentRejected, match="exact allocator/grant authority"):
        expired.agent.run_once()
    assert expired.runtime.prepare_calls == 0
    assert expired.runtime.launch_calls == 0
    assert "terminated" not in expired.allocator.calls
    assert "verifying" not in expired.allocator.calls
