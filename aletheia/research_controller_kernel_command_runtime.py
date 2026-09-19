"""Guarded composition of the three ARL-2 kernel-command signing services.

B4 landed the exact Kernel command authorities unhosted.  This module is
their production host: three guarded builders — admission, action,
transition — one service per authority and one pinned ordinary key per
service, each served through the existing
``scripts/run_research_controller_rpc_service.py`` entrypoint as a
single-operation RPC service.  The services face the ARL-2 driver; the
worker's operation pins acquire neither.

Everything a builder trusts arrives as pinned bytes: the composition config
(sha-pinned by the deployment manifest), the authority implementation source
(pinned to the exact ``kernel_authority.py`` file the process would import),
and the signing key (a 0o400 regular file owned by the service account).  The
wire never carries the idempotency or source-event keys; each handler derives
them through the authority's own exact-proposal convention, so a request
cannot express a rebound key.  A domain refusal becomes one signed blocker
code; every other failure fails closed without a signature.
"""

from __future__ import annotations


def build_admission_kernel_command_rpc_service(*, deployment, configuration_bytes):
    """Compose the admission kernel-command signing service (one operation, one key)."""

    return _compose(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
        domain="admission",
    )


def build_action_kernel_command_rpc_service(*, deployment, configuration_bytes):
    """Compose the action kernel-command signing service (one operation, one key)."""

    return _compose(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
        domain="action",
    )


def build_transition_kernel_command_rpc_service(*, deployment, configuration_bytes):
    """Compose the transition kernel-command signing service (one operation, one key)."""

    return _compose(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
        domain="transition",
    )


def _compose(*, deployment, configuration_bytes, domain: str):
    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from pathlib import Path
    from typing import Literal

    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.research_controller import kernel_authority as kernel_authority_module
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ActionKernelCommandRPCPayload,
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        ControllerWorkerRPCServiceBlocked,
        TransitionKernelCommandRPCPayload,
    )
    from aletheia.research_controller.kernel_authority import (
        ControllerKernelPolicyAssignment,
        ExactActionKernelAuthority,
        ExactAdmissionKernelAuthority,
        ExactTransitionKernelAuthority,
        KernelCommandAuthorityError,
    )
    from aletheia.research_kernel.policy import ResearchAuthorizationTrustRootV1
    from aletheia.research_kernel.schemas import canonical_json_bytes

    _SHA256_PATTERN = r"^[0-9a-f]{64}$"
    if domain == "admission":
        operation = ControllerWorkerRPCOperation.SIGN_ADMISSION_COMMAND
        payload_type = ActionKernelCommandRPCPayload
        authority_type = ExactAdmissionKernelAuthority
    elif domain == "action":
        operation = ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND
        payload_type = ActionKernelCommandRPCPayload
        authority_type = ExactActionKernelAuthority
    elif domain == "transition":
        operation = ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND
        payload_type = TransitionKernelCommandRPCPayload
        authority_type = ExactTransitionKernelAuthority
    else:  # pragma: no cover - the three public builders are the only callers
        raise ValueError("kernel command domain is unknown")

    class DomainSigningKeyPin(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        path: str
        file_sha256: str = Field(pattern=_SHA256_PATTERN)
        key_id: str = Field(pattern=_SHA256_PATTERN)
        owner_uid: int = Field(ge=0, le=2**31 - 1)
        group_gid: int = Field(ge=0, le=2**31 - 1)
        file_mode: Literal[0o400] = 0o400

        @model_validator(mode="after")
        def _path_is_canonical(self):
            candidate = Path(self.path)
            if (
                not candidate.is_absolute()
                or self.path != os.path.normpath(candidate)
                or self.path == "/"
            ):
                raise ValueError("kernel command signing key path is not canonical")
            return self

    class KernelCommandRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
        controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
        worker_process_principal_id: str = Field(min_length=1, max_length=192)
        service_id: str | None = Field(default=None, min_length=1, max_length=192)
        service_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
        prepared_at: AwareDatetime
        authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
        kernel_authority_source_path: str
        kernel_authority_source_sha256: str = Field(pattern=_SHA256_PATTERN)
        trust_root: ResearchAuthorizationTrustRootV1
        policy_assignments: tuple[ControllerKernelPolicyAssignment, ...] = Field(min_length=1)
        command_signing_key: DomainSigningKeyPin

        @model_validator(mode="after")
        def _authority_custody_is_isolated(self):
            key = self.command_signing_key
            if (
                Path(self.kernel_authority_source_path) == Path(key.path)
                or Path(self.kernel_authority_source_path).parent == Path(key.path).parent
            ):
                raise ValueError("kernel command authority source and key share custody")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate kernel command config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(
        path: Path,
        *,
        expected_sha256: str,
        label: str,
        expected_size: int | None = None,
        expected_owner: tuple[int, int] | None = None,
        expected_mode: int | None = None,
    ) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError(f"{label} traverses a symlink")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or not 0 < before.st_size <= 4 * 1024 * 1024
                    or (expected_size is not None and before.st_size != expected_size)
                    or (
                        expected_owner is not None
                        and (before.st_uid, before.st_gid) != expected_owner
                    )
                    or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
                ):
                    raise ValueError(f"{label} has unsafe file custody")
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError(f"{label} ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if os.read(descriptor, 1) or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(f"{label} changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(f"{label} differs from its byte pin")
        return payload

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = KernelCommandRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("kernel command RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("kernel command RPC config is not canonical JSON")

    pin = deployment.service_pin
    policy_keys = tuple(
        item.authorization_policy.key(config.authorization_key_id)
        for item in config.policy_assignments
    )
    domain_public_keys = {key.public_key_ed25519_hex for key in policy_keys} | {
        key.public_key_ed25519_hex for key in config.trust_root.commissioning_keys
    }
    domain_key_ids = {key.key_id for key in policy_keys} | {
        key.key_id for key in config.trust_root.commissioning_keys
    }
    domain_principals = {key.principal_id for key in policy_keys} | {
        key.principal_id for key in config.trust_root.commissioning_keys
    }
    if (
        pin.operations != (operation,)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.command_signing_key.key_id != config.authorization_key_id
        or config.command_signing_key.file_sha256 == deployment.receipt_private_key_sha256
        or pin.receipt_key_id in domain_key_ids
        or pin.receipt_public_key_ed25519_hex in domain_public_keys
        or pin.service_principal_id in domain_principals
        or pin.service_principal_id == config.worker_process_principal_id
        or config.command_signing_key.owner_uid != deployment.process_uid
        or config.command_signing_key.group_gid != deployment.process_gid
    ):
        raise ValueError("kernel command config differs from deployment or authority")

    authority_path = Path(config.kernel_authority_source_path)
    key_path = Path(config.command_signing_key.path)
    expected_authority_path = Path(kernel_authority_module.__file__).resolve(strict=True)
    if authority_path != expected_authority_path:
        raise ValueError("kernel command authority source is not the reviewed module")

    before_authority = fresh_regular_bytes(
        authority_path,
        expected_sha256=config.kernel_authority_source_sha256,
        label="kernel command authority implementation",
    )
    private_key = fresh_regular_bytes(
        key_path,
        expected_sha256=config.command_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.command_signing_key.owner_uid,
            config.command_signing_key.group_gid,
        ),
        expected_mode=config.command_signing_key.file_mode,
        label="kernel command signing key",
    )
    authority = authority_type(
        trust_root=config.trust_root,
        assignments=config.policy_assignments,
        authorization_key_id=config.authorization_key_id,
        private_key=private_key,
    )
    if (
        authority.public_key_ed25519_hex == pin.receipt_public_key_ed25519_hex
        or authority.principal_id == pin.service_principal_id
    ):
        raise ValueError("kernel command signing identity is not isolated from transport")

    if domain == "admission":

        def sign_command(payload):
            if type(payload) is not payload_type:
                raise TypeError("kernel command RPC handler received another payload type")
            action_sha256 = payload.submitted.action.object_ref.object_sha256
            try:
                return authority.authorize_admission(
                    proposal=payload.proposal,
                    submitted=payload.submitted,
                    # the admission command the submission carries gets its own
                    # idempotency AND source identity: the store's receipt
                    # lookup matches rows by either key, so a second command
                    # sharing the authorization's source_event_key would
                    # collide with the admission's persisted receipt
                    idempotency_key=f"action-proposed:{action_sha256}",
                    source_event_key=f"action-proposed:{action_sha256}",
                )
            except KernelCommandAuthorityError as exc:
                raise ControllerWorkerRPCServiceBlocked(
                    ("kernel_command_authority_refusal",)
                ) from exc

    elif domain == "action":

        def sign_command(payload):
            if type(payload) is not payload_type:
                raise TypeError("kernel command RPC handler received another payload type")
            action_sha256 = payload.submitted.action.object_ref.object_sha256
            try:
                return authority.authorize_action(
                    proposal=payload.proposal,
                    submitted=payload.submitted,
                    idempotency_key=f"action:{action_sha256}",
                    source_event_key=f"action-proposal:{action_sha256}",
                )
            except KernelCommandAuthorityError as exc:
                raise ControllerWorkerRPCServiceBlocked(
                    ("kernel_command_authority_refusal",)
                ) from exc

    else:

        def sign_command(payload):
            if type(payload) is not payload_type:
                raise TypeError("kernel command RPC handler received another payload type")
            decision = getattr(payload.proposal.payload, "decision", None)
            if decision is None:
                # The exact-proposal gate refuses every payload kind without a
                # transition decision; the derived key cannot exist either.
                raise ControllerWorkerRPCServiceBlocked(("kernel_command_authority_refusal",))
            try:
                return authority.authorize_transition(
                    proposal=payload.proposal,
                    submitted=payload.submitted,
                    receipt=payload.receipt,
                    incorporated_event=payload.incorporated_event,
                    idempotency_key=f"transition:{decision.decision_sha256}",
                    source_event_key=f"continuation:{payload.receipt.receipt_sha256}",
                )
            except KernelCommandAuthorityError as exc:
                raise ControllerWorkerRPCServiceBlocked(
                    ("kernel_command_authority_refusal",)
                ) from exc

    after_authority = fresh_regular_bytes(
        authority_path,
        expected_sha256=config.kernel_authority_source_sha256,
        label="kernel command authority implementation",
    )
    after_key = fresh_regular_bytes(
        key_path,
        expected_sha256=config.command_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.command_signing_key.owner_uid,
            config.command_signing_key.group_gid,
        ),
        expected_mode=config.command_signing_key.file_mode,
        label="kernel command signing key",
    )
    if before_authority != after_authority or private_key != after_key:
        raise ValueError("kernel command authority source or signing key changed")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(ControllerWorkerRPCHandlerBinding(operation=operation, handler=sign_command),),
    )


__all__ = [
    "build_admission_kernel_command_rpc_service",
    "build_action_kernel_command_rpc_service",
    "build_transition_kernel_command_rpc_service",
]
