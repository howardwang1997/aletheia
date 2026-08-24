"""Pure contracts for the local execution-foundation qualification boundary.

The values in this module contain no placement, process, filesystem, database, or scientific
admission authority.  A deployment-pinned qualification key may authorize a bounded engineering
exercise, but the resulting grant is deliberately incapable of admitting an observation.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.ports import (
    ExecutionAuthorityResolverPort,
    VerifiedInputArtifactResolverPort,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    ExecutionEffectClass,
    ExecutionIntent,
    ExecutionModel,
    ExecutionReceipt,
    ExecutionRetryMode,
    ExecutionTerminalState,
    NetworkPolicy,
    ResourceKind,
    canonical_json_bytes,
    canonical_sha256,
    verify_execution_retry_binding,
)
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    verify_compilation,
    verify_execution_intent_binding,
)
from aletheia.protocols.schemas import ProtocolCompilationResult, WorkOrderDAG

RUNTIME_CONTRACT_SCHEMA_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_REPLICATE_SLOT_ID_PATTERN = r"^rps_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"


def _canonical_strings(
    values: tuple[str, ...], label: str, *, required: bool = False
) -> tuple[str, ...]:
    if required and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not item or item != item.strip() for item in values):
        raise ValueError(f"{label} must contain nonempty canonical strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


def _public_key_bytes(private_key: bytes) -> bytes:
    if len(private_key) != 32:
        raise ValueError("Ed25519 private keys must contain exactly 32 raw bytes")
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _require_utc_timestamp(timestamp: datetime, label: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise QualificationVerificationError(f"{label} must be timezone-aware UTC")
    if timestamp.utcoffset().total_seconds() != 0:
        raise QualificationVerificationError(f"{label} must be timezone-aware UTC")


def qualification_key_id(public_key_ed25519_hex: str) -> str:
    """Derive the deployment pin identity from one raw Ed25519 public key."""

    try:
        public_key = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("Ed25519 public keys must be hexadecimal") from exc
    if len(public_key) != 32:
        raise ValueError("Ed25519 public keys must contain exactly 32 raw bytes")
    return hashlib.sha256(public_key).hexdigest()


class NodeHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class WorkerNodeManifest(ExecutionModel):
    """Frozen deployment identity and policy envelope for one pull-based node agent."""

    schema_name: Literal["aletheia.worker_node_manifest"] = "aletheia.worker_node_manifest"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    site_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    agent_version: str = Field(min_length=1, max_length=64)
    agent_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    operating_system: str = Field(min_length=1, max_length=128)
    cpu_architecture: str = Field(min_length=1, max_length=64)
    oci_platform: str = Field(min_length=1, max_length=128)
    container_runtime: str = Field(min_length=1, max_length=128)
    sandbox_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_class_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    allowed_data_classifications: tuple[str, ...] = Field(min_length=1, max_length=128)
    network_policies: tuple[NetworkPolicy, ...] = Field(min_length=1, max_length=8)
    egress_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    node_signing_public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_valid_from: AwareDatetime
    key_expires_at: AwareDatetime
    key_revoked_at: AwareDatetime | None = None
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _manifest_is_canonical_and_key_bound(self) -> "WorkerNodeManifest":
        _canonical_strings(self.resource_class_ids, "node resource classes", required=True)
        _canonical_strings(
            self.allowed_data_classifications,
            "node data classifications",
            required=True,
        )
        expected_network = tuple(sorted(set(self.network_policies), key=lambda item: item.value))
        if self.network_policies != expected_network:
            raise ValueError("node network policies must be unique and canonical")
        if self.node_signing_key_id != qualification_key_id(
            self.node_signing_public_key_ed25519_hex
        ):
            raise ValueError("node signing key id does not match its public key")
        if not self.key_valid_from <= self.frozen_at < self.key_expires_at:
            raise ValueError("node manifest freeze must fall inside its key validity")
        if self.key_revoked_at is not None and not (
            self.key_valid_from <= self.key_revoked_at <= self.key_expires_at
        ):
            raise ValueError("node key revocation must fall inside its validity window")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class NodeEnrollmentAuthorityPin(ExecutionModel):
    """Deployment-owned root key used only for worker-node enrollment."""

    schema_name: Literal["aletheia.node_enrollment_authority_pin"] = (
        "aletheia.node_enrollment_authority_pin"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    signature_domain: Literal["ALETHEIA_WORKER_NODE_ENROLLMENT_V1"] = (
        "ALETHEIA_WORKER_NODE_ENROLLMENT_V1"
    )
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_valid(self) -> "NodeEnrollmentAuthorityPin":
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("node-enrollment key id does not match its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("node-enrollment key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("node-enrollment key revocation is outside its validity")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class WorkerNodeEnrollmentMessage(ExecutionModel):
    """Deployment-root authorization for one exact immutable node manifest."""

    schema_name: Literal["aletheia.worker_node_enrollment_message"] = (
        "aletheia.worker_node_enrollment_message"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    signature_domain: Literal["ALETHEIA_WORKER_NODE_ENROLLMENT_V1"] = (
        "ALETHEIA_WORKER_NODE_ENROLLMENT_V1"
    )
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    site_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    node_signing_public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_enrollment_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    enrolled_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    enrollment_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _enrollment_window_is_valid(self) -> "WorkerNodeEnrollmentMessage":
        if self.expires_at <= self.issued_at:
            raise ValueError("worker node enrollment expiry must follow issuance")
        return self

    @property
    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class WorkerNodeEnrollment(ExecutionModel):
    """Signed registration certificate; a node cannot self-enroll with its own key."""

    schema_name: Literal["aletheia.worker_node_enrollment"] = "aletheia.worker_node_enrollment"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    message: WorkerNodeEnrollmentMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def enrollment_sha256(self) -> str:
        return canonical_sha256(self)


class NodeEnrollmentAuthorityVerifier:
    """Verify enrollment certificates against an independently pinned deployment root."""

    def __init__(self, pin: NodeEnrollmentAuthorityPin) -> None:
        self._pin = NodeEnrollmentAuthorityPin.model_validate(pin.model_dump(mode="python"))

    @property
    def pin(self) -> NodeEnrollmentAuthorityPin:
        return self._pin

    def verify_signature(
        self,
        enrollment: WorkerNodeEnrollment,
        *,
        observed_at: datetime,
    ) -> None:
        _require_utc_timestamp(observed_at, "worker node enrollment verifier observed_at")
        enrollment = WorkerNodeEnrollment.model_validate(enrollment.model_dump(mode="python"))
        message = enrollment.message
        if (
            message.signature_domain != self._pin.signature_domain
            or message.node_enrollment_policy_sha256 != self._pin.policy_sha256
            or message.enrolled_by_principal_id != self._pin.principal_id
            or message.enrollment_authority_key_id != self._pin.key_id
        ):
            raise QualificationVerificationError(
                "worker node enrollment is not issued by the deployment-pinned root"
            )
        if not message.issued_at <= observed_at < message.expires_at:
            raise QualificationVerificationError("worker node enrollment is inactive")
        if message.expires_at > self._pin.active_until or not (
            self._pin.active_at(message.issued_at) and self._pin.active_at(observed_at)
        ):
            raise QualificationVerificationError(
                "worker node enrollment outlives its deployment root"
            )
        try:
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(self._pin.public_key_ed25519_hex)
            ).verify(bytes.fromhex(enrollment.signature_ed25519_hex), message.message_bytes)
        except (InvalidSignature, ValueError) as exc:
            raise QualificationVerificationError(
                "worker node enrollment signature is invalid"
            ) from exc


def issue_worker_node_enrollment(
    *,
    manifest: WorkerNodeManifest,
    pin: NodeEnrollmentAuthorityPin,
    private_key: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> WorkerNodeEnrollment:
    """Deployment-side helper for an exact, domain-separated node enrollment."""

    _require_utc_timestamp(issued_at, "worker node enrollment issued_at")
    _require_utc_timestamp(expires_at, "worker node enrollment expires_at")
    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    pin = NodeEnrollmentAuthorityPin.model_validate(pin.model_dump(mode="python"))
    manifest_active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if _public_key_bytes(private_key).hex() != pin.public_key_ed25519_hex:
        raise QualificationVerificationError(
            "node-enrollment private key does not match the deployment root"
        )
    if not (
        manifest.frozen_at <= issued_at < expires_at
        and pin.active_at(issued_at)
        and expires_at <= min(pin.active_until, manifest_active_until)
    ):
        raise QualificationVerificationError("worker node enrollment has an invalid time window")
    message = WorkerNodeEnrollmentMessage(
        node_manifest_sha256=manifest.manifest_sha256,
        node_id=manifest.node_id,
        site_id=manifest.site_id,
        principal_id=manifest.principal_id,
        node_signing_key_id=manifest.node_signing_key_id,
        node_signing_public_key_ed25519_hex=(manifest.node_signing_public_key_ed25519_hex),
        node_enrollment_policy_sha256=pin.policy_sha256,
        enrolled_by_principal_id=pin.principal_id,
        enrollment_authority_key_id=pin.key_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message.message_bytes)
    return WorkerNodeEnrollment(
        message=message,
        signature_ed25519_hex=signature.hex(),
    )


def _verify_worker_node_signature_against_manifest(
    *,
    manifest: WorkerNodeManifest,
    signing_key_id: str,
    message: bytes,
    signature_ed25519_hex: str,
    signed_at: datetime,
) -> None:
    _require_utc_timestamp(signed_at, "worker node signature time")
    manifest_active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if (
        signing_key_id != manifest.node_signing_key_id
        or not manifest.frozen_at <= signed_at < manifest_active_until
    ):
        raise QualificationVerificationError(
            "worker node signature is outside its exact manifest key authority"
        )
    try:
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(manifest.node_signing_public_key_ed25519_hex)
        ).verify(bytes.fromhex(signature_ed25519_hex), message)
    except (InvalidSignature, ValueError) as exc:
        raise QualificationVerificationError("worker node signature is invalid") from exc


class WorkerNodeAuthorityVerifier:
    """Verify node signatures only after deployment-root enrollment of an exact manifest."""

    def __init__(
        self,
        *,
        manifest: WorkerNodeManifest,
        enrollment: WorkerNodeEnrollment,
        enrollment_authority: NodeEnrollmentAuthorityVerifier,
        expected_manifest_sha256: str,
        observed_at: datetime,
    ) -> None:
        _require_utc_timestamp(observed_at, "worker node registration observed_at")
        self._manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
        self._enrollment = WorkerNodeEnrollment.model_validate(enrollment.model_dump(mode="python"))
        enrollment_authority.verify_signature(self._enrollment, observed_at=observed_at)
        message = self._enrollment.message
        if (
            expected_manifest_sha256 != self._manifest.manifest_sha256
            or message.node_manifest_sha256 != self._manifest.manifest_sha256
            or message.node_id != self._manifest.node_id
            or message.site_id != self._manifest.site_id
            or message.principal_id != self._manifest.principal_id
            or message.node_signing_key_id != self._manifest.node_signing_key_id
            or message.node_signing_public_key_ed25519_hex
            != self._manifest.node_signing_public_key_ed25519_hex
        ):
            raise QualificationVerificationError(
                "WorkerNodeManifest differs from its deployment-root enrollment"
            )
        manifest_active_until = min(
            self._manifest.key_expires_at,
            self._manifest.key_revoked_at or self._manifest.key_expires_at,
        )
        if message.expires_at > manifest_active_until:
            raise QualificationVerificationError(
                "worker node enrollment outlives its manifest signing key"
            )
        self._enrollment_authority = enrollment_authority

    @property
    def enrollment(self) -> WorkerNodeEnrollment:
        return self._enrollment

    @property
    def enrollment_authority_pin(self) -> NodeEnrollmentAuthorityPin:
        """Exact immutable deployment-root pin required for persistence/revalidation."""

        return self._enrollment_authority.pin

    @property
    def manifest(self) -> WorkerNodeManifest:
        return self._manifest

    @property
    def active_until(self) -> datetime:
        return min(
            self._enrollment.message.expires_at,
            self._enrollment_authority.pin.active_until,
            self._manifest.key_expires_at,
            self._manifest.key_revoked_at or self._manifest.key_expires_at,
        )

    def verify_signature(
        self,
        *,
        signing_key_id: str,
        message: bytes,
        signature_ed25519_hex: str,
        signed_at: datetime,
    ) -> None:
        if not (
            self._enrollment.message.issued_at <= signed_at < self.active_until
            and self._enrollment_authority.pin.active_at(signed_at)
        ):
            raise QualificationVerificationError(
                "deployment-enrolled WorkerNodeManifest is inactive"
            )
        _verify_worker_node_signature_against_manifest(
            manifest=self._manifest,
            signing_key_id=signing_key_id,
            message=message,
            signature_ed25519_hex=signature_ed25519_hex,
            signed_at=signed_at,
        )


def verify_worker_node_enrollment(
    *,
    manifest: WorkerNodeManifest,
    enrollment: WorkerNodeEnrollment,
    enrollment_authority: NodeEnrollmentAuthorityVerifier,
    expected_manifest_sha256: str,
    observed_at: datetime,
) -> WorkerNodeAuthorityVerifier:
    """Return a node verifier only after exact deployment-root registration succeeds."""

    return WorkerNodeAuthorityVerifier(
        manifest=manifest,
        enrollment=enrollment,
        enrollment_authority=enrollment_authority,
        expected_manifest_sha256=expected_manifest_sha256,
        observed_at=observed_at,
    )


class NodeInventoryResource(ExecutionModel):
    """One live local resource after safety reserve and managed/external occupancy."""

    resource_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    kind: ResourceKind
    resource_class_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    health: NodeHealth
    cpu_cores_total: int = Field(ge=0)
    cpu_cores_safety_reserve: int = Field(ge=0)
    cpu_cores_managed_occupied: int = Field(ge=0)
    cpu_cores_external_occupied: int = Field(ge=0)
    cpu_cores_allocatable: int = Field(ge=0)
    memory_bytes_total: int = Field(ge=0)
    memory_bytes_safety_reserve: int = Field(ge=0)
    memory_bytes_managed_occupied: int = Field(ge=0)
    memory_bytes_external_occupied: int = Field(ge=0)
    memory_bytes_allocatable: int = Field(ge=0)
    scratch_bytes_total: int = Field(ge=0)
    scratch_bytes_safety_reserve: int = Field(ge=0)
    scratch_bytes_managed_occupied: int = Field(ge=0)
    scratch_bytes_external_occupied: int = Field(ge=0)
    scratch_bytes_allocatable: int = Field(ge=0)
    accelerator_uuid: str | None = Field(default=None, pattern=_SYMBOLIC_ID_PATTERN)
    accelerator_model: str | None = Field(default=None, min_length=1, max_length=128)
    accelerator_memory_bytes_total: int | None = Field(default=None, ge=1)
    accelerator_memory_bytes_safety_reserve: int | None = Field(default=None, ge=0)
    accelerator_memory_bytes_managed_occupied: int | None = Field(default=None, ge=0)
    accelerator_memory_bytes_external_occupied: int | None = Field(default=None, ge=0)
    accelerator_memory_bytes_allocatable: int | None = Field(default=None, ge=0)
    accelerator_compute_capability: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$")
    features: tuple[str, ...] = ()
    external_process_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _resource_reconciles(self) -> "NodeInventoryResource":
        _canonical_strings(self.resource_class_ids, "inventory resource classes", required=True)
        _canonical_strings(self.features, "inventory resource features")
        if self.kind is ResourceKind.EXTERNAL:
            raise ValueError("external providers are not local node inventory resources")
        expected_cpu = max(
            0,
            self.cpu_cores_total
            - self.cpu_cores_safety_reserve
            - self.cpu_cores_managed_occupied
            - self.cpu_cores_external_occupied,
        )
        if self.cpu_cores_allocatable != expected_cpu:
            raise ValueError("allocatable CPU does not reconcile to occupancy and reserve")
        expected_memory = max(
            0,
            self.memory_bytes_total
            - self.memory_bytes_safety_reserve
            - self.memory_bytes_managed_occupied
            - self.memory_bytes_external_occupied,
        )
        if self.memory_bytes_allocatable != expected_memory:
            raise ValueError("allocatable memory does not reconcile to occupancy and reserve")
        expected_scratch = max(
            0,
            self.scratch_bytes_total
            - self.scratch_bytes_safety_reserve
            - self.scratch_bytes_managed_occupied
            - self.scratch_bytes_external_occupied,
        )
        if self.scratch_bytes_allocatable != expected_scratch:
            raise ValueError("allocatable scratch does not reconcile to occupancy and reserve")
        accelerator_fields = (
            self.accelerator_uuid,
            self.accelerator_model,
            self.accelerator_memory_bytes_total,
            self.accelerator_memory_bytes_safety_reserve,
            self.accelerator_memory_bytes_managed_occupied,
            self.accelerator_memory_bytes_external_occupied,
            self.accelerator_memory_bytes_allocatable,
            self.accelerator_compute_capability,
        )
        if self.kind is ResourceKind.ACCELERATOR:
            if any(item is None for item in accelerator_fields):
                raise ValueError("accelerator inventory requires complete device identity")
            assert self.accelerator_memory_bytes_total is not None
            assert self.accelerator_memory_bytes_safety_reserve is not None
            assert self.accelerator_memory_bytes_managed_occupied is not None
            assert self.accelerator_memory_bytes_external_occupied is not None
            assert self.accelerator_memory_bytes_allocatable is not None
            expected_accelerator = max(
                0,
                self.accelerator_memory_bytes_total
                - self.accelerator_memory_bytes_safety_reserve
                - self.accelerator_memory_bytes_managed_occupied
                - self.accelerator_memory_bytes_external_occupied,
            )
            if self.accelerator_memory_bytes_allocatable != expected_accelerator:
                raise ValueError(
                    "allocatable accelerator memory does not reconcile to occupancy and reserve"
                )
        elif any(item is not None for item in accelerator_fields):
            raise ValueError("CPU inventory cannot declare accelerator identity")
        if self.external_process_count == 0 and (
            self.cpu_cores_external_occupied > 0
            or self.memory_bytes_external_occupied > 0
            or self.scratch_bytes_external_occupied > 0
            or (self.accelerator_memory_bytes_external_occupied or 0) > 0
        ):
            raise ValueError("external occupancy requires at least one observed external process")
        if self.health is not NodeHealth.HEALTHY and (
            self.cpu_cores_allocatable
            or self.memory_bytes_allocatable
            or self.scratch_bytes_allocatable
            or (self.accelerator_memory_bytes_allocatable or 0)
        ):
            raise ValueError("non-healthy resources cannot advertise allocatable capacity")
        return self


class NodeInventoryAttestation(ExecutionModel):
    """Short-lived signed live inventory, never a static resource catalog."""

    schema_name: Literal["aletheia.node_inventory_attestation"] = (
        "aletheia.node_inventory_attestation"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    site_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    sequence: int = Field(ge=1)
    observed_monotonic_ns: int = Field(ge=0)
    resources: tuple[NodeInventoryResource, ...] = Field(min_length=1, max_length=1024)
    collector_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    collector_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _inventory_is_canonical(self) -> "NodeInventoryAttestation":
        expected = tuple(sorted(self.resources, key=lambda item: item.resource_id))
        if self.resources != expected or len({item.resource_id for item in self.resources}) != len(
            self.resources
        ):
            raise ValueError("inventory resources must be unique and canonical")
        if self.expires_at <= self.observed_at:
            raise ValueError("inventory expiry must follow observation")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def inventory_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedNodeInventoryAttestation(ExecutionModel):
    """Pure verification result for one deployment-pinned live inventory snapshot."""

    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    sequence: int = Field(ge=1)
    observed_monotonic_ns: int = Field(ge=0)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def issue_node_inventory_attestation(
    *,
    manifest: WorkerNodeManifest,
    boot_id: str,
    sequence: int,
    observed_monotonic_ns: int,
    resources: tuple[NodeInventoryResource, ...],
    collector_implementation_sha256: str,
    collector_output_sha256: str,
    observed_at: datetime,
    expires_at: datetime,
    private_key: bytes,
) -> NodeInventoryAttestation:
    """Sign a short-lived snapshot with the exact key frozen in ``manifest``."""

    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    _require_utc_timestamp(observed_at, "inventory observed_at")
    _require_utc_timestamp(expires_at, "inventory expires_at")
    if _public_key_bytes(private_key).hex() != manifest.node_signing_public_key_ed25519_hex:
        raise QualificationVerificationError("inventory private key does not match node manifest")
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if not manifest.frozen_at <= observed_at < expires_at <= active_until:
        raise QualificationVerificationError(
            "inventory validity must be inside the exact manifest signing-key window"
        )
    unsigned = NodeInventoryAttestation(
        node_manifest_sha256=manifest.manifest_sha256,
        node_id=manifest.node_id,
        site_id=manifest.site_id,
        principal_id=manifest.principal_id,
        boot_id=boot_id,
        sequence=sequence,
        observed_monotonic_ns=observed_monotonic_ns,
        resources=resources,
        collector_implementation_sha256=collector_implementation_sha256,
        collector_output_sha256=collector_output_sha256,
        observed_at=observed_at,
        expires_at=expires_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    signed = NodeInventoryAttestation.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )
    _validate_inventory_manifest_binding(signed, manifest)
    _verify_worker_node_signature_against_manifest(
        manifest=manifest,
        signing_key_id=signed.signing_key_id,
        message=signed.signature_message,
        signature_ed25519_hex=signed.signature_ed25519_hex,
        signed_at=signed.observed_at,
    )
    return signed


def _validate_inventory_manifest_binding(
    attestation: NodeInventoryAttestation,
    manifest: WorkerNodeManifest,
) -> None:
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if (
        attestation.node_manifest_sha256 != manifest.manifest_sha256
        or attestation.node_id != manifest.node_id
        or attestation.site_id != manifest.site_id
        or attestation.principal_id != manifest.principal_id
        or attestation.signing_key_id != manifest.node_signing_key_id
    ):
        raise QualificationVerificationError(
            "inventory is not bound to the exact WorkerNodeManifest identity"
        )
    resource_classes = {
        resource_class_id
        for resource in attestation.resources
        for resource_class_id in resource.resource_class_ids
    }
    if not resource_classes or not resource_classes.issubset(set(manifest.resource_class_ids)):
        raise QualificationVerificationError(
            "inventory advertises a resource class outside its frozen node manifest"
        )
    if not manifest.frozen_at <= attestation.observed_at < attestation.expires_at <= active_until:
        raise QualificationVerificationError(
            "inventory validity is outside the manifest signing-key window"
        )


def _verify_inventory_signature(
    attestation: NodeInventoryAttestation,
    authority: WorkerNodeAuthorityVerifier,
) -> None:
    manifest = authority.manifest
    _validate_inventory_manifest_binding(attestation, manifest)
    if attestation.expires_at > authority.active_until:
        raise QualificationVerificationError(
            "inventory outlives its deployment-pinned node authority"
        )
    authority.verify_signature(
        signing_key_id=attestation.signing_key_id,
        message=attestation.signature_message,
        signature_ed25519_hex=attestation.signature_ed25519_hex,
        signed_at=attestation.observed_at,
    )


def verify_node_inventory_attestation(
    *,
    attestation: NodeInventoryAttestation,
    authority: WorkerNodeAuthorityVerifier,
    expected_manifest_sha256: str,
    observed_at: datetime,
    previous_attestation: NodeInventoryAttestation | None = None,
) -> VerifiedNodeInventoryAttestation:
    """Verify identity, key, wall-clock freshness and same-boot monotonic ordering.

    ``observed_at`` is a trusted allocator/database clock supplied by the caller.  This pure
    verifier deliberately never reads a process or host clock and cannot attest the caller's time.
    """

    _require_utc_timestamp(observed_at, "inventory verifier observed_at")
    try:
        attestation = NodeInventoryAttestation.model_validate(attestation.model_dump(mode="python"))
        if expected_manifest_sha256 != authority.manifest.manifest_sha256:
            raise QualificationVerificationError(
                "inventory expected manifest differs from the registered node authority"
            )
        _verify_inventory_signature(attestation, authority)
        if not attestation.observed_at <= observed_at < attestation.expires_at:
            raise QualificationVerificationError("node inventory is not fresh at allocator time")
        if previous_attestation is not None:
            previous = NodeInventoryAttestation.model_validate(
                previous_attestation.model_dump(mode="python")
            )
            _verify_inventory_signature(previous, authority)
            if previous.observed_at >= attestation.observed_at:
                raise QualificationVerificationError("node inventory wall-clock order regressed")
            if previous.boot_id == attestation.boot_id:
                if previous.sequence >= attestation.sequence:
                    raise QualificationVerificationError(
                        "same-boot node inventory sequence did not advance"
                    )
                if previous.observed_monotonic_ns >= attestation.observed_monotonic_ns:
                    raise QualificationVerificationError(
                        "same-boot node inventory monotonic time did not advance"
                    )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError("node inventory failed closed revalidation") from exc
    return VerifiedNodeInventoryAttestation(
        node_manifest_sha256=authority.manifest.manifest_sha256,
        node_inventory_sha256=attestation.inventory_sha256,
        node_id=attestation.node_id,
        boot_id=attestation.boot_id,
        sequence=attestation.sequence,
        observed_monotonic_ns=attestation.observed_monotonic_ns,
        verified_at=observed_at,
    )


class NodeRuntimeIdentity(ExecutionModel):
    """PID-reuse-safe identity for one node-local runtime instance."""

    schema_name: Literal["aletheia.node_runtime_identity"] = "aletheia.node_runtime_identity"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    runtime_engine: str = Field(min_length=1, max_length=128)
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    started_monotonic_ns: int = Field(ge=0)

    @property
    def runtime_identity_sha256(self) -> str:
        return canonical_sha256(self)


class RuntimeInspectionState(str, Enum):
    RUNNING = "running"
    TERMINATED = "terminated"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class RuntimeInspectionReceipt(ExecutionModel):
    """Node-signed inspection of one exact runtime under one exact fence."""

    schema_name: Literal["aletheia.runtime_inspection_receipt"] = (
        "aletheia.runtime_inspection_receipt"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity: NodeRuntimeIdentity
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    state: RuntimeInspectionState
    inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspected_at: AwareDatetime
    inspected_monotonic_ns: int = Field(ge=0)
    expires_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _inspection_is_exact_and_ordered(self) -> "RuntimeInspectionReceipt":
        if self.runtime_identity_sha256 != self.runtime_identity.runtime_identity_sha256:
            raise ValueError("runtime inspection identity differs from its exact bytes")
        if (
            self.inspected_at < self.runtime_identity.started_at
            or self.inspected_monotonic_ns < self.runtime_identity.started_monotonic_ns
            or self.expires_at <= self.inspected_at
        ):
            raise ValueError("runtime inspection time or node monotonic order is invalid")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def inspection_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedRuntimeInspection(ExecutionModel):
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    state: RuntimeInspectionState
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def issue_runtime_inspection_receipt(
    *,
    manifest: WorkerNodeManifest,
    runtime_identity: NodeRuntimeIdentity,
    fencing_epoch: int,
    lease_token_sha256: str,
    inspection_sequence: int,
    state: RuntimeInspectionState,
    inspection_evidence_sha256: str,
    inspected_at: datetime,
    inspected_monotonic_ns: int,
    expires_at: datetime,
    private_key: bytes,
) -> RuntimeInspectionReceipt:
    """Node-side helper that signs a bounded runtime observation."""

    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    runtime_identity = NodeRuntimeIdentity.model_validate(
        runtime_identity.model_dump(mode="python")
    )
    if (
        runtime_identity.node_id != manifest.node_id
        or _public_key_bytes(private_key).hex() != manifest.node_signing_public_key_ed25519_hex
    ):
        raise QualificationVerificationError(
            "runtime inspection signer or runtime belongs to another node manifest"
        )
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if not manifest.frozen_at <= inspected_at < expires_at <= active_until:
        raise QualificationVerificationError(
            "runtime inspection validity is outside its node signing authority"
        )
    unsigned = RuntimeInspectionReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        runtime_identity=runtime_identity,
        runtime_identity_sha256=runtime_identity.runtime_identity_sha256,
        fencing_epoch=fencing_epoch,
        lease_token_sha256=lease_token_sha256,
        inspection_sequence=inspection_sequence,
        state=state,
        inspection_evidence_sha256=inspection_evidence_sha256,
        inspected_at=inspected_at,
        inspected_monotonic_ns=inspected_monotonic_ns,
        expires_at=expires_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return RuntimeInspectionReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def _verify_runtime_inspection(
    *,
    receipt: RuntimeInspectionReceipt,
    authority: WorkerNodeAuthorityVerifier,
    expected_runtime_identity: NodeRuntimeIdentity,
    expected_fencing_epoch: int,
    expected_lease_token_sha256: str,
    permitted_states: frozenset[RuntimeInspectionState],
    maximum_inspection_age_seconds: int,
    observed_at: datetime,
) -> VerifiedRuntimeInspection:
    _require_utc_timestamp(observed_at, "runtime inspection verifier observed_at")
    if maximum_inspection_age_seconds < 1:
        raise QualificationVerificationError(
            "runtime inspection maximum age must be a positive deployment bound"
        )
    try:
        receipt = RuntimeInspectionReceipt.model_validate(receipt.model_dump(mode="python"))
        expected_runtime_identity = NodeRuntimeIdentity.model_validate(
            expected_runtime_identity.model_dump(mode="python")
        )
        manifest = authority.manifest
        if (
            receipt.node_manifest_sha256 != manifest.manifest_sha256
            or receipt.runtime_identity != expected_runtime_identity
            or receipt.runtime_identity.node_id != manifest.node_id
            or receipt.fencing_epoch != expected_fencing_epoch
            or receipt.lease_token_sha256 != expected_lease_token_sha256
            or receipt.state not in permitted_states
        ):
            raise QualificationVerificationError(
                "runtime inspection differs from its exact pinned runtime authority"
            )
        if not receipt.inspected_at <= observed_at < receipt.expires_at:
            raise QualificationVerificationError(
                "runtime inspection is not fresh at allocator database time"
            )
        maximum_age = timedelta(seconds=maximum_inspection_age_seconds)
        if (
            receipt.expires_at - receipt.inspected_at > maximum_age
            or observed_at - receipt.inspected_at > maximum_age
        ):
            raise QualificationVerificationError(
                "runtime inspection exceeds the deployment-pinned freshness bound"
            )
        if receipt.expires_at > authority.active_until:
            raise QualificationVerificationError(
                "runtime inspection outlives deployment-pinned node authority"
            )
        authority.verify_signature(
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.inspected_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "runtime inspection failed closed revalidation"
        ) from exc
    return VerifiedRuntimeInspection(
        node_manifest_sha256=authority.manifest.manifest_sha256,
        runtime_inspection_receipt_sha256=receipt.inspection_receipt_sha256,
        runtime_identity_sha256=receipt.runtime_identity_sha256,
        execution_id=receipt.runtime_identity.execution_id,
        infrastructure_attempt_id=receipt.runtime_identity.infrastructure_attempt_id,
        fencing_epoch=receipt.fencing_epoch,
        lease_token_sha256=receipt.lease_token_sha256,
        state=receipt.state,
        verified_at=observed_at,
    )


def verify_runtime_for_adoption(
    *,
    receipt: RuntimeInspectionReceipt,
    authority: WorkerNodeAuthorityVerifier,
    expected_runtime_identity: NodeRuntimeIdentity,
    expected_fencing_epoch: int,
    expected_lease_token_sha256: str,
    maximum_inspection_age_seconds: int,
    observed_at: datetime,
) -> VerifiedRuntimeInspection:
    """Authorize adoption only when the exact old-fence runtime is observed running."""

    return _verify_runtime_inspection(
        receipt=receipt,
        authority=authority,
        expected_runtime_identity=expected_runtime_identity,
        expected_fencing_epoch=expected_fencing_epoch,
        expected_lease_token_sha256=expected_lease_token_sha256,
        permitted_states=frozenset({RuntimeInspectionState.RUNNING}),
        maximum_inspection_age_seconds=maximum_inspection_age_seconds,
        observed_at=observed_at,
    )


def verify_runtime_for_release_or_retry(
    *,
    receipt: RuntimeInspectionReceipt,
    authority: WorkerNodeAuthorityVerifier,
    expected_runtime_identity: NodeRuntimeIdentity,
    expected_fencing_epoch: int,
    expected_lease_token_sha256: str,
    maximum_inspection_age_seconds: int,
    observed_at: datetime,
) -> VerifiedRuntimeInspection:
    """Authorize release/retry only after exact terminated-or-absent node proof."""

    return _verify_runtime_inspection(
        receipt=receipt,
        authority=authority,
        expected_runtime_identity=expected_runtime_identity,
        expected_fencing_epoch=expected_fencing_epoch,
        expected_lease_token_sha256=expected_lease_token_sha256,
        permitted_states=frozenset(
            {RuntimeInspectionState.TERMINATED, RuntimeInspectionState.ABSENT}
        ),
        maximum_inspection_age_seconds=maximum_inspection_age_seconds,
        observed_at=observed_at,
    )


class AttemptAdoptionReason(str, Enum):
    CONTROL_PLANE_FAILOVER = "control_plane_failover"
    ALLOCATOR_PROCESS_RECOVERY = "allocator_process_recovery"
    NODE_AGENT_RECONNECT = "node_agent_reconnect"


class AttemptAdoptionReceipt(ExecutionModel):
    """Node-acknowledged same-runtime fence rotation under a singleton local lock."""

    schema_name: Literal["aletheia.attempt_adoption_receipt"] = "aletheia.attempt_adoption_receipt"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_receipt: RuntimeInspectionReceipt
    runtime_inspection_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    adoption_sequence: int = Field(ge=1)
    previous_fencing_epoch: int = Field(ge=1)
    new_fencing_epoch: int = Field(ge=2)
    previous_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason: AttemptAdoptionReason
    singleton_lock_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    singleton_lock_acquired_monotonic_ns: int = Field(ge=0)
    allocator_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    adopted_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _adoption_is_one_exact_running_runtime_rotation(self) -> "AttemptAdoptionReceipt":
        inspection = self.runtime_inspection_receipt
        runtime = inspection.runtime_identity
        if (
            self.runtime_inspection_receipt_sha256 != inspection.inspection_receipt_sha256
            or inspection.state is not RuntimeInspectionState.RUNNING
            or self.node_manifest_sha256 != inspection.node_manifest_sha256
            or self.node_id != runtime.node_id
            or self.boot_id != runtime.boot_id
            or self.execution_id != runtime.execution_id
            or self.infrastructure_attempt_id != runtime.infrastructure_attempt_id
            or self.runtime_identity_sha256 != runtime.runtime_identity_sha256
            or self.previous_fencing_epoch != inspection.fencing_epoch
            or self.previous_lease_token_sha256 != inspection.lease_token_sha256
            or self.new_fencing_epoch != self.previous_fencing_epoch + 1
            or self.new_lease_token_sha256 == self.previous_lease_token_sha256
        ):
            raise ValueError("attempt adoption changed its exact node/runtime/fence authority")
        if (
            self.singleton_lock_acquired_monotonic_ns < inspection.inspected_monotonic_ns
            or not inspection.inspected_at <= self.adopted_at < inspection.expires_at
        ):
            raise ValueError("attempt adoption lacks ordered fresh singleton-lock evidence")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def adoption_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedAttemptAdoption(ExecutionModel):
    adoption_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    previous_fencing_epoch: int = Field(ge=1)
    new_fencing_epoch: int = Field(ge=2)
    previous_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    singleton_lock_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def issue_attempt_adoption_receipt(
    *,
    manifest: WorkerNodeManifest,
    runtime_inspection_receipt: RuntimeInspectionReceipt,
    adoption_sequence: int,
    new_fencing_epoch: int,
    new_lease_token_sha256: str,
    reason: AttemptAdoptionReason,
    singleton_lock_evidence_sha256: str,
    singleton_lock_acquired_monotonic_ns: int,
    allocator_principal_id: str,
    adopted_at: datetime,
    private_key: bytes,
) -> AttemptAdoptionReceipt:
    """Node-side acknowledgement of one typed adoption; opaque reasons are impossible."""

    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    inspection = RuntimeInspectionReceipt.model_validate(
        runtime_inspection_receipt.model_dump(mode="python")
    )
    if (
        _public_key_bytes(private_key).hex() != manifest.node_signing_public_key_ed25519_hex
        or inspection.node_manifest_sha256 != manifest.manifest_sha256
        or inspection.runtime_identity.node_id != manifest.node_id
    ):
        raise QualificationVerificationError("adoption private key differs from node manifest")
    _verify_worker_node_signature_against_manifest(
        manifest=manifest,
        signing_key_id=inspection.signing_key_id,
        message=inspection.signature_message,
        signature_ed25519_hex=inspection.signature_ed25519_hex,
        signed_at=inspection.inspected_at,
    )
    runtime = inspection.runtime_identity
    unsigned = AttemptAdoptionReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        node_id=runtime.node_id,
        boot_id=runtime.boot_id,
        execution_id=runtime.execution_id,
        infrastructure_attempt_id=runtime.infrastructure_attempt_id,
        runtime_identity_sha256=runtime.runtime_identity_sha256,
        runtime_inspection_receipt=inspection,
        runtime_inspection_receipt_sha256=inspection.inspection_receipt_sha256,
        adoption_sequence=adoption_sequence,
        previous_fencing_epoch=inspection.fencing_epoch,
        new_fencing_epoch=new_fencing_epoch,
        previous_lease_token_sha256=inspection.lease_token_sha256,
        new_lease_token_sha256=new_lease_token_sha256,
        reason=reason,
        singleton_lock_evidence_sha256=singleton_lock_evidence_sha256,
        singleton_lock_acquired_monotonic_ns=singleton_lock_acquired_monotonic_ns,
        allocator_principal_id=allocator_principal_id,
        adopted_at=adopted_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return AttemptAdoptionReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_attempt_adoption(
    *,
    receipt: AttemptAdoptionReceipt,
    authority: WorkerNodeAuthorityVerifier,
    expected_runtime_identity: NodeRuntimeIdentity,
    expected_previous_fencing_epoch: int,
    expected_previous_lease_token_sha256: str,
    expected_new_fencing_epoch: int,
    expected_new_lease_token_sha256: str,
    expected_allocator_principal_id: str,
    maximum_inspection_age_seconds: int,
    observed_at: datetime,
) -> VerifiedAttemptAdoption:
    """Verify exact DB-locked old/new authority and node-local singleton adoption proof."""

    _require_utc_timestamp(observed_at, "attempt adoption verifier observed_at")
    try:
        receipt = AttemptAdoptionReceipt.model_validate(receipt.model_dump(mode="python"))
        verify_runtime_for_adoption(
            receipt=receipt.runtime_inspection_receipt,
            authority=authority,
            expected_runtime_identity=expected_runtime_identity,
            expected_fencing_epoch=expected_previous_fencing_epoch,
            expected_lease_token_sha256=expected_previous_lease_token_sha256,
            maximum_inspection_age_seconds=maximum_inspection_age_seconds,
            observed_at=observed_at,
        )
        if (
            receipt.node_manifest_sha256 != authority.manifest.manifest_sha256
            or receipt.runtime_identity_sha256 != expected_runtime_identity.runtime_identity_sha256
            or receipt.previous_fencing_epoch != expected_previous_fencing_epoch
            or receipt.previous_lease_token_sha256 != expected_previous_lease_token_sha256
            or receipt.new_fencing_epoch != expected_new_fencing_epoch
            or receipt.new_lease_token_sha256 != expected_new_lease_token_sha256
            or receipt.allocator_principal_id != expected_allocator_principal_id
            or receipt.adopted_at > observed_at
        ):
            raise QualificationVerificationError(
                "attempt adoption differs from the exact DB-locked authority transition"
            )
        authority.verify_signature(
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.adopted_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError("attempt adoption failed closed revalidation") from exc
    return VerifiedAttemptAdoption(
        adoption_receipt_sha256=receipt.adoption_receipt_sha256,
        runtime_inspection_receipt_sha256=receipt.runtime_inspection_receipt_sha256,
        runtime_identity_sha256=receipt.runtime_identity_sha256,
        execution_id=receipt.execution_id,
        infrastructure_attempt_id=receipt.infrastructure_attempt_id,
        previous_fencing_epoch=receipt.previous_fencing_epoch,
        new_fencing_epoch=receipt.new_fencing_epoch,
        previous_lease_token_sha256=receipt.previous_lease_token_sha256,
        new_lease_token_sha256=receipt.new_lease_token_sha256,
        singleton_lock_evidence_sha256=receipt.singleton_lock_evidence_sha256,
        verified_at=observed_at,
    )


def artifact_output_tree_sha256(manifest: ArtifactManifest) -> str:
    """Hash only canonical output identity/content fields, excluding custody locations."""

    manifest = ArtifactManifest.model_validate(manifest.model_dump(mode="python"))
    return canonical_sha256(
        {
            "schema_name": "aletheia.artifact_output_tree",
            "schema_version": RUNTIME_CONTRACT_SCHEMA_VERSION,
            "entries": tuple(
                {
                    "artifact_key": entry.artifact_key,
                    "content_sha256": entry.content_sha256,
                    "bytes": entry.bytes,
                    "media_type": entry.media_type,
                    "schema_sha256": entry.schema_sha256,
                }
                for entry in manifest.entries
            ),
        }
    )


class NodeExecutionReceipt(ExecutionModel):
    """Node-signed process/output receipt; central ExecutionReceipt remains a separate boundary."""

    schema_name: Literal["aletheia.node_execution_receipt"] = "aletheia.node_execution_receipt"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity: NodeRuntimeIdentity
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    started_monotonic_ns: int = Field(ge=0)
    ended_at: AwareDatetime
    ended_monotonic_ns: int = Field(ge=0)
    exit_code: int = Field(ge=-255, le=255)
    confirmed_terminated: Literal[True] = True
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_inspection_receipt: RuntimeInspectionReceipt
    termination_inspection_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _execution_receipt_is_exact_and_terminated(self) -> "NodeExecutionReceipt":
        runtime = self.runtime_identity
        termination = self.termination_inspection_receipt
        if (
            self.runtime_identity_sha256 != runtime.runtime_identity_sha256
            or self.node_manifest_sha256 != termination.node_manifest_sha256
            or self.execution_id != runtime.execution_id
            or self.infrastructure_attempt_id != runtime.infrastructure_attempt_id
            or self.started_at != runtime.started_at
            or self.started_monotonic_ns != runtime.started_monotonic_ns
            or self.termination_inspection_receipt_sha256 != termination.inspection_receipt_sha256
            or termination.runtime_identity != runtime
            or termination.fencing_epoch != self.fencing_epoch
            or termination.lease_token_sha256 != self.lease_token_sha256
            or termination.state
            not in {RuntimeInspectionState.TERMINATED, RuntimeInspectionState.ABSENT}
        ):
            raise ValueError("node execution receipt changed its exact runtime authority")
        if not (
            self.started_at <= self.ended_at <= termination.inspected_at <= self.signed_at
            and self.started_monotonic_ns
            <= self.ended_monotonic_ns
            <= termination.inspected_monotonic_ns
            and self.signed_at < termination.expires_at
        ):
            raise ValueError("node execution termination times are out of order")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def node_execution_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedNodeExecution(ExecutionModel):
    node_execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    exit_code: int = Field(ge=-255, le=255)
    verified_at: AwareDatetime
    confirmed_terminated: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def issue_node_execution_receipt(
    *,
    manifest: WorkerNodeManifest,
    intent: ExecutionIntent,
    node_inventory_sha256: str,
    resource_lease_sha256: str,
    runtime_identity: NodeRuntimeIdentity,
    fencing_epoch: int,
    lease_token_sha256: str,
    ended_at: datetime,
    ended_monotonic_ns: int,
    exit_code: int,
    artifact_manifest: ArtifactManifest,
    termination_inspection_receipt: RuntimeInspectionReceipt,
    signed_at: datetime,
    private_key: bytes,
) -> NodeExecutionReceipt:
    """Node-side helper that signs exact exit, termination, and output-tree material."""

    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    runtime = NodeRuntimeIdentity.model_validate(runtime_identity.model_dump(mode="python"))
    termination = RuntimeInspectionReceipt.model_validate(
        termination_inspection_receipt.model_dump(mode="python")
    )
    artifact_manifest = ArtifactManifest.model_validate(artifact_manifest.model_dump(mode="python"))
    if (
        _public_key_bytes(private_key).hex() != manifest.node_signing_public_key_ed25519_hex
        or runtime.node_id != manifest.node_id
        or runtime.execution_id != intent.execution_id
        or runtime.infrastructure_attempt_id
        != intent.infrastructure_attempt.infrastructure_attempt_id
        or artifact_manifest.intent_sha256 != intent.intent_sha256
        or artifact_manifest.execution_id != intent.execution_id
        or artifact_manifest.replicate_slot_id != intent.replicate_slot.replicate_slot_id
        or artifact_manifest.infrastructure_attempt_id
        != intent.infrastructure_attempt.infrastructure_attempt_id
        or termination.node_manifest_sha256 != manifest.manifest_sha256
        or termination.runtime_identity != runtime
        or not runtime.started_at <= artifact_manifest.produced_at <= ended_at
    ):
        raise QualificationVerificationError(
            "node execution signer, runtime, intent, or artifact manifest scope diverges"
        )
    _verify_worker_node_signature_against_manifest(
        manifest=manifest,
        signing_key_id=termination.signing_key_id,
        message=termination.signature_message,
        signature_ed25519_hex=termination.signature_ed25519_hex,
        signed_at=termination.inspected_at,
    )
    unsigned = NodeExecutionReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        node_inventory_sha256=node_inventory_sha256,
        resource_lease_sha256=resource_lease_sha256,
        runtime_identity=runtime,
        runtime_identity_sha256=runtime.runtime_identity_sha256,
        fencing_epoch=fencing_epoch,
        lease_token_sha256=lease_token_sha256,
        started_at=runtime.started_at,
        started_monotonic_ns=runtime.started_monotonic_ns,
        ended_at=ended_at,
        ended_monotonic_ns=ended_monotonic_ns,
        exit_code=exit_code,
        artifact_manifest_sha256=artifact_manifest.manifest_sha256,
        output_tree_sha256=artifact_output_tree_sha256(artifact_manifest),
        termination_inspection_receipt=termination,
        termination_inspection_receipt_sha256=termination.inspection_receipt_sha256,
        signed_at=signed_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return NodeExecutionReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_node_execution_receipt(
    *,
    receipt: NodeExecutionReceipt,
    authority: WorkerNodeAuthorityVerifier,
    expected_intent: ExecutionIntent,
    expected_runtime_identity: NodeRuntimeIdentity,
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    expected_artifact_manifest: ArtifactManifest,
    expected_fencing_epoch: int,
    expected_lease_token_sha256: str,
    maximum_inspection_age_seconds: int,
    observed_at: datetime,
) -> VerifiedNodeExecution:
    """Verify exact runtime exit only while fresh terminated/absent node proof is valid."""

    _require_utc_timestamp(observed_at, "node execution verifier observed_at")
    try:
        receipt = NodeExecutionReceipt.model_validate(receipt.model_dump(mode="python"))
        expected_intent = ExecutionIntent.model_validate(expected_intent.model_dump(mode="python"))
        expected_artifact_manifest = ArtifactManifest.model_validate(
            expected_artifact_manifest.model_dump(mode="python")
        )
        verify_runtime_for_release_or_retry(
            receipt=receipt.termination_inspection_receipt,
            authority=authority,
            expected_runtime_identity=expected_runtime_identity,
            expected_fencing_epoch=expected_fencing_epoch,
            expected_lease_token_sha256=expected_lease_token_sha256,
            maximum_inspection_age_seconds=maximum_inspection_age_seconds,
            observed_at=observed_at,
        )
        if (
            receipt.node_manifest_sha256 != authority.manifest.manifest_sha256
            or receipt.intent_sha256 != expected_intent.intent_sha256
            or receipt.execution_id != expected_intent.execution_id
            or receipt.infrastructure_attempt_id
            != expected_intent.infrastructure_attempt.infrastructure_attempt_id
            or receipt.runtime_identity != expected_runtime_identity
            or receipt.runtime_identity.node_id != authority.manifest.node_id
            or receipt.node_inventory_sha256 != expected_node_inventory_sha256
            or receipt.resource_lease_sha256 != expected_resource_lease_sha256
            or receipt.artifact_manifest_sha256 != expected_artifact_manifest.manifest_sha256
            or receipt.output_tree_sha256 != artifact_output_tree_sha256(expected_artifact_manifest)
            or expected_artifact_manifest.intent_sha256 != expected_intent.intent_sha256
            or expected_artifact_manifest.execution_id != expected_intent.execution_id
            or expected_artifact_manifest.replicate_slot_id
            != expected_intent.replicate_slot.replicate_slot_id
            or expected_artifact_manifest.infrastructure_attempt_id
            != expected_intent.infrastructure_attempt.infrastructure_attempt_id
            or receipt.fencing_epoch != expected_fencing_epoch
            or receipt.lease_token_sha256 != expected_lease_token_sha256
            or not receipt.signed_at <= observed_at
        ):
            raise QualificationVerificationError(
                "node execution receipt differs from exact allocator authority"
            )
        authority.verify_signature(
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.signed_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "node execution receipt failed closed revalidation"
        ) from exc
    return VerifiedNodeExecution(
        node_execution_receipt_sha256=receipt.node_execution_receipt_sha256,
        intent_sha256=receipt.intent_sha256,
        execution_id=receipt.execution_id,
        infrastructure_attempt_id=receipt.infrastructure_attempt_id,
        runtime_identity_sha256=receipt.runtime_identity_sha256,
        node_inventory_sha256=receipt.node_inventory_sha256,
        resource_lease_sha256=receipt.resource_lease_sha256,
        fencing_epoch=receipt.fencing_epoch,
        artifact_manifest_sha256=receipt.artifact_manifest_sha256,
        output_tree_sha256=receipt.output_tree_sha256,
        exit_code=receipt.exit_code,
        verified_at=observed_at,
    )


class TerminalVerificationAuthorityPin(ExecutionModel):
    """Deployment-owned key for the trusted central terminal composer."""

    schema_name: Literal["aletheia.terminal_verification_authority_pin"] = (
        "aletheia.terminal_verification_authority_pin"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_valid(self) -> "TerminalVerificationAuthorityPin":
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("terminal-verification key id does not match its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("terminal-verification key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError(
                "terminal-verification key revocation must fall inside its validity window"
            )
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class TerminalVerificationAttestationMessage(ExecutionModel):
    """Canonical signed decision over one exact node and central terminal receipt pair."""

    schema_name: Literal["aletheia.terminal_verification_attestation_message"] = (
        "aletheia.terminal_verification_attestation_message"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    algorithm: Literal["ed25519-canonical-json-v1"] = "ed25519-canonical-json-v1"
    execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    worker_node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...]
    terminal_state: ExecutionTerminalState
    failure_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    terminal_verification_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verification_key_id: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _message_is_canonical(self) -> "TerminalVerificationAttestationMessage":
        _canonical_strings(
            self.artifact_verified_receipt_sha256s,
            "terminal artifact verification receipt hashes",
        )
        if any(
            re.fullmatch(_SHA256_PATTERN, item) is None
            for item in self.artifact_verified_receipt_sha256s
        ):
            raise ValueError("terminal artifact verification receipts must be SHA-256 values")
        if self.expires_at <= self.verified_at:
            raise ValueError("terminal-verification expiry must follow verification time")
        return self

    @property
    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def message_sha256(self) -> str:
        return hashlib.sha256(self.message_bytes).hexdigest()


class TerminalVerificationAttestation(ExecutionModel):
    """Signed authority for committing one exact central engineering disposition."""

    schema_name: Literal["aletheia.terminal_verification_attestation"] = (
        "aletheia.terminal_verification_attestation"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    message: TerminalVerificationAttestationMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def attestation_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedTerminalVerification(ExecutionModel):
    """Exact signed terminal authority verified at the allocator DB observation time."""

    terminal_verification_attestation_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    terminal_state: ExecutionTerminalState
    failure_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def _terminal_verification_message(
    *,
    execution_receipt: ExecutionReceipt,
    node_execution_receipt: NodeExecutionReceipt,
    pin: TerminalVerificationAuthorityPin,
    expires_at: datetime,
) -> TerminalVerificationAttestationMessage:
    manifest = execution_receipt.artifact_manifest
    if manifest is None:
        raise QualificationVerificationError(
            "terminal verification requires the exact collected artifact manifest"
        )
    receipt_hashes = tuple(
        sorted(
            item.verified_receipt_sha256 for item in execution_receipt.artifact_verified_receipts
        )
    )
    return TerminalVerificationAttestationMessage(
        execution_receipt_sha256=execution_receipt.execution_receipt_sha256,
        node_execution_receipt_sha256=(node_execution_receipt.node_execution_receipt_sha256),
        intent_sha256=execution_receipt.intent.intent_sha256,
        execution_id=execution_receipt.intent.execution_id,
        infrastructure_attempt_id=(
            execution_receipt.intent.infrastructure_attempt.infrastructure_attempt_id
        ),
        worker_node_manifest_sha256=execution_receipt.worker_node_manifest_sha256,
        node_inventory_sha256=execution_receipt.node_inventory_sha256,
        resource_lease_sha256=execution_receipt.resource_lease_sha256,
        artifact_manifest_sha256=manifest.manifest_sha256,
        artifact_verified_receipt_sha256s=receipt_hashes,
        terminal_state=execution_receipt.terminal_state,
        failure_sha256=(
            canonical_sha256(execution_receipt.failure)
            if execution_receipt.failure is not None
            else None
        ),
        terminal_verification_policy_sha256=pin.policy_sha256,
        verified_by_principal_id=pin.principal_id,
        verification_key_id=pin.key_id,
        verified_at=execution_receipt.verified_at,
        expires_at=expires_at,
    )


def _assert_terminal_receipts_match(
    *,
    execution_receipt: ExecutionReceipt,
    node_execution_receipt: NodeExecutionReceipt,
    pin: TerminalVerificationAuthorityPin,
) -> None:
    manifest = execution_receipt.artifact_manifest
    if (
        execution_receipt.verified_by_principal_id != pin.principal_id
        or execution_receipt.node_execution_receipt_sha256
        != node_execution_receipt.node_execution_receipt_sha256
        or execution_receipt.intent.intent_sha256 != node_execution_receipt.intent_sha256
        or execution_receipt.intent.execution_id != node_execution_receipt.execution_id
        or execution_receipt.intent.infrastructure_attempt.infrastructure_attempt_id
        != node_execution_receipt.infrastructure_attempt_id
        or execution_receipt.worker_node_manifest_sha256
        != node_execution_receipt.node_manifest_sha256
        or execution_receipt.node_inventory_sha256 != node_execution_receipt.node_inventory_sha256
        or execution_receipt.resource_lease_sha256 != node_execution_receipt.resource_lease_sha256
        or execution_receipt.started_at != node_execution_receipt.started_at
        or execution_receipt.ended_at != node_execution_receipt.ended_at
        or execution_receipt.observed_at < node_execution_receipt.signed_at
        or manifest is None
        or manifest.manifest_sha256 != node_execution_receipt.artifact_manifest_sha256
    ):
        raise QualificationVerificationError(
            "central terminal disposition differs from its exact node receipt or verifier pin"
        )


def issue_terminal_verification_attestation(
    *,
    execution_receipt: ExecutionReceipt,
    node_execution_receipt: NodeExecutionReceipt,
    pin: TerminalVerificationAuthorityPin,
    private_key: bytes,
    expires_at: datetime,
) -> TerminalVerificationAttestation:
    """Sign a trusted central disposition after exact node/artifact verification."""

    try:
        execution_receipt = ExecutionReceipt.model_validate(
            execution_receipt.model_dump(mode="python")
        )
        node_execution_receipt = NodeExecutionReceipt.model_validate(
            node_execution_receipt.model_dump(mode="python")
        )
        pin = TerminalVerificationAuthorityPin.model_validate(pin.model_dump(mode="python"))
        _require_utc_timestamp(execution_receipt.verified_at, "terminal verified_at")
        _require_utc_timestamp(expires_at, "terminal attestation expires_at")
        if _public_key_bytes(private_key).hex() != pin.public_key_ed25519_hex:
            raise QualificationVerificationError(
                "terminal-verification private key differs from the deployment pin"
            )
        if not pin.active_at(execution_receipt.verified_at) or expires_at > pin.active_until:
            raise QualificationVerificationError(
                "terminal-verification attestation exceeds its pinned authority window"
            )
        _assert_terminal_receipts_match(
            execution_receipt=execution_receipt,
            node_execution_receipt=node_execution_receipt,
            pin=pin,
        )
        message = _terminal_verification_message(
            execution_receipt=execution_receipt,
            node_execution_receipt=node_execution_receipt,
            pin=pin,
            expires_at=expires_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "terminal-verification signer failed closed revalidation"
        ) from exc
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message.message_bytes)
    return TerminalVerificationAttestation(
        message=message,
        signature_ed25519_hex=signature.hex(),
    )


class TerminalVerificationAuthorityVerifier:
    """Verify a terminal disposition only against one deployment-owned authority pin."""

    def __init__(self, pin: TerminalVerificationAuthorityPin) -> None:
        self._pin = TerminalVerificationAuthorityPin.model_validate(pin.model_dump(mode="python"))

    @property
    def pin(self) -> TerminalVerificationAuthorityPin:
        return self._pin

    def verify(
        self,
        *,
        attestation: TerminalVerificationAttestation,
        execution_receipt: ExecutionReceipt,
        node_execution_receipt: NodeExecutionReceipt,
        observed_at: datetime,
    ) -> VerifiedTerminalVerification:
        _require_utc_timestamp(observed_at, "terminal-verification observed_at")
        try:
            attestation = TerminalVerificationAttestation.model_validate(
                attestation.model_dump(mode="python")
            )
            execution_receipt = ExecutionReceipt.model_validate(
                execution_receipt.model_dump(mode="python")
            )
            node_execution_receipt = NodeExecutionReceipt.model_validate(
                node_execution_receipt.model_dump(mode="python")
            )
            _assert_terminal_receipts_match(
                execution_receipt=execution_receipt,
                node_execution_receipt=node_execution_receipt,
                pin=self._pin,
            )
            expected = _terminal_verification_message(
                execution_receipt=execution_receipt,
                node_execution_receipt=node_execution_receipt,
                pin=self._pin,
                expires_at=attestation.message.expires_at,
            )
            if attestation.message != expected:
                raise QualificationVerificationError(
                    "terminal-verification attestation changed its exact disposition"
                )
            if not (
                expected.verified_at <= observed_at < expected.expires_at
                and self._pin.active_at(expected.verified_at)
                and self._pin.active_at(observed_at)
                and expected.expires_at <= self._pin.active_until
            ):
                raise QualificationVerificationError(
                    "terminal-verification attestation or pinned key is inactive"
                )
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(self._pin.public_key_ed25519_hex)
            ).verify(
                bytes.fromhex(attestation.signature_ed25519_hex),
                expected.message_bytes,
            )
        except QualificationVerificationError:
            raise
        except InvalidSignature as exc:
            raise QualificationVerificationError(
                "terminal-verification attestation signature is invalid"
            ) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise QualificationVerificationError(
                "terminal-verification attestation failed closed revalidation"
            ) from exc
        return VerifiedTerminalVerification(
            terminal_verification_attestation_sha256=attestation.attestation_sha256,
            execution_receipt_sha256=expected.execution_receipt_sha256,
            node_execution_receipt_sha256=expected.node_execution_receipt_sha256,
            intent_sha256=expected.intent_sha256,
            execution_id=expected.execution_id,
            infrastructure_attempt_id=expected.infrastructure_attempt_id,
            terminal_state=expected.terminal_state,
            failure_sha256=expected.failure_sha256,
            verified_by_principal_id=expected.verified_by_principal_id,
            verified_at=observed_at,
        )


class ExecutionCostQuote(ExecutionModel):
    """Immutable maximum charge for one exact infrastructure attempt."""

    schema_name: Literal["aletheia.execution_cost_quote"] = "aletheia.execution_cost_quote"
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    accepted_resource_class_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    permitted_node_manifest_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    selected_node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_resource_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    rate_card_sha256: str = Field(pattern=_SHA256_PATTERN)
    fixed_charge_microunits: int = Field(ge=0)
    charge_per_second_microunits: int = Field(ge=0)
    maximum_lease_seconds: int = Field(ge=1)
    maximum_charge_microunits: int = Field(ge=0)
    pricing_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    quoted_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    quoted_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _quote_is_canonical(self) -> "ExecutionCostQuote":
        _canonical_strings(
            self.accepted_resource_class_ids,
            "quoted resource classes",
            required=True,
        )
        _canonical_strings(
            self.permitted_node_manifest_sha256s,
            "quoted node manifests",
            required=True,
        )
        _canonical_strings(
            self.selected_resource_ids,
            "quoted selected resource ids",
            required=True,
        )
        if any(
            re.fullmatch(_SHA256_PATTERN, item) is None
            for item in self.permitted_node_manifest_sha256s
        ):
            raise ValueError("quoted node manifests must be SHA-256 identities")
        if self.selected_node_manifest_sha256 not in self.permitted_node_manifest_sha256s:
            raise ValueError("selected node manifest is outside the quote placement envelope")
        expected_charge = self.fixed_charge_microunits + (
            self.charge_per_second_microunits * self.maximum_lease_seconds
        )
        if self.maximum_charge_microunits != expected_charge:
            raise ValueError("maximum charge does not reconcile to rate card and lease duration")
        if self.expires_at <= self.quoted_at:
            raise ValueError("cost quote expiry must follow quote time")
        return self

    @property
    def quote_sha256(self) -> str:
        return canonical_sha256(self)


class BudgetAuthorization(ExecutionModel):
    """Qualification-only budget cap copied from one frozen protocol budget contract."""

    schema_name: Literal["aletheia.execution_budget_authorization"] = (
        "aletheia.execution_budget_authorization"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_budget_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    maximum_cost_microunits: int = Field(ge=0)
    deadline: AwareDatetime
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _budget_window_is_valid(self) -> "BudgetAuthorization":
        if not self.authorized_at < self.expires_at <= self.deadline:
            raise ValueError("budget authorization must expire inside its execution deadline")
        return self

    @property
    def authorization_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedBudgetAuthorizationResolution(ExecutionModel):
    """Trusted source-byte/signature verification plus its exact qualification projection."""

    schema_name: Literal["aletheia.verified_budget_authorization_resolution"] = (
        "aletheia.verified_budget_authorization_resolution"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    source_budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authorization_canonical_bytes_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authorization_signature_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authorization_signature_verified: Literal[True] = True
    budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    budget_authorization: BudgetAuthorization
    resolved_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    resolved_at: AwareDatetime
    custody_reverified: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _resolution_exactly_projects_registered_source(
        self,
    ) -> "VerifiedBudgetAuthorizationResolution":
        authorization = self.budget_authorization
        if (
            self.source_budget_authorization_sha256
            != self.source_authorization_canonical_bytes_sha256
            or self.source_budget_authorization_sha256
            != authorization.source_budget_authorization_sha256
            or self.budget_authorization_sha256 != authorization.authorization_sha256
            or self.resolved_at < authorization.authorized_at
        ):
            raise ValueError(
                "budget resolution differs from its registered source bytes or projection"
            )
        return self


class VerifiedExecutionReceiptResolution(ExecutionModel):
    """Exact terminal-row custody with its PostgreSQL commit linearization time."""

    schema_name: Literal["aletheia.verified_execution_receipt_resolution"] = (
        "aletheia.verified_execution_receipt_resolution"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_receipt: ExecutionReceipt
    committed_at: AwareDatetime
    resolved_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    resolved_at: AwareDatetime
    custody_reverified: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _resolution_is_exact_and_committed(self) -> "VerifiedExecutionReceiptResolution":
        if (
            self.execution_receipt_sha256 != self.execution_receipt.execution_receipt_sha256
            or self.execution_receipt.verified_at > self.committed_at
            or self.committed_at > self.resolved_at
        ):
            raise ValueError("execution receipt resolution differs from committed terminal custody")
        return self


class VerifiedInputArtifactResolution(ExecutionModel):
    """Fresh CAS/custody resolution, with successful producer lineage when applicable."""

    schema_name: Literal["aletheia.verified_input_artifact_resolution"] = (
        "aletheia.verified_input_artifact_resolution"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_receipt: ArtifactVerifiedReceipt
    artifact_manifest: ArtifactManifest
    producer_execution_receipt: ExecutionReceipt | None = None
    content_rehash_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_bytes: int = Field(ge=0)
    custody_reverified: Literal[True] = True
    resolved_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    resolved_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _resolution_closes_manifest_cas_and_optional_lineage(
        self,
    ) -> "VerifiedInputArtifactResolution":
        receipt = self.verified_receipt
        manifest = self.artifact_manifest
        if self.verified_receipt_sha256 != receipt.verified_receipt_sha256:
            raise ValueError("input resolution receipt identity differs from its exact bytes")
        if receipt.artifact_manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("input resolution receipt belongs to another artifact manifest")
        entries = tuple(item for item in manifest.entries if item == receipt.artifact)
        if len(entries) != 1:
            raise ValueError("input resolution artifact is absent from its immutable manifest")
        if receipt.producer_attempt_id != manifest.infrastructure_attempt_id:
            raise ValueError("input resolution manifest and producer attempt diverge")
        if (
            self.content_rehash_sha256 != receipt.artifact.content_sha256
            or self.content_bytes != receipt.artifact.bytes
        ):
            raise ValueError("fresh CAS rehash differs from the verified artifact entry")
        if self.producer_execution_receipt is not None:
            producer = self.producer_execution_receipt
            if (
                producer.terminal_state is not ExecutionTerminalState.ENGINEERING_SUCCEEDED
                or producer.artifact_manifest != manifest
                or receipt not in producer.artifact_verified_receipts
                or producer.intent.infrastructure_attempt.infrastructure_attempt_id
                != receipt.producer_attempt_id
            ):
                raise ValueError(
                    "input resolution does not contain exact successful producer receipt lineage"
                )
        return self


class EngineeringQualificationBundle(ExecutionModel):
    """Full frozen material whose hashes are signed by a qualification grant."""

    schema_name: Literal["aletheia.engineering_qualification_bundle"] = (
        "aletheia.engineering_qualification_bundle"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    compilation_request: ProtocolCompilationRequest
    compilation_result: ProtocolCompilationResult
    work_order: WorkOrderDAG
    intent: ExecutionIntent
    prior_execution_receipt: ExecutionReceipt | None = None
    input_artifact_verified_receipt_sha256s: tuple[str, ...]
    budget_authorization: BudgetAuthorization
    cost_quote: ExecutionCostQuote
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _bundle_is_closed(self) -> "EngineeringQualificationBundle":
        _canonical_strings(
            self.input_artifact_verified_receipt_sha256s,
            "qualification input artifact receipts",
        )
        if any(
            re.fullmatch(_SHA256_PATTERN, item) is None
            for item in self.input_artifact_verified_receipt_sha256s
        ):
            raise ValueError("qualification input receipts must be SHA-256 identities")
        bound_receipts = tuple(
            sorted(
                {
                    item.artifact_verified_receipt_sha256
                    for item in self.intent.input_artifact_bindings
                }
            )
        )
        if self.input_artifact_verified_receipt_sha256s != bound_receipts:
            raise ValueError("qualification bundle must name every exact intent input receipt")
        if self.compilation_result.work_order != self.work_order:
            raise ValueError("qualification WorkOrder differs from its compilation result")
        if self.compilation_result.receipt.work_order_sha256 != self.work_order.work_order_sha256:
            raise ValueError("qualification compilation receipt names another WorkOrder")
        if self.intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE:
            raise ValueError("engineering qualification permits only replay-safe execution")
        if self.intent.external_action_kind is not None or self.intent.external_request is not None:
            raise ValueError("engineering qualification cannot invoke an external adapter")
        if self.intent.resource_request.network_policy is not NetworkPolicy.NONE:
            raise ValueError("engineering qualification requires network-none execution")
        if self.intent.retry_policy.mode not in {
            ExecutionRetryMode.NEVER,
            ExecutionRetryMode.IDEMPOTENT_NEW_ATTEMPT,
        }:
            raise ValueError("engineering qualification does not authorize checkpoint/reconcile")
        attempt = self.intent.infrastructure_attempt
        if attempt.attempt_number == 1:
            if self.prior_execution_receipt is not None:
                raise ValueError("initial infrastructure attempt cannot carry a prior receipt")
        else:
            prior = self.prior_execution_receipt
            if (
                prior is None
                or attempt.previous_attempt_id
                != prior.intent.infrastructure_attempt.infrastructure_attempt_id
                or attempt.prior_confirmed_failure_receipt_sha256 != prior.execution_receipt_sha256
            ):
                raise ValueError("retry qualification must bind the exact prior ExecutionReceipt")
        protocol = self.compilation_request.protocol
        budget = protocol.resource_budget
        authorization = self.budget_authorization
        quote = self.cost_quote
        common_scope = (
            self.work_order.quest_id
            == self.intent.quest_id
            == authorization.quest_id
            == quote.quest_id,
            protocol.protocol_sha256
            == self.work_order.protocol_sha256
            == self.intent.protocol_sha256
            == authorization.protocol_sha256
            == quote.protocol_sha256,
            self.work_order.work_order_sha256
            == self.intent.work_order_sha256
            == authorization.work_order_sha256
            == quote.work_order_sha256,
            self.intent.intent_sha256 == quote.intent_sha256,
            self.intent.execution_id == quote.execution_id,
            self.intent.infrastructure_attempt.infrastructure_attempt_id
            == quote.infrastructure_attempt_id,
            self.work_order.resource_budget_sha256
            == budget.resource_budget_sha256
            == authorization.resource_budget_sha256,
            budget.budget_authorization_sha256 == authorization.source_budget_authorization_sha256,
            budget.currency_code == authorization.currency_code == quote.currency_code,
            budget.maximum_cost_microunits == authorization.maximum_cost_microunits,
            budget.deadline == authorization.deadline,
            self.intent.resource_request.accepted_resource_class_ids
            == quote.accepted_resource_class_ids,
        )
        if not all(common_scope):
            raise ValueError("qualification budget, quote, compilation, and intent scope diverge")
        if quote.maximum_charge_microunits > authorization.maximum_cost_microunits:
            raise ValueError("execution cost quote exceeds its exact budget authorization")
        if quote.maximum_lease_seconds != self.intent.resource_request.wall_time_seconds:
            raise ValueError("quoted lease duration differs from the frozen execution request")
        if self.intent.deadline > authorization.deadline:
            raise ValueError("execution intent exceeds its frozen protocol budget deadline")
        if not (
            authorization.authorized_at
            <= self.intent.authorized_at
            <= quote.quoted_at
            < quote.expires_at
            <= authorization.expires_at
            <= authorization.deadline
        ):
            raise ValueError("qualification budget, quote, and intent times are out of order")
        lease_window_seconds = int(
            (
                min(
                    self.intent.deadline,
                    quote.expires_at,
                    authorization.expires_at,
                )
                - self.intent.authorized_at
            ).total_seconds()
        )
        if quote.maximum_lease_seconds > lease_window_seconds:
            raise ValueError("quoted lease can outlive the qualification authorization window")
        return self

    @property
    def bundle_sha256(self) -> str:
        return canonical_sha256(self)


class EngineeringQualificationGrantMessage(ExecutionModel):
    """Canonical deployment-signature message for exactly one qualification bundle."""

    schema_name: Literal["aletheia.engineering_qualification_grant_message"] = (
        "aletheia.engineering_qualification_grant_message"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    algorithm: Literal["ed25519-canonical-json-v1"] = "ed25519-canonical-json-v1"
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    graph_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    replicate_slot_id: str = Field(pattern=_REPLICATE_SLOT_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    input_artifact_verified_receipt_sha256s: tuple[str, ...]
    prior_execution_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    cost_quote_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _grant_window_and_inputs_are_canonical(self) -> "EngineeringQualificationGrantMessage":
        _canonical_strings(
            self.input_artifact_verified_receipt_sha256s,
            "grant input artifact receipts",
        )
        if self.expires_at <= self.authorized_at:
            raise ValueError("qualification grant expiry must follow authorization")
        return self

    @property
    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def message_sha256(self) -> str:
        return hashlib.sha256(self.message_bytes).hexdigest()


class EngineeringQualificationGrant(ExecutionModel):
    """Signed engineering authority that can never authorize scientific admission."""

    schema_name: Literal["aletheia.engineering_qualification_grant"] = (
        "aletheia.engineering_qualification_grant"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    message: EngineeringQualificationGrantMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def grant_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationAuthorityPin(ExecutionModel):
    """Deployment-owned trust input; a grant cannot select or replace this key."""

    schema_name: Literal["aletheia.qualification_authority_pin"] = (
        "aletheia.qualification_authority_pin"
    )
    schema_version: Literal[1] = RUNTIME_CONTRACT_SCHEMA_VERSION
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_valid(self) -> "QualificationAuthorityPin":
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("qualification key id does not match its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("qualification key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("qualification key revocation must fall inside its validity window")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class QualificationVerificationError(ValueError):
    """A qualification grant, frozen bundle, or archived input failed closed."""


def _resolve_registered_execution_authority(
    *,
    bundle: EngineeringQualificationBundle,
    resolver: ExecutionAuthorityResolverPort,
    observed_at: datetime,
) -> VerifiedExecutionReceiptResolution | None:
    """Require registry/archive custody for quote, source budget, and retry receipt."""

    try:
        quote_candidate = resolver.resolve_execution_cost_quote(
            cost_quote_sha256=bundle.cost_quote.quote_sha256,
            observed_at=observed_at,
        )
        if quote_candidate is None:
            raise QualificationVerificationError(
                "qualification cost quote is absent from registered custody"
            )
        registered_quote = ExecutionCostQuote.model_validate(
            quote_candidate.model_dump(mode="python")
        )
        if (
            registered_quote.quote_sha256 != bundle.cost_quote.quote_sha256
            or registered_quote != bundle.cost_quote
        ):
            raise QualificationVerificationError(
                "qualification cost quote differs from registered canonical bytes"
            )

        budget_candidate = resolver.resolve_budget_authorization(
            source_budget_authorization_sha256=(
                bundle.budget_authorization.source_budget_authorization_sha256
            ),
            observed_at=observed_at,
        )
        if budget_candidate is None:
            raise QualificationVerificationError(
                "qualification source budget authorization is absent from registered custody"
            )
        budget_resolution = VerifiedBudgetAuthorizationResolution.model_validate(
            budget_candidate.model_dump(mode="python")
        )
        if (
            budget_resolution.resolved_at != observed_at
            or budget_resolution.budget_authorization != bundle.budget_authorization
            or budget_resolution.budget_authorization_sha256
            != bundle.budget_authorization.authorization_sha256
        ):
            raise QualificationVerificationError(
                "qualification budget projection differs from verified source authority"
            )

        if bundle.prior_execution_receipt is not None:
            prior_candidate = resolver.resolve_execution_receipt(
                execution_receipt_sha256=(bundle.prior_execution_receipt.execution_receipt_sha256),
                observed_at=observed_at,
            )
            if prior_candidate is None:
                raise QualificationVerificationError(
                    "retry prior ExecutionReceipt is absent from registered custody"
                )
            prior_resolution = VerifiedExecutionReceiptResolution.model_validate(
                prior_candidate.model_dump(mode="python")
            )
            if (
                prior_resolution.resolved_at != observed_at
                or prior_resolution.execution_receipt_sha256
                != bundle.prior_execution_receipt.execution_receipt_sha256
                or prior_resolution.execution_receipt != bundle.prior_execution_receipt
            ):
                raise QualificationVerificationError(
                    "retry prior ExecutionReceipt differs from registered canonical bytes"
                )
            return prior_resolution
        return None
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "registered execution authority resolver returned invalid bytes"
        ) from exc


class QualificationAuthorityVerifier:
    """Verify grants only against an independently supplied deployment pin."""

    def __init__(self, pin: QualificationAuthorityPin) -> None:
        self._pin = QualificationAuthorityPin.model_validate(pin.model_dump(mode="python"))

    @property
    def pin(self) -> QualificationAuthorityPin:
        return self._pin

    def verify_signature(
        self,
        grant: EngineeringQualificationGrant,
        *,
        observed_at: datetime,
    ) -> None:
        _require_utc_timestamp(observed_at, "qualification verifier observed_at")
        grant = EngineeringQualificationGrant.model_validate(grant.model_dump(mode="python"))
        message = grant.message
        pin = self._pin
        if (
            message.qualification_authority_policy_sha256 != pin.policy_sha256
            or message.authorized_by_principal_id != pin.principal_id
            or message.authorization_key_id != pin.key_id
        ):
            raise QualificationVerificationError(
                "qualification grant is not issued by the deployment-pinned authority"
            )
        if not message.authorized_at <= observed_at < message.expires_at:
            raise QualificationVerificationError("qualification grant is outside its validity")
        if message.expires_at > pin.active_until:
            raise QualificationVerificationError(
                "qualification grant outlives the deployment-pinned key"
            )
        if not pin.active_at(message.authorized_at) or not pin.active_at(observed_at):
            raise QualificationVerificationError("deployment-pinned qualification key is inactive")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
                bytes.fromhex(grant.signature_ed25519_hex),
                message.message_bytes,
            )
        except (InvalidSignature, ValueError) as exc:
            raise QualificationVerificationError(
                "qualification grant signature is invalid"
            ) from exc


class VerifiedEngineeringQualification(ExecutionModel):
    """Pure execution-qualification receipt; it can never admit scientific evidence."""

    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    input_artifact_verified_receipt_sha256s: tuple[str, ...]
    prior_execution_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    cost_quote_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def _grant_message(
    bundle: EngineeringQualificationBundle,
    *,
    pin: QualificationAuthorityPin,
    authorized_at: datetime,
    expires_at: datetime,
) -> EngineeringQualificationGrantMessage:
    request = bundle.compilation_request
    result = bundle.compilation_result
    intent = bundle.intent
    return EngineeringQualificationGrantMessage(
        bundle_sha256=bundle.bundle_sha256,
        quest_id=intent.quest_id,
        graph_scope_sha256=request.protocol.graph_scope.graph_scope_sha256,
        protocol_sha256=intent.protocol_sha256,
        compilation_request_sha256=canonical_sha256(request),
        compilation_result_sha256=canonical_sha256(result),
        compilation_receipt_sha256=result.receipt.receipt_sha256,
        work_order_id=bundle.work_order.work_order_id,
        work_order_sha256=bundle.work_order.work_order_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        input_artifact_verified_receipt_sha256s=(bundle.input_artifact_verified_receipt_sha256s),
        prior_execution_receipt_sha256=(
            bundle.prior_execution_receipt.execution_receipt_sha256
            if bundle.prior_execution_receipt is not None
            else None
        ),
        budget_authorization_sha256=bundle.budget_authorization.authorization_sha256,
        cost_quote_sha256=bundle.cost_quote.quote_sha256,
        qualification_authority_policy_sha256=pin.policy_sha256,
        authorized_by_principal_id=pin.principal_id,
        authorization_key_id=pin.key_id,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )


def _resolve_qualification_inputs(
    *,
    bundle: EngineeringQualificationBundle,
    artifact_resolver: VerifiedInputArtifactResolverPort,
    observed_at: datetime,
) -> dict[str, VerifiedInputArtifactResolution]:
    """Freshly close every input before a qualification is signed or admitted.

    Artifact and producer ``verified_at`` values are node/verifier evidence, not an
    authorization clock.  The ordering guarantee comes from performing this exact custody
    resolution before the qualification signer emits its signature, and repeating it at the
    allocator's PostgreSQL observation time.
    """

    protocol_ports = {item.port_id: item for item in bundle.compilation_request.protocol.data_ports}
    resolved: dict[str, VerifiedInputArtifactResolution] = {}
    for receipt_sha256 in bundle.input_artifact_verified_receipt_sha256s:
        try:
            candidate = artifact_resolver.resolve_verified_input_artifact(
                verified_receipt_sha256=receipt_sha256,
                observed_at=observed_at,
            )
            if candidate is None:
                raise QualificationVerificationError(
                    "qualification input receipt is absent from verified archive custody"
                )
            resolution = VerifiedInputArtifactResolution.model_validate(
                candidate.model_dump(mode="python")
            )
        except QualificationVerificationError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise QualificationVerificationError(
                "qualification input receipt archive returned invalid bytes"
            ) from exc
        receipt = resolution.verified_receipt
        if resolution.verified_receipt_sha256 != receipt_sha256:
            raise QualificationVerificationError(
                "qualification input receipt bytes changed from their archived identity"
            )
        if resolution.resolved_at != observed_at:
            raise QualificationVerificationError(
                "qualification input was not freshly resolved at the authority observation"
            )
        resolved[receipt_sha256] = resolution

    for binding in bundle.intent.input_artifact_bindings:
        resolution = resolved[binding.artifact_verified_receipt_sha256]
        receipt = resolution.verified_receipt
        if receipt.artifact.role is not ArtifactRole.RAW_OUTPUT:
            raise QualificationVerificationError(
                "qualification input must resolve to a verified raw-output artifact"
            )
        if binding.source_kind == "protocol_input":
            port = protocol_ports.get(binding.input_port_id)
            if port is None or receipt.artifact.schema_sha256 != port.schema_ref.schema_sha256:
                raise QualificationVerificationError(
                    "protocol input receipt does not match its exact frozen schema"
                )
            continue

        producer_receipt = resolution.producer_execution_receipt
        if producer_receipt is None:
            raise QualificationVerificationError(
                "intermediate input lacks successful producer ExecutionReceipt lineage"
            )
        try:
            verify_execution_intent_binding(bundle.work_order, producer_receipt.intent)
        except (TypeError, ValueError) as exc:
            raise QualificationVerificationError(
                "intermediate producer intent differs from its frozen WorkOrder"
            ) from exc
        producers = tuple(
            node
            for node in bundle.work_order.nodes
            if binding.input_port_id in node.output_port_ids
        )
        if len(producers) != 1:
            raise QualificationVerificationError(
                "intermediate input receipt has no unique WorkOrder producer"
            )
        expected = next(
            item
            for item in producers[0].expected_artifacts
            if item.artifact_key == binding.input_port_id
        )
        if (
            receipt.artifact.expected_artifact_id != expected.expected_artifact_id
            or receipt.artifact.schema_sha256 != expected.schema_sha256
            or receipt.artifact.media_type != expected.media_type
            or producer_receipt.intent.work_order_sha256 != bundle.work_order.work_order_sha256
            or producer_receipt.intent.work_order_node_id != binding.source_work_order_node_id
            or producer_receipt.intent.work_order_node_sha256
            != binding.source_work_order_node_sha256
            or producer_receipt.intent.replicate_slot.replicate_slot_id
            != binding.source_replicate_slot_id
            or producer_receipt.intent.replicate_slot.slot_index != binding.source_slot_index
        ):
            raise QualificationVerificationError(
                "intermediate input receipt differs from its producer artifact contract"
            )
    return resolved


def issue_engineering_qualification_grant(
    bundle: EngineeringQualificationBundle,
    *,
    pin: QualificationAuthorityPin,
    artifact_resolver: VerifiedInputArtifactResolverPort,
    authority_resolver: ExecutionAuthorityResolverPort,
    private_key: bytes,
    authorized_at: datetime,
    expires_at: datetime,
) -> EngineeringQualificationGrant:
    """Sign one exact qualification bundle with its deployment-pinned key."""

    _require_utc_timestamp(authorized_at, "qualification authorized_at")
    _require_utc_timestamp(expires_at, "qualification expires_at")
    bundle = EngineeringQualificationBundle.model_validate(bundle.model_dump(mode="python"))
    pin = QualificationAuthorityPin.model_validate(pin.model_dump(mode="python"))
    prior_resolution = _resolve_registered_execution_authority(
        bundle=bundle,
        resolver=authority_resolver,
        observed_at=authorized_at,
    )
    _resolve_qualification_inputs(
        bundle=bundle,
        artifact_resolver=artifact_resolver,
        observed_at=authorized_at,
    )
    if _public_key_bytes(private_key).hex() != pin.public_key_ed25519_hex:
        raise QualificationVerificationError(
            "qualification private key does not match the deployment pin"
        )
    if not pin.active_at(authorized_at) or not authorized_at < expires_at <= min(
        bundle.intent.deadline,
        bundle.budget_authorization.expires_at,
        bundle.cost_quote.expires_at,
        pin.active_until,
    ):
        raise QualificationVerificationError("qualification grant has an invalid time window")
    if not bundle.budget_authorization.authorized_at <= bundle.intent.authorized_at:
        raise QualificationVerificationError("qualification predates its budget authorization")
    if not (
        bundle.intent.authorized_at
        <= bundle.cost_quote.quoted_at
        <= authorized_at
        < bundle.cost_quote.expires_at
    ):
        raise QualificationVerificationError("qualification uses an inactive cost quote")
    if prior_resolution is not None and (
        bundle.cost_quote.quoted_at < prior_resolution.committed_at
        or authorized_at < prior_resolution.committed_at
    ):
        raise QualificationVerificationError(
            "retry quote or qualification predates committed prior failure custody"
        )
    if authorized_at + timedelta(seconds=bundle.cost_quote.maximum_lease_seconds) > min(
        expires_at,
        bundle.intent.deadline,
        bundle.budget_authorization.expires_at,
        bundle.cost_quote.expires_at,
        pin.active_until,
    ):
        raise QualificationVerificationError(
            "qualification cannot fit the quoted lease inside its active authorization window"
        )
    message = _grant_message(
        bundle,
        pin=pin,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )
    return EngineeringQualificationGrant(
        message=message,
        signature_ed25519_hex=(
            Ed25519PrivateKey.from_private_bytes(private_key).sign(message.message_bytes).hex()
        ),
    )


def verify_engineering_qualification(
    *,
    bundle: EngineeringQualificationBundle,
    grant: EngineeringQualificationGrant,
    authority: QualificationAuthorityVerifier,
    artifact_resolver: VerifiedInputArtifactResolverPort,
    authority_resolver: ExecutionAuthorityResolverPort,
    observed_at: datetime,
) -> VerifiedEngineeringQualification:
    """Recompute compilation/intent bindings, resolve full input lineage, and verify the grant.

    ``observed_at`` must be supplied by the allocator's trusted database clock.  This pure
    verifier does not read or confer authority on a process/worker clock.  Each resolver result
    must use the same database-clock observation for its fresh CAS/custody recheck.
    """

    _require_utc_timestamp(observed_at, "qualification verifier observed_at")
    try:
        bundle = EngineeringQualificationBundle.model_validate(bundle.model_dump(mode="python"))
        grant = EngineeringQualificationGrant.model_validate(grant.model_dump(mode="python"))
        prior_resolution = _resolve_registered_execution_authority(
            bundle=bundle,
            resolver=authority_resolver,
            observed_at=observed_at,
        )
        authority.verify_signature(grant, observed_at=observed_at)
        expected_message = _grant_message(
            bundle,
            pin=authority.pin,
            authorized_at=grant.message.authorized_at,
            expires_at=grant.message.expires_at,
        )
        if grant.message != expected_message:
            raise QualificationVerificationError(
                "qualification grant is rebound to different frozen execution material"
            )
        if not (
            bundle.budget_authorization.authorized_at
            <= observed_at
            < bundle.budget_authorization.expires_at
            and bundle.cost_quote.quoted_at <= observed_at < bundle.cost_quote.expires_at
            and grant.message.authorized_at <= observed_at < grant.message.expires_at
        ):
            raise QualificationVerificationError(
                "qualification budget, quote, or grant is inactive at allocator time"
            )
        if not (
            bundle.intent.authorized_at
            <= bundle.cost_quote.quoted_at
            <= grant.message.authorized_at
            <= observed_at
        ):
            raise QualificationVerificationError(
                "intent, quote, grant, and allocator observation times are out of order"
            )
        if prior_resolution is not None and (
            bundle.cost_quote.quoted_at < prior_resolution.committed_at
            or grant.message.authorized_at < prior_resolution.committed_at
        ):
            raise QualificationVerificationError(
                "retry quote or qualification predates committed prior failure custody"
            )
        if grant.message.expires_at > min(
            bundle.intent.deadline,
            bundle.budget_authorization.expires_at,
            bundle.cost_quote.expires_at,
            authority.pin.active_until,
        ):
            raise QualificationVerificationError(
                "qualification grant outlives a bound budget, quote, intent, or key"
            )
        if grant.message.authorized_at + timedelta(
            seconds=bundle.cost_quote.maximum_lease_seconds
        ) > min(
            grant.message.expires_at,
            bundle.intent.deadline,
            bundle.budget_authorization.expires_at,
            bundle.cost_quote.expires_at,
            authority.pin.active_until,
        ):
            raise QualificationVerificationError(
                "qualification cannot fit the quoted lease inside its active window"
            )
        verify_compilation(bundle.compilation_request, bundle.compilation_result)
        verify_execution_intent_binding(bundle.work_order, bundle.intent)
        if bundle.prior_execution_receipt is not None:
            verify_execution_retry_binding(
                bundle.prior_execution_receipt.intent,
                bundle.intent,
                bundle.prior_execution_receipt,
            )
    except QualificationVerificationError:
        raise
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "qualification compilation or intent failed canonical verification"
        ) from exc

    _resolve_qualification_inputs(
        bundle=bundle,
        artifact_resolver=artifact_resolver,
        observed_at=observed_at,
    )

    return VerifiedEngineeringQualification(
        grant_sha256=grant.grant_sha256,
        bundle_sha256=bundle.bundle_sha256,
        intent_sha256=bundle.intent.intent_sha256,
        execution_id=bundle.intent.execution_id,
        infrastructure_attempt_id=(bundle.intent.infrastructure_attempt.infrastructure_attempt_id),
        input_artifact_verified_receipt_sha256s=(bundle.input_artifact_verified_receipt_sha256s),
        prior_execution_receipt_sha256=(
            bundle.prior_execution_receipt.execution_receipt_sha256
            if bundle.prior_execution_receipt is not None
            else None
        ),
        budget_authorization_sha256=bundle.budget_authorization.authorization_sha256,
        cost_quote_sha256=bundle.cost_quote.quote_sha256,
        verified_at=observed_at,
    )


__all__ = [
    "AttemptAdoptionReason",
    "AttemptAdoptionReceipt",
    "BudgetAuthorization",
    "EngineeringQualificationBundle",
    "EngineeringQualificationGrant",
    "EngineeringQualificationGrantMessage",
    "ExecutionCostQuote",
    "NodeEnrollmentAuthorityPin",
    "NodeEnrollmentAuthorityVerifier",
    "NodeExecutionReceipt",
    "NodeHealth",
    "NodeInventoryAttestation",
    "NodeInventoryResource",
    "NodeRuntimeIdentity",
    "QualificationAuthorityPin",
    "QualificationAuthorityVerifier",
    "QualificationVerificationError",
    "RUNTIME_CONTRACT_SCHEMA_VERSION",
    "RuntimeInspectionReceipt",
    "RuntimeInspectionState",
    "TerminalVerificationAttestation",
    "TerminalVerificationAttestationMessage",
    "TerminalVerificationAuthorityPin",
    "TerminalVerificationAuthorityVerifier",
    "VerifiedAttemptAdoption",
    "VerifiedBudgetAuthorizationResolution",
    "VerifiedEngineeringQualification",
    "VerifiedExecutionReceiptResolution",
    "VerifiedInputArtifactResolution",
    "VerifiedNodeExecution",
    "VerifiedNodeInventoryAttestation",
    "VerifiedRuntimeInspection",
    "VerifiedTerminalVerification",
    "WorkerNodeAuthorityVerifier",
    "WorkerNodeEnrollment",
    "WorkerNodeEnrollmentMessage",
    "WorkerNodeManifest",
    "artifact_output_tree_sha256",
    "issue_attempt_adoption_receipt",
    "issue_engineering_qualification_grant",
    "issue_node_inventory_attestation",
    "issue_node_execution_receipt",
    "issue_runtime_inspection_receipt",
    "issue_terminal_verification_attestation",
    "issue_worker_node_enrollment",
    "qualification_key_id",
    "verify_attempt_adoption",
    "verify_engineering_qualification",
    "verify_node_inventory_attestation",
    "verify_node_execution_receipt",
    "verify_runtime_for_adoption",
    "verify_runtime_for_release_or_retry",
    "verify_worker_node_enrollment",
]
