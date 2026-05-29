"""Phase 2 step 1: cross-vendor critic providers — registry resolution and the
OpenAI-compatible key/base_url wiring (mocked; no network)."""

from __future__ import annotations

import sys
import types

import pytest

from aletheia.config.settings import CriticConfig, CriticsConfig
from aletheia.critics.gateway import _PROVIDERS, CriticGateway
from aletheia.critics.providers.gemini_api import GeminiAPIProvider
from aletheia.critics.providers.openai_api import OpenAIAPIProvider
from aletheia.critics.providers.openai_compatible import (
    DeepSeekAPIProvider,
    ZhipuAPIProvider,
)


def test_registry_resolves_every_vendor():
    assert _PROVIDERS["openai"]["api"] is OpenAIAPIProvider
    assert _PROVIDERS["gemini"]["api"] is GeminiAPIProvider
    assert _PROVIDERS["deepseek"]["api"] is DeepSeekAPIProvider
    assert _PROVIDERS["zhipu"]["api"] is ZhipuAPIProvider


def test_gateway_instantiates_distinct_vendor_models():
    cfg = CriticsConfig(
        panel=[
            CriticConfig(id="openai", transport="api", model="gpt-5.5"),
            CriticConfig(id="gemini", transport="api", model="gemini-latest"),
            CriticConfig(
                id="deepseek", transport="api", model="deepseek-chat",
                base_url="https://api.deepseek.com",
            ),
        ]
    )
    provs = CriticGateway(cfg)._providers()
    assert [p.critic_id for p in provs] == ["openai", "gemini", "deepseek"]
    assert isinstance(provs[2], DeepSeekAPIProvider)


def test_openai_compatible_requires_key(monkeypatch):
    import aletheia.critics.providers.openai_compatible as mod

    fake = types.SimpleNamespace(vendor_key=lambda _id: None)
    monkeypatch.setattr(mod, "get_settings", lambda: fake)
    prov = DeepSeekAPIProvider(
        CriticConfig(id="deepseek", model="deepseek-chat", base_url="https://api.deepseek.com")
    )
    with pytest.raises(RuntimeError, match="No API key"):
        prov.review("instruction", "content")


def test_openai_compatible_builds_client_with_key_and_base_url(monkeypatch):
    import aletheia.critics.providers.openai_compatible as mod

    seen = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            msg = types.SimpleNamespace(
                content='{"verdict":"approve","confidence":0.8,"summary":"ok","findings":[]}'
            )
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class _FakeClient:
        def __init__(self, api_key, base_url):
            seen["api_key"], seen["base_url"] = api_key, base_url
            self.chat = types.SimpleNamespace(completions=_FakeCompletions())

    # inject a fake `openai` module so `from openai import OpenAI` resolves
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    fake_settings = types.SimpleNamespace(vendor_key=lambda _id: "sk-zhipu")
    monkeypatch.setattr(mod, "get_settings", lambda: fake_settings)

    prov = ZhipuAPIProvider(
        CriticConfig(
            id="zhipu", model="glm-latest",
            base_url="https://open.bigmodel.cn/api/paas/v4",
        )
    )
    resp = prov.review("instr", "content")
    assert resp.verdict == "approve"
    assert seen["api_key"] == "sk-zhipu"
    assert seen["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert seen["model"] == "glm-latest"
    assert seen["response_format"] == {"type": "json_object"}


def test_gemini_requires_key(monkeypatch):
    import aletheia.critics.providers.gemini_api as mod

    monkeypatch.setattr(mod, "get_settings", lambda: types.SimpleNamespace(google_api_key=None))
    prov = GeminiAPIProvider(CriticConfig(id="gemini", model="gemini-latest"))
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        prov.review("instruction", "content")
