"""Phase 2 increment 7: hard-sandbox compute backend — the hardened `docker run`
command, host-side featurize/stage + finalize (docker mocked), backend selection,
and the dry-run path. The real container is exercised by a separate manual smoke
(docker/sandbox.Dockerfile); these tests stay fast + deterministic (no daemon).
"""

from __future__ import annotations

import json
import types

import numpy as np
import pandas as pd

from aletheia.compute.base import JobSpec
from aletheia.compute.docker_backend import DockerBackend
from aletheia.compute.factory import get_compute_backend
from aletheia.db import create_all


def test_docker_command_is_hardened():
    cmd = DockerBackend().docker_command(__import__("pathlib").Path("/tmp/job_x"))
    s = " ".join(cmd)
    assert "--network none" in s  # no-network is the hard boundary
    assert "--read-only" in cmd and "--cap-drop" in cmd and "ALL" in cmd
    assert "no-new-privileges" in s and "--pids-limit" in cmd
    assert "--memory" in cmd and "--cpus" in cmd and "--user" in cmd
    assert "/tmp/job_x:/work" in s and ":/repo:ro" in s  # workspace rw, source ro
    assert cmd[-3:] == ["aletheia-sandbox:latest", "python", "/work/train_sandbox.py"]


def test_allow_network_drops_isolation_flag(monkeypatch):
    import aletheia.compute.docker_backend as mod

    monkeypatch.setattr(
        mod, "get_settings",
        lambda: types.SimpleNamespace(
            sandbox_max_memory_mb=2048, sandbox_docker_cpus=1.0, sandbox_docker_pids=128,
            sandbox_allow_network=True, sandbox_docker_image="img:test",
        ),
    )
    cmd = DockerBackend().docker_command(__import__("pathlib").Path("/tmp/j"))
    assert "--network" not in cmd  # opt-in escape hatch removes isolation


def test_backend_selection(monkeypatch):
    import aletheia.compute.factory as fac

    monkeypatch.setattr(fac, "get_settings", lambda: types.SimpleNamespace(compute_backend="local"))
    assert get_compute_backend().name == "local"
    monkeypatch.setattr(fac, "get_settings", lambda: types.SimpleNamespace(compute_backend="docker"))
    assert get_compute_backend().name == "docker"


def test_dry_run_synthesizes_without_docker():
    create_all()
    backend = DockerBackend()
    spec = JobSpec(run_id="dock-dry", domain="materials", design={"model": "rf"},
                   data_spec={}, experiment_id=None, dry_run=True)
    job_id = backend.submit(spec)  # must not invoke docker
    st = backend.status(job_id)
    assert st.status == "done" and st.metrics["mae_lcso"] == 0.63


def test_submit_stages_and_finalizes(monkeypatch):
    """Host featurizes + stages X/y; the container run is mocked to emit metrics."""
    create_all()
    import aletheia.compute.docker_backend as mod

    class _FakePlugin:
        def load_data(self, spec):
            return pd.DataFrame({"composition": ["GaN"] * 6})

        def featurize(self, df, design):
            X = np.zeros((6, 3))
            y = np.arange(6.0)
            groups = np.array(["a", "b", "c", "a", "b", "c"], dtype=object)
            return X, y, ["f0", "f1", "f2"], groups

    monkeypatch.setattr(mod, "get_domain_plugin", lambda d: _FakePlugin())

    def fake_run(cmd, **kw):
        workdir = next(a[: -len(":/work")] for a in cmd if a.endswith(":/work"))
        from pathlib import Path

        # the staged arrays the host wrote are present for the (mocked) container
        assert (Path(workdir) / "staged.npz").exists()
        assert (Path(workdir) / "train_sandbox.py").exists()
        (Path(workdir) / "metrics.json").write_text(
            json.dumps({"metrics": {"mae_lcso": 0.42}, "artifacts": [], "info": {}})
        )
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    backend = DockerBackend()
    spec = JobSpec(run_id="dock-real", domain="materials",
                   design={"model": "rf", "solution_code": "def build_pipeline():\n    pass\n"},
                   data_spec={}, experiment_id=None, dry_run=False)
    job_id = backend.submit(spec)
    st = backend.status(job_id)
    assert st.status == "done" and st.metrics["mae_lcso"] == 0.42
