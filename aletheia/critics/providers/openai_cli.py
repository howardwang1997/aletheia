"""OpenAI provider via the Codex CLI — runs GPT-5.5 on the user's ChatGPT/Coding
Plan login (no API key), mirroring how the Claude side uses its Coding Plan.

Key invocation choices:
  * ``--output-schema`` forces the final message to conform to CriticResponse;
  * ``--ignore-user-config`` disables config.toml (incl. MCP servers) — without it,
    Codex silently drops ``--output-schema`` when tools/MCP are active — while auth
    still resolves from CODEX_HOME (auth.json), so the subscription login is used;
  * ``--ephemeral`` + ``--sandbox read-only`` keep the call stateless and side-effect free.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse
from aletheia.orchestrator.codex_cli import CODEX_LOCK, subscription_environment

# Serialize all `codex exec` calls process-wide. The critic panel fans out concurrently
# (e.g. one model running both an adversarial AND a supportive stance), but every codex
# process shares ONE ~/.codex/auth.json holding a SINGLE-USE, rotating OAuth refresh
# token. When the access token has expired, two concurrent processes both try to refresh
# with the same refresh token; the server honors the first and invalidates it, failing the
# loser with "refresh token was already used" — and a stale write can burn the token on
# disk. Serializing means the first call refreshes once; the rest reuse the fresh token.
_CODEX_LOCK = CODEX_LOCK

# JSON Schema keywords OpenAI's strict structured-output mode rejects.
_UNSUPPORTED = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "default",
)


def strict_schema(model: type) -> dict:
    """Pydantic schema -> OpenAI strict-compatible schema: every object gets
    ``additionalProperties: false`` + all properties required, and unsupported
    validation keywords are stripped."""

    def walk(node):
        if isinstance(node, dict):
            for k in _UNSUPPORTED:
                node.pop(k, None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                props = node.get("properties")
                if isinstance(props, dict):
                    node["required"] = list(props.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        return node

    return walk(model.model_json_schema())


class OpenAICodexProvider(CriticProvider):
    transport = "cli"

    def review(self, instruction: str, content: str) -> CriticResponse:
        schema = strict_schema(CriticResponse)
        prompt = (
            f"{instruction}\n\n--- CONTENT TO REVIEW ---\n{content}\n\n"
            "Respond ONLY with a JSON object conforming to the provided schema."
        )
        # one retry: under the lock the first call refreshes the OAuth token, so a
        # transient refresh-race loser (or a just-rotated token) succeeds on retry.
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return self._exec_codex(schema, prompt)
            except RuntimeError as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.5)
        raise last_exc  # type: ignore[misc]

    def _exec_codex(self, schema: dict, prompt: str) -> CriticResponse:
        settings = get_settings()
        codex = settings.codex_command
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            schema_path = tdp / "schema.json"
            out_path = tdp / "out.json"
            schema_path.write_text(json.dumps(schema))
            cmd = [
                codex,
                "exec",
                "--ignore-user-config",  # no MCP/config -> --output-schema is honored
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "-c",
                'forced_login_method="chatgpt"',
                "-C",
                str(tdp),
                "--output-schema",
                str(schema_path),
                "-o",
                str(out_path),
            ]
            if self.model:
                cmd += ["-m", self.model]
            cmd.append(prompt)
            # stdin=DEVNULL is essential: `codex exec` otherwise blocks reading stdin
            # for EOF when stdin is a pipe (e.g. under uvicorn / a background task).
            # Serialized: concurrent codex processes race the single-use OAuth refresh
            # token (see _CODEX_LOCK above).
            with _CODEX_LOCK:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=settings.codex_timeout_s,
                    env=subscription_environment(),
                )
            if out_path.exists() and out_path.read_text().strip():
                return self._parse(out_path.read_text())
            # fall back to stdout, else surface the error
            if proc.stdout.strip():
                return self._parse(proc.stdout)
            raise RuntimeError(
                f"codex exec produced no output (rc={proc.returncode}): "
                f"{(proc.stderr or '')[-500:]}"
            )
