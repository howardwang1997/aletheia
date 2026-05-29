"""User + identity resolution and the owner bootstrap.

Security: OAuth/phone logins do NOT open self-registration. A not-yet-linked
identity is admitted only if (a) it is the very first user (becomes owner),
(b) its email matches an existing user, or (c) its email/phone is in the
``auth_allowed_logins`` allowlist. Otherwise the login is refused.
"""

from __future__ import annotations

from sqlalchemy import func

from aletheia.auth.passwords import hash_password, verify_password
from aletheia.auth.providers.base import Claim
from aletheia.config import get_settings
from aletheia.db import session_scope
from aletheia.memory.ledger import Identity, User


def resolve_login(claim: Claim) -> str | None:
    """Map a provider Claim to a user id, creating/linking as policy allows.
    Returns None if the identity is not authorized to sign in."""
    settings = get_settings()
    with session_scope() as s:
        ident = (
            s.query(Identity)
            .filter(Identity.provider == claim.provider, Identity.subject == claim.subject)
            .first()
        )
        if ident is not None:
            return ident.user_id  # already linked

        user = None
        if claim.email:
            user = s.query(User).filter(User.email == claim.email).first()

        if user is None:
            n_users = s.query(func.count(User.id)).scalar() or 0
            allowlist = settings.allowed_logins_set
            candidates = {c.lower() for c in (claim.email, claim.subject) if c}
            permitted = (n_users == 0) or bool(candidates & allowlist)
            if not permitted:
                return None  # no open self-registration
            role = "owner" if n_users == 0 else "operator"
            user = User(display_name=claim.display_name, email=claim.email, role=role)
            s.add(user)
            s.flush()

        s.add(
            Identity(
                user_id=user.id,
                provider=claim.provider,
                subject=claim.subject,
                meta_json=claim.meta,
            )
        )
        s.flush()
        return user.id


def authenticate_local(email: str, password: str) -> str | None:
    """Verify a local-password login; returns the user id or None."""
    with session_scope() as s:
        ident = (
            s.query(Identity)
            .filter(Identity.provider == "local", Identity.subject == email.strip().lower())
            .first()
        )
        if ident is None or not verify_password(password, ident.secret_hash):
            return None
        return ident.user_id


def bootstrap_owner() -> None:
    """Seed the owner's local-password account from settings (idempotent)."""
    settings = get_settings()
    if not (settings.owner_email and settings.owner_password):
        return
    subject = settings.owner_email.strip().lower()
    with session_scope() as s:
        exists = (
            s.query(Identity)
            .filter(Identity.provider == "local", Identity.subject == subject)
            .first()
        )
        if exists is not None:
            return
        user = s.query(User).filter(User.email == settings.owner_email).first()
        if user is None:
            user = User(display_name="owner", email=settings.owner_email, role="owner")
            s.add(user)
            s.flush()
        s.add(
            Identity(
                user_id=user.id,
                provider="local",
                subject=subject,
                secret_hash=hash_password(settings.owner_password),
            )
        )
