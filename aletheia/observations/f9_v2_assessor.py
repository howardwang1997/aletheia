"""Conservative exact-content assessor for the graph-scoped F9-v2 validator.

This is a deliberately narrow deployment baseline, not a general scientific analysis engine.  A
freshly rehashed raw observation is recognized only when its complete scientific context and
content digest match one frozen catalog entry.  Unknown content remains an engineering blocker;
it is never guessed into a positive, negative, or inconclusive observation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from aletheia.observations.f9_v2_validation import (
    F9V2IndependentValidationAssessment,
    F9V2ValidationRequest,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    RawRunEnvelope,
    ScientificBridgeModel,
    validate_raw_run_structure,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.:/-]{1,127}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"


class FrozenF9V2ExactContentAssessmentTemplate(ScientificBridgeModel):
    """One pre-reviewed mapping from exact observation bytes to one closed assessment."""

    schema_name: Literal["aletheia.frozen_f9_v2_exact_content_assessment_template"] = (
        "aletheia.frozen_f9_v2_exact_content_assessment_template"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_authorized_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_graph_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    scientific_observation_artifact_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    raw_observation_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_bytes: int = Field(ge=1)
    raw_observation_media_type: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
    )
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: Literal[
        BridgeValidationDisposition.VALIDATED_CONFIRMATION,
        BridgeValidationDisposition.REJECTED_SCIENTIFIC,
    ]
    outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    blocker_codes: tuple[str, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def _assessment_is_closed(self) -> "FrozenF9V2ExactContentAssessmentTemplate":
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))) or any(
            re.fullmatch(_LOCAL_ID_PATTERN, item) is None for item in self.blocker_codes
        ):
            raise ValueError("F9-v2 template blockers must be unique and canonical")
        if self.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
            if self.blocker_codes:
                raise ValueError("validated F9-v2 template cannot carry blockers")
        elif not self.blocker_codes:
            raise ValueError("scientifically rejected F9-v2 template requires blockers")
        return self

    @property
    def template_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def lookup_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_exact_content_assessment_lookup",
                "schema_version": 1,
                "quest_id": self.quest_id,
                "action_sha256": self.action_sha256,
                "action_authorized_event_sha256": self.action_authorized_event_sha256,
                "authorized_graph_snapshot_sha256": self.authorized_graph_snapshot_sha256,
                "graph_scope_sha256": self.graph_scope_sha256,
                "protocol_sha256": self.protocol_sha256,
                "world_model_snapshot_sha256": self.world_model_snapshot_sha256,
                "scientific_slot_id": self.scientific_slot_id,
                "scientific_observation_artifact_binding_sha256": (
                    self.scientific_observation_artifact_binding_sha256
                ),
                "artifact_key": self.artifact_key,
                "raw_observation_schema_sha256": self.raw_observation_schema_sha256,
                "raw_observation_content_sha256": self.raw_observation_content_sha256,
                "raw_observation_bytes": self.raw_observation_bytes,
                "raw_observation_media_type": self.raw_observation_media_type,
                "validator_manifest_sha256": self.validator_manifest_sha256,
                "observation_validation_policy_sha256": (self.observation_validation_policy_sha256),
            }
        )

    @classmethod
    def from_raw_run(
        cls,
        *,
        raw_run: RawRunEnvelope,
        disposition: Literal[
            BridgeValidationDisposition.VALIDATED_CONFIRMATION,
            BridgeValidationDisposition.REJECTED_SCIENTIFIC,
        ],
        outcome_bin_id: str,
        blocker_codes: tuple[str, ...] = (),
    ) -> "FrozenF9V2ExactContentAssessmentTemplate":
        raw_run = validate_raw_run_structure(raw_run)
        authorization = raw_run.scientific_authorization.message
        binding = authorization.action_protocol_binding
        protocol = binding.compilation_request.protocol
        world_model = protocol.world_model
        artifact = authorization.scientific_observation_artifact_binding
        entries = tuple(
            item
            for item in raw_run.artifact_manifest.entries
            if item.artifact_key == artifact.artifact_key
        )
        if world_model is None or len(entries) != 1:
            raise ValueError("F9-v2 assessment template requires one graph-scoped observation")
        entry = entries[0]
        return cls(
            quest_id=binding.action.quest_id,
            action_sha256=binding.action.object_sha256,
            action_authorized_event_sha256=binding.action_authorized_event.event_sha256,
            authorized_graph_snapshot_sha256=binding.authorized_graph_snapshot_sha256,
            graph_scope_sha256=protocol.graph_scope.graph_scope_sha256,
            protocol_sha256=protocol.protocol_sha256,
            world_model_snapshot_sha256=world_model.world_model_sha256,
            scientific_slot_id=authorization.scientific_slot_id,
            scientific_observation_artifact_binding_sha256=artifact.binding_sha256,
            artifact_key=artifact.artifact_key,
            raw_observation_schema_sha256=artifact.expected_artifact.schema_sha256,
            raw_observation_content_sha256=entry.content_sha256,
            raw_observation_bytes=entry.bytes,
            raw_observation_media_type=entry.media_type,
            validator_manifest_sha256=authorization.validator_manifest_sha256,
            observation_validation_policy_sha256=(
                authorization.observation_validation_policy_sha256
            ),
            disposition=disposition,
            outcome_bin_id=outcome_bin_id,
            blocker_codes=blocker_codes,
        )


class FrozenF9V2ExactContentAssessmentCatalog(ScientificBridgeModel):
    """Canonical set of exact-content templates deployed with one validator implementation."""

    schema_name: Literal["aletheia.frozen_f9_v2_exact_content_assessment_catalog"] = (
        "aletheia.frozen_f9_v2_exact_content_assessment_catalog"
    )
    schema_version: Literal[1] = 1
    catalog_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    assessor_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    templates: tuple[FrozenF9V2ExactContentAssessmentTemplate, ...] = Field(
        min_length=1,
        max_length=4096,
    )

    @model_validator(mode="after")
    def _templates_are_canonical(self) -> "FrozenF9V2ExactContentAssessmentCatalog":
        identities = tuple(item.lookup_sha256 for item in self.templates)
        hashes = tuple(item.template_sha256 for item in self.templates)
        if hashes != tuple(sorted(hashes)) or len(set(identities)) != len(identities):
            raise ValueError("F9-v2 assessment templates must be sorted and uniquely scoped")
        return self

    @property
    def catalog_sha256(self) -> str:
        return canonical_sha256(self)


class ExactContentF9V2ObservationAssessor:
    """Map only fresh-rehashed, fully scoped known content to a frozen assessment."""

    def __init__(
        self,
        *,
        catalog: FrozenF9V2ExactContentAssessmentCatalog,
        implementation_sha256: str,
    ) -> None:
        if catalog.assessor_implementation_sha256 != implementation_sha256:
            raise ValueError("F9-v2 assessor implementation differs from its catalog pin")
        self._catalog = catalog

    def assess_observation(
        self,
        *,
        request: F9V2ValidationRequest,
        raw_run: RawRunEnvelope,
        assessed_at: datetime,
    ) -> F9V2IndependentValidationAssessment:
        request = F9V2ValidationRequest.model_validate(request.model_dump(mode="python"))
        raw_run = validate_raw_run_structure(raw_run)
        lookup = _request_lookup_sha256(request=request, raw_run=raw_run)
        matches = tuple(item for item in self._catalog.templates if item.lookup_sha256 == lookup)
        if not matches:
            return F9V2IndependentValidationAssessment(
                validation_request_sha256=request.request_sha256,
                raw_observation_content_sha256=request.raw_observation_content_sha256,
                disposition=BridgeValidationDisposition.BLOCKED_EXECUTION,
                outcome_bin_id=None,
                validation_batch_sha256=None,
                blocker_codes=("f9-v2:unrecognized-exact-content",),
                assessed_at=assessed_at,
            )
        if len(matches) != 1:  # pragma: no cover - catalog model prevents this state
            raise ValueError("F9-v2 assessment catalog has an ambiguous exact-content match")
        template = matches[0]
        admission_policy = raw_run.scientific_authorization.message.admission_policy
        mapped_bins = tuple(item.outcome_bin_id for item in admission_policy.outcome_bin_mappings)
        if (
            admission_policy.validator_manifest_sha256 != request.validator_manifest_sha256
            or admission_policy.observation_validation_policy_sha256
            != request.observation_validation_policy_sha256
            or admission_policy.analysis_outcome_space_sha256
            != request.analysis_outcome_space_sha256
            or template.outcome_bin_id not in mapped_bins
        ):
            raise ValueError("F9-v2 exact-content assessment escaped the admission policy")
        batch_sha256 = canonical_sha256(
            {
                "schema_name": "aletheia.f9_v2_exact_content_validation_batch",
                "schema_version": 1,
                "catalog_sha256": self._catalog.catalog_sha256,
                "template_sha256": template.template_sha256,
                "validation_request_sha256": request.request_sha256,
                "raw_run_sha256": request.raw_run_sha256,
            }
        )
        return F9V2IndependentValidationAssessment(
            validation_request_sha256=request.request_sha256,
            raw_observation_content_sha256=request.raw_observation_content_sha256,
            disposition=template.disposition,
            outcome_bin_id=template.outcome_bin_id,
            validation_batch_sha256=batch_sha256,
            blocker_codes=template.blocker_codes,
            assessed_at=assessed_at,
        )


def _request_lookup_sha256(*, request: F9V2ValidationRequest, raw_run: RawRunEnvelope) -> str:
    authorization = raw_run.scientific_authorization.message
    artifact = authorization.scientific_observation_artifact_binding
    if raw_run.raw_run_sha256 != request.raw_run_sha256:
        raise ValueError("F9-v2 assessor raw run differs from its request")
    return canonical_sha256(
        {
            "schema_name": "aletheia.f9_v2_exact_content_assessment_lookup",
            "schema_version": 1,
            "quest_id": request.quest_id,
            "action_sha256": request.action_sha256,
            "action_authorized_event_sha256": request.action_authorized_event_sha256,
            "authorized_graph_snapshot_sha256": request.authorized_graph_snapshot_sha256,
            "graph_scope_sha256": request.graph_scope_sha256,
            "protocol_sha256": request.protocol_sha256,
            "world_model_snapshot_sha256": request.world_model_snapshot_sha256,
            "scientific_slot_id": request.scientific_slot_id,
            "scientific_observation_artifact_binding_sha256": (
                request.scientific_observation_artifact_binding_sha256
            ),
            "artifact_key": request.artifact_key,
            "raw_observation_schema_sha256": artifact.expected_artifact.schema_sha256,
            "raw_observation_content_sha256": request.raw_observation_content_sha256,
            "raw_observation_bytes": request.raw_observation_bytes,
            "raw_observation_media_type": request.raw_observation_media_type,
            "validator_manifest_sha256": request.validator_manifest_sha256,
            "observation_validation_policy_sha256": (request.observation_validation_policy_sha256),
        }
    )


__all__ = [
    "ExactContentF9V2ObservationAssessor",
    "FrozenF9V2ExactContentAssessmentCatalog",
    "FrozenF9V2ExactContentAssessmentTemplate",
]
