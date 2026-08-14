"""Evaluator-owned attempt orchestration for the F7 Frontier Gate."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from aletheia.evals.boundary import EvaluationBoundary, EvaluationBoundaryError
from aletheia.evals.ledger import EvaluationLedger, EvaluationLedgerError
from aletheia.evals.sandbox import (
    EvaluationExecution,
    EvaluationExecutionContext,
    EvaluationExecutor,
    EvaluationExecutorError,
    seal_research_workspace,
    stage_attempt_directories,
)
from aletheia.evals.schemas import (
    AttemptStatus,
    EvaluationAttempt,
    EvaluationAttemptManifest,
    EvaluationAttemptSlot,
    EvaluationExecutionReceipt,
    EvaluationPublicAsset,
    EvaluationResearchRequest,
    EvaluationRunPlan,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    ExecutionExitReason,
    InvalidReason,
    ScorerReceipt,
    SignedScorerReceipt,
)


class EvaluationRunnerError(RuntimeError):
    pass


class EvaluationProtocolError(EvaluationRunnerError):
    def __init__(self, message: str, reason: InvalidReason = InvalidReason.PROTOCOL_BREACH):
        super().__init__(message)
        self.reason = reason


class EvaluationScorer(Protocol):
    @property
    def scorer_sha256(self) -> str: ...

    def score(
        self,
        *,
        task: EvaluationTask,
        hidden_asset: bytes,
        submission: EvaluationSubmission,
        artifacts: dict[str, bytes],
    ) -> EvaluationScore: ...


class EvaluationScorerInfrastructureError(RuntimeError):
    """Only this explicit trusted failure class authorizes a scorer-side retry."""


@dataclass(frozen=True)
class EvaluationOutcome:
    attempt: EvaluationAttempt
    attempt_manifest: EvaluationAttemptManifest
    execution_receipt: EvaluationExecutionReceipt | None
    submission: EvaluationSubmission | None
    scorer_receipt: SignedScorerReceipt | None
    research_workspace: Path
    submission_inbox: Path
    evaluator_attempt_workspace: Path
    detail: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class IndependentEvaluationRunner:
    """Run one pre-registered slot without exposing evaluator assets to research code."""

    def __init__(
        self,
        *,
        root: Path,
        ledger: EvaluationLedger,
        executor: EvaluationExecutor,
        scorer: EvaluationScorer,
        evaluator_manifest_sha256: str,
        receipt_key_id: str,
        receipt_signing_key: bytes,
        formal: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.ledger = ledger
        self.executor = executor
        self.scorer = scorer
        self.evaluator_manifest_sha256 = evaluator_manifest_sha256
        self.receipt_key_id = receipt_key_id
        self.receipt_signing_key = receipt_signing_key
        self.formal = formal
        if len(receipt_signing_key) < 32:
            raise ValueError("evaluator receipt signing keys must contain at least 32 bytes")
        if self.ledger.path == self.root or not _is_within(self.ledger.path, self.root):
            raise ValueError("evaluation ledger must live under the evaluator root")

    def _assert_contract(self, task: EvaluationTask) -> None:
        contract = self.executor.contract
        if self.formal and contract.security_level != "hard":
            raise EvaluationRunnerError("formal evaluation requires a hard executor boundary")
        if contract.network_mode != "none":
            raise EvaluationRunnerError("formal evaluation networking must be disabled")
        missing_tools = set(task.allowed_tools) - set(contract.exposed_tools)
        undeclared_tools = set(contract.exposed_tools) - set(task.allowed_tools)
        if missing_tools or undeclared_tools:
            raise EvaluationRunnerError(
                "executor tool capabilities must exactly match the frozen task contract; "
                f"missing={sorted(missing_tools)}, undeclared={sorted(undeclared_tools)}"
            )
        if task.resource_budget.gpu_seconds > 0 and not contract.gpu_enabled:
            raise EvaluationRunnerError("task requests GPU time but executor has no GPU boundary")
        if task.resource_budget.gpu_seconds == 0 and contract.gpu_enabled:
            raise EvaluationRunnerError("GPU access was not declared by the task")
        metering_required = (
            task.resource_budget.token_cap is not None or task.resource_budget.usd_cap is not None
        )
        if metering_required and contract.usage_metering == "unavailable":
            raise EvaluationRunnerError(
                "token/USD budgets require a trusted executor or provider usage receipt"
            )

    @staticmethod
    def _slot(plan: EvaluationRunPlan, task: EvaluationTask, repeat_index: int) -> EvaluationAttemptSlot:
        matches = [
            slot
            for slot in plan.slots
            if slot.task_manifest_sha256 == task.manifest_sha256
            and slot.repeat_index == repeat_index
        ]
        if len(matches) != 1:
            raise EvaluationRunnerError(
                "attempt is not a unique pre-registered task/repeat slot in the run plan"
            )
        return matches[0]

    def _validate_identities(
        self,
        *,
        suite: EvaluationSuite,
        plan: EvaluationRunPlan,
        task: EvaluationTask,
    ) -> None:
        if plan.suite_manifest_sha256 != suite.manifest_sha256:
            raise EvaluationRunnerError("run plan is not bound to this suite")
        if plan.evaluator_manifest_sha256 != self.evaluator_manifest_sha256:
            raise EvaluationRunnerError("run plan is not bound to this evaluator manifest")
        if task.manifest_sha256 not in suite.task_manifest_sha256s:
            raise EvaluationRunnerError("task is not a member of the frozen suite")
        if any(
            slot.task_manifest_sha256 not in suite.task_manifest_sha256s for slot in plan.slots
        ):
            raise EvaluationRunnerError("run plan contains a task outside the frozen suite")
        plan_accesses: dict[str, int] = {}
        for slot in plan.slots:
            plan_accesses[slot.task_manifest_sha256] = (
                plan_accesses.get(slot.task_manifest_sha256, 0) + 1
            )
        if plan_accesses.get(task.manifest_sha256, 0) > task.contamination_policy.test_access_limit:
            raise EvaluationRunnerError(
                "run plan exceeds the task's frozen hidden-test access limit"
            )
        if self.scorer.scorer_sha256 != task.scorer_sha256:
            raise EvaluationRunnerError("loaded scorer hash does not match the frozen task")

    def _new_attempt(
        self,
        *,
        plan: EvaluationRunPlan,
        task: EvaluationTask,
        slot: EvaluationAttemptSlot,
        retry_of: str | None,
        intervention_count: int,
    ) -> EvaluationAttempt:
        return EvaluationAttempt(
            attempt_id=f"att-{uuid.uuid4().hex}",
            suite_manifest_sha256=plan.suite_manifest_sha256,
            run_plan_sha256=plan.manifest_sha256,
            task_manifest_sha256=task.manifest_sha256,
            system_manifest_sha256=plan.system_manifest_sha256,
            repeat_index=slot.repeat_index,
            seed=slot.seed,
            intervention_count=intervention_count,
            retry_of_attempt_id=retry_of,
            retry_reason="infra_failure" if retry_of else None,
        )

    def _attempt_paths(
        self, attempt_id: str
    ) -> tuple[Path, Path, Path, Path, EvaluationBoundary]:
        research, inbox = stage_attempt_directories(self.root, attempt_id)
        evaluator_workspace = self.root / "evaluator_attempts" / attempt_id
        public_inputs = self.root / "public_inputs" / attempt_id
        hidden_root = self.root / "hidden_assets"
        public_assets_root = self.root / "public_assets"
        evaluator_workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        public_inputs.mkdir(parents=True, exist_ok=False, mode=0o700)
        hidden_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        public_assets_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        boundary = EvaluationBoundary(
            research_workspace=research,
            submission_inbox=inbox,
            evaluator_workspace=evaluator_workspace,
            hidden_assets_root=hidden_root,
            public_inputs_root=public_inputs,
            public_assets_root=public_assets_root,
        )
        return research, inbox, evaluator_workspace, public_inputs, boundary

    @staticmethod
    def _safe_extract_public_archive(
        data: bytes,
        *,
        destination: Path,
        expected_files: int,
        expected_bytes: int,
    ) -> None:
        """Expand a regular-file-only tarball without trusting tar paths or link metadata."""

        destination.mkdir(parents=True, exist_ok=False, mode=0o755)
        seen: set[PurePosixPath] = set()
        files = 0
        expanded_bytes = 0
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        except (tarfile.TarError, OSError) as exc:
            raise EvaluationRunnerError(f"public task asset is not a valid tar.gz: {exc}") from exc
        try:
            members = archive.getmembers()
            if len(members) > max(10_000, expected_files * 4 + 32):
                raise EvaluationRunnerError("public task archive contains too many entries")
            for member in members:
                if "\\" in member.name:
                    raise EvaluationRunnerError("public task archive uses a non-portable path")
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or not relative.parts or any(
                    part in {"", ".", ".."} for part in relative.parts
                ):
                    raise EvaluationRunnerError("public task archive contains an unsafe path")
                if relative in seen:
                    raise EvaluationRunnerError("public task archive contains duplicate paths")
                seen.add(relative)
                if not (member.isdir() or member.isreg()):
                    raise EvaluationRunnerError(
                        "public task archives may contain only directories and regular files"
                    )
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if member.isdir():
                    target.mkdir(exist_ok=True, mode=0o755)
                    continue
                files += 1
                expanded_bytes += member.size
                if files > expected_files or expanded_bytes > expected_bytes:
                    raise EvaluationRunnerError("public task archive exceeds its frozen expansion")
                source = archive.extractfile(member)
                if source is None:
                    raise EvaluationRunnerError("public task archive file has no readable payload")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, 0o755 if member.mode & 0o111 else 0o644)
                written = 0
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        while block := source.read(1024 * 1024):
                            written += len(block)
                            if written > member.size:
                                raise EvaluationRunnerError(
                                    "public task archive member exceeds its declared size"
                                )
                            handle.write(block)
                finally:
                    source.close()
                if written != member.size:
                    raise EvaluationRunnerError(
                        "public task archive member is shorter than its declared size"
                    )
        finally:
            archive.close()
        if files != expected_files or expanded_bytes != expected_bytes:
            raise EvaluationRunnerError("public task archive expansion differs from its manifest")

    def _stage_public_assets(
        self, *, task: EvaluationTask, boundary: EvaluationBoundary
    ) -> dict[str, str]:
        staged: dict[str, str] = {}
        prefix = "evaluator://public/"
        for asset in task.public_assets:
            relative = PurePosixPath(asset.evaluator_ref[len(prefix) :])
            assert boundary.public_assets_root is not None
            path = boundary.assert_public_asset_read(
                boundary.public_assets_root.joinpath(*relative.parts)
            )
            try:
                data = self._read_regular_file(path, root=boundary.public_assets_root, max_bytes=asset.bytes)
            except (FileNotFoundError, EvaluationProtocolError, EvaluationBoundaryError) as exc:
                raise EvaluationRunnerError(f"public evaluator asset is unavailable: {exc}") from exc
            if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
                raise EvaluationRunnerError("public evaluator asset differs from the frozen task")
            destination = boundary.assert_research_write(
                boundary.research_workspace.joinpath(*PurePosixPath(asset.mount_path).parts)
            )
            self._safe_extract_public_archive(
                data,
                destination=destination,
                expected_files=asset.file_count,
                expected_bytes=asset.expanded_bytes,
            )
            staged[asset.asset_id] = asset.sha256
        return staged

    @staticmethod
    def stage_public_asset(
        *, evaluator_root: Path, asset: EvaluationPublicAsset, source: Path
    ) -> Path:
        """Atomically place exact public bytes in evaluator storage for later attempts."""

        asset = EvaluationPublicAsset.model_validate(asset)
        source = Path(source).resolve(strict=True)
        if source.is_symlink() or not source.is_file():
            raise ValueError("public evaluator assets must be regular non-symlink files")
        if source.stat().st_size != asset.bytes:
            raise ValueError("public evaluator asset byte count differs from its manifest")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != asset.sha256:
            raise ValueError("public evaluator asset hash differs from its manifest")
        prefix = "evaluator://public/"
        relative = PurePosixPath(asset.evaluator_ref[len(prefix) :])
        target = Path(evaluator_root) / "public_assets" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            existing = hashlib.sha256(target.read_bytes()).hexdigest()
            if target.is_symlink() or existing != asset.sha256:
                raise ValueError("an existing public evaluator asset has different bytes")
            return target
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output:
                while block := input_handle.read(1024 * 1024):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o400)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return target

    def _transition(
        self,
        attempt: EvaluationAttempt,
        status: AttemptStatus,
        *,
        slot_sha256: str,
        started_at: datetime,
        ended_at: datetime | None = None,
    ) -> EvaluationAttempt:
        next_attempt = attempt.model_copy(
            update={"status": status, "started_at": started_at, "ended_at": ended_at}
        )
        next_attempt = EvaluationAttempt.model_validate(next_attempt.model_dump())
        self.ledger.append_attempt_state(next_attempt, slot_sha256=slot_sha256)
        return next_attempt

    @staticmethod
    def _execution_receipt(
        *,
        attempt: EvaluationAttempt,
        manifest: EvaluationAttemptManifest,
        execution: EvaluationExecution,
    ) -> EvaluationExecutionReceipt:
        output_hash = hashlib.sha256(execution.output).hexdigest()
        contract = manifest.executor
        return EvaluationExecutionReceipt(
            attempt_id=attempt.attempt_id,
            attempt_manifest_sha256=manifest.manifest_sha256,
            executor_manifest_sha256=contract.manifest_sha256,
            sandbox_image_id=contract.sandbox_image_id,
            resource_budget=manifest.public_task.resource_budget,
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            wall_time_s=execution.wall_time_s,
            returncode=execution.returncode,
            timed_out=execution.timed_out,
            exit_reason=execution.exit_reason,
            stdout_retained_sha256=output_hash,
            stdout_retained_bytes=len(execution.output),
            stdout_total_bytes=execution.output_total_bytes,
            stdout_truncated=execution.output_truncated,
            input_tokens=execution.input_tokens,
            output_tokens=execution.output_tokens,
            cost_usd=execution.cost_usd,
            usage_source=contract.usage_metering,
            infrastructure_detail=execution.infrastructure_detail,
        )

    @staticmethod
    def _artifact_relative_uri(uri: str) -> PurePosixPath:
        prefix = "inbox://"
        if not uri.startswith(prefix):
            raise EvaluationProtocolError("artifact uri must use inbox://", InvalidReason.PROTOCOL_BREACH)
        relative = PurePosixPath(uri[len(prefix) :])
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise EvaluationProtocolError("artifact uri escaped submission inbox")
        return relative

    @staticmethod
    def _read_regular_file(path: Path, *, root: Path, max_bytes: int) -> bytes:
        root = root.resolve(strict=True)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise EvaluationProtocolError("symlink artifacts are forbidden")
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, root) or not resolved.is_file():
            raise EvaluationProtocolError("artifact path escaped submission inbox")
        stat = resolved.stat()
        if stat.st_size > max_bytes:
            raise EvaluationProtocolError("artifact exceeds declared byte limit", InvalidReason.RESOURCE_LIMIT)
        data = resolved.read_bytes()
        if len(data) != stat.st_size:
            raise EvaluationProtocolError("artifact changed while being validated")
        return data

    def _validate_submission(
        self,
        *,
        attempt: EvaluationAttempt,
        task: EvaluationTask,
        inbox: Path,
        boundary: EvaluationBoundary,
    ) -> tuple[EvaluationSubmission, dict[str, bytes]]:
        manifest_path = inbox / "submission.json"
        try:
            boundary.assert_submission_read(manifest_path)
            raw_manifest = self._read_regular_file(manifest_path, root=inbox, max_bytes=1_048_576)
        except (FileNotFoundError, EvaluationBoundaryError) as exc:
            raise EvaluationProtocolError(
                "required submission.json is missing", InvalidReason.MISSING_ARTIFACT
            ) from exc
        try:
            submission = EvaluationSubmission.model_validate_json(raw_manifest)
        except ValidationError as exc:
            raise EvaluationProtocolError(f"submission manifest is invalid: {exc}") from exc
        if submission.attempt_id != attempt.attempt_id:
            raise EvaluationProtocolError("submission attempt identity does not match")
        if submission.task_manifest_sha256 != task.manifest_sha256:
            raise EvaluationProtocolError("submission task identity does not match")
        if submission.system_manifest_sha256 != attempt.system_manifest_sha256:
            raise EvaluationProtocolError("submission system identity does not match")
        requirements = {item.kind: item for item in task.expected_artifacts}
        submitted = {item.kind: item for item in submission.artifacts}
        unknown = set(submitted) - set(requirements)
        missing = {kind for kind, requirement in requirements.items() if requirement.required} - set(
            submitted
        )
        if unknown:
            raise EvaluationProtocolError(f"submission contains undeclared artifact kinds: {sorted(unknown)}")
        if missing:
            raise EvaluationProtocolError(
                f"submission is missing required artifact kinds: {sorted(missing)}",
                InvalidReason.MISSING_ARTIFACT,
            )
        payloads: dict[str, bytes] = {}
        seen_paths: set[Path] = set()
        for artifact in submission.artifacts:
            requirement = requirements[artifact.kind]
            if artifact.media_type != requirement.media_type:
                raise EvaluationProtocolError(
                    f"artifact {artifact.kind!r} media type does not match its contract"
                )
            relative = self._artifact_relative_uri(artifact.uri)
            path = inbox.joinpath(*relative.parts)
            resolved = path.resolve(strict=False)
            if resolved in seen_paths:
                raise EvaluationProtocolError("multiple artifacts cannot alias one file")
            seen_paths.add(resolved)
            try:
                data = self._read_regular_file(path, root=inbox, max_bytes=requirement.max_bytes)
            except FileNotFoundError as exc:
                raise EvaluationProtocolError(
                    f"submitted artifact {artifact.kind!r} is missing",
                    InvalidReason.MISSING_ARTIFACT,
                ) from exc
            if len(data) != artifact.bytes:
                raise EvaluationProtocolError("submitted artifact byte count does not match")
            if hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise EvaluationProtocolError("submitted artifact hash does not match bytes")
            payloads[artifact.kind] = data
        return submission, payloads

    def _hidden_asset(self, task: EvaluationTask, boundary: EvaluationBoundary) -> bytes:
        prefix = "evaluator://hidden/"
        if not task.hidden_asset_ref.startswith(prefix):
            raise EvaluationRunnerError("hidden asset ref must use evaluator://hidden/")
        relative = PurePosixPath(task.hidden_asset_ref[len(prefix) :])
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise EvaluationRunnerError("hidden asset ref escaped evaluator storage")
        path = boundary.hidden_assets_root.joinpath(*relative.parts)
        try:
            data = self._read_regular_file(path, root=boundary.hidden_assets_root, max_bytes=1 << 30)
        except (FileNotFoundError, EvaluationProtocolError) as exc:
            raise EvaluationRunnerError(f"hidden evaluator asset is unavailable: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != task.hidden_asset_sha256:
            raise EvaluationRunnerError("hidden evaluator asset hash does not match frozen task")
        return data

    def run(
        self,
        *,
        suite: EvaluationSuite,
        plan: EvaluationRunPlan,
        task: EvaluationTask,
        repeat_index: int,
        retry_of_attempt_id: str | None = None,
        intervention_count: int = 0,
    ) -> EvaluationOutcome:
        self._validate_identities(suite=suite, plan=plan, task=task)
        self._assert_contract(task)
        slot = self._slot(plan, task, repeat_index)
        self.ledger.register_plan(plan)
        attempt = self._new_attempt(
            plan=plan,
            task=task,
            slot=slot,
            retry_of=retry_of_attempt_id,
            intervention_count=intervention_count,
        )
        try:
            self.ledger.claim_attempt(
                attempt,
                slot_sha256=slot.slot_sha256,
                retry_of_attempt_id=retry_of_attempt_id,
                max_infra_retries=plan.max_infra_retries_per_slot,
            )
        except EvaluationLedgerError as exc:
            raise EvaluationRunnerError(str(exc)) from exc
        if retry_of_attempt_id is not None:
            self.ledger.append(
                "retry_authorized",
                {
                    "plan_sha256": plan.manifest_sha256,
                    "slot_sha256": slot.slot_sha256,
                    "retry_of_attempt_id": retry_of_attempt_id,
                    "reason": "infra_failure",
                },
                attempt_id=attempt.attempt_id,
            )
        research, inbox, evaluator_workspace, public_inputs, boundary = self._attempt_paths(
            attempt.attempt_id
        )
        manifest = EvaluationAttemptManifest(
            attempt=attempt,
            public_task=task.public_view(),
            executor=self.executor.contract,
            evaluator_manifest_sha256=self.evaluator_manifest_sha256,
            frozen_at=_utcnow(),
        )
        request = EvaluationResearchRequest(
            attempt_id=attempt.attempt_id,
            run_plan_sha256=plan.manifest_sha256,
            system_manifest_sha256=plan.system_manifest_sha256,
            repeat_index=slot.repeat_index,
            seed=slot.seed,
            public_task=task.public_view(),
        )
        request_path = boundary.assert_research_read(public_inputs / "request.json")
        _atomic_json(request_path, request, mode=0o444)
        seal_research_workspace(public_inputs)
        _atomic_json(evaluator_workspace / "attempt_manifest.v1.json", manifest)
        self.ledger.append(
            "attempt_manifest_frozen",
            {
                "attempt_manifest_sha256": manifest.manifest_sha256,
                "executor_manifest_sha256": manifest.executor.manifest_sha256,
                "public_request_sha256": request.request_sha256,
            },
            attempt_id=attempt.attempt_id,
        )
        started_at = _utcnow()
        try:
            staged_public_assets = self._stage_public_assets(task=task, boundary=boundary)
        except EvaluationRunnerError as exc:
            attempt = self._transition(
                attempt,
                AttemptStatus.INFRA_FAILURE,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=_utcnow(),
            )
            _atomic_json(
                evaluator_workspace / "infrastructure_failure.json",
                {"classification": "infra_failure", "detail": str(exc)[:1024]},
            )
            seal_research_workspace(research)
            seal_research_workspace(inbox)
            return EvaluationOutcome(
                attempt,
                manifest,
                None,
                None,
                None,
                research,
                inbox,
                evaluator_workspace,
                str(exc),
            )
        if staged_public_assets:
            self.ledger.append(
                "public_assets_staged",
                {
                    "assets": staged_public_assets,
                    "public_task_manifest_sha256": task.manifest_sha256,
                },
                attempt_id=attempt.attempt_id,
            )
        attempt = self._transition(
            attempt, AttemptStatus.RUNNING, slot_sha256=slot.slot_sha256, started_at=started_at
        )
        context = EvaluationExecutionContext(
            request=request,
            research_workspace=research,
            submission_inbox=inbox,
            request_path=request_path,
        )
        try:
            execution = self.executor.execute(context, task.resource_budget)
        except EvaluationExecutorError as exc:
            ended_at = _utcnow()
            attempt = self._transition(
                attempt,
                AttemptStatus.INFRA_FAILURE,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=ended_at,
            )
            _atomic_json(
                evaluator_workspace / "infrastructure_failure.json",
                {"classification": "infra_failure", "detail": str(exc)[:1024]},
            )
            seal_research_workspace(research)
            seal_research_workspace(inbox)
            return EvaluationOutcome(
                attempt,
                manifest,
                None,
                None,
                None,
                research,
                inbox,
                evaluator_workspace,
                str(exc),
            )
        if execution.ended_at < execution.started_at:
            raise EvaluationRunnerError("executor returned an invalid timestamp interval")
        measured_wall_time = max(
            0.0, (execution.ended_at - execution.started_at).total_seconds()
        )
        wall_slack = max(0.25, task.resource_budget.wall_time_s * 0.05)
        if execution.wall_time_s + wall_slack < measured_wall_time:
            raise EvaluationRunnerError("executor wall-time receipt understates its timestamps")
        if (
            execution.exit_reason is ExecutionExitReason.COMPLETED
            and max(execution.wall_time_s, measured_wall_time)
            > task.resource_budget.wall_time_s + wall_slack
        ):
            execution = EvaluationExecution(
                returncode=execution.returncode,
                output=execution.output,
                output_total_bytes=execution.output_total_bytes,
                output_truncated=execution.output_truncated,
                started_at=execution.started_at,
                ended_at=execution.ended_at,
                wall_time_s=max(execution.wall_time_s, measured_wall_time),
                exit_reason=ExecutionExitReason.WALL_TIME_LIMIT,
                timed_out=True,
                input_tokens=execution.input_tokens,
                output_tokens=execution.output_tokens,
                cost_usd=execution.cost_usd,
                infrastructure_detail="executor exceeded the frozen wall-time budget",
                container_name=execution.container_name,
            )
        receipt = self._execution_receipt(
            attempt=attempt, manifest=manifest, execution=execution
        )
        seal_research_workspace(research)
        seal_research_workspace(inbox)
        _atomic_json(evaluator_workspace / "execution_receipt.v1.json", receipt)
        self.ledger.append(
            "execution_receipt_issued",
            {
                "execution_receipt_sha256": receipt.receipt_sha256,
                "exit_reason": receipt.exit_reason.value,
                "returncode": receipt.returncode,
                "wall_time_s": receipt.wall_time_s,
                "cost_usd": receipt.cost_usd,
                "input_tokens": receipt.input_tokens,
                "output_tokens": receipt.output_tokens,
            },
            attempt_id=attempt.attempt_id,
        )
        if receipt.exit_reason is ExecutionExitReason.INFRA_FAILURE:
            terminal = AttemptStatus.INFRA_FAILURE
        elif receipt.exit_reason is ExecutionExitReason.WALL_TIME_LIMIT:
            terminal = AttemptStatus.TIMEOUT
        elif receipt.exit_reason in {
            ExecutionExitReason.RESOURCE_LIMIT,
            ExecutionExitReason.PROCESS_ERROR,
        }:
            terminal = AttemptStatus.INVALID
        else:
            terminal = None
        if terminal is not None:
            attempt = self._transition(
                attempt,
                terminal,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=execution.ended_at,
            )
            return EvaluationOutcome(
                attempt,
                manifest,
                receipt,
                None,
                None,
                research,
                inbox,
                evaluator_workspace,
                receipt.infrastructure_detail or receipt.exit_reason.value,
            )
        try:
            submission, artifact_payloads = self._validate_submission(
                attempt=attempt, task=task, inbox=inbox, boundary=boundary
            )
        except EvaluationProtocolError as exc:
            attempt = self._transition(
                attempt,
                AttemptStatus.INVALID,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=_utcnow(),
            )
            _atomic_json(
                evaluator_workspace / "protocol_failure.json",
                {"invalid_reason": exc.reason.value, "detail": str(exc)[:1024]},
            )
            return EvaluationOutcome(
                attempt,
                manifest,
                receipt,
                None,
                None,
                research,
                inbox,
                evaluator_workspace,
                str(exc),
            )
        attempt = self._transition(
            attempt, AttemptStatus.SUBMITTED, slot_sha256=slot.slot_sha256, started_at=started_at
        )
        self.ledger.append(
            "submission_accepted",
            {
                "submission_sha256": submission.submission_sha256,
                "artifact_sha256s": {
                    artifact.kind: artifact.sha256 for artifact in submission.artifacts
                },
                "declared_contamination": list(submission.declared_contamination),
            },
            attempt_id=attempt.attempt_id,
        )
        budget_invalid = False
        if task.resource_budget.token_cap is not None:
            used_tokens = (receipt.input_tokens or 0) + (receipt.output_tokens or 0)
            budget_invalid = used_tokens > task.resource_budget.token_cap
        if (
            task.resource_budget.usd_cap is not None
            and receipt.cost_usd is not None
            and receipt.cost_usd > task.resource_budget.usd_cap
        ):
            budget_invalid = True
        if budget_invalid:
            # A trusted usage overage is already terminal.  Do not consume hidden-test access or
            # execute an expensive scorer after the attempt has exceeded its frozen budget.
            score = EvaluationScore(invalid_reasons=(InvalidReason.RESOURCE_LIMIT,))
        else:
            score = None
        try:
            if score is None:
                hidden = self._hidden_asset(task, boundary)
                score = self.scorer.score(
                    task=task,
                    hidden_asset=hidden,
                    submission=submission,
                    artifacts=artifact_payloads,
                )
                score = EvaluationScore.model_validate(score)
        except EvaluationScorerInfrastructureError as exc:
            attempt = self._transition(
                attempt,
                AttemptStatus.INFRA_FAILURE,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=_utcnow(),
            )
            _atomic_json(
                evaluator_workspace / "scorer_failure.json",
                {"classification": "infra_failure", "detail": str(exc)[:1024]},
            )
            return EvaluationOutcome(
                attempt,
                manifest,
                receipt,
                submission,
                None,
                research,
                inbox,
                evaluator_workspace,
                str(exc),
            )
        except Exception as exc:
            attempt = self._transition(
                attempt,
                AttemptStatus.INVALID,
                slot_sha256=slot.slot_sha256,
                started_at=started_at,
                ended_at=_utcnow(),
            )
            _atomic_json(
                evaluator_workspace / "scorer_failure.json",
                {"invalid_reason": InvalidReason.SCORER_FAILURE.value, "detail": str(exc)[:1024]},
            )
            return EvaluationOutcome(
                attempt,
                manifest,
                receipt,
                submission,
                None,
                research,
                inbox,
                evaluator_workspace,
                str(exc),
            )
        scorer_receipt = ScorerReceipt(
            attempt_id=attempt.attempt_id,
            run_plan_sha256=plan.manifest_sha256,
            attempt_manifest_sha256=manifest.manifest_sha256,
            task_manifest_sha256=task.manifest_sha256,
            system_manifest_sha256=plan.system_manifest_sha256,
            submission_sha256=submission.submission_sha256,
            execution_receipt_sha256=receipt.receipt_sha256,
            scorer_sha256=self.scorer.scorer_sha256,
            evaluator_manifest_sha256=self.evaluator_manifest_sha256,
            score=score,
            scored_at=_utcnow(),
        )
        scorer_receipt.verify_submission(submission)
        scorer_receipt.verify_attempt(attempt)
        signed = SignedScorerReceipt.issue(
            scorer_receipt, key_id=self.receipt_key_id, key=self.receipt_signing_key
        )
        signed.verify(key=self.receipt_signing_key, expected_key_id=self.receipt_key_id)
        _atomic_json(evaluator_workspace / "scorer_receipt.signed.v1.json", signed)
        self.ledger.append(
            "score_receipt_issued",
            {
                "scorer_receipt_sha256": scorer_receipt.receipt_sha256,
                "signed_envelope_sha256": signed.envelope_sha256,
                "score": score.model_dump(mode="json", exclude_none=True),
            },
            attempt_id=attempt.attempt_id,
        )
        for evidence_name, evidence_object in sorted(score.evidence_objects.items()):
            self.ledger.append(
                "score_evidence_recorded",
                {
                    "scorer_receipt_sha256": scorer_receipt.receipt_sha256,
                    "evidence_name": evidence_name,
                    "evidence_sha256": score.evidence_sha256s[evidence_name],
                    "evidence": evidence_object,
                },
                attempt_id=attempt.attempt_id,
            )
        if score.invalid_reasons:
            terminal = AttemptStatus.INVALID
        elif score.scientific_success is True:
            terminal = AttemptStatus.COMPLETED
        elif score.scientific_success is False:
            terminal = AttemptStatus.SCIENTIFIC_FAILURE
        else:
            # A pending adjudication is not a final scientific result.  Retain it as invalid
            # until F7-S6 introduces an explicit adjudication state machine.
            terminal = AttemptStatus.INVALID
        attempt = self._transition(
            attempt,
            terminal,
            slot_sha256=slot.slot_sha256,
            started_at=started_at,
            ended_at=_utcnow(),
        )
        return EvaluationOutcome(
            attempt,
            manifest,
            receipt,
            submission,
            signed,
            research,
            inbox,
            evaluator_workspace,
        )
