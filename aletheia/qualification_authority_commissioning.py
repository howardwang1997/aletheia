"""Crash-replayable qualification config, key, and PostgreSQL commissioning.

This target-host stage consumes one completed disabled bootstrap and produces the exact inputs for
the disabled installer.  It publishes three node-owned private keys and five canonical service
configs, then creates the three passwordless local-peer PostgreSQL roles and applies the frozen ACL
in one transaction.  It never installs, enables, or starts a unit and never grants scientific
authority.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import TextClause

from aletheia.execution.assignment_contracts import (
    node_transport_key_id,
    x25519_public_key_hex,
)
from aletheia.execution.qualification_deployment import (
    EXECUTION_SEQUENCES,
    EXECUTION_TABLES,
    EXPECTED_EXECUTION_SCHEMA_REVISION,
    PostgreSQLExecutionObjectOwnerObservation,
    PostgreSQLExpectedRoutine,
    PostgreSQLExpectedSequenceConfiguration,
    PostgreSQLExpectedTrigger,
    PostgreSQLNonExecutionRoutineOwnerObservation,
    QualificationDeploymentSpecV1,
    postgresql_role_privileges_sha256,
    qualification_postgresql_peer_database_url,
    render_postgresql_acl,
)
from aletheia.execution.qualification_node_service import (
    QualificationNodeMutableRootPinV1,
    QualificationNodePrivateKeyPinV1,
    QualificationNodeServiceConfigV1,
)
from aletheia.execution.oci_runtime import host_parent_chain_sha256
from aletheia.execution.qualification_outbox_service import (
    QualificationOutboxSpoolRootPinV1,
    QualificationTerminalOutboxServiceConfigV1,
)
from aletheia.execution.qualification_root_services import (
    QualificationQuotaServiceConfigV2,
    QualificationWatchdogServiceConfigV1,
    QualificationWorkspaceServiceConfigV1,
)
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceDeploymentManifestV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256
from aletheia.qualification_bootstrap import (
    QUALIFICATION_UNFINALIZED_MANIFEST_SHA256,
    LinuxQualificationBootstrapHost,
    QualificationBootstrapDirectoryObservation,
    QualificationBootstrapReceiptV1,
    QualificationBootstrapRequestV1,
    verify_qualification_bootstrap_receipt,
)
from aletheia.qualification_installer import (
    QualificationInstallationRequestV1,
    QualificationInstalledFileObservation,
    QualificationSystemdQuiescenceObservation,
    QualificationSystemdUnitState,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ROLE_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_OPT_IN_CONFIRMATION = "COMMISSION_QUALIFICATION_AUTHORITY_DISABLED"
_ADMIN_DATABASE_URL_ENV = "ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL"
_APPLICATION_ROLE_CONFIG = ("TimeZone=UTC", "search_path=pg_catalog, public")
_ARTIFACT_ORDER = (
    "key:node_signing",
    "key:assignment_transport",
    "key:runtime_control",
    "config:workspace",
    "config:quota",
    "config:watchdog",
    "config:node",
    "config:outbox",
)


class QualificationAuthorityCommissioningError(RuntimeError):
    """The commissioning request, host, journal, artifact, or database failed closed."""


def _absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not path.is_absolute()
        or value != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be one canonical absolute path")
    return path


def _is_direct_child(path: str, root: str) -> bool:
    candidate = _absolute_path(path, label="commissioning target")
    parent = _absolute_path(root, label="commissioning target root")
    return candidate.parent == parent


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


def _directory_observations(
    receipt: QualificationBootstrapReceiptV1,
) -> dict[str, QualificationBootstrapDirectoryObservation]:
    return {
        completion.application.observation.directory.purpose: (completion.application.observation)
        for completion in receipt.directory_completions
    }


def _root_pin_matches(
    pin: QualificationNodeMutableRootPinV1,
    observation: QualificationBootstrapDirectoryObservation,
) -> bool:
    return (
        pin.purpose == observation.directory.purpose
        and pin.path == observation.directory.path
        and pin.device == observation.device
        and pin.inode == observation.inode
        and pin.owner_uid == observation.observed_owner_uid
        and pin.owner_gid == observation.observed_owner_gid
        and pin.mode == observation.observed_mode
        and pin.parent_chain_sha256 == observation.parent_chain_sha256
    )


def finalize_qualification_deployment_spec(
    bootstrap_spec: QualificationDeploymentSpecV1,
    service_manifest: QualificationServiceDeploymentManifestV1,
) -> QualificationDeploymentSpecV1:
    """Replace only the explicit bootstrap sentinel with one final manifest digest."""

    bootstrap_spec = QualificationDeploymentSpecV1.model_validate(
        bootstrap_spec.model_dump(mode="python")
    )
    service_manifest = QualificationServiceDeploymentManifestV1.model_validate(
        service_manifest.model_dump(mode="python")
    )
    if (
        bootstrap_spec.deployment_manifest_sha256 != QUALIFICATION_UNFINALIZED_MANIFEST_SHA256
        or bootstrap_spec.expected_deployment_manifest.reviewed_sha256
        != QUALIFICATION_UNFINALIZED_MANIFEST_SHA256
        or service_manifest.deployment_id != bootstrap_spec.deployment_id
    ):
        raise QualificationAuthorityCommissioningError(
            "bootstrap spec sentinel or service-manifest deployment differs"
        )
    return QualificationDeploymentSpecV1.model_validate(
        {
            **bootstrap_spec.model_dump(mode="python"),
            "deployment_manifest_sha256": service_manifest.file_sha256,
            "expected_deployment_manifest": (
                bootstrap_spec.expected_deployment_manifest.model_copy(
                    update={"reviewed_sha256": service_manifest.file_sha256}
                )
            ),
        }
    )


class QualificationPrivateKeySourceV1(ExecutionModel):
    """Root-owned source and exact node-owned destination for one raw private key."""

    schema_name: Literal["aletheia.qualification_private_key_source"] = (
        "aletheia.qualification_private_key_source"
    )
    schema_version: Literal[1] = 1
    role: Literal["node_signing", "assignment_transport", "runtime_control"]
    source_path: str
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_owner_uid: Literal[0] = 0
    source_owner_gid: Literal[0] = 0
    source_mode: Literal[0o400] = 0o400
    target: QualificationNodePrivateKeyPinV1

    @model_validator(mode="after")
    def _source_is_external_and_exact(self) -> "QualificationPrivateKeySourceV1":
        source = _absolute_path(self.source_path, label=f"{self.role} key source")
        target = _absolute_path(self.target.path, label=f"{self.role} key target")
        if (
            self.target.role != self.role
            or self.source_sha256 != self.target.file_sha256
            or source == target
            or source in target.parents
            or target in source.parents
        ):
            raise ValueError("qualification private-key source differs from its target pin")
        return self

    @property
    def source_pin_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationCommissioningArtifactV1(ExecutionModel):
    """One immutable key or canonical config published before service installation."""

    schema_name: Literal["aletheia.qualification_commissioning_artifact"] = (
        "aletheia.qualification_commissioning_artifact"
    )
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=7)
    artifact_key: Literal[
        "key:node_signing",
        "key:assignment_transport",
        "key:runtime_control",
        "config:workspace",
        "config:quota",
        "config:watchdog",
        "config:node",
        "config:outbox",
    ]
    artifact_kind: Literal["private_key", "service_config"]
    target_path: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1, le=_MAX_ARTIFACT_BYTES)
    owner_uid: int = Field(ge=0, le=2**31 - 1)
    owner_gid: int = Field(ge=0, le=2**31 - 1)
    mode: int = Field(ge=0, le=0o7777)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _artifact_is_canonical(self) -> "QualificationCommissioningArtifactV1":
        _absolute_path(self.target_path, label="commissioning artifact")
        if self.artifact_key != _ARTIFACT_ORDER[self.ordinal]:
            raise ValueError("commissioning artifact ordinal differs")
        if (self.ordinal < 3) != (self.artifact_kind == "private_key"):
            raise ValueError("commissioning artifact kind differs from its ordinal")
        if self.artifact_kind == "private_key":
            if self.byte_length != 32 or self.mode != 0o400 or self.owner_uid == 0:
                raise ValueError("commissioned private-key custody is unsafe")
        elif self.mode & 0o222 or not self.mode & 0o400:
            raise ValueError("commissioned config must be owner-readable and immutable")
        return self

    @property
    def artifact_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLRoleProjectionV1(ExecutionModel):
    role_name: str = Field(pattern=_ROLE_PATTERN)
    can_login: bool
    superuser: Literal[False] = False
    create_database: Literal[False] = False
    create_role: Literal[False] = False
    inherit: Literal[False] = False
    replication: Literal[False] = False
    bypass_rls: Literal[False] = False
    connection_limit: int = Field(ge=-1, le=1024)
    password_is_null: Literal[True] = True
    valid_until_is_infinite: Literal[True] = True
    role_config: tuple[str, ...] = ()
    direct_memberships: tuple[str, ...] = ()
    direct_members: tuple[str, ...] = ()
    target_privileges_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class QualificationPostgreSQLHbaPeerRuleV1(ExecutionModel):
    role_name: str = Field(pattern=_ROLE_PATTERN)
    line_number: int = Field(ge=1)
    connection_type: Literal["local"] = "local"
    database_names: tuple[str, ...] = Field(min_length=1)
    user_names: tuple[str, ...] = Field(min_length=1)
    auth_method: Literal["peer"] = "peer"
    options: tuple[str, ...] = ()


class QualificationPostgreSQLServerIdentityV1(ExecutionModel):
    """Stable cluster/database projection pinned before any role or ACL mutation."""

    schema_name: Literal["aletheia.qualification_postgresql_server_identity"] = (
        "aletheia.qualification_postgresql_server_identity"
    )
    schema_version: Literal[1] = 1
    system_identifier: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    server_version_num: int = Field(ge=100_000, le=999_999)
    database_name: str
    database_oid: int = Field(ge=1, le=2**32 - 1)
    database_encoding: str = Field(pattern=r"^[A-Z0-9_-]{1,32}$")

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLExecutionCatalogProjectionV1(ExecutionModel):
    """Fresh routine, trigger, sequence, and owner projection from one database snapshot."""

    schema_name: Literal["aletheia.qualification_postgresql_execution_catalog_projection"] = (
        "aletheia.qualification_postgresql_execution_catalog_projection"
    )
    schema_version: Literal[1] = 1
    routines: tuple[PostgreSQLExpectedRoutine, ...] = Field(min_length=1)
    triggers: tuple[PostgreSQLExpectedTrigger, ...] = Field(min_length=1)
    sequences: tuple[PostgreSQLExpectedSequenceConfiguration, ...] = Field(min_length=1)
    object_owners: tuple[PostgreSQLExecutionObjectOwnerObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _catalog_is_canonical(self) -> "QualificationPostgreSQLExecutionCatalogProjectionV1":
        keys: tuple[tuple[tuple[object, ...], ...], ...] = (
            tuple(
                (item.routine_kind, item.routine_name, item.identity_argument_types)
                for item in self.routines
            ),
            tuple((item.table_name, item.trigger_name) for item in self.triggers),
            tuple((item.sequence_name,) for item in self.sequences),
            tuple((item.object_kind, item.object_name) for item in self.object_owners),
        )
        if any(values != tuple(sorted(set(values))) for values in keys):
            raise ValueError("PostgreSQL execution catalog projection is not canonical")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLCommissionedStateV1(ExecutionModel):
    """Stable post-transaction projection; observation time is journal metadata."""

    schema_name: Literal["aletheia.qualification_postgresql_commissioned_state"] = (
        "aletheia.qualification_postgresql_commissioned_state"
    )
    schema_version: Literal[1] = 1
    database_name: str
    database_owner_role: str = Field(pattern=_ROLE_PATTERN)
    admin_role: str = Field(pattern=_ROLE_PATTERN)
    admin_is_superuser: Literal[True] = True
    schema_revision: Literal[EXPECTED_EXECUTION_SCHEMA_REVISION]
    acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    server_identity: QualificationPostgreSQLServerIdentityV1
    roles: tuple[QualificationPostgreSQLRoleProjectionV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    hba_peer_rules: tuple[QualificationPostgreSQLHbaPeerRuleV1, ...] = Field(
        min_length=2,
        max_length=2,
    )

    @model_validator(mode="after")
    def _state_is_canonical(self) -> "QualificationPostgreSQLCommissionedStateV1":
        if (
            self.server_identity.database_name != self.database_name
            or tuple(item.role_name for item in self.roles)
            != tuple(sorted(item.role_name for item in self.roles))
            or tuple(item.role_name for item in self.hba_peer_rules)
            != tuple(sorted(item.role_name for item in self.hba_peer_rules))
        ):
            raise ValueError("PostgreSQL commissioning projection is not canonical")
        privilege_projections = {
            item.role_name: item.target_privileges_sha256 for item in self.roles
        }
        if privilege_projections.get(self.database_owner_role) is not None or any(
            digest is None
            for role_name, digest in privilege_projections.items()
            if role_name != self.database_owner_role
        ):
            raise ValueError("PostgreSQL target privilege projections are not exact")
        return self

    @property
    def state_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLDeploymentProjectionV1(ExecutionModel):
    """Atomic read-only deployment projection used by the independent observer."""

    schema_name: Literal["aletheia.qualification_postgresql_deployment_projection"] = (
        "aletheia.qualification_postgresql_deployment_projection"
    )
    schema_version: Literal[1] = 1
    commissioned_state: QualificationPostgreSQLCommissionedStateV1
    execution_catalog: QualificationPostgreSQLExecutionCatalogProjectionV1
    non_execution_public_routine_owners: tuple[PostgreSQLNonExecutionRoutineOwnerObservation, ...]
    database_time: AwareDatetime

    @model_validator(mode="after")
    def _projection_is_canonical(self) -> "QualificationPostgreSQLDeploymentProjectionV1":
        owner_keys = tuple(
            (item.routine_kind, item.identity, item.owner_role)
            for item in self.non_execution_public_routine_owners
        )
        if owner_keys != tuple(
            sorted(set(owner_keys))
        ) or self.database_time.utcoffset() != timedelta(0):
            raise ValueError("PostgreSQL deployment projection is not canonical or UTC")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationAuthorityCommissioningRequestV1(ExecutionModel):
    """Externally pinned request joining bootstrap evidence to final installer inputs."""

    schema_name: Literal["aletheia.qualification_authority_commissioning_request"] = (
        "aletheia.qualification_authority_commissioning_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=r"^qcr_[0-9a-f]{32}$")
    bootstrap_request: QualificationBootstrapRequestV1
    bootstrap_receipt: QualificationBootstrapReceiptV1
    installation_request: QualificationInstallationRequestV1
    workspace_config: QualificationWorkspaceServiceConfigV1
    quota_config: QualificationQuotaServiceConfigV2
    watchdog_config: QualificationWatchdogServiceConfigV1
    node_config: QualificationNodeServiceConfigV1
    outbox_config: QualificationTerminalOutboxServiceConfigV1
    private_key_sources: tuple[QualificationPrivateKeySourceV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    admin_database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    admin_role: str = Field(pattern=_ROLE_PATTERN)
    expected_postgresql_server_identity: QualificationPostgreSQLServerIdentityV1
    application_role_connection_limit: int = Field(default=16, ge=1, le=1024)
    requested_at: AwareDatetime
    opt_in_confirmation: Literal["COMMISSION_QUALIFICATION_AUTHORITY_DISABLED"] = (
        _OPT_IN_CONFIRMATION
    )
    publish_configs: Literal[True] = True
    publish_private_keys: Literal[True] = True
    create_postgresql_roles: Literal[True] = True
    apply_postgresql_acl: Literal[True] = True
    install_systemd_units: Literal[False] = False
    enable_services: Literal[False] = False
    start_services: Literal[False] = False
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_closed(self) -> "QualificationAuthorityCommissioningRequestV1":
        try:
            verify_qualification_bootstrap_receipt(
                self.bootstrap_request,
                self.bootstrap_receipt,
            )
        except QualificationAuthorityCommissioningError:
            raise
        except Exception as exc:
            raise ValueError("bootstrap receipt is not valid for commissioning") from exc
        seed = self.bootstrap_request.deployment_spec
        final = self.installation_request.deployment_spec
        manifest = self.installation_request.service_manifest
        if (
            seed.deployment_manifest_sha256 != QUALIFICATION_UNFINALIZED_MANIFEST_SHA256
            or seed.expected_deployment_manifest.reviewed_sha256
            != QUALIFICATION_UNFINALIZED_MANIFEST_SHA256
        ):
            raise ValueError("commissioning bootstrap spec is not explicitly unfinalized")
        expected_final = finalize_qualification_deployment_spec(seed, manifest)
        if final != expected_final:
            raise ValueError("final deployment spec changes more than the manifest digest")
        if (
            final.expected_schema_revision != EXPECTED_EXECUTION_SCHEMA_REVISION
            or self.expected_postgresql_server_identity.database_name != final.postgresql_database
            or self.expected_postgresql_server_identity.identity_sha256
            != final.postgresql_server_identity_sha256
            or manifest.deployment_id != seed.deployment_id
            or self.installation_request.journal_root
            != self.bootstrap_request.installer_journal_root
            or self.requested_at < self.bootstrap_receipt.completed_at
            or self.installation_request.requested_at < self.requested_at
            or not _is_utc(self.requested_at)
        ):
            raise ValueError("commissioning deployment identity, schema, journal, or time differs")

        config_by_role: dict[QualificationServiceRole, ExecutionModel] = {
            QualificationServiceRole.WORKSPACE: self.workspace_config,
            QualificationServiceRole.QUOTA: self.quota_config,
            QualificationServiceRole.WATCHDOG: self.watchdog_config,
            QualificationServiceRole.NODE: self.node_config,
            QualificationServiceRole.OUTBOX: self.outbox_config,
        }
        for role, config in config_by_role.items():
            process = manifest.process_for(role)
            payload = canonical_json_bytes(config)
            if (
                config.deployment_id != seed.deployment_id
                or config.process_config_binding_sha256
                != qualification_service_process_config_binding_sha256(process)
                or process.composition_config_file_sha256 != hashlib.sha256(payload).hexdigest()
                or not _is_direct_child(
                    process.composition_config_path,
                    self.bootstrap_request.service_config_root,
                )
            ):
                raise ValueError(f"{role.value} config differs from the final service process")

        node_url = qualification_postgresql_peer_database_url(
            final,
            role_name=final.postgresql_allocator_role,
        )
        outbox_url = qualification_postgresql_peer_database_url(
            final,
            role_name=final.postgresql_outbox_role,
        )
        if (
            self.node_config.database_url_sha256
            != hashlib.sha256(node_url.encode("utf-8")).hexdigest()
            or self.node_config.postgresql_role != final.postgresql_allocator_role
            or self.node_config.schema_revision != EXPECTED_EXECUTION_SCHEMA_REVISION
            or self.outbox_config.database_url_sha256
            != hashlib.sha256(outbox_url.encode("utf-8")).hexdigest()
            or self.outbox_config.postgresql_role != final.postgresql_outbox_role
            or self.outbox_config.schema_revision != EXPECTED_EXECUTION_SCHEMA_REVISION
        ):
            raise ValueError("node or outbox config differs from passwordless local-peer binding")
        if (
            self.node_config.qualification_custody.artifact_store_root != final.artifact_store_root
            or self.node_config.qualification_custody.authority_registry_root
            != final.authority_registry_root
            or self.node_config.image_layout.layout_root != final.oci_layout_root
            or self.node_config.quota_deployment != self.quota_config.quota_deployment
            or self.node_config.watchdog_deployment != self.watchdog_config.watchdog_deployment
            or self.node_config.oci_policy != self.quota_config.oci_policy
            or self.node_config.oci_policy != self.watchdog_config.oci_policy
            or self.quota_config.runtime_journal_root != final.runtime_journal_root
            or self.outbox_config.spool_root.path != final.outbox_spool_root
        ):
            raise ValueError("service configs differ from final deployment custody or policy")

        observations = _directory_observations(self.bootstrap_receipt)
        node_roots = (
            self.node_config.artifact_store_root_pin,
            self.node_config.node_state_root_pin,
            self.node_config.input_materialization_journal_root_pin,
            self.node_config.runtime_journal_root_pin,
        )
        if any(not _root_pin_matches(pin, observations[pin.purpose]) for pin in node_roots):
            raise ValueError("node config mutable roots differ from bootstrap evidence")
        spool = self.outbox_config.spool_root
        spool_observation = observations["outbox_spool"]
        if spool != QualificationOutboxSpoolRootPinV1(
            path=spool_observation.directory.path,
            device=spool_observation.device,
            inode=spool_observation.inode,
            owner_uid=spool_observation.observed_owner_uid,
            owner_gid=spool_observation.observed_owner_gid,
            mode=spool_observation.observed_mode,
            parent_chain_sha256=spool_observation.parent_chain_sha256,
        ):
            raise ValueError("outbox config spool differs from bootstrap evidence")
        self._verify_privileged_root_bindings(observations)

        expected_targets = (
            self.node_config.node_signing_key,
            self.node_config.assignment_transport_key,
            self.node_config.runtime_control_key,
        )
        if tuple(item.role for item in self.private_key_sources) != (
            "node_signing",
            "assignment_transport",
            "runtime_control",
        ):
            raise ValueError("private-key sources are not exhaustive and canonical")
        if tuple(item.target for item in self.private_key_sources) != expected_targets:
            raise ValueError("private-key sources differ from the node config")
        for source in self.private_key_sources:
            if (
                not _is_direct_child(
                    source.target.path,
                    self.bootstrap_request.node_private_key_root,
                )
                or source.target.owner_uid != final.node_uid
                or source.target.owner_gid != final.node_gid
            ):
                raise ValueError("private-key target escaped node-owned bootstrap custody")
        source_paths = tuple(Path(item.source_path) for item in self.private_key_sources)
        protected_roots = tuple(
            Path(value)
            for value in (
                self.bootstrap_request.journal_root,
                self.bootstrap_request.service_config_root,
                self.bootstrap_request.node_private_key_root,
                self.bootstrap_request.installer_journal_root,
                final.code_root,
                final.reviewed_python_environment.root_path,
                final.workspace_source_root,
                final.output_workspace_root,
                final.quota_backing_root,
                final.quota_state_root,
                final.watchdog_state_root,
                final.runtime_journal_root,
                final.node_state_root,
                final.artifact_store_root,
                final.input_materialization_journal_root,
                final.authority_registry_root,
                final.oci_layout_root,
                final.outbox_spool_root,
            )
        )
        if len(set(source_paths)) != 3 or any(
            source == root or source in root.parents or root in source.parents
            for source in source_paths
            for root in protected_roots
        ):
            raise ValueError("private-key sources overlap commissioning or deployment custody")
        if self.admin_role in {
            final.postgresql_owner_role,
            final.postgresql_allocator_role,
            final.postgresql_outbox_role,
        }:
            raise ValueError("PostgreSQL admin role must remain separate from application roles")
        expected_id = f"qcr_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected_id:
            raise ValueError("qualification commissioning request id is not derived")
        object.__setattr__(self, "request_id", expected_id)
        return self

    def _verify_privileged_root_bindings(
        self,
        observations: dict[str, QualificationBootstrapDirectoryObservation],
    ) -> None:
        workspace = self.workspace_config.workspace_deployment
        source = observations["workspace_source"]
        target = observations["output_workspace_underlay"]
        if (
            workspace.source_root,
            workspace.source_root_device,
            workspace.source_root_inode,
            workspace.source_root_owner_gid,
            workspace.source_root_mode,
            workspace.source_root_parent_chain_sha256,
        ) != (
            source.directory.path,
            source.device,
            source.inode,
            source.observed_owner_gid,
            source.observed_mode,
            source.parent_chain_sha256,
        ) or (
            workspace.target_root,
            workspace.target_underlay_device,
            workspace.target_underlay_inode,
            workspace.target_underlay_owner_uid,
            workspace.target_underlay_owner_gid,
            workspace.target_underlay_mode,
            workspace.target_parent_chain_sha256,
        ) != (
            target.directory.path,
            target.device,
            target.inode,
            target.observed_owner_uid,
            target.observed_owner_gid,
            target.observed_mode,
            target.parent_chain_sha256,
        ):
            raise ValueError("workspace config differs from bootstrap evidence")
        quota = self.quota_config.quota_deployment
        quota_bindings = (
            (
                "quota_backing",
                quota.backing_root,
                quota.backing_root_device,
                quota.backing_root_inode,
                quota.backing_root_mode,
                quota.backing_root_parent_chain_sha256,
            ),
            (
                "quota_state",
                quota.state_root,
                quota.state_root_device,
                quota.state_root_inode,
                quota.state_root_mode,
                quota.state_root_parent_chain_sha256,
            ),
            (
                "quota_socket_parent",
                str(Path(quota.socket_path).parent),
                quota.socket_parent_device,
                quota.socket_parent_inode,
                quota.socket_parent_mode,
                quota.socket_parent_parent_chain_sha256,
            ),
        )
        watchdog = self.watchdog_config.watchdog_deployment
        watchdog_bindings = (
            (
                "runtime_journal",
                watchdog.journal_root,
                watchdog.journal_root_device,
                watchdog.journal_root_inode,
                watchdog.journal_root_mode,
                watchdog.journal_root_parent_chain_sha256,
            ),
            (
                "watchdog_state",
                watchdog.state_root,
                watchdog.state_root_device,
                watchdog.state_root_inode,
                watchdog.state_root_mode,
                watchdog.state_root_parent_chain_sha256,
            ),
            (
                "watchdog_socket_parent",
                str(Path(watchdog.socket_path).parent),
                watchdog.socket_parent_device,
                watchdog.socket_parent_inode,
                watchdog.socket_parent_mode,
                watchdog.socket_parent_parent_chain_sha256,
            ),
        )
        for purpose, path, device, inode, mode, parent_sha256 in (
            *quota_bindings,
            *watchdog_bindings,
        ):
            observed = observations[purpose]
            if (path, device, inode, mode, parent_sha256) != (
                observed.directory.path,
                observed.device,
                observed.inode,
                observed.observed_mode,
                observed.parent_chain_sha256,
            ):
                raise ValueError(f"{purpose} config differs from bootstrap evidence")
        workspace_pin = quota.workspace_root_pin
        if (
            workspace_pin.path != target.directory.path
            or workspace_pin.device != source.device
            or workspace_pin.inode != source.inode
            or workspace_pin.owner_gid != source.observed_owner_gid
            or workspace_pin.mode != source.observed_mode
            or workspace_pin.parent_chain_sha256 != target.parent_chain_sha256
        ):
            raise ValueError("quota workspace pin differs from post-bind custody")

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def file_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def _config_payloads(
    request: QualificationAuthorityCommissioningRequestV1,
) -> tuple[tuple[QualificationServiceRole, ExecutionModel, bytes], ...]:
    values: tuple[tuple[QualificationServiceRole, ExecutionModel], ...] = (
        (QualificationServiceRole.WORKSPACE, request.workspace_config),
        (QualificationServiceRole.QUOTA, request.quota_config),
        (QualificationServiceRole.WATCHDOG, request.watchdog_config),
        (QualificationServiceRole.NODE, request.node_config),
        (QualificationServiceRole.OUTBOX, request.outbox_config),
    )
    return tuple((role, config, canonical_json_bytes(config)) for role, config in values)


class QualificationAuthorityCommissioningPlanV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_authority_commissioning_plan"] = (
        "aletheia.qualification_authority_commissioning_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str | None = Field(default=None, pattern=r"^qcp_[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    deployment_id: str
    bootstrap_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    installation_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    postgresql_acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifacts: tuple[QualificationCommissioningArtifactV1, ...] = Field(
        min_length=8,
        max_length=8,
    )
    services_installed: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_canonical(self) -> "QualificationAuthorityCommissioningPlanV1":
        if (
            tuple(item.ordinal for item in self.artifacts) != tuple(range(8))
            or tuple(item.artifact_key for item in self.artifacts) != _ARTIFACT_ORDER
        ):
            raise ValueError("commissioning artifacts are not exhaustive and canonical")
        if len({item.target_path for item in self.artifacts}) != 8:
            raise ValueError("commissioning artifact targets must be unique")
        expected_id = f"qcp_{self.identity_sha256[:32]}"
        if self.plan_id is not None and self.plan_id != expected_id:
            raise ValueError("qualification commissioning plan id is not derived")
        object.__setattr__(self, "plan_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"}))

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)


def build_qualification_authority_commissioning_plan(
    request: QualificationAuthorityCommissioningRequestV1,
) -> QualificationAuthorityCommissioningPlanV1:
    request = QualificationAuthorityCommissioningRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    key_artifacts = tuple(
        QualificationCommissioningArtifactV1(
            ordinal=ordinal,
            artifact_key=f"key:{source.role}",
            artifact_kind="private_key",
            target_path=source.target.path,
            content_sha256=source.target.file_sha256,
            byte_length=32,
            owner_uid=source.target.owner_uid,
            owner_gid=source.target.owner_gid,
            mode=source.target.file_mode,
        )
        for ordinal, source in enumerate(request.private_key_sources)
    )
    config_artifacts = tuple(
        QualificationCommissioningArtifactV1(
            ordinal=ordinal,
            artifact_key=f"config:{role.value}",
            artifact_kind="service_config",
            target_path=request.installation_request.service_manifest.process_for(
                role
            ).composition_config_path,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            byte_length=len(payload),
            owner_uid=request.installation_request.service_manifest.process_for(
                role
            ).composition_config_owner_uid,
            owner_gid=request.installation_request.service_manifest.process_for(
                role
            ).composition_config_owner_gid,
            mode=request.installation_request.service_manifest.process_for(
                role
            ).composition_config_mode,
        )
        for ordinal, (role, _config, payload) in enumerate(_config_payloads(request), start=3)
    )
    return QualificationAuthorityCommissioningPlanV1(
        request_id=request.request_id,
        request_sha256=canonical_sha256(request),
        deployment_id=request.installation_request.deployment_spec.deployment_id,
        bootstrap_receipt_sha256=request.bootstrap_receipt.receipt_sha256,
        final_spec_sha256=request.installation_request.deployment_spec.spec_sha256,
        installation_request_sha256=canonical_sha256(request.installation_request),
        postgresql_acl_sha256=hashlib.sha256(
            render_postgresql_acl(request.installation_request.deployment_spec)
        ).hexdigest(),
        artifacts=(*key_artifacts, *config_artifacts),
    )


class QualificationCommissioningActiveRequest(ExecutionModel):
    deployment_id: str
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qcp_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    automatic_start: Literal[False] = False


class QualificationCommissioningArtifactIntent(ExecutionModel):
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact: QualificationCommissioningArtifactV1

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationCommissioningArtifactCompletion(ExecutionModel):
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_ordinal: int = Field(ge=0, le=7)
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_file: QualificationInstalledFileObservation
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _completion_is_utc(self) -> "QualificationCommissioningArtifactCompletion":
        if not _is_utc(self.completed_at):
            raise ValueError("commissioning artifact completion must use UTC")
        return self

    @property
    def completion_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLCommissioningIntent(ExecutionModel):
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_name: str
    admin_database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    admin_role: str = Field(pattern=_ROLE_PATTERN)
    owner_role: str = Field(pattern=_ROLE_PATTERN)
    allocator_role: str = Field(pattern=_ROLE_PATTERN)
    outbox_role: str = Field(pattern=_ROLE_PATTERN)
    application_role_connection_limit: int = Field(ge=1, le=1024)
    acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: Literal[EXPECTED_EXECUTION_SCHEMA_REVISION]
    single_transaction_required: Literal[True] = True
    passwordless_local_peer_required: Literal[True] = True

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLCommissioningCompletion(ExecutionModel):
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    commissioned_state: QualificationPostgreSQLCommissionedStateV1
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _completion_is_utc(self) -> "QualificationPostgreSQLCommissioningCompletion":
        if not _is_utc(self.completed_at):
            raise ValueError("PostgreSQL commissioning completion must use UTC")
        return self


class QualificationAuthorityCommissioningReceiptV1(ExecutionModel):
    """Operational evidence for eight files and one exact database transaction."""

    schema_name: Literal["aletheia.qualification_authority_commissioning_receipt"] = (
        "aletheia.qualification_authority_commissioning_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^qcx_[0-9a-f]{32}$")
    deployment_id: str
    request_id: str = Field(pattern=r"^qcr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qcp_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    bootstrap_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_completions: tuple[QualificationCommissioningArtifactCompletion, ...] = Field(
        min_length=8,
        max_length=8,
    )
    postgresql_completion: QualificationPostgreSQLCommissioningCompletion
    quiescence_before_sha256: str = Field(pattern=_SHA256_PATTERN)
    quiescence_after_sha256: str = Field(pattern=_SHA256_PATTERN)
    installation_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    configs_published: Literal[True] = True
    private_keys_published: Literal[True] = True
    postgresql_roles_created: Literal[True] = True
    postgresql_acl_applied: Literal[True] = True
    services_installed: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_complete(self) -> "QualificationAuthorityCommissioningReceiptV1":
        if not _is_utc(self.completed_at):
            raise ValueError("qualification commissioning receipt must use UTC")
        if tuple(item.artifact_ordinal for item in self.artifact_completions) != tuple(range(8)):
            raise ValueError("commissioning receipt does not cover all artifacts")
        times = tuple(item.completed_at for item in self.artifact_completions)
        if times != tuple(sorted(times)):
            raise ValueError("commissioning artifact timestamps are not canonical")
        if any(
            item.request_id != self.request_id
            or item.plan_sha256 != self.plan_sha256
            or item.completed_at > self.completed_at
            for item in self.artifact_completions
        ):
            raise ValueError("commissioning artifact completion differs from receipt")
        if (
            self.postgresql_completion.request_id != self.request_id
            or self.postgresql_completion.plan_sha256 != self.plan_sha256
            or self.postgresql_completion.completed_at < times[-1]
            or self.postgresql_completion.completed_at > self.completed_at
        ):
            raise ValueError("PostgreSQL completion differs from commissioning receipt")
        expected_id = f"qcx_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("qualification commissioning receipt id is not derived")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def verify_qualification_authority_commissioning_receipt(
    request: QualificationAuthorityCommissioningRequestV1,
    receipt: QualificationAuthorityCommissioningReceiptV1,
) -> QualificationAuthorityCommissioningPlanV1:
    """Reconstruct the complete non-secret commissioning receipt chain."""

    try:
        request = QualificationAuthorityCommissioningRequestV1.model_validate(
            request.model_dump(mode="python")
        )
    except ValueError as exc:
        raise QualificationAuthorityCommissioningError(
            "qualification authority commissioning request is invalid"
        ) from exc
    try:
        receipt = QualificationAuthorityCommissioningReceiptV1.model_validate(
            receipt.model_dump(mode="python")
        )
    except ValueError as exc:
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL commissioning receipt chain differs"
        ) from exc
    plan = build_qualification_authority_commissioning_plan(request)
    if (
        receipt.deployment_id != plan.deployment_id
        or receipt.request_id != request.request_id
        or receipt.request_sha256 != canonical_sha256(request)
        or receipt.plan_id != plan.plan_id
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.bootstrap_receipt_sha256 != request.bootstrap_receipt.receipt_sha256
        or receipt.installation_request_sha256 != canonical_sha256(request.installation_request)
        or receipt.completed_at < request.requested_at
    ):
        raise QualificationAuthorityCommissioningError(
            "commissioning receipt differs from request or plan"
        )
    for artifact, completion in zip(plan.artifacts, receipt.artifact_completions, strict=True):
        intent = QualificationCommissioningArtifactIntent(
            request_id=request.request_id,
            plan_sha256=plan.plan_sha256,
            artifact=artifact,
        )
        if (
            completion.artifact_ordinal != artifact.ordinal
            or completion.artifact_sha256 != artifact.artifact_sha256
            or completion.intent_sha256 != intent.intent_sha256
            or not _observation_matches_artifact(completion.installed_file, artifact)
            or completion.completed_at < request.requested_at
        ):
            raise QualificationAuthorityCommissioningError(
                "commissioning artifact receipt chain differs"
            )
    database_intent = _postgresql_intent(request, plan)
    database_completion = receipt.postgresql_completion
    state = database_completion.commissioned_state
    expected_roles = tuple(
        QualificationPostgreSQLRoleProjectionV1(
            role_name=role,
            can_login=role != database_intent.owner_role,
            connection_limit=(
                -1
                if role == database_intent.owner_role
                else database_intent.application_role_connection_limit
            ),
            role_config=(_APPLICATION_ROLE_CONFIG if role != database_intent.owner_role else ()),
            target_privileges_sha256=(
                postgresql_role_privileges_sha256(
                    request.installation_request.deployment_spec,
                    role_name=role,
                )
                if role != database_intent.owner_role
                else None
            ),
        )
        for role in sorted(
            (
                database_intent.owner_role,
                database_intent.allocator_role,
                database_intent.outbox_role,
            )
        )
    )
    if (
        database_completion.intent_sha256 != database_intent.intent_sha256
        or state.database_name != database_intent.database_name
        or state.database_owner_role != database_intent.owner_role
        or state.admin_role != database_intent.admin_role
        or state.schema_revision != database_intent.schema_revision
        or state.acl_sha256 != database_intent.acl_sha256
        or state.server_identity != request.expected_postgresql_server_identity
        or state.roles != expected_roles
        or tuple(item.role_name for item in state.hba_peer_rules)
        != tuple(sorted((database_intent.allocator_role, database_intent.outbox_role)))
        or any(
            item.database_names != (database_intent.database_name,)
            or item.user_names != (item.role_name,)
            for item in state.hba_peer_rules
        )
    ):
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL commissioning receipt chain differs"
        )
    return plan


class QualificationAuthorityCommissioningHostPort(Protocol):
    def assert_linux_root(self) -> None: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def verify_bootstrap(self) -> None: ...

    def observe_systemd(
        self,
        unit_names: tuple[str, ...],
    ) -> QualificationSystemdQuiescenceObservation: ...

    def read_journal(self, path: Path) -> bytes | None: ...

    def write_journal_once(self, path: Path, payload: bytes) -> None: ...

    def load_private_key_source(self, source: QualificationPrivateKeySourceV1) -> bytes: ...

    def publish_artifact(
        self,
        artifact: QualificationCommissioningArtifactV1,
        payload: bytes,
    ) -> QualificationInstalledFileObservation: ...

    def observe_artifact(
        self,
        artifact: QualificationCommissioningArtifactV1,
    ) -> QualificationInstalledFileObservation: ...

    def commission_postgresql(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLCommissionedStateV1: ...

    def observe_postgresql(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLCommissionedStateV1: ...


def _journal_paths(
    request: QualificationAuthorityCommissioningRequestV1,
) -> tuple[Path, Path]:
    root = Path(request.bootstrap_request.journal_root)
    deployment_key = hashlib.sha256(
        request.installation_request.deployment_spec.deployment_id.encode("utf-8")
    ).hexdigest()[:32]
    return (
        root / f"authority-active-{deployment_key}.json",
        root / f"authority-{request.request_id}",
    )


def _validated_journal_model(
    payload: bytes | None,
    model_type: type[ExecutionModel],
    *,
    label: str,
) -> ExecutionModel | None:
    if payload is None:
        return None
    try:
        value = model_type.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise QualificationAuthorityCommissioningError(f"{label} journal is invalid") from exc
    if payload != canonical_json_bytes(value):
        raise QualificationAuthorityCommissioningError(f"{label} journal is not canonical")
    return value


def _observation_matches_artifact(
    observation: QualificationInstalledFileObservation,
    artifact: QualificationCommissioningArtifactV1,
) -> bool:
    return (
        observation.path == artifact.target_path
        and observation.content_sha256 == artifact.content_sha256
        and observation.byte_length == artifact.byte_length
        and observation.owner_uid == artifact.owner_uid
        and observation.owner_gid == artifact.owner_gid
        and observation.mode == artifact.mode
    )


def _units_are_absent(
    observation: QualificationSystemdQuiescenceObservation,
    unit_names: tuple[str, ...],
) -> bool:
    return tuple(item.unit_name for item in observation.units) == unit_names and all(
        item.load_state == "not-found" and item.unit_file_state == "not-found"
        for item in observation.units
    )


def _verify_private_key_payload(
    source: QualificationPrivateKeySourceV1,
    payload: bytes,
) -> None:
    if len(payload) != 32 or hashlib.sha256(payload).hexdigest() != source.source_sha256:
        raise QualificationAuthorityCommissioningError("private-key source bytes differ")
    if source.role == "assignment_transport":
        key_id = node_transport_key_id(x25519_public_key_hex(payload))
    else:
        public_hex = (
            Ed25519PrivateKey.from_private_bytes(payload)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            .hex()
        )
        key_id = qualification_key_id(public_hex)
    if key_id != source.target.key_id:
        raise QualificationAuthorityCommissioningError(
            "private-key source public identity differs from authority pin"
        )


def _postgresql_intent(
    request: QualificationAuthorityCommissioningRequestV1,
    plan: QualificationAuthorityCommissioningPlanV1,
) -> QualificationPostgreSQLCommissioningIntent:
    spec = request.installation_request.deployment_spec
    return QualificationPostgreSQLCommissioningIntent(
        request_id=request.request_id,
        plan_sha256=plan.plan_sha256,
        database_name=spec.postgresql_database,
        admin_database_url_sha256=request.admin_database_url_sha256,
        admin_role=request.admin_role,
        owner_role=spec.postgresql_owner_role,
        allocator_role=spec.postgresql_allocator_role,
        outbox_role=spec.postgresql_outbox_role,
        application_role_connection_limit=request.application_role_connection_limit,
        acl_sha256=plan.postgresql_acl_sha256,
        schema_revision=spec.expected_schema_revision,
    )


def qualification_postgresql_commissioning_intent(
    request: QualificationAuthorityCommissioningRequestV1,
) -> QualificationPostgreSQLCommissioningIntent:
    """Rebuild the exact PostgreSQL intent for independent read-only observation."""

    frozen = QualificationAuthorityCommissioningRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    return _postgresql_intent(
        frozen,
        build_qualification_authority_commissioning_plan(frozen),
    )


def commission_qualification_authority(
    request: QualificationAuthorityCommissioningRequestV1,
    host: QualificationAuthorityCommissioningHostPort,
    *,
    clock: Callable[[], datetime] | None = None,
    fault: Callable[[str], None] | None = None,
) -> QualificationAuthorityCommissioningReceiptV1:
    """Publish/resume eight exact files and one atomic PostgreSQL role/ACL transaction."""

    request = QualificationAuthorityCommissioningRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    plan = build_qualification_authority_commissioning_plan(request)
    config_payloads = {
        f"config:{role.value}": payload for role, _config, payload in _config_payloads(request)
    }
    now = clock or (lambda: datetime.now(timezone.utc))
    last_timestamp = request.requested_at

    def monitored_now() -> datetime:
        nonlocal last_timestamp
        observed = now()
        if not _is_utc(observed) or observed < last_timestamp:
            raise QualificationAuthorityCommissioningError(
                "commissioning clock moved backwards or left UTC"
            )
        last_timestamp = observed
        return observed

    inject = fault or (lambda _phase: None)
    active_path, request_root = _journal_paths(request)
    active = QualificationCommissioningActiveRequest(
        deployment_id=plan.deployment_id,
        request_id=request.request_id,
        request_sha256=canonical_sha256(request),
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )
    unit_names = tuple(
        sorted(
            (
                request.installation_request.deployment_spec.workspace_unit_name,
                request.installation_request.deployment_spec.quota_unit_name,
                request.installation_request.deployment_spec.watchdog_unit_name,
                request.installation_request.deployment_spec.node_unit_name,
                request.installation_request.deployment_spec.outbox_unit_name,
            )
        )
    )

    host.assert_linux_root()
    with host.lock():
        host.verify_bootstrap()
        live_before = host.observe_systemd(unit_names)
        if not _units_are_absent(live_before, unit_names):
            raise QualificationAuthorityCommissioningError(
                "systemd units must remain absent during authority commissioning"
            )
        host.write_journal_once(active_path, canonical_json_bytes(active))
        host.write_journal_once(request_root / "request.json", canonical_json_bytes(request))
        host.write_journal_once(request_root / "plan.json", canonical_json_bytes(plan))
        stored_before = _validated_journal_model(
            host.read_journal(request_root / "systemd-before.json"),
            QualificationSystemdQuiescenceObservation,
            label="systemd before",
        )
        if stored_before is None:
            before = live_before
            host.write_journal_once(
                request_root / "systemd-before.json",
                canonical_json_bytes(before),
            )
        else:
            assert isinstance(stored_before, QualificationSystemdQuiescenceObservation)
            before = stored_before
            if not _units_are_absent(before, unit_names):
                raise QualificationAuthorityCommissioningError(
                    "stored pre-commissioning systemd state differs"
                )
        inject("after_journal_initialized")

        stored_receipt = _validated_journal_model(
            host.read_journal(request_root / "receipt.json"),
            QualificationAuthorityCommissioningReceiptV1,
            label="commissioning receipt",
        )
        completions: list[QualificationCommissioningArtifactCompletion] = []
        for artifact in plan.artifacts:
            intent = QualificationCommissioningArtifactIntent(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                artifact=artifact,
            )
            intent_path = request_root / f"artifact-{artifact.ordinal:02d}-intent.json"
            completion_path = request_root / f"artifact-{artifact.ordinal:02d}-completion.json"
            host.write_journal_once(intent_path, canonical_json_bytes(intent))
            stored = _validated_journal_model(
                host.read_journal(completion_path),
                QualificationCommissioningArtifactCompletion,
                label=f"artifact {artifact.ordinal} completion",
            )
            if stored is None:
                if artifact.artifact_kind == "private_key":
                    source = request.private_key_sources[artifact.ordinal]
                    payload = host.load_private_key_source(source)
                    _verify_private_key_payload(source, payload)
                else:
                    payload = config_payloads[artifact.artifact_key]
                installed = host.publish_artifact(artifact, payload)
                if not _observation_matches_artifact(installed, artifact):
                    raise QualificationAuthorityCommissioningError(
                        "published commissioning artifact differs"
                    )
                completion = QualificationCommissioningArtifactCompletion(
                    request_id=request.request_id,
                    plan_sha256=plan.plan_sha256,
                    artifact_ordinal=artifact.ordinal,
                    artifact_sha256=artifact.artifact_sha256,
                    intent_sha256=intent.intent_sha256,
                    installed_file=installed,
                    completed_at=monitored_now(),
                )
                host.write_journal_once(completion_path, canonical_json_bytes(completion))
            else:
                assert isinstance(stored, QualificationCommissioningArtifactCompletion)
                completion = stored
                observed = host.observe_artifact(artifact)
                if (
                    completion.request_id != request.request_id
                    or completion.plan_sha256 != plan.plan_sha256
                    or completion.artifact_ordinal != artifact.ordinal
                    or completion.artifact_sha256 != artifact.artifact_sha256
                    or completion.intent_sha256 != intent.intent_sha256
                    or completion.installed_file != observed
                    or not _observation_matches_artifact(observed, artifact)
                ):
                    raise QualificationAuthorityCommissioningError(
                        "artifact completion differs from exact retry"
                    )
                last_timestamp = max(last_timestamp, completion.completed_at)
            completions.append(completion)
            inject(f"after_artifact:{artifact.ordinal}")

        database_intent = _postgresql_intent(request, plan)
        host.write_journal_once(
            request_root / "postgresql-intent.json",
            canonical_json_bytes(database_intent),
        )
        stored_database = _validated_journal_model(
            host.read_journal(request_root / "postgresql-completion.json"),
            QualificationPostgreSQLCommissioningCompletion,
            label="PostgreSQL completion",
        )
        if stored_database is None:
            state = host.commission_postgresql(database_intent)
            database_completion = QualificationPostgreSQLCommissioningCompletion(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                intent_sha256=database_intent.intent_sha256,
                commissioned_state=state,
                completed_at=monitored_now(),
            )
            host.write_journal_once(
                request_root / "postgresql-completion.json",
                canonical_json_bytes(database_completion),
            )
        else:
            assert isinstance(stored_database, QualificationPostgreSQLCommissioningCompletion)
            database_completion = stored_database
            state = host.observe_postgresql(database_intent)
            if (
                database_completion.request_id != request.request_id
                or database_completion.plan_sha256 != plan.plan_sha256
                or database_completion.intent_sha256 != database_intent.intent_sha256
                or database_completion.commissioned_state != state
            ):
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL completion differs from exact retry"
                )
            last_timestamp = max(last_timestamp, database_completion.completed_at)
        inject("after_postgresql")
        live_after = host.observe_systemd(unit_names)
        if not _units_are_absent(live_after, unit_names):
            raise QualificationAuthorityCommissioningError(
                "commissioning installed, enabled, or started a systemd unit"
            )
        stored_after = _validated_journal_model(
            host.read_journal(request_root / "systemd-after.json"),
            QualificationSystemdQuiescenceObservation,
            label="systemd after",
        )
        if stored_after is None:
            after = live_after
            host.write_journal_once(
                request_root / "systemd-after.json",
                canonical_json_bytes(after),
            )
        else:
            assert isinstance(stored_after, QualificationSystemdQuiescenceObservation)
            after = stored_after
            if not _units_are_absent(after, unit_names):
                raise QualificationAuthorityCommissioningError(
                    "stored post-commissioning systemd state differs"
                )

        if stored_receipt is not None:
            assert isinstance(stored_receipt, QualificationAuthorityCommissioningReceiptV1)
            if (
                stored_receipt.deployment_id != plan.deployment_id
                or stored_receipt.request_id != request.request_id
                or stored_receipt.request_sha256 != canonical_sha256(request)
                or stored_receipt.plan_id != plan.plan_id
                or stored_receipt.plan_sha256 != plan.plan_sha256
                or stored_receipt.bootstrap_receipt_sha256
                != request.bootstrap_receipt.receipt_sha256
                or stored_receipt.artifact_completions != tuple(completions)
                or stored_receipt.postgresql_completion != database_completion
                or stored_receipt.installation_request_sha256
                != canonical_sha256(request.installation_request)
                or stored_receipt.quiescence_before_sha256 != before.observation_sha256
                or stored_receipt.quiescence_after_sha256 != after.observation_sha256
            ):
                raise QualificationAuthorityCommissioningError(
                    "stored commissioning receipt differs from exact retry"
                )
            verify_qualification_authority_commissioning_receipt(request, stored_receipt)
            return stored_receipt

        receipt = QualificationAuthorityCommissioningReceiptV1(
            deployment_id=plan.deployment_id,
            request_id=request.request_id,
            request_sha256=canonical_sha256(request),
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            bootstrap_receipt_sha256=request.bootstrap_receipt.receipt_sha256,
            artifact_completions=tuple(completions),
            postgresql_completion=database_completion,
            quiescence_before_sha256=before.observation_sha256,
            quiescence_after_sha256=after.observation_sha256,
            installation_request_sha256=canonical_sha256(request.installation_request),
            completed_at=monitored_now(),
        )
        verify_qualification_authority_commissioning_receipt(request, receipt)
        host.write_journal_once(request_root / "receipt.json", canonical_json_bytes(receipt))
        inject("after_receipt")
        return receipt


def _fresh_file(
    path_value: str | Path,
    *,
    expected_sha256: str | None,
    expected_owner_uid: int | None,
    expected_owner_gid: int | None,
    expected_mode: int | None,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
    preserve_missing: bool = False,
) -> tuple[bytes, QualificationInstalledFileObservation]:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path:
            raise QualificationAuthorityCommissioningError("pinned file traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        if preserve_missing:
            raise
        raise QualificationAuthorityCommissioningError("pinned file is missing") from None
    except OSError as exc:
        raise QualificationAuthorityCommissioningError("pinned file cannot be opened") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum_bytes
            or (expected_owner_uid is not None and before.st_uid != expected_owner_uid)
            or (expected_owner_gid is not None and before.st_gid != expected_owner_gid)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
        ):
            raise QualificationAuthorityCommissioningError("pinned file custody differs")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise QualificationAuthorityCommissioningError("pinned file exceeds byte bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or total != before.st_size:
        raise QualificationAuthorityCommissioningError("pinned file changed while read")
    payload = b"".join(chunks)
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise QualificationAuthorityCommissioningError("pinned file digest differs")
    return payload, QualificationInstalledFileObservation(
        path=str(path),
        content_sha256=digest,
        byte_length=len(payload),
        owner_uid=before.st_uid,
        owner_gid=before.st_gid,
        mode=stat.S_IMODE(before.st_mode),
        device=before.st_dev,
        inode=before.st_ino,
        link_count=before.st_nlink,
    )


def _quoted_identifier(value: str) -> str:
    if re.fullmatch(_ROLE_PATTERN, value) is None:
        raise QualificationAuthorityCommissioningError("PostgreSQL identifier is not canonical")
    return '"' + value.replace('"', '""') + '"'


def _acl_transaction_body(payload: bytes) -> str:
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualificationAuthorityCommissioningError("PostgreSQL ACL is not UTF-8") from exc
    lines = source.splitlines()
    try:
        begin = lines.index("BEGIN;")
        commit = len(lines) - 1 - lines[::-1].index("COMMIT;")
    except ValueError as exc:
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL ACL transaction wrapper is missing"
        ) from exc
    if begin >= commit or any(line.strip() for line in lines[commit + 1 :]):
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL ACL transaction wrapper is ambiguous"
        )
    return "\n".join(lines[begin + 1 : commit]) + "\n"


def _acl_transaction_statement(payload: bytes) -> TextClause:
    """Compile the generated ACL through SQLAlchemy so psycopg does not consume PL/pgSQL `%I`."""

    return text(_acl_transaction_body(payload))


def _acl_read_only_validation(payload: bytes) -> str:
    """Extract the generated catalog-validation block without any ACL mutation."""

    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualificationAuthorityCommissioningError("PostgreSQL ACL is not UTF-8") from exc
    lines = source.splitlines()
    opening = "DO $aletheia_acl$"
    closing = "$aletheia_acl$;"
    starts = [index for index, line in enumerate(lines) if line == opening]
    ends = [index for index, line in enumerate(lines) if line == closing]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL ACL read-only validation block is missing or ambiguous"
        )
    validation = lines[starts[0] : ends[0] + 1]
    forbidden_starts = (
        "ALTER ",
        "CALL ",
        "CREATE ",
        "DELETE ",
        "DROP ",
        "EXECUTE ",
        "GRANT ",
        "INSERT ",
        "MERGE ",
        "PERFORM ",
        "REVOKE ",
        "TRUNCATE ",
        "UPDATE ",
    )
    if any(line.strip().upper().startswith(forbidden_starts) for line in validation):
        raise QualificationAuthorityCommissioningError(
            "PostgreSQL ACL validation block is not read-only"
        )
    return "\n".join(validation) + "\n"


class LinuxQualificationAuthorityCommissioningHost:
    """Concrete Linux/root adapter for files, systemd observation, and PostgreSQL."""

    def __init__(self, request: QualificationAuthorityCommissioningRequestV1) -> None:
        self.request = QualificationAuthorityCommissioningRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        self._bootstrap = LinuxQualificationBootstrapHost(self.request.bootstrap_request)
        self._journal_root = Path(self.request.bootstrap_request.journal_root)
        self._active_path, self._request_root = _journal_paths(self.request)

    def assert_linux_root(self) -> None:
        self._bootstrap.assert_linux_root()

    def lock(self) -> AbstractContextManager[None]:
        return self._bootstrap.lock()

    @staticmethod
    def _assert_directory(
        path: Path,
        observation: QualificationBootstrapDirectoryObservation,
    ) -> None:
        try:
            if path.resolve(strict=True) != path:
                raise QualificationAuthorityCommissioningError(
                    "commissioning directory traverses a symlink"
                )
            metadata = path.lstat()
            parent_sha256 = host_parent_chain_sha256(path)
        except (OSError, ValueError) as exc:
            raise QualificationAuthorityCommissioningError(
                "commissioning directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_dev != observation.device
            or metadata.st_ino != observation.inode
            or metadata.st_uid != observation.observed_owner_uid
            or metadata.st_gid != observation.observed_owner_gid
            or stat.S_IMODE(metadata.st_mode) != observation.observed_mode
            or parent_sha256 != observation.parent_chain_sha256
        ):
            raise QualificationAuthorityCommissioningError(
                "commissioning directory differs from bootstrap evidence"
            )

    def verify_bootstrap(self) -> None:
        verify_qualification_bootstrap_receipt(
            self.request.bootstrap_request,
            self.request.bootstrap_receipt,
        )
        self._bootstrap.verify_pinned_inputs(completed=True)
        plan = verify_qualification_bootstrap_receipt(
            self.request.bootstrap_request,
            self.request.bootstrap_receipt,
        )
        for principal, completion in zip(
            plan.principals,
            self.request.bootstrap_receipt.principal_completions,
            strict=True,
        ):
            if self._bootstrap.observe_principal(principal) != completion.application.observation:
                raise QualificationAuthorityCommissioningError(
                    "live qualification principal differs from bootstrap receipt"
                )
        for directory, completion in zip(
            plan.directories,
            self.request.bootstrap_receipt.directory_completions,
            strict=True,
        ):
            observed = completion.application.observation
            if self._bootstrap.observe_directory(directory) != observed:
                raise QualificationAuthorityCommissioningError(
                    "live qualification directory differs from bootstrap receipt"
                )
            self._assert_directory(Path(directory.path), observed)
        for source in self.request.private_key_sources:
            try:
                source_parent_sha256 = host_parent_chain_sha256(Path(source.source_path))
            except ValueError as exc:
                raise QualificationAuthorityCommissioningError(
                    "private-key source parent chain is unsafe"
                ) from exc
            if source_parent_sha256 != source.source_parent_chain_sha256:
                raise QualificationAuthorityCommissioningError(
                    "private-key source parent chain differs from its pin"
                )
            _fresh_file(
                source.source_path,
                expected_sha256=source.source_sha256,
                expected_owner_uid=source.source_owner_uid,
                expected_owner_gid=source.source_owner_gid,
                expected_mode=source.source_mode,
                maximum_bytes=32,
            )
            try:
                parent_sha256 = host_parent_chain_sha256(Path(source.target.path))
            except ValueError as exc:
                raise QualificationAuthorityCommissioningError(
                    "private-key target parent chain is unavailable"
                ) from exc
            if parent_sha256 != source.target.parent_chain_sha256:
                raise QualificationAuthorityCommissioningError(
                    "private-key target parent chain differs from node config"
                )
        tool = self.request.installation_request.systemctl_executable
        _fresh_file(
            tool.path,
            expected_sha256=tool.reviewed_sha256,
            expected_owner_uid=tool.expected_owner_uid,
            expected_owner_gid=tool.expected_owner_gid,
            expected_mode=tool.expected_mode,
        )

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        tool = self.request.installation_request.systemctl_executable
        _fresh_file(
            tool.path,
            expected_sha256=tool.reviewed_sha256,
            expected_owner_uid=tool.expected_owner_uid,
            expected_owner_gid=tool.expected_owner_gid,
            expected_mode=tool.expected_mode,
        )
        try:
            return subprocess.run(
                [tool.path, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise QualificationAuthorityCommissioningError(
                "pinned systemctl invocation failed"
            ) from exc

    def observe_systemd(
        self,
        unit_names: tuple[str, ...],
    ) -> QualificationSystemdQuiescenceObservation:
        states: list[QualificationSystemdUnitState] = []
        for unit_name in unit_names:
            result = self._systemctl(
                "show",
                unit_name,
                "--property=LoadState,ActiveState,UnitFileState",
            )
            if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
                raise QualificationAuthorityCommissioningError(
                    "systemd unit state could not be observed"
                )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in values:
                    raise QualificationAuthorityCommissioningError(
                        "systemd returned duplicate state"
                    )
                values[key] = value
            try:
                unit_file_state = values["UnitFileState"] or "not-found"
                states.append(
                    QualificationSystemdUnitState(
                        unit_name=unit_name,
                        load_state=values["LoadState"],
                        active_state=values["ActiveState"],
                        unit_file_state=unit_file_state,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise QualificationAuthorityCommissioningError(
                    "systemd unit is present, active, enabled, failed, or ambiguous"
                ) from exc
        return QualificationSystemdQuiescenceObservation(
            units=tuple(states),
            observed_at=datetime.now(timezone.utc),
        )

    def _prepare_journal_parent(self, path: Path) -> None:
        if path == self._journal_root:
            return
        if path != self._request_root:
            raise QualificationAuthorityCommissioningError(
                "commissioning journal write escaped request root"
            )
        try:
            os.mkdir(path, 0o700)
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise QualificationAuthorityCommissioningError(
                "commissioning request journal could not be created"
            ) from exc
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise QualificationAuthorityCommissioningError(
                "commissioning request journal custody differs"
            )

    def read_journal(self, path: Path) -> bytes | None:
        self._prepare_journal_parent(path.parent)
        try:
            return _fresh_file(
                path,
                expected_sha256=None,
                expected_owner_uid=0,
                expected_owner_gid=0,
                expected_mode=0o400,
                preserve_missing=True,
            )[0]
        except FileNotFoundError:
            return None

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        self._prepare_journal_parent(path.parent)
        try:
            existing, _observation = _fresh_file(
                path,
                expected_sha256=None,
                expected_owner_uid=0,
                expected_owner_gid=0,
                expected_mode=0o400,
                preserve_missing=True,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != payload:
                raise QualificationAuthorityCommissioningError(
                    "commissioning journal exact retry differs"
                )
            return
        staging = path.parent / f".{path.name}-{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise QualificationAuthorityCommissioningError(
                        "commissioning journal write made no progress"
                    )
                offset += written
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if os.path.lexists(path):
                raise QualificationAuthorityCommissioningError(
                    "commissioning journal target appeared during publication"
                )
            os.replace(staging, path)
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
            raise

    def load_private_key_source(self, source: QualificationPrivateKeySourceV1) -> bytes:
        try:
            parent_sha256 = host_parent_chain_sha256(Path(source.source_path))
        except ValueError as exc:
            raise QualificationAuthorityCommissioningError(
                "private-key source parent chain is unsafe"
            ) from exc
        if parent_sha256 != source.source_parent_chain_sha256:
            raise QualificationAuthorityCommissioningError(
                "private-key source parent chain differs from its pin"
            )
        return _fresh_file(
            source.source_path,
            expected_sha256=source.source_sha256,
            expected_owner_uid=source.source_owner_uid,
            expected_owner_gid=source.source_owner_gid,
            expected_mode=source.source_mode,
            maximum_bytes=32,
        )[0]

    def _artifact_parent_observation(
        self,
        artifact: QualificationCommissioningArtifactV1,
    ) -> QualificationBootstrapDirectoryObservation:
        purpose = (
            "node_private_keys" if artifact.artifact_kind == "private_key" else "service_configs"
        )
        observation = _directory_observations(self.request.bootstrap_receipt)[purpose]
        if Path(artifact.target_path).parent != Path(observation.directory.path):
            raise QualificationAuthorityCommissioningError(
                "commissioning artifact escaped its exact bootstrap root"
            )
        self._assert_directory(Path(observation.directory.path), observation)
        return observation

    def _remove_stale_staging(
        self,
        artifact: QualificationCommissioningArtifactV1,
    ) -> None:
        target = Path(artifact.target_path)
        prefix = f".aletheia-{self.request.request_id}-{artifact.ordinal}-"
        try:
            entries = tuple(target.parent.iterdir())
        except OSError as exc:
            raise QualificationAuthorityCommissioningError(
                "commissioning artifact root cannot be listed"
            ) from exc
        removed = False
        for candidate in entries:
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(".tmp"):
                continue
            metadata = candidate.lstat()
            safe_root_staging = (
                metadata.st_uid == 0
                and metadata.st_gid == 0
                and stat.S_IMODE(metadata.st_mode) == 0o600
            )
            safe_final_staging = (
                metadata.st_uid == artifact.owner_uid
                and metadata.st_gid == artifact.owner_gid
                and stat.S_IMODE(metadata.st_mode) == artifact.mode
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not (safe_root_staging or safe_final_staging)
            ):
                raise QualificationAuthorityCommissioningError(
                    "stale commissioning staging custody differs"
                )
            candidate.unlink()
            removed = True
        if removed:
            descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def publish_artifact(
        self,
        artifact: QualificationCommissioningArtifactV1,
        payload: bytes,
    ) -> QualificationInstalledFileObservation:
        self._artifact_parent_observation(artifact)
        if (
            len(payload) != artifact.byte_length
            or hashlib.sha256(payload).hexdigest() != artifact.content_sha256
        ):
            raise QualificationAuthorityCommissioningError(
                "commissioning publication payload differs from artifact"
            )
        target = Path(artifact.target_path)
        try:
            return _fresh_file(
                target,
                expected_sha256=artifact.content_sha256,
                expected_owner_uid=artifact.owner_uid,
                expected_owner_gid=artifact.owner_gid,
                expected_mode=artifact.mode,
            )[1]
        except QualificationAuthorityCommissioningError:
            if os.path.lexists(target):
                raise QualificationAuthorityCommissioningError(
                    "commissioning target already exists with variant custody"
                ) from None
        self._remove_stale_staging(artifact)
        staging = target.parent / (
            f".aletheia-{self.request.request_id}-{artifact.ordinal}-{secrets.token_hex(8)}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise QualificationAuthorityCommissioningError(
                        "commissioning staging write made no progress"
                    )
                offset += written
            os.fchown(descriptor, artifact.owner_uid, artifact.owner_gid)
            os.fchmod(descriptor, artifact.mode)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if os.path.lexists(target):
                raise QualificationAuthorityCommissioningError(
                    "commissioning target appeared during atomic publication"
                )
            os.replace(staging, target)
            parent_descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
            raise
        return self.observe_artifact(artifact)

    def observe_artifact(
        self,
        artifact: QualificationCommissioningArtifactV1,
    ) -> QualificationInstalledFileObservation:
        self._artifact_parent_observation(artifact)
        return _fresh_file(
            artifact.target_path,
            expected_sha256=artifact.content_sha256,
            expected_owner_uid=artifact.owner_uid,
            expected_owner_gid=artifact.owner_gid,
            expected_mode=artifact.mode,
        )[1]

    def _database_url(self, intent: QualificationPostgreSQLCommissioningIntent) -> str:
        value = os.environ.get(_ADMIN_DATABASE_URL_ENV)
        if value is None or hashlib.sha256(value.encode("utf-8")).hexdigest() != (
            intent.admin_database_url_sha256
        ):
            raise QualificationAuthorityCommissioningError(
                "commissioning admin database URL is absent or differs from its pin"
            )
        try:
            url = make_url(value)
        except Exception as exc:
            raise QualificationAuthorityCommissioningError(
                "commissioning admin database URL is invalid"
            ) from exc
        if not url.drivername.startswith("postgresql") or url.database != intent.database_name:
            raise QualificationAuthorityCommissioningError(
                "commissioning admin URL targets another database"
            )
        return value

    @staticmethod
    def _admin_and_schema(
        connection: Connection,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> None:
        row = connection.execute(
            text(
                """
                SELECT current_user,
                       (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user),
                       current_database()
                """
            )
        ).one()
        revisions = tuple(
            item[0]
            for item in connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
        )
        if (
            row[0] != intent.admin_role
            or row[1] is not True
            or row[2] != intent.database_name
            or revisions != (intent.schema_revision,)
        ):
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL admin, database, or schema revision differs"
            )

    @staticmethod
    def _server_identity(connection: Connection) -> QualificationPostgreSQLServerIdentityV1:
        row = (
            connection.execute(
                text(
                    """
                SELECT control.system_identifier::text AS system_identifier,
                       current_setting('server_version_num')::integer AS server_version_num,
                       database.datname AS database_name,
                       database.oid AS database_oid,
                       pg_catalog.pg_encoding_to_char(database.encoding) AS database_encoding
                  FROM pg_catalog.pg_database AS database
                  CROSS JOIN pg_catalog.pg_control_system() AS control
                 WHERE database.datname = current_database()
                """
                )
            )
            .mappings()
            .one()
        )
        return QualificationPostgreSQLServerIdentityV1.model_validate(dict(row))

    def _require_server_identity(self, connection: Connection) -> None:
        if self._server_identity(connection) != self.request.expected_postgresql_server_identity:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL cluster/database identity differs from deployment pin"
            )

    @staticmethod
    def _raw_hba_rows(connection: Connection) -> tuple[dict[str, object], ...]:
        rows = connection.execute(
            text(
                """
                SELECT line_number, type, database, user_name, auth_method, options, error
                  FROM pg_catalog.pg_hba_file_rules
                 ORDER BY line_number
                """
            )
        ).mappings()
        result: list[dict[str, object]] = []
        for row in rows:
            if row["error"] is not None:
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL HBA contains an invalid rule"
                )
            result.append(dict(row))
        return tuple(result)

    @staticmethod
    def _hba_peer_rules(
        connection: Connection,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> tuple[QualificationPostgreSQLHbaPeerRuleV1, ...]:
        rows = LinuxQualificationAuthorityCommissioningHost._raw_hba_rows(connection)
        selected: list[QualificationPostgreSQLHbaPeerRuleV1] = []
        for role in sorted((intent.allocator_role, intent.outbox_role)):
            applicable: list[dict[str, object]] = []
            exact: list[dict[str, object]] = []
            for row in rows:
                databases = tuple(row["database"] or ())
                users = tuple(row["user_name"] or ())
                if row["type"] != "local":
                    continue
                if ("all" in databases or intent.database_name in databases) and (
                    "all" in users or role in users
                ):
                    applicable.append(row)
                if databases == (intent.database_name,) and users == (role,):
                    exact.append(row)
            if len(exact) != 1 or not applicable or applicable[0] is not exact[0]:
                raise QualificationAuthorityCommissioningError(
                    "passwordless peer HBA rule is absent, duplicated, or shadowed"
                )
            row = exact[0]
            options = tuple(row["options"] or ())
            if row["auth_method"] != "peer" or options:
                raise QualificationAuthorityCommissioningError(
                    "qualification HBA rule is not exact option-free peer authentication"
                )
            selected.append(
                QualificationPostgreSQLHbaPeerRuleV1(
                    role_name=role,
                    line_number=row["line_number"],
                    database_names=tuple(row["database"] or ()),
                    user_names=tuple(row["user_name"] or ()),
                    auth_method=row["auth_method"],
                    options=options,
                )
            )
        return tuple(selected)

    @staticmethod
    def _role_privileges_sha256(
        connection: Connection,
        spec: QualificationDeploymentSpecV1,
        role_name: str,
    ) -> str:
        rows = (
            connection.execute(
                text(
                    """
                    WITH direct_privileges AS (
                        SELECT 'database'::text AS scope,
                               database.datname::text AS object_name,
                               NULL::text AS subobject_name,
                               privilege.privilege_type::text AS privilege_type,
                               privilege.is_grantable
                          FROM pg_catalog.pg_database AS database
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                              database.datacl,
                              pg_catalog.acldefault('d', database.datdba)
                            )
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND database.datname = :database_name
                        UNION ALL
                        SELECT 'schema', namespace.nspname, NULL,
                               privilege.privilege_type, privilege.is_grantable
                          FROM pg_catalog.pg_namespace AS namespace
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                              namespace.nspacl,
                              pg_catalog.acldefault('n', namespace.nspowner)
                            )
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND namespace.nspname = :schema_name
                        UNION ALL
                        SELECT 'table', object.relname, NULL,
                               privilege.privilege_type, privilege.is_grantable
                          FROM pg_catalog.pg_class AS object
                          JOIN pg_catalog.pg_namespace AS namespace
                            ON namespace.oid = object.relnamespace
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                              object.relacl,
                              pg_catalog.acldefault('r', object.relowner)
                            )
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND namespace.nspname = :schema_name
                           AND object.relkind IN ('r', 'p')
                        UNION ALL
                        SELECT 'column', object.relname, attribute.attname,
                               privilege.privilege_type, privilege.is_grantable
                          FROM pg_catalog.pg_attribute AS attribute
                          JOIN pg_catalog.pg_class AS object
                            ON object.oid = attribute.attrelid
                          JOIN pg_catalog.pg_namespace AS namespace
                            ON namespace.oid = object.relnamespace
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            attribute.attacl
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND namespace.nspname = :schema_name
                           AND object.relkind IN ('r', 'p')
                           AND attribute.attnum > 0
                           AND NOT attribute.attisdropped
                        UNION ALL
                        SELECT 'sequence', object.relname, NULL,
                               privilege.privilege_type, privilege.is_grantable
                          FROM pg_catalog.pg_class AS object
                          JOIN pg_catalog.pg_namespace AS namespace
                            ON namespace.oid = object.relnamespace
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                              object.relacl,
                              pg_catalog.acldefault('S', object.relowner)
                            )
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND namespace.nspname = :schema_name
                           AND object.relkind = 'S'
                        UNION ALL
                        SELECT 'routine',
                               routine.proname || '(' || COALESCE((
                                 SELECT string_agg(
                                   pg_catalog.format_type(argument.type_oid, NULL),
                                   ', ' ORDER BY argument.ordinality
                                 )
                                   FROM unnest(routine.proargtypes::oid[]) WITH ORDINALITY
                                        AS argument(type_oid, ordinality)
                               ), '') || ')',
                               NULL, privilege.privilege_type, privilege.is_grantable
                          FROM pg_catalog.pg_proc AS routine
                          JOIN pg_catalog.pg_namespace AS namespace
                            ON namespace.oid = routine.pronamespace
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                              routine.proacl,
                              pg_catalog.acldefault('f', routine.proowner)
                            )
                          ) AS privilege
                          JOIN pg_catalog.pg_roles AS grantee
                            ON grantee.oid = privilege.grantee
                         WHERE grantee.rolname = :role_name
                           AND namespace.nspname = :schema_name
                           AND routine.prokind IN ('f', 'p')
                    )
                    SELECT scope, object_name, subobject_name,
                           privilege_type, is_grantable
                      FROM direct_privileges
                     ORDER BY scope, object_name, subobject_name, privilege_type
                    """
                ),
                {
                    "role_name": role_name,
                    "database_name": spec.postgresql_database,
                    "schema_name": spec.postgresql_schema,
                },
            )
            .mappings()
            .all()
        )
        projection: dict[str, object] = {
            "schema": "aletheia.qualification_postgresql_role_privileges",
            "schema_version": 1,
            "role_name": role_name,
            "database_connect": (),
            "database_create": (),
            "database_temporary": (),
            "schema_usage": (),
            "schema_create": (),
            "table_select": (),
            "table_insert": (),
            "table_update": (),
            "column_update": (),
            "table_delete": (),
            "table_truncate": (),
            "table_references": (),
            "table_trigger": (),
            "sequence_usage": (),
            "routine_execute": (),
            "grantable_privileges": (),
        }
        scalar_keys = {
            ("database", "CONNECT"): "database_connect",
            ("database", "CREATE"): "database_create",
            ("database", "TEMPORARY"): "database_temporary",
            ("schema", "USAGE"): "schema_usage",
            ("schema", "CREATE"): "schema_create",
            ("table", "SELECT"): "table_select",
            ("table", "INSERT"): "table_insert",
            ("table", "UPDATE"): "table_update",
            ("table", "DELETE"): "table_delete",
            ("table", "TRUNCATE"): "table_truncate",
            ("table", "REFERENCES"): "table_references",
            ("table", "TRIGGER"): "table_trigger",
            ("sequence", "USAGE"): "sequence_usage",
            ("routine", "EXECUTE"): "routine_execute",
        }
        values: dict[str, set[str]] = {key: set() for key in scalar_keys.values()}
        column_updates: dict[str, set[str]] = {}
        grantable: set[tuple[str, str, str, str]] = set()
        for row in rows:
            scope = row["scope"]
            object_name = row["object_name"]
            subobject_name = row["subobject_name"]
            privilege_type = row["privilege_type"]
            if row["is_grantable"]:
                grantable.add((scope, object_name, subobject_name or "", privilege_type))
            if scope == "column":
                if privilege_type != "UPDATE" or subobject_name is None:
                    raise QualificationAuthorityCommissioningError(
                        "PostgreSQL application role has an unsupported column privilege"
                    )
                column_updates.setdefault(object_name, set()).add(subobject_name)
                continue
            key = scalar_keys.get((scope, privilege_type))
            if key is None:
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL application role has an unsupported direct privilege"
                )
            values[key].add(object_name)
        for key, items in values.items():
            projection[key] = tuple(sorted(items))
        projection["column_update"] = tuple(
            (table_name, tuple(sorted(column_names)))
            for table_name, column_names in sorted(column_updates.items())
        )
        projection["grantable_privileges"] = tuple(sorted(grantable))
        return canonical_sha256(projection)

    @classmethod
    def _role_projection(
        cls,
        connection: Connection,
        spec: QualificationDeploymentSpecV1,
        role_name: str,
        *,
        observe_privileges: bool = True,
    ) -> QualificationPostgreSQLRoleProjectionV1 | None:
        row = (
            connection.execute(
                text(
                    """
                SELECT role.rolname, role.rolcanlogin, role.rolsuper,
                       role.rolcreatedb, role.rolcreaterole, role.rolinherit,
                       role.rolreplication, role.rolbypassrls, role.rolconnlimit,
                       authority.rolpassword IS NULL AS password_is_null,
                       role.rolvaliduntil IS NULL AS valid_until_is_infinite,
                       COALESCE(role.rolconfig, ARRAY[]::text[]) AS role_config
                  FROM pg_catalog.pg_roles AS role
                  JOIN pg_catalog.pg_authid AS authority ON authority.oid = role.oid
                 WHERE role.rolname = :role_name
                """
                ),
                {"role_name": role_name},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        memberships = tuple(
            item[0]
            for item in connection.execute(
                text(
                    """
                    SELECT granted.rolname
                      FROM pg_catalog.pg_auth_members AS membership
                      JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
                      JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
                     WHERE member.rolname = :role_name
                     ORDER BY granted.rolname
                    """
                ),
                {"role_name": role_name},
            )
        )
        members = tuple(
            item[0]
            for item in connection.execute(
                text(
                    """
                    SELECT member.rolname
                      FROM pg_catalog.pg_auth_members AS membership
                      JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
                      JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
                     WHERE granted.rolname = :role_name
                     ORDER BY member.rolname
                    """
                ),
                {"role_name": role_name},
            )
        )
        return QualificationPostgreSQLRoleProjectionV1(
            role_name=row["rolname"],
            can_login=row["rolcanlogin"],
            superuser=row["rolsuper"],
            create_database=row["rolcreatedb"],
            create_role=row["rolcreaterole"],
            inherit=row["rolinherit"],
            replication=row["rolreplication"],
            bypass_rls=row["rolbypassrls"],
            connection_limit=row["rolconnlimit"],
            password_is_null=row["password_is_null"],
            valid_until_is_infinite=row["valid_until_is_infinite"],
            role_config=tuple(sorted(row["role_config"])),
            direct_memberships=memberships,
            direct_members=members,
            target_privileges_sha256=(
                None
                if role_name == spec.postgresql_owner_role or not observe_privileges
                else cls._role_privileges_sha256(connection, spec, role_name)
            ),
        )

    @staticmethod
    def _expected_role(
        role_name: str,
        *,
        spec: QualificationDeploymentSpecV1,
        can_login: bool,
        connection_limit: int,
    ) -> QualificationPostgreSQLRoleProjectionV1:
        return QualificationPostgreSQLRoleProjectionV1(
            role_name=role_name,
            can_login=can_login,
            connection_limit=connection_limit,
            role_config=_APPLICATION_ROLE_CONFIG if can_login else (),
            target_privileges_sha256=(
                postgresql_role_privileges_sha256(spec, role_name=role_name) if can_login else None
            ),
        )

    @staticmethod
    def _execution_catalog(
        connection: Connection,
        spec: QualificationDeploymentSpecV1,
    ) -> QualificationPostgreSQLExecutionCatalogProjectionV1:
        routine_rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT CASE routine.prokind
                             WHEN 'f' THEN 'function'
                             WHEN 'p' THEN 'procedure'
                           END AS routine_kind,
                           routine.proname AS routine_name,
                           ARRAY(
                             SELECT pg_catalog.format_type(argument.type_oid, NULL)
                               FROM unnest(routine.proargtypes::oid[])
                                    WITH ORDINALITY AS argument(type_oid, ordinal)
                              ORDER BY argument.ordinal
                           ) AS identity_argument_types,
                           pg_catalog.pg_get_functiondef(routine.oid) AS definition,
                           language.lanname AS language,
                           routine.prosecdef AS security_definer,
                           COALESCE(routine.proconfig, ARRAY[]::text[]) AS configuration,
                           CASE routine.provolatile
                             WHEN 'i' THEN 'immutable'
                             WHEN 's' THEN 'stable'
                             WHEN 'v' THEN 'volatile'
                           END AS volatility,
                           owner.rolname AS owner_role
                      FROM pg_catalog.pg_proc AS routine
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = routine.pronamespace
                      JOIN pg_catalog.pg_language AS language
                        ON language.oid = routine.prolang
                      JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
                     WHERE namespace.nspname = 'public'
                       AND routine.prokind IN ('f', 'p')
                       AND left(routine.proname, :prefix_length) = :prefix
                     ORDER BY routine_kind, routine_name, identity_argument_types
                    """
                ),
                {
                    "prefix": spec.postgresql_execution_routine_name_prefix,
                    "prefix_length": len(spec.postgresql_execution_routine_name_prefix),
                },
            ).mappings()
        )
        routines: list[PostgreSQLExpectedRoutine] = []
        routine_owners: list[PostgreSQLExecutionObjectOwnerObservation] = []
        for row in routine_rows:
            definition = row["definition"]
            if not isinstance(definition, str):
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL routine definition is not text"
                )
            arguments = tuple(row["identity_argument_types"] or ())
            routine = PostgreSQLExpectedRoutine(
                routine_kind=row["routine_kind"],
                routine_schema="public",
                execution_owned=True,
                routine_name=row["routine_name"],
                identity_argument_types=arguments,
                definition_sha256=hashlib.sha256(definition.encode("utf-8")).hexdigest(),
                language=row["language"],
                security_definer=row["security_definer"],
                configuration=tuple(sorted(row["configuration"] or ())),
                volatility=row["volatility"],
            )
            routines.append(routine)
            routine_owners.append(
                PostgreSQLExecutionObjectOwnerObservation(
                    object_kind=routine.routine_kind,
                    object_name=routine.identity,
                    owner_role=row["owner_role"],
                )
            )

        trigger_rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT table_relation.relname AS table_name,
                           trigger.tgname AS trigger_name,
                           function.proname AS function_name,
                           ARRAY(
                             SELECT pg_catalog.format_type(argument.type_oid, NULL)
                               FROM unnest(function.proargtypes::oid[])
                                    WITH ORDINALITY AS argument(type_oid, ordinal)
                              ORDER BY argument.ordinal
                           ) AS function_identity_argument_types,
                           pg_catalog.pg_get_triggerdef(trigger.oid, false) AS definition,
                           CASE trigger.tgenabled
                             WHEN 'O' THEN 'origin'
                             WHEN 'D' THEN 'disabled'
                             WHEN 'R' THEN 'replica'
                             WHEN 'A' THEN 'always'
                           END AS enabled,
                           function_namespace.nspname AS function_schema
                      FROM pg_catalog.pg_trigger AS trigger
                      JOIN pg_catalog.pg_class AS table_relation
                        ON table_relation.oid = trigger.tgrelid
                      JOIN pg_catalog.pg_namespace AS table_namespace
                        ON table_namespace.oid = table_relation.relnamespace
                      JOIN pg_catalog.pg_proc AS function
                        ON function.oid = trigger.tgfoid
                      JOIN pg_catalog.pg_namespace AS function_namespace
                        ON function_namespace.oid = function.pronamespace
                     WHERE table_namespace.nspname = 'public'
                       AND table_relation.relname::text = ANY(CAST(:table_names AS text[]))
                       AND NOT trigger.tgisinternal
                     ORDER BY table_name, trigger_name
                    """
                ),
                {"table_names": list(EXECUTION_TABLES)},
            ).mappings()
        )
        triggers: list[PostgreSQLExpectedTrigger] = []
        for row in trigger_rows:
            definition = row["definition"]
            if row["function_schema"] != "public" or not isinstance(definition, str):
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL execution trigger escaped the public routine namespace"
                )
            arguments = tuple(row["function_identity_argument_types"] or ())
            triggers.append(
                PostgreSQLExpectedTrigger(
                    table_name=row["table_name"],
                    trigger_name=row["trigger_name"],
                    function_identity=(f"{row['function_name']}({', '.join(arguments)})"),
                    definition_sha256=hashlib.sha256(definition.encode("utf-8")).hexdigest(),
                    enabled=row["enabled"],
                )
            )

        sequence_rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT sequence_relation.relname AS sequence_name,
                           pg_catalog.format_type(sequence.seqtypid, NULL) AS data_type,
                           CASE sequence_relation.relpersistence
                             WHEN 'p' THEN 'permanent'
                             WHEN 'u' THEN 'unlogged'
                             WHEN 't' THEN 'temporary'
                           END AS persistence,
                           sequence.seqstart AS start_value,
                           sequence.seqmin AS minimum_value,
                           sequence.seqmax AS maximum_value,
                           sequence.seqincrement AS increment_by,
                           sequence.seqcache AS cache_size,
                           sequence.seqcycle AS cycles,
                           owned_relation.relname AS owned_by_table,
                           owned_column.attname AS owned_by_column,
                           owner.rolname AS owner_role
                      FROM pg_catalog.pg_class AS sequence_relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = sequence_relation.relnamespace
                      JOIN pg_catalog.pg_sequence AS sequence
                        ON sequence.seqrelid = sequence_relation.oid
                      JOIN pg_catalog.pg_roles AS owner
                        ON owner.oid = sequence_relation.relowner
                      LEFT JOIN pg_catalog.pg_depend AS dependency
                        ON dependency.classid = 'pg_catalog.pg_class'::regclass
                       AND dependency.objid = sequence_relation.oid
                       AND dependency.objsubid = 0
                       AND dependency.refclassid = 'pg_catalog.pg_class'::regclass
                       AND dependency.deptype IN ('a', 'i')
                      LEFT JOIN pg_catalog.pg_class AS owned_relation
                        ON owned_relation.oid = dependency.refobjid
                      LEFT JOIN pg_catalog.pg_attribute AS owned_column
                        ON owned_column.attrelid = dependency.refobjid
                       AND owned_column.attnum = dependency.refobjsubid
                     WHERE namespace.nspname = 'public'
                       AND sequence_relation.relkind = 'S'
                       AND sequence_relation.relname::text = ANY(CAST(:sequence_names AS text[]))
                     ORDER BY sequence_name
                    """
                ),
                {"sequence_names": list(EXECUTION_SEQUENCES)},
            ).mappings()
        )
        sequences = tuple(
            PostgreSQLExpectedSequenceConfiguration(
                sequence_name=row["sequence_name"],
                data_type=row["data_type"],
                persistence=row["persistence"],
                start_value=row["start_value"],
                minimum_value=row["minimum_value"],
                maximum_value=row["maximum_value"],
                increment_by=row["increment_by"],
                cache_size=row["cache_size"],
                cycles=row["cycles"],
                owned_by_table=row["owned_by_table"],
                owned_by_column=row["owned_by_column"],
            )
            for row in sequence_rows
        )
        relation_owner_rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT CASE relation.relkind
                             WHEN 'r' THEN 'table'
                             WHEN 'p' THEN 'table'
                             WHEN 'S' THEN 'sequence'
                           END AS object_kind,
                           relation.relname AS object_name,
                           owner.rolname AS owner_role
                      FROM pg_catalog.pg_class AS relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                     WHERE namespace.nspname = 'public'
                       AND (
                         relation.relname::text = ANY(CAST(:table_names AS text[]))
                         OR relation.relname::text = ANY(CAST(:sequence_names AS text[]))
                       )
                       AND relation.relkind IN ('r', 'p', 'S')
                     ORDER BY object_kind, object_name
                    """
                ),
                {
                    "table_names": list(EXECUTION_TABLES),
                    "sequence_names": list(EXECUTION_SEQUENCES),
                },
            ).mappings()
        )
        database_owner = (
            connection.execute(
                text(
                    """
                SELECT owner.rolname AS owner_role
                  FROM pg_catalog.pg_database AS database
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = database.datdba
                 WHERE database.datname = current_database()
                """
                )
            )
            .mappings()
            .one()
        )
        schema_owner = (
            connection.execute(
                text(
                    """
                SELECT owner.rolname AS owner_role
                  FROM pg_catalog.pg_namespace AS namespace
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
                 WHERE namespace.nspname = 'public'
                """
                )
            )
            .mappings()
            .one()
        )
        owners = tuple(
            sorted(
                (
                    PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="database",
                        object_name=spec.postgresql_database,
                        owner_role=database_owner["owner_role"],
                    ),
                    PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="schema",
                        object_name=spec.postgresql_schema,
                        owner_role=schema_owner["owner_role"],
                    ),
                    *(
                        PostgreSQLExecutionObjectOwnerObservation.model_validate(dict(row))
                        for row in relation_owner_rows
                    ),
                    *routine_owners,
                ),
                key=lambda item: (item.object_kind, item.object_name),
            )
        )
        return QualificationPostgreSQLExecutionCatalogProjectionV1(
            routines=tuple(routines),
            triggers=tuple(triggers),
            sequences=sequences,
            object_owners=owners,
        )

    def _state(
        self,
        connection: Connection,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLCommissionedStateV1:
        self._admin_and_schema(connection, intent)
        self._require_server_identity(connection)
        spec = self.request.installation_request.deployment_spec
        acl = render_postgresql_acl(spec)
        if hashlib.sha256(acl).hexdigest() != intent.acl_sha256:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL ACL differs from commissioning intent"
            )
        connection.exec_driver_sql(_acl_read_only_validation(acl))
        role_names = tuple(sorted((intent.owner_role, intent.allocator_role, intent.outbox_role)))
        roles: list[QualificationPostgreSQLRoleProjectionV1] = []
        for role_name in role_names:
            observed = self._role_projection(connection, spec, role_name)
            expected = self._expected_role(
                role_name,
                spec=spec,
                can_login=role_name != intent.owner_role,
                connection_limit=(
                    -1
                    if role_name == intent.owner_role
                    else intent.application_role_connection_limit
                ),
            )
            if observed != expected:
                raise QualificationAuthorityCommissioningError(
                    "PostgreSQL application role projection differs"
                )
            roles.append(observed)
        owner = connection.execute(
            text(
                """
                SELECT owner.rolname
                  FROM pg_catalog.pg_database AS database
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = database.datdba
                 WHERE database.datname = :database_name
                """
            ),
            {"database_name": intent.database_name},
        ).scalar_one()
        if owner != intent.owner_role:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL database owner differs from commissioning intent"
            )
        return QualificationPostgreSQLCommissionedStateV1(
            database_name=intent.database_name,
            database_owner_role=owner,
            admin_role=intent.admin_role,
            schema_revision=intent.schema_revision,
            acl_sha256=intent.acl_sha256,
            server_identity=self.request.expected_postgresql_server_identity,
            roles=tuple(roles),
            hba_peer_rules=self._hba_peer_rules(connection, intent),
        )

    @staticmethod
    def _create_role_sql(
        role_name: str,
        *,
        can_login: bool,
        connection_limit: int,
    ) -> str:
        login = "LOGIN" if can_login else "NOLOGIN"
        return (
            f"CREATE ROLE {_quoted_identifier(role_name)} {login} NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
            f"CONNECTION LIMIT {connection_limit} PASSWORD NULL"
        )

    def commission_postgresql(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLCommissionedStateV1:
        intent = QualificationPostgreSQLCommissioningIntent.model_validate(
            intent.model_dump(mode="python")
        )
        expected_intent = _postgresql_intent(
            self.request,
            build_qualification_authority_commissioning_plan(self.request),
        )
        if intent != expected_intent:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL commissioning intent differs from request"
            )
        engine = create_engine(self._database_url(intent), pool_pre_ping=True, future=True)
        try:
            with engine.begin() as connection:
                spec = self.request.installation_request.deployment_spec
                self._admin_and_schema(connection, intent)
                self._require_server_identity(connection)
                self._hba_peer_rules(connection, intent)
                roles = (
                    (intent.owner_role, False, -1),
                    (
                        intent.allocator_role,
                        True,
                        intent.application_role_connection_limit,
                    ),
                    (
                        intent.outbox_role,
                        True,
                        intent.application_role_connection_limit,
                    ),
                )
                for role_name, can_login, connection_limit in roles:
                    observed = self._role_projection(
                        connection,
                        spec,
                        role_name,
                        observe_privileges=False,
                    )
                    expected = self._expected_role(
                        role_name,
                        spec=spec,
                        can_login=can_login,
                        connection_limit=connection_limit,
                    ).model_copy(update={"target_privileges_sha256": None})
                    if observed is None:
                        connection.exec_driver_sql(
                            self._create_role_sql(
                                role_name,
                                can_login=can_login,
                                connection_limit=connection_limit,
                            )
                        )
                    elif observed.model_copy(
                        update={"role_config": expected.role_config}
                    ) != expected or observed.role_config not in ((), expected.role_config):
                        raise QualificationAuthorityCommissioningError(
                            "pre-existing PostgreSQL role has variant authority"
                        )
                connection.exec_driver_sql(
                    f"ALTER DATABASE {_quoted_identifier(intent.database_name)} "
                    f"OWNER TO {_quoted_identifier(intent.owner_role)}"
                )
                connection.exec_driver_sql(
                    f"REVOKE ALL PRIVILEGES ON DATABASE "
                    f"{_quoted_identifier(intent.database_name)} "
                    f"FROM {_quoted_identifier(intent.admin_role)}"
                )
                acl = render_postgresql_acl(spec)
                if hashlib.sha256(acl).hexdigest() != intent.acl_sha256:
                    raise QualificationAuthorityCommissioningError(
                        "rendered PostgreSQL ACL differs from intent"
                    )
                connection.execute(_acl_transaction_statement(acl))
                state = self._state(connection, intent)
            return state
        except QualificationAuthorityCommissioningError:
            raise
        except SQLAlchemyError as exc:
            raise QualificationAuthorityCommissioningError(
                "atomic PostgreSQL role/ACL commissioning failed"
            ) from exc
        finally:
            engine.dispose()

    def observe_postgresql(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLCommissionedStateV1:
        intent = QualificationPostgreSQLCommissioningIntent.model_validate(
            intent.model_dump(mode="python")
        )
        expected_intent = _postgresql_intent(
            self.request,
            build_qualification_authority_commissioning_plan(self.request),
        )
        if intent != expected_intent:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL observation intent differs from request"
            )
        engine = create_engine(self._database_url(intent), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    return self._state(connection, intent)
        except QualificationAuthorityCommissioningError:
            raise
        except SQLAlchemyError as exc:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL commissioned state could not be observed"
            ) from exc
        finally:
            engine.dispose()

    @staticmethod
    def _non_execution_public_routine_owners(
        connection: Connection,
        *,
        execution_prefix: str,
    ) -> tuple[PostgreSQLNonExecutionRoutineOwnerObservation, ...]:
        rows = connection.execute(
            text(
                """
                SELECT CASE routine.prokind
                         WHEN 'f' THEN 'function'
                         WHEN 'p' THEN 'procedure'
                       END AS routine_kind,
                       routine.proname AS routine_name,
                       ARRAY(
                         SELECT pg_catalog.format_type(argument.type_oid, NULL)
                           FROM unnest(routine.proargtypes::oid[])
                                WITH ORDINALITY AS argument(type_oid, ordinal)
                          ORDER BY argument.ordinal
                       ) AS identity_argument_types,
                       owner.rolname AS owner_role
                  FROM pg_catalog.pg_proc AS routine
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = routine.pronamespace
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
                 WHERE namespace.nspname = 'public'
                   AND routine.prokind IN ('f', 'p')
                   AND left(routine.proname, :prefix_length) <> :prefix
                 ORDER BY routine_kind, routine_name,
                          identity_argument_types, owner_role
                """
            ),
            {
                "prefix": execution_prefix,
                "prefix_length": len(execution_prefix),
            },
        ).mappings()
        return tuple(
            PostgreSQLNonExecutionRoutineOwnerObservation(
                routine_kind=row["routine_kind"],
                routine_schema="public",
                routine_name=row["routine_name"],
                identity_argument_types=tuple(row["identity_argument_types"]),
                owner_role=row["owner_role"],
            )
            for row in rows
        )

    def observe_postgresql_deployment_projection(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> QualificationPostgreSQLDeploymentProjectionV1:
        """Read every signed PostgreSQL observation field from one repeatable snapshot."""

        intent = QualificationPostgreSQLCommissioningIntent.model_validate(
            intent.model_dump(mode="python")
        )
        if intent != qualification_postgresql_commissioning_intent(self.request):
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL deployment observation intent differs from request"
            )
        engine = create_engine(self._database_url(intent), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection, connection.begin():
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                state = self._state(connection, intent)
                spec = self.request.installation_request.deployment_spec
                catalog = self._execution_catalog(connection, spec)
                unrelated = self._non_execution_public_routine_owners(
                    connection,
                    execution_prefix=spec.postgresql_execution_routine_name_prefix,
                )
                database_time = connection.execute(
                    text("SELECT pg_catalog.clock_timestamp()")
                ).scalar_one()
                return QualificationPostgreSQLDeploymentProjectionV1(
                    commissioned_state=state,
                    execution_catalog=catalog,
                    non_execution_public_routine_owners=unrelated,
                    database_time=database_time,
                )
        except QualificationAuthorityCommissioningError:
            raise
        except (SQLAlchemyError, ValueError) as exc:
            raise QualificationAuthorityCommissioningError(
                "atomic PostgreSQL deployment projection could not be observed"
            ) from exc
        finally:
            engine.dispose()

    def observe_non_execution_public_routine_owners(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> tuple[PostgreSQLNonExecutionRoutineOwnerObservation, ...]:
        """Enumerate the full immutable owner baseline outside the execution namespace."""

        intent = QualificationPostgreSQLCommissioningIntent.model_validate(
            intent.model_dump(mode="python")
        )
        expected_intent = qualification_postgresql_commissioning_intent(self.request)
        if intent != expected_intent:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL owner observation intent differs from request"
            )
        engine = create_engine(self._database_url(intent), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                self._admin_and_schema(connection, intent)
                self._require_server_identity(connection)
                return self._non_execution_public_routine_owners(
                    connection,
                    execution_prefix=self.request.installation_request.deployment_spec.postgresql_execution_routine_name_prefix,
                )
        except QualificationAuthorityCommissioningError:
            raise
        except SQLAlchemyError as exc:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL public-routine owners could not be observed"
            ) from exc
        finally:
            engine.dispose()

    def observe_postgresql_clock(
        self,
        intent: QualificationPostgreSQLCommissioningIntent,
    ) -> datetime:
        """Read one transaction-local database wall-clock sample through the pinned admin path."""

        intent = QualificationPostgreSQLCommissioningIntent.model_validate(
            intent.model_dump(mode="python")
        )
        if intent != qualification_postgresql_commissioning_intent(self.request):
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL clock observation intent differs from request"
            )
        engine = create_engine(self._database_url(intent), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                self._admin_and_schema(connection, intent)
                value = connection.execute(text("SELECT pg_catalog.clock_timestamp()")).scalar_one()
                if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
                    raise QualificationAuthorityCommissioningError(
                        "PostgreSQL clock sample is not timezone-aware UTC"
                    )
                return value
        except QualificationAuthorityCommissioningError:
            raise
        except SQLAlchemyError as exc:
            raise QualificationAuthorityCommissioningError(
                "PostgreSQL clock could not be observed"
            ) from exc
        finally:
            engine.dispose()


def _load_request_file(path: Path) -> QualificationAuthorityCommissioningRequestV1:
    payload, _observation = _fresh_file(
        path,
        expected_sha256=None,
        expected_owner_uid=0,
        expected_owner_gid=0,
        expected_mode=0o400,
    )
    try:
        request = QualificationAuthorityCommissioningRequestV1.model_validate_json(payload)
    except ValueError as exc:
        raise QualificationAuthorityCommissioningError(
            "commissioning request file is invalid"
        ) from exc
    if canonical_json_bytes(request) != payload:
        raise QualificationAuthorityCommissioningError(
            "commissioning request file is not canonical JSON"
        )
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply disabled qualification authority commissioning"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument("request", type=Path)
        if command == "apply":
            child.add_argument("--acknowledge", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        request = _load_request_file(arguments.request)
        if arguments.command == "plan":
            payload = canonical_json_bytes(
                build_qualification_authority_commissioning_plan(request)
            )
        else:
            if arguments.acknowledge != _OPT_IN_CONFIRMATION:
                raise QualificationAuthorityCommissioningError(
                    "commissioning requires the exact disabled-only acknowledgement"
                )
            payload = canonical_json_bytes(
                commission_qualification_authority(
                    request,
                    LinuxQualificationAuthorityCommissioningHost(request),
                )
            )
    except QualificationAuthorityCommissioningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(payload + b"\n")
    return 0


__all__ = [
    "LinuxQualificationAuthorityCommissioningHost",
    "QualificationAuthorityCommissioningError",
    "QualificationAuthorityCommissioningHostPort",
    "QualificationAuthorityCommissioningPlanV1",
    "QualificationAuthorityCommissioningReceiptV1",
    "QualificationAuthorityCommissioningRequestV1",
    "QualificationCommissioningArtifactCompletion",
    "QualificationCommissioningArtifactIntent",
    "QualificationCommissioningArtifactV1",
    "QualificationPostgreSQLCommissionedStateV1",
    "QualificationPostgreSQLCommissioningCompletion",
    "QualificationPostgreSQLCommissioningIntent",
    "QualificationPostgreSQLHbaPeerRuleV1",
    "QualificationPostgreSQLRoleProjectionV1",
    "QualificationPostgreSQLServerIdentityV1",
    "QualificationPrivateKeySourceV1",
    "build_qualification_authority_commissioning_plan",
    "commission_qualification_authority",
    "finalize_qualification_deployment_spec",
    "main",
    "qualification_postgresql_commissioning_intent",
    "verify_qualification_authority_commissioning_receipt",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the checked-in wrapper
    raise SystemExit(main())
