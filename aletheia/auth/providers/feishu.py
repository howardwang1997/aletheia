"""Feishu (Lark) OAuth login.

Flow: redirect to authen/v1/index -> on callback, mint an app_access_token, then
exchange the code for the user's identity. Endpoint shapes follow Feishu's v1/v3
APIs and may need adjusting to your tenant when real credentials are configured.
"""

from __future__ import annotations

from urllib.parse import urlencode

from aletheia.auth.providers.base import Claim, OAuthProvider
from aletheia.config import get_settings

_AUTHORIZE = "https://open.feishu.cn/open-apis/authen/v1/index"
_APP_TOKEN = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
_ACCESS = "https://open.feishu.cn/open-apis/authen/v1/access_token"


class FeishuOAuthProvider(OAuthProvider):
    id = "feishu"

    def _redirect_uri(self) -> str:
        return f"{get_settings().app_base_url}/auth/feishu/callback"

    def start(self, state: str) -> str:
        s = get_settings()
        params = urlencode(
            {"app_id": s.feishu_app_id, "redirect_uri": self._redirect_uri(), "state": state}
        )
        return f"{_AUTHORIZE}?{params}"

    def complete(self, code: str) -> Claim:
        import httpx

        s = get_settings()
        with httpx.Client(timeout=15.0) as c:
            app_token = c.post(
                _APP_TOKEN,
                json={"app_id": s.feishu_app_id, "app_secret": s.feishu_app_secret},
            ).json()["app_access_token"]
            resp = c.post(
                _ACCESS,
                headers={"Authorization": f"Bearer {app_token}"},
                json={"grant_type": "authorization_code", "code": code},
            ).json()
            data = resp.get("data", resp)
        return Claim(
            provider="feishu",
            subject=str(data.get("open_id") or data.get("union_id")),
            display_name=data.get("name"),
            email=data.get("email") or data.get("enterprise_email"),
            meta={"tenant_key": data.get("tenant_key")},
        )
