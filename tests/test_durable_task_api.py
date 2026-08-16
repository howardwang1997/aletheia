"""F11-S1 API is a task control plane; workers remain separate processes."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aletheia.api.deps import require_access
from aletheia.api.main import app
from aletheia.db import create_all
from aletheia.jobs import RetryPolicy, TaskSpec


@pytest.fixture(autouse=True)
def _owner_access():
    app.dependency_overrides[require_access] = lambda: {"id": "test", "role": "owner"}
    yield
    app.dependency_overrides.clear()


def test_task_api_enqueue_read_attempts_and_recover_route():
    create_all()
    identity = uuid.uuid4().hex
    spec = TaskSpec(
        task_id=f"task-api-{identity}",
        task_type=f"test.api-{identity}",
        inputs={"value": 1},
        owner="api-test",
        idempotency_key=f"api:{identity}",
        concurrency_key=f"api-lock:{identity}",
        retry_policy=RetryPolicy(
            lease_seconds=10,
            heartbeat_interval_seconds=2,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
    )
    with TestClient(app) as client:
        created = client.post("/tasks", json=spec.model_dump(mode="json"))
        assert created.status_code == 200
        assert created.json()["created"] is True

        replay = client.post("/tasks", json=spec.model_dump(mode="json"))
        assert replay.status_code == 200
        assert replay.json()["created"] is False

        fetched = client.get(f"/tasks/{spec.task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["request_sha256"] == spec.request_sha256
        assert client.get(f"/tasks/{spec.task_id}/attempts").json() == []

        changed = spec.model_dump(mode="json")
        changed["inputs"] = {"value": 2}
        conflict = client.post("/tasks", json=changed)
        assert conflict.status_code == 409

        # This exact path must not be swallowed by the dynamic /tasks/{task_id} route.
        recovery = client.post("/tasks/operations/recover-expired?limit=10")
        assert recovery.status_code == 200
        assert "recovered_task_ids" in recovery.json()
