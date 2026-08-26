"""Durable, proposal-only service for the controller's three action-producing steps.

The controller worker never receives a Kernel signing key or a mutable Kernel store.  This
operational service re-audits the authoritative Kernel/receipt state, lets an untrusted provider
choose only the bounded proposal fields, and publishes one immutable submission per audited
request.  A separate command authority must still verify, sign, and commit the proposal.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from aletheia.db import session_scope
from aletheia.observations.store import (
    get_continuation_receipt_by_slot,
    get_protocol_compilation_by_action,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, verify_compilation
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.research_controller.action_proposals import (
    ActionProposalBlocked,
    ActionProposalContextSourcePort,
    ActionProposalDraftProviderPort,
    ActionProposalDraftVerificationPort,
    ActionProposalError,
    ActionProposalSubmissionStorePort,
    ActionProposalTarget,
    ActionProposalTargetLifecycle,
    ControllerActionProposalRequest,
    SubmittedActionProposal,
    materialize_action_proposal,
    verify_submitted_action_proposal,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    BranchLifecycle,
    ResearchStateGraph,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    EvidenceKind,
    EvidenceRef,
    EventType,
    KernelObjectRef,
    ObservationIncorporatedPayload,
    ResearchActionProposal,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.research_store.store import (
    ResearchKernelStore,
    ResearchObjectArchive,
    ResearchReplayAudit,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_SUBMISSION_BYTES = 4 * 1024 * 1024
_PROPOSAL_STEPS = frozenset(
    {
        ControllerStep.PROPOSE_ACTION,
        ControllerStep.PROPOSE_REDESIGN,
        ControllerStep.PROPOSE_FOLLOWUP,
    }
)


def _stable_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    """Exclude access time, which a successful read may legitimately update."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _action_ref_key(value: KernelObjectRef) -> tuple[str, str]:
    return (value.object_id, value.object_sha256)


def _evidence_key(value: EvidenceRef) -> tuple[str, str, str]:
    return (value.kind.value, value.object_sha256, value.object_id or "")


def _require_exact_tick(
    *,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
) -> None:
    if (
        plan_recovery_tick(projection) != plan
        or plan.step not in _PROPOSAL_STEPS
        or wakeup.quest_id != projection.quest_id
        or plan.audited_stream_version != projection.audited_stream_version
        or plan.audited_tail_event_sha256 != projection.audited_tail_event_sha256
        or plan.audited_snapshot_sha256 != projection.audited_snapshot_sha256
    ):
        raise ActionProposalError("action proposal context received a stale controller tick")


def _require_exact_audit(
    *,
    audit: ResearchReplayAudit,
    projection: ControllerRecoveryProjection,
) -> ResearchReplayAudit:
    try:
        audit = ResearchReplayAudit.model_validate(audit.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ActionProposalError("Kernel audit is not a closed replay projection") from exc
    state = audit.state
    if (
        audit.quest_id != projection.quest_id
        or state.quest_id != projection.quest_id
        or state.terminal
        or state.charter_ref is None
        or not audit.events
        or state.stream_version != projection.audited_stream_version
        or state.tail_event_sha256 != projection.audited_tail_event_sha256
        or state.snapshot_sha256 != projection.audited_snapshot_sha256
        or audit.events[-1].sequence != state.stream_version
        or audit.events[-1].event_sha256 != state.tail_event_sha256
        or len(audit.verified_snapshot_sha256s) != len(audit.events)
        or audit.verified_snapshot_sha256s[-1] != state.snapshot_sha256
    ):
        raise ActionProposalError("Kernel audit differs from the controller recovery projection")
    return audit


def _target_lifecycle(value: BranchLifecycle) -> ActionProposalTargetLifecycle | None:
    return {
        BranchLifecycle.ACTIVE: ActionProposalTargetLifecycle.ACTIVE,
        BranchLifecycle.ADMITTED: ActionProposalTargetLifecycle.ADMITTED,
        BranchLifecycle.PAUSED: ActionProposalTargetLifecycle.PAUSED,
    }.get(value)


def _initial_targets(state: ResearchStateGraph) -> tuple[ActionProposalTarget, ...]:
    active_kinds = tuple(
        sorted(
            (kind for kind in ActionKind if kind is not ActionKind.ACTIVATE),
            key=lambda item: item.value,
        )
    )
    targets: list[ActionProposalTarget] = []
    for branch in state.branches:
        lifecycle = _target_lifecycle(branch.lifecycle)
        if lifecycle is None or not branch.question_refs:
            continue
        targets.append(
            ActionProposalTarget(
                branch_id=branch.branch_id,
                branch_lifecycle=lifecycle,
                question_ref=branch.question_refs[-1],
                allowed_action_kinds=(
                    active_kinds
                    if lifecycle is ActionProposalTargetLifecycle.ACTIVE
                    else (ActionKind.ACTIVATE,)
                ),
            )
        )
    return tuple(
        sorted(
            targets,
            key=lambda item: (
                item.branch_id,
                item.question_ref.object_sha256,
                item.target_sha256,
            ),
        )
    )


def _source_action(state: ResearchStateGraph, action_sha256: str):
    matches = tuple(
        action for action in state.actions if action.action_ref.object_sha256 == action_sha256
    )
    if len(matches) != 1:
        raise ActionProposalError("proposal source action is not unique in the audited Kernel")
    return matches[0]


def _downstream_target(
    *,
    state: ResearchStateGraph,
    action_sha256: str,
    required_kind: ActionKind,
) -> ActionProposalTarget:
    source = _source_action(state, action_sha256)
    branches = {item.branch_id: item for item in state.branches}
    branch = branches.get(source.branch_id)
    if branch is None or branch.lifecycle is not BranchLifecycle.ACTIVE or not branch.question_refs:
        raise ActionProposalBlocked(("action_proposal:source_branch_not_active",))
    return ActionProposalTarget(
        branch_id=branch.branch_id,
        branch_lifecycle=ActionProposalTargetLifecycle.ACTIVE,
        question_ref=branch.question_refs[-1],
        allowed_action_kinds=(required_kind,),
    )


class PostgreSQLActionProposalContextSource(ActionProposalContextSourcePort):
    """Rebuild a bounded proposal request from locked Kernel and durable receipt rows."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        object_archive: ResearchObjectArchive,
    ) -> None:
        if not callable(getattr(kernel_store, "audit_in_session", None)) or not callable(
            getattr(object_archive, "load_object", None)
        ):
            raise TypeError("action proposal context requires Kernel audit and object custody")
        self._kernel_store = kernel_store
        self._object_archive = object_archive

    def load_request(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerActionProposalRequest:
        try:
            _require_exact_tick(wakeup=wakeup, projection=projection, plan=plan)
            with session_scope() as session:
                audit = _require_exact_audit(
                    audit=self._kernel_store.audit_in_session(session, projection.quest_id),
                    projection=projection,
                )
                return self._build_request(
                    session=session,
                    audit=audit,
                    state=audit.state,
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                    object_archive=self._object_archive,
                )
        except (ActionProposalBlocked, ActionProposalError):
            raise
        except Exception as exc:  # noqa: BLE001 - DB/CAS/receipt verification fails closed
            raise ActionProposalError("action proposal context audit failed closed") from exc

    @staticmethod
    def _build_request(
        *,
        session,
        audit: ResearchReplayAudit,
        state: ResearchStateGraph,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
        object_archive: ResearchObjectArchive,
    ) -> ControllerActionProposalRequest:
        required_kind: ActionKind | None = None
        required_evidence: tuple[EvidenceRef, ...] = ()
        source_action_sha256: str | None = None
        source_receipt_sha256: str | None = None

        if plan.step is ControllerStep.PROPOSE_ACTION:
            targets = _initial_targets(state)
        elif plan.step is ControllerStep.PROPOSE_REDESIGN:
            if (
                projection.action_sha256 is None
                or projection.compilation_disposition is not CompilationDisposition.BLOCKED
            ):
                raise ActionProposalError("redesign request lacks a blocked source compilation")
            source = _source_action(state, projection.action_sha256)
            if source.lifecycle is not ActionLifecycle.AUTHORIZED:
                raise ActionProposalError("redesign source action is not authorized")
            archived_action = object_archive.load_object(source.action_ref)
            if not isinstance(archived_action.payload, ResearchActionProposal):
                raise ActionProposalError("redesign source CAS object is not an action")
            source_object = archived_action.payload
            compilation = get_protocol_compilation_by_action(
                session,
                quest_id=projection.quest_id,
                action_sha256=projection.action_sha256,
            )
            if compilation is None:
                raise ActionProposalError("blocked compilation receipt is missing")
            request = ProtocolCompilationRequest.model_validate(compilation.request_json)
            result = ProtocolCompilationResult.model_validate(compilation.result_json)
            verify_compilation(request, result)
            graph_scope = request.protocol.graph_scope
            proposed_event = audit.events[-2]
            authorized_event = audit.events[-1]
            if (
                compilation.quest_id != projection.quest_id
                or compilation.action_sha256 != projection.action_sha256
                or compilation.protocol_id != request.protocol.protocol_id
                or compilation.protocol_version != request.protocol.version
                or compilation.protocol_sha256 != request.protocol.protocol_sha256
                or compilation.request_sha256 != canonical_sha256(request)
                or compilation.result_sha256 != canonical_sha256(result)
                or result.report.accepted
                or result.work_order is not None
                or compilation.receipt_sha256 != result.receipt.receipt_sha256
                or graph_scope.scope_binding != audit.scope_binding
                or graph_scope.graph_snapshot_sha256 != projection.audited_snapshot_sha256
                or graph_scope.branch_id != source.branch_id
                or graph_scope.question_ref != source_object.question_ref
                or source_object.object_ref != source.action_ref
                or source_object.kind is not source.kind
                or proposed_event.event_type is not EventType.ACTION_PROPOSED
                or not isinstance(proposed_event.payload, ActionProposedPayload)
                or proposed_event.payload.action_ref != source.action_ref
                or proposed_event.payload.branch_id != source.branch_id
                or proposed_event.parent_event_sha256 != source_object.basis_tail_event_sha256
                or source.proposed_event_sha256 != proposed_event.event_sha256
                or authorized_event.event_type is not EventType.ACTION_AUTHORIZED
                or not isinstance(authorized_event.payload, ActionAuthorizedPayload)
                or authorized_event.payload.action_id != source.action_ref.object_id
                or authorized_event.payload.branch_id != source.branch_id
                or authorized_event.parent_event_sha256 != proposed_event.event_sha256
                or source.decided_event_sha256 != authorized_event.event_sha256
                or request.protocol.authored_at < authorized_event.committed_at
                or compilation.registered_at < request.protocol.authored_at
            ):
                raise ActionProposalError("blocked compilation escaped its exact graph source")
            required_kind = ActionKind.REFINE
            source_action_sha256 = projection.action_sha256
            source_receipt_sha256 = result.receipt.receipt_sha256
            required_evidence = (
                EvidenceRef(
                    kind=EvidenceKind.OBJECTION,
                    object_sha256=source_receipt_sha256,
                    object_id=f"compilation:{source_receipt_sha256[:32]}",
                ),
            )
            targets = (
                _downstream_target(
                    state=state,
                    action_sha256=source_action_sha256,
                    required_kind=required_kind,
                ),
            )
        else:
            if (
                projection.action_sha256 is None
                or projection.scientific_slot_id is None
                or not projection.continuation_committed
            ):
                raise ActionProposalError("follow-up request lacks an exact continuation source")
            source = _source_action(state, projection.action_sha256)
            if (
                source.lifecycle is not ActionLifecycle.APPLIED
                or source.observation_evidence_ref is None
                or source.observation_evidence_ref.object_id != projection.scientific_slot_id
            ):
                raise ActionProposalError("follow-up action lacks its incorporated observation")
            row = get_continuation_receipt_by_slot(
                session,
                quest_id=projection.quest_id,
                scientific_slot_id=projection.scientific_slot_id,
            )
            if row is None:
                raise ActionProposalError("continuation receipt is missing")
            receipt = ContinuationReceipt.model_validate(row.receipt_json)
            incorporated = tuple(
                event
                for event in audit.events
                if event.event_type is EventType.OBSERVATION_INCORPORATED
                and isinstance(event.payload, ObservationIncorporatedPayload)
                and event.event_sha256 == source.decided_event_sha256
            )
            if len(incorporated) != 1:
                raise ActionProposalError("follow-up source observation event is not unique")
            incorporated_event = incorporated[0]
            incorporated_payload = incorporated_event.payload
            if (
                row.receipt_sha256 != receipt.receipt_sha256
                or row.action_sha256 != projection.action_sha256
                or row.scientific_slot_id != projection.scientific_slot_id
                or row.world_model_snapshot_sha256 != receipt.world_model_snapshot_sha256
                or row.observation_projection_sha256 != receipt.observation_projection_sha256
                or row.disposition != receipt.disposition.value
                or row.committed_admission_sha256 != source.observation_evidence_ref.object_sha256
                or incorporated_payload.action_id != source.action_ref.object_id
                or incorporated_payload.branch_id != source.branch_id
                or incorporated_payload.scientific_slot_id != projection.scientific_slot_id
                or incorporated_payload.committed_admission_sha256 != row.committed_admission_sha256
                or incorporated_payload.scientific_observation_sha256
                != row.scientific_observation_sha256
                or incorporated_payload.source_world_model_sha256 != row.world_model_snapshot_sha256
                or row.recorded_at < incorporated_event.committed_at
            ):
                raise ActionProposalError("continuation row differs from its canonical receipt")
            receipt_kind = {
                ContinuationDisposition.READY: EvidenceKind.POLICY,
                ContinuationDisposition.REDESIGN_OBSERVABLE: EvidenceKind.OBJECTION,
                ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED: EvidenceKind.CONTRADICTION,
            }[receipt.disposition]
            required_kind = receipt.proposed_action_kind
            source_action_sha256 = projection.action_sha256
            source_receipt_sha256 = receipt.receipt_sha256
            required_evidence = tuple(
                sorted(
                    (
                        source.observation_evidence_ref,
                        EvidenceRef(
                            kind=receipt_kind,
                            object_sha256=receipt.receipt_sha256,
                            object_id=f"continuation:{projection.scientific_slot_id}",
                        ),
                    ),
                    key=_evidence_key,
                )
            )
            targets = (
                _downstream_target(
                    state=state,
                    action_sha256=source_action_sha256,
                    required_kind=required_kind,
                ),
            )

        if not targets:
            raise ActionProposalBlocked(("action_proposal:no_eligible_graph_target",))
        alternatives = tuple(
            sorted((item.action_ref for item in state.actions), key=_action_ref_key)
        )
        return ControllerActionProposalRequest(
            wakeup_sha256=wakeup.wakeup_sha256,
            recovery_projection_sha256=projection.projection_sha256,
            plan_sha256=plan.plan_sha256,
            step=plan.step,
            quest_id=projection.quest_id,
            scope_binding=audit.scope_binding,
            expected_stream_version=projection.audited_stream_version,
            expected_tail_event_sha256=projection.audited_tail_event_sha256,
            expected_snapshot_sha256=projection.audited_snapshot_sha256,
            charter_ref=state.charter_ref,
            targets=targets,
            required_action_kind=required_kind,
            required_evidence_refs=required_evidence,
            allowed_alternative_action_refs=alternatives,
            source_action_sha256=source_action_sha256,
            source_receipt_sha256=source_receipt_sha256,
            latest_event_committed_at=audit.events[-1].committed_at,
        )


class WriteOnceActionProposalSpool(ActionProposalSubmissionStorePort):
    """Private write-once request index with first-writer-wins process-safe publication."""

    def __init__(
        self,
        root: Path,
        *,
        authority_binding: ControllerStepAuthorityBinding,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
        device_id: int | None = None,
        inode: int | None = None,
        directory_mode: int | None = None,
    ) -> None:
        binding = ControllerStepAuthorityBinding.model_validate(
            authority_binding.model_dump(mode="python")
        )
        if (
            binding.role is not ControllerStepAuthorityRole.ACTION_PROPOSAL
            or binding.key_id is not None
        ):
            raise ValueError("action proposal spool requires a powerless proposal authority")
        custody_values = (owner_uid, owner_gid, device_id, inode, directory_mode)
        if any(value is not None for value in custody_values) and any(
            value is None for value in custody_values
        ):
            raise ValueError("action proposal spool custody pin must be complete")
        if directory_mode not in (None, 0o700):
            raise ValueError("action proposal spool custody requires mode 0700")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ActionProposalError("action proposal spool root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = candidate.lstat()
        if (
            candidate.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ActionProposalError("action proposal spool root must be a private directory")
        self.root = candidate.resolve(strict=True)
        self.authority_binding = binding
        self._custody_pin = (
            None if owner_uid is None else (owner_uid, owner_gid, device_id, inode, directory_mode)
        )
        self._verify_root()

    def _verify_root(self) -> None:
        try:
            metadata = self.root.lstat()
            resolved = self.root.resolve(strict=True)
        except OSError as exc:
            raise ActionProposalError("action proposal spool root is unavailable") from exc
        if (
            resolved != self.root
            or self.root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ActionProposalError("action proposal spool root changed custody")
        if (
            self._custody_pin is not None
            and (
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
            )
            != self._custody_pin
        ):
            raise ActionProposalError("action proposal spool root differs from its custody pin")

    def load(self, *, request_sha256: str) -> SubmittedActionProposal | None:
        self._verify_root()
        target = self._path(request_sha256)
        if not self._prepare_parent(target, create=False):
            return None
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            return None
        if target.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ActionProposalError("action proposal spool target is unsafe")
        with self._publication_lock(target, exclusive=False):
            payload = self._read_regular(target)
        return self._verify_archive_binding(
            self._parse(payload, expected_request_sha256=request_sha256)
        )

    def put_once(self, submission: SubmittedActionProposal) -> SubmittedActionProposal:
        self._verify_root()
        submission = self._verify_archive_binding(submission)
        payload = canonical_json_bytes(submission)
        if len(payload) > _MAX_SUBMISSION_BYTES:
            raise ActionProposalError("action proposal submission exceeds the archive bound")
        target = self._path(submission.request.request_sha256)
        self._prepare_parent(target, create=True)
        with self._publication_lock(target, exclusive=True):
            self._recover_interrupted_publication(target)
            try:
                target.lstat()
            except FileNotFoundError:
                self._publish_locked(target=target, payload=payload)
            winner = self._read_regular(target)
        return self._verify_archive_binding(
            self._parse(
                winner,
                expected_request_sha256=submission.request.request_sha256,
            )
        )

    def _verify_archive_binding(
        self, submission: SubmittedActionProposal
    ) -> SubmittedActionProposal:
        try:
            submission = SubmittedActionProposal.model_validate(
                submission.model_dump(mode="python")
            )
            if (
                submission.proposal_authority_binding_sha256
                != self.authority_binding.binding_sha256
                or submission.proposed_by_principal_id != self.authority_binding.principal_id
                or submission.proposal_policy_sha256 != self.authority_binding.policy_sha256
                or submission.proposal_service_manifest_sha256
                != self.authority_binding.service_manifest_sha256
            ):
                raise ValueError("submission belongs to another proposal authority")
            rebuilt = materialize_action_proposal(
                request=submission.request,
                draft=submission.draft,
                authority_binding=self.authority_binding,
                submitted_at=submission.submitted_at,
            )
            if rebuilt != submission:
                raise ValueError("submission differs from canonical materialization")
            return submission
        except ActionProposalError:
            raise
        except Exception as exc:  # noqa: BLE001 - archived bytes fail closed
            raise ActionProposalError("action proposal spool binding failed closed") from exc

    def _path(self, request_sha256: str) -> Path:
        if re.fullmatch(_SHA256_PATTERN, request_sha256) is None:
            raise ActionProposalError("action proposal request identity is not SHA-256")
        target = self.root / "requests" / request_sha256[:2] / f"{request_sha256}.json"
        if self.root not in target.parents:
            raise ActionProposalError("action proposal spool path escaped its root")
        return target

    def _prepare_parent(self, target: Path, *, create: bool) -> bool:
        self._verify_root()
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
                raise ActionProposalError("action proposal spool parent is missing") from exc
            if (
                current.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (
                    self._custody_pin is not None
                    and (metadata.st_uid, metadata.st_gid, metadata.st_dev) != self._custody_pin[:3]
                )
            ):
                raise ActionProposalError("action proposal spool parent chain became unsafe")
        return True

    @contextmanager
    def _publication_lock(self, target: Path, *, exclusive: bool) -> Iterator[None]:
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
            raise ActionProposalError("action proposal spool lock is missing or unsafe") from exc
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (
                    self._custody_pin is not None
                    and (metadata.st_uid, metadata.st_gid, metadata.st_dev) != self._custody_pin[:3]
                )
            ):
                raise ActionProposalError("action proposal spool lock is not private regular data")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _recover_interrupted_publication(self, target: Path) -> None:
        pending = target.with_name(f".{target.name}.pending")
        try:
            pending_metadata = pending.lstat()
        except FileNotFoundError:
            return
        if (
            pending.is_symlink()
            or not stat.S_ISREG(pending_metadata.st_mode)
            or (
                self._custody_pin is not None
                and (pending_metadata.st_uid, pending_metadata.st_gid, pending_metadata.st_dev)
                != self._custody_pin[:3]
            )
        ):
            raise ActionProposalError("action proposal pending publication is unsafe")
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None and (
            target_metadata.st_dev,
            target_metadata.st_ino,
        ) != (pending_metadata.st_dev, pending_metadata.st_ino):
            raise ActionProposalError("action proposal pending publication conflicts with winner")
        try:
            pending.unlink()
        except OSError as exc:
            raise ActionProposalError(
                "action proposal pending publication cannot be recovered"
            ) from exc
        self._fsync_parent(target)

    def _publish_locked(self, *, target: Path, payload: bytes) -> None:
        pending = target.with_name(f".{target.name}.pending")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(pending, flags, 0o400)
        except OSError as exc:
            raise ActionProposalError("action proposal spool refused pending publication") from exc
        complete = False
        try:
            offset = 0
            view = memoryview(payload)
            while offset < len(payload):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise ActionProposalError("action proposal spool write made no progress")
                offset += written
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            complete = True
        finally:
            os.close(descriptor)
            if not complete:
                try:
                    pending.unlink()
                except FileNotFoundError:
                    pass
        try:
            os.link(pending, target, follow_symlinks=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ActionProposalError("action proposal spool refused atomic publication") from exc
        finally:
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
        self._fsync_parent(target)

    @staticmethod
    def _fsync_parent(target: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_regular(self, target: Path) -> bytes:
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ActionProposalError("action proposal submission is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or (
                    self._custody_pin is not None
                    and (before.st_uid, before.st_gid, before.st_dev) != self._custody_pin[:3]
                )
                or before.st_size < 1
                or before.st_size > _MAX_SUBMISSION_BYTES
            ):
                raise ActionProposalError(
                    "action proposal submission is not immutable regular data"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ActionProposalError("action proposal submission ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1) or _stable_stat_identity(os.fstat(descriptor)) != (
                _stable_stat_identity(before)
            ):
                raise ActionProposalError("action proposal submission changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _parse(payload: bytes, *, expected_request_sha256: str) -> SubmittedActionProposal:
        try:
            submission = SubmittedActionProposal.model_validate_json(payload)
        except ValueError as exc:
            raise ActionProposalError("action proposal spool contains invalid bytes") from exc
        if (
            canonical_json_bytes(submission) != payload
            or submission.request.request_sha256 != expected_request_sha256
        ):
            raise ActionProposalError("action proposal spool changed canonical request identity")
        return submission


ServiceClock = Callable[[], datetime]


class ActionProposalMaterializationService:
    """External proposal service; it owns no Kernel signer and performs no Kernel mutation."""

    def __init__(
        self,
        *,
        context_source: ActionProposalContextSourcePort,
        provider: ActionProposalDraftProviderPort,
        draft_verifier: ActionProposalDraftVerificationPort,
        submissions: ActionProposalSubmissionStorePort,
        clock: ServiceClock,
    ) -> None:
        if (
            not callable(getattr(context_source, "load_request", None))
            or not callable(getattr(provider, "propose_action", None))
            or not callable(getattr(draft_verifier, "verify_action_proposal_draft", None))
            or not callable(getattr(submissions, "load", None))
            or not callable(getattr(submissions, "put_once", None))
            or not callable(clock)
        ):
            raise TypeError("action proposal service dependencies are invalid")
        binding = ControllerStepAuthorityBinding.model_validate(
            submissions.authority_binding.model_dump(mode="python")
        )
        if (
            binding.role is not ControllerStepAuthorityRole.ACTION_PROPOSAL
            or binding.key_id is not None
            or binding.private_key_loaded_in_worker
        ):
            raise ValueError("action proposal service requires a powerless proposal authority")
        self._context_source = context_source
        self._provider = provider
        self._draft_verifier = draft_verifier
        self._submissions = submissions
        self._clock = clock
        self.authority_binding = binding

    def materialize_and_submit(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> SubmittedActionProposal:
        try:
            _require_exact_tick(wakeup=wakeup, projection=projection, plan=plan)
            request = self._context_source.load_request(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
            )
            if (
                request.wakeup_sha256 != wakeup.wakeup_sha256
                or request.recovery_projection_sha256 != projection.projection_sha256
                or request.plan_sha256 != plan.plan_sha256
            ):
                raise ActionProposalError("action proposal context source changed the exact tick")
            existing = self._submissions.load(request_sha256=request.request_sha256)
            if existing is None:
                draft = self._draft_verifier.verify_action_proposal_draft(
                    request=request,
                    draft=self._provider.propose_action(request),
                )
                candidate = materialize_action_proposal(
                    request=request,
                    draft=draft,
                    authority_binding=self.authority_binding,
                    submitted_at=self._clock(),
                )
                existing = self._submissions.put_once(candidate)
            verified_draft = self._draft_verifier.verify_action_proposal_draft(
                request=request,
                draft=existing.draft,
            )
            if verified_draft != existing.draft:
                raise ActionProposalError(
                    "action proposal draft verifier changed the durable submission"
                )
            return verify_submitted_action_proposal(
                submission=existing,
                wakeup=wakeup,
                projection=projection,
                plan=plan,
                authority_binding=self.authority_binding,
            )
        except (ActionProposalBlocked, ActionProposalError):
            raise
        except Exception as exc:  # noqa: BLE001 - provider/archive/service failures fail closed
            raise ActionProposalError("action proposal service failed closed") from exc


__all__ = [
    "ActionProposalMaterializationService",
    "PostgreSQLActionProposalContextSource",
    "ServiceClock",
    "WriteOnceActionProposalSpool",
]
