"""Claude auth resolution for the orchestrator.

Auth is a config switch (subscription token vs API key), with automatic
inheritance of an existing machine login (so on a dev box already logged into
Claude Code, no token is needed in `.env`).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

from aletheia.config import Settings


def configure_auth(settings: Settings) -> None:
    """Make the spawned Claude CLI use the chosen auth path.

    The SDK auth precedence puts ANTHROPIC_API_KEY ABOVE CLAUDE_CODE_OAUTH_TOKEN,
    so for subscription mode we clear the API key from the environment (and fall
    through to the machine login if no explicit token is set).
    """
    if settings.claude_auth_mode == "subscription":
        if settings.claude_code_oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        if settings.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key


def machine_has_claude_login() -> bool:
    """True if this machine already has a logged-in Claude Code session.

    The spawned ``claude`` CLI reuses these credentials automatically. Checks the
    macOS Keychain, the Linux credentials file, and the `oauthAccount` marker.
    """
    try:
        if platform.system() == "Darwin":
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True,
                timeout=5,
            )
            if r.returncode == 0:
                return True
        if (Path.home() / ".claude" / ".credentials.json").exists():
            return True
        cfg = Path.home() / ".claude.json"
        if cfg.exists():
            data = json.loads(cfg.read_text() or "{}")
            if data.get("oauthAccount"):
                return True
    except Exception:
        return False
    return False


def has_credentials(settings: Settings) -> bool:
    if settings.claude_auth_mode == "subscription":
        return bool(settings.claude_code_oauth_token) or machine_has_claude_login()
    return bool(settings.anthropic_api_key)
