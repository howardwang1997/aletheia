"""Shared helpers for the real AI-authored demonstration e2e scripts.

Imported as a sibling module (``scripts/`` is on ``sys.path`` when a script is run directly):

    from _e2e_common import RunRecorder, write_e2e_summary

The point is auditability: after a real run, a reviewer should be able to open ONE JSON file and
tell what happened — which route fired (AI-authored vs a registered fallback), whether the
demonstration held, what the independent audit said, and the final claim ledger — without
scraping the live console stream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aletheia.memory.service import get_run, list_claims, list_metrics
from aletheia.paths import artifacts_dir

# the explicit capability id the driver routes to when the AI authored the computation; any other
# value (or None) means a registered/hand-built demonstration ran instead.
AI_AUTHORED_CAPABILITY_ID = "ai_authored_demonstration"


class RunRecorder:
    """Accumulates the event stream so the summary can be built without a second query.

    Keeps the LAST payload seen for each event type (last-wins), which is what we want for the
    single-experiment e2e scripts: the final ``demonstration`` / ``demonstration_audit`` /
    ``claims`` events reflect the run's outcome.
    """

    def __init__(self) -> None:
        self._last: dict[str, dict] = {}

    def observe(self, evt: dict) -> None:
        self._last[evt.get("type", "")] = evt.get("payload") or {}

    def payload(self, event_type: str) -> dict:
        return self._last.get(event_type, {})


def _claim_ledger(run_id: str) -> list[dict]:
    """The final claim ledger in the SAME row shape the ``claims`` event/UI use."""
    rows: list[dict] = []
    for c in list_claims(run_id):
        rows.append({
            "claim_type": c.get("claim_type"),
            "status": c.get("status"),
            "strength": c.get("strength"),
            "evidence_kinds": sorted({e.get("evidence_kind") for e in (c.get("evidence") or [])
                                      if e.get("evidence_kind")}),
            "claim_text": str(c.get("claim_text", ""))[:240],
        })
    return rows


def write_e2e_summary(
    *,
    run_id: str,
    exp_id: str,
    timestamp: str,
    prefer_authored: bool,
    recorder: RunRecorder,
    domain: str,
) -> Path:
    """Write a compact, machine-readable run summary under ``artifacts/`` and return its path."""
    demo = recorder.payload("demonstration")  # last-wins -> the POST-audit verdict if re-published
    audit = recorder.payload("demonstration_audit")
    demo_code = recorder.payload("demonstration_code")
    prereg = recorder.payload("preregistration")
    repro = recorder.payload("reproduction")
    exploration = recorder.payload("demonstration_exploration")  # K1 explore->confirm seal
    run = get_run(run_id) or {}
    route = demo.get("capability")  # None until a demonstration actually computes

    summary: dict[str, Any] = {
        "run_id": run_id,
        "exp_id": exp_id,
        "timestamp": timestamp,
        "domain": domain,
        "status": run.get("status"),
        # did the FRONTIER (AI-authored) path actually fire, or did a registered capability run?
        "demonstration_prefer_authored": prefer_authored,
        "route": route,
        "ai_authored_route": route == AI_AUTHORED_CAPABILITY_ID,
        "metrics": list_metrics(exp_id),
        "claims": _claim_ledger(run_id),
        "demonstration": {
            "computed": demo.get("computed"),
            "holds": demo.get("holds"),
            "test_statistic": demo.get("test_statistic"),
            "control_statistic": demo.get("control_statistic"),
            "capability": route,
            "audit_refuted": demo.get("audit_refuted"),
            # K1 explore->confirm seal: did the demonstration run on a held-out CONFIRM partition the
            # authoring never saw? False/None => the seal was NOT applied (blind fallback), which
            # caps the formulation claim below `strong`.
            "exploration_applied": demo.get("exploration_applied"),
            "n_confirm": demo.get("n_confirm"),
        },
        # the EXPLORATION phase: descriptive observations the AI measured on the disjoint explore
        # subset to calibrate its pre-registered threshold (never an input to the verdict). Empty
        # when the seal was not applied. ``exploration_missing`` = the AI-authored demo bypassed the
        # seal (so a reviewer can tell a sealed run from a blind-fallback one at a glance).
        "exploration": {
            "applied": bool(demo.get("exploration_applied")),
            "exploration_missing": (route == AI_AUTHORED_CAPABILITY_ID
                                    and not demo.get("exploration_applied")),
            "observations": exploration.get("observations"),
            "n_explore": exploration.get("n_explore"),
            "n_confirm": exploration.get("n_confirm") or demo.get("n_confirm"),
            "split": exploration.get("split") or demo.get("split_meta"),
        },
        "demonstration_audit": {
            "verdict": audit.get("verdict"),  # passed | reject | degraded | error
            "gate_passed": audit.get("gate_passed"),
            "auditors": audit.get("auditors"),
            "n_auditors": audit.get("n_auditors"),
            "min_vendors": audit.get("min_vendors"),
            "error": audit.get("error"),
        },
        # reproduction decomposed into qualitative (verdict) vs statistical (statistic) stability,
        # with both seeds + statistics so a reviewer can audit a statistic swing directly.
        "reproduction": {
            "demonstration_reproduced": repro.get("demonstration_reproduced"),
            "verdict_stable": repro.get("demonstration_verdict_stable"),
            "statistic_stable": repro.get("demonstration_statistic_stable"),
            "original_statistic": repro.get("demonstration_original_statistic"),
            "repro_statistic": repro.get("demonstration_repro_statistic"),
            "reproduce_factor": repro.get("demonstration_reproduce_factor"),
            "seeds": repro.get("demonstration_seeds"),
        },
        "demonstration_code": {
            "accepted": demo_code.get("accepted"),
            "lines": demo_code.get("lines"),
            "preregistration_valid": demo_code.get("preregistration_valid"),
            "reasons": demo_code.get("reasons"),
        },
        "preregistration": prereg,
    }
    path = artifacts_dir() / f"demo_e2e_{domain}_{run_id}_{timestamp}.json"
    path.write_text(json.dumps(summary, indent=2, default=str))
    return path
