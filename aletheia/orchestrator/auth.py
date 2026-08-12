"""Provider-aware auth resolution for the research orchestrator.

Auth is a provider-specific switch (subscription login vs API key), with automatic inheritance of
existing Claude Code and Codex CLI machine logins.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

from aletheia.config import Settings
from aletheia.orchestrator.codex_cli import machine_has_codex_subscription


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


def has_credentials(settings: Settings, provider: str | None = None) -> bool:
    selected = provider or settings.orchestrator_provider
    if selected == "openai":
        if settings.openai_auth_mode == "subscription":
            return machine_has_codex_subscription(settings.codex_command)
        return bool(settings.openai_api_key)
    if settings.claude_auth_mode == "subscription":
        return bool(settings.claude_code_oauth_token) or machine_has_claude_login()
    return bool(settings.anthropic_api_key)
