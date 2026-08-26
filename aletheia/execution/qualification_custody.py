"""Read-only PR-4 qualification custody for pre-admission scientific authorization.

The execution-authorization signer must prove the exact qualification bundle without gaining the
allocator's mutation methods.  This module composes only public verification keys, immutable
authority registries, and fresh artifact reads.  It deliberately cannot prove that a later
qualification admission exists; that separate historical proof remains with the allocator-backed
raw-run custody adapter.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.execution.allocator import PostgreSQLExecutionReceiptArchive
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.authority_contracts import (
    AuthorityRegistryFilesystemPin,
    PricingAuthorityPin,
    SourceBudgetAuthorityPin,
)
from aletheia.execution.authority_registry import (
    CompositeExecutionAuthorityResolver,
    ExactExecutionCostQuoteRegistry,
    SourceBudgetProjectionRegistry,
)
from aletheia.execution.input_resolver import LocalVerifiedInputArtifactResolver
from aletheia.execution.runtime_contracts import (
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    ExecutionAuthorityResolverPort,
    QualificationAuthorityPin,
    QualificationAuthorityVerifier,
    QualificationVerificationError,
    TerminalVerificationAuthorityPin,
    TerminalVerificationAuthorityVerifier,
    VerifiedEngineeringQualification,
    VerifiedInputArtifactResolverPort,
    verify_engineering_qualification,
)

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"


def _absolute_path(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not candidate.is_absolute()
        or value != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be one canonical absolute path")
    return candidate


class QualificationPreAdmissionCustodyConfig(BaseModel):
    """Deployment-pinned public inputs for fresh qualification recomputation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aletheia.qualification_pre_admission_custody_config"] = (
        "aletheia.qualification_pre_admission_custody_config"
    )
    schema_version: Literal[1] = 1
    artifact_store_root: str
    artifact_verifier_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    artifact_object_store_id: str = Field(pattern=_IDENTITY_PATTERN)
    artifact_max_object_bytes: int = Field(ge=1, le=1024**4)
    authority_registry_root: str
    authority_registry_filesystem_pin: AuthorityRegistryFilesystemPin
    pricing_authority_pin: PricingAuthorityPin
    source_budget_authority_pin: SourceBudgetAuthorityPin
    qualification_authority_pin: QualificationAuthorityPin
    terminal_verification_authority_pin: TerminalVerificationAuthorityPin
    input_resolver_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    prepared_at: AwareDatetime
    public_key_verification_only: Literal[True] = True
    execution_mutation_allowed: Literal[False] = False
    qualification_admission_claim_allowed: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False
    signing_private_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _roots_and_authorities_are_separate(self) -> "QualificationPreAdmissionCustodyConfig":
        artifact_root = _absolute_path(self.artifact_store_root, label="artifact store root")
        registry_root = _absolute_path(
            self.authority_registry_root,
            label="authority registry root",
        )
        if (
            artifact_root == registry_root
            or artifact_root in registry_root.parents
            or registry_root in artifact_root.parents
        ):
            raise ValueError("qualification artifact and authority roots must not overlap")
        if self.prepared_at.utcoffset() != timedelta(0):
            raise ValueError("qualification custody preparation time must be UTC")
        if not all(
            pin.active_at(self.prepared_at)
            for pin in (
                self.pricing_authority_pin,
                self.source_budget_authority_pin,
                self.qualification_authority_pin,
                self.terminal_verification_authority_pin,
            )
        ):
            raise ValueError("qualification custody authority is inactive at preparation time")
        principals = (
            self.artifact_verifier_principal_id,
            self.input_resolver_principal_id,
            self.pricing_authority_pin.principal_id,
            self.source_budget_authority_pin.principal_id,
            self.qualification_authority_pin.principal_id,
            self.terminal_verification_authority_pin.principal_id,
        )
        keys = (
            self.pricing_authority_pin.key_id,
            self.source_budget_authority_pin.key_id,
            self.qualification_authority_pin.key_id,
            self.terminal_verification_authority_pin.key_id,
        )
        if any(count > 1 for count in Counter(principals).values()) or any(
            count > 1 for count in Counter(keys).values()
        ):
            raise ValueError("qualification custody principals and keys must be role-separated")
        return self


class PreAdmissionEngineeringQualificationCustody:
    """Freshly recompute PR-4 eligibility while exposing no allocator write capability."""

    def __init__(
        self,
        *,
        authority: QualificationAuthorityVerifier,
        artifact_resolver: VerifiedInputArtifactResolverPort,
        execution_authority_resolver: ExecutionAuthorityResolverPort,
    ) -> None:
        if (
            not isinstance(authority, QualificationAuthorityVerifier)
            or not callable(getattr(artifact_resolver, "resolve_verified_input_artifact", None))
            or any(
                not callable(getattr(execution_authority_resolver, name, None))
                for name in (
                    "resolve_execution_cost_quote",
                    "resolve_budget_authorization",
                    "resolve_execution_receipt",
                )
            )
        ):
            raise TypeError("pre-admission qualification custody dependencies are invalid")
        self._authority = authority
        self._artifact_resolver = artifact_resolver
        self._execution_authority_resolver = execution_authority_resolver

    @property
    def qualification_authority(self) -> QualificationAuthorityVerifier:
        return self._authority

    def verify_engineering_qualification_custody(
        self,
        *,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification:
        return verify_engineering_qualification(
            bundle=bundle,
            grant=grant,
            authority=self._authority,
            artifact_resolver=self._artifact_resolver,
            authority_resolver=self._execution_authority_resolver,
            observed_at=observed_at,
        )

    def verify_qualification_admission(
        self,
        *,
        qualification_admission_sha256: str,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification:
        del qualification_admission_sha256, bundle, grant, observed_at
        raise QualificationVerificationError(
            "pre-admission qualification custody cannot assert a later allocator admission"
        )


@dataclass(frozen=True)
class QualificationPreAdmissionVerification:
    authority: QualificationAuthorityVerifier
    custody: PreAdmissionEngineeringQualificationCustody


def compose_qualification_pre_admission_verification(
    config: QualificationPreAdmissionCustodyConfig,
) -> QualificationPreAdmissionVerification:
    """Compose public, read-only verification dependencies from one frozen config."""

    config = QualificationPreAdmissionCustodyConfig.model_validate(config.model_dump(mode="python"))
    terminal_verifier = TerminalVerificationAuthorityVerifier(
        config.terminal_verification_authority_pin
    )
    receipt_archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=terminal_verifier
    )
    artifact_store = LocalArtifactStore(
        Path(config.artifact_store_root),
        verifier_principal_id=config.artifact_verifier_principal_id,
        object_store_id=config.artifact_object_store_id,
        max_object_bytes=config.artifact_max_object_bytes,
        read_only=True,
    )
    artifact_resolver = LocalVerifiedInputArtifactResolver(
        artifact_store=artifact_store,
        terminal_receipt_archive=receipt_archive,
        resolver_principal_id=config.input_resolver_principal_id,
    )
    quote_registry = ExactExecutionCostQuoteRegistry(
        Path(config.authority_registry_root),
        filesystem_pin=config.authority_registry_filesystem_pin,
        pricing_authority_pin=config.pricing_authority_pin,
    )
    budget_registry = SourceBudgetProjectionRegistry(
        Path(config.authority_registry_root),
        filesystem_pin=config.authority_registry_filesystem_pin,
        source_budget_authority_pin=config.source_budget_authority_pin,
    )
    execution_authority_resolver = CompositeExecutionAuthorityResolver(
        quote_registry=quote_registry,
        budget_registry=budget_registry,
        execution_receipt_resolver=receipt_archive,
    )
    authority = QualificationAuthorityVerifier(config.qualification_authority_pin)
    return QualificationPreAdmissionVerification(
        authority=authority,
        custody=PreAdmissionEngineeringQualificationCustody(
            authority=authority,
            artifact_resolver=artifact_resolver,
            execution_authority_resolver=execution_authority_resolver,
        ),
    )


__all__ = [
    "PreAdmissionEngineeringQualificationCustody",
    "QualificationPreAdmissionCustodyConfig",
    "QualificationPreAdmissionVerification",
    "compose_qualification_pre_admission_verification",
]
