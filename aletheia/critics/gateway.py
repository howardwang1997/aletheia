"""Critic gateway: fans a target (design/results) out to the configured vendor
panel, each producing a supportive and an adversarial pass, then aggregates into a
``CritiquePanel`` (consensus + gate), persists it, and emits an event.

Phase 1 runs a single vendor (OpenAI / GPT-5.5) via either transport. The other
vendors slot in by adding providers to ``_PROVIDERS`` and enabling them in
``critics.yaml`` — no gateway changes needed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aletheia.config import get_settings
from aletheia.config.settings import CriticConfig, CriticsConfig
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.providers.openai_api import OpenAIAPIProvider
from aletheia.critics.providers.openai_cli import OpenAICodexProvider
from aletheia.critics.schemas import CriticFinding, Critique, CritiquePanel
from aletheia.events.bus import get_bus, make_event
from aletheia.memory.service import record_critique_panel

# vendor id -> {transport: provider class}. Phase 1 implements OpenAI only.
_PROVIDERS: dict[str, dict[str, type[CriticProvider]]] = {
    "openai": {"api": OpenAIAPIProvider, "cli": OpenAICodexProvider},
}

_STANCES = ("supportive", "adversarial")


def _instruction(target: str, stance: str) -> str:
    role = (
        "You are an expert, rigorous peer reviewer of machine-learning experiments "
        "in materials science."
    )
    focus = {
        "design": (
            "Review the proposed EXPERIMENT DESIGN before it runs. Scrutinize: data "
            "leakage between train/test, the appropriateness of the model & features, "
            "whether baselines are adequate, the metric choice, statistical validity, "
            "and reproducibility."
        ),
        "results": (
            "Review the experiment RESULTS. Scrutinize: data leakage, overfitting, "
            "whether the metrics actually support the claims, baseline comparisons, and "
            "whether conclusions are over-stated."
        ),
        "direction": (
            "Review the research DIRECTION at a high level: novelty, scope, and whether "
            "it is worth pursuing."
        ),
    }[target]
    stance_text = {
        "supportive": (
            "Take a SUPPORTIVE stance: assume good faith, identify strengths, and give "
            "constructive suggestions — but still flag any real blockers honestly."
        ),
        "adversarial": (
            "Take an ADVERSARIAL (red-team) stance: actively try to find what is wrong, "
            "what could invalidate the results, and the strongest objections."
        ),
    }[stance]
    return (
        f"{role}\n{focus}\n{stance_text}\n\n"
        "Return your review as JSON with fields: verdict (approve | "
        "approve_with_changes | reject), confidence (0..1), summary, and findings "
        "(each: severity [blocker|major|minor|nit|praise], category "
        "[validity|leakage|stats|baseline|reproducibility|novelty|scope|cost], claim, "
        "evidence, suggestion). Reserve 'reject'/'blocker' for issues that genuinely "
        "invalidate the work."
    )


class CriticGateway:
    def __init__(self, config: CriticsConfig | None = None) -> None:
        self.config = config or get_settings().critics

    def _providers(self) -> list[CriticProvider]:
        providers: list[CriticProvider] = []
        for c in self.config.active:
            impls = _PROVIDERS.get(c.id)
            if not impls:
                continue  # vendor not implemented yet (Phase 2)
            cls = impls.get(c.transport)
            if cls is None:
                continue
            providers.append(cls(c))
        return providers

    # --- consensus (any_blocker rule) ---
    def _consensus(self, critiques: list[Critique]) -> tuple[str, bool]:
        has_blocker = any(
            cq.verdict == "reject" or any(f.severity == "blocker" for f in cq.findings)
            for cq in critiques
        )
        if has_blocker:
            return "reject", False
        if any(cq.verdict == "approve_with_changes" for cq in critiques):
            return "approve_with_changes", True
        return ("approve", True) if critiques else ("approve", True)

    def _canned_panel(self, target: str, target_ref: str) -> CritiquePanel:
        finding = CriticFinding(
            severity="minor",
            category="baseline",
            claim="(critic-dry-run) baselines look adequate",
            evidence="no real model was queried",
            suggestion="add a mean-predictor baseline for context",
        )
        critiques = [
            Critique(
                critic_id="openai",
                stance=st,
                verdict="approve_with_changes" if st == "adversarial" else "approve",
                confidence=0.7,
                summary=f"[critic-dry-run] {st} pass on {target}",
                findings=[finding] if st == "adversarial" else [],
            )
            for st in _STANCES
        ]
        consensus, gate = self._consensus(critiques)
        return CritiquePanel(
            target=target,
            target_ref=target_ref,
            critiques=critiques,
            consensus_verdict=consensus,
            gate_passed=gate,
        )

    def review_sync(
        self,
        target: str,
        content_obj: dict[str, Any],
        target_ref: str,
        dry_run: bool = False,
    ) -> CritiquePanel:
        providers = [] if dry_run else self._providers()
        if not providers:
            panel = self._canned_panel(target, target_ref)
        else:
            content = json.dumps(content_obj, indent=2, default=str)
            critiques: list[Critique] = []
            for prov in providers:
                for stance in _STANCES:
                    resp = prov.review(_instruction(target, stance), content)
                    critiques.append(
                        Critique(critic_id=prov.critic_id, stance=stance, **resp.model_dump())
                    )
            consensus, gate = self._consensus(critiques)
            panel = CritiquePanel(
                target=target,
                target_ref=target_ref,
                critiques=critiques,
                consensus_verdict=consensus,
                gate_passed=gate,
            )
        record_critique_panel(
            target=panel.target,
            target_ref=panel.target_ref,
            consensus_verdict=panel.consensus_verdict,
            gate_passed=panel.gate_passed,
            raw_json=panel.model_dump(),
        )
        return panel

    async def review(
        self,
        target: str,
        content_obj: dict[str, Any],
        target_ref: str,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> CritiquePanel:
        """Async wrapper: runs the (blocking) provider calls off the event loop and
        publishes a ``critique_panel`` event."""
        panel = await asyncio.to_thread(
            self.review_sync, target, content_obj, target_ref, dry_run
        )
        await get_bus().publish(
            make_event(
                "critique_panel",
                run_id=run_id,
                agent="critic",
                payload={
                    "target": panel.target,
                    "target_ref": panel.target_ref,
                    "consensus_verdict": panel.consensus_verdict,
                    "gate_passed": panel.gate_passed,
                    "critiques": [
                        {
                            "critic_id": c.critic_id,
                            "stance": c.stance,
                            "verdict": c.verdict,
                            "summary": c.summary,
                            "findings": [f.model_dump() for f in c.findings],
                        }
                        for c in panel.critiques
                    ],
                },
            )
        )
        return panel
