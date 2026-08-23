"""Protocol-only ports for future artifact custody and execution-receipt persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    ExecutionIntent,
    ExecutionReceipt,
)


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Quarantine, rehash, and promote artifacts without admitting scientific evidence."""

    def verify_manifest(
        self,
        *,
        intent: ExecutionIntent,
        manifest: ArtifactManifest,
    ) -> tuple[ArtifactVerifiedReceipt, ...]: ...

    def load_verified_receipt(
        self,
        *,
        verified_receipt_sha256: str,
    ) -> ArtifactVerifiedReceipt | None: ...


@runtime_checkable
class ExecutionReceiptPort(Protocol):
    """Append immutable attempt receipts and expose their reconciliation lineage."""

    def append_receipt(
        self,
        *,
        receipt: ExecutionReceipt,
        expected_previous_receipt_sha256: str | None,
    ) -> ExecutionReceipt: ...

    def load_latest_receipt(
        self,
        *,
        execution_id: str,
        infrastructure_attempt_id: str,
    ) -> ExecutionReceipt | None: ...

    def list_attempt_receipts(
        self,
        *,
        execution_id: str,
        infrastructure_attempt_id: str,
    ) -> tuple[ExecutionReceipt, ...]: ...


__all__ = ["ArtifactStorePort", "ExecutionReceiptPort"]
