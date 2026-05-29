"""Critic gateway: a cross-model PEER REVIEW of a target (design/results).

Each gate fans out to several *distinct* vendor models (GPT-5.5 / Gemini / DeepSeek
/ Zhipu), each reviewing in its own isolated context with an assigned stance (≥1
adversarial). When reviewers disagree, the panel runs more **rebuttal rounds** —
each round feeds back the prior reviews plus an author (Opus) rebuttal so reviewers
can update — early-stopping on convergence, capped by the gate's importance. A
deterministic rule then yields ``consensus_verdict`` + ``gate_passed``; the full
multi-round transcript is persisted and streamed.

Vendors plug in via ``_PROVIDERS`` + ``critics.yaml``; the round/importance policy
lives in ``policy.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aletheia.config import get_settings
from aletheia.config.settings import CriticsConfig
from aletheia.critics import policy
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.providers.gemini_api import GeminiAPIProvider
from aletheia.critics.providers.openai_api import OpenAIAPIProvider
from aletheia.critics.providers.openai_cli import OpenAICodexProvider
from aletheia.critics.providers.openai_compatible import DeepSeekAPIProvider, ZhipuAPIProvider
from aletheia.critics.schemas import CriticFinding, Critique, CritiquePanel
from aletheia.events.bus import get_bus, make_event
from aletheia.memory.service import record_critique_panel
from aletheia.orchestrator.worker import run_worker

# vendor id -> {transport: provider class}. Cross-vendor panel (Phase 2).
_PROVIDERS: dict[str, dict[str, type[CriticProvider]]] = {
    "openai": {"api": OpenAIAPIProvider, "cli": OpenAICodexProvider},
    "gemini": {"api": GeminiAPIProvider},
    "deepseek": {"api": DeepSeekAPIProvider},
    "zhipu": {"api": ZhipuAPIProvider},
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


def _round_context(prev: list[Critique], rebuttal: str) -> str:
    """Extra content shown to reviewers in a rebuttal round."""
    reviews = "\n".join(
        f"- [{c.critic_id}/{c.stance}] {c.verdict}: {c.summary}"
        + ("" if not c.findings else " | " + "; ".join(f"{f.severity}:{f.claim}" for f in c.findings))
        for c in prev
    )
    return (
        "\n\n--- PRIOR ROUND PEER REVIEWS ---\n" + reviews
        + "\n\n--- AUTHOR REBUTTAL ---\n" + rebuttal
        + "\n\nReconsider your verdict in light of the other reviews and the author's "
        "rebuttal. Update it (or maintain it) and justify; drop objections the rebuttal "
        "adequately answers."
    )


class CriticGateway:
    def __init__(self, config: CriticsConfig | None = None) -> None:
        self.config = config or get_settings().critics

    def _has_credentials(self, c: Any) -> bool:
        """A vendor is usable only if its credentials are present. OpenAI via the
        Codex CLI uses the Coding-Plan login (no key); everything else needs a key.
        This lets enabled-but-unkeyed vendors be skipped gracefully (e.g. run
        OpenAI-only until Gemini/DeepSeek keys are configured) instead of crashing."""
        if c.id == "openai" and c.transport == "cli":
            return True
        return bool(get_settings().vendor_key(c.id))

    def _providers(self) -> list[CriticProvider]:
        providers: list[CriticProvider] = []
        for c in self.config.active:
            impls = _PROVIDERS.get(c.id)
            if not impls:
                continue
            cls = impls.get(c.transport)
            if cls is None or not self._has_credentials(c):
                continue
            providers.append(cls(c))
        return providers

    # --- consensus (config-driven rule over the FINAL round) ---
    def _consensus(self, critiques: list[Critique]) -> tuple[str, bool]:
        if not critiques:
            return "approve", True
        has_blocker = any(
            cq.verdict == "reject" or any(f.severity == "blocker" for f in cq.findings)
            for cq in critiques
        )
        if self.config.consensus.rule == "majority":
            rejects = sum(1 for cq in critiques if cq.verdict == "reject")
            if rejects * 2 > len(critiques):
                return "reject", False
            if has_blocker or any(cq.verdict == "approve_with_changes" for cq in critiques):
                return "approve_with_changes", True
            return "approve", True
        # any_blocker (default): a blocker surviving to the final round is unrefuted
        if has_blocker:
            return "reject", False
        if any(cq.verdict == "approve_with_changes" for cq in critiques):
            return "approve_with_changes", True
        return "approve", True

    # --- one round of independent reviews ---
    def _review_one(self, prov: CriticProvider, target: str, stance: str, content: str) -> Critique:
        resp = prov.review(_instruction(target, stance), content)
        return Critique(critic_id=prov.critic_id, stance=stance, **resp.model_dump())

    def _run_round_sync(
        self, target: str, content: str, assignments: list[tuple[CriticProvider, str]]
    ) -> list[Critique]:
        return [self._review_one(p, target, st, content) for p, st in assignments]

    # --- dry-run canned panel (two distinct models, gate passes) ---
    def _canned_panel(self, target: str, target_ref: str) -> CritiquePanel:
        critiques = [
            Critique(
                critic_id="gemini", stance="supportive", verdict="approve", confidence=0.8,
                summary=f"[critic-dry-run] supportive pass on {target}", findings=[],
            ),
            Critique(
                critic_id="openai", stance="adversarial", verdict="approve_with_changes",
                confidence=0.7, summary=f"[critic-dry-run] adversarial pass on {target}",
                findings=[
                    CriticFinding(
                        severity="minor", category="baseline",
                        claim="(critic-dry-run) baselines look adequate",
                        evidence="no real model was queried",
                        suggestion="add a mean-predictor baseline for context",
                    )
                ],
            ),
        ]
        consensus, gate = self._consensus(critiques)
        return CritiquePanel(
            target=target, target_ref=target_ref, critiques=critiques,
            consensus_verdict=consensus, gate_passed=gate,
        )

    def review_sync(
        self, target: str, content_obj: dict[str, Any], target_ref: str, dry_run: bool = False
    ) -> CritiquePanel:
        """Single-round peer review (sync). The async ``review`` adds rebuttal rounds."""
        providers = [] if dry_run else self._providers()
        if not providers:
            panel = self._canned_panel(target, target_ref)
        else:
            content = json.dumps(content_obj, indent=2, default=str)
            critiques = self._run_round_sync(target, content, policy.assign_stances(providers))
            consensus, gate = self._consensus(critiques)
            panel = CritiquePanel(
                target=target, target_ref=target_ref, critiques=critiques,
                consensus_verdict=consensus, gate_passed=gate,
            )
        record_critique_panel(
            target=panel.target, target_ref=panel.target_ref,
            consensus_verdict=panel.consensus_verdict, gate_passed=panel.gate_passed,
            raw_json=panel.model_dump(),
        )
        return panel

    async def _author_rebuttal(
        self, target: str, content_obj: dict[str, Any], critiques: list[Critique],
        run_id: str | None, dry_run: bool,
    ) -> str:
        objections = "; ".join(
            f"[{c.critic_id}] {f.severity}:{f.claim}"
            for c in critiques for f in c.findings
            if f.severity in ("blocker", "major")
        ) or "(no blocker/major objections)"
        prompt = (
            f"You are the AUTHOR defending your {target}. Reviewers raised: {objections}.\n"
            f"ARTIFACT: {json.dumps(content_obj, default=str)}\n"
            "Respond concisely: for each, either refute it with evidence or concede and "
            "state the fix. Be honest — concede real problems."
        )
        return await run_worker(
            run_id or "", "author-rebuttal", prompt,
            dry_run=dry_run, dry_value="[dry-run] Author rebuttal: objections addressed.",
        )

    async def _run_round_async(
        self, target: str, content: str, assignments: list[tuple[CriticProvider, str]]
    ) -> list[Critique]:
        results = await asyncio.gather(
            *[asyncio.to_thread(self._review_one, p, target, st, content) for p, st in assignments],
            return_exceptions=True,
        )
        # a reviewer that errored (vendor API hiccup) is dropped, not fatal to the gate
        return [r for r in results if isinstance(r, Critique)]

    async def review(
        self, target: str, content_obj: dict[str, Any], target_ref: str,
        run_id: str | None = None, dry_run: bool = False,
    ) -> CritiquePanel:
        """Cross-model peer review with dynamic rebuttal rounds. Streams a
        ``critique_panel`` event per round and persists the full transcript."""
        providers = [] if dry_run else self._providers()
        rounds: list[list[Critique]] = []
        rebuttals: list[str] = []

        if not providers:
            panel = self._canned_panel(target, target_ref)
            rounds = [panel.critiques]
        else:
            assignments = policy.assign_stances(providers)
            rconf = self.config.rounds
            max_rounds = rconf.max_rounds(target)
            base_content = json.dumps(content_obj, indent=2, default=str)
            extra = ""
            r = 0
            while True:
                r += 1
                critiques = await self._run_round_async(target, base_content + extra, assignments)
                rounds.append(critiques)
                await self._emit_round(target, target_ref, critiques, r, max_rounds, run_id)
                if not policy.should_continue(
                    r, max_rounds, critiques,
                    dynamic=rconf.dynamic, threshold=rconf.disagreement_threshold,
                ):
                    break
                rebuttal = await self._author_rebuttal(
                    target, content_obj, critiques, run_id, dry_run
                )
                rebuttals.append(rebuttal)
                extra = _round_context(critiques, rebuttal)

            final = rounds[-1]
            consensus, gate = self._consensus(final)
            panel = CritiquePanel(
                target=target, target_ref=target_ref, critiques=final,
                consensus_verdict=consensus, gate_passed=gate,
            )

        raw = panel.model_dump()
        raw["rounds"] = [[c.model_dump() for c in rd] for rd in rounds]
        raw["rebuttals"] = rebuttals
        record_critique_panel(
            target=panel.target, target_ref=panel.target_ref,
            consensus_verdict=panel.consensus_verdict, gate_passed=panel.gate_passed,
            raw_json=raw,
        )
        await self._emit_panel(panel, len(rounds), run_id)
        return panel

    # --- events ---
    def _critique_payload(self, critiques: list[Critique]) -> list[dict[str, Any]]:
        return [
            {
                "critic_id": c.critic_id, "stance": c.stance, "verdict": c.verdict,
                "summary": c.summary, "findings": [f.model_dump() for f in c.findings],
            }
            for c in critiques
        ]

    async def _emit_round(
        self, target: str, target_ref: str, critiques: list[Critique],
        round_idx: int, max_rounds: int, run_id: str | None,
    ) -> None:
        await get_bus().publish(
            make_event(
                "critique_round", run_id=run_id, agent="critic",
                payload={
                    "target": target, "target_ref": target_ref,
                    "round": round_idx, "max_rounds": max_rounds,
                    "disagreement": round(policy.disagreement(critiques), 3),
                    "critiques": self._critique_payload(critiques),
                },
            )
        )

    async def _emit_panel(self, panel: CritiquePanel, rounds: int, run_id: str | None) -> None:
        await get_bus().publish(
            make_event(
                "critique_panel", run_id=run_id, agent="critic",
                payload={
                    "target": panel.target, "target_ref": panel.target_ref,
                    "consensus_verdict": panel.consensus_verdict,
                    "gate_passed": panel.gate_passed, "rounds": rounds,
                    "critiques": self._critique_payload(panel.critiques),
                },
            )
        )
