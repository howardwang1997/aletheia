# Self-review before the Codex pass — 2026-06-04

Scope: the work merged since the last Codex review (PRs #30–#55). This is an honest map for
the reviewer: what was built, the holes I found + fixed in this self-review, the holes I am
**flagging unfixed**, and whether it is on the path to the north star (AI doing frontier
scientific research).

## What shipped (themes)
- **Real-run correctness + persistence** (#30–#31): domain-aware artifact contract, fail-closed
  dense fallback, robust model persistence (cloudpickle).
- **Paradigm mode P1–P4** (#32–#35): `formulation` claim type; contribution_type; results gate
  judges paradigm work on the right axes (SOTA-irrelevant); the claim is grounded only by a
  reproducible discriminating **demonstration**; molecules computes two demonstrations
  (scaffold-generalization, activity-cliff Lipschitz) deterministically on real ESOL.
- **Critic reliability** (#36–#46): Codex token-race serialized; GLM (Coding-Plan endpoint, key
  from OpenCode); Claude CLI critic; Grok (xAI, key from sciminer); throttle + 1113 fast-fail;
  Claude-only default while Codex/GLM creds are down.
- **Front-end (idea generation)** (#47–#54): adversarial ideation debate; conversational
  gap-discovery (ranked GENUINE open gaps); literature retrieval upgrade (Semantic Scholar +
  cross-encoder rerank + relevance cutoff); demonstration decoupled from the performance eval;
  survey rate-limit resilience (query cache + fast-fail 429).
- **Grounding honesty** (#55, this self-review): see below.

## Fixed in this self-review (PR #55)
1. **Mismatched demonstration** — `run_demonstration` dispatched any non-cliff claim to the
   *scaffold* demo, so a paradigm claim about something else would be "grounded" by an unrelated
   demonstration. Now **fail-closed**: compute only the demonstration the claim describes, else
   `None` (formulation stays a proposal).
2. **Critic blind to the computed result** — the results-review payload carried only the
   *proposed* spec, not the *computed* result. Added `demonstration_result` ({form, holds,
   statistic, detail}); the paradigm critic instruction now checks it AND that it measures what
   the formulation claims.

## Also fixed in this self-review (PR #57)
- **CLI critic arg-length** — `solution_code` (the only unbounded field) is now capped at 20 KB in
  the results-review payload, so the CLI critics (which pass content as a process arg) never
  approach ARG_MAX.
- **Deterministic demonstration ⇒ weak "reproduction"** — the cliff demo now computes on a
  SEED-perturbed 90% subsample (the design's `random_state` is threaded into the demonstration),
  so the reproduction re-run is a genuine independent re-computation; `demonstration_reproduced`
  now means "still holds AND the statistic is order-stable (within ~2×)", not a bit-identical
  recompute.
- **`_instruction` role text** is now domain-neutral (materials / molecules / ML methodology).

## Holes I am FLAGGING (unfixed — please scrutinize)
- **Demonstration executor is single-domain + keyword-matched** (the real coverage limit). Only
  `molecules` implements `run_demonstration`, only two forms (cliff, scaffold), matched by
  keyword; a claim phrased without the exact keywords fail-closes (returns None), and
  materials/RAG can't ground a paradigm at all. The general "compute an arbitrary AI-proposed
  demonstration" is unsolved — a research problem, not a patch.
- **Rerank cutoff (`reranker_min_relevance=0.05`) is uncalibrated** for ms-marco scores — could
  over-filter or under-filter. (A "keep-at-least-N" safety was rejected because it would undo the
  off-topic drop that is the feature's point; left as a tuning concern.)
- **Survey grounding has no offline fallback.** When all free literature APIs 429 simultaneously,
  the survey gets 0 papers → fail-closed pause (correct, but blocks). Query cache is exact-key
  only; no global query budget. (An S2 API key — hook in place — removes the dominant 429 source.)
- **Ideation debate is self-debate** (same model proposes + critiques) — weaker independence than
  the cross-vendor gate; observed to help once (caught a SALI rebrand + a tautology), n=1.
- **Secrets live in three places** now (aletheia/.env, sciminer/.env, OpenCode config); all
  gitignored, but the fallback readers remain.

## Path assessment — is it on the right path?
Yes on the highest-leverage axis, with an honest caveat.

- The **front-end** (the explicitly-flagged top gap toward the goal) genuinely advanced:
  gap-discovery surfaces *genuine* open problems with on-topic citations, ideation **debates**
  them, retrieval is relevance-grounded, and a **cross-vendor** adversarial panel judges them.
  The system can now *pose and rigorously vet* frontier-ish questions — not just execute a
  handed plan.
- The **epistemic spine** is stronger and more honest: paradigm grounding, fail-closed
  everywhere, no overclaiming, the demonstration is harness-computed (not LLM-asserted) and now
  critic-verified for match.
- **But substance still trails.** Every paradigm contribution exercised end-to-end has been a
  known/near-known idea — the scaffold-gap is textbook; the cliff-Lipschitz quantity *is* SALI
  (the debate itself flagged this). No genuinely novel finding has been *produced* end-to-end,
  and the demonstration executor covers one domain / two forms. The system has become good at
  *finding and judging* frontier questions; *executing* a genuinely novel demonstration remains
  narrow. Form still outpaces substance — but the gap narrowed markedly on idea-generation.

Net: correct direction, real progress on the front-end + epistemics; the open frontier is a
**general demonstration executor** + a genuinely novel, independently-verified result.
