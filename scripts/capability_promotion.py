#!/usr/bin/env python3
"""Audit, promote, verify, and inspect F10-S7 signed capability updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from aletheia.capabilities import (
    CapabilityPromotionPolicy,
    CapabilityPromotionRequest,
    CapabilityRegistrySnapshot,
    SignedCapabilityPromotionAudit,
    SignedCapabilityRegistryUpdate,
    audit_capability_promotion,
    build_capability_promotion_readiness_audit,
    promote_capability_registry,
    verify_capability_registry_update,
)


_KEY_ID = re.compile(r"^[0-9a-f]{64}$")


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset or Z")
    return parsed


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace frozen promotion artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to replace frozen promotion artifact: {destination}"
            ) from exc
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _print(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _private_key(path: Path) -> bytes:
    requested = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        if requested.is_symlink():
            raise ValueError("promotion signing key cannot be a symlink") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("promotion signing key must be a regular file")
        if metadata.st_uid != os.getuid():
            raise PermissionError("promotion signing key must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("promotion signing key must not be group/world accessible")
        payload = os.read(descriptor, 129)
        if os.read(descriptor, 1):
            raise ValueError("promotion signing key file is unexpectedly large")
    finally:
        os.close(descriptor)
    if len(payload) == 32:
        return payload
    stripped = payload.strip()
    if len(stripped) == 64:
        try:
            decoded = bytes.fromhex(stripped.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                "promotion signing key is not raw or hexadecimal Ed25519 data"
            ) from exc
        if len(decoded) == 32:
            return decoded
    raise ValueError("promotion signing key must contain 32 raw bytes or 64 hexadecimal digits")


def _signers(values: list[str]) -> dict[str, bytes]:
    signers: dict[str, bytes] = {}
    for value in values:
        key_id, separator, raw_path = value.partition("=")
        if not separator or not _KEY_ID.fullmatch(key_id) or not raw_path:
            raise ValueError("signing keys must use KEY_ID=/owner-only/path syntax")
        if key_id in signers:
            raise ValueError("promotion signing key id was supplied twice")
        signers[key_id] = _private_key(Path(raw_path))
    if not signers:
        raise ValueError("at least one promotion signing key is required")
    return signers


def _readiness(args: argparse.Namespace) -> int:
    registry = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    audit = build_capability_promotion_readiness_audit(
        audit_id=args.audit_id,
        registry=registry,
        audited_at=args.audited_at,
    )
    _print(audit)
    return 0 if audit.production_promotion_ready or not args.require_ready else 2


def _audit(args: argparse.Namespace) -> int:
    registry = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    policy = CapabilityPromotionPolicy.model_validate(_read(args.policy))
    request = CapabilityPromotionRequest.model_validate(_read(args.request))
    signed = audit_capability_promotion(
        snapshot=registry,
        policy=policy,
        request=request,
        auditor_private_keys=_signers(args.auditor_key),
        audited_at=args.audited_at,
    )
    destination = _atomic_new_json(args.output, signed)
    _print(
        {
            "output": str(destination),
            "decision": signed.audit.decision.value,
            "blockers": signed.audit.blockers,
            "audit_receipt_sha256": signed.audit.receipt_sha256,
            "audit_envelope_sha256": signed.envelope_sha256,
        }
    )
    return 0 if signed.audit.decision.value == "approved" else 3


def _promote(args: argparse.Namespace) -> int:
    source = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    policy = CapabilityPromotionPolicy.model_validate(_read(args.policy))
    request = CapabilityPromotionRequest.model_validate(_read(args.request))
    signed_audit = SignedCapabilityPromotionAudit.model_validate(_read(args.audit))
    update = promote_capability_registry(
        source_snapshot=source,
        policy=policy,
        request=request,
        signed_audit=signed_audit,
        promoter_private_keys=_signers(args.promoter_key),
        promoted_at=args.promoted_at,
    )
    destination = _atomic_new_json(args.output, update)
    _print(
        {
            "output": str(destination),
            "source_registry_sha256": source.snapshot_sha256,
            "target_registry_sha256": update.target_snapshot.snapshot_sha256,
            "promotion_receipt_sha256": update.promotion_receipt.receipt_sha256,
            "signed_update_sha256": update.update_sha256,
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    source = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    policy = CapabilityPromotionPolicy.model_validate(_read(args.policy))
    request = CapabilityPromotionRequest.model_validate(_read(args.request))
    signed_audit = SignedCapabilityPromotionAudit.model_validate(_read(args.audit))
    update = SignedCapabilityRegistryUpdate.model_validate(_read(args.update))
    target = verify_capability_registry_update(
        update=update,
        source_snapshot=source,
        policy=policy,
        request=request,
        signed_audit=signed_audit,
    )
    _print(
        {
            "verified": True,
            "registry_id": target.registry_id,
            "source_registry_sha256": source.snapshot_sha256,
            "target_registry_sha256": target.snapshot_sha256,
            "signed_update_sha256": update.update_sha256,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    readiness = subparsers.add_parser("readiness", help="audit unsigned production gaps")
    readiness.add_argument("--registry", type=Path, required=True)
    readiness.add_argument("--audit-id", required=True)
    readiness.add_argument("--audited-at", type=_aware_datetime, required=True)
    readiness.add_argument("--require-ready", action="store_true")
    readiness.set_defaults(handler=_readiness)

    audit = subparsers.add_parser("audit", help="independently audit and sign a request")
    audit.add_argument("--registry", type=Path, required=True)
    audit.add_argument("--policy", type=Path, required=True)
    audit.add_argument("--request", type=Path, required=True)
    audit.add_argument(
        "--auditor-key",
        action="append",
        default=[],
        metavar="KEY_ID=PATH",
        help="repeat to satisfy the audit signature threshold",
    )
    audit.add_argument("--audited-at", type=_aware_datetime, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.set_defaults(handler=_audit)

    promote = subparsers.add_parser("promote", help="append and sign one registered successor")
    promote.add_argument("--registry", type=Path, required=True)
    promote.add_argument("--policy", type=Path, required=True)
    promote.add_argument("--request", type=Path, required=True)
    promote.add_argument("--audit", type=Path, required=True)
    promote.add_argument(
        "--promoter-key",
        action="append",
        default=[],
        metavar="KEY_ID=PATH",
        help="repeat to satisfy the registry-promotion signature threshold",
    )
    promote.add_argument("--promoted-at", type=_aware_datetime, required=True)
    promote.add_argument("--output", type=Path, required=True)
    promote.set_defaults(handler=_promote)

    verify = subparsers.add_parser("verify", help="verify and reconstruct a signed update")
    verify.add_argument("--registry", type=Path, required=True)
    verify.add_argument("--policy", type=Path, required=True)
    verify.add_argument("--request", type=Path, required=True)
    verify.add_argument("--audit", type=Path, required=True)
    verify.add_argument("--update", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
