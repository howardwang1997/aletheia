from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

import aletheia.execution.oci_runtime as oci_runtime_module
from aletheia.execution.node_agent import (
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedLaunchSpec,
    ReservedDeviceBinding,
    RuntimeLabel,
    RuntimeLaunchRequest,
)
from aletheia.execution.oci_runtime import (
    DeploymentPinnedOCIPolicy,
    OCIDevicePathPin,
    OCIJournalError,
    OCIProductionCapability,
    OCIProductionCapabilityError,
    OCIWatchdogCleanupQuiescence,
    LocalQualificationOCIRuntime,
    SystemOCIRuntimeClock,
    host_parent_chain_sha256,
)
from aletheia.execution.qualification_launch_gate import (
    QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
)
from aletheia.execution.runtime_contracts import (
    NodeRuntimeIdentity,
    RuntimeInspectionState,
    qualification_key_id,
)
from aletheia.execution.runtime_v2_contracts import (
    InputMaterializationEntry,
    InputMaterializationReceipt,
    OutputQuotaProvisioningReceipt,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeFenceRebindRequest,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimeLaunchEvidence,
    issue_runtime_launch_authorization,
)
from aletheia.execution.schemas import NetworkPolicy, canonical_sha256

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
EXECUTION_ID = "exe_" + "a" * 32
ATTEMPT_ID = "iat_" + "b" * 32
CONTROL_PRIVATE_KEY = bytes(range(32))


class _Clock:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 1_000_000_000
        self.boottime = 1_000_000_000

    def now(self) -> datetime:
        self.wall += timedelta(microseconds=1)
        return self.wall

    def monotonic_ns(self) -> int:
        self.boottime += 1_000
        self.monotonic = self.boottime
        return self.boottime

    def boottime_ns(self) -> int:
        self.boottime += 1_000
        return self.boottime


class _QuotaController:
    def verify_enforced_quota(self, **scope: object) -> str:
        value = scope["expected_evidence_sha256"]
        assert isinstance(value, str)
        return value


class _LaunchGateVerifier:
    def verify_immutable_launch_gate(self, **scope: object) -> str:
        value = scope["expected_evidence_sha256"]
        assert isinstance(value, str)
        return value


class _WrongQuotaController:
    def verify_enforced_quota(self, **scope: object) -> str:
        del scope
        return H0


class _WrongLaunchGateVerifier:
    def verify_immutable_launch_gate(self, **scope: object) -> str:
        del scope
        return H0


class _DeadlineWatchdogController:
    def arm_and_verify_deadline_watchdog(self, **scope: object) -> str:
        value = scope["expected_evidence_sha256"]
        assert isinstance(value, str)
        return value

    def retire_and_verify_deadline_watchdog(self, **scope: object) -> OCIWatchdogCleanupQuiescence:
        value = scope["expected_evidence_sha256"]
        assert isinstance(value, str)
        return OCIWatchdogCleanupQuiescence(
            cleanup_evidence_sha256=value,
            decision="retired",
        )


class _RecordingDeadlineWatchdogController(_DeadlineWatchdogController):
    def __init__(self) -> None:
        self.retire_calls: list[dict[str, object]] = []

    def retire_and_verify_deadline_watchdog(self, **scope: object) -> OCIWatchdogCleanupQuiescence:
        self.retire_calls.append(scope)
        return super().retire_and_verify_deadline_watchdog(**scope)


class _WrongDeadlineWatchdogController:
    def arm_and_verify_deadline_watchdog(self, **scope: object) -> str:
        del scope
        return H0

    def retire_and_verify_deadline_watchdog(self, **scope: object) -> OCIWatchdogCleanupQuiescence:
        del scope
        return OCIWatchdogCleanupQuiescence(
            cleanup_evidence_sha256=H0,
            decision="retired",
        )


class _WrongRetirementDeadlineWatchdogController(_DeadlineWatchdogController):
    def retire_and_verify_deadline_watchdog(self, **scope: object) -> OCIWatchdogCleanupQuiescence:
        del scope
        return OCIWatchdogCleanupQuiescence(
            cleanup_evidence_sha256=H0,
            decision="retired",
        )


class _WrongContainerDeadlineWatchdogController(_DeadlineWatchdogController):
    def retire_and_verify_deadline_watchdog(self, **scope: object) -> OCIWatchdogCleanupQuiescence:
        value = scope["expected_evidence_sha256"]
        assert isinstance(value, str)
        return OCIWatchdogCleanupQuiescence(
            cleanup_evidence_sha256=value,
            decision="fired_stopped",
            service_quiescence_record_sha256=H1,
            container_id="f" * 64,
        )


class _RejectingInitialDeviceFenceController:
    def __init__(self) -> None:
        self.initial_calls = 0

    def apply_initial_fence(self, **scope: object) -> str:
        self.initial_calls += 1
        del scope
        return H0

    def expected_rebind_evidence_sha256(self, **scope: object) -> str:
        del scope
        return H1

    def apply_rebind(self, **scope: object) -> str:
        del scope
        return H1


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _control_pin() -> RuntimeControlAuthorityPin:
    public_key = (
        Ed25519PrivateKey.from_private_bytes(CONTROL_PRIVATE_KEY)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-control-policy"),
        principal_id="principal:runtime-control-authority",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
    )


def _spec() -> PinnedLaunchSpec:
    return PinnedLaunchSpec(
        command_sha256=_digest("command"),
        environment_sha256=_digest("environment"),
        capability_manifest_sha256=_digest("capability"),
        executable_sha256=_digest("executable"),
        runtime_engine="docker",
        argv=("/opt/qualifier/bin/run", "--input", "/opt/aletheia/input/data.bin"),
        environment=(PinnedEnvironmentVariable(name="MODE", value="qualification"),),
        artifact_paths=(PinnedArtifactPath(artifact_key="raw", relative_path="result.bin"),),
        network_policy=NetworkPolicy.NONE,
    )


def _deployment_file(path: Path, payload: bytes, mode: int) -> os.stat_result:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return path.stat()


def _policy(tmp_path: Path, spec: PinnedLaunchSpec | None = None) -> DeploymentPinnedOCIPolicy:
    spec = spec or _spec()
    deployment_root = tmp_path / "deployment"
    binary_path = deployment_root / "bin" / "docker"
    seccomp_path = deployment_root / "policy" / "seccomp.json"
    binary_bytes = b"#!/bin/sh\nexit 99\n"
    seccomp_bytes = b'{"defaultAction":"SCMP_ACT_ERRNO"}\n'
    binary = _deployment_file(binary_path, binary_bytes, 0o500)
    seccomp = _deployment_file(seccomp_path, seccomp_bytes, 0o400)
    return DeploymentPinnedOCIPolicy(
        policy_id="policy.oci-qualification.v2",
        runtime_binary_path=str(binary_path),
        runtime_binary_sha256=hashlib.sha256(binary_bytes).hexdigest(),
        runtime_binary_device=binary.st_dev,
        runtime_binary_inode=binary.st_ino,
        runtime_binary_owner_uid=binary.st_uid,
        runtime_binary_owner_gid=binary.st_gid,
        runtime_binary_mode=stat.S_IMODE(binary.st_mode),
        runtime_binary_parent_chain_sha256=host_parent_chain_sha256(binary_path),
        image_reference=f"registry.invalid/aletheia/qualifier@sha256:{H1}",
        image_manifest_sha256=H1,
        image_config_sha256=H2,
        oci_platform="linux/amd64",
        launch_spec_sha256=spec.launch_spec_sha256,
        capability_manifest_sha256=spec.capability_manifest_sha256,
        command_sha256=spec.command_sha256,
        environment_sha256=spec.environment_sha256,
        executable_sha256=spec.executable_sha256,
        launch_gate_path="/opt/aletheia/bin/qualification-launch-gate",
        launch_gate_executable_sha256=_digest("launch-gate-executable"),
        launch_gate_protocol_sha256=QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
        sandbox_policy_sha256=_digest("sandbox-policy"),
        seccomp_profile_path=str(seccomp_path),
        seccomp_profile_sha256=hashlib.sha256(seccomp_bytes).hexdigest(),
        seccomp_profile_device=seccomp.st_dev,
        seccomp_profile_inode=seccomp.st_ino,
        seccomp_profile_owner_uid=seccomp.st_uid,
        seccomp_profile_owner_gid=seccomp.st_gid,
        seccomp_profile_mode=stat.S_IMODE(seccomp.st_mode),
        seccomp_profile_parent_chain_sha256=host_parent_chain_sha256(seccomp_path),
        apparmor_profile="aletheia-qualification-v2",
        workload_uid=os.geteuid(),
        workload_gid=os.getegid(),
    )


def _request(tmp_path: Path) -> tuple[RuntimeLaunchRequest, DeploymentPinnedOCIPolicy]:
    spec = _spec()
    policy = _policy(tmp_path, spec)
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir(mode=0o700)
    input_root.chmod(0o500)
    output_root.mkdir(mode=0o700)
    input_metadata = input_root.lstat()
    receipt = InputMaterializationReceipt(
        intent_sha256=H3,
        execution_id=EXECUTION_ID,
        infrastructure_attempt_id=ATTEMPT_ID,
        entries=(),
        staged_root_identity_sha256=canonical_sha256(
            {
                "schema": "aletheia.local_staged_input_root_identity.v2",
                "resolved_path_sha256": hashlib.sha256(os.fsencode(input_root)).hexdigest(),
                "device": input_metadata.st_dev,
                "inode": input_metadata.st_ino,
                "owner_uid": input_metadata.st_uid,
                "owner_gid": input_metadata.st_gid,
                "mode": stat.S_IMODE(input_metadata.st_mode),
                "link_count": input_metadata.st_nlink,
                "modified_ns": input_metadata.st_mtime_ns,
                "changed_ns": input_metadata.st_ctime_ns,
                "entries": (),
            }
        ),
        materializer_principal_id="principal:input-materializer",
        materialized_at=NOW - timedelta(minutes=1),
    )
    output_metadata = output_root.lstat()
    quota_receipt = OutputQuotaProvisioningReceipt(
        node_manifest_sha256=H0,
        node_id="node.qualification-01",
        boot_id="boot-test-01",
        execution_id=EXECUTION_ID,
        infrastructure_attempt_id=ATTEMPT_ID,
        intent_sha256=H3,
        output_root=str(output_root),
        output_quota_bytes=1024 * 1024,
        output_root_device=output_metadata.st_dev,
        output_root_inode=output_metadata.st_ino,
        output_root_owner_uid=output_metadata.st_uid,
        output_root_owner_gid=output_metadata.st_gid,
        mount_id=1,
        mount_parent_id=1,
        block_device_major=os.major(output_metadata.st_dev),
        block_device_minor=os.minor(output_metadata.st_dev),
        block_device_capacity_bytes=1024 * 1024,
        filesystem_type="ext4",
        filesystem_uuid_sha256=_digest("test-output-filesystem"),
        mount_options=("nodev", "noexec", "nosuid", "rw"),
        backing_file_identity_sha256=_digest("test-output-backing"),
        provisioner_policy_sha256=_digest("test-output-provisioner"),
        provisioner_principal_id="principal:test-output-provisioner",
        provisioned_at=NOW - timedelta(minutes=2),
    )
    request = RuntimeLaunchRequest(
        spec=spec,
        node_manifest_sha256=H0,
        node_id="node.qualification-01",
        boot_id="boot-test-01",
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        intent_sha256=H3,
        node_inventory_sha256=H4,
        resource_lease_sha256=H5,
        selected_resource_ids=("cpu.0",),
        cpu_cores=2,
        memory_bytes=1024 * 1024 * 1024,
        scratch_bytes=64 * 1024 * 1024,
        exclusive=True,
        device_leases=(),
        fencing_epoch=1,
        lease_token_sha256=_digest("lease-token-1"),
        runtime_id="qualification-runtime-01",
        labels=(RuntimeLabel(name="aletheia.runtime_id", value="qualification-runtime-01"),),
        input_root=input_root,
        output_root=output_root,
        output_quota_bytes=1024 * 1024,
        deadline=NOW + timedelta(minutes=5),
        input_materialization_receipt=receipt,
        output_quota_provisioning_receipt=quota_receipt,
    )
    return request, policy


def _request_with_materialized_file(
    tmp_path: Path,
) -> tuple[RuntimeLaunchRequest, DeploymentPinnedOCIPolicy]:
    request, policy = _request(tmp_path)
    root = request.input_root
    root.chmod(0o700)
    payload = b"trusted qualification input\n"
    path = root / "data.bin"
    path.write_bytes(payload)
    path.chmod(0o400)
    root.chmod(0o500)
    content_sha256 = hashlib.sha256(payload).hexdigest()
    file_metadata = path.lstat()
    file_identity = canonical_sha256(
        {
            "schema": "aletheia.local_staged_input_file_identity.v2",
            "relative_path": "data.bin",
            "content_sha256": content_sha256,
            "content_bytes": len(payload),
            "device": file_metadata.st_dev,
            "inode": file_metadata.st_ino,
            "owner_uid": file_metadata.st_uid,
            "owner_gid": file_metadata.st_gid,
            "mode": stat.S_IMODE(file_metadata.st_mode),
            "link_count": file_metadata.st_nlink,
            "modified_ns": file_metadata.st_mtime_ns,
            "changed_ns": file_metadata.st_ctime_ns,
        }
    )
    entry = InputMaterializationEntry(
        input_port_id="input.data",
        verified_receipt_sha256=_digest("verified-input-receipt"),
        content_sha256=content_sha256,
        content_bytes=len(payload),
        relative_path="data.bin",
        staged_file_identity_sha256=file_identity,
    )
    root_metadata = root.lstat()
    receipt = InputMaterializationReceipt(
        intent_sha256=request.intent_sha256,
        execution_id=request.execution_id,
        infrastructure_attempt_id=request.attempt_id,
        entries=(entry,),
        staged_root_identity_sha256=canonical_sha256(
            {
                "schema": "aletheia.local_staged_input_root_identity.v2",
                "resolved_path_sha256": hashlib.sha256(os.fsencode(root)).hexdigest(),
                "device": root_metadata.st_dev,
                "inode": root_metadata.st_ino,
                "owner_uid": root_metadata.st_uid,
                "owner_gid": root_metadata.st_gid,
                "mode": stat.S_IMODE(root_metadata.st_mode),
                "link_count": root_metadata.st_nlink,
                "modified_ns": root_metadata.st_mtime_ns,
                "changed_ns": root_metadata.st_ctime_ns,
                "entries": (
                    {
                        "input_port_id": entry.input_port_id,
                        "relative_path": entry.relative_path,
                        "staged_file_identity_sha256": (entry.staged_file_identity_sha256),
                    },
                ),
            }
        ),
        materializer_principal_id="principal:input-materializer",
        materialized_at=NOW - timedelta(minutes=1),
    )
    return replace(request, input_materialization_receipt=receipt), policy


def _runtime(
    tmp_path: Path,
    policy: DeploymentPinnedOCIPolicy,
    *,
    clock: _Clock | None = None,
) -> LocalQualificationOCIRuntime:
    return LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "runtime-journal",
        clock=clock or _Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
        launch_gate_verifier=_LaunchGateVerifier(),
        deadline_watchdog_controller=_DeadlineWatchdogController(),
    )


def _launch_authorization(
    preparation,  # type: ignore[no-untyped-def]
    *,
    max_launch_delay_ns: int = 10_000_000,
    nonce_label: str = "runtime-launch-nonce",
) -> tuple[RuntimeLaunchAuthorizationRequest, RuntimeLaunchAuthorization]:
    authorization_request = RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest(nonce_label),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        requested_at=preparation.prepared_at,
        requested_monotonic_ns=preparation.prepared_monotonic_ns + 1_000,
    )
    authorization = issue_runtime_launch_authorization(
        pin=_control_pin(),
        private_key=CONTROL_PRIVATE_KEY,
        admission_sha256=_digest("runtime-launch-admission"),
        qualification_grant_sha256=_digest("runtime-launch-grant"),
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
        lease_expires_at=NOW + timedelta(minutes=5),
        hard_deadline=NOW + timedelta(minutes=5),
        issued_at=preparation.prepared_at + timedelta(microseconds=1),
        expires_at=preparation.prepared_at + timedelta(seconds=30),
        max_launch_delay_ns=max_launch_delay_ns,
    )
    return authorization_request, authorization


def _capability(
    runtime: LocalQualificationOCIRuntime,
    request,  # type: ignore[no-untyped-def]
) -> OCIProductionCapability:
    return OCIProductionCapability(
        node_id=request.node_id,
        boot_id=request.boot_id,
        cgroup_controllers=("cpu", "memory", "pids"),
        cgroup_mount_sha256=_digest("cgroup-mount"),
        runtime_binary_sha256=runtime.policy.runtime_binary_sha256,
        seccomp_profile_sha256=runtime.policy.seccomp_profile_sha256,
        engine_info_sha256=_digest("engine-info"),
        output_quota_evidence_sha256=(
            runtime._expected_output_quota_evidence_sha256(request)  # noqa: SLF001
        ),
        launch_gate_attestation_sha256=(
            runtime._expected_launch_gate_attestation_sha256()  # noqa: SLF001
        ),
        observed_at=NOW,
        observed_monotonic_ns=1_000_000_000,
    )


def _exact_engine_inspection(
    runtime: LocalQualificationOCIRuntime,
    request: RuntimeLaunchRequest,
) -> dict[str, object]:
    config = runtime.build_oci_configuration(request=request)
    mounts = [
        {
            "Type": "bind",
            "Source": item.source,
            "Destination": item.destination,
            "RW": not item.read_only,
            "Propagation": item.propagation,
        }
        for item in config.mounts
    ]
    host_mounts = [
        {
            "Type": "bind",
            "Source": item.source,
            "Target": item.destination,
            "ReadOnly": item.read_only,
            "BindOptions": {"Propagation": item.propagation},
        }
        for item in config.mounts
    ]
    devices = [
        {
            "PathOnHost": item.host_device_path,
            "PathInContainer": item.container_device_path,
            "CgroupPermissions": item.access,
        }
        for item in config.devices
    ]
    tmpfs = {}
    if config.scratch_bytes:
        tmpfs[runtime.policy.scratch_mount_target] = (
            f"rw,noexec,nosuid,nodev,size={config.scratch_bytes},mode=0700"
        )
    environment = [
        f"{item.name}={item.value}" for item in (*config.image_environment, *config.environment)
    ]
    return {
        "Id": "e" * 64,
        "Image": f"sha256:{config.image_config_sha256}",
        "AppArmorProfile": config.apparmor_profile,
        "Config": {
            "Image": config.image_reference,
            "Entrypoint": [config.entrypoint],
            "Cmd": list(config.arguments),
            "User": f"{config.workload_uid}:{config.workload_gid}",
            "WorkingDir": config.working_directory,
            "Env": environment,
            "Labels": dict(config.labels),
            "OpenStdin": False,
            "StdinOnce": False,
            "Tty": False,
            "Healthcheck": {"Test": ["NONE"]},
        },
        "HostConfig": {
            "NetworkMode": "none",
            "IpcMode": "none",
            "PidMode": "",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "Runtime": config.low_level_runtime,
            "MaskedPaths": list(config.masked_paths),
            "ReadonlyPaths": list(config.readonly_paths),
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": [
                "no-new-privileges=true",
                f"seccomp={config.seccomp_profile_path}",
                f"apparmor={config.apparmor_profile}",
            ],
            "Devices": devices,
            "DeviceRequests": [],
            "DeviceCgroupRules": [],
            "Mounts": host_mounts,
            "Binds": [],
            "Tmpfs": tmpfs,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "AutoRemove": False,
            "LogConfig": {"Type": "none", "Config": {}},
            "PublishAllPorts": False,
            "Init": False,
            "OomKillDisable": False,
            "Memory": config.memory_bytes,
            "MemorySwap": config.memory_swap_bytes,
            "CpuPeriod": config.cpu_period_microseconds,
            "CpuQuota": config.cpu_quota_microseconds,
            "PidsLimit": config.pids_limit,
        },
        "Mounts": mounts,
    }


def _created_engine_inspection(
    runtime: LocalQualificationOCIRuntime,
    request: RuntimeLaunchRequest,
) -> dict[str, object]:
    inspection = _exact_engine_inspection(runtime, request)
    inspection["RestartCount"] = 0
    inspection["State"] = {
        "Status": "created",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": False,
        "Dead": False,
        "Pid": 0,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "0001-01-01T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    }
    return inspection


def _runtime_root(tmp_path: Path) -> Path:
    roots = tuple(
        path
        for path in (tmp_path / "runtime-journal").iterdir()
        if path.is_dir() and len(path.name) == 64
    )
    assert len(roots) == 1
    return roots[0]


def _publish_submission_markers(
    runtime: LocalQualificationOCIRuntime,
    *,
    runtime_root: Path,
    request: RuntimeLaunchRequest,
    preparation,  # type: ignore[no-untyped-def]
    authorization_request: RuntimeLaunchAuthorizationRequest,
    authorization: RuntimeLaunchAuthorization,
):  # type: ignore[no-untyped-def]
    plan = runtime._coerce_request(request)  # noqa: SLF001
    config = runtime.build_oci_configuration(request=plan)
    create_command = runtime.build_create_command(request=plan)
    start_command = (
        runtime.policy.runtime_binary_path,
        "--host",
        runtime.policy.engine_endpoint,
        "start",
        config.container_name,
    )
    for phase, command in (("create", create_command), ("start", start_command)):
        runtime._publish_engine_submission(  # noqa: SLF001
            runtime_root=runtime_root,
            phase=phase,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            command=command,
        )
    return runtime._required_engine_submissions(  # noqa: SLF001
        runtime_root=runtime_root,
        request=plan,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        config=config,
    )


def _seed_pending_launch_generation(
    runtime: LocalQualificationOCIRuntime,
    *,
    runtime_root: Path,
    request: RuntimeLaunchRequest,
    preparation,  # type: ignore[no-untyped-def]
    authorization_request: RuntimeLaunchAuthorizationRequest,
    authorization: RuntimeLaunchAuthorization,
    preflight: bool,
    create_submitted: bool = False,
    start_submitted: bool = False,
):  # type: ignore[no-untyped-def]
    plan = runtime._coerce_request(request)  # noqa: SLF001
    config = runtime.build_oci_configuration(request=plan)
    runtime._publish_model(  # noqa: SLF001
        runtime_root / "launch-pending.json",
        oci_runtime_module._LaunchPending(  # noqa: SLF001
            runtime_request_sha256=plan.runtime_request_sha256,
            oci_config_sha256=config.oci_config_sha256,
            authorization_request_sha256=authorization_request.request_sha256,
            runtime_launch_authorization_sha256=authorization.authorization_sha256,
            pending_at=preparation.prepared_at + timedelta(microseconds=1),
            pending_boottime_ns=preparation.prepared_monotonic_ns + 1_000,
        ),
    )
    if preflight:
        runtime._ensure_launch_gate_authorization(  # noqa: SLF001
            runtime_root=runtime_root,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        runtime._publish_model(  # noqa: SLF001
            runtime_root / "production-capability.json",
            _capability(runtime, plan),
        )
        runtime._ensure_deadline_watchdog(  # noqa: SLF001
            runtime_root=runtime_root,
            request=plan,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    if start_submitted:
        create_submitted = True
    create_command = runtime.build_create_command(request=plan)
    start_command = (
        runtime.policy.runtime_binary_path,
        "--host",
        runtime.policy.engine_endpoint,
        "start",
        config.container_name,
    )
    if create_submitted:
        runtime._publish_engine_submission(  # noqa: SLF001
            runtime_root=runtime_root,
            phase="create",
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            command=create_command,
        )
    if start_submitted:
        runtime._publish_engine_submission(  # noqa: SLF001
            runtime_root=runtime_root,
            phase="start",
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            command=start_command,
        )
    return plan, config


def _seed_launch(
    runtime: LocalQualificationOCIRuntime,
    tmp_path: Path,
    preparation,
) -> NodeRuntimeIdentity:  # type: ignore[no-untyped-def]
    identity = NodeRuntimeIdentity(
        node_id=preparation.node_id,
        boot_id=preparation.boot_id,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        runtime_id=preparation.runtime_id,
        runtime_engine=preparation.runtime_engine,
        launch_spec_sha256=preparation.launch_spec_sha256,
        sandbox_instance_sha256=_digest("sandbox-instance"),
        process_identity_sha256=_digest("process-identity"),
        started_at=preparation.prepared_at + timedelta(microseconds=1),
        started_monotonic_ns=preparation.prepared_monotonic_ns + 1_000,
    )
    evidence = RuntimeLaunchEvidence(
        preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=_digest("runtime-launch-authorization"),
        runtime_identity=identity,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        engine_start_monotonic_lower_bound_ns=identity.started_monotonic_ns,
        engine_start_monotonic_upper_bound_exclusive_ns=(identity.started_monotonic_ns + 1),
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
        enforced_fencing_epoch=preparation.fencing_epoch,
        enforced_lease_token_sha256=preparation.lease_token_sha256,
        engine_launch_journal_sha256=_digest("engine-launch-journal"),
        launch_evidence_sha256=_digest("engine-launch-evidence"),
        observed_at=identity.started_at + timedelta(microseconds=1),
        observed_monotonic_ns=identity.started_monotonic_ns + 1_000,
    )
    runtime._publish_model(  # noqa: SLF001 - seeds durable engine evidence for Darwin journal QA
        _runtime_root(tmp_path) / "launch-evidence.json",
        evidence,
    )
    return identity


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_reference", "registry.invalid/aletheia/qualifier:latest"),
        ("network_mode", "bridge"),
        ("privileged", True),
        ("inherit_host_environment", True),
        ("allow_extra_mounts", True),
        ("docker_socket_mounted", True),
    ],
)
def test_policy_rejects_mutable_image_and_every_isolation_escape(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _policy(tmp_path).model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        DeploymentPinnedOCIPolicy.model_validate(payload)


def test_command_is_deterministic_closed_and_binds_immutable_image(tmp_path: Path) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)

    first = runtime.build_create_command(request=request)
    second = runtime.build_create_command(request=request)
    config = runtime.build_oci_configuration(request=request)

    assert first == second
    assert first[0] == policy.runtime_binary_path
    assert ("--pull", "never") == first[first.index("--pull") : first.index("--pull") + 2]
    assert ("--network", "none") == first[first.index("--network") : first.index("--network") + 2]
    assert "--read-only" in first
    assert "--cap-drop" in first and "ALL" in first
    assert "no-new-privileges=true" in first
    assert "--privileged" not in first
    assert "--env-file" not in first
    assert "-v" not in first
    image_index = first.index(policy.image_reference)
    assert first[image_index + 1 :] == config.arguments
    mount_values = [first[index + 1] for index, item in enumerate(first) if item == "--mount"]
    assert len(mount_values) == 3
    assert sum("readonly" in item for item in mount_values) == 2
    assert all("docker.sock" not in item for item in mount_values)
    assert config.image_manifest_sha256 == H1
    assert config.image_config_sha256 == H2
    assert config.entrypoint == policy.launch_gate_path
    assert config.arguments[-(len(request.spec.argv) + 1) :] == (
        "--",
        *request.spec.argv,
    )
    assert config.workload_argv == request.spec.argv
    assert "CLOCK_BOOTTIME" in config.arguments
    assert config.policy_sha256 == policy.policy_sha256
    assert ("--runtime", "runc") == first[first.index("--runtime") : first.index("--runtime") + 2]
    assert ("--cgroupns", "private") == first[
        first.index("--cgroupns") : first.index("--cgroupns") + 2
    ]
    assert "--no-healthcheck" in first
    assert policy.scratch_mount_target not in Path(policy.output_mount_target).parents
    assert Path(policy.scratch_mount_target).parent != Path(policy.output_mount_target)


@pytest.mark.parametrize(
    "weakening",
    [
        "empty-security-options",
        "cap-add-sys-admin",
        "extra-device",
        "host-pid",
        "shared-propagation",
        "extra-mount",
        "extra-label",
        "missing-scratch",
        "different-runtime",
        "host-cgroupns",
        "unmasked-system-paths",
    ],
)
def test_engine_inspection_rejects_weaker_or_expanded_sandbox(
    tmp_path: Path,
    weakening: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    exact = _exact_engine_inspection(runtime, request)
    runtime._validate_engine_configuration(  # noqa: SLF001
        exact,
        config=runtime.build_oci_configuration(request=request),
    )
    inspection = copy.deepcopy(exact)
    host_config = inspection["HostConfig"]
    container_config = inspection["Config"]
    mounts = inspection["Mounts"]
    assert isinstance(host_config, dict)
    assert isinstance(container_config, dict)
    assert isinstance(mounts, list)
    if weakening == "empty-security-options":
        host_config["SecurityOpt"] = []
    elif weakening == "cap-add-sys-admin":
        host_config["CapAdd"] = ["SYS_ADMIN"]
    elif weakening == "extra-device":
        host_config["Devices"] = [
            {
                "PathOnHost": "/dev/mem",
                "PathInContainer": "/dev/mem",
                "CgroupPermissions": "rwm",
            }
        ]
    elif weakening == "host-pid":
        host_config["PidMode"] = "host"
    elif weakening == "shared-propagation":
        mounts[0]["Propagation"] = "rshared"
    elif weakening == "extra-mount":
        mounts.append(
            {
                "Type": "bind",
                "Source": "/etc",
                "Destination": "/host-etc",
                "RW": False,
                "Propagation": "rprivate",
            }
        )
    elif weakening == "extra-label":
        container_config["Labels"]["attacker.extra"] = "true"
    elif weakening == "missing-scratch":
        host_config["Tmpfs"] = {}
    elif weakening == "different-runtime":
        host_config["Runtime"] = "youki"
    elif weakening == "host-cgroupns":
        host_config["CgroupnsMode"] = "host"
    else:
        host_config["MaskedPaths"] = []

    with pytest.raises(oci_runtime_module.OCIEngineError, match="enforcement|mount|device"):
        runtime._validate_engine_configuration(  # noqa: SLF001
            inspection,
            config=runtime.build_oci_configuration(request=request),
        )


@pytest.mark.parametrize("image_identity", ["config", "manifest"])
def test_engine_inspection_accepts_only_frozen_docker_image_identity_variants(
    tmp_path: Path,
    image_identity: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    config = runtime.build_oci_configuration(request=request)
    inspection = _exact_engine_inspection(runtime, request)
    inspection["Image"] = (
        f"sha256:{config.image_config_sha256}"
        if image_identity == "config"
        else f"sha256:{config.image_manifest_sha256}"
    )

    runtime._validate_engine_configuration(inspection, config=config)  # noqa: SLF001

    inspection["Image"] = f"sha256:{_digest('unfrozen-image')}"
    with pytest.raises(
        oci_runtime_module.OCIEngineError,
        match="outside the frozen image digests",
    ):
        runtime._validate_engine_configuration(inspection, config=config)  # noqa: SLF001


def test_engine_inspection_accepts_docker29_inline_seccomp_only_when_semantically_exact(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime._ensure_seccomp_copy()  # noqa: SLF001
    config = runtime.build_oci_configuration(request=request)
    inspection = _exact_engine_inspection(runtime, request)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    source = json.loads(Path(policy.seccomp_profile_path).read_bytes())
    inline = json.dumps(source, separators=(",", ":"), sort_keys=False)
    host_config["SecurityOpt"] = [
        "no-new-privileges=true",
        f"seccomp={inline}",
        f"apparmor={config.apparmor_profile}",
    ]

    runtime._validate_engine_configuration(inspection, config=config)  # noqa: SLF001


@pytest.mark.parametrize(
    "inline",
    [
        '{"defaultAction":"SCMP_ACT_ALLOW"}',
        '{"defaultAction":"SCMP_ACT_ERRNO","unknown":null}',
        '{"defaultAction":"SCMP_ACT_ERRNO","defaultAction":"SCMP_ACT_ERRNO"}',
        '{"defaultAction":NaN}',
    ],
)
def test_engine_inspection_rejects_nonexact_or_ambiguous_inline_seccomp(
    tmp_path: Path,
    inline: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime._ensure_seccomp_copy()  # noqa: SLF001
    config = runtime.build_oci_configuration(request=request)
    inspection = _exact_engine_inspection(runtime, request)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["SecurityOpt"] = [
        "no-new-privileges=true",
        f"seccomp={inline}",
        f"apparmor={config.apparmor_profile}",
    ]

    with pytest.raises(oci_runtime_module.OCIEngineError, match="seccomp|enforcement"):
        runtime._validate_engine_configuration(inspection, config=config)  # noqa: SLF001


def test_engine_capability_hash_excludes_dynamic_counts_but_requires_security(
    tmp_path: Path,
) -> None:
    _, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    info: dict[str, object] = {
        "Architecture": "x86_64",
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "DefaultRuntime": "runc",
        "DockerRootDir": "/var/lib/docker",
        "Driver": "overlay2",
        "ID": "daemon-id",
        "KernelVersion": "6.12.0",
        "Name": "qualification-node",
        "OperatingSystem": "Qualification Linux",
        "OSType": "linux",
        "Runtimes": {"runc": {"path": "runc"}},
        "SecurityOptions": ["name=seccomp,profile=builtin", "name=apparmor", "name=cgroupns"],
        "ServerVersion": "28.0.0",
        "Containers": 0,
        "SystemTime": "2026-08-24T12:00:00Z",
    }
    first = runtime._engine_security_projection(info)  # noqa: SLF001
    changed_dynamic = copy.deepcopy(info)
    changed_dynamic["Containers"] = 999
    changed_dynamic["SystemTime"] = "2026-08-24T13:00:00Z"
    second = runtime._engine_security_projection(changed_dynamic)  # noqa: SLF001

    assert canonical_sha256(first) == canonical_sha256(second)
    weakened = copy.deepcopy(info)
    weakened["SecurityOptions"] = ["name=cgroupns"]
    with pytest.raises(OCIProductionCapabilityError, match="seccomp/AppArmor"):
        runtime._engine_security_projection(weakened)  # noqa: SLF001


def test_prepare_is_inert_durable_and_byte_identical_on_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)

    def _no_engine(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("prepare contacted the OCI engine")

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _no_engine)
    preparation = runtime.prepare(request=request)
    replay = runtime.prepare(request=request)

    assert replay == preparation
    assert preparation.runtime_request_sha256 == request.runtime_request_sha256
    assert preparation.enforced_placement_sha256 == request.enforced_placement_sha256
    assert preparation.input_materialization_receipt_sha256 == (
        request.input_materialization_receipt.materialization_receipt_sha256
    )
    assert not hasattr(preparation, "runtime_identity")
    assert not hasattr(preparation, "started_at")
    root = _runtime_root(tmp_path)
    assert not (root / "launch-pending.json").exists()
    assert not (root / "launch-evidence.json").exists()
    for name in (
        "plan.json",
        "oci-config.json",
        "prepare-intent.json",
        "preparation.json",
        "prelaunch-absence.json",
    ):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o400
    assert stat.S_IMODE((root / "control" / "current.json").stat().st_mode) == 0o400


@pytest.mark.parametrize(
    "crash_point",
    ["publish-prelink", "publish-postlink", "replace-precommit"],
)
def test_oci_journal_temp_crash_residue_is_recovered_under_runtime_lock(
    tmp_path: Path,
    crash_point: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    root = _runtime_root(tmp_path)
    if crash_point == "publish-prelink":
        final = root / "plan.json"
        temporary = final.with_name(f".{final.name}.{'a' * 32}.tmp")
        temporary.write_bytes(b"interrupted")
        temporary.chmod(0o600)
    elif crash_point == "publish-postlink":
        final = root / "preparation.json"
        temporary = final.with_name(f".{final.name}.{'b' * 32}.tmp")
        os.link(final, temporary)
        assert final.stat().st_nlink == 2
    else:
        final = root / "control" / "current.json"
        temporary = final.with_name(f".{final.name}.{'c' * 32}.cas")
        temporary.write_bytes(final.read_bytes())
        temporary.chmod(0o400)

    replay = runtime.prepare(request=request)

    assert replay == preparation
    assert not temporary.exists()
    assert final.stat().st_nlink == 1


def test_crash_after_engine_launch_journal_reinspects_live_identity_with_fresh_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    config = runtime.build_oci_configuration(request=plan)
    root = _runtime_root(tmp_path)
    pending = oci_runtime_module._LaunchPending(  # noqa: SLF001
        runtime_request_sha256=plan.runtime_request_sha256,
        oci_config_sha256=config.oci_config_sha256,
        authorization_request_sha256=authorization_request.request_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        pending_at=preparation.prepared_at + timedelta(microseconds=1),
        pending_boottime_ns=preparation.prepared_monotonic_ns + 1_000,
    )
    runtime._publish_model(root / "launch-pending.json", pending)  # noqa: SLF001
    gate = runtime._ensure_launch_gate_authorization(  # noqa: SLF001
        runtime_root=root,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    capability = _capability(runtime, plan)
    runtime._publish_model(  # noqa: SLF001
        root / "production-capability.json",
        capability,
    )
    watchdog = runtime._ensure_deadline_watchdog(  # noqa: SLF001
        runtime_root=root,
        request=plan,
        preparation=preparation,
        config=config,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    create_submission, start_submission = _publish_submission_markers(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    container_id = "d" * 64
    started_at = preparation.prepared_at + timedelta(microseconds=2)
    started_monotonic_ns = preparation.prepared_monotonic_ns + 2_000
    workload_argv_sha256 = runtime._expected_workload_argv_sha256(plan)  # noqa: SLF001
    started_monotonic_upper_ns = started_monotonic_ns + 1_000
    process_scope = {
        "schema": "aletheia.linux_oci_process_identity.v2",
        "boot_id": plan.boot_id,
        "container_id": container_id,
        "pid": 4242,
        "proc_start_ticks": 1234,
        "pid_namespace_device": 9,
        "pid_namespace_inode": 10,
        "proc_cgroup_sha256": _digest("proc-cgroup"),
        "cgroup_limits_sha256": _digest("cgroup-limits"),
        "workload_executable_sha256": policy.executable_sha256,
        "workload_argv_sha256": workload_argv_sha256,
        "started_at": started_at.isoformat(),
        "started_monotonic_lower_bound_ns": started_monotonic_ns,
        "started_monotonic_upper_bound_exclusive_ns": started_monotonic_upper_ns,
    }
    engine_journal = oci_runtime_module._EngineLaunchJournal(  # noqa: SLF001
        preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        production_capability_sha256=capability.capability_sha256,
        launch_gate_authorization_journal_sha256=gate.journal_sha256,
        deadline_watchdog_journal_sha256=watchdog.journal_sha256,
        create_submission_journal_sha256=create_submission.journal_sha256,
        start_submission_journal_sha256=start_submission.journal_sha256,
        container_id=container_id,
        container_inspection_sha256=_digest("container-inspection"),
        sandbox_instance_sha256=canonical_sha256(
            {
                "schema": "aletheia.oci_sandbox_instance_identity.v2",
                "container_id": container_id,
                "image_config_sha256": policy.image_config_sha256,
                "oci_config_sha256": preparation.oci_config_sha256,
                "runtime_request_sha256": preparation.runtime_request_sha256,
            }
        ),
        process_identity_sha256=canonical_sha256(process_scope),
        pid=4242,
        proc_start_ticks=1234,
        pid_namespace_device=9,
        pid_namespace_inode=10,
        proc_cgroup_sha256=_digest("proc-cgroup"),
        cgroup_limits_sha256=_digest("cgroup-limits"),
        workload_executable_sha256=policy.executable_sha256,
        workload_argv_sha256=workload_argv_sha256,
        started_at=started_at,
        started_monotonic_lower_bound_ns=started_monotonic_ns,
        started_monotonic_upper_bound_exclusive_ns=started_monotonic_upper_ns,
        observed_at=started_at + timedelta(microseconds=1),
        observed_monotonic_ns=started_monotonic_upper_ns + 1_000,
    )
    runtime._publish_model(root / "engine-launch.json", engine_journal)  # noqa: SLF001
    assert not (root / "launch-evidence.json").exists()

    def _no_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("durable engine journal replay reminted capability evidence")

    monkeypatch.setattr(runtime, "probe_production_capability", _no_probe)
    fresh_at = engine_journal.observed_at + timedelta(minutes=5)
    fresh_monotonic_ns = engine_journal.observed_monotonic_ns + 300_000_000_000
    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: {"State": {"Running": True}},
    )
    monkeypatch.setattr(runtime, "_validate_engine_configuration", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_output_mount_generation_sha256", lambda request: H0)
    monkeypatch.setattr(
        runtime,
        "_launch_journal",
        lambda **kwargs: engine_journal.model_copy(
            update={
                "container_inspection_sha256": _digest("fresh-container-inspection"),
                "observed_at": fresh_at,
                "observed_monotonic_ns": fresh_monotonic_ns,
            }
        ),
    )
    evidence = runtime.ensure_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    replay = runtime.ensure_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert replay == evidence
    assert evidence.observed_at == fresh_at
    assert evidence.observed_monotonic_ns == fresh_monotonic_ns
    assert evidence.engine_launch_journal_sha256 == engine_journal.journal_sha256
    assert len(tuple((root / "launch-recovery").glob("observation-*.json"))) == 1

    later_at = fresh_at + timedelta(minutes=5)
    later_monotonic_ns = fresh_monotonic_ns + 300_000_000_000
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(
        runtime,
        "_launch_journal",
        lambda **kwargs: engine_journal.model_copy(
            update={
                "container_inspection_sha256": _digest("later-container-inspection"),
                "observed_at": later_at,
                "observed_monotonic_ns": later_monotonic_ns,
            }
        ),
    )
    recovered = runtime.recover_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert recovered is not None
    assert recovered.observed_at == later_at
    assert recovered.observed_monotonic_ns == later_monotonic_ns
    assert recovered.runtime_identity == evidence.runtime_identity
    assert recovered.evidence_sha256 != evidence.evidence_sha256
    assert (
        runtime._load_required(  # noqa: SLF001
            root / "launch-evidence.json", RuntimeLaunchEvidence
        )
        == evidence
    )
    assert len(tuple((root / "launch-recovery").glob("observation-*.json"))) == 2

    monkeypatch.setattr(
        runtime,
        "_launch_journal",
        lambda **kwargs: engine_journal.model_copy(
            update={
                "process_identity_sha256": _digest("reused-or-changed-process"),
                "observed_at": later_at + timedelta(minutes=1),
                "observed_monotonic_ns": later_monotonic_ns + 60_000_000_000,
            }
        ),
    )
    with pytest.raises(OCIJournalError, match="PID/workload/cgroup identity"):
        runtime.recover_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )

    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: {"State": {"Running": False, "Status": "exited"}},
    )
    assert (
        runtime.recover_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        is None
    )
    assert (
        runtime.cleanup_never_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        ).state
        is RuntimeInspectionState.UNKNOWN
    )


def test_post_start_pre_journal_crash_recovers_actual_start_from_historical_ticket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(
        preparation,
        max_launch_delay_ns=20_000,
    )
    plan = runtime._coerce_request(request)  # noqa: SLF001
    config = runtime.build_oci_configuration(request=plan)
    root = _runtime_root(tmp_path)
    pending = oci_runtime_module._LaunchPending(  # noqa: SLF001
        runtime_request_sha256=plan.runtime_request_sha256,
        oci_config_sha256=config.oci_config_sha256,
        authorization_request_sha256=authorization_request.request_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        pending_at=preparation.prepared_at + timedelta(microseconds=1),
        pending_boottime_ns=preparation.prepared_monotonic_ns + 1_000,
    )
    runtime._publish_model(root / "launch-pending.json", pending)  # noqa: SLF001
    runtime._ensure_launch_gate_authorization(  # noqa: SLF001
        runtime_root=root,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    capability = _capability(runtime, plan)
    runtime._publish_model(  # noqa: SLF001
        root / "production-capability.json",
        capability,
    )
    runtime._ensure_deadline_watchdog(  # noqa: SLF001
        runtime_root=root,
        request=plan,
        preparation=preparation,
        config=config,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    create_submission, start_submission = _publish_submission_markers(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    started_at = authorization.issued_at + timedelta(microseconds=1)
    started_monotonic_ns = authorization_request.requested_monotonic_ns + 1_000
    started_monotonic_upper_ns = started_monotonic_ns + 1_000
    journal = oci_runtime_module._EngineLaunchJournal(  # noqa: SLF001
        preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        production_capability_sha256=capability.capability_sha256,
        launch_gate_authorization_journal_sha256=runtime._load_required(  # noqa: SLF001
            root / "control" / "launch-authorization.json",
            oci_runtime_module._LaunchGateAuthorizationJournal,  # noqa: SLF001
        ).journal_sha256,
        deadline_watchdog_journal_sha256=runtime._load_required(  # noqa: SLF001
            root / "deadline-watchdog.json",
            oci_runtime_module._DeadlineWatchdogJournal,  # noqa: SLF001
        ).journal_sha256,
        create_submission_journal_sha256=create_submission.journal_sha256,
        start_submission_journal_sha256=start_submission.journal_sha256,
        container_id="d" * 64,
        container_inspection_sha256=_digest("recovered-container-inspection"),
        sandbox_instance_sha256=canonical_sha256(
            {
                "schema": "aletheia.oci_sandbox_instance_identity.v2",
                "container_id": "d" * 64,
                "image_config_sha256": policy.image_config_sha256,
                "oci_config_sha256": preparation.oci_config_sha256,
                "runtime_request_sha256": preparation.runtime_request_sha256,
            }
        ),
        process_identity_sha256=H0,
        pid=4242,
        proc_start_ticks=1234,
        pid_namespace_device=9,
        pid_namespace_inode=10,
        proc_cgroup_sha256=_digest("recovered-proc-cgroup"),
        cgroup_limits_sha256=_digest("recovered-cgroup-limits"),
        workload_executable_sha256=policy.executable_sha256,
        workload_argv_sha256=runtime._expected_workload_argv_sha256(plan),  # noqa: SLF001
        started_at=started_at,
        started_monotonic_lower_bound_ns=started_monotonic_ns,
        started_monotonic_upper_bound_exclusive_ns=started_monotonic_upper_ns,
        observed_at=authorization.expires_at + timedelta(seconds=1),
        observed_monotonic_ns=(
            authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns + 1_000
        ),
    )
    journal = journal.model_copy(
        update={
            "process_identity_sha256": runtime._expected_process_identity_sha256(  # noqa: SLF001
                plan, journal
            )
        }
    )
    clock.wall = authorization.expires_at + timedelta(seconds=1)
    clock.boottime = journal.observed_monotonic_ns
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: capability,
    )
    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: {"Id": "d" * 64, "State": {"Running": True}},
    )
    monkeypatch.setattr(runtime, "_validate_engine_configuration", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_launch_journal", lambda **kwargs: journal)

    evidence = runtime.ensure_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert evidence.runtime_launch_authorization_sha256 == authorization.authorization_sha256
    assert evidence.runtime_identity.started_at == started_at
    assert evidence.runtime_identity.started_monotonic_ns == started_monotonic_ns
    assert evidence.observed_at > authorization.expires_at
    assert (
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        == evidence
    )
    outside_window = journal.model_copy(
        update={
            "started_monotonic_lower_bound_ns": (
                authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns + 1
            ),
            "started_monotonic_upper_bound_exclusive_ns": (
                authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns + 2
            ),
        }
    )
    with pytest.raises(OCIProductionCapabilityError, match="outside its signed launch ticket"):
        runtime._verify_actual_launch_window(  # noqa: SLF001
            journal=outside_window,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


def test_same_kernel_tick_start_ambiguity_is_rejected_as_outside_signed_window(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    requested = authorization_request.requested_monotonic_ns

    with pytest.raises(OCIProductionCapabilityError, match="outside its signed launch ticket"):
        runtime._verify_actual_launch_window(  # type: ignore[arg-type]  # noqa: SLF001
            journal=SimpleNamespace(
                started_at=authorization.issued_at + timedelta(microseconds=1),
                started_monotonic_lower_bound_ns=requested - 1,
                started_monotonic_upper_bound_exclusive_ns=requested + 1,
            ),
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


def test_cleanup_before_any_engine_submission_is_exact_idempotent_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)
    plan, _ = _seed_pending_launch_generation(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=False,
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)

    evidence = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    clock.wall += timedelta(minutes=5)
    clock.boottime += 300_000_000_000
    replay = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert replay.state is RuntimeInspectionState.ABSENT
    assert replay.prelaunch_absence_journal_sha256 == evidence.prelaunch_absence_journal_sha256
    assert replay.inspected_at > evidence.inspected_at
    assert replay.inspected_monotonic_ns > evidence.inspected_monotonic_ns
    assert evidence.state is RuntimeInspectionState.ABSENT
    assert evidence.prelaunch_absence_epoch == 1
    assert evidence.prelaunch_authorization_request_sha256 == authorization_request.request_sha256
    assert evidence.prelaunch_authorization_sha256 == authorization.authorization_sha256
    completed = runtime._load_required(  # noqa: SLF001
        root / "cleanup" / "absence-1-completed.json",
        oci_runtime_module._NeverStartedCleanupCompleted,  # noqa: SLF001
    )
    assert completed.deleted_container_id is None
    assert completed.exact_engine_absence_sha256 == runtime._exact_engine_absence_sha256(  # noqa: SLF001
        runtime.build_oci_configuration(request=plan).container_name
    )


def test_cleanup_before_launch_journal_is_exact_idempotent_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)

    probe_calls = 0

    def _probe(*, request):  # type: ignore[no-untyped-def]
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            raise OCIProductionCapabilityError("synthetic prelaunch capability failure")
        return _capability(runtime, request)

    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        _probe,
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)

    with pytest.raises(OCIProductionCapabilityError, match="prelaunch capability failure"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    assert not (root / "launch-pending.json").exists()

    evidence = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    clock.wall += timedelta(minutes=5)
    clock.boottime += 300_000_000_000
    replay = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert evidence.state is RuntimeInspectionState.ABSENT
    assert replay.state is RuntimeInspectionState.ABSENT
    assert replay.prelaunch_absence_journal_sha256 == evidence.prelaunch_absence_journal_sha256
    pending = runtime._load_required(  # noqa: SLF001
        root / "cleanup" / "absence-1-pending.json",
        oci_runtime_module._NeverStartedCleanupPending,  # noqa: SLF001
    )
    assert pending.launch_pending is None
    assert pending.launch_gate_authorization is None
    assert pending.production_capability is None
    assert pending.deadline_watchdog is None
    assert pending.create_submission is None
    assert not (root / "launch-pending.json").exists()


def test_cleanup_exact_created_pid_zero_retires_watchdog_then_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    controller = _RecordingDeadlineWatchdogController()
    runtime = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "runtime-journal",
        clock=_Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
        launch_gate_verifier=_LaunchGateVerifier(),
        deadline_watchdog_controller=controller,
    )
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=True,
    )
    created = _created_engine_inspection(runtime, request)
    observations: list[dict[str, object] | None] = [created, None]
    deleted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: observations.pop(0))
    monkeypatch.setattr(runtime, "_remove_created_container", deleted.append)

    evidence = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert evidence.state is RuntimeInspectionState.ABSENT
    assert deleted == [created["Id"]]
    assert len(controller.retire_calls) == 1
    assert controller.retire_calls[0]["pre_runtime_absence_epoch"] == 0
    completed = runtime._load_required(  # noqa: SLF001
        root / "cleanup" / "absence-1-completed.json",
        oci_runtime_module._NeverStartedCleanupCompleted,  # noqa: SLF001
    )
    assert completed.deleted_container_id == created["Id"]
    assert completed.watchdog_retirement_evidence_sha256 is not None

    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)
    replay = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    assert replay.state is RuntimeInspectionState.ABSENT
    assert replay.prelaunch_absence_journal_sha256 == evidence.prelaunch_absence_journal_sha256
    assert replay.inspected_at > evidence.inspected_at
    assert deleted == [created["Id"]]


def test_cleanup_rejects_wrong_watchdog_retirement_before_container_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=_runtime_root(tmp_path),
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=True,
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: _created_engine_inspection(runtime, request),
    )
    monkeypatch.setattr(
        runtime,
        "_deadline_watchdog_controller",
        _WrongRetirementDeadlineWatchdogController(),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_created_container",
        lambda *args: pytest.fail("container deleted before watchdog retirement proof"),
    )

    with pytest.raises(OCIProductionCapabilityError, match="retirement evidence changed"):
        runtime.cleanup_never_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


def test_cleanup_rejects_fired_quiescence_for_wrong_container_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=_runtime_root(tmp_path),
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=True,
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: _created_engine_inspection(runtime, request),
    )
    monkeypatch.setattr(
        runtime,
        "_deadline_watchdog_controller",
        _WrongContainerDeadlineWatchdogController(),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_created_container",
        lambda *args: pytest.fail("container deleted before exact watchdog quiescence"),
    )

    with pytest.raises(
        OCIProductionCapabilityError,
        match="fired quiescence differs from never-started cleanup scope",
    ):
        runtime.cleanup_never_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


@pytest.mark.parametrize("crash_point", ["before-remove", "after-remove-before-completed"])
def test_cleanup_created_container_crash_windows_roll_forward_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=True,
    )
    created = _created_engine_inspection(runtime, request)
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    deleted: list[str] = []
    if crash_point == "before-remove":
        monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: created)

        def _crash_before_remove(container_id: str) -> None:
            del container_id
            raise RuntimeError("injected crash before engine remove")

        monkeypatch.setattr(runtime, "_remove_created_container", _crash_before_remove)
        with pytest.raises(RuntimeError, match="injected crash"):
            runtime.cleanup_never_started(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
        observations: list[dict[str, object] | None] = [created, None]
        monkeypatch.setattr(
            runtime,
            "_engine_inspect",
            lambda *args, **kwargs: observations.pop(0),
        )
        monkeypatch.setattr(runtime, "_remove_created_container", deleted.append)
    else:
        observations = [created, None]
        monkeypatch.setattr(
            runtime,
            "_engine_inspect",
            lambda *args, **kwargs: observations.pop(0),
        )
        monkeypatch.setattr(runtime, "_remove_created_container", deleted.append)
        original_publish = runtime._publish_model  # noqa: SLF001
        injected = False

        def _crash_before_completed(path: Path, value) -> None:  # type: ignore[no-untyped-def]
            nonlocal injected
            if path.name == "absence-1-completed.json" and not injected:
                injected = True
                raise RuntimeError("injected crash before completed cleanup journal")
            original_publish(path, value)

        monkeypatch.setattr(runtime, "_publish_model", _crash_before_completed)
        with pytest.raises(RuntimeError, match="injected crash"):
            runtime.cleanup_never_started(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
        monkeypatch.setattr(runtime, "_publish_model", original_publish)
        monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)

    evidence = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert evidence.state is RuntimeInspectionState.ABSENT
    assert deleted == [created["Id"]]
    assert (root / "cleanup" / "absence-1-pending.json").exists()
    assert (root / "cleanup" / "absence-1-completed.json").exists()


@pytest.mark.parametrize("engine_absent", [False, True])
def test_cleanup_never_claims_absence_after_start_submission_or_inflight_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine_absent: bool,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=True,
        start_submitted=not engine_absent,
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    if engine_absent:
        monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)
    else:
        monkeypatch.setattr(
            runtime,
            "_engine_inspect",
            lambda *args, **kwargs: pytest.fail("start-submitted cleanup inspected/deleted engine"),
        )
    monkeypatch.setattr(
        runtime,
        "_remove_created_container",
        lambda *args, **kwargs: pytest.fail("ambiguous daemon mutation was deleted"),
    )

    evidence = runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert evidence.state is RuntimeInspectionState.UNKNOWN
    assert not (root / "cleanup" / "absence-1-completed.json").exists()


def test_completed_cleanup_tombstone_rejects_fresh_nonce_at_stale_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)
    _seed_pending_launch_generation(
        runtime,
        runtime_root=root,
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=False,
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)
    runtime.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    stale_request, stale_authorization = _launch_authorization(
        preparation,
        nonce_label="fresh-stale-epoch-nonce",
    )
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda **kwargs: pytest.fail("stale generation reached capability probe"),
    )

    with pytest.raises(OCIJournalError, match="cannot be restarted"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=stale_request,
            authorization=stale_authorization,
        )


def test_cleanup_tombstone_created_during_probe_blocks_later_launch_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)

    def _probe(*, request):  # type: ignore[no-untyped-def]
        tombstone = root / "cleanup" / "absence-1-pending.json"
        tombstone.write_bytes(b"{}\n")
        tombstone.chmod(0o400)
        return _capability(runtime, request)

    monkeypatch.setattr(runtime, "probe_production_capability", _probe)
    with pytest.raises(OCIJournalError, match="under never-started cleanup"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    assert not (root / "engine-create-submitted.json").exists()


def test_prelaunch_inspection_is_exact_absence_not_started_identity(tmp_path: Path) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)

    inspection = runtime.inspect(request=request, preparation=preparation, identity=None)

    assert inspection.state is RuntimeInspectionState.ABSENT
    assert inspection.runtime_identity is None
    assert inspection.prelaunch_absence_journal_sha256 is not None
    assert inspection.engine_terminal_journal_sha256 is None
    assert inspection.enforced_fencing_epoch == request.fencing_epoch
    assert inspection.enforced_lease_token_sha256 == request.lease_token_sha256
    assert inspection.runtime_control_journal_sha256


def test_darwin_can_prepare_and_build_but_never_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "darwin")

    def _no_engine(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Darwin launch reached the OCI engine")

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _no_engine)
    with pytest.raises(OCIProductionCapabilityError, match="Linux"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    assert not (_runtime_root(tmp_path) / "launch-pending.json").exists()


def test_real_launch_requires_independent_immutable_gate_attestation(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    missing = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "missing-gate-journal",
        clock=_Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
    )
    wrong = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "wrong-gate-journal",
        clock=_Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
        launch_gate_verifier=_WrongLaunchGateVerifier(),
    )

    with pytest.raises(OCIProductionCapabilityError, match="launch-gate verifier"):
        missing._verify_launch_gate()  # noqa: SLF001
    with pytest.raises(OCIProductionCapabilityError, match="differs"):
        wrong._verify_launch_gate()  # noqa: SLF001
    assert request.spec.argv[0] != policy.launch_gate_path


@pytest.mark.parametrize("gate_still_running", [True, False])
def test_launch_evidence_requires_gate_to_exec_exact_pinned_workload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_still_running: bool,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    config = runtime.build_oci_configuration(request=plan)
    pid = 4242
    stat_fields = ["S", *("0" for _ in range(18)), "150"]
    proc_stat = f"{pid} (qualification) {' '.join(stat_fields)}\n"
    expected_argv = (
        (policy.launch_gate_path, *config.arguments) if gate_still_running else request.spec.argv
    )
    proc_cmdline = b"\x00".join(os.fsencode(item) for item in expected_argv) + b"\x00"
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_stat = Path.stat

    def _read_text(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == f"/proc/{pid}/stat":
            return proc_stat
        return original_read_text(path, *args, **kwargs)

    def _read_bytes(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == f"/proc/{pid}/cgroup":
            return b"0::/aletheia/qualification\n"
        if str(path) == f"/proc/{pid}/cmdline":
            return proc_cmdline
        return original_read_bytes(path, *args, **kwargs)

    def _stat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == f"/proc/{pid}/ns/pid":
            return original_stat(tmp_path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    monkeypatch.setattr(Path, "stat", _stat)
    monkeypatch.setattr(oci_runtime_module.os, "sysconf", lambda name: 100)
    monkeypatch.setattr(
        runtime,
        "_rehash_proc_executable",
        lambda observed_pid: policy.executable_sha256,
    )
    monkeypatch.setattr(
        runtime,
        "_verify_cgroup_v2_enforcement",
        lambda **scope: _digest("live-cgroup-limits"),
    )
    clock.boottime = 2_000_000_000
    started_at = preparation.prepared_at + timedelta(microseconds=1)
    inspection = {
        "Id": "f" * 64,
        "State": {
            "Running": True,
            "Pid": pid,
            "StartedAt": started_at.isoformat().replace("+00:00", "Z"),
        },
    }

    if gate_still_running:
        with pytest.raises(oci_runtime_module.OCIEngineError, match="has not execved"):
            runtime._launch_journal(  # noqa: SLF001
                request=plan,
                preparation=preparation,
                inspection=inspection,
                authorization_sha256=_digest("authorization"),
                production_capability_sha256=_digest("capability"),
                launch_gate_authorization_journal_sha256=_digest("gate-journal"),
                deadline_watchdog_journal_sha256=_digest("deadline-watchdog"),
                create_submission_journal_sha256=_digest("create-submission"),
                start_submission_journal_sha256=_digest("start-submission"),
            )
        return

    journal = runtime._launch_journal(  # noqa: SLF001
        request=plan,
        preparation=preparation,
        inspection=inspection,
        authorization_sha256=_digest("authorization"),
        production_capability_sha256=_digest("capability"),
        launch_gate_authorization_journal_sha256=_digest("gate-journal"),
        deadline_watchdog_journal_sha256=_digest("deadline-watchdog"),
        create_submission_journal_sha256=_digest("create-submission"),
        start_submission_journal_sha256=_digest("start-submission"),
    )
    assert journal.workload_executable_sha256 == policy.executable_sha256
    assert journal.workload_argv_sha256 == runtime._expected_workload_argv_sha256(  # noqa: SLF001
        plan
    )
    assert journal.process_identity_sha256 == runtime._expected_process_identity_sha256(  # noqa: SLF001
        plan,
        journal,
    )


@pytest.mark.parametrize(
    ("changed_control", "changed_value"),
    [
        ("cpu.max", "max 100000"),
        ("memory.max", "max"),
        ("memory.swap.max", "1048576"),
        ("pids.max", "max"),
    ],
)
def test_live_cgroup_v2_limits_are_exact_not_just_docker_claims(
    tmp_path: Path,
    changed_control: str,
    changed_value: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    container_id = "a" * 64
    relative = Path("system.slice") / f"docker-{container_id}.scope"
    leaf = tmp_path / "cgroup-v2" / relative
    leaf.mkdir(parents=True)
    exact_values = {
        "cpu.max": (
            f"{request.cpu_cores * policy.cpu_period_microseconds} {policy.cpu_period_microseconds}"
        ),
        "memory.max": str(request.memory_bytes),
        "memory.swap.max": "0",
        "pids.max": str(policy.pids_limit),
    }
    for name, value in exact_values.items():
        (leaf / name).write_text(f"{value}\n")
    proc_cgroup = f"0::/{relative.as_posix()}\n".encode()

    evidence_sha256 = runtime._verify_cgroup_v2_enforcement(  # noqa: SLF001
        request=plan,
        container_id=container_id,
        proc_cgroup=proc_cgroup,
        cgroup_root=tmp_path / "cgroup-v2",
    )
    assert len(evidence_sha256) == 64

    (leaf / changed_control).write_text(f"{changed_value}\n")
    with pytest.raises(oci_runtime_module.OCIEngineError, match="limits differ"):
        runtime._verify_cgroup_v2_enforcement(  # noqa: SLF001
            request=plan,
            container_id=container_id,
            proc_cgroup=proc_cgroup,
            cgroup_root=tmp_path / "cgroup-v2",
        )


def test_system_runtime_evidence_uses_suspend_aware_boottime_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_clock_ids: list[int] = []
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "linux")
    monkeypatch.setattr(oci_runtime_module.time, "CLOCK_BOOTTIME", 1234, raising=False)

    def _clock_gettime_ns(clock_id: int) -> int:
        observed_clock_ids.append(clock_id)
        return 9_000_000_000

    monkeypatch.setattr(oci_runtime_module.time, "clock_gettime_ns", _clock_gettime_ns)
    monkeypatch.setattr(
        oci_runtime_module.time,
        "monotonic_ns",
        lambda: pytest.fail("CLOCK_MONOTONIC crossed the BOOTTIME evidence boundary"),
    )
    clock = SystemOCIRuntimeClock()

    assert clock.monotonic_ns() == 9_000_000_000
    assert clock.boottime_ns() == 9_000_000_000
    assert observed_clock_ids == [1234, 1234]


def test_owner_only_workspace_rejects_mismatched_container_uid_gid(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    wrong_uid = 1 if os.geteuid() != 1 else 2
    changed_policy = DeploymentPinnedOCIPolicy.model_validate(
        policy.model_copy(update={"workload_uid": wrong_uid}).model_dump(mode="python")
    )
    runtime = _runtime(tmp_path, changed_policy)

    with pytest.raises(oci_runtime_module.OCIPolicyRejected, match="uid/gid"):
        runtime.prepare(request=request)


def test_input_tree_is_reopened_and_rehashed_at_engine_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request_with_materialized_file(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    input_path = request.input_root / "data.bin"
    input_path.chmod(0o600)
    input_path.write_bytes(b"attacker changed staged bytes\n")
    input_path.chmod(0o400)
    runtime._ensure_seccomp_copy()  # noqa: SLF001
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)

    def _no_mutation(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("changed staged input reached docker create")

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _no_mutation)
    with pytest.raises(oci_runtime_module.OCIPolicyRejected, match="input"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


def test_output_root_ownership_and_mode_are_rechecked_at_engine_mutation(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime.prepare(request=request)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    request.output_root.chmod(0o755)

    with pytest.raises(oci_runtime_module.OCIPolicyRejected, match="custody metadata"):
        runtime._validate_empty_output_tree(plan)  # noqa: SLF001


@pytest.mark.parametrize("payload", ["", "not-json", "{}"])
def test_optional_engine_inspection_never_treats_ambiguous_response_as_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    monkeypatch.setattr(
        runtime,
        "_invoke_engine",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=payload.encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(oci_runtime_module.OCIEngineError, match="inspect response"):
        runtime._engine_inspect("aletheia-q-exact", optional=True)  # noqa: SLF001

    monkeypatch.setattr(
        runtime,
        "_invoke_engine",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=b"[]\n",
            stderr=b"Error: No such object: aletheia-q-exact\n",
        ),
    )
    assert runtime._engine_inspect("aletheia-q-exact", optional=True) is None  # noqa: SLF001


def test_optional_engine_inspection_accepts_only_exact_pinned_docker29_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy = _request(tmp_path)
    docker29_template = "error: no such object: {container_name}\n"
    runtime = _runtime(
        tmp_path,
        policy.model_copy(update={"inspect_absence_stderr_template": docker29_template}),
    )
    response = {
        "value": subprocess.CompletedProcess(
            args=(),
            returncode=1,
            stdout=b"[]\n",
            stderr=b"error: no such object: aletheia-q-exact\n",
        )
    }
    monkeypatch.setattr(runtime, "_invoke_engine", lambda *args, **kwargs: response["value"])

    assert runtime._engine_inspect("aletheia-q-exact", optional=True) is None  # noqa: SLF001

    response["value"] = subprocess.CompletedProcess(
        args=(),
        returncode=1,
        stdout=b"[]\n",
        stderr=b"Error: No such object: aletheia-q-exact\n",
    )
    with pytest.raises(oci_runtime_module.OCIEngineError, match="absence response"):
        runtime._engine_inspect("aletheia-q-exact", optional=True)  # noqa: SLF001


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "error"),
    [
        (1, b"[]\n", b"transport unavailable\n", "absence response"),
        (
            1,
            b'[{"Id":"' + b"a" * 64 + b'"}]\n',
            b"Error: No such object: aletheia-q-exact\n",
            "absence response",
        ),
        (0, b"[]\n", b"", "exactly one container"),
        (0, b'[{"Id":"' + b"a" * 64 + b'"}]\n', b"warning\n", "unexpected stderr"),
    ],
)
def test_optional_engine_inspection_requires_exact_exit_stdout_stderr_cross_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    error: str,
) -> None:
    _, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    monkeypatch.setattr(
        runtime,
        "_invoke_engine",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises(oci_runtime_module.OCIEngineError, match=error):
        runtime._engine_inspect("aletheia-q-exact", optional=True)  # noqa: SLF001


def test_unverified_output_quota_cannot_reach_engine_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "runtime-journal",
        clock=_Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_WrongQuotaController(),
        launch_gate_verifier=_LaunchGateVerifier(),
        deadline_watchdog_controller=_DeadlineWatchdogController(),
    )
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    runtime._ensure_seccomp_copy()  # noqa: SLF001
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_engine_inspect", lambda *args, **kwargs: None)

    def _no_mutation(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("unverified quota reached docker create")

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _no_mutation)
    with pytest.raises(OCIProductionCapabilityError, match="quota differs"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )


@pytest.mark.parametrize("watchdog", [None, _WrongDeadlineWatchdogController()])
def test_missing_or_unverified_deadline_watchdog_cannot_reach_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    watchdog: object | None,
) -> None:
    request, policy = _request(tmp_path)
    runtime = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "runtime-journal",
        clock=_Clock(),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
        launch_gate_verifier=_LaunchGateVerifier(),
        deadline_watchdog_controller=watchdog,  # type: ignore[arg-type]
    )
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(
        oci_runtime_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unwatched workload reached docker create"),
    )

    with pytest.raises(OCIProductionCapabilityError, match="watchdog"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    assert not (_runtime_root(tmp_path) / "deadline-watchdog.json").exists()


def test_cpu_only_cut_rejects_device_launch_before_controller_or_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    device = ReservedDeviceBinding(
        device_id="device.gpu0",
        hardware_uuid="hardware.gpu0",
        fencing_epoch=request.fencing_epoch,
        requested_memory_bytes=256 * 1024 * 1024,
    )
    request = replace(
        request,
        selected_resource_ids=("cpu.0", "device.gpu0"),
        device_leases=(device,),
    )
    pin = OCIDevicePathPin(
        device_id=device.device_id,
        hardware_uuid=device.hardware_uuid,
        host_device_path="/dev/null",
        container_device_path="/dev/qualification-gpu0",
        device_policy_sha256=_digest("device-policy"),
    )
    controller = _RejectingInitialDeviceFenceController()
    runtime = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "runtime-journal",
        clock=_Clock(),
        device_fence_controller=controller,
        device_path_pins=(pin,),
        runtime_control_authority=RuntimeControlAuthorityVerifier(_control_pin()),
        output_quota_controller=_QuotaController(),
        launch_gate_verifier=_LaunchGateVerifier(),
        deadline_watchdog_controller=_DeadlineWatchdogController(),
    )
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *args, **kwargs: pytest.fail("device launch reached capability probe"),
    )

    def _no_mutation(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("unfenced device reached docker create")

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _no_mutation)
    with pytest.raises(OCIProductionCapabilityError, match="CPU-only"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
    assert controller.initial_calls == 0


def test_forged_launch_authorization_is_rejected_before_capability_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    forged = authorization.model_copy(update={"signature_ed25519_hex": "0" * 128})

    def _no_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("forged ticket reached the production capability probe")

    monkeypatch.setattr(runtime, "probe_production_capability", _no_probe)
    with pytest.raises(OCIProductionCapabilityError, match="historical ticket"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=forged,
        )
    assert not (_runtime_root(tmp_path) / "launch-pending.json").exists()


@pytest.mark.parametrize(
    ("expiry_point", "expected_mutations"),
    [("create", 0), ("start", 1)],
)
def test_suspend_aware_ticket_delay_is_checked_immediately_before_each_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_point: str,
    expected_mutations: int,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(
        preparation,
        max_launch_delay_ns=20_000,
    )
    runtime._ensure_seccomp_copy()  # noqa: SLF001 - isolate mutation-guard boundary
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_validate_engine_configuration", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_output_mount_generation_sha256", lambda request: H0)
    config = runtime.build_oci_configuration(request=request)
    created_id = "d" * 64
    inspections = 0

    def _inspection(name: str, *, optional: bool):  # type: ignore[no-untyped-def]
        nonlocal inspections
        assert name == config.container_name
        inspections += 1
        if inspections == 1:
            if expiry_point == "create":
                clock.boottime = (
                    authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns
                )
            return None
        assert optional is False
        if expiry_point == "start":
            clock.boottime = (
                authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns
            )
        return {
            "Id": created_id,
            "State": {"Running": False, "Status": "created"},
        }

    monkeypatch.setattr(runtime, "_engine_inspect", _inspection)
    mutations: list[tuple[str, ...]] = []

    def _subprocess_run(command, **kwargs):  # type: ignore[no-untyped-def]
        mutations.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{created_id}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _subprocess_run)

    with pytest.raises(OCIProductionCapabilityError, match="expired, delayed"):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )

    assert len(mutations) == expected_mutations
    if mutations:
        assert "create" in mutations[0]
    assert all("start" not in command for command in mutations)
    gate = runtime._load_required(  # noqa: SLF001
        _runtime_root(tmp_path) / "control" / "launch-authorization.json",
        oci_runtime_module._LaunchGateAuthorizationJournal,  # noqa: SLF001
    )
    assert gate.authorization_request == authorization_request
    assert gate.authorization == authorization
    assert gate.runtime_control_authority == _control_pin()
    assert gate.launch_gate_protocol_sha256 == policy.launch_gate_protocol_sha256
    watchdog = runtime._load_required(  # noqa: SLF001
        _runtime_root(tmp_path) / "deadline-watchdog.json",
        oci_runtime_module._DeadlineWatchdogJournal,  # noqa: SLF001
    )
    assert watchdog.preparation_sha256 == preparation.preparation_sha256
    assert watchdog.hard_deadline == request.deadline
    assert watchdog.hard_deadline_boottime_ns > preparation.prepared_monotonic_ns


@pytest.mark.parametrize(
    ("boundary", "failing_generation_check", "expected_engine_mutations"),
    [
        ("pre-create", 1, ()),
        ("post-create", 3, ("create", "rm")),
        ("pre-start", 4, ("create",)),
        ("post-start", 6, ("create", "start", "kill")),
    ],
)
def test_output_mount_generation_mismatch_brackets_create_start_and_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failing_generation_check: int,
    expected_engine_mutations: tuple[str, ...],
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    runtime._ensure_seccomp_copy()  # noqa: SLF001 - isolate daemon mutation boundaries
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime,
        "probe_production_capability",
        lambda *, request: _capability(runtime, request),
    )
    monkeypatch.setattr(runtime, "_validate_engine_configuration", lambda *args, **kwargs: None)

    generation_checks = 0

    def _generation(request):  # type: ignore[no-untyped-def]
        nonlocal generation_checks
        generation_checks += 1
        if generation_checks == failing_generation_check:
            raise OCIProductionCapabilityError(f"output mount generation changed at {boundary}")
        return H0

    monkeypatch.setattr(runtime, "_output_mount_generation_sha256", _generation)
    config = runtime.build_oci_configuration(request=request)
    container_id = "d" * 64
    engine_state: str | None = None

    def _inspection(name: str, *, optional: bool):  # type: ignore[no-untyped-def]
        del optional
        assert name in {config.container_name, container_id}
        if engine_state is None:
            return None
        return {
            "Id": container_id,
            "State": {
                "Running": engine_state == "running",
                "Status": engine_state,
                "Pid": 0 if engine_state == "created" else 4242,
            },
        }

    monkeypatch.setattr(runtime, "_engine_inspect", _inspection)
    mutations: list[str] = []

    def _subprocess_run(command, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal engine_state
        del kwargs
        operation = next(item for item in ("create", "start", "rm", "kill") if item in command)
        mutations.append(operation)
        if operation in {"create", "start"}:
            lock_descriptor = os.open(
                _runtime_root(tmp_path) / "engine-mutation.lock",
                os.O_RDWR,
            )
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock_descriptor)
        if operation == "create":
            assert engine_state is None
            engine_state = "created"
        elif operation == "start":
            assert engine_state == "created"
            engine_state = "running"
        elif operation == "rm":
            assert engine_state == "created"
            engine_state = None
        else:
            assert operation == "kill"
            assert engine_state == "running"
            engine_state = "exited"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{container_id}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(oci_runtime_module.subprocess, "run", _subprocess_run)

    with pytest.raises(OCIProductionCapabilityError, match=boundary):
        runtime.ensure_started(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )

    assert tuple(mutations) == expected_engine_mutations
    if boundary == "post-create":
        assert engine_state is None
    elif boundary == "post-start":
        assert engine_state == "exited"
    elif boundary == "pre-start":
        assert engine_state == "created"
        assert (_runtime_root(tmp_path) / "engine-create-submitted.json").is_file()
        assert not (_runtime_root(tmp_path) / "engine-start-submitted.json").exists()
    else:
        assert engine_state is None
        assert not (_runtime_root(tmp_path) / "engine-create-submitted.json").exists()


def test_fence_rebind_is_exact_replayable_and_accepts_post_adoption_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    identity = _seed_launch(runtime, tmp_path, preparation)
    monkeypatch.setattr(oci_runtime_module.sys, "platform", "darwin")
    before = runtime.inspect(request=request, preparation=preparation, identity=identity)
    rebind_request = RuntimeFenceRebindRequest(
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity_sha256=identity.runtime_identity_sha256,
        previous_fencing_epoch=1,
        previous_lease_token_sha256=request.lease_token_sha256,
        new_fencing_epoch=2,
        new_lease_token_sha256=_digest("lease-token-2"),
        rebind_sequence=1,
        expected_runtime_control_journal_sha256=before.runtime_control_journal_sha256,
        requested_at=NOW,
        requested_monotonic_ns=1_000_000_000,
    )

    evidence = runtime.rebind_fence(
        request=rebind_request,
        preparation=preparation,
        identity=identity,
    )
    replay = runtime.rebind_fence(
        request=rebind_request,
        preparation=preparation,
        identity=identity,
    )
    post_request = replace(
        request,
        fencing_epoch=2,
        lease_token_sha256=rebind_request.new_lease_token_sha256,
    )
    assert post_request.enforced_placement_sha256 == request.enforced_placement_sha256
    assert post_request.runtime_request_sha256 != request.runtime_request_sha256
    after = runtime.inspect(
        request=post_request,
        preparation=preparation,
        identity=identity,
    )

    assert replay == evidence
    assert evidence.previous_runtime_control_journal_sha256 == (
        before.runtime_control_journal_sha256
    )
    assert evidence.new_runtime_control_journal_sha256 == after.runtime_control_journal_sha256
    assert after.enforced_placement_sha256 == preparation.enforced_placement_sha256
    assert after.enforced_fencing_epoch == 2
    assert after.enforced_lease_token_sha256 == rebind_request.new_lease_token_sha256
    assert after.state is RuntimeInspectionState.UNKNOWN
    root = _runtime_root(tmp_path)
    for path in (
        root / "rebind" / "00000001.pending.json",
        root / "rebind" / "00000001.completed.json",
        root / "control" / "current.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o400

    conflict = rebind_request.model_copy(
        update={"new_lease_token_sha256": _digest("conflicting-token")}
    )
    with pytest.raises(OCIJournalError, match="another request"):
        runtime.rebind_fence(
            request=conflict,
            preparation=preparation,
            identity=identity,
        )


def test_caller_authored_terminal_event_cannot_mark_live_runtime_terminated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    identity = _seed_launch(runtime, tmp_path, preparation)

    monkeypatch.setattr(oci_runtime_module.sys, "platform", "darwin")
    assert not hasattr(oci_runtime_module, "OCIEngineTerminalObservation")
    assert not hasattr(runtime, "record_terminal_observation")
    observed = runtime.inspect(
        request=request,
        preparation=preparation,
        identity=identity,
    )

    assert observed.state is RuntimeInspectionState.UNKNOWN
    assert observed.exit_code is None
    assert observed.engine_terminal_journal_sha256 is None


def test_adapter_terminal_capture_rejects_live_second_reinspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    preparation = runtime.prepare(request=request)
    identity = _seed_launch(runtime, tmp_path, preparation)
    root = _runtime_root(tmp_path)
    launch = runtime._load_required(  # noqa: SLF001 - terminal compositor fault harness
        root / "launch-evidence.json",
        RuntimeLaunchEvidence,
    )
    engine_launch = oci_runtime_module._EngineLaunchJournal(  # noqa: SLF001
        preparation_sha256=preparation.preparation_sha256,
        runtime_launch_authorization_sha256=(launch.runtime_launch_authorization_sha256),
        production_capability_sha256=_digest("production-capability"),
        launch_gate_authorization_journal_sha256=_digest("launch-gate-journal"),
        deadline_watchdog_journal_sha256=_digest("deadline-watchdog"),
        create_submission_journal_sha256=_digest("create-submission"),
        start_submission_journal_sha256=_digest("start-submission"),
        container_id="c" * 64,
        container_inspection_sha256=_digest("container-inspection"),
        sandbox_instance_sha256=identity.sandbox_instance_sha256,
        process_identity_sha256=identity.process_identity_sha256,
        pid=2_147_000_000,
        proc_start_ticks=123,
        pid_namespace_device=1,
        pid_namespace_inode=1,
        proc_cgroup_sha256=_digest("proc-cgroup"),
        cgroup_limits_sha256=_digest("cgroup-limits"),
        workload_executable_sha256=policy.executable_sha256,
        workload_argv_sha256=_digest("workload-argv"),
        started_at=identity.started_at,
        started_monotonic_lower_bound_ns=identity.started_monotonic_ns,
        started_monotonic_upper_bound_exclusive_ns=(identity.started_monotonic_ns + 1),
        observed_at=launch.observed_at,
        observed_monotonic_ns=launch.observed_monotonic_ns,
    )
    launch = launch.model_copy(
        update={"engine_launch_journal_sha256": engine_launch.journal_sha256}
    )
    runtime._publish_model(root / "engine-launch.json", engine_launch)  # noqa: SLF001
    first_inspection = {
        "Id": engine_launch.container_id,
        "State": {
            "Running": False,
            "Status": "exited",
            "Pid": 0,
            "ExitCode": 7,
            "FinishedAt": "2026-08-24T12:00:02Z",
        },
    }
    second_inspection = {
        "Id": engine_launch.container_id,
        "State": {
            "Running": True,
            "Status": "running",
            "Pid": 42,
            "ExitCode": 0,
            "FinishedAt": "0001-01-01T00:00:00Z",
        },
    }
    monkeypatch.setattr(runtime, "_run_engine", lambda *args, **kwargs: "7\n")
    monkeypatch.setattr(
        runtime,
        "_engine_inspect",
        lambda *args, **kwargs: second_inspection,
    )
    monkeypatch.setattr(runtime, "_validate_engine_configuration", lambda *args, **kwargs: None)

    with pytest.raises(oci_runtime_module.OCIEngineError, match="live or different"):
        runtime._capture_terminal_from_engine(  # noqa: SLF001
            runtime_root=root,
            preparation=preparation,
            launch=launch,
            first_inspection=first_inspection,
        )

    assert not (root / "engine-terminal.json").exists()


def test_runtime_binary_and_seccomp_rename_replacement_fail_pinned_inode(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime.prepare(request=request)
    with runtime._pinned_runtime_binary():  # noqa: SLF001 - direct TOCTOU boundary QA
        pass
    runtime._ensure_seccomp_copy()  # noqa: SLF001 - direct TOCTOU boundary QA

    binary = Path(policy.runtime_binary_path)
    displaced_binary = binary.with_name("docker.displaced")
    binary.rename(displaced_binary)
    _deployment_file(binary, displaced_binary.read_bytes(), policy.runtime_binary_mode)
    with pytest.raises(OCIProductionCapabilityError, match="inode"):
        with runtime._pinned_runtime_binary():  # noqa: SLF001
            pass

    seccomp = Path(policy.seccomp_profile_path)
    displaced_seccomp = seccomp.with_name("seccomp.displaced.json")
    seccomp.rename(displaced_seccomp)
    _deployment_file(seccomp, displaced_seccomp.read_bytes(), policy.seccomp_profile_mode)
    second = LocalQualificationOCIRuntime(
        policy=policy,
        journal_root=tmp_path / "second-runtime-journal",
        clock=_Clock(),
    )
    with pytest.raises(OCIProductionCapabilityError, match="inode"):
        second._ensure_seccomp_copy()  # noqa: SLF001


@pytest.mark.parametrize("crash_point", ["prelink", "postlink"])
def test_seccomp_private_copy_recovers_exact_publish_crash_residue(
    tmp_path: Path,
    crash_point: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime.prepare(request=request)
    runtime._ensure_seccomp_copy()  # noqa: SLF001
    final = runtime._seccomp_copy_path  # noqa: SLF001
    parent = final.parent
    parent.chmod(0o700)
    temporary = final.with_name(f".{final.name}.{'e' * 32}.tmp")
    if crash_point == "prelink":
        temporary.write_bytes(b"interrupted seccomp copy")
        temporary.chmod(0o400)
    else:
        os.link(final, temporary)
        assert final.stat().st_nlink == 2
    parent.chmod(0o500)

    runtime._ensure_seccomp_copy()  # noqa: SLF001

    assert not temporary.exists()
    assert final.stat().st_nlink == 1
    assert hashlib.sha256(final.read_bytes()).hexdigest() == policy.seccomp_profile_sha256


def test_runtime_root_mkdir_crash_replays_parent_fsync_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    runtime_root = runtime._runtime_path(plan)  # noqa: SLF001

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "runtime-directory-created-before-parent-fsync" and path == runtime_root:
            raise _PowerLoss

    monkeypatch.setattr(oci_runtime_module, "_durable_runtime_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        runtime._ensure_runtime_directories(plan)  # noqa: SLF001
    assert runtime_root.is_dir()

    monkeypatch.setattr(
        oci_runtime_module,
        "_durable_runtime_checkpoint",
        lambda phase, path: None,
    )
    restarted = _runtime(tmp_path, policy)
    fsynced: list[Path] = []
    original_fsync_directory = restarted._fsync_directory  # noqa: SLF001

    def _record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(restarted, "_fsync_directory", _record_fsync)
    restarted._ensure_runtime_directories(plan)  # noqa: SLF001

    assert fsynced[0] == runtime_root.parent
    assert all(path.is_dir() for path in (runtime_root, runtime_root / "control"))


def test_runtime_publisher_fsyncs_0600_bytes_before_sealing_0400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    plan = runtime._coerce_request(request)  # noqa: SLF001
    runtime_root = runtime._ensure_runtime_directories(plan)  # noqa: SLF001
    events: list[tuple[str, int]] = []
    original_fsync = os.fsync
    original_fchmod = os.fchmod

    def _fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            events.append(("fsync", stat.S_IMODE(metadata.st_mode)))
        original_fsync(descriptor)

    def _fchmod(descriptor: int, mode: int) -> None:
        events.append(("fchmod", mode))
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(oci_runtime_module.os, "fsync", _fsync)
    monkeypatch.setattr(oci_runtime_module.os, "fchmod", _fchmod)
    runtime._publish_blob(runtime_root / "order.json", b"{}")  # noqa: SLF001

    assert events[:3] == [("fsync", 0o600), ("fchmod", 0o400), ("fsync", 0o400)]


def test_seccomp_directory_mkdir_crash_replays_parent_fsync_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    del request
    policy_root = runtime._seccomp_copy_path.parent  # noqa: SLF001

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "seccomp-directory-created-before-parent-fsync" and path == policy_root:
            raise _PowerLoss

    monkeypatch.setattr(oci_runtime_module, "_durable_runtime_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        runtime._ensure_seccomp_copy()  # noqa: SLF001
    assert policy_root.is_dir()

    monkeypatch.setattr(
        oci_runtime_module,
        "_durable_runtime_checkpoint",
        lambda phase, path: None,
    )
    restarted = _runtime(tmp_path, policy)
    fsynced: list[Path] = []
    original_fsync_directory = restarted._fsync_directory  # noqa: SLF001

    def _record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(restarted, "_fsync_directory", _record_fsync)
    restarted._ensure_seccomp_copy()  # noqa: SLF001

    assert policy_root.parent in fsynced
    assert restarted._seccomp_copy_path.is_file()  # noqa: SLF001


def test_launch_gate_journal_commit_return_replay_keeps_original_timestamped_bytes(
    tmp_path: Path,
) -> None:
    request, policy = _request(tmp_path)
    clock = _Clock()
    runtime = _runtime(tmp_path, policy, clock=clock)
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    root = _runtime_root(tmp_path)

    first = runtime._ensure_launch_gate_authorization(  # noqa: SLF001
        runtime_root=root,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    original_bytes = (root / "control" / "launch-authorization.json").read_bytes()
    clock.wall += timedelta(hours=1)
    clock.boottime += 3_600_000_000_000
    second = runtime._ensure_launch_gate_authorization(  # noqa: SLF001
        runtime_root=root,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )

    assert second == first
    assert (root / "control" / "launch-authorization.json").read_bytes() == original_bytes


def test_same_runtime_id_rejects_changed_request_scope(tmp_path: Path) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    runtime.prepare(request=request)
    changed = replace(request, memory_bytes=request.memory_bytes * 2)

    with pytest.raises(OCIJournalError, match="another OCI request"):
        runtime.prepare(request=changed)
