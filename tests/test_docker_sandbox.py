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

from aletheia.coder.executor import SandboxExecution, hardened_docker_command
from aletheia.compute.base import JobSpec
from aletheia.compute.docker_backend import DockerBackend
from aletheia.compute.factory import get_compute_backend
from aletheia.db import create_all

_IMAGE_ID = "sha256:" + "a" * 64


def test_docker_command_is_hardened():
    cmd = DockerBackend().docker_command(
        __import__("pathlib").Path("/tmp/job_x"), image_id=_IMAGE_ID
    )
    s = " ".join(cmd)
    assert "--network none" in s  # no-network is the hard boundary
    assert "--read-only" in cmd and "--cap-drop" in cmd and "ALL" in cmd
    assert "no-new-privileges" in s and "--pids-limit" in cmd
    assert "--memory" in cmd and "--cpus" in cmd and "--user" in cmd
    assert "job_x,dst=/work" in s
    assert "/repo" not in s and ".env" not in s
    assert cmd[-3:] == [_IMAGE_ID, "python", "/work/train_sandbox.py"]


def test_hardened_command_normalizes_fractional_cpu_precision():
    cmd = hardened_docker_command(
        __import__("pathlib").Path("/tmp/job_cpu_precision"),
        image_id=_IMAGE_ID,
        container_name="aletheia-cpu-precision-test",
        cpus=10 / 45,
    )

    assert cmd[cmd.index("--cpus") + 1] == "0.222"


def test_network_escape_hatch_cannot_weaken_authored_sandbox(monkeypatch):
    from aletheia.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_allow_network", True)
    cmd = DockerBackend().docker_command(
        __import__("pathlib").Path("/tmp/j"), image_id=_IMAGE_ID
    )
    assert "--network none" in " ".join(cmd)


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
        mount = cmd[cmd.index("--mount") + 1]
        workdir = next(x.removeprefix("src=") for x in mount.split(",") if x.startswith("src="))
        from pathlib import Path

        # the staged arrays the host wrote are present for the (mocked) container
        assert (Path(workdir) / "staged.npz").exists()
        assert (Path(workdir) / "train_sandbox.py").exists()
        (Path(workdir) / "metrics.json").write_text(
            json.dumps({"metrics": {"mae_lcso": 0.42}, "artifacts": [], "info": {}})
        )
        return SandboxExecution(0, "ALETHEIA_JOB_OK\n", image_id=_IMAGE_ID)

    monkeypatch.setattr(mod, "resolve_docker_image", lambda: _IMAGE_ID)
    monkeypatch.setattr(mod, "run_hardened_container", fake_run)

    backend = DockerBackend()
    spec = JobSpec(run_id="dock-real", domain="materials",
                   design={"model": "rf", "solution_code": "def build_pipeline():\n    pass\n"},
                   data_spec={}, experiment_id=None, dry_run=False)
    job_id = backend.submit(spec)
    st = backend.status(job_id)
    assert st.status == "done" and st.metrics["mae_lcso"] == 0.42


def test_submit_preserves_hard_sandbox_timeout_reason(monkeypatch):
    create_all()
    import aletheia.compute.docker_backend as mod

    class _FakePlugin:
        def load_data(self, spec):
            return pd.DataFrame({"composition": ["GaN"] * 6})

        def featurize(self, df, design):
            return (
                np.zeros((6, 3)),
                np.arange(6.0),
                ["f0", "f1", "f2"],
                np.array(["a", "b", "c", "a", "b", "c"], dtype=object),
            )

    monkeypatch.setattr(mod, "get_domain_plugin", lambda d: _FakePlugin())
    monkeypatch.setattr(mod, "resolve_docker_image", lambda: _IMAGE_ID)
    monkeypatch.setattr(
        mod,
        "run_hardened_container",
        lambda *args, **kwargs: SandboxExecution(
            -15,
            "partial output",
            image_id=_IMAGE_ID,
            timed_out=True,
            error="hard sandbox timed out after 600s",
        ),
    )
    backend = DockerBackend()
    job_id = backend.submit(JobSpec(
        run_id="dock-timeout",
        domain="materials",
        design={"model": "rf"},
        data_spec={},
        experiment_id=None,
        dry_run=False,
    ))
    status = backend.status(job_id)
    assert status.status == "failed"
    assert "timed out after 600s" in status.error
