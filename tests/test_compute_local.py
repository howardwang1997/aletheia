"""Phase 1 step 4: local compute backend submits a real subprocess job and
finalizes metrics/artifacts into the ledger; plus the compute-dry-run path."""

from __future__ import annotations

import time

import pandas as pd

from aletheia.compute.base import JobSpec
from aletheia.compute.local import LocalBackend
from aletheia.db import create_all
from aletheia.memory.service import finalize_plan, list_artifacts, list_metrics
from aletheia.memory.service import create_run

_COMPS = [
    ("Si", 1.12), ("Ge", 0.67), ("GaAs", 1.42), ("GaN", 3.4), ("ZnO", 3.37),
    ("ZnS", 3.6), ("CdTe", 1.49), ("CdS", 2.42), ("NaCl", 8.5), ("MgO", 7.8),
    ("TiO2", 3.2), ("SiC", 3.0), ("AlN", 6.2), ("InP", 1.35), ("InAs", 0.36),
    ("PbS", 0.37), ("Cu2O", 2.1), ("Fe2O3", 2.2), ("SnO2", 3.6), ("WO3", 2.7),
]


def _make_experiment() -> tuple[str, str]:
    create_all()
    run_id = create_run("compute test", domain="materials", status="scoping")
    exp_id = finalize_plan(run_id, {"objective": "bandgap", "domain": "materials"})
    return run_id, exp_id


def test_dry_run_job_synthesizes_metrics():
    run_id, exp_id = _make_experiment()
    backend = LocalBackend()
    spec = JobSpec(
        run_id=run_id, domain="materials", design={}, data_spec={}, experiment_id=exp_id, dry_run=True
    )
    job_id = backend.submit(spec)
    st = backend.status(job_id)
    assert st.status == "done"
    assert set(st.metrics) == {"mae", "r2", "rmse"}
    # persisted to the ledger
    names = {m["name"] for m in list_metrics(exp_id)}
    assert {"mae", "r2", "rmse"} <= names


def test_subprocess_job_trains_offline(tmp_path):
    run_id, exp_id = _make_experiment()
    csv = tmp_path / "comps.csv"
    pd.DataFrame(_COMPS, columns=["composition", "band_gap"]).to_csv(csv, index=False)

    backend = LocalBackend()
    spec = JobSpec(
        run_id=run_id,
        domain="materials",
        design={
            "model": "random_forest",
            "model_params": {"n_estimators": 30},
            "composition_column": "composition",
            "target_column": "band_gap",
            "test_size": 0.25,
            "random_state": 0,
        },
        data_spec={
            "source": "upload",
            "uri": str(csv),
            "ref": str(csv),
            "target_column": "band_gap",
            "feature_kind": "composition",
        },
        experiment_id=exp_id,
    )
    job_id = backend.submit(spec)

    deadline = time.time() + 120
    st = backend.status(job_id)
    while not st.finished and time.time() < deadline:
        time.sleep(1.0)
        st = backend.status(job_id)

    assert st.status == "done", f"job failed: {st.error}\nlogs:\n{backend.logs(job_id)}"
    assert "mae" in st.metrics and "r2" in st.metrics
    # metrics + a model artifact landed in the ledger
    assert {"mae", "r2", "rmse"} <= {m["name"] for m in list_metrics(exp_id)}
    assert any(a["kind"] == "model" for a in list_artifacts(exp_id))
