"""Phone-number login via a one-time code.

The challenge is held in-process with a short TTL (fine for a single-process lab;
a multi-worker deployment would move this to the DB/redis). In dev mode the code
is logged; otherwise it is POSTed to a configured SMS webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import time

from aletheia.auth.providers.base import Claim
from aletheia.config import get_settings

_TTL_S = 300.0
# phone -> (code_hash, expires_at)
_CHALLENGES: dict[str, tuple[str, float]] = {}


def _norm(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit() or ch == "+")


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def request_code(phone: str, *, now: float | None = None) -> None:
    """Generate + deliver a 6-digit code for ``phone``."""
    phone = _norm(phone)
    now = time.time() if now is None else now
    code = f"{secrets.randbelow(1_000_000):06d}"
    _CHALLENGES[phone] = (_hash(code), now + _TTL_S)
    s = get_settings()
    if s.phone_otp_dev_mode or not s.sms_webhook_url:
        print(f"[phone-otp] code for {phone}: {code}", file=sys.stderr)
        return
    import httpx

    with httpx.Client(timeout=10.0) as c:
        c.post(s.sms_webhook_url, json={"phone": phone, "code": code})


def verify_code(phone: str, code: str, *, now: float | None = None) -> Claim | None:
    """Check a submitted code; returns a Claim on success (single-use)."""
    phone = _norm(phone)
    now = time.time() if now is None else now
    entry = _CHALLENGES.get(phone)
    if entry is None:
        return None
    code_hash, expires_at = entry
    if now > expires_at or not hmac.compare_digest(code_hash, _hash(code)):
        return None
    del _CHALLENGES[phone]  # single use
    return Claim(provider="phone", subject=phone, display_name=phone)
