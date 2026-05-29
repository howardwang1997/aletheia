"""Phase 2 increment 4: platform IAM — password hashing, session lifecycle,
identity resolution policy (no open self-registration), local + OAuth + phone
provider paths, and the require_user gate over the FastAPI surface.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from aletheia.auth import session as auth_session
from aletheia.auth import users as users_mod
from aletheia.auth.passwords import hash_password, verify_password
from aletheia.auth.providers import phone as phone_provider
from aletheia.auth.providers.base import Claim
from aletheia.auth.users import authenticate_local, bootstrap_owner, resolve_login
from aletheia.config import get_settings
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import AuthSession, Identity, User


def _clear_auth_tables():
    with session_scope() as s:
        s.query(AuthSession).delete()
        s.query(Identity).delete()
        s.query(User).delete()


# --- passwords ------------------------------------------------------------
def test_password_hash_verify():
    h = hash_password("hunter2")
    assert h != "hunter2" and verify_password("hunter2", h)
    assert not verify_password("wrong", h)
    assert not verify_password("hunter2", None)
    assert not verify_password("x", "not-a-valid-hash")


# --- session lifecycle ----------------------------------------------------
def test_session_issue_validate_revoke():
    create_all()
    _clear_auth_tables()
    with session_scope() as s:
        u = User(email="s@x.com", display_name="s", role="owner")
        s.add(u)
        s.flush()
        uid = u.id

    raw = auth_session.issue(uid)
    who = auth_session.validate(raw)
    assert who and who["id"] == uid and who["role"] == "owner"

    auth_session.revoke(raw)
    assert auth_session.validate(raw) is None
    assert auth_session.validate(None) is None
    assert auth_session.validate("nonexistent-token") is None


def test_session_expiry():
    create_all()
    _clear_auth_tables()
    with session_scope() as s:
        u = User(email="exp@x.com", role="owner")
        s.add(u)
        s.flush()
        # a session that already expired
        s.add(
            AuthSession(
                user_id=u.id,
                token_hash=auth_session._hash("expired-raw"),
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
    assert auth_session.validate("expired-raw") is None


# --- identity resolution policy ------------------------------------------
def test_resolve_login_policy(monkeypatch):
    create_all()
    _clear_auth_tables()
    monkeypatch.setattr(
        users_mod, "get_settings",
        lambda: types.SimpleNamespace(allowed_logins_set={"friend@x.com"}),
    )
    # first user ever -> becomes owner
    uid1 = resolve_login(Claim(provider="github", subject="1", email="me@x.com", display_name="me"))
    with session_scope() as s:
        assert s.get(User, uid1).role == "owner"

    # same identity again -> same user (idempotent link)
    assert resolve_login(Claim(provider="github", subject="1", email="me@x.com")) == uid1

    # a new, unlinked, non-allowlisted identity -> denied
    assert resolve_login(Claim(provider="github", subject="2", email="stranger@x.com")) is None

    # an allowlisted identity -> admitted as operator
    uid3 = resolve_login(Claim(provider="feishu", subject="abc", email="friend@x.com"))
    assert uid3 is not None
    with session_scope() as s:
        assert s.get(User, uid3).role == "operator"

    # a different provider with the SAME email links to the existing user
    uid4 = resolve_login(Claim(provider="phone", subject="+100", email="me@x.com"))
    assert uid4 == uid1


def test_bootstrap_owner_and_local_auth(monkeypatch):
    create_all()
    _clear_auth_tables()
    monkeypatch.setattr(
        users_mod, "get_settings",
        lambda: types.SimpleNamespace(owner_email="boss@x.com", owner_password="pw12345"),
    )
    bootstrap_owner()
    bootstrap_owner()  # idempotent — no duplicate identity
    with session_scope() as s:
        assert s.query(Identity).filter(Identity.provider == "local").count() == 1
    assert authenticate_local("boss@x.com", "pw12345") is not None
    assert authenticate_local("boss@x.com", "nope") is None
    assert authenticate_local("ghost@x.com", "pw12345") is None


# --- providers ------------------------------------------------------------
def test_github_provider_complete(monkeypatch):
    import aletheia.auth.providers.github as gh

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            return types.SimpleNamespace(json=lambda: {"access_token": "tok"})

        def get(self, url, **kw):
            if url.endswith("/user"):
                return types.SimpleNamespace(
                    json=lambda: {"id": 42, "login": "octo", "name": "Octo", "email": "octo@x.com"}
                )
            return types.SimpleNamespace(json=lambda: [])

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.Client = lambda **kw: _Client()
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    monkeypatch.setattr(
        gh, "get_settings",
        lambda: types.SimpleNamespace(
            app_base_url="http://localhost:8000",
            github_oauth_client_id="cid",
            github_oauth_client_secret="sec",
        ),
    )
    claim = gh.GitHubOAuthProvider().complete("the-code")
    assert claim.provider == "github" and claim.subject == "42"
    assert claim.email == "octo@x.com" and claim.meta["login"] == "octo"


def test_phone_otp_request_and_verify(monkeypatch):
    phone_provider._CHALLENGES.clear()
    monkeypatch.setattr(phone_provider.secrets, "randbelow", lambda n: 123)  # code -> "000123"
    phone_provider.request_code("+66123", now=1000.0)

    assert phone_provider.verify_code("+66123", "999999", now=1001.0) is None  # wrong code
    # expired code fails (and does not consume the challenge)
    assert phone_provider.verify_code("+66123", "000123", now=1000.0 + 10_000) is None
    assert "+66123" in phone_provider._CHALLENGES
    # correct code within TTL -> a Claim, single-use
    claim = phone_provider.verify_code("+66123", "000123", now=1010.0)
    assert claim is not None and claim.provider == "phone" and claim.subject == "+66123"
    assert phone_provider.verify_code("+66123", "000123", now=1011.0) is None  # consumed


# --- API gating -----------------------------------------------------------
def test_api_requires_auth(monkeypatch):
    monkeypatch.setenv("ALETHEIA_OWNER_EMAIL", "owner@x.com")
    monkeypatch.setenv("ALETHEIA_OWNER_PASSWORD", "pw123456")
    get_settings.cache_clear()
    create_all()
    _clear_auth_tables()
    bootstrap_owner()

    from aletheia.api.main import app

    with TestClient(app) as c:
        # gated endpoints reject anonymous callers
        assert c.get("/runs").status_code == 401
        assert c.get("/auth/me").status_code == 401
        # health + providers stay open
        assert c.get("/healthz").status_code == 200
        assert c.get("/auth/providers").json()["local"] is True
        # wrong creds rejected
        assert c.post("/auth/login", json={"email": "owner@x.com", "password": "x"}).status_code == 401
        # login -> cookie -> access granted
        assert c.post("/auth/login", json={"email": "owner@x.com", "password": "pw123456"}).status_code == 200
        assert c.get("/runs").status_code == 200
        assert c.get("/auth/me").json()["role"] == "owner"
        # logout -> gated again
        c.post("/auth/logout")
        assert c.get("/runs").status_code == 401

    get_settings.cache_clear()
