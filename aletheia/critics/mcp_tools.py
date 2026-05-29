"""MCP tools exposing the critic gates to a Claude-driven loop. The lifecycle
driver calls ``CriticGateway`` directly; these mirror it for the orchestrator."""

from __future__ import annotations

import json
from typing import Any

from aletheia.critics.gateway import CriticGateway


def _verdict_text(panel) -> str:
    lines = [
        f"Critique panel on {panel.target} (ref {panel.target_ref}): "
        f"consensus={panel.consensus_verdict}, gate_passed={panel.gate_passed}"
    ]
    for c in panel.critiques:
        block = [f"  [{c.critic_id}/{c.stance}] {c.verdict}: {c.summary}"]
        for f in c.findings:
            block.append(f"    - {f.severity}/{f.category}: {f.claim} -> {f.suggestion}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def build_critic_tools(run_id: str, experiment_id: str | None, dry_run: bool = False):
    from claude_agent_sdk import tool

    gateway = CriticGateway()
    ref = experiment_id or run_id

    @tool(
        "critique_design",
        "Submit the current experiment design (as a JSON string) for adversarial + "
        "supportive critic review before running. Returns the panel verdict and whether "
        "the design gate passed.",
        {"design_json": str},
    )
    async def critique_design(args: dict[str, Any]) -> dict[str, Any]:
        try:
            design = json.loads(args.get("design_json") or "{}")
        except json.JSONDecodeError as e:
            return {"content": [{"type": "text", "text": f"Invalid design_json: {e}"}]}
        panel = await gateway.review("design", design, ref, run_id=run_id, dry_run=dry_run)
        return {"content": [{"type": "text", "text": _verdict_text(panel)}]}

    @tool(
        "review_results",
        "Submit the experiment results (metrics + design as a JSON string) for critic "
        "review of leakage / overfitting / unsupported claims. Returns the panel verdict.",
        {"results_json": str},
    )
    async def review_results(args: dict[str, Any]) -> dict[str, Any]:
        try:
            results = json.loads(args.get("results_json") or "{}")
        except json.JSONDecodeError as e:
            return {"content": [{"type": "text", "text": f"Invalid results_json: {e}"}]}
        panel = await gateway.review("results", results, ref, run_id=run_id, dry_run=dry_run)
        return {"content": [{"type": "text", "text": _verdict_text(panel)}]}

    return [critique_design, review_results]
