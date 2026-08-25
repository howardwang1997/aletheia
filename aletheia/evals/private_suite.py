"""Evaluator-owned custody, one-time access, and retirement for private F7 suites.

No encryption key or private plaintext belongs in these manifests.  The custody registry stores
only content-addressed ciphertext envelopes and review/provenance metadata.  An operator-provided
KMS decryptor may materialize a suite exactly once after a two-person authorization binds the
acceptance configuration, four-arm baseline matrix, evaluator, and exact run plans.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Protocol, Sequence

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.baselines import (
    BaselineMatrixPlan,
    MatrixPhase,
    build_baseline_run_plans,
    validate_matrix_suite,
)
from aletheia.evals.runner import (
    EvaluationAccessRevokedError,
    EvaluationCustodyInfrastructureError,
)
from aletheia.evals.schemas import (
    EvaluationRunPlan,
    EvaluationSuite,
    EvaluationTask,
    FrozenModel,
    content_sha256,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class PrivateSuitePolicyError(RuntimeError):
    """A private-suite action would weaken or contradict its frozen custody policy."""


class PrivateSuiteTier(str, Enum):
    PILOT = "pilot"
    FRONTIER_GATE = "frontier_gate"


class PrivateTaskCase(str, Enum):
    TRUE_EFFECT = "true_effect"
    NULL_EFFECT = "null_effect"
    CONFOUNDING = "confounding"
    LABEL_ERROR = "label_error"
    DISTRIBUTION_SHIFT = "distribution_shift"
    INSUFFICIENT_SAMPLE = "insufficient_sample"


class ContaminationRisk(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class EncryptedAssetRole(str, Enum):
    SUITE_MANIFEST = "suite_manifest"
    TASK_MANIFEST = "task_manifest"
    HIDDEN_ASSET = "hidden_asset"
    GOLD_EVIDENCE = "gold_evidence"


class ContaminationSeverity(str, Enum):
    MATERIAL = "material"
    CRITICAL = "critical"


class ContaminationSource(str, Enum):
    DEVELOPMENT_DISCLOSURE = "development_disclosure"
    SUBMISSION_DECLARATION = "submission_declaration"
    SCORER_CANARY = "scorer_canary"
    ACCESS_POLICY_BREACH = "access_policy_breach"
    OPERATOR_REPORT = "operator_report"


class EncryptedAssetEnvelope(FrozenModel):
    """Content identity for ciphertext and plaintext; key material remains in an external KMS."""

    schema_version: Literal[1] = 1
    asset_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    role: EncryptedAssetRole
    storage_ref: str = Field(min_length=1, max_length=1024)
    ciphertext_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ciphertext_bytes: int = Field(gt=0)
    plaintext_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plaintext_bytes: int = Field(gt=0)
    encryption_scheme: Literal["aes-256-gcm-envelope-v1", "age-x25519-v1"]
    key_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _storage_is_role_scoped_and_normalized(self) -> "EncryptedAssetEnvelope":
        prefixes = {
            EncryptedAssetRole.SUITE_MANIFEST: "custody://suite-manifests/",
            EncryptedAssetRole.TASK_MANIFEST: "custody://task-manifests/",
            EncryptedAssetRole.HIDDEN_ASSET: "custody://hidden-assets/",
            EncryptedAssetRole.GOLD_EVIDENCE: "custody://gold-evidence/",
        }
        prefix = prefixes[self.role]
        if not self.storage_ref.startswith(prefix):
            raise ValueError(f"{self.role.value} storage must use {prefix}")
        relative = PurePosixPath(self.storage_ref[len(prefix) :])
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != self.storage_ref[len(prefix) :]
        ):
            raise ValueError("custody storage refs must be normalized relative paths")
        if self.ciphertext_sha256 == self.plaintext_sha256:
            raise ValueError("ciphertext and plaintext identities must differ")
        return self

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)


class PrivateSourceRecord(FrozenModel):
    schema_version: Literal[1] = 1
    source_type: Literal[
        "prospective_measurement",
        "commissioned_synthetic",
        "held_back_operational",
        "licensed_private_dataset",
    ]
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_id: str = Field(min_length=1, max_length=256)
    license_terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_use_authorized: Literal[True] = True
    redistribution_allowed: bool = False
    human_subjects_status: Literal["not_applicable", "approved", "exempt"] = "not_applicable"
    ethics_review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retention_deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def _ethics_evidence_is_present_when_applicable(self) -> "PrivateSourceRecord":
        if self.human_subjects_status != "not_applicable" and self.ethics_review_sha256 is None:
            raise ValueError("approved or exempt human-subjects data requires ethics evidence")
        return self


class PrivateDomainReview(FrozenModel):
    schema_version: Literal[1] = 1
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expertise_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conflict_check_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptable_conclusions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    approved: Literal[True] = True


class PrivateContaminationAssessment(FrozenModel):
    schema_version: Literal[1] = 1
    task_created_at: AwareDatetime
    prospective_after: AwareDatetime
    assessed_at: AwareDatetime
    assessor_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk: ContaminationRisk
    training_overlap: Literal["none_known", "suspected", "material"] = "none_known"
    publicly_disclosed: Literal[False] = False
    similarity_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _task_is_prospective_and_assessed_after_creation(self) -> "PrivateContaminationAssessment":
        if self.task_created_at <= self.prospective_after:
            raise ValueError("private prospective tasks must be created after the frozen cutoff")
        if self.assessed_at < self.task_created_at:
            raise ValueError("contamination assessment cannot predate task creation")
        if self.training_overlap == "material" and self.risk is not ContaminationRisk.CRITICAL:
            raise ValueError("material training overlap must be classified critical")
        return self


class PrivateTaskCustodyRecord(FrozenModel):
    schema_version: Literal[1] = 1
    private_task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    evaluation_task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain: str = Field(min_length=1, max_length=128)
    case_type: PrivateTaskCase
    structural_family_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_analog_task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: PrivateSourceRecord
    review: PrivateDomainReview
    contamination: PrivateContaminationAssessment
    task_manifest_envelope: EncryptedAssetEnvelope
    hidden_asset_envelope: EncryptedAssetEnvelope
    gold_evidence_envelope: EncryptedAssetEnvelope
    scheduled_retire_at: AwareDatetime

    @model_validator(mode="after")
    def _private_task_assets_and_review_are_bound(self) -> "PrivateTaskCustodyRecord":
        if self.validation_analog_task_manifest_sha256 == self.evaluation_task_manifest_sha256:
            raise ValueError("validation analog and private test task must have different content")
        expected_roles = (
            (self.task_manifest_envelope, EncryptedAssetRole.TASK_MANIFEST),
            (self.hidden_asset_envelope, EncryptedAssetRole.HIDDEN_ASSET),
            (self.gold_evidence_envelope, EncryptedAssetRole.GOLD_EVIDENCE),
        )
        for envelope, role in expected_roles:
            if envelope.role is not role:
                raise ValueError(f"private task {role.value} envelope has the wrong role")
        if self.gold_evidence_envelope.plaintext_sha256 != self.review.gold_evidence_sha256:
            raise ValueError("review does not bind the encrypted gold evidence plaintext")
        if self.scheduled_retire_at <= self.contamination.assessed_at:
            raise ValueError("private task retirement must follow contamination review")
        envelopes = [item[0] for item in expected_roles]
        if len({item.storage_ref for item in envelopes}) != len(envelopes):
            raise ValueError(
                "task manifest, hidden asset, and gold evidence storage must be disjoint"
            )
        if self.task_manifest_envelope.key_id_sha256 == self.hidden_asset_envelope.key_id_sha256:
            raise ValueError("task manifest and hidden asset must use separate key identities")
        if (
            self.task_manifest_envelope.access_policy_sha256
            == self.hidden_asset_envelope.access_policy_sha256
        ):
            raise ValueError("task manifest and hidden asset must use separate access policies")
        if self.gold_evidence_envelope.key_id_sha256 in {
            self.task_manifest_envelope.key_id_sha256,
            self.hidden_asset_envelope.key_id_sha256,
        }:
            raise ValueError("gold evidence must use a third key identity")
        return self


class PrivateSuitePolicy(FrozenModel):
    schema_version: Literal[1] = 1
    one_time_access: Literal[True] = True
    require_two_person_authorization: Literal[True] = True
    contamination_retirement_scope: Literal["suite"] = "suite"
    post_use_disposition: Literal["retire", "publish_as_regression"] = "retire"
    access_authorization_ttl_hours: int = Field(default=72, ge=1, le=168)
    retain_ciphertext_audit_years: int = Field(default=7, ge=1, le=100)
    plaintext_cleanup_required: Literal[True] = True


class PrivateSuiteManifest(FrozenModel):
    schema_version: Literal[1] = 1
    suite_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,79}$")
    version: str = Field(min_length=1, max_length=128)
    tier: PrivateSuiteTier
    evaluation_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_suite_envelope: EncryptedAssetEnvelope
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_owner_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_auditor_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[PrivateTaskCustodyRecord, ...] = Field(min_length=1, max_length=20)
    policy: PrivateSuitePolicy = Field(default_factory=PrivateSuitePolicy)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _suite_has_independent_complete_custody(self) -> "PrivateSuiteManifest":
        if self.evaluation_suite_envelope.role is not EncryptedAssetRole.SUITE_MANIFEST:
            raise ValueError("private suite must use a suite-manifest envelope")
        principals = {
            self.custody_owner_principal_sha256,
            self.independent_auditor_principal_sha256,
            self.research_principal_sha256,
        }
        if len(principals) != 3:
            raise ValueError(
                "custody owner, independent auditor, and research principal must differ"
            )
        task_ids = [task.private_task_id for task in self.tasks]
        task_hashes = [task.evaluation_task_manifest_sha256 for task in self.tasks]
        analogs = [task.validation_analog_task_manifest_sha256 for task in self.tasks]
        if len(task_ids) != len(set(task_ids)) or len(task_hashes) != len(set(task_hashes)):
            raise ValueError("private task IDs and evaluation task manifests must be unique")
        if len(analogs) != len(set(analogs)):
            raise ValueError("each private task must bind a distinct validation analog")
        if any(task.contamination.risk is ContaminationRisk.CRITICAL for task in self.tasks):
            raise ValueError("critical-contamination tasks cannot enter a frozen private suite")
        if any(task.contamination.training_overlap == "material" for task in self.tasks):
            raise ValueError("materially contaminated tasks cannot enter a frozen private suite")
        if any(task.scheduled_retire_at <= self.frozen_at for task in self.tasks):
            raise ValueError("private tasks must have a future retirement date at suite freeze")

        envelopes = [self.evaluation_suite_envelope]
        for task in self.tasks:
            envelopes.extend(
                (
                    task.task_manifest_envelope,
                    task.hidden_asset_envelope,
                    task.gold_evidence_envelope,
                )
            )
        if len({item.storage_ref for item in envelopes}) != len(envelopes):
            raise ValueError("every encrypted private-suite asset needs a unique storage ref")

        if self.tier is PrivateSuiteTier.FRONTIER_GATE:
            if not 10 <= len(self.tasks) <= 20:
                raise ValueError("Frontier Gate private suites require 10–20 tasks")
            if len({task.domain for task in self.tasks}) < 2:
                raise ValueError("Frontier Gate private suites require at least two domains")
            if {task.case_type for task in self.tasks} != set(PrivateTaskCase):
                raise ValueError(
                    "Frontier Gate suites must cover every required scientific case type"
                )
            if self.policy.contamination_retirement_scope != "suite":
                raise ValueError("formal Frontier Gate contamination must retire the suite version")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    def task_by_manifest(self, task_manifest_sha256: str) -> PrivateTaskCustodyRecord:
        for task in self.tasks:
            if task.evaluation_task_manifest_sha256 == task_manifest_sha256:
                return task
        raise KeyError(task_manifest_sha256)


class PrivateSuiteAccessAuthorization(FrozenModel):
    schema_version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_run_plan_sha256s: tuple[str, ...] = Field(min_length=1)
    custody_approver_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_approver_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["formal_private_test"] = "formal_private_test"
    authorized_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _authorization_is_bounded_and_unique(self) -> "PrivateSuiteAccessAuthorization":
        if self.expires_at <= self.authorized_at:
            raise ValueError("private-suite authorization must have a future expiry")
        if len(self.allowed_run_plan_sha256s) != len(set(self.allowed_run_plan_sha256s)):
            raise ValueError("authorized run plans must be unique")
        if self.custody_approver_principal_sha256 == self.independent_approver_principal_sha256:
            raise ValueError("private-suite access requires two distinct approvers")
        if self.custody_approval_evidence_sha256 == self.independent_approval_evidence_sha256:
            raise ValueError("private-suite access requires two independent approval artifacts")
        return self

    @property
    def authorization_sha256(self) -> str:
        return content_sha256(self)


class PrivateSuiteMaterializationReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    access_id: str
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_plan_sha256s: tuple[str, ...]
    task_manifest_sha256s: tuple[str, ...]
    hidden_asset_sha256s: tuple[str, ...]
    gold_evidence_sha256s: tuple[str, ...]
    storage_layout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialized_at: AwareDatetime

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class PrivatePlaintextCleanupReceipt(FrozenModel):
    """Proof that the exact evaluator plaintext set was removed or already absent."""

    schema_version: Literal[1] = 1
    access_id: str
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposed_plaintext_sha256s: tuple[str, ...] = Field(min_length=1)
    expected_file_count: int = Field(gt=0)
    removed_file_count: int = Field(ge=0)
    cleanup_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleaned_at: AwareDatetime

    @model_validator(mode="after")
    def _cleanup_counts_and_identities_are_valid(self) -> "PrivatePlaintextCleanupReceipt":
        if self.removed_file_count > self.expected_file_count:
            raise ValueError("cleanup cannot remove more files than its frozen scope")
        if any(
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            for identity in self.disposed_plaintext_sha256s
        ):
            raise ValueError("cleanup plaintext identities must be SHA-256 values")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class PrivateContaminationReport(FrozenModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ContaminationSource
    severity: ContaminationSeverity
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reporter_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detected_at: AwareDatetime

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class PrivateRetirementRecord(FrozenModel):
    schema_version: Literal[1] = 1
    retirement_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["task", "suite"]
    evaluation_task_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: Literal["post_use", "scheduled", "contamination", "operator_withdrawal"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_approver_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_approver_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retired_at: AwareDatetime

    @model_validator(mode="after")
    def _retirement_scope_is_explicit(self) -> "PrivateRetirementRecord":
        if (self.scope == "task") != (self.evaluation_task_manifest_sha256 is not None):
            raise ValueError("task retirement requires exactly one evaluation task identity")
        if self.custody_approver_principal_sha256 == self.independent_approver_principal_sha256:
            raise ValueError("retirement requires two distinct approvers")
        if self.custody_approval_evidence_sha256 == self.independent_approval_evidence_sha256:
            raise ValueError("retirement requires two independent approval artifacts")
        return self

    @property
    def retirement_sha256(self) -> str:
        return content_sha256(self)


class CustodyEventType(str, Enum):
    SUITE_REGISTERED = "suite_registered"
    ACCESS_AUTHORIZED = "access_authorized"
    ACCESS_OPENED = "access_opened"
    SUITE_MATERIALIZED = "suite_materialized"
    MATERIALIZATION_FAILED = "materialization_failed"
    CONTAMINATION_REPORTED = "contamination_reported"
    ACCESS_CLOSED = "access_closed"
    RETIRED = "retired"


class PrivateCustodyEvent(FrozenModel):
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    event_type: CustodyEventType
    occurred_at: AwareDatetime
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def calculate_hash(
        *,
        sequence: int,
        event_type: CustodyEventType,
        occurred_at: datetime,
        private_suite_manifest_sha256: str,
        payload: dict[str, Any],
        previous_event_sha256: str | None,
    ) -> str:
        return content_sha256(
            {
                "schema_version": 1,
                "sequence": sequence,
                "event_type": event_type.value,
                "occurred_at": occurred_at.isoformat(),
                "private_suite_manifest_sha256": private_suite_manifest_sha256,
                "payload": payload,
                "previous_event_sha256": previous_event_sha256,
            }
        )

    @model_validator(mode="after")
    def _event_hash_is_valid(self) -> "PrivateCustodyEvent":
        expected = self.calculate_hash(
            sequence=self.sequence,
            event_type=self.event_type,
            occurred_at=self.occurred_at,
            private_suite_manifest_sha256=self.private_suite_manifest_sha256,
            payload=self.payload,
            previous_event_sha256=self.previous_event_sha256,
        )
        if self.event_sha256 != expected:
            raise ValueError("private custody event hash is invalid")
        return self


class PrivateCustodyState(FrozenModel):
    schema_version: Literal[1] = 1
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered: bool
    authorization_ids: tuple[str, ...]
    opened_access_id: str | None = None
    materialization_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cleanup_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    materialization_failed: bool = False
    contamination_report_ids: tuple[str, ...]
    retired_task_manifest_sha256s: tuple[str, ...]
    access_closed: bool
    suite_retired: bool


class PrivateCustodyLedger:
    """Concurrency-safe, hash-chained registry that never stores private plaintext or keys."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve(strict=False)

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _decode(handle: Any) -> list[PrivateCustodyEvent]:
        handle.seek(0)
        events: list[PrivateCustodyEvent] = []
        previous: str | None = None
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise PrivateSuitePolicyError(f"blank custody ledger line at {line_number}")
            try:
                event = PrivateCustodyEvent.model_validate_json(raw)
            except Exception as exc:
                raise PrivateSuitePolicyError(
                    f"invalid custody ledger record at line {line_number}: {exc}"
                ) from exc
            if event.sequence != line_number or event.previous_event_sha256 != previous:
                raise PrivateSuitePolicyError(f"custody ledger chain breaks at line {line_number}")
            previous = event.event_sha256
            events.append(event)
        return events

    def events(self) -> tuple[PrivateCustodyEvent, ...]:
        self._ensure_file()
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return tuple(self._decode(handle))
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append_if(
        self,
        *,
        event_type: CustodyEventType,
        suite_sha256: str,
        payload: dict[str, Any],
        predicate: Any,
        occurred_at: datetime | None = None,
    ) -> PrivateCustodyEvent | None:
        self._ensure_file()
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                events = self._decode(handle)
                if not predicate(events):
                    return None
                sequence = len(events) + 1
                previous = events[-1].event_sha256 if events else None
                timestamp = occurred_at or _now()
                digest = PrivateCustodyEvent.calculate_hash(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    private_suite_manifest_sha256=suite_sha256,
                    payload=payload,
                    previous_event_sha256=previous,
                )
                event = PrivateCustodyEvent(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    private_suite_manifest_sha256=suite_sha256,
                    payload=payload,
                    previous_event_sha256=previous,
                    event_sha256=digest,
                )
                handle.seek(0, os.SEEK_END)
                handle.write(event.model_dump_json(exclude_none=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return event
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _manifest(events: Sequence[PrivateCustodyEvent], suite_sha256: str) -> PrivateSuiteManifest:
        matches = [
            event
            for event in events
            if event.event_type is CustodyEventType.SUITE_REGISTERED
            and event.private_suite_manifest_sha256 == suite_sha256
        ]
        if len(matches) != 1:
            raise PrivateSuitePolicyError("private suite is not uniquely registered")
        manifest = PrivateSuiteManifest.model_validate(matches[0].payload["manifest"])
        if manifest.manifest_sha256 != suite_sha256:
            raise PrivateSuitePolicyError("registered private-suite manifest hash differs")
        return manifest

    @staticmethod
    def _authorization(
        events: Sequence[PrivateCustodyEvent], suite_sha256: str, authorization_id: str
    ) -> PrivateSuiteAccessAuthorization:
        matches = [
            event
            for event in events
            if event.event_type is CustodyEventType.ACCESS_AUTHORIZED
            and event.private_suite_manifest_sha256 == suite_sha256
            and event.payload.get("authorization", {}).get("authorization_id") == authorization_id
        ]
        if len(matches) != 1:
            raise PrivateSuitePolicyError("private-suite authorization is unavailable or ambiguous")
        return PrivateSuiteAccessAuthorization.model_validate(matches[0].payload["authorization"])

    def register_suite(self, manifest: PrivateSuiteManifest) -> None:
        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            for event in events:
                if event.event_type is not CustodyEventType.SUITE_REGISTERED:
                    continue
                existing = PrivateSuiteManifest.model_validate(event.payload["manifest"])
                if existing.suite_id == manifest.suite_id and existing.version == manifest.version:
                    if existing.manifest_sha256 == manifest.manifest_sha256:
                        return False
                    raise PrivateSuitePolicyError(
                        "private suite ID/version is already registered with different content"
                    )
            return True

        self._append_if(
            event_type=CustodyEventType.SUITE_REGISTERED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "manifest_sha256": manifest.manifest_sha256,
                "manifest": manifest.model_dump(mode="json"),
            },
            predicate=predicate,
        )

    @staticmethod
    def _validate_authorization_binding(
        manifest: PrivateSuiteManifest, authorization: PrivateSuiteAccessAuthorization
    ) -> None:
        expected = (
            manifest.manifest_sha256,
            manifest.evaluation_suite_manifest_sha256,
            manifest.evaluator_manifest_sha256,
            manifest.baseline_matrix_manifest_sha256,
            manifest.acceptance_config_sha256,
        )
        actual = (
            authorization.private_suite_manifest_sha256,
            authorization.evaluation_suite_manifest_sha256,
            authorization.evaluator_manifest_sha256,
            authorization.baseline_matrix_manifest_sha256,
            authorization.acceptance_config_sha256,
        )
        if actual != expected:
            raise PrivateSuitePolicyError(
                "authorization differs from frozen private-suite bindings"
            )
        if (
            authorization.custody_approver_principal_sha256
            != manifest.custody_owner_principal_sha256
            or authorization.independent_approver_principal_sha256
            != manifest.independent_auditor_principal_sha256
        ):
            raise PrivateSuitePolicyError(
                "authorization is not approved by the frozen custody roles"
            )
        if manifest.research_principal_sha256 in {
            authorization.custody_approver_principal_sha256,
            authorization.independent_approver_principal_sha256,
        }:
            raise PrivateSuitePolicyError(
                "the research principal cannot approve private-test access"
            )
        if authorization.authorized_at < manifest.frozen_at:
            raise PrivateSuitePolicyError("private-test access cannot predate the suite freeze")
        if authorization.expires_at - authorization.authorized_at > timedelta(
            hours=manifest.policy.access_authorization_ttl_hours
        ):
            raise PrivateSuitePolicyError("private-test authorization exceeds the frozen TTL")
        if (
            manifest.tier is PrivateSuiteTier.FRONTIER_GATE
            and len(authorization.allowed_run_plan_sha256s) != 4
        ):
            raise PrivateSuitePolicyError(
                "Frontier Gate authorization must bind exactly four run plans"
            )

    def authorize_access(
        self, manifest: PrivateSuiteManifest, authorization: PrivateSuiteAccessAuthorization
    ) -> None:
        self._validate_authorization_binding(manifest, authorization)

        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            registered = self._manifest(events, manifest.manifest_sha256)
            if registered != manifest:
                raise PrivateSuitePolicyError(
                    "authorization manifest differs from custody registry"
                )
            state = self._state(events, manifest)
            if state.opened_access_id is not None or state.suite_retired:
                raise PrivateSuitePolicyError("cannot authorize an opened or retired private suite")
            for event in events:
                if event.event_type is not CustodyEventType.ACCESS_AUTHORIZED:
                    continue
                if event.private_suite_manifest_sha256 != manifest.manifest_sha256:
                    continue
                existing = PrivateSuiteAccessAuthorization.model_validate(
                    event.payload["authorization"]
                )
                if existing.authorization_id == authorization.authorization_id:
                    if existing.authorization_sha256 == authorization.authorization_sha256:
                        return False
                    raise PrivateSuitePolicyError(
                        "authorization ID is already bound to different content"
                    )
                frozen_binding = (
                    existing.evaluation_suite_manifest_sha256,
                    existing.evaluator_manifest_sha256,
                    existing.baseline_matrix_manifest_sha256,
                    existing.acceptance_config_sha256,
                    existing.allowed_run_plan_sha256s,
                )
                new_binding = (
                    authorization.evaluation_suite_manifest_sha256,
                    authorization.evaluator_manifest_sha256,
                    authorization.baseline_matrix_manifest_sha256,
                    authorization.acceptance_config_sha256,
                    authorization.allowed_run_plan_sha256s,
                )
                if frozen_binding != new_binding:
                    raise PrivateSuitePolicyError(
                        "replacement authorization cannot change the frozen test configuration"
                    )
            return True

        self._append_if(
            event_type=CustodyEventType.ACCESS_AUTHORIZED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "authorization_sha256": authorization.authorization_sha256,
                "authorization": authorization.model_dump(mode="json"),
            },
            predicate=predicate,
        )

    def open_access(
        self,
        manifest: PrivateSuiteManifest,
        authorization_id: str,
        *,
        opened_at: datetime | None = None,
    ) -> PrivateCustodyEvent:
        timestamp = opened_at or _now()

        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            registered = self._manifest(events, manifest.manifest_sha256)
            if registered != manifest:
                raise PrivateSuitePolicyError("access manifest differs from custody registry")
            authorization = self._authorization(events, manifest.manifest_sha256, authorization_id)
            self._validate_authorization_binding(manifest, authorization)
            state = self._state(events, manifest)
            if state.opened_access_id is not None:
                raise PrivateSuitePolicyError("private suite access has already been opened once")
            if state.suite_retired or state.retired_task_manifest_sha256s:
                raise PrivateSuitePolicyError(
                    "retired or contaminated private suite cannot be opened"
                )
            if timestamp < authorization.authorized_at or timestamp > authorization.expires_at:
                raise PrivateSuitePolicyError("private-suite authorization is not currently valid")
            if any(task.scheduled_retire_at <= timestamp for task in manifest.tasks):
                raise PrivateSuitePolicyError("a private task reached retirement before access")
            return True

        event = self._append_if(
            event_type=CustodyEventType.ACCESS_OPENED,
            suite_sha256=manifest.manifest_sha256,
            payload={"access_id": authorization_id},
            predicate=predicate,
            occurred_at=timestamp,
        )
        if event is None:  # pragma: no cover - predicate claims or raises.
            raise PrivateSuitePolicyError("private-suite access was not opened")
        return event

    def record_materialized(
        self, manifest: PrivateSuiteManifest, receipt: PrivateSuiteMaterializationReceipt
    ) -> None:
        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            state = self._state(events, manifest)
            if state.opened_access_id != receipt.access_id:
                raise PrivateSuitePolicyError("materialization does not match the opened access")
            if state.materialization_failed or state.suite_retired:
                raise PrivateSuitePolicyError("failed or retired access cannot materialize")
            if state.materialization_receipt_sha256 is not None:
                if state.materialization_receipt_sha256 == receipt.receipt_sha256:
                    return False
                raise PrivateSuitePolicyError("private suite was already materialized differently")
            return True

        self._append_if(
            event_type=CustodyEventType.SUITE_MATERIALIZED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "receipt_sha256": receipt.receipt_sha256,
                "receipt": receipt.model_dump(mode="json"),
            },
            predicate=predicate,
        )

    def record_materialization_failure(
        self,
        manifest: PrivateSuiteManifest,
        access_id: str,
        error_sha256: str,
        *,
        failed_at: datetime | None = None,
    ) -> None:
        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            state = self._state(events, manifest)
            if state.opened_access_id != access_id:
                raise PrivateSuitePolicyError("failure does not match the opened access")
            if state.materialization_receipt_sha256 is not None:
                raise PrivateSuitePolicyError("a materialized suite cannot become failed")
            return not state.materialization_failed

        self._append_if(
            event_type=CustodyEventType.MATERIALIZATION_FAILED,
            suite_sha256=manifest.manifest_sha256,
            payload={"access_id": access_id, "error_sha256": error_sha256},
            predicate=predicate,
            occurred_at=failed_at,
        )

    def report_contamination(
        self, manifest: PrivateSuiteManifest, report: PrivateContaminationReport
    ) -> None:
        if report.private_suite_manifest_sha256 != manifest.manifest_sha256:
            raise PrivateSuitePolicyError("contamination report belongs to another private suite")

        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            registered = self._manifest(events, manifest.manifest_sha256)
            if registered != manifest:
                raise PrivateSuitePolicyError("contamination manifest differs from registry")
            try:
                manifest.task_by_manifest(report.evaluation_task_manifest_sha256)
            except KeyError as exc:
                raise PrivateSuitePolicyError(
                    "contamination report names a non-suite task"
                ) from exc
            for event in events:
                if event.event_type is not CustodyEventType.CONTAMINATION_REPORTED:
                    continue
                if event.private_suite_manifest_sha256 != manifest.manifest_sha256:
                    continue
                existing = PrivateContaminationReport.model_validate(event.payload["report"])
                if existing.report_id == report.report_id:
                    if existing.report_sha256 == report.report_sha256:
                        return False
                    raise PrivateSuitePolicyError(
                        "contamination report ID is bound to different content"
                    )
            return True

        self._append_if(
            event_type=CustodyEventType.CONTAMINATION_REPORTED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "report_sha256": report.report_sha256,
                "report": report.model_dump(mode="json"),
            },
            predicate=predicate,
            occurred_at=report.detected_at,
        )

    def close_access(
        self,
        manifest: PrivateSuiteManifest,
        access_id: str,
        cleanup_receipt: PrivatePlaintextCleanupReceipt,
        *,
        closed_at: datetime | None = None,
    ) -> None:
        if (
            cleanup_receipt.private_suite_manifest_sha256 != manifest.manifest_sha256
            or cleanup_receipt.access_id != access_id
        ):
            raise PrivateSuitePolicyError("cleanup receipt does not match the private access")

        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            state = self._state(events, manifest)
            if state.opened_access_id != access_id:
                raise PrivateSuitePolicyError("close does not match the opened access")
            if state.materialization_receipt_sha256 is None and not state.materialization_failed:
                raise PrivateSuitePolicyError(
                    "cannot close an access without materialization or a recorded failure"
                )
            if state.access_closed:
                if state.cleanup_receipt_sha256 != cleanup_receipt.receipt_sha256:
                    raise PrivateSuitePolicyError(
                        "closed access is bound to a different cleanup receipt"
                    )
                return False
            return True

        self._append_if(
            event_type=CustodyEventType.ACCESS_CLOSED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "access_id": access_id,
                "cleanup_receipt_sha256": cleanup_receipt.receipt_sha256,
                "cleanup_receipt": cleanup_receipt.model_dump(mode="json"),
            },
            predicate=predicate,
            occurred_at=closed_at,
        )

    def retire(self, manifest: PrivateSuiteManifest, retirement: PrivateRetirementRecord) -> None:
        if retirement.private_suite_manifest_sha256 != manifest.manifest_sha256:
            raise PrivateSuitePolicyError("retirement belongs to another private suite")
        if retirement.retired_at < manifest.frozen_at:
            raise PrivateSuitePolicyError("private-suite retirement cannot predate its freeze")
        if (
            retirement.custody_approver_principal_sha256 != manifest.custody_owner_principal_sha256
            or retirement.independent_approver_principal_sha256
            != manifest.independent_auditor_principal_sha256
        ):
            raise PrivateSuitePolicyError("retirement lacks the frozen two-person approval")
        if retirement.evaluation_task_manifest_sha256 is not None:
            try:
                manifest.task_by_manifest(retirement.evaluation_task_manifest_sha256)
            except KeyError as exc:
                raise PrivateSuitePolicyError("retirement names a non-suite task") from exc

        def predicate(events: list[PrivateCustodyEvent]) -> bool:
            self._manifest(events, manifest.manifest_sha256)
            for event in events:
                if event.event_type is not CustodyEventType.RETIRED:
                    continue
                if event.private_suite_manifest_sha256 != manifest.manifest_sha256:
                    continue
                existing = PrivateRetirementRecord.model_validate(event.payload["retirement"])
                if existing.retirement_id == retirement.retirement_id:
                    if existing.retirement_sha256 == retirement.retirement_sha256:
                        return False
                    raise PrivateSuitePolicyError("retirement ID is bound to different content")
            return True

        self._append_if(
            event_type=CustodyEventType.RETIRED,
            suite_sha256=manifest.manifest_sha256,
            payload={
                "retirement_sha256": retirement.retirement_sha256,
                "retirement": retirement.model_dump(mode="json"),
            },
            predicate=predicate,
            occurred_at=retirement.retired_at,
        )

    @staticmethod
    def _state(
        events: Sequence[PrivateCustodyEvent], manifest: PrivateSuiteManifest
    ) -> PrivateCustodyState:
        relevant = [
            event
            for event in events
            if event.private_suite_manifest_sha256 == manifest.manifest_sha256
        ]
        registered = any(
            event.event_type is CustodyEventType.SUITE_REGISTERED for event in relevant
        )
        authorizations: list[str] = []
        opened: str | None = None
        materialized: str | None = None
        cleanup_receipt: str | None = None
        failed = False
        report_ids: list[str] = []
        retired_tasks: set[str] = set()
        closed = False
        suite_retired = False
        for event in relevant:
            if event.event_type is CustodyEventType.ACCESS_AUTHORIZED:
                authorization = PrivateSuiteAccessAuthorization.model_validate(
                    event.payload["authorization"]
                )
                authorizations.append(authorization.authorization_id)
            elif event.event_type is CustodyEventType.ACCESS_OPENED:
                opened = str(event.payload["access_id"])
            elif event.event_type is CustodyEventType.SUITE_MATERIALIZED:
                materialized = str(event.payload["receipt_sha256"])
            elif event.event_type is CustodyEventType.MATERIALIZATION_FAILED:
                failed = True
                suite_retired = True
            elif event.event_type is CustodyEventType.CONTAMINATION_REPORTED:
                report = PrivateContaminationReport.model_validate(event.payload["report"])
                report_ids.append(report.report_id)
                retired_tasks.add(report.evaluation_task_manifest_sha256)
                if (
                    report.severity is ContaminationSeverity.CRITICAL
                    or manifest.policy.contamination_retirement_scope == "suite"
                ):
                    suite_retired = True
            elif event.event_type is CustodyEventType.ACCESS_CLOSED:
                closed = True
                cleanup_receipt = str(event.payload["cleanup_receipt_sha256"])
                if manifest.policy.one_time_access:
                    suite_retired = True
            elif event.event_type is CustodyEventType.RETIRED:
                retirement = PrivateRetirementRecord.model_validate(event.payload["retirement"])
                if retirement.scope == "suite":
                    suite_retired = True
                else:
                    assert retirement.evaluation_task_manifest_sha256 is not None
                    retired_tasks.add(retirement.evaluation_task_manifest_sha256)
        return PrivateCustodyState(
            private_suite_manifest_sha256=manifest.manifest_sha256,
            registered=registered,
            authorization_ids=tuple(authorizations),
            opened_access_id=opened,
            materialization_receipt_sha256=materialized,
            cleanup_receipt_sha256=cleanup_receipt,
            materialization_failed=failed,
            contamination_report_ids=tuple(report_ids),
            retired_task_manifest_sha256s=tuple(sorted(retired_tasks)),
            access_closed=closed,
            suite_retired=suite_retired,
        )

    def state(self, manifest: PrivateSuiteManifest) -> PrivateCustodyState:
        events = self.events()
        registered = self._manifest(events, manifest.manifest_sha256)
        if registered != manifest:
            raise PrivateSuitePolicyError("private-suite manifest differs from custody registry")
        return self._state(events, manifest)

    def authorization(
        self, manifest: PrivateSuiteManifest, authorization_id: str
    ) -> PrivateSuiteAccessAuthorization:
        return self._authorization(self.events(), manifest.manifest_sha256, authorization_id)

    def cleanup_receipt(self, manifest: PrivateSuiteManifest) -> PrivatePlaintextCleanupReceipt:
        matches = [
            event
            for event in self.events()
            if event.event_type is CustodyEventType.ACCESS_CLOSED
            and event.private_suite_manifest_sha256 == manifest.manifest_sha256
        ]
        if len(matches) != 1:
            raise PrivateSuitePolicyError("private cleanup receipt is unavailable or ambiguous")
        receipt = PrivatePlaintextCleanupReceipt.model_validate(
            matches[0].payload["cleanup_receipt"]
        )
        if receipt.receipt_sha256 != matches[0].payload["cleanup_receipt_sha256"]:
            raise PrivateSuitePolicyError("private cleanup receipt hash differs")
        return receipt

    def materialization_receipt(
        self, manifest: PrivateSuiteManifest
    ) -> PrivateSuiteMaterializationReceipt:
        matches = [
            event
            for event in self.events()
            if event.event_type is CustodyEventType.SUITE_MATERIALIZED
            and event.private_suite_manifest_sha256 == manifest.manifest_sha256
        ]
        if len(matches) != 1:
            raise PrivateSuitePolicyError(
                "private materialization receipt is unavailable or ambiguous"
            )
        receipt = PrivateSuiteMaterializationReceipt.model_validate(matches[0].payload["receipt"])
        if receipt.receipt_sha256 != matches[0].payload["receipt_sha256"]:
            raise PrivateSuitePolicyError("private materialization receipt hash differs")
        if receipt.private_suite_manifest_sha256 != manifest.manifest_sha256:
            raise PrivateSuitePolicyError(
                "private materialization receipt belongs to another suite"
            )
        return receipt

    def assert_integrity(self) -> dict[str, Any]:
        self._ensure_file()
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                raw = handle.read()
                handle.seek(0)
                events = self._decode(handle)
                return {
                    "path": str(self.path),
                    "events": len(events),
                    "head_sha256": events[-1].event_sha256 if events else None,
                    "file_sha256": _bytes_sha256(raw.encode("utf-8")),
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PrivateCiphertextStore(Protocol):
    def read_ciphertext(self, storage_ref: str) -> bytes: ...


class PrivateEnvelopeDecryptor(Protocol):
    def decrypt(self, envelope: EncryptedAssetEnvelope, ciphertext: bytes) -> bytes: ...


@dataclass(frozen=True)
class MaterializedPrivateSuite:
    suite: EvaluationSuite
    tasks: tuple[EvaluationTask, ...]
    receipt: PrivateSuiteMaterializationReceipt


def _verified_ciphertext(store: PrivateCiphertextStore, envelope: EncryptedAssetEnvelope) -> bytes:
    ciphertext = bytes(store.read_ciphertext(envelope.storage_ref))
    if len(ciphertext) != envelope.ciphertext_bytes:
        raise PrivateSuitePolicyError("private ciphertext byte count differs from its envelope")
    if _bytes_sha256(ciphertext) != envelope.ciphertext_sha256:
        raise PrivateSuitePolicyError("private ciphertext hash differs from its envelope")
    return ciphertext


def _verified_plaintext(
    decryptor: PrivateEnvelopeDecryptor,
    envelope: EncryptedAssetEnvelope,
    ciphertext: bytes,
) -> bytes:
    plaintext = bytes(decryptor.decrypt(envelope, ciphertext))
    if len(plaintext) != envelope.plaintext_bytes:
        raise PrivateSuitePolicyError("private plaintext byte count differs from its envelope")
    if _bytes_sha256(plaintext) != envelope.plaintext_sha256:
        raise PrivateSuitePolicyError("private plaintext hash differs from its envelope")
    return plaintext


def _write_new_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        path.chmod(0o400)
    except OSError:
        pass


def _private_hidden_relative(suite_id: str, hidden_asset_ref: str) -> PurePosixPath:
    prefix = f"evaluator://hidden/private/{suite_id}/"
    if not hidden_asset_ref.startswith(prefix):
        raise PrivateSuitePolicyError("private hidden asset ref is outside its suite scope")
    hidden_prefix = "evaluator://hidden/"
    raw = hidden_asset_ref[len(hidden_prefix) :]
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
        or relative.parts[:2] != ("private", suite_id)
    ):
        raise PrivateSuitePolicyError("private hidden asset ref is not normalized in suite scope")
    return relative


def _remove_created_private_files(paths: Sequence[Path]) -> None:
    failures: list[str] = []
    for path in reversed(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failures.append(str(path))
    if failures:
        raise PrivateSuitePolicyError(
            "failed to clean partially materialized private plaintext: " + ", ".join(failures)
        )


def materialize_private_suite(
    *,
    manifest: PrivateSuiteManifest,
    authorization: PrivateSuiteAccessAuthorization,
    baseline_matrix: BaselineMatrixPlan,
    ledger: PrivateCustodyLedger,
    store: PrivateCiphertextStore,
    decryptor: PrivateEnvelopeDecryptor,
    evaluator_root: Path,
    opened_at: datetime | None = None,
) -> MaterializedPrivateSuite:
    """Consume the one-time access, verify every envelope, and stage evaluator-only plaintext."""

    ledger._validate_authorization_binding(manifest, authorization)
    if baseline_matrix.manifest_sha256 != manifest.baseline_matrix_manifest_sha256:
        raise PrivateSuitePolicyError("baseline matrix differs from private-suite custody")
    if baseline_matrix.phase is not MatrixPhase.TEST:
        raise PrivateSuitePolicyError("private prospective access requires a frozen test matrix")
    if baseline_matrix.evaluator_manifest_sha256 != manifest.evaluator_manifest_sha256:
        raise PrivateSuitePolicyError(
            "baseline matrix evaluator differs from private-suite custody"
        )

    current_state = ledger.state(manifest)
    if current_state.opened_access_id is not None:
        raise PrivateSuitePolicyError("private suite access has already been opened once")
    root = Path(evaluator_root).expanduser().resolve(strict=False)
    plaintext_scopes = (
        root / "private_manifests" / manifest.suite_id,
        root / "hidden_assets" / "private" / manifest.suite_id,
    )
    if any(
        scope.resolve(strict=False) == root or root not in scope.resolve(strict=False).parents
        for scope in plaintext_scopes
    ):
        raise PrivateSuitePolicyError("private plaintext scope escaped evaluator storage")
    if any(scope.exists() or scope.is_symlink() for scope in plaintext_scopes):
        raise PrivateSuitePolicyError(
            "private plaintext scopes must be absent before spending one-time access"
        )

    envelopes = [manifest.evaluation_suite_envelope]
    for record in manifest.tasks:
        envelopes.extend(
            (
                record.task_manifest_envelope,
                record.hidden_asset_envelope,
                record.gold_evidence_envelope,
            )
        )
    ciphertext = {item.storage_ref: _verified_ciphertext(store, item) for item in envelopes}
    ledger.open_access(manifest, authorization.authorization_id, opened_at=opened_at)
    created_paths: list[Path] = []

    try:
        plaintext = {
            item.storage_ref: _verified_plaintext(decryptor, item, ciphertext[item.storage_ref])
            for item in envelopes
        }
        suite_bytes = plaintext[manifest.evaluation_suite_envelope.storage_ref]
        suite = EvaluationSuite.model_validate_json(suite_bytes)
        if suite.manifest_sha256 != manifest.evaluation_suite_manifest_sha256:
            raise PrivateSuitePolicyError(
                "decrypted evaluation suite differs from custody metadata"
            )
        validate_matrix_suite(baseline_matrix, suite)
        materialized_plans = build_baseline_run_plans(baseline_matrix, suite)
        run_plans = tuple(item.run_plan for item in materialized_plans)
        if {plan.manifest_sha256 for plan in run_plans} != set(
            authorization.allowed_run_plan_sha256s
        ):
            raise PrivateSuitePolicyError("authorized run plans differ from the baseline matrix")

        tasks: list[EvaluationTask] = []
        hidden_bytes: dict[str, bytes] = {}
        task_manifest_bytes: dict[str, bytes] = {}
        for record in manifest.tasks:
            task_raw = plaintext[record.task_manifest_envelope.storage_ref]
            task = EvaluationTask.model_validate_json(task_raw)
            if task.manifest_sha256 != record.evaluation_task_manifest_sha256:
                raise PrivateSuitePolicyError(
                    "decrypted task manifest differs from custody metadata"
                )
            if task.manifest_sha256 not in suite.task_manifest_sha256s:
                raise PrivateSuitePolicyError("decrypted task is outside the evaluation suite")
            if not task.contamination_policy.retire_after_access:
                raise PrivateSuitePolicyError("private test tasks must retire after access")
            _private_hidden_relative(manifest.suite_id, task.hidden_asset_ref)
            hidden = plaintext[record.hidden_asset_envelope.storage_ref]
            if _bytes_sha256(hidden) != task.hidden_asset_sha256:
                raise PrivateSuitePolicyError("decrypted hidden asset differs from task manifest")
            for plan in run_plans:
                planned = sum(
                    slot.task_manifest_sha256 == task.manifest_sha256 for slot in plan.slots
                )
                if planned > task.contamination_policy.test_access_limit:
                    raise PrivateSuitePolicyError("run plan exceeds private task access policy")
            gold = plaintext[record.gold_evidence_envelope.storage_ref]
            if _bytes_sha256(gold) != record.review.gold_evidence_sha256:
                raise PrivateSuitePolicyError("decrypted gold evidence differs from domain review")
            tasks.append(task)
            hidden_bytes[task.hidden_asset_ref] = hidden
            task_manifest_bytes[record.private_task_id] = task_raw
        if {task.manifest_sha256 for task in tasks} != set(suite.task_manifest_sha256s):
            raise PrivateSuitePolicyError("private custody tasks do not exactly cover the suite")

        file_payloads: list[tuple[str, Path, bytes]] = []
        suite_path = root / "private_manifests" / manifest.suite_id / "suite.v1.json"
        file_payloads.append(("suite_manifest", suite_path, suite_bytes))
        for private_task_id, raw in task_manifest_bytes.items():
            path = (
                root / "private_manifests" / manifest.suite_id / f"{private_task_id}.task.v1.json"
            )
            file_payloads.append(("task_manifest", path, raw))
        for ref, raw in hidden_bytes.items():
            relative = _private_hidden_relative(manifest.suite_id, ref)
            path = root / "hidden_assets" / Path(*relative.parts)
            file_payloads.append(("hidden_asset", path, raw))

        for _role, path, _raw in file_payloads:
            resolved = path.resolve(strict=False)
            if resolved == root or root not in resolved.parents:
                raise PrivateSuitePolicyError("private plaintext path escaped evaluator storage")
            if path.exists() or path.is_symlink():
                raise PrivateSuitePolicyError(
                    "refusing to replace existing evaluator private plaintext"
                )

        layout: list[dict[str, str]] = []
        for role, path, raw in file_payloads:
            _write_new_private_file(path, raw)
            created_paths.append(path)
            layout.append(
                {
                    "role": role,
                    "relative_path": str(path.relative_to(root)),
                    "sha256": _bytes_sha256(raw),
                }
            )

        receipt = PrivateSuiteMaterializationReceipt(
            access_id=authorization.authorization_id,
            private_suite_manifest_sha256=manifest.manifest_sha256,
            evaluation_suite_manifest_sha256=suite.manifest_sha256,
            baseline_matrix_manifest_sha256=baseline_matrix.manifest_sha256,
            run_plan_sha256s=tuple(plan.manifest_sha256 for plan in run_plans),
            task_manifest_sha256s=tuple(task.manifest_sha256 for task in tasks),
            hidden_asset_sha256s=tuple(task.hidden_asset_sha256 for task in tasks),
            gold_evidence_sha256s=tuple(
                record.review.gold_evidence_sha256 for record in manifest.tasks
            ),
            storage_layout_sha256=content_sha256({"layout": layout}),
            materialized_at=_now(),
        )
        ledger.record_materialized(manifest, receipt)
        return MaterializedPrivateSuite(suite=suite, tasks=tuple(tasks), receipt=receipt)
    except Exception as exc:
        cleanup_error: Exception | None = None
        try:
            _remove_created_private_files(created_paths)
        except Exception as cleanup_exc:
            cleanup_error = cleanup_exc
        try:
            failure_identity = _bytes_sha256(
                f"{exc}; cleanup={cleanup_error}".encode("utf-8")
                if cleanup_error is not None
                else str(exc).encode("utf-8")
            )
            fail_private_suite_materialization(
                manifest=manifest,
                ledger=ledger,
                access_id=authorization.authorization_id,
                evaluator_root=root,
                error_evidence_sha256=failure_identity,
            )
            cleanup_error = None
        except Exception as recovery_exc:
            if cleanup_error is None:
                cleanup_error = recovery_exc
        if cleanup_error is not None:
            raise PrivateSuitePolicyError(
                f"private-suite materialization and plaintext cleanup failed: {cleanup_error}"
            ) from exc
        if isinstance(exc, PrivateSuitePolicyError):
            raise
        raise PrivateSuitePolicyError(f"private-suite materialization failed: {exc}") from exc


def _private_file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PrivateSuitePolicyError("private plaintext cleanup encountered a non-regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_verified_private_file(path: Path, envelope: EncryptedAssetEnvelope) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PrivateSuitePolicyError(
            "materialized private plaintext is unavailable or non-regular"
        )
    data = path.read_bytes()
    if len(data) != envelope.plaintext_bytes or _bytes_sha256(data) != envelope.plaintext_sha256:
        raise PrivateSuitePolicyError("materialized private plaintext differs from custody")
    return data


def load_materialized_private_suite(
    *,
    manifest: PrivateSuiteManifest,
    ledger: PrivateCustodyLedger,
    access_id: str,
    evaluator_root: Path,
) -> MaterializedPrivateSuite:
    """Reconstruct evaluator models after a post-commit crash without decrypting or reopening."""

    state = ledger.state(manifest)
    if (
        state.opened_access_id != access_id
        or state.materialization_receipt_sha256 is None
        or state.materialization_failed
        or state.access_closed
        or state.suite_retired
    ):
        raise PrivateSuitePolicyError("private suite is not actively materialized")
    receipt = ledger.materialization_receipt(manifest)
    if receipt.access_id != access_id:
        raise PrivateSuitePolicyError("materialization receipt belongs to another access")

    root = Path(evaluator_root).expanduser().resolve(strict=False)
    manifest_scope = root / "private_manifests" / manifest.suite_id
    hidden_scope = root / "hidden_assets" / "private" / manifest.suite_id
    for scope in (manifest_scope, hidden_scope):
        resolved = scope.resolve(strict=False)
        if resolved == root or root not in resolved.parents or scope.is_symlink():
            raise PrivateSuitePolicyError("materialized private scope escaped evaluator storage")
    suite_raw = _read_verified_private_file(
        manifest_scope / "suite.v1.json", manifest.evaluation_suite_envelope
    )
    suite = EvaluationSuite.model_validate_json(suite_raw)
    if suite.manifest_sha256 != manifest.evaluation_suite_manifest_sha256:
        raise PrivateSuitePolicyError("materialized evaluation suite differs from custody")

    tasks: list[EvaluationTask] = []
    for record in manifest.tasks:
        task_raw = _read_verified_private_file(
            manifest_scope / f"{record.private_task_id}.task.v1.json",
            record.task_manifest_envelope,
        )
        task = EvaluationTask.model_validate_json(task_raw)
        if task.manifest_sha256 != record.evaluation_task_manifest_sha256:
            raise PrivateSuitePolicyError("materialized evaluation task differs from custody")
        relative = _private_hidden_relative(manifest.suite_id, task.hidden_asset_ref)
        _read_verified_private_file(
            root / "hidden_assets" / Path(*relative.parts),
            record.hidden_asset_envelope,
        )
        tasks.append(task)

    task_hashes = tuple(task.manifest_sha256 for task in tasks)
    hidden_hashes = tuple(task.hidden_asset_sha256 for task in tasks)
    gold_hashes = tuple(record.review.gold_evidence_sha256 for record in manifest.tasks)
    if (
        set(task_hashes) != set(suite.task_manifest_sha256s)
        or receipt.evaluation_suite_manifest_sha256 != suite.manifest_sha256
        or receipt.task_manifest_sha256s != task_hashes
        or receipt.hidden_asset_sha256s != hidden_hashes
        or receipt.gold_evidence_sha256s != gold_hashes
    ):
        raise PrivateSuitePolicyError(
            "materialized private suite differs from its committed receipt"
        )
    return MaterializedPrivateSuite(suite=suite, tasks=tuple(tasks), receipt=receipt)


def cleanup_materialized_private_suite(
    *,
    manifest: PrivateSuiteManifest,
    ledger: PrivateCustodyLedger,
    access_id: str,
    evaluator_root: Path,
    cleaned_at: datetime | None = None,
) -> PrivatePlaintextCleanupReceipt:
    """Remove only the verified plaintext scope, tolerating an interrupted earlier cleanup."""

    state = ledger.state(manifest)
    if state.opened_access_id != access_id:
        raise PrivateSuitePolicyError("cleanup does not match the opened private access")
    if state.materialization_receipt_sha256 is None and not state.materialization_failed:
        raise PrivateSuitePolicyError(
            "private plaintext cleanup requires materialization or a recorded failure"
        )
    if state.access_closed:
        return ledger.cleanup_receipt(manifest)

    root = Path(evaluator_root).expanduser().resolve(strict=False)
    manifest_scope = root / "private_manifests" / manifest.suite_id
    hidden_scope = root / "hidden_assets" / "private" / manifest.suite_id
    for scope in (manifest_scope, hidden_scope):
        resolved = scope.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            raise PrivateSuitePolicyError("private cleanup scope escaped evaluator storage")
        if scope.is_symlink():
            raise PrivateSuitePolicyError("private cleanup scope cannot be a symlink")

    expected_manifest_files = [
        (
            manifest_scope / "suite.v1.json",
            manifest.evaluation_suite_envelope.plaintext_sha256,
        )
    ]
    expected_manifest_files.extend(
        (
            manifest_scope / f"{record.private_task_id}.task.v1.json",
            record.task_manifest_envelope.plaintext_sha256,
        )
        for record in manifest.tasks
    )
    expected_hidden = Counter(
        record.hidden_asset_envelope.plaintext_sha256 for record in manifest.tasks
    )

    present: list[Path] = []
    expected_manifest_paths = {path for path, _identity in expected_manifest_files}
    if manifest_scope.exists():
        if not manifest_scope.is_dir():
            raise PrivateSuitePolicyError("private manifest cleanup scope is not a directory")
        for path in manifest_scope.iterdir():
            if path not in expected_manifest_paths:
                raise PrivateSuitePolicyError(
                    "private manifest cleanup scope contains unexpected content"
                )
    for path, expected_sha256 in expected_manifest_files:
        if path.is_symlink():
            raise PrivateSuitePolicyError("private cleanup refuses a symlinked plaintext file")
        if not path.exists():
            continue
        if _private_file_sha256(path) != expected_sha256:
            raise PrivateSuitePolicyError(
                "private manifest plaintext changed before cleanup; operator review required"
            )
        present.append(path)

    actual_hidden: Counter[str] = Counter()
    if hidden_scope.exists():
        if not hidden_scope.is_dir():
            raise PrivateSuitePolicyError("private hidden cleanup scope is not a directory")
        for path in hidden_scope.rglob("*"):
            if path.is_symlink():
                raise PrivateSuitePolicyError("private cleanup refuses symlinked hidden content")
            if path.is_dir():
                continue
            if not path.is_file():
                raise PrivateSuitePolicyError(
                    "private cleanup encountered unsupported hidden content"
                )
            identity = _private_file_sha256(path)
            actual_hidden[identity] += 1
            present.append(path)
    if any(count > expected_hidden[identity] for identity, count in actual_hidden.items()):
        raise PrivateSuitePolicyError(
            "private hidden cleanup scope contains unexpected or modified plaintext"
        )

    for path in present:
        try:
            path.unlink()
        except OSError as exc:
            raise PrivateSuitePolicyError(
                f"failed to remove evaluator private plaintext: {path}"
            ) from exc

    cleanup_directories = {manifest_scope, hidden_scope}
    for path in present:
        parent = path.parent
        while parent == hidden_scope or hidden_scope in parent.parents:
            if parent == root:
                break
            cleanup_directories.add(parent)
            parent = parent.parent
    for directory in sorted(cleanup_directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Shared parent scopes may legitimately remain; all verified files are already gone.
            pass

    disposed = tuple(
        sorted(
            [manifest.evaluation_suite_envelope.plaintext_sha256]
            + [record.task_manifest_envelope.plaintext_sha256 for record in manifest.tasks]
            + [record.hidden_asset_envelope.plaintext_sha256 for record in manifest.tasks]
        )
    )
    scope_sha256 = content_sha256(
        {
            "private_suite_manifest_sha256": manifest.manifest_sha256,
            "access_id": access_id,
            "disposed_plaintext_sha256s": disposed,
        }
    )
    return PrivatePlaintextCleanupReceipt(
        access_id=access_id,
        private_suite_manifest_sha256=manifest.manifest_sha256,
        disposed_plaintext_sha256s=disposed,
        expected_file_count=len(disposed),
        removed_file_count=len(present),
        cleanup_scope_sha256=scope_sha256,
        cleaned_at=cleaned_at or _now(),
    )


def close_private_suite_access(
    *,
    manifest: PrivateSuiteManifest,
    ledger: PrivateCustodyLedger,
    access_id: str,
    evaluator_root: Path,
    closed_at: datetime | None = None,
) -> PrivatePlaintextCleanupReceipt:
    """Dispose verified plaintext before irreversibly closing the one-time access."""

    state = ledger.state(manifest)
    if state.access_closed:
        return ledger.cleanup_receipt(manifest)
    timestamp = closed_at or _now()
    receipt = cleanup_materialized_private_suite(
        manifest=manifest,
        ledger=ledger,
        access_id=access_id,
        evaluator_root=evaluator_root,
        cleaned_at=timestamp,
    )
    ledger.close_access(
        manifest,
        access_id,
        receipt,
        closed_at=timestamp,
    )
    return receipt


def fail_private_suite_materialization(
    *,
    manifest: PrivateSuiteManifest,
    ledger: PrivateCustodyLedger,
    access_id: str,
    evaluator_root: Path,
    error_evidence_sha256: str,
    failed_at: datetime | None = None,
) -> PrivatePlaintextCleanupReceipt:
    """Recover an opened-but-uncommitted access after a crash and retire it permanently."""

    if len(error_evidence_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in error_evidence_sha256
    ):
        raise ValueError("materialization failure evidence must be a SHA-256 identity")
    state = ledger.state(manifest)
    if state.access_closed:
        return ledger.cleanup_receipt(manifest)
    if state.opened_access_id != access_id:
        raise PrivateSuitePolicyError("failure recovery does not match the opened access")
    if state.materialization_receipt_sha256 is not None:
        raise PrivateSuitePolicyError("successful materialization must use normal close")
    timestamp = failed_at or _now()
    ledger.record_materialization_failure(
        manifest,
        access_id,
        error_evidence_sha256,
        failed_at=timestamp,
    )
    receipt = cleanup_materialized_private_suite(
        manifest=manifest,
        ledger=ledger,
        access_id=access_id,
        evaluator_root=evaluator_root,
        cleaned_at=timestamp,
    )
    ledger.close_access(
        manifest,
        access_id,
        receipt,
        closed_at=timestamp,
    )
    return receipt


class PrivateSuiteAccessGuard:
    """Bind runner access to one opened/materialized authorization and retire on contamination."""

    def __init__(
        self,
        *,
        manifest: PrivateSuiteManifest,
        ledger: PrivateCustodyLedger,
        authorization_id: str,
        evaluator_principal_sha256: str,
        evaluator_root: Path,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if len(evaluator_principal_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in evaluator_principal_sha256
        ):
            raise ValueError("evaluator principal must be a SHA-256 identity")
        if evaluator_principal_sha256 == manifest.research_principal_sha256:
            raise ValueError("research principal cannot operate the private-suite access guard")
        self.manifest = manifest
        self.ledger = ledger
        self.authorization_id = authorization_id
        self.evaluator_principal_sha256 = evaluator_principal_sha256
        self.evaluator_root = Path(evaluator_root).expanduser().resolve(strict=False)
        self.clock = clock

    def assert_access(
        self,
        *,
        suite: EvaluationSuite,
        plan: EvaluationRunPlan,
        task: EvaluationTask,
    ) -> None:
        try:
            state = self.ledger.state(self.manifest)
            authorization = self.ledger.authorization(self.manifest, self.authorization_id)
        except PrivateSuitePolicyError as exc:
            raise EvaluationCustodyInfrastructureError(str(exc)) from exc
        if (
            state.opened_access_id != self.authorization_id
            or state.materialization_receipt_sha256 is None
            or state.materialization_failed
            or state.access_closed
            or state.suite_retired
        ):
            raise EvaluationAccessRevokedError(
                "private-suite access is not open, materialized, and active"
            )
        if task.manifest_sha256 in state.retired_task_manifest_sha256s:
            raise EvaluationAccessRevokedError("private task is retired or contaminated")
        try:
            record = self.manifest.task_by_manifest(task.manifest_sha256)
        except KeyError as exc:
            raise EvaluationAccessRevokedError("task is outside private-suite custody") from exc
        if record.scheduled_retire_at <= self.clock():
            raise EvaluationAccessRevokedError("private task reached its scheduled retirement")
        if (
            suite.manifest_sha256 != self.manifest.evaluation_suite_manifest_sha256
            or plan.suite_manifest_sha256 != suite.manifest_sha256
            or plan.evaluator_manifest_sha256 != self.manifest.evaluator_manifest_sha256
            or plan.manifest_sha256 not in authorization.allowed_run_plan_sha256s
        ):
            raise EvaluationAccessRevokedError(
                "suite, evaluator, or run plan is outside private-test authorization"
            )
        if not task.contamination_policy.retire_after_access:
            raise EvaluationAccessRevokedError("authorized private task lost its one-time policy")

    def record_contamination(
        self,
        *,
        suite: EvaluationSuite,
        plan: EvaluationRunPlan,
        task: EvaluationTask,
        attempt_id: str,
        source: Literal["submission", "scorer"],
        evidence_sha256: str,
        detail_sha256: str,
    ) -> None:
        self.assert_access(suite=suite, plan=plan, task=task)
        source_type = (
            ContaminationSource.SUBMISSION_DECLARATION
            if source == "submission"
            else ContaminationSource.SCORER_CANARY
        )
        severity = (
            ContaminationSeverity.CRITICAL
            if self.manifest.policy.contamination_retirement_scope == "suite"
            else ContaminationSeverity.MATERIAL
        )
        identity = content_sha256(
            {
                "suite": self.manifest.manifest_sha256,
                "task": task.manifest_sha256,
                "attempt_id": attempt_id,
                "source": source_type.value,
                "evidence_sha256": evidence_sha256,
                "detail_sha256": detail_sha256,
            }
        )
        report = PrivateContaminationReport(
            report_id=f"cont-{identity[:24]}",
            private_suite_manifest_sha256=self.manifest.manifest_sha256,
            evaluation_task_manifest_sha256=task.manifest_sha256,
            source=source_type,
            severity=severity,
            evidence_sha256=evidence_sha256,
            detail_sha256=detail_sha256,
            reporter_principal_sha256=self.evaluator_principal_sha256,
            detected_at=self.clock(),
        )
        try:
            self.ledger.report_contamination(self.manifest, report)
            close_private_suite_access(
                manifest=self.manifest,
                ledger=self.ledger,
                access_id=self.authorization_id,
                evaluator_root=self.evaluator_root,
                closed_at=report.detected_at,
            )
        except PrivateSuitePolicyError as exc:
            raise EvaluationCustodyInfrastructureError(str(exc)) from exc
