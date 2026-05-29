"""Provider contract. A ``Claim`` is the normalized identity a provider returns
after a successful login; ``users.resolve_login`` maps it to a platform user."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Claim:
    provider: str
    subject: str  # provider-scoped stable id
    display_name: str | None = None
    email: str | None = None
    meta: dict[str, Any] | None = None


class OAuthProvider(ABC):
    id: str = "base"

    @abstractmethod
    def start(self, state: str) -> str:
        """Return the provider authorize URL to redirect the user to."""

    @abstractmethod
    def complete(self, code: str) -> Claim:
        """Exchange the callback ``code`` for a normalized identity Claim."""
