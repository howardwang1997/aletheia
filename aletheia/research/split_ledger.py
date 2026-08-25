"""Campaign-level, immutable data-role allocation (Epistemic Seal v2).

One hypothesis-level explore/confirm split is not enough for an adaptive campaign:
later hypotheses are informed by earlier confirmation outcomes.  This module seals
the full campaign before ideation into a reusable exploration pool, mutually
exclusive per-round confirmation batches, and a one-time final holdout.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SPLIT_LEDGER_VERSION = 2


def _update_blob(h: Any, value: bytes) -> None:
    h.update(len(value).to_bytes(8, "little"))
    h.update(value)


def staged_data_identity(
    X: Any,
    y: Any,
    groups: Any,
    feature_names: list[str] | None = None,
) -> dict[str, Any]:
    """Return a stable identity for the exact rows the harness will split."""
    import numpy as np

    xa = np.ascontiguousarray(np.asarray(X, dtype=np.float64))
    ya = np.ascontiguousarray(np.asarray(y, dtype=np.float64).reshape(-1))
    if xa.shape[0] != ya.shape[0]:
        raise ValueError("X/y row mismatch while fingerprinting staged data")
    ga = (
        [str(v) for v in np.asarray(groups, dtype=object).reshape(-1).tolist()]
        if groups is not None
        else [f"row:{i}" for i in range(len(ya))]
    )
    if len(ga) != len(ya):
        raise ValueError("groups/y row mismatch while fingerprinting staged data")

    dataset = hashlib.sha256()
    _update_blob(dataset, json.dumps(list(xa.shape)).encode())
    _update_blob(dataset, xa.tobytes())
    _update_blob(dataset, ya.tobytes())
    _update_blob(dataset, json.dumps(ga, separators=(",", ":")).encode())
    _update_blob(dataset, json.dumps(feature_names or [], separators=(",", ":")).encode())
    fingerprint = dataset.hexdigest()

    rows = hashlib.sha256()
    for i, group in enumerate(ga):
        row = hashlib.sha256()
        _update_blob(row, fingerprint.encode())
        _update_blob(row, int(i).to_bytes(8, "little"))
        _update_blob(row, xa[i].tobytes())
        _update_blob(row, ya[i].tobytes())
        _update_blob(row, group.encode())
        rows.update(row.digest())
    return {
        "dataset_fingerprint": fingerprint,
        "row_identity_hash": rows.hexdigest(),
        "n_rows": int(len(ya)),
        "n_features": int(xa.shape[1]) if xa.ndim > 1 else 1,
        "n_groups": len(set(ga)),
    }


def _index_hash(role: str, indices: list[int]) -> str:
    import numpy as np

    h = hashlib.sha256(role.encode() + b"|")
    h.update(np.ascontiguousarray(indices, dtype=np.int64).tobytes())
    return h.hexdigest()


def allocate_campaign_splits(
    groups: Any,
    n: int,
    *,
    seed: int,
    confirmation_batches: int,
    explore_fraction: float = 0.5,
    final_holdout_fraction: float = 0.2,
    min_confirmation_n: int = 40,
    family_alpha: float = 0.05,
    final_alpha: float = 0.01,
) -> dict[str, Any]:
    """Allocate every row/group to exactly one immutable campaign role.

    Assignment is deterministic and group-disjoint.  Groups are ordered by a
    salted hash and greedily balanced against role row targets, which avoids the
    severe starvation produced by assigning many uneven groups independently.
    """
    import numpy as np

    n = int(n)
    batches = int(confirmation_batches)
    if n <= 0 or batches <= 0:
        raise ValueError("campaign split requires rows and at least one confirmation batch")
    explore_fraction = float(explore_fraction)
    final_holdout_fraction = float(final_holdout_fraction)
    if not (0 < explore_fraction < 1) or not (0 < final_holdout_fraction < 1):
        raise ValueError("explore/final fractions must be inside (0,1)")
    confirm_total = 1.0 - explore_fraction - final_holdout_fraction
    if confirm_total <= 0:
        raise ValueError("explore + final fractions leave no confirmation data")
    if not (0 < final_alpha < family_alpha < 1):
        raise ValueError("alpha plan must reserve 0 < final_alpha < family_alpha < 1")

    ga = (
        np.asarray(groups, dtype=object).reshape(-1)
        if groups is not None
        else np.asarray([f"row:{i}" for i in range(n)], dtype=object)
    )
    if len(ga) != n:
        raise ValueError("groups length does not match staged rows")
    grouped: dict[str, list[int]] = {}
    for idx, raw in enumerate(ga.tolist()):
        grouped.setdefault(str(raw), []).append(idx)

    roles = ["explore", *[f"confirm:{i + 1}" for i in range(batches)], "final"]
    fractions = {
        "explore": explore_fraction,
        "final": final_holdout_fraction,
        **{f"confirm:{i + 1}": confirm_total / batches for i in range(batches)},
    }
    targets = {role: max(1.0, n * fractions[role]) for role in roles}
    assigned: dict[str, list[int]] = {role: [] for role in roles}

    def _salted(group: str) -> str:
        return hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()

    # Largest groups first, with a salted deterministic tie-break; assign to the role with the
    # greatest normalized deficit.  A second salted tie-break avoids a fixed role-order bias.
    ordered = sorted(grouped, key=lambda g: (-len(grouped[g]), _salted(g)))
    for group in ordered:
        deficits = {
            role: (targets[role] - len(assigned[role])) / targets[role] for role in roles
        }
        role = max(
            roles,
            key=lambda r: (
                deficits[r],
                hashlib.sha256(f"{seed}:{group}:{r}".encode()).hexdigest(),
            ),
        )
        assigned[role].extend(grouped[group])

    for role in roles:
        assigned[role].sort()
    if len(assigned["explore"]) < min_confirmation_n:
        raise ValueError("exploration pool is sample-starved")
    if len(assigned["final"]) < min_confirmation_n:
        raise ValueError("final holdout is sample-starved")
    for i in range(batches):
        if len(assigned[f"confirm:{i + 1}"]) < min_confirmation_n:
            raise ValueError(f"confirmation batch {i + 1} is sample-starved")

    flattened = [idx for role in roles for idx in assigned[role]]
    if len(flattened) != n or len(set(flattened)) != n or set(flattened) != set(range(n)):
        raise AssertionError("campaign split roles are not a disjoint cover")

    confirmation_alpha = (float(family_alpha) - float(final_alpha)) / batches
    confirmations = [
        {
            "batch": i + 1,
            "indices": assigned[f"confirm:{i + 1}"],
            "n": len(assigned[f"confirm:{i + 1}"]),
            "index_hash": _index_hash(f"confirm:{i + 1}", assigned[f"confirm:{i + 1}"]),
            "alpha": confirmation_alpha,
        }
        for i in range(batches)
    ]
    membership = hashlib.sha256(
        "|".join(
            f"{role}:{_index_hash(role, assigned[role])}" for role in roles
        ).encode()
    ).hexdigest()
    return {
        "split_algo_version": SPLIT_LEDGER_VERSION,
        "algorithm": "group_balanced_hash_v2",
        "seed": int(seed),
        "n_rows": n,
        "group_disjoint": groups is not None,
        "membership_hash": membership,
        "alpha_plan": {
            "method": "bonferroni_fixed_sequence",
            "family_alpha": float(family_alpha),
            "confirmation_alpha_each": confirmation_alpha,
            "final_alpha": float(final_alpha),
            "attempts_disclosed": True,
        },
        "explore": {
            "indices": assigned["explore"],
            "n": len(assigned["explore"]),
            "index_hash": _index_hash("explore", assigned["explore"]),
        },
        "confirmations": confirmations,
        "final_holdout": {
            "indices": assigned["final"],
            "n": len(assigned["final"]),
            "index_hash": _index_hash("final", assigned["final"]),
            "alpha": float(final_alpha),
        },
    }


def public_split_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Remove raw row membership while retaining everything needed for audit/status events."""
    return {
        "split_algo_version": plan.get("split_algo_version"),
        "algorithm": plan.get("algorithm"),
        "seed": plan.get("seed"),
        "n_rows": plan.get("n_rows"),
        "group_disjoint": plan.get("group_disjoint"),
        "membership_hash": plan.get("membership_hash"),
        "alpha_plan": plan.get("alpha_plan"),
        "explore": {k: v for k, v in (plan.get("explore") or {}).items() if k != "indices"},
        "confirmations": [
            {k: v for k, v in row.items() if k != "indices"}
            for row in (plan.get("confirmations") or [])
        ],
        "final_holdout": {
            k: v for k, v in (plan.get("final_holdout") or {}).items() if k != "indices"
        },
    }
