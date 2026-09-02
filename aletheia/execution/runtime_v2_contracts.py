"""Honest qualification-runtime contracts for production-local composition.

These contracts deliberately separate inert runtime preparation from evidence observed only after
an OCI process really starts.  They remain engineering-qualification values: none of them admits a
scientific action, observation, or claim.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.runtime_contracts import (
    NodeRuntimeIdentity,
    QualificationVerificationError,
    RuntimeInspectionState,
    WorkerNodeAuthorityVerifier,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
    qualification_key_id,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    ExecutionIntent,
    ExecutionModel,
    canonical_json_bytes,
    canonical_sha256,
)

RUNTIME_V2_CONTRACT_SCHEMA_VERSION = 2

# A loop device can be sector-aligned yet still be too small for the pinned ext4 formatter.
# Qualification deployments use this conservative floor and must prove it in the opt-in real
# Linux campaign before admission is enabled.
MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES = 16 * 1024 * 1024

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_RUNTIME_CONTROL_DOMAIN = b"ALETHEIA_RUNTIME_CONTROL_V2\x00"
_MAX_NODE_PROOF_SIGNING_LAG = timedelta(seconds=60)


def _public_key_hex(private_key: bytes) -> str:
    if len(private_key) != 32:
        raise QualificationVerificationError(
            "node signing private key must contain exactly 32 raw bytes"
        )
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise QualificationVerificationError(f"{label} must be timezone-aware UTC")


def _validate_workload_projection(*, executable_sha256: str, argv: tuple[str, ...]) -> None:
    executable = argv[0] if argv else ""
    components = executable.split("/")
    if (
        re.fullmatch(_SHA256_PATTERN, executable_sha256) is None
        or not executable.startswith("/")
        or executable.endswith("/")
        or any(component in {"", ".", ".."} for component in components[1:])
        or any(
            not argument
            or len(argument) > 16_384
            or any(character in argument for character in ("\x00", "\n", "\r"))
            for argument in argv
        )
    ):
        raise ValueError("runtime workload projection is not canonical direct-exec authority")


def _runtime_control_message(*, kind: str, payload: dict[str, object]) -> bytes:
    return _RUNTIME_CONTROL_DOMAIN + kind.encode("ascii") + b"\x00" + canonical_json_bytes(payload)


class RuntimeControlAuthorityPin(ExecutionModel):
    """Deployment-pinned allocator/DB signer for runtime-control challenges and recovery."""

    schema_name: Literal["aletheia.runtime_control_authority_pin"] = (
        "aletheia.runtime_control_authority_pin"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _pin_is_exact_and_finite(self) -> "RuntimeControlAuthorityPin":
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("runtime-control key id differs from pinned public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("runtime-control key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("runtime-control key revocation is outside its validity")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class RuntimeControlAuthorityVerifier:
    """Verify online DB runtime authority only against an independent deployment pin."""

    def __init__(self, pin: RuntimeControlAuthorityPin) -> None:
        self._pin = RuntimeControlAuthorityPin.model_validate(pin.model_dump(mode="python"))

    @property
    def pin(self) -> RuntimeControlAuthorityPin:
        return self._pin

    def verify(
        self,
        *,
        kind: str,
        payload: dict[str, object],
        signature_ed25519_hex: str,
        policy_sha256: str,
        principal_id: str,
        key_id: str,
        signed_at: datetime,
        expires_at: datetime,
        observed_at: datetime,
    ) -> None:
        _require_utc(observed_at, "runtime-control verifier observed_at")
        pin = self._pin
        if (
            policy_sha256 != pin.policy_sha256
            or principal_id != pin.principal_id
            or key_id != pin.key_id
            or not signed_at <= observed_at < expires_at <= pin.active_until
            or not pin.active_at(signed_at)
            or not pin.active_at(observed_at)
        ):
            raise QualificationVerificationError(
                "runtime-control authority, window, or deployment pin differs"
            )
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
                bytes.fromhex(signature_ed25519_hex),
                _runtime_control_message(kind=kind, payload=payload),
            )
        except (InvalidSignature, ValueError) as exc:
            raise QualificationVerificationError(
                "runtime-control authority signature is invalid"
            ) from exc

    def verify_historical(
        self,
        *,
        kind: str,
        payload: dict[str, object],
        signature_ed25519_hex: str,
        policy_sha256: str,
        principal_id: str,
        key_id: str,
        signed_at: datetime,
    ) -> None:
        """Verify an immutable accepted record at its signing time, without freshness."""

        pin = self._pin
        if (
            policy_sha256 != pin.policy_sha256
            or principal_id != pin.principal_id
            or key_id != pin.key_id
            or not pin.active_at(signed_at)
        ):
            raise QualificationVerificationError(
                "historical runtime-control authority differs from deployment pin"
            )
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
                bytes.fromhex(signature_ed25519_hex),
                _runtime_control_message(kind=kind, payload=payload),
            )
        except (InvalidSignature, ValueError) as exc:
            raise QualificationVerificationError(
                "historical runtime-control signature is invalid"
            ) from exc


class AttemptScopedPreRuntimeCleanupAuthorityPin(ExecutionModel):
    """One deployment-pinned key that can only release one never-started attempt.

    This authority exists for the narrow case where an exact pre-workload cleanup remains
    necessary after the enrolled node signing key has expired.  It is not a node enrollment,
    runtime-control, launch, terminal, or scientific authority.  The source launch lineage and
    root-watchdog deployment are frozen directly into the pin so the key cannot be reused for a
    second attempt or cleanup generation.
    """

    schema_name: Literal["aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin"] = (
        "aletheia.attempt_scoped_pre_runtime_cleanup_authority_pin"
    )
    schema_version: Literal[1] = 1
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    source_node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    watchdog_deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    cleanup_only: Literal[True] = True
    launch_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authority_is_finite_and_exact(self) -> "AttemptScopedPreRuntimeCleanupAuthorityPin":
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("pre-runtime cleanup key id differs from pinned public key")
        if self.expires_at <= self.valid_from or self.expires_at - self.valid_from > timedelta(
            hours=1
        ):
            raise ValueError(
                "pre-runtime cleanup authority must have a positive window of at most one hour"
            )
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("pre-runtime cleanup revocation is outside its validity window")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    @property
    def authority_sha256(self) -> str:
        return canonical_sha256(self)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class AttemptScopedPreRuntimeCleanupAuthorityVerifier:
    """Verify only signatures made by one exact cleanup-only deployment pin."""

    def __init__(self, pin: AttemptScopedPreRuntimeCleanupAuthorityPin) -> None:
        self._pin = AttemptScopedPreRuntimeCleanupAuthorityPin.model_validate(
            pin.model_dump(mode="python")
        )

    @property
    def pin(self) -> AttemptScopedPreRuntimeCleanupAuthorityPin:
        return self._pin

    def verify_signature(
        self,
        *,
        message: bytes,
        signature_ed25519_hex: str,
        signing_key_id: str,
        signed_at: datetime,
        expires_at: datetime,
        observed_at: datetime,
    ) -> None:
        _require_utc(observed_at, "pre-runtime cleanup verifier observed_at")
        pin = self._pin
        if (
            signing_key_id != pin.key_id
            or not pin.active_at(signed_at)
            or not signed_at <= observed_at < expires_at <= pin.active_until
        ):
            raise QualificationVerificationError(
                "pre-runtime cleanup receipt is outside its exact recovery authority"
            )
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
                bytes.fromhex(signature_ed25519_hex), message
            )
        except (InvalidSignature, ValueError) as exc:
            raise QualificationVerificationError(
                "pre-runtime cleanup recovery signature is invalid"
            ) from exc


class RuntimeLaunchAuthorizationRequest(ExecutionModel):
    """Node-monotonic nonce that prevents a delayed/rolled-back-wall-clock launch."""

    schema_name: Literal["aletheia.runtime_launch_authorization_request"] = (
        "aletheia.runtime_launch_authorization_request"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    request_nonce_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    pre_runtime_absence_epoch: int = Field(default=0, ge=0)
    pre_runtime_absence_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    requested_at: AwareDatetime
    requested_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _absence_epoch_is_exact(self) -> "RuntimeLaunchAuthorizationRequest":
        if (self.pre_runtime_absence_epoch == 0) != (
            self.pre_runtime_absence_receipt_sha256 is None
        ):
            raise ValueError(
                "initial launch has absence epoch zero; replacement launch binds one receipt"
            )
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class RuntimeLaunchAuthorization(ExecutionModel):
    """Short-lived DB launch ticket issued only after one inert preparation is persisted."""

    schema_name: Literal["aletheia.runtime_launch_authorization"] = (
        "aletheia.runtime_launch_authorization"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    lease_expires_at: AwareDatetime
    hard_deadline: AwareDatetime
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    max_launch_delay_ns: int = Field(ge=1, le=60_000_000_000)
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _ticket_is_short_and_bounded(self) -> "RuntimeLaunchAuthorization":
        if not (self.issued_at < self.expires_at <= self.lease_expires_at <= self.hard_deadline):
            raise ValueError("runtime launch authorization outlives lease or hard deadline")
        _validate_workload_projection(
            executable_sha256=self.workload_executable_sha256,
            argv=self.workload_argv,
        )
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="runtime_launch_authorization", payload=self.signature_payload
        )

    @property
    def authorization_sha256(self) -> str:
        return canonical_sha256(self)


def issue_runtime_launch_authorization(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> RuntimeLaunchAuthorization:
    """Issue one DB-clock ticket; callers must provide its complete immutable scope."""

    try:
        unsigned = RuntimeLaunchAuthorization(
            **scope,
            runtime_control_policy_sha256=pin.policy_sha256,
            authorized_by_principal_id=pin.principal_id,
            authorization_key_id=pin.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "runtime launch authorization scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or not pin.active_at(unsigned.issued_at)
        or unsigned.expires_at > pin.active_until
    ):
        raise QualificationVerificationError("runtime launch authorization signer is inactive")
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return RuntimeLaunchAuthorization.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_runtime_launch_authorization(
    *,
    authorization: RuntimeLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: RuntimePreparation,
    authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
    observed_monotonic_ns: int,
) -> None:
    """Verify a ticket immediately before a create/start mutation.

    This online check intentionally uses the node's suspend-aware request clock.  Crash recovery
    of an already-started process uses :func:`verify_runtime_launch_authorization_historical`
    instead and proves that the *actual engine start* occurred inside this window.
    """

    _require_utc(observed_at, "runtime launch authorization observed_at")
    if observed_monotonic_ns < 0:
        raise QualificationVerificationError(
            "runtime launch authorization monotonic observation is invalid"
        )
    authorization, authorization_request, preparation = (
        _validated_runtime_launch_authorization_scope(
            authorization=authorization,
            authorization_request=authorization_request,
            preparation=preparation,
        )
    )
    monotonic_age_ns = observed_monotonic_ns - authorization_request.requested_monotonic_ns
    if (
        authorization_request.requested_at > observed_at
        or monotonic_age_ns < 0
        or monotonic_age_ns >= authorization.max_launch_delay_ns
    ):
        raise QualificationVerificationError(
            "runtime launch authorization differs from exact preparation"
        )
    authority.verify(
        kind="runtime_launch_authorization",
        payload=authorization.signature_payload,
        signature_ed25519_hex=authorization.signature_ed25519_hex,
        policy_sha256=authorization.runtime_control_policy_sha256,
        principal_id=authorization.authorized_by_principal_id,
        key_id=authorization.authorization_key_id,
        signed_at=authorization.issued_at,
        expires_at=authorization.expires_at,
        observed_at=observed_at,
    )


class PinnedInputPath(ExecutionModel):
    """Exact WorkOrder input-port to sandbox-relative path binding."""

    schema_name: Literal["aletheia.pinned_qualification_input_path"] = (
        "aletheia.pinned_qualification_input_path"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    input_port_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    relative_path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _path_is_relative_and_canonical(self) -> "PinnedInputPath":
        components = self.relative_path.split("/")
        if (
            self.relative_path.startswith("/")
            or "\\" in self.relative_path
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise ValueError("pinned input path must be a canonical relative path")
        return self


class InputMaterializationEntry(ExecutionModel):
    """One freshly rehashed CAS object written to one exact read-only staged path."""

    schema_name: Literal["aletheia.input_materialization_entry"] = (
        "aletheia.input_materialization_entry"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    input_port_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_bytes: int = Field(ge=0)
    relative_path: str = Field(min_length=1, max_length=1024)
    staged_file_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def _entry_path_is_canonical(self) -> "InputMaterializationEntry":
        PinnedInputPath(input_port_id=self.input_port_id, relative_path=self.relative_path)
        return self


class InputMaterializationReceipt(ExecutionModel):
    """Complete input tree that is subsequently bound into the runtime launch evidence."""

    schema_name: Literal["aletheia.input_materialization_receipt"] = (
        "aletheia.input_materialization_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    entries: tuple[InputMaterializationEntry, ...]
    staged_root_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    materializer_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    materialized_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _entries_are_complete_and_canonical(self) -> "InputMaterializationReceipt":
        expected = tuple(sorted(self.entries, key=lambda item: item.input_port_id))
        ports = tuple(item.input_port_id for item in self.entries)
        paths = tuple(item.relative_path for item in self.entries)
        if (
            self.entries != expected
            or len(set(ports)) != len(ports)
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("input materialization entries and paths must be unique and canonical")
        return self

    @property
    def materialization_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class OutputQuotaProvisioningReceipt(ExecutionModel):
    """Privileged, post-mount identity of one attempt-scoped output byte ceiling.

    The receipt is created before input materialization and runtime preparation.  It is not an
    authority to mount arbitrary paths: the node supplies the already attempt-scoped workspace,
    while a deployment-pinned provisioner establishes one exclusive loop-backed filesystem and
    returns its exact kernel-visible identity.  Replays must return the same receipt bytes.
    """

    schema_name: Literal["aletheia.output_quota_provisioning_receipt"] = (
        "aletheia.output_quota_provisioning_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_root: str = Field(min_length=2, max_length=4096)
    # The generic receipt remains able to represent bounded test/non-loop controllers.  Concrete
    # loop deployments pin and enforce ``MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES`` through their
    # provisioner capability; allocator admission uses the same exported constant.
    output_quota_bytes: int = Field(ge=512)
    output_root_device: int = Field(ge=0)
    output_root_inode: int = Field(ge=1)
    output_root_owner_uid: int = Field(ge=1, le=2**31 - 1)
    output_root_owner_gid: int = Field(ge=1, le=2**31 - 1)
    output_root_mode: Literal[0o700] = 0o700
    mount_id: int = Field(ge=1)
    mount_parent_id: int = Field(ge=1)
    block_device_major: int = Field(ge=0)
    block_device_minor: int = Field(ge=0)
    block_device_capacity_bytes: int = Field(ge=512)
    filesystem_type: Literal["ext4", "xfs"]
    filesystem_uuid_sha256: str = Field(pattern=_SHA256_PATTERN)
    # Linux projects generic VFS mount flags (left side of ``mountinfo``) separately
    # from filesystem superblock options.  A real ext4 mount normally adds an atime
    # policy such as ``relatime`` even when mount(8) was only given the security
    # flags.  Keep the complete, sorted VFS projection in the receipt so recovery
    # can compare it byte-for-byte without pretending those kernel-added flags do
    # not exist.
    mount_options: tuple[
        Literal[
            "lazytime",
            "noatime",
            "nodev",
            "noexec",
            "nosuid",
            "relatime",
            "rw",
            "strictatime",
        ],
        ...,
    ]
    backing_file_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    provisioner_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    provisioner_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    provisioned_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _quota_mount_is_exact_and_canonical(self) -> "OutputQuotaProvisioningReceipt":
        root = Path(self.output_root)
        if (
            not root.is_absolute()
            or str(root) != self.output_root
            or self.output_root == "/"
            or any(character in self.output_root for character in ("\x00", "\n", "\r"))
            or self.block_device_capacity_bytes % 512 != 0
            or self.block_device_capacity_bytes > self.output_quota_bytes
            or tuple(sorted(self.mount_options)) != self.mount_options
            or len(set(self.mount_options)) != len(self.mount_options)
            or not {"nodev", "noexec", "nosuid", "rw"}.issubset(self.mount_options)
            or len({"noatime", "relatime", "strictatime"}.intersection(self.mount_options)) > 1
        ):
            raise ValueError("output quota provisioning receipt is not one exact mount ceiling")
        return self

    @property
    def provisioning_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class PinnedOutputWorkspaceRoot(ExecutionModel):
    """Boot/deployment-pinned root custody for attempt output mountpoints.

    This is deliberately separate from the node's private 0700 journal root.  The root-owned
    sticky directory lets the dedicated node UID create a fresh attempt directory; after the
    privileged provisioner transfers that attempt directory to root, the node UID can traverse it
    but cannot rename it or replace its output child.
    """

    schema_name: Literal["aletheia.pinned_output_workspace_root"] = (
        "aletheia.pinned_output_workspace_root"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    path: str = Field(min_length=2, max_length=4096)
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    mount_id: int = Field(ge=1)
    owner_uid: Literal[0] = 0
    owner_gid: int = Field(ge=1, le=2**31 - 1)
    mode: Literal[0o1730] = 0o1730
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True

    @model_validator(mode="after")
    def _path_is_canonical(self) -> "PinnedOutputWorkspaceRoot":
        root = Path(self.path)
        if (
            not root.is_absolute()
            or str(root) != self.path
            or self.path == "/"
            or any(character in self.path for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("pinned output workspace root is not canonical")
        return self

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class RuntimePreparation(ExecutionModel):
    """Crash-durable, inert runtime metadata created before launch authorization.

    A preparation intentionally contains no sandbox/container/process identity and no start time.
    Those facts can only appear in :class:`RuntimeLaunchEvidence` after the engine starts work.
    """

    schema_name: Literal["aletheia.runtime_preparation"] = "aletheia.runtime_preparation"
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    runtime_engine: str = Field(min_length=1, max_length=128)
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_quota_provisioning_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_runtime_locator_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    prepared_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _preparation_binds_direct_workload(self) -> "RuntimePreparation":
        _validate_workload_projection(
            executable_sha256=self.workload_executable_sha256,
            argv=self.workload_argv,
        )
        return self

    @property
    def preparation_sha256(self) -> str:
        return canonical_sha256(self)


def _validated_runtime_launch_authorization_scope(
    *,
    authorization: RuntimeLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: RuntimePreparation,
) -> tuple[RuntimeLaunchAuthorization, RuntimeLaunchAuthorizationRequest, RuntimePreparation]:
    """Return one closed ticket/request/preparation scope without granting freshness."""

    try:
        authorization = RuntimeLaunchAuthorization.model_validate(
            authorization.model_dump(mode="python")
        )
        authorization_request = RuntimeLaunchAuthorizationRequest.model_validate(
            authorization_request.model_dump(mode="python")
        )
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "runtime launch authorization scope failed closed validation"
        ) from exc
    if (
        authorization.authorization_request_sha256 != authorization_request.request_sha256
        or authorization_request.runtime_preparation_sha256 != preparation.preparation_sha256
        or authorization_request.infrastructure_attempt_id != preparation.infrastructure_attempt_id
        or authorization_request.fencing_epoch != preparation.fencing_epoch
        or authorization_request.lease_token_sha256 != preparation.lease_token_sha256
        or authorization.node_manifest_sha256 != preparation.node_manifest_sha256
        or authorization.node_id != preparation.node_id
        or authorization.boot_id != preparation.boot_id
        or authorization.execution_id != preparation.execution_id
        or authorization.infrastructure_attempt_id != preparation.infrastructure_attempt_id
        or authorization.intent_sha256 != preparation.intent_sha256
        or authorization.runtime_preparation_sha256 != preparation.preparation_sha256
        or authorization.launch_spec_sha256 != preparation.launch_spec_sha256
        or authorization.oci_config_sha256 != preparation.oci_config_sha256
        or authorization.workload_executable_sha256 != preparation.workload_executable_sha256
        or authorization.workload_argv != preparation.workload_argv
        or authorization.enforced_placement_sha256 != preparation.enforced_placement_sha256
        or authorization.input_materialization_receipt_sha256
        != preparation.input_materialization_receipt_sha256
        or authorization.fencing_epoch != preparation.fencing_epoch
        or authorization.lease_token_sha256 != preparation.lease_token_sha256
        or preparation.prepared_at > authorization_request.requested_at
        or authorization_request.requested_at > authorization.issued_at
        or preparation.prepared_monotonic_ns > authorization_request.requested_monotonic_ns
    ):
        raise QualificationVerificationError(
            "runtime launch authorization differs from exact preparation"
        )
    return authorization, authorization_request, preparation


class HistoricalPreRuntimeRecoveryLineage(ExecutionModel):
    """Exact persisted start lineage used only to prove and clean a pre-workload generation.

    The embedded launch authorization is historical evidence, not fresh launch authority.  A
    consumer must independently verify its signature and must never call a runtime start from
    this aggregate.
    """

    schema_name: Literal["aletheia.historical_pre_runtime_recovery_lineage"] = (
        "aletheia.historical_pre_runtime_recovery_lineage"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    runtime_preparation: RuntimePreparation
    runtime_launch_authorization_request: RuntimeLaunchAuthorizationRequest
    runtime_launch_authorization: RuntimeLaunchAuthorization
    cleanup_only: Literal[True] = True
    launch_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _lineage_is_exact_and_never_authorizes_launch(
        self,
    ) -> "HistoricalPreRuntimeRecoveryLineage":
        _validated_runtime_launch_authorization_scope(
            authorization=self.runtime_launch_authorization,
            authorization_request=self.runtime_launch_authorization_request,
            preparation=self.runtime_preparation,
        )
        return self

    @property
    def lineage_sha256(self) -> str:
        return canonical_sha256(self)


def verify_runtime_launch_authorization_ticket_historical(
    *,
    authorization: RuntimeLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: RuntimePreparation,
    authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Verify immutable ticket scope/signature without treating it as fresh authority."""

    authorization, _, _ = _validated_runtime_launch_authorization_scope(
        authorization=authorization,
        authorization_request=authorization_request,
        preparation=preparation,
    )
    if authorization.expires_at > authority.pin.active_until:
        raise QualificationVerificationError(
            "historical runtime launch ticket outlives its deployment pin"
        )
    authority.verify_historical(
        kind="runtime_launch_authorization",
        payload=authorization.signature_payload,
        signature_ed25519_hex=authorization.signature_ed25519_hex,
        policy_sha256=authorization.runtime_control_policy_sha256,
        principal_id=authorization.authorized_by_principal_id,
        key_id=authorization.authorization_key_id,
        signed_at=authorization.issued_at,
    )


def verify_runtime_launch_authorization_historical(
    *,
    authorization: RuntimeLaunchAuthorization,
    authorization_request: RuntimeLaunchAuthorizationRequest,
    preparation: RuntimePreparation,
    authority: RuntimeControlAuthorityVerifier,
    started_at: datetime,
    started_monotonic_lower_bound_ns: int,
    started_monotonic_upper_bound_exclusive_ns: int,
) -> None:
    """Verify that an actual engine start occurred inside one signed launch ticket.

    Unlike the online mutation gate, this check does not require the later observation to fall
    inside the ticket.  It is therefore safe for post-start/pre-journal crash recovery while still
    proving the process could only have started during the node request's short BOOTTIME window.
    """

    _require_utc(started_at, "historical runtime start started_at")
    if (
        started_monotonic_lower_bound_ns < 0
        or started_monotonic_upper_bound_exclusive_ns <= started_monotonic_lower_bound_ns
    ):
        raise QualificationVerificationError("historical runtime start interval is invalid")
    verify_runtime_launch_authorization_ticket_historical(
        authorization=authorization,
        authorization_request=authorization_request,
        preparation=preparation,
        authority=authority,
    )
    authorization, authorization_request, _ = _validated_runtime_launch_authorization_scope(
        authorization=authorization,
        authorization_request=authorization_request,
        preparation=preparation,
    )
    launch_window_end_ns = (
        authorization_request.requested_monotonic_ns + authorization.max_launch_delay_ns
    )
    if (
        authorization.issued_at > started_at
        or started_at >= authorization.expires_at
        or started_monotonic_lower_bound_ns < authorization_request.requested_monotonic_ns
        or started_monotonic_upper_bound_exclusive_ns > launch_window_end_ns
    ):
        raise QualificationVerificationError(
            "actual runtime start falls outside its signed launch ticket"
        )


class RuntimeLaunchEvidence(ExecutionModel):
    """Engine evidence observed only after a real runtime instance starts.

    Linux ``/proc`` exposes process start in clock ticks, not exact nanoseconds.  The identity's
    ``started_monotonic_ns`` is therefore explicitly the lower bound of the half-open interval
    below.  Qualification requires the *entire* interval to fit inside signed authority; an
    ambiguous same-tick request/start is rejected rather than presented as exact evidence.
    """

    schema_name: Literal["aletheia.runtime_launch_evidence"] = "aletheia.runtime_launch_evidence"
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity: NodeRuntimeIdentity
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_start_monotonic_lower_bound_ns: int = Field(ge=0)
    engine_start_monotonic_upper_bound_exclusive_ns: int = Field(ge=1)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_fencing_epoch: int = Field(ge=1)
    enforced_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_launch_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _launch_contains_real_ordered_identity(self) -> "RuntimeLaunchEvidence":
        identity = self.runtime_identity
        if (
            self.runtime_identity_sha256 != identity.runtime_identity_sha256
            or self.engine_start_monotonic_lower_bound_ns != identity.started_monotonic_ns
            or self.engine_start_monotonic_upper_bound_exclusive_ns
            <= self.engine_start_monotonic_lower_bound_ns
            or self.observed_at < identity.started_at
            or self.observed_monotonic_ns < identity.started_monotonic_ns
            or self.observed_monotonic_ns < self.engine_start_monotonic_upper_bound_exclusive_ns
        ):
            raise ValueError("runtime launch evidence lacks an exact observed start identity")
        return self

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class NodeRuntimeLaunchReceipt(ExecutionModel):
    """Node-signed binding from one inert preparation to one actual started runtime."""

    schema_name: Literal["aletheia.node_runtime_launch_receipt"] = (
        "aletheia.node_runtime_launch_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_evidence: RuntimeLaunchEvidence
    launch_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_binds_exact_launch(self) -> "NodeRuntimeLaunchReceipt":
        if (
            self.launch_evidence_sha256 != self.launch_evidence.evidence_sha256
            or self.signed_at < self.launch_evidence.observed_at
            or self.signed_at - self.launch_evidence.observed_at > _MAX_NODE_PROOF_SIGNING_LAG
        ):
            raise ValueError("node runtime launch receipt changed or predates its exact evidence")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def launch_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedNodeRuntimeLaunch(ExecutionModel):
    launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def _validate_launch_against_preparation(
    *,
    preparation: RuntimePreparation,
    evidence: RuntimeLaunchEvidence,
    authorization: RuntimeLaunchAuthorization | None = None,
) -> None:
    identity = evidence.runtime_identity
    if (
        evidence.preparation_sha256 != preparation.preparation_sha256
        or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
        or evidence.input_materialization_receipt_sha256
        != preparation.input_materialization_receipt_sha256
        or evidence.enforced_fencing_epoch != preparation.fencing_epoch
        or evidence.enforced_lease_token_sha256 != preparation.lease_token_sha256
        or identity.node_id != preparation.node_id
        or identity.boot_id != preparation.boot_id
        or identity.execution_id != preparation.execution_id
        or identity.infrastructure_attempt_id != preparation.infrastructure_attempt_id
        or identity.runtime_id != preparation.runtime_id
        or identity.runtime_engine != preparation.runtime_engine
        or identity.launch_spec_sha256 != preparation.launch_spec_sha256
        or identity.started_at < preparation.prepared_at
        or identity.started_monotonic_ns < preparation.prepared_monotonic_ns
    ):
        raise QualificationVerificationError(
            "runtime launch evidence differs from its exact inert preparation"
        )
    if authorization is not None and (
        evidence.runtime_launch_authorization_sha256 != authorization.authorization_sha256
    ):
        raise QualificationVerificationError(
            "runtime launch evidence differs from its DB launch authorization"
        )


def issue_node_runtime_launch_receipt(
    *,
    manifest: WorkerNodeManifest,
    preparation: RuntimePreparation,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    launch_evidence: RuntimeLaunchEvidence,
    runtime_authority: RuntimeControlAuthorityVerifier,
    signed_at: datetime,
    private_key: bytes,
) -> NodeRuntimeLaunchReceipt:
    """Sign actual engine start evidence without allowing the runtime adapter to hold the key."""

    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    launch_evidence = RuntimeLaunchEvidence.model_validate(
        launch_evidence.model_dump(mode="python")
    )
    launch_authorization = RuntimeLaunchAuthorization.model_validate(
        launch_authorization.model_dump(mode="python")
    )
    _require_utc(signed_at, "runtime launch signed_at")
    if (
        _public_key_hex(private_key) != manifest.node_signing_public_key_ed25519_hex
        or preparation.node_manifest_sha256 != manifest.manifest_sha256
        or signed_at < launch_evidence.observed_at
    ):
        raise QualificationVerificationError(
            "runtime launch signer, manifest, or signed time differs from exact evidence"
        )
    _validate_launch_against_preparation(
        preparation=preparation,
        evidence=launch_evidence,
        authorization=launch_authorization,
    )
    verify_runtime_launch_authorization_historical(
        authorization=launch_authorization,
        authorization_request=launch_authorization_request,
        preparation=preparation,
        authority=runtime_authority,
        started_at=launch_evidence.runtime_identity.started_at,
        started_monotonic_lower_bound_ns=(launch_evidence.engine_start_monotonic_lower_bound_ns),
        started_monotonic_upper_bound_exclusive_ns=(
            launch_evidence.engine_start_monotonic_upper_bound_exclusive_ns
        ),
    )
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if not manifest.frozen_at <= signed_at < active_until:
        raise QualificationVerificationError("runtime launch signing key is inactive")
    unsigned = NodeRuntimeLaunchReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        launch_evidence=launch_evidence,
        launch_evidence_sha256=launch_evidence.evidence_sha256,
        signed_at=signed_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return NodeRuntimeLaunchReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_node_runtime_launch_receipt_historical(
    *,
    receipt: NodeRuntimeLaunchReceipt,
    preparation: RuntimePreparation,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> VerifiedNodeRuntimeLaunch:
    """Verify the complete signed launch lineage at its original engine observation time."""

    try:
        receipt = NodeRuntimeLaunchReceipt.model_validate(receipt.model_dump(mode="python"))
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        launch_authorization = RuntimeLaunchAuthorization.model_validate(
            launch_authorization.model_dump(mode="python")
        )
        evidence = receipt.launch_evidence
        _validate_launch_against_preparation(
            preparation=preparation,
            evidence=evidence,
            authorization=launch_authorization,
        )
        verify_runtime_launch_authorization_historical(
            authorization=launch_authorization,
            authorization_request=launch_authorization_request,
            preparation=preparation,
            authority=runtime_authority,
            started_at=evidence.runtime_identity.started_at,
            started_monotonic_lower_bound_ns=(evidence.engine_start_monotonic_lower_bound_ns),
            started_monotonic_upper_bound_exclusive_ns=(
                evidence.engine_start_monotonic_upper_bound_exclusive_ns
            ),
        )
        if (
            receipt.node_manifest_sha256 != authority.manifest.manifest_sha256
            or preparation.node_manifest_sha256 != authority.manifest.manifest_sha256
        ):
            raise QualificationVerificationError("runtime launch receipt belongs to another node")
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
            "runtime launch receipt failed closed revalidation"
        ) from exc
    return VerifiedNodeRuntimeLaunch(
        launch_receipt_sha256=receipt.launch_receipt_sha256,
        preparation_sha256=preparation.preparation_sha256,
        runtime_identity_sha256=evidence.runtime_identity_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        fencing_epoch=preparation.fencing_epoch,
        lease_token_sha256=preparation.lease_token_sha256,
        verified_at=receipt.signed_at,
    )


def verify_node_runtime_launch_receipt(
    *,
    receipt: NodeRuntimeLaunchReceipt,
    preparation: RuntimePreparation,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> VerifiedNodeRuntimeLaunch:
    """Verify an actual runtime launch against deployment enrollment and freshness."""

    _require_utc(observed_at, "runtime launch observed_at")
    if maximum_age_seconds < 1:
        raise QualificationVerificationError("runtime launch maximum age must be positive")
    verified = verify_node_runtime_launch_receipt_historical(
        receipt=receipt,
        preparation=preparation,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        authority=authority,
        runtime_authority=runtime_authority,
    )
    receipt = NodeRuntimeLaunchReceipt.model_validate(receipt.model_dump(mode="python"))
    evidence = receipt.launch_evidence
    maximum_age = timedelta(seconds=maximum_age_seconds)
    if (
        not receipt.signed_at <= observed_at
        or observed_at - receipt.signed_at > maximum_age
        or observed_at - evidence.observed_at > maximum_age
    ):
        raise QualificationVerificationError(
            "runtime launch receipt or recovered engine observation is stale"
        )
    return verified.model_copy(update={"verified_at": observed_at})


class RuntimeInspectionEvidence(ExecutionModel):
    """Journal-backed engine observation; ``ABSENT`` means exact pre-workload absence only."""

    schema_name: Literal["aletheia.runtime_inspection_evidence"] = (
        "aletheia.runtime_inspection_evidence"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    state: RuntimeInspectionState
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity: NodeRuntimeIdentity | None = None
    runtime_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_fencing_epoch: int = Field(ge=1)
    enforced_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_control_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    prelaunch_absence_journal_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    prelaunch_absence_epoch: int | None = Field(default=None, ge=1)
    prelaunch_authorization_request_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    prelaunch_authorization_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    engine_terminal_journal_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    inspected_at: AwareDatetime
    inspected_monotonic_ns: int = Field(ge=0)
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    ended_at: AwareDatetime | None = None
    ended_monotonic_ns: int | None = Field(default=None, ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _state_requires_honest_engine_evidence(self) -> "RuntimeInspectionEvidence":
        identity = self.runtime_identity
        identity_fields = (identity, self.runtime_identity_sha256)
        terminal_fields = (self.exit_code, self.ended_at, self.ended_monotonic_ns)
        absence_fields = (
            self.prelaunch_absence_journal_sha256,
            self.prelaunch_absence_epoch,
            self.prelaunch_authorization_request_sha256,
            self.prelaunch_authorization_sha256,
        )
        if any(item is None for item in identity_fields) != all(
            item is None for item in identity_fields
        ):
            raise ValueError("runtime inspection identity and hash must be paired")
        if (
            identity is not None
            and self.runtime_identity_sha256 != identity.runtime_identity_sha256
        ):
            raise ValueError("runtime inspection changed its exact runtime identity")
        if self.state is RuntimeInspectionState.RUNNING:
            if (
                identity is None
                or any(item is not None for item in terminal_fields)
                or any(item is not None for item in absence_fields)
                or self.engine_terminal_journal_sha256 is not None
            ):
                raise ValueError("running inspection has impossible terminal or absence evidence")
        elif self.state is RuntimeInspectionState.TERMINATED:
            if (
                identity is None
                or any(item is None for item in terminal_fields)
                or self.engine_terminal_journal_sha256 is None
                or any(item is not None for item in absence_fields)
            ):
                raise ValueError(
                    "terminated inspection requires identity, exit, and engine terminal journal"
                )
        elif self.state is RuntimeInspectionState.ABSENT:
            if (
                identity is not None
                or any(item is not None for item in terminal_fields)
                or self.prelaunch_absence_journal_sha256 is None
                or self.prelaunch_absence_epoch is None
                or self.engine_terminal_journal_sha256 is not None
            ):
                raise ValueError(
                    "absent inspection is only a journal-backed exact prelaunch absence proof"
                )
            if (self.prelaunch_authorization_request_sha256 is None) != (
                self.prelaunch_authorization_sha256 is None
            ) or (
                self.prelaunch_authorization_request_sha256 is None
                and self.prelaunch_absence_epoch != 1
            ):
                raise ValueError(
                    "cleaned prelaunch absence must bind its prior authorization pair and epoch"
                )
        elif self.state is RuntimeInspectionState.UNKNOWN:
            if (
                any(item is not None for item in terminal_fields)
                or any(item is not None for item in absence_fields)
                or self.engine_terminal_journal_sha256 is not None
            ):
                raise ValueError("unknown runtime cannot claim absence or terminal evidence")
        if identity is not None and (
            self.inspected_at < identity.started_at
            or self.inspected_monotonic_ns < identity.started_monotonic_ns
        ):
            raise ValueError("runtime inspection predates its actual runtime identity")
        if self.ended_at is not None:
            assert identity is not None and self.ended_monotonic_ns is not None
            if not (
                identity.started_at <= self.ended_at <= self.inspected_at
                and identity.started_monotonic_ns
                <= self.ended_monotonic_ns
                <= self.inspected_monotonic_ns
            ):
                raise ValueError("engine terminal evidence is out of order")
        return self

    @property
    def inspection_sha256(self) -> str:
        return canonical_sha256(self)


def _validate_runtime_inspection_evidence_refresh(
    *,
    previous: RuntimeInspectionEvidence,
    refreshed: RuntimeInspectionEvidence,
    expected_state: RuntimeInspectionState,
) -> None:
    """Require a new observation of the same immutable engine fact generation.

    Refresh is deliberately narrower than replacement: only the per-inspection evidence hash and
    the two observation clocks may advance.  Runtime identity, custody, fence, journal, terminal,
    tombstone, and prior-authorization facts remain byte-exact.
    """

    try:
        previous = RuntimeInspectionEvidence.model_validate(previous.model_dump(mode="python"))
        refreshed = RuntimeInspectionEvidence.model_validate(refreshed.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "runtime inspection refresh failed closed revalidation"
        ) from exc
    changing_fields = {
        "inspection_evidence_sha256",
        "inspected_at",
        "inspected_monotonic_ns",
    }
    if (
        previous.state is not expected_state
        or refreshed.state is not expected_state
        or refreshed.inspected_at <= previous.inspected_at
        or refreshed.inspected_monotonic_ns <= previous.inspected_monotonic_ns
        or refreshed.inspection_evidence_sha256 == previous.inspection_evidence_sha256
        or previous.model_dump(mode="json", exclude=changing_fields)
        != refreshed.model_dump(mode="json", exclude=changing_fields)
    ):
        raise QualificationVerificationError(
            "runtime inspection refresh changed immutable engine facts or did not advance"
        )


def validate_runtime_terminal_evidence_refresh(
    *, previous: RuntimeInspectionEvidence, refreshed: RuntimeInspectionEvidence
) -> None:
    """Validate a fresh inspection of one exact already-terminated runtime."""

    _validate_runtime_inspection_evidence_refresh(
        previous=previous,
        refreshed=refreshed,
        expected_state=RuntimeInspectionState.TERMINATED,
    )


def validate_pre_runtime_absence_evidence_refresh(
    *, previous: RuntimeInspectionEvidence, refreshed: RuntimeInspectionEvidence
) -> None:
    """Validate one exact pre-workload tombstone and absence epoch."""

    _validate_runtime_inspection_evidence_refresh(
        previous=previous,
        refreshed=refreshed,
        expected_state=RuntimeInspectionState.ABSENT,
    )


class PreRuntimeAbsenceReceipt(ExecutionModel):
    """Node-signed proof that one exact preparation never started its authorized workload.

    The OCI container is normally absent or CREATED/PID0.  A deployment adapter may also bind a
    narrower, independently quiesced launch-gate rejection that proves the gate could not have
    execed the workload.  This receipt never stands for process termination or engineering output.
    """

    schema_name: Literal["aletheia.pre_runtime_absence_receipt"] = (
        "aletheia.pre_runtime_absence_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation: RuntimePreparation
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    absence_evidence: RuntimeInspectionEvidence
    absence_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    expires_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    cleanup_recovery_authority: AttemptScopedPreRuntimeCleanupAuthorityPin | None = None
    cleanup_recovery_authority_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_exact_never_started_proof(self) -> "PreRuntimeAbsenceReceipt":
        evidence = self.absence_evidence
        recovery = self.cleanup_recovery_authority
        if (
            self.preparation_sha256 != self.preparation.preparation_sha256
            or self.absence_evidence_sha256 != evidence.inspection_sha256
            or evidence.preparation_sha256 != self.preparation_sha256
            or evidence.state is not RuntimeInspectionState.ABSENT
            or evidence.enforced_placement_sha256 != self.preparation.enforced_placement_sha256
            or evidence.input_materialization_receipt_sha256
            != self.preparation.input_materialization_receipt_sha256
            or evidence.enforced_fencing_epoch != self.preparation.fencing_epoch
            or evidence.enforced_lease_token_sha256 != self.preparation.lease_token_sha256
            or self.signed_at < evidence.inspected_at
            or self.signed_at - evidence.inspected_at > _MAX_NODE_PROOF_SIGNING_LAG
            or self.expires_at <= self.signed_at
        ):
            raise ValueError("pre-runtime absence receipt changed its exact pre-workload proof")
        if (recovery is None) != (self.cleanup_recovery_authority_sha256 is None):
            raise ValueError("pre-runtime absence recovery authority is incomplete")
        if recovery is not None and (
            self.cleanup_recovery_authority_sha256 != recovery.authority_sha256
            or self.signing_key_id != recovery.key_id
            or self.node_manifest_sha256 != recovery.source_node_manifest_sha256
            or self.preparation.node_id != recovery.source_node_id
            or self.preparation.infrastructure_attempt_id != recovery.infrastructure_attempt_id
            or self.preparation_sha256 != recovery.runtime_preparation_sha256
            or evidence.prelaunch_authorization_sha256
            != recovery.runtime_launch_authorization_sha256
            or evidence.prelaunch_absence_epoch != recovery.cleanup_absence_epoch
            or not recovery.active_at(self.signed_at)
            or self.expires_at > recovery.active_until
        ):
            raise ValueError("pre-runtime absence changed its attempt-scoped recovery authority")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def absence_receipt_sha256(self) -> str:
        return canonical_sha256(self)


def _validate_pre_runtime_absence_launch_lineage(
    *,
    evidence: RuntimeInspectionEvidence,
    preparation: RuntimePreparation,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest | None,
    launch_authorization: RuntimeLaunchAuthorization | None,
    runtime_authority: RuntimeControlAuthorityVerifier | None,
) -> None:
    epoch = evidence.prelaunch_absence_epoch
    if epoch is None:
        raise QualificationVerificationError("pre-runtime absence omitted its exact epoch")
    if evidence.prelaunch_authorization_request_sha256 is None:
        if (
            epoch != 1
            or launch_authorization_request is not None
            or launch_authorization is not None
            or runtime_authority is not None
            or evidence.prelaunch_authorization_sha256 is not None
        ):
            raise QualificationVerificationError(
                "initial pre-runtime absence cannot claim a launch authorization"
            )
        return
    if (
        launch_authorization_request is None
        or launch_authorization is None
        or runtime_authority is None
        or evidence.prelaunch_authorization_request_sha256
        != launch_authorization_request.request_sha256
        or evidence.prelaunch_authorization_sha256 != launch_authorization.authorization_sha256
        or epoch != launch_authorization_request.pre_runtime_absence_epoch + 1
    ):
        raise QualificationVerificationError(
            "cleaned pre-runtime absence differs from its prior launch authorization epoch"
        )
    verify_runtime_launch_authorization_ticket_historical(
        authorization=launch_authorization,
        authorization_request=launch_authorization_request,
        preparation=preparation,
        authority=runtime_authority,
    )


def issue_pre_runtime_absence_receipt(
    *,
    manifest: WorkerNodeManifest,
    preparation: RuntimePreparation,
    absence_evidence: RuntimeInspectionEvidence,
    signed_at: datetime,
    expires_at: datetime,
    private_key: bytes,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest | None = None,
    launch_authorization: RuntimeLaunchAuthorization | None = None,
    runtime_authority: RuntimeControlAuthorityVerifier | None = None,
) -> PreRuntimeAbsenceReceipt:
    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    evidence = RuntimeInspectionEvidence.model_validate(absence_evidence.model_dump(mode="python"))
    _require_utc(signed_at, "pre-runtime absence signed_at")
    _require_utc(expires_at, "pre-runtime absence expires_at")
    _validate_pre_runtime_absence_launch_lineage(
        evidence=evidence,
        preparation=preparation,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        runtime_authority=runtime_authority,
    )
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if (
        _public_key_hex(private_key) != manifest.node_signing_public_key_ed25519_hex
        or preparation.node_manifest_sha256 != manifest.manifest_sha256
        or evidence.preparation_sha256 != preparation.preparation_sha256
        or evidence.state is not RuntimeInspectionState.ABSENT
        or not evidence.inspected_at <= signed_at < expires_at <= active_until
    ):
        raise QualificationVerificationError(
            "pre-runtime absence signer or evidence differs from deployment authority"
        )
    unsigned = PreRuntimeAbsenceReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        preparation=preparation,
        preparation_sha256=preparation.preparation_sha256,
        absence_evidence=evidence,
        absence_evidence_sha256=evidence.inspection_sha256,
        signed_at=signed_at,
        expires_at=expires_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return PreRuntimeAbsenceReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def issue_attempt_scoped_pre_runtime_cleanup_receipt(
    *,
    authority_pin: AttemptScopedPreRuntimeCleanupAuthorityPin,
    preparation: RuntimePreparation,
    absence_evidence: RuntimeInspectionEvidence,
    signed_at: datetime,
    expires_at: datetime,
    private_key: bytes,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> PreRuntimeAbsenceReceipt:
    """Sign one fresh absence after source-node expiry without reviving node authority."""

    pin = AttemptScopedPreRuntimeCleanupAuthorityPin.model_validate(
        authority_pin.model_dump(mode="python")
    )
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    evidence = RuntimeInspectionEvidence.model_validate(absence_evidence.model_dump(mode="python"))
    request = RuntimeLaunchAuthorizationRequest.model_validate(
        launch_authorization_request.model_dump(mode="python")
    )
    authorization = RuntimeLaunchAuthorization.model_validate(
        launch_authorization.model_dump(mode="python")
    )
    _require_utc(signed_at, "attempt-scoped pre-runtime cleanup signed_at")
    _require_utc(expires_at, "attempt-scoped pre-runtime cleanup expires_at")
    _validate_pre_runtime_absence_launch_lineage(
        evidence=evidence,
        preparation=preparation,
        launch_authorization_request=request,
        launch_authorization=authorization,
        runtime_authority=runtime_authority,
    )
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or preparation.node_id != pin.source_node_id
        or preparation.node_manifest_sha256 != pin.source_node_manifest_sha256
        or preparation.infrastructure_attempt_id != pin.infrastructure_attempt_id
        or preparation.preparation_sha256 != pin.runtime_preparation_sha256
        or authorization.authorization_sha256 != pin.runtime_launch_authorization_sha256
        or evidence.prelaunch_absence_epoch != pin.cleanup_absence_epoch
        or evidence.prelaunch_authorization_sha256 != authorization.authorization_sha256
        or not evidence.inspected_at <= signed_at < expires_at <= pin.active_until
        or not pin.active_at(signed_at)
    ):
        raise QualificationVerificationError(
            "pre-runtime cleanup evidence differs from attempt-scoped recovery authority"
        )
    unsigned = PreRuntimeAbsenceReceipt(
        node_manifest_sha256=preparation.node_manifest_sha256,
        preparation=preparation,
        preparation_sha256=preparation.preparation_sha256,
        absence_evidence=evidence,
        absence_evidence_sha256=evidence.inspection_sha256,
        signed_at=signed_at,
        expires_at=expires_at,
        signing_key_id=pin.key_id,
        signature_ed25519_hex="0" * 128,
        cleanup_recovery_authority=pin,
        cleanup_recovery_authority_sha256=pin.authority_sha256,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return PreRuntimeAbsenceReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


class VerifiedPreRuntimeAbsence(ExecutionModel):
    absence_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    absence_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    verified_at: AwareDatetime
    cleanup_recovery_authority_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def verify_pre_runtime_absence_receipt(
    *,
    receipt: PreRuntimeAbsenceReceipt,
    preparation: RuntimePreparation,
    authority: WorkerNodeAuthorityVerifier,
    observed_at: datetime,
    maximum_age_seconds: int,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest | None = None,
    launch_authorization: RuntimeLaunchAuthorization | None = None,
    runtime_authority: RuntimeControlAuthorityVerifier | None = None,
    cleanup_recovery_authority: (AttemptScopedPreRuntimeCleanupAuthorityVerifier | None) = None,
) -> VerifiedPreRuntimeAbsence:
    """Verify a fresh pre-workload proof; it is not a process termination receipt."""

    _require_utc(observed_at, "pre-runtime absence observed_at")
    if maximum_age_seconds < 1:
        raise QualificationVerificationError("pre-runtime absence maximum age must be positive")
    try:
        receipt = PreRuntimeAbsenceReceipt.model_validate(receipt.model_dump(mode="python"))
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        if (
            receipt.preparation != preparation
            or preparation.node_manifest_sha256 != authority.manifest.manifest_sha256
            or not receipt.signed_at <= observed_at < receipt.expires_at
            or observed_at - receipt.signed_at > timedelta(seconds=maximum_age_seconds)
            or observed_at - receipt.absence_evidence.inspected_at
            > timedelta(seconds=maximum_age_seconds)
        ):
            raise QualificationVerificationError(
                "pre-runtime absence is stale or belongs to another exact preparation"
            )
        _validate_pre_runtime_absence_launch_lineage(
            evidence=receipt.absence_evidence,
            preparation=preparation,
            launch_authorization_request=launch_authorization_request,
            launch_authorization=launch_authorization,
            runtime_authority=runtime_authority,
        )
        recovery_pin = receipt.cleanup_recovery_authority
        if recovery_pin is None:
            if cleanup_recovery_authority is not None:
                raise QualificationVerificationError(
                    "ordinary pre-runtime absence cannot select cleanup recovery authority"
                )
            if receipt.node_manifest_sha256 != authority.manifest.manifest_sha256:
                raise QualificationVerificationError(
                    "pre-runtime absence belongs to another enrolled node manifest"
                )
            authority.verify_signature(
                signing_key_id=receipt.signing_key_id,
                message=receipt.signature_message,
                signature_ed25519_hex=receipt.signature_ed25519_hex,
                signed_at=receipt.signed_at,
            )
        else:
            if (
                cleanup_recovery_authority is None
                or cleanup_recovery_authority.pin != recovery_pin
                or receipt.cleanup_recovery_authority_sha256 != recovery_pin.authority_sha256
                or recovery_pin.source_node_manifest_sha256 != authority.manifest.manifest_sha256
            ):
                raise QualificationVerificationError(
                    "pre-runtime absence recovery authority differs from deployment pin"
                )
            cleanup_recovery_authority.verify_signature(
                message=receipt.signature_message,
                signature_ed25519_hex=receipt.signature_ed25519_hex,
                signing_key_id=receipt.signing_key_id,
                signed_at=receipt.signed_at,
                expires_at=receipt.expires_at,
                observed_at=observed_at,
            )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "pre-runtime absence receipt failed closed revalidation"
        ) from exc
    return VerifiedPreRuntimeAbsence(
        absence_receipt_sha256=receipt.absence_receipt_sha256,
        preparation_sha256=preparation.preparation_sha256,
        absence_evidence_sha256=receipt.absence_evidence_sha256,
        execution_id=preparation.execution_id,
        infrastructure_attempt_id=preparation.infrastructure_attempt_id,
        verified_at=observed_at,
        cleanup_recovery_authority_sha256=(receipt.cleanup_recovery_authority_sha256),
    )


class RuntimeFenceRebindRequest(ExecutionModel):
    """One exact old-to-next fence CAS requested under a node-local singleton lock."""

    schema_name: Literal["aletheia.runtime_fence_rebind_request"] = (
        "aletheia.runtime_fence_rebind_request"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    previous_fencing_epoch: int = Field(ge=1)
    previous_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_fencing_epoch: int = Field(ge=2)
    new_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    rebind_sequence: int = Field(ge=1)
    expected_runtime_control_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    requested_at: AwareDatetime
    requested_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_one_next_fence(self) -> "RuntimeFenceRebindRequest":
        if (
            self.new_fencing_epoch != self.previous_fencing_epoch + 1
            or self.new_lease_token_sha256 == self.previous_lease_token_sha256
        ):
            raise ValueError("runtime fence rebind must rotate exactly one epoch and token")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class RuntimeFenceRebindEvidence(ExecutionModel):
    """Runtime-owned durable evidence that the exact fence sidecar CAS completed."""

    schema_name: Literal["aletheia.runtime_fence_rebind_evidence"] = (
        "aletheia.runtime_fence_rebind_evidence"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    previous_fencing_epoch: int = Field(ge=1)
    previous_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_fencing_epoch: int = Field(ge=2)
    new_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    rebind_sequence: int = Field(ge=1)
    previous_runtime_control_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    new_runtime_control_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    rebind_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    rebound_at: AwareDatetime
    rebound_monotonic_ns: int = Field(ge=0)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _evidence_is_one_completed_rotation(self) -> "RuntimeFenceRebindEvidence":
        if (
            self.new_fencing_epoch != self.previous_fencing_epoch + 1
            or self.new_lease_token_sha256 == self.previous_lease_token_sha256
            or self.new_runtime_control_journal_sha256
            == self.previous_runtime_control_journal_sha256
        ):
            raise ValueError("runtime fence evidence does not prove one completed CAS")
        return self

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


def validate_runtime_fence_rebind_evidence(
    *, request: RuntimeFenceRebindRequest, evidence: RuntimeFenceRebindEvidence
) -> None:
    request = RuntimeFenceRebindRequest.model_validate(request.model_dump(mode="python"))
    evidence = RuntimeFenceRebindEvidence.model_validate(evidence.model_dump(mode="python"))
    if (
        evidence.request_sha256 != request.request_sha256
        or evidence.preparation_sha256 != request.preparation_sha256
        or evidence.runtime_identity_sha256 != request.runtime_identity_sha256
        or evidence.previous_fencing_epoch != request.previous_fencing_epoch
        or evidence.previous_lease_token_sha256 != request.previous_lease_token_sha256
        or evidence.new_fencing_epoch != request.new_fencing_epoch
        or evidence.new_lease_token_sha256 != request.new_lease_token_sha256
        or evidence.rebind_sequence != request.rebind_sequence
        or evidence.previous_runtime_control_journal_sha256
        != request.expected_runtime_control_journal_sha256
        or evidence.rebound_at < request.requested_at
        or evidence.rebound_monotonic_ns < request.requested_monotonic_ns
    ):
        raise QualificationVerificationError(
            "runtime fence rebind evidence differs from its exact CAS request"
        )


class RuntimeFenceRebindReceipt(ExecutionModel):
    """Node-signed acknowledgement emitted only after the runtime CAS journal is durable."""

    schema_name: Literal["aletheia.runtime_fence_rebind_receipt"] = (
        "aletheia.runtime_fence_rebind_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence: RuntimeFenceRebindEvidence
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_binds_exact_rebind(self) -> "RuntimeFenceRebindReceipt":
        if (
            self.evidence_sha256 != self.evidence.evidence_sha256
            or self.signed_at < self.evidence.rebound_at
            or self.signed_at - self.evidence.rebound_at > _MAX_NODE_PROOF_SIGNING_LAG
        ):
            raise ValueError("runtime fence receipt changed or predates its evidence")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def rebind_receipt_sha256(self) -> str:
        return canonical_sha256(self)


def issue_runtime_fence_rebind_receipt(
    *,
    manifest: WorkerNodeManifest,
    request: RuntimeFenceRebindRequest,
    evidence: RuntimeFenceRebindEvidence,
    signed_at: datetime,
    private_key: bytes,
) -> RuntimeFenceRebindReceipt:
    manifest = WorkerNodeManifest.model_validate(manifest.model_dump(mode="python"))
    request = RuntimeFenceRebindRequest.model_validate(request.model_dump(mode="python"))
    evidence = RuntimeFenceRebindEvidence.model_validate(evidence.model_dump(mode="python"))
    _require_utc(signed_at, "runtime fence rebind signed_at")
    validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
    active_until = min(
        manifest.key_expires_at,
        manifest.key_revoked_at or manifest.key_expires_at,
    )
    if (
        _public_key_hex(private_key) != manifest.node_signing_public_key_ed25519_hex
        or not evidence.rebound_at <= signed_at < active_until
    ):
        raise QualificationVerificationError("runtime fence rebind signer is inactive or foreign")
    unsigned = RuntimeFenceRebindReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        evidence=evidence,
        evidence_sha256=evidence.evidence_sha256,
        signed_at=signed_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return RuntimeFenceRebindReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


class VerifiedRuntimeFenceRebind(ExecutionModel):
    rebind_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    previous_fencing_epoch: int = Field(ge=1)
    new_fencing_epoch: int = Field(ge=2)
    new_lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def verify_runtime_fence_rebind_receipt(
    *,
    receipt: RuntimeFenceRebindReceipt,
    request: RuntimeFenceRebindRequest,
    authority: WorkerNodeAuthorityVerifier,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> VerifiedRuntimeFenceRebind:
    _require_utc(observed_at, "runtime fence rebind observed_at")
    if maximum_age_seconds < 1:
        raise QualificationVerificationError("runtime fence rebind maximum age must be positive")
    try:
        receipt = RuntimeFenceRebindReceipt.model_validate(receipt.model_dump(mode="python"))
        request = RuntimeFenceRebindRequest.model_validate(request.model_dump(mode="python"))
        validate_runtime_fence_rebind_evidence(request=request, evidence=receipt.evidence)
        if (
            receipt.node_manifest_sha256 != authority.manifest.manifest_sha256
            or not receipt.signed_at <= observed_at
            or observed_at - receipt.signed_at > timedelta(seconds=maximum_age_seconds)
            or observed_at - receipt.evidence.rebound_at > timedelta(seconds=maximum_age_seconds)
        ):
            raise QualificationVerificationError(
                "runtime fence rebind receipt is stale or belongs to another node"
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
            "runtime fence rebind receipt failed closed revalidation"
        ) from exc
    evidence = receipt.evidence
    return VerifiedRuntimeFenceRebind(
        rebind_receipt_sha256=receipt.rebind_receipt_sha256,
        request_sha256=request.request_sha256,
        preparation_sha256=evidence.preparation_sha256,
        runtime_identity_sha256=evidence.runtime_identity_sha256,
        previous_fencing_epoch=evidence.previous_fencing_epoch,
        new_fencing_epoch=evidence.new_fencing_epoch,
        new_lease_token_sha256=evidence.new_lease_token_sha256,
        verified_at=observed_at,
    )


class HistoricalRuntimeRecoveryGrant(ExecutionModel):
    """Allocator-authenticated historical authority that can recover but never launch work."""

    schema_name: Literal["aletheia.historical_runtime_recovery_grant"] = (
        "aletheia.historical_runtime_recovery_grant"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    admitted_at: AwareDatetime
    hard_deadline: AwareDatetime
    issued_at: AwareDatetime
    recovery_expires_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    recovery_only: Literal[True] = True
    launch_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _historical_authority_is_bounded(self) -> "HistoricalRuntimeRecoveryGrant":
        if not (
            self.admitted_at < self.hard_deadline < self.recovery_expires_at
            and self.issued_at < self.recovery_expires_at
        ):
            raise ValueError("historical runtime recovery interval is invalid")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="historical_runtime_recovery_grant", payload=self.signature_payload
        )

    @property
    def recovery_grant_sha256(self) -> str:
        return canonical_sha256(self)


def issue_historical_runtime_recovery_grant(
    *, pin: RuntimeControlAuthorityPin, private_key: bytes, **scope: object
) -> HistoricalRuntimeRecoveryGrant:
    try:
        unsigned = HistoricalRuntimeRecoveryGrant(
            **scope,
            runtime_control_policy_sha256=pin.policy_sha256,
            authorized_by_principal_id=pin.principal_id,
            authorization_key_id=pin.key_id,
            signature_ed25519_hex="0" * 128,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "historical runtime recovery scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or not pin.active_at(unsigned.issued_at)
        or unsigned.recovery_expires_at > pin.active_until
    ):
        raise QualificationVerificationError("historical runtime recovery signer is inactive")
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return HistoricalRuntimeRecoveryGrant.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_historical_runtime_recovery_grant(
    *,
    grant: HistoricalRuntimeRecoveryGrant,
    authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
) -> None:
    grant = HistoricalRuntimeRecoveryGrant.model_validate(grant.model_dump(mode="python"))
    authority.verify(
        kind="historical_runtime_recovery_grant",
        payload=grant.signature_payload,
        signature_ed25519_hex=grant.signature_ed25519_hex,
        policy_sha256=grant.runtime_control_policy_sha256,
        principal_id=grant.authorized_by_principal_id,
        key_id=grant.authorization_key_id,
        signed_at=grant.issued_at,
        expires_at=grant.recovery_expires_at,
        observed_at=observed_at,
    )


class RuntimeTerminationAcceptanceChallenge(ExecutionModel):
    """DB-signed, short-lived challenge for one exact full terminal observation."""

    schema_name: Literal["aletheia.runtime_termination_acceptance_challenge"] = (
        "aletheia.runtime_termination_acceptance_challenge"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    challenge_id: str = Field(pattern=_SHA256_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    challenged_at: AwareDatetime
    expires_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenged_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    challenge_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _challenge_window_is_bounded(self) -> "RuntimeTerminationAcceptanceChallenge":
        if (
            self.expires_at <= self.challenged_at
            or self.artifact_submission_deadline <= self.hard_deadline
        ):
            raise ValueError("runtime termination challenge expiry must follow DB challenge time")
        expected_id = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"challenge_id", "signature_ed25519_hex"},
            )
        )
        if self.challenge_id != expected_id:
            raise ValueError("runtime termination challenge id differs from exact DB scope")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="runtime_termination_acceptance_challenge",
            payload=self.signature_payload,
        )

    @property
    def challenge_sha256(self) -> str:
        return canonical_sha256(self)


def issue_runtime_termination_acceptance_challenge(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    **scope: object,
) -> RuntimeTerminationAcceptanceChallenge:
    """Issue a DB challenge; arbitrary callers cannot construct trusted authority."""

    authority_scope = {
        **scope,
        "runtime_control_policy_sha256": pin.policy_sha256,
        "challenged_by_principal_id": pin.principal_id,
        "challenge_key_id": pin.key_id,
    }
    provisional = RuntimeTerminationAcceptanceChallenge.model_construct(
        challenge_id="0" * 64,
        signature_ed25519_hex="0" * 128,
        **authority_scope,
    )
    challenge_id = canonical_sha256(
        provisional.model_dump(mode="json", exclude={"challenge_id", "signature_ed25519_hex"})
    )
    try:
        unsigned = RuntimeTerminationAcceptanceChallenge(
            challenge_id=challenge_id,
            signature_ed25519_hex="0" * 128,
            **authority_scope,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "runtime termination challenge scope is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or not pin.active_at(unsigned.challenged_at)
        or unsigned.expires_at > pin.active_until
        or unsigned.artifact_submission_deadline > pin.active_until
    ):
        raise QualificationVerificationError("runtime termination challenge signer is inactive")
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return RuntimeTerminationAcceptanceChallenge.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_runtime_termination_acceptance_challenge(
    *,
    challenge: RuntimeTerminationAcceptanceChallenge,
    authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
) -> None:
    challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    authority.verify(
        kind="runtime_termination_acceptance_challenge",
        payload=challenge.signature_payload,
        signature_ed25519_hex=challenge.signature_ed25519_hex,
        policy_sha256=challenge.runtime_control_policy_sha256,
        principal_id=challenge.challenged_by_principal_id,
        key_id=challenge.challenge_key_id,
        signed_at=challenge.challenged_at,
        expires_at=challenge.expires_at,
        observed_at=observed_at,
    )


class NodeRuntimeTerminationReceipt(ExecutionModel):
    """Node signature over the complete v2 terminal evidence and DB challenge."""

    schema_name: Literal["aletheia.node_runtime_termination_receipt"] = (
        "aletheia.node_runtime_termination_receipt"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    termination_evidence: RuntimeInspectionEvidence
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    expires_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_binds_full_terminal_evidence(self) -> "NodeRuntimeTerminationReceipt":
        if (
            self.termination_evidence.state is not RuntimeInspectionState.TERMINATED
            or self.termination_evidence_sha256 != self.termination_evidence.inspection_sha256
            or self.termination_evidence.preparation_sha256 != self.runtime_preparation_sha256
            or self.signed_at < self.termination_evidence.inspected_at
            or self.signed_at - self.termination_evidence.inspected_at > _MAX_NODE_PROOF_SIGNING_LAG
            or self.expires_at <= self.signed_at
        ):
            raise ValueError("node termination receipt changed or omitted full terminal evidence")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def termination_receipt_sha256(self) -> str:
        return canonical_sha256(self)


def _validate_terminal_scope(
    *,
    challenge: RuntimeTerminationAcceptanceChallenge,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    evidence: RuntimeInspectionEvidence,
) -> None:
    identity = evidence.runtime_identity
    launch_identity = launch_receipt.launch_evidence.runtime_identity
    if (
        identity is None
        or challenge.attempt_id != preparation.infrastructure_attempt_id
        or challenge.execution_id != preparation.execution_id
        or challenge.intent_sha256 != preparation.intent_sha256
        or challenge.node_manifest_sha256 != preparation.node_manifest_sha256
        or challenge.runtime_preparation_sha256 != preparation.preparation_sha256
        or challenge.node_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
        or challenge.runtime_identity_sha256 != identity.runtime_identity_sha256
        or challenge.runtime_inspection_evidence_sha256 != evidence.inspection_sha256
        or evidence.state is not RuntimeInspectionState.TERMINATED
        or evidence.preparation_sha256 != preparation.preparation_sha256
        or evidence.runtime_identity != launch_identity
        or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
        or evidence.input_materialization_receipt_sha256
        != preparation.input_materialization_receipt_sha256
        or evidence.enforced_fencing_epoch != challenge.fencing_epoch
        or evidence.enforced_lease_token_sha256 != challenge.lease_token_sha256
    ):
        raise QualificationVerificationError(
            "runtime termination evidence differs from challenge/preparation/launch lineage"
        )


def issue_node_runtime_termination_receipt(
    *,
    challenge: RuntimeTerminationAcceptanceChallenge,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    termination_evidence: RuntimeInspectionEvidence,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    signed_at: datetime,
    expires_at: datetime,
    private_key: bytes,
) -> NodeRuntimeTerminationReceipt:
    """Verify DB authority and sign the full canonical v2 terminal object."""

    challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
        launch_receipt.model_dump(mode="python")
    )
    evidence = RuntimeInspectionEvidence.model_validate(
        termination_evidence.model_dump(mode="python")
    )
    _require_utc(signed_at, "node runtime termination signed_at")
    _require_utc(expires_at, "node runtime termination expires_at")
    verify_runtime_termination_acceptance_challenge(
        challenge=challenge,
        authority=runtime_authority,
        observed_at=signed_at,
    )
    verify_node_runtime_launch_receipt_historical(
        receipt=launch_receipt,
        preparation=preparation,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        authority=node_authority,
        runtime_authority=runtime_authority,
    )
    _validate_terminal_scope(
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        evidence=evidence,
    )
    manifest = node_authority.manifest
    try:
        node_authority.verify_signature(
            signing_key_id=launch_receipt.signing_key_id,
            message=launch_receipt.signature_message,
            signature_ed25519_hex=launch_receipt.signature_ed25519_hex,
            signed_at=launch_receipt.signed_at,
        )
    except QualificationVerificationError as exc:
        raise QualificationVerificationError(
            "terminal proof launch receipt signature is invalid"
        ) from exc
    if (
        _public_key_hex(private_key) != manifest.node_signing_public_key_ed25519_hex
        or preparation.node_manifest_sha256 != manifest.manifest_sha256
        or launch_receipt.node_manifest_sha256 != manifest.manifest_sha256
        or not evidence.inspected_at <= signed_at < expires_at
        or expires_at > challenge.expires_at
        or expires_at > node_authority.active_until
    ):
        raise QualificationVerificationError(
            "node runtime termination signer, window, or node lineage differs"
        )
    unsigned = NodeRuntimeTerminationReceipt(
        node_manifest_sha256=manifest.manifest_sha256,
        challenge_sha256=challenge.challenge_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_launch_authorization_request_sha256=(launch_authorization_request.request_sha256),
        runtime_launch_authorization_sha256=(launch_authorization.authorization_sha256),
        inspection_sequence=challenge.inspection_sequence,
        termination_evidence=evidence,
        termination_evidence_sha256=evidence.inspection_sha256,
        signed_at=signed_at,
        expires_at=expires_at,
        signing_key_id=manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return NodeRuntimeTerminationReceipt.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


class VerifiedNodeRuntimeTermination(ExecutionModel):
    termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    termination_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    exit_code: int = Field(ge=-255, le=255)
    ended_at: AwareDatetime
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def verify_node_runtime_termination_receipt(
    *,
    receipt: NodeRuntimeTerminationReceipt,
    challenge: RuntimeTerminationAcceptanceChallenge,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> VerifiedNodeRuntimeTermination:
    _require_utc(observed_at, "node runtime termination observed_at")
    if maximum_age_seconds < 1:
        raise QualificationVerificationError("termination maximum age must be positive")
    try:
        receipt = NodeRuntimeTerminationReceipt.model_validate(receipt.model_dump(mode="python"))
        challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
            challenge.model_dump(mode="python")
        )
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
            launch_receipt.model_dump(mode="python")
        )
        verify_runtime_termination_acceptance_challenge(
            challenge=challenge,
            authority=runtime_authority,
            observed_at=observed_at,
        )
        verify_node_runtime_launch_receipt_historical(
            receipt=launch_receipt,
            preparation=preparation,
            launch_authorization_request=launch_authorization_request,
            launch_authorization=launch_authorization,
            authority=node_authority,
            runtime_authority=runtime_authority,
        )
        _validate_terminal_scope(
            challenge=challenge,
            preparation=preparation,
            launch_receipt=launch_receipt,
            evidence=receipt.termination_evidence,
        )
        if (
            receipt.node_manifest_sha256 != node_authority.manifest.manifest_sha256
            or receipt.challenge_sha256 != challenge.challenge_sha256
            or receipt.runtime_preparation_sha256 != preparation.preparation_sha256
            or receipt.node_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
            or receipt.runtime_launch_authorization_request_sha256
            != launch_authorization_request.request_sha256
            or receipt.runtime_launch_authorization_sha256
            != launch_authorization.authorization_sha256
            or receipt.inspection_sequence != challenge.inspection_sequence
            or not receipt.signed_at <= observed_at < receipt.expires_at
            or observed_at - receipt.signed_at > timedelta(seconds=maximum_age_seconds)
            or observed_at - receipt.termination_evidence.inspected_at
            > timedelta(seconds=maximum_age_seconds)
        ):
            raise QualificationVerificationError(
                "node runtime termination proof is stale or rebound"
            )
        node_authority.verify_signature(
            signing_key_id=launch_receipt.signing_key_id,
            message=launch_receipt.signature_message,
            signature_ed25519_hex=launch_receipt.signature_ed25519_hex,
            signed_at=launch_receipt.signed_at,
        )
        node_authority.verify_signature(
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.signed_at,
        )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "node runtime termination receipt failed closed revalidation"
        ) from exc
    evidence = receipt.termination_evidence
    assert evidence.runtime_identity_sha256 is not None
    assert evidence.exit_code is not None and evidence.ended_at is not None
    return VerifiedNodeRuntimeTermination(
        termination_receipt_sha256=receipt.termination_receipt_sha256,
        challenge_sha256=challenge.challenge_sha256,
        runtime_preparation_sha256=preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        termination_evidence_sha256=evidence.inspection_sha256,
        inspection_sequence=receipt.inspection_sequence,
        runtime_identity_sha256=evidence.runtime_identity_sha256,
        exit_code=evidence.exit_code,
        ended_at=evidence.ended_at,
        verified_at=observed_at,
    )


def verify_node_runtime_termination_receipt_historical(
    *,
    receipt: NodeRuntimeTerminationReceipt,
    challenge: RuntimeTerminationAcceptanceChallenge,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Verify a complete persisted terminal proof without reviving its expired authority.

    This verifier is suitable only for recovering an acceptance which the trusted allocator says
    already exists.  It verifies historical signatures and exact lineage, but intentionally does
    not make the old challenge usable to create a new acceptance.
    """

    try:
        receipt = NodeRuntimeTerminationReceipt.model_validate(receipt.model_dump(mode="python"))
        challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
            challenge.model_dump(mode="python")
        )
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
            launch_receipt.model_dump(mode="python")
        )
        _validate_terminal_scope(
            challenge=challenge,
            preparation=preparation,
            launch_receipt=launch_receipt,
            evidence=receipt.termination_evidence,
        )
        verify_node_runtime_launch_receipt_historical(
            receipt=launch_receipt,
            preparation=preparation,
            launch_authorization_request=launch_authorization_request,
            launch_authorization=launch_authorization,
            authority=node_authority,
            runtime_authority=runtime_authority,
        )
        runtime_authority.verify_historical(
            kind="runtime_termination_acceptance_challenge",
            payload=challenge.signature_payload,
            signature_ed25519_hex=challenge.signature_ed25519_hex,
            policy_sha256=challenge.runtime_control_policy_sha256,
            principal_id=challenge.challenged_by_principal_id,
            key_id=challenge.challenge_key_id,
            signed_at=challenge.challenged_at,
        )
        node_authority.verify_signature(
            signing_key_id=receipt.signing_key_id,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
            signed_at=receipt.signed_at,
        )
        evidence = receipt.termination_evidence
        if (
            receipt.node_manifest_sha256 != node_authority.manifest.manifest_sha256
            or receipt.challenge_sha256 != challenge.challenge_sha256
            or receipt.runtime_preparation_sha256 != preparation.preparation_sha256
            or receipt.node_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
            or receipt.runtime_launch_authorization_request_sha256
            != launch_authorization_request.request_sha256
            or receipt.runtime_launch_authorization_sha256
            != launch_authorization.authorization_sha256
            or receipt.inspection_sequence != challenge.inspection_sequence
            or receipt.termination_evidence_sha256 != evidence.inspection_sha256
            or not evidence.inspected_at <= receipt.signed_at < receipt.expires_at
            or receipt.expires_at > challenge.expires_at
            or receipt.expires_at > node_authority.active_until
        ):
            raise QualificationVerificationError(
                "historical node runtime termination proof is rebound or incomplete"
            )
    except QualificationVerificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationVerificationError(
            "historical node runtime termination proof failed closed revalidation"
        ) from exc


class AcceptedRuntimeTermination(ExecutionModel):
    """Immutable DB acceptance of a fresh engine proof, before artifact quarantine.

    It deliberately cannot bind a ``NodeExecutionReceipt`` or artifact manifest: those values do
    not exist until after this acceptance releases compute and output quarantine/rehash runs.  A
    later node-signed terminal submission binds independently rehashed artifacts to
    ``accepted_termination_sha256`` and receives a separate runtime-control acceptance.
    """

    schema_name: Literal["aletheia.accepted_runtime_termination"] = (
        "aletheia.accepted_runtime_termination"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_terminal_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_ended_at: AwareDatetime
    exit_code: int = Field(ge=-255, le=255)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    proof_signed_at: AwareDatetime
    proof_expires_at: AwareDatetime
    accepted_at: AwareDatetime
    billable_ended_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    acceptance_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    proof_was_fresh: Literal[True] = True
    compute_release_allowed: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    qualification_only: Literal[True] = True

    @model_validator(mode="after")
    def _acceptance_preserves_fresh_historical_order(self) -> "AcceptedRuntimeTermination":
        if not (
            self.runtime_ended_at
            <= self.proof_signed_at
            <= self.accepted_at
            < self.proof_expires_at
            and self.accepted_at < self.artifact_submission_deadline
            and self.hard_deadline < self.artifact_submission_deadline
            and self.runtime_ended_at <= self.billable_ended_at <= self.accepted_at
        ):
            raise ValueError("accepted runtime termination was not fresh or time ordered")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="accepted_runtime_termination", payload=self.signature_payload
        )

    @property
    def accepted_termination_sha256(self) -> str:
        return canonical_sha256(self)


def issue_accepted_runtime_termination(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    accepted_at: datetime,
    billable_ended_at: datetime,
    maximum_proof_age_seconds: int,
) -> AcceptedRuntimeTermination:
    """Persistable DB acceptance created while the complete node proof is still fresh."""

    verified = verify_node_runtime_termination_receipt(
        receipt=node_termination_receipt,
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        observed_at=accepted_at,
        maximum_age_seconds=maximum_proof_age_seconds,
    )
    evidence = node_termination_receipt.termination_evidence
    assert evidence.engine_terminal_journal_sha256 is not None
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or not pin.active_at(accepted_at)
        or runtime_authority.pin != pin
    ):
        raise QualificationVerificationError("runtime termination acceptance signer is inactive")
    unsigned = AcceptedRuntimeTermination(
        challenge_sha256=challenge.challenge_sha256,
        attempt_id=challenge.attempt_id,
        runtime_preparation_sha256=preparation.preparation_sha256,
        node_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
        runtime_launch_authorization_request_sha256=(launch_authorization_request.request_sha256),
        runtime_launch_authorization_sha256=(launch_authorization.authorization_sha256),
        node_runtime_termination_receipt_sha256=verified.termination_receipt_sha256,
        inspection_sequence=node_termination_receipt.inspection_sequence,
        runtime_identity_sha256=verified.runtime_identity_sha256,
        runtime_inspection_evidence_sha256=verified.termination_evidence_sha256,
        engine_terminal_journal_sha256=evidence.engine_terminal_journal_sha256,
        fencing_epoch=challenge.fencing_epoch,
        lease_token_sha256=challenge.lease_token_sha256,
        runtime_ended_at=verified.ended_at,
        exit_code=verified.exit_code,
        hard_deadline=challenge.hard_deadline,
        artifact_submission_deadline=challenge.artifact_submission_deadline,
        proof_signed_at=node_termination_receipt.signed_at,
        proof_expires_at=node_termination_receipt.expires_at,
        accepted_at=accepted_at,
        billable_ended_at=billable_ended_at,
        runtime_control_policy_sha256=pin.policy_sha256,
        accepted_by_principal_id=pin.principal_id,
        acceptance_key_id=pin.key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return AcceptedRuntimeTermination.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_accepted_runtime_termination(
    *,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> None:
    """Verify historical acceptance without requiring a fresh post-quarantine node proof."""

    accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
    challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    receipt = NodeRuntimeTerminationReceipt.model_validate(
        node_termination_receipt.model_dump(mode="python")
    )
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
        launch_receipt.model_dump(mode="python")
    )
    evidence = receipt.termination_evidence
    _validate_terminal_scope(
        challenge=challenge,
        preparation=preparation,
        launch_receipt=launch_receipt,
        evidence=evidence,
    )
    verify_node_runtime_launch_receipt_historical(
        receipt=launch_receipt,
        preparation=preparation,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        authority=node_authority,
        runtime_authority=runtime_authority,
    )
    runtime_authority.verify_historical(
        kind="runtime_termination_acceptance_challenge",
        payload=challenge.signature_payload,
        signature_ed25519_hex=challenge.signature_ed25519_hex,
        policy_sha256=challenge.runtime_control_policy_sha256,
        principal_id=challenge.challenged_by_principal_id,
        key_id=challenge.challenge_key_id,
        signed_at=challenge.challenged_at,
    )
    node_authority.verify_signature(
        signing_key_id=launch_receipt.signing_key_id,
        message=launch_receipt.signature_message,
        signature_ed25519_hex=launch_receipt.signature_ed25519_hex,
        signed_at=launch_receipt.signed_at,
    )
    node_authority.verify_signature(
        signing_key_id=receipt.signing_key_id,
        message=receipt.signature_message,
        signature_ed25519_hex=receipt.signature_ed25519_hex,
        signed_at=receipt.signed_at,
    )
    if (
        receipt.challenge_sha256 != challenge.challenge_sha256
        or receipt.inspection_sequence != challenge.inspection_sequence
        or receipt.runtime_launch_authorization_request_sha256
        != launch_authorization_request.request_sha256
        or receipt.runtime_launch_authorization_sha256 != launch_authorization.authorization_sha256
        or accepted.challenge_sha256 != challenge.challenge_sha256
        or accepted.attempt_id != challenge.attempt_id
        or accepted.runtime_preparation_sha256 != preparation.preparation_sha256
        or accepted.node_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
        or accepted.runtime_launch_authorization_request_sha256
        != launch_authorization_request.request_sha256
        or accepted.runtime_launch_authorization_sha256 != launch_authorization.authorization_sha256
        or accepted.node_runtime_termination_receipt_sha256 != receipt.termination_receipt_sha256
        or accepted.inspection_sequence != receipt.inspection_sequence
        or accepted.runtime_identity_sha256 != evidence.runtime_identity_sha256
        or accepted.runtime_inspection_evidence_sha256 != evidence.inspection_sha256
        or accepted.engine_terminal_journal_sha256 != evidence.engine_terminal_journal_sha256
        or accepted.fencing_epoch != challenge.fencing_epoch
        or accepted.lease_token_sha256 != challenge.lease_token_sha256
        or accepted.runtime_ended_at != evidence.ended_at
        or accepted.exit_code != evidence.exit_code
        or accepted.hard_deadline != challenge.hard_deadline
        or accepted.artifact_submission_deadline != challenge.artifact_submission_deadline
        or accepted.proof_signed_at != receipt.signed_at
        or accepted.proof_expires_at != receipt.expires_at
    ):
        raise QualificationVerificationError(
            "accepted runtime termination differs from exact historical proof"
        )
    runtime_authority.verify_historical(
        kind="accepted_runtime_termination",
        payload=accepted.signature_payload,
        signature_ed25519_hex=accepted.signature_ed25519_hex,
        policy_sha256=accepted.runtime_control_policy_sha256,
        principal_id=accepted.accepted_by_principal_id,
        key_id=accepted.acceptance_key_id,
        signed_at=accepted.accepted_at,
    )


class QualificationTerminalDeadlineExpiration(ExecutionModel):
    """Pre-signed conditional failure activated by DB time after artifact grace.

    This authority intentionally contains no artifact manifest or verification-receipt fields:
    their absence is the condition later adjudicated.  It is signed while the runtime-control
    pin is live, but cannot activate until DB time reaches the exact artifact deadline and the
    transaction proves that no terminal-submission acceptance exists.
    """

    schema_name: Literal["aletheia.qualification_terminal_deadline_expiration"] = (
        "aletheia.qualification_terminal_deadline_expiration"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_termination_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_runtime_termination_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_terminal_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspection_sequence: int = Field(ge=1)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_ended_at: AwareDatetime
    exit_code: int = Field(ge=-255, le=255)
    hard_deadline: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    accepted_runtime_termination_at: AwareDatetime
    authorized_at: AwareDatetime
    expired_at: AwareDatetime
    reason: Literal["artifact_submission_deadline_expired"] = "artifact_submission_deadline_expired"
    disposition: Literal["invalid_output"] = "invalid_output"
    retryable: Literal[False] = False
    conditional_on_terminal_submission_absence: Literal[True] = True
    database_time_activation_required: Literal[True] = True
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    adjudicated_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    adjudication_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _expiration_is_time_ordered_and_nonretryable(
        self,
    ) -> "QualificationTerminalDeadlineExpiration":
        if not (
            self.runtime_ended_at
            <= self.accepted_runtime_termination_at
            == self.authorized_at
            < self.artifact_submission_deadline
            == self.expired_at
            and self.hard_deadline < self.artifact_submission_deadline
        ):
            raise ValueError("qualification terminal deadline expiration is not time ordered")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="qualification_terminal_deadline_expiration",
            payload=self.signature_payload,
        )

    @property
    def terminal_deadline_expiration_sha256(self) -> str:
        return canonical_sha256(self)


def issue_qualification_terminal_deadline_expiration(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> QualificationTerminalDeadlineExpiration:
    """Pre-sign deterministic failure authority while accepting runtime termination."""
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
    challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
        launch_receipt.model_dump(mode="python")
    )
    node_termination_receipt = NodeRuntimeTerminationReceipt.model_validate(
        node_termination_receipt.model_dump(mode="python")
    )
    verify_accepted_runtime_termination(
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or runtime_authority.pin != pin
        or not pin.active_at(accepted.accepted_at)
        or accepted.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
        or challenge.execution_id != intent.execution_id
        or challenge.intent_sha256 != intent.intent_sha256
        or challenge.node_manifest_sha256 != node_authority.manifest.manifest_sha256
        or preparation.node_id != node_authority.manifest.node_id
        or preparation.node_manifest_sha256 != node_authority.manifest.manifest_sha256
        or challenge.node_inventory_sha256 != expected_node_inventory_sha256
        or challenge.resource_lease_sha256 != expected_resource_lease_sha256
    ):
        raise QualificationVerificationError(
            "terminal deadline expiration signer, scope, or DB time differs"
        )
    unsigned = QualificationTerminalDeadlineExpiration(
        attempt_id=accepted.attempt_id,
        execution_id=intent.execution_id,
        intent_sha256=intent.intent_sha256,
        node_id=preparation.node_id,
        node_manifest_sha256=preparation.node_manifest_sha256,
        node_inventory_sha256=expected_node_inventory_sha256,
        resource_lease_sha256=expected_resource_lease_sha256,
        runtime_preparation_sha256=accepted.runtime_preparation_sha256,
        runtime_launch_authorization_request_sha256=(
            accepted.runtime_launch_authorization_request_sha256
        ),
        runtime_launch_authorization_sha256=(accepted.runtime_launch_authorization_sha256),
        node_runtime_launch_receipt_sha256=(accepted.node_runtime_launch_receipt_sha256),
        runtime_termination_challenge_sha256=accepted.challenge_sha256,
        node_runtime_termination_receipt_sha256=(accepted.node_runtime_termination_receipt_sha256),
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        runtime_identity_sha256=accepted.runtime_identity_sha256,
        runtime_inspection_evidence_sha256=(accepted.runtime_inspection_evidence_sha256),
        engine_terminal_journal_sha256=accepted.engine_terminal_journal_sha256,
        inspection_sequence=accepted.inspection_sequence,
        fencing_epoch=accepted.fencing_epoch,
        lease_token_sha256=accepted.lease_token_sha256,
        runtime_ended_at=accepted.runtime_ended_at,
        exit_code=accepted.exit_code,
        hard_deadline=accepted.hard_deadline,
        artifact_submission_deadline=accepted.artifact_submission_deadline,
        accepted_runtime_termination_at=accepted.accepted_at,
        authorized_at=accepted.accepted_at,
        expired_at=accepted.artifact_submission_deadline,
        runtime_control_policy_sha256=pin.policy_sha256,
        adjudicated_by_principal_id=pin.principal_id,
        adjudication_key_id=pin.key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return QualificationTerminalDeadlineExpiration.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_qualification_terminal_deadline_expiration(
    *,
    expiration: QualificationTerminalDeadlineExpiration,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> QualificationTerminalDeadlineExpiration:
    """Historically verify a signed no-artifact terminal adjudication."""

    expiration = QualificationTerminalDeadlineExpiration.model_validate(
        expiration.model_dump(mode="python")
    )
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
    challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
        challenge.model_dump(mode="python")
    )
    preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
    verify_accepted_runtime_termination(
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    if (
        accepted.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
        or challenge.execution_id != intent.execution_id
        or challenge.intent_sha256 != intent.intent_sha256
        or challenge.node_manifest_sha256 != node_authority.manifest.manifest_sha256
        or preparation.node_id != node_authority.manifest.node_id
        or preparation.node_manifest_sha256 != node_authority.manifest.manifest_sha256
        or challenge.node_inventory_sha256 != expected_node_inventory_sha256
        or challenge.resource_lease_sha256 != expected_resource_lease_sha256
        or expiration.attempt_id != accepted.attempt_id
        or expiration.execution_id != intent.execution_id
        or expiration.intent_sha256 != intent.intent_sha256
        or expiration.node_id != preparation.node_id
        or expiration.node_manifest_sha256 != preparation.node_manifest_sha256
        or expiration.node_inventory_sha256 != expected_node_inventory_sha256
        or expiration.resource_lease_sha256 != expected_resource_lease_sha256
        or expiration.runtime_preparation_sha256 != accepted.runtime_preparation_sha256
        or expiration.runtime_launch_authorization_request_sha256
        != accepted.runtime_launch_authorization_request_sha256
        or expiration.runtime_launch_authorization_sha256
        != accepted.runtime_launch_authorization_sha256
        or expiration.node_runtime_launch_receipt_sha256
        != accepted.node_runtime_launch_receipt_sha256
        or expiration.runtime_termination_challenge_sha256 != accepted.challenge_sha256
        or expiration.node_runtime_termination_receipt_sha256
        != accepted.node_runtime_termination_receipt_sha256
        or expiration.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
        or expiration.runtime_identity_sha256 != accepted.runtime_identity_sha256
        or expiration.runtime_inspection_evidence_sha256
        != accepted.runtime_inspection_evidence_sha256
        or expiration.engine_terminal_journal_sha256 != accepted.engine_terminal_journal_sha256
        or expiration.inspection_sequence != accepted.inspection_sequence
        or expiration.fencing_epoch != accepted.fencing_epoch
        or expiration.lease_token_sha256 != accepted.lease_token_sha256
        or expiration.runtime_ended_at != accepted.runtime_ended_at
        or expiration.exit_code != accepted.exit_code
        or expiration.hard_deadline != accepted.hard_deadline
        or expiration.artifact_submission_deadline != accepted.artifact_submission_deadline
        or expiration.accepted_runtime_termination_at != accepted.accepted_at
        or expiration.authorized_at != accepted.accepted_at
        or expiration.expired_at != accepted.artifact_submission_deadline
    ):
        raise QualificationVerificationError(
            "terminal deadline expiration differs from accepted runtime lineage"
        )
    runtime_authority.verify_historical(
        kind="qualification_terminal_deadline_expiration",
        payload=expiration.signature_payload,
        signature_ed25519_hex=expiration.signature_ed25519_hex,
        policy_sha256=expiration.runtime_control_policy_sha256,
        principal_id=expiration.adjudicated_by_principal_id,
        key_id=expiration.adjudication_key_id,
        signed_at=expiration.authorized_at,
    )
    return expiration


class QualificationTerminalSubmission(ExecutionModel):
    """Node-signed post-quarantine output provenance bound to historical termination.

    This is intentionally independent of the legacy ``NodeExecutionReceipt`` and its short-lived
    inspection receipt.  The enrolled node signs only after independent CAS rehash, while the
    deployment must ensure the enrollment key covers the hard deadline plus artifact grace.
    """

    schema_name: Literal["aletheia.qualification_terminal_submission"] = (
        "aletheia.qualification_terminal_submission"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...] = ()
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    submitted_at: AwareDatetime
    signing_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _submission_is_canonical(self) -> "QualificationTerminalSubmission":
        hashes = self.artifact_verified_receipt_sha256s
        if hashes != tuple(sorted(set(hashes))) or any(
            re.fullmatch(_SHA256_PATTERN, item) is None for item in hashes
        ):
            raise ValueError("artifact verification receipt hashes must be sorted and unique")
        return self

    @property
    def signature_message(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"signature_ed25519_hex"}))

    @property
    def terminal_submission_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedQualificationTerminalSubmission(ExecutionModel):
    terminal_submission_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...]
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    verified_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False


def _artifact_verified_receipt_sha256s(
    *,
    manifest: ArtifactManifest,
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
) -> tuple[str, ...]:
    manifest_keys = tuple(item.artifact_key for item in manifest.entries)
    receipt_keys = tuple(item.artifact.artifact_key for item in artifact_verified_receipts)
    by_key = {item.artifact.artifact_key: item for item in artifact_verified_receipts}
    if (
        receipt_keys != manifest_keys
        or len(by_key) != len(artifact_verified_receipts)
        or set(by_key) != set(manifest_keys)
    ):
        raise QualificationVerificationError(
            "artifact verification receipts must exactly cover manifest artifact-key order"
        )
    for entry in manifest.entries:
        receipt = by_key[entry.artifact_key]
        if (
            receipt.artifact_manifest_sha256 != manifest.manifest_sha256
            or receipt.producer_attempt_id != manifest.infrastructure_attempt_id
            or receipt.artifact != entry
        ):
            raise QualificationVerificationError(
                "artifact verification receipt differs from exact manifest entry"
            )
    return tuple(sorted(item.verified_receipt_sha256 for item in artifact_verified_receipts))


def _qualification_terminal_disposition(
    *,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    manifest: ArtifactManifest,
) -> Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]:
    actual_keys = {item.artifact_key for item in manifest.entries}
    missing_required = any(
        item.required and item.artifact_key not in actual_keys for item in intent.expected_artifacts
    )
    if accepted.exit_code != 0:
        return "process_failed"
    if accepted.runtime_ended_at > min(intent.deadline, accepted.hard_deadline):
        return "timeout"
    if missing_required:
        return "invalid_output"
    return "process_succeeded"


def issue_qualification_terminal_submission(
    *,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    private_key: bytes,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    node_inventory_sha256: str,
    resource_lease_sha256: str,
    artifact_manifest: ArtifactManifest,
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"],
    submitted_at: datetime,
) -> QualificationTerminalSubmission:
    """Sign exact post-quarantine provenance without refreshing runtime termination proof."""

    _require_utc(submitted_at, "qualification terminal submitted_at")
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
    artifact_manifest = ArtifactManifest.model_validate(artifact_manifest.model_dump(mode="python"))
    receipts = tuple(
        ArtifactVerifiedReceipt.model_validate(item.model_dump(mode="python"))
        for item in artifact_verified_receipts
    )
    receipt_hashes = _artifact_verified_receipt_sha256s(
        manifest=artifact_manifest,
        artifact_verified_receipts=receipts,
    )
    verify_accepted_runtime_termination(
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    expected_disposition = _qualification_terminal_disposition(
        intent=intent,
        accepted=accepted,
        manifest=artifact_manifest,
    )
    manifest_scope_is_exact = (
        accepted.attempt_id
        == intent.infrastructure_attempt.infrastructure_attempt_id
        == artifact_manifest.infrastructure_attempt_id
        and artifact_manifest.intent_sha256 == intent.intent_sha256
        and artifact_manifest.execution_id == intent.execution_id
        and artifact_manifest.replicate_slot_id == intent.replicate_slot.replicate_slot_id
        and artifact_manifest.produced_at == accepted.runtime_ended_at
    )
    if (
        _public_key_hex(private_key) != node_authority.manifest.node_signing_public_key_ed25519_hex
        or not manifest_scope_is_exact
        or disposition != expected_disposition
        or submitted_at < accepted.accepted_at
        or not submitted_at < accepted.artifact_submission_deadline
        or not submitted_at < node_authority.active_until
    ):
        raise QualificationVerificationError(
            "terminal submission signer, scope, disposition, or grace window differs"
        )
    unsigned = QualificationTerminalSubmission(
        node_manifest_sha256=node_authority.manifest.manifest_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        attempt_id=accepted.attempt_id,
        node_inventory_sha256=node_inventory_sha256,
        resource_lease_sha256=resource_lease_sha256,
        fencing_epoch=accepted.fencing_epoch,
        lease_token_sha256=accepted.lease_token_sha256,
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        artifact_manifest_sha256=artifact_manifest.manifest_sha256,
        output_tree_sha256=artifact_output_tree_sha256(artifact_manifest),
        artifact_verified_receipt_sha256s=receipt_hashes,
        disposition=disposition,
        submitted_at=submitted_at,
        signing_key_id=node_authority.manifest.node_signing_key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return QualificationTerminalSubmission.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_qualification_terminal_submission(
    *,
    submission: QualificationTerminalSubmission,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    artifact_manifest: ArtifactManifest,
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    verified_at: datetime,
) -> VerifiedQualificationTerminalSubmission:
    """Historically verify node output provenance after runtime/node proof expiry."""

    _require_utc(verified_at, "qualification terminal verified_at")
    submission = QualificationTerminalSubmission.model_validate(
        submission.model_dump(mode="python")
    )
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
    artifact_manifest = ArtifactManifest.model_validate(artifact_manifest.model_dump(mode="python"))
    receipts = tuple(
        ArtifactVerifiedReceipt.model_validate(item.model_dump(mode="python"))
        for item in artifact_verified_receipts
    )
    receipt_hashes = _artifact_verified_receipt_sha256s(
        manifest=artifact_manifest,
        artifact_verified_receipts=receipts,
    )
    verify_accepted_runtime_termination(
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
    )
    expected_disposition = _qualification_terminal_disposition(
        intent=intent,
        accepted=accepted,
        manifest=artifact_manifest,
    )
    if (
        submission.node_manifest_sha256 != node_authority.manifest.manifest_sha256
        or submission.intent_sha256 != intent.intent_sha256
        or submission.execution_id != intent.execution_id
        or submission.attempt_id != accepted.attempt_id
        or accepted.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
        or artifact_manifest.infrastructure_attempt_id != accepted.attempt_id
        or artifact_manifest.intent_sha256 != intent.intent_sha256
        or artifact_manifest.execution_id != intent.execution_id
        or artifact_manifest.replicate_slot_id != intent.replicate_slot.replicate_slot_id
        or artifact_manifest.produced_at != accepted.runtime_ended_at
        or submission.node_inventory_sha256 != expected_node_inventory_sha256
        or submission.resource_lease_sha256 != expected_resource_lease_sha256
        or submission.fencing_epoch != accepted.fencing_epoch
        or submission.lease_token_sha256 != accepted.lease_token_sha256
        or submission.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
        or submission.artifact_manifest_sha256 != artifact_manifest.manifest_sha256
        or submission.output_tree_sha256 != artifact_output_tree_sha256(artifact_manifest)
        or submission.artifact_verified_receipt_sha256s != receipt_hashes
        or submission.disposition != expected_disposition
        or submission.submitted_at < accepted.accepted_at
        or not submission.submitted_at <= verified_at < accepted.artifact_submission_deadline
    ):
        raise QualificationVerificationError(
            "terminal submission differs from accepted runtime and exact verified artifacts"
        )
    node_authority.verify_signature(
        signing_key_id=submission.signing_key_id,
        message=submission.signature_message,
        signature_ed25519_hex=submission.signature_ed25519_hex,
        signed_at=submission.submitted_at,
    )
    return VerifiedQualificationTerminalSubmission(
        terminal_submission_sha256=submission.terminal_submission_sha256,
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        artifact_manifest_sha256=artifact_manifest.manifest_sha256,
        output_tree_sha256=submission.output_tree_sha256,
        artifact_verified_receipt_sha256s=receipt_hashes,
        disposition=expected_disposition,
        verified_at=verified_at,
    )


class AcceptedQualificationTerminalSubmission(ExecutionModel):
    """Immutable runtime-control acceptance of fresh node-signed artifact provenance."""

    schema_name: Literal["aletheia.accepted_qualification_terminal_submission"] = (
        "aletheia.accepted_qualification_terminal_submission"
    )
    schema_version: Literal[2] = RUNTIME_V2_CONTRACT_SCHEMA_VERSION
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_submission_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...]
    disposition: Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]
    node_submitted_at: AwareDatetime
    artifact_submission_deadline: AwareDatetime
    accepted_at: AwareDatetime
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    acceptance_key_id: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _acceptance_is_canonical_and_fresh(
        self,
    ) -> "AcceptedQualificationTerminalSubmission":
        hashes = self.artifact_verified_receipt_sha256s
        if (
            hashes != tuple(sorted(set(hashes)))
            or not self.node_submitted_at <= self.accepted_at < self.artifact_submission_deadline
        ):
            raise ValueError("terminal submission acceptance is noncanonical or late")
        return self

    @property
    def signature_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"signature_ed25519_hex"})

    @property
    def signature_message(self) -> bytes:
        return _runtime_control_message(
            kind="accepted_qualification_terminal_submission",
            payload=self.signature_payload,
        )

    @property
    def accepted_terminal_submission_sha256(self) -> str:
        return canonical_sha256(self)


def issue_accepted_qualification_terminal_submission(
    *,
    pin: RuntimeControlAuthorityPin,
    private_key: bytes,
    submission: QualificationTerminalSubmission,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    artifact_manifest: ArtifactManifest,
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
    accepted_at: datetime,
) -> AcceptedQualificationTerminalSubmission:
    """Accept fresh verified artifact provenance before its signed grace deadline."""

    verified = verify_qualification_terminal_submission(
        submission=submission,
        intent=intent,
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        artifact_manifest=artifact_manifest,
        artifact_verified_receipts=artifact_verified_receipts,
        expected_node_inventory_sha256=expected_node_inventory_sha256,
        expected_resource_lease_sha256=expected_resource_lease_sha256,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        verified_at=accepted_at,
    )
    if (
        _public_key_hex(private_key) != pin.public_key_ed25519_hex
        or runtime_authority.pin != pin
        or not pin.active_at(accepted_at)
    ):
        raise QualificationVerificationError(
            "terminal submission acceptance signer is inactive or unpinned"
        )
    unsigned = AcceptedQualificationTerminalSubmission(
        attempt_id=submission.attempt_id,
        node_manifest_sha256=submission.node_manifest_sha256,
        terminal_submission_sha256=verified.terminal_submission_sha256,
        accepted_runtime_termination_sha256=(verified.accepted_runtime_termination_sha256),
        artifact_manifest_sha256=verified.artifact_manifest_sha256,
        output_tree_sha256=verified.output_tree_sha256,
        artifact_verified_receipt_sha256s=(verified.artifact_verified_receipt_sha256s),
        disposition=verified.disposition,
        node_submitted_at=submission.submitted_at,
        artifact_submission_deadline=accepted.artifact_submission_deadline,
        accepted_at=accepted_at,
        runtime_control_policy_sha256=pin.policy_sha256,
        accepted_by_principal_id=pin.principal_id,
        acceptance_key_id=pin.key_id,
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    return AcceptedQualificationTerminalSubmission.model_validate(
        unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()}).model_dump(
            mode="python"
        )
    )


def verify_accepted_qualification_terminal_submission(
    *,
    terminal_acceptance: AcceptedQualificationTerminalSubmission,
    submission: QualificationTerminalSubmission,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    challenge: RuntimeTerminationAcceptanceChallenge,
    node_termination_receipt: NodeRuntimeTerminationReceipt,
    preparation: RuntimePreparation,
    launch_receipt: NodeRuntimeLaunchReceipt,
    launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    launch_authorization: RuntimeLaunchAuthorization,
    artifact_manifest: ArtifactManifest,
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
    expected_node_inventory_sha256: str,
    expected_resource_lease_sha256: str,
    node_authority: WorkerNodeAuthorityVerifier,
    runtime_authority: RuntimeControlAuthorityVerifier,
) -> VerifiedQualificationTerminalSubmission:
    """Reconstruct historical fresh verification after all online proof windows expire."""

    terminal_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
        terminal_acceptance.model_dump(mode="python")
    )
    verified = verify_qualification_terminal_submission(
        submission=submission,
        intent=intent,
        accepted=accepted,
        challenge=challenge,
        node_termination_receipt=node_termination_receipt,
        preparation=preparation,
        launch_receipt=launch_receipt,
        launch_authorization_request=launch_authorization_request,
        launch_authorization=launch_authorization,
        artifact_manifest=artifact_manifest,
        artifact_verified_receipts=artifact_verified_receipts,
        expected_node_inventory_sha256=expected_node_inventory_sha256,
        expected_resource_lease_sha256=expected_resource_lease_sha256,
        node_authority=node_authority,
        runtime_authority=runtime_authority,
        verified_at=terminal_acceptance.accepted_at,
    )
    if (
        terminal_acceptance.attempt_id != submission.attempt_id
        or terminal_acceptance.node_manifest_sha256 != submission.node_manifest_sha256
        or terminal_acceptance.terminal_submission_sha256 != verified.terminal_submission_sha256
        or terminal_acceptance.accepted_runtime_termination_sha256
        != verified.accepted_runtime_termination_sha256
        or terminal_acceptance.artifact_manifest_sha256 != verified.artifact_manifest_sha256
        or terminal_acceptance.output_tree_sha256 != verified.output_tree_sha256
        or terminal_acceptance.artifact_verified_receipt_sha256s
        != verified.artifact_verified_receipt_sha256s
        or terminal_acceptance.disposition != verified.disposition
        or terminal_acceptance.node_submitted_at != submission.submitted_at
        or terminal_acceptance.artifact_submission_deadline != accepted.artifact_submission_deadline
    ):
        raise QualificationVerificationError(
            "accepted terminal submission differs from historical full proof"
        )
    runtime_authority.verify_historical(
        kind="accepted_qualification_terminal_submission",
        payload=terminal_acceptance.signature_payload,
        signature_ed25519_hex=terminal_acceptance.signature_ed25519_hex,
        policy_sha256=terminal_acceptance.runtime_control_policy_sha256,
        principal_id=terminal_acceptance.accepted_by_principal_id,
        key_id=terminal_acceptance.acceptance_key_id,
        signed_at=terminal_acceptance.accepted_at,
    )
    return verified


class RuntimeControlVerificationPort(Protocol):
    """Public-key-only runtime-control boundary used by historical readers."""

    @property
    def authority_pin(self) -> RuntimeControlAuthorityPin: ...

    @property
    def authority_verifier(self) -> RuntimeControlAuthorityVerifier: ...


class PinnedRuntimeControlVerificationAuthority:
    """Concrete verifier that deliberately has no issuance or private-key surface."""

    def __init__(self, pin: RuntimeControlAuthorityPin) -> None:
        self._verifier = RuntimeControlAuthorityVerifier(pin)

    @property
    def authority_pin(self) -> RuntimeControlAuthorityPin:
        return self._verifier.pin

    @property
    def authority_verifier(self) -> RuntimeControlAuthorityVerifier:
        return self._verifier


class RuntimeControlIssuancePort(RuntimeControlVerificationPort, Protocol):
    """Narrow key-custody boundary; allocator code never receives a raw private key."""

    def issue_launch_authorization(
        self,
        *,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        preparation: RuntimePreparation,
        admission_sha256: str,
        qualification_grant_sha256: str,
        lease_expires_at: datetime,
        hard_deadline: datetime,
        issued_at: datetime,
        expires_at: datetime,
        max_launch_delay_ns: int,
    ) -> RuntimeLaunchAuthorization: ...

    def issue_termination_challenge(
        self,
        *,
        preparation: RuntimePreparation,
        launch_receipt: NodeRuntimeLaunchReceipt,
        termination_evidence: RuntimeInspectionEvidence,
        inspection_sequence: int,
        node_inventory_sha256: str,
        resource_lease_sha256: str,
        fencing_epoch: int,
        lease_token_sha256: str,
        hard_deadline: datetime,
        artifact_submission_deadline: datetime,
        challenged_at: datetime,
        expires_at: datetime,
    ) -> RuntimeTerminationAcceptanceChallenge: ...

    def issue_accepted_termination(
        self,
        *,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_termination_receipt: NodeRuntimeTerminationReceipt,
        preparation: RuntimePreparation,
        launch_receipt: NodeRuntimeLaunchReceipt,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
        launch_authorization: RuntimeLaunchAuthorization,
        node_authority: WorkerNodeAuthorityVerifier,
        accepted_at: datetime,
        billable_ended_at: datetime,
        maximum_proof_age_seconds: int,
    ) -> AcceptedRuntimeTermination: ...

    def issue_terminal_submission_acceptance(
        self,
        *,
        submission: QualificationTerminalSubmission,
        intent: ExecutionIntent,
        accepted: AcceptedRuntimeTermination,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_termination_receipt: NodeRuntimeTerminationReceipt,
        preparation: RuntimePreparation,
        launch_receipt: NodeRuntimeLaunchReceipt,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
        launch_authorization: RuntimeLaunchAuthorization,
        artifact_manifest: ArtifactManifest,
        artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
        expected_node_inventory_sha256: str,
        expected_resource_lease_sha256: str,
        node_authority: WorkerNodeAuthorityVerifier,
        accepted_at: datetime,
    ) -> AcceptedQualificationTerminalSubmission: ...

    def issue_terminal_deadline_expiration(
        self,
        *,
        intent: ExecutionIntent,
        accepted: AcceptedRuntimeTermination,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_termination_receipt: NodeRuntimeTerminationReceipt,
        preparation: RuntimePreparation,
        launch_receipt: NodeRuntimeLaunchReceipt,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
        launch_authorization: RuntimeLaunchAuthorization,
        expected_node_inventory_sha256: str,
        expected_resource_lease_sha256: str,
        node_authority: WorkerNodeAuthorityVerifier,
    ) -> QualificationTerminalDeadlineExpiration: ...

    def issue_historical_recovery(
        self,
        *,
        admission_sha256: str,
        qualification_grant_sha256: str,
        intent_sha256: str,
        execution_id: str,
        infrastructure_attempt_id: str,
        runtime_preparation_sha256: str,
        node_runtime_launch_receipt_sha256: str,
        accepted_runtime_termination_sha256: str | None,
        admitted_at: datetime,
        hard_deadline: datetime,
        issued_at: datetime,
        recovery_expires_at: datetime,
    ) -> HistoricalRuntimeRecoveryGrant: ...


__all__ = [
    "AcceptedQualificationTerminalSubmission",
    "AcceptedRuntimeTermination",
    "AttemptScopedPreRuntimeCleanupAuthorityPin",
    "AttemptScopedPreRuntimeCleanupAuthorityVerifier",
    "HistoricalPreRuntimeRecoveryLineage",
    "HistoricalRuntimeRecoveryGrant",
    "InputMaterializationEntry",
    "InputMaterializationReceipt",
    "MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES",
    "NodeRuntimeLaunchReceipt",
    "NodeRuntimeTerminationReceipt",
    "OutputQuotaProvisioningReceipt",
    "PinnedOutputWorkspaceRoot",
    "PinnedInputPath",
    "PreRuntimeAbsenceReceipt",
    "QualificationTerminalDeadlineExpiration",
    "RUNTIME_V2_CONTRACT_SCHEMA_VERSION",
    "RuntimeFenceRebindEvidence",
    "RuntimeFenceRebindReceipt",
    "RuntimeFenceRebindRequest",
    "RuntimeInspectionEvidence",
    "RuntimeControlAuthorityPin",
    "RuntimeControlAuthorityVerifier",
    "PinnedRuntimeControlVerificationAuthority",
    "RuntimeControlIssuancePort",
    "RuntimeControlVerificationPort",
    "RuntimeLaunchAuthorization",
    "RuntimeLaunchAuthorizationRequest",
    "RuntimeLaunchEvidence",
    "RuntimePreparation",
    "RuntimeTerminationAcceptanceChallenge",
    "QualificationTerminalSubmission",
    "VerifiedQualificationTerminalSubmission",
    "VerifiedNodeRuntimeLaunch",
    "VerifiedNodeRuntimeTermination",
    "VerifiedPreRuntimeAbsence",
    "VerifiedRuntimeFenceRebind",
    "issue_accepted_runtime_termination",
    "issue_accepted_qualification_terminal_submission",
    "issue_attempt_scoped_pre_runtime_cleanup_receipt",
    "issue_historical_runtime_recovery_grant",
    "issue_node_runtime_launch_receipt",
    "issue_node_runtime_termination_receipt",
    "issue_pre_runtime_absence_receipt",
    "issue_qualification_terminal_deadline_expiration",
    "issue_qualification_terminal_submission",
    "issue_runtime_fence_rebind_receipt",
    "issue_runtime_launch_authorization",
    "issue_runtime_termination_acceptance_challenge",
    "validate_pre_runtime_absence_evidence_refresh",
    "validate_runtime_fence_rebind_evidence",
    "validate_runtime_terminal_evidence_refresh",
    "verify_accepted_runtime_termination",
    "verify_accepted_qualification_terminal_submission",
    "verify_historical_runtime_recovery_grant",
    "verify_node_runtime_launch_receipt",
    "verify_node_runtime_launch_receipt_historical",
    "verify_node_runtime_termination_receipt",
    "verify_node_runtime_termination_receipt_historical",
    "verify_pre_runtime_absence_receipt",
    "verify_qualification_terminal_deadline_expiration",
    "verify_qualification_terminal_submission",
    "verify_runtime_fence_rebind_receipt",
    "verify_runtime_launch_authorization",
    "verify_runtime_launch_authorization_historical",
    "verify_runtime_launch_authorization_ticket_historical",
    "verify_runtime_termination_acceptance_challenge",
]
