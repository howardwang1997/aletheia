"""Pinned PostgreSQL composition for controller outbox and delivery-reconciler processes.

This factory intentionally supports only roles that need operational database authority.  The
terminal dispatcher and controller worker require deployment-specific PR-4 custody and independent
step-authority adapters; trying to obtain either role here fails closed.
"""

from __future__ import annotations


def build_postgresql_runtime(*, deployment, controller_manifest, configuration_bytes):
    """Build the two authority-minimal PostgreSQL roles from one closed config."""

    import hashlib
    import json
    from collections import Counter
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision
    from aletheia.jobs.queue import DurableTaskQueue
    from aletheia.research_controller_runtime import (
        ResearchControllerRuntimeDependencies,
        ResearchControllerRuntimeRole,
    )
    from aletheia.research_store.store import PostgreSQLResearchKernelOutbox

    class PostgreSQLRuntimeConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.research_controller_postgresql_runtime_config"] = (
            "aletheia.research_controller_postgresql_runtime_config"
        )
        schema_version: Literal[1] = 1
        role: Literal["kernel_dispatcher", "delivery_reconciler"]
        process_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
        database_url_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
        scientific_authority: Literal[False] = False
        kernel_command_authority: Literal[False] = False
        observation_admission_authority: Literal[False] = False

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate PostgreSQL runtime config keys: {duplicates}")
        return dict(pairs)

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = PostgreSQLRuntimeConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("PostgreSQL controller runtime config is invalid") from exc
    database_url_sha256 = hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
    if (
        config.role != deployment.role.value
        or config.process_principal_id != deployment.process_principal_id
        or config.database_url_sha256 != database_url_sha256
        or config.schema_revision != expected_schema_revision()
        or controller_manifest.manifest_sha256 != deployment.controller_manifest_sha256
    ):
        raise ValueError("PostgreSQL controller runtime config differs from deployment state")
    queue = DurableTaskQueue(principal=config.process_principal_id)
    if deployment.role is ResearchControllerRuntimeRole.KERNEL_DISPATCHER:
        return ResearchControllerRuntimeDependencies(
            queue=queue,
            kernel_store=PostgreSQLResearchKernelOutbox(),
        )
    if deployment.role is ResearchControllerRuntimeRole.DELIVERY_RECONCILER:
        return ResearchControllerRuntimeDependencies(queue=queue)
    raise ValueError("PostgreSQL runtime factory does not own terminal or step authority")


__all__ = ["build_postgresql_runtime"]
