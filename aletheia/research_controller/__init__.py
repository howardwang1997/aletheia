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
    EXACT_OUTCOME_BIN_PREDICTION_POLICY_SHA256,
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
    ContinuationAssessmentProvenance,
    ContinuationDisposition,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    continuation_assessment_source_sha256,
    continuation_to_action_kind,
    derive_continuation_v2,
    exact_outcome_bin_prediction_sha256,
    project_admitted_scientific_observation,
)

__all__ = [
    "CONTROLLER_TASK_TYPE",
    "CompilationDisposition",
    "ContinuationAssessmentProvenance",
    "ControllerRecoveryProjection",
    "ControllerStep",
    "ControllerTickPlan",
    "ControllerWakeup",
    "ControllerWakeupKind",
    "ContinuationDisposition",
    "ContinuationReceipt",
    "EXACT_OUTCOME_BIN_PREDICTION_POLICY_SHA256",
    "HypothesisPredictionAssessment",
    "OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256",
    "PredictionFit",
    "ResearchControllerLaunchReceipt",
    "ResearchControllerLaunchRequest",
    "ResearchControllerManifest",
    "ResearchControllerRegistration",
    "ResearchControllerTaskInput",
    "ScientificObservationProjection",
    "continuation_assessment_source_sha256",
    "continuation_to_action_kind",
    "controller_task_spec",
    "derive_continuation_v2",
    "exact_outcome_bin_prediction_sha256",
    "plan_recovery_tick",
    "project_admitted_scientific_observation",
]
