"""Pinned PR-4 allocator composition for atomic scientific execution registration.

The registration service may perform exactly the qualification admission and resource reservation
needed by ``REGISTER_EXECUTION``.  It loads no runtime-control, node, terminal, qualification, or
scientific signing key.  All engineering inputs are freshly resolved through immutable registries
and a read-only artifact store before the existing allocator writes inside the registrar's caller-
owned PostgreSQL transaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.execution.allocator import (
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
    PostgreSQLExecutionReceiptArchive,
)
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.authority_registry import (
    CompositeExecutionAuthorityResolver,
    ExactExecutionCostQuoteRegistry,
    SourceBudgetProjectionRegistry,
)
from aletheia.execution.input_resolver import LocalVerifiedInputArtifactResolver
from aletheia.execution.qualification_custody import (
    PreAdmissionEngineeringQualificationCustody,
    QualificationPreAdmissionCustodyConfig,
)
from aletheia.execution.runtime_contracts import (
    NodeEnrollmentAuthorityVerifier,
    QualificationAuthorityVerifier,
    TerminalVerificationAuthorityVerifier,
    WorkerNodeAuthorityVerifier,
)
from aletheia.execution.runtime_v2_contracts import (
    PinnedRuntimeControlVerificationAuthority,
    RuntimeControlAuthorityPin,
)
from aletheia.execution.terminal_runtime import TerminalNodeAuthorityConfig

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"


class QualificationExecutionRegistrationConfig(BaseModel):
    """Complete public authority closure for one narrow registration allocator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aletheia.qualification_execution_registration_config"] = (
        "aletheia.qualification_execution_registration_config"
    )
    schema_version: Literal[1] = 1
    qualification_custody: QualificationPreAdmissionCustodyConfig
    runtime_control_authority_pin: RuntimeControlAuthorityPin
    node_authorities: tuple[TerminalNodeAuthorityConfig, ...] = Field(min_length=1)
    allowed_rate_card_sha256s: tuple[str, ...] = Field(min_length=1)
    allowed_currency_codes: tuple[str, ...] = Field(min_length=1)
    allocator_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    initial_assignment_lease_seconds: int = Field(default=15, ge=1, le=7200)
    prepared_at: AwareDatetime
    qualification_admission_and_reservation_allowed: Literal[True] = True
    sea_registration_allowed: Literal[True] = True
    execution_launch_allowed: Literal[False] = False
    node_registry_mutation_allowed: Literal[False] = False
    runtime_lifecycle_mutation_allowed: Literal[False] = False
    terminal_commit_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    scientific_signing_key_loaded: Literal[False] = False
    runtime_control_signing_key_loaded: Literal[False] = False
    qualification_signing_key_loaded: Literal[False] = False
    node_signing_key_loaded: Literal[False] = False
    terminal_signing_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _authority_closure_is_exact(self) -> "QualificationExecutionRegistrationConfig":
        custody = self.qualification_custody
        if (
            self.prepared_at.utcoffset() != timedelta(0)
            or custody.prepared_at != self.prepared_at
            or not self.runtime_control_authority_pin.active_at(self.prepared_at)
        ):
            raise ValueError("execution registration authority is inactive or time-rebound")
        node_ids = tuple(item.manifest.node_id for item in self.node_authorities)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("execution registration nodes must be unique and canonical")
        if self.allowed_rate_card_sha256s != tuple(
            sorted(set(self.allowed_rate_card_sha256s))
        ) or any(
            len(item) != 64 or any(character not in "0123456789abcdef" for character in item)
            for item in self.allowed_rate_card_sha256s
        ):
            raise ValueError("execution registration rate cards must be canonical SHA-256 values")
        if self.allowed_currency_codes != tuple(sorted(set(self.allowed_currency_codes))) or any(
            len(item) != 3 or not item.isalpha() or item != item.upper()
            for item in self.allowed_currency_codes
        ):
            raise ValueError("execution registration currencies must be canonical ISO-style codes")
        for item in self.node_authorities:
            manifest = item.manifest
            manifest_active_until = min(
                manifest.key_expires_at,
                manifest.key_revoked_at or manifest.key_expires_at,
            )
            if not (
                item.enrollment_authority_pin.active_at(self.prepared_at)
                and item.assignment_transport_pin.active_at(self.prepared_at)
                and manifest.key_valid_from <= self.prepared_at < manifest_active_until
            ):
                raise ValueError("execution registration node authority is inactive")

        principals = (
            custody.artifact_verifier_principal_id,
            custody.input_resolver_principal_id,
            custody.pricing_authority_pin.principal_id,
            custody.source_budget_authority_pin.principal_id,
            custody.qualification_authority_pin.principal_id,
            custody.terminal_verification_authority_pin.principal_id,
            self.runtime_control_authority_pin.principal_id,
            self.allocator_principal_id,
            *(item.manifest.principal_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in self.node_authorities),
            *(
                item.assignment_transport_pin.transport_principal_id
                for item in self.node_authorities
            ),
        )
        keys = (
            custody.pricing_authority_pin.key_id,
            custody.source_budget_authority_pin.key_id,
            custody.qualification_authority_pin.key_id,
            custody.terminal_verification_authority_pin.key_id,
            self.runtime_control_authority_pin.key_id,
            *(item.manifest.node_signing_key_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.key_id for item in self.node_authorities),
            *(item.assignment_transport_pin.transport_key_id for item in self.node_authorities),
        )
        policies = (
            custody.pricing_authority_pin.policy_sha256,
            custody.source_budget_authority_pin.policy_sha256,
            custody.qualification_authority_pin.policy_sha256,
            custody.terminal_verification_authority_pin.policy_sha256,
            self.runtime_control_authority_pin.policy_sha256,
            *(item.enrollment_authority_pin.policy_sha256 for item in self.node_authorities),
            *(
                item.assignment_transport_pin.transport_policy_sha256
                for item in self.node_authorities
            ),
        )
        duplicate_principals = tuple(
            sorted(value for value, count in Counter(principals).items() if count > 1)
        )
        duplicate_keys = tuple(sorted(value for value, count in Counter(keys).items() if count > 1))
        duplicate_policies = tuple(
            sorted(value for value, count in Counter(policies).items() if count > 1)
        )
        if duplicate_principals or duplicate_keys or duplicate_policies:
            raise ValueError(
                "execution registration authorities must use distinct principals, keys, and "
                f"policies: principals={duplicate_principals}, keys={duplicate_keys}, "
                f"policies={duplicate_policies}"
            )
        return self

    @property
    def authority_principal_ids(self) -> tuple[str, ...]:
        custody = self.qualification_custody
        return (
            custody.artifact_verifier_principal_id,
            custody.input_resolver_principal_id,
            custody.pricing_authority_pin.principal_id,
            custody.source_budget_authority_pin.principal_id,
            custody.qualification_authority_pin.principal_id,
            custody.terminal_verification_authority_pin.principal_id,
            self.runtime_control_authority_pin.principal_id,
            self.allocator_principal_id,
            *(item.manifest.principal_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in self.node_authorities),
            *(
                item.assignment_transport_pin.transport_principal_id
                for item in self.node_authorities
            ),
        )

    @property
    def authority_key_ids(self) -> tuple[str, ...]:
        custody = self.qualification_custody
        return (
            custody.pricing_authority_pin.key_id,
            custody.source_budget_authority_pin.key_id,
            custody.qualification_authority_pin.key_id,
            custody.terminal_verification_authority_pin.key_id,
            self.runtime_control_authority_pin.key_id,
            *(item.manifest.node_signing_key_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.key_id for item in self.node_authorities),
            *(item.assignment_transport_pin.transport_key_id for item in self.node_authorities),
        )

    @property
    def authority_policy_sha256s(self) -> tuple[str, ...]:
        custody = self.qualification_custody
        return (
            custody.pricing_authority_pin.policy_sha256,
            custody.source_budget_authority_pin.policy_sha256,
            custody.qualification_authority_pin.policy_sha256,
            custody.terminal_verification_authority_pin.policy_sha256,
            self.runtime_control_authority_pin.policy_sha256,
            *(item.enrollment_authority_pin.policy_sha256 for item in self.node_authorities),
            *(
                item.assignment_transport_pin.transport_policy_sha256
                for item in self.node_authorities
            ),
        )


@dataclass(frozen=True)
class QualificationExecutionRegistrationComposition:
    """Narrow registrar dependencies; the service never exports the allocator itself."""

    qualification_authority: QualificationAuthorityVerifier
    qualification_custody: PreAdmissionEngineeringQualificationCustody
    allocator: PostgreSQLExecutionAllocator


def compose_qualification_execution_registration(
    config: QualificationExecutionRegistrationConfig,
) -> QualificationExecutionRegistrationComposition:
    """Build the public verifiers and PR-4 reservation writer from one frozen config."""

    config = QualificationExecutionRegistrationConfig.model_validate(
        config.model_dump(mode="python")
    )
    custody = config.qualification_custody
    terminal_verifier = TerminalVerificationAuthorityVerifier(
        custody.terminal_verification_authority_pin
    )
    receipt_archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=terminal_verifier
    )
    artifact_store = LocalArtifactStore(
        Path(custody.artifact_store_root),
        verifier_principal_id=custody.artifact_verifier_principal_id,
        object_store_id=custody.artifact_object_store_id,
        max_object_bytes=custody.artifact_max_object_bytes,
        read_only=True,
    )
    artifact_resolver = LocalVerifiedInputArtifactResolver(
        artifact_store=artifact_store,
        terminal_receipt_archive=receipt_archive,
        resolver_principal_id=custody.input_resolver_principal_id,
    )
    quote_registry = ExactExecutionCostQuoteRegistry(
        Path(custody.authority_registry_root),
        filesystem_pin=custody.authority_registry_filesystem_pin,
        pricing_authority_pin=custody.pricing_authority_pin,
    )
    budget_registry = SourceBudgetProjectionRegistry(
        Path(custody.authority_registry_root),
        filesystem_pin=custody.authority_registry_filesystem_pin,
        source_budget_authority_pin=custody.source_budget_authority_pin,
    )
    execution_authority_resolver = CompositeExecutionAuthorityResolver(
        quote_registry=quote_registry,
        budget_registry=budget_registry,
        execution_receipt_resolver=receipt_archive,
    )
    node_authorities = tuple(
        WorkerNodeAuthorityVerifier(
            manifest=item.manifest,
            enrollment=item.enrollment,
            enrollment_authority=NodeEnrollmentAuthorityVerifier(item.enrollment_authority_pin),
            expected_manifest_sha256=item.manifest.manifest_sha256,
            observed_at=config.prepared_at,
        )
        for item in config.node_authorities
    )
    qualification_authority = QualificationAuthorityVerifier(custody.qualification_authority_pin)
    pre_admission_custody = PreAdmissionEngineeringQualificationCustody(
        authority=qualification_authority,
        artifact_resolver=artifact_resolver,
        execution_authority_resolver=execution_authority_resolver,
    )
    allocator = PostgreSQLExecutionAllocator(
        authority=qualification_authority,
        artifact_resolver=artifact_resolver,
        execution_authority_resolver=execution_authority_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({custody.pricing_authority_pin.principal_id}),
            rate_card_sha256s=frozenset(config.allowed_rate_card_sha256s),
            pricing_policy_sha256s=frozenset({custody.pricing_authority_pin.policy_sha256}),
            currency_codes=frozenset(config.allowed_currency_codes),
        ),
        node_authorities=node_authorities,
        node_assignment_transport_pins=tuple(
            item.assignment_transport_pin for item in config.node_authorities
        ),
        terminal_verification_authority=terminal_verifier,
        allocator_principal_id=config.allocator_principal_id,
        runtime_control_authority=PinnedRuntimeControlVerificationAuthority(
            config.runtime_control_authority_pin
        ),
        initial_assignment_lease_seconds=config.initial_assignment_lease_seconds,
    )
    if allocator.runtime_control_issuance_enabled:
        raise ValueError("execution registration composition unexpectedly loaded a runtime signer")
    return QualificationExecutionRegistrationComposition(
        qualification_authority=qualification_authority,
        qualification_custody=pre_admission_custody,
        allocator=allocator,
    )


__all__ = [
    "QualificationExecutionRegistrationComposition",
    "QualificationExecutionRegistrationConfig",
    "compose_qualification_execution_registration",
]
