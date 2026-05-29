"""Auth dependency. Resolves the current user from (in order) the session cookie,
an ``Authorization: Bearer`` header (native clients), or a ``?token=`` query param
(EventSource fallback). Raises 401 when unauthenticated."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, Request

from aletheia.auth import session as auth_session
from aletheia.config import get_settings


def _token_from(request: Request) -> str | None:
    raw = request.cookies.get(get_settings().auth_cookie_name)
    if raw:
        return raw
    authz = request.headers.get("Authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return request.query_params.get("token")


async def require_user(request: Request) -> dict[str, Any]:
    user = await asyncio.to_thread(auth_session.validate, _token_from(request))
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user
