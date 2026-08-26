"""Read-only PR-4 terminal source exposed to the durable research controller.

The execution allocator owns a much larger mutation surface.  A terminal-dispatcher process gets
only this wrapper, which can re-run historical verification and re-read the exact immutable outbox
inside a caller-owned transaction.  It cannot admit, reserve, launch, terminate, settle, or sign.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from aletheia.execution.allocator import (
    PostgreSQLExecutionAllocator,
    QualificationTerminalOutboxItem,
    VerifiedQualificationRawRunMaterial,
    VerifiedQualificationRunLineage,
    VerifiedQualificationTerminalSource,
)


class VerifiedQualificationTerminalOutboxReader:
    """Narrow public facade over a public-key-only allocator verification composition."""

    def __init__(self, allocator: PostgreSQLExecutionAllocator) -> None:
        if not isinstance(allocator, PostgreSQLExecutionAllocator):
            raise TypeError("terminal source reader requires the PostgreSQL execution facade")
        if allocator.runtime_control_issuance_enabled:
            raise ValueError("terminal source reader cannot retain runtime-control issuance")
        if not allocator.runtime_control_verification_enabled:
            raise ValueError("terminal source reader requires pinned runtime-control verification")
        self._allocator = allocator

    def load_verified_qualification_terminal_source(
        self,
        *,
        execution_id: str,
        attempt_id: str,
    ) -> VerifiedQualificationTerminalSource | None:
        candidate = self._allocator.load_verified_qualification_terminal_source(
            execution_id=execution_id,
            attempt_id=attempt_id,
        )
        if candidate is None:
            return None
        return VerifiedQualificationTerminalSource.model_validate(
            candidate.model_dump(mode="python")
        )

    def load_qualification_terminal_outbox_in_session(
        self,
        session: Session,
        *,
        execution_id: str,
        attempt_id: str,
    ) -> QualificationTerminalOutboxItem | None:
        candidate = self._allocator.load_qualification_terminal_outbox_in_session(
            session,
            execution_id=execution_id,
            attempt_id=attempt_id,
        )
        if candidate is None:
            return None
        return QualificationTerminalOutboxItem.model_validate(candidate.model_dump(mode="python"))


class VerifiedQualificationRawRunMaterialReader:
    """Narrow read-only facade over the full PR-4 terminal-lineage verifier."""

    def __init__(self, allocator: PostgreSQLExecutionAllocator) -> None:
        if not isinstance(allocator, PostgreSQLExecutionAllocator):
            raise TypeError("raw-run material reader requires the PostgreSQL execution facade")
        if allocator.runtime_control_issuance_enabled:
            raise ValueError("raw-run material reader cannot retain runtime-control issuance")
        if not allocator.runtime_control_verification_enabled:
            raise ValueError("raw-run material reader requires pinned runtime-control verification")
        self._allocator = allocator

    def load_verified_qualification_raw_run_material(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        observed_at: datetime,
    ) -> VerifiedQualificationRawRunMaterial | None:
        candidate = self._allocator.load_verified_qualification_raw_run_material(
            execution_id=execution_id,
            attempt_id=attempt_id,
            observed_at=observed_at,
        )
        if candidate is None:
            return None
        return VerifiedQualificationRawRunMaterial.model_validate(
            candidate.model_dump(mode="python")
        )


class VerifiedQualificationRunLineageReader:
    """Narrow facade exposing only complete, historically verified PR-4 run lineage."""

    def __init__(self, allocator: PostgreSQLExecutionAllocator) -> None:
        if not isinstance(allocator, PostgreSQLExecutionAllocator):
            raise TypeError("run-lineage reader requires the PostgreSQL execution facade")
        if allocator.runtime_control_issuance_enabled:
            raise ValueError("run-lineage reader cannot retain runtime-control issuance")
        if not allocator.runtime_control_verification_enabled:
            raise ValueError("run-lineage reader requires pinned runtime-control verification")
        self._allocator = allocator

    def load_verified_qualification_run_lineage(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        observed_at: datetime,
    ) -> VerifiedQualificationRunLineage | None:
        candidate = self._allocator.load_verified_qualification_run_lineage(
            execution_id=execution_id,
            attempt_id=attempt_id,
            observed_at=observed_at,
        )
        if candidate is None:
            return None
        return VerifiedQualificationRunLineage.model_validate(candidate.model_dump(mode="python"))


__all__ = [
    "VerifiedQualificationRawRunMaterialReader",
    "VerifiedQualificationRunLineageReader",
    "VerifiedQualificationTerminalOutboxReader",
]
