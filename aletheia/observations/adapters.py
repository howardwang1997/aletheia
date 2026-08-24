"""Concrete PR-5 custody adapters for the scientific observation bridge.

This protected module contains only the action-authority and raw-run adapters. Legacy F9-v1
campaign interpretation lives in the explicit migration compatibility leaf and is never imported
by the observation or controller authority graphs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from aletheia.execution.allocator import VerifiedQualificationRunLineage
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.observations.scientific_bridge import (
    RawRunEnvelope,
    ScientificActionProtocolBinding,
    ScientificExecutionAuthorization,
    VerifiedArtifactCustodyProjection,
    VerifiedExecutionAuthorityProjection,
    VerifiedRawRunCustodyProjection,
    validate_raw_run_structure,
)
from aletheia.observations.store import (
    ScientificExecutionAuthorizationWrite,
    get_scientific_execution_authorization_by_attempt,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    canonical_sha256,
)
from aletheia.research_store.store import (
    ResearchKernelStore,
    ResearchReplayAudit,
)


class ObservationAdapterVerificationError(RuntimeError):
    """A concrete adapter could not prove the requested historical binding."""


class QualificationRunLineageArchive(Protocol):
    """Public execution read seam; implementations must not expose private ORM records."""

    def load_verified_qualification_run_lineage(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        observed_at: datetime,
    ) -> VerifiedQualificationRunLineage | None: ...


class PostgreSQLRawRunCustodyVerificationAdapter:
    """Close registered SEA, PostgreSQL runtime-v2, and fresh filesystem-CAS custody.

    The execution facade performs the cryptographic and row-level proof.  This adapter adds the
    cross-store fact that the exact SEA was durably registered before admission/reservation/actual
    process start, rejects any rebound raw envelope, and freshly reopens every manifest/receipt CAS
    object.  Pricing and artifact verification do not currently carry signing key IDs in their
    v1 contracts, so their deployment-owned key projections are explicit constructor pins.
    """

    def __init__(
        self,
        *,
        execution_lineage: QualificationRunLineageArchive,
        artifact_store: LocalArtifactStore,
        sea_sessions: sessionmaker[Session],
        allocator_authority: VerifiedExecutionAuthorityProjection,
        artifact_authority: VerifiedExecutionAuthorityProjection,
    ) -> None:
        if not callable(
            getattr(execution_lineage, "load_verified_qualification_run_lineage", None)
        ):
            raise TypeError("raw-run custody requires the public execution lineage archive")
        if not isinstance(artifact_store, LocalArtifactStore):
            raise TypeError("raw-run custody requires LocalArtifactStore fresh-rehash custody")
        if not callable(sea_sessions):
            raise TypeError("raw-run custody requires a scientific authorization session factory")
        self._execution_lineage = execution_lineage
        self._artifact_store = artifact_store
        self._sea_sessions = sea_sessions
        self._allocator_authority = VerifiedExecutionAuthorityProjection.model_validate(
            allocator_authority.model_dump(mode="python")
        )
        self._artifact_authority = VerifiedExecutionAuthorityProjection.model_validate(
            artifact_authority.model_dump(mode="python")
        )
        if self._artifact_authority.principal_id != artifact_store.verifier_principal_id:
            raise ValueError("artifact authority pin differs from the local CAS verifier principal")

    def verify_raw_run_custody(
        self,
        *,
        raw_run: RawRunEnvelope,
        observed_at: datetime,
    ) -> VerifiedRawRunCustodyProjection:
        _require_utc(observed_at, label="raw-run custody observation time")
        try:
            raw_run = validate_raw_run_structure(raw_run)
            if raw_run.assembled_at > observed_at:
                raise ObservationAdapterVerificationError(
                    "raw run was assembled after the custody observation time"
                )
            authorization = raw_run.scientific_authorization
            message = authorization.message
            intent = message.qualification_bundle.intent
            attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
            registration = self._load_registration(
                execution_id=intent.execution_id,
                attempt_id=attempt_id,
            )
            self._verify_registration(
                registration=registration,
                authorization=authorization,
            )
            candidate = self._execution_lineage.load_verified_qualification_run_lineage(
                execution_id=intent.execution_id,
                attempt_id=attempt_id,
                observed_at=observed_at,
            )
            if candidate is None:
                raise ObservationAdapterVerificationError(
                    "registered SEA has no completed qualification run lineage"
                )
            lineage = VerifiedQualificationRunLineage.model_validate(
                candidate.model_dump(mode="python")
            )
            self._verify_exact_lineage(raw_run=raw_run, lineage=lineage)
            if not (
                registration.registered_at < lineage.qualification_admitted_at
                and registration.registered_at < lineage.resource_reserved_at
                and registration.registered_at < lineage.runtime_launched_at
            ):
                raise ObservationAdapterVerificationError(
                    "SEA was not durably registered before admission, reservation, and launch"
                )
            fresh_artifacts = self._fresh_artifact_custody(raw_run)
            authorities = self._authority_projections(raw_run=raw_run, lineage=lineage)
            return VerifiedRawRunCustodyProjection(
                raw_run_sha256=raw_run.raw_run_sha256,
                scientific_execution_authorization_sha256=authorization.authorization_sha256,
                scientific_slot_id=message.scientific_slot_id,
                qualification_admission_sha256=raw_run.qualification_admission_sha256,
                sea_registration_sha256=canonical_sha256(registration),
                sea_registered_at=registration.registered_at,
                qualification_admitted_at=lineage.qualification_admitted_at,
                resource_reservation_sha256=lineage.resource_reservation_sha256,
                resource_reserved_at=lineage.resource_reserved_at,
                runtime_launch_sha256=lineage.runtime_launch_sha256,
                runtime_launched_at=lineage.runtime_launched_at,
                terminal_submission_sha256=lineage.terminal_submission_sha256,
                terminal_acceptance_sha256=lineage.terminal_acceptance_sha256,
                terminal_accepted_at=lineage.terminal_accepted_at,
                cost_quote_sha256=lineage.cost_quote_sha256,
                quoted_worker_node_manifest=lineage.quoted_worker_node_manifest,
                terminal_worker_node_manifest=lineage.terminal_worker_node_manifest,
                worker_node_enrollment=lineage.worker_node_enrollment,
                allocator_authority=authorities[0],
                qualification_authority=authorities[1],
                node_enrollment_authority=authorities[2],
                node_execution_authority=authorities[3],
                runtime_control_authority=authorities[4],
                terminal_submission_authority=authorities[5],
                terminal_acceptance_authority=authorities[6],
                artifact_manifest_sha256=lineage.artifact_manifest_sha256,
                output_tree_sha256=lineage.output_tree_sha256,
                artifact_verified_receipt_sha256s=(lineage.artifact_verified_receipt_sha256s),
                fresh_artifacts=fresh_artifacts,
                verified_at=observed_at,
            )
        except ObservationAdapterVerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed across DB/signature/CAS boundaries
            raise ObservationAdapterVerificationError(
                "raw-run custody could not prove the exact registered execution lineage"
            ) from exc

    def _load_registration(
        self,
        *,
        execution_id: str,
        attempt_id: str,
    ) -> ScientificExecutionAuthorizationWrite:
        with self._sea_sessions() as session:
            registration = get_scientific_execution_authorization_by_attempt(
                session,
                execution_id=execution_id,
                attempt_id=attempt_id,
            )
        if registration is None:
            raise ObservationAdapterVerificationError(
                "raw run has no durable scientific execution authorization registration"
            )
        return registration

    @staticmethod
    def _verify_registration(
        *,
        registration: ScientificExecutionAuthorizationWrite,
        authorization: ScientificExecutionAuthorization,
    ) -> None:
        message = authorization.message
        binding = message.action_protocol_binding
        source = binding.action_authorized_event
        intent = message.qualification_bundle.intent
        try:
            persisted = ScientificExecutionAuthorization.model_validate(
                registration.authorization_json
            )
        except (TypeError, ValueError) as exc:
            raise ObservationAdapterVerificationError(
                "persisted scientific execution authorization bytes are invalid"
            ) from exc
        if (
            persisted != authorization
            or registration.authorization_sha256 != authorization.authorization_sha256
            or registration.quest_id != binding.action.quest_id
            or registration.scientific_slot_id != message.scientific_slot_id
            or registration.action_sha256 != binding.action.object_sha256
            or registration.execution_id != intent.execution_id
            or registration.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
            or registration.source_event_sequence != source.sequence
            or registration.source_event_sha256 != source.event_sha256
            or registration.qualification_bundle_sha256
            != message.qualification_bundle.bundle_sha256
            or registration.qualification_grant_sha256 != message.qualification_grant.grant_sha256
            or registration.authorized_at != message.authorized_at
            or registration.expires_at != message.expires_at
            or registration.observation_admission_deadline != message.observation_admission_deadline
        ):
            raise ObservationAdapterVerificationError(
                "persisted SEA registration was rebound to different authority"
            )

    @staticmethod
    def _verify_exact_lineage(
        *,
        raw_run: RawRunEnvelope,
        lineage: VerifiedQualificationRunLineage,
    ) -> None:
        message = raw_run.scientific_authorization.message
        bundle = message.qualification_bundle
        grant = message.qualification_grant
        intent = bundle.intent
        accepted = raw_run.accepted_runtime_termination
        submission = raw_run.terminal_submission
        terminal = raw_run.accepted_terminal_submission
        receipts = raw_run.artifact_verified_receipts
        if (
            lineage.execution_id != intent.execution_id
            or lineage.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
            or lineage.intent_sha256 != intent.intent_sha256
            or lineage.qualification_bundle_sha256 != bundle.bundle_sha256
            or lineage.qualification_grant_sha256 != grant.grant_sha256
            or lineage.qualification_admission_sha256 != raw_run.qualification_admission_sha256
            or lineage.resource_reservation_sha256 != submission.resource_lease_sha256
            or lineage.runtime_launch_sha256 != accepted.node_runtime_launch_receipt_sha256
            or lineage.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
            or lineage.terminal_submission_sha256 != submission.terminal_submission_sha256
            or lineage.terminal_acceptance_sha256 != terminal.accepted_terminal_submission_sha256
            or lineage.terminal_accepted_at != terminal.accepted_at
            or lineage.cost_quote_sha256 != bundle.cost_quote.quote_sha256
            or lineage.node_inventory_sha256 != submission.node_inventory_sha256
            or lineage.quoted_worker_node_manifest.manifest_sha256
            != bundle.cost_quote.selected_node_manifest_sha256
            or lineage.terminal_worker_node_manifest.manifest_sha256
            != submission.node_manifest_sha256
            or lineage.artifact_manifest_sha256 != raw_run.artifact_manifest.manifest_sha256
            or lineage.output_tree_sha256 != submission.output_tree_sha256
            or lineage.artifact_verified_receipt_sha256s
            != submission.artifact_verified_receipt_sha256s
            or lineage.artifact_manifest != raw_run.artifact_manifest
            or lineage.artifact_verified_receipts != receipts
            or lineage.verified_at < raw_run.assembled_at
        ):
            raise ObservationAdapterVerificationError(
                "public execution lineage was rebound from the exact raw run"
            )

    def _fresh_artifact_custody(
        self,
        raw_run: RawRunEnvelope,
    ) -> tuple[VerifiedArtifactCustodyProjection, ...]:
        manifest = self._artifact_store.load_manifest(
            manifest_sha256=raw_run.artifact_manifest.manifest_sha256
        )
        if manifest is None or manifest != raw_run.artifact_manifest:
            raise ObservationAdapterVerificationError(
                "fresh CAS manifest lookup differs from the raw run"
            )
        fresh: list[VerifiedArtifactCustodyProjection] = []
        for expected in raw_run.artifact_verified_receipts:
            receipt = self._artifact_store.load_verified_receipt(
                verified_receipt_sha256=expected.verified_receipt_sha256
            )
            if receipt is None or receipt != expected:
                raise ObservationAdapterVerificationError(
                    "fresh CAS receipt lookup differs from the raw run"
                )
            if receipt.verifier_principal_id != self._artifact_authority.principal_id:
                raise ObservationAdapterVerificationError(
                    "artifact receipt differs from the deployment-pinned verifier"
                )
            fresh.append(
                VerifiedArtifactCustodyProjection(
                    artifact_key=receipt.artifact.artifact_key,
                    content_sha256=receipt.artifact.content_sha256,
                    artifact_verified_receipt_sha256=receipt.verified_receipt_sha256,
                    authority=self._artifact_authority,
                )
            )
        return tuple(sorted(fresh, key=lambda item: item.artifact_key))

    def _authority_projections(
        self,
        *,
        raw_run: RawRunEnvelope,
        lineage: VerifiedQualificationRunLineage,
    ) -> tuple[VerifiedExecutionAuthorityProjection, ...]:
        def authority(principal_id: str, key_id: str, policy_sha256: str):
            return VerifiedExecutionAuthorityProjection(
                principal_id=principal_id,
                key_id=key_id,
                policy_sha256=policy_sha256,
            )

        if (
            self._allocator_authority.principal_id != lineage.allocator_principal_id
            or self._allocator_authority.policy_sha256 != lineage.allocator_policy_sha256
        ):
            raise ObservationAdapterVerificationError(
                "pricing/allocator lineage differs from its deployment authority pin"
            )
        projections = (
            self._allocator_authority,
            authority(
                lineage.qualification_principal_id,
                lineage.qualification_key_id,
                lineage.qualification_policy_sha256,
            ),
            authority(
                lineage.node_enrollment_principal_id,
                lineage.node_enrollment_key_id,
                lineage.node_enrollment_policy_sha256,
            ),
            authority(
                lineage.node_execution_principal_id,
                lineage.node_execution_key_id,
                lineage.node_execution_policy_sha256,
            ),
            authority(
                lineage.runtime_control_principal_id,
                lineage.runtime_control_key_id,
                lineage.runtime_control_policy_sha256,
            ),
            authority(
                lineage.terminal_submission_principal_id,
                lineage.terminal_submission_key_id,
                lineage.terminal_submission_policy_sha256,
            ),
            authority(
                lineage.terminal_acceptance_principal_id,
                lineage.terminal_acceptance_key_id,
                lineage.terminal_acceptance_policy_sha256,
            ),
        )
        groups = (
            projections[0],
            projections[1],
            projections[2],
            projections[3],
            projections[4],
            self._artifact_authority,
            authority(
                raw_run.scientific_authorization.message.authorized_by_principal_id,
                raw_run.scientific_authorization.message.authorization_key_id,
                raw_run.scientific_authorization.message.execution_authority_policy_sha256,
            ),
            authority(
                raw_run.scientific_authorization.message.validator_principal_id,
                raw_run.scientific_authorization.message.validator_key_id,
                raw_run.scientific_authorization.message.validator_authority_policy_sha256,
            ),
            authority(
                raw_run.scientific_authorization.message.admission_principal_id,
                raw_run.scientific_authorization.message.admission_key_id,
                raw_run.scientific_authorization.message.admission_authority_policy_sha256,
            ),
        )
        if len({item.principal_id for item in groups}) != len(groups) or len(
            {item.key_id for item in groups}
        ) != len(groups):
            raise ObservationAdapterVerificationError(
                "raw-run trust roots violate deployment principal/key separation"
            )
        return projections


class PostgreSQLResearchActionAuthorityAdapter:
    """Consume ``ResearchKernelStore.audit`` as the action/protocol authority proof.

    ``audit`` performs the database-chain, signed receipt, CAS-object, reducer, and per-event
    snapshot verification.  This adapter verifies the bridge-specific fact that the exact proposal
    and authorization were adjacent and that the authorization's freshly audited snapshot is the
    graph snapshot compiled into the protocol.
    """

    def __init__(self, store: ResearchKernelStore) -> None:
        if not callable(getattr(store, "audit", None)):
            raise TypeError("action authority adapter requires ResearchKernelStore.audit")
        self._store = store

    def verify_action_protocol_binding(
        self,
        *,
        binding: ScientificActionProtocolBinding,
        observed_at: datetime,
    ) -> str:
        _require_utc(observed_at, label="action authority observation time")
        try:
            binding = ScientificActionProtocolBinding.model_validate(
                binding.model_dump(mode="python")
            )
            if observed_at < binding.bound_at:
                raise ValueError("action authority observation predates the binding")
            scope = binding.compilation_request.protocol.graph_scope
            audit = self._store.audit(
                binding.action.quest_id,
                expected_scope_binding=scope.scope_binding,
            )
            audit = ResearchReplayAudit.model_validate(audit.model_dump(mode="python"))
            self._verify_audit(binding=binding, audit=audit, observed_at=observed_at)
        except ObservationAdapterVerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the store/CAS boundary
            raise ObservationAdapterVerificationError(
                "research action audit did not prove the exact action/protocol binding"
            ) from exc
        return binding.binding_sha256

    @staticmethod
    def _verify_audit(
        *,
        binding: ScientificActionProtocolBinding,
        audit: ResearchReplayAudit,
        observed_at: datetime,
    ) -> None:
        scope = binding.compilation_request.protocol.graph_scope
        if (
            audit.quest_id != binding.action.quest_id
            or audit.scope_binding != scope.scope_binding
            or audit.state.quest_id != binding.action.quest_id
        ):
            raise ObservationAdapterVerificationError(
                "research audit returned another Quest or scope"
            )
        if len(audit.events) != len(audit.verified_snapshot_sha256s):
            raise ObservationAdapterVerificationError(
                "research audit did not verify one snapshot for every event"
            )
        if any(event.committed_at > observed_at for event in audit.events):
            raise ObservationAdapterVerificationError(
                "research audit used an event committed after the observation time"
            )

        proposed_indexes = tuple(
            index
            for index, event in enumerate(audit.events)
            if event == binding.action_proposed_event
        )
        authorized_indexes = tuple(
            index
            for index, event in enumerate(audit.events)
            if event == binding.action_authorized_event
        )
        if (
            len(proposed_indexes) != 1
            or len(authorized_indexes) != 1
            or authorized_indexes[0] != proposed_indexes[0] + 1
        ):
            raise ObservationAdapterVerificationError(
                "exact action proposal and authorization are not unique adjacent audit events"
            )
        authorized_index = authorized_indexes[0]
        if (
            audit.verified_snapshot_sha256s[authorized_index]
            != binding.authorized_graph_snapshot_sha256
        ):
            raise ObservationAdapterVerificationError(
                "authorized event snapshot differs from the compiled graph snapshot"
            )

        questions = tuple(
            admission
            for admission in audit.state.questions
            if admission.object_ref == binding.action.question_ref
        )
        actions = tuple(
            action
            for action in audit.state.actions
            if action.action_ref == binding.action.object_ref
        )
        if len(questions) != 1 or len(actions) != 1:
            raise ObservationAdapterVerificationError(
                "replayed state does not contain the exact action and question CAS references"
            )
        action = actions[0]
        if (
            action.branch_id != scope.branch_id
            or action.kind is not binding.action.kind
            or action.proposed_event_sha256 != binding.action_proposed_event.event_sha256
        ):
            raise ObservationAdapterVerificationError(
                "replayed action state differs from its proposed CAS object"
            )
        if action.lifecycle is ActionLifecycle.AUTHORIZED:
            if action.decided_event_sha256 != binding.action_authorized_event.event_sha256:
                raise ObservationAdapterVerificationError(
                    "replayed action authorization differs from the exact authorization event"
                )
            return
        if action.lifecycle is not ActionLifecycle.APPLIED:
            raise ObservationAdapterVerificationError(
                "action is not authorized and has no admitted observation"
            )
        incorporated = tuple(
            event
            for event in audit.events[authorized_index + 1 :]
            if event.event_type is EventType.OBSERVATION_INCORPORATED
            and isinstance(event.payload, ObservationIncorporatedPayload)
            and event.payload.action_id == binding.action.action_id
            and event.payload.branch_id == scope.branch_id
            and event.event_sha256 == action.decided_event_sha256
        )
        if len(incorporated) != 1:
            raise ObservationAdapterVerificationError(
                "applied action does not resolve to one later audited observation event"
            )


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ObservationAdapterVerificationError(f"{label} must be timezone-aware UTC")


__all__ = [
    "ObservationAdapterVerificationError",
    "PostgreSQLRawRunCustodyVerificationAdapter",
    "PostgreSQLResearchActionAuthorityAdapter",
    "QualificationRunLineageArchive",
]
