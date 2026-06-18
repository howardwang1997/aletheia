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
3. **Exploratory -> confirmatory demonstrations (K1) — built, hardening.** The AI calibrates a
   threshold on an exploration partition, commits a pre-registration, then is judged ONLY on a
   disjoint confirmation partition; a deterministic seal rejects doom-to-zero / control-not-silent /
   trivially-easy thresholds. A real holding result is reachable without weakening the spine.
4. **Campaign learning loop (K2) — built, hardening.** A refuted or unstable demonstration's *typed
   outcome reason* feeds the next round's hypothesis/design (no fresh blind guess); a calibrated
   belief credence moves ONLY on a harness confirm-split verdict; acceptance scores one final verdict
   per round. This is the transition from one-shot automation to a research *program*.
5. **Strong novelty/SOTA grounding — partial, improving.** Literature retrieval, structured findings,
   SOTA rows, and a cross-vendor *direction gate* (which rejects "repackaged applicability-domain"-type
   claims) exist; a staged probe pipeline now triangulates *novel-and-holds* candidates cheaply before
   a live run. Still needs stronger automated prior-work mapping to tell new science from a rephrased
   known idea — and discovery is still human-screened, not autonomous (see Status).
6. **Reproducible research bundle — partial.** The final output should be a stable bundle containing
   the question, literature, data card, split metadata, code, artifacts, metrics, claims, audits,
   reproduction, limitations, paper, and reproducibility package/PR.

Roughly: keystones 1-4 are substantially built (the evidence spine, the AI-authored demonstration
harness, the explore->confirm seal, and the campaign learning loop, all offline-green); 5-6 are
partial. The owed milestone is a **live end-to-end FULL run** that lands a harness-verified,
cross-vendor-audited, belief-moving result on a real dataset — a cuprate-Tc diagnostic is wired and
verified offline, awaiting its live run.

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

The deterministic lifecycle runs end-to-end (dry-run + real), exercised by the test suite
(**~420 tests, offline-green**). On top of Phases G–P (fail-closed guardrails, evidence/claims
ledger, literature + SOTA tables, scorecards, reproduction, EIG-ranked planner, the RAG / materials
/ molecules domains), the **anti-fakeability spine** is built and hardened: the AI authors a
sandboxed `compute_demonstration`, but the harness owns `holds` via pre-registration + negative
control + leakage probes + an independent **cross-vendor audit** (author vendor excluded); the
**explore→confirm seal (K1)** and the **campaign learning loop (K2)** are in. Real runs have
happened (live Opus + real cross-vendor critics + real training on matbench / ESOL / UCI
superconductivity); a verified, novel-*and*-holds diagnostic (a cuprate-Tc model blind spot) is
wired for a live K2 campaign.

**Honest caveat:** a clean **live FULL** — a harness-verified, reproduced, cross-vendor-audited,
belief-moving result in a single multi-round run — has **not yet landed** (it is the owed next
milestone; runs go in a separate terminal, see `docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md`).
And the cuprate effect was **pre-screened** by the probe pipeline, so a successful FULL would
demonstrate a *guarded, audited research loop on a real dataset* — **not yet autonomous discovery**.
The next maturity gap is to move from "run a pre-screened verified candidate" to "autonomously
discover, justify, test, and refine candidates." See `docs/PROJECT_REVIEW.md`,
`docs/AUTONOMOUS_RESEARCH_ROADMAP.md`, and `docs/K2_CUPRATE_CAMPAIGN_PLAN_2026_06_16.md`.
