"""Content-addressed, non-live snapshots of mutable legacy research records."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_SHA1_PATTERN = r"^[0-9a-f]{40}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
_SNAPSHOT_ID_PATTERN = r"^lgs_[0-9a-f]{32}$"
_RECEIPT_ID_PATTERN = r"^lgi_[0-9a-f]{32}$"
_IMPORT_KEY_PATTERN = r"^lgk_[0-9a-f]{32}$"
_FORBIDDEN_BASENAMES = {
    ".env",
    ".envrc",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
_FORBIDDEN_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
_FORBIDDEN_BYTE_MARKERS = (
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyObjectRole(str, Enum):
    EVENT_LOG = "event_log"
    ARTIFACT = "artifact"
    REPORT = "report"
    MANIFEST = "manifest"
    DATASET_SLICE = "dataset_slice"
    GOLDEN_CONTRACT = "golden_contract"


class LegacyDataClass(str, Enum):
    DEV_FIXTURE = "dev_fixture"
    PUBLIC = "public"
    INTERNAL_SANITIZED = "internal_sanitized"


class LegacyDataRole(str, Enum):
    DEV_FIXTURE = "dev_fixture"
    COMPATIBILITY_ONLY = "compatibility_only"
    COLD_ARCHIVE = "cold_archive"


def legacy_exporter_code_sha256(
    *,
    commit: str,
    tree: str,
    entrypoint: str,
    entrypoint_sha256: str,
) -> str:
    """Derive a Git-and-entrypoint code binding for a declared legacy exporter."""

    return content_sha256(
        {
            "schema_name": "aletheia.legacy_exporter_git_entrypoint_identity",
            "schema_version": 1,
            "commit": commit,
            "tree": tree,
            "entrypoint": _validated_relative_path(entrypoint),
            "entrypoint_sha256": entrypoint_sha256,
        }
    )


def _validated_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("legacy snapshot paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("legacy snapshot path must be a normalized relative path")
    return path.as_posix()


class LegacySnapshotInput(_FrozenModel):
    logical_name: str = Field(pattern=_IDENTITY_PATTERN)
    source_relative_path: str
    role: LegacyObjectRole
    media_type: str = Field(min_length=1, max_length=128)
    data_class: LegacyDataClass

    @field_validator("source_relative_path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        return _validated_relative_path(value)


class LegacyFreezerSourceFile(_FrozenModel):
    """One runtime source file hashed immediately before a freeze."""

    relative_path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        return _validated_relative_path(value)


class LegacyFreezerIdentity(_FrozenModel):
    """Identity of the freezer source bytes present immediately before a freeze."""

    schema_name: Literal["aletheia.legacy_freezer_runtime_identity"] = (
        "aletheia.legacy_freezer_runtime_identity"
    )
    schema_version: Literal[1] = 1
    identity_scheme: Literal["runtime_source_bundle_sha256_v1"] = "runtime_source_bundle_sha256_v1"
    entrypoint: str
    source_files: tuple[LegacyFreezerSourceFile, ...] = Field(min_length=1)
    execution_assurance: Literal["runtime_source_bytes_hashed_before_freeze"] = (
        "runtime_source_bytes_hashed_before_freeze"
    )
    code_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("entrypoint")
    @classmethod
    def _safe_entrypoint(cls, value: str) -> str:
        return _validated_relative_path(value)

    @model_validator(mode="after")
    def _canonical_and_content_addressed(self) -> "LegacyFreezerIdentity":
        canonical = tuple(sorted(self.source_files, key=lambda item: item.relative_path))
        if canonical != self.source_files:
            raise ValueError("legacy freezer source files must be in canonical order")
        paths = {item.relative_path for item in canonical}
        if len(paths) != len(canonical):
            raise ValueError("legacy freezer source file paths must be unique")
        if self.entrypoint not in paths:
            raise ValueError("legacy freezer entrypoint must be one of its source files")
        expected = content_sha256(self.model_dump(mode="json", exclude={"code_sha256"}))
        if self.code_sha256 is not None and self.code_sha256 != expected:
            raise ValueError("legacy freezer code hash does not match its source bundle")
        object.__setattr__(self, "code_sha256", expected)
        return self


def _read_stable_code_file(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError(f"legacy freezer source cannot be a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"legacy freezer source must be a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_ino,
            before.st_dev,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_ino,
            after.st_dev,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("legacy freezer source changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def build_legacy_freezer_identity(
    *,
    entrypoint: str,
    source_files: Iterable[tuple[str, Path]],
) -> LegacyFreezerIdentity:
    """Hash the freezer source files actually present immediately before a freeze."""

    items: list[LegacyFreezerSourceFile] = []
    for relative_path, source_path in source_files:
        normalized = _validated_relative_path(relative_path)
        payload = _read_stable_code_file(source_path)
        items.append(
            LegacyFreezerSourceFile(
                relative_path=normalized,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )
        )
    return LegacyFreezerIdentity(
        entrypoint=entrypoint,
        source_files=tuple(sorted(items, key=lambda item: item.relative_path)),
    )


class LegacyFreezeRequest(_FrozenModel):
    schema_name: Literal["aletheia.legacy_freeze_request"] = "aletheia.legacy_freeze_request"
    schema_version: Literal[1] = 1
    source_system: str = Field(pattern=_IDENTITY_PATTERN)
    source_scope: str = Field(pattern=_IDENTITY_PATTERN)
    source_version: str = Field(pattern=_IDENTITY_PATTERN)
    redaction_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_identity_scheme: Literal["git_tracked_entrypoint_v1"] = "git_tracked_entrypoint_v1"
    exporter_git_commit: str = Field(pattern=_GIT_SHA1_PATTERN)
    exporter_git_tree: str = Field(pattern=_GIT_SHA1_PATTERN)
    exporter_entrypoint: str
    exporter_entrypoint_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_execution_assurance: Literal["operator_attested"] = "operator_attested"
    objects: tuple[LegacySnapshotInput, ...] = Field(min_length=1, max_length=10_000)

    @field_validator("exporter_entrypoint")
    @classmethod
    def _safe_exporter_entrypoint(cls, value: str) -> str:
        return _validated_relative_path(value)

    @model_validator(mode="after")
    def _canonical_objects(self) -> "LegacyFreezeRequest":
        expected_exporter_sha = legacy_exporter_code_sha256(
            commit=self.exporter_git_commit,
            tree=self.exporter_git_tree,
            entrypoint=self.exporter_entrypoint,
            entrypoint_sha256=self.exporter_entrypoint_sha256,
        )
        if self.exporter_code_sha256 != expected_exporter_sha:
            raise ValueError("exporter code hash does not match its Git entrypoint binding")
        canonical = tuple(
            sorted(self.objects, key=lambda item: (item.logical_name, item.source_relative_path))
        )
        if canonical != self.objects:
            raise ValueError("legacy snapshot inputs must be in canonical order")
        if len({item.logical_name for item in canonical}) != len(canonical):
            raise ValueError("legacy snapshot logical names must be unique")
        if len({item.source_relative_path for item in canonical}) != len(canonical):
            raise ValueError("legacy snapshot source paths must be unique")
        return self


class LegacySnapshotObject(_FrozenModel):
    logical_name: str = Field(pattern=_IDENTITY_PATTERN)
    source_relative_path: str
    role: LegacyObjectRole
    media_type: str = Field(min_length=1, max_length=128)
    data_class: LegacyDataClass
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    cas_relative_uri: str

    @field_validator("source_relative_path", "cas_relative_uri")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        return _validated_relative_path(value)

    @model_validator(mode="after")
    def _cas_identity_matches(self) -> "LegacySnapshotObject":
        expected = f"objects/sha256/{self.sha256[:2]}/{self.sha256}"
        if self.cas_relative_uri != expected:
            raise ValueError("legacy object CAS URI does not match its digest")
        return self


class LegacySnapshotManifest(_FrozenModel):
    schema_name: Literal["aletheia.legacy_snapshot"] = "aletheia.legacy_snapshot"
    schema_version: Literal[1] = 1
    snapshot_id: str | None = Field(default=None, pattern=_SNAPSHOT_ID_PATTERN)
    source_system: str = Field(pattern=_IDENTITY_PATTERN)
    source_scope: str = Field(pattern=_IDENTITY_PATTERN)
    source_version: str = Field(pattern=_IDENTITY_PATTERN)
    redaction_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_identity_scheme: Literal["git_tracked_entrypoint_v1"] = "git_tracked_entrypoint_v1"
    exporter_git_commit: str = Field(pattern=_GIT_SHA1_PATTERN)
    exporter_git_tree: str = Field(pattern=_GIT_SHA1_PATTERN)
    exporter_entrypoint: str
    exporter_entrypoint_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    exporter_execution_assurance: Literal["operator_attested"] = "operator_attested"
    freezer_identity: LegacyFreezerIdentity
    objects: tuple[LegacySnapshotObject, ...] = Field(min_length=1, max_length=10_000)
    object_count: int | None = Field(default=None, ge=1)
    total_bytes: int | None = Field(default=None, ge=0)
    payload_authority: Literal["snapshot_cas"] = "snapshot_cas"
    live_refresh_allowed: Literal[False] = False
    legacy_mutation_propagates: Literal[False] = False
    snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("exporter_entrypoint")
    @classmethod
    def _safe_exporter_entrypoint(cls, value: str) -> str:
        return _validated_relative_path(value)

    @model_validator(mode="after")
    def _canonical_and_content_addressed(self) -> "LegacySnapshotManifest":
        expected_exporter_sha = legacy_exporter_code_sha256(
            commit=self.exporter_git_commit,
            tree=self.exporter_git_tree,
            entrypoint=self.exporter_entrypoint,
            entrypoint_sha256=self.exporter_entrypoint_sha256,
        )
        if self.exporter_code_sha256 != expected_exporter_sha:
            raise ValueError("exporter code hash does not match its Git entrypoint binding")
        canonical = tuple(
            sorted(self.objects, key=lambda item: (item.logical_name, item.source_relative_path))
        )
        if canonical != self.objects:
            raise ValueError("legacy snapshot objects must be in canonical order")
        if len({item.logical_name for item in canonical}) != len(canonical):
            raise ValueError("legacy snapshot logical names must be unique")
        if len({item.source_relative_path for item in canonical}) != len(canonical):
            raise ValueError("legacy snapshot source paths must be unique")
        count = len(canonical)
        size = sum(item.size_bytes for item in canonical)
        if self.object_count is not None and self.object_count != count:
            raise ValueError("legacy snapshot object count does not match its objects")
        if self.total_bytes is not None and self.total_bytes != size:
            raise ValueError("legacy snapshot byte count does not match its objects")
        object.__setattr__(self, "object_count", count)
        object.__setattr__(self, "total_bytes", size)
        payload = self.model_dump(
            mode="json",
            exclude={"snapshot_id", "snapshot_sha256"},
        )
        expected_sha = content_sha256(payload)
        expected_id = f"lgs_{expected_sha[:32]}"
        if self.snapshot_sha256 is not None and self.snapshot_sha256 != expected_sha:
            raise ValueError("legacy snapshot hash does not match its contents")
        if self.snapshot_id is not None and self.snapshot_id != expected_id:
            raise ValueError("legacy snapshot ID does not match its contents")
        object.__setattr__(self, "snapshot_sha256", expected_sha)
        object.__setattr__(self, "snapshot_id", expected_id)
        return self


class LegacyImportReceipt(_FrozenModel):
    """Unsigned snapshot-verification and target-scope intent record.

    The receipt is content addressed but does not itself mutate the target scope, persist an import,
    enforce uniqueness, or prove issuer identity.  Those authorities belong to the future event store.
    """

    schema_name: Literal["aletheia.legacy_import_receipt"] = "aletheia.legacy_import_receipt"
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=_RECEIPT_ID_PATTERN)
    import_key: str | None = Field(default=None, pattern=_IMPORT_KEY_PATTERN)
    snapshot_id: str = Field(pattern=_SNAPSHOT_ID_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_scope_id: str = Field(pattern=_IDENTITY_PATTERN)
    imported_by: str = Field(pattern=_IDENTITY_PATTERN)
    importer_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    imported_at: AwareDatetime
    object_count: int = Field(ge=1)
    total_bytes: int = Field(ge=0)
    data_role: LegacyDataRole = LegacyDataRole.COMPATIBILITY_ONLY
    claim_ceiling: Literal["engineering_regression_only"] = "engineering_regression_only"
    scientific_admission_allowed: Literal[False] = False
    training_use_allowed: Literal[False] = False
    verification_status: Literal["accepted"] = "accepted"
    import_mode: Literal["read_only_snapshot"] = "read_only_snapshot"
    payload_authority: Literal["snapshot_cas"] = "snapshot_cas"
    live_refresh_allowed: Literal[False] = False
    legacy_mutation_propagates: Literal[False] = False
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed(self) -> "LegacyImportReceipt":
        expected_snapshot_id = f"lgs_{self.snapshot_sha256[:32]}"
        if self.snapshot_id != expected_snapshot_id:
            raise ValueError("legacy import receipt snapshot ID does not match its hash")
        expected_import_key = (
            "lgk_"
            + content_sha256(
                {
                    "schema_name": self.schema_name,
                    "schema_version": self.schema_version,
                    "snapshot_sha256": self.snapshot_sha256,
                    "target_scope_id": self.target_scope_id,
                    "importer_code_sha256": self.importer_code_sha256,
                    "data_role": self.data_role,
                }
            )[:32]
        )
        if self.import_key is not None and self.import_key != expected_import_key:
            raise ValueError("legacy import idempotency key does not match its scope")
        object.__setattr__(self, "import_key", expected_import_key)
        expected_sha = content_sha256(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        expected_id = f"lgi_{expected_sha[:32]}"
        if self.receipt_sha256 is not None and self.receipt_sha256 != expected_sha:
            raise ValueError("legacy import receipt hash does not match its contents")
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("legacy import receipt ID does not match its contents")
        object.__setattr__(self, "receipt_sha256", expected_sha)
        object.__setattr__(self, "receipt_id", expected_id)
        return self


def _reject_obvious_secret(path: Path, payload: bytes) -> None:
    name = path.name.lower()
    if (
        name in _FORBIDDEN_BASENAMES
        or name.startswith(".env.")
        or path.suffix.lower() in _FORBIDDEN_SUFFIXES
    ):
        raise ValueError(f"legacy snapshot rejects credential-like path: {path.name}")
    if any(marker in payload for marker in _FORBIDDEN_BYTE_MARKERS):
        raise ValueError(f"legacy snapshot rejects private-key material: {path.name}")


def _read_stable_regular_file(path: Path, source_root: Path) -> bytes:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("legacy snapshot source escapes its root") from exc
    relative_parts = path.relative_to(source_root).parts
    cursor = source_root
    for part in relative_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("legacy snapshot sources cannot traverse symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("legacy snapshot source must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_ino,
            before.st_dev,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_ino,
            after.st_dev,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("legacy snapshot source changed while it was read")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    _reject_obvious_secret(path, payload)
    return payload


def _verify_exact_file(path: Path, payload: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"snapshot CAS target is not a regular file: {path}")
    existing = path.read_bytes()
    if existing != payload:
        raise RuntimeError(f"snapshot CAS collision or corruption at {path}")


def _safe_store_target(store: Path, relative_path: Path) -> Path:
    """Reject symlink traversal and targets outside the resolved snapshot-store root."""

    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise RuntimeError("snapshot store target must be a normalized relative path")
    target = store / relative_path
    cursor = store
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeError(f"snapshot store target traverses a symlink: {target}")
    try:
        target.parent.resolve(strict=False).relative_to(store)
        if target.exists():
            target.resolve(strict=True).relative_to(store)
    except ValueError as exc:
        raise RuntimeError(f"snapshot store target escapes its root: {target}") from exc
    return target


def _durable_put_new_or_verify(path: Path, payload: bytes, *, store_root: Path) -> None:
    relative_path = path.relative_to(store_root)
    _safe_store_target(store_root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_store_target(store_root, relative_path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - os.write progresses or raises
                raise OSError("legacy snapshot write made no progress")
            written += count
        os.fchmod(descriptor, 0o440)
        # Persist the final bytes and read-only mode together.  Calling fsync before fchmod would
        # leave the permission metadata outside the durability boundary.
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path)
        except FileExistsError:
            _verify_exact_file(path, payload)
        _safe_store_target(store_root, relative_path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def freeze_legacy_snapshot(
    request: LegacyFreezeRequest,
    *,
    source_root: Path,
    snapshot_store: Path,
    freezer_identity: LegacyFreezerIdentity | None = None,
    freezer_source_root: Path | None = None,
) -> LegacySnapshotManifest:
    """Copy explicit sanitized inputs into a content-addressed snapshot store."""

    if freezer_identity is None:
        if freezer_source_root is not None:
            raise ValueError("freezer_source_root requires an explicit freezer_identity")
        freezer_identity = build_legacy_freezer_identity(
            entrypoint="aletheia/migration/legacy.py",
            source_files=(("aletheia/migration/legacy.py", Path(__file__)),),
        )
    else:
        # A self-consistent identity model is still only a declaration.  Re-read every declared
        # source at the durable boundary so callers cannot claim nonexistent or different bytes.
        declared_identity = LegacyFreezerIdentity.model_validate(
            freezer_identity.model_dump(mode="json")
        )
        if freezer_source_root is None:
            raise ValueError("an explicit freezer_identity requires freezer_source_root")
        freezer_root = freezer_source_root.resolve(strict=True)
        source_files: list[tuple[str, Path]] = []
        for item in declared_identity.source_files:
            source_path = freezer_root.joinpath(*PurePosixPath(item.relative_path).parts)
            resolved = source_path.resolve(strict=True)
            try:
                resolved.relative_to(freezer_root)
            except ValueError as exc:
                raise ValueError("legacy freezer source escapes its root") from exc
            cursor = freezer_root
            for part in PurePosixPath(item.relative_path).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("legacy freezer sources cannot traverse symlinks")
            source_files.append((item.relative_path, source_path))
        actual_identity = build_legacy_freezer_identity(
            entrypoint=declared_identity.entrypoint,
            source_files=source_files,
        )
        if actual_identity != declared_identity:
            raise ValueError("legacy freezer identity does not match the current source bytes")
        freezer_identity = actual_identity
    source = source_root.resolve(strict=True)
    store = snapshot_store.resolve(strict=False)
    objects: list[LegacySnapshotObject] = []
    for item in request.objects:
        payload = _read_stable_regular_file(source / item.source_relative_path, source)
        digest = hashlib.sha256(payload).hexdigest()
        cas_uri = f"objects/sha256/{digest[:2]}/{digest}"
        cas_path = _safe_store_target(store, Path(cas_uri))
        _durable_put_new_or_verify(cas_path, payload, store_root=store)
        objects.append(
            LegacySnapshotObject(
                logical_name=item.logical_name,
                source_relative_path=item.source_relative_path,
                role=item.role,
                media_type=item.media_type,
                data_class=item.data_class,
                sha256=digest,
                size_bytes=len(payload),
                cas_relative_uri=cas_uri,
            )
        )
    manifest = LegacySnapshotManifest(
        source_system=request.source_system,
        source_scope=request.source_scope,
        source_version=request.source_version,
        redaction_manifest_sha256=request.redaction_manifest_sha256,
        exporter_identity_scheme=request.exporter_identity_scheme,
        exporter_git_commit=request.exporter_git_commit,
        exporter_git_tree=request.exporter_git_tree,
        exporter_entrypoint=request.exporter_entrypoint,
        exporter_entrypoint_sha256=request.exporter_entrypoint_sha256,
        exporter_code_sha256=request.exporter_code_sha256,
        exporter_execution_assurance=request.exporter_execution_assurance,
        freezer_identity=freezer_identity,
        objects=tuple(objects),
    )
    assert manifest.snapshot_sha256 is not None
    manifest_payload = canonical_json_bytes(manifest) + b"\n"
    _durable_put_new_or_verify(
        _safe_store_target(
            store,
            Path("manifests") / f"{manifest.snapshot_sha256}.json",
        ),
        manifest_payload,
        store_root=store,
    )
    binding_path, binding_payload = _snapshot_version_binding(manifest, store=store)
    _durable_put_new_or_verify(binding_path, binding_payload, store_root=store)
    verify_legacy_snapshot(manifest, snapshot_store=store)
    return manifest


def _snapshot_version_binding(
    manifest: LegacySnapshotManifest,
    *,
    store: Path,
) -> tuple[Path, bytes]:
    """Bind one declared legacy version to exactly one frozen snapshot."""

    source_identity = {
        "source_system": manifest.source_system,
        "source_scope": manifest.source_scope,
        "source_version": manifest.source_version,
    }
    binding_sha256 = content_sha256(source_identity)
    payload = (
        canonical_json_bytes(
            {
                "schema_name": "aletheia.legacy_snapshot_version_binding",
                "schema_version": 1,
                **source_identity,
                "snapshot_id": manifest.snapshot_id,
                "snapshot_sha256": manifest.snapshot_sha256,
            }
        )
        + b"\n"
    )
    return _safe_store_target(
        store,
        Path("bindings") / f"{binding_sha256}.json",
    ), payload


def verify_legacy_snapshot(
    manifest: LegacySnapshotManifest,
    *,
    snapshot_store: Path,
) -> None:
    """Rehash every copied object and the immutable manifest."""

    store = snapshot_store.resolve(strict=True)
    for item in manifest.objects:
        path = _safe_store_target(store, Path(item.cas_relative_uri))
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"legacy snapshot object is missing or unsafe: {item.logical_name}")
        payload = path.read_bytes()
        if len(payload) != item.size_bytes or hashlib.sha256(payload).hexdigest() != item.sha256:
            raise RuntimeError(f"legacy snapshot object failed verification: {item.logical_name}")
    assert manifest.snapshot_sha256 is not None
    manifest_path = _safe_store_target(
        store,
        Path("manifests") / f"{manifest.snapshot_sha256}.json",
    )
    _verify_exact_file(manifest_path, canonical_json_bytes(manifest) + b"\n")
    binding_path, binding_payload = _snapshot_version_binding(manifest, store=store)
    _verify_exact_file(binding_path, binding_payload)


def build_legacy_import_receipt(
    manifest: LegacySnapshotManifest,
    *,
    snapshot_store: Path,
    target_scope_id: str,
    imported_by: str,
    importer_code_sha256: str,
    imported_at: datetime,
    data_role: LegacyDataRole = LegacyDataRole.COMPATIBILITY_ONLY,
) -> LegacyImportReceipt:
    """Issue an unsigned verification/scope-intent record without mutating the target scope."""

    verify_legacy_snapshot(manifest, snapshot_store=snapshot_store)
    assert manifest.snapshot_id is not None
    assert manifest.snapshot_sha256 is not None
    assert manifest.object_count is not None
    assert manifest.total_bytes is not None
    return LegacyImportReceipt(
        snapshot_id=manifest.snapshot_id,
        snapshot_sha256=manifest.snapshot_sha256,
        target_scope_id=target_scope_id,
        imported_by=imported_by,
        importer_code_sha256=importer_code_sha256,
        imported_at=imported_at,
        object_count=manifest.object_count,
        total_bytes=manifest.total_bytes,
        data_role=data_role,
    )
