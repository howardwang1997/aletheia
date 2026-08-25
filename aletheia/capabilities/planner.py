"""Observation-blind exact planner for F10 experiment capabilities."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.capabilities.registry import CapabilityRegistry, CapabilityRegistrySnapshot
from aletheia.capabilities.schemas import (
    ApprovalClass,
    CapabilityClaimType,
    CapabilityEvidenceLevel,
    CapabilityLifecycle,
    ExperimentCapabilityManifest,
    SafetyClass,
    evidence_level_rank,
    safety_class_rank,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


class CapabilityPlanDisposition(str, Enum):
    SELECTED = "selected"
    UNSUPPORTED = "unsupported"


class CapabilityPlanningQuery(FrozenModel):
    schema_name: Literal["aletheia.capability_planning_query"] = (
        "aletheia.capability_planning_query"
    )
    schema_version: Literal[1] = 1
    query_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    domain: str
    scientific_question_id: str
    claim_type: CapabilityClaimType
    minimum_evidence_level: CapabilityEvidenceLevel
    available_data_modalities: tuple[str, ...] = Field(min_length=1)
    available_metadata: tuple[str, ...] = ()
    maximum_safety_class: SafetyClass
    allowed_approval_classes: tuple[ApprovalClass, ...] = Field(min_length=1)
    explicit_capability_id: str | None = None
    explicit_version: str | None = None
    allow_provisional: bool = False
    observation_access: Literal["none"] = "none"

    @model_validator(mode="after")
    def _query_sets_are_canonical(self) -> "CapabilityPlanningQuery":
        def canonical_value(value: object) -> str:
            return value.value if isinstance(value, Enum) else str(value)

        for values, label in (
            (self.available_data_modalities, "data modalities"),
            (self.available_metadata, "metadata"),
            (self.allowed_approval_classes, "approval classes"),
        ):
            if values != tuple(sorted(set(values), key=canonical_value)):
                raise ValueError(f"capability query {label} must be unique and sorted")
        if self.explicit_version is not None and self.explicit_capability_id is None:
            raise ValueError("explicit capability version requires an explicit capability ID")
        if (
            evidence_level_rank(self.minimum_evidence_level)
            > evidence_level_rank(CapabilityEvidenceLevel.EXPLORATORY)
            and self.allow_provisional
        ):
            raise ValueError("provisional capabilities cannot satisfy confirmatory queries")
        return self

    @property
    def query_sha256(self) -> str:
        return content_sha256(self)


class CapabilityCandidateAudit(FrozenModel):
    schema_version: Literal[1] = 1
    capability_id: str
    version: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible: bool
    blockers: tuple[str, ...]
    rank: int = Field(ge=1)
    selected: bool


class CapabilityPlan(FrozenModel):
    schema_name: Literal["aletheia.capability_plan"] = "aletheia.capability_plan"
    schema_version: Literal[1] = 1
    query: CapabilityPlanningQuery
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_audits: tuple[CapabilityCandidateAudit, ...]
    disposition: CapabilityPlanDisposition
    selected_manifest: ExperimentCapabilityManifest | None = None
    reason_codes: tuple[str, ...] = Field(min_length=1)
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _selection_is_nonvacuous(self) -> "CapabilityPlan":
        selected = [item for item in self.candidate_audits if item.selected]
        if self.disposition is CapabilityPlanDisposition.SELECTED:
            if len(selected) != 1 or self.selected_manifest is None:
                raise ValueError("selected capability plan requires one exact manifest")
            if selected[0].manifest_sha256 != self.selected_manifest.manifest_sha256:
                raise ValueError("capability plan selected audit/manifest mismatch")
        elif selected or self.selected_manifest is not None:
            raise ValueError("unsupported capability plan cannot contain a selection")
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


def _blockers(
    manifest: ExperimentCapabilityManifest, query: CapabilityPlanningQuery
) -> tuple[str, ...]:
    blockers: list[str] = []
    if manifest.lifecycle is CapabilityLifecycle.RETIRED:
        blockers.append("capability_retired")
    if manifest.lifecycle is CapabilityLifecycle.PROVISIONAL and not query.allow_provisional:
        blockers.append("capability_not_registered")
    if manifest.domain != query.domain:
        blockers.append("domain_mismatch")
    if query.scientific_question_id not in manifest.scientific_question_ids:
        blockers.append("scientific_question_unsupported")
    if query.claim_type not in manifest.claim_types_supported:
        blockers.append("claim_type_unsupported")
    if evidence_level_rank(manifest.maximum_evidence_level) < evidence_level_rank(
        query.minimum_evidence_level
    ):
        blockers.append("evidence_level_insufficient")
    if not set(manifest.accepted_data_modalities).issubset(query.available_data_modalities):
        for modality in sorted(
            set(manifest.accepted_data_modalities) - set(query.available_data_modalities)
        ):
            blockers.append(f"data_modality_missing:{modality}")
    if not set(manifest.required_metadata).issubset(query.available_metadata):
        for metadata in sorted(set(manifest.required_metadata) - set(query.available_metadata)):
            blockers.append(f"required_metadata_missing:{metadata}")
    if safety_class_rank(manifest.safety_class) > safety_class_rank(query.maximum_safety_class):
        blockers.append("safety_class_exceeds_authority")
    if manifest.approval_class not in query.allowed_approval_classes:
        blockers.append(f"approval_not_authorized:{manifest.approval_class.value}")
    return tuple(blockers)


def plan_capability(
    *, snapshot: CapabilityRegistrySnapshot, query: CapabilityPlanningQuery
) -> CapabilityPlan:
    registry = CapabilityRegistry(snapshot)
    if query.explicit_capability_id is not None:
        chain = [
            item
            for item in snapshot.manifests
            if item.capability_id == query.explicit_capability_id
            and (query.explicit_version is None or item.version == query.explicit_version)
        ]
        candidates = tuple(chain[-1:])
        if not candidates:
            return CapabilityPlan(
                query=query,
                registry_snapshot_sha256=snapshot.snapshot_sha256,
                candidate_audits=(),
                disposition=CapabilityPlanDisposition.UNSUPPORTED,
                reason_codes=("explicit_capability_not_found",),
            )
    else:
        candidates = registry.latest_manifests(include_provisional=query.allow_provisional)
    rows = [(manifest, _blockers(manifest, query)) for manifest in candidates]
    ordered = sorted(
        rows,
        key=lambda item: (
            bool(item[1]),
            -evidence_level_rank(item[0].maximum_evidence_level),
            item[0].resources.estimated_cost_usd,
            item[0].resources.estimated_wall_time_seconds,
            item[0].capability_id,
            tuple(-part for part in item[0].semantic_version),
        ),
    )
    eligible = [item for item in ordered if not item[1]]
    winner = eligible[0][0].manifest_sha256 if eligible else None
    audits = tuple(
        CapabilityCandidateAudit(
            capability_id=manifest.capability_id,
            version=manifest.version,
            manifest_sha256=manifest.manifest_sha256,
            eligible=not blockers,
            blockers=blockers,
            rank=index,
            selected=manifest.manifest_sha256 == winner,
        )
        for index, (manifest, blockers) in enumerate(ordered, start=1)
    )
    if winner is None:
        return CapabilityPlan(
            query=query,
            registry_snapshot_sha256=snapshot.snapshot_sha256,
            candidate_audits=audits,
            disposition=CapabilityPlanDisposition.UNSUPPORTED,
            reason_codes=("no_exact_capability_satisfies_query",),
        )
    manifest = next(item for item, blockers in ordered if not blockers)
    return CapabilityPlan(
        query=query,
        registry_snapshot_sha256=snapshot.snapshot_sha256,
        candidate_audits=audits,
        disposition=CapabilityPlanDisposition.SELECTED,
        selected_manifest=manifest,
        reason_codes=(
            "exact_provisional_capability_selected"
            if manifest.lifecycle is CapabilityLifecycle.PROVISIONAL
            else "exact_registered_capability_selected",
        ),
    )


__all__ = [
    "CapabilityCandidateAudit",
    "CapabilityPlan",
    "CapabilityPlanDisposition",
    "CapabilityPlanningQuery",
    "plan_capability",
]
