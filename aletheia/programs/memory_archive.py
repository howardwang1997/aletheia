"""Content-addressed artifact storage for reconstructed scientific memory."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import AwareDatetime

from aletheia.knowledge.response_archive import (
    ArchivedSearchLedger,
    ContentAddressedResponseArchive,
)
from aletheia.programs.memory_schemas import (
    MemoryArtifactReceipt,
    MemoryCompactionArtifact,
)


class ScientificMemoryArchive:
    """Small typed façade over the repository's audited write-once JSON archive."""

    def __init__(self, root: Path) -> None:
        self._archive = ContentAddressedResponseArchive(root)

    def store(
        self,
        artifact: MemoryCompactionArtifact,
        *,
        archived_at: AwareDatetime,
    ) -> MemoryArtifactReceipt:
        receipt = self._archive.store_ledger(
            value=artifact,
            object_sha256=artifact.object_sha256,
            archived_at=archived_at,
        )
        return MemoryArtifactReceipt(
            artifact_sha256=receipt.ledger_sha256,
            artifact_bytes=receipt.ledger_bytes,
            relative_path=receipt.relative_path,
            object_sha256=receipt.object_sha256,
            archived_at=receipt.archived_at,
        )

    def read(self, receipt: MemoryArtifactReceipt) -> MemoryCompactionArtifact:
        raw = self._archive.read_ledger(
            ArchivedSearchLedger(
                ledger_sha256=receipt.artifact_sha256,
                ledger_bytes=receipt.artifact_bytes,
                relative_path=receipt.relative_path,
                object_sha256=receipt.object_sha256,
                archived_at=receipt.archived_at,
            )
        )
        value = json.loads(raw)
        artifact = MemoryCompactionArtifact.model_validate(value)
        if artifact.object_sha256 != receipt.object_sha256:
            raise ValueError("memory artifact object identity changed")
        return artifact


__all__ = ["ScientificMemoryArchive"]
