"""PR-6 legacy-evaluation compatibility leaf.

The package initializer intentionally exports nothing.  Protected Research Kernel/controller
packages consume only ordinary protocol, execution, and raw-observation contracts; importing the
legacy harness adapter is an explicit outer-composition decision.
"""

__all__: list[str] = []
