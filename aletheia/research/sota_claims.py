"""Fail-closed consumption of an F8-S6 campaign by the manuscript claim ledger."""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.knowledge.schemas import KnowledgeModel
from aletheia.knowledge.sota_evaluation import (
    BenchmarkResultOutcome,
    SOTACampaignVerdict,
    SOTAEvaluationCampaign,
)


class SOTAWriteupDisposition(str, Enum):
    AUTHORIZED = "headline_authorized"
    NOT_DEMONSTRATED = "headline_not_demonstrated"
    BLOCKED = "headline_blocked"


class AuditableSOTAWriteupDecision(KnowledgeModel):
    schema_version: Literal[1] = 1
    campaign_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_protocol_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidate_result_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    comparison_row_sha256s: tuple[str, ...] = ()
    headline_metric: str = Field(min_length=1, max_length=1024)
    headline_score: float | None = None
    campaign_verdict: SOTACampaignVerdict | None = None
    disposition: SOTAWriteupDisposition
    headline_authorized: bool
    claim_status: Literal["supported", "refuted", "unverified"]
    claim_strength: Literal["weak", "moderate"]
    claim_text: str = Field(min_length=1, max_length=4096)
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _writeup_fields_are_fail_closed(self) -> "AuditableSOTAWriteupDecision":
        if self.headline_score is not None and not math.isfinite(self.headline_score):
            raise ValueError("SOTA write-up headline score must be finite")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", row_sha256) is None
            for row_sha256 in self.comparison_row_sha256s
        ):
            raise ValueError("SOTA write-up rows must be SHA-256 identities")
        if len(self.comparison_row_sha256s) != len(set(self.comparison_row_sha256s)):
            raise ValueError("SOTA write-up rows must be unique")
        if any(not reason.strip() for reason in self.reason_codes) or len(self.reason_codes) != len(
            set(self.reason_codes)
        ):
            raise ValueError("SOTA write-up reason codes must be unique and non-blank")
        authorized = self.disposition is SOTAWriteupDisposition.AUTHORIZED
        if self.headline_authorized != authorized:
            raise ValueError("SOTA write-up authorization bit contradicts disposition")
        has_campaign = self.campaign_sha256 is not None
        if has_campaign != bool(
            self.candidate_protocol_sha256
            and self.candidate_result_receipt_sha256
            and self.comparison_row_sha256s
            and self.campaign_verdict
        ):
            raise ValueError("SOTA write-up campaign evidence must be complete or absent")
        if authorized:
            if (
                self.campaign_sha256 is None
                or self.candidate_protocol_sha256 is None
                or self.candidate_result_receipt_sha256 is None
                or not self.comparison_row_sha256s
                or self.campaign_verdict is not SOTACampaignVerdict.CONFIRMED
                or self.claim_status != "supported"
                or self.claim_strength != "moderate"
                or self.reason_codes
            ):
                raise ValueError("authorized SOTA write-up lacks a confirmed campaign")
        elif not self.reason_codes:
            raise ValueError("blocked SOTA write-up requires a reason")
        elif self.disposition is SOTAWriteupDisposition.NOT_DEMONSTRATED:
            if (
                self.campaign_verdict is not SOTACampaignVerdict.NOT_DEMONSTRATED
                or self.claim_status != "refuted"
                or self.claim_strength != "weak"
            ):
                raise ValueError("unbeaten-reference evidence must refute the headline")
        elif self.claim_status != "unverified" or self.claim_strength != "weak":
            raise ValueError("blocked SOTA evidence must remain weak and unverified")
        return self

    @property
    def decision_sha256(self) -> str:
        from aletheia.reproducibility.manifest import content_sha256

        return content_sha256(self)


def _metric_matches(campaign: SOTAEvaluationCampaign, headline_metric: str) -> bool:
    metric = campaign.candidate_protocol.metric
    expected = headline_metric.strip().casefold()
    identities = {
        metric.metric_id.strip().casefold(),
        metric.canonical_name.strip().casefold(),
        *(alias.strip().casefold() for alias in metric.aliases),
    }
    return expected in identities


def blocked_sota_writeup_decision(
    *,
    headline_metric: str,
    headline_score: float | None,
    reason_code: str,
) -> AuditableSOTAWriteupDecision:
    return AuditableSOTAWriteupDecision(
        headline_metric=headline_metric,
        headline_score=headline_score,
        disposition=SOTAWriteupDisposition.BLOCKED,
        headline_authorized=False,
        claim_status="unverified",
        claim_strength="weak",
        claim_text=(
            "No auditable SOTA headline is permitted because the F8-S6 evidence binding "
            f"failed ({reason_code})."
        ),
        reason_codes=(reason_code,),
    )


def screen_auditable_sota_campaign(
    *,
    campaign: SOTAEvaluationCampaign,
    receipt_key: bytes,
    expected_candidate_protocol_sha256: str,
    headline_metric: str,
    headline_score: float | None,
    contribution_type: str = "performance",
) -> AuditableSOTAWriteupDecision:
    """Revalidate and bind a campaign before a SOTA claim can enter manuscript prose."""

    if not isinstance(campaign, SOTAEvaluationCampaign):
        raise TypeError("auditable SOTA provider did not return a campaign")
    campaign = SOTAEvaluationCampaign.model_validate(campaign.model_dump(mode="python"))
    for receipt in (campaign.candidate_result, *campaign.reference_results):
        receipt.verify(
            key=receipt_key,
            expected_key_id=campaign.evaluator_manifest.receipt_key_id,
        )

    reasons: list[str] = []
    if contribution_type != "performance":
        reasons.append(f"sota_irrelevant_to_contribution:{contribution_type}")
    if not expected_candidate_protocol_sha256:
        reasons.append("missing_candidate_protocol_identity")
    elif campaign.candidate_protocol.protocol_sha256 != expected_candidate_protocol_sha256:
        reasons.append("candidate_protocol_identity_mismatch")
    if not _metric_matches(campaign, headline_metric):
        reasons.append("headline_metric_identity_mismatch")
    payload = campaign.candidate_result.payload
    if payload.outcome is not BenchmarkResultOutcome.SUCCESS:
        reasons.append("candidate_result_error")
    elif headline_score is None or not math.isfinite(headline_score):
        reasons.append("missing_or_nonfinite_headline_score")
    elif payload.aggregate_score is None or not math.isclose(
        payload.aggregate_score,
        headline_score,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        reasons.append("headline_score_receipt_mismatch")

    common = {
        "campaign_sha256": campaign.campaign_sha256,
        "candidate_protocol_sha256": campaign.candidate_protocol.protocol_sha256,
        "candidate_result_receipt_sha256": campaign.candidate_result.receipt_sha256,
        "comparison_row_sha256s": tuple(row.row_sha256 for row in campaign.rows),
        "headline_metric": headline_metric,
        "headline_score": headline_score,
        "campaign_verdict": campaign.verdict,
    }
    if reasons:
        reason_codes = tuple(reasons)
        return AuditableSOTAWriteupDecision(
            **common,
            disposition=SOTAWriteupDisposition.BLOCKED,
            headline_authorized=False,
            claim_status="unverified",
            claim_strength="weak",
            claim_text=(
                "No auditable SOTA headline is permitted because the frozen campaign does not "
                f"bind to this manuscript result ({', '.join(reason_codes)})."
            ),
            reason_codes=reason_codes,
        )
    if campaign.verdict is SOTACampaignVerdict.BLOCKED_EVIDENCE:
        reason_codes = tuple(campaign.blockers) or ("campaign_evidence_blocked",)
        return AuditableSOTAWriteupDecision(
            **common,
            disposition=SOTAWriteupDisposition.BLOCKED,
            headline_authorized=False,
            claim_status="unverified",
            claim_strength="weak",
            claim_text=(
                "No auditable SOTA headline is permitted because at least one pre-sealed "
                "reference comparison lacks comparable, successful evidence."
            ),
            reason_codes=reason_codes,
        )
    if (
        campaign.verdict is SOTACampaignVerdict.NOT_DEMONSTRATED
        or not campaign.headline_sota_allowed
    ):
        reason_codes = tuple(campaign.blockers) or ("campaign_not_superior",)
        return AuditableSOTAWriteupDecision(
            **common,
            disposition=SOTAWriteupDisposition.NOT_DEMONSTRATED,
            headline_authorized=False,
            claim_status="refuted",
            claim_strength="weak",
            claim_text=(
                f"The frozen {headline_metric} comparison did not demonstrate superiority over "
                f"all {len(campaign.registry.references)} pre-sealed references."
            ),
            reason_codes=reason_codes,
        )
    return AuditableSOTAWriteupDecision(
        **common,
        disposition=SOTAWriteupDisposition.AUTHORIZED,
        headline_authorized=True,
        claim_status="supported",
        claim_strength="moderate",
        claim_text=(
            f"The signed paired evaluation at {headline_metric}={headline_score} beat all "
            f"{len(campaign.registry.references)} pre-sealed comparable references under the "
            "frozen exact-sign, Holm-corrected, practical-improvement policy."
        ),
        reason_codes=(),
    )


__all__ = [
    "AuditableSOTAWriteupDecision",
    "SOTAWriteupDisposition",
    "blocked_sota_writeup_decision",
    "screen_auditable_sota_campaign",
]
