"""Adversarial acceptance tests for the production authored-code boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aletheia.coder.executor import (
    execute_python_files,
    hard_sandbox_preflight,
    hardened_docker_command,
    resolve_docker_image,
    run_hardened_container,
    wait_until_container_gone,
)
from aletheia.config import get_settings
from aletheia.paths import WORKSPACES_ROOT

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _hard_sandbox_available():
    status = hard_sandbox_preflight(freeze=False)
    if not status.get("ok"):
        pytest.skip(f"hard sandbox unavailable: {status.get('error')}")


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"trusted-terminal-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def _sentinel(output: str) -> dict:
    line = next(x for x in output.splitlines() if x.startswith("ALETHEIA_HOSTILE_RESULT "))
    return json.loads(line.removeprefix("ALETHEIA_HOSTILE_RESULT "))


def test_hostile_code_cannot_read_host_files_env_network_or_modify_repo(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    canary = root / ".sandbox-host-canary"
    canary.write_text("HOST_ONLY_ORIGINAL")
    monkeypatch.setenv("ALETHEIA_HOST_SECRET", "DO_NOT_EXPOSE_6d66b7")
    script = f"""
import json, os, socket
host_path = {str(canary)!r}
result = {{}}
try:
    result['host_read'] = open(host_path).read()
except Exception:
    result['host_read'] = None
try:
    open(host_path, 'w').write('PWNED')
    result['host_write'] = True
except Exception:
    result['host_write'] = False
result['secret'] = os.environ.get('ALETHEIA_HOST_SECRET')
s = socket.socket()
s.settimeout(1.0)
try:
    s.connect(('1.1.1.1', 443))
    result['network'] = True
except Exception:
    result['network'] = False
finally:
    s.close()
print('ALETHEIA_HOSTILE_RESULT ' + json.dumps(result, sort_keys=True))
"""
    try:
        result = execute_python_files({"runner.py": script}, timeout_s=10, backend="docker")
        assert result.ok, result
        observed = _sentinel(result.output)
        assert observed == {
            "host_read": None,
            "host_write": False,
            "network": False,
            "secret": None,
        }
        assert canary.read_text() == "HOST_ONLY_ORIGINAL"
        assert "DO_NOT_EXPOSE_6d66b7" not in result.output
    finally:
        canary.unlink(missing_ok=True)


def test_fork_bomb_is_pid_capped_and_cannot_outlive_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_docker_pids", 16)
    script = """
import os, time
while True:
    try:
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
    except OSError:
        pass
"""
    result = execute_python_files({"runner.py": script}, timeout_s=1.5, backend="docker")
    assert result.timed_out
    assert result.container_name
    assert wait_until_container_gone(result.container_name)


def test_memory_exhaustion_is_killed_inside_container(monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_max_memory_mb", 96)
    result = execute_python_files(
        {"runner.py": ("blocks=[]\nwhile True:\n    blocks.append(bytearray(16 * 1024 * 1024))\n")},
        timeout_s=15,
        backend="docker",
    )
    assert not result.ok
    assert result.container_name
    assert wait_until_container_gone(result.container_name)


def test_trusted_terminal_receipt_ends_one_shot_container(workspace_tmp_path):
    work = workspace_tmp_path / "trusted-terminal-work"
    work.mkdir()
    receipt = work / "result.json"
    (work / "trusted.py").write_text(
        """import json, os, time
temporary = '/work/.result.json.tmp'
with open(temporary, 'w', encoding='utf-8') as handle:
    json.dump({'complete': True}, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, '/work/result.json')
time.sleep(30)
""",
        encoding="utf-8",
    )
    image_id = resolve_docker_image()
    name = "aletheia-trusted-terminal-test"
    command = hardened_docker_command(
        work,
        image_id=image_id,
        container_name=name,
        container_dir="/work",
        writable=True,
        command=["python", "/work/trusted.py"],
        include_aletheia_pythonpath=False,
    )

    result = run_hardened_container(
        command,
        container_name=name,
        timeout_s=25,
        image_id=image_id,
        trusted_terminal_receipt=receipt,
    )

    assert result.trusted_terminal_receipt_observed
    assert not result.timed_out and result.error is None
    assert json.loads(receipt.read_text(encoding="utf-8")) == {"complete": True}
    assert wait_until_container_gone(name)


def test_unbounded_stdout_is_truncated_and_job_is_removed(monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_output_limit_bytes", 16_384)
    result = execute_python_files(
        {"runner.py": "while True:\n    print('X' * 4096, flush=True)\n"},
        timeout_s=1.0,
        backend="docker",
    )
    assert result.timed_out
    assert len(result.output.encode()) <= 16_384
    assert result.container_name
    assert wait_until_container_gone(result.container_name)


def test_no_aletheia_sandbox_container_is_left_running():
    proc = subprocess.run(
        [
            get_settings().sandbox_docker_command,
            "ps",
            "--filter",
            "name=aletheia-auth-",
            "--filter",
            "name=aletheia-job-",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not proc.stdout.strip()


def test_real_authored_training_runs_against_baked_harness(monkeypatch):
    """Exercise the actual writable job mount and baked Aletheia evaluation package."""
    import aletheia.compute.docker_backend as module
    from aletheia.compute.base import JobSpec
    from aletheia.compute.docker_backend import DockerBackend
    from aletheia.db import create_all

    class _HostStagePlugin:
        def load_data(self, spec):
            return pd.DataFrame({"x": range(40)})

        def featurize(self, df, design):
            rng = np.random.default_rng(7)
            X = rng.normal(size=(40, 5))
            y = X[:, 0] * 1.5 + rng.normal(scale=0.1, size=40)
            groups = np.asarray([f"g{i % 8}" for i in range(40)], dtype=object)
            return X, y, [f"f{i}" for i in range(5)], groups

    create_all()
    monkeypatch.setattr(module, "get_domain_plugin", lambda _domain: _HostStagePlugin())
    backend = DockerBackend()
    job_id = backend.submit(
        JobSpec(
            run_id="hard-sandbox-integration",
            domain="materials",
            design={
                "model": "ridge",
                "random_state": 7,
                "test_size": 0.2,
                "solution_code": (
                    "from sklearn.linear_model import Ridge\n"
                    "def build_pipeline():\n"
                    "    return Ridge(alpha=0.5)\n"
                ),
            },
            data_spec={},
            experiment_id=None,
            dry_run=False,
        )
    )
    status = backend.status(job_id)
    assert status.status == "done", status.error
    assert status.metrics and status.metrics["mae_lcso"] >= 0
    assert str(status.info.get("sandbox_image_id", "")).startswith("sha256:")
    assert {a.get("kind") for a in status.artifacts} >= {"model", "eval"}
    assert all(Path(a["uri"]).exists() for a in status.artifacts)
