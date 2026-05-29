"""Map a run's domain string to a DomainPlugin. Materials is the only Phase-1
domain; chemistry/others slot in here later."""

from __future__ import annotations

from aletheia.domains.base import DomainPlugin


def get_domain_plugin(domain: str | None) -> DomainPlugin:
    from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

    # Phase 1: every run resolves to the materials band-gap plugin.
    return MaterialsBandGapPlugin()
