"""Materials-domain public APIs."""

from __future__ import annotations

from typing import Any

_IDENTITY_EXPORTS = frozenset(
    {
        "FormulaIdentity",
        "LicensedSourceArtifact",
        "MaterialIdentityLevel",
        "MaterialRecordIdentity",
        "MaterialSplitLedger",
        "MaterialSplitPolicy",
        "SampleIdentity",
        "StructureIdentity",
        "SynthesisBatchIdentity",
        "build_material_split_ledger",
        "build_structure_identity_from_cif",
        "normalize_formula",
    }
)
_K3_EVIDENCE_EXPORTS = frozenset(
    {
        "MaterialsBeliefUpdate",
        "MaterialsChainDisposition",
        "MaterialsK3EvidenceBundle",
        "MaterialsK3Protocol",
        "MaterialsPreregistration",
        "SignedMaterialsObservation",
        "SignedMaterialsValidation",
        "assemble_materials_evidence_bundle",
        "build_materials_preregistration",
        "derive_materials_belief_update",
        "derive_materials_scientific_decision",
        "measure_materials_experiment",
        "validate_materials_observation",
        "verify_materials_evidence_bundle",
    }
)
_MEASUREMENT_EXPORTS = frozenset(
    {
        "MaterialMeasurementAudit",
        "MaterialMeasurementAuditPolicy",
        "MaterialMeasurementRecord",
        "audit_material_measurements",
    }
)
_MECHANISTIC_CAMPAIGN_EXPORTS = frozenset(
    {
        "CapabilityReadinessItem",
        "ConfirmationIndependenceKind",
        "FreshConfirmationReservation",
        "MechanisticCampaignDecision",
        "MechanisticCampaignDisposition",
        "MechanisticCampaignEvidenceBundle",
        "MechanisticCampaignProtocol",
        "MechanisticCampaignReadinessAudit",
        "MechanisticCapabilityQualification",
        "MechanisticClaimCeiling",
        "MechanisticDecisionPolicy",
        "MechanisticEvidenceRole",
        "MechanisticExperimentFamily",
        "MechanisticExperimentSlot",
        "MechanisticScenarioLikelihood",
        "MechanisticSlotAssessment",
        "MechanisticSlotEvidence",
        "OutcomeMappingManifest",
        "build_mechanistic_campaign_protocol",
        "build_mechanistic_campaign_readiness_audit",
        "evaluate_mechanistic_campaign",
    }
)
_SIMULATION_EXPORTS = frozenset(
    {
        "AseEmtEosJob",
        "AseEmtSimulationBundle",
        "AseEmtSimulationProtocol",
        "SimulationExecutionStatus",
        "SimulationRawRun",
        "SimulationReproductionReceipt",
        "SimulationValidation",
        "SimulationValidationDisposition",
        "assemble_ase_emt_simulation_bundle",
        "compare_ase_emt_simulation_reproduction",
        "parse_ase_emt_simulation",
        "validate_ase_emt_simulation",
    }
)
_STRUCTURE_EXPORTS = frozenset(
    {
        "StructureDatasetQualityDisposition",
        "StructureDatasetQualityLedger",
        "StructureFeatureMatrixReceipt",
        "StructureGeometryFeaturePolicy",
        "StructureQualityPolicy",
        "StructureRowDisposition",
        "StructureRowReceipt",
        "build_structure_feature_matrix",
        "build_structure_quality_ledger",
        "extract_structure_geometry_features",
        "inspect_structure_row",
        "structure_geometry_feature_names",
    }
)


def __getattr__(name: str) -> Any:
    """Preserve the public facade without importing every materials control surface eagerly."""

    if name in _IDENTITY_EXPORTS:
        import aletheia.domains.materials.identity as module
    elif name in _K3_EVIDENCE_EXPORTS:
        import aletheia.domains.materials.k3_evidence as module
    elif name in _MEASUREMENT_EXPORTS:
        import aletheia.domains.materials.measurements as module
    elif name in _MECHANISTIC_CAMPAIGN_EXPORTS:
        import aletheia.domains.materials.mechanistic_campaign as module
    elif name in _SIMULATION_EXPORTS:
        import aletheia.domains.materials.simulation as module
    elif name in _STRUCTURE_EXPORTS:
        import aletheia.domains.materials.structures as module
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


__all__ = [
    "AseEmtEosJob",
    "AseEmtSimulationBundle",
    "AseEmtSimulationProtocol",
    "CapabilityReadinessItem",
    "ConfirmationIndependenceKind",
    "FormulaIdentity",
    "FreshConfirmationReservation",
    "LicensedSourceArtifact",
    "MaterialsBeliefUpdate",
    "MaterialsChainDisposition",
    "MaterialsK3EvidenceBundle",
    "MaterialsK3Protocol",
    "MaterialsPreregistration",
    "MaterialIdentityLevel",
    "MaterialMeasurementAudit",
    "MaterialMeasurementAuditPolicy",
    "MaterialMeasurementRecord",
    "MaterialRecordIdentity",
    "MaterialSplitLedger",
    "MaterialSplitPolicy",
    "MechanisticCampaignDecision",
    "MechanisticCampaignDisposition",
    "MechanisticCampaignEvidenceBundle",
    "MechanisticCampaignProtocol",
    "MechanisticCampaignReadinessAudit",
    "MechanisticCapabilityQualification",
    "MechanisticClaimCeiling",
    "MechanisticDecisionPolicy",
    "MechanisticEvidenceRole",
    "MechanisticExperimentFamily",
    "MechanisticExperimentSlot",
    "MechanisticScenarioLikelihood",
    "MechanisticSlotAssessment",
    "MechanisticSlotEvidence",
    "OutcomeMappingManifest",
    "SampleIdentity",
    "SimulationExecutionStatus",
    "SimulationRawRun",
    "SimulationReproductionReceipt",
    "SimulationValidation",
    "SimulationValidationDisposition",
    "SignedMaterialsObservation",
    "SignedMaterialsValidation",
    "StructureIdentity",
    "StructureDatasetQualityDisposition",
    "StructureDatasetQualityLedger",
    "StructureFeatureMatrixReceipt",
    "StructureGeometryFeaturePolicy",
    "StructureQualityPolicy",
    "StructureRowDisposition",
    "StructureRowReceipt",
    "SynthesisBatchIdentity",
    "assemble_materials_evidence_bundle",
    "audit_material_measurements",
    "assemble_ase_emt_simulation_bundle",
    "build_material_split_ledger",
    "build_mechanistic_campaign_protocol",
    "build_mechanistic_campaign_readiness_audit",
    "build_materials_preregistration",
    "build_structure_identity_from_cif",
    "build_structure_feature_matrix",
    "build_structure_quality_ledger",
    "derive_materials_belief_update",
    "derive_materials_scientific_decision",
    "compare_ase_emt_simulation_reproduction",
    "measure_materials_experiment",
    "normalize_formula",
    "parse_ase_emt_simulation",
    "extract_structure_geometry_features",
    "evaluate_mechanistic_campaign",
    "inspect_structure_row",
    "structure_geometry_feature_names",
    "validate_materials_observation",
    "validate_ase_emt_simulation",
    "verify_materials_evidence_bundle",
]
