"""Bound-audit acceptance tests: ``as_of`` must reproduce the commit-time view.

Receipt replay pins its observation time to the signed commitment (#153).  The audit
it consumes must therefore stop at the prefix of events that commitment had already
seen; without the bound, an admission's own later ``observation_incorporated`` event
reads as a future commitment and every exact retry fails closed.  These tests hold
the store half of that contract: the prefix is cut exactly at ``committed_at``,
every per-event invariant still runs on it, and the catalog/snapshot/head totals
stay checked against the complete stream.

The tail totals have no post-hoc tamper tests on a migrated rig: events and
snapshots are append-only by trigger, the Quest head must advance exactly one
version per write, and the object catalog rejects a row without its exact
admitting event, so the database refuses every inconsistent state before an
audit could observe one.  The bounded tail checks read the same complete tables
the unbounded ones do; the unbounded suite exercises those invariants.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aletheia.db import create_all
from aletheia.research_kernel.commands import ResearchCommandProposal
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    EventType,
    QuestionAdmittedPayload,
    QuestionKind,
    ResearchActionProposal,
    ResearchQuestionVersion,
)

_KERNEL_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_KERNEL_TESTS))
from test_store import (  # noqa: E402
    T0,
    _authorize,
    _identity,
    _problem_command,
    _quest_fixture,
    _sha,
    _store,
)

@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _question_object(
    *,
    archive,
    scope,
    charter,
    problem,
    label: str,
    authored_at_offset: timedelta,
) -> ResearchQuestionVersion:
    question = ResearchQuestionVersion(
        question_id=_identity("question", label),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.MECHANISTIC,
        statement="Which bounded event ordering must an audit reproduce?",
        scope="bound-audit acceptance",
        answer_space=("bounded", "unbounded"),
        scientific_value="Proves replay reproduces the commit-time event set.",
        falsifiability="A future commitment must stay invisible to a bounded audit.",
        semantic_delta="initial question",
        authored_by_principal_id="fixture:question-author",
        authored_at=T0 + authored_at_offset,
    )
    archive.archive_object(question)
    return question


def _question_command(
    *,
    scope,
    trust_root,
    policy,
    branch_id: str,
    question: ResearchQuestionVersion,
    expected_version: int,
    expected_tail: str,
    label: str,
    proposed_at_offset: timedelta,
):
    return _authorize(
        ResearchCommandProposal(
            quest_id=scope.quest_id,
            scope_binding=scope,
            expected_stream_version=expected_version,
            expected_tail_event_sha256=expected_tail,
            event_type=EventType.QUESTION_ADMITTED,
            payload=QuestionAdmittedPayload(
                question_ref=question.object_ref,
                branch_id=branch_id,
            ),
            proposed_by_principal_id="model:planner",
            proposed_at=T0 + proposed_at_offset,
        ),
        trust_root=trust_root,
        policy=policy,
        role=ResearchAuthorizationRole.ORDINARY,
        label=label,
        authorized_at=T0 + proposed_at_offset,
    )


def _committed_stream(tmp_path: Path):
    """Genesis -> problem -> question -> action proposed -> authorized -> later event.

    The sixth event stands in for the incorporation append an admission commit makes
    moments after its own commitment: same stream, strictly later ``committed_at``.
    """

    archive, scope, charter, genesis, branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label="audit-as-of",
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
        label="audit-as-of-problem",
    )
    head = store.commit(problem_command)
    question = _question_object(
        archive=archive,
        scope=scope,
        charter=charter,
        problem=problem,
        label="audit-as-of",
        authored_at_offset=timedelta(seconds=2),
    )
    head = store.commit(
        _question_command(
            scope=scope,
            trust_root=trust_root,
            policy=policy,
            branch_id=branch_id,
            question=question,
            expected_version=2,
            expected_tail=head.result_event_sha256,
            label="audit-as-of-question",
            proposed_at_offset=timedelta(seconds=2),
        )
    )

    action = ResearchActionProposal(
        action_id=_identity("action", "audit-as-of"),
        quest_id=scope.quest_id,
        charter_ref=charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=head.result_event_sha256,
        kind=ActionKind.CONTINUE,
        epistemic_purpose="Exercise the bound-audit seam with a later kernel event.",
        candidate_outcomes=("advance", "stop"),
        cost_receipt_sha256=_sha("audit-as-of-cost"),
        risk_receipt_sha256=_sha("audit-as-of-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id="model:planner",
        proposed_at=T0 + timedelta(seconds=3),
    )
    archive.archive_object(action)
    head = store.commit(
        _authorize(
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
            label="audit-as-of-proposal",
            authorized_at=T0 + timedelta(seconds=3),
        )
    )
    authorized_receipt = store.commit(
        _authorize(
            ResearchCommandProposal(
                quest_id=scope.quest_id,
                scope_binding=scope,
                expected_stream_version=4,
                expected_tail_event_sha256=head.result_event_sha256,
                event_type=EventType.ACTION_AUTHORIZED,
                payload=ActionAuthorizedPayload(action_id=action.action_id, branch_id=branch_id),
                proposed_by_principal_id="model:planner",
                proposed_at=T0 + timedelta(seconds=4),
            ),
            trust_root=trust_root,
            policy=policy,
            role=ResearchAuthorizationRole.ORDINARY,
            label="audit-as-of-authorize",
            authorized_at=T0 + timedelta(seconds=4),
        )
    )

    later_question = _question_object(
        archive=archive,
        scope=scope,
        charter=charter,
        problem=problem,
        label="audit-as-of-later",
        authored_at_offset=timedelta(seconds=6),
    )
    store.commit(
        _question_command(
            scope=scope,
            trust_root=trust_root,
            policy=policy,
            branch_id=branch_id,
            question=later_question,
            expected_version=5,
            expected_tail=authorized_receipt.result_event_sha256,
            label="audit-as-of-later-question",
            proposed_at_offset=timedelta(seconds=6),
        )
    )
    return store, scope


def test_audit_as_of_returns_the_exact_commit_time_prefix(tmp_path: Path) -> None:
    store, scope = _committed_stream(tmp_path)

    full = store.audit(scope.quest_id)
    assert len(full.events) == 6
    authorized = full.events[4]
    assert authorized.event_type is EventType.ACTION_AUTHORIZED
    # The later event must be strictly younger than the bound for the prefix cut
    # to be observable; the committed_at values come from the database clock.
    assert full.events[5].committed_at > authorized.committed_at

    bounded = store.audit(scope.quest_id, as_of=authorized.committed_at)

    assert bounded.events == full.events[:5]
    assert bounded.verified_snapshot_sha256s == full.verified_snapshot_sha256s[:5]
    assert bounded.state.stream_version == 5
    assert bounded.state.tail_event_sha256 == authorized.event_sha256
    actions = tuple(
        action for action in bounded.state.actions
        if action.proposed_event_sha256 == full.events[3].event_sha256
    )
    assert len(actions) == 1
    assert actions[0].lifecycle is ActionLifecycle.AUTHORIZED

    # The unbounded historical path is byte-for-byte what every other consumer sees.
    assert store.audit(scope.quest_id) == full


def test_audit_as_of_at_or_past_the_head_degenerates_to_the_full_audit(
    tmp_path: Path,
) -> None:
    store, scope = _committed_stream(tmp_path)
    full = store.audit(scope.quest_id)
    last_commitment = full.events[-1].committed_at

    assert store.audit(scope.quest_id, as_of=last_commitment) == full
    assert (
        store.audit(scope.quest_id, as_of=last_commitment + timedelta(hours=1))
        == full
    )


def test_audit_as_of_before_every_event_returns_the_empty_prefix(
    tmp_path: Path,
) -> None:
    store, scope = _committed_stream(tmp_path)
    full = store.audit(scope.quest_id)

    early = store.audit(
        scope.quest_id,
        as_of=full.events[0].committed_at - timedelta(microseconds=1),
    )

    assert early.events == ()
    assert early.verified_snapshot_sha256s == ()
    assert early.state.stream_version == 0


def test_audit_as_of_requires_a_timezone_aware_bound(tmp_path: Path) -> None:
    store, scope = _committed_stream(tmp_path)
    full = store.audit(scope.quest_id)
    aware = full.events[4].committed_at
    naive = datetime(aware.year, aware.month, aware.day, tzinfo=None)

    with pytest.raises(ValueError, match="audit bound must be timezone-aware"):
        store.audit(scope.quest_id, as_of=naive)

