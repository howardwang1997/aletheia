"""Precommitted shadow-portfolio work order for the production phonon endurance Quest."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from decimal import Decimal
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
    verify_phonon_replay_code_identity,
)
from aletheia.programs.endurance import ResearchEnduranceNotFound, ResearchEnduranceStore
from aletheia.programs.endurance_controller import (
    EnduranceControllerManifest,
    verify_endurance_controller_code_identity,
)
from aletheia.programs.graph import ProgramGraphStore
from aletheia.programs.memory import ResearchMemoryStore
from aletheia.programs.memory_schemas import (
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    ResearchMemoryFactSpec,
    TaskContextRequest,
)
from aletheia.programs.portfolio import ResearchPortfolioNotFound, ResearchPortfolioStore
from aletheia.programs.portfolio_harness import PORTFOLIO_SELECTOR_CODE_SHA256
from aletheia.programs.portfolio_schemas import (
    PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
    HumanPortfolioPlanSpec,
    PortfolioActionSpec,
    PortfolioActionType,
    PortfolioAssessmentBatch,
    PortfolioAssessmentManifest,
    PortfolioAssessorKind,
    PortfolioCandidateAssessment,
    PortfolioCostEstimate,
    PortfolioEpochSnapshot,
    PortfolioHypothesisLikelihood,
    PortfolioInformationModel,
    PortfolioMeasurementStatus,
    PortfolioOutcomeProbability,
    PortfolioPriorProbability,
    PortfolioProposal,
    PortfolioRiskLevel,
    PortfolioSelectionPolicy,
    PortfolioSlateSnapshot,
    PortfolioSlateSpec,
)
from aletheia.programs.schemas import BudgetKind, DataRole, GraphCommandContext
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_WORK_ORDER_ID_PATTERN = r"^ppw_[0-9a-f]{32}$"
_STAGE_ID_PATTERN = r"^pps_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_FAMILY_ID_PATTERN = r"^fam_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_PROTOCOL_ID_PATTERN = r"^pirp_[0-9a-f]{32}$"
_COMMISSIONING_ID_PATTERN = r"^pcm_[0-9a-f]{32}$"
_CODE_COMPONENTS = (
    "aletheia/domains/materials/phonon_commissioning.py",
    "aletheia/domains/materials/phonon_endurance_portfolio.py",
    "aletheia/domains/materials/phonon_reproduction.py",
    "aletheia/programs/graph.py",
    "aletheia/programs/memory.py",
    "aletheia/programs/memory_schemas.py",
    "aletheia/programs/portfolio.py",
    "aletheia/programs/portfolio_harness.py",
    "aletheia/programs/portfolio_schemas.py",
    "scripts/run_phonon_endurance_portfolio.py",
)


class PhononEndurancePortfolioError(RuntimeError):
    """The production portfolio work order is invalid or used out of sequence."""


class PhononEndurancePortfolioConflict(PhononEndurancePortfolioError):
    """Frozen portfolio sources or durable state differ from the work order."""


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
        raise ValueError("portfolio artifact path must be repository-relative")
    return path


def _database_now() -> datetime:
    with engine().connect() as connection:
        observed = connection.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL contract
        raise PhononEndurancePortfolioConflict("PostgreSQL did not return a portfolio clock")
    return observed


class PhononPortfolioCodeIdentity(_FrozenModel):
    git_commit: str = Field(pattern=_GIT_SHA_PATTERN)
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_provenance_verified: bool

    @model_validator(mode="after")
    def _closed_components(self) -> "PhononPortfolioCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("phonon portfolio code-component matrix is incomplete")
        expected = content_sha256(
            {
                "schema": "aletheia.phonon_endurance_portfolio_code.v1",
                "git_commit": self.git_commit,
                "components": components,
                "committed_provenance_verified": self.committed_provenance_verified,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("phonon portfolio code identity is inconsistent")
        object.__setattr__(self, "component_sha256s", components)
        return self


class PhononPortfolioArtifact(_FrozenModel):
    relative_path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> "PhononPortfolioArtifact":
        _safe_relative(self.relative_path)
        return self


class PhononPortfolioAssessmentTemplate(_FrozenModel):
    candidate_id: str = Field(pattern=r"^pca_[0-9a-f]{32}$")
    estimated_costs: tuple[PortfolioCostEstimate, ...]
    estimated_duration_seconds: int = Field(ge=0)
    risk_level: PortfolioRiskLevel
    measurement_status: PortfolioMeasurementStatus
    measurement_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    required_capability_sha256s: tuple[str, ...]
    available_capability_sha256s: tuple[str, ...]
    required_data_roles: tuple[DataRole, ...]
    data_readiness_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    information_model: PortfolioInformationModel | None
    importance_ppm: int = Field(ge=0, le=1_000_000)
    novelty_ppm: int = Field(ge=0, le=1_000_000)
    success_probability_ppm: int = Field(ge=0, le=1_000_000)
    value_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    replication_debt_ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    replication_debt_before: int = Field(ge=0)
    expected_replication_debt_reduction: int = Field(ge=0)
    independent_replication_protocol_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    correlation_tags: tuple[str, ...]
    diversity_tags: tuple[str, ...]
    assessment_evidence_sha256s: tuple[str, ...]

    def instantiate(
        self,
        action: PortfolioActionSpec,
        *,
        completed_at: datetime,
    ) -> PortfolioCandidateAssessment:
        if action.candidate_id != self.candidate_id:
            raise PhononEndurancePortfolioConflict("assessment template changed candidate")
        return PortfolioCandidateAssessment(
            candidate_id=self.candidate_id,
            action_sha256=action.action_sha256,
            estimated_costs=self.estimated_costs,
            estimated_duration_seconds=self.estimated_duration_seconds,
            risk_level=self.risk_level,
            measurement_status=self.measurement_status,
            measurement_evidence_sha256=self.measurement_evidence_sha256,
            required_capability_sha256s=self.required_capability_sha256s,
            available_capability_sha256s=self.available_capability_sha256s,
            required_data_roles=self.required_data_roles,
            data_readiness_evidence_sha256=self.data_readiness_evidence_sha256,
            information_model=self.information_model,
            importance_ppm=self.importance_ppm,
            novelty_ppm=self.novelty_ppm,
            success_probability_ppm=self.success_probability_ppm,
            value_evidence_sha256=self.value_evidence_sha256,
            replication_debt_ledger_sha256=self.replication_debt_ledger_sha256,
            replication_debt_before=self.replication_debt_before,
            expected_replication_debt_reduction=self.expected_replication_debt_reduction,
            independent_replication_protocol_sha256=(self.independent_replication_protocol_sha256),
            correlation_tags=self.correlation_tags,
            diversity_tags=self.diversity_tags,
            approval=None,
            assessment_evidence_sha256s=self.assessment_evidence_sha256s,
            completed_at=completed_at,
        )


class PhononEndurancePortfolioWorkOrder(_FrozenModel):
    schema_version: Literal[1] = 1
    work_order_id: str | None = Field(default=None, pattern=_WORK_ORDER_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    family_id: str = Field(pattern=_FAMILY_ID_PATTERN)
    original_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    mechanism_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    external_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    commissioning_id: str = Field(pattern=_COMMISSIONING_ID_PATTERN)
    initial_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller: PhononPortfolioArtifact
    protocol: PhononPortfolioArtifact
    commissioning: PhononPortfolioArtifact
    memory_fact: ResearchMemoryFactSpec
    actions: tuple[PortfolioActionSpec, ...] = Field(min_length=4, max_length=16)
    assessment_templates: tuple[PhononPortfolioAssessmentTemplate, ...]
    code_identity: PhononPortfolioCodeIdentity
    prepared_at: AwareDatetime
    human_plan_required: Literal[True] = True
    planner_output_access_before_human_plan: Literal["none"] = "none"
    shadow_only: Literal[True] = True
    actions_enqueued: Literal[False] = False
    automatic_gate_start: Literal[False] = False

    @model_validator(mode="after")
    def _closed_work_order(self) -> "PhononEndurancePortfolioWorkOrder":
        actions = tuple(sorted(self.actions, key=lambda item: item.candidate_id))
        templates = tuple(sorted(self.assessment_templates, key=lambda item: item.candidate_id))
        if {item.candidate_id for item in actions} != {item.candidate_id for item in templates}:
            raise ValueError("portfolio templates do not cover every action")
        if self.memory_fact.scope_node_id != self.quest_id:
            raise ValueError("portfolio memory fact belongs to another scope")
        if {item.action_type for item in actions} != {
            PortfolioActionType.REPLICATION,
            PortfolioActionType.MECHANISM_TEST,
            PortfolioActionType.START_CAMPAIGN,
            PortfolioActionType.ACQUIRE_DATA,
        }:
            raise ValueError("phonon portfolio must preserve four precommitted alternatives")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "assessment_templates", templates)
        expected = f"ppw_{self.work_order_sha256[:32]}"
        if self.work_order_id is not None and self.work_order_id != expected:
            raise ValueError("portfolio work-order ID differs from its content")
        object.__setattr__(self, "work_order_id", expected)
        return self

    @property
    def work_order_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"work_order_id"}))


class PhononPortfolioPlanCandidate(_FrozenModel):
    candidate_id: str = Field(pattern=r"^pca_[0-9a-f]{32}$")
    action_type: PortfolioActionType
    target_node_id: str
    title: str


class PhononPortfolioStageReceipt(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    stage_id: str | None = Field(default=None, pattern=_STAGE_ID_PATTERN)
    slate_id: str = Field(pattern=r"^psl_[0-9a-f]{32}$")
    memory_fact_id: str = Field(pattern=r"^mem_[0-9a-f]{32}$")
    compaction_id: str = Field(pattern=r"^mcp_[0-9a-f]{32}$")
    context_receipt_id: str = Field(pattern=r"^mctx_[0-9a-f]{32}$")
    candidates: tuple[PhononPortfolioPlanCandidate, ...]
    staged_at: AwareDatetime
    human_plan_required: Literal[True] = True
    planner_output_materialized: Literal[False] = False
    actions_enqueued: Literal[False] = False

    @model_validator(mode="after")
    def _content_identity(self) -> "PhononPortfolioStageReceipt":
        candidates = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValueError("portfolio stage candidate list is not unique")
        object.__setattr__(self, "candidates", candidates)
        expected = f"pps_{self.receipt_sha256[:32]}"
        if self.stage_id is not None and self.stage_id != expected:
            raise ValueError("portfolio stage ID differs from its receipt")
        object.__setattr__(self, "stage_id", expected)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"stage_id"}))


class PhononBlindPortfolioSelection(_FrozenModel):
    selected_candidate_ids: tuple[str, ...] = Field(max_length=16)
    rationale: str = Field(min_length=1, max_length=4_000)
    planner_output_access: Literal["none"] = "none"
    human_choice_confirmed: Literal[True] = True

    @model_validator(mode="after")
    def _canonical(self) -> "PhononBlindPortfolioSelection":
        selected = tuple(sorted(set(self.selected_candidate_ids)))
        if len(selected) != len(self.selected_candidate_ids):
            raise ValueError("blind portfolio choice repeats a candidate")
        if any(not item.startswith("pca_") or len(item) != 36 for item in selected):
            raise ValueError("blind portfolio choice contains an invalid candidate")
        object.__setattr__(self, "selected_candidate_ids", selected)
        object.__setattr__(self, "rationale", self.rationale.strip())
        return self


class PhononPortfolioPlanReceipt(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    stage_id: str = Field(pattern=_STAGE_ID_PATTERN)
    slate_id: str = Field(pattern=r"^psl_[0-9a-f]{32}$")
    human_plan_id: str = Field(pattern=r"^php_[0-9a-f]{32}$")
    selected_candidate_ids: tuple[str, ...]
    human_principal: str
    committed_at: AwareDatetime
    planner_output_access: Literal["none"] = "none"
    actions_enqueued: Literal[False] = False


class PhononPortfolioStartPreflight(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    stage_id: str = Field(pattern=_STAGE_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    database_observed_at: AwareDatetime
    ready_for_explicit_gate_start: bool
    blockers: tuple[str, ...]
    human_plan_committed: bool
    planner_output_materialized: bool
    graph_verified: bool
    code_and_files_verified: bool
    actions_enqueued: Literal[False] = False
    automatic_gate_start: Literal[False] = False

    @model_validator(mode="after")
    def _verdict(self) -> "PhononPortfolioStartPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("portfolio start blockers must be canonical")
        expected = (
            not blockers
            and self.human_plan_committed
            and not self.planner_output_materialized
            and self.graph_verified
            and self.code_and_files_verified
        )
        if self.ready_for_explicit_gate_start != expected:
            raise ValueError("portfolio start-preflight verdict differs from blockers")
        return self


class PhononPortfolioEpochReceipt(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    stage_id: str = Field(pattern=_STAGE_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    epoch: PortfolioEpochSnapshot
    in_window_verified: Literal[True] = True
    shadow_only: Literal[True] = True
    actions_enqueued: Literal[False] = False
    automatic_graph_transition: Literal[False] = False


def capture_phonon_portfolio_code_identity(
    *,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononPortfolioCodeIdentity:
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
        raise PhononEndurancePortfolioConflict(
            "phonon portfolio components must be tracked and committed"
        )
    commit = _git(root, "rev-parse", "HEAD")
    projection = {
        "schema": "aletheia.phonon_endurance_portfolio_code.v1",
        "git_commit": commit,
        "components": dict(sorted(components.items())),
        "committed_provenance_verified": committed and require_committed,
    }
    return PhononPortfolioCodeIdentity(
        git_commit=commit,
        component_sha256s=components,
        aggregate_sha256=content_sha256(projection),
        committed_provenance_verified=committed and require_committed,
    )


def verify_phonon_portfolio_code_identity(
    identity: PhononPortfolioCodeIdentity,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    root = repository_root.resolve()
    live = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    if dict(sorted(live.items())) != identity.component_sha256s:
        raise PhononEndurancePortfolioConflict("live portfolio code differs from frozen identity")
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
            raise PhononEndurancePortfolioConflict(
                "frozen portfolio Git provenance cannot be reconstructed"
            ) from exc
        if dict(sorted(frozen.items())) != identity.component_sha256s:
            raise PhononEndurancePortfolioConflict(
                "frozen commit does not contain portfolio components"
            )


def _artifact(path: Path, *, content_sha256_value: str, root: Path) -> PhononPortfolioArtifact:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PhononEndurancePortfolioConflict("portfolio artifact escaped repository") from exc
    return PhononPortfolioArtifact(
        relative_path=relative,
        file_sha256=_sha256_file(resolved),
        content_sha256=content_sha256_value,
    )


def _information_model(world: Any) -> PortfolioInformationModel:
    hypotheses = tuple(sorted(world.belief_state.hypotheses, key=lambda item: item.hypothesis_id))
    ppm = [int(Decimal(str(item.probability)) * Decimal(1_000_000)) for item in hypotheses]
    ppm[-1] += 1_000_000 - sum(ppm)
    predictions = {item.hypothesis_id: item for item in world.predictions}
    outcome_ids = tuple(
        sorted(
            {
                outcome_id
                for prediction in predictions.values()
                for outcome_id in (
                    *prediction.outcome_space,
                    prediction.expected_outcome,
                )
            }
        )
    )
    likelihoods = []
    for hypothesis in hypotheses:
        expected = predictions[hypothesis.hypothesis_id].expected_outcome
        alternatives = tuple(item for item in outcome_ids if item != expected)
        if alternatives:
            remaining = 200_000
            values = {item: remaining // len(alternatives) for item in alternatives}
            values[alternatives[-1]] += remaining - sum(values.values())
            values[expected] = 800_000
        else:
            values = {expected: 1_000_000}
        likelihoods.append(
            PortfolioHypothesisLikelihood(
                hypothesis_id=hypothesis.hypothesis_id,
                outcomes=tuple(
                    PortfolioOutcomeProbability(outcome_id=item, probability_ppm=values[item])
                    for item in outcome_ids
                ),
            )
        )
    return PortfolioInformationModel(
        belief_state_sha256=content_sha256(world.belief_state),
        prediction_receipt_sha256=content_sha256(
            [item.model_dump(mode="json") for item in world.predictions]
        ),
        priors=tuple(
            PortfolioPriorProbability(
                hypothesis_id=item.hypothesis_id,
                probability_ppm=probability,
            )
            for item, probability in zip(hypotheses, ppm, strict=True)
        ),
        likelihoods=tuple(likelihoods),
    )


def _template(
    action: PortfolioActionSpec,
    *,
    information_model: PortfolioInformationModel | None,
    costs: tuple[PortfolioCostEstimate, ...],
    duration: int,
    measurement_status: PortfolioMeasurementStatus,
    measurement_sha256: str | None,
    capabilities: tuple[str, ...],
    roles: tuple[DataRole, ...],
    readiness_sha256: str | None,
    importance: int,
    novelty: int,
    success: int,
    value_sha256: str,
    debt_sha256: str,
    debt_before: int = 0,
    debt_reduction: int = 0,
    replication_protocol_sha256: str | None = None,
    correlation_tags: tuple[str, ...] = (),
    diversity_tags: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> PhononPortfolioAssessmentTemplate:
    return PhononPortfolioAssessmentTemplate(
        candidate_id=action.candidate_id,
        estimated_costs=costs,
        estimated_duration_seconds=duration,
        risk_level=PortfolioRiskLevel.LOW,
        measurement_status=measurement_status,
        measurement_evidence_sha256=measurement_sha256,
        required_capability_sha256s=capabilities,
        available_capability_sha256s=capabilities,
        required_data_roles=roles,
        data_readiness_evidence_sha256=readiness_sha256,
        information_model=information_model,
        importance_ppm=importance,
        novelty_ppm=novelty,
        success_probability_ppm=success,
        value_evidence_sha256=value_sha256,
        replication_debt_ledger_sha256=debt_sha256,
        replication_debt_before=debt_before,
        expected_replication_debt_reduction=debt_reduction,
        independent_replication_protocol_sha256=replication_protocol_sha256,
        correlation_tags=correlation_tags,
        diversity_tags=diversity_tags,
        assessment_evidence_sha256s=evidence,
    )


def prepare_phonon_endurance_portfolio_work_order(
    *,
    controller: EnduranceControllerManifest,
    controller_path: Path,
    protocol: PhononIndependentReplayProtocol,
    protocol_path: Path,
    commissioning: PhononQuestCommissioningManifest,
    commissioning_path: Path,
    prepared_at: datetime,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononEndurancePortfolioWorkOrder:
    root = repository_root.resolve()
    if (
        controller.controller_id is None
        or controller.gate_manifest.gate_id is None
        or protocol.protocol_id is None
        or commissioning.commissioning_id is None
    ):
        raise PhononEndurancePortfolioConflict("production source identity is incomplete")
    if not (controller.gate_manifest.quest_id == protocol.quest_id == commissioning.quest.node_id):
        raise PhononEndurancePortfolioConflict("portfolio sources belong to different Quests")
    if (
        protocol.controller_id != controller.controller_id
        or protocol.gate_id != controller.gate_manifest.gate_id
        or protocol.commissioning_id != commissioning.commissioning_id
    ):
        raise PhononEndurancePortfolioConflict("portfolio protocol binding changed")
    campaigns = {item.identity_key.rsplit(":", 1)[-1]: item for item in commissioning.campaigns}
    mechanism = campaigns["mechanism-ablation"]
    external = campaigns["external-calculation"]
    if protocol.reproduction_campaign_id != mechanism.node_id:
        raise PhononEndurancePortfolioConflict(
            "reproduction protocol is not bound to the mechanism/replay successor"
        )
    original = protocol.original_campaign_id
    actions = (
        PortfolioActionSpec(
            action_type=PortfolioActionType.REPLICATION,
            target_node_id=original,
            task_key="phonon:independent-implementation-replay",
            title="Run the frozen implementation-diverse same-source replay",
            rationale="Measure whether the retained signal survives independent code before stronger claims.",
        ),
        PortfolioActionSpec(
            action_type=PortfolioActionType.MECHANISM_TEST,
            target_node_id=original,
            task_key="phonon:local-vs-global-ablation",
            title="Run the local-packing versus global-lattice ablation",
            rationale="Discriminate the precommitted local, global, and null explanations.",
        ),
        PortfolioActionSpec(
            action_type=PortfolioActionType.START_CAMPAIGN,
            target_node_id=commissioning.program.node_id,
            family_id=commissioning.family.family_id,
            task_key="phonon:activate-mechanism-branch",
            title="Activate the precommitted mechanism-ablation Campaign",
            rationale="Keep a structurally different successor ready for an evidence-caused pivot.",
        ),
        PortfolioActionSpec(
            action_type=PortfolioActionType.ACQUIRE_DATA,
            target_node_id=commissioning.program.node_id,
            task_key="phonon:qualify-independent-corpus",
            title="Qualify an independently calculated phonon corpus",
            rationale="Audit lineage and target compatibility before any external target is opened.",
        ),
    )
    worlds = {item.question.kind.value: item for item in commissioning.world_models}
    mechanism_information = _information_model(worlds["mechanism"])
    replication_information = _information_model(worlds["predictive"])
    source_evidence = commissioning.evidence.evidence_sha256
    protocol_sha = protocol.protocol_sha256
    data_sha = content_sha256(commissioning.data_role)
    external_candidates_sha = content_sha256(
        [item.model_dump(mode="json") for item in commissioning.external_corpus_candidates]
    )
    capability = (protocol.code_identity.aggregate_sha256,)
    experiment_costs = (
        PortfolioCostEstimate(kind=BudgetKind.EXPERIMENT_COUNT, amount_microunits=1_000_000),
        PortfolioCostEstimate(kind=BudgetKind.GPU_HOURS, amount_microunits=2_000_000),
        PortfolioCostEstimate(kind=BudgetKind.USD, amount_microunits=5_000_000),
        PortfolioCostEstimate(kind=BudgetKind.WALL_CLOCK_HOURS, amount_microunits=8_000_000),
    )
    by_type = {item.action_type: item for item in actions}
    templates = (
        _template(
            by_type[PortfolioActionType.REPLICATION],
            information_model=replication_information,
            costs=experiment_costs,
            duration=8 * 3600,
            measurement_status=PortfolioMeasurementStatus.VALIDATED,
            measurement_sha256=source_evidence,
            capabilities=capability,
            roles=(DataRole.EXPLORATION,),
            readiness_sha256=data_sha,
            importance=900_000,
            novelty=500_000,
            success=750_000,
            value_sha256=content_sha256(
                {"value": "implementation-reproduction", "source": protocol_sha}
            ),
            debt_sha256=content_sha256({"replication_debt": 2, "quest": protocol.quest_id}),
            debt_before=2,
            debt_reduction=1,
            replication_protocol_sha256=protocol_sha,
            correlation_tags=("same-source", "structure-signal"),
            diversity_tags=("independent-code", "replication"),
            evidence=(source_evidence, protocol_sha),
        ),
        _template(
            by_type[PortfolioActionType.MECHANISM_TEST],
            information_model=mechanism_information,
            costs=experiment_costs,
            duration=12 * 3600,
            measurement_status=PortfolioMeasurementStatus.VALIDATED,
            measurement_sha256=source_evidence,
            capabilities=capability,
            roles=(DataRole.EXPLORATION,),
            readiness_sha256=data_sha,
            importance=850_000,
            novelty=700_000,
            success=650_000,
            value_sha256=content_sha256(
                {"value": "mechanism-discrimination", "source": source_evidence}
            ),
            debt_sha256=content_sha256({"replication_debt": 2, "quest": protocol.quest_id}),
            correlation_tags=("same-source", "structure-signal"),
            diversity_tags=("ablation", "mechanism"),
            evidence=(source_evidence, mechanism_information.information_model_sha256),
        ),
        _template(
            by_type[PortfolioActionType.START_CAMPAIGN],
            information_model=None,
            costs=(),
            duration=300,
            measurement_status=PortfolioMeasurementStatus.BOUNDED,
            measurement_sha256=None,
            capabilities=(),
            roles=(),
            readiness_sha256=None,
            importance=700_000,
            novelty=400_000,
            success=900_000,
            value_sha256=content_sha256(
                {"value": "precommitted-successor", "campaign": mechanism.node_id}
            ),
            debt_sha256=content_sha256({"replication_debt": 2, "quest": protocol.quest_id}),
            correlation_tags=("campaign-lifecycle",),
            diversity_tags=("strategy-transition",),
            evidence=(commissioning.manifest_sha256,),
        ),
        _template(
            by_type[PortfolioActionType.ACQUIRE_DATA],
            information_model=replication_information,
            costs=(PortfolioCostEstimate(kind=BudgetKind.USD, amount_microunits=10_000_000),),
            duration=24 * 3600,
            measurement_status=PortfolioMeasurementStatus.BOUNDED,
            measurement_sha256=None,
            capabilities=(),
            roles=(DataRole.EXTERNAL_VALIDATION,),
            readiness_sha256=external_candidates_sha,
            importance=950_000,
            novelty=800_000,
            success=350_000,
            value_sha256=content_sha256(
                {
                    "value": "external-transfer",
                    "candidates_sha256": external_candidates_sha,
                }
            ),
            debt_sha256=content_sha256({"replication_debt": 2, "quest": protocol.quest_id}),
            correlation_tags=("external-data",),
            diversity_tags=("domain-shift", "external-validation"),
            evidence=(external_candidates_sha,),
        ),
    )
    controller_artifact = _artifact(
        controller_path,
        content_sha256_value=controller.manifest_sha256,
        root=root,
    )
    protocol_artifact = _artifact(
        protocol_path,
        content_sha256_value=protocol.protocol_sha256,
        root=root,
    )
    commissioning_artifact = _artifact(
        commissioning_path,
        content_sha256_value=commissioning.manifest_sha256,
        root=root,
    )
    memory_fact = ResearchMemoryFactSpec(
        scope_node_id=protocol.quest_id,
        kind=MemoryFactKind.GOAL,
        statement=(
            "Choose a bounded in-window phonon research portfolio that prioritizes falsifiable "
            "replication and mechanism discrimination without external allocation or claim inflation."
        ),
        detail={
            "work_order": "phonon_endurance_portfolio_v1",
            "gate_id": protocol.gate_id,
            "candidate_ids": sorted(item.candidate_id for item in actions),
            "same_source_claim_ceiling": True,
            "autonomous_allocation_enabled": False,
        },
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="portfolio-plan",
                context_role=MemoryContextRole.REQUIRED,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=commissioning.commissioning_id,
                sha256=commissioning_artifact.file_sha256,
                uri=commissioning_artifact.relative_path,
            ),
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=protocol.protocol_id,
                sha256=protocol_artifact.file_sha256,
                uri=protocol_artifact.relative_path,
            ),
        ),
    )
    return PhononEndurancePortfolioWorkOrder(
        quest_id=protocol.quest_id,
        program_id=commissioning.program.node_id,
        family_id=commissioning.family.family_id,
        original_campaign_id=original,
        mechanism_campaign_id=mechanism.node_id,
        external_campaign_id=external.node_id,
        gate_id=protocol.gate_id,
        controller_id=controller.controller_id,
        protocol_id=protocol.protocol_id,
        commissioning_id=commissioning.commissioning_id,
        initial_graph_sha256=controller.gate_manifest.initial_graph_sha256,
        controller=controller_artifact,
        protocol=protocol_artifact,
        commissioning=commissioning_artifact,
        memory_fact=memory_fact,
        actions=actions,
        assessment_templates=templates,
        code_identity=capture_phonon_portfolio_code_identity(
            repository_root=root,
            require_committed=require_committed,
        ),
        prepared_at=prepared_at,
    )


def _load_bound(
    binding: PhononPortfolioArtifact,
    model: type[BaseModel],
    *,
    repository_root: Path,
) -> BaseModel:
    relative = _safe_relative(binding.relative_path)
    root = repository_root.resolve()
    path = (root / Path(*relative.parts)).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PhononEndurancePortfolioConflict(
            "portfolio bound artifact resolved outside repository"
        ) from exc
    if _sha256_file(path) != binding.file_sha256:
        raise PhononEndurancePortfolioConflict("portfolio bound artifact bytes changed")
    value = model.model_validate_json(path.read_bytes())
    content = getattr(value, "manifest_sha256", None) or getattr(value, "protocol_sha256", None)
    if content != binding.content_sha256:
        raise PhononEndurancePortfolioConflict("portfolio bound artifact content changed")
    return value


def verify_phonon_endurance_portfolio_work_order(
    work_order: PhononEndurancePortfolioWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
) -> tuple[
    EnduranceControllerManifest,
    PhononIndependentReplayProtocol,
    PhononQuestCommissioningManifest,
]:
    root = repository_root.resolve()
    verify_phonon_portfolio_code_identity(work_order.code_identity, repository_root=root)
    controller = _load_bound(
        work_order.controller, EnduranceControllerManifest, repository_root=root
    )
    protocol = _load_bound(
        work_order.protocol, PhononIndependentReplayProtocol, repository_root=root
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
    ):
        raise PhononEndurancePortfolioConflict("portfolio work-order source binding changed")
    graph = ProgramGraphStore().get_quest(work_order.quest_id)
    if graph.graph_sha256 != work_order.initial_graph_sha256:
        raise PhononEndurancePortfolioConflict("portfolio Quest graph changed before staging")
    return controller, protocol, commissioning


def _ensure_not_started(work_order: PhononEndurancePortfolioWorkOrder) -> None:
    try:
        ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound:
        return
    raise PhononEndurancePortfolioConflict("portfolio pre-start operation found a started gate")


def stage_phonon_endurance_portfolio(
    work_order: PhononEndurancePortfolioWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioStageReceipt:
    verify_phonon_endurance_portfolio_work_order(
        work_order,
        repository_root=repository_root,
    )
    _ensure_not_started(work_order)
    assert work_order.work_order_id is not None
    memory = ResearchMemoryStore()
    prefix = work_order.work_order_id
    fact = memory.register_fact(
        work_order.memory_fact,
        GraphCommandContext(
            idempotency_key=f"{prefix}:memory-fact",
            principal="planner:phonon-portfolio",
        ),
        now=_database_now(),
    )
    eligible = memory.eligible_facts(work_order.quest_id, "portfolio-plan")
    if tuple(item.fact_id for item in eligible) != (work_order.memory_fact.fact_id,):
        raise PhononEndurancePortfolioConflict(
            "portfolio-plan memory contains unreviewed additional facts"
        )
    draft = MemorySummaryDraft(
        producer_provider="aletheia",
        producer_model="deterministic-phonon-portfolio-context-v1",
        prompt_sha256=content_sha256({"template": "phonon_endurance_portfolio_context_v1"}),
        summary_text=(
            "Prioritize the frozen implementation replay and discriminating mechanism work; "
            "keep external data unavailable until lineage and target compatibility are audited."
        ),
        covered_fact_ids=(work_order.memory_fact.fact_id,),
    )
    compaction = memory.compact(
        scope_node_id=work_order.quest_id,
        task_key="portfolio-plan",
        draft=draft,
        context=GraphCommandContext(
            idempotency_key=f"{prefix}:memory-compact",
            principal="harness:phonon-memory",
        ),
        now=_database_now(),
    )
    context_receipt = memory.build_task_context(
        TaskContextRequest(
            scope_node_id=work_order.quest_id,
            task_key="portfolio-plan",
            compaction_id=compaction.object_id,
            consumer_provider="aletheia",
            consumer_model="deterministic-phonon-portfolio-v1",
        ),
        GraphCommandContext(
            idempotency_key=f"{prefix}:memory-context",
            principal="scheduler:phonon-portfolio",
        ),
        now=_database_now(),
    )
    frozen_at = context_receipt.command.committed_at
    policy = PortfolioSelectionPolicy(
        policy_id=f"phonon-endurance-{work_order.work_order_id}",
        quest_id=work_order.quest_id,
        maximum_selected_actions=2,
        maximum_actions_per_program=2,
        maximum_actions_per_family=2,
        maximum_actions_per_correlation_tag=1,
        maximum_duration_seconds=48 * 3600,
        maximum_risk_level=PortfolioRiskLevel.LOW,
        minimum_expected_information_gain_ratio_ppm=10_000,
        minimum_replication_actions=1,
        required_approval_risks=(PortfolioRiskLevel.MODERATE, PortfolioRiskLevel.HIGH),
        selector_code_sha256=PORTFOLIO_SELECTOR_CODE_SHA256,
        frozen_at=frozen_at,
    )
    proposal = PortfolioProposal(
        quest_id=work_order.quest_id,
        graph_sha256=work_order.initial_graph_sha256,
        memory_context_receipt_id=context_receipt.context_receipt_id,
        proposer_principal="planner:phonon-portfolio",
        proposer_provider="aletheia",
        proposer_model="deterministic-phonon-portfolio-v1",
        proposer_model_identity_sha256=work_order.code_identity.aggregate_sha256,
        prompt_sha256=content_sha256(
            {
                "work_order_id": work_order.work_order_id,
                "context_sha256": context_receipt.context.context_sha256,
                "candidate_ids": [item.candidate_id for item in work_order.actions],
            }
        ),
        candidates=work_order.actions,
        generated_at=frozen_at,
    )
    actions = {item.candidate_id: item for item in work_order.actions}
    assessments = tuple(
        template.instantiate(actions[template.candidate_id], completed_at=frozen_at)
        for template in work_order.assessment_templates
    )
    batch = PortfolioAssessmentBatch(
        manifest=PortfolioAssessmentManifest(
            assessor_principal="assessor:phonon-portfolio-evidence",
            assessor_kind=PortfolioAssessorKind.DETERMINISTIC_HARNESS,
            assessor_code_sha256=work_order.code_identity.aggregate_sha256,
            output_schema_sha256=PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
            observation_access="none",
            frozen_at=frozen_at,
        ),
        assessments=assessments,
        completed_at=frozen_at,
    )
    spec = PortfolioSlateSpec(policy=policy, proposal=proposal, assessment_batch=batch)
    portfolio = ResearchPortfolioStore()
    slate_receipt = portfolio.register_slate(
        spec,
        GraphCommandContext(
            idempotency_key=f"{prefix}:slate",
            principal="controller:phonon-portfolio",
        ),
        now=_database_now(),
    )
    snapshot = portfolio.get_slate(spec.slate_id)
    if snapshot.human_plan_id is not None or snapshot.epoch_id is not None:
        raise PhononEndurancePortfolioConflict(
            "portfolio staging unexpectedly materialized plan or planner output"
        )
    return PhononPortfolioStageReceipt(
        work_order_id=work_order.work_order_id,
        slate_id=spec.slate_id,
        memory_fact_id=fact.object_id,
        compaction_id=compaction.object_id,
        context_receipt_id=context_receipt.context_receipt_id,
        candidates=tuple(
            PhononPortfolioPlanCandidate(
                candidate_id=item.candidate_id,
                action_type=item.action_type,
                target_node_id=item.target_node_id,
                title=item.title,
            )
            for item in work_order.actions
        ),
        staged_at=slate_receipt.command.committed_at,
    )


def _verify_stage(
    work_order: PhononEndurancePortfolioWorkOrder,
    stage: PhononPortfolioStageReceipt,
) -> PortfolioSlateSnapshot:
    if stage.work_order_id != work_order.work_order_id:
        raise PhononEndurancePortfolioConflict("portfolio stage belongs to another work order")
    expected = tuple(
        sorted(
            (
                PhononPortfolioPlanCandidate(
                    candidate_id=item.candidate_id,
                    action_type=item.action_type,
                    target_node_id=item.target_node_id,
                    title=item.title,
                )
                for item in work_order.actions
            ),
            key=lambda item: item.candidate_id,
        )
    )
    if stage.candidates != expected:
        raise PhononEndurancePortfolioConflict("portfolio stage candidate projection changed")
    try:
        slate = ResearchPortfolioStore().get_slate(stage.slate_id)
    except ResearchPortfolioNotFound as exc:
        raise PhononEndurancePortfolioConflict("portfolio staged slate is missing") from exc
    if (
        slate.spec.proposal.memory_context_receipt_id != stage.context_receipt_id
        or slate.spec.slate_id != stage.slate_id
    ):
        raise PhononEndurancePortfolioConflict("portfolio staged slate binding changed")
    return slate


def commit_phonon_blind_portfolio_plan(
    work_order: PhononEndurancePortfolioWorkOrder,
    stage: PhononPortfolioStageReceipt,
    selection: PhononBlindPortfolioSelection,
    *,
    human_principal: str,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioPlanReceipt:
    verify_phonon_endurance_portfolio_work_order(
        work_order,
        repository_root=repository_root,
    )
    _ensure_not_started(work_order)
    if not human_principal.startswith("human:"):
        raise PhononEndurancePortfolioConflict(
            "portfolio baseline requires an explicit human:* principal"
        )
    selection = PhononBlindPortfolioSelection.model_validate(selection.model_dump(mode="python"))
    slate = _verify_stage(work_order, stage)
    known = {item.candidate_id for item in stage.candidates}
    if not set(selection.selected_candidate_ids).issubset(known):
        raise PhononEndurancePortfolioConflict("human plan selected an unknown candidate")
    if slate.epoch_id is not None:
        raise PhononEndurancePortfolioConflict("planner output already materialized")
    if slate.human_plan is None:
        plan = HumanPortfolioPlanSpec(
            selected_candidate_ids=selection.selected_candidate_ids,
            rationale=selection.rationale,
            issued_at=_database_now(),
        )
    else:
        plan = slate.human_plan
        if (
            plan.selected_candidate_ids != selection.selected_candidate_ids
            or plan.rationale != selection.rationale
        ):
            raise PhononEndurancePortfolioConflict(
                "existing blind human plan differs from requested replay"
            )
    assert work_order.work_order_id is not None
    receipt = ResearchPortfolioStore().commit_human_plan(
        slate_id=stage.slate_id,
        plan=plan,
        context=GraphCommandContext(
            idempotency_key=f"{work_order.work_order_id}:human-plan",
            principal=human_principal,
        ),
        now=_database_now(),
    )
    return PhononPortfolioPlanReceipt(
        work_order_id=work_order.work_order_id,
        stage_id=str(stage.stage_id),
        slate_id=stage.slate_id,
        human_plan_id=receipt.object_id,
        selected_candidate_ids=plan.selected_candidate_ids,
        human_principal=human_principal,
        committed_at=receipt.command.committed_at,
    )


def preflight_phonon_portfolio_start(
    work_order: PhononEndurancePortfolioWorkOrder,
    stage: PhononPortfolioStageReceipt,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioStartPreflight:
    blockers: list[str] = []
    code_files = graph_ok = False
    try:
        verify_phonon_endurance_portfolio_work_order(
            work_order,
            repository_root=repository_root,
        )
        code_files = True
        graph_ok = True
    except (RuntimeError, OSError, ValueError):
        blockers.append("work_order:code_files_or_graph_changed")
    try:
        _ensure_not_started(work_order)
    except RuntimeError:
        blockers.append("gate:already_started")
    plan = epoch = False
    try:
        slate = _verify_stage(work_order, stage)
        plan = slate.human_plan_id is not None
        epoch = slate.epoch_id is not None
    except RuntimeError:
        blockers.append("stage:missing_or_changed")
    if not plan:
        blockers.append("human_plan:not_committed")
    if epoch:
        blockers.append("planner_output:materialized_before_gate")
    canonical = tuple(sorted(set(blockers)))
    assert work_order.work_order_id is not None
    return PhononPortfolioStartPreflight(
        work_order_id=work_order.work_order_id,
        stage_id=str(stage.stage_id),
        gate_id=work_order.gate_id,
        database_observed_at=_database_now(),
        ready_for_explicit_gate_start=(
            not canonical and plan and not epoch and graph_ok and code_files
        ),
        blockers=canonical,
        human_plan_committed=plan,
        planner_output_materialized=epoch,
        graph_verified=graph_ok,
        code_and_files_verified=code_files,
    )


def evaluate_phonon_endurance_portfolio(
    work_order: PhononEndurancePortfolioWorkOrder,
    stage: PhononPortfolioStageReceipt,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioEpochReceipt:
    controller, _, _ = verify_phonon_endurance_portfolio_work_order(
        work_order,
        repository_root=repository_root,
    )
    try:
        gate = ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound as exc:
        raise PhononEndurancePortfolioConflict(
            "portfolio evaluation requires the explicit gate start"
        ) from exc
    if gate.manifest != controller.gate_manifest or gate.report is not None:
        raise PhononEndurancePortfolioConflict("portfolio evaluation found another/terminal gate")
    slate = _verify_stage(work_order, stage)
    if slate.human_plan_id is None:
        raise PhononEndurancePortfolioConflict("portfolio evaluation requires a human plan")
    assert work_order.work_order_id is not None
    mutation = ResearchPortfolioStore().evaluate_slate(
        slate_id=stage.slate_id,
        context=GraphCommandContext(
            idempotency_key=f"{work_order.work_order_id}:evaluate",
            principal="harness:phonon-portfolio",
        ),
        now=_database_now(),
    )
    epoch = ResearchPortfolioStore().get_epoch(mutation.object_id)
    if epoch.evaluated_at < gate.started_at:
        raise PhononEndurancePortfolioConflict("portfolio epoch predates the endurance window")
    if not epoch.decision.shadow_only or epoch.decision.actions_enqueued:
        raise PhononEndurancePortfolioConflict("portfolio epoch escaped shadow mode")
    return PhononPortfolioEpochReceipt(
        work_order_id=work_order.work_order_id,
        stage_id=str(stage.stage_id),
        gate_id=work_order.gate_id,
        epoch=epoch,
    )


__all__ = [
    "PhononBlindPortfolioSelection",
    "PhononEndurancePortfolioConflict",
    "PhononEndurancePortfolioError",
    "PhononEndurancePortfolioWorkOrder",
    "PhononPortfolioArtifact",
    "PhononPortfolioCodeIdentity",
    "PhononPortfolioEpochReceipt",
    "PhononPortfolioPlanCandidate",
    "PhononPortfolioPlanReceipt",
    "PhononPortfolioStageReceipt",
    "PhononPortfolioStartPreflight",
    "capture_phonon_portfolio_code_identity",
    "commit_phonon_blind_portfolio_plan",
    "evaluate_phonon_endurance_portfolio",
    "preflight_phonon_portfolio_start",
    "prepare_phonon_endurance_portfolio_work_order",
    "stage_phonon_endurance_portfolio",
    "verify_phonon_endurance_portfolio_work_order",
    "verify_phonon_portfolio_code_identity",
]
