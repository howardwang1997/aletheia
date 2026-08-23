"""Pure, immutable contracts at the scientific-protocol/execution boundary.

These schemas describe what may be placed and what an executor may report.  They do not inspect
live capacity, allocate hardware, move bytes, execute code, or admit a scientific observation.
In particular, a scientific replicate slot is preregistered identity while an infrastructure
attempt is a recoverable engineering lineage beneath that slot.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

EXECUTION_SCHEMA_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_RESOURCE_CLASS_ID_PATTERN = r"^rsc_[0-9a-f]{32}$"
_REPLICATE_SLOT_ID_PATTERN = r"^rps_[0-9a-f]{32}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_ARTIFACT_ID_PATTERN = r"^art_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MEDIA_TYPE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"


def _without_none(value: object) -> object:
    if isinstance(value, BaseModel):
        return _without_none(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one execution contract using canonical JSON v1."""

    return json.dumps(
        _without_none(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _derived_id(*, prefix: str, context: str, value: object) -> str:
    digest = canonical_sha256(
        {
            "schema": context,
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "value": value,
        }
    )
    return f"{prefix}_{digest[:32]}"


def _canonical_strings(
    values: tuple[str, ...], label: str, *, required: bool = False
) -> tuple[str, ...]:
    if required and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not item or item != item.strip() for item in values):
        raise ValueError(f"{label} must contain nonempty canonical strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


class ExecutionModel(BaseModel):
    """Closed, frozen base for every execution contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _datetimes_are_canonical_utc(self) -> "ExecutionModel":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


class ResourceKind(str, Enum):
    CPU = "cpu"
    ACCELERATOR = "accelerator"
    EXTERNAL = "external"


class NetworkPolicy(str, Enum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    AUTHENTICATED_EXTERNAL = "authenticated_external"


class DataLocality(str, Enum):
    ANY = "any"
    SITE_PINNED = "site_pinned"
    REGION_PINNED = "region_pinned"


class ExecutionEffectClass(str, Enum):
    """Replay semantics of the real-world effect, not of queue delivery."""

    REPLAY_SAFE = "replay_safe"
    IDEMPOTENT_EXTERNAL = "idempotent_external"
    ONE_TIME_EXTERNAL = "one_time_external"


class ScientificReplicateKind(str, Enum):
    PRIMARY = "primary"
    EXPLORATORY = "exploratory"
    CONFIRMATION = "confirmation"
    INDEPENDENT_REPLICATION = "independent_replication"
    CALIBRATION = "calibration"
    CONTROL = "control"


class ArtifactRole(str, Enum):
    RAW_OUTPUT = "raw_output"
    CHECKPOINT = "checkpoint"
    STDOUT = "stdout"
    STDERR = "stderr"
    TELEMETRY = "telemetry"
    PROVIDER_RECEIPT = "provider_receipt"


class ArtifactCustodyMode(str, Enum):
    CENTRAL_REHASH = "central_rehash"
    SITE_LOCAL_ATTESTED = "site_local_attested"


class ExecutionTerminalState(str, Enum):
    ENGINEERING_SUCCEEDED = "engineering_succeeded"
    EXECUTION_FAILED = "execution_failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED = "cancelled"


class ExecutionFailureCategory(str, Enum):
    INFRASTRUCTURE = "infrastructure"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    TIMEOUT = "timeout"
    PROCESS_ERROR = "process_error"
    INVALID_OUTPUT = "invalid_output"
    ARTIFACT_VERIFICATION = "artifact_verification"
    POLICY = "policy"
    AMBIGUOUS_EXTERNAL_OUTCOME = "ambiguous_external_outcome"
    CANCELLED = "cancelled"


_RETRYABLE_FAILURE_CATEGORIES = frozenset(
    {
        ExecutionFailureCategory.INFRASTRUCTURE,
        ExecutionFailureCategory.RESOURCE_EXHAUSTED,
        ExecutionFailureCategory.TIMEOUT,
        ExecutionFailureCategory.PROCESS_ERROR,
    }
)


class ExecutionRetryMode(str, Enum):
    NEVER = "never"
    IDEMPOTENT_NEW_ATTEMPT = "idempotent_new_attempt"
    RECONCILE_THEN_NEW_ATTEMPT = "reconcile_then_new_attempt"
    CHECKPOINT_RESUME = "checkpoint_resume"


class ExecutionRetryDisposition(str, Enum):
    RETRYABLE = "retryable"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class CapabilityFailureCategory(str, Enum):
    INFRASTRUCTURE = "infrastructure"
    EXECUTION = "execution"
    MEASUREMENT = "measurement"
    INVALID_OUTPUT = "invalid_output"
    POLICY = "policy"
    SAFETY = "safety"


_CAPABILITY_TO_EXECUTION_FAILURE_CATEGORIES = {
    CapabilityFailureCategory.INFRASTRUCTURE: frozenset(
        {
            ExecutionFailureCategory.INFRASTRUCTURE,
            ExecutionFailureCategory.RESOURCE_EXHAUSTED,
            ExecutionFailureCategory.TIMEOUT,
        }
    ),
    CapabilityFailureCategory.EXECUTION: frozenset({ExecutionFailureCategory.PROCESS_ERROR}),
}


class ExecutionRetryRule(ExecutionModel):
    """Exact selected-capability failure rule that may participate in recovery."""

    capability_failure_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    capability_failure_category: CapabilityFailureCategory
    detection_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: ExecutionRetryDisposition


class ExecutionRetryPolicy(ExecutionModel):
    """Execution-side projection of the selected immutable capability retry contract."""

    schema_name: Literal["aletheia.execution_retry_policy"] = "aletheia.execution_retry_policy"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    mode: ExecutionRetryMode
    maximum_attempts_per_scientific_slot: int = Field(ge=1, le=100)
    retry_rules: tuple[ExecutionRetryRule, ...] = ()
    idempotency_rule_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reconciliation_rule_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    checkpoint_schema_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _policy_is_closed(self) -> "ExecutionRetryPolicy":
        failure_ids = tuple(item.capability_failure_id for item in self.retry_rules)
        if failure_ids != tuple(sorted(set(failure_ids))):
            raise ValueError("execution retry rules must be unique and canonical")
        if self.mode is ExecutionRetryMode.NEVER:
            if (
                self.maximum_attempts_per_scientific_slot != 1
                or self.retry_rules
                or self.idempotency_rule_sha256 is not None
                or self.reconciliation_rule_sha256 is not None
                or self.checkpoint_schema_sha256 is not None
            ):
                raise ValueError("non-retryable execution policy must be empty and single-attempt")
            return self
        if (
            self.maximum_attempts_per_scientific_slot < 2
            or not self.retry_rules
            or self.idempotency_rule_sha256 is None
        ):
            raise ValueError("retryable execution policy requires typed rules and idempotency")
        reconciliation_failures = any(
            item.disposition is ExecutionRetryDisposition.RECONCILIATION_REQUIRED
            for item in self.retry_rules
        )
        if reconciliation_failures and (
            self.mode is not ExecutionRetryMode.RECONCILE_THEN_NEW_ATTEMPT
        ):
            raise ValueError("reconciliation-required failures cannot authorize a direct retry")
        if self.mode is ExecutionRetryMode.RECONCILE_THEN_NEW_ATTEMPT:
            if self.reconciliation_rule_sha256 is None:
                raise ValueError("reconciliation retry policy requires its exact rule")
        elif self.reconciliation_rule_sha256 is not None:
            raise ValueError("only reconciliation mode may carry a reconciliation rule")
        if self.mode is ExecutionRetryMode.CHECKPOINT_RESUME:
            if self.checkpoint_schema_sha256 is None:
                raise ValueError("checkpoint retry policy requires its exact schema")
        elif self.checkpoint_schema_sha256 is not None:
            raise ValueError("only checkpoint mode may carry a checkpoint schema")
        return self

    @property
    def retry_policy_sha256(self) -> str:
        return canonical_sha256(self)


class RawScientificOutcome(str, Enum):
    """Unadmitted executor report; never an authoritative scientific observation."""

    NOT_ASSESSED = "not_assessed"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    INCONCLUSIVE = "inconclusive"


class StaticResourceClass(ExecutionModel):
    """One immutable structural resource class, with no live capacity claims."""

    schema_name: Literal["aletheia.static_resource_class"] = "aletheia.static_resource_class"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    class_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    kind: ResourceKind
    cpu_architecture: str = Field(min_length=1, max_length=64)
    oci_platform: str = Field(min_length=1, max_length=128)
    container_runtime: str = Field(min_length=1, max_length=128)
    cpu_cores: int = Field(ge=1)
    memory_bytes: int = Field(ge=1)
    scratch_bytes: int = Field(ge=1)
    accelerator_model: str | None = Field(default=None, min_length=1, max_length=128)
    accelerator_count: int = Field(default=0, ge=0, le=64)
    accelerator_memory_bytes: int | None = Field(default=None, ge=1)
    accelerator_compute_capability: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$")
    features: tuple[str, ...] = ()
    network_policies: tuple[NetworkPolicy, ...] = Field(min_length=1)
    locality_labels: tuple[str, ...] = ()
    supports_exclusive: bool = True
    supports_preemption: bool = False
    supports_checkpointing: bool = False
    external_action_kinds: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _resource_class_is_static_and_canonical(self) -> "StaticResourceClass":
        _canonical_strings(self.features, "resource features")
        _canonical_strings(self.locality_labels, "resource locality labels")
        _canonical_strings(self.external_action_kinds, "external action kinds")
        if self.network_policies != tuple(
            sorted(set(self.network_policies), key=lambda item: item.value)
        ):
            raise ValueError("resource network policies must be unique and canonically ordered")
        accelerator_fields = (
            self.accelerator_model,
            self.accelerator_memory_bytes,
            self.accelerator_compute_capability,
        )
        if self.kind is ResourceKind.ACCELERATOR:
            if self.accelerator_count < 1 or any(item is None for item in accelerator_fields):
                raise ValueError(
                    "accelerator resource classes require complete accelerator identity"
                )
        elif self.accelerator_count != 0 or any(item is not None for item in accelerator_fields):
            raise ValueError("only accelerator resource classes may declare accelerator fields")
        if self.kind is ResourceKind.EXTERNAL:
            if not self.external_action_kinds:
                raise ValueError("external resource classes require external action kinds")
        elif self.external_action_kinds:
            raise ValueError("only external resource classes may declare external action kinds")
        return self

    @property
    def resource_class_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def resource_class_id(self) -> str:
        return _derived_id(
            prefix="rsc",
            context="aletheia.static_resource_class_identity",
            value=self,
        )


class StaticResourceCatalog(ExecutionModel):
    """Frozen compiler catalog; intentionally excludes inventories and current availability."""

    schema_name: Literal["aletheia.static_resource_catalog"] = "aletheia.static_resource_catalog"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    catalog_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    resource_classes: tuple[StaticResourceClass, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _catalog_is_canonical(self) -> "StaticResourceCatalog":
        expected = tuple(sorted(self.resource_classes, key=lambda item: item.resource_class_id))
        if self.resource_classes != expected:
            raise ValueError("resource classes must be canonically ordered by resource class id")
        ids = tuple(item.resource_class_id for item in self.resource_classes)
        if len(ids) != len(set(ids)):
            raise ValueError("resource catalog repeats a resource class")
        keys = tuple(item.class_key for item in self.resource_classes)
        if len(keys) != len(set(keys)):
            raise ValueError("resource catalog repeats a resource class key")
        return self

    @property
    def catalog_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def resource_class_ids(self) -> tuple[str, ...]:
        return tuple(item.resource_class_id for item in self.resource_classes)


class ExecutionResourceRequest(ExecutionModel):
    """Frozen structural envelope; an allocator decides live placement later."""

    schema_name: Literal["aletheia.execution_resource_request"] = (
        "aletheia.execution_resource_request"
    )
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    accepted_resource_class_ids: tuple[str, ...] = Field(min_length=1)
    cpu_cores: int = Field(ge=1)
    memory_bytes: int = Field(ge=1)
    scratch_bytes: int = Field(ge=1)
    wall_time_seconds: int = Field(ge=1, le=31_536_000)
    accelerator_count: int = Field(default=0, ge=0, le=64)
    allowed_accelerator_models: tuple[str, ...] = ()
    minimum_accelerator_memory_bytes: int | None = Field(default=None, ge=1)
    minimum_compute_capability: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$")
    required_features: tuple[str, ...] = ()
    exclusive: bool = True
    preemptible: bool = False
    checkpoint_interval_seconds: int | None = Field(default=None, ge=1)
    data_locality: DataLocality = DataLocality.ANY
    locality_labels: tuple[str, ...] = ()
    network_policy: NetworkPolicy = NetworkPolicy.NONE
    egress_allowlist_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    max_infrastructure_attempts: int = Field(default=1, ge=1, le=100)
    artifact_quota_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def _request_is_canonical(self) -> "ExecutionResourceRequest":
        for values, label in (
            (self.accepted_resource_class_ids, "accepted resource class ids"),
            (self.allowed_accelerator_models, "allowed accelerator models"),
            (self.required_features, "required resource features"),
            (self.locality_labels, "resource locality labels"),
        ):
            _canonical_strings(values, label, required=label == "accepted resource class ids")
        if any(
            not re.fullmatch(_RESOURCE_CLASS_ID_PATTERN, item)
            for item in self.accepted_resource_class_ids
        ):
            raise ValueError("accepted resource class ids must be deterministic class identities")
        accelerator_fields = (
            self.minimum_accelerator_memory_bytes,
            self.minimum_compute_capability,
        )
        if self.accelerator_count:
            if not self.allowed_accelerator_models or any(
                item is None for item in accelerator_fields
            ):
                raise ValueError(
                    "accelerator requests require allowed models, memory, and capability"
                )
        elif self.allowed_accelerator_models or any(
            item is not None for item in accelerator_fields
        ):
            raise ValueError("CPU-only requests cannot declare accelerator constraints")
        if self.checkpoint_interval_seconds is not None:
            if self.checkpoint_interval_seconds >= self.wall_time_seconds:
                raise ValueError("checkpoint interval must be shorter than wall time")
        if self.data_locality is DataLocality.ANY and self.locality_labels:
            raise ValueError("unconstrained locality cannot declare locality labels")
        if self.data_locality is not DataLocality.ANY and not self.locality_labels:
            raise ValueError("pinned locality requires at least one locality label")
        if (self.network_policy is NetworkPolicy.ALLOWLIST) != (
            self.egress_allowlist_sha256 is not None
        ):
            raise ValueError("only allowlisted network requests require an egress allowlist hash")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class ExpectedArtifact(ExecutionModel):
    schema_name: Literal["aletheia.expected_artifact"] = "aletheia.expected_artifact"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    role: ArtifactRole
    media_type: str = Field(pattern=_MEDIA_TYPE_PATTERN)
    schema_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    required: bool = True
    max_bytes: int = Field(ge=1)
    data_classification: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    retention_policy_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def expected_artifact_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def expected_artifact_id(self) -> str:
        return _derived_id(
            prefix="art",
            context="aletheia.expected_artifact_identity",
            value=self,
        )


class ScientificReplicateSlot(ExecutionModel):
    """Preregistered scientific identity; infrastructure recovery never creates a new slot."""

    schema_name: Literal["aletheia.scientific_replicate_slot"] = (
        "aletheia.scientific_replicate_slot"
    )
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_node_sha256: str = Field(pattern=_SHA256_PATTERN)
    slot_count: int = Field(ge=1, le=10_000)
    slot_index: int = Field(ge=1)
    replicate_kind: ScientificReplicateKind
    preregistration_sha256: str = Field(pattern=_SHA256_PATTERN)
    randomization_seed_sha256: str = Field(pattern=_SHA256_PATTERN)
    independent_site_required: bool = False

    @model_validator(mode="after")
    def _slot_is_preregistered(self) -> "ScientificReplicateSlot":
        if self.slot_index > self.slot_count:
            raise ValueError("scientific replicate slot index exceeds its preregistered count")
        return self

    @property
    def replicate_slot_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def replicate_slot_id(self) -> str:
        return _derived_id(
            prefix="rps",
            context="aletheia.scientific_replicate_slot_identity",
            value=self,
        )


class InputArtifactBinding(ExecutionModel):
    """Typed custody pointer for one WorkOrder input port.

    Protocol inputs are admitted outside the WorkOrder. Intermediate inputs additionally bind the
    exact producer node and preregistered producer slot; PR-4 must resolve and verify the receipt
    bytes before launch.
    """

    schema_name: Literal["aletheia.input_artifact_binding"] = "aletheia.input_artifact_binding"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    input_port_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    source_kind: Literal["protocol_input", "work_order_output"]
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_work_order_node_id: str | None = Field(default=None, pattern=_SYMBOLIC_ID_PATTERN)
    source_work_order_node_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_replicate_slot_id: str | None = Field(default=None, pattern=_REPLICATE_SLOT_ID_PATTERN)
    source_slot_index: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def _source_shape_is_exact(self) -> "InputArtifactBinding":
        source_fields = (
            self.source_work_order_node_id,
            self.source_work_order_node_sha256,
            self.source_replicate_slot_id,
            self.source_slot_index,
        )
        if self.source_kind == "protocol_input" and any(item is not None for item in source_fields):
            raise ValueError("protocol inputs cannot claim a WorkOrder producer")
        if self.source_kind == "work_order_output" and any(item is None for item in source_fields):
            raise ValueError("WorkOrder outputs require complete producer and slot identity")
        return self


class InfrastructureAttempt(ExecutionModel):
    """Engineering attempt lineage for one and only one scientific replicate slot."""

    schema_name: Literal["aletheia.infrastructure_attempt"] = "aletheia.infrastructure_attempt"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    replicate_slot_id: str = Field(pattern=_REPLICATE_SLOT_ID_PATTERN)
    attempt_number: int = Field(ge=1, le=100)
    previous_attempt_id: str | None = Field(default=None, pattern=_ATTEMPT_ID_PATTERN)
    prior_confirmed_failure_receipt_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    prior_failure_category: ExecutionFailureCategory | None = None

    @model_validator(mode="after")
    def _retry_has_confirmed_lineage(self) -> "InfrastructureAttempt":
        first = self.attempt_number == 1
        prior_values = (
            self.previous_attempt_id,
            self.prior_confirmed_failure_receipt_sha256,
            self.prior_failure_category,
        )
        if first and any(item is not None for item in prior_values):
            raise ValueError("infrastructure attempt one cannot have prior attempt lineage")
        if not first and any(item is None for item in prior_values):
            raise ValueError("infrastructure retries require complete confirmed failure lineage")
        if (
            self.prior_failure_category is not None
            and self.prior_failure_category not in _RETRYABLE_FAILURE_CATEGORIES
        ):
            raise ValueError("only a confirmed infrastructure failure may create a new attempt")
        return self

    @property
    def infrastructure_attempt_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def infrastructure_attempt_id(self) -> str:
        return _derived_id(
            prefix="iat",
            context="aletheia.infrastructure_attempt_identity",
            value=self,
        )


class ExternalRequestIdentity(ExecutionModel):
    """Stable provider identity used for idempotency and explicit reconciliation."""

    schema_name: Literal["aletheia.external_request_identity"] = (
        "aletheia.external_request_identity"
    )
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    provider_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    action_kind: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    scope_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    replicate_slot_id: str = Field(pattern=_REPLICATE_SLOT_ID_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_idempotency_key: str | None = Field(default=None, pattern=_SYMBOLIC_ID_PATTERN)
    ambiguous_outcome_disposition: Literal["reconciliation_required"] = "reconciliation_required"

    @model_validator(mode="after")
    def _provider_identity_is_deterministic(self) -> "ExternalRequestIdentity":
        expected = f"aletheia:{canonical_sha256({'schema': 'aletheia.external_provider_idempotency.v1', 'external_action_id': self.external_action_id})}"
        if self.provider_idempotency_key is not None and self.provider_idempotency_key != expected:
            raise ValueError("provider idempotency key does not match the external action identity")
        object.__setattr__(self, "provider_idempotency_key", expected)
        return self

    @property
    def external_action_id(self) -> str:
        return _derived_id(
            prefix="exa",
            context="aletheia.external_action_identity",
            value={
                "provider_id": self.provider_id,
                "action_kind": self.action_kind,
                "scope_key": self.scope_key,
                "replicate_slot_id": self.replicate_slot_id,
                "request_sha256": self.request_sha256,
            },
        )


class ExecutionIntent(ExecutionModel):
    """Exact immutable input to placement and execution."""

    schema_name: Literal["aletheia.execution_intent"] = "aletheia.execution_intent"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_node_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_action_kind: str | None = Field(default=None, pattern=_SYMBOLIC_ID_PATTERN)
    resource_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_request: ExecutionResourceRequest
    retry_policy: ExecutionRetryPolicy
    replicate_slot: ScientificReplicateSlot
    infrastructure_attempt: InfrastructureAttempt
    input_artifact_bindings: tuple[InputArtifactBinding, ...] = ()
    expected_artifacts: tuple[ExpectedArtifact, ...] = Field(min_length=1)
    environment_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_parameters_sha256: str = Field(pattern=_SHA256_PATTERN)
    effect_class: ExecutionEffectClass = ExecutionEffectClass.REPLAY_SAFE
    external_request: ExternalRequestIdentity | None = None
    authorized_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _intent_is_bound_and_canonical(self) -> "ExecutionIntent":
        if self.deadline <= self.authorized_at:
            raise ValueError("execution deadline must follow authorization")
        if (
            self.replicate_slot.quest_id != self.quest_id
            or self.replicate_slot.protocol_sha256 != self.protocol_sha256
            or self.replicate_slot.work_order_id != self.work_order_id
            or self.replicate_slot.work_order_node_id != self.work_order_node_id
            or self.replicate_slot.work_order_node_sha256 != self.work_order_node_sha256
        ):
            raise ValueError(
                "execution intent and scientific replicate slot are not exactly node-bound"
            )
        if self.infrastructure_attempt.replicate_slot_id != self.replicate_slot.replicate_slot_id:
            raise ValueError("infrastructure attempt belongs to another scientific replicate slot")
        if (
            self.infrastructure_attempt.attempt_number
            > self.resource_request.max_infrastructure_attempts
        ):
            raise ValueError("infrastructure attempt exceeds the frozen resource retry bound")
        if (
            self.resource_request.max_infrastructure_attempts
            > self.retry_policy.maximum_attempts_per_scientific_slot
        ):
            raise ValueError("resource retry bound exceeds the selected capability retry policy")
        if (
            self.resource_request.checkpoint_interval_seconds is not None
            and self.retry_policy.mode is not ExecutionRetryMode.CHECKPOINT_RESUME
        ):
            raise ValueError("checkpointing requires a checkpoint-resume retry policy")
        input_keys = tuple(item.input_port_id for item in self.input_artifact_bindings)
        if input_keys != tuple(sorted(set(input_keys))):
            raise ValueError("input artifact bindings must be unique and canonically ordered")
        expected = tuple(sorted(self.expected_artifacts, key=lambda item: item.artifact_key))
        if self.expected_artifacts != expected:
            raise ValueError("expected artifacts must be canonically ordered by artifact key")
        keys = tuple(item.artifact_key for item in self.expected_artifacts)
        if len(keys) != len(set(keys)):
            raise ValueError("execution intent repeats an expected artifact key")
        external = self.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        if external != (self.external_request is not None):
            raise ValueError("external effect classes require one stable external request identity")
        if external and (
            self.external_action_kind is None
            or self.external_request is None
            or self.external_request.action_kind != self.external_action_kind
        ):
            raise ValueError("external request must bind the WorkOrder external action kind")
        if (
            external
            and self.external_request.replicate_slot_id != self.replicate_slot.replicate_slot_id
        ):
            raise ValueError("external request belongs to another scientific replicate slot")
        if self.effect_class is ExecutionEffectClass.ONE_TIME_EXTERNAL and (
            self.infrastructure_attempt.attempt_number != 1
            or self.resource_request.max_infrastructure_attempts != 1
            or self.retry_policy.mode is not ExecutionRetryMode.NEVER
        ):
            raise ValueError("one-time external effects permit exactly one infrastructure attempt")
        provider_receipts = tuple(
            item for item in self.expected_artifacts if item.role is ArtifactRole.PROVIDER_RECEIPT
        )
        if external and not (len(provider_receipts) == 1 and provider_receipts[0].required):
            raise ValueError("external effects require exactly one provider receipt artifact")
        if not external and provider_receipts:
            raise ValueError("replay-safe effects cannot declare a provider receipt artifact")
        return self

    @property
    def execution_id(self) -> str:
        """Stable across infrastructure attempts for the same work-order replicate slot."""

        return _derived_id(
            prefix="exe",
            context="aletheia.execution_identity",
            value={
                "quest_id": self.quest_id,
                "protocol_sha256": self.protocol_sha256,
                "work_order_id": self.work_order_id,
                "work_order_sha256": self.work_order_sha256,
                "replicate_slot_id": self.replicate_slot.replicate_slot_id,
            },
        )

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self)


class ArtifactManifestEntry(ExecutionModel):
    schema_name: Literal["aletheia.artifact_manifest_entry"] = "aletheia.artifact_manifest_entry"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    expected_artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    role: ArtifactRole
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    bytes: int = Field(ge=0)
    media_type: str = Field(pattern=_MEDIA_TYPE_PATTERN)
    schema_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    quarantine_ref: str = Field(min_length=1, max_length=1024)

    @property
    def manifest_entry_sha256(self) -> str:
        return canonical_sha256(self)


class ArtifactManifest(ExecutionModel):
    schema_name: Literal["aletheia.artifact_manifest"] = "aletheia.artifact_manifest"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    replicate_slot_id: str = Field(pattern=_REPLICATE_SLOT_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    entries: tuple[ArtifactManifestEntry, ...]
    produced_at: AwareDatetime

    @model_validator(mode="after")
    def _manifest_is_canonical(self) -> "ArtifactManifest":
        expected = tuple(sorted(self.entries, key=lambda item: item.artifact_key))
        if self.entries != expected:
            raise ValueError("artifact manifest entries must be ordered by artifact key")
        keys = tuple(item.artifact_key for item in self.entries)
        if len(keys) != len(set(keys)):
            raise ValueError("artifact manifest repeats an artifact key")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class ArtifactVerifiedReceipt(ExecutionModel):
    """Independent rehash/custody proof for one immutable manifest entry."""

    schema_name: Literal["aletheia.artifact_verified_receipt"] = (
        "aletheia.artifact_verified_receipt"
    )
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    artifact: ArtifactManifestEntry
    custody_mode: ArtifactCustodyMode
    verifier_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    object_store_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    final_object_ref: str = Field(min_length=1, max_length=1024)
    final_object_version: str = Field(min_length=1, max_length=256)
    custody_receipt_sha256s: tuple[str, ...] = ()
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def _custody_is_canonical(self) -> "ArtifactVerifiedReceipt":
        _canonical_strings(self.custody_receipt_sha256s, "custody receipt hashes")
        if any(not re.fullmatch(_SHA256_PATTERN, item) for item in self.custody_receipt_sha256s):
            raise ValueError("custody receipt hashes must be SHA-256 values")
        if (
            self.custody_mode is ArtifactCustodyMode.SITE_LOCAL_ATTESTED
            and not self.custody_receipt_sha256s
        ):
            raise ValueError("site-local artifact verification requires a custody receipt")
        return self

    @property
    def verified_receipt_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def verified_receipt_id(self) -> str:
        return _derived_id(
            prefix="avr",
            context="aletheia.artifact_verified_receipt_identity",
            value=self,
        )


class ExecutionFailure(ExecutionModel):
    schema_name: Literal["aletheia.execution_failure"] = "aletheia.execution_failure"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    category: ExecutionFailureCategory
    detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_failure_id: str | None = Field(default=None, pattern=_SYMBOLIC_ID_PATTERN)
    capability_failure_detection_rule_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    retryable_after_confirmed_termination: bool = False

    @model_validator(mode="after")
    def _only_infrastructure_failures_are_retryable(self) -> "ExecutionFailure":
        capability_fields = (
            self.capability_failure_id,
            self.capability_failure_detection_rule_sha256,
        )
        if any(item is None for item in capability_fields) != all(
            item is None for item in capability_fields
        ):
            raise ValueError("capability failure identity and detection rule must be paired")
        if self.retryable_after_confirmed_termination and (
            self.category not in _RETRYABLE_FAILURE_CATEGORIES
            or any(item is None for item in capability_fields)
        ):
            raise ValueError(
                "retry requires a confirmed infrastructure failure and exact capability rule"
            )
        return self


class ExecutionReceipt(ExecutionModel):
    """Immutable engineering receipt; scientific admission occurs in a separate boundary."""

    schema_name: Literal["aletheia.execution_receipt"] = "aletheia.execution_receipt"
    schema_version: Literal[1] = EXECUTION_SCHEMA_VERSION
    intent: ExecutionIntent
    worker_node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_execution_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    observed_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    terminal_state: ExecutionTerminalState
    failure: ExecutionFailure | None = None
    raw_scientific_outcome: RawScientificOutcome = RawScientificOutcome.NOT_ASSESSED
    artifact_manifest: ArtifactManifest | None = None
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...] = ()
    checkpoint_receipt_sha256s: tuple[str, ...] = ()
    protocol_deviation_sha256s: tuple[str, ...] = ()
    telemetry_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    external_provider_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reconciles_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_is_exact_and_honest(self) -> "ExecutionReceipt":
        if self.observed_at < self.started_at or self.verified_at < self.observed_at:
            raise ValueError("execution receipt timestamps are out of order")
        if self.ended_at is not None and not self.started_at <= self.ended_at <= self.observed_at:
            raise ValueError("execution end time is outside its observed interval")
        for values, label in (
            (self.checkpoint_receipt_sha256s, "checkpoint receipt hashes"),
            (self.protocol_deviation_sha256s, "protocol deviation hashes"),
        ):
            _canonical_strings(values, label)
            if any(not re.fullmatch(_SHA256_PATTERN, item) for item in values):
                raise ValueError(f"{label} must be SHA-256 values")
        success = self.terminal_state is ExecutionTerminalState.ENGINEERING_SUCCEEDED
        failed = self.terminal_state is ExecutionTerminalState.EXECUTION_FAILED
        reconciling = self.terminal_state is ExecutionTerminalState.RECONCILIATION_REQUIRED
        cancelled = self.terminal_state is ExecutionTerminalState.CANCELLED
        if success:
            if self.ended_at is None or self.failure is not None:
                raise ValueError("engineering success requires an end time and forbids failure")
        elif failed:
            if self.ended_at is None or self.failure is None:
                raise ValueError("execution failure requires an end time and typed failure")
        elif reconciling:
            if self.intent.effect_class is ExecutionEffectClass.REPLAY_SAFE:
                raise ValueError("replay-safe work cannot claim an ambiguous external outcome")
            if (
                self.failure is None
                or self.failure.category is not ExecutionFailureCategory.AMBIGUOUS_EXTERNAL_OUTCOME
            ):
                raise ValueError("reconciliation requires an ambiguous-external-outcome failure")
            if self.external_provider_receipt_sha256 is not None:
                raise ValueError("an unknown external outcome cannot contain a provider receipt")
            if self.ended_at is not None:
                raise ValueError("an unresolved external effect cannot claim a known end time")
        elif cancelled and (
            self.ended_at is None
            or self.failure is None
            or self.failure.category is not ExecutionFailureCategory.CANCELLED
        ):
            raise ValueError("cancelled execution requires an end time and cancellation failure")
        if not success and self.raw_scientific_outcome is not RawScientificOutcome.NOT_ASSESSED:
            raise ValueError("failed execution cannot manufacture a scientific outcome")
        if (
            self.failure is not None
            and self.failure.category is ExecutionFailureCategory.AMBIGUOUS_EXTERNAL_OUTCOME
            and not reconciling
        ):
            raise ValueError("an ambiguous external outcome must require reconciliation")
        if (
            self.failure is not None
            and self.failure.category is ExecutionFailureCategory.CANCELLED
            and not cancelled
        ):
            raise ValueError("a cancellation failure must use the cancelled terminal state")
        external = self.intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        if success and external and self.external_provider_receipt_sha256 is None:
            raise ValueError("successful external execution requires a provider receipt")
        if not external and self.external_provider_receipt_sha256 is not None:
            raise ValueError("replay-safe execution cannot contain an external provider receipt")
        if not external and self.reconciles_receipt_sha256 is not None:
            raise ValueError("replay-safe execution cannot reconcile an external receipt")
        if reconciling and self.reconciles_receipt_sha256 is not None:
            raise ValueError("a reconciliation marker cannot reconcile itself")
        if (
            self.intent.effect_class is ExecutionEffectClass.ONE_TIME_EXTERNAL
            and self.failure is not None
            and self.failure.retryable_after_confirmed_termination
        ):
            raise ValueError("one-time external effects never authorize another attempt")
        self._validate_artifacts(success=success)
        return self

    def _validate_artifacts(self, *, success: bool) -> None:
        if self.artifact_manifest is None:
            if success or self.artifact_verified_receipts:
                raise ValueError("verified artifacts require an exact artifact manifest")
            return
        manifest = self.artifact_manifest
        if (
            manifest.intent_sha256 != self.intent.intent_sha256
            or manifest.execution_id != self.intent.execution_id
            or manifest.replicate_slot_id != self.intent.replicate_slot.replicate_slot_id
            or manifest.infrastructure_attempt_id
            != self.intent.infrastructure_attempt.infrastructure_attempt_id
        ):
            raise ValueError("artifact manifest belongs to another execution identity")
        if not self.started_at <= manifest.produced_at <= self.verified_at:
            raise ValueError(
                "artifact manifest production is outside the execution receipt interval"
            )
        expected = {item.artifact_key: item for item in self.intent.expected_artifacts}
        entries = {item.artifact_key: item for item in manifest.entries}
        if set(entries) - set(expected):
            raise ValueError("artifact manifest contains undeclared output")
        if (
            sum(item.bytes for item in manifest.entries)
            > self.intent.resource_request.artifact_quota_bytes
        ):
            raise ValueError("artifact manifest exceeds the frozen aggregate artifact quota")
        for key, entry in entries.items():
            requirement = expected[key]
            if (
                entry.expected_artifact_id != requirement.expected_artifact_id
                or entry.role is not requirement.role
                or entry.media_type != requirement.media_type
                or entry.schema_sha256 != requirement.schema_sha256
                or entry.bytes > requirement.max_bytes
            ):
                raise ValueError("artifact manifest violates its frozen expectation")
        if success and any(
            requirement.required and key not in entries for key, requirement in expected.items()
        ):
            raise ValueError("engineering success is missing a required artifact")
        receipts = self.artifact_verified_receipts
        ordered = tuple(sorted(receipts, key=lambda item: item.artifact.artifact_key))
        if receipts != ordered:
            raise ValueError("artifact verification receipts must be ordered by artifact key")
        receipt_keys = tuple(item.artifact.artifact_key for item in receipts)
        if len(receipt_keys) != len(set(receipt_keys)):
            raise ValueError("execution receipt repeats artifact verification")
        for receipt in receipts:
            entry = entries.get(receipt.artifact.artifact_key)
            if (
                entry is None
                or receipt.artifact != entry
                or receipt.artifact_manifest_sha256 != manifest.manifest_sha256
                or receipt.producer_attempt_id
                != self.intent.infrastructure_attempt.infrastructure_attempt_id
            ):
                raise ValueError("artifact verification receipt is not bound to this manifest")
            if not manifest.produced_at <= receipt.verified_at <= self.verified_at:
                raise ValueError("artifact verification is outside the final receipt interval")
        if success and set(receipt_keys) != set(entries):
            raise ValueError("engineering success requires verification of every artifact")
        if success and self.intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE:
            provider_entries = tuple(
                item for item in manifest.entries if item.role is ArtifactRole.PROVIDER_RECEIPT
            )
            if (
                len(provider_entries) != 1
                or provider_entries[0].content_sha256 != self.external_provider_receipt_sha256
            ):
                raise ValueError("external provider receipt hash is not bound to its artifact")

    @property
    def execution_receipt_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def execution_receipt_id(self) -> str:
        return _derived_id(
            prefix="xrc",
            context="aletheia.execution_receipt_identity",
            value=self,
        )


class ExecutionRetryBindingError(ValueError):
    """A proposed retry changed scientific intent or lacks a confirmed failure receipt."""


def verify_execution_retry_binding(
    previous_intent: ExecutionIntent,
    current_intent: ExecutionIntent,
    previous_receipt: ExecutionReceipt,
) -> None:
    """Verify that a retry changes only its infrastructure-attempt lineage."""

    try:
        previous = ExecutionIntent.model_validate(
            previous_intent.model_dump(mode="python", warnings="none")
        )
        current = ExecutionIntent.model_validate(
            current_intent.model_dump(mode="python", warnings="none")
        )
        receipt = ExecutionReceipt.model_validate(
            previous_receipt.model_dump(mode="python", warnings="none")
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionRetryBindingError("execution retry contracts failed revalidation") from exc

    failure = receipt.failure
    if (
        receipt.intent != previous
        or receipt.terminal_state is not ExecutionTerminalState.EXECUTION_FAILED
        or failure is None
        or not failure.retryable_after_confirmed_termination
    ):
        raise ExecutionRetryBindingError(
            "execution retry requires the exact prior intent and a confirmed retryable failure"
        )
    retry_rules = {item.capability_failure_id: item for item in previous.retry_policy.retry_rules}
    retry_rule = retry_rules.get(failure.capability_failure_id or "")
    if (
        previous.retry_policy.mode is not ExecutionRetryMode.IDEMPOTENT_NEW_ATTEMPT
        or retry_rule is None
        or retry_rule.disposition is not ExecutionRetryDisposition.RETRYABLE
        or failure.category
        not in _CAPABILITY_TO_EXECUTION_FAILURE_CATEGORIES.get(
            retry_rule.capability_failure_category,
            frozenset(),
        )
        or retry_rule.detection_rule_sha256 != failure.capability_failure_detection_rule_sha256
    ):
        raise ExecutionRetryBindingError(
            "prior failure is not retryable under the selected capability policy and "
            "direct-idempotent retry boundary"
        )
    prior_attempt = previous.infrastructure_attempt
    next_attempt = current.infrastructure_attempt
    if (
        next_attempt.attempt_number != prior_attempt.attempt_number + 1
        or next_attempt.previous_attempt_id != prior_attempt.infrastructure_attempt_id
        or next_attempt.prior_confirmed_failure_receipt_sha256 != receipt.execution_receipt_sha256
        or next_attempt.prior_failure_category is not failure.category
    ):
        raise ExecutionRetryBindingError("execution retry lineage does not bind the prior failure")

    previous_payload = previous.model_dump(mode="json", exclude_none=True)
    current_payload = current.model_dump(mode="json", exclude_none=True)
    previous_payload.pop("infrastructure_attempt")
    current_payload.pop("infrastructure_attempt")
    if canonical_json_bytes(previous_payload) != canonical_json_bytes(current_payload):
        raise ExecutionRetryBindingError(
            "execution retry changed a frozen scientific or execution-intent field"
        )


__all__ = [
    "ArtifactCustodyMode",
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "ArtifactRole",
    "ArtifactVerifiedReceipt",
    "CapabilityFailureCategory",
    "DataLocality",
    "EXECUTION_SCHEMA_VERSION",
    "ExecutionEffectClass",
    "ExecutionFailure",
    "ExecutionFailureCategory",
    "ExecutionIntent",
    "ExecutionModel",
    "ExecutionResourceRequest",
    "ExecutionTerminalState",
    "ExecutionReceipt",
    "ExecutionRetryDisposition",
    "ExecutionRetryBindingError",
    "ExecutionRetryMode",
    "ExecutionRetryPolicy",
    "ExecutionRetryRule",
    "ExpectedArtifact",
    "ExternalRequestIdentity",
    "InfrastructureAttempt",
    "InputArtifactBinding",
    "NetworkPolicy",
    "RawScientificOutcome",
    "ResourceKind",
    "ScientificReplicateKind",
    "ScientificReplicateSlot",
    "StaticResourceCatalog",
    "StaticResourceClass",
    "canonical_json_bytes",
    "canonical_sha256",
    "verify_execution_retry_binding",
]
