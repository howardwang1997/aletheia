from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from aletheia.api.deps import require_access
from aletheia.api.main import app
from aletheia.db import create_all
from aletheia.programs import QuestSpec


@pytest.fixture(autouse=True)
def _schema_and_auth():
    create_all()
    app.dependency_overrides[require_access] = lambda: {"id": "graph-owner", "role": "owner"}
    yield
    app.dependency_overrides.clear()


def _body(seed: str) -> dict:
    spec = QuestSpec(
        identity_key=f"api-quest-{seed}",
        title="API Quest",
        direction="Test the controller boundary.",
        value_boundary="Ledger truth only.",
        safety_boundary=("No hidden mutation",),
    )
    return {
        "idempotency_key": f"api:{seed}:quest",
        "spec": spec.model_dump(mode="json"),
    }


def test_api_is_controller_over_transactional_store_and_rebuild_view() -> None:
    seed = uuid.uuid4().hex
    body = _body(seed)
    with TestClient(app) as client:
        created = client.post("/research-graph/quests", json=body)
        assert created.status_code == 200
        assert created.json()["command"]["created"] is True
        quest_id = created.json()["object_id"]

        replay = client.post("/research-graph/quests", json=body)
        assert replay.status_code == 200
        assert replay.json()["command"]["created"] is False

        snapshot = client.get(f"/research-graph/quests/{quest_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["quest_id"] == quest_id
        assert snapshot.json()["nodes"][0]["spec"]["title"] == "API Quest"
        assert len(snapshot.json()["graph_sha256"]) == 64


def test_viewer_can_rebuild_but_cannot_mutate() -> None:
    async def viewer_gate(request: Request):
        if request.method != "GET":
            raise HTTPException(status_code=403, detail="read-only role")
        return {"id": "graph-viewer", "role": "viewer"}

    app.dependency_overrides[require_access] = viewer_gate
    with TestClient(app) as client:
        assert client.get("/research-graph/quests").status_code == 200
        denied = client.post("/research-graph/quests", json=_body(uuid.uuid4().hex))
        assert denied.status_code == 403
