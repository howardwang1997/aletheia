"""Critic-panel reliability: (1) GLM (Coding Plan) wired as a 2nd vendor with its key
read from OpenCode's config + the coding-plan endpoint; (2) Codex calls serialized +
retried so concurrent stances don't race the single-use OAuth refresh token. Offline."""

from __future__ import annotations

import json

import aletheia.config.settings as st
from aletheia.config.settings import CriticConfig, _opencode_glm_key


# --- GLM key is read from OpenCode's config ------------------------------------
def test_opencode_glm_key_read(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "provider": {"zhipuai-coding-plan": {"options": {"apiKey": "glm-key-123"}}}
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _opencode_glm_key.cache_clear()
    assert _opencode_glm_key() == "glm-key-123"


def test_opencode_glm_key_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no opencode.json here
    _opencode_glm_key.cache_clear()
    assert _opencode_glm_key() is None


def test_vendor_key_zhipu_falls_back_to_opencode(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(
        {"provider": {"zhipuai-coding-plan": {"options": {"apiKey": "glm-key-xyz"}}}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _opencode_glm_key.cache_clear()
    s = st.Settings(_env_file=None)
    monkeypatch.setattr(s, "zhipu_api_key", None, raising=False)
    assert s.vendor_key("zhipu") == "glm-key-xyz"


# --- GLM is an active critic on the CODING-PLAN endpoint ------------------------
def test_glm_critic_active_on_coding_endpoint():
    from aletheia.config import get_settings

    zhipu = next((c for c in get_settings().critics.active if c.id == "zhipu"), None)
    assert zhipu is not None, "GLM (zhipu) must be an active critic"
    assert "/coding/" in (zhipu.base_url or ""), "must use the GLM Coding Plan endpoint"
    assert zhipu.model == "glm-4.6"


# --- Codex provider: serialized + retried --------------------------------------
def test_codex_serialized_and_retries(monkeypatch):
    from aletheia.critics.providers import openai_cli
    from aletheia.critics.providers.openai_cli import OpenAICodexProvider
    from aletheia.critics.schemas import CriticResponse

    assert isinstance(openai_cli._CODEX_LOCK, type(__import__("threading").Lock()))

    p = OpenAICodexProvider(CriticConfig(id="openai", transport="cli", model="gpt-5.5"))
    calls = {"n": 0}

    def flaky_exec(schema, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("codex exec produced no output (refresh token already used)")
        return CriticResponse(verdict="approve", confidence=0.9, summary="ok", findings=[])

    monkeypatch.setattr(p, "_exec_codex", flaky_exec)
    monkeypatch.setattr(openai_cli.time, "sleep", lambda *_: None)  # no real delay
    r = p.review("instruction", "content")
    assert r.verdict == "approve" and calls["n"] == 2  # retried once after the transient fail
