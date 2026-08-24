"""Protocol-only ports for future artifact custody and execution-receipt persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    ExecutionIntent,
    ExecutionReceipt,
    ExecutionTerminalState,
)

if TYPE_CHECKING:
    from aletheia.execution.runtime_contracts import (
        ExecutionCostQuote,
        VerifiedBudgetAuthorizationResolution,
        VerifiedExecutionReceiptResolution,
        VerifiedInputArtifactResolution,
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


@dataclass(frozen=True)
class ArchivedExecutionTerminalReceipt:
    """Read-only snapshot of every authority-bearing terminal-receipt row field."""

    receipt_sha256: str
    attempt_id: str
    execution_id: str
    intent_sha256: str
    resource_lease_sha256: str
    terminal_state: ExecutionTerminalState | str
    payload_sha256: str
    receipt: ExecutionReceipt
    node_execution_receipt_sha256: str
    node_execution_receipt_json: Mapping[str, object]
    terminal_verification_attestation_sha256: str
    terminal_verification_attestation_json: Mapping[str, object]
    terminal_verification_authority_pin_sha256: str
    terminal_verification_authority_pin_json: Mapping[str, object]
    terminal_verification_policy_sha256: str
    terminal_verification_key_id: str
    committed_by_principal_id: str
    artifact_manifest_sha256: str | None
    artifact_verified_receipt_sha256s: tuple[str, ...]
    committed_at: datetime


@runtime_checkable
class ExecutionTerminalReceiptArchivePort(Protocol):
    """Read immutable terminal rows without exposing execution persistence writes."""

    def list_terminal_receipts_for_attempt(
        self,
        *,
        infrastructure_attempt_id: str,
    ) -> tuple[ArchivedExecutionTerminalReceipt, ...]: ...


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


@runtime_checkable
class VerifiedInputArtifactResolverPort(Protocol):
    """Re-resolve an input from trusted archive custody before qualification.

    Implementations must read the current immutable-manifest and verified-receipt bytes, re-read
    and hash the referenced CAS object, and re-check its custody state before returning.  For a
    WorkOrder-produced input they must also return the immutable successful ExecutionReceipt
    lineage.  A previously loaded standalone ``ArtifactVerifiedReceipt`` is not a valid result.
    """

    def resolve_artifact_manifest(
        self,
        *,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> ArtifactManifest | None: ...

    def resolve_verified_input_artifact(
        self,
        *,
        verified_receipt_sha256: str,
        observed_at: datetime,
    ) -> "VerifiedInputArtifactResolution | None": ...


@runtime_checkable
class ExecutionAuthorityResolverPort(Protocol):
    """Resolve canonical registered authority bytes, never caller-supplied inline claims.

    Implementations must load immutable registry/archive bytes by their content identity and
    revalidate custody before returning.  Qualification signing and admission compare these
    returned bytes with the inline bundle and fail closed on absence or divergence.
    """

    def resolve_execution_cost_quote(
        self,
        *,
        cost_quote_sha256: str,
        observed_at: datetime,
    ) -> "ExecutionCostQuote | None": ...

    def resolve_budget_authorization(
        self,
        *,
        source_budget_authorization_sha256: str,
        observed_at: datetime,
    ) -> "VerifiedBudgetAuthorizationResolution | None": ...

    def resolve_execution_receipt(
        self,
        *,
        execution_receipt_sha256: str,
        observed_at: datetime,
    ) -> "VerifiedExecutionReceiptResolution | None": ...


__all__ = [
    "ArchivedExecutionTerminalReceipt",
    "ArtifactStorePort",
    "ExecutionAuthorityResolverPort",
    "ExecutionReceiptPort",
    "ExecutionTerminalReceiptArchivePort",
    "VerifiedInputArtifactResolverPort",
]
