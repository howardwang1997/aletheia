"""Immutable content-addressed archive for F8-S2 provider responses and ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.schemas import KnowledgeModel
from aletheia.knowledge.search import PlannedSearchQuery, ProviderAdapterManifest
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_FORBIDDEN_TEXT_KEYS = {"abstract", "body", "full_text", "fulltext", "source_text", "summary"}
_JSON_MEDIA_TYPES = {"application/json", "application/vnd.api+json"}
_XML_MEDIA_TYPES = {"application/atom+xml", "application/xml", "text/xml"}


class ResponseArchiveError(RuntimeError):
    """The response archive could not prove an immutable, policy-safe object."""


class ResponsePolicyViolation(ResponseArchiveError):
    """Provider bytes exceed the frozen metadata-only access boundary."""


class ResponseArchiveCorruption(ResponseArchiveError):
    """Archived bytes no longer match their content identity."""


class ArchivedProviderResponse(KnowledgeModel):
    schema_version: Literal[1] = 1
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(
        pattern=r"^responses/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.response$"
    )
    source_id: str = Field(min_length=1, max_length=80)
    adapter_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_access_class: Literal["metadata_only"] = "metadata_only"
    policy_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _path_is_content_addressed(self) -> "ArchivedProviderResponse":
        expected = (
            f"responses/{self.response_sha256[:2]}/{self.response_sha256[2:4]}/"
            f"{self.response_sha256}.response"
        )
        if self.relative_path != expected:
            raise ValueError("response archive path does not match its content identity")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ArchivedSearchLedger(KnowledgeModel):
    schema_version: Literal[1] = 1
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    relative_path: str = Field(pattern=r"^ledgers/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.json$")
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived_at: AwareDatetime

    @model_validator(mode="after")
    def _path_is_content_addressed(self) -> "ArchivedSearchLedger":
        expected = (
            f"ledgers/{self.ledger_sha256[:2]}/{self.ledger_sha256[2:4]}/{self.ledger_sha256}.json"
        )
        if self.relative_path != expected:
            raise ValueError("search-ledger path does not match its content identity")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


# The underlying ledger namespace is deliberately object-neutral.  Keep the original F8-S2 name
# as a compatibility alias while later knowledge stages use the accurate generic name.
ArchivedKnowledgeLedger = ArchivedSearchLedger


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key).casefold())
            keys.update(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_walk_keys(item))
    return keys


def _metadata_policy_evidence(
    *, payload: bytes, media_type: str, manifest: ProviderAdapterManifest
) -> str:
    normalized_media_type = media_type.split(";", 1)[0].strip().casefold()
    allowed_media_types = {
        item.split(";", 1)[0].strip().casefold() for item in manifest.media_types
    }
    if normalized_media_type not in allowed_media_types:
        raise ResponsePolicyViolation("provider media type is outside the frozen adapter manifest")
    try:
        if normalized_media_type in _JSON_MEDIA_TYPES or normalized_media_type.endswith("+json"):
            decoded = json.loads(payload)
            forbidden = _walk_keys(decoded) & _FORBIDDEN_TEXT_KEYS
        elif normalized_media_type in _XML_MEDIA_TYPES or normalized_media_type.endswith("+xml"):
            root = ElementTree.fromstring(payload)
            keys = {element.tag.rsplit("}", 1)[-1].casefold() for element in root.iter()}
            forbidden = keys & _FORBIDDEN_TEXT_KEYS
        else:
            raise ResponsePolicyViolation(
                "metadata-only response archive accepts structured JSON or XML only"
            )
    except ResponsePolicyViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ElementTree.ParseError) as exc:
        raise ResponsePolicyViolation("provider response is not valid structured metadata") from exc
    if forbidden:
        raise ResponsePolicyViolation(
            "provider response contains forbidden text-bearing fields: "
            + ", ".join(sorted(forbidden))
        )
    return content_sha256(
        {
            "policy": "f8s2-metadata-only-structured-response-v1",
            "manifest_sha256": manifest.manifest_sha256,
            "media_type": normalized_media_type,
            "forbidden_fields_absent": sorted(_FORBIDDEN_TEXT_KEYS),
            "response_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )


class ContentAddressedResponseArchive:
    """Write-once response/ledger storage with bounded, rehashed reads."""

    def __init__(self, root: Path, *, max_object_bytes: int = 64 * 1024 * 1024) -> None:
        if max_object_bytes < 1 or max_object_bytes > 64 * 1024 * 1024:
            raise ValueError("response archive object limit must be between 1 byte and 64 MiB")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ResponseArchiveError("response archive root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ResponseArchiveError("response archive root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        self.max_object_bytes = max_object_bytes

    def _relative_path(self, namespace: Literal["responses", "ledgers"], digest: str) -> str:
        suffix = "response" if namespace == "responses" else "json"
        return f"{namespace}/{digest[:2]}/{digest[2:4]}/{digest}.{suffix}"

    def _path(self, relative_path: str) -> Path:
        parts = Path(relative_path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ResponseArchiveError("response archive path is not canonical")
        target = self.root.joinpath(*parts)
        if self.root not in target.parents:
            raise ResponseArchiveError("response archive path escapes its root")
        return target

    def _ensure_parent(self, target: Path) -> None:
        relative_parent = target.parent.relative_to(self.root)
        current = self.root
        for part in relative_parent.parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if current.is_symlink() or not current.is_dir():
                raise ResponseArchiveError("response archive contains an unsafe directory")

    def _read_exact(self, *, relative_path: str, digest: str, expected_bytes: int) -> bytes:
        if expected_bytes < 1 or expected_bytes > self.max_object_bytes:
            raise ResponseArchiveCorruption("archived object size is outside configured bounds")
        target = self._path(relative_path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except (FileNotFoundError, OSError) as exc:
            raise ResponseArchiveCorruption("archived object is missing or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ResponseArchiveCorruption("archived object is not a regular file")
            if metadata.st_size != expected_bytes:
                raise ResponseArchiveCorruption("archived object byte count changed")
            chunks: list[bytes] = []
            remaining = expected_bytes
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ResponseArchiveCorruption("archived object ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ResponseArchiveCorruption("archived object exceeds its receipt")
        finally:
            os.close(descriptor)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ResponseArchiveCorruption("archived object hash changed")
        return payload

    def _write_once(self, *, namespace: Literal["responses", "ledgers"], payload: bytes) -> str:
        if not payload:
            raise ResponseArchiveError("empty objects cannot enter the response archive")
        if len(payload) > self.max_object_bytes:
            raise ResponseArchiveError("object exceeds the response archive byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = self._relative_path(namespace, digest)
        target = self._path(relative_path)
        self._ensure_parent(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o400)
        except FileExistsError:
            self._read_exact(
                relative_path=relative_path,
                digest=digest,
                expected_bytes=len(payload),
            )
            return relative_path
        except OSError as exc:
            raise ResponseArchiveError("response archive refused a new object") from exc
        committed = False
        try:
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise ResponseArchiveError("response archive write made no progress")
                written += count
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            committed = True
        finally:
            os.close(descriptor)
            if not committed:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        self._read_exact(
            relative_path=relative_path,
            digest=digest,
            expected_bytes=len(payload),
        )
        return relative_path

    def store_response(
        self,
        *,
        payload: bytes,
        media_type: str,
        manifest: ProviderAdapterManifest,
        query: PlannedSearchQuery,
        request_sha256: str,
        received_at: AwareDatetime,
    ) -> ArchivedProviderResponse:
        if query.source_id != manifest.source_id:
            raise ResponseArchiveError("response source does not match its adapter manifest")
        if query.adapter_manifest_sha256 != manifest.manifest_sha256:
            raise ResponseArchiveError("response query is not bound to its adapter manifest")
        limit = min(self.max_object_bytes, manifest.max_response_bytes)
        if not payload or len(payload) > limit:
            raise ResponseArchiveError(
                "provider response is empty or exceeds its frozen byte limit"
            )
        policy_evidence = _metadata_policy_evidence(
            payload=payload, media_type=media_type, manifest=manifest
        )
        response_sha256 = hashlib.sha256(payload).hexdigest()
        relative_path = self._write_once(namespace="responses", payload=payload)
        return ArchivedProviderResponse(
            response_sha256=response_sha256,
            response_bytes=len(payload),
            media_type=media_type,
            relative_path=relative_path,
            source_id=manifest.source_id,
            adapter_manifest_sha256=manifest.manifest_sha256,
            logical_query_sha256=query.logical_query_sha256,
            request_sha256=request_sha256,
            policy_evidence_sha256=policy_evidence,
            received_at=received_at,
        )

    def read_response(self, receipt: ArchivedProviderResponse) -> bytes:
        return self._read_exact(
            relative_path=receipt.relative_path,
            digest=receipt.response_sha256,
            expected_bytes=receipt.response_bytes,
        )

    def store_ledger(
        self, *, value: object, object_sha256: str, archived_at: AwareDatetime
    ) -> ArchivedSearchLedger:
        payload = canonical_json_bytes(value)
        relative_path = self._write_once(namespace="ledgers", payload=payload)
        return ArchivedSearchLedger(
            ledger_sha256=hashlib.sha256(payload).hexdigest(),
            ledger_bytes=len(payload),
            relative_path=relative_path,
            object_sha256=object_sha256,
            archived_at=archived_at,
        )

    def read_ledger(self, receipt: ArchivedSearchLedger) -> bytes:
        return self._read_exact(
            relative_path=receipt.relative_path,
            digest=receipt.ledger_sha256,
            expected_bytes=receipt.ledger_bytes,
        )


__all__ = [
    "ArchivedKnowledgeLedger",
    "ArchivedProviderResponse",
    "ArchivedSearchLedger",
    "ContentAddressedResponseArchive",
    "ResponseArchiveCorruption",
    "ResponseArchiveError",
    "ResponsePolicyViolation",
]
