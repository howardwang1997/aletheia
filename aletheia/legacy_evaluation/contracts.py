"""Closed, authority-limited contracts for the PR-6 legacy evaluation leaf.

These models describe one already-authorized, atomic evaluation operation and its untrusted raw
outputs.  They contain no domain-specific metric names and never turn engineering success into a
scientific outcome, observation, claim, or Kernel mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_REPLICATE_SLOT_ID_PATTERN = r"^rps_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_SOURCE_REF_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$"
_MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9.+-]*/[a-zA-Z0-9][a-zA-Z0-9.+-]*$"


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_json_text(value: JsonValue) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be timezone-aware UTC")


def _canonical_strings(values: tuple[str, ...], *, label: str, required: bool = False) -> None:
    if required and not values:
        raise ValueError(f"{label} must not be empty")
    if values != tuple(sorted(set(values))) or any(
        not item or item != item.strip() for item in values
    ):
        raise ValueError(f"{label} must be unique canonical strings")


def _canonical_relative_path(value: str, *, label: str) -> None:
    components = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError(f"{label} must be a canonical relative path")


class LegacyEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LegacyEvaluationSourceBinding(LegacyEvaluationModel):
    """Fresh-readable source identity used to freeze one reviewed harness implementation."""

    relative_path: str = Field(min_length=1, max_length=1_024)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _path_is_relative(self) -> "LegacyEvaluationSourceBinding":
        _canonical_relative_path(self.relative_path, label="legacy evaluation source")
        if not self.relative_path.endswith(".py"):
            raise ValueError("legacy evaluation sources must be Python files")
        return self


class LegacyMetricProjection(LegacyEvaluationModel):
    """Mechanical lookup from one returned metric to the independently parsed eval record."""

    metric_name: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    eval_json_path: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _path_is_closed(self) -> "LegacyMetricProjection":
        if any(not item or item != item.strip() for item in self.eval_json_path):
            raise ValueError("metric projection path contains an empty component")
        return self


class LegacyEvaluationHarnessManifest(LegacyEvaluationModel):
    """Frozen description of the narrow DomainPlugin methods retained by PR-6."""

    schema_name: Literal["aletheia.legacy_evaluation_harness_manifest"] = (
        "aletheia.legacy_evaluation_harness_manifest"
    )
    schema_version: Literal[1] = 1
    capability_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    semantic_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    plugin_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    plugin_class_ref: str = Field(pattern=_SOURCE_REF_PATTERN)
    adapter_ref: Literal["aletheia.legacy_evaluation.capability:execute_legacy_evaluation"] = (
        "aletheia.legacy_evaluation.capability:execute_legacy_evaluation"
    )
    source_bindings: tuple[LegacyEvaluationSourceBinding, ...] = Field(min_length=4, max_length=16)
    allowed_design_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    required_design_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    allowed_model_names: tuple[str, ...] = Field(min_length=1, max_length=32)
    frozen_random_seed: int = Field(ge=0, le=2**31 - 1)
    input_table_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    metric_projections: tuple[LegacyMetricProjection, ...] = Field(min_length=1, max_length=128)
    required_legacy_artifact_kinds: tuple[Literal["eval", "model"], ...] = (
        "eval",
        "model",
    )
    maximum_input_bytes: int = Field(ge=1, le=2**40)
    maximum_rows: int = Field(ge=10, le=10_000_000)
    maximum_artifact_bytes: int = Field(ge=1, le=2**40)
    executor_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    frozen_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    frozen_at: AwareDatetime
    experiment_driver_allowed: Literal[False] = False
    plugin_run_experiment_allowed: Literal[False] = False
    plugin_data_loader_allowed: Literal[False] = False
    dynamic_plugin_loading_allowed: Literal[False] = False
    network_access_allowed: Literal[False] = False
    raw_artifact_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False
    claim_authority: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_canonical(self) -> "LegacyEvaluationHarnessManifest":
        _require_utc(self.frozen_at, label="legacy evaluation freeze time")
        expected_sources = tuple(sorted(self.source_bindings, key=lambda item: item.relative_path))
        if self.source_bindings != expected_sources or len(
            {item.relative_path for item in self.source_bindings}
        ) != len(self.source_bindings):
            raise ValueError("legacy evaluation sources must be unique and canonical")
        _canonical_strings(self.allowed_design_keys, label="allowed design keys", required=True)
        _canonical_strings(self.required_design_keys, label="required design keys", required=True)
        _canonical_strings(self.allowed_model_names, label="allowed model names", required=True)
        if not set(self.required_design_keys).issubset(self.allowed_design_keys):
            raise ValueError("required design keys must be a subset of allowed keys")
        projections = tuple(sorted(self.metric_projections, key=lambda item: item.metric_name))
        if self.metric_projections != projections or len(
            {item.metric_name for item in self.metric_projections}
        ) != len(self.metric_projections):
            raise ValueError("metric projections must be unique and canonical")
        if self.required_legacy_artifact_kinds != ("eval", "model"):
            raise ValueError("the tabular compatibility leaf requires eval and model artifacts")
        if self.executor_principal_id == self.frozen_by_principal_id:
            raise ValueError("legacy evaluation author and executor must differ")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def implementation_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.legacy_evaluation_implementation_identity",
                "schema_version": 1,
                "adapter_ref": self.adapter_ref,
                "plugin_class_ref": self.plugin_class_ref,
                "source_bindings": self.source_bindings,
            }
        )


class LegacyEvaluationInputTable(LegacyEvaluationModel):
    input_port_id: Literal["legacy.evaluation.table"] = "legacy.evaluation.table"
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    bytes: int = Field(ge=1)
    media_type: Literal["text/csv"] = "text/csv"
    schema_sha256: str = Field(pattern=_SHA256_PATTERN)


class LegacyEvaluationInvocation(LegacyEvaluationModel):
    """Content-addressed request derived from an ordinary compiled WorkOrder node."""

    schema_name: Literal["aletheia.legacy_evaluation_invocation"] = (
        "aletheia.legacy_evaluation_invocation"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    work_order_node_sha256: str = Field(pattern=_SHA256_PATTERN)
    replicate_slot_id: str = Field(pattern=_REPLICATE_SLOT_ID_PATTERN)
    capability_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_table: LegacyEvaluationInputTable
    design_json: str = Field(min_length=2, max_length=262_144)
    execution_parameters_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _invocation_is_canonical(self) -> "LegacyEvaluationInvocation":
        _require_utc(self.issued_at, label="legacy evaluation issue time")
        _require_utc(self.deadline, label="legacy evaluation deadline")
        if self.deadline <= self.issued_at:
            raise ValueError("legacy evaluation deadline must follow issue time")
        try:
            design = json.loads(self.design_json)
        except json.JSONDecodeError as exc:
            raise ValueError("legacy evaluation design is not JSON") from exc
        if not isinstance(design, dict) or canonical_json_text(design) != self.design_json:
            raise ValueError("legacy evaluation design must be a canonical JSON object")
        if self.execution_parameters_sha256 != self.derived_execution_parameters_sha256:
            raise ValueError("legacy evaluation parameters hash differs from the invocation")
        return self

    @property
    def design(self) -> dict[str, JsonValue]:
        value = json.loads(self.design_json)
        assert isinstance(value, dict)
        return value

    @property
    def derived_execution_parameters_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.legacy_evaluation_execution_parameters",
                "schema_version": 1,
                "harness_manifest_sha256": self.harness_manifest_sha256,
                "design_json": self.design_json,
                "input_table": self.input_table,
            }
        )

    @property
    def invocation_sha256(self) -> str:
        return canonical_sha256(self)


def build_legacy_evaluation_invocation(
    *,
    quest_id: str,
    protocol_sha256: str,
    work_order_id: str,
    work_order_sha256: str,
    work_order_node_id: str,
    work_order_node_sha256: str,
    replicate_slot_id: str,
    capability_id: str,
    capability_manifest_sha256: str,
    harness_manifest_sha256: str,
    input_table: LegacyEvaluationInputTable,
    design: dict[str, JsonValue],
    issued_at: datetime,
    deadline: datetime,
) -> LegacyEvaluationInvocation:
    """Build the self-hashed invocation without permitting a caller-selected hash."""

    design_json = canonical_json_text(design)
    execution_parameters_sha256 = canonical_sha256(
        {
            "schema_name": "aletheia.legacy_evaluation_execution_parameters",
            "schema_version": 1,
            "harness_manifest_sha256": harness_manifest_sha256,
            "design_json": design_json,
            "input_table": input_table,
        }
    )
    return LegacyEvaluationInvocation(
        quest_id=quest_id,
        protocol_sha256=protocol_sha256,
        work_order_id=work_order_id,
        work_order_sha256=work_order_sha256,
        work_order_node_id=work_order_node_id,
        work_order_node_sha256=work_order_node_sha256,
        replicate_slot_id=replicate_slot_id,
        capability_id=capability_id,
        capability_manifest_sha256=capability_manifest_sha256,
        harness_manifest_sha256=harness_manifest_sha256,
        input_table=input_table,
        design_json=design_json,
        execution_parameters_sha256=execution_parameters_sha256,
        issued_at=issued_at,
        deadline=deadline,
    )


class LegacyEvaluationMetric(LegacyEvaluationModel):
    name: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    value: float

    @model_validator(mode="after")
    def _value_is_finite(self) -> "LegacyEvaluationMetric":
        if not math.isfinite(self.value):
            raise ValueError("legacy evaluation metrics must be finite")
        return self


class LegacyArtifactKind(str, Enum):
    EVAL = "eval"
    MODEL = "model"


class LegacyEvaluationArtifact(LegacyEvaluationModel):
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    legacy_kind: LegacyArtifactKind
    relative_path: str = Field(min_length=1, max_length=1_024)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    bytes: int = Field(ge=1)
    media_type: str = Field(pattern=_MEDIA_TYPE_PATTERN)
    required: bool

    @model_validator(mode="after")
    def _path_is_relative(self) -> "LegacyEvaluationArtifact":
        _canonical_relative_path(self.relative_path, label="legacy evaluation artifact")
        return self


class LegacyEvaluationRawResult(LegacyEvaluationModel):
    """Executor-produced raw artifact index; every authority flag is permanently closed."""

    schema_name: Literal["aletheia.legacy_evaluation_raw_result"] = (
        "aletheia.legacy_evaluation_raw_result"
    )
    schema_version: Literal[1] = 1
    invocation_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    plugin_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    executor_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    metrics: tuple[LegacyEvaluationMetric, ...] = Field(min_length=1, max_length=128)
    info_json: str = Field(min_length=2, max_length=1_048_576)
    artifacts: tuple[LegacyEvaluationArtifact, ...] = Field(min_length=2, max_length=2)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    process_status: Literal["process_succeeded"] = "process_succeeded"
    scientific_outcome: Literal["not_assessed"] = "not_assessed"
    raw_artifact_only: Literal[True] = True
    executor_reported_scientific_outcome_trusted: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False
    claim_authority: Literal[False] = False

    @model_validator(mode="after")
    def _result_is_canonical(self) -> "LegacyEvaluationRawResult":
        _require_utc(self.started_at, label="legacy evaluation start time")
        _require_utc(self.ended_at, label="legacy evaluation end time")
        if self.ended_at < self.started_at:
            raise ValueError("legacy evaluation ended before it started")
        expected_metrics = tuple(sorted(self.metrics, key=lambda item: item.name))
        if self.metrics != expected_metrics or len({item.name for item in self.metrics}) != len(
            self.metrics
        ):
            raise ValueError("legacy evaluation metrics must be unique and canonical")
        expected_artifacts = tuple(sorted(self.artifacts, key=lambda item: item.artifact_key))
        if self.artifacts != expected_artifacts or len(
            {item.artifact_key for item in self.artifacts}
        ) != len(self.artifacts):
            raise ValueError("legacy evaluation artifacts must be unique and canonical")
        if len({item.relative_path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("legacy evaluation artifacts cannot share a path")
        try:
            info = json.loads(self.info_json)
        except json.JSONDecodeError as exc:
            raise ValueError("legacy evaluation info is not JSON") from exc
        if not isinstance(info, dict) or canonical_json_text(info) != self.info_json:
            raise ValueError("legacy evaluation info must be a canonical JSON object")
        return self

    @property
    def raw_result_sha256(self) -> str:
        return canonical_sha256(self)


class LegacyEvaluationValidationDisposition(str, Enum):
    VALIDATED_RAW_ARTIFACT = "validated_raw_artifact"
    REJECTED = "rejected"


def legacy_evaluation_key_id(public_key_ed25519_hex: str) -> str:
    try:
        payload = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("legacy evaluation public key must be hexadecimal") from exc
    if len(payload) != 32:
        raise ValueError("legacy evaluation public key must contain 32 bytes")
    return hashlib.sha256(payload).hexdigest()


class LegacyEvaluationValidatorPin(LegacyEvaluationModel):
    schema_name: Literal["aletheia.legacy_evaluation_validator_pin"] = (
        "aletheia.legacy_evaluation_validator_pin"
    )
    schema_version: Literal[1] = 1
    signature_domain: Literal["ALETHEIA_LEGACY_EVALUATION_VALIDATION_V1"] = (
        "ALETHEIA_LEGACY_EVALUATION_VALIDATION_V1"
    )
    validator_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    trusted_harness_manifest_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    require_grouped_protocol: bool = True
    valid_from: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _pin_is_canonical(self) -> "LegacyEvaluationValidatorPin":
        _require_utc(self.valid_from, label="legacy evaluation validator validity start")
        _require_utc(self.expires_at, label="legacy evaluation validator expiry")
        if self.expires_at <= self.valid_from:
            raise ValueError("legacy evaluation validator expiry must follow validity start")
        if self.key_id != legacy_evaluation_key_id(self.public_key_ed25519_hex):
            raise ValueError("legacy evaluation validator key id differs from its public key")
        _canonical_strings(
            self.trusted_harness_manifest_sha256s,
            label="trusted legacy evaluation harness hashes",
            required=True,
        )
        if any(
            re.fullmatch(_SHA256_PATTERN, item) is None
            for item in self.trusted_harness_manifest_sha256s
        ):
            raise ValueError("trusted legacy evaluation harness identities must be SHA-256")
        return self


class LegacyEvaluationValidationMessage(LegacyEvaluationModel):
    schema_name: Literal["aletheia.legacy_evaluation_validation"] = (
        "aletheia.legacy_evaluation_validation"
    )
    schema_version: Literal[1] = 1
    signature_domain: Literal["ALETHEIA_LEGACY_EVALUATION_VALIDATION_V1"] = (
        "ALETHEIA_LEGACY_EVALUATION_VALIDATION_V1"
    )
    raw_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    invocation_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    validator_key_id: str = Field(pattern=_SHA256_PATTERN)
    validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    fresh_artifact_sha256s: tuple[str, ...] = Field(min_length=1, max_length=5)
    disposition: LegacyEvaluationValidationDisposition
    blocker_codes: tuple[str, ...] = Field(max_length=64)
    validated_at: AwareDatetime
    eligible_for_independent_scientific_validation: bool
    scientific_outcome: Literal["not_assessed"] = "not_assessed"
    evaluator_only: Literal[True] = True
    writes_research_state: Literal[False] = False
    grants_scientific_admission: Literal[False] = False
    grants_claim_promotion: Literal[False] = False

    @model_validator(mode="after")
    def _message_is_canonical(self) -> "LegacyEvaluationValidationMessage":
        _require_utc(self.validated_at, label="legacy evaluation validation time")
        _canonical_strings(
            self.fresh_artifact_sha256s, label="fresh artifact hashes", required=True
        )
        _canonical_strings(self.blocker_codes, label="legacy evaluation blockers")
        accepted = self.disposition is LegacyEvaluationValidationDisposition.VALIDATED_RAW_ARTIFACT
        if accepted != (not self.blocker_codes):
            raise ValueError("legacy evaluation validation disposition differs from blockers")
        if accepted != self.eligible_for_independent_scientific_validation:
            raise ValueError("legacy evaluation eligibility differs from validation disposition")
        return self

    @property
    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class SignedLegacyEvaluationValidation(LegacyEvaluationModel):
    message: LegacyEvaluationValidationMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


__all__ = [
    "LegacyArtifactKind",
    "LegacyEvaluationArtifact",
    "LegacyEvaluationHarnessManifest",
    "LegacyEvaluationInputTable",
    "LegacyEvaluationInvocation",
    "LegacyEvaluationMetric",
    "LegacyEvaluationRawResult",
    "LegacyEvaluationSourceBinding",
    "LegacyEvaluationValidationDisposition",
    "LegacyEvaluationValidationMessage",
    "LegacyEvaluationValidatorPin",
    "LegacyMetricProjection",
    "SignedLegacyEvaluationValidation",
    "build_legacy_evaluation_invocation",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_sha256",
    "legacy_evaluation_key_id",
]
