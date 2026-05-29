"""GitHub OAuth login (distinct from the GitHub *App* used for repo IAM)."""

from __future__ import annotations

from urllib.parse import urlencode

from aletheia.auth.providers.base import Claim, OAuthProvider
from aletheia.config import get_settings

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER = "https://api.github.com/user"
_EMAILS = "https://api.github.com/user/emails"


class GitHubOAuthProvider(OAuthProvider):
    id = "github"

    def _redirect_uri(self) -> str:
        return f"{get_settings().app_base_url}/auth/github/callback"

    def start(self, state: str) -> str:
        s = get_settings()
        params = urlencode(
            {
                "client_id": s.github_oauth_client_id,
                "redirect_uri": self._redirect_uri(),
                "scope": "read:user user:email",
                "state": state,
            }
        )
        return f"{_AUTHORIZE}?{params}"

    def complete(self, code: str) -> Claim:
        import httpx

        s = get_settings()
        with httpx.Client(timeout=15.0) as c:
            tok = c.post(
                _TOKEN,
                headers={"Accept": "application/json"},
                data={
                    "client_id": s.github_oauth_client_id,
                    "client_secret": s.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": self._redirect_uri(),
                },
            ).json()
            access = tok["access_token"]
            auth = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
            u = c.get(_USER, headers=auth).json()
            email = u.get("email")
            if not email:  # primary email may be private
                emails = c.get(_EMAILS, headers=auth).json()
                primary = next((e for e in emails if e.get("primary")), None)
                email = (primary or (emails[0] if emails else {})).get("email")
        return Claim(
            provider="github",
            subject=str(u["id"]),
            display_name=u.get("name") or u.get("login"),
            email=email,
            meta={"login": u.get("login")},
        )
