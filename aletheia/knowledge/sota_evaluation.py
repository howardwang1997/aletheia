"""F8-S6 audited protocol-safe SOTA evaluation and headline suppression."""

from __future__ import annotations

import hashlib
import hmac
import math
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.novelty_decision import ResearchDirectionGate
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    ComparabilityStatus,
    KnowledgeModel,
    MetricDirection,
    ProtocolSignature,
    SOTAComparison,
    build_sota_comparison,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


SOTA_REPLICATE_AGGREGATION_POLICY_SHA256 = content_sha256(
    {
        "policy": "f8s6-arithmetic-mean-over-frozen-paired-replicates-v1",
        "finite_scores_required": True,
        "replicate_order": "ordinal_then_id",
    }
)
SOTA_STATISTICAL_POLICY_SHA256 = content_sha256(
    {
        "policy": "f8s6-exact-one-sided-paired-sign-test-holm-v1",
        "null_win_probability": 0.5,
        "ties": "excluded_from_sign_test_but_retained_in_audit",
        "tie_absolute_tolerance": 1e-12,
        "multiple_comparisons": "holm_step_down",
        "superiority": "adjusted_p_at_most_alpha_and_mean_favorable_delta_at_least_margin",
    }
)


class BenchmarkResultOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class BenchmarkReplicateScore(KnowledgeModel):
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=100_000)
    replicate_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    evaluation_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _score_is_finite(self) -> "BenchmarkReplicateScore":
        if not math.isfinite(self.score):
            raise ValueError("benchmark replicate score must be finite")
        return self

    @property
    def replicate_sha256(self) -> str:
        return content_sha256(self)


class SOTAEvaluatorManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    evaluator_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    evaluator_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score_parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregation_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimum_replicates: int = Field(default=10, ge=10, le=10_000)
    receipt_key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_uses_exact_unprivileged_policies(self) -> "SOTAEvaluatorManifest":
        if (
            self.aggregation_policy_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256
            or self.statistical_policy_sha256 != SOTA_STATISTICAL_POLICY_SHA256
        ):
            raise ValueError("SOTA evaluator uses another aggregation/statistical policy")
        if self.tool_names:
            raise ValueError("SOTA evaluator cannot receive tool authority")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class BenchmarkResultPayload(KnowledgeModel):
    schema_version: Literal[1] = 1
    result_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: BenchmarkResultOutcome
    replicates: tuple[BenchmarkReplicateScore, ...] = ()
    aggregate_score: float | None = None
    failure_class: str | None = Field(default=None, max_length=256)
    failure_detail_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _result_is_complete_or_explicit_error(self) -> "BenchmarkResultPayload":
        ordinals = [replicate.ordinal for replicate in self.replicates]
        ids = [replicate.replicate_id for replicate in self.replicates]
        partitions = [replicate.evaluation_partition_sha256 for replicate in self.replicates]
        if ordinals and ordinals != list(range(len(ordinals))):
            raise ValueError("benchmark replicate ordinals must be contiguous")
        if len(ids) != len(set(ids)) or len(partitions) != len(set(partitions)):
            raise ValueError("benchmark replicate IDs and partitions must be unique")
        if self.outcome is BenchmarkResultOutcome.SUCCESS:
            expected = (
                math.fsum(replicate.score for replicate in self.replicates) / len(self.replicates)
                if self.replicates
                else None
            )
            if (
                not self.replicates
                or self.aggregate_score is None
                or not math.isfinite(self.aggregate_score)
                or self.aggregate_score != expected
                or self.failure_class is not None
                or self.failure_detail_sha256 is not None
            ):
                raise ValueError("successful benchmark result is not exactly aggregated")
        elif (
            self.replicates
            or self.aggregate_score is not None
            or not self.failure_class
            or self.failure_detail_sha256 is None
        ):
            raise ValueError("failed benchmark result requires hashed failure and no scores")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


class SignedBenchmarkResultReceipt(KnowledgeModel):
    schema_version: Literal[1] = 1
    payload: BenchmarkResultPayload
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _message(payload: BenchmarkResultPayload, key_id: str) -> bytes:
        return b"aletheia-f8s6-benchmark-result-v1\0" + canonical_json_bytes(
            {
                "key_id": key_id,
                "payload": payload.model_dump(mode="json", exclude_none=True),
            }
        )

    @classmethod
    def sign(
        cls,
        *,
        payload: BenchmarkResultPayload,
        key_id: str,
        key: bytes,
    ) -> "SignedBenchmarkResultReceipt":
        if len(key) < 32:
            raise ValueError("SOTA result signing key must contain at least 32 bytes")
        signature = hmac.new(
            key,
            cls._message(payload, key_id),
            hashlib.sha256,
        ).hexdigest()
        return cls(payload=payload, key_id=key_id, hmac_sha256=signature)

    def verify(self, *, key: bytes, expected_key_id: str) -> None:
        if len(key) < 32:
            raise ValueError("SOTA result signing key must contain at least 32 bytes")
        if self.key_id != expected_key_id:
            raise ValueError("SOTA result receipt uses another evaluator key")
        expected = hmac.new(
            key,
            self._message(self.payload, self.key_id),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, self.hmac_sha256):
            raise ValueError("SOTA result receipt signature is invalid")

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class SOTAReferenceKind(str, Enum):
    OFFICIAL_LEADERBOARD = "official_leaderboard"
    PEER_REVIEWED_RESULT = "peer_reviewed_result"
    STRONG_BASELINE = "strong_baseline"


class SOTAReferenceEntry(KnowledgeModel):
    schema_version: Literal[1] = 1
    reference_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    kind: SOTAReferenceKind
    protocol: ProtocolSignature
    source_paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_evidence_span_sha256s: tuple[str, ...] = Field(min_length=1)
    selection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_author_excluded: Literal[True] = True
    selected_at: AwareDatetime
    required: Literal[True] = True
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _entry_has_unique_source_evidence(self) -> "SOTAReferenceEntry":
        if len(self.result_evidence_span_sha256s) != len(set(self.result_evidence_span_sha256s)):
            raise ValueError("SOTA reference evidence spans must be unique")
        if self.selected_at < self.protocol.evaluation_date:
            raise ValueError("SOTA reference cannot be selected before its reported evaluation")
        return self

    @property
    def entry_sha256(self) -> str:
        return content_sha256(self)


class SOTAReferenceRegistry(KnowledgeModel):
    schema_version: Literal[1] = 1
    registry_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    direction_gate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_search_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_author_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    selector_reviewer_principal_sha256s: tuple[str, ...] = Field(min_length=2)
    references: tuple[SOTAReferenceEntry, ...] = Field(min_length=3)
    evidence_cutoff: AwareDatetime
    sealed_at: AwareDatetime
    state: Literal["sealed"] = "sealed"

    @model_validator(mode="after")
    def _registry_is_complete_canonical_and_author_excluded(
        self,
    ) -> "SOTAReferenceRegistry":
        for identities, name in (
            (self.candidate_author_principal_sha256s, "candidate authors"),
            (self.selector_reviewer_principal_sha256s, "reference selectors"),
        ):
            if identities != tuple(sorted(set(identities))):
                raise ValueError(f"SOTA {name} must be unique and sorted")
        if set(self.candidate_author_principal_sha256s) & set(
            self.selector_reviewer_principal_sha256s
        ):
            raise ValueError("candidate authors cannot select their SOTA references")
        expected = tuple(sorted(self.references, key=lambda reference: reference.reference_id))
        if self.references != expected:
            raise ValueError("SOTA references must use canonical reference-ID order")
        identity_groups = (
            [reference.reference_id for reference in self.references],
            [reference.entry_sha256 for reference in self.references],
            [reference.protocol.protocol_sha256 for reference in self.references],
            [reference.source_paper_snapshot_sha256 for reference in self.references],
            [reference.review_receipt_sha256 for reference in self.references],
        )
        if any(len(values) != len(set(values)) for values in identity_groups):
            raise ValueError("SOTA registry references require unique identities")
        if any(
            reference.independent_reviewer_principal_sha256
            in self.candidate_author_principal_sha256s
            for reference in self.references
        ):
            raise ValueError("candidate authors cannot review SOTA reference inclusion")
        if any(
            reference.protocol.evaluation_date > self.evidence_cutoff
            or reference.selected_at > self.sealed_at
            for reference in self.references
        ):
            raise ValueError("SOTA registry includes future or post-sealing evidence")
        if self.sealed_at < self.evidence_cutoff:
            raise ValueError("SOTA registry cannot seal before its evidence cutoff")
        return self

    @property
    def registry_sha256(self) -> str:
        return content_sha256(self)


class SOTAComparisonPolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    minimum_references: int = Field(default=3, ge=3, le=100)
    minimum_replicates: int = Field(default=10, ge=10, le=10_000)
    alpha: Literal[0.05] = 0.05
    test: Literal["exact_one_sided_paired_sign"] = "exact_one_sided_paired_sign"
    multiple_comparisons: Literal["holm_step_down"] = "holm_step_down"
    minimum_practical_improvement: float = Field(gt=0.0)
    require_every_reference_comparable: Literal[True] = True
    require_every_reference_successful: Literal[True] = True
    require_every_reference_beaten: Literal[True] = True
    aggregation_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_is_exact_and_finite(self) -> "SOTAComparisonPolicy":
        if (
            self.aggregation_policy_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256
            or self.statistical_policy_sha256 != SOTA_STATISTICAL_POLICY_SHA256
        ):
            raise ValueError("SOTA comparison policy uses another derivation")
        if not math.isfinite(self.minimum_practical_improvement):
            raise ValueError("SOTA practical improvement must be finite")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class SOTARowConclusion(str, Enum):
    BEATS_REFERENCE = "beats_reference"
    DOES_NOT_BEAT_REFERENCE = "does_not_beat_reference"
    NON_COMPARABLE = "non_comparable"
    RESULT_ERROR = "result_error"


class AuditedSOTAComparisonRow(KnowledgeModel):
    schema_version: Literal[1] = 1
    reference_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparison: SOTAComparison | None = None
    wins: int | None = Field(default=None, ge=0)
    losses: int | None = Field(default=None, ge=0)
    ties: int | None = Field(default=None, ge=0)
    one_sided_p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    holm_adjusted_p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    mean_favorable_delta: float | None = None
    statistically_significant: bool | None = None
    practically_significant: bool | None = None
    conclusion: SOTARowConclusion

    @model_validator(mode="after")
    def _row_shape_matches_conclusion(self) -> "AuditedSOTAComparisonRow":
        statistical = self.conclusion in {
            SOTARowConclusion.BEATS_REFERENCE,
            SOTARowConclusion.DOES_NOT_BEAT_REFERENCE,
        }
        fields = (
            self.wins,
            self.losses,
            self.ties,
            self.one_sided_p_value,
            self.holm_adjusted_p_value,
            self.mean_favorable_delta,
            self.statistically_significant,
            self.practically_significant,
        )
        if statistical != all(value is not None for value in fields):
            raise ValueError("SOTA row statistical fields do not match its conclusion")
        if self.conclusion is SOTARowConclusion.RESULT_ERROR:
            if self.comparison is not None:
                raise ValueError("failed SOTA result cannot contain a comparison")
        elif self.comparison is None:
            raise ValueError("non-error SOTA row requires a protocol comparison")
        elif self.conclusion is SOTARowConclusion.NON_COMPARABLE and (
            self.comparison.comparability.status is not ComparabilityStatus.NON_COMPARABLE
        ):
            raise ValueError("non-comparable SOTA row requires blocking protocol mismatch")
        return self

    @property
    def row_sha256(self) -> str:
        return content_sha256(self)


class SOTACampaignVerdict(str, Enum):
    CONFIRMED = "sota_confirmed"
    NOT_DEMONSTRATED = "sota_not_demonstrated"
    BLOCKED_EVIDENCE = "sota_blocked_evidence"


class SOTAClaimCeiling(str, Enum):
    NONE = "none"
    COMPARATIVE_ONLY = "comparative_only"
    MODERATE = "moderate"


class SOTAEvaluationCampaign(KnowledgeModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    direction_gate: ResearchDirectionGate
    registry: SOTAReferenceRegistry
    policy: SOTAComparisonPolicy
    evaluator_manifest: SOTAEvaluatorManifest
    candidate_protocol: ProtocolSignature
    candidate_result: SignedBenchmarkResultReceipt
    reference_results: tuple[SignedBenchmarkResultReceipt, ...]
    rows: tuple[AuditedSOTAComparisonRow, ...]
    verdict: SOTACampaignVerdict
    blockers: tuple[str, ...]
    headline_sota_allowed: bool
    claim_ceiling: SOTAClaimCeiling
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_complete_and_rederived(self) -> "SOTAEvaluationCampaign":
        _validate_campaign_bindings(
            direction_gate=self.direction_gate,
            registry=self.registry,
            policy=self.policy,
            evaluator_manifest=self.evaluator_manifest,
            candidate_protocol=self.candidate_protocol,
            candidate_result=self.candidate_result,
            reference_results=self.reference_results,
            generated_at=self.generated_at,
        )
        expected_rows = _derive_rows(
            registry=self.registry,
            policy=self.policy,
            candidate_protocol=self.candidate_protocol,
            candidate_result=self.candidate_result,
            reference_results=self.reference_results,
            generated_at=self.generated_at,
        )
        if self.rows != expected_rows:
            raise ValueError("SOTA matrix rows are not derived from exact protocols/results")
        verdict, blockers, headline, ceiling = _derive_campaign_decision(expected_rows)
        if (
            self.verdict is not verdict
            or self.blockers != blockers
            or self.headline_sota_allowed != headline
            or self.claim_ceiling is not ceiling
        ):
            raise ValueError("SOTA campaign decision/headline is not mechanically derived")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedSOTAEvaluationCampaign(KnowledgeModel):
    schema_version: Literal[1] = 1
    campaign: SOTAEvaluationCampaign
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedSOTAEvaluationCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("SOTA evaluation ledger does not commit its campaign")
        return self


def issue_benchmark_result_receipt(
    *,
    result_id: str,
    protocol: ProtocolSignature,
    replicates: tuple[BenchmarkReplicateScore, ...],
    evaluator_manifest: SOTAEvaluatorManifest,
    receipt_key: bytes,
    completed_at: AwareDatetime,
) -> SignedBenchmarkResultReceipt:
    if protocol.metric.aggregation_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256:
        raise ValueError("benchmark protocol metric uses another replicate aggregation")
    if len(replicates) < evaluator_manifest.minimum_replicates:
        raise ValueError("benchmark result has too few frozen replicates")
    if completed_at < max(protocol.evaluation_date, evaluator_manifest.frozen_at):
        raise ValueError("benchmark result predates its protocol/evaluator")
    payload = BenchmarkResultPayload(
        result_id=result_id,
        protocol_sha256=protocol.protocol_sha256,
        metric_sha256=protocol.metric.metric_sha256,
        evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
        outcome=BenchmarkResultOutcome.SUCCESS,
        replicates=replicates,
        aggregate_score=(math.fsum(replicate.score for replicate in replicates) / len(replicates)),
        completed_at=completed_at,
    )
    return SignedBenchmarkResultReceipt.sign(
        payload=payload,
        key_id=evaluator_manifest.receipt_key_id,
        key=receipt_key,
    )


def issue_failed_benchmark_result_receipt(
    *,
    result_id: str,
    protocol: ProtocolSignature,
    evaluator_manifest: SOTAEvaluatorManifest,
    receipt_key: bytes,
    failure: Exception,
    completed_at: AwareDatetime,
) -> SignedBenchmarkResultReceipt:
    if protocol.metric.aggregation_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256:
        raise ValueError("benchmark protocol metric uses another replicate aggregation")
    if completed_at < max(protocol.evaluation_date, evaluator_manifest.frozen_at):
        raise ValueError("failed benchmark result predates its protocol/evaluator")
    failure_class = f"{type(failure).__module__}.{type(failure).__qualname__}"[:256]
    detail = content_sha256(
        {
            "failure_class": failure_class,
            "message_sha256": hashlib.sha256(
                str(failure).encode("utf-8", errors="replace")
            ).hexdigest(),
        }
    )
    payload = BenchmarkResultPayload(
        result_id=result_id,
        protocol_sha256=protocol.protocol_sha256,
        metric_sha256=protocol.metric.metric_sha256,
        evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
        outcome=BenchmarkResultOutcome.ERROR,
        failure_class=failure_class,
        failure_detail_sha256=detail,
        completed_at=completed_at,
    )
    return SignedBenchmarkResultReceipt.sign(
        payload=payload,
        key_id=evaluator_manifest.receipt_key_id,
        key=receipt_key,
    )


def _validate_reference_source_closure(
    *,
    direction_gate: ResearchDirectionGate,
    references: tuple[SOTAReferenceEntry, ...],
) -> None:
    corpus = direction_gate.novelty_decision.coverage.ingestion_bundle.corpus
    paper_hashes = {paper.snapshot_sha256 for paper in corpus.papers}
    spans_by_paper: dict[str, set[str]] = {paper_sha256: set() for paper_sha256 in paper_hashes}
    for span in corpus.spans:
        spans_by_paper[span.paper_snapshot_sha256].add(span.span_sha256)
    for reference in references:
        if reference.source_paper_snapshot_sha256 not in paper_hashes:
            raise ValueError("SOTA reference source paper lies outside the bound corpus")
        if not set(reference.result_evidence_span_sha256s).issubset(
            spans_by_paper[reference.source_paper_snapshot_sha256]
        ):
            raise ValueError(
                "SOTA reference result evidence span lies outside its bound source paper"
            )


def build_sota_reference_registry(
    *,
    registry_id: str,
    direction_gate: ResearchDirectionGate,
    selection_protocol_sha256: str,
    selector_reviewer_principal_sha256s: tuple[str, ...],
    references: tuple[SOTAReferenceEntry, ...],
    evidence_cutoff: AwareDatetime,
    sealed_at: AwareDatetime,
) -> SOTAReferenceRegistry:
    if not direction_gate.experiment_authorized:
        raise ValueError("SOTA registry requires an authorized research direction")
    _validate_reference_source_closure(
        direction_gate=direction_gate,
        references=references,
    )
    decision = direction_gate.novelty_decision
    coverage = decision.coverage
    return SOTAReferenceRegistry(
        registry_id=registry_id,
        direction_gate_sha256=direction_gate.gate_sha256,
        knowledge_coverage_sha256=coverage.coverage_sha256,
        reference_search_session_sha256=(
            coverage.search_assessment.aggregate_session.session_sha256
        ),
        corpus_snapshot_sha256=coverage.ingestion_bundle.corpus.snapshot_sha256,
        selection_protocol_sha256=selection_protocol_sha256,
        candidate_author_principal_sha256s=(decision.authorship_manifest.author_principal_sha256s),
        selector_reviewer_principal_sha256s=tuple(sorted(selector_reviewer_principal_sha256s)),
        references=references,
        evidence_cutoff=evidence_cutoff,
        sealed_at=sealed_at,
    )


def _validate_result_binding(
    *,
    receipt: SignedBenchmarkResultReceipt,
    protocol: ProtocolSignature,
    evaluator_manifest: SOTAEvaluatorManifest,
    policy: SOTAComparisonPolicy,
) -> None:
    payload = receipt.payload
    if (
        protocol.metric.aggregation_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256
        or payload.protocol_sha256 != protocol.protocol_sha256
        or payload.metric_sha256 != protocol.metric.metric_sha256
        or payload.evaluator_manifest_sha256 != evaluator_manifest.manifest_sha256
        or receipt.key_id != evaluator_manifest.receipt_key_id
    ):
        raise ValueError("benchmark result receipt is bound to another protocol/evaluator")
    if payload.outcome is BenchmarkResultOutcome.SUCCESS:
        if len(payload.replicates) < policy.minimum_replicates:
            raise ValueError("benchmark result has too few policy replicates")
        for replicate in payload.replicates:
            if (
                protocol.metric.valid_minimum is not None
                and replicate.score < protocol.metric.valid_minimum
            ) or (
                protocol.metric.valid_maximum is not None
                and replicate.score > protocol.metric.valid_maximum
            ):
                raise ValueError("benchmark replicate lies outside the metric range")


def _validate_campaign_bindings(
    *,
    direction_gate: ResearchDirectionGate,
    registry: SOTAReferenceRegistry,
    policy: SOTAComparisonPolicy,
    evaluator_manifest: SOTAEvaluatorManifest,
    candidate_protocol: ProtocolSignature,
    candidate_result: SignedBenchmarkResultReceipt,
    reference_results: tuple[SignedBenchmarkResultReceipt, ...],
    generated_at: AwareDatetime,
) -> None:
    decision = direction_gate.novelty_decision
    coverage = decision.coverage
    if not direction_gate.experiment_authorized:
        raise ValueError("SOTA campaign requires an authorized research direction")
    if (
        registry.direction_gate_sha256 != direction_gate.gate_sha256
        or registry.knowledge_coverage_sha256 != coverage.coverage_sha256
        or registry.reference_search_session_sha256
        != coverage.search_assessment.aggregate_session.session_sha256
        or registry.corpus_snapshot_sha256 != coverage.ingestion_bundle.corpus.snapshot_sha256
        or registry.candidate_author_principal_sha256s
        != decision.authorship_manifest.author_principal_sha256s
    ):
        raise ValueError("SOTA registry is bound to another direction/knowledge search")
    _validate_reference_source_closure(
        direction_gate=direction_gate,
        references=registry.references,
    )
    if len(registry.references) < policy.minimum_references:
        raise ValueError("SOTA registry is below the frozen reference floor")
    if (
        direction_gate.decided_at > registry.sealed_at
        or evaluator_manifest.frozen_at > registry.sealed_at
        or registry.sealed_at > policy.frozen_at
        or evaluator_manifest.frozen_at > policy.frozen_at
        or policy.frozen_at > candidate_protocol.frozen_at
        or candidate_protocol.frozen_at > candidate_protocol.evaluation_date
    ):
        raise ValueError("SOTA registry/policy/protocol freeze order is invalid")
    if (
        policy.evaluator_manifest_sha256 != evaluator_manifest.manifest_sha256
        or policy.minimum_replicates < evaluator_manifest.minimum_replicates
        or candidate_protocol.metric.aggregation_sha256 != SOTA_REPLICATE_AGGREGATION_POLICY_SHA256
    ):
        raise ValueError("SOTA policy/evaluator/candidate aggregation binding is invalid")
    if len(reference_results) != len(registry.references):
        raise ValueError("SOTA campaign requires one result for every sealed reference")
    _validate_result_binding(
        receipt=candidate_result,
        protocol=candidate_protocol,
        evaluator_manifest=evaluator_manifest,
        policy=policy,
    )
    for entry, receipt in zip(registry.references, reference_results, strict=True):
        _validate_result_binding(
            receipt=receipt,
            protocol=entry.protocol,
            evaluator_manifest=evaluator_manifest,
            policy=policy,
        )
    receipts = (candidate_result, *reference_results)
    result_ids = [receipt.payload.result_id for receipt in receipts]
    receipt_hashes = [receipt.receipt_sha256 for receipt in receipts]
    if len(result_ids) != len(set(result_ids)) or len(receipt_hashes) != len(set(receipt_hashes)):
        raise ValueError("SOTA campaign result IDs/receipts must be unique")
    successful = [
        receipt.payload
        for receipt in receipts
        if receipt.payload.outcome is BenchmarkResultOutcome.SUCCESS
    ]
    if successful:
        replicate_keys = tuple(
            (item.replicate_id, item.evaluation_partition_sha256)
            for item in successful[0].replicates
        )
        if any(
            tuple(
                (item.replicate_id, item.evaluation_partition_sha256) for item in payload.replicates
            )
            != replicate_keys
            for payload in successful[1:]
        ):
            raise ValueError("SOTA results do not use the same paired frozen replicates")
        executions = [
            replicate.execution_receipt_sha256
            for payload in successful
            for replicate in payload.replicates
        ]
        predictions = [
            replicate.prediction_artifact_sha256
            for payload in successful
            for replicate in payload.replicates
        ]
        if len(executions) != len(set(executions)) or len(predictions) != len(set(predictions)):
            raise ValueError("SOTA methods cannot reuse execution/prediction artifacts")
    if generated_at < max(receipt.payload.completed_at for receipt in receipts):
        raise ValueError("SOTA campaign predates a result receipt")


def _one_sided_sign_p_value(wins: int, losses: int) -> float:
    trials = wins + losses
    if trials == 0:
        return 1.0

    def lower_binomial_sum(maximum: int) -> int:
        if maximum < 0:
            return 0
        term = 1
        total = term
        for k in range(1, maximum + 1):
            term = term * (trials - k + 1) // k
            total += term
        return total

    denominator = 1 << trials
    if wins > trials // 2:
        # Symmetry turns the upper tail into a short exact integer lower tail and avoids
        # converting enormous individual combinations to float.
        return lower_binomial_sum(trials - wins) / denominator
    lower_excluded = lower_binomial_sum(wins - 1)
    return (denominator - lower_excluded) / denominator


def _holm_adjust(p_values: list[tuple[int, float]]) -> dict[int, float]:
    ordered = sorted(p_values, key=lambda item: (item[1], item[0]))
    adjusted: dict[int, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[index] = running
    return adjusted


def _derive_rows(
    *,
    registry: SOTAReferenceRegistry,
    policy: SOTAComparisonPolicy,
    candidate_protocol: ProtocolSignature,
    candidate_result: SignedBenchmarkResultReceipt,
    reference_results: tuple[SignedBenchmarkResultReceipt, ...],
    generated_at: AwareDatetime,
) -> tuple[AuditedSOTAComparisonRow, ...]:
    preliminary: list[dict[str, object]] = []
    p_values: list[tuple[int, float]] = []
    candidate_payload = candidate_result.payload
    for index, (entry, receipt) in enumerate(
        zip(registry.references, reference_results, strict=True)
    ):
        if (
            candidate_payload.outcome is BenchmarkResultOutcome.ERROR
            or receipt.payload.outcome is BenchmarkResultOutcome.ERROR
        ):
            preliminary.append(
                {
                    "entry": entry,
                    "receipt": receipt,
                    "conclusion": SOTARowConclusion.RESULT_ERROR,
                }
            )
            continue
        comparison = build_sota_comparison(
            comparison_id=f"sota:{entry.reference_id}",
            candidate=candidate_protocol,
            reference=entry.protocol,
            candidate_score=candidate_payload.aggregate_score,
            reference_score=receipt.payload.aggregate_score,
            assessed_at=generated_at,
            generated_at=generated_at,
        )
        if comparison.comparability.status is ComparabilityStatus.NON_COMPARABLE:
            preliminary.append(
                {
                    "entry": entry,
                    "receipt": receipt,
                    "comparison": comparison,
                    "conclusion": SOTARowConclusion.NON_COMPARABLE,
                }
            )
            continue
        favorable_differences = tuple(
            (
                candidate.score - reference.score
                if candidate_protocol.metric.direction is MetricDirection.HIGHER_IS_BETTER
                else reference.score - candidate.score
            )
            for candidate, reference in zip(
                candidate_payload.replicates,
                receipt.payload.replicates,
                strict=True,
            )
        )
        wins = sum(delta > 1e-12 for delta in favorable_differences)
        losses = sum(delta < -1e-12 for delta in favorable_differences)
        ties = len(favorable_differences) - wins - losses
        p_value = _one_sided_sign_p_value(wins, losses)
        p_values.append((index, p_value))
        preliminary.append(
            {
                "entry": entry,
                "receipt": receipt,
                "comparison": comparison,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "p_value": p_value,
                "mean": comparison.favorable_delta,
            }
        )
    adjusted = _holm_adjust(p_values)
    rows: list[AuditedSOTAComparisonRow] = []
    for index, item in enumerate(preliminary):
        entry = item["entry"]
        receipt = item["receipt"]
        conclusion = item.get("conclusion")
        if conclusion is not None:
            rows.append(
                AuditedSOTAComparisonRow(
                    reference_entry_sha256=entry.entry_sha256,
                    result_receipt_sha256=receipt.receipt_sha256,
                    comparison=item.get("comparison"),
                    conclusion=conclusion,
                )
            )
            continue
        statistically_significant = adjusted[index] <= policy.alpha
        practically_significant = item["mean"] >= policy.minimum_practical_improvement
        beats = statistically_significant and practically_significant
        rows.append(
            AuditedSOTAComparisonRow(
                reference_entry_sha256=entry.entry_sha256,
                result_receipt_sha256=receipt.receipt_sha256,
                comparison=item["comparison"],
                wins=item["wins"],
                losses=item["losses"],
                ties=item["ties"],
                one_sided_p_value=item["p_value"],
                holm_adjusted_p_value=adjusted[index],
                mean_favorable_delta=item["mean"],
                statistically_significant=statistically_significant,
                practically_significant=practically_significant,
                conclusion=(
                    SOTARowConclusion.BEATS_REFERENCE
                    if beats
                    else SOTARowConclusion.DOES_NOT_BEAT_REFERENCE
                ),
            )
        )
    return tuple(rows)


def _derive_campaign_decision(
    rows: tuple[AuditedSOTAComparisonRow, ...],
) -> tuple[SOTACampaignVerdict, tuple[str, ...], bool, SOTAClaimCeiling]:
    evidence_blockers: list[str] = []
    performance_blockers: list[str] = []
    for row in rows:
        if row.conclusion is SOTARowConclusion.RESULT_ERROR:
            evidence_blockers.append(f"result_error:{row.reference_entry_sha256}")
        elif row.conclusion is SOTARowConclusion.NON_COMPARABLE:
            dimensions = ",".join(
                mismatch.dimension.value
                for mismatch in row.comparison.comparability.mismatches
                if mismatch.blocking
            )
            evidence_blockers.append(f"non_comparable:{row.reference_entry_sha256}:{dimensions}")
        elif row.conclusion is SOTARowConclusion.DOES_NOT_BEAT_REFERENCE:
            performance_blockers.append(f"not_superior:{row.reference_entry_sha256}")
    blockers = tuple((*evidence_blockers, *performance_blockers))
    if evidence_blockers:
        return (
            SOTACampaignVerdict.BLOCKED_EVIDENCE,
            blockers,
            False,
            SOTAClaimCeiling.NONE,
        )
    if performance_blockers:
        return (
            SOTACampaignVerdict.NOT_DEMONSTRATED,
            blockers,
            False,
            SOTAClaimCeiling.COMPARATIVE_ONLY,
        )
    return (
        SOTACampaignVerdict.CONFIRMED,
        (),
        True,
        SOTAClaimCeiling.MODERATE,
    )


def build_sota_evaluation_campaign(
    *,
    campaign_id: str,
    direction_gate: ResearchDirectionGate,
    registry: SOTAReferenceRegistry,
    policy: SOTAComparisonPolicy,
    evaluator_manifest: SOTAEvaluatorManifest,
    candidate_protocol: ProtocolSignature,
    candidate_result: SignedBenchmarkResultReceipt,
    reference_results: tuple[SignedBenchmarkResultReceipt, ...],
    receipt_key: bytes,
    generated_at: AwareDatetime,
) -> SOTAEvaluationCampaign:
    for receipt in (candidate_result, *reference_results):
        receipt.verify(
            key=receipt_key,
            expected_key_id=evaluator_manifest.receipt_key_id,
        )
    _validate_campaign_bindings(
        direction_gate=direction_gate,
        registry=registry,
        policy=policy,
        evaluator_manifest=evaluator_manifest,
        candidate_protocol=candidate_protocol,
        candidate_result=candidate_result,
        reference_results=reference_results,
        generated_at=generated_at,
    )
    rows = _derive_rows(
        registry=registry,
        policy=policy,
        candidate_protocol=candidate_protocol,
        candidate_result=candidate_result,
        reference_results=reference_results,
        generated_at=generated_at,
    )
    verdict, blockers, headline, ceiling = _derive_campaign_decision(rows)
    return SOTAEvaluationCampaign(
        campaign_id=campaign_id,
        direction_gate=direction_gate,
        registry=registry,
        policy=policy,
        evaluator_manifest=evaluator_manifest,
        candidate_protocol=candidate_protocol,
        candidate_result=candidate_result,
        reference_results=reference_results,
        rows=rows,
        verdict=verdict,
        blockers=blockers,
        headline_sota_allowed=headline,
        claim_ceiling=ceiling,
        generated_at=generated_at,
    )


def commit_sota_evaluation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: SOTAEvaluationCampaign,
) -> CommittedSOTAEvaluationCampaign:
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=campaign.generated_at,
    )
    return CommittedSOTAEvaluationCampaign(campaign=campaign, ledger=ledger)


def load_sota_evaluation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
    receipt_key: bytes,
) -> SOTAEvaluationCampaign:
    payload = archive.read_ledger(ledger)
    campaign = SOTAEvaluationCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived SOTA campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived SOTA campaign changed object identity")
    for receipt in (
        campaign.candidate_result,
        *campaign.reference_results,
    ):
        receipt.verify(
            key=receipt_key,
            expected_key_id=campaign.evaluator_manifest.receipt_key_id,
        )
    return campaign


__all__ = [
    "SOTA_REPLICATE_AGGREGATION_POLICY_SHA256",
    "SOTA_STATISTICAL_POLICY_SHA256",
    "AuditedSOTAComparisonRow",
    "BenchmarkReplicateScore",
    "BenchmarkResultOutcome",
    "BenchmarkResultPayload",
    "CommittedSOTAEvaluationCampaign",
    "SOTACampaignVerdict",
    "SOTAClaimCeiling",
    "SOTAComparisonPolicy",
    "SOTAEvaluationCampaign",
    "SOTAEvaluatorManifest",
    "SOTAReferenceEntry",
    "SOTAReferenceKind",
    "SOTAReferenceRegistry",
    "SOTARowConclusion",
    "SignedBenchmarkResultReceipt",
    "build_sota_evaluation_campaign",
    "build_sota_reference_registry",
    "commit_sota_evaluation_campaign",
    "issue_benchmark_result_receipt",
    "issue_failed_benchmark_result_receipt",
    "load_sota_evaluation_campaign",
]
