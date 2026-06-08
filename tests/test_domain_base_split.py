"""K1 S1: the harness-owned explore/confirm partitioner — the anti-p-hacking seal.

The split must be a pure, deterministic function of ``(groups, seed)`` (the AI never chooses it),
group-disjoint (no group spans both sides, else a leaked group defeats the seal), and FAIL CLOSED
when the data is too small for an honest 4-way split (explore/confirm x test/control)."""

from __future__ import annotations

import numpy as np

from aletheia.domains.base import DomainPlugin

_split = DomainPlugin._split_explore_confirm  # static; no instance needed


def test_deterministic_same_inputs_same_split():
    groups = np.array([f"g{i // 5}" for i in range(300)], dtype=object)
    a = _split(groups, 300, seed=42)
    b = _split(groups, 300, seed=42)
    assert a is not None and b is not None
    assert a["meta"]["index_hash"] == b["meta"]["index_hash"]
    assert np.array_equal(a["explore_idx"], b["explore_idx"])
    assert np.array_equal(a["confirm_idx"], b["confirm_idx"])


def test_explore_and_confirm_are_disjoint_and_cover_all_rows():
    groups = np.array([f"g{i // 5}" for i in range(300)], dtype=object)
    r = _split(groups, 300, seed=7)
    assert r is not None
    ex, cf = set(r["explore_idx"].tolist()), set(r["confirm_idx"].tolist())
    assert ex.isdisjoint(cf)
    assert ex | cf == set(range(300))
    assert r["meta"]["n_explore"] == len(ex) and r["meta"]["n_confirm"] == len(cf)


def test_group_disjoint_no_group_spans_both_sides():
    groups = np.array([f"g{i // 5}" for i in range(300)], dtype=object)
    r = _split(groups, 300, seed=11)
    assert r is not None and r["meta"]["group_disjoint"] is True
    ex = set(r["explore_idx"].tolist())
    for gid in {str(x) for x in groups.tolist()}:
        members = {i for i, x in enumerate(groups) if str(x) == gid}
        # every member of a group is on the SAME side
        assert members <= ex or members.isdisjoint(ex)


def test_seed_changes_the_partition():
    groups = np.array([f"g{i // 5}" for i in range(300)], dtype=object)
    h1 = _split(groups, 300, seed=1)["meta"]["index_hash"]
    h2 = _split(groups, 300, seed=2)["meta"]["index_hash"]
    assert h1 != h2


def test_row_split_fallback_when_groups_is_none():
    r = _split(None, 300, seed=3)
    assert r is not None
    assert r["meta"]["group_disjoint"] is False
    ex, cf = set(r["explore_idx"].tolist()), set(r["confirm_idx"].tolist())
    assert ex.isdisjoint(cf) and ex | cf == set(range(300))


def test_fail_closed_when_too_small_for_4way_split():
    # default demonstration_min_samples is 20 -> confirm needs >= 40, so n=30 can never qualify
    assert _split(None, 30, seed=1) is None
    assert _split(np.array([f"g{i}" for i in range(30)], dtype=object), 30, seed=1) is None
    assert _split(None, 0, seed=1) is None


def test_meta_shape_and_index_hash_stable():
    groups = np.array([f"g{i // 4}" for i in range(240)], dtype=object)
    r = _split(groups, 240, seed=5)
    assert r is not None
    m = r["meta"]
    assert set(m) == {
        "seed", "explore_frac", "n_explore", "n_confirm",
        "split_algo_version", "group_disjoint", "index_hash",
    }
    assert m["seed"] == 5 and m["split_algo_version"] == DomainPlugin.SPLIT_ALGO_VERSION
    assert isinstance(m["index_hash"], str) and len(m["index_hash"]) == 16
