"""Critic-panel reliability: (1) GLM (Coding Plan) wired as a 2nd vendor with its key
read from OpenCode's config + the coding-plan endpoint; (2) Codex calls serialized +
retried so concurrent stances don't race the single-use OAuth refresh token. Offline."""

from __future__ import annotations

import json
import sys
import types

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
    assert zhipu.model == "glm-5.1"


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


# --- Claude critic via the CLI (machine login, no key) -------------------------
def test_claude_cli_provider_registered_and_credentialed():
    from aletheia.critics.gateway import _PROVIDERS

    from aletheia.critics.providers.anthropic_cli import ClaudeCLIProvider
    assert _PROVIDERS["anthropic"]["cli"] is ClaudeCLIProvider
    # an enabled `anthropic` CLI critic is on the panel
    from aletheia.config import get_settings
    anthropic = next((c for c in get_settings().critics.active if c.id == "anthropic"), None)
    assert anthropic is not None and anthropic.transport == "cli"
    assert anthropic.model == "claude-opus-4-8"


def test_claude_cli_parses_json_envelope(monkeypatch):
    import types

    from aletheia.config.settings import CriticConfig
    from aletheia.critics.providers import anthropic_cli
    from aletheia.critics.providers.anthropic_cli import ClaudeCLIProvider

    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": '```json\n{"verdict":"reject","confidence":0.8,"summary":"weak baselines",'
                  '"findings":[]}\n```',
    })
    monkeypatch.setattr(anthropic_cli.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=envelope, stderr="", returncode=0))
    p = ClaudeCLIProvider(CriticConfig(id="anthropic", transport="cli", model="claude-sonnet-4-6"))
    r = p.review("instruction", "content")
    assert r.verdict == "reject" and r.confidence == 0.8


def test_claude_cli_raises_on_error_envelope(monkeypatch):
    import types

    from aletheia.config.settings import CriticConfig
    from aletheia.critics.providers import anthropic_cli
    from aletheia.critics.providers.anthropic_cli import ClaudeCLIProvider

    bad = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "boom"})
    monkeypatch.setattr(anthropic_cli.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=bad, stderr="", returncode=0))
    p = ClaudeCLIProvider(CriticConfig(id="anthropic", transport="cli", model="claude-sonnet-4-6"))
    try:
        p.review("i", "c")
        raise AssertionError("expected an error envelope to raise")
    except RuntimeError:
        pass


# --- GLM throttle: serialize per vendor, fast-fail on quota (1113) --------------
def test_is_quota_exhausted_detects_1113():
    from aletheia.critics.providers.openai_compatible import _is_quota_exhausted

    e1 = type("E", (Exception,), {"code": "1113"})()
    assert _is_quota_exhausted(e1) is True
    assert _is_quota_exhausted(Exception("Error code: 429 - 余额不足或无可用资源包")) is True
    assert _is_quota_exhausted(Exception("Error code: 429 - too many requests")) is False


def test_vendor_gate_is_stable_per_vendor():
    from aletheia.critics.providers.openai_compatible import _vendor_gate

    assert _vendor_gate("zhipu") is _vendor_gate("zhipu")
    assert _vendor_gate("zhipu") is not _vendor_gate("deepseek")


def _fake_openai(create_fn):
    m = types.ModuleType("openai")
    m.RateLimitError = type("RateLimitError", (Exception,), {})
    m.APITimeoutError = type("APITimeoutError", (Exception,), {})
    m.APIStatusError = type("APIStatusError", (Exception,), {"status_code": 500})
    counter = {"n": 0}

    class _Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **kw: create_fn(counter, m))
            )

    m.OpenAI = _Client
    return m, counter


def test_glm_quota_1113_fails_fast_no_retry(monkeypatch):
    from aletheia.critics.providers.openai_compatible import ZhipuAPIProvider

    def create(counter, m):
        counter["n"] += 1
        exc = m.RateLimitError("Error code: 429 - 余额不足")
        exc.code = "1113"
        raise exc

    fake, counter = _fake_openai(create)
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setattr("aletheia.critics.providers.openai_compatible.get_settings",
                        lambda: types.SimpleNamespace(vendor_key=lambda _: "k",
                                                      vendor_base_url=lambda _: "http://x",
                                                      critic_vendor_min_interval_s=0.0))
    prov = ZhipuAPIProvider(CriticConfig(id="zhipu", model="glm-4.6", base_url="http://x"))
    try:
        prov.review("i", "c")
        raise AssertionError("expected the 1113 error to propagate")
    except Exception:
        pass
    assert counter["n"] == 1  # fast-fail: NOT retried (would have burned the quota window)


def test_glm_transient_429_is_retried(monkeypatch):
    from aletheia.critics.providers.openai_compatible import ZhipuAPIProvider

    def create(counter, m):
        counter["n"] += 1
        raise m.RateLimitError("Error code: 429 - rate limited, slow down")  # not 1113

    fake, counter = _fake_openai(create)
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setattr("aletheia.critics.providers.openai_compatible.time.sleep", lambda *_: None)
    monkeypatch.setattr("aletheia.critics.providers.openai_compatible.get_settings",
                        lambda: types.SimpleNamespace(vendor_key=lambda _: "k",
                                                      vendor_base_url=lambda _: "http://x",
                                                      critic_vendor_min_interval_s=0.0))
    prov = ZhipuAPIProvider(CriticConfig(id="zhipu", model="glm-4.6", base_url="http://x"))
    try:
        prov.review("i", "c")
    except Exception:
        pass
    assert counter["n"] == 3  # transient 429 retried up to 3 attempts
