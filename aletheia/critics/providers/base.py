"""Critic provider interface. A provider turns (instruction, content) into a
structured ``CriticResponse``. Transport (HTTP API vs Codex CLI) is the only thing
that varies between concrete providers for a given vendor."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from aletheia.config.settings import CriticConfig
from aletheia.critics.schemas import CriticResponse

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class CriticProvider(ABC):
    def __init__(self, config: CriticConfig) -> None:
        self.config = config
        self.critic_id = config.id
        self.model = config.model

    @abstractmethod
    def review(self, instruction: str, content: str) -> CriticResponse:
        """Return a structured critique for the given instruction + content."""

    @staticmethod
    def _parse(text: str) -> CriticResponse:
        """Parse model output into a CriticResponse, tolerating markdown fences."""
        text = (text or "").strip()
        try:
            return CriticResponse.model_validate_json(text)
        except Exception:
            pass
        m = _JSON_FENCE.search(text)
        if m:
            return CriticResponse.model_validate_json(m.group(1))
        # last resort: first {...} block
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            return CriticResponse.model_validate(json.loads(text[start : end + 1]))
        raise ValueError(f"could not parse critic response from: {text[:200]!r}")
