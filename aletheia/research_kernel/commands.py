"""Pure command contracts for authoritative research-kernel mutations.

Models and discovery policies may construct :class:`ResearchCommandProposal` values, but a
proposal intentionally lacks the authority fields required by the persistence adapter.  Only an
immutable :class:`AuthorizedResearchCommand` can be converted into a committed
:class:`~aletheia.research_kernel.schemas.ResearchEvent`.

This module remains persistence-free so the scientific kernel cannot reach SQLAlchemy, legacy
drivers, schedulers, or operational outboxes through its import graph.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import AwareDatetime, Field, model_validator

from aletheia.research_kernel.policy import (
    ResearchAuthorizationError,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationRole,
    ResearchAuthorizationTrustRootV1,
    sign_authorization_message,
    verify_authorization_message,
    verify_research_authorization_policy,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    EventPayload,
    EventType,
    KernelModel,
    KernelObject,
    KernelObjectRef,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchEvent,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    canonical_json_bytes,
    canonical_sha256,
    emergency_halt_action_ref,
)

COMMAND_SCHEMA_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_PRINCIPAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"


class ResearchScopeBinding(KernelModel):
    """Immutable routing compatibility frozen when a Quest stream is created.

    The binding does not import, mirror, or independently prove a legacy graph relationship.  It
    only prevents one authoritative Quest stream from silently changing its external routing
    identity over time.
    """

    schema_name: Literal["aletheia.research_scope_binding"] = "aletheia.research_scope_binding"
    schema_version: Literal[1] = COMMAND_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    program_id: str | None = Field(default=None, pattern=_PROGRAM_ID_PATTERN)
    campaign_id: str | None = Field(default=None, pattern=_CAMPAIGN_ID_PATTERN)

    @model_validator(mode="after")
    def _campaign_is_program_scoped(self) -> "ResearchScopeBinding":
        if self.campaign_id is not None and self.program_id is None:
            raise ValueError("campaign scope requires a program scope")
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)


def _payload_reference(payload: EventPayload) -> KernelObjectRef | None:
    for field_name in (
        "charter_ref",
        "opportunity_ref",
        "problem_ref",
        "question_ref",
        "action_ref",
    ):
        value = getattr(payload, field_name, None)
        if isinstance(value, KernelObjectRef):
            return value
    return None


def directly_referenced_object(payload: EventPayload) -> KernelObjectRef | None:
    """Return the object version directly admitted by an event payload, if one exists."""

    return _payload_reference(payload)


def _validate_command_envelope(
    *,
    quest_id: str,
    scope_binding: ResearchScopeBinding,
    expected_stream_version: int,
    expected_tail_event_sha256: str | None,
    event_type: EventType,
    payload: EventPayload,
) -> None:
    if scope_binding.quest_id != quest_id:
        raise ValueError("command quest_id must equal its immutable scope binding")
    if event_type.value != payload.kind:
        raise ValueError("command event_type must match the typed payload kind")
    direct_ref = _payload_reference(payload)
    if direct_ref is not None and direct_ref.quest_id != quest_id:
        raise ValueError("command payload object belongs to another Quest")
    decision = getattr(payload, "decision", None)
    if decision is not None and decision.quest_id != quest_id:
        raise ValueError("command transition decision belongs to another Quest")
    is_genesis = event_type is EventType.CHARTER_ACTIVATED
    if (expected_stream_version == 0) != is_genesis:
        raise ValueError("only expected stream version zero may activate a charter genesis")
    if (expected_stream_version == 0) != (expected_tail_event_sha256 is None):
        raise ValueError("only expected stream version zero may omit the expected tail hash")


class ResearchCommandProposal(KernelModel):
    """A typed, content-addressed request with no authority to mutate research state."""

    schema_name: Literal["aletheia.research_command_proposal"] = (
        "aletheia.research_command_proposal"
    )
    schema_version: Literal[1] = COMMAND_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    expected_stream_version: int = Field(ge=0)
    expected_tail_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    event_type: EventType
    payload: EventPayload
    proposed_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    proposed_at: AwareDatetime

    @model_validator(mode="after")
    def _proposal_is_consistent(self) -> "ResearchCommandProposal":
        _validate_command_envelope(
            quest_id=self.quest_id,
            scope_binding=self.scope_binding,
            expected_stream_version=self.expected_stream_version,
            expected_tail_event_sha256=self.expected_tail_event_sha256,
            event_type=self.event_type,
            payload=self.payload,
        )
        return self

    @property
    def proposal_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchCommandAuthorizationMessage(KernelModel):
    """The exact context-separated bytes signed before an authoritative commit."""

    schema_name: Literal["aletheia.research_command_authorization_message"] = (
        "aletheia.research_command_authorization_message"
    )
    schema_version: Literal[1] = COMMAND_SCHEMA_VERSION
    algorithm: Literal["ed25519-canonical-json-v1"] = "ed25519-canonical-json-v1"
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    expected_stream_version: int = Field(ge=0)
    expected_tail_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    source_event_key: str | None = Field(default=None, pattern=_IDEMPOTENCY_KEY_PATTERN)
    event_type: EventType
    payload: EventPayload
    principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    authorization_trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_at: AwareDatetime

    @property
    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def message_sha256(self) -> str:
        return hashlib.sha256(self.message_bytes).hexdigest()


def required_authorization_role(
    *, event_type: EventType, payload: EventPayload
) -> ResearchAuthorizationRole:
    """Map every current event to one exact, non-overlapping authorization role."""

    if event_type is EventType.CHARTER_ACTIVATED:
        return ResearchAuthorizationRole.COMMISSIONING
    if event_type is EventType.CHARTER_REVISED:
        return ResearchAuthorizationRole.AMENDMENT
    if (
        event_type is EventType.STOP_COMMITTED
        and isinstance(payload, StopCommittedPayload)
        and isinstance(payload.decision.directive, StopDirective)
        and payload.decision.directive.stop_reason is StopReason.EMERGENCY_STOP
    ):
        return ResearchAuthorizationRole.EMERGENCY
    return ResearchAuthorizationRole.ORDINARY


def _authorization_receipt_sha256(
    *, message: ResearchCommandAuthorizationMessage, signature_ed25519_hex: str
) -> str:
    return canonical_sha256(
        {
            "schema_name": "aletheia.research_command_authorization_receipt",
            "schema_version": COMMAND_SCHEMA_VERSION,
            "algorithm": "ed25519-canonical-json-v1",
            "message_sha256": message.message_sha256,
            "authorization_trust_root_sha256": message.authorization_trust_root_sha256,
            "authorization_policy_sha256": message.authorization_policy_sha256,
            "authorization_key_id": message.authorization_key_id,
            "principal_id": message.principal_id,
            "signature_ed25519_hex": signature_ed25519_hex,
        }
    )


class AuthorizedResearchCommand(KernelModel):
    """The sole pure command type accepted by the authoritative persistence adapter."""

    schema_name: Literal["aletheia.authorized_research_command"] = (
        "aletheia.authorized_research_command"
    )
    schema_version: Literal[1] = COMMAND_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    expected_stream_version: int = Field(ge=0)
    expected_tail_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    source_event_key: str | None = Field(default=None, pattern=_IDEMPOTENCY_KEY_PATTERN)
    event_type: EventType
    payload: EventPayload
    principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    authorization_trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    authorization_signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    authorization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_at: AwareDatetime

    @model_validator(mode="after")
    def _authorized_command_is_consistent(self) -> "AuthorizedResearchCommand":
        _validate_command_envelope(
            quest_id=self.quest_id,
            scope_binding=self.scope_binding,
            expected_stream_version=self.expected_stream_version,
            expected_tail_event_sha256=self.expected_tail_event_sha256,
            event_type=self.event_type,
            payload=self.payload,
        )
        expected_receipt = _authorization_receipt_sha256(
            message=self.authorization_message,
            signature_ed25519_hex=self.authorization_signature_ed25519_hex,
        )
        if self.authorization_receipt_sha256 != expected_receipt:
            raise ValueError("authorization receipt does not match the signed command message")
        return self

    @property
    def authorization_message(self) -> ResearchCommandAuthorizationMessage:
        return ResearchCommandAuthorizationMessage(
            quest_id=self.quest_id,
            scope_binding=self.scope_binding,
            expected_stream_version=self.expected_stream_version,
            expected_tail_event_sha256=self.expected_tail_event_sha256,
            idempotency_key=self.idempotency_key,
            source_event_key=self.source_event_key,
            event_type=self.event_type,
            payload=self.payload,
            principal_id=self.principal_id,
            authorization_trust_root_sha256=self.authorization_trust_root_sha256,
            authorization_policy_sha256=self.authorization_policy_sha256,
            authorization_key_id=self.authorization_key_id,
            proposal_sha256=self.proposal_sha256,
            authorized_at=self.authorized_at,
        )

    @property
    def unsigned_command_sha256(self) -> str:
        return self.authorization_message.message_sha256

    @property
    def command_id(self) -> str:
        identity = canonical_json_bytes(
            {
                "schema": "aletheia.research_command_identity.v1",
                "quest_id": self.quest_id,
                "idempotency_key": self.idempotency_key,
            }
        )
        return f"rkc_{hashlib.sha256(identity).hexdigest()[:32]}"

    @property
    def command_sha256(self) -> str:
        return canonical_sha256(self)

    def to_event(
        self,
        *,
        sequence: int,
        parent_event_sha256: str | None,
        committed_at: datetime,
    ) -> ResearchEvent:
        """Bind this authorized request to one exact committed stream position."""

        if sequence != self.expected_stream_version + 1:
            raise ValueError("event sequence does not match command expected stream version")
        if (sequence == 1) != (parent_event_sha256 is None):
            raise ValueError("only a genesis command may omit the parent event hash")
        if parent_event_sha256 != self.expected_tail_event_sha256:
            raise ValueError("event parent does not match the command expected tail hash")
        return ResearchEvent(
            quest_id=self.quest_id,
            sequence=sequence,
            parent_event_sha256=parent_event_sha256,
            event_type=self.event_type,
            payload=self.payload,
            command_sha256=self.command_sha256,
            principal_id=self.principal_id,
            authorization_receipt_sha256=self.authorization_receipt_sha256,
            committed_at=committed_at,
        )


# The architecture uses "committed command" to contrast this authority-bearing type with model
# proposals.  Authorization precedes the database transaction; the persisted receipt proves the
# actual commit.  Export both precise names without defining two wire formats.
CommittedResearchCommand: TypeAlias = AuthorizedResearchCommand


def authorize_research_proposal(
    proposal: ResearchCommandProposal,
    *,
    idempotency_key: str,
    authorization_policy: ResearchAuthorizationPolicyV1,
    trust_root: ResearchAuthorizationTrustRootV1,
    authorization_key_id: str,
    private_key: bytes,
    authorized_at: datetime,
    source_event_key: str | None = None,
) -> AuthorizedResearchCommand:
    """Sign a proposal with one exact role under a root-certified, Quest-scoped policy."""

    proposal = ResearchCommandProposal.model_validate(proposal.model_dump(mode="python"))
    authorization_policy = ResearchAuthorizationPolicyV1.model_validate(
        authorization_policy.model_dump(mode="python")
    )
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(
        trust_root.model_dump(mode="python")
    )
    verify_research_authorization_policy(
        policy=authorization_policy,
        trust_root=trust_root,
    )
    if authorization_policy.quest_id != proposal.quest_id:
        raise ResearchAuthorizationError("authorization policy belongs to another Quest")
    if proposal.proposed_at > authorized_at:
        raise ResearchAuthorizationError("proposal cannot postdate its authorization")
    if not (
        trust_root.frozen_at
        <= authorization_policy.frozen_at
        <= authorization_policy.certified_at
        <= authorized_at
    ):
        raise ResearchAuthorizationError("command predates its certified authorization policy")
    key = authorization_policy.key(authorization_key_id)
    message = ResearchCommandAuthorizationMessage(
        quest_id=proposal.quest_id,
        scope_binding=proposal.scope_binding,
        expected_stream_version=proposal.expected_stream_version,
        expected_tail_event_sha256=proposal.expected_tail_event_sha256,
        idempotency_key=idempotency_key,
        source_event_key=source_event_key,
        event_type=proposal.event_type,
        payload=proposal.payload,
        principal_id=key.principal_id,
        authorization_trust_root_sha256=trust_root.trust_root_sha256,
        authorization_policy_sha256=authorization_policy.policy_sha256,
        authorization_key_id=key.key_id,
        proposal_sha256=proposal.proposal_sha256,
        authorized_at=authorized_at,
    )
    signature = sign_authorization_message(
        policy=authorization_policy,
        key_id=key.key_id,
        private_key=private_key,
        principal_id=key.principal_id,
        required_role=required_authorization_role(
            event_type=proposal.event_type,
            payload=proposal.payload,
        ),
        authorized_at=authorized_at,
        message=message.message_bytes,
    )
    return AuthorizedResearchCommand(
        quest_id=proposal.quest_id,
        scope_binding=proposal.scope_binding,
        expected_stream_version=proposal.expected_stream_version,
        expected_tail_event_sha256=proposal.expected_tail_event_sha256,
        idempotency_key=idempotency_key,
        source_event_key=source_event_key,
        event_type=proposal.event_type,
        payload=proposal.payload,
        principal_id=key.principal_id,
        authorization_trust_root_sha256=trust_root.trust_root_sha256,
        authorization_policy_sha256=authorization_policy.policy_sha256,
        authorization_key_id=key.key_id,
        authorization_signature_ed25519_hex=signature,
        authorization_receipt_sha256=_authorization_receipt_sha256(
            message=message,
            signature_ed25519_hex=signature,
        ),
        proposal_sha256=proposal.proposal_sha256,
        authorized_at=authorized_at,
    )


def _assert_charter_delegations_match_policy(
    charter: ResearchCharterVersion,
    policy: ResearchAuthorizationPolicyV1,
    *,
    committed_at: datetime,
) -> None:
    if charter.expires_at is None:
        raise ResearchAuthorizationError(
            "a fixed-policy charter must have a finite authorization expiry"
        )
    if charter.expires_at <= committed_at:
        raise ResearchAuthorizationError("a newly admitted charter is already expired")

    def continuously_covered(
        principal_id: str,
        role: ResearchAuthorizationRole,
    ) -> bool:
        intervals = sorted(
            (
                key.valid_from,
                min(key.expires_at, key.revoked_at)
                if key.revoked_at is not None
                else key.expires_at,
            )
            for key in policy.keys
            if key.principal_id == principal_id and key.role is role
        )
        cursor = committed_at
        for valid_from, expires_at in intervals:
            if expires_at <= cursor:
                continue
            if valid_from > cursor:
                return False
            cursor = max(cursor, expires_at)
            if cursor >= charter.expires_at:
                return True
        return False

    expected = (
        (charter.emergency_stop_principal_ids, ResearchAuthorizationRole.EMERGENCY),
        (charter.amendment_principal_ids, ResearchAuthorizationRole.AMENDMENT),
    )
    for principals, role in expected:
        if not set(principals) <= policy.principals_for_role(role):
            raise ResearchAuthorizationError(
                f"charter delegates {role.value} authority outside the certified policy"
            )
        for principal_id in principals:
            if not continuously_covered(principal_id, role):
                raise ResearchAuthorizationError(
                    f"charter {role.value} authority is not continuously available through "
                    "charter expiry"
                )

    if not any(
        continuously_covered(principal_id, ResearchAuthorizationRole.ORDINARY)
        for principal_id in policy.principals_for_role(ResearchAuthorizationRole.ORDINARY)
    ):
        raise ResearchAuthorizationError(
            "charter has no continuously available ordinary authority through charter expiry"
        )


def verify_research_command_authorization(
    command: AuthorizedResearchCommand,
    *,
    authorization_policy: ResearchAuthorizationPolicyV1,
    trust_root: ResearchAuthorizationTrustRootV1,
    committed_at: datetime,
    active_charter: ResearchCharterVersion | None,
    admitted_object: KernelObject | None,
    resolved_action: ResearchActionProposal | None = None,
) -> ResearchAuthorizationRole:
    """Verify crypto, trust, time, object custody, and disjoint charter delegation."""

    command = AuthorizedResearchCommand.model_validate(command.model_dump(mode="python"))
    authorization_policy = ResearchAuthorizationPolicyV1.model_validate(
        authorization_policy.model_dump(mode="python")
    )
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(
        trust_root.model_dump(mode="python")
    )
    verify_research_authorization_policy(
        policy=authorization_policy,
        trust_root=trust_root,
    )
    if (
        command.quest_id != authorization_policy.quest_id
        or command.authorization_policy_sha256 != authorization_policy.policy_sha256
        or command.authorization_trust_root_sha256 != trust_root.trust_root_sha256
    ):
        raise ResearchAuthorizationError("command is bound to another Quest trust policy")
    if not (
        trust_root.frozen_at
        <= authorization_policy.frozen_at
        <= authorization_policy.certified_at
        <= command.authorized_at
        <= committed_at
    ):
        raise ResearchAuthorizationError("command has an invalid trust-policy time lineage")
    role = required_authorization_role(event_type=command.event_type, payload=command.payload)
    verify_authorization_message(
        policy=authorization_policy,
        key_id=command.authorization_key_id,
        principal_id=command.principal_id,
        required_role=role,
        authorized_at=command.authorized_at,
        committed_at=committed_at,
        message=command.authorization_message.message_bytes,
        signature_ed25519_hex=command.authorization_signature_ed25519_hex,
    )

    direct_ref = directly_referenced_object(command.payload)
    if direct_ref is not None and (
        admitted_object is None or admitted_object.object_ref != direct_ref
    ):
        raise ResearchAuthorizationError("command authorization lacks its exact admitted object")

    if role is ResearchAuthorizationRole.COMMISSIONING:
        if active_charter is not None or not isinstance(admitted_object, ResearchCharterVersion):
            raise ResearchAuthorizationError("only a fresh Quest may commission a charter")
        root_key = trust_root.key(authorization_policy.certified_by_key_id)
        if not (
            trust_root.frozen_at
            <= authorization_policy.frozen_at
            <= authorization_policy.certified_at
            <= committed_at
        ) or not root_key.active_at(committed_at):
            raise ResearchAuthorizationError(
                "policy certification is not valid at the trusted genesis authorization time"
            )
        if (
            admitted_object.authorized_by_principal_id != command.principal_id
            or admitted_object.authorized_at != command.authorized_at
            or command.principal_id
            not in authorization_policy.principals_for_role(ResearchAuthorizationRole.COMMISSIONING)
        ):
            raise ResearchAuthorizationError("charter commissioning principal is not bound")
        _assert_charter_delegations_match_policy(
            admitted_object,
            authorization_policy,
            committed_at=committed_at,
        )
        return role

    if active_charter is None:
        raise ResearchAuthorizationError("non-genesis authority requires an active charter")
    if active_charter.quest_id != command.quest_id:
        raise ResearchAuthorizationError("active charter belongs to another Quest")

    if role is ResearchAuthorizationRole.AMENDMENT:
        if not isinstance(admitted_object, ResearchCharterVersion):
            raise ResearchAuthorizationError("charter amendment must admit a charter version")
        if command.principal_id not in active_charter.amendment_principal_ids:
            raise ResearchAuthorizationError("principal cannot amend the active charter")
        if active_charter.expires_at is not None and committed_at >= active_charter.expires_at:
            raise ResearchAuthorizationError("an expired charter cannot be amended")
        if (
            admitted_object.authorized_by_principal_id != command.principal_id
            or admitted_object.authorized_at != command.authorized_at
        ):
            raise ResearchAuthorizationError("charter amendment is not bound to its signer")
        _assert_charter_delegations_match_policy(
            admitted_object,
            authorization_policy,
            committed_at=committed_at,
        )
        return role

    if role is ResearchAuthorizationRole.EMERGENCY:
        if command.principal_id not in active_charter.emergency_stop_principal_ids:
            raise ResearchAuthorizationError("principal cannot emergency-stop this Quest")
        decision = getattr(command.payload, "decision", None)
        expected_marker = emergency_halt_action_ref(
            quest_id=command.quest_id,
            charter_ref=active_charter.object_ref,
        )
        if decision is None or decision.selected_action_ref != expected_marker:
            raise ResearchAuthorizationError(
                "emergency stop must use the deterministic global-halt authority marker"
            )
        return role

    if active_charter.authorized_at > committed_at or (
        active_charter.expires_at is not None and committed_at >= active_charter.expires_at
    ):
        raise ResearchAuthorizationError(
            "active charter is not valid at the authorization linearization time"
        )
    action = resolved_action
    if isinstance(admitted_object, ResearchActionProposal):
        if action is not None and action.object_ref != admitted_object.object_ref:
            raise ResearchAuthorizationError("resolved action differs from the admitted action")
        action = admitted_object
    expected_action_ref: KernelObjectRef | None = None
    expected_action_id: str | None = None
    if isinstance(command.payload, ActionAuthorizedPayload):
        expected_action_id = command.payload.action_id
    decision = getattr(command.payload, "decision", None)
    if decision is not None:
        expected_action_ref = decision.selected_action_ref
    if expected_action_id is not None or expected_action_ref is not None or action is not None:
        if action is None:
            raise ResearchAuthorizationError(
                "action authority requires the exact previously admitted action"
            )
        if (expected_action_id is not None and action.action_id != expected_action_id) or (
            expected_action_ref is not None and action.object_ref != expected_action_ref
        ):
            raise ResearchAuthorizationError("resolved action does not match the command payload")
        if action.charter_ref != active_charter.object_ref:
            raise ResearchAuthorizationError(
                "research action was not proposed under the current charter version"
            )
        if action.requested_authority_class not in active_charter.allowed_action_classes:
            raise ResearchAuthorizationError(
                "research action requests an authority class forbidden by the charter"
            )
    return role


__all__ = [
    "COMMAND_SCHEMA_VERSION",
    "AuthorizedResearchCommand",
    "CommittedResearchCommand",
    "ResearchCommandAuthorizationMessage",
    "ResearchCommandProposal",
    "ResearchScopeBinding",
    "authorize_research_proposal",
    "directly_referenced_object",
    "required_authorization_role",
    "verify_research_command_authorization",
]
