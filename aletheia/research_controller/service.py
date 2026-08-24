"""Restart-safe one-tick controller service.

Each invocation reconstructs its plan from authoritative Kernel audit and durable receipts through
the projection port.  The service retains no scientific checkpoint and performs at most one typed
step, so lease retry and duplicate outbox delivery are safe at this boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ControllerStepDisposition(str, Enum):
    COMPLETED = "completed"
    AWAITING_AUTHORITY = "awaiting_authority"
    AWAITING_EXTERNAL_RESULT = "awaiting_external_result"
    BLOCKED = "blocked"


class ControllerStepReceipt(ControllerModel):
    """One replayable operational step result, never independent scientific authority."""

    schema_name: Literal["aletheia.research_controller_step_receipt"] = (
        "aletheia.research_controller_step_receipt"
    )
    schema_version: Literal[1] = 1
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: ControllerStepDisposition
    result_artifact_sha256s: tuple[str, ...] = Field(max_length=64)
    blocker_codes: tuple[str, ...] = Field(max_length=64)
    signed_kernel_command_committed: bool = False
    independent_observation_admission_committed: bool = False
    direct_kernel_mutation_used: Literal[False] = False
    legacy_optimize_used: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_canonical(self) -> "ControllerStepReceipt":
        if self.result_artifact_sha256s != tuple(sorted(set(self.result_artifact_sha256s))):
            raise ValueError("controller result artifacts must be unique and canonical")
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ValueError("controller blocker codes must be unique and canonical")
        if (self.disposition is ControllerStepDisposition.BLOCKED) != bool(self.blocker_codes):
            raise ValueError("only a blocked controller step may carry blocker codes")
        authoritative_commit = (
            self.signed_kernel_command_committed or self.independent_observation_admission_committed
        )
        if authoritative_commit and self.disposition is not ControllerStepDisposition.COMPLETED:
            raise ValueError("only a completed controller step may claim an authoritative commit")
        if (
            self.independent_observation_admission_committed
            and not self.signed_kernel_command_committed
        ):
            raise ValueError("independent observation admission requires its atomic Kernel command")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerTickReceipt(ControllerModel):
    schema_name: Literal["aletheia.research_controller_tick_receipt"] = (
        "aletheia.research_controller_tick_receipt"
    )
    schema_version: Literal[1] = 1
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    recovery_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan: ControllerTickPlan
    step_receipt: ControllerStepReceipt

    @model_validator(mode="after")
    def _chain_matches(self) -> "ControllerTickReceipt":
        if self.plan.projection_sha256 != self.recovery_projection_sha256:
            raise ValueError("controller plan differs from its recovered projection")
        if self.step_receipt.wakeup_sha256 != self.wakeup_sha256:
            raise ValueError("controller step receipt belongs to another wakeup")
        if self.step_receipt.plan_sha256 != self.plan.plan_sha256:
            raise ValueError("controller step receipt belongs to another plan")
        admission_commit = self.step_receipt.independent_observation_admission_committed
        if admission_commit and self.plan.step is not ControllerStep.COMMIT_ADMISSION:
            raise ValueError("only the admission step may claim observation admission")
        if (
            self.plan.step is ControllerStep.COMMIT_ADMISSION
            and self.step_receipt.disposition is ControllerStepDisposition.COMPLETED
            and not (self.step_receipt.signed_kernel_command_committed and admission_commit)
        ):
            raise ValueError(
                "completed admission must atomically commit its Kernel command and observation"
            )
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerRecoveryProjectionPort(Protocol):
    """Rebuild the current step exclusively from authoritative/durable state."""

    def load(self, wakeup: ControllerWakeup) -> ControllerRecoveryProjection: ...


class ControllerStepExecutionPort(Protocol):
    """Execute one idempotent typed step through its dedicated authority adapter."""

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt: ...


class ResearchControllerService:
    """Stateless deterministic controller composition."""

    def __init__(
        self,
        *,
        recovery: ControllerRecoveryProjectionPort,
        executor: ControllerStepExecutionPort,
    ) -> None:
        self._recovery = recovery
        self._executor = executor

    def tick(self, wakeup: ControllerWakeup) -> ControllerTickReceipt:
        projection = self._recovery.load(wakeup)
        if projection.quest_id != wakeup.quest_id:
            raise ValueError("controller recovery projection belongs to another Quest")
        plan = plan_recovery_tick(projection)
        step_receipt = self._executor.execute(wakeup=wakeup, plan=plan)
        return ControllerTickReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            recovery_projection_sha256=projection.projection_sha256,
            plan=plan,
            step_receipt=step_receipt,
        )


__all__ = [
    "ControllerRecoveryProjectionPort",
    "ControllerStepDisposition",
    "ControllerStepExecutionPort",
    "ControllerStepReceipt",
    "ControllerTickReceipt",
    "ResearchControllerService",
]
