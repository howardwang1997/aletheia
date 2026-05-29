"""Server-side sessions. The cookie carries an opaque random token; only its
sha256 is stored, so sessions are revocable and the raw token never persists."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from aletheia.config import get_settings
from aletheia.db import session_scope
from aletheia.memory.ledger import AuthSession, User


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(user_id: str) -> str:
    """Create a session for ``user_id`` and return the raw cookie token."""
    raw = secrets.token_urlsafe(32)
    ttl = get_settings().auth_session_ttl_hours
    with session_scope() as s:
        s.add(
            AuthSession(
                user_id=user_id,
                token_hash=_hash(raw),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl),
            )
        )
    return raw


def validate(raw: str | None) -> dict[str, Any] | None:
    """Return the active user for a cookie token, or None if missing/expired/revoked."""
    if not raw:
        return None
    with session_scope() as s:
        row = s.query(AuthSession).filter(AuthSession.token_hash == _hash(raw)).first()
        if row is None or row.revoked:
            return None
        if row.expires_at <= datetime.now(timezone.utc):
            return None
        u = s.get(User, row.user_id)
        if u is None or not u.is_active:
            return None
        return {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role}


def revoke(raw: str | None) -> None:
    if not raw:
        return
    with session_scope() as s:
        row = s.query(AuthSession).filter(AuthSession.token_hash == _hash(raw)).first()
        if row is not None:
            row.revoked = True
