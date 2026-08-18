"""Restart-safe run-once controller for the durable research endurance gate.

The endurance ledger already owns scientific truth and the PostgreSQL clock.  This controller adds
the operational layer needed for a real multi-day run: one advisory-lock owner per tick, stable
idempotency keys, database-clock due decisions, and a write-once evidence spool that survives a
crash between the database commit and local archival.

Production deployment should invoke ``run_controller_tick`` repeatedly from a supervisor.  The
controller never finalizes a gate automatically; efficiency assessment and terminal review remain
independent actions.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text

from aletheia.db import REPO_ROOT, engine
from aletheia.programs.endurance import (
    ResearchEnduranceConflict,
    ResearchEnduranceNotFound,
    ResearchEnduranceStore,
    prepare_endurance_gate_manifest,
)
from aletheia.programs.endurance_schemas import (
    EnduranceCheckpointEvidence,
    EnduranceCommandContext,
    EnduranceEvidenceClass,
    EnduranceGateManifest,
    EnduranceGateSnapshot,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_ENVELOPE_ID_PATTERN = r"^edev_[0-9a-f]{32}$"
_TICK_ID_PATTERN = r"^edt_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_CODE_COMPONENTS = (
    "aletheia/domains/materials/phonon_endurance_portfolio.py",
    "aletheia/jobs/outbox.py",
    "aletheia/jobs/persistence.py",
    "aletheia/programs/__init__.py",
    "aletheia/programs/endurance.py",
    "aletheia/programs/endurance_controller.py",
    "aletheia/programs/endurance_fault_evidence.py",
    "aletheia/programs/endurance_schemas.py",
    "aletheia/programs/endurance_supervisor.py",
    "aletheia/programs/graph.py",
    "aletheia/programs/memory.py",
    "aletheia/programs/memory_schemas.py",
    "aletheia/programs/persistence.py",
    "aletheia/programs/portfolio.py",
    "aletheia/programs/portfolio_harness.py",
    "aletheia/programs/portfolio_schemas.py",
    "scripts/run_endurance_controller.py",
    "scripts/run_endurance_gate.py",
    "scripts/run_endurance_supervisor.py",
    "scripts/run_phonon_endurance_portfolio.py",
    "scripts/submit_endurance_fault_evidence.py",
)


class EnduranceControllerError(RuntimeError):
    """Base operational controller error."""


class EnduranceControllerConflict(EnduranceControllerError):
    """Frozen controller identity, spool state, or gate state conflicts."""


class EnduranceControllerPreflightError(EnduranceControllerError):
    """The real gate must not start because commissioning is incomplete or changed."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnduranceControllerAction(str, Enum):
    STARTED = "started"
    CHECKPOINTED = "checkpointed"
    NOT_DUE = "not_due"
    LOCK_BUSY = "lock_busy"
    RECOVERED_SPOOL = "recovered_spool"
    TERMINAL = "terminal"


class EnduranceControllerCodeIdentity(_FrozenModel):
    git_commit: str = Field(pattern=_GIT_SHA_PATTERN)
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_code_matrix(self) -> "EnduranceControllerCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("endurance controller code-component matrix is incomplete")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in components.values()
        ):
            raise ValueError("controller component hashes must be lowercase SHA-256")
        expected = content_sha256(
            {
                "schema": "aletheia.endurance_controller_code.v1",
                "git_commit": self.git_commit,
                "components": components,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("endurance controller code identity is inconsistent")
        object.__setattr__(self, "component_sha256s", components)
        return self


class EnduranceControllerManifest(_FrozenModel):
    schema_version: Literal[1] = 1
    controller_id: str | None = Field(default=None, pattern=_CONTROLLER_ID_PATTERN)
    controller_key: str = Field(pattern=_IDENTITY_PATTERN)
    gate_manifest: EnduranceGateManifest
    principal: str = Field(min_length=1, max_length=128)
    spool_root: str = Field(min_length=1, max_length=1_024)
    supervisor_poll_seconds: int = Field(ge=5, le=60 * 60)
    code_identity: EnduranceControllerCodeIdentity
    prepared_at: AwareDatetime
    invocation_mode: Literal["supervised_run_once"] = "supervised_run_once"
    automatic_finalization: Literal[False] = False

    @model_validator(mode="after")
    def _safe_schedule_and_identity(self) -> "EnduranceControllerManifest":
        _safe_relative(self.spool_root)
        gate = self.gate_manifest
        if self.supervisor_poll_seconds > gate.checkpoint_interval_seconds // 2:
            raise ValueError("controller polling must be at least twice per checkpoint interval")
        slack = gate.maximum_checkpoint_gap_seconds - gate.checkpoint_interval_seconds
        if 2 * self.supervisor_poll_seconds > slack:
            raise ValueError("controller schedule lacks two-poll margin before maximum gap")
        expected = f"edctl_{self.manifest_sha256[:32]}"
        if self.controller_id is not None and self.controller_id != expected:
            raise ValueError("endurance controller ID differs from its manifest")
        object.__setattr__(self, "controller_id", expected)
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"controller_id"}))


class EnduranceEvidenceEnvelope(_FrozenModel):
    schema_version: Literal[1] = 1
    envelope_id: str | None = Field(default=None, pattern=_ENVELOPE_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    evidence: EnduranceCheckpointEvidence
    producer: str = Field(min_length=1, max_length=128)
    submitted_at: AwareDatetime

    @model_validator(mode="after")
    def _nonempty_content_identity(self) -> "EnduranceEvidenceEnvelope":
        count = (
            len(self.evidence.reproductions)
            + len(self.evidence.interruptions)
            + len(self.evidence.structural_pivots)
        )
        if count == 0:
            raise ValueError("controller evidence envelope cannot be empty")
        expected = f"edev_{self.envelope_sha256[:32]}"
        if self.envelope_id is not None and self.envelope_id != expected:
            raise ValueError("controller evidence envelope ID differs from content")
        object.__setattr__(self, "envelope_id", expected)
        return self

    @property
    def envelope_sha256(self) -> str:
        # Submission metadata is outside the identity so an ambiguous local write can be retried
        # without manufacturing a second scientific envelope.
        return content_sha256(
            {
                "schema": "aletheia.endurance_evidence_envelope.v1",
                "controller_id": self.controller_id,
                "gate_id": self.gate_id,
                "evidence": self.evidence.model_dump(mode="json"),
            }
        )


class EnduranceControllerTick(_FrozenModel):
    schema_version: Literal[1] = 1
    tick_id: str | None = Field(default=None, pattern=_TICK_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    action: EnduranceControllerAction
    database_observed_at: AwareDatetime
    prior_checkpoint_count: int = Field(ge=0)
    pending_envelope_ids_before: tuple[str, ...]
    recovered_envelope_ids: tuple[str, ...]
    checkpoint_envelope_ids: tuple[str, ...]
    previous_tail_sha256: str = Field(pattern=_SHA256_PATTERN)
    previous_tail_observed_at: AwareDatetime
    checkpoint_id: str | None = Field(default=None, pattern=r"^edc_[0-9a-f]{32}$")
    checkpoint_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    command_id: str | None = None
    mutation_created: bool | None = None
    resulting_checkpoint_count: int = Field(ge=0)
    checkpoint_due_at_before_action: AwareDatetime
    maximum_gap_deadline_before_action: AwareDatetime
    next_checkpoint_due_at: AwareDatetime | None = None
    next_maximum_gap_deadline_at: AwareDatetime | None = None
    overdue_before_action: bool
    message: str = Field(min_length=1, max_length=2_048)
    controller_code_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _canonical_and_bound(self) -> "EnduranceControllerTick":
        for field in (
            "pending_envelope_ids_before",
            "recovered_envelope_ids",
            "checkpoint_envelope_ids",
        ):
            values = tuple(sorted(set(getattr(self, field))))
            if values != getattr(self, field):
                raise ValueError("controller tick envelope IDs must be unique and canonical")
        checkpointed = self.action is EnduranceControllerAction.CHECKPOINTED
        has_checkpoint = self.checkpoint_id is not None and self.checkpoint_sha256 is not None
        if checkpointed != has_checkpoint:
            raise ValueError("checkpoint action must bind an exact checkpoint, and only then")
        if checkpointed != (self.command_id is not None and self.mutation_created is not None):
            raise ValueError("checkpoint action must bind its mutation receipt")
        if bool(self.checkpoint_envelope_ids) and not checkpointed:
            raise ValueError("only a checkpoint action can consume pending envelopes")
        if set(self.recovered_envelope_ids).intersection(self.checkpoint_envelope_ids):
            raise ValueError("one envelope cannot be recovered and checkpointed in the same tick")
        if not set(self.recovered_envelope_ids).issubset(self.pending_envelope_ids_before):
            raise ValueError("recovered envelopes were not observed in the pending spool")
        if not set(self.checkpoint_envelope_ids).issubset(self.pending_envelope_ids_before):
            raise ValueError("checkpoint envelopes were not observed in the pending spool")
        if self.resulting_checkpoint_count != self.prior_checkpoint_count + int(checkpointed):
            raise ValueError("controller tick checkpoint count is inconsistent with its action")
        if self.checkpoint_due_at_before_action <= self.previous_tail_observed_at:
            raise ValueError("checkpoint due time must follow the previous ledger tail")
        if self.maximum_gap_deadline_before_action < self.checkpoint_due_at_before_action:
            raise ValueError("maximum-gap deadline cannot precede checkpoint due time")
        expected_overdue = self.database_observed_at > self.maximum_gap_deadline_before_action
        if self.overdue_before_action != expected_overdue:
            raise ValueError("controller overdue verdict differs from database time")
        if (self.next_checkpoint_due_at is None) != (self.next_maximum_gap_deadline_at is None):
            raise ValueError("next checkpoint schedule must be complete or absent")
        if (
            self.next_checkpoint_due_at is not None
            and self.next_maximum_gap_deadline_at is not None
            and self.next_maximum_gap_deadline_at < self.next_checkpoint_due_at
        ):
            raise ValueError("next maximum-gap deadline cannot precede its due time")
        expected = f"edt_{self.tick_sha256[:32]}"
        if self.tick_id is not None and self.tick_id != expected:
            raise ValueError("controller tick ID differs from content")
        object.__setattr__(self, "tick_id", expected)
        return self

    @property
    def tick_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"tick_id"}))


class EnduranceControllerPreflight(_FrozenModel):
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    database_observed_at: AwareDatetime
    eligible_to_start: bool
    blockers: tuple[str, ...]
    pending_envelope_count: int = Field(ge=0)
    code_identity_verified: bool
    gate_sources_verified: bool
    unfinished_gate_count: int = Field(ge=0)
    automatic_finalization: Literal[False] = False

    @model_validator(mode="after")
    def _canonical_verdict(self) -> "EnduranceControllerPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("controller preflight blockers must be canonical")
        expected = not blockers and self.code_identity_verified and self.gate_sources_verified
        if self.eligible_to_start != expected:
            raise ValueError("controller preflight verdict differs from blockers")
        return self


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("controller spool root must be a safe relative path")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(repository_root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def capture_endurance_controller_code_identity(
    *,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> EnduranceControllerCodeIdentity:
    root = repository_root.resolve()
    commit = _git_output(root, "rev-parse", "HEAD")
    components: dict[str, str] = {}
    for relative in _CODE_COMPONENTS:
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - repository-owned constant matrix
            raise EnduranceControllerConflict(
                f"controller code escaped repository: {relative}"
            ) from exc
        components[relative] = _sha256_file(path)
    if require_committed:
        tracked = subprocess.run(
            ("git", "ls-files", "--error-unmatch", *_CODE_COMPONENTS),
            cwd=root,
            capture_output=True,
            text=True,
        )
        unstaged = subprocess.run(
            ("git", "diff", "--quiet", "--", *_CODE_COMPONENTS),
            cwd=root,
        )
        staged = subprocess.run(
            ("git", "diff", "--cached", "--quiet", "--", *_CODE_COMPONENTS),
            cwd=root,
        )
        if tracked.returncode != 0 or unstaged.returncode != 0 or staged.returncode != 0:
            raise EnduranceControllerPreflightError(
                "controller code components must be tracked and committed before preparation"
            )
    aggregate = content_sha256(
        {
            "schema": "aletheia.endurance_controller_code.v1",
            "git_commit": commit,
            "components": dict(sorted(components.items())),
        }
    )
    return EnduranceControllerCodeIdentity(
        git_commit=commit,
        component_sha256s=components,
        aggregate_sha256=aggregate,
    )


def verify_endurance_controller_code_identity(
    identity: EnduranceControllerCodeIdentity,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    live = capture_endurance_controller_code_identity(
        repository_root=repository_root,
        require_committed=False,
    )
    if live != identity:
        raise EnduranceControllerConflict("live controller code differs from frozen identity")


def prepare_endurance_controller_manifest(
    gate_manifest: EnduranceGateManifest,
    *,
    controller_key: str,
    principal: str,
    spool_root: str,
    supervisor_poll_seconds: int = 300,
    prepared_at: datetime | None = None,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> EnduranceControllerManifest:
    gate_manifest = EnduranceGateManifest.model_validate(gate_manifest.model_dump(mode="python"))
    return EnduranceControllerManifest(
        controller_key=controller_key,
        gate_manifest=gate_manifest,
        principal=principal,
        spool_root=spool_root,
        supervisor_poll_seconds=supervisor_poll_seconds,
        code_identity=capture_endurance_controller_code_identity(
            repository_root=repository_root,
            require_committed=require_committed,
        ),
        prepared_at=prepared_at or datetime.now(timezone.utc),
    )


def _spool_paths(
    manifest: EnduranceControllerManifest,
    artifact_root: Path,
) -> tuple[Path, Path, Path, Path]:
    root = artifact_root.resolve()
    relative = _safe_relative(manifest.spool_root)
    spool = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        spool.relative_to(root)
    except ValueError as exc:
        raise EnduranceControllerConflict("controller spool escaped artifact root") from exc
    return spool, spool / "pending", spool / "committed", spool / "receipts"


def prepare_controller_spool(
    manifest: EnduranceControllerManifest,
    *,
    artifact_root: Path = REPO_ROOT,
) -> Path:
    spool, pending, committed, receipts = _spool_paths(manifest, artifact_root)
    for path in (spool, pending, committed, receipts):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return spool


def _write_new(path: Path, payload: bytes, *, allow_identical: bool) -> bool:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if allow_identical and destination.is_file() and destination.read_bytes() == payload:
            return False
        raise EnduranceControllerConflict(f"refusing to replace controller evidence: {destination}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("controller evidence write made no progress")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if allow_identical and destination.read_bytes() == payload:
                return False
            raise EnduranceControllerConflict(
                f"concurrent controller evidence conflicts: {destination}"
            )
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _model_payload(model: BaseModel) -> bytes:
    return canonical_json_bytes(model) + b"\n"


def submit_controller_evidence(
    manifest: EnduranceControllerManifest,
    evidence: EnduranceCheckpointEvidence,
    *,
    producer: str,
    submitted_at: datetime | None = None,
    artifact_root: Path = REPO_ROOT,
) -> tuple[EnduranceEvidenceEnvelope, bool]:
    manifest = EnduranceControllerManifest.model_validate(manifest.model_dump(mode="python"))
    evidence = EnduranceCheckpointEvidence.model_validate(evidence.model_dump(mode="python"))
    assert manifest.controller_id is not None
    assert manifest.gate_manifest.gate_id is not None
    envelope = EnduranceEvidenceEnvelope(
        controller_id=manifest.controller_id,
        gate_id=manifest.gate_manifest.gate_id,
        evidence=evidence,
        producer=producer,
        submitted_at=submitted_at or datetime.now(timezone.utc),
    )
    _, pending, committed, _ = _spool_paths(manifest, artifact_root)
    prepare_controller_spool(manifest, artifact_root=artifact_root)
    assert envelope.envelope_id is not None
    pending_path = pending / f"{envelope.envelope_id}.json"
    committed_path = committed / f"{envelope.envelope_id}.json"
    payload = _model_payload(envelope)

    def prior_submission(path: Path) -> EnduranceEvidenceEnvelope | None:
        if not path.exists():
            return None
        try:
            prior = EnduranceEvidenceEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise EnduranceControllerConflict(
                f"existing controller envelope is invalid: {path.name}"
            ) from exc
        if (
            prior.envelope_id != envelope.envelope_id
            or prior.controller_id != envelope.controller_id
            or prior.gate_id != envelope.gate_id
            or prior.evidence != envelope.evidence
        ):
            raise EnduranceControllerConflict("existing controller envelope identity conflicts")
        return prior

    for existing_path in (committed_path, pending_path):
        prior = prior_submission(existing_path)
        if prior is not None:
            return prior, False
    try:
        created = _write_new(pending_path, payload, allow_identical=True)
    except EnduranceControllerConflict:
        # A concurrent identical submit may win the hard-link race after the checks above.
        prior = prior_submission(pending_path)
        if prior is None:
            raise
        return prior, False
    return envelope, created


def _load_pending(
    manifest: EnduranceControllerManifest,
    artifact_root: Path,
) -> tuple[tuple[EnduranceEvidenceEnvelope, Path], ...]:
    _, pending, _, _ = _spool_paths(manifest, artifact_root)
    prepare_controller_spool(manifest, artifact_root=artifact_root)
    unexpected = [
        path
        for path in pending.iterdir()
        if not path.name.startswith(".") and path.suffix != ".json"
    ]
    if unexpected:
        raise EnduranceControllerConflict(
            "controller pending spool contains unexpected files: "
            + ", ".join(sorted(path.name for path in unexpected))
        )
    loaded: list[tuple[EnduranceEvidenceEnvelope, Path]] = []
    for path in sorted(pending.glob("*.json")):
        try:
            envelope = EnduranceEvidenceEnvelope.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise EnduranceControllerConflict(
                f"invalid pending evidence envelope: {path.name}"
            ) from exc
        if (
            envelope.controller_id != manifest.controller_id
            or envelope.gate_id != manifest.gate_manifest.gate_id
            or path.name != f"{envelope.envelope_id}.json"
        ):
            raise EnduranceControllerConflict(f"pending envelope binding changed: {path.name}")
        loaded.append((envelope, path))
    return tuple(loaded)


def _evidence_ids(evidence: EnduranceCheckpointEvidence) -> set[str]:
    return {
        str(item.receipt_id)
        for group in (
            evidence.reproductions,
            evidence.interruptions,
            evidence.structural_pivots,
        )
        for item in group
    }


def _committed_evidence_ids(snapshot: EnduranceGateSnapshot) -> set[str]:
    return {
        str(item.receipt_id)
        for checkpoint in snapshot.checkpoints
        for group in (
            checkpoint.checkpoint.evidence.reproductions,
            checkpoint.checkpoint.evidence.interruptions,
            checkpoint.checkpoint.evidence.structural_pivots,
        )
        for item in group
    }


def _merge_envelopes(
    envelopes: tuple[EnduranceEvidenceEnvelope, ...],
) -> EnduranceCheckpointEvidence:
    reproductions = tuple(
        item for envelope in envelopes for item in envelope.evidence.reproductions
    )
    interruptions = tuple(
        item for envelope in envelopes for item in envelope.evidence.interruptions
    )
    pivots = tuple(item for envelope in envelopes for item in envelope.evidence.structural_pivots)
    return EnduranceCheckpointEvidence(
        reproductions=tuple(sorted(reproductions, key=lambda item: item.receipt_id or "")),
        interruptions=tuple(sorted(interruptions, key=lambda item: item.receipt_id or "")),
        structural_pivots=tuple(sorted(pivots, key=lambda item: item.receipt_id or "")),
    )


def _archive_envelope(
    manifest: EnduranceControllerManifest,
    envelope: EnduranceEvidenceEnvelope,
    source: Path,
    artifact_root: Path,
) -> None:
    _, pending, committed, _ = _spool_paths(manifest, artifact_root)
    assert envelope.envelope_id is not None
    if source.parent.resolve() != pending.resolve():
        raise EnduranceControllerConflict("controller attempted to archive outside pending spool")
    destination = committed / f"{envelope.envelope_id}.json"
    payload = _model_payload(envelope)
    _write_new(destination, payload, allow_identical=True)
    if source.exists():
        if source.read_bytes() != payload:
            raise EnduranceControllerConflict("pending envelope changed before archival")
        source.unlink()
        directory_descriptor = os.open(pending, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def controller_advisory_key(gate_id: str) -> int:
    if (
        not gate_id.startswith("edg_")
        or len(gate_id) != 36
        or any(char not in "0123456789abcdef" for char in gate_id[4:])
    ):
        raise ValueError("controller advisory lock requires an endurance gate ID")
    raw = int(
        content_sha256(
            {
                "schema": "aletheia.endurance_controller_advisory_lock.v1",
                "gate_id": gate_id,
            }
        )[:16],
        16,
    )
    return raw - 2**64 if raw >= 2**63 else raw


def _database_now(connection: Any, injected: datetime | None) -> datetime:
    if injected is not None:
        if injected.tzinfo is None or injected.utcoffset() is None:
            raise ValueError("accelerated controller clock must be timezone-aware")
        return injected
    observed = connection.scalar(select(func.clock_timestamp()))
    if observed is None or observed.tzinfo is None or observed.utcoffset() is None:
        raise EnduranceControllerConflict("PostgreSQL did not return an aware clock")
    return observed


def _tail(snapshot: EnduranceGateSnapshot) -> tuple[datetime, str]:
    if snapshot.checkpoints:
        checkpoint = snapshot.checkpoints[-1].checkpoint
        assert checkpoint.checkpoint_sha256 is not None
        return checkpoint.observation.observed_at, checkpoint.checkpoint_sha256
    return snapshot.started_at, snapshot.manifest.manifest_sha256


def _tick(
    manifest: EnduranceControllerManifest,
    *,
    action: EnduranceControllerAction,
    now: datetime,
    checkpoint_count: int,
    pending_before: tuple[str, ...],
    recovered: tuple[str, ...],
    checkpoint_envelopes: tuple[str, ...],
    tail_sha256: str,
    tail_at: datetime,
    resulting_tail_at: datetime | None = None,
    checkpoint_id: str | None = None,
    checkpoint_sha256: str | None = None,
    command_id: str | None = None,
    mutation_created: bool | None = None,
    message: str,
) -> EnduranceControllerTick:
    gate = manifest.gate_manifest
    assert manifest.controller_id is not None
    assert gate.gate_id is not None
    due_before = tail_at + timedelta(seconds=gate.checkpoint_interval_seconds)
    deadline_before = tail_at + timedelta(seconds=gate.maximum_checkpoint_gap_seconds)
    next_tail = resulting_tail_at or tail_at
    terminal = action is EnduranceControllerAction.TERMINAL
    return EnduranceControllerTick(
        controller_id=manifest.controller_id,
        gate_id=gate.gate_id,
        action=action,
        database_observed_at=now,
        prior_checkpoint_count=checkpoint_count,
        pending_envelope_ids_before=tuple(sorted(pending_before)),
        recovered_envelope_ids=tuple(sorted(recovered)),
        checkpoint_envelope_ids=tuple(sorted(checkpoint_envelopes)),
        previous_tail_sha256=tail_sha256,
        previous_tail_observed_at=tail_at,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        command_id=command_id,
        mutation_created=mutation_created,
        resulting_checkpoint_count=checkpoint_count
        + int(action is EnduranceControllerAction.CHECKPOINTED),
        checkpoint_due_at_before_action=due_before,
        maximum_gap_deadline_before_action=deadline_before,
        next_checkpoint_due_at=(
            None if terminal else next_tail + timedelta(seconds=gate.checkpoint_interval_seconds)
        ),
        next_maximum_gap_deadline_at=(
            None if terminal else next_tail + timedelta(seconds=gate.maximum_checkpoint_gap_seconds)
        ),
        overdue_before_action=now > deadline_before,
        message=message,
        controller_code_sha256=manifest.code_identity.aggregate_sha256,
    )


def _write_tick(
    manifest: EnduranceControllerManifest,
    tick: EnduranceControllerTick,
    artifact_root: Path,
) -> None:
    _, _, _, receipts = _spool_paths(manifest, artifact_root)
    assert tick.tick_id is not None
    _write_new(
        receipts / f"{tick.tick_id}.json",
        _model_payload(tick),
        allow_identical=True,
    )


def _fresh_gate_manifest(manifest: EnduranceControllerManifest) -> EnduranceGateManifest:
    gate = manifest.gate_manifest
    return prepare_endurance_gate_manifest(
        gate_key=gate.gate_key,
        quest_id=gate.quest_id,
        evidence_class=gate.evidence_class,
        required_duration_seconds=gate.required_duration_seconds,
        checkpoint_interval_seconds=gate.checkpoint_interval_seconds,
        maximum_checkpoint_gap_seconds=gate.maximum_checkpoint_gap_seconds,
        prerequisite_fault_campaign_id=gate.prerequisite_fault_campaign_id,
        harness_code_sha256=gate.harness_code_sha256,
        environment_manifest_sha256=gate.environment_manifest_sha256,
        minimum_efficiency_improvement_ppm=gate.minimum_efficiency_improvement_ppm,
    )


def preflight_endurance_controller(
    manifest: EnduranceControllerManifest,
    *,
    repository_root: Path = REPO_ROOT,
    artifact_root: Path = REPO_ROOT,
    now: datetime | None = None,
) -> EnduranceControllerPreflight:
    manifest = EnduranceControllerManifest.model_validate(manifest.model_dump(mode="python"))
    if (
        now is not None
        and manifest.gate_manifest.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H
    ):
        raise EnduranceControllerPreflightError("real-time controller rejects an injected clock")
    blockers: list[str] = []
    code_ok = True
    source_ok = True
    try:
        verify_endurance_controller_code_identity(
            manifest.code_identity,
            repository_root=repository_root,
        )
    except Exception:
        code_ok = False
        blockers.append("code:identity_drift")
    try:
        fresh = _fresh_gate_manifest(manifest)
        if fresh != manifest.gate_manifest:
            source_ok = False
            blockers.append("gate:frozen_sources_changed")
    except Exception:
        source_ok = False
        blockers.append("gate:source_preflight_failed")
    try:
        pending = _load_pending(manifest, artifact_root)
    except Exception:
        pending = ()
        blockers.append("spool:invalid_pending_evidence")
    spool, pending_path, committed_path, receipts_path = _spool_paths(manifest, artifact_root)
    del spool
    if pending:
        blockers.append("spool:evidence_submitted_before_start")
    for label, path in (
        ("pending", pending_path),
        ("committed", committed_path),
        ("receipts", receipts_path),
    ):
        entries = tuple(path.iterdir())
        expected_pending = (
            {item_path.resolve() for _, item_path in pending} if label == "pending" else set()
        )
        unexplained = (
            tuple(item for item in entries if item.resolve() not in expected_pending)
            if label == "pending"
            else entries
        )
        if unexplained:
            blockers.append(f"spool:{label}_history_not_empty")
    snapshots = ResearchEnduranceStore().list(
        quest_id=manifest.gate_manifest.quest_id,
        limit=1_000,
    )
    unfinished = sum(item.report is None for item in snapshots)
    if unfinished:
        blockers.append("gate:quest_has_unfinished_endurance_gate")
    if any(item.manifest.gate_id == manifest.gate_manifest.gate_id for item in snapshots):
        blockers.append("gate:identity_already_started")
    with engine().connect() as connection:
        observed_at = _database_now(connection, now)
    canonical = tuple(sorted(set(blockers)))
    assert manifest.controller_id is not None
    assert manifest.gate_manifest.gate_id is not None
    return EnduranceControllerPreflight(
        controller_id=manifest.controller_id,
        gate_id=manifest.gate_manifest.gate_id,
        database_observed_at=observed_at,
        eligible_to_start=not canonical and code_ok and source_ok,
        blockers=canonical,
        pending_envelope_count=len(pending),
        code_identity_verified=code_ok,
        gate_sources_verified=source_ok,
        unfinished_gate_count=unfinished,
    )


def start_endurance_controller_gate(
    manifest: EnduranceControllerManifest,
    *,
    repository_root: Path = REPO_ROOT,
    artifact_root: Path = REPO_ROOT,
    now: datetime | None = None,
) -> EnduranceControllerTick:
    manifest = EnduranceControllerManifest.model_validate(manifest.model_dump(mode="python"))
    gate = manifest.gate_manifest
    if now is not None and gate.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
        raise EnduranceControllerPreflightError("real-time controller rejects an injected clock")
    verify_endurance_controller_code_identity(
        manifest.code_identity, repository_root=repository_root
    )
    prepare_controller_spool(manifest, artifact_root=artifact_root)
    assert manifest.controller_id is not None
    assert gate.gate_id is not None
    key = controller_advisory_key(gate.gate_id)
    with engine().connect() as connection:
        acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        observed_at = _database_now(connection, now)
        if not acquired:
            tick = _tick(
                manifest,
                action=EnduranceControllerAction.LOCK_BUSY,
                now=observed_at,
                checkpoint_count=0,
                pending_before=(),
                recovered=(),
                checkpoint_envelopes=(),
                tail_sha256=gate.manifest_sha256,
                tail_at=observed_at,
                message="another controller invocation owns the gate advisory lock",
            )
            _write_tick(manifest, tick, artifact_root)
            return tick
        try:
            try:
                existing = ResearchEnduranceStore().get(gate.gate_id)
            except ResearchEnduranceNotFound:
                existing = None
            if existing is not None and existing.manifest != gate:
                raise EnduranceControllerConflict(
                    "persisted endurance gate differs from the controller manifest"
                )
            if existing is None:
                preflight = preflight_endurance_controller(
                    manifest,
                    repository_root=repository_root,
                    artifact_root=artifact_root,
                    now=now,
                )
                if not preflight.eligible_to_start:
                    raise EnduranceControllerPreflightError(
                        "controller start preflight failed: " + ", ".join(preflight.blockers)
                    )
            receipt = ResearchEnduranceStore().start(
                gate,
                EnduranceCommandContext(
                    idempotency_key=f"{manifest.controller_id}:start",
                    principal=manifest.principal,
                ),
                now=now,
            )
            snapshot = ResearchEnduranceStore().get(gate.gate_id)
            observed_at = _database_now(connection, now)
            tail_at, tail_sha = _tail(snapshot)
            tick = _tick(
                manifest,
                action=EnduranceControllerAction.STARTED,
                now=observed_at,
                checkpoint_count=len(snapshot.checkpoints),
                pending_before=(),
                recovered=(),
                checkpoint_envelopes=(),
                tail_sha256=tail_sha,
                tail_at=tail_at,
                message=(
                    "database-clock endurance gate started"
                    if receipt.created
                    else "database-clock endurance start receipt replayed"
                ),
            )
            _write_tick(manifest, tick, artifact_root)
            return tick
        finally:
            connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def run_controller_tick(
    manifest: EnduranceControllerManifest,
    *,
    repository_root: Path = REPO_ROOT,
    artifact_root: Path = REPO_ROOT,
    now: datetime | None = None,
) -> EnduranceControllerTick:
    manifest = EnduranceControllerManifest.model_validate(manifest.model_dump(mode="python"))
    gate = manifest.gate_manifest
    if now is not None and gate.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
        raise EnduranceControllerConflict("real-time controller rejects an injected clock")
    verify_endurance_controller_code_identity(
        manifest.code_identity, repository_root=repository_root
    )
    prepare_controller_spool(manifest, artifact_root=artifact_root)
    assert manifest.controller_id is not None
    assert gate.gate_id is not None
    key = controller_advisory_key(gate.gate_id)
    with engine().connect() as connection:
        acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        observed_at = _database_now(connection, now)
        if not acquired:
            try:
                snapshot = ResearchEnduranceStore().get(gate.gate_id)
                tail_at, tail_sha = _tail(snapshot)
                count = len(snapshot.checkpoints)
            except ResearchEnduranceNotFound:
                tail_at, tail_sha, count = observed_at, gate.manifest_sha256, 0
            tick = _tick(
                manifest,
                action=EnduranceControllerAction.LOCK_BUSY,
                now=observed_at,
                checkpoint_count=count,
                pending_before=(),
                recovered=(),
                checkpoint_envelopes=(),
                tail_sha256=tail_sha,
                tail_at=tail_at,
                message="another controller invocation owns the gate advisory lock",
            )
            _write_tick(manifest, tick, artifact_root)
            return tick
        try:
            try:
                snapshot = ResearchEnduranceStore().get(gate.gate_id)
            except ResearchEnduranceNotFound as exc:
                raise EnduranceControllerConflict(
                    "endurance gate has not been explicitly started"
                ) from exc
            if snapshot.manifest != gate:
                raise EnduranceControllerConflict("controller is bound to another gate manifest")
            tail_at, tail_sha = _tail(snapshot)
            loaded = _load_pending(manifest, artifact_root)
            pending_before_ids = tuple(str(item.envelope_id) for item, _ in loaded)
            committed_ids = _committed_evidence_ids(snapshot)
            archived: list[str] = []
            new: list[tuple[EnduranceEvidenceEnvelope, Path]] = []
            for envelope, path in loaded:
                ids = _evidence_ids(envelope.evidence)
                overlap = ids.intersection(committed_ids)
                if overlap and overlap != ids:
                    raise EnduranceControllerConflict(
                        f"pending envelope is partially committed: {envelope.envelope_id}"
                    )
                if ids and ids.issubset(committed_ids):
                    _archive_envelope(manifest, envelope, path, artifact_root)
                    archived.append(str(envelope.envelope_id))
                else:
                    new.append((envelope, path))
            pending_ids = tuple(str(item.envelope_id) for item, _ in new)
            if snapshot.report is not None:
                tick = _tick(
                    manifest,
                    action=EnduranceControllerAction.TERMINAL,
                    now=observed_at,
                    checkpoint_count=len(snapshot.checkpoints),
                    pending_before=pending_before_ids,
                    recovered=tuple(archived),
                    checkpoint_envelopes=(),
                    tail_sha256=tail_sha,
                    tail_at=tail_at,
                    message="endurance gate is terminal; controller will not mutate it",
                )
                _write_tick(manifest, tick, artifact_root)
                return tick
            due_at = tail_at + timedelta(seconds=gate.checkpoint_interval_seconds)
            if not new and observed_at < due_at:
                action = (
                    EnduranceControllerAction.RECOVERED_SPOOL
                    if archived
                    else EnduranceControllerAction.NOT_DUE
                )
                tick = _tick(
                    manifest,
                    action=action,
                    now=observed_at,
                    checkpoint_count=len(snapshot.checkpoints),
                    pending_before=pending_before_ids,
                    recovered=tuple(archived),
                    checkpoint_envelopes=(),
                    tail_sha256=tail_sha,
                    tail_at=tail_at,
                    message=(
                        "archived evidence already present in the durable checkpoint chain"
                        if archived
                        else "next database-clock checkpoint is not due"
                    ),
                )
                _write_tick(manifest, tick, artifact_root)
                return tick
            evidence = _merge_envelopes(tuple(item for item, _ in new))
            context = EnduranceCommandContext(
                idempotency_key=f"{manifest.controller_id}:checkpoint:{tail_sha[:32]}",
                principal=manifest.principal,
            )
            mutation = ResearchEnduranceStore().append_checkpoint(
                gate.gate_id,
                evidence,
                context,
                now=now,
            )
            updated = ResearchEnduranceStore().get(gate.gate_id)
            checkpoint_snapshot = next(
                (
                    item
                    for item in updated.checkpoints
                    if item.checkpoint.checkpoint_id == mutation.object_id
                ),
                None,
            )
            if checkpoint_snapshot is None or checkpoint_snapshot.checkpoint.evidence != evidence:
                raise EnduranceControllerConflict(
                    "controller mutation receipt does not resolve to exact checkpoint evidence"
                )
            checkpoint = checkpoint_snapshot.checkpoint
            assert checkpoint.checkpoint_id is not None
            assert checkpoint.checkpoint_sha256 is not None
            tick = _tick(
                manifest,
                action=EnduranceControllerAction.CHECKPOINTED,
                now=checkpoint.observation.observed_at,
                checkpoint_count=len(snapshot.checkpoints),
                pending_before=pending_before_ids,
                recovered=tuple(archived),
                checkpoint_envelopes=pending_ids,
                tail_sha256=tail_sha,
                tail_at=tail_at,
                resulting_tail_at=checkpoint.observation.observed_at,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_sha256=checkpoint.checkpoint_sha256,
                command_id=mutation.command_id,
                mutation_created=mutation.created,
                message=(
                    "durable checkpoint committed from database clock and pending evidence"
                    if new
                    else "scheduled database-clock checkpoint committed"
                ),
            )
            _write_tick(manifest, tick, artifact_root)
            for envelope, path in new:
                _archive_envelope(manifest, envelope, path, artifact_root)
            return tick
        except ResearchEnduranceConflict:
            raise
        finally:
            connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def controller_status(
    manifest: EnduranceControllerManifest,
    *,
    artifact_root: Path = REPO_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    manifest = EnduranceControllerManifest.model_validate(manifest.model_dump(mode="python"))
    gate = manifest.gate_manifest
    if now is not None and gate.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
        raise EnduranceControllerConflict("real-time controller rejects an injected clock")
    with engine().connect() as connection:
        observed_at = _database_now(connection, now)
    try:
        snapshot = ResearchEnduranceStore().get(str(gate.gate_id))
    except ResearchEnduranceNotFound:
        snapshot = None
    pending = _load_pending(manifest, artifact_root)
    if snapshot is None:
        return {
            "controller_id": manifest.controller_id,
            "gate_id": gate.gate_id,
            "state": "not_started",
            "database_observed_at": observed_at.isoformat(),
            "checkpoint_count": 0,
            "pending_envelope_ids": [item.envelope_id for item, _ in pending],
            "automatic_finalization": False,
        }
    tail_at, tail_sha = _tail(snapshot)
    due = tail_at + timedelta(seconds=gate.checkpoint_interval_seconds)
    deadline = tail_at + timedelta(seconds=gate.maximum_checkpoint_gap_seconds)
    return {
        "controller_id": manifest.controller_id,
        "gate_id": gate.gate_id,
        "state": "terminal" if snapshot.report is not None else "running",
        "database_observed_at": observed_at.isoformat(),
        "started_at": snapshot.started_at.isoformat(),
        "checkpoint_count": len(snapshot.checkpoints),
        "tail_sha256": tail_sha,
        "next_checkpoint_due_at": due.isoformat(),
        "maximum_gap_deadline_at": deadline.isoformat(),
        "seconds_until_due": int((due - observed_at).total_seconds()),
        "seconds_until_maximum_gap": int((deadline - observed_at).total_seconds()),
        "overdue": observed_at > deadline,
        "pending_envelope_ids": [item.envelope_id for item, _ in pending],
        "automatic_finalization": False,
    }


__all__ = [
    "EnduranceControllerAction",
    "EnduranceControllerCodeIdentity",
    "EnduranceControllerConflict",
    "EnduranceControllerError",
    "EnduranceControllerManifest",
    "EnduranceControllerPreflight",
    "EnduranceControllerPreflightError",
    "EnduranceControllerTick",
    "EnduranceEvidenceEnvelope",
    "capture_endurance_controller_code_identity",
    "controller_advisory_key",
    "controller_status",
    "preflight_endurance_controller",
    "prepare_controller_spool",
    "prepare_endurance_controller_manifest",
    "run_controller_tick",
    "start_endurance_controller_gate",
    "submit_controller_evidence",
    "verify_endurance_controller_code_identity",
]
