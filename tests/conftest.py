"""Suite-wide test policy.

Unit tests opt into the explicitly unsafe local developer executor so they do not
depend on a Docker daemon.  Dedicated hard-sandbox integration tests override this
fixture and exercise the real container boundary.
"""

from __future__ import annotations

import pytest

from aletheia.config import get_settings


@pytest.fixture(autouse=True)
def _explicit_local_dev_authored_code():
    settings = get_settings()
    old_backend = settings.authored_code_backend
    old_allow = settings.allow_unsafe_host_authored_code
    old_dirty_manifest = settings.allow_dirty_frozen_manifest
    settings.authored_code_backend = "local_dev"
    settings.allow_unsafe_host_authored_code = True
    settings.allow_dirty_frozen_manifest = True
    try:
        yield
    finally:
        settings.authored_code_backend = old_backend
        settings.allow_unsafe_host_authored_code = old_allow
        settings.allow_dirty_frozen_manifest = old_dirty_manifest
