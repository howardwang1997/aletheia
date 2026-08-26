"""Pinned public-key-only composition for the controller terminal-dispatcher role."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.allocator import (
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
    PostgreSQLExecutionReceiptArchive,
)
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.assignment_contracts import NodeAssignmentTransportPin
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
    NodeEnrollmentAuthorityPin,
    NodeEnrollmentAuthorityVerifier,
    QualificationAuthorityPin,
    QualificationAuthorityVerifier,
    TerminalVerificationAuthorityPin,
    TerminalVerificationAuthorityVerifier,
    WorkerNodeAuthorityVerifier,
    WorkerNodeEnrollment,
    WorkerNodeManifest,
)
from aletheia.execution.runtime_v2_contracts import (
    PinnedRuntimeControlVerificationAuthority,
    RuntimeControlAuthorityPin,
)
from aletheia.execution.terminal_source import (
    VerifiedQualificationRawRunMaterialReader,
    VerifiedQualificationRunLineageReader,
    VerifiedQualificationTerminalOutboxReader,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
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


class TerminalNodeAuthorityConfig(BaseModel):
    """Public deployment roots for one exact enrolled qualification node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: WorkerNodeManifest
    enrollment: WorkerNodeEnrollment
    enrollment_authority_pin: NodeEnrollmentAuthorityPin
    assignment_transport_pin: NodeAssignmentTransportPin

    @model_validator(mode="after")
    def _node_is_exact(self) -> "TerminalNodeAuthorityConfig":
        message = self.enrollment.message
        if (
            message.node_manifest_sha256 != self.manifest.manifest_sha256
            or message.node_id != self.manifest.node_id
            or self.assignment_transport_pin.node_id != self.manifest.node_id
            or self.assignment_transport_pin.node_manifest_sha256 != self.manifest.manifest_sha256
            or message.enrollment_authority_key_id != self.enrollment_authority_pin.key_id
            or message.enrolled_by_principal_id != self.enrollment_authority_pin.principal_id
            or message.node_enrollment_policy_sha256 != self.enrollment_authority_pin.policy_sha256
        ):
            raise ValueError("terminal node authority config is rebound")
        return self


class QualificationTerminalReaderConfig(BaseModel):
    """Reusable public verification material for one read-only terminal lineage reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aletheia.qualification_terminal_reader_config"] = (
        "aletheia.qualification_terminal_reader_config"
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
    runtime_control_authority_pin: RuntimeControlAuthorityPin
    node_authorities: tuple[TerminalNodeAuthorityConfig, ...] = Field(min_length=1)
    allowed_rate_card_sha256s: tuple[str, ...] = Field(min_length=1)
    allowed_currency_codes: tuple[str, ...] = Field(min_length=1)
    allocator_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    input_resolver_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    prepared_at: AwareDatetime
    scientific_authority: Literal[False] = False
    signing_private_key_loaded: Literal[False] = False
    execution_mutation_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _config_is_canonical_and_separated(self) -> "QualificationTerminalReaderConfig":
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
            raise ValueError("artifact and authority registry roots must not overlap")
        node_ids = tuple(item.manifest.node_id for item in self.node_authorities)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("terminal node authorities must be unique and canonical")
        if self.allowed_rate_card_sha256s != tuple(
            sorted(set(self.allowed_rate_card_sha256s))
        ) or any(
            len(item) != 64 or any(character not in "0123456789abcdef" for character in item)
            for item in self.allowed_rate_card_sha256s
        ):
            raise ValueError("allowed rate cards must be canonical SHA-256 identities")
        if self.allowed_currency_codes != tuple(sorted(set(self.allowed_currency_codes))):
            raise ValueError("allowed currencies must be unique and canonical")
        principals = (
            self.artifact_verifier_principal_id,
            self.input_resolver_principal_id,
            self.pricing_authority_pin.principal_id,
            self.source_budget_authority_pin.principal_id,
            self.qualification_authority_pin.principal_id,
            self.terminal_verification_authority_pin.principal_id,
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
            self.pricing_authority_pin.key_id,
            self.source_budget_authority_pin.key_id,
            self.qualification_authority_pin.key_id,
            self.terminal_verification_authority_pin.key_id,
            self.runtime_control_authority_pin.key_id,
            *(item.manifest.node_signing_key_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.key_id for item in self.node_authorities),
            *(item.assignment_transport_pin.transport_key_id for item in self.node_authorities),
        )
        duplicate_principals = tuple(
            sorted(value for value, count in Counter(principals).items() if count > 1)
        )
        duplicate_keys = tuple(sorted(value for value, count in Counter(keys).items() if count > 1))
        if duplicate_principals or duplicate_keys:
            raise ValueError(
                "terminal runtime authorities must use distinct principals and keys: "
                f"principals={duplicate_principals}, keys={duplicate_keys}"
            )
        return self

    @property
    def authority_principal_ids(self) -> tuple[str, ...]:
        """Every deployment identity reachable by the public reader composition."""

        return (
            self.artifact_verifier_principal_id,
            self.input_resolver_principal_id,
            self.pricing_authority_pin.principal_id,
            self.source_budget_authority_pin.principal_id,
            self.qualification_authority_pin.principal_id,
            self.terminal_verification_authority_pin.principal_id,
            self.runtime_control_authority_pin.principal_id,
            self.allocator_principal_id,
            *(item.manifest.principal_id for item in self.node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in self.node_authorities),
            *(
                item.assignment_transport_pin.transport_principal_id
                for item in self.node_authorities
            ),
        )


class QualificationTerminalRuntimeConfig(QualificationTerminalReaderConfig):
    """Closed terminal-dispatcher deployment configuration with public keys only."""

    schema_name: Literal["aletheia.qualification_terminal_runtime_config"] = (
        "aletheia.qualification_terminal_runtime_config"
    )
    schema_version: Literal[1] = 1
    role: Literal["terminal_dispatcher"]
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

    @model_validator(mode="after")
    def _process_principal_is_separate(self) -> "QualificationTerminalRuntimeConfig":
        if self.process_principal_id in self.authority_principal_ids:
            raise ValueError(
                "terminal runtime authorities must use distinct principals: "
                f"principals={(self.process_principal_id,)}"
            )
        return self


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate terminal runtime config keys: {duplicates}")
    return dict(pairs)


def compose_verified_qualification_terminal_reader(
    *,
    role: str,
    process_principal_id: str,
    controller_manifest_sha256: str,
    prepared_at: AwareDatetime,
    configuration_bytes: bytes,
) -> VerifiedQualificationTerminalOutboxReader:
    """Build only the public-key terminal source; the outer runtime owns the legacy queue."""

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=_unique_object)
        config = QualificationTerminalRuntimeConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("qualification terminal runtime config is invalid") from exc
    database_url_sha256 = hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
    if (
        role != "terminal_dispatcher"
        or config.role != role
        or config.process_principal_id != process_principal_id
        or config.controller_manifest_sha256 != controller_manifest_sha256
        or config.database_url_sha256 != database_url_sha256
        or config.schema_revision != expected_schema_revision()
        or config.prepared_at != prepared_at
    ):
        raise ValueError("qualification terminal runtime config differs from deployment state")

    return compose_qualification_terminal_reader(config)


def compose_qualification_terminal_reader(
    config: QualificationTerminalReaderConfig,
) -> VerifiedQualificationTerminalOutboxReader:
    """Compose the reusable public-key-only reader from an already pinned config."""

    return VerifiedQualificationTerminalOutboxReader(
        _compose_qualification_verification_allocator(config)
    )


def compose_qualification_raw_run_material_reader(
    config: QualificationTerminalReaderConfig,
) -> VerifiedQualificationRawRunMaterialReader:
    """Compose only the raw-run material read facade from public verification pins."""

    return VerifiedQualificationRawRunMaterialReader(
        _compose_qualification_verification_allocator(config)
    )


def compose_qualification_run_lineage_reader(
    config: QualificationTerminalReaderConfig,
) -> VerifiedQualificationRunLineageReader:
    """Compose only the complete run-lineage read facade from public verification pins."""

    return VerifiedQualificationRunLineageReader(
        _compose_qualification_verification_allocator(config)
    )


def _compose_qualification_verification_allocator(
    config: QualificationTerminalReaderConfig,
) -> PostgreSQLExecutionAllocator:
    """Build the internal verifier; callers receive only an operation-closed read facade."""

    try:
        payload = config.model_dump(
            mode="python",
            include=set(QualificationTerminalReaderConfig.model_fields),
        )
        payload["schema_name"] = "aletheia.qualification_terminal_reader_config"
        payload["schema_version"] = 1
        config = QualificationTerminalReaderConfig.model_validate(payload)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("qualification terminal reader config is invalid") from exc

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
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(config.qualification_authority_pin),
        artifact_resolver=artifact_resolver,
        execution_authority_resolver=execution_authority_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({config.pricing_authority_pin.principal_id}),
            rate_card_sha256s=frozenset(config.allowed_rate_card_sha256s),
            pricing_policy_sha256s=frozenset({config.pricing_authority_pin.policy_sha256}),
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
    )
    return allocator


__all__ = [
    "QualificationTerminalReaderConfig",
    "QualificationTerminalRuntimeConfig",
    "TerminalNodeAuthorityConfig",
    "compose_qualification_raw_run_material_reader",
    "compose_qualification_run_lineage_reader",
    "compose_qualification_terminal_reader",
    "compose_verified_qualification_terminal_reader",
]
