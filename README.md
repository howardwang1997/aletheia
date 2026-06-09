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
3. **Exploratory -> confirmatory demonstrations — next keystone.** The AI should calibrate on an
   exploration partition, commit a pre-registered threshold, then be judged only on a disjoint
   confirmation partition. This makes a real holding result reachable without weakening the
   anti-fakeability spine.
4. **Campaign learning loop — missing.** A refuted or unstable demonstration should feed back into
   the next hypothesis/design instead of starting from a fresh blind guess. This is the transition
   from one-shot automation to a research program.
5. **Strong novelty/SOTA grounding — partial.** Literature retrieval, structured findings, and SOTA
   rows exist, but novelty and SOTA claims still need stronger health checks and prior-work mapping
   before the system can reliably tell new science from a rephrased known idea.
6. **Reproducible research bundle — partial.** The final output should be a stable bundle containing
   the question, literature, data card, split metadata, code, artifacts, metrics, claims, audits,
   reproduction, limitations, paper, and reproducibility package/PR.

Roughly: keystones 1-2 are substantially built, keystone 3 is the next implementation target, and
keystones 4-6 are the remaining large steps before Aletheia can credibly claim autonomous frontier
science rather than a well-guarded research execution system.

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

## Token / cost usage

Every Claude SDK call persists its real `total_cost_usd` + token `usage`; these are summed
per run from the ledger (no estimate). A live run is dominated by `cache_read` tokens — that,
not USD, is what meters the rolling subscription window (`cost_usd` reads ~0 under a
subscription login).

```bash
conda run -n aletheia python scripts/usage_report.py             # all runs + grand total
conda run -n aletheia python scripts/usage_report.py --top 10    # priciest runs by tokens
conda run -n aletheia python scripts/usage_report.py <run_id>    # full breakdown for one run
```

Each e2e summary also carries a `usage` block (incl. `five_hour_window`: the SDK-reported
pressure on the 5-hour rolling limit during the run — peak utilization + whether it was
throttled), and `token_cap_per_run` (off by default) bounds a run's total tokens. The budget
cap now binds on the SDK's real reported cost, not a flat per-stage estimate.

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
