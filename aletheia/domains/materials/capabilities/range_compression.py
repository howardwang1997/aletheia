"""Observation parser for the provisional band-gap range-compression capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aletheia.domains.materials.k3_evidence import MaterialsExperimentResult


def parse_range_compression_observation(
    payload: MaterialsExperimentResult | Mapping[str, Any],
) -> MaterialsExperimentResult:
    """Parse one exact executor result without dropping failures or unknown fields."""

    if isinstance(payload, MaterialsExperimentResult):
        return payload
    return MaterialsExperimentResult.model_validate(dict(payload))


__all__ = ["parse_range_compression_observation"]
