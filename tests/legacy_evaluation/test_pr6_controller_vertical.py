from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.legacy_evaluation.capability import LegacyEvaluationCapability
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationRawResult,
    SignedLegacyEvaluationValidation,
)
from aletheia.legacy_evaluation.validator import validate_legacy_evaluation_raw_result
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
    ResearchControllerService,
)
from aletheia.scheduler.driver import ExperimentDriver

from conftest import NOW, SOURCE_ROOT, LegacyEvaluationCase, digest


class _CompatibilityLedger:
    def __init__(self, case: LegacyEvaluationCase) -> None:
        self.case = case
        self.raw_result: LegacyEvaluationRawResult | None = None
        self.validation: SignedLegacyEvaluationValidation | None = None

    def load(self, wakeup: ControllerWakeup) -> ControllerRecoveryProjection:
        return ControllerRecoveryProjection(
            quest_id=wakeup.quest_id,
            action_sha256=digest("pr6-authorized-action"),
            scientific_slot_id="sos_" + digest("pr6-scientific-slot")[:32],
            audited_stream_version=12,
            audited_tail_event_sha256=digest("pr6-kernel-tail"),
            audited_snapshot_sha256=digest("pr6-kernel-snapshot"),
            action_authorized=True,
            compilation_disposition=CompilationDisposition.ACCEPTED,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=self.raw_result is not None,
            validation_committed=self.validation is not None,
            admission_committed=False,
            observation_incorporated=False,
            continuation_committed=False,
            blocker_codes=(),
        )


class _CompatibilityExecutor:
    def __init__(self, *, ledger: _CompatibilityLedger, output_root: Path) -> None:
        self._ledger = ledger
        self._output_root = output_root

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan,
    ) -> ControllerStepReceipt:
        assert projection.projection_sha256 == plan.projection_sha256
        case = self._ledger.case
        if plan.step is ControllerStep.AWAIT_EXECUTION:
            timestamps = iter((NOW + timedelta(minutes=2), NOW + timedelta(minutes=3)))
            self._ledger.raw_result = LegacyEvaluationCapability(
                plugin=case.plugin,
                harness=case.harness,
                protocol_manifest=case.manifest,
                source_root=SOURCE_ROOT,
                clock=lambda: next(timestamps),
            ).execute(
                invocation=case.invocation,
                intent=case.intent,
                invocation_artifact_verified_receipt_sha256=case.invocation_receipt_sha256,
                input_table_path=case.input_table_path,
                output_root=self._output_root,
            )
            artifact_hashes = (self._ledger.raw_result.raw_result_sha256,)
        elif plan.step is ControllerStep.COMMIT_VALIDATION:
            assert self._ledger.raw_result is not None
            self._ledger.validation = validate_legacy_evaluation_raw_result(
                raw_result=self._ledger.raw_result,
                invocation=case.invocation,
                harness=case.harness,
                output_root=self._output_root,
                validator_pin=case.validator_pin,
                validated_at=NOW + timedelta(minutes=4),
                validator_private_key=case.validator_private_key,
            )
            artifact_hashes = (self._ledger.validation.receipt_sha256,)
        else:  # pragma: no cover - a future planner change must make this test fail loudly
            raise AssertionError(f"unexpected compatibility step {plan.step.value}")
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.COMPLETED,
            result_artifact_sha256s=artifact_hashes,
            blocker_codes=(),
        )


def test_new_controller_executes_and_validates_one_compatibility_leaf_after_restart(
    legacy_evaluation_case: LegacyEvaluationCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    optimize_calls = 0

    def forbidden_optimize(*_args: object, **_kwargs: object) -> None:
        nonlocal optimize_calls
        optimize_calls += 1
        raise AssertionError("PR-6 compatibility execution reached legacy optimize")

    monkeypatch.setattr(ExperimentDriver, "_optimize", forbidden_optimize)
    wakeup = ControllerWakeup(
        registration_id="rcr_" + digest("pr6-registration")[:32],
        quest_id=legacy_evaluation_case.intent.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:pr6-compatibility",
        source_sha256=digest("pr6-launch-source"),
    )
    ledger = _CompatibilityLedger(legacy_evaluation_case)
    executor = _CompatibilityExecutor(ledger=ledger, output_root=tmp_path / "outputs")

    first = ResearchControllerService(recovery=ledger, executor=executor).tick(wakeup)
    assert first.plan.step is ControllerStep.AWAIT_EXECUTION
    assert ledger.raw_result is not None
    assert first.step_receipt.legacy_optimize_used is False

    restarted = ResearchControllerService(recovery=ledger, executor=executor)
    second = restarted.tick(wakeup)
    assert second.plan.step is ControllerStep.COMMIT_VALIDATION
    assert ledger.validation is not None
    assert ledger.validation.message.eligible_for_independent_scientific_validation is True
    assert ledger.validation.message.scientific_outcome == "not_assessed"
    assert second.step_receipt.signed_kernel_command_committed is False
    assert second.step_receipt.independent_observation_admission_committed is False
    assert optimize_calls == 0
