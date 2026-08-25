"""Replay-safe commissioning for the real structure/phonon research Quest.

The F10 structure-discrimination result is useful evidence, but it is not itself a durable
research program.  This module turns the immutable local evidence into a Quest with explicit
competing world models, bounded campaigns, data roles, and budgets.  Preparation is pure and
content addressed.  Application is restart-safe: every legacy Run/DataAsset receives a stable
primary key and every graph mutation uses a stable scientific-command idempotency key.

The existing Matbench holdout is intentionally registered only as exploration data.  External
corpora remain non-allocated candidates until a separate lineage and target-harmonisation audit
has made them eligible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from aletheia.db import REPO_ROOT, session_scope
from aletheia.domains.materials.capabilities.structure_discrimination import (
    StructureAwareExperimentPlan,
    StructureAwareExperimentResult,
)
from aletheia.epistemics.persistence import (
    get_world_model_snapshot,
    store_world_model_snapshot,
)
from aletheia.epistemics.schemas import (
    Assumption,
    AssumptionKind,
    BeliefState,
    BeliefUpdateKind,
    HypothesisBelief,
    HypothesisLifecycle,
    HypothesisRole,
    HypothesisVersion,
    Prediction,
    PredictionDirection,
    ResearchQuestion,
    ResearchQuestionKind,
    WorldModelSnapshot,
)
from aletheia.memory.ledger import DATA_SOURCES, DATA_STATUSES, RUN_STATUSES, DataAsset, Run
from aletheia.programs.graph import ProgramGraphStore
from aletheia.programs.schemas import (
    BudgetAllocationSpec,
    BudgetKind,
    CampaignRunBindingSpec,
    CampaignSpec,
    DataRole,
    DataRoleAllocationSpec,
    GraphCommandContext,
    GraphNodeState,
    NodeTransitionSpec,
    ProgramQuestionBindingSpec,
    QuestGraphSnapshot,
    QuestSpec,
    ResearchProgramSpec,
    ScientificFamilySpec,
)
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_DATA_ASSET_ID_PATTERN = r"^[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_DEFAULT_NAMESPACE = "phonon-structure-information-v1"
_CODE_COMPONENTS = (
    "aletheia/domains/materials/phonon_commissioning.py",
    "aletheia/epistemics/persistence.py",
    "aletheia/epistemics/schemas.py",
    "aletheia/memory/ledger.py",
    "aletheia/programs/graph.py",
    "aletheia/programs/persistence.py",
    "aletheia/programs/schemas.py",
)


class PhononCommissioningError(RuntimeError):
    """Base error for evidence preparation or Quest application."""


class PhononCommissioningConflict(PhononCommissioningError):
    """A stable identity already exists with different content."""


class PhononCommissioningEvidenceError(PhononCommissioningError):
    """Local evidence is missing, changed, or scientifically mislabelled."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _stable_hex(namespace: str, kind: str, key: str) -> str:
    return content_sha256(
        {
            "schema": "aletheia.phonon_commissioning_stable_identity.v1",
            "namespace": namespace,
            "kind": kind,
            "key": key,
        }
    )[:32]


def _lineage_id(prefix: str, namespace: str, kind: str, key: str) -> str:
    return f"{prefix}_{_stable_hex(namespace, kind, key)}"


def _author_sha256(principal: str) -> str:
    return content_sha256(
        {
            "schema": "aletheia.phonon_commissioning_author.v1",
            "principal": principal,
        }
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("commissioning artifact paths must be safe repository-relative paths")
    return relative


class LocalArtifactIdentity(_FrozenModel):
    relative_path: str = Field(min_length=1, max_length=1_024)
    byte_size: int = Field(ge=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _path_is_repository_relative(self) -> "LocalArtifactIdentity":
        _safe_relative_path(self.relative_path)
        return self


class StructureSignalEvidenceReceipt(_FrozenModel):
    """Minimal reconstructible identity of the real F10 evidence used for scoping."""

    schema_version: Literal[1] = 1
    dataset_file: LocalArtifactIdentity
    plan_file: LocalArtifactIdentity
    result_file: LocalArtifactIdentity
    dataset_ref: str = Field(min_length=1, max_length=256)
    source_uri: str = Field(min_length=1, max_length=2_048)
    license_expression: str = Field(min_length=1, max_length=128)
    license_uri: str = Field(min_length=1, max_length=2_048)
    structure_column: str = Field(min_length=1, max_length=128)
    target_column: str = Field(min_length=1, max_length=128)
    target_quantity_kind_id: str = Field(min_length=1, max_length=256)
    target_unit_ucum: str = Field(min_length=1, max_length=64)
    row_count: int = Field(ge=3)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    dataset_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_disposition: Literal["robust_aligned_structure_signal"]
    result_completed_at: AwareDatetime
    public_retrospective_dataset: Literal[True] = True
    holdout_is_same_dataset_not_external_replication: Literal[True] = True
    causal_or_mechanism_claim_forbidden: Literal[True] = True
    validation_level: Literal["schema_and_content_hash"] = "schema_and_content_hash"

    @model_validator(mode="after")
    def _source_identity_is_closed(self) -> "StructureSignalEvidenceReceipt":
        if self.dataset_file.sha256 == self.plan_file.sha256:
            raise ValueError("dataset and plan artifacts unexpectedly share one identity")
        if self.dataset_file.sha256 == self.result_file.sha256:
            raise ValueError("dataset and result artifacts unexpectedly share one identity")
        return self

    @property
    def evidence_sha256(self) -> str:
        return content_sha256(self)


class CommissionedRunSpec(_FrozenModel):
    identity_namespace: str = Field(pattern=_IDENTITY_PATTERN)
    identity_key: str = Field(pattern=_IDENTITY_PATTERN)
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    goal: str = Field(min_length=1, max_length=8_192)
    domain: str = Field(min_length=1, max_length=128)
    direction: str = Field(min_length=1, max_length=8_192)
    owner: str = Field(min_length=1, max_length=128)
    budget_cap_usd: float = Field(gt=0)
    gpu_hours_cap: float = Field(gt=0)
    initial_status: Literal["active", "paused"]

    @model_validator(mode="after")
    def _id_is_stable(self) -> "CommissionedRunSpec":
        expected = _stable_hex(self.identity_namespace, "run", self.identity_key)
        if self.run_id is not None and self.run_id != expected:
            raise ValueError("commissioned Run ID differs from its stable identity")
        object.__setattr__(self, "run_id", expected)
        return self


class CommissionedDataAssetSpec(_FrozenModel):
    identity_namespace: str = Field(pattern=_IDENTITY_PATTERN)
    identity_key: str = Field(pattern=_IDENTITY_PATTERN)
    data_asset_id: str | None = Field(default=None, pattern=_DATA_ASSET_ID_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    role: Literal["primary"] = "primary"
    source: Literal["url"] = "url"
    ref: str = Field(min_length=1, max_length=4_096)
    target_column: str = Field(min_length=1, max_length=128)
    composition_column: str | None = Field(default=None, max_length=128)
    feature_kind: Literal["crystal_structure"] = "crystal_structure"
    description: str = Field(min_length=1, max_length=8_192)
    uri: str = Field(min_length=1, max_length=4_096)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    profile_json: dict[str, Any]
    status: Literal["ready"] = "ready"
    requested_by: Literal["human"] = "human"

    @model_validator(mode="after")
    def _id_and_uri_are_stable(self) -> "CommissionedDataAssetSpec":
        expected = _stable_hex(
            self.identity_namespace,
            "data_asset",
            f"{self.run_id}:{self.identity_key}",
        )
        if self.data_asset_id is not None and self.data_asset_id != expected:
            raise ValueError("commissioned DataAsset ID differs from its stable identity")
        _safe_relative_path(self.uri)
        if not self.profile_json:
            raise ValueError("commissioned DataAsset requires a non-empty frozen profile")
        object.__setattr__(self, "data_asset_id", expected)
        return self


class CampaignRunSeed(_FrozenModel):
    campaign_id: str = Field(pattern=r"^cmp_[0-9a-f]{32}$")
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class QuestionBindingSeed(_FrozenModel):
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class CommissionedBudgetPlan(_FrozenModel):
    kind: BudgetKind
    quest_cap_microunits: int = Field(gt=0)
    program_cap_microunits: int = Field(gt=0)
    quest_policy: dict[str, Any]
    program_policy: dict[str, Any]

    @model_validator(mode="after")
    def _child_fits_parent(self) -> "CommissionedBudgetPlan":
        if self.program_cap_microunits > self.quest_cap_microunits:
            raise ValueError("commissioned Program budget exceeds its Quest cap")
        if not self.quest_policy or not self.program_policy:
            raise ValueError("commissioned budgets require explicit policies")
        return self


class ExternalCorpusCandidate(_FrozenModel):
    candidate_key: str = Field(pattern=_IDENTITY_PATTERN)
    title: str = Field(min_length=1, max_length=512)
    official_url: str = Field(min_length=1, max_length=2_048)
    calculation_lineage: str = Field(min_length=1, max_length=4_096)
    target_compatibility: str = Field(min_length=1, max_length=4_096)
    status: Literal[
        "candidate_requires_lineage_and_target_audit",
        "excluded_same_source_lineage",
        "candidate_for_distinct_property_only",
    ]
    allocation_forbidden: Literal[True] = True


class CommissioningCodeIdentity(_FrozenModel):
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _matrix_is_closed(self) -> "CommissioningCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("commissioning code-component matrix is incomplete")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in components.values()
        ):
            raise ValueError("commissioning component hashes must be lowercase SHA-256")
        expected = content_sha256(
            {
                "schema": "aletheia.phonon_commissioning_code.v1",
                "components": components,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("commissioning aggregate code hash differs from its components")
        object.__setattr__(self, "component_sha256s", components)
        return self


class PhononQuestCommissioningManifest(_FrozenModel):
    schema_version: Literal[1] = 1
    commissioning_id: str | None = Field(
        default=None,
        pattern=r"^pcm_[0-9a-f]{32}$",
    )
    identity_namespace: str = Field(pattern=_IDENTITY_PATTERN)
    prepared_at: AwareDatetime
    command_principal: str = Field(min_length=1, max_length=128)
    code_identity: CommissioningCodeIdentity
    evidence: StructureSignalEvidenceReceipt
    quest: QuestSpec
    program: ResearchProgramSpec
    family: ScientificFamilySpec
    campaigns: tuple[CampaignSpec, ...] = Field(min_length=3, max_length=16)
    runs: tuple[CommissionedRunSpec, ...] = Field(min_length=3, max_length=16)
    campaign_runs: tuple[CampaignRunSeed, ...] = Field(min_length=3, max_length=32)
    world_models: tuple[WorldModelSnapshot, ...] = Field(min_length=2, max_length=16)
    question_bindings: tuple[QuestionBindingSeed, ...] = Field(min_length=2, max_length=16)
    data_asset: CommissionedDataAssetSpec
    data_role: DataRoleAllocationSpec
    budgets: tuple[CommissionedBudgetPlan, ...] = Field(min_length=1, max_length=6)
    initial_active_campaign_id: str = Field(pattern=r"^cmp_[0-9a-f]{32}$")
    external_corpus_candidates: tuple[ExternalCorpusCandidate, ...] = Field(
        min_length=2,
        max_length=16,
    )
    durable_blockers: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _manifest_is_closed_and_honest(self) -> "PhononQuestCommissioningManifest":
        if self.prepared_at < self.evidence.result_completed_at:
            raise ValueError("commissioning cannot predate its source result")
        if self.quest.node_id != self.program.quest_id:
            raise ValueError("commissioned Program belongs to another Quest")
        if self.family.program_id != self.program.node_id:
            raise ValueError("commissioned scientific family belongs to another Program")

        campaigns = tuple(sorted(self.campaigns, key=lambda item: item.node_id))
        runs = tuple(sorted(self.runs, key=lambda item: item.run_id or ""))
        campaign_runs = tuple(
            sorted(self.campaign_runs, key=lambda item: (item.campaign_id, item.run_id))
        )
        world_models = tuple(
            sorted(self.world_models, key=lambda item: item.question.question_sha256)
        )
        bindings = tuple(sorted(self.question_bindings, key=lambda item: item.question_sha256))
        budgets = tuple(sorted(self.budgets, key=lambda item: item.kind.value))
        candidates = tuple(
            sorted(self.external_corpus_candidates, key=lambda item: item.candidate_key)
        )
        blockers = tuple(sorted(set(self.durable_blockers)))
        if (
            campaigns != self.campaigns
            or runs != self.runs
            or campaign_runs != self.campaign_runs
            or world_models != self.world_models
            or bindings != self.question_bindings
            or budgets != self.budgets
            or candidates != self.external_corpus_candidates
            or blockers != self.durable_blockers
        ):
            raise ValueError("commissioning collections must be unique and canonical")

        campaign_ids = {item.node_id for item in campaigns}
        run_ids = {item.run_id for item in runs}
        if len(campaign_ids) != len(campaigns) or len(run_ids) != len(runs):
            raise ValueError("commissioning campaign/run identities must be unique")
        if any(
            item.program_id != self.program.node_id or item.family_id != self.family.family_id
            for item in campaigns
        ):
            raise ValueError("commissioned Campaign escaped its Program/family")
        if self.initial_active_campaign_id not in campaign_ids:
            raise ValueError("initial active Campaign is absent")
        if {item.campaign_id for item in campaign_runs} != campaign_ids:
            raise ValueError("every commissioned Campaign requires one bound Run")
        if any(item.run_id not in run_ids for item in campaign_runs):
            raise ValueError("Campaign/Run binding references a foreign Run")
        if len({(item.campaign_id, item.run_id) for item in campaign_runs}) != len(
            campaign_runs
        ):
            raise ValueError("commissioned Campaign/Run bindings must be unique")

        question_hashes = {item.question.question_sha256 for item in world_models}
        if len(question_hashes) != len(world_models):
            raise ValueError("commissioned research questions must be unique")
        if {item.question_sha256 for item in bindings} != question_hashes:
            raise ValueError("question bindings do not close the world-model set")
        bound_run_ids = {item.run_id for item in campaign_runs}
        if any(item.question.run_id not in bound_run_ids for item in world_models):
            raise ValueError("world-model question Run is not bound to a Campaign")
        if self.data_asset.run_id not in bound_run_ids:
            raise ValueError("commissioned DataAsset Run is outside the Quest")
        if self.data_asset.content_sha256 != self.evidence.dataset_file.sha256:
            raise ValueError("DataAsset differs from the frozen source artifact")
        if (
            self.data_role.scope_node_id != self.program.node_id
            or self.data_role.data_asset_id != self.data_asset.data_asset_id
            or self.data_role.role is not DataRole.EXPLORATION
        ):
            raise ValueError("source data must be allocated only as Program exploration data")
        if not self.data_role.exclusive:
            raise ValueError("commissioned source data allocation must be exclusive")
        if any(not item.allocation_forbidden for item in candidates):
            raise ValueError("unverified external candidates cannot be allocated")

        expected_id = f"pcm_{self.manifest_sha256[:32]}"
        if self.commissioning_id is not None and self.commissioning_id != expected_id:
            raise ValueError("commissioning ID differs from its manifest")
        object.__setattr__(self, "campaigns", campaigns)
        object.__setattr__(self, "runs", runs)
        object.__setattr__(self, "campaign_runs", campaign_runs)
        object.__setattr__(self, "world_models", world_models)
        object.__setattr__(self, "question_bindings", bindings)
        object.__setattr__(self, "budgets", budgets)
        object.__setattr__(self, "external_corpus_candidates", candidates)
        object.__setattr__(self, "durable_blockers", blockers)
        object.__setattr__(self, "commissioning_id", expected_id)
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"commissioning_id"}))


class PhononQuestCommissioningReceipt(_FrozenModel):
    schema_version: Literal[1] = 1
    commissioning_id: str = Field(pattern=r"^pcm_[0-9a-f]{32}$")
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    program_id: str = Field(pattern=r"^prg_[0-9a-f]{32}$")
    campaign_ids: tuple[str, ...] = Field(min_length=3)
    run_ids: tuple[str, ...] = Field(min_length=3)
    data_asset_id: str = Field(pattern=_DATA_ASSET_ID_PATTERN)
    question_sha256s: tuple[str, ...] = Field(min_length=2)
    world_model_sha256s: tuple[str, ...] = Field(min_length=2)
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_object_count: int = Field(ge=0)
    replayed_object_count: int = Field(ge=0)
    initial_active_campaign_id: str = Field(pattern=r"^cmp_[0-9a-f]{32}$")
    durable_blockers: tuple[str, ...] = Field(min_length=1)


def capture_commissioning_code_identity(
    repository_root: Path = REPO_ROOT,
) -> CommissioningCodeIdentity:
    root = repository_root.resolve()
    components: dict[str, str] = {}
    for relative in _CODE_COMPONENTS:
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - constants are repository owned
            raise PhononCommissioningEvidenceError(
                f"commissioning code escaped repository root: {relative}"
            ) from exc
        components[relative] = _file_sha256(path)
    aggregate = content_sha256(
        {
            "schema": "aletheia.phonon_commissioning_code.v1",
            "components": dict(sorted(components.items())),
        }
    )
    return CommissioningCodeIdentity(
        component_sha256s=components,
        aggregate_sha256=aggregate,
    )


def local_artifact_identity(path: Path, *, repository_root: Path = REPO_ROOT) -> LocalArtifactIdentity:
    root = repository_root.resolve()
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PhononCommissioningEvidenceError(
            f"commissioning evidence is outside the repository: {resolved}"
        ) from exc
    if not resolved.is_file():
        raise PhononCommissioningEvidenceError(
            f"commissioning evidence is not a regular file: {resolved}"
        )
    return LocalArtifactIdentity(
        relative_path=relative,
        byte_size=resolved.stat().st_size,
        sha256=_file_sha256(resolved),
    )


def inspect_structure_signal_evidence(
    workspace: Path,
    *,
    repository_root: Path = REPO_ROOT,
) -> StructureSignalEvidenceReceipt:
    """Validate and bind the real dataset/plan/result without fitting a model."""

    dataset_path = workspace / "source" / "matbench_phonons.json.gz"
    plan_path = workspace / "plan.json"
    result_path = workspace / "result.json"
    dataset_file = local_artifact_identity(dataset_path, repository_root=repository_root)
    plan_file = local_artifact_identity(plan_path, repository_root=repository_root)
    result_file = local_artifact_identity(result_path, repository_root=repository_root)
    try:
        plan = StructureAwareExperimentPlan.model_validate(
            json.loads(plan_path.read_text(encoding="utf-8"))
        )
        result = StructureAwareExperimentResult.model_validate(
            json.loads(result_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise PhononCommissioningEvidenceError(
            "structure signal plan/result failed immutable schema validation"
        ) from exc
    if result.plan_sha256 != plan.plan_sha256:
        raise PhononCommissioningEvidenceError("structure result is bound to another plan")
    if dataset_file.sha256 != plan.protocol.dataset.expected_file_sha256:
        raise PhononCommissioningEvidenceError("dataset bytes differ from the frozen protocol")
    if dataset_file.sha256 != plan.dataset_receipt.source.sha256:
        raise PhononCommissioningEvidenceError("dataset bytes differ from the plan receipt")
    if result.completed_at < plan.prepared_at:
        raise PhononCommissioningEvidenceError("structure result predates its pre-fit plan")
    dataset = plan.protocol.dataset
    return StructureSignalEvidenceReceipt(
        dataset_file=dataset_file,
        plan_file=plan_file,
        result_file=result_file,
        dataset_ref=dataset.dataset_ref,
        source_uri=dataset.source_uri,
        license_expression=dataset.license_expression,
        license_uri=dataset.license_uri,
        structure_column=dataset.structure_column,
        target_column=dataset.target_column,
        target_quantity_kind_id=dataset.target_quantity_kind_id,
        target_unit_ucum=dataset.target_unit_ucum,
        row_count=plan.dataset_receipt.row_count,
        protocol_sha256=plan.protocol.protocol_sha256,
        implementation_sha256=plan.protocol.implementation_sha256,
        plan_sha256=plan.plan_sha256,
        dataset_receipt_sha256=plan.dataset_receipt.receipt_sha256,
        result_sha256=result.result_sha256,
        result_disposition=result.disposition.value,
        result_completed_at=result.completed_at,
        public_retrospective_dataset=plan.protocol.public_retrospective_dataset,
        holdout_is_same_dataset_not_external_replication=(
            result.holdout_is_same_dataset_not_external_replication
        ),
        causal_or_mechanism_claim_forbidden=result.causal_or_mechanism_claim_forbidden,
    )


def verify_commissioning_artifacts(
    manifest: PhononQuestCommissioningManifest,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    """Rehash every local input and reject code/evidence drift before database writes."""

    manifest = PhononQuestCommissioningManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    live_code = capture_commissioning_code_identity(repository_root)
    if live_code != manifest.code_identity:
        raise PhononCommissioningEvidenceError(
            "live commissioning code differs from the frozen manifest"
        )
    root = repository_root.resolve()
    for artifact in (
        manifest.evidence.dataset_file,
        manifest.evidence.plan_file,
        manifest.evidence.result_file,
    ):
        relative = _safe_relative_path(artifact.relative_path)
        resolved = (root / Path(*relative.parts)).resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PhononCommissioningEvidenceError(
                f"commissioning artifact escaped repository root: {artifact.relative_path}"
            ) from exc
        if (
            not resolved.is_file()
            or resolved.stat().st_size != artifact.byte_size
            or _file_sha256(resolved) != artifact.sha256
        ):
            raise PhononCommissioningEvidenceError(
                f"commissioning artifact changed: {artifact.relative_path}"
            )


def _world_model(
    *,
    namespace: str,
    question_key: str,
    run_id: str,
    kind: ResearchQuestionKind,
    statement: str,
    scope: dict[str, Any],
    hypotheses: tuple[dict[str, Any], ...],
    frozen_at: datetime,
    principal: str,
    measurement_protocol: dict[str, Any],
) -> WorldModelSnapshot:
    author = _author_sha256(principal)
    question_id = _lineage_id("rq", namespace, "question", question_key)
    question = ResearchQuestion(
        run_id=run_id,
        question_id=question_id,
        version=1,
        kind=kind,
        statement=statement,
        scope_sha256=content_sha256(scope),
        author_principal_sha256=author,
        frozen_at=frozen_at,
    )
    hypothesis_models: list[HypothesisVersion] = []
    hypothesis_by_key: dict[str, HypothesisVersion] = {}
    for item in hypotheses:
        key = str(item["key"])
        hypothesis = HypothesisVersion(
            run_id=run_id,
            question_id=question_id,
            question_version_sha256=question.question_sha256,
            hypothesis_id=_lineage_id(
                "hyp", namespace, "hypothesis", f"{question_key}:{key}"
            ),
            version=1,
            role=HypothesisRole(str(item["role"])),
            lifecycle=HypothesisLifecycle.ACTIVE,
            statement=str(item["statement"]),
            mechanism=item.get("mechanism"),
            rationale_sha256=content_sha256(
                {
                    "schema": "aletheia.phonon_hypothesis_rationale.v1",
                    "question_key": question_key,
                    "hypothesis_key": key,
                    "rationale": item["rationale"],
                }
            ),
            author_principal_sha256=author,
            frozen_at=frozen_at,
        )
        hypothesis_models.append(hypothesis)
        hypothesis_by_key[key] = hypothesis
    hypothesis_models.sort(key=lambda item: item.hypothesis_id)
    protocol_sha256 = content_sha256(measurement_protocol)
    assumptions: list[Assumption] = []
    predictions: list[Prediction] = []
    probabilities: dict[str, float] = {}
    for item in hypotheses:
        key = str(item["key"])
        hypothesis = hypothesis_by_key[key]
        other_ids = tuple(
            sorted(
                candidate.hypothesis_id
                for candidate in hypothesis_models
                if candidate.hypothesis_id != hypothesis.hypothesis_id
            )
        )
        assumptions.append(
            Assumption(
                run_id=run_id,
                assumption_id=_lineage_id(
                    "asm", namespace, "assumption", f"{question_key}:{key}"
                ),
                version=1,
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                kind=AssumptionKind(str(item["assumption_kind"])),
                statement=str(item["assumption"]),
                risk_if_violated=str(item["assumption_risk"]),
                author_principal_sha256=author,
                frozen_at=frozen_at,
            )
        )
        outcomes = tuple(str(value) for value in item["outcome_space"])
        predictions.append(
            Prediction(
                run_id=run_id,
                prediction_id=_lineage_id(
                    "pred", namespace, "prediction", f"{question_key}:{key}"
                ),
                version=1,
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                observable_id=str(item["observable_id"]),
                outcome_space=outcomes,
                expected_outcome=str(item["expected_outcome"]),
                direction=PredictionDirection(str(item["direction"])),
                discriminates_from_hypothesis_ids=other_ids,
                measurement_protocol_sha256=protocol_sha256,
                author_principal_sha256=author,
                frozen_at=frozen_at,
            )
        )
        probabilities[hypothesis.hypothesis_id] = float(item["prior"])
    assumptions.sort(key=lambda item: (item.assumption_id, item.version))
    predictions.sort(key=lambda item: (item.prediction_id, item.version))
    beliefs = tuple(
        HypothesisBelief(
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_version_sha256=hypothesis.hypothesis_sha256,
            probability=probabilities[hypothesis.hypothesis_id],
        )
        for hypothesis in hypothesis_models
    )
    belief = BeliefState(
        run_id=run_id,
        belief_lineage_id=_lineage_id("blf", namespace, "belief", question_key),
        version=1,
        question_id=question_id,
        question_version_sha256=question.question_sha256,
        hypotheses=beliefs,
        update_kind=BeliefUpdateKind.PRIOR,
        author_principal_sha256=author,
        frozen_at=frozen_at,
    )
    return WorldModelSnapshot(
        question=question,
        hypotheses=tuple(hypothesis_models),
        assumptions=tuple(assumptions),
        predictions=tuple(predictions),
        belief_state=belief,
        frozen_at=frozen_at,
    )


def _external_candidates() -> tuple[ExternalCorpusCandidate, ...]:
    return tuple(
        sorted(
            (
                ExternalCorpusCandidate(
                    candidate_key="alexandria-pbe-phonon-2025-08-11",
                    title="Alexandria PBE phonon dataset",
                    official_url="https://alexandria.icams.rub.de/datasets.html",
                    calculation_lineage=(
                        "Alexandria describes Quantum ESPRESSO phonon calculations and a PBE "
                        "recalculation of MDR materials; exact workflow, material overlap, and "
                        "target extraction must be audited before use."
                    ),
                    target_compatibility=(
                        "Potentially supports a separately calculated last-phonon-DOS-peak target, "
                        "but no target harmonisation or leakage audit has yet been committed."
                    ),
                    status="candidate_requires_lineage_and_target_audit",
                ),
                ExternalCorpusCandidate(
                    candidate_key="materials-project-legacy-dfpt",
                    title="Materials Project legacy DFPT phonons",
                    official_url=(
                        "https://docs.materialsproject.org/methodology/materials-methodology/"
                        "phonon-dispersion"
                    ),
                    calculation_lineage=(
                        "The current F10 Matbench target traces to the Materials Project/Petretto "
                        "high-throughput phonon corpus, so another API extraction is not an "
                        "independent source."
                    ),
                    target_compatibility="Target is compatible but source independence is absent.",
                    status="excluded_same_source_lineage",
                ),
                ExternalCorpusCandidate(
                    candidate_key="phonondb-mdr-pbesol",
                    title="Phonondb PBEsol calculations at NIMS MDR",
                    official_url="https://github.com/atztogo/phonondb",
                    calculation_lineage=(
                        "The official migration index points to PBEsol phonon calculations in "
                        "NIMS MDR. Initial structures may originate from Materials Project, while "
                        "the phonon calculations use a distinct finite-displacement workflow."
                    ),
                    target_compatibility=(
                        "Raw phonon DOS may support an independently calculated last-peak target; "
                        "material overlap, calculation independence, and extraction parity remain "
                        "unverified."
                    ),
                    status="candidate_requires_lineage_and_target_audit",
                ),
                ExternalCorpusCandidate(
                    candidate_key="phonix-2026-03-28",
                    title="Phonix anharmonic phonon database",
                    official_url="https://phonix-db.org/",
                    calculation_lineage=(
                        "Auto-kappa integrates VASP and ALAMODE over Materials Project and "
                        "Phonondb-derived structures."
                    ),
                    target_compatibility=(
                        "Primary released emphasis is anharmonic interaction and thermal "
                        "conductivity, not the frozen harmonic last-DOS-peak target."
                    ),
                    status="candidate_for_distinct_property_only",
                ),
            ),
            key=lambda item: item.candidate_key,
        )
    )


def build_phonon_quest_commissioning_manifest(
    evidence: StructureSignalEvidenceReceipt,
    *,
    prepared_at: datetime,
    command_principal: str,
    identity_namespace: str = _DEFAULT_NAMESPACE,
    repository_root: Path = REPO_ROOT,
) -> PhononQuestCommissioningManifest:
    """Build a complete frozen Quest blueprint from already inspected real evidence."""

    evidence = StructureSignalEvidenceReceipt.model_validate(evidence.model_dump(mode="python"))
    if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise ValueError("commissioning preparation time must be timezone-aware")
    if not command_principal.strip():
        raise ValueError("commissioning command principal cannot be blank")

    quest = QuestSpec(
        identity_key=f"{identity_namespace}:quest",
        title="Discriminate transferable structural information in inorganic phonon spectra",
        direction=(
            "Determine which crystal-structure factors carry reproducible predictive information "
            "about the last phonon density-of-states peak beyond composition, and establish the "
            "boundary under independent implementation and independently calculated data."
        ),
        value_boundary=(
            "Prefer discriminating negative results and calibrated uncertainty over a larger "
            "headline metric. The existing same-dataset result supports predictive information "
            "only; mechanism, causality, and external replication require new evidence."
        ),
        safety_boundary=(
            "No autonomous purchase, credential use, publication, or outward communication.",
            "Do not open or adapt to sealed external-validation targets before protocol freeze.",
            "Do not promote same-source replay as independent external replication.",
            "Do not promote observational descriptor ablations as causal interventions.",
            "Stop on provenance ambiguity, budget exhaustion, or unreconciled durable state.",
        ),
        resource_boundary={
            "budget_authority": "database_allocations_only",
            "outward_actions_allowed": False,
            "autonomous_allocation_enabled": False,
            "minimum_human_review_for_external_data": True,
            "target_quantity_kind_id": evidence.target_quantity_kind_id,
            "target_unit_ucum": evidence.target_unit_ucum,
        },
    )
    program = ResearchProgramSpec(
        quest_id=quest.node_id,
        identity_key=f"{identity_namespace}:program",
        title="Structure-aware phonon mechanism and replication program",
        objective=(
            "Reproduce the frozen aligned-structure gain, distinguish local packing from global "
            "lattice/symmetry explanations with precommitted ablations, and test transfer on an "
            "independently calculated corpus only after source-lineage qualification."
        ),
        problem_domain="computational_materials_phonons",
        knowledge_boundary={
            "as_of": prepared_at.isoformat(),
            "source_evidence_sha256": evidence.evidence_sha256,
            "source_result_sha256": evidence.result_sha256,
            "source_result_disposition": evidence.result_disposition,
            "known_claim_ceiling": "predictive_information_same_public_dataset",
            "mechanism_status": "unresolved",
            "external_replication_status": "not_yet_attempted",
            "external_candidates_are_allocated": False,
        },
    )
    family = ScientificFamilySpec(
        program_id=program.node_id,
        family_key="structure-information-phonon-peak",
        title="Structural information for last phonon-DOS peak",
        scientific_scope=(
            "All attempts that test whether aligned crystal structure adds stable information "
            "beyond composition for the last phonon-DOS peak, including restarts and replication."
        ),
        multiplicity_policy={
            "familywise_alpha": 0.05,
            "method": "precommitted_hierarchical_gatekeeping",
            "target_selection_on_holdout_forbidden": True,
            "one_time_external_validation": True,
        },
    )

    campaign_by_key: dict[str, CampaignSpec] = {}
    for key, title, objective, stopping in (
        (
            "independent-replay",
            "Independent implementation replay",
            (
                "Reconstruct the frozen F10 result from exact source identities using an "
                "independently reviewed implementation path and retain contradictions."
            ),
            {
                "maximum_formal_attempts": 1,
                "required_outcomes": ["confirmed", "contradicted", "inconclusive"],
                "negative_result_memory_required_on_contradiction": True,
                "same_dataset_external_replication_claim_forbidden": True,
            },
        ),
        (
            "mechanism-ablation",
            "Local-packing versus global-lattice discrimination",
            (
                "Run capacity-matched, group-disjoint local-only and global-only structure "
                "ablations that discriminate the frozen mechanism hypotheses."
            ),
            {
                "maximum_formal_attempts": 3,
                "minimum_cluster_count_per_role": 200,
                "selection_on_locked_role_forbidden": True,
                "causal_language_forbidden_without_intervention": True,
                "stop_if_no_hypothesis_pair_is_discriminated": True,
            },
        ),
        (
            "external-calculation",
            "Independent calculation-corpus validation",
            (
                "Qualify a separately calculated phonon corpus, freeze target harmonisation and "
                "material matching without target inspection, then test directional transfer."
            ),
            {
                "maximum_formal_attempts": 1,
                "lineage_audit_required_before_data_allocation": True,
                "minimum_common_materials": 200,
                "target_blind_material_matching": True,
                "external_target_opened_once": True,
                "stop_on_source_lineage_overlap": True,
            },
        ),
    ):
        campaign_by_key[key] = CampaignSpec(
            program_id=program.node_id,
            family_id=family.family_id,
            identity_key=f"{identity_namespace}:campaign:{key}",
            title=title,
            objective=objective,
            stopping_boundary=stopping,
        )

    runs_by_key = {
        "independent-replay": CommissionedRunSpec(
            identity_namespace=identity_namespace,
            identity_key="independent-replay",
            goal="Independently reproduce the frozen structure-information result.",
            domain="materials",
            direction="Exact replay first; report confirmed, contradicted, or inconclusive.",
            owner="human-supervised-autonomous-scientist",
            budget_cap_usd=20.0,
            gpu_hours_cap=8.0,
            initial_status="active",
        ),
        "mechanism-ablation": CommissionedRunSpec(
            identity_namespace=identity_namespace,
            identity_key="mechanism-ablation",
            goal="Discriminate local packing, global lattice, and null explanations.",
            domain="materials",
            direction="Precommit capacity-matched structure ablations before evaluation.",
            owner="human-supervised-autonomous-scientist",
            budget_cap_usd=30.0,
            gpu_hours_cap=12.0,
            initial_status="paused",
        ),
        "external-calculation": CommissionedRunSpec(
            identity_namespace=identity_namespace,
            identity_key="external-calculation",
            goal="Test transfer on a separately calculated phonon corpus.",
            domain="materials",
            direction="Qualify lineage and target compatibility before registering data.",
            owner="human-supervised-autonomous-scientist",
            budget_cap_usd=30.0,
            gpu_hours_cap=12.0,
            initial_status="paused",
        ),
    }

    mechanism_world = _world_model(
        namespace=identity_namespace,
        question_key="mechanism",
        run_id=str(runs_by_key["mechanism-ablation"].run_id),
        kind=ResearchQuestionKind.MECHANISM,
        statement=(
            "Which explanation best accounts for the aligned-structure predictive gain beyond "
            "composition on chemical-system-disjoint Matbench phonons: local atomic packing, "
            "global lattice/symmetry, or no stable structure-specific signal after stricter controls?"
        ),
        scope={
            "dataset_sha256": evidence.dataset_file.sha256,
            "result_sha256": evidence.result_sha256,
            "target": evidence.target_quantity_kind_id,
            "claim_ceiling": "predictive_mechanism_discrimination_not_causal_effect",
        },
        measurement_protocol={
            "schema": "aletheia.phonon_mechanism_ablation_protocol_intent.v1",
            "group_identity": "chemical_system",
            "arms": ["composition", "local_only", "global_only", "aligned_full", "permuted"],
            "matched_capacity": True,
            "locked_role_selection_forbidden": True,
            "source_protocol_sha256": evidence.protocol_sha256,
        },
        hypotheses=(
            {
                "key": "null-artifact",
                "role": "null",
                "statement": (
                    "No stable structure-specific signal survives independent replay and nested "
                    "chemical-system controls."
                ),
                "mechanism": None,
                "rationale": "The current result could reflect one split, representation, or control.",
                "assumption_kind": "statistical",
                "assumption": "Repeated group-disjoint estimates expose split-specific gains.",
                "assumption_risk": "Weak repeats could mistake variance for disappearance.",
                "observable_id": "nested_group_replay.structure_gain",
                "outcome_space": (
                    "stable_positive_gain",
                    "ci_includes_zero_or_gain_below_floor",
                ),
                "expected_outcome": "ci_includes_zero_or_gain_below_floor",
                "direction": "no_change",
                "prior": 0.20,
            },
            {
                "key": "local-packing",
                "role": "primary",
                "statement": (
                    "Species-blind local packing distances and coordination carry most of the "
                    "transferable structural information."
                ),
                "mechanism": (
                    "Local force-constant environments constrain the upper phonon-frequency scale, "
                    "so radial packing features retain the gain after global features are removed."
                ),
                "rationale": "Vibrational frequencies depend on local bonding geometry and masses.",
                "assumption_kind": "measurement",
                "assumption": "The local-only feature block measures packing without global leakage.",
                "assumption_risk": "Feature entanglement would make the local/global contrast ambiguous.",
                "observable_id": "matched_ablation.local_vs_global_retained_gain",
                "outcome_space": (
                    "local_retains_majority",
                    "global_retains_majority",
                    "neither_retains_stable_gain",
                ),
                "expected_outcome": "local_retains_majority",
                "direction": "increase",
                "prior": 0.50,
            },
            {
                "key": "global-lattice",
                "role": "alternative",
                "statement": (
                    "Global lattice scale, volume, symmetry, and crystal-system constraints carry "
                    "most of the transferable structural information."
                ),
                "mechanism": (
                    "The final DOS peak is governed chiefly by unit-cell scale and symmetry-imposed "
                    "mode structure rather than detailed local packing."
                ),
                "rationale": "The existing feature block includes strong lattice and symmetry summaries.",
                "assumption_kind": "measurement",
                "assumption": "The global-only block excludes local radial information.",
                "assumption_risk": "Residual local information would inflate the global explanation.",
                "observable_id": "matched_ablation.local_vs_global_retained_gain",
                "outcome_space": (
                    "local_retains_majority",
                    "global_retains_majority",
                    "neither_retains_stable_gain",
                ),
                "expected_outcome": "global_retains_majority",
                "direction": "increase",
                "prior": 0.30,
            },
        ),
        frozen_at=prepared_at,
        principal=command_principal,
    )
    replication_world = _world_model(
        namespace=identity_namespace,
        question_key="replication",
        run_id=str(runs_by_key["external-calculation"].run_id),
        kind=ResearchQuestionKind.PREDICTIVE,
        statement=(
            "Does a precommitted aligned-structure gain reproduce under an independent "
            "implementation on the frozen source and remain directionally positive on a separately "
            "calculated phonon corpus after target-blind material matching?"
        ),
        scope={
            "source_dataset_sha256": evidence.dataset_file.sha256,
            "source_result_sha256": evidence.result_sha256,
            "external_source": "unselected_until_lineage_audit",
            "target": evidence.target_quantity_kind_id,
            "claim_ceiling": "external_predictive_transfer_not_experimental_validation",
        },
        measurement_protocol={
            "schema": "aletheia.phonon_replication_protocol_intent.v1",
            "same_source_independent_implementation": True,
            "external_calculation_lineage_required": True,
            "target_blind_material_matching": True,
            "common_target_unit": evidence.target_unit_ucum,
            "source_protocol_sha256": evidence.protocol_sha256,
        },
        hypotheses=(
            {
                "key": "null-nonreproducible",
                "role": "null",
                "statement": "The aligned-structure gain does not survive independent reproduction.",
                "mechanism": None,
                "rationale": "The original gain may be implementation- or corpus-specific.",
                "assumption_kind": "statistical",
                "assumption": "Independent code and data checks have enough power to detect the frozen floor.",
                "assumption_risk": "Low overlap or noisy targets could create a false non-replication.",
                "observable_id": "replication.two_stage_directional_gain",
                "outcome_space": (
                    "neither_reproduces",
                    "same_source_only",
                    "same_source_and_external",
                ),
                "expected_outcome": "neither_reproduces",
                "direction": "no_change",
                "prior": 0.20,
            },
            {
                "key": "stable-transfer",
                "role": "primary",
                "statement": (
                    "The structural information gain survives both independent implementation and "
                    "a separately calculated compatible corpus."
                ),
                "mechanism": (
                    "Aligned geometry carries a stable material-property relation that is not tied "
                    "to one implementation or calculation collection."
                ),
                "rationale": "The source effect is large, group-disjoint, and beats a matched permutation.",
                "assumption_kind": "scope",
                "assumption": "The qualified external corpus measures a harmonisable physical target.",
                "assumption_risk": "Target mismatch would invalidate cross-corpus interpretation.",
                "observable_id": "replication.two_stage_directional_gain",
                "outcome_space": (
                    "neither_reproduces",
                    "same_source_only",
                    "same_source_and_external",
                ),
                "expected_outcome": "same_source_and_external",
                "direction": "increase",
                "prior": 0.45,
            },
            {
                "key": "domain-shift",
                "role": "alternative",
                "statement": (
                    "The gain reproduces in independent code on the source data but not across a "
                    "separately calculated corpus because calculation/label shift dominates."
                ),
                "mechanism": (
                    "Differences in functional, pseudopotential, relaxation, or DOS extraction alter "
                    "the target mapping while leaving the source implementation result intact."
                ),
                "rationale": "Computed phonon labels are workflow-dependent even for matched materials.",
                "assumption_kind": "scope",
                "assumption": "Source and external calculations are independent enough to expose workflow shift.",
                "assumption_risk": "Hidden shared calculations would masquerade as transfer.",
                "observable_id": "replication.two_stage_directional_gain",
                "outcome_space": (
                    "neither_reproduces",
                    "same_source_only",
                    "same_source_and_external",
                ),
                "expected_outcome": "same_source_only",
                "direction": "distributional",
                "prior": 0.35,
            },
        ),
        frozen_at=prepared_at,
        principal=command_principal,
    )

    source_run = runs_by_key["independent-replay"]
    data_asset = CommissionedDataAssetSpec(
        identity_namespace=identity_namespace,
        identity_key="matbench-phonons-f10-source",
        run_id=str(source_run.run_id),
        ref=evidence.source_uri,
        target_column=evidence.target_column,
        feature_kind="crystal_structure",
        description=(
            "Exact F10 Matbench phonons source. Exploration/replay only; the same-dataset locked "
            "role is not independent external validation and cannot support a causal claim."
        ),
        uri=evidence.dataset_file.relative_path,
        content_sha256=evidence.dataset_file.sha256,
        profile_json={
            "schema": "aletheia.phonon_commissioned_data_profile.v1",
            "dataset_ref": evidence.dataset_ref,
            "row_count": evidence.row_count,
            "structure_column": evidence.structure_column,
            "target_column": evidence.target_column,
            "target_quantity_kind_id": evidence.target_quantity_kind_id,
            "target_unit_ucum": evidence.target_unit_ucum,
            "dataset_receipt_sha256": evidence.dataset_receipt_sha256,
            "evidence_sha256": evidence.evidence_sha256,
            "public_retrospective_dataset": True,
            "external_validation": False,
        },
    )
    data_role = DataRoleAllocationSpec(
        scope_node_id=program.node_id,
        data_asset_id=str(data_asset.data_asset_id),
        role=DataRole.EXPLORATION,
        exclusive=True,
        policy={
            "allowed_uses": [
                "physical_replay",
                "independent_implementation",
                "mechanism_ablation_design",
            ],
            "forbidden_claims": [
                "independent_external_replication",
                "causal_mechanism",
                "experimental_validation",
            ],
            "adaptive_use_after_external_protocol_freeze_forbidden": True,
        },
    )
    campaign_runs = tuple(
        sorted(
            (
                CampaignRunSeed(
                    campaign_id=campaign_by_key[key].node_id,
                    run_id=str(runs_by_key[key].run_id),
                    role=("original_evidence" if key == "independent-replay" else "primary"),
                )
                for key in campaign_by_key
            ),
            key=lambda item: (item.campaign_id, item.run_id),
        )
    )
    worlds = tuple(
        sorted(
            (mechanism_world, replication_world),
            key=lambda item: item.question.question_sha256,
        )
    )
    question_bindings = tuple(
        sorted(
            (
                QuestionBindingSeed(
                    question_sha256=mechanism_world.question.question_sha256,
                    role="mechanism",
                ),
                QuestionBindingSeed(
                    question_sha256=replication_world.question.question_sha256,
                    role="replication",
                ),
            ),
            key=lambda item: item.question_sha256,
        )
    )
    budgets = tuple(
        sorted(
            (
                CommissionedBudgetPlan(
                    kind=BudgetKind.USD,
                    quest_cap_microunits=100_000_000,
                    program_cap_microunits=80_000_000,
                    quest_policy={"hard_cap": True, "unit": "USD", "human_increase_only": True},
                    program_policy={"hard_cap": True, "stop_at_cap": True},
                ),
                CommissionedBudgetPlan(
                    kind=BudgetKind.GPU_HOURS,
                    quest_cap_microunits=40_000_000,
                    program_cap_microunits=32_000_000,
                    quest_policy={"hard_cap": True, "unit": "gpu_hour"},
                    program_policy={"hard_cap": True, "stop_at_cap": True},
                ),
                CommissionedBudgetPlan(
                    kind=BudgetKind.TOKENS,
                    quest_cap_microunits=10_000_000_000_000,
                    program_cap_microunits=8_000_000_000_000,
                    quest_policy={"hard_cap": True, "unit": "token"},
                    program_policy={"hard_cap": True, "stop_at_cap": True},
                ),
                CommissionedBudgetPlan(
                    kind=BudgetKind.WALL_CLOCK_HOURS,
                    quest_cap_microunits=120_000_000,
                    program_cap_microunits=96_000_000,
                    quest_policy={"hard_cap": True, "unit": "hour"},
                    program_policy={"hard_cap": True, "stop_at_cap": True},
                ),
                CommissionedBudgetPlan(
                    kind=BudgetKind.EXPERIMENT_COUNT,
                    quest_cap_microunits=30_000_000,
                    program_cap_microunits=24_000_000,
                    quest_policy={"hard_cap": True, "unit": "experiment"},
                    program_policy={"hard_cap": True, "stop_at_cap": True},
                ),
            ),
            key=lambda item: item.kind.value,
        )
    )
    return PhononQuestCommissioningManifest(
        identity_namespace=identity_namespace,
        prepared_at=prepared_at,
        command_principal=command_principal,
        code_identity=capture_commissioning_code_identity(repository_root),
        evidence=evidence,
        quest=quest,
        program=program,
        family=family,
        campaigns=tuple(sorted(campaign_by_key.values(), key=lambda item: item.node_id)),
        runs=tuple(sorted(runs_by_key.values(), key=lambda item: item.run_id or "")),
        campaign_runs=campaign_runs,
        world_models=worlds,
        question_bindings=question_bindings,
        data_asset=data_asset,
        data_role=data_role,
        budgets=budgets,
        initial_active_campaign_id=campaign_by_key["independent-replay"].node_id,
        external_corpus_candidates=_external_candidates(),
        durable_blockers=tuple(
            sorted(
                (
                    "independent_external_dataset_not_yet_qualified_or_registered",
                    "independent_reproduction_result_not_yet_committed",
                    "quest_scoped_fault_prerequisite_not_yet_run",
                    "restart_safe_real_time_endurance_controller_not_yet_commissioned",
                )
            )
        ),
    )


def prepare_phonon_quest_commissioning(
    workspace: Path,
    *,
    prepared_at: datetime,
    command_principal: str,
    identity_namespace: str = _DEFAULT_NAMESPACE,
    repository_root: Path = REPO_ROOT,
) -> PhononQuestCommissioningManifest:
    evidence = inspect_structure_signal_evidence(
        workspace,
        repository_root=repository_root,
    )
    return build_phonon_quest_commissioning_manifest(
        evidence,
        prepared_at=prepared_at,
        command_principal=command_principal,
        identity_namespace=identity_namespace,
        repository_root=repository_root,
    )


def _ensure_run(spec: CommissionedRunSpec) -> bool:
    assert spec.run_id is not None
    values = {
        "id": spec.run_id,
        "goal": spec.goal,
        "domain": spec.domain,
        "direction": spec.direction,
        "human_owner": spec.owner,
        "budget_cap_usd": spec.budget_cap_usd,
        "gpu_hours_cap": spec.gpu_hours_cap,
        "status": spec.initial_status,
    }
    with session_scope() as session:
        inserted = session.scalar(
            postgresql_insert(Run)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(Run.id)
        )
        session.flush()
        row = session.get(Run, spec.run_id)
        if row is None:
            raise PhononCommissioningConflict(f"could not persist commissioned Run: {spec.run_id}")
        immutable = {
            "id": row.id,
            "goal": row.goal,
            "domain": row.domain,
            "direction": row.direction,
            "human_owner": row.human_owner,
            "budget_cap_usd": row.budget_cap_usd,
            "gpu_hours_cap": row.gpu_hours_cap,
        }
        expected = {key: value for key, value in values.items() if key != "status"}
        if immutable != expected or row.status not in RUN_STATUSES:
            raise PhononCommissioningConflict(
                f"commissioned Run stable identity is rebound: {spec.run_id}"
            )
        return inserted is not None


def _ensure_data_asset(spec: CommissionedDataAssetSpec) -> bool:
    assert spec.data_asset_id is not None
    values = {
        "id": spec.data_asset_id,
        "run_id": spec.run_id,
        "role": spec.role,
        "source": spec.source,
        "ref": spec.ref,
        "target_column": spec.target_column,
        "composition_column": spec.composition_column,
        "feature_kind": spec.feature_kind,
        "description": spec.description,
        "status": spec.status,
        "uri": spec.uri,
        "content_sha256": spec.content_sha256,
        "profile_json": spec.profile_json,
        "requested_by": spec.requested_by,
    }
    with session_scope() as session:
        inserted = session.scalar(
            postgresql_insert(DataAsset)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(DataAsset.id)
        )
        session.flush()
        row = session.get(DataAsset, spec.data_asset_id)
        if row is None:
            raise PhononCommissioningConflict(
                f"could not persist commissioned DataAsset: {spec.data_asset_id}"
            )
        observed = {
            "id": row.id,
            "run_id": row.run_id,
            "role": row.role,
            "source": row.source,
            "ref": row.ref,
            "target_column": row.target_column,
            "composition_column": row.composition_column,
            "feature_kind": row.feature_kind,
            "description": row.description,
            "status": row.status,
            "uri": row.uri,
            "content_sha256": row.content_sha256,
            "profile_json": row.profile_json,
            "requested_by": row.requested_by,
        }
        if (
            observed != values
            or row.source not in DATA_SOURCES
            or row.status not in DATA_STATUSES
        ):
            raise PhononCommissioningConflict(
                f"commissioned DataAsset stable identity is rebound: {spec.data_asset_id}"
            )
        return inserted is not None


def _context(
    manifest: PhononQuestCommissioningManifest,
    label: str,
) -> GraphCommandContext:
    assert manifest.commissioning_id is not None
    return GraphCommandContext(
        idempotency_key=f"{manifest.commissioning_id}:{label}",
        principal=manifest.command_principal,
    )


def _assert_commissioned_graph(
    manifest: PhononQuestCommissioningManifest,
    graph: QuestGraphSnapshot,
) -> None:
    expected_specs = {
        manifest.quest.node_id: manifest.quest.model_dump(mode="json"),
        manifest.program.node_id: manifest.program.model_dump(mode="json"),
        **{item.node_id: item.model_dump(mode="json") for item in manifest.campaigns},
    }
    nodes = {item.node_id: item for item in graph.nodes}
    if set(nodes) != set(expected_specs):
        raise PhononCommissioningConflict("commissioned Quest graph has missing or extra nodes")
    for node_id, expected in expected_specs.items():
        if nodes[node_id].spec != expected:
            raise PhononCommissioningConflict(f"commissioned graph spec changed: {node_id}")
    if (
        nodes[manifest.quest.node_id].state is not GraphNodeState.ACTIVE
        or nodes[manifest.program.node_id].state is not GraphNodeState.ACTIVE
        or nodes[manifest.initial_active_campaign_id].state is not GraphNodeState.ACTIVE
    ):
        raise PhononCommissioningConflict("commissioned activation path is incomplete")
    planned = {
        item.node_id
        for item in manifest.campaigns
        if item.node_id != manifest.initial_active_campaign_id
    }
    if any(nodes[node_id].state is not GraphNodeState.PLANNED for node_id in planned):
        raise PhononCommissioningConflict("non-initial Campaign must remain planned")
    expected_versions = {
        manifest.quest.node_id: 2,
        manifest.program.node_id: 2,
        manifest.initial_active_campaign_id: 2,
        **{node_id: 1 for node_id in planned},
    }
    if any(nodes[node_id].state_version != version for node_id, version in expected_versions.items()):
        raise PhononCommissioningConflict("commissioned graph has advanced beyond initial state")
    if graph.dependencies:
        raise PhononCommissioningConflict("initial commissioned graph must not add hidden dependencies")
    if (
        len(graph.scientific_families) != 1
        or graph.scientific_families[0].family_id != manifest.family.family_id
        or graph.scientific_families[0].spec != manifest.family.model_dump(mode="json")
    ):
        raise PhononCommissioningConflict("commissioned scientific family differs from manifest")
    if {
        (item.campaign_id, item.family_id) for item in graph.campaign_families
    } != {(item.node_id, manifest.family.family_id) for item in manifest.campaigns}:
        raise PhononCommissioningConflict("commissioned Campaign/family closure differs")

    expected_runs = {
        (item.campaign_id, item.run_id, item.role) for item in manifest.campaign_runs
    }
    observed_runs = {
        (item.scope_node_id, item.external_id, item.role)
        for item in graph.external_bindings
        if item.binding_type == "run"
    }
    expected_questions = {
        (manifest.program.node_id, item.question_sha256, item.role)
        for item in manifest.question_bindings
    }
    observed_questions = {
        (item.scope_node_id, item.external_id, item.role)
        for item in graph.external_bindings
        if item.binding_type == "research_question"
    }
    if (
        len(graph.external_bindings) != len(expected_runs) + len(expected_questions)
        or observed_runs != expected_runs
        or observed_questions != expected_questions
    ):
        raise PhononCommissioningConflict("commissioned external bindings differ from manifest")
    if len(graph.data_allocations) != 1:
        raise PhononCommissioningConflict("commissioned Quest requires exactly one source allocation")
    data = graph.data_allocations[0]
    if (
        data.scope_node_id != manifest.data_role.scope_node_id
        or data.data_asset_id != manifest.data_role.data_asset_id
        or data.role is not manifest.data_role.role
        or data.exclusive != manifest.data_role.exclusive
        or data.policy != manifest.data_role.policy
    ):
        raise PhononCommissioningConflict("commissioned source allocation differs from manifest")
    if len(graph.budget_allocations) != 2 * len(manifest.budgets):
        raise PhononCommissioningConflict("commissioned budget matrix is incomplete")
    for plan in manifest.budgets:
        quest_rows = [
            item
            for item in graph.budget_allocations
            if item.scope_node_id == manifest.quest.node_id and item.kind is plan.kind
        ]
        program_rows = [
            item
            for item in graph.budget_allocations
            if item.scope_node_id == manifest.program.node_id and item.kind is plan.kind
        ]
        if (
            len(quest_rows) != 1
            or len(program_rows) != 1
            or quest_rows[0].cap_microunits != plan.quest_cap_microunits
            or quest_rows[0].policy != plan.quest_policy
            or program_rows[0].cap_microunits != plan.program_cap_microunits
            or program_rows[0].policy != plan.program_policy
            or program_rows[0].parent_allocation_id != quest_rows[0].allocation_id
        ):
            raise PhononCommissioningConflict(
                f"commissioned {plan.kind.value} budget differs from manifest"
            )
    for world in manifest.world_models:
        if get_world_model_snapshot(world.snapshot_sha256) != world:
            raise PhononCommissioningConflict(
                f"commissioned world model changed: {world.snapshot_sha256}"
            )


def apply_phonon_quest_commissioning(
    manifest: PhononQuestCommissioningManifest,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononQuestCommissioningReceipt:
    """Apply a frozen blueprint exactly once; identical retries only replay receipts."""

    manifest = PhononQuestCommissioningManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    verify_commissioning_artifacts(manifest, repository_root=repository_root)
    created = 0
    replayed = 0
    for run in manifest.runs:
        if _ensure_run(run):
            created += 1
        else:
            replayed += 1
    if _ensure_data_asset(manifest.data_asset):
        created += 1
    else:
        replayed += 1
    for world in manifest.world_models:
        receipt = store_world_model_snapshot(world)
        if receipt.created:
            created += 1
        else:
            replayed += 1

    store = ProgramGraphStore()

    def account(receipt: Any) -> Any:
        nonlocal created, replayed
        if receipt.created:
            created += 1
        else:
            replayed += 1
        return receipt

    account(store.create_quest(manifest.quest, _context(manifest, "create-quest")))
    account(store.create_program(manifest.program, _context(manifest, "create-program")))
    account(
        store.create_scientific_family(
            manifest.family,
            _context(manifest, "create-family"),
        )
    )
    for campaign in manifest.campaigns:
        account(
            store.create_campaign(
                campaign,
                _context(manifest, f"create-campaign:{campaign.node_id}"),
            )
        )
    for binding in manifest.campaign_runs:
        account(
            store.bind_run(
                CampaignRunBindingSpec(
                    campaign_id=binding.campaign_id,
                    run_id=binding.run_id,
                    role=binding.role,
                ),
                _context(manifest, f"bind-run:{binding.campaign_id}"),
            )
        )
    for binding in manifest.question_bindings:
        account(
            store.bind_question(
                ProgramQuestionBindingSpec(
                    program_id=manifest.program.node_id,
                    question_sha256=binding.question_sha256,
                    role=binding.role,
                ),
                _context(manifest, f"bind-question:{binding.question_sha256[:16]}"),
            )
        )
    account(
        store.allocate_data(
            manifest.data_role,
            _context(manifest, "allocate-source-data"),
        )
    )
    for plan in manifest.budgets:
        quest_budget = account(
            store.allocate_budget(
                BudgetAllocationSpec(
                    scope_node_id=manifest.quest.node_id,
                    kind=plan.kind,
                    cap_microunits=plan.quest_cap_microunits,
                    policy=plan.quest_policy,
                ),
                _context(manifest, f"allocate-quest-budget:{plan.kind.value}"),
            )
        )
        account(
            store.allocate_budget(
                BudgetAllocationSpec(
                    scope_node_id=manifest.program.node_id,
                    parent_allocation_id=quest_budget.object_id,
                    kind=plan.kind,
                    cap_microunits=plan.program_cap_microunits,
                    policy=plan.program_policy,
                ),
                _context(manifest, f"allocate-program-budget:{plan.kind.value}"),
            )
        )
    account(
        store.transition_node(
            NodeTransitionSpec(
                node_id=manifest.quest.node_id,
                expected_version=1,
                to_state=GraphNodeState.ACTIVE,
                reason="frozen scientific direction, safety boundary, and budgets approved",
            ),
            _context(manifest, "activate-quest"),
        )
    )
    account(
        store.transition_node(
            NodeTransitionSpec(
                node_id=manifest.program.node_id,
                expected_version=1,
                to_state=GraphNodeState.ACTIVE,
                reason="competing questions, evidence ceiling, and data roles frozen",
            ),
            _context(manifest, "activate-program"),
        )
    )
    account(
        store.transition_node(
            NodeTransitionSpec(
                node_id=manifest.initial_active_campaign_id,
                expected_version=1,
                to_state=GraphNodeState.ACTIVE,
                reason="begin independent implementation replay before mechanism promotion",
            ),
            _context(manifest, "activate-initial-campaign"),
        )
    )
    graph = store.get_quest(manifest.quest.node_id)
    _assert_commissioned_graph(manifest, graph)
    assert manifest.commissioning_id is not None
    assert manifest.data_asset.data_asset_id is not None
    return PhononQuestCommissioningReceipt(
        commissioning_id=manifest.commissioning_id,
        manifest_sha256=manifest.manifest_sha256,
        quest_id=manifest.quest.node_id,
        program_id=manifest.program.node_id,
        campaign_ids=tuple(item.node_id for item in manifest.campaigns),
        run_ids=tuple(str(item.run_id) for item in manifest.runs),
        data_asset_id=manifest.data_asset.data_asset_id,
        question_sha256s=tuple(
            item.question.question_sha256 for item in manifest.world_models
        ),
        world_model_sha256s=tuple(item.snapshot_sha256 for item in manifest.world_models),
        graph_sha256=graph.graph_sha256,
        created_object_count=created,
        replayed_object_count=replayed,
        initial_active_campaign_id=manifest.initial_active_campaign_id,
        durable_blockers=manifest.durable_blockers,
    )


def audit_phonon_quest_commissioning(
    manifest: PhononQuestCommissioningManifest,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononQuestCommissioningReceipt:
    """Revalidate files, world models, data identity, and exact initial graph without mutation."""

    manifest = PhononQuestCommissioningManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    verify_commissioning_artifacts(manifest, repository_root=repository_root)
    for run in manifest.runs:
        _ensure_run_read_only(run)
    _ensure_data_asset_read_only(manifest.data_asset)
    graph = ProgramGraphStore().get_quest(manifest.quest.node_id)
    _assert_commissioned_graph(manifest, graph)
    assert manifest.commissioning_id is not None
    assert manifest.data_asset.data_asset_id is not None
    return PhononQuestCommissioningReceipt(
        commissioning_id=manifest.commissioning_id,
        manifest_sha256=manifest.manifest_sha256,
        quest_id=manifest.quest.node_id,
        program_id=manifest.program.node_id,
        campaign_ids=tuple(item.node_id for item in manifest.campaigns),
        run_ids=tuple(str(item.run_id) for item in manifest.runs),
        data_asset_id=manifest.data_asset.data_asset_id,
        question_sha256s=tuple(
            item.question.question_sha256 for item in manifest.world_models
        ),
        world_model_sha256s=tuple(item.snapshot_sha256 for item in manifest.world_models),
        graph_sha256=graph.graph_sha256,
        created_object_count=0,
        replayed_object_count=(
            len(manifest.runs)
            + 1
            + len(manifest.world_models)
            + len(graph.transitions)
            + len(graph.external_bindings)
            + len(graph.data_allocations)
            + len(graph.budget_allocations)
        ),
        initial_active_campaign_id=manifest.initial_active_campaign_id,
        durable_blockers=manifest.durable_blockers,
    )


def _ensure_run_read_only(spec: CommissionedRunSpec) -> None:
    assert spec.run_id is not None
    with session_scope() as session:
        row = session.get(Run, spec.run_id)
        if row is None:
            raise PhononCommissioningConflict(f"commissioned Run is missing: {spec.run_id}")
        expected = {
            "goal": spec.goal,
            "domain": spec.domain,
            "direction": spec.direction,
            "human_owner": spec.owner,
            "budget_cap_usd": spec.budget_cap_usd,
            "gpu_hours_cap": spec.gpu_hours_cap,
        }
        observed = {key: getattr(row, key) for key in expected}
        if observed != expected or row.status not in RUN_STATUSES:
            raise PhononCommissioningConflict(f"commissioned Run changed: {spec.run_id}")


def _ensure_data_asset_read_only(spec: CommissionedDataAssetSpec) -> None:
    assert spec.data_asset_id is not None
    with session_scope() as session:
        row = session.get(DataAsset, spec.data_asset_id)
        if row is None:
            raise PhononCommissioningConflict(
                f"commissioned DataAsset is missing: {spec.data_asset_id}"
            )
        expected = {
            "run_id": spec.run_id,
            "role": spec.role,
            "source": spec.source,
            "ref": spec.ref,
            "target_column": spec.target_column,
            "composition_column": spec.composition_column,
            "feature_kind": spec.feature_kind,
            "description": spec.description,
            "status": spec.status,
            "uri": spec.uri,
            "content_sha256": spec.content_sha256,
            "profile_json": spec.profile_json,
            "requested_by": spec.requested_by,
        }
        observed = {key: getattr(row, key) for key in expected}
        if observed != expected:
            raise PhononCommissioningConflict(
                f"commissioned DataAsset changed: {spec.data_asset_id}"
            )


__all__ = [
    "CampaignRunSeed",
    "CommissionedBudgetPlan",
    "CommissionedDataAssetSpec",
    "CommissionedRunSpec",
    "CommissioningCodeIdentity",
    "ExternalCorpusCandidate",
    "LocalArtifactIdentity",
    "PhononCommissioningConflict",
    "PhononCommissioningError",
    "PhononCommissioningEvidenceError",
    "PhononQuestCommissioningManifest",
    "PhononQuestCommissioningReceipt",
    "QuestionBindingSeed",
    "StructureSignalEvidenceReceipt",
    "apply_phonon_quest_commissioning",
    "audit_phonon_quest_commissioning",
    "build_phonon_quest_commissioning_manifest",
    "capture_commissioning_code_identity",
    "inspect_structure_signal_evidence",
    "local_artifact_identity",
    "prepare_phonon_quest_commissioning",
    "verify_commissioning_artifacts",
]
