"""Frozen contracts for F11 fault-injection campaigns and recovery evidence."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CAMPAIGN_ID_PATTERN = r"^fic_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class FrozenFaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FaultBoundary(str, Enum):
    API_PROCESS = "api_process"
    WORKER_PROCESS = "worker_process"
    DATABASE_CONNECTION = "database_connection"
    EVALUATOR = "evaluator"
    PROVIDER = "provider"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    STALE_LEASE = "stale_lease"
    ARCHIVE_STORAGE = "archive_storage"
    RUNTIME_IDENTITY = "runtime_identity"
    OUTWARD_ACTION = "outward_action"


class FaultInjectionOutcome(str, Enum):
    PROCESS_EXIT = "process_exit"
    CONNECTION_LOST = "connection_lost"
    TRANSACTION_ABORTED = "transaction_aborted"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DUPLICATE_DELIVERED = "duplicate_delivered"
    LEASE_EXPIRED = "lease_expired"
    STORAGE_EXHAUSTED = "storage_exhausted"
    IDENTITY_MISMATCH = "identity_mismatch"
    AMBIGUOUS_REMOTE_RESULT = "ambiguous_remote_result"


class FaultRecoveryAction(str, Enum):
    RECONNECT_DATABASE = "reconnect_database"
    REPLAY_EXACT_COMMAND = "replay_exact_command"
    RECLAIM_EXPIRED_LEASE = "reclaim_expired_lease"
    REJECT_STALE_CALLBACK = "reject_stale_callback"
    RETRY_INFRASTRUCTURE_ATTEMPT = "retry_infrastructure_attempt"
    REBUILD_FROM_LEDGER = "rebuild_from_ledger"
    VERIFY_ARCHIVE = "verify_archive"
    REJECT_RUNTIME_MISMATCH = "reject_runtime_mismatch"
    REQUIRE_OUTWARD_RECONCILIATION = "require_outward_reconciliation"


class FaultMetric(str, Enum):
    SCIENTIFIC_STATE_LOSS_COUNT = "scientific_state_loss_count"
    DUPLICATE_SCIENTIFIC_STATE_COUNT = "duplicate_scientific_state_count"
    DUPLICATE_BUDGET_CHARGE_COUNT = "duplicate_budget_charge_count"
    DUPLICATE_OUTWARD_AUTHORIZATION_COUNT = "duplicate_outward_authorization_count"
    UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT = (
        "unresolved_ambiguity_without_block_count"
    )
    EVENT_STATE_MISMATCH_COUNT = "event_state_mismatch_count"
    COMMITTED_SCIENTIFIC_STATE_COUNT = "committed_scientific_state_count"
    COMMITTED_COMMAND_COUNT = "committed_command_count"
    KEYED_EVENT_COUNT = "keyed_event_count"
    BUDGET_CHARGE_COUNT = "budget_charge_count"
    OUTWARD_AUTHORIZATION_COUNT = "outward_authorization_count"
    OUTWARD_RECEIPT_COUNT = "outward_receipt_count"
    RECONCILIATION_REQUIRED_COUNT = "reconciliation_required_count"
    RECOVERED_TASK_COUNT = "recovered_task_count"
    SUCCEEDED_TASK_COUNT = "succeeded_task_count"
    TASK_ATTEMPT_COUNT = "task_attempt_count"
    REJECTED_STALE_CALLBACK_COUNT = "rejected_stale_callback_count"
    REPLAYED_RECEIPT_COUNT = "replayed_receipt_count"
    RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT = (
        "retryable_infrastructure_failure_count"
    )
    REJECTED_RUNTIME_MISMATCH_COUNT = "rejected_runtime_mismatch_count"
    COMMITTED_ARCHIVE_COUNT = "committed_archive_count"
    ORPHAN_ARCHIVE_COUNT = "orphan_archive_count"


CORE_ZERO_METRICS = frozenset(
    {
        FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT,
        FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT,
        FaultMetric.DUPLICATE_BUDGET_CHARGE_COUNT,
        FaultMetric.DUPLICATE_OUTWARD_AUTHORIZATION_COUNT,
        FaultMetric.UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT,
        FaultMetric.EVENT_STATE_MISMATCH_COUNT,
    }
)


class FaultComparator(str, Enum):
    EXACT = "exact"
    AT_MOST = "at_most"
    AT_LEAST = "at_least"


class FaultScenarioDisposition(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class FaultCampaignDisposition(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class FaultInvariantExpectation(FrozenFaultModel):
    metric: FaultMetric
    comparator: FaultComparator = FaultComparator.EXACT
    expected_value: int = Field(ge=0)


class FaultMetricObservation(FrozenFaultModel):
    metric: FaultMetric
    observed_value: int = Field(ge=0)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)


class FaultScenarioSpec(FrozenFaultModel):
    schema_version: Literal[1] = 1
    scenario_id: str = Field(pattern=_IDENTITY_PATTERN)
    boundary: FaultBoundary
    injection_point: str = Field(pattern=_IDENTITY_PATTERN)
    expected_outcome: FaultInjectionOutcome
    required_recovery_actions: tuple[FaultRecoveryAction, ...] = Field(
        min_length=1, max_length=32
    )
    expectations: tuple[FaultInvariantExpectation, ...] = Field(
        min_length=len(CORE_ZERO_METRICS), max_length=64
    )
    timeout_seconds: int = Field(default=60, ge=1, le=3_600)
    tags: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _expectations_are_complete(self) -> "FaultScenarioSpec":
        recovery_actions = tuple(
            sorted(set(self.required_recovery_actions), key=lambda item: item.value)
        )
        expectations = tuple(sorted(self.expectations, key=lambda item: item.metric.value))
        metrics = [item.metric for item in expectations]
        if metrics != sorted(set(metrics), key=lambda item: item.value):
            raise ValueError("fault scenario expectations must use unique metrics")
        by_metric = {item.metric: item for item in expectations}
        for metric in CORE_ZERO_METRICS:
            expectation = by_metric.get(metric)
            if (
                expectation is None
                or expectation.comparator is not FaultComparator.EXACT
                or expectation.expected_value != 0
            ):
                raise ValueError(
                    f"fault scenario must require exact zero for {metric.value}"
                )
        tags = tuple(sorted(set(self.tags)))
        if any(not tag or len(tag) > 128 for tag in tags):
            raise ValueError("fault scenario tags must contain 1-128 characters")
        object.__setattr__(self, "required_recovery_actions", recovery_actions)
        object.__setattr__(self, "expectations", expectations)
        object.__setattr__(self, "tags", tags)
        return self

    @property
    def spec_sha256(self) -> str:
        return content_sha256(self)


class FaultScenarioObservation(FrozenFaultModel):
    scenario_id: str = Field(pattern=_IDENTITY_PATTERN)
    observed_outcome: FaultInjectionOutcome | None
    injection_confirmed: bool
    recovery_actions: tuple[FaultRecoveryAction, ...] = Field(min_length=1, max_length=32)
    metrics: tuple[FaultMetricObservation, ...] = Field(min_length=1, max_length=64)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    diagnostic_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _observation_is_canonical(self) -> "FaultScenarioObservation":
        if self.injection_confirmed and self.observed_outcome is None:
            raise ValueError("confirmed fault injection requires an observed outcome")
        if self.completed_at < self.started_at:
            raise ValueError("fault scenario completes before it starts")
        actions = tuple(sorted(set(self.recovery_actions), key=lambda item: item.value))
        metrics = tuple(sorted(self.metrics, key=lambda item: item.metric.value))
        metric_ids = [item.metric for item in metrics]
        if metric_ids != sorted(set(metric_ids), key=lambda item: item.value):
            raise ValueError("fault observation metrics must be unique")
        evidence = tuple(sorted(set(self.evidence_sha256s)))
        object.__setattr__(self, "recovery_actions", actions)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "evidence_sha256s", evidence)
        return self

    @property
    def observation_sha256(self) -> str:
        return content_sha256(self)


class FaultInvariantResult(FrozenFaultModel):
    metric: FaultMetric
    comparator: FaultComparator
    expected_value: int = Field(ge=0)
    observed_value: int = Field(ge=0)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    passed: bool

    @model_validator(mode="after")
    def _result_is_derived(self) -> "FaultInvariantResult":
        expected = {
            FaultComparator.EXACT: self.observed_value == self.expected_value,
            FaultComparator.AT_MOST: self.observed_value <= self.expected_value,
            FaultComparator.AT_LEAST: self.observed_value >= self.expected_value,
        }[self.comparator]
        if self.passed != expected:
            raise ValueError("fault invariant verdict is not derived from its values")
        return self


class FaultScenarioResult(FrozenFaultModel):
    scenario_id: str = Field(pattern=_IDENTITY_PATTERN)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation: FaultScenarioObservation
    invariants: tuple[FaultInvariantResult, ...]
    disposition: FaultScenarioDisposition
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _scenario_verdict_is_canonical(self) -> "FaultScenarioResult":
        if self.observation.scenario_id != self.scenario_id:
            raise ValueError("fault result observation belongs to another scenario")
        metrics = [item.metric for item in self.invariants]
        if metrics != sorted(set(metrics), key=lambda item: item.value):
            raise ValueError("fault invariant results must be unique and canonical")
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("fault scenario blockers must be canonical")
        expected_disposition = (
            FaultScenarioDisposition.PASSED
            if not blockers
            else FaultScenarioDisposition.BLOCKED
            if not self.observation.injection_confirmed
            else FaultScenarioDisposition.FAILED
        )
        if self.disposition is not expected_disposition:
            raise ValueError("fault scenario disposition is inconsistent")
        return self


class FaultCampaignManifest(FrozenFaultModel):
    schema_version: Literal[1] = 1
    campaign_id: str | None = Field(default=None, pattern=_CAMPAIGN_ID_PATTERN)
    campaign_key: str = Field(pattern=_IDENTITY_PATTERN)
    quest_id: str | None = Field(default=None, pattern=_QUEST_ID_PATTERN)
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    harness_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    scenarios: tuple[FaultScenarioSpec, ...] = Field(min_length=10, max_length=128)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _matrix_is_complete(self) -> "FaultCampaignManifest":
        scenarios = tuple(sorted(self.scenarios, key=lambda item: item.scenario_id))
        ids = [item.scenario_id for item in scenarios]
        if ids != sorted(set(ids)):
            raise ValueError("fault campaign scenario identities must be unique")
        boundaries = {item.boundary for item in scenarios}
        if boundaries != set(FaultBoundary):
            missing = sorted(item.value for item in set(FaultBoundary) - boundaries)
            raise ValueError(f"fault campaign boundary matrix is incomplete: {missing}")
        object.__setattr__(self, "scenarios", scenarios)
        expected_id = f"fic_{self.manifest_sha256[:32]}"
        if self.campaign_id is not None and self.campaign_id != expected_id:
            raise ValueError("fault campaign ID does not match its manifest")
        object.__setattr__(self, "campaign_id", expected_id)
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"campaign_id"}))


class FaultCampaignReport(FrozenFaultModel):
    manifest: FaultCampaignManifest
    results: tuple[FaultScenarioResult, ...]
    disposition: FaultCampaignDisposition
    scenario_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    scientific_state_loss_count: int = Field(ge=0)
    duplicate_scientific_state_count: int = Field(ge=0)
    duplicate_budget_charge_count: int = Field(ge=0)
    duplicate_outward_authorization_count: int = Field(ge=0)
    unresolved_ambiguity_without_block_count: int = Field(ge=0)
    event_state_mismatch_count: int = Field(ge=0)
    completed_at: AwareDatetime
    report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _report_is_derived(self) -> "FaultCampaignReport":
        results = tuple(sorted(self.results, key=lambda item: item.scenario_id))
        result_ids = [item.scenario_id for item in results]
        scenario_ids = [item.scenario_id for item in self.manifest.scenarios]
        if result_ids != scenario_ids:
            raise ValueError("fault report must cover every manifested scenario exactly once")
        specs_by_id = {item.scenario_id: item for item in self.manifest.scenarios}
        if any(
            item.spec_sha256 != specs_by_id[item.scenario_id].spec_sha256
            for item in results
        ):
            raise ValueError("fault report scenario specification hash changed")
        if self.completed_at < self.manifest.created_at or any(
            self.completed_at < item.observation.completed_at for item in results
        ):
            raise ValueError("fault report predates its campaign evidence")
        if any(
            item.observation.started_at < self.manifest.created_at for item in results
        ):
            raise ValueError("fault scenario evidence predates its campaign manifest")
        counts = {
            FaultScenarioDisposition.PASSED: sum(
                item.disposition is FaultScenarioDisposition.PASSED for item in results
            ),
            FaultScenarioDisposition.FAILED: sum(
                item.disposition is FaultScenarioDisposition.FAILED for item in results
            ),
            FaultScenarioDisposition.BLOCKED: sum(
                item.disposition is FaultScenarioDisposition.BLOCKED for item in results
            ),
        }
        if (
            self.scenario_count != len(results)
            or self.passed_count != counts[FaultScenarioDisposition.PASSED]
            or self.failed_count != counts[FaultScenarioDisposition.FAILED]
            or self.blocked_count != counts[FaultScenarioDisposition.BLOCKED]
        ):
            raise ValueError("fault report scenario counts are inconsistent")
        expected_disposition = (
            FaultCampaignDisposition.FAILED
            if self.failed_count
            else FaultCampaignDisposition.BLOCKED
            if self.blocked_count
            else FaultCampaignDisposition.PASSED
        )
        if self.disposition is not expected_disposition:
            raise ValueError("fault campaign disposition is inconsistent")
        aggregate_fields = {
            FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: "scientific_state_loss_count",
            FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: (
                "duplicate_scientific_state_count"
            ),
            FaultMetric.DUPLICATE_BUDGET_CHARGE_COUNT: "duplicate_budget_charge_count",
            FaultMetric.DUPLICATE_OUTWARD_AUTHORIZATION_COUNT: (
                "duplicate_outward_authorization_count"
            ),
            FaultMetric.UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT: (
                "unresolved_ambiguity_without_block_count"
            ),
            FaultMetric.EVENT_STATE_MISMATCH_COUNT: "event_state_mismatch_count",
        }
        for metric, field_name in aggregate_fields.items():
            observed_total = sum(
                item.observed_value
                for result in results
                for item in result.observation.metrics
                if item.metric is metric
            )
            if getattr(self, field_name) != observed_total:
                raise ValueError(f"fault report aggregate is inconsistent: {field_name}")
        object.__setattr__(self, "results", results)
        expected_sha256 = content_sha256(
            self.model_dump(mode="json", exclude={"report_sha256"})
        )
        if self.report_sha256 is not None and self.report_sha256 != expected_sha256:
            raise ValueError("fault campaign report hash does not match its contents")
        object.__setattr__(self, "report_sha256", expected_sha256)
        return self


class FaultCampaignCommitContext(FrozenFaultModel):
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    principal: str = Field(min_length=1, max_length=128)
    source_event_key: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)


class FaultCampaignCommitReceipt(FrozenFaultModel):
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    command_id: str
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    created: bool


class FaultCampaignSnapshot(FrozenFaultModel):
    report: FaultCampaignReport
    command_id: str
    created_by: str
    created_at: AwareDatetime


class FaultCampaignAudit(FrozenFaultModel):
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    campaign_count: int = Field(ge=0)
    passed_campaign_count: int = Field(ge=0)
    latest_campaign_id: str | None = Field(default=None, pattern=_CAMPAIGN_ID_PATTERN)
    latest_campaign_passed: bool
    eligible_for_endurance_gate_review: bool
    autonomous_allocation_enabled: Literal[False] = False
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _audit_is_canonical(self) -> "FaultCampaignAudit":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("fault campaign audit blockers must be canonical")
        if self.passed_campaign_count > self.campaign_count:
            raise ValueError("fault campaign audit pass count exceeds campaign count")
        if (self.latest_campaign_id is None) != (self.campaign_count == 0):
            raise ValueError("fault campaign audit latest identity differs from campaign count")
        if self.eligible_for_endurance_gate_review != (not blockers):
            raise ValueError("fault campaign audit eligibility differs from blockers")
        if self.latest_campaign_passed != (
            self.latest_campaign_id is not None and not blockers
        ):
            raise ValueError("fault campaign latest-pass status is inconsistent")
        return self


__all__ = [
    "CORE_ZERO_METRICS",
    "FaultBoundary",
    "FaultCampaignAudit",
    "FaultCampaignCommitContext",
    "FaultCampaignCommitReceipt",
    "FaultCampaignDisposition",
    "FaultCampaignManifest",
    "FaultCampaignReport",
    "FaultCampaignSnapshot",
    "FaultComparator",
    "FaultInjectionOutcome",
    "FaultInvariantExpectation",
    "FaultInvariantResult",
    "FaultMetric",
    "FaultMetricObservation",
    "FaultRecoveryAction",
    "FaultScenarioDisposition",
    "FaultScenarioObservation",
    "FaultScenarioResult",
    "FaultScenarioSpec",
]
