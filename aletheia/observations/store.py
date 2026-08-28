"""Caller-transaction persistence primitives for the scientific-observation bridge.

Every function in this module uses the supplied SQLAlchemy :class:`~sqlalchemy.orm.Session` and
flushes its append-only row, but never commits, rolls back, or closes that session.  This is the
transaction seam used to couple controller delivery, observation admission, and Research Kernel
authority without introducing a second scientific ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aletheia.observations.persistence import (
    ResearchContinuationReceiptRecord,
    ResearchControllerDeliveryAttemptRecord,
    ResearchControllerDeliveryResolutionRecord,
    ResearchControllerDeliveryRecord,
    ResearchControllerRegistrationRecord,
    ResearchObservationAdmissionRecord,
    ResearchObservationIssuanceChallengeRecord,
    ResearchObservationValidationReceiptRecord,
    ResearchProtocolCompilationRecord,
    ResearchScientificExecutionAuthorizationRecord,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_PATTERN = r"^qst_[0-9a-f]{32}$"
_SLOT_PATTERN = r"^sos_[0-9a-f]{32}$"
_REGISTRATION_PATTERN = r"^rcr_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_DATETIME_ADAPTER = TypeAdapter(datetime)


class ObservationPersistenceError(RuntimeError):
    """Base error for a durable observation-store contract failure."""


class ObservationIdentityConflict(ObservationPersistenceError):
    """A stable slot, source, challenge, or content hash was rebound to another value."""


class ObservationPersistenceInvariantError(ObservationPersistenceError):
    """A supposedly exact persisted row is absent or internally inconsistent."""


class _WriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _timestamps_are_utc(self) -> "_WriteModel":
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{name} must be timezone-aware UTC")
        return self


def _model_json(model: Any) -> dict[str, Any]:
    try:
        value = model.model_dump(mode="json", exclude_none=True)
    except AttributeError as exc:  # pragma: no cover - public factories make this programmer error
        raise TypeError("verified contract must be a Pydantic model") from exc
    if not isinstance(value, dict):  # pragma: no cover - Pydantic models always dump mappings
        raise TypeError("verified contract must serialize to a JSON object")
    return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _payload_identity(payload: dict[str, Any], expected: str, *, label: str) -> None:
    if canonical_sha256(payload) != expected:
        raise ValueError(f"{label} hash differs from its canonical JSON")


class ControllerRegistrationWrite(_WriteModel):
    registration_sha256: str = Field(pattern=_SHA256_PATTERN)
    registration_id: str = Field(pattern=_REGISTRATION_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_kind: Literal["research.controller.v1"] = "research.controller.v1"
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    registered_by_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    launch_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    registration_json: dict[str, Any]
    registered_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_is_exact(self) -> "ControllerRegistrationWrite":
        _payload_identity(
            self.registration_json,
            self.registration_sha256,
            label="controller registration",
        )
        return self

    @classmethod
    def from_contract(
        cls,
        registration: Any,
    ) -> "ControllerRegistrationWrite":
        launch_request = registration.launch_request
        payload = _model_json(registration)
        return cls(
            registration_sha256=registration.registration_sha256,
            registration_id=registration.registration_id,
            quest_id=launch_request.quest_id,
            controller_id=registration.controller_id,
            controller_manifest_sha256=registration.controller_manifest_sha256,
            controller_principal_id=registration.controller_principal_id,
            registered_by_principal_id=registration.registered_by_principal_id,
            launch_request_sha256=launch_request.request_sha256,
            registration_json=payload,
            registered_at=registration.registered_at,
        )


class ControllerDeliveryWrite(_WriteModel):
    delivery_sha256: str = Field(pattern=_SHA256_PATTERN)
    registration_sha256: str = Field(pattern=_SHA256_PATTERN)
    registration_id: str = Field(pattern=_REGISTRATION_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    source_kind: Literal["launch", "kernel_outbox", "execution_terminal_outbox"]
    source_key: str = Field(pattern=_IDENTITY_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_stream_version: int | None = Field(default=None, ge=1)
    launch_request_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    execution_id: str | None = Field(default=None, pattern=r"^exe_[0-9a-f]{32}$")
    attempt_id: str | None = Field(default=None, pattern=r"^iat_[0-9a-f]{32}$")
    task_id: str = Field(min_length=1, max_length=96)
    delivery_json: dict[str, Any]
    delivered_at: AwareDatetime

    @model_validator(mode="after")
    def _source_and_payload_are_exact(self) -> "ControllerDeliveryWrite":
        kernel = self.source_kind == "kernel_outbox"
        terminal = self.source_kind == "execution_terminal_outbox"
        launch = self.source_kind == "launch"
        if kernel != (self.source_stream_version is not None):
            raise ValueError("only a Kernel outbox delivery carries a stream version")
        if terminal != (self.execution_id is not None and self.attempt_id is not None):
            raise ValueError("only a terminal-outbox delivery carries execution/attempt identity")
        if launch != (self.launch_request_sha256 is not None):
            raise ValueError("only a launch delivery carries the launch request hash")
        if launch and (
            self.source_key != self.registration_id
            or self.source_sha256 != self.launch_request_sha256
        ):
            raise ValueError("launch delivery differs from its registration request")
        _payload_identity(self.delivery_json, self.delivery_sha256, label="controller delivery")
        return self

    @classmethod
    def from_contract(
        cls,
        *,
        registration_sha256: str,
        wakeup: Any,
        task_id: str,
        delivered_at: datetime,
        execution_id: str | None = None,
        attempt_id: str | None = None,
    ) -> "ControllerDeliveryWrite":
        source_kind = _enum_value(wakeup.source_kind)
        payload = {
            "schema_name": "aletheia.research_controller_delivery",
            "schema_version": 1,
            "registration_sha256": registration_sha256,
            "wakeup": _model_json(wakeup),
            "task_id": task_id,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        return cls(
            delivery_sha256=canonical_sha256(payload),
            registration_sha256=registration_sha256,
            registration_id=wakeup.registration_id,
            quest_id=wakeup.quest_id,
            source_kind=source_kind,
            source_key=wakeup.source_key,
            source_sha256=wakeup.source_sha256,
            source_stream_version=wakeup.source_stream_version,
            launch_request_sha256=(wakeup.source_sha256 if source_kind == "launch" else None),
            execution_id=execution_id,
            attempt_id=attempt_id,
            task_id=task_id,
            delivery_json=payload,
            delivered_at=delivered_at,
        )


class ControllerDeliveryAttemptWrite(_WriteModel):
    attempt_sha256: str = Field(pattern=_SHA256_PATTERN)
    delivery_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    generation: int = Field(ge=0, le=1_024)
    kind: Literal["initial", "failure_redrive", "completed_successor"]
    task_id: str = Field(min_length=1, max_length=96)
    task_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    supersedes_task_id: str | None = Field(default=None, min_length=1, max_length=96)
    predecessor_status: Literal["failed", "succeeded"] | None = None
    predecessor_terminal_category: str | None = Field(default=None, min_length=1, max_length=40)
    predecessor_terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    predecessor_result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    predecessor_tick_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    attempt_json: dict[str, Any]
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_is_exact(self) -> "ControllerDeliveryAttemptWrite":
        _payload_identity(self.attempt_json, self.attempt_sha256, label="controller attempt")
        flattened = {
            "delivery_sha256": self.delivery_sha256,
            "quest_id": self.quest_id,
            "wakeup_sha256": self.wakeup_sha256,
            "controller_manifest_sha256": self.controller_manifest_sha256,
            "generation": self.generation,
            "kind": self.kind,
            "task_id": self.task_id,
            "task_request_sha256": self.task_request_sha256,
            "supersedes_task_id": self.supersedes_task_id,
            "predecessor_status": self.predecessor_status,
            "predecessor_terminal_category": self.predecessor_terminal_category,
            "predecessor_terminal_detail_sha256": self.predecessor_terminal_detail_sha256,
            "predecessor_result_sha256": self.predecessor_result_sha256,
            "predecessor_tick_receipt_sha256": self.predecessor_tick_receipt_sha256,
            "recorded_at": _DATETIME_ADAPTER.dump_python(self.recorded_at, mode="json"),
        }
        if any(self.attempt_json.get(key) != value for key, value in flattened.items()):
            raise ValueError("controller attempt columns differ from canonical JSON")
        return self

    @classmethod
    def from_contract(cls, attempt: Any) -> "ControllerDeliveryAttemptWrite":
        payload = _model_json(attempt)
        return cls(
            attempt_sha256=attempt.attempt_sha256,
            delivery_sha256=attempt.delivery_sha256,
            quest_id=attempt.quest_id,
            wakeup_sha256=attempt.wakeup_sha256,
            controller_manifest_sha256=attempt.controller_manifest_sha256,
            generation=attempt.generation,
            kind=_enum_value(attempt.kind),
            task_id=attempt.task_id,
            task_request_sha256=attempt.task_request_sha256,
            supersedes_task_id=attempt.supersedes_task_id,
            predecessor_status=attempt.predecessor_status,
            predecessor_terminal_category=attempt.predecessor_terminal_category,
            predecessor_terminal_detail_sha256=attempt.predecessor_terminal_detail_sha256,
            predecessor_result_sha256=attempt.predecessor_result_sha256,
            predecessor_tick_receipt_sha256=attempt.predecessor_tick_receipt_sha256,
            attempt_json=payload,
            recorded_at=attempt.recorded_at,
        )


class ControllerDeliveryResolutionWrite(_WriteModel):
    resolution_sha256: str = Field(pattern=_SHA256_PATTERN)
    delivery_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    latest_attempt_sha256: str = Field(pattern=_SHA256_PATTERN)
    exhausted_generation: int = Field(ge=0, le=1_024)
    max_delivery_generation: int = Field(ge=0, le=1_024)
    terminal_task_id: str = Field(min_length=1, max_length=96)
    terminal_task_status: Literal["failed", "succeeded", "cancelled"]
    terminal_category: str = Field(min_length=1, max_length=40)
    terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    terminal_result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tick_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    step_disposition: (
        Literal["completed", "awaiting_authority", "awaiting_external_result", "blocked"] | None
    ) = None
    signed_kernel_command_committed: bool | None = None
    independent_observation_admission_committed: bool | None = None
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal[
        "awaiting_authority",
        "awaiting_external_result",
        "blocked",
        "authoritative_source_committed",
        "dead_letter",
    ]
    dead_letter_reason: (
        Literal["generation_limit_exhausted", "invalid_succeeded_result", "task_cancelled"] | None
    ) = None
    resolution_json: dict[str, Any]
    resolved_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_is_exact(self) -> "ControllerDeliveryResolutionWrite":
        _payload_identity(
            self.resolution_json,
            self.resolution_sha256,
            label="controller delivery resolution",
        )
        flattened = {
            "delivery_sha256": self.delivery_sha256,
            "quest_id": self.quest_id,
            "latest_attempt_sha256": self.latest_attempt_sha256,
            "exhausted_generation": self.exhausted_generation,
            "max_delivery_generation": self.max_delivery_generation,
            "terminal_task_id": self.terminal_task_id,
            "terminal_task_status": self.terminal_task_status,
            "terminal_category": self.terminal_category,
            "terminal_detail_sha256": self.terminal_detail_sha256,
            "terminal_result_sha256": self.terminal_result_sha256,
            "tick_receipt_sha256": self.tick_receipt_sha256,
            "step_disposition": self.step_disposition,
            "signed_kernel_command_committed": self.signed_kernel_command_committed,
            "independent_observation_admission_committed": (
                self.independent_observation_admission_committed
            ),
            "controller_manifest_sha256": self.controller_manifest_sha256,
            "disposition": self.disposition,
            "dead_letter_reason": self.dead_letter_reason,
            "resolved_at": _DATETIME_ADAPTER.dump_python(self.resolved_at, mode="json"),
        }
        if any(self.resolution_json.get(key) != value for key, value in flattened.items()):
            raise ValueError("controller resolution columns differ from canonical JSON")
        return self

    @classmethod
    def from_contract(cls, resolution: Any) -> "ControllerDeliveryResolutionWrite":
        payload = _model_json(resolution)
        return cls(
            resolution_sha256=resolution.resolution_sha256,
            delivery_sha256=resolution.delivery_sha256,
            quest_id=resolution.quest_id,
            latest_attempt_sha256=resolution.latest_attempt_sha256,
            exhausted_generation=resolution.exhausted_generation,
            max_delivery_generation=resolution.max_delivery_generation,
            terminal_task_id=resolution.terminal_task_id,
            terminal_task_status=resolution.terminal_task_status,
            terminal_category=resolution.terminal_category,
            terminal_detail_sha256=resolution.terminal_detail_sha256,
            terminal_result_sha256=resolution.terminal_result_sha256,
            tick_receipt_sha256=resolution.tick_receipt_sha256,
            step_disposition=resolution.step_disposition,
            signed_kernel_command_committed=resolution.signed_kernel_command_committed,
            independent_observation_admission_committed=(
                resolution.independent_observation_admission_committed
            ),
            controller_manifest_sha256=resolution.controller_manifest_sha256,
            disposition=_enum_value(resolution.disposition),
            dead_letter_reason=(
                _enum_value(resolution.dead_letter_reason)
                if resolution.dead_letter_reason is not None
                else None
            ),
            resolution_json=payload,
            resolved_at=resolution.resolved_at,
        )


class ProtocolCompilationWrite(_WriteModel):
    compilation_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_id: str = Field(pattern=r"^[a-z][a-z0-9_.:/-]{1,127}$")
    protocol_version: int = Field(ge=1)
    revision_parent_version: int | None = Field(default=None, ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_json: dict[str, Any]
    result_json: dict[str, Any]
    registered_at: AwareDatetime

    @model_validator(mode="after")
    def _hashes_and_lineage_are_exact(self) -> "ProtocolCompilationWrite":
        if self.protocol_version == 1:
            if self.revision_parent_version is not None or self.revision_parent_sha256 is not None:
                raise ValueError("protocol version 1 cannot name a revision parent")
        elif (
            self.revision_parent_version != self.protocol_version - 1
            or self.revision_parent_sha256 is None
        ):
            raise ValueError("protocol revision must name its immediately preceding version")
        _payload_identity(self.request_json, self.request_sha256, label="compilation request")
        _payload_identity(self.result_json, self.result_sha256, label="compilation result")
        protocol = self.request_json.get("protocol")
        if not isinstance(protocol, dict) or canonical_sha256(protocol) != self.protocol_sha256:
            raise ValueError("compilation request differs from its protocol hash")
        receipt = self.result_json.get("receipt")
        if not isinstance(receipt, dict) or canonical_sha256(receipt) != self.receipt_sha256:
            raise ValueError("compilation result differs from its receipt hash")
        identity = _compilation_identity(
            quest_id=self.quest_id,
            action_sha256=self.action_sha256,
            request_sha256=self.request_sha256,
            result_sha256=self.result_sha256,
            receipt_sha256=self.receipt_sha256,
        )
        if identity != self.compilation_sha256:
            raise ValueError("compilation registration identity differs from its exact inputs")
        return self

    @classmethod
    def from_contract(
        cls,
        *,
        quest_id: str,
        action_sha256: str,
        request: Any,
        result: Any,
        registered_at: datetime,
    ) -> "ProtocolCompilationWrite":
        request_json = _model_json(request)
        result_json = _model_json(result)
        protocol = request.protocol
        request_sha256 = canonical_sha256(request_json)
        result_sha256 = canonical_sha256(result_json)
        receipt_sha256 = result.receipt.receipt_sha256
        return cls(
            compilation_sha256=_compilation_identity(
                quest_id=quest_id,
                action_sha256=action_sha256,
                request_sha256=request_sha256,
                result_sha256=result_sha256,
                receipt_sha256=receipt_sha256,
            ),
            quest_id=quest_id,
            action_sha256=action_sha256,
            protocol_id=protocol.protocol_id,
            protocol_version=protocol.version,
            revision_parent_version=(protocol.version - 1 if protocol.version > 1 else None),
            revision_parent_sha256=protocol.revision_parent_sha256,
            protocol_sha256=protocol.protocol_sha256,
            request_sha256=request_sha256,
            result_sha256=result_sha256,
            receipt_sha256=receipt_sha256,
            request_json=request_json,
            result_json=result_json,
            registered_at=registered_at,
        )


def _compilation_identity(
    *,
    quest_id: str,
    action_sha256: str,
    request_sha256: str,
    result_sha256: str,
    receipt_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema_name": "aletheia.protocol_compilation_registration_identity",
            "schema_version": 1,
            "quest_id": quest_id,
            "action_sha256": action_sha256,
            "request_sha256": request_sha256,
            "result_sha256": result_sha256,
            "receipt_sha256": receipt_sha256,
        }
    )


class ScientificExecutionAuthorizationWrite(_WriteModel):
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^iat_[0-9a-f]{32}$")
    source_event_sequence: int = Field(ge=1)
    source_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_event_type: Literal["action_authorized"] = "action_authorized"
    qualification_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_json: dict[str, Any]
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    observation_admission_deadline: AwareDatetime
    registered_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_and_window_are_exact(self) -> "ScientificExecutionAuthorizationWrite":
        _payload_identity(
            self.authorization_json,
            self.authorization_sha256,
            label="scientific execution authorization",
        )
        if not (
            self.authorized_at <= self.registered_at < self.expires_at
            and self.expires_at < self.observation_admission_deadline
        ):
            raise ValueError("scientific authorization registration is outside its live window")
        return self

    @classmethod
    def from_contract(
        cls, authorization: Any, *, registered_at: datetime
    ) -> "ScientificExecutionAuthorizationWrite":
        message = authorization.message
        binding = message.action_protocol_binding
        source = binding.action_authorized_event
        intent = message.qualification_bundle.intent
        return cls(
            authorization_sha256=authorization.authorization_sha256,
            quest_id=binding.action.quest_id,
            scientific_slot_id=message.scientific_slot_id,
            action_sha256=binding.action.object_sha256,
            execution_id=intent.execution_id,
            attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
            source_event_sequence=source.sequence,
            source_event_sha256=source.event_sha256,
            source_event_type=_enum_value(source.event_type),
            qualification_bundle_sha256=message.qualification_bundle.bundle_sha256,
            qualification_grant_sha256=message.qualification_grant.grant_sha256,
            authorization_json=_model_json(authorization),
            authorized_at=message.authorized_at,
            expires_at=message.expires_at,
            observation_admission_deadline=message.observation_admission_deadline,
            registered_at=registered_at,
        )


class ObservationIssuanceChallengeWrite(_WriteModel):
    challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    purpose: Literal["validation", "admission"]
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    nonce_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_scope: str = Field(pattern=_IDENTITY_PATTERN)
    raw_run_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    validation_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    database_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_by_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    issuance_key_id: str = Field(pattern=_SHA256_PATTERN)
    challenge_json: dict[str, Any]
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    observation_admission_deadline: AwareDatetime
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_source_and_window_are_exact(self) -> "ObservationIssuanceChallengeWrite":
        validation = self.purpose == "validation"
        if validation != (self.raw_run_sha256 is not None):
            raise ValueError("validation challenge must name exactly one raw run")
        if validation:
            if (
                self.committed_validation_receipt_sha256 is not None
                or self.validation_receipt_sha256 is not None
            ):
                raise ValueError("validation challenge cannot name a validation receipt")
        elif (
            self.committed_validation_receipt_sha256 is None
            or self.validation_receipt_sha256 is None
        ):
            raise ValueError("admission challenge must name both validation receipt identities")
        _payload_identity(self.challenge_json, self.challenge_sha256, label="issuance challenge")
        if not (
            self.issued_at <= self.recorded_at < self.expires_at
            and self.expires_at <= self.observation_admission_deadline
        ):
            raise ValueError("issuance challenge registration is outside its live window")
        return self

    @classmethod
    def from_contract(
        cls,
        challenge: Any,
        *,
        quest_id: str,
        authorization_sha256: str,
        recorded_at: datetime,
    ) -> "ObservationIssuanceChallengeWrite":
        message = challenge.message
        validation = message.purpose == "issue_observation_validation_receipt"
        return cls(
            challenge_sha256=challenge.challenge_sha256,
            purpose="validation" if validation else "admission",
            quest_id=quest_id,
            scientific_slot_id=message.scientific_slot_id,
            authorization_sha256=authorization_sha256,
            nonce_sha256=message.nonce_sha256,
            row_scope=message.row_scope,
            raw_run_sha256=getattr(message, "raw_run_sha256", None),
            committed_validation_receipt_sha256=getattr(
                message, "committed_validation_receipt_sha256", None
            ),
            validation_receipt_sha256=getattr(message, "validation_receipt_sha256", None),
            database_authority_policy_sha256=message.database_authority_policy_sha256,
            issued_by_principal_id=message.issued_by_principal_id,
            issuance_key_id=message.issuance_key_id,
            challenge_json=_model_json(challenge),
            issued_at=message.issued_at,
            expires_at=message.expires_at,
            observation_admission_deadline=message.observation_admission_deadline,
            recorded_at=recorded_at,
        )


class ObservationValidationReceiptWrite(_WriteModel):
    committed_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    issuance_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    disposition: Literal["validated_confirmation", "rejected_scientific", "blocked_execution"]
    outcome: Literal["positive", "negative", "inconclusive"] | None = None
    scientific_observation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    committed_receipt_json: dict[str, Any]
    validated_at: AwareDatetime
    registered_at: AwareDatetime
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_outcome_and_time_are_exact(self) -> "ObservationValidationReceiptWrite":
        confirmed = self.disposition == "validated_confirmation"
        if confirmed != (
            self.outcome is not None and self.scientific_observation_sha256 is not None
        ):
            raise ValueError("only a validated confirmation carries a scientific observation")
        _payload_identity(
            self.committed_receipt_json,
            self.committed_receipt_sha256,
            label="committed validation receipt",
        )
        if not self.validated_at <= self.registered_at <= self.committed_at:
            raise ValueError("validation receipt database times are out of order")
        return self

    @classmethod
    def from_contract(
        cls, committed_receipt: Any, *, quest_id: str
    ) -> "ObservationValidationReceiptWrite":
        commit = committed_receipt.message
        receipt = commit.receipt
        message = receipt.message
        raw_run = message.raw_run
        authorization = raw_run.scientific_authorization
        projection = message.validation_campaign_projection
        return cls(
            committed_receipt_sha256=committed_receipt.committed_receipt_sha256,
            validation_receipt_sha256=receipt.receipt_sha256,
            quest_id=quest_id,
            scientific_slot_id=message.scientific_slot_id,
            authorization_sha256=authorization.authorization_sha256,
            qualification_admission_sha256=raw_run.qualification_admission_sha256,
            raw_run_sha256=raw_run.raw_run_sha256,
            issuance_challenge_sha256=commit.issuance_challenge_sha256,
            validation_campaign_sha256=(projection.campaign_sha256 if projection else None),
            disposition=_enum_value(message.disposition),
            outcome=_enum_value(message.outcome) if message.outcome is not None else None,
            scientific_observation_sha256=message.scientific_observation_sha256,
            committed_receipt_json=_model_json(committed_receipt),
            validated_at=message.validated_at,
            registered_at=commit.registered_at,
            committed_at=commit.committed_at,
        )


class ObservationAdmissionWrite(_WriteModel):
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    issuance_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal["admitted"]
    admitted_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    admission_json: dict[str, Any]
    registered_at: AwareDatetime
    committed_at: AwareDatetime
    incorporated_event_sequence: int | None = Field(default=None, ge=1)
    incorporated_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    incorporated_event_type: Literal["observation_incorporated"] | None = None

    @model_validator(mode="after")
    def _payload_and_incorporation_are_exact(self) -> "ObservationAdmissionWrite":
        event_complete = (
            self.incorporated_event_sequence is not None
            and self.incorporated_event_sha256 is not None
            and self.incorporated_event_type == "observation_incorporated"
        )
        if not event_complete:
            raise ValueError("admitted observation requires its exact incorporation event")
        _payload_identity(
            self.admission_json,
            self.committed_admission_sha256,
            label="committed observation admission",
        )
        if self.registered_at > self.committed_at:
            raise ValueError("observation admission database times are out of order")
        return self

    @classmethod
    def from_contract(
        cls,
        admission: Any,
        *,
        quest_id: str,
        incorporated_event_sequence: int | None = None,
        incorporated_event_sha256: str | None = None,
        incorporated_event_type: str | None = None,
    ) -> "ObservationAdmissionWrite":
        commit = admission.message
        decision = commit.decision
        decision_message = decision.message
        validation = decision_message.committed_validation_receipt
        validation_message = validation.message.receipt.message
        authorization = validation_message.raw_run.scientific_authorization
        return cls(
            committed_admission_sha256=admission.committed_admission_sha256,
            decision_sha256=decision.decision_sha256,
            quest_id=quest_id,
            scientific_slot_id=decision_message.scientific_slot_id,
            authorization_sha256=authorization.authorization_sha256,
            committed_validation_receipt_sha256=(validation.committed_receipt_sha256),
            validation_receipt_sha256=validation.message.validation_receipt_sha256,
            issuance_challenge_sha256=commit.issuance_challenge_sha256,
            disposition=_enum_value(decision_message.disposition),
            admitted_observation_sha256=decision_message.admitted_observation_sha256,
            admission_json=_model_json(admission),
            registered_at=commit.registered_at,
            committed_at=commit.committed_at,
            incorporated_event_sequence=incorporated_event_sequence,
            incorporated_event_sha256=incorporated_event_sha256,
            incorporated_event_type=incorporated_event_type,
        )


class ContinuationReceiptWrite(_WriteModel):
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal["ready", "redesign_observable", "hypothesis_set_fork_required"]
    receipt_json: dict[str, Any]
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _payload_is_exact(self) -> "ContinuationReceiptWrite":
        _payload_identity(self.receipt_json, self.receipt_sha256, label="continuation receipt")
        return self

    @classmethod
    def from_contract(
        cls,
        receipt: Any,
        *,
        quest_id: str,
        action_sha256: str,
        observation: Any,
        recorded_at: datetime,
    ) -> "ContinuationReceiptWrite":
        if receipt.scientific_slot_id != observation.scientific_slot_id:
            raise ValueError("continuation receipt changed the admitted scientific slot")
        if receipt.observation_projection_sha256 != observation.projection_sha256:
            raise ValueError("continuation receipt changed its observation projection")
        if receipt.world_model_snapshot_sha256 != observation.source_world_model_sha256:
            raise ValueError("continuation receipt changed the admitted source world model")
        return cls(
            receipt_sha256=receipt.receipt_sha256,
            quest_id=quest_id,
            action_sha256=action_sha256,
            scientific_slot_id=receipt.scientific_slot_id,
            world_model_snapshot_sha256=receipt.world_model_snapshot_sha256,
            observation_projection_sha256=receipt.observation_projection_sha256,
            scientific_observation_sha256=observation.scientific_observation_sha256,
            committed_admission_sha256=observation.committed_admission_sha256,
            disposition=_enum_value(receipt.disposition),
            receipt_json=_model_json(receipt),
            recorded_at=recorded_at,
        )


@dataclass(frozen=True)
class AppendReceipt:
    """Result of a new append or an exact replay of the same immutable request."""

    identity_sha256: str
    created: bool


_RecordT = TypeVar("_RecordT")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(actual, datetime) and isinstance(expected, datetime):
        return _utc(actual) == _utc(expected)
    return actual == expected


def _row_is_exact(row: Any, values: dict[str, Any], *, ignored: frozenset[str]) -> bool:
    return all(
        key in ignored or _equivalent(getattr(row, key), expected)
        for key, expected in values.items()
    )


def _append_exact(
    session: Session,
    *,
    record_type: type[_RecordT],
    values: dict[str, Any],
    identity_sha256: str,
    collision_keys: tuple[tuple[str, ...], ...],
    replay_ignored_fields: frozenset[str] = frozenset(),
) -> AppendReceipt:
    """Append once, replay exact content, and reject every unique-key variant."""

    conditions = tuple(
        and_(*(getattr(record_type, key) == values[key] for key in keys))
        for keys in collision_keys
        if all(values[key] is not None for key in keys)
    )
    existing_rows = list(session.scalars(select(record_type).where(or_(*conditions))).all())
    if existing_rows:
        if len(existing_rows) == 1 and _row_is_exact(
            existing_rows[0], values, ignored=replay_ignored_fields
        ):
            return AppendReceipt(identity_sha256=identity_sha256, created=False)
        raise ObservationIdentityConflict(
            f"{record_type.__tablename__} stable identity is already bound to another variant"
        )

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(record_type).values(**values).on_conflict_do_nothing()
    elif dialect_name == "sqlite":
        statement = sqlite_insert(record_type).values(**values).on_conflict_do_nothing()
    else:  # pragma: no cover - production and contract-test dialects are deliberately explicit
        raise ObservationPersistenceError(
            f"unsupported observation persistence dialect: {dialect_name}"
        )
    result = session.execute(statement)
    session.flush()

    rows = list(session.scalars(select(record_type).where(or_(*conditions))).all())
    if len(rows) != 1:
        raise ObservationPersistenceInvariantError(
            f"{record_type.__tablename__} append did not resolve to one durable identity"
        )
    if not _row_is_exact(rows[0], values, ignored=replay_ignored_fields):
        raise ObservationIdentityConflict(
            f"{record_type.__tablename__} concurrent append committed another variant"
        )
    return AppendReceipt(identity_sha256=identity_sha256, created=result.rowcount == 1)


_WriteT = TypeVar("_WriteT", bound=_WriteModel)


def _write_projection(model_type: type[_WriteT], row: Any | None) -> _WriteT | None:
    if row is None:
        return None
    values: dict[str, Any] = {}
    for name in model_type.model_fields:
        value = getattr(row, name)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        values[name] = value
    return model_type.model_validate(values)


def get_controller_registration_by_quest(
    session: Session, quest_id: str
) -> ControllerRegistrationWrite | None:
    row = session.scalar(
        select(ResearchControllerRegistrationRecord).where(
            ResearchControllerRegistrationRecord.quest_id == quest_id
        )
    )
    return _write_projection(ControllerRegistrationWrite, row)


def get_controller_registration_by_launch_request(
    session: Session, launch_request_sha256: str
) -> ControllerRegistrationWrite | None:
    row = session.scalar(
        select(ResearchControllerRegistrationRecord).where(
            ResearchControllerRegistrationRecord.launch_request_sha256 == launch_request_sha256
        )
    )
    return _write_projection(ControllerRegistrationWrite, row)


def list_controller_registrations(session: Session) -> tuple[ControllerRegistrationWrite, ...]:
    rows = session.scalars(
        select(ResearchControllerRegistrationRecord).order_by(
            ResearchControllerRegistrationRecord.registered_at,
            ResearchControllerRegistrationRecord.registration_sha256,
        )
    ).all()
    return tuple(
        item
        for row in rows
        if (item := _write_projection(ControllerRegistrationWrite, row)) is not None
    )


def get_controller_delivery_by_source(
    session: Session, *, source_kind: str, source_key: str
) -> ControllerDeliveryWrite | None:
    row = session.scalar(
        select(ResearchControllerDeliveryRecord).where(
            ResearchControllerDeliveryRecord.source_kind == source_kind,
            ResearchControllerDeliveryRecord.source_key == source_key,
        )
    )
    return _write_projection(ControllerDeliveryWrite, row)


def get_controller_delivery_by_sha256(
    session: Session,
    delivery_sha256: str,
    *,
    lock_for_update: bool = False,
) -> ControllerDeliveryWrite | None:
    statement = select(ResearchControllerDeliveryRecord).where(
        ResearchControllerDeliveryRecord.delivery_sha256 == delivery_sha256
    )
    if lock_for_update:
        statement = statement.with_for_update()
    return _write_projection(ControllerDeliveryWrite, session.scalar(statement))


def list_controller_deliveries(
    session: Session, *, registration_id: str | None = None
) -> tuple[ControllerDeliveryWrite, ...]:
    statement = select(ResearchControllerDeliveryRecord)
    if registration_id is not None:
        statement = statement.where(
            ResearchControllerDeliveryRecord.registration_id == registration_id
        )
    rows = session.scalars(
        statement.order_by(
            ResearchControllerDeliveryRecord.delivered_at,
            ResearchControllerDeliveryRecord.delivery_sha256,
        )
    ).all()
    return tuple(
        item
        for row in rows
        if (item := _write_projection(ControllerDeliveryWrite, row)) is not None
    )


def list_controller_delivery_attempts(
    session: Session, *, delivery_sha256: str
) -> tuple[ControllerDeliveryAttemptWrite, ...]:
    rows = session.scalars(
        select(ResearchControllerDeliveryAttemptRecord)
        .where(ResearchControllerDeliveryAttemptRecord.delivery_sha256 == delivery_sha256)
        .order_by(ResearchControllerDeliveryAttemptRecord.generation)
    ).all()
    return tuple(
        item
        for row in rows
        if (item := _write_projection(ControllerDeliveryAttemptWrite, row)) is not None
    )


def get_controller_delivery_resolution(
    session: Session, *, delivery_sha256: str
) -> ControllerDeliveryResolutionWrite | None:
    row = session.scalar(
        select(ResearchControllerDeliveryResolutionRecord).where(
            ResearchControllerDeliveryResolutionRecord.delivery_sha256 == delivery_sha256
        )
    )
    return _write_projection(ControllerDeliveryResolutionWrite, row)


def list_controller_delivery_resolutions(
    session: Session, *, quest_id: str | None = None
) -> tuple[ControllerDeliveryResolutionWrite, ...]:
    statement = select(ResearchControllerDeliveryResolutionRecord)
    if quest_id is not None:
        statement = statement.where(ResearchControllerDeliveryResolutionRecord.quest_id == quest_id)
    rows = session.scalars(
        statement.order_by(
            ResearchControllerDeliveryResolutionRecord.resolved_at,
            ResearchControllerDeliveryResolutionRecord.resolution_sha256,
        )
    ).all()
    return tuple(
        item
        for row in rows
        if (item := _write_projection(ControllerDeliveryResolutionWrite, row)) is not None
    )


def get_protocol_compilation_by_action(
    session: Session, *, quest_id: str, action_sha256: str
) -> ProtocolCompilationWrite | None:
    row = session.scalar(
        select(ResearchProtocolCompilationRecord).where(
            ResearchProtocolCompilationRecord.quest_id == quest_id,
            ResearchProtocolCompilationRecord.action_sha256 == action_sha256,
        )
    )
    return _write_projection(ProtocolCompilationWrite, row)


def get_protocol_compilation_by_protocol_version(
    session: Session,
    *,
    quest_id: str,
    protocol_id: str,
    protocol_version: int,
) -> ProtocolCompilationWrite | None:
    """Resolve one immutable protocol version for contiguous revision verification."""

    row = session.scalar(
        select(ResearchProtocolCompilationRecord).where(
            ResearchProtocolCompilationRecord.quest_id == quest_id,
            ResearchProtocolCompilationRecord.protocol_id == protocol_id,
            ResearchProtocolCompilationRecord.protocol_version == protocol_version,
        )
    )
    return _write_projection(ProtocolCompilationWrite, row)


def get_scientific_execution_authorization_by_slot(
    session: Session, *, quest_id: str, scientific_slot_id: str
) -> ScientificExecutionAuthorizationWrite | None:
    row = session.scalar(
        select(ResearchScientificExecutionAuthorizationRecord).where(
            ResearchScientificExecutionAuthorizationRecord.quest_id == quest_id,
            ResearchScientificExecutionAuthorizationRecord.scientific_slot_id == scientific_slot_id,
        )
    )
    return _write_projection(ScientificExecutionAuthorizationWrite, row)


def get_scientific_execution_authorization_by_attempt(
    session: Session, *, execution_id: str, attempt_id: str
) -> ScientificExecutionAuthorizationWrite | None:
    row = session.scalar(
        select(ResearchScientificExecutionAuthorizationRecord).where(
            ResearchScientificExecutionAuthorizationRecord.execution_id == execution_id,
            ResearchScientificExecutionAuthorizationRecord.attempt_id == attempt_id,
        )
    )
    return _write_projection(ScientificExecutionAuthorizationWrite, row)


def list_scientific_execution_authorizations(
    session: Session, *, quest_id: str | None = None
) -> tuple[ScientificExecutionAuthorizationWrite, ...]:
    """Return canonical SEA registrations for terminal-outbox reconciliation."""

    statement = select(ResearchScientificExecutionAuthorizationRecord)
    if quest_id is not None:
        statement = statement.where(
            ResearchScientificExecutionAuthorizationRecord.quest_id == quest_id
        )
    rows = session.scalars(
        statement.order_by(
            ResearchScientificExecutionAuthorizationRecord.quest_id,
            ResearchScientificExecutionAuthorizationRecord.execution_id,
            ResearchScientificExecutionAuthorizationRecord.attempt_id,
        )
    ).all()
    return tuple(
        item
        for row in rows
        if (item := _write_projection(ScientificExecutionAuthorizationWrite, row)) is not None
    )


def get_live_observation_issuance_challenge(
    session: Session,
    *,
    purpose: Literal["validation", "admission"],
    quest_id: str,
    scientific_slot_id: str,
    authorization_sha256: str,
    observed_at: datetime,
) -> ObservationIssuanceChallengeWrite | None:
    """Return the sole live challenge for one exact slot/purpose, if present."""

    rows = session.scalars(
        select(ResearchObservationIssuanceChallengeRecord).where(
            ResearchObservationIssuanceChallengeRecord.purpose == purpose,
            ResearchObservationIssuanceChallengeRecord.quest_id == quest_id,
            ResearchObservationIssuanceChallengeRecord.scientific_slot_id == scientific_slot_id,
            ResearchObservationIssuanceChallengeRecord.authorization_sha256 == authorization_sha256,
            ResearchObservationIssuanceChallengeRecord.expires_at > observed_at,
        )
    ).all()
    if len(rows) > 1:
        raise ObservationPersistenceInvariantError(
            "scientific slot has multiple live issuance challenges for one purpose"
        )
    return _write_projection(ObservationIssuanceChallengeWrite, rows[0] if rows else None)


def get_observation_issuance_challenge_by_sha256(
    session: Session,
    *,
    challenge_sha256: str,
) -> ObservationIssuanceChallengeWrite | None:
    """Load one exact immutable challenge, including expired historical rows."""

    row = session.get(ResearchObservationIssuanceChallengeRecord, challenge_sha256)
    return _write_projection(ObservationIssuanceChallengeWrite, row)


def lock_scientific_execution_authorization_by_slot(
    session: Session,
    *,
    quest_id: str,
    scientific_slot_id: str,
) -> ScientificExecutionAuthorizationWrite | None:
    """Serialize all DB-attestation operations for one scientific slot."""

    row = session.scalar(
        select(ResearchScientificExecutionAuthorizationRecord)
        .where(
            ResearchScientificExecutionAuthorizationRecord.quest_id == quest_id,
            ResearchScientificExecutionAuthorizationRecord.scientific_slot_id == scientific_slot_id,
        )
        .with_for_update()
    )
    return _write_projection(ScientificExecutionAuthorizationWrite, row)


def get_observation_validation_receipt_by_slot(
    session: Session, *, quest_id: str, scientific_slot_id: str
) -> ObservationValidationReceiptWrite | None:
    row = session.scalar(
        select(ResearchObservationValidationReceiptRecord).where(
            ResearchObservationValidationReceiptRecord.quest_id == quest_id,
            ResearchObservationValidationReceiptRecord.scientific_slot_id == scientific_slot_id,
        )
    )
    return _write_projection(ObservationValidationReceiptWrite, row)


def get_observation_admission_by_slot(
    session: Session, *, quest_id: str, scientific_slot_id: str
) -> ObservationAdmissionWrite | None:
    row = session.scalar(
        select(ResearchObservationAdmissionRecord).where(
            ResearchObservationAdmissionRecord.quest_id == quest_id,
            ResearchObservationAdmissionRecord.scientific_slot_id == scientific_slot_id,
        )
    )
    return _write_projection(ObservationAdmissionWrite, row)


def get_observation_admission_by_decision(
    session: Session, *, decision_sha256: str
) -> ObservationAdmissionWrite | None:
    row = session.scalar(
        select(ResearchObservationAdmissionRecord).where(
            ResearchObservationAdmissionRecord.decision_sha256 == decision_sha256
        )
    )
    return _write_projection(ObservationAdmissionWrite, row)


def get_continuation_receipt_by_slot(
    session: Session, *, quest_id: str, scientific_slot_id: str
) -> ContinuationReceiptWrite | None:
    row = session.scalar(
        select(ResearchContinuationReceiptRecord).where(
            ResearchContinuationReceiptRecord.quest_id == quest_id,
            ResearchContinuationReceiptRecord.scientific_slot_id == scientific_slot_id,
        )
    )
    return _write_projection(ContinuationReceiptWrite, row)


def register_controller(session: Session, write: ControllerRegistrationWrite) -> AppendReceipt:
    values = write.model_dump(mode="python")
    return _append_exact(
        session,
        record_type=ResearchControllerRegistrationRecord,
        values=values,
        identity_sha256=write.registration_sha256,
        collision_keys=(
            ("registration_sha256",),
            ("registration_id",),
            ("quest_id",),
            ("launch_request_sha256",),
        ),
    )


def _lock_controller_delivery(
    session: Session,
    *,
    delivery_sha256: str,
    quest_id: str,
) -> ResearchControllerDeliveryRecord:
    delivery = session.scalar(
        select(ResearchControllerDeliveryRecord)
        .where(
            ResearchControllerDeliveryRecord.delivery_sha256 == delivery_sha256,
            ResearchControllerDeliveryRecord.quest_id == quest_id,
        )
        .with_for_update()
    )
    if delivery is None:
        raise ObservationPersistenceInvariantError(
            "controller generation state requires its exact locked delivery"
        )
    return delivery


def record_controller_delivery(session: Session, write: ControllerDeliveryWrite) -> AppendReceipt:
    values = write.model_dump(mode="python")
    return _append_exact(
        session,
        record_type=ResearchControllerDeliveryRecord,
        values=values,
        identity_sha256=write.delivery_sha256,
        collision_keys=(
            ("delivery_sha256",),
            ("source_kind", "source_key"),
            ("source_kind", "source_sha256"),
            ("task_id",),
        ),
    )


def record_controller_delivery_attempt(
    session: Session, write: ControllerDeliveryAttemptWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    _lock_controller_delivery(
        session,
        delivery_sha256=write.delivery_sha256,
        quest_id=write.quest_id,
    )
    resolution = session.scalar(
        select(ResearchControllerDeliveryResolutionRecord).where(
            ResearchControllerDeliveryResolutionRecord.delivery_sha256 == write.delivery_sha256
        )
    )
    if resolution is not None:
        existing = session.scalar(
            select(ResearchControllerDeliveryAttemptRecord).where(
                ResearchControllerDeliveryAttemptRecord.attempt_sha256 == write.attempt_sha256
            )
        )
        if existing is None or not _row_is_exact(existing, values, ignored=frozenset()):
            raise ObservationIdentityConflict(
                "resolved controller delivery cannot append another attempt"
            )
    return _append_exact(
        session,
        record_type=ResearchControllerDeliveryAttemptRecord,
        values=values,
        identity_sha256=write.attempt_sha256,
        collision_keys=(
            ("attempt_sha256",),
            ("delivery_sha256", "generation"),
            ("task_id",),
            ("supersedes_task_id",),
        ),
    )


def record_controller_delivery_resolution(
    session: Session, write: ControllerDeliveryResolutionWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    _lock_controller_delivery(
        session,
        delivery_sha256=write.delivery_sha256,
        quest_id=write.quest_id,
    )
    latest = session.scalar(
        select(ResearchControllerDeliveryAttemptRecord)
        .where(ResearchControllerDeliveryAttemptRecord.delivery_sha256 == write.delivery_sha256)
        .order_by(ResearchControllerDeliveryAttemptRecord.generation.desc())
        .limit(1)
    )
    if latest is None or (
        latest.attempt_sha256,
        latest.delivery_sha256,
        latest.generation,
        latest.task_id,
    ) != (
        write.latest_attempt_sha256,
        write.delivery_sha256,
        write.exhausted_generation,
        write.terminal_task_id,
    ):
        raise ObservationIdentityConflict(
            "controller resolution must target the latest exact delivery attempt"
        )
    return _append_exact(
        session,
        record_type=ResearchControllerDeliveryResolutionRecord,
        values=values,
        identity_sha256=write.resolution_sha256,
        collision_keys=(
            ("resolution_sha256",),
            ("delivery_sha256",),
            ("terminal_task_id",),
        ),
    )


def register_protocol_compilation(
    session: Session, write: ProtocolCompilationWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    return _append_exact(
        session,
        record_type=ResearchProtocolCompilationRecord,
        values=values,
        identity_sha256=write.compilation_sha256,
        collision_keys=(
            ("compilation_sha256",),
            ("action_sha256",),
            ("request_sha256",),
            ("result_sha256",),
            ("receipt_sha256",),
            ("quest_id", "protocol_id", "protocol_version"),
        ),
    )


def register_scientific_execution_authorization(
    session: Session, write: ScientificExecutionAuthorizationWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    return _append_exact(
        session,
        record_type=ResearchScientificExecutionAuthorizationRecord,
        values=values,
        identity_sha256=write.authorization_sha256,
        collision_keys=(
            ("authorization_sha256",),
            ("scientific_slot_id",),
            ("execution_id",),
            ("attempt_id",),
            ("qualification_bundle_sha256",),
            ("qualification_grant_sha256",),
        ),
    )


def record_observation_issuance_challenge(
    session: Session, write: ObservationIssuanceChallengeWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")

    # Challenge rows are immutable, so a permanent UNIQUE(row_scope) would also make expiry
    # permanent.  Serialize issuance on the stable authorization row instead, then reject a
    # second live challenge for the same purpose/row scope.  Once the prior window has expired a
    # fresh nonce and content identity can be appended without rewriting history.
    authorization = session.scalar(
        select(ResearchScientificExecutionAuthorizationRecord)
        .where(
            ResearchScientificExecutionAuthorizationRecord.authorization_sha256
            == write.authorization_sha256,
            ResearchScientificExecutionAuthorizationRecord.quest_id == write.quest_id,
            ResearchScientificExecutionAuthorizationRecord.scientific_slot_id
            == write.scientific_slot_id,
        )
        .with_for_update()
    )
    if authorization is None:
        raise ObservationPersistenceInvariantError(
            "issuance challenge requires its exact scientific authorization"
        )
    live_rows = list(
        session.scalars(
            select(ResearchObservationIssuanceChallengeRecord).where(
                ResearchObservationIssuanceChallengeRecord.purpose == write.purpose,
                ResearchObservationIssuanceChallengeRecord.quest_id == write.quest_id,
                ResearchObservationIssuanceChallengeRecord.scientific_slot_id
                == write.scientific_slot_id,
                ResearchObservationIssuanceChallengeRecord.authorization_sha256
                == write.authorization_sha256,
                ResearchObservationIssuanceChallengeRecord.row_scope == write.row_scope,
                ResearchObservationIssuanceChallengeRecord.expires_at > write.recorded_at,
            )
        ).all()
    )
    if live_rows and not (
        len(live_rows) == 1 and _row_is_exact(live_rows[0], values, ignored=frozenset())
    ):
        raise ObservationIdentityConflict(
            "issuance challenge purpose and row scope already have a live variant"
        )
    return _append_exact(
        session,
        record_type=ResearchObservationIssuanceChallengeRecord,
        values=values,
        identity_sha256=write.challenge_sha256,
        collision_keys=(("challenge_sha256",), ("nonce_sha256",)),
    )


def record_observation_validation_receipt(
    session: Session, write: ObservationValidationReceiptWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    collision_keys: list[tuple[str, ...]] = [
        ("committed_receipt_sha256",),
        ("validation_receipt_sha256",),
        ("scientific_slot_id",),
        ("raw_run_sha256",),
        ("issuance_challenge_sha256",),
    ]
    if write.scientific_observation_sha256 is not None:
        collision_keys.append(("scientific_observation_sha256",))
    return _append_exact(
        session,
        record_type=ResearchObservationValidationReceiptRecord,
        values=values,
        identity_sha256=write.committed_receipt_sha256,
        collision_keys=tuple(collision_keys),
    )


def record_observation_admission(
    session: Session, write: ObservationAdmissionWrite
) -> AppendReceipt:
    values = write.model_dump(mode="python")
    collision_keys: list[tuple[str, ...]] = [
        ("committed_admission_sha256",),
        ("decision_sha256",),
        ("scientific_slot_id",),
        ("committed_validation_receipt_sha256",),
        ("issuance_challenge_sha256",),
    ]
    for key in ("admitted_observation_sha256", "incorporated_event_sha256"):
        if values[key] is not None:
            collision_keys.append((key,))
    return _append_exact(
        session,
        record_type=ResearchObservationAdmissionRecord,
        values=values,
        identity_sha256=write.committed_admission_sha256,
        collision_keys=tuple(collision_keys),
    )


def record_continuation_receipt(session: Session, write: ContinuationReceiptWrite) -> AppendReceipt:
    values = write.model_dump(mode="python")
    return _append_exact(
        session,
        record_type=ResearchContinuationReceiptRecord,
        values=values,
        identity_sha256=write.receipt_sha256,
        collision_keys=(
            ("receipt_sha256",),
            ("scientific_slot_id",),
            ("action_sha256",),
            ("observation_projection_sha256",),
        ),
    )


__all__ = [
    "AppendReceipt",
    "ContinuationReceiptWrite",
    "ControllerDeliveryAttemptWrite",
    "ControllerDeliveryResolutionWrite",
    "ControllerDeliveryWrite",
    "ControllerRegistrationWrite",
    "ObservationAdmissionWrite",
    "ObservationIdentityConflict",
    "ObservationIssuanceChallengeWrite",
    "ObservationPersistenceError",
    "ObservationPersistenceInvariantError",
    "ObservationValidationReceiptWrite",
    "ProtocolCompilationWrite",
    "ScientificExecutionAuthorizationWrite",
    "get_continuation_receipt_by_slot",
    "get_controller_delivery_by_source",
    "get_controller_delivery_by_sha256",
    "get_controller_delivery_resolution",
    "get_controller_registration_by_launch_request",
    "get_controller_registration_by_quest",
    "get_live_observation_issuance_challenge",
    "get_observation_issuance_challenge_by_sha256",
    "get_observation_admission_by_slot",
    "get_observation_admission_by_decision",
    "get_observation_validation_receipt_by_slot",
    "get_protocol_compilation_by_action",
    "get_protocol_compilation_by_protocol_version",
    "get_scientific_execution_authorization_by_slot",
    "lock_scientific_execution_authorization_by_slot",
    "get_scientific_execution_authorization_by_attempt",
    "list_controller_deliveries",
    "list_controller_delivery_attempts",
    "list_controller_delivery_resolutions",
    "list_controller_registrations",
    "list_scientific_execution_authorizations",
    "record_continuation_receipt",
    "record_controller_delivery",
    "record_controller_delivery_attempt",
    "record_controller_delivery_resolution",
    "record_observation_admission",
    "record_observation_issuance_challenge",
    "record_observation_validation_receipt",
    "register_controller",
    "register_protocol_compilation",
    "register_scientific_execution_authorization",
]
