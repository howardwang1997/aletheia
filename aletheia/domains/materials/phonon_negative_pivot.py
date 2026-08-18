"""Conditional negative-result pivot for the production phonon endurance Quest."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from aletheia.db import REPO_ROOT, engine
from aletheia.domains.materials.phonon_commissioning import (
    PhononQuestCommissioningManifest,
    verify_commissioning_artifacts,
)
from aletheia.domains.materials.phonon_reproduction import (
    PhononIndependentReplayProtocol,
    PhononReplayCommitReceipt,
    verify_phonon_replay_code_identity,
)
from aletheia.programs.endurance import ResearchEnduranceNotFound, ResearchEnduranceStore
from aletheia.programs.endurance_controller import (
    EnduranceControllerManifest,
    EnduranceEvidenceEnvelope,
    submit_controller_evidence,
    verify_endurance_controller_code_identity,
)
from aletheia.programs.endurance_schemas import (
    EnduranceCheckpointEvidence,
    EnduranceReproductionConclusion,
    EnduranceReproductionReceipt,
    EnduranceStrategyFingerprint,
    EnduranceStructuralPivotReceipt,
)
from aletheia.programs.graph import ProgramGraphStore
from aletheia.programs.memory import ResearchMemoryStore
from aletheia.programs.memory_schemas import MemoryFactKind, MemoryFactSnapshot
from aletheia.programs.schemas import GraphCommandContext, GraphNodeState, NodeTransitionSpec
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_WORK_ORDER_ID_PATTERN = r"^pnpw_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_PROTOCOL_ID_PATTERN = r"^pirp_[0-9a-f]{32}$"
_COMMISSIONING_ID_PATTERN = r"^pcm_[0-9a-f]{32}$"
_NEGATIVE_STATEMENT = (
    "Implementation-diverse same-source replay contradicted the preregistered robust "
    "aligned-structure signal and must trigger strategy review without claim repair."
)
_CODE_COMPONENTS = (
    "aletheia/domains/materials/phonon_commissioning.py",
    "aletheia/domains/materials/phonon_negative_pivot.py",
    "aletheia/domains/materials/phonon_reproduction.py",
    "aletheia/programs/endurance.py",
    "aletheia/programs/endurance_controller.py",
    "aletheia/programs/endurance_schemas.py",
    "aletheia/programs/graph.py",
    "aletheia/programs/memory.py",
    "aletheia/programs/memory_schemas.py",
    "scripts/run_phonon_negative_pivot.py",
)


class PhononNegativePivotError(RuntimeError):
    """The conditional production pivot is invalid or out of sequence."""


class PhononNegativePivotConflict(PhononNegativePivotError):
    """Frozen sources, evidence, or durable state differ from the pivot work order."""


class PhononNegativePivotNotApplicable(PhononNegativePivotError):
    """The replay outcome is not a contradiction and must not trigger this pivot."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str, text: bool = True) -> Any:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout.strip() if text else result.stdout


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("phonon pivot artifact path must be repository-relative")
    return path


def _database_now() -> datetime:
    with engine().connect() as connection:
        observed = connection.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise PhononNegativePivotConflict("PostgreSQL did not return an aware pivot clock")
    return observed


class PhononNegativePivotCodeIdentity(_FrozenModel):
    git_commit: str = Field(pattern=_GIT_SHA_PATTERN)
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_provenance_verified: bool

    @model_validator(mode="after")
    def _closed_identity(self) -> "PhononNegativePivotCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("phonon negative-pivot code-component matrix is incomplete")
        expected = content_sha256(
            {
                "schema": "aletheia.phonon_negative_pivot_code.v1",
                "git_commit": self.git_commit,
                "components": components,
                "committed_provenance_verified": self.committed_provenance_verified,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("phonon negative-pivot code identity is inconsistent")
        object.__setattr__(self, "component_sha256s", components)
        return self


class PhononNegativePivotArtifact(_FrozenModel):
    relative_path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> "PhononNegativePivotArtifact":
        _safe_relative(self.relative_path)
        return self


class PhononNegativePivotWorkOrder(_FrozenModel):
    schema_version: Literal[1] = 1
    work_order_id: str | None = Field(default=None, pattern=_WORK_ORDER_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    commissioning_id: str = Field(pattern=_COMMISSIONING_ID_PATTERN)
    original_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    source_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    successor_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    initial_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_planned_version: int = Field(ge=1)
    source_active_version: int = Field(ge=2)
    successor_planned_version: int = Field(ge=1)
    controller: PhononNegativePivotArtifact
    protocol: PhononNegativePivotArtifact
    commissioning: PhononNegativePivotArtifact
    before: EnduranceStrategyFingerprint
    after: EnduranceStrategyFingerprint
    source_stop_reason: str = Field(min_length=1, max_length=4_000)
    successor_start_reason: str = Field(min_length=1, max_length=4_000)
    transition_principal: str = Field(min_length=1, max_length=128)
    assessed_by: str = Field(min_length=1, max_length=128)
    producer: str = Field(min_length=1, max_length=128)
    code_identity: PhononNegativePivotCodeIdentity
    prepared_at: AwareDatetime
    required_replay_conclusion: Literal["contradicted"] = "contradicted"
    successor_authority: Literal["lineage_and_target_qualification_only"] = (
        "lineage_and_target_qualification_only"
    )
    data_allocation_allowed: Literal[False] = False
    outward_actions_allowed: Literal[False] = False
    automatic_pivot: Literal[False] = False

    @model_validator(mode="after")
    def _closed_work_order(self) -> "PhononNegativePivotWorkOrder":
        if len(
            {
                self.original_campaign_id,
                self.source_campaign_id,
                self.successor_campaign_id,
            }
        ) != 3:
            raise ValueError("phonon pivot requires three distinct Campaigns")
        if self.source_active_version != self.source_planned_version + 1:
            raise ValueError("phonon pivot source version does not follow one activation")
        if self.before == self.after:
            raise ValueError("phonon pivot strategy must change")
        if self.transition_principal == self.assessed_by:
            raise ValueError("phonon pivot assessor must be independent from transitions")
        # Reuse the endurance receipt validator to enforce the structural-change rule now.
        EnduranceStructuralPivotReceipt(
            negative_result_fact_id="mem_" + "0" * 32,
            source_campaign_id=self.source_campaign_id,
            successor_campaign_id=self.successor_campaign_id,
            source_transition_id="precommitted-source-transition",
            successor_transition_id="precommitted-successor-transition",
            before=self.before,
            after=self.after,
            assessor_code_sha256=self.code_identity.aggregate_sha256,
            assessed_by=self.assessed_by,
            evidence_sha256s=("0" * 64,),
            occurred_at=self.prepared_at,
        )
        expected = f"pnpw_{self.work_order_sha256[:32]}"
        if self.work_order_id is not None and self.work_order_id != expected:
            raise ValueError("phonon pivot work-order ID differs from content")
        object.__setattr__(self, "work_order_id", expected)
        return self

    @property
    def work_order_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"work_order_id"}))


class PhononNegativePivotPreflight(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    database_observed_at: AwareDatetime
    ready_for_explicit_gate_start: bool
    blockers: tuple[str, ...]
    code_and_files_verified: bool
    graph_verified: bool
    source_state: GraphNodeState | None
    successor_state: GraphNodeState | None
    automatic_pivot: Literal[False] = False

    @model_validator(mode="after")
    def _derived_verdict(self) -> "PhononNegativePivotPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("phonon pivot preflight blockers must be canonical")
        expected = not blockers and self.code_and_files_verified and self.graph_verified
        if self.ready_for_explicit_gate_start != expected:
            raise ValueError("phonon pivot preflight verdict differs from blockers")
        return self


class PhononNegativePivotExecutionReceipt(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    replay_result_id: str = Field(pattern=r"^pirr_[0-9a-f]{32}$")
    replay_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    negative_result_fact_id: str = Field(pattern=r"^mem_[0-9a-f]{32}$")
    source_transition_id: str
    successor_transition_id: str
    pivot: EnduranceStructuralPivotReceipt
    envelope: EnduranceEvidenceEnvelope
    source_stopped: Literal[True] = True
    successor_active: Literal[True] = True
    successor_authority: Literal["lineage_and_target_qualification_only"] = (
        "lineage_and_target_qualification_only"
    )
    data_allocated: Literal[False] = False
    outward_action_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _closed_receipt(self) -> "PhononNegativePivotExecutionReceipt":
        if (
            self.pivot.negative_result_fact_id != self.negative_result_fact_id
            or self.pivot.source_transition_id != self.source_transition_id
            or self.pivot.successor_transition_id != self.successor_transition_id
            or self.envelope.evidence.structural_pivots != (self.pivot,)
        ):
            raise ValueError("phonon pivot execution receipt is internally inconsistent")
        return self


def capture_phonon_negative_pivot_code_identity(
    *,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononNegativePivotCodeIdentity:
    root = repository_root.resolve()
    components = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    tracked = subprocess.run(
        ("git", "ls-files", "--error-unmatch", *_CODE_COMPONENTS),
        cwd=root,
        capture_output=True,
        text=True,
    )
    unstaged = subprocess.run(("git", "diff", "--quiet", "--", *_CODE_COMPONENTS), cwd=root)
    staged = subprocess.run(
        ("git", "diff", "--cached", "--quiet", "--", *_CODE_COMPONENTS), cwd=root
    )
    committed = tracked.returncode == unstaged.returncode == staged.returncode == 0
    if require_committed and not committed:
        raise PhononNegativePivotConflict(
            "phonon negative-pivot components must be tracked and committed"
        )
    git_commit = _git(root, "rev-parse", "HEAD")
    projection = {
        "schema": "aletheia.phonon_negative_pivot_code.v1",
        "git_commit": git_commit,
        "components": dict(sorted(components.items())),
        "committed_provenance_verified": committed and require_committed,
    }
    return PhononNegativePivotCodeIdentity(
        git_commit=git_commit,
        component_sha256s=components,
        aggregate_sha256=content_sha256(projection),
        committed_provenance_verified=committed and require_committed,
    )


def verify_phonon_negative_pivot_code_identity(
    identity: PhononNegativePivotCodeIdentity,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    root = repository_root.resolve()
    live = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    if dict(sorted(live.items())) != identity.component_sha256s:
        raise PhononNegativePivotConflict("live phonon pivot code differs from frozen identity")
    if identity.committed_provenance_verified:
        try:
            _git(root, "cat-file", "-e", f"{identity.git_commit}^{{commit}}")
            frozen = {
                relative: hashlib.sha256(
                    _git(root, "show", f"{identity.git_commit}:{relative}", text=False)
                ).hexdigest()
                for relative in _CODE_COMPONENTS
            }
        except subprocess.CalledProcessError as exc:
            raise PhononNegativePivotConflict(
                "frozen phonon pivot Git provenance cannot be reconstructed"
            ) from exc
        if dict(sorted(frozen.items())) != identity.component_sha256s:
            raise PhononNegativePivotConflict(
                "frozen commit does not contain the phonon pivot components"
            )


def _artifact(
    path: Path,
    *,
    content_sha256_value: str,
    root: Path,
) -> PhononNegativePivotArtifact:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PhononNegativePivotConflict("phonon pivot artifact escaped repository") from exc
    return PhononNegativePivotArtifact(
        relative_path=relative,
        file_sha256=_sha256_file(resolved),
        content_sha256=content_sha256_value,
    )


def _model_projection(items: Any) -> Any:
    if isinstance(items, BaseModel):
        return items.model_dump(mode="json")
    return [item.model_dump(mode="json") for item in items]


def _strategy_fingerprints(
    protocol: PhononIndependentReplayProtocol,
    commissioning: PhononQuestCommissioningManifest,
) -> tuple[EnduranceStrategyFingerprint, EnduranceStrategyFingerprint]:
    worlds = {item.question.kind.value: item for item in commissioning.world_models}
    predictive = worlds["predictive"]
    shared_hypotheses = content_sha256(_model_projection(predictive.hypotheses))
    before = EnduranceStrategyFingerprint(
        hypothesis_semantics_sha256=shared_hypotheses,
        prediction_pattern_sha256=content_sha256(_model_projection(predictive.predictions)),
        capability_input_sha256=content_sha256(
            {
                "dataset": protocol.dataset.file_sha256,
                "source_plan": protocol.source_plan.content_sha256,
                "replay_code": protocol.code_identity.aggregate_sha256,
            }
        ),
        analysis_plan_sha256=content_sha256(
            {
                "protocol": protocol.protocol_sha256,
                "estimator": protocol.estimator_policy.model_dump(mode="json"),
                "same_source_only": True,
            }
        ),
        discriminated_pairs_sha256=content_sha256(
            [
                {
                    "hypothesis_id": item.hypothesis_id,
                    "discriminates_from": item.discriminates_from_hypothesis_ids,
                }
                for item in predictive.predictions
            ]
        ),
    )
    candidates_sha = content_sha256(_model_projection(commissioning.external_corpus_candidates))
    after = EnduranceStrategyFingerprint(
        hypothesis_semantics_sha256=shared_hypotheses,
        prediction_pattern_sha256=content_sha256(
            {
                "strategy": "cross-calculation-transfer-qualification",
                "target_quantity_kind_id": commissioning.evidence.target_quantity_kind_id,
                "candidate_manifest_sha256": candidates_sha,
                "target_values_access": "none",
            }
        ),
        capability_input_sha256=content_sha256(
            {
                "required_data_role": "external_validation",
                "available": False,
                "qualification_only": True,
                "candidate_manifest_sha256": candidates_sha,
            }
        ),
        analysis_plan_sha256=content_sha256(
            {
                "steps": [
                    "calculation_lineage_audit",
                    "license_and_custody_audit",
                    "target_definition_harmonisation",
                    "target_blind_material_overlap",
                ],
                "data_allocation": False,
                "outward_action": False,
            }
        ),
        discriminated_pairs_sha256=content_sha256(
            {
                "pairs": [
                    "same_source_specific_vs_cross_workflow_transferable",
                    "target_compatible_vs_target_incommensurate",
                    "independent_lineage_vs_source_overlap",
                ]
            }
        ),
    )
    return before, after


def prepare_phonon_negative_pivot_work_order(
    *,
    controller: EnduranceControllerManifest,
    controller_path: Path,
    protocol: PhononIndependentReplayProtocol,
    protocol_path: Path,
    commissioning: PhononQuestCommissioningManifest,
    commissioning_path: Path,
    prepared_at: datetime,
    transition_principal: str = "controller:phonon-science",
    assessed_by: str = "harness:phonon-negative-pivot",
    producer: str = "harness:phonon-negative-pivot",
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononNegativePivotWorkOrder:
    root = repository_root.resolve()
    if (
        controller.controller_id is None
        or controller.gate_manifest.gate_id is None
        or protocol.protocol_id is None
        or commissioning.commissioning_id is None
    ):
        raise PhononNegativePivotConflict("phonon pivot source identity is incomplete")
    if (
        protocol.quest_id != commissioning.quest.node_id
        or protocol.controller_id != controller.controller_id
        or protocol.gate_id != controller.gate_manifest.gate_id
        or protocol.commissioning_id != commissioning.commissioning_id
    ):
        raise PhononNegativePivotConflict("phonon pivot source binding changed")
    campaigns = {item.identity_key.rsplit(":", 1)[-1]: item for item in commissioning.campaigns}
    if protocol.original_campaign_id != campaigns["independent-replay"].node_id:
        raise PhononNegativePivotConflict("phonon pivot original Campaign changed")
    if protocol.reproduction_campaign_id != campaigns["mechanism-ablation"].node_id:
        raise PhononNegativePivotConflict("phonon pivot source Campaign changed")
    successor_id = campaigns["external-calculation"].node_id
    graph = ProgramGraphStore().get_quest(protocol.quest_id)
    by_id = {item.node_id: item for item in graph.nodes}
    source = by_id.get(protocol.reproduction_campaign_id)
    successor = by_id.get(successor_id)
    if source is None or successor is None:
        raise PhononNegativePivotConflict("phonon pivot Campaign disappeared")
    if source.state is not GraphNodeState.PLANNED or successor.state is not GraphNodeState.PLANNED:
        raise PhononNegativePivotConflict("phonon pivot must be frozen from commissioned states")
    before, after = _strategy_fingerprints(protocol, commissioning)
    return PhononNegativePivotWorkOrder(
        quest_id=protocol.quest_id,
        gate_id=protocol.gate_id,
        controller_id=controller.controller_id,
        protocol_id=protocol.protocol_id,
        commissioning_id=commissioning.commissioning_id,
        original_campaign_id=protocol.original_campaign_id,
        source_campaign_id=protocol.reproduction_campaign_id,
        successor_campaign_id=successor_id,
        initial_graph_sha256=graph.graph_sha256,
        source_planned_version=source.state_version,
        source_active_version=source.state_version + 1,
        successor_planned_version=successor.state_version,
        controller=_artifact(
            controller_path,
            content_sha256_value=controller.manifest_sha256,
            root=root,
        ),
        protocol=_artifact(
            protocol_path,
            content_sha256_value=protocol.protocol_sha256,
            root=root,
        ),
        commissioning=_artifact(
            commissioning_path,
            content_sha256_value=commissioning.manifest_sha256,
            root=root,
        ),
        before=before,
        after=after,
        source_stop_reason=(
            "Stop the contradicted same-source replay/mechanism branch without repairing its "
            "negative result or inflating the original claim."
        ),
        successor_start_reason=(
            "Activate only the precommitted external-corpus lineage and target qualification "
            "strategy; no data allocation, target access, or outward action is authorized."
        ),
        transition_principal=transition_principal,
        assessed_by=assessed_by,
        producer=producer,
        code_identity=capture_phonon_negative_pivot_code_identity(
            repository_root=root,
            require_committed=require_committed,
        ),
        prepared_at=prepared_at,
    )


def _load_bound(
    binding: PhononNegativePivotArtifact,
    model: type[BaseModel],
    *,
    repository_root: Path,
) -> BaseModel:
    root = repository_root.resolve()
    relative = _safe_relative(binding.relative_path)
    path = (root / Path(*relative.parts)).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PhononNegativePivotConflict(
            "phonon pivot artifact resolved outside repository"
        ) from exc
    if _sha256_file(path) != binding.file_sha256:
        raise PhononNegativePivotConflict("phonon pivot bound artifact bytes changed")
    value = model.model_validate_json(path.read_bytes())
    content = getattr(value, "manifest_sha256", None) or getattr(value, "protocol_sha256", None)
    if content != binding.content_sha256:
        raise PhononNegativePivotConflict("phonon pivot artifact content changed")
    return value


def verify_phonon_negative_pivot_work_order(
    work_order: PhononNegativePivotWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
    require_initial_graph: bool = False,
) -> tuple[
    EnduranceControllerManifest,
    PhononIndependentReplayProtocol,
    PhononQuestCommissioningManifest,
]:
    root = repository_root.resolve()
    verify_phonon_negative_pivot_code_identity(work_order.code_identity, repository_root=root)
    controller = _load_bound(
        work_order.controller,
        EnduranceControllerManifest,
        repository_root=root,
    )
    protocol = _load_bound(
        work_order.protocol,
        PhononIndependentReplayProtocol,
        repository_root=root,
    )
    commissioning = _load_bound(
        work_order.commissioning,
        PhononQuestCommissioningManifest,
        repository_root=root,
    )
    assert isinstance(controller, EnduranceControllerManifest)
    assert isinstance(protocol, PhononIndependentReplayProtocol)
    assert isinstance(commissioning, PhononQuestCommissioningManifest)
    verify_endurance_controller_code_identity(controller.code_identity, repository_root=root)
    verify_phonon_replay_code_identity(protocol.code_identity, repository_root=root)
    verify_commissioning_artifacts(commissioning, repository_root=root)
    if (
        controller.controller_id != work_order.controller_id
        or controller.gate_manifest.gate_id != work_order.gate_id
        or protocol.protocol_id != work_order.protocol_id
        or commissioning.commissioning_id != work_order.commissioning_id
        or protocol.original_campaign_id != work_order.original_campaign_id
        or protocol.reproduction_campaign_id != work_order.source_campaign_id
    ):
        raise PhononNegativePivotConflict("phonon pivot work-order binding changed")
    before, after = _strategy_fingerprints(protocol, commissioning)
    if before != work_order.before or after != work_order.after:
        raise PhononNegativePivotConflict("phonon pivot strategy fingerprint changed")
    if require_initial_graph:
        graph = ProgramGraphStore().get_quest(work_order.quest_id)
        if graph.graph_sha256 != work_order.initial_graph_sha256:
            raise PhononNegativePivotConflict("phonon pivot initial Quest graph changed")
    return controller, protocol, commissioning


def preflight_phonon_negative_pivot_start(
    work_order: PhononNegativePivotWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononNegativePivotPreflight:
    blockers: list[str] = []
    code_files = graph_ok = False
    source_state = successor_state = None
    try:
        verify_phonon_negative_pivot_work_order(
            work_order,
            repository_root=repository_root,
            require_initial_graph=True,
        )
        code_files = True
        graph = ProgramGraphStore().get_quest(work_order.quest_id)
        by_id = {item.node_id: item for item in graph.nodes}
        source = by_id[work_order.source_campaign_id]
        successor = by_id[work_order.successor_campaign_id]
        source_state = source.state
        successor_state = successor.state
        if (
            source.state is not GraphNodeState.PLANNED
            or source.state_version != work_order.source_planned_version
        ):
            blockers.append("campaign:source_not_planned")
        if (
            successor.state is not GraphNodeState.PLANNED
            or successor.state_version != work_order.successor_planned_version
        ):
            blockers.append("campaign:successor_not_planned")
        graph_ok = not blockers
    except (RuntimeError, OSError, ValueError, KeyError):
        blockers.append("work_order:code_files_or_graph_changed")
    try:
        ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound:
        pass
    else:
        blockers.append("gate:already_started")
    canonical = tuple(sorted(set(blockers)))
    assert work_order.work_order_id is not None
    return PhononNegativePivotPreflight(
        work_order_id=work_order.work_order_id,
        gate_id=work_order.gate_id,
        database_observed_at=_database_now(),
        ready_for_explicit_gate_start=(not canonical and code_files and graph_ok),
        blockers=canonical,
        code_and_files_verified=code_files,
        graph_verified=graph_ok,
        source_state=source_state,
        successor_state=successor_state,
    )


def _verify_envelope_in_spool(
    controller: EnduranceControllerManifest,
    envelope: EnduranceEvidenceEnvelope,
    *,
    artifact_root: Path,
) -> None:
    root = artifact_root.resolve()
    relative = _safe_relative(controller.spool_root)
    spool = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        spool.relative_to(root)
    except ValueError as exc:
        raise PhononNegativePivotConflict("phonon pivot controller spool escaped root") from exc
    assert envelope.envelope_id is not None
    paths = tuple(
        spool / state / f"{envelope.envelope_id}.json" for state in ("pending", "committed")
    )
    found = False
    for path in paths:
        if not path.exists():
            continue
        found = True
        try:
            payload = path.read_bytes()
            stored = EnduranceEvidenceEnvelope.model_validate_json(payload)
        except Exception as exc:
            raise PhononNegativePivotConflict(
                "phonon pivot replay envelope is corrupt"
            ) from exc
        if stored != envelope or payload != canonical_json_bytes(stored) + b"\n":
            raise PhononNegativePivotConflict("phonon pivot replay envelope bytes changed")
    if not found:
        raise PhononNegativePivotConflict("phonon pivot replay envelope is absent from spool")


def _verify_negative_trigger(
    work_order: PhononNegativePivotWorkOrder,
    protocol: PhononIndependentReplayProtocol,
    controller: EnduranceControllerManifest,
    replay_commit: PhononReplayCommitReceipt,
    *,
    artifact_root: Path,
) -> tuple[MemoryFactSnapshot, EnduranceReproductionReceipt]:
    replay_commit = PhononReplayCommitReceipt.model_validate(
        replay_commit.model_dump(mode="python")
    )
    if (
        replay_commit.protocol_id != work_order.protocol_id
        or replay_commit.result_id != f"pirr_{replay_commit.result_sha256[:32]}"
    ):
        raise PhononNegativePivotConflict("phonon pivot replay commit uses another protocol")
    if replay_commit.conclusion is not EnduranceReproductionConclusion.CONTRADICTED:
        raise PhononNegativePivotNotApplicable(
            "phonon pivot requires a contradicted replay; retain this outcome without graph change"
        )
    envelope = replay_commit.envelope
    if envelope.controller_id != controller.controller_id or envelope.gate_id != work_order.gate_id:
        raise PhononNegativePivotConflict("phonon pivot replay envelope uses another controller")
    evidence = envelope.evidence
    if len(evidence.reproductions) != 1 or evidence.interruptions or evidence.structural_pivots:
        raise PhononNegativePivotConflict("phonon pivot replay envelope has unexpected evidence")
    reproduction = evidence.reproductions[0]
    if (
        reproduction.original_campaign_id != work_order.original_campaign_id
        or reproduction.reproduction_campaign_id != work_order.source_campaign_id
        or reproduction.protocol_sha256 != protocol.protocol_sha256
        or reproduction.reproduction_result_sha256 != replay_commit.result_sha256
        or reproduction.conclusion is not EnduranceReproductionConclusion.CONTRADICTED
    ):
        raise PhononNegativePivotConflict("phonon pivot replay evidence binding changed")
    _verify_envelope_in_spool(controller, envelope, artifact_root=artifact_root)
    facts = ResearchMemoryStore().eligible_facts(work_order.source_campaign_id, "pivot-analysis")
    matches = tuple(item for item in facts if item.fact_id == replay_commit.memory_fact_id)
    if len(matches) != 1:
        raise PhononNegativePivotConflict("phonon pivot negative-result fact is missing")
    fact = matches[0]
    expected_detail = {
        "schema": "aletheia.phonon_independent_replay_outcome.v1",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.protocol_sha256,
        "result_id": replay_commit.result_id,
        "result_sha256": replay_commit.result_sha256,
        "disposition": "no_aligned_structure_advantage",
        "conclusion": "contradicted",
        "same_source_only": True,
        "independent_external_replication_claim_forbidden": True,
        "causal_or_mechanism_claim_forbidden": True,
    }
    if (
        fact.kind is not MemoryFactKind.NEGATIVE_RESULT
        or fact.scope_node_id != work_order.source_campaign_id
        or fact.statement != _NEGATIVE_STATEMENT
        or fact.detail != expected_detail
        or {
            (item.task_key, item.context_role)
            for item in fact.task_bindings
        }
        != {
            ("phonon-replay-outcome", "required"),
            ("pivot-analysis", "required"),
        }
        or len(fact.sources) != 1
        or fact.sources[0].kind.value != "artifact"
        or fact.sources[0].source_id
        != f"phonon-independent-replay:{replay_commit.result_id}"
        or fact.sources[0].sha256 != replay_commit.result_sha256
        or fact.created_at < reproduction.completed_at
    ):
        raise PhononNegativePivotConflict("phonon pivot negative-result fact changed provenance")
    return fact, reproduction


def _transition(
    *,
    work_order: PhononNegativePivotWorkOrder,
    node_id: str,
    expected_version: int,
    target: GraphNodeState,
    reason: str,
    key: str,
):
    assert work_order.work_order_id is not None
    return ProgramGraphStore().transition_node(
        NodeTransitionSpec(
            node_id=node_id,
            expected_version=expected_version,
            to_state=target,
            reason=reason,
        ),
        GraphCommandContext(
            idempotency_key=f"{work_order.work_order_id}:{key}",
            principal=work_order.transition_principal,
        ),
    )


def execute_phonon_negative_result_pivot(
    work_order: PhononNegativePivotWorkOrder,
    replay_commit: PhononReplayCommitReceipt,
    *,
    artifact_root: Path = REPO_ROOT,
    repository_root: Path = REPO_ROOT,
) -> PhononNegativePivotExecutionReceipt:
    controller, protocol, _ = verify_phonon_negative_pivot_work_order(
        work_order,
        repository_root=repository_root,
    )
    try:
        gate = ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound as exc:
        raise PhononNegativePivotConflict("phonon pivot requires an explicitly started gate") from exc
    if gate.report is not None or gate.manifest.quest_id != work_order.quest_id:
        raise PhononNegativePivotConflict("phonon pivot found a terminal or different gate")
    fact, reproduction = _verify_negative_trigger(
        work_order,
        protocol,
        controller,
        replay_commit,
        artifact_root=artifact_root,
    )
    if not gate.started_at <= reproduction.completed_at <= fact.created_at:
        raise PhononNegativePivotConflict("phonon pivot trigger is outside the live gate window")
    graph = ProgramGraphStore().get_quest(work_order.quest_id)
    by_id = {item.node_id: item for item in graph.nodes}
    source = by_id[work_order.source_campaign_id]
    successor = by_id[work_order.successor_campaign_id]
    allowed = {
        (GraphNodeState.ACTIVE, GraphNodeState.PLANNED),
        (GraphNodeState.STOPPED, GraphNodeState.PLANNED),
        (GraphNodeState.STOPPED, GraphNodeState.ACTIVE),
    }
    if (source.state, successor.state) not in allowed:
        raise PhononNegativePivotConflict("phonon pivot Campaign state is not initial/partial/final")
    source_mutation = _transition(
        work_order=work_order,
        node_id=work_order.source_campaign_id,
        expected_version=work_order.source_active_version,
        target=GraphNodeState.STOPPED,
        reason=work_order.source_stop_reason,
        key="stop-source",
    )
    successor_mutation = _transition(
        work_order=work_order,
        node_id=work_order.successor_campaign_id,
        expected_version=work_order.successor_planned_version,
        target=GraphNodeState.ACTIVE,
        reason=work_order.successor_start_reason,
        key="start-successor",
    )
    source_transition_id = str(source_mutation.command.result["transition_id"])
    successor_transition_id = str(successor_mutation.command.result["transition_id"])
    final_graph = ProgramGraphStore().get_quest(work_order.quest_id)
    final_nodes = {item.node_id: item for item in final_graph.nodes}
    if (
        final_nodes[work_order.source_campaign_id].state is not GraphNodeState.STOPPED
        or final_nodes[work_order.successor_campaign_id].state is not GraphNodeState.ACTIVE
    ):
        raise PhononNegativePivotConflict("phonon pivot graph transitions did not persist")
    transitions = {item.transition_id: item for item in final_graph.transitions}
    source_transition = transitions.get(source_transition_id)
    successor_transition = transitions.get(successor_transition_id)
    if (
        source_transition is None
        or source_transition.from_state is not GraphNodeState.ACTIVE
        or source_transition.to_state is not GraphNodeState.STOPPED
        or source_transition.principal != work_order.transition_principal
        or successor_transition is None
        or successor_transition.from_state is not GraphNodeState.PLANNED
        or successor_transition.to_state is not GraphNodeState.ACTIVE
        or successor_transition.principal != work_order.transition_principal
        or fact.created_at > source_transition.created_at
        or fact.created_at > successor_transition.created_at
    ):
        raise PhononNegativePivotConflict("phonon pivot transition provenance changed")
    occurred_at = max(source_transition.created_at, successor_transition.created_at)
    pivot = EnduranceStructuralPivotReceipt(
        negative_result_fact_id=fact.fact_id,
        source_campaign_id=work_order.source_campaign_id,
        successor_campaign_id=work_order.successor_campaign_id,
        source_transition_id=source_transition_id,
        successor_transition_id=successor_transition_id,
        before=work_order.before,
        after=work_order.after,
        assessor_code_sha256=work_order.code_identity.aggregate_sha256,
        assessed_by=work_order.assessed_by,
        evidence_sha256s=tuple(
            sorted(
                {
                    work_order.work_order_sha256,
                    protocol.protocol_sha256,
                    replay_commit.result_sha256,
                    fact.fact_sha256,
                    work_order.before.fingerprint_sha256,
                    work_order.after.fingerprint_sha256,
                    content_sha256({"transition_id": source_transition_id}),
                    content_sha256({"transition_id": successor_transition_id}),
                }
            )
        ),
        occurred_at=occurred_at,
    )
    envelope, _ = submit_controller_evidence(
        controller,
        EnduranceCheckpointEvidence(structural_pivots=(pivot,)),
        producer=work_order.producer,
        submitted_at=occurred_at,
        artifact_root=artifact_root,
    )
    assert work_order.work_order_id is not None
    return PhononNegativePivotExecutionReceipt(
        work_order_id=work_order.work_order_id,
        gate_id=work_order.gate_id,
        replay_result_id=replay_commit.result_id,
        replay_result_sha256=replay_commit.result_sha256,
        negative_result_fact_id=fact.fact_id,
        source_transition_id=source_transition_id,
        successor_transition_id=successor_transition_id,
        pivot=pivot,
        envelope=envelope,
    )


__all__ = [
    "PhononNegativePivotArtifact",
    "PhononNegativePivotCodeIdentity",
    "PhononNegativePivotConflict",
    "PhononNegativePivotError",
    "PhononNegativePivotExecutionReceipt",
    "PhononNegativePivotNotApplicable",
    "PhononNegativePivotPreflight",
    "PhononNegativePivotWorkOrder",
    "capture_phonon_negative_pivot_code_identity",
    "execute_phonon_negative_result_pivot",
    "preflight_phonon_negative_pivot_start",
    "prepare_phonon_negative_pivot_work_order",
    "verify_phonon_negative_pivot_code_identity",
    "verify_phonon_negative_pivot_work_order",
]
