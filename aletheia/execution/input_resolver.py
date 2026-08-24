"""Fresh, read-only resolution of local input-artifact custody and producer lineage.

The resolver joins two independently retained facts without gaining execution authority: the
filesystem artifact store must still contain the canonical AVR, manifest, and CAS bytes, while a
read-only terminal archive may establish the exact successful WorkOrder producer.  The allocator
supplies its database clock observation; no process wall clock participates in qualification.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError

from aletheia.execution.artifact_store import ArtifactStoreError, LocalArtifactStore
from aletheia.execution.ports import (
    ArchivedExecutionTerminalReceipt,
    ExecutionTerminalReceiptArchivePort,
)
from aletheia.execution.runtime_contracts import VerifiedInputArtifactResolution
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    ExecutionReceipt,
    ExecutionTerminalState,
    canonical_sha256,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")


class InputArtifactResolutionError(RuntimeError):
    """Archived custody or producer lineage was absent, ambiguous, or inconsistent."""


class LocalVerifiedInputArtifactResolver:
    """Resolve local CAS custody plus optional immutable successful-producer lineage."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        terminal_receipt_archive: ExecutionTerminalReceiptArchivePort,
        resolver_principal_id: str = "execution-input-artifact-resolver",
    ) -> None:
        if not isinstance(artifact_store, LocalArtifactStore):
            raise TypeError("input resolution requires the deployment LocalArtifactStore")
        if _PRINCIPAL_ID.fullmatch(resolver_principal_id) is None:
            raise ValueError("input resolver principal id is invalid")
        self._artifact_store = artifact_store
        self._terminal_receipt_archive = terminal_receipt_archive
        self._resolver_principal_id = resolver_principal_id

    @staticmethod
    def _require_database_observation(observed_at: datetime) -> None:
        try:
            offset = observed_at.utcoffset()
        except AttributeError as exc:
            raise InputArtifactResolutionError(
                "input resolution requires an allocator database timestamp"
            ) from exc
        if observed_at.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise InputArtifactResolutionError(
                "input resolution requires a timezone-aware UTC database timestamp"
            )

    @staticmethod
    def _validate_producer_row(
        row: ArchivedExecutionTerminalReceipt,
        *,
        verified_receipt_sha256: str,
        verified_receipt: ArtifactVerifiedReceipt,
        artifact_manifest: ArtifactManifest,
        observed_at: datetime,
    ) -> ExecutionReceipt:
        try:
            receipt = ExecutionReceipt.model_validate(
                row.receipt.model_dump(mode="python", warnings="none")
            )
            manifest_sha256 = artifact_manifest.manifest_sha256
            manifest_attempt_id = artifact_manifest.infrastructure_attempt_id
            manifest_execution_id = artifact_manifest.execution_id
            manifest_intent_sha256 = artifact_manifest.intent_sha256
            receipt_hashes = tuple(
                item.verified_receipt_sha256 for item in receipt.artifact_verified_receipts
            )
            node_payload = row.node_execution_receipt_json
            attestation_payload = row.terminal_verification_attestation_json
            pin_payload = row.terminal_verification_authority_pin_json
            if not all(
                isinstance(item, Mapping)
                for item in (node_payload, attestation_payload, pin_payload)
            ):
                raise TypeError("terminal authority payloads must be canonical mappings")
            attestation_message = attestation_payload.get("message")
            if not isinstance(attestation_message, Mapping):
                raise TypeError("terminal attestation message must be a canonical mapping")
            committed_offset = row.committed_at.utcoffset()
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise InputArtifactResolutionError(
                "terminal archive returned invalid execution-receipt bytes"
            ) from exc

        if (
            row.receipt_sha256 != receipt.execution_receipt_sha256
            or row.payload_sha256 != receipt.execution_receipt_sha256
            or row.node_execution_receipt_sha256 != receipt.node_execution_receipt_sha256
            or canonical_sha256(node_payload) != row.node_execution_receipt_sha256
            or canonical_sha256(attestation_payload) != row.terminal_verification_attestation_sha256
            or canonical_sha256(pin_payload) != row.terminal_verification_authority_pin_sha256
            or attestation_message.get("execution_receipt_sha256") != row.receipt_sha256
            or attestation_message.get("node_execution_receipt_sha256")
            != row.node_execution_receipt_sha256
            or attestation_message.get("terminal_state") != receipt.terminal_state.value
            or attestation_message.get("terminal_verification_policy_sha256")
            != row.terminal_verification_policy_sha256
            or attestation_message.get("verification_key_id") != row.terminal_verification_key_id
            or attestation_message.get("verified_by_principal_id") != row.committed_by_principal_id
            or pin_payload.get("policy_sha256") != row.terminal_verification_policy_sha256
            or pin_payload.get("key_id") != row.terminal_verification_key_id
            or pin_payload.get("principal_id") != row.committed_by_principal_id
            or receipt.verified_by_principal_id != row.committed_by_principal_id
            or row.attempt_id != manifest_attempt_id
            or row.execution_id != manifest_execution_id
            or row.intent_sha256 != manifest_intent_sha256
            or row.resource_lease_sha256 != receipt.resource_lease_sha256
            or row.terminal_state != ExecutionTerminalState.ENGINEERING_SUCCEEDED
            or receipt.terminal_state is not ExecutionTerminalState.ENGINEERING_SUCCEEDED
            or row.artifact_manifest_sha256 != manifest_sha256
            or receipt.artifact_manifest != artifact_manifest
            or row.artifact_verified_receipt_sha256s != receipt_hashes
            or committed_offset is None
            or committed_offset.total_seconds() != 0
            or receipt.verified_at > row.committed_at
            or row.committed_at > observed_at
            or receipt_hashes.count(verified_receipt_sha256) != 1
            or tuple(
                item
                for item in receipt.artifact_verified_receipts
                if item.verified_receipt_sha256 == verified_receipt_sha256
            )
            != (verified_receipt,)
        ):
            raise InputArtifactResolutionError(
                "terminal row is not the exact immutable successful producer lineage"
            )
        return receipt

    def resolve_verified_input_artifact(
        self,
        *,
        verified_receipt_sha256: str,
        observed_at: datetime,
    ) -> VerifiedInputArtifactResolution | None:
        """Freshly rehash one AVR/manifest/CAS closure at allocator database time."""

        if _SHA256.fullmatch(verified_receipt_sha256) is None:
            raise ValueError("verified receipt identity must be a lowercase SHA-256 digest")
        self._require_database_observation(observed_at)
        try:
            verified_receipt = self._artifact_store.load_verified_receipt(
                verified_receipt_sha256=verified_receipt_sha256
            )
            if verified_receipt is None:
                return None
            artifact_manifest = self._artifact_store.load_manifest(
                manifest_sha256=verified_receipt.artifact_manifest_sha256
            )
        except ArtifactStoreError as exc:
            raise InputArtifactResolutionError(
                "input artifact custody failed fresh local revalidation"
            ) from exc
        if artifact_manifest is None:
            raise InputArtifactResolutionError(
                "verified input artifact lacks its immutable manifest sidecar"
            )

        try:
            rows = tuple(
                self._terminal_receipt_archive.list_terminal_receipts_for_attempt(
                    infrastructure_attempt_id=artifact_manifest.infrastructure_attempt_id
                )
            )
        except Exception as exc:
            raise InputArtifactResolutionError(
                "terminal receipt archive could not establish producer lineage"
            ) from exc
        if len(rows) > 1:
            raise InputArtifactResolutionError(
                "terminal receipt archive returned ambiguous producer lineage"
            )

        producer_receipt: ExecutionReceipt | None = None
        if rows:
            row = rows[0]
            if not isinstance(row, ArchivedExecutionTerminalReceipt):
                raise InputArtifactResolutionError(
                    "terminal receipt archive returned an untyped row"
                )
            producer_receipt = self._validate_producer_row(
                row,
                verified_receipt_sha256=verified_receipt_sha256,
                verified_receipt=verified_receipt,
                artifact_manifest=artifact_manifest,
                observed_at=observed_at,
            )

        try:
            return VerifiedInputArtifactResolution(
                verified_receipt_sha256=verified_receipt_sha256,
                verified_receipt=verified_receipt,
                artifact_manifest=artifact_manifest,
                producer_execution_receipt=producer_receipt,
                content_rehash_sha256=verified_receipt.artifact.content_sha256,
                content_bytes=verified_receipt.artifact.bytes,
                custody_reverified=True,
                resolved_by_principal_id=self._resolver_principal_id,
                resolved_at=observed_at,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise InputArtifactResolutionError(
                "input artifact resolution failed exact contract revalidation"
            ) from exc

    def resolve_artifact_manifest(
        self,
        *,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> ArtifactManifest | None:
        """Freshly reload one complete manifest/CAS closure at allocator database time."""

        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ValueError("artifact manifest identity must be a lowercase SHA-256 digest")
        self._require_database_observation(observed_at)
        try:
            return self._artifact_store.load_manifest(manifest_sha256=manifest_sha256)
        except ArtifactStoreError as exc:
            raise InputArtifactResolutionError(
                "artifact manifest custody failed fresh local revalidation"
            ) from exc


__all__ = [
    "InputArtifactResolutionError",
    "LocalVerifiedInputArtifactResolver",
]
