"""Append-only storage and audit for deterministic fault-injection campaigns."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.jobs.fault_injection import (
    FaultCampaignInvariantError,
    validate_fault_campaign_report,
)
from aletheia.jobs.fault_schemas import (
    FaultCampaignAudit,
    FaultCampaignCommitContext,
    FaultCampaignCommitReceipt,
    FaultCampaignDisposition,
    FaultCampaignReport,
    FaultCampaignSnapshot,
)
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import (
    FaultInjectionCampaignRecord,
    ScientificCommandRecord,
)
from aletheia.programs.persistence import ResearchGraphNodeRecord

_CAMPAIGN_ID_PATTERN = re.compile(r"^fic_[0-9a-f]{32}$")
_QUEST_ID_PATTERN = re.compile(r"^qst_[0-9a-f]{32}$")


class FaultCampaignStoreError(RuntimeError):
    """Base error for fault-campaign persistence or reconstruction."""


class FaultCampaignNotFound(FaultCampaignStoreError):
    """A requested campaign or Quest does not exist."""


class FaultCampaignConflict(FaultCampaignStoreError):
    """A content-addressed campaign or command identity was rebound."""


class FaultCampaignPersistenceInvariantError(FaultCampaignStoreError):
    """Persisted campaign evidence no longer reconstructs exactly."""


def _command_result(report: FaultCampaignReport) -> dict[str, object]:
    assert report.manifest.campaign_id is not None
    assert report.report_sha256 is not None
    return {
        "kind": "fault_injection_campaign",
        "campaign_id": report.manifest.campaign_id,
        "manifest_sha256": report.manifest.manifest_sha256,
        "report_sha256": report.report_sha256,
        "disposition": report.disposition.value,
        "scenario_count": report.scenario_count,
        "passed_count": report.passed_count,
        "failed_count": report.failed_count,
        "blocked_count": report.blocked_count,
        "scientific_state_loss_count": report.scientific_state_loss_count,
        "duplicate_scientific_state_count": report.duplicate_scientific_state_count,
        "duplicate_budget_charge_count": report.duplicate_budget_charge_count,
        "duplicate_outward_authorization_count": (
            report.duplicate_outward_authorization_count
        ),
        "unresolved_ambiguity_without_block_count": (
            report.unresolved_ambiguity_without_block_count
        ),
        "event_state_mismatch_count": report.event_state_mismatch_count,
    }


def _command_spec(
    report: FaultCampaignReport,
    context: FaultCampaignCommitContext,
) -> ScientificCommandSpec:
    assert report.manifest.campaign_id is not None
    assert report.report_sha256 is not None
    return ScientificCommandSpec(
        command_type=ScientificCommandType.RESILIENCE_FAULT_CAMPAIGN_COMMIT.value,
        aggregate_type="fault_campaign",
        aggregate_id=report.manifest.campaign_id,
        idempotency_key=context.idempotency_key,
        source_event_key=context.source_event_key,
        input={
            "operation": "commit_campaign",
            **_command_result(report),
        },
        principal=context.principal,
        event_type="fault_injection_campaign_committed",
    )


class FaultCampaignStore:
    """Commit and reconstruct immutable campaign reports through the scientific outbox."""

    def __init__(self) -> None:
        self._commands = ScientificTransitionStore()

    @staticmethod
    def _verify_command(
        session: Session,
        row: FaultInjectionCampaignRecord,
    ) -> ScientificCommandReceipt:
        command = session.get(ScientificCommandRecord, row.command_id)
        if command is None:
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign command is missing: {row.command_id}"
            )
        try:
            ScientificTransitionStore._verify_event(session, command)
            receipt = ScientificTransitionStore._receipt(command, created=False)
        except Exception as exc:
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign command receipt is invalid: {row.command_id}"
            ) from exc
        if (
            command.command_type
            != ScientificCommandType.RESILIENCE_FAULT_CAMPAIGN_COMMIT.value
            or command.aggregate_type != "fault_campaign"
            or command.aggregate_id != row.campaign_id
            or command.principal != row.created_by
        ):
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign command was rebound: {row.command_id}"
            )
        return receipt

    @classmethod
    def _snapshot(
        cls,
        session: Session,
        row: FaultInjectionCampaignRecord,
    ) -> FaultCampaignSnapshot:
        try:
            report = validate_fault_campaign_report(
                FaultCampaignReport.model_validate(row.report_json)
            )
        except (ValidationError, FaultCampaignInvariantError, ValueError, TypeError) as exc:
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign report is invalid: {row.campaign_id}"
            ) from exc
        assert report.manifest.campaign_id is not None
        assert report.report_sha256 is not None
        persisted = {
            "campaign_id": row.campaign_id,
            "quest_id": row.quest_id,
            "manifest_sha256": row.manifest_sha256,
            "report_sha256": row.report_sha256,
            "disposition": row.disposition,
            "scenario_count": row.scenario_count,
            "passed_count": row.passed_count,
            "failed_count": row.failed_count,
            "blocked_count": row.blocked_count,
            "scientific_state_loss_count": row.scientific_state_loss_count,
            "duplicate_scientific_state_count": row.duplicate_scientific_state_count,
            "duplicate_budget_charge_count": row.duplicate_budget_charge_count,
            "duplicate_outward_authorization_count": (
                row.duplicate_outward_authorization_count
            ),
            "unresolved_ambiguity_without_block_count": (
                row.unresolved_ambiguity_without_block_count
            ),
            "event_state_mismatch_count": row.event_state_mismatch_count,
            "completed_at": row.completed_at,
        }
        expected = {
            "campaign_id": report.manifest.campaign_id,
            "quest_id": report.manifest.quest_id,
            "manifest_sha256": report.manifest.manifest_sha256,
            "report_sha256": report.report_sha256,
            "disposition": report.disposition.value,
            "scenario_count": report.scenario_count,
            "passed_count": report.passed_count,
            "failed_count": report.failed_count,
            "blocked_count": report.blocked_count,
            "scientific_state_loss_count": report.scientific_state_loss_count,
            "duplicate_scientific_state_count": report.duplicate_scientific_state_count,
            "duplicate_budget_charge_count": report.duplicate_budget_charge_count,
            "duplicate_outward_authorization_count": (
                report.duplicate_outward_authorization_count
            ),
            "unresolved_ambiguity_without_block_count": (
                report.unresolved_ambiguity_without_block_count
            ),
            "event_state_mismatch_count": report.event_state_mismatch_count,
            "completed_at": report.completed_at,
        }
        if persisted != expected or row.created_at < row.completed_at:
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign bindings changed: {row.campaign_id}"
            )
        command = cls._verify_command(session, row)
        if command.result != _command_result(report):
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign command result changed: {row.campaign_id}"
            )
        return FaultCampaignSnapshot(
            report=report,
            command_id=row.command_id,
            created_by=row.created_by,
            created_at=row.created_at,
        )

    def commit(
        self,
        report: FaultCampaignReport,
        context: FaultCampaignCommitContext,
        *,
        now: datetime | None = None,
    ) -> FaultCampaignCommitReceipt:
        report = validate_fault_campaign_report(report)
        context = FaultCampaignCommitContext.model_validate(
            context.model_dump(mode="python")
        )
        campaign_id = report.manifest.campaign_id
        report_sha256 = report.report_sha256
        assert campaign_id is not None and report_sha256 is not None
        command = _command_spec(report, context)

        def apply(session: Session) -> ScientificMutation:
            if session.get(FaultInjectionCampaignRecord, campaign_id) is not None:
                raise FaultCampaignConflict(
                    "fault campaign identity is already committed under another command"
                )
            if report.manifest.quest_id is not None:
                quest = session.get(ResearchGraphNodeRecord, report.manifest.quest_id)
                if quest is None or quest.node_type != "quest":
                    raise FaultCampaignNotFound(
                        f"fault campaign Quest not found: {report.manifest.quest_id}"
                    )
            created_at = now if now is not None else session.scalar(select(func.now()))
            if created_at is None:  # pragma: no cover - PostgreSQL always returns now()
                raise FaultCampaignPersistenceInvariantError(
                    "database did not provide a fault campaign commit timestamp"
                )
            if (
                created_at.tzinfo is None
                or created_at.utcoffset() is None
                or created_at < report.completed_at
            ):
                raise FaultCampaignConflict(
                    "fault campaign commit timestamp predates its completed evidence"
                )
            session.add(
                FaultInjectionCampaignRecord(
                    campaign_id=campaign_id,
                    quest_id=report.manifest.quest_id,
                    manifest_sha256=report.manifest.manifest_sha256,
                    report_sha256=report_sha256,
                    report_json=report.model_dump(mode="json"),
                    disposition=report.disposition.value,
                    scenario_count=report.scenario_count,
                    passed_count=report.passed_count,
                    failed_count=report.failed_count,
                    blocked_count=report.blocked_count,
                    scientific_state_loss_count=report.scientific_state_loss_count,
                    duplicate_scientific_state_count=(
                        report.duplicate_scientific_state_count
                    ),
                    duplicate_budget_charge_count=report.duplicate_budget_charge_count,
                    duplicate_outward_authorization_count=(
                        report.duplicate_outward_authorization_count
                    ),
                    unresolved_ambiguity_without_block_count=(
                        report.unresolved_ambiguity_without_block_count
                    ),
                    event_state_mismatch_count=report.event_state_mismatch_count,
                    completed_at=report.completed_at,
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=created_at,
                )
            )
            return ScientificMutation(
                result=_command_result(report),
                event_projection={
                    "campaign_id": campaign_id,
                    "report_sha256": report_sha256,
                    "disposition": report.disposition.value,
                    "engineering_evidence_only": True,
                    "autonomous_allocation_enabled": False,
                },
            )

        try:
            receipt = self._commands.execute(command, apply, now=now)
        except IntegrityError as exc:
            raise FaultCampaignConflict(
                "fault campaign or command identity already has different content"
            ) from exc
        if receipt.result != _command_result(report):
            raise FaultCampaignPersistenceInvariantError(
                f"fault campaign replay returned another result: {campaign_id}"
            )
        return FaultCampaignCommitReceipt(
            campaign_id=campaign_id,
            command_id=receipt.command_id,
            report_sha256=report_sha256,
            created=receipt.created,
        )

    def get(self, campaign_id: str) -> FaultCampaignSnapshot:
        if _CAMPAIGN_ID_PATTERN.fullmatch(campaign_id) is None:
            raise ValueError("invalid fault campaign id")
        with session_scope() as session:
            row = session.get(FaultInjectionCampaignRecord, campaign_id)
            if row is None:
                raise FaultCampaignNotFound(f"fault campaign not found: {campaign_id}")
            return self._snapshot(session, row)

    def list(
        self,
        *,
        quest_id: str | None = None,
        limit: int = 100,
    ) -> tuple[FaultCampaignSnapshot, ...]:
        if quest_id is not None and _QUEST_ID_PATTERN.fullmatch(quest_id) is None:
            raise ValueError("invalid fault campaign Quest id")
        if limit < 1 or limit > 1_000:
            raise ValueError("fault campaign list limit must be in [1, 1000]")
        with session_scope() as session:
            statement = select(FaultInjectionCampaignRecord)
            if quest_id is not None:
                statement = statement.where(
                    FaultInjectionCampaignRecord.quest_id == quest_id
                )
            rows = session.scalars(
                statement.order_by(
                    FaultInjectionCampaignRecord.completed_at,
                    FaultInjectionCampaignRecord.campaign_id,
                ).limit(limit)
            ).all()
            return tuple(self._snapshot(session, row) for row in rows)

    def audit(self, quest_id: str) -> FaultCampaignAudit:
        if _QUEST_ID_PATTERN.fullmatch(quest_id) is None:
            raise ValueError("invalid fault campaign Quest id")
        with session_scope() as session:
            quest = session.get(ResearchGraphNodeRecord, quest_id)
            if quest is None or quest.node_type != "quest":
                raise FaultCampaignNotFound(f"fault campaign Quest not found: {quest_id}")
            rows = session.scalars(
                select(FaultInjectionCampaignRecord)
                .where(FaultInjectionCampaignRecord.quest_id == quest_id)
                .order_by(
                    FaultInjectionCampaignRecord.completed_at,
                    FaultInjectionCampaignRecord.campaign_id,
                )
            ).all()
            snapshots = tuple(self._snapshot(session, row) for row in rows)
        latest = snapshots[-1] if snapshots else None
        latest_passed = bool(
            latest
            and latest.report.disposition is FaultCampaignDisposition.PASSED
        )
        blockers: list[str] = []
        if latest is None:
            blockers.append("campaign:none")
        elif not latest_passed:
            blockers.append(
                f"campaign:latest_not_passed:{latest.report.disposition.value}"
            )
        canonical = tuple(sorted(blockers))
        return FaultCampaignAudit(
            quest_id=quest_id,
            campaign_count=len(snapshots),
            passed_campaign_count=sum(
                item.report.disposition is FaultCampaignDisposition.PASSED
                for item in snapshots
            ),
            latest_campaign_id=(
                latest.report.manifest.campaign_id if latest is not None else None
            ),
            latest_campaign_passed=latest_passed,
            eligible_for_endurance_gate_review=not canonical,
            autonomous_allocation_enabled=False,
            blockers=canonical,
        )


__all__ = [
    "FaultCampaignConflict",
    "FaultCampaignNotFound",
    "FaultCampaignPersistenceInvariantError",
    "FaultCampaignStore",
    "FaultCampaignStoreError",
]
