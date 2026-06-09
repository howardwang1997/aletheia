"""K2-S1: the belief primitive (calibrated credence math).

Pure, dependency-free unit tests — no DB, no network, no LLM. They pin down the four guarantees the
campaign learning loop leans on: priors are always weak, the credence moves ONLY on a harness-
verified confirm-split verdict, expected information gain matches hand-computed entropy reductions,
and the weak-prior flag clears only after enough real evidence accrues.
"""

from __future__ import annotations

import pytest

from aletheia.memory import belief
from aletheia.memory.belief import Credence


# --- priors are always weak, whatever the scorecard says -------------------------------------

@pytest.mark.parametrize("scores", [
    None, {}, {"novelty": 0.0}, {"novelty": 0.5}, {"novelty": 1.0},
    {"novelty": "nonsense"}, {"novelty": 2.0}, {"novelty": -1.0},
])
def test_prior_is_always_weak(scores):
    c = belief.prior_from_scorecard(scores)
    assert belief.is_weak_prior(c)
    # total mass is held at exactly 2 so the prior can never masquerade as evidence
    assert c.alpha + c.beta == pytest.approx(2.0)
    assert c.n_updates == 0
    # novelty only tilts the mean within [0.375, 0.625]; it never asserts belief
    assert 0.375 <= belief.mean(c) <= 0.625


def test_prior_novelty_tilts_mean_monotonically():
    low = belief.mean(belief.prior_from_scorecard({"novelty": 0.0}))
    mid = belief.mean(belief.prior_from_scorecard({"novelty": 0.5}))
    high = belief.mean(belief.prior_from_scorecard({"novelty": 1.0}))
    assert low < mid < high
    assert mid == pytest.approx(0.5)


# --- the credence moves ONLY on a harness-verified confirm-split verdict ----------------------

def test_update_noops_unless_confirm_split_verdict():
    c = Credence(1.0, 1.0)
    # not_evaluated -> no move
    assert belief.update(c, holds=None, confirm_split=True) == c
    # held but only on the exploration partition (no confirm split) -> no move
    assert belief.update(c, holds=True, confirm_split=False) == c
    # held on confirm but the independent audit could not run -> no move (missing verification)
    assert belief.update(c, holds=True, confirm_split=True, audit_error=True) == c


def test_update_folds_in_a_confirmed_hold_and_refute():
    c = Credence(1.0, 1.0)
    held = belief.update(c, holds=True, confirm_split=True)
    assert (held.alpha, held.beta, held.n_updates) == (2.0, 1.0, 1)
    refuted = belief.update(c, holds=False, confirm_split=True)
    assert (refuted.alpha, refuted.beta, refuted.n_updates) == (1.0, 2.0, 1)
    # a confirmed hold raises P(holds); a refute lowers it
    assert belief.mean(held) > belief.mean(c) > belief.mean(refuted)


def test_update_is_immutable():
    c = Credence(1.0, 1.0)
    _ = belief.update(c, holds=True, confirm_split=True)
    assert (c.alpha, c.beta, c.n_updates) == (1.0, 1.0, 0)  # original untouched


# --- entropy + expected information gain match hand computation -------------------------------

def test_binary_entropy_endpoints_and_peak():
    assert belief.binary_entropy(0.0) == 0.0
    assert belief.binary_entropy(1.0) == 0.0
    assert belief.binary_entropy(0.5) == pytest.approx(1.0)


def test_expected_entropy_reduction_uniform_prior():
    # Beta(1,1), predicted P(holds)=0.5: reduction = 1 - [0.5*H(2/3) + 0.5*H(1/3)] = 0.0817042
    c = Credence(1.0, 1.0)
    eig = belief.expected_entropy_reduction(c, p_holds=0.5)
    h = belief.binary_entropy(2.0 / 3.0)  # == H(1/3) by symmetry
    assert eig == pytest.approx(1.0 - h, abs=1e-9)
    assert eig == pytest.approx(0.081704, abs=1e-5)


def test_eig_is_nonnegative_and_saturates_as_belief_concentrates():
    weak = Credence(1.0, 1.0)
    # a credence that has seen many confirming rounds is nearly certain -> little left to learn
    concentrated = Credence(40.0, 1.0)
    eig_weak = belief.expected_entropy_reduction(weak, p_holds=belief.mean(weak))
    eig_conc = belief.expected_entropy_reduction(concentrated, p_holds=belief.mean(concentrated))
    assert eig_weak >= 0.0 and eig_conc >= 0.0
    assert eig_conc < eig_weak  # convergence: gain shrinks as the belief saturates
    assert eig_conc < 0.05


# --- measured EIG normalizes to [0,1] and falls closed on weak priors -------------------------

def test_normalized_eig_is_near_one_for_fresh_priors():
    # any scorecard-seeded prior is weak -> a fresh candidate has ~all the information to gain,
    # so it normalizes to ~1.0 and never CAPS the LLM's self-reported number (fail-closed).
    for nov in (0.0, 0.5, 1.0):
        c = belief.prior_from_scorecard({"novelty": nov})
        g = belief.normalized_information_gain(c, p_holds=belief.mean(c))
        assert 0.95 <= g <= 1.0


def test_normalized_eig_collapses_as_belief_concentrates():
    # a credence with many confirming rounds has little left to learn -> measured EIG -> 0, so it
    # caps an inflated LLM EIG below the campaign floor (this is what makes the loop converge).
    concentrated = Credence(20.0, 1.0)
    g = belief.normalized_information_gain(concentrated, p_holds=belief.mean(concentrated))
    assert g < 0.3  # below the default campaign_min_eig floor
    # the harness number wins over an inflated LLM claim
    assert min(0.99, g) == g


# --- the weak-prior flag clears only after enough harness-verified mass -----------------------

def test_weak_prior_needs_two_confirm_split_updates_to_clear():
    c = belief.prior_from_scorecard({"novelty": 0.5})  # mass 2.0
    assert belief.is_weak_prior(c)
    c1 = belief.update(c, holds=True, confirm_split=True)  # mass 3.0
    assert belief.is_weak_prior(c1)
    c2 = belief.update(c1, holds=True, confirm_split=True)  # mass 4.0
    assert not belief.is_weak_prior(c2)  # crossed WEAK_PRIOR_MAX_MASS
    assert c2.n_updates == 2
