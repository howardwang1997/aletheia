from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

import aletheia.api.programs as programs_api
from aletheia.api.deps import require_access
from aletheia.api.main import app
from aletheia.db import create_all
from aletheia.programs import (
    GraphCommandContext,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    ProgramGraphStore,
    QuestSpec,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
)
from aletheia.reproducibility.manifest import content_sha256


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
        assert client.post("/research-graph/quests", json=body).status_code == 404
        created = client.post("/legacy/research-graph/quests", json=body)
        assert created.status_code == 200
        assert created.json()["command"]["created"] is True
        quest_id = created.json()["object_id"]

        replay = client.post("/legacy/research-graph/quests", json=body)
        assert replay.status_code == 200
        assert replay.json()["command"]["created"] is False

        snapshot = client.get(f"/legacy/research-graph/quests/{quest_id}")
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
        assert client.get("/legacy/research-graph/quests").status_code == 200
        denied = client.post("/legacy/research-graph/quests", json=_body(uuid.uuid4().hex))
        assert denied.status_code == 403


def test_memory_api_is_a_controller_over_receipts(tmp_path, monkeypatch) -> None:
    seed = uuid.uuid4().hex
    quest = QuestSpec(
        identity_key=f"api-memory-{seed}",
        title="API memory quest",
        direction="Preserve contradictory evidence.",
        value_boundary="Ledger truth only.",
        safety_boundary=("No hidden context",),
    )
    ProgramGraphStore().create_quest(
        quest,
        GraphCommandContext(
            idempotency_key=f"api-memory:{seed}:quest",
            principal="pytest:api-memory",
        ),
    )
    memory_store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    monkeypatch.setattr(programs_api, "_MEMORY_STORE", memory_store)
    fact = ResearchMemoryFactSpec(
        scope_node_id=quest.node_id,
        kind=MemoryFactKind.CONTRADICTION,
        statement="The observed response contradicts the primary mechanism.",
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="api-task",
                context_role=MemoryContextRole.SUPPORTING,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id="api-fixture",
                sha256=content_sha256({"api": seed}),
                uri="fixture://api-memory",
            ),
        ),
    )
    with TestClient(app) as client:
        registered = client.post(
            "/legacy/research-graph/memory/facts",
            json={
                "idempotency_key": f"api-memory:{seed}:fact",
                "fact": fact.model_dump(mode="json"),
            },
        )
        assert registered.status_code == 200
        assert registered.json()["object_id"] == fact.fact_id

        draft = MemorySummaryDraft(
            producer_provider="provider-a",
            producer_model="model-a",
            prompt_sha256="a" * 64,
            summary_text="The primary mechanism is contradicted.",
            covered_fact_ids=(fact.fact_id,),
        )
        compacted = client.post(
            "/legacy/research-graph/memory/compactions",
            json={
                "idempotency_key": f"api-memory:{seed}:compact",
                "scope_node_id": quest.node_id,
                "task_key": "api-task",
                "draft": draft.model_dump(mode="json"),
            },
        )
        assert compacted.status_code == 200
        compaction_id = compacted.json()["object_id"]

        built = client.post(
            "/legacy/research-graph/memory/contexts",
            json={
                "idempotency_key": f"api-memory:{seed}:context",
                "request": {
                    "scope_node_id": quest.node_id,
                    "task_key": "api-task",
                    "compaction_id": compaction_id,
                    "max_chars": 12_000,
                    "consumer_provider": "openai",
                    "consumer_model": "gpt-test",
                },
            },
        )
        assert built.status_code == 200
        body = built.json()
        assert fact.statement in body["context"]["prompt_text"]
        context_id = body["context_receipt_id"]

        rebuilt = client.get(
            f"/legacy/research-graph/memory/{quest.node_id}",
            params={"task_key": "api-task"},
        )
        assert rebuilt.status_code == 200
        assert rebuilt.json()["facts"][0]["created_by"] == "api:graph-owner"
        assert (
            client.get(
                f"/legacy/research-graph/memory/compactions/{compaction_id}/artifact"
            ).status_code
            == 200
        )
        loaded = client.get(f"/legacy/research-graph/memory/contexts/{context_id}")
        assert loaded.status_code == 200
        assert loaded.json()["context"]["context_sha256"] == body["context"]["context_sha256"]
