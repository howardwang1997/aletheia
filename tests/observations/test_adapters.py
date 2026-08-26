from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

import aletheia.epistemics as e
from aletheia.epistemics.belief_update import CommittedObservationValidationCampaign
from aletheia.execution.allocator import VerifiedQualificationRunLineage
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from aletheia.migration.f9_v1_observation_compatibility import (
    ContentAddressedF9ValidationCampaignArchiveAdapter,
    ObservationArchiveCorruption,
)
from aletheia.observations.adapters import (
    ObservationAdapterVerificationError,
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    ObservationAdmissionPolicy,
    RawRunEnvelope,
    ScientificObservationArtifactBinding,
    VerifiedExecutionAuthorityProjection,
    issue_scientific_execution_authorization,
)
from aletheia.observations.store import (
    ScientificExecutionAuthorizationWrite,
    register_scientific_execution_authorization,
)
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    ObjectAdmission,
    ResearchStateGraph,
)
from aletheia.research_store.store import ResearchReplayAudit
from epistemics.f9s2_fixtures import StepClock, build_f9s2_fixture
from epistemics.f9s3_fixtures import build_f9s3_fixture
from epistemics.f9s6_fixtures import FixtureObservationValidator, build_f9s6_fixture
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture

_OBSERVATION_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_OBSERVATION_TESTS))
from test_scientific_bridge import (  # noqa: E402
    EXECUTION_AUTHORITY_PRIVATE_KEY,
    BridgeCase,
    _bridge_case,
    _digest,
    _raw_run,
)
from persistence_test_support import sqlite_observation_engine  # noqa: E402
import test_runtime_contracts as runtime_test_module  # noqa: E402
import test_scientific_bridge as bridge_test_module  # noqa: E402
import fixtures as protocol_fixture_module  # noqa: E402


@dataclass
class _AuditStore:
    result: ResearchReplayAudit
    calls: list[tuple[str, object]] = field(default_factory=list)

    def audit(self, quest_id: str, *, expected_scope_binding=None) -> ResearchReplayAudit:
        self.calls.append((quest_id, expected_scope_binding))
        return self.result

    def audit_in_session(
        self,
        _session,
        quest_id: str,
        *,
        expected_scope_binding=None,
    ) -> ResearchReplayAudit:
        self.calls.append((quest_id, expected_scope_binding))
        return self.result


def _action_audit(case: BridgeCase) -> ResearchReplayAudit:
    binding = case.binding
    scope = binding.compilation_request.protocol.graph_scope
    proposed = binding.action_proposed_event
    authorized = binding.action_authorized_event
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=authorized.sequence,
        tail_event_sha256=authorized.event_sha256,
        event_ids=(proposed.event_id, authorized.event_id),
        event_sha256s=(proposed.event_sha256, authorized.event_sha256),
        questions=(
            ObjectAdmission(
                object_ref=binding.action.question_ref,
                branch_id=scope.branch_id,
                admitted_event_sha256=_digest("adapter-question-admission"),
            ),
        ),
        actions=(
            ActionSnapshot(
                action_ref=binding.action.object_ref,
                branch_id=scope.branch_id,
                kind=binding.action.kind,
                lifecycle=ActionLifecycle.AUTHORIZED,
                proposed_event_sha256=proposed.event_sha256,
                decided_event_sha256=authorized.event_sha256,
            ),
        ),
    )
    return ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=scope.scope_binding,
        events=(proposed, authorized),
        state=state,
        verified_snapshot_sha256s=(
            _digest("adapter-proposed-snapshot"),
            binding.authorized_graph_snapshot_sha256,
        ),
    )


@pytest.fixture(scope="module")
def action_bridge_case() -> BridgeCase:
    return _bridge_case()


def test_postgresql_action_adapter_consumes_exact_audited_authority(
    action_bridge_case: BridgeCase,
) -> None:
    case = action_bridge_case
    audit = _action_audit(case)
    store = _AuditStore(audit)
    adapter = PostgreSQLResearchActionAuthorityAdapter(store)  # type: ignore[arg-type]
    observed_at = case.binding.bound_at + timedelta(minutes=1)

    assert (
        adapter.verify_action_protocol_binding(
            binding=case.binding,
            observed_at=observed_at,
        )
        == case.binding.binding_sha256
    )
    assert store.calls == [
        (
            case.binding.action.quest_id,
            case.binding.compilation_request.protocol.graph_scope.scope_binding,
        )
    ]


def test_postgresql_action_adapter_locks_exact_current_authorized_head(
    action_bridge_case: BridgeCase,
) -> None:
    case = action_bridge_case
    store = _AuditStore(_action_audit(case))
    adapter = PostgreSQLResearchActionAuthorityAdapter(store)  # type: ignore[arg-type]
    observed_at = case.binding.bound_at + timedelta(minutes=1)

    with Session() as session, session.begin():
        assert (
            adapter.verify_current_action_protocol_binding_in_session(
                session,
                binding=case.binding,
                observed_at=observed_at,
            )
            == case.binding.binding_sha256
        )
        assert session.in_transaction()

    filler = case.binding.action_authorized_event.model_copy(
        update={"command_sha256": _digest("adapter-later-kernel-head")}
    )
    stale = store.result.model_copy(
        update={
            "events": (*store.result.events, filler),
            "verified_snapshot_sha256s": (
                *store.result.verified_snapshot_sha256s,
                _digest("adapter-later-kernel-snapshot"),
            ),
        }
    )
    adapter = PostgreSQLResearchActionAuthorityAdapter(  # type: ignore[arg-type]
        _AuditStore(stale)
    )
    with Session() as session, session.begin():
        with pytest.raises(ObservationAdapterVerificationError, match="current authorized head"):
            adapter.verify_current_action_protocol_binding_in_session(
                session,
                binding=case.binding,
                observed_at=observed_at,
            )


@pytest.mark.parametrize("fault", ["nonadjacent", "snapshot", "action", "future"])
def test_postgresql_action_adapter_rejects_rebound_audit(
    fault: str,
    action_bridge_case: BridgeCase,
) -> None:
    case = action_bridge_case
    audit = _action_audit(case)
    observed_at = case.binding.bound_at + timedelta(minutes=1)
    if fault == "nonadjacent":
        filler = case.binding.action_authorized_event.model_copy(
            update={"command_sha256": _digest("adapter-nonadjacent-event")}
        )
        audit = audit.model_copy(
            update={
                "events": (audit.events[0], filler, audit.events[1]),
                "verified_snapshot_sha256s": (
                    audit.verified_snapshot_sha256s[0],
                    _digest("adapter-filler-snapshot"),
                    audit.verified_snapshot_sha256s[1],
                ),
            }
        )
    elif fault == "snapshot":
        audit = audit.model_copy(
            update={
                "verified_snapshot_sha256s": (
                    audit.verified_snapshot_sha256s[0],
                    _digest("adapter-rebound-authorized-snapshot"),
                )
            }
        )
    elif fault == "action":
        action = audit.state.actions[0].model_copy(
            update={"decided_event_sha256": _digest("adapter-rebound-action-decision")}
        )
        audit = audit.model_copy(
            update={"state": audit.state.model_copy(update={"actions": (action,)})}
        )
    else:
        future = case.binding.action_authorized_event.model_copy(
            update={
                "command_sha256": _digest("adapter-future-event"),
                "committed_at": observed_at + timedelta(seconds=1),
            }
        )
        audit = audit.model_copy(
            update={
                "events": (*audit.events, future),
                "verified_snapshot_sha256s": (
                    *audit.verified_snapshot_sha256s,
                    _digest("adapter-future-snapshot"),
                ),
            }
        )

    adapter = PostgreSQLResearchActionAuthorityAdapter(  # type: ignore[arg-type]
        _AuditStore(audit)
    )
    with pytest.raises(ObservationAdapterVerificationError):
        adapter.verify_action_protocol_binding(
            binding=case.binding,
            observed_at=observed_at,
        )


@pytest.fixture(scope="module")
def causal_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("observation-adapter-causal-source")
    live = asyncio.run(build_f8s5_live_fixture(root / "f8", novelty_kind="strong"))
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:observation-adapter-source-hypotheses",
            direction_gate=hypotheses["gate"],
            policy=hypotheses["policy"],
            request=hypotheses["request"],
            generator=hypotheses["generator"],
            deduplicator=hypotheses["deduplicator"],
            clock=hypotheses["clock"],
        )
    )
    causal = build_f9s3_fixture(hypothesis_campaign)
    return asyncio.run(
        e.run_causal_identification_audit(
            campaign_id="campaign:observation-adapter-source-causal-audit",
            source_campaign=causal["source_campaign"],
            policy=causal["policy"],
            request=causal["request"],
            author=causal["author"],
            reviewer=causal["reviewer"],
            clock=causal["clock"],
        )
    )


@dataclass(frozen=True)
class _AlignedF9Case:
    bridge_case: BridgeCase
    raw_run: RawRunEnvelope
    failed_raw_run: RawRunEnvelope
    committed: CommittedObservationValidationCampaign
    rejected_committed: CommittedObservationValidationCampaign


@pytest.fixture(scope="module")
def aligned_f9_case(causal_source, tmp_path_factory) -> _AlignedF9Case:
    root = tmp_path_factory.mktemp("observation-adapter-f9")
    parts = build_f9s6_fixture(causal_source, root / "base")
    bridge_now = parts["observation_receipt"].observed_at - timedelta(minutes=25)
    signed_case_defaults = {
        "prior_execution_receipt": None,
        "quote_at": bridge_now,
        "grant_at": bridge_now + timedelta(minutes=1),
        "grant_expires_at": bridge_now + timedelta(minutes=10),
    }
    with (
        patch.object(bridge_test_module, "NOW", bridge_now),
        patch.object(runtime_test_module, "NOW", bridge_now),
        patch.object(protocol_fixture_module, "_NOW", bridge_now),
        patch.object(
            runtime_test_module._signed_case,
            "__kwdefaults__",
            signed_case_defaults,
        ),
    ):
        bridge_case = _bridge_case()
        original = bridge_case.authorization.message
        selected_prediction = parts["selected_candidate"].committed_prediction
        artifact_payload = original.scientific_observation_artifact_binding.model_dump(
            mode="python"
        )
        artifact_payload.update(
            {
                "observation_namespace_sha256": (
                    parts["observation_receipt"].experiment_namespace_sha256
                ),
                "selection_campaign_sha256": parts["selection"].campaign_sha256,
                "prediction_campaign_sha256": selected_prediction.campaign.campaign_sha256,
                "prediction_commitment_sha256": selected_prediction.campaign.commitment_sha256,
            }
        )
        artifact_binding = ScientificObservationArtifactBinding.model_validate(artifact_payload)
        admission_payload = original.admission_policy.model_dump(mode="python")
        admission_payload.update(
            {
                "validator_manifest_sha256": parts["validator_manifest"].manifest_sha256,
                "observation_validation_policy_sha256": (parts["validation_policy"].policy_sha256),
            }
        )
        admission_policy = ObservationAdmissionPolicy.model_validate(admission_payload)
        authorization = issue_scientific_execution_authorization(
            action_protocol_binding=bridge_case.binding,
            qualification_bundle=bridge_case.qualification.bundle,
            qualification_grant=bridge_case.qualification.grant,
            validator_manifest_sha256=parts["validator_manifest"].manifest_sha256,
            observation_validation_policy_sha256=parts["validation_policy"].policy_sha256,
            admission_policy=admission_policy,
            scientific_observation_artifact_binding=artifact_binding,
            qualification_authority=bridge_case.qualification_authority,
            action_authority=bridge_case.action_authority,
            qualification_custody=bridge_case.qualification_custody,
            execution_authority_pin=bridge_case.execution_pin,
            validator_authority_pin=bridge_case.validator_pin,
            admission_authority_pin=bridge_case.admission_pin,
            private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
            authorized_at=bridge_now + timedelta(minutes=5),
            expires_at=bridge_now + timedelta(minutes=9),
            observation_admission_deadline=bridge_now + timedelta(hours=2),
        )
        bridge_case = replace(bridge_case, authorization=authorization)
        artifact_updates = {
            "content_sha256": parts["observation_receipt"].observation_sha256,
            "bytes": len(parts["raw_observation"]),
            "media_type": parts["observation_receipt"].media_type,
        }
        raw_run = _raw_run(bridge_case, artifact_entry_updates=artifact_updates)
        failed_raw_run = _raw_run(
            bridge_case,
            "process_failed",
            artifact_entry_updates=artifact_updates,
        )

    request = parts["validation_request"]
    rejected_validator = FixtureObservationValidator(
        parts["validator_manifest"],
        completed_at=request.issued_at + timedelta(minutes=1),
        overrides={"sample_count": 1},
    )
    rejected_campaign = asyncio.run(
        e.run_observation_validation(
            campaign_id="campaign:observation-adapter-rejected-validation",
            policy=parts["validation_policy"],
            request=request,
            validator=rejected_validator,
            selection_archive=parts["selection_archive"],
            prediction_archive=parts["prediction_archive"],
            observation_store=parts["observation_store"],
            clock=StepClock(request.issued_at + timedelta(minutes=2)),
        )
    )
    campaign = parts["validation_campaign"]
    assert campaign.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
    assert rejected_campaign.disposition is e.ObservationValidationDisposition.REJECTED_SCIENTIFIC
    validation_archive = parts["validation_archive"]
    committed = parts["committed_validation"]
    rejected_committed = e.commit_observation_validation_campaign(
        archive=validation_archive,
        campaign=rejected_campaign,
        committed_at=rejected_campaign.generated_at + timedelta(minutes=1),
    )
    return _AlignedF9Case(
        bridge_case=bridge_case,
        raw_run=raw_run,
        failed_raw_run=failed_raw_run,
        committed=committed,
        rejected_committed=rejected_committed,
    )


def _localized_campaign(
    tmp_path: Path,
    *,
    aligned: _AlignedF9Case,
    committed: CommittedObservationValidationCampaign | None = None,
):
    committed = committed or aligned.committed
    campaign_archive = ContentAddressedResponseArchive(tmp_path / "campaign-cas")
    ledger = campaign_archive.store_ledger(
        value=committed.campaign,
        object_sha256=committed.campaign.campaign_sha256,
        archived_at=committed.committed_at,
    )
    localized = CommittedObservationValidationCampaign(
        campaign=committed.campaign,
        ledger=ledger,
        committed_at=committed.committed_at,
    )
    adapter = ContentAddressedF9ValidationCampaignArchiveAdapter(
        tmp_path / "graph-binding-cas",
        campaign_archive=campaign_archive,
    )
    binding = adapter.archive_committed_campaign(
        committed_campaign=localized,
        raw_run=aligned.raw_run,
        bound_at=localized.committed_at + timedelta(seconds=1),
    )
    return adapter, campaign_archive, localized, binding


def test_f9_archive_freshly_projects_exact_graph_scoped_campaign(
    tmp_path: Path,
    aligned_f9_case: _AlignedF9Case,
) -> None:
    adapter, _, committed, binding = _localized_campaign(
        tmp_path,
        aligned=aligned_f9_case,
    )
    authorization = aligned_f9_case.raw_run.scientific_authorization.message

    projection = adapter.verify_observation_validation_campaign(
        campaign_sha256=committed.campaign.campaign_sha256,
        raw_run=aligned_f9_case.raw_run,
        expected_validator_manifest_sha256=authorization.validator_manifest_sha256,
        expected_observation_validation_policy_sha256=(
            authorization.observation_validation_policy_sha256
        ),
        observed_at=binding.bound_at + timedelta(seconds=1),
    )

    assert projection.campaign_sha256 == committed.campaign.campaign_sha256
    assert projection.committed_campaign_sha256 == committed.receipt_sha256
    assert projection.raw_run_sha256 == aligned_f9_case.raw_run.raw_run_sha256
    assert projection.protocol_sha256 == (
        authorization.action_protocol_binding.compilation_request.protocol.protocol_sha256
    )
    assert projection.scientific_observation_artifact_binding_sha256 == (
        authorization.scientific_observation_artifact_binding.binding_sha256
    )
    assert projection.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION


def test_f9_archive_rehashes_campaign_bytes_on_every_verification(
    tmp_path: Path,
    aligned_f9_case: _AlignedF9Case,
) -> None:
    adapter, campaign_archive, committed, binding = _localized_campaign(
        tmp_path,
        aligned=aligned_f9_case,
    )
    target = campaign_archive.root / committed.ledger.relative_path
    target.chmod(0o600)
    target.write_bytes(b"{}")
    authorization = aligned_f9_case.raw_run.scientific_authorization.message

    with pytest.raises(ObservationAdapterVerificationError):
        adapter.verify_observation_validation_campaign(
            campaign_sha256=committed.campaign.campaign_sha256,
            raw_run=aligned_f9_case.raw_run,
            expected_validator_manifest_sha256=authorization.validator_manifest_sha256,
            expected_observation_validation_policy_sha256=(
                authorization.observation_validation_policy_sha256
            ),
            observed_at=binding.bound_at + timedelta(seconds=1),
        )


def test_f9_archive_rejects_graph_rebinding_pins_and_deadline(
    tmp_path: Path,
    aligned_f9_case: _AlignedF9Case,
) -> None:
    adapter, _, committed, binding = _localized_campaign(
        tmp_path,
        aligned=aligned_f9_case,
    )
    authorization = aligned_f9_case.raw_run.scientific_authorization.message
    rebound_raw = RawRunEnvelope.model_validate(
        {
            **aligned_f9_case.raw_run.model_dump(mode="python"),
            "assembled_at": aligned_f9_case.raw_run.assembled_at + timedelta(seconds=1),
        }
    )
    common = {
        "campaign_sha256": committed.campaign.campaign_sha256,
        "expected_validator_manifest_sha256": authorization.validator_manifest_sha256,
        "expected_observation_validation_policy_sha256": (
            authorization.observation_validation_policy_sha256
        ),
    }

    with pytest.raises(ObservationAdapterVerificationError):
        adapter.verify_observation_validation_campaign(
            **common,
            raw_run=rebound_raw,
            observed_at=binding.bound_at + timedelta(seconds=1),
        )
    with pytest.raises(ObservationAdapterVerificationError):
        adapter.verify_observation_validation_campaign(
            **{
                **common,
                "expected_validator_manifest_sha256": _digest("adapter-attacker-validator"),
            },
            raw_run=aligned_f9_case.raw_run,
            observed_at=binding.bound_at + timedelta(seconds=1),
        )
    with pytest.raises(ObservationAdapterVerificationError):
        adapter.verify_observation_validation_campaign(
            **common,
            raw_run=aligned_f9_case.raw_run,
            observed_at=authorization.observation_admission_deadline,
        )
    with pytest.raises(ObservationArchiveCorruption):
        adapter.archive_committed_campaign(
            committed_campaign=committed,
            raw_run=rebound_raw,
            bound_at=binding.bound_at,
        )


def test_f9_archive_preserves_scientific_rejection_and_rejects_engineering_failure(
    tmp_path: Path,
    aligned_f9_case: _AlignedF9Case,
) -> None:
    adapter, campaign_archive, committed, binding = _localized_campaign(
        tmp_path,
        aligned=aligned_f9_case,
        committed=aligned_f9_case.rejected_committed,
    )
    authorization = aligned_f9_case.raw_run.scientific_authorization.message
    projection = adapter.verify_observation_validation_campaign(
        campaign_sha256=committed.campaign.campaign_sha256,
        raw_run=aligned_f9_case.raw_run,
        expected_validator_manifest_sha256=authorization.validator_manifest_sha256,
        expected_observation_validation_policy_sha256=(
            authorization.observation_validation_policy_sha256
        ),
        observed_at=binding.bound_at + timedelta(seconds=1),
    )
    assert projection.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC
    assert projection.validation_batch_sha256 is not None
    assert projection.outcome_bin_id is not None
    assert projection.blocker_codes

    failed_archive = ContentAddressedF9ValidationCampaignArchiveAdapter(
        tmp_path / "failed-binding-cas",
        campaign_archive=campaign_archive,
    )
    with pytest.raises(ObservationAdapterVerificationError):
        failed_archive.archive_committed_campaign(
            committed_campaign=committed,
            raw_run=aligned_f9_case.failed_raw_run,
            bound_at=binding.bound_at,
        )


def test_f9_archive_rejects_symlinked_index_parent(
    tmp_path: Path,
    aligned_f9_case: _AlignedF9Case,
) -> None:
    campaign_archive = ContentAddressedResponseArchive(tmp_path / "campaign-cas")
    committed = aligned_f9_case.committed
    ledger = campaign_archive.store_ledger(
        value=committed.campaign,
        object_sha256=committed.campaign.campaign_sha256,
        archived_at=committed.committed_at,
    )
    localized = CommittedObservationValidationCampaign(
        campaign=committed.campaign,
        ledger=ledger,
        committed_at=committed.committed_at,
    )
    root = tmp_path / "binding-cas"
    adapter = ContentAddressedF9ValidationCampaignArchiveAdapter(
        root,
        campaign_archive=campaign_archive,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "campaigns")

    with pytest.raises(ObservationArchiveCorruption):
        adapter.archive_committed_campaign(
            committed_campaign=localized,
            raw_run=aligned_f9_case.raw_run,
            bound_at=localized.committed_at + timedelta(seconds=1),
        )


@dataclass
class _RunLineageArchive:
    projection: VerifiedQualificationRunLineage
    calls: list[tuple[str, str, object]] = field(default_factory=list)

    def load_verified_qualification_run_lineage(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        observed_at,
    ) -> VerifiedQualificationRunLineage:
        self.calls.append((execution_id, attempt_id, observed_at))
        return self.projection.model_copy(update={"verified_at": observed_at})


@dataclass(frozen=True)
class _RawCustodyCase:
    bridge: BridgeCase
    raw_run: RawRunEnvelope
    artifact_store: LocalArtifactStore
    sea_sessions: sessionmaker[Session]
    lineage: VerifiedQualificationRunLineage
    allocator_authority: VerifiedExecutionAuthorityProjection
    artifact_authority: VerifiedExecutionAuthorityProjection
    observed_at: object


def _registered_sea_sessions(
    raw_run: RawRunEnvelope,
    *,
    registered_at,
) -> sessionmaker[Session]:
    authorization = raw_run.scientific_authorization
    message = authorization.message
    binding = message.action_protocol_binding
    source = binding.action_authorized_event
    intent = message.qualification_bundle.intent
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"),
            {"quest": binding.action.quest_id},
        )
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": binding.action.object_sha256},
        )
        session.execute(
            text("INSERT INTO execution_attempts VALUES (:attempt, :execution)"),
            {
                "attempt": intent.infrastructure_attempt.infrastructure_attempt_id,
                "execution": intent.execution_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO research_kernel_events VALUES "
                "(:quest, :sequence, :event, 'action_authorized')"
            ),
            {
                "quest": binding.action.quest_id,
                "sequence": source.sequence,
                "event": source.event_sha256,
            },
        )
        register_scientific_execution_authorization(
            session,
            ScientificExecutionAuthorizationWrite.from_contract(
                authorization,
                registered_at=registered_at,
            ),
        )
    return sessionmaker(engine, expire_on_commit=False)


def _raw_run_with_local_cas(
    *,
    case: BridgeCase,
    artifact_store: LocalArtifactStore,
    output_root: Path,
) -> RawRunEnvelope:
    base = _raw_run(case)
    intent = case.qualification.bundle.intent
    output_root.mkdir()
    artifact_paths: dict[str, str] = {}
    for index, expected in enumerate(intent.expected_artifacts, start=1):
        relative_path = f"artifact-{index:03d}.bin"
        (output_root / relative_path).write_bytes(f"raw-{index}".encode())
        artifact_paths[expected.artifact_key] = relative_path
    manifest = artifact_store.quarantine_outputs(
        intent=intent,
        output_root=output_root,
        artifact_paths=artifact_paths,
        produced_at=base.accepted_runtime_termination.runtime_ended_at,
    )
    receipts = artifact_store.verify_manifest(intent=intent, manifest=manifest)
    receipt_hashes = tuple(sorted(item.verified_receipt_sha256 for item in receipts))
    submission = type(base.terminal_submission).model_validate(
        base.terminal_submission.model_copy(
            update={
                "artifact_manifest_sha256": manifest.manifest_sha256,
                "output_tree_sha256": runtime_test_module.artifact_output_tree_sha256(manifest),
                "artifact_verified_receipt_sha256s": receipt_hashes,
            }
        ).model_dump(mode="python")
    )
    terminal = type(base.accepted_terminal_submission).model_validate(
        base.accepted_terminal_submission.model_copy(
            update={
                "terminal_submission_sha256": submission.terminal_submission_sha256,
                "artifact_manifest_sha256": manifest.manifest_sha256,
                "output_tree_sha256": submission.output_tree_sha256,
                "artifact_verified_receipt_sha256s": receipt_hashes,
            }
        ).model_dump(mode="python")
    )
    return RawRunEnvelope(
        scientific_authorization=base.scientific_authorization,
        qualification_admission_sha256=base.qualification_admission_sha256,
        accepted_runtime_termination=base.accepted_runtime_termination,
        terminal_submission=submission,
        accepted_terminal_submission=terminal,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
        assembled_at=base.assembled_at,
    )


def _run_lineage(
    *,
    case: BridgeCase,
    raw_run: RawRunEnvelope,
    observed_at,
) -> VerifiedQualificationRunLineage:
    authorization = raw_run.scientific_authorization.message
    bundle = authorization.qualification_bundle
    grant = authorization.qualification_grant.message
    intent = bundle.intent
    accepted = raw_run.accepted_runtime_termination
    submission = raw_run.terminal_submission
    terminal = raw_run.accepted_terminal_submission
    manifest = case.worker_manifest
    enrollment = case.worker_enrollment.message
    admitted_at = authorization.authorized_at + timedelta(seconds=30)
    launched_at = authorization.authorized_at + timedelta(minutes=2)
    verified = case.qualification_custody._verified(
        bundle=bundle,
        grant=authorization.qualification_grant,
        verified_at=admitted_at,
    )
    return VerifiedQualificationRunLineage(
        execution_id=intent.execution_id,
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        intent_sha256=intent.intent_sha256,
        qualification_bundle_sha256=bundle.bundle_sha256,
        qualification_grant_sha256=authorization.qualification_grant.grant_sha256,
        qualification_admission_sha256=raw_run.qualification_admission_sha256,
        verified_engineering_qualification=verified,
        qualification_admitted_at=admitted_at,
        resource_reservation_sha256=submission.resource_lease_sha256,
        resource_reserved_at=admitted_at,
        runtime_launch_sha256=accepted.node_runtime_launch_receipt_sha256,
        runtime_launched_at=launched_at,
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        terminal_submission_sha256=submission.terminal_submission_sha256,
        terminal_acceptance_sha256=terminal.accepted_terminal_submission_sha256,
        terminal_accepted_at=terminal.accepted_at,
        cost_quote_sha256=bundle.cost_quote.quote_sha256,
        node_inventory_sha256=submission.node_inventory_sha256,
        quoted_worker_node_manifest=manifest,
        terminal_worker_node_manifest=manifest,
        worker_node_enrollment=case.worker_enrollment,
        allocator_principal_id=bundle.cost_quote.quoted_by_principal_id,
        allocator_policy_sha256=bundle.cost_quote.pricing_policy_sha256,
        qualification_principal_id=grant.authorized_by_principal_id,
        qualification_key_id=grant.authorization_key_id,
        qualification_policy_sha256=grant.qualification_authority_policy_sha256,
        node_enrollment_principal_id=enrollment.enrolled_by_principal_id,
        node_enrollment_key_id=enrollment.enrollment_authority_key_id,
        node_enrollment_policy_sha256=enrollment.node_enrollment_policy_sha256,
        node_execution_principal_id=manifest.principal_id,
        node_execution_key_id=manifest.node_signing_key_id,
        node_execution_policy_sha256=manifest.sandbox_policy_sha256,
        runtime_control_principal_id=accepted.accepted_by_principal_id,
        runtime_control_key_id=accepted.acceptance_key_id,
        runtime_control_policy_sha256=accepted.runtime_control_policy_sha256,
        terminal_submission_principal_id=manifest.principal_id,
        terminal_submission_key_id=manifest.node_signing_key_id,
        terminal_submission_policy_sha256=manifest.sandbox_policy_sha256,
        terminal_acceptance_principal_id=terminal.accepted_by_principal_id,
        terminal_acceptance_key_id=terminal.acceptance_key_id,
        terminal_acceptance_policy_sha256=terminal.runtime_control_policy_sha256,
        artifact_manifest_sha256=raw_run.artifact_manifest.manifest_sha256,
        output_tree_sha256=submission.output_tree_sha256,
        artifact_verified_receipt_sha256s=submission.artifact_verified_receipt_sha256s,
        artifact_manifest=raw_run.artifact_manifest,
        artifact_verified_receipts=raw_run.artifact_verified_receipts,
        verified_at=observed_at,
    )


@pytest.fixture
def raw_custody_case(tmp_path: Path) -> _RawCustodyCase:
    bridge = _bridge_case()
    artifact_store = LocalArtifactStore(
        tmp_path / "raw-cas",
        verifier_principal_id="principal:artifact-verifier",
        object_store_id="store:raw-custody-test",
    )
    raw_run = _raw_run_with_local_cas(
        case=bridge,
        artifact_store=artifact_store,
        output_root=tmp_path / "raw-output",
    )
    observed_at = raw_run.assembled_at + timedelta(seconds=1)
    authorization = raw_run.scientific_authorization.message
    allocator_authority = VerifiedExecutionAuthorityProjection(
        principal_id=authorization.qualification_bundle.cost_quote.quoted_by_principal_id,
        key_id=_digest("raw-custody-pricing-key"),
        policy_sha256=authorization.qualification_bundle.cost_quote.pricing_policy_sha256,
    )
    artifact_authority = VerifiedExecutionAuthorityProjection(
        principal_id=artifact_store.verifier_principal_id,
        key_id=_digest("raw-custody-artifact-key"),
        policy_sha256=_digest("raw-custody-artifact-policy"),
    )
    return _RawCustodyCase(
        bridge=bridge,
        raw_run=raw_run,
        artifact_store=artifact_store,
        sea_sessions=_registered_sea_sessions(
            raw_run,
            registered_at=authorization.authorized_at + timedelta(seconds=1),
        ),
        lineage=_run_lineage(
            case=bridge,
            raw_run=raw_run,
            observed_at=observed_at,
        ),
        allocator_authority=allocator_authority,
        artifact_authority=artifact_authority,
        observed_at=observed_at,
    )


def _raw_custody_adapter(
    case: _RawCustodyCase,
    *,
    lineage: VerifiedQualificationRunLineage | None = None,
    sea_sessions: sessionmaker[Session] | None = None,
) -> PostgreSQLRawRunCustodyVerificationAdapter:
    return PostgreSQLRawRunCustodyVerificationAdapter(
        execution_lineage=_RunLineageArchive(lineage or case.lineage),
        artifact_store=case.artifact_store,
        sea_sessions=sea_sessions or case.sea_sessions,
        allocator_authority=case.allocator_authority,
        artifact_authority=case.artifact_authority,
    )


def test_raw_run_custody_closes_registered_execution_and_fresh_cas(
    raw_custody_case: _RawCustodyCase,
) -> None:
    case = raw_custody_case
    projection = _raw_custody_adapter(case).verify_raw_run_custody(
        raw_run=case.raw_run,
        observed_at=case.observed_at,
    )

    assert projection.raw_run_sha256 == case.raw_run.raw_run_sha256
    assert projection.runtime_launch_sha256 == (
        case.raw_run.accepted_runtime_termination.node_runtime_launch_receipt_sha256
    )
    assert projection.fresh_artifacts[0].content_sha256 == (
        case.raw_run.artifact_manifest.entries[0].content_sha256
    )


def test_raw_run_custody_resolves_exact_qualification_admission(
    raw_custody_case: _RawCustodyCase,
) -> None:
    case = raw_custody_case
    authorization = case.raw_run.scientific_authorization.message
    adapter = _raw_custody_adapter(case)

    admitted = adapter.verify_qualification_admission(
        qualification_admission_sha256=case.raw_run.qualification_admission_sha256,
        bundle=authorization.qualification_bundle,
        grant=authorization.qualification_grant,
        observed_at=case.observed_at,
    )
    refreshed = adapter.verify_engineering_qualification_custody(
        bundle=authorization.qualification_bundle,
        grant=authorization.qualification_grant,
        observed_at=case.observed_at,
    )

    assert admitted == case.lineage.verified_engineering_qualification
    assert refreshed.model_dump(exclude={"verified_at"}) == admitted.model_dump(
        exclude={"verified_at"}
    )
    assert refreshed.verified_at == case.observed_at
    with pytest.raises(ObservationAdapterVerificationError, match="rebound"):
        adapter.verify_qualification_admission(
            qualification_admission_sha256=_digest("wrong-qualification-admission"),
            bundle=authorization.qualification_bundle,
            grant=authorization.qualification_grant,
            observed_at=case.observed_at,
        )


def test_raw_run_custody_rejects_late_sea_registration(
    raw_custody_case: _RawCustodyCase,
) -> None:
    case = raw_custody_case
    authorization = case.raw_run.scientific_authorization.message
    late_sessions = _registered_sea_sessions(
        case.raw_run,
        registered_at=authorization.authorized_at + timedelta(minutes=3),
    )

    with pytest.raises(ObservationAdapterVerificationError, match="before admission"):
        _raw_custody_adapter(case, sea_sessions=late_sessions).verify_raw_run_custody(
            raw_run=case.raw_run,
            observed_at=case.observed_at,
        )


@pytest.mark.parametrize("fault", ["attempt", "node", "terminal"])
def test_raw_run_custody_rejects_rebound_public_lineage(
    raw_custody_case: _RawCustodyCase,
    fault: str,
) -> None:
    case = raw_custody_case
    if fault == "attempt":
        updates = {"attempt_id": "iat_" + "f" * 32}
    elif fault == "node":
        updates = {"node_inventory_sha256": _digest("rebound-node-inventory")}
    else:
        updates = {"terminal_acceptance_sha256": _digest("rebound-terminal")}
    rebound = case.lineage.model_copy(update=updates)

    with pytest.raises(ObservationAdapterVerificationError):
        _raw_custody_adapter(case, lineage=rebound).verify_raw_run_custody(
            raw_run=case.raw_run,
            observed_at=case.observed_at,
        )


def test_raw_run_custody_fresh_rehash_rejects_artifact_byte_tamper(
    raw_custody_case: _RawCustodyCase,
) -> None:
    case = raw_custody_case
    entry = case.raw_run.artifact_manifest.entries[0]
    object_path = (
        case.artifact_store.root
        / "objects"
        / "sha256"
        / entry.content_sha256[:2]
        / entry.content_sha256
    )
    object_path.chmod(0o600)
    object_path.write_bytes(b"x" * entry.bytes)
    object_path.chmod(0o400)

    with pytest.raises(ObservationAdapterVerificationError):
        _raw_custody_adapter(case).verify_raw_run_custody(
            raw_run=case.raw_run,
            observed_at=case.observed_at,
        )
