from __future__ import annotations

import pytest

from aletheia.api.execution_surfaces import LEGACY_PROTOCOL_EXECUTOR
from aletheia.api.runs import LaunchResponse, StartRunResponse, get_runs
from aletheia.api.sessions import CreateSessionResponse, get_session


def test_legacy_response_contracts_cannot_omit_or_rename_the_surface() -> None:
    assert StartRunResponse(run_id="run-one", mode="dry_run").execution_surface == (
        LEGACY_PROTOCOL_EXECUTOR
    )
    assert (
        LaunchResponse(
            run_id="run-one",
            task_id="task-one",
            status="queued",
            mode="dry_run",
        ).execution_surface
        == LEGACY_PROTOCOL_EXECUTOR
    )
    assert CreateSessionResponse(run_id="run-one", mode="dry_run").execution_surface == (
        LEGACY_PROTOCOL_EXECUTOR
    )


@pytest.mark.asyncio
async def test_legacy_list_and_session_reads_are_marked_without_mutating_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_run = {"id": "run-one", "status": "scoping"}
    monkeypatch.setattr("aletheia.api.runs.list_runs", lambda: [stored_run])
    monkeypatch.setattr("aletheia.api.sessions.get_run", lambda _run_id: stored_run)

    listed = await get_runs()
    session = await get_session("run-one")

    assert listed[0]["execution_surface"] == LEGACY_PROTOCOL_EXECUTOR
    assert session["execution_surface"] == LEGACY_PROTOCOL_EXECUTOR
    assert "execution_surface" not in stored_run
