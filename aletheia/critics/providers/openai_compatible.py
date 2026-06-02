"""OpenAI-compatible critic providers (DeepSeek, Zhipu/GLM, …).

These vendors expose an OpenAI-shaped HTTP API behind their own ``base_url`` and key,
so one provider serves them all: the vendor is identified by ``config.id`` (which
selects the key via ``settings.vendor_key``) and ``config.base_url`` (mandatory).

Unlike the first-party OpenAI provider (which uses the Responses ``.parse`` API),
these use Chat Completions JSON mode + tolerant parsing, which every OpenAI-compatible
endpoint supports.
"""

from __future__ import annotations

import time

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
        client = OpenAI(api_key=key, base_url=base_url, max_retries=0)
        # JSON mode is the portable structured-output path across OpenAI-compatible
        # endpoints; the instruction already asks for the JSON schema by field.
        # Retry transient rate-limit / server errors with backoff — subscription coding
        # plans (e.g. GLM) rate-limit bursts (HTTP 429 code 1113), and the panel fans out
        # concurrently; without this a transient 429 would silently drop this reviewer.
        from openai import APIStatusError, APITimeoutError, RateLimitError

        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
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
            except (RateLimitError, APITimeoutError) as exc:
                last_exc = exc
            except APIStatusError as exc:
                if exc.status_code < 500:  # non-transient client error -> don't retry
                    raise
                last_exc = exc
            if attempt < 3:
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
        raise last_exc  # type: ignore[misc]


class DeepSeekAPIProvider(OpenAICompatibleProvider):
    """DeepSeek (latest), OpenAI-compatible at https://api.deepseek.com."""


class ZhipuAPIProvider(OpenAICompatibleProvider):
    """Zhipu/GLM via the GLM Coding Plan, OpenAI-compatible at the bigmodel.cn
    ``/api/coding/paas/v4`` endpoint (the Coding Plan subscription is NOT served by the
    standard ``/api/paas/v4``). The key is read from OpenCode's config (see settings)."""
