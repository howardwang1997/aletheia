"""Hermetic materials capability-registry fixtures built from tracked manifests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from aletheia.capabilities import (
    CapabilityRegistrySnapshot,
    ExperimentCapabilityManifest,
    build_capability_registry_snapshot,
)


_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_NAMES = (
    "materials_band_gap_range_compression_provisional_v1.yaml",
    "materials_band_gap_range_compression_provisional_v2.yaml",
    "materials_band_gap_range_compression_provisional_v2_1.yaml",
    "materials_ase_emt_eos_reference_provisional_v1.yaml",
)


@pytest.fixture(scope="session")
def materials_registry_v4() -> CapabilityRegistrySnapshot:
    """Rebuild the committed v4 registry without relying on ignored workspace output."""

    manifests = tuple(
        ExperimentCapabilityManifest.model_validate(
            yaml.safe_load((_ROOT / "configs/capabilities" / name).read_text(encoding="utf-8"))
        )
        for name in _MANIFEST_NAMES
    )
    return build_capability_registry_snapshot(
        registry_id="materials-capabilities-v4",
        manifests=manifests,
        created_at=datetime(2026, 8, 15, 9, 39, 9, 739376, tzinfo=timezone.utc),
    )
