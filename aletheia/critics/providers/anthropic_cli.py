"""Anthropic provider via the Claude CLI — runs Claude on the machine's Claude Code /
Coding Plan login (no API key), the SAME subscription the orchestrator uses, so it is the
most reliable critic credential available. Mirrors the OpenAI Codex CLI provider.

A note on independence: the orchestrator authors with Claude Opus, so the critic runs a
DIFFERENT Claude (Sonnet) to reduce same-model self-review correlation; the cross-VENDOR
panel (GPT/GLM) remains the primary independent check.

Invocation: ``claude -p <content> --output-format json --append-system-prompt <instruction>
--strict-mcp-config`` — headless, no project MCP/tools, returns a JSON envelope whose
``result`` field holds the model's review (which we parse into a CriticResponse).
"""

from __future__ import annotations

import json
import subprocess
import threading

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse

# Serialize Claude CLI calls process-wide: concurrent stances would spawn parallel
# `claude` processes that share one machine login (and can contend on token refresh),
# and Claude reviews are slow — one at a time keeps it reliable.
_CLAUDE_LOCK = threading.Lock()


class ClaudeCLIProvider(CriticProvider):
    transport = "cli"

    def review(self, instruction: str, content: str) -> CriticResponse:
        settings = get_settings()
        prompt = (
            f"{content}\n\n"
            "Respond ONLY with a JSON object: verdict (approve | approve_with_changes | "
            "reject), confidence (0..1), summary, findings (each: severity "
            "[blocker|major|minor|nit|praise], category "
            "[validity|leakage|stats|baseline|reproducibility|novelty|scope|cost], claim, "
            "evidence, suggestion)."
        )
        cmd = [
            settings.claude_command,
            "-p", prompt,
            "--output-format", "json",
            "--append-system-prompt", instruction,
            "--strict-mcp-config",  # no project MCP servers — a clean, stateless review
        ]
        if self.model:
            cmd += ["--model", self.model]
        # stdin=DEVNULL so `claude -p` never blocks reading stdin under a background task.
        with _CLAUDE_LOCK:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=settings.claude_cli_timeout_s,
            )
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError(
                f"claude -p produced no output (rc={proc.returncode}): "
                f"{(proc.stderr or '')[-500:]}"
            )
        envelope = json.loads(out)
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise RuntimeError(f"claude -p error: {str(envelope.get('result') or envelope)[:300]}")
        return self._parse(str(envelope.get("result", "")))  # _parse tolerates ```json fences
