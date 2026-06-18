"""OpenAI-compatible critic providers (DeepSeek, Zhipu/GLM, …).

These vendors expose an OpenAI-shaped HTTP API behind their own ``base_url`` and key,
so one provider serves them all: the vendor is identified by ``config.id`` (which
selects the key via ``settings.vendor_key``) and ``config.base_url`` (mandatory).

Unlike the first-party OpenAI provider (which uses the Responses ``.parse`` API),
these use Chat Completions JSON mode + tolerant parsing, which every OpenAI-compatible
endpoint supports.
"""

from __future__ import annotations

import threading
import time

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse

# Per-vendor THROTTLE: a subscription coding plan (e.g. GLM) is slow AND rate-limits
# bursts, while the panel fans out concurrently (≥2 stances) over several rounds. We
# serialize each vendor's calls (one in flight at a time, process-wide) and space them
# out by a minimum interval, so we never trip the vendor's rate/concurrency limit.
_VENDOR_GATES: dict[str, threading.Lock] = {}
_VENDOR_LAST: dict[str, float] = {}
_GATES_GUARD = threading.Lock()


def _vendor_gate(vendor_id: str) -> threading.Lock:
    with _GATES_GUARD:
        return _VENDOR_GATES.setdefault(vendor_id, threading.Lock())


def _is_quota_exhausted(exc: Exception) -> bool:
    """A vendor's quota/balance is used up for this window (e.g. GLM Coding Plan 429
    code 1113 余额不足/无可用资源包). Retrying is futile and just burns the rate window —
    fail fast and let the OTHER vendor carry the round."""
    code = str(getattr(exc, "code", "") or "")
    blob = f"{code} {getattr(exc, 'message', '') or ''} {exc}"
    return "1113" in blob or "余额不足" in blob or "无可用资源包" in blob


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
        # throttle this vendor (serialize + min-interval), then run with backoff retry.
        gate = _vendor_gate(self.critic_id)
        min_interval = float(settings.critic_vendor_min_interval_s)
        with gate:
            wait = min_interval - (time.monotonic() - _VENDOR_LAST.get(self.critic_id, 0.0))
            if wait > 0:
                time.sleep(wait)
            try:
                return self._complete(client, instruction, content)
            finally:
                _VENDOR_LAST[self.critic_id] = time.monotonic()

    def _complete(self, client, instruction: str, content: str) -> CriticResponse:
        # JSON mode is the portable structured-output path across OpenAI-compatible
        # endpoints; the instruction already asks for the JSON schema by field. Retry
        # transient rate-limit / connection / server errors with backoff — EXCEPT a
        # quota-exhausted 429 (e.g. GLM 1113), which won't recover in-window, so fail fast.
        from openai import APIConnectionError, APIStatusError, RateLimitError

        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(3):
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
            except RateLimitError as exc:
                if _is_quota_exhausted(exc):  # 1113: futile to retry, don't burn the window
                    raise
                last_exc = exc
            except APIConnectionError as exc:
                # APITimeoutError subclasses this, so it covers BOTH timeouts and plain connection
                # errors (ConnectionRefused / "Connection error"). On a flaky direct link a momentary
                # blip must be retried, not drop the vendor below the audit's cross-vendor floor.
                last_exc = exc
            except APIStatusError as exc:
                if exc.status_code < 500:  # non-transient client error -> don't retry
                    raise
                last_exc = exc
            if attempt < 2:
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
        raise last_exc  # type: ignore[misc]


class DeepSeekAPIProvider(OpenAICompatibleProvider):
    """DeepSeek (latest), OpenAI-compatible at https://api.deepseek.com."""


class ZhipuAPIProvider(OpenAICompatibleProvider):
    """Zhipu/GLM via the GLM Coding Plan, OpenAI-compatible at the bigmodel.cn
    ``/api/coding/paas/v4`` endpoint (the Coding Plan subscription is NOT served by the
    standard ``/api/paas/v4``). The key is read from OpenCode's config (see settings)."""


class GrokAPIProvider(OpenAICompatibleProvider):
    """xAI Grok, OpenAI-compatible at ``https://api.x.ai/v1``. The key is read from the
    sibling sciminer project's .env (``GROK_API_KEY``); see settings."""
