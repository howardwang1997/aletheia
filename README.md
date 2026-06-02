# aletheia

Autonomous researcher — a personal, lights-out AI Scientist. All you need is deploy and make dataset.

Aletheia plans research directions, designs & runs experiments, analyzes & optimizes,
and writes up results — autonomously, within budget guardrails. A **Claude Opus 4.8**
orchestrator (on the Claude Agent SDK) coordinates Claude worker subagents and a
**cross-vendor critic panel** (GPT-5.5 · latest Gemini · latest DeepSeek · latest Zhipu GLM)
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

## Invariants (the safety/quality spine — never traded for a feature)

1. **Deterministic FSM + hard gates** — capability is added as gated stages, not free-roaming autonomy.
2. **Honest evaluation** — a fixed, leakage-aware harness computes metrics; the agent never grades its own homework (independent re-compute when it authors training code).
3. **Adversarial cross-model peer review** — ≥1 distinct-vendor red-team reviewer per gate; novelty/SOTA claims must cite literature.
4. **Full provenance** — every transition/decision/metric/critique/artifact is in the ledger; every claim traces to code (a PR).
5. **Budget guardrails + sandboxed code** — per-run caps; AI-authored code runs behind an AST gate + a no-network Docker hard sandbox.
6. **Latest models always.**
7. **Human role** — set the domain/direction + connect data/keys; the AI does the science lights-out within the guardrails (irreversible/outward actions stay gated).

## Architecture (one-liner)

`Next.js dashboard ⇄ FastAPI ⇄ {orchestrator (Opus 4.8), critic gateway, memory/ledger,
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

**On a machine already logged into Claude Code, real runs fire automatically** — the
orchestrator inherits your existing subscription login (macOS Keychain / `claude`
CLI), and the OpenAI critic likewise reuses your `codex login`. No keys or tokens in
`.env` are needed; one distinct-vendor critic (OpenAI via Codex) already satisfies the
peer-review gate. Runs fall back to **dry-run** (full DB → event-bus → SSE path, no
model calls) only when *no* Claude login is detected at all.

To point at different auth (e.g. a remote/headless box with no machine login, or to
force an API key):

```bash
# subscription (default, cheaper): only needed headless — run `claude setup-token`
ALETHEIA_CLAUDE_AUTH_MODE=subscription
CLAUDE_CODE_OAUTH_TOKEN=...            # leave BLANK to inherit the machine login
# or API key:
# ALETHEIA_CLAUDE_AUTH_MODE=api_key
# ANTHROPIC_API_KEY=...
```

## Verify

```bash
conda run -n aletheia python -m pytest        # Phase 0 skeleton tests (needs Postgres up)
```

## Status

The full deterministic research lifecycle runs end-to-end in **dry-run** (DB → event-bus
→ SSE, no model calls) and is exercised by the test suite. Built out through Phases G–P:
fail-closed guardrails, an evidence/claims ledger, structured literature + SOTA tables,
hypothesis scorecards, a reproduction pass, an EIG-ranked experiment planner, and the first
AI-application domain (RAG: lexical & dense retrieval, host-side LLM generation,
cross-vendor faithfulness) alongside the materials and molecules regression domains.

**Honest caveat:** this is breadth of *machinery*, not yet depth of *result*. Every domain
has so far been exercised on small / synthetic data; a substantive real run (real models,
real dataset, confronting real failure) is the next milestone — not more breadth. See
`docs/PROJECT_REVIEW.md` and `docs/AUTONOMOUS_RESEARCH_ROADMAP.md`.
