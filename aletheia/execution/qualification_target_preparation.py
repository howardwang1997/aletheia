"""Fail-closed preparation of one reviewed qualification Python runtime.

The deployment observer accepts only an exhaustive, root-owned tree without symlinks or
hardlinks.  Conda environments intentionally contain many symlink aliases, and the Python ELF
interpreter normally resolves the host's ambient glibc.  This module turns a disposable Conda
build environment into a separate immutable runtime: aliases are materialized as independent
files/directories, an explicitly pinned native closure is copied into the tree, Python is rebound
to the in-tree loader, and a real ``-S`` import probe must have no native mappings outside the
prepared root.

Preparation is operational evidence only.  The returned tree still has to be embedded in a
reviewed deployment request and pass the independent target-host observer/campaign.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.qualification_deployment import (
    QualificationExpectedRootExecutable,
    QualificationReviewedCodeDirectory,
    QualificationReviewedCodeFile,
    QualificationReviewedCodeTree,
    reviewed_code_tree_manifest_sha256,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REQUEST_ID_PATTERN = r"^qpr_[0-9a-f]{32}$"
_SAFE_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:[.][A-Za-z_][A-Za-z0-9_]*)*$")
_MAX_REQUEST_BYTES = 1024 * 1024
_OPT_IN_CONFIRMATION = "PREPARE_QUALIFICATION_PYTHON_RUNTIME"


class QualificationTargetPreparationError(RuntimeError):
    """The request, source custody, copy, patch, or isolated runtime probe failed closed."""


def _absolute_path(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or value == "/"
        or not candidate.is_absolute()
        or str(candidate) != value
        or _SAFE_ABSOLUTE_PATH.fullmatch(value) is None
        or any(component in {"", ".", ".."} for component in value.split("/")[1:])
    ):
        raise ValueError(f"{label} must be one canonical safe absolute path")
    return candidate


def _relative_path(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or candidate.is_absolute()
        or str(candidate) != value
        or _SAFE_RELATIVE_PATH.fullmatch(value) is None
        or any(component in {"", ".", ".."} for component in value.split("/"))
    ):
        raise ValueError(f"{label} must be one canonical safe relative path")
    return candidate


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


class QualificationNativeRuntimeSourceV1(ExecutionModel):
    """One root-owned native source copied into the prepared Python tree."""

    source_path: str
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_mode: int = Field(ge=0, le=0o7777)
    target_relative_path: str
    target_mode: Literal[0o444, 0o555]
    role: Literal["elf_interpreter", "shared_library"]

    @model_validator(mode="after")
    def _source_is_closed(self) -> "QualificationNativeRuntimeSourceV1":
        _absolute_path(self.source_path, label="native source")
        relative = _relative_path(self.target_relative_path, label="native target")
        if relative.parts[0] != "lib" or len(relative.parts) != 2:
            raise ValueError("native target must be one direct file under the runtime lib root")
        expected_mode = 0o555 if self.role == "elf_interpreter" else 0o444
        if self.target_mode != expected_mode:
            raise ValueError("native target mode differs from its runtime role")
        if self.source_mode & 0o7022 or self.source_mode & 0o404 != 0o404:
            raise ValueError("native source must be root-controlled and worker-readable")
        if self.role == "elf_interpreter" and self.source_mode & 0o101 != 0o101:
            raise ValueError("native ELF interpreter source must be executable")
        return self


class QualificationPythonRuntimePreparationRequestV1(ExecutionModel):
    """Externally SHA-pinned request for one non-authoritative runtime preparation."""

    schema_name: Literal["aletheia.qualification_python_runtime_preparation_request"] = (
        "aletheia.qualification_python_runtime_preparation_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=_REQUEST_ID_PATTERN)
    source_environment_root: str
    target_environment_root: str
    source_patchelf: QualificationExpectedRootExecutable
    python_relative_path: str = "bin/python"
    site_packages_relative_path: str
    native_sources: tuple[QualificationNativeRuntimeSourceV1, ...] = Field(min_length=7)
    probe_modules: tuple[str, ...] = Field(min_length=1)
    requested_at: AwareDatetime
    opt_in_confirmation: Literal["PREPARE_QUALIFICATION_PYTHON_RUNTIME"] = _OPT_IN_CONFIRMATION
    source_environment_is_disposable: Literal[True] = True
    target_environment_must_be_absent: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_closed(self) -> "QualificationPythonRuntimePreparationRequestV1":
        source = _absolute_path(self.source_environment_root, label="source environment root")
        target = _absolute_path(self.target_environment_root, label="target environment root")
        patchelf = _absolute_path(self.source_patchelf.path, label="source patchelf")
        python = _relative_path(self.python_relative_path, label="runtime Python")
        site_packages = _relative_path(
            self.site_packages_relative_path,
            label="runtime site-packages",
        )
        if (
            _paths_overlap(source, target)
            or source not in patchelf.parents
            or python != Path("bin/python")
            or re.fullmatch(r"lib/python[0-9]+[.][0-9]+/site-packages", str(site_packages)) is None
        ):
            raise ValueError("runtime source, target, Python, site-packages, or patchelf differs")
        targets = tuple(item.target_relative_path for item in self.native_sources)
        if targets != tuple(sorted(set(targets))):
            raise ValueError("native runtime targets must be unique and canonically ordered")
        if sum(item.role == "elf_interpreter" for item in self.native_sources) != 1:
            raise ValueError("native runtime request requires exactly one ELF interpreter")
        if self.probe_modules != tuple(sorted(set(self.probe_modules))) or any(
            _MODULE_NAME.fullmatch(value) is None for value in self.probe_modules
        ):
            raise ValueError("runtime probe modules must be canonical and unique")
        if self.requested_at.utcoffset() != timezone.utc.utcoffset(self.requested_at):
            raise ValueError("runtime preparation request time must be UTC")
        expected_id = f"qpr_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected_id:
            raise ValueError("runtime preparation request id is not derived")
        object.__setattr__(self, "request_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def file_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


class QualificationPythonRuntimePreparationPlanV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_python_runtime_preparation_plan"] = (
        "aletheia.qualification_python_runtime_preparation_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str | None = Field(default=None, pattern=r"^qpp_[0-9a-f]{32}$")
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_environment_root: str
    target_environment_root: str
    native_target_relative_paths: tuple[str, ...]
    probe_modules: tuple[str, ...]
    mutation_enabled: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _plan_id_is_derived(self) -> "QualificationPythonRuntimePreparationPlanV1":
        expected = f"qpp_{self.identity_sha256[:32]}"
        if self.plan_id is not None and self.plan_id != expected:
            raise ValueError("runtime preparation plan id is not derived")
        object.__setattr__(self, "plan_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"}))


class QualificationPythonRuntimePreparationReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_python_runtime_preparation_receipt"] = (
        "aletheia.qualification_python_runtime_preparation_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^qpc_[0-9a-f]{32}$")
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewed_python_environment: QualificationReviewedCodeTree
    expected_python_executable: QualificationExpectedRootExecutable
    site_packages_path: str
    probe_modules: tuple[str, ...]
    loaded_native_paths: tuple[str, ...]
    external_native_paths: tuple[str, ...] = Field(max_length=0)
    symlink_count: Literal[0] = 0
    hardlinked_regular_file_count: Literal[0] = 0
    completed_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    deployment_qualified: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_closed(self) -> "QualificationPythonRuntimePreparationReceiptV1":
        root = Path(self.reviewed_python_environment.root_path)
        python = Path(self.expected_python_executable.path)
        site_packages = Path(self.site_packages_path)
        if (
            root not in python.parents
            or root not in site_packages.parents
            or not self.loaded_native_paths
            or self.loaded_native_paths != tuple(sorted(set(self.loaded_native_paths)))
            or any(
                root != Path(value) and root not in Path(value).parents
                for value in self.loaded_native_paths
            )
            or self.completed_at.utcoffset() != timezone.utc.utcoffset(self.completed_at)
        ):
            raise ValueError("prepared runtime receipt escaped its exhaustive reviewed tree")
        relative_python = python.relative_to(root).as_posix()
        relative_site_packages = site_packages.relative_to(root).as_posix()
        python_entry = next(
            (
                item
                for item in self.reviewed_python_environment.entries
                if item.relative_path == relative_python
            ),
            None,
        )
        if (
            python_entry is None
            or python_entry.reviewed_sha256 != self.expected_python_executable.reviewed_sha256
            or python_entry.expected_mode != self.expected_python_executable.expected_mode
            or relative_site_packages
            not in {item.relative_path for item in self.reviewed_python_environment.directories}
            or self.probe_modules != tuple(sorted(set(self.probe_modules)))
        ):
            raise ValueError("prepared runtime receipt does not bind its Python or imports")
        expected = f"qpc_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected:
            raise ValueError("runtime preparation receipt id is not derived")
        object.__setattr__(self, "receipt_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


def build_qualification_python_runtime_preparation_plan(
    request: QualificationPythonRuntimePreparationRequestV1,
) -> QualificationPythonRuntimePreparationPlanV1:
    request = QualificationPythonRuntimePreparationRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    assert request.request_id is not None
    return QualificationPythonRuntimePreparationPlanV1(
        request_id=request.request_id,
        request_sha256=request.file_sha256,
        source_environment_root=request.source_environment_root,
        target_environment_root=request.target_environment_root,
        native_target_relative_paths=tuple(
            item.target_relative_path for item in request.native_sources
        ),
        probe_modules=request.probe_modules,
    )


def _stream_file_sha256(path: Path) -> tuple[str, os.stat_result]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise QualificationTargetPreparationError(f"file cannot be opened safely: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise QualificationTargetPreparationError(f"path is not a regular file: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (  # noqa: E731 - one exact before/after projection
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after) or total != before.st_size:
        raise QualificationTargetPreparationError(f"file changed while hashed: {path}")
    return digest.hexdigest(), after


def _source_inventory_sha256(root: Path, *, expected_owner_uid: int) -> str:
    """Hash every source directory, regular file, and in-tree symlink before and after copy."""

    try:
        root_metadata = root.lstat()
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise QualificationTargetPreparationError("source environment root is unavailable") from exc
    if (
        resolved_root != root
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != expected_owner_uid
    ):
        raise QualificationTargetPreparationError("source environment root custody differs")
    entries: list[dict[str, object]] = []
    for current_value, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(current_value)
        for name in (*directory_names, *file_names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise QualificationTargetPreparationError(
                    f"source environment entry is unavailable: {relative}"
                ) from exc
            common = {
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
            }
            if metadata.st_uid != expected_owner_uid:
                raise QualificationTargetPreparationError(
                    f"source environment entry has another owner: {relative}"
                )
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                    target_metadata = resolved.lstat()
                except (OSError, ValueError, RuntimeError) as exc:
                    raise QualificationTargetPreparationError(
                        f"source environment symlink escaped or is invalid: {relative}"
                    ) from exc
                if not (
                    stat.S_ISREG(target_metadata.st_mode) or stat.S_ISDIR(target_metadata.st_mode)
                ):
                    raise QualificationTargetPreparationError(
                        f"source environment symlink targets a special file: {relative}"
                    )
                if stat.S_ISDIR(target_metadata.st_mode) and (
                    resolved == current or resolved in path.parents
                ):
                    raise QualificationTargetPreparationError(
                        f"source environment directory symlink is recursive: {relative}"
                    )
                entries.append(
                    {
                        **common,
                        "kind": "symlink",
                        "link_target": os.readlink(path),
                        "resolved_target": resolved.relative_to(root).as_posix(),
                    }
                )
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append({**common, "kind": "directory"})
            elif stat.S_ISREG(metadata.st_mode):
                digest, after = _stream_file_sha256(path)
                entries.append(
                    {
                        **common,
                        "kind": "regular",
                        "sha256": digest,
                        "byte_length": after.st_size,
                        "link_count": after.st_nlink,
                    }
                )
            else:
                raise QualificationTargetPreparationError(
                    f"source environment contains a special file: {relative}"
                )
    return canonical_sha256(
        {
            "schema": "aletheia.qualification_python_source_inventory",
            "schema_version": 1,
            "root": str(root),
            "root_mode": stat.S_IMODE(root_metadata.st_mode),
            "root_owner_uid": root_metadata.st_uid,
            "root_owner_gid": root_metadata.st_gid,
            "entries": entries,
        }
    )


def _verify_native_source(source: QualificationNativeRuntimeSourceV1) -> None:
    digest, metadata = _stream_file_sha256(Path(source.source_path))
    if (
        digest != source.source_sha256
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != source.source_mode
    ):
        raise QualificationTargetPreparationError(
            f"native runtime source differs from its pin: {source.source_path}"
        )


def _verify_patchelf(pin: QualificationExpectedRootExecutable) -> None:
    digest, metadata = _stream_file_sha256(Path(pin.path))
    if (
        digest != pin.reviewed_sha256
        or metadata.st_uid != pin.expected_owner_uid
        or metadata.st_gid != pin.expected_owner_gid
        or stat.S_IMODE(metadata.st_mode) != pin.expected_mode
    ):
        raise QualificationTargetPreparationError("source patchelf differs from its exact pin")


def _normalize_materialized_tree(
    root: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    for current_value, directory_names, file_names in os.walk(root, topdown=False):
        current = Path(current_value)
        for name in file_names:
            path = current / name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise QualificationTargetPreparationError(
                    "materialized runtime retained a symlink or special file"
                )
            os.chown(path, owner_uid, owner_gid, follow_symlinks=False)
            os.chmod(path, 0o555 if metadata.st_mode & 0o111 else 0o444, follow_symlinks=False)
        for name in directory_names:
            path = current / name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise QualificationTargetPreparationError(
                    "materialized runtime retained a symlink or special directory"
                )
            os.chown(path, owner_uid, owner_gid, follow_symlinks=False)
            os.chmod(path, 0o555, follow_symlinks=False)
    os.chown(root, owner_uid, owner_gid, follow_symlinks=False)
    os.chmod(root, 0o555, follow_symlinks=False)


def _remove_exact_preparation_tree(
    path: Path,
    *,
    expected_device: int,
    expected_inode: int,
) -> None:
    """Remove only the exact staging inode created by this preparation attempt."""

    try:
        observed = path.lstat()
    except OSError as exc:
        raise QualificationTargetPreparationError(
            "failed preparation tree disappeared before cleanup"
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
        expected_device,
        expected_inode,
    ):
        raise QualificationTargetPreparationError(
            "failed preparation tree was rebound before cleanup"
        )
    shutil.rmtree(path)


def _patch_python(
    *,
    patchelf: Path,
    python: Path,
    interpreter: Path,
) -> None:
    completed = subprocess.run(
        (
            str(patchelf),
            "--set-interpreter",
            str(interpreter),
            "--set-rpath",
            "$ORIGIN/../lib",
            str(python),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise QualificationTargetPreparationError(
            "patchelf failed to bind the reviewed Python interpreter"
        )


def _probe_runtime(
    *,
    root: Path,
    python_relative_path: str,
    site_packages_relative_path: str,
    modules: tuple[str, ...],
) -> tuple[str, ...]:
    python = root / python_relative_path
    site_packages = root / site_packages_relative_path
    timezone_data = root / "share" / "zoneinfo"
    try:
        timezone_metadata = timezone_data.lstat()
    except OSError as exc:
        raise QualificationTargetPreparationError(
            "prepared Python timezone data is unavailable"
        ) from exc
    if timezone_data.is_symlink() or not stat.S_ISDIR(timezone_metadata.st_mode):
        raise QualificationTargetPreparationError(
            "prepared Python timezone data is not a directory"
        )
    code = (
        "import importlib,json,sys,zoneinfo;"
        f"sys.path.append({str(site_packages)!r});"
        f"assert zoneinfo.TZPATH==({str(timezone_data)!r},);"
        "zoneinfo.ZoneInfo('UTC');zoneinfo.ZoneInfo('Etc/UTC');"
        f"[importlib.import_module(value) for value in {modules!r}];"
        "paths=sorted({line.split(None,5)[5].removesuffix(' (deleted)') "
        "for line in open('/proc/self/maps',encoding='ascii') "
        "if len(line.split(None,5))==6 and line.split(None,5)[5].startswith('/')});"
        "print(json.dumps(paths,separators=(',',':')))"
    )
    environment = {
        "ALETHEIA_QUALIFICATION_SITE_PACKAGES": str(site_packages),
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONHOME": str(root),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONTZPATH": str(timezone_data),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        (str(python), "-S", "-s", "-P", "-c", code),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    if completed.returncode != 0:
        raise QualificationTargetPreparationError(
            "prepared Python failed its isolated dependency/native-map probe"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationTargetPreparationError("runtime probe output is invalid") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise QualificationTargetPreparationError("runtime probe output is not a path list")
    paths = tuple(value)
    if paths != tuple(sorted(set(paths))):
        raise QualificationTargetPreparationError("runtime probe paths are not canonical")
    external = tuple(
        path for path in paths if Path(path) != root and root not in Path(path).parents
    )
    if external:
        raise QualificationTargetPreparationError(
            "prepared Python loaded native objects outside its reviewed tree: "
            + ", ".join(external)
        )
    return paths


def _root_owned_reviewed_tree(root: Path) -> QualificationReviewedCodeTree:
    metadata = root.lstat()
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (0, 0, 0o555)
    ):
        raise QualificationTargetPreparationError("prepared runtime root custody differs")
    directories: list[QualificationReviewedCodeDirectory] = []
    files: list[QualificationReviewedCodeFile] = []
    for current_value, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(current_value)
        for name in directory_names:
            path = current / name
            observed = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISDIR(observed.st_mode)
                or (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode))
                != (0, 0, 0o555)
            ):
                raise QualificationTargetPreparationError(
                    "prepared runtime directory custody differs"
                )
            directories.append(
                QualificationReviewedCodeDirectory(
                    relative_path=path.relative_to(root).as_posix(),
                    expected_mode=0o555,
                )
            )
        for name in file_names:
            path = current / name
            digest, observed = _stream_file_sha256(path)
            mode = stat.S_IMODE(observed.st_mode)
            if observed.st_uid != 0 or observed.st_gid != 0 or observed.st_nlink != 1:
                raise QualificationTargetPreparationError("prepared runtime file custody differs")
            files.append(
                QualificationReviewedCodeFile(
                    relative_path=path.relative_to(root).as_posix(),
                    reviewed_sha256=digest,
                    byte_length=observed.st_size,
                    expected_mode=mode,
                )
            )
    directory_tuple = tuple(sorted(directories, key=lambda item: item.relative_path))
    file_tuple = tuple(sorted(files, key=lambda item: item.relative_path))
    manifest_sha256 = reviewed_code_tree_manifest_sha256(
        root_path=str(root),
        directories=directory_tuple,
        entries=file_tuple,
        expected_root_mode=0o555,
    )
    return QualificationReviewedCodeTree(
        root_path=str(root),
        expected_root_mode=0o555,
        directories=directory_tuple,
        entries=file_tuple,
        manifest_sha256=manifest_sha256,
    )


def prepare_qualification_python_runtime(
    request: QualificationPythonRuntimePreparationRequestV1,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> QualificationPythonRuntimePreparationReceiptV1:
    """Materialize, patch, probe, and freeze one exact root-owned runtime tree."""

    request = QualificationPythonRuntimePreparationRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
        raise QualificationTargetPreparationError("runtime preparation requires Linux root:root")
    source = Path(request.source_environment_root)
    target = Path(request.target_environment_root)
    parent = target.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise QualificationTargetPreparationError("runtime target parent is unavailable") from exc
    if (
        target.exists()
        or target.is_symlink()
        or parent.is_symlink()
        or parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise QualificationTargetPreparationError("runtime target is present or parent is unsafe")
    _verify_patchelf(request.source_patchelf)
    for native_source in request.native_sources:
        _verify_native_source(native_source)
    source_before = _source_inventory_sha256(source, expected_owner_uid=0)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    staging_metadata = staging.lstat()
    preparation_identity = (staging_metadata.st_dev, staging_metadata.st_ino)
    published = False
    try:
        shutil.copytree(
            source,
            staging,
            symlinks=False,
            copy_function=shutil.copy2,
            dirs_exist_ok=True,
        )
        for native_source in request.native_sources:
            destination = staging / native_source.target_relative_path
            if destination.is_symlink() or destination.exists():
                if destination.is_dir() and not destination.is_symlink():
                    raise QualificationTargetPreparationError(
                        "native target collided with a materialized directory"
                    )
                destination.unlink()
            shutil.copyfile(native_source.source_path, destination, follow_symlinks=False)
            os.chmod(destination, native_source.target_mode, follow_symlinks=False)
            os.chown(destination, 0, 0, follow_symlinks=False)
        interpreter_relative = next(
            item.target_relative_path
            for item in request.native_sources
            if item.role == "elf_interpreter"
        )
        staging_python = staging / request.python_relative_path
        _patch_python(
            patchelf=Path(request.source_patchelf.path),
            python=staging_python,
            interpreter=staging / interpreter_relative,
        )
        _probe_runtime(
            root=staging,
            python_relative_path=request.python_relative_path,
            site_packages_relative_path=request.site_packages_relative_path,
            modules=request.probe_modules,
        )
        _patch_python(
            patchelf=Path(request.source_patchelf.path),
            python=staging_python,
            interpreter=target / interpreter_relative,
        )
        _normalize_materialized_tree(staging)
        source_after = _source_inventory_sha256(source, expected_owner_uid=0)
        if source_before != source_after:
            raise QualificationTargetPreparationError("source environment changed during copy")
        os.rename(staging, target)
        published = True
        loaded_paths = _probe_runtime(
            root=target,
            python_relative_path=request.python_relative_path,
            site_packages_relative_path=request.site_packages_relative_path,
            modules=request.probe_modules,
        )
        reviewed = _root_owned_reviewed_tree(target)
        python_path = target / request.python_relative_path
        python_entry = next(
            item for item in reviewed.entries if item.relative_path == request.python_relative_path
        )
        expected_python = QualificationExpectedRootExecutable(
            path=str(python_path),
            reviewed_sha256=python_entry.reviewed_sha256,
            expected_mode=python_entry.expected_mode,
        )
        completed_at = clock()
        if completed_at.utcoffset() != timezone.utc.utcoffset(completed_at):
            raise QualificationTargetPreparationError("runtime preparation clock must return UTC")
        assert request.request_id is not None
        return QualificationPythonRuntimePreparationReceiptV1(
            request_id=request.request_id,
            request_sha256=request.file_sha256,
            source_inventory_sha256=source_before,
            reviewed_python_environment=reviewed,
            expected_python_executable=expected_python,
            site_packages_path=str(target / request.site_packages_relative_path),
            probe_modules=request.probe_modules,
            loaded_native_paths=loaded_paths,
            external_native_paths=(),
            completed_at=completed_at,
        )
    except Exception:
        failed_path = target if published else staging
        try:
            _remove_exact_preparation_tree(
                failed_path,
                expected_device=preparation_identity[0],
                expected_inode=preparation_identity[1],
            )
        except Exception as cleanup_exc:
            raise QualificationTargetPreparationError(
                "runtime preparation failed and its exact tree could not be removed"
            ) from cleanup_exc
        raise


def load_qualification_python_runtime_preparation_request(
    path: str | Path,
    *,
    expected_file_sha256: str,
    require_root_custody: bool,
) -> QualificationPythonRuntimePreparationRequestV1:
    candidate = Path(path)
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise QualificationTargetPreparationError(
            "runtime preparation request is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= _MAX_REQUEST_BYTES
            or before.st_nlink != 1
            or (
                require_root_custody
                and (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (0, 0, 0o400)
            )
        ):
            raise QualificationTargetPreparationError("runtime preparation request custody differs")
        payload = os.read(descriptor, _MAX_REQUEST_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or hashlib.sha256(payload).hexdigest() != expected_file_sha256
    ):
        raise QualificationTargetPreparationError("runtime preparation request changed or differs")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        duplicates = [key for key, count in Counter(key for key, _ in pairs).items() if count > 1]
        if duplicates:
            raise ValueError(f"duplicate JSON keys: {duplicates}")
        return dict(pairs)

    try:
        request = QualificationPythonRuntimePreparationRequestV1.model_validate(
            json.loads(payload, object_pairs_hook=unique_object)
        )
    except (TypeError, ValueError) as exc:
        raise QualificationTargetPreparationError("runtime preparation request is invalid") from exc
    if payload != canonical_json_bytes(request):
        raise QualificationTargetPreparationError("runtime preparation request is not canonical")
    return request


def _emit(value: ExecutionModel) -> None:
    """Write one canonical JSON record with an explicit line delimiter."""

    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    sys.stdout.buffer.flush()


def run_qualification_python_runtime_preparation_cli(
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge")
    args = parser.parse_args(argv)
    request = load_qualification_python_runtime_preparation_request(
        args.request,
        expected_file_sha256=args.request_sha256,
        require_root_custody=args.apply,
    )
    if not args.apply:
        _emit(build_qualification_python_runtime_preparation_plan(request))
        return 0
    if args.acknowledge != request.opt_in_confirmation:
        parser.error(f"--apply requires --acknowledge {_OPT_IN_CONFIRMATION}")
    _emit(prepare_qualification_python_runtime(request))
    return 0


__all__ = [
    "QualificationNativeRuntimeSourceV1",
    "QualificationPythonRuntimePreparationPlanV1",
    "QualificationPythonRuntimePreparationReceiptV1",
    "QualificationPythonRuntimePreparationRequestV1",
    "QualificationTargetPreparationError",
    "build_qualification_python_runtime_preparation_plan",
    "load_qualification_python_runtime_preparation_request",
    "prepare_qualification_python_runtime",
    "run_qualification_python_runtime_preparation_cli",
]
