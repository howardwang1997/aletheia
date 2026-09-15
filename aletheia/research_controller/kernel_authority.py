"""Exact-policy ordinary Research Kernel authorities for actions and transitions."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, model_validator

from aletheia.research_controller.action_proposals import SubmittedActionProposal
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.continuation import (
    ContinuationReceipt,
    continuation_to_action_kind,
)
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchScopeBinding,
    authorize_research_proposal,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationRole,
    ResearchAuthorizationTrustRootV1,
    verify_research_authorization_policy,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ContinueCommittedPayload,
    EvidenceKind,
    EvidenceRef,
    EventType,
    ForkCommittedPayload,
    ObservationIncorporatedPayload,
    RefineCommittedPayload,
    ResearchEvent,
    StopCommittedPayload,
    TransitionDecision,
    TransitionDirective,
    canonical_sha256,
)

_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"

# The four-way frozen challenge maps each continuation disposition to exactly one
# committed transition kind; emergency-class stops stay on the emergency key.
_TRANSITION_DIRECTIVE_KINDS: dict[ActionKind, str] = {
    ActionKind.CONTINUE: "continue",
    ActionKind.REFINE: "refine",
    ActionKind.FORK: "fork",
    ActionKind.STOP: "stop",
}

_COMMITTED_EVENT_TYPES: dict[str, tuple[EventType, type]] = {
    "continue": (EventType.CONTINUE_COMMITTED, ContinueCommittedPayload),
    "refine": (EventType.REFINE_COMMITTED, RefineCommittedPayload),
    "fork": (EventType.FORK_COMMITTED, ForkCommittedPayload),
    "stop": (EventType.STOP_COMMITTED, StopCommittedPayload),
}


class KernelCommandAuthorityError(RuntimeError):
    """An exact controller-side Kernel command authority failed closed."""


class ControllerKernelPolicyAssignment(ControllerModel):
    """One exact Quest/scope/policy served by a shared ordinary command key."""

    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    authorization_policy: ResearchAuthorizationPolicyV1

    @model_validator(mode="after")
    def _assignment_is_exact(self) -> "ControllerKernelPolicyAssignment":
        if (
            self.authorization_policy.quest_id != self.quest_id
            or self.scope_binding.quest_id != self.quest_id
        ):
            raise ValueError("controller Kernel policy belongs to another Quest")
        return self

    @property
    def assignment_sha256(self) -> str:
        return canonical_sha256(self)


def _single_ordinary_signing_identity(
    *,
    trust_root: ResearchAuthorizationTrustRootV1,
    assignments: tuple[ControllerKernelPolicyAssignment, ...],
    authorization_key_id: str,
    private_key: bytes,
    domain: str,
) -> tuple[
    ResearchAuthorizationTrustRootV1,
    tuple[ControllerKernelPolicyAssignment, ...],
    str,
    str,
]:
    try:
        trust_root = ResearchAuthorizationTrustRootV1.model_validate(
            trust_root.model_dump(mode="python")
        )
        assignments = tuple(
            ControllerKernelPolicyAssignment.model_validate(item.model_dump(mode="python"))
            for item in assignments
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise KernelCommandAuthorityError(
            f"{domain} Kernel authority configuration is invalid"
        ) from exc
    if (
        not assignments
        or assignments != tuple(sorted(assignments, key=lambda item: item.quest_id))
        or len({item.quest_id for item in assignments}) != len(assignments)
    ):
        raise KernelCommandAuthorityError(
            f"{domain} Kernel assignments must be nonempty, unique, and canonical"
        )
    public_keys: set[str] = set()
    principals: set[str] = set()
    try:
        for assignment in assignments:
            verify_research_authorization_policy(
                policy=assignment.authorization_policy,
                trust_root=trust_root,
            )
            key = assignment.authorization_policy.key(authorization_key_id)
            if key.role is not ResearchAuthorizationRole.ORDINARY:
                raise KernelCommandAuthorityError(
                    f"{domain} Kernel commands require an ordinary Kernel key"
                )
            public_keys.add(key.public_key_ed25519_hex)
            principals.add(key.principal_id)
        signing_key = Ed25519PrivateKey.from_private_bytes(private_key)
    except KernelCommandAuthorityError:
        raise
    except (TypeError, ValueError) as exc:
        raise KernelCommandAuthorityError(
            f"{domain} Kernel key or policy verification failed"
        ) from exc
    if len(public_keys) != 1 or len(principals) != 1:
        raise KernelCommandAuthorityError(
            f"{domain} Kernel assignments changed signing identity"
        )
    observed_public_key = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if observed_public_key.hex() != next(iter(public_keys)):
        raise KernelCommandAuthorityError(
            f"{domain} Kernel private key differs from its policy assignments"
        )
    return trust_root, assignments, next(iter(principals)), next(iter(public_keys))


def continuation_receipt_evidence_ref(receipt: ContinuationReceipt) -> EvidenceRef:
    """Return the canonical transition citation of one recorded continuation receipt."""

    return EvidenceRef(
        kind=EvidenceKind.POLICY,
        object_sha256=receipt.receipt_sha256,
        object_id=f"continuation:{receipt.receipt_sha256[:32]}",
    )


def _directive_branch_id(directive: TransitionDirective) -> str:
    return getattr(directive, "branch_id", getattr(directive, "source_branch_id", ""))


class ExactActionKernelAuthority:
    """Sign only action-authorization proposals under frozen Quest policies."""

    def __init__(
        self,
        *,
        trust_root: ResearchAuthorizationTrustRootV1,
        assignments: tuple[ControllerKernelPolicyAssignment, ...],
        authorization_key_id: str,
        private_key: bytes,
    ) -> None:
        (
            self.trust_root,
            self.assignments,
            self.principal_id,
            self.public_key_ed25519_hex,
        ) = _single_ordinary_signing_identity(
            trust_root=trust_root,
            assignments=assignments,
            authorization_key_id=authorization_key_id,
            private_key=private_key,
            domain="action",
        )
        self.authorization_key_id = authorization_key_id
        self._private_key = private_key
        self._by_quest = {item.quest_id: item for item in self.assignments}

    def authorize_action(
        self,
        *,
        proposal: ResearchCommandProposal,
        submitted: SubmittedActionProposal,
        idempotency_key: str,
        source_event_key: str,
    ) -> AuthorizedResearchCommand:
        try:
            proposal = ResearchCommandProposal.model_validate(proposal.model_dump(mode="python"))
            submitted = SubmittedActionProposal.model_validate(
                submitted.model_dump(mode="python")
            )
            assignment = self._by_quest.get(proposal.quest_id)
            if assignment is None or proposal.scope_binding != assignment.scope_binding:
                raise KernelCommandAuthorityError(
                    "action proposal escaped its exact Quest policy assignment"
                )
            _require_exact_action_proposal(
                proposal=proposal,
                submitted=submitted,
                idempotency_key=idempotency_key,
                source_event_key=source_event_key,
            )
            if proposal.proposed_by_principal_id == self.principal_id:
                raise KernelCommandAuthorityError(
                    "action Kernel authority cannot sign its own proposal"
                )
            return authorize_research_proposal(
                proposal,
                idempotency_key=idempotency_key,
                source_event_key=source_event_key,
                authorization_policy=assignment.authorization_policy,
                trust_root=self.trust_root,
                authorization_key_id=self.authorization_key_id,
                private_key=self._private_key,
                authorized_at=proposal.proposed_at,
            )
        except KernelCommandAuthorityError:
            raise
        except Exception as exc:  # noqa: BLE001 - external command authority fails closed
            raise KernelCommandAuthorityError(
                "action Kernel authorization failed closed"
            ) from exc


def _require_exact_action_proposal(
    *,
    proposal: ResearchCommandProposal,
    submitted: SubmittedActionProposal,
    idempotency_key: str,
    source_event_key: str,
) -> None:
    payload = proposal.payload
    action_ref = submitted.action.object_ref
    expected_idempotency = f"action:{action_ref.object_sha256}"
    expected_source = f"action-proposal:{action_ref.object_sha256}"
    if (
        proposal.event_type is not EventType.ACTION_AUTHORIZED
        or not isinstance(payload, ActionAuthorizedPayload)
        or proposal.quest_id != submitted.action.quest_id
        or proposal.quest_id != submitted.request.quest_id
        or proposal.scope_binding != submitted.request.scope_binding
        or payload.action_id != action_ref.object_id
        or payload.branch_id != submitted.target_branch_id
        or submitted.command_proposal.payload.action_ref != action_ref
        or not submitted.awaiting_independent_kernel_authority
        or idempotency_key != expected_idempotency
        or source_event_key != expected_source
    ):
        raise KernelCommandAuthorityError(
            "action Kernel proposal rebound its submitted action proposal"
        )


class ExactTransitionKernelAuthority:
    """Sign only the four-way transition commits a continuation receipt forces."""

    def __init__(
        self,
        *,
        trust_root: ResearchAuthorizationTrustRootV1,
        assignments: tuple[ControllerKernelPolicyAssignment, ...],
        authorization_key_id: str,
        private_key: bytes,
    ) -> None:
        (
            self.trust_root,
            self.assignments,
            self.principal_id,
            self.public_key_ed25519_hex,
        ) = _single_ordinary_signing_identity(
            trust_root=trust_root,
            assignments=assignments,
            authorization_key_id=authorization_key_id,
            private_key=private_key,
            domain="transition",
        )
        self.authorization_key_id = authorization_key_id
        self._private_key = private_key
        self._by_quest = {item.quest_id: item for item in self.assignments}

    def authorize_transition(
        self,
        *,
        proposal: ResearchCommandProposal,
        submitted: SubmittedActionProposal,
        receipt: ContinuationReceipt,
        incorporated_event: ResearchEvent,
        idempotency_key: str,
        source_event_key: str,
    ) -> AuthorizedResearchCommand:
        try:
            proposal = ResearchCommandProposal.model_validate(proposal.model_dump(mode="python"))
            submitted = SubmittedActionProposal.model_validate(
                submitted.model_dump(mode="python")
            )
            receipt = ContinuationReceipt.model_validate(receipt.model_dump(mode="python"))
            incorporated_event = ResearchEvent.model_validate(
                incorporated_event.model_dump(mode="python")
            )
            assignment = self._by_quest.get(proposal.quest_id)
            if assignment is None or proposal.scope_binding != assignment.scope_binding:
                raise KernelCommandAuthorityError(
                    "transition proposal escaped its exact Quest policy assignment"
                )
            _require_exact_transition_proposal(
                proposal=proposal,
                submitted=submitted,
                receipt=receipt,
                incorporated_event=incorporated_event,
                idempotency_key=idempotency_key,
                source_event_key=source_event_key,
            )
            if proposal.proposed_by_principal_id == self.principal_id:
                raise KernelCommandAuthorityError(
                    "transition Kernel authority cannot sign its own proposal"
                )
            return authorize_research_proposal(
                proposal,
                idempotency_key=idempotency_key,
                source_event_key=source_event_key,
                authorization_policy=assignment.authorization_policy,
                trust_root=self.trust_root,
                authorization_key_id=self.authorization_key_id,
                private_key=self._private_key,
                authorized_at=proposal.proposed_at,
            )
        except KernelCommandAuthorityError:
            raise
        except Exception as exc:  # noqa: BLE001 - external command authority fails closed
            raise KernelCommandAuthorityError(
                "transition Kernel authorization failed closed"
            ) from exc


def _require_exact_transition_proposal(
    *,
    proposal: ResearchCommandProposal,
    submitted: SubmittedActionProposal,
    receipt: ContinuationReceipt,
    incorporated_event: ResearchEvent,
    idempotency_key: str,
    source_event_key: str,
) -> None:
    payload = proposal.payload
    if not isinstance(payload, (ContinueCommittedPayload, RefineCommittedPayload,
                                ForkCommittedPayload, StopCommittedPayload)):
        raise KernelCommandAuthorityError(
            "transition Kernel proposal rebound its recorded continuation"
        )
    decision: TransitionDecision = payload.decision
    expected_action_kind = continuation_to_action_kind(receipt.disposition)
    expected_directive_kind = _TRANSITION_DIRECTIVE_KINDS.get(expected_action_kind)
    expected_event_type, expected_payload_type = (
        _COMMITTED_EVENT_TYPES[expected_directive_kind]
        if expected_directive_kind is not None
        else (None, None)
    )
    directive_branch = _directive_branch_id(decision.directive)
    event_payload = incorporated_event.payload
    if (
        expected_directive_kind is None
        or proposal.event_type is not expected_event_type
        or type(payload) is not expected_payload_type
        or decision.directive.kind != expected_directive_kind
        or decision.quest_id != proposal.quest_id
        or decision.quest_id != submitted.action.quest_id
        or decision.charter_ref != submitted.action.charter_ref
        or decision.selected_action_ref != submitted.action.object_ref
        or submitted.action.kind is not expected_action_kind
        or directive_branch != submitted.target_branch_id
        or proposal.scope_binding != submitted.request.scope_binding
        or continuation_receipt_evidence_ref(receipt) not in decision.evidence_refs
        or incorporated_event.event_sha256 not in decision.evidence_event_sha256s
        or incorporated_event.quest_id != proposal.quest_id
        or incorporated_event.event_type is not EventType.OBSERVATION_INCORPORATED
        or not isinstance(event_payload, ObservationIncorporatedPayload)
        or event_payload.scientific_slot_id != receipt.scientific_slot_id
        or event_payload.source_world_model_sha256 != receipt.world_model_snapshot_sha256
        or event_payload.branch_id != directive_branch
        or idempotency_key != f"transition:{decision.decision_sha256}"
        or source_event_key != f"continuation:{receipt.receipt_sha256}"
    ):
        raise KernelCommandAuthorityError(
            "transition Kernel proposal rebound its recorded continuation"
        )


__all__ = [
    "ControllerKernelPolicyAssignment",
    "ExactActionKernelAuthority",
    "ExactTransitionKernelAuthority",
    "KernelCommandAuthorityError",
    "continuation_receipt_evidence_ref",
]
