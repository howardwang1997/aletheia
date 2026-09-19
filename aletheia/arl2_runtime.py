"""Guarded ARL-2 question-campaign driver: orchestrate, courier signatures, replay.

This module is the D2 orchestrator, never a monolith.  It commits the three
pre-signed quest-activation commands (charter, problem, question - the kernel
requires an admitted problem before any question admission), launches the
controller, wakes the
deployed role entrypoints, carries materialized proposals to the two
kernel-command signing services, polls for the terminal STOP_COMMITTED event,
and replays the assembled campaign bundle in-process.  Every service that
signs, judges, or mutates a store stays behind its own deployment manifest;
the driver holds no private signing key and never authors a kernel command.

STOP discipline is structural: the driver only commits commands the signing
services returned.  A STOP reaches the stream solely when a STOP_REQUIRED
continuation receipt forced the followup proposal, and the transition service
refuses every other stop.  There is no driver-side convenience stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.db import expected_schema_revision, session_factory
from aletheia.research_controller.action_proposals import SubmittedActionProposal
from aletheia.research_controller.contracts import (
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    ControllerWorkerRPCTransport,
)
from aletheia.research_controller.campaign_replay import StagedRoundRowsV1
from aletheia.research_controller.kernel_authority import (
    continuation_receipt_evidence_ref,
)
from aletheia.research_controller.launch import ResearchControllerLauncher
from aletheia.research_controller.persistence import PostgreSQLControllerLaunchAdapter
from aletheia.research_controller.protocol_compilation_step import (
    RoundSplitBindingPolicyV1,
)
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
)
from aletheia.research_kernel.policy import ResearchAuthorizationTrustRootV1
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    EventType,
    KernelModel,
    ObservationIncorporatedPayload,
    RefineCommittedPayload,
    RefineDirective,
    ResearchEvent,
    ResearchQuestionVersion,
    StopCommittedPayload,
    StopDirective,
    TransitionDecision,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore
from aletheia.schema_migrations import require_schema_exact

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_PINNED_FILE_BYTES = 64 * 1024 * 1024
_POLL_INTERVAL_SECONDS = 1.0
_ACKNOWLEDGEMENT = "RUN_ARL2_QUESTION_CAMPAIGN"


class ARL2RuntimeError(RuntimeError):
    """The ARL-2 driver deployment, custody, or campaign terminal state failed closed."""


def _canonical_absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\x00" in value
        or not path.is_absolute()
        or value != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be a canonical absolute path")
    return path


class ARL2KernelCommandServiceSetV1(KernelModel):
    """The two driver-facing signing services, one exact operation each."""

    schema_name: Literal["aletheia.arl2_kernel_command_service_set"] = (
        "aletheia.arl2_kernel_command_service_set"
    )
    schema_version: Literal[1] = 1
    action_kernel_command: ControllerWorkerRPCServicePin
    transition_kernel_command: ControllerWorkerRPCServicePin

    @model_validator(mode="after")
    def _services_are_minimal_and_disjoint(self) -> "ARL2KernelCommandServiceSetV1":
        expected_operations = {
            "action_kernel_command": (ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,),
            "transition_kernel_command": (ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,),
        }
        for name, pin in self.named_pins:
            if pin.operations != expected_operations[name]:
                raise ValueError(f"ARL-2 kernel-command service {name} has another operation set")
        if (
            self.action_kernel_command.service_id == self.transition_kernel_command.service_id
            or self.action_kernel_command.service_principal_id
            == self.transition_kernel_command.service_principal_id
            or self.action_kernel_command.receipt_key_id
            == self.transition_kernel_command.receipt_key_id
            or self.action_kernel_command.socket_path == self.transition_kernel_command.socket_path
        ):
            raise ValueError("ARL-2 kernel-command services must be pairwise distinct")
        return self

    @property
    def named_pins(self) -> tuple[tuple[str, ControllerWorkerRPCServicePin], ...]:
        return (
            ("action_kernel_command", self.action_kernel_command),
            ("transition_kernel_command", self.transition_kernel_command),
        )

    @property
    def service_set_sha256(self) -> str:
        return canonical_sha256(self)


class ARL2KernelWriterConfigV1(KernelModel):
    """The driver's write-once Research Kernel custody for command commits."""

    schema_name: Literal["aletheia.arl2_kernel_writer_config"] = (
        "aletheia.arl2_kernel_writer_config"
    )
    schema_version: Literal[1] = 1
    trust_root: ResearchAuthorizationTrustRootV1
    cas_root: str
    cas_owner_uid: int = Field(ge=0)
    cas_group_gid: int = Field(ge=0)
    cas_device_id: int = Field(ge=0)
    cas_inode: int = Field(gt=0)
    cas_directory_mode: Literal[0o700, 0o750] = 0o700
    max_object_bytes: int = Field(ge=1, le=1024**3)
    read_only: Literal[False] = False

    @model_validator(mode="after")
    def _cas_root_is_canonical_and_private(self) -> "ARL2KernelWriterConfigV1":
        _canonical_absolute_path(self.cas_root, label="ARL-2 kernel writer CAS root")
        return self


class ARL2RoleInvocationV1(KernelModel):
    """One deployed role entrypoint the driver wakes in canonical order."""

    schema_name: Literal["aletheia.arl2_role_invocation"] = "aletheia.arl2_role_invocation"
    schema_version: Literal[1] = 1
    role: Literal["kernel_dispatcher", "terminal_dispatcher", "worker", "delivery_reconciler"]
    deployment_manifest_path: str
    deployment_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_uid: int | None = Field(default=None, ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def _manifest_path_is_canonical(self) -> "ARL2RoleInvocationV1":
        _canonical_absolute_path(
            self.deployment_manifest_path,
            label="ARL-2 role deployment manifest",
        )
        return self


class ARL2QuestionCampaignRuntimeConfigV1(KernelModel):
    """Public/keyless dependencies the ARL-2 driver process may touch."""

    schema_name: Literal["aletheia.arl2_question_campaign_runtime_config"] = (
        "aletheia.arl2_question_campaign_runtime_config"
    )
    schema_version: Literal[1] = 1
    configuration_id: str | None = Field(default=None, pattern=r"^arl2c_[0-9a-f]{32}$")
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    controller_manifest_path: str
    controller_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    kernel_writer: ARL2KernelWriterConfigV1
    kernel_command_services: ARL2KernelCommandServiceSetV1
    charter_command_path: str
    charter_command_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    problem_command_path: str
    problem_command_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_command_path: str
    question_command_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_proposal_spool_root: str
    runtime_entrypoint_path: str
    runtime_entrypoint_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    role_invocations: tuple[ARL2RoleInvocationV1, ...] = Field(min_length=4, max_length=4)
    bundle_output_root: str
    replay_implementation_source_path: str
    replay_implementation_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    data_registration_source_path: str
    data_registration_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_revision_source_path: str
    world_model_revision_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    experiment_selection_source_path: str
    experiment_selection_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign_deadline: AwareDatetime
    prepared_at: AwareDatetime
    private_signing_key_loaded: Literal[False] = False
    driver_side_stop_allowed: Literal[False] = False
    autonomous_research_design_allowed: Literal[False] = False
    generic_callback_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authority_and_custody_are_closed(self) -> "ARL2QuestionCampaignRuntimeConfigV1":
        for label, value in (
            ("controller manifest", self.controller_manifest_path),
            ("charter command", self.charter_command_path),
            ("problem command", self.problem_command_path),
            ("question command", self.question_command_path),
            ("action proposal spool", self.action_proposal_spool_root),
            ("runtime entrypoint", self.runtime_entrypoint_path),
            ("bundle output", self.bundle_output_root),
        ):
            _canonical_absolute_path(value, label=f"ARL-2 {label} path")
        command_paths = (
            self.charter_command_path,
            self.problem_command_path,
            self.question_command_path,
        )
        if len(set(command_paths)) != len(command_paths):
            raise ValueError("ARL-2 activation command paths must be distinct")
        for name, pin in self.kernel_command_services.named_pins:
            # SO_PEERCRED on the connected socket names the service, not this
            # driver, so peer_uid must differ from ours while the socket itself
            # is service-owned on our shared group (ARL-1 wiring, arl1_runtime).
            if (
                pin.peer_uid == self.process_uid
                or pin.peer_gid != self.process_gid
                or pin.socket_owner_uid != pin.peer_uid
                or pin.socket_group_gid != self.process_gid
                or pin.socket_mode != 0o660
            ):
                raise ValueError(
                    "ARL-2 kernel-command service must face this driver identity "
                    "on a UID-separated, GID-shared socket"
                )
            if not pin.valid_from <= self.prepared_at < pin.expires_at:
                raise ValueError("ARL-2 kernel-command receipt key is not active at preparation")
        if tuple(item.role for item in self.role_invocations) != (
            "kernel_dispatcher",
            "terminal_dispatcher",
            "worker",
            "delivery_reconciler",
        ):
            raise ValueError("ARL-2 role invocations are not exhaustive and canonical")
        service_principals = {
            pin.service_principal_id for _name, pin in self.kernel_command_services.named_pins
        }
        kernel_principals = {
            item.principal_id for item in self.kernel_writer.trust_root.commissioning_keys
        }
        if (
            self.process_principal_id in service_principals | kernel_principals
            or self.process_principal_id == self.controller_principal_id
            or self.controller_principal_id in service_principals
        ):
            raise ValueError("ARL-2 driver, controller, or signing authority principals overlap")
        roots = (
            Path(self.kernel_writer.cas_root),
            Path(self.action_proposal_spool_root),
            Path(self.bundle_output_root),
        )
        for index, first in enumerate(roots):
            for second in roots[index + 1 :]:
                if first == second or first in second.parents or second in first.parents:
                    raise ValueError("ARL-2 driver custody roots overlap")
        if self.prepared_at >= self.campaign_deadline:
            raise ValueError("ARL-2 campaign deadline must follow preparation")
        expected = f"arl2c_{self.configuration_sha256[:32]}"
        if self.configuration_id is not None and self.configuration_id != expected:
            raise ValueError("ARL-2 runtime configuration id differs from its contents")
        object.__setattr__(self, "configuration_id", expected)
        return self

    @property
    def configuration_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"configuration_id"}))


class ARL2QuestionCampaignRequestV1(KernelModel):
    """Campaign content registered before launch: question, card, splits, staging."""

    schema_name: Literal["aletheia.arl2_question_campaign_request"] = (
        "aletheia.arl2_question_campaign_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=r"^arl2q_[0-9a-f]{32}$")
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    launch_request: ResearchControllerLaunchRequest
    question_version: ResearchQuestionVersion
    grounding_object_sha256s: tuple[str, ...] = Field(min_length=3, max_length=3)
    dataset_card_path: str
    dataset_card_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    dataset_content_path: str
    dataset_content_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    round_split_bindings: tuple[RoundSplitBindingPolicyV1, ...] = Field(min_length=2, max_length=2)
    staged_rounds: tuple[StagedRoundRowsV1, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _request_content_is_canonical(self) -> "ARL2QuestionCampaignRequestV1":
        _canonical_absolute_path(self.dataset_card_path, label="ARL-2 dataset card path")
        _canonical_absolute_path(self.dataset_content_path, label="ARL-2 dataset content path")
        if tuple(item.round_index for item in self.round_split_bindings) != (1, 2) or tuple(
            item.round_index for item in self.staged_rounds
        ) != (1, 2):
            raise ValueError("ARL-2 campaign request must carry rounds one and two only")
        if tuple(sorted(item.object_sha256 for item in self.question_version.evidence_refs)) != (
            tuple(sorted(set(self.grounding_object_sha256s)))
        ):
            raise ValueError("ARL-2 question grounding refs differ from the pinned shas")
        if (
            self.question_version.quest_id != self.quest_id
            or self.launch_request.quest_id != self.quest_id
        ):
            raise ValueError("ARL-2 campaign request quest binding differs")
        if self.launch_request.expected_stream_version != 3:
            raise ValueError("ARL-2 launch request must pin the three pre-signed activation events")
        expected = f"arl2q_{self.request_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected:
            raise ValueError("ARL-2 campaign request id differs from its contents")
        object.__setattr__(self, "request_id", expected)
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))


class ARL2QuestionCampaignRuntimeDeploymentV1(KernelModel):
    """Externally pinned one-shot invocation; creation alone does not execute work."""

    schema_name: Literal["aletheia.arl2_question_campaign_runtime_deployment"] = (
        "aletheia.arl2_question_campaign_runtime_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str | None = Field(default=None, pattern=r"^arl2d_[0-9a-f]{32}$")
    configuration_path: str
    configuration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_path: str
    request_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    prepared_at: AwareDatetime
    campaign_deadline: AwareDatetime
    linux_required: Literal[True] = True
    explicit_apply_required: Literal[True] = True
    acknowledgement: Literal["RUN_ARL2_QUESTION_CAMPAIGN"] = _ACKNOWLEDGEMENT
    private_signing_key_loaded: Literal[False] = False
    autonomous_research_design_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_paths_are_distinct(self) -> "ARL2QuestionCampaignRuntimeDeploymentV1":
        config = _canonical_absolute_path(
            self.configuration_path, label="ARL-2 runtime configuration"
        )
        request = _canonical_absolute_path(self.request_path, label="ARL-2 campaign request")
        if config == request:
            raise ValueError("ARL-2 runtime configuration and request must be distinct")
        if self.prepared_at >= self.campaign_deadline:
            raise ValueError("ARL-2 campaign deadline must follow preparation")
        expected = f"arl2d_{self.deployment_sha256[:32]}"
        if self.deployment_id is not None and self.deployment_id != expected:
            raise ValueError("ARL-2 runtime deployment id differs from its contents")
        object.__setattr__(self, "deployment_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"deployment_id"}))


class ARL2QuestionCampaignRunReceiptV1(KernelModel):
    """Terminal evidence for one register-only or applied campaign invocation."""

    schema_name: Literal["aletheia.arl2_question_campaign_run_receipt"] = (
        "aletheia.arl2_question_campaign_run_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=r"^arl2r_[0-9a-f]{32}$")
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    kind: Literal["register_only", "applied"]
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    charter_command_sha256: str = Field(pattern=_SHA256_PATTERN)
    problem_command_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_command_sha256: str = Field(pattern=_SHA256_PATTERN)
    registered_object_sha256s: tuple[str, ...] = Field(max_length=64)
    kernel_stream_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    kernel_event_count: int = Field(default=0, ge=0)
    stop_committed_at: datetime | None = None
    bundle_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    replay_report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_matches_its_kind(self) -> "ARL2QuestionCampaignRunReceiptV1":
        if self.kind == "applied" and (
            self.stop_committed_at is None
            or self.kernel_stream_sha256 is None
            or self.bundle_manifest_sha256 is None
            or self.replay_report_sha256 is None
            or self.kernel_event_count < 1
        ):
            raise ValueError("applied ARL-2 receipt lacks its terminal campaign evidence")
        if self.kind == "register_only" and any(
            (
                self.stop_committed_at is not None,
                self.kernel_stream_sha256 is not None,
                self.bundle_manifest_sha256 is not None,
                self.replay_report_sha256 is not None,
            )
        ):
            raise ValueError("register-only ARL-2 receipt carries terminal campaign evidence")
        expected = f"arl2r_{self.receipt_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected:
            raise ValueError("ARL-2 run receipt id differs from its contents")
        object.__setattr__(self, "receipt_id", expected)
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


def _fresh_pinned_bytes(
    path_value: str | Path,
    expected_sha256: str,
    *,
    label: str,
) -> bytes:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise ARL2RuntimeError(f"{label} traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or not 0 < before.st_size <= _MAX_PINNED_FILE_BYTES
            ):
                raise ARL2RuntimeError(f"{label} has unsafe file custody")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise ARL2RuntimeError(f"{label} ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ARL2RuntimeError:
        raise
    except OSError as exc:
        raise ARL2RuntimeError(f"{label} is unavailable") from exc
    payload = b"".join(chunks)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ARL2RuntimeError(f"{label} changed or differs from its byte pin")
    return payload


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate ARL-2 runtime JSON keys: {duplicates}")
    return dict(pairs)


def _load_canonical_model(payload: bytes, model_type, *, label: str):
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
        value = model_type.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ARL2RuntimeError(f"{label} is invalid") from exc
    if canonical_json_bytes(value) != payload:
        raise ARL2RuntimeError(f"{label} is not canonical JSON")
    return value


def load_arl2_question_campaign_runtime_deployment(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> ARL2QuestionCampaignRuntimeDeploymentV1:
    payload = _fresh_pinned_bytes(
        path,
        expected_file_sha256,
        label="ARL-2 runtime deployment manifest",
    )
    return _load_canonical_model(
        payload,
        ARL2QuestionCampaignRuntimeDeploymentV1,
        label="ARL-2 runtime deployment manifest",
    )


def load_arl2_question_campaign_runtime_inputs(
    deployment: ARL2QuestionCampaignRuntimeDeploymentV1,
) -> tuple[ARL2QuestionCampaignRuntimeConfigV1, ARL2QuestionCampaignRequestV1]:
    deployment = ARL2QuestionCampaignRuntimeDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    config = _load_canonical_model(
        _fresh_pinned_bytes(
            deployment.configuration_path,
            deployment.configuration_file_sha256,
            label="ARL-2 runtime configuration",
        ),
        ARL2QuestionCampaignRuntimeConfigV1,
        label="ARL-2 runtime configuration",
    )
    request = _load_canonical_model(
        _fresh_pinned_bytes(
            deployment.request_path,
            deployment.request_file_sha256,
            label="ARL-2 campaign request",
        ),
        ARL2QuestionCampaignRequestV1,
        label="ARL-2 campaign request",
    )
    if (
        config.configuration_sha256 != deployment.configuration_sha256
        or request.request_sha256 != deployment.request_sha256
        or config.process_principal_id != deployment.process_principal_id
        or config.process_uid != deployment.process_uid
        or config.process_gid != deployment.process_gid
        or config.prepared_at != deployment.prepared_at
        or config.campaign_deadline != deployment.campaign_deadline
    ):
        raise ARL2RuntimeError("ARL-2 runtime inputs differ from the deployment manifest")
    return config, request


def action_authorization_proposal(
    submission: SubmittedActionProposal,
    *,
    expected_stream_version: int,
    expected_tail_event_sha256: str,
) -> ResearchCommandProposal:
    """Derive the exact ACTION_AUTHORIZED proposal one submission waits on.

    The stream pins name the CURRENT kernel head after the paired
    ACTION_PROPOSED commit, never the (stale) pins carried by the original
    action submission; the store's CAS would refuse those, and the scientific
    bridge binds the authorized event to the proposed one
    (sequence + 1, parent = proposed event sha).
    """

    action_ref = submission.action.object_ref
    return ResearchCommandProposal(
        quest_id=submission.request.quest_id,
        scope_binding=submission.request.scope_binding,
        expected_stream_version=expected_stream_version,
        expected_tail_event_sha256=expected_tail_event_sha256,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=action_ref.object_id,
            branch_id=submission.target_branch_id,
        ),
        proposed_by_principal_id=submission.proposed_by_principal_id,
        proposed_at=submission.submitted_at,
    )


def _campaign_transition_proposal(
    submission: SubmittedActionProposal,
    receipt: ContinuationReceipt,
    incorporated_event: ResearchEvent,
    *,
    expected_stream_version: int,
    expected_tail_event_sha256: str,
) -> ResearchCommandProposal:
    """Derive the transition proposal this campaign's two receipts can force.

    The stream pins name the CURRENT kernel head after the paired action commit,
    never the (stale) pins carried by the original action submission; the store's
    CAS would refuse those on the second command of every exchange.
    """

    action_ref = submission.action.object_ref
    if receipt.disposition is ContinuationDisposition.REDESIGN_OBSERVABLE:
        directive = RefineDirective(
            source_branch_id=submission.target_branch_id,
            child_branch_id=(
                "rbr_"
                + hashlib.sha256(
                    f"arl2-refine-child:{receipt.receipt_sha256}".encode()
                ).hexdigest()[:32]
            ),
        )
        event_type = EventType.REFINE_COMMITTED
        transition_kind = "refine"
    elif receipt.disposition is ContinuationDisposition.STOP_REQUIRED:
        if receipt.stop_grounds is None:
            raise ARL2RuntimeError("ARL-2 stop-required receipt carries no stop grounds")
        directive = StopDirective(
            branch_id=submission.target_branch_id,
            stop_reason=receipt.stop_grounds.stop_reason,
        )
        event_type = EventType.STOP_COMMITTED
        transition_kind = "stop"
    else:
        # READY and FORK dispositions are outside this campaign; fail closed
        # rather than inventing a directive the signing service must refuse.
        raise ARL2RuntimeError("ARL-2 campaign carries no other transition disposition")
    decision = TransitionDecision(
        transition_id=(
            "transition:"
            + hashlib.sha256(
                f"arl2-{transition_kind}:{receipt.receipt_sha256}".encode()
            ).hexdigest()[:32]
        ),
        quest_id=submission.request.quest_id,
        charter_ref=submission.action.charter_ref,
        source_graph_sha256=submission.request.expected_snapshot_sha256,
        selected_action_ref=action_ref,
        directive=directive,
        evidence_refs=(continuation_receipt_evidence_ref(receipt),),
        evidence_event_sha256s=(incorporated_event.event_sha256,),
        budget_receipt_sha256=submission.draft.cost_receipt_sha256,
        risk_receipt_sha256=submission.draft.risk_receipt_sha256,
        policy_receipt_sha256=submission.proposal_policy_sha256,
        reason_codes=receipt.reason_codes,
        rationale=submission.draft.epistemic_purpose,
        decided_by_principal_id=submission.proposed_by_principal_id,
        decided_at=submission.submitted_at,
    )
    payload = (
        RefineCommittedPayload(decision=decision)
        if transition_kind == "refine"
        else StopCommittedPayload(decision=decision)
    )
    return ResearchCommandProposal(
        quest_id=submission.request.quest_id,
        scope_binding=submission.request.scope_binding,
        expected_stream_version=expected_stream_version,
        expected_tail_event_sha256=expected_tail_event_sha256,
        event_type=event_type,
        payload=payload,
        proposed_by_principal_id=submission.proposed_by_principal_id,
        proposed_at=submission.submitted_at,
    )


class ARL2QuestionCampaignDriver:
    """Compose the exact keyless driver; optional ports support hermetic tests only."""

    def __init__(
        self,
        *,
        config: ARL2QuestionCampaignRuntimeConfigV1,
        request: ARL2QuestionCampaignRequestV1,
        deployment_sha256: str,
        kernel_store: ResearchKernelStore | None = None,
        kernel_archive: FilesystemResearchArchive | None = None,
        transport: ControllerWorkerRPCTransport | None = None,
        runner: Callable[[list[str]], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        receipt_lookup: (Callable[[str, str], tuple[ContinuationReceipt, str, str]] | None) = None,
    ) -> None:
        self.config = ARL2QuestionCampaignRuntimeConfigV1.model_validate(
            config.model_dump(mode="python")
        )
        self.request = ARL2QuestionCampaignRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        self.deployment_sha256 = deployment_sha256
        self._implementation_sources = self._load_implementation_sources()
        if kernel_store is None:
            self._archive = kernel_archive or self._compose_archive()
            self._kernel_store = ResearchKernelStore(
                trust_root=self.config.kernel_writer.trust_root,
                archive=self._archive,
            )
        else:
            self._kernel_store = kernel_store
            self._archive = kernel_archive
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._receipt_lookup = receipt_lookup or self._continuation_receipt
        self._action_client = ControllerWorkerRPCClient(
            pin=self.config.kernel_command_services.action_kernel_command,
            controller_id=self.config.controller_id,
            controller_manifest_sha256=self.config.controller_manifest_sha256,
            worker_process_principal_id=self.config.process_principal_id,
            transport=transport,
            clock=self._clock,
        )
        self._transition_client = ControllerWorkerRPCClient(
            pin=self.config.kernel_command_services.transition_kernel_command,
            controller_id=self.config.controller_id,
            controller_manifest_sha256=self.config.controller_manifest_sha256,
            worker_process_principal_id=self.config.process_principal_id,
            transport=transport,
            clock=self._clock,
        )

    def _compose_archive(self) -> FilesystemResearchArchive:
        writer = self.config.kernel_writer
        path = Path(writer.cas_root)
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ARL2RuntimeError("ARL-2 kernel writer CAS is unavailable") from exc
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != writer.cas_owner_uid
            or metadata.st_gid != writer.cas_group_gid
            or metadata.st_dev != writer.cas_device_id
            or metadata.st_ino != writer.cas_inode
            or stat.S_IMODE(metadata.st_mode) != writer.cas_directory_mode
        ):
            raise ARL2RuntimeError("ARL-2 kernel writer CAS custody differs")
        return FilesystemResearchArchive(
            path,
            max_object_bytes=writer.max_object_bytes,
            read_only=False,
            directory_mode=writer.cas_directory_mode,
            # the 0750 writer root pairs with group-readable objects so the
            # kernel_reader services and the worker role compose read-only
            # archives through the group class at distinct uids
            object_mode=0o440 if writer.cas_directory_mode == 0o750 else 0o400,
        )

    def _load_implementation_sources(self) -> dict[str, tuple[Path, bytes]]:
        package_root = Path(__file__).resolve(strict=True).parent
        expected = {
            "replay": (
                self.config.replay_implementation_source_path,
                self.config.replay_implementation_source_sha256,
                package_root / "research_controller" / "campaign_replay.py",
            ),
            "data_registration": (
                self.config.data_registration_source_path,
                self.config.data_registration_source_sha256,
                package_root / "protocols" / "data_registration.py",
            ),
            "world_model_revision": (
                self.config.world_model_revision_source_path,
                self.config.world_model_revision_source_sha256,
                package_root / "research_controller" / "world_model_revision.py",
            ),
            "experiment_selection": (
                self.config.experiment_selection_source_path,
                self.config.experiment_selection_source_sha256,
                package_root / "research_controller" / "experiment_selection.py",
            ),
        }
        loaded: dict[str, tuple[Path, bytes]] = {}
        paths = []
        for name, (declared, sha, module_path) in expected.items():
            path = Path(declared)
            if path.resolve(strict=True) != module_path.resolve(strict=True):
                raise ARL2RuntimeError(f"ARL-2 {name} implementation path resolves another module")
            payload = _fresh_pinned_bytes(path, sha, label=f"ARL-2 {name} implementation")
            loaded[name] = (path, payload)
            paths.append(path)
        if len(set(paths)) != len(paths):
            raise ARL2RuntimeError("ARL-2 implementation pins must be distinct")
        return loaded

    def _verify_implementation_sources_unchanged(self) -> None:
        for name, (path, payload) in self._implementation_sources.items():
            if payload != _fresh_pinned_bytes(
                path,
                hashlib.sha256(payload).hexdigest(),
                label=f"ARL-2 {name} implementation",
            ):
                raise ARL2RuntimeError("ARL-2 implementation changed during composition")

    _COMMAND_WIRE_LABELS = {
        "charter": "ARL-2 charter activation command",
        "problem": "ARL-2 problem admission command",
        "question": "ARL-2 question admission command",
    }

    def _pre_signed_command(self, label: str) -> AuthorizedResearchCommand:
        if label not in self._COMMAND_WIRE_LABELS:
            raise ARL2RuntimeError(f"ARL-2 has no pre-signed command labelled {label!r}")
        wire_label = self._COMMAND_WIRE_LABELS[label]
        path = getattr(self.config, f"{label}_command_path")
        sha = getattr(self.config, f"{label}_command_file_sha256")
        return _load_canonical_model(
            _fresh_pinned_bytes(path, sha, label=wire_label),
            AuthorizedResearchCommand,
            label=wire_label,
        )

    def _activation_commands(
        self,
    ) -> tuple[AuthorizedResearchCommand, AuthorizedResearchCommand, AuthorizedResearchCommand]:
        """Load the three pre-signed activation commands and their agreements."""

        charter = self._pre_signed_command("charter")
        problem = self._pre_signed_command("problem")
        question = self._pre_signed_command("question")
        if (
            charter.payload.charter_ref.object_sha256
            != self.request.question_version.charter_ref.object_sha256
        ):
            raise ARL2RuntimeError("ARL-2 charter command names another charter version")
        if (
            problem.payload.problem_ref.object_sha256
            != self.request.question_version.problem_ref.object_sha256
        ):
            raise ARL2RuntimeError("ARL-2 problem command names another problem version")
        if (
            question.payload.question_ref.object_sha256
            != self.request.question_version.object_sha256
        ):
            raise ARL2RuntimeError("ARL-2 question command names another question version")
        return charter, problem, question

    def _continuation_receipt(
        self, quest_id: str, receipt_sha256: str
    ) -> tuple[ContinuationReceipt, str, str]:
        """Read one recorded continuation receipt and its admission identity."""

        with session_factory() as session:
            rows = session.scalars(_continuation_rows_query(quest_id)).all()
            matches = [row for row in rows if row.receipt_sha256 == receipt_sha256]
            if len(matches) != 1:
                raise ARL2RuntimeError("ARL-2 continuation receipt is not uniquely recorded")
            row = matches[0]
            return (
                ContinuationReceipt.model_validate(row.receipt_json),
                row.committed_admission_sha256,
                row.scientific_observation_sha256,
            )

    def _pending_submissions(self) -> tuple[SubmittedActionProposal, ...]:
        """Enumerate the write-once spool this driver's quest may carry to signing."""

        root = Path(self.config.action_proposal_spool_root)
        if not root.is_dir():
            return ()
        submissions = []
        for shard in sorted(root.rglob("*.json")):
            payload = shard.read_bytes()
            try:
                submission = SubmittedActionProposal.model_validate(
                    json.loads(payload, object_pairs_hook=_unique_object)
                )
            except (OSError, TypeError, ValueError):
                raise ARL2RuntimeError("ARL-2 action proposal spool holds an invalid submission")
            if submission.request.quest_id == self.request.quest_id:
                submissions.append(submission)
        return tuple(submissions)

    def _sign_action(
        self,
        submission: SubmittedActionProposal,
        *,
        expected_stream_version: int,
        expected_tail_event_sha256: str,
    ) -> AuthorizedResearchCommand:
        proposal = action_authorization_proposal(
            submission,
            expected_stream_version=expected_stream_version,
            expected_tail_event_sha256=expected_tail_event_sha256,
        )
        return self._action_client.call(
            ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,
            payload={
                "proposal": proposal.model_dump(mode="json"),
                "submitted": submission.model_dump(mode="json"),
            },
            result_type=AuthorizedResearchCommand,
        )

    def _sign_action_proposed(
        self, submission: SubmittedActionProposal
    ) -> AuthorizedResearchCommand:
        """Sign the admission command the submission itself carries verbatim."""

        return self._action_client.call(
            ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,
            payload={
                "proposal": submission.command_proposal.model_dump(mode="json"),
                "submitted": submission.model_dump(mode="json"),
            },
            result_type=AuthorizedResearchCommand,
        )

    def _sign_transition(
        self,
        submission: SubmittedActionProposal,
        receipt: ContinuationReceipt,
        incorporated_event: ResearchEvent,
        *,
        expected_stream_version: int,
        expected_tail_event_sha256: str,
    ) -> AuthorizedResearchCommand:
        proposal = _campaign_transition_proposal(
            submission,
            receipt,
            incorporated_event,
            expected_stream_version=expected_stream_version,
            expected_tail_event_sha256=expected_tail_event_sha256,
        )
        return self._transition_client.call(
            ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,
            payload={
                "proposal": proposal.model_dump(mode="json"),
                "submitted": submission.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
                "incorporated_event": incorporated_event.model_dump(mode="json"),
            },
            result_type=AuthorizedResearchCommand,
        )

    def _wake_roles(self) -> None:
        _fresh_pinned_bytes(
            self.config.runtime_entrypoint_path,
            self.config.runtime_entrypoint_file_sha256,
            label="ARL-2 runtime entrypoint",
        )
        for invocation in self.config.role_invocations:
            _fresh_pinned_bytes(
                invocation.deployment_manifest_path,
                invocation.deployment_manifest_file_sha256,
                label=f"ARL-2 {invocation.role} deployment manifest",
            )
            remaining = max(
                1.0,
                (self.config.campaign_deadline - self._utc_now()).total_seconds(),
            )
            command = [
                sys.executable,
                self.config.runtime_entrypoint_path,
                "--deployment-manifest",
                invocation.deployment_manifest_path,
                "--deployment-manifest-sha256",
                invocation.deployment_manifest_file_sha256,
                "--once",
            ]
            if invocation.process_uid is not None:
                # the worker role runs at a uid distinct from this driver so
                # its read-only CAS compose passes through the group class;
                # the numeric sudo form keeps the pin uid-only (no name
                # coupling) and the sudoers rule pins the interpreter
                command = [
                    "/usr/bin/sudo",
                    "-n",
                    "-u",
                    f"#{invocation.process_uid}",
                    "--preserve-env=ALETHEIA_DATABASE_URL,PYTHONPATH,"
                    "PYTHONDONTWRITEBYTECODE",
                    *command,
                ]
            if self._runner is None:
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        check=False,
                        timeout=remaining,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ARL2RuntimeError(
                        f"ARL-2 {invocation.role} cycle reached the campaign deadline"
                    ) from exc
                except OSError as exc:
                    # a missing or unexecutable sudo (the sudo-prefixed worker
                    # spawn) must fail closed through the same error contract
                    # as every other role-cycle failure, not a raw traceback
                    raise ARL2RuntimeError(
                        f"ARL-2 {invocation.role} cycle could not launch: {exc}"
                    ) from exc
                if completed.returncode != 0:
                    raise ARL2RuntimeError(f"ARL-2 {invocation.role} cycle failed closed")
                continue
            self._runner(command)

    def _launch_controller(self) -> None:
        manifest = _load_canonical_model(
            _fresh_pinned_bytes(
                self.config.controller_manifest_path,
                self.config.controller_manifest_file_sha256,
                label="ARL-2 controller manifest",
            ),
            ResearchControllerManifest,
            label="ARL-2 controller manifest",
        )
        if (
            manifest.controller_id != self.config.controller_id
            or manifest.manifest_sha256 != self.config.controller_manifest_sha256
        ):
            raise ARL2RuntimeError("ARL-2 controller manifest differs from the driver config")
        launcher = ResearchControllerLauncher(
            kernel_store=self._kernel_store,
            manifest=manifest,
            persistence=PostgreSQLControllerLaunchAdapter(
                kernel_store=self._kernel_store,
                queue=_launch_queue(),
            ),
        )
        launcher.launch(
            self.request.launch_request,
            registered_by_principal_id=self.config.controller_principal_id,
        )

    def _assemble_bundle(
        self, stop_event: ResearchEvent
    ) -> tuple[bytes, object, tuple[ResearchEvent, ...]]:
        """Export the as_of-pinned stream, index the bundle, verify it in-process."""

        from aletheia.research_controller.campaign_replay import (
            DATASET_CARD_KIND,
            DATASET_CONTENT_KIND,
            KERNEL_STREAM_KIND,
            ROUND_SPLIT_POLICY_KIND_PREFIX,
            STAGED_ROUND_ROWS_KIND_PREFIX,
            CampaignBundleEntryV1,
            CampaignBundleManifestV1,
            kernel_stream_bytes,
            kernel_stream_sha256,
            verify_campaign_bundle,
        )
        from aletheia.protocols.data_registration import RegisteredDatasetV1

        events = self._kernel_store.audit(
            self.request.quest_id,
            as_of=stop_event.committed_at,
        ).events
        card_bytes = _fresh_pinned_bytes(
            self.request.dataset_card_path,
            self.request.dataset_card_file_sha256,
            label="ARL-2 dataset card",
        )
        csv_bytes = _fresh_pinned_bytes(
            self.request.dataset_content_path,
            self.request.dataset_content_file_sha256,
            label="ARL-2 dataset content",
        )
        card = _load_canonical_model(card_bytes, RegisteredDatasetV1, label="ARL-2 dataset card")
        bindings = self.request.round_split_bindings
        staged = self.request.staged_rounds
        stream_bytes = kernel_stream_bytes(events)
        objects: dict[str, bytes] = {
            canonical_sha256(card): card_bytes,
            hashlib.sha256(csv_bytes).hexdigest(): csv_bytes,
            kernel_stream_sha256(events): stream_bytes,
        }
        entries = [
            CampaignBundleEntryV1(
                object_kind=DATASET_CARD_KIND,
                object_sha256=canonical_sha256(card),
                byte_length=len(card_bytes),
                canonical_json=True,
            ),
            CampaignBundleEntryV1(
                object_kind=DATASET_CONTENT_KIND,
                object_sha256=hashlib.sha256(csv_bytes).hexdigest(),
                byte_length=len(csv_bytes),
                canonical_json=False,
            ),
            CampaignBundleEntryV1(
                object_kind=KERNEL_STREAM_KIND,
                object_sha256=kernel_stream_sha256(events),
                byte_length=len(stream_bytes),
                canonical_json=True,
            ),
        ]
        for binding in bindings:
            payload = canonical_json_bytes(binding)
            sha = hashlib.sha256(payload).hexdigest()
            entries.append(
                CampaignBundleEntryV1(
                    object_kind=f"{ROUND_SPLIT_POLICY_KIND_PREFIX}round-{binding.round_index:03d}",
                    object_sha256=sha,
                    byte_length=len(payload),
                    canonical_json=True,
                )
            )
            objects[sha] = payload
        for rows in staged:
            payload = canonical_json_bytes(rows)
            sha = hashlib.sha256(payload).hexdigest()
            entries.append(
                CampaignBundleEntryV1(
                    object_kind=f"{STAGED_ROUND_ROWS_KIND_PREFIX}round-{rows.round_index:03d}",
                    object_sha256=sha,
                    byte_length=len(payload),
                    canonical_json=True,
                )
            )
            objects[sha] = payload
        manifest = CampaignBundleManifestV1(
            quest_id=self.request.quest_id,
            as_of=stop_event.committed_at,
            entries=tuple(sorted(entries, key=lambda item: item.object_kind)),
        )
        report = verify_campaign_bundle(
            card=card,
            csv_bytes=csv_bytes,
            events=events,
            round_split_bindings=bindings,
            staged_rounds=staged,
            manifest=manifest,
            objects=objects,
        )
        bundle_root = Path(self.config.bundle_output_root)
        for entry in manifest.entries:
            extension = "json" if entry.canonical_json else "bin"
            target = bundle_root / f"{entry.object_kind}.{extension}"
            target.write_bytes(objects[entry.object_sha256])
        (bundle_root / "campaign-bundle-manifest.json").write_bytes(canonical_json_bytes(manifest))
        self._verify_implementation_sources_unchanged()
        return canonical_json_bytes(manifest), report, events

    def register(self) -> ARL2QuestionCampaignRunReceiptV1:
        """Archive campaign content and commit the three pre-signed commands."""

        from aletheia.protocols.data_registration import RegisteredDatasetV1, verify_dataset_card

        card_bytes = _fresh_pinned_bytes(
            self.request.dataset_card_path,
            self.request.dataset_card_file_sha256,
            label="ARL-2 dataset card",
        )
        csv_bytes = _fresh_pinned_bytes(
            self.request.dataset_content_path,
            self.request.dataset_content_file_sha256,
            label="ARL-2 dataset content",
        )
        card = _load_canonical_model(card_bytes, RegisteredDatasetV1, label="ARL-2 dataset card")
        verify_dataset_card(card, csv_bytes)
        from aletheia.research_controller.campaign_replay import (
            verify_pre_registered_round_splits,
        )

        verify_pre_registered_round_splits(
            card=card,
            csv_bytes=csv_bytes,
            round_split_bindings=self.request.round_split_bindings,
        )
        if self._archive is None:
            raise ARL2RuntimeError("ARL-2 register requires the writer archive")
        archived = self._archive.archive_object(self.request.question_version)
        charter, problem, question = self._activation_commands()
        self._kernel_store.commit(charter)
        self._kernel_store.commit(problem)
        self._kernel_store.commit(question)
        self._verify_implementation_sources_unchanged()
        return ARL2QuestionCampaignRunReceiptV1(
            quest_id=self.request.quest_id,
            kind="register_only",
            configuration_sha256=self.config.configuration_sha256,
            deployment_sha256=self.deployment_sha256,
            charter_command_sha256=charter.command_sha256,
            problem_command_sha256=problem.command_sha256,
            question_command_sha256=question.command_sha256,
            registered_object_sha256s=(archived.object_ref.object_sha256,),
            finished_at=self._clock(),
        )

    def _utc_now(self) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
            raise ARL2RuntimeError("ARL-2 runtime clock is not timezone-aware UTC")
        return observed_at

    def execute(self) -> ARL2QuestionCampaignRunReceiptV1:
        """Drive the launched campaign to its receipt-forced terminal stop."""

        charter, problem, question = self._activation_commands()
        self._kernel_store.commit(charter)
        self._kernel_store.commit(problem)
        self._kernel_store.commit(question)
        self._launch_controller()
        while True:
            if self._utc_now() >= self.config.campaign_deadline:
                raise ARL2RuntimeError("ARL-2 campaign reached its deadline without a stop")
            self._wake_roles()
            events = self._kernel_store.audit(self.request.quest_id).events
            stop_events = [
                event for event in events if event.event_type is EventType.STOP_COMMITTED
            ]
            self._carry_signing_work(events)
            if stop_events:
                stop_event = stop_events[-1]
                manifest_bytes, report, pinned_events = self._assemble_bundle(stop_event)
                from aletheia.research_controller.campaign_replay import kernel_stream_sha256

                return ARL2QuestionCampaignRunReceiptV1(
                    quest_id=self.request.quest_id,
                    kind="applied",
                    configuration_sha256=self.config.configuration_sha256,
                    deployment_sha256=self.deployment_sha256,
                    charter_command_sha256=charter.command_sha256,
                    problem_command_sha256=problem.command_sha256,
                    question_command_sha256=question.command_sha256,
                    registered_object_sha256s=(),
                    kernel_stream_sha256=kernel_stream_sha256(pinned_events),
                    kernel_event_count=len(pinned_events),
                    stop_committed_at=stop_event.committed_at,
                    bundle_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                    replay_report_sha256=canonical_sha256(report),
                    finished_at=self._utc_now(),
                )
            self._sleeper(_POLL_INTERVAL_SECONDS)

    def _carry_signing_work(self, events: tuple[ResearchEvent, ...]) -> None:
        """Carry every still-unsigned proposal to its signing service and commit."""

        authorized_action_ids = {
            event.payload.action_id
            for event in events
            if event.event_type is EventType.ACTION_AUTHORIZED
        }
        proposed_action_ids = {
            event.payload.action_ref.object_id
            for event in events
            if event.event_type is EventType.ACTION_PROPOSED
        }
        committed_transition_ids = {
            event.payload.decision.transition_id
            for event in events
            if event.event_type in (EventType.REFINE_COMMITTED, EventType.STOP_COMMITTED)
        }
        by_event_sha = {event.event_sha256: event for event in events}
        for submission in self._pending_submissions():
            action_id = submission.action.object_ref.object_id
            if action_id not in authorized_action_ids:
                if action_id not in proposed_action_ids:
                    # The real store resolves an ACTION_AUTHORIZED payload
                    # only against an action its ACTION_PROPOSED event already
                    # admitted (state.actions), so the unsigned admission
                    # command the submission carries is signed and committed
                    # FIRST, with the action object staged into the CAS
                    # archive the same way the register path stages its
                    # question version.
                    if self._archive is None:
                        raise ARL2RuntimeError(
                            "ARL-2 action admission requires the writer archive"
                        )
                    self._archive.archive_object(submission.action)
                    self._kernel_store.commit(self._sign_action_proposed(submission))
                    events = self._kernel_store.audit(self.request.quest_id).events
                    proposed_action_ids = {
                        event.payload.action_ref.object_id
                        for event in events
                        if event.event_type is EventType.ACTION_PROPOSED
                    }
                # The authorization pins the head the admission commit just
                # moved; the submission's own pins are pre-proposal and the
                # store CAS would refuse them.
                self._kernel_store.commit(
                    self._sign_action(
                        submission,
                        expected_stream_version=len(events),
                        expected_tail_event_sha256=events[-1].event_sha256,
                    )
                )
                # The action commit moved the kernel head: re-audit so the
                # paired transition pins the live head.  The submission's own
                # pins are now stale and the store CAS would refuse them.
                events = self._kernel_store.audit(self.request.quest_id).events
                authorized_action_ids = {
                    event.payload.action_id
                    for event in events
                    if event.event_type is EventType.ACTION_AUTHORIZED
                }
                by_event_sha = {event.event_sha256: event for event in events}
            source_receipt = submission.request.source_receipt_sha256
            if source_receipt is None:
                continue
            receipt, admission_sha, observation_sha = self._receipt_lookup(
                submission.request.quest_id,
                source_receipt,
            )
            # The incorporated event is the stream's own record of the exact
            # admission the receipt row names; anything else is not signed.
            incorporated = [
                event
                for event in events
                if event.event_type is EventType.OBSERVATION_INCORPORATED
                and isinstance(event.payload, ObservationIncorporatedPayload)
                and event.payload.scientific_slot_id == receipt.scientific_slot_id
                and event.payload.committed_admission_sha256 == admission_sha
                and event.payload.scientific_observation_sha256 == observation_sha
                and event.event_sha256 in by_event_sha
            ]
            if len(incorporated) != 1:
                continue
            incorporated_event = incorporated[0]
            expected_stream_version = len(events)
            expected_tail_event_sha256 = events[-1].event_sha256
            proposal = _campaign_transition_proposal(
                submission,
                receipt,
                incorporated_event,
                expected_stream_version=expected_stream_version,
                expected_tail_event_sha256=expected_tail_event_sha256,
            )
            if proposal.payload.decision.transition_id in committed_transition_ids:
                continue
            self._kernel_store.commit(
                self._sign_transition(
                    submission,
                    receipt,
                    incorporated_event,
                    expected_stream_version=expected_stream_version,
                    expected_tail_event_sha256=expected_tail_event_sha256,
                )
            )
            events = self._kernel_store.audit(self.request.quest_id).events
            committed_transition_ids = {
                event.payload.decision.transition_id
                for event in events
                if event.event_type in (EventType.REFINE_COMMITTED, EventType.STOP_COMMITTED)
            }
            by_event_sha = {event.event_sha256: event for event in events}


def _launch_queue():
    from aletheia.jobs.queue import DurableTaskQueue

    return DurableTaskQueue(principal="research-controller:launcher")


def _continuation_rows_query(quest_id: str):
    from aletheia.observations.store import ResearchContinuationReceiptRecord

    from sqlalchemy import select

    return select(ResearchContinuationReceiptRecord).where(
        ResearchContinuationReceiptRecord.quest_id == quest_id
    )


def execute_arl2_question_campaign_deployment(
    deployment: ARL2QuestionCampaignRuntimeDeploymentV1,
    *,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
    register_only: bool = False,
    transport: ControllerWorkerRPCTransport | None = None,
    runner: Callable[[list[str]], bytes] | None = None,
    kernel_store: ResearchKernelStore | None = None,
    kernel_archive: FilesystemResearchArchive | None = None,
    receipt_lookup: (Callable[[str, str], tuple[ContinuationReceipt, str, str]] | None) = None,
) -> ARL2QuestionCampaignRunReceiptV1:
    """Execute on the exact Linux identity; the driver only couriers signatures."""

    if sys.platform != "linux":
        raise ARL2RuntimeError("ARL-2 question campaign execution requires Linux")
    if os.geteuid() != deployment.process_uid or os.getegid() != deployment.process_gid:
        raise ARL2RuntimeError("ARL-2 runtime process identity differs from deployment")
    require_schema_exact()
    config, request = load_arl2_question_campaign_runtime_inputs(deployment)
    if (
        config.database_url_sha256 != hashlib.sha256(_database_url().encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ARL2RuntimeError("ARL-2 runtime database identity differs")
    driver = ARL2QuestionCampaignDriver(
        config=config,
        request=request,
        deployment_sha256=deployment.deployment_sha256,
        kernel_store=kernel_store,
        kernel_archive=kernel_archive,
        transport=transport,
        runner=runner,
        clock=clock,
        sleeper=sleeper,
        receipt_lookup=receipt_lookup,
    )
    if register_only:
        return driver.register()
    return driver.execute()


def _database_url() -> str:
    from aletheia.config import get_settings

    return get_settings().database_url


__all__ = [
    "ARL2KernelCommandServiceSetV1",
    "ARL2KernelWriterConfigV1",
    "ARL2QuestionCampaignDriver",
    "ARL2QuestionCampaignRequestV1",
    "ARL2QuestionCampaignRunReceiptV1",
    "ARL2QuestionCampaignRuntimeConfigV1",
    "ARL2QuestionCampaignRuntimeDeploymentV1",
    "ARL2RoleInvocationV1",
    "ARL2RuntimeError",
    "action_authorization_proposal",
    "execute_arl2_question_campaign_deployment",
    "load_arl2_question_campaign_runtime_deployment",
    "load_arl2_question_campaign_runtime_inputs",
]
