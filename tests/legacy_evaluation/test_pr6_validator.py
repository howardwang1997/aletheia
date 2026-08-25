from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationValidationDisposition,
    SignedLegacyEvaluationValidation,
)
from aletheia.legacy_evaluation.validator import (
    LegacyEvaluationValidationError,
    validate_legacy_evaluation_raw_result,
    verify_signed_legacy_evaluation_validation,
)

from conftest import NOW, LegacyEvaluationRun


def _validate(run: LegacyEvaluationRun, output_root: Path):
    case = run.case
    return validate_legacy_evaluation_raw_result(
        raw_result=run.raw_result,
        invocation=case.invocation,
        harness=case.harness,
        output_root=output_root,
        validator_pin=case.validator_pin,
        validated_at=NOW + timedelta(minutes=4),
        validator_private_key=case.validator_private_key,
    )


def test_independent_validator_fresh_rehashes_and_signs_evaluator_only_receipt(
    legacy_evaluation_run: LegacyEvaluationRun,
) -> None:
    run = legacy_evaluation_run
    signed = _validate(run, run.output_root)

    assert signed.message.disposition is (
        LegacyEvaluationValidationDisposition.VALIDATED_RAW_ARTIFACT
    )
    assert signed.message.eligible_for_independent_scientific_validation is True
    assert signed.message.scientific_outcome == "not_assessed"
    assert signed.message.evaluator_only is True
    assert signed.message.writes_research_state is False
    assert signed.message.grants_scientific_admission is False
    assert signed.message.grants_claim_promotion is False
    assert len(signed.message.fresh_artifact_sha256s) == len(run.raw_result.artifacts) + 1
    assert (
        verify_signed_legacy_evaluation_validation(
            signed=signed,
            raw_result=run.raw_result,
            invocation=run.case.invocation,
            harness=run.case.harness,
            validator_pin=run.case.validator_pin,
        )
        == signed.message
    )


def test_eval_artifact_tamper_is_rejected_not_promoted(
    legacy_evaluation_run: LegacyEvaluationRun,
    tmp_path: Path,
) -> None:
    copied = tmp_path / "tampered"
    shutil.copytree(legacy_evaluation_run.output_root, copied)
    eval_path = copied / "eval.json"
    payload = eval_path.read_bytes()
    eval_path.write_bytes(payload.replace(b'"mae":', b'"mae_x":', 1))

    signed = _validate(legacy_evaluation_run, copied)

    assert signed.message.disposition is LegacyEvaluationValidationDisposition.REJECTED
    assert signed.message.eligible_for_independent_scientific_validation is False
    assert "artifact_content_changed" in signed.message.blocker_codes
    assert "metric_projection_changed" in signed.message.blocker_codes
    assert signed.message.scientific_outcome == "not_assessed"


def test_signed_validation_rejects_signature_tamper(
    legacy_evaluation_run: LegacyEvaluationRun,
) -> None:
    run = legacy_evaluation_run
    signed = _validate(run, run.output_root)
    replacement = ("0" if signed.signature_ed25519_hex[0] != "0" else "1") + (
        signed.signature_ed25519_hex[1:]
    )
    forged = SignedLegacyEvaluationValidation(
        message=signed.message,
        signature_ed25519_hex=replacement,
    )

    with pytest.raises(LegacyEvaluationValidationError, match="signature or binding"):
        verify_signed_legacy_evaluation_validation(
            signed=forged,
            raw_result=run.raw_result,
            invocation=run.case.invocation,
            harness=run.case.harness,
            validator_pin=run.case.validator_pin,
        )
