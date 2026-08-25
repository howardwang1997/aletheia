from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aletheia.api.deps import require_access
from aletheia.api.research_kernel import (
    get_research_controller_launcher,
    router,
)
from aletheia.jobs.queue import QueueInvariantError
from aletheia.research_controller.contracts import (
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchReceipt,
    ResearchControllerRegistration,
)
from aletheia.research_store.store import ResearchQuestNotFound, ResearchStoreInvariantError

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
PROGRAM_ID = "prg_" + "1" * 32
QUEST_ID = "qst_" + "2" * 32


class _Launcher:
    def __init__(self) -> None:
        self.calls = []

    def launch(self, request, *, registered_by_principal_id):
        self.calls.append((request, registered_by_principal_id))
        registration = ResearchControllerRegistration(
            registration_id=request.registration_id,
            launch_request=request,
            controller_id="rctl_" + "3" * 32,
            controller_manifest_sha256="4" * 64,
            controller_principal_id="controller:deployment-v1",
            registered_by_principal_id=registered_by_principal_id,
            registered_at=NOW,
        )
        wakeup = ControllerWakeup(
            registration_id=registration.registration_id,
            quest_id=request.quest_id,
            source_kind=ControllerWakeupKind.LAUNCH,
            source_key=registration.registration_id,
            source_sha256=request.request_sha256,
        )
        return ResearchControllerLaunchReceipt(
            registration=registration,
            wakeup=wakeup,
            durable_task_id="task-rctl-" + "5" * 32,
            created=True,
        )


def _body() -> dict[str, object]:
    return {
        "idempotency_key": "launch:api-one",
        "expected_stream_version": 7,
        "expected_tail_event_sha256": "6" * 64,
        "expected_snapshot_sha256": "7" * 64,
    }


def test_launch_api_builds_path_scoped_request_and_keeps_policy_server_side() -> None:
    app = FastAPI()
    app.include_router(router)
    launcher = _Launcher()
    app.dependency_overrides[require_access] = lambda: {
        "id": "user-123",
        "role": "owner",
    }
    app.dependency_overrides[get_research_controller_launcher] = lambda: launcher
    with TestClient(app) as client:
        response = client.post(
            f"/research-kernel/programs/{PROGRAM_ID}/quests/{QUEST_ID}/launch",
            json=_body(),
        )
    assert response.status_code == 200, response.text
    request, principal = launcher.calls[0]
    assert request.program_id == PROGRAM_ID
    assert request.quest_id == QUEST_ID
    assert principal == "http-user:user-123"
    assert response.json()["registration"]["scientific_checkpoint_created"] is False


def test_launch_api_rejects_caller_selected_controller_policy() -> None:
    app = FastAPI()
    app.include_router(router)
    launcher = _Launcher()
    app.dependency_overrides[require_access] = lambda: {
        "id": "user-123",
        "role": "owner",
    }
    app.dependency_overrides[get_research_controller_launcher] = lambda: launcher
    body = {**_body(), "controller_manifest_sha256": "8" * 64}
    with TestClient(app) as client:
        response = client.post(
            f"/research-kernel/programs/{PROGRAM_ID}/quests/{QUEST_ID}/launch",
            json=body,
        )
    assert response.status_code == 422
    assert launcher.calls == []


class _RaisingLauncher:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def launch(self, *_args, **_kwargs):
        raise self.error


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (ResearchQuestNotFound("unknown Quest"), 404, "unknown Quest"),
        (
            ResearchStoreInvariantError("sensitive persisted invariant detail"),
            500,
            "research-kernel audit failed",
        ),
        (
            QueueInvariantError("sensitive durable queue invariant detail"),
            500,
            "controller queue audit failed",
        ),
    ],
)
def test_launch_api_maps_kernel_reaudit_failures(error, status_code, detail) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_access] = lambda: {
        "id": "user-123",
        "role": "owner",
    }
    app.dependency_overrides[get_research_controller_launcher] = lambda: _RaisingLauncher(error)

    with TestClient(app) as client:
        response = client.post(
            f"/research-kernel/programs/{PROGRAM_ID}/quests/{QUEST_ID}/launch",
            json=_body(),
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
