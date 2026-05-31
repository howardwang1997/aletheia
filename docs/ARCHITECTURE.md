# Aletheia — Architecture

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
5. **Budget guardrails + sandboxed code.** Per-run caps with auto-pause; AI-authored code runs behind an
   AST allowlist gate and a no-network, read-only, capability-dropped, resource-capped Docker sandbox.
6. **Latest models always.**
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
Next.js dashboard  ⇄  FastAPI (session auth + RBAC)  ⇄  event bus → SSE
                                  │
   ┌──────────────┬──────────────┼───────────────┬──────────────┐
   orchestrator    critic gateway   memory/ledger    scheduler/FSM   compute + IAM
   (Opus workers,  (cross-vendor    (Postgres +      (driver:        (local subprocess
    isolated per    peer review)     pgvector recall)  stages+gates)   / Docker sandbox;
    stage)                                                            GitHub App repos)
```

- **orchestrator** (`aletheia/orchestrator/`): isolated `run_worker` per stage; the main loop holds only
  structured results, never sub-task transcripts.
- **critic gateway** (`aletheia/critics/`): distinct-vendor reviewers, dynamic rebuttal rounds, rule-based
  consensus.
- **memory** (`aletheia/memory/`): the ledger is the source of truth; pgvector `recall` surfaces relevant
  prior work (own runs and — from the SURVEY iteration — external literature) before designing.
- **scheduler** (`aletheia/scheduler/`): the deterministic driver walks the FSM and enforces budget.
- **compute** (`aletheia/compute/`): `local` restricted subprocess (default) or `docker` hard sandbox.
- **coder** (`aletheia/coder/`): authors model code behind the AST gate; runs only in the sandbox.
- **iam** (`aletheia/iam/`, `aletheia/auth/`): GitHub App for repo/branch/PR-per-experiment; session login
  + owner/operator/viewer RBAC for the dashboard/API.
