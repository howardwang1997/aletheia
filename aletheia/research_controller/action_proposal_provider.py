"""Conservative deterministic provider for powerless controller action proposals.

This baseline provider selects only from the exact audited request.  Its cost and risk receipts
explicitly remain unknown and require later independent authority; they are content identities,
not approvals.  A restarted service reconstructs the complete draft instead of trusting the
stored provider fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from aletheia.research_controller.action_proposals import (
    ActionProposalBlocked,
    ActionProposalDraft,
    ActionProposalError,
    ActionProposalTarget,
    ControllerActionProposalRequest,
)
from aletheia.research_controller.contracts import ControllerModel, ControllerStep
from aletheia.research_kernel.schemas import ActionKind, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"
_AUTHORITY_CLASS_PATTERN = r"^[a-z][a-z0-9_:/.-]{2,127}$"


class DeterministicActionProposalPolicyPin(ControllerModel):
    """Deployment-frozen baseline choices; none of them grant Kernel authority."""

    schema_name: Literal["aletheia.deterministic_action_proposal_policy_pin"] = (
        "aletheia.deterministic_action_proposal_policy_pin"
    )
    schema_version: Literal[1] = 1
    provider_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_principal_id: str = Field(pattern=_PRINCIPAL_PATTERN)
    initial_action_kind_preference: tuple[ActionKind, ...] = Field(min_length=1, max_length=32)
    initial_epistemic_purpose: str = Field(min_length=1, max_length=4_000)
    redesign_epistemic_purpose: str = Field(min_length=1, max_length=4_000)
    followup_epistemic_purpose: str = Field(min_length=1, max_length=4_000)
    candidate_outcomes: tuple[str, ...] = Field(min_length=1, max_length=128)
    requested_authority_class: str = Field(pattern=_AUTHORITY_CLASS_PATTERN)
    cost_screening_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    risk_screening_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    alternative_action_selection_allowed: Literal[False] = False
    external_model_callback_allowed: Literal[False] = False
    cost_estimate_is_authorization: Literal[False] = False
    risk_screening_is_authorization: Literal[False] = False

    @model_validator(mode="after")
    def _policy_is_closed(self) -> "DeterministicActionProposalPolicyPin":
        if (
            len(self.initial_action_kind_preference)
            != len(set(self.initial_action_kind_preference))
            or ActionKind.ACTIVATE in self.initial_action_kind_preference
        ):
            raise ValueError("initial action preference must be unique and cannot activate")
        if self.candidate_outcomes != tuple(sorted(set(self.candidate_outcomes))):
            raise ValueError("deterministic proposal outcomes must be unique and canonical")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class ConservativeProposalCostReceipt(ControllerModel):
    """Proposal-only acknowledgement that no execution cost has yet been authorized."""

    schema_name: Literal["aletheia.conservative_proposal_cost_receipt"] = (
        "aletheia.conservative_proposal_cost_receipt"
    )
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_kind: ActionKind
    screening_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal["unknown_requires_independent_budget_authority"] = (
        "unknown_requires_independent_budget_authority"
    )
    estimated_amount: Literal[None] = None
    currency: Literal[None] = None
    execution_authorized: Literal[False] = False

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ConservativeProposalRiskReceipt(ControllerModel):
    """Proposal-only acknowledgement that safety/risk authority has not approved an action."""

    schema_name: Literal["aletheia.conservative_proposal_risk_receipt"] = (
        "aletheia.conservative_proposal_risk_receipt"
    )
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_kind: ActionKind
    screening_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal["unassessed_requires_independent_risk_authority"] = (
        "unassessed_requires_independent_risk_authority"
    )
    safety_approved: Literal[False] = False
    external_action_approved: Literal[False] = False

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def _selection(
    request: ControllerActionProposalRequest,
    policy: DeterministicActionProposalPolicyPin,
) -> tuple[ActionProposalTarget, ActionKind]:
    if request.required_action_kind is not None:
        eligible = tuple(
            target
            for target in request.targets
            if request.required_action_kind in target.allowed_action_kinds
        )
        if eligible:
            return eligible[0], request.required_action_kind
    else:
        for kind in policy.initial_action_kind_preference:
            eligible = tuple(
                target for target in request.targets if kind in target.allowed_action_kinds
            )
            if eligible:
                return eligible[0], kind
    raise ActionProposalBlocked(("action_proposal:no_policy_eligible_action",))


def _purpose(
    request: ControllerActionProposalRequest,
    policy: DeterministicActionProposalPolicyPin,
) -> str:
    return {
        ControllerStep.PROPOSE_ACTION: policy.initial_epistemic_purpose,
        ControllerStep.PROPOSE_REDESIGN: policy.redesign_epistemic_purpose,
        ControllerStep.PROPOSE_FOLLOWUP: policy.followup_epistemic_purpose,
    }[request.step]


def _draft_for(
    *,
    request: ControllerActionProposalRequest,
    policy: DeterministicActionProposalPolicyPin,
    proposed_at: datetime,
) -> ActionProposalDraft:
    target, kind = _selection(request, policy)
    cost = ConservativeProposalCostReceipt(
        request_sha256=request.request_sha256,
        target_sha256=target.target_sha256,
        action_kind=kind,
        screening_policy_sha256=policy.cost_screening_policy_sha256,
    )
    risk = ConservativeProposalRiskReceipt(
        request_sha256=request.request_sha256,
        target_sha256=target.target_sha256,
        action_kind=kind,
        screening_policy_sha256=policy.risk_screening_policy_sha256,
    )
    identity = canonical_sha256(
        {
            "schema_name": "aletheia.deterministic_action_proposal_identity",
            "schema_version": 1,
            "request_sha256": request.request_sha256,
            "target_sha256": target.target_sha256,
            "action_kind": kind,
            "provider_policy_sha256": policy.policy_sha256,
        }
    )
    return ActionProposalDraft(
        request_sha256=request.request_sha256,
        action_id=f"action:deterministic:{identity[:32]}",
        target_sha256=target.target_sha256,
        kind=kind,
        epistemic_purpose=_purpose(request, policy),
        candidate_outcomes=policy.candidate_outcomes,
        cost_receipt_sha256=cost.receipt_sha256,
        risk_receipt_sha256=risk.receipt_sha256,
        alternative_action_refs=(),
        requested_authority_class=policy.requested_authority_class,
        proposed_at=proposed_at,
    )


class DeterministicActionProposalProvider:
    """Reconstructable proposal generator with no model, signing key, or store handle."""

    def __init__(
        self,
        *,
        policy: DeterministicActionProposalPolicyPin,
        implementation_sha256: str,
        principal_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        policy = DeterministicActionProposalPolicyPin.model_validate(
            policy.model_dump(mode="python")
        )
        if (
            implementation_sha256 != policy.provider_implementation_sha256
            or principal_id != policy.provider_principal_id
            or re.fullmatch(_PRINCIPAL_PATTERN, principal_id) is None
        ):
            raise ValueError("deterministic proposal provider differs from its policy")
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def propose_action(self, request: ControllerActionProposalRequest) -> ActionProposalDraft:
        request = ControllerActionProposalRequest.model_validate(request.model_dump(mode="python"))
        proposed_at = self._clock()
        if proposed_at.tzinfo is None or proposed_at.utcoffset() is None:
            raise ActionProposalError("deterministic proposal clock must be timezone-aware")
        return _draft_for(
            request=request,
            policy=self._policy,
            proposed_at=proposed_at,
        )

    def verify_action_proposal_draft(
        self,
        *,
        request: ControllerActionProposalRequest,
        draft: ActionProposalDraft,
    ) -> ActionProposalDraft:
        try:
            request = ControllerActionProposalRequest.model_validate(
                request.model_dump(mode="python")
            )
            draft = ActionProposalDraft.model_validate(draft.model_dump(mode="python"))
            if draft.proposed_at < request.latest_event_committed_at:
                raise ValueError("deterministic proposal predates its audited request")
            expected = _draft_for(
                request=request,
                policy=self._policy,
                proposed_at=draft.proposed_at,
            )
            if draft != expected:
                raise ValueError("deterministic proposal differs from its frozen policy")
            return draft
        except ActionProposalBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - stored/provider draft fails closed
            raise ActionProposalError(
                "deterministic action proposal verification failed closed"
            ) from exc


__all__ = [
    "ConservativeProposalCostReceipt",
    "ConservativeProposalRiskReceipt",
    "DeterministicActionProposalPolicyPin",
    "DeterministicActionProposalProvider",
]
