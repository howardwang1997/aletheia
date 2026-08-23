# Aletheia — Architecture

> **Status note (2026-08-23):** this file describes the currently implemented/legacy execution
> architecture. The proposed target direction is
> [`END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md`](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
> PR-0 now freezes the legacy authority boundary, and PR-1 supplies the pure, deterministic
> `research_kernel` contracts/reducer. The fixed global FSM and regression-shaped `DomainPlugin`
> remain compatibility paths; there is not yet an authoritative kernel event store, controller,
> Protocol IR, or execution fabric. New scientific functionality must target those RFC boundaries.

## Ultimate goal

**用AI做最前沿的科学研究 — AI conducting frontier scientific research, end to end.**
Aletheia is not an AutoML-on-a-benchmark tool. The north star is a system that poses novel,
literature-grounded questions, designs and runs experiments with frontier methods, reasons about
what it learned (claims, mechanisms, ablations) against published SOTA, and writes up cited results —
autonomously, within guardrails. Every increment is weighed against that goal.

## Invariants (the safety/quality spine — never traded for a feature)

1. **Deterministic FSM + hard gates.** No free-roaming autonomy; capability is added as gated stages.
2. **Honest evaluation.** A fixed, leakage-aware harness computes metrics (leave-chemical-system-out is
   the honest headline for materials); the agent never grades its own homework — when it authors training
   code, the harness re-computes the evaluation independently.
3. **Adversarial cross-model peer review.** Each hard gate runs several *distinct-vendor* models, ≥1 as a
   red-team; consensus is a deterministic rule over the final round (no single judge). Novelty / SOTA
   claims must be grounded in cited literature.
4. **Full provenance.** Every transition, decision, metric, critique, and artifact is persisted to the
   Postgres ledger; every claim traces to artifacts + code (a per-experiment PR).
5. **Budget guardrails + sandboxed code.** Per-run caps with auto-pause; production authored code must
   run in a no-network, read-only, capability-dropped, resource-capped Docker sandbox. The current host
   subprocess fallback is soft isolation and remains a P0 gap.
6. **Evaluated frontier models.** Track frontier aliases for exploration, but record and pin an
   explicit model identifier for benchmark and reproduction runs.
7. **Human role.** The human sets the domain/direction and connects data/keys; the AI does the science
   lights-out within the guardrails. Irreversible/outward actions (repo creation, purchases, publishing)
   stay gated.

## Lifecycle (target)

The human sets a domain/direction; the lab then runs, lights-out, through a deterministic FSM whose hard
gates are cross-model peer reviews:

```
(human sets domain/direction)
  → SURVEY              deep literature research (arXiv / OpenAlex), ingested into recall
  → IDEATE / HYPOTHESIZE   novelty-checked against the literature   → [novelty + feasibility gate]
  → EXPERIMENT_DESIGN                                               → [critique_design gate]
  → CODE → EXECUTION    SOTA methods / simulation, sandboxed
  → ANALYSIS            scientific claims + ablations, vs published SOTA → [review_results gate]
  → UPDATE BELIEFS / choose the next experiment    ⟲  (campaign loop)
  → WRITE PAPER         structured + cited          → SUBMIT (per-experiment PR)
```

Current state: the EXPERIMENT_DESIGN → ARCHIVE spine is built and merged (Phase 1–2), with cross-model
peer-review gates, pgvector semantic recall, GitHub-App + platform IAM/RBAC, and a coder behind an AST
gate + Docker hard sandbox. The transformation toward the lifecycle above is tracked in the approved plan
(`SURVEY` + literature grounding is the running iteration; ideation, scientific analysis, cited papers,
campaigns, and multi-domain generalization follow).

## Components

```
Next.js dashboard  ⇄  FastAPI control plane  ⇄  Postgres task + command/event ledger  → cursor SSE
                                                    │
                                           independent durable workers
                                                    │
   ┌──────────────┬──────────────┬──────────────────┼──────────────┐
   orchestrator    critic gateway   memory/ledger    scheduler/FSM   compute + IAM
   (Claude or GPT, (cross-vendor    (Postgres +      (durable task   (local subprocess
    isolated per    peer review)     pgvector recall)  + driver)       / Docker sandbox;
    stage)                                                               GitHub App repos)
```

- **orchestrator** (`aletheia/orchestrator/`): isolated `run_worker` per stage, selected globally via
  `ALETHEIA_ORCHESTRATOR_PROVIDER=claude|openai`. OpenAI additionally selects
  `ALETHEIA_OPENAI_AUTH_MODE=subscription|api_key`: subscription calls the official non-interactive
  Codex CLI with the cached ChatGPT login, while API-key mode calls Responses directly.
  Provider-neutral local tools are adapted to Claude MCP, strict Responses functions, or a strict
  Codex control loop; the main loop retains only structured results.
- **critic gateway** (`aletheia/critics/`): distinct-vendor reviewers, dynamic rebuttal rounds, rule-based
  consensus.
- **memory** (`aletheia/memory/`): the ledger is the source of truth; pgvector `recall` surfaces relevant
  prior work (own runs and — from the SURVEY iteration — external literature) before designing.
- **scheduler/jobs** (`aletheia/scheduler/`, `aletheia/jobs/`): launch/resume enters the Postgres
  durable queue; a separate worker leases and heartbeats the deterministic FSM driver. Queue state
  coordinates delivery but never replaces typed scientific ledgers. Transactional scientific
  commands atomically bind migrated domain writes to result receipts and keyed events. One-time
  outward actions expose one raw authorization token, retain a stable provider idempotency key, and
  require reconciliation instead of automatic replay when the remote outcome is unknown.
- **compute** (`aletheia/compute/`): `local` restricted subprocess (default) or `docker` hard sandbox.
- **coder** (`aletheia/coder/`): authors model code behind the AST gate; runs only in the sandbox.
- **iam** (`aletheia/iam/`, `aletheia/auth/`): GitHub App for repo/branch/PR-per-experiment; session login
  + owner/operator/viewer RBAC for the dashboard/API.
