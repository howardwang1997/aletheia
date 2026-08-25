"""Hard evaluation sandbox and its minimal research-side request contract."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from aletheia.coder.executor import (
    docker_execution_is_infrastructure_failure,
    hardened_docker_command,
    resolve_docker_image,
    run_hardened_container,
)
from aletheia.evals.schemas import (
    EvaluationResearchRequest,
    ExecutionExitReason,
    ExecutorContract,
    ResourceBudget,
)


class EvaluationExecutorError(RuntimeError):
    """Trusted evaluator infrastructure failed before or around authored execution."""


@dataclass(frozen=True)
class EvaluationExecution:
    returncode: int | None
    output: bytes
    output_total_bytes: int
    output_truncated: bool
    started_at: datetime
    ended_at: datetime
    wall_time_s: float
    exit_reason: ExecutionExitReason
    timed_out: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    infrastructure_detail: str | None = None
    container_name: str | None = None


@dataclass(frozen=True)
class EvaluationExecutionContext:
    request: EvaluationResearchRequest
    research_workspace: Path
    submission_inbox: Path
    request_path: Path


class EvaluationExecutor(Protocol):
    @property
    def contract(self) -> ExecutorContract: ...

    def execute(
        self, context: EvaluationExecutionContext, budget: ResourceBudget
    ) -> EvaluationExecution: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DockerEvaluationExecutor:
    """Run a system command with no network and only public/work/submission mounts.

    The command executes inside the configured immutable sandbox image.  It receives
    ``/workspace/request.json`` and writes ``/submission/submission.json`` plus its
    declared artifacts.  No evaluator directory is mounted.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        image_ref: str | None = None,
        exposed_tools: tuple[str, ...] = (),
        gpu_enabled: bool = False,
    ) -> None:
        if not command or any(not str(part) for part in command):
            raise ValueError("evaluation container command cannot be empty")
        if gpu_enabled:
            raise ValueError("GPU evaluation requires a dedicated GPU executor; fail closed")
        self._command = tuple(command)
        if image_ref is None:
            from aletheia.config import get_settings

            image_ref = get_settings().evaluator_agent_docker_image
        self._image_ref = image_ref
        self._exposed_tools = tuple(exposed_tools)
        self._contract: ExecutorContract | None = None

    @property
    def contract(self) -> ExecutorContract:
        if self._contract is None:
            try:
                image_id = resolve_docker_image(self._image_ref)
            except Exception as exc:
                raise EvaluationExecutorError(str(exc)) from exc
            command_hash = hashlib.sha256("\0".join(self._command).encode("utf-8")).hexdigest()
            self._contract = ExecutorContract(
                executor_id=f"docker-eval-v1:{command_hash}",
                security_level="hard",
                sandbox_image_id=image_id,
                exposed_tools=self._exposed_tools,
                gpu_enabled=False,
            )
        return self._contract

    def execute(
        self, context: EvaluationExecutionContext, budget: ResourceBudget
    ) -> EvaluationExecution:
        contract = self.contract
        from aletheia.config import get_settings

        started_at = _utcnow()
        monotonic_start = time.monotonic()
        container_name = f"aletheia-eval-{uuid.uuid4().hex[:20]}"
        host_cpu_cap = float(get_settings().sandbox_docker_cpus)
        cpu_rate = max(0.01, min(host_cpu_cap, budget.cpu_seconds / budget.wall_time_s))
        try:
            command = hardened_docker_command(
                context.research_workspace,
                image_id=contract.sandbox_image_id,
                container_name=container_name,
                container_dir="/workspace",
                writable=True,
                command=list(self._command),
                additional_mounts=(
                    (context.request_path.parent, "/request", False),
                    (context.submission_inbox, "/submission", True),
                ),
                memory_mb=budget.memory_mb,
                cpus=cpu_rate,
                cpu_seconds=budget.cpu_seconds,
                environment={
                    "ALETHEIA_EVAL_ATTEMPT_ID": context.request.attempt_id,
                    "ALETHEIA_EVAL_REQUEST": "/request/request.json",
                    "ALETHEIA_EVAL_SUBMISSION_DIR": "/submission",
                    "ALETHEIA_EVAL_SEED": str(context.request.seed),
                },
                include_aletheia_pythonpath=False,
            )
            result = run_hardened_container(
                command,
                container_name=container_name,
                timeout_s=float(budget.wall_time_s),
                image_id=contract.sandbox_image_id,
            )
        except Exception as exc:
            raise EvaluationExecutorError(str(exc)) from exc
        ended_at = _utcnow()
        elapsed = max(0.0, time.monotonic() - monotonic_start)
        output = result.output.encode("utf-8")
        total_bytes = max(len(output), int(result.output_total_bytes))
        if result.timed_out:
            reason = ExecutionExitReason.WALL_TIME_LIMIT
        elif result.error is not None or docker_execution_is_infrastructure_failure(result):
            reason = ExecutionExitReason.INFRA_FAILURE
        elif result.returncode in {-9, 137, 152}:  # OOM/SIGKILL/SIGXCPU conventions.
            reason = ExecutionExitReason.RESOURCE_LIMIT
        elif result.returncode != 0:
            reason = ExecutionExitReason.PROCESS_ERROR
        else:
            reason = ExecutionExitReason.COMPLETED
        return EvaluationExecution(
            returncode=result.returncode,
            output=output,
            output_total_bytes=total_bytes,
            output_truncated=result.output_truncated or total_bytes > len(output),
            started_at=started_at,
            ended_at=ended_at,
            wall_time_s=elapsed,
            exit_reason=reason,
            timed_out=result.timed_out,
            infrastructure_detail=(result.error[:1024] if result.error else None),
            container_name=result.container_name,
        )


class LocalProcessEvaluationExecutor:
    """Explicit development-only executor; never accepted by a formal runner."""

    def __init__(self, command: tuple[str, ...]) -> None:
        if not command:
            raise ValueError("local evaluation command cannot be empty")
        self._command = command
        command_hash = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
        self._contract = ExecutorContract(
            executor_id=f"local-development-v1:{command_hash}",
            security_level="development",
            sandbox_image_id="sha256:" + "0" * 64,
        )

    @property
    def contract(self) -> ExecutorContract:
        return self._contract

    def execute(
        self, context: EvaluationExecutionContext, budget: ResourceBudget
    ) -> EvaluationExecution:
        import subprocess

        import resource

        def apply_limits() -> None:
            cpu = int(budget.cpu_seconds)
            memory = int(budget.memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            except (ValueError, OSError):
                pass

        started_at = _utcnow()
        monotonic_start = time.monotonic()
        safe_environment = {
            "HOME": str(context.research_workspace),
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(context.research_workspace / "tmp"),
            "ALETHEIA_EVAL_ATTEMPT_ID": context.request.attempt_id,
            "ALETHEIA_EVAL_REQUEST": str(context.request_path),
            "ALETHEIA_EVAL_SUBMISSION_DIR": str(context.submission_inbox),
            "ALETHEIA_EVAL_SEED": str(context.request.seed),
        }
        (context.research_workspace / "tmp").mkdir(exist_ok=True)
        try:
            proc = subprocess.run(
                self._command,
                cwd=context.research_workspace,
                env=safe_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=float(budget.wall_time_s),
                preexec_fn=apply_limits,
            )
            returncode = proc.returncode
            raw = proc.stdout or b""
            reason = (
                ExecutionExitReason.COMPLETED
                if proc.returncode == 0
                else ExecutionExitReason.PROCESS_ERROR
            )
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = None
            raw = bytes(exc.stdout or b"")
            reason = ExecutionExitReason.WALL_TIME_LIMIT
            timed_out = True
        except Exception as exc:
            raise EvaluationExecutorError(str(exc)) from exc
        cap = 262_144
        retained = raw[-cap:]
        ended_at = _utcnow()
        return EvaluationExecution(
            returncode=returncode,
            output=retained,
            output_total_bytes=len(raw),
            output_truncated=len(raw) > len(retained),
            started_at=started_at,
            ended_at=ended_at,
            wall_time_s=max(0.0, time.monotonic() - monotonic_start),
            exit_reason=reason,
            timed_out=timed_out,
            container_name=None,
        )


def stage_attempt_directories(root: Path, attempt_id: str) -> tuple[Path, Path]:
    """Create fresh research and inbox leaves without following caller-controlled paths."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", attempt_id):
        raise ValueError("invalid attempt id")
    root = Path(root).resolve(strict=False)
    research_parent = root / "research_attempts"
    inbox_parent = root / "submission_inbox"
    research_parent.mkdir(parents=True, exist_ok=True)
    inbox_parent.mkdir(parents=True, exist_ok=True)
    research = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=research_parent))
    inbox = Path(tempfile.mkdtemp(prefix=f"{attempt_id}-", dir=inbox_parent))
    return research.resolve(), inbox.resolve()


def seal_research_workspace(path: Path) -> None:
    """Remove write bits after execution so retained attempts cannot be silently edited."""
    path = Path(path)
    for candidate in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if candidate.is_symlink():
                continue
            candidate.chmod(0o500 if candidate.is_dir() else 0o400)
        except OSError:
            pass
    try:
        path.chmod(0o500)
    except OSError:
        pass


def erase_development_attempt(path: Path) -> None:
    """Test-only helper; formal evaluator code never calls this."""
    shutil.rmtree(path)
