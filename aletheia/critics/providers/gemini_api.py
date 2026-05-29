"""Gemini critic provider via the google-genai SDK.

Gemini isn't OpenAI-compatible, so it gets its own provider. We use structured
output (``response_schema=CriticResponse``) and fall back to tolerant JSON parsing.
"""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticResponse


class GeminiAPIProvider(CriticProvider):
    transport = "api"

    def review(self, instruction: str, content: str) -> CriticResponse:
        from google import genai
        from google.genai import types

        settings = get_settings()
        key = settings.google_api_key
        if not key:
            raise RuntimeError("GOOGLE_API_KEY is not set (needed for the Gemini critic).")
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=self.model,
            contents=f"{instruction}\n\n--- CONTENT TO REVIEW ---\n{content}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CriticResponse,
                temperature=0.3,
            ),
        )
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, CriticResponse):
            return parsed
        return self._parse(getattr(resp, "text", "") or "")
