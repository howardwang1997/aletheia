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


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CONTROL_ROLES = {"owner", "operator"}


async def require_user(request: Request) -> dict[str, Any]:
    """Any authenticated user. Use for read-only endpoints."""
    user = await asyncio.to_thread(auth_session.validate, _token_from(request))
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


async def require_access(request: Request) -> dict[str, Any]:
    """Method-aware gate: any authenticated user may read (GET/HEAD/OPTIONS);
    only owner/operator may mutate. Viewers are read-only."""
    user = await require_user(request)
    if request.method not in _SAFE_METHODS and user["role"] not in _CONTROL_ROLES:
        raise HTTPException(status_code=403, detail="read-only role: this action requires operator")
    return user


async def require_owner(request: Request) -> dict[str, Any]:
    """Owner only — user/role administration."""
    user = await require_user(request)
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    return user
