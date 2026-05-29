"""Auth endpoints: local login, multi-provider OAuth (github/feishu), phone OTP,
plus session cookie management. All other routers require the resulting session."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from aletheia.api.deps import require_owner, require_user
from aletheia.auth import session as auth_session
from aletheia.auth.providers import get_oauth_provider
from aletheia.auth.providers import phone as phone_provider
from aletheia.auth.users import (
    authenticate_local,
    list_users,
    resolve_login,
    set_user_role,
)
from aletheia.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])

_OAUTH_PROVIDERS = ("github", "feishu")
_STATE_COOKIE = "aletheia_oauth_state"


def _set_session_cookie(resp: Response, raw: str) -> None:
    s = get_settings()
    resp.set_cookie(
        s.auth_cookie_name, raw,
        httponly=True, secure=s.auth_cookie_secure, samesite=s.auth_cookie_samesite,
        max_age=s.auth_session_ttl_hours * 3600, path="/",
    )


class LoginBody(BaseModel):
    email: str
    password: str


class PhoneRequestBody(BaseModel):
    phone: str


class PhoneVerifyBody(BaseModel):
    phone: str
    code: str


@router.get("/providers")
async def providers() -> dict:
    """Which login methods are enabled (drives the login UI). No auth required."""
    s = get_settings()
    return {p: s.auth_provider_enabled(p) for p in ("local", "github", "feishu", "phone")}


@router.get("/me")
async def me(user: dict = Depends(require_user)) -> dict:
    return user


# --- user/role administration (owner only) --------------------------------
class RoleBody(BaseModel):
    role: str


@router.get("/users")
async def users_list(_owner: dict = Depends(require_owner)) -> list[dict]:
    return await asyncio.to_thread(list_users)


@router.post("/users/{user_id}/role")
async def users_set_role(
    user_id: str, body: RoleBody, _owner: dict = Depends(require_owner)
) -> dict:
    try:
        ok = await asyncio.to_thread(set_user_role, user_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True, "user_id": user_id, "role": body.role}


@router.post("/login")
async def login(body: LoginBody, response: Response) -> dict:
    uid = await asyncio.to_thread(authenticate_local, body.email, body.password)
    if uid is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    raw = await asyncio.to_thread(auth_session.issue, uid)
    _set_session_cookie(response, raw)
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    raw = request.cookies.get(get_settings().auth_cookie_name)
    await asyncio.to_thread(auth_session.revoke, raw)
    response.delete_cookie(get_settings().auth_cookie_name, path="/")
    return {"ok": True}


# --- OAuth (github / feishu) ----------------------------------------------
@router.get("/{provider}/start")
async def oauth_start(provider: str) -> RedirectResponse:
    s = get_settings()
    if provider not in _OAUTH_PROVIDERS or not s.auth_provider_enabled(provider):
        raise HTTPException(status_code=404, detail="provider not enabled")
    state = secrets.token_urlsafe(16)
    url = get_oauth_provider(provider).start(state)
    resp = RedirectResponse(url)
    # bind the state to a short-lived cookie for CSRF protection on callback
    resp.set_cookie(
        _STATE_COOKIE, state, httponly=True, secure=s.auth_cookie_secure,
        samesite=s.auth_cookie_samesite, max_age=600, path="/",
    )
    return resp


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, code: str, state: str, request: Request) -> RedirectResponse:
    s = get_settings()
    if provider not in _OAUTH_PROVIDERS or not s.auth_provider_enabled(provider):
        raise HTTPException(status_code=404, detail="provider not enabled")
    if not state or state != request.cookies.get(_STATE_COOKIE):
        raise HTTPException(status_code=400, detail="invalid OAuth state")
    claim = await asyncio.to_thread(get_oauth_provider(provider).complete, code)
    uid = await asyncio.to_thread(resolve_login, claim)
    if uid is None:
        raise HTTPException(status_code=403, detail="this account is not authorized")
    raw = await asyncio.to_thread(auth_session.issue, uid)
    resp = RedirectResponse(s.frontend_base_url)
    _set_session_cookie(resp, raw)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


# --- phone OTP -------------------------------------------------------------
@router.post("/phone/request")
async def phone_request(body: PhoneRequestBody) -> dict:
    if not get_settings().auth_provider_enabled("phone"):
        raise HTTPException(status_code=404, detail="phone login not enabled")
    await asyncio.to_thread(phone_provider.request_code, body.phone)
    return {"ok": True}


@router.post("/phone/verify")
async def phone_verify(body: PhoneVerifyBody, response: Response) -> dict:
    claim = await asyncio.to_thread(phone_provider.verify_code, body.phone, body.code)
    if claim is None:
        raise HTTPException(status_code=401, detail="invalid or expired code")
    uid = await asyncio.to_thread(resolve_login, claim)
    if uid is None:
        raise HTTPException(status_code=403, detail="this number is not authorized")
    raw = await asyncio.to_thread(auth_session.issue, uid)
    _set_session_cookie(response, raw)
    return {"ok": True}
