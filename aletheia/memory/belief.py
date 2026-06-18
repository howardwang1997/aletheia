"""K2 (campaign learning loop) — the belief primitive (the epistemic world model's state).

A campaign carries, per *open-question lineage*, a calibrated credence ``Beta(alpha, beta)`` for
"will this line of inquiry hold on held-out data?". The literature/scorecard sets a deliberately
*weak* prior; the **only** thing that moves the credence is a harness-verified confirm-split verdict
(+1 to ``alpha`` on a confirmed hold, +1 to ``beta`` on an evaluated refute). A ``not_evaluated`` /
degraded / non-confirm-split round NEVER moves it.

This module is the math half of K2-S1: a PURE, dependency-free, unit-testable primitive (mirrors
``outcome.py``'s purity). It has NO I/O, NO LLM, NO DB. It NEVER decides ``holds`` / ``supported`` /
claim strength — those stay harness-owned. The belief state is a *planning aid* (it sizes
information gain and flags weak priors); the harness disposes.

The decision-relevant belief is ``p = mean(Beta) = alpha / (alpha + beta) ∈ (0, 1)`` — the credence
that the line holds. Its uncertainty is the **binary entropy** of ``p`` (in bits): dependency-free,
no digamma/scipy, and exactly the entropy of the ``P(holds)`` we plan against. Expected information
gain from running an experiment is the expected reduction of that entropy under one harness update.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

# A credence stops being a "weak prior" only once it has accumulated enough mass that it rests on
# real, replicated, harness-verified evidence rather than the scorecard's guess. With a prior mass
# of ~2 (see ``prior_from_scorecard``), this requires >=2 confirm-split updates — i.e. a single
# confirm-split hold is, honestly, still a weak prior.
WEAK_PRIOR_MAX_MASS = 4.0


@dataclass(frozen=True)
class Credence:
    """An immutable Beta credence over "this open question holds on held-out data".

    ``n_updates`` counts the harness-verified confirm-split verdicts folded in (NOT the prior),
    so callers can report how much real evidence the belief rests on.
    """

    alpha: float
    beta: float
    n_updates: int = 0


def prior_from_scorecard(scores: dict | None = None) -> Credence:
    """Seed a *weak* prior from the pre-execution hypothesis scorecard.

    Base is the uniform ``Beta(1, 1)``; the novelty score tilts the mean symmetrically about 0.5
    while keeping the total mass fixed at exactly 2, so the prior is always weak (``is_weak_prior``
    is True for ANY input). The literature/scorecard may *propose* a lean; it can never *assert*
    belief — only harness verdicts do that.
    """
    try:
        novelty = float((scores or {}).get("novelty", 0.5))
    except (TypeError, ValueError):
        novelty = 0.5
    novelty = min(1.0, max(0.0, novelty))
    # constant total mass = 2.0; tilt in [-0.25, 0.25] -> mean = (1 + tilt) / 2 in [0.375, 0.625]
    tilt = 0.5 * (novelty - 0.5)
    return Credence(alpha=1.0 + tilt, beta=1.0 - tilt, n_updates=0)


def update(
    c: Credence,
    *,
    holds: bool | None,
    confirm_split: bool,
    audit_error: bool = False,
) -> Credence:
    """Fold a harness verdict into the credence — the ONLY mutator.

    Returns ``c`` UNCHANGED unless the round produced a trustworthy, held-out verdict:
    ``holds`` is a real boolean (not ``None``/not_evaluated) AND it was decided on the held-out
    confirm split AND the independent audit did not error. Then ``+1`` to ``alpha`` (held) or
    ``beta`` (refuted) and ``n_updates += 1``. A dreamed rollout, an LLM say-so, an exploration-only
    result, or a degraded audit can never move the belief (fail-closed).
    """
    if holds is None or not confirm_split or audit_error:
        return c
    if holds:
        return replace(c, alpha=c.alpha + 1.0, n_updates=c.n_updates + 1)
    return replace(c, beta=c.beta + 1.0, n_updates=c.n_updates + 1)


def mean(c: Credence) -> float:
    """Posterior mean ``P(holds) = alpha / (alpha + beta)``."""
    total = c.alpha + c.beta
    return c.alpha / total if total > 0 else 0.5


def binary_entropy(p: float) -> float:
    """Binary entropy of a Bernoulli(``p``) in bits, with ``0·log0 := 0``."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -p * math.log2(p) - (1.0 - p) * math.log2(1.0 - p)


def entropy(c: Credence) -> float:
    """Uncertainty of the credence = binary entropy of its mean (bits)."""
    return binary_entropy(mean(c))


def expected_entropy_reduction(c: Credence, p_holds: float) -> float:
    """Expected reduction (bits) in the credence's entropy from ONE harness update, given a
    predicted outcome distribution ``{holds: p_holds, refuted: 1 - p_holds}``.

    This is the *measured* expected information gain the planner uses for selection — a checkable
    quantity, unlike the LLM's self-reported number. The posterior means after a hold / refute
    update are ``(alpha+1)/(M+1)`` and ``alpha/(M+1)`` for ``M = alpha + beta``. Clamped at 0.
    """
    p = min(1.0, max(0.0, float(p_holds)))
    m = c.alpha + c.beta
    if m <= 0:
        return 0.0
    mean_plus = (c.alpha + 1.0) / (m + 1.0)
    mean_minus = c.alpha / (m + 1.0)
    expected_post = p * binary_entropy(mean_plus) + (1.0 - p) * binary_entropy(mean_minus)
    return max(0.0, entropy(c) - expected_post)


def is_weak_prior(c: Credence) -> bool:
    """True while the credence rests on too little harness-verified mass to be trusted as strong —
    it must not yield a ``strong`` claim (mirrors the K1 ``exploration_missing`` cap). With a prior
    mass of ~2, this needs >=2 confirm-split updates to clear.
    """
    return (c.alpha + c.beta) < WEAK_PRIOR_MAX_MASS


# The most a SINGLE held-out observation can reduce the credence's entropy: the uniform Beta(1,1)
# at p=0.5 (the weakest prior we ever hold). Used to normalize measured EIG into [0, 1] so it is
# directly comparable to the LLM's self-reported 0..1 information-gain and the campaign EIG floor.
# A fresh/weak belief therefore normalizes to ~1.0 (no capping = today's behavior — fail-closed);
# only an ACCUMULATED belief, whose remaining information is small, normalizes down toward 0.
MAX_SINGLE_OBS_EIG_BITS = expected_entropy_reduction(Credence(1.0, 1.0), 0.5)


def normalized_information_gain(c: Credence, p_holds: float) -> float:
    """Measured expected information gain of one harness update, scaled to [0, 1] against the best
    a single observation can do. This is the *checkable* quantity the planner floors on — the LLM's
    self-reported number can only LOSE to it (``effective = min(llm, measured)``)."""
    if MAX_SINGLE_OBS_EIG_BITS <= 0:
        return 0.0
    return min(1.0, expected_entropy_reduction(c, p_holds) / MAX_SINGLE_OBS_EIG_BITS)
