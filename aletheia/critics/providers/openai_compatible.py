"""OpenAI-compatible critic providers (DeepSeek, Zhipu/GLM, …).

These vendors expose an OpenAI-shaped HTTP API behind their own ``base_url`` and key,
so one provider serves them all: the vendor is identified by ``config.id`` (which
selects the key via ``settings.vendor_key``) and ``config.base_url`` (mandatory).

Unlike the first-party OpenAI provider (which uses the Responses ``.parse`` API),
these use Chat Completions JSON mode + tolerant parsing, which every OpenAI-compatible
endpoint supports.
"""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse


class OpenAICompatibleProvider(CriticProvider):
    """Base for any vendor speaking the OpenAI API at a custom ``base_url``."""

    transport = "api"

    def review(self, instruction: str, content: str) -> CriticResponse:
        from openai import OpenAI

        settings = get_settings()
        key = settings.vendor_key(self.critic_id)
        if not key:
            raise RuntimeError(
                f"No API key for vendor '{self.critic_id}' "
                f"(set the corresponding *_API_KEY for critic transport 'api')."
            )
        base_url = settings.vendor_base_url(self.critic_id) or self.config.base_url
        if not base_url:
            raise RuntimeError(
                f"base_url is required for OpenAI-compatible vendor '{self.critic_id}' "
                f"(set it in critics.yaml or {self.critic_id.upper()}_BASE_URL)."
            )
        client = OpenAI(api_key=key, base_url=base_url)
        # JSON mode is the portable structured-output path across OpenAI-compatible
        # endpoints; the instruction already asks for the JSON schema by field.
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        return self._parse(resp.choices[0].message.content or "")


class DeepSeekAPIProvider(OpenAICompatibleProvider):
    """DeepSeek (latest), OpenAI-compatible at https://api.deepseek.com."""


class ZhipuAPIProvider(OpenAICompatibleProvider):
    """Zhipu/GLM (latest), OpenAI-compatible at the bigmodel.cn paas endpoint."""
