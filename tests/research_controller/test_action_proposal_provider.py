from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.research_controller.action_proposal_provider import (
    ConservativeProposalCostReceipt,
    ConservativeProposalRiskReceipt,
    DeterministicActionProposalPolicyPin,
    DeterministicActionProposalProvider,
)
from aletheia.research_controller.action_proposals import (
    ActionProposalBlocked,
    ActionProposalError,
    materialize_action_proposal,
)
from aletheia.research_controller.contracts import ControllerStep
from aletheia.research_kernel.schemas import ActionKind, canonical_json_bytes

_TEST_ROOT = Path(__file__).resolve().parent
if str(_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_TEST_ROOT))

from test_action_proposals import NOW, _binding, _request  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _policy(*, preferences: tuple[ActionKind, ...] = (ActionKind.DISCRIMINATE,)):
    implementation = (
        Path(__file__).resolve().parents[2]
        / "aletheia/research_controller/action_proposal_provider.py"
    ).resolve()
    return DeterministicActionProposalPolicyPin(
        provider_implementation_sha256=hashlib.sha256(implementation.read_bytes()).hexdigest(),
        provider_principal_id="service:action-proposal",
        initial_action_kind_preference=preferences,
        initial_epistemic_purpose="Select a bounded action against the audited question.",
        redesign_epistemic_purpose="Repair the exact compiler blocker without changing evidence.",
        followup_epistemic_purpose="Discriminate the exact continuation alternatives.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        requested_authority_class="scientific-measurement",
        cost_screening_policy_sha256=_sha("conservative-cost-policy"),
        risk_screening_policy_sha256=_sha("conservative-risk-policy"),
    )


def _provider(
    *,
    policy: DeterministicActionProposalPolicyPin | None = None,
    when=NOW + timedelta(seconds=1),
):
    policy = policy or _policy()
    return DeterministicActionProposalProvider(
        policy=policy,
        implementation_sha256=policy.provider_implementation_sha256,
        principal_id=policy.provider_principal_id,
        clock=lambda: when,
    )


@pytest.mark.parametrize(
    ("step", "kind", "purpose"),
    (
        (
            ControllerStep.PROPOSE_ACTION,
            ActionKind.DISCRIMINATE,
            "Select a bounded action against the audited question.",
        ),
        (
            ControllerStep.PROPOSE_REDESIGN,
            ActionKind.REFINE,
            "Repair the exact compiler blocker without changing evidence.",
        ),
        (
            ControllerStep.PROPOSE_FOLLOWUP,
            ActionKind.FORK,
            "Discriminate the exact continuation alternatives.",
        ),
    ),
)
def test_provider_selects_only_the_exact_audited_target_and_kind(step, kind, purpose) -> None:
    request = _request(step)
    provider = _provider()

    draft = provider.propose_action(request)
    verified = provider.verify_action_proposal_draft(request=request, draft=draft)
    submission = materialize_action_proposal(
        request=request,
        draft=verified,
        authority_binding=_binding(),
        submitted_at=NOW + timedelta(seconds=2),
    )

    assert verified.target_sha256 == request.targets[0].target_sha256
    assert verified.kind is kind
    assert verified.epistemic_purpose == purpose
    assert submission.action.evidence_refs == request.required_evidence_refs
    assert submission.action.kind is kind
    assert submission.awaiting_independent_kernel_authority
    assert not submission.kernel_command_signed
    assert not submission.kernel_state_mutated


def test_cost_and_risk_receipts_are_reconstructable_non_authority_identities() -> None:
    request = _request(ControllerStep.PROPOSE_ACTION)
    policy = _policy()
    draft = _provider(policy=policy).propose_action(request)
    cost = ConservativeProposalCostReceipt(
        request_sha256=request.request_sha256,
        target_sha256=draft.target_sha256,
        action_kind=draft.kind,
        screening_policy_sha256=policy.cost_screening_policy_sha256,
    )
    risk = ConservativeProposalRiskReceipt(
        request_sha256=request.request_sha256,
        target_sha256=draft.target_sha256,
        action_kind=draft.kind,
        screening_policy_sha256=policy.risk_screening_policy_sha256,
    )

    assert draft.cost_receipt_sha256 == cost.receipt_sha256
    assert draft.risk_receipt_sha256 == risk.receipt_sha256
    assert cost.estimated_amount is None and cost.currency is None
    assert not cost.execution_authorized
    assert not risk.safety_approved and not risk.external_action_approved
    encoded = b"".join(canonical_json_bytes(item) for item in (policy, cost, risk, draft))
    assert b"private_key" not in encoded
    assert b'"external_model_callback_allowed":false' in encoded
    assert b'"execution_authorized":false' in encoded


def test_restart_verification_reconstructs_every_provider_owned_field() -> None:
    request = _request(ControllerStep.PROPOSE_ACTION)
    provider = _provider()
    draft = provider.propose_action(request)
    restarted = _provider(when=NOW + timedelta(hours=1))

    assert restarted.verify_action_proposal_draft(request=request, draft=draft) == draft
    for field, value in (
        ("epistemic_purpose", "Rebound purpose."),
        ("cost_receipt_sha256", _sha("rebound-cost")),
        ("requested_authority_class", "rebound-authority"),
        ("action_id", "action:rebound"),
    ):
        with pytest.raises(ActionProposalError, match="failed closed"):
            restarted.verify_action_proposal_draft(
                request=request,
                draft=draft.model_copy(update={field: value}),
            )

    with pytest.raises(ActionProposalError, match="failed closed"):
        restarted.verify_action_proposal_draft(
            request=request,
            draft=draft.model_copy(update={"proposed_at": NOW - timedelta(seconds=1)}),
        )


def test_action_identity_is_content_derived_not_clock_derived() -> None:
    request = _request(ControllerStep.PROPOSE_ACTION)
    first = _provider(when=NOW + timedelta(seconds=1)).propose_action(request)
    second = _provider(when=NOW + timedelta(seconds=5)).propose_action(request)

    assert first.action_id == second.action_id
    assert first.draft_sha256 != second.draft_sha256


def test_policy_without_an_eligible_initial_kind_returns_canonical_blocker() -> None:
    request = _request(ControllerStep.PROPOSE_ACTION)
    provider = _provider(policy=_policy(preferences=(ActionKind.FALSIFY,)))

    with pytest.raises(ActionProposalBlocked) as exc_info:
        provider.propose_action(request)

    assert exc_info.value.blocker_codes == ("action_proposal:no_policy_eligible_action",)


def test_provider_rejects_rebound_implementation_or_principal() -> None:
    policy = _policy()
    with pytest.raises(ValueError, match="differs from its policy"):
        DeterministicActionProposalProvider(
            policy=policy,
            implementation_sha256=_sha("other-source"),
            principal_id=policy.provider_principal_id,
        )
    with pytest.raises(ValueError, match="differs from its policy"):
        DeterministicActionProposalProvider(
            policy=policy,
            implementation_sha256=policy.provider_implementation_sha256,
            principal_id="service:other-provider",
        )
