from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture

from .f9s2_fixtures import StepClock, build_f9s2_fixture, digest
from .f9s3_fixtures import build_f9s3_fixture
from .f9s7_fixtures import build_f9s7_fixture


def build_f9s8_source(root: Path) -> e.CausalAuditCampaign:
    live = asyncio.run(build_f8s5_live_fixture(root / "f8", novelty_kind="strong"))
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(
        gate,
        run_id=digest(f"f9s8:{root.resolve()}")[:32],
    )
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s8:source-hypotheses",
            direction_gate=hypotheses["gate"],
            policy=hypotheses["policy"],
            request=hypotheses["request"],
            generator=hypotheses["generator"],
            deduplicator=hypotheses["deduplicator"],
            clock=hypotheses["clock"],
        )
    )
    causal = build_f9s3_fixture(hypothesis_campaign)
    campaign = asyncio.run(
        e.run_causal_identification_audit(
            campaign_id="campaign:f9s8:source-causal-audit",
            source_campaign=causal["source_campaign"],
            policy=causal["policy"],
            request=causal["request"],
            author=causal["author"],
            reviewer=causal["reviewer"],
            clock=causal["clock"],
        )
    )
    assert campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    return campaign


def build_f9s8_fixture(
    source_campaign: e.CausalAuditCampaign,
    root: Path,
    *,
    terminal_action: e.K3TerminalAction = e.K3TerminalAction.CONTINUE_RESEARCH,
    update_policy_updates: dict[str, object] | None = None,
) -> dict[str, Any]:
    parts = build_f9s7_fixture(
        source_campaign,
        root / "f9s7",
        terminal_action=terminal_action,
        update_policy_updates=update_policy_updates,
    )
    request = parts["acceptance_request"]
    acceptance = e.run_k3_acceptance(
        campaign_id="campaign:f9s8:k3-acceptance",
        policy=parts["acceptance_policy"],
        scorer_manifest=parts["scorer_manifest"],
        request=request,
        selection_archive=parts["selection_archive"],
        validation_archive=parts["validation_archive"],
        update_archive=parts["update_archive"],
        evidence_archive=parts["evidence_archive"],
        clock=StepClock(request.issued_at + timedelta(minutes=1)),
    )
    acceptance_archive = ContentAddressedResponseArchive(root / "acceptance-archive")
    committed_acceptance = e.commit_k3_acceptance_campaign(
        archive=acceptance_archive,
        campaign=acceptance,
        committed_at=acceptance.generated_at + timedelta(minutes=1),
    )
    transition = e.build_world_model_transition(
        transition_id=(
            "f9s8-world-model-transition-"
            f"{parts['round_evidence'].committed_updates[0].campaign.updated_world_model_snapshot.question.run_id}"
        ),
        round_evidence=parts["round_evidence"],
        revision_materializations=parts["revision_materializations"],
        persistence_principal_sha256=(parts["evidence_ledger"].persistence_principal_sha256),
        persisted_at=parts["evidence_ledger"].persisted_at,
    )
    return {
        **parts,
        "acceptance": acceptance,
        "acceptance_archive": acceptance_archive,
        "committed_acceptance": committed_acceptance,
        "transition": transition,
    }


__all__ = ["build_f9s8_fixture", "build_f9s8_source"]
