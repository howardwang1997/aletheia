"""F9-S2 F8-grounded competing-hypothesis generation.

Models may propose candidates, but the harness owns admission.  It preserves every raw draft,
requires an independent complete pairwise semantic ledger, removes duplicates only through an
explicit mapping, and proves pairwise observable discrimination before emitting an immutable
F9-S1 world-model snapshot.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.persistence import WorldModelStoreReceipt, store_world_model_snapshot
from aletheia.epistemics.schemas import (
    Assumption,
    AssumptionKind,
    BeliefState,
    BeliefUpdateKind,
    EpistemicModel,
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
from aletheia.knowledge.novelty_decision import ResearchDirectionGate
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import AtomicClaim, ClaimOrigin, PriorArtRelation
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_QUESTION_ID_PATTERN = r"^rq_[0-9a-f]{32}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"

SEMANTIC_NORMALIZER_SHA256 = hashlib.sha256(
    b"aletheia.f9s2.nfkc-casefold-alnum-whitespace-v1"
).hexdigest()


class HypothesisGeneratorRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class SemanticHypothesisRelation(str, Enum):
    DISTINCT = "distinct"
    EQUIVALENT = "equivalent"
    UNCERTAIN = "uncertain"


class DuplicateDisposition(str, Enum):
    KEPT = "kept"
    DUPLICATE = "duplicate"


class HypothesisGenerationFailureKind(str, Enum):
    GENERATOR_ERROR = "generator_error"
    GENERATOR_OUTPUT_INVALID = "generator_output_invalid"
    DEDUPLICATOR_ERROR = "deduplicator_error"
    DEDUPLICATOR_OUTPUT_INVALID = "deduplicator_output_invalid"


class HypothesisGenerationDisposition(str, Enum):
    READY = "ready_for_world_model"
    BLOCKED_GENERATION = "blocked_generation"
    BLOCKED_GROUNDING = "blocked_grounding"
    BLOCKED_DUPLICATES = "blocked_duplicate_resolution"
    BLOCKED_DISCRIMINATION = "blocked_discrimination"


class QuestionDraft(EpistemicModel):
    statement: str = Field(min_length=1, max_length=8192)
    kind: ResearchQuestionKind

    @model_validator(mode="after")
    def _statement_is_not_blank(self) -> "QuestionDraft":
        if not self.statement.strip():
            raise ValueError("research-question draft cannot be blank")
        return self


class AssumptionDraft(EpistemicModel):
    local_assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: AssumptionKind
    statement: str = Field(min_length=1, max_length=8192)
    risk_if_violated: str = Field(min_length=1, max_length=8192)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _grounding_is_canonical(self) -> "AssumptionDraft":
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("assumption grounding claims must be unique and sorted")
        if any(not re.fullmatch(_SHA256_PATTERN, item) for item in self.grounding_claim_sha256s):
            raise ValueError("assumption grounding claim identity must be a SHA-256")
        if not self.statement.strip() or not self.risk_if_violated.strip():
            raise ValueError("assumption statement and violation risk cannot be blank")
        return self

    @property
    def draft_sha256(self) -> str:
        return content_sha256(self)


class PredictionDraft(EpistemicModel):
    local_prediction_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    observable_id: str = Field(min_length=1, max_length=512)
    outcome_space: tuple[str, ...] = Field(min_length=2, max_length=128)
    expected_outcome: str = Field(min_length=1, max_length=2048)
    direction: PredictionDirection
    discriminates_from_local_hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=63)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _prediction_is_canonical(self) -> "PredictionDraft":
        outcomes = tuple(item.strip() for item in self.outcome_space)
        if any(not item for item in outcomes) or len(outcomes) != len(set(outcomes)):
            raise ValueError("prediction outcome space must contain unique non-blank labels")
        if self.expected_outcome not in outcomes:
            raise ValueError("prediction expected outcome must belong to its outcome space")
        targets = self.discriminates_from_local_hypothesis_ids
        if targets != tuple(sorted(set(targets))):
            raise ValueError("prediction discrimination targets must be unique and sorted")
        if any(not re.fullmatch(_LOCAL_ID_PATTERN, item) for item in targets):
            raise ValueError("prediction discrimination target has an invalid local ID")
        return self

    @property
    def draft_sha256(self) -> str:
        return content_sha256(self)


class HypothesisDraft(EpistemicModel):
    local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    role: HypothesisRole
    statement: str = Field(min_length=1, max_length=8192)
    mechanism: str | None = Field(default=None, max_length=16384)
    rationale_sha256: str = Field(pattern=_SHA256_PATTERN)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    assumptions: tuple[AssumptionDraft, ...] = Field(min_length=1, max_length=64)
    predictions: tuple[PredictionDraft, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _draft_is_canonical(self) -> "HypothesisDraft":
        if not self.statement.strip():
            raise ValueError("hypothesis draft statement cannot be blank")
        if self.mechanism is not None and not self.mechanism.strip():
            raise ValueError("hypothesis draft mechanism cannot be blank")
        if self.role is not HypothesisRole.NULL and self.mechanism is None:
            raise ValueError("primary and alternative drafts require an explicit mechanism")
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("hypothesis grounding claims must be unique and sorted")
        if any(not re.fullmatch(_SHA256_PATTERN, item) for item in self.grounding_claim_sha256s):
            raise ValueError("hypothesis grounding claim identity must be a SHA-256")
        assumption_order = [item.local_assumption_id for item in self.assumptions]
        prediction_order = [item.local_prediction_id for item in self.predictions]
        if assumption_order != sorted(set(assumption_order)):
            raise ValueError("hypothesis assumptions must have unique canonical local IDs")
        if prediction_order != sorted(set(prediction_order)):
            raise ValueError("hypothesis predictions must have unique canonical local IDs")
        if any(
            self.local_hypothesis_id in prediction.discriminates_from_local_hypothesis_ids
            for prediction in self.predictions
        ):
            raise ValueError("a hypothesis prediction cannot discriminate from itself")
        return self

    @property
    def draft_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGenerationBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    generator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    question: QuestionDraft
    hypotheses: tuple[HypothesisDraft, ...] = Field(min_length=3, max_length=64)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _batch_is_complete_and_canonical(self) -> "HypothesisGenerationBatch":
        identities = [item.local_hypothesis_id for item in self.hypotheses]
        if identities != sorted(set(identities)):
            raise ValueError("generated hypotheses must have unique canonical local IDs")
        roles = [item.role for item in self.hypotheses]
        if roles.count(HypothesisRole.NULL) != 1 or roles.count(HypothesisRole.PRIMARY) != 1:
            raise ValueError("generation requires exactly one null and one primary hypothesis")
        if roles.count(HypothesisRole.ALTERNATIVE) < 1:
            raise ValueError("generation requires at least one alternative hypothesis")
        known = set(identities)
        unknown_targets = {
            target
            for hypothesis in self.hypotheses
            for prediction in hypothesis.predictions
            for target in prediction.discriminates_from_local_hypothesis_ids
            if target not in known
        }
        if unknown_targets:
            raise ValueError("prediction references a hypothesis outside the generation batch")
        assumption_ids = [
            item.local_assumption_id
            for hypothesis in self.hypotheses
            for item in hypothesis.assumptions
        ]
        prediction_ids = [
            item.local_prediction_id
            for hypothesis in self.hypotheses
            for item in hypothesis.predictions
        ]
        if len(assumption_ids) != len(set(assumption_ids)):
            raise ValueError("generation batch cannot reuse an assumption local ID")
        if len(prediction_ids) != len(set(prediction_ids)):
            raise ValueError("generation batch cannot reuse a prediction local ID")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256 = content_sha256(
    HypothesisGenerationBatch.model_json_schema()
)


class HypothesisGeneratorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    generator_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    runtime: HypothesisGeneratorRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    generator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    maximum_hypotheses: int = Field(ge=3, le=64)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_is_exact_and_unprivileged(self) -> "HypothesisGeneratorManifest":
        if self.output_schema_sha256 != HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256:
            raise ValueError("hypothesis generator uses another output schema")
        if self.tool_names:
            raise ValueError("hypothesis generator cannot receive tool authority")
        model_fields = self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        if self.runtime is HypothesisGeneratorRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model generator requires frozen instruction/model and transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic generator cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class HypothesisPairJudgment(EpistemicModel):
    schema_version: Literal[1] = 1
    left_local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    right_local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    left_draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    right_draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    relation: SemanticHypothesisRelation
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _pair_is_canonical(self) -> "HypothesisPairJudgment":
        if self.left_local_hypothesis_id >= self.right_local_hypothesis_id:
            raise ValueError("semantic hypothesis pair must use canonical local-ID order")
        return self

    @property
    def judgment_sha256(self) -> str:
        return content_sha256(self)


class HypothesisDeduplicationBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    generation_batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    deduplicator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    judgments: tuple[HypothesisPairJudgment, ...] = Field(min_length=3)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _judgments_are_unique_and_canonical(self) -> "HypothesisDeduplicationBatch":
        pairs = [
            (item.left_local_hypothesis_id, item.right_local_hypothesis_id)
            for item in self.judgments
        ]
        if pairs != sorted(set(pairs)):
            raise ValueError("semantic hypothesis judgments must have unique canonical pair order")
        if self.completed_at < max(item.completed_at for item in self.judgments):
            raise ValueError("deduplication batch predates a pair judgment")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256 = content_sha256(
    HypothesisDeduplicationBatch.model_json_schema()
)


class HypothesisDeduplicatorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    deduplicator_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    runtime: HypothesisGeneratorRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reviewer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_normalizer_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_is_exact_and_unprivileged(self) -> "HypothesisDeduplicatorManifest":
        if self.output_schema_sha256 != HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256:
            raise ValueError("hypothesis deduplicator uses another output schema")
        if self.semantic_normalizer_sha256 != SEMANTIC_NORMALIZER_SHA256:
            raise ValueError("hypothesis deduplicator uses another exact-duplicate normalizer")
        if self.tool_names:
            raise ValueError("hypothesis deduplicator cannot receive tool authority")
        model_fields = self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        if self.runtime is HypothesisGeneratorRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model deduplicator requires frozen instruction/model and transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic deduplicator cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGenerationPolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    minimum_hypotheses: int = Field(default=3, ge=3, le=64)
    maximum_hypotheses: int = Field(default=16, ge=3, le=64)
    minimum_distinct_alternatives: int = Field(default=1, ge=1, le=62)
    minimum_semantic_judgment_confidence: float = Field(default=0.8, gt=0.5, le=1.0)
    require_alternative_prior_art_grounding: Literal[True] = True
    require_complete_pairwise_discrimination: Literal[True] = True
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "HypothesisGenerationPolicy":
        if self.maximum_hypotheses < self.minimum_hypotheses:
            raise ValueError("hypothesis-generation policy range is reversed")
        if self.minimum_distinct_alternatives > self.maximum_hypotheses - 2:
            raise ValueError("alternative floor exceeds the hypothesis capacity")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGenerationRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    question_id: str = Field(pattern=_QUESTION_ID_PATTERN)
    direction_gate_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_claim_sha256: str = Field(pattern=_SHA256_PATTERN)
    corpus_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    claim_graph_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    claim_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_art_resolution_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_claim_sha256s: tuple[str, ...] = Field(min_length=2)
    accepted_prior_art_relation_sha256s: tuple[str, ...] = Field(min_length=1)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    generator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    deduplicator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["none"] = "none"

    @model_validator(mode="after")
    def _evidence_identities_are_canonical(self) -> "HypothesisGenerationRequest":
        if self.input_claim_sha256s != tuple(sorted(set(self.input_claim_sha256s))):
            raise ValueError("generation input claims must be unique and sorted")
        relations = self.accepted_prior_art_relation_sha256s
        if len(relations) != len(set(relations)):
            raise ValueError("generation input prior-art relations must be unique")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class DuplicateResolution(EpistemicModel):
    local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: DuplicateDisposition
    canonical_local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    supporting_judgment_sha256s: tuple[str, ...]

    @property
    def resolution_sha256(self) -> str:
        return content_sha256(self)


class DiscriminationEdge(EpistemicModel):
    left_local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    right_local_hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    left_prediction_draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    right_prediction_draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    observable_id: str = Field(min_length=1, max_length=512)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _edge_is_canonical(self) -> "DiscriminationEdge":
        if self.left_local_hypothesis_id >= self.right_local_hypothesis_id:
            raise ValueError("discrimination edge must use canonical hypothesis order")
        return self

    @property
    def edge_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGenerationFailure(EpistemicModel):
    kind: HypothesisGenerationFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGenerationCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    direction_gate: ResearchDirectionGate
    policy: HypothesisGenerationPolicy
    generator_manifest: HypothesisGeneratorManifest
    deduplicator_manifest: HypothesisDeduplicatorManifest
    request: HypothesisGenerationRequest
    generation_batch: HypothesisGenerationBatch | None = None
    deduplication_batch: HypothesisDeduplicationBatch | None = None
    failure: HypothesisGenerationFailure | None = None
    duplicate_resolutions: tuple[DuplicateResolution, ...]
    discrimination_edges: tuple[DiscriminationEdge, ...]
    blockers: tuple[str, ...]
    disposition: HypothesisGenerationDisposition
    world_model_snapshot: WorldModelSnapshot | None = None
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "HypothesisGenerationCampaign":
        _validate_request_bindings(
            gate=self.direction_gate,
            policy=self.policy,
            generator_manifest=self.generator_manifest,
            deduplicator_manifest=self.deduplicator_manifest,
            request=self.request,
        )
        if self.failure is not None:
            if self.deduplication_batch is not None or self.world_model_snapshot is not None:
                raise ValueError("failed generation cannot contain a deduplication/world model")
            generator_failure = self.failure.kind in {
                HypothesisGenerationFailureKind.GENERATOR_ERROR,
                HypothesisGenerationFailureKind.GENERATOR_OUTPUT_INVALID,
            }
            if generator_failure != (self.generation_batch is None):
                raise ValueError("generation failure stage does not match retained batch")
            if not generator_failure:
                assert self.generation_batch is not None
                _validate_generation_batch(
                    batch=self.generation_batch,
                    request=self.request,
                    manifest=self.generator_manifest,
                    policy=self.policy,
                )
                if self.failure.occurred_at < self.generation_batch.completed_at:
                    raise ValueError("deduplicator failure predates retained generation output")
            expected_blockers = (f"generation_failure:{self.failure.kind.value}",)
            if (
                self.duplicate_resolutions
                or self.discrimination_edges
                or self.blockers != expected_blockers
                or self.disposition is not HypothesisGenerationDisposition.BLOCKED_GENERATION
            ):
                raise ValueError("failed hypothesis campaign decision is not derived")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("hypothesis campaign predates its failure")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("hypothesis-generation failure predates its request")
            return self
        if self.generation_batch is None or self.deduplication_batch is None:
            raise ValueError("successful generation requires complete generation and deduplication")
        _validate_generation_batch(
            batch=self.generation_batch,
            request=self.request,
            manifest=self.generator_manifest,
            policy=self.policy,
        )
        _validate_deduplication_batch(
            batch=self.deduplication_batch,
            generation_batch=self.generation_batch,
            manifest=self.deduplicator_manifest,
        )
        derived = _derive_campaign_outputs(
            gate=self.direction_gate,
            policy=self.policy,
            generator_manifest=self.generator_manifest,
            request=self.request,
            generation_batch=self.generation_batch,
            deduplication_batch=self.deduplication_batch,
            generated_at=self.generated_at,
        )
        if (
            self.duplicate_resolutions != derived.duplicate_resolutions
            or self.discrimination_edges != derived.discrimination_edges
            or self.blockers != derived.blockers
            or self.disposition is not derived.disposition
            or self.world_model_snapshot != derived.world_model_snapshot
        ):
            raise ValueError("hypothesis campaign outputs are not mechanically derived")
        if self.generated_at < self.deduplication_batch.completed_at:
            raise ValueError("hypothesis campaign predates deduplication")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedHypothesisGenerationCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: HypothesisGenerationCampaign
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedHypothesisGenerationCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("hypothesis-generation ledger does not commit its campaign")
        return self


class CompetingHypothesisGeneratorAdapter(Protocol):
    @property
    def manifest(self) -> HypothesisGeneratorManifest: ...

    async def generate(
        self,
        *,
        request: HypothesisGenerationRequest,
        candidate_claim: AtomicClaim,
        prior_claims: tuple[AtomicClaim, ...],
        accepted_prior_art_relations: tuple[PriorArtRelation, ...],
    ) -> object: ...


class HypothesisSemanticDeduplicatorAdapter(Protocol):
    @property
    def manifest(self) -> HypothesisDeduplicatorManifest: ...

    async def compare(
        self,
        *,
        generation_batch: HypothesisGenerationBatch,
    ) -> object: ...


class _DerivedCampaignOutputs(EpistemicModel):
    duplicate_resolutions: tuple[DuplicateResolution, ...]
    discrimination_edges: tuple[DiscriminationEdge, ...]
    blockers: tuple[str, ...]
    disposition: HypothesisGenerationDisposition
    world_model_snapshot: WorldModelSnapshot | None


def _scope_sha256(gate: ResearchDirectionGate) -> str:
    coverage = gate.novelty_decision.coverage
    return content_sha256(
        {
            "policy": "aletheia.f9s2.f8-grounded-question-scope-v1",
            "direction_gate_sha256": gate.gate_sha256,
            "candidate_claim_sha256": (
                gate.novelty_decision.evidence_package.candidate_claim_sha256
            ),
            "corpus_snapshot_sha256": coverage.ingestion_bundle.corpus.snapshot_sha256,
            "claim_graph_bundle_sha256": coverage.claim_graph_bundle.bundle_sha256,
            "prior_art_resolution_sha256": coverage.prior_art_resolution.resolution_sha256,
        }
    )


def _validate_independent_manifests(
    generator: HypothesisGeneratorManifest,
    deduplicator: HypothesisDeduplicatorManifest,
) -> None:
    if generator.generator_principal_sha256 == deduplicator.reviewer_principal_sha256:
        raise ValueError("hypothesis generator and semantic deduplicator must be independent")
    if (
        generator.model_identity_sha256 is not None
        and generator.model_identity_sha256 == deduplicator.model_identity_sha256
    ):
        raise ValueError("hypothesis generator and semantic deduplicator cannot use one model")


def _validate_request_bindings(
    *,
    gate: ResearchDirectionGate,
    policy: HypothesisGenerationPolicy,
    generator_manifest: HypothesisGeneratorManifest,
    deduplicator_manifest: HypothesisDeduplicatorManifest,
    request: HypothesisGenerationRequest,
) -> None:
    if not gate.experiment_authorized:
        raise ValueError("F9 hypothesis generation requires an authorized F8 direction")
    _validate_independent_manifests(generator_manifest, deduplicator_manifest)
    if policy.maximum_hypotheses > generator_manifest.maximum_hypotheses:
        raise ValueError("generation policy exceeds the frozen generator capacity")
    for frozen_at, label in (
        (policy.frozen_at, "generation policy"),
        (generator_manifest.frozen_at, "generator manifest"),
        (deduplicator_manifest.frozen_at, "deduplicator manifest"),
        (gate.decided_at, "F8 direction gate"),
    ):
        if frozen_at > request.issued_at:
            raise ValueError(f"{label} must freeze before the generation request")

    decision = gate.novelty_decision
    coverage = decision.coverage
    graph_bundle = coverage.claim_graph_bundle
    graph = graph_bundle.graph
    resolution = coverage.prior_art_resolution
    candidate_sha256 = decision.evidence_package.candidate_claim_sha256
    claims = {claim.claim_sha256: claim for claim in graph.claims}
    candidate = claims.get(candidate_sha256)
    if candidate is None or candidate.origin is not ClaimOrigin.CANDIDATE:
        raise ValueError("F8 direction candidate is absent from its exact claim graph")
    expected_relations = tuple(item.relation.relation_sha256 for item in resolution.accepted)
    expected = {
        "direction_gate_sha256": gate.gate_sha256,
        "candidate_claim_sha256": candidate_sha256,
        "corpus_snapshot_sha256": coverage.ingestion_bundle.corpus.snapshot_sha256,
        "claim_graph_bundle_sha256": graph_bundle.bundle_sha256,
        "claim_graph_sha256": graph.graph_sha256,
        "prior_art_resolution_sha256": resolution.resolution_sha256,
        "input_claim_sha256s": tuple(sorted(claims)),
        "accepted_prior_art_relation_sha256s": expected_relations,
        "scope_sha256": _scope_sha256(gate),
        "generator_manifest_sha256": generator_manifest.manifest_sha256,
        "deduplicator_manifest_sha256": deduplicator_manifest.manifest_sha256,
        "policy_sha256": policy.policy_sha256,
    }
    for field, value in expected.items():
        if getattr(request, field) != value:
            raise ValueError(f"hypothesis-generation request changed exact {field}")


def build_hypothesis_generation_request(
    *,
    request_id: str,
    run_id: str,
    question_id: str,
    direction_gate: ResearchDirectionGate,
    policy: HypothesisGenerationPolicy,
    generator_manifest: HypothesisGeneratorManifest,
    deduplicator_manifest: HypothesisDeduplicatorManifest,
    issued_at: AwareDatetime,
) -> HypothesisGenerationRequest:
    coverage = direction_gate.novelty_decision.coverage
    request = HypothesisGenerationRequest(
        request_id=request_id,
        run_id=run_id,
        question_id=question_id,
        direction_gate_sha256=direction_gate.gate_sha256,
        candidate_claim_sha256=(
            direction_gate.novelty_decision.evidence_package.candidate_claim_sha256
        ),
        corpus_snapshot_sha256=coverage.ingestion_bundle.corpus.snapshot_sha256,
        claim_graph_bundle_sha256=coverage.claim_graph_bundle.bundle_sha256,
        claim_graph_sha256=coverage.claim_graph_bundle.graph.graph_sha256,
        prior_art_resolution_sha256=coverage.prior_art_resolution.resolution_sha256,
        input_claim_sha256s=tuple(
            sorted(claim.claim_sha256 for claim in coverage.claim_graph_bundle.graph.claims)
        ),
        accepted_prior_art_relation_sha256s=tuple(
            item.relation.relation_sha256 for item in coverage.prior_art_resolution.accepted
        ),
        scope_sha256=_scope_sha256(direction_gate),
        generator_manifest_sha256=generator_manifest.manifest_sha256,
        deduplicator_manifest_sha256=deduplicator_manifest.manifest_sha256,
        policy_sha256=policy.policy_sha256,
        issued_at=issued_at,
    )
    _validate_request_bindings(
        gate=direction_gate,
        policy=policy,
        generator_manifest=generator_manifest,
        deduplicator_manifest=deduplicator_manifest,
        request=request,
    )
    return request


def _validate_generation_batch(
    *,
    batch: HypothesisGenerationBatch,
    request: HypothesisGenerationRequest,
    manifest: HypothesisGeneratorManifest,
    policy: HypothesisGenerationPolicy,
    received_at: datetime | None = None,
) -> None:
    if (
        batch.request_sha256 != request.request_sha256
        or batch.generator_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("hypothesis generation output is bound to another request/generator")
    if batch.completed_at < request.issued_at:
        raise ValueError("hypothesis generation output predates its request")
    if received_at is not None and batch.completed_at > received_at:
        raise ValueError("hypothesis generation output claims a future completion time")
    if not (
        policy.minimum_hypotheses
        <= len(batch.hypotheses)
        <= min(policy.maximum_hypotheses, manifest.maximum_hypotheses)
    ):
        raise ValueError("hypothesis generation output violates the frozen candidate-count policy")


def _validate_deduplication_batch(
    *,
    batch: HypothesisDeduplicationBatch,
    generation_batch: HypothesisGenerationBatch,
    manifest: HypothesisDeduplicatorManifest,
    received_at: datetime | None = None,
) -> None:
    if (
        batch.generation_batch_sha256 != generation_batch.batch_sha256
        or batch.deduplicator_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("semantic deduplication is bound to another generation/manifest")
    if batch.completed_at < generation_batch.completed_at:
        raise ValueError("semantic deduplication predates hypothesis generation")
    if received_at is not None and batch.completed_at > received_at:
        raise ValueError("semantic deduplication claims a future completion time")
    drafts = {item.local_hypothesis_id: item for item in generation_batch.hypotheses}
    expected_pairs = list(combinations(sorted(drafts), 2))
    actual_pairs = [
        (item.left_local_hypothesis_id, item.right_local_hypothesis_id)
        for item in batch.judgments
    ]
    if actual_pairs != expected_pairs:
        raise ValueError("semantic deduplicator must judge every hypothesis pair exactly once")
    for judgment in batch.judgments:
        left = drafts[judgment.left_local_hypothesis_id]
        right = drafts[judgment.right_local_hypothesis_id]
        if (
            judgment.left_draft_sha256 != left.draft_sha256
            or judgment.right_draft_sha256 != right.draft_sha256
        ):
            raise ValueError("semantic judgment changed a hypothesis draft identity")
        if judgment.completed_at < generation_batch.completed_at:
            raise ValueError("semantic judgment predates hypothesis generation")


def _semantic_signature(draft: HypothesisDraft) -> str:
    value = f"{draft.statement}\n{draft.mechanism or ''}"
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _deduplicate_hypotheses(
    *,
    generation_batch: HypothesisGenerationBatch,
    deduplication_batch: HypothesisDeduplicationBatch,
    policy: HypothesisGenerationPolicy,
) -> tuple[tuple[DuplicateResolution, ...], tuple[str, ...]]:
    drafts = {item.local_hypothesis_id: item for item in generation_batch.hypotheses}
    pair_map = {
        (item.left_local_hypothesis_id, item.right_local_hypothesis_id): item
        for item in deduplication_batch.judgments
    }
    parent = {identity: identity for identity in drafts}

    def find(identity: str) -> str:
        while parent[identity] != identity:
            parent[identity] = parent[parent[identity]]
            identity = parent[identity]
        return identity

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    blockers: list[str] = []
    for pair, judgment in pair_map.items():
        left, right = (drafts[item] for item in pair)
        exact_duplicate = _semantic_signature(left) == _semantic_signature(right)
        confident = judgment.confidence >= policy.minimum_semantic_judgment_confidence
        if judgment.relation is SemanticHypothesisRelation.UNCERTAIN or not confident:
            blockers.append(f"semantic_pair_unresolved:{pair[0]}:{pair[1]}")
            continue
        if exact_duplicate and judgment.relation is not SemanticHypothesisRelation.EQUIVALENT:
            blockers.append(f"deduplicator_contradicts_exact_duplicate:{pair[0]}:{pair[1]}")
            continue
        if judgment.relation is SemanticHypothesisRelation.EQUIVALENT:
            union(*pair)

    components: dict[str, list[str]] = {}
    for identity in drafts:
        components.setdefault(find(identity), []).append(identity)
    for component in components.values():
        if len(component) < 2:
            continue
        roles = {drafts[item].role for item in component}
        if len(roles) != 1:
            blockers.append(f"semantic_duplicate_crosses_roles:{':'.join(sorted(component))}")
        for pair in combinations(sorted(component), 2):
            judgment = pair_map[pair]
            if (
                judgment.relation is not SemanticHypothesisRelation.EQUIVALENT
                or judgment.confidence < policy.minimum_semantic_judgment_confidence
            ):
                blockers.append(f"semantic_equivalence_nontransitive:{pair[0]}:{pair[1]}")
    if blockers:
        return (), tuple(dict.fromkeys(blockers))

    resolutions: list[DuplicateResolution] = []
    for component in sorted((sorted(value) for value in components.values()), key=lambda value: value[0]):
        canonical = component[0]
        for identity in component:
            supporting = ()
            if identity != canonical:
                pair = tuple(sorted((canonical, identity)))
                supporting = (pair_map[pair].judgment_sha256,)
            resolutions.append(
                DuplicateResolution(
                    local_hypothesis_id=identity,
                    draft_sha256=drafts[identity].draft_sha256,
                    disposition=(
                        DuplicateDisposition.KEPT
                        if identity == canonical
                        else DuplicateDisposition.DUPLICATE
                    ),
                    canonical_local_hypothesis_id=canonical,
                    supporting_judgment_sha256s=supporting,
                )
            )
    resolutions.sort(key=lambda item: item.local_hypothesis_id)
    return tuple(resolutions), ()


def _grounding_blockers(
    *,
    gate: ResearchDirectionGate,
    generation_batch: HypothesisGenerationBatch,
) -> tuple[str, ...]:
    graph = gate.novelty_decision.coverage.claim_graph_bundle.graph
    claims = {item.claim_sha256: item for item in graph.claims}
    candidate_sha256 = gate.novelty_decision.evidence_package.candidate_claim_sha256
    linked_prior_claims = {
        item.relation.prior_claim_sha256
        for item in gate.novelty_decision.coverage.prior_art_resolution.accepted
        if item.relation.candidate_claim_sha256 == candidate_sha256
    }
    blockers: list[str] = []
    if generation_batch.question.kind not in {
        ResearchQuestionKind.MECHANISM,
        ResearchQuestionKind.CAUSAL_EFFECT,
    }:
        blockers.append("question_is_not_mechanistic_or_causal")
    for draft in generation_batch.hypotheses:
        unknown = set(draft.grounding_claim_sha256s) - set(claims)
        unknown.update(
            claim_sha256
            for assumption in draft.assumptions
            for claim_sha256 in assumption.grounding_claim_sha256s
            if claim_sha256 not in claims
        )
        if unknown:
            blockers.append(f"unknown_grounding_claim:{draft.local_hypothesis_id}")
        if draft.role in {HypothesisRole.NULL, HypothesisRole.PRIMARY} and (
            candidate_sha256 not in draft.grounding_claim_sha256s
        ):
            blockers.append(f"candidate_claim_missing:{draft.local_hypothesis_id}")
        if draft.role is HypothesisRole.ALTERNATIVE and not (
            set(draft.grounding_claim_sha256s) & linked_prior_claims
        ):
            blockers.append(f"alternative_lacks_accepted_prior_art:{draft.local_hypothesis_id}")
    return tuple(dict.fromkeys(blockers))


def _discrimination_edges(
    *,
    generation_batch: HypothesisGenerationBatch,
    resolutions: tuple[DuplicateResolution, ...],
) -> tuple[tuple[DiscriminationEdge, ...], tuple[str, ...]]:
    drafts = {item.local_hypothesis_id: item for item in generation_batch.hypotheses}
    canonical_by_local = {
        item.local_hypothesis_id: item.canonical_local_hypothesis_id for item in resolutions
    }
    kept = {
        item.local_hypothesis_id: drafts[item.local_hypothesis_id]
        for item in resolutions
        if item.disposition is DuplicateDisposition.KEPT
    }

    def canonical_targets(prediction: PredictionDraft) -> set[str]:
        return {
            canonical_by_local[target]
            for target in prediction.discriminates_from_local_hypothesis_ids
            if target in canonical_by_local
        }

    edges: list[DiscriminationEdge] = []
    blockers: list[str] = []
    for left_id, right_id in combinations(sorted(kept), 2):
        left, right = kept[left_id], kept[right_id]
        witnesses: list[tuple[PredictionDraft, PredictionDraft]] = []
        for left_prediction in left.predictions:
            if right_id not in canonical_targets(left_prediction):
                continue
            for right_prediction in right.predictions:
                if left_id not in canonical_targets(right_prediction):
                    continue
                if (
                    left_prediction.observable_id == right_prediction.observable_id
                    and left_prediction.measurement_protocol_sha256
                    == right_prediction.measurement_protocol_sha256
                    and left_prediction.outcome_space == right_prediction.outcome_space
                    and left_prediction.expected_outcome != right_prediction.expected_outcome
                ):
                    witnesses.append((left_prediction, right_prediction))
        if not witnesses:
            blockers.append(f"no_pairwise_discriminating_prediction:{left_id}:{right_id}")
            continue
        left_prediction, right_prediction = min(
            witnesses, key=lambda pair: (pair[0].draft_sha256, pair[1].draft_sha256)
        )
        edges.append(
            DiscriminationEdge(
                left_local_hypothesis_id=left_id,
                right_local_hypothesis_id=right_id,
                left_prediction_draft_sha256=left_prediction.draft_sha256,
                right_prediction_draft_sha256=right_prediction.draft_sha256,
                observable_id=left_prediction.observable_id,
                measurement_protocol_sha256=left_prediction.measurement_protocol_sha256,
            )
        )
    return tuple(edges), tuple(blockers)


def _stable_lineage_id(prefix: str, request_sha256: str, local_id: str) -> str:
    digest = hashlib.sha256(f"{request_sha256}\0{local_id}".encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _build_world_model_snapshot(
    *,
    policy: HypothesisGenerationPolicy,
    generator_manifest: HypothesisGeneratorManifest,
    request: HypothesisGenerationRequest,
    generation_batch: HypothesisGenerationBatch,
    resolutions: tuple[DuplicateResolution, ...],
    discrimination_edges: tuple[DiscriminationEdge, ...],
    generated_at: AwareDatetime,
) -> WorldModelSnapshot:
    drafts = {item.local_hypothesis_id: item for item in generation_batch.hypotheses}
    canonical_by_local = {
        item.local_hypothesis_id: item.canonical_local_hypothesis_id for item in resolutions
    }
    kept_local_ids = {
        item.local_hypothesis_id
        for item in resolutions
        if item.disposition is DuplicateDisposition.KEPT
    }
    hypothesis_ids = {
        local_id: _stable_lineage_id("hyp", request.request_sha256, local_id)
        for local_id in kept_local_ids
    }
    question = ResearchQuestion(
        run_id=request.run_id,
        question_id=request.question_id,
        version=1,
        kind=generation_batch.question.kind,
        statement=generation_batch.question.statement,
        scope_sha256=request.scope_sha256,
        author_principal_sha256=generator_manifest.generator_principal_sha256,
        frozen_at=generation_batch.completed_at,
    )
    hypotheses = tuple(
        sorted(
            (
                HypothesisVersion(
                    run_id=request.run_id,
                    question_id=request.question_id,
                    question_version_sha256=question.question_sha256,
                    hypothesis_id=hypothesis_ids[local_id],
                    version=1,
                    role=drafts[local_id].role,
                    lifecycle=HypothesisLifecycle.ACTIVE,
                    statement=drafts[local_id].statement,
                    mechanism=drafts[local_id].mechanism,
                    rationale_sha256=drafts[local_id].rationale_sha256,
                    author_principal_sha256=generator_manifest.generator_principal_sha256,
                    frozen_at=generation_batch.completed_at,
                )
                for local_id in kept_local_ids
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    hypothesis_by_local = {
        local_id: next(item for item in hypotheses if item.hypothesis_id == hypothesis_ids[local_id])
        for local_id in kept_local_ids
    }
    assumptions = tuple(
        sorted(
            (
                Assumption(
                    run_id=request.run_id,
                    assumption_id=_stable_lineage_id(
                        "asm", request.request_sha256, assumption.local_assumption_id
                    ),
                    version=1,
                    hypothesis_id=hypothesis_ids[local_id],
                    hypothesis_version_sha256=hypothesis_by_local[
                        local_id
                    ].hypothesis_sha256,
                    kind=assumption.kind,
                    statement=assumption.statement,
                    risk_if_violated=assumption.risk_if_violated,
                    author_principal_sha256=generator_manifest.generator_principal_sha256,
                    frozen_at=generation_batch.completed_at,
                )
                for local_id in kept_local_ids
                for assumption in drafts[local_id].assumptions
            ),
            key=lambda item: item.assumption_id,
        )
    )
    witness_hashes = {
        identity
        for edge in discrimination_edges
        for identity in (
            edge.left_prediction_draft_sha256,
            edge.right_prediction_draft_sha256,
        )
    }
    predictions: list[Prediction] = []
    for local_id in kept_local_ids:
        for draft in drafts[local_id].predictions:
            if draft.draft_sha256 not in witness_hashes:
                continue
            canonical_targets = {
                canonical_by_local[target]
                for target in draft.discriminates_from_local_hypothesis_ids
                if target in canonical_by_local
            }
            canonical_targets.discard(local_id)
            stable_targets = tuple(
                sorted(hypothesis_ids[target] for target in canonical_targets if target in kept_local_ids)
            )
            if not stable_targets:
                continue
            predictions.append(
                Prediction(
                    run_id=request.run_id,
                    prediction_id=_stable_lineage_id(
                        "pred", request.request_sha256, draft.local_prediction_id
                    ),
                    version=1,
                    hypothesis_id=hypothesis_ids[local_id],
                    hypothesis_version_sha256=hypothesis_by_local[
                        local_id
                    ].hypothesis_sha256,
                    observable_id=draft.observable_id,
                    outcome_space=draft.outcome_space,
                    expected_outcome=draft.expected_outcome,
                    direction=draft.direction,
                    discriminates_from_hypothesis_ids=stable_targets,
                    measurement_protocol_sha256=draft.measurement_protocol_sha256,
                    author_principal_sha256=generator_manifest.generator_principal_sha256,
                    frozen_at=generation_batch.completed_at,
                )
            )
    predictions.sort(key=lambda item: item.prediction_id)
    probability = 1.0 / len(hypotheses)
    belief_state = BeliefState(
        run_id=request.run_id,
        belief_lineage_id=_stable_lineage_id("blf", request.request_sha256, "belief"),
        version=1,
        question_id=request.question_id,
        question_version_sha256=question.question_sha256,
        hypotheses=tuple(
            HypothesisBelief(
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                probability=probability,
            )
            for hypothesis in hypotheses
        ),
        update_kind=BeliefUpdateKind.PRIOR,
        author_principal_sha256=policy.harness_principal_sha256,
        frozen_at=generated_at,
    )
    return WorldModelSnapshot(
        question=question,
        hypotheses=hypotheses,
        assumptions=assumptions,
        predictions=tuple(predictions),
        belief_state=belief_state,
        frozen_at=generated_at,
    )


def _derive_campaign_outputs(
    *,
    gate: ResearchDirectionGate,
    policy: HypothesisGenerationPolicy,
    generator_manifest: HypothesisGeneratorManifest,
    request: HypothesisGenerationRequest,
    generation_batch: HypothesisGenerationBatch,
    deduplication_batch: HypothesisDeduplicationBatch,
    generated_at: AwareDatetime,
) -> _DerivedCampaignOutputs:
    resolutions, duplicate_blockers = _deduplicate_hypotheses(
        generation_batch=generation_batch,
        deduplication_batch=deduplication_batch,
        policy=policy,
    )
    grounding_blockers = _grounding_blockers(gate=gate, generation_batch=generation_batch)
    if duplicate_blockers:
        return _DerivedCampaignOutputs(
            duplicate_resolutions=(),
            discrimination_edges=(),
            blockers=duplicate_blockers + grounding_blockers,
            disposition=HypothesisGenerationDisposition.BLOCKED_DUPLICATES,
            world_model_snapshot=None,
        )
    kept = {
        item.local_hypothesis_id
        for item in resolutions
        if item.disposition is DuplicateDisposition.KEPT
    }
    alternatives = sum(
        drafts.role is HypothesisRole.ALTERNATIVE
        for drafts in generation_batch.hypotheses
        if drafts.local_hypothesis_id in kept
    )
    alternative_blockers = (
        ("insufficient_distinct_alternatives_after_deduplication",)
        if alternatives < policy.minimum_distinct_alternatives
        else ()
    )
    if alternative_blockers:
        return _DerivedCampaignOutputs(
            duplicate_resolutions=resolutions,
            discrimination_edges=(),
            blockers=alternative_blockers + grounding_blockers,
            disposition=HypothesisGenerationDisposition.BLOCKED_DUPLICATES,
            world_model_snapshot=None,
        )
    if grounding_blockers:
        return _DerivedCampaignOutputs(
            duplicate_resolutions=resolutions,
            discrimination_edges=(),
            blockers=grounding_blockers,
            disposition=HypothesisGenerationDisposition.BLOCKED_GROUNDING,
            world_model_snapshot=None,
        )
    edges, discrimination_blockers = _discrimination_edges(
        generation_batch=generation_batch,
        resolutions=resolutions,
    )
    if discrimination_blockers:
        return _DerivedCampaignOutputs(
            duplicate_resolutions=resolutions,
            discrimination_edges=edges,
            blockers=discrimination_blockers,
            disposition=HypothesisGenerationDisposition.BLOCKED_DISCRIMINATION,
            world_model_snapshot=None,
        )
    snapshot = _build_world_model_snapshot(
        policy=policy,
        generator_manifest=generator_manifest,
        request=request,
        generation_batch=generation_batch,
        resolutions=resolutions,
        discrimination_edges=edges,
        generated_at=generated_at,
    )
    return _DerivedCampaignOutputs(
        duplicate_resolutions=resolutions,
        discrimination_edges=edges,
        blockers=(),
        disposition=HypothesisGenerationDisposition.READY,
        world_model_snapshot=snapshot,
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("hypothesis-generation clock must return a timezone-aware datetime")
    return value


def _opaque_output_sha256(value: object) -> str:
    try:
        return content_sha256(value)
    except Exception:  # noqa: BLE001 - hash an opaque representation; never retain it
        return hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()


def _failure(
    *,
    kind: HypothesisGenerationFailureKind,
    error: Exception,
    occurred_at: datetime,
    raw_output: object | None = None,
) -> HypothesisGenerationFailure:
    detail = f"{type(error).__module__}.{type(error).__qualname__}:{error}"
    return HypothesisGenerationFailure(
        kind=kind,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        raw_output_sha256=(None if raw_output is None else _opaque_output_sha256(raw_output)),
        occurred_at=occurred_at,
    )


async def run_competing_hypothesis_generation(
    *,
    campaign_id: str,
    direction_gate: ResearchDirectionGate,
    policy: HypothesisGenerationPolicy,
    request: HypothesisGenerationRequest,
    generator: CompetingHypothesisGeneratorAdapter,
    deduplicator: HypothesisSemanticDeduplicatorAdapter,
    clock: Callable[[], datetime] | None = None,
) -> HypothesisGenerationCampaign:
    """Execute the unprivileged proposal/review path and derive the admission decision."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    if generator.manifest.manifest_sha256 != request.generator_manifest_sha256:
        raise ValueError("runtime hypothesis generator differs from the frozen request")
    if deduplicator.manifest.manifest_sha256 != request.deduplicator_manifest_sha256:
        raise ValueError("runtime hypothesis deduplicator differs from the frozen request")
    _validate_request_bindings(
        gate=direction_gate,
        policy=policy,
        generator_manifest=generator.manifest,
        deduplicator_manifest=deduplicator.manifest,
        request=request,
    )
    graph = direction_gate.novelty_decision.coverage.claim_graph_bundle.graph
    candidate = next(
        item for item in graph.claims if item.claim_sha256 == request.candidate_claim_sha256
    )
    prior_claims = tuple(item for item in graph.claims if item.origin is ClaimOrigin.PRIOR_ART)
    relations = tuple(
        item.relation
        for item in direction_gate.novelty_decision.coverage.prior_art_resolution.accepted
    )
    try:
        raw_generation = await generator.generate(
            request=request,
            candidate_claim=candidate,
            prior_claims=prior_claims,
            accepted_prior_art_relations=relations,
        )
    except Exception as exc:  # noqa: BLE001 - explicit sanitized failure artifact
        failure = _failure(
            kind=HypothesisGenerationFailureKind.GENERATOR_ERROR,
            error=exc,
            occurred_at=_now(clock),
        )
        return HypothesisGenerationCampaign(
            campaign_id=campaign_id,
            direction_gate=direction_gate,
            policy=policy,
            generator_manifest=generator.manifest,
            deduplicator_manifest=deduplicator.manifest,
            request=request,
            failure=failure,
            duplicate_resolutions=(),
            discrimination_edges=(),
            blockers=(f"generation_failure:{failure.kind.value}",),
            disposition=HypothesisGenerationDisposition.BLOCKED_GENERATION,
            generated_at=_now(clock),
        )
    generation_received_at = _now(clock)
    try:
        generation_batch = (
            raw_generation
            if isinstance(raw_generation, HypothesisGenerationBatch)
            else HypothesisGenerationBatch.model_validate(raw_generation)
        )
        _validate_generation_batch(
            batch=generation_batch,
            request=request,
            manifest=generator.manifest,
            policy=policy,
            received_at=generation_received_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _failure(
            kind=HypothesisGenerationFailureKind.GENERATOR_OUTPUT_INVALID,
            error=exc,
            raw_output=raw_generation,
            occurred_at=generation_received_at,
        )
        return HypothesisGenerationCampaign(
            campaign_id=campaign_id,
            direction_gate=direction_gate,
            policy=policy,
            generator_manifest=generator.manifest,
            deduplicator_manifest=deduplicator.manifest,
            request=request,
            failure=failure,
            duplicate_resolutions=(),
            discrimination_edges=(),
            blockers=(f"generation_failure:{failure.kind.value}",),
            disposition=HypothesisGenerationDisposition.BLOCKED_GENERATION,
            generated_at=_now(clock),
        )
    try:
        raw_deduplication = await deduplicator.compare(generation_batch=generation_batch)
    except Exception as exc:  # noqa: BLE001 - explicit sanitized failure artifact
        failure = _failure(
            kind=HypothesisGenerationFailureKind.DEDUPLICATOR_ERROR,
            error=exc,
            occurred_at=_now(clock),
        )
        return HypothesisGenerationCampaign(
            campaign_id=campaign_id,
            direction_gate=direction_gate,
            policy=policy,
            generator_manifest=generator.manifest,
            deduplicator_manifest=deduplicator.manifest,
            request=request,
            generation_batch=generation_batch,
            failure=failure,
            duplicate_resolutions=(),
            discrimination_edges=(),
            blockers=(f"generation_failure:{failure.kind.value}",),
            disposition=HypothesisGenerationDisposition.BLOCKED_GENERATION,
            generated_at=_now(clock),
        )
    deduplication_received_at = _now(clock)
    try:
        deduplication_batch = (
            raw_deduplication
            if isinstance(raw_deduplication, HypothesisDeduplicationBatch)
            else HypothesisDeduplicationBatch.model_validate(raw_deduplication)
        )
        _validate_deduplication_batch(
            batch=deduplication_batch,
            generation_batch=generation_batch,
            manifest=deduplicator.manifest,
            received_at=deduplication_received_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _failure(
            kind=HypothesisGenerationFailureKind.DEDUPLICATOR_OUTPUT_INVALID,
            error=exc,
            raw_output=raw_deduplication,
            occurred_at=deduplication_received_at,
        )
        return HypothesisGenerationCampaign(
            campaign_id=campaign_id,
            direction_gate=direction_gate,
            policy=policy,
            generator_manifest=generator.manifest,
            deduplicator_manifest=deduplicator.manifest,
            request=request,
            generation_batch=generation_batch,
            failure=failure,
            duplicate_resolutions=(),
            discrimination_edges=(),
            blockers=(f"generation_failure:{failure.kind.value}",),
            disposition=HypothesisGenerationDisposition.BLOCKED_GENERATION,
            generated_at=_now(clock),
        )
    generated_at = _now(clock)
    derived = _derive_campaign_outputs(
        gate=direction_gate,
        policy=policy,
        generator_manifest=generator.manifest,
        request=request,
        generation_batch=generation_batch,
        deduplication_batch=deduplication_batch,
        generated_at=generated_at,
    )
    return HypothesisGenerationCampaign(
        campaign_id=campaign_id,
        direction_gate=direction_gate,
        policy=policy,
        generator_manifest=generator.manifest,
        deduplicator_manifest=deduplicator.manifest,
        request=request,
        generation_batch=generation_batch,
        deduplication_batch=deduplication_batch,
        duplicate_resolutions=derived.duplicate_resolutions,
        discrimination_edges=derived.discrimination_edges,
        blockers=derived.blockers,
        disposition=derived.disposition,
        world_model_snapshot=derived.world_model_snapshot,
        generated_at=generated_at,
    )


def commit_hypothesis_generation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: HypothesisGenerationCampaign,
) -> CommittedHypothesisGenerationCampaign:
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=campaign.generated_at,
    )
    return CommittedHypothesisGenerationCampaign(campaign=campaign, ledger=ledger)


def load_hypothesis_generation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> HypothesisGenerationCampaign:
    payload = archive.read_ledger(ledger)
    campaign = HypothesisGenerationCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived hypothesis-generation campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived hypothesis-generation campaign changed object identity")
    return campaign


def persist_ready_world_model(
    campaign: HypothesisGenerationCampaign,
) -> WorldModelStoreReceipt:
    if (
        campaign.disposition is not HypothesisGenerationDisposition.READY
        or campaign.world_model_snapshot is None
    ):
        raise ValueError("only a ready hypothesis-generation campaign can persist a world model")
    return store_world_model_snapshot(campaign.world_model_snapshot)


__all__ = [
    "HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256",
    "HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256",
    "SEMANTIC_NORMALIZER_SHA256",
    "AssumptionDraft",
    "CommittedHypothesisGenerationCampaign",
    "CompetingHypothesisGeneratorAdapter",
    "DiscriminationEdge",
    "DuplicateDisposition",
    "DuplicateResolution",
    "HypothesisDeduplicationBatch",
    "HypothesisDeduplicatorManifest",
    "HypothesisDraft",
    "HypothesisGenerationBatch",
    "HypothesisGenerationCampaign",
    "HypothesisGenerationDisposition",
    "HypothesisGenerationFailure",
    "HypothesisGenerationFailureKind",
    "HypothesisGenerationPolicy",
    "HypothesisGenerationRequest",
    "HypothesisGeneratorManifest",
    "HypothesisGeneratorRuntime",
    "HypothesisPairJudgment",
    "HypothesisSemanticDeduplicatorAdapter",
    "PredictionDraft",
    "QuestionDraft",
    "SemanticHypothesisRelation",
    "build_hypothesis_generation_request",
    "commit_hypothesis_generation_campaign",
    "load_hypothesis_generation_campaign",
    "persist_ready_world_model",
    "run_competing_hypothesis_generation",
]
