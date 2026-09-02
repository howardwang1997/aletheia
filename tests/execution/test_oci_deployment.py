from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import aletheia.execution.oci_deployment as oci_deployment_module
import aletheia.execution.oci_runtime as oci_runtime_module

from aletheia.execution.oci_deployment import (
    DurableDeadlineWatchdogService,
    ImmutableOCIImageLaunchGateVerifier,
    LoopbackOutputQuotaController,
    LoopbackOutputQuotaProvisionerClient,
    LoopbackOutputQuotaProvisioningService,
    OCIImageAttestationError,
    OCIOutputQuotaError,
    OCIWatchdogError,
    PinnedOCIImageLayout,
    PinnedRootFile,
    PinnedRootExecutable,
    PreinstalledOutputWorkspaceRootPin,
    SystemdDeadlineWatchdogController,
    SystemdWatchdogDeploymentPin,
    _QuotaFilesystemFormatted,
    _QuotaLoopAttachment,
    _QuotaProvisioningIntent,
    _WatchdogArmedRecord,
)
from aletheia.execution.oci_runtime import DeploymentPinnedOCIPolicy
from aletheia.execution.runtime_v2_contracts import (
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    OutputQuotaProvisioningReceipt,
)
from aletheia.execution.schemas import canonical_json_bytes, canonical_sha256

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_oci_runtime import (  # noqa: E402
    _Clock,
    _capability,
    _control_pin,
    _created_engine_inspection,
    _expired_gate_engine_inspection,
    _launch_authorization,
    _policy,
    _request,
    _runtime,
    _runtime_root,
    _seed_pending_launch_generation,
)

H0 = "0" * 64
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def test_loop_filesystem_uuid_is_read_directly_without_udev_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "loop.img"
    filesystem_uuid = uuid.UUID("fff9e109-169a-fd8f-8673-368d8603d25d")
    payload = bytearray(1024 + 0x78)
    payload[1024 + 0x38 : 1024 + 0x3A] = b"\x53\xef"
    payload[1024 + 0x68 : 1024 + 0x78] = filesystem_uuid.bytes
    image.write_bytes(payload)
    real_open = os.open

    def open_loop(path: str, flags: int) -> int:
        assert path == "/dev/loop22"
        return real_open(image, flags)

    monkeypatch.setattr(oci_deployment_module.os, "open", open_loop)
    monkeypatch.setattr(oci_deployment_module.stat, "S_ISBLK", lambda _mode: True)

    observed = LoopbackOutputQuotaController._filesystem_uuid_sha256(  # noqa: SLF001
        source="/dev/loop22",
        major=0,
        minor=0,
    )

    assert observed == hashlib.sha256(str(filesystem_uuid).encode("ascii")).hexdigest()


@pytest.mark.parametrize(
    ("magic", "filesystem_uuid", "message"),
    [
        (b"\x00\x00", uuid.UUID(int=1), "superblock"),
        (b"\x53\xef", uuid.UUID(int=0), "UUID is absent"),
    ],
)
def test_loop_filesystem_uuid_rejects_invalid_ext4_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    magic: bytes,
    filesystem_uuid: uuid.UUID,
    message: str,
) -> None:
    image = tmp_path / "loop.img"
    payload = bytearray(1024 + 0x78)
    payload[1024 + 0x38 : 1024 + 0x3A] = magic
    payload[1024 + 0x68 : 1024 + 0x78] = filesystem_uuid.bytes
    image.write_bytes(payload)
    real_open = os.open
    monkeypatch.setattr(
        oci_deployment_module.os,
        "open",
        lambda _path, flags: real_open(image, flags),
    )
    monkeypatch.setattr(oci_deployment_module.stat, "S_ISBLK", lambda _mode: True)

    with pytest.raises(OCIOutputQuotaError, match=message):
        LoopbackOutputQuotaController._filesystem_uuid_sha256(  # noqa: SLF001
            source="/dev/loop22",
            major=0,
            minor=0,
        )


def _tar_layer(entries: tuple[tuple[str, bytes | None, int, str], ...]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, content, mode, kind in entries:
            member = tarfile.TarInfo(name=name)
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            member.mode = mode
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "elsewhere"
                archive.addfile(member)
            else:
                assert content is not None
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


def _write_layout_blob(root: Path, payload: bytes) -> tuple[str, int]:
    digest = hashlib.sha256(payload).hexdigest()
    path = root / "blobs" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o400)
    return digest, len(payload)


def _image_fixture(
    tmp_path: Path,
    *,
    extra_layers: tuple[bytes, ...] = (),
    image_environment: tuple[tuple[str, str], ...] = (),
) -> tuple[
    DeploymentPinnedOCIPolicy,
    PinnedOCIImageLayout,
    dict[str, object],
    bytes,
]:
    layout_root = tmp_path / "layout"
    layout_root.mkdir(mode=0o500)
    layout_root.chmod(0o700)
    gate_bytes = b"reviewed-launch-gate-v1\n"
    gate_path = "opt/aletheia/bin/qualification-launch-gate"
    base_layer = _tar_layer(
        (
            ("opt", None, 0o755, "directory"),
            ("opt/aletheia", None, 0o755, "directory"),
            ("opt/aletheia/bin", None, 0o755, "directory"),
            (gate_path, gate_bytes, 0o500, "file"),
        )
    )
    layers = (base_layer, *extra_layers)
    layer_descriptors: list[dict[str, object]] = []
    diff_ids: list[str] = []
    for layer in layers:
        digest, size = _write_layout_blob(layout_root, layer)
        layer_descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": f"sha256:{digest}",
                "size": size,
            }
        )
        diff_ids.append(f"sha256:{digest}")
    config_payload = json.dumps(
        {
            "architecture": "amd64",
            "config": {"Env": [f"{name}={value}" for name, value in image_environment]},
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    config_sha256, config_size = _write_layout_blob(layout_root, config_payload)
    manifest_payload = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": f"sha256:{config_sha256}",
                "size": config_size,
            },
            "layers": layer_descriptors,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    manifest_sha256, manifest_size = _write_layout_blob(layout_root, manifest_payload)
    (layout_root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (layout_root / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": f"sha256:{manifest_sha256}",
                        "size": manifest_size,
                    }
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    for path in (layout_root / "oci-layout", layout_root / "index.json"):
        path.chmod(0o400)
    layout_root.chmod(0o500)
    base = _policy(tmp_path / "policy")
    policy_payload = base.model_dump(mode="python")
    policy_payload.update(
        {
            "image_reference": (f"registry.invalid/aletheia/qualifier@sha256:{manifest_sha256}"),
            "image_manifest_sha256": manifest_sha256,
            "image_config_sha256": config_sha256,
            "image_environment": [
                {"name": name, "value": value} for name, value in image_environment
            ],
            "launch_gate_executable_sha256": hashlib.sha256(gate_bytes).hexdigest(),
        }
    )
    policy = DeploymentPinnedOCIPolicy.model_validate(policy_payload)
    root_stat = layout_root.lstat()
    pin = PinnedOCIImageLayout(
        policy_sha256=policy.policy_sha256,
        layout_root=str(layout_root),
        layout_root_device=root_stat.st_dev,
        layout_root_inode=root_stat.st_ino,
        layout_root_mode=0o500,
        layout_parent_chain_sha256=H0,
        reviewed_launch_gate_executable_sha256=policy.launch_gate_executable_sha256,
        reviewed_launch_gate_protocol_sha256=policy.launch_gate_protocol_sha256,
    )
    docker = {
        "Id": f"sha256:{config_sha256}",
        "RepoDigests": [policy.image_reference],
        "RootFS": {"Type": "layers", "Layers": diff_ids},
        "Os": "linux",
        "Architecture": "amd64",
    }
    return policy, pin, docker, gate_bytes


def _verifier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: DeploymentPinnedOCIPolicy,
    pin: PinnedOCIImageLayout,
    docker: dict[str, object],
) -> ImmutableOCIImageLaunchGateVerifier:
    verifier = ImmutableOCIImageLaunchGateVerifier(
        policy=policy,
        runtime_control_authority=_control_pin(),
        image_layout=pin,
    )
    monkeypatch.setattr(verifier, "_require_linux_root_owned_layout", lambda: None)
    monkeypatch.setattr(verifier, "_docker_image_inspection", lambda: docker)
    monkeypatch.setattr(verifier, "_trusted_layout_owner_uid", lambda: os.geteuid())
    return verifier


def _verify_gate(verifier: ImmutableOCIImageLaunchGateVerifier) -> str:
    policy = verifier._policy  # noqa: SLF001
    expected = verifier._expected_evidence_sha256()  # noqa: SLF001
    return verifier.verify_immutable_launch_gate(
        image_reference=policy.image_reference,
        image_manifest_sha256=policy.image_manifest_sha256,
        image_config_sha256=policy.image_config_sha256,
        launch_gate_path=policy.launch_gate_path,
        launch_gate_executable_sha256=policy.launch_gate_executable_sha256,
        launch_gate_protocol_sha256=policy.launch_gate_protocol_sha256,
        expected_evidence_sha256=expected,
    )


def test_launch_gate_verifier_hashes_manifest_config_layers_and_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    assert _verify_gate(verifier) == verifier._expected_evidence_sha256()  # noqa: SLF001


def test_launch_gate_verifier_binds_pinned_image_environment_to_oci_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = (("LANG", "C.UTF-8"), ("PATH", "/usr/bin:/bin"))
    policy, pin, docker, _ = _image_fixture(
        tmp_path,
        image_environment=environment,
    )
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    assert _verify_gate(verifier) == verifier._expected_evidence_sha256()  # noqa: SLF001

    changed_payload = policy.model_dump(mode="python")
    changed_payload["image_environment"] = [
        {"name": "LANG", "value": "C.UTF-8"},
        {"name": "PATH", "value": "/usr/local/bin:/usr/bin:/bin"},
    ]
    changed_policy = DeploymentPinnedOCIPolicy.model_validate(changed_payload)
    changed_pin_payload = pin.model_dump(mode="python")
    changed_pin_payload["policy_sha256"] = changed_policy.policy_sha256
    changed_pin = PinnedOCIImageLayout.model_validate(changed_pin_payload)
    changed_verifier = _verifier(
        monkeypatch,
        policy=changed_policy,
        pin=changed_pin,
        docker=docker,
    )

    with pytest.raises(OCIImageAttestationError, match="environment differs"):
        _verify_gate(changed_verifier)


def test_launch_gate_verifier_accepts_docker_containerd_manifest_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    manifest_path = Path(pin.layout_root) / "blobs" / "sha256" / policy.image_manifest_sha256
    docker.update(
        {
            "Id": f"sha256:{policy.image_manifest_sha256}",
            "Descriptor": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{policy.image_manifest_sha256}",
                "size": manifest_path.stat().st_size,
            },
        }
    )
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    assert _verify_gate(verifier) == verifier._expected_evidence_sha256()  # noqa: SLF001


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("mediaType", "application/vnd.oci.image.config.v1+json"),
        ("digest", f"sha256:{H0}"),
        ("size", 1),
    ),
)
def test_launch_gate_verifier_rejects_tampered_docker_containerd_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    manifest_path = Path(pin.layout_root) / "blobs" / "sha256" / policy.image_manifest_sha256
    docker.update(
        {
            "Id": f"sha256:{policy.image_manifest_sha256}",
            "Descriptor": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{policy.image_manifest_sha256}",
                "size": manifest_path.stat().st_size,
            },
        }
    )
    descriptor = docker["Descriptor"]
    assert isinstance(descriptor, dict)
    descriptor[field] = value
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="Docker image differs"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_noncanonical_docker_containerd_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    manifest_path = Path(pin.layout_root) / "blobs" / "sha256" / policy.image_manifest_sha256
    docker.update(
        {
            "Id": f"sha256:{policy.image_manifest_sha256}",
            "Descriptor": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{policy.image_manifest_sha256}",
                "size": manifest_path.stat().st_size,
                "annotations": {},
            },
        }
    )
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="Docker image differs"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_mixed_containerd_descriptor_and_config_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    manifest_path = Path(pin.layout_root) / "blobs" / "sha256" / policy.image_manifest_sha256
    docker["Descriptor"] = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": f"sha256:{policy.image_manifest_sha256}",
        "size": manifest_path.stat().st_size,
    }
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="Docker image differs"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_docker_diff_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    docker["RootFS"] = {"Type": "layers", "Layers": [f"sha256:{H0}"]}
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="Docker image differs"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_whiteout_of_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whiteout = _tar_layer((("opt/aletheia/bin/.wh.qualification-launch-gate", b"", 0o000, "file"),))
    policy, pin, docker, _ = _image_fixture(tmp_path, extra_layers=(whiteout,))
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="does not contain"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_historical_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    symlink = _tar_layer((("opt/aletheia", None, 0o777, "symlink"),))
    policy, pin, docker, _ = _image_fixture(tmp_path, extra_layers=(symlink,))
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="ancestor"):
        _verify_gate(verifier)


def test_launch_gate_verifier_rejects_expected_hash_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, pin, docker, _ = _image_fixture(tmp_path)
    verifier = _verifier(monkeypatch, policy=policy, pin=pin, docker=docker)

    with pytest.raises(OCIImageAttestationError, match="challenge differs"):
        verifier.verify_immutable_launch_gate(
            image_reference=policy.image_reference,
            image_manifest_sha256=policy.image_manifest_sha256,
            image_config_sha256=policy.image_config_sha256,
            launch_gate_path=policy.launch_gate_path,
            launch_gate_executable_sha256=policy.launch_gate_executable_sha256,
            launch_gate_protocol_sha256=policy.launch_gate_protocol_sha256,
            expected_evidence_sha256=H0,
        )


def test_mountinfo_parser_requires_closed_linux_shape() -> None:
    parsed = LoopbackOutputQuotaController._parse_mountinfo(  # noqa: SLF001
        "41 32 7:4 / /srv/output rw,nosuid,nodev,noexec,relatime - ext4 /dev/loop4 rw"
    )
    assert parsed == {
        "mount_id": 41,
        "mount_parent_id": 32,
        "major": 7,
        "minor": 4,
        "mountpoint": "/srv/output",
        "mount_options": frozenset({"rw", "nosuid", "nodev", "noexec", "relatime"}),
        "fstype": "ext4",
        "source": "/dev/loop4",
        "super_options": frozenset({"rw"}),
    }
    with pytest.raises(OCIOutputQuotaError, match="unparseable"):
        LoopbackOutputQuotaController._parse_mountinfo("malformed")  # noqa: SLF001


def test_preinstalled_workspace_resolves_kernel_mount_id_only_after_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PreinstalledOutputWorkspaceRootPin(
        path="/srv/output",
        device=7,
        inode=8,
        owner_gid=2101,
        parent_chain_sha256=H0,
    )
    metadata = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o1730,
        st_uid=0,
        st_gid=2101,
        st_dev=7,
        st_ino=8,
    )
    mountinfo = "77 1 7:8 / /srv/output rw shared:99 - ext4 /dev/root rw\n"
    monkeypatch.setattr(Path, "resolve", lambda self, strict=True: self)
    monkeypatch.setattr(oci_deployment_module.os, "open", lambda *args, **kwargs: 41)
    monkeypatch.setattr(oci_deployment_module.os, "fstat", lambda descriptor: metadata)
    monkeypatch.setattr(oci_deployment_module.os, "close", lambda descriptor: None)
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: mountinfo)
    monkeypatch.setattr(oci_deployment_module, "host_parent_chain_sha256", lambda path: H0)

    observed = oci_deployment_module._observe_live_output_workspace_root(expected)
    assert observed.mount_id == 77
    assert "mount_id" not in type(expected).model_fields

    mountinfo = "77 1 7:8 / /srv/output rw - ext4 /dev/root rw\n"
    with pytest.raises(OCIOutputQuotaError, match="pre-install custody"):
        oci_deployment_module._observe_live_output_workspace_root(expected)


def test_root_service_health_attestations_bind_peer_boot_and_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota = object.__new__(LoopbackOutputQuotaProvisionerClient)
    quota._deployment = SimpleNamespace(deployment_sha256=H0)  # noqa: SLF001
    quota_response = {
        "schema": "aletheia.loopback_output_quota_health_response.v1",
        "deployment_sha256": H0,
        "service_pid": 123,
        "service_boot_id": "boot.health",
        "managed_by_systemd": True,
    }
    monkeypatch.setattr(quota, "_request", lambda request: (quota_response, 123))
    monkeypatch.setattr(quota, "_current_boot_id", lambda: "boot.health")
    assert quota.verify_service_health() == canonical_sha256(quota_response)

    policy = _policy(tmp_path / "policy")
    watchdog = SystemdDeadlineWatchdogController(
        policy=policy,
        deployment=_watchdog_deployment(tmp_path, policy),
    )
    watchdog_response = json.loads(
        canonical_json_bytes(
            {
                "schema": "aletheia.systemd_oci_watchdog_response.v1",
                "operation": "health",
                "deployment_sha256": watchdog._deployment.deployment_sha256,  # noqa: SLF001
                "service_pid": 456,
                "service_boot_id": "boot.health",
                "managed_by_systemd": True,
                "evidence_sha256": None,
                "job_sha256": None,
                "terminal_decision": None,
                "cleanup_quiescence_record_sha256": None,
                "cleanup_container_id": None,
            }
        )
    )
    assert "evidence_sha256" not in watchdog_response
    assert "terminal_decision" not in watchdog_response
    monkeypatch.setattr(watchdog, "_request", lambda request: watchdog_response)
    monkeypatch.setattr(watchdog, "_current_boot_id", lambda: "boot.health")
    assert watchdog.verify_service_health() == canonical_sha256(watchdog_response)

    quota_response["service_pid"] = 999
    with pytest.raises(OCIOutputQuotaError, match="health differs"):
        quota.verify_service_health()


def test_quota_controller_constructor_requires_private_pinned_journal(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy")
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o755)
    backing = tmp_path / "backing"
    backing.mkdir()

    with pytest.raises(OCIOutputQuotaError, match="journal root custody"):
        LoopbackOutputQuotaController(
            policy=policy,
            journal_root=journal,
            backing_root=backing,
        )


def _watchdog_deployment(
    tmp_path: Path, policy: DeploymentPinnedOCIPolicy
) -> SystemdWatchdogDeploymentPin:
    unit_name = "aletheia-qualification-oci-watchdog-test.service"
    return SystemdWatchdogDeploymentPin(
        deployment_id="watchdog.test",
        policy_sha256=policy.policy_sha256,
        systemd_unit_name=unit_name,
        service_executable=PinnedRootExecutable(
            path=policy.runtime_binary_path,
            sha256=policy.runtime_binary_sha256,
            device=policy.runtime_binary_device,
            inode=policy.runtime_binary_inode,
            mode=policy.runtime_binary_mode,
            parent_chain_sha256=policy.runtime_binary_parent_chain_sha256,
        ),
        service_module_sha256=H0,
        service_module_device=1,
        service_module_inode=1,
        service_module_mode=0o400,
        service_module_parent_chain_sha256=H0,
        journal_root=str(tmp_path / "journal"),
        journal_root_device=1,
        journal_root_inode=1,
        journal_root_parent_chain_sha256=H0,
        state_root=str(tmp_path / "watchdog-state"),
        state_root_device=1,
        state_root_inode=1,
        state_root_parent_chain_sha256=H0,
        socket_path=str(tmp_path / "run" / "watchdog.sock"),
        socket_parent_device=1,
        socket_parent_inode=1,
        socket_parent_parent_chain_sha256=H0,
        allowed_client_uid=os.geteuid(),
        allowed_client_gid=os.getegid(),
    )


def _overdue_armed(
    deployment: SystemdWatchdogDeploymentPin,
    *,
    boot_id: str = "12345678-1234-1234-1234-123456789abc",
) -> _WatchdogArmedRecord:
    overdue = datetime.now(timezone.utc) - timedelta(seconds=1)
    armed = _WatchdogArmedRecord(
        deployment_sha256=deployment.deployment_sha256,
        preparation_sha256="1" * 64,
        boot_id=boot_id,
        runtime_id="runtime.test",
        container_name="aletheia-q-test",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=overdue,
        hard_deadline_boottime_ns=0,
        expected_evidence_sha256="4" * 64,
        container_labels=(("aletheia.runtime_id", "runtime.test"),),
        armed_at=overdue - timedelta(minutes=1),
        service_boot_id=boot_id,
    )
    _ensure_test_runtime_generation_lock(deployment, armed.runtime_id)
    return armed


def _ensure_test_runtime_generation_lock(
    deployment: SystemdWatchdogDeploymentPin,
    runtime_id: str,
) -> None:
    runtime_key = hashlib.sha256(
        b"ALETHEIA_QUALIFICATION_OCI_RUNTIME_V2\x00" + runtime_id.encode()
    ).hexdigest()
    root = Path(deployment.journal_root) / runtime_key
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    lock = root / "engine-mutation.lock"
    lock.touch(mode=0o600, exist_ok=True)
    lock.chmod(0o600)


def test_watchdog_service_requires_root_systemd_supervision(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)

    with pytest.raises(OCIWatchdogError, match="root on Linux|systemd supervision"):
        service._require_root_systemd_service()  # noqa: SLF001


def test_watchdog_explicit_service_module_pin_preserves_durable_deployment_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    module_path = Path(oci_deployment_module.__file__).resolve()
    active_module = PinnedRootFile(
        path=str(module_path),
        sha256="9" * 64,
        device=91,
        inode=92,
        mode=0o444,
        parent_chain_sha256="8" * 64,
    )
    service = DurableDeadlineWatchdogService(
        policy=policy,
        deployment=deployment,
        service_module_pin=active_module,
    )
    verified: list[PinnedRootFile] = []

    def read_text(path: Path, *, encoding: str) -> str:
        assert encoding == "ascii"
        return {
            "/proc/1/comm": "systemd\n",
            "/proc/self/cgroup": "0::/system.slice/watchdog.service\n",
            "/proc/self/status": "Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\n",
        }[str(path)]

    monkeypatch.setattr(oci_deployment_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(oci_deployment_module.os, "getegid", lambda: 0)
    monkeypatch.setattr(oci_deployment_module.sys, "platform", "linux")
    monkeypatch.setattr(oci_deployment_module.Path, "read_text", read_text)
    monkeypatch.setattr(oci_deployment_module, "_in_exact_systemd_unit", lambda *_args: True)
    monkeypatch.setattr(
        oci_deployment_module,
        "_verify_root_file_pin",
        lambda pin, **_scope: verified.append(pin),
    )
    monkeypatch.setattr(
        oci_deployment_module,
        "_verify_root_process_executable",
        lambda *_args, **_scope: None,
    )
    monkeypatch.setattr(service, "_verify_deployment_roots", lambda: None)
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)

    service._require_root_systemd_service()  # noqa: SLF001

    assert verified == [active_module]
    assert service._deployment.deployment_sha256 == deployment.deployment_sha256  # noqa: SLF001
    assert service._service_module_pin == active_module  # noqa: SLF001


def test_watchdog_rejects_mutable_service_module_upgrade_pin(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    with pytest.raises(ValueError, match="implementation"):
        DurableDeadlineWatchdogService(
            policy=policy,
            deployment=deployment,
            service_module_pin=PinnedRootFile(
                path=str(Path(oci_deployment_module.__file__).resolve()),
                sha256="9" * 64,
                device=91,
                inode=92,
                mode=0o600,
                parent_chain_sha256="8" * 64,
            ),
        )


def test_watchdog_recovery_kills_only_exact_labelled_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    state_root = Path(deployment.state_root)
    state_root.mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    boot_id = "12345678-1234-1234-1234-123456789abc"
    overdue = datetime.now(timezone.utc) - timedelta(seconds=1)
    armed = _WatchdogArmedRecord(
        deployment_sha256=deployment.deployment_sha256,
        preparation_sha256="1" * 64,
        boot_id=boot_id,
        runtime_id="runtime.test",
        container_name="aletheia-q-test",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=overdue,
        hard_deadline_boottime_ns=0,
        expected_evidence_sha256="4" * 64,
        container_labels=(("aletheia.runtime_id", "runtime.test"),),
        armed_at=overdue - timedelta(minutes=1),
        service_boot_id=boot_id,
    )
    _ensure_test_runtime_generation_lock(deployment, armed.runtime_id)
    monkeypatch.setattr(service, "_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(service, "_inspect_container", lambda identifier: None)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001

    assert service.recover_due_jobs() == 1
    assert service._load_terminal(armed) is None  # noqa: SLF001
    assert service.recover_due_jobs() == 1


def test_watchdog_recovery_rejects_container_name_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    state_root = Path(deployment.state_root)
    state_root.mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    boot_id = "12345678-1234-1234-1234-123456789abc"
    overdue = datetime.now(timezone.utc) - timedelta(seconds=1)
    armed = _WatchdogArmedRecord(
        deployment_sha256=deployment.deployment_sha256,
        preparation_sha256="1" * 64,
        boot_id=boot_id,
        runtime_id="runtime.test",
        container_name="aletheia-q-test",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=overdue,
        hard_deadline_boottime_ns=0,
        expected_evidence_sha256="4" * 64,
        container_labels=(("aletheia.runtime_id", "runtime.test"),),
        armed_at=overdue - timedelta(minutes=1),
        service_boot_id=boot_id,
    )
    _ensure_test_runtime_generation_lock(deployment, armed.runtime_id)
    inspection = {
        "Id": "a" * 64,
        "Name": "/aletheia-q-test",
        "Config": {"Labels": {"aletheia.runtime_id": "attacker"}},
        "State": {"Running": True},
    }
    monkeypatch.setattr(service, "_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(service, "_inspect_container", lambda identifier: inspection)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001

    with pytest.raises(OCIWatchdogError, match="differs from exact watchdog custody"):
        service.recover_due_jobs()
    assert service._load_terminal(armed) is None  # noqa: SLF001


@pytest.mark.parametrize(
    "crash_phase",
    ["pending-written-before-mode", "pending-fsynced", "commit-return"],
)
def test_watchdog_arm_replay_returns_the_exact_old_time_varying_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    boot_id = "12345678-1234-1234-1234-123456789abc"
    wall = {"now": NOW}

    class _AdvancingDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            del tz
            return wall["now"]

    def _arm_record(**scope: object) -> _WatchdogArmedRecord:
        return _WatchdogArmedRecord(
            **scope,
            deployment_sha256=deployment.deployment_sha256,
            container_labels=(),
        )

    monkeypatch.setattr(oci_deployment_module, "datetime", _AdvancingDateTime)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_current_boot_id", lambda: boot_id)
    monkeypatch.setattr(service, "_deadline_reached", lambda armed, now: False)
    monkeypatch.setattr(service._scope, "arm_record", _arm_record)  # noqa: SLF001
    request = {
        "preparation_sha256": "1" * 64,
        "boot_id": boot_id,
        "runtime_id": "runtime.replay",
        "container_name": "container.replay",
        "engine_endpoint": "unix:///var/run/docker.sock",
        "authorization_request_sha256": "2" * 64,
        "runtime_launch_authorization_sha256": "3" * 64,
        "pre_runtime_absence_epoch": 0,
        "hard_deadline": (NOW + timedelta(minutes=5)).isoformat(),
        "hard_deadline_boottime_ns": 1,
        "expected_evidence_sha256": "4" * 64,
    }
    original_job_sha256: str

    if crash_phase == "commit-return":
        first = service.arm(request)
        original_job_sha256 = str(first["job_sha256"])
    else:

        class _PowerLoss(BaseException):
            pass

        def _crash(phase: str, path: Path) -> None:
            if phase == crash_phase:
                raise _PowerLoss

        monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
        with pytest.raises(_PowerLoss):
            service.arm(request)
        path = service._armed_scope_path(  # noqa: SLF001
            "runtime.replay", "2" * 64
        )
        pending = path.with_name(f".{path.name}.pending")
        old = _WatchdogArmedRecord.model_validate_json(pending.read_bytes())
        original_job_sha256 = old.job_sha256

    wall["now"] = NOW + timedelta(minutes=1)
    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    replay = service.arm(request)
    replay_again = service.arm(request)

    assert replay["job_sha256"] == original_job_sha256
    assert replay_again["job_sha256"] == replay["job_sha256"]


def test_watchdog_kill_completion_recovers_without_reclassifying_running_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    armed = _overdue_armed(deployment)
    container_id = "a" * 64
    cgroup_path = f"/system.slice/docker-{container_id}.scope"
    cgroup_identity = "6" * 64
    inspections = 0
    kills = 0

    def _inspect(identifier: str) -> dict[str, object]:
        nonlocal inspections
        inspections += 1
        assert identifier == armed.container_name
        return {
            "Id": container_id,
            "Name": f"/{armed.container_name}",
            "Config": {"Labels": dict(armed.container_labels)},
            "State": {"Running": True, "Pid": 4321},
        }

    def _kill(**scope: object) -> tuple[str, str]:
        nonlocal kills
        kills += 1
        assert scope == {
            "container_id": container_id,
            "init_pid": 4321,
            "cgroup_path": cgroup_path,
            "expected_identity_sha256": cgroup_identity,
        }
        return cgroup_path, cgroup_identity

    monkeypatch.setattr(service, "_current_boot_id", lambda: armed.boot_id)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_inspect_container", _inspect)
    monkeypatch.setattr(
        service,
        "_resolve_cgroup_identity",
        lambda **scope: (cgroup_path, cgroup_identity),
    )
    monkeypatch.setattr(service, "_kill_exact_cgroup", _kill)
    service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "watchdog-cgroup-killed-before-completed":
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service.recover_due_jobs()
    assert inspections == 1
    assert kills == 1
    assert service._recover_firing_intent(armed) is not None  # noqa: SLF001
    assert service._load_terminal(armed) is None  # noqa: SLF001

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    assert service.recover_due_jobs() == 1
    terminal = service._load_terminal(armed)  # noqa: SLF001
    assert terminal is not None
    assert terminal.status == "fired"
    assert terminal.container_was_running is True
    assert terminal.container_id == container_id
    assert terminal.cgroup_path == cgroup_path
    assert terminal.cgroup_identity_sha256 == cgroup_identity
    assert terminal.cgroup_empty is True
    assert inspections == 1
    assert kills == 2


def test_watchdog_firing_intent_is_durable_before_cgroup_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    armed = _overdue_armed(deployment)
    container_id = "b" * 64
    cgroup_path = f"/system.slice/docker-{container_id}.scope"
    cgroup_identity = "6" * 64
    kills = 0
    monkeypatch.setattr(service, "_current_boot_id", lambda: armed.boot_id)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(
        service,
        "_inspect_container",
        lambda identifier: {
            "Id": container_id,
            "Name": f"/{armed.container_name}",
            "Config": {"Labels": dict(armed.container_labels)},
            "State": {"Running": True, "Pid": 5432},
        },
    )
    monkeypatch.setattr(
        service,
        "_resolve_cgroup_identity",
        lambda **scope: (cgroup_path, cgroup_identity),
    )

    def _kill(**scope: object) -> tuple[str, str]:
        nonlocal kills
        kills += 1
        return cgroup_path, cgroup_identity

    monkeypatch.setattr(service, "_kill_exact_cgroup", _kill)
    service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "pending-fsynced" and path == service._firing_intent_path(armed):  # noqa: SLF001
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service.recover_due_jobs()
    assert kills == 0
    assert service._load_terminal(armed) is None  # noqa: SLF001

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    assert service.recover_due_jobs() == 1
    assert kills == 1
    terminal = service._load_terminal(armed)  # noqa: SLF001
    assert terminal is not None and terminal.container_was_running is True


def test_sealed_retirement_pending_wins_deadline_race_before_any_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    armed = _overdue_armed(deployment)
    retirement = "7" * 64
    monkeypatch.setattr(service, "_current_boot_id", lambda: armed.boot_id)
    monkeypatch.setattr(service, "_deadline_reached", lambda armed, now: False)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(
        service._scope,  # noqa: SLF001
        "retirement_evidence",
        lambda **scope: retirement,
    )
    service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001
    request = {
        "preparation_sha256": armed.preparation_sha256,
        "runtime_id": armed.runtime_id,
        "container_name": armed.container_name,
        "authorization_request_sha256": armed.authorization_request_sha256,
        "runtime_launch_authorization_sha256": armed.runtime_launch_authorization_sha256,
        "pre_runtime_absence_epoch": armed.pre_runtime_absence_epoch,
        "watchdog_journal_sha256": "8" * 64,
        "expected_evidence_sha256": retirement,
    }

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "pending-fsynced" and path == service._terminal_path(armed):  # noqa: SLF001
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service.retire(request)
    terminal_path = service._terminal_path(armed)  # noqa: SLF001
    assert not terminal_path.exists()
    assert terminal_path.with_name(f".{terminal_path.name}.pending").is_file()

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    monkeypatch.setattr(
        service,
        "_inspect_container",
        lambda identifier: pytest.fail("retired pending was not recovered before inspection"),
    )
    monkeypatch.setattr(
        service,
        "_kill_exact_cgroup",
        lambda **scope: pytest.fail("retired pending was not recovered before kill"),
    )
    assert service.recover_due_jobs() == 0
    terminal = service._load_terminal(armed)  # noqa: SLF001
    assert terminal is not None and terminal.status == "retired"
    assert terminal.retirement_evidence_sha256 == retirement


@pytest.mark.parametrize(
    "watchdog_observation",
    [
        "absent",
        "blocked_create",
        "created",
        "expired_gate",
        "expired_gate_config_drift",
        "expired_gate_timestamp_drift",
    ],
)
def test_fired_preworkload_watchdog_validates_exact_quiescence_for_cold_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    watchdog_observation: str,
) -> None:
    request, policy = _request(tmp_path)
    runtime = _runtime(tmp_path, policy)
    deployment_payload = _watchdog_deployment(tmp_path, policy).model_dump(mode="python")
    deployment_payload["journal_root"] = str(tmp_path / "runtime-journal")
    deployment = SystemdWatchdogDeploymentPin.model_validate(deployment_payload)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    controller = SystemdDeadlineWatchdogController(policy=policy, deployment=deployment)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_current_boot_id", lambda: request.boot_id)
    monkeypatch.setattr(service, "_deadline_reached", lambda armed, now: False)

    def _direct_request(raw: dict[str, object]) -> dict[str, object]:
        payload = dict(raw)
        operation = payload.pop("operation")
        if operation == "arm":
            return json.loads(canonical_json_bytes(service.arm(payload)))
        if operation == "retire":
            return json.loads(canonical_json_bytes(service.retire(payload)))
        raise AssertionError(f"unexpected watchdog operation: {operation}")

    monkeypatch.setattr(controller, "_request", _direct_request)
    runtime._deadline_watchdog_controller = controller  # noqa: SLF001
    preparation = runtime.prepare(request=request)
    authorization_request, authorization = _launch_authorization(preparation)
    created = _created_engine_inspection(runtime, request)
    expired_gate = _expired_gate_engine_inspection(runtime, request, authorization)
    config = runtime.build_oci_configuration(request=request)
    created["Name"] = f"/{config.container_name}"
    expired_gate["Name"] = f"/{config.container_name}"
    create_submitted = watchdog_observation != "absent"
    start_submitted = watchdog_observation.startswith("expired_gate")
    _seed_pending_launch_generation(
        runtime,
        runtime_root=_runtime_root(tmp_path),
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
        preflight=True,
        create_submitted=create_submitted,
        start_submitted=start_submitted,
    )
    armed = service._load_armed(  # noqa: SLF001
        preparation.runtime_id,
        authorization_request.request_sha256,
    )
    observed_container = expired_gate if start_submitted else created
    initial_inspection = (
        observed_container if watchdog_observation == "created" or start_submitted else None
    )
    current_inspection = {"value": initial_inspection}
    monkeypatch.setattr(
        service,
        "_inspect_container",
        lambda identifier: current_inspection["value"],
    )
    engine_lock_path = _runtime_root(tmp_path) / "engine-mutation.lock"
    engine_lock_descriptor = os.open(engine_lock_path, os.O_RDWR)
    fcntl.flock(engine_lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for _ in range(3):
            service._fire(armed, now=armed.hard_deadline)  # noqa: SLF001
    finally:
        os.close(engine_lock_descriptor)
    assert service._load_terminal(armed) is None  # noqa: SLF001
    assert service._recover_firing_intent(armed) is None  # noqa: SLF001
    if watchdog_observation == "blocked_create":
        current_inspection["value"] = created
    if start_submitted:
        # Docker may rewrite unrelated inspection metadata after the immutable expired-gate
        # observation was journaled.  The watchdog must ignore that byte-level drift while still
        # revalidating every frozen enforcement field and exact process-state timestamp.
        replay_inspection = copy.deepcopy(observed_container)
        replay_inspection["GraphDriver"] = {
            "Data": {"MergedDir": "/engine-maintained/non-security-metadata"},
            "Name": "overlay2",
        }
        assert canonical_sha256(replay_inspection) != canonical_sha256(observed_container)
        if watchdog_observation == "expired_gate_config_drift":
            host_config = replay_inspection["HostConfig"]
            assert isinstance(host_config, dict)
            host_config["Memory"] = int(host_config["Memory"]) + 1
        if watchdog_observation == "expired_gate_timestamp_drift":
            state = replay_inspection["State"]
            assert isinstance(state, dict)
            finished_at = datetime.fromisoformat(str(state["FinishedAt"]).replace("Z", "+00:00"))
            state["FinishedAt"] = (
                (finished_at + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
            )
        current_inspection["value"] = replay_inspection
    monkeypatch.setattr(service, "_deadline_reached", lambda armed, now: True)

    # Cold node restart: the service's immutable fired terminal is acknowledged as quiescent,
    # then the runtime alone removes the exact pre-workload id (or completes exact absence).
    restarted_clock = _Clock()
    if start_submitted:
        restarted_clock.wall = authorization.expires_at + timedelta(seconds=2)
    restarted = _runtime(tmp_path, policy, clock=restarted_clock)
    restarted._deadline_watchdog_controller = controller  # noqa: SLF001
    monkeypatch.setattr(
        restarted,
        "probe_production_capability",
        lambda *, request: _capability(restarted, request),
    )
    present = {"value": create_submitted}
    deleted: list[str] = []

    def _inspect(*args: object, **kwargs: object) -> dict[str, object] | None:
        del args, kwargs
        return observed_container if present["value"] else None

    def _remove(container_id: str) -> None:
        assert present["value"] is True
        assert container_id == observed_container["Id"]
        deleted.append(container_id)
        present["value"] = False

    monkeypatch.setattr(restarted, "_engine_inspect", _inspect)
    monkeypatch.setattr(restarted, "_remove_created_container", _remove)
    if watchdog_observation in {
        "expired_gate_config_drift",
        "expired_gate_timestamp_drift",
    }:
        expected_error = (
            "differs from frozen OCI enforcement"
            if watchdog_observation == "expired_gate_config_drift"
            else "changed exact process timestamps"
        )
        with pytest.raises(
            OCIWatchdogError,
            match=expected_error,
        ):
            restarted.cleanup_never_started(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
        assert present["value"] is True
        assert deleted == []
        assert service._load_terminal(armed) is not None  # noqa: SLF001
        assert (
            service._recover_watchdog_record(  # noqa: SLF001
                service._cleanup_quiescence_path(armed),  # noqa: SLF001
                oci_deployment_module._WatchdogCleanupQuiescenceRecord,  # noqa: SLF001
            )
            is None
        )
        return
    evidence = restarted.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    assert evidence.state.value == "absent"
    assert deleted == ([observed_container["Id"]] if create_submitted else [])
    terminal = service._load_terminal(armed)  # noqa: SLF001
    assert terminal is not None and terminal.status == "fired"
    assert terminal.container_was_running is (False if create_submitted else None)
    quiescence = service._recover_watchdog_record(  # noqa: SLF001
        service._cleanup_quiescence_path(armed),  # noqa: SLF001
        oci_deployment_module._WatchdogCleanupQuiescenceRecord,  # noqa: SLF001
    )
    assert quiescence is not None
    assert quiescence.decision == ("fired_stopped" if create_submitted else "fired_absent")
    completed = restarted._load_required(  # noqa: SLF001
        _runtime_root(tmp_path) / "cleanup" / "absence-1-completed.json",
        oci_runtime_module._NeverStartedCleanupCompleted,  # noqa: SLF001
    )
    assert completed.watchdog_cleanup_quiescence_journal_sha256 is not None
    assert (completed.expired_launch_gate_rejection_sha256 is not None) is start_submitted
    if start_submitted:
        pending = restarted._load_required(  # noqa: SLF001
            _runtime_root(tmp_path) / "cleanup" / "absence-1-pending.json",
            oci_runtime_module._NeverStartedCleanupPending,  # noqa: SLF001
        )
        assert pending.expired_launch_gate_rejection is not None
        assert (
            pending.expired_launch_gate_rejection.container_inspection_sha256
            == canonical_sha256(observed_container)
        )
        assert (
            pending.expired_launch_gate_rejection.container_inspection_sha256
            != canonical_sha256(current_inspection["value"])
        )

    # Reusing the Docker name cannot reactivate the old watchdog generation: its fired terminal
    # short-circuits before any inspection or kill, while exact local cleanup replays once.
    monkeypatch.setattr(
        service,
        "_inspect_container",
        lambda identifier: pytest.fail("old fired watchdog inspected a replacement name"),
    )
    assert service.recover_due_jobs() == 0
    replay = restarted.cleanup_never_started(
        request=request,
        preparation=preparation,
        authorization_request=authorization_request,
        authorization=authorization,
    )
    assert replay.state.value == "absent"
    assert deleted == ([observed_container["Id"]] if create_submitted else [])


def test_busy_start_mutation_does_not_starve_another_overdue_running_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    Path(deployment.state_root).mkdir(mode=0o700)
    service = DurableDeadlineWatchdogService(policy=policy, deployment=deployment)
    base = _overdue_armed(deployment)
    job_a = base.model_copy(
        update={
            "runtime_id": "runtime.blocked-start",
            "container_name": "aletheia-q-blocked-start",
            "authorization_request_sha256": "a" * 64,
            "container_labels": (("aletheia.runtime_id", "runtime.blocked-start"),),
        }
    )
    job_b = base.model_copy(
        update={
            "runtime_id": "runtime.overdue-running",
            "container_name": "aletheia-q-overdue-running",
            "authorization_request_sha256": "b" * 64,
            "container_labels": (("aletheia.runtime_id", "runtime.overdue-running"),),
        }
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    for armed in (job_a, job_b):
        _ensure_test_runtime_generation_lock(deployment, armed.runtime_id)
        service._publish_once(service._armed_path(armed), armed)  # noqa: SLF001
    monkeypatch.setattr(service, "_current_boot_id", lambda: base.boot_id)
    a_running = {"value": False}
    ids = {job_a.container_name: "a" * 64, job_b.container_name: "b" * 64}

    def _inspection(identifier: str) -> dict[str, object]:
        armed = job_a if identifier == job_a.container_name else job_b
        running = a_running["value"] if armed == job_a else True
        return {
            "Id": ids[identifier],
            "Name": f"/{identifier}",
            "Config": {"Labels": dict(armed.container_labels)},
            "State": {
                "Status": "running" if running else "created",
                "Running": running,
                "Pid": 4242 if running else 0,
            },
        }

    monkeypatch.setattr(service, "_inspect_container", _inspection)
    monkeypatch.setattr(
        service,
        "_resolve_cgroup_identity",
        lambda *, container_id, init_pid: (
            f"/system.slice/docker-{container_id}.scope",
            hashlib.sha256(f"{container_id}:{init_pid}".encode()).hexdigest(),
        ),
    )
    killed: list[str] = []

    def _kill(**scope: object) -> tuple[str, str]:
        container_id = str(scope["container_id"])
        killed.append(container_id)
        return str(scope["cgroup_path"]), str(scope["expected_identity_sha256"])

    monkeypatch.setattr(service, "_kill_exact_cgroup", _kill)
    a_key = hashlib.sha256(
        b"ALETHEIA_QUALIFICATION_OCI_RUNTIME_V2\x00" + job_a.runtime_id.encode()
    ).hexdigest()
    lock_descriptor = os.open(
        Path(deployment.journal_root) / a_key / "engine-mutation.lock",
        os.O_RDWR,
    )
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert service.recover_due_jobs() == 2
        assert service._load_terminal(job_a) is None  # noqa: SLF001
        terminal_b = service._load_terminal(job_b)  # noqa: SLF001
        assert terminal_b is not None and terminal_b.container_was_running is True
        assert killed == [ids[job_b.container_name]]
    finally:
        os.close(lock_descriptor)

    # Docker start may now complete, but the still-armed job is re-inspected as RUNNING and its
    # exact cgroup is killed; the earlier CREATED snapshot never became terminal evidence.
    a_running["value"] = True
    assert service.recover_due_jobs() == 1
    terminal_a = service._load_terminal(job_a)  # noqa: SLF001
    assert terminal_a is not None and terminal_a.container_was_running is True
    assert killed == [ids[job_b.container_name], ids[job_a.container_name]]


def test_watchdog_armed_record_is_canonical_and_hash_bound() -> None:
    armed = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW,
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW - timedelta(seconds=1),
        service_boot_id="boot",
    )
    assert (
        hashlib.sha256(
            canonical_json_bytes(
                armed.model_dump(mode="json", exclude={"armed_at", "service_boot_id"})
            )
        ).hexdigest()
        == armed.job_sha256
    )
    assert len(armed.job_sha256) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-job",
        "retired-with-record",
        "absent-with-container",
        "stopped-without-container",
    ],
)
def test_watchdog_client_rejects_wrong_job_and_impossible_quiescence_union(
    tmp_path: Path,
    mutation: str,
) -> None:
    policy = _policy(tmp_path / "policy")
    deployment = _watchdog_deployment(tmp_path, policy)
    controller = SystemdDeadlineWatchdogController(policy=policy, deployment=deployment)
    response: dict[str, object] = {
        "schema": "aletheia.systemd_oci_watchdog_response.v1",
        "operation": "retire",
        "deployment_sha256": deployment.deployment_sha256,
        "service_pid": 1,
        "service_boot_id": "boot",
        "managed_by_systemd": True,
        "evidence_sha256": H0,
        "job_sha256": H0,
        "terminal_decision": "retired",
    }
    if mutation == "wrong-job":
        response["job_sha256"] = "1" * 64
    elif mutation == "retired-with-record":
        response["cleanup_quiescence_record_sha256"] = "2" * 64
    elif mutation == "absent-with-container":
        response["terminal_decision"] = "fired_absent"
        response["cleanup_quiescence_record_sha256"] = "2" * 64
        response["cleanup_container_id"] = "3" * 64
    else:
        assert mutation == "stopped-without-container"
        response["terminal_decision"] = "fired_stopped"
        response["cleanup_quiescence_record_sha256"] = "2" * 64

    with pytest.raises(OCIWatchdogError, match="attestation differs"):
        controller._validate_response(  # noqa: SLF001 - exact response-union regression
            response,
            operation="retire",
            evidence=H0,
            expected_job_sha256=H0,
        )


@pytest.mark.parametrize(
    "phase",
    ["pending-moded-before-final-fsync", "pending-fsynced", "final-linked-fsynced"],
)
@pytest.mark.parametrize("publisher", ["watchdog", "quota"])
def test_root_service_record_publish_recovers_both_power_loss_residues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    publisher: str,
) -> None:
    root = tmp_path / publisher
    root.mkdir(mode=0o700)
    record = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW + timedelta(minutes=1),
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW,
        service_boot_id="boot",
    )
    final = root / "record.json"
    if publisher == "watchdog":
        policy = _policy(tmp_path / "policy")
        service = DurableDeadlineWatchdogService(
            policy=policy,
            deployment=_watchdog_deployment(tmp_path, policy),
        )
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_once  # noqa: SLF001
    else:
        service = object.__new__(LoopbackOutputQuotaProvisioningService)
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_root_model  # noqa: SLF001

    class _PowerLoss(BaseException):
        pass

    def _crash(observed: str, path: Path) -> None:
        assert path == final
        if observed == phase:
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        publish(final, record)
    pending = final.with_name(f".{final.name}.pending")
    assert pending.is_file()
    if phase in {"pending-moded-before-final-fsync", "pending-fsynced"}:
        assert not final.exists()
        assert pending.stat().st_nlink == 1
    else:
        assert final.is_file()
        assert pending.stat().st_ino == final.stat().st_ino
        assert pending.stat().st_nlink == final.stat().st_nlink == 2

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    publish(final, record)
    assert not pending.exists()
    assert final.stat().st_nlink == 1
    assert final.read_bytes() == canonical_json_bytes(record)


def test_root_publisher_fsyncs_complete_0600_bytes_before_sealing_0400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "watchdog"
    root.mkdir(mode=0o700)
    policy = _policy(tmp_path / "policy")
    service = DurableDeadlineWatchdogService(
        policy=policy,
        deployment=_watchdog_deployment(tmp_path, policy),
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    record = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW + timedelta(minutes=1),
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW,
        service_boot_id="boot",
    )
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

    monkeypatch.setattr(oci_deployment_module.os, "fsync", _fsync)
    monkeypatch.setattr(oci_deployment_module.os, "fchmod", _fchmod)
    service._publish_once(root / "record.json", record)  # noqa: SLF001

    assert events[:3] == [("fsync", 0o600), ("fchmod", 0o400), ("fsync", 0o400)]


def test_artificial_partial_0400_pending_is_rejected_not_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "watchdog"
    root.mkdir(mode=0o700)
    final = root / "record.json"
    pending = final.with_name(f".{final.name}.pending")
    pending.write_bytes(b'{"schema_name":')
    pending.chmod(0o400)
    policy = _policy(tmp_path / "policy")
    service = DurableDeadlineWatchdogService(
        policy=policy,
        deployment=_watchdog_deployment(tmp_path, policy),
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())

    with pytest.raises(OCIWatchdogError, match="validation"):
        service._recover_watchdog_record(final, _WatchdogArmedRecord)  # noqa: SLF001

    assert not final.exists()
    assert pending.is_file()


@pytest.mark.parametrize("residue", ["empty", "partial", "exact"])
@pytest.mark.parametrize("publisher", ["watchdog", "quota"])
def test_root_service_record_publish_discards_only_safe_unsealed_pending_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue: str,
    publisher: str,
) -> None:
    root = tmp_path / publisher
    root.mkdir(mode=0o700)
    record = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW + timedelta(minutes=1),
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW,
        service_boot_id="boot",
    )
    final = root / "record.json"
    pending = final.with_name(f".{final.name}.pending")
    payload = canonical_json_bytes(record)
    residues = {
        "empty": b"",
        "partial": payload[: max(1, len(payload) // 3)],
        "exact": payload,
    }
    pending.write_bytes(residues[residue])
    pending.chmod(0o600)
    phases: list[str] = []
    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: phases.append(phase),
    )
    if publisher == "watchdog":
        policy = _policy(tmp_path / "policy")
        service = DurableDeadlineWatchdogService(
            policy=policy,
            deployment=_watchdog_deployment(tmp_path, policy),
        )
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_once  # noqa: SLF001
    else:
        service = object.__new__(LoopbackOutputQuotaProvisioningService)
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_root_model  # noqa: SLF001

    publish(final, record)

    assert "pending-created" in phases
    assert not pending.exists()
    assert final.read_bytes() == payload
    assert final.stat().st_nlink == 1
    assert stat.S_IMODE(final.stat().st_mode) == 0o400


def test_root_service_record_publish_discards_unsealed_nonprefix_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "quota"
    root.mkdir(mode=0o700)
    record = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW + timedelta(minutes=1),
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW,
        service_boot_id="boot",
    )
    final = root / "record.json"
    pending = final.with_name(f".{final.name}.pending")
    pending.write_bytes(b"not-a-prefix")
    pending.chmod(0o600)
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())

    service._publish_root_model(final, record)  # noqa: SLF001

    assert not pending.exists()
    assert final.read_bytes() == canonical_json_bytes(record)


def test_root_service_record_publish_rejects_unsealed_pending_with_extra_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "quota"
    root.mkdir(mode=0o700)
    record = _WatchdogArmedRecord(
        deployment_sha256="0" * 64,
        preparation_sha256="1" * 64,
        boot_id="boot",
        runtime_id="runtime",
        container_name="container",
        engine_endpoint="unix:///var/run/docker.sock",
        authorization_request_sha256="2" * 64,
        runtime_launch_authorization_sha256="3" * 64,
        pre_runtime_absence_epoch=0,
        hard_deadline=NOW + timedelta(minutes=1),
        hard_deadline_boottime_ns=1,
        expected_evidence_sha256="4" * 64,
        container_labels=(),
        armed_at=NOW,
        service_boot_id="boot",
    )
    final = root / "record.json"
    pending = final.with_name(f".{final.name}.pending")
    pending.write_bytes(b"unpublished")
    pending.chmod(0o600)
    os.link(pending, root / "unexpected-link")
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())

    with pytest.raises(OCIOutputQuotaError, match="custody is unsafe"):
        service._publish_root_model(final, record)  # noqa: SLF001

    assert not final.exists()


@pytest.mark.parametrize("publisher", ["watchdog", "quota"])
def test_unpublished_time_varying_root_record_is_rebuilt_from_new_replay_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher: str,
) -> None:
    root = tmp_path / publisher
    root.mkdir(mode=0o700)
    final = root / "record.json"
    if publisher == "watchdog":
        old = _WatchdogArmedRecord(
            deployment_sha256="0" * 64,
            preparation_sha256="1" * 64,
            boot_id="boot",
            runtime_id="runtime",
            container_name="container",
            engine_endpoint="unix:///var/run/docker.sock",
            authorization_request_sha256="2" * 64,
            runtime_launch_authorization_sha256="3" * 64,
            pre_runtime_absence_epoch=0,
            hard_deadline=NOW + timedelta(minutes=1),
            hard_deadline_boottime_ns=1,
            expected_evidence_sha256="4" * 64,
            container_labels=(),
            armed_at=NOW,
            service_boot_id="boot",
        )
        new = old.model_copy(update={"armed_at": NOW + timedelta(seconds=1)})
        policy = _policy(tmp_path / "policy")
        service = DurableDeadlineWatchdogService(
            policy=policy,
            deployment=_watchdog_deployment(tmp_path, policy),
        )
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_once  # noqa: SLF001
    else:
        capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
        old = _QuotaProvisioningIntent(
            deployment_sha256="0" * 64,
            node_manifest_sha256="1" * 64,
            node_id="node.test",
            boot_id="boot.test",
            execution_id="exe_" + "2" * 32,
            infrastructure_attempt_id="iat_" + "3" * 32,
            intent_sha256="4" * 64,
            output_root=str(tmp_path / "output"),
            output_quota_bytes=capacity,
            block_device_capacity_bytes=capacity,
            underlying_root_device=1,
            underlying_root_inode=1,
            backing_file=str(tmp_path / "backing.img"),
            filesystem_uuid="12345678-1234-1234-1234-123456789abc",
            created_at=NOW,
            service_boot_id="boot",
        )
        new = old.model_copy(update={"created_at": NOW + timedelta(seconds=1)})
        service = object.__new__(LoopbackOutputQuotaProvisioningService)
        monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
        publish = service._publish_root_model  # noqa: SLF001
    pending = final.with_name(f".{final.name}.pending")
    pending.write_bytes(canonical_json_bytes(old))
    pending.chmod(0o600)

    publish(final, new)

    assert not pending.exists()
    assert final.read_bytes() == canonical_json_bytes(new)


def test_quota_generation_directory_is_parent_fsynced_before_first_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    output_root = tmp_path / "output"
    output_root.mkdir(mode=0o700)
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        state_root=str(state_root),
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())

    @contextmanager
    def _target(attempt_id: str):
        descriptor = os.open(output_root, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    fsynced: list[Path] = []
    monkeypatch.setattr(service, "_sealed_output_target", _target)
    monkeypatch.setattr(service, "_fsync_directory", lambda path: fsynced.append(path))

    class _PowerLossAfterMkdir(BaseException):
        pass

    class _StopAfterGeneration(BaseException):
        pass

    def _stop(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _StopAfterGeneration

    monkeypatch.setattr(service, "_ensure_intent", _stop)
    request = {
        "node_manifest_sha256": "1" * 64,
        "node_id": "node.test",
        "boot_id": "boot.test",
        "execution_id": "exe_" + "2" * 32,
        "attempt_id": "iat_" + "3" * 32,
        "intent_sha256": "4" * 64,
        "output_root": str(output_root),
        "output_quota_bytes": MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        "expected_receipt": "none",
    }

    crashed = False

    def _crash_after_mkdir(phase: str, path: Path) -> None:
        nonlocal crashed
        if phase == "quota-generation-directory-created-before-parent-fsync":
            crashed = True
            raise _PowerLossAfterMkdir

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        _crash_after_mkdir,
    )
    with pytest.raises(_PowerLossAfterMkdir):
        service.ensure(request)
    assert crashed is True
    assert fsynced == []
    assert service._generation_root(request["attempt_id"]).is_dir()  # noqa: SLF001

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    with pytest.raises(_StopAfterGeneration):
        service.ensure(request)
    assert fsynced == [state_root]
    assert service._generation_root(request["attempt_id"]).is_dir()  # noqa: SLF001


def test_backing_file_zero_length_create_crash_is_completed_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    backing = tmp_path / "quota.img"
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    intent = _QuotaProvisioningIntent(
        deployment_sha256="0" * 64,
        node_manifest_sha256="1" * 64,
        node_id="node.test",
        boot_id="boot.test",
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        intent_sha256="4" * 64,
        output_root=str(tmp_path / "output"),
        output_quota_bytes=capacity + 1,
        block_device_capacity_bytes=capacity,
        underlying_root_device=1,
        underlying_root_inode=1,
        backing_file=str(backing),
        filesystem_uuid="12345678-1234-1234-1234-123456789abc",
        created_at=NOW,
        service_boot_id="boot",
    )

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "backing-created":
            assert path == backing
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service._ensure_backing_file(intent)  # noqa: SLF001
    inode = backing.stat().st_ino
    assert backing.stat().st_size == 0

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    identity = service._ensure_backing_file(intent)  # noqa: SLF001
    assert backing.stat().st_ino == inode
    assert backing.stat().st_size == capacity
    assert len(identity) == 64


def test_backing_file_truncate_before_fsync_is_flushed_on_existing_inode_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    backing = tmp_path / "quota.img"
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    intent = _QuotaProvisioningIntent(
        deployment_sha256="0" * 64,
        node_manifest_sha256="1" * 64,
        node_id="node.test",
        boot_id="boot.test",
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        intent_sha256="4" * 64,
        output_root=str(tmp_path / "output"),
        output_quota_bytes=capacity,
        block_device_capacity_bytes=capacity,
        underlying_root_device=1,
        underlying_root_inode=1,
        backing_file=str(backing),
        filesystem_uuid="12345678-1234-1234-1234-123456789abc",
        created_at=NOW,
        service_boot_id="boot",
    )

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "backing-sized-before-fsync":
            assert path == backing
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service._ensure_backing_file(intent)  # noqa: SLF001
    inode = backing.stat().st_ino
    assert backing.stat().st_size == capacity

    original_fsync = os.fsync
    file_fsynced = False

    def _record_fsync(descriptor: int) -> None:
        nonlocal file_fsynced
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_ino == inode:
            file_fsynced = True
        original_fsync(descriptor)

    monkeypatch.setattr(oci_deployment_module.os, "fsync", _record_fsync)
    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    service._ensure_backing_file(intent)  # noqa: SLF001

    assert file_fsynced is True
    assert backing.stat().st_ino == inode


@pytest.mark.parametrize(
    "crash_phase",
    ["pending-written-before-mode", "pending-fsynced", "final-linked-fsynced"],
)
def test_quota_attachment_replay_consumes_the_exact_published_time_varying_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    intent = _QuotaProvisioningIntent(
        deployment_sha256="0" * 64,
        node_manifest_sha256="1" * 64,
        node_id="node.test",
        boot_id="boot.test",
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        intent_sha256="4" * 64,
        output_root=str(tmp_path / "output"),
        output_quota_bytes=capacity,
        block_device_capacity_bytes=capacity,
        underlying_root_device=1,
        underlying_root_inode=1,
        backing_file=str(tmp_path / "quota.img"),
        filesystem_uuid="12345678-1234-1234-1234-123456789abc",
        created_at=NOW,
        service_boot_id="boot",
    )
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(deployment_sha256="0" * 64)  # noqa: SLF001
    wall = {"now": NOW}

    class _AdvancingDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            del tz
            return wall["now"]

    monkeypatch.setattr(oci_deployment_module, "datetime", _AdvancingDateTime)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_find_existing_loop", lambda path: "/dev/loop7")
    monkeypatch.setattr(service, "_verify_loop_association", lambda **scope: None)
    monkeypatch.setattr(service, "_seal_loop_device_node", lambda path: None)

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == crash_phase:
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service._ensure_loop_attachment(  # noqa: SLF001
            intent,
            backing_identity="5" * 64,
            generation_root=generation,
        )
    final = generation / "loop-attached.json"
    pending = final.with_name(f".{final.name}.pending")
    old = _QuotaLoopAttachment.model_validate_json(pending.read_bytes())

    wall["now"] = NOW + timedelta(minutes=1)
    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    recovered = service._ensure_loop_attachment(  # noqa: SLF001
        intent,
        backing_identity="5" * 64,
        generation_root=generation,
    )

    if crash_phase == "pending-written-before-mode":
        assert recovered.attached_at == wall["now"]
        assert recovered.attachment_record_sha256 != old.attachment_record_sha256
    else:
        assert recovered == old
        assert recovered.attachment_record_sha256 == old.attachment_record_sha256
    assert final.read_bytes() == canonical_json_bytes(recovered)


def test_quota_service_seals_the_exact_reserved_loop_device_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    expected_uid = os.geteuid()
    expected_gid = os.getegid()
    monkeypatch.setattr(service, "_trusted_root_service_uid", lambda: expected_uid)

    before = SimpleNamespace(
        st_mode=stat.S_IFBLK | 0o660,
        st_uid=expected_uid,
        st_gid=expected_gid + 1,
        st_nlink=1,
        st_dev=10,
        st_ino=20,
        st_rdev=os.makedev(7, 22),
    )
    sealed = SimpleNamespace(
        st_mode=stat.S_IFBLK | 0o600,
        st_uid=expected_uid,
        st_gid=expected_gid,
        st_nlink=1,
        st_dev=before.st_dev,
        st_ino=before.st_ino,
        st_rdev=before.st_rdev,
    )
    fstats = iter((before, sealed))
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(oci_deployment_module.os, "open", lambda path, flags: 41)
    monkeypatch.setattr(oci_deployment_module.os, "fstat", lambda descriptor: next(fstats))
    monkeypatch.setattr(
        oci_deployment_module.os,
        "fchown",
        lambda descriptor, uid, gid: calls.append(("chown", descriptor, uid, gid)),
    )
    monkeypatch.setattr(
        oci_deployment_module.os,
        "fchmod",
        lambda descriptor, mode: calls.append(("chmod", descriptor, mode)),
    )
    monkeypatch.setattr(
        oci_deployment_module.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )
    monkeypatch.setattr(Path, "lstat", lambda path: sealed)

    service._seal_loop_device_node("/dev/loop22")  # noqa: SLF001

    assert calls == [
        ("chown", 41, expected_uid, expected_gid),
        ("chmod", 41, 0o600),
        ("close", 41),
    ]


def test_quota_service_rejects_loop_device_node_replacement_while_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    expected_uid = os.geteuid()
    expected_gid = os.getegid()
    monkeypatch.setattr(service, "_trusted_root_service_uid", lambda: expected_uid)
    sealed = SimpleNamespace(
        st_mode=stat.S_IFBLK | 0o600,
        st_uid=expected_uid,
        st_gid=expected_gid,
        st_nlink=1,
        st_dev=10,
        st_ino=20,
        st_rdev=os.makedev(7, 22),
    )
    replaced = SimpleNamespace(**{**sealed.__dict__, "st_ino": 21})
    fstats = iter((sealed, sealed))
    monkeypatch.setattr(oci_deployment_module.os, "open", lambda path, flags: 41)
    monkeypatch.setattr(oci_deployment_module.os, "fstat", lambda descriptor: next(fstats))
    monkeypatch.setattr(oci_deployment_module.os, "close", lambda descriptor: None)
    monkeypatch.setattr(Path, "lstat", lambda path: replaced)

    with pytest.raises(OCIOutputQuotaError, match="sealing did not persist"):
        service._seal_loop_device_node("/dev/loop22")  # noqa: SLF001


def test_quota_service_reverifies_kernel_association_after_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_verify_loop_association",
        lambda **scope: calls.append(f"verify:{scope['loop_device']}"),
    )
    monkeypatch.setattr(
        service,
        "_seal_loop_device_node",
        lambda path: calls.append(f"seal:{path}"),
    )

    service._verify_and_seal_loop_association(  # noqa: SLF001
        loop_device="/dev/loop22",
        backing_file=Path("/quota.img"),
        quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )

    assert calls == ["verify:/dev/loop22", "seal:/dev/loop22", "verify:/dev/loop22"]


def test_sealed_formatted_record_is_promoted_before_any_second_mkfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    intent = _QuotaProvisioningIntent(
        deployment_sha256="0" * 64,
        node_manifest_sha256="1" * 64,
        node_id="node.test",
        boot_id="boot.test",
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        intent_sha256="4" * 64,
        output_root=str(tmp_path / "output"),
        output_quota_bytes=capacity,
        block_device_capacity_bytes=capacity,
        underlying_root_device=1,
        underlying_root_inode=1,
        backing_file=str(tmp_path / "quota.img"),
        filesystem_uuid="12345678-1234-1234-1234-123456789abc",
        created_at=NOW,
        service_boot_id="boot",
    )
    attachment = _QuotaLoopAttachment(
        deployment_sha256="0" * 64,
        intent_record_sha256=intent.intent_record_sha256,
        loop_device="/dev/loop7",
        backing_file_identity_sha256="5" * 64,
        attached_at=NOW,
    )
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        deployment_sha256="0" * 64,
        mkfs=object(),
    )
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    mkfs_calls = 0

    def _mkfs(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mkfs_calls
        mkfs_calls += 1
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(service, "_run_pinned", _mkfs)

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "pending-fsynced":
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service._ensure_formatted(  # noqa: SLF001
            intent,
            attachment=attachment,
            generation_root=generation,
        )
    assert mkfs_calls == 1

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    formatted = service._ensure_formatted(  # noqa: SLF001
        intent,
        attachment=attachment,
        generation_root=generation,
    )

    assert mkfs_calls == 1
    assert formatted.attachment_record_sha256 == attachment.attachment_record_sha256


def test_mount_before_receipt_crash_recovers_same_root_service_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    backing_root = tmp_path / "backing"
    backing_root.mkdir(mode=0o700)
    workspace_root = tmp_path / "workspace"
    attempt_id = "iat_" + "3" * 32
    attempt_key = hashlib.sha256(attempt_id.encode()).hexdigest()
    output_root = workspace_root / attempt_key / "output"
    output_root.mkdir(parents=True, mode=0o700)
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        deployment_sha256="0" * 64,
        state_root=str(state_root),
        backing_root=str(backing_root),
        workspace_root=str(workspace_root),
        allowed_client_uid=os.geteuid(),
        allowed_client_gid=os.getegid(),
        provisioner_policy_sha256="5" * 64,
        provisioner_principal_id="principal:quota-test",
    )
    mounted = False

    @contextmanager
    def _target(attempt_id: str):
        assert attempt_id == "iat_" + "3" * 32
        descriptor = os.open(output_root, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(service, "_sealed_output_target", _target)
    monkeypatch.setattr(service, "_trusted_state_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_current_boot_id", lambda: "boot")
    monkeypatch.setattr(
        service,
        "_find_mount",
        lambda path: {"mount_id": 41} if mounted else None,
    )
    monkeypatch.setattr(service, "_ensure_backing_file", lambda intent: "6" * 64)
    monkeypatch.setattr(service, "_ensure_loop_attachment", lambda *args, **kwargs: object())
    monkeypatch.setattr(service, "_ensure_formatted", lambda *args, **kwargs: object())
    monkeypatch.setattr(service, "_verify_live_receipt", lambda receipt: None)

    def _mount(*args, **kwargs) -> OutputQuotaProvisioningReceipt:  # type: ignore[no-untyped-def]
        nonlocal mounted
        mounted = True
        metadata = output_root.lstat()
        return OutputQuotaProvisioningReceipt(
            node_manifest_sha256="1" * 64,
            node_id="node.test",
            boot_id="boot.test",
            execution_id="exe_" + "2" * 32,
            infrastructure_attempt_id="iat_" + "3" * 32,
            intent_sha256="4" * 64,
            output_root=str(output_root),
            output_quota_bytes=capacity + 1,
            output_root_device=metadata.st_dev,
            output_root_inode=metadata.st_ino,
            output_root_owner_uid=metadata.st_uid,
            output_root_owner_gid=metadata.st_gid,
            mount_id=41,
            mount_parent_id=32,
            block_device_major=os.major(metadata.st_dev),
            block_device_minor=os.minor(metadata.st_dev),
            block_device_capacity_bytes=capacity,
            filesystem_type="ext4",
            filesystem_uuid_sha256="7" * 64,
            mount_options=("noatime", "nodev", "noexec", "nosuid", "rw"),
            backing_file_identity_sha256="6" * 64,
            provisioner_policy_sha256="5" * 64,
            provisioner_principal_id="principal:quota-test",
            provisioned_at=NOW,
        )

    monkeypatch.setattr(service, "_ensure_mounted", _mount)
    request = {
        "node_manifest_sha256": "1" * 64,
        "node_id": "node.test",
        "boot_id": "boot.test",
        "execution_id": "exe_" + "2" * 32,
        "attempt_id": "iat_" + "3" * 32,
        "intent_sha256": "4" * 64,
        "output_root": str(output_root),
        "output_quota_bytes": capacity + 1,
        "expected_receipt": "none",
    }

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "quota-mounted-before-receipt":
            raise _PowerLoss

    monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
    with pytest.raises(_PowerLoss):
        service.ensure(request)
    generation = service._generation_root(request["attempt_id"])  # noqa: SLF001
    assert (generation / "intent.json").is_file()
    assert not (generation / "receipt.json").exists()

    monkeypatch.setattr(
        oci_deployment_module,
        "_durable_publish_checkpoint",
        lambda phase, path: None,
    )
    receipt = service.ensure(request)
    replay = dict(request)
    replay["expected_receipt"] = receipt.model_dump(mode="json")
    assert service.ensure(replay) == receipt
    assert receipt.block_device_capacity_bytes == capacity
    assert receipt.output_quota_bytes == capacity + 1


def test_mount_command_return_crash_recovers_root_owned_partial_before_chown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir(mode=0o755)
    capacity = MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        filesystem_type="ext4",
        mount=object(),
        allowed_client_uid=os.geteuid(),
        allowed_client_gid=os.getegid(),
        provisioner_policy_sha256="5" * 64,
        provisioner_principal_id="principal:quota-test",
    )
    intent = _QuotaProvisioningIntent(
        deployment_sha256="0" * 64,
        node_manifest_sha256="1" * 64,
        node_id="node.test",
        boot_id="boot.test",
        execution_id="exe_" + "2" * 32,
        infrastructure_attempt_id="iat_" + "3" * 32,
        intent_sha256="4" * 64,
        output_root=str(output_root),
        output_quota_bytes=capacity + 1,
        block_device_capacity_bytes=capacity,
        underlying_root_device=output_root.stat().st_dev,
        underlying_root_inode=output_root.stat().st_ino,
        backing_file=str(tmp_path / "backing.img"),
        filesystem_uuid="12345678-1234-1234-1234-123456789abc",
        created_at=NOW,
        service_boot_id="boot",
    )
    attachment = _QuotaLoopAttachment(
        deployment_sha256="0" * 64,
        intent_record_sha256=intent.intent_record_sha256,
        loop_device="/dev/loop7",
        backing_file_identity_sha256="6" * 64,
        attached_at=NOW,
    )
    formatted = _QuotaFilesystemFormatted(
        deployment_sha256="0" * 64,
        attachment_record_sha256=attachment.attachment_record_sha256,
        filesystem_uuid=intent.filesystem_uuid,
        filesystem_uuid_sha256="7" * 64,
        formatted_at=NOW,
    )
    mount = {
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
    mounted = False

    def _find_mount(path: Path):  # type: ignore[no-untyped-def]
        assert path == output_root
        return mount if mounted else None

    def _run_pinned(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal mounted
        mounted = True
        return object()

    monkeypatch.setattr(service, "_find_mount", _find_mount)
    monkeypatch.setattr(service, "_run_pinned", _run_pinned)

    class _PowerLoss(BaseException):
        pass

    def _crash(phase: str, path: Path) -> None:
        if phase == "quota-mount-command-returned":
            assert path == output_root
            raise _PowerLoss

    descriptor = os.open(output_root, os.O_RDONLY)
    try:
        monkeypatch.setattr(oci_deployment_module, "_durable_publish_checkpoint", _crash)
        with pytest.raises(_PowerLoss):
            service._ensure_mounted(  # noqa: SLF001
                intent,
                attachment=attachment,
                formatted=formatted,
                output_descriptor=descriptor,
            )
        assert stat.S_IMODE(output_root.stat().st_mode) == 0o755
        monkeypatch.setattr(
            oci_deployment_module,
            "_durable_publish_checkpoint",
            lambda phase, path: None,
        )
        receipt = service._ensure_mounted(  # noqa: SLF001
            intent,
            attachment=attachment,
            formatted=formatted,
            output_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)
    assert receipt.output_root_mode == 0o700
    assert receipt.block_device_capacity_bytes == capacity


@pytest.mark.parametrize("mutation", ["parent-symlink", "rename-race"])
def test_quota_target_lineage_rejects_symlink_and_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    workspace = tmp_path / "workspaces"
    workspace.mkdir(mode=0o1730)
    workspace.chmod(0o1730)
    attempt_id = "iat_" + "a" * 32
    attempt_name = hashlib.sha256(attempt_id.encode()).hexdigest()
    attempt = workspace / attempt_name
    if mutation == "parent-symlink":
        escaped = tmp_path / "escaped"
        (escaped / "output").mkdir(parents=True)
        attempt.symlink_to(escaped, target_is_directory=True)
    else:
        (attempt / "output").mkdir(parents=True, mode=0o700)
        attempt.chmod(0o700)
        (attempt / "output").chmod(0o700)
    metadata = workspace.lstat()
    service = object.__new__(LoopbackOutputQuotaProvisioningService)
    service._deployment = SimpleNamespace(  # noqa: SLF001
        workspace_root=str(workspace),
        workspace_root_pin=SimpleNamespace(
            mode=0o1730,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            parent_chain_sha256=H0,
        ),
        allowed_client_uid=os.geteuid(),
        allowed_client_gid=os.getegid(),
    )
    monkeypatch.setattr(service, "_trusted_root_service_uid", lambda: os.geteuid())
    monkeypatch.setattr(service, "_find_mount", lambda path: {"mount_id": 9})
    monkeypatch.setattr(
        oci_deployment_module,
        "_observe_live_output_workspace_root",
        lambda expected: SimpleNamespace(mount_id=9),
    )
    monkeypatch.setattr(oci_deployment_module, "host_parent_chain_sha256", lambda path: H0)
    if mutation == "rename-race":
        original_fchown = os.fchown

        def _race(descriptor: int, uid: int, gid: int) -> None:
            original_fchown(descriptor, uid, gid)
            attempt.rename(workspace / "detached")
            attempt.mkdir(mode=0o710)

        monkeypatch.setattr(oci_deployment_module.os, "fchown", _race)
    with pytest.raises(OCIOutputQuotaError, match="lineage|changed|safely opened"):
        with service._sealed_output_target(attempt_id):  # noqa: SLF001
            pytest.fail("unsafe output lineage was accepted")


def test_watchdog_cgroup_parser_binds_full_container_identity() -> None:
    container_id = "a" * 64
    path = DurableDeadlineWatchdogService._container_cgroup_path(  # noqa: SLF001
        f"0::/system.slice/docker-{container_id}.scope\n",
        container_id=container_id,
    )
    assert path == Path(f"/system.slice/docker-{container_id}.scope")
    with pytest.raises(OCIWatchdogError, match="Docker identity"):
        DurableDeadlineWatchdogService._container_cgroup_path(  # noqa: SLF001
            f"0::/docker/{'b' * 64}\n",
            container_id=container_id,
        )
