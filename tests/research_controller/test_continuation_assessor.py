from __future__ import annotations

import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from aletheia.research_controller.continuation import (
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
    ContinuationDisposition,
    ContinuationReceipt,
    PredictionFit,
    exact_outcome_bin_prediction_sha256,
)
from aletheia.research_controller.continuation_assessor import (
    EXACT_OUTCOME_BIN_FIT_RULE_SHA256,
    ContinuationAssessmentArtifactError,
    ExactOutcomeBinContinuationAssessor,
    WriteOnceContinuationAssessmentArtifactArchive,
    build_exact_outcome_fit_assessment_artifact,
)
from aletheia.research_controller.continuation_step import (
    ContinuationAssessmentPolicyPin,
    DurableContinuationAssessmentService,
)
from aletheia.research_controller.contracts import plan_recovery_tick

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "observations",
    _TESTS / "protocols",
    _TESTS / "research_controller",
):
    sys.path.insert(0, str(_fixture_dir))

from test_continuation_step import (  # noqa: E402
    _Archive,
    _FailProvider,
    _Kernel,
    _binding,
    _projection,
    _seed,
    _sessions,
    _source,
    _wakeup,
)
from persistence_test_support import sqlite_observation_engine  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _artifact_archive(root: Path) -> WriteOnceContinuationAssessmentArtifactArchive:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    metadata = root.stat()
    return WriteOnceContinuationAssessmentArtifactArchive(
        root.resolve(),
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        device_id=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _policy() -> ContinuationAssessmentPolicyPin:
    return ContinuationAssessmentPolicyPin(
        assessment_implementation_sha256=_sha("exact-outcome-bin-assessor-v1"),
        observed_outcome_identity_policy_sha256=OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
        allowed_assessor_principal_ids=("service:continuation-assessor",),
        allowed_fit_rule_sha256s=(EXACT_OUTCOME_BIN_FIT_RULE_SHA256,),
    )


def _context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source = _source(monkeypatch)
    engine = sqlite_observation_engine()
    _seed(engine, source)
    policy = _policy()
    archive = _artifact_archive(tmp_path / "artifacts")
    assessor = ExactOutcomeBinContinuationAssessor(
        policy=policy,
        principal_id=policy.allowed_assessor_principal_ids[0],
        implementation_sha256=policy.assessment_implementation_sha256,
        artifacts=archive,
        clock=lambda: source.event.committed_at + timedelta(seconds=1),
    )
    service = DurableContinuationAssessmentService(
        kernel_store=_Kernel(source.audit),
        object_archive=_Archive(source),
        provider=assessor,
        artifact_custody=archive,
        assessment_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: source.event.committed_at + timedelta(seconds=3),
    )
    projection = _projection(source)
    with Session(engine) as session:
        context = service._context(  # noqa: SLF001 - verifies the concrete provider boundary
            session=session,
            wakeup=_wakeup(source),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    return source, engine, policy, archive, assessor, service, projection, context


def test_exact_outcome_bins_produce_fork_and_fresh_artifact_custody(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, engine, policy, archive, _assessor, service, projection, context = _context(
        monkeypatch,
        tmp_path,
    )
    assert context.observation.observed_outcome_bin_id == "outcome.negative"
    assert context.observation.admissible_outcome_bin_ids == (
        "outcome.inconclusive",
        "outcome.negative",
        "outcome.positive",
    )

    write = service.derive_and_register(
        wakeup=_wakeup(source),
        projection=projection,
        plan=plan_recovery_tick(projection),
    )
    assert write.disposition == ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED.value
    assessments = ContinuationReceipt.model_validate(write.receipt_json).assessments
    assert assessments
    assert {item.prediction_fit for item in assessments} == {PredictionFit.OUT_OF_SUPPORT}
    archive.verify_assessment_artifacts(context=context, assessments=assessments)

    restarted = DurableContinuationAssessmentService(
        kernel_store=_Kernel(source.audit),
        object_archive=_Archive(source),
        provider=_FailProvider(),
        artifact_custody=archive,
        assessment_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: source.event.committed_at + timedelta(seconds=10),
    )
    assert (
        restarted.derive_and_register(
            wakeup=_wakeup(source),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
        == write
    )


def test_opaque_or_ambiguous_prediction_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    *_prefix, context = _context(monkeypatch, tmp_path)
    hypothesis = next(
        item for item in context.world_model.hypotheses if item.lifecycle.value == "active"
    )
    original = next(
        item
        for item in context.world_model.predictions
        if item.hypothesis_sha256 == hypothesis.hypothesis_sha256
    )
    opaque = original.model_copy(update={"predicted_outcome_sha256": _sha("opaque-schema")})
    opaque_model = context.world_model.model_copy(
        update={
            "predictions": tuple(
                opaque if item == original else item for item in context.world_model.predictions
            )
        }
    )
    opaque_context = context.model_copy(update={"world_model": opaque_model})
    artifact = build_exact_outcome_fit_assessment_artifact(
        opaque_context,
        hypothesis_sha256=hypothesis.hypothesis_sha256,
    )
    assert artifact is not None
    assert artifact.prediction_fit is PredictionFit.INDETERMINATE
    assert artifact.resolved_predicted_outcome_bin_id is None

    duplicate = original.model_copy(
        update={
            "prediction_id": "pred_" + _sha("ambiguous-prediction")[:32],
            "predicted_outcome_sha256": _sha("other-opaque-schema"),
        }
    )
    ambiguous_context = context.model_copy(
        update={
            "world_model": context.world_model.model_copy(
                update={"predictions": (*context.world_model.predictions, duplicate)}
            )
        }
    )
    artifact = build_exact_outcome_fit_assessment_artifact(
        ambiguous_context,
        hypothesis_sha256=hypothesis.hypothesis_sha256,
    )
    assert artifact is not None
    assert artifact.prediction_fit is PredictionFit.INDETERMINATE
    assert len(artifact.exact_context_prediction_sha256s) == 2


def test_recognized_observed_bin_is_in_support(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    *_prefix, context = _context(monkeypatch, tmp_path)
    hypothesis = next(
        item for item in context.world_model.hypotheses if item.lifecycle.value == "active"
    )
    prediction = next(
        item
        for item in context.world_model.predictions
        if item.hypothesis_sha256 == hypothesis.hypothesis_sha256
    )
    predicted_bin = next(
        outcome_bin_id
        for outcome_bin_id in context.observation.admissible_outcome_bin_ids
        if exact_outcome_bin_prediction_sha256(
            observable_spec_sha256=context.observation.observable_spec_sha256,
            measurement_protocol_sha256=context.observation.measurement_protocol_sha256,
            outcome_space_sha256=context.observation.outcome_space_sha256,
            outcome_bin_id=outcome_bin_id,
        )
        == prediction.predicted_outcome_sha256
    )
    same_bin_context = context.model_copy(
        update={
            "observation": context.observation.model_copy(
                update={"observed_outcome_bin_id": predicted_bin}
            )
        }
    )
    artifact = build_exact_outcome_fit_assessment_artifact(
        same_bin_context,
        hypothesis_sha256=hypothesis.hypothesis_sha256,
    )
    assert artifact is not None
    assert artifact.prediction_fit is PredictionFit.IN_SUPPORT
    assert artifact.resolved_predicted_outcome_bin_id == predicted_bin


def test_prediction_identity_rejects_untyped_inputs() -> None:
    with pytest.raises(ValueError, match="inputs are invalid"):
        exact_outcome_bin_prediction_sha256(
            observable_spec_sha256="not-a-hash",
            measurement_protocol_sha256="1" * 64,
            outcome_space_sha256="2" * 64,
            outcome_bin_id="outcome.negative",
        )


def test_restart_rehash_rejects_same_path_artifact_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source_value, _engine, _policy_pin, archive, assessor, _service, _projection_value, context = (
        _context(monkeypatch, tmp_path)
    )
    prepared = assessor.assess_continuation(context)
    target_hash = prepared.assessments[0].assessment_artifact_sha256
    target = archive.root / "sha256" / target_hash[:2] / target_hash
    payload = target.read_bytes()
    target.chmod(0o600)
    target.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    target.chmod(0o400)

    with pytest.raises(ContinuationAssessmentArtifactError, match="bytes changed"):
        archive.verify_assessment_artifacts(
            context=context,
            assessments=prepared.assessments,
        )


def test_concurrent_exact_artifact_publication_converges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        _source_value,
        _engine,
        _policy_pin,
        archive,
        _assessor,
        _service,
        _projection_value,
        context,
    ) = _context(monkeypatch, tmp_path)
    hypothesis = next(
        item for item in context.world_model.hypotheses if item.lifecycle.value == "active"
    )
    artifact = build_exact_outcome_fit_assessment_artifact(
        context,
        hypothesis_sha256=hypothesis.hypothesis_sha256,
    )
    assert artifact is not None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: archive.put_once(artifact), range(32)))

    assert set(results) == {artifact.artifact_sha256}
    assert archive.load(artifact.artifact_sha256) == artifact


def test_artifact_archive_rejects_rebound_root_identity(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    archive = _artifact_archive(root)
    os.chmod(root, 0o750)
    with pytest.raises(ContinuationAssessmentArtifactError, match="custody pin"):
        archive.load("0" * 64)
