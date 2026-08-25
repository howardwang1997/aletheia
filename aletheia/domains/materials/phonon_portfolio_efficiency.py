"""Derive the phonon endurance efficiency receipt from a blind shadow portfolio epoch."""

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
from aletheia.domains.materials.phonon_endurance_portfolio import (
    PhononEndurancePortfolioWorkOrder,
    PhononPortfolioStageReceipt,
    verify_phonon_endurance_portfolio_work_order,
    verify_phonon_portfolio_code_identity,
)
from aletheia.programs.endurance import ResearchEnduranceNotFound, ResearchEnduranceStore
from aletheia.programs.endurance_schemas import (
    EnduranceEfficiencyMetric,
    EnduranceEfficiencyReceipt,
)
from aletheia.programs.endurance_controller import (
    EnduranceControllerManifest,
    verify_endurance_controller_code_identity,
)
from aletheia.programs.portfolio import ResearchPortfolioStore
from aletheia.programs.portfolio_schemas import (
    PortfolioActionType,
    PortfolioEpochDisposition,
    PortfolioEpochSnapshot,
    PortfolioSlateSnapshot,
)
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_WORK_ORDER_ID_PATTERN = r"^ppew_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_PORTFOLIO_WORK_ORDER_ID_PATTERN = r"^ppw_[0-9a-f]{32}$"
_STAGE_ID_PATTERN = r"^pps_[0-9a-f]{32}$"
_SLATE_ID_PATTERN = r"^psl_[0-9a-f]{32}$"
_HUMAN_PLAN_ID_PATTERN = r"^php_[0-9a-f]{32}$"
_CANDIDATE_ID_PATTERN = r"^pca_[0-9a-f]{32}$"
_CODE_COMPONENTS = (
    "aletheia/domains/materials/phonon_commissioning.py",
    "aletheia/domains/materials/phonon_endurance_portfolio.py",
    "aletheia/domains/materials/phonon_portfolio_efficiency.py",
    "aletheia/programs/endurance.py",
    "aletheia/programs/endurance_controller.py",
    "aletheia/programs/endurance_schemas.py",
    "aletheia/programs/portfolio.py",
    "aletheia/programs/portfolio_harness.py",
    "aletheia/programs/portfolio_schemas.py",
    "scripts/run_phonon_portfolio_efficiency.py",
)


class PhononPortfolioEfficiencyError(RuntimeError):
    """The frozen portfolio efficiency assessment is invalid or out of sequence."""


class PhononPortfolioEfficiencyConflict(PhononPortfolioEfficiencyError):
    """Efficiency inputs or durable portfolio state differ from the work order."""


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
        raise ValueError("phonon efficiency artifact path must be repository-relative")
    return path


def _database_now() -> datetime:
    with engine().connect() as connection:
        observed = connection.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise PhononPortfolioEfficiencyConflict(
            "PostgreSQL did not return an aware efficiency clock"
        )
    return observed


class PhononPortfolioEfficiencyCodeIdentity(_FrozenModel):
    git_commit: str = Field(pattern=_GIT_SHA_PATTERN)
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_provenance_verified: bool

    @model_validator(mode="after")
    def _closed_identity(self) -> "PhononPortfolioEfficiencyCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("phonon efficiency code-component matrix is incomplete")
        expected = content_sha256(
            {
                "schema": "aletheia.phonon_portfolio_efficiency_code.v1",
                "git_commit": self.git_commit,
                "components": components,
                "committed_provenance_verified": self.committed_provenance_verified,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("phonon efficiency code identity is inconsistent")
        object.__setattr__(self, "component_sha256s", components)
        return self


class PhononPortfolioEfficiencyArtifact(_FrozenModel):
    relative_path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> "PhononPortfolioEfficiencyArtifact":
        _safe_relative(self.relative_path)
        return self


class PhononPortfolioEfficiencyCandidate(_FrozenModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    action_type: PortfolioActionType
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimated_duration_seconds: int = Field(gt=0)


class PhononPortfolioEfficiencyWorkOrder(_FrozenModel):
    schema_version: Literal[1] = 1
    work_order_id: str | None = Field(default=None, pattern=_WORK_ORDER_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    portfolio_work_order_id: str = Field(pattern=_PORTFOLIO_WORK_ORDER_ID_PATTERN)
    portfolio_stage_id: str = Field(pattern=_STAGE_ID_PATTERN)
    slate_id: str = Field(pattern=_SLATE_ID_PATTERN)
    human_plan_id: str = Field(pattern=_HUMAN_PLAN_ID_PATTERN)
    baseline_candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    candidates: tuple[PhononPortfolioEfficiencyCandidate, ...]
    baseline_value_units: int = Field(gt=0)
    baseline_cost_microunits: int = Field(gt=0)
    portfolio_work_order: PhononPortfolioEfficiencyArtifact
    portfolio_stage: PhononPortfolioEfficiencyArtifact
    minimum_improvement_ppm: int = Field(ge=1)
    assessed_by: str = Field(min_length=1, max_length=128)
    code_identity: PhononPortfolioEfficiencyCodeIdentity
    prepared_at: AwareDatetime
    metric: Literal[EnduranceEfficiencyMetric.QUESTION_COVERAGE] = (
        EnduranceEfficiencyMetric.QUESTION_COVERAGE
    )
    value_basis: Literal["distinct_frozen_question_coverage"] = (
        "distinct_frozen_question_coverage"
    )
    cost_basis: Literal["estimated_duration_microseconds"] = (
        "estimated_duration_microseconds"
    )
    expected_not_realized_scientific_efficiency: Literal[True] = True
    shadow_actions_enqueued: Literal[False] = False

    @model_validator(mode="after")
    def _closed_work_order(self) -> "PhononPortfolioEfficiencyWorkOrder":
        candidates = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        ids = [item.candidate_id for item in candidates]
        if ids != sorted(set(ids)) or self.baseline_candidate_id not in ids:
            raise ValueError("phonon efficiency candidate mapping is incomplete")
        baseline = next(
            item for item in candidates if item.candidate_id == self.baseline_candidate_id
        )
        if self.baseline_value_units != 1:
            raise ValueError("phonon efficiency baseline must cover exactly one question")
        if self.baseline_cost_microunits != baseline.estimated_duration_seconds * 1_000_000:
            raise ValueError("phonon efficiency baseline cost changed from frozen duration")
        object.__setattr__(self, "candidates", candidates)
        expected = f"ppew_{self.work_order_sha256[:32]}"
        if self.work_order_id is not None and self.work_order_id != expected:
            raise ValueError("phonon efficiency work-order ID differs from content")
        object.__setattr__(self, "work_order_id", expected)
        return self

    @property
    def work_order_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"work_order_id"}))


class PhononPortfolioEfficiencyPreflight(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    database_observed_at: AwareDatetime
    ready_for_explicit_gate_start: bool
    blockers: tuple[str, ...]
    code_and_files_verified: bool
    blind_baseline_verified: bool
    planner_output_materialized: bool
    expected_not_realized_scientific_efficiency: Literal[True] = True

    @model_validator(mode="after")
    def _derived_verdict(self) -> "PhononPortfolioEfficiencyPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("phonon efficiency preflight blockers must be canonical")
        expected = (
            not blockers
            and self.code_and_files_verified
            and self.blind_baseline_verified
            and not self.planner_output_materialized
        )
        if self.ready_for_explicit_gate_start != expected:
            raise ValueError("phonon efficiency preflight verdict differs from blockers")
        return self


class PhononPortfolioEfficiencyAssessment(_FrozenModel):
    work_order_id: str = Field(pattern=_WORK_ORDER_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    epoch_id: str = Field(pattern=r"^pep_[0-9a-f]{32}$")
    baseline_candidate_ids: tuple[str, ...]
    planner_candidate_ids: tuple[str, ...]
    baseline_question_sha256s: tuple[str, ...]
    planner_question_sha256s: tuple[str, ...]
    receipt: EnduranceEfficiencyReceipt
    minimum_improvement_ppm: int = Field(ge=1)
    meets_gate_floor: bool
    expected_not_realized_scientific_efficiency: Literal[True] = True
    actions_enqueued: Literal[False] = False

    @model_validator(mode="after")
    def _closed_assessment(self) -> "PhononPortfolioEfficiencyAssessment":
        if self.meets_gate_floor != (
            self.receipt.improvement_ppm >= self.minimum_improvement_ppm
        ):
            raise ValueError("phonon efficiency gate-floor verdict is not derived")
        if self.receipt.metric is not EnduranceEfficiencyMetric.QUESTION_COVERAGE:
            raise ValueError("phonon efficiency receipt changed metric")
        return self


def capture_phonon_portfolio_efficiency_code_identity(
    *,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononPortfolioEfficiencyCodeIdentity:
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
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency components must be tracked and committed"
        )
    git_commit = _git(root, "rev-parse", "HEAD")
    projection = {
        "schema": "aletheia.phonon_portfolio_efficiency_code.v1",
        "git_commit": git_commit,
        "components": dict(sorted(components.items())),
        "committed_provenance_verified": committed and require_committed,
    }
    return PhononPortfolioEfficiencyCodeIdentity(
        git_commit=git_commit,
        component_sha256s=components,
        aggregate_sha256=content_sha256(projection),
        committed_provenance_verified=committed and require_committed,
    )


def verify_phonon_portfolio_efficiency_code_identity(
    identity: PhononPortfolioEfficiencyCodeIdentity,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    root = repository_root.resolve()
    live = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    if dict(sorted(live.items())) != identity.component_sha256s:
        raise PhononPortfolioEfficiencyConflict(
            "live phonon efficiency code differs from frozen identity"
        )
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
            raise PhononPortfolioEfficiencyConflict(
                "frozen phonon efficiency Git provenance cannot be reconstructed"
            ) from exc
        if dict(sorted(frozen.items())) != identity.component_sha256s:
            raise PhononPortfolioEfficiencyConflict(
                "frozen commit does not contain phonon efficiency components"
            )


def _artifact(
    path: Path,
    *,
    content_sha256_value: str,
    root: Path,
) -> PhononPortfolioEfficiencyArtifact:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency artifact escaped repository"
        ) from exc
    return PhononPortfolioEfficiencyArtifact(
        relative_path=relative,
        file_sha256=_sha256_file(resolved),
        content_sha256=content_sha256_value,
    )


def _load_bound(
    binding: Any,
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
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency artifact resolved outside repository"
        ) from exc
    if _sha256_file(path) != binding.file_sha256:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency artifact bytes changed")
    value = model.model_validate_json(path.read_bytes())
    content = (
        getattr(value, "manifest_sha256", None)
        or getattr(value, "protocol_sha256", None)
        or getattr(value, "work_order_sha256", None)
        or getattr(value, "receipt_sha256", None)
    )
    if content != binding.content_sha256:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency artifact content changed")
    return value


def _question_mapping(
    portfolio: PhononEndurancePortfolioWorkOrder,
    commissioning: Any,
) -> tuple[PhononPortfolioEfficiencyCandidate, ...]:
    worlds = {item.question.kind.value: item for item in commissioning.world_models}
    predictive = worlds["predictive"].question.question_sha256
    mechanism = worlds["mechanism"].question.question_sha256
    question_by_type = {
        PortfolioActionType.REPLICATION: predictive,
        PortfolioActionType.MECHANISM_TEST: mechanism,
        PortfolioActionType.START_CAMPAIGN: mechanism,
        PortfolioActionType.ACQUIRE_DATA: predictive,
    }
    templates = {item.candidate_id: item for item in portfolio.assessment_templates}
    return tuple(
        sorted(
            (
                PhononPortfolioEfficiencyCandidate(
                    candidate_id=action.candidate_id,
                    action_type=action.action_type,
                    question_sha256=question_by_type[action.action_type],
                    estimated_duration_seconds=templates[
                        action.candidate_id
                    ].estimated_duration_seconds,
                )
                for action in portfolio.actions
            ),
            key=lambda item: item.candidate_id,
        )
    )


def _verify_stage_and_slate(
    portfolio: PhononEndurancePortfolioWorkOrder,
    stage: PhononPortfolioStageReceipt,
) -> PortfolioSlateSnapshot:
    if stage.work_order_id != portfolio.work_order_id:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency stage uses another work order")
    expected_ids = tuple(sorted(item.candidate_id for item in portfolio.actions))
    if tuple(item.candidate_id for item in stage.candidates) != expected_ids:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency stage candidates changed")
    slate = ResearchPortfolioStore().get_slate(stage.slate_id)
    if (
        slate.spec.slate_id != stage.slate_id
        or slate.spec.proposal.memory_context_receipt_id != stage.context_receipt_id
    ):
        raise PhononPortfolioEfficiencyConflict("phonon efficiency slate binding changed")
    return slate


def prepare_phonon_portfolio_efficiency_work_order(
    *,
    portfolio: PhononEndurancePortfolioWorkOrder,
    portfolio_path: Path,
    stage: PhononPortfolioStageReceipt,
    stage_path: Path,
    prepared_at: datetime,
    assessed_by: str = "harness:phonon-portfolio-efficiency",
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononPortfolioEfficiencyWorkOrder:
    root = repository_root.resolve()
    controller, _, commissioning = verify_phonon_endurance_portfolio_work_order(
        portfolio,
        repository_root=root,
    )
    slate = _verify_stage_and_slate(portfolio, stage)
    if slate.human_plan_id is None or slate.human_plan is None:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency requires the blind human baseline"
        )
    if slate.epoch_id is not None:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency work order must precede planner output"
        )
    selected = slate.human_plan.selected_candidate_ids
    if len(selected) != 1:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency requires a one-candidate human baseline"
        )
    candidates = _question_mapping(portfolio, commissioning)
    by_id = {item.candidate_id: item for item in candidates}
    baseline = by_id[selected[0]]
    if baseline.action_type not in {
        PortfolioActionType.REPLICATION,
        PortfolioActionType.MECHANISM_TEST,
    }:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency baseline must be one scientific experiment candidate"
        )
    if assessed_by in {
        controller.principal,
        slate.spec.proposal.proposer_principal,
        slate.spec.assessment_batch.manifest.assessor_principal,
        "harness:phonon-portfolio",
    }:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency assessor must be independent from source roles"
        )
    assert portfolio.work_order_id is not None
    assert stage.stage_id is not None
    assert controller.controller_id is not None
    return PhononPortfolioEfficiencyWorkOrder(
        quest_id=portfolio.quest_id,
        gate_id=portfolio.gate_id,
        controller_id=controller.controller_id,
        portfolio_work_order_id=portfolio.work_order_id,
        portfolio_stage_id=stage.stage_id,
        slate_id=stage.slate_id,
        human_plan_id=slate.human_plan_id,
        baseline_candidate_id=selected[0],
        candidates=candidates,
        baseline_value_units=1,
        baseline_cost_microunits=baseline.estimated_duration_seconds * 1_000_000,
        portfolio_work_order=_artifact(
            portfolio_path,
            content_sha256_value=portfolio.work_order_sha256,
            root=root,
        ),
        portfolio_stage=_artifact(
            stage_path,
            content_sha256_value=stage.receipt_sha256,
            root=root,
        ),
        minimum_improvement_ppm=controller.gate_manifest.minimum_efficiency_improvement_ppm,
        assessed_by=assessed_by,
        code_identity=capture_phonon_portfolio_efficiency_code_identity(
            repository_root=root,
            require_committed=require_committed,
        ),
        prepared_at=prepared_at,
    )


def _load_efficiency_sources(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    *,
    repository_root: Path,
) -> tuple[PhononEndurancePortfolioWorkOrder, PhononPortfolioStageReceipt]:
    root = repository_root.resolve()
    verify_phonon_portfolio_efficiency_code_identity(
        work_order.code_identity,
        repository_root=root,
    )
    portfolio = _load_bound(
        work_order.portfolio_work_order,
        PhononEndurancePortfolioWorkOrder,
        repository_root=root,
    )
    stage = _load_bound(
        work_order.portfolio_stage,
        PhononPortfolioStageReceipt,
        repository_root=root,
    )
    assert isinstance(portfolio, PhononEndurancePortfolioWorkOrder)
    assert isinstance(stage, PhononPortfolioStageReceipt)
    verify_phonon_portfolio_code_identity(portfolio.code_identity, repository_root=root)
    controller = _load_bound(
        portfolio.controller,
        EnduranceControllerManifest,
        repository_root=root,
    )
    commissioning = _load_bound(
        portfolio.commissioning,
        PhononQuestCommissioningManifest,
        repository_root=root,
    )
    assert isinstance(controller, EnduranceControllerManifest)
    assert isinstance(commissioning, PhononQuestCommissioningManifest)
    verify_endurance_controller_code_identity(controller.code_identity, repository_root=root)
    verify_commissioning_artifacts(commissioning, repository_root=root)
    if (
        portfolio.work_order_id != work_order.portfolio_work_order_id
        or portfolio.gate_id != work_order.gate_id
        or portfolio.controller_id != work_order.controller_id
        or stage.stage_id != work_order.portfolio_stage_id
        or stage.slate_id != work_order.slate_id
        or controller.controller_id != work_order.controller_id
        or controller.gate_manifest.quest_id != work_order.quest_id
        or controller.gate_manifest.minimum_efficiency_improvement_ppm
        != work_order.minimum_improvement_ppm
        or _question_mapping(portfolio, commissioning) != work_order.candidates
    ):
        raise PhononPortfolioEfficiencyConflict("phonon efficiency source binding changed")
    return portfolio, stage


def verify_phonon_portfolio_efficiency_work_order(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
    require_no_epoch: bool = False,
) -> tuple[PhononEndurancePortfolioWorkOrder, PhononPortfolioStageReceipt, PortfolioSlateSnapshot]:
    portfolio, stage = _load_efficiency_sources(
        work_order,
        repository_root=repository_root,
    )
    if require_no_epoch:
        verify_phonon_endurance_portfolio_work_order(
            portfolio,
            repository_root=repository_root,
        )
    slate = _verify_stage_and_slate(portfolio, stage)
    if (
        slate.human_plan_id != work_order.human_plan_id
        or slate.human_plan is None
        or slate.human_plan.selected_candidate_ids != (work_order.baseline_candidate_id,)
    ):
        raise PhononPortfolioEfficiencyConflict("phonon efficiency human baseline changed")
    if work_order.assessed_by in {
        slate.spec.proposal.proposer_principal,
        slate.spec.assessment_batch.manifest.assessor_principal,
        "harness:phonon-portfolio",
    }:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency assessor is not independent from source roles"
        )
    if require_no_epoch and slate.epoch_id is not None:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency planner output already exists")
    return portfolio, stage, slate


def preflight_phonon_portfolio_efficiency_start(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioEfficiencyPreflight:
    blockers: list[str] = []
    code_files = baseline = epoch = False
    try:
        _, _, slate = verify_phonon_portfolio_efficiency_work_order(
            work_order,
            repository_root=repository_root,
            require_no_epoch=True,
        )
        code_files = True
        baseline = slate.human_plan_id == work_order.human_plan_id
        epoch = slate.epoch_id is not None
    except (RuntimeError, OSError, ValueError):
        blockers.append("work_order:code_files_baseline_or_graph_changed")
    try:
        ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound:
        pass
    else:
        blockers.append("gate:already_started")
    if not baseline:
        blockers.append("human_baseline:not_verified")
    if epoch:
        blockers.append("planner_output:materialized_before_gate")
    canonical = tuple(sorted(set(blockers)))
    assert work_order.work_order_id is not None
    return PhononPortfolioEfficiencyPreflight(
        work_order_id=work_order.work_order_id,
        gate_id=work_order.gate_id,
        database_observed_at=_database_now(),
        ready_for_explicit_gate_start=(not canonical and code_files and baseline and not epoch),
        blockers=canonical,
        code_and_files_verified=code_files,
        blind_baseline_verified=baseline,
        planner_output_materialized=epoch,
    )


def _derive_efficiency_assessment(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    slate: PortfolioSlateSnapshot,
    epoch: PortfolioEpochSnapshot,
) -> PhononPortfolioEfficiencyAssessment:
    if epoch.slate_id != work_order.slate_id or epoch.human_plan_id != work_order.human_plan_id:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency epoch binding changed")
    if (
        epoch.decision.disposition is not PortfolioEpochDisposition.SHADOW_READY
        or not epoch.decision.selected_candidate_ids
        or not epoch.decision.shadow_only
        or epoch.decision.actions_enqueued
    ):
        raise PhononPortfolioEfficiencyConflict("phonon efficiency planner batch is not shadow-ready")
    if (
        epoch.comparison.human_hard_filter_violations
        or epoch.comparison.human_batch_constraint_violations
    ):
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency baseline violates frozen portfolio constraints"
        )
    mappings = {item.candidate_id: item for item in work_order.candidates}
    baseline_ids = (work_order.baseline_candidate_id,)
    planner_ids = epoch.decision.selected_candidate_ids
    if any(item not in mappings for item in (*baseline_ids, *planner_ids)):
        raise PhononPortfolioEfficiencyConflict("phonon efficiency candidate mapping changed")
    baseline_questions = tuple(sorted({mappings[item].question_sha256 for item in baseline_ids}))
    planner_questions = tuple(sorted({mappings[item].question_sha256 for item in planner_ids}))
    baseline_cost = sum(
        mappings[item].estimated_duration_seconds * 1_000_000 for item in baseline_ids
    )
    planner_cost = sum(
        mappings[item].estimated_duration_seconds * 1_000_000 for item in planner_ids
    )
    if not baseline_questions or not planner_questions or baseline_cost <= 0 or planner_cost <= 0:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency value/cost basis is empty")
    denominator = len(baseline_questions) * planner_cost
    numerator = len(planner_questions) * baseline_cost
    improvement = ((numerator - denominator) * 1_000_000) // denominator
    receipt = EnduranceEfficiencyReceipt(
        metric=EnduranceEfficiencyMetric.QUESTION_COVERAGE,
        baseline_value_units=len(baseline_questions),
        baseline_cost_microunits=baseline_cost,
        endurance_value_units=len(planner_questions),
        endurance_cost_microunits=planner_cost,
        improvement_ppm=improvement,
        evidence_sha256s=tuple(
            sorted(
                {
                    work_order.work_order_sha256,
                    work_order.portfolio_work_order.content_sha256,
                    work_order.portfolio_stage.content_sha256,
                    slate.snapshot_sha256,
                    epoch.epoch_sha256,
                    epoch.decision.decision_sha256,
                    epoch.comparison.comparison_sha256,
                    content_sha256(
                        {
                            "baseline_questions": baseline_questions,
                            "planner_questions": planner_questions,
                            "cost_basis": work_order.cost_basis,
                        }
                    ),
                }
            )
        ),
        assessor_code_sha256=work_order.code_identity.aggregate_sha256,
        assessed_by=work_order.assessed_by,
        assessed_at=epoch.evaluated_at,
    )
    assert work_order.work_order_id is not None
    return PhononPortfolioEfficiencyAssessment(
        work_order_id=work_order.work_order_id,
        gate_id=work_order.gate_id,
        epoch_id=epoch.epoch_id,
        baseline_candidate_ids=baseline_ids,
        planner_candidate_ids=planner_ids,
        baseline_question_sha256s=baseline_questions,
        planner_question_sha256s=planner_questions,
        receipt=receipt,
        minimum_improvement_ppm=work_order.minimum_improvement_ppm,
        meets_gate_floor=improvement >= work_order.minimum_improvement_ppm,
    )


def assess_phonon_portfolio_efficiency(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononPortfolioEfficiencyAssessment:
    _, _, slate = verify_phonon_portfolio_efficiency_work_order(
        work_order,
        repository_root=repository_root,
    )
    if slate.epoch_id is None:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency requires the shadow epoch")
    try:
        gate = ResearchEnduranceStore().get(work_order.gate_id)
    except ResearchEnduranceNotFound as exc:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency requires an explicitly started gate"
        ) from exc
    epoch = ResearchPortfolioStore().get_epoch(slate.epoch_id)
    if not gate.started_at <= epoch.evaluated_at:
        raise PhononPortfolioEfficiencyConflict("phonon efficiency epoch predates the live gate")
    return _derive_efficiency_assessment(work_order, slate, epoch)


def verify_phonon_portfolio_efficiency_assessment(
    work_order: PhononPortfolioEfficiencyWorkOrder,
    assessment: PhononPortfolioEfficiencyAssessment,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    expected = assess_phonon_portfolio_efficiency(
        work_order,
        repository_root=repository_root,
    )
    if expected != assessment:
        raise PhononPortfolioEfficiencyConflict(
            "phonon efficiency assessment does not replay exactly"
        )


__all__ = [
    "PhononPortfolioEfficiencyArtifact",
    "PhononPortfolioEfficiencyAssessment",
    "PhononPortfolioEfficiencyCandidate",
    "PhononPortfolioEfficiencyCodeIdentity",
    "PhononPortfolioEfficiencyConflict",
    "PhononPortfolioEfficiencyError",
    "PhononPortfolioEfficiencyPreflight",
    "PhononPortfolioEfficiencyWorkOrder",
    "assess_phonon_portfolio_efficiency",
    "capture_phonon_portfolio_efficiency_code_identity",
    "preflight_phonon_portfolio_efficiency_start",
    "prepare_phonon_portfolio_efficiency_work_order",
    "verify_phonon_portfolio_efficiency_assessment",
    "verify_phonon_portfolio_efficiency_code_identity",
    "verify_phonon_portfolio_efficiency_work_order",
]
