"""Graph-scoped F9-v2 observation-validation campaigns and write-once custody.

This module never imports the legacy F9 control plane.  It binds an independently signed
validation assessment to the exact Research Kernel graph scope, v2 world model and predictions,
scientific slot, PR-4 raw run, and preregistered observation artifact.  A write-once raw-run index
is the publication linearization point; every later verification reopens and rehashes its bytes.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    EngineeringQualificationCustodyVerificationPort,
    ObservationDatabaseAuthorityPin,
    ObservationValidationReceipt,
    RawRunCustodyVerificationPort,
    RawRunEnvelope,
    ResearchActionAuthorityVerificationPort,
    ScientificBridgeAuthorityPin,
    ScientificBridgeModel,
    ScientificBridgeRole,
    ValidationIssuanceChallenge,
    VerifiedObservationValidationCampaignProjection,
    issue_observation_validation_receipt,
    validate_raw_run_structure,
    verify_raw_run_for_independent_validation,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.:/-]{1,127}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MEDIA_TYPE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_CAMPAIGN_SIGNATURE_CONTEXT = b"aletheia.f9_v2_validation.ed25519.v1\0"
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024


class F9V2ValidationError(RuntimeError):
    """A graph-scoped F9-v2 campaign or its immutable custody failed closed."""


class F9V2ValidationRequest(ScientificBridgeModel):
    """Exact pre-assessment scope mechanically derived from a signed raw run."""

    schema_name: Literal["aletheia.f9_v2_observation_validation_request"] = (
        "aletheia.f9_v2_observation_validation_request"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_authorized_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_graph_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    research_scope_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    branch_id: str = Field(pattern=r"^rbr_[0-9a-f]{32}$")
    question_object_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypothesis_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    prediction_sha256s: tuple[str, ...] = Field(min_length=1, max_length=1024)
    analysis_outcome_space_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_artifact_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_bytes: int = Field(ge=1)
    raw_observation_media_type: str = Field(pattern=_MEDIA_TYPE_PATTERN)
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    requested_at: AwareDatetime
    observation_admission_deadline: AwareDatetime

    @model_validator(mode="after")
    def _request_is_canonical(self) -> "F9V2ValidationRequest":
        for values, label in (
            (self.hypothesis_sha256s, "hypotheses"),
            (self.prediction_sha256s, "predictions"),
        ):
            if values != tuple(sorted(set(values))) or any(
                re.fullmatch(_SHA256_PATTERN, value) is None for value in values
            ):
                raise ValueError(f"F9-v2 validation {label} must be unique and canonical")
        if not self.requested_at < self.observation_admission_deadline:
            raise ValueError("F9-v2 validation request has no live admission window")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def observation_staging_receipt_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_observation_staging_receipt",
                "schema_version": 1,
                "scientific_slot_id": self.scientific_slot_id,
                "raw_run_sha256": self.raw_run_sha256,
                "artifact_key": self.artifact_key,
                "artifact_verified_receipt_sha256": (self.artifact_verified_receipt_sha256),
                "raw_observation_content_sha256": self.raw_observation_content_sha256,
                "observation_namespace_sha256": self.observation_namespace_sha256,
            }
        )

    @property
    def prediction_commitment_receipt_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_prediction_commitment_receipt",
                "schema_version": 1,
                "graph_scope_sha256": self.graph_scope_sha256,
                "world_model_snapshot_sha256": self.world_model_snapshot_sha256,
                "measurement_protocol_sha256": self.measurement_protocol_sha256,
                "prediction_sha256s": self.prediction_sha256s,
                "prediction_campaign_sha256": self.prediction_campaign_sha256,
                "prediction_commitment_sha256": self.prediction_commitment_sha256,
            }
        )

    @property
    def observation_receipt_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_raw_observation_receipt",
                "schema_version": 1,
                "observation_staging_receipt_sha256": (self.observation_staging_receipt_sha256),
                "raw_observation_bytes": self.raw_observation_bytes,
                "raw_observation_media_type": self.raw_observation_media_type,
            }
        )

    @property
    def namespace_seal_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_observation_namespace_seal",
                "schema_version": 1,
                "quest_id": self.quest_id,
                "graph_scope_sha256": self.graph_scope_sha256,
                "protocol_sha256": self.protocol_sha256,
                "scientific_slot_id": self.scientific_slot_id,
                "observation_namespace_sha256": self.observation_namespace_sha256,
            }
        )


class F9V2IndependentValidationAssessment(ScientificBridgeModel):
    """Authority-neutral result produced by a deployment-owned validator implementation."""

    schema_name: Literal["aletheia.f9_v2_independent_validation_assessment"] = (
        "aletheia.f9_v2_independent_validation_assessment"
    )
    schema_version: Literal[1] = 1
    validation_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: BridgeValidationDisposition
    outcome_bin_id: str | None = Field(default=None, pattern=_LOCAL_ID_PATTERN)
    validation_batch_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    blocker_codes: tuple[str, ...] = Field(max_length=256)
    assessed_at: AwareDatetime

    @model_validator(mode="after")
    def _assessment_is_closed(self) -> "F9V2IndependentValidationAssessment":
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))) or any(
            re.fullmatch(_LOCAL_ID_PATTERN, value) is None for value in self.blocker_codes
        ):
            raise ValueError("F9-v2 assessment blockers must be unique and canonical")
        if self.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
            if (
                self.outcome_bin_id is None
                or self.validation_batch_sha256 is None
                or self.blocker_codes
            ):
                raise ValueError(
                    "validated F9-v2 assessment requires batch/outcome and no blockers"
                )
        elif self.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC:
            if (
                self.outcome_bin_id is None
                or self.validation_batch_sha256 is None
                or not self.blocker_codes
            ):
                raise ValueError("rejected F9-v2 assessment requires batch/outcome/blockers")
        elif (
            self.outcome_bin_id is not None
            or self.validation_batch_sha256 is not None
            or not self.blocker_codes
        ):
            raise ValueError("blocked F9-v2 assessment requires blockers and no outcome batch")
        return self

    @property
    def assessment_sha256(self) -> str:
        return canonical_sha256(self)


class F9V2ValidationCampaignMessage(ScientificBridgeModel):
    schema_name: Literal["aletheia.f9_v2_observation_validation_campaign_message"] = (
        "aletheia.f9_v2_observation_validation_campaign_message"
    )
    schema_version: Literal[1] = 1
    request: F9V2ValidationRequest
    assessment: F9V2IndependentValidationAssessment
    validator_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    validated_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    validation_key_id: str = Field(pattern=_SHA256_PATTERN)
    generated_at: AwareDatetime
    independent_from_executor: Literal[True] = True
    graph_scoped_f9_version: Literal[2] = 2
    legacy_f9_authority_used: Literal[False] = False

    @model_validator(mode="after")
    def _campaign_is_exact(self) -> "F9V2ValidationCampaignMessage":
        if (
            self.assessment.validation_request_sha256 != self.request.request_sha256
            or self.assessment.raw_observation_content_sha256
            != self.request.raw_observation_content_sha256
            or self.assessment.assessed_at != self.generated_at
            or not self.request.requested_at
            <= self.generated_at
            < self.request.observation_admission_deadline
        ):
            raise ValueError("F9-v2 campaign assessment escaped its request or time window")
        return self

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class SignedF9V2ValidationCampaign(ScientificBridgeModel):
    schema_name: Literal["aletheia.signed_f9_v2_observation_validation_campaign"] = (
        "aletheia.signed_f9_v2_observation_validation_campaign"
    )
    schema_version: Literal[1] = 1
    message: F9V2ValidationCampaignMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _CAMPAIGN_SIGNATURE_CONTEXT + canonical_json_bytes(self.message)

    @property
    def campaign_sha256(self) -> str:
        return canonical_sha256(self)


class CommittedF9V2ValidationCampaign(ScientificBridgeModel):
    schema_name: Literal["aletheia.committed_f9_v2_observation_validation_campaign"] = (
        "aletheia.committed_f9_v2_observation_validation_campaign"
    )
    schema_version: Literal[1] = 1
    campaign: SignedF9V2ValidationCampaign
    campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_at: AwareDatetime
    write_once_raw_run_binding: Literal[True] = True

    @model_validator(mode="after")
    def _commit_is_exact(self) -> "CommittedF9V2ValidationCampaign":
        if (
            self.campaign_sha256 != self.campaign.campaign_sha256
            or self.raw_run_sha256 != self.campaign.message.request.raw_run_sha256
            or not self.campaign.message.generated_at
            <= self.committed_at
            < self.campaign.message.request.observation_admission_deadline
        ):
            raise ValueError("F9-v2 campaign commit changed campaign, raw run, or time")
        return self

    @property
    def committed_campaign_sha256(self) -> str:
        return canonical_sha256(self)


class F9V2ObservationAssessmentPort(Protocol):
    """Deployment-owned analysis implementation; the archive signs only its closed result."""

    def assess_observation(
        self,
        *,
        request: F9V2ValidationRequest,
        raw_run: RawRunEnvelope,
        assessed_at: datetime,
    ) -> F9V2IndependentValidationAssessment: ...


@dataclass(frozen=True)
class F9V2BridgeVerificationContext:
    """Public verification material available to the external validator service."""

    qualification_authority: QualificationAuthorityVerifier
    action_authority: ResearchActionAuthorityVerificationPort
    qualification_custody: EngineeringQualificationCustodyVerificationPort
    raw_run_custody: RawRunCustodyVerificationPort
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin
    database_authority_pin: ObservationDatabaseAuthorityPin


def build_f9_v2_validation_request(
    *,
    raw_run: RawRunEnvelope,
    requested_at: datetime,
) -> F9V2ValidationRequest:
    """Derive the exact graph/world-model/prediction scope from a successful raw run."""

    _require_utc(requested_at, label="F9-v2 validation request time")
    try:
        raw_run = validate_raw_run_structure(raw_run)
        authorization = raw_run.scientific_authorization.message
        binding = authorization.action_protocol_binding
        protocol = binding.compilation_request.protocol
        world_model = protocol.world_model
        artifact_binding = authorization.scientific_observation_artifact_binding
        if raw_run.accepted_terminal_submission.disposition != "process_succeeded":
            raise ValueError("engineering failure cannot create an F9-v2 validation campaign")
        if world_model is None:
            raise ValueError("F9-v2 validation requires a graph-scoped world model")
        entries = tuple(
            item
            for item in raw_run.artifact_manifest.entries
            if item.artifact_key == artifact_binding.artifact_key
        )
        receipts = tuple(
            item
            for item in raw_run.artifact_verified_receipts
            if item.artifact.artifact_key == artifact_binding.artifact_key
        )
        if len(entries) != 1 or len(receipts) != 1 or receipts[0].artifact != entries[0]:
            raise ValueError("F9-v2 request lacks one exact verified raw observation")
        matching_predictions = tuple(
            item
            for item in world_model.predictions
            if item.observable_spec_sha256 == artifact_binding.observable.observable_sha256
            and item.measurement_protocol_sha256 == protocol.method.method_contract_sha256
            and item.outcome_space_sha256 == protocol.analysis_plan.outcome_space_sha256
        )
        if not matching_predictions:
            raise ValueError("F9-v2 request has no preregistered prediction for its observable")
        if not raw_run.assembled_at <= requested_at < authorization.observation_admission_deadline:
            raise ValueError("F9-v2 request is outside the raw-run admission window")
        scope = protocol.graph_scope
        return F9V2ValidationRequest(
            quest_id=binding.action.quest_id,
            action_sha256=binding.action.object_sha256,
            action_authorized_event_sha256=binding.action_authorized_event.event_sha256,
            authorized_graph_snapshot_sha256=binding.authorized_graph_snapshot_sha256,
            research_scope_binding_sha256=scope.scope_binding.binding_sha256,
            graph_scope_sha256=scope.graph_scope_sha256,
            branch_id=scope.branch_id,
            question_object_sha256=scope.question_ref.object_sha256,
            protocol_sha256=protocol.protocol_sha256,
            world_model_snapshot_sha256=world_model.world_model_sha256,
            measurement_protocol_sha256=protocol.method.method_contract_sha256,
            hypothesis_sha256s=tuple(
                sorted(item.hypothesis_sha256 for item in world_model.hypotheses)
            ),
            prediction_sha256s=tuple(
                sorted(item.prediction_sha256 for item in matching_predictions)
            ),
            analysis_outcome_space_sha256=protocol.analysis_plan.outcome_space_sha256,
            scientific_slot_id=authorization.scientific_slot_id,
            raw_run_sha256=raw_run.raw_run_sha256,
            scientific_observation_artifact_binding_sha256=artifact_binding.binding_sha256,
            artifact_key=artifact_binding.artifact_key,
            artifact_verified_receipt_sha256=receipts[0].verified_receipt_sha256,
            raw_observation_content_sha256=entries[0].content_sha256,
            raw_observation_bytes=entries[0].bytes,
            raw_observation_media_type=entries[0].media_type,
            validator_manifest_sha256=authorization.validator_manifest_sha256,
            observation_validation_policy_sha256=(
                authorization.observation_validation_policy_sha256
            ),
            observation_namespace_sha256=artifact_binding.observation_namespace_sha256,
            selection_campaign_sha256=artifact_binding.selection_campaign_sha256,
            prediction_campaign_sha256=artifact_binding.prediction_campaign_sha256,
            prediction_commitment_sha256=artifact_binding.prediction_commitment_sha256,
            requested_at=requested_at,
            observation_admission_deadline=authorization.observation_admission_deadline,
        )
    except F9V2ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed over nested signed/compiled material
        raise F9V2ValidationError(
            "raw run could not produce an exact graph-scoped F9-v2 validation request"
        ) from exc


def issue_f9_v2_validation_campaign(
    *,
    request: F9V2ValidationRequest,
    assessment: F9V2IndependentValidationAssessment,
    validator_manifest_sha256: str,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    private_key: bytes,
) -> SignedF9V2ValidationCampaign:
    """Sign one exact external assessment without granting observation admission."""

    try:
        request = F9V2ValidationRequest.model_validate(request.model_dump(mode="python"))
        assessment = F9V2IndependentValidationAssessment.model_validate(
            assessment.model_dump(mode="python")
        )
        _require_validator_private_key(private_key=private_key, pin=validator_authority_pin)
        if (
            validator_manifest_sha256 != request.validator_manifest_sha256
            or not validator_authority_pin.active_at(assessment.assessed_at)
        ):
            raise ValueError("F9-v2 campaign changed its validator manifest or key window")
        message = F9V2ValidationCampaignMessage(
            request=request,
            assessment=assessment,
            validator_authority_policy_sha256=validator_authority_pin.policy_sha256,
            validated_by_principal_id=validator_authority_pin.principal_id,
            validation_key_id=validator_authority_pin.key_id,
            generated_at=assessment.assessed_at,
        )
        unsigned = SignedF9V2ValidationCampaign(
            message=message,
            signature_ed25519_hex="0" * 128,
        )
        return unsigned.model_copy(
            update={
                "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
                .sign(unsigned.signature_message)
                .hex()
            }
        )
    except F9V2ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed at external signing authority
        raise F9V2ValidationError("F9-v2 validation campaign signing failed closed") from exc


def verify_f9_v2_validation_campaign(
    *,
    campaign: SignedF9V2ValidationCampaign,
    raw_run: RawRunEnvelope,
    validator_manifest_sha256: str,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    observed_at: datetime,
) -> SignedF9V2ValidationCampaign:
    """Verify campaign signature and mechanically rebuild its entire raw-run request."""

    _require_utc(observed_at, label="F9-v2 campaign observation time")
    try:
        campaign = SignedF9V2ValidationCampaign.model_validate(campaign.model_dump(mode="python"))
        message = campaign.message
        request = message.request
        rebuilt = build_f9_v2_validation_request(
            raw_run=raw_run,
            requested_at=request.requested_at,
        )
        if (
            rebuilt != request
            or validator_manifest_sha256 != request.validator_manifest_sha256
            or message.validator_authority_policy_sha256 != validator_authority_pin.policy_sha256
            or message.validated_by_principal_id != validator_authority_pin.principal_id
            or message.validation_key_id != validator_authority_pin.key_id
            or validator_authority_pin.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
            or not validator_authority_pin.active_at(message.generated_at)
            or not message.generated_at <= observed_at < request.observation_admission_deadline
        ):
            raise ValueError("F9-v2 campaign changed request, authority, or verification window")
        try:
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(validator_authority_pin.public_key_ed25519_hex)
            ).verify(bytes.fromhex(campaign.signature_ed25519_hex), campaign.signature_message)
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("F9-v2 campaign signature is invalid") from exc
        return campaign
    except F9V2ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed across signed graph/raw material
        raise F9V2ValidationError("F9-v2 validation campaign verification failed closed") from exc


class WriteOnceF9V2ValidationCampaignArchive:
    """One immutable campaign binding per raw run with fresh byte rehash on every read."""

    def __init__(
        self,
        root: Path,
        *,
        validator_manifest_sha256: str,
        validator_authority_pin: ScientificBridgeAuthorityPin,
        read_only: bool = False,
    ) -> None:
        if re.fullmatch(_SHA256_PATTERN, validator_manifest_sha256) is None:
            raise ValueError("F9-v2 archive validator manifest must be SHA-256")
        if validator_authority_pin.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR:
            raise ValueError("F9-v2 archive requires an observation-validator authority pin")
        candidate = Path(root)
        if candidate.is_symlink():
            raise F9V2ValidationError("F9-v2 archive root cannot be a symlink")
        if read_only:
            if not candidate.exists():
                raise F9V2ValidationError("read-only F9-v2 archive root must already exist")
        else:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022:
            raise F9V2ValidationError("F9-v2 archive root must be a private directory")
        self.root = candidate.resolve(strict=True)
        self.validator_manifest_sha256 = validator_manifest_sha256
        self.validator_authority_pin = ScientificBridgeAuthorityPin.model_validate(
            validator_authority_pin.model_dump(mode="python")
        )
        self.read_only = bool(read_only)

    def publish_campaign(
        self,
        *,
        campaign: SignedF9V2ValidationCampaign,
        raw_run: RawRunEnvelope,
        committed_at: datetime,
    ) -> CommittedF9V2ValidationCampaign:
        """Publish one campaign; a concurrent existing raw-run winner is returned exactly."""

        if self.read_only:
            raise F9V2ValidationError("read-only F9-v2 archive cannot publish campaigns")
        _require_utc(committed_at, label="F9-v2 campaign commitment time")
        campaign = verify_f9_v2_validation_campaign(
            campaign=campaign,
            raw_run=raw_run,
            validator_manifest_sha256=self.validator_manifest_sha256,
            validator_authority_pin=self.validator_authority_pin,
            observed_at=committed_at,
        )
        candidate = CommittedF9V2ValidationCampaign(
            campaign=campaign,
            campaign_sha256=campaign.campaign_sha256,
            raw_run_sha256=raw_run.raw_run_sha256,
            committed_at=committed_at,
        )
        payload = canonical_json_bytes(candidate)
        target = self._path(raw_run.raw_run_sha256)
        winner = self._publish_once(target=target, payload=payload)
        committed = self._parse_committed(winner, expected_raw_run_sha256=raw_run.raw_run_sha256)
        verify_f9_v2_validation_campaign(
            campaign=committed.campaign,
            raw_run=raw_run,
            validator_manifest_sha256=self.validator_manifest_sha256,
            validator_authority_pin=self.validator_authority_pin,
            observed_at=committed_at,
        )
        return committed

    def load_committed_campaign(
        self,
        *,
        raw_run: RawRunEnvelope,
        observed_at: datetime,
    ) -> CommittedF9V2ValidationCampaign | None:
        """Fresh-read the write-once raw-run binding, if one has been published."""

        _require_utc(observed_at, label="F9-v2 archive observation time")
        raw_run = validate_raw_run_structure(raw_run)
        target = self._path(raw_run.raw_run_sha256)
        if not self._prepare_parent(target, create=False):
            return None
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            return None
        if target.is_symlink() or not stat.S_ISREG(target_metadata.st_mode):
            raise F9V2ValidationError("F9-v2 campaign binding target is unsafe")
        if self.read_only:
            payload = self._read_regular(target)
        else:
            with self._publication_lock(target, exclusive=False):
                payload = self._read_regular(target)
        committed = self._parse_committed(
            payload,
            expected_raw_run_sha256=raw_run.raw_run_sha256,
        )
        verify_f9_v2_validation_campaign(
            campaign=committed.campaign,
            raw_run=raw_run,
            validator_manifest_sha256=self.validator_manifest_sha256,
            validator_authority_pin=self.validator_authority_pin,
            observed_at=observed_at,
        )
        if committed.committed_at > observed_at:
            raise F9V2ValidationError("F9-v2 archive commitment is future-dated")
        return committed

    def verify_observation_validation_campaign(
        self,
        *,
        campaign_sha256: str,
        raw_run: RawRunEnvelope,
        expected_validator_manifest_sha256: str,
        expected_observation_validation_policy_sha256: str,
        observed_at: datetime,
    ) -> VerifiedObservationValidationCampaignProjection:
        """Implement the scientific bridge's mandatory campaign-custody verification port."""

        committed = self.load_committed_campaign(raw_run=raw_run, observed_at=observed_at)
        if committed is None:
            raise F9V2ValidationError("F9-v2 campaign archive has no raw-run binding")
        request = committed.campaign.message.request
        assessment = committed.campaign.message.assessment
        if (
            committed.campaign_sha256 != campaign_sha256
            or expected_validator_manifest_sha256 != request.validator_manifest_sha256
            or expected_validator_manifest_sha256 != self.validator_manifest_sha256
            or expected_observation_validation_policy_sha256
            != request.observation_validation_policy_sha256
        ):
            raise F9V2ValidationError(
                "F9-v2 campaign differs from externally pinned validation material"
            )
        return VerifiedObservationValidationCampaignProjection(
            observation_staging_receipt_sha256=(request.observation_staging_receipt_sha256),
            validation_request_sha256=request.request_sha256,
            campaign_sha256=committed.campaign_sha256,
            committed_campaign_sha256=committed.committed_campaign_sha256,
            validation_batch_sha256=assessment.validation_batch_sha256,
            validator_manifest_sha256=request.validator_manifest_sha256,
            observation_validation_policy_sha256=(request.observation_validation_policy_sha256),
            observation_namespace_sha256=request.observation_namespace_sha256,
            protocol_sha256=request.protocol_sha256,
            selection_campaign_sha256=request.selection_campaign_sha256,
            prediction_campaign_sha256=request.prediction_campaign_sha256,
            prediction_commitment_sha256=request.prediction_commitment_sha256,
            prediction_commitment_receipt_sha256=(request.prediction_commitment_receipt_sha256),
            observation_receipt_sha256=request.observation_receipt_sha256,
            namespace_seal_sha256=request.namespace_seal_sha256,
            raw_run_sha256=request.raw_run_sha256,
            scientific_observation_artifact_binding_sha256=(
                request.scientific_observation_artifact_binding_sha256
            ),
            artifact_verified_receipt_sha256=(request.artifact_verified_receipt_sha256),
            raw_observation_content_sha256=request.raw_observation_content_sha256,
            outcome_bin_id=assessment.outcome_bin_id,
            disposition=assessment.disposition,
            blocker_codes=assessment.blocker_codes,
            generated_at=committed.campaign.message.generated_at,
            committed_at=committed.committed_at,
        )

    def _path(self, raw_run_sha256: str) -> Path:
        if re.fullmatch(_SHA256_PATTERN, raw_run_sha256) is None:
            raise F9V2ValidationError("F9-v2 archive raw-run identity is not SHA-256")
        target = self.root / "raw-runs" / raw_run_sha256[:2] / f"{raw_run_sha256}.json"
        if self.root not in target.parents:
            raise F9V2ValidationError("F9-v2 archive path escaped its root")
        return target

    def _prepare_parent(self, target: Path, *, create: bool) -> bool:
        current = self.root
        for component in target.parent.relative_to(self.root).parts:
            current /= component
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            try:
                metadata = current.lstat()
            except FileNotFoundError as exc:
                if not create:
                    return False
                raise F9V2ValidationError("F9-v2 archive parent chain is missing") from exc
            if (
                current.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_mode & 0o022
            ):
                raise F9V2ValidationError("F9-v2 archive parent chain became unsafe")
        return True

    def _publish_once(self, *, target: Path, payload: bytes) -> bytes:
        self._prepare_parent(target, create=True)
        with self._publication_lock(target, exclusive=True):
            return self._publish_once_locked(target=target, payload=payload)

    def _publish_once_locked(self, *, target: Path, payload: bytes) -> bytes:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o400)
        except FileExistsError:
            return self._read_regular(target)
        except OSError as exc:
            raise F9V2ValidationError("F9-v2 archive refused campaign publication") from exc
        committed = False
        try:
            offset = 0
            view = memoryview(payload)
            while offset < len(payload):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise F9V2ValidationError("F9-v2 archive write made no progress")
                offset += written
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
        parent = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return payload

    @contextmanager
    def _publication_lock(self, target: Path, *, exclusive: bool) -> Iterator[None]:
        """Serialize legitimate readers/writers so no partial first-write is observable."""

        lock_path = target.with_name(f".{target.name}.lock")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            if exclusive:
                try:
                    descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    descriptor = os.open(lock_path, flags)
            else:
                descriptor = os.open(lock_path, flags)
        except OSError as exc:
            raise F9V2ValidationError("F9-v2 publication lock is missing or unsafe") from exc
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise F9V2ValidationError("F9-v2 publication lock is not private regular data")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_regular(self, target: Path) -> bytes:
        self._prepare_parent(target, create=False)
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise F9V2ValidationError("F9-v2 campaign binding is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size < 1
                or before.st_size > _MAX_ARCHIVE_BYTES
            ):
                raise F9V2ValidationError("F9-v2 campaign binding is not immutable regular data")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise F9V2ValidationError("F9-v2 campaign binding ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1) or _stable_stat_identity(os.fstat(descriptor)) != (
                _stable_stat_identity(before)
            ):
                raise F9V2ValidationError("F9-v2 campaign binding changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _parse_committed(
        payload: bytes,
        *,
        expected_raw_run_sha256: str,
    ) -> CommittedF9V2ValidationCampaign:
        try:
            committed = CommittedF9V2ValidationCampaign.model_validate_json(payload)
        except ValueError as exc:
            raise F9V2ValidationError("F9-v2 archived campaign is invalid") from exc
        if (
            canonical_json_bytes(committed) != payload
            or committed.raw_run_sha256 != expected_raw_run_sha256
        ):
            raise F9V2ValidationError("F9-v2 archived campaign changed canonical identity")
        return committed


ServiceClock = Callable[[], datetime]


class F9V2IndependentValidationService:
    """External validator service implementation; deploy outside the controller worker."""

    def __init__(
        self,
        *,
        archive: WriteOnceF9V2ValidationCampaignArchive,
        assessor: F9V2ObservationAssessmentPort,
        verification: F9V2BridgeVerificationContext,
        validator_private_key: bytes,
        clock: ServiceClock,
    ) -> None:
        if not callable(getattr(assessor, "assess_observation", None)) or not callable(clock):
            raise TypeError("F9-v2 service requires an assessment implementation and clock")
        if archive.validator_authority_pin != verification.validator_authority_pin:
            raise ValueError("F9-v2 service archive differs from bridge validator authority")
        _require_validator_private_key(
            private_key=validator_private_key,
            pin=verification.validator_authority_pin,
        )
        self._archive = archive
        self._assessor = assessor
        self._verification = verification
        self._validator_private_key = validator_private_key
        self._clock = clock

    def prepare_validation_campaign(self, *, raw_run: RawRunEnvelope) -> str | None:
        try:
            raw_run = validate_raw_run_structure(raw_run)
            if raw_run.accepted_terminal_submission.disposition != "process_succeeded":
                return None
            observed_at = self._clock()
            context = self._verification
            raw_run = verify_raw_run_for_independent_validation(
                raw_run=raw_run,
                qualification_authority=context.qualification_authority,
                action_authority=context.action_authority,
                qualification_custody=context.qualification_custody,
                raw_run_custody=context.raw_run_custody,
                execution_authority_pin=context.execution_authority_pin,
                validator_authority_pin=context.validator_authority_pin,
                admission_authority_pin=context.admission_authority_pin,
                observed_at=observed_at,
            )
            existing = self._archive.load_committed_campaign(
                raw_run=raw_run,
                observed_at=observed_at,
            )
            if existing is not None:
                return existing.campaign_sha256
            request = build_f9_v2_validation_request(
                raw_run=raw_run,
                requested_at=observed_at,
            )
            assessed_at = self._clock()
            assessment = F9V2IndependentValidationAssessment.model_validate(
                self._assessor.assess_observation(
                    request=request,
                    raw_run=raw_run,
                    assessed_at=assessed_at,
                ).model_dump(mode="python")
            )
            if assessment.assessed_at != assessed_at:
                raise F9V2ValidationError(
                    "F9-v2 assessor changed the service-owned assessment time"
                )
            campaign = issue_f9_v2_validation_campaign(
                request=request,
                assessment=assessment,
                validator_manifest_sha256=self._archive.validator_manifest_sha256,
                validator_authority_pin=context.validator_authority_pin,
                private_key=self._validator_private_key,
            )
            committed = self._archive.publish_campaign(
                campaign=campaign,
                raw_run=raw_run,
                committed_at=self._clock(),
            )
            return committed.campaign_sha256
        except F9V2ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - external authority boundary fails closed
            raise F9V2ValidationError("F9-v2 independent validation service failed closed") from exc

    def issue_validation_receipt(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
        issuance_challenge: ValidationIssuanceChallenge,
    ) -> ObservationValidationReceipt:
        context = self._verification
        return issue_observation_validation_receipt(
            raw_run=raw_run,
            validation_campaign_sha256=validation_campaign_sha256,
            issuance_challenge=issuance_challenge,
            qualification_authority=context.qualification_authority,
            action_authority=context.action_authority,
            qualification_custody=context.qualification_custody,
            raw_run_custody=context.raw_run_custody,
            validation_campaign_custody=self._archive,
            execution_authority_pin=context.execution_authority_pin,
            validator_authority_pin=context.validator_authority_pin,
            admission_authority_pin=context.admission_authority_pin,
            database_authority_pin=context.database_authority_pin,
            private_key=self._validator_private_key,
        )


def _require_validator_private_key(
    *,
    private_key: bytes,
    pin: ScientificBridgeAuthorityPin,
) -> None:
    if pin.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR:
        raise F9V2ValidationError("F9-v2 campaign signer is not an observation validator")
    try:
        key = Ed25519PrivateKey.from_private_bytes(private_key)
    except (TypeError, ValueError) as exc:
        raise F9V2ValidationError("F9-v2 validator private key is invalid") from exc
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if public.hex() != pin.public_key_ed25519_hex:
        raise F9V2ValidationError("F9-v2 validator private key differs from its deployment pin")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise F9V2ValidationError(f"{label} must be timezone-aware UTC")


def _stable_stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Exclude access time, which a successful read may legitimately update."""

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


__all__ = [
    "CommittedF9V2ValidationCampaign",
    "F9V2BridgeVerificationContext",
    "F9V2IndependentValidationAssessment",
    "F9V2IndependentValidationService",
    "F9V2ObservationAssessmentPort",
    "F9V2ValidationCampaignMessage",
    "F9V2ValidationError",
    "F9V2ValidationRequest",
    "SignedF9V2ValidationCampaign",
    "WriteOnceF9V2ValidationCampaignArchive",
    "build_f9_v2_validation_request",
    "issue_f9_v2_validation_campaign",
    "verify_f9_v2_validation_campaign",
]
