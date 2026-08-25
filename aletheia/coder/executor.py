"""One execution boundary for every piece of AI-authored Python.

Production execution is a short-lived, no-network Docker container addressed by
its immutable image id.  The container receives only a staged directory; Docker
does not inherit the host environment and the repository is never mounted.

``local_dev`` exists solely for explicit unit/developer use.  It retains the old
rlimit subprocess behaviour, but is intentionally not an unattended-science
security boundary.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SandboxExecution:
    returncode: int | None
    output: str = ""
    image_id: str | None = None
    timed_out: bool = False
    error: str | None = None
    container_name: str | None = None
    output_total_bytes: int = 0
    output_truncated: bool = False
    wall_time_s: float = 0.0
    container_started: bool = False
    trusted_terminal_receipt_observed: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


def docker_execution_is_infrastructure_failure(result: SandboxExecution) -> bool:
    """Distinguish Docker failure from an authored process deliberately exiting 125.

    Docker uses 125 when ``docker run`` itself fails, but it also forwards an
    identical exit code from the container command.  Treating every 125 as
    retryable would let authored code manufacture infrastructure retries.  A
    125 is therefore infrastructure failure only when Docker did not issue a
    container ID.  The CID file lives outside every candidate mount, so authored
    code cannot forge or remove that launch evidence.
    """

    if result.timed_out:
        return False
    if result.error is not None or result.returncode is None:
        return True
    return result.returncode == 125 and not result.container_started


def resolve_docker_image(image_ref: str | None = None) -> str:
    """Resolve a mutable tag to the immutable local image id used by ``docker run``."""
    from aletheia.config import get_settings

    s = get_settings()
    ref = str(image_ref or s.sandbox_docker_image).strip()
    if not ref:
        raise RuntimeError("sandbox image is not configured")
    proc = subprocess.run(
        [s.sandbox_docker_command, "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )
    image_id = (proc.stdout or "").strip()
    if proc.returncode != 0 or not _IMAGE_ID.fullmatch(image_id):
        detail = (proc.stderr or proc.stdout or "image not found").strip().splitlines()[-1]
        raise RuntimeError(f"hard-sandbox image unavailable ({ref}): {detail[:240]}")
    return image_id


def hardened_docker_command(
    host_dir: Path,
    *,
    image_id: str,
    container_name: str,
    container_dir: str = "/input",
    writable: bool = False,
    command: list[str] | None = None,
    additional_mounts: Sequence[tuple[Path, str, bool]] = (),
    memory_mb: int | None = None,
    cpus: float | None = None,
    cpu_seconds: int | None = None,
    environment: Mapping[str, str] | None = None,
    include_aletheia_pythonpath: bool = True,
) -> list[str]:
    """Build the common hardened command used by probes, demos, and training."""
    from aletheia.config import get_settings

    if not _IMAGE_ID.fullmatch(str(image_id)):
        raise ValueError("sandbox execution requires an immutable sha256 image id")
    s = get_settings()
    memory = int(memory_mb if memory_mb is not None else s.sandbox_max_memory_mb)
    cpu_rate = float(cpus if cpus is not None else s.sandbox_docker_cpus)
    if memory <= 0 or cpu_rate <= 0:
        raise ValueError("sandbox memory and CPU limits must be positive")
    # Docker's NanoCPUs parser rejects arbitrarily precise decimal strings.
    # The scheduler never grants less than 0.01 CPU, so mill CPU precision is
    # deterministic and sufficiently finer than any frozen evaluation budget.
    docker_cpu_rate = f"{cpu_rate:.3f}".rstrip("0").rstrip(".")
    mount = f"type=bind,src={Path(host_dir).resolve()},dst={container_dir}"
    if not writable:
        mount += ",readonly"
    mounts = ["--mount", mount]
    for extra_host, extra_container, extra_writable in additional_mounts:
        if not str(extra_container).startswith("/"):
            raise ValueError("sandbox container mount paths must be absolute")
        extra = f"type=bind,src={Path(extra_host).resolve()},dst={extra_container}"
        if not extra_writable:
            extra += ",readonly"
        mounts.extend(["--mount", extra])
    limits: list[str] = []
    if cpu_seconds is not None:
        if int(cpu_seconds) <= 0:
            raise ValueError("sandbox CPU-second limit must be positive")
        limits = ["--ulimit", f"cpu={int(cpu_seconds)}:{int(cpu_seconds)}"]
    explicit_environment: list[str] = []
    for key, value in sorted((environment or {}).items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
            raise ValueError(f"invalid sandbox environment key: {key!r}")
        explicit_environment.extend(["--env", f"{key}={value}"])
    python_environment = (
        ["--env", "PYTHONPATH=/opt/aletheia"] if include_aletheia_pythonpath else []
    )
    return [
        s.sandbox_docker_command,
        "run",
        "--rm",
        "--name",
        container_name,
        "--init",
        "--stop-timeout",
        "1",
        "--network",
        "none",
        "--ipc",
        "none",
        "--memory",
        f"{memory}m",
        "--memory-swap",
        f"{memory}m",
        "--cpus",
        docker_cpu_rate,
        "--pids-limit",
        str(s.sandbox_docker_pids),
        "--ulimit",
        "nofile=64:64",
        *limits,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=512m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        *mounts,
        "--workdir",
        container_dir,
        "--env",
        "HOME=/tmp",
        *python_environment,
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "OMP_NUM_THREADS=1",
        "--env",
        "OPENBLAS_NUM_THREADS=1",
        "--env",
        "MKL_NUM_THREADS=1",
        "--env",
        "NUMEXPR_NUM_THREADS=1",
        "--env",
        "VECLIB_MAXIMUM_THREADS=1",
        *explicit_environment,
        image_id,
        *(command or ["python", f"{container_dir}/runner.py"]),
    ]


def terminate_hardened_container(name: str) -> None:
    """Best-effort cleanup for one explicitly named hardened container.

    Callers generate a fresh scoped name before launch.  The conservative name validation keeps
    this helper from becoming a general Docker target selector.
    """
    from aletheia.config import get_settings

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError("invalid hardened container name")
    try:
        subprocess.run(
            [get_settings().sandbox_docker_command, "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def run_hardened_container(
    command: list[str],
    *,
    container_name: str,
    timeout_s: float,
    image_id: str,
    trusted_terminal_receipt: Path | None = None,
) -> SandboxExecution:
    """Run Docker with bounded output and guaranteed timeout cleanup.

    A reader drains stdout continuously but retains only the tail.  This prevents
    an authored program from exhausting host memory with output while preserving
    the trusted runner's final result sentinel.

    ``trusted_terminal_receipt`` is only for a path in an evaluator-owned mount that
    candidate code cannot write. Its atomic appearance is a terminal commit from a
    one-shot trusted process, so the container is explicitly removed instead of
    depending on a possibly lost Docker client exit notification. Never use it for
    authored-program output.
    """
    from aletheia.config import get_settings

    terminal_path: Path | None = None
    if trusted_terminal_receipt is not None:
        requested = Path(trusted_terminal_receipt)
        if requested.parent.is_symlink():
            raise ValueError("trusted terminal receipt parent cannot be a symlink")
        parent = requested.parent.resolve(strict=True)
        if not parent.is_dir() or requested.name in {"", ".", ".."}:
            raise ValueError("trusted terminal receipt parent must be a real directory")
        terminal_path = parent / requested.name
        if terminal_path.exists() or terminal_path.is_symlink():
            raise ValueError("trusted terminal receipt must not exist before container launch")

    cap = max(4096, int(get_settings().sandbox_output_limit_bytes))
    monotonic_start = time.monotonic()
    tail = bytearray()
    total_bytes = 0
    lock = threading.Lock()
    proc: subprocess.Popen[bytes] | None = None
    timed_out = False
    error: str | None = None
    container_started = False
    trusted_terminal_receipt_observed = False
    cid_directory = tempfile.TemporaryDirectory(prefix="aletheia-docker-cid-")
    cidfile = Path(cid_directory.name) / "container.cid"
    try:
        docker_command = list(command)
        try:
            image_index = docker_command.index(image_id)
        except ValueError as exc:
            raise ValueError("hardened Docker command omits its immutable image id") from exc
        docker_command[image_index:image_index] = ["--cidfile", str(cidfile)]
        proc = subprocess.Popen(
            docker_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        def _drain() -> None:
            nonlocal total_bytes
            assert proc is not None and proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                with lock:
                    total_bytes += len(chunk)
                    tail.extend(chunk)
                    if len(tail) > cap:
                        del tail[: len(tail) - cap]

        reader = threading.Thread(
            target=_drain, name=f"sandbox-output-{container_name}", daemon=True
        )
        reader.start()

        def terminal_receipt_is_committed() -> bool:
            if terminal_path is None:
                return False
            try:
                metadata = terminal_path.lstat()
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("trusted terminal receipt is not a regular file")
            return True

        try:
            deadline = monotonic_start + float(timeout_s)
            while True:
                if terminal_receipt_is_committed():
                    trusted_terminal_receipt_observed = True
                    terminate_hardened_container(container_name)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=3)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(proc.args, float(timeout_s))
                try:
                    proc.wait(timeout=min(0.05, remaining))
                    trusted_terminal_receipt_observed = terminal_receipt_is_committed()
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            try:
                container_running = subprocess.run(
                    [
                        get_settings().sandbox_docker_command,
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        container_name,
                    ],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                running = (
                    container_running.returncode == 0
                    and container_running.stdout.strip().lower() == "true"
                )
            except Exception:
                running = True
            timed_out = running
            error = (
                f"hard sandbox timed out after {float(timeout_s):g}s"
                if running
                else "Docker client did not exit after its container stopped"
            )
            terminate_hardened_container(container_name)
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        reader.join(timeout=3)
    except Exception as exc:  # Docker missing/daemon unavailable/etc.
        error = str(exc)
    finally:
        try:
            container_started = bool(cidfile.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, UnicodeError):
            container_started = False
        # ``--rm`` handles normal completion.  This extra scoped cleanup proves a
        # timed-out client cannot leave its container running in the background.
        terminate_hardened_container(container_name)
        cid_directory.cleanup()
    with lock:
        output = bytes(tail).decode("utf-8", errors="replace")
    return SandboxExecution(
        returncode=(proc.returncode if proc is not None else None),
        output=output,
        image_id=image_id,
        timed_out=timed_out,
        error=error,
        container_name=container_name,
        output_total_bytes=total_bytes,
        output_truncated=total_bytes > len(tail),
        wall_time_s=max(0.0, time.monotonic() - monotonic_start),
        container_started=container_started,
        trusted_terminal_receipt_observed=trusted_terminal_receipt_observed,
    )


def _run_local_dev(workdir: Path, script_name: str, timeout_s: float) -> SandboxExecution:
    import sys

    from aletheia.coder.sandbox import resource_limits
    from aletheia.config import get_settings

    try:
        proc = subprocess.run(
            [sys.executable, str(workdir / script_name)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            stdin=subprocess.DEVNULL,
            preexec_fn=resource_limits(),
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxExecution(None, str(exc), timed_out=True, error="local_dev timeout")
    except Exception as exc:
        return SandboxExecution(None, error=str(exc))
    output = (proc.stdout or "") + (proc.stderr or "")
    output_bytes = output.encode("utf-8")
    cap = max(4096, int(get_settings().sandbox_output_limit_bytes))
    retained = output_bytes[-cap:]
    return SandboxExecution(
        proc.returncode,
        retained.decode("utf-8", errors="replace"),
        output_total_bytes=len(output_bytes),
        output_truncated=len(output_bytes) > len(retained),
    )


def execute_python_files(
    files: Mapping[str, str | bytes],
    *,
    script_name: str = "runner.py",
    timeout_s: float,
    backend: str | None = None,
    image_id: str | None = None,
) -> SandboxExecution:
    """Stage files and execute one Python script through the selected boundary."""
    from aletheia.config import get_settings

    explicit_backend = backend is not None
    selected = str(backend or get_settings().authored_code_backend)
    # Colima/Docker Desktop reliably share the workspace's /Users path, while macOS's default
    # ``/private/var/folders`` temp root may not exist inside the Linux VM.  Mount only this random
    # leaf directory—not its repository parent—so no source or .env becomes visible.
    from aletheia.paths import WORKSPACES_ROOT

    staging_root = WORKSPACES_ROOT / ".sandbox_tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aletheia-authored-", dir=staging_root) as td:
        workdir = Path(td)
        for name, value in files.items():
            path = workdir / name
            if path.parent != workdir:
                raise ValueError("sandbox staged filenames must be flat")
            path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
        if selected == "local_dev":
            if not explicit_backend and not get_settings().allow_unsafe_host_authored_code:
                return SandboxExecution(
                    None,
                    error="local_dev authored-code execution requires explicit unsafe opt-in",
                )
            return _run_local_dev(workdir, script_name, timeout_s)
        if selected != "docker":
            return SandboxExecution(None, error=f"unknown authored-code backend: {selected}")
        try:
            immutable = resolve_docker_image(image_id)
            name = f"aletheia-auth-{uuid.uuid4().hex[:20]}"
            cmd = hardened_docker_command(
                workdir,
                image_id=immutable,
                container_name=name,
                command=["python", f"/input/{script_name}"],
            )
            return run_hardened_container(
                cmd, container_name=name, timeout_s=timeout_s, image_id=immutable
            )
        except Exception as exc:
            return SandboxExecution(None, error=str(exc))


def hard_sandbox_preflight(*, freeze: bool = True) -> dict[str, str | bool | None]:
    """Prove Docker can run the configured image, optionally freezing its id in settings."""
    from aletheia.config import get_settings

    try:
        image_id = resolve_docker_image()
        result = execute_python_files(
            {"runner.py": "print('ALETHEIA_SANDBOX_READY')\n"},
            timeout_s=float(get_settings().sandbox_preflight_timeout_s),
            backend="docker",
            image_id=image_id,
        )
        ok = result.ok and "ALETHEIA_SANDBOX_READY" in result.output
        if ok and freeze:
            # All later executions resolve this immutable id rather than the mutable tag.
            get_settings().sandbox_docker_image = image_id
        return {
            "ok": ok,
            "image_id": image_id,
            "error": None if ok else (result.error or result.output[-240:] or "probe failed"),
        }
    except Exception as exc:
        return {"ok": False, "image_id": None, "error": str(exc)}


def wait_until_container_gone(name: str, timeout_s: float = 5.0) -> bool:
    """Test/diagnostic helper used to prove a timed-out sandbox cannot outlive its job."""
    from aletheia.config import get_settings

    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        proc = subprocess.run(
            [get_settings().sandbox_docker_command, "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            return True
        time.sleep(0.05)
    return False
