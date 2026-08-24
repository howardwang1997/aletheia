from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.execution.oci_runtime import (
    _LaunchGateAuthorizationJournal,
    _RuntimeControlJournal,
)
from aletheia.execution.qualification_launch_gate import (
    QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
    LaunchGateRejected,
    _exec_verified_workload,
    _open_verified_workload,
    _runtime_control_message,
    verify_launch,
)
from aletheia.execution import qualification_launch_gate as launch_gate_module
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_v2_contracts import (
    RuntimeControlAuthorityPin,
    RuntimeLaunchAuthorizationRequest,
    RuntimePreparation,
    issue_runtime_launch_authorization,
)
from aletheia.execution.schemas import canonical_json_bytes

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    gate_source = Path(launch_gate_module.__file__)
    gate_path = tmp_path / "qualification-launch-gate"
    gate_path.write_bytes(gate_source.read_bytes())
    gate_path.chmod(0o555)
    gate_digest = hashlib.sha256(gate_path.read_bytes()).hexdigest()
    private_key = bytes(range(1, 33))
    public_key = (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    pin = RuntimeControlAuthorityPin(
        policy_sha256=_digest("runtime-policy"),
        principal_id="principal:runtime-control",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
    )
    preparation = RuntimePreparation(
        node_manifest_sha256=_digest("node-manifest"),
        node_id="node.local",
        boot_id="boot.local",
        execution_id=f"exe_{'1' * 32}",
        infrastructure_attempt_id=f"iat_{'2' * 32}",
        intent_sha256=_digest("intent"),
        runtime_id="runtime.local",
        runtime_engine="docker",
        launch_spec_sha256=_digest("launch-spec"),
        workload_executable_sha256=_digest("workload"),
        workload_argv=("/usr/local/bin/workload", "--exact"),
        runtime_request_sha256=_digest("runtime-request"),
        enforced_placement_sha256=_digest("placement"),
        input_materialization_receipt_sha256=_digest("inputs"),
        output_quota_provisioning_receipt_sha256=_digest("output-quota"),
        fencing_epoch=1,
        lease_token_sha256=_digest("lease-token"),
        prepared_runtime_locator_sha256=_digest("runtime-locator"),
        oci_config_sha256=_digest("oci-config"),
        prepared_at=NOW,
        prepared_monotonic_ns=1_000_000,
    )
    request = RuntimeLaunchAuthorizationRequest(
        request_nonce_sha256=_digest("nonce"),
        runtime_preparation_sha256=preparation.preparation_sha256,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        requested_at=NOW + timedelta(milliseconds=1),
        requested_monotonic_ns=2_000_000,
    )
    authorization = issue_runtime_launch_authorization(
        pin=pin,
        private_key=private_key,
        admission_sha256=_digest("admission"),
        qualification_grant_sha256=_digest("grant"),
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
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        lease_expires_at=NOW + timedelta(minutes=5),
        hard_deadline=NOW + timedelta(minutes=10),
        issued_at=NOW + timedelta(milliseconds=2),
        expires_at=NOW + timedelta(seconds=5),
        max_launch_delay_ns=5_000_000_000,
    )
    journal = _LaunchGateAuthorizationJournal(
        preparation_sha256=preparation.preparation_sha256,
        authorization_request=request,
        authorization_request_sha256=request.request_sha256,
        authorization=authorization,
        runtime_launch_authorization_sha256=authorization.authorization_sha256,
        runtime_control_authority=pin,
        launch_gate_executable_sha256=gate_digest,
        launch_gate_protocol_sha256=QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
        published_at=NOW + timedelta(milliseconds=3),
        published_boottime_ns=3_000_000,
    )
    control = _RuntimeControlJournal(
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity_sha256=None,
        sequence=0,
        fencing_epoch=1,
        lease_token_sha256=preparation.lease_token_sha256,
        enforced_placement_sha256=preparation.enforced_placement_sha256,
        device_fences=(),
        device_fence_evidence_sha256=_digest("empty-device-fence"),
        previous_runtime_control_journal_sha256=None,
    )
    authorization_path = tmp_path / "launch-authorization.json"
    control_path = tmp_path / "current.json"
    authorization_path.write_bytes(canonical_json_bytes(journal))
    control_path.write_bytes(canonical_json_bytes(control))
    authorization_path.chmod(0o400)
    control_path.chmod(0o400)
    scope: dict[str, object] = {
        "authorization_path": authorization_path,
        "runtime_control_path": control_path,
        "authority_policy_sha256": pin.policy_sha256,
        "authority_key_id": pin.key_id,
        "authority_public_key_ed25519_hex": pin.public_key_ed25519_hex,
        "launch_gate_protocol_sha256": QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
        "workload_executable_sha256": _digest("workload"),
        "workload_argv": ("/usr/local/bin/workload", "--exact"),
        "launch_gate_executable_path": gate_path,
        "observed_at": NOW + timedelta(milliseconds=4),
        "observed_boottime_ns": 4_000_000,
    }
    return scope, {
        "journal": journal,
        "control": control,
        "authorization_path": authorization_path,
        "control_path": control_path,
    }


def test_gate_verifies_exact_signed_ticket_fence_and_boot_clock(tmp_path: Path) -> None:
    scope, values = _fixture(tmp_path)

    verified = verify_launch(**scope)  # type: ignore[arg-type]

    journal = values["journal"]
    assert isinstance(journal, _LaunchGateAuthorizationJournal)
    assert verified.preparation_sha256 == journal.preparation_sha256
    assert verified.fencing_epoch == 1
    assert verified.workload_argv == ("/usr/local/bin/workload", "--exact")
    verified.require_fresh(
        observed_at=NOW + timedelta(seconds=1),
        observed_boottime_ns=1_002_000_000,
    )
    with pytest.raises(LaunchGateRejected, match="expired before"):
        verified.require_fresh(
            observed_at=NOW + timedelta(seconds=6),
            observed_boottime_ns=6_002_000_000,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "signature",
        "fence",
        "protocol",
        "gate-digest",
        "control-schema",
        "delayed",
        "noncanonical",
        "writable",
    ],
)
def test_gate_rejects_rebound_stale_or_unsafe_authority(
    tmp_path: Path,
    mutation: str,
) -> None:
    scope, values = _fixture(tmp_path)
    authorization_path = values["authorization_path"]
    control_path = values["control_path"]
    assert isinstance(authorization_path, Path)
    assert isinstance(control_path, Path)
    if mutation == "signature":
        journal = values["journal"]
        assert isinstance(journal, _LaunchGateAuthorizationJournal)
        rebound = journal.model_copy(
            update={
                "authorization": journal.authorization.model_copy(
                    update={"signature_ed25519_hex": "0" * 128}
                ),
            }
        )
        authorization_path.chmod(0o600)
        authorization_path.write_bytes(canonical_json_bytes(rebound))
        authorization_path.chmod(0o400)
    elif mutation == "fence":
        control = values["control"]
        assert isinstance(control, _RuntimeControlJournal)
        control_path.chmod(0o600)
        control_path.write_bytes(
            canonical_json_bytes(control.model_copy(update={"fencing_epoch": 2}))
        )
        control_path.chmod(0o400)
    elif mutation == "protocol":
        scope["launch_gate_protocol_sha256"] = _digest("another-protocol")
    elif mutation == "gate-digest":
        journal = values["journal"]
        assert isinstance(journal, _LaunchGateAuthorizationJournal)
        authorization_path.chmod(0o600)
        authorization_path.write_bytes(
            canonical_json_bytes(
                journal.model_copy(update={"launch_gate_executable_sha256": _digest("other-gate")})
            )
        )
        authorization_path.chmod(0o400)
    elif mutation == "control-schema":
        payload = json.loads(control_path.read_bytes())
        payload["sequence"] = False
        payload["unexpected_field"] = "attacker"
        payload.pop("device_fence_evidence_sha256")
        control_path.chmod(0o600)
        control_path.write_bytes(canonical_json_bytes(payload))
        control_path.chmod(0o400)
    elif mutation == "delayed":
        scope["observed_boottime_ns"] = 5_002_000_000
    elif mutation == "noncanonical":
        authorization_path.chmod(0o600)
        authorization_path.write_bytes(authorization_path.read_bytes() + b"\n")
        authorization_path.chmod(0o400)
    else:
        control_path.chmod(0o600)

    with pytest.raises(LaunchGateRejected):
        verify_launch(**scope)  # type: ignore[arg-type]


def test_workload_executable_is_rehashed_from_one_open_descriptor() -> None:
    executable = Path(os.path.realpath(os.sys.executable))
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()

    descriptor = _open_verified_workload(executable, digest)
    try:
        assert os.fstat(descriptor).st_ino == executable.stat().st_ino
    finally:
        os.close(descriptor)

    with pytest.raises(LaunchGateRejected, match="bytes changed|differ"):
        _open_verified_workload(executable, "0" * 64)


def test_exec_rehashes_then_rechecks_freshness_immediately_before_fd_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    descriptor = os.open(os.path.realpath(os.sys.executable), os.O_RDONLY)

    class _Verified:
        workload_path = Path(os.path.realpath(os.sys.executable))
        workload_argv = (str(workload_path), "--version")

        @staticmethod
        def require_fresh() -> None:
            events.append("fresh")

    def _open(path: Path, digest: str) -> int:
        assert path == _Verified.workload_path
        assert digest == _digest("workload")
        events.append("open")
        return descriptor

    class _ExecCalled(RuntimeError):
        pass

    def _execve(fd: int, argv: tuple[str, ...], environment: dict[str, str]) -> None:
        assert fd == descriptor
        assert argv == _Verified.workload_argv
        assert environment
        events.append("exec")
        raise _ExecCalled

    monkeypatch.setattr(launch_gate_module, "_open_verified_workload", _open)
    monkeypatch.setattr(launch_gate_module.os, "execve", _execve)
    monkeypatch.setattr(launch_gate_module.os, "supports_fd", {_execve})
    monkeypatch.setattr(launch_gate_module.os, "umask", lambda mode: events.append("umask"))

    with pytest.raises(_ExecCalled):
        _exec_verified_workload(  # type: ignore[arg-type]
            verified=_Verified(),
            expected_sha256=_digest("workload"),
        )

    assert events == ["open", "umask", "fresh", "exec"]


@pytest.mark.parametrize("field", ["workload_executable_sha256", "workload_argv"])
def test_gate_rejects_workload_projection_outside_the_signed_ticket(
    tmp_path: Path,
    field: str,
) -> None:
    scope, _ = _fixture(tmp_path)
    if field == "workload_executable_sha256":
        scope[field] = _digest("substituted-workload")
    else:
        scope[field] = ("/usr/local/bin/substituted", "--attacker")

    with pytest.raises(LaunchGateRejected, match="runtime control journal differs"):
        verify_launch(**scope)  # type: ignore[arg-type]


def test_gate_rejects_signed_launch_delay_outside_the_v2_contract(tmp_path: Path) -> None:
    scope, values = _fixture(tmp_path)
    authorization_path = values["authorization_path"]
    assert isinstance(authorization_path, Path)
    journal = json.loads(authorization_path.read_bytes())
    authorization = journal["authorization"]
    authorization["max_launch_delay_ns"] = 60_000_000_001
    payload = {key: value for key, value in authorization.items() if key != "signature_ed25519_hex"}
    authorization["signature_ed25519_hex"] = (
        Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        .sign(_runtime_control_message("runtime_launch_authorization", payload))
        .hex()
    )
    journal["runtime_launch_authorization_sha256"] = hashlib.sha256(
        canonical_json_bytes(authorization)
    ).hexdigest()
    authorization_path.chmod(0o600)
    authorization_path.write_bytes(canonical_json_bytes(journal))
    authorization_path.chmod(0o400)

    with pytest.raises(LaunchGateRejected, match="maximum launch delay"):
        verify_launch(**scope)  # type: ignore[arg-type]
