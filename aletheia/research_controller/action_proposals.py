"""Typed action-proposal materialization at the controller/Kernel authority boundary.

An action proposal is deliberately useful but powerless: it carries a complete Kernel object and
an unsigned ``ResearchCommandProposal``, then waits for a separately deployed command authority.
This module validates that an external proposal service stayed inside the exact audited controller
context; it cannot archive, sign, or commit a Kernel command itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_kernel.commands import ResearchCommandProposal, ResearchScopeBinding
from aletheia.research_kernel.schemas import (
    ActionKind,
    ActionProposedPayload,
    EvidenceRef,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ResearchActionProposal,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ACTION_ID_PATTERN = r"^[a-z][a-z0-9_:/.-]{2,127}$"
_BRANCH_ID_PATTERN = r"^rbr_[0-9a-f]{32}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"

_PROPOSAL_STEPS = frozenset(
    {
        ControllerStep.PROPOSE_ACTION,
        ControllerStep.PROPOSE_REDESIGN,
        ControllerStep.PROPOSE_FOLLOWUP,
    }
)


class ActionProposalError(RuntimeError):
    """An action proposal escaped its audited request or immutable custody."""


class ActionProposalBlocked(ActionProposalError):
    """No valid proposal target exists for the current audited graph."""

    def __init__(self, blocker_codes: tuple[str, ...]) -> None:
        if not blocker_codes or blocker_codes != tuple(sorted(set(blocker_codes))):
            raise ValueError("action-proposal blockers must be nonempty and canonical")
        self.blocker_codes = blocker_codes
        super().__init__(",".join(blocker_codes))


class ActionProposalTargetLifecycle(str, Enum):
    ACTIVE = "active"
    ADMITTED = "admitted"
    PAUSED = "paused"


class ActionProposalTarget(ControllerModel):
    """One exact branch/question pair on which a proposal may be formed."""

    schema_name: Literal["aletheia.controller_action_proposal_target"] = (
        "aletheia.controller_action_proposal_target"
    )
    schema_version: Literal[1] = 1
    branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    branch_lifecycle: ActionProposalTargetLifecycle
    question_ref: KernelObjectRef
    allowed_action_kinds: tuple[ActionKind, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _target_is_exact(self) -> "ActionProposalTarget":
        if self.question_ref.object_kind is not KernelObjectKind.QUESTION:
            raise ValueError("action proposal target requires a question reference")
        expected = tuple(sorted(set(self.allowed_action_kinds), key=lambda item: item.value))
        if self.allowed_action_kinds != expected:
            raise ValueError("action proposal target kinds must be unique and canonical")
        if self.branch_lifecycle is not ActionProposalTargetLifecycle.ACTIVE and (
            self.allowed_action_kinds != (ActionKind.ACTIVATE,)
        ):
            raise ValueError("only activation may target an admitted or paused branch")
        if self.branch_lifecycle is ActionProposalTargetLifecycle.ACTIVE and (
            ActionKind.ACTIVATE in self.allowed_action_kinds
        ):
            raise ValueError("an already-active branch cannot propose activation")
        return self

    @property
    def target_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerActionProposalRequest(ControllerModel):
    """Fresh audited context supplied to one powerless proposal generator."""

    schema_name: Literal["aletheia.controller_action_proposal_request"] = (
        "aletheia.controller_action_proposal_request"
    )
    schema_version: Literal[1] = 1
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    recovery_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    step: ControllerStep
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    scope_binding: ResearchScopeBinding
    expected_stream_version: int = Field(ge=1)
    expected_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    charter_ref: KernelObjectRef
    targets: tuple[ActionProposalTarget, ...] = Field(min_length=1, max_length=128)
    required_action_kind: ActionKind | None = None
    required_evidence_refs: tuple[EvidenceRef, ...] = Field(max_length=128)
    allowed_alternative_action_refs: tuple[KernelObjectRef, ...] = Field(max_length=128)
    source_action_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    latest_event_committed_at: AwareDatetime
    direct_kernel_mutation_allowed: Literal[False] = False
    signing_key_available: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_exact(self) -> "ControllerActionProposalRequest":
        if self.step not in _PROPOSAL_STEPS:
            raise ValueError("action proposal request names a non-proposal controller step")
        if (
            self.scope_binding.quest_id != self.quest_id
            or self.charter_ref.object_kind is not KernelObjectKind.CHARTER
            or self.charter_ref.quest_id != self.quest_id
        ):
            raise ValueError("action proposal request escaped its Quest or charter")
        target_keys = tuple(
            (item.branch_id, item.question_ref.object_sha256, item.target_sha256)
            for item in self.targets
        )
        if target_keys != tuple(sorted(set(target_keys))):
            raise ValueError("action proposal targets must be unique and canonical")
        if any(item.question_ref.quest_id != self.quest_id for item in self.targets):
            raise ValueError("action proposal target belongs to another Quest")
        evidence_keys = tuple(
            (item.kind.value, item.object_sha256, item.object_id or "")
            for item in self.required_evidence_refs
        )
        if evidence_keys != tuple(sorted(set(evidence_keys))):
            raise ValueError("required proposal evidence must be unique and canonical")
        alternative_keys = tuple(
            (item.object_id, item.object_sha256) for item in self.allowed_alternative_action_refs
        )
        if alternative_keys != tuple(sorted(set(alternative_keys))):
            raise ValueError("allowed alternative actions must be unique and canonical")
        if any(
            item.object_kind is not KernelObjectKind.ACTION or item.quest_id != self.quest_id
            for item in self.allowed_alternative_action_refs
        ):
            raise ValueError("allowed alternative action belongs to another Quest")
        downstream = self.step is not ControllerStep.PROPOSE_ACTION
        if downstream and not all(
            (
                self.source_action_sha256 is not None,
                self.source_receipt_sha256 is not None,
                self.required_action_kind is not None,
                bool(self.required_evidence_refs),
            )
        ):
            raise ValueError("redesign/follow-up requests require their exact source receipts")
        if not downstream and any(
            (
                self.source_action_sha256 is not None,
                self.source_receipt_sha256 is not None,
                self.required_action_kind is not None,
                bool(self.required_evidence_refs),
            )
        ):
            raise ValueError("initial action requests cannot carry downstream receipt context")
        if downstream and any(
            item.allowed_action_kinds != (self.required_action_kind,) for item in self.targets
        ):
            raise ValueError("downstream targets must carry only the required action kind")
        if self.step is ControllerStep.PROPOSE_REDESIGN and (
            self.required_action_kind is not ActionKind.REFINE
        ):
            raise ValueError("compiler redesign must propose a REFINE action")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class ActionProposalDraft(ControllerModel):
    """Model/provider-owned choices; all graph authority fields are added by the service."""

    schema_name: Literal["aletheia.action_proposal_draft"] = "aletheia.action_proposal_draft"
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_id: str = Field(pattern=_ACTION_ID_PATTERN)
    target_sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: ActionKind
    epistemic_purpose: str = Field(min_length=1, max_length=4_000)
    candidate_outcomes: tuple[str, ...] = Field(min_length=1, max_length=128)
    cost_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    risk_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    alternative_action_refs: tuple[KernelObjectRef, ...] = Field(max_length=64)
    requested_authority_class: str = Field(pattern=_ACTION_ID_PATTERN)
    proposed_at: AwareDatetime

    @model_validator(mode="after")
    def _draft_is_canonical(self) -> "ActionProposalDraft":
        if self.candidate_outcomes != tuple(sorted(set(self.candidate_outcomes))):
            raise ValueError("action proposal outcomes must be unique and canonical")
        alternative_keys = tuple(
            (item.object_id, item.object_sha256) for item in self.alternative_action_refs
        )
        if alternative_keys != tuple(sorted(set(alternative_keys))):
            raise ValueError("action proposal alternatives must be unique and canonical")
        return self

    @property
    def draft_sha256(self) -> str:
        return canonical_sha256(self)


class SubmittedActionProposal(ControllerModel):
    """Write-once proposal queued for an independent Kernel command authority."""

    schema_name: Literal["aletheia.submitted_action_proposal"] = (
        "aletheia.submitted_action_proposal"
    )
    schema_version: Literal[1] = 1
    request: ControllerActionProposalRequest
    draft: ActionProposalDraft
    action: ResearchActionProposal
    target_branch_id: str = Field(pattern=_BRANCH_ID_PATTERN)
    command_proposal: ResearchCommandProposal
    proposal_authority_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposed_by_principal_id: str = Field(pattern=_PRINCIPAL_PATTERN)
    proposal_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal_service_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    submitted_at: AwareDatetime
    awaiting_independent_kernel_authority: Literal[True] = True
    kernel_command_signed: Literal[False] = False
    kernel_state_mutated: Literal[False] = False

    @model_validator(mode="after")
    def _submission_is_closed(self) -> "SubmittedActionProposal":
        if self.command_proposal.event_type is not EventType.ACTION_PROPOSED or not isinstance(
            self.command_proposal.payload, ActionProposedPayload
        ):
            raise ValueError("submitted action proposal requires an ACTION_PROPOSED command")
        if (
            self.draft.request_sha256 != self.request.request_sha256
            or self.action.quest_id != self.request.quest_id
            or self.action.object_ref != self.command_proposal.payload.action_ref
            or self.command_proposal.payload.branch_id != self.target_branch_id
            or self.command_proposal.quest_id != self.request.quest_id
            or self.command_proposal.scope_binding != self.request.scope_binding
            or self.command_proposal.expected_stream_version != self.request.expected_stream_version
            or self.command_proposal.expected_tail_event_sha256
            != self.request.expected_tail_event_sha256
            or self.command_proposal.proposed_by_principal_id != self.proposed_by_principal_id
            or self.action.proposed_by_principal_id != self.proposed_by_principal_id
            or self.action.proposed_at != self.draft.proposed_at
            or self.command_proposal.proposed_at != self.draft.proposed_at
            or not self.request.latest_event_committed_at
            <= self.draft.proposed_at
            <= self.submitted_at
        ):
            raise ValueError("submitted action proposal escaped its request or chronology")
        return self

    @property
    def submission_sha256(self) -> str:
        return canonical_sha256(self)


class ActionProposalDraftProviderPort(Protocol):
    """Untrusted proposal-only provider; it receives no signer or store handle."""

    def propose_action(self, request: ControllerActionProposalRequest) -> ActionProposalDraft: ...


class ActionProposalContextSourcePort(Protocol):
    """Rebuild proposal context from authoritative receipts for the exact controller tick."""

    def load_request(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerActionProposalRequest: ...


class ActionProposalSubmissionStorePort(Protocol):
    """Write-once request-to-submission custody owned outside the controller worker."""

    authority_binding: ControllerStepAuthorityBinding

    def load(self, *, request_sha256: str) -> SubmittedActionProposal | None: ...

    def put_once(self, submission: SubmittedActionProposal) -> SubmittedActionProposal: ...


class ActionProposalMaterializationPort(Protocol):
    """Durably materialize one powerless proposal through its pinned service boundary."""

    authority_binding: ControllerStepAuthorityBinding

    def materialize_and_submit(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> SubmittedActionProposal: ...


def materialize_action_proposal(
    *,
    request: ControllerActionProposalRequest,
    draft: ActionProposalDraft,
    authority_binding: ControllerStepAuthorityBinding,
    submitted_at: datetime,
) -> SubmittedActionProposal:
    """Turn provider choices into exact graph-bound object/command proposal bytes."""

    try:
        request = ControllerActionProposalRequest.model_validate(request.model_dump(mode="python"))
        draft = ActionProposalDraft.model_validate(draft.model_dump(mode="python"))
        binding = ControllerStepAuthorityBinding.model_validate(
            authority_binding.model_dump(mode="python")
        )
        if (
            binding.role is not ControllerStepAuthorityRole.ACTION_PROPOSAL
            or binding.key_id is not None
            or binding.binding_sha256 != authority_binding.binding_sha256
            or draft.request_sha256 != request.request_sha256
        ):
            raise ValueError("action proposal used another request or authority role")
        targets = tuple(
            item for item in request.targets if item.target_sha256 == draft.target_sha256
        )
        if len(targets) != 1:
            raise ValueError("action proposal selected an unavailable graph target")
        target = targets[0]
        if draft.kind not in target.allowed_action_kinds or (
            request.required_action_kind is not None
            and draft.kind is not request.required_action_kind
        ):
            raise ValueError("action proposal changed its required action kind")
        allowed_alternatives = set(request.allowed_alternative_action_refs)
        if draft.action_id in {item.object_id for item in allowed_alternatives}:
            raise ValueError("action proposal reused an admitted action id")
        if any(item not in allowed_alternatives for item in draft.alternative_action_refs):
            raise ValueError("action proposal named an unverified alternative action")
        action = ResearchActionProposal(
            action_id=draft.action_id,
            quest_id=request.quest_id,
            charter_ref=request.charter_ref,
            question_ref=target.question_ref,
            basis_tail_event_sha256=request.expected_tail_event_sha256,
            kind=draft.kind,
            epistemic_purpose=draft.epistemic_purpose,
            candidate_outcomes=draft.candidate_outcomes,
            evidence_refs=request.required_evidence_refs,
            cost_receipt_sha256=draft.cost_receipt_sha256,
            risk_receipt_sha256=draft.risk_receipt_sha256,
            alternative_action_refs=draft.alternative_action_refs,
            requested_authority_class=draft.requested_authority_class,
            proposed_by_principal_id=binding.principal_id,
            proposed_at=draft.proposed_at,
        )
        command = ResearchCommandProposal(
            quest_id=request.quest_id,
            scope_binding=request.scope_binding,
            expected_stream_version=request.expected_stream_version,
            expected_tail_event_sha256=request.expected_tail_event_sha256,
            event_type=EventType.ACTION_PROPOSED,
            payload=ActionProposedPayload(
                action_ref=action.object_ref,
                branch_id=target.branch_id,
            ),
            proposed_by_principal_id=binding.principal_id,
            proposed_at=draft.proposed_at,
        )
        return SubmittedActionProposal(
            request=request,
            draft=draft,
            action=action,
            target_branch_id=target.branch_id,
            command_proposal=command,
            proposal_authority_binding_sha256=binding.binding_sha256,
            proposed_by_principal_id=binding.principal_id,
            proposal_policy_sha256=binding.policy_sha256,
            proposal_service_manifest_sha256=binding.service_manifest_sha256,
            submitted_at=submitted_at,
        )
    except ActionProposalError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed across provider-owned material
        raise ActionProposalError("action proposal materialization failed closed") from exc


def verify_submitted_action_proposal(
    *,
    submission: SubmittedActionProposal,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
    authority_binding: ControllerStepAuthorityBinding,
) -> SubmittedActionProposal:
    """Rebuild an external submission and bind it to the worker's exact current tick."""

    try:
        submission = SubmittedActionProposal.model_validate(submission.model_dump(mode="python"))
        if (
            plan_recovery_tick(projection) != plan
            or plan.step not in _PROPOSAL_STEPS
            or submission.request.wakeup_sha256 != wakeup.wakeup_sha256
            or submission.request.recovery_projection_sha256 != projection.projection_sha256
            or submission.request.plan_sha256 != plan.plan_sha256
            or submission.request.step is not plan.step
            or submission.request.quest_id != projection.quest_id
            or submission.request.expected_stream_version != projection.audited_stream_version
            or submission.request.expected_tail_event_sha256 != projection.audited_tail_event_sha256
            or submission.request.expected_snapshot_sha256 != projection.audited_snapshot_sha256
        ):
            raise ValueError("submitted action proposal differs from the controller tick")
        rebuilt = materialize_action_proposal(
            request=submission.request,
            draft=submission.draft,
            authority_binding=authority_binding,
            submitted_at=submission.submitted_at,
        )
        if rebuilt != submission:
            raise ValueError("submitted action proposal differs from canonical materialization")
        return submission
    except ActionProposalError:
        raise
    except Exception as exc:  # noqa: BLE001 - external proposal verification fails closed
        raise ActionProposalError("submitted action proposal verification failed closed") from exc


class ActionProposalStepAdapter:
    """Route one proposal step to a powerless, durable materialization service."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        proposals: ActionProposalMaterializationPort,
    ) -> None:
        try:
            frozen = ControllerStepAdapterManifest.model_validate(
                manifest.model_dump(mode="python")
            )
            binding = ControllerStepAuthorityBinding.model_validate(
                proposals.authority_binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("action proposal adapter dependencies are invalid") from exc
        if frozen.step not in _PROPOSAL_STEPS:
            raise ValueError("action proposal adapter requires an exact proposal step")
        if (
            manifest != frozen
            or frozen.authorities != (binding,)
            or binding.role is not ControllerStepAuthorityRole.ACTION_PROPOSAL
        ):
            raise ValueError("action proposal service differs from its deployment manifest")
        self.manifest = frozen
        self._proposals = proposals
        self._authority_binding = binding

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            if (
                plan_recovery_tick(projection) != plan
                or plan.step is not self.manifest.step
                or projection.quest_id != wakeup.quest_id
            ):
                raise ActionProposalError("action proposal step received stale controller input")
            submission = self._proposals.materialize_and_submit(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
            )
            submission = verify_submitted_action_proposal(
                submission=submission,
                wakeup=wakeup,
                projection=projection,
                plan=plan,
                authority_binding=self._authority_binding,
            )
        except ActionProposalBlocked as exc:
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.BLOCKED,
                result_artifact_sha256s=(),
                blocker_codes=exc.blocker_codes,
            )
        except ActionProposalError as exc:
            raise ControllerStepExecutionError("action proposal step failed closed") from exc
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.AWAITING_AUTHORITY,
            result_artifact_sha256s=tuple(
                sorted(
                    (
                        submission.action.object_sha256,
                        submission.command_proposal.proposal_sha256,
                        submission.submission_sha256,
                    )
                )
            ),
            blocker_codes=(),
        )


__all__ = [
    "ActionProposalBlocked",
    "ActionProposalContextSourcePort",
    "ActionProposalDraft",
    "ActionProposalDraftProviderPort",
    "ActionProposalError",
    "ActionProposalMaterializationPort",
    "ActionProposalSubmissionStorePort",
    "ActionProposalStepAdapter",
    "ActionProposalTarget",
    "ActionProposalTargetLifecycle",
    "ControllerActionProposalRequest",
    "SubmittedActionProposal",
    "materialize_action_proposal",
    "verify_submitted_action_proposal",
]
