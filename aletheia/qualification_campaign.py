"""Opt-in target-host qualification campaign with crash-replayable evidence.

This is the first boundary allowed to enable and start the five qualification services.  It binds
one already-atomic scientific execution registration, kills real service/database processes, and
accepts a deployment verdict only after an independent signed observation and immutable terminal
outbox envelope survive recovery.  It never grants scientific-observation admission authority.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aletheia.execution.oci_deployment import (
    LoopbackOutputQuotaProvisionerClient,
    SystemdDeadlineWatchdogController,
)
from aletheia.execution.qualification_deployment import (
    QualificationDeploymentPreflight,
    QualificationDeploymentSpecV1,
    QualificationInstalledDeploymentManifestV1,
    RenderedSystemdUnit,
    SignedQualificationLinuxDeploymentObservation,
    freeze_installed_manifest,
    qualification_postgresql_peer_database_url,
    render_postgresql_acl,
    render_systemd_units,
    verify_installed_manifest,
    verify_recorded_installed_manifest_observation,
)
from aletheia.execution.qualification_outbox_service import (
    QualificationTerminalSpoolEnvelopeV1,
    qualification_terminal_spool_envelope_from_row,
)
from aletheia.execution.runtime_v2_contracts import (
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    OutputQuotaProvisioningReceipt,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.observations.scientific_bridge import ScientificExecutionAuthorization
from aletheia.qualification_observer import (
    LinuxQualificationDeploymentObserver,
    QualificationLinuxObserverConfigV1,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_MAX_CHILD_BYTES = 4 * 1024 * 1024
_OPT_IN_CONFIRMATION = "RUN_QUALIFICATION_TARGET_CAMPAIGN"
_ADMIN_DATABASE_URL_ENV = "ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL"

# Keep this statement executable by PostgreSQL itself, not only by SQLite-backed
# test doubles.  AUTHORIZATION is a reserved PostgreSQL keyword and therefore
# must not be used as an unquoted relation alias.
_ATTEMPT_PROJECTION_SQL = """
    SELECT attempt.status, attempt.intent_sha256, attempt.admission_sha256,
           attempt.grant_sha256, attempt.bundle_sha256, attempt.node_id,
           attempt.hard_deadline, attempt.accepted_terminal_submission_sha256,
           attempt.terminal_deadline_expiration_sha256,
           envelope.node_manifest_sha256,
           envelope.resource_lease_sha256,
           sea.authorization_sha256,
           sea.quest_id,
           sea.scientific_slot_id,
           sea.action_sha256,
           sea.qualification_bundle_sha256,
           sea.qualification_grant_sha256,
           sea.authorization_json,
           sea.authorized_at,
           sea.expires_at,
           sea.observation_admission_deadline,
           sea.registered_at
      FROM execution_attempts AS attempt
      JOIN execution_assignment_envelopes AS envelope
        ON envelope.attempt_id = attempt.attempt_id
      JOIN research_scientific_execution_authorizations AS sea
        ON sea.execution_id = attempt.execution_id
       AND sea.attempt_id = attempt.attempt_id
     WHERE attempt.execution_id = :execution_id
       AND attempt.attempt_id = :attempt_id
"""


class QualificationCampaignError(RuntimeError):
    """The campaign request, target, recovery, or evidence failed closed."""


class QualificationCampaignExecutionExpectationV1(ExecutionModel):
    """One pre-reserved real execution whose node recovery and terminal path are exercised."""

    schema_name: Literal["aletheia.qualification_campaign_execution_expectation"] = (
        "aletheia.qualification_campaign_execution_expectation"
    )
    schema_version: Literal[1] = 1
    registration_receipt: AtomicScientificExecutionRegistrationReceipt
    expected_terminal_disposition: Literal["process_succeeded"] = "process_succeeded"
    kill_at_status: Literal["running"] = "running"
    terminal_status: Literal["succeeded"] = "succeeded"
    require_v2_terminal_outbox: Literal[True] = True
    require_durable_spool: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @property
    def expectation_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationTargetCampaignRequestV1(ExecutionModel):
    """Externally pinned, explicit authorization for one destructive target campaign."""

    schema_name: Literal["aletheia.qualification_target_campaign_request"] = (
        "aletheia.qualification_target_campaign_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=r"^qtr_[0-9a-f]{32}$")
    observer_config: QualificationLinuxObserverConfigV1
    execution: QualificationCampaignExecutionExpectationV1
    campaign_journal_root: str
    requested_at: AwareDatetime
    maximum_campaign_seconds: int = Field(default=900, ge=60, le=7200)
    poll_milliseconds: int = Field(default=250, ge=50, le=5000)
    quota_probe_bytes: int = Field(
        default=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        ge=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        le=1024**3,
    )
    opt_in_confirmation: Literal["RUN_QUALIFICATION_TARGET_CAMPAIGN"] = _OPT_IN_CONFIRMATION
    enable_services: Literal[True] = True
    start_services: Literal[True] = True
    kill_service_processes: Literal[True] = True
    terminate_postgresql_backend: Literal[True] = True
    automatic_start: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_closed(self) -> "QualificationTargetCampaignRequestV1":
        config = self.observer_config
        installation = config.commissioning_request.installation_request
        registration = self.execution.registration_receipt
        journal = Path(self.campaign_journal_root)
        installer_journal = Path(installation.journal_root)
        if (
            self.requested_at.utcoffset() != timedelta(0)
            or self.requested_at < config.prepared_at
            or registration.reserved_at > self.requested_at
            or not journal.is_absolute()
            or str(journal) != os.path.normpath(self.campaign_journal_root)
            or journal.parent != installer_journal
            or journal == installer_journal
            or any(character in self.campaign_journal_root for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("campaign chronology or journal scope differs")
        expected_id = f"qtr_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected_id:
            raise ValueError("campaign request id is not derived")
        object.__setattr__(self, "request_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


_CAMPAIGN_SCENARIOS = (
    "activate_exact_systemd_units",
    "independent_installed_manifest",
    "node_process_kill_execution_recovery",
    "terminal_outbox_process_kill_recovery",
    "node_postgresql_peer",
    "outbox_postgresql_peer",
    "loop_ext4_quota_and_root_health",
    "quota_watchdog_process_kill_recovery",
    "postgresql_backend_kill_rollback",
    "independent_post_kill_reobservation",
)


class QualificationTargetCampaignPlanV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_target_campaign_plan"] = (
        "aletheia.qualification_target_campaign_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str | None = Field(default=None, pattern=r"^qtp_[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^qtr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    observer_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    installation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    commissioning_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_registration_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    scenarios: tuple[str, ...] = Field(
        min_length=len(_CAMPAIGN_SCENARIOS),
        max_length=len(_CAMPAIGN_SCENARIOS),
    )
    campaign_executed: Literal[False] = False
    deployment_qualified: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_canonical(self) -> "QualificationTargetCampaignPlanV1":
        if self.scenarios != _CAMPAIGN_SCENARIOS:
            raise ValueError("campaign scenarios are not exhaustive and ordered")
        expected_id = f"qtp_{self.identity_sha256[:32]}"
        if self.plan_id is not None and self.plan_id != expected_id:
            raise ValueError("campaign plan id is not derived")
        object.__setattr__(self, "plan_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"}))

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)


def build_qualification_target_campaign_plan(
    request: QualificationTargetCampaignRequestV1,
) -> QualificationTargetCampaignPlanV1:
    frozen = QualificationTargetCampaignRequestV1.model_validate(request.model_dump(mode="python"))
    config = frozen.observer_config
    return QualificationTargetCampaignPlanV1(
        request_id=frozen.request_id,
        request_sha256=frozen.request_sha256,
        deployment_id=config.commissioning_request.installation_request.deployment_spec.deployment_id,
        observer_config_sha256=config.config_sha256,
        installation_receipt_sha256=config.installation_receipt.receipt_sha256,
        commissioning_receipt_sha256=config.commissioning_receipt.receipt_sha256,
        execution_registration_receipt_sha256=(
            frozen.execution.registration_receipt.receipt_sha256
        ),
        scenarios=_CAMPAIGN_SCENARIOS,
    )


class QualificationSystemdActivationReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_systemd_activation_receipt"] = (
        "aletheia.qualification_systemd_activation_receipt"
    )
    schema_version: Literal[1] = 1
    unit_names: tuple[str, ...] = Field(min_length=5, max_length=5)
    main_pids: tuple[tuple[str, int], ...] = Field(min_length=4, max_length=4)
    activated_at: AwareDatetime
    all_loaded: Literal[True] = True
    all_enabled: Literal[True] = True
    all_active: Literal[True] = True

    @model_validator(mode="after")
    def _activation_is_canonical(self) -> "QualificationSystemdActivationReceiptV1":
        if (
            self.unit_names != tuple(sorted(set(self.unit_names)))
            or tuple(name for name, _pid in self.main_pids)
            != tuple(sorted(name for name, _pid in self.main_pids))
            or any(pid <= 1 for _name, pid in self.main_pids)
            or self.activated_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("systemd activation receipt is not canonical")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLPeerProbeReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_postgresql_peer_probe"] = (
        "aletheia.qualification_postgresql_peer_probe"
    )
    schema_version: Literal[1] = 1
    role_kind: Literal["node", "outbox"]
    process_uid: int = Field(ge=1)
    process_gid: int = Field(ge=1)
    postgresql_role: str
    database_name: str
    schema_revision: str
    database_clock: AwareDatetime
    probed_at: AwareDatetime
    password_supplied: Literal[False] = False
    local_unix_peer: Literal[True] = True

    @model_validator(mode="after")
    def _probe_is_ordered(self) -> "QualificationPostgreSQLPeerProbeReceiptV1":
        if (
            self.database_clock.utcoffset() != timedelta(0)
            or self.probed_at.utcoffset() != timedelta(0)
            or abs((self.probed_at - self.database_clock).total_seconds()) > 1
        ):
            raise ValueError("PostgreSQL peer probe clocks differ")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationQuotaCampaignReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_quota_campaign_receipt"] = (
        "aletheia.qualification_quota_campaign_receipt"
    )
    schema_version: Literal[1] = 1
    quota_health_before_sha256: str = Field(pattern=_SHA256_PATTERN)
    watchdog_health_before_sha256: str = Field(pattern=_SHA256_PATTERN)
    provisioning_receipt: OutputQuotaProvisioningReceipt
    loop_device: str = Field(pattern=r"^/dev/loop[0-9]+$")
    replayed_provisioning_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    quota_health_after_sha256: str = Field(pattern=_SHA256_PATTERN)
    watchdog_health_after_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    real_loop_device: Literal[True] = True
    real_ext4_filesystem: Literal[True] = True
    exact_replay: Literal[True] = True

    @model_validator(mode="after")
    def _quota_is_exact(self) -> "QualificationQuotaCampaignReceiptV1":
        receipt = self.provisioning_receipt
        if (
            receipt.filesystem_type != "ext4"
            or self.replayed_provisioning_receipt_sha256 != receipt.provisioning_receipt_sha256
            or self.quota_health_before_sha256 == self.quota_health_after_sha256
            or self.watchdog_health_before_sha256 == self.watchdog_health_after_sha256
            or self.completed_at.utcoffset() != timedelta(0)
            or self.completed_at < receipt.provisioned_at
        ):
            raise ValueError("quota campaign did not prove one replayed loop/ext4 generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationServiceKillReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_service_kill_receipt"] = (
        "aletheia.qualification_service_kill_receipt"
    )
    schema_version: Literal[1] = 1
    unit_name: str
    pid_before: int = Field(ge=2)
    pid_after: int = Field(ge=2)
    killed_at: AwareDatetime
    recovered_at: AwareDatetime
    signal_name: Literal["SIGKILL"] = "SIGKILL"
    systemd_restart_observed: Literal[True] = True

    @model_validator(mode="after")
    def _kill_is_real(self) -> "QualificationServiceKillReceiptV1":
        if (
            self.killed_at.utcoffset() != timedelta(0)
            or self.recovered_at.utcoffset() != timedelta(0)
            or self.pid_before == self.pid_after
            or self.recovered_at <= self.killed_at
        ):
            raise ValueError("service process kill did not produce one later process")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationPostgreSQLKillReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_postgresql_kill_receipt"] = (
        "aletheia.qualification_postgresql_kill_receipt"
    )
    schema_version: Literal[1] = 1
    postgresql_role: str
    backend_pid: int = Field(ge=2)
    advisory_lock_key: int
    transaction_started_at: AwareDatetime
    terminated_at: AwareDatetime
    reconnected_at: AwareDatetime
    transaction_connection_lost: Literal[True] = True
    transaction_lock_released: Literal[True] = True
    peer_reconnect_verified: Literal[True] = True

    @model_validator(mode="after")
    def _rollback_is_ordered(self) -> "QualificationPostgreSQLKillReceiptV1":
        if (
            any(
                item.utcoffset() != timedelta(0)
                for item in (
                    self.transaction_started_at,
                    self.terminated_at,
                    self.reconnected_at,
                )
            )
            or not self.transaction_started_at <= self.terminated_at < self.reconnected_at
        ):
            raise ValueError("PostgreSQL reconnect did not follow backend termination")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationSpoolFileObservationV1(ExecutionModel):
    path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1, le=_MAX_JOURNAL_BYTES)
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=1)
    owner_gid: int = Field(ge=1)
    mode: Literal[0o400] = 0o400
    link_count: Literal[1] = 1

    @model_validator(mode="after")
    def _path_is_canonical(self) -> "QualificationSpoolFileObservationV1":
        path = Path(self.path)
        if not path.is_absolute() or str(path) != os.path.normpath(self.path):
            raise ValueError("spool file path is not canonical")
        return self


class QualificationOutboxQuiescenceReceiptV1(ExecutionModel):
    """Proof that the outbox was down before the selected terminal row existed."""

    schema_name: Literal["aletheia.qualification_outbox_quiescence_receipt"] = (
        "aletheia.qualification_outbox_quiescence_receipt"
    )
    schema_version: Literal[1] = 1
    unit_name: str
    prior_pid: int = Field(ge=2)
    baseline_spool_filenames: tuple[str, ...]
    stopped_at: AwareDatetime
    unit_inactive: Literal[True] = True
    selected_attempt_terminal_absent: Literal[True] = True

    @model_validator(mode="after")
    def _quiescence_is_canonical(self) -> "QualificationOutboxQuiescenceReceiptV1":
        if (
            self.baseline_spool_filenames != tuple(sorted(set(self.baseline_spool_filenames)))
            or any(
                re.fullmatch(r"(?:qto|xob)_[0-9a-f]{64}[.]json", item) is None
                for item in self.baseline_spool_filenames
            )
            or self.stopped_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("outbox quiescence receipt is not canonical")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationTerminalCampaignReceiptV1(ExecutionModel):
    schema_name: Literal["aletheia.qualification_terminal_campaign_receipt"] = (
        "aletheia.qualification_terminal_campaign_receipt"
    )
    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^iat_[0-9a-f]{32}$")
    outbox_quiescence: QualificationOutboxQuiescenceReceiptV1
    node_kill: QualificationServiceKillReceiptV1
    outbox_kill: QualificationServiceKillReceiptV1
    terminal_envelope: QualificationTerminalSpoolEnvelopeV1
    spool_file: QualificationSpoolFileObservationV1
    completed_at: AwareDatetime
    exact_attempt_recovered: Literal[True] = True
    terminal_row_exactly_once: Literal[True] = True
    terminal_row_committed_while_outbox_stopped: Literal[True] = True
    durable_spool_recovered: Literal[True] = True
    durable_spool_replayed_after_outbox_kill: Literal[True] = True

    @model_validator(mode="after")
    def _terminal_is_exact(self) -> "QualificationTerminalCampaignReceiptV1":
        envelope = self.terminal_envelope
        if (
            envelope.execution_id != self.execution_id
            or envelope.attempt_id != self.attempt_id
            or self.outbox_quiescence.unit_name != self.outbox_kill.unit_name
            or envelope.terminal_authority_kind != "accepted_terminal_submission"
            or getattr(envelope.payload, "disposition", None) != "process_succeeded"
            or self.spool_file.path != str(Path(self.spool_file.path).parent / envelope.filename)
            or self.spool_file.sha256 != hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
            or envelope.filename in self.outbox_quiescence.baseline_spool_filenames
            or self.node_kill.killed_at < self.outbox_quiescence.stopped_at
            or envelope.source_created_at
            < max(self.outbox_quiescence.stopped_at, self.node_kill.recovered_at)
            or self.outbox_kill.killed_at < envelope.source_created_at
            or self.completed_at.utcoffset() != timedelta(0)
            or self.completed_at < max(self.node_kill.recovered_at, self.outbox_kill.recovered_at)
        ):
            raise ValueError("terminal campaign evidence differs from exact successful attempt")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationCampaignReobservationEvidenceV1(ExecutionModel):
    """Retained signed post-kill projection and its mechanically derived preflight."""

    schema_name: Literal["aletheia.qualification_campaign_reobservation_evidence"] = (
        "aletheia.qualification_campaign_reobservation_evidence"
    )
    schema_version: Literal[1] = 1
    signed_observation: SignedQualificationLinuxDeploymentObservation
    preflight: QualificationDeploymentPreflight
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _evidence_is_bound(self) -> "QualificationCampaignReobservationEvidenceV1":
        if (
            self.preflight.observed_at != self.signed_observation.observation.observed_at
            or self.preflight.verified_at < self.signed_observation.signed_at
            or not self.preflight.ready_for_opt_in_campaign
            or self.preflight.blockers
        ):
            raise ValueError("campaign reobservation differs from its signed projection")
        return self

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationTargetCampaignReceiptV1(ExecutionModel):
    """Final deployment-only verdict; all full evidence remains embedded and replayable."""

    schema_name: Literal["aletheia.qualification_target_campaign_receipt"] = (
        "aletheia.qualification_target_campaign_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^qtx_[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^qtr_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r"^qtp_[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    activation: QualificationSystemdActivationReceiptV1
    installed_manifest: QualificationInstalledDeploymentManifestV1
    peer_probes: tuple[QualificationPostgreSQLPeerProbeReceiptV1, ...] = Field(
        min_length=2,
        max_length=2,
    )
    quota_campaign: QualificationQuotaCampaignReceiptV1
    root_service_kills: tuple[QualificationServiceKillReceiptV1, ...] = Field(
        min_length=2,
        max_length=2,
    )
    postgresql_kill: QualificationPostgreSQLKillReceiptV1
    terminal_campaign: QualificationTerminalCampaignReceiptV1
    final_reobservation: QualificationCampaignReobservationEvidenceV1
    scenario_evidence_sha256s: tuple[str, ...] = Field(
        min_length=len(_CAMPAIGN_SCENARIOS),
        max_length=len(_CAMPAIGN_SCENARIOS),
    )
    completed_at: AwareDatetime
    campaign_executed: Literal[True] = True
    deployment_qualified: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _verdict_is_derived(self) -> "QualificationTargetCampaignReceiptV1":
        final_preflight = self.final_reobservation.preflight
        destructive_evidence_completed_at = max(
            self.activation.activated_at,
            *(item.probed_at for item in self.peer_probes),
            self.quota_campaign.completed_at,
            *(item.recovered_at for item in self.root_service_kills),
            self.postgresql_kill.reconnected_at,
            self.terminal_campaign.completed_at,
        )
        expected = (
            self.activation.receipt_sha256,
            self.installed_manifest.manifest_sha256,
            self.terminal_campaign.node_kill.receipt_sha256,
            self.terminal_campaign.receipt_sha256,
            self.peer_probes[0].receipt_sha256,
            self.peer_probes[1].receipt_sha256,
            self.quota_campaign.receipt_sha256,
            canonical_sha256(self.root_service_kills),
            self.postgresql_kill.receipt_sha256,
            self.final_reobservation.evidence_sha256,
        )
        if (
            tuple(item.role_kind for item in self.peer_probes) != ("node", "outbox")
            or tuple(item.unit_name for item in self.root_service_kills)
            != tuple(sorted(item.unit_name for item in self.root_service_kills))
            or self.scenario_evidence_sha256s != expected
            or not final_preflight.ready_for_opt_in_campaign
            or final_preflight.blockers
            or final_preflight.installed_manifest_sha256 != self.installed_manifest.manifest_sha256
            or self.final_reobservation.signed_observation.observation.observation_started_at
            <= destructive_evidence_completed_at
            or self.completed_at.utcoffset() != timedelta(0)
            or self.completed_at < final_preflight.verified_at
        ):
            raise ValueError("campaign deployment verdict differs from embedded evidence")
        expected_id = f"qtx_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("campaign receipt id is not derived")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def verify_qualification_target_campaign_receipt(
    request: QualificationTargetCampaignRequestV1,
    receipt: QualificationTargetCampaignReceiptV1,
) -> QualificationTargetCampaignPlanV1:
    """Offline-verify every request, signature, scope, chronology, and derived evidence link."""

    try:
        request = QualificationTargetCampaignRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        receipt = QualificationTargetCampaignReceiptV1.model_validate(
            receipt.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationCampaignError("campaign request or receipt is invalid") from exc
    plan = build_qualification_target_campaign_plan(request)
    spec = request.observer_config.commissioning_request.installation_request.deployment_spec
    pin = request.observer_config.observer_pin
    registration = request.execution.registration_receipt
    expected_units = tuple(
        sorted(
            (
                spec.workspace_unit_name,
                spec.quota_unit_name,
                spec.watchdog_unit_name,
                spec.node_unit_name,
                spec.outbox_unit_name,
            )
        )
    )
    expected_running_units = tuple(
        sorted(
            (
                spec.quota_unit_name,
                spec.watchdog_unit_name,
                spec.node_unit_name,
                spec.outbox_unit_name,
            )
        )
    )
    terminal = receipt.terminal_campaign
    peers = receipt.peer_probes
    root_kills = receipt.root_service_kills
    final = receipt.final_reobservation
    if (
        receipt.request_id != request.request_id
        or receipt.request_sha256 != request.request_sha256
        or receipt.plan_id != plan.plan_id
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.installed_manifest.spec != spec
        or receipt.installed_manifest.observer_pin_sha256 != pin.pin_sha256
        or receipt.activation.unit_names != expected_units
        or tuple(name for name, _pid in receipt.activation.main_pids) != expected_running_units
        or terminal.execution_id != registration.execution_id
        or terminal.attempt_id != registration.attempt_id
        or terminal.node_kill.unit_name != spec.node_unit_name
        or terminal.outbox_quiescence.unit_name != spec.outbox_unit_name
        or terminal.outbox_kill.unit_name != spec.outbox_unit_name
        or tuple(item.role_kind for item in peers) != ("node", "outbox")
        or (peers[0].process_uid, peers[0].process_gid, peers[0].postgresql_role)
        != (spec.node_uid, spec.node_gid, spec.postgresql_allocator_role)
        or (peers[1].process_uid, peers[1].process_gid, peers[1].postgresql_role)
        != (spec.outbox_uid, spec.outbox_gid, spec.postgresql_outbox_role)
        or any(
            item.database_name != spec.postgresql_database
            or item.schema_revision != spec.expected_schema_revision
            for item in peers
        )
        or tuple(item.unit_name for item in root_kills)
        != tuple(sorted((spec.quota_unit_name, spec.watchdog_unit_name)))
        or receipt.postgresql_kill.postgresql_role != spec.postgresql_allocator_role
        or final.preflight.deployment_id != spec.deployment_id
        or final.preflight.spec_sha256 != spec.spec_sha256
    ):
        raise QualificationCampaignError("campaign receipt rebound its exact request scope")
    try:
        installed_preflight = verify_recorded_installed_manifest_observation(
            receipt.installed_manifest,
            receipt.installed_manifest.installed_observation,
            pin,
            verified_at=receipt.installed_manifest.frozen_at,
            strictly_after_manifest=False,
        )
        recomputed_final = verify_recorded_installed_manifest_observation(
            receipt.installed_manifest,
            final.signed_observation,
            pin,
            verified_at=final.preflight.verified_at,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationCampaignError("campaign signed observation chain is invalid") from exc
    if installed_preflight.blockers or recomputed_final != final.preflight:
        raise QualificationCampaignError("campaign preflight is not derived from retained evidence")
    if not (
        request.requested_at
        <= receipt.activation.activated_at
        <= receipt.installed_manifest.frozen_at
        <= terminal.outbox_quiescence.stopped_at
        <= terminal.node_kill.killed_at
        < terminal.node_kill.recovered_at
        <= terminal.terminal_envelope.source_created_at
        <= terminal.outbox_kill.killed_at
        < terminal.outbox_kill.recovered_at
        <= terminal.completed_at
        <= peers[0].probed_at
        <= peers[1].probed_at
        <= receipt.quota_campaign.provisioning_receipt.provisioned_at
    ):
        raise QualificationCampaignError("campaign phase chronology is not monotonic")
    if (
        any(
            item.killed_at < receipt.quota_campaign.provisioning_receipt.provisioned_at
            or item.recovered_at > receipt.quota_campaign.completed_at
            for item in root_kills
        )
        or receipt.postgresql_kill.transaction_started_at < receipt.quota_campaign.completed_at
        or final.signed_observation.observation.observation_started_at
        <= receipt.postgresql_kill.reconnected_at
        or receipt.completed_at < final.preflight.verified_at
    ):
        raise QualificationCampaignError("campaign destructive evidence chronology differs")
    return plan


class QualificationRootServiceCampaignEvidenceV1(ExecutionModel):
    quota_campaign: QualificationQuotaCampaignReceiptV1
    service_kills: tuple[QualificationServiceKillReceiptV1, ...] = Field(
        min_length=2,
        max_length=2,
    )

    @model_validator(mode="after")
    def _services_are_canonical(self) -> "QualificationRootServiceCampaignEvidenceV1":
        names = tuple(item.unit_name for item in self.service_kills)
        if names != tuple(sorted(set(names))):
            raise ValueError("root-service kill evidence is not canonical")
        return self


class QualificationTargetCampaignHostPort(Protocol):
    """Narrow mutation/observation port used by the crash-replayable state machine."""

    def assert_target(self) -> None: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def read_record(self, name: str) -> bytes | None: ...

    def write_record_once(self, name: str, payload: bytes) -> None: ...

    def activate_services(self) -> QualificationSystemdActivationReceiptV1: ...

    def freeze_installed_manifest(self) -> QualificationInstalledDeploymentManifestV1: ...

    def probe_postgresql_peer(
        self,
        role_kind: Literal["node", "outbox"],
    ) -> QualificationPostgreSQLPeerProbeReceiptV1: ...

    def run_root_service_campaign(self) -> QualificationRootServiceCampaignEvidenceV1: ...

    def run_postgresql_kill_campaign(self) -> QualificationPostgreSQLKillReceiptV1: ...

    def run_terminal_campaign(self) -> QualificationTerminalCampaignReceiptV1: ...

    def reobserve(
        self,
        manifest: QualificationInstalledDeploymentManifestV1,
    ) -> QualificationCampaignReobservationEvidenceV1: ...

    def revalidate_completed(self, receipt: QualificationTargetCampaignReceiptV1) -> None: ...


_ModelT = TypeVar("_ModelT", bound=ExecutionModel)


def _load_record(
    host: QualificationTargetCampaignHostPort,
    name: str,
    model: type[_ModelT],
) -> _ModelT | None:
    payload = host.read_record(name)
    if payload is None:
        return None
    try:
        value = model.model_validate_json(payload)
    except ValueError as exc:
        raise QualificationCampaignError(f"campaign journal record is invalid: {name}") from exc
    if canonical_json_bytes(value) != payload:
        raise QualificationCampaignError(f"campaign journal record is noncanonical: {name}")
    return value


def _record_phase(
    host: QualificationTargetCampaignHostPort,
    name: str,
    model: type[_ModelT],
    operation: Callable[[], _ModelT],
    *,
    fault: Callable[[str], None],
) -> _ModelT:
    existing = _load_record(host, name, model)
    if existing is not None:
        return existing
    value = model.model_validate(operation().model_dump(mode="python"))
    fault(f"after_operation:{name}")
    host.write_record_once(name, canonical_json_bytes(value))
    fault(f"after_record:{name}")
    return value


def run_qualification_target_campaign(
    request: QualificationTargetCampaignRequestV1,
    host: QualificationTargetCampaignHostPort,
    *,
    clock: Callable[[], datetime] | None = None,
    fault: Callable[[str], None] | None = None,
) -> QualificationTargetCampaignReceiptV1:
    """Run/resume one exact target campaign and return a deployment-only verdict."""

    frozen = QualificationTargetCampaignRequestV1.model_validate(request.model_dump(mode="python"))
    plan = build_qualification_target_campaign_plan(frozen)
    now = clock or (lambda: datetime.now(timezone.utc))
    inject = fault or (lambda _phase: None)
    last = frozen.requested_at

    def monitored_now() -> datetime:
        nonlocal last
        observed = now()
        if observed.utcoffset() != timedelta(0) or observed < last:
            raise QualificationCampaignError("campaign clock moved backwards or left UTC")
        if observed > frozen.requested_at + timedelta(seconds=frozen.maximum_campaign_seconds):
            raise QualificationCampaignError("campaign exceeded its externally frozen deadline")
        last = observed
        return observed

    host.assert_target()
    with host.lock():
        host.write_record_once("request.json", canonical_json_bytes(frozen))
        host.write_record_once("plan.json", canonical_json_bytes(plan))
        existing = _load_record(host, "receipt.json", QualificationTargetCampaignReceiptV1)
        if existing is not None:
            verify_qualification_target_campaign_receipt(frozen, existing)
            host.revalidate_completed(existing)
            return existing
        activation = _record_phase(
            host,
            "01-activation.json",
            QualificationSystemdActivationReceiptV1,
            host.activate_services,
            fault=inject,
        )
        manifest = _record_phase(
            host,
            "02-installed-manifest.json",
            QualificationInstalledDeploymentManifestV1,
            host.freeze_installed_manifest,
            fault=inject,
        )
        terminal = _record_phase(
            host,
            "03-terminal-campaign.json",
            QualificationTerminalCampaignReceiptV1,
            host.run_terminal_campaign,
            fault=inject,
        )
        node_peer = _record_phase(
            host,
            "04-node-peer.json",
            QualificationPostgreSQLPeerProbeReceiptV1,
            lambda: host.probe_postgresql_peer("node"),
            fault=inject,
        )
        outbox_peer = _record_phase(
            host,
            "05-outbox-peer.json",
            QualificationPostgreSQLPeerProbeReceiptV1,
            lambda: host.probe_postgresql_peer("outbox"),
            fault=inject,
        )
        root_campaign = _record_phase(
            host,
            "06-root-services.json",
            QualificationRootServiceCampaignEvidenceV1,
            host.run_root_service_campaign,
            fault=inject,
        )
        postgresql_kill = _record_phase(
            host,
            "07-postgresql-kill.json",
            QualificationPostgreSQLKillReceiptV1,
            host.run_postgresql_kill_campaign,
            fault=inject,
        )
        final_reobservation = _record_phase(
            host,
            "08-final-reobservation.json",
            QualificationCampaignReobservationEvidenceV1,
            lambda: host.reobserve(manifest),
            fault=inject,
        )
        completed_at = monitored_now()
        evidence = (
            activation.receipt_sha256,
            manifest.manifest_sha256,
            terminal.node_kill.receipt_sha256,
            terminal.receipt_sha256,
            node_peer.receipt_sha256,
            outbox_peer.receipt_sha256,
            root_campaign.quota_campaign.receipt_sha256,
            canonical_sha256(root_campaign.service_kills),
            postgresql_kill.receipt_sha256,
            final_reobservation.evidence_sha256,
        )
        receipt = QualificationTargetCampaignReceiptV1(
            request_id=frozen.request_id,
            request_sha256=frozen.request_sha256,
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            activation=activation,
            installed_manifest=manifest,
            peer_probes=(node_peer, outbox_peer),
            quota_campaign=root_campaign.quota_campaign,
            root_service_kills=root_campaign.service_kills,
            postgresql_kill=postgresql_kill,
            terminal_campaign=terminal,
            final_reobservation=final_reobservation,
            scenario_evidence_sha256s=evidence,
            completed_at=completed_at,
        )
        verify_qualification_target_campaign_receipt(frozen, receipt)
        host.write_record_once("receipt.json", canonical_json_bytes(receipt))
        inject("after_receipt")
        host.revalidate_completed(receipt)
        return receipt


class _RecordedQualificationDeploymentObserver:
    """One-shot adapter retaining the exact signed bytes consumed by preflight derivation."""

    def __init__(self, signed: SignedQualificationLinuxDeploymentObservation) -> None:
        self._signed = signed

    def observe(
        self,
        *,
        spec: QualificationDeploymentSpecV1,
        rendered_units: tuple[RenderedSystemdUnit, ...],
        postgresql_acl: bytes,
    ) -> SignedQualificationLinuxDeploymentObservation:
        del spec, rendered_units, postgresql_acl
        return self._signed


class LinuxQualificationTargetCampaignHost:
    """Concrete root/systemd/PostgreSQL adapter for the frozen campaign state machine."""

    def __init__(self, request: QualificationTargetCampaignRequestV1) -> None:
        self.request = QualificationTargetCampaignRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        config = self.request.observer_config
        self.spec = config.commissioning_request.installation_request.deployment_spec
        self._observer = LinuxQualificationDeploymentObserver(config)
        self._systemctl_pin = config.commissioning_request.installation_request.systemctl_executable
        self._root = Path(self.request.campaign_journal_root)
        self._lock_descriptor: int | None = None
        self._deadline = self.request.requested_at + timedelta(
            seconds=self.request.maximum_campaign_seconds
        )

    def assert_target(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
            raise QualificationCampaignError("target campaign requires Linux root:root")
        observed_at = datetime.now(timezone.utc)
        if not self.request.requested_at <= observed_at < self._deadline:
            raise QualificationCampaignError("target campaign is outside its frozen time window")
        try:
            pid_one = Path("/proc/1/comm").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise QualificationCampaignError("target PID 1 is unavailable") from exc
        if pid_one != "systemd" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise QualificationCampaignError("target is not real systemd/cgroup-v2 Linux")
        self._admin_database_url()

    @contextmanager
    def lock(self) -> Iterator[None]:
        parent = self._root.parent
        descriptor = -1
        try:
            parent_stat = parent.lstat()
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or parent.is_symlink()
                or parent_stat.st_uid != 0
                or parent_stat.st_gid != 0
                or stat.S_IMODE(parent_stat.st_mode) != 0o700
            ):
                raise QualificationCampaignError("campaign journal parent custody differs")
            try:
                self._root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            root_stat = self._root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or self._root.is_symlink()
                or root_stat.st_uid != 0
                or root_stat.st_gid != 0
                or stat.S_IMODE(root_stat.st_mode) != 0o700
            ):
                raise QualificationCampaignError("campaign journal root custody differs")
            parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            descriptor = os.open(
                self._root / ".campaign.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != 0
                or lock_stat.st_gid != 0
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise QualificationCampaignError("campaign lock custody differs")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except QualificationCampaignError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise QualificationCampaignError("campaign journal lock is unavailable") from exc
        self._lock_descriptor = descriptor
        try:
            yield
        finally:
            self._lock_descriptor = None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _require_locked(self) -> None:
        if self._lock_descriptor is None:
            raise QualificationCampaignError("campaign journal mutation is not locked")

    def _record_path(self, name: str) -> Path:
        if re.fullmatch(r"(?:[0-9]{2}-)?[a-z0-9-]+[.]json", name) is None:
            raise QualificationCampaignError("campaign record name is not canonical")
        return self._root / name

    @staticmethod
    def _read_file(
        path: Path,
        *,
        maximum: int = _MAX_JOURNAL_BYTES,
        allowed_links: frozenset[int] = frozenset({1}),
    ) -> tuple[bytes, os.stat_result]:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise QualificationCampaignError(f"campaign evidence is unavailable: {path}") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > maximum
                or before.st_nlink not in allowed_links
            ):
                raise QualificationCampaignError("campaign evidence custody differs")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > maximum:
                    raise QualificationCampaignError("campaign evidence is oversized")
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_gid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
            )

        if identity(before) != identity(after):
            raise QualificationCampaignError("campaign evidence changed while read")
        return bytes(payload), after

    def read_record(self, name: str) -> bytes | None:
        self._require_locked()
        path = self._record_path(name)
        try:
            payload, observed = self._read_file(
                path,
                allowed_links=frozenset({1, 2}),
            )
        except QualificationCampaignError:
            if not os.path.lexists(path):
                return None
            raise
        if observed.st_uid != 0 or observed.st_gid != 0 or stat.S_IMODE(observed.st_mode) != 0o400:
            raise QualificationCampaignError("campaign journal record custody differs")
        pending = path.with_name(f".{path.name}.pending")
        if observed.st_nlink == 2:
            pending_payload, pending_stat = self._read_file(
                pending,
                allowed_links=frozenset({2}),
            )
            if (
                pending_payload != payload
                or (pending_stat.st_dev, pending_stat.st_ino) != (observed.st_dev, observed.st_ino)
                or pending_stat.st_uid != 0
                or pending_stat.st_gid != 0
                or stat.S_IMODE(pending_stat.st_mode) != 0o400
            ):
                raise QualificationCampaignError("campaign journal linked crash residue differs")
            try:
                pending.unlink()
                self._fsync_directory(self._root)
            except OSError as exc:
                raise QualificationCampaignError(
                    "campaign journal linked crash residue could not be recovered"
                ) from exc
            payload, observed = self._read_file(path)
            if (
                observed.st_uid != 0
                or observed.st_gid != 0
                or stat.S_IMODE(observed.st_mode) != 0o400
            ):
                raise QualificationCampaignError("campaign journal recovery changed final custody")
        elif os.path.lexists(pending):
            raise QualificationCampaignError(
                "campaign journal contains an impossible pending record"
            )
        return payload

    def write_record_once(self, name: str, payload: bytes) -> None:
        self._require_locked()
        if not payload or len(payload) > _MAX_JOURNAL_BYTES:
            raise QualificationCampaignError("campaign journal payload is empty or oversized")
        path = self._record_path(name)
        existing = self.read_record(name)
        if existing is not None:
            if existing != payload:
                raise QualificationCampaignError("campaign journal exact retry differs")
            return
        pending = path.with_name(f".{path.name}.pending")
        if os.path.lexists(pending):
            pending_payload, pending_stat = self._read_file(pending)
            if (
                pending_stat.st_uid != 0
                or pending_stat.st_gid != 0
                or stat.S_IMODE(pending_stat.st_mode) not in {0o600, 0o400}
                or pending_stat.st_nlink != 1
            ):
                raise QualificationCampaignError("campaign pending record custody differs")
            if stat.S_IMODE(pending_stat.st_mode) == 0o600:
                pending.unlink()
                self._fsync_directory(self._root)
            elif pending_payload != payload:
                raise QualificationCampaignError("sealed campaign pending record differs")
        if not os.path.lexists(pending):
            descriptor = os.open(
                pending,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise QualificationCampaignError("campaign journal write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_directory(self._root)
        try:
            os.link(pending, path, follow_symlinks=False)
        except FileExistsError:
            existing = self.read_record(name)
            if existing != payload:
                raise QualificationCampaignError("campaign record appeared with other bytes")
        else:
            self._fsync_directory(self._root)
        pending.unlink()
        self._fsync_directory(self._root)
        final, observed = self._read_file(path)
        if (
            final != payload
            or observed.st_uid != 0
            or observed.st_gid != 0
            or stat.S_IMODE(observed.st_mode) != 0o400
        ):
            raise QualificationCampaignError("campaign record publication failed")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _systemctl(self, *arguments: str, allowed: tuple[int, ...] = (0,)) -> str:
        pin = self._systemctl_pin
        path = Path(pin.path)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != pin.expected_owner_uid
                or metadata.st_gid != pin.expected_owner_gid
                or stat.S_IMODE(metadata.st_mode) != pin.expected_mode
                or digest.hexdigest() != pin.reviewed_sha256
            ):
                raise QualificationCampaignError("systemctl executable differs from pin")
            completed = subprocess.run(
                (pin.path, *arguments),
                executable=f"/proc/self/fd/{descriptor}",
                pass_fds=(descriptor,),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                cwd="/",
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise QualificationCampaignError("pinned systemctl invocation failed") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        if (
            completed.returncode not in allowed
            or len(completed.stdout.encode()) > _MAX_CHILD_BYTES
            or len(completed.stderr.encode()) > _MAX_CHILD_BYTES
        ):
            raise QualificationCampaignError(f"systemctl {' '.join(arguments)} failed closed")
        return completed.stdout

    def _show(self, unit_name: str, *properties: str) -> dict[str, str]:
        output = self._systemctl(
            "show",
            unit_name,
            *(f"--property={item}" for item in properties),
        )
        values: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise QualificationCampaignError("systemd show output is ambiguous")
            values[key] = value
        if set(values) != set(properties):
            raise QualificationCampaignError("systemd show omitted campaign state")
        return values

    def _unit_names_in_start_order(self) -> tuple[str, ...]:
        return (
            self.spec.workspace_unit_name,
            self.spec.quota_unit_name,
            self.spec.watchdog_unit_name,
            self.spec.node_unit_name,
            self.spec.outbox_unit_name,
        )

    def activate_services(self) -> QualificationSystemdActivationReceiptV1:
        for unit in self._unit_names_in_start_order():
            self._systemctl("enable", unit)
            self._systemctl("start", unit)
        main_pids: list[tuple[str, int]] = []
        for unit in self._unit_names_in_start_order():
            state = self._show(
                unit,
                "LoadState",
                "ActiveState",
                "SubState",
                "UnitFileState",
                "MainPID",
                "NeedDaemonReload",
            )
            if (
                state["LoadState"] != "loaded"
                or state["ActiveState"] != "active"
                or state["UnitFileState"] != "enabled"
                or state["NeedDaemonReload"] != "no"
            ):
                raise QualificationCampaignError(f"systemd activation failed: {unit}")
            pid = int(state["MainPID"])
            if unit == self.spec.workspace_unit_name:
                if state["SubState"] != "exited" or pid != 0:
                    raise QualificationCampaignError("workspace one-shot did not remain active")
            elif state["SubState"] != "running" or pid <= 1:
                raise QualificationCampaignError(f"service did not remain running: {unit}")
            else:
                main_pids.append((unit, pid))
        return QualificationSystemdActivationReceiptV1(
            unit_names=tuple(sorted(self._unit_names_in_start_order())),
            main_pids=tuple(sorted(main_pids)),
            activated_at=datetime.now(timezone.utc),
        )

    def freeze_installed_manifest(self) -> QualificationInstalledDeploymentManifestV1:
        return freeze_installed_manifest(
            self.spec,
            self._observer,
            self.request.observer_config.observer_pin,
        )

    def _admin_database_url(self) -> str:
        value = os.environ.get(_ADMIN_DATABASE_URL_ENV, "")
        if (
            not value
            or hashlib.sha256(value.encode("utf-8")).hexdigest()
            != self.request.observer_config.admin_database_url_sha256
        ):
            raise QualificationCampaignError("admin database URL is absent or differs")
        try:
            parsed = make_url(value)
        except Exception as exc:
            raise QualificationCampaignError("admin database URL is invalid") from exc
        if (
            parsed.get_backend_name() != "postgresql"
            or parsed.database != self.spec.postgresql_database
        ):
            raise QualificationCampaignError("admin database URL targets another database")
        return value

    def _child_identity(
        self,
        *,
        uid: int,
        gid: int,
        supplementary_gids: tuple[int, ...],
        operation: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - exercised only by the opt-in Linux campaign
            os.close(read_fd)
            try:
                os.setgroups(list(supplementary_gids))
                os.setgid(gid)
                os.setuid(uid)
                if os.geteuid() != uid or os.getegid() != gid:
                    raise RuntimeError("identity transition did not take effect")
                result = {"ok": dict(operation())}
            except BaseException as exc:  # noqa: BLE001 - child must report and terminate
                result = {"error": f"{type(exc).__name__}:{exc}"}
            try:
                payload = canonical_json_bytes(result)
            except BaseException as exc:  # noqa: BLE001 - make child failure observable
                payload = json.dumps(
                    {"error": f"child-result:{type(exc).__name__}"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(write_fd, view)
                    if written <= 0:
                        break
                    view = view[written:]
            finally:
                try:
                    os.close(write_fd)
                finally:
                    os._exit(0)
        os.close(write_fd)
        payload = bytearray()
        monotonic_deadline = time.monotonic() + max(
            1.0,
            (self._deadline - datetime.now(timezone.utc)).total_seconds(),
        )
        try:
            try:
                while True:
                    remaining = monotonic_deadline - time.monotonic()
                    if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
                        raise QualificationCampaignError(
                            "identity probe exceeded campaign deadline"
                        )
                    chunk = os.read(read_fd, 64 * 1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > _MAX_CHILD_BYTES:
                        raise QualificationCampaignError("identity probe exceeded its byte bound")
            finally:
                os.close(read_fd)
        except BaseException:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            raise
        _child_pid, status = os.waitpid(pid, 0)
        if status != 0:
            raise QualificationCampaignError("identity probe process failed")
        try:
            result = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QualificationCampaignError("identity probe returned invalid JSON") from exc
        if (
            not isinstance(result, dict)
            or set(result) != {"ok"}
            or not isinstance(result["ok"], dict)
        ):
            reason = result.get("error") if isinstance(result, dict) else "invalid"
            raise QualificationCampaignError(f"identity probe failed closed: {reason}")
        return result["ok"]

    def probe_postgresql_peer(
        self,
        role_kind: Literal["node", "outbox"],
    ) -> QualificationPostgreSQLPeerProbeReceiptV1:
        if role_kind == "node":
            uid, gid = self.spec.node_uid, self.spec.node_gid
            role = self.spec.postgresql_allocator_role
            groups = (self.spec.docker_gid,)
        else:
            uid, gid = self.spec.outbox_uid, self.spec.outbox_gid
            role = self.spec.postgresql_outbox_role
            groups = ()
        database_url = qualification_postgresql_peer_database_url(self.spec, role_name=role)

        def operation() -> Mapping[str, object]:
            engine = create_engine(database_url, pool_pre_ping=True, future=True)
            try:
                with engine.connect() as connection, connection.begin():
                    row = connection.execute(
                        text(
                            "SELECT current_user, current_database(), "
                            "pg_catalog.clock_timestamp(), "
                            "(SELECT version_num FROM alembic_version)"
                        )
                    ).one()
                return {
                    "postgresql_role": row[0],
                    "database_name": row[1],
                    "database_clock": row[2].isoformat(),
                    "schema_revision": row[3],
                    "probed_at": datetime.now(timezone.utc).isoformat(),
                }
            finally:
                engine.dispose()

        result = self._child_identity(
            uid=uid,
            gid=gid,
            supplementary_gids=groups,
            operation=operation,
        )
        receipt = QualificationPostgreSQLPeerProbeReceiptV1(
            role_kind=role_kind,
            process_uid=uid,
            process_gid=gid,
            **result,
        )
        if (
            receipt.postgresql_role != role
            or receipt.database_name != self.spec.postgresql_database
            or receipt.schema_revision != self.spec.expected_schema_revision
        ):
            raise QualificationCampaignError("PostgreSQL peer probe rebound its exact role")
        return receipt

    def _kill_and_wait(self, unit_name: str) -> QualificationServiceKillReceiptV1:
        before = self._show(unit_name, "MainPID", "ActiveState", "SubState")
        try:
            pid_before = int(before["MainPID"])
        except ValueError as exc:
            raise QualificationCampaignError("systemd MainPID is invalid before kill") from exc
        if pid_before <= 1 or before["ActiveState"] != "active" or before["SubState"] != "running":
            raise QualificationCampaignError(f"service is not running before kill: {unit_name}")
        killed_at = datetime.now(timezone.utc)
        self._systemctl("kill", "--kill-who=main", "--signal=KILL", unit_name)
        while datetime.now(timezone.utc) < self._deadline:
            current = self._show(unit_name, "MainPID", "ActiveState", "SubState")
            try:
                pid_after = int(current["MainPID"])
            except ValueError:
                pid_after = 0
            if (
                pid_after > 1
                and pid_after != pid_before
                and current["ActiveState"] == "active"
                and current["SubState"] == "running"
            ):
                return QualificationServiceKillReceiptV1(
                    unit_name=unit_name,
                    pid_before=pid_before,
                    pid_after=pid_after,
                    killed_at=killed_at,
                    recovered_at=datetime.now(timezone.utc),
                )
            time.sleep(self.request.poll_milliseconds / 1000)
        raise QualificationCampaignError(f"service did not recover before deadline: {unit_name}")

    @staticmethod
    def _loop_source(output_root: str) -> str:
        try:
            lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise QualificationCampaignError("mountinfo is unavailable after quota probe") from exc
        sources: list[str] = []
        for line in lines:
            left, separator, right = line.partition(" - ")
            if not separator:
                raise QualificationCampaignError("mountinfo is malformed")
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 6 or len(right_fields) < 2:
                raise QualificationCampaignError("mountinfo entry is malformed")
            mountpoint = left_fields[4].replace("\\040", " ")
            if mountpoint == output_root:
                sources.append(right_fields[1])
        if len(sources) != 1 or re.fullmatch(r"/dev/loop[0-9]+", sources[0]) is None:
            raise QualificationCampaignError("quota probe is not mounted from one loop device")
        return sources[0]

    def _quota_child(
        self,
        expected: OutputQuotaProvisioningReceipt | None,
    ) -> Mapping[str, object]:
        config = self.request.observer_config.commissioning_request
        node = config.node_config
        quota = LoopbackOutputQuotaProvisionerClient(node.quota_deployment)
        watchdog = SystemdDeadlineWatchdogController(
            policy=node.oci_policy,
            deployment=node.watchdog_deployment,
        )
        request_hash = self.request.request_sha256
        execution_id = f"exe_{canonical_sha256({'campaign': request_hash, 'kind': 'quota'})[:32]}"
        attempt_id = (
            f"iat_{canonical_sha256({'campaign': request_hash, 'kind': 'quota-attempt'})[:32]}"
        )
        intent_sha256 = canonical_sha256(
            {
                "schema": "aletheia.qualification_quota_campaign_intent.v1",
                "request_sha256": request_hash,
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "quota_bytes": self.request.quota_probe_bytes,
            }
        )
        key = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
        attempt_root = Path(self.spec.output_workspace_root) / key
        output_root = attempt_root / "output"
        if expected is None:
            for path in (attempt_root, attempt_root / "input", output_root):
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                observed = path.lstat()
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or path.is_symlink()
                    or observed.st_uid != self.spec.node_uid
                    or observed.st_gid != self.spec.node_gid
                    or stat.S_IMODE(observed.st_mode) != 0o700
                ):
                    raise QualificationCampaignError("quota campaign workspace custody differs")
        receipt = quota.ensure_output_quota(
            node_manifest_sha256=self.spec.node_manifest_sha256,
            node_id=self.spec.node_id,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(),
            execution_id=execution_id,
            attempt_id=attempt_id,
            intent_sha256=intent_sha256,
            output_root=output_root,
            output_quota_bytes=self.request.quota_probe_bytes,
            expected_receipt=expected,
        )
        return {
            "quota_health_sha256": quota.verify_service_health(),
            "watchdog_health_sha256": watchdog.verify_service_health(),
            "provisioning_receipt": receipt.model_dump(mode="json"),
        }

    def _run_quota_child(
        self,
        expected: OutputQuotaProvisioningReceipt | None,
    ) -> Mapping[str, object]:
        return self._child_identity(
            uid=self.spec.node_uid,
            gid=self.spec.node_gid,
            supplementary_gids=(self.spec.docker_gid,),
            operation=lambda: self._quota_child(expected),
        )

    def run_root_service_campaign(self) -> QualificationRootServiceCampaignEvidenceV1:
        existing = _load_record(
            self,
            "06-root-result.json",
            QualificationRootServiceCampaignEvidenceV1,
        )
        if existing is not None:
            self._revalidate_root_service_campaign(existing)
            return existing
        before = self._run_quota_child(None)
        try:
            receipt = OutputQuotaProvisioningReceipt.model_validate(before["provisioning_receipt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationCampaignError("quota child omitted its typed receipt") from exc
        loop_device = self._loop_source(receipt.output_root)
        kills = tuple(
            sorted(
                (
                    self._kill_and_wait(self.spec.quota_unit_name),
                    self._kill_and_wait(self.spec.watchdog_unit_name),
                ),
                key=lambda item: item.unit_name,
            )
        )
        after = self._run_quota_child(receipt)
        try:
            replayed = OutputQuotaProvisioningReceipt.model_validate(after["provisioning_receipt"])
            quota_campaign = QualificationQuotaCampaignReceiptV1(
                quota_health_before_sha256=before["quota_health_sha256"],
                watchdog_health_before_sha256=before["watchdog_health_sha256"],
                provisioning_receipt=receipt,
                loop_device=loop_device,
                replayed_provisioning_receipt_sha256=replayed.provisioning_receipt_sha256,
                quota_health_after_sha256=after["quota_health_sha256"],
                watchdog_health_after_sha256=after["watchdog_health_sha256"],
                completed_at=datetime.now(timezone.utc),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationCampaignError("root-service replay evidence is invalid") from exc
        if replayed != receipt:
            raise QualificationCampaignError("quota service replay changed its exact receipt")
        evidence = QualificationRootServiceCampaignEvidenceV1(
            quota_campaign=quota_campaign,
            service_kills=kills,
        )
        self.write_record_once("06-root-result.json", canonical_json_bytes(evidence))
        self._revalidate_root_service_campaign(evidence)
        return evidence

    def _revalidate_root_service_campaign(
        self,
        evidence: QualificationRootServiceCampaignEvidenceV1,
    ) -> None:
        expected_units = tuple(sorted((self.spec.quota_unit_name, self.spec.watchdog_unit_name)))
        if tuple(item.unit_name for item in evidence.service_kills) != expected_units:
            raise QualificationCampaignError("root-service campaign rebound its exact units")
        result = self._run_quota_child(evidence.quota_campaign.provisioning_receipt)
        try:
            replayed = OutputQuotaProvisioningReceipt.model_validate(result["provisioning_receipt"])
            quota_health_sha256 = result["quota_health_sha256"]
            watchdog_health_sha256 = result["watchdog_health_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationCampaignError(
                "root-service campaign could not be freshly revalidated"
            ) from exc
        if (
            replayed != evidence.quota_campaign.provisioning_receipt
            or self._loop_source(replayed.output_root) != evidence.quota_campaign.loop_device
            or not isinstance(quota_health_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, quota_health_sha256) is None
            or not isinstance(watchdog_health_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, watchdog_health_sha256) is None
        ):
            raise QualificationCampaignError("root-service live replay differs from its receipt")

    @staticmethod
    def _write_frame(descriptor: int, value: Mapping[str, object]) -> None:
        payload = canonical_json_bytes(dict(value))
        if not payload or len(payload) > _MAX_CHILD_BYTES:
            raise QualificationCampaignError("campaign child frame is empty or oversized")
        frame = len(payload).to_bytes(4, "big") + payload
        view = memoryview(frame)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise QualificationCampaignError("campaign child frame made no progress")
            view = view[written:]

    @staticmethod
    def _read_frame(descriptor: int, *, timeout_seconds: float) -> Mapping[str, object]:
        monotonic_deadline = time.monotonic() + max(timeout_seconds, 0.001)

        def read_exact(length: int) -> bytes:
            payload = bytearray()
            while len(payload) < length:
                remaining = monotonic_deadline - time.monotonic()
                if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                    raise QualificationCampaignError("campaign child frame timed out")
                chunk = os.read(descriptor, length - len(payload))
                if not chunk:
                    raise QualificationCampaignError("campaign child frame ended early")
                payload.extend(chunk)
            return bytes(payload)

        length = int.from_bytes(read_exact(4), "big")
        if length < 1 or length > _MAX_CHILD_BYTES:
            raise QualificationCampaignError("campaign child frame length is invalid")
        try:
            value = json.loads(read_exact(length))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QualificationCampaignError("campaign child frame is invalid JSON") from exc
        if not isinstance(value, dict):
            raise QualificationCampaignError("campaign child frame is not an object")
        return value

    def run_postgresql_kill_campaign(self) -> QualificationPostgreSQLKillReceiptV1:
        """Terminate a real node-role transaction and prove rollback plus peer reconnect."""

        role = self.spec.postgresql_allocator_role
        peer_url = qualification_postgresql_peer_database_url(self.spec, role_name=role)
        lock_key = int(
            canonical_sha256({"campaign": self.request.request_sha256, "kind": "postgresql-kill"})[
                :15
            ],
            16,
        )
        child_read, parent_write = os.pipe()
        parent_read, child_write = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - opt-in Linux/PostgreSQL campaign only
            os.close(parent_write)
            os.close(parent_read)
            engine = None
            connection = None
            transaction = None
            try:
                os.setgroups([self.spec.docker_gid])
                os.setgid(self.spec.node_gid)
                os.setuid(self.spec.node_uid)
                if os.geteuid() != self.spec.node_uid or os.getegid() != self.spec.node_gid:
                    raise RuntimeError("node identity transition did not take effect")
                engine = create_engine(peer_url, pool_pre_ping=True, future=True)
                connection = engine.connect()
                transaction = connection.begin()
                row = connection.execute(
                    text(
                        "SELECT current_user, current_database(), pg_backend_pid(), "
                        "pg_advisory_xact_lock(:lock_key), pg_catalog.clock_timestamp()"
                    ),
                    {"lock_key": lock_key},
                ).one()
                self._write_frame(
                    child_write,
                    {
                        "postgresql_role": row[0],
                        "database_name": row[1],
                        "backend_pid": row[2],
                        "locked_at": row[4].isoformat(),
                    },
                )
                if os.read(child_read, 1) != b"1":
                    raise RuntimeError("PostgreSQL termination acknowledgement is absent")
                connection_lost = False
                try:
                    connection.execute(text("SELECT 1")).scalar_one()
                except SQLAlchemyError:
                    connection_lost = True
                if not connection_lost:
                    raise RuntimeError("terminated PostgreSQL transaction connection remained live")
                try:
                    transaction.rollback()
                except SQLAlchemyError:
                    pass
                connection.close()
                connection = None
                engine.dispose()
                engine = create_engine(peer_url, pool_pre_ping=True, future=True)
                with engine.connect() as recovered, recovered.begin():
                    recovered_row = recovered.execute(
                        text(
                            "SELECT current_user, current_database(), "
                            "pg_try_advisory_lock(:lock_key), pg_catalog.clock_timestamp()"
                        ),
                        {"lock_key": lock_key},
                    ).one()
                    if recovered_row[2] is not True:
                        raise RuntimeError("terminated transaction retained its advisory lock")
                    unlocked = recovered.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": lock_key},
                    ).scalar_one()
                    if unlocked is not True:
                        raise RuntimeError("recovery connection could not release advisory lock")
                self._write_frame(
                    child_write,
                    {
                        "ok": True,
                        "postgresql_role": recovered_row[0],
                        "database_name": recovered_row[1],
                        "reconnected_at": recovered_row[3].isoformat(),
                        "transaction_connection_lost": True,
                        "transaction_lock_released": True,
                    },
                )
            except BaseException as exc:  # noqa: BLE001 - child must report and exit
                try:
                    self._write_frame(child_write, {"error": f"{type(exc).__name__}:{exc}"})
                except BaseException:  # noqa: BLE001 - no recovery remains in the child
                    pass
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except BaseException:  # noqa: BLE001
                        pass
                if engine is not None:
                    try:
                        engine.dispose()
                    except BaseException:  # noqa: BLE001
                        pass
                try:
                    os.close(child_read)
                except OSError:
                    pass
                try:
                    os.close(child_write)
                except OSError:
                    pass
                os._exit(0)

        os.close(child_read)
        os.close(child_write)
        try:
            remaining = max(
                0.001,
                (self._deadline - datetime.now(timezone.utc)).total_seconds(),
            )
            ready = self._read_frame(parent_read, timeout_seconds=remaining)
            if (
                set(ready) != {"postgresql_role", "database_name", "backend_pid", "locked_at"}
                or ready["postgresql_role"] != role
                or ready["database_name"] != self.spec.postgresql_database
                or isinstance(ready["backend_pid"], bool)
                or not isinstance(ready["backend_pid"], int)
                or ready["backend_pid"] <= 1
            ):
                raise QualificationCampaignError("PostgreSQL kill child rebound its peer scope")
            try:
                transaction_started_at = datetime.fromisoformat(str(ready["locked_at"]))
            except ValueError as exc:
                raise QualificationCampaignError(
                    "PostgreSQL kill child returned an invalid transaction time"
                ) from exc
            if transaction_started_at.utcoffset() != timedelta(0):
                raise QualificationCampaignError("PostgreSQL kill transaction time is not UTC")
            engine = create_engine(self._admin_database_url(), pool_pre_ping=True, future=True)
            try:
                with engine.connect() as connection, connection.begin():
                    terminated = connection.execute(
                        text("SELECT pg_terminate_backend(:backend_pid)"),
                        {"backend_pid": ready["backend_pid"]},
                    ).scalar_one()
                    terminated_at = connection.execute(
                        text("SELECT pg_catalog.clock_timestamp()")
                    ).scalar_one()
            finally:
                engine.dispose()
            if terminated is not True:
                raise QualificationCampaignError("PostgreSQL refused to terminate peer backend")
            os.write(parent_write, b"1")
            remaining = max(
                0.001,
                (self._deadline - datetime.now(timezone.utc)).total_seconds(),
            )
            result = self._read_frame(parent_read, timeout_seconds=remaining)
        except BaseException:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        finally:
            os.close(parent_write)
            os.close(parent_read)
            _child_pid, status = os.waitpid(pid, 0)
        if status != 0 or result.get("ok") is not True:
            raise QualificationCampaignError(
                f"PostgreSQL kill child failed closed: {result.get('error', 'invalid')}"
            )
        try:
            receipt = QualificationPostgreSQLKillReceiptV1(
                postgresql_role=result["postgresql_role"],
                backend_pid=ready["backend_pid"],
                advisory_lock_key=lock_key,
                transaction_started_at=transaction_started_at,
                terminated_at=terminated_at,
                reconnected_at=result["reconnected_at"],
                transaction_connection_lost=result["transaction_connection_lost"],
                transaction_lock_released=result["transaction_lock_released"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationCampaignError("PostgreSQL kill evidence is invalid") from exc
        if (
            receipt.postgresql_role != role
            or result["database_name"] != self.spec.postgresql_database
        ):
            raise QualificationCampaignError("PostgreSQL recovery rebound its exact peer role")
        return receipt

    def _attempt_projection(self) -> Mapping[str, object] | None:
        registration = self.request.execution.registration_receipt
        engine = create_engine(self._admin_database_url(), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection, connection.begin():
                row = (
                    connection.execute(
                        text(_ATTEMPT_PROJECTION_SQL),
                        {
                            "execution_id": registration.execution_id,
                            "attempt_id": registration.attempt_id,
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                return dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise QualificationCampaignError("campaign attempt projection is unavailable") from exc
        finally:
            engine.dispose()

    def _verify_attempt_projection(self, row: Mapping[str, object]) -> None:
        registration = self.request.execution.registration_receipt
        try:
            authorization = ScientificExecutionAuthorization.model_validate(
                row["authorization_json"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationCampaignError(
                "campaign scientific execution authorization is invalid"
            ) from exc
        message = authorization.message
        intent = message.qualification_bundle.intent
        if (
            authorization.authorization_sha256 != registration.authorization_sha256
            or row["authorization_json"] != authorization.model_dump(mode="json", exclude_none=True)
            or intent.execution_id != registration.execution_id
            or intent.infrastructure_attempt.infrastructure_attempt_id != registration.attempt_id
            or intent.intent_sha256 != row["intent_sha256"]
            or message.scientific_slot_id != registration.scientific_slot_id
            or message.action_protocol_binding.action.object_sha256 != registration.action_sha256
            or message.qualification_bundle.bundle_sha256
            != registration.qualification_bundle_sha256
            or message.qualification_grant.grant_sha256 != registration.qualification_grant_sha256
            or row["authorized_at"] != message.authorized_at
            or row["expires_at"] != message.expires_at
            or row["observation_admission_deadline"] != message.observation_admission_deadline
            or row["admission_sha256"] != registration.qualification_admission_sha256
            or row["grant_sha256"] != registration.qualification_grant_sha256
            or row["bundle_sha256"] != registration.qualification_bundle_sha256
            or row["node_id"] != self.spec.node_id
            or row["node_manifest_sha256"] != self.spec.node_manifest_sha256
            or row["resource_lease_sha256"] != registration.resource_reservation_sha256
            or row["authorization_sha256"] != registration.authorization_sha256
            or row["quest_id"] != registration.quest_id
            or row["scientific_slot_id"] != registration.scientific_slot_id
            or row["action_sha256"] != registration.action_sha256
            or row["qualification_bundle_sha256"] != registration.qualification_bundle_sha256
            or row["qualification_grant_sha256"] != registration.qualification_grant_sha256
            or row["registered_at"] != registration.registered_at
        ):
            raise QualificationCampaignError("campaign attempt rebound its atomic registration")
        hard_deadline = row["hard_deadline"]
        if not isinstance(hard_deadline, datetime) or hard_deadline.utcoffset() != timedelta(0):
            raise QualificationCampaignError("campaign attempt hard deadline is invalid")

    def _wait_for_attempt_running(self) -> Mapping[str, object]:
        while datetime.now(timezone.utc) < self._deadline:
            row = self._attempt_projection()
            if row is not None:
                self._verify_attempt_projection(row)
                if row["status"] == "running":
                    if row["hard_deadline"] <= datetime.now(timezone.utc):
                        raise QualificationCampaignError("campaign attempt hard deadline elapsed")
                    return row
                if row["status"] in {"succeeded", "failed", "cancelled"}:
                    raise QualificationCampaignError(
                        "campaign attempt terminated before the node-kill checkpoint"
                    )
            time.sleep(self.request.poll_milliseconds / 1000)
        raise QualificationCampaignError("campaign attempt did not reach running before deadline")

    def _qualification_outbox_envelope(self) -> QualificationTerminalSpoolEnvelopeV1 | None:
        registration = self.request.execution.registration_receipt
        engine = create_engine(self._admin_database_url(), pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection, connection.begin():
                rows = tuple(
                    connection.execute(
                        text(
                            """
                            SELECT outbox_id, terminal_authority_kind, terminal_authority_sha256,
                                   accepted_terminal_submission_sha256,
                                   terminal_deadline_expiration_sha256, execution_id, attempt_id,
                                   topic, delivery_key, payload_sha256, payload_json, created_at
                              FROM execution_qualification_terminal_outbox
                             WHERE execution_id = :execution_id AND attempt_id = :attempt_id
                             ORDER BY created_at, outbox_id
                            """
                        ),
                        {
                            "execution_id": registration.execution_id,
                            "attempt_id": registration.attempt_id,
                        },
                    ).mappings()
                )
        except SQLAlchemyError as exc:
            raise QualificationCampaignError(
                "qualification terminal outbox is unavailable"
            ) from exc
        finally:
            engine.dispose()
        if len(rows) > 1:
            raise QualificationCampaignError("qualification terminal outbox is not exactly once")
        if not rows:
            return None
        try:
            envelope = qualification_terminal_spool_envelope_from_row(rows[0])
        except Exception as exc:
            raise QualificationCampaignError(
                "qualification terminal outbox row is invalid"
            ) from exc
        if (
            envelope.execution_id != registration.execution_id
            or envelope.attempt_id != registration.attempt_id
        ):
            raise QualificationCampaignError("qualification terminal outbox rebound its attempt")
        return envelope

    def _wait_for_terminal_envelope(self) -> QualificationTerminalSpoolEnvelopeV1:
        while datetime.now(timezone.utc) < self._deadline:
            envelope = self._qualification_outbox_envelope()
            if envelope is not None:
                if (
                    envelope.terminal_authority_kind != "accepted_terminal_submission"
                    or getattr(envelope.payload, "disposition", None)
                    != self.request.execution.expected_terminal_disposition
                ):
                    raise QualificationCampaignError(
                        "campaign execution produced another terminal disposition"
                    )
                row = self._attempt_projection()
                if row is None:
                    raise QualificationCampaignError("terminal attempt projection disappeared")
                self._verify_attempt_projection(row)
                if (
                    row["status"] != self.request.execution.terminal_status
                    or row["accepted_terminal_submission_sha256"]
                    != envelope.terminal_authority_sha256
                    or row["terminal_deadline_expiration_sha256"] is not None
                ):
                    raise QualificationCampaignError("terminal attempt authority differs")
                return envelope
            time.sleep(self.request.poll_milliseconds / 1000)
        raise QualificationCampaignError("campaign terminal outbox did not appear before deadline")

    def _spool_inventory(self) -> tuple[str, ...]:
        root = Path(self.spec.outbox_spool_root)
        try:
            entries = tuple(os.scandir(root))
        except OSError as exc:
            raise QualificationCampaignError("qualification outbox spool is unavailable") from exc
        names: list[str] = []
        for entry in entries:
            if entry.name == ".service.lock":
                continue
            if re.fullmatch(r"(?:qto|xob)_[0-9a-f]{64}[.]json", entry.name) is None:
                raise QualificationCampaignError("qualification outbox spool has foreign state")
            if not entry.is_file(follow_symlinks=False):
                raise QualificationCampaignError("qualification outbox spool entry is not regular")
            names.append(entry.name)
        return tuple(sorted(names))

    def _quiesce_outbox(self) -> QualificationOutboxQuiescenceReceiptV1:
        if self._qualification_outbox_envelope() is not None:
            raise QualificationCampaignError("selected terminal existed before outbox quiescence")
        state = self._show(self.spec.outbox_unit_name, "MainPID", "ActiveState", "SubState")
        try:
            prior_pid = int(state["MainPID"])
        except ValueError as exc:
            raise QualificationCampaignError("outbox MainPID is invalid") from exc
        if prior_pid <= 1 or state["ActiveState"] != "active" or state["SubState"] != "running":
            raise QualificationCampaignError("outbox is not running before quiescence")
        baseline = self._spool_inventory()
        self._systemctl("stop", self.spec.outbox_unit_name)
        stopped = self._show(self.spec.outbox_unit_name, "MainPID", "ActiveState", "SubState")
        if (
            stopped["MainPID"] != "0"
            or stopped["ActiveState"] != "inactive"
            or stopped["SubState"] != "dead"
        ):
            raise QualificationCampaignError("outbox did not become inactive")
        return QualificationOutboxQuiescenceReceiptV1(
            unit_name=self.spec.outbox_unit_name,
            prior_pid=prior_pid,
            baseline_spool_filenames=baseline,
            stopped_at=datetime.now(timezone.utc),
        )

    def _start_and_wait(self, unit_name: str) -> None:
        self._systemctl("start", unit_name)
        while datetime.now(timezone.utc) < self._deadline:
            state = self._show(unit_name, "MainPID", "ActiveState", "SubState")
            try:
                pid = int(state["MainPID"])
            except ValueError:
                pid = 0
            if pid > 1 and state["ActiveState"] == "active" and state["SubState"] == "running":
                return
            time.sleep(self.request.poll_milliseconds / 1000)
        raise QualificationCampaignError(f"service did not start before deadline: {unit_name}")

    def _observe_spool(
        self,
        envelope: QualificationTerminalSpoolEnvelopeV1,
    ) -> QualificationSpoolFileObservationV1 | None:
        path = Path(self.spec.outbox_spool_root) / envelope.filename
        if not os.path.lexists(path):
            return None
        payload, observed = self._read_file(path)
        try:
            parsed = QualificationTerminalSpoolEnvelopeV1.model_validate_json(payload)
        except ValueError as exc:
            raise QualificationCampaignError("terminal spool envelope is invalid") from exc
        if parsed != envelope or payload != canonical_json_bytes(envelope):
            raise QualificationCampaignError("terminal spool envelope differs from database")
        result = QualificationSpoolFileObservationV1(
            path=str(path),
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_length=len(payload),
            device=observed.st_dev,
            inode=observed.st_ino,
            owner_uid=observed.st_uid,
            owner_gid=observed.st_gid,
            mode=stat.S_IMODE(observed.st_mode),
            link_count=observed.st_nlink,
        )
        if result.owner_uid != self.spec.outbox_uid or result.owner_gid != self.spec.outbox_gid:
            raise QualificationCampaignError("terminal spool ownership differs")
        return result

    def _wait_for_spool(
        self,
        envelope: QualificationTerminalSpoolEnvelopeV1,
    ) -> QualificationSpoolFileObservationV1:
        while datetime.now(timezone.utc) < self._deadline:
            observed = self._observe_spool(envelope)
            if observed is not None:
                return observed
            time.sleep(self.request.poll_milliseconds / 1000)
        raise QualificationCampaignError("terminal spool file did not appear before deadline")

    def run_terminal_campaign(self) -> QualificationTerminalCampaignReceiptV1:
        existing = _load_record(
            self,
            "03-terminal-result.json",
            QualificationTerminalCampaignReceiptV1,
        )
        if existing is not None:
            self._revalidate_terminal(existing)
            return existing

        quiescence = _load_record(
            self,
            "03-outbox-quiescence.json",
            QualificationOutboxQuiescenceReceiptV1,
        )
        if quiescence is None:
            self._wait_for_attempt_running()
            quiescence = self._quiesce_outbox()
            self.write_record_once(
                "03-outbox-quiescence.json",
                canonical_json_bytes(quiescence),
            )

        node_kill = _load_record(
            self,
            "03-node-kill.json",
            QualificationServiceKillReceiptV1,
        )
        if node_kill is None:
            if self._qualification_outbox_envelope() is not None:
                raise QualificationCampaignError("terminal appeared without recorded node kill")
            node_kill = self._kill_and_wait(self.spec.node_unit_name)
            self.write_record_once("03-node-kill.json", canonical_json_bytes(node_kill))

        envelope = self._wait_for_terminal_envelope()
        if envelope.source_created_at < max(quiescence.stopped_at, node_kill.killed_at):
            raise QualificationCampaignError("terminal outbox predates its destructive checkpoints")
        if envelope.filename in quiescence.baseline_spool_filenames:
            raise QualificationCampaignError("terminal spool filename existed before this attempt")
        spool_before_start = self._observe_spool(envelope)
        if spool_before_start is not None:
            raise QualificationCampaignError("terminal was spooled while outbox was quiescent")

        self._start_and_wait(self.spec.outbox_unit_name)
        first_spool = self._wait_for_spool(envelope)
        outbox_kill = _load_record(
            self,
            "03-outbox-kill.json",
            QualificationServiceKillReceiptV1,
        )
        if outbox_kill is None:
            outbox_kill = self._kill_and_wait(self.spec.outbox_unit_name)
            self.write_record_once("03-outbox-kill.json", canonical_json_bytes(outbox_kill))
        replayed_spool = self._wait_for_spool(envelope)
        if replayed_spool != first_spool:
            raise QualificationCampaignError("terminal spool changed across outbox recovery")

        registration = self.request.execution.registration_receipt
        receipt = QualificationTerminalCampaignReceiptV1(
            execution_id=registration.execution_id,
            attempt_id=registration.attempt_id,
            outbox_quiescence=quiescence,
            node_kill=node_kill,
            outbox_kill=outbox_kill,
            terminal_envelope=envelope,
            spool_file=replayed_spool,
            completed_at=datetime.now(timezone.utc),
        )
        self.write_record_once("03-terminal-result.json", canonical_json_bytes(receipt))
        self._revalidate_terminal(receipt)
        return receipt

    def reobserve(
        self,
        manifest: QualificationInstalledDeploymentManifestV1,
    ) -> QualificationCampaignReobservationEvidenceV1:
        signed = self._observer.observe(
            spec=self.spec,
            rendered_units=render_systemd_units(self.spec),
            postgresql_acl=render_postgresql_acl(self.spec),
        )
        preflight = verify_installed_manifest(
            manifest,
            _RecordedQualificationDeploymentObserver(signed),
            self.request.observer_config.observer_pin,
        )
        if not preflight.ready_for_opt_in_campaign or preflight.blockers:
            raise QualificationCampaignError(
                f"post-campaign deployment observation failed: {preflight.blockers}"
            )
        return QualificationCampaignReobservationEvidenceV1(
            signed_observation=signed,
            preflight=preflight,
        )

    def _revalidate_terminal(self, receipt: QualificationTerminalCampaignReceiptV1) -> None:
        registration = self.request.execution.registration_receipt
        if (
            receipt.execution_id != registration.execution_id
            or receipt.attempt_id != registration.attempt_id
            or receipt.node_kill.unit_name != self.spec.node_unit_name
            or receipt.outbox_kill.unit_name != self.spec.outbox_unit_name
        ):
            raise QualificationCampaignError("terminal receipt rebound campaign identity")
        envelope = self._qualification_outbox_envelope()
        if envelope is None or envelope != receipt.terminal_envelope:
            raise QualificationCampaignError("terminal database envelope changed or disappeared")
        row = self._attempt_projection()
        if row is None:
            raise QualificationCampaignError("terminal attempt disappeared")
        self._verify_attempt_projection(row)
        if (
            row["status"] != self.request.execution.terminal_status
            or row["accepted_terminal_submission_sha256"] != envelope.terminal_authority_sha256
            or row["terminal_deadline_expiration_sha256"] is not None
        ):
            raise QualificationCampaignError("terminal attempt no longer has exact success state")
        spool = self._observe_spool(envelope)
        if spool is None or spool != receipt.spool_file:
            raise QualificationCampaignError("terminal spool changed or disappeared")

    def revalidate_completed(self, receipt: QualificationTargetCampaignReceiptV1) -> None:
        request = self.request
        verify_qualification_target_campaign_receipt(request, receipt)
        for unit in self._unit_names_in_start_order():
            state = self._show(unit, "LoadState", "ActiveState", "SubState", "UnitFileState")
            expected_substate = "exited" if unit == self.spec.workspace_unit_name else "running"
            if (
                state["LoadState"] != "loaded"
                or state["ActiveState"] != "active"
                or state["SubState"] != expected_substate
                or state["UnitFileState"] != "enabled"
            ):
                raise QualificationCampaignError(f"completed campaign service drifted: {unit}")
        self._revalidate_terminal(receipt.terminal_campaign)
        current = self.reobserve(receipt.installed_manifest)
        if (
            current.preflight.installed_manifest_sha256
            != receipt.installed_manifest.manifest_sha256
            or not current.preflight.ready_for_opt_in_campaign
            or current.preflight.blockers
        ):
            raise QualificationCampaignError("completed campaign no longer passes observation")


def load_qualification_target_campaign_request(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> QualificationTargetCampaignRequestV1:
    """Load one root-custodied canonical campaign request by out-of-band digest."""

    source = Path(path)
    if (
        not source.is_absolute()
        or str(source) != os.path.normpath(str(path))
        or source == Path("/")
    ):
        raise QualificationCampaignError("campaign request path is not canonical and absolute")
    payload, observed = LinuxQualificationTargetCampaignHost._read_file(source)
    if (
        hashlib.sha256(payload).hexdigest() != expected_file_sha256
        or observed.st_uid != 0
        or observed.st_gid != 0
        or stat.S_IMODE(observed.st_mode) != 0o400
    ):
        raise QualificationCampaignError("campaign request file digest or custody differs")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload, object_pairs_hook=unique_object)
        request = QualificationTargetCampaignRequestV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationCampaignError("campaign request is invalid") from exc
    if payload != canonical_json_bytes(request):
        raise QualificationCampaignError("campaign request is not canonical JSON")
    return request


def _emit(value: ExecutionModel) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")


def run_qualification_target_campaign_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge")
    arguments = parser.parse_args(argv)
    try:
        request = load_qualification_target_campaign_request(
            arguments.request,
            expected_file_sha256=arguments.request_sha256,
        )
        if not arguments.apply:
            _emit(build_qualification_target_campaign_plan(request))
            return 0
        if arguments.acknowledge != request.opt_in_confirmation:
            raise QualificationCampaignError(
                "--apply requires --acknowledge RUN_QUALIFICATION_TARGET_CAMPAIGN"
            )
        receipt = run_qualification_target_campaign(
            request,
            LinuxQualificationTargetCampaignHost(request),
        )
    except QualificationCampaignError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _emit(receipt)
    return 0


__all__ = [
    "LinuxQualificationTargetCampaignHost",
    "QualificationCampaignReobservationEvidenceV1",
    "QualificationCampaignError",
    "QualificationCampaignExecutionExpectationV1",
    "QualificationOutboxQuiescenceReceiptV1",
    "QualificationPostgreSQLKillReceiptV1",
    "QualificationPostgreSQLPeerProbeReceiptV1",
    "QualificationQuotaCampaignReceiptV1",
    "QualificationRootServiceCampaignEvidenceV1",
    "QualificationServiceKillReceiptV1",
    "QualificationSpoolFileObservationV1",
    "QualificationSystemdActivationReceiptV1",
    "QualificationTargetCampaignHostPort",
    "QualificationTargetCampaignPlanV1",
    "QualificationTargetCampaignReceiptV1",
    "QualificationTargetCampaignRequestV1",
    "QualificationTerminalCampaignReceiptV1",
    "build_qualification_target_campaign_plan",
    "load_qualification_target_campaign_request",
    "run_qualification_target_campaign",
    "run_qualification_target_campaign_cli",
    "verify_qualification_target_campaign_receipt",
]


if __name__ == "__main__":  # pragma: no cover - exercised through checked-in wrapper
    raise SystemExit(run_qualification_target_campaign_cli())
