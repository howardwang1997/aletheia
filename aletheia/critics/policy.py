"""Peer-review policy: how distinct models are assigned stances, how disagreement
is measured, and when another rebuttal round runs.

Kept separate from the gateway so the (deterministic, network-free) decision logic
is unit-testable on its own.
"""

from __future__ import annotations

from typing import Any

from aletheia.critics.schemas import Critique

_STANCES = ("supportive", "adversarial")


def assign_stances(providers: list[Any]) -> list[tuple[Any, str]]:
    """Assign each *distinct-model* provider one stance for round 1.

    With ≥2 models we alternate so opposing positions are held by DIFFERENT models
    (no same-model self-play), guaranteeing ≥1 adversarial and ≥1 supportive. With a
    single vendor we degrade to that one model running both stances.
    """
    if not providers:
        return []
    if len(providers) == 1:
        return [(providers[0], "adversarial"), (providers[0], "supportive")]
    return [(p, "adversarial" if i % 2 == 0 else "supportive") for i, p in enumerate(providers)]


def disagreement(critiques: list[Critique]) -> float:
    """Gate-relevant disagreement among reviewers, in [0, 1].

    Measured on the pass/fail axis (``approve``/``approve_with_changes`` = pass,
    ``reject`` = fail), since that is what the gate turns on — two reviewers split only
    between approve and approve_with_changes are not in real conflict. A *contested*
    blocker (a blocker/reject exists but not everyone rejects) is strong disagreement
    and floors the score at 0.5.
    """
    if not critiques:
        return 0.0
    passes = sum(1 for c in critiques if c.verdict != "reject")
    fails = len(critiques) - passes
    dissent = min(passes, fails) / len(critiques)
    has_block = any(
        c.verdict == "reject" or any(f.severity == "blocker" for f in c.findings)
        for c in critiques
    )
    all_reject = all(c.verdict == "reject" for c in critiques)
    if has_block and not all_reject:
        dissent = max(dissent, 0.5)
    return dissent


def should_continue(
    round_idx: int, max_rounds: int, critiques: list[Critique], *, dynamic: bool, threshold: float
) -> bool:
    """Run another round? Only while below the cap AND (if dynamic) reviewers still
    disagree beyond ``threshold``. Static mode caps at ``max_rounds`` with no early stop."""
    if round_idx >= max_rounds:
        return False
    if not dynamic:
        return True
    return disagreement(critiques) >= threshold
