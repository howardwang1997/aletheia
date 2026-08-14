"""Hard-sandbox compute backend.

The untrusted surface is the coder-authored model code. So we split the work:
the **host** (trusted, has network + matminer) loads + featurizes the data and
stages ``X/y/groups``; a **light, no-network container** runs only
``train_evaluate`` on the staged arrays — that is where the AI-authored
``build_pipeline`` executes, behind real isolation (no network, read-only root,
dropped capabilities, non-root, memory/PID/CPU caps). The small trusted evaluation
harness is baked into the immutable image; neither the repository nor host secrets
are mounted into the container.
"""

from __future__ import annotations

import json
from pathlib import Path

from aletheia.coder.executor import (
    hardened_docker_command,
    resolve_docker_image,
    run_hardened_container,
)
from aletheia.compute.base import ComputeBackend, JobSpec, JobStatus
from aletheia.compute.local import _dry_result
from aletheia.config import get_settings
from aletheia.domains.registry import get_domain_plugin
from aletheia.memory.service import (
    create_compute_job,
    record_artifacts,
    record_metrics,
    set_compute_job_status,
)
from aletheia.paths import run_workspace

# Runs INSIDE the container: load staged arrays + design, run the fixed eval
# harness (which executes the coder's build_pipeline), write metrics.json.
_SANDBOX_SCRIPT = '''\
import json, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, "/opt/aletheia")
here = Path("/work")
payload = json.loads((here / "payload.json").read_text())
data = np.load(here / "staged.npz", allow_pickle=True)
X, y, groups = data["X"], data["y"], data["groups"]
design = dict(payload["design"])
if (here / "solution.py").exists():
    design["solution_path"] = str(here / "solution.py")

from aletheia.domains.registry import get_domain_plugin

plugin = get_domain_plugin(payload.get("domain"))
result = plugin.train_evaluate(X, y, design, here, groups=groups)
(here / "metrics.json").write_text(json.dumps(result.to_dict()))
print("ALETHEIA_JOB_OK")
'''


class DockerBackend(ComputeBackend):
    name = "docker"

    def __init__(self) -> None:
        self._terminal: dict[str, JobStatus] = {}

    # --- the hardened `docker run` command ---
    def docker_command(
        self,
        workdir: Path,
        *,
        image_id: str | None = None,
        container_name: str = "aletheia-compute-preview",
    ) -> list[str]:
        immutable = image_id or resolve_docker_image()
        return hardened_docker_command(
            workdir,
            image_id=immutable,
            container_name=container_name,
            container_dir="/work",
            writable=True,
            command=["python", "/work/train_sandbox.py"],
        )

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
            metrics, info = _dry_result(spec.domain)
            (workdir / "metrics.json").write_text(
                json.dumps({"metrics": metrics, "artifacts": [], "info": info})
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
        # The raw-data location is deliberately omitted: the container receives only staged arrays.
        (workdir / "payload.json").write_text(json.dumps({"domain": spec.domain, "design": design}))
        (workdir / "train_sandbox.py").write_text(_SANDBOX_SCRIPT)

        set_compute_job_status(job_id, "running")
        try:
            image_id = resolve_docker_image()
            container_name = f"aletheia-job-{job_id[:20]}"
            execution = run_hardened_container(
                self.docker_command(
                    workdir, image_id=image_id, container_name=container_name
                ),
                container_name=container_name,
                timeout_s=get_settings().sandbox_timeout_s,
                image_id=image_id,
            )
            (workdir / "job.log").write_text(execution.output)
            rc = execution.returncode if execution.ok else -1
            failure = execution.error
            if execution.timed_out and not failure:
                failure = f"hard sandbox timed out after {get_settings().sandbox_timeout_s:g}s"
            if failure:
                (workdir / "job.log").write_text(
                    execution.output + f"\nSANDBOX_ERROR {failure}\n"
                )
        except Exception as exc:
            rc = -1
            image_id = None
            failure = f"hard sandbox launch failed: {exc}"
            (workdir / "job.log").write_text(f"SANDBOX_ERROR {exc}\n")
        return self._finalize(
            job_id, workdir, spec.experiment_id, rc=rc, image_id=image_id,
            failure=failure,
        )

    def _finalize(
        self,
        job_id: str,
        workdir: Path,
        experiment_id: str | None,
        rc: int,
        image_id: str | None = None,
        failure: str | None = None,
    ) -> str:
        metrics_path = workdir / "metrics.json"
        if not metrics_path.exists() or rc not in (0, None):
            set_compute_job_status(job_id, "failed")
            self._terminal[job_id] = JobStatus(
                job_id=job_id, status="failed",
                error=(
                    f"{failure}; container rc={rc}; metrics.json present={metrics_path.exists()}"
                    if failure else
                    f"container rc={rc}; metrics.json present={metrics_path.exists()}"
                ),
            )
            return job_id
        data = json.loads(metrics_path.read_text())
        metrics, artifacts, info = data.get("metrics", {}), data.get("artifacts", []), data.get("info", {})
        # Container paths are provenance, but host consumers need the actual staged artifact URI.
        for artifact in artifacts:
            uri = str(artifact.get("uri") or "")
            if uri.startswith("/work/"):
                artifact["uri"] = str(workdir / uri.removeprefix("/work/"))
        if image_id:
            info["sandbox_image_id"] = image_id
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
