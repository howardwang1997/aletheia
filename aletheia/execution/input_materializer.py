"""Crash-idempotent materialization of verified local-CAS qualification inputs.

The workload input tree contains only deployment-pinned input-port paths.  Receipts and locks live
in a separate node-owned journal root, so they cannot accidentally become undeclared workload
inputs.  Every replay freshly validates central custody and every copy is streamed and rehashed
before an immutable name is published and fsynced.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from aletheia.execution.artifact_store import ArtifactStoreError, LocalArtifactStore
from aletheia.execution.runtime_v2_contracts import (
    InputMaterializationEntry,
    InputMaterializationReceipt,
    PinnedInputPath,
)
from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionIntent,
    canonical_json_bytes,
    canonical_sha256,
)

_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class InputMaterializationError(RuntimeError):
    """Verified input bytes or their destination custody failed closed validation."""


class InputMaterializerClock(Protocol):
    """Wall-clock boundary used only when first publishing a receipt."""

    def now(self) -> datetime: ...


class SystemInputMaterializerClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _ResolvedInput:
    input_port_id: str
    relative_path: str
    verified_receipt_sha256: str
    content_sha256: str
    content_bytes: int


class LocalCASInputMaterializer:
    """Copy exact verified CAS objects into a sealed attempt-scoped input tree."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        journal_root: Path,
        path_pins: tuple[PinnedInputPath, ...],
        materializer_principal_id: str = "execution-input-materializer",
        clock: InputMaterializerClock | None = None,
    ) -> None:
        if not isinstance(artifact_store, LocalArtifactStore):
            raise TypeError("local input materialization requires LocalArtifactStore custody")
        validated = tuple(
            PinnedInputPath.model_validate(item.model_dump(mode="python")) for item in path_pins
        )
        expected_order = tuple(sorted(validated, key=lambda item: item.input_port_id))
        ports = tuple(item.input_port_id for item in validated)
        paths = tuple(item.relative_path for item in validated)
        if (
            validated != expected_order
            or len(set(ports)) != len(ports)
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("input path pins must have unique canonical ports and paths")
        try:
            # Reuse the shared contract's closed symbolic-principal validator.
            probe = InputMaterializationReceipt(
                intent_sha256="0" * 64,
                execution_id="exe_" + "0" * 32,
                infrastructure_attempt_id="iat_" + "0" * 32,
                entries=(),
                staged_root_identity_sha256="0" * 64,
                materializer_principal_id=materializer_principal_id,
                materialized_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
        except ValidationError as exc:
            raise ValueError("input materializer principal is invalid") from exc
        del probe
        self._artifact_store = artifact_store
        self._pins = {item.input_port_id: item for item in validated}
        self._principal_id = materializer_principal_id
        self._clock = clock or SystemInputMaterializerClock()
        self._journal_root = self._prepare_private_root(journal_root, "materialization journal")
        if self._roots_overlap(self._journal_root, artifact_store.root):
            raise ValueError("materialization journal and artifact-store roots must not overlap")

    def ensure_verified_inputs(
        self,
        *,
        intent: ExecutionIntent,
        destination: Path,
    ) -> InputMaterializationReceipt:
        """Canonical v2 port method returning the complete typed receipt."""

        return self.ensure_verified_inputs_receipt(
            intent=intent,
            destination=destination,
        )

    def ensure_verified_inputs_sha256(
        self,
        *,
        intent: ExecutionIntent,
        destination: Path,
    ) -> str:
        """Convenience projection for callers that only persist the exact receipt identity."""

        return self.ensure_verified_inputs(
            intent=intent,
            destination=destination,
        ).materialization_receipt_sha256

    def ensure_verified_inputs_receipt(
        self,
        *,
        intent: ExecutionIntent,
        destination: Path,
    ) -> InputMaterializationReceipt:
        """Materialize or byte-identically replay a complete read-only input tree."""

        intent = self._validate_intent(intent)
        root = self._validated_destination(destination)
        if self._roots_overlap(root, self._journal_root):
            raise InputMaterializationError(
                "input destination and materialization journal roots must not overlap"
            )
        sources = self._resolve_inputs(intent)
        receipt_path = self._receipt_path(intent)
        with self._attempt_lock(intent):
            self._recover_interrupted_receipt_publish(receipt_path)
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt = self._read_receipt(receipt_path)
                self._validate_receipt_scope(receipt, intent=intent, sources=sources)
                self._validate_exact_tree(root, receipt)
                return receipt

            self._recover_interrupted_copies(root, sources)
            self._validate_partial_tree(root, sources)
            for source in sources:
                self._materialize_entry(root, source)
            self._seal_tree(root)
            entries = tuple(self._entry_for_file(root, item) for item in sources)
            receipt = InputMaterializationReceipt(
                intent_sha256=intent.intent_sha256,
                execution_id=intent.execution_id,
                infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
                entries=entries,
                staged_root_identity_sha256=self._root_identity(root, entries),
                materializer_principal_id=self._principal_id,
                materialized_at=self._utc_now(),
            )
            self._validate_exact_tree(root, receipt)
            self._publish_receipt(receipt_path, receipt)
            return receipt

    def load_receipt(
        self,
        *,
        intent: ExecutionIntent,
        destination: Path,
    ) -> InputMaterializationReceipt | None:
        """Reload one attempt receipt, freshly revalidating CAS authority and the staged tree."""

        intent = self._validate_intent(intent)
        root = self._validated_destination(destination)
        if self._roots_overlap(root, self._journal_root):
            raise InputMaterializationError(
                "input destination and materialization journal roots must not overlap"
            )
        receipt_path = self._receipt_path(intent)
        with self._attempt_lock(intent):
            self._recover_interrupted_receipt_publish(receipt_path)
            if not receipt_path.exists() and not receipt_path.is_symlink():
                return None
            sources = self._resolve_inputs(intent)
            receipt = self._read_receipt(receipt_path)
            self._validate_receipt_scope(receipt, intent=intent, sources=sources)
            self._validate_exact_tree(root, receipt)
            return receipt

    @staticmethod
    def _validate_intent(intent: ExecutionIntent) -> ExecutionIntent:
        try:
            return ExecutionIntent.model_validate(intent.model_dump(mode="python", warnings="none"))
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise InputMaterializationError("input intent failed closed-model validation") from exc

    @staticmethod
    def _prepare_private_root(path: Path, label: str) -> Path:
        candidate = Path(path)
        if candidate.is_symlink():
            raise ValueError(f"{label} root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            root = candidate.resolve(strict=True)
            metadata = root.lstat()
        except OSError as exc:
            raise ValueError(f"{label} root is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError(f"{label} root must be an owner-controlled private directory")
        return root

    def _validated_destination(self, destination: Path) -> Path:
        candidate = Path(destination)
        if candidate.is_symlink():
            raise InputMaterializationError("input destination cannot be a symlink")
        try:
            root = candidate.resolve(strict=True)
            metadata = root.lstat()
        except OSError as exc:
            raise InputMaterializationError("input destination must already exist") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise InputMaterializationError(
                "input destination must be an owner-controlled private directory"
            )
        if self._roots_overlap(root, self._artifact_store.root):
            raise InputMaterializationError(
                "input destination and artifact-store roots must not overlap"
            )
        return root

    @staticmethod
    def _roots_overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    def _receipt_path(self, intent: ExecutionIntent) -> Path:
        attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
        return self._journal_root / f"{attempt_id}.input.json"

    @contextmanager
    def _attempt_lock(self, intent: ExecutionIntent) -> Iterator[None]:
        attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
        path = self._journal_root / f"{attempt_id}.lock"
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise InputMaterializationError("materialization lock custody is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise InputMaterializationError("materialization lock could not be acquired") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)

    def _resolve_inputs(self, intent: ExecutionIntent) -> tuple[_ResolvedInput, ...]:
        bindings = intent.input_artifact_bindings
        if set(self._pins) != {item.input_port_id for item in bindings}:
            raise InputMaterializationError(
                "deployment input path pins differ from the exact intent ports"
            )
        resolved: list[_ResolvedInput] = []
        for binding in bindings:
            pin = self._pins[binding.input_port_id]
            try:
                verified = self._artifact_store.load_verified_receipt(
                    verified_receipt_sha256=binding.artifact_verified_receipt_sha256
                )
                if verified is None:
                    raise InputMaterializationError(
                        "intent input receipt is absent from immutable local custody"
                    )
                manifest = self._artifact_store.load_manifest(
                    manifest_sha256=verified.artifact_manifest_sha256
                )
            except ArtifactStoreError as exc:
                raise InputMaterializationError(
                    "intent input failed fresh CAS/custody revalidation"
                ) from exc
            artifact = verified.artifact
            if (
                manifest is None
                or verified.verified_receipt_sha256 != binding.artifact_verified_receipt_sha256
                or verified.artifact_manifest_sha256 != manifest.manifest_sha256
                or verified.producer_attempt_id != manifest.infrastructure_attempt_id
                or artifact.role is not ArtifactRole.RAW_OUTPUT
                or sum(item == artifact for item in manifest.entries) != 1
            ):
                raise InputMaterializationError(
                    "intent input receipt, manifest, or raw-output custody diverges"
                )
            resolved.append(
                _ResolvedInput(
                    input_port_id=binding.input_port_id,
                    relative_path=pin.relative_path,
                    verified_receipt_sha256=verified.verified_receipt_sha256,
                    content_sha256=artifact.content_sha256,
                    content_bytes=artifact.bytes,
                )
            )
        return tuple(sorted(resolved, key=lambda item: item.input_port_id))

    @staticmethod
    def _relative_path(value: str) -> Path:
        try:
            pin = PinnedInputPath(input_port_id="input", relative_path=value)
        except ValidationError as exc:
            raise InputMaterializationError("materialized input path is not canonical") from exc
        return Path(*pin.relative_path.split("/"))

    @classmethod
    def _ensure_parent(cls, root: Path, relative_path: str) -> tuple[Path, int]:
        relative = cls._relative_path(relative_path)
        current = root
        for component in relative.parts[:-1]:
            candidate = current / component
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                try:
                    candidate.mkdir(mode=0o700)
                    cls._fsync_directory(current)
                    metadata = candidate.lstat()
                except OSError as exc:
                    raise InputMaterializationError(
                        "input destination parent could not be created"
                    ) from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise InputMaterializationError("input destination parent is unsafe")
            current = candidate
        try:
            descriptor = os.open(
                current,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise InputMaterializationError("input destination parent became unsafe") from exc
        return current, descriptor

    def _materialize_entry(self, root: Path, source_entry: _ResolvedInput) -> None:
        target = root / self._relative_path(source_entry.relative_path)
        if target.exists() or target.is_symlink():
            self._verify_file(
                target,
                expected_sha256=source_entry.content_sha256,
                expected_bytes=source_entry.content_bytes,
            )
            return
        source, source_parent = self._open_cas_object(source_entry.content_sha256)
        try:
            parent, parent_descriptor = self._ensure_parent(root, source_entry.relative_path)
        except (InputMaterializationError, OSError):
            os.close(source)
            os.close(source_parent)
            raise
        temporary = f".aletheia-input-{source_entry.content_sha256}.{secrets.token_hex(16)}.tmp"
        destination: int | None = None
        try:
            source_before = self._stat_identity(os.fstat(source))
            source_metadata = os.fstat(source)
            if (
                not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_nlink != 1
                or stat.S_IMODE(source_metadata.st_mode) != 0o400
            ):
                raise InputMaterializationError("CAS source is not one immutable regular object")
            destination = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(source, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > source_entry.content_bytes:
                    raise InputMaterializationError("CAS source exceeded its verified byte bound")
                digest.update(chunk)
                self._write_all(destination, chunk)
            if (
                digest.hexdigest() != source_entry.content_sha256
                or size != source_entry.content_bytes
                or self._stat_identity(os.fstat(source)) != source_before
            ):
                raise InputMaterializationError("CAS source changed while it was streamed")
            os.fsync(destination)
            os.fchmod(destination, 0o400)
            os.fsync(destination)
            os.close(destination)
            destination = None
            try:
                os.link(
                    temporary,
                    target.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise InputMaterializationError(
                "input CAS copy could not be atomically published"
            ) from exc
        finally:
            if destination is not None:
                os.close(destination)
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)
            os.close(source)
            os.close(source_parent)
        self._verify_file(
            parent / target.name,
            expected_sha256=source_entry.content_sha256,
            expected_bytes=source_entry.content_bytes,
        )

    def _open_cas_object(self, digest: str) -> tuple[int, int]:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            current = os.open(self._artifact_store.root, directory_flags)
            descriptors.append(current)
            for component in ("objects", "sha256", digest[:2]):
                current = os.open(component, directory_flags, dir_fd=current)
                descriptors.append(current)
            source = os.open(
                digest,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
        except OSError as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise InputMaterializationError("verified CAS object is missing or unsafe") from exc
        for descriptor in descriptors[:-1]:
            os.close(descriptor)
        return source, descriptors[-1]

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:  # pragma: no cover - regular-file writes progress or raise
                raise InputMaterializationError("input materialization write made no progress")
            offset += written

    @staticmethod
    def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    @classmethod
    def _verify_file(
        cls,
        path: Path,
        *,
        expected_sha256: str,
        expected_bytes: int,
        allowed_link_counts: tuple[int, ...] = (1,),
    ) -> os.stat_result:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise InputMaterializationError("materialized input file is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink not in allowed_link_counts
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
            ):
                raise InputMaterializationError(
                    "materialized input has unsafe owner, links, type, or mode"
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_bytes:
                    raise InputMaterializationError(
                        "materialized input exceeded its receipt byte bound"
                    )
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                cls._stat_identity(before) != cls._stat_identity(after)
                or digest.hexdigest() != expected_sha256
                or size != expected_bytes
            ):
                raise InputMaterializationError("materialized input differs from its exact receipt")
            return after
        finally:
            os.close(descriptor)

    @classmethod
    def _file_identity(
        cls,
        *,
        root: Path,
        relative_path: str,
        content_sha256: str,
        content_bytes: int,
    ) -> str:
        metadata = cls._verify_file(
            root / cls._relative_path(relative_path),
            expected_sha256=content_sha256,
            expected_bytes=content_bytes,
        )
        return canonical_sha256(
            {
                "schema": "aletheia.local_staged_input_file_identity.v2",
                "relative_path": relative_path,
                "content_sha256": content_sha256,
                "content_bytes": content_bytes,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "link_count": metadata.st_nlink,
                "modified_ns": metadata.st_mtime_ns,
                "changed_ns": metadata.st_ctime_ns,
            }
        )

    @classmethod
    def _entry_for_file(cls, root: Path, source: _ResolvedInput) -> InputMaterializationEntry:
        return InputMaterializationEntry(
            input_port_id=source.input_port_id,
            verified_receipt_sha256=source.verified_receipt_sha256,
            content_sha256=source.content_sha256,
            content_bytes=source.content_bytes,
            relative_path=source.relative_path,
            staged_file_identity_sha256=cls._file_identity(
                root=root,
                relative_path=source.relative_path,
                content_sha256=source.content_sha256,
                content_bytes=source.content_bytes,
            ),
        )

    @classmethod
    def _root_identity(
        cls,
        root: Path,
        entries: tuple[InputMaterializationEntry, ...],
    ) -> str:
        metadata = root.lstat()
        return canonical_sha256(
            {
                "schema": "aletheia.local_staged_input_root_identity.v2",
                "resolved_path_sha256": hashlib.sha256(os.fsencode(root)).hexdigest(),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "link_count": metadata.st_nlink,
                "modified_ns": metadata.st_mtime_ns,
                "changed_ns": metadata.st_ctime_ns,
                "entries": tuple(
                    {
                        "input_port_id": item.input_port_id,
                        "relative_path": item.relative_path,
                        "staged_file_identity_sha256": item.staged_file_identity_sha256,
                    }
                    for item in entries
                ),
            }
        )

    @classmethod
    def _scan_tree(cls, root: Path) -> tuple[dict[str, os.stat_result], dict[str, os.stat_result]]:
        files: dict[str, os.stat_result] = {}
        directories: dict[str, os.stat_result] = {}
        for current_root, directory_names, file_names in os.walk(root, topdown=True):
            current = Path(current_root)
            for name in tuple(directory_names):
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise InputMaterializationError(
                        "input tree contains a symlink or non-directory"
                    )
                directories[relative] = metadata
            for name in file_names:
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if not stat.S_ISREG(metadata.st_mode):
                    raise InputMaterializationError(
                        "input tree contains a link or non-regular file"
                    )
                files[relative] = metadata
        return files, directories

    @classmethod
    def _expected_directories(cls, paths: tuple[str, ...]) -> set[str]:
        expected: set[str] = set()
        for path in paths:
            parts = cls._relative_path(path).parts[:-1]
            for index in range(1, len(parts) + 1):
                expected.add("/".join(parts[:index]))
        return expected

    @classmethod
    def _recover_interrupted_copies(
        cls,
        root: Path,
        sources: tuple[_ResolvedInput, ...],
    ) -> None:
        """Remove only exact adapter temps left by a crash around hardlink publication."""

        expected = {
            (cls._relative_path(item.relative_path).parent.as_posix(), item.content_sha256): item
            for item in sources
        }
        pattern = re.compile(r"^\.aletheia-input-([0-9a-f]{64})\.([0-9a-f]{32})\.tmp$")
        for current_root, _, file_names in os.walk(root, topdown=True):
            parent = Path(current_root)
            relative_parent = parent.relative_to(root).as_posix()
            for name in file_names:
                if not name.startswith(".aletheia-input-"):
                    continue
                match = pattern.fullmatch(name)
                source = expected.get(
                    (relative_parent, match.group(1) if match is not None else "")
                )
                temporary = parent / name
                try:
                    metadata = temporary.lstat()
                except OSError as exc:
                    raise InputMaterializationError(
                        "interrupted input temp custody could not be inspected"
                    ) from exc
                if (
                    source is None
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink not in {1, 2}
                    or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
                ):
                    raise InputMaterializationError(
                        "input destination contains an unsafe interrupted temp"
                    )
                target = root / cls._relative_path(source.relative_path)
                if metadata.st_nlink == 2:
                    try:
                        target_metadata = target.lstat()
                    except OSError as exc:
                        raise InputMaterializationError(
                            "interrupted input hardlink lost its exact target"
                        ) from exc
                    if (
                        cls._stat_identity(metadata) != cls._stat_identity(target_metadata)
                        or stat.S_IMODE(metadata.st_mode) != 0o400
                    ):
                        raise InputMaterializationError(
                            "interrupted input hardlink differs from its target"
                        )
                    cls._verify_file(
                        temporary,
                        expected_sha256=source.content_sha256,
                        expected_bytes=source.content_bytes,
                        allowed_link_counts=(2,),
                    )
                try:
                    temporary.unlink()
                    cls._fsync_directory(parent)
                except OSError as exc:
                    raise InputMaterializationError(
                        "interrupted input temp could not be durably removed"
                    ) from exc

    @classmethod
    def _validate_partial_tree(cls, root: Path, sources: tuple[_ResolvedInput, ...]) -> None:
        files, directories = cls._scan_tree(root)
        expected_files = {item.relative_path: item for item in sources}
        expected_directories = cls._expected_directories(tuple(expected_files))
        if set(files) - set(expected_files) or set(directories) - expected_directories:
            raise InputMaterializationError("input destination contains undeclared custody paths")
        for relative_path, source in expected_files.items():
            if relative_path in files:
                cls._verify_file(
                    root / cls._relative_path(relative_path),
                    expected_sha256=source.content_sha256,
                    expected_bytes=source.content_bytes,
                )

    @classmethod
    def _seal_tree(cls, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise InputMaterializationError("input root became unsafe before sealing")
        for current_root, directory_names, file_names in os.walk(root, topdown=False):
            current = Path(current_root)
            for name in file_names:
                path = current / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise InputMaterializationError("input tree contains an unsafe file")
                os.chmod(path, 0o400, follow_symlinks=False)
            for name in directory_names:
                path = current / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise InputMaterializationError("input tree contains an unsafe directory")
                os.chmod(path, 0o500, follow_symlinks=False)
                cls._fsync_directory(path)
        os.chmod(root, 0o500, follow_symlinks=False)
        cls._fsync_directory(root)

    @classmethod
    def _validate_exact_tree(
        cls,
        root: Path,
        receipt: InputMaterializationReceipt,
    ) -> None:
        files, directories = cls._scan_tree(root)
        expected_files = {item.relative_path: item for item in receipt.entries}
        expected_directories = cls._expected_directories(tuple(expected_files))
        if set(files) != set(expected_files) or set(directories) != expected_directories:
            raise InputMaterializationError("sealed input tree differs from its exact receipt")
        root_metadata = root.lstat()
        if root_metadata.st_uid != os.geteuid() or stat.S_IMODE(root_metadata.st_mode) != 0o500:
            raise InputMaterializationError("sealed input root is not owner-owned mode 0500")
        for metadata in directories.values():
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o500:
                raise InputMaterializationError(
                    "sealed input directory is not owner-owned mode 0500"
                )
        observed_entries = tuple(
            InputMaterializationEntry(
                **entry.model_dump(
                    mode="python",
                    exclude={"staged_file_identity_sha256"},
                ),
                staged_file_identity_sha256=cls._file_identity(
                    root=root,
                    relative_path=entry.relative_path,
                    content_sha256=entry.content_sha256,
                    content_bytes=entry.content_bytes,
                ),
            )
            for entry in receipt.entries
        )
        if observed_entries != receipt.entries or (
            cls._root_identity(root, observed_entries) != receipt.staged_root_identity_sha256
        ):
            raise InputMaterializationError(
                "staged input identities changed after receipt issuance"
            )

    def _validate_receipt_scope(
        self,
        receipt: InputMaterializationReceipt,
        *,
        intent: ExecutionIntent,
        sources: tuple[_ResolvedInput, ...],
    ) -> None:
        expected = tuple(
            (
                item.input_port_id,
                item.relative_path,
                item.verified_receipt_sha256,
                item.content_sha256,
                item.content_bytes,
            )
            for item in sources
        )
        observed = tuple(
            (
                item.input_port_id,
                item.relative_path,
                item.verified_receipt_sha256,
                item.content_sha256,
                item.content_bytes,
            )
            for item in receipt.entries
        )
        if (
            receipt.intent_sha256 != intent.intent_sha256
            or receipt.execution_id != intent.execution_id
            or receipt.infrastructure_attempt_id
            != intent.infrastructure_attempt.infrastructure_attempt_id
            or receipt.materializer_principal_id != self._principal_id
            or observed != expected
        ):
            raise InputMaterializationError(
                "stored input receipt differs from its exact intent, custody, or path pins"
            )

    def _publish_receipt(
        self,
        path: Path,
        receipt: InputMaterializationReceipt,
    ) -> None:
        payload = canonical_json_bytes(receipt)
        temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
        parent_descriptor = os.open(
            self._journal_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                observed = self._read_receipt(path)
                if observed != receipt:
                    raise InputMaterializationError(
                        "materialization receipt name is already bound to other bytes"
                    )
            os.unlink(temporary, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise InputMaterializationError(
                "materialization receipt could not be published"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)

    def _recover_interrupted_receipt_publish(self, receipt_path: Path) -> None:
        """Recover bounded pre-link/post-link receipt temps under the attempt lock."""

        pattern = re.compile(rf"^\.{re.escape(receipt_path.name)}\.([0-9a-f]{{32}})\.tmp$")
        try:
            candidates = tuple(
                path
                for path in self._journal_root.iterdir()
                if path.name.startswith(f".{receipt_path.name}.")
            )
        except OSError as exc:
            raise InputMaterializationError(
                "materialization receipt journal could not be scanned"
            ) from exc
        for temporary in candidates:
            if pattern.fullmatch(temporary.name) is None:
                raise InputMaterializationError(
                    "materialization journal contains an unsafe receipt temp"
                )
            try:
                metadata = temporary.lstat()
            except OSError as exc:
                raise InputMaterializationError(
                    "interrupted receipt temp custody could not be inspected"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink not in {1, 2}
                or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
                or metadata.st_size > _MAX_RECEIPT_BYTES
            ):
                raise InputMaterializationError(
                    "interrupted receipt temp has unsafe custody metadata"
                )
            if metadata.st_nlink == 2:
                try:
                    receipt_metadata = receipt_path.lstat()
                except OSError as exc:
                    raise InputMaterializationError(
                        "interrupted receipt hardlink lost its final name"
                    ) from exc
                if (
                    self._stat_identity(metadata) != self._stat_identity(receipt_metadata)
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    raise InputMaterializationError(
                        "interrupted receipt hardlink differs from its final name"
                    )
            try:
                temporary.unlink()
                self._fsync_directory(self._journal_root)
            except OSError as exc:
                raise InputMaterializationError(
                    "interrupted receipt temp could not be durably removed"
                ) from exc

    @staticmethod
    def _read_receipt(path: Path) -> InputMaterializationReceipt:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise InputMaterializationError("input materialization receipt is unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size > _MAX_RECEIPT_BYTES
            ):
                raise InputMaterializationError("input receipt custody metadata is unsafe")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(descriptor)
            if LocalCASInputMaterializer._stat_identity(before) != (
                LocalCASInputMaterializer._stat_identity(after)
            ):
                raise InputMaterializationError("input receipt changed while it was read")
        finally:
            os.close(descriptor)
        try:
            receipt = InputMaterializationReceipt.model_validate_json(bytes(payload))
        except ValidationError as exc:
            raise InputMaterializationError("input materialization receipt is invalid") from exc
        if canonical_json_bytes(receipt) != bytes(payload):
            raise InputMaterializationError("input materialization receipt is not canonical")
        return receipt

    def _utc_now(self) -> datetime:
        observed = self._clock.now()
        if observed.tzinfo is None or observed.utcoffset() != timezone.utc.utcoffset(observed):
            raise InputMaterializationError("materializer clock must return timezone-aware UTC")
        return observed

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "InputMaterializationError",
    "InputMaterializerClock",
    "LocalCASInputMaterializer",
    "SystemInputMaterializerClock",
]
