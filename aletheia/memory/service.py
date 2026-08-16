"""Thin helpers over the ledger. Used by the API and by the ``memory.log`` MCP
tool so agents persist work-log entries to the single source of truth."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from aletheia.db import session_scope
from aletheia.jobs.actions import (
    ExternalActionReceipt,
    ExternalActionStatus,
    ExternalActionType,
    OneTimeExternalActionSpec,
    OneTimeExternalActionStore,
)
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.memory.ledger import (
    Artifact,
    BeliefState,
    BudgetEvent,
    Claim,
    ClaimEvidence,
    CampaignSplitLedger,
    ComputeJob,
    CritiquePanel,
    Decision,
    Experiment,
    ExternalValidationLedger,
    HypothesisScorecard,
    HypothesisAttempt,
    LiteratureFinding,
    Metric,
    Run,
    RunManifestRecord,
    SOTAResult,
    WorkerCache,
)
from aletheia.programs.persistence import (
    ResearchBudgetAllocationRecord,
    ResearchCampaignFamilyRecord,
    ResearchCampaignRunRecord,
    ResearchGraphNodeRecord,
    ResearchScientificFamilyRecord,
)
from aletheia.reproducibility.manifest import content_sha256


def _dedup_hash(*parts: Any) -> str:
    """A stable content fingerprint for idempotent (resume-safe) row creation."""
    raw = "\0".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_run(
    goal: str,
    domain: str | None = None,
    direction: str | None = None,
    owner: str | None = None,
    budget_cap_usd: float | None = None,
    gpu_hours_cap: float | None = None,
    status: str = "active",
) -> str:
    with session_scope() as s:
        run = Run(
            goal=goal,
            domain=domain,
            direction=direction,
            human_owner=owner,
            budget_cap_usd=budget_cap_usd,
            gpu_hours_cap=gpu_hours_cap,
            status=status,
        )
        s.add(run)
        s.flush()
        return run.id


def get_run(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        r = s.get(Run, run_id)
        if r is None:
            return None
        plan = (
            s.query(Experiment)
            .filter(Experiment.run_id == run_id, Experiment.stage == "experiment_design")
            .order_by(Experiment.created_at.desc())
            .first()
        )
        return {
            "id": r.id,
            "goal": r.goal,
            "domain": r.domain,
            "direction": r.direction,
            "status": r.status,
            "budget_cap_usd": r.budget_cap_usd,
            "gpu_hours_cap": r.gpu_hours_cap,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "plan": plan.design_json if plan else None,
            "plan_experiment_id": plan.id if plan else None,
        }


def record_run_manifest(
    run_id: str,
    *,
    manifest_sha256: str,
    schema_version: int,
    uri: str,
    payload: dict[str, Any],
    parent_manifest_sha256: str | None = None,
) -> None:
    """Persist the one frozen manifest for a run; retries must be byte-identical."""
    with session_scope() as s:
        existing = (
            s.query(RunManifestRecord).filter(RunManifestRecord.run_id == run_id).one_or_none()
        )
        if existing is not None:
            if existing.manifest_sha256 != manifest_sha256 or existing.payload_json != payload:
                raise ValueError(
                    f"run {run_id} already has a different frozen manifest "
                    f"({existing.manifest_sha256})"
                )
            return
        s.add(
            RunManifestRecord(
                manifest_sha256=manifest_sha256,
                run_id=run_id,
                schema_version=schema_version,
                parent_manifest_sha256=parent_manifest_sha256,
                uri=uri,
                payload_json=payload,
                state="frozen",
            )
        )


def get_run_manifest_record(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.query(RunManifestRecord).filter(RunManifestRecord.run_id == run_id).one_or_none()
        if row is None:
            return None
        return {
            "manifest_sha256": row.manifest_sha256,
            "run_id": row.run_id,
            "schema_version": row.schema_version,
            "parent_manifest_sha256": row.parent_manifest_sha256,
            "uri": row.uri,
            "payload": row.payload_json,
            "state": row.state,
            "frozen_at": row.frozen_at.isoformat() if row.frozen_at else None,
        }


def finalize_plan(run_id: str, plan: dict[str, Any]) -> str:
    """Record the agreed experiment plan: mark the run planned and create an
    EXPERIMENT_DESIGN experiment carrying the structured plan. Returns its id.
    This is the hand-off artifact for Phase-1 execution."""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = "planned"
            if plan.get("domain"):
                run.domain = plan["domain"]
            if plan.get("direction"):
                run.direction = plan["direction"]
            if plan.get("objective"):
                run.goal = plan["objective"]
        exp = Experiment(
            run_id=run_id,
            hypothesis=plan.get("hypothesis"),
            design_json=plan,
            stage="experiment_design",
            status="planned",
        )
        s.add(exp)
        s.flush()
        return exp.id


def create_experiment(
    run_id: str,
    plan: dict[str, Any],
    *,
    hypothesis: str | None = None,
    parent_experiment_id: str | None = None,
    stage: str = "experiment_design",
    status: str = "planned",
) -> str:
    """Create an additional Experiment for a campaign round, linked to the prior
    round via ``parent_experiment_id``. Round 1 reuses ``finalize_plan``'s
    experiment; rounds 2..N use this. Returns the new experiment id.

    Idempotent: keyed on a content fingerprint of (run_id, parent, plan, stage), so a
    RESUMED run that replays this round REUSES the existing experiment instead of
    inserting a duplicate."""
    key = _dedup_hash(
        run_id, parent_experiment_id, stage, json.dumps(plan, sort_keys=True, default=str)
    )
    with session_scope() as s:
        existing = (
            s.query(Experiment)
            .filter(Experiment.run_id == run_id, Experiment.dedup_key == key)
            .first()
        )
        if existing is not None:
            return existing.id
        exp = Experiment(
            run_id=run_id,
            hypothesis=hypothesis or plan.get("hypothesis"),
            design_json=plan,
            stage=stage,
            status=status,
            parent_experiment_id=parent_experiment_id,
            dedup_key=key,
        )
        s.add(exp)
        s.flush()
        return exp.id


def set_experiment_hypothesis(experiment_id: str, hypothesis: str) -> None:
    """Record the hypothesis the IDEATE stage chose for this experiment."""
    with session_scope() as s:
        exp = s.get(Experiment, experiment_id)
        if exp is not None:
            exp.hypothesis = hypothesis


def set_experiment_repo(
    experiment_id: str, code_repo: str | None = None, code_branch: str | None = None
) -> None:
    """Record the GitHub repo/branch this experiment lives in (IAM audit trail)."""
    with session_scope() as s:
        exp = s.get(Experiment, experiment_id)
        if exp is not None:
            if code_repo is not None:
                exp.code_repo = code_repo
            if code_branch is not None:
                exp.code_branch = code_branch


def set_run_status(run_id: str, status: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = status


# --- Epistemic Seal v2: campaign splits + family-wise attempt disclosure -----------------------


def seal_campaign_splits(
    run_id: str,
    *,
    dataset_fingerprint: str,
    row_identity_hash: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Create the immutable split ledger, or verify/reuse it on resume."""
    with session_scope() as s:
        existing = s.query(CampaignSplitLedger).filter(CampaignSplitLedger.run_id == run_id).first()
        if existing is not None:
            if existing.dataset_fingerprint != dataset_fingerprint:
                raise RuntimeError("staged dataset changed after campaign split sealing")
            if existing.row_identity_hash != row_identity_hash:
                raise RuntimeError("stable row identity changed after campaign split sealing")
            if (existing.plan_json or {}).get("membership_hash") != plan.get("membership_hash"):
                raise RuntimeError("campaign split allocation changed on resume")
            return {
                "id": existing.id,
                "plan": existing.plan_json,
                "state": existing.state,
                "final_result": existing.final_result_json,
                "reused": True,
            }
        row = CampaignSplitLedger(
            run_id=run_id,
            dataset_fingerprint=dataset_fingerprint,
            row_identity_hash=row_identity_hash,
            split_algo_version=int(plan.get("split_algo_version", 0)),
            state="sealed",
            plan_json=plan,
        )
        s.add(row)
        s.flush()
        return {
            "id": row.id,
            "plan": row.plan_json,
            "state": row.state,
            "final_result": None,
            "reused": False,
        }


def get_campaign_split_ledger(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.query(CampaignSplitLedger).filter(CampaignSplitLedger.run_id == run_id).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "dataset_fingerprint": row.dataset_fingerprint,
            "row_identity_hash": row.row_identity_hash,
            "state": row.state,
            "plan": row.plan_json,
            "final_result": row.final_result_json,
            "final_action_id": row.final_action_id,
            "final_action_receipt_sha256": row.final_action_receipt_sha256,
            "final_opened_at": row.final_opened_at.isoformat() if row.final_opened_at else None,
        }


def claim_final_holdout(
    run_id: str,
    *,
    claim_owner: str = "experiment-driver",
    claim_ttl_seconds: int = 86_400,
) -> dict[str, Any]:
    """Claim one final-holdout opening and return its non-persisted execution token once."""

    with session_scope() as s:
        row = s.query(CampaignSplitLedger).filter(CampaignSplitLedger.run_id == run_id).first()
        if row is None:
            raise RuntimeError("campaign split ledger is missing")
        if row.state != "sealed" and row.final_action_id is None:
            # Legacy pre-F11-S2 rows remain fail-closed: opening is already consumed and no token
            # can be reconstructed or safely reissued.
            return {
                "claimed": False,
                "state": row.state,
                "result": row.final_result_json,
                "reconciliation_required": row.final_result_json is None,
            }
        final = dict((row.plan_json or {}).get("final_holdout") or {})
        action_request = {
            "ledger_id": row.id,
            "dataset_fingerprint": row.dataset_fingerprint,
            "row_identity_hash": row.row_identity_hash,
            "split_algo_version": row.split_algo_version,
            "final_holdout": {
                "index_hash": final.get("index_hash"),
                "n": final.get("n"),
                "alpha": final.get("alpha"),
            },
        }

    spec = OneTimeExternalActionSpec(
        run_id=run_id,
        action_type=ExternalActionType.FINAL_HOLDOUT_OPEN.value,
        scope_key=f"final-holdout:{run_id}",
        request=action_request,
        principal="epistemic-seal",
        claim_ttl_seconds=claim_ttl_seconds,
    )

    def open_ledger(session, action_id: str, claimed_at: datetime) -> None:
        row = (
            session.query(CampaignSplitLedger)
            .filter(CampaignSplitLedger.run_id == run_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise RuntimeError("campaign split ledger is missing")
        if row.state != "sealed" or row.final_action_id is not None:
            raise RuntimeError("final holdout was already opened")
        row.state = "final_opened"
        row.final_action_id = action_id
        row.final_opened_at = claimed_at

    claimed = OneTimeExternalActionStore().claim(
        spec,
        claim_owner=claim_owner,
        on_claim=open_ledger,
    )
    action = claimed.action
    with session_scope() as s:
        persisted = (
            s.query(CampaignSplitLedger).filter(CampaignSplitLedger.run_id == run_id).first()
        )
        result = None if persisted is None else persisted.final_result_json
    return {
        "claimed": claimed.created,
        "state": "final_completed"
        if action.status is ExternalActionStatus.COMPLETED
        else "final_opened",
        "result": result,
        "action_id": action.action_id,
        "execution_token": claimed.execution_token,
        "provider_idempotency_key": action.provider_idempotency_key,
        "action_status": action.status.value,
        "reconcile_after": action.reconcile_after.isoformat(),
        "reconciliation_required": (action.status is ExternalActionStatus.RECONCILIATION_REQUIRED),
    }


def record_final_holdout_result(
    run_id: str,
    result: dict[str, Any],
    *,
    action_id: str,
    execution_token: str,
    provider_receipt: dict[str, Any] | None = None,
    completed_by: str = "experiment-driver",
) -> dict[str, Any]:
    def complete_ledger(session, receipt: ExternalActionReceipt) -> None:
        row = (
            session.query(CampaignSplitLedger)
            .filter(CampaignSplitLedger.run_id == run_id)
            .with_for_update()
            .first()
        )
        if (
            row is None
            or row.state not in ("final_opened", "final_completed")
            or row.final_action_id != action_id
        ):
            raise RuntimeError("final holdout was not atomically opened by this action")
        if row.final_result_json is not None and row.final_result_json != result:
            raise RuntimeError("final holdout result is immutable")
        row.final_result_json = result
        row.final_action_receipt_sha256 = receipt.receipt_sha256
        row.state = "final_completed"

    completion = OneTimeExternalActionStore().complete(
        action_id=action_id,
        execution_token=execution_token,
        outcome=result,
        provider_receipt=provider_receipt or {},
        completed_by=completed_by,
        event_projection={"ledger": "final_holdout", "result": result},
        on_complete=complete_ledger,
    )
    return {
        **completion.receipt.model_dump(mode="json"),
        "receipt_sha256": completion.receipt.receipt_sha256,
        "replayed": completion.replayed,
    }


def seal_external_validation(
    run_id: str,
    *,
    data_asset_id: str,
    dataset_fingerprint: str,
    row_identity_hash: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Seal the external bytes/rows before any adaptive campaign outcome exists."""
    with session_scope() as s:
        existing = (
            s.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .first()
        )
        if existing is not None:
            immutable = (
                existing.data_asset_id == data_asset_id
                and existing.dataset_fingerprint == dataset_fingerprint
                and existing.row_identity_hash == row_identity_hash
                and (existing.provenance_json or {}) == provenance
            )
            if not immutable:
                raise RuntimeError("external-validation dataset changed after sealing")
            return {
                "id": existing.id,
                "state": existing.state,
                "result": existing.result_json,
                "reused": True,
            }
        row = ExternalValidationLedger(
            run_id=run_id,
            data_asset_id=data_asset_id,
            dataset_fingerprint=dataset_fingerprint,
            row_identity_hash=row_identity_hash,
            state="sealed",
            provenance_json=provenance,
        )
        s.add(row)
        s.flush()
        return {"id": row.id, "state": row.state, "result": None, "reused": False}


def get_external_validation_ledger(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = (
            s.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .first()
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "data_asset_id": row.data_asset_id,
            "dataset_fingerprint": row.dataset_fingerprint,
            "row_identity_hash": row.row_identity_hash,
            "state": row.state,
            "provenance": row.provenance_json,
            "result": row.result_json,
            "action_id": row.action_id,
            "action_receipt_sha256": row.action_receipt_sha256,
            "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        }


def claim_external_validation(
    run_id: str,
    *,
    claim_owner: str = "experiment-driver",
    claim_ttl_seconds: int = 86_400,
) -> dict[str, Any]:
    """Claim one external-validation opening and return its execution token once."""

    with session_scope() as s:
        row = (
            s.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .first()
        )
        if row is None:
            raise RuntimeError("external-validation ledger is missing")
        if row.state != "sealed" and row.action_id is None:
            return {
                "claimed": False,
                "state": row.state,
                "result": row.result_json,
                "reconciliation_required": row.result_json is None,
            }
        action_request = {
            "ledger_id": row.id,
            "data_asset_id": row.data_asset_id,
            "dataset_fingerprint": row.dataset_fingerprint,
            "row_identity_hash": row.row_identity_hash,
            "provenance_sha256": content_sha256(row.provenance_json or {}),
        }

    spec = OneTimeExternalActionSpec(
        run_id=run_id,
        action_type=ExternalActionType.EXTERNAL_VALIDATION_OPEN.value,
        scope_key=f"external-validation:{run_id}",
        request=action_request,
        principal="external-validation-ledger",
        claim_ttl_seconds=claim_ttl_seconds,
    )

    def open_ledger(session, action_id: str, claimed_at: datetime) -> None:
        row = (
            session.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise RuntimeError("external-validation ledger is missing")
        if row.state != "sealed" or row.action_id is not None:
            raise RuntimeError("external-validation dataset was already opened")
        row.state = "opened"
        row.action_id = action_id
        row.opened_at = claimed_at

    claimed = OneTimeExternalActionStore().claim(
        spec,
        claim_owner=claim_owner,
        on_claim=open_ledger,
    )
    action = claimed.action
    with session_scope() as s:
        persisted = (
            s.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .first()
        )
        result = None if persisted is None else persisted.result_json
    return {
        "claimed": claimed.created,
        "state": "completed" if action.status is ExternalActionStatus.COMPLETED else "opened",
        "result": result,
        "action_id": action.action_id,
        "execution_token": claimed.execution_token,
        "provider_idempotency_key": action.provider_idempotency_key,
        "action_status": action.status.value,
        "reconcile_after": action.reconcile_after.isoformat(),
        "reconciliation_required": (action.status is ExternalActionStatus.RECONCILIATION_REQUIRED),
    }


def record_external_validation_result(
    run_id: str,
    result: dict[str, Any],
    *,
    action_id: str,
    execution_token: str,
    provider_receipt: dict[str, Any] | None = None,
    completed_by: str = "experiment-driver",
) -> dict[str, Any]:
    def complete_ledger(session, receipt: ExternalActionReceipt) -> None:
        row = (
            session.query(ExternalValidationLedger)
            .filter(ExternalValidationLedger.run_id == run_id)
            .with_for_update()
            .first()
        )
        if row is None or row.state not in ("opened", "completed") or row.action_id != action_id:
            raise RuntimeError(
                "external-validation dataset was not atomically opened by this action"
            )
        if row.result_json is not None and row.result_json != result:
            raise RuntimeError("external-validation result is immutable")
        row.result_json = result
        row.action_receipt_sha256 = receipt.receipt_sha256
        row.state = "completed"

    completion = OneTimeExternalActionStore().complete(
        action_id=action_id,
        execution_token=execution_token,
        outcome=result,
        provider_receipt=provider_receipt or {},
        completed_by=completed_by,
        event_projection={"ledger": "external_validation", "result": result},
        on_complete=complete_ledger,
    )
    return {
        **completion.receipt.model_dump(mode="json"),
        "receipt_sha256": completion.receipt.receipt_sha256,
        "replayed": completion.replayed,
    }


def register_hypothesis_attempt(
    run_id: str,
    *,
    experiment_id: str | None,
    family_key: str,
    hypothesis_text: str,
    round_index: int,
    phase: str,
    split_hash: str,
    alpha_allocated: float,
    confirmation_batch: int | None = None,
    research_family_id: str | None = None,
) -> int:
    hypothesis_key = _dedup_hash(family_key, hypothesis_text)
    with session_scope() as s:
        run_binding = (
            s.query(ResearchCampaignRunRecord)
            .filter(ResearchCampaignRunRecord.run_id == run_id)
            .first()
        )
        resolved_family_id = research_family_id
        if run_binding is not None:
            campaign_family = s.get(
                ResearchCampaignFamilyRecord,
                run_binding.campaign_node_id,
            )
            if campaign_family is None:
                raise RuntimeError("research campaign has no scientific family binding")
            graph_family = s.get(
                ResearchScientificFamilyRecord,
                campaign_family.family_id,
            )
            if graph_family is None:
                raise RuntimeError("research campaign scientific family is missing")
            if resolved_family_id is not None and resolved_family_id != graph_family.family_id:
                raise RuntimeError("hypothesis attempt changed scientific family identity")
            if family_key != graph_family.family_key:
                raise RuntimeError(
                    "hypothesis family key does not match the campaign scientific family"
                )
            resolved_family_id = graph_family.family_id
        elif resolved_family_id is not None:
            graph_family = s.get(ResearchScientificFamilyRecord, resolved_family_id)
            if graph_family is None or graph_family.family_key != family_key:
                raise RuntimeError("hypothesis scientific family identity is invalid")
        existing = (
            s.query(HypothesisAttempt)
            .filter(
                HypothesisAttempt.run_id == run_id,
                HypothesisAttempt.experiment_id == experiment_id,
                HypothesisAttempt.phase == phase,
            )
            .first()
        )
        if existing is not None:
            if (
                existing.split_hash != split_hash
                or existing.hypothesis_key != hypothesis_key
                or abs(float(existing.alpha_allocated) - float(alpha_allocated)) > 1e-12
                or existing.research_family_id != resolved_family_id
            ):
                raise RuntimeError("hypothesis attempt changed after registration")
            return int(existing.id)
        row = HypothesisAttempt(
            run_id=run_id,
            research_family_id=resolved_family_id,
            experiment_id=experiment_id,
            family_key=family_key,
            hypothesis_key=hypothesis_key,
            hypothesis_text=hypothesis_text,
            round_index=int(round_index),
            phase=phase,
            confirmation_batch=confirmation_batch,
            split_hash=split_hash,
            alpha_allocated=float(alpha_allocated),
            status="registered",
        )
        s.add(row)
        s.flush()
        return int(row.id)


def finish_hypothesis_attempt(attempt_id: int, *, status: str, outcome: dict[str, Any]) -> None:
    with session_scope() as s:
        row = s.get(HypothesisAttempt, attempt_id)
        if row is None:
            raise RuntimeError("hypothesis attempt is missing")
        if row.outcome_json is not None and row.outcome_json != outcome:
            raise RuntimeError("hypothesis attempt outcome is immutable")
        row.status = status
        row.outcome_json = outcome


def list_hypothesis_attempts(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (
            s.query(HypothesisAttempt)
            .filter(HypothesisAttempt.run_id == run_id)
            .order_by(HypothesisAttempt.id)
            .all()
        )
        return [
            {
                "id": row.id,
                "experiment_id": row.experiment_id,
                "research_family_id": row.research_family_id,
                "family_key": row.family_key,
                "hypothesis_key": row.hypothesis_key,
                "hypothesis_text": row.hypothesis_text,
                "round": row.round_index,
                "phase": row.phase,
                "confirmation_batch": row.confirmation_batch,
                "split_hash": row.split_hash,
                "alpha_allocated": row.alpha_allocated,
                "status": row.status,
                "outcome": row.outcome_json,
            }
            for row in rows
        ]


def list_scientific_family_attempts(research_family_id: str) -> list[dict[str, Any]]:
    """List attempts across every campaign/run bound to one immutable scientific family."""

    with session_scope() as s:
        family = s.get(ResearchScientificFamilyRecord, research_family_id)
        if family is None:
            raise RuntimeError("research scientific family is missing")
        rows = (
            s.query(HypothesisAttempt)
            .filter(HypothesisAttempt.research_family_id == research_family_id)
            .order_by(HypothesisAttempt.id)
            .all()
        )
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "experiment_id": row.experiment_id,
                "research_family_id": row.research_family_id,
                "family_key": row.family_key,
                "hypothesis_key": row.hypothesis_key,
                "hypothesis_text": row.hypothesis_text,
                "round": row.round_index,
                "phase": row.phase,
                "confirmation_batch": row.confirmation_batch,
                "split_hash": row.split_hash,
                "alpha_allocated": row.alpha_allocated,
                "status": row.status,
                "outcome": row.outcome_json,
            }
            for row in rows
        ]


def list_runs() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Run).order_by(Run.created_at.desc()).all()
        return [
            {
                "id": r.id,
                "goal": r.goal,
                "domain": r.domain,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def log_note(
    run_id: str,
    note: str,
    actor: str = "orchestrator",
    stage_from: str | None = None,
    stage_to: str | None = None,
    experiment_id: str | None = None,
) -> int:
    """Append a work-log / decision entry. Returns the decision row id."""
    with session_scope() as s:
        d = Decision(
            run_id=run_id,
            experiment_id=experiment_id,
            stage_from=stage_from,
            stage_to=stage_to,
            rationale=note,
            actor=actor,
        )
        s.add(d)
        s.flush()
        return d.id


# --- compute / metrics / artifacts (Phase 1) -------------------------------


def create_compute_job(
    experiment_id: str | None,
    backend: str,
    resources: dict[str, Any] | None = None,
    status: str = "queued",
) -> str:
    with session_scope() as s:
        job = ComputeJob(
            experiment_id=experiment_id,
            backend=backend,
            status=status,
            resources_json=resources,
        )
        s.add(job)
        s.flush()
        return job.id


def set_compute_job_status(job_id: str, status: str, ext_id: str | None = None) -> None:
    with session_scope() as s:
        job = s.get(ComputeJob, job_id)
        if job is not None:
            job.status = status
            if ext_id is not None:
                job.ext_id = ext_id


def get_compute_job(job_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        job = s.get(ComputeJob, job_id)
        if job is None:
            return None
        return {
            "id": job.id,
            "experiment_id": job.experiment_id,
            "backend": job.backend,
            "status": job.status,
            "ext_id": job.ext_id,
            "resources": job.resources_json,
        }


def record_metrics(
    experiment_id: str,
    metrics: dict[str, float],
    split: str | None = "test",
    step: int | None = None,
) -> None:
    """Idempotent on the natural key (experiment_id, name, split, step): a resumed run that
    recomputes the same metrics UPDATES the value in place rather than inserting duplicates."""
    with session_scope() as s:
        for name, value in metrics.items():
            existing = (
                s.query(Metric)
                .filter(
                    Metric.experiment_id == experiment_id,
                    Metric.name == name,
                    Metric.split == split,
                    Metric.step == step,
                )
                .first()
            )
            if existing is not None:
                existing.value = float(value)
            else:
                s.add(
                    Metric(
                        experiment_id=experiment_id,
                        name=name,
                        value=float(value),
                        split=split,
                        step=step,
                    )
                )


def record_artifacts(
    experiment_id: str,
    artifacts: list[dict[str, Any]],
    *,
    idempotency_key: str | None = None,
    principal: str = "artifact-committer",
) -> ScientificCommandReceipt:
    """Commit one artifact batch and its keyed event once under worker redelivery.

    Files are expected to have been durably written before this call.  A failed database
    transaction may therefore leave an unreferenced content-addressed file, but it cannot leave an
    artifact ledger row without the matching command receipt and event.
    """

    with session_scope() as s:
        experiment = s.get(Experiment, experiment_id)
        if experiment is None:
            raise RuntimeError("artifact experiment is missing")
        run_id = experiment.run_id

    normalized = [
        {
            "kind": str(artifact.get("kind", "model")),
            "uri": str(artifact.get("uri", "")),
            "sha256": artifact.get("sha256"),
            "bytes": artifact.get("bytes"),
        }
        for artifact in artifacts
    ]
    batch_sha256 = content_sha256({"experiment_id": experiment_id, "artifacts": normalized})
    spec = ScientificCommandSpec(
        run_id=run_id,
        command_type=ScientificCommandType.ARTIFACT_COMMIT.value,
        aggregate_type="experiment_artifacts",
        aggregate_id=experiment_id,
        idempotency_key=(idempotency_key or f"artifact:{experiment_id}:{batch_sha256}"),
        input={"experiment_id": experiment_id, "artifacts": normalized},
        principal=principal,
        event_type="artifacts_committed",
    )

    def apply(session):
        if session.get(Experiment, experiment_id) is None:
            raise RuntimeError("artifact experiment disappeared")
        rows: list[Artifact] = []
        for ordinal, artifact in enumerate(spec.input["artifacts"]):
            row = Artifact(
                experiment_id=experiment_id,
                kind=artifact["kind"],
                uri=artifact["uri"],
                sha256=artifact.get("sha256"),
                bytes=artifact.get("bytes"),
                scientific_command_id=spec.command_id,
                commit_ordinal=ordinal,
            )
            session.add(row)
            rows.append(row)
        session.flush()
        artifact_ids = [int(row.id) for row in rows]
        return ScientificMutation(
            result={
                "experiment_id": experiment_id,
                "artifact_ids": artifact_ids,
                "artifact_count": len(rows),
                "batch_sha256": batch_sha256,
            },
            event_projection={
                "experiment_id": experiment_id,
                "artifact_ids": artifact_ids,
                "artifacts": spec.input["artifacts"],
                "batch_sha256": batch_sha256,
            },
        )

    return ScientificTransitionStore().execute(spec, apply)


def list_metrics(experiment_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Metric).filter(Metric.experiment_id == experiment_id).all()
        return [{"name": m.name, "value": m.value, "split": m.split, "step": m.step} for m in rows]


def list_artifacts(experiment_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Artifact).filter(Artifact.experiment_id == experiment_id).all()
        return [{"kind": a.kind, "uri": a.uri, "sha256": a.sha256, "bytes": a.bytes} for a in rows]


# --- budget events (Phase 1) ----------------------------------------------


def record_budget_event(
    run_id: str,
    kind: str,
    amount: float,
    *,
    research_budget_allocation_id: str | None = None,
) -> float:
    """Append a budget charge of ``kind`` and return the new running total for that
    kind (cumulative = previous cumulative + amount)."""
    with session_scope() as s:
        allocation = None
        if research_budget_allocation_id is not None:
            if amount < 0:
                raise RuntimeError("allocated budget charges cannot be negative")
            allocation = (
                s.query(ResearchBudgetAllocationRecord)
                .filter(
                    ResearchBudgetAllocationRecord.allocation_id
                    == research_budget_allocation_id
                )
                .with_for_update()
                .first()
            )
            if allocation is None:
                raise RuntimeError("research budget allocation is missing")
            if allocation.kind != kind:
                raise RuntimeError("budget event kind does not match its allocation")
            run_binding = (
                s.query(ResearchCampaignRunRecord)
                .filter(ResearchCampaignRunRecord.run_id == run_id)
                .first()
            )
            if run_binding is None or run_binding.quest_id != allocation.quest_id:
                raise RuntimeError("run is outside the budget allocation quest")
            scope = s.get(ResearchGraphNodeRecord, allocation.scope_node_id)
            campaign = s.get(ResearchGraphNodeRecord, run_binding.campaign_node_id)
            if scope is None or campaign is None:
                raise RuntimeError("budget allocation graph scope is missing")
            if scope.node_type == "program" and campaign.parent_node_id != scope.node_id:
                raise RuntimeError("run is outside the budget allocation program")
            allocated_rows = (
                s.query(BudgetEvent)
                .filter(
                    BudgetEvent.research_budget_allocation_id
                    == research_budget_allocation_id
                )
                .all()
            )
            spent_microunits = sum(
                int(round(float(row.amount) * 1_000_000)) for row in allocated_rows
            )
            charge_microunits = int(round(float(amount) * 1_000_000))
            if spent_microunits + charge_microunits > int(allocation.cap_microunits):
                raise RuntimeError("research budget allocation cap exceeded")
        last = (
            s.query(BudgetEvent)
            .filter(BudgetEvent.run_id == run_id, BudgetEvent.kind == kind)
            .order_by(BudgetEvent.id.desc())
            .first()
        )
        prev = last.cumulative if last and last.cumulative is not None else 0.0
        cumulative = prev + float(amount)
        s.add(
            BudgetEvent(
                run_id=run_id,
                research_budget_allocation_id=(
                    allocation.allocation_id if allocation is not None else None
                ),
                kind=kind,
                amount=float(amount),
                cumulative=cumulative,
            )
        )
        return cumulative


def budget_spent(run_id: str, kind: str = "usd") -> float:
    with session_scope() as s:
        last = (
            s.query(BudgetEvent)
            .filter(BudgetEvent.run_id == run_id, BudgetEvent.kind == kind)
            .order_by(BudgetEvent.id.desc())
            .first()
        )
        return float(last.cumulative) if last and last.cumulative is not None else 0.0


# --- worker result cache (resume / checkpoint) --------------------------------


def get_cached_worker(run_id: str, cache_key: str) -> str | None:
    """Return a previously-stored worker result for this (run, call), or None."""
    with session_scope() as s:
        row = (
            s.query(WorkerCache)
            .filter(WorkerCache.run_id == run_id, WorkerCache.cache_key == cache_key)
            .first()
        )
        return row.result if row is not None else None


def put_cached_worker(run_id: str, cache_key: str, label: str | None, result: str) -> None:
    """Store a successful worker result, idempotent on (run_id, cache_key)."""
    with session_scope() as s:
        existing = (
            s.query(WorkerCache)
            .filter(WorkerCache.run_id == run_id, WorkerCache.cache_key == cache_key)
            .first()
        )
        if existing is not None:
            existing.result = result
        else:
            s.add(WorkerCache(run_id=run_id, cache_key=cache_key, label=label, result=result))


# --- critic panels (Phase 1) ----------------------------------------------


def record_critique_panel(
    target: str,
    target_ref: str | None,
    consensus_verdict: str | None,
    gate_passed: bool | None,
    raw_json: dict[str, Any] | None,
) -> str:
    with session_scope() as s:
        panel = CritiquePanel(
            target=target,
            target_ref=target_ref,
            consensus_verdict=consensus_verdict,
            gate_passed=gate_passed,
            raw_json=raw_json,
        )
        s.add(panel)
        s.flush()
        return panel.id


# --- evidence ledger: claims ↔ evidence (Phase G) -------------------------


def create_claim(
    run_id: str,
    *,
    claim_text: str,
    claim_type: str,
    strength: str,
    status: str = "proposed",
    experiment_id: str | None = None,
    created_by: str | None = None,
    stage: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    """Record a scientific claim + (optionally) attach its evidence in one call.
    ``evidence`` items: {evidence_kind, evidence_ref, note?}.

    Idempotent: keyed on a content fingerprint of (run_id, experiment_id, claim_type,
    claim_text), so a RESUMED run that replays a stage UPDATES the existing claim (and
    skips re-attaching its evidence) instead of inserting a duplicate. The returned id is
    stable across attempts, so the driver's later ``update_claim`` lands on the same row."""
    key = _dedup_hash(run_id, experiment_id, claim_type, claim_text)
    with session_scope() as s:
        existing = s.query(Claim).filter(Claim.run_id == run_id, Claim.dedup_key == key).first()
        if existing is not None:
            existing.strength = strength
            existing.status = status
            if stage is not None:
                existing.stage = stage
            return existing.id
        claim = Claim(
            run_id=run_id,
            experiment_id=experiment_id,
            claim_text=claim_text,
            claim_type=claim_type,
            strength=strength,
            status=status,
            created_by=created_by,
            stage=stage,
            dedup_key=key,
        )
        s.add(claim)
        s.flush()
        for e in evidence or []:
            s.add(
                ClaimEvidence(
                    claim_id=claim.id,
                    evidence_kind=e.get("evidence_kind", "artifact"),
                    evidence_ref=str(e.get("evidence_ref", "")),
                    note=e.get("note"),
                )
            )
        return claim.id


def update_claim(
    claim_id: str,
    *,
    strength: str | None = None,
    status: str | None = None,
    claim_text: str | None = None,
) -> None:
    """Finalize a claim once more evidence is in (e.g. after the results gate)."""
    with session_scope() as s:
        claim = s.get(Claim, claim_id)
        if claim is None:
            return
        if strength is not None:
            claim.strength = strength
        if status is not None:
            claim.status = status
        if claim_text is not None:
            claim.claim_text = claim_text


def attach_claim_evidence(
    claim_id: str, evidence_kind: str, evidence_ref: str, note: str | None = None
) -> None:
    with session_scope() as s:
        s.add(
            ClaimEvidence(
                claim_id=claim_id,
                evidence_kind=evidence_kind,
                evidence_ref=str(evidence_ref),
                note=note,
            )
        )


# --- K2 belief state: durable mirror of the campaign's calibrated credences ----------------


def upsert_credence(
    run_id: str, question_key: str, alpha: float, beta: float, n_updates: int = 0
) -> None:
    """Write-through the in-memory credence for an open-question lineage. Upsert by
    ``(run_id, question_key)`` so the durable row always reflects the latest belief — the credence
    is a planning aid, never a verdict, so this stores state only (it sets no claim strength)."""
    with session_scope() as s:
        row = (
            s.query(BeliefState)
            .filter(BeliefState.run_id == run_id, BeliefState.question_key == question_key)
            .one_or_none()
        )
        if row is None:
            s.add(
                BeliefState(
                    run_id=run_id,
                    question_key=question_key,
                    alpha=float(alpha),
                    beta=float(beta),
                    n_updates=int(n_updates),
                )
            )
        else:
            row.alpha = float(alpha)
            row.beta = float(beta)
            row.n_updates = int(n_updates)


def get_credence(run_id: str, question_key: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = (
            s.query(BeliefState)
            .filter(BeliefState.run_id == run_id, BeliefState.question_key == question_key)
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "question_key": row.question_key,
            "alpha": row.alpha,
            "beta": row.beta,
            "n_updates": row.n_updates,
        }


def list_credences(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (
            s.query(BeliefState)
            .filter(BeliefState.run_id == run_id)
            .order_by(BeliefState.updated_at.asc())
            .all()
        )
        return [
            {
                "question_key": r.question_key,
                "alpha": r.alpha,
                "beta": r.beta,
                "n_updates": r.n_updates,
            }
            for r in rows
        ]


def list_claims(run_id: str, experiment_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Claim).filter(Claim.run_id == run_id)
        if experiment_id is not None:
            q = q.filter(Claim.experiment_id == experiment_id)
        rows = q.order_by(Claim.created_at).all()
        out: list[dict[str, Any]] = []
        for c in rows:
            ev = s.query(ClaimEvidence).filter(ClaimEvidence.claim_id == c.id).all()
            out.append(
                {
                    "id": c.id,
                    "claim_text": c.claim_text,
                    "claim_type": c.claim_type,
                    "strength": c.strength,
                    "status": c.status,
                    "created_by": c.created_by,
                    "stage": c.stage,
                    "experiment_id": c.experiment_id,
                    "evidence": [
                        {
                            "evidence_kind": e.evidence_kind,
                            "evidence_ref": e.evidence_ref,
                            "note": e.note,
                        }
                        for e in ev
                    ],
                }
            )
        return out


# --- structured literature + SOTA (Phase H) -------------------------------

_LIT_FIELDS = (
    "paper_id",
    "query",
    "method",
    "dataset",
    "metric",
    "result",
    "limitation",
    "gap",
    "relevance",
    "source",
)
_SOTA_FIELDS = ("task", "dataset", "metric", "score", "method", "source", "split_policy", "notes")


def record_literature_finding(run_id: str, **fields: Any) -> int:
    with session_scope() as s:
        row = LiteratureFinding(run_id=run_id, **{k: fields.get(k) for k in _LIT_FIELDS})
        s.add(row)
        s.flush()
        return row.id


def list_literature_findings(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (
            s.query(LiteratureFinding)
            .filter(LiteratureFinding.run_id == run_id)
            .order_by(LiteratureFinding.id)
            .all()
        )
        return [{"id": r.id, **{k: getattr(r, k) for k in _LIT_FIELDS}} for r in rows]


def record_scorecard(
    run_id: str,
    *,
    experiment_id: str | None,
    scores: dict[str, Any],
    decision: str,
    rationale: str | None = None,
) -> str:
    with session_scope() as s:
        row = HypothesisScorecard(
            run_id=run_id,
            experiment_id=experiment_id,
            scores=scores,
            decision=decision,
            rationale=rationale,
        )
        s.add(row)
        s.flush()
        return row.id


def list_scorecards(run_id: str, experiment_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(HypothesisScorecard).filter(HypothesisScorecard.run_id == run_id)
        if experiment_id is not None:
            q = q.filter(HypothesisScorecard.experiment_id == experiment_id)
        rows = q.order_by(HypothesisScorecard.created_at).all()
        return [
            {
                "id": r.id,
                "experiment_id": r.experiment_id,
                "scores": r.scores,
                "decision": r.decision,
                "rationale": r.rationale,
            }
            for r in rows
        ]


def record_sota_result(run_id: str, domain: str | None, **fields: Any) -> int:
    with session_scope() as s:
        score = fields.get("score")
        row = SOTAResult(
            run_id=run_id,
            domain=domain,
            score=(float(score) if score is not None else None),
            **{k: fields.get(k) for k in _SOTA_FIELDS if k != "score"},
        )
        s.add(row)
        s.flush()
        return row.id


def list_sota_results(run_id: str, domain: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(SOTAResult).filter(SOTAResult.run_id == run_id)
        if domain is not None:
            q = q.filter(SOTAResult.domain == domain)
        rows = q.order_by(SOTAResult.id).all()
        return [
            {"id": r.id, "domain": r.domain, **{k: getattr(r, k) for k in _SOTA_FIELDS}}
            for r in rows
        ]
