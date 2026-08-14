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
5. **Strong novelty/SOTA grounding — F8 engineering built; real scientific validation pending.**
   The isolated F8 contract versions corpus/time, exact source spans, replayable searches, hard
   coverage, atomic claims, ranked prior art, bounded novelty, and protocol-safe SOTA comparison.
   F8-S1 persists license-explicit corpus/provider evidence as immutable ordered rows; F8-S2 adds
   deterministic metadata search/replay and mechanical citation traversal; F8-S3 adds
   authorization-aware exact-span claim extraction, independent low-confidence review, and
   contradiction-preserving graph closure; F8-S4 adds four-channel complete-union recall,
   no-delete reranking, strict six-way prior-art relations/component differences, and independent
   blocking/low-confidence review. F8-S5 adds evaluator-owned validation/later temporal splits,
   signed complete trial ledgers, one-sided confidence-bound gates, artifact-derived live coverage,
   author-excluded domain/librarian review, claim ceilings, and an optional exact-claim discovery
   gate. F8-S6 adds independently pre-sealed reference registries, exact protocol matrices, signed
   paired result receipts, exact sign tests with Holm correction and practical margins, immutable
   SOTA verdicts, and an optional no-fallback write-up gate. The included calibration and SOTA
   campaign are synthetic: live adapters, real expert-labelled calibration, private holdout custody,
   prospective false-novelty results, and reproduction of a real complete reference matrix are still
   required before the system can reliably recognize or claim frontier science.
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
four-rule suite and the complete 29-test Docker matrix are frozen and passing. F7 now also has a
pre-registered four-arm evaluation path for direct model, generic agent, Aletheia without K2, and
full K2. It enforces paired seeds, same-model identity, disclosed resource/tool mismatches,
all-attempt ledger reconciliation, signed-score verification, no-best-of-N accounting, paired
confidence intervals, Holm correction, and cost/failure decomposition; see
[`docs/benchmarks/BASELINE_MATRIX.md`](docs/benchmarks/BASELINE_MATRIX.md). Private prospective
suites now have role-separated encrypted envelopes, bounded two-person authorization, a
concurrency-safe one-time unlock, exact runner guards, contamination-triggered retirement, verified
plaintext cleanup, and an operator CLI; see
[`docs/benchmarks/PRIVATE_SUITE_CUSTODY.md`](docs/benchmarks/PRIVATE_SUITE_CUSTODY.md). These are
now joined by a validation-calibrated Frontier Gate contract: independently reviewed reference
evidence and raw validation receipts derive immutable thresholds; the final reporter re-verifies
all test ledgers and signed scorer receipts, distinguishes measured `FAIL` from missing-evidence
`BLOCKED`, audits private cleanup/retirement, and emits content-addressed JSON, Markdown, and SVG.
See [`docs/benchmarks/FRONTIER_GATE_REPORT.md`](docs/benchmarks/FRONTIER_GATE_REPORT.md). This
completes F7's planned evaluation/report engineering slice, but not its scientific exit. The
repository still lacks operator-frozen production configurations, commissioned private tasks,
authenticated approvals, and real four-track runs; therefore it contains no legitimate Frontier
Gate pass. Development proceeds through the isolated F8 knowledge-boundary slices while real F7
execution remains a release gate.

F8 issue 12 now freezes the knowledge-boundary interface before retrieval integration. A synthetic
temporal-holdout fixture and 13 adversarial tests reject future-paper leakage, invented full text,
literature prompt-authority fields, missing source-span evidence, outage-as-novelty, self-review,
evidence-package tampering, and SOTA claims across different split bytes. The schema package is
deliberately disconnected from the research driver and database, so this is an engineering
contract—not evidence that Aletheia can yet determine novelty. See
[`docs/benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md`](docs/benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md) and
[`docs/adr/0009-f8-knowledge-boundary-schema-spike.md`](docs/adr/0009-f8-knowledge-boundary-schema-spike.md).

F8-S1 adds the first durable implementation under that interface. Article-level grants separate
metadata/abstract/full-text capability from automation, model-input, retention, and redistribution
rights; normalized source, paper, span, update, grant, receipt, corpus, and ordered membership rows
are content-addressed, reject SQL mutation, and re-hash on every read. The offline CLI validates,
persists, or audits typed JSON without network retrieval or raw paper text. This store remains
disconnected from SURVEY and cannot confer novelty. See
[`docs/knowledge/CORPUS_PERSISTENCE.md`](docs/knowledge/CORPUS_PERSISTENCE.md).

F8-S2 freezes deterministic multi-source query axes and adapter/parser identities, archives only
policy-checked structured metadata responses, records every page/failure, replays exact parsers,
expands all new forward/backward citations mechanically, and derives four fail-closed coverage
signals. The fixtures are synthetic and no live provider is configured. See
[`docs/knowledge/SEARCH_REPLAY_AND_CITATION.md`](docs/knowledge/SEARCH_REPLAY_AND_CITATION.md).

F8-S3 now validates exact licensed document/span identities ephemerally, separates
`span_extraction` from model `model_input` rights, accepts only strict atomic scientific fields,
routes OCR/low-confidence candidates to independent accept/revise/reject review, preserves
supporting/refuting/qualifying edges, and commits the execution, resolution, and exact graph view to
write-once ledgers. Source bytes are not persisted. This is still a synthetic engineering harness,
not measured real-literature accuracy or a novelty capability. See
[`docs/knowledge/CLAIM_EXTRACTION_AND_REVIEW.md`](docs/knowledge/CLAIM_EXTRACTION_AND_REVIEW.md).

F8-S4 now runs separately frozen lexical, embedding, citation, and structured-entity recall,
rederives and retains their complete candidate union, requires a reranker to score every item while
the harness owns ordering/selection, and emits strict equivalent/subsumes/special-case/extension/
combination/contradiction relations with exact component differences and source-span identities.
Blocking or weak matches require independent accept/revise/reject review, and execution/resolution
remain write-once. This is synthetic-tested matching machinery, not calibrated novelty evidence.
See
[`docs/knowledge/PRIOR_ART_MATCHING_AND_REVIEW.md`](docs/knowledge/PRIOR_ART_MATCHING_AND_REVIEW.md).

F8-S5 now freezes evaluator-owned known-answer and strictly later temporal splits, signs one result
for every base/perturbed trial, and applies one-sided confidence bounds to recall, ranking,
classification, false/missed strong novelty, and stability. Live coverage is derived from the exact
F8-S1–S4/correction artifacts rather than caller-supplied numbers. Candidate authors are excluded
from role-distinct domain-expert/research-librarian review; classification, claim ceiling, and the
research-direction authorization bit are mechanical. Discovery can consume an exact gate through
an optional candidate-claim-bound callback. The 80-case fixture is synthetic and no production
calibration pass is claimed. See
[`docs/knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md`](docs/knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md).

F8-S6 now seals an author-excluded reference registry before candidate evaluation, requires signed
scores on identical paired partitions, compares exact dataset/split/metric/statistics/resource
protocols, and permits a SOTA headline only when every required reference passes both a
Holm-corrected paired sign test and a preregistered practical-improvement margin. Campaign verdicts
and manuscript claim status are mechanically re-derived; the optional scheduler consumer exact-binds
protocol, metric, and score and never falls back after an audited-path error. The fixture is
synthetic and establishes no real SOTA. See
[`docs/knowledge/PROTOCOL_SAFE_SOTA_EVALUATION.md`](docs/knowledge/PROTOCOL_SAFE_SOTA_EVALUATION.md).

F9-S1 now adds an append-only competitive-world-model substrate alongside K2. Stable question,
hypothesis, assumption, prediction, and belief lineages are separated from immutable content-hash
versions. A complete snapshot requires H0, one primary mechanism, at least one alternative,
assumptions and discriminating predictions for every hypothesis, and one normalized belief vector
over the exact versions. PostgreSQL rejects mutation, while a labelled read-only compatibility view
keeps the existing K2 Beta service usable without fabricating F9 alternatives. This remains schema
and persistence engineering—not evidence of causal discovery or calibrated multi-model beliefs.
See [`docs/epistemics/WORLD_MODEL_VERSIONING.md`](docs/epistemics/WORLD_MODEL_VERSIONING.md).

F9-S2 now admits competing hypotheses only from an exact experiment-authorized F8 direction. An
unprivileged generator retains every valid raw draft; a separately manifested reviewer must judge
the complete candidate-pair ledger. The harness blocks uncertain, cross-role, non-transitive, or
exact-normalizer-inconsistent duplicate decisions, requires alternatives to cite accepted linked
prior art, and proves that every kept pair disagrees bidirectionally in one shared observable/
protocol/outcome space. Only then does it derive a uniform-prior F9-S1 snapshot; failures retain
hashes rather than untrusted text, campaigns are content-addressed, and only ready campaigns can be
persisted. The current adapters and scientific content are synthetic, so this is not evidence of
real hypothesis quality, causal identification, or calibrated belief. See
[`docs/epistemics/F8_GROUNDED_HYPOTHESIS_GENERATION.md`](docs/epistemics/F8_GROUNDED_HYPOTHESIS_GENERATION.md).

F9-S3 now turns a ready hypothesis campaign into an explicit causal artifact: a shared typed variable
registry, one exact graph per hypothesis, latent common causes, measurement and selection processes,
one estimand/adjustment set, and scoped identification assumptions. The harness detects cycles,
undefined references, invalid descendants/bad controls, H0/mechanism path contradictions, and
endpoint/protocol rebinding; its tested mathematical surface is deliberately limited to the
back-door criterion and retains open-path witnesses. A distinct reviewer must cover every frozen
assumption, while unresolved judgments or selection-recovery gaps cap future claims at association
and rejected assumptions block prediction planning. `ready_identified` still means only graphically
identified under reviewed assumptions—not observed or causally confirmed. All fixtures remain
synthetic. See
[`docs/epistemics/CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md`](docs/epistemics/CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md).

F9-S4 now freezes the experiment namespace, causal/measurement boundary, outcome bins, analysis/
exclusion/stopping/parser identities, and one prediction for every active hypothesis before target
observation. Probability mode requires independently evaluated historical calibration, a likelihood
derived from the calibrated frozen family, complete normalized mass, pairwise separation, entropy,
and measurement-error sensitivity; ordinal mode remains available but cannot claim probability or
EIG eligibility. The private staging store reloads an immutable ready receipt before writing raw
bytes, requires observation time after commitment, and seals the experiment namespace on first
observation. Exact retries are allowed; a scientifically changed commitment is rejected before raw
write and persisted as a security/scientific-integrity violation. Fixtures remain synthetic, so
`ready` is not evidence that an experiment, prediction, or causal claim is correct. See
[`docs/epistemics/PREOBSERVATION_PREDICTION_COMMITMENT.md`](docs/epistemics/PREOBSERVATION_PREDICTION_COMMITMENT.md).

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

The isolated F8 knowledge boundary now has immutable corpus/access persistence, deterministic
metadata search/replay and citation traversal, plus licensed exact-span atomic-claim extraction with
independent low-confidence review and contradiction-preserving graph closure, plus auditable
four-channel prior-art matching, complete-union reranking, component differences, and independent
relation review. It now also has synthetic-tested calibrated novelty acceptance, artifact-derived
coverage, role-distinct review, claim ceilings, and an optional exact-claim discovery callback.
WRITE_UP can explicitly consume an exact F8-S6 campaign with no fallback, but the default scheduler
and scorecard still do not automatically materialize or consume the full F8 artifact chain.
Live-provider/extractor/matcher calibration plus a real prospective suite and reference matrix
remain release gates. See `docs/F8_S5_CALIBRATED_NOVELTY_IMPLEMENTATION_REPORT_2026_08_15.md` and
`docs/F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md`.

The isolated F9 chain now reaches pre-observation prediction commitment: competing hypotheses,
explicit causal graphs/assumptions, calibrated-or-ordinal likelihood semantics, immutable receipts,
and a sealed raw-observation entry boundary are synthetic-tested end to end. It still lacks an
observation-blind constrained experiment selector, validated-observation-to-posterior update path,
negative-result revision policy, K3 acceptance scorer, and real-domain calibration/replication.

**Honest caveat:** this is still stronger evidence for the *machinery* than for a scientific result.
The first strict live campaign completed, but its external evaluation was negative and the external
literature extraction has disclosed formula overlap with the primary dataset. Repeated campaigns,
stronger novelty grounding, and lower-overlap or laboratory replication are the next milestones.
See `docs/PROJECT_REVIEW.md`, `docs/AUTONOMOUS_RESEARCH_ROADMAP.md`, and the current gap analysis in
`docs/FINAL_GOAL_GAP_ANALYSIS_2026_08_12.md`.
