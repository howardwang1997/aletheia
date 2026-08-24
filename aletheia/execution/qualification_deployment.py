"""Pure deployment artifacts for the qualification-only local execution worker.

This module deliberately stops before installation.  A portable deployment specification can be
rendered on any host, but only a real Linux observer may freeze or revalidate an installed
manifest.  Rendering a unit file or PostgreSQL ACL is therefore never deployment evidence and
never grants scientific authority.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.oci_deployment import (
    LoopbackQuotaProvisionerDeploymentPin,
    PinnedOCIImageLayout,
    PinnedRootExecutable,
    PinnedRootFile,
    SystemdWatchdogDeploymentPin,
)
from aletheia.execution.runtime_v2_contracts import PinnedOutputWorkspaceRoot
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.schemas import (
    ExecutionModel,
    canonical_json_bytes,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_POSTGRESQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_POSTGRESQL_IDENTITY_ARGUMENT = re.compile(r"^[a-z][a-z0-9_.]*(?: [a-z][a-z0-9_.]*)*(?:\[\])?$")
_SAFE_SYSTEMD_PATH = re.compile(r"^/[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_SAFE_RELATIVE_CODE_PATH = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_UNIT_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "workspace": re.compile(r"^aletheia-qualification-workspace(?:-[a-z0-9_.-]+)?[.]service$"),
    "quota": re.compile(r"^aletheia-qualification-output-quota(?:-[a-z0-9_.-]+)?[.]service$"),
    "watchdog": re.compile(r"^aletheia-qualification-oci-watchdog(?:-[a-z0-9_.-]+)?[.]service$"),
    "node": re.compile(r"^aletheia-qualification-node(?:-[a-z0-9_.-]+)?[.]service$"),
    "outbox": re.compile(r"^aletheia-qualification-outbox(?:-[a-z0-9_.-]+)?[.]service$"),
}
_MOUNT_NAMESPACE = re.compile(r"^mnt:\[[1-9][0-9]*\]$")
_LINUX_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OBSERVER_SIGNATURE_DOMAIN = b"aletheia.qualification-deployment-observation.v1\x00"

_CUSTODY_ROOT_PURPOSES = (
    "artifact_store",
    "authority_registry",
    "input_materialization_journal",
    "node_state",
    "outbox_spool",
    "workspace_source",
)

EXPECTED_EXECUTION_SCHEMA_REVISION = "20260827_0026"

EXECUTION_TABLES = (
    "execution_assignment_envelopes",
    "execution_attempt_adoptions",
    "execution_attempts",
    "execution_budget_authorizations",
    "execution_budget_events",
    "execution_budget_heads",
    "execution_budget_reservations",
    "execution_device_heads",
    "execution_device_leases",
    "execution_heads",
    "execution_inventory_attestations",
    "execution_inventory_devices",
    "execution_nodes",
    "execution_outbox",
    "execution_pre_runtime_absence_decisions",
    "execution_qualification_admissions",
    "execution_qualification_terminal_acceptances",
    "execution_qualification_terminal_deadline_expirations",
    "execution_qualification_terminal_outbox",
    "execution_resource_leases",
    "execution_runtime_fence_rebinds",
    "execution_runtime_launch_authorizations",
    "execution_runtime_launch_receipts",
    "execution_runtime_preparations",
    "execution_runtime_termination_acceptances",
    "execution_runtime_termination_challenges",
    "execution_terminal_receipts",
)

ALLOCATOR_UPDATE_TABLES = (
    "execution_attempts",
    "execution_budget_heads",
    "execution_budget_reservations",
    "execution_device_heads",
    "execution_device_leases",
    "execution_heads",
    "execution_nodes",
    "execution_resource_leases",
)

EXECUTION_SEQUENCES = ("execution_budget_events_event_id_seq",)

POSTGRESQL_DANGEROUS_BUILTIN_ROLES = (
    "pg_checkpoint",
    "pg_create_subscription",
    "pg_execute_server_program",
    "pg_maintain",
    "pg_monitor",
    "pg_read_all_data",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_read_server_files",
    "pg_signal_backend",
    "pg_stat_scan_tables",
    "pg_use_reserved_connections",
    "pg_write_all_data",
    "pg_write_server_files",
)


class QualificationDeploymentError(RuntimeError):
    """Base error for deployment artifact or observation conflicts."""


class QualificationDeploymentEnvironmentError(QualificationDeploymentError):
    """The current host cannot issue installed Linux deployment evidence."""


class QualificationDeploymentObservationError(QualificationDeploymentError):
    """A Linux installation observation differs from the frozen desired state."""


def _absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        len(value) > 4096
        or not path.is_absolute()
        or str(path) != value
        or value == "/"
        or _SAFE_SYSTEMD_PATH.fullmatch(value) is None
        or any(component in {"", ".", ".."} for component in value.split("/")[1:])
    ):
        raise ValueError(
            f"{label} must be one canonical systemd-safe absolute path without expansion"
        )
    return path


def _require_utc(timestamp: datetime, *, label: str) -> None:
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        or timestamp.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{label} must be timezone-aware UTC")


def _monitored_utc_now() -> datetime:
    """Return the verifier's monitored wall clock; tests replace this narrow boundary."""

    return datetime.now(timezone.utc)


def _canonical_postgresql_identifiers(
    values: tuple[str, ...], *, label: str, allow_public: bool = False
) -> tuple[str, ...]:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    for value in values:
        if allow_public and value == "PUBLIC":
            continue
        _quoted_identifier(value)
    return values


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _quoted_identifier(value: str) -> str:
    if _POSTGRESQL_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("PostgreSQL identifiers must be lowercase unquoted identifiers")
    return f'"{value}"'


def _canonical_routine_identity(value: str) -> str:
    match = re.fullmatch(r"([a-z][a-z0-9_]{0,62})\((.*)\)", value)
    if match is None:
        raise ValueError("PostgreSQL routine identity is not canonical")
    arguments = match.group(2)
    if arguments:
        values = tuple(arguments.split(", "))
        if ", ".join(values) != arguments or any(
            _POSTGRESQL_IDENTITY_ARGUMENT.fullmatch(argument) is None for argument in values
        ):
            raise ValueError("PostgreSQL routine identity is not canonical")
    return value


class QualificationExpectedRootFile(ExecutionModel):
    """Portable reviewed bytes and custody intent; never a live inode observation."""

    schema_name: Literal["aletheia.qualification_expected_root_file"] = (
        "aletheia.qualification_expected_root_file"
    )
    schema_version: Literal[1] = 1
    path: str
    reviewed_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_owner_uid: Literal[0] = 0
    expected_owner_gid: Literal[0] = 0
    expected_mode: int = Field(ge=0, le=0o7777)
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _expected_file_is_portable_and_read_only(self) -> "QualificationExpectedRootFile":
        _absolute_path(self.path, label="expected root file")
        if self.expected_mode & 0o7133 or self.expected_mode & 0o404 != 0o404:
            raise ValueError(
                "expected root file must be non-executable, worker-readable, and writable only "
                "by root"
            )
        return self


class QualificationExpectedRootExecutable(ExecutionModel):
    """Portable expected root executable without Linux device or inode identity."""

    schema_name: Literal["aletheia.qualification_expected_root_executable"] = (
        "aletheia.qualification_expected_root_executable"
    )
    schema_version: Literal[1] = 1
    path: str
    reviewed_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_owner_uid: Literal[0] = 0
    expected_owner_gid: Literal[0] = 0
    expected_mode: int = Field(ge=0, le=0o7777)
    executable: Literal[True] = True

    @model_validator(mode="after")
    def _expected_executable_is_portable_and_root_only(
        self,
    ) -> "QualificationExpectedRootExecutable":
        _absolute_path(self.path, label="expected root executable")
        if self.expected_mode & 0o505 != 0o505 or self.expected_mode & 0o7022:
            raise ValueError(
                "expected root executable must be worker-readable/executable and root-controlled"
            )
        return self


class QualificationObservedCustodyRoot(ExecutionModel):
    """One signed live directory identity for a mutable or immutable service root."""

    schema_name: Literal["aletheia.qualification_observed_custody_root"] = (
        "aletheia.qualification_observed_custody_root"
    )
    schema_version: Literal[1] = 1
    purpose: Literal[
        "artifact_store",
        "authority_registry",
        "input_materialization_journal",
        "node_state",
        "outbox_spool",
        "workspace_source",
    ]
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=0, le=2**31 - 1)
    owner_gid: int = Field(ge=0, le=2**31 - 1)
    mode: int = Field(ge=0, le=0o7777)
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_chain_root_controlled: Literal[True] = True
    file_type: Literal["directory"] = "directory"
    symlink: Literal[False] = False

    @model_validator(mode="after")
    def _root_is_canonical(self) -> "QualificationObservedCustodyRoot":
        _absolute_path(self.path, label=f"observed {self.purpose} custody root")
        return self


class ReviewedNativeDependencyFile(ExecutionModel):
    """Portable reviewed ELF interpreter or shared-object bytes."""

    path: str
    reviewed_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_owner_uid: Literal[0] = 0
    expected_owner_gid: Literal[0] = 0
    expected_mode: int = Field(ge=0, le=0o7777)
    executable_required: bool

    @model_validator(mode="after")
    def _native_file_is_worker_readable(self) -> "ReviewedNativeDependencyFile":
        _absolute_path(self.path, label="reviewed native dependency")
        required = 0o505 if self.executable_required else 0o404
        if self.expected_mode & 0o7022 or self.expected_mode & required != required:
            raise ValueError(
                "reviewed native dependency must be root-controlled and readable/executable by "
                "root and worker"
            )
        return self


class ReviewedNativeDependency(ExecutionModel):
    """One SONAME resolved to exact bytes and its own DT_NEEDED edges."""

    soname: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}$")
    file: ReviewedNativeDependencyFile
    needed_sonames: tuple[str, ...]

    @model_validator(mode="after")
    def _dependency_is_canonical(self) -> "ReviewedNativeDependency":
        if self.needed_sonames != tuple(sorted(set(self.needed_sonames))) or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}", value) is None
            for value in self.needed_sonames
        ):
            raise ValueError("reviewed native DT_NEEDED edges must be canonical")
        return self


def reviewed_native_dependency_closure_sha256(
    *,
    executable: QualificationExpectedRootExecutable,
    elf_interpreter: ReviewedNativeDependencyFile,
    executable_needed_sonames: tuple[str, ...],
    dependencies: tuple[ReviewedNativeDependency, ...],
) -> str:
    return canonical_sha256(
        {
            "schema": "aletheia.reviewed_native_dependency_closure",
            "schema_version": 1,
            "executable": executable,
            "elf_interpreter": elf_interpreter,
            "executable_needed_sonames": executable_needed_sonames,
            "dependencies": dependencies,
            "exhaustive": True,
            "external_native_dependencies_allowed": False,
        }
    )


class ReviewedNativeDependencyClosure(ExecutionModel):
    """Exhaustive portable PT_INTERP and transitive DT_NEEDED resolution."""

    schema_name: Literal["aletheia.reviewed_native_dependency_closure"] = (
        "aletheia.reviewed_native_dependency_closure"
    )
    schema_version: Literal[1] = 1
    executable: QualificationExpectedRootExecutable
    elf_interpreter: ReviewedNativeDependencyFile
    executable_needed_sonames: tuple[str, ...]
    dependencies: tuple[ReviewedNativeDependency, ...] = Field(min_length=1)
    exhaustive: Literal[True] = True
    external_native_dependencies_allowed: Literal[False] = False
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closure_is_exhaustive(self) -> "ReviewedNativeDependencyClosure":
        if not self.elf_interpreter.executable_required:
            raise ValueError("reviewed ELF interpreter must be executable")
        if self.executable_needed_sonames != tuple(
            sorted(set(self.executable_needed_sonames))
        ):
            raise ValueError("reviewed executable DT_NEEDED edges must be canonical")
        sonames = tuple(dependency.soname for dependency in self.dependencies)
        if sonames != tuple(sorted(set(sonames))):
            raise ValueError("reviewed native dependencies must be canonical")
        resolved = set(sonames)
        if not set(self.executable_needed_sonames).issubset(resolved) or any(
            not set(dependency.needed_sonames).issubset(resolved)
            for dependency in self.dependencies
        ):
            raise ValueError("reviewed native dependency graph has an unresolved DT_NEEDED edge")
        paths = (
            self.executable.path,
            self.elf_interpreter.path,
            *(dependency.file.path for dependency in self.dependencies),
        )
        if len(set(paths)) != len(paths):
            raise ValueError("reviewed native closure paths must be distinct")
        expected = reviewed_native_dependency_closure_sha256(
            executable=self.executable,
            elf_interpreter=self.elf_interpreter,
            executable_needed_sonames=self.executable_needed_sonames,
            dependencies=self.dependencies,
        )
        if self.manifest_sha256 != expected:
            raise ValueError("reviewed native dependency closure hash is not derived")
        return self


class QualificationReviewedCodeFile(ExecutionModel):
    """One exhaustive regular-file entry in the portable reviewed code tree."""

    relative_path: str
    reviewed_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=0, le=2**63 - 1)
    expected_owner_uid: Literal[0] = 0
    expected_owner_gid: Literal[0] = 0
    expected_mode: int = Field(ge=0, le=0o7777)
    file_type: Literal["regular"] = "regular"

    @model_validator(mode="after")
    def _entry_is_canonical(self) -> "QualificationReviewedCodeFile":
        components = self.relative_path.split("/")
        if (
            _SAFE_RELATIVE_CODE_PATH.fullmatch(self.relative_path) is None
            or any(component in {"", ".", ".."} for component in components)
            or self.expected_mode & 0o7022
            or self.expected_mode & 0o404 != 0o404
        ):
            raise ValueError("reviewed code file is not a canonical root-controlled regular file")
        return self


class QualificationReviewedCodeDirectory(ExecutionModel):
    """One nested root-owned directory required to make the tree non-mutable."""

    relative_path: str
    expected_owner_uid: Literal[0] = 0
    expected_owner_gid: Literal[0] = 0
    expected_mode: int = Field(ge=0, le=0o7777)
    file_type: Literal["directory"] = "directory"

    @model_validator(mode="after")
    def _directory_is_canonical(self) -> "QualificationReviewedCodeDirectory":
        components = self.relative_path.split("/")
        if (
            _SAFE_RELATIVE_CODE_PATH.fullmatch(self.relative_path) is None
            or any(component in {"", ".", ".."} for component in components)
            or self.expected_mode & 0o7022
            or self.expected_mode & 0o505 != 0o505
        ):
            raise ValueError("reviewed code directory is not canonical root-controlled custody")
        return self


def reviewed_code_tree_manifest_sha256(
    *,
    root_path: str,
    directories: tuple[QualificationReviewedCodeDirectory, ...],
    entries: tuple[QualificationReviewedCodeFile, ...],
    expected_root_mode: int,
) -> str:
    """Hash the exhaustive no-symlink tree manifest, not live inode metadata."""

    _absolute_path(root_path, label="reviewed code root")
    return canonical_sha256(
        {
            "schema": "aletheia.qualification_reviewed_code_tree_manifest",
            "schema_version": 1,
            "root_path": root_path,
            "expected_root_owner_uid": 0,
            "expected_root_owner_gid": 0,
            "expected_root_mode": expected_root_mode,
            "symlinks_allowed": False,
            "directories": directories,
            "entries": entries,
        }
    )


class QualificationReviewedCodeTree(ExecutionModel):
    """Exhaustive portable code-root manifest; live device/inode data is deliberately absent."""

    schema_name: Literal["aletheia.qualification_reviewed_code_tree"] = (
        "aletheia.qualification_reviewed_code_tree"
    )
    schema_version: Literal[1] = 1
    root_path: str
    expected_root_owner_uid: Literal[0] = 0
    expected_root_owner_gid: Literal[0] = 0
    expected_root_mode: int = Field(ge=0, le=0o7777)
    symlinks_allowed: Literal[False] = False
    exhaustive: Literal[True] = True
    directories: tuple[QualificationReviewedCodeDirectory, ...]
    entries: tuple[QualificationReviewedCodeFile, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _manifest_is_exhaustive_and_derived(self) -> "QualificationReviewedCodeTree":
        _absolute_path(self.root_path, label="reviewed code root")
        if self.expected_root_mode & 0o7022 or self.expected_root_mode & 0o505 != 0o505:
            raise ValueError(
                "reviewed tree root must remain root controlled and worker-traversable"
            )
        paths = tuple(entry.relative_path for entry in self.entries)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("reviewed code files must be unique and canonically ordered")
        directory_paths = tuple(directory.relative_path for directory in self.directories)
        if directory_paths != tuple(sorted(set(directory_paths))):
            raise ValueError("reviewed code directories must be unique and canonically ordered")
        directory_set = set(directory_paths)
        if set(paths).intersection(directory_set) or any(
            str(Path(path).parent) not in {".", *directory_set}
            for path in (*paths, *directory_paths)
        ):
            raise ValueError(
                "reviewed code tree must enumerate every nested directory and disjoint file"
            )
        expected = reviewed_code_tree_manifest_sha256(
            root_path=self.root_path,
            directories=self.directories,
            entries=self.entries,
            expected_root_mode=self.expected_root_mode,
        )
        if self.manifest_sha256 != expected:
            raise ValueError("reviewed code-tree manifest hash is not derived from its entries")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(entry.byte_length for entry in self.entries)


def qualification_agent_implementation_sha256(
    *,
    reviewed_code_tree: QualificationReviewedCodeTree,
    reviewed_python_environment: QualificationReviewedCodeTree,
    expected_python_executable: QualificationExpectedRootExecutable,
    expected_runners: tuple[QualificationExpectedRootFile, ...],
    expected_service_modules: tuple[QualificationExpectedRootFile, ...],
    expected_python_import_paths: tuple[str, ...],
) -> str:
    """Derive identity from both exhaustive import trees and every direct entrypoint."""

    return canonical_sha256(
        {
            "schema": "aletheia.qualification_agent_implementation",
            "schema_version": 1,
            "reviewed_code_tree_manifest_sha256": reviewed_code_tree.manifest_sha256,
            "reviewed_python_environment_manifest_sha256": (
                reviewed_python_environment.manifest_sha256
            ),
            "expected_python_executable": expected_python_executable,
            "expected_runners": expected_runners,
            "expected_service_modules": expected_service_modules,
            "expected_python_import_paths": expected_python_import_paths,
        }
    )


class PostgreSQLExpectedRoutine(ExecutionModel):
    """One exact public routine definition in the migration-owned catalog projection."""

    routine_kind: Literal["function", "procedure"]
    routine_schema: Literal["public"]
    execution_owned: Literal[True]
    routine_name: str
    identity_argument_types: tuple[str, ...]
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    language: str
    security_definer: bool
    configuration: tuple[str, ...]
    volatility: Literal["immutable", "stable", "volatile"]

    @model_validator(mode="after")
    def _signature_is_canonical(self) -> "PostgreSQLExpectedRoutine":
        _quoted_identifier(self.routine_name)
        if any(
            _POSTGRESQL_IDENTITY_ARGUMENT.fullmatch(argument) is None
            for argument in self.identity_argument_types
        ):
            raise ValueError("PostgreSQL routine identity argument types are not canonical")
        _quoted_identifier(self.language)
        if self.configuration != tuple(sorted(set(self.configuration))) or any(
            not value
            or len(value) > 1024
            or any(character in value for character in ("\x00", "\n", "\r"))
            for value in self.configuration
        ):
            raise ValueError("PostgreSQL routine configuration must be canonical")
        return self

    @property
    def identity(self) -> str:
        return f"{self.routine_name}({', '.join(self.identity_argument_types)})"

    @property
    def sql_reference(self) -> str:
        arguments = ", ".join(self.identity_argument_types)
        return f"{self.routine_schema}.{_quoted_identifier(self.routine_name)}({arguments})"


class PostgreSQLExpectedTrigger(ExecutionModel):
    """One exact execution-table trigger definition and enabled state."""

    table_name: str
    trigger_name: str
    function_identity: str
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    enabled: Literal["origin", "disabled", "replica", "always"]

    @model_validator(mode="after")
    def _trigger_is_canonical(self) -> "PostgreSQLExpectedTrigger":
        _quoted_identifier(self.table_name)
        _quoted_identifier(self.trigger_name)
        _canonical_routine_identity(self.function_identity)
        return self


class PostgreSQLExpectedSequenceConfiguration(ExecutionModel):
    """One exact execution sequence configuration from ``pg_sequence``."""

    sequence_name: str
    data_type: Literal["smallint", "integer", "bigint"]
    persistence: Literal["permanent", "unlogged", "temporary"]
    start_value: int
    minimum_value: int
    maximum_value: int
    increment_by: int
    cache_size: int = Field(ge=1)
    cycles: bool
    owned_by_table: str | None
    owned_by_column: str | None

    @model_validator(mode="after")
    def _sequence_is_canonical(self) -> "PostgreSQLExpectedSequenceConfiguration":
        _quoted_identifier(self.sequence_name)
        if (self.owned_by_table is None) != (self.owned_by_column is None):
            raise ValueError("PostgreSQL sequence OWNED BY projection is incomplete")
        if self.owned_by_table is not None:
            _quoted_identifier(self.owned_by_table)
            _quoted_identifier(self.owned_by_column or "")
        if (
            self.increment_by == 0
            or self.minimum_value >= self.maximum_value
            or not self.minimum_value <= self.start_value <= self.maximum_value
        ):
            raise ValueError("PostgreSQL sequence configuration is internally inconsistent")
        return self


class QualificationDeploymentSpecV1(ExecutionModel):
    """Portable desired state; it contains no claim about a live Linux installation."""

    schema_name: Literal["aletheia.qualification_deployment_spec"] = (
        "aletheia.qualification_deployment_spec"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_cpu_architecture: str = Field(pattern=r"^[a-z0-9_]+$")
    expected_oci_platform: str = Field(pattern=r"^linux/[a-z0-9_]+$")

    node_uid: int = Field(ge=1, le=2**31 - 1)
    node_gid: int = Field(ge=1, le=2**31 - 1)
    docker_gid: int = Field(ge=1, le=2**31 - 1)
    outbox_uid: int = Field(ge=1, le=2**31 - 1)
    outbox_gid: int = Field(ge=1, le=2**31 - 1)

    python_executable: str
    expected_python_executable: QualificationExpectedRootExecutable
    reviewed_python_environment: QualificationReviewedCodeTree
    expected_python_import_paths: tuple[str, ...] = Field(min_length=1)
    code_root: str
    reviewed_code_tree: QualificationReviewedCodeTree
    deployment_manifest_path: str
    deployment_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_deployment_manifest: QualificationExpectedRootFile
    workspace_source_root: str
    output_workspace_root: str
    quota_backing_root: str
    quota_state_root: str
    quota_socket_path: str
    watchdog_state_root: str
    watchdog_socket_path: str
    runtime_journal_root: str
    node_state_root: str
    artifact_store_root: str
    input_materialization_journal_root: str
    authority_registry_root: str
    oci_layout_root: str
    outbox_spool_root: str
    seccomp_profile_path: str
    expected_seccomp_profile: QualificationExpectedRootFile
    apparmor_profile_path: str
    apparmor_profile_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    workspace_runner_path: str
    expected_workspace_runner: QualificationExpectedRootFile
    quota_runner_path: str
    expected_quota_runner: QualificationExpectedRootFile
    watchdog_runner_path: str
    expected_watchdog_runner: QualificationExpectedRootFile
    node_runner_path: str
    expected_node_runner: QualificationExpectedRootFile
    outbox_runner_path: str
    expected_outbox_runner: QualificationExpectedRootFile
    expected_quota_service_module: QualificationExpectedRootFile
    expected_watchdog_service_module: QualificationExpectedRootFile
    expected_losetup_executable: QualificationExpectedRootExecutable
    expected_mkfs_ext4_executable: QualificationExpectedRootExecutable
    expected_mount_executable: QualificationExpectedRootExecutable
    reviewed_privileged_tool_native_closures: tuple[
        ReviewedNativeDependencyClosure, ...
    ] = Field(min_length=3, max_length=3)
    systemd_unit_root: str = "/etc/systemd/system"
    workspace_unit_name: str
    quota_unit_name: str
    watchdog_unit_name: str
    node_unit_name: str
    outbox_unit_name: str

    postgresql_database: str
    postgresql_schema: Literal["public"] = "public"
    postgresql_owner_role: str
    postgresql_allocator_role: str
    postgresql_outbox_role: str
    postgresql_execution_routine_name_prefix: Literal["aletheia_execution_"] = (
        "aletheia_execution_"
    )
    expected_postgresql_routines: tuple[PostgreSQLExpectedRoutine, ...] = Field(min_length=1)
    expected_postgresql_triggers: tuple[PostgreSQLExpectedTrigger, ...] = Field(min_length=1)
    expected_postgresql_sequences: tuple[PostgreSQLExpectedSequenceConfiguration, ...] = Field(
        min_length=1
    )
    expected_schema_revision: Literal[EXPECTED_EXECUTION_SCHEMA_REVISION] = (
        EXPECTED_EXECUTION_SCHEMA_REVISION
    )

    agent_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_quota_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    docker_security_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    postgresql_server_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    seccomp_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    apparmor_profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    worker_poll_milliseconds: int = Field(default=250, ge=50, le=60_000)
    maximum_active_watchdog_jobs: int = Field(default=4096, ge=1, le=1_000_000)
    maximum_observation_duration_seconds: int = Field(default=10, ge=1, le=60)
    observation_ttl_seconds: int = Field(default=30, ge=1, le=300)
    automatic_installation: Literal[False] = False
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _desired_state_is_closed(self) -> "QualificationDeploymentSpecV1":
        path_fields = (
            "python_executable",
            "reviewed_python_environment",
            "code_root",
            "deployment_manifest_path",
            "workspace_source_root",
            "output_workspace_root",
            "quota_backing_root",
            "quota_state_root",
            "quota_socket_path",
            "watchdog_state_root",
            "watchdog_socket_path",
            "runtime_journal_root",
            "node_state_root",
            "artifact_store_root",
            "input_materialization_journal_root",
            "authority_registry_root",
            "oci_layout_root",
            "outbox_spool_root",
            "seccomp_profile_path",
            "apparmor_profile_path",
            "workspace_runner_path",
            "quota_runner_path",
            "watchdog_runner_path",
            "node_runner_path",
            "outbox_runner_path",
            "systemd_unit_root",
        )
        paths = {
            field: _absolute_path(
                (
                    getattr(self, field).root_path
                    if field == "reviewed_python_environment"
                    else getattr(self, field)
                ),
                label=field,
            )
            for field in path_fields
        }
        runners = tuple(
            paths[field]
            for field in (
                "workspace_runner_path",
                "quota_runner_path",
                "watchdog_runner_path",
                "node_runner_path",
                "outbox_runner_path",
            )
        )
        if len(set(runners)) != len(runners) or any(
            paths["code_root"] not in runner.parents for runner in runners
        ):
            raise ValueError("deployment runners must be distinct children of the frozen code root")
        expected_files = {
            "python_executable": self.expected_python_executable,
            "workspace_runner_path": self.expected_workspace_runner,
            "quota_runner_path": self.expected_quota_runner,
            "watchdog_runner_path": self.expected_watchdog_runner,
            "node_runner_path": self.expected_node_runner,
            "outbox_runner_path": self.expected_outbox_runner,
        }
        if any(
            pin.path != str(paths[field])
            or _absolute_path(pin.path, label=f"expected {field}") != paths[field]
            for field, pin in expected_files.items()
        ):
            raise ValueError("expected Python and runner pins must bind their exact rendered paths")
        if len({pin.path for pin in expected_files.values()}) != len(expected_files):
            raise ValueError("expected Python and runner pins must bind distinct paths")
        if (
            self.expected_deployment_manifest.path != self.deployment_manifest_path
            or self.expected_deployment_manifest.reviewed_sha256 != self.deployment_manifest_sha256
        ):
            raise ValueError("expected deployment manifest must bind its exact path and digest")
        if (
            self.expected_seccomp_profile.path != self.seccomp_profile_path
            or self.expected_seccomp_profile.reviewed_sha256 != self.seccomp_profile_sha256
        ):
            raise ValueError("expected seccomp profile must bind its exact path and digest")
        if self.expected_python_executable.path != self.python_executable:
            raise ValueError("expected Python executable must bind the rendered interpreter")
        python_environment_root = paths["reviewed_python_environment"]
        if python_environment_root not in paths["python_executable"].parents:
            raise ValueError("Python executable must be a child of its exhaustive environment")
        if self.expected_python_import_paths != tuple(
            dict.fromkeys(self.expected_python_import_paths)
        ):
            raise ValueError("expected Python import paths must be unique and ordered")
        for import_path_value in self.expected_python_import_paths:
            import_path = _absolute_path(import_path_value, label="expected Python import path")
            containing_tree = next(
                (
                    tree
                    for tree in (self.reviewed_code_tree, self.reviewed_python_environment)
                    if import_path == Path(tree.root_path)
                    or Path(tree.root_path) in import_path.parents
                ),
                None,
            )
            if containing_tree is None:
                raise ValueError("expected Python import paths must stay inside reviewed trees")
            if import_path != Path(containing_tree.root_path) and str(
                import_path.relative_to(containing_tree.root_path)
            ) not in {directory.relative_path for directory in containing_tree.directories}:
                raise ValueError("expected Python import paths must name reviewed directories")
        python_entry = {
            entry.relative_path: entry for entry in self.reviewed_python_environment.entries
        }.get(str(paths["python_executable"].relative_to(python_environment_root)))
        if (
            python_entry is None
            or python_entry.reviewed_sha256 != self.expected_python_executable.reviewed_sha256
            or python_entry.expected_mode != self.expected_python_executable.expected_mode
        ):
            raise ValueError("expected Python must be an exact reviewed environment entry")
        if self.reviewed_code_tree.root_path != str(paths["code_root"]):
            raise ValueError("reviewed code-tree root must equal the rendered code root")
        reviewed_entries = {entry.relative_path: entry for entry in self.reviewed_code_tree.entries}
        expected_runners = (
            self.expected_workspace_runner,
            self.expected_quota_runner,
            self.expected_watchdog_runner,
            self.expected_node_runner,
            self.expected_outbox_runner,
        )
        for runner in expected_runners:
            relative_path = str(Path(runner.path).relative_to(paths["code_root"]))
            reviewed = reviewed_entries.get(relative_path)
            if (
                reviewed is None
                or reviewed.reviewed_sha256 != runner.reviewed_sha256
                or reviewed.expected_mode != runner.expected_mode
            ):
                raise ValueError(
                    "every expected runner must be an exact entry in the exhaustive code tree"
                )
        expected_modules = (
            self.expected_quota_service_module,
            self.expected_watchdog_service_module,
        )
        for service_module in expected_modules:
            module_path = _absolute_path(service_module.path, label="expected service module")
            if paths["code_root"] not in module_path.parents:
                raise ValueError("expected service module must be a child of the code root")
            reviewed = reviewed_entries.get(str(module_path.relative_to(paths["code_root"])))
            if (
                reviewed is None
                or reviewed.reviewed_sha256 != service_module.reviewed_sha256
                or reviewed.expected_mode != service_module.expected_mode
            ):
                raise ValueError("every expected service module must be an exact code-tree entry")
        tool_paths = tuple(
            _absolute_path(pin.path, label="expected privileged tool")
            for pin in (
                self.expected_losetup_executable,
                self.expected_mkfs_ext4_executable,
                self.expected_mount_executable,
            )
        )
        if len(set(tool_paths)) != len(tool_paths) or any(
            _paths_overlap(tool, paths["code_root"])
            or _paths_overlap(tool, python_environment_root)
            for tool in tool_paths
        ):
            raise ValueError("expected privileged tools must be distinct from reviewed trees")
        native_closure_paths = tuple(
            closure.executable.path for closure in self.reviewed_privileged_tool_native_closures
        )
        if native_closure_paths != tuple(sorted(set(native_closure_paths))):
            raise ValueError("reviewed privileged native closures must be canonical")
        native_closures = {
            Path(closure.executable.path): closure
            for closure in self.reviewed_privileged_tool_native_closures
        }
        expected_tools = {
            Path(pin.path): pin
            for pin in (
                self.expected_losetup_executable,
                self.expected_mkfs_ext4_executable,
                self.expected_mount_executable,
            )
        }
        if set(native_closures) != set(expected_tools) or any(
            native_closures[path].executable != expected
            for path, expected in expected_tools.items()
        ):
            raise ValueError(
                "reviewed native dependency closures must bind all privileged tools exactly"
            )
        derived_implementation = qualification_agent_implementation_sha256(
            reviewed_code_tree=self.reviewed_code_tree,
            reviewed_python_environment=self.reviewed_python_environment,
            expected_python_executable=self.expected_python_executable,
            expected_runners=expected_runners,
            expected_service_modules=expected_modules,
            expected_python_import_paths=self.expected_python_import_paths,
        )
        if self.agent_implementation_sha256 != derived_implementation:
            raise ValueError(
                "agent implementation hash must derive from code tree, Python, and runners"
            )

        custody_roots = tuple(
            paths[field]
            for field in (
                "workspace_source_root",
                "output_workspace_root",
                "quota_backing_root",
                "quota_state_root",
                "watchdog_state_root",
                "runtime_journal_root",
                "node_state_root",
                "artifact_store_root",
                "input_materialization_journal_root",
                "authority_registry_root",
                "oci_layout_root",
                "outbox_spool_root",
            )
        ) + (paths["quota_socket_path"].parent, paths["watchdog_socket_path"].parent)
        if any(
            _paths_overlap(left, right)
            for index, left in enumerate(custody_roots)
            for right in custody_roots[index + 1 :]
        ):
            raise ValueError("deployment custody roots must not overlap")
        if any(
            _paths_overlap(reviewed_root, root)
            for reviewed_root in (paths["code_root"], python_environment_root)
            for root in custody_roots
        ):
            raise ValueError("deployment code/Python and custody roots must not overlap")
        if _paths_overlap(paths["code_root"], python_environment_root):
            raise ValueError("reviewed code and Python environment roots must not overlap")
        manifest = paths["deployment_manifest_path"]
        if any(
            _paths_overlap(manifest, root)
            for root in (*custody_roots, paths["code_root"], python_environment_root)
        ):
            raise ValueError(
                "deployment manifest path must not overlap code or worker custody roots"
            )
        if any(
            _paths_overlap(paths["systemd_unit_root"], root)
            for root in (*custody_roots, paths["code_root"], python_environment_root)
        ):
            raise ValueError("systemd unit and deployment custody roots must not overlap")

        unit_names = {
            "workspace": self.workspace_unit_name,
            "quota": self.quota_unit_name,
            "watchdog": self.watchdog_unit_name,
            "node": self.node_unit_name,
            "outbox": self.outbox_unit_name,
        }
        if len(set(unit_names.values())) != len(unit_names):
            raise ValueError("deployment systemd unit names must be distinct")
        for purpose, name in unit_names.items():
            if _UNIT_PATTERNS[purpose].fullmatch(name) is None:
                raise ValueError(f"{purpose} systemd unit name is not deployment-scoped")

        if (
            self.node_uid == self.outbox_uid
            or self.node_gid == self.outbox_gid
            or self.docker_gid in {self.node_gid, self.outbox_gid}
        ):
            raise ValueError("node, Docker, and outbox UID/GID identities must be distinct")
        roles = (
            self.postgresql_owner_role,
            self.postgresql_allocator_role,
            self.postgresql_outbox_role,
        )
        if len(set(roles)) != len(roles):
            raise ValueError("PostgreSQL deployment roles must be distinct")
        for role in roles:
            _quoted_identifier(role)
        _quoted_identifier(self.postgresql_database)
        routine_keys = tuple(
            (routine.routine_kind, routine.routine_name, routine.identity_argument_types)
            for routine in self.expected_postgresql_routines
        )
        if routine_keys != tuple(sorted(set(routine_keys))):
            raise ValueError("expected PostgreSQL routines must be unique and canonically ordered")
        if any(
            not routine.routine_name.startswith(self.postgresql_execution_routine_name_prefix)
            for routine in self.expected_postgresql_routines
        ):
            raise ValueError("expected PostgreSQL routines must stay in the execution namespace")
        if len({routine.identity for routine in self.expected_postgresql_routines}) != len(
            self.expected_postgresql_routines
        ):
            raise ValueError("expected PostgreSQL routine identities must be globally unique")
        trigger_keys = tuple(
            (trigger.table_name, trigger.trigger_name)
            for trigger in self.expected_postgresql_triggers
        )
        if trigger_keys != tuple(sorted(set(trigger_keys))) or any(
            trigger.table_name not in EXECUTION_TABLES
            or trigger.function_identity
            not in {routine.identity for routine in self.expected_postgresql_routines}
            for trigger in self.expected_postgresql_triggers
        ):
            raise ValueError("expected PostgreSQL trigger projection must be exact and canonical")
        sequence_names = tuple(
            sequence.sequence_name for sequence in self.expected_postgresql_sequences
        )
        if sequence_names != tuple(sorted(set(sequence_names))) or set(sequence_names) != set(
            EXECUTION_SEQUENCES
        ):
            raise ValueError("expected PostgreSQL sequence projection must cover exact sequences")
        if any(
            sequence.persistence != "permanent"
            or sequence.owned_by_table not in EXECUTION_TABLES
            or sequence.owned_by_column is None
            for sequence in self.expected_postgresql_sequences
        ):
            raise ValueError(
                "expected PostgreSQL sequences must be permanent and owned by execution columns"
            )
        if self.apparmor_profile_name.lower() == "unconfined":
            raise ValueError("qualification deployment cannot select unconfined AppArmor")
        if self.maximum_observation_duration_seconds > self.observation_ttl_seconds:
            raise ValueError("observation duration cannot exceed signed observation TTL")
        return self

    @property
    def spec_sha256(self) -> str:
        return canonical_sha256(self)


class RenderedSystemdUnit(ExecutionModel):
    """One deterministic candidate unit; candidate bytes are not installation evidence."""

    schema_name: Literal["aletheia.rendered_qualification_systemd_unit"] = (
        "aletheia.rendered_qualification_systemd_unit"
    )
    schema_version: Literal[1] = 1
    unit_name: str
    path: str
    owner_uid: Literal[0] = 0
    owner_gid: Literal[0] = 0
    mode: Literal[0o444] = 0o444
    content: str = Field(min_length=1, max_length=64 * 1024)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _content_is_exact(self) -> "RenderedSystemdUnit":
        path = _absolute_path(self.path, label="rendered systemd unit")
        if path.name != self.unit_name or not self.content.endswith("\n"):
            raise ValueError("rendered systemd unit path or final newline is not canonical")
        expected = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("rendered systemd unit content hash differs from exact bytes")
        return self


def _systemd_unit(
    spec: QualificationDeploymentSpecV1, name: str, lines: tuple[str, ...]
) -> RenderedSystemdUnit:
    content = "\n".join(lines) + "\n"
    return RenderedSystemdUnit(
        unit_name=name,
        path=str(Path(spec.systemd_unit_root) / name),
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _exec_start(spec: QualificationDeploymentSpecV1, runner: str, operation: str) -> str:
    return "ExecStart=" + " ".join(_exec_start_argv(spec, runner, operation))


def _exec_start_argv(
    spec: QualificationDeploymentSpecV1, runner: str, operation: str
) -> tuple[str, ...]:
    argv = (
        spec.python_executable,
        "-S",
        "-s",
        "-P",
        runner,
        "--manifest",
        spec.deployment_manifest_path,
        operation,
    )
    if runner == spec.node_runner_path and operation == "run":
        return (*argv, "--poll-milliseconds", str(spec.worker_poll_milliseconds))
    return argv


def _python_environment_assignments(spec: QualificationDeploymentSpecV1) -> tuple[str, ...]:
    return (
        f"PYTHONHOME={spec.reviewed_python_environment.root_path}",
        f"PYTHONPATH={spec.code_root}",
        "PYTHONNOUSERSITE=1",
        "PYTHONSAFEPATH=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONHASHSEED=0",
    )


_PYTHON_UNSET_ENVIRONMENT_NAMES = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONBREAKPOINT",
    "PYTHONINSPECT",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
)


def _python_service_environment(spec: QualificationDeploymentSpecV1) -> tuple[str, ...]:
    """Close Python imports over reviewed code and runtime trees."""

    return (
        f"WorkingDirectory={spec.code_root}",
        *(f"Environment={value}" for value in _python_environment_assignments(spec)),
        "UnsetEnvironment=" + " ".join(_PYTHON_UNSET_ENVIRONMENT_NAMES),
    )


def render_systemd_units(
    spec: QualificationDeploymentSpecV1,
) -> tuple[RenderedSystemdUnit, ...]:
    """Render exact candidate systemd bytes without touching systemd or the filesystem."""

    spec = QualificationDeploymentSpecV1.model_validate(spec.model_dump(mode="python"))
    common_root = (
        "UMask=0077",
        "NoNewPrivileges=yes",
        "PrivateMounts=no",
        "PrivateDevices=no",
        "RestrictAddressFamilies=AF_UNIX",
    )
    python_environment = _python_service_environment(spec)
    workspace = _systemd_unit(
        spec,
        spec.workspace_unit_name,
        (
            "[Unit]",
            "Description=Aletheia qualification shared output workspace",
            "Before=docker.service " + spec.quota_unit_name + " " + spec.node_unit_name,
            "",
            "[Service]",
            "Type=oneshot",
            "User=root",
            "Group=root",
            "SupplementaryGroups=",
            *python_environment,
            *common_root,
            "CapabilityBoundingSet=CAP_SYS_ADMIN",
            "AmbientCapabilities=CAP_SYS_ADMIN",
            _exec_start(spec, spec.workspace_runner_path, "ensure-shared-workspace"),
            "RemainAfterExit=yes",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )
    quota = _systemd_unit(
        spec,
        spec.quota_unit_name,
        (
            "[Unit]",
            "Description=Aletheia qualification loopback output quota service",
            f"Requires={spec.workspace_unit_name}",
            f"After={spec.workspace_unit_name}",
            f"Before={spec.node_unit_name}",
            "",
            "[Service]",
            "Type=simple",
            "User=root",
            "Group=root",
            "SupplementaryGroups=",
            *python_environment,
            *common_root,
            "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SYS_ADMIN",
            "AmbientCapabilities=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SYS_ADMIN",
            _exec_start(spec, spec.quota_runner_path, "serve"),
            "Restart=on-failure",
            "RestartSec=1s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )
    watchdog = _systemd_unit(
        spec,
        spec.watchdog_unit_name,
        (
            "[Unit]",
            "Description=Aletheia qualification independent deadline watchdog",
            "Requires=docker.service",
            "After=docker.service",
            f"Before={spec.node_unit_name}",
            "",
            "[Service]",
            "Type=simple",
            "User=root",
            "Group=root",
            "SupplementaryGroups=",
            *python_environment,
            *common_root,
            "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_KILL CAP_SYS_ADMIN",
            "AmbientCapabilities=CAP_CHOWN CAP_DAC_OVERRIDE CAP_KILL CAP_SYS_ADMIN",
            _exec_start(spec, spec.watchdog_runner_path, "serve"),
            "Restart=on-failure",
            "RestartSec=1s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )
    node = _systemd_unit(
        spec,
        spec.node_unit_name,
        (
            "[Unit]",
            "Description=Aletheia qualification-only local node worker",
            (
                "Requires=docker.service "
                + spec.workspace_unit_name
                + " "
                + spec.quota_unit_name
                + " "
                + spec.watchdog_unit_name
            ),
            (
                "After=docker.service "
                + spec.workspace_unit_name
                + " "
                + spec.quota_unit_name
                + " "
                + spec.watchdog_unit_name
            ),
            "",
            "[Service]",
            "Type=simple",
            f"User={spec.node_uid}",
            f"Group={spec.node_gid}",
            f"SupplementaryGroups={spec.docker_gid}",
            *python_environment,
            "UMask=0077",
            "NoNewPrivileges=yes",
            "PrivateMounts=no",
            "PrivateDevices=no",
            "RestrictAddressFamilies=AF_UNIX",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            _exec_start(spec, spec.node_runner_path, "run"),
            "Restart=always",
            "RestartSec=1s",
            "KillMode=mixed",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )
    outbox = _systemd_unit(
        spec,
        spec.outbox_unit_name,
        (
            "[Unit]",
            "Description=Aletheia qualification terminal outbox dispatcher",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={spec.outbox_uid}",
            f"Group={spec.outbox_gid}",
            "SupplementaryGroups=",
            *python_environment,
            "UMask=0077",
            "NoNewPrivileges=yes",
            "PrivateMounts=yes",
            "PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            _exec_start(spec, spec.outbox_runner_path, "run"),
            "Restart=always",
            "RestartSec=1s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )
    return tuple(
        sorted((workspace, quota, watchdog, node, outbox), key=lambda item: item.unit_name)
    )


def _table_list(tables: tuple[str, ...]) -> str:
    return ", ".join(f"public.{_quoted_identifier(table)}" for table in tables)


def _sequence_list(sequences: tuple[str, ...]) -> str:
    return ", ".join(f"public.{_quoted_identifier(sequence)}" for sequence in sequences)


def _sql_literal_list(values: tuple[str, ...]) -> str:
    for value in values:
        _quoted_identifier(value)
    return ", ".join(f"'{value}'" for value in values)


def render_postgresql_acl(spec: QualificationDeploymentSpecV1) -> bytes:
    """Render the exact role ACL; no role, database, or table is touched here."""

    spec = QualificationDeploymentSpecV1.model_validate(spec.model_dump(mode="python"))
    database = _quoted_identifier(spec.postgresql_database)
    owner = _quoted_identifier(spec.postgresql_owner_role)
    allocator = _quoted_identifier(spec.postgresql_allocator_role)
    outbox = _quoted_identifier(spec.postgresql_outbox_role)
    all_tables = _table_list(EXECUTION_TABLES)
    updates = _table_list(ALLOCATOR_UPDATE_TABLES)
    all_sequences = _sequence_list(EXECUTION_SEQUENCES)
    table_literals = _sql_literal_list(EXECUTION_TABLES)
    sequence_literals = _sql_literal_list(EXECUTION_SEQUENCES)
    routine_literals = ", ".join(
        f"'{routine.identity}'" for routine in spec.expected_postgresql_routines
    )
    application_role_literals = _sql_literal_list(
        (spec.postgresql_allocator_role, spec.postgresql_outbox_role)
    )
    allowed_grantee_literals = _sql_literal_list(
        tuple(
            sorted(
                (
                    spec.postgresql_owner_role,
                    spec.postgresql_allocator_role,
                    spec.postgresql_outbox_role,
                )
            )
        )
    )
    owner_grantee_literal = _sql_literal_list((spec.postgresql_owner_role,))
    unexpected_or_grantable_acl = (
        "       AND (",
        "         COALESCE(grantee_role.rolname, 'PUBLIC')",
        f"             NOT IN ({allowed_grantee_literals})",
        "         OR (",
        "           COALESCE(grantee_role.rolname, 'PUBLIC')",
        f"               IN ({application_role_literals})",
        "           AND privilege.is_grantable",
        "         )",
        "       )",
    )
    routine_projection_sha256 = canonical_sha256(spec.expected_postgresql_routines)
    trigger_projection_sha256 = canonical_sha256(spec.expected_postgresql_triggers)
    sequence_projection_sha256 = canonical_sha256(spec.expected_postgresql_sequences)
    lines = (
        "-- Aletheia qualification execution ACL v1.",
        f"-- exact routine catalog projection sha256: {routine_projection_sha256}",
        f"-- exact trigger catalog projection sha256: {trigger_projection_sha256}",
        f"-- exact sequence catalog projection sha256: {sequence_projection_sha256}",
        "-- Roles must already exist; this script creates no login or secret.",
        "-- PRECONDITION: application roles have no direct or transitive role memberships.",
        "-- PRECONDITION: the signed observer enumerates all grants and execution object owners.",
        "-- Unexpected historical grantees are rejected below, never silently retained.",
        "BEGIN;",
        f"ALTER ROLE {owner} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;",
        f"ALTER ROLE {allocator} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;",
        f"ALTER ROLE {outbox} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;",
        f"REVOKE {owner} FROM {allocator};",
        f"REVOKE {owner} FROM {outbox};",
        f"REVOKE {allocator} FROM {outbox};",
        f"REVOKE {outbox} FROM {allocator};",
        f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM PUBLIC, {allocator}, {outbox};",
        f"GRANT CONNECT ON DATABASE {database} TO {allocator}, {outbox};",
        f"ALTER SCHEMA public OWNER TO {owner};",
        f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC, {allocator}, {outbox};",
        f"GRANT USAGE ON SCHEMA public TO {allocator}, {outbox};",
        f"REVOKE ALL PRIVILEGES ON {all_tables} FROM PUBLIC, {allocator}, {outbox};",
        f"REVOKE ALL PRIVILEGES ON SEQUENCE {all_sequences} FROM PUBLIC, {allocator}, {outbox};",
        *(
            f"ALTER TABLE public.{_quoted_identifier(table)} OWNER TO {owner};"
            for table in EXECUTION_TABLES
        ),
        *(
            f"ALTER SEQUENCE public.{_quoted_identifier(sequence)} OWNER TO {owner};"
            for sequence in EXECUTION_SEQUENCES
        ),
        *(
            f"ALTER {routine.routine_kind.upper()} {routine.sql_reference} OWNER TO {owner};"
            for routine in spec.expected_postgresql_routines
        ),
        *(
            f"REVOKE ALL PRIVILEGES ON {routine.routine_kind.upper()} "
            f"{routine.sql_reference} FROM PUBLIC, {allocator}, {outbox};"
            for routine in spec.expected_postgresql_routines
        ),
        "DO $aletheia_column_acl$",
        "DECLARE",
        "  grant_row record;",
        "BEGIN",
        "  FOR grant_row IN",
        "    SELECT object.relname AS table_name,",
        "           attribute.attname AS column_name,",
        "           COALESCE(grantee_role.rolname, 'PUBLIC') AS grantee,",
        "           privilege.privilege_type",
        "      FROM pg_catalog.pg_attribute AS attribute",
        "      JOIN pg_catalog.pg_class AS object ON object.oid = attribute.attrelid",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = object.relnamespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(attribute.attacl, ARRAY[]::pg_catalog.aclitem[])",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        "       AND object.relkind IN ('r', 'p')",
        f"       AND object.relname IN ({table_literals})",
        "       AND attribute.attnum > 0",
        "       AND NOT attribute.attisdropped",
        "       AND COALESCE(grantee_role.rolname, 'PUBLIC') IN ('PUBLIC', "
        f"{application_role_literals})",
        "  LOOP",
        "    EXECUTE format(",
        "      'REVOKE %s (%I) ON TABLE public.%I FROM %s',",
        "      grant_row.privilege_type,",
        "      grant_row.column_name,",
        "      grant_row.table_name,",
        "      CASE WHEN grant_row.grantee = 'PUBLIC' THEN 'PUBLIC'",
        "           ELSE format('%I', grant_row.grantee) END",
        "    );",
        "  END LOOP;",
        "END",
        "$aletheia_column_acl$;",
        f"GRANT SELECT, INSERT ON {all_tables} TO {allocator};",
        f"GRANT UPDATE ON {updates} TO {allocator};",
        f"GRANT USAGE ON SEQUENCE {all_sequences} TO {allocator};",
        (
            "GRANT SELECT, UPDATE (status, publish_attempts, published_at) ON "
            f"public.{_quoted_identifier('execution_outbox')} TO {outbox};"
        ),
        (
            "GRANT SELECT ON "
            f"public.{_quoted_identifier('execution_qualification_terminal_outbox')} TO {outbox};"
        ),
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON {all_tables} FROM {allocator}, {outbox};",
        "DO $aletheia_acl$",
        "BEGIN",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_auth_members AS membership",
        "      JOIN pg_catalog.pg_roles AS member_role",
        "        ON member_role.oid = membership.member",
        f"     WHERE member_role.rolname IN ({application_role_literals})",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification application roles retain role memberships';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_auth_members AS membership",
        "      JOIN pg_catalog.pg_roles AS granted_role",
        "        ON granted_role.oid = membership.roleid",
        f"     WHERE granted_role.rolname IN ({application_role_literals})",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification application roles retain unexpected members';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_auth_members AS membership",
        "      JOIN pg_catalog.pg_roles AS granted_role",
        "        ON granted_role.oid = membership.roleid",
        f"     WHERE granted_role.rolname = '{spec.postgresql_owner_role}'",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution owner role retains members';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_auth_members AS membership",
        "      JOIN pg_catalog.pg_roles AS member_role",
        "        ON member_role.oid = membership.member",
        f"     WHERE member_role.rolname = '{spec.postgresql_owner_role}'",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution owner role retains memberships';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_proc AS routine",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = routine.pronamespace",
        "      JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = routine.proowner",
        "     WHERE namespace.nspname = 'public'",
        "       AND routine.prokind IN ('f', 'p')",
        f"       AND left(routine.proname, {len(spec.postgresql_execution_routine_name_prefix)})",
        f"           <> '{spec.postgresql_execution_routine_name_prefix}'",
        f"       AND owner_role.rolname IN ({allowed_grantee_literals})",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification protected roles own a non-execution public routine';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_database AS database",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(database.datacl, pg_catalog.acldefault('d', database.datdba))",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        f"     WHERE database.datname = '{spec.postgresql_database}'",
        *unexpected_or_grantable_acl,
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification database retains an unexpected grantee or grant option';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_namespace AS namespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        *unexpected_or_grantable_acl,
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification public schema retains an unexpected grantee or grant option';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_class AS object",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = object.relnamespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(object.relacl, pg_catalog.acldefault('r', object.relowner))",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        "       AND object.relkind IN ('r', 'p')",
        f"       AND object.relname IN ({table_literals})",
        *unexpected_or_grantable_acl,
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution tables retain an unexpected grantee or grant option';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_attribute AS attribute",
        "      JOIN pg_catalog.pg_class AS object ON object.oid = attribute.attrelid",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = object.relnamespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(attribute.attacl, ARRAY[]::pg_catalog.aclitem[])",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        "       AND object.relkind IN ('r', 'p')",
        f"       AND object.relname IN ({table_literals})",
        "       AND attribute.attnum > 0",
        "       AND NOT attribute.attisdropped",
        *unexpected_or_grantable_acl,
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution columns retain an unexpected grantee or grant option';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_class AS object",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = object.relnamespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(object.relacl, pg_catalog.acldefault('S', object.relowner))",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        "       AND object.relkind = 'S'",
        f"       AND object.relname IN ({sequence_literals})",
        *unexpected_or_grantable_acl,
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution sequences retain an unexpected grantee or grant option';",
        "  END IF;",
        "  IF EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_proc AS routine",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = routine.pronamespace",
        "      CROSS JOIN LATERAL pg_catalog.aclexplode(",
        "        COALESCE(routine.proacl, pg_catalog.acldefault('f', routine.proowner))",
        "      ) AS privilege",
        "      LEFT JOIN pg_catalog.pg_roles AS grantee_role",
        "        ON grantee_role.oid = privilege.grantee",
        "     WHERE namespace.nspname = 'public'",
        "       AND routine.prokind IN ('f', 'p')",
        "       AND (routine.proname || '(' ||",
        "            pg_catalog.pg_get_function_identity_arguments(routine.oid) || ')')",
        f"           IN ({routine_literals})",
        "       AND COALESCE(grantee_role.rolname, 'PUBLIC')",
        f"           NOT IN ({owner_grantee_literal})",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification execution routines retain an unexpected EXECUTE grantee';",
        "  END IF;",
        "  IF NOT EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_database AS database",
        "      JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = database.datdba",
        f"     WHERE database.datname = '{spec.postgresql_database}'",
        f"       AND owner_role.rolname = '{spec.postgresql_owner_role}'",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification database owner is not exact';",
        "  END IF;",
        "  IF NOT EXISTS (",
        "    SELECT 1",
        "      FROM pg_catalog.pg_namespace AS namespace",
        "      JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = namespace.nspowner",
        "     WHERE namespace.nspname = 'public'",
        f"       AND owner_role.rolname = '{spec.postgresql_owner_role}'",
        "  ) THEN",
        "    RAISE EXCEPTION 'qualification public schema owner is not exact';",
        "  END IF;",
        "  IF (",
        "    SELECT count(*)",
        "      FROM pg_catalog.pg_proc AS routine",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = routine.pronamespace",
        "      JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = routine.proowner",
        "     WHERE namespace.nspname = 'public'",
        "       AND routine.prokind IN ('f', 'p')",
        "       AND (routine.proname || '(' ||",
        "            pg_catalog.pg_get_function_identity_arguments(routine.oid) || ')')",
        f"           IN ({routine_literals})",
        f"       AND owner_role.rolname = '{spec.postgresql_owner_role}'",
        f"  ) <> {len(spec.expected_postgresql_routines)} THEN",
        "    RAISE EXCEPTION 'qualification execution routine ownership is not exact';",
        "  END IF;",
        "  IF (",
        "    SELECT count(*)",
        "      FROM pg_catalog.pg_proc AS routine",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = routine.pronamespace",
        "     WHERE namespace.nspname = 'public'",
        "       AND routine.prokind IN ('f', 'p')",
        f"       AND left(routine.proname, {len(spec.postgresql_execution_routine_name_prefix)})",
        f"           = '{spec.postgresql_execution_routine_name_prefix}'",
        f"  ) <> {len(spec.expected_postgresql_routines)} THEN",
        "    RAISE EXCEPTION 'qualification execution routine signature set is not exact';",
        "  END IF;",
        "  IF (",
        "    SELECT count(*)",
        "      FROM pg_catalog.pg_class AS object",
        "      JOIN pg_catalog.pg_namespace AS namespace",
        "        ON namespace.oid = object.relnamespace",
        "      JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = object.relowner",
        "     WHERE namespace.nspname = 'public'",
        "       AND (",
        f"         (object.relkind IN ('r', 'p') AND object.relname IN ({table_literals}))",
        f"         OR (object.relkind = 'S' AND object.relname IN ({sequence_literals}))",
        "       )",
        f"       AND owner_role.rolname = '{spec.postgresql_owner_role}'",
        f"  ) <> {len(EXECUTION_TABLES) + len(EXECUTION_SEQUENCES)} THEN",
        "    RAISE EXCEPTION 'qualification execution object ownership is not exact';",
        "  END IF;",
        "END",
        "$aletheia_acl$;",
        f"ALTER ROLE {allocator} SET search_path = pg_catalog, public;",
        f"ALTER ROLE {outbox} SET search_path = pg_catalog, public;",
        "COMMIT;",
        "",
    )
    return "\n".join(lines).encode("utf-8")


def postgresql_role_privileges_sha256(
    spec: QualificationDeploymentSpecV1,
    *,
    role_name: str,
) -> str:
    """Return the exact expected table/column privilege projection for one application role."""

    spec = QualificationDeploymentSpecV1.model_validate(spec.model_dump(mode="python"))
    if role_name == spec.postgresql_allocator_role:
        projection: dict[str, object] = {
            "schema": "aletheia.qualification_postgresql_role_privileges",
            "schema_version": 1,
            "role_name": role_name,
            "database_connect": (spec.postgresql_database,),
            "database_create": (),
            "database_temporary": (),
            "schema_usage": (spec.postgresql_schema,),
            "schema_create": (),
            "table_select": EXECUTION_TABLES,
            "table_insert": EXECUTION_TABLES,
            "table_update": ALLOCATOR_UPDATE_TABLES,
            "column_update": (),
            "table_delete": (),
            "table_truncate": (),
            "table_references": (),
            "table_trigger": (),
            "sequence_usage": EXECUTION_SEQUENCES,
            "routine_execute": (),
            "grantable_privileges": (),
        }
    elif role_name == spec.postgresql_outbox_role:
        projection = {
            "schema": "aletheia.qualification_postgresql_role_privileges",
            "schema_version": 1,
            "role_name": role_name,
            "database_connect": (spec.postgresql_database,),
            "database_create": (),
            "database_temporary": (),
            "schema_usage": (spec.postgresql_schema,),
            "schema_create": (),
            "table_select": (
                "execution_outbox",
                "execution_qualification_terminal_outbox",
            ),
            "table_insert": (),
            "table_update": (),
            "column_update": (
                (
                    "execution_outbox",
                    ("publish_attempts", "published_at", "status"),
                ),
            ),
            "table_delete": (),
            "table_truncate": (),
            "table_references": (),
            "table_trigger": (),
            "sequence_usage": (),
            "routine_execute": (),
            "grantable_privileges": (),
        }
    else:
        raise ValueError("PostgreSQL role is outside the qualification application ACL")
    return canonical_sha256(projection)


class PostgreSQLRestrictedRoleObservation(ExecutionModel):
    """Read-only observation of one application role's dangerous privileges."""

    role_name: str
    can_login: bool
    is_superuser: bool
    can_create_database: bool
    can_create_role: bool
    inherits_roles: bool
    can_replicate: bool
    bypasses_row_security: bool
    member_of_owner_role: bool
    owns_execution_objects: bool
    can_create_in_schema: bool
    can_create_temporary_tables: bool
    can_delete_execution_rows: bool
    can_truncate_execution_tables: bool
    can_execute_ddl: bool
    can_mutate_triggers_or_functions: bool
    direct_role_memberships: tuple[str, ...]
    transitive_role_memberships: tuple[str, ...]
    role_members: tuple[str, ...]
    dangerous_builtin_role_memberships: tuple[str, ...]
    table_privileges_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _role_name_is_safe(self) -> "PostgreSQLRestrictedRoleObservation":
        _quoted_identifier(self.role_name)
        _canonical_postgresql_identifiers(
            self.direct_role_memberships, label="direct PostgreSQL role memberships"
        )
        _canonical_postgresql_identifiers(
            self.transitive_role_memberships,
            label="transitive PostgreSQL role memberships",
        )
        _canonical_postgresql_identifiers(
            self.role_members,
            label="PostgreSQL application role members",
        )
        _canonical_postgresql_identifiers(
            self.dangerous_builtin_role_memberships,
            label="dangerous PostgreSQL built-in role memberships",
        )
        if not set(self.direct_role_memberships).issubset(self.transitive_role_memberships):
            raise ValueError("direct PostgreSQL memberships must be present transitively")
        if not set(self.dangerous_builtin_role_memberships).issubset(
            self.transitive_role_memberships
        ):
            raise ValueError("dangerous PostgreSQL memberships must be present transitively")
        if not set(self.dangerous_builtin_role_memberships).issubset(
            POSTGRESQL_DANGEROUS_BUILTIN_ROLES
        ):
            raise ValueError("dangerous PostgreSQL memberships are not recognized built-ins")
        return self

    @property
    def restricted(self) -> bool:
        return self.can_login and not any(
            (
                self.is_superuser,
                self.can_create_database,
                self.can_create_role,
                self.inherits_roles,
                self.can_replicate,
                self.bypasses_row_security,
                self.member_of_owner_role,
                self.owns_execution_objects,
                self.can_create_in_schema,
                self.can_create_temporary_tables,
                self.can_delete_execution_rows,
                self.can_truncate_execution_tables,
                self.can_execute_ddl,
                self.can_mutate_triggers_or_functions,
                bool(self.direct_role_memberships),
                bool(self.transitive_role_memberships),
                bool(self.role_members),
                bool(self.dangerous_builtin_role_memberships),
            )
        )


class PostgreSQLExecutionObjectOwnerObservation(ExecutionModel):
    """One exact execution table or sequence owner from the signed catalog projection."""

    object_kind: Literal["database", "schema", "table", "sequence", "function", "procedure"]
    object_name: str
    owner_role: str

    @model_validator(mode="after")
    def _object_is_canonical(self) -> "PostgreSQLExecutionObjectOwnerObservation":
        if self.object_kind in {"function", "procedure"}:
            _canonical_routine_identity(self.object_name)
        else:
            _quoted_identifier(self.object_name)
        _quoted_identifier(self.owner_role)
        return self


class PostgreSQLNonExecutionRoutineOwnerObservation(ExecutionModel):
    """Owner baseline for a public routine outside the managed execution namespace."""

    routine_kind: Literal["function", "procedure"]
    routine_schema: Literal["public"]
    routine_name: str
    identity_argument_types: tuple[str, ...]
    owner_role: str

    @model_validator(mode="after")
    def _routine_owner_is_canonical(self) -> "PostgreSQLNonExecutionRoutineOwnerObservation":
        _canonical_routine_identity(self.identity)
        _quoted_identifier(self.owner_role)
        return self

    @property
    def identity(self) -> str:
        return f"{self.routine_name}({', '.join(self.identity_argument_types)})"


class PostgreSQLUnexpectedPrivilegeObservation(ExecutionModel):
    """Typed unexpected catalog grant; empty typed sets are required for readiness."""

    object_kind: Literal[
        "database", "schema", "table", "column", "sequence", "function", "procedure"
    ]
    object_identity: str
    grantee: str
    privilege_type: Literal[
        "CONNECT",
        "CREATE",
        "DELETE",
        "EXECUTE",
        "INSERT",
        "REFERENCES",
        "SELECT",
        "TEMPORARY",
        "TRIGGER",
        "TRUNCATE",
        "UPDATE",
        "USAGE",
    ]
    is_grantable: bool
    column_name: str | None = None

    @model_validator(mode="after")
    def _unexpected_grant_is_canonical(
        self,
    ) -> "PostgreSQLUnexpectedPrivilegeObservation":
        if self.object_kind in {"function", "procedure"}:
            _canonical_routine_identity(self.object_identity)
        else:
            _quoted_identifier(self.object_identity)
        if self.grantee != "PUBLIC":
            _quoted_identifier(self.grantee)
        if (self.object_kind == "column") != (self.column_name is not None):
            raise ValueError("only a PostgreSQL column grant may carry a column name")
        if self.column_name is not None:
            _quoted_identifier(self.column_name)
        return self


class QualificationObservedRootCodeTree(ExecutionModel):
    """Exact live Linux code-root identity plus exhaustive observed tree-manifest hash."""

    schema_name: Literal["aletheia.qualification_observed_root_code_tree"] = (
        "aletheia.qualification_observed_root_code_tree"
    )
    schema_version: Literal[1] = 1
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: Literal[0] = 0
    owner_gid: Literal[0] = 0
    mode: int = Field(ge=0, le=0o7777)
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    tree_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    directory_count: int = Field(ge=0)
    regular_file_count: int = Field(ge=1)
    total_regular_file_bytes: int = Field(ge=0, le=2**63 - 1)
    symlink_count: Literal[0] = 0

    @model_validator(mode="after")
    def _live_code_root_is_canonical(self) -> "QualificationObservedRootCodeTree":
        _absolute_path(self.path, label="observed live code root")
        if self.mode & 0o7022 or self.mode & 0o505 != 0o505:
            raise ValueError(
                "observed live tree root is not root controlled and worker-traversable"
            )
        return self


class QualificationSystemdServiceIdentityObservation(ExecutionModel):
    """Effective process identity after systemd and NSS group expansion."""

    unit_name: str
    fragment_path: str
    loaded_fragment_sha256: str = Field(pattern=_SHA256_PATTERN)
    daemon_reload_generation_matches_fragment: bool
    drop_in_paths: tuple[str, ...]
    exec_start_argvs: tuple[tuple[str, ...], ...]
    exec_start_pre_argvs: tuple[tuple[str, ...], ...]
    exec_start_post_argvs: tuple[tuple[str, ...], ...]
    effective_environment: tuple[str, ...]
    unset_environment_names: tuple[str, ...]
    effective_uid: int = Field(ge=0, le=2**31 - 1)
    effective_gid: int = Field(ge=0, le=2**31 - 1)
    supplementary_gids: tuple[int, ...]
    effective_capabilities: tuple[str, ...]
    no_new_privileges: Literal[True]
    private_mounts: bool
    working_directory: str
    python_executable_path: str
    python_environment_root: str
    python_path: str
    python_flags: tuple[Literal["-S"], Literal["-s"], Literal["-P"]]
    worker_poll_milliseconds: int | None = Field(default=None, ge=50, le=60_000)

    @model_validator(mode="after")
    def _identity_is_canonical(self) -> "QualificationSystemdServiceIdentityObservation":
        if _UNIT_PATTERNS.keys() and not any(
            pattern.fullmatch(self.unit_name) for pattern in _UNIT_PATTERNS.values()
        ):
            raise ValueError("observed systemd service name is not deployment-scoped")
        fragment = _absolute_path(self.fragment_path, label="effective systemd FragmentPath")
        if fragment.name != self.unit_name:
            raise ValueError("effective systemd FragmentPath differs from its unit")
        drop_ins = tuple(
            str(_absolute_path(value, label="effective systemd DropInPath"))
            for value in self.drop_in_paths
        )
        if drop_ins != tuple(sorted(set(drop_ins))):
            raise ValueError("effective systemd DropInPaths must be canonical")
        if not self.exec_start_argvs or any(
            not argv or any(not argument or "\x00" in argument for argument in argv)
            for argv in (
                *self.exec_start_argvs,
                *self.exec_start_pre_argvs,
                *self.exec_start_post_argvs,
            )
        ):
            raise ValueError("effective systemd Exec argv is incomplete")
        environment_names = tuple(value.partition("=")[0] for value in self.effective_environment)
        if (
            any(
                not separator
                or re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None
                or any(character in payload for character in ("\x00", "\n", "\r"))
                for value in self.effective_environment
                for name, separator, payload in (value.partition("="),)
            )
            or len(set(environment_names)) != len(environment_names)
        ):
            raise ValueError("effective systemd environment must contain unique exact assignments")
        if self.unset_environment_names != tuple(sorted(set(self.unset_environment_names))) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None
            for name in self.unset_environment_names
        ):
            raise ValueError("effective systemd unset environment names must be canonical")
        if self.supplementary_gids != tuple(sorted(set(self.supplementary_gids))):
            raise ValueError("systemd supplementary groups must be exact and canonical")
        if self.effective_capabilities != tuple(sorted(set(self.effective_capabilities))) or any(
            re.fullmatch(r"CAP_[A-Z0-9_]+", capability) is None
            for capability in self.effective_capabilities
        ):
            raise ValueError("systemd effective capabilities must be exact and canonical")
        for value, label in (
            (self.working_directory, "systemd working directory"),
            (self.python_executable_path, "systemd Python executable"),
            (self.python_environment_root, "systemd Python environment"),
            (self.python_path, "systemd Python path"),
        ):
            _absolute_path(value, label=label)
        is_node_service = _UNIT_PATTERNS["node"].fullmatch(self.unit_name) is not None
        if is_node_service != (self.worker_poll_milliseconds is not None):
            raise ValueError(
                "only the node service may carry one explicit worker poll interval"
            )
        return self


class ObservedNativeDependency(ExecutionModel):
    """Live root-owned SONAME resolution and DT_NEEDED edges."""

    soname: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}$")
    file: PinnedRootFile
    needed_sonames: tuple[str, ...]

    @model_validator(mode="after")
    def _dependency_is_canonical(self) -> "ObservedNativeDependency":
        if self.needed_sonames != tuple(sorted(set(self.needed_sonames))) or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}", value) is None
            for value in self.needed_sonames
        ):
            raise ValueError("observed native DT_NEEDED edges must be canonical")
        return self


class ObservedNativeDependencyClosure(ExecutionModel):
    """Signed live PT_INTERP and transitive DT_NEEDED resolution."""

    executable: PinnedRootExecutable
    elf_interpreter: PinnedRootFile
    executable_needed_sonames: tuple[str, ...]
    dependencies: tuple[ObservedNativeDependency, ...] = Field(min_length=1)
    exhaustive: Literal[True]
    external_native_dependency_paths: tuple[str, ...]

    @model_validator(mode="after")
    def _closure_is_canonical(self) -> "ObservedNativeDependencyClosure":
        if self.executable_needed_sonames != tuple(
            sorted(set(self.executable_needed_sonames))
        ):
            raise ValueError("observed executable DT_NEEDED edges must be canonical")
        sonames = tuple(dependency.soname for dependency in self.dependencies)
        if sonames != tuple(sorted(set(sonames))):
            raise ValueError("observed native dependencies must be canonical")
        external_paths = tuple(
            str(_absolute_path(value, label="external native dependency"))
            for value in self.external_native_dependency_paths
        )
        if external_paths != tuple(sorted(set(external_paths))):
            raise ValueError("external native dependency paths must be canonical")
        return self


class QualificationLinuxDeploymentObservation(ExecutionModel):
    """Typed output of a read-only observer running on the candidate Linux target."""

    schema_name: Literal["aletheia.qualification_linux_deployment_observation"] = (
        "aletheia.qualification_linux_deployment_observation"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    platform: str
    cpu_architecture: str
    oci_platform: str
    kernel_release: str = Field(min_length=1, max_length=256)
    boot_id: str
    pid_one_comm: str
    cgroup_version: int
    docker_cgroup_driver: str
    docker_security_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    pid_one_mount_namespace: str
    quota_mount_namespace: str
    node_mount_namespace: str
    docker_mount_namespace: str
    shared_output_mount_visible: bool
    host_clock_synchronized: bool
    custody_roots: tuple[QualificationObservedCustodyRoot, ...] = Field(
        min_length=len(_CUSTODY_ROOT_PURPOSES),
        max_length=len(_CUSTODY_ROOT_PURPOSES),
    )

    python_executable: PinnedRootExecutable
    python_environment_root: QualificationObservedRootCodeTree
    python_import_paths: tuple[str, ...]
    python_external_loaded_native_object_paths: tuple[str, ...]
    entrypoint_files: tuple[PinnedRootFile, ...]
    code_root: QualificationObservedRootCodeTree
    deployment_manifest_file: PinnedRootFile
    systemd_unit_files: tuple[PinnedRootFile, ...]
    service_module_files: tuple[PinnedRootFile, ...]
    privileged_tool_native_closures: tuple[ObservedNativeDependencyClosure, ...]
    systemd_service_identities: tuple[QualificationSystemdServiceIdentityObservation, ...]
    seccomp_profile: PinnedRootFile
    apparmor_profile: PinnedRootFile
    loaded_apparmor_profile_name: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )
    apparmor_profile_enforcing: bool
    agent_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_workspace_root: PinnedOutputWorkspaceRoot
    oci_image_layout: PinnedOCIImageLayout
    loaded_image_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    loaded_image_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    quota_deployment: LoopbackQuotaProvisionerDeploymentPin
    watchdog_deployment: SystemdWatchdogDeploymentPin
    quota_service_systemd_verified: bool
    watchdog_service_systemd_verified: bool

    schema_revision: str
    postgresql_server_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    postgresql_acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    postgresql_clock_healthy: bool
    postgresql_roles: tuple[PostgreSQLRestrictedRoleObservation, ...]
    postgresql_owner_role_inherits: bool
    postgresql_owner_direct_role_memberships: tuple[str, ...]
    postgresql_owner_transitive_role_memberships: tuple[str, ...]
    postgresql_owner_dangerous_builtin_role_memberships: tuple[str, ...]
    postgresql_owner_role_members: tuple[str, ...]
    postgresql_unexpected_database_grants: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_schema_grants: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_table_grants: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_column_grants: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_sequence_grants: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_routine_execute_grants: tuple[
        PostgreSQLUnexpectedPrivilegeObservation, ...
    ]
    postgresql_unexpected_grant_options: tuple[PostgreSQLUnexpectedPrivilegeObservation, ...]
    postgresql_unexpected_execution_routines: tuple[PostgreSQLExpectedRoutine, ...]
    postgresql_routines: tuple[PostgreSQLExpectedRoutine, ...]
    postgresql_triggers: tuple[PostgreSQLExpectedTrigger, ...]
    postgresql_sequences: tuple[PostgreSQLExpectedSequenceConfiguration, ...]
    postgresql_non_execution_public_routine_owners: tuple[
        PostgreSQLNonExecutionRoutineOwnerObservation, ...
    ]
    postgresql_non_execution_public_routine_owner_projection_exhaustive: Literal[True]
    postgresql_execution_object_owners: tuple[PostgreSQLExecutionObjectOwnerObservation, ...]
    observation_started_at: AwareDatetime
    observed_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _observation_is_canonical(self) -> "QualificationLinuxDeploymentObservation":
        _require_utc(
            self.observation_started_at,
            label="Linux deployment observation observation_started_at",
        )
        _require_utc(self.observed_at, label="Linux deployment observation observed_at")
        if self.observed_at < self.observation_started_at:
            raise ValueError("Linux deployment observation completion precedes its start")
        if _LINUX_BOOT_ID.fullmatch(self.boot_id) is None:
            raise ValueError("Linux deployment observation boot id is not canonical")
        for value in (
            self.pid_one_mount_namespace,
            self.quota_mount_namespace,
            self.node_mount_namespace,
            self.docker_mount_namespace,
        ):
            if _MOUNT_NAMESPACE.fullmatch(value) is None:
                raise ValueError("Linux mount namespace identity is not canonical")
        if self.python_import_paths != tuple(dict.fromkeys(self.python_import_paths)):
            raise ValueError("observed Python import paths must be unique and ordered")
        custody_purposes = tuple(item.purpose for item in self.custody_roots)
        if custody_purposes != _CUSTODY_ROOT_PURPOSES:
            raise ValueError(
                "observed custody roots must be exhaustive and canonically ordered"
            )
        for value in self.python_import_paths:
            _absolute_path(value, label="observed Python import path")
        external_native_paths = tuple(
            _absolute_path(value, label="external loaded native object")
            for value in self.python_external_loaded_native_object_paths
        )
        if external_native_paths != tuple(sorted(set(external_native_paths))):
            raise ValueError("external loaded native objects must be canonical")
        for values, label, key in (
            (self.entrypoint_files, "entrypoint", lambda item: item.path),
            (self.systemd_unit_files, "systemd unit", lambda item: item.path),
            (self.service_module_files, "service module", lambda item: item.path),
            (
                self.privileged_tool_native_closures,
                "privileged native closure",
                lambda item: item.executable.path,
            ),
            (
                self.systemd_service_identities,
                "systemd service identity",
                lambda item: item.unit_name,
            ),
            (self.postgresql_roles, "PostgreSQL role", lambda item: item.role_name),
        ):
            keys = tuple(key(item) for item in values)
            if keys != tuple(sorted(set(keys))):
                raise ValueError(f"{label} observations must be unique and canonically ordered")
        _canonical_postgresql_identifiers(
            self.postgresql_owner_role_members,
            label="PostgreSQL execution owner role members",
        )
        for values, label in (
            (
                self.postgresql_owner_direct_role_memberships,
                "PostgreSQL owner direct memberships",
            ),
            (
                self.postgresql_owner_transitive_role_memberships,
                "PostgreSQL owner transitive memberships",
            ),
            (
                self.postgresql_owner_dangerous_builtin_role_memberships,
                "PostgreSQL owner dangerous memberships",
            ),
        ):
            _canonical_postgresql_identifiers(values, label=label)
        if not set(self.postgresql_owner_direct_role_memberships).issubset(
            self.postgresql_owner_transitive_role_memberships
        ) or not set(self.postgresql_owner_dangerous_builtin_role_memberships).issubset(
            self.postgresql_owner_transitive_role_memberships
        ):
            raise ValueError("PostgreSQL owner direct/dangerous memberships must be transitive")
        if not set(self.postgresql_owner_dangerous_builtin_role_memberships).issubset(
            POSTGRESQL_DANGEROUS_BUILTIN_ROLES
        ):
            raise ValueError("PostgreSQL owner dangerous memberships are not built-ins")
        for grants, label, expected_kinds in (
            (
                self.postgresql_unexpected_database_grants,
                "database",
                {"database"},
            ),
            (self.postgresql_unexpected_schema_grants, "schema", {"schema"}),
            (self.postgresql_unexpected_table_grants, "table", {"table"}),
            (self.postgresql_unexpected_column_grants, "column", {"column"}),
            (
                self.postgresql_unexpected_sequence_grants,
                "sequence",
                {"sequence"},
            ),
            (
                self.postgresql_unexpected_routine_execute_grants,
                "routine",
                {"function", "procedure"},
            ),
        ):
            keys = tuple(
                (
                    item.object_kind,
                    item.object_identity,
                    item.column_name or "",
                    item.grantee,
                    item.privilege_type,
                    item.is_grantable,
                )
                for item in grants
            )
            if keys != tuple(sorted(set(keys))) or any(
                item.object_kind not in expected_kinds for item in grants
            ):
                raise ValueError(
                    f"unexpected PostgreSQL {label} grants must be typed and canonical"
                )
        grant_option_keys = tuple(
            (
                item.object_kind,
                item.object_identity,
                item.column_name or "",
                item.grantee,
                item.privilege_type,
            )
            for item in self.postgresql_unexpected_grant_options
        )
        if grant_option_keys != tuple(sorted(set(grant_option_keys))) or any(
            not item.is_grantable for item in self.postgresql_unexpected_grant_options
        ):
            raise ValueError("unexpected PostgreSQL grant options must be exact and canonical")
        unexpected_routine_keys = tuple(
            (item.routine_kind, item.routine_name, item.identity_argument_types)
            for item in self.postgresql_unexpected_execution_routines
        )
        if unexpected_routine_keys != tuple(sorted(set(unexpected_routine_keys))):
            raise ValueError("unexpected execution PostgreSQL routines must be canonical")
        for values, label, key in (
            (
                self.postgresql_routines,
                "PostgreSQL routines",
                lambda item: (item.routine_kind, item.routine_name, item.identity_argument_types),
            ),
            (
                self.postgresql_triggers,
                "PostgreSQL triggers",
                lambda item: (item.table_name, item.trigger_name),
            ),
            (
                self.postgresql_sequences,
                "PostgreSQL sequences",
                lambda item: item.sequence_name,
            ),
            (
                self.postgresql_non_execution_public_routine_owners,
                "non-execution PostgreSQL routine owners",
                lambda item: (item.routine_kind, item.identity),
            ),
        ):
            keys = tuple(key(item) for item in values)
            if keys != tuple(sorted(set(keys))):
                raise ValueError(f"{label} must be exact and canonically ordered")
        owner_keys = tuple(
            (item.object_kind, item.object_name) for item in self.postgresql_execution_object_owners
        )
        if owner_keys != tuple(sorted(set(owner_keys))):
            raise ValueError(
                "PostgreSQL execution object owners must be unique and canonically ordered"
            )
        return self

    @property
    def stable_evidence_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"observation_started_at", "observed_at"},
            )
        )


class QualificationDeploymentObserverPin(ExecutionModel):
    """Independent deployment trust root; observations cannot select this key."""

    schema_name: Literal["aletheia.qualification_deployment_observer_pin"] = (
        "aletheia.qualification_deployment_observer_pin"
    )
    schema_version: Literal[1] = 1
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _pin_is_external_and_finite(self) -> "QualificationDeploymentObserverPin":
        _require_utc(self.valid_from, label="observer pin valid_from")
        _require_utc(self.expires_at, label="observer pin expires_at")
        if self.revoked_at is not None:
            _require_utc(self.revoked_at, label="observer pin revoked_at")
        if self.key_id != qualification_key_id(self.public_key_ed25519_hex):
            raise ValueError("observer key id differs from pinned public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("observer key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("observer key revocation is outside its validity")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class SignedQualificationLinuxDeploymentObservation(ExecutionModel):
    """One observer-signed Linux projection with no row-carried verification key."""

    schema_name: Literal["aletheia.signed_qualification_linux_deployment_observation"] = (
        "aletheia.signed_qualification_linux_deployment_observation"
    )
    schema_version: Literal[1] = 1
    observation: QualificationLinuxDeploymentObservation
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_systemd_units_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_postgresql_acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    observer_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    observer_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    observer_key_id: str = Field(pattern=_SHA256_PATTERN)
    signed_at: AwareDatetime
    expires_at: AwareDatetime
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _signed_window_binds_observation(
        self,
    ) -> "SignedQualificationLinuxDeploymentObservation":
        _require_utc(self.signed_at, label="observer signed_at")
        _require_utc(self.expires_at, label="observer expires_at")
        if self.signed_at != self.observation.observed_at:
            raise ValueError("observer signature time must equal observation time")
        if self.expires_at <= self.signed_at:
            raise ValueError("observer signature expiry must follow signing time")
        return self

    @property
    def message_bytes(self) -> bytes:
        unsigned = self.model_dump(mode="json", exclude={"signature_ed25519_hex"})
        return _OBSERVER_SIGNATURE_DOMAIN + canonical_json_bytes(unsigned)

    @property
    def signed_observation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationDeploymentObserver(Protocol):
    """Read-only target adapter; implementations may not install or repair anything."""

    def observe(
        self,
        *,
        spec: QualificationDeploymentSpecV1,
        rendered_units: tuple[RenderedSystemdUnit, ...],
        postgresql_acl: bytes,
    ) -> SignedQualificationLinuxDeploymentObservation: ...


class QualificationInstalledDeploymentManifestV1(ExecutionModel):
    """One exact Linux observation frozen against portable desired state."""

    schema_name: Literal["aletheia.qualification_installed_deployment_manifest"] = (
        "aletheia.qualification_installed_deployment_manifest"
    )
    schema_version: Literal[1] = 1
    spec: QualificationDeploymentSpecV1
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    rendered_systemd_units_sha256: str = Field(pattern=_SHA256_PATTERN)
    postgresql_acl_sha256: str = Field(pattern=_SHA256_PATTERN)
    observer_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_observation: SignedQualificationLinuxDeploymentObservation
    installed_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_stable_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_derived(self) -> "QualificationInstalledDeploymentManifestV1":
        _require_utc(self.frozen_at, label="installed deployment manifest frozen_at")
        units = render_systemd_units(self.spec)
        acl = render_postgresql_acl(self.spec)
        signed = self.installed_observation
        if (
            self.spec_sha256 != self.spec.spec_sha256
            or self.rendered_systemd_units_sha256 != canonical_sha256(units)
            or self.postgresql_acl_sha256 != hashlib.sha256(acl).hexdigest()
            or signed.spec_sha256 != self.spec.spec_sha256
            or signed.rendered_systemd_units_sha256 != canonical_sha256(units)
            or signed.rendered_postgresql_acl_sha256 != hashlib.sha256(acl).hexdigest()
            or self.installed_observation_sha256 != signed.signed_observation_sha256
            or self.installed_stable_evidence_sha256 != signed.observation.stable_evidence_sha256
            or not signed.signed_at <= self.frozen_at < signed.expires_at
            or signed.expires_at
            > signed.signed_at + timedelta(seconds=self.spec.observation_ttl_seconds)
            or signed.observation.observed_at - signed.observation.observation_started_at
            > timedelta(seconds=self.spec.maximum_observation_duration_seconds)
        ):
            raise ValueError("installed deployment manifest differs from its derived evidence")
        blockers, _checks = _observation_blockers(
            spec=self.spec,
            observation=signed.observation,
            rendered_units=units,
            postgresql_acl=acl,
        )
        if blockers:
            raise ValueError("installed deployment manifest contains unqualified observations")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationDeploymentPreflight(ExecutionModel):
    """Read-only eligibility for a later opt-in campaign, never a deployment verdict."""

    schema_name: Literal["aletheia.qualification_deployment_preflight"] = (
        "aletheia.qualification_deployment_preflight"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: AwareDatetime
    verified_at: AwareDatetime
    observer_provenance_verified: bool
    observation_freshness_verified: bool
    linux_systemd_cgroup_verified: bool
    shared_mount_namespace_verified: bool
    installed_files_verified: bool
    custody_roots_verified: bool
    systemd_units_verified: bool
    postgresql_acl_verified: bool
    postgresql_schema_verified: bool
    postgresql_roles_verified: bool
    postgresql_acl_closure_verified: bool
    postgresql_clock_verified: bool
    image_layout_verified: bool
    output_quota_service_verified: bool
    deadline_watchdog_service_verified: bool
    code_identity_verified: bool
    blockers: tuple[str, ...]
    ready_for_opt_in_campaign: bool
    campaign_executed: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _verdict_is_derived(self) -> "QualificationDeploymentPreflight":
        _require_utc(self.observed_at, label="deployment preflight observed_at")
        _require_utc(self.verified_at, label="deployment preflight verified_at")
        if self.observation_freshness_verified and self.observed_at > self.verified_at:
            raise ValueError("fresh deployment observation cannot postdate verification")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("deployment preflight blockers must be canonical")
        checks = (
            self.observer_provenance_verified,
            self.observation_freshness_verified,
            self.linux_systemd_cgroup_verified,
            self.shared_mount_namespace_verified,
            self.installed_files_verified,
            self.custody_roots_verified,
            self.systemd_units_verified,
            self.postgresql_acl_verified,
            self.postgresql_schema_verified,
            self.postgresql_roles_verified,
            self.postgresql_acl_closure_verified,
            self.postgresql_clock_verified,
            self.image_layout_verified,
            self.output_quota_service_verified,
            self.deadline_watchdog_service_verified,
            self.code_identity_verified,
        )
        if self.ready_for_opt_in_campaign != (not self.blockers and all(checks)):
            raise ValueError("deployment preflight verdict differs from its checks")
        return self


def _file_map(files: tuple[PinnedRootFile, ...]) -> dict[str, PinnedRootFile]:
    return {item.path: item for item in files}


def _expected_custody_root_policies(
    spec: QualificationDeploymentSpecV1,
) -> dict[str, tuple[str, int, int, int]]:
    """Return exact path/UID/GID/mode policy for every service-owned root."""

    return {
        "artifact_store": (spec.artifact_store_root, spec.node_uid, spec.node_gid, 0o700),
        "authority_registry": (spec.authority_registry_root, 0, 0, 0o555),
        "input_materialization_journal": (
            spec.input_materialization_journal_root,
            spec.node_uid,
            spec.node_gid,
            0o700,
        ),
        "node_state": (spec.node_state_root, spec.node_uid, spec.node_gid, 0o700),
        "outbox_spool": (
            spec.outbox_spool_root,
            spec.outbox_uid,
            spec.outbox_gid,
            0o700,
        ),
        "workspace_source": (
            spec.workspace_source_root,
            0,
            spec.node_gid,
            0o1730,
        ),
    }


def _same_boot_custody_roots_match(
    current: tuple[QualificationObservedCustodyRoot, ...],
    baseline_observation: QualificationLinuxDeploymentObservation | None,
    current_boot_id: str,
) -> bool:
    if baseline_observation is None or current_boot_id != baseline_observation.boot_id:
        return True
    return current == baseline_observation.custody_roots


def _installed_file_matches(
    expected: QualificationExpectedRootFile | QualificationExpectedRootExecutable,
    installed: PinnedRootFile,
) -> bool:
    return (
        installed.path == expected.path
        and installed.sha256 == expected.reviewed_sha256
        and installed.owner_uid == expected.expected_owner_uid
        and installed.owner_gid == expected.expected_owner_gid
        and installed.mode == expected.expected_mode
        and (not expected.executable or bool(installed.mode & 0o111))
    )


def _expected_systemd_service_identities(
    spec: QualificationDeploymentSpecV1,
) -> tuple[QualificationSystemdServiceIdentityObservation, ...]:
    rendered_units = {unit.unit_name: unit for unit in render_systemd_units(spec)}

    def loaded_state(unit_name: str, runner: str, operation: str) -> dict[str, object]:
        unit = rendered_units[unit_name]
        return {
            "fragment_path": unit.path,
            "loaded_fragment_sha256": unit.content_sha256,
            "daemon_reload_generation_matches_fragment": True,
            "drop_in_paths": (),
            "exec_start_argvs": (_exec_start_argv(spec, runner, operation),),
            "exec_start_pre_argvs": (),
            "exec_start_post_argvs": (),
            "effective_environment": _python_environment_assignments(spec),
            "unset_environment_names": _PYTHON_UNSET_ENVIRONMENT_NAMES,
        }

    common = {
        "working_directory": spec.code_root,
        "python_executable_path": spec.python_executable,
        "python_environment_root": spec.reviewed_python_environment.root_path,
        "python_path": spec.code_root,
        "python_flags": ("-S", "-s", "-P"),
        "no_new_privileges": True,
    }
    return tuple(
        sorted(
            (
                QualificationSystemdServiceIdentityObservation(
                    unit_name=spec.workspace_unit_name,
                    effective_uid=0,
                    effective_gid=0,
                    supplementary_gids=(),
                    effective_capabilities=("CAP_SYS_ADMIN",),
                    private_mounts=False,
                    **loaded_state(
                        spec.workspace_unit_name,
                        spec.workspace_runner_path,
                        "ensure-shared-workspace",
                    ),
                    **common,
                ),
                QualificationSystemdServiceIdentityObservation(
                    unit_name=spec.quota_unit_name,
                    effective_uid=0,
                    effective_gid=0,
                    supplementary_gids=(),
                    effective_capabilities=(
                        "CAP_CHOWN",
                        "CAP_DAC_OVERRIDE",
                        "CAP_FOWNER",
                        "CAP_SYS_ADMIN",
                    ),
                    private_mounts=False,
                    **loaded_state(spec.quota_unit_name, spec.quota_runner_path, "serve"),
                    **common,
                ),
                QualificationSystemdServiceIdentityObservation(
                    unit_name=spec.watchdog_unit_name,
                    effective_uid=0,
                    effective_gid=0,
                    supplementary_gids=(),
                    effective_capabilities=(
                        "CAP_CHOWN",
                        "CAP_DAC_OVERRIDE",
                        "CAP_KILL",
                        "CAP_SYS_ADMIN",
                    ),
                    private_mounts=False,
                    **loaded_state(spec.watchdog_unit_name, spec.watchdog_runner_path, "serve"),
                    **common,
                ),
                QualificationSystemdServiceIdentityObservation(
                    unit_name=spec.node_unit_name,
                    effective_uid=spec.node_uid,
                    effective_gid=spec.node_gid,
                    supplementary_gids=(spec.docker_gid,),
                    effective_capabilities=(),
                    private_mounts=False,
                    worker_poll_milliseconds=spec.worker_poll_milliseconds,
                    **loaded_state(spec.node_unit_name, spec.node_runner_path, "run"),
                    **common,
                ),
                QualificationSystemdServiceIdentityObservation(
                    unit_name=spec.outbox_unit_name,
                    effective_uid=spec.outbox_uid,
                    effective_gid=spec.outbox_gid,
                    supplementary_gids=(),
                    effective_capabilities=(),
                    private_mounts=True,
                    **loaded_state(spec.outbox_unit_name, spec.outbox_runner_path, "run"),
                    **common,
                ),
            ),
            key=lambda item: item.unit_name,
        )
    )


def _service_module_matches_nested_pin(
    installed: PinnedRootFile,
    expected: QualificationExpectedRootFile,
    deployment: LoopbackQuotaProvisionerDeploymentPin | SystemdWatchdogDeploymentPin,
) -> bool:
    return (
        _installed_file_matches(expected, installed)
        and deployment.service_module_sha256 == installed.sha256
        and deployment.service_module_device == installed.device
        and deployment.service_module_inode == installed.inode
        and deployment.service_module_mode == installed.mode
        and deployment.service_module_parent_chain_sha256 == installed.parent_chain_sha256
    )


def _installed_native_file_matches(
    expected: ReviewedNativeDependencyFile,
    installed: PinnedRootFile,
) -> bool:
    return (
        installed.path == expected.path
        and installed.sha256 == expected.reviewed_sha256
        and installed.owner_uid == expected.expected_owner_uid
        and installed.owner_gid == expected.expected_owner_gid
        and installed.mode == expected.expected_mode
        and (not expected.executable_required or installed.mode & 0o505 == 0o505)
    )


def _native_dependency_closure_matches(
    expected: ReviewedNativeDependencyClosure,
    observed: ObservedNativeDependencyClosure,
) -> bool:
    expected_dependencies = {item.soname: item for item in expected.dependencies}
    observed_dependencies = {item.soname: item for item in observed.dependencies}
    return (
        _installed_file_matches(expected.executable, observed.executable)
        and _installed_native_file_matches(expected.elf_interpreter, observed.elf_interpreter)
        and observed.executable_needed_sonames == expected.executable_needed_sonames
        and set(observed_dependencies) == set(expected_dependencies)
        and all(
            observed_dependencies[soname].needed_sonames == dependency.needed_sonames
            and _installed_native_file_matches(
                dependency.file, observed_dependencies[soname].file
            )
            for soname, dependency in expected_dependencies.items()
        )
        and observed.exhaustive
        and not observed.external_native_dependency_paths
    )


def _quota_persistent_projection(
    deployment: LoopbackQuotaProvisionerDeploymentPin,
) -> dict[str, object]:
    return deployment.model_dump(
        mode="python",
        exclude={
            "workspace_root_pin",
            "socket_parent_device",
            "socket_parent_inode",
            "socket_parent_parent_chain_sha256",
        },
    )


def _watchdog_persistent_projection(
    deployment: SystemdWatchdogDeploymentPin,
) -> dict[str, object]:
    return deployment.model_dump(
        mode="python",
        exclude={
            "socket_parent_device",
            "socket_parent_inode",
            "socket_parent_parent_chain_sha256",
        },
    )


def _same_boot_workspace_and_namespaces_match(
    observation: QualificationLinuxDeploymentObservation,
    baseline: QualificationLinuxDeploymentObservation | None,
) -> bool:
    if baseline is None or observation.boot_id != baseline.boot_id:
        return True
    return (
        observation.output_workspace_root == baseline.output_workspace_root
        and observation.pid_one_mount_namespace == baseline.pid_one_mount_namespace
        and observation.quota_mount_namespace == baseline.quota_mount_namespace
        and observation.node_mount_namespace == baseline.node_mount_namespace
        and observation.docker_mount_namespace == baseline.docker_mount_namespace
    )


def _same_boot_quota_socket_parent_matches(
    current: LoopbackQuotaProvisionerDeploymentPin,
    baseline_observation: QualificationLinuxDeploymentObservation | None,
    current_boot_id: str,
) -> bool:
    if baseline_observation is None or current_boot_id != baseline_observation.boot_id:
        return True
    baseline = baseline_observation.quota_deployment
    return (
        current.socket_parent_device == baseline.socket_parent_device
        and current.socket_parent_inode == baseline.socket_parent_inode
        and current.socket_parent_parent_chain_sha256
        == baseline.socket_parent_parent_chain_sha256
    )


def _same_boot_watchdog_socket_parent_matches(
    current: SystemdWatchdogDeploymentPin,
    baseline_observation: QualificationLinuxDeploymentObservation | None,
    current_boot_id: str,
) -> bool:
    if baseline_observation is None or current_boot_id != baseline_observation.boot_id:
        return True
    baseline = baseline_observation.watchdog_deployment
    return (
        current.socket_parent_device == baseline.socket_parent_device
        and current.socket_parent_inode == baseline.socket_parent_inode
        and current.socket_parent_parent_chain_sha256
        == baseline.socket_parent_parent_chain_sha256
    )


def _observation_blockers(
    *,
    spec: QualificationDeploymentSpecV1,
    observation: QualificationLinuxDeploymentObservation,
    rendered_units: tuple[RenderedSystemdUnit, ...],
    postgresql_acl: bytes,
    installed_baseline: QualificationLinuxDeploymentObservation | None = None,
) -> tuple[tuple[str, ...], dict[str, bool]]:
    expected_entrypoints = {
        pin.path: pin
        for pin in (
            spec.expected_workspace_runner,
            spec.expected_quota_runner,
            spec.expected_watchdog_runner,
            spec.expected_node_runner,
            spec.expected_outbox_runner,
        )
    }
    entrypoints = _file_map(observation.entrypoint_files)
    unit_files = _file_map(observation.systemd_unit_files)
    service_modules = _file_map(observation.service_module_files)
    custody_roots = {item.purpose: item for item in observation.custody_roots}
    expected_custody_roots = _expected_custody_root_policies(spec)
    expected_unit_files = {item.path: item.content_sha256 for item in rendered_units}
    expected_service_modules = {
        pin.path: pin
        for pin in (
            spec.expected_quota_service_module,
            spec.expected_watchdog_service_module,
        )
    }
    roles = {item.role_name: item for item in observation.postgresql_roles}
    expected_roles = {spec.postgresql_allocator_role, spec.postgresql_outbox_role}
    expected_owners = tuple(
        sorted(
            (
                PostgreSQLExecutionObjectOwnerObservation(
                    object_kind="database",
                    object_name=spec.postgresql_database,
                    owner_role=spec.postgresql_owner_role,
                ),
                PostgreSQLExecutionObjectOwnerObservation(
                    object_kind="schema",
                    object_name=spec.postgresql_schema,
                    owner_role=spec.postgresql_owner_role,
                ),
                *(
                    PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="table",
                        object_name=table,
                        owner_role=spec.postgresql_owner_role,
                    )
                    for table in EXECUTION_TABLES
                ),
                *(
                    PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="sequence",
                        object_name=sequence,
                        owner_role=spec.postgresql_owner_role,
                    )
                    for sequence in EXECUTION_SEQUENCES
                ),
                *(
                    PostgreSQLExecutionObjectOwnerObservation(
                        object_kind=routine.routine_kind,
                        object_name=routine.identity,
                        owner_role=spec.postgresql_owner_role,
                    )
                    for routine in spec.expected_postgresql_routines
                ),
            ),
            key=lambda item: (item.object_kind, item.object_name),
        )
    )
    namespaces = {
        observation.pid_one_mount_namespace,
        observation.quota_mount_namespace,
        observation.node_mount_namespace,
        observation.docker_mount_namespace,
    }
    baseline_entrypoints = (
        _file_map(installed_baseline.entrypoint_files) if installed_baseline is not None else None
    )
    baseline_units = (
        _file_map(installed_baseline.systemd_unit_files) if installed_baseline is not None else None
    )
    baseline_service_modules = (
        _file_map(installed_baseline.service_module_files)
        if installed_baseline is not None
        else None
    )
    expected_native_closures = {
        item.executable.path: item for item in spec.reviewed_privileged_tool_native_closures
    }
    observed_native_closures = {
        item.executable.path: item for item in observation.privileged_tool_native_closures
    }
    native_closures_ok = (
        set(observed_native_closures) == set(expected_native_closures)
        and all(
            _native_dependency_closure_matches(
                expected, observed_native_closures[path]
            )
            for path, expected in expected_native_closures.items()
        )
        and (
            installed_baseline is None
            or observation.privileged_tool_native_closures
            == installed_baseline.privileged_tool_native_closures
        )
    )

    host_ok = (
        observation.deployment_id == spec.deployment_id
        and observation.node_id == spec.node_id
        and observation.node_manifest_sha256 == spec.node_manifest_sha256
        and observation.platform == "linux"
        and observation.cpu_architecture == spec.expected_cpu_architecture
        and observation.oci_platform == spec.expected_oci_platform
        and observation.pid_one_comm == "systemd"
        and observation.host_clock_synchronized
        and observation.cgroup_version == 2
        and observation.docker_cgroup_driver == "systemd"
        and observation.docker_security_projection_sha256 == spec.docker_security_projection_sha256
    )
    mount_ok = (
        len(namespaces) == 1
        and observation.shared_output_mount_visible
        and _same_boot_workspace_and_namespaces_match(observation, installed_baseline)
    )
    workspace_source = custody_roots["workspace_source"]
    custody_ok = (
        set(custody_roots) == set(expected_custody_roots)
        and all(
            (
                observed.path,
                observed.owner_uid,
                observed.owner_gid,
                observed.mode,
            )
            == expected_custody_roots[purpose]
            and observed.parent_chain_root_controlled
            and not observed.symlink
            and observed.file_type == "directory"
            for purpose, observed in custody_roots.items()
        )
        and workspace_source.device == observation.output_workspace_root.device
        and workspace_source.inode == observation.output_workspace_root.inode
        and _same_boot_custody_roots_match(
            observation.custody_roots,
            installed_baseline,
            observation.boot_id,
        )
    )
    files_ok = (
        _installed_file_matches(spec.expected_python_executable, observation.python_executable)
        and observation.python_environment_root.path == spec.reviewed_python_environment.root_path
        and observation.python_environment_root.mode
        == spec.reviewed_python_environment.expected_root_mode
        and observation.python_environment_root.tree_manifest_sha256
        == spec.reviewed_python_environment.manifest_sha256
        and observation.python_environment_root.directory_count
        == len(spec.reviewed_python_environment.directories)
        and observation.python_environment_root.regular_file_count
        == len(spec.reviewed_python_environment.entries)
        and observation.python_environment_root.total_regular_file_bytes
        == spec.reviewed_python_environment.total_bytes
        and observation.python_import_paths == spec.expected_python_import_paths
        and not observation.python_external_loaded_native_object_paths
        and set(entrypoints) == set(expected_entrypoints)
        and all(
            _installed_file_matches(expected, entrypoints[path])
            for path, expected in expected_entrypoints.items()
        )
        and observation.code_root.path == spec.reviewed_code_tree.root_path
        and observation.code_root.mode == spec.reviewed_code_tree.expected_root_mode
        and _installed_file_matches(
            spec.expected_deployment_manifest, observation.deployment_manifest_file
        )
        and set(service_modules) == set(expected_service_modules)
        and all(
            _installed_file_matches(expected, service_modules[path])
            for path, expected in expected_service_modules.items()
        )
        and _installed_file_matches(spec.expected_seccomp_profile, observation.seccomp_profile)
        and observation.apparmor_profile.path == spec.apparmor_profile_path
        and observation.apparmor_profile.sha256 == spec.apparmor_profile_sha256
        and (
            installed_baseline is None
            or (
                observation.python_executable == installed_baseline.python_executable
                and observation.python_environment_root
                == installed_baseline.python_environment_root
                and entrypoints == baseline_entrypoints
                and service_modules == baseline_service_modules
                and observation.code_root == installed_baseline.code_root
                and observation.deployment_manifest_file
                == installed_baseline.deployment_manifest_file
                and observation.seccomp_profile == installed_baseline.seccomp_profile
                and observation.apparmor_profile == installed_baseline.apparmor_profile
            )
        )
    )
    units_ok = (
        set(unit_files) == set(expected_unit_files)
        and all(
            unit_files[path].sha256 == digest
            and unit_files[path].owner_uid == 0
            and unit_files[path].owner_gid == 0
            and unit_files[path].mode == 0o444
            for path, digest in expected_unit_files.items()
        )
        and observation.systemd_service_identities == _expected_systemd_service_identities(spec)
        and (installed_baseline is None or unit_files == baseline_units)
    )
    acl_ok = observation.postgresql_acl_sha256 == hashlib.sha256(postgresql_acl).hexdigest()
    schema_ok = (
        observation.schema_revision == spec.expected_schema_revision
        and observation.postgresql_server_identity_sha256 == spec.postgresql_server_identity_sha256
    )
    roles_ok = (
        set(roles) == expected_roles
        and all(item.restricted for item in roles.values())
        and all(
            roles[role_name].table_privileges_sha256
            == postgresql_role_privileges_sha256(spec, role_name=role_name)
            for role_name in expected_roles
        )
    )
    acl_closure_ok = (
        not observation.postgresql_owner_role_inherits
        and not observation.postgresql_owner_direct_role_memberships
        and not observation.postgresql_owner_transitive_role_memberships
        and not observation.postgresql_owner_dangerous_builtin_role_memberships
        and not observation.postgresql_owner_role_members
        and not observation.postgresql_unexpected_database_grants
        and not observation.postgresql_unexpected_schema_grants
        and not observation.postgresql_unexpected_table_grants
        and not observation.postgresql_unexpected_column_grants
        and not observation.postgresql_unexpected_sequence_grants
        and not observation.postgresql_unexpected_routine_execute_grants
        and not observation.postgresql_unexpected_grant_options
        and not observation.postgresql_unexpected_execution_routines
        and observation.postgresql_routines == spec.expected_postgresql_routines
        and observation.postgresql_triggers == spec.expected_postgresql_triggers
        and observation.postgresql_sequences == spec.expected_postgresql_sequences
        and all(
            not item.routine_name.startswith(spec.postgresql_execution_routine_name_prefix)
            and item.owner_role
            not in {
                spec.postgresql_owner_role,
                spec.postgresql_allocator_role,
                spec.postgresql_outbox_role,
            }
            for item in observation.postgresql_non_execution_public_routine_owners
        )
        and (
            installed_baseline is None
            or observation.postgresql_non_execution_public_routine_owners
            == installed_baseline.postgresql_non_execution_public_routine_owners
        )
        and observation.postgresql_execution_object_owners == expected_owners
    )
    clock_ok = observation.host_clock_synchronized and observation.postgresql_clock_healthy
    image_ok = (
        observation.oci_image_layout.policy_sha256 == spec.oci_policy_sha256
        and observation.oci_image_layout.layout_root == spec.oci_layout_root
        and observation.oci_image_layout.reviewed_launch_gate_executable_sha256
        == spec.launch_gate_executable_sha256
        and observation.oci_image_layout.reviewed_launch_gate_protocol_sha256
        == spec.launch_gate_protocol_sha256
        and observation.loaded_image_manifest_sha256 == spec.image_manifest_sha256
        and observation.loaded_image_config_sha256 == spec.image_config_sha256
        and observation.loaded_apparmor_profile_name == spec.apparmor_profile_name
        and observation.apparmor_profile_enforcing
        and (
            installed_baseline is None
            or observation.oci_image_layout == installed_baseline.oci_image_layout
        )
    )
    quota = observation.quota_deployment
    quota_module = service_modules.get(spec.expected_quota_service_module.path)
    quota_unit = unit_files.get(str(Path(spec.systemd_unit_root) / spec.quota_unit_name))
    quota_ok = (
        observation.quota_service_systemd_verified
        and quota.deployment_id == f"{spec.deployment_id}:quota"
        and quota.systemd_unit_name == spec.quota_unit_name
        and quota.workspace_root == spec.output_workspace_root
        and quota.workspace_root_pin == observation.output_workspace_root
        and quota.backing_root == spec.quota_backing_root
        and quota.state_root == spec.quota_state_root
        and quota.socket_path == spec.quota_socket_path
        and quota.allowed_client_uid == spec.node_uid
        and quota.allowed_client_gid == spec.node_gid
        and quota.provisioner_policy_sha256 == spec.output_quota_policy_sha256
        and quota.systemd_unit == quota_unit
        and quota.service_executable == observation.python_executable
        and _installed_file_matches(spec.expected_losetup_executable, quota.losetup)
        and _installed_file_matches(spec.expected_mkfs_ext4_executable, quota.mkfs)
        and _installed_file_matches(spec.expected_mount_executable, quota.mount)
        and native_closures_ok
        and observed_native_closures[quota.losetup.path].executable == quota.losetup
        and observed_native_closures[quota.mkfs.path].executable == quota.mkfs
        and observed_native_closures[quota.mount.path].executable == quota.mount
        and quota_module is not None
        and _service_module_matches_nested_pin(
            quota_module, spec.expected_quota_service_module, quota
        )
        and quota.backing_root_mode == 0o700
        and quota.state_root_mode == 0o700
        and quota.socket_parent_mode == 0o755
        and _same_boot_quota_socket_parent_matches(
            quota, installed_baseline, observation.boot_id
        )
        and (
            installed_baseline is None
            or _quota_persistent_projection(quota)
            == _quota_persistent_projection(installed_baseline.quota_deployment)
        )
    )
    watchdog = observation.watchdog_deployment
    watchdog_module = service_modules.get(spec.expected_watchdog_service_module.path)
    watchdog_unit = unit_files.get(str(Path(spec.systemd_unit_root) / spec.watchdog_unit_name))
    watchdog_ok = (
        observation.watchdog_service_systemd_verified
        and watchdog.deployment_id == f"{spec.deployment_id}:watchdog"
        and watchdog.policy_sha256 == spec.oci_policy_sha256
        and watchdog.systemd_unit_name == spec.watchdog_unit_name
        and watchdog.journal_root == spec.runtime_journal_root
        and watchdog.state_root == spec.watchdog_state_root
        and watchdog.socket_path == spec.watchdog_socket_path
        and watchdog.allowed_client_uid == spec.node_uid
        and watchdog.allowed_client_gid == spec.node_gid
        and watchdog.maximum_active_jobs == spec.maximum_active_watchdog_jobs
        and watchdog.systemd_unit == watchdog_unit
        and watchdog.service_executable == observation.python_executable
        and watchdog_module is not None
        and _service_module_matches_nested_pin(
            watchdog_module, spec.expected_watchdog_service_module, watchdog
        )
        and watchdog.journal_root_mode == 0o700
        and watchdog.state_root_mode == 0o700
        and watchdog.socket_parent_mode == 0o755
        and _same_boot_watchdog_socket_parent_matches(
            watchdog, installed_baseline, observation.boot_id
        )
        and (
            installed_baseline is None
            or _watchdog_persistent_projection(watchdog)
            == _watchdog_persistent_projection(installed_baseline.watchdog_deployment)
        )
    )
    code_ok = (
        observation.agent_implementation_sha256 == spec.agent_implementation_sha256
        and observation.authority_bundle_sha256 == spec.authority_bundle_sha256
        and observation.code_root.tree_manifest_sha256 == spec.reviewed_code_tree.manifest_sha256
        and observation.code_root.directory_count == len(spec.reviewed_code_tree.directories)
        and observation.code_root.regular_file_count == len(spec.reviewed_code_tree.entries)
        and observation.code_root.total_regular_file_bytes == spec.reviewed_code_tree.total_bytes
        and observation.python_environment_root.tree_manifest_sha256
        == spec.reviewed_python_environment.manifest_sha256
        and observation.python_import_paths == spec.expected_python_import_paths
        and not observation.python_external_loaded_native_object_paths
    )
    checks = {
        "linux_systemd_cgroup_verified": host_ok,
        "shared_mount_namespace_verified": mount_ok,
        "installed_files_verified": files_ok,
        "custody_roots_verified": custody_ok,
        "systemd_units_verified": units_ok,
        "postgresql_acl_verified": acl_ok,
        "postgresql_schema_verified": schema_ok,
        "postgresql_roles_verified": roles_ok,
        "postgresql_acl_closure_verified": acl_closure_ok,
        "postgresql_clock_verified": clock_ok,
        "image_layout_verified": image_ok,
        "output_quota_service_verified": quota_ok,
        "deadline_watchdog_service_verified": watchdog_ok,
        "code_identity_verified": code_ok,
    }
    blocker_names = {
        "linux_systemd_cgroup_verified": "host:linux-systemd-cgroup-drift",
        "shared_mount_namespace_verified": "host:shared-mount-namespace-drift",
        "installed_files_verified": "files:installed-custody-drift",
        "custody_roots_verified": "files:service-custody-root-drift",
        "systemd_units_verified": "systemd:unit-bytes-or-custody-drift",
        "postgresql_acl_verified": "postgresql:acl-bytes-drift",
        "postgresql_schema_verified": "postgresql:schema-or-server-drift",
        "postgresql_roles_verified": "postgresql:role-privilege-drift",
        "postgresql_acl_closure_verified": "postgresql:grant-membership-or-owner-closure-drift",
        "postgresql_clock_verified": "postgresql:clock-unhealthy",
        "image_layout_verified": "oci:image-layout-or-loaded-image-drift",
        "output_quota_service_verified": "quota:deployment-drift",
        "deadline_watchdog_service_verified": "watchdog:deployment-drift",
        "code_identity_verified": "code:implementation-or-authority-drift",
    }
    blockers = tuple(sorted(blocker_names[name] for name, passed in checks.items() if not passed))
    return blockers, checks


def _observer_provenance_is_valid(
    *,
    signed: SignedQualificationLinuxDeploymentObservation,
    observer_pin: QualificationDeploymentObserverPin,
    spec: QualificationDeploymentSpecV1,
    rendered_units: tuple[RenderedSystemdUnit, ...],
    postgresql_acl: bytes,
) -> bool:
    if (
        signed.spec_sha256 != spec.spec_sha256
        or signed.rendered_systemd_units_sha256 != canonical_sha256(rendered_units)
        or signed.rendered_postgresql_acl_sha256 != hashlib.sha256(postgresql_acl).hexdigest()
        or signed.observer_policy_sha256 != observer_pin.policy_sha256
        or signed.observer_principal_id != observer_pin.principal_id
        or signed.observer_key_id != observer_pin.key_id
        or not observer_pin.active_at(signed.signed_at)
        or signed.expires_at > observer_pin.active_until
    ):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(observer_pin.public_key_ed25519_hex)
        ).verify(bytes.fromhex(signed.signature_ed25519_hex), signed.message_bytes)
    except (InvalidSignature, ValueError):
        return False
    return True


def _observation_time_is_valid(
    *,
    signed: SignedQualificationLinuxDeploymentObservation,
    observer_pin: QualificationDeploymentObserverPin,
    spec: QualificationDeploymentSpecV1,
    checked_at: datetime,
    strictly_after: datetime | None = None,
) -> bool:
    _require_utc(checked_at, label="deployment observation check time")
    if strictly_after is not None:
        _require_utc(strictly_after, label="deployment observation lower time bound")
    return (
        signed.signed_at <= checked_at < signed.expires_at
        and signed.observation.observed_at - signed.observation.observation_started_at
        <= timedelta(seconds=spec.maximum_observation_duration_seconds)
        and signed.expires_at <= signed.signed_at + timedelta(seconds=spec.observation_ttl_seconds)
        and observer_pin.active_at(checked_at)
        and (
            strictly_after is None
            or signed.observation.observation_started_at > strictly_after
        )
    )


def freeze_installed_manifest(
    spec: QualificationDeploymentSpecV1,
    observer: QualificationDeploymentObserver,
    observer_pin: QualificationDeploymentObserverPin,
) -> QualificationInstalledDeploymentManifestV1:
    """Freeze exact installed evidence; the real interpreter must be running on Linux."""

    if sys.platform != "linux":
        raise QualificationDeploymentEnvironmentError(
            "installed qualification manifests can be frozen only on the real Linux target"
        )
    spec = QualificationDeploymentSpecV1.model_validate(spec.model_dump(mode="python"))
    observer_pin = QualificationDeploymentObserverPin.model_validate(
        observer_pin.model_dump(mode="python")
    )
    units = render_systemd_units(spec)
    acl = render_postgresql_acl(spec)
    try:
        observed = observer.observe(spec=spec, rendered_units=units, postgresql_acl=acl)
        signed = SignedQualificationLinuxDeploymentObservation.model_validate(
            observed.model_dump(mode="python")
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise QualificationDeploymentObservationError(
            "Linux deployment observer returned no closed signed observation"
        ) from exc
    if not _observer_provenance_is_valid(
        signed=signed,
        observer_pin=observer_pin,
        spec=spec,
        rendered_units=units,
        postgresql_acl=acl,
    ):
        raise QualificationDeploymentObservationError(
            "Linux deployment observer signature differs from the external observer pin"
        )
    frozen_at = _monitored_utc_now()
    _require_utc(frozen_at, label="monitored deployment freeze time")
    if not _observation_time_is_valid(
        signed=signed,
        observer_pin=observer_pin,
        spec=spec,
        checked_at=frozen_at,
    ):
        raise QualificationDeploymentObservationError(
            "Linux deployment observer observation is stale, future-dated, or outside its pin"
        )
    observation = signed.observation
    blockers, _checks = _observation_blockers(
        spec=spec,
        observation=observation,
        rendered_units=units,
        postgresql_acl=acl,
    )
    if blockers:
        raise QualificationDeploymentObservationError(
            "installed qualification deployment failed closed: " + ", ".join(blockers)
        )
    return QualificationInstalledDeploymentManifestV1(
        spec=spec,
        spec_sha256=spec.spec_sha256,
        rendered_systemd_units_sha256=canonical_sha256(units),
        postgresql_acl_sha256=hashlib.sha256(acl).hexdigest(),
        observer_pin_sha256=observer_pin.pin_sha256,
        installed_observation=signed,
        installed_observation_sha256=signed.signed_observation_sha256,
        installed_stable_evidence_sha256=observation.stable_evidence_sha256,
        frozen_at=frozen_at,
    )


def _preflight(
    manifest: QualificationInstalledDeploymentManifestV1,
    *,
    observed_at: AwareDatetime,
    verified_at: AwareDatetime,
    blockers: tuple[str, ...],
    checks: Mapping[str, bool],
) -> QualificationDeploymentPreflight:
    canonical_blockers = tuple(sorted(set(blockers)))
    return QualificationDeploymentPreflight(
        deployment_id=manifest.spec.deployment_id,
        spec_sha256=manifest.spec_sha256,
        installed_manifest_sha256=manifest.manifest_sha256,
        observed_at=observed_at,
        verified_at=verified_at,
        blockers=canonical_blockers,
        ready_for_opt_in_campaign=(not canonical_blockers and all(checks.values())),
        **checks,
    )


def verify_installed_manifest(
    manifest: QualificationInstalledDeploymentManifestV1,
    observer: QualificationDeploymentObserver,
    observer_pin: QualificationDeploymentObserverPin,
) -> QualificationDeploymentPreflight:
    """Reobserve an installed target; this never runs the opt-in production campaign."""

    manifest = QualificationInstalledDeploymentManifestV1.model_validate(
        manifest.model_dump(mode="python")
    )
    observer_pin = QualificationDeploymentObserverPin.model_validate(
        observer_pin.model_dump(mode="python")
    )
    check_names = (
        "observer_provenance_verified",
        "observation_freshness_verified",
        "linux_systemd_cgroup_verified",
        "shared_mount_namespace_verified",
        "installed_files_verified",
        "custody_roots_verified",
        "systemd_units_verified",
        "postgresql_acl_verified",
        "postgresql_schema_verified",
        "postgresql_roles_verified",
        "postgresql_acl_closure_verified",
        "postgresql_clock_verified",
        "image_layout_verified",
        "output_quota_service_verified",
        "deadline_watchdog_service_verified",
        "code_identity_verified",
    )
    if sys.platform != "linux":
        verified_at = _monitored_utc_now()
        _require_utc(verified_at, label="monitored deployment verification time")
        return _preflight(
            manifest,
            observed_at=manifest.installed_observation.observation.observed_at,
            verified_at=verified_at,
            blockers=("host:linux-required",),
            checks={name: False for name in check_names},
        )

    units = render_systemd_units(manifest.spec)
    acl = render_postgresql_acl(manifest.spec)
    baseline_signed = manifest.installed_observation
    if (
        manifest.observer_pin_sha256 != observer_pin.pin_sha256
        or not _observer_provenance_is_valid(
            signed=baseline_signed,
            observer_pin=observer_pin,
            spec=manifest.spec,
            rendered_units=units,
            postgresql_acl=acl,
        )
        or not _observation_time_is_valid(
            signed=baseline_signed,
            observer_pin=observer_pin,
            spec=manifest.spec,
            checked_at=manifest.frozen_at,
        )
    ):
        verified_at = _monitored_utc_now()
        _require_utc(verified_at, label="monitored deployment verification time")
        return _preflight(
            manifest,
            observed_at=baseline_signed.observation.observed_at,
            verified_at=verified_at,
            blockers=("observer:installed-provenance-invalid",),
            checks={name: False for name in check_names},
        )
    try:
        observed = observer.observe(spec=manifest.spec, rendered_units=units, postgresql_acl=acl)
        signed = SignedQualificationLinuxDeploymentObservation.model_validate(
            observed.model_dump(mode="python")
        )
    except (AttributeError, OSError, TypeError, ValueError):
        verified_at = _monitored_utc_now()
        _require_utc(verified_at, label="monitored deployment verification time")
        return _preflight(
            manifest,
            observed_at=baseline_signed.observation.observed_at,
            verified_at=verified_at,
            blockers=("observation:unavailable",),
            checks={name: False for name in check_names},
        )
    verified_at = _monitored_utc_now()
    _require_utc(verified_at, label="monitored deployment verification time")
    provenance_ok = _observer_provenance_is_valid(
        signed=signed,
        observer_pin=observer_pin,
        spec=manifest.spec,
        rendered_units=units,
        postgresql_acl=acl,
    )
    freshness_ok = _observation_time_is_valid(
        signed=signed,
        observer_pin=observer_pin,
        spec=manifest.spec,
        checked_at=verified_at,
        strictly_after=manifest.frozen_at,
    )
    if not provenance_ok or not freshness_ok:
        blockers = tuple(
            sorted(
                blocker
                for blocker, failed in (
                    ("observer:provenance-invalid", not provenance_ok),
                    ("observation:rollback-or-stale", not freshness_ok),
                )
                if failed
            )
        )
        failed_checks = {name: False for name in check_names}
        failed_checks["observer_provenance_verified"] = provenance_ok
        failed_checks["observation_freshness_verified"] = freshness_ok
        return _preflight(
            manifest,
            observed_at=signed.observation.observed_at,
            verified_at=verified_at,
            blockers=blockers,
            checks=failed_checks,
        )
    observation = signed.observation
    blockers, checks = _observation_blockers(
        spec=manifest.spec,
        observation=observation,
        rendered_units=units,
        postgresql_acl=acl,
        installed_baseline=baseline_signed.observation,
    )
    checks = {
        "observer_provenance_verified": True,
        "observation_freshness_verified": True,
        **checks,
    }
    return _preflight(
        manifest,
        observed_at=observation.observed_at,
        verified_at=verified_at,
        blockers=blockers,
        checks=checks,
    )


__all__ = [
    "ALLOCATOR_UPDATE_TABLES",
    "EXPECTED_EXECUTION_SCHEMA_REVISION",
    "EXECUTION_SEQUENCES",
    "EXECUTION_TABLES",
    "ObservedNativeDependency",
    "ObservedNativeDependencyClosure",
    "POSTGRESQL_DANGEROUS_BUILTIN_ROLES",
    "PostgreSQLExpectedRoutine",
    "PostgreSQLExpectedSequenceConfiguration",
    "PostgreSQLExpectedTrigger",
    "PostgreSQLExecutionObjectOwnerObservation",
    "PostgreSQLNonExecutionRoutineOwnerObservation",
    "PostgreSQLRestrictedRoleObservation",
    "PostgreSQLUnexpectedPrivilegeObservation",
    "QualificationDeploymentEnvironmentError",
    "QualificationDeploymentError",
    "QualificationDeploymentObservationError",
    "QualificationDeploymentObserver",
    "QualificationDeploymentObserverPin",
    "QualificationDeploymentPreflight",
    "QualificationDeploymentSpecV1",
    "QualificationExpectedRootExecutable",
    "QualificationExpectedRootFile",
    "QualificationInstalledDeploymentManifestV1",
    "QualificationLinuxDeploymentObservation",
    "QualificationObservedCustodyRoot",
    "QualificationObservedRootCodeTree",
    "QualificationReviewedCodeDirectory",
    "QualificationReviewedCodeFile",
    "QualificationReviewedCodeTree",
    "QualificationSystemdServiceIdentityObservation",
    "ReviewedNativeDependency",
    "ReviewedNativeDependencyClosure",
    "ReviewedNativeDependencyFile",
    "RenderedSystemdUnit",
    "SignedQualificationLinuxDeploymentObservation",
    "freeze_installed_manifest",
    "qualification_agent_implementation_sha256",
    "postgresql_role_privileges_sha256",
    "render_postgresql_acl",
    "render_systemd_units",
    "reviewed_code_tree_manifest_sha256",
    "reviewed_native_dependency_closure_sha256",
    "verify_installed_manifest",
]
