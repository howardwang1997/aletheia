"""Immutable F10 experiment-capability registry and compatibility checks."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.schemas import (
    CapabilityLifecycle,
    ExperimentCapabilityManifest,
    safety_class_rank,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


class CapabilityRegistryError(RuntimeError):
    """Base class for invalid, missing, or corrupt capability registry operations."""


class UnsupportedCapability(CapabilityRegistryError):
    """No exact active capability satisfies the requested identity."""


class IncompatibleCapabilityVersion(CapabilityRegistryError):
    """A manifest version violates append-only compatibility semantics."""


class CapabilityRegistrySnapshot(FrozenModel):
    schema_name: Literal["aletheia.capability_registry_snapshot"] = (
        "aletheia.capability_registry_snapshot"
    )
    schema_version: Literal[1] = 1
    registry_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    manifests: tuple[ExperimentCapabilityManifest, ...] = Field(min_length=1)
    created_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _registry_is_canonical_and_compatible(self) -> "CapabilityRegistrySnapshot":
        expected = tuple(
            sorted(
                self.manifests,
                key=lambda item: (item.capability_id, item.semantic_version),
            )
        )
        if self.manifests != expected:
            raise ValueError("capability registry manifests must be canonically ordered")
        identities = [(item.capability_id, item.version) for item in self.manifests]
        if len(identities) != len(set(identities)):
            raise ValueError("capability registry repeats a capability version")
        hashes = [item.manifest_sha256 for item in self.manifests]
        if len(hashes) != len(set(hashes)):
            raise ValueError("capability registry repeats a manifest object")
        if self.created_at < max(item.frozen_at for item in self.manifests):
            raise ValueError("capability registry snapshot predates a manifest")
        grouped: dict[str, list[ExperimentCapabilityManifest]] = defaultdict(list)
        for manifest in self.manifests:
            grouped[manifest.capability_id].append(manifest)
        for chain in grouped.values():
            _validate_version_chain(tuple(chain))
        return self

    @property
    def snapshot_sha256(self) -> str:
        return content_sha256(self)


class CommittedCapabilityRegistry(FrozenModel):
    schema_version: Literal[1] = 1
    snapshot: CapabilityRegistrySnapshot
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_snapshot(self) -> "CommittedCapabilityRegistry":
        payload = canonical_json_bytes(self.snapshot)
        if (
            self.ledger.object_sha256 != self.snapshot.snapshot_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("capability registry ledger does not commit its snapshot")
        return self

    @property
    def commitment_sha256(self) -> str:
        return content_sha256(self)


def _lifecycle_rank(value: CapabilityLifecycle) -> int:
    return {
        CapabilityLifecycle.PROVISIONAL: 0,
        CapabilityLifecycle.REGISTERED: 1,
        CapabilityLifecycle.RETIRED: 2,
    }[value]


def _validate_compatible_successor(
    previous: ExperimentCapabilityManifest,
    current: ExperimentCapabilityManifest,
) -> None:
    if current.domain != previous.domain:
        raise IncompatibleCapabilityVersion("capability domain cannot change across versions")
    if current.semantic_version <= previous.semantic_version:
        raise IncompatibleCapabilityVersion("capability versions must increase strictly")
    if current.supersedes_manifest_sha256 != previous.manifest_sha256:
        raise IncompatibleCapabilityVersion(
            "capability successor must bind the exact prior manifest"
        )
    if _lifecycle_rank(current.lifecycle) < _lifecycle_rank(previous.lifecycle):
        raise IncompatibleCapabilityVersion("capability lifecycle cannot move backward")
    previous_major = previous.semantic_version[0]
    current_major = current.semantic_version[0]
    breaking_fields = (
        current.action_type != previous.action_type,
        current.input_schema.json_schema_sha256 != previous.input_schema.json_schema_sha256,
        current.output_schema.json_schema_sha256 != previous.output_schema.json_schema_sha256,
        current.preregistration_schema.json_schema_sha256
        != previous.preregistration_schema.json_schema_sha256,
        current.accepted_data_modalities != previous.accepted_data_modalities,
    )
    if any(breaking_fields) and current_major == previous_major:
        raise IncompatibleCapabilityVersion(
            "breaking capability contract changes require a major version bump"
        )
    if current_major == previous_major:
        if not set(current.required_metadata).issubset(previous.required_metadata):
            raise IncompatibleCapabilityVersion(
                "compatible capability update cannot add required metadata"
            )
        if safety_class_rank(current.safety_class) < safety_class_rank(previous.safety_class):
            raise IncompatibleCapabilityVersion(
                "compatible capability update cannot silently lower safety class"
            )
    if previous.lifecycle is CapabilityLifecycle.RETIRED:
        raise IncompatibleCapabilityVersion("retired capability lineages cannot be reactivated")


def _validate_version_chain(chain: tuple[ExperimentCapabilityManifest, ...]) -> None:
    domains = {item.domain for item in chain}
    if len(domains) != 1:
        raise IncompatibleCapabilityVersion("one capability ID cannot span multiple domains")
    if chain[0].supersedes_manifest_sha256 is not None:
        raise IncompatibleCapabilityVersion("first capability version cannot supersede an object")
    for previous, current in zip(chain, chain[1:], strict=False):
        _validate_compatible_successor(previous, current)


def build_capability_registry_snapshot(
    *,
    registry_id: str,
    manifests: tuple[ExperimentCapabilityManifest, ...],
    created_at: datetime,
) -> CapabilityRegistrySnapshot:
    return CapabilityRegistrySnapshot(
        registry_id=registry_id,
        manifests=tuple(
            sorted(manifests, key=lambda item: (item.capability_id, item.semantic_version))
        ),
        created_at=created_at,
    )


def commit_capability_registry(
    *,
    archive: ContentAddressedResponseArchive,
    snapshot: CapabilityRegistrySnapshot,
    committed_at: datetime,
) -> CommittedCapabilityRegistry:
    if committed_at < snapshot.created_at:
        raise ValueError("capability registry commitment predates its snapshot")
    ledger = archive.store_ledger(
        value=snapshot,
        object_sha256=snapshot.snapshot_sha256,
        archived_at=committed_at,
    )
    return CommittedCapabilityRegistry(snapshot=snapshot, ledger=ledger)


def load_capability_registry(
    *,
    archive: ContentAddressedResponseArchive,
    committed: CommittedCapabilityRegistry,
) -> CapabilityRegistrySnapshot:
    payload = archive.read_ledger(committed.ledger)
    snapshot = CapabilityRegistrySnapshot.model_validate_json(payload)
    if snapshot != committed.snapshot:
        raise CapabilityRegistryError(
            "physical capability registry differs from committed snapshot"
        )
    return snapshot


class CapabilityRegistry:
    """Exact discovery API over one immutable registry snapshot."""

    def __init__(self, snapshot: CapabilityRegistrySnapshot) -> None:
        self.snapshot = snapshot
        grouped: dict[str, list[ExperimentCapabilityManifest]] = defaultdict(list)
        for manifest in snapshot.manifests:
            grouped[manifest.capability_id].append(manifest)
        self._chains = {key: tuple(value) for key, value in grouped.items()}

    def get(
        self,
        capability_id: str,
        *,
        version: str | None = None,
        allow_provisional: bool = False,
    ) -> ExperimentCapabilityManifest:
        chain = self._chains.get(capability_id)
        if chain is None:
            raise UnsupportedCapability(f"unsupported capability ID {capability_id!r}")
        if version is not None:
            candidates = [item for item in chain if item.version == version]
            if not candidates:
                raise UnsupportedCapability(
                    f"unsupported capability version {capability_id!r}@{version!r}"
                )
            manifest = candidates[0]
        else:
            manifest = chain[-1]
        if manifest.lifecycle is CapabilityLifecycle.RETIRED:
            raise UnsupportedCapability(f"capability {capability_id!r} is retired")
        if manifest.lifecycle is CapabilityLifecycle.PROVISIONAL and not allow_provisional:
            raise UnsupportedCapability(
                f"capability {capability_id!r} is provisional, not registered"
            )
        return manifest

    def latest_manifests(self, *, include_provisional: bool = False):
        manifests = tuple(chain[-1] for chain in self._chains.values())
        return tuple(
            sorted(
                (
                    item
                    for item in manifests
                    if item.lifecycle is CapabilityLifecycle.REGISTERED
                    or (include_provisional and item.lifecycle is CapabilityLifecycle.PROVISIONAL)
                ),
                key=lambda item: item.capability_id,
            )
        )


__all__ = [
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "CapabilityRegistrySnapshot",
    "CommittedCapabilityRegistry",
    "IncompatibleCapabilityVersion",
    "UnsupportedCapability",
    "build_capability_registry_snapshot",
    "commit_capability_registry",
    "load_capability_registry",
]
