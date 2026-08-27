"""F11-S1 durable queue acceptance: delivery, restart recovery, and exact replay."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, session_scope
from aletheia.events.bus import make_event
from aletheia.events.store import (
    EventIdentityConflict,
    latest_event_id,
    list_events_after,
    persist_event,
)
from aletheia.jobs import (
    DurableTaskQueue,
    IdempotencyConflict,
    InvalidTaskTransition,
    LeaseMismatch,
    RetryPolicy,
    TaskExecutionResult,
    TaskConcurrencyConflict,
    TaskLease,
    TaskNotFound,
    TaskSpec,
    TaskStatus,
    TerminalCategory,
)
from aletheia.jobs.persistence import DurableTaskAttemptRecord, DurableTaskRecord
from aletheia.jobs.worker import DurableWorker, InfrastructureTaskFailure
from aletheia.memory.ledger import Event

T0 = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)


def _identity(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex}"


def _policy(*, max_attempts: int = 3, lease_seconds: int = 10) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=max(1, lease_seconds // 4),
        initial_backoff_seconds=0,
        max_backoff_seconds=0,
    )


def _spec(
    label: str,
    *,
    task_type: str | None = None,
    dependencies: tuple[str, ...] = (),
    policy: RetryPolicy | None = None,
    inputs: dict | None = None,
) -> TaskSpec:
    identity = _identity(label)
    return TaskSpec(
        task_id=f"task-{identity}",
        task_type=task_type or f"test.{identity}",
        inputs=inputs or {"label": label},
        dependency_ids=dependencies,
        owner="pytest",
        idempotency_key=f"idem:{identity}",
        retry_policy=policy or _policy(),
    )


@pytest.fixture
def queue() -> DurableTaskQueue:
    create_all()
    return DurableTaskQueue(principal="pytest:durable_queue")


def _claim(queue: DurableTaskQueue, spec: TaskSpec, *, now: datetime = T0):
    lease = queue.claim(
        worker_id="pytest-worker",
        worker_manifest_sha256="a" * 64,
        task_types=[spec.task_type],
        now=now,
    )
    assert lease is not None
    return lease


def _result(label: str = "ok") -> TaskExecutionResult:
    return TaskExecutionResult(
        result_artifact_id=f"artifact:{label}",
        result={"label": label, "valid": True},
    )


def test_enqueue_is_content_bound_and_event_is_atomic(queue, monkeypatch):
    spec = _spec("enqueue", inputs={"x": 1})
    cursor = latest_event_id()
    first = queue.enqueue(spec, now=T0)
    replay = queue.enqueue(spec, now=T0 + timedelta(hours=1))
    assert first.created is True
    assert replay.created is False
    assert replay.task == first.task

    rebound = TaskSpec(
        **{
            **spec.model_dump(),
            "inputs": {"x": 2},
        }
    )
    with pytest.raises(IdempotencyConflict, match="different content"):
        queue.enqueue(rebound, now=T0)

    task_events = [
        event
        for event in list_events_after(cursor, limit=100)
        if (event.get("payload") or {}).get("task_id") == spec.task_id
    ]
    assert len(task_events) == 1
    assert task_events[0]["event_key"] == f"durable-task:{spec.task_id}:1"
    assert task_events[0]["event_sha256"]

    rollback_spec = _spec("atomic-rollback")

    def reject_event(*_args, **_kwargs):
        raise RuntimeError("event sink unavailable")

    monkeypatch.setattr("aletheia.jobs.queue.persist_event", reject_event)
    with pytest.raises(RuntimeError, match="event sink unavailable"):
        queue.enqueue(rollback_spec, now=T0)
    with pytest.raises(TaskNotFound, match="not found"):
        queue.get(rollback_spec.task_id)


def test_enqueue_in_caller_session_commits_and_rolls_back_atomically(queue):
    committed = _spec("caller-session-commit")
    with session_scope() as session:
        receipt = queue.enqueue_in_session(session, committed, now=T0)
        assert receipt.created is True
        assert receipt.task.task_id == committed.task_id
    assert queue.get(committed.task_id).task_id == committed.task_id

    rolled_back = _spec("caller-session-rollback")
    with pytest.raises(RuntimeError, match="force caller rollback"):
        with session_scope() as session:
            receipt = queue.enqueue_in_session(session, rolled_back, now=T0)
            assert receipt.created is True
            raise RuntimeError("force caller rollback")
    with pytest.raises(TaskNotFound, match="durable task not found"):
        queue.get(rolled_back.task_id)


def test_get_in_caller_session_can_lock_exact_task(queue):
    spec = _spec("caller-session-get")
    created = queue.enqueue(spec, now=T0).task
    with session_scope() as session:
        assert queue.get_in_session(session, spec.task_id, lock_for_update=True) == created
    with pytest.raises(TypeError, match="SQLAlchemy Session"):
        queue.get_in_session(object(), spec.task_id)


def test_lease_token_is_hashed_heartbeat_extends_and_completion_replays(queue):
    spec = _spec("lease")
    queue.enqueue(spec, now=T0)
    lease = _claim(queue, spec)
    assert lease.task.status == TaskStatus.LEASED

    with session_scope() as session:
        task_row = session.get(DurableTaskRecord, spec.task_id)
        attempt_row = session.get(DurableTaskAttemptRecord, lease.attempt_id)
        assert task_row is not None and attempt_row is not None
        assert task_row.lease_token_sha256 == attempt_row.lease_token_sha256
        assert task_row.lease_token_sha256 != lease.lease_token
        assert lease.lease_token not in json.dumps(task_row.__dict__, default=str)
        assert lease.lease_token not in json.dumps(attempt_row.__dict__, default=str)

    bad_lease = lease.model_copy(update={"lease_token": "wrong-token-" * 4})
    with pytest.raises(LeaseMismatch):
        queue.heartbeat(bad_lease, now=T0 + timedelta(seconds=1))

    heartbeat = queue.heartbeat(lease, now=T0 + timedelta(seconds=5))
    assert heartbeat.lease_expires_at == T0 + timedelta(seconds=15)
    result = TaskExecutionResult(
        result_artifact_id="artifact:complete",
        result={"ok": True},
        partial_artifact_ids=("artifact:partial",),
    )
    outcome = queue.complete(heartbeat, result, now=T0 + timedelta(seconds=6))
    assert outcome.task.status == TaskStatus.SUCCEEDED
    assert outcome.task.result_artifact_id == "artifact:complete"
    assert outcome.attempt.partial_artifact_ids == ("artifact:partial",)
    assert outcome.replayed is False

    replay = queue.complete(heartbeat, result, now=T0 + timedelta(days=1))
    assert replay.replayed is True
    with pytest.raises(InvalidTaskTransition, match="different output"):
        queue.complete(heartbeat, _result("conflict"), now=T0 + timedelta(days=1))


def test_infrastructure_retry_and_stale_callback_do_not_mutate_new_attempt(queue):
    spec = _spec("retry", policy=_policy(max_attempts=3))
    queue.enqueue(spec, now=T0)
    first = _claim(queue, spec)
    detail = "b" * 64
    failure = queue.fail(
        first,
        category=TerminalCategory.INFRASTRUCTURE,
        detail_sha256=detail,
        partial_artifact_ids=("artifact:unvalidated",),
        now=T0 + timedelta(seconds=1),
    )
    assert failure.task.status == TaskStatus.RETRY_WAIT
    assert failure.task.result_artifact_id is None
    assert failure.attempt.partial_artifact_ids == ("artifact:unvalidated",)

    second = _claim(queue, spec, now=T0 + timedelta(seconds=1))
    assert second.attempt_number == 2
    stale_replay = queue.fail(
        first,
        category=TerminalCategory.INFRASTRUCTURE,
        detail_sha256=detail,
        partial_artifact_ids=("artifact:unvalidated",),
        now=T0 + timedelta(seconds=2),
    )
    assert stale_replay.replayed is True
    assert stale_replay.task.active_attempt_id == second.attempt_id
    with pytest.raises(InvalidTaskTransition, match="different content"):
        queue.fail(
            first,
            category=TerminalCategory.INFRASTRUCTURE,
            detail_sha256=detail,
            partial_artifact_ids=("artifact:unvalidated",),
            retry=False,
            now=T0 + timedelta(seconds=2),
        )

    terminal = queue.fail(
        second,
        category=TerminalCategory.SCIENTIFIC,
        detail_sha256="c" * 64,
        retry=False,
        now=T0 + timedelta(seconds=2),
    )
    assert terminal.task.status == TaskStatus.FAILED
    assert terminal.task.terminal_category == TerminalCategory.SCIENTIFIC


def test_dependencies_release_on_success_and_fail_closed_on_parent_failure(queue):
    shared_type = f"test.dependency-{uuid.uuid4().hex}"
    parent = _spec("parent", task_type=shared_type)
    child = _spec("child", task_type=shared_type, dependencies=(parent.task_id,))
    queue.enqueue(parent, now=T0)
    assert queue.enqueue(child, now=T0).task.status == TaskStatus.BLOCKED
    parent_lease = _claim(queue, parent)
    queue.complete(parent_lease, _result("parent"), now=T0 + timedelta(seconds=1))
    assert queue.get(child.task_id).status == TaskStatus.QUEUED
    child_lease = _claim(queue, child, now=T0 + timedelta(seconds=1))
    assert child_lease.task.task_id == child.task_id
    queue.complete(child_lease, _result("child"), now=T0 + timedelta(seconds=2))

    failed_parent = _spec("failed-parent", task_type=shared_type)
    failed_child = _spec(
        "failed-child",
        task_type=shared_type,
        dependencies=(failed_parent.task_id,),
    )
    queue.enqueue(failed_parent, now=T0)
    queue.enqueue(failed_child, now=T0)
    failed_lease = queue.claim(
        worker_id="pytest-worker",
        worker_manifest_sha256="a" * 64,
        task_types=[shared_type],
        now=T0,
    )
    assert failed_lease is not None and failed_lease.task.task_id == failed_parent.task_id
    queue.fail(
        failed_lease,
        category=TerminalCategory.INVALID_OUTPUT,
        detail_sha256="d" * 64,
        retry=False,
        now=T0 + timedelta(seconds=1),
    )
    blocked = queue.get(failed_child.task_id)
    assert blocked.status == TaskStatus.FAILED
    assert blocked.terminal_category == TerminalCategory.DEPENDENCY_FAILED


def test_skip_locked_allows_only_one_concurrent_owner(queue):
    spec = _spec("concurrent")
    queue.enqueue(spec, now=T0)
    barrier = Barrier(2)

    def race(worker: str):
        barrier.wait()
        return DurableTaskQueue(principal=worker).claim(
            worker_id=worker,
            worker_manifest_sha256="e" * 64,
            task_types=[spec.task_type],
            now=T0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(race, ("worker-a", "worker-b")))
    leases = [result for result in results if result is not None]
    assert len(leases) == 1
    assert queue.get(spec.task_id).lease_owner in {"worker-a", "worker-b"}


def test_concurrency_key_closes_double_enqueue_race_and_reopens_after_terminal(queue):
    task_type = f"test.mutex-{uuid.uuid4().hex}"
    concurrency_key = f"mutex:{uuid.uuid4().hex}"
    first = _spec("mutex-a", task_type=task_type)
    second = _spec("mutex-b", task_type=task_type)
    first = TaskSpec(**{**first.model_dump(), "concurrency_key": concurrency_key})
    second = TaskSpec(**{**second.model_dump(), "concurrency_key": concurrency_key})
    barrier = Barrier(2)

    def race(spec):
        barrier.wait()
        try:
            return DurableTaskQueue(principal=spec.task_id).enqueue(spec, now=T0)
        except TaskConcurrencyConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(race, (first, second)))
    receipts = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, TaskConcurrencyConflict)]
    assert len(receipts) == 1 and receipts[0].created is True
    assert len(conflicts) == 1
    assert conflicts[0].existing_task_id == receipts[0].task.task_id

    winner = first if receipts[0].task.task_id == first.task_id else second
    loser = second if winner is first else first
    lease = _claim(queue, winner)
    queue.complete(lease, _result("mutex-winner"), now=T0 + timedelta(seconds=1))
    assert queue.enqueue(loser, now=T0 + timedelta(seconds=1)).created is True


def test_real_worker_process_kill_is_recovered_after_restart(queue):
    spec = _spec("process-kill", policy=_policy(max_attempts=2, lease_seconds=2))
    queue.enqueue(spec)
    script = "\n".join(
        (
            "import os",
            "import time",
            "from aletheia.jobs import DurableTaskQueue",
            "q=DurableTaskQueue(principal='killed-child')",
            "lease=None",
            "for _ in range(100):",
            (
                "    lease=q.claim(worker_id='killed-worker',"
                "worker_manifest_sha256='f'*64,"
                f"task_types=['{spec.task_type}'])"
            ),
            "    if lease is not None:",
            "        break",
            "    time.sleep(0.01)",
            "if lease is None:",
            "    raise RuntimeError('exact queued task remained unavailable')",
            "print(lease.model_dump_json(), flush=True)",
            "os._exit(23)",
        )
    )
    child = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(__import__("pathlib").Path(__file__).parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 23, child.stderr
    killed_lease = TaskLease.model_validate_json(child.stdout.strip())

    restarted = DurableTaskQueue(principal="restarted-worker")
    recovered_at = killed_lease.lease_expires_at + timedelta(microseconds=1)
    receipt = restarted.recover_expired(now=recovered_at)
    assert spec.task_id in receipt.recovered_task_ids
    attempts = restarted.attempts(spec.task_id)
    assert attempts[0].terminal_category == TerminalCategory.LEASE_EXPIRED

    replacement = restarted.claim(
        worker_id="replacement-worker",
        worker_manifest_sha256="1" * 64,
        task_types=[spec.task_type],
        now=recovered_at,
    )
    assert replacement is not None and replacement.attempt_number == 2
    with pytest.raises(InvalidTaskTransition):
        restarted.complete(killed_lease, _result("late"), now=recovered_at)
    assert restarted.complete(replacement, _result("recovered"), now=recovered_at).task.status == (
        TaskStatus.SUCCEEDED
    )


def test_expired_final_attempt_is_infrastructure_exhaustion_not_scientific(queue):
    spec = _spec("exhausted", policy=_policy(max_attempts=1))
    queue.enqueue(spec, now=T0)
    lease = _claim(queue, spec)
    receipt = queue.recover_expired(now=lease.lease_expires_at + timedelta(microseconds=1))
    assert receipt.terminalized_task_ids == (spec.task_id,)
    task = queue.get(spec.task_id)
    assert task.status == TaskStatus.FAILED
    assert task.terminal_category == TerminalCategory.INFRASTRUCTURE_EXHAUSTED
    assert queue.attempts(spec.task_id)[0].terminal_category == TerminalCategory.LEASE_EXPIRED


def test_durable_event_key_replays_and_rebinding_fails(queue):
    key = _identity("event-key")
    cursor = latest_event_id()
    event = make_event("test_durable_event", payload={"value": 1})
    first = persist_event(event, event_key=key)
    assert persist_event(event, event_key=key) == first
    with pytest.raises(EventIdentityConflict, match="different content"):
        persist_event(make_event("test_durable_event", payload={"value": 2}), event_key=key)
    page = list_events_after(cursor, limit=100)
    assert [item["id"] for item in page if item["event_key"] == key] == [first]
    with pytest.raises(DBAPIError, match="keyed durable events are immutable"):
        with session_scope() as session:
            session.execute(
                update(Event).where(Event.id == first).values(payload={"value": "tampered"})
            )


def test_independent_worker_executes_and_retries_handler(queue):
    spec = _spec("worker-runtime", policy=_policy(max_attempts=2))
    queue.enqueue(spec)
    calls = 0

    async def handler(task):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InfrastructureTaskFailure("transient executor outage")
        return TaskExecutionResult(
            result_artifact_id=f"artifact:{task.task_id}",
            result={"attempt": calls},
        )

    worker = DurableWorker(
        worker_id="runtime-worker",
        worker_manifest_sha256="2" * 64,
        handlers={spec.task_type: handler},
        queue=queue,
    )
    first = asyncio.run(worker.run_once())
    assert first is not None and first.task.status == TaskStatus.RETRY_WAIT
    second = asyncio.run(worker.run_once())
    assert second is not None and second.task.status == TaskStatus.SUCCEEDED
    assert second.task.result == {"attempt": 2}
