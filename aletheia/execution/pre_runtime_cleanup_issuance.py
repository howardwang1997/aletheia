"""Narrow signer for one attempt-scoped post-expiry pre-runtime cleanup."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.execution.runtime_v2_contracts import (
    AttemptScopedPreRuntimeCleanupAuthorityPin,
    AttemptScopedPreRuntimeCleanupAuthorityVerifier,
    PreRuntimeAbsenceReceipt,
    issue_attempt_scoped_pre_runtime_cleanup_receipt,
)


class PinnedAttemptScopedPreRuntimeCleanupAuthority:
    """Hold one exact key without exposing a generic signing callback."""

    def __init__(
        self,
        *,
        pin: AttemptScopedPreRuntimeCleanupAuthorityPin,
        private_key: bytes,
    ) -> None:
        self._pin = AttemptScopedPreRuntimeCleanupAuthorityPin.model_validate(
            pin.model_dump(mode="python")
        )
        key = bytes(private_key)
        try:
            public_key = (
                Ed25519PrivateKey.from_private_bytes(key)
                .public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except ValueError as exc:
            raise ValueError(
                "pre-runtime cleanup private key must contain exactly 32 raw bytes"
            ) from exc
        if public_key != self._pin.public_key_ed25519_hex:
            raise ValueError("pre-runtime cleanup private key differs from deployment pin")
        self._private_key = key
        self._verifier = AttemptScopedPreRuntimeCleanupAuthorityVerifier(self._pin)

    @property
    def authority_pin(self) -> AttemptScopedPreRuntimeCleanupAuthorityPin:
        return self._pin

    @property
    def authority_verifier(self) -> AttemptScopedPreRuntimeCleanupAuthorityVerifier:
        return self._verifier

    def issue(self, **scope: object) -> PreRuntimeAbsenceReceipt:
        return issue_attempt_scoped_pre_runtime_cleanup_receipt(
            authority_pin=self._pin,
            private_key=self._private_key,
            **scope,
        )


__all__ = ["PinnedAttemptScopedPreRuntimeCleanupAuthority"]
