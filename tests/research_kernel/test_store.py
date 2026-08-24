"""PR-2 acceptance tests for the authoritative PostgreSQL research event store."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchScopeBinding,
    authorize_research_proposal,
)
from aletheia.research_kernel.policy import (
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
from aletheia.research_kernel.reducer import BranchLifecycle
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    CharterActivatedPayload,
    CharterRevisedPayload,
    EventType,
    ForkCommittedPayload,
    ForkDirective,
    ProblemAdmittedPayload,
    QuestionAdmittedPayload,
    QuestionKind,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchProblemVersion,
    ResearchQuestionVersion,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
    emergency_halt_action_ref,
)
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.persistence import (
    ResearchKernelCommandReceiptRecord,
    ResearchKernelEventRecord,
    ResearchKernelObjectRecord,
    ResearchKernelOutboxRecord,
    ResearchKernelSnapshotRecord,
    ResearchQuestAuthorityRecord,
    ResearchQuestStreamRecord,
)
from aletheia.research_store.store import (
    ResearchAuthorizationError,
    ResearchIdempotencyConflict,
    ResearchKernelStore,
    ResearchStoreError,
    ResearchStoreInvariantError,
    ResearchVersionConflict,
    UncommittedProposalError,
)

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
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


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _identity(prefix: str, label: str) -> str:
    return f"{prefix}_{hashlib.sha256(f'{label}:{uuid.uuid4().hex}'.encode()).hexdigest()[:32]}"


def _authority(
    quest_id: str,
    *,
    label: str,
) -> tuple[ResearchAuthorizationTrustRootV1, ResearchAuthorizationPolicyV1]:
    root_public = ed25519_public_key_hex(_ROOT_PRIVATE)
    trust_root = ResearchAuthorizationTrustRootV1(
        trust_root_id=_identity("rat", label),
        frozen_at=T0 - timedelta(days=3),
        commissioning_keys=(
            ResearchAuthorizationTrustKey(
                key_id=ed25519_key_id(root_public),
                principal_id="deployment:commissioner",
                public_key_ed25519_hex=root_public,
                valid_from=T0 - timedelta(days=4),
                expires_at=T0 + timedelta(days=30),
            ),
        ),
    )
    keys = []
    for role, private_key in _PRIVATE_KEYS.items():
        public_key = ed25519_public_key_hex(private_key)
        keys.append(
            ResearchAuthorizationKey(
                key_id=ed25519_key_id(public_key),
                principal_id=_PRINCIPALS[role],
                role=role,
                public_key_ed25519_hex=public_key,
                valid_from=T0 - timedelta(days=1),
                expires_at=T0 + timedelta(days=10),
            )
        )
    proposal = ResearchAuthorizationPolicyProposalV1(
        policy_id=_identity("rap", label),
        quest_id=quest_id,
        trust_root_sha256=trust_root.trust_root_sha256,
        frozen_at=T0 - timedelta(hours=2),
        keys=tuple(sorted(keys, key=lambda item: item.key_id)),
    )
    return trust_root, certify_research_authorization_policy(
        proposal,
        trust_root=trust_root,
        root_key_id=trust_root.commissioning_keys[0].key_id,
        private_key=_ROOT_PRIVATE,
        certified_at=T0 - timedelta(hours=1),
    )


def _role_key(policy: ResearchAuthorizationPolicyV1, role: ResearchAuthorizationRole):
    return next(item for item in policy.keys if item.role is role)


def _quest_fixture(
    archive_root: Path,
    *,
    label: str,
    idempotency_key: str | None = None,
    program: bool = True,
    expires_at: datetime | None = T0 + timedelta(days=9),
    quest_id: str | None = None,
) -> tuple[
    FilesystemResearchArchive,
    ResearchScopeBinding,
    ResearchCharterVersion,
    AuthorizedResearchCommand,
    str,
    ResearchAuthorizationTrustRootV1,
    ResearchAuthorizationPolicyV1,
]:
    quest_id = quest_id or _identity("qst", label)
    root_branch_id = _identity("rbr", f"{label}:root")
    scope = ResearchScopeBinding(
        quest_id=quest_id,
        program_id=_identity("prg", label) if program else None,
    )
    charter = ResearchCharterVersion(
        quest_id=quest_id,
        charter_id=f"charter:{uuid.uuid4().hex}",
        version=1,
        mission=f"authoritative store acceptance {label}",
        value_boundaries=("scientific_integrity",),
        included_scopes=("bounded_research",),
        allowed_action_classes=("analysis",),
        safety_policy_sha256=_sha(f"{label}:safety"),
        ethics_policy_sha256=_sha(f"{label}:ethics"),
        license_policy_sha256=_sha(f"{label}:license"),
        privacy_policy_sha256=_sha(f"{label}:privacy"),
        egress_policy_sha256=_sha(f"{label}:egress"),
        budget_policy_sha256=_sha(f"{label}:budget"),
        approval_policy_sha256=_sha(f"{label}:approval"),
        publication_policy_sha256=_sha(f"{label}:publication"),
        amendment_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],),
        emergency_stop_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.EMERGENCY],),
        authorized_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.COMMISSIONING],
        authority_receipt_sha256=_sha(f"{label}:charter-authority"),
        authorized_at=T0,
        expires_at=expires_at,
    )
    archive = FilesystemResearchArchive(archive_root)
    archive.archive_object(charter)
    trust_root, policy = _authority(quest_id, label=label)
    proposal = ResearchCommandProposal(
        quest_id=quest_id,
        scope_binding=scope,
        expected_stream_version=0,
        expected_tail_event_sha256=None,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter.object_ref,
            root_branch_id=root_branch_id,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=T0,
    )
    command = authorize_research_proposal(
        proposal,
        idempotency_key=idempotency_key or f"store:{label}:{uuid.uuid4().hex}",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role_key(policy, ResearchAuthorizationRole.COMMISSIONING).key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=T0,
        source_event_key=f"fixture:{label}:{uuid.uuid4().hex}",
    )
    return archive, scope, charter, command, root_branch_id, trust_root, policy


def _problem_command(
    *,
    archive: FilesystemResearchArchive,
    scope: ResearchScopeBinding,
    charter: ResearchCharterVersion,
    trust_root: ResearchAuthorizationTrustRootV1,
    policy: ResearchAuthorizationPolicyV1,
    root_branch_id: str,
    expected_version: int,
    expected_tail: str,
    label: str,
) -> tuple[ResearchProblemVersion, AuthorizedResearchCommand]:
    problem = ResearchProblemVersion(
        problem_id=f"problem:{uuid.uuid4().hex}",
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        version=1,
        title=f"store problem {label}",
        statement="Which bounded uncertainty should be reduced?",
        scope="authoritative store acceptance",
        importance_rationale="validates durable scientific custody",
        unknowns=("bounded_unknown",),
        semantic_delta="initial problem version",
        authored_by_principal_id="fixture:problem-author",
        authored_at=T0 + timedelta(seconds=1),
    )
    archive.archive_object(problem)
    proposal = ResearchCommandProposal(
        quest_id=scope.quest_id,
        scope_binding=scope,
        expected_stream_version=expected_version,
        expected_tail_event_sha256=expected_tail,
        event_type=EventType.PROBLEM_ADMITTED,
        payload=ProblemAdmittedPayload(
            problem_ref=problem.object_ref,
            branch_id=root_branch_id,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=T0 + timedelta(seconds=1),
    )
    return problem, authorize_research_proposal(
        proposal,
        idempotency_key=f"problem:{label}:{uuid.uuid4().hex}",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role_key(policy, ResearchAuthorizationRole.ORDINARY).key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=T0 + timedelta(seconds=1),
        source_event_key=f"fixture:problem:{label}:{uuid.uuid4().hex}",
    )


def _store(
    trust_root: ResearchAuthorizationTrustRootV1,
    policy: ResearchAuthorizationPolicyV1 | None = None,
    *,
    archive: FilesystemResearchArchive,
) -> ResearchKernelStore:
    return ResearchKernelStore(
        trust_root=trust_root,
        archive=archive,
        genesis_policy=policy,
    )


def _authorize(
    proposal: ResearchCommandProposal,
    *,
    trust_root: ResearchAuthorizationTrustRootV1,
    policy: ResearchAuthorizationPolicyV1,
    role: ResearchAuthorizationRole,
    label: str,
    authorized_at: datetime,
) -> AuthorizedResearchCommand:
    return authorize_research_proposal(
        proposal,
        idempotency_key=f"{label}:{uuid.uuid4().hex}",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role_key(policy, role).key_id,
        private_key=_PRIVATE_KEYS[role],
        authorized_at=authorized_at,
        source_event_key=f"source:{label}:{uuid.uuid4().hex}",
    )


def _counts(quest_id: str) -> tuple[int, int, int, int, int]:
    with session_scope() as session:
        models = (
            ResearchKernelCommandReceiptRecord,
            ResearchKernelEventRecord,
            ResearchKernelObjectRecord,
            ResearchKernelSnapshotRecord,
            ResearchKernelOutboxRecord,
        )
        return tuple(
            int(
                session.scalar(
                    select(func.count()).select_from(model).where(model.quest_id == quest_id)
                )
                or 0
            )
            for model in models
        )


def test_proposal_cannot_write_and_commit_is_atomic_idempotent_and_auditable(
    tmp_path: Path,
) -> None:
    archive, scope, charter, command, _root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="atomic",
    )
    proposal = ResearchCommandProposal(
        quest_id=command.quest_id,
        scope_binding=scope,
        expected_stream_version=0,
        expected_tail_event_sha256=None,
        event_type=command.event_type,
        payload=command.payload,
        proposed_by_principal_id="model:proposer",
        proposed_at=T0,
    )
    store = _store(trust_root, policy, archive=archive)

    with pytest.raises(UncommittedProposalError):
        store.commit(proposal)
    with session_scope() as session:
        assert session.get(ResearchQuestStreamRecord, scope.quest_id) is None

    first = store.commit(command)
    exact = store.commit(command)
    assert first.created is True
    assert exact.created is False
    assert exact.model_copy(update={"created": True}) == first
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)

    audit_store = _store(trust_root, archive=archive)
    audit = audit_store.audit(scope.quest_id, expected_scope_binding=scope)
    replayed = audit_store.replay(scope.quest_id)
    assert replayed == audit.state
    assert audit.events[0].event_sha256 == first.result_event_sha256
    assert audit.state.snapshot_sha256 == first.result_snapshot_sha256
    loaded = store.load_command_receipt_for_event(
        quest_id=scope.quest_id,
        result_event_sha256=first.result_event_sha256,
    )
    assert loaded is not None
    assert loaded.model_copy(update={"created": True}) == first
    with session_scope() as session:
        participating_audit = store.audit_in_session(
            session,
            scope.quest_id,
            expected_scope_binding=scope,
        )
        in_session = store.load_command_receipt_for_event_in_session(
            session,
            quest_id=scope.quest_id,
            result_event_sha256=first.result_event_sha256,
        )
        assert participating_audit == audit
        assert in_session == loaded

    with session_scope() as session:
        object_row = session.get(ResearchKernelObjectRecord, charter.object_sha256)
        event_row = session.get(ResearchKernelEventRecord, first.result_event_sha256)
        command_row = session.get(ResearchKernelCommandReceiptRecord, first.command_id)
        outbox_row = session.get(ResearchKernelOutboxRecord, first.outbox_id)
        assert object_row is not None and event_row is not None
        assert command_row is not None and outbox_row is not None
        assert "payload" not in ResearchKernelObjectRecord.__table__.columns
        assert object_row.storage_key.endswith(charter.object_sha256)
        assert charter.mission not in json.dumps(event_row.event_json)
        assert command_row.command_sha256 == command.command_sha256
        assert command_row.command_json == command.model_dump(mode="json", exclude_none=True)
        assert command_row.authorization_trust_root_sha256 == trust_root.trust_root_sha256
        assert command_row.authorization_policy_sha256 == policy.policy_sha256
        head = session.get(ResearchQuestStreamRecord, scope.quest_id)
        authority = session.get(ResearchQuestAuthorityRecord, scope.quest_id)
        assert head is not None
        assert authority is not None
        assert authority.authority_kind == "research_kernel_v1"
        assert head.authorization_trust_root_sha256 == trust_root.trust_root_sha256
        assert head.authorization_policy_sha256 == policy.policy_sha256
        assert head.authorization_policy_json == policy.model_dump(mode="json", exclude_none=True)
        assert outbox_row.payload_sha256 == event_row.event_sha256

    rebound = authorize_research_proposal(
        proposal,
        idempotency_key=command.idempotency_key,
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=command.authorization_key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=T0,
        source_event_key=f"changed-source:{uuid.uuid4().hex}",
    )
    with pytest.raises(ResearchIdempotencyConflict, match="different content"):
        store.commit(rebound)
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)


def test_genesis_requires_the_deployment_pinned_root_and_exact_certified_policy(
    tmp_path: Path,
) -> None:
    archive, scope, _charter, command, _root, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="trusted-policy",
    )
    with pytest.raises(ResearchAuthorizationError, match="deployment-pinned policy"):
        _store(trust_root, archive=archive).commit(command)

    attacker_root, attacker_policy = _authority(scope.quest_id, label="attacker-policy")
    attacker_command = authorize_research_proposal(
        ResearchCommandProposal(
            quest_id=command.quest_id,
            scope_binding=command.scope_binding,
            expected_stream_version=0,
            expected_tail_event_sha256=None,
            event_type=command.event_type,
            payload=command.payload,
            proposed_by_principal_id="attacker:model",
            proposed_at=T0,
        ),
        idempotency_key=command.idempotency_key,
        authorization_policy=attacker_policy,
        trust_root=attacker_root,
        authorization_key_id=_role_key(
            attacker_policy, ResearchAuthorizationRole.COMMISSIONING
        ).key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=T0,
        source_event_key=command.source_event_key,
    )
    with pytest.raises(ResearchAuthorizationError, match="another deployment trust policy"):
        _store(trust_root, policy, archive=archive).commit(attacker_command)
    with session_scope() as session:
        assert session.get(ResearchQuestStreamRecord, scope.quest_id) is None
    assert _counts(scope.quest_id) == (0, 0, 0, 0, 0)


def test_exact_retry_precedes_stale_version_and_head_requires_version_and_hash(
    tmp_path: Path,
) -> None:
    archive, scope, charter, genesis, root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="cas-head",
    )
    store = _store(trust_root, policy, archive=archive)
    first = store.commit(genesis)
    _problem, second_command = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=root_branch_id,
        expected_version=1,
        expected_tail=first.result_event_sha256,
        label="cas-head-second",
    )
    second = store.commit(second_command)

    exact_genesis = store.commit(genesis)
    assert exact_genesis.created is False
    assert exact_genesis.result_event_sha256 == first.result_event_sha256

    _stale_problem, stale = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=root_branch_id,
        expected_version=1,
        expected_tail=first.result_event_sha256,
        label="stale-version",
    )
    with pytest.raises(ResearchVersionConflict, match="stale Quest head"):
        store.commit(stale)

    _wrong_tail_problem, wrong_tail = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=root_branch_id,
        expected_version=2,
        expected_tail=_sha("not-the-current-tail"),
        label="wrong-tail",
    )
    with pytest.raises(ResearchVersionConflict, match="stale Quest head"):
        store.commit(wrong_tail)
    assert _counts(scope.quest_id) == (2, 2, 2, 2, 2)
    assert store.replay(scope.quest_id).tail_event_sha256 == (second.result_event_sha256)


def test_charter_revision_revokes_pending_action_authority_at_commit_time(
    tmp_path: Path,
) -> None:
    archive, scope, charter, genesis, branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="revoked-action",
    )
    store = _store(trust_root, policy, archive=archive)
    head = store.commit(genesis)
    problem, problem_command = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=branch_id,
        expected_version=1,
        expected_tail=head.result_event_sha256,
        label="revoked-action-problem",
    )
    head = store.commit(problem_command)

    question = ResearchQuestionVersion(
        question_id=_identity("question", "revoked-action"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.MECHANISTIC,
        statement="Which bounded action should be permitted?",
        scope="revocation acceptance",
        answer_space=("deny", "permit"),
        scientific_value="Proves authorization follows the current charter.",
        falsifiability="A stale permission must fail closed.",
        semantic_delta="initial question",
        authored_by_principal_id="fixture:question-author",
        authored_at=T0 + timedelta(seconds=2),
    )
    archive.archive_object(question)
    question_command = _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=2,
            expected_tail_event_sha256=head.result_event_sha256,
            event_type=EventType.QUESTION_ADMITTED,
            payload=QuestionAdmittedPayload(
                question_ref=question.object_ref,
                branch_id=branch_id,
            ),
            proposed_by_principal_id="model:planner",
            proposed_at=T0 + timedelta(seconds=2),
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.ORDINARY,
        label="revoked-action-question",
        authorized_at=T0 + timedelta(seconds=2),
    )
    head = store.commit(question_command)

    action = ResearchActionProposal(
        action_id=_identity("action", "revoked-action"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=head.result_event_sha256,
        kind=ActionKind.CONTINUE,
        epistemic_purpose="Exercise the currently allowed analysis authority.",
        candidate_outcomes=("advance", "stop"),
        cost_receipt_sha256=_sha("revoked-action-cost"),
        risk_receipt_sha256=_sha("revoked-action-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id="model:planner",
        proposed_at=T0 + timedelta(seconds=3),
    )
    archive.archive_object(action)
    action_command = _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=3,
            expected_tail_event_sha256=head.result_event_sha256,
            event_type=EventType.ACTION_PROPOSED,
            payload=ActionProposedPayload(action_ref=action.object_ref, branch_id=branch_id),
            proposed_by_principal_id="model:planner",
            proposed_at=T0 + timedelta(seconds=3),
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.ORDINARY,
        label="revoked-action-proposal",
        authorized_at=T0 + timedelta(seconds=3),
    )
    head = store.commit(action_command)

    revised_data = charter.model_dump(mode="python")
    revised_data.update(
        version=2,
        revision_parent_sha256=charter.object_sha256,
        allowed_action_classes=("simulation",),
        authorized_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],
        authority_receipt_sha256=_sha("revoked-action-amendment"),
        authorized_at=T0 + timedelta(seconds=4),
    )
    revised = ResearchCharterVersion.model_validate(revised_data)
    archive.archive_object(revised)
    revision_command = _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=4,
            expected_tail_event_sha256=head.result_event_sha256,
            event_type=EventType.CHARTER_REVISED,
            payload=CharterRevisedPayload(charter_ref=revised.object_ref),
            proposed_by_principal_id="human:amender",
            proposed_at=T0 + timedelta(seconds=4),
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.AMENDMENT,
        label="revoked-action-charter",
        authorized_at=T0 + timedelta(seconds=4),
    )
    head = store.commit(revision_command)

    authorize_action = _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=5,
            expected_tail_event_sha256=head.result_event_sha256,
            event_type=EventType.ACTION_AUTHORIZED,
            payload=ActionAuthorizedPayload(action_id=action.action_id, branch_id=branch_id),
            proposed_by_principal_id="model:planner",
            proposed_at=T0 + timedelta(seconds=5),
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.ORDINARY,
        label="revoked-action-authorize",
        authorized_at=T0 + timedelta(seconds=5),
    )
    with pytest.raises(ResearchAuthorizationError, match="current charter version"):
        store.commit(authorize_action)
    replayed = store.replay(scope.quest_id)
    assert replayed.stream_version == 5
    assert replayed.actions[0].lifecycle.value == "proposed"


def test_expired_charter_emergency_halt_is_global_actionless_and_replayable(
    tmp_path: Path,
) -> None:
    archive, scope, charter, genesis, root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="global-emergency-halt",
    )
    store = _store(trust_root, policy, archive=archive)
    head = store.commit(genesis)
    problem, problem_command = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=root_branch_id,
        expected_version=1,
        expected_tail=head.result_event_sha256,
        label="global-emergency-problem",
    )
    head = store.commit(problem_command)

    question = ResearchQuestionVersion(
        question_id=_identity("question", "global-emergency"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.MECHANISTIC,
        statement="Can the safety authority halt every live branch?",
        scope="global emergency halt acceptance",
        answer_space=("halted", "unsafe"),
        scientific_value="Makes the emergency authority executable, not merely signed.",
        falsifiability="Any surviving live branch falsifies the guarantee.",
        semantic_delta="initial emergency-halt question",
        authored_by_principal_id="fixture:question-author",
        authored_at=T0 + timedelta(seconds=2),
    )
    archive.archive_object(question)
    head = store.commit(
        _authorize(
            ResearchCommandProposal(
                quest_id=scope.quest_id,
                scope_binding=scope,
                expected_stream_version=2,
                expected_tail_event_sha256=head.result_event_sha256,
                event_type=EventType.QUESTION_ADMITTED,
                payload=QuestionAdmittedPayload(
                    question_ref=question.object_ref,
                    branch_id=root_branch_id,
                ),
                proposed_by_principal_id="model:planner",
                proposed_at=T0 + timedelta(seconds=2),
            ),
            trust_root=trust_root,
            policy=policy,
            role=ResearchAuthorizationRole.ORDINARY,
            label="global-emergency-question",
            authorized_at=T0 + timedelta(seconds=2),
        )
    )

    fork_action = ResearchActionProposal(
        action_id=_identity("action", "global-emergency-fork"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=head.result_event_sha256,
        kind=ActionKind.FORK,
        epistemic_purpose="Create concurrent branches before exercising the safety boundary.",
        candidate_outcomes=("branch_a", "branch_b"),
        cost_receipt_sha256=_sha("global-emergency-fork-cost"),
        risk_receipt_sha256=_sha("global-emergency-fork-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id="model:planner",
        proposed_at=T0 + timedelta(seconds=3),
    )
    archive.archive_object(fork_action)
    head = store.commit(
        _authorize(
            ResearchCommandProposal(
                quest_id=scope.quest_id,
                scope_binding=scope,
                expected_stream_version=3,
                expected_tail_event_sha256=head.result_event_sha256,
                event_type=EventType.ACTION_PROPOSED,
                payload=ActionProposedPayload(
                    action_ref=fork_action.object_ref,
                    branch_id=root_branch_id,
                ),
                proposed_by_principal_id="model:planner",
                proposed_at=T0 + timedelta(seconds=3),
            ),
            trust_root=trust_root,
            policy=policy,
            role=ResearchAuthorizationRole.ORDINARY,
            label="global-emergency-fork-action",
            authorized_at=T0 + timedelta(seconds=3),
        )
    )
    child_branch_ids = tuple(
        sorted(
            (
                _identity("rbr", "global-emergency-child-a"),
                _identity("rbr", "global-emergency-child-b"),
            )
        )
    )
    fork_decision = TransitionDecision(
        transition_id=_identity("transition", "global-emergency-fork"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        source_graph_sha256=store.replay(scope.quest_id).snapshot_sha256,
        selected_action_ref=fork_action.object_ref,
        directive=ForkDirective(
            source_branch_id=root_branch_id,
            child_branch_ids=child_branch_ids,
        ),
        budget_receipt_sha256=_sha("global-emergency-fork-budget"),
        risk_receipt_sha256=_sha("global-emergency-fork-decision-risk"),
        policy_receipt_sha256=_sha("global-emergency-fork-policy"),
        reason_codes=("parallel_discrimination",),
        rationale="Two branches make a single-branch stop observably unsafe.",
        decided_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.ORDINARY],
        decided_at=T0 + timedelta(seconds=4),
    )
    head = store.commit(
        _authorize(
            ResearchCommandProposal(
                quest_id=scope.quest_id,
                scope_binding=scope,
                expected_stream_version=4,
                expected_tail_event_sha256=head.result_event_sha256,
                event_type=EventType.FORK_COMMITTED,
                payload=ForkCommittedPayload(decision=fork_decision),
                proposed_by_principal_id="model:controller",
                proposed_at=T0 + timedelta(seconds=4),
            ),
            trust_root=trust_root,
            policy=policy,
            role=ResearchAuthorizationRole.ORDINARY,
            label="global-emergency-fork-transition",
            authorized_at=T0 + timedelta(seconds=4),
        )
    )

    with session_scope() as session:
        database_now = session.scalar(select(func.clock_timestamp()))
    assert isinstance(database_now, datetime)
    revised_data = charter.model_dump(mode="python")
    revised_data.update(
        version=2,
        revision_parent_sha256=charter.object_sha256,
        authorized_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],
        authority_receipt_sha256=_sha("global-emergency-short-charter"),
        authorized_at=database_now,
        expires_at=database_now + timedelta(seconds=3),
    )
    expiring_charter = ResearchCharterVersion.model_validate(revised_data)
    archive.archive_object(expiring_charter)
    head = store.commit(
        _authorize(
            ResearchCommandProposal(
                quest_id=scope.quest_id,
                scope_binding=scope,
                expected_stream_version=5,
                expected_tail_event_sha256=head.result_event_sha256,
                event_type=EventType.CHARTER_REVISED,
                payload=CharterRevisedPayload(charter_ref=expiring_charter.object_ref),
                proposed_by_principal_id="human:amender",
                proposed_at=database_now,
            ),
            trust_root=trust_root,
            policy=policy,
            role=ResearchAuthorizationRole.AMENDMENT,
            label="global-emergency-short-charter",
            authorized_at=database_now,
        )
    )

    wait_deadline = time.monotonic() + 10
    while True:
        with session_scope() as session:
            observed = session.scalar(select(func.clock_timestamp()))
        assert isinstance(observed, datetime)
        if observed >= expiring_charter.expires_at:
            break
        if time.monotonic() >= wait_deadline:
            pytest.fail("PostgreSQL clock did not reach the short Charter expiry")
        time.sleep(0.05)

    pre_halt = store.replay(scope.quest_id)
    assert (
        sum(
            branch.lifecycle
            in {BranchLifecycle.ADMITTED, BranchLifecycle.ACTIVE, BranchLifecycle.PAUSED}
            for branch in pre_halt.branches
        )
        >= 3
    )
    halt_marker = emergency_halt_action_ref(
        quest_id=scope.quest_id,
        charter_ref=expiring_charter.object_ref,
    )
    halt_decision = TransitionDecision(
        transition_id=_identity("transition", "global-emergency-halt"),
        quest_id=scope.quest_id,
        charter_ref=expiring_charter.object_ref,
        source_graph_sha256=pre_halt.snapshot_sha256,
        selected_action_ref=halt_marker,
        directive=StopDirective(
            branch_id=child_branch_ids[0],
            stop_reason=StopReason.EMERGENCY_STOP,
        ),
        budget_receipt_sha256=_sha("global-emergency-halt-budget"),
        risk_receipt_sha256=_sha("global-emergency-halt-risk"),
        policy_receipt_sha256=_sha("global-emergency-halt-policy"),
        reason_codes=("emergency_authority",),
        rationale="Atomically halt all live branches after ordinary authority expires.",
        decided_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.EMERGENCY],
        decided_at=observed,
    )
    halt_command = _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=6,
            expected_tail_event_sha256=head.result_event_sha256,
            event_type=EventType.STOP_COMMITTED,
            payload=StopCommittedPayload(decision=halt_decision),
            proposed_by_principal_id="safety:monitor",
            proposed_at=observed,
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.EMERGENCY,
        label="global-emergency-halt",
        authorized_at=observed,
    )
    counts_before_halt = _counts(scope.quest_id)
    halt_receipt = store.commit(halt_command)
    counts_after_halt = _counts(scope.quest_id)
    assert counts_after_halt == (
        counts_before_halt[0] + 1,
        counts_before_halt[1] + 1,
        counts_before_halt[2],
        counts_before_halt[3] + 1,
        counts_before_halt[4] + 1,
    )

    audit = store.audit(scope.quest_id, expected_scope_binding=scope)
    assert audit.state == store.replay(scope.quest_id)
    assert audit.state.terminal
    assert audit.state.terminal_event_sha256 == halt_receipt.result_event_sha256
    assert all(
        branch.lifecycle
        not in {BranchLifecycle.ADMITTED, BranchLifecycle.ACTIVE, BranchLifecycle.PAUSED}
        for branch in audit.state.branches
    )
    assert not any(action.action_ref == halt_marker for action in audit.state.actions)
    with session_scope() as session:
        assert session.get(ResearchKernelObjectRecord, halt_marker.object_sha256) is None

    with pytest.raises(ResearchStoreError, match="not a valid research transition"):
        store.commit(
            _authorize(
                ResearchCommandProposal(
                    quest_id=scope.quest_id,
                    scope_binding=scope,
                    expected_stream_version=7,
                    expected_tail_event_sha256=halt_receipt.result_event_sha256,
                    event_type=EventType.STOP_COMMITTED,
                    payload=StopCommittedPayload(
                        decision=halt_decision.model_copy(
                            update={
                                "transition_id": _identity("transition", "post-global-emergency"),
                                "source_graph_sha256": audit.state.snapshot_sha256,
                                "decided_at": observed,
                            }
                        )
                    ),
                    proposed_by_principal_id="safety:monitor",
                    proposed_at=observed,
                ),
                trust_root=trust_root,
                policy=policy,
                role=ResearchAuthorizationRole.EMERGENCY,
                label="post-global-emergency",
                authorized_at=observed,
            )
        )
    assert _counts(scope.quest_id) == counts_after_halt


def test_legacy_and_kernel_stores_cannot_claim_the_same_quest_identity(
    tmp_path: Path,
) -> None:
    from aletheia.programs import GraphCommandContext, ProgramGraphStore, QuestSpec
    from aletheia.programs.persistence import ResearchGraphNodeRecord

    assert ProgramGraphStore.AUTHORITY_SCOPE == "legacy_program_graph_only"
    assert ProgramGraphStore.NEW_RESEARCH_QUEST_MUTATIONS_ALLOWED is False

    def legacy_spec(label: str) -> QuestSpec:
        return QuestSpec(
            identity_key=f"authority-cutover-{label}-{uuid.uuid4().hex}",
            title="Legacy authority collision probe",
            direction="Prove one qst identity cannot have two scientific authorities.",
            value_boundary="No dual authority.",
            safety_boundary=("Fail closed on namespace collision",),
        )

    legacy_first = legacy_spec("legacy-first")
    ProgramGraphStore().create_quest(
        legacy_first,
        GraphCommandContext(
            idempotency_key=f"legacy-first:{uuid.uuid4().hex}",
            principal="pytest:legacy-authority",
        ),
    )
    archive, scope, _charter, genesis, _branch, trust_root, policy = _quest_fixture(
        tmp_path / "legacy-first-cas",
        label="legacy-first-kernel",
        quest_id=legacy_first.node_id,
    )
    with pytest.raises(DBAPIError, match="already claimed by legacy_program_graph"):
        _store(trust_root, policy, archive=archive).commit(genesis)
    with session_scope() as session:
        authority = session.get(ResearchQuestAuthorityRecord, legacy_first.node_id)
        assert authority is not None
        assert authority.authority_kind == "legacy_program_graph"
        assert session.get(ResearchQuestStreamRecord, scope.quest_id) is None

    kernel_first = legacy_spec("kernel-first")
    archive, scope, _charter, genesis, _branch, trust_root, policy = _quest_fixture(
        tmp_path / "kernel-first-cas",
        label="kernel-first",
        quest_id=kernel_first.node_id,
    )
    _store(trust_root, policy, archive=archive).commit(genesis)
    with pytest.raises(DBAPIError, match="already claimed by research_kernel_v1"):
        ProgramGraphStore().create_quest(
            kernel_first,
            GraphCommandContext(
                idempotency_key=f"kernel-first:{uuid.uuid4().hex}",
                principal="pytest:legacy-authority",
            ),
        )
    with session_scope() as session:
        authority = session.get(ResearchQuestAuthorityRecord, kernel_first.node_id)
        assert authority is not None
        assert authority.authority_kind == "research_kernel_v1"
        assert session.get(ResearchGraphNodeRecord, kernel_first.node_id) is None


def test_concurrent_compare_and_swap_commits_exactly_one_mutation(tmp_path: Path) -> None:
    archive, scope, charter, genesis, root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="concurrent",
    )
    store = _store(trust_root, policy, archive=archive)
    first = store.commit(genesis)
    candidates = [
        _problem_command(
            archive=archive,
            scope=scope,
            charter=charter,
            trust_root=trust_root,
            policy=policy,
            root_branch_id=root_branch_id,
            expected_version=1,
            expected_tail=first.result_event_sha256,
            label=f"concurrent-{index}",
        )[1]
        for index in range(2)
    ]
    barrier = Barrier(2)

    def invoke(command: AuthorizedResearchCommand):
        barrier.wait()
        try:
            return store.commit(command)
        except ResearchVersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, candidates))
    receipts = [item for item in outcomes if not isinstance(item, Exception)]
    conflicts = [item for item in outcomes if isinstance(item, ResearchVersionConflict)]
    assert len(receipts) == 1
    assert len(conflicts) == 1
    assert _counts(scope.quest_id) == (2, 2, 2, 2, 2)
    assert store.replay(scope.quest_id).stream_version == 2


def test_concurrent_genesis_redelivery_returns_one_create_and_one_exact_retry(
    tmp_path: Path,
) -> None:
    archive, scope, _charter, genesis, _branch, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="concurrent-genesis",
    )
    store = _store(trust_root, policy, archive=archive)
    barrier = Barrier(2)

    def invoke(_index: int):
        barrier.wait()
        return store.commit(genesis)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(invoke, range(2)))
    assert sorted(receipt.created for receipt in receipts) == [False, True]
    assert len({receipt.result_event_sha256 for receipt in receipts}) == 1
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)


@pytest.mark.parametrize("crash_point", ["after_snapshot_cas_before_db", "before_commit"])
def test_crash_before_commit_rolls_back_and_retry_commits_once(
    tmp_path: Path,
    crash_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        archive,
        scope,
        _charter,
        command,
        _root_branch_id,
        trust_root,
        policy,
    ) = _quest_fixture(
        tmp_path / "cas",
        label=f"crash-before-{crash_point}",
    )

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError("injected crash before commit")

    crashing_store = _store(trust_root, policy, archive=archive)
    monkeypatch.setattr(crashing_store, "_inject_fault", crash)
    with pytest.raises(RuntimeError, match="before commit"):
        crashing_store.commit(command)
    with session_scope() as session:
        assert session.get(ResearchQuestStreamRecord, scope.quest_id) is None
    assert _counts(scope.quest_id) == (0, 0, 0, 0, 0)
    assert len([path for path in archive.root.rglob("*") if path.is_file()]) >= 2

    recovered = _store(trust_root, policy, archive=archive).commit(command)
    assert recovered.created is True
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)


def test_crash_after_commit_retries_to_exact_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        archive,
        scope,
        _charter,
        command,
        _root_branch_id,
        trust_root,
        policy,
    ) = _quest_fixture(
        tmp_path / "cas",
        label="crash-after",
    )

    def crash(point: str) -> None:
        if point == "after_commit":
            raise RuntimeError("injected crash after commit")

    crashing_store = _store(trust_root, policy, archive=archive)
    monkeypatch.setattr(crashing_store, "_inject_fault", crash)
    with pytest.raises(RuntimeError, match="after commit"):
        crashing_store.commit(command)
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)

    recovered = _store(trust_root, archive=archive).commit(command)
    assert recovered.created is False
    assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)


def test_quest_idempotency_and_scope_are_isolated(tmp_path: Path) -> None:
    shared_idempotency = f"same-key:{uuid.uuid4().hex}"
    archive = FilesystemResearchArchive(tmp_path / "cas")
    fixtures = [
        _quest_fixture(
            tmp_path / "cas",
            label=f"isolated-{index}",
            idempotency_key=shared_idempotency,
        )
        for index in range(2)
    ]
    stores = [_store(fixture[5], fixture[6], archive=archive) for fixture in fixtures]
    receipts = [store.commit(fixture[3]) for store, fixture in zip(stores, fixtures, strict=True)]
    assert receipts[0].command_id != receipts[1].command_id
    assert all(receipt.created for receipt in receipts)

    first_scope = fixtures[0][1]
    second_scope = fixtures[1][1]
    with pytest.raises(ResearchStoreError, match="requested scope"):
        stores[0].audit(
            first_scope.quest_id,
            expected_scope_binding=second_scope,
        )
    for fixture, store in zip(fixtures, stores, strict=True):
        scope = fixture[1]
        audit = store.audit(scope.quest_id, expected_scope_binding=scope)
        assert audit.scope_binding == scope
        assert audit.state.quest_id == scope.quest_id
        assert _counts(scope.quest_id) == (1, 1, 1, 1, 1)


def test_replay_fails_closed_when_snapshot_cas_bytes_are_tampered(tmp_path: Path) -> None:
    (
        archive,
        scope,
        _charter,
        command,
        _root_branch_id,
        trust_root,
        policy,
    ) = _quest_fixture(
        tmp_path / "cas",
        label="snapshot-tamper",
    )
    receipt = _store(trust_root, policy, archive=archive).commit(command)
    with session_scope() as session:
        snapshot = session.get(ResearchKernelSnapshotRecord, receipt.result_snapshot_sha256)
        assert snapshot is not None
        path = archive.root / snapshot.storage_key
    payload = path.read_bytes()
    os.chmod(path, 0o600)
    path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])

    with pytest.raises(ResearchStoreInvariantError, match="snapshot"):
        _store(trust_root, archive=archive).audit(scope.quest_id)


def test_replay_fails_closed_when_quest_authority_registry_is_tampered(tmp_path: Path) -> None:
    archive, scope, _charter, command, _root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="authority-registry-tamper",
    )
    _store(trust_root, policy, archive=archive).commit(command)

    # Model a privileged/out-of-band corruption that bypasses the ordinary immutable-row guard.
    # Audit must still derive no trust from the mere existence of a stream head.
    with engine().begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE research_quest_authorities "
            "DISABLE TRIGGER trg_research_quest_authority_immutable"
        )
        connection.execute(
            ResearchQuestAuthorityRecord.__table__.update()
            .where(ResearchQuestAuthorityRecord.quest_id == scope.quest_id)
            .values(authority_kind="legacy_program_graph")
        )
        connection.exec_driver_sql(
            "ALTER TABLE research_quest_authorities "
            "ENABLE TRIGGER trg_research_quest_authority_immutable"
        )

    with pytest.raises(ResearchStoreInvariantError, match="authority registry"):
        _store(trust_root, archive=archive).audit(scope.quest_id)
