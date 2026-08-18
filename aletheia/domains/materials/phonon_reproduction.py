"""Precommitted implementation-diverse reproduction for the production phonon Quest.

This module intentionally does not call the F10 feature or estimator helpers.  It reloads the
licensed source, independently reconstructs the frozen matrices from public library APIs, checks
their hashes against the pre-fit F10 plan, and fits a different estimator family.  The resulting
claim remains same-source implementation reproduction: it is neither external validation nor a
causal/mechanistic result.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from aletheia.db import REPO_ROOT, engine
from aletheia.domains.materials.capabilities.structure_discrimination import (
    MatchedCapacityReceipt,
    PermutationRoleReceipt,
    StructureArmEvaluation,
    StructureAwareExperimentPlan,
    StructureAwareExperimentResult,
    StructureEvaluationRole,
    StructureExperimentArm,
    StructureSignalDisposition,
    StructureSignalEvaluation,
)
from aletheia.domains.materials.phonon_commissioning import (
    PhononQuestCommissioningManifest,
)
from aletheia.programs.endurance_controller import (
    EnduranceControllerManifest,
    EnduranceEvidenceEnvelope,
    submit_controller_evidence,
)
from aletheia.programs.endurance_schemas import (
    EnduranceCheckpointEvidence,
    EnduranceReproductionConclusion,
    EnduranceReproductionReceipt,
)
from aletheia.programs.graph import ProgramGraphStore
from aletheia.programs.memory import ResearchMemoryStore
from aletheia.programs.memory_schemas import (
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryTaskBindingSpec,
    ResearchMemoryFactSpec,
)
from aletheia.programs.schemas import (
    GraphCommandContext,
    GraphNodeState,
    NodeTransitionSpec,
)
from aletheia.programs.endurance import ResearchEnduranceNotFound, ResearchEnduranceStore
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^edctl_[0-9a-f]{32}$"
_PROTOCOL_ID_PATTERN = r"^pirp_[0-9a-f]{32}$"
_RESULT_ID_PATTERN = r"^pirr_[0-9a-f]{32}$"
_GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_REQUIRED_PACKAGES = (
    "matminer",
    "numpy",
    "pandas",
    "pymatgen",
    "scikit-learn",
    "spglib",
)
_CODE_COMPONENTS = (
    "aletheia/domains/materials/capabilities/structure_discrimination.py",
    "aletheia/domains/materials/featurizers.py",
    "aletheia/domains/materials/phonon_commissioning.py",
    "aletheia/domains/materials/phonon_reproduction.py",
    "aletheia/domains/materials/structures.py",
    "aletheia/jobs/outbox.py",
    "aletheia/programs/endurance.py",
    "aletheia/programs/endurance_controller.py",
    "aletheia/programs/endurance_schemas.py",
    "aletheia/programs/graph.py",
    "aletheia/programs/memory.py",
    "aletheia/programs/persistence.py",
    "scripts/run_phonon_reproduction.py",
)


class PhononReproductionError(RuntimeError):
    """Base implementation-diverse reproduction error."""


class PhononReproductionConflict(PhononReproductionError):
    """A frozen identity, live gate, Campaign, or artifact conflicts."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhononReplayArtifact(_FrozenModel):
    relative_path: str = Field(min_length=1, max_length=1_024)
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> "PhononReplayArtifact":
        _safe_relative(self.relative_path)
        return self


class PhononReplayCodeIdentity(_FrozenModel):
    git_commit: str = Field(pattern=_GIT_SHA_PATTERN)
    component_sha256s: dict[str, str]
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_provenance_verified: bool

    @model_validator(mode="after")
    def _closed_identity(self) -> "PhononReplayCodeIdentity":
        components = dict(sorted(self.component_sha256s.items()))
        if set(components) != set(_CODE_COMPONENTS):
            raise ValueError("phonon replay code-component matrix is incomplete")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in components.values()
        ):
            raise ValueError("phonon replay component hashes must be lowercase SHA-256")
        expected = content_sha256(
            {
                "schema": "aletheia.phonon_replay_code.v1",
                "git_commit": self.git_commit,
                "components": components,
                "committed_provenance_verified": self.committed_provenance_verified,
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("phonon replay aggregate code identity is inconsistent")
        object.__setattr__(self, "component_sha256s", components)
        return self


class IndependentExtraTreesPolicy(_FrozenModel):
    estimator: Literal["sklearn.extra_trees_regressor"] = "sklearn.extra_trees_regressor"
    n_estimators: int = Field(default=512, ge=32, le=4096)
    max_depth: int | None = Field(default=20, ge=2, le=128)
    min_samples_leaf: int = Field(default=2, ge=1, le=1_024)
    max_features: float = Field(default=0.75, gt=0, le=1)
    random_state: int = Field(default=20260829, ge=0, le=2**32 - 1)
    n_jobs: Literal[1] = 1
    bootstrap: Literal[False] = False
    hyperparameter_tuning_forbidden: Literal[True] = True
    fit_once_per_arm: Literal[True] = True

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class PhononIndependentReplayProtocol(_FrozenModel):
    schema_version: Literal[1] = 1
    protocol_id: str | None = Field(default=None, pattern=_PROTOCOL_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    gate_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    commissioning_id: str = Field(pattern=r"^pcm_[0-9a-f]{32}$")
    commissioning_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    original_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    reproduction_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    dataset: PhononReplayArtifact
    source_plan: PhononReplayArtifact
    source_result: PhononReplayArtifact
    source_result_disposition: Literal["robust_aligned_structure_signal"] = (
        "robust_aligned_structure_signal"
    )
    source_split_membership_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_composition_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_structure_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimator_policy: IndependentExtraTreesPolicy
    permutation_seed: int = Field(default=20260830, ge=0, le=2**32 - 1)
    bootstrap_seed: int = Field(default=20260831, ge=0, le=2**32 - 1)
    bootstrap_resamples: int = Field(default=5_000, ge=100, le=100_000)
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)
    minimum_relative_mae_improvement: float = Field(default=0.05, ge=0, le=1)
    required_package_versions: dict[str, str]
    code_identity: PhononReplayCodeIdentity
    prepared_at: AwareDatetime
    execution_class: Literal["production", "engineering"] = "production"
    same_source_implementation_reproduction_only: Literal[True] = True
    independent_external_replication_claim_forbidden: Literal[True] = True
    causal_or_mechanism_claim_forbidden: Literal[True] = True
    outward_actions_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _closed_protocol(self) -> "PhononIndependentReplayProtocol":
        if self.original_campaign_id == self.reproduction_campaign_id:
            raise ValueError("phonon reproduction requires distinct Campaign branches")
        if set(self.required_package_versions) != set(_REQUIRED_PACKAGES):
            raise ValueError("phonon replay package-version matrix is incomplete")
        if any(not value.strip() for value in self.required_package_versions.values()):
            raise ValueError("phonon replay package versions cannot be empty")
        if self.dataset.content_sha256 is not None:
            raise ValueError("raw phonon dataset must not claim a model content identity")
        if self.source_plan.content_sha256 is None or self.source_result.content_sha256 is None:
            raise ValueError("source plan and result require internal content identities")
        if (
            self.execution_class == "production"
            and not self.code_identity.committed_provenance_verified
        ):
            raise ValueError("production phonon replay requires committed code provenance")
        expected = f"pirp_{self.protocol_sha256[:32]}"
        if self.protocol_id is not None and self.protocol_id != expected:
            raise ValueError("phonon replay protocol ID differs from frozen content")
        object.__setattr__(
            self,
            "required_package_versions",
            dict(sorted(self.required_package_versions.items())),
        )
        object.__setattr__(self, "protocol_id", expected)
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"protocol_id"}))


class PhononIndependentReplayResult(_FrozenModel):
    schema_version: Literal[1] = 1
    result_id: str | None = Field(default=None, pattern=_RESULT_ID_PATTERN)
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    reproduction_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    dataset_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    split_membership_sha256: str = Field(pattern=_SHA256_PATTERN)
    composition_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    structure_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimator_policy: IndependentExtraTreesPolicy
    permutation_receipts: tuple[PermutationRoleReceipt, ...] = Field(min_length=3, max_length=3)
    matched_capacity: MatchedCapacityReceipt
    arm_evaluations: tuple[StructureArmEvaluation, ...] = Field(min_length=6, max_length=6)
    signal_evaluations: tuple[StructureSignalEvaluation, ...] = Field(min_length=2, max_length=2)
    minimum_relative_mae_improvement: float = Field(ge=0, le=1)
    disposition: StructureSignalDisposition
    code_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    all_preregistered_arms_retained: Literal[True] = True
    same_source_implementation_reproduction_only: Literal[True] = True
    independent_external_replication_claim_forbidden: Literal[True] = True
    causal_or_mechanism_claim_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _complete_and_derived(self) -> "PhononIndependentReplayResult":
        permutation_roles = tuple(item.role.value for item in self.permutation_receipts)
        if permutation_roles != tuple(sorted(set(permutation_roles))):
            raise ValueError("phonon replay permutation receipts must cover unique roles")
        keys = tuple((item.arm.value, item.role.value) for item in self.arm_evaluations)
        expected_keys = {
            (arm.value, role.value)
            for arm in StructureExperimentArm
            for role in (
                StructureEvaluationRole.INTERNAL_VALIDATION,
                StructureEvaluationRole.LOCKED_HOLDOUT,
            )
        }
        if keys != tuple(sorted(set(keys))) or set(keys) != expected_keys:
            raise ValueError("phonon replay omitted or duplicated an arm/role evaluation")
        if self.matched_capacity.estimator_policy_sha256 != self.estimator_policy.policy_sha256:
            raise ValueError("phonon replay matched-capacity receipt changed estimator policy")
        signal_roles = tuple(item.role.value for item in self.signal_evaluations)
        if signal_roles != tuple(sorted(set(signal_roles))):
            raise ValueError("phonon replay signal roles must be unique and canonical")
        evaluations = {(item.arm, item.role): item for item in self.arm_evaluations}
        for signal in self.signal_evaluations:
            aligned = evaluations[(StructureExperimentArm.ALIGNED_STRUCTURE, signal.role)]
            control = evaluations[(StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL, signal.role)]
            if signal.aligned_mae != aligned.mae or signal.permuted_control_mae != control.mae:
                raise ValueError("phonon replay signal changed its arm metrics")
        expected_disposition = _derive_disposition(
            self.signal_evaluations,
            self.minimum_relative_mae_improvement,
        )
        if self.disposition is not expected_disposition:
            raise ValueError("phonon replay disposition is not derived from frozen acceptance")
        expected = f"pirr_{self.result_sha256[:32]}"
        if self.result_id is not None and self.result_id != expected:
            raise ValueError("phonon replay result ID differs from retained content")
        object.__setattr__(self, "result_id", expected)
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"result_id"}))


class PhononReplayCommitReceipt(_FrozenModel):
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    result_id: str = Field(pattern=_RESULT_ID_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    conclusion: EnduranceReproductionConclusion
    memory_fact_id: str = Field(pattern=r"^mem_[0-9a-f]{32}$")
    memory_fact_created: bool
    envelope: EnduranceEvidenceEnvelope
    envelope_created: bool
    same_source_only: Literal[True] = True
    automatic_pivot: Literal[False] = False


class PhononReplayActivationReceipt(_FrozenModel):
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    command_id: str | None = None
    transition_id: str | None = None
    created: bool
    active_verified: Literal[True] = True


class PhononReplayPreflight(_FrozenModel):
    protocol_id: str = Field(pattern=_PROTOCOL_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    database_observed_at: AwareDatetime
    ready_for_gate_start: bool
    blockers: tuple[str, ...]
    code_identity_verified: bool
    source_artifacts_verified: bool
    original_campaign_state: GraphNodeState
    reproduction_campaign_state: GraphNodeState
    model_fit_count: Literal[0] = 0
    same_source_only: Literal[True] = True

    @model_validator(mode="after")
    def _derived_preflight(self) -> "PhononReplayPreflight":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("phonon replay preflight blockers must be canonical")
        expected = not blockers and self.code_identity_verified and self.source_artifacts_verified
        if self.ready_for_gate_start != expected:
            raise ValueError("phonon replay preflight verdict differs from blockers")
        return self


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("phonon replay artifacts require safe relative paths")
    return path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repository_root: Path, *args: str, text: bool = True) -> Any:
    result = subprocess.run(
        ("git", *args),
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout.strip() if text else result.stdout


def _components_are_committed(repository_root: Path) -> bool:
    tracked = subprocess.run(
        ("git", "ls-files", "--error-unmatch", *_CODE_COMPONENTS),
        cwd=repository_root,
        capture_output=True,
        text=True,
    )
    unstaged = subprocess.run(
        ("git", "diff", "--quiet", "--", *_CODE_COMPONENTS), cwd=repository_root
    )
    staged = subprocess.run(
        ("git", "diff", "--cached", "--quiet", "--", *_CODE_COMPONENTS),
        cwd=repository_root,
    )
    return tracked.returncode == unstaged.returncode == staged.returncode == 0


def capture_phonon_replay_code_identity(
    *,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononReplayCodeIdentity:
    root = repository_root.resolve()
    components = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    committed = _components_are_committed(root)
    if require_committed and not committed:
        raise PhononReproductionConflict(
            "phonon replay components must be tracked and committed before preparation"
        )
    commit = _git(root, "rev-parse", "HEAD")
    projection = {
        "schema": "aletheia.phonon_replay_code.v1",
        "git_commit": commit,
        "components": dict(sorted(components.items())),
        "committed_provenance_verified": committed and require_committed,
    }
    return PhononReplayCodeIdentity(
        git_commit=commit,
        component_sha256s=components,
        aggregate_sha256=content_sha256(projection),
        committed_provenance_verified=committed and require_committed,
    )


def verify_phonon_replay_code_identity(
    identity: PhononReplayCodeIdentity,
    *,
    repository_root: Path = REPO_ROOT,
) -> None:
    root = repository_root.resolve()
    live = {
        relative: _sha256_file((root / relative).resolve(strict=True))
        for relative in _CODE_COMPONENTS
    }
    if dict(sorted(live.items())) != identity.component_sha256s:
        raise PhononReproductionConflict("live phonon replay code differs from frozen identity")
    if identity.committed_provenance_verified:
        try:
            _git(root, "cat-file", "-e", f"{identity.git_commit}^{{commit}}")
            frozen = {
                relative: _sha256_bytes(
                    _git(root, "show", f"{identity.git_commit}:{relative}", text=False)
                )
                for relative in _CODE_COMPONENTS
            }
        except subprocess.CalledProcessError as exc:
            raise PhononReproductionConflict(
                "frozen phonon replay git provenance cannot be reconstructed"
            ) from exc
        if dict(sorted(frozen.items())) != identity.component_sha256s:
            raise PhononReproductionConflict(
                "frozen commit does not contain the bound phonon replay components"
            )


def _package_versions() -> dict[str, str]:
    return {name: importlib.metadata.version(name) for name in _REQUIRED_PACKAGES}


def _artifact(path: Path, *, root: Path, content: str | None = None) -> PhononReplayArtifact:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PhononReproductionConflict("phonon replay artifact escaped repository") from exc
    return PhononReplayArtifact(
        relative_path=relative,
        file_sha256=_sha256_file(resolved),
        content_sha256=content,
    )


def verify_phonon_replay_artifact_files(
    protocol: PhononIndependentReplayProtocol,
    *,
    repository_root: Path = REPO_ROOT,
) -> dict[str, Path]:
    root = repository_root.resolve()
    resolved: dict[str, Path] = {}
    for label, binding in (
        ("dataset", protocol.dataset),
        ("source_plan", protocol.source_plan),
        ("source_result", protocol.source_result),
    ):
        relative = _safe_relative(binding.relative_path)
        path = (root / Path(*relative.parts)).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PhononReproductionConflict(f"phonon replay {label} escaped repository") from exc
        if not path.is_file() or _sha256_file(path) != binding.file_sha256:
            raise PhononReproductionConflict(f"phonon replay {label} file bytes changed")
        resolved[label] = path
    return resolved


def prepare_phonon_independent_replay_protocol(
    *,
    controller: EnduranceControllerManifest,
    commissioning: PhononQuestCommissioningManifest,
    dataset_path: Path,
    source_plan_path: Path,
    source_result_path: Path,
    source_plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    reproduction_campaign_id: str,
    prepared_at: datetime,
    repository_root: Path = REPO_ROOT,
    require_committed: bool = True,
) -> PhononIndependentReplayProtocol:
    gate = controller.gate_manifest
    if gate.quest_id != commissioning.quest.node_id:
        raise PhononReproductionConflict("controller and commissioning bind different Quests")
    if controller.controller_id is None or gate.gate_id is None:
        raise PhononReproductionConflict("controller/gate identity is incomplete")
    campaigns = {item.node_id: item for item in commissioning.campaigns}
    if reproduction_campaign_id not in campaigns:
        raise PhononReproductionConflict("reproduction Campaign is outside commissioning")
    if reproduction_campaign_id == commissioning.initial_active_campaign_id:
        raise PhononReproductionConflict("reproduction Campaign must differ from original branch")
    if set(campaigns) != set(gate.initial_campaign_ids):
        raise PhononReproductionConflict("gate initial Campaigns differ from commissioning")
    if source_plan.dataset_receipt.source.sha256 != commissioning.evidence.dataset_file.sha256:
        raise PhononReproductionConflict("source plan dataset differs from commissioning")
    if source_result.plan_sha256 != source_plan.plan_sha256:
        raise PhononReproductionConflict("source result belongs to another plan")
    if source_result.result_sha256 != commissioning.evidence.result_sha256:
        raise PhononReproductionConflict("source result differs from commissioned evidence")
    if source_result.disposition is not StructureSignalDisposition.ROBUST_ALIGNED_STRUCTURE_SIGNAL:
        raise PhononReproductionConflict("source result lacks the frozen robust signal")
    dataset = _artifact(dataset_path, root=repository_root)
    if dataset.file_sha256 != commissioning.evidence.dataset_file.sha256:
        raise PhononReproductionConflict("phonon replay dataset bytes changed")
    plan_binding = _artifact(
        source_plan_path,
        root=repository_root,
        content=source_plan.plan_sha256,
    )
    result_binding = _artifact(
        source_result_path,
        root=repository_root,
        content=source_result.result_sha256,
    )
    return PhononIndependentReplayProtocol(
        quest_id=gate.quest_id,
        gate_id=gate.gate_id,
        gate_manifest_sha256=gate.manifest_sha256,
        controller_id=controller.controller_id,
        controller_manifest_sha256=controller.manifest_sha256,
        commissioning_id=str(commissioning.commissioning_id),
        commissioning_manifest_sha256=commissioning.manifest_sha256,
        original_campaign_id=commissioning.initial_active_campaign_id,
        reproduction_campaign_id=reproduction_campaign_id,
        dataset=dataset,
        source_plan=plan_binding,
        source_result=result_binding,
        source_split_membership_sha256=source_plan.split_receipt.membership_sha256,
        source_composition_matrix_sha256=source_plan.composition_features.matrix_sha256,
        source_structure_matrix_sha256=source_plan.structure_features.matrix_sha256,
        estimator_policy=IndependentExtraTreesPolicy(),
        required_package_versions=_package_versions(),
        code_identity=capture_phonon_replay_code_identity(
            repository_root=repository_root,
            require_committed=require_committed,
        ),
        prepared_at=prepared_at,
        execution_class="production" if require_committed else "engineering",
    )


def _matrix_sha256(matrix: Any) -> str:
    import numpy as np

    contiguous = np.ascontiguousarray(matrix, dtype=np.float64)
    return content_sha256(
        {
            "shape": list(contiguous.shape),
            "dtype": str(contiguous.dtype),
            "bytes_sha256": _sha256_bytes(contiguous.tobytes()),
        }
    )


def _direct_composition_matrix(plan: StructureAwareExperimentPlan) -> Any:
    import numpy as np
    import pandas as pd
    from matminer.featurizers.composition import ElementProperty
    from pymatgen.core import Composition

    frame = pd.DataFrame(
        {
            "composition": [
                Composition(item.formula.canonical_formula) for item in plan.quality_ledger.rows
            ]
        }
    )
    featurizer = ElementProperty.from_preset("magpie")
    try:
        featurizer.set_n_jobs(1)
    except Exception:  # pragma: no cover - supported matminer versions expose one of these
        featurizer.n_jobs = 1
    computed = featurizer.featurize_dataframe(
        frame,
        "composition",
        ignore_errors=False,
        pbar=False,
    )
    names = tuple(str(item) for item in featurizer.feature_labels())
    if names != plan.composition_features.feature_names:
        raise PhononReproductionConflict("independent Magpie feature names changed")
    matrix = np.ascontiguousarray(
        computed[list(names)].to_numpy(dtype=np.float64), dtype=np.float64
    )
    if not np.isfinite(matrix).all():
        raise PhononReproductionConflict("independent composition matrix contains nonfinite values")
    if _matrix_sha256(matrix) != plan.composition_features.matrix_sha256:
        raise PhononReproductionConflict("independent composition matrix differs from frozen plan")
    return matrix


def _direct_structure_row(structure: Any, plan: StructureAwareExperimentPlan) -> tuple[float, ...]:
    import numpy as np
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    quality = plan.protocol.quality_policy
    features = plan.protocol.geometry_feature_policy
    analyzer = SpacegroupAnalyzer(
        structure,
        symprec=quality.symprec_angstrom,
        angle_tolerance=quality.angle_tolerance_degree,
    )
    standardized = analyzer.get_primitive_standard_structure(
        international_monoclinic=True,
        keep_site_properties=False,
    )
    standardized_analyzer = SpacegroupAnalyzer(
        standardized,
        symprec=quality.symprec_angstrom,
        angle_tolerance=quality.angle_tolerance_degree,
    )
    space_group = standardized_analyzer.get_space_group_number()
    crystal_system_index = {
        "triclinic": 1,
        "monoclinic": 2,
        "orthorhombic": 3,
        "tetragonal": 4,
        "trigonal": 5,
        "hexagonal": 6,
        "cubic": 7,
    }[standardized_analyzer.get_crystal_system()]
    maximum_radius = features.radial_bin_edges_angstrom[-1]
    distances = np.asarray(
        [
            float(neighbor.nn_distance)
            for site in standardized
            for neighbor in standardized.get_neighbors(site, maximum_radius)
            if neighbor.nn_distance > 1e-12
        ],
        dtype=np.float64,
    )
    if not len(distances):
        raise PhononReproductionConflict("independent structure row has no periodic neighbors")
    histogram, _ = np.histogram(
        distances,
        bins=np.asarray(features.radial_bin_edges_angstrom, dtype=np.float64),
    )
    a = float(standardized.lattice.a)
    row = (
        float(standardized.volume / len(standardized)),
        float(standardized.lattice.b / a),
        float(standardized.lattice.c / a),
        float(standardized.lattice.alpha),
        float(standardized.lattice.beta),
        float(standardized.lattice.gamma),
        float(space_group),
        float(distances.min()),
        float(distances.mean()),
        float(distances.std(ddof=0)),
        float(distances.max()),
        *(1.0 if index == crystal_system_index else 0.0 for index in range(1, 8)),
        *(float(value) / len(standardized) for value in histogram.tolist()),
    )
    if any(not math.isfinite(value) for value in row):
        raise PhononReproductionConflict("independent structure row contains nonfinite values")
    return row


def _direct_structure_matrix(
    plan: StructureAwareExperimentPlan, structures: tuple[Any, ...]
) -> Any:
    import numpy as np

    if len(structures) != len(plan.quality_ledger.rows):
        raise PhononReproductionConflict("independent structure rows differ from frozen plan")
    for structure, receipt in zip(structures, plan.quality_ledger.rows, strict=True):
        if content_sha256(structure.as_dict()) != receipt.source_row_sha256:
            raise PhononReproductionConflict(
                f"independent structure row changed: {receipt.row_position}"
            )
    matrix = np.ascontiguousarray(
        [_direct_structure_row(structure, plan) for structure in structures],
        dtype=np.float64,
    )
    if matrix.shape != (
        len(structures),
        plan.structure_features.feature_count,
    ):
        raise PhononReproductionConflict("independent structure matrix shape changed")
    if _matrix_sha256(matrix) != plan.structure_features.matrix_sha256:
        raise PhononReproductionConflict("independent structure matrix differs from frozen plan")
    return matrix


def _role_indices(plan: StructureAwareExperimentPlan, role: StructureEvaluationRole) -> Any:
    import numpy as np

    return np.asarray(
        [item.row_position for item in plan.split_receipt.assignments if item.role is role],
        dtype=np.int64,
    )


def _permuted_structure_matrix(
    protocol: PhononIndependentReplayProtocol,
    plan: StructureAwareExperimentPlan,
    matrix: Any,
) -> tuple[Any, tuple[PermutationRoleReceipt, ...]]:
    import numpy as np

    permuted_matrix = np.empty_like(matrix)
    receipts = []
    for offset, role in enumerate(sorted(StructureEvaluationRole, key=lambda item: item.value)):
        indices = _role_indices(plan, role)
        random = np.random.default_rng(protocol.permutation_seed + offset)
        permuted = random.permutation(indices)
        permuted_matrix[indices] = matrix[permuted]
        receipts.append(
            PermutationRoleReceipt(
                role=role,
                row_count=len(indices),
                source_positions_sha256=content_sha256(indices.tolist()),
                permuted_positions_sha256=content_sha256(permuted.tolist()),
            )
        )
    return permuted_matrix, tuple(receipts)


def _fit_predict(
    policy: IndependentExtraTreesPolicy,
    matrix: Any,
    targets: Any,
    train_indices: Any,
    evaluation_indices: dict[StructureEvaluationRole, Any],
) -> dict[StructureEvaluationRole, Any]:
    from sklearn.ensemble import ExtraTreesRegressor

    estimator = ExtraTreesRegressor(
        n_estimators=policy.n_estimators,
        max_depth=policy.max_depth,
        min_samples_leaf=policy.min_samples_leaf,
        max_features=policy.max_features,
        random_state=policy.random_state,
        n_jobs=policy.n_jobs,
        bootstrap=policy.bootstrap,
    )
    estimator.fit(matrix[train_indices], targets[train_indices])
    return {
        role: estimator.predict(matrix[indices]) for role, indices in evaluation_indices.items()
    }


def _arm_evaluation(
    *,
    arm: StructureExperimentArm,
    role: StructureEvaluationRole,
    targets: Any,
    indices: Any,
    predictions: Any,
    feature_count: int,
) -> StructureArmEvaluation:
    import numpy as np

    actual = np.ascontiguousarray(targets[indices], dtype=np.float64)
    predicted = np.ascontiguousarray(predictions, dtype=np.float64)
    errors = np.ascontiguousarray(np.abs(actual - predicted), dtype=np.float64)
    return StructureArmEvaluation(
        arm=arm,
        role=role,
        row_count=len(indices),
        feature_count=feature_count,
        mae=float(errors.mean()),
        rmse=float(np.sqrt(np.mean((actual - predicted) ** 2))),
        predictions_sha256=_matrix_sha256(predicted.reshape(-1, 1)),
        absolute_errors_sha256=_matrix_sha256(errors.reshape(-1, 1)),
    )


def _bootstrap_signal(
    *,
    protocol: PhononIndependentReplayProtocol,
    plan: StructureAwareExperimentPlan,
    role: StructureEvaluationRole,
    targets: Any,
    indices: Any,
    aligned_predictions: Any,
    control_predictions: Any,
) -> StructureSignalEvaluation:
    import numpy as np

    actual = np.asarray(targets[indices], dtype=np.float64)
    aligned_errors = np.abs(actual - np.asarray(aligned_predictions, dtype=np.float64))
    control_errors = np.abs(actual - np.asarray(control_predictions, dtype=np.float64))
    deltas = control_errors - aligned_errors
    groups: dict[str, list[int]] = {}
    assignments = plan.split_receipt.assignments
    for local_position, row_position in enumerate(indices.tolist()):
        identity = assignments[row_position].chemical_system_identity_sha256
        groups.setdefault(identity, []).append(local_position)
    group_ids = sorted(groups)
    random = np.random.default_rng(
        protocol.bootstrap_seed + (0 if role is StructureEvaluationRole.INTERNAL_VALIDATION else 1)
    )
    distribution = np.empty(protocol.bootstrap_resamples, dtype=np.float64)
    for iteration in range(protocol.bootstrap_resamples):
        sampled = random.integers(0, len(group_ids), size=len(group_ids))
        selected = [index for group_index in sampled for index in groups[group_ids[group_index]]]
        distribution[iteration] = float(deltas[selected].mean())
    alpha = 1.0 - protocol.confidence_level
    lower, upper = np.quantile(distribution, [alpha / 2, 1 - alpha / 2])
    aligned_mae = float(aligned_errors.mean())
    control_mae = float(control_errors.mean())
    delta = control_mae - aligned_mae
    return StructureSignalEvaluation(
        role=role,
        aligned_mae=aligned_mae,
        permuted_control_mae=control_mae,
        control_minus_aligned_mae=delta,
        relative_mae_improvement=delta / control_mae if control_mae else 0.0,
        cluster_ci_lower=float(lower),
        cluster_ci_upper=float(upper),
        bootstrap_probability_improvement=float(np.mean(distribution > 0)),
        bootstrap_distribution_sha256=_matrix_sha256(distribution.reshape(-1, 1)),
    )


def _derive_disposition(
    signals: tuple[StructureSignalEvaluation, ...],
    minimum_relative_improvement: float,
) -> StructureSignalDisposition:
    if all(
        item.cluster_ci_lower > 0 and item.relative_mae_improvement >= minimum_relative_improvement
        for item in signals
    ):
        return StructureSignalDisposition.ROBUST_ALIGNED_STRUCTURE_SIGNAL
    if all(item.control_minus_aligned_mae > 0 for item in signals):
        return StructureSignalDisposition.SUGGESTIVE_NOT_ROBUST
    return StructureSignalDisposition.NO_ALIGNED_STRUCTURE_ADVANTAGE


def _calculate_independent_replay(
    *,
    protocol: PhononIndependentReplayProtocol,
    plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
    repository_root: Path,
) -> dict[str, Any]:
    import numpy as np

    verify_phonon_replay_code_identity(protocol.code_identity, repository_root=repository_root)
    if protocol.required_package_versions != _package_versions():
        raise PhononReproductionConflict("phonon replay package versions changed")
    if _sha256_bytes(dataset_file_bytes) != protocol.dataset.file_sha256:
        raise PhononReproductionConflict("phonon replay dataset bytes changed")
    if plan.plan_sha256 != protocol.source_plan.content_sha256:
        raise PhononReproductionConflict("phonon replay source plan identity changed")
    if source_result.result_sha256 != protocol.source_result.content_sha256:
        raise PhononReproductionConflict("phonon replay source result identity changed")
    if source_result.plan_sha256 != plan.plan_sha256:
        raise PhononReproductionConflict("phonon replay source result belongs to another plan")
    if source_result.disposition.value != protocol.source_result_disposition:
        raise PhononReproductionConflict("phonon replay source disposition changed")
    if plan.split_receipt.membership_sha256 != protocol.source_split_membership_sha256:
        raise PhononReproductionConflict("phonon replay split membership changed")
    if len(dataframe) != plan.dataset_receipt.row_count:
        raise PhononReproductionConflict("phonon replay dataframe row count changed")
    dataset_contract = plan.protocol.dataset
    structures = tuple(dataframe[dataset_contract.structure_column].tolist())
    targets = np.ascontiguousarray(
        dataframe[dataset_contract.target_column].to_numpy(dtype=np.float64),
        dtype=np.float64,
    )
    if not np.isfinite(targets).all():
        raise PhononReproductionConflict("phonon replay target contains nonfinite values")
    if _matrix_sha256(targets.reshape(-1, 1)) != plan.dataset_receipt.target_vector_sha256:
        raise PhononReproductionConflict("phonon replay target vector differs from frozen plan")
    composition = _direct_composition_matrix(plan)
    structure = _direct_structure_matrix(plan, structures)
    if (
        _matrix_sha256(composition) != protocol.source_composition_matrix_sha256
        or _matrix_sha256(structure) != protocol.source_structure_matrix_sha256
    ):
        raise PhononReproductionConflict("phonon replay feature lineage changed")
    permuted, permutation_receipts = _permuted_structure_matrix(protocol, plan, structure)
    matrices = {
        StructureExperimentArm.COMPOSITION_ONLY: composition,
        StructureExperimentArm.ALIGNED_STRUCTURE: np.ascontiguousarray(
            np.hstack((composition, structure)), dtype=np.float64
        ),
        StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL: np.ascontiguousarray(
            np.hstack((composition, permuted)), dtype=np.float64
        ),
    }
    train_indices = _role_indices(plan, StructureEvaluationRole.TRAIN)
    evaluation_indices = {
        role: _role_indices(plan, role)
        for role in (
            StructureEvaluationRole.INTERNAL_VALIDATION,
            StructureEvaluationRole.LOCKED_HOLDOUT,
        )
    }
    predictions = {
        arm: _fit_predict(
            protocol.estimator_policy,
            matrix,
            targets,
            train_indices,
            evaluation_indices,
        )
        for arm, matrix in matrices.items()
    }
    arm_evaluations = tuple(
        sorted(
            (
                _arm_evaluation(
                    arm=arm,
                    role=role,
                    targets=targets,
                    indices=evaluation_indices[role],
                    predictions=predictions[arm][role],
                    feature_count=matrices[arm].shape[1],
                )
                for arm in StructureExperimentArm
                for role in evaluation_indices
            ),
            key=lambda item: (item.arm.value, item.role.value),
        )
    )
    signals = tuple(
        sorted(
            (
                _bootstrap_signal(
                    protocol=protocol,
                    plan=plan,
                    role=role,
                    targets=targets,
                    indices=evaluation_indices[role],
                    aligned_predictions=predictions[StructureExperimentArm.ALIGNED_STRUCTURE][role],
                    control_predictions=predictions[
                        StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL
                    ][role],
                )
                for role in evaluation_indices
            ),
            key=lambda item: item.role.value,
        )
    )
    return {
        "permutation_receipts": permutation_receipts,
        "matched_capacity": MatchedCapacityReceipt(
            aligned_feature_count=matrices[StructureExperimentArm.ALIGNED_STRUCTURE].shape[1],
            permuted_control_feature_count=matrices[
                StructureExperimentArm.PERMUTED_STRUCTURE_CONTROL
            ].shape[1],
            train_rows_each=len(train_indices),
            estimator_policy_sha256=protocol.estimator_policy.policy_sha256,
        ),
        "arm_evaluations": arm_evaluations,
        "signal_evaluations": signals,
        "disposition": _derive_disposition(
            signals,
            protocol.minimum_relative_mae_improvement,
        ),
    }


def _build_result(
    protocol: PhononIndependentReplayProtocol,
    calculation: dict[str, Any],
    *,
    completed_at: datetime,
) -> PhononIndependentReplayResult:
    if completed_at <= protocol.prepared_at:
        raise PhononReproductionConflict("phonon replay completed before protocol freeze")
    assert protocol.protocol_id is not None
    return PhononIndependentReplayResult(
        protocol_id=protocol.protocol_id,
        protocol_sha256=protocol.protocol_sha256,
        gate_id=protocol.gate_id,
        reproduction_campaign_id=protocol.reproduction_campaign_id,
        dataset_file_sha256=protocol.dataset.file_sha256,
        source_plan_sha256=str(protocol.source_plan.content_sha256),
        source_result_sha256=str(protocol.source_result.content_sha256),
        split_membership_sha256=protocol.source_split_membership_sha256,
        composition_matrix_sha256=protocol.source_composition_matrix_sha256,
        structure_matrix_sha256=protocol.source_structure_matrix_sha256,
        estimator_policy=protocol.estimator_policy,
        permutation_receipts=calculation["permutation_receipts"],
        matched_capacity=calculation["matched_capacity"],
        arm_evaluations=calculation["arm_evaluations"],
        signal_evaluations=calculation["signal_evaluations"],
        minimum_relative_mae_improvement=protocol.minimum_relative_mae_improvement,
        disposition=calculation["disposition"],
        code_sha256=protocol.code_identity.aggregate_sha256,
        completed_at=completed_at,
    )


def run_phonon_independent_replay(
    *,
    protocol: PhononIndependentReplayProtocol,
    plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
    completed_at: datetime,
    repository_root: Path = REPO_ROOT,
) -> PhononIndependentReplayResult:
    calculation = _calculate_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        repository_root=repository_root,
    )
    return _build_result(protocol, calculation, completed_at=completed_at)


def verify_phonon_independent_replay(
    *,
    protocol: PhononIndependentReplayProtocol,
    result: PhononIndependentReplayResult,
    plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
    repository_root: Path = REPO_ROOT,
) -> None:
    expected = run_phonon_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        completed_at=result.completed_at,
        repository_root=repository_root,
    )
    if expected != result:
        raise PhononReproductionConflict("phonon independent replay does not reproduce exactly")


def _database_now() -> datetime:
    with engine().connect() as connection:
        observed = connection.scalar(select(func.clock_timestamp()))
    if observed is None or observed.tzinfo is None or observed.utcoffset() is None:
        raise PhononReproductionConflict("PostgreSQL did not provide an aware replay clock")
    return observed


def preflight_phonon_independent_replay(
    protocol: PhononIndependentReplayProtocol,
    controller: EnduranceControllerManifest,
    *,
    repository_root: Path = REPO_ROOT,
) -> PhononReplayPreflight:
    blockers: list[str] = []
    code_ok = True
    artifacts_ok = True
    if protocol.execution_class != "production":
        blockers.append("protocol:engineering_only")
    if (
        controller.controller_id != protocol.controller_id
        or controller.manifest_sha256 != protocol.controller_manifest_sha256
        or controller.gate_manifest.gate_id != protocol.gate_id
    ):
        blockers.append("controller:binding_changed")
    try:
        verify_phonon_replay_code_identity(protocol.code_identity, repository_root=repository_root)
    except Exception:
        code_ok = False
        blockers.append("code:identity_drift")
    try:
        verify_phonon_replay_artifact_files(protocol, repository_root=repository_root)
    except Exception:
        artifacts_ok = False
        blockers.append("source:artifact_drift")
    graph = ProgramGraphStore().get_quest(protocol.quest_id)
    by_id = {item.node_id: item for item in graph.nodes}
    original = by_id.get(protocol.original_campaign_id)
    reproduction = by_id.get(protocol.reproduction_campaign_id)
    if original is None or reproduction is None:
        raise PhononReproductionConflict("preflight Campaign disappeared from Quest")
    if original.state is not GraphNodeState.ACTIVE:
        blockers.append("campaign:original_not_active")
    if reproduction.state is not GraphNodeState.PLANNED:
        blockers.append("campaign:reproduction_not_planned")
    try:
        ResearchEnduranceStore().get(protocol.gate_id)
    except ResearchEnduranceNotFound:
        pass
    else:
        blockers.append("gate:already_started")
    canonical = tuple(sorted(set(blockers)))
    assert protocol.protocol_id is not None
    return PhononReplayPreflight(
        protocol_id=protocol.protocol_id,
        gate_id=protocol.gate_id,
        controller_id=protocol.controller_id,
        database_observed_at=_database_now(),
        ready_for_gate_start=not canonical and code_ok and artifacts_ok,
        blockers=canonical,
        code_identity_verified=code_ok,
        source_artifacts_verified=artifacts_ok,
        original_campaign_state=original.state,
        reproduction_campaign_state=reproduction.state,
    )


def _running_graph(protocol: PhononIndependentReplayProtocol) -> Any:
    if protocol.execution_class != "production":
        raise PhononReproductionConflict("engineering replay protocol cannot mutate production")
    try:
        snapshot = ResearchEnduranceStore().get(protocol.gate_id)
    except ResearchEnduranceNotFound as exc:
        raise PhononReproductionConflict("phonon endurance gate has not been started") from exc
    if snapshot.report is not None:
        raise PhononReproductionConflict("terminal phonon endurance gate rejects reproduction work")
    if (
        snapshot.manifest.manifest_sha256 != protocol.gate_manifest_sha256
        or snapshot.manifest.quest_id != protocol.quest_id
    ):
        raise PhononReproductionConflict("live endurance gate differs from replay protocol")
    graph = ProgramGraphStore().get_quest(protocol.quest_id)
    by_id = {item.node_id: item for item in graph.nodes}
    if protocol.original_campaign_id not in by_id or protocol.reproduction_campaign_id not in by_id:
        raise PhononReproductionConflict("phonon replay Campaign disappeared from Quest graph")
    return graph


def activate_phonon_reproduction_campaign(
    protocol: PhononIndependentReplayProtocol,
    *,
    principal: str,
    repository_root: Path = REPO_ROOT,
) -> PhononReplayActivationReceipt:
    verify_phonon_replay_code_identity(protocol.code_identity, repository_root=repository_root)
    graph = _running_graph(protocol)
    by_id = {item.node_id: item for item in graph.nodes}
    original = by_id[protocol.original_campaign_id]
    reproduction = by_id[protocol.reproduction_campaign_id]
    if original.state is not GraphNodeState.ACTIVE:
        raise PhononReproductionConflict("original-evidence Campaign is not active")
    context = GraphCommandContext(
        idempotency_key=f"phonon-replay:{protocol.protocol_id}:activate",
        principal=principal,
    )
    if reproduction.state is GraphNodeState.ACTIVE:
        command_id = None
        transition_id = None
        created = False
    elif reproduction.state is GraphNodeState.PLANNED:
        mutation = ProgramGraphStore().transition_node(
            NodeTransitionSpec(
                node_id=protocol.reproduction_campaign_id,
                expected_version=reproduction.state_version,
                to_state=GraphNodeState.ACTIVE,
                reason=(
                    "Activate the precommitted implementation-diverse same-source reproduction; "
                    "external and causal claims remain forbidden."
                ),
            ),
            context,
        )
        command_id = mutation.command.command_id
        transition_id = str(mutation.command.result["transition_id"])
        created = mutation.created
    else:
        raise PhononReproductionConflict(
            f"reproduction Campaign cannot activate from {reproduction.state.value}"
        )
    updated = ProgramGraphStore().get_quest(protocol.quest_id)
    current = next(
        item for item in updated.nodes if item.node_id == protocol.reproduction_campaign_id
    )
    if current.state is not GraphNodeState.ACTIVE:
        raise PhononReproductionConflict("reproduction Campaign activation did not persist")
    assert protocol.protocol_id is not None
    return PhononReplayActivationReceipt(
        protocol_id=protocol.protocol_id,
        campaign_id=protocol.reproduction_campaign_id,
        command_id=command_id,
        transition_id=transition_id,
        created=created,
    )


def execute_phonon_independent_replay(
    *,
    protocol: PhononIndependentReplayProtocol,
    plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
    repository_root: Path = REPO_ROOT,
) -> PhononIndependentReplayResult:
    verify_phonon_replay_artifact_files(protocol, repository_root=repository_root)
    graph = _running_graph(protocol)
    by_id = {item.node_id: item for item in graph.nodes}
    if any(
        by_id[campaign_id].state is not GraphNodeState.ACTIVE
        for campaign_id in (protocol.original_campaign_id, protocol.reproduction_campaign_id)
    ):
        raise PhononReproductionConflict(
            "original and reproduction Campaigns must both be active before model fitting"
        )
    calculation = _calculate_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        repository_root=repository_root,
    )
    completed_at = _database_now()
    # Close the race where the gate terminalized or a Campaign stopped during computation.
    graph = _running_graph(protocol)
    by_id = {item.node_id: item for item in graph.nodes}
    if any(
        by_id[campaign_id].state is not GraphNodeState.ACTIVE
        for campaign_id in (protocol.original_campaign_id, protocol.reproduction_campaign_id)
    ):
        raise PhononReproductionConflict("phonon Campaign state changed during reproduction")
    return _build_result(protocol, calculation, completed_at=completed_at)


def _reproduction_conclusion(
    result: PhononIndependentReplayResult,
) -> EnduranceReproductionConclusion:
    if result.disposition is StructureSignalDisposition.ROBUST_ALIGNED_STRUCTURE_SIGNAL:
        return EnduranceReproductionConclusion.CONFIRMED
    if result.disposition is StructureSignalDisposition.NO_ALIGNED_STRUCTURE_ADVANTAGE:
        return EnduranceReproductionConclusion.CONTRADICTED
    return EnduranceReproductionConclusion.INCONCLUSIVE


def _outcome_fact(
    protocol: PhononIndependentReplayProtocol,
    result: PhononIndependentReplayResult,
    *,
    result_uri: str,
) -> ResearchMemoryFactSpec:
    conclusion = _reproduction_conclusion(result)
    if conclusion is EnduranceReproductionConclusion.CONFIRMED:
        kind = MemoryFactKind.RESULT
        statement = (
            "Implementation-diverse same-source replay confirmed the preregistered aligned-"
            "structure signal; this is not independent external replication or a causal result."
        )
    elif conclusion is EnduranceReproductionConclusion.CONTRADICTED:
        kind = MemoryFactKind.NEGATIVE_RESULT
        statement = (
            "Implementation-diverse same-source replay contradicted the preregistered robust "
            "aligned-structure signal and must trigger strategy review without claim repair."
        )
    else:
        kind = MemoryFactKind.LIMITATION
        statement = (
            "Implementation-diverse same-source replay was inconclusive under the frozen robust-"
            "signal threshold; the uncertainty must be retained exactly."
        )
    return ResearchMemoryFactSpec(
        scope_node_id=protocol.reproduction_campaign_id,
        kind=kind,
        statement=statement,
        detail={
            "schema": "aletheia.phonon_independent_replay_outcome.v1",
            "protocol_id": protocol.protocol_id,
            "protocol_sha256": protocol.protocol_sha256,
            "result_id": result.result_id,
            "result_sha256": result.result_sha256,
            "disposition": result.disposition.value,
            "conclusion": conclusion.value,
            "same_source_only": True,
            "independent_external_replication_claim_forbidden": True,
            "causal_or_mechanism_claim_forbidden": True,
        },
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="phonon-replay-outcome",
                context_role=MemoryContextRole.REQUIRED,
            ),
            MemoryTaskBindingSpec(
                task_key="pivot-analysis",
                context_role=MemoryContextRole.REQUIRED,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=f"phonon-independent-replay:{result.result_id}",
                sha256=result.result_sha256,
                uri=result_uri,
            ),
        ),
    )


def commit_phonon_reproduction_outcome(
    *,
    protocol: PhononIndependentReplayProtocol,
    result: PhononIndependentReplayResult,
    controller: EnduranceControllerManifest,
    plan: StructureAwareExperimentPlan,
    source_result: StructureAwareExperimentResult,
    dataframe: Any,
    dataset_file_bytes: bytes,
    result_uri: str,
    principal: str,
    producer: str,
    artifact_root: Path = REPO_ROOT,
    repository_root: Path = REPO_ROOT,
) -> PhononReplayCommitReceipt:
    _safe_relative(result_uri)
    verify_phonon_replay_artifact_files(protocol, repository_root=repository_root)
    if (
        controller.controller_id != protocol.controller_id
        or controller.manifest_sha256 != protocol.controller_manifest_sha256
    ):
        raise PhononReproductionConflict("evidence controller differs from replay protocol")
    verify_phonon_independent_replay(
        protocol=protocol,
        result=result,
        plan=plan,
        source_result=source_result,
        dataframe=dataframe,
        dataset_file_bytes=dataset_file_bytes,
        repository_root=repository_root,
    )
    snapshot = ResearchEnduranceStore().get(protocol.gate_id)
    if snapshot.report is not None or not snapshot.started_at <= result.completed_at:
        raise PhononReproductionConflict(
            "phonon replay result is outside the live endurance window"
        )
    fact = _outcome_fact(protocol, result, result_uri=result_uri)
    memory = ResearchMemoryStore().register_fact(
        fact,
        GraphCommandContext(
            idempotency_key=f"phonon-replay:{protocol.protocol_id}:outcome",
            principal=principal,
        ),
    )
    conclusion = _reproduction_conclusion(result)
    evidence_hashes = tuple(
        sorted(
            {
                protocol.dataset.file_sha256,
                str(protocol.source_plan.content_sha256),
                str(protocol.source_result.content_sha256),
                protocol.code_identity.aggregate_sha256,
                result.result_sha256,
            }
        )
    )
    evidence = EnduranceCheckpointEvidence(
        reproductions=(
            EnduranceReproductionReceipt(
                original_campaign_id=protocol.original_campaign_id,
                reproduction_campaign_id=protocol.reproduction_campaign_id,
                protocol_sha256=protocol.protocol_sha256,
                original_result_sha256=str(protocol.source_result.content_sha256),
                reproduction_result_sha256=result.result_sha256,
                conclusion=conclusion,
                evidence_sha256s=evidence_hashes,
                validated_by="harness:phonon-independent-replay",
                completed_at=result.completed_at,
            ),
        )
    )
    envelope, envelope_created = submit_controller_evidence(
        controller,
        evidence,
        producer=producer,
        submitted_at=result.completed_at,
        artifact_root=artifact_root,
    )
    assert protocol.protocol_id is not None
    assert result.result_id is not None
    return PhononReplayCommitReceipt(
        protocol_id=protocol.protocol_id,
        result_id=result.result_id,
        result_sha256=result.result_sha256,
        conclusion=conclusion,
        memory_fact_id=fact.fact_id,
        memory_fact_created=memory.created,
        envelope=envelope,
        envelope_created=envelope_created,
    )


__all__ = [
    "IndependentExtraTreesPolicy",
    "PhononIndependentReplayProtocol",
    "PhononIndependentReplayResult",
    "PhononReplayActivationReceipt",
    "PhononReplayArtifact",
    "PhononReplayCodeIdentity",
    "PhononReplayCommitReceipt",
    "PhononReplayPreflight",
    "PhononReproductionConflict",
    "PhononReproductionError",
    "activate_phonon_reproduction_campaign",
    "capture_phonon_replay_code_identity",
    "commit_phonon_reproduction_outcome",
    "execute_phonon_independent_replay",
    "prepare_phonon_independent_replay_protocol",
    "preflight_phonon_independent_replay",
    "run_phonon_independent_replay",
    "verify_phonon_independent_replay",
    "verify_phonon_replay_artifact_files",
    "verify_phonon_replay_code_identity",
]
