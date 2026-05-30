"""Local subprocess compute backend.

``submit`` writes a small generated ``train.py`` into a per-job workspace and runs
it as a separate process (so training never executes in the orchestrator's own
context). ``status`` polls the process and, on first completion, reads
``metrics.json`` and persists metrics + artifacts + the job's terminal status to
the ledger. A ``dry_run`` job synthesizes metrics and spawns nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aletheia.compute.base import ComputeBackend, JobSpec, JobStatus
from aletheia.memory.service import (
    create_compute_job,
    record_artifacts,
    record_metrics,
    set_compute_job_status,
)
from aletheia.paths import run_workspace

# Generated per job; kept tiny and dependency-light. Reads payload.json, runs the
# domain plugin end-to-end, writes metrics.json.
_TRAIN_SCRIPT = '''\
import json
from pathlib import Path

from aletheia.domains.registry import get_domain_plugin

here = Path(__file__).resolve().parent
payload = json.loads((here / "payload.json").read_text())
plugin = get_domain_plugin(payload.get("domain"))
result = plugin.run_experiment(payload["design"], payload["data_spec"], here)
(here / "metrics.json").write_text(json.dumps(result.to_dict()))
print("ALETHEIA_JOB_OK")
'''


@dataclass
class _Job:
    job_id: str
    spec: JobSpec
    workdir: Path
    proc: subprocess.Popen | None = None
    started_at: float = field(default_factory=time.time)
    finalized: bool = False
    terminal: JobStatus | None = None


class LocalBackend(ComputeBackend):
    name = "local"

    def __init__(self, max_concurrent: int = 2) -> None:
        self._jobs: dict[str, _Job] = {}
        self._max_concurrent = max_concurrent

    # --- submit ---
    def submit(self, spec: JobSpec) -> str:
        job_id = create_compute_job(
            spec.experiment_id,
            backend=self.name,
            resources={"design": spec.design, "data_spec": spec.data_spec, "dry_run": spec.dry_run},
            status="queued",
        )
        workdir = run_workspace(spec.run_id) / f"job_{job_id}"
        workdir.mkdir(parents=True, exist_ok=True)
        design = dict(spec.design)
        # a coder-authored solution: persist it and point the design at it so the
        # plugin's harness uses it as the candidate estimator (sandboxed in-subprocess)
        solution_code = design.pop("solution_code", None)
        if solution_code:
            sol_path = workdir / "solution.py"
            sol_path.write_text(solution_code)
            design["solution_path"] = str(sol_path.resolve())
            spec.design = design
        (workdir / "payload.json").write_text(
            json.dumps(
                {"domain": spec.domain, "design": design, "data_spec": spec.data_spec}
            )
        )
        job = _Job(job_id=job_id, spec=spec, workdir=workdir)

        if spec.dry_run:
            # compute-dry-run: synthesize a plausible result, spawn nothing. Keys
            # mirror the real protocol so the dashboard/report render the full panel;
            # headline mae/r2 = the (pessimistic) leave-chemical-system-out numbers.
            metrics = {
                "mae": 0.63, "r2": 0.41, "rmse": 0.84,
                "mae_lcso": 0.63, "r2_lcso": 0.41, "rmse_lcso": 0.84,
                "mae_cv_mean": 0.49, "mae_cv_std": 0.03, "r2_cv_mean": 0.65,
                "mae_holdout": 0.49, "r2_holdout": 0.65, "rmse_holdout": 0.61,
            }
            info = {
                "dry_run": True,
                "eval_summary": (
                    "LCSO(GroupKFold) MAE 0.630 eV, R² 0.410, RMSE 0.840 (HEADLINE, all same split) | "
                    "RepeatedKFold 5x5 MAE 0.490±0.030 | random holdout MAE 0.490 (comparison only) | "
                    "baselines(LCSO MAE): dummy_mean 1.100, ridge 0.850, knn 0.780, gbm 0.610, "
                    "random_forest 0.630 | errors by gap: metal~0 n=10 MAE=0.20; [0.5,2) n=8 MAE=0.55"
                ),
            }
            (workdir / "metrics.json").write_text(
                json.dumps({"metrics": metrics, "artifacts": [], "info": info})
            )
            self._jobs[job_id] = job
            self._finalize(job)  # persists + sets terminal status immediately
            return job_id

        (workdir / "train.py").write_text(_TRAIN_SCRIPT)
        log = (workdir / "job.log").open("wb")
        # sys.executable == this env's python (aletheia is editable-installed).
        # Restricted subprocess: CPU/memory rlimits applied in the child.
        from aletheia.coder.sandbox import resource_limits

        job.proc = subprocess.Popen(
            [sys.executable, str(workdir / "train.py")],
            cwd=str(workdir),
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=resource_limits(),
        )
        set_compute_job_status(job_id, "running")
        self._jobs[job_id] = job
        return job_id

    # --- status ---
    def status(self, job_id: str) -> JobStatus:
        job = self._jobs.get(job_id)
        if job is None:
            return JobStatus(job_id=job_id, status="failed", error="unknown job")
        if job.finalized and job.terminal is not None:
            return job.terminal
        if job.proc is not None and job.proc.poll() is None:
            # wall-clock backstop: kill a training subprocess that overruns
            from aletheia.config import get_settings

            if time.time() - job.started_at > get_settings().sandbox_timeout_s:
                job.proc.kill()
                set_compute_job_status(job_id, "failed")
                job.finalized = True
                job.terminal = JobStatus(job_id=job_id, status="failed", error="sandbox timeout")
                return job.terminal
            return JobStatus(job_id=job_id, status="running")
        # process exited (or dry-run path that wasn't pre-finalized) -> finalize
        return self._finalize(job)

    def _finalize(self, job: _Job) -> JobStatus:
        metrics_path = job.workdir / "metrics.json"
        rc = job.proc.returncode if job.proc is not None else 0
        if not metrics_path.exists() or (rc not in (0, None)):
            status = JobStatus(
                job_id=job.job_id,
                status="failed",
                error=f"return code {rc}; metrics.json present={metrics_path.exists()}",
            )
            set_compute_job_status(job.job_id, "failed")
        else:
            data = json.loads(metrics_path.read_text())
            metrics = data.get("metrics", {})
            artifacts = data.get("artifacts", [])
            info = data.get("info", {})
            if job.spec.experiment_id:
                record_metrics(job.spec.experiment_id, metrics, split="test")
                if artifacts:
                    record_artifacts(job.spec.experiment_id, artifacts)
            set_compute_job_status(job.job_id, "done")
            status = JobStatus(
                job_id=job.job_id,
                status="done",
                metrics=metrics,
                artifacts=artifacts,
                info=info,
            )
        job.finalized = True
        job.terminal = status
        return status

    # --- logs / cancel / capacity ---
    def logs(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return ""
        log_path = job.workdir / "job.log"
        return log_path.read_text() if log_path.exists() else ""

    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.proc and job.proc.poll() is None:
            job.proc.terminate()
            set_compute_job_status(job_id, "failed")

    def capacity(self) -> dict[str, Any]:
        running = sum(
            1 for j in self._jobs.values() if j.proc and j.proc.poll() is None
        )
        return {
            "backend": self.name,
            "running": running,
            "max_concurrent": self._max_concurrent,
        }


_BACKEND: LocalBackend | None = None


def get_local_backend() -> LocalBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = LocalBackend()
    return _BACKEND
