from __future__ import annotations

from datetime import datetime, timezone

from aletheia.durable_tasks.contracts import RetryPolicy, TaskSpec
from aletheia.jobs.contracts import RetryPolicy as LegacyRetryPolicy
from aletheia.jobs.contracts import TaskSpec as LegacyTaskSpec


def test_authority_neutral_task_contract_preserves_legacy_identity_and_hashes() -> None:
    """Moving the schema out of ``jobs`` must not rebound persisted queue identities."""

    assert LegacyRetryPolicy is RetryPolicy
    assert LegacyTaskSpec is TaskSpec
    task = TaskSpec(
        task_id="task_hash_golden",
        task_type="research.controller.v1",
        inputs={"z": None, "a": {"y": 2, "x": 1}},
        owner="research-controller:test",
        run_id=None,
        idempotency_key="controller:hash-golden",
        concurrency_key="quest:qst_0123456789abcdef0123456789abcdef",
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        priority=7,
        available_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    assert task.inputs_sha256 == "a9fdcd2ed3b1c70bdf32595fe6ad510857975eda307bf160f5c63b066732ea47"
    assert task.request_sha256 == "d37d4e3a6050f1324332f95b8749b7447611245faca82f522d8901def47ddaca"
