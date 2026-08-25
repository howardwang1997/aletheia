"""F10-S7 signed, role-separated capability promotion.

The promotion boundary treats AI-authored capability code like an untrusted supply-chain
artifact.  Provisional code may be explored only after a hard-sandbox receipt; generated tests
are frozen separately; an independent validator and domain reviewer attest their own results;
and a different promotion auditor approves the complete request.  A registry promoter can then
append exactly one registered successor to the exact source snapshot.

Private keys are deliberately absent from every model.  Callers provide raw Ed25519 private-key
bytes only to the signing helper, while persisted policies contain public keys, scoped
permissions, thresholds, validity windows, and revocation times.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.registry import (
    CapabilityRegistrySnapshot,
    build_capability_registry_snapshot,
)
from aletheia.capabilities.schemas import (
    CapabilityBoundary,
    CapabilityEvidenceLevel,
    CapabilityLifecycle,
    CapabilityRegistrationEvidence,
    CapabilityRole,
    CapabilityRoleBinding,
    ControlKind,
    ExperimentCapabilityManifest,
    evidence_level_rank,
)
from aletheia.coder.executor import SandboxExecution, execute_python_files
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$"
_SIGNATURE_CONTEXT = "aletheia.capability-promotion/v1"


class CapabilityPromotionError(RuntimeError):
    """A promotion artifact is unauthorized, inconsistent, stale, or corrupt."""


class PromotionPermission(str, Enum):
    SANDBOX_ATTEST = "sandbox_attest"
    TEST_SUITE_ATTEST = "test_suite_attest"
    VALIDATION_ATTEST = "validation_attest"
    DOMAIN_REVIEW_ATTEST = "domain_review_attest"
    PROMOTION_AUDIT = "promotion_audit"
    REGISTRY_PROMOTE = "registry_promote"


_PERMISSION_ORDER = tuple(sorted(PromotionPermission, key=lambda item: item.value))


class PromotionArtifactKind(str, Enum):
    SANDBOX_AUTHORING = "sandbox_authoring"
    GENERATED_TEST_SUITE = "generated_test_suite"
    INDEPENDENT_VALIDATION = "independent_validation"
    DOMAIN_REVIEW = "domain_review"
    PROMOTION_AUDIT = "promotion_audit"
    REGISTRY_UPDATE = "registry_update"


_ARTIFACT_PERMISSION = {
    PromotionArtifactKind.SANDBOX_AUTHORING: PromotionPermission.SANDBOX_ATTEST,
    PromotionArtifactKind.GENERATED_TEST_SUITE: PromotionPermission.TEST_SUITE_ATTEST,
    PromotionArtifactKind.INDEPENDENT_VALIDATION: PromotionPermission.VALIDATION_ATTEST,
    PromotionArtifactKind.DOMAIN_REVIEW: PromotionPermission.DOMAIN_REVIEW_ATTEST,
    PromotionArtifactKind.PROMOTION_AUDIT: PromotionPermission.PROMOTION_AUDIT,
    PromotionArtifactKind.REGISTRY_UPDATE: PromotionPermission.REGISTRY_PROMOTE,
}


class PromotionDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


def _semantic_version(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(item) for item in value.split("."))
    except ValueError as exc:
        raise ValueError("promotion target version must use semantic versioning") from exc
    if len(parts) != 3 or any(item < 0 for item in parts):
        raise ValueError("promotion target version must use semantic versioning")
    return parts  # type: ignore[return-value]


def _public_key_bytes(private_key: bytes) -> bytes:
    if len(private_key) != 32:
        raise CapabilityPromotionError("Ed25519 private keys must contain exactly 32 raw bytes")
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def ed25519_public_key_hex(private_key: bytes) -> str:
    """Return the raw public-key encoding without retaining the private key."""

    return _public_key_bytes(private_key).hex()


def ed25519_key_id(public_key_hex: str) -> str:
    """Derive the immutable key id from the raw Ed25519 public key."""

    try:
        public_key = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise ValueError("Ed25519 public key must be hexadecimal") from exc
    if len(public_key) != 32:
        raise ValueError("Ed25519 public keys must contain exactly 32 raw bytes")
    return hashlib.sha256(public_key).hexdigest()


class TrustedPromotionKey(FrozenModel):
    schema_version: Literal[1] = 1
    key_id: str = Field(pattern=_SHA256_PATTERN)
    principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    domains: tuple[str, ...] = Field(min_length=1)
    capability_prefixes: tuple[str, ...] = Field(min_length=1)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _key_is_canonical_and_bounded(self) -> "TrustedPromotionKey":
        if self.key_id != ed25519_key_id(self.public_key_ed25519_hex):
            raise ValueError("promotion key id does not match its Ed25519 public key")
        if self.domains != tuple(sorted(set(self.domains))):
            raise ValueError("promotion key domains must be unique and sorted")
        if self.capability_prefixes != tuple(sorted(set(self.capability_prefixes))):
            raise ValueError("promotion key capability prefixes must be unique and sorted")
        if any(not item.strip() for item in (*self.domains, *self.capability_prefixes)):
            raise ValueError("promotion key scopes cannot be blank")
        if self.expires_at <= self.valid_from:
            raise ValueError("promotion key expiry must follow its validity start")
        return self

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp <= self.expires_at and (
            self.revoked_at is None or timestamp < self.revoked_at
        )

    def permits(self, *, domain: str, capability_id: str) -> bool:
        return domain in self.domains and any(
            capability_id.startswith(prefix) for prefix in self.capability_prefixes
        )


class PromotionRolePolicy(FrozenModel):
    schema_version: Literal[1] = 1
    permission: PromotionPermission
    key_ids: tuple[str, ...] = Field(min_length=1)
    threshold: int = Field(ge=1)

    @model_validator(mode="after")
    def _role_is_canonical(self) -> "PromotionRolePolicy":
        if self.key_ids != tuple(sorted(set(self.key_ids))):
            raise ValueError("promotion role keys must be unique and sorted")
        if self.threshold > len(self.key_ids):
            raise ValueError("promotion role threshold exceeds its key count")
        return self


class CapabilityPromotionPolicy(FrozenModel):
    """Externally trusted root for one exact registry promotion epoch."""

    schema_name: Literal["aletheia.capability_promotion_policy"] = (
        "aletheia.capability_promotion_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ID_PATTERN)
    registry_id: str = Field(pattern=_ID_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    trusted_keys: tuple[TrustedPromotionKey, ...] = Field(min_length=6)
    roles: tuple[PromotionRolePolicy, ...] = Field(min_length=6, max_length=6)
    allowed_sandbox_image_ids: tuple[str, ...] = Field(min_length=1)
    frozen_at: AwareDatetime
    expires_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_is_canonical_and_role_separated(self) -> "CapabilityPromotionPolicy":
        if self.expires_at <= self.frozen_at:
            raise ValueError("promotion policy expiry must follow its freeze time")
        if self.trusted_keys != tuple(sorted(self.trusted_keys, key=lambda item: item.key_id)):
            raise ValueError("trusted promotion keys must be sorted by key id")
        key_ids = tuple(item.key_id for item in self.trusted_keys)
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("promotion policy repeats a trusted key")
        if self.roles != tuple(sorted(self.roles, key=lambda item: item.permission.value)):
            raise ValueError("promotion role policies must be sorted by permission")
        permissions = tuple(item.permission for item in self.roles)
        if permissions != _PERMISSION_ORDER:
            raise ValueError("promotion policy must define every permission exactly once")
        if self.allowed_sandbox_image_ids != tuple(sorted(set(self.allowed_sandbox_image_ids))):
            raise ValueError("allowed sandbox images must be unique and sorted")

        keys = {item.key_id: item for item in self.trusted_keys}
        permission_principals: dict[PromotionPermission, set[str]] = {}
        for role in self.roles:
            missing = set(role.key_ids) - set(keys)
            if missing:
                raise ValueError("promotion role references an unknown trusted key")
            principals = {keys[key_id].principal_sha256 for key_id in role.key_ids}
            if role.threshold > len(principals):
                raise ValueError("promotion threshold requires distinct principals, not only keys")
            active = {
                keys[key_id].principal_sha256
                for key_id in role.key_ids
                if keys[key_id].active_at(self.frozen_at)
            }
            if len(active) < role.threshold:
                raise ValueError("promotion role lacks enough active principals at policy freeze")
            permission_principals[role.permission] = principals
        for index, permission in enumerate(_PERMISSION_ORDER):
            for other in _PERMISSION_ORDER[index + 1 :]:
                if permission_principals[permission] & permission_principals[other]:
                    raise ValueError("promotion permissions require role-separated principals")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)

    def key(self, key_id: str) -> TrustedPromotionKey:
        matches = [item for item in self.trusted_keys if item.key_id == key_id]
        if len(matches) != 1:
            raise CapabilityPromotionError(f"untrusted promotion key {key_id!r}")
        return matches[0]

    def role(self, permission: PromotionPermission) -> PromotionRolePolicy:
        matches = [item for item in self.roles if item.permission is permission]
        if len(matches) != 1:
            raise CapabilityPromotionError(
                f"promotion permission {permission.value!r} is undefined"
            )
        return matches[0]


class PromotionSignature(FrozenModel):
    schema_version: Literal[1] = 1
    key_id: str = Field(pattern=_SHA256_PATTERN)
    principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    signature_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{128}$")


class SignedPromotionArtifact(FrozenModel):
    """A compact, context-separated signature envelope for one content hash."""

    schema_name: Literal["aletheia.signed_promotion_artifact"] = (
        "aletheia.signed_promotion_artifact"
    )
    schema_version: Literal[1] = 1
    artifact_kind: PromotionArtifactKind
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    promotion_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_id: str = Field(pattern=_ID_PATTERN)
    capability_id: str = Field(pattern=_ID_PATTERN)
    domain: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    issued_at: AwareDatetime
    signatures: tuple[PromotionSignature, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _signatures_are_canonical(self) -> "SignedPromotionArtifact":
        if self.signatures != tuple(sorted(self.signatures, key=lambda item: item.key_id)):
            raise ValueError("promotion signatures must be sorted by key id")
        key_ids = tuple(item.key_id for item in self.signatures)
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("one key cannot sign a promotion artifact twice")
        return self

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)


def _signature_message(
    *,
    artifact_kind: PromotionArtifactKind,
    artifact_sha256: str,
    promotion_policy_sha256: str,
    registry_id: str,
    capability_id: str,
    domain: str,
    issued_at: datetime,
) -> bytes:
    return canonical_json_bytes(
        {
            "context": _SIGNATURE_CONTEXT,
            "artifact_kind": artifact_kind.value,
            "artifact_sha256": artifact_sha256,
            "promotion_policy_sha256": promotion_policy_sha256,
            "registry_id": registry_id,
            "capability_id": capability_id,
            "domain": domain,
            "issued_at": issued_at.isoformat(),
        }
    )


def _signer_principals(
    *,
    policy: CapabilityPromotionPolicy,
    permission: PromotionPermission,
    signer_private_keys: Mapping[str, bytes],
    domain: str,
    capability_id: str,
    issued_at: datetime,
) -> tuple[str, ...]:
    role = policy.role(permission)
    if not signer_private_keys:
        raise CapabilityPromotionError("promotion artifact has no signer")
    if set(signer_private_keys) - set(role.key_ids):
        raise CapabilityPromotionError("promotion signer lacks the required permission")
    principals: list[str] = []
    for key_id, private_key in signer_private_keys.items():
        key = policy.key(key_id)
        if not key.active_at(issued_at):
            raise CapabilityPromotionError("promotion signer key is expired, premature, or revoked")
        if not key.permits(domain=domain, capability_id=capability_id):
            raise CapabilityPromotionError("promotion signer key is outside its delegated scope")
        if _public_key_bytes(private_key).hex() != key.public_key_ed25519_hex:
            raise CapabilityPromotionError(
                "promotion private key does not match its trusted key id"
            )
        principals.append(key.principal_sha256)
    if len(set(principals)) < role.threshold:
        raise CapabilityPromotionError("promotion artifact does not meet its signature threshold")
    return tuple(sorted(set(principals)))


def sign_promotion_artifact(
    *,
    policy: CapabilityPromotionPolicy,
    artifact_kind: PromotionArtifactKind,
    artifact_sha256: str,
    capability_id: str,
    domain: str,
    issued_at: datetime,
    signer_private_keys: Mapping[str, bytes],
) -> SignedPromotionArtifact:
    """Sign one hash with the authorized keys for its artifact kind."""

    permission = _ARTIFACT_PERMISSION[artifact_kind]
    _signer_principals(
        policy=policy,
        permission=permission,
        signer_private_keys=signer_private_keys,
        domain=domain,
        capability_id=capability_id,
        issued_at=issued_at,
    )
    message = _signature_message(
        artifact_kind=artifact_kind,
        artifact_sha256=artifact_sha256,
        promotion_policy_sha256=policy.policy_sha256,
        registry_id=policy.registry_id,
        capability_id=capability_id,
        domain=domain,
        issued_at=issued_at,
    )
    signatures = []
    for key_id, private_key in sorted(signer_private_keys.items()):
        key = policy.key(key_id)
        signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
        signatures.append(
            PromotionSignature(
                key_id=key_id,
                principal_sha256=key.principal_sha256,
                signature_ed25519_hex=signature.hex(),
            )
        )
    envelope = SignedPromotionArtifact(
        artifact_kind=artifact_kind,
        artifact_sha256=artifact_sha256,
        promotion_policy_sha256=policy.policy_sha256,
        registry_id=policy.registry_id,
        capability_id=capability_id,
        domain=domain,
        issued_at=issued_at,
        signatures=tuple(signatures),
    )
    verify_promotion_artifact(envelope=envelope, policy=policy)
    return envelope


def verify_promotion_artifact(
    *, envelope: SignedPromotionArtifact, policy: CapabilityPromotionPolicy
) -> tuple[str, ...]:
    """Verify scope, time, permission, threshold, principal identity, and Ed25519 bytes."""

    envelope = SignedPromotionArtifact.model_validate(envelope.model_dump(mode="python"))
    policy = CapabilityPromotionPolicy.model_validate(policy.model_dump(mode="python"))
    if envelope.promotion_policy_sha256 != policy.policy_sha256:
        raise CapabilityPromotionError("promotion artifact is bound to another trust policy")
    if envelope.registry_id != policy.registry_id:
        raise CapabilityPromotionError("promotion artifact is bound to another registry")
    if not (policy.frozen_at <= envelope.issued_at <= policy.expires_at):
        raise CapabilityPromotionError("promotion artifact is outside the policy validity window")
    permission = _ARTIFACT_PERMISSION[envelope.artifact_kind]
    role = policy.role(permission)
    message = _signature_message(
        artifact_kind=envelope.artifact_kind,
        artifact_sha256=envelope.artifact_sha256,
        promotion_policy_sha256=envelope.promotion_policy_sha256,
        registry_id=envelope.registry_id,
        capability_id=envelope.capability_id,
        domain=envelope.domain,
        issued_at=envelope.issued_at,
    )
    principals: set[str] = set()
    for signature in envelope.signatures:
        if signature.key_id not in role.key_ids:
            raise CapabilityPromotionError("promotion signature lacks the required permission")
        key = policy.key(signature.key_id)
        if signature.principal_sha256 != key.principal_sha256:
            raise CapabilityPromotionError("promotion signature principal does not match its key")
        if not key.active_at(envelope.issued_at):
            raise CapabilityPromotionError("promotion signature uses an inactive key")
        if not key.permits(domain=envelope.domain, capability_id=envelope.capability_id):
            raise CapabilityPromotionError("promotion signature is outside its delegated scope")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_ed25519_hex)).verify(
                bytes.fromhex(signature.signature_ed25519_hex), message
            )
        except (InvalidSignature, ValueError) as exc:
            raise CapabilityPromotionError("promotion artifact signature is invalid") from exc
        principals.add(key.principal_sha256)
    if len(principals) < role.threshold:
        raise CapabilityPromotionError("promotion artifact does not meet its signature threshold")
    return tuple(sorted(principals))


class CapabilitySourceArtifact(FrozenModel):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    sha256: str = Field(pattern=_SHA256_PATTERN)
    bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _name_is_flat(self) -> "CapabilitySourceArtifact":
        path = PurePosixPath(self.name)
        if path.name != self.name or self.name in {".", ".."}:
            raise ValueError("sandbox source artifact names must be flat")
        return self


class SandboxAuthoringReceipt(FrozenModel):
    schema_name: Literal["aletheia.capability_sandbox_authoring_receipt"] = (
        "aletheia.capability_sandbox_authoring_receipt"
    )
    schema_version: Literal[1] = 1
    provisional_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    authored_implementation_sha256s: tuple[str, ...] = Field(min_length=1)
    source_artifacts: tuple[CapabilitySourceArtifact, ...] = Field(min_length=1)
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_review_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_review_passed: Literal[True] = True
    boundary: Literal[CapabilityBoundary.HARD_SANDBOX] = CapabilityBoundary.HARD_SANDBOX
    sandbox_image_id: str = Field(pattern=_IMAGE_PATTERN)
    network_disabled: Literal[True] = True
    read_only_root: Literal[True] = True
    repository_mounted: Literal[False] = False
    secrets_injected: Literal[False] = False
    host_write_allowed: Literal[False] = False
    execution_passed: Literal[True] = True
    return_code: Literal[0] = 0
    output_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_truncated: Literal[False] = False
    started_at: AwareDatetime
    finished_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _receipt_is_canonical(self) -> "SandboxAuthoringReceipt":
        if self.finished_at < self.started_at:
            raise ValueError("sandbox authoring receipt finishes before it starts")
        if self.authored_implementation_sha256s != tuple(
            sorted(set(self.authored_implementation_sha256s))
        ):
            raise ValueError("authored implementation hashes must be unique and sorted")
        if self.source_artifacts != tuple(
            sorted(self.source_artifacts, key=lambda item: item.name)
        ):
            raise ValueError("sandbox source artifacts must be sorted by name")
        if len({item.name for item in self.source_artifacts}) != len(self.source_artifacts):
            raise ValueError("sandbox source artifact names must be unique")
        source_index = tuple(item.model_dump(mode="json") for item in self.source_artifacts)
        if self.source_bundle_sha256 != content_sha256(source_index):
            raise ValueError("sandbox source bundle hash is invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def build_sandbox_authoring_receipt(
    *,
    provisional_manifest: ExperimentCapabilityManifest,
    author_principal_sha256: str,
    source_files: Mapping[str, str | bytes],
    source_review_sha256: str,
    execution: SandboxExecution,
    success_sentinel: str,
    started_at: datetime,
    finished_at: datetime,
) -> SandboxAuthoringReceipt:
    """Convert an actual hard-sandbox result into immutable authoring evidence.

    Authority comes from the subsequent sandbox-controller signature, not from constructing this
    value.  Local-dev results, mutable image tags, truncated output, missing sentinels, and failed
    executions cannot produce a promotable receipt.
    """

    if provisional_manifest.lifecycle is not CapabilityLifecycle.PROVISIONAL:
        raise CapabilityPromotionError("sandbox authoring requires a provisional manifest")
    if not execution.ok or execution.returncode != 0:
        raise CapabilityPromotionError("capability authoring probe did not complete successfully")
    if execution.image_id is None or not execution.image_id.startswith("sha256:"):
        raise CapabilityPromotionError("capability authoring did not use an immutable Docker image")
    if execution.output_truncated:
        raise CapabilityPromotionError("truncated capability authoring output is not promotable")
    if not success_sentinel.strip() or success_sentinel not in execution.output:
        raise CapabilityPromotionError("capability authoring success sentinel is absent")
    if not source_files:
        raise CapabilityPromotionError("capability authoring source bundle is empty")
    artifacts = []
    for name, value in sorted(source_files.items()):
        payload = value if isinstance(value, bytes) else value.encode("utf-8")
        artifacts.append(
            CapabilitySourceArtifact(
                name=name,
                sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),
            )
        )
    implementation_hashes = tuple(
        sorted(
            {
                role.implementation_sha256
                for role in provisional_manifest.roles
                if role.agent_authored and role.role is not CapabilityRole.PLANNER
            }
        )
    )
    if not implementation_hashes:
        raise CapabilityPromotionError("provisional manifest has no executable AI-authored role")
    return SandboxAuthoringReceipt(
        provisional_manifest_sha256=provisional_manifest.manifest_sha256,
        author_principal_sha256=author_principal_sha256,
        authored_implementation_sha256s=implementation_hashes,
        source_artifacts=tuple(artifacts),
        source_bundle_sha256=content_sha256(
            tuple(item.model_dump(mode="json") for item in artifacts)
        ),
        source_review_sha256=source_review_sha256,
        sandbox_image_id=execution.image_id,
        output_sha256=hashlib.sha256(execution.output.encode("utf-8")).hexdigest(),
        started_at=started_at,
        finished_at=finished_at,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_provisional_capability_authoring(
    *,
    provisional_manifest: ExperimentCapabilityManifest,
    author_principal_sha256: str,
    source_files: Mapping[str, str | bytes],
    script_name: str,
    source_review_sha256: str,
    success_sentinel: str,
    timeout_s: float,
    image_id: str | None = None,
) -> SandboxAuthoringReceipt:
    """Execute a provisional authoring probe through the production Docker boundary.

    This convenience entry point deliberately hard-codes ``backend="docker"``.  The returned
    receipt still requires a trusted sandbox-controller attestation before promotion.
    """

    started_at = _utc_now()
    execution = execute_python_files(
        source_files,
        script_name=script_name,
        timeout_s=timeout_s,
        backend="docker",
        image_id=image_id,
    )
    finished_at = _utc_now()
    return build_sandbox_authoring_receipt(
        provisional_manifest=provisional_manifest,
        author_principal_sha256=author_principal_sha256,
        source_files=source_files,
        source_review_sha256=source_review_sha256,
        execution=execution,
        success_sentinel=success_sentinel,
        started_at=started_at,
        finished_at=finished_at,
    )


class GeneratedCapabilityTestSuiteReceipt(FrozenModel):
    schema_name: Literal["aletheia.generated_capability_test_suite_receipt"] = (
        "aletheia.generated_capability_test_suite_receipt"
    )
    schema_version: Literal[1] = 1
    provisional_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_authoring_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    test_generator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    test_suite_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_fixtures_sha256: str = Field(pattern=_SHA256_PATTERN)
    adversarial_fixtures_sha256: str = Field(pattern=_SHA256_PATTERN)
    positive_control_fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    negative_control_fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_case_count: int = Field(ge=1)
    adversarial_case_count: int = Field(ge=1)
    positive_control_case_count: int = Field(ge=1)
    negative_control_case_count: int = Field(ge=1)
    generated_in_hard_sandbox: Literal[True] = True
    sandbox_image_id: str = Field(pattern=_IMAGE_PATTERN)
    network_disabled: Literal[True] = True
    frozen_at: AwareDatetime
    state: Literal["frozen_before_validation"] = "frozen_before_validation"

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class CapabilityControlExecutionReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    control_kind: Literal[ControlKind.POSITIVE, ControlKind.NEGATIVE]
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    passed: Literal[True] = True

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class IndependentCapabilityValidationReceipt(FrozenModel):
    schema_name: Literal["aletheia.independent_capability_validation_receipt"] = (
        "aletheia.independent_capability_validation_receipt"
    )
    schema_version: Literal[1] = 1
    provisional_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_authoring_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    generated_test_suite_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    test_suite_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_agent_authored: Literal[False] = False
    execution_boundary: Literal[CapabilityBoundary.HARD_SANDBOX] = CapabilityBoundary.HARD_SANDBOX
    sandbox_image_id: str = Field(pattern=_IMAGE_PATTERN)
    network_disabled: Literal[True] = True
    reference_cases_total: int = Field(ge=1)
    reference_cases_passed: int = Field(ge=1)
    adversarial_cases_total: int = Field(ge=1)
    adversarial_cases_passed: int = Field(ge=1)
    positive_control: CapabilityControlExecutionReceipt
    negative_control: CapabilityControlExecutionReceipt
    exact_reexecution_count: int = Field(ge=1)
    independent_recomputation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    reproduction_policy_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    independent_implementation_verified: bool
    independent_dataset_verified: bool
    failures: tuple[str, ...] = ()
    started_at: AwareDatetime
    validated_at: AwareDatetime
    state: Literal["validated"] = "validated"

    @model_validator(mode="after")
    def _validation_is_complete(self) -> "IndependentCapabilityValidationReceipt":
        if self.validated_at < self.started_at:
            raise ValueError("capability validation finishes before it starts")
        if self.reference_cases_passed != self.reference_cases_total:
            raise ValueError("not every reference capability case passed")
        if self.adversarial_cases_passed != self.adversarial_cases_total:
            raise ValueError("not every adversarial capability case passed")
        if self.positive_control.control_kind is not ControlKind.POSITIVE:
            raise ValueError("positive capability control has the wrong kind")
        if self.negative_control.control_kind is not ControlKind.NEGATIVE:
            raise ValueError("negative capability control has the wrong kind")
        if self.failures:
            raise ValueError("validated capability receipt cannot retain failed cases")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class DomainCapabilityReviewReceipt(FrozenModel):
    schema_name: Literal["aletheia.domain_capability_review_receipt"] = (
        "aletheia.domain_capability_review_receipt"
    )
    schema_version: Literal[1] = 1
    provisional_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    independent_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    approved_claim_types: tuple[str, ...] = Field(min_length=1)
    approved_maximum_evidence_level: CapabilityEvidenceLevel
    safety_review_sha256: str = Field(pattern=_SHA256_PATTERN)
    domain_review_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_scope_approved: Literal[True] = True
    measurement_scope_approved: Literal[True] = True
    claim_scope_approved: Literal[True] = True
    approved: Literal[True] = True
    reviewed_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _claims_are_canonical(self) -> "DomainCapabilityReviewReceipt":
        if self.approved_claim_types != tuple(sorted(set(self.approved_claim_types))):
            raise ValueError("approved capability claim types must be unique and sorted")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class CapabilityPromotionRequest(FrozenModel):
    schema_name: Literal["aletheia.capability_promotion_request"] = (
        "aletheia.capability_promotion_request"
    )
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=_ID_PATTERN)
    promotion_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_manifest: ExperimentCapabilityManifest
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_authoring: SandboxAuthoringReceipt
    sandbox_attestation: SignedPromotionArtifact
    generated_test_suite: GeneratedCapabilityTestSuiteReceipt
    test_suite_attestation: SignedPromotionArtifact
    independent_validation: IndependentCapabilityValidationReceipt
    validation_attestation: SignedPromotionArtifact
    domain_review: DomainCapabilityReviewReceipt
    domain_review_attestation: SignedPromotionArtifact
    independent_validator_binding: CapabilityRoleBinding
    target_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    target_maximum_evidence_level: CapabilityEvidenceLevel
    requested_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _request_is_exact_and_role_separated(self) -> "CapabilityPromotionRequest":
        manifest = self.source_manifest
        if manifest.lifecycle is not CapabilityLifecycle.PROVISIONAL:
            raise ValueError("promotion source manifest must be provisional")
        if self.source_manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("promotion request source manifest hash is invalid")
        receipt_bindings = (
            self.sandbox_authoring.provisional_manifest_sha256,
            self.generated_test_suite.provisional_manifest_sha256,
            self.independent_validation.provisional_manifest_sha256,
            self.domain_review.provisional_manifest_sha256,
        )
        if any(item != manifest.manifest_sha256 for item in receipt_bindings):
            raise ValueError("promotion evidence binds another provisional manifest")
        if (
            self.generated_test_suite.sandbox_authoring_receipt_sha256
            != self.sandbox_authoring.receipt_sha256
            or self.independent_validation.sandbox_authoring_receipt_sha256
            != self.sandbox_authoring.receipt_sha256
            or self.independent_validation.generated_test_suite_receipt_sha256
            != self.generated_test_suite.receipt_sha256
            or self.independent_validation.test_suite_sha256
            != self.generated_test_suite.test_suite_sha256
            or self.domain_review.independent_validation_receipt_sha256
            != self.independent_validation.receipt_sha256
        ):
            raise ValueError("promotion evidence chain is not content-addressed end to end")
        if (
            self.independent_validation.reference_cases_total
            != self.generated_test_suite.reference_case_count
            or self.independent_validation.adversarial_cases_total
            != self.generated_test_suite.adversarial_case_count
        ):
            raise ValueError("validation case counts differ from the frozen generated suite")
        if (
            self.independent_validation.positive_control.fixture_sha256
            != self.generated_test_suite.positive_control_fixture_sha256
            or self.independent_validation.negative_control.fixture_sha256
            != self.generated_test_suite.negative_control_fixture_sha256
        ):
            raise ValueError("validation controls differ from the frozen generated fixtures")
        expected_attestations = (
            (
                self.sandbox_attestation,
                PromotionArtifactKind.SANDBOX_AUTHORING,
                self.sandbox_authoring.receipt_sha256,
            ),
            (
                self.test_suite_attestation,
                PromotionArtifactKind.GENERATED_TEST_SUITE,
                self.generated_test_suite.receipt_sha256,
            ),
            (
                self.validation_attestation,
                PromotionArtifactKind.INDEPENDENT_VALIDATION,
                self.independent_validation.receipt_sha256,
            ),
            (
                self.domain_review_attestation,
                PromotionArtifactKind.DOMAIN_REVIEW,
                self.domain_review.receipt_sha256,
            ),
        )
        for envelope, kind, digest in expected_attestations:
            if (
                envelope.artifact_kind is not kind
                or envelope.artifact_sha256 != digest
                or envelope.promotion_policy_sha256 != self.promotion_policy_sha256
                or envelope.capability_id != manifest.capability_id
                or envelope.domain != manifest.domain
            ):
                raise ValueError("promotion attestation does not bind its exact evidence artifact")

        author = self.sandbox_authoring.author_principal_sha256
        generator = self.generated_test_suite.test_generator_principal_sha256
        validator = self.independent_validation.validator_principal_sha256
        reviewer = self.domain_review.reviewer_principal_sha256
        if len({author, generator, validator, reviewer}) != 4:
            raise ValueError("author, test generator, validator, and domain reviewer must differ")
        source_role_principals = {item.principal_sha256 for item in manifest.roles}
        if author not in source_role_principals:
            raise ValueError("sandbox author is not bound to a provisional capability role")
        if {generator, validator, reviewer} & source_role_principals:
            raise ValueError("promotion test/review roles must be independent of source roles")
        if generator not in {
            item.principal_sha256 for item in self.test_suite_attestation.signatures
        }:
            raise ValueError("generated test suite lacks its generator attestation")
        if validator not in {
            item.principal_sha256 for item in self.validation_attestation.signatures
        }:
            raise ValueError("capability validation lacks its validator attestation")
        if reviewer not in {
            item.principal_sha256 for item in self.domain_review_attestation.signatures
        }:
            raise ValueError("domain review lacks its reviewer attestation")

        expected_implementations = tuple(
            sorted(
                {
                    role.implementation_sha256
                    for role in manifest.roles
                    if role.agent_authored and role.role is not CapabilityRole.PLANNER
                }
            )
        )
        if self.sandbox_authoring.authored_implementation_sha256s != expected_implementations:
            raise ValueError("sandbox receipt does not cover every executable AI-authored role")
        for role in manifest.roles:
            if (
                role.agent_authored
                and role.role is not CapabilityRole.PLANNER
                and role.boundary
                not in {CapabilityBoundary.HARD_SANDBOX, CapabilityBoundary.DIGEST_PINNED_CONTAINER}
            ):
                raise ValueError("executable AI-authored capability role lacks a hard boundary")

        binding = self.independent_validator_binding
        if binding.role is not CapabilityRole.VALIDATOR:
            raise ValueError("promotion validator binding has the wrong role")
        if binding.agent_authored:
            raise ValueError("an AI-authored validator cannot promote its own capability")
        if binding.principal_sha256 != validator:
            raise ValueError("validator binding principal differs from the validation receipt")
        if (
            binding.implementation_sha256
            != self.independent_validation.validator_implementation_sha256
        ):
            raise ValueError("validator binding implementation differs from the validation receipt")
        if binding.frozen_at > self.independent_validation.started_at:
            raise ValueError("independent validator was not frozen before validation")
        executor = next(item for item in manifest.roles if item.role is CapabilityRole.EXECUTOR)
        if binding.adapter_ref == executor.adapter_ref:
            raise ValueError("independent validator cannot reuse the capability executor")

        source_version = manifest.semantic_version
        target_version = _semantic_version(self.target_version)
        if target_version <= source_version or target_version[0] != source_version[0]:
            raise ValueError("promotion requires a higher, contract-compatible version")
        if self.target_maximum_evidence_level is CapabilityEvidenceLevel.EXPLORATORY:
            raise ValueError("registered successor must exceed exploratory evidence")
        if evidence_level_rank(self.target_maximum_evidence_level) > evidence_level_rank(
            self.domain_review.approved_maximum_evidence_level
        ):
            raise ValueError("promotion exceeds the independently reviewed evidence level")
        supported_claims = {item.value for item in manifest.claim_types_supported}
        if not supported_claims.issubset(self.domain_review.approved_claim_types):
            raise ValueError("promotion exceeds the independently reviewed claim scope")

        if self.sandbox_authoring.finished_at > self.generated_test_suite.frozen_at:
            raise ValueError("generated tests were frozen before sandbox authoring completed")
        if self.generated_test_suite.frozen_at > self.independent_validation.started_at:
            raise ValueError("validation started before generated tests were frozen")
        if self.independent_validation.validated_at > self.domain_review.reviewed_at:
            raise ValueError("domain review predates independent validation")
        if self.domain_review.reviewed_at > self.requested_at:
            raise ValueError("promotion request predates its domain review")
        if manifest.frozen_at > self.sandbox_authoring.started_at:
            raise ValueError("sandbox authoring predates the provisional manifest")
        artifact_times = (
            (self.sandbox_attestation.issued_at, self.sandbox_authoring.finished_at),
            (self.test_suite_attestation.issued_at, self.generated_test_suite.frozen_at),
            (self.validation_attestation.issued_at, self.independent_validation.validated_at),
            (self.domain_review_attestation.issued_at, self.domain_review.reviewed_at),
        )
        if any(signature_time < artifact_time for signature_time, artifact_time in artifact_times):
            raise ValueError("promotion attestation predates the artifact it claims to sign")
        if any(signature_time > self.requested_at for signature_time, _ in artifact_times):
            raise ValueError("promotion request predates one of its evidence attestations")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


_AUDIT_CHECKS = tuple(
    sorted(
        (
            "ai_code_hard_sandboxed",
            "append_only_source_is_latest",
            "domain_scope_approved",
            "generated_tests_frozen_before_validation",
            "independent_recomputation_complete",
            "independent_validator_bound",
            "positive_and_negative_controls_passed",
            "role_permissions_and_signatures_valid",
            "source_registry_exact",
        )
    )
)


class CapabilityPromotionAuditReceipt(FrozenModel):
    schema_name: Literal["aletheia.capability_promotion_audit_receipt"] = (
        "aletheia.capability_promotion_audit_receipt"
    )
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    promotion_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    auditor_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    checks: tuple[str, ...]
    blockers: tuple[str, ...]
    decision: PromotionDecision
    audited_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _audit_is_canonical(self) -> "CapabilityPromotionAuditReceipt":
        if self.auditor_principal_sha256s != tuple(sorted(set(self.auditor_principal_sha256s))):
            raise ValueError("promotion auditor principals must be unique and sorted")
        if self.checks != tuple(sorted(set(self.checks))):
            raise ValueError("promotion audit checks must be unique and sorted")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("promotion audit blockers must be unique and sorted")
        if (self.decision is PromotionDecision.APPROVED) != (not self.blockers):
            raise ValueError("promotion audit decision disagrees with its blockers")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class SignedCapabilityPromotionAudit(FrozenModel):
    schema_version: Literal[1] = 1
    audit: CapabilityPromotionAuditReceipt
    attestation: SignedPromotionArtifact

    @model_validator(mode="after")
    def _audit_attestation_matches(self) -> "SignedCapabilityPromotionAudit":
        if (
            self.attestation.artifact_kind is not PromotionArtifactKind.PROMOTION_AUDIT
            or self.attestation.artifact_sha256 != self.audit.receipt_sha256
            or self.attestation.promotion_policy_sha256 != self.audit.promotion_policy_sha256
        ):
            raise ValueError("promotion audit signature does not bind the audit receipt")
        return self

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)


def _attestation_blocker(
    *,
    label: str,
    envelope: SignedPromotionArtifact,
    policy: CapabilityPromotionPolicy,
) -> str | None:
    try:
        verify_promotion_artifact(envelope=envelope, policy=policy)
    except CapabilityPromotionError:
        return f"{label}_attestation_invalid"
    return None


def _promotion_blockers(
    *,
    snapshot: CapabilityRegistrySnapshot,
    policy: CapabilityPromotionPolicy,
    request: CapabilityPromotionRequest,
    audited_at: datetime,
) -> tuple[str, ...]:
    blockers: list[str] = []
    manifest = request.source_manifest
    if snapshot.registry_id != policy.registry_id:
        blockers.append("registry_id_mismatch")
    if snapshot.snapshot_sha256 != policy.source_registry_sha256:
        blockers.append("policy_source_registry_mismatch")
    if request.source_registry_sha256 != snapshot.snapshot_sha256:
        blockers.append("request_source_registry_mismatch")
    if policy.frozen_at < snapshot.created_at:
        blockers.append("promotion_policy_predates_source_registry")
    matches = [
        item
        for item in snapshot.manifests
        if item.capability_id == manifest.capability_id
        and item.manifest_sha256 == manifest.manifest_sha256
    ]
    if len(matches) != 1:
        blockers.append("source_manifest_not_in_registry")
    chain = [item for item in snapshot.manifests if item.capability_id == manifest.capability_id]
    if not chain or chain[-1].manifest_sha256 != manifest.manifest_sha256:
        blockers.append("source_manifest_not_latest")
    if request.promotion_policy_sha256 != policy.policy_sha256:
        blockers.append("request_policy_mismatch")
    for label, envelope in (
        ("sandbox", request.sandbox_attestation),
        ("test_suite", request.test_suite_attestation),
        ("validation", request.validation_attestation),
        ("domain_review", request.domain_review_attestation),
    ):
        blocker = _attestation_blocker(label=label, envelope=envelope, policy=policy)
        if blocker is not None:
            blockers.append(blocker)
    if request.sandbox_authoring.sandbox_image_id not in policy.allowed_sandbox_image_ids:
        blockers.append("authoring_sandbox_image_not_allowed")
    if request.generated_test_suite.sandbox_image_id not in policy.allowed_sandbox_image_ids:
        blockers.append("test_generation_sandbox_image_not_allowed")
    if request.independent_validation.sandbox_image_id not in policy.allowed_sandbox_image_ids:
        blockers.append("validation_sandbox_image_not_allowed")
    reproduction = manifest.reproduction_policy
    validation = request.independent_validation
    if validation.exact_reexecution_count < reproduction.minimum_exact_reexecutions:
        blockers.append("exact_reexecution_requirement_not_met")
    if (
        reproduction.independent_implementation_required
        and not validation.independent_implementation_verified
    ):
        blockers.append("independent_implementation_requirement_not_met")
    if reproduction.independent_dataset_required and not validation.independent_dataset_verified:
        blockers.append("independent_dataset_requirement_not_met")
    if audited_at < request.requested_at:
        blockers.append("promotion_audit_predates_request")
    if not (policy.frozen_at <= audited_at <= policy.expires_at):
        blockers.append("promotion_audit_outside_policy_window")
    actor_principals = {
        request.sandbox_authoring.author_principal_sha256,
        request.generated_test_suite.test_generator_principal_sha256,
        request.independent_validation.validator_principal_sha256,
        request.domain_review.reviewer_principal_sha256,
        *(item.principal_sha256 for item in manifest.roles),
    }
    sandbox_principals = {item.principal_sha256 for item in request.sandbox_attestation.signatures}
    if sandbox_principals & actor_principals:
        blockers.append("sandbox_controller_not_role_independent")
    return tuple(sorted(set(blockers)))


def audit_capability_promotion(
    *,
    snapshot: CapabilityRegistrySnapshot,
    policy: CapabilityPromotionPolicy,
    request: CapabilityPromotionRequest,
    auditor_private_keys: Mapping[str, bytes],
    audited_at: datetime,
) -> SignedCapabilityPromotionAudit:
    """Independently audit a frozen request and sign either approval or rejection."""

    snapshot = CapabilityRegistrySnapshot.model_validate(snapshot.model_dump(mode="python"))
    policy = CapabilityPromotionPolicy.model_validate(policy.model_dump(mode="python"))
    request = CapabilityPromotionRequest.model_validate(request.model_dump(mode="python"))
    manifest = request.source_manifest
    auditor_principals = _signer_principals(
        policy=policy,
        permission=PromotionPermission.PROMOTION_AUDIT,
        signer_private_keys=auditor_private_keys,
        domain=manifest.domain,
        capability_id=manifest.capability_id,
        issued_at=audited_at,
    )
    occupied = {
        request.sandbox_authoring.author_principal_sha256,
        request.generated_test_suite.test_generator_principal_sha256,
        request.independent_validation.validator_principal_sha256,
        request.domain_review.reviewer_principal_sha256,
        *(item.principal_sha256 for item in manifest.roles),
    }
    blockers = list(
        _promotion_blockers(
            snapshot=snapshot,
            policy=policy,
            request=request,
            audited_at=audited_at,
        )
    )
    if set(auditor_principals) & occupied:
        blockers.append("promotion_auditor_not_role_independent")
    canonical_blockers = tuple(sorted(set(blockers)))
    audit = CapabilityPromotionAuditReceipt(
        request_sha256=request.request_sha256,
        source_registry_sha256=snapshot.snapshot_sha256,
        promotion_policy_sha256=policy.policy_sha256,
        auditor_principal_sha256s=auditor_principals,
        checks=_AUDIT_CHECKS,
        blockers=canonical_blockers,
        decision=(
            PromotionDecision.APPROVED if not canonical_blockers else PromotionDecision.REJECTED
        ),
        audited_at=audited_at,
    )
    attestation = sign_promotion_artifact(
        policy=policy,
        artifact_kind=PromotionArtifactKind.PROMOTION_AUDIT,
        artifact_sha256=audit.receipt_sha256,
        capability_id=manifest.capability_id,
        domain=manifest.domain,
        issued_at=audited_at,
        signer_private_keys=auditor_private_keys,
    )
    return SignedCapabilityPromotionAudit(audit=audit, attestation=attestation)


def _registered_successor(
    *,
    request: CapabilityPromotionRequest,
    audit: CapabilityPromotionAuditReceipt,
    promoted_at: datetime,
) -> ExperimentCapabilityManifest:
    source = request.source_manifest
    roles = tuple(
        request.independent_validator_binding if item.role is CapabilityRole.VALIDATOR else item
        for item in source.roles
    )
    validation = request.independent_validation
    tests = request.generated_test_suite
    review = request.domain_review
    registration = CapabilityRegistrationEvidence(
        reference_fixtures_sha256=tests.reference_fixtures_sha256,
        adversarial_fixtures_sha256=tests.adversarial_fixtures_sha256,
        positive_control_receipt_sha256=validation.positive_control.receipt_sha256,
        negative_control_receipt_sha256=validation.negative_control.receipt_sha256,
        independent_recomputation_receipt_sha256=(
            validation.independent_recomputation_receipt_sha256
        ),
        reproduction_policy_evidence_sha256=validation.reproduction_policy_evidence_sha256,
        safety_review_sha256=review.safety_review_sha256,
        domain_review_receipt_sha256=review.receipt_sha256,
        domain_reviewer_principal_sha256=review.reviewer_principal_sha256,
        promotion_auditor_principal_sha256=audit.auditor_principal_sha256s[0],
        reviewed_at=audit.audited_at,
    )
    payload = source.model_dump(mode="python")
    payload.update(
        {
            "version": request.target_version,
            "lifecycle": CapabilityLifecycle.REGISTERED,
            "maximum_evidence_level": request.target_maximum_evidence_level,
            "roles": roles,
            "supersedes_manifest_sha256": source.manifest_sha256,
            "registration_evidence": registration,
            "frozen_at": promoted_at,
        }
    )
    return ExperimentCapabilityManifest.model_validate(payload)


class CapabilityPromotionReceipt(FrozenModel):
    schema_name: Literal["aletheia.capability_promotion_receipt"] = (
        "aletheia.capability_promotion_receipt"
    )
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    audit_envelope_sha256: str = Field(pattern=_SHA256_PATTERN)
    promotion_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    registered_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    promoter_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    promoted_at: AwareDatetime
    state: Literal["committed"] = "committed"

    @model_validator(mode="after")
    def _promoters_are_canonical(self) -> "CapabilityPromotionReceipt":
        if self.promoter_principal_sha256s != tuple(sorted(set(self.promoter_principal_sha256s))):
            raise ValueError("registry promoter principals must be unique and sorted")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class SignedCapabilityRegistryUpdate(FrozenModel):
    schema_name: Literal["aletheia.signed_capability_registry_update"] = (
        "aletheia.signed_capability_registry_update"
    )
    schema_version: Literal[1] = 1
    target_snapshot: CapabilityRegistrySnapshot
    promotion_receipt: CapabilityPromotionReceipt
    registry_attestation: SignedPromotionArtifact

    @model_validator(mode="after")
    def _update_is_bound(self) -> "SignedCapabilityRegistryUpdate":
        if self.promotion_receipt.target_registry_sha256 != self.target_snapshot.snapshot_sha256:
            raise ValueError("promotion receipt does not bind the target registry")
        if (
            self.registry_attestation.artifact_kind is not PromotionArtifactKind.REGISTRY_UPDATE
            or self.registry_attestation.artifact_sha256 != self.promotion_receipt.receipt_sha256
            or self.registry_attestation.promotion_policy_sha256
            != self.promotion_receipt.promotion_policy_sha256
        ):
            raise ValueError("registry signature does not bind the promotion receipt")
        return self

    @property
    def update_sha256(self) -> str:
        return content_sha256(self)


def _verify_signed_audit(
    *,
    signed_audit: SignedCapabilityPromotionAudit,
    request: CapabilityPromotionRequest,
    policy: CapabilityPromotionPolicy,
) -> tuple[str, ...]:
    signed_audit = SignedCapabilityPromotionAudit.model_validate(
        signed_audit.model_dump(mode="python")
    )
    audit = signed_audit.audit
    if (
        audit.request_sha256 != request.request_sha256
        or audit.promotion_policy_sha256 != policy.policy_sha256
        or audit.source_registry_sha256 != request.source_registry_sha256
    ):
        raise CapabilityPromotionError("promotion audit does not bind the request and policy")
    if audit.checks != _AUDIT_CHECKS:
        raise CapabilityPromotionError("promotion audit omits required checks")
    if (
        signed_audit.attestation.capability_id != request.source_manifest.capability_id
        or signed_audit.attestation.domain != request.source_manifest.domain
    ):
        raise CapabilityPromotionError("promotion audit signature has the wrong capability scope")
    principals = verify_promotion_artifact(envelope=signed_audit.attestation, policy=policy)
    if principals != audit.auditor_principal_sha256s:
        raise CapabilityPromotionError("promotion audit principals differ from its signatures")
    return principals


def promote_capability_registry(
    *,
    source_snapshot: CapabilityRegistrySnapshot,
    policy: CapabilityPromotionPolicy,
    request: CapabilityPromotionRequest,
    signed_audit: SignedCapabilityPromotionAudit,
    promoter_private_keys: Mapping[str, bytes],
    promoted_at: datetime,
) -> SignedCapabilityRegistryUpdate:
    """Append one registered successor and issue a separately authorized registry receipt."""

    source_snapshot = CapabilityRegistrySnapshot.model_validate(
        source_snapshot.model_dump(mode="python")
    )
    policy = CapabilityPromotionPolicy.model_validate(policy.model_dump(mode="python"))
    request = CapabilityPromotionRequest.model_validate(request.model_dump(mode="python"))
    signed_audit = SignedCapabilityPromotionAudit.model_validate(
        signed_audit.model_dump(mode="python")
    )
    _verify_signed_audit(signed_audit=signed_audit, request=request, policy=policy)
    blockers = _promotion_blockers(
        snapshot=source_snapshot,
        policy=policy,
        request=request,
        audited_at=signed_audit.audit.audited_at,
    )
    if signed_audit.audit.decision is not PromotionDecision.APPROVED or blockers:
        raise CapabilityPromotionError("rejected capability promotion cannot update the registry")
    if promoted_at < signed_audit.audit.audited_at:
        raise CapabilityPromotionError("registry promotion predates its independent audit")
    if promoted_at > policy.expires_at:
        raise CapabilityPromotionError("registry promotion occurs after policy expiry")
    manifest = _registered_successor(
        request=request,
        audit=signed_audit.audit,
        promoted_at=promoted_at,
    )
    target = build_capability_registry_snapshot(
        registry_id=source_snapshot.registry_id,
        manifests=(*source_snapshot.manifests, manifest),
        created_at=promoted_at,
    )
    promoter_principals = _signer_principals(
        policy=policy,
        permission=PromotionPermission.REGISTRY_PROMOTE,
        signer_private_keys=promoter_private_keys,
        domain=manifest.domain,
        capability_id=manifest.capability_id,
        issued_at=promoted_at,
    )
    occupied = {
        request.sandbox_authoring.author_principal_sha256,
        request.generated_test_suite.test_generator_principal_sha256,
        request.independent_validation.validator_principal_sha256,
        request.domain_review.reviewer_principal_sha256,
        *signed_audit.audit.auditor_principal_sha256s,
        *(item.principal_sha256 for item in request.source_manifest.roles),
    }
    if set(promoter_principals) & occupied:
        raise CapabilityPromotionError("registry promoter is not role-independent")
    receipt = CapabilityPromotionReceipt(
        request_sha256=request.request_sha256,
        audit_envelope_sha256=signed_audit.envelope_sha256,
        promotion_policy_sha256=policy.policy_sha256,
        source_registry_sha256=source_snapshot.snapshot_sha256,
        target_registry_sha256=target.snapshot_sha256,
        source_manifest_sha256=request.source_manifest.manifest_sha256,
        registered_manifest_sha256=manifest.manifest_sha256,
        promoter_principal_sha256s=promoter_principals,
        promoted_at=promoted_at,
    )
    attestation = sign_promotion_artifact(
        policy=policy,
        artifact_kind=PromotionArtifactKind.REGISTRY_UPDATE,
        artifact_sha256=receipt.receipt_sha256,
        capability_id=manifest.capability_id,
        domain=manifest.domain,
        issued_at=promoted_at,
        signer_private_keys=promoter_private_keys,
    )
    update = SignedCapabilityRegistryUpdate(
        target_snapshot=target,
        promotion_receipt=receipt,
        registry_attestation=attestation,
    )
    verify_capability_registry_update(
        update=update,
        source_snapshot=source_snapshot,
        policy=policy,
        request=request,
        signed_audit=signed_audit,
    )
    return update


def verify_capability_registry_update(
    *,
    update: SignedCapabilityRegistryUpdate,
    source_snapshot: CapabilityRegistrySnapshot,
    policy: CapabilityPromotionPolicy,
    request: CapabilityPromotionRequest,
    signed_audit: SignedCapabilityPromotionAudit,
) -> CapabilityRegistrySnapshot:
    """Verify signatures and reconstruct the only authorized append-only target snapshot."""

    update = SignedCapabilityRegistryUpdate.model_validate(update.model_dump(mode="python"))
    source_snapshot = CapabilityRegistrySnapshot.model_validate(
        source_snapshot.model_dump(mode="python")
    )
    policy = CapabilityPromotionPolicy.model_validate(policy.model_dump(mode="python"))
    request = CapabilityPromotionRequest.model_validate(request.model_dump(mode="python"))
    signed_audit = SignedCapabilityPromotionAudit.model_validate(
        signed_audit.model_dump(mode="python")
    )
    _verify_signed_audit(signed_audit=signed_audit, request=request, policy=policy)
    receipt = update.promotion_receipt
    if (
        source_snapshot.snapshot_sha256 != policy.source_registry_sha256
        or receipt.source_registry_sha256 != source_snapshot.snapshot_sha256
        or receipt.request_sha256 != request.request_sha256
        or receipt.audit_envelope_sha256 != signed_audit.envelope_sha256
        or receipt.promotion_policy_sha256 != policy.policy_sha256
    ):
        raise CapabilityPromotionError(
            "registry update source, request, audit, or policy is invalid"
        )
    if (
        update.registry_attestation.capability_id != request.source_manifest.capability_id
        or update.registry_attestation.domain != request.source_manifest.domain
    ):
        raise CapabilityPromotionError("registry signature has the wrong capability scope")
    promoter_principals = verify_promotion_artifact(
        envelope=update.registry_attestation, policy=policy
    )
    if promoter_principals != receipt.promoter_principal_sha256s:
        raise CapabilityPromotionError("registry promoter principals differ from signatures")
    if signed_audit.audit.decision is not PromotionDecision.APPROVED:
        raise CapabilityPromotionError("registry update is based on a rejected audit")
    expected_manifest = _registered_successor(
        request=request,
        audit=signed_audit.audit,
        promoted_at=receipt.promoted_at,
    )
    expected_snapshot = build_capability_registry_snapshot(
        registry_id=source_snapshot.registry_id,
        manifests=(*source_snapshot.manifests, expected_manifest),
        created_at=receipt.promoted_at,
    )
    if expected_snapshot != update.target_snapshot:
        raise CapabilityPromotionError("registry update is not the exact authorized append")
    if receipt.registered_manifest_sha256 != expected_manifest.manifest_sha256:
        raise CapabilityPromotionError("promotion receipt registered manifest hash is invalid")
    if receipt.target_registry_sha256 != expected_snapshot.snapshot_sha256:
        raise CapabilityPromotionError("promotion receipt target registry hash is invalid")
    return expected_snapshot


class CapabilityPromotionCandidateReadiness(FrozenModel):
    schema_version: Literal[1] = 1
    capability_id: str = Field(pattern=_ID_PATTERN)
    version: str
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    lifecycle: CapabilityLifecycle
    validator_agent_authored: bool
    promotion_ready: bool
    blockers: tuple[str, ...]


class CapabilityPromotionReadinessAudit(FrozenModel):
    schema_name: Literal["aletheia.capability_promotion_readiness_audit"] = (
        "aletheia.capability_promotion_readiness_audit"
    )
    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=_ID_PATTERN)
    registry_id: str = Field(pattern=_ID_PATTERN)
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidates: tuple[CapabilityPromotionCandidateReadiness, ...]
    registered_capability_count: int = Field(ge=0)
    production_promotion_ready: bool
    blockers: tuple[str, ...]
    audited_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _readiness_is_canonical(self) -> "CapabilityPromotionReadinessAudit":
        if self.candidates != tuple(sorted(self.candidates, key=lambda item: item.capability_id)):
            raise ValueError("promotion readiness candidates must be sorted")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("promotion readiness blockers must be unique and sorted")
        if self.production_promotion_ready != (not self.blockers):
            raise ValueError("promotion readiness disagrees with its blockers")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


def build_capability_promotion_readiness_audit(
    *,
    audit_id: str,
    registry: CapabilityRegistrySnapshot,
    audited_at: datetime,
) -> CapabilityPromotionReadinessAudit:
    """Report honest production gaps without manufacturing reviewers, keys, or evidence."""

    latest: dict[str, ExperimentCapabilityManifest] = {}
    for manifest in registry.manifests:
        latest[manifest.capability_id] = manifest
    candidates = []
    for capability_id, manifest in sorted(latest.items()):
        validator = next(item for item in manifest.roles if item.role is CapabilityRole.VALIDATOR)
        blockers: list[str] = []
        if manifest.lifecycle is not CapabilityLifecycle.REGISTERED:
            blockers.extend(
                (
                    "authorized_registry_update_missing",
                    "independent_domain_review_missing",
                    "independent_validation_missing",
                    "signed_promotion_audit_missing",
                    "trusted_promotion_policy_missing",
                )
            )
        if validator.agent_authored:
            blockers.append("validator_is_agent_authored")
        canonical = tuple(sorted(set(blockers)))
        candidates.append(
            CapabilityPromotionCandidateReadiness(
                capability_id=capability_id,
                version=manifest.version,
                manifest_sha256=manifest.manifest_sha256,
                lifecycle=manifest.lifecycle,
                validator_agent_authored=validator.agent_authored,
                promotion_ready=not canonical,
                blockers=canonical,
            )
        )
    global_blockers = tuple(
        sorted(
            {
                f"{candidate.capability_id}:{blocker}"
                for candidate in candidates
                for blocker in candidate.blockers
            }
        )
    )
    return CapabilityPromotionReadinessAudit(
        audit_id=audit_id,
        registry_id=registry.registry_id,
        registry_snapshot_sha256=registry.snapshot_sha256,
        candidates=tuple(candidates),
        registered_capability_count=sum(
            item.lifecycle is CapabilityLifecycle.REGISTERED for item in candidates
        ),
        production_promotion_ready=not global_blockers,
        blockers=global_blockers,
        audited_at=audited_at,
    )


__all__ = [
    "CapabilityControlExecutionReceipt",
    "CapabilityPromotionAuditReceipt",
    "CapabilityPromotionCandidateReadiness",
    "CapabilityPromotionError",
    "CapabilityPromotionPolicy",
    "CapabilityPromotionReadinessAudit",
    "CapabilityPromotionReceipt",
    "CapabilityPromotionRequest",
    "CapabilitySourceArtifact",
    "DomainCapabilityReviewReceipt",
    "GeneratedCapabilityTestSuiteReceipt",
    "IndependentCapabilityValidationReceipt",
    "PromotionArtifactKind",
    "PromotionDecision",
    "PromotionPermission",
    "PromotionRolePolicy",
    "PromotionSignature",
    "SandboxAuthoringReceipt",
    "SignedCapabilityPromotionAudit",
    "SignedCapabilityRegistryUpdate",
    "SignedPromotionArtifact",
    "TrustedPromotionKey",
    "audit_capability_promotion",
    "build_capability_promotion_readiness_audit",
    "build_sandbox_authoring_receipt",
    "ed25519_key_id",
    "ed25519_public_key_hex",
    "promote_capability_registry",
    "run_provisional_capability_authoring",
    "sign_promotion_artifact",
    "verify_capability_registry_update",
    "verify_promotion_artifact",
]
