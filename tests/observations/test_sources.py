from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from pydantic import ValidationError

from aletheia.execution.allocator import VerifiedQualificationRawRunMaterial
from aletheia.observations import adapters as adapters_module
from aletheia.observations.adapters import (
    ObservationAdapterVerificationError,
    PostgreSQLCommittedObservationValidationSource,
    PostgreSQLRawRunEnvelopeSourceAdapter,
)
from aletheia.observations.store import (
    ObservationValidationReceiptWrite,
    ScientificExecutionAuthorizationWrite,
)

from test_scientific_bridge import (
    _bridge_case,
    _commit_validation,
    _raw_run,
    _validated_receipt,
)


@contextmanager
def _sessions():
    yield object()


class _MaterialArchive:
    def __init__(self, material):
        self.material = material
        self.calls = []

    def load_verified_qualification_raw_run_material(self, **scope):
        self.calls.append(scope)
        return self.material


def _material(raw_run, *, verified_at):
    authorization = raw_run.scientific_authorization.message
    intent = authorization.qualification_bundle.intent
    return VerifiedQualificationRawRunMaterial(
        execution_id=intent.execution_id,
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        intent_sha256=intent.intent_sha256,
        qualification_bundle_sha256=authorization.qualification_bundle.bundle_sha256,
        qualification_grant_sha256=authorization.qualification_grant.grant_sha256,
        qualification_admission_sha256=raw_run.qualification_admission_sha256,
        accepted_runtime_termination=raw_run.accepted_runtime_termination,
        terminal_submission=raw_run.terminal_submission,
        accepted_terminal_submission=raw_run.accepted_terminal_submission,
        artifact_manifest=raw_run.artifact_manifest,
        artifact_verified_receipts=raw_run.artifact_verified_receipts,
        verified_at=verified_at,
    )


def test_raw_run_source_rebuilds_exact_deterministic_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    original = _raw_run(case)
    verified_at = original.assembled_at + timedelta(seconds=1)
    material = _material(original, verified_at=verified_at)
    archive = _MaterialArchive(material)
    registration = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at + timedelta(seconds=1),
    )
    monkeypatch.setattr(
        adapters_module,
        "get_scientific_execution_authorization_by_slot",
        lambda *_args, **_kwargs: registration,
    )
    source = PostgreSQLRawRunEnvelopeSourceAdapter(
        execution_material=archive,
        sea_sessions=_sessions,
        database_clock=lambda _session: verified_at,
    )
    binding = case.authorization.message.action_protocol_binding

    first = source.load_raw_run(
        quest_id=binding.action.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=case.authorization.message.scientific_slot_id,
    )
    second = source.load_raw_run(
        quest_id=binding.action.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=case.authorization.message.scientific_slot_id,
    )

    expected_assembled_at = max(
        original.accepted_terminal_submission.accepted_at,
        *(item.verified_at for item in original.artifact_verified_receipts),
    )
    assert first == second
    assert first.raw_run_sha256 == second.raw_run_sha256
    assert first.assembled_at == expected_assembled_at
    assert first.scientific_authorization == case.authorization
    assert len(archive.calls) == 2


def test_raw_run_source_rejects_rebound_action_and_exported_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    original = _raw_run(case)
    verified_at = original.assembled_at + timedelta(seconds=1)
    registration = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at + timedelta(seconds=1),
    )
    monkeypatch.setattr(
        adapters_module,
        "get_scientific_execution_authorization_by_slot",
        lambda *_args, **_kwargs: registration,
    )
    source = PostgreSQLRawRunEnvelopeSourceAdapter(
        execution_material=_MaterialArchive(_material(original, verified_at=verified_at)),
        sea_sessions=_sessions,
        database_clock=lambda _session: verified_at,
    )
    binding = case.authorization.message.action_protocol_binding

    with pytest.raises(ObservationAdapterVerificationError, match="action and slot"):
        source.load_raw_run(
            quest_id=binding.action.quest_id,
            action_sha256="f" * 64,
            scientific_slot_id=case.authorization.message.scientific_slot_id,
        )

    rebound = _material(original, verified_at=verified_at).model_copy(
        update={"qualification_bundle_sha256": "e" * 64}
    )
    rebound_source = PostgreSQLRawRunEnvelopeSourceAdapter(
        execution_material=_MaterialArchive(rebound),
        sea_sessions=_sessions,
        database_clock=lambda _session: verified_at,
    )
    with pytest.raises(ObservationAdapterVerificationError, match="rebound"):
        rebound_source.load_raw_run(
            quest_id=binding.action.quest_id,
            action_sha256=binding.action.object_sha256,
            scientific_slot_id=case.authorization.message.scientific_slot_id,
        )


def test_raw_run_material_contract_rejects_terminal_hash_rebinding() -> None:
    case = _bridge_case()
    raw_run = _raw_run(case)
    material = _material(raw_run, verified_at=raw_run.assembled_at)
    rebound = material.terminal_submission.model_copy(update={"artifact_manifest_sha256": "d" * 64})

    with pytest.raises(ValidationError, match="raw-run material is rebound"):
        VerifiedQualificationRawRunMaterial.model_validate(
            {
                **material.model_dump(mode="python"),
                "terminal_submission": rebound,
            }
        )


def test_committed_validation_source_rehashes_row_and_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    validation = _validated_receipt(case)
    committed = _commit_validation(case, validation)
    binding = case.authorization.message.action_protocol_binding
    write = ObservationValidationReceiptWrite.from_contract(
        committed,
        quest_id=binding.action.quest_id,
    )
    monkeypatch.setattr(
        adapters_module,
        "get_observation_validation_receipt_by_slot",
        lambda *_args, **_kwargs: write,
    )
    source = PostgreSQLCommittedObservationValidationSource(sessions=_sessions)

    assert (
        source.load_committed_validation(
            quest_id=binding.action.quest_id,
            action_sha256=binding.action.object_sha256,
            scientific_slot_id=case.authorization.message.scientific_slot_id,
        )
        == committed
    )
    with pytest.raises(ObservationAdapterVerificationError, match="rebound"):
        source.load_committed_validation(
            quest_id=binding.action.quest_id,
            action_sha256="f" * 64,
            scientific_slot_id=case.authorization.message.scientific_slot_id,
        )
