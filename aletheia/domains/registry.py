"""Map a run's domain string to a DomainPlugin. New domains slot in by adding a
loader to ``_LOADERS`` — the rest of the loop is domain-agnostic (it reads each
plugin's ``profile()`` for all domain-specific vocabulary)."""

from __future__ import annotations

import sys
from typing import Callable

from aletheia.domains.base import DomainPlugin


def _materials() -> DomainPlugin:
    from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

    return MaterialsBandGapPlugin()


def _molecules() -> DomainPlugin:
    from aletheia.domains.molecules.plugin import MoleculePropertyPlugin

    return MoleculePropertyPlugin()


def _rag() -> DomainPlugin:
    from aletheia.domains.rag.plugin import RagEvalPlugin

    return RagEvalPlugin()


_LOADERS: dict[str, Callable[[], DomainPlugin]] = {
    "materials": _materials,
    "molecules": _molecules,
    "rag": _rag,
}

DEFAULT_DOMAIN = "materials"


def get_domain_plugin(domain: str | None, *, strict: bool = False) -> DomainPlugin:
    """Resolve a domain string to its plugin. Unknown / blank domains fall back to
    the default (materials), logged so the choice is visible.

    ``strict=True`` is for real autonomous runs: an unknown non-empty domain is a
    blocked research setup, not a safe fallback to unrelated science.
    """
    key = (domain or "").strip().lower()
    loader = _LOADERS.get(key)
    if loader is None:
        if strict and key:
            supported = ", ".join(sorted(_LOADERS))
            raise ValueError(f"unsupported domain {domain!r}; supported domains: {supported}")
        if key:
            print(f"[domains] unknown domain {domain!r}; using {DEFAULT_DOMAIN}", file=sys.stderr)
        loader = _LOADERS[DEFAULT_DOMAIN]
    return loader()
