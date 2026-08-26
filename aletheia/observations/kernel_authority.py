"""Exact-policy ordinary Research Kernel authority for observation incorporation."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, model_validator

from aletheia.observations.scientific_bridge import (
    CommittedObservationAdmission,
    ObservationAdmissionDisposition,
    ScientificBridgeModel,
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
    EventType,
    ObservationIncorporatedPayload,
    canonical_sha256,
)

_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"


class ObservationKernelAuthorityError(RuntimeError):
    """An ordinary Kernel authority assignment or request failed closed."""


class ObservationKernelPolicyAssignment(ScientificBridgeModel):
    """One exact Quest/scope/policy served by a shared ordinary command key."""

    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    authorization_policy: ResearchAuthorizationPolicyV1

    @model_validator(mode="after")
    def _assignment_is_exact(self) -> "ObservationKernelPolicyAssignment":
        if (
            self.authorization_policy.quest_id != self.quest_id
            or self.scope_binding.quest_id != self.quest_id
        ):
            raise ValueError("observation Kernel policy belongs to another Quest")
        return self

    @property
    def assignment_sha256(self) -> str:
        return canonical_sha256(self)


class ExactObservationKernelAuthority:
    """Sign only observation-incorporation proposals under frozen Quest policies."""

    def __init__(
        self,
        *,
        trust_root: ResearchAuthorizationTrustRootV1,
        assignments: tuple[ObservationKernelPolicyAssignment, ...],
        authorization_key_id: str,
        private_key: bytes,
    ) -> None:
        try:
            trust_root = ResearchAuthorizationTrustRootV1.model_validate(
                trust_root.model_dump(mode="python")
            )
            assignments = tuple(
                ObservationKernelPolicyAssignment.model_validate(item.model_dump(mode="python"))
                for item in assignments
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ObservationKernelAuthorityError(
                "observation Kernel authority configuration is invalid"
            ) from exc
        if (
            not assignments
            or assignments != tuple(sorted(assignments, key=lambda item: item.quest_id))
            or len({item.quest_id for item in assignments}) != len(assignments)
        ):
            raise ObservationKernelAuthorityError(
                "observation Kernel assignments must be nonempty, unique, and canonical"
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
                    raise ObservationKernelAuthorityError(
                        "observation incorporation requires an ordinary Kernel key"
                    )
                public_keys.add(key.public_key_ed25519_hex)
                principals.add(key.principal_id)
            signing_key = Ed25519PrivateKey.from_private_bytes(private_key)
        except ObservationKernelAuthorityError:
            raise
        except (TypeError, ValueError) as exc:
            raise ObservationKernelAuthorityError(
                "observation Kernel key or policy verification failed"
            ) from exc
        if len(public_keys) != 1 or len(principals) != 1:
            raise ObservationKernelAuthorityError(
                "observation Kernel assignments changed signing identity"
            )
        observed_public_key = signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if observed_public_key.hex() != next(iter(public_keys)):
            raise ObservationKernelAuthorityError(
                "observation Kernel private key differs from its policy assignments"
            )
        self.trust_root = trust_root
        self.assignments = assignments
        self.authorization_key_id = authorization_key_id
        self.principal_id = next(iter(principals))
        self.public_key_ed25519_hex = next(iter(public_keys))
        self._private_key = private_key
        self._by_quest = {item.quest_id: item for item in assignments}

    def authorize_observation_incorporation(
        self,
        *,
        proposal: ResearchCommandProposal,
        committed_admission: CommittedObservationAdmission,
        idempotency_key: str,
        source_event_key: str,
    ) -> AuthorizedResearchCommand:
        try:
            proposal = ResearchCommandProposal.model_validate(proposal.model_dump(mode="python"))
            committed = CommittedObservationAdmission.model_validate(
                committed_admission.model_dump(mode="python")
            )
            assignment = self._by_quest.get(proposal.quest_id)
            if assignment is None or proposal.scope_binding != assignment.scope_binding:
                raise ObservationKernelAuthorityError(
                    "observation proposal escaped its exact Quest policy assignment"
                )
            _require_exact_observation_proposal(
                proposal=proposal,
                committed=committed,
                idempotency_key=idempotency_key,
                source_event_key=source_event_key,
            )
            if proposal.proposed_by_principal_id == self.principal_id:
                raise ObservationKernelAuthorityError(
                    "observation Kernel authority cannot sign its own proposal"
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
        except ObservationKernelAuthorityError:
            raise
        except Exception as exc:  # noqa: BLE001 - external command authority fails closed
            raise ObservationKernelAuthorityError(
                "observation Kernel authorization failed closed"
            ) from exc


def _require_exact_observation_proposal(
    *,
    proposal: ResearchCommandProposal,
    committed: CommittedObservationAdmission,
    idempotency_key: str,
    source_event_key: str,
) -> None:
    decision = committed.message.decision.message
    validation = decision.committed_validation_receipt.message.receipt.message
    authorization = validation.raw_run.scientific_authorization.message
    binding = authorization.action_protocol_binding
    protocol = binding.compilation_request.protocol
    payload = proposal.payload
    expected_idempotency = f"observation-admission:{committed.message.decision.decision_sha256}"
    expected_source = f"scientific-slot:{decision.scientific_slot_id}"
    if (
        proposal.event_type is not EventType.OBSERVATION_INCORPORATED
        or not isinstance(payload, ObservationIncorporatedPayload)
        or decision.disposition is not ObservationAdmissionDisposition.ADMITTED
        or decision.admitted_observation_sha256 is None
        or validation.outcome is None
        or protocol.world_model is None
        or idempotency_key != expected_idempotency
        or source_event_key != expected_source
        or proposal.quest_id != binding.action.quest_id
        or payload.branch_id != protocol.graph_scope.branch_id
        or payload.action_id != binding.action.action_id
        or payload.scientific_slot_id != decision.scientific_slot_id
        or payload.committed_admission_sha256 != committed.committed_admission_sha256
        or payload.scientific_observation_sha256 != decision.admitted_observation_sha256
        or payload.outcome != validation.outcome.value
        or payload.source_world_model_sha256 != protocol.world_model.world_model_sha256
    ):
        raise ObservationKernelAuthorityError(
            "observation Kernel proposal rebound its independent admission"
        )


__all__ = [
    "ExactObservationKernelAuthority",
    "ObservationKernelAuthorityError",
    "ObservationKernelPolicyAssignment",
]
