"""Phase 2 increment 5: role-based access — viewer is read-only, owner/operator
may mutate, owner-only user/role administration, and the last-owner lockout guard.
Exercises the real require_access/require_owner gates via TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aletheia.api.main import app
from aletheia.auth.passwords import hash_password
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import AuthSession, Identity, User

_PW = "pw123456"


def _clear():
    with session_scope() as s:
        s.query(AuthSession).delete()
        s.query(Identity).delete()
        s.query(User).delete()


def _mk_user(email: str, role: str) -> str:
    with session_scope() as s:
        u = User(email=email, role=role, display_name=role)
        s.add(u)
        s.flush()
        uid = u.id
        s.add(Identity(user_id=uid, provider="local", subject=email.lower(), secret_hash=hash_password(_PW)))
    return uid


def _client_for(email: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200
    return c


@pytest.fixture()
def roles():
    create_all()
    _clear()
    ids = {
        "owner": _mk_user("owner@x.com", "owner"),
        "operator": _mk_user("operator@x.com", "operator"),
        "viewer": _mk_user("viewer@x.com", "viewer"),
    }
    return ids


# --- read vs mutate ------------------------------------------------------
def test_anonymous_is_rejected():
    create_all()
    with TestClient(app) as c:
        assert c.get("/runs").status_code == 401
        assert c.post("/runs/none/launch", json={"dry_run": True}).status_code == 401


def test_viewer_can_read_not_mutate(roles):
    with _client_for("viewer@x.com") as c:
        assert c.get("/runs").status_code == 200            # read OK
        assert c.get("/auth/me").json()["role"] == "viewer"
        # any mutation is forbidden for a viewer (gate fires before the handler)
        assert c.post("/runs/does-not-exist/launch", json={"dry_run": True}).status_code == 403


def test_operator_can_mutate(roles):
    with _client_for("operator@x.com") as c:
        # passes the role gate -> reaches the handler -> 404 for the unknown run
        assert c.post("/runs/does-not-exist/launch", json={"dry_run": True}).status_code == 404
        assert c.get("/runs").status_code == 200


# --- owner-only administration -------------------------------------------
def test_user_admin_is_owner_only(roles):
    with _client_for("operator@x.com") as c:
        assert c.get("/auth/users").status_code == 403
    with _client_for("viewer@x.com") as c:
        assert c.get("/auth/users").status_code == 403
    with _client_for("owner@x.com") as c:
        users = c.get("/auth/users")
        assert users.status_code == 200 and len(users.json()) == 3


def test_owner_sets_role_and_enforcement_follows(roles):
    with _client_for("owner@x.com") as owner:
        # promote the viewer to operator
        r = owner.post(f"/auth/users/{roles['viewer']}/role", json={"role": "operator"})
        assert r.status_code == 200
        # an invalid role is rejected
        assert owner.post(f"/auth/users/{roles['operator']}/role", json={"role": "wizard"}).status_code == 400
        # cannot demote the last owner (lockout guard)
        assert owner.post(f"/auth/users/{roles['owner']}/role", json={"role": "viewer"}).status_code == 400

    # the ex-viewer can now mutate
    with _client_for("viewer@x.com") as exviewer:
        assert exviewer.post("/runs/does-not-exist/launch", json={"dry_run": True}).status_code == 404
