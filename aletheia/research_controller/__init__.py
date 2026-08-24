"""Durable operational controller for the authoritative Research Kernel.

The package may wake and reconcile work, but it never bypasses signed Kernel commands or
independent observation admission.
"""

from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchReceipt,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    ResearchControllerTaskInput,
    controller_task_spec,
    plan_recovery_tick,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    continuation_to_action_kind,
    derive_continuation_v2,
)

__all__ = [
    "CONTROLLER_TASK_TYPE",
    "CompilationDisposition",
    "ControllerRecoveryProjection",
    "ControllerStep",
    "ControllerTickPlan",
    "ControllerWakeup",
    "ControllerWakeupKind",
    "ContinuationDisposition",
    "ContinuationReceipt",
    "HypothesisPredictionAssessment",
    "PredictionFit",
    "ResearchControllerLaunchReceipt",
    "ResearchControllerLaunchRequest",
    "ResearchControllerManifest",
    "ResearchControllerRegistration",
    "ResearchControllerTaskInput",
    "ScientificObservationProjection",
    "continuation_to_action_kind",
    "controller_task_spec",
    "derive_continuation_v2",
    "plan_recovery_tick",
]
