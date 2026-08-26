"""Opt-in Linux principal and custody-root bootstrap for qualification deployment.

This is the first target-host commissioning stage.  It creates only two locked service accounts,
their primary groups, and an exhaustive set of empty custody roots while all systemd units remain
absent or disabled.  It does not publish configs or keys, mutate PostgreSQL, install units, start a
service, qualify a host, or admit scientific state.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import secrets
import stat
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.oci_deployment import host_parent_chain_sha256
from aletheia.execution.qualification_deployment import (
    QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY,
    QualificationDeploymentSpecV1,
    QualificationExpectedRootExecutable,
    qualification_postgresql_peer_database_url,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LINUX_NAME_PATTERN = r"^[a-z_][a-z0-9_-]{0,31}$"
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_OPT_IN_CONFIRMATION = "BOOTSTRAP_QUALIFICATION_ONLY_DISABLED"
_DIRECTORY_PURPOSES = (
    "artifact_store",
    "input_materialization_journal",
    "installer_journal",
    "node_private_keys",
    "node_state",
    "outbox_spool",
    "output_workspace_underlay",
    "quota_backing",
    "quota_socket_parent",
    "quota_state",
    "runtime_journal",
    "service_configs",
    "watchdog_socket_parent",
    "watchdog_state",
    "workspace_source",
)


class QualificationBootstrapError(RuntimeError):
    """The bootstrap request, journal, Linux identity, or directory state failed closed."""


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


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


class QualificationBootstrapSocketDirectoryPinV1(ExecutionModel):
    """Exact pre-existing PostgreSQL Unix-socket directory."""

    schema_name: Literal["aletheia.qualification_bootstrap_socket_directory_pin"] = (
        "aletheia.qualification_bootstrap_socket_directory_pin"
    )
    schema_version: Literal[1] = 1
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=0, le=2**31 - 1)
    owner_gid: int = Field(ge=0, le=2**31 - 1)
    mode: int = Field(ge=0, le=0o7777)
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    file_type: Literal["directory"] = "directory"
    symlink: Literal[False] = False

    @model_validator(mode="after")
    def _pin_is_safe(self) -> "QualificationBootstrapSocketDirectoryPinV1":
        _absolute_path(self.path, label="PostgreSQL socket directory")
        if self.mode & 0o002 or self.mode & 0o111 != 0o111:
            raise ValueError("PostgreSQL socket directory is not safely traversable")
        return self


class QualificationBootstrapPrincipalV1(ExecutionModel):
    """One locked Linux service identity paired with its PostgreSQL peer role."""

    schema_name: Literal["aletheia.qualification_bootstrap_principal"] = (
        "aletheia.qualification_bootstrap_principal"
    )
    schema_version: Literal[1] = 1
    role: Literal["node", "outbox"]
    user_name: str = Field(pattern=_LINUX_NAME_PATTERN)
    primary_group_name: str = Field(pattern=_LINUX_NAME_PATTERN)
    uid: int = Field(ge=1, le=2**31 - 1)
    gid: int = Field(ge=1, le=2**31 - 1)
    supplementary_group_names: tuple[str, ...]
    supplementary_gids: tuple[int, ...]
    postgresql_role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    home_directory: Literal["/nonexistent"] = "/nonexistent"
    login_shell: str
    password_locked: Literal[True] = True
    create_home: Literal[False] = False

    @model_validator(mode="after")
    def _principal_is_canonical(self) -> "QualificationBootstrapPrincipalV1":
        _absolute_path(self.login_shell, label=f"{self.role} login shell")
        if (
            self.user_name != self.primary_group_name
            or self.user_name != self.postgresql_role
            or self.supplementary_group_names != tuple(sorted(set(self.supplementary_group_names)))
            or self.supplementary_gids != tuple(sorted(set(self.supplementary_gids)))
            or len(self.supplementary_group_names) != len(self.supplementary_gids)
        ):
            raise ValueError("qualification service principal is not canonical")
        return self

    @property
    def principal_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapDirectoryV1(ExecutionModel):
    """One empty directory whose inode becomes a later service-config pin."""

    schema_name: Literal["aletheia.qualification_bootstrap_directory"] = (
        "aletheia.qualification_bootstrap_directory"
    )
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=len(_DIRECTORY_PURPOSES) - 1)
    purpose: Literal[
        "artifact_store",
        "input_materialization_journal",
        "installer_journal",
        "node_private_keys",
        "node_state",
        "outbox_spool",
        "output_workspace_underlay",
        "quota_backing",
        "quota_socket_parent",
        "quota_state",
        "runtime_journal",
        "service_configs",
        "watchdog_socket_parent",
        "watchdog_state",
        "workspace_source",
    ]
    path: str
    owner_uid: int = Field(ge=0, le=2**31 - 1)
    owner_gid: int = Field(ge=0, le=2**31 - 1)
    mode: int = Field(ge=0, le=0o7777)
    must_be_empty_at_creation: Literal[True] = True

    @model_validator(mode="after")
    def _directory_is_safe(self) -> "QualificationBootstrapDirectoryV1":
        _absolute_path(self.path, label=f"{self.purpose} bootstrap directory")
        if self.mode & 0o002 or self.mode & 0o700 != 0o700:
            raise ValueError("bootstrap directory must be owner-controlled")
        return self

    @property
    def directory_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapRequestV1(ExecutionModel):
    """Externally SHA-pinned, root-operator bootstrap request."""

    schema_name: Literal["aletheia.qualification_bootstrap_request"] = (
        "aletheia.qualification_bootstrap_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=r"^qbr_[0-9a-f]{32}$")
    deployment_spec: QualificationDeploymentSpecV1
    journal_root: str
    service_config_root: str
    node_private_key_root: str
    installer_journal_root: str
    node_user_name: str = Field(pattern=_LINUX_NAME_PATTERN)
    outbox_user_name: str = Field(pattern=_LINUX_NAME_PATTERN)
    docker_group_name: str = Field(pattern=_LINUX_NAME_PATTERN)
    docker_group_expected_member_names: tuple[str, ...]
    groupadd_executable: QualificationExpectedRootExecutable
    useradd_executable: QualificationExpectedRootExecutable
    nologin_executable: QualificationExpectedRootExecutable
    postgresql_socket_directory: QualificationBootstrapSocketDirectoryPinV1
    requested_at: AwareDatetime
    opt_in_confirmation: Literal["BOOTSTRAP_QUALIFICATION_ONLY_DISABLED"] = _OPT_IN_CONFIRMATION
    create_service_principals: Literal[True] = True
    create_empty_custody_roots: Literal[True] = True
    publish_configs: Literal[False] = False
    publish_private_keys: Literal[False] = False
    create_postgresql_roles: Literal[False] = False
    apply_postgresql_acl: Literal[False] = False
    install_systemd_units: Literal[False] = False
    enable_services: Literal[False] = False
    start_services: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_closed(self) -> "QualificationBootstrapRequestV1":
        spec = self.deployment_spec
        paths = tuple(
            _absolute_path(value, label=label)
            for value, label in (
                (self.journal_root, "bootstrap journal root"),
                (self.service_config_root, "service config root"),
                (self.node_private_key_root, "node private-key root"),
                (self.installer_journal_root, "installer journal root"),
            )
        )
        if any(
            _overlap(left, right)
            for index, left in enumerate(paths)
            for right in paths[index + 1 :]
        ):
            raise ValueError("bootstrap control roots overlap")
        guarded = tuple(
            Path(value)
            for value in (
                spec.code_root,
                spec.reviewed_python_environment.root_path,
                spec.deployment_manifest_path,
                spec.workspace_source_root,
                spec.output_workspace_root,
                spec.quota_backing_root,
                spec.quota_state_root,
                str(Path(spec.quota_socket_path).parent),
                spec.watchdog_state_root,
                str(Path(spec.watchdog_socket_path).parent),
                spec.runtime_journal_root,
                spec.node_state_root,
                spec.artifact_store_root,
                spec.input_materialization_journal_root,
                spec.authority_registry_root,
                spec.oci_layout_root,
                spec.outbox_spool_root,
                QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY,
            )
        )
        extra_targets = paths[1:]
        if any(_overlap(target, item) for target in extra_targets for item in guarded):
            raise ValueError("bootstrap config/key/journal root overlaps deployment custody")
        if any(_overlap(paths[0], item) for item in (*guarded, *extra_targets)):
            raise ValueError("bootstrap journal overlaps a deployment target")
        tools = (self.groupadd_executable, self.useradd_executable, self.nologin_executable)
        if len({item.path for item in tools}) != len(tools):
            raise ValueError("bootstrap tools must be distinct")
        if (
            self.node_user_name != spec.postgresql_allocator_role
            or self.outbox_user_name != spec.postgresql_outbox_role
            or self.node_user_name == self.outbox_user_name
            or self.docker_group_expected_member_names != (self.node_user_name,)
            or self.postgresql_socket_directory.path != QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY
            or not _is_utc(self.requested_at)
        ):
            raise ValueError("bootstrap principals or PostgreSQL peer binding differ from spec")
        deployment_targets = tuple(Path(value) for value in guarded[:-1])
        if any(
            _overlap(Path(self.postgresql_socket_directory.path), target)
            for target in deployment_targets
        ):
            raise ValueError("PostgreSQL socket directory overlaps deployment custody")
        expected_id = f"qbr_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected_id:
            raise ValueError("qualification bootstrap request id is not derived")
        object.__setattr__(self, "request_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def file_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def _principals(
    request: QualificationBootstrapRequestV1,
) -> tuple[QualificationBootstrapPrincipalV1, ...]:
    spec = request.deployment_spec
    return (
        QualificationBootstrapPrincipalV1(
            role="node",
            user_name=request.node_user_name,
            primary_group_name=request.node_user_name,
            uid=spec.node_uid,
            gid=spec.node_gid,
            supplementary_group_names=(request.docker_group_name,),
            supplementary_gids=(spec.docker_gid,),
            postgresql_role=spec.postgresql_allocator_role,
            login_shell=request.nologin_executable.path,
        ),
        QualificationBootstrapPrincipalV1(
            role="outbox",
            user_name=request.outbox_user_name,
            primary_group_name=request.outbox_user_name,
            uid=spec.outbox_uid,
            gid=spec.outbox_gid,
            supplementary_group_names=(),
            supplementary_gids=(),
            postgresql_role=spec.postgresql_outbox_role,
            login_shell=request.nologin_executable.path,
        ),
    )


def _directories(
    request: QualificationBootstrapRequestV1,
) -> tuple[QualificationBootstrapDirectoryV1, ...]:
    spec = request.deployment_spec
    values: dict[str, tuple[str, int, int, int]] = {
        "artifact_store": (spec.artifact_store_root, spec.node_uid, spec.node_gid, 0o700),
        "input_materialization_journal": (
            spec.input_materialization_journal_root,
            spec.node_uid,
            spec.node_gid,
            0o700,
        ),
        "installer_journal": (request.installer_journal_root, 0, 0, 0o700),
        "node_private_keys": (
            request.node_private_key_root,
            spec.node_uid,
            spec.node_gid,
            0o700,
        ),
        "node_state": (spec.node_state_root, spec.node_uid, spec.node_gid, 0o700),
        "outbox_spool": (spec.outbox_spool_root, spec.outbox_uid, spec.outbox_gid, 0o700),
        "output_workspace_underlay": (spec.output_workspace_root, 0, 0, 0o755),
        "quota_backing": (spec.quota_backing_root, 0, 0, 0o700),
        "quota_socket_parent": (str(Path(spec.quota_socket_path).parent), 0, 0, 0o755),
        "quota_state": (spec.quota_state_root, 0, 0, 0o700),
        "runtime_journal": (
            spec.runtime_journal_root,
            spec.node_uid,
            spec.node_gid,
            0o700,
        ),
        "service_configs": (request.service_config_root, 0, 0, 0o755),
        "watchdog_socket_parent": (
            str(Path(spec.watchdog_socket_path).parent),
            0,
            0,
            0o755,
        ),
        "watchdog_state": (spec.watchdog_state_root, 0, 0, 0o700),
        "workspace_source": (spec.workspace_source_root, 0, spec.node_gid, 0o1730),
    }
    return tuple(
        QualificationBootstrapDirectoryV1(
            ordinal=ordinal,
            purpose=purpose,
            path=values[purpose][0],
            owner_uid=values[purpose][1],
            owner_gid=values[purpose][2],
            mode=values[purpose][3],
        )
        for ordinal, purpose in enumerate(_DIRECTORY_PURPOSES)
    )


class QualificationBootstrapPlanV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_bootstrap_plan"] = (
        "aletheia.qualification_bootstrap_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str | None = Field(default=None, pattern=r"^qbp_[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    deployment_id: str
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_peer_database_url: str
    outbox_peer_database_url: str
    principals: tuple[QualificationBootstrapPrincipalV1, ...] = Field(min_length=2, max_length=2)
    directories: tuple[QualificationBootstrapDirectoryV1, ...] = Field(
        min_length=len(_DIRECTORY_PURPOSES),
        max_length=len(_DIRECTORY_PURPOSES),
    )
    configs_published: Literal[False] = False
    private_keys_published: Literal[False] = False
    postgresql_roles_created: Literal[False] = False
    postgresql_acl_applied: Literal[False] = False
    services_installed: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_canonical(self) -> "QualificationBootstrapPlanV1":
        if tuple(item.role for item in self.principals) != ("node", "outbox"):
            raise ValueError("bootstrap principals are not exhaustive")
        if tuple(item.ordinal for item in self.directories) != tuple(
            range(len(_DIRECTORY_PURPOSES))
        ):
            raise ValueError("bootstrap directory ordinals are not exhaustive")
        if tuple(item.purpose for item in self.directories) != _DIRECTORY_PURPOSES:
            raise ValueError("bootstrap directories are not canonical")
        if len({item.path for item in self.directories}) != len(self.directories):
            raise ValueError("bootstrap directory targets are not unique")
        expected_id = f"qbp_{self.identity_sha256[:32]}"
        if self.plan_id is not None and self.plan_id != expected_id:
            raise ValueError("qualification bootstrap plan id is not derived")
        object.__setattr__(self, "plan_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"}))

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)


def build_qualification_bootstrap_plan(
    request: QualificationBootstrapRequestV1,
) -> QualificationBootstrapPlanV1:
    request = QualificationBootstrapRequestV1.model_validate(request.model_dump(mode="python"))
    spec = request.deployment_spec
    return QualificationBootstrapPlanV1(
        request_id=request.request_id,
        request_sha256=canonical_sha256(request),
        deployment_id=spec.deployment_id,
        spec_sha256=spec.spec_sha256,
        node_peer_database_url=qualification_postgresql_peer_database_url(
            spec,
            role_name=spec.postgresql_allocator_role,
        ),
        outbox_peer_database_url=qualification_postgresql_peer_database_url(
            spec,
            role_name=spec.postgresql_outbox_role,
        ),
        principals=_principals(request),
        directories=_directories(request),
    )


class QualificationBootstrapPrincipalObservation(ExecutionModel):
    principal: QualificationBootstrapPrincipalV1
    observed_user_name: str
    observed_primary_group_name: str
    observed_uid: int = Field(ge=1)
    observed_gid: int = Field(ge=1)
    observed_supplementary_group_names: tuple[str, ...]
    observed_supplementary_gids: tuple[int, ...]
    observed_home_directory: str
    observed_login_shell: str
    password_locked: Literal[True]

    @model_validator(mode="after")
    def _observation_is_exact(self) -> "QualificationBootstrapPrincipalObservation":
        expected = self.principal
        if (
            self.observed_user_name != expected.user_name
            or self.observed_primary_group_name != expected.primary_group_name
            or self.observed_uid != expected.uid
            or self.observed_gid != expected.gid
            or self.observed_supplementary_group_names != expected.supplementary_group_names
            or self.observed_supplementary_gids != expected.supplementary_gids
            or self.observed_home_directory != expected.home_directory
            or self.observed_login_shell != expected.login_shell
        ):
            raise ValueError("observed qualification principal differs")
        return self

    @property
    def observation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapDirectoryObservation(ExecutionModel):
    directory: QualificationBootstrapDirectoryV1
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    observed_owner_uid: int = Field(ge=0)
    observed_owner_gid: int = Field(ge=0)
    observed_mode: int = Field(ge=0, le=0o7777)
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    file_type: Literal["directory"] = "directory"
    symlink: Literal[False] = False

    @model_validator(mode="after")
    def _observation_is_exact(self) -> "QualificationBootstrapDirectoryObservation":
        if (
            self.observed_owner_uid != self.directory.owner_uid
            or self.observed_owner_gid != self.directory.owner_gid
            or self.observed_mode != self.directory.mode
        ):
            raise ValueError("observed qualification directory differs")
        return self

    @property
    def observation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapPrincipalApplication(ExecutionModel):
    observation: QualificationBootstrapPrincipalObservation
    group_created: bool
    user_created: bool
    command_sha256s: tuple[str, ...]

    @model_validator(mode="after")
    def _commands_are_canonical(self) -> "QualificationBootstrapPrincipalApplication":
        if self.command_sha256s != tuple(dict.fromkeys(self.command_sha256s)):
            raise ValueError("bootstrap principal commands are not canonical")
        if bool(self.command_sha256s) != (self.group_created or self.user_created):
            raise ValueError("bootstrap principal command receipt differs from mutation")
        return self


class QualificationBootstrapDirectoryApplication(ExecutionModel):
    observation: QualificationBootstrapDirectoryObservation
    created: bool


class QualificationBootstrapActiveRequest(ExecutionModel):
    deployment_id: str
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qbp_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    automatic_start: Literal[False] = False


class QualificationBootstrapPrincipalIntent(ExecutionModel):
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal: QualificationBootstrapPrincipalV1

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapPrincipalCompletion(ExecutionModel):
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    application: QualificationBootstrapPrincipalApplication
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _completion_is_utc(self) -> "QualificationBootstrapPrincipalCompletion":
        if not _is_utc(self.completed_at):
            raise ValueError("bootstrap principal completion must use UTC")
        return self


class QualificationBootstrapDirectoryIntent(ExecutionModel):
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    directory: QualificationBootstrapDirectoryV1

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapDirectoryCompletion(ExecutionModel):
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    application: QualificationBootstrapDirectoryApplication
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _completion_is_utc(self) -> "QualificationBootstrapDirectoryCompletion":
        if not _is_utc(self.completed_at):
            raise ValueError("bootstrap directory completion must use UTC")
        return self


class QualificationBootstrapReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_bootstrap_receipt"] = (
        "aletheia.qualification_bootstrap_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^qbx_[0-9a-f]{32}$")
    deployment_id: str
    request_id: str = Field(pattern=r"^qbr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qbp_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_completions: tuple[QualificationBootstrapPrincipalCompletion, ...] = Field(
        min_length=2,
        max_length=2,
    )
    directory_completions: tuple[QualificationBootstrapDirectoryCompletion, ...] = Field(
        min_length=len(_DIRECTORY_PURPOSES),
        max_length=len(_DIRECTORY_PURPOSES),
    )
    completed_at: AwareDatetime
    configs_published: Literal[False] = False
    private_keys_published: Literal[False] = False
    postgresql_roles_created: Literal[False] = False
    postgresql_acl_applied: Literal[False] = False
    services_installed: Literal[False] = False
    services_enabled: Literal[False] = False
    services_started: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_complete(self) -> "QualificationBootstrapReceiptV1":
        if not _is_utc(self.completed_at):
            raise ValueError("bootstrap receipt must use UTC")
        if tuple(
            item.application.observation.principal.role for item in self.principal_completions
        ) != ("node", "outbox"):
            raise ValueError("bootstrap receipt principal set is incomplete")
        if (
            tuple(
                item.application.observation.directory.purpose
                for item in self.directory_completions
            )
            != _DIRECTORY_PURPOSES
        ):
            raise ValueError("bootstrap receipt directory set is incomplete")
        completions = (*self.principal_completions, *self.directory_completions)
        if any(
            item.request_id != self.request_id
            or item.plan_sha256 != self.plan_sha256
            or item.completed_at > self.completed_at
            for item in completions
        ):
            raise ValueError("bootstrap receipt completion differs")
        times = tuple(item.completed_at for item in completions)
        if times != tuple(sorted(times)):
            raise ValueError("bootstrap completion timestamps are not canonical")
        expected_id = f"qbx_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("qualification bootstrap receipt id is not derived")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationBootstrapHostPort(Protocol):
    def assert_linux_root(self) -> None: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def verify_pinned_inputs(self, *, completed: bool) -> None: ...

    def read_journal(self, path: Path) -> bytes | None: ...

    def write_journal_once(self, path: Path, payload: bytes) -> None: ...

    def ensure_principal(
        self,
        principal: QualificationBootstrapPrincipalV1,
    ) -> QualificationBootstrapPrincipalApplication: ...

    def observe_principal(
        self,
        principal: QualificationBootstrapPrincipalV1,
    ) -> QualificationBootstrapPrincipalObservation: ...

    def ensure_directory(
        self,
        directory: QualificationBootstrapDirectoryV1,
    ) -> QualificationBootstrapDirectoryApplication: ...

    def observe_directory(
        self,
        directory: QualificationBootstrapDirectoryV1,
    ) -> QualificationBootstrapDirectoryObservation: ...


def _journal_paths(request: QualificationBootstrapRequestV1) -> tuple[Path, Path]:
    root = Path(request.journal_root)
    deployment_key = hashlib.sha256(request.deployment_spec.deployment_id.encode()).hexdigest()[:32]
    return root / f"active-{deployment_key}.json", root / request.request_id


def _journal_model(
    payload: bytes | None,
    model: type[ExecutionModel],
    *,
    label: str,
) -> ExecutionModel | None:
    if payload is None:
        return None
    try:
        value = model.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise QualificationBootstrapError(f"{label} journal is invalid") from exc
    if payload != canonical_json_bytes(value):
        raise QualificationBootstrapError(f"{label} journal is not canonical")
    return value


def bootstrap_qualification_host(
    request: QualificationBootstrapRequestV1,
    host: QualificationBootstrapHostPort,
    *,
    clock: Callable[[], datetime] | None = None,
    fault: Callable[[str], None] | None = None,
) -> QualificationBootstrapReceiptV1:
    """Create or replay the exact disabled principal/root bootstrap."""

    request = QualificationBootstrapRequestV1.model_validate(request.model_dump(mode="python"))
    plan = build_qualification_bootstrap_plan(request)
    now = clock or (lambda: datetime.now(timezone.utc))
    inject = fault or (lambda _phase: None)
    last = request.requested_at

    def monitored_now() -> datetime:
        nonlocal last
        observed = now()
        if not _is_utc(observed):
            raise QualificationBootstrapError("bootstrap clock must return UTC")
        if observed < last:
            raise QualificationBootstrapError("bootstrap clock moved backwards")
        last = observed
        return observed

    def assert_clock_ready() -> None:
        observed = now()
        if not _is_utc(observed):
            raise QualificationBootstrapError("bootstrap clock must return UTC")
        if observed < last:
            raise QualificationBootstrapError("bootstrap clock moved backwards")

    active_path, request_root = _journal_paths(request)
    active = QualificationBootstrapActiveRequest(
        deployment_id=plan.deployment_id,
        request_id=request.request_id,
        request_sha256=canonical_sha256(request),
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )
    host.assert_linux_root()
    with host.lock():
        host.verify_pinned_inputs(completed=False)
        host.write_journal_once(active_path, canonical_json_bytes(active))
        host.write_journal_once(request_root / "request.json", canonical_json_bytes(request))
        host.write_journal_once(request_root / "plan.json", canonical_json_bytes(plan))
        inject("after_journal_initialized")

        existing = _journal_model(
            host.read_journal(request_root / "receipt.json"),
            QualificationBootstrapReceiptV1,
            label="bootstrap receipt",
        )
        if existing is not None:
            assert isinstance(existing, QualificationBootstrapReceiptV1)
            if (
                existing.deployment_id != plan.deployment_id
                or existing.request_id != request.request_id
                or existing.request_sha256 != canonical_sha256(request)
                or existing.plan_id != plan.plan_id
                or existing.plan_sha256 != plan.plan_sha256
            ):
                raise QualificationBootstrapError("existing bootstrap receipt differs")
            for principal, completion in zip(
                plan.principals,
                existing.principal_completions,
                strict=True,
            ):
                if host.observe_principal(principal) != completion.application.observation:
                    raise QualificationBootstrapError("bootstrapped principal changed")
            for directory, completion in zip(
                plan.directories,
                existing.directory_completions,
                strict=True,
            ):
                if host.observe_directory(directory) != completion.application.observation:
                    raise QualificationBootstrapError("bootstrapped directory changed")
            host.verify_pinned_inputs(completed=True)
            return existing

        principal_completions: list[QualificationBootstrapPrincipalCompletion] = []
        for ordinal, principal in enumerate(plan.principals):
            intent = QualificationBootstrapPrincipalIntent(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                principal=principal,
            )
            intent_path = request_root / f"principal-{ordinal}.intent.json"
            completion_path = request_root / f"principal-{ordinal}.completed.json"
            host.write_journal_once(intent_path, canonical_json_bytes(intent))
            stored = _journal_model(
                host.read_journal(completion_path),
                QualificationBootstrapPrincipalCompletion,
                label=f"principal {ordinal} completion",
            )
            if stored is None:
                assert_clock_ready()
                application = host.ensure_principal(principal)
                inject(f"after_principal_apply:{ordinal}")
                completion = QualificationBootstrapPrincipalCompletion(
                    request_id=request.request_id,
                    plan_sha256=plan.plan_sha256,
                    intent_sha256=intent.intent_sha256,
                    application=application,
                    completed_at=monitored_now(),
                )
                host.write_journal_once(completion_path, canonical_json_bytes(completion))
            else:
                assert isinstance(stored, QualificationBootstrapPrincipalCompletion)
                completion = stored
                if (
                    completion.request_id != request.request_id
                    or completion.plan_sha256 != plan.plan_sha256
                    or completion.intent_sha256 != intent.intent_sha256
                    or host.observe_principal(principal) != completion.application.observation
                ):
                    raise QualificationBootstrapError("principal completion differs")
                if completion.completed_at < last:
                    raise QualificationBootstrapError("principal completion time moved backwards")
                last = completion.completed_at
            principal_completions.append(completion)
        host.verify_pinned_inputs(completed=True)

        directory_completions: list[QualificationBootstrapDirectoryCompletion] = []
        for directory in plan.directories:
            intent = QualificationBootstrapDirectoryIntent(
                request_id=request.request_id,
                plan_sha256=plan.plan_sha256,
                directory=directory,
            )
            intent_path = request_root / f"directory-{directory.ordinal}.intent.json"
            completion_path = request_root / f"directory-{directory.ordinal}.completed.json"
            host.write_journal_once(intent_path, canonical_json_bytes(intent))
            stored = _journal_model(
                host.read_journal(completion_path),
                QualificationBootstrapDirectoryCompletion,
                label=f"directory {directory.ordinal} completion",
            )
            if stored is None:
                assert_clock_ready()
                application = host.ensure_directory(directory)
                inject(f"after_directory_apply:{directory.ordinal}")
                completion = QualificationBootstrapDirectoryCompletion(
                    request_id=request.request_id,
                    plan_sha256=plan.plan_sha256,
                    intent_sha256=intent.intent_sha256,
                    application=application,
                    completed_at=monitored_now(),
                )
                host.write_journal_once(completion_path, canonical_json_bytes(completion))
            else:
                assert isinstance(stored, QualificationBootstrapDirectoryCompletion)
                completion = stored
                if (
                    completion.request_id != request.request_id
                    or completion.plan_sha256 != plan.plan_sha256
                    or completion.intent_sha256 != intent.intent_sha256
                    or host.observe_directory(directory) != completion.application.observation
                ):
                    raise QualificationBootstrapError("directory completion differs")
                if completion.completed_at < last:
                    raise QualificationBootstrapError("directory completion time moved backwards")
                last = completion.completed_at
            directory_completions.append(completion)

        receipt = QualificationBootstrapReceiptV1(
            deployment_id=plan.deployment_id,
            request_id=request.request_id,
            request_sha256=canonical_sha256(request),
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            principal_completions=tuple(principal_completions),
            directory_completions=tuple(directory_completions),
            completed_at=monitored_now(),
        )
        host.write_journal_once(request_root / "receipt.json", canonical_json_bytes(receipt))
        inject("after_receipt")
        return receipt


def _fresh_file(
    path: str | Path,
    *,
    expected_sha256: str | None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    mode: int | None = None,
    preserve_missing: bool = False,
) -> tuple[bytes, os.stat_result]:
    value = Path(path)
    try:
        if value.resolve(strict=True) != value:
            raise QualificationBootstrapError("bootstrap file traverses a symlink")
        descriptor = os.open(
            value,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        if preserve_missing:
            raise
        raise QualificationBootstrapError("bootstrap file is missing") from None
    except OSError as exc:
        raise QualificationBootstrapError("bootstrap file cannot be opened") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_JOURNAL_BYTES
            or (owner_uid is not None and before.st_uid != owner_uid)
            or (owner_gid is not None and before.st_gid != owner_gid)
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        ):
            raise QualificationBootstrapError("bootstrap file custody differs")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_JOURNAL_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_JOURNAL_BYTES:
                raise QualificationBootstrapError("bootstrap file is oversized")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or total != before.st_size
        or (expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256)
    ):
        raise QualificationBootstrapError("bootstrap file changed or differs")
    return payload, after


class LinuxQualificationBootstrapHost:
    """Concrete root/Linux adapter for the disabled principal/root bootstrap."""

    def __init__(self, request: QualificationBootstrapRequestV1) -> None:
        self.request = QualificationBootstrapRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        self._journal_root = Path(self.request.journal_root)
        self._active_path, self._request_root = _journal_paths(self.request)

    @staticmethod
    def _assert_directory(
        path: Path,
        *,
        owner_uid: int,
        owner_gid: int,
        mode: int,
        label: str,
    ) -> os.stat_result:
        try:
            if path.resolve(strict=True) != path:
                raise QualificationBootstrapError(f"{label} traverses a symlink")
            observed = os.lstat(path)
        except OSError as exc:
            raise QualificationBootstrapError(f"{label} is unavailable") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != owner_uid
            or observed.st_gid != owner_gid
            or stat.S_IMODE(observed.st_mode) != mode
        ):
            raise QualificationBootstrapError(f"{label} custody differs")
        return observed

    def assert_linux_root(self) -> None:
        if not sys.platform.startswith("linux"):
            raise QualificationBootstrapError("qualification bootstrap requires Linux")
        if os.geteuid() != 0 or os.getegid() != 0:
            raise QualificationBootstrapError("qualification bootstrap requires root:root")
        self._assert_directory(
            self._journal_root,
            owner_uid=0,
            owner_gid=0,
            mode=0o700,
            label="bootstrap journal root",
        )

    @contextmanager
    def lock(self) -> Iterator[None]:
        descriptor = os.open(
            self._journal_root / "bootstrap.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != 0
                or observed.st_gid != 0
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o600
            ):
                raise QualificationBootstrapError("bootstrap lock custody differs")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _verify_tool(self, tool: QualificationExpectedRootExecutable) -> None:
        _fresh_file(
            tool.path,
            expected_sha256=tool.reviewed_sha256,
            owner_uid=tool.expected_owner_uid,
            owner_gid=tool.expected_owner_gid,
            mode=tool.expected_mode,
        )

    def _docker_group(self, *, completed: bool) -> grp.struct_group:
        try:
            value = grp.getgrnam(self.request.docker_group_name)
        except KeyError as exc:
            raise QualificationBootstrapError("pinned Docker group is missing") from exc
        expected_members = self.request.docker_group_expected_member_names
        members = tuple(sorted(set(value.gr_mem)))
        allowed = (
            expected_members
            if completed
            else tuple(item for item in expected_members if item in members)
        )
        if (
            value.gr_gid != self.request.deployment_spec.docker_gid
            or grp.getgrgid(value.gr_gid).gr_name != value.gr_name
            or members != allowed
        ):
            raise QualificationBootstrapError("pinned Docker group differs")
        return value

    def verify_pinned_inputs(self, *, completed: bool) -> None:
        for tool in (
            self.request.groupadd_executable,
            self.request.useradd_executable,
            self.request.nologin_executable,
        ):
            self._verify_tool(tool)
        self._docker_group(completed=completed)
        pin = self.request.postgresql_socket_directory
        observed = self._assert_directory(
            Path(pin.path),
            owner_uid=pin.owner_uid,
            owner_gid=pin.owner_gid,
            mode=pin.mode,
            label="PostgreSQL socket directory",
        )
        if (observed.st_dev, observed.st_ino) != (
            pin.device,
            pin.inode,
        ) or host_parent_chain_sha256(Path(pin.path)) != pin.parent_chain_sha256:
            raise QualificationBootstrapError("PostgreSQL socket directory identity differs")

    def _prepare_journal_parent(self, path: Path) -> None:
        if path == self._journal_root:
            return
        if path != self._request_root:
            raise QualificationBootstrapError("bootstrap journal escaped request root")
        try:
            os.mkdir(path, 0o700)
            parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except FileExistsError:
            pass
        except OSError as exc:
            raise QualificationBootstrapError(
                "bootstrap request journal cannot be created"
            ) from exc
        self._assert_directory(
            path,
            owner_uid=0,
            owner_gid=0,
            mode=0o700,
            label="bootstrap request journal",
        )

    def read_journal(self, path: Path) -> bytes | None:
        self._prepare_journal_parent(path.parent)
        try:
            return _fresh_file(
                path,
                expected_sha256=None,
                owner_uid=0,
                owner_gid=0,
                mode=0o400,
                preserve_missing=True,
            )[0]
        except FileNotFoundError:
            return None

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        self._prepare_journal_parent(path.parent)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            existing, _metadata = _fresh_file(
                path,
                expected_sha256=digest,
                owner_uid=0,
                owner_gid=0,
                mode=0o400,
            )
        except QualificationBootstrapError:
            if os.path.lexists(path):
                raise QualificationBootstrapError("bootstrap journal exact retry differs")
        else:
            if existing != payload:
                raise QualificationBootstrapError("bootstrap journal bytes differ")
            return
        staging = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
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
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise QualificationBootstrapError("bootstrap journal write made no progress")
                view = view[written:]
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if os.path.lexists(path):
                raise QualificationBootstrapError("bootstrap journal target appeared")
            os.replace(staging, path)
            parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _shadow_locked(user_name: str) -> bool:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                import spwd

                password = spwd.getspnam(user_name).sp_pwdp
        except (ImportError, KeyError, PermissionError) as exc:
            raise QualificationBootstrapError("service password lock cannot be observed") from exc
        return password.startswith(("!", "*"))

    def observe_principal(
        self,
        principal: QualificationBootstrapPrincipalV1,
    ) -> QualificationBootstrapPrincipalObservation:
        try:
            user = pwd.getpwnam(principal.user_name)
            primary = grp.getgrnam(principal.primary_group_name)
            by_uid = pwd.getpwuid(principal.uid)
            by_gid = grp.getgrgid(principal.gid)
        except KeyError as exc:
            raise QualificationBootstrapError("qualification service principal is missing") from exc
        supplementary = tuple(
            sorted(
                (item.gr_name, item.gr_gid)
                for item in grp.getgrall()
                if principal.user_name in item.gr_mem and item.gr_gid != principal.gid
            )
        )
        if (
            by_uid.pw_name != principal.user_name
            or by_gid.gr_name != principal.primary_group_name
            or primary.gr_mem
            or len(supplementary) != len(set(supplementary))
        ):
            raise QualificationBootstrapError("qualification principal NSS identity is rebound")
        return QualificationBootstrapPrincipalObservation(
            principal=principal,
            observed_user_name=user.pw_name,
            observed_primary_group_name=primary.gr_name,
            observed_uid=user.pw_uid,
            observed_gid=user.pw_gid,
            observed_supplementary_group_names=tuple(item[0] for item in supplementary),
            observed_supplementary_gids=tuple(sorted(item[1] for item in supplementary)),
            observed_home_directory=user.pw_dir,
            observed_login_shell=user.pw_shell,
            password_locked=self._shadow_locked(principal.user_name),
        )

    def _run_tool(
        self,
        tool: QualificationExpectedRootExecutable,
        arguments: tuple[str, ...],
    ) -> str:
        self._verify_tool(tool)
        argv = (tool.path, *arguments)
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise QualificationBootstrapError("bootstrap identity tool failed") from exc
        if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
            raise QualificationBootstrapError("bootstrap identity tool rejected exact request")
        return canonical_sha256(
            {
                "schema": "aletheia.qualification_bootstrap_command",
                "schema_version": 1,
                "executable_sha256": tool.reviewed_sha256,
                "argv": argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )

    def ensure_principal(
        self,
        principal: QualificationBootstrapPrincipalV1,
    ) -> QualificationBootstrapPrincipalApplication:
        commands: list[str] = []
        group_created = False
        user_created = False
        try:
            group = grp.getgrnam(principal.primary_group_name)
        except KeyError:
            try:
                grp.getgrgid(principal.gid)
            except KeyError:
                pass
            else:
                raise QualificationBootstrapError("bootstrap GID is already occupied") from None
            commands.append(
                self._run_tool(
                    self.request.groupadd_executable,
                    ("--gid", str(principal.gid), "--system", principal.primary_group_name),
                )
            )
            group_created = True
        else:
            if group.gr_gid != principal.gid or group.gr_mem:
                raise QualificationBootstrapError("existing primary group differs")
        try:
            pwd.getpwnam(principal.user_name)
        except KeyError:
            try:
                pwd.getpwuid(principal.uid)
            except KeyError:
                pass
            else:
                raise QualificationBootstrapError("bootstrap UID is already occupied") from None
            arguments = (
                "--uid",
                str(principal.uid),
                "--gid",
                principal.primary_group_name,
                *(self._user_groups_arguments(principal)),
                "--system",
                "--no-create-home",
                "--home-dir",
                principal.home_directory,
                "--shell",
                principal.login_shell,
                "--password",
                "!",
                principal.user_name,
            )
            commands.append(self._run_tool(self.request.useradd_executable, arguments))
            user_created = True
        observation = self.observe_principal(principal)
        return QualificationBootstrapPrincipalApplication(
            observation=observation,
            group_created=group_created,
            user_created=user_created,
            command_sha256s=tuple(commands),
        )

    @staticmethod
    def _user_groups_arguments(
        principal: QualificationBootstrapPrincipalV1,
    ) -> tuple[str, ...]:
        if not principal.supplementary_group_names:
            return ()
        return ("--groups", ",".join(principal.supplementary_group_names))

    @staticmethod
    def _assert_root_parent(path: Path) -> None:
        current = path
        while True:
            try:
                observed = os.lstat(current)
            except OSError as exc:
                raise QualificationBootstrapError("bootstrap parent is unavailable") from exc
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != 0
                or observed.st_gid != 0
                or stat.S_IMODE(observed.st_mode) & 0o022
            ):
                raise QualificationBootstrapError("bootstrap parent chain is not root-controlled")
            if current == Path("/"):
                return
            current = current.parent

    def observe_directory(
        self,
        directory: QualificationBootstrapDirectoryV1,
    ) -> QualificationBootstrapDirectoryObservation:
        observed = self._assert_directory(
            Path(directory.path),
            owner_uid=directory.owner_uid,
            owner_gid=directory.owner_gid,
            mode=directory.mode,
            label=f"{directory.purpose} bootstrap directory",
        )
        return QualificationBootstrapDirectoryObservation(
            directory=directory,
            device=observed.st_dev,
            inode=observed.st_ino,
            observed_owner_uid=observed.st_uid,
            observed_owner_gid=observed.st_gid,
            observed_mode=stat.S_IMODE(observed.st_mode),
            parent_chain_sha256=host_parent_chain_sha256(Path(directory.path)),
        )

    def ensure_directory(
        self,
        directory: QualificationBootstrapDirectoryV1,
    ) -> QualificationBootstrapDirectoryApplication:
        path = Path(directory.path)
        self._assert_root_parent(path.parent)
        created = False
        parent = -1
        descriptor = -1
        try:
            parent = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            metadata = os.fstat(descriptor)
            if path.resolve(strict=True) != path or not stat.S_ISDIR(metadata.st_mode):
                raise QualificationBootstrapError(
                    "bootstrap directory is not a canonical directory"
                )
            identity = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
            expected = (directory.owner_uid, directory.owner_gid, directory.mode)
            recoverable = {
                expected,
                (0, 0, 0o700),
                (directory.owner_uid, directory.owner_gid, 0o700),
            }
            if identity not in recoverable:
                raise QualificationBootstrapError("bootstrap directory already differs")
            if os.listdir(descriptor):
                raise QualificationBootstrapError("incomplete bootstrap directory is not empty")
            if identity != expected:
                os.fchown(descriptor, directory.owner_uid, directory.owner_gid)
                os.fchmod(descriptor, directory.mode)
                os.fsync(descriptor)
                os.fsync(parent)
        except QualificationBootstrapError:
            raise
        except OSError as exc:
            raise QualificationBootstrapError("bootstrap directory could not be created") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)
        observed = self.observe_directory(directory)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if os.listdir(descriptor):
                    raise QualificationBootstrapError("incomplete bootstrap directory is not empty")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise QualificationBootstrapError("bootstrap directory cannot be inspected") from exc
        return QualificationBootstrapDirectoryApplication(observation=observed, created=created)


def load_qualification_bootstrap_request(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> QualificationBootstrapRequestV1:
    payload, _metadata = _fresh_file(path, expected_sha256=expected_file_sha256)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload, object_pairs_hook=unique_object)
        request = QualificationBootstrapRequestV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationBootstrapError("qualification bootstrap request is invalid") from exc
    if payload != canonical_json_bytes(request):
        raise QualificationBootstrapError("qualification bootstrap request is not canonical")
    return request


def _emit(value: ExecutionModel) -> None:
    print(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def run_qualification_bootstrap_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge")
    args = parser.parse_args(argv)
    request = load_qualification_bootstrap_request(
        args.request,
        expected_file_sha256=args.request_sha256,
    )
    if not args.apply:
        _emit(build_qualification_bootstrap_plan(request))
        return 0
    if args.acknowledge != request.opt_in_confirmation:
        parser.error("--apply requires --acknowledge BOOTSTRAP_QUALIFICATION_ONLY_DISABLED")
    receipt = bootstrap_qualification_host(
        request,
        LinuxQualificationBootstrapHost(request),
    )
    _emit(receipt)
    return 0


__all__ = [
    "LinuxQualificationBootstrapHost",
    "QualificationBootstrapDirectoryApplication",
    "QualificationBootstrapDirectoryCompletion",
    "QualificationBootstrapDirectoryObservation",
    "QualificationBootstrapDirectoryV1",
    "QualificationBootstrapError",
    "QualificationBootstrapHostPort",
    "QualificationBootstrapPlanV1",
    "QualificationBootstrapPrincipalApplication",
    "QualificationBootstrapPrincipalCompletion",
    "QualificationBootstrapPrincipalObservation",
    "QualificationBootstrapPrincipalV1",
    "QualificationBootstrapReceiptV1",
    "QualificationBootstrapRequestV1",
    "QualificationBootstrapSocketDirectoryPinV1",
    "bootstrap_qualification_host",
    "build_qualification_bootstrap_plan",
    "load_qualification_bootstrap_request",
    "run_qualification_bootstrap_cli",
]
