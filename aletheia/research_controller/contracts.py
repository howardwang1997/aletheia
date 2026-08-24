"""Closed contracts for restart-safe Research Kernel controller delivery.

These values are operational projections, not a second scientific ledger.  A controller task may
audit state, reconcile receipts, and propose the next signed action.  It cannot directly authorize
an action, admit an observation, or mutate a Kernel stream.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.durable_tasks.contracts import RetryPolicy, TaskSpec
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

CONTROLLER_TASK_TYPE = "research.controller.v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^rctl_[0-9a-f]{32}$"
_REGISTRATION_ID_PATTERN = r"^rcr_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$"


class ControllerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _timestamps_are_utc(self) -> "ControllerModel":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


class ResearchControllerManifest(ControllerModel):
    """Deployment-pinned controller identity; callers cannot select these policies."""

    schema_name: Literal["aletheia.research_controller_manifest"] = (
        "aletheia.research_controller_manifest"
    )
    schema_version: Literal[1] = 1
    controller_id: str | None = Field(default=None, pattern=_CONTROLLER_ID_PATTERN)
    controller_key: str = Field(pattern=_IDENTITY_PATTERN)
    controller_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_registry_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_bridge_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    worker_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    retry_policy: RetryPolicy
    max_delivery_generation: int = Field(default=8, ge=0, le=1_024)
    prepared_at: AwareDatetime
    ticks_per_task: Literal[1] = 1
    legacy_optimize_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _identity_is_derived_and_policies_are_separate(self) -> "ResearchControllerManifest":
        policies = (
            self.controller_policy_sha256,
            self.capability_catalog_sha256,
            self.protocol_registry_policy_sha256,
            self.scientific_bridge_policy_sha256,
        )
        if len(set(policies)) != len(policies):
            raise ValueError("controller, capability, protocol, and bridge policies must differ")
        expected = f"rctl_{self.manifest_sha256[:32]}"
        if self.controller_id is not None and self.controller_id != expected:
            raise ValueError("controller id differs from its manifest")
        object.__setattr__(self, "controller_id", expected)
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"controller_id"}))


class ResearchControllerLaunchRequest(ControllerModel):
    """Transport request to subscribe one existing Quest to the pinned controller."""

    schema_name: Literal["aletheia.research_controller_launch_request"] = (
        "aletheia.research_controller_launch_request"
    )
    schema_version: Literal[1] = 1
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    expected_stream_version: int = Field(ge=1)
    expected_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def registration_id(self) -> str:
        digest = canonical_sha256(
            {
                "schema_name": "aletheia.research_controller_registration_identity",
                "schema_version": 1,
                "quest_id": self.quest_id,
                "idempotency_key": self.idempotency_key,
            }
        )
        return f"rcr_{digest[:32]}"


class ResearchControllerRegistration(ControllerModel):
    """DB-time subscription of one Quest to the deployment-pinned controller."""

    schema_name: Literal["aletheia.research_controller_registration"] = (
        "aletheia.research_controller_registration"
    )
    schema_version: Literal[1] = 1
    registration_id: str = Field(pattern=_REGISTRATION_ID_PATTERN)
    launch_request: ResearchControllerLaunchRequest
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    registered_by_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    registered_at: AwareDatetime
    scientific_checkpoint_created: Literal[False] = False

    @model_validator(mode="after")
    def _registration_matches_launch(self) -> "ResearchControllerRegistration":
        if self.registration_id != self.launch_request.registration_id:
            raise ValueError("controller registration id differs from the launch request")
        return self

    @property
    def registration_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerWakeupKind(str, Enum):
    LAUNCH = "launch"
    KERNEL_OUTBOX = "kernel_outbox"
    EXECUTION_TERMINAL_OUTBOX = "execution_terminal_outbox"


class ControllerWakeup(ControllerModel):
    """One immutable source delivery; duplicate delivery creates the same durable task."""

    schema_name: Literal["aletheia.research_controller_wakeup"] = (
        "aletheia.research_controller_wakeup"
    )
    schema_version: Literal[1] = 1
    registration_id: str = Field(pattern=_REGISTRATION_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    source_kind: ControllerWakeupKind
    source_key: str = Field(pattern=_IDENTITY_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_stream_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _source_shape_matches_kind(self) -> "ControllerWakeup":
        kernel = self.source_kind is ControllerWakeupKind.KERNEL_OUTBOX
        if kernel != (self.source_stream_version is not None):
            raise ValueError("only a Kernel outbox wakeup carries a stream version")
        return self

    @property
    def wakeup_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchControllerTaskInput(ControllerModel):
    """Closed queue payload; worker-side validation never trusts a loose JSON dictionary."""

    schema_name: Literal["aletheia.research_controller_task_input"] = (
        "aletheia.research_controller_task_input"
    )
    schema_version: Literal[1] = 1
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    wakeup: ControllerWakeup
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    delivery_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    delivery_generation: int = Field(default=0, ge=0, le=1_024)
    supersedes_task_id: str | None = Field(default=None, pattern=_TASK_ID_PATTERN)

    @model_validator(mode="after")
    def _wakeup_hash_matches(self) -> "ResearchControllerTaskInput":
        if self.wakeup_sha256 != self.wakeup.wakeup_sha256:
            raise ValueError("controller task wakeup hash differs from its payload")
        initial = self.delivery_generation == 0
        if (
            initial and (self.delivery_sha256 is not None or self.supersedes_task_id is not None)
        ) or (not initial and (self.delivery_sha256 is None or self.supersedes_task_id is None)):
            raise ValueError(
                "only a redriven controller task carries delivery and predecessor identity"
            )
        return self


class ControllerDeliveryAttemptKind(str, Enum):
    INITIAL = "initial"
    FAILURE_REDRIVE = "failure_redrive"
    COMPLETED_SUCCESSOR = "completed_successor"


class ControllerDeliveryAttempt(ControllerModel):
    """Append-only generation binding one delivery to one deterministic task."""

    schema_name: Literal["aletheia.research_controller_delivery_attempt"] = (
        "aletheia.research_controller_delivery_attempt"
    )
    schema_version: Literal[1] = 1
    delivery_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    generation: int = Field(ge=0, le=1_024)
    kind: ControllerDeliveryAttemptKind
    task_id: str = Field(pattern=_TASK_ID_PATTERN)
    task_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    supersedes_task_id: str | None = Field(default=None, pattern=_TASK_ID_PATTERN)
    predecessor_status: Literal["failed", "succeeded"] | None = None
    predecessor_terminal_category: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$"
    )
    predecessor_terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    predecessor_result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    predecessor_tick_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _generation_chain_is_typed(self) -> "ControllerDeliveryAttempt":
        predecessor_fields = (
            self.supersedes_task_id,
            self.predecessor_status,
            self.predecessor_terminal_category,
            self.predecessor_terminal_detail_sha256,
            self.predecessor_result_sha256,
            self.predecessor_tick_receipt_sha256,
        )
        if self.generation == 0:
            if self.kind is not ControllerDeliveryAttemptKind.INITIAL or any(predecessor_fields):
                raise ValueError("initial delivery attempt cannot carry a predecessor")
            return self
        if self.kind is ControllerDeliveryAttemptKind.INITIAL or self.supersedes_task_id is None:
            raise ValueError("successor delivery attempt requires an exact predecessor")
        if self.task_id == self.supersedes_task_id:
            raise ValueError("successor delivery task cannot supersede itself")
        if self.kind is ControllerDeliveryAttemptKind.FAILURE_REDRIVE:
            if (
                self.predecessor_status != "failed"
                or self.predecessor_terminal_category is None
                or self.predecessor_terminal_detail_sha256 is None
                or self.predecessor_result_sha256 is not None
                or self.predecessor_tick_receipt_sha256 is not None
            ):
                raise ValueError("failure redrive requires an exact failed predecessor")
        elif (
            self.predecessor_status != "succeeded"
            or self.predecessor_terminal_category != "success"
            or self.predecessor_terminal_detail_sha256 is not None
            or self.predecessor_result_sha256 is None
            or self.predecessor_tick_receipt_sha256 is None
        ):
            raise ValueError("completed successor requires an exact successful tick receipt")
        return self

    @property
    def attempt_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerDeadLetterReason(str, Enum):
    GENERATION_LIMIT_EXHAUSTED = "generation_limit_exhausted"
    INVALID_SUCCEEDED_RESULT = "invalid_succeeded_result"
    TASK_CANCELLED = "task_cancelled"


class ControllerDeliveryResolutionDisposition(str, Enum):
    AWAITING_AUTHORITY = "awaiting_authority"
    AWAITING_EXTERNAL_RESULT = "awaiting_external_result"
    BLOCKED = "blocked"
    AUTHORITATIVE_SOURCE_COMMITTED = "authoritative_source_committed"
    DEAD_LETTER = "dead_letter"


class ControllerDeliveryResolution(ControllerModel):
    """Append-only terminal resolution that removes a delivery from reconciliation."""

    schema_name: Literal["aletheia.research_controller_delivery_resolution"] = (
        "aletheia.research_controller_delivery_resolution"
    )
    schema_version: Literal[1] = 1
    delivery_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    latest_attempt_sha256: str = Field(pattern=_SHA256_PATTERN)
    exhausted_generation: int = Field(ge=0, le=1_024)
    max_delivery_generation: int = Field(ge=0, le=1_024)
    terminal_task_id: str = Field(pattern=_TASK_ID_PATTERN)
    terminal_task_status: Literal["failed", "succeeded", "cancelled"]
    terminal_category: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    terminal_result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tick_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    step_disposition: (
        Literal["completed", "awaiting_authority", "awaiting_external_result", "blocked"] | None
    ) = None
    signed_kernel_command_committed: bool | None = None
    independent_observation_admission_committed: bool | None = None
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: ControllerDeliveryResolutionDisposition
    dead_letter_reason: ControllerDeadLetterReason | None = None
    resolved_at: AwareDatetime

    @model_validator(mode="after")
    def _terminal_shape_is_exact(self) -> "ControllerDeliveryResolution":
        if self.exhausted_generation > self.max_delivery_generation:
            raise ValueError("resolution generation exceeds its deployment-pinned limit")
        if (
            self.independent_observation_admission_committed is True
            and self.signed_kernel_command_committed is not True
        ):
            raise ValueError("independent observation admission requires its atomic Kernel command")
        unsuccessful = self.terminal_task_status in {"failed", "cancelled"}
        if unsuccessful != (
            self.terminal_detail_sha256 is not None and self.terminal_result_sha256 is None
        ):
            raise ValueError("resolution terminal evidence differs from task status")
        if not unsuccessful and (
            self.terminal_category != "success"
            or self.terminal_result_sha256 is None
            or self.terminal_detail_sha256 is not None
        ):
            raise ValueError("successful resolution requires an exact result envelope")
        if self.terminal_task_status == "cancelled" and self.terminal_category != "cancelled":
            raise ValueError("cancelled resolution requires the cancelled terminal category")
        dead_letter = self.disposition is ControllerDeliveryResolutionDisposition.DEAD_LETTER
        if dead_letter != (self.dead_letter_reason is not None):
            raise ValueError("only a dead-letter resolution carries a dead-letter reason")
        if dead_letter:
            if (
                self.dead_letter_reason is ControllerDeadLetterReason.GENERATION_LIMIT_EXHAUSTED
                and self.exhausted_generation != self.max_delivery_generation
            ):
                raise ValueError("generation-limit dead-letter was created before the cap")
            if (
                self.dead_letter_reason is ControllerDeadLetterReason.INVALID_SUCCEEDED_RESULT
                and unsuccessful
            ):
                raise ValueError("only a succeeded task can carry an invalid result dead-letter")
            if (self.dead_letter_reason is ControllerDeadLetterReason.TASK_CANCELLED) != (
                self.terminal_task_status == "cancelled"
            ):
                raise ValueError("cancelled task requires its typed cancellation dead-letter")
            unverified = (
                self.dead_letter_reason
                in {
                    ControllerDeadLetterReason.INVALID_SUCCEEDED_RESULT,
                    ControllerDeadLetterReason.TASK_CANCELLED,
                }
                or self.terminal_task_status == "failed"
            )
            if unverified and any(
                value is not None
                for value in (
                    self.tick_receipt_sha256,
                    self.step_disposition,
                    self.signed_kernel_command_committed,
                    self.independent_observation_admission_committed,
                )
            ):
                raise ValueError("invalid result dead-letter cannot claim a verified tick receipt")
            if not unverified and (
                self.tick_receipt_sha256 is None
                or self.step_disposition != "completed"
                or self.signed_kernel_command_committed is not False
                or self.independent_observation_admission_committed is not False
            ):
                raise ValueError(
                    "successful generation-cap dead-letter requires a successor-eligible tick"
                )
            return self
        if (
            unsuccessful
            or self.tick_receipt_sha256 is None
            or self.step_disposition is None
            or self.signed_kernel_command_committed is None
            or self.independent_observation_admission_committed is None
        ):
            raise ValueError("non-dead-letter resolution requires a verified successful tick")
        expected = {
            ControllerDeliveryResolutionDisposition.AWAITING_AUTHORITY: "awaiting_authority",
            ControllerDeliveryResolutionDisposition.AWAITING_EXTERNAL_RESULT: (
                "awaiting_external_result"
            ),
            ControllerDeliveryResolutionDisposition.BLOCKED: "blocked",
            ControllerDeliveryResolutionDisposition.AUTHORITATIVE_SOURCE_COMMITTED: "completed",
        }[self.disposition]
        if self.step_disposition != expected:
            raise ValueError("delivery resolution differs from its tick disposition")
        authoritative = (
            self.disposition
            is ControllerDeliveryResolutionDisposition.AUTHORITATIVE_SOURCE_COMMITTED
        )
        if authoritative != (
            self.signed_kernel_command_committed or self.independent_observation_admission_committed
        ):
            raise ValueError("resolution differs from its authoritative commit flags")
        return self

    @property
    def resolution_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchControllerLaunchReceipt(ControllerModel):
    """Exact launch registration and initial durable wakeup result."""

    schema_name: Literal["aletheia.research_controller_launch_receipt"] = (
        "aletheia.research_controller_launch_receipt"
    )
    schema_version: Literal[1] = 1
    registration: ResearchControllerRegistration
    wakeup: ControllerWakeup
    durable_task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
    created: bool

    @model_validator(mode="after")
    def _launch_chain_matches(self) -> "ResearchControllerLaunchReceipt":
        if (
            self.wakeup.registration_id != self.registration.registration_id
            or self.wakeup.quest_id != self.registration.launch_request.quest_id
            or self.wakeup.source_kind is not ControllerWakeupKind.LAUNCH
            or self.wakeup.source_sha256 != self.registration.launch_request.request_sha256
        ):
            raise ValueError("controller launch receipt contains a mismatched wakeup")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class CompilationDisposition(str, Enum):
    MISSING = "missing"
    BLOCKED = "blocked"
    ACCEPTED = "accepted"


class ControllerStep(str, Enum):
    PROPOSE_ACTION = "propose_action"
    AWAIT_ACTION_AUTHORIZATION = "await_action_authorization"
    COMPILE_PROTOCOL = "compile_protocol"
    PROPOSE_REDESIGN = "propose_redesign"
    REGISTER_EXECUTION = "register_execution"
    AWAIT_EXECUTION = "await_execution"
    COMMIT_VALIDATION = "commit_validation"
    COMMIT_ADMISSION = "commit_admission"
    DERIVE_CONTINUATION = "derive_continuation"
    PROPOSE_FOLLOWUP = "propose_followup"
    BLOCKED = "blocked"


class ControllerRecoveryProjection(ControllerModel):
    """Recomputable receipt presence for one action/slot; never a scientific checkpoint."""

    schema_name: Literal["aletheia.research_controller_recovery_projection"] = (
        "aletheia.research_controller_recovery_projection"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    action_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    scientific_slot_id: str | None = Field(default=None, pattern=r"^sos_[0-9a-f]{32}$")
    audited_stream_version: int = Field(ge=1)
    audited_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    audited_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_authorized: bool
    compilation_disposition: CompilationDisposition
    scientific_execution_authorization_registered: bool
    execution_terminal_observed: bool
    validation_committed: bool
    admission_committed: bool
    observation_incorporated: bool
    continuation_committed: bool
    blocker_codes: tuple[str, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def _receipt_chain_is_monotonic(self) -> "ControllerRecoveryProjection":
        blockers = tuple(sorted(set(self.blocker_codes)))
        if blockers != self.blocker_codes:
            raise ValueError("controller blocker codes must be unique and canonical")
        downstream = any(
            (
                self.compilation_disposition is not CompilationDisposition.MISSING,
                self.scientific_execution_authorization_registered,
                self.execution_terminal_observed,
                self.validation_committed,
                self.admission_committed,
                self.observation_incorporated,
                self.continuation_committed,
            )
        )
        if self.action_sha256 is None and (self.action_authorized or downstream):
            raise ValueError("controller receipts require an exact action")
        if self.scientific_slot_id is not None and self.action_sha256 is None:
            raise ValueError("scientific slot requires an exact action")
        if self.compilation_disposition is CompilationDisposition.BLOCKED and any(
            (
                self.scientific_execution_authorization_registered,
                self.execution_terminal_observed,
                self.validation_committed,
                self.admission_committed,
                self.observation_incorporated,
                self.continuation_committed,
            )
        ):
            raise ValueError("blocked compilation cannot have downstream execution receipts")
        accepted = self.compilation_disposition is CompilationDisposition.ACCEPTED
        if self.scientific_slot_id is not None and not accepted:
            raise ValueError("only an accepted compilation may carry an exact scientific slot")
        if (
            self.compilation_disposition is not CompilationDisposition.MISSING
            and not self.action_authorized
        ):
            raise ValueError("protocol compilation requires an authorized action")
        if self.scientific_execution_authorization_registered and (self.scientific_slot_id is None):
            raise ValueError("execution registration requires an exact scientific slot")
        if self.scientific_execution_authorization_registered and (
            not self.action_authorized
            or self.compilation_disposition is not CompilationDisposition.ACCEPTED
        ):
            raise ValueError("execution registration requires an authorized accepted compilation")
        if self.admission_committed != self.observation_incorporated:
            raise ValueError(
                "observation admission and Kernel incorporation must commit atomically"
            )
        chain = (
            self.scientific_execution_authorization_registered,
            self.execution_terminal_observed,
            self.validation_committed,
            self.admission_committed,
            self.observation_incorporated,
            self.continuation_committed,
        )
        if any(chain[index] and not chain[index - 1] for index in range(1, len(chain))):
            raise ValueError("controller recovery receipts are not a monotonic chain")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerTickPlan(ControllerModel):
    """Deterministic operational next step derived from durable receipts."""

    schema_name: Literal["aletheia.research_controller_tick_plan"] = (
        "aletheia.research_controller_tick_plan"
    )
    schema_version: Literal[1] = 1
    projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    step: ControllerStep
    audited_stream_version: int = Field(ge=1)
    audited_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    audited_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    blocker_codes: tuple[str, ...]
    direct_scientific_mutation_allowed: Literal[False] = False

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)


def plan_recovery_tick(projection: ControllerRecoveryProjection) -> ControllerTickPlan:
    """Choose one replay-safe step without interpreting a scientific result."""

    if projection.blocker_codes:
        step = ControllerStep.BLOCKED
    elif projection.action_sha256 is None:
        step = ControllerStep.PROPOSE_ACTION
    elif not projection.action_authorized:
        step = ControllerStep.AWAIT_ACTION_AUTHORIZATION
    elif projection.compilation_disposition is CompilationDisposition.MISSING:
        step = ControllerStep.COMPILE_PROTOCOL
    elif projection.compilation_disposition is CompilationDisposition.BLOCKED:
        step = ControllerStep.PROPOSE_REDESIGN
    elif not projection.scientific_execution_authorization_registered:
        step = ControllerStep.REGISTER_EXECUTION
    elif not projection.execution_terminal_observed:
        step = ControllerStep.AWAIT_EXECUTION
    elif not projection.validation_committed:
        step = ControllerStep.COMMIT_VALIDATION
    elif not projection.admission_committed:
        step = ControllerStep.COMMIT_ADMISSION
    elif not projection.continuation_committed:
        step = ControllerStep.DERIVE_CONTINUATION
    else:
        step = ControllerStep.PROPOSE_FOLLOWUP
    return ControllerTickPlan(
        projection_sha256=projection.projection_sha256,
        step=step,
        audited_stream_version=projection.audited_stream_version,
        audited_tail_event_sha256=projection.audited_tail_event_sha256,
        audited_snapshot_sha256=projection.audited_snapshot_sha256,
        blocker_codes=projection.blocker_codes,
    )


def controller_task_spec(
    *,
    manifest: ResearchControllerManifest,
    wakeup: ControllerWakeup,
    delivery_sha256: str | None = None,
    delivery_generation: int = 0,
    supersedes_task_id: str | None = None,
) -> TaskSpec:
    """Build the exact idempotent one-tick durable task for a wakeup source."""

    inputs = ResearchControllerTaskInput(
        controller_id=manifest.controller_id,
        controller_manifest_sha256=manifest.manifest_sha256,
        wakeup=wakeup,
        wakeup_sha256=wakeup.wakeup_sha256,
        delivery_sha256=delivery_sha256,
        delivery_generation=delivery_generation,
        supersedes_task_id=supersedes_task_id,
    )
    identity = {
        "schema_name": "aletheia.research_controller_task_identity",
        "schema_version": 1,
        "controller_manifest_sha256": manifest.manifest_sha256,
        "wakeup_sha256": wakeup.wakeup_sha256,
    }
    if delivery_generation > 0:
        identity = {
            **identity,
            "schema_version": 2,
            "delivery_sha256": delivery_sha256,
            "delivery_generation": delivery_generation,
            "supersedes_task_id": supersedes_task_id,
        }
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return TaskSpec(
        task_id=f"task-rctl-{digest[:32]}",
        task_type=CONTROLLER_TASK_TYPE,
        inputs=inputs.model_dump(mode="json"),
        owner=f"research-controller:{wakeup.quest_id}",
        run_id=None,
        idempotency_key=f"research-controller:{wakeup.quest_id}:{digest[:32]}",
        concurrency_key=f"research-controller:{wakeup.quest_id}",
        retry_policy=manifest.retry_policy,
    )


def controller_initial_delivery_attempt(
    *,
    manifest: ResearchControllerManifest,
    wakeup: ControllerWakeup,
    delivery_sha256: str,
    task_spec: TaskSpec,
    recorded_at: datetime,
) -> ControllerDeliveryAttempt:
    """Bind generation zero to the exact initial delivery task."""

    expected = controller_task_spec(manifest=manifest, wakeup=wakeup)
    if task_spec != expected:
        raise ValueError("initial controller delivery attempt received a rebound task")
    return ControllerDeliveryAttempt(
        delivery_sha256=delivery_sha256,
        quest_id=wakeup.quest_id,
        wakeup_sha256=wakeup.wakeup_sha256,
        controller_manifest_sha256=manifest.manifest_sha256,
        generation=0,
        kind=ControllerDeliveryAttemptKind.INITIAL,
        task_id=task_spec.task_id,
        task_request_sha256=task_spec.request_sha256,
        recorded_at=recorded_at,
    )


__all__ = [
    "CONTROLLER_TASK_TYPE",
    "CompilationDisposition",
    "ControllerDeadLetterReason",
    "ControllerDeliveryAttempt",
    "ControllerDeliveryAttemptKind",
    "ControllerDeliveryResolution",
    "ControllerDeliveryResolutionDisposition",
    "ControllerRecoveryProjection",
    "ControllerStep",
    "ControllerTickPlan",
    "ControllerWakeup",
    "ControllerWakeupKind",
    "ResearchControllerLaunchRequest",
    "ResearchControllerLaunchReceipt",
    "ResearchControllerManifest",
    "ResearchControllerRegistration",
    "ResearchControllerTaskInput",
    "controller_initial_delivery_attempt",
    "controller_task_spec",
    "plan_recovery_tick",
]
