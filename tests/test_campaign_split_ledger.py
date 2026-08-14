"""Epistemic Seal v2: immutable, cross-round-disjoint campaign data roles."""

from __future__ import annotations

import numpy as np
import pytest

from aletheia.data.registry import register_dataset
from aletheia.db import create_all
from aletheia.memory.service import (
    claim_external_validation,
    claim_final_holdout,
    create_run,
    finalize_plan,
    finish_hypothesis_attempt,
    get_campaign_split_ledger,
    get_external_validation_ledger,
    list_hypothesis_attempts,
    record_final_holdout_result,
    record_external_validation_result,
    register_hypothesis_attempt,
    seal_campaign_splits,
    seal_external_validation,
)
from aletheia.research.split_ledger import allocate_campaign_splits, staged_data_identity
from aletheia.scheduler.driver import ExperimentDriver


def _data(n: int = 600):
    rng = np.random.default_rng(8)
    X = rng.normal(size=(n, 6))
    y = X[:, 0] + rng.normal(size=n)
    groups = np.asarray([f"g{i % 60}" for i in range(n)], dtype=object)
    return X, y, groups


def _plan(groups, n, seed=19):
    return allocate_campaign_splits(
        groups,
        n,
        seed=seed,
        confirmation_batches=3,
        explore_fraction=0.5,
        final_holdout_fraction=0.2,
        min_confirmation_n=40,
        family_alpha=0.05,
        final_alpha=0.01,
    )


def test_campaign_roles_are_deterministic_disjoint_cover_and_group_disjoint():
    X, y, groups = _data()
    a = _plan(groups, len(y))
    b = _plan(groups, len(y))
    assert a == b
    role_sets = [set(a["explore"]["indices"])]
    role_sets += [set(row["indices"]) for row in a["confirmations"]]
    role_sets += [set(a["final_holdout"]["indices"])]
    assert set.union(*role_sets) == set(range(len(y)))
    for i, left in enumerate(role_sets):
        for right in role_sets[i + 1 :]:
            assert left.isdisjoint(right)
    group_roles: dict[str, int] = {}
    for role, indices in enumerate(role_sets):
        for idx in indices:
            gid = str(groups[idx])
            assert gid not in group_roles or group_roles[gid] == role
            group_roles[gid] = role
    assert sum(row["alpha"] for row in a["confirmations"]) + a["final_holdout"]["alpha"] == pytest.approx(0.05)


def test_seed_changes_membership_but_not_role_sizes_or_coverage():
    _, y, groups = _data()
    a = _plan(groups, len(y), seed=1)
    b = _plan(groups, len(y), seed=2)
    assert a["membership_hash"] != b["membership_hash"]
    assert sorted(row["n"] for row in a["confirmations"]) == sorted(
        row["n"] for row in b["confirmations"]
    )


def test_staged_identity_changes_on_any_row_change():
    X, y, groups = _data(100)
    a = staged_data_identity(X, y, groups, [f"f{i}" for i in range(6)])
    y2 = y.copy()
    y2[17] += 0.001
    b = staged_data_identity(X, y2, groups, [f"f{i}" for i in range(6)])
    assert a["dataset_fingerprint"] != b["dataset_fingerprint"]
    assert a["row_identity_hash"] != b["row_identity_hash"]


def test_starved_confirmation_fails_instead_of_reshuffling():
    groups = np.asarray(["one-group"] * 100, dtype=object)
    with pytest.raises(ValueError, match="sample-starved"):
        _plan(groups, len(groups))


def test_persisted_split_is_immutable_and_final_holdout_is_one_time():
    create_all()
    X, y, groups = _data()
    identity = staged_data_identity(X, y, groups, [f"f{i}" for i in range(6)])
    plan = _plan(groups, len(y))
    run_id = create_run("split ledger test", domain="materials")
    first = seal_campaign_splits(
        run_id,
        dataset_fingerprint=identity["dataset_fingerprint"],
        row_identity_hash=identity["row_identity_hash"],
        plan=plan,
    )
    second = seal_campaign_splits(
        run_id,
        dataset_fingerprint=identity["dataset_fingerprint"],
        row_identity_hash=identity["row_identity_hash"],
        plan=plan,
    )
    assert first["reused"] is False and second["reused"] is True
    with pytest.raises(RuntimeError, match="dataset changed"):
        seal_campaign_splits(
            run_id,
            dataset_fingerprint="f" * 64,
            row_identity_hash=identity["row_identity_hash"],
            plan=plan,
        )
    assert claim_final_holdout(run_id)["claimed"] is True
    assert claim_final_holdout(run_id) == {
        "claimed": False,
        "state": "final_opened",
        "result": None,
    }
    result = {"evaluated": True, "holds": True, "split_hash": plan["final_holdout"]["index_hash"]}
    record_final_holdout_result(run_id, result)
    claimed = claim_final_holdout(run_id)
    assert claimed["claimed"] is False and claimed["result"] == result
    assert get_campaign_split_ledger(run_id)["state"] == "final_completed"


def test_every_family_attempt_is_disclosed_with_split_and_alpha():
    create_all()
    run_id = create_run("family attempts", domain="materials")
    first = register_hypothesis_attempt(
        run_id,
        experiment_id=None,
        family_key="family",
        hypothesis_text="hypothesis one",
        round_index=1,
        phase="confirmation",
        confirmation_batch=1,
        split_hash="a" * 64,
        alpha_allocated=0.01,
    )
    finish_hypothesis_attempt(first, status="evaluated", outcome={"holds": False})
    rows = list_hypothesis_attempts(run_id)
    assert len(rows) == 1
    assert rows[0]["family_key"] == "family"
    assert rows[0]["split_hash"] == "a" * 64
    assert rows[0]["alpha_allocated"] == pytest.approx(0.01)
    assert rows[0]["outcome"] == {"holds": False}


def test_driver_uses_a_different_sealed_confirmation_batch_each_round():
    X, y, groups = _data()

    class _Plugin:
        def load_data(self, data_spec):
            return object()

        def featurize(self, df, design):
            return X, y, [f"f{i}" for i in range(6)], groups

    driver = ExperimentDriver("split-driver", dry_run=True)
    driver._campaign_dataset_identity = staged_data_identity(
        X, y, groups, [f"f{i}" for i in range(6)]
    )
    driver._campaign_split_plan = _plan(groups, len(y))
    driver._current_round_idx = 1
    driver._current_attempt_sequence = 1
    one = driver._stage_explore_arrays(_Plugin(), {}, {}, 42)
    # A pivot can remain scientific round 1, but it is a new hypothesis attempt and must burn a
    # fresh batch.  Batch identity therefore follows attempt_sequence, never display round.
    driver._current_attempt_sequence = 2
    two = driver._stage_explore_arrays(_Plugin(), {}, {}, 42)
    assert one is not None and two is not None
    assert one[4]["confirmation_batch"] == 1
    assert two[4]["confirmation_batch"] == 2
    assert set(one[3]).isdisjoint(two[3])
    assert set(one[3]).isdisjoint(driver._campaign_split_plan["final_holdout"]["indices"])
    with pytest.raises(RuntimeError, match="exhausted"):
        driver._confirmation_for_attempt(4)


@pytest.mark.asyncio
async def test_driver_opens_final_holdout_once_and_reuses_recorded_result():
    create_all()
    X, y, groups = _data()
    identity = staged_data_identity(X, y, groups, [f"f{i}" for i in range(6)])
    plan = _plan(groups, len(y))
    run_id = create_run("final driver test", domain="materials")
    exp_id = finalize_plan(run_id, {"objective": "test", "domain": "materials"})
    seal_campaign_splits(
        run_id,
        dataset_fingerprint=identity["dataset_fingerprint"],
        row_identity_hash=identity["row_identity_hash"],
        plan=plan,
    )

    class _Plugin:
        AI_AUTHORED_CAPABILITY_ID = "ai_authored_demonstration"
        calls = 0

        def run_demonstration(self, spec, data_spec, workdir):
            self.calls += 1
            assert spec["confirm_index"] == plan["final_holdout"]["indices"]
            assert spec["split_meta"]["role"] == "final_holdout"
            return {
                "holds": True,
                "test_statistic": 2.0,
                "control_statistic": 0.0,
                "detail": "external-looking sealed validation",
            }

    plugin = _Plugin()
    driver = ExperimentDriver(run_id, dry_run=True)
    driver._campaign_split_plan = plan
    driver._campaign_dataset_identity = identity
    driver._family_key = "family"
    outcomes = [{
        "round": 1,
        "exp_id": exp_id,
        "headline": 0.3,
        "hypothesis": "h",
        "_final_candidate": {
            "code": "def compute_demonstration(*args): pass",
            "preregistration": {"supported_if": {"op": ">", "threshold": 1}},
            "demonstration": {},
            "random_state": 42,
        },
    }]
    first = await driver._validate_final_holdout(outcomes, {}, plugin)
    second = await driver._validate_final_holdout(outcomes, {}, plugin)
    assert first["holds"] is True and second == first
    assert plugin.calls == 1


def test_external_validation_is_immutable_and_one_time():
    create_all()
    run_id = create_run("external validation ledger", domain="materials")
    asset_id = register_dataset(
        run_id,
        "upload",
        role="external_validation",
        ref="external.csv",
        uri="external.csv",
        status="ready",
        content_sha256="c" * 64,
    )
    args = {
        "data_asset_id": asset_id,
        "dataset_fingerprint": "d" * 64,
        "row_identity_hash": "r" * 64,
        "provenance": {"source": "independent extraction", "sha256": "c" * 64},
    }
    assert seal_external_validation(run_id, **args)["reused"] is False
    assert seal_external_validation(run_id, **args)["reused"] is True
    with pytest.raises(RuntimeError, match="changed after sealing"):
        seal_external_validation(run_id, **{**args, "dataset_fingerprint": "x" * 64})
    assert claim_external_validation(run_id)["claimed"] is True
    assert claim_external_validation(run_id) == {
        "claimed": False,
        "state": "opened",
        "result": None,
    }
    result = {"evaluated": True, "holds": False, "detail": "negative replication preserved"}
    record_external_validation_result(run_id, result)
    assert claim_external_validation(run_id)["result"] == result
    assert get_external_validation_ledger(run_id)["state"] == "completed"


@pytest.mark.asyncio
async def test_driver_runs_locked_external_replication_only_once(monkeypatch):
    create_all()
    run_id = create_run("external driver test", domain="materials")
    exp_id = finalize_plan(run_id, {"objective": "test", "domain": "materials"})
    asset_id = register_dataset(
        run_id,
        "upload",
        role="external_validation",
        ref="external.csv",
        uri="external.csv",
        status="ready",
        content_sha256="c" * 64,
    )
    identity = {"dataset_fingerprint": "d" * 64, "row_identity_hash": "r" * 64, "n_rows": 55}
    seal_external_validation(
        run_id,
        data_asset_id=asset_id,
        dataset_fingerprint=identity["dataset_fingerprint"],
        row_identity_hash=identity["row_identity_hash"],
        provenance={"asset_id": asset_id},
    )

    class _Plugin:
        AI_AUTHORED_CAPABILITY_ID = "ai_authored_demonstration"
        calls = 0

        def run_demonstration(self, spec, data_spec, workdir):
            self.calls += 1
            assert spec["confirm_index"] == list(range(55))
            assert spec["split_meta"]["role"] == "external_replication"
            assert spec["demonstration_code"] == "locked-code"
            return {
                "holds": False,
                "test_statistic": 0.2,
                "control_statistic": 0.0,
                "detail": "honest external refutation",
            }

    plugin = _Plugin()
    driver = ExperimentDriver(run_id, dry_run=True)
    driver._family_key = "family"
    driver._external_validation_spec = {
        "asset_id": asset_id,
        "content_sha256": "c" * 64,
        "profile": {"external_provenance": {"independence_class": "independent"}},
    }
    driver._external_dataset_identity = identity
    monkeypatch.setattr(driver, "_build_external_identity", lambda *_args: identity)
    outcomes = [{
        "round": 1,
        "exp_id": exp_id,
        "headline": 0.3,
        "hypothesis": "h",
        "_final_candidate": {
            "code": "locked-code",
            "preregistration": {
                "supported_if": {"op": ">", "threshold": 1},
                "control_silent_if": {"op": "<=", "threshold": 1},
            },
            "demonstration": {},
            "random_state": 42,
        },
    }]
    first = await driver._validate_external_replication(outcomes, plugin)
    second = await driver._validate_external_replication(outcomes, plugin)
    assert first is not None and first["holds"] is False and second == first
    assert plugin.calls == 1
