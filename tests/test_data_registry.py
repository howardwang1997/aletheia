"""Phase 1 step 2: DataAsset registry + profiler + readiness gate."""

from __future__ import annotations

import pandas as pd

from aletheia.data.registry import (
    all_ready,
    attach_upload,
    list_datasets,
    mark_ready,
    pending_datasets,
    register_dataset,
)
from aletheia.db import create_all
from aletheia.memory.service import create_run


def test_profile_and_readiness(tmp_path):
    create_all()
    run_id = create_run("data-registry test", domain="materials", status="scoping")

    # --- push scenario: human uploads a tiny composition dataset ---
    csv = tmp_path / "bandgaps.csv"
    pd.DataFrame(
        {"composition": ["GaAs", "Si", "ZnO", "NaCl"], "band_gap": [1.42, 1.12, 3.37, 8.5]}
    ).to_csv(csv, index=False)

    asset = attach_upload(run_id, str(csv), target_column="band_gap")
    assert asset is not None
    assert asset["status"] == "ready"
    prof = asset["profile"]
    assert prof["n_rows"] == 4
    assert set(prof["columns"]) == {"composition", "band_gap"}
    assert "band_gap" in prof["target_candidates"]
    assert "composition" in prof["composition_candidates"]
    # readiness gate passes once the only declared dataset is ready
    assert all_ready(run_id) is True

    # --- pull scenario: agent requests a benchmark that isn't satisfied yet ---
    needed_id = register_dataset(
        run_id,
        source="benchmark",
        ref="matbench_expt_gap",
        status="needed",
        requested_by="agent",
        description="canonical experimental band-gap folds",
    )
    assert all_ready(run_id) is False
    assert any(d["id"] == needed_id for d in pending_datasets(run_id))

    # human satisfies it -> gate opens again
    mark_ready(needed_id)
    assert all_ready(run_id) is True
    assert len(list_datasets(run_id)) == 2
