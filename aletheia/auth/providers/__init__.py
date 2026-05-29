"""Pluggable login providers. OAuth providers (github/feishu) share the
``start``/``complete`` shape; local password and phone OTP are handled directly
in ``aletheia.auth.users`` / the auth router."""

from __future__ import annotations

from aletheia.auth.providers.base import Claim, OAuthProvider
from aletheia.auth.providers.feishu import FeishuOAuthProvider
from aletheia.auth.providers.github import GitHubOAuthProvider

_OAUTH: dict[str, type[OAuthProvider]] = {
    "github": GitHubOAuthProvider,
    "feishu": FeishuOAuthProvider,
}


def get_oauth_provider(provider_id: str) -> OAuthProvider:
    cls = _OAUTH.get(provider_id)
    if cls is None:
        raise KeyError(f"unknown OAuth provider: {provider_id}")
    return cls()


__all__ = ["Claim", "OAuthProvider", "get_oauth_provider"]
