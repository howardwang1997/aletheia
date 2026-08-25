"""Real hard-boundary acceptance tests for the F7-S2 evaluator runner."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.coder.executor import SandboxExecution, wait_until_container_gone
from aletheia.evals.sandbox import (
    DockerEvaluationExecutor,
    EvaluationExecutionContext,
)
from aletheia.evals.schemas import AttemptStatus, EvaluationResearchRequest, ExecutionExitReason
from aletheia.paths import WORKSPACES_ROOT

from .f7s2_fixtures import build_case

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _evaluator_image_available():
    settings = get_settings()
    image = settings.evaluator_agent_docker_image
    inspect = subprocess.run(
        [settings.sandbox_docker_command, "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip(f"evaluator agent image unavailable: {image}")


def test_real_container_sees_public_request_but_not_evaluator_assets_or_host_env(
    workspace_tmp_path, monkeypatch
):
    monkeypatch.setenv("ALETHEIA_EVALUATOR_HOST_SECRET", "must-not-leak")
    script = r"""
import hashlib, json, os, pathlib, socket
request = json.loads(pathlib.Path(os.environ["ALETHEIA_EVAL_REQUEST"]).read_text())
assert "hidden_asset_ref" not in json.dumps(request)
assert "scorer" not in json.dumps(request)
assert os.environ.get("ALETHEIA_EVALUATOR_HOST_SECRET") is None
assert not pathlib.Path("/opt/aletheia").exists()
assert not pathlib.Path("/evaluator").exists()
assert not pathlib.Path("/hidden").exists()
sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect(("1.1.1.1", 443))
    raise RuntimeError("network unexpectedly available")
except OSError:
    pass
finally:
    sock.close()
raw = b'{"answer": "42"}'
inbox = pathlib.Path(os.environ["ALETHEIA_EVAL_SUBMISSION_DIR"])
(inbox / "answer.json").write_bytes(raw)
submission = {
    "schema_version": 1,
    "attempt_id": request["attempt_id"],
    "task_manifest_sha256": request["public_task"]["task_manifest_sha256"],
    "system_manifest_sha256": request["system_manifest_sha256"],
    "artifacts": [{
        "kind": "answer",
        "media_type": "application/json",
        "uri": "inbox://answer.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }],
    "submitted_at": "2026-08-13T00:00:00Z",
    "declared_contamination": [],
}
(inbox / "submission.json").write_text(json.dumps(submission))
"""
    executor = DockerEvaluationExecutor(("python", "-c", script))
    runner, suite, plan, task, _ledger = build_case(workspace_tmp_path, executor=executor)

    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is AttemptStatus.COMPLETED
    assert outcome.execution_receipt.sandbox_image_id.startswith("sha256:")
    assert outcome.scorer_receipt is not None


def test_real_evaluation_timeout_removes_exact_container(workspace_tmp_path):
    executor = DockerEvaluationExecutor(("python", "-c", "while True: pass"))
    runner, _suite, plan, task, _ledger = build_case(workspace_tmp_path, executor=executor)
    research = workspace_tmp_path / "timeout-research"
    inbox = workspace_tmp_path / "timeout-inbox"
    public = workspace_tmp_path / "timeout-public"
    for path in (research, inbox, public):
        path.mkdir(parents=True)
    request = EvaluationResearchRequest(
        attempt_id="timeout-attempt",
        run_plan_sha256=plan.manifest_sha256,
        system_manifest_sha256=plan.system_manifest_sha256,
        repeat_index=0,
        seed=plan.slots[0].seed,
        public_task=task.public_view(),
    )
    request_path = public / "request.json"
    request_path.write_text(request.model_dump_json())
    budget = task.resource_budget.model_copy(update={"wall_time_s": 1})

    execution = executor.execute(
        EvaluationExecutionContext(
            request=request,
            research_workspace=research,
            submission_inbox=inbox,
            request_path=request_path,
        ),
        budget,
    )

    assert execution.exit_reason is ExecutionExitReason.WALL_TIME_LIMIT
    assert execution.timed_out is True
    assert execution.container_name
    assert wait_until_container_gone(execution.container_name)


def test_authored_exit_125_is_process_failure_not_retryable_infrastructure(
    workspace_tmp_path,
):
    executor = DockerEvaluationExecutor(("python", "-c", "raise SystemExit(125)"))
    _runner, _suite, plan, task, _ledger = build_case(workspace_tmp_path, executor=executor)
    research = workspace_tmp_path / "exit-125-research"
    inbox = workspace_tmp_path / "exit-125-inbox"
    public = workspace_tmp_path / "exit-125-public"
    for path in (research, inbox, public):
        path.mkdir(parents=True)
    request = EvaluationResearchRequest(
        attempt_id="exit-125-attempt",
        run_plan_sha256=plan.manifest_sha256,
        system_manifest_sha256=plan.system_manifest_sha256,
        repeat_index=0,
        seed=plan.slots[0].seed,
        public_task=task.public_view(),
    )
    request_path = public / "request.json"
    request_path.write_text(request.model_dump_json())

    execution = executor.execute(
        EvaluationExecutionContext(
            request=request,
            research_workspace=research,
            submission_inbox=inbox,
            request_path=request_path,
        ),
        task.resource_budget,
    )

    assert execution.returncode == 125
    assert execution.exit_reason is ExecutionExitReason.PROCESS_ERROR
    assert execution.infrastructure_detail is None


def test_stopped_container_client_hang_is_infrastructure_failure(monkeypatch, workspace_tmp_path):
    import aletheia.evals.sandbox as sandbox_module

    executor = DockerEvaluationExecutor(("python", "-c", "print('done')"))
    monkeypatch.setattr(
        sandbox_module,
        "run_hardened_container",
        lambda *_args, **_kwargs: SandboxExecution(
            -15,
            "done\n",
            image_id=executor.contract.sandbox_image_id,
            error="Docker client did not exit after its container stopped",
            container_started=True,
        ),
    )
    _runner, _suite, plan, task, _ledger = build_case(workspace_tmp_path, executor=executor)
    research = workspace_tmp_path / "client-hang-research"
    inbox = workspace_tmp_path / "client-hang-inbox"
    public = workspace_tmp_path / "client-hang-public"
    for path in (research, inbox, public):
        path.mkdir(parents=True)
    request = EvaluationResearchRequest(
        attempt_id="client-hang-attempt",
        run_plan_sha256=plan.manifest_sha256,
        system_manifest_sha256=plan.system_manifest_sha256,
        repeat_index=0,
        seed=plan.slots[0].seed,
        public_task=task.public_view(),
    )
    request_path = public / "request.json"
    request_path.write_text(request.model_dump_json())

    execution = executor.execute(
        EvaluationExecutionContext(
            request=request,
            research_workspace=research,
            submission_inbox=inbox,
            request_path=request_path,
        ),
        task.resource_budget,
    )

    assert execution.timed_out is False
    assert execution.exit_reason is ExecutionExitReason.INFRA_FAILURE
    assert execution.infrastructure_detail == "Docker client did not exit after its container stopped"


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        import shutil

        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)
