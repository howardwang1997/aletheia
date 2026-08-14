# aletheia

Autonomous researcher — a personal, lights-out AI Scientist. All you need is deploy and make dataset.

Aletheia plans research directions, designs & runs experiments, analyzes & optimizes,
and writes up results — autonomously, within budget guardrails. A configurable **Claude or GPT**
orchestrator coordinates isolated worker contexts through the Claude Agent SDK or OpenAI Responses
API, alongside a **cross-vendor critic panel**
that reviews designs/results from supportive and adversarial angles. A Postgres
**experiment ledger** is the source-of-truth work log; a **Next.js + FastAPI** dashboard
streams live activity and lets you steer the lab. See the full design in
`docs/` and the approved plan.

## Ultimate goal

**用AI做最前沿的科学研究 — have AI conduct frontier scientific research, end to end.**
Not AutoML on a benchmark: the north star is a system that *poses novel, literature-grounded
questions*, designs and runs experiments with frontier methods, reasons about what it learned,
and writes up cited results — autonomously, within guardrails. Every change is weighed by whether
it moves Aletheia toward that. See the roadmap in the approved plan and `docs/ARCHITECTURE.md`.

## Keystone roadmap

Aletheia's path to the north star is organized around six keystones. The project is past the toy
demo stage and is best understood today as a credible early research operating system, not yet a
reliable autonomous frontier scientist.

1. **Evidence spine — mostly done, still hardening.** Structured claims, evidence refs, claim
   strength/status, fail-closed gates, cross-vendor review, reproduction checks, and write-up rules
   prevent polished prose from outrunning the ledger.
2. **AI-authored demonstration harness — mostly done, still hardening.** The AI can author a
   `compute_demonstration`, but the harness owns `holds` through pre-registration, negative
   controls, probes, audit, and claim finalization. Molecules and materials exercise this path.
3. **Exploratory -> confirmatory demonstrations — built.** The AI calibrates on an exploration
   partition, commits a pre-registered threshold, and is judged only on a disjoint confirmation
   partition. Discovery now screens only the exploration side of the same immutable split.
4. **Campaign learning loop — built and live-validated once.** Harness verdicts update a belief
   ledger and feed typed failure reasons into the next EIG-ranked experiment. Real Campaign Gate v1
   completed a two-round campaign with fresh confirmation batches, one-time final holdout, and a
   pre-sealed SuperCon2 external evaluation; the protocol passed even though the external scientific
   result was negative.
5. **Strong novelty/SOTA grounding — partial.** Literature retrieval, structured findings, and SOTA
   rows exist, but novelty and SOTA claims still need stronger health checks and prior-work mapping
   before the system can reliably tell new science from a rephrased known idea.
6. **Reproducible research bundle — partial.** The final output should be a stable bundle containing
   the question, literature, data card, split metadata, code, artifacts, metrics, claims, audits,
   reproduction, limitations, paper, and reproducibility package/PR.

The remaining load-bearing work is knowledge-grounded novelty/SOTA validation, a richer repertoire
of causal/mechanistic experiments, repeated lower-overlap or laboratory replication, and a complete
evidence bundle.

The executable plan for the next frontier program is
[`docs/FRONTIER_SCIENTIST_F7_F12_DETAILED_PLAN_2026_08_13.md`](docs/FRONTIER_SCIENTIST_F7_F12_DETAILED_PLAN_2026_08_13.md):
F7 independent discovery evaluation, F8 knowledge-boundary grounding, F9/K3 competing causal
hypotheses, F10 an open experiment engine and deep materials domain, F11 durable long-horizon
research portfolios, and F12 reality-linked independent replication.

F7's independent evaluation plane now includes all three engineering-complete public adapters:
ScienceAgentBench scientific coding, an Asta CORE-Bench-Hard reproduction mini-suite, and a
DiscoveryWorld hidden-rule/action-trace suite. DiscoveryWorld runs the candidate and official world
in different offline containers and scores controlled trials, belief revision, explicit governing
rule, terminal task success, and exact repeated trajectories; see
[`docs/benchmarks/DISCOVERYWORLD_ADAPTER.md`](docs/benchmarks/DISCOVERYWORLD_ADAPTER.md). Its final
four-rule suite and the complete 29-test Docker matrix are frozen and passing. F7 still needs the
predeclared baseline matrix, private-suite custody, and frozen acceptance report.

## Invariants (the safety/quality spine — never traded for a feature)

1. **Deterministic FSM + hard gates** — capability is added as gated stages, not free-roaming autonomy.
2. **Honest evaluation** — a fixed, leakage-aware harness computes metrics; the agent never grades its own homework (independent re-compute when it authors training code).
3. **Adversarial cross-model peer review** — ≥1 distinct-vendor red-team reviewer per gate; novelty/SOTA claims must cite literature.
4. **Full provenance** — every transition/decision/metric/critique/artifact is in the ledger; every claim traces to code (a PR).
5. **Budget guardrails + sandboxed code** — per-run caps; every AI-authored smoke, exploration,
   demonstration, training, and reproduction path defaults to one immutable no-network Docker
   boundary. Host subprocess execution is an explicit development-only override and unattended
   real runs fail closed if the hard sandbox is unavailable.
6. **Evaluated frontier models.** Defaults may track current frontier models; benchmark and
   reproduction runs pin an explicit model identifier and record it in provenance.
7. **Human role** — set the domain/direction + connect data/keys; the AI does the science lights-out within the guardrails (irreversible/outward actions stay gated).

## Architecture (one-liner)

`Next.js dashboard ⇄ FastAPI ⇄ {orchestrator (Claude or GPT), critic gateway, memory/ledger,
scheduler, compute, IAM}`, with every message flowing through an **event bus → SSE**.

## Quickstart (Phase 0 — walking skeleton)

Prereqs: `conda`, Docker runtime (Docker Desktop or `colima start`), Node ≥ 18.

```bash
# 1. Python env (conda is required)
conda env create -f environment.yml
conda run -n aletheia pip install -e .

# 2. Infra: Postgres + pgvector
docker compose up -d            # needs a running docker daemon (e.g. `colima start`)

# 3. Config
cp .env.example .env            # dry-run works with no secrets

# 4. Backend
conda run -n aletheia uvicorn aletheia.api.main:app --reload --port 8000

# 5. Dashboard (separate shell)
cd frontend && npm install && npm run dev    # http://localhost:3000
```

Open http://localhost:3000, type a goal, click **Start run**.

The default provider is Claude. Both Claude and OpenAI support subscription login or API-key
authentication. To use GPT with the ChatGPT subscription already logged into Codex CLI, run
`codex login` once and select the OpenAI provider:

```bash
# Claude subscription (default): only needed headless — run `claude setup-token`
ALETHEIA_ORCHESTRATOR_PROVIDER=claude
ALETHEIA_CLAUDE_AUTH_MODE=subscription
CLAUDE_CODE_OAUTH_TOKEN=...            # leave BLANK to inherit the machine login
# or API key:
# ALETHEIA_CLAUDE_AUTH_MODE=api_key
# ANTHROPIC_API_KEY=...

# GPT via ChatGPT/Codex subscription (no OPENAI_API_KEY):
# ALETHEIA_ORCHESTRATOR_PROVIDER=openai
# ALETHEIA_OPENAI_AUTH_MODE=subscription
# ALETHEIA_OPENAI_MODEL=gpt-5.6-sol
# `codex login status` must say: Logged in using ChatGPT

# GPT via the metered OpenAI Responses API:
# ALETHEIA_ORCHESTRATOR_PROVIDER=openai
# ALETHEIA_OPENAI_AUTH_MODE=api_key
# OPENAI_API_KEY=...
# ALETHEIA_OPENAI_MODEL=gpt-5.6-sol
# ALETHEIA_OPENAI_REASONING_EFFORT=high
```

If the selected provider has no usable credentials, Aletheia falls back to **dry-run**. Local tools
share one provider-neutral contract. Claude receives MCP adapters; OpenAI API mode receives strict
Responses functions; subscription mode uses strict Codex CLI control objects and executes only
Aletheia's allowlisted local tools. Subscription calls run in an empty temporary directory with a
read-only sandbox and built-in Codex tools/config disabled. Both GPT paths keep session history
locally and persist canonical events to Aletheia's ledger. See OpenAI's official
[authentication](https://learn.chatgpt.com/docs/auth) and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) documentation.

## Verify

```bash
conda run -n aletheia python -m pytest        # Phase 0 skeleton tests (needs Postgres up)
```

### Real Campaign Gate v1 evidence

Run `4443d7d226b64ffeb16cce722498063a` completed the strict K2 acceptance path: two adaptive rounds
on distinct confirmation batches, Epistemic Seal v2, one final-holdout opening, then one external
SuperCon2 opening with the same locked code and preregistration. The internal final holdout supported
the diagnostic, while the external evaluation did not; the run is therefore honestly archived as
`results_rejected` even though all anti-fakeability acceptance checks passed.

- [Campaign summary](workspaces/4443d7d226b64ffeb16cce722498063a/artifacts/campaign.md)
- [Final holdout record](workspaces/4443d7d226b64ffeb16cce722498063a/artifacts/final_holdout.json)
- [External replication record](workspaces/4443d7d226b64ffeb16cce722498063a/artifacts/external_replication.json)
- [Machine-readable run summary](artifacts/demo_e2e_materials_4443d7d226b64ffeb16cce722498063a_20260813T051822.json)
- [Lossless event transcript](artifacts/transcript_materials_4443d7d226b64ffeb16cce722498063a_20260813T051822.jsonl)

The external asset is an independently pinned literature extraction, not an independent laboratory
campaign. Its provenance discloses formula overlap with the primary UCI/SuperCon-derived dataset.

## Token / cost usage

Every provider call persists token `usage` in the event ledger. Claude SDK calls also persist the
reported `total_cost_usd`; OpenAI Responses and Codex CLI persist exact tokens but no calculated
dollar amount, so the configured per-stage estimate remains the OpenAI USD guardrail until a pricing
calculator is added. Subscription runs may report zero USD despite consuming plan allowance.

```bash
conda run -n aletheia python scripts/usage_report.py             # all runs + grand total
conda run -n aletheia python scripts/usage_report.py --top 10    # priciest runs by tokens
conda run -n aletheia python scripts/usage_report.py <run_id>    # full breakdown for one run
```

Each e2e summary also carries a `usage` block. For Claude it includes the SDK-reported five-hour
window pressure; `token_cap_per_run` (off by default) bounds either provider's total tokens.

## Conversation records

Every model turn, tool call, tool result, and per-call usage is persisted to the `events`
ledger, so the full dialogue of any run can be exported to durable files — a lossless
`.jsonl` (the archive) and a readable, lane-tagged `.md` with a token/cost + 5h-window header.
Each e2e run auto-archives its transcript; any past run can be exported on demand:

```bash
conda run -n aletheia python scripts/export_transcript.py <run_id>   # one run
conda run -n aletheia python scripts/export_transcript.py --last 5   # 5 most recent
conda run -n aletheia python scripts/export_transcript.py --all      # every run with events
```

## Status

The full deterministic research lifecycle runs end-to-end in **dry-run** (DB → event-bus
→ SSE, no model calls) and is exercised by the test suite. Built out through Phases G–P:
fail-closed guardrails, an evidence/claims ledger, structured literature + SOTA tables,
hypothesis scorecards, a reproduction pass, an EIG-ranked experiment planner, and the first
AI-application domain (RAG: lexical & dense retrieval, host-side LLM generation,
cross-vendor faithfulness) alongside the materials and molecules regression domains.

**Honest caveat:** this is still stronger evidence for the *machinery* than for a scientific result.
The first strict live campaign completed, but its external evaluation was negative and the external
literature extraction has disclosed formula overlap with the primary dataset. Repeated campaigns,
stronger novelty grounding, and lower-overlap or laboratory replication are the next milestones.
See `docs/PROJECT_REVIEW.md`, `docs/AUTONOMOUS_RESEARCH_ROADMAP.md`, and the current gap analysis in
`docs/FINAL_GOAL_GAP_ANALYSIS_2026_08_12.md`.
