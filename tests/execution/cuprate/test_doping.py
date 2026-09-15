"""Regressions for the frozen-valence hole-doping estimator."""

import pytest
from pymatgen.core import Composition

from aletheia.execution.cuprate.doping import (
    FORMAL_VALENCES,
    HALOGEN_ANIONS,
    stoichiometric_holes_per_copper,
)


def test_frozen_valence_table_spot_values():
    assert FORMAL_VALENCES["Y"] == 3
    assert FORMAL_VALENCES["Ba"] == 2
    assert FORMAL_VALENCES["S"] == 6
    assert FORMAL_VALENCES["N"] == 5
    assert FORMAL_VALENCES["Pb"] == 4
    assert "O" not in FORMAL_VALENCES
    assert "Cu" not in FORMAL_VALENCES
    assert HALOGEN_ANIONS == frozenset({"F", "Cl", "Br", "I"})


def test_ybco_hand_check_is_overdoped_positive():
    # (14 - 7 - 6) / 3 = 1/3 holes per Cu, the design's pinned hand check.
    assert stoichiometric_holes_per_copper(Composition("YBa2Cu3O7")) == pytest.approx(1 / 3)


def test_known_neutral_compositions_balance_to_zero():
    # BaCuO2: (4 - 2 - 2) / 1 = 0; La2SrCu2O6: (12 - 8 - 4) / 2 = 0.
    assert stoichiometric_holes_per_copper(Composition("BaCuO2")) == pytest.approx(0.0)
    assert stoichiometric_holes_per_copper(Composition("La2SrCu2O6")) == pytest.approx(0.0)


def test_oxygen_content_moves_the_estimate_monotonically():
    p6 = stoichiometric_holes_per_copper(Composition("YBa2Cu3O6"))
    p65 = stoichiometric_holes_per_copper(Composition("YBa2Cu3O6.5"))
    p7 = stoichiometric_holes_per_copper(Composition("YBa2Cu3O7"))
    assert p6 < p65 < p7


def test_untabled_family_element_fails_closed():
    with pytest.raises(ValueError, match="Xe"):
        stoichiometric_holes_per_copper(Composition("YBa2Cu3O7Xe0.1"))


def test_missing_copper_fails_closed():
    with pytest.raises(ValueError, match="positive Cu amount"):
        stoichiometric_holes_per_copper(Composition("BaY2O4"))


def test_halogens_count_as_single_charged_anions():
    # Sr2CuO2Cl2: anion 2*2 + 2*1 = 6, cation 4, baseline 2 -> (6 - 4 - 2) / 1 = 0.
    assert stoichiometric_holes_per_copper(Composition("Sr2CuO2Cl2")) == pytest.approx(0.0)
