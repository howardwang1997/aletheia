# aletheia

Aletheia is a research automation system under development. Its target is end-to-end,
literature-grounded research with independently validated observations, reproducible experiments,
and reports derived from an evidence ledger.

The backend uses Python, FastAPI and PostgreSQL; the dashboard uses Next.js. Model runtimes
propose work through constrained interfaces. The Research Kernel owns scientific state, and
execution, validation, admission and qualification use separate authorities.

## Current status

**The deployed system has produced ARL-1 qualification receipts, most recently on 2026-09-14** —
generation 20260914v closed every exit stage on the merged freeze, and its per-source-class
tamper-rejection audit completed all 87 cases, the first full matrix in any generation. Each
case passed with an unchanged-copy control, a rejected one-byte mutation and a fresh recovery,
and the retained sources stayed unchanged. The verification window was sized at composition
time from the measured matrix budget and the authority-pin deadlines (the deployment contract
allows spans up to 24 hours); a pre-matrix budget check cleared the run before it started, and
the matrix closed in 9 h 19 min inside the composed window with no mid-matrix enforcement
trip. The deployment reboot-recovery drill passed on 2026-09-15 across three reboots,
including the mandatory negative path. ARL-1 qualifies bounded protocol execution; it does not
establish scientific validity, independent replication or autonomous research design.

The bounded ARL-2 campaign integration is built: the question loop's control plane, external
bridge and dispatch executor, and acceptance chain are merged on main. The immediate work is
completing its qualification dry run, which has not yet finished end-to-end. F9 world-model
components feed that loop through typed contracts and the F11 durable task queue carries its
execution; F8 knowledge and the F10 capability registry do not enter it yet. None of this
establishes a qualified autonomous scientist.

- [Current roadmap](docs/LONG_TERM_ROADMAP_TO_ARL4_2026_09_06.md)
- [ARL-1 qualification contract](docs/ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md)
- [Deployment qualification procedure](docs/PR8H_QUALIFICATION_TARGET_CAMPAIGN.md)
- [Evidence and publication boundary](docs/PUBLICATION_BOUNDARY.md)

## Invariants

1. Models propose; independent authorities validate and commit observations.
2. Protocols, predictions, evaluator identities and model versions are frozen before evaluation.
3. Every scientific claim requires evidence from its current valid protocol and retains its
   development, post-hoc or confirmatory classification.
4. Attempts, negative observations and inconclusive measurements remain in the scientific ledger.
5. Execution is bounded by explicit budgets, permissions and sandbox policies.
6. Recovery preserves committed identities and cannot create a duplicate scientific observation.

The quickstart below exposes the development and compatibility interfaces. Production Kernel
qualification requires the separately commissioned Linux deployment described in the linked guides.

## Development quickstart

Prereqs: `conda`, Docker runtime (Docker Desktop or `colima start`), Node 22 (the CI version).

```bash
# 1. Python env (conda is required)
conda env create -f environment.yml
conda run -n aletheia pip install -e .

# 2. Infra: Postgres + pgvector
docker compose up -d            # needs a running docker daemon (e.g. `colima start`)

# 3. Config
cp .env.example .env            # dry-run works with no secrets

# 4. Backend control plane
conda run -n aletheia uvicorn aletheia.api.main:app --reload --port 8000

# 5. Durable research worker (separate shell; replace the example with the retained
#    deployment worker-manifest SHA-256)
conda run -n aletheia python scripts/durable_worker.py \
  --worker-id research-worker-01 \
  --worker-manifest-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --handler research.experiment_driver.v1=aletheia.scheduler.durable:run_driver_task

# 6. Dashboard (separate shell)
cd frontend && npm install && npm run dev    # http://localhost:3000
```

Open http://localhost:3000 and log in (the owner credentials seed from `ALETHEIA_OWNER_EMAIL` /
`ALETHEIA_OWNER_PASSWORD` in `.env`). Type in the conversation input until the agent finalizes
a plan, connect and ready the data in the data panel, then click **Launch experiment** — the
button enables only once the plan is finalized, every declared dataset (uploaded or connected)
is ready, the run is not already launched, and you hold a control-role account.

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
conda run -n aletheia python -m pytest        # requires a disposable test database
```

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
