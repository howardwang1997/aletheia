"""Phase E: the domain registry dispatches a domain string to its plugin; unknown /
blank domains fall back to the default (materials)."""

from __future__ import annotations

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.domains.molecules.plugin import MoleculePropertyPlugin
from aletheia.domains.registry import get_domain_plugin


def test_registry_dispatches_known_domains():
    assert isinstance(get_domain_plugin("materials"), MaterialsBandGapPlugin)
    assert isinstance(get_domain_plugin("molecules"), MoleculePropertyPlugin)
    assert isinstance(get_domain_plugin("MOLECULES"), MoleculePropertyPlugin)  # case-insensitive


def test_registry_unknown_falls_back_to_materials():
    assert isinstance(get_domain_plugin("astrophysics"), MaterialsBandGapPlugin)
    assert isinstance(get_domain_plugin(None), MaterialsBandGapPlugin)
    assert isinstance(get_domain_plugin(""), MaterialsBandGapPlugin)


def test_every_plugin_exposes_a_profile():
    for d in ("materials", "molecules"):
        prof = get_domain_plugin(d).profile()
        assert prof.headline_metric and prof.task and prof.dry_metrics
        assert prof.headline_metric in prof.dry_metrics  # dry-run carries the headline key
