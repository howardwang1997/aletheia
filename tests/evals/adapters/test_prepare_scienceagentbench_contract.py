"""Non-Docker argument contracts for the ScienceAgentBench preparation CLI."""

from __future__ import annotations

import pytest

from aletheia.evals.adapters.scienceagentbench import parse_custom_instance_requirements


def test_custom_requirements_are_explicit_and_complete():
    assert parse_custom_instance_requirements(
        ["1=scikit-learn,numpy", "2=rdkit"], selected_ids=("1", "2")
    ) == {"1": ("scikit-learn", "numpy"), "2": ("rdkit",)}

    with pytest.raises(ValueError, match="one --required-distribution"):
        parse_custom_instance_requirements(["1=scikit-learn"], selected_ids=("1", "2"))
    with pytest.raises(ValueError, match="must use"):
        parse_custom_instance_requirements(["1"], selected_ids=("1",))
    with pytest.raises(ValueError, match="unique"):
        parse_custom_instance_requirements(["1=numpy,numpy"], selected_ids=("1",))
