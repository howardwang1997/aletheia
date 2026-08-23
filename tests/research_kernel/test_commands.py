from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchScopeBinding,
    authorize_research_proposal,
    verify_research_command_authorization,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationError,
    ResearchAuthorizationKey,
    ResearchAuthorizationPolicyProposalV1,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationRole,
    ResearchAuthorizationTrustKey,
    ResearchAuthorizationTrustRootV1,
    certify_research_authorization_policy,
    ed25519_key_id,
    ed25519_public_key_hex,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    ActionRejectedPayload,
    CharterActivatedPayload,
    ContinueCommittedPayload,
    ContinueDirective,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ResearchCharterVersion,
    ResearchActionProposal,
    ResearchProblemVersion,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
    canonical_json_bytes,
    emergency_halt_action_ref,
)

_HASH = "a" * 64
_QUEST_ID = "qst_" + "1" * 32
_ROOT_BRANCH = "rbr_" + "2" * 32
_AT = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
_ROOT_PRIVATE = b"\x10" * 32
_PRIVATE_KEYS = {
    ResearchAuthorizationRole.COMMISSIONING: b"\x21" * 32,
    ResearchAuthorizationRole.ORDINARY: b"\x22" * 32,
    ResearchAuthorizationRole.AMENDMENT: b"\x23" * 32,
    ResearchAuthorizationRole.EMERGENCY: b"\x24" * 32,
}
_PRINCIPALS = {
    ResearchAuthorizationRole.COMMISSIONING: "human:commissioner",
    ResearchAuthorizationRole.ORDINARY: "agent:operator",
    ResearchAuthorizationRole.AMENDMENT: "human:amender",
    ResearchAuthorizationRole.EMERGENCY: "human:emergency",
}


def _authority(
    *,
    quest_id: str = _QUEST_ID,
    root_expires_at: datetime | None = None,
    key_windows: dict[ResearchAuthorizationRole, tuple[datetime, datetime]] | None = None,
    extra_keys: tuple[ResearchAuthorizationKey, ...] = (),
) -> tuple[ResearchAuthorizationTrustRootV1, ResearchAuthorizationPolicyV1]:
    root_public = ed25519_public_key_hex(_ROOT_PRIVATE)
    trust_root = ResearchAuthorizationTrustRootV1(
        trust_root_id="rat_" + "3" * 32,
        frozen_at=_AT - timedelta(days=3),
        commissioning_keys=(
            ResearchAuthorizationTrustKey(
                key_id=ed25519_key_id(root_public),
                principal_id="deployment:commissioner",
                public_key_ed25519_hex=root_public,
                valid_from=_AT - timedelta(days=4),
                expires_at=root_expires_at or _AT + timedelta(days=30),
            ),
        ),
    )
    keys = []
    for role, private_key in _PRIVATE_KEYS.items():
        public = ed25519_public_key_hex(private_key)
        valid_from, expires_at = (key_windows or {}).get(
            role,
            (_AT - timedelta(days=1), _AT + timedelta(days=10)),
        )
        keys.append(
            ResearchAuthorizationKey(
                key_id=ed25519_key_id(public),
                principal_id=_PRINCIPALS[role],
                role=role,
                public_key_ed25519_hex=public,
                valid_from=valid_from,
                expires_at=expires_at,
            )
        )
    keys.extend(extra_keys)
    proposal = ResearchAuthorizationPolicyProposalV1(
        policy_id="rap_" + "4" * 32,
        quest_id=quest_id,
        trust_root_sha256=trust_root.trust_root_sha256,
        frozen_at=_AT - timedelta(hours=2),
        keys=tuple(sorted(keys, key=lambda item: item.key_id)),
    )
    policy = certify_research_authorization_policy(
        proposal,
        trust_root=trust_root,
        root_key_id=trust_root.commissioning_keys[0].key_id,
        private_key=_ROOT_PRIVATE,
        certified_at=_AT - timedelta(hours=1),
    )
    return trust_root, policy


def _charter(
    *, quest_id: str = _QUEST_ID, expires_at: datetime | None = _AT + timedelta(days=9)
) -> ResearchCharterVersion:
    return ResearchCharterVersion(
        quest_id=quest_id,
        charter_id="charter:command-test",
        version=1,
        mission="Test the pure command authority boundary",
        value_boundaries=("honesty",),
        included_scopes=("fixture",),
        allowed_action_classes=("characterize",),
        safety_policy_sha256=_HASH,
        ethics_policy_sha256=_HASH,
        license_policy_sha256=_HASH,
        privacy_policy_sha256=_HASH,
        egress_policy_sha256=_HASH,
        budget_policy_sha256=_HASH,
        approval_policy_sha256=_HASH,
        publication_policy_sha256=_HASH,
        amendment_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],),
        emergency_stop_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.EMERGENCY],),
        authorized_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.COMMISSIONING],
        authority_receipt_sha256=_HASH,
        authorized_at=_AT,
        expires_at=expires_at,
    )


def _proposal(*, charter: ResearchCharterVersion | None = None) -> ResearchCommandProposal:
    charter = charter or _charter()
    return ResearchCommandProposal(
        quest_id=charter.quest_id,
        scope_binding=ResearchScopeBinding(quest_id=charter.quest_id),
        expected_stream_version=0,
        expected_tail_event_sha256=None,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter.object_ref,
            root_branch_id=_ROOT_BRANCH,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=_AT,
    )


def _authorized(
    *,
    idempotency_key: str = "fixture:genesis",
    source_event_key: str | None = "fixture:source",
) -> tuple[
    AuthorizedResearchCommand,
    ResearchAuthorizationTrustRootV1,
    ResearchAuthorizationPolicyV1,
    ResearchCharterVersion,
]:
    charter = _charter()
    trust_root, policy = _authority()
    command = authorize_research_proposal(
        _proposal(charter=charter),
        idempotency_key=idempotency_key,
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role_key(policy, ResearchAuthorizationRole.COMMISSIONING).key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
        source_event_key=source_event_key,
    )
    return command, trust_root, policy, charter


def _role_key(
    policy: ResearchAuthorizationPolicyV1, role: ResearchAuthorizationRole
) -> ResearchAuthorizationKey:
    return next(item for item in policy.keys if item.role is role)


def test_scope_binding_is_content_addressed_and_campaign_requires_program() -> None:
    quest_only = ResearchScopeBinding(quest_id=_QUEST_ID)
    program = ResearchScopeBinding(quest_id=_QUEST_ID, program_id="prg_" + "3" * 32)

    assert quest_only.binding_sha256 != program.binding_sha256
    assert ResearchScopeBinding.model_validate_json(quest_only.model_dump_json()) == quest_only
    with pytest.raises(ValidationError, match="campaign scope requires a program"):
        ResearchScopeBinding(quest_id=_QUEST_ID, campaign_id="cmp_" + "4" * 32)


def test_model_proposal_has_no_commit_authority_and_authorization_is_signed() -> None:
    proposal = _proposal()
    command, trust_root, policy, charter = _authorized()

    assert not hasattr(proposal, "to_event")
    assert command.proposal_sha256 == proposal.proposal_sha256
    assert command.principal_id == _PRINCIPALS[ResearchAuthorizationRole.COMMISSIONING]
    assert len(command.authorization_signature_ed25519_hex) == 128
    assert command.authorization_receipt_sha256 != charter.authority_receipt_sha256
    assert command.command_id.startswith("rkc_")
    assert (
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=1),
            active_charter=None,
            admitted_object=charter,
        )
        is ResearchAuthorizationRole.COMMISSIONING
    )


def test_command_binds_expected_version_and_exact_parent_hash() -> None:
    command, *_ = _authorized()
    event = command.to_event(
        sequence=1,
        parent_event_sha256=None,
        committed_at=_AT + timedelta(seconds=1),
    )

    assert event.sequence == 1
    assert event.parent_event_sha256 is None
    assert event.command_sha256 == command.command_sha256
    with pytest.raises(ValueError, match="sequence"):
        command.to_event(sequence=2, parent_event_sha256=None, committed_at=_AT)


def test_non_genesis_command_requires_exact_expected_tail() -> None:
    proposal = _proposal().model_copy(
        update={
            "expected_stream_version": 1,
            "expected_tail_event_sha256": "c" * 64,
            "event_type": EventType.CHARTER_ACTIVATED,
        }
    )
    with pytest.raises(ValidationError, match="only expected stream version zero"):
        ResearchCommandProposal.model_validate(proposal.model_dump(mode="python"))

    raw = _proposal().model_dump(mode="python")
    raw["expected_stream_version"] = 1
    raw["expected_tail_event_sha256"] = None
    raw["event_type"] = EventType.ACTION_REJECTED
    raw["payload"] = ActionRejectedPayload(
        action_id="action:missing-tail",
        branch_id=_ROOT_BRANCH,
        reason_codes=("invalid",),
    )
    with pytest.raises(ValidationError, match="omit the expected tail hash"):
        ResearchCommandProposal.model_validate(raw)


def test_same_idempotency_identity_with_changed_signed_content_has_distinct_hash() -> None:
    first, *_ = _authorized(source_event_key="fixture:first")
    second, *_ = _authorized(source_event_key="fixture:second")

    assert first.command_id == second.command_id
    assert first.command_sha256 != second.command_sha256


def test_receipt_and_signed_content_tampering_fail_closed() -> None:
    command, trust_root, policy, charter = _authorized()
    raw = command.model_dump(mode="python")
    raw["authorization_receipt_sha256"] = "c" * 64
    with pytest.raises(ValidationError, match="receipt does not match"):
        AuthorizedResearchCommand.model_validate(raw)

    bypass = command.model_copy(update={"authorization_signature_ed25519_hex": "0" * 128})
    with pytest.raises(ValidationError, match="receipt does not match"):
        verify_research_command_authorization(
            bypass,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=1),
            active_charter=None,
            admitted_object=charter,
        )


def test_wrong_role_cannot_sign_genesis() -> None:
    trust_root, policy = _authority()
    ordinary = _role_key(policy, ResearchAuthorizationRole.ORDINARY)

    with pytest.raises(ResearchAuthorizationError, match="exact required role"):
        authorize_research_proposal(
            _proposal(),
            idempotency_key="fixture:wrong-role",
            authorization_policy=policy,
            trust_root=trust_root,
            authorization_key_id=ordinary.key_id,
            private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
            authorized_at=_AT,
        )


def test_backdated_policy_certificate_cannot_outlive_root_at_genesis_commit() -> None:
    trust_root, policy = _authority(root_expires_at=_AT + timedelta(milliseconds=500))
    charter = _charter()
    key = _role_key(policy, ResearchAuthorizationRole.COMMISSIONING)
    command = authorize_research_proposal(
        _proposal(charter=charter),
        idempotency_key="fixture:expired-root",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=key.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
    )

    with pytest.raises(ResearchAuthorizationError, match="genesis authorization time"):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=1),
            active_charter=None,
            admitted_object=charter,
        )


@pytest.mark.parametrize(
    ("charter", "authority_kwargs", "message"),
    [
        (
            _charter(expires_at=None),
            {},
            "finite authorization expiry",
        ),
        (
            _charter(expires_at=_AT + timedelta(days=11)),
            {},
            "emergency authority is not continuously available",
        ),
        (
            _charter(expires_at=_AT + timedelta(days=1)),
            {
                "key_windows": {
                    ResearchAuthorizationRole.EMERGENCY: (
                        _AT - timedelta(days=2),
                        _AT - timedelta(seconds=1),
                    )
                }
            },
            "emergency authority is not continuously available",
        ),
    ],
)
def test_genesis_rejects_a_charter_without_continuous_emergency_authority(
    charter: ResearchCharterVersion,
    authority_kwargs: dict[str, object],
    message: str,
) -> None:
    trust_root, policy = _authority(**authority_kwargs)
    commissioning = _role_key(policy, ResearchAuthorizationRole.COMMISSIONING)
    command = authorize_research_proposal(
        _proposal(charter=charter),
        idempotency_key=f"fixture:unsafe-charter:{charter.object_sha256}",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=commissioning.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
    )

    with pytest.raises(ResearchAuthorizationError, match=message):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(milliseconds=1),
            active_charter=None,
            admitted_object=charter,
        )


@pytest.mark.parametrize(
    ("role", "message"),
    [
        (
            ResearchAuthorizationRole.AMENDMENT,
            "amendment authority is not continuously available",
        ),
        (
            ResearchAuthorizationRole.ORDINARY,
            "no continuously available ordinary authority",
        ),
    ],
)
def test_genesis_rejects_other_required_role_coverage_gaps(
    role: ResearchAuthorizationRole,
    message: str,
) -> None:
    charter = _charter(expires_at=_AT + timedelta(days=5))
    trust_root, policy = _authority(
        key_windows={
            role: (_AT - timedelta(days=1), _AT + timedelta(days=1)),
        }
    )
    commissioning = _role_key(policy, ResearchAuthorizationRole.COMMISSIONING)
    command = authorize_research_proposal(
        _proposal(charter=charter),
        idempotency_key=f"fixture:{role.value}-coverage-gap",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=commissioning.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
    )

    with pytest.raises(ResearchAuthorizationError, match=message):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(milliseconds=1),
            active_charter=None,
            admitted_object=charter,
        )


def test_adjacent_same_principal_keys_provide_continuous_emergency_coverage() -> None:
    second_private_key = b"\x25" * 32
    second_public_key = ed25519_public_key_hex(second_private_key)
    second_emergency_key = ResearchAuthorizationKey(
        key_id=ed25519_key_id(second_public_key),
        principal_id=_PRINCIPALS[ResearchAuthorizationRole.EMERGENCY],
        role=ResearchAuthorizationRole.EMERGENCY,
        public_key_ed25519_hex=second_public_key,
        valid_from=_AT + timedelta(days=2),
        expires_at=_AT + timedelta(days=6),
    )
    trust_root, policy = _authority(
        key_windows={
            ResearchAuthorizationRole.EMERGENCY: (
                _AT - timedelta(days=1),
                _AT + timedelta(days=2),
            )
        },
        extra_keys=(second_emergency_key,),
    )
    charter = _charter(expires_at=_AT + timedelta(days=6))
    commissioning = _role_key(policy, ResearchAuthorizationRole.COMMISSIONING)
    command = authorize_research_proposal(
        _proposal(charter=charter),
        idempotency_key="fixture:adjacent-emergency-keys",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=commissioning.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
    )

    assert (
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(milliseconds=1),
            active_charter=None,
            admitted_object=charter,
        )
        is ResearchAuthorizationRole.COMMISSIONING
    )


def test_commands_are_closed_utc_and_canonical_json_roundtrippable() -> None:
    command, *_ = _authorized()

    assert AuthorizedResearchCommand.model_validate_json(command.model_dump_json()) == command
    assert canonical_json_bytes(command) == canonical_json_bytes(
        AuthorizedResearchCommand.model_validate(command.model_dump(mode="python"))
    )
    raw = command.model_dump(mode="python")
    raw["authorized_at"] = _AT.astimezone(timezone(timedelta(hours=12)))
    with pytest.raises(ValidationError, match="authorized_at must be timezone-aware UTC"):
        AuthorizedResearchCommand.model_validate(raw)


def test_command_rejects_cross_quest_direct_object_reference() -> None:
    other = _charter(quest_id="qst_" + "9" * 32)
    raw = _proposal().model_dump(mode="python")
    raw["payload"] = CharterActivatedPayload(
        charter_ref=other.object_ref,
        root_branch_id=_ROOT_BRANCH,
    )
    with pytest.raises(ValidationError, match="another Quest"):
        ResearchCommandProposal.model_validate(raw)


def test_ordinary_authority_expires_but_emergency_stop_survives_charter_expiry() -> None:
    charter = _charter(expires_at=_AT + timedelta(seconds=3))
    trust_root, policy = _authority()
    ordinary = _role_key(policy, ResearchAuthorizationRole.ORDINARY)
    problem = ResearchProblemVersion(
        problem_id="problem:expiry",
        quest_id=_QUEST_ID,
        charter_ref=charter.object_ref,
        version=1,
        title="Bounded expiry test",
        statement="Ordinary commits must stop at charter expiry.",
        scope="fixture",
        importance_rationale="Fail closed at the authority boundary.",
        unknowns=("expiry",),
        semantic_delta="initial",
        authored_by_principal_id="model:planner",
        authored_at=_AT + timedelta(seconds=1),
    )
    ordinary_proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID),
        expected_stream_version=1,
        expected_tail_event_sha256="5" * 64,
        event_type=EventType.PROBLEM_ADMITTED,
        payload={
            "kind": "problem_admitted",
            "problem_ref": problem.object_ref,
            "branch_id": _ROOT_BRANCH,
        },
        proposed_by_principal_id="model:planner",
        proposed_at=_AT + timedelta(seconds=1),
    )
    ordinary_command = authorize_research_proposal(
        ordinary_proposal,
        idempotency_key="fixture:expired-ordinary",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=ordinary.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=_AT + timedelta(seconds=2),
    )
    with pytest.raises(ResearchAuthorizationError, match="charter is not valid"):
        verify_research_command_authorization(
            ordinary_command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=4),
            active_charter=charter,
            admitted_object=problem,
        )

    emergency = _role_key(policy, ResearchAuthorizationRole.EMERGENCY)
    decision = TransitionDecision(
        transition_id="transition:emergency",
        quest_id=_QUEST_ID,
        charter_ref=charter.object_ref,
        source_graph_sha256="6" * 64,
        selected_action_ref=emergency_halt_action_ref(
            quest_id=_QUEST_ID,
            charter_ref=charter.object_ref,
        ),
        directive=StopDirective(
            branch_id=_ROOT_BRANCH,
            stop_reason=StopReason.EMERGENCY_STOP,
        ),
        budget_receipt_sha256="8" * 64,
        risk_receipt_sha256="9" * 64,
        policy_receipt_sha256="a" * 64,
        reason_codes=("emergency",),
        rationale="The emergency role remains available after ordinary charter expiry.",
        decided_by_principal_id="safety:monitor",
        decided_at=_AT + timedelta(seconds=4),
    )
    emergency_proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID),
        expected_stream_version=1,
        expected_tail_event_sha256="5" * 64,
        event_type=EventType.STOP_COMMITTED,
        payload=StopCommittedPayload(decision=decision),
        proposed_by_principal_id="safety:monitor",
        proposed_at=_AT + timedelta(seconds=4),
    )
    emergency_command = authorize_research_proposal(
        emergency_proposal,
        idempotency_key="fixture:emergency",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=emergency.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.EMERGENCY],
        authorized_at=_AT + timedelta(seconds=4),
    )
    assert (
        verify_research_command_authorization(
            emergency_command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=5),
            active_charter=charter,
            admitted_object=None,
        )
        is ResearchAuthorizationRole.EMERGENCY
    )


def test_signed_ordinary_command_cannot_admit_charter_forbidden_action_class() -> None:
    charter = _charter()
    trust_root, policy = _authority()
    ordinary = _role_key(policy, ResearchAuthorizationRole.ORDINARY)
    action = ResearchActionProposal(
        action_id="action:forbidden-authority",
        quest_id=_QUEST_ID,
        charter_ref=charter.object_ref,
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id="question:fixture",
            object_sha256="b" * 64,
            quest_id=_QUEST_ID,
        ),
        basis_tail_event_sha256="c" * 64,
        kind=ActionKind.CHARACTERIZE,
        epistemic_purpose="Verify charter action-class enforcement.",
        candidate_outcomes=("blocked",),
        cost_receipt_sha256="d" * 64,
        risk_receipt_sha256="e" * 64,
        requested_authority_class="wet_lab:unapproved",
        proposed_by_principal_id="model:planner",
        proposed_at=_AT + timedelta(seconds=1),
    )
    proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID),
        expected_stream_version=1,
        expected_tail_event_sha256="f" * 64,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=action.object_ref,
            branch_id=_ROOT_BRANCH,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=_AT + timedelta(seconds=1),
    )
    command = authorize_research_proposal(
        proposal,
        idempotency_key="fixture:forbidden-action",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=ordinary.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=_AT + timedelta(seconds=2),
    )

    with pytest.raises(ResearchAuthorizationError, match="forbidden by the charter"):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=3),
            active_charter=charter,
            admitted_object=action,
        )


def test_charter_revision_can_revoke_authority_from_a_previously_proposed_action() -> None:
    original = _charter()
    revised = ResearchCharterVersion.model_validate(
        {
            **original.model_dump(mode="python"),
            "version": 2,
            "revision_parent_sha256": original.object_sha256,
            "allowed_action_classes": ("analysis",),
            "authorized_by_principal_id": _PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],
            "authority_receipt_sha256": "1" * 64,
            "authorized_at": _AT + timedelta(seconds=1),
        }
    )
    action = ResearchActionProposal(
        action_id="action:revoked-after-proposal",
        quest_id=_QUEST_ID,
        charter_ref=original.object_ref,
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id="question:fixture",
            object_sha256="2" * 64,
            quest_id=_QUEST_ID,
        ),
        basis_tail_event_sha256="3" * 64,
        kind=ActionKind.CHARACTERIZE,
        epistemic_purpose="Prove a charter revision can withdraw pending authority.",
        candidate_outcomes=("withdrawn",),
        cost_receipt_sha256="4" * 64,
        risk_receipt_sha256="5" * 64,
        requested_authority_class="characterize",
        proposed_by_principal_id="model:planner",
        proposed_at=_AT,
    )
    trust_root, policy = _authority()
    ordinary = _role_key(policy, ResearchAuthorizationRole.ORDINARY)
    proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID),
        expected_stream_version=3,
        expected_tail_event_sha256="6" * 64,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=action.action_id,
            branch_id=_ROOT_BRANCH,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=_AT + timedelta(seconds=2),
    )
    command = authorize_research_proposal(
        proposal,
        idempotency_key="fixture:revoked-action",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=ordinary.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=_AT + timedelta(seconds=2),
    )

    with pytest.raises(ResearchAuthorizationError, match="current charter version"):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=3),
            active_charter=revised,
            admitted_object=None,
            resolved_action=action,
        )


def test_transition_rechecks_selected_action_against_current_charter() -> None:
    original = _charter()
    revised = ResearchCharterVersion.model_validate(
        {
            **original.model_dump(mode="python"),
            "version": 2,
            "revision_parent_sha256": original.object_sha256,
            "authorized_by_principal_id": _PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],
            "authority_receipt_sha256": "7" * 64,
            "authorized_at": _AT + timedelta(seconds=1),
        }
    )
    action = ResearchActionProposal(
        action_id="action:stale-charter-transition",
        quest_id=_QUEST_ID,
        charter_ref=original.object_ref,
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id="question:fixture",
            object_sha256="8" * 64,
            quest_id=_QUEST_ID,
        ),
        basis_tail_event_sha256="9" * 64,
        kind=ActionKind.CONTINUE,
        epistemic_purpose="Ensure transitions cannot execute stale Charter authority.",
        candidate_outcomes=("blocked",),
        cost_receipt_sha256="a" * 64,
        risk_receipt_sha256="b" * 64,
        requested_authority_class="characterize",
        proposed_by_principal_id="model:planner",
        proposed_at=_AT,
    )
    decision = TransitionDecision(
        transition_id="transition:stale-charter",
        quest_id=_QUEST_ID,
        charter_ref=revised.object_ref,
        source_graph_sha256="c" * 64,
        selected_action_ref=action.object_ref,
        directive=ContinueDirective(branch_id=_ROOT_BRANCH),
        budget_receipt_sha256="d" * 64,
        risk_receipt_sha256="e" * 64,
        policy_receipt_sha256="f" * 64,
        reason_codes=("continue",),
        rationale="A transition must re-evaluate the selected action under the live Charter.",
        decided_by_principal_id="model:controller",
        decided_at=_AT + timedelta(seconds=2),
    )
    trust_root, policy = _authority()
    ordinary = _role_key(policy, ResearchAuthorizationRole.ORDINARY)
    proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID),
        expected_stream_version=3,
        expected_tail_event_sha256="1" * 64,
        event_type=EventType.CONTINUE_COMMITTED,
        payload=ContinueCommittedPayload(decision=decision),
        proposed_by_principal_id="model:controller",
        proposed_at=_AT + timedelta(seconds=2),
    )
    command = authorize_research_proposal(
        proposal,
        idempotency_key="fixture:stale-charter-transition",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=ordinary.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=_AT + timedelta(seconds=2),
    )

    with pytest.raises(ResearchAuthorizationError, match="current charter version"):
        verify_research_command_authorization(
            command,
            authorization_policy=policy,
            trust_root=trust_root,
            committed_at=_AT + timedelta(seconds=3),
            active_charter=revised,
            admitted_object=None,
            resolved_action=action,
        )
