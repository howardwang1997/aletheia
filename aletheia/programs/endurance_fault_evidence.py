"""Convert a committed in-window F11 fault report into typed endurance evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aletheia.db import REPO_ROOT
from aletheia.jobs.fault_injection import validate_fault_campaign_report
from aletheia.jobs.fault_schemas import (
    FaultBoundary,
    FaultCampaignDisposition,
    FaultCampaignReport,
    FaultInjectionOutcome,
    FaultScenarioDisposition,
)
from aletheia.programs.endurance import ResearchEnduranceNotFound, ResearchEnduranceStore
from aletheia.programs.endurance_controller import (
    EnduranceControllerManifest,
    EnduranceEvidenceEnvelope,
    submit_controller_evidence,
)
from aletheia.programs.endurance_schemas import (
    EnduranceCheckpointEvidence,
    EnduranceInterruptionKind,
    EnduranceInterruptionReceipt,
)


class EnduranceFaultEvidenceError(RuntimeError):
    """The fault report cannot support a typed in-window endurance receipt."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnduranceFaultEvidenceSubmission(_FrozenModel):
    gate_id: str = Field(pattern=r"^edg_[0-9a-f]{32}$")
    fault_campaign_id: str = Field(pattern=r"^fic_[0-9a-f]{32}$")
    fault_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_receipt_id: str = Field(pattern=r"^edi_[0-9a-f]{32}$")
    provider_receipt_id: str = Field(pattern=r"^edi_[0-9a-f]{32}$")
    envelope: EnduranceEvidenceEnvelope
    envelope_created: bool
    report_commit_verified: Literal[True] = True
    automatic_checkpoint: Literal[False] = False


def build_endurance_interruption_evidence(
    report: FaultCampaignReport,
) -> EnduranceCheckpointEvidence:
    report = validate_fault_campaign_report(
        FaultCampaignReport.model_validate(report.model_dump(mode="python"))
    )
    if report.disposition is not FaultCampaignDisposition.PASSED:
        raise EnduranceFaultEvidenceError(
            "endurance interruption evidence requires a passing report"
        )
    if report.manifest.campaign_id is None or report.manifest.quest_id is None:
        raise EnduranceFaultEvidenceError("fault report is not Quest-scoped and content-addressed")
    if report.report_sha256 is None:
        raise EnduranceFaultEvidenceError("fault report lacks its content identity")
    specs = {item.scenario_id: item for item in report.manifest.scenarios}

    def select_result(boundary: FaultBoundary):
        matches = [
            item
            for item in report.results
            if specs[item.scenario_id].boundary is boundary
            and item.disposition is FaultScenarioDisposition.PASSED
        ]
        if len(matches) != 1:
            raise EnduranceFaultEvidenceError(
                f"fault report requires one passing {boundary.value} scenario"
            )
        return matches[0]

    process = select_result(FaultBoundary.API_PROCESS)
    provider = select_result(FaultBoundary.PROVIDER)
    if process.observation.observed_outcome is not FaultInjectionOutcome.PROCESS_EXIT:
        raise EnduranceFaultEvidenceError("API process scenario did not observe process_exit")
    if provider.observation.observed_outcome not in {
        FaultInjectionOutcome.UNAVAILABLE,
        FaultInjectionOutcome.TIMEOUT,
    }:
        raise EnduranceFaultEvidenceError(
            "provider scenario did not observe transport interruption"
        )
    receipts = tuple(
        sorted(
            (
                EnduranceInterruptionReceipt(
                    kind=EnduranceInterruptionKind.PROCESS_KILL,
                    fault_campaign_id=report.manifest.campaign_id,
                    fault_report_sha256=report.report_sha256,
                    scenario_id=process.scenario_id,
                    recovery_evidence_sha256s=process.observation.evidence_sha256s,
                    occurred_at=process.observation.completed_at,
                ),
                EnduranceInterruptionReceipt(
                    kind=EnduranceInterruptionKind.PROVIDER_TRANSPORT,
                    fault_campaign_id=report.manifest.campaign_id,
                    fault_report_sha256=report.report_sha256,
                    scenario_id=provider.scenario_id,
                    recovery_evidence_sha256s=provider.observation.evidence_sha256s,
                    occurred_at=provider.observation.completed_at,
                ),
            ),
            key=lambda item: item.receipt_id or "",
        )
    )
    return EnduranceCheckpointEvidence(interruptions=receipts)


def submit_endurance_fault_evidence(
    controller: EnduranceControllerManifest,
    report: FaultCampaignReport,
    *,
    producer: str,
    artifact_root: Path = REPO_ROOT,
) -> EnduranceFaultEvidenceSubmission:
    # Import at call time because fault-campaign persistence references graph persistence;
    # eager loading here would create a jobs -> programs -> jobs package cycle.
    from aletheia.jobs.fault_campaign import FaultCampaignStore, FaultCampaignStoreError

    gate = controller.gate_manifest
    if controller.controller_id is None or gate.gate_id is None:
        raise EnduranceFaultEvidenceError("controller/gate identity is incomplete")
    report = validate_fault_campaign_report(
        FaultCampaignReport.model_validate(report.model_dump(mode="python"))
    )
    if report.manifest.quest_id != gate.quest_id:
        raise EnduranceFaultEvidenceError("fault report belongs to another Quest")
    if report.manifest.campaign_id is None or report.report_sha256 is None:
        raise EnduranceFaultEvidenceError("fault report identity is incomplete")
    try:
        gate_snapshot = ResearchEnduranceStore().get(gate.gate_id)
    except ResearchEnduranceNotFound as exc:
        raise EnduranceFaultEvidenceError("endurance gate has not started") from exc
    if gate_snapshot.report is not None:
        raise EnduranceFaultEvidenceError("terminal endurance gate rejects fault evidence")
    if gate_snapshot.manifest != gate:
        raise EnduranceFaultEvidenceError("controller is bound to another endurance gate")
    try:
        persisted = FaultCampaignStore().get(report.manifest.campaign_id)
    except FaultCampaignStoreError as exc:
        raise EnduranceFaultEvidenceError(
            "fault report is not committed to the append-only fault store"
        ) from exc
    if persisted.report != report or persisted.report.report_sha256 != report.report_sha256:
        raise EnduranceFaultEvidenceError("fault report commit does not replay exact evidence")
    if not gate_snapshot.started_at <= report.completed_at:
        raise EnduranceFaultEvidenceError("fault report completed before the endurance window")
    evidence = build_endurance_interruption_evidence(report)
    if any(item.occurred_at < gate_snapshot.started_at for item in evidence.interruptions):
        raise EnduranceFaultEvidenceError(
            "fault interruption observations occurred before the endurance window"
        )
    envelope, created = submit_controller_evidence(
        controller,
        evidence,
        producer=producer,
        submitted_at=report.completed_at,
        artifact_root=artifact_root,
    )
    by_kind = {item.kind: item for item in evidence.interruptions}
    process = by_kind[EnduranceInterruptionKind.PROCESS_KILL]
    provider = by_kind[EnduranceInterruptionKind.PROVIDER_TRANSPORT]
    assert process.receipt_id is not None
    assert provider.receipt_id is not None
    return EnduranceFaultEvidenceSubmission(
        gate_id=gate.gate_id,
        fault_campaign_id=report.manifest.campaign_id,
        fault_report_sha256=report.report_sha256,
        process_receipt_id=process.receipt_id,
        provider_receipt_id=provider.receipt_id,
        envelope=envelope,
        envelope_created=created,
    )


__all__ = [
    "EnduranceFaultEvidenceError",
    "EnduranceFaultEvidenceSubmission",
    "build_endurance_interruption_evidence",
    "submit_endurance_fault_evidence",
]
