"""Stoichiometric hole-doping estimate per Cu (D2's stratification variable).

The estimate is composition-only and deliberately coarse — it is the
stoichiometric charge-balance hole count over the Cu(II) baseline:

    p = (2 * n_O + 1 * n_halogens - sum_{i != Cu} z_i * n_i - 2 * n_Cu) / n_Cu

with formal oxide valences from a frozen table.  Mixed-valence cations
(Bi, Tl, Pb, chain Cu) carry single formal values; the coarseness this
introduces is recorded in the capability manifest's limitations and is part
of why the ~0.16 holes/Cu optimum is a cited constant pinned in the
protocol text (KEYSTONE_A section 3), not a derived quantity.

Anions: oxygen is -2, halogens -1; every other element is a tabled cation.
A family formula containing an element outside the table fails closed —
the estimator never silently buckets an unknown chemistry.

YBa2Cu3O7 hand check: (14 - 7 - 6) / 3 = 1/3 holes per Cu — overdoped,
the correct sign and scale for an O7 composition.
"""

from __future__ import annotations

from typing import Any

# Formal oxide valences for every element appearing in the multi-alkaline-earth
# cuprate stratum of the registered UCI card (enumerated 2026-09-16 over the
# 4,009 family rows), plus Cu handled by the baseline term and oxygen/halogens
# handled as anions.  S, Se, Te, N and P use the polyatomic-anion convention
# (sulfate S(+6) with its own oxygens balances; nitrate N(+5) likewise).
FORMAL_VALENCES: dict[str, int] = {
    **dict.fromkeys(("H", "Li", "Na", "K", "Cs", "Ag"), 1),
    **dict.fromkeys(("Be", "Mg", "Ca", "Sr", "Ba", "Ni", "Zn", "Cd", "Hg", "Co", "Mn"), 2),
    **dict.fromkeys(
        (
            "B",
            "Al",
            "Ga",
            "In",
            "Sc",
            "Y",
            "La",
            "Ce",
            "Pr",
            "Nd",
            "Sm",
            "Eu",
            "Gd",
            "Tb",
            "Dy",
            "Ho",
            "Er",
            "Tm",
            "Yb",
            "Lu",
            "Bi",
            "Tl",
            "Cr",
            "Fe",
            "Au",
        ),
        3,
    ),
    **dict.fromkeys(("C", "Si", "Ge", "Sn", "Pb", "Zr", "Hf", "Ti", "Ru", "Pt"), 4),
    **dict.fromkeys(("N", "P", "Sb", "V"), 5),
    **dict.fromkeys(("S", "Se", "Te", "Mo", "W", "Re"), 6),
}

HALOGEN_ANIONS = frozenset({"F", "Cl", "Br", "I"})
OXYGEN = "O"
COPPER = "Cu"
COPPER_BASELINE_VALENCE = 2


def stoichiometric_holes_per_copper(composition: Any) -> float:
    """Charge-balance hole count per Cu for one composition.

    Fails closed on any non-Cu, non-anion element missing from the frozen
    table, on missing Cu, and on non-positive Cu amount.
    """

    amounts = {element.symbol: amount for element, amount in composition.items()}
    if COPPER not in amounts or amounts[COPPER] <= 0:
        raise ValueError("doping estimate requires a positive Cu amount")
    anion_charge = 0.0
    cation_charge = 0.0
    for symbol, amount in amounts.items():
        if symbol == COPPER:
            continue
        if symbol == OXYGEN:
            anion_charge += 2.0 * amount
        elif symbol in HALOGEN_ANIONS:
            anion_charge += 1.0 * amount
        elif symbol in FORMAL_VALENCES:
            cation_charge += FORMAL_VALENCES[symbol] * amount
        else:
            raise ValueError(f"doping estimate has no frozen valence for element {symbol}")
    copper_amount = amounts[COPPER]
    return (anion_charge - cation_charge - COPPER_BASELINE_VALENCE * copper_amount) / copper_amount
