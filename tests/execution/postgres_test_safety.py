"""Fail-closed guard for destructive execution-authority PostgreSQL tests."""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import URL, make_url

from aletheia.db import engine

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DATABASE_PREFIX = "aletheia_pr4"


def _is_isolated_pr4_postgres(url: URL) -> bool:
    return (
        url.drivername.startswith("postgresql")
        and url.host in _LOOPBACK_HOSTS
        and url.database is not None
        and url.database.startswith(_DATABASE_PREFIX)
    )


def require_isolated_pr4_postgres() -> None:
    """Skip before opening a session unless env and configured engine are isolated."""

    raw_url = os.environ.get("ALETHEIA_DATABASE_URL")
    if not raw_url:
        pytest.skip("destructive PostgreSQL test requires explicit ALETHEIA_DATABASE_URL")
    try:
        requested = make_url(raw_url)
        configured = engine().url
    except (TypeError, ValueError):
        pytest.skip("destructive PostgreSQL test received an invalid explicit URL")
    if not _is_isolated_pr4_postgres(requested) or not _is_isolated_pr4_postgres(configured):
        pytest.skip("destructive PostgreSQL test requires loopback aletheia_pr4* database")
    if (
        requested.drivername,
        requested.host,
        requested.port,
        requested.database,
        requested.username,
    ) != (
        configured.drivername,
        configured.host,
        configured.port,
        configured.database,
        configured.username,
    ):
        pytest.skip("destructive PostgreSQL test engine differs from explicit isolated URL")


__all__ = ["require_isolated_pr4_postgres"]
