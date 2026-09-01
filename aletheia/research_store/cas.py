"""Write-once filesystem CAS for research-kernel objects and snapshots.

The database stores only the metadata returned by this adapter.  Object bytes are canonical JSON
owned here; a failed database transaction may therefore leave an unreachable immutable object,
but it cannot create an event that points at bytes which were never durably staged.
"""

from __future__ import annotations

import hashlib
import fcntl
import os
import secrets
import stat
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from aletheia.research_kernel.schemas import (
    KernelObject,
    KernelObjectRef,
    canonical_json_bytes,
)
from aletheia.research_store.store import (
    ArchivedKernelObject,
    ArchivedObjectMetadata,
    ArchivedSnapshotMetadata,
)

_OBJECT_ADAPTER = TypeAdapter(KernelObject)


class ResearchArchiveError(RuntimeError):
    """The research archive could not safely stage or read an object."""


class ResearchArchiveCorruption(ResearchArchiveError):
    """Archived bytes or filesystem custody no longer match their content identity."""


class FilesystemResearchArchive:
    """Bounded, write-once CAS using ``sha256/<prefix>/<digest>`` keys."""

    def __init__(
        self,
        root: Path,
        *,
        max_object_bytes: int = 64 * 1024 * 1024,
        read_only: bool = False,
        directory_mode: int = 0o700,
        object_mode: int = 0o400,
    ) -> None:
        if max_object_bytes < 1 or max_object_bytes > 1024 * 1024 * 1024:
            raise ValueError("research archive limit must be between 1 byte and 1 GiB")
        if (directory_mode, object_mode) not in {(0o700, 0o400), (0o750, 0o440)}:
            raise ValueError(
                "research archive writable custody must be private or owner-write/group-read"
            )
        candidate = Path(root)
        if candidate.is_symlink():
            raise ResearchArchiveError("research archive root cannot be a symlink")
        if read_only:
            if not candidate.exists():
                raise ResearchArchiveError("read-only research archive root must already exist")
        else:
            candidate.mkdir(parents=True, exist_ok=True, mode=directory_mode)
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ResearchArchiveError("research archive root must be a regular directory")
        root_mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_mode & 0o022:
            raise ResearchArchiveError("research archive root cannot be group/world writable")
        if read_only:
            if os.geteuid() == 0:
                raise ResearchArchiveError(
                    "read-only research archive cannot run under a privileged process"
                )
            if not any(root_mode & mask == mask for mask in (0o500, 0o050, 0o005)):
                raise ResearchArchiveError("read-only research archive root is not traversable")
            if metadata.st_uid == os.geteuid():
                effective_mode = (root_mode >> 6) & 0o7
            elif metadata.st_gid == os.getegid():
                effective_mode = (root_mode >> 3) & 0o7
            else:
                effective_mode = root_mode & 0o7
            if effective_mode & 0o5 != 0o5 or effective_mode & 0o2:
                raise ResearchArchiveError(
                    "read-only research archive is writable or inaccessible to this process"
                )
            directory_mode = root_mode
            object_mode = root_mode & 0o444
        if not read_only and (
            root_mode != directory_mode
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
        ):
            raise ResearchArchiveError(
                "writable research archive root differs from its process-owned custody"
            )
        self.root = candidate.resolve(strict=True)
        self.max_object_bytes = max_object_bytes
        self.read_only = read_only
        self.directory_mode = directory_mode
        self.object_mode = object_mode
        self._root_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            root_mode,
        )

    @staticmethod
    def _storage_key(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest}"

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:  # pragma: no cover - requires a concurrent root replacement
            raise ResearchArchiveError("research archive root became unsafe") from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)  # pragma: no cover - guarded by O_DIRECTORY
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
            )
            != self._root_identity
        ):
            os.close(descriptor)
            raise ResearchArchiveError("research archive root custody changed")
        return descriptor

    def _open_parent(self, digest: str, *, create: bool) -> int:
        descriptor = self._open_root()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in ("sha256", digest[:2]):
                created = False
                if create:
                    try:
                        os.mkdir(component, mode=self.directory_mode, dir_fd=descriptor)
                        os.fsync(descriptor)
                        created = True
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    if created:
                        os.fchmod(child, self.directory_mode)
                        os.fsync(child)
                    child_metadata = os.fstat(child)
                    child_mode = stat.S_IMODE(child_metadata.st_mode)
                    if (
                        not stat.S_ISDIR(child_metadata.st_mode)  # pragma: no cover - O_DIRECTORY
                        or child_metadata.st_uid != self._root_identity[2]
                        or child_metadata.st_gid != self._root_identity[3]
                        or child_metadata.st_mode & 0o022
                        or child_mode != self.directory_mode
                    ):
                        error = ResearchArchiveError if create else ResearchArchiveCorruption
                        raise error("research archive parent custody is unsafe")
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except ResearchArchiveError:
            os.close(descriptor)
            raise
        except (FileNotFoundError, OSError) as exc:
            os.close(descriptor)
            error = ResearchArchiveError if create else ResearchArchiveCorruption
            raise error("research archive path is missing or unsafe") from exc

    def _read_exact(self, *, digest: str, expected_bytes: int | None = None) -> bytes:
        parent = self._open_parent(digest, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                descriptor = os.open(digest, flags, dir_fd=parent)
            except (FileNotFoundError, OSError) as exc:
                raise ResearchArchiveCorruption("archived object is missing or unsafe") from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ResearchArchiveCorruption("archived object is not a regular file")
                object_mode = stat.S_IMODE(metadata.st_mode)
                if (
                    metadata.st_nlink != 1
                    or metadata.st_uid != self._root_identity[2]
                    or metadata.st_gid != self._root_identity[3]
                    or object_mode & 0o222
                    or not object_mode & 0o444
                    or object_mode != self.object_mode
                ):
                    raise ResearchArchiveCorruption("archived object custody is not immutable")
                if metadata.st_size < 1 or metadata.st_size > self.max_object_bytes:
                    raise ResearchArchiveCorruption("archived object size is outside bounds")
                if expected_bytes is not None and metadata.st_size != expected_bytes:
                    raise ResearchArchiveCorruption("archived object byte count changed")
                chunks: list[bytes] = []
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise ResearchArchiveCorruption("archived object ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):  # pragma: no cover - fstat size bounds the loop
                    raise ResearchArchiveCorruption("archived object grew while being read")
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ResearchArchiveCorruption("archived object hash changed")
        return payload

    @contextmanager
    def _publication_lock(self, parent: int, digest: str) -> Iterator[None]:
        lock_name = f".{digest}.lock"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                descriptor = os.open(
                    lock_name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                descriptor = os.open(lock_name, flags, dir_fd=parent)
        except OSError as exc:
            raise ResearchArchiveError("research archive publication lock is unsafe") from exc
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != self._root_identity[2]
                or metadata.st_gid != self._root_identity[3]
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ResearchArchiveError("research archive publication lock is not private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _write_once(self, payload: bytes) -> str:
        if self.read_only:
            raise ResearchArchiveError("read-only research archive cannot stage new bytes")
        if not payload or len(payload) > self.max_object_bytes:
            raise ResearchArchiveError("research object is empty or exceeds the archive limit")
        digest = hashlib.sha256(payload).hexdigest()
        parent = self._open_parent(digest, create=True)
        try:
            with self._publication_lock(parent, digest):
                existing_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    existing = os.open(digest, existing_flags, dir_fd=parent)
                except FileNotFoundError:
                    existing = None
                except OSError as exc:
                    raise ResearchArchiveCorruption(
                        "archived object publication target is unsafe"
                    ) from exc
                if existing is not None:
                    os.close(existing)
                    self._read_exact(digest=digest, expected_bytes=len(payload))
                    return self._storage_key(digest)

                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                staging_name = f".{digest}.{secrets.token_hex(16)}.tmp"
                try:
                    descriptor = os.open(staging_name, flags, 0o600, dir_fd=parent)
                except OSError as exc:
                    raise ResearchArchiveError("research archive refused a staging object") from exc
                staged = False
                try:
                    view = memoryview(payload)
                    offset = 0
                    while offset < len(payload):
                        written = os.write(descriptor, view[offset:])
                        if written <= 0:  # pragma: no cover - regular file writes make progress
                            raise ResearchArchiveError("research archive write made no progress")
                        offset += written
                    os.fchmod(descriptor, self.object_mode)
                    os.fsync(descriptor)
                    staged = True
                finally:
                    os.close(descriptor)
                    if not staged:
                        try:
                            os.unlink(staging_name, dir_fd=parent)
                        except FileNotFoundError:
                            pass
                try:
                    os.rename(
                        staging_name,
                        digest,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                    )
                except OSError as exc:
                    raise ResearchArchiveError(
                        "research archive could not publish staged bytes"
                    ) from exc
                finally:
                    try:
                        os.unlink(staging_name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
                os.fsync(parent)
        finally:
            os.close(parent)
        self._read_exact(digest=digest, expected_bytes=len(payload))
        return self._storage_key(digest)

    def archive_object(self, payload: KernelObject) -> ArchivedObjectMetadata:
        """Validate and durably stage one typed kernel object before command commit."""

        try:
            validated = _OBJECT_ADAPTER.validate_python(payload.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise ResearchArchiveError("research object is not a valid kernel object") from exc
        object_bytes = canonical_json_bytes(validated)
        digest = hashlib.sha256(object_bytes).hexdigest()
        if digest != validated.object_sha256:
            raise ResearchArchiveError("research object identity is not its canonical content hash")
        storage_key = self._write_once(object_bytes)
        return ArchivedObjectMetadata(
            object_ref=validated.object_ref,
            object_version=int(getattr(validated, "version", 1)),
            object_schema_name=validated.schema_name,
            object_schema_version=validated.schema_version,
            object_size_bytes=len(object_bytes),
            storage_key=storage_key,
        )

    def load_object(self, ref: KernelObjectRef) -> ArchivedKernelObject:
        """Load, parse, and rehash the exact object named by ``ref``."""

        ref = KernelObjectRef.model_validate(ref.model_dump(mode="python"))
        object_bytes = self._read_exact(digest=ref.object_sha256)
        try:
            payload = _OBJECT_ADAPTER.validate_json(object_bytes)
        except ValidationError as exc:
            raise ResearchArchiveCorruption("archived object is not a typed kernel object") from exc
        if payload.object_ref != ref:
            raise ResearchArchiveCorruption(
                "archived object does not match the requested reference"
            )
        metadata = ArchivedObjectMetadata(
            object_ref=ref,
            object_version=int(getattr(payload, "version", 1)),
            object_schema_name=payload.schema_name,
            object_schema_version=payload.schema_version,
            object_size_bytes=len(object_bytes),
            storage_key=self._storage_key(ref.object_sha256),
        )
        return ArchivedKernelObject(metadata=metadata, payload=payload)

    def archive_snapshot(
        self,
        *,
        quest_id: str,
        stream_version: int,
        snapshot_sha256: str,
        payload: bytes,
    ) -> ArchivedSnapshotMetadata:
        """Stage already-canonical reducer output and bind its declared identity."""

        observed = hashlib.sha256(payload).hexdigest()
        if observed != snapshot_sha256:
            raise ResearchArchiveError("snapshot bytes do not match the declared content hash")
        storage_key = self._write_once(payload)
        return ArchivedSnapshotMetadata(
            quest_id=quest_id,
            stream_version=stream_version,
            snapshot_sha256=snapshot_sha256,
            snapshot_size_bytes=len(payload),
            storage_key=storage_key,
        )

    def load_snapshot(self, metadata: ArchivedSnapshotMetadata) -> bytes:
        """Read and rehash snapshot bytes from immutable metadata."""

        metadata = ArchivedSnapshotMetadata.model_validate(metadata.model_dump(mode="python"))
        return self._read_exact(
            digest=metadata.snapshot_sha256,
            expected_bytes=metadata.snapshot_size_bytes,
        )


__all__ = [
    "FilesystemResearchArchive",
    "ResearchArchiveCorruption",
    "ResearchArchiveError",
]
