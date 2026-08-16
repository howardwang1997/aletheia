"""Phase 1 step 6: the launch endpoint enforces the data-readiness gate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aletheia.api.deps import require_access
from aletheia.api.main import app
from aletheia.data.registry import register_dataset
from aletheia.db import create_all
from aletheia.memory.service import create_run, finalize_plan


@pytest.fixture(autouse=True)
def _bypass_auth():
    """These tests exercise the launch gate, not auth — stand in an owner.
    The gated routers depend on require_access, so override that."""
    app.dependency_overrides[require_access] = lambda: {"id": "test", "role": "owner"}
    yield
    app.dependency_overrides.clear()


def test_launch_blocked_until_data_ready():
    create_all()
    with TestClient(app) as client:
        run_id = create_run("launch-gate test", domain="materials", status="scoping")
        finalize_plan(run_id, {"objective": "bandgap", "domain": "materials"})

        # a needed (unsatisfied) dataset blocks launch
        asset_id = register_dataset(
            run_id, "benchmark", ref="matbench_expt_gap", status="needed", requested_by="agent"
        )
        r = client.post(f"/runs/{run_id}/launch", json={"dry_run": True})
        assert r.status_code == 409
        body = r.json()["detail"]
        assert body["error"] == "data not ready"
        assert any(p["id"] == asset_id for p in body["pending"])

        # satisfy it -> launch is allowed
        ok = client.post(f"/runs/{run_id}/datasets/{asset_id}/ready")
        assert ok.status_code == 200
        operation_id = f"launch-gate-{run_id}"
        r2 = client.post(
            f"/runs/{run_id}/launch",
            json={"dry_run": True, "operation_id": operation_id},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "queued"
        assert r2.json()["task_id"].startswith(f"driver-{run_id}-")
        replay = client.post(
            f"/runs/{run_id}/launch",
            json={"dry_run": True, "operation_id": operation_id},
        )
        assert replay.status_code == 200
        assert replay.json()["task_id"] == r2.json()["task_id"]
        duplicate_click = client.post(
            f"/runs/{run_id}/launch",
            json={"dry_run": True, "operation_id": f"another-{operation_id}"},
        )
        assert duplicate_click.status_code == 200
        assert duplicate_click.json()["task_id"] == r2.json()["task_id"]
        conflicting_mode = client.post(
            f"/runs/{run_id}/launch",
            json={"dry_run": False, "operation_id": f"real-{operation_id}"},
        )
        assert conflicting_mode.status_code == 409


def test_launch_unknown_run_404():
    create_all()
    with TestClient(app) as client:
        r = client.post("/runs/does-not-exist/launch", json={"dry_run": True})
        assert r.status_code == 404
