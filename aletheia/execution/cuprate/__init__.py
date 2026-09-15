"""Cuprate diagnostic capability (ARL-2 keystone B, PR B7).

One seeded deterministic analysis over the registered dataset card's rows:
the complexity-matched control contrast (D1) ported from
``scripts/cuprate_matched_control.py`` and the doping-deviation
stratification (D2) built on the permutation-null machinery of
``scripts/chem_effect_probe.py`` (commit 96abe08, SEED=0 lineage).
Both source scripts stay unmodified; this package is the copy-port with
hardcoded CSV paths replaced by the registered-card binding.
"""
