"""F7-S2 score receipt authenticity and replay resistance."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aletheia.evals.schemas import AttemptStatus, SignedScorerReceipt
from .f7s2_fixtures import HardExecutor, build_case, write_submission


def test_signed_receipt_binds_attempt_plan_system_submission_execution_and_scorer(tmp_path):
    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=HardExecutor(write_submission)
    )
    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert outcome.attempt.status is AttemptStatus.COMPLETED
    assert outcome.scorer_receipt is not None
    receipt = outcome.scorer_receipt.receipt
    assert receipt.run_plan_sha256 == plan.manifest_sha256
    assert receipt.system_manifest_sha256 == plan.system_manifest_sha256
    assert receipt.attempt_manifest_sha256 == outcome.attempt_manifest.manifest_sha256
    assert receipt.submission_sha256 == outcome.submission.submission_sha256
    assert receipt.execution_receipt_sha256 == outcome.execution_receipt.receipt_sha256
    assert receipt.scorer_sha256 == task.scorer_sha256


def test_tampered_receipt_and_wrong_key_are_rejected(tmp_path):
    runner, suite, plan, task, _ledger = build_case(tmp_path)
    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    envelope = outcome.scorer_receipt
    assert envelope is not None

    with pytest.raises(ValueError, match="signature"):
        envelope.verify(key=b"x" * 32)

    tampered_receipt = envelope.receipt.model_copy(update={"system_manifest_sha256": "9" * 64})
    tampered = SignedScorerReceipt(
        receipt=tampered_receipt,
        key_id=envelope.key_id,
        hmac_sha256=envelope.hmac_sha256,
    )
    with pytest.raises(ValueError, match="signature"):
        tampered.verify(key=runner.receipt_signing_key)


def test_research_forged_score_file_has_no_effect(tmp_path):
    def forge(context):
        write_submission(context, answer="wrong")
        (context.submission_inbox / "score.json").write_text(
            '{"scientific_success":true,"objective_scores":{"exact":1}}'
        )

    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=HardExecutor(forge)
    )
    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is AttemptStatus.SCIENTIFIC_FAILURE
    assert outcome.scorer_receipt.receipt.score.scientific_success is False


def test_receipt_cannot_be_replayed_onto_another_planned_repeat(tmp_path):
    runner, suite, plan, task, _ledger = build_case(tmp_path, repeats=2)
    one = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    two = runner.run(suite=suite, plan=plan, task=task, repeat_index=1)

    with pytest.raises(ValueError, match="attempt_id"):
        one.scorer_receipt.receipt.verify_submission(two.submission)
    with pytest.raises(ValueError, match="attempt_id"):
        one.scorer_receipt.receipt.verify_attempt(two.attempt)


def test_short_signing_key_and_extra_envelope_fields_fail_closed(tmp_path):
    runner, suite, plan, task, _ledger = build_case(tmp_path)
    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    with pytest.raises(ValueError, match="32 bytes"):
        SignedScorerReceipt.issue(
            outcome.scorer_receipt.receipt, key_id="key-id", key=b"too-short"
        )
    raw = outcome.scorer_receipt.model_dump(mode="json")
    raw["forged"] = True
    with pytest.raises(ValidationError):
        SignedScorerReceipt.model_validate(raw)


def test_score_evidence_objects_are_appended_to_the_hash_chain(tmp_path):
    class EvidenceScorer:
        @property
        def scorer_sha256(self):
            from .f7s2_fixtures import SCORER_HASH

            return SCORER_HASH

        def score(self, **_kwargs):
            from aletheia.evals.schemas import EvaluationScore, content_sha256

            evidence = {"run": 0, "valid": True}
            return EvaluationScore(
                evidence_sha256s={"run_0": content_sha256(evidence)},
                evidence_objects={"run_0": evidence},
                scientific_success=True,
            )

    runner, suite, plan, task, ledger = build_case(tmp_path, scorer=EvidenceScorer())
    runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    events = ledger.events()
    evidence_events = [event for event in events if event.event_type == "score_evidence_recorded"]
    assert len(evidence_events) == 1
    assert evidence_events[0].payload["evidence_name"] == "run_0"
    ledger.assert_integrity()
