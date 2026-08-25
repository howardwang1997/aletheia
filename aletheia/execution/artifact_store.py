"""Local quarantine and write-once artifact custody for replay-safe execution.

This adapter is deliberately narrower than a node agent.  A trusted caller first stops the
workload and then gives this class one output directory plus an explicit artifact-key/path map.
The adapter snapshots those files into an opaque quarantine, independently rehashes them into a
write-once content-addressed store, and emits :class:`ArtifactVerifiedReceipt` values.

It does not launch processes, persist database state, accept provider receipts, resume
checkpoints, or authorize scientific admission.  In particular, paths never cross the public
contract: an ``ArtifactManifestEntry.quarantine_ref`` is an opaque, content-bound identifier.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactVerifiedReceipt,
    ExecutionEffectClass,
    ExecutionIntent,
    ExpectedArtifact,
    canonical_json_bytes,
    canonical_sha256,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE_ID = re.compile(r"^qtn_[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024


class ArtifactStoreError(RuntimeError):
    """Base error for local artifact custody."""


class ArtifactQuarantineError(ArtifactStoreError):
    """The workload output could not be snapshotted safely."""


class ArtifactVerificationError(ArtifactStoreError):
    """A manifest or quarantine object failed independent verification."""


class ArtifactStoreCorruption(ArtifactStoreError):
    """Previously published custody bytes no longer match their immutable identity."""


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _write_all(descriptor: int, payload: memoryview) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - regular-file writes progress or raise
            raise ArtifactStoreError("artifact custody write made no progress")
        offset += written


class LocalArtifactStore:
    """Filesystem quarantine/CAS with central rehash and write-once receipt sidecars.

    The filesystem is trusted against the isolated workload, not against a malicious host root.
    Every read nevertheless uses ``O_NOFOLLOW``, checks regular-file/link metadata, and rehashes
    the exact bytes named by the receipt.
    """

    def __init__(
        self,
        root: Path,
        *,
        verifier_principal_id: str = "execution-artifact-verifier",
        object_store_id: str = "local-artifact-cas",
        max_object_bytes: int = 64 * 1024**3,
    ) -> None:
        if max_object_bytes < 1 or max_object_bytes > 1024**4:
            raise ValueError("artifact object limit must be between 1 byte and 1 TiB")
        if not verifier_principal_id or not object_store_id:
            raise ValueError("artifact verifier and object-store identities must be nonempty")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ArtifactStoreError("artifact store root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ArtifactStoreError("artifact store root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        self.verifier_principal_id = verifier_principal_id
        self.object_store_id = object_store_id
        self.max_object_bytes = max_object_bytes
        root_descriptor = self._open_root()
        try:
            for components in (
                ("quarantine", "staging"),
                ("quarantine", "objects"),
                ("objects", "sha256"),
                ("manifests", "sha256"),
                ("verification",),
                ("receipts", "sha256"),
            ):
                child = self._open_directory_from(
                    root_descriptor, components=components, create=True
                )
                os.close(child)
        finally:
            os.close(root_descriptor)

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:  # pragma: no cover - requires concurrent root replacement
            raise ArtifactStoreError("artifact store root became unsafe") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):  # pragma: no cover - guarded by O_DIRECTORY
            os.close(descriptor)
            raise ArtifactStoreError("artifact store root is not a directory")
        if metadata.st_mode & 0o022:
            os.close(descriptor)
            raise ArtifactStoreError("artifact store root cannot be group/world writable")
        return descriptor

    @staticmethod
    def _open_directory_from(
        root_descriptor: int,
        *,
        components: tuple[str, ...],
        create: bool,
    ) -> int:
        descriptor = os.dup(root_descriptor)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in components:
                if not component or component in {".", ".."} or "/" in component:
                    raise ArtifactStoreError("artifact store path component is invalid")
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise ArtifactStoreError(
                        "artifact store directory is missing or unsafe"
                    ) from exc
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022:
                    os.close(child)
                    raise ArtifactStoreError("artifact store directory is unsafe")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_directory(self, *components: str, create: bool) -> int:
        root_descriptor = self._open_root()
        try:
            return self._open_directory_from(
                root_descriptor,
                components=tuple(components),
                create=create,
            )
        finally:
            os.close(root_descriptor)

    @staticmethod
    def _validate_regular(
        metadata: os.stat_result,
        *,
        label: str,
        require_immutable_mode: bool = False,
    ) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactStoreError(f"{label} is not a regular file")
        if metadata.st_nlink != 1:
            raise ArtifactStoreError(f"{label} must not be hard-linked")
        if require_immutable_mode and stat.S_IMODE(metadata.st_mode) != 0o400:
            raise ArtifactStoreError(f"{label} must have immutable 0400 mode")

    @staticmethod
    def _open_relative_file(root_descriptor: int, relative_path: str) -> int:
        components = tuple(relative_path.split("/"))
        descriptor = os.dup(root_descriptor)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in components[:-1]:
                child = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return os.open(
                components[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise ArtifactQuarantineError("workload output path is missing or unsafe") from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
            raise ArtifactQuarantineError("workload output path must be a canonical relative path")
        components = value.split("/")
        if any(component in {"", ".", ".."} for component in components):
            raise ArtifactQuarantineError("workload output path must not escape its root")
        return "/".join(components)

    def _scan_outputs(
        self, root_descriptor: int
    ) -> tuple[dict[str, tuple[int, int, int, int, int, int, int]], set[str]]:
        files: dict[str, tuple[int, int, int, int, int, int, int]] = {}
        directories: set[str] = set()

        def walk(directory_descriptor: int, prefix: str) -> None:
            try:
                names = tuple(sorted(os.listdir(directory_descriptor)))
            except OSError as exc:
                raise ArtifactQuarantineError(
                    "workload output directory cannot be scanned"
                ) from exc
            for name in names:
                if not name or name in {".", ".."} or "/" in name:
                    raise ArtifactQuarantineError("workload output contains an invalid name")
                relative = f"{prefix}/{name}" if prefix else name
                try:
                    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise ArtifactQuarantineError("workload output changed during scan") from exc
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(relative)
                    flags = (
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        child = os.open(name, flags, dir_fd=directory_descriptor)
                    except OSError as exc:
                        raise ArtifactQuarantineError(
                            "workload output directory is unsafe"
                        ) from exc
                    try:
                        walk(child, relative)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        raise ArtifactQuarantineError("workload output must not be hard-linked")
                    files[relative] = _stat_identity(metadata)
                else:
                    raise ArtifactQuarantineError(
                        "workload output contains a symlink or non-regular object"
                    )

        walk(root_descriptor, "")
        return files, directories

    @staticmethod
    def _expected_parent_directories(paths: tuple[str, ...]) -> set[str]:
        parents: set[str] = set()
        for path in paths:
            components = path.split("/")
            for index in range(1, len(components)):
                parents.add("/".join(components[:index]))
        return parents

    def _stream_descriptor(
        self,
        source_descriptor: int,
        *,
        sink_descriptor: int | None,
        byte_limit: int,
        expected_identity: tuple[int, int, int, int, int, int, int] | None = None,
        require_immutable_mode: bool = False,
        error_type: type[ArtifactStoreError] = ArtifactStoreCorruption,
        label: str,
    ) -> tuple[str, int]:
        try:
            before = os.fstat(source_descriptor)
            try:
                self._validate_regular(
                    before,
                    label=label,
                    require_immutable_mode=require_immutable_mode,
                )
            except ArtifactStoreError as exc:
                raise error_type(str(exc)) from exc
            if expected_identity is not None and _stat_identity(before) != expected_identity:
                raise error_type(f"{label} changed between scan and open")
            if before.st_size > byte_limit or before.st_size > self.max_object_bytes:
                raise error_type(f"{label} exceeds its frozen byte limit")
            digest = hashlib.sha256()
            observed = 0
            while True:
                chunk = os.read(source_descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > byte_limit or observed > self.max_object_bytes:
                    raise error_type(f"{label} grew beyond its frozen byte limit")
                digest.update(chunk)
                if sink_descriptor is not None:
                    _write_all(sink_descriptor, memoryview(chunk))
            after = os.fstat(source_descriptor)
            if _stat_identity(after) != _stat_identity(before) or observed != before.st_size:
                raise error_type(f"{label} changed while it was read")
            return digest.hexdigest(), observed
        except error_type:
            raise
        except (ArtifactStoreError, OSError) as exc:
            raise error_type(f"{label} could not be streamed safely") from exc

    @staticmethod
    def _quarantine_id(
        *, intent: ExecutionIntent, requirement: ExpectedArtifact, digest: str, size: int
    ) -> str:
        identity = canonical_sha256(
            {
                "schema": "aletheia.local_quarantine_object_identity.v1",
                "intent_sha256": intent.intent_sha256,
                "infrastructure_attempt_id": (
                    intent.infrastructure_attempt.infrastructure_attempt_id
                ),
                "expected_artifact_id": requirement.expected_artifact_id,
                "artifact_key": requirement.artifact_key,
                "content_sha256": digest,
                "bytes": size,
            }
        )
        return f"qtn_{identity}"

    def _read_named_blob(
        self,
        *,
        parent_components: tuple[str, ...],
        name: str,
        byte_limit: int,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
        error_type: type[ArtifactStoreError] = ArtifactStoreCorruption,
        optional: bool = False,
    ) -> bytes | None:
        try:
            parent = self._open_directory(*parent_components, create=False)
        except ArtifactStoreError:
            if optional:
                return None
            raise
        try:
            fcntl.flock(parent, fcntl.LOCK_SH)
        except OSError as exc:
            os.close(parent)
            raise error_type("artifact custody directory could not be locked") from exc
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                if optional:
                    return None
                raise error_type("artifact custody object is missing") from None
            except OSError as exc:
                raise error_type("artifact custody object is missing or unsafe") from exc
            try:
                metadata = os.fstat(descriptor)
                try:
                    self._validate_regular(
                        metadata,
                        label="artifact custody object",
                        require_immutable_mode=True,
                    )
                except ArtifactStoreError as exc:
                    raise error_type(str(exc)) from exc
                if metadata.st_size > byte_limit:
                    raise error_type("artifact custody object exceeds its byte limit")
                chunks: list[bytes] = []
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise error_type("artifact custody object ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise error_type("artifact custody object grew while being read")
                after = os.fstat(descriptor)
                if _stat_identity(after) != _stat_identity(metadata):
                    raise error_type("artifact custody object changed while being read")
            finally:
                os.close(descriptor)
        finally:
            fcntl.flock(parent, fcntl.LOCK_UN)
            os.close(parent)
        payload = b"".join(chunks)
        if expected_bytes is not None and len(payload) != expected_bytes:
            raise error_type("artifact custody object byte count changed")
        if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise error_type("artifact custody object hash changed")
        return payload

    def _publish_staged(
        self,
        *,
        staging_parent: int,
        staging_name: str,
        target_components: tuple[str, ...],
        target_name: str,
        expected_sha256: str,
        expected_bytes: int,
        error_type: type[ArtifactStoreError],
    ) -> None:
        try:
            target_parent = self._open_directory(*target_components, create=True)
        except ArtifactStoreError as exc:
            raise error_type("artifact custody target directory is unsafe") from exc
        try:
            fcntl.flock(target_parent, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(target_parent)
            raise error_type("artifact custody target directory could not be locked") from exc
        target_existed = False
        try:
            try:
                os.link(
                    staging_name,
                    target_name,
                    src_dir_fd=staging_parent,
                    dst_dir_fd=target_parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                target_existed = True
            except OSError as exc:
                raise error_type("artifact custody could not publish staged bytes") from exc
            if not target_existed:
                try:
                    os.unlink(staging_name, dir_fd=staging_parent)
                except OSError as exc:
                    raise error_type("artifact custody could not finalize staged bytes") from exc
                os.fsync(staging_parent)
            os.fsync(target_parent)
        finally:
            fcntl.flock(target_parent, fcntl.LOCK_UN)
            os.close(target_parent)
        self._verify_named_stream(
            parent_components=target_components,
            name=target_name,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
            error_type=error_type,
            label="published quarantine object",
        )

    def _verify_named_stream(
        self,
        *,
        parent_components: tuple[str, ...],
        name: str,
        expected_sha256: str,
        expected_bytes: int,
        error_type: type[ArtifactStoreError],
        label: str,
    ) -> None:
        try:
            parent = self._open_directory(*parent_components, create=False)
        except ArtifactStoreError as exc:
            raise error_type(f"{label} is missing or unsafe") from exc
        try:
            fcntl.flock(parent, fcntl.LOCK_SH)
        except OSError as exc:
            os.close(parent)
            raise error_type(f"{label} directory could not be locked") from exc
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except OSError as exc:
                raise error_type(f"{label} is missing or unsafe") from exc
            try:
                observed_sha256, observed_bytes = self._stream_descriptor(
                    descriptor,
                    sink_descriptor=None,
                    byte_limit=expected_bytes,
                    require_immutable_mode=True,
                    error_type=error_type,
                    label=label,
                )
            finally:
                os.close(descriptor)
        finally:
            fcntl.flock(parent, fcntl.LOCK_UN)
            os.close(parent)
        if observed_sha256 != expected_sha256 or observed_bytes != expected_bytes:
            raise error_type(f"{label} hash or byte count changed")

    def _stage_source(
        self,
        *,
        output_root_descriptor: int,
        relative_path: str,
        scanned_identity: tuple[int, int, int, int, int, int, int],
        intent: ExecutionIntent,
        requirement: ExpectedArtifact,
    ) -> ArtifactManifestEntry:
        source = self._open_relative_file(output_root_descriptor, relative_path)
        try:
            staging_parent = self._open_directory("quarantine", "staging", create=False)
        except ArtifactStoreError as exc:
            os.close(source)
            raise ArtifactQuarantineError("quarantine staging directory is unsafe") from exc
        temporary_name = f".{secrets.token_hex(24)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                destination = os.open(temporary_name, flags, 0o600, dir_fd=staging_parent)
            except OSError as exc:
                os.close(source)
                raise ArtifactQuarantineError("quarantine refused a staging object") from exc
            committed = False
            try:
                digest, size = self._stream_descriptor(
                    source,
                    sink_descriptor=destination,
                    byte_limit=requirement.max_bytes,
                    expected_identity=scanned_identity,
                    error_type=ArtifactQuarantineError,
                    label="workload output",
                )
                # A descriptor pins the opened inode, which is necessary but insufficient for
                # pathname custody: a hostile process could rename that inode away and replace
                # the declared path while it is being read. Reopen the path and require it still
                # names the exact scanned object before publishing the snapshot.
                reopened = self._open_relative_file(output_root_descriptor, relative_path)
                try:
                    if _stat_identity(os.fstat(reopened)) != scanned_identity:
                        raise ArtifactQuarantineError(
                            "workload output path changed while it was read"
                        )
                finally:
                    os.close(reopened)
                os.fchmod(destination, 0o400)
                os.fsync(destination)
                committed = True
            finally:
                os.close(destination)
                os.close(source)
                if not committed:
                    try:
                        os.unlink(temporary_name, dir_fd=staging_parent)
                    except FileNotFoundError:
                        pass
            quarantine_id = self._quarantine_id(
                intent=intent,
                requirement=requirement,
                digest=digest,
                size=size,
            )
            quarantine_digest = quarantine_id.removeprefix("qtn_")
            try:
                self._publish_staged(
                    staging_parent=staging_parent,
                    staging_name=temporary_name,
                    target_components=("quarantine", "objects", quarantine_digest[:2]),
                    target_name=quarantine_id,
                    expected_sha256=digest,
                    expected_bytes=size,
                    error_type=ArtifactQuarantineError,
                )
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=staging_parent)
                    os.fsync(staging_parent)
                except FileNotFoundError:
                    pass
        finally:
            os.close(staging_parent)
        return ArtifactManifestEntry(
            expected_artifact_id=requirement.expected_artifact_id,
            artifact_key=requirement.artifact_key,
            role=requirement.role,
            content_sha256=digest,
            bytes=size,
            media_type=requirement.media_type,
            schema_sha256=requirement.schema_sha256,
            quarantine_ref=quarantine_id,
        )

    def quarantine_outputs(
        self,
        *,
        intent: ExecutionIntent,
        output_root: Path,
        artifact_paths: Mapping[str, str],
        produced_at: datetime,
        allow_partial: bool = False,
    ) -> ArtifactManifest:
        """Snapshot an exact, explicitly mapped output tree into opaque quarantine objects.

        The normal path requires every mandatory artifact. A terminal failure collector may set
        ``allow_partial=True`` to retain the exact subset that exists; this flag never weakens
        declaration, filesystem-safety, or quota checks.
        """

        try:
            intent = ExecutionIntent.model_validate(
                intent.model_dump(mode="python", warnings="none")
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise ArtifactQuarantineError(
                "execution intent failed closed-model revalidation"
            ) from exc
        if intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE:
            raise ArtifactQuarantineError(
                "local artifact quarantine supports replay-safe execution only"
            )
        candidate = Path(output_root)
        if candidate.is_symlink():
            raise ArtifactQuarantineError("workload output root cannot be a symlink")
        try:
            resolved_output = candidate.resolve(strict=True)
        except OSError as exc:
            raise ArtifactQuarantineError("workload output root is missing") from exc
        if not resolved_output.is_dir():
            raise ArtifactQuarantineError("workload output root must be a directory")
        if (
            resolved_output == self.root
            or resolved_output in self.root.parents
            or self.root in resolved_output.parents
        ):
            raise ArtifactQuarantineError(
                "workload output and artifact-store roots must not overlap"
            )

        expectations = {item.artifact_key: item for item in intent.expected_artifacts}
        normalized: dict[str, str] = {}
        for artifact_key, relative_path in artifact_paths.items():
            if artifact_key not in expectations:
                raise ArtifactQuarantineError("workload output declares an unknown artifact key")
            normalized[artifact_key] = self._normalize_relative_path(relative_path)
        if len(set(normalized.values())) != len(normalized):
            raise ArtifactQuarantineError("multiple artifact keys cannot name the same output path")
        if not allow_partial and any(
            requirement.required and artifact_key not in normalized
            for artifact_key, requirement in expectations.items()
        ):
            raise ArtifactQuarantineError("workload output is missing a required artifact")

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_descriptor = os.open(resolved_output, flags)
        except OSError as exc:
            raise ArtifactQuarantineError("workload output root became unsafe") from exc
        try:
            files, directories = self._scan_outputs(root_descriptor)
            declared_paths = tuple(sorted(normalized.values()))
            if set(files) != set(
                declared_paths
            ) or directories != self._expected_parent_directories(declared_paths):
                raise ArtifactQuarantineError(
                    "workload output tree contains missing, undeclared, or empty paths"
                )
            preflight_total = sum(files[path][4] for path in declared_paths)
            if preflight_total > intent.resource_request.artifact_quota_bytes:
                raise ArtifactQuarantineError("workload output exceeds aggregate artifact quota")
            for artifact_key, relative_path in normalized.items():
                if files[relative_path][4] > expectations[artifact_key].max_bytes:
                    raise ArtifactQuarantineError("workload output exceeds per-artifact quota")

            entries = tuple(
                sorted(
                    (
                        self._stage_source(
                            output_root_descriptor=root_descriptor,
                            relative_path=relative_path,
                            scanned_identity=files[relative_path],
                            intent=intent,
                            requirement=expectations[artifact_key],
                        )
                        for artifact_key, relative_path in normalized.items()
                    ),
                    key=lambda item: item.artifact_key,
                )
            )
        finally:
            os.close(root_descriptor)
        if sum(item.bytes for item in entries) > intent.resource_request.artifact_quota_bytes:
            raise ArtifactQuarantineError("workload output grew beyond aggregate artifact quota")
        return ArtifactManifest(
            intent_sha256=intent.intent_sha256,
            execution_id=intent.execution_id,
            replicate_slot_id=intent.replicate_slot.replicate_slot_id,
            infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
            entries=entries,
            produced_at=produced_at,
        )

    def _open_quarantine(self, quarantine_id: str) -> int:
        if _QUARANTINE_ID.fullmatch(quarantine_id) is None:
            raise ArtifactVerificationError("manifest quarantine reference is not an opaque qid")
        digest = quarantine_id.removeprefix("qtn_")
        try:
            parent = self._open_directory("quarantine", "objects", digest[:2], create=False)
        except ArtifactStoreError as exc:
            raise ArtifactVerificationError("quarantine object is missing or unsafe") from exc
        try:
            try:
                return os.open(
                    quarantine_id,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except OSError as exc:
                raise ArtifactVerificationError("quarantine object is missing or unsafe") from exc
        finally:
            os.close(parent)

    def _verify_final_object(self, *, digest: str, size: int) -> None:
        self._verify_named_stream(
            parent_components=("objects", "sha256", digest[:2]),
            name=digest,
            expected_sha256=digest,
            expected_bytes=size,
            error_type=ArtifactStoreCorruption,
            label="final CAS object",
        )

    def _promote_quarantine(self, entry: ArtifactManifestEntry) -> None:
        quarantine = self._open_quarantine(entry.quarantine_ref)
        try:
            target_parent = self._open_directory(
                "objects", "sha256", entry.content_sha256[:2], create=True
            )
        except ArtifactStoreError as exc:
            os.close(quarantine)
            raise ArtifactVerificationError("artifact CAS directory is unsafe") from exc
        try:
            fcntl.flock(target_parent, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(quarantine)
            os.close(target_parent)
            raise ArtifactVerificationError("artifact CAS directory could not be locked") from exc
        temporary_name = f".{entry.content_sha256}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                destination = os.open(temporary_name, flags, 0o600, dir_fd=target_parent)
            except OSError as exc:
                os.close(quarantine)
                raise ArtifactVerificationError("artifact CAS refused a staging object") from exc
            committed = False
            try:
                digest, size = self._stream_descriptor(
                    quarantine,
                    sink_descriptor=destination,
                    byte_limit=entry.bytes,
                    require_immutable_mode=True,
                    error_type=ArtifactVerificationError,
                    label="quarantine object",
                )
                if digest != entry.content_sha256 or size != entry.bytes:
                    raise ArtifactVerificationError(
                        "central rehash differs from the artifact manifest"
                    )
                os.fchmod(destination, 0o400)
                os.fsync(destination)
                committed = True
            finally:
                os.close(destination)
                os.close(quarantine)
                if not committed:
                    try:
                        os.unlink(temporary_name, dir_fd=target_parent)
                    except FileNotFoundError:
                        pass
            try:
                try:
                    os.link(
                        temporary_name,
                        entry.content_sha256,
                        src_dir_fd=target_parent,
                        dst_dir_fd=target_parent,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # A digest collision, prior partial publication, or tampering is checked by
                    # the mandatory namespace reopen below, after the publication lock is free.
                    pass
                except OSError as exc:
                    raise ArtifactVerificationError(
                        "artifact CAS could not conditionally publish staged bytes"
                    ) from exc
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=target_parent)
                except FileNotFoundError:
                    pass
            os.fsync(target_parent)
        finally:
            fcntl.flock(target_parent, fcntl.LOCK_UN)
            os.close(target_parent)
        # Reopen the final namespace even after a successful link; the receipt never attests only
        # to the temporary bytes or to a filesystem operation's return code.
        self._verify_final_object(digest=entry.content_sha256, size=entry.bytes)

    def _publish_bytes_or_read_winner(
        self,
        *,
        parent_components: tuple[str, ...],
        name: str,
        payload: bytes,
    ) -> bytes:
        parent = self._open_directory(*parent_components, create=True)
        try:
            fcntl.flock(parent, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(parent)
            raise ArtifactStoreError("artifact receipt directory could not be locked") from exc
        temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
            committed = False
            try:
                _write_all(descriptor, memoryview(payload))
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                committed = True
            finally:
                os.close(descriptor)
                if not committed:
                    try:
                        os.unlink(temporary_name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            won = False
            try:
                try:
                    os.link(
                        temporary_name,
                        name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                    won = True
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ArtifactStoreError("artifact receipt could not be published") from exc
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.fsync(parent)
        finally:
            fcntl.flock(parent, fcntl.LOCK_UN)
            os.close(parent)
        if won:
            return payload
        existing = self._read_named_blob(
            parent_components=parent_components,
            name=name,
            byte_limit=_MAX_RECEIPT_BYTES,
        )
        assert existing is not None
        return existing

    def _persist_verification_receipt(
        self,
        *,
        manifest: ArtifactManifest,
        entry: ArtifactManifestEntry,
    ) -> ArtifactVerifiedReceipt:
        candidate = ArtifactVerifiedReceipt(
            artifact_manifest_sha256=manifest.manifest_sha256,
            producer_attempt_id=manifest.infrastructure_attempt_id,
            artifact=entry,
            custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
            verifier_principal_id=self.verifier_principal_id,
            object_store_id=self.object_store_id,
            final_object_ref=f"cas://sha256/{entry.content_sha256}",
            final_object_version=f"sha256:{entry.content_sha256}",
            verified_at=datetime.now(timezone.utc),
        )
        candidate_bytes = canonical_json_bytes(candidate)
        candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
        # Store the content-addressed sidecar before competing for the deterministic verification
        # key.  A losing concurrent candidate is an unreachable immutable orphan, never authority.
        stored_candidate = self._publish_bytes_or_read_winner(
            parent_components=("receipts", "sha256", candidate_hash[:2]),
            name=f"{candidate_hash}.json",
            payload=candidate_bytes,
        )
        if stored_candidate != candidate_bytes:
            raise ArtifactStoreCorruption("receipt content-addressed identity collided")

        verification_key = canonical_sha256(
            {
                "schema": "aletheia.local_artifact_verification_key.v1",
                "artifact_manifest_sha256": manifest.manifest_sha256,
                "manifest_entry_sha256": entry.manifest_entry_sha256,
                "custody_mode": ArtifactCustodyMode.CENTRAL_REHASH.value,
                "verifier_principal_id": self.verifier_principal_id,
                "object_store_id": self.object_store_id,
            }
        )
        winner_bytes = self._publish_bytes_or_read_winner(
            parent_components=("verification", verification_key[:2]),
            name=f"{verification_key}.json",
            payload=candidate_bytes,
        )
        try:
            winner = ArtifactVerifiedReceipt.model_validate_json(winner_bytes)
        except ValidationError as exc:
            raise ArtifactStoreCorruption(
                "stored artifact verification receipt is invalid"
            ) from exc
        if (
            winner.artifact_manifest_sha256 != manifest.manifest_sha256
            or winner.producer_attempt_id != manifest.infrastructure_attempt_id
            or winner.artifact != entry
            or winner.custody_mode is not ArtifactCustodyMode.CENTRAL_REHASH
            or winner.verifier_principal_id != self.verifier_principal_id
            or winner.object_store_id != self.object_store_id
            or winner.final_object_ref != f"cas://sha256/{entry.content_sha256}"
            or winner.final_object_version != f"sha256:{entry.content_sha256}"
        ):
            raise ArtifactStoreCorruption("verification key was rebound to different custody")
        winner_hash = winner.verified_receipt_sha256
        stored_winner = self._publish_bytes_or_read_winner(
            parent_components=("receipts", "sha256", winner_hash[:2]),
            name=f"{winner_hash}.json",
            payload=winner_bytes,
        )
        if stored_winner != winner_bytes:
            raise ArtifactStoreCorruption("winning receipt identity was rebound")
        return winner

    def _persist_manifest(self, manifest: ArtifactManifest) -> None:
        """Publish one canonical manifest sidecar under its content identity."""

        payload = canonical_json_bytes(manifest)
        manifest_sha256 = manifest.manifest_sha256
        stored = self._publish_bytes_or_read_winner(
            parent_components=("manifests", "sha256", manifest_sha256[:2]),
            name=f"{manifest_sha256}.json",
            payload=payload,
        )
        if stored != payload:
            raise ArtifactStoreCorruption("manifest identity was rebound to different bytes")

    @staticmethod
    def _manifest_matches_intent(intent: ExecutionIntent, manifest: ArtifactManifest) -> bool:
        return (
            manifest.intent_sha256 == intent.intent_sha256
            and manifest.execution_id == intent.execution_id
            and manifest.replicate_slot_id == intent.replicate_slot.replicate_slot_id
            and manifest.infrastructure_attempt_id
            == intent.infrastructure_attempt.infrastructure_attempt_id
        )

    def verify_manifest(
        self,
        *,
        intent: ExecutionIntent,
        manifest: ArtifactManifest,
    ) -> tuple[ArtifactVerifiedReceipt, ...]:
        """Centrally rehash every quarantined entry and promote it to write-once CAS."""

        try:
            intent = ExecutionIntent.model_validate(
                intent.model_dump(mode="python", warnings="none")
            )
            manifest = ArtifactManifest.model_validate(
                manifest.model_dump(mode="python", warnings="none")
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise ArtifactVerificationError(
                "artifact contracts failed closed-model revalidation"
            ) from exc
        if intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE:
            raise ArtifactVerificationError(
                "local central verification supports replay-safe execution only"
            )
        if not self._manifest_matches_intent(intent, manifest):
            raise ArtifactVerificationError("artifact manifest belongs to another execution")
        expectations = {item.artifact_key: item for item in intent.expected_artifacts}
        if set(item.artifact_key for item in manifest.entries) - expectations.keys():
            raise ArtifactVerificationError("artifact manifest contains an undeclared output")
        if (
            sum(item.bytes for item in manifest.entries)
            > intent.resource_request.artifact_quota_bytes
        ):
            raise ArtifactVerificationError("artifact manifest exceeds aggregate artifact quota")

        # Validate the complete declaration before publishing any authoritative sidecar.
        for entry in manifest.entries:
            requirement = expectations[entry.artifact_key]
            if (
                entry.expected_artifact_id != requirement.expected_artifact_id
                or entry.role is not requirement.role
                or entry.media_type != requirement.media_type
                or entry.schema_sha256 != requirement.schema_sha256
                or entry.bytes > requirement.max_bytes
            ):
                raise ArtifactVerificationError("artifact entry violates its frozen expectation")
            expected_qid = self._quarantine_id(
                intent=intent,
                requirement=requirement,
                digest=entry.content_sha256,
                size=entry.bytes,
            )
            if entry.quarantine_ref != expected_qid:
                raise ArtifactVerificationError(
                    "manifest quarantine reference is not bound to this artifact"
                )

        # CAS publication may leave only content-addressed orphan bytes on interruption.  The
        # canonical manifest sidecar is published after every entry has been independently
        # reopened and rehashed, and before any receipt can name it.
        for entry in manifest.entries:
            self._promote_quarantine(entry)

        self._persist_manifest(manifest)
        receipts: list[ArtifactVerifiedReceipt] = []
        for entry in manifest.entries:
            receipts.append(self._persist_verification_receipt(manifest=manifest, entry=entry))
        return tuple(receipts)

    def load_manifest(self, *, manifest_sha256: str) -> ArtifactManifest | None:
        """Reload a canonical manifest sidecar and freshly rehash every named CAS object."""

        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ValueError("artifact manifest identity must be a lowercase SHA-256 digest")
        payload = self._read_named_blob(
            parent_components=("manifests", "sha256", manifest_sha256[:2]),
            name=f"{manifest_sha256}.json",
            byte_limit=_MAX_RECEIPT_BYTES,
            optional=True,
        )
        if payload is None:
            return None
        try:
            manifest = ArtifactManifest.model_validate_json(payload)
        except ValidationError as exc:
            raise ArtifactStoreCorruption("stored artifact manifest is invalid") from exc
        if manifest.manifest_sha256 != manifest_sha256 or canonical_json_bytes(manifest) != payload:
            raise ArtifactStoreCorruption("stored artifact manifest identity changed")
        for entry in manifest.entries:
            if _QUARANTINE_ID.fullmatch(entry.quarantine_ref) is None:
                raise ArtifactStoreCorruption("stored manifest contains an invalid custody ref")
            self._verify_final_object(digest=entry.content_sha256, size=entry.bytes)
        return manifest

    def load_verified_receipt(
        self,
        *,
        verified_receipt_sha256: str,
    ) -> ArtifactVerifiedReceipt | None:
        """Load a receipt sidecar and reopen/rehash its exact final CAS object."""

        if _SHA256.fullmatch(verified_receipt_sha256) is None:
            raise ValueError("verified receipt identity must be a lowercase SHA-256 digest")
        payload = self._read_named_blob(
            parent_components=("receipts", "sha256", verified_receipt_sha256[:2]),
            name=f"{verified_receipt_sha256}.json",
            byte_limit=_MAX_RECEIPT_BYTES,
            optional=True,
        )
        if payload is None:
            return None
        try:
            receipt = ArtifactVerifiedReceipt.model_validate_json(payload)
        except ValidationError as exc:
            raise ArtifactStoreCorruption("stored artifact receipt is invalid") from exc
        if (
            receipt.verified_receipt_sha256 != verified_receipt_sha256
            or canonical_json_bytes(receipt) != payload
        ):
            raise ArtifactStoreCorruption("stored artifact receipt hash changed")
        digest = receipt.artifact.content_sha256
        if (
            receipt.custody_mode is not ArtifactCustodyMode.CENTRAL_REHASH
            or receipt.verifier_principal_id != self.verifier_principal_id
            or receipt.object_store_id != self.object_store_id
            or receipt.final_object_ref != f"cas://sha256/{digest}"
            or receipt.final_object_version != f"sha256:{digest}"
        ):
            raise ArtifactStoreCorruption("stored receipt does not describe this local CAS")
        self._verify_final_object(digest=digest, size=receipt.artifact.bytes)
        return receipt

    def resolve_verified_receipt(
        self,
        *,
        verified_receipt_sha256: str,
    ) -> ArtifactVerifiedReceipt | None:
        """Compatibility alias for a receipt-only lookup, including final-CAS rehash.

        This deliberately is not the richer input-artifact resolver: a local artifact store has
        no authority to establish producer ``ExecutionReceipt`` lineage.
        """

        return self.load_verified_receipt(verified_receipt_sha256=verified_receipt_sha256)


__all__ = [
    "ArtifactQuarantineError",
    "ArtifactStoreCorruption",
    "ArtifactStoreError",
    "ArtifactVerificationError",
    "LocalArtifactStore",
]
