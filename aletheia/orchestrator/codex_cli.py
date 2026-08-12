"""Shared safety and authentication helpers for Codex CLI subscription calls.

The orchestrator and the OpenAI critic both reuse one cached ChatGPT login.  Codex OAuth refresh
tokens rotate, so all ``codex exec`` processes are serialized through one process-wide lock.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Mapping


CODEX_LOCK = threading.Lock()


def subscription_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot silently select metered API-key authentication.

    ``CODEX_HOME`` and the rest of the environment are retained so the CLI can find and refresh its
    cached ChatGPT login.  Per-process API/access-token variables are removed deliberately.
    """
    env = dict(source if source is not None else os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    return env


def machine_has_codex_subscription(command: str = "codex") -> bool:
    """Whether ``command`` has an active *ChatGPT* login, not merely an API-key login."""
    try:
        proc = subprocess.run(
            [command, "login", "status"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            env=subscription_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    status = f"{proc.stdout}\n{proc.stderr}".lower()
    return proc.returncode == 0 and "logged in using chatgpt" in status
