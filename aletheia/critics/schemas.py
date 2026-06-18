"""Structured critic contract. The gateway fans a target (design/results) out to
the configured vendor panel; each vendor returns a ``CriticResponse`` (the part the
model authors), which the gateway wraps into a ``Critique`` (adds critic_id +
stance) and aggregates into a ``CritiquePanel`` (consensus + gate)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["blocker", "major", "minor", "nit", "praise"]
Category = Literal[
    "validity", "leakage", "stats", "baseline", "reproducibility", "novelty", "scope", "cost"
]
Verdict = Literal["approve", "approve_with_changes", "reject"]
Target = Literal["design", "direction", "results", "demonstration_audit"]
Stance = Literal["supportive", "adversarial"]


class CriticFinding(BaseModel):
    severity: Severity
    category: Category
    claim: str
    evidence: str
    suggestion: str


class CriticResponse(BaseModel):
    """What a single vendor authors for one stance (structured-output target).

    Note: no numeric ``ge/le`` bound on ``confidence`` — OpenAI strict structured
    outputs reject ``minimum``/``maximum`` keywords. Range is clamped on use.
    """

    verdict: Verdict
    confidence: float
    summary: str
    findings: list[CriticFinding]


class Critique(CriticResponse):
    """A CriticResponse attributed to a vendor + stance."""

    critic_id: str
    stance: Stance


class CritiquePanel(BaseModel):
    """The aggregated verdict returned to the orchestrator / persisted to the ledger."""

    target: Target
    target_ref: str
    critiques: list[Critique]
    consensus_verdict: Verdict
    gate_passed: bool
