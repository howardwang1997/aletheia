#!/usr/bin/env python3
"""Stop and restart the ARL-2 campaign driver between its role cycles.

The driver's only operator gate is the process itself: roles run solely under
its wake loop (aletheia/arl2_runtime.py:931-971), so pausing the window means
stopping the driver and restarting it on the SAME deployment manifest.  This
helper does both without depending on crash recovery mid-window:

    # commissioned once (by hand or by author-arl2-deployments.py):
    # configs/arl2-driver-invocation.json =
    #   {"user": "arl2drv",
    #    "env": {"ALETHEIA_DATABASE_URL": "...", "PYTHONDONTWRITEBYTECODE": "1"},
    #    "argv": ["/opt/aletheia/python/bin/python",
    #             "/opt/aletheia/release-<sha>-arl2dry/scripts/run-arl2-question-campaign.py",
    #             "--deployment-manifest", "...", "--deployment-manifest-sha256", "...",
    #             "--apply", "--acknowledge", "RUN_ARL2_QUESTION_CAMPAIGN"],
    #    "log_path": "/opt/aletheia/arl2-dryrun/spool/driver.log"}

    sudo /opt/aletheia/python/bin/python scripts/arl2-driver-control.py \
        --invocation /opt/aletheia/arl2-dryrun/configs/arl2-driver-invocation.json \
        --pid-file /opt/aletheia/arl2-dryrun/spool/driver.pid \
        {start|stop|status}

stop: SIGTERM the driver, wait for it to exit, then wait (bounded) for any
role cycle still running as its orphaned child; SIGTERM and finally SIGKILL
a role child that outlives the bound.  An interrupted role cycle is
recoverable (durable queue, research_controller_runtime.py:512-514) but
waiting for it first avoids exercising that path.  start refuses if a driver
is already running.  Runs as root on the box; Linux only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_DRIVER_MARKER = "run-arl2-question-campaign.py"
_ROLE_MARKER = "run_research_controller_runtime.py"
_EXIT_GRACE_SECONDS = 30.0
_ROLE_GRACE_SECONDS = 180.0


def _fail(message: str) -> None:
    raise SystemExit(f"arl2-driver-control: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--invocation",
        required=True,
        help="path to the commissioned driver-invocation JSON",
    )
    parser.add_argument(
        "--pid-file",
        required=True,
        help="path where the running driver pid is recorded",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=("start", "stop", "status"),
        help="start the driver, stop it between cycles, or report status",
    )
    return parser


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _uid_of(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/status").read_text().split("Uid:\t")[1].split()[0])
    except (OSError, IndexError):
        return None


def _matches(pid: int, marker: str, needles: list[str]) -> bool:
    parts = _cmdline(pid)
    return marker in parts and all(needle in parts for needle in needles)


def _scan(marker: str, needles: list[str]) -> list[int]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        if _matches(pid, marker, needles):
            found.append(pid)
    return sorted(found)


def _children_of(pid: int) -> list[int]:
    children = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat_fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)
            ppid = int(stat_fields[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid == pid:
            children.append(int(entry))
    return sorted(children)


def _wait_gone(pids: list[int], grace: float) -> list[int]:
    deadline = time.monotonic() + grace
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if Path(f"/proc/{pid}").exists()]
        if remaining:
            time.sleep(0.5)
    return [pid for pid in remaining if Path(f"/proc/{pid}").exists()]


def _load_invocation(path: Path) -> dict:
    invocation = json.loads(path.read_text())
    for field in ("user", "env", "argv", "log_path"):
        if field not in invocation:
            _fail(f"invocation file is missing {field!r}")
    if _DRIVER_MARKER not in invocation["argv"]:
        _fail(f"invocation argv does not name {_DRIVER_MARKER}")
    return invocation


def _needles(invocation: dict) -> list[str]:
    argv = invocation["argv"]
    needles = []
    for flag in ("--deployment-manifest", "--deployment-manifest-sha256"):
        if flag in argv:
            needles.append(argv[argv.index(flag) + 1])
    return needles


def _do_status(invocation: dict, pid_file: Path) -> int:
    drivers = _scan(_DRIVER_MARKER, _needles(invocation))
    roles = _scan(_ROLE_MARKER, [])
    lines = [f"drivers running: {drivers or ['none']}", f"role cycles running: {roles or ['none']}"]
    if pid_file.exists():
        lines.append(f"pid-file: {pid_file.read_text().strip()}")
    for pid in drivers:
        lines.append(f"driver {pid} argv: {' '.join(_cmdline(pid))}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _do_start(invocation: dict, pid_file: Path) -> int:
    existing = _scan(_DRIVER_MARKER, _needles(invocation))
    if existing:
        _fail(f"driver already running as pid {existing}; stop it first")
    log_path = Path(invocation["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab", 0)
    # sudo's env_reset drops inherited variables, so the commissioned
    # environment is injected via /usr/bin/env inside the sudo boundary.
    environment = dict(os.environ)
    argv = (
        ["/usr/bin/sudo", "-u", invocation["user"], "--", "/usr/bin/env"]
        + [f"{key}={value}" for key, value in sorted(invocation["env"].items())]
        + list(invocation["argv"])
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    time.sleep(2.0)
    if process.poll() is not None:
        _fail(f"driver exited immediately with code {process.returncode}; see {log_path}")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{process.pid}\n")
    sys.stdout.write(f"driver started as sudo pid {process.pid}; log {log_path}\n")
    return 0


def _do_stop(invocation: dict, pid_file: Path) -> int:
    drivers = _scan(_DRIVER_MARKER, _needles(invocation))
    if not drivers:
        sys.stdout.write("no driver running\n")
        pid_file.unlink(missing_ok=True)
        return 0
    for pid in drivers:
        roles = [
            child
            for child in _children_of(pid)
            if _matches(child, _ROLE_MARKER, [])
        ]
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        survivors = _wait_gone([pid], _EXIT_GRACE_SECONDS)
        for alive in survivors:
            os.kill(alive, signal.SIGKILL)
        _wait_gone(survivors, _EXIT_GRACE_SECONDS)
        # A role cycle orphaned by the driver's death finishes on its own;
        # give it the bounded grace before escalating.
        orphans = [child for child in roles if Path(f"/proc/{child}").exists()]
        late = _wait_gone(orphans, _ROLE_GRACE_SECONDS)
        for alive in late:
            os.kill(alive, signal.SIGTERM)
        late = _wait_gone(late, _EXIT_GRACE_SECONDS)
        for alive in late:
            os.kill(alive, signal.SIGKILL)
        _wait_gone(late, _EXIT_GRACE_SECONDS)
        sys.stdout.write(
            f"driver {pid} stopped"
            + (f"; role cycles {roles} awaited" if roles else "")
            + "\n"
        )
    pid_file.unlink(missing_ok=True)
    if _scan(_DRIVER_MARKER, _needles(invocation)):
        _fail("a driver process survived the stop sequence")
    return 0


def main() -> int:
    if not sys.platform.startswith("linux"):
        _fail("this helper runs on the box (Linux) only")
    args = _parser().parse_args()
    invocation = _load_invocation(Path(args.invocation).resolve(strict=True))
    pid_file = Path(args.pid_file)
    if args.action == "start":
        return _do_start(invocation, pid_file)
    if args.action == "stop":
        return _do_stop(invocation, pid_file)
    return _do_status(invocation, pid_file)


if __name__ == "__main__":
    raise SystemExit(main())
