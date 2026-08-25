"""Write-once F9-v1 observation-campaign migration compatibility adapter.

This migration-only adapter reads a frozen legacy campaign and writes only its immutable
graph/raw-run binding CAS. It is not validation or admission authority. The protected
``aletheia.observations`` and ``aletheia.research_controller`` packages never import this module;
production composition must opt into it at the outer migration boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.belief_update import (
    CommittedObservationValidationCampaign,
    ObservationValidationCampaign,
    load_observation_validation_campaign,
)
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
    ResponseArchiveError,
)
from aletheia.observations.adapters import ObservationAdapterVerificationError
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    RawRunEnvelope,
    ScientificBridgeModel,
    VerifiedObservationValidationCampaignProjection,
    validate_raw_run_structure,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_MAX_BINDING_BYTES = 1024 * 1024


class ObservationArchiveCorruption(ObservationAdapterVerificationError):
    """A legacy graph binding or its content-addressed campaign bytes became unsafe."""


class ArchivedF9ValidationCampaignBinding(ScientificBridgeModel):
    """Write-once bridge from one opaque F9 v1 campaign into one exact graph/raw-run scope."""

    schema_name: Literal["aletheia.archived_f9_validation_campaign_binding"] = (
        "aletheia.archived_f9_validation_campaign_binding"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    research_scope_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_graph_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    branch_id: str
    question_object_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_artifact_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign_ledger: ArchivedKnowledgeLedger
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_staging_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    namespace_seal_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_at: AwareDatetime
    bound_at: AwareDatetime

    @model_validator(mode="after")
    def _binding_is_content_addressed_and_temporal(
        self,
    ) -> "ArchivedF9ValidationCampaignBinding":
        if (
            self.campaign_ledger.object_sha256 != self.campaign_sha256
            or self.campaign_ledger.archived_at != self.committed_at
        ):
            raise ValueError("F9 binding ledger does not name the exact committed campaign")
        if self.bound_at < self.committed_at:
            raise ValueError("F9 graph binding predates campaign commitment")
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)


class ContentAddressedF9ValidationCampaignArchiveAdapter:
    """Write-once graph binding and fresh-reading verifier for an existing F9 file CAS."""

    def __init__(
        self,
        root: Path,
        *,
        campaign_archive: ContentAddressedResponseArchive,
    ) -> None:
        if not callable(getattr(campaign_archive, "read_ledger", None)):
            raise TypeError("F9 adapter requires a content-addressed campaign archive")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ObservationArchiveCorruption("F9 graph-binding root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ObservationArchiveCorruption("F9 graph-binding root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        self._campaign_archive = campaign_archive

    def archive_committed_campaign(
        self,
        *,
        committed_campaign: CommittedObservationValidationCampaign,
        raw_run: RawRunEnvelope,
        bound_at: datetime,
    ) -> ArchivedF9ValidationCampaignBinding:
        """Fresh-read an F9 campaign and publish its immutable graph-scoped lookup."""

        _require_utc(bound_at, label="F9 graph binding time")
        try:
            committed = CommittedObservationValidationCampaign.model_validate(
                committed_campaign.model_dump(mode="python")
            )
            raw_run = validate_raw_run_structure(raw_run)
            fresh_campaign = load_observation_validation_campaign(
                archive=self._campaign_archive,
                ledger=committed.ledger,
            )
            if fresh_campaign != committed.campaign:
                raise ValueError("embedded F9 campaign differs from freshly rehashed archive bytes")
            material = _verify_f9_raw_binding(campaign=fresh_campaign, raw_run=raw_run)
            authorization = raw_run.scientific_authorization.message
            if not (
                committed.committed_at <= bound_at < authorization.observation_admission_deadline
            ):
                raise ValueError("F9 graph binding is outside the observation admission deadline")
            binding = _campaign_binding(
                committed=committed,
                raw_run=raw_run,
                material=material,
                bound_at=bound_at,
            )
            payload = canonical_json_bytes(binding)
            if hashlib.sha256(payload).hexdigest() != binding.binding_sha256:
                raise ValueError("F9 binding canonical bytes changed content identity")
            self._write_once(
                relative_path=self._binding_path(binding.binding_sha256),
                payload=payload,
            )
            self._write_once(
                relative_path=self._index_path(binding.campaign_sha256),
                payload=binding.binding_sha256.encode("ascii"),
            )
            return self._load_binding(binding.campaign_sha256)
        except ObservationAdapterVerificationError:
            raise
        except (
            AttributeError,
            ResponseArchiveError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise ObservationAdapterVerificationError(
                "F9 campaign archive could not bind the exact graph-scoped raw observation"
            ) from exc

    def verify_observation_validation_campaign(
        self,
        *,
        campaign_sha256: str,
        raw_run: RawRunEnvelope,
        expected_validator_manifest_sha256: str,
        expected_observation_validation_policy_sha256: str,
        observed_at: datetime,
    ) -> VerifiedObservationValidationCampaignProjection:
        """Fresh-rehash the binding and F9 ledger, then derive the exact bridge projection."""

        _require_utc(observed_at, label="F9 campaign verification time")
        if re.fullmatch(_SHA256_PATTERN, campaign_sha256) is None:
            raise ObservationAdapterVerificationError("F9 campaign identity is not SHA-256")
        try:
            raw_run = validate_raw_run_structure(raw_run)
            binding = self._load_binding(campaign_sha256)
            fresh_campaign = load_observation_validation_campaign(
                archive=self._campaign_archive,
                ledger=binding.campaign_ledger,
            )
            committed = CommittedObservationValidationCampaign(
                campaign=fresh_campaign,
                ledger=binding.campaign_ledger,
                committed_at=binding.committed_at,
            )
            if committed.receipt_sha256 != binding.committed_campaign_sha256:
                raise ValueError("fresh F9 campaign differs from its committed receipt")
            material = _verify_f9_raw_binding(campaign=fresh_campaign, raw_run=raw_run)
            expected_binding = _campaign_binding(
                committed=committed,
                raw_run=raw_run,
                material=material,
                bound_at=binding.bound_at,
            )
            if expected_binding != binding:
                raise ValueError("F9 campaign was rebound to another graph or raw artifact")

            authorization = raw_run.scientific_authorization.message
            if (
                expected_validator_manifest_sha256 != authorization.validator_manifest_sha256
                or expected_observation_validation_policy_sha256
                != authorization.observation_validation_policy_sha256
                or fresh_campaign.validator_manifest.manifest_sha256
                != expected_validator_manifest_sha256
                or fresh_campaign.policy.policy_sha256
                != expected_observation_validation_policy_sha256
            ):
                raise ValueError(
                    "F9 campaign differs from the externally pinned validation material"
                )
            if not (binding.bound_at <= observed_at < authorization.observation_admission_deadline):
                raise ValueError("F9 verification is outside the observation admission deadline")
            return _campaign_projection(
                committed=committed,
                raw_run=raw_run,
                material=material,
            )
        except ObservationAdapterVerificationError:
            raise
        except (
            AttributeError,
            ResponseArchiveError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise ObservationAdapterVerificationError(
                "F9 campaign archive did not prove the requested validation campaign"
            ) from exc

    def _binding_path(self, digest: str) -> str:
        return f"bindings/{digest[:2]}/{digest[2:4]}/{digest}.json"

    def _index_path(self, campaign_sha256: str) -> str:
        return f"campaigns/{campaign_sha256[:2]}/{campaign_sha256[2:4]}/{campaign_sha256}.ref"

    def _path(self, relative_path: str) -> Path:
        parts = Path(relative_path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ObservationArchiveCorruption("F9 archive path is not canonical")
        target = self.root.joinpath(*parts)
        if self.root not in target.parents:
            raise ObservationArchiveCorruption("F9 archive path escapes its root")
        return target

    def _check_parent_chain(self, target: Path, *, create: bool) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ObservationArchiveCorruption("F9 archive root became unsafe")
        current = self.root
        for part in target.parent.relative_to(self.root).parts:
            current /= part
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            if current.is_symlink() or not current.is_dir():
                raise ObservationArchiveCorruption(
                    "F9 archive contains an unsafe directory component"
                )

    def _read_regular(
        self,
        relative_path: str,
        *,
        maximum_bytes: int,
        exact_bytes: int | None = None,
    ) -> bytes:
        target = self._path(relative_path)
        self._check_parent_chain(target, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except (FileNotFoundError, OSError) as exc:
            raise ObservationArchiveCorruption("F9 archive object is missing or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ObservationArchiveCorruption("F9 archive object is not a regular file")
            if (
                metadata.st_size < 1
                or metadata.st_size > maximum_bytes
                or (exact_bytes is not None and metadata.st_size != exact_bytes)
            ):
                raise ObservationArchiveCorruption("F9 archive object has an invalid byte count")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ObservationArchiveCorruption("F9 archive object ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ObservationArchiveCorruption("F9 archive object grew while being read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _write_once(self, *, relative_path: str, payload: bytes) -> None:
        target = self._path(relative_path)
        self._check_parent_chain(target, create=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o400)
        except FileExistsError:
            existing = self._read_regular(
                relative_path,
                maximum_bytes=max(_MAX_BINDING_BYTES, len(payload)),
                exact_bytes=len(payload),
            )
            if existing != payload:
                raise ObservationArchiveCorruption(
                    "write-once F9 archive identity contains different bytes"
                )
            return
        except OSError as exc:
            raise ObservationArchiveCorruption("F9 archive refused a new object") from exc
        committed = False
        try:
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise ObservationArchiveCorruption("F9 archive write made no progress")
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
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _load_binding(self, campaign_sha256: str) -> ArchivedF9ValidationCampaignBinding:
        pointer = self._read_regular(
            self._index_path(campaign_sha256),
            maximum_bytes=64,
            exact_bytes=64,
        )
        try:
            binding_sha256 = pointer.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ObservationArchiveCorruption("F9 campaign index is not ASCII") from exc
        if re.fullmatch(_SHA256_PATTERN, binding_sha256) is None:
            raise ObservationArchiveCorruption("F9 campaign index is not a canonical SHA-256")
        payload = self._read_regular(
            self._binding_path(binding_sha256),
            maximum_bytes=_MAX_BINDING_BYTES,
        )
        if hashlib.sha256(payload).hexdigest() != binding_sha256:
            raise ObservationArchiveCorruption("F9 graph-binding content hash changed")
        try:
            binding = ArchivedF9ValidationCampaignBinding.model_validate_json(payload)
        except ValidationError as exc:
            raise ObservationArchiveCorruption("F9 graph-binding object is invalid") from exc
        if (
            canonical_json_bytes(binding) != payload
            or binding.binding_sha256 != binding_sha256
            or binding.campaign_sha256 != campaign_sha256
        ):
            raise ObservationArchiveCorruption("F9 graph binding changed canonical identity")
        return binding


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ObservationAdapterVerificationError(f"{label} must be timezone-aware UTC")


def _selected_prediction(campaign: ObservationValidationCampaign):
    request = campaign.request
    matches = tuple(
        candidate
        for candidate in request.committed_selection.campaign.request.candidates
        if candidate.candidate_id == request.selected_candidate_id
    )
    if len(matches) != 1:
        raise ValueError("F9 request does not resolve one exact selected prediction")
    return matches[0].committed_prediction


def _verify_f9_raw_binding(
    *,
    campaign: ObservationValidationCampaign,
    raw_run: RawRunEnvelope,
) -> tuple[object, object, object]:
    authorization = raw_run.scientific_authorization.message
    artifact_binding = authorization.scientific_observation_artifact_binding
    if raw_run.terminal_submission.disposition != "process_succeeded":
        raise ValueError("F9 campaign cannot convert engineering failure into an observation")
    entries = tuple(
        item
        for item in raw_run.artifact_manifest.entries
        if item.artifact_key == artifact_binding.artifact_key
    )
    if len(entries) != 1:
        raise ValueError("raw run does not contain one exact scientific observation artifact")
    entry = entries[0]
    receipts = tuple(
        receipt for receipt in raw_run.artifact_verified_receipts if receipt.artifact == entry
    )
    if len(receipts) != 1:
        raise ValueError("raw observation does not have one exact artifact custody receipt")
    artifact_receipt = receipts[0]

    request = campaign.request
    observation = request.observation_receipt
    selection = request.committed_selection
    prediction = _selected_prediction(campaign)
    expected = (
        (campaign.validator_manifest.manifest_sha256, authorization.validator_manifest_sha256),
        (campaign.policy.policy_sha256, authorization.observation_validation_policy_sha256),
        (selection.campaign.campaign_sha256, artifact_binding.selection_campaign_sha256),
        (
            prediction.campaign.campaign_sha256,
            artifact_binding.prediction_campaign_sha256,
        ),
        (
            prediction.campaign.commitment_sha256,
            artifact_binding.prediction_commitment_sha256,
        ),
        (observation.experiment_namespace_sha256, artifact_binding.observation_namespace_sha256),
        (observation.prediction_campaign_sha256, prediction.campaign.campaign_sha256),
        (observation.commitment_sha256, prediction.campaign.commitment_sha256),
        (observation.prediction_commitment_receipt_sha256, prediction.receipt_sha256),
        (observation.observation_sha256, entry.content_sha256),
        (observation.observation_bytes, entry.bytes),
        (observation.media_type, entry.media_type),
    )
    if any(actual != required for actual, required in expected):
        raise ValueError("F9 campaign escaped its graph-bound raw observation material")
    if (
        not (
            raw_run.accepted_runtime_termination.runtime_ended_at
            <= observation.observed_at
            <= observation.staged_at
            <= request.issued_at
            <= campaign.generated_at
        )
        or max(artifact_receipt.verified_at, raw_run.assembled_at) > request.issued_at
    ):
        raise ValueError("raw custody and F9 validation times are out of order")
    if campaign.generated_at >= authorization.observation_admission_deadline:
        raise ValueError("F9 campaign was generated after the observation admission deadline")
    return prediction, entry, artifact_receipt


def _campaign_binding(
    *,
    committed: CommittedObservationValidationCampaign,
    raw_run: RawRunEnvelope,
    material: tuple[object, object, object],
    bound_at: datetime,
) -> ArchivedF9ValidationCampaignBinding:
    prediction, entry, artifact_receipt = material
    campaign = committed.campaign
    request = campaign.request
    authorization = raw_run.scientific_authorization.message
    action_binding = authorization.action_protocol_binding
    scope = action_binding.compilation_request.protocol.graph_scope
    artifact_binding = authorization.scientific_observation_artifact_binding
    observation = request.observation_receipt
    return ArchivedF9ValidationCampaignBinding(
        quest_id=scope.scope_binding.quest_id,
        research_scope_binding_sha256=scope.scope_binding.binding_sha256,
        graph_scope_sha256=scope.graph_scope_sha256,
        authorized_graph_snapshot_sha256=action_binding.authorized_graph_snapshot_sha256,
        branch_id=scope.branch_id,
        question_object_sha256=scope.question_ref.object_sha256,
        protocol_sha256=action_binding.compilation_request.protocol.protocol_sha256,
        scientific_slot_id=authorization.scientific_slot_id,
        raw_run_sha256=raw_run.raw_run_sha256,
        scientific_observation_artifact_binding_sha256=artifact_binding.binding_sha256,
        artifact_verified_receipt_sha256=artifact_receipt.verified_receipt_sha256,
        raw_observation_content_sha256=entry.content_sha256,
        campaign_sha256=campaign.campaign_sha256,
        committed_campaign_sha256=committed.receipt_sha256,
        campaign_ledger=committed.ledger,
        validator_manifest_sha256=campaign.validator_manifest.manifest_sha256,
        observation_validation_policy_sha256=campaign.policy.policy_sha256,
        observation_namespace_sha256=observation.experiment_namespace_sha256,
        selection_campaign_sha256=request.committed_selection.campaign.campaign_sha256,
        prediction_campaign_sha256=prediction.campaign.campaign_sha256,
        prediction_commitment_sha256=prediction.campaign.commitment_sha256,
        prediction_commitment_receipt_sha256=prediction.receipt_sha256,
        observation_staging_receipt_sha256=observation.receipt_sha256,
        namespace_seal_sha256=observation.namespace_seal_sha256,
        committed_at=committed.committed_at,
        bound_at=bound_at,
    )


def _campaign_projection(
    *,
    committed: CommittedObservationValidationCampaign,
    raw_run: RawRunEnvelope,
    material: tuple[object, object, object],
) -> VerifiedObservationValidationCampaignProjection:
    prediction, entry, artifact_receipt = material
    campaign = committed.campaign
    request = campaign.request
    authorization = raw_run.scientific_authorization.message
    artifact_binding = authorization.scientific_observation_artifact_binding
    observation = request.observation_receipt
    return VerifiedObservationValidationCampaignProjection(
        observation_staging_receipt_sha256=observation.receipt_sha256,
        validation_request_sha256=request.request_sha256,
        campaign_sha256=campaign.campaign_sha256,
        committed_campaign_sha256=committed.receipt_sha256,
        validation_batch_sha256=(
            campaign.validation_batch.batch_sha256
            if campaign.validation_batch is not None
            else None
        ),
        validator_manifest_sha256=campaign.validator_manifest.manifest_sha256,
        observation_validation_policy_sha256=campaign.policy.policy_sha256,
        observation_namespace_sha256=observation.experiment_namespace_sha256,
        protocol_sha256=(
            authorization.action_protocol_binding.compilation_request.protocol.protocol_sha256
        ),
        selection_campaign_sha256=request.committed_selection.campaign.campaign_sha256,
        prediction_campaign_sha256=prediction.campaign.campaign_sha256,
        prediction_commitment_sha256=prediction.campaign.commitment_sha256,
        prediction_commitment_receipt_sha256=prediction.receipt_sha256,
        observation_receipt_sha256=observation.receipt_sha256,
        namespace_seal_sha256=observation.namespace_seal_sha256,
        raw_run_sha256=raw_run.raw_run_sha256,
        scientific_observation_artifact_binding_sha256=artifact_binding.binding_sha256,
        artifact_verified_receipt_sha256=artifact_receipt.verified_receipt_sha256,
        raw_observation_content_sha256=entry.content_sha256,
        outcome_bin_id=campaign.probe.outcome_bin_id if campaign.probe is not None else None,
        disposition=BridgeValidationDisposition(campaign.disposition.value),
        blocker_codes=campaign.blockers,
        generated_at=campaign.generated_at,
        committed_at=committed.committed_at,
    )


__all__ = [
    "ArchivedF9ValidationCampaignBinding",
    "ContentAddressedF9ValidationCampaignArchiveAdapter",
    "ObservationArchiveCorruption",
]
