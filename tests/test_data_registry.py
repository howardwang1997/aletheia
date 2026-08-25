"""Phase 1 step 2: DataAsset registry + profiler + readiness gate."""

from __future__ import annotations

import pandas as pd
import pytest

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


def test_composition_column_plumbs_to_data_spec():
    """Step 1a: an explicit composition_column survives register -> _to_dict -> resolve_data_spec,
    so the featurizer resolves it deterministically AND it appears in the JSON-dumped data_spec the
    design/demonstration authoring prompts see (no first-non-numeric-column guessing)."""
    from aletheia.compute.mcp_tools import resolve_data_spec

    create_all()
    run_id = create_run("comp-col plumbing", domain="materials", status="scoping")
    aid = register_dataset(
        run_id, source="upload", ref="superconduct_unique_m.csv",
        uri="artifacts/datasets/superconduct_unique_m.csv",
        target_column="critical_temp", composition_column="material",
        feature_kind="composition", status="ready",
    )
    d = next(x for x in list_datasets(run_id) if x["id"] == aid)
    assert d["composition_column"] == "material"          # carried through _to_dict
    spec = resolve_data_spec(run_id)
    assert spec["composition_column"] == "material"        # reaches the effective data_spec
    assert spec["target_column"] == "critical_temp"


def test_data_spec_omits_composition_column_when_absent():
    """The band-gap/benchmark path is unaffected: no composition_column key is injected when unset."""
    from aletheia.compute.mcp_tools import resolve_data_spec

    create_all()
    run_id = create_run("no comp-col", domain="materials", status="scoping")
    register_dataset(run_id, source="benchmark", ref="matbench_expt_gap",
                     target_column="gap expt", status="ready")
    spec = resolve_data_spec(run_id)
    assert "composition_column" not in spec


def test_external_asset_never_replaces_primary_data_spec():
    from aletheia.compute.mcp_tools import resolve_data_spec, resolve_external_data_spec

    create_all()
    run_id = create_run("role isolation", domain="materials", status="scoping")
    register_dataset(
        run_id,
        source="upload",
        role="external_validation",
        ref="external.csv",
        uri="external.csv",
        target_column="external_target",
        status="ready",
        content_sha256="e" * 64,
    )
    register_dataset(
        run_id,
        source="upload",
        role="primary",
        ref="primary.csv",
        uri="primary.csv",
        target_column="primary_target",
        status="ready",
        content_sha256="p" * 64,
    )
    assert resolve_data_spec(run_id)["target_column"] == "primary_target"
    ext = resolve_external_data_spec(run_id)
    assert ext is not None and ext["target_column"] == "external_target"
    assert ext["role"] == "external_validation"


@pytest.mark.asyncio
async def test_external_asset_profile_is_hidden_from_model_dataset_tool():
    from aletheia.orchestrator.tools import build_inspect_dataset_tool

    create_all()
    run_id = create_run("external profile isolation", domain="materials", status="scoping")
    external_id = register_dataset(
        run_id,
        source="upload",
        role="external_validation",
        ref="external.csv",
        uri="external.csv",
        target_column="secret_external_outcome",
        status="ready",
        profile_json={"columns": ["secret_external_outcome"], "n_rows": 99},
    )
    tool = build_inspect_dataset_tool(run_id)
    listing = await tool.handler({"asset_id": ""})
    direct = await tool.handler({"asset_id": external_id})
    assert "secret_external_outcome" not in listing["content"][0]["text"]
    assert "secret_external_outcome" not in direct["content"][0]["text"]
