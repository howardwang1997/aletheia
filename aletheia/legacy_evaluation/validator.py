"""Independent structural validator for PR-6 legacy-evaluation raw artifacts.

The validator never imports a domain plugin and never deserializes a model.  It fresh-reads every
declared file, mechanically projects metrics from the legacy eval JSON, and signs only an
evaluator-only eligibility receipt.  Scientific interpretation and observation admission remain
the PR-5 bridge's separate responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from aletheia.legacy_evaluation.contracts import (
    LegacyArtifactKind,
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInvocation,
    LegacyEvaluationRawResult,
    LegacyEvaluationValidationDisposition,
    LegacyEvaluationValidationMessage,
    LegacyEvaluationValidatorPin,
    SignedLegacyEvaluationValidation,
    canonical_json_bytes,
    canonical_sha256,
    legacy_evaluation_key_id,
)

_RAW_RESULT_RELATIVE_PATH = "raw-result.json"
_ARTIFACT_CONTRACT = {
    LegacyArtifactKind.EVAL: (
        "legacy.evaluation.eval",
        "eval.json",
        "application/json",
        True,
    ),
    LegacyArtifactKind.MODEL: (
        "legacy.evaluation.model",
        "model.bin",
        "application/octet-stream",
        True,
    ),
}


class LegacyEvaluationValidationError(RuntimeError):
    """The validator could not establish a safe, attributable validation boundary."""


def _read_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise LegacyEvaluationValidationError(f"{label} cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise LegacyEvaluationValidationError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise LegacyEvaluationValidationError(f"{label} must be a nonempty regular file")
        if before.st_size > maximum_bytes:
            raise LegacyEvaluationValidationError(f"{label} exceeds its frozen byte ceiling")
        payload = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise LegacyEvaluationValidationError(f"{label} changed while it was read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LegacyEvaluationValidationError(f"{label} grew while it was read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise LegacyEvaluationValidationError(f"{label} changed while it was read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("JSON object repeats a key")
        return dict(pairs)

    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant {value}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must contain an object")
    return value


def _lookup(value: object, path: tuple[str, ...]) -> object:
    current = value
    for component in path:
        if not isinstance(current, dict) or component not in current:
            raise KeyError(component)
        current = current[component]
    return current


def _private_public_hex(private_key: bytes) -> str:
    if len(private_key) != 32:
        raise LegacyEvaluationValidationError("validator private key must contain 32 raw bytes")
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _validated_root(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise LegacyEvaluationValidationError("legacy evaluation output root cannot be a symlink")
    try:
        root = candidate.resolve(strict=True)
        metadata = root.lstat()
    except OSError as exc:
        raise LegacyEvaluationValidationError(
            "legacy evaluation output root is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise LegacyEvaluationValidationError("legacy evaluation output root must be a directory")
    return root


def _sign(message: LegacyEvaluationValidationMessage, *, private_key: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(private_key).sign(message.message_bytes).hex()


def validate_legacy_evaluation_raw_result(
    *,
    raw_result: LegacyEvaluationRawResult,
    invocation: LegacyEvaluationInvocation,
    harness: LegacyEvaluationHarnessManifest,
    output_root: Path,
    validator_pin: LegacyEvaluationValidatorPin,
    validated_at: datetime,
    validator_private_key: bytes,
) -> SignedLegacyEvaluationValidation:
    """Fresh-read and sign an eligibility receipt without interpreting a scientific outcome."""

    try:
        raw_result = LegacyEvaluationRawResult.model_validate(raw_result.model_dump(mode="python"))
        invocation = LegacyEvaluationInvocation.model_validate(invocation.model_dump(mode="python"))
        harness = LegacyEvaluationHarnessManifest.model_validate(harness.model_dump(mode="python"))
        validator_pin = LegacyEvaluationValidatorPin.model_validate(
            validator_pin.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise LegacyEvaluationValidationError(
            "legacy evaluation validation input is not closed"
        ) from exc
    if (
        validated_at.tzinfo is None
        or validated_at.utcoffset() is None
        or (validated_at.utcoffset().total_seconds() != 0)
    ):
        raise LegacyEvaluationValidationError("legacy evaluation validation time must be UTC")
    if not validator_pin.valid_from <= validated_at < validator_pin.expires_at:
        raise LegacyEvaluationValidationError("legacy evaluation validator key is not active")
    if _private_public_hex(validator_private_key) != validator_pin.public_key_ed25519_hex:
        raise LegacyEvaluationValidationError("legacy evaluation validator private key differs")
    if harness.manifest_sha256 not in validator_pin.trusted_harness_manifest_sha256s:
        raise LegacyEvaluationValidationError("legacy evaluation harness is not validator-trusted")
    if validator_pin.validator_principal_id in {
        harness.executor_principal_id,
        harness.frozen_by_principal_id,
    }:
        raise LegacyEvaluationValidationError("legacy evaluation validator is not independent")

    blockers: set[str] = set()
    if (
        raw_result.invocation_sha256 != invocation.invocation_sha256
        or raw_result.harness_manifest_sha256 != harness.manifest_sha256
        or raw_result.capability_manifest_sha256 != invocation.capability_manifest_sha256
        or raw_result.plugin_name != harness.plugin_name
        or raw_result.executor_principal_id != harness.executor_principal_id
        or invocation.harness_manifest_sha256 != harness.manifest_sha256
        or invocation.capability_id != harness.capability_id
    ):
        blockers.add("result_binding_changed")
    if (
        raw_result.started_at < invocation.issued_at
        or raw_result.ended_at >= invocation.deadline
        or validated_at < raw_result.ended_at
    ):
        blockers.add("execution_window_changed")

    root = _validated_root(output_root)
    declared_paths = {item.relative_path for item in raw_result.artifacts} | {
        _RAW_RESULT_RELATIVE_PATH
    }
    actual_files: set[str] = set()
    for child in root.iterdir():
        if child.is_symlink() or child.is_dir():
            blockers.add("unexpected_output")
            continue
        if not child.is_file():
            blockers.add("unexpected_output")
            continue
        actual_files.add(child.relative_to(root).as_posix())
    if actual_files != declared_paths:
        blockers.add("unexpected_output")

    fresh_identities: list[str] = []
    raw_bytes = _read_regular_file(
        root / _RAW_RESULT_RELATIVE_PATH,
        maximum_bytes=harness.maximum_artifact_bytes,
        label="legacy evaluation raw result",
    )
    fresh_identities.append(
        canonical_sha256(
            {
                "relative_path": _RAW_RESULT_RELATIVE_PATH,
                "content_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            }
        )
    )
    if raw_bytes != canonical_json_bytes(raw_result):
        blockers.add("raw_result_content_changed")

    artifacts_by_kind = {item.legacy_kind: item for item in raw_result.artifacts}
    required_kinds = {LegacyArtifactKind(item) for item in harness.required_legacy_artifact_kinds}
    if not required_kinds.issubset(artifacts_by_kind) or len(artifacts_by_kind) != len(
        raw_result.artifacts
    ):
        blockers.add("artifact_contract_changed")
    for kind, artifact in artifacts_by_kind.items():
        if (
            kind not in _ARTIFACT_CONTRACT
            or (
                artifact.artifact_key,
                artifact.relative_path,
                artifact.media_type,
                artifact.required,
            )
            != _ARTIFACT_CONTRACT[kind]
        ):
            blockers.add("artifact_contract_changed")

    eval_payload: dict[str, Any] | None = None
    for artifact in raw_result.artifacts:
        payload = _read_regular_file(
            root / artifact.relative_path,
            maximum_bytes=harness.maximum_artifact_bytes,
            label=f"legacy evaluation artifact {artifact.artifact_key}",
        )
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        fresh_identities.append(
            canonical_sha256(
                {
                    "artifact_key": artifact.artifact_key,
                    "relative_path": artifact.relative_path,
                    "content_sha256": actual_sha256,
                }
            )
        )
        if actual_sha256 != artifact.content_sha256 or len(payload) != artifact.bytes:
            blockers.add("artifact_content_changed")
        if artifact.legacy_kind is LegacyArtifactKind.EVAL:
            try:
                eval_payload = _strict_json_object(payload)
            except (UnicodeDecodeError, ValueError):
                blockers.add("eval_record_invalid")

    metrics = {item.name: item.value for item in raw_result.metrics}
    if set(metrics) != {item.metric_name for item in harness.metric_projections}:
        blockers.add("metric_contract_changed")
    if eval_payload is None:
        blockers.add("eval_record_invalid")
    else:
        for projection in harness.metric_projections:
            try:
                observed = _lookup(eval_payload, projection.eval_json_path)
                if type(observed) not in {int, float} or not math.isfinite(float(observed)):
                    raise ValueError("projected metric is not finite")
                if (
                    projection.metric_name not in metrics
                    or float(observed) != metrics[projection.metric_name]
                ):
                    blockers.add("metric_projection_changed")
            except (KeyError, TypeError, ValueError):
                blockers.add("metric_projection_changed")
        if validator_pin.require_grouped_protocol:
            try:
                if _lookup(eval_payload, ("protocol", "status")) != "grouped":
                    blockers.add("grouped_protocol_not_satisfied")
            except KeyError:
                blockers.add("grouped_protocol_not_satisfied")

    blocker_codes = tuple(sorted(blockers))
    disposition = (
        LegacyEvaluationValidationDisposition.VALIDATED_RAW_ARTIFACT
        if not blocker_codes
        else LegacyEvaluationValidationDisposition.REJECTED
    )
    message = LegacyEvaluationValidationMessage(
        raw_result_sha256=raw_result.raw_result_sha256,
        invocation_sha256=invocation.invocation_sha256,
        harness_manifest_sha256=harness.manifest_sha256,
        capability_manifest_sha256=invocation.capability_manifest_sha256,
        validator_principal_id=validator_pin.validator_principal_id,
        validator_key_id=validator_pin.key_id,
        validation_policy_sha256=validator_pin.policy_sha256,
        fresh_artifact_sha256s=tuple(sorted(fresh_identities)),
        disposition=disposition,
        blocker_codes=blocker_codes,
        validated_at=validated_at,
        eligible_for_independent_scientific_validation=not blocker_codes,
    )
    return SignedLegacyEvaluationValidation(
        message=message,
        signature_ed25519_hex=_sign(message, private_key=validator_private_key),
    )


def verify_signed_legacy_evaluation_validation(
    *,
    signed: SignedLegacyEvaluationValidation,
    raw_result: LegacyEvaluationRawResult,
    invocation: LegacyEvaluationInvocation,
    harness: LegacyEvaluationHarnessManifest,
    validator_pin: LegacyEvaluationValidatorPin,
) -> LegacyEvaluationValidationMessage:
    """Verify signature, independence, validity, and every immutable identity binding."""

    try:
        signed = SignedLegacyEvaluationValidation.model_validate(signed.model_dump(mode="python"))
        raw_result = LegacyEvaluationRawResult.model_validate(raw_result.model_dump(mode="python"))
        invocation = LegacyEvaluationInvocation.model_validate(invocation.model_dump(mode="python"))
        harness = LegacyEvaluationHarnessManifest.model_validate(harness.model_dump(mode="python"))
        validator_pin = LegacyEvaluationValidatorPin.model_validate(
            validator_pin.model_dump(mode="python")
        )
        message = signed.message
        if (
            message.raw_result_sha256 != raw_result.raw_result_sha256
            or message.invocation_sha256 != invocation.invocation_sha256
            or message.harness_manifest_sha256 != harness.manifest_sha256
            or message.capability_manifest_sha256 != invocation.capability_manifest_sha256
            or message.validator_principal_id != validator_pin.validator_principal_id
            or message.validator_key_id != validator_pin.key_id
            or message.validation_policy_sha256 != validator_pin.policy_sha256
            or harness.manifest_sha256 not in validator_pin.trusted_harness_manifest_sha256s
            or message.validator_principal_id
            in {harness.executor_principal_id, harness.frozen_by_principal_id}
            or not validator_pin.valid_from <= message.validated_at < validator_pin.expires_at
        ):
            raise ValueError("legacy evaluation validation binding changed")
        public_key = bytes.fromhex(validator_pin.public_key_ed25519_hex)
        if legacy_evaluation_key_id(validator_pin.public_key_ed25519_hex) != validator_pin.key_id:
            raise ValueError("legacy evaluation validator key identity changed")
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(signed.signature_ed25519_hex),
            message.message_bytes,
        )
        return message
    except LegacyEvaluationValidationError:
        raise
    except (InvalidSignature, TypeError, ValidationError, ValueError) as exc:
        raise LegacyEvaluationValidationError(
            "legacy evaluation validation signature or binding is invalid"
        ) from exc


__all__ = [
    "LegacyEvaluationValidationError",
    "validate_legacy_evaluation_raw_result",
    "verify_signed_legacy_evaluation_validation",
]
