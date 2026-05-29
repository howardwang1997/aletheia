"""OpenAI provider via the HTTP API (billed per token through OPENAI_API_KEY).
Fallback transport when the Codex/Coding-Plan path isn't configured."""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse


class OpenAIAPIProvider(CriticProvider):
    transport = "api"

    def review(self, instruction: str, content: str) -> CriticResponse:
        from openai import OpenAI

        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (needed for critic transport 'api').")
        client = OpenAI(api_key=settings.openai_api_key, base_url=self.config.base_url or None)
        # Responses API structured-output parse -> guaranteed schema conformance.
        resp = client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": content},
            ],
            text_format=CriticResponse,
        )
        parsed = resp.output_parsed
        if parsed is None:  # pragma: no cover - defensive
            return self._parse(resp.output_text)
        return parsed
