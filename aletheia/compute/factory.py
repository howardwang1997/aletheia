"""Compute-backend selection. ``local`` = restricted host subprocess (default);
``docker`` = hard sandbox (host featurizes, a no-network container trains)."""

from __future__ import annotations

from aletheia.compute.base import ComputeBackend
from aletheia.config import get_settings


def get_compute_backend() -> ComputeBackend:
    if get_settings().compute_backend == "docker":
        from aletheia.compute.docker_backend import get_docker_backend

        return get_docker_backend()
    from aletheia.compute.local import get_local_backend

    return get_local_backend()
