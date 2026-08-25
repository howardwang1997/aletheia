"""Append-only, resumable F11-S7 research endurance gate.

The store treats PostgreSQL as the durable clock and checkpoint ledger.  A process can disappear
between checkpoints and a new process can continue from the unique parent-hashed tail.  Explicit
clock injection is accepted only for the permanently non-scientific accelerated evidence class.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.epistemics.schemas import ResearchQuestion
from aletheia.jobs.fault_injection import validate_fault_campaign_report
from aletheia.jobs.fault_schemas import (
    FaultBoundary,
    FaultCampaignDisposition,
    FaultCampaignReport,
    FaultInjectionOutcome,
    FaultScenarioDisposition,
)
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
    scientific_command_id,
)
from aletheia.jobs.persistence import (
    ExternalActionReceiptRecord,
    FaultInjectionCampaignRecord,
    OneTimeExternalActionRecord,
    ScientificCommandRecord,
)
from aletheia.memory.ledger import BudgetEvent
from aletheia.programs.endurance_schemas import (
    REAL_72H_SECONDS,
    EnduranceBudgetState,
    EnduranceCampaignStatus,
    EnduranceCheckpoint,
    EnduranceCheckpointEvidence,
    EnduranceCheckpointSnapshot,
    EnduranceCommandContext,
    EnduranceEfficiencyReceipt,
    EnduranceEvidenceClass,
    EnduranceGateAudit,
    EnduranceGateDisposition,
    EnduranceGateManifest,
    EnduranceGateReport,
    EnduranceGateSnapshot,
    EnduranceInterruptionKind,
    EnduranceInterruptionReceipt,
    EnduranceLedgerObservation,
    EnduranceMutationReceipt,
    EndurancePortfolioReport,
    EnduranceReproductionReceipt,
    EnduranceStructuralPivotReceipt,
)
from aletheia.programs.graph import ProgramGraphStore
from aletheia.programs.memory_schemas import (
    MemoryFactKind,
    MemorySourceRef,
    MemoryTaskBindingSpec,
    ResearchMemoryFactSpec,
)
from aletheia.programs.persistence import (
    ResearchCampaignRunRecord,
    ResearchEnduranceCheckpointRecord,
    ResearchEnduranceGateRecord,
    ResearchEnduranceReportRecord,
    ResearchGraphNodeRecord,
    ResearchGraphTransitionRecord,
    ResearchMemoryFactRecord,
    ResearchMemoryTaskBindingRecord,
    ResearchPortfolioEpochRecord,
)
from aletheia.programs.portfolio import ResearchPortfolioStore
from aletheia.programs.schemas import GraphNodeState, GraphNodeType, QuestGraphSnapshot
from aletheia.reproducibility.manifest import content_sha256

_GATE_ID = re.compile(r"^edg_[0-9a-f]{32}$")
_QUEST_ID = re.compile(r"^qst_[0-9a-f]{32}$")


class ResearchEnduranceError(RuntimeError):
    """Base error for endurance contracts and persistence."""


class ResearchEnduranceConflict(ResearchEnduranceError):
    """An identity, active window, or checkpoint tail conflicts."""


class ResearchEnduranceNotFound(ResearchEnduranceError):
    """A gate or source receipt does not exist."""


class ResearchEnduranceInvariantError(ResearchEnduranceError):
    """Persisted endurance evidence no longer reconstructs exactly."""


class ResearchEnduranceSourceError(ResearchEnduranceError):
    """A claimed scientific/fault source cannot support the gate evidence."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _command_key(context: EnduranceCommandContext, operation: str) -> str:
    return "edk_" + content_sha256(
        {
            "schema": "aletheia.research_endurance_command_key.v1",
            "operation": operation,
            "client_idempotency_key": context.idempotency_key,
        }
    )[:32]


def _source_key(context: EnduranceCommandContext) -> str | None:
    if context.source_event_key is None:
        return None
    return "eds_" + content_sha256(
        {
            "schema": "aletheia.research_endurance_source_key.v1",
            "source_event_key": context.source_event_key,
        }
    )[:32]


def _command(
    *,
    operation: str,
    gate_id: str,
    payload: dict[str, Any],
    context: EnduranceCommandContext,
    event_type: str,
) -> ScientificCommandSpec:
    return ScientificCommandSpec(
        run_id=None,
        command_type=ScientificCommandType.RESEARCH_ENDURANCE_MUTATION.value,
        aggregate_type="research_endurance",
        aggregate_id=gate_id,
        idempotency_key=_command_key(context, operation),
        source_event_key=_source_key(context),
        input={"operation": operation, "gate_id": gate_id, **payload},
        principal=context.principal,
        event_type=event_type,
    )


def _budget_manifest(graph: QuestGraphSnapshot) -> str:
    return content_sha256(
        [
            {
                "allocation_id": item.allocation_id,
                "scope_node_id": item.scope_node_id,
                "parent_allocation_id": item.parent_allocation_id,
                "kind": item.kind.value,
                "cap_microunits": item.cap_microunits,
                "policy_sha256": item.policy_sha256,
            }
            for item in graph.budget_allocations
        ]
    )


def _data_role_manifest(graph: QuestGraphSnapshot) -> str:
    return content_sha256(
        [
            {
                "allocation_id": item.allocation_id,
                "scope_node_id": item.scope_node_id,
                "data_asset_id": item.data_asset_id,
                "role": item.role.value,
                "exclusive": item.exclusive,
                "data_asset_scope_sha256": item.data_asset_scope_sha256,
                "policy_sha256": item.policy_sha256,
            }
            for item in graph.data_allocations
        ]
    )


def _graph_sources(graph: QuestGraphSnapshot) -> tuple[Any, tuple[str, ...], tuple[str, ...]]:
    quest = next(
        (
            item
            for item in graph.nodes
            if item.node_id == graph.quest_id and item.node_type is GraphNodeType.QUEST
        ),
        None,
    )
    if quest is None or quest.state is not GraphNodeState.ACTIVE:
        raise ResearchEnduranceSourceError("endurance gate requires one active Quest")
    questions = tuple(
        sorted(
            item.external_id
            for item in graph.external_bindings
            if item.binding_type == "research_question"
        )
    )
    campaigns = tuple(
        sorted(
            item.node_id
            for item in graph.nodes
            if item.node_type is GraphNodeType.CAMPAIGN
        )
    )
    if len(questions) < 2:
        raise ResearchEnduranceSourceError("endurance gate requires at least two frozen questions")
    if len(campaigns) < 3:
        raise ResearchEnduranceSourceError("endurance gate requires at least three campaigns")
    if not graph.budget_allocations:
        raise ResearchEnduranceSourceError("endurance gate requires a frozen budget allocation")
    return quest, questions, campaigns


def prepare_endurance_gate_manifest(
    *,
    gate_key: str,
    quest_id: str,
    evidence_class: EnduranceEvidenceClass,
    required_duration_seconds: int,
    checkpoint_interval_seconds: int,
    maximum_checkpoint_gap_seconds: int,
    prerequisite_fault_campaign_id: str,
    harness_code_sha256: str,
    environment_manifest_sha256: str,
    minimum_efficiency_improvement_ppm: int = 100_000,
) -> EnduranceGateManifest:
    """Seal current Quest, budget/data, and passing fault-prerequisite identities."""

    graph = ProgramGraphStore().get_quest(quest_id)
    quest, questions, campaigns = _graph_sources(graph)
    with session_scope() as session:
        for question_sha256 in questions:
            from aletheia.epistemics.persistence import EpistemicResearchQuestionRecord

            row = session.get(EpistemicResearchQuestionRecord, question_sha256)
            if row is None:
                raise ResearchEnduranceSourceError(
                    f"frozen question is missing: {question_sha256}"
                )
            try:
                question = ResearchQuestion.model_validate(row.payload_json)
            except Exception as exc:
                raise ResearchEnduranceSourceError(
                    f"frozen question payload is invalid: {question_sha256}"
                ) from exc
            if question.question_sha256 != question_sha256:
                raise ResearchEnduranceSourceError(
                    f"frozen question content changed: {question_sha256}"
                )
        fault_row = session.get(FaultInjectionCampaignRecord, prerequisite_fault_campaign_id)
        if fault_row is None:
            raise ResearchEnduranceSourceError(
                f"fault prerequisite is missing: {prerequisite_fault_campaign_id}"
            )
        latest_fault_id = session.scalar(
            select(FaultInjectionCampaignRecord.campaign_id)
            .where(FaultInjectionCampaignRecord.quest_id == quest_id)
            .order_by(
                FaultInjectionCampaignRecord.completed_at.desc(),
                FaultInjectionCampaignRecord.campaign_id.desc(),
            )
            .limit(1)
        )
        if latest_fault_id != prerequisite_fault_campaign_id:
            raise ResearchEnduranceSourceError(
                "fault prerequisite must be the latest Quest-scoped campaign"
            )
        if (
            fault_row.quest_id != quest_id
            or fault_row.disposition != FaultCampaignDisposition.PASSED.value
            or any(
                getattr(fault_row, field) != 0
                for field in (
                    "scientific_state_loss_count",
                    "duplicate_scientific_state_count",
                    "duplicate_budget_charge_count",
                    "duplicate_outward_authorization_count",
                    "unresolved_ambiguity_without_block_count",
                    "event_state_mismatch_count",
                )
            )
        ):
            raise ResearchEnduranceSourceError(
                "fault prerequisite is not a zero-loss passing Quest campaign"
            )
        report_sha256 = fault_row.report_sha256
    return EnduranceGateManifest(
        gate_key=gate_key,
        quest_id=quest_id,
        evidence_class=evidence_class,
        required_duration_seconds=required_duration_seconds,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
        maximum_checkpoint_gap_seconds=maximum_checkpoint_gap_seconds,
        frozen_quest_spec_sha256=quest.spec_sha256,
        initial_graph_sha256=graph.graph_sha256,
        frozen_question_sha256s=questions,
        initial_campaign_ids=campaigns,
        frozen_budget_manifest_sha256=_budget_manifest(graph),
        frozen_data_role_manifest_sha256=_data_role_manifest(graph),
        prerequisite_fault_campaign_id=prerequisite_fault_campaign_id,
        prerequisite_fault_report_sha256=report_sha256,
        harness_code_sha256=harness_code_sha256,
        environment_manifest_sha256=environment_manifest_sha256,
        minimum_efficiency_improvement_ppm=minimum_efficiency_improvement_ppm,
    )


def _checkpoint_chain_sha256(
    manifest: EnduranceGateManifest,
    checkpoints: tuple[EnduranceCheckpoint, ...],
) -> str:
    return content_sha256(
        {
            "schema": "aletheia.research_endurance_checkpoint_chain.v1",
            "manifest_sha256": manifest.manifest_sha256,
            "checkpoint_sha256s": tuple(item.checkpoint_sha256 for item in checkpoints),
        }
    )


def _all_evidence(
    checkpoints: tuple[EnduranceCheckpoint, ...],
) -> tuple[
    tuple[EnduranceReproductionReceipt, ...],
    tuple[EnduranceInterruptionReceipt, ...],
    tuple[EnduranceStructuralPivotReceipt, ...],
]:
    reproductions = tuple(item for checkpoint in checkpoints for item in checkpoint.evidence.reproductions)
    interruptions = tuple(item for checkpoint in checkpoints for item in checkpoint.evidence.interruptions)
    pivots = tuple(item for checkpoint in checkpoints for item in checkpoint.evidence.structural_pivots)
    for label, items in (
        ("reproduction", reproductions),
        ("interruption", interruptions),
        ("pivot", pivots),
    ):
        ids = [item.receipt_id for item in items]
        if len(ids) != len(set(ids)):
            raise ResearchEnduranceInvariantError(
                f"endurance checkpoint chain repeats a {label} receipt"
            )
    return reproductions, interruptions, pivots


def _campaign_statuses(graph: QuestGraphSnapshot) -> tuple[EnduranceCampaignStatus, ...]:
    transitions = {item.transition_id: item for item in graph.transitions}
    latest_by_node = {
        item.node_id: item
        for item in sorted(graph.transitions, key=lambda value: (value.node_id, value.to_version))
    }
    statuses: list[EnduranceCampaignStatus] = []
    for node in graph.nodes:
        if node.node_type is not GraphNodeType.CAMPAIGN:
            continue
        latest = latest_by_node.get(node.node_id)
        if (
            latest is None
            or latest.transition_id not in transitions
            or latest.to_state is not node.state
            or latest.to_version != node.state_version
            or not latest.reason.strip()
        ):
            raise ResearchEnduranceInvariantError(
                f"campaign lacks an auditable current-state reason: {node.node_id}"
            )
        statuses.append(
            EnduranceCampaignStatus(
                campaign_id=node.node_id,
                state=node.state,
                state_version=node.state_version,
                latest_transition_id=latest.transition_id,
                reason=latest.reason,
            )
        )
    return tuple(sorted(statuses, key=lambda item: item.campaign_id))


def evaluate_endurance_gate(
    *,
    manifest: EnduranceGateManifest,
    started_at: datetime,
    completed_at: datetime,
    checkpoints: tuple[EnduranceCheckpoint, ...],
    final_observation: EnduranceLedgerObservation,
    final_graph: QuestGraphSnapshot,
    efficiency: EnduranceEfficiencyReceipt | None,
) -> EnduranceGateReport:
    """Independently derive the terminal report from the immutable ledger projection."""

    started_at = _aware(started_at, "endurance start")
    completed_at = _aware(completed_at, "endurance completion")
    if completed_at < started_at:
        raise ResearchEnduranceSourceError("endurance completion predates its start")
    if final_observation.observed_at != completed_at:
        raise ResearchEnduranceInvariantError(
            "final endurance observation must use the completion timestamp"
        )
    if (
        final_observation.graph_sha256 != final_graph.graph_sha256
        or final_graph.quest_id != manifest.quest_id
    ):
        raise ResearchEnduranceInvariantError(
            "final endurance observation is rebound from its graph projection"
        )
    ordered = tuple(sorted(checkpoints, key=lambda item: item.sequence))
    parent = manifest.manifest_sha256
    for sequence, checkpoint in enumerate(ordered, start=1):
        if (
            checkpoint.gate_id != manifest.gate_id
            or checkpoint.sequence != sequence
            or checkpoint.parent_sha256 != parent
            or checkpoint.observation.observed_at < started_at
            or checkpoint.observation.observed_at > completed_at
        ):
            raise ResearchEnduranceInvariantError(
                "endurance checkpoint chain is non-contiguous or outside its window"
            )
        assert checkpoint.checkpoint_sha256 is not None
        parent = checkpoint.checkpoint_sha256
    reproductions, interruptions, pivots = _all_evidence(ordered)
    process_kills = tuple(
        item for item in interruptions if item.kind is EnduranceInterruptionKind.PROCESS_KILL
    )
    provider_interruptions = tuple(
        item
        for item in interruptions
        if item.kind is EnduranceInterruptionKind.PROVIDER_TRANSPORT
    )
    points = (started_at, *(item.observation.observed_at for item in ordered), completed_at)
    gaps = tuple(
        math.ceil((right - left).total_seconds())
        for left, right in zip(points, points[1:])
    )
    maximum_gap = max(gaps, default=0)
    elapsed = int((completed_at - started_at).total_seconds())
    all_observations = tuple(item.observation for item in ordered) + (final_observation,)
    blockers: list[str] = []
    if elapsed < manifest.required_duration_seconds:
        blockers.append(
            f"duration:minimum_not_met:{elapsed}/{manifest.required_duration_seconds}"
        )
    if not ordered:
        blockers.append("checkpoints:none")
    if maximum_gap > manifest.maximum_checkpoint_gap_seconds:
        blockers.append(
            "checkpoints:maximum_gap_exceeded:"
            f"{maximum_gap}/{manifest.maximum_checkpoint_gap_seconds}"
        )
    if any(not item.core_zero for item in all_observations):
        blockers.append("integrity:nonzero_core_invariant")
    if final_observation.quest_spec_sha256 != manifest.frozen_quest_spec_sha256:
        blockers.append("integrity:quest_direction_changed")
    if final_observation.question_sha256s != manifest.frozen_question_sha256s:
        blockers.append("integrity:frozen_questions_changed")
    if not set(manifest.initial_campaign_ids).issubset(final_observation.campaign_ids):
        blockers.append("integrity:initial_campaign_lost")
    if len(final_observation.campaign_ids) < 3:
        blockers.append("campaigns:minimum_not_met")
    if len(final_observation.negative_result_fact_ids) < manifest.minimum_negative_results:
        blockers.append(
            "negative_results:minimum_not_met:"
            f"{len(final_observation.negative_result_fact_ids)}/"
            f"{manifest.minimum_negative_results}"
        )
    if len(reproductions) < manifest.minimum_reproductions:
        blockers.append(
            f"reproductions:minimum_not_met:{len(reproductions)}/"
            f"{manifest.minimum_reproductions}"
        )
    if len(process_kills) < manifest.minimum_process_kills:
        blockers.append(
            f"process_kills:minimum_not_met:{len(process_kills)}/"
            f"{manifest.minimum_process_kills}"
        )
    if len(provider_interruptions) < manifest.minimum_provider_interruptions:
        blockers.append(
            "provider_interruptions:minimum_not_met:"
            f"{len(provider_interruptions)}/{manifest.minimum_provider_interruptions}"
        )
    if len(pivots) < manifest.minimum_structural_pivots:
        blockers.append(
            f"structural_pivots:minimum_not_met:{len(pivots)}/"
            f"{manifest.minimum_structural_pivots}"
        )
    if len(final_observation.portfolio_epoch_ids) < manifest.minimum_portfolio_epochs:
        blockers.append(
            "portfolio_epochs:minimum_not_met:"
            f"{len(final_observation.portfolio_epoch_ids)}/"
            f"{manifest.minimum_portfolio_epochs}"
        )
    if efficiency is None:
        blockers.append("efficiency:receipt_missing")
    elif efficiency.improvement_ppm < manifest.minimum_efficiency_improvement_ppm:
        blockers.append(
            "efficiency:improvement_below_floor:"
            f"{efficiency.improvement_ppm}/"
            f"{manifest.minimum_efficiency_improvement_ppm}"
        )
    campaign_statuses = _campaign_statuses(final_graph)
    portfolio = EndurancePortfolioReport(
        quest_id=manifest.quest_id,
        graph_sha256=final_graph.graph_sha256,
        question_sha256s=final_observation.question_sha256s,
        campaigns=campaign_statuses,
        negative_result_fact_ids=final_observation.negative_result_fact_ids,
        reproduction_receipt_ids=tuple(sorted(item.receipt_id for item in reproductions)),
        interruption_receipt_ids=tuple(sorted(item.receipt_id for item in interruptions)),
        structural_pivot_receipt_ids=tuple(sorted(item.receipt_id for item in pivots)),
        portfolio_epoch_ids=final_observation.portfolio_epoch_ids,
        budget_state=final_observation.budget_state,
    )
    canonical = tuple(sorted(set(blockers)))
    disposition = (
        EnduranceGateDisposition.FAILED
        if any(item.startswith("integrity:") for item in canonical)
        else EnduranceGateDisposition.PASSED
        if not canonical
        else EnduranceGateDisposition.BLOCKED
    )
    real_pass = (
        disposition is EnduranceGateDisposition.PASSED
        and manifest.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H
        and elapsed >= REAL_72H_SECONDS
    )
    return EnduranceGateReport(
        manifest=manifest,
        started_at=started_at,
        completed_at=completed_at,
        elapsed_seconds=elapsed,
        checkpoint_count=len(ordered),
        maximum_observed_gap_seconds=maximum_gap,
        checkpoint_chain_sha256=_checkpoint_chain_sha256(manifest, ordered),
        negative_result_count=len(final_observation.negative_result_fact_ids),
        reproduction_count=len(reproductions),
        process_kill_count=len(process_kills),
        provider_interruption_count=len(provider_interruptions),
        structural_pivot_count=len(pivots),
        portfolio_epoch_count=len(final_observation.portfolio_epoch_ids),
        efficiency=efficiency,
        final_portfolio=portfolio,
        disposition=disposition,
        blockers=canonical,
        real_72h_passed=real_pass,
        eligible_for_f11_scientific_exit_review=real_pass,
        autonomous_allocation_enabled=False,
    )


class ResearchEnduranceStore:
    """Durable start/checkpoint/finalize boundary for one frozen Quest."""

    def __init__(self) -> None:
        self._commands = ScientificTransitionStore()

    @staticmethod
    def _clock(
        session: Session,
        evidence_class: EnduranceEvidenceClass,
        supplied: datetime | None,
    ) -> datetime:
        if supplied is not None:
            if evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
                raise ResearchEnduranceConflict(
                    "real-time endurance evidence rejects caller-supplied clocks"
                )
            return _aware(supplied, "accelerated endurance clock")
        observed = session.scalar(select(func.clock_timestamp()))
        if observed is None:  # pragma: no cover - PostgreSQL always supplies this
            raise ResearchEnduranceInvariantError(
                "database did not provide an endurance wall-clock timestamp"
            )
        return _aware(observed, "database endurance clock")

    @staticmethod
    def _command_created_at(session: Session, command_id: str) -> datetime:
        value = session.scalar(
            select(ScientificCommandRecord.created_at).where(
                ScientificCommandRecord.command_id == command_id
            )
        )
        if value is None:
            raise ResearchEnduranceInvariantError("endurance applying command disappeared")
        return value

    @staticmethod
    def _verify_command(
        session: Session,
        *,
        command_id: str,
        gate_id: str,
        operation: str,
        principal: str,
        object_id: str,
    ) -> ScientificCommandReceipt:
        row = session.get(ScientificCommandRecord, command_id)
        if row is None:
            raise ResearchEnduranceInvariantError(
                f"endurance command is missing: {command_id}"
            )
        try:
            ScientificTransitionStore._verify_event(session, row)
            receipt = ScientificTransitionStore._receipt(row, created=False)
        except Exception as exc:
            raise ResearchEnduranceInvariantError(
                f"endurance command receipt is invalid: {command_id}"
            ) from exc
        if (
            row.command_type != ScientificCommandType.RESEARCH_ENDURANCE_MUTATION.value
            or row.aggregate_type != "research_endurance"
            or row.aggregate_id != gate_id
            or row.principal != principal
            or row.input_json.get("operation") != operation
            or row.input_json.get("gate_id") != gate_id
            or receipt.result.get("object_id") != object_id
        ):
            raise ResearchEnduranceInvariantError(
                f"endurance command was rebound: {command_id}"
            )
        return receipt

    @staticmethod
    def _validate_manifest_locked(
        session: Session,
        manifest: EnduranceGateManifest,
        graph: QuestGraphSnapshot,
    ) -> None:
        quest = session.scalar(
            select(ResearchGraphNodeRecord)
            .where(ResearchGraphNodeRecord.node_id == manifest.quest_id)
            .with_for_update()
        )
        if (
            quest is None
            or quest.node_type != GraphNodeType.QUEST.value
            or quest.current_state != GraphNodeState.ACTIVE.value
            or quest.spec_sha256 != manifest.frozen_quest_spec_sha256
        ):
            raise ResearchEnduranceSourceError("frozen endurance Quest changed before start")
        graph_quest, questions, campaigns = _graph_sources(graph)
        if (
            graph_quest.spec_sha256 != manifest.frozen_quest_spec_sha256
            or graph.graph_sha256 != manifest.initial_graph_sha256
            or questions != manifest.frozen_question_sha256s
            or campaigns != manifest.initial_campaign_ids
            or _budget_manifest(graph) != manifest.frozen_budget_manifest_sha256
            or _data_role_manifest(graph) != manifest.frozen_data_role_manifest_sha256
        ):
            raise ResearchEnduranceSourceError("endurance manifest no longer matches the Quest")
        fault = session.get(
            FaultInjectionCampaignRecord,
            manifest.prerequisite_fault_campaign_id,
        )
        latest_fault_id = session.scalar(
            select(FaultInjectionCampaignRecord.campaign_id)
            .where(FaultInjectionCampaignRecord.quest_id == manifest.quest_id)
            .order_by(
                FaultInjectionCampaignRecord.completed_at.desc(),
                FaultInjectionCampaignRecord.campaign_id.desc(),
            )
            .limit(1)
        )
        if (
            fault is None
            or latest_fault_id != fault.campaign_id
            or fault.quest_id != manifest.quest_id
            or fault.report_sha256 != manifest.prerequisite_fault_report_sha256
            or fault.disposition != FaultCampaignDisposition.PASSED.value
        ):
            raise ResearchEnduranceSourceError("endurance fault prerequisite changed")

    @staticmethod
    def _checkpoint_rows(
        session: Session,
        gate_id: str,
        *,
        lock_tail: bool = False,
    ) -> tuple[ResearchEnduranceCheckpointRecord, ...]:
        statement = (
            select(ResearchEnduranceCheckpointRecord)
            .where(ResearchEnduranceCheckpointRecord.gate_id == gate_id)
            .order_by(ResearchEnduranceCheckpointRecord.sequence)
        )
        if lock_tail:
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    @staticmethod
    def _parse_checkpoint(row: ResearchEnduranceCheckpointRecord) -> EnduranceCheckpoint:
        try:
            checkpoint = EnduranceCheckpoint.model_validate(row.checkpoint_json)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ResearchEnduranceInvariantError(
                f"endurance checkpoint payload is invalid: {row.checkpoint_id}"
            ) from exc
        if (
            checkpoint.checkpoint_id != row.checkpoint_id
            or checkpoint.gate_id != row.gate_id
            or checkpoint.sequence != row.sequence
            or checkpoint.parent_sha256 != row.parent_sha256
            or checkpoint.checkpoint_sha256 != row.checkpoint_sha256
            or checkpoint.observation.observation_sha256 != row.observation_sha256
            or checkpoint.observation.observed_at != row.observed_at
            or len(checkpoint.evidence.reproductions) != row.reproduction_count
            or sum(
                item.kind is EnduranceInterruptionKind.PROCESS_KILL
                for item in checkpoint.evidence.interruptions
            )
            != row.process_kill_count
            or sum(
                item.kind is EnduranceInterruptionKind.PROVIDER_TRANSPORT
                for item in checkpoint.evidence.interruptions
            )
            != row.provider_interruption_count
            or len(checkpoint.evidence.structural_pivots) != row.structural_pivot_count
        ):
            raise ResearchEnduranceInvariantError(
                f"endurance checkpoint bindings changed: {row.checkpoint_id}"
            )
        return checkpoint

    @staticmethod
    def _negative_result_ids(
        session: Session,
        *,
        quest_id: str,
        started_at: datetime,
        observed_at: datetime,
    ) -> tuple[str, ...]:
        rows = session.scalars(
            select(ResearchMemoryFactRecord)
            .where(
                ResearchMemoryFactRecord.quest_id == quest_id,
                ResearchMemoryFactRecord.kind == MemoryFactKind.NEGATIVE_RESULT.value,
                ResearchMemoryFactRecord.created_at >= started_at,
                ResearchMemoryFactRecord.created_at <= observed_at,
            )
            .order_by(ResearchMemoryFactRecord.fact_id)
        ).all()
        verified: list[str] = []
        for row in rows:
            binding_rows = session.scalars(
                select(ResearchMemoryTaskBindingRecord)
                .where(ResearchMemoryTaskBindingRecord.fact_id == row.fact_id)
                .order_by(ResearchMemoryTaskBindingRecord.task_key)
            ).all()
            try:
                spec = ResearchMemoryFactSpec(
                    scope_node_id=row.scope_node_id,
                    kind=row.kind,
                    statement=row.statement,
                    detail=row.detail_json,
                    task_bindings=tuple(
                        MemoryTaskBindingSpec(
                            task_key=item.task_key,
                            context_role=item.context_role,
                        )
                        for item in binding_rows
                    ),
                    sources=tuple(MemorySourceRef.model_validate(item) for item in row.source_refs_json),
                )
            except Exception as exc:
                raise ResearchEnduranceInvariantError(
                    f"negative-result fact payload is invalid: {row.fact_id}"
                ) from exc
            command = session.get(ScientificCommandRecord, row.command_id)
            if (
                spec.fact_id != row.fact_id
                or spec.fact_sha256 != row.fact_sha256
                or command is None
                or command.command_type
                != ScientificCommandType.RESEARCH_MEMORY_MUTATION.value
                or command.aggregate_id != row.fact_id
                or command.result_json is None
                or command.result_json.get("object_id") != row.fact_id
            ):
                raise ResearchEnduranceInvariantError(
                    f"negative-result fact cannot be reconstructed: {row.fact_id}"
                )
            verified.append(row.fact_id)
        return tuple(verified)

    @staticmethod
    def _budget_state(
        session: Session,
        graph: QuestGraphSnapshot,
    ) -> tuple[EnduranceBudgetState, ...]:
        allocation_ids = [item.allocation_id for item in graph.budget_allocations]
        spent = {item: 0 for item in allocation_ids}
        if allocation_ids:
            events = session.scalars(
                select(BudgetEvent)
                .where(BudgetEvent.research_budget_allocation_id.in_(allocation_ids))
                .order_by(BudgetEvent.id)
            ).all()
            seen_charges: set[tuple[str, str, int, int]] = set()
            for event in events:
                allocation_id = event.research_budget_allocation_id
                if allocation_id is None:  # pragma: no cover - filtered above
                    continue
                amount = int(round(float(event.amount) * 1_000_000))
                if amount < 0:
                    raise ResearchEnduranceInvariantError(
                        f"allocated budget has a negative charge: {event.id}"
                    )
                identity = (allocation_id, event.run_id, amount, int(event.id))
                if identity in seen_charges:  # primary keys make this defensive only
                    raise ResearchEnduranceInvariantError(
                        f"allocated budget repeats a charge identity: {event.id}"
                    )
                seen_charges.add(identity)
                spent[allocation_id] += amount
        states: list[EnduranceBudgetState] = []
        for allocation in graph.budget_allocations:
            observed = spent[allocation.allocation_id]
            if observed > allocation.cap_microunits:
                raise ResearchEnduranceInvariantError(
                    f"endurance budget exceeds its cap: {allocation.allocation_id}"
                )
            states.append(
                EnduranceBudgetState(
                    allocation_id=allocation.allocation_id,
                    scope_node_id=allocation.scope_node_id,
                    kind=allocation.kind,
                    cap_microunits=allocation.cap_microunits,
                    spent_microunits=observed,
                    available_microunits=allocation.cap_microunits - observed,
                )
            )
        return tuple(sorted(states, key=lambda item: item.allocation_id))

    @classmethod
    def _observe(
        cls,
        session: Session,
        *,
        manifest: EnduranceGateManifest,
        started_at: datetime,
        observed_at: datetime,
        graph: QuestGraphSnapshot,
    ) -> EnduranceLedgerObservation:
        quest, questions, campaigns = _graph_sources(graph)
        negative_ids = cls._negative_result_ids(
            session,
            quest_id=manifest.quest_id,
            started_at=started_at,
            observed_at=observed_at,
        )
        epoch_rows = session.scalars(
            select(ResearchPortfolioEpochRecord)
            .where(
                ResearchPortfolioEpochRecord.quest_id == manifest.quest_id,
                ResearchPortfolioEpochRecord.evaluated_at >= started_at,
                ResearchPortfolioEpochRecord.evaluated_at <= observed_at,
            )
            .order_by(ResearchPortfolioEpochRecord.epoch_id)
        ).all()
        for row in epoch_rows:
            # Replaying the frozen selector detects payload, score, and command tampering.
            ResearchPortfolioStore().get_epoch(row.epoch_id)
        run_ids = tuple(
            session.scalars(
                select(ResearchCampaignRunRecord.run_id).where(
                    ResearchCampaignRunRecord.quest_id == manifest.quest_id
                )
            ).all()
        )
        actions = (
            tuple(
                session.scalars(
                    select(OneTimeExternalActionRecord).where(
                        OneTimeExternalActionRecord.run_id.in_(run_ids)
                    )
                ).all()
            )
            if run_ids
            else ()
        )
        action_ids = [item.action_id for item in actions]
        receipts = (
            tuple(
                session.scalars(
                    select(ExternalActionReceiptRecord).where(
                        ExternalActionReceiptRecord.action_id.in_(action_ids)
                    )
                ).all()
            )
            if action_ids
            else ()
        )
        lost = sum(item.status == "completed" and item.receipt_sha256 is None for item in actions)
        unresolved = sum(
            item.status == "claimed" and item.reconcile_after <= observed_at for item in actions
        )
        event_mismatch = sum(
            item.status == "completed"
            and not any(receipt.action_id == item.action_id for receipt in receipts)
            for item in actions
        )
        duplicate_actions = len(actions) - len(
            {(item.scope_key, item.provider_idempotency_key) for item in actions}
        )
        in_window_faults = session.scalars(
            select(FaultInjectionCampaignRecord).where(
                FaultInjectionCampaignRecord.quest_id == manifest.quest_id,
                FaultInjectionCampaignRecord.completed_at >= started_at,
                FaultInjectionCampaignRecord.completed_at <= observed_at,
            )
        ).all()
        # Local import avoids the jobs.fault_campaign -> programs.persistence package-init cycle.
        from aletheia.jobs.fault_campaign import FaultCampaignStore

        fault_snapshots = tuple(
            FaultCampaignStore._snapshot(session, row) for row in in_window_faults
        )
        fault_totals = {
            field: sum(int(getattr(snapshot.report, field)) for snapshot in fault_snapshots)
            for field in (
                "scientific_state_loss_count",
                "duplicate_scientific_state_count",
                "duplicate_budget_charge_count",
                "duplicate_outward_authorization_count",
                "unresolved_ambiguity_without_block_count",
                "event_state_mismatch_count",
            )
        }
        budget_changed = _budget_manifest(graph) != manifest.frozen_budget_manifest_sha256
        data_changed = _data_role_manifest(graph) != manifest.frozen_data_role_manifest_sha256
        direction_changed = quest.spec_sha256 != manifest.frozen_quest_spec_sha256
        questions_changed = questions != manifest.frozen_question_sha256s
        return EnduranceLedgerObservation(
            quest_spec_sha256=quest.spec_sha256,
            graph_sha256=graph.graph_sha256,
            question_sha256s=questions,
            campaign_ids=campaigns,
            negative_result_fact_ids=negative_ids,
            portfolio_epoch_ids=tuple(row.epoch_id for row in epoch_rows),
            budget_state=cls._budget_state(session, graph),
            one_time_action_count=len(actions),
            one_time_action_receipt_count=len(receipts),
            reconciliation_required_count=sum(
                item.status == "reconciliation_required" for item in actions
            ),
            scientific_state_loss_count=(
                lost + fault_totals["scientific_state_loss_count"]
            ),
            duplicate_scientific_state_count=fault_totals[
                "duplicate_scientific_state_count"
            ],
            duplicate_budget_charge_count=fault_totals[
                "duplicate_budget_charge_count"
            ],
            duplicate_outward_action_count=(
                duplicate_actions
                + fault_totals["duplicate_outward_authorization_count"]
            ),
            unresolved_ambiguity_without_block_count=(
                unresolved
                + fault_totals["unresolved_ambiguity_without_block_count"]
            ),
            event_state_mismatch_count=(
                event_mismatch
                + fault_totals["event_state_mismatch_count"]
                + int(budget_changed)
                + int(data_changed)
                + int(direction_changed)
                + int(questions_changed)
            ),
            observed_at=observed_at,
        )

    @staticmethod
    def _validate_reproduction(
        receipt: EnduranceReproductionReceipt,
        *,
        campaign_ids: set[str],
        started_at: datetime,
        observed_at: datetime,
    ) -> None:
        if not {
            receipt.original_campaign_id,
            receipt.reproduction_campaign_id,
        }.issubset(campaign_ids):
            raise ResearchEnduranceSourceError(
                f"reproduction left the endurance Quest: {receipt.receipt_id}"
            )
        if not started_at <= receipt.completed_at <= observed_at:
            raise ResearchEnduranceSourceError(
                f"reproduction is outside the endurance window: {receipt.receipt_id}"
            )

    @staticmethod
    def _validate_interruption(
        session: Session,
        receipt: EnduranceInterruptionReceipt,
        *,
        quest_id: str,
        started_at: datetime,
        observed_at: datetime,
    ) -> None:
        row = session.get(FaultInjectionCampaignRecord, receipt.fault_campaign_id)
        if row is None:
            raise ResearchEnduranceSourceError(
                f"interruption fault report is missing: {receipt.fault_campaign_id}"
            )
        try:
            report = validate_fault_campaign_report(
                FaultCampaignReport.model_validate(row.report_json)
            )
        except Exception as exc:
            raise ResearchEnduranceSourceError(
                f"interruption fault report is invalid: {receipt.fault_campaign_id}"
            ) from exc
        result = next(
            (item for item in report.results if item.scenario_id == receipt.scenario_id),
            None,
        )
        if (
            row.quest_id != quest_id
            or row.report_sha256 != receipt.fault_report_sha256
            or report.disposition is not FaultCampaignDisposition.PASSED
            or result is None
            or result.disposition is not FaultScenarioDisposition.PASSED
            or result.observation.completed_at != receipt.occurred_at
            or not set(receipt.recovery_evidence_sha256s).issubset(
                result.observation.evidence_sha256s
            )
            or not started_at <= receipt.occurred_at <= observed_at
        ):
            raise ResearchEnduranceSourceError(
                f"interruption receipt is not bound to passing in-window evidence: "
                f"{receipt.receipt_id}"
            )
        spec = next(item for item in report.manifest.scenarios if item.scenario_id == result.scenario_id)
        if receipt.kind is EnduranceInterruptionKind.PROCESS_KILL:
            valid = (
                spec.boundary in {FaultBoundary.API_PROCESS, FaultBoundary.WORKER_PROCESS}
                and result.observation.observed_outcome
                is FaultInjectionOutcome.PROCESS_EXIT
            )
        else:
            valid = (
                spec.boundary is FaultBoundary.PROVIDER
                and result.observation.observed_outcome
                in {FaultInjectionOutcome.UNAVAILABLE, FaultInjectionOutcome.TIMEOUT}
            )
        if not valid:
            raise ResearchEnduranceSourceError(
                f"interruption kind differs from its injected boundary: {receipt.receipt_id}"
            )

    @staticmethod
    def _validate_pivot(
        session: Session,
        receipt: EnduranceStructuralPivotReceipt,
        *,
        quest_id: str,
        started_at: datetime,
        observed_at: datetime,
    ) -> None:
        fact = session.get(ResearchMemoryFactRecord, receipt.negative_result_fact_id)
        source = session.get(ResearchGraphTransitionRecord, receipt.source_transition_id)
        successor = session.get(
            ResearchGraphTransitionRecord,
            receipt.successor_transition_id,
        )
        source_node = (
            session.get(ResearchGraphNodeRecord, receipt.source_campaign_id)
            if source is not None
            else None
        )
        successor_node = (
            session.get(ResearchGraphNodeRecord, receipt.successor_campaign_id)
            if successor is not None
            else None
        )
        if (
            fact is None
            or fact.quest_id != quest_id
            or fact.kind != MemoryFactKind.NEGATIVE_RESULT.value
            or source is None
            or source.node_id != receipt.source_campaign_id
            or source.to_state not in {
                GraphNodeState.PAUSED.value,
                GraphNodeState.STOPPED.value,
                GraphNodeState.FAILED.value,
            }
            or successor is None
            or successor.node_id != receipt.successor_campaign_id
            or successor.to_state != GraphNodeState.ACTIVE.value
            or source_node is None
            or source_node.quest_id != quest_id
            or successor_node is None
            or successor_node.quest_id != quest_id
            or not (
                started_at
                <= fact.created_at
                <= source.created_at
                <= receipt.occurred_at
                <= observed_at
            )
            or not (
                fact.created_at <= successor.created_at <= receipt.occurred_at
            )
            or receipt.assessed_by in {source.principal, successor.principal}
        ):
            raise ResearchEnduranceSourceError(
                f"structural pivot is not caused by its negative result: {receipt.receipt_id}"
            )

    @classmethod
    def _validate_new_evidence(
        cls,
        session: Session,
        evidence: EnduranceCheckpointEvidence,
        *,
        quest_id: str,
        campaign_ids: set[str],
        started_at: datetime,
        observed_at: datetime,
    ) -> None:
        for receipt in evidence.reproductions:
            cls._validate_reproduction(
                receipt,
                campaign_ids=campaign_ids,
                started_at=started_at,
                observed_at=observed_at,
            )
        for receipt in evidence.interruptions:
            cls._validate_interruption(
                session,
                receipt,
                quest_id=quest_id,
                started_at=started_at,
                observed_at=observed_at,
            )
        for receipt in evidence.structural_pivots:
            cls._validate_pivot(
                session,
                receipt,
                quest_id=quest_id,
                started_at=started_at,
                observed_at=observed_at,
            )

    @staticmethod
    def _existing_command(
        *,
        context: EnduranceCommandContext,
        operation: str,
    ) -> ScientificCommandRecord | None:
        command_id = scientific_command_id(
            ScientificCommandType.RESEARCH_ENDURANCE_MUTATION.value,
            _command_key(context, operation),
        )
        with session_scope() as session:
            return session.get(ScientificCommandRecord, command_id)

    def start(
        self,
        manifest: EnduranceGateManifest,
        context: EnduranceCommandContext,
        *,
        now: datetime | None = None,
    ) -> EnduranceMutationReceipt:
        manifest = EnduranceGateManifest.model_validate(manifest.model_dump(mode="python"))
        context = EnduranceCommandContext.model_validate(context.model_dump(mode="python"))
        assert manifest.gate_id is not None
        if now is not None and manifest.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
            raise ResearchEnduranceConflict(
                "real-time endurance evidence rejects caller-supplied clocks"
            )
        command = _command(
            operation="start",
            gate_id=manifest.gate_id,
            payload={
                "manifest_sha256": manifest.manifest_sha256,
                "manifest": manifest.model_dump(mode="json"),
            },
            context=context,
            event_type="research_endurance_started",
        )

        def apply(session: Session) -> ScientificMutation:
            # Graph mutations take this same Quest lock.  Fetching the replay projection only
            # after the lock closes the race between manifest preparation and gate start.
            session.scalar(
                select(ResearchGraphNodeRecord)
                .where(ResearchGraphNodeRecord.node_id == manifest.quest_id)
                .with_for_update()
            )
            current_graph = ProgramGraphStore().get_quest(manifest.quest_id)
            self._validate_manifest_locked(session, manifest, current_graph)
            active = session.scalar(
                select(ResearchEnduranceGateRecord)
                .where(ResearchEnduranceGateRecord.quest_id == manifest.quest_id)
                .where(
                    ~ResearchEnduranceGateRecord.gate_id.in_(
                        select(ResearchEnduranceReportRecord.gate_id)
                    )
                )
                .with_for_update()
            )
            if active is not None:
                raise ResearchEnduranceConflict(
                    f"Quest already has an unfinished endurance gate: {active.gate_id}"
                )
            if session.get(ResearchEnduranceGateRecord, manifest.gate_id) is not None:
                raise ResearchEnduranceConflict(
                    "endurance gate identity is already committed under another command"
                )
            started_at = self._clock(session, manifest.evidence_class, now)
            assert command.command_id is not None
            session.add(
                ResearchEnduranceGateRecord(
                    gate_id=manifest.gate_id,
                    quest_id=manifest.quest_id,
                    manifest_sha256=manifest.manifest_sha256,
                    manifest_json=manifest.model_dump(mode="json"),
                    evidence_class=manifest.evidence_class.value,
                    required_duration_seconds=manifest.required_duration_seconds,
                    checkpoint_interval_seconds=manifest.checkpoint_interval_seconds,
                    maximum_checkpoint_gap_seconds=manifest.maximum_checkpoint_gap_seconds,
                    frozen_quest_spec_sha256=manifest.frozen_quest_spec_sha256,
                    initial_graph_sha256=manifest.initial_graph_sha256,
                    frozen_budget_manifest_sha256=manifest.frozen_budget_manifest_sha256,
                    frozen_data_role_manifest_sha256=manifest.frozen_data_role_manifest_sha256,
                    prerequisite_fault_campaign_id=manifest.prerequisite_fault_campaign_id,
                    prerequisite_fault_report_sha256=(
                        manifest.prerequisite_fault_report_sha256
                    ),
                    started_at=started_at,
                    command_id=command.command_id,
                    started_by=context.principal,
                    created_at=self._command_created_at(session, command.command_id),
                )
            )
            return ScientificMutation(
                result={
                    "kind": "research_endurance_gate",
                    "object_id": manifest.gate_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "evidence_class": manifest.evidence_class.value,
                },
                event_projection={
                    "quest_id": manifest.quest_id,
                    "gate_id": manifest.gate_id,
                    "evidence_class": manifest.evidence_class.value,
                    "required_duration_seconds": manifest.required_duration_seconds,
                    "autonomous_allocation_enabled": False,
                },
            )

        try:
            receipt = self._commands.execute(command, apply, now=now)
        except IntegrityError as exc:
            raise ResearchEnduranceConflict(
                "endurance gate or command identity conflicts with persisted content"
            ) from exc
        return EnduranceMutationReceipt(
            object_id=manifest.gate_id,
            command_id=receipt.command_id,
            created=receipt.created,
        )

    def append_checkpoint(
        self,
        gate_id: str,
        evidence: EnduranceCheckpointEvidence,
        context: EnduranceCommandContext,
        *,
        now: datetime | None = None,
    ) -> EnduranceMutationReceipt:
        if _GATE_ID.fullmatch(gate_id) is None:
            raise ValueError("invalid endurance gate id")
        evidence = EnduranceCheckpointEvidence.model_validate(evidence.model_dump(mode="python"))
        context = EnduranceCommandContext.model_validate(context.model_dump(mode="python"))
        evidence_sha256 = content_sha256(evidence)
        existing = self._existing_command(context=context, operation="checkpoint")
        if existing is not None:
            if (
                existing.aggregate_id != gate_id
                or existing.input_json.get("evidence_sha256") != evidence_sha256
                or existing.result_json is None
            ):
                raise ResearchEnduranceConflict(
                    "checkpoint idempotency key is rebound to different evidence"
                )
            return EnduranceMutationReceipt(
                object_id=str(existing.result_json["object_id"]),
                command_id=existing.command_id,
                created=False,
            )
        with session_scope() as session:
            gate_probe = session.get(ResearchEnduranceGateRecord, gate_id)
            if gate_probe is None:
                raise ResearchEnduranceNotFound(f"endurance gate not found: {gate_id}")
            evidence_class = EnduranceEvidenceClass(gate_probe.evidence_class)
        if now is not None and evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
            raise ResearchEnduranceConflict(
                "real-time endurance evidence rejects caller-supplied clocks"
            )
        command = _command(
            operation="checkpoint",
            gate_id=gate_id,
            payload={
                "evidence_sha256": evidence_sha256,
                "evidence": evidence.model_dump(mode="json"),
            },
            context=context,
            event_type="research_endurance_checkpointed",
        )

        def apply(session: Session) -> ScientificMutation:
            gate = session.scalar(
                select(ResearchEnduranceGateRecord)
                .where(ResearchEnduranceGateRecord.gate_id == gate_id)
                .with_for_update()
            )
            if gate is None:
                raise ResearchEnduranceNotFound(f"endurance gate not found: {gate_id}")
            if session.get(ResearchEnduranceReportRecord, gate_id) is not None:
                raise ResearchEnduranceConflict("terminal endurance gate cannot accept checkpoints")
            manifest = EnduranceGateManifest.model_validate(gate.manifest_json)
            observed_at = self._clock(session, manifest.evidence_class, now)
            rows = self._checkpoint_rows(session, gate_id, lock_tail=True)
            previous = self._parse_checkpoint(rows[-1]) if rows else None
            if observed_at < gate.started_at or (
                previous is not None and observed_at <= previous.observation.observed_at
            ):
                raise ResearchEnduranceConflict(
                    "endurance checkpoint time must advance its durable window"
                )
            prior = tuple(self._parse_checkpoint(row) for row in rows)
            prior_ids = {
                item.receipt_id
                for checkpoint in prior
                for group in (
                    checkpoint.evidence.reproductions,
                    checkpoint.evidence.interruptions,
                    checkpoint.evidence.structural_pivots,
                )
                for item in group
            }
            new_ids = {
                item.receipt_id
                for group in (
                    evidence.reproductions,
                    evidence.interruptions,
                    evidence.structural_pivots,
                )
                for item in group
            }
            if prior_ids.intersection(new_ids):
                raise ResearchEnduranceConflict(
                    "endurance evidence receipt was already committed in an earlier checkpoint"
                )
            graph = ProgramGraphStore().get_quest(manifest.quest_id)
            observation = self._observe(
                session,
                manifest=manifest,
                started_at=gate.started_at,
                observed_at=observed_at,
                graph=graph,
            )
            self._validate_new_evidence(
                session,
                evidence,
                quest_id=manifest.quest_id,
                campaign_ids=set(observation.campaign_ids),
                started_at=gate.started_at,
                observed_at=observed_at,
            )
            parent_sha256 = (
                previous.checkpoint_sha256 if previous is not None else manifest.manifest_sha256
            )
            assert parent_sha256 is not None
            checkpoint = EnduranceCheckpoint(
                gate_id=gate_id,
                sequence=len(rows) + 1,
                parent_sha256=parent_sha256,
                observation=observation,
                evidence=evidence,
            )
            assert checkpoint.checkpoint_id is not None
            assert checkpoint.checkpoint_sha256 is not None
            assert observation.observation_sha256 is not None
            assert command.command_id is not None
            session.add(
                ResearchEnduranceCheckpointRecord(
                    checkpoint_id=checkpoint.checkpoint_id,
                    gate_id=gate_id,
                    sequence=checkpoint.sequence,
                    parent_sha256=checkpoint.parent_sha256,
                    observation_sha256=observation.observation_sha256,
                    checkpoint_sha256=checkpoint.checkpoint_sha256,
                    checkpoint_json=checkpoint.model_dump(mode="json"),
                    reproduction_count=len(evidence.reproductions),
                    process_kill_count=sum(
                        item.kind is EnduranceInterruptionKind.PROCESS_KILL
                        for item in evidence.interruptions
                    ),
                    provider_interruption_count=sum(
                        item.kind is EnduranceInterruptionKind.PROVIDER_TRANSPORT
                        for item in evidence.interruptions
                    ),
                    structural_pivot_count=len(evidence.structural_pivots),
                    observed_at=observed_at,
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=self._command_created_at(session, command.command_id),
                )
            )
            return ScientificMutation(
                result={
                    "kind": "research_endurance_checkpoint",
                    "object_id": checkpoint.checkpoint_id,
                    "gate_id": gate_id,
                    "sequence": checkpoint.sequence,
                    "checkpoint_sha256": checkpoint.checkpoint_sha256,
                },
                event_projection={
                    "gate_id": gate_id,
                    "quest_id": manifest.quest_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "sequence": checkpoint.sequence,
                    "checkpoint_sha256": checkpoint.checkpoint_sha256,
                    "core_zero": observation.core_zero,
                    "autonomous_allocation_enabled": False,
                },
            )

        try:
            receipt = self._commands.execute(
                command,
                apply,
                now=now if evidence_class is EnduranceEvidenceClass.ACCELERATED_ENGINEERING else None,
            )
        except IntegrityError as exc:
            raise ResearchEnduranceConflict(
                "checkpoint command or chain identity conflicts with persisted content"
            ) from exc
        return EnduranceMutationReceipt(
            object_id=str(receipt.result["object_id"]),
            command_id=receipt.command_id,
            created=receipt.created,
        )

    def finalize(
        self,
        gate_id: str,
        context: EnduranceCommandContext,
        *,
        efficiency: EnduranceEfficiencyReceipt | None,
        now: datetime | None = None,
    ) -> EnduranceMutationReceipt:
        if _GATE_ID.fullmatch(gate_id) is None:
            raise ValueError("invalid endurance gate id")
        context = EnduranceCommandContext.model_validate(context.model_dump(mode="python"))
        if efficiency is not None:
            efficiency = EnduranceEfficiencyReceipt.model_validate(
                efficiency.model_dump(mode="python")
            )
        efficiency_sha256 = content_sha256(efficiency) if efficiency is not None else None
        existing = self._existing_command(context=context, operation="finalize")
        if existing is not None:
            if (
                existing.aggregate_id != gate_id
                or existing.input_json.get("efficiency_sha256") != efficiency_sha256
                or existing.result_json is None
            ):
                raise ResearchEnduranceConflict(
                    "finalize idempotency key is rebound to another report"
                )
            return EnduranceMutationReceipt(
                object_id=str(existing.result_json["object_id"]),
                command_id=existing.command_id,
                created=False,
            )
        with session_scope() as session:
            gate_probe = session.get(ResearchEnduranceGateRecord, gate_id)
            if gate_probe is None:
                raise ResearchEnduranceNotFound(f"endurance gate not found: {gate_id}")
            evidence_class = EnduranceEvidenceClass(gate_probe.evidence_class)
        if now is not None and evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
            raise ResearchEnduranceConflict(
                "real-time endurance evidence rejects caller-supplied clocks"
            )
        command = _command(
            operation="finalize",
            gate_id=gate_id,
            payload={
                "efficiency_sha256": efficiency_sha256,
                "efficiency": (
                    efficiency.model_dump(mode="json") if efficiency is not None else None
                ),
            },
            context=context,
            event_type="research_endurance_finalized",
        )

        def apply(session: Session) -> ScientificMutation:
            gate = session.scalar(
                select(ResearchEnduranceGateRecord)
                .where(ResearchEnduranceGateRecord.gate_id == gate_id)
                .with_for_update()
            )
            if gate is None:
                raise ResearchEnduranceNotFound(f"endurance gate not found: {gate_id}")
            if session.get(ResearchEnduranceReportRecord, gate_id) is not None:
                raise ResearchEnduranceConflict(
                    "endurance gate is already finalized under another command"
                )
            manifest = EnduranceGateManifest.model_validate(gate.manifest_json)
            completed_at = self._clock(session, manifest.evidence_class, now)
            rows = self._checkpoint_rows(session, gate_id, lock_tail=True)
            checkpoints = tuple(self._parse_checkpoint(row) for row in rows)
            if checkpoints and completed_at < checkpoints[-1].observation.observed_at:
                raise ResearchEnduranceConflict(
                    "endurance completion predates its last checkpoint"
                )
            if efficiency is not None and not gate.started_at <= efficiency.assessed_at <= completed_at:
                raise ResearchEnduranceSourceError(
                    "efficiency assessment is outside the endurance window"
                )
            if efficiency is not None and efficiency.assessed_by == context.principal:
                raise ResearchEnduranceSourceError(
                    "efficiency assessor must be independent from the gate controller"
                )
            graph = ProgramGraphStore().get_quest(manifest.quest_id)
            final_observation = self._observe(
                session,
                manifest=manifest,
                started_at=gate.started_at,
                observed_at=completed_at,
                graph=graph,
            )
            report = evaluate_endurance_gate(
                manifest=manifest,
                started_at=gate.started_at,
                completed_at=completed_at,
                checkpoints=checkpoints,
                final_observation=final_observation,
                final_graph=graph,
                efficiency=efficiency,
            )
            assert report.report_sha256 is not None
            assert command.command_id is not None
            session.add(
                ResearchEnduranceReportRecord(
                    gate_id=gate_id,
                    quest_id=manifest.quest_id,
                    report_sha256=report.report_sha256,
                    report_json=report.model_dump(mode="json"),
                    evidence_class=manifest.evidence_class.value,
                    disposition=report.disposition.value,
                    elapsed_seconds=report.elapsed_seconds,
                    checkpoint_count=report.checkpoint_count,
                    negative_result_count=report.negative_result_count,
                    reproduction_count=report.reproduction_count,
                    process_kill_count=report.process_kill_count,
                    provider_interruption_count=report.provider_interruption_count,
                    structural_pivot_count=report.structural_pivot_count,
                    portfolio_epoch_count=report.portfolio_epoch_count,
                    real_72h_passed=report.real_72h_passed,
                    eligible_for_f11_scientific_exit_review=(
                        report.eligible_for_f11_scientific_exit_review
                    ),
                    completed_at=completed_at,
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=self._command_created_at(session, command.command_id),
                )
            )
            return ScientificMutation(
                result={
                    "kind": "research_endurance_report",
                    "object_id": gate_id,
                    "report_sha256": report.report_sha256,
                    "disposition": report.disposition.value,
                    "real_72h_passed": report.real_72h_passed,
                },
                event_projection={
                    "gate_id": gate_id,
                    "quest_id": manifest.quest_id,
                    "report_sha256": report.report_sha256,
                    "disposition": report.disposition.value,
                    "evidence_class": manifest.evidence_class.value,
                    "elapsed_seconds": report.elapsed_seconds,
                    "real_72h_passed": report.real_72h_passed,
                    "autonomous_allocation_enabled": False,
                },
            )

        try:
            receipt = self._commands.execute(
                command,
                apply,
                now=now if evidence_class is EnduranceEvidenceClass.ACCELERATED_ENGINEERING else None,
            )
        except IntegrityError as exc:
            raise ResearchEnduranceConflict(
                "endurance report or command identity conflicts with persisted content"
            ) from exc
        return EnduranceMutationReceipt(
            object_id=gate_id,
            command_id=receipt.command_id,
            created=receipt.created,
        )

    @classmethod
    def _snapshot(cls, session: Session, gate: ResearchEnduranceGateRecord) -> EnduranceGateSnapshot:
        try:
            manifest = EnduranceGateManifest.model_validate(gate.manifest_json)
        except Exception as exc:
            raise ResearchEnduranceInvariantError(
                f"endurance manifest is invalid: {gate.gate_id}"
            ) from exc
        if (
            manifest.gate_id != gate.gate_id
            or manifest.quest_id != gate.quest_id
            or manifest.manifest_sha256 != gate.manifest_sha256
            or manifest.evidence_class.value != gate.evidence_class
            or manifest.required_duration_seconds != gate.required_duration_seconds
            or manifest.checkpoint_interval_seconds != gate.checkpoint_interval_seconds
            or manifest.maximum_checkpoint_gap_seconds != gate.maximum_checkpoint_gap_seconds
            or manifest.frozen_quest_spec_sha256 != gate.frozen_quest_spec_sha256
            or manifest.initial_graph_sha256 != gate.initial_graph_sha256
            or manifest.frozen_budget_manifest_sha256 != gate.frozen_budget_manifest_sha256
            or manifest.frozen_data_role_manifest_sha256 != gate.frozen_data_role_manifest_sha256
            or manifest.prerequisite_fault_campaign_id != gate.prerequisite_fault_campaign_id
            or manifest.prerequisite_fault_report_sha256
            != gate.prerequisite_fault_report_sha256
        ):
            raise ResearchEnduranceInvariantError(
                f"endurance start bindings changed: {gate.gate_id}"
            )
        cls._verify_command(
            session,
            command_id=gate.command_id,
            gate_id=gate.gate_id,
            operation="start",
            principal=gate.started_by,
            object_id=gate.gate_id,
        )
        rows = cls._checkpoint_rows(session, gate.gate_id)
        snapshots: list[EnduranceCheckpointSnapshot] = []
        parent = manifest.manifest_sha256
        for sequence, row in enumerate(rows, start=1):
            checkpoint = cls._parse_checkpoint(row)
            if checkpoint.sequence != sequence or checkpoint.parent_sha256 != parent:
                raise ResearchEnduranceInvariantError(
                    f"endurance checkpoint chain broke at: {row.checkpoint_id}"
                )
            assert checkpoint.checkpoint_sha256 is not None
            parent = checkpoint.checkpoint_sha256
            cls._verify_command(
                session,
                command_id=row.command_id,
                gate_id=gate.gate_id,
                operation="checkpoint",
                principal=row.created_by,
                object_id=row.checkpoint_id,
            )
            snapshots.append(
                EnduranceCheckpointSnapshot(
                    checkpoint=checkpoint,
                    command_id=row.command_id,
                    created_by=row.created_by,
                    created_at=row.created_at,
                )
            )
        checkpoints = tuple(item.checkpoint for item in snapshots)
        _all_evidence(checkpoints)
        report_row = session.get(ResearchEnduranceReportRecord, gate.gate_id)
        report = None
        if report_row is not None:
            try:
                report = EnduranceGateReport.model_validate(report_row.report_json)
            except Exception as exc:
                raise ResearchEnduranceInvariantError(
                    f"endurance report is invalid: {gate.gate_id}"
                ) from exc
            if (
                report.manifest != manifest
                or report.report_sha256 != report_row.report_sha256
                or report.disposition.value != report_row.disposition
                or report.manifest.evidence_class.value != report_row.evidence_class
                or report.elapsed_seconds != report_row.elapsed_seconds
                or report.checkpoint_count != report_row.checkpoint_count
                or report.negative_result_count != report_row.negative_result_count
                or report.reproduction_count != report_row.reproduction_count
                or report.process_kill_count != report_row.process_kill_count
                or report.provider_interruption_count != report_row.provider_interruption_count
                or report.structural_pivot_count != report_row.structural_pivot_count
                or report.portfolio_epoch_count != report_row.portfolio_epoch_count
                or report.real_72h_passed != report_row.real_72h_passed
                or report.eligible_for_f11_scientific_exit_review
                != report_row.eligible_for_f11_scientific_exit_review
                or report.completed_at != report_row.completed_at
                or report.checkpoint_chain_sha256
                != _checkpoint_chain_sha256(manifest, checkpoints)
            ):
                raise ResearchEnduranceInvariantError(
                    f"endurance report bindings changed: {gate.gate_id}"
                )
            cls._verify_command(
                session,
                command_id=report_row.command_id,
                gate_id=gate.gate_id,
                operation="finalize",
                principal=report_row.created_by,
                object_id=gate.gate_id,
            )
        return EnduranceGateSnapshot(
            manifest=manifest,
            started_at=gate.started_at,
            checkpoints=tuple(snapshots),
            report=report,
            start_command_id=gate.command_id,
            started_by=gate.started_by,
            created_at=gate.created_at,
        )

    def get(self, gate_id: str) -> EnduranceGateSnapshot:
        if _GATE_ID.fullmatch(gate_id) is None:
            raise ValueError("invalid endurance gate id")
        with session_scope() as session:
            gate = session.get(ResearchEnduranceGateRecord, gate_id)
            if gate is None:
                raise ResearchEnduranceNotFound(f"endurance gate not found: {gate_id}")
            return self._snapshot(session, gate)

    def list(
        self,
        *,
        quest_id: str | None = None,
        limit: int = 100,
    ) -> tuple[EnduranceGateSnapshot, ...]:
        if quest_id is not None and _QUEST_ID.fullmatch(quest_id) is None:
            raise ValueError("invalid endurance Quest id")
        if limit < 1 or limit > 1_000:
            raise ValueError("endurance gate list limit must be in [1, 1000]")
        with session_scope() as session:
            statement = select(ResearchEnduranceGateRecord)
            if quest_id is not None:
                statement = statement.where(ResearchEnduranceGateRecord.quest_id == quest_id)
            rows = session.scalars(
                statement.order_by(
                    ResearchEnduranceGateRecord.started_at,
                    ResearchEnduranceGateRecord.gate_id,
                ).limit(limit)
            ).all()
            return tuple(self._snapshot(session, row) for row in rows)

    def audit(self, quest_id: str) -> EnduranceGateAudit:
        if _QUEST_ID.fullmatch(quest_id) is None:
            raise ValueError("invalid endurance Quest id")
        snapshots = self.list(quest_id=quest_id, limit=1_000)
        latest = snapshots[-1] if snapshots else None
        blockers: list[str] = []
        if latest is None:
            blockers.append("gate:none")
        elif latest.report is None:
            blockers.append(f"gate:running:{latest.manifest.gate_id}")
        elif not latest.report.real_72h_passed:
            blockers.append(
                "gate:latest_not_real_72h_passed:"
                f"{latest.report.disposition.value}:"
                f"{latest.manifest.evidence_class.value}"
            )
        canonical = tuple(sorted(blockers))
        report = latest.report if latest is not None else None
        return EnduranceGateAudit(
            quest_id=quest_id,
            gate_count=len(snapshots),
            latest_gate_id=latest.manifest.gate_id if latest is not None else None,
            latest_disposition=report.disposition if report is not None else None,
            latest_evidence_class=(latest.manifest.evidence_class if latest is not None else None),
            latest_real_72h_passed=bool(report and report.real_72h_passed),
            eligible_for_f11_scientific_exit_review=not canonical,
            autonomous_allocation_enabled=False,
            blockers=canonical,
        )


__all__ = [
    "ResearchEnduranceConflict",
    "ResearchEnduranceError",
    "ResearchEnduranceInvariantError",
    "ResearchEnduranceNotFound",
    "ResearchEnduranceSourceError",
    "ResearchEnduranceStore",
    "evaluate_endurance_gate",
    "prepare_endurance_gate_manifest",
]
