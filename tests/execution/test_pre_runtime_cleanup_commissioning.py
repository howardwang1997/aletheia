from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

import aletheia.pre_runtime_cleanup_commissioning as commissioning
from aletheia.execution.qualification_node_service import (
    AttemptScopedPreRuntimeCleanupServiceConfigV1,
    QualificationNodeServiceConfigV1,
)
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.pre_runtime_cleanup_commissioning import (
    PreRuntimeCleanupCommissioningError,
    PreRuntimeCleanupCommissioningRequestV1,
    commission_pre_runtime_cleanup,
)

from .test_qualification_node_service import _fixture


def _source_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    _process, base = _fixture(monkeypatch, fixture_root)
    key_root = (tmp_path / "deployment" / "keys").resolve()
    config_root = (tmp_path / "deployment" / "config").resolve()
    key_root.mkdir(parents=True)
    config_root.mkdir(parents=True)
    source = QualificationNodeServiceConfigV1.model_validate(
        base.model_copy(
            update={
                "schema_revision": "20260903_0032",
                "node_signing_key": base.node_signing_key.model_copy(
                    update={"path": str(key_root / "node-signing.key")}
                ),
                "assignment_transport_key": base.assignment_transport_key.model_copy(
                    update={"path": str(key_root / "assignment-transport.key")}
                ),
                "runtime_control_key": base.runtime_control_key.model_copy(
                    update={"path": str(key_root / "runtime-control.key")}
                ),
            }
        ).model_dump(mode="python")
    )
    return source, key_root, config_root


def _request(
    source: QualificationNodeServiceConfigV1,
    *,
    key_root: Path,
    config_root: Path,
) -> PreRuntimeCleanupCommissioningRequestV1:
    active_until = min(
        source.node_authority.manifest.key_expires_at,
        source.node_authority.manifest.key_revoked_at
        or source.node_authority.manifest.key_expires_at,
    )
    return PreRuntimeCleanupCommissioningRequestV1(
        source_node_config_path=str(config_root / "node.json"),
        source_node_config_sha256="1" * 64,
        target_cleanup_key_path=str(key_root / "pre-runtime-cleanup.key"),
        target_cleanup_config_path=str(config_root / "pre-runtime-cleanup.json"),
        principal_id="principal:test-pre-runtime-cleanup",
        policy_sha256=hashlib.sha256(b"test-pre-runtime-cleanup-policy").hexdigest(),
        infrastructure_attempt_id="iat_" + "7" * 32,
        runtime_preparation_sha256="8" * 64,
        runtime_launch_authorization_sha256="9" * 64,
        cleanup_absence_epoch=1,
        valid_from=active_until,
        expires_at=active_until + timedelta(minutes=30),
        configured_at=active_until,
    )


def test_target_local_commissioning_exact_replay_never_exports_private_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, key_root, config_root = _source_config(monkeypatch, tmp_path)
    request = _request(source, key_root=key_root, config_root=config_root)
    metadata: dict[Path, tuple[int, int, int]] = {}

    monkeypatch.setattr(commissioning.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commissioning, "_load_source", lambda _request: source)
    monkeypatch.setattr(commissioning, "expected_schema_revision", lambda: source.schema_revision)
    monkeypatch.setattr(commissioning, "host_parent_chain_sha256", lambda _path: "a" * 64)

    def publish(path: Path, payload: bytes, *, uid: int, gid: int, mode: int) -> bool:
        if path.exists():
            assert path.read_bytes() == payload
            assert metadata[path] == (uid, gid, mode)
            return False
        path.write_bytes(payload)
        path.chmod(mode)
        metadata[path] = (uid, gid, mode)
        return True

    def read(
        path: Path,
        *,
        expected_sha256: str | None,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
        expected_mode: int | None = None,
        expected_bytes: int | None = None,
        allowed_link_counts: frozenset[int] = frozenset({1}),
    ) -> bytes:
        assert allowed_link_counts in {frozenset({1}), frozenset({1, 2})}
        payload = path.read_bytes()
        uid, gid, mode = metadata[path]
        assert expected_uid in {None, uid}
        assert expected_gid in {None, gid}
        assert expected_mode in {None, mode}
        assert expected_bytes in {None, len(payload)}
        assert expected_sha256 in {None, hashlib.sha256(payload).hexdigest()}
        return payload

    monkeypatch.setattr(commissioning, "_publish_exclusive", publish)
    monkeypatch.setattr(commissioning, "_read_regular", read)

    first = commission_pre_runtime_cleanup(request)
    second = commission_pre_runtime_cleanup(request)

    assert first == second
    assert first.key_published is True and first.config_published is True
    receipt_json = json.loads(canonical_json_bytes(first))
    assert receipt_json["private_key_exported"] is False
    assert "private_key_bytes" not in receipt_json
    assert "private_key_file_sha256" not in receipt_json
    config_payload = Path(first.cleanup_config_path).read_bytes()
    config = AttemptScopedPreRuntimeCleanupServiceConfigV1.model_validate(
        json.loads(config_payload)
    )
    assert canonical_json_bytes(config) == config_payload
    assert config.cleanup_authority_pin.infrastructure_attempt_id == (
        request.infrastructure_attempt_id
    )
    assert config.cleanup_authority_pin.launch_allowed is False
    assert config.cleanup_signing_key.path == request.target_cleanup_key_path


def test_exclusive_publisher_replays_only_identical_custody(tmp_path: Path) -> None:
    target = tmp_path / "published.bin"
    payload = b"reviewed commissioning bytes"
    uid = os.geteuid()
    gid = os.getegid()

    assert commissioning._publish_exclusive(  # noqa: SLF001
        target,
        payload,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    assert not commissioning._publish_exclusive(  # noqa: SLF001
        target,
        payload,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    with pytest.raises(PreRuntimeCleanupCommissioningError, match="custody differs"):
        commissioning._publish_exclusive(  # noqa: SLF001
            target,
            b"variant",
            uid=uid,
            gid=gid,
            mode=0o400,
        )


def test_exclusive_publisher_recovers_each_closed_crash_residue(tmp_path: Path) -> None:
    payload = b"sealed commissioning recovery bytes"
    uid = os.geteuid()
    gid = os.getegid()

    unsealed_target = tmp_path / "unsealed.bin"
    unsealed_pending = tmp_path / ".unsealed.bin.pending"
    unsealed_pending.write_bytes(b"partial")
    unsealed_pending.chmod(0o600)
    assert commissioning._publish_exclusive(  # noqa: SLF001
        unsealed_target,
        payload,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    assert unsealed_target.read_bytes() == payload
    assert not unsealed_pending.exists()

    sealed_target = tmp_path / "sealed.bin"
    sealed_pending = tmp_path / ".sealed.bin.pending"
    sealed_pending.write_bytes(payload)
    sealed_pending.chmod(0o400)
    assert commissioning._publish_exclusive(  # noqa: SLF001
        sealed_target,
        payload,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    assert sealed_target.read_bytes() == payload
    assert not sealed_pending.exists()

    linked_target = tmp_path / "linked.bin"
    linked_pending = tmp_path / ".linked.bin.pending"
    linked_pending.write_bytes(payload)
    linked_pending.chmod(0o400)
    os.link(linked_pending, linked_target)
    assert not commissioning._publish_exclusive(  # noqa: SLF001
        linked_target,
        payload,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    assert linked_target.read_bytes() == payload
    assert linked_target.stat().st_nlink == 1
    assert not linked_pending.exists()
