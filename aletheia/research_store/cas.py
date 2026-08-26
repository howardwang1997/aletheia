"""Write-once filesystem CAS for research-kernel objects and snapshots.

The database stores only the metadata returned by this adapter.  Object bytes are canonical JSON
owned here; a failed database transaction may therefore leave an unreachable immutable object,
but it cannot create an event that points at bytes which were never durably staged.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
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
    ) -> None:
        if max_object_bytes < 1 or max_object_bytes > 1024 * 1024 * 1024:
            raise ValueError("research archive limit must be between 1 byte and 1 GiB")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ResearchArchiveError("research archive root cannot be a symlink")
        if read_only:
            if not candidate.exists():
                raise ResearchArchiveError("read-only research archive root must already exist")
        else:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ResearchArchiveError("research archive root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        self.max_object_bytes = max_object_bytes
        self.read_only = read_only

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
        if not stat.S_ISDIR(metadata.st_mode):  # pragma: no cover - guarded by O_DIRECTORY
            os.close(descriptor)
            raise ResearchArchiveError("research archive root is not a directory")
        return descriptor

    def _open_parent(self, digest: str, *, create: bool) -> int:
        descriptor = self._open_root()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in ("sha256", digest[:2]):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                child_metadata = os.fstat(child)
                if not stat.S_ISDIR(child_metadata.st_mode):  # pragma: no cover - O_DIRECTORY
                    os.close(child)
                    raise ResearchArchiveError("research archive path is not a directory")
                os.close(descriptor)
                descriptor = child
            return descriptor
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

    def _write_once(self, payload: bytes) -> str:
        if self.read_only:
            raise ResearchArchiveError("read-only research archive cannot stage new bytes")
        if not payload or len(payload) > self.max_object_bytes:
            raise ResearchArchiveError("research object is empty or exceeds the archive limit")
        digest = hashlib.sha256(payload).hexdigest()
        parent = self._open_parent(digest, create=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        staging_name = f".{digest}.{secrets.token_hex(16)}.tmp"
        try:
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
                os.fchmod(descriptor, 0o400)
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
                os.link(
                    staging_name,
                    digest,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._read_exact(digest=digest, expected_bytes=len(payload))
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
