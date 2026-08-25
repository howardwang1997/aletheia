"""Production ten-boundary fault harness for Quest-scoped resilience evidence.

The lower-level :mod:`aletheia.jobs.fault_injection` module grades caller-supplied
observations.  This module owns the supported executors that create those observations from
the repository's real PostgreSQL, process, queue, archive, identity, and outward-action
boundaries.  A frozen environment manifest binds the executable code and runtime before any
fault is injected.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from aletheia.config import get_settings
from aletheia.db import REPO_ROOT, engine, schema_status, session_scope
from aletheia.jobs.actions import (
    ExternalActionStatus,
    OneTimeExternalActionSpec,
    OneTimeExternalActionStore,
)
from aletheia.jobs.contracts import (
    EnqueueReceipt,
    RetryPolicy,
    TaskExecutionResult,
    TaskLease,
    TaskSpec,
    TaskStatus,
)
from aletheia.jobs.fault_injection import (
    run_fault_campaign,
    validate_fault_campaign_report,
)
from aletheia.jobs.fault_schemas import (
    CORE_ZERO_METRICS,
    FaultBoundary,
    FaultCampaignManifest,
    FaultCampaignReport,
    FaultComparator,
    FaultInjectionOutcome,
    FaultInvariantExpectation,
    FaultMetric,
    FaultMetricObservation,
    FaultRecoveryAction,
    FaultScenarioObservation,
    FaultScenarioSpec,
)
from aletheia.jobs.outbox import (
    ScientificCommandSpec,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import (
    DurableTaskRecord,
    ExternalActionReceiptRecord,
    OneTimeExternalActionRecord,
    ScientificCommandRecord,
)
from aletheia.jobs.queue import DurableTaskQueue, InvalidTaskTransition, LeaseMismatch
from aletheia.jobs.worker import DurableWorker, InfrastructureTaskFailure
from aletheia.memory.ledger import Decision, Event
from aletheia.paths import artifacts_dir
from aletheia.programs.memory import ResearchMemoryStore
from aletheia.programs.memory_archive import ScientificMemoryArchive
from aletheia.programs.memory_schemas import (
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    ResearchMemoryFactSpec,
)
from aletheia.programs.persistence import (
    ResearchMemoryCompactionRecord,
    ResearchMemoryFactRecord,
)
from aletheia.programs.schemas import GraphCommandContext
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PACKAGE_NAMES = ("alembic", "pgvector", "psycopg", "pydantic", "sqlalchemy")
_CODE_COMPONENTS = (
    "aletheia/db.py",
    "aletheia/events/bus.py",
    "aletheia/events/store.py",
    "aletheia/jobs/actions.py",
    "aletheia/jobs/contracts.py",
    "aletheia/jobs/fault_harness.py",
    "aletheia/jobs/fault_injection.py",
    "aletheia/jobs/fault_schemas.py",
    "aletheia/jobs/outbox.py",
    "aletheia/jobs/persistence.py",
    "aletheia/jobs/queue.py",
    "aletheia/jobs/worker.py",
    "aletheia/knowledge/response_archive.py",
    "aletheia/memory/ledger.py",
    "aletheia/memory/service.py",
    "aletheia/paths.py",
    "aletheia/programs/memory.py",
    "aletheia/programs/memory_archive.py",
    "aletheia/programs/memory_schemas.py",
    "aletheia/programs/persistence.py",
    "aletheia/programs/schemas.py",
    "aletheia/reproducibility/manifest.py",
)


class FaultHarnessError(RuntimeError):
    """Base error for the supported real-boundary harness."""


class FaultHarnessEnvironmentMismatch(FaultHarnessError):
    """The live code/runtime no longer matches the frozen preparation."""


class FaultHarnessExecutionError(FaultHarnessError):
    """A boundary did not exhibit the required injected failure or recovery."""


class _FrozenHarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FaultHarnessEnvironmentManifest(_FrozenHarnessModel):
    """Non-secret runtime identity captured before a campaign starts."""

    schema_version: Literal[1] = 1
    python_implementation: str = Field(min_length=1, max_length=64)
    python_version: str = Field(min_length=1, max_length=64)
    python_executable: str = Field(min_length=1, max_length=1_024)
    platform_system: str = Field(min_length=1, max_length=128)
    platform_release: str = Field(min_length=1, max_length=256)
    platform_machine: str = Field(min_length=1, max_length=128)
    database_dialect: str = Field(min_length=1, max_length=64)
    database_driver: str = Field(min_length=1, max_length=64)
    database_server_version: str = Field(min_length=1, max_length=128)
    database_target_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=128)
    package_versions: dict[str, str]
    component_sha256s: dict[str, str]
    harness_code_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _canonical_and_bound(self) -> "FaultHarnessEnvironmentManifest":
        packages = dict(sorted(self.package_versions.items()))
        components = dict(sorted(self.component_sha256s.items()))
        if not packages or any(not key or not value for key, value in packages.items()):
            raise ValueError("fault harness package versions must be non-empty")
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("fault harness code-component matrix is incomplete")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in components.values()
        ):
            raise ValueError("fault harness component hashes must be lowercase SHA-256")
        expected = content_sha256(
            {
                "schema": "aletheia.fault_harness_code.v1",
                "components": components,
            }
        )
        if self.harness_code_sha256 != expected:
            raise ValueError("fault harness aggregate code hash does not match its components")
        object.__setattr__(self, "package_versions", packages)
        object.__setattr__(self, "component_sha256s", components)
        return self

    @property
    def environment_manifest_sha256(self) -> str:
        return content_sha256(self)


class FaultHarnessEvidenceBundle(_FrozenHarnessModel):
    """Self-verifying report plus the diagnostic facts behind every evidence hash."""

    schema_version: Literal[1] = 1
    environment: FaultHarnessEnvironmentManifest
    report: FaultCampaignReport
    diagnostics: dict[str, dict[str, Any]]
    bundle_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _evidence_is_closed(self) -> "FaultHarnessEvidenceBundle":
        validate_fault_campaign_report(self.report)
        diagnostics = json.loads(canonical_json_bytes(self.diagnostics))
        if not isinstance(diagnostics, dict):  # pragma: no cover - public type already requires it
            raise ValueError("fault harness diagnostics must be an object")
        manifest = self.report.manifest
        if manifest.environment_manifest_sha256 != self.environment.environment_manifest_sha256:
            raise ValueError("fault report is not bound to the supplied environment manifest")
        if manifest.harness_code_sha256 != self.environment.harness_code_sha256:
            raise ValueError("fault report is not bound to the supplied harness code")
        scenario_ids = {item.scenario_id for item in manifest.scenarios}
        if set(diagnostics) != scenario_ids:
            raise ValueError("fault harness diagnostic matrix differs from the report")
        campaign_id = manifest.campaign_id
        for result in self.report.results:
            detail = diagnostics[result.scenario_id]
            diagnostic_sha256 = content_sha256(
                {
                    "schema": "aletheia.fault_harness_diagnostic.v1",
                    "campaign_id": campaign_id,
                    "scenario_id": result.scenario_id,
                    "detail": detail,
                }
            )
            if result.observation.diagnostic_sha256 != diagnostic_sha256:
                raise ValueError(
                    f"fault harness diagnostic changed for {result.scenario_id}"
                )
            expected_evidence = {diagnostic_sha256}
            for metric in result.observation.metrics:
                metric_sha256 = content_sha256(
                    {
                        "schema": "aletheia.fault_harness_metric_evidence.v1",
                        "campaign_id": campaign_id,
                        "scenario_id": result.scenario_id,
                        "metric": metric.metric.value,
                        "observed_value": metric.observed_value,
                        "diagnostic_sha256": diagnostic_sha256,
                    }
                )
                if metric.evidence_sha256 != metric_sha256:
                    raise ValueError(
                        f"fault harness metric evidence changed for {result.scenario_id}"
                    )
                expected_evidence.add(metric_sha256)
            if set(result.observation.evidence_sha256s) != expected_evidence:
                raise ValueError(
                    f"fault harness evidence closure changed for {result.scenario_id}"
                )
        object.__setattr__(self, "diagnostics", diagnostics)
        expected_bundle_sha256 = content_sha256(
            self.model_dump(mode="json", exclude={"bundle_sha256"})
        )
        if self.bundle_sha256 is not None and self.bundle_sha256 != expected_bundle_sha256:
            raise ValueError("fault harness bundle hash does not match its content")
        object.__setattr__(self, "bundle_sha256", expected_bundle_sha256)
        return self


_SCENARIO_CONFIG: dict[
    FaultBoundary,
    tuple[
        FaultInjectionOutcome,
        tuple[FaultRecoveryAction, ...],
        dict[FaultMetric, int],
    ],
] = {
    FaultBoundary.API_PROCESS: (
        FaultInjectionOutcome.PROCESS_EXIT,
        (FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.WORKER_PROCESS: (
        FaultInjectionOutcome.PROCESS_EXIT,
        (
            FaultRecoveryAction.RECLAIM_EXPIRED_LEASE,
            FaultRecoveryAction.REJECT_STALE_CALLBACK,
        ),
        {
            FaultMetric.RECOVERED_TASK_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
            FaultMetric.REJECTED_STALE_CALLBACK_COUNT: 1,
        },
    ),
    FaultBoundary.DATABASE_CONNECTION: (
        FaultInjectionOutcome.CONNECTION_LOST,
        (
            FaultRecoveryAction.RECONNECT_DATABASE,
            FaultRecoveryAction.REPLAY_EXACT_COMMAND,
        ),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_COMMAND_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.EVALUATOR: (
        FaultInjectionOutcome.TIMEOUT,
        (FaultRecoveryAction.RETRY_INFRASTRUCTURE_ATTEMPT,),
        {
            FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
        },
    ),
    FaultBoundary.PROVIDER: (
        FaultInjectionOutcome.UNAVAILABLE,
        (FaultRecoveryAction.RETRY_INFRASTRUCTURE_ATTEMPT,),
        {
            FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
        },
    ),
    FaultBoundary.DUPLICATE_DELIVERY: (
        FaultInjectionOutcome.DUPLICATE_DELIVERED,
        (FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_COMMAND_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.STALE_LEASE: (
        FaultInjectionOutcome.LEASE_EXPIRED,
        (
            FaultRecoveryAction.RECLAIM_EXPIRED_LEASE,
            FaultRecoveryAction.REJECT_STALE_CALLBACK,
        ),
        {
            FaultMetric.RECOVERED_TASK_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
            FaultMetric.REJECTED_STALE_CALLBACK_COUNT: 1,
        },
    ),
    FaultBoundary.ARCHIVE_STORAGE: (
        FaultInjectionOutcome.STORAGE_EXHAUSTED,
        (FaultRecoveryAction.VERIFY_ARCHIVE,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_ARCHIVE_COUNT: 0,
            FaultMetric.ORPHAN_ARCHIVE_COUNT: 0,
        },
    ),
    FaultBoundary.RUNTIME_IDENTITY: (
        FaultInjectionOutcome.IDENTITY_MISMATCH,
        (FaultRecoveryAction.REJECT_RUNTIME_MISMATCH,),
        {
            FaultMetric.REJECTED_RUNTIME_MISMATCH_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 1,
        },
    ),
    FaultBoundary.OUTWARD_ACTION: (
        FaultInjectionOutcome.AMBIGUOUS_REMOTE_RESULT,
        (FaultRecoveryAction.REQUIRE_OUTWARD_RECONCILIATION,),
        {
            FaultMetric.OUTWARD_AUTHORIZATION_COUNT: 1,
            FaultMetric.OUTWARD_RECEIPT_COUNT: 0,
            FaultMetric.RECONCILIATION_REQUIRED_COUNT: 1,
        },
    ),
}


def _component_sha256s() -> dict[str, str]:
    values: dict[str, str] = {}
    for relative_path in _CODE_COMPONENTS:
        path = REPO_ROOT / relative_path
        if not path.is_file():
            raise FaultHarnessEnvironmentMismatch(
                f"fault harness code component is missing: {relative_path}"
            )
        values[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def capture_fault_harness_environment() -> FaultHarnessEnvironmentManifest:
    """Capture the exact non-secret runtime identity used by the real harness."""

    components = _component_sha256s()
    harness_code_sha256 = content_sha256(
        {"schema": "aletheia.fault_harness_code.v1", "components": components}
    )
    packages: dict[str, str] = {}
    for name in _PACKAGE_NAMES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover - bad deployment
            raise FaultHarnessEnvironmentMismatch(
                f"required fault harness package is not installed: {name}"
            ) from exc

    configured_url = make_url(get_settings().database_url)
    database_target_sha256 = content_sha256(
        {
            "drivername": configured_url.drivername,
            "username": configured_url.username,
            "host": configured_url.host,
            "port": configured_url.port,
            "database": configured_url.database,
            "query": dict(sorted(configured_url.query.items())),
        }
    )
    with engine().connect() as connection:
        status = schema_status(connection)
        if not status.is_current or status.current_revision is None:
            raise FaultHarnessEnvironmentMismatch(
                "fault harness requires the exact current Alembic schema"
            )
        server_version = connection.dialect.server_version_info
        if not server_version:
            server_version_text = "unknown"
        else:
            server_version_text = ".".join(str(item) for item in server_version)
        database_dialect = connection.dialect.name
        database_driver = connection.dialect.driver
    return FaultHarnessEnvironmentManifest(
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        python_executable=str(Path(sys.executable).resolve()),
        platform_system=platform.system(),
        platform_release=platform.release(),
        platform_machine=platform.machine(),
        database_dialect=database_dialect,
        database_driver=database_driver,
        database_server_version=server_version_text,
        database_target_sha256=database_target_sha256,
        schema_revision=status.current_revision,
        package_versions=packages,
        component_sha256s=components,
        harness_code_sha256=harness_code_sha256,
    )


def _expectations(extra: dict[FaultMetric, int]) -> tuple[FaultInvariantExpectation, ...]:
    values = {metric: 0 for metric in CORE_ZERO_METRICS}
    values.update(extra)
    return tuple(
        FaultInvariantExpectation(
            metric=metric,
            comparator=FaultComparator.EXACT,
            expected_value=value,
        )
        for metric, value in sorted(values.items(), key=lambda item: item[0].value)
    )


def prepare_durable_fault_campaign(
    *,
    quest_id: str,
    environment: FaultHarnessEnvironmentManifest,
    campaign_key: str | None = None,
    seed: int = 17,
    created_at: datetime | None = None,
) -> FaultCampaignManifest:
    """Freeze the supported ten-boundary campaign before the first injected mutation."""

    environment = FaultHarnessEnvironmentManifest.model_validate(
        environment.model_dump(mode="python")
    )
    observed_at = created_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("fault campaign creation timestamp must be timezone-aware")
    logical_key = campaign_key or (
        f"f11s6.real.{observed_at.strftime('%Y%m%dT%H%M%S')}.{uuid.uuid4().hex[:16]}"
    )
    scenarios = tuple(
        FaultScenarioSpec(
            scenario_id=f"f11s6.{boundary.value}",
            boundary=boundary,
            injection_point=f"durable.{boundary.value}",
            expected_outcome=outcome,
            required_recovery_actions=actions,
            expectations=_expectations(extra),
            timeout_seconds=120,
            tags=("f11s6", "production-harness", "quest-scoped"),
        )
        for boundary, (outcome, actions, extra) in _SCENARIO_CONFIG.items()
    )
    return FaultCampaignManifest(
        campaign_key=logical_key,
        quest_id=quest_id,
        seed=seed,
        harness_code_sha256=environment.harness_code_sha256,
        environment_manifest_sha256=environment.environment_manifest_sha256,
        scenarios=scenarios,
        created_at=observed_at,
    )


def validate_fault_harness_environment(
    manifest: FaultCampaignManifest,
    frozen: FaultHarnessEnvironmentManifest,
    *,
    current: FaultHarnessEnvironmentManifest | None = None,
) -> FaultHarnessEnvironmentManifest:
    """Fail closed when the manifest, frozen environment, and live runtime diverge."""

    manifest = FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
    frozen = FaultHarnessEnvironmentManifest.model_validate(frozen.model_dump(mode="python"))
    observed = current or capture_fault_harness_environment()
    if manifest.quest_id is None:
        raise FaultHarnessEnvironmentMismatch(
            "the production fault harness requires a Quest-scoped manifest"
        )
    if manifest.harness_code_sha256 != frozen.harness_code_sha256:
        raise FaultHarnessEnvironmentMismatch(
            "fault campaign harness-code hash differs from the frozen environment"
        )
    if manifest.environment_manifest_sha256 != frozen.environment_manifest_sha256:
        raise FaultHarnessEnvironmentMismatch(
            "fault campaign environment hash differs from the frozen environment"
        )
    if observed != frozen:
        raise FaultHarnessEnvironmentMismatch(
            "live fault harness code/runtime differs from the frozen environment"
        )
    return observed


class _ExhaustedScientificMemoryArchive(ScientificMemoryArchive):
    def store(self, *_args: Any, **_kwargs: Any):
        raise OSError(errno.ENOSPC, "injected archive quota exhausted")


class DurableFaultHarness:
    """Execute the supported ten real durability boundaries for one frozen campaign."""

    def __init__(
        self,
        *,
        environment: FaultHarnessEnvironmentManifest,
        principal: str,
        archive_root: Path | None = None,
    ) -> None:
        if not principal or len(principal) > 96:
            raise ValueError("fault harness principal must contain 1-96 characters")
        self.environment = environment
        self.principal = principal
        self.archive_root = Path(archive_root) if archive_root is not None else None
        self._attempt_id = uuid.uuid4().hex
        self._diagnostics: dict[str, dict[str, Any]] = {}
        self._campaign_id: str | None = None
        self._quest_id: str | None = None

    def _role(self, role: str) -> str:
        value = f"{self.principal}:{role}"
        if len(value) > 128:
            raise FaultHarnessExecutionError("fault harness role principal exceeds 128 characters")
        return value

    def _identity(self, label: str) -> str:
        return f"{label}:{self._attempt_id}"

    def _task_spec(self, label: str, *, lease_seconds: int = 2) -> TaskSpec:
        short = self._attempt_id[:20]
        return TaskSpec(
            task_id=f"task-{label}-{short}",
            task_type=f"fault.{label}.{short}",
            inputs={"label": label, "attempt_id": self._attempt_id},
            owner=self._role("task"),
            idempotency_key=f"fault:{label}:{short}",
            retry_policy=RetryPolicy(
                max_attempts=3,
                lease_seconds=lease_seconds,
                heartbeat_interval_seconds=1,
                initial_backoff_seconds=0,
                max_backoff_seconds=0,
            ),
        )

    @staticmethod
    def _result(label: str) -> TaskExecutionResult:
        return TaskExecutionResult(
            result_artifact_id=f"artifact:{label}",
            result={"label": label, "valid": True},
        )

    @staticmethod
    def _measured(
        spec: FaultScenarioSpec,
        *,
        specific: dict[FaultMetric, int],
        core: dict[FaultMetric, int] | None = None,
    ) -> dict[FaultMetric, int]:
        values = {metric: 0 for metric in CORE_ZERO_METRICS}
        if core is not None:
            values.update(core)
        values.update(specific)
        expected = {item.metric for item in spec.expectations}
        if set(values) != expected:
            raise FaultHarnessExecutionError(
                f"measured metric matrix differs for {spec.scenario_id}"
            )
        return values

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise FaultHarnessExecutionError(message)

    def _observation(
        self,
        spec: FaultScenarioSpec,
        *,
        measured: dict[FaultMetric, int],
        detail: dict[str, Any],
        started_at: datetime,
        completed_at: datetime,
    ) -> FaultScenarioObservation:
        if self._campaign_id is None:  # pragma: no cover - run sets this before execution
            raise FaultHarnessExecutionError("fault harness has no active campaign identity")
        canonical_detail = json.loads(canonical_json_bytes(detail))
        diagnostic = content_sha256(
            {
                "schema": "aletheia.fault_harness_diagnostic.v1",
                "campaign_id": self._campaign_id,
                "scenario_id": spec.scenario_id,
                "detail": canonical_detail,
            }
        )
        metrics = tuple(
            FaultMetricObservation(
                metric=metric,
                observed_value=value,
                evidence_sha256=content_sha256(
                    {
                        "schema": "aletheia.fault_harness_metric_evidence.v1",
                        "campaign_id": self._campaign_id,
                        "scenario_id": spec.scenario_id,
                        "metric": metric.value,
                        "observed_value": value,
                        "diagnostic_sha256": diagnostic,
                    }
                ),
            )
            for metric, value in sorted(measured.items(), key=lambda item: item[0].value)
        )
        self._diagnostics[spec.scenario_id] = canonical_detail
        return FaultScenarioObservation(
            scenario_id=spec.scenario_id,
            observed_outcome=spec.expected_outcome,
            injection_confirmed=True,
            recovery_actions=spec.required_recovery_actions,
            metrics=metrics,
            evidence_sha256s=(diagnostic, *(item.evidence_sha256 for item in metrics)),
            diagnostic_sha256=diagnostic,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _api_process(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = self._task_spec("api-process")
        script = "\n".join(
            (
                "import os",
                "from aletheia.jobs import DurableTaskQueue, TaskSpec",
                f"spec=TaskSpec.model_validate_json({task.model_dump_json()!r})",
                (
                    "receipt=DurableTaskQueue("
                    f"principal={self._role('api-child')!r}).enqueue(spec)"
                ),
                "print(receipt.model_dump_json(), flush=True)",
                "os._exit(51)",
            )
        )
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout_seconds,
        )
        self._require(child.returncode == 51, "API child did not exit at the injected boundary")
        try:
            child_receipt = EnqueueReceipt.model_validate_json(
                child.stdout.strip().splitlines()[-1]
            )
        except (IndexError, ValueError) as exc:
            raise FaultHarnessExecutionError("API child did not emit a valid enqueue receipt") from exc
        replay = DurableTaskQueue(principal=self._role("api-recovery")).enqueue(task)
        self._require(
            child_receipt.created and not replay.created,
            "API recovery did not replay the exact durable task",
        )
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(DurableTaskRecord)
                .where(DurableTaskRecord.task_id == task.task_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == f"durable-task:{task.task_id}:1")
            )
        self._require(
            state_count is not None and event_count is not None,
            "API recovery counts were unavailable",
        )
        assert state_count is not None and event_count is not None
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(event_count != state_count),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(not replay.created),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "child_returncode": child.returncode,
                "child_stderr_sha256": hashlib.sha256(child.stderr.encode()).hexdigest(),
                "task_id": task.task_id,
                "state_count": state_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _worker_process(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = self._task_spec("worker-process", lease_seconds=2)
        queue = DurableTaskQueue(principal=self._role("worker-parent"))
        queue.enqueue(task)
        script = "\n".join(
            (
                "import os",
                "from aletheia.jobs import DurableTaskQueue",
                f"queue=DurableTaskQueue(principal={self._role('worker-child')!r})",
                (
                    "lease=queue.claim(worker_id='killed-fault-worker',"
                    "worker_manifest_sha256='a'*64,"
                    f"task_types=[{task.task_type!r}])"
                ),
                "print(lease.model_dump_json(), flush=True)",
                "os._exit(52)",
            )
        )
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout_seconds,
        )
        self._require(child.returncode == 52, "worker child did not exit while holding its lease")
        try:
            killed_lease = TaskLease.model_validate_json(child.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise FaultHarnessExecutionError("worker child did not emit a valid lease") from exc
        recovered_at = killed_lease.lease_expires_at + timedelta(microseconds=1)
        recovered = queue.recover_expired(now=recovered_at)
        replacement = queue.claim(
            worker_id="replacement-fault-worker",
            worker_manifest_sha256="b" * 64,
            task_types=(task.task_type,),
            now=recovered_at,
        )
        self._require(replacement is not None, "replacement worker did not reclaim the task")
        assert replacement is not None
        try:
            queue.complete(killed_lease, self._result("stale-worker"), now=recovered_at)
        except (InvalidTaskTransition, LeaseMismatch):
            rejected_stale = 1
        else:
            raise FaultHarnessExecutionError("killed worker's stale callback was accepted")
        completed = queue.complete(
            replacement,
            self._result("replacement-worker"),
            now=recovered_at + timedelta(microseconds=1),
        )
        attempts = queue.attempts(task.task_id)
        task_count = int(queue.get(task.task_id).status is TaskStatus.SUCCEEDED)
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - task_count,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RECOVERED_TASK_COUNT: int(
                    task.task_id in recovered.recovered_task_ids
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: int(
                    completed.task.status is TaskStatus.SUCCEEDED
                ),
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
                FaultMetric.REJECTED_STALE_CALLBACK_COUNT: rejected_stale,
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "child_returncode": child.returncode,
                "child_stderr_sha256": hashlib.sha256(child.stderr.encode()).hexdigest(),
                "task_id": task.task_id,
                "attempt_ids": [item.attempt_id for item in attempts],
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _database_connection(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        from aletheia.memory.service import create_run

        started = datetime.now(timezone.utc)
        run_id = create_run(
            f"F11-S6 database reconnect {self._attempt_id}", domain="resilience"
        )
        command = ScientificCommandSpec(
            run_id=run_id,
            command_type="scientific.generic",
            aggregate_type="fault_state",
            aggregate_id=self._identity("database-state"),
            idempotency_key=self._identity("database-command"),
            source_event_key=self._identity("database-source"),
            input={"scenario": spec.scenario_id, "attempt_id": self._attempt_id},
            principal=self._role("database"),
            event_type="fault_database_state_committed",
        )
        self._require(command.command_id is not None, "database command has no identity")

        def apply(session):
            row = Decision(
                run_id=run_id,
                stage_from="fault",
                stage_to="recovered",
                rationale=command.aggregate_id,
                actor=self._role("database"),
                scientific_command_id=command.command_id,
            )
            session.add(row)
            session.flush()
            return ScientificMutation(
                result={"decision_id": row.id},
                event_projection={"decision_id": row.id},
            )

        def disconnect(point, _session):
            if point == "after_event_before_receipt":
                raise ConnectionError("injected database connection loss")

        transition = ScientificTransitionStore()
        try:
            transition.execute(command, apply, fault_hook=disconnect)
        except ConnectionError as exc:
            self._require("injected" in str(exc), "database failure was not the injected loss")
        else:
            raise FaultHarnessExecutionError("database connection injection did not abort")
        with session_scope() as session:
            rolled_back = (
                session.get(ScientificCommandRecord, command.command_id) is None
                and session.scalar(
                    select(Decision).where(
                        Decision.scientific_command_id == command.command_id
                    )
                )
                is None
                and session.scalar(
                    select(Event).where(Event.event_key == command.output_event_key)
                )
                is None
            )
        self._require(rolled_back, "database fault left a partial scientific transaction")
        engine().dispose()
        committed = transition.execute(command, apply)

        def replay_must_not_apply(_session):
            raise FaultHarnessExecutionError("database command replay invoked its mutation")

        replay = transition.execute(command, replay_must_not_apply)
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(Decision)
                .where(Decision.scientific_command_id == command.command_id)
            )
            command_count = session.scalar(
                select(func.count())
                .select_from(ScientificCommandRecord)
                .where(ScientificCommandRecord.command_id == command.command_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == command.output_event_key)
            )
        self._require(
            state_count is not None and command_count is not None and event_count is not None,
            "database recovery counts were unavailable",
        )
        assert state_count is not None and command_count is not None and event_count is not None
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(
                    len({state_count, command_count, event_count}) != 1
                ),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.COMMITTED_COMMAND_COUNT: command_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(
                    committed.created and not replay.created
                ),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "command_id": command.command_id,
                "rollback_was_clean": rolled_back,
                "state_count": state_count,
                "command_count": command_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _retrying_worker(
        self,
        spec: FaultScenarioSpec,
        *,
        label: str,
        first_failure: type[Exception],
    ) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = self._task_spec(label)
        queue = DurableTaskQueue(principal=self._role(label))
        queue.enqueue(task)
        calls = 0

        async def handler(_task):
            nonlocal calls
            calls += 1
            if calls == 1:
                if first_failure is TimeoutError:
                    raise TimeoutError("injected evaluator timeout")
                raise InfrastructureTaskFailure("injected provider unavailable")
            return self._result(f"{label}-success")

        worker = DurableWorker(
            worker_id=f"fault-worker-{label}",
            worker_manifest_sha256=content_sha256(
                {"schema": "aletheia.fault_worker.v1", "label": label}
            ),
            handlers={task.task_type: handler},
            queue=queue,
        )
        first = asyncio.run(worker.run_once())
        second = asyncio.run(worker.run_once())
        self._require(first is not None and second is not None, "worker retry was not executed")
        assert first is not None and second is not None
        attempts = queue.attempts(task.task_id)
        infrastructure_failures = sum(
            item.terminal_category is not None
            and item.terminal_category.value == "infrastructure"
            for item in attempts
        )
        succeeded = int(second.task.status is TaskStatus.SUCCEEDED)
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: (
                    infrastructure_failures
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "task_id": task.task_id,
                "handler_call_count": calls,
                "attempt_categories": [
                    item.terminal_category.value if item.terminal_category else None
                    for item in attempts
                ],
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _evaluator(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        return self._retrying_worker(
            spec,
            label="evaluator-timeout",
            first_failure=TimeoutError,
        )

    def _provider(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        return self._retrying_worker(
            spec,
            label="provider-unavailable",
            first_failure=InfrastructureTaskFailure,
        )

    def _duplicate_delivery(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        from aletheia.memory.service import create_run

        started = datetime.now(timezone.utc)
        run_id = create_run(
            f"F11-S6 duplicate delivery {self._attempt_id}", domain="resilience"
        )
        command = ScientificCommandSpec(
            run_id=run_id,
            command_type="scientific.generic",
            aggregate_type="fault_state",
            aggregate_id=self._identity("duplicate-state"),
            idempotency_key=self._identity("duplicate-command"),
            source_event_key=self._identity("duplicate-source"),
            input={"scenario": spec.scenario_id, "attempt_id": self._attempt_id},
            principal=self._role("duplicate"),
            event_type="fault_duplicate_state_committed",
        )
        self._require(command.command_id is not None, "duplicate command has no identity")
        calls = 0

        def apply(session):
            nonlocal calls
            calls += 1
            row = Decision(
                run_id=run_id,
                stage_from="delivery",
                stage_to="committed",
                rationale=command.aggregate_id,
                actor=self._role("duplicate"),
                scientific_command_id=command.command_id,
            )
            session.add(row)
            session.flush()
            return ScientificMutation(
                result={"decision_id": row.id},
                event_projection={"decision_id": row.id},
            )

        transition = ScientificTransitionStore()
        first = transition.execute(command, apply)
        replay = transition.execute(command, apply)
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(Decision)
                .where(Decision.scientific_command_id == command.command_id)
            )
            command_count = session.scalar(
                select(func.count())
                .select_from(ScientificCommandRecord)
                .where(ScientificCommandRecord.command_id == command.command_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == command.output_event_key)
            )
        self._require(
            state_count is not None and command_count is not None and event_count is not None,
            "duplicate-delivery counts were unavailable",
        )
        self._require(calls == 1, "duplicate delivery invoked its mutation more than once")
        assert state_count is not None and command_count is not None and event_count is not None
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(
                    len({state_count, command_count, event_count}) != 1
                ),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.COMMITTED_COMMAND_COUNT: command_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(
                    first.created and not replay.created
                ),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "command_id": command.command_id,
                "mutation_call_count": calls,
                "state_count": state_count,
                "command_count": command_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _stale_lease(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = self._task_spec("stale-lease", lease_seconds=2)
        queue = DurableTaskQueue(principal=self._role("stale-lease"))
        queue.enqueue(task)
        stale = queue.claim(
            worker_id="stale-owner",
            worker_manifest_sha256="c" * 64,
            task_types=(task.task_type,),
        )
        self._require(stale is not None, "stale-lease worker did not claim its task")
        assert stale is not None
        recovered_at = stale.lease_expires_at + timedelta(microseconds=1)
        recovered = queue.recover_expired(now=recovered_at)
        replacement = queue.claim(
            worker_id="lease-replacement",
            worker_manifest_sha256="d" * 64,
            task_types=(task.task_type,),
            now=recovered_at,
        )
        self._require(replacement is not None, "stale lease was not reclaimed")
        assert replacement is not None
        try:
            queue.complete(stale, self._result("late-stale"), now=recovered_at)
        except (InvalidTaskTransition, LeaseMismatch):
            rejected_stale = 1
        else:
            raise FaultHarnessExecutionError("stale lease callback was accepted")
        completed = queue.complete(
            replacement,
            self._result("lease-recovered"),
            now=recovered_at + timedelta(microseconds=1),
        )
        attempts = queue.attempts(task.task_id)
        succeeded = int(completed.task.status is TaskStatus.SUCCEEDED)
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RECOVERED_TASK_COUNT: int(
                    task.task_id in recovered.recovered_task_ids
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
                FaultMetric.REJECTED_STALE_CALLBACK_COUNT: rejected_stale,
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "task_id": task.task_id,
                "attempt_ids": [item.attempt_id for item in attempts],
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _archive_storage(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        if self._campaign_id is None:  # pragma: no cover - run sets this before execution
            raise FaultHarnessExecutionError("archive scenario has no campaign identity")
        if self._quest_id is None:  # pragma: no cover - run sets this before execution
            raise FaultHarnessExecutionError("archive scenario has no Quest identity")
        quest_id = self._quest_id
        root = self.archive_root or (
            artifacts_dir() / "fault_campaigns" / self._campaign_id / "archive-exhaustion"
        )
        archive = _ExhaustedScientificMemoryArchive(root)
        memory = ResearchMemoryStore(archive=archive)
        task_key = f"fault-resume-{self._attempt_id[:20]}"
        fact = ResearchMemoryFactSpec(
            scope_node_id=quest_id,
            kind=MemoryFactKind.NEGATIVE_RESULT,
            statement=(
                "Archive-exhaustion probe preserved its exact negative result "
                f"for attempt {self._attempt_id}."
            ),
            detail={"scenario": spec.scenario_id, "attempt_id": self._attempt_id},
            task_bindings=(
                MemoryTaskBindingSpec(
                    task_key=task_key,
                    context_role=MemoryContextRole.REQUIRED,
                ),
            ),
            sources=(
                MemorySourceRef(
                    kind=MemorySourceKind.ARTIFACT,
                    source_id=self._identity("fault-memory-source"),
                    sha256=content_sha256(
                        {
                            "schema": "aletheia.fault_memory_source.v1",
                            "attempt_id": self._attempt_id,
                        }
                    ),
                ),
            ),
        )
        memory.register_fact(
            fact,
            GraphCommandContext(
                idempotency_key=self._identity("fault-memory-fact"),
                principal=self._role("memory"),
            ),
        )
        draft = MemorySummaryDraft(
            producer_provider="fault-harness",
            producer_model="archive-exhaustion",
            prompt_sha256=content_sha256(
                {
                    "schema": "aletheia.fault_archive_prompt.v1",
                    "attempt_id": self._attempt_id,
                }
            ),
            summary_text="The injected archive exhaustion preserved the source fact.",
            covered_fact_ids=(fact.fact_id,),
        )
        try:
            memory.compact(
                scope_node_id=quest_id,
                task_key=task_key,
                draft=draft,
                context=GraphCommandContext(
                    idempotency_key=self._identity("fault-memory-compact"),
                    principal=self._role("memory"),
                ),
            )
        except OSError as exc:
            self._require(exc.errno == errno.ENOSPC, "archive injection was not ENOSPC")
            observed_errno = exc.errno
        else:
            raise FaultHarnessExecutionError("archive exhaustion injection did not fail")
        facts = memory.eligible_facts(quest_id, task_key)
        with session_scope() as session:
            compaction_count = session.scalar(
                select(func.count())
                .select_from(ResearchMemoryCompactionRecord)
                .where(
                    ResearchMemoryCompactionRecord.scope_node_id == quest_id,
                    ResearchMemoryCompactionRecord.task_key == task_key,
                )
            )
            fact_count = session.scalar(
                select(func.count())
                .select_from(ResearchMemoryFactRecord)
                .where(ResearchMemoryFactRecord.fact_id == fact.fact_id)
            )
        files = tuple(path for path in root.rglob("*") if path.is_file())
        self._require(
            compaction_count is not None and fact_count is not None,
            "archive recovery counts were unavailable",
        )
        self._require(
            tuple(item.fact_id for item in facts) == (fact.fact_id,),
            "source memory fact was not reconstructible after archive exhaustion",
        )
        assert compaction_count is not None and fact_count is not None
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - fact_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(fact_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: fact_count,
                FaultMetric.COMMITTED_ARCHIVE_COUNT: compaction_count,
                FaultMetric.ORPHAN_ARCHIVE_COUNT: len(files),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "fact_id": fact.fact_id,
                "task_key": task_key,
                "fact_count": fact_count,
                "compaction_count": compaction_count,
                "orphan_relative_paths": [str(item.relative_to(root)) for item in files],
                "errno": observed_errno,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _runtime_identity(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = self._task_spec("runtime-identity")
        queue = DurableTaskQueue(principal=self._role("runtime-identity"))
        queue.enqueue(task)
        lease = queue.claim(
            worker_id="identity-worker",
            worker_manifest_sha256="e" * 64,
            task_types=(task.task_type,),
        )
        self._require(lease is not None, "identity worker did not claim its task")
        assert lease is not None
        forged = lease.model_copy(update={"worker_manifest_sha256": "f" * 64})
        try:
            queue.complete(forged, self._result("forged-runtime"))
        except LeaseMismatch:
            rejected = 1
        else:
            raise FaultHarnessExecutionError("forged runtime identity was accepted")
        self._require(
            queue.get(task.task_id).status is TaskStatus.LEASED,
            "forged runtime callback mutated the leased task",
        )
        completed = queue.complete(lease, self._result("verified-runtime"))
        attempts = queue.attempts(task.task_id)
        succeeded = int(completed.task.status is TaskStatus.SUCCEEDED)
        values = self._measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.REJECTED_RUNTIME_MISMATCH_COUNT: rejected,
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "task_id": task.task_id,
                "accepted_manifest_sha256": lease.worker_manifest_sha256,
                "rejected_manifest_sha256": forged.worker_manifest_sha256,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def _outward_action(self, spec: FaultScenarioSpec) -> FaultScenarioObservation:
        from aletheia.memory.service import create_run

        started = datetime.now(timezone.utc)
        run_id = create_run(
            f"F11-S6 ambiguous outward action {self._attempt_id}", domain="resilience"
        )
        action = OneTimeExternalActionSpec(
            run_id=run_id,
            action_type="provider.request",
            scope_key=self._identity("fault-outward"),
            request={
                "scenario": spec.scenario_id,
                "payload_sha256": content_sha256(
                    {
                        "schema": "aletheia.fault_outward_payload.v1",
                        "attempt_id": self._attempt_id,
                    }
                ),
            },
            principal=self._role("outward"),
            claim_ttl_seconds=1,
        )
        store = OneTimeExternalActionStore()
        claim = store.claim(action, claim_owner="fault-outward-worker")
        self._require(
            claim.execution_token is not None,
            "outward action did not produce its one-time authorization",
        )
        recovered = store.recover_stale(
            now=claim.action.reconcile_after + timedelta(microseconds=1)
        )
        replay = store.claim(
            action,
            claim_owner="fault-outward-replacement",
            now=claim.action.reconcile_after + timedelta(seconds=1),
        )
        self._require(
            replay.execution_token is None,
            "ambiguous outward action produced a second execution token",
        )
        with session_scope() as session:
            authorization_count = session.scalar(
                select(func.count())
                .select_from(OneTimeExternalActionRecord)
                .where(OneTimeExternalActionRecord.action_id == action.action_id)
            )
            receipt_count = session.scalar(
                select(func.count())
                .select_from(ExternalActionReceiptRecord)
                .where(ExternalActionReceiptRecord.action_id == action.action_id)
            )
        self._require(
            authorization_count is not None and receipt_count is not None,
            "outward-action recovery counts were unavailable",
        )
        assert authorization_count is not None and receipt_count is not None
        reconciliation_count = int(
            replay.action.status is ExternalActionStatus.RECONCILIATION_REQUIRED
            and action.action_id in recovered.action_ids
        )
        values = self._measured(
            spec,
            core={
                FaultMetric.DUPLICATE_OUTWARD_AUTHORIZATION_COUNT: max(
                    authorization_count - 1, 0
                ),
                FaultMetric.UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT: int(
                    reconciliation_count == 0
                ),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.OUTWARD_AUTHORIZATION_COUNT: authorization_count,
                FaultMetric.OUTWARD_RECEIPT_COUNT: receipt_count,
                FaultMetric.RECONCILIATION_REQUIRED_COUNT: reconciliation_count,
            },
        )
        return self._observation(
            spec,
            measured=values,
            detail={
                "action_id": action.action_id,
                "authorization_count": authorization_count,
                "receipt_count": receipt_count,
                "status": replay.action.status.value,
                "second_execution_token_returned": replay.execution_token is not None,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def run(self, manifest: FaultCampaignManifest) -> FaultHarnessEvidenceBundle:
        manifest = FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
        validate_fault_harness_environment(manifest, self.environment)
        self._require(
            datetime.now(timezone.utc) >= manifest.created_at,
            "fault campaign manifest was created in the future",
        )
        assert manifest.quest_id is not None
        assert manifest.campaign_id is not None
        self._quest_id = manifest.quest_id
        self._campaign_id = manifest.campaign_id
        self._diagnostics = {}
        executors = {
            FaultBoundary.API_PROCESS: self._api_process,
            FaultBoundary.WORKER_PROCESS: self._worker_process,
            FaultBoundary.DATABASE_CONNECTION: self._database_connection,
            FaultBoundary.EVALUATOR: self._evaluator,
            FaultBoundary.PROVIDER: self._provider,
            FaultBoundary.DUPLICATE_DELIVERY: self._duplicate_delivery,
            FaultBoundary.STALE_LEASE: self._stale_lease,
            FaultBoundary.ARCHIVE_STORAGE: self._archive_storage,
            FaultBoundary.RUNTIME_IDENTITY: self._runtime_identity,
            FaultBoundary.OUTWARD_ACTION: self._outward_action,
        }
        specs = {item.boundary: item for item in manifest.scenarios}
        report = run_fault_campaign(
            manifest,
            {
                specs[boundary].scenario_id: executor
                for boundary, executor in executors.items()
            },
            clock=lambda: datetime.now(timezone.utc),
        )
        return FaultHarnessEvidenceBundle(
            environment=self.environment,
            report=report,
            diagnostics=self._diagnostics,
        )


def run_durable_fault_campaign(
    manifest: FaultCampaignManifest,
    *,
    environment: FaultHarnessEnvironmentManifest,
    principal: str,
    archive_root: Path | None = None,
) -> FaultHarnessEvidenceBundle:
    """Convenience entry point for the supported production harness."""

    return DurableFaultHarness(
        environment=environment,
        principal=principal,
        archive_root=archive_root,
    ).run(manifest)


def validate_fault_harness_bundle(
    bundle: FaultHarnessEvidenceBundle,
) -> FaultHarnessEvidenceBundle:
    """Reconstruct all environment, diagnostic, metric, report, and bundle hashes."""

    return FaultHarnessEvidenceBundle.model_validate(bundle.model_dump(mode="python"))


__all__ = [
    "DurableFaultHarness",
    "FaultHarnessEnvironmentManifest",
    "FaultHarnessEnvironmentMismatch",
    "FaultHarnessError",
    "FaultHarnessEvidenceBundle",
    "FaultHarnessExecutionError",
    "capture_fault_harness_environment",
    "prepare_durable_fault_campaign",
    "run_durable_fault_campaign",
    "validate_fault_harness_bundle",
    "validate_fault_harness_environment",
]
