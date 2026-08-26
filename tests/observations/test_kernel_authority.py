from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from aletheia.observations.kernel_authority import (
    ExactObservationKernelAuthority,
    ObservationKernelAuthorityError,
    ObservationKernelPolicyAssignment,
)
from aletheia.research_kernel.commands import ResearchCommandProposal
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.schemas import EventType, ObservationIncorporatedPayload

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "research_controller",
    _TESTS / "observations",
    _TESTS / "research_kernel",
):
    if str(_fixture_dir) not in sys.path:
        sys.path.insert(0, str(_fixture_dir))

from test_atomic_admission import _case as _atomic_case  # noqa: E402
from test_commands import (  # noqa: E402
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
    _role_key as command_role_key,
)
from test_scientific_bridge import _commit_admission  # noqa: E402


def _authority(monkeypatch: pytest.MonkeyPatch):
    bridge, decision, audit, _verification = _atomic_case(monkeypatch)
    committed = _commit_admission(bridge, decision)
    trust_root, policy = command_authority(quest_id=audit.quest_id)
    key = command_role_key(policy, ResearchAuthorizationRole.ORDINARY)
    assignment = ObservationKernelPolicyAssignment(
        quest_id=audit.quest_id,
        scope_binding=audit.scope_binding,
        authorization_policy=policy,
    )
    authority = ExactObservationKernelAuthority(
        trust_root=trust_root,
        assignments=(assignment,),
        authorization_key_id=key.key_id,
        private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
    )
    decision_message = committed.message.decision.message
    validation = decision_message.committed_validation_receipt.message.receipt.message
    binding = validation.raw_run.scientific_authorization.message.action_protocol_binding
    protocol = binding.compilation_request.protocol
    assert protocol.world_model is not None
    payload = ObservationIncorporatedPayload(
        branch_id=protocol.graph_scope.branch_id,
        action_id=binding.action.action_id,
        scientific_slot_id=decision_message.scientific_slot_id,
        committed_admission_sha256=committed.committed_admission_sha256,
        scientific_observation_sha256=decision_message.admitted_observation_sha256,
        outcome=validation.outcome.value,
        source_world_model_sha256=protocol.world_model.world_model_sha256,
    )
    proposal = ResearchCommandProposal(
        quest_id=audit.quest_id,
        scope_binding=audit.scope_binding,
        expected_stream_version=audit.state.stream_version,
        expected_tail_event_sha256=audit.state.tail_event_sha256,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=payload,
        proposed_by_principal_id="principal.observation.atomic-coordinator",
        proposed_at=committed.message.committed_at,
    )
    return authority, assignment, committed, proposal


def test_exact_kernel_authority_signs_only_the_bound_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _assignment, committed, proposal = _authority(monkeypatch)
    decision = committed.message.decision
    idempotency = f"observation-admission:{decision.decision_sha256}"
    source = f"scientific-slot:{decision.message.scientific_slot_id}"

    command = authority.authorize_observation_incorporation(
        proposal=proposal,
        committed_admission=committed,
        idempotency_key=idempotency,
        source_event_key=source,
    )

    assert command.proposal_sha256 == proposal.proposal_sha256
    assert command.payload == proposal.payload
    assert command.idempotency_key == idempotency
    assert command.source_event_key == source
    assert command.principal_id == authority.principal_id
    assert command.authorized_at == proposal.proposed_at


def test_exact_kernel_authority_rejects_rebound_slot_or_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _assignment, committed, proposal = _authority(monkeypatch)
    decision = committed.message.decision
    idempotency = f"observation-admission:{decision.decision_sha256}"
    source = f"scientific-slot:{decision.message.scientific_slot_id}"
    rebound_payload = proposal.payload.model_copy(update={"scientific_slot_id": "sos_" + "f" * 32})
    rebound = proposal.model_copy(update={"payload": rebound_payload})

    with pytest.raises(ObservationKernelAuthorityError, match="rebound"):
        authority.authorize_observation_incorporation(
            proposal=rebound,
            committed_admission=committed,
            idempotency_key=idempotency,
            source_event_key=source,
        )
    with pytest.raises(ObservationKernelAuthorityError, match="rebound"):
        authority.authorize_observation_incorporation(
            proposal=proposal,
            committed_admission=committed,
            idempotency_key=idempotency,
            source_event_key="scientific-slot:sos_" + "0" * 32,
        )


def test_exact_kernel_authority_rejects_wrong_private_key_and_noncanonical_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authority_service, assignment, _committed, _proposal = _authority(monkeypatch)
    trust_root, policy = command_authority(quest_id=assignment.quest_id)
    key = command_role_key(policy, ResearchAuthorizationRole.ORDINARY)

    with pytest.raises(ObservationKernelAuthorityError, match="private key"):
        ExactObservationKernelAuthority(
            trust_root=trust_root,
            assignments=(assignment,),
            authorization_key_id=key.key_id,
            private_key=hashlib.sha256(b"another-kernel-key").digest(),
        )
    with pytest.raises(ObservationKernelAuthorityError, match="unique.*canonical"):
        ExactObservationKernelAuthority(
            trust_root=trust_root,
            assignments=(assignment, assignment),
            authorization_key_id=key.key_id,
            private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        )

    with pytest.raises(ValueError, match="another Quest"):
        ObservationKernelPolicyAssignment(
            quest_id=assignment.quest_id,
            scope_binding=ResearchScopeBinding(quest_id="qst_" + "f" * 32),
            authorization_policy=assignment.authorization_policy,
        )
