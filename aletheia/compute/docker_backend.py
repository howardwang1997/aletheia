"""Hard-sandbox compute backend.

The untrusted surface is the coder-authored model code. So we split the work:
the **host** (trusted, has network + matminer) loads + featurizes the data and
stages ``X/y/groups``; a **light, no-network container** runs only
``train_evaluate`` on the staged arrays — that is where the AI-authored
``build_pipeline`` executes, behind real isolation (no network, read-only root,
dropped capabilities, non-root, memory/PID/CPU caps). The aletheia source is
mounted read-only at ``/repo``; the agent runtime never enters the sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import aletheia
from aletheia.compute.base import ComputeBackend, JobSpec, JobStatus
from aletheia.compute.local import DRY_RUN_INFO, DRY_RUN_METRICS
from aletheia.config import get_settings
from aletheia.domains.registry import get_domain_plugin
from aletheia.memory.service import (
    create_compute_job,
    record_artifacts,
    record_metrics,
    set_compute_job_status,
)
from aletheia.paths import run_workspace

_REPO_ROOT = Path(aletheia.__file__).resolve().parents[1]

# Runs INSIDE the container: load staged arrays + design, run the fixed eval
# harness (which executes the coder's build_pipeline), write metrics.json.
_SANDBOX_SCRIPT = '''\
import json, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, "/repo")
here = Path("/work")
payload = json.loads((here / "payload.json").read_text())
data = np.load(here / "staged.npz", allow_pickle=True)
X, y, groups = data["X"], data["y"], data["groups"]
design = dict(payload["design"])
if (here / "solution.py").exists():
    design["solution_path"] = str(here / "solution.py")

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

result = MaterialsBandGapPlugin().train_evaluate(X, y, design, here, groups=groups)
(here / "metrics.json").write_text(json.dumps(result.to_dict()))
print("ALETHEIA_JOB_OK")
'''


class DockerBackend(ComputeBackend):
    name = "docker"

    def __init__(self) -> None:
        self._terminal: dict[str, JobStatus] = {}

    # --- the hardened `docker run` command ---
    def docker_command(self, workdir: Path) -> list[str]:
        s = get_settings()
        cmd = [
            "docker", "run", "--rm",
            "--memory", f"{s.sandbox_max_memory_mb}m",
            "--cpus", str(s.sandbox_docker_cpus),
            "--pids-limit", str(s.sandbox_docker_pids),
            "--read-only", "--tmpfs", "/tmp:rw,size=512m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{workdir}:/work",
            "-v", f"{_REPO_ROOT}:/repo:ro",
            "-w", "/work",
            "-e", "PYTHONPATH=/repo",
            "-e", "MPLBACKEND=Agg",
            "-e", "MPLCONFIGDIR=/tmp",  # writable on the read-only root (tmpfs)
        ]
        if not s.sandbox_allow_network:
            cmd += ["--network", "none"]
        cmd += [s.sandbox_docker_image, "python", "/work/train_sandbox.py"]
        return cmd

    # --- submit (blocking: host stages, container trains, then finalize) ---
    def submit(self, spec: JobSpec) -> str:
        job_id = create_compute_job(
            spec.experiment_id, backend=self.name,
            resources={"design": spec.design, "data_spec": spec.data_spec, "dry_run": spec.dry_run},
            status="queued",
        )
        workdir = run_workspace(spec.run_id) / f"job_{job_id}"
        workdir.mkdir(parents=True, exist_ok=True)

        if spec.dry_run:
            (workdir / "metrics.json").write_text(
                json.dumps({"metrics": dict(DRY_RUN_METRICS), "artifacts": [], "info": dict(DRY_RUN_INFO)})
            )
            return self._finalize(job_id, workdir, spec.experiment_id, rc=0)

        # host-side: featurize (trusted, may use network) and stage arrays
        import numpy as np

        design = dict(spec.design)
        solution_code = design.pop("solution_code", None)
        if solution_code:
            (workdir / "solution.py").write_text(solution_code)
        plugin = get_domain_plugin(spec.domain)
        df = plugin.load_data(spec.data_spec)
        X, y, _features, groups = plugin.featurize(df, design)
        np.savez(
            workdir / "staged.npz",
            X=np.asarray(X, dtype=float),
            y=np.asarray(y, dtype=float),
            groups=np.asarray(groups, dtype=object),
        )
        (workdir / "payload.json").write_text(
            json.dumps({"domain": spec.domain, "design": design, "data_spec": spec.data_spec})
        )
        (workdir / "train_sandbox.py").write_text(_SANDBOX_SCRIPT)

        set_compute_job_status(job_id, "running")
        log = (workdir / "job.log").open("wb")
        try:
            proc = subprocess.run(
                self.docker_command(workdir),
                stdout=log, stderr=subprocess.STDOUT,
                timeout=get_settings().sandbox_timeout_s, check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            log.close()
        return self._finalize(job_id, workdir, spec.experiment_id, rc=rc)

    def _finalize(self, job_id: str, workdir: Path, experiment_id: str | None, rc: int) -> str:
        metrics_path = workdir / "metrics.json"
        if not metrics_path.exists() or rc not in (0, None):
            set_compute_job_status(job_id, "failed")
            self._terminal[job_id] = JobStatus(
                job_id=job_id, status="failed",
                error=f"container rc={rc}; metrics.json present={metrics_path.exists()}",
            )
            return job_id
        data = json.loads(metrics_path.read_text())
        metrics, artifacts, info = data.get("metrics", {}), data.get("artifacts", []), data.get("info", {})
        if experiment_id:
            record_metrics(experiment_id, metrics, split="test")
            if artifacts:
                record_artifacts(experiment_id, artifacts)
        set_compute_job_status(job_id, "done")
        self._terminal[job_id] = JobStatus(
            job_id=job_id, status="done", metrics=metrics, artifacts=artifacts, info=info
        )
        return job_id

    def status(self, job_id: str) -> JobStatus:
        return self._terminal.get(job_id, JobStatus(job_id=job_id, status="failed", error="unknown job"))

    def logs(self, job_id: str) -> str:
        return ""  # submit is blocking; the container log lives in the job workspace

    def cancel(self, job_id: str) -> None:
        return None  # a blocking container run is past the cancellation point here

    def capacity(self) -> dict:
        return {"backend": self.name, "running": 0, "max_concurrent": 1}


_BACKEND: DockerBackend | None = None


def get_docker_backend() -> DockerBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = DockerBackend()
    return _BACKEND
