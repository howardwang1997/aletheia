#!/usr/bin/env python3
"""Minimal in-container launch gate for qualification-only OCI workloads.

The host runtime mounts two immutable canonical JSON files into the container: one contains a
short-lived Ed25519 runtime authorization and the other contains the current fence/token control
journal.  This gate verifies both with the suspend-aware Linux boot clock and then replaces itself
with the exact digest-pinned workload executable.  It deliberately has no database, network, or
artifact-store client.

The module is self-contained so a qualification image can copy this single file to the pinned
``launch_gate_path``.  ``cryptography`` is its only non-stdlib runtime dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_RUNTIME_CONTROL_DOMAIN = b"ALETHEIA_RUNTIME_CONTROL_V2\x00"
_MAX_CONTROL_BYTES = 1 << 20
_SHA256_LENGTH = 64
_SIGNATURE_LENGTH = 128

_REQUEST_REQUIRED_KEYS = {
    "schema_name",
    "schema_version",
    "request_nonce_sha256",
    "runtime_preparation_sha256",
    "infrastructure_attempt_id",
    "fencing_epoch",
    "lease_token_sha256",
    "pre_runtime_absence_epoch",
    "requested_at",
    "requested_monotonic_ns",
    "qualification_only",
    "scientific_admission_allowed",
}
_REQUEST_OPTIONAL_KEYS = {"pre_runtime_absence_receipt_sha256"}
_AUTHORIZATION_KEYS = {
    "schema_name",
    "schema_version",
    "admission_sha256",
    "qualification_grant_sha256",
    "node_manifest_sha256",
    "node_id",
    "boot_id",
    "execution_id",
    "infrastructure_attempt_id",
    "intent_sha256",
    "runtime_preparation_sha256",
    "authorization_request_sha256",
    "launch_spec_sha256",
    "oci_config_sha256",
    "workload_executable_sha256",
    "workload_argv",
    "enforced_placement_sha256",
    "input_materialization_receipt_sha256",
    "fencing_epoch",
    "lease_token_sha256",
    "lease_expires_at",
    "hard_deadline",
    "issued_at",
    "expires_at",
    "max_launch_delay_ns",
    "runtime_control_policy_sha256",
    "authorized_by_principal_id",
    "authorization_key_id",
    "signature_ed25519_hex",
    "qualification_only",
    "scientific_admission_allowed",
}
_PIN_REQUIRED_KEYS = {
    "schema_name",
    "schema_version",
    "policy_sha256",
    "principal_id",
    "key_id",
    "public_key_ed25519_hex",
    "valid_from",
    "expires_at",
    "qualification_only",
    "scientific_admission_allowed",
}
_PIN_OPTIONAL_KEYS = {"revoked_at"}
_CONTROL_REQUIRED_KEYS = {
    "preparation_sha256",
    "sequence",
    "fencing_epoch",
    "lease_token_sha256",
    "enforced_placement_sha256",
    "device_fences",
    "device_fence_evidence_sha256",
}
_CONTROL_OPTIONAL_KEYS = {
    "runtime_identity_sha256",
    "previous_runtime_control_journal_sha256",
}


class LaunchGateRejected(RuntimeError):
    """The mounted authority, current fence, time window, or workload is unsafe."""


def _without_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _without_none(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256 = _canonical_sha256(
    {
        "schema": "aletheia.qualification_launch_gate_protocol.v1",
        "authorization_schema": "aletheia.runtime_launch_authorization.v2",
        "authorization_request_schema": ("aletheia.runtime_launch_authorization_request.v2"),
        "clock": "CLOCK_BOOTTIME",
        "signature_domain_hex": _RUNTIME_CONTROL_DOMAIN.hex(),
        "control_fields": (
            "preparation_sha256",
            "fencing_epoch",
            "lease_token_sha256",
            "enforced_placement_sha256",
        ),
        "exec": "linux-fd-execve",
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LaunchGateRejected("control JSON contains a duplicate key")
        result[key] = value
    return result


def _read_exact_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise LaunchGateRejected(f"{label} path is not canonical and absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LaunchGateRejected(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_size < 2
            or before.st_size > _MAX_CONTROL_BYTES
        ):
            raise LaunchGateRejected(f"{label} custody metadata is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise LaunchGateRejected(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LaunchGateRejected(f"{label} grew while being read")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise LaunchGateRejected(f"{label} changed while being read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    except (LaunchGateRejected, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchGateRejected(f"{label} is not valid canonical JSON") from exc
    if not isinstance(decoded, dict) or _canonical_json_bytes(decoded) != payload:
        raise LaunchGateRejected(f"{label} is not canonical JSON")
    return decoded


def _require_dict(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LaunchGateRejected(f"{label} is not an object")
    return value


def _require_exact_keys(value: dict[str, Any], keys: set[str], *, label: str) -> None:
    if set(value) != keys:
        raise LaunchGateRejected(f"{label} fields differ from the closed schema")


def _require_keys_with_optional(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    keys = set(value)
    if not required.issubset(keys) or keys - required - optional:
        raise LaunchGateRejected(f"{label} fields differ from the closed schema")


def _require_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LaunchGateRejected(f"{label} is not a lowercase SHA-256 digest")
    return value


def _require_int(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise LaunchGateRejected(f"{label} is not an exact integer")
    return value


def _require_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise LaunchGateRejected(f"{label} is not canonical text")
    return value


def _require_utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise LaunchGateRejected(f"{label} is not an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LaunchGateRejected(f"{label} is not an RFC 3339 timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
    ):
        raise LaunchGateRejected(f"{label} is not timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _runtime_control_message(kind: str, payload: dict[str, Any]) -> bytes:
    return _RUNTIME_CONTROL_DOMAIN + kind.encode("ascii") + b"\x00" + _canonical_json_bytes(payload)


def _boottime_ns() -> int:
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise LaunchGateRejected("qualification launch gate requires Linux CLOCK_BOOTTIME")
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class VerifiedLaunch:
    workload_path: Path
    workload_argv: tuple[str, ...]
    preparation_sha256: str
    fencing_epoch: int
    lease_token_sha256: str
    authorization_expires_at: datetime
    requested_boottime_ns: int
    max_launch_delay_ns: int

    def require_fresh(
        self,
        *,
        observed_at: datetime | None = None,
        observed_boottime_ns: int | None = None,
    ) -> None:
        """Recheck the ticket immediately before the fd-based exec mutation."""

        now = observed_at or datetime.now(timezone.utc)
        boottime_ns = observed_boottime_ns if observed_boottime_ns is not None else _boottime_ns()
        if (
            now.tzinfo is None
            or now.utcoffset() is None
            or now.utcoffset().total_seconds() != 0
            or type(boottime_ns) is not int
            or boottime_ns < self.requested_boottime_ns
            or boottime_ns - self.requested_boottime_ns >= self.max_launch_delay_ns
            or not now < self.authorization_expires_at
        ):
            raise LaunchGateRejected(
                "runtime launch authority expired before the workload exec mutation"
            )


def verify_launch(
    *,
    authorization_path: Path,
    runtime_control_path: Path,
    authority_policy_sha256: str,
    authority_key_id: str,
    authority_public_key_ed25519_hex: str,
    launch_gate_protocol_sha256: str,
    workload_executable_sha256: str,
    workload_argv: tuple[str, ...],
    launch_gate_executable_path: Path,
    observed_at: datetime | None = None,
    observed_boottime_ns: int | None = None,
) -> VerifiedLaunch:
    """Verify mounted authority and return the exact fd-exec workload scope."""

    _require_digest(authority_policy_sha256, label="authority policy")
    _require_digest(authority_key_id, label="authority key id")
    _require_digest(launch_gate_protocol_sha256, label="launch gate protocol")
    _require_digest(workload_executable_sha256, label="workload executable")
    if launch_gate_protocol_sha256 != QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256:
        raise LaunchGateRejected("launch gate protocol differs from this executable")
    if len(authority_public_key_ed25519_hex) != 64 or any(
        character not in "0123456789abcdef" for character in authority_public_key_ed25519_hex
    ):
        raise LaunchGateRejected("runtime-control public key is not canonical Ed25519")
    public_key = bytes.fromhex(authority_public_key_ed25519_hex)
    if hashlib.sha256(public_key).hexdigest() != authority_key_id:
        raise LaunchGateRejected("runtime-control key id differs from its public key")
    if not workload_argv or not workload_argv[0].startswith("/"):
        raise LaunchGateRejected("workload argv must begin with an absolute executable")
    if any(not item or "\x00" in item or "\n" in item or "\r" in item for item in workload_argv):
        raise LaunchGateRejected("workload argv contains an unsafe token")

    journal = _read_exact_json(authorization_path, label="launch authorization")
    control = _read_exact_json(runtime_control_path, label="runtime control")
    _require_exact_keys(
        journal,
        {
            "preparation_sha256",
            "authorization_request",
            "authorization_request_sha256",
            "authorization",
            "runtime_launch_authorization_sha256",
            "runtime_control_authority",
            "launch_gate_executable_sha256",
            "launch_gate_protocol_sha256",
            "published_at",
            "published_boottime_ns",
        },
        label="launch authorization journal",
    )
    request = _require_dict(journal["authorization_request"], label="authorization request")
    authorization = _require_dict(journal["authorization"], label="authorization")
    pin = _require_dict(journal["runtime_control_authority"], label="runtime-control pin")
    _require_keys_with_optional(
        request,
        required=_REQUEST_REQUIRED_KEYS,
        optional=_REQUEST_OPTIONAL_KEYS,
        label="authorization request",
    )
    _require_exact_keys(authorization, _AUTHORIZATION_KEYS, label="authorization")
    _require_keys_with_optional(
        pin,
        required=_PIN_REQUIRED_KEYS,
        optional=_PIN_OPTIONAL_KEYS,
        label="runtime-control pin",
    )
    _require_keys_with_optional(
        control,
        required=_CONTROL_REQUIRED_KEYS,
        optional=_CONTROL_OPTIONAL_KEYS,
        label="runtime control",
    )
    expected_request_hash = _canonical_sha256(request)
    expected_authorization_hash = _canonical_sha256(authorization)
    preparation_sha256 = _require_digest(journal["preparation_sha256"], label="preparation")
    if (
        journal["authorization_request_sha256"] != expected_request_hash
        or journal["runtime_launch_authorization_sha256"] != expected_authorization_hash
        or journal["launch_gate_protocol_sha256"] != launch_gate_protocol_sha256
        or request.get("runtime_preparation_sha256") != preparation_sha256
        or authorization.get("runtime_preparation_sha256") != preparation_sha256
        or authorization.get("authorization_request_sha256") != expected_request_hash
    ):
        raise LaunchGateRejected("launch journal changed request or authorization identity")
    launch_gate_executable_sha256 = _require_digest(
        journal["launch_gate_executable_sha256"], label="launch gate executable"
    )
    gate_descriptor = _open_verified_workload(
        launch_gate_executable_path,
        launch_gate_executable_sha256,
    )
    os.close(gate_descriptor)

    if (
        pin.get("schema_name") != "aletheia.runtime_control_authority_pin"
        or pin.get("schema_version") != 2
        or pin.get("policy_sha256") != authority_policy_sha256
        or pin.get("key_id") != authority_key_id
        or pin.get("public_key_ed25519_hex") != authority_public_key_ed25519_hex
        or pin.get("qualification_only") is not True
        or pin.get("scientific_admission_allowed") is not False
        or authorization.get("schema_name") != "aletheia.runtime_launch_authorization"
        or authorization.get("schema_version") != 2
        or authorization.get("runtime_control_policy_sha256") != authority_policy_sha256
        or authorization.get("authorized_by_principal_id") != pin.get("principal_id")
        or authorization.get("authorization_key_id") != authority_key_id
        or authorization.get("qualification_only") is not True
        or authorization.get("scientific_admission_allowed") is not False
        or request.get("schema_name") != "aletheia.runtime_launch_authorization_request"
        or request.get("schema_version") != 2
        or request.get("qualification_only") is not True
        or request.get("scientific_admission_allowed") is not False
    ):
        raise LaunchGateRejected("launch authority differs from the deployment pin")

    signature = authorization.get("signature_ed25519_hex")
    if (
        not isinstance(signature, str)
        or len(signature) != _SIGNATURE_LENGTH
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise LaunchGateRejected("runtime launch authorization signature is malformed")
    signature_payload = {
        key: value for key, value in authorization.items() if key != "signature_ed25519_hex"
    }
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(signature),
            _runtime_control_message("runtime_launch_authorization", signature_payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise LaunchGateRejected("runtime launch authorization signature is invalid") from exc

    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None or now.utcoffset().total_seconds() != 0:
        raise LaunchGateRejected("launch gate clock is not timezone-aware UTC")
    boottime_ns = observed_boottime_ns if observed_boottime_ns is not None else _boottime_ns()
    if type(boottime_ns) is not int or boottime_ns < 0:
        raise LaunchGateRejected("launch gate boot clock is invalid")
    requested_at = _require_utc(request.get("requested_at"), label="request time")
    issued_at = _require_utc(authorization.get("issued_at"), label="ticket issuance")
    expires_at = _require_utc(authorization.get("expires_at"), label="ticket expiry")
    lease_expires_at = _require_utc(authorization.get("lease_expires_at"), label="lease expiry")
    hard_deadline = _require_utc(authorization.get("hard_deadline"), label="hard deadline")
    pin_valid_from = _require_utc(pin.get("valid_from"), label="authority validity")
    pin_expires_at = _require_utc(pin.get("expires_at"), label="authority expiry")
    pin_revoked = pin.get("revoked_at")
    pin_active_until = min(
        pin_expires_at,
        _require_utc(pin_revoked, label="authority revocation")
        if pin_revoked is not None
        else pin_expires_at,
    )
    published_at = _require_utc(journal["published_at"], label="journal publication")
    requested_boottime_ns = _require_int(
        request.get("requested_monotonic_ns"), label="request boot time"
    )
    published_boottime_ns = _require_int(
        journal["published_boottime_ns"], label="publication boot time"
    )
    max_launch_delay_ns = _require_int(
        authorization.get("max_launch_delay_ns"),
        label="maximum launch delay",
        minimum=1,
        maximum=60_000_000_000,
    )
    for key in (
        "request_nonce_sha256",
        "runtime_preparation_sha256",
        "lease_token_sha256",
    ):
        _require_digest(request.get(key), label=f"request {key}")
    optional_absence = request.get("pre_runtime_absence_receipt_sha256")
    absence_epoch = _require_int(
        request.get("pre_runtime_absence_epoch"), label="pre-runtime absence epoch"
    )
    if optional_absence is not None:
        _require_digest(optional_absence, label="pre-runtime absence receipt")
    if (absence_epoch == 0) != (optional_absence is None):
        raise LaunchGateRejected("pre-runtime absence epoch and receipt are inconsistent")
    _require_text(request.get("infrastructure_attempt_id"), label="request attempt id")
    for key in (
        "admission_sha256",
        "qualification_grant_sha256",
        "node_manifest_sha256",
        "intent_sha256",
        "runtime_preparation_sha256",
        "authorization_request_sha256",
        "launch_spec_sha256",
        "oci_config_sha256",
        "workload_executable_sha256",
        "enforced_placement_sha256",
        "input_materialization_receipt_sha256",
        "lease_token_sha256",
        "runtime_control_policy_sha256",
        "authorization_key_id",
    ):
        _require_digest(authorization.get(key), label=f"authorization {key}")
    authorized_argv = authorization.get("workload_argv")
    if (
        not isinstance(authorized_argv, list)
        or not authorized_argv
        or len(authorized_argv) > 256
        or any(
            not isinstance(item, str)
            or not item
            or any(character in item for character in ("\x00", "\n", "\r"))
            for item in authorized_argv
        )
    ):
        raise LaunchGateRejected("authorization workload argv is not canonical")
    for key in (
        "node_id",
        "boot_id",
        "execution_id",
        "infrastructure_attempt_id",
        "authorized_by_principal_id",
    ):
        _require_text(authorization.get(key), label=f"authorization {key}")
    _require_int(authorization.get("fencing_epoch"), label="authorization fence", minimum=1)
    _require_text(pin.get("principal_id"), label="runtime-control principal")
    _require_digest(pin.get("policy_sha256"), label="runtime-control policy")
    _require_digest(pin.get("key_id"), label="runtime-control key")
    if not (
        pin_valid_from <= issued_at <= published_at <= now
        and issued_at < expires_at <= lease_expires_at <= hard_deadline
        and expires_at <= pin_active_until
        and requested_at <= issued_at
        and requested_boottime_ns <= published_boottime_ns <= boottime_ns
        and boottime_ns - requested_boottime_ns < max_launch_delay_ns
        and now < expires_at
    ):
        raise LaunchGateRejected("runtime launch authority is stale, delayed, or time-reordered")

    fencing_epoch = _require_int(request.get("fencing_epoch"), label="request fence", minimum=1)
    lease_token_sha256 = _require_digest(
        request.get("lease_token_sha256"), label="request lease token"
    )
    if (
        authorization.get("infrastructure_attempt_id") != request.get("infrastructure_attempt_id")
        or authorization.get("fencing_epoch") != fencing_epoch
        or authorization.get("lease_token_sha256") != lease_token_sha256
        or authorization.get("workload_executable_sha256") != workload_executable_sha256
        or authorized_argv != list(workload_argv)
        or control.get("preparation_sha256") != preparation_sha256
        or control.get("fencing_epoch") != fencing_epoch
        or control.get("lease_token_sha256") != lease_token_sha256
        or control.get("enforced_placement_sha256")
        != authorization.get("enforced_placement_sha256")
        or control.get("runtime_identity_sha256") is not None
        or control.get("sequence") != 0
        or control.get("previous_runtime_control_journal_sha256") is not None
        or control.get("device_fences") != []
    ):
        raise LaunchGateRejected("runtime control journal differs from the initial ticket")
    _require_digest(control.get("device_fence_evidence_sha256"), label="device fence evidence")
    _require_int(control.get("fencing_epoch"), label="runtime control fence", minimum=1)
    _require_int(control.get("sequence"), label="runtime control sequence")

    return VerifiedLaunch(
        workload_path=Path(workload_argv[0]),
        workload_argv=workload_argv,
        preparation_sha256=preparation_sha256,
        fencing_epoch=fencing_epoch,
        lease_token_sha256=lease_token_sha256,
        authorization_expires_at=expires_at,
        requested_boottime_ns=requested_boottime_ns,
        max_launch_delay_ns=max_launch_delay_ns,
    )


def _open_verified_workload(path: Path, expected_sha256: str) -> int:
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise LaunchGateRejected("workload executable path is not canonical and absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LaunchGateRejected("workload executable cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not before.st_mode & stat.S_IXUSR
        ):
            raise LaunchGateRejected("workload executable custody metadata is unsafe")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if digest.hexdigest() != expected_sha256 or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            raise LaunchGateRejected("workload executable bytes changed or differ from its pin")
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _exec_verified_workload(*, verified: VerifiedLaunch, expected_sha256: str) -> NoReturn:
    descriptor = _open_verified_workload(verified.workload_path, expected_sha256)
    if os.execve not in os.supports_fd:
        os.close(descriptor)
        raise LaunchGateRejected("Linux fd-based execve is unavailable")
    environment = dict(os.environ)
    os.umask(0o077)
    verified.require_fresh()
    os.execve(descriptor, verified.workload_argv, environment)
    raise AssertionError("execve returned unexpectedly")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--runtime-control", required=True, type=Path)
    parser.add_argument("--authority-policy-sha256", required=True)
    parser.add_argument("--authority-key-id", required=True)
    parser.add_argument("--authority-public-key-ed25519-hex", required=True)
    parser.add_argument("--launch-gate-protocol-sha256", required=True)
    parser.add_argument("--workload-executable-sha256", required=True)
    parser.add_argument("--clock", choices=("CLOCK_BOOTTIME",), required=True)
    parser.add_argument("workload_argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> NoReturn:
    arguments = _parser().parse_args(argv)
    workload = tuple(arguments.workload_argv)
    if workload and workload[0] == "--":
        workload = workload[1:]
    try:
        verified = verify_launch(
            authorization_path=arguments.authorization,
            runtime_control_path=arguments.runtime_control,
            authority_policy_sha256=arguments.authority_policy_sha256,
            authority_key_id=arguments.authority_key_id,
            authority_public_key_ed25519_hex=(arguments.authority_public_key_ed25519_hex),
            launch_gate_protocol_sha256=arguments.launch_gate_protocol_sha256,
            workload_executable_sha256=arguments.workload_executable_sha256,
            workload_argv=workload,
            launch_gate_executable_path=Path(sys.argv[0]),
        )
        _exec_verified_workload(
            verified=verified,
            expected_sha256=arguments.workload_executable_sha256,
        )
    except LaunchGateRejected as exc:
        print(f"qualification launch rejected: {exc}", file=sys.stderr)
        raise SystemExit(126) from exc


if __name__ == "__main__":
    main()
