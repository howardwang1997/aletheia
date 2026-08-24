# aletheia

Autonomous researcher — a personal, lights-out AI Scientist. All you need is deploy and make dataset.

Aletheia plans research directions, designs & runs experiments, analyzes & optimizes,
and writes up results — autonomously, within budget guardrails. A configurable **Claude or GPT**
orchestrator coordinates isolated worker contexts through the Claude Agent SDK or OpenAI Responses
API, alongside a **cross-vendor critic panel**
that reviews designs/results from supportive and adversarial angles. A Postgres
**experiment ledger** is the source-of-truth work log; a **Next.js + FastAPI** dashboard
streams live activity and lets you steer the lab. The current control-plane target and executable
PR sequence are in
[`docs/END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md`](docs/END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).

## Ultimate goal

**用AI做最前沿的科学研究 — have AI conduct frontier scientific research, end to end.**
Not AutoML on a benchmark: the north star is a system that *poses novel, literature-grounded
questions*, designs and runs experiments with frontier methods, reasons about what it learned,
and writes up cited results — autonomously, within guardrails. Every change is weighed by whether
it moves Aletheia toward that. See the roadmap in the approved plan and `docs/ARCHITECTURE.md`.
The latter is retained as a legacy architecture record; the dated end-to-end plan linked above is
the authoritative migration roadmap.

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
6. **Durable execution and reproducible research bundle — partial.** F11-S1 now provides a
   Postgres-backed task/attempt/dependency ledger, leases, heartbeats, finite retry, process-kill
   recovery, content-bound idempotency, independent workers, and resumable database-cursor SSE.
   F11-S2 adds transactionally exact scientific commands: domain state, immutable result receipt,
   and keyed event commit together, while prediction, observation validation, and belief update stay
   separate. Final holdout/external actions now have one raw authorization token, stable provider
   key, immutable receipt, and fail-closed reconciliation after an unknown outcome.
   F11-S3 adds a reconstructible Quest/Program/Campaign graph with cross-Campaign family identity
   and frozen allocations. F11-S4 adds immutable task-bound memory facts, receipt-backed
   non-destructive compaction, exact preservation of negative/contradictory state, deterministic
   recovery, and provider-neutral task context delivery. F11-S5 adds observation-blind portfolio
   proposals, independent frozen assessments, harness-computed hard filters/EIG/cost/replication/
   diversity, constrained shadow batches, and a human plan committed before planner revelation.
   Every epoch is receipt-backed with `actions_enqueued=false`; the audit cannot enable autonomous
   allocation. F11-S6 adds deterministic ten-boundary fault campaigns, independent zero-loss/
   zero-duplicate regrading, a frozen-environment production harness shared by tests and CLI,
   reconstructible diagnostic bundles, real process/transaction/lease/archive/identity/
   outward-action recovery, and append-only reports whose audit still cannot activate allocation.
   F11-S7 adds a database-clock, append-only endurance gate with immutable starts, resumable
   parent-hashed checkpoints, typed reproduction/interruption/pivot evidence, final portfolio and
   efficiency reports, and an unforgeable split between accelerated engineering evidence and a
   real 72-hour pass. The accelerated acceptance run passes. The first real 72-hour Quest has now
   completed, but its immutable report is `blocked` because structural pivots reached `0/1`; it is
   not a real 72-hour pass, is ineligible for scientific-exit review, and allocation remains disabled.
   The first production-shaped materials Quest is now
   commissioned from the real F10 phonon artifacts with two closed competing world models, three
   Campaigns, exploration-only source data, five hard budget dimensions, and exact replay/audit;
   its independent-implementation branch is active. Its Quest-scoped production fault prerequisite
   has passed all ten boundaries with every core loss/duplicate/mismatch count at zero. A
   committed-code, advisory-locked, write-once-spooled run-once endurance controller is implemented
   and tested; the historical v1 gate and controller identities are frozen, and that gate finalized
   without satisfying its precommitted scientific floor. A committed in-window F11-S6 report can now
   be deterministically converted into typed process/provider interruption receipts without manual
   evidence JSON. A frozen launchd adapter bound the exact Conda/Python runtime, controller,
   deployment files, five-minute cadence, and logs; it had no automatic start/finalize path. A
   zero-fit same-source reproduction protocol now adds an
   independently coded feature path, a distinct ExtraTrees estimator, target/split/matrix hash
   parity, exact negative/inconclusive outcome retention, and typed endurance evidence submission.
   The frozen v1 report records one reproduction, one negative result, and operational recovery,
   but it does not establish independent F12 replication or a qualifying structural pivot.
   The final output still needs a stable bundle containing the question, literature, data card,
   split metadata, code, artifacts, metrics, claims, audits, reproduction, limitations, paper, and
   reproducibility package/PR; a qualifying scientific-exit gate and F12 remain.

The new-Quest control-plane migration has completed the local source/test slices for PR-0 through
PR-5, including the PR-4a foundation and PR-4b qualification-only local-execution composition. A deployment-pinned,
root-certified Ed25519 policy now gates full signed commands into an append-only event/CAS store;
deterministic replay, full audit, crash-safe idempotency, a Quest-wide emergency halt, and an
immutable cross-store namespace prevent the legacy Program graph and research kernel from owning
the same Quest. The authoritative API is under `/research-kernel/...`; compatibility mutations are
explicitly deprecated under `/legacy/research-graph/...` and never dual-write. This is a durable
scientific authority substrate plus a pure, domain-independent scientific Protocol IR/compiler and
a fenced engineering-qualification substrate plus a restart-safe scientific-controller
vertical—not a target-host-qualified production service or a reliable autonomous scientist. PR-3 can reject or canonically
compile frozen protocols against atomic capability manifests and static resource classes. Work-order nodes now
freeze logical command, input/output, artifact, and replicate kind/count/seed/site identities;
PR-4a reruns the pure compilation/intent verifier before reservation and the confirmed-failure retry
verifier before another infrastructure attempt. It adds deployment-signed engineering grants,
enrolled node/inventory contracts, PostgreSQL resource/budget fencing, quarantine/CAS rehash, typed
terminal receipts, and the original node fault harness. PR-4b adds signed read-only quote/source-
budget registries, sealed X25519/AEAD assignment delivery, a PostgreSQL allocator-to-node
adapter/worker, exact input materialization, a local CPU-only OCI runtime and launch gate,
loop-backed output quota, an independent systemd deadline watchdog, and runtime-v2 termination/
artifact settlement. Exact reexecution requires at least two preregistered slots on every
scientific-executor branch; it does not prove independent
implementation or site. PR-3 still cannot inspect live capacity, validate input receipt bytes or
custody, reserve hardware or budget, authorize or run work, or admit evidence by itself. PR-4b is
still permanently `qualification_only=true` / `scientific_admission_allowed=false`, rejects device/
GPU launch, and has no HTTP or Research Kernel launch authorization. Its deployment-evidence
closure now provides a portable closed desired-state contract, deterministic systemd/PostgreSQL
rendering, externally pinned signed Linux observations, a derived installed-manifest schema, and
read-only revalidation. It deliberately does not install or repair a host, implement the concrete
observer, or run the campaign. The exact Linux/rootful-Docker/systemd/loop/ext4/cgroup-v2/shared-
mount campaign must pass before any host is called deployable. No target-host manifest instance has
been frozen, that exact campaign has not run, and PR-4b is therefore nondeployable. PR-5 now adds
the signed action-to-execution bridge, DB-time independent validation/admission, atomic
`observation_incorporated` Kernel transition, `research.controller.v1` durable wakeups and
lease/restart recovery, a pinned `/quests/{id}/launch` path, and graph-scoped typed continuation.
Its local synthetic vertical covers a measurement blocker, typed refinement and recompilation,
valid negative/inconclusive evidence, a signed hypothesis fork, selected-child activation, and a
discriminating follow-up that is compiled, executed, admitted into a second distinct scientific
slot, and incorporated without the legacy `ExperimentDriver`. It is engineering evidence, not a
scientific claim or deployment proof. The PR-2 store also still has one immutable policy epoch and
`O(N²)` lifecycle audits. See [ADR 0046](docs/adr/0046-root-certified-research-command-event-store.md),
the [PR-2 operator guide](docs/migration/PR2_RESEARCH_EVENT_STORE.md), and the
[PR-3 compiler guide](docs/PR3_PROTOCOL_COMPILER.md), plus the
[PR-4a foundation guide](docs/PR4_LOCAL_EXECUTION_FOUNDATION.md) and
[PR-4b composition guide](docs/PR4B_LOCAL_EXECUTION_COMPOSITION.md), and
[PR-5 controller guide](docs/PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).

The remaining load-bearing work is knowledge-grounded novelty/SOTA validation, a richer repertoire
of causal/mechanistic experiments, production provider receipt/reconciliation commissioning,
production portfolio activation after a qualifying gate, repeated lower-overlap or laboratory
replication, and a complete evidence bundle. The completed v1 72-hour gate remains blocked and is
not retroactively repaired by these next steps.

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

F9-S5 now compares at least two physically reloaded F9-S4 commitments under one exact F9-S1 prior.
It derives every outcome marginal and hypothetical posterior, expected entropy reduction, and
pairwise likelihood separation, then applies non-compensable cost, time, risk, measurement-validity,
proxy, capability, information, and fresh-confirmation gates. Only feasible candidates receive a
policy-fixed utility combining information, replication debt, resources, and risk; rankings retain
every winner, loser, blocker, and reason, while an all-infeasible set explicitly selects nothing. The
independent assessor and request have no observation/tool access, and archive replay rederives the
decision. Fixtures and assessments remain synthetic, so selection is not evidence of real
feasibility, validity, safety, or mechanism truth. See
[`docs/epistemics/CONSTRAINED_EXPERIMENT_SELECTION.md`](docs/epistemics/CONSTRAINED_EXPERIMENT_SELECTION.md).

F9-S6 now separates raw-observation validation from belief update. An independently manifested,
no-tool validator is the only role that receives exact staged bytes; it physically verifies the
selection, selected prediction, observation receipt, fresh-confirmation reservation, and frozen
analysis/measurement identities before applying confirmation, identity, custody, validity,
blinding, protocol, audit, and sample hard gates. The updater can consume only a committed validated
artifact, reloads it from archive, derives the exact nominal and every frozen likelihood-sensitivity
posterior, and creates a child belief state without mutating the source snapshot. Negative and
surprising evidence produces append-only retain/retire/narrow and world-level fork/new-measurement
directives plus an open contradiction queue. Fixtures and validator assertions remain synthetic, so
this is not evidence that any real observation, likelihood, measurement, or mechanism is valid. See
[`docs/epistemics/VALIDATED_OBSERVATION_BELIEF_UPDATE.md`](docs/epistemics/VALIDATED_OBSERVATION_BELIEF_UPDATE.md).

F9-S7 now independently reopens the committed selection, validation, update, and evidence-ledger
archives and rederives eleven K3 checks covering competing hypotheses, chronology, valid-observation/
update bijection, high-belief discrimination, belief lineage, mechanism claims, negative-result
revision, contradictions, persistence, terminal action, and nonvacuous positive update. Missing or
forged evidence fails closed; zero updates can be honest partial but never accepted; and narrowing
must create new testable prediction versions rather than only new wording. The scheduler entry point
delegates to the same no-tool/no-raw-data scorer. An accepted chain is an engineering verdict over
synthetic artifacts, not the F9 hidden-world or real-materials scientific exit. See
[`docs/epistemics/INDEPENDENT_K3_ACCEPTANCE.md`](docs/epistemics/INDEPENDENT_K3_ACCEPTANCE.md).

F9-S8 closes the cross-round state hand-off. A successful update and its exact revision
materializations now produce a closed `WorldModelTransition`; PostgreSQL commits source, posterior,
revised versions, next snapshot, transition row, and one typed event atomically. Independent K3
terminal evidence then authorizes the exact physical child snapshot as the next F9-S3 causal source.
Narrowing rebinds assumptions, predictions, and belief members append-only; retirement, fork, and
stop remain fail-closed. This has been exercised through a genuine second synthetic causal round,
but is not the hidden-world or real-materials scientific exit. See
[`docs/epistemics/TRANSACTIONAL_WORLD_MODEL_CONTINUATION.md`](docs/epistemics/TRANSACTIONAL_WORLD_MODEL_CONTINUATION.md).

F9-S9 turns the hidden-world scientific exit into an executable preregistration. A separate matched
headline/K2-single/K3-competing matrix now derives truth-relative elimination, discriminating-trial,
posterior Brier/ECE, false-mechanism, claim-coverage, and contraction endpoints from signed
DiscoveryWorld traces; validation freezes a held-out test config, paired task/repeat bootstrap and
Holm statistics are recomputed from the ledger, and private custody is mandatory for a formal exit.
The checked-in protocol currently reports `blocked`: its synthetic tests validate the machinery,
while the private prospective suite and live same-model runner/receipts are still missing. See
[`docs/benchmarks/K3_HIDDEN_WORLD_SCIENTIFIC_EXIT.md`](docs/benchmarks/K3_HIDDEN_WORLD_SCIENTIFIC_EXIT.md).

F9-S10 adds the real-materials limb. A frozen Matbench band-gap protocol maintains null,
unseen-system-specific, and generic-shrinkage explanations; observation-blind EIG selects a
chemical-system contrast; distinct local keys sign measurement and full physical recomputation; and
only the validated outcome drives nominal plus sensitivity posteriors. The authoritative v2 result
favored generic shrinkage, but worst-case hypothesis-space contraction was only 1.34%, so it is an
honest `valid_update_without_robust_contraction`, not a scientific-exit pass. The public benchmark
and single-operator key custody make it retrospective internal confirmation, not external
replication. See
[`docs/benchmarks/K3_REAL_MATERIALS_EVIDENCE_CHAIN.md`](docs/benchmarks/K3_REAL_MATERIALS_EVIDENCE_CHAIN.md).

F10-S1 now adds an immutable experiment-capability registry and an exact observation-blind planner.
Provisional capabilities are exploratory-only; schema changes require append-only semantic
versioning, and confirmatory requests fail closed. The real materials capability froze and ran all
five registered partitions with two physical validator recomputations per slot. The outcome was
honestly `partition_sensitive` (2 unseen-specific, 2 generic, 1 ambiguous): all point deltas were
positive, but only two intervals excluded zero. Same-dataset partitions were not pooled into a
joint posterior, and the capability remains provisional. See
[`docs/benchmarks/F10_MATERIALS_CAPABILITY_REPLICATION.md`](docs/benchmarks/F10_MATERIALS_CAPABILITY_REPLICATION.md).

F10-S2 now keeps raw executor artifacts, parser candidates, domain-validator reports, and final
measurement admission as separate replayable layers. Values require a quantity kind, UCUM literal,
uncertainty, method, conditions, sample count, and raw lineage. A real Matbench slot reexecution
ended `validated_negative`, while its exact-reexecution purpose correctly prevented it from being
counted again as new F9 evidence. Invalid units/conditions, failed execution, parser/validator
failure, and scientific negatives have distinct terminal states. See
[`docs/capabilities/TYPED_OBSERVATION_PIPELINE.md`](docs/capabilities/TYPED_OBSERVATION_PIPELINE.md).

F10-S3 now separates formula, normalized structure, synthesis batch, physical sample, and source
record identity. Required identity levels are checked independently across splits; failed, duplicate,
condition-incompatible, and conflicting measurements cannot silently enter variability estimates.
Gold fixtures distinguish two NaCl polymorphs and exercise sample leakage plus available/unavailable
noise decomposition. A physical audit of all 4,604 `matbench_expt_gap` rows found three unresolved
normalized-composition collisions, but correctly limits the table to composition benchmarking
because its structure/sample/batch/method/condition/uncertainty/source metadata is absent. See
[`docs/capabilities/MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT.md`](docs/capabilities/MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT.md).

F10-S4 now freezes and physically replays a three-arm structure-discrimination experiment. All
1,265 `matbench_phonons` structures pass an all-row quality gate and are split by 1,082 chemical
systems before fitting. A 159-feature composition-plus-aligned-geometry model is compared with an
equal-capacity within-role-permuted control and a 132-feature composition reference under the same
fixed random-forest budget. Aligned geometry improved MAE over the matched control by 52.2% on
internal validation and 47.8% on locked holdout; both chemical-system cluster intervals excluded
zero and exact replay reproduced the immutable result. This is bounded same-public-dataset DFPT
evidence, not an external replication or mechanism claim. See
[`docs/capabilities/STRUCTURE_AWARE_MATERIALS_EXPERIMENT.md`](docs/capabilities/STRUCTURE_AWARE_MATERIALS_EXPERIMENT.md).

F10-S5 now adds a real digest-pinned simulation lifecycle around ASE 3.29.0/EMT: a strict periodic
EOS job, hardened no-network container, per-evaluation atomic checkpoints, content-addressed raw
artifacts, typed timeout/quota/infrastructure/worker failures, eleven independently recomputed
quality/gold checks, and exact evidence replay. The retained v1 bind-mount failure is superseded
append-only by v2; two successful v2 Cu fcc attempts exactly match the frozen payload and the ASE
reference lattice constant. The registry exposes this only as an explicitly opted-in provisional
capability. It calibrates a classical potential execution boundary, not DFT, experiment,
transferability, mechanism, or independent implementation replication. See
[`docs/capabilities/ASE_EMT_REFERENCE_SIMULATION.md`](docs/capabilities/ASE_EMT_REFERENCE_SIMULATION.md).

F10-S6 now closes an exact F8→F9→F10 mechanistic campaign before observation access. A protocol
binds a frozen capability registry, independently reviewed family qualifications, at least two
distinct experiment families including C3/C4,
unique probabilistic prediction campaigns, executor/data/input identities, resource ceilings, a
fresh independent-confirmation reservation, and a robust sensitivity-aware decision rule. Results
must arrive through committed typed observation pipelines and an independent pre-frozen outcome
mapper. Slots are scored separately and only concordance is used—no joint posterior or
pseudoreplication. Claim release is intersected with F9 causal authority, registered capability
claim types, confirmatory admission, and fresh/independent evidence. The engineering template and
13 synthetic adversarial tests are complete, but the frozen current audit reports both execution
and scientific release blocked because no production F8/F9 lineage, registered confirmatory
mechanism capabilities, independently reviewed family qualifications, fresh reservation, or
independent confirmation exists. See
[`docs/capabilities/MECHANISTIC_CAMPAIGN_TEMPLATE.md`](docs/capabilities/MECHANISTIC_CAMPAIGN_TEMPLATE.md).

F10-S7 now makes capability registration a signed, role-separated supply-chain transition instead
of a lifecycle-field edit. A provisional manifest must bind a hard-sandbox authoring receipt,
separately frozen generated tests, exact reference/adversarial/positive/negative cases, a
non-agent-authored independent validator, domain/safety/claim-scope review, and a complete promotion
audit. Ed25519 policies delegate six distinct permissions with public-key-derived IDs, thresholds,
domain/capability scopes, validity, and revocation; a separate registry promoter can append only the
exact reconstructed successor to the exact source snapshot. Forgery, wrong permission, role reuse,
fixture rebinding, stale-source races, rollback, post-signature mutation, and unsafe private-key
files fail closed. The synthetic full upgrade proves the engineering contract only: the unchanged
materials registry v4 still has zero registered capabilities and both latest candidates remain
blocked on real independent validators/reviewers, production trust policy, signed audit, and
authorized update. See
[`docs/capabilities/CAPABILITY_AUTHORING_AND_PROMOTION.md`](docs/capabilities/CAPABILITY_AUTHORING_AND_PROMOTION.md).

F11-S1 moves long-running execution out of the API process. Postgres owns immutable task
request identities, dependency edges, attempt history, current leases, heartbeat/retry state, and
recovery audits; `FOR UPDATE SKIP LOCKED` gives concurrent workers one active owner per attempt,
while exact stale callbacks cannot change a replacement attempt. Launch/resume returns a durable
task ID, and a separately deployed worker runs the existing experiment driver. Each task transition
and its content-bound event commit in one transaction. SSE tails the database by event ID and
supports `Last-Event-ID`, so API restarts or multiple API processes no longer define event truth.
An actual killed child process, replacement recovery, concurrency, duplicate delivery, dependency,
and event-rollback cases are tested. See
[`docs/jobs/DURABLE_TASK_ORCHESTRATION.md`](docs/jobs/DURABLE_TASK_ORCHESTRATION.md).

F11-S2 now places prediction, observation validation, belief update, stage decision, artifact batch,
and accepted world-model transitions behind content-bound PostgreSQL command receipts. The domain
rows, command result, and keyed durable event either all commit or all roll back; exact worker
redelivery returns the first receipt without applying another update. One-time final-holdout and
external-validation claims expose a raw token only once, persist only its SHA-256, pass a stable
provider idempotency key, and atomically bind the result to an immutable external-action receipt.
An expired claim becomes `reconciliation_required` and is never automatically reissued. This is an
at-most-one Aletheia authorization guarantee, not cross-system exactly-once execution. Production
portfolio activation and a qualifying scientific-exit gate remain; the first actual 72-hour run
completed with a blocked disposition.
See [`docs/jobs/TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md`](docs/jobs/TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md).

F11-S3 gives each long-horizon effort a reconstructible Quest → Program → Campaign spine, durable
scientific-family identity, dependency DAG, and frozen data/budget allocations. F11-S4 builds a
separate authoritative memory ledger on that ancestry: typed facts and task bindings are immutable;
compaction artifacts retain complete per-fact coverage receipts; unfavorable evidence and required
state remain exact; and a worker reloads a provider-neutral context receipt before delivery. New
facts make an old projection stale, while missing/corrupt artifacts, context overflow, or a
provider/model mismatch fail closed. This proves recovery custody, not the semantic truth of a
model-written summary or autonomous research value. See
[`docs/programs/RESEARCH_PROGRAM_GRAPH.md`](docs/programs/RESEARCH_PROGRAM_GRAPH.md) and
[`docs/programs/RECEIPT_BACKED_SCIENTIFIC_MEMORY.md`](docs/programs/RECEIPT_BACKED_SCIENTIFIC_MEMORY.md).

F11-S5 adds a cross-Program shadow portfolio layer without granting action authority. The proposer
can only name typed actions and rationales; an independent assessment freezes evidence inputs; an
integer/Decimal harness recomputes lifecycle/dependency/data/capability/measurement/risk/approval/
budget/EIG gates, utility, replication quota, and diversity-constrained batch selection. A human
plan is committed with no planner-output access before the epoch exists. Graph, budget, or memory
change blocks a new epoch, while old epochs replay from frozen state. The readiness audit can only
recommend human activation review and always reports autonomous allocation disabled. See
[`docs/programs/SHADOW_RESEARCH_PORTFOLIO.md`](docs/programs/SHADOW_RESEARCH_PORTFOLIO.md).

F11-S6 freezes ten failure boundaries and six mandatory exact-zero invariants, executes real
process kills, PostgreSQL rollback/reconnect, retry, duplicate/stale delivery, archive exhaustion,
runtime mismatch, and ambiguous outward-action recovery, then independently derives the verdict.
The production CLI freezes code/runtime identity and retains reconstructible diagnostics in a
self-hashed evidence bundle; its tests invoke the identical executor path. Passing, failed, and
blocked reports are append-only and regraded on every read. A passing latest campaign permits only
F11-S7 review and always reports autonomous allocation disabled. See
[`docs/jobs/FAULT_INJECTION_CAMPAIGNS.md`](docs/jobs/FAULT_INJECTION_CAMPAIGNS.md).

F11-S7 now makes the endurance experiment durable without pretending that elapsed time can be unit
tested. One immutable start and a parent-hashed checkpoint chain use PostgreSQL wall-clock time in
`real_time_72h`; explicit clocks exist only in `accelerated_engineering`, whose passing report can
never set the real-72-hour verdict. Checkpoints reconstruct graph, negative-result memory, budgets,
one-time actions, fault sources, and portfolio epochs; typed evidence proves reproduction,
process/provider interruption, and a negative-result-caused structural pivot. Terminal reports
retain pass/blocked/failed state and complete portfolio/efficiency evidence. The supervised
run-once controller adds committed code identity, gate-specific PostgreSQL advisory-lock exclusion, stable
tail-derived checkpoint keys, write-once evidence/receipt spooling, ambiguous-commit recovery, and
no automatic finalization or caller-clock path. The accelerated end-to-end acceptance passes. The
actual v1 72-hour Quest subsequently completed, but its frozen report is blocked by
`structural_pivots:minimum_not_met:0/1`; it cannot be presented as a pass. See
[`docs/programs/RESEARCH_ENDURANCE_GATE.md`](docs/programs/RESEARCH_ENDURANCE_GATE.md).

Production commissioning now turns the real F10 structure-signal artifacts into a replay-safe F11
Quest rather than a test fixture. The applied Quest `qst_cd143727c9e8c48fcff45ab6087db3d2`
contains two immutable research questions with null/primary/alternative hypotheses, three bounded
Campaigns, exact Run/DataAsset identities, exploration-only Matbench source custody, and USD/GPU/
token/wall-clock/experiment caps. First application created 31 objects; exact retry created zero,
and both the commissioning audit and general graph rebuild produced graph SHA-256
`41a47946b28c9685468b5946e6b782c7f9979a8c2e9fada6d201a4b2c34286b8`. Candidate Phonondb,
Alexandria, and Phonix sources remain unallocated pending lineage/target audits; Materials Project
legacy DFPT is explicitly excluded as independent source lineage. See
[`docs/programs/PHONON_QUEST_COMMISSIONING.md`](docs/programs/PHONON_QUEST_COMMISSIONING.md) and the
[`commissioning report`](docs/F11_PRODUCTION_PHONON_QUEST_COMMISSIONING_REPORT_2026_08_18.md).

The first gate-bound scientific producer is an implementation-diverse same-source phonon replay.
It independently rebuilds and hash-checks the frozen composition/geometry matrices, replaces the
F10 RandomForest with one precommitted ExtraTrees fit per matched arm, obtains completion time from
PostgreSQL, and retains confirmed, contradicted, or inconclusive outcomes in scientific memory
before spooling a typed reproduction receipt. The historical v1 endurance report now records the
terminal evidence counts, but remains blocked; this pre-run producer description does not override
that report or establish independent replication. See
[`docs/programs/PHONON_IMPLEMENTATION_REPRODUCTION.md`](docs/programs/PHONON_IMPLEMENTATION_REPRODUCTION.md).

The production portfolio producer now freezes four evidence-bound alternatives before the run,
stages an observation-blind slate before gate start, requires an explicit `human:*` baseline while
planner output is absent, and permits exactly one PostgreSQL-timed shadow epoch only after explicit
start and before any graph transition. The external-corpus candidate remains hard-filtered because
the Quest has no audited `external_validation` data role. The workflow never enqueues actions or
starts/transitions Campaigns. See
[`docs/programs/PHONON_ENDURANCE_PORTFOLIO.md`](docs/programs/PHONON_ENDURANCE_PORTFOLIO.md).

A separate production pivot work order now reacts only to an exact committed `contradicted` replay.
It verifies the byte-identical controller envelope and non-droppable negative memory fact before
durably stopping the source Campaign and activating the external-calculation Campaign for
lineage/target qualification only. Confirmed or inconclusive outcomes cause no graph change; the
workflow allocates no data and authorizes no outward action. See
[`docs/programs/PHONON_NEGATIVE_RESULT_PIVOT.md`](docs/programs/PHONON_NEGATIVE_RESULT_PIVOT.md).

Final efficiency can no longer be supplied as operator-chosen production numbers. After one blind
experimental baseline is frozen, the efficiency adapter reconstructs the in-window shadow epoch and
derives expected question coverage per preregistered duration for the human and planner batches. It
retains below-floor results and labels the metric as expected—not realized—because shadow actions
are never enqueued. See
[`docs/programs/PHONON_PORTFOLIO_EFFICIENCY.md`](docs/programs/PHONON_PORTFOLIO_EFFICIENCY.md).

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

`Next.js dashboard ⇄ FastAPI control plane ⇄ Postgres task/event ledger ⇄ independent workers ⇄
{orchestrator (Claude or GPT), critic gateway, scheduler, compute, IAM}`, with SSE replaying the
durable event cursor.

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

The isolated F9 chain now reaches independent K3 evidence-chain acceptance: competing hypotheses,
explicit causal graphs/assumptions, calibrated-or-ordinal likelihood semantics, immutable prediction
receipts, sealed raw-observation entry, archived-candidate verification, full EIG ledgers, hard
validity/resource/safety gates, reasoned no-fallback selection, independent observation admission,
exact posterior/sensitivity audits, child belief snapshots, append-only negative-result directives,
contradiction retention, physical end-to-end replay, mechanism-claim gates, prediction-changing
negative revision, exact persistence bundles, and nonvacuous verdicts are synthetic-tested end to
end. F9-S8 now atomically persists source/posterior/revision-closed next snapshots with one typed
event, physically reloads that transition, gates it on the independent K3 terminal verdict, and has
verified that the exact child state drives a second F9-S3 causal round; retirement/fork/stop remain
fail-closed. F9-S9 has completed the frozen K3-versus-K2/headline hidden-world evaluation machinery
and truth-relative scorer, but no live/private execution has passed it. F9-S10 has completed one
real Matbench alternatives/experiment/validated-update chain with locally authenticated evidence;
its split-sensitive results did not pass robust contraction. The project still lacks contradiction
resolution, a prospective custody-bound hidden-world result, external validator custody, and
registered real-domain replication/calibration.

**Honest caveat:** this is still stronger evidence for the *machinery* than for a scientific result.
The first strict live campaign completed, but its external evaluation was negative and the external
literature extraction has disclosed formula overlap with the primary dataset. Repeated campaigns,
stronger novelty grounding, and lower-overlap or laboratory replication are the next milestones.
See `docs/PROJECT_REVIEW.md`, `docs/AUTONOMOUS_RESEARCH_ROADMAP.md`, and the current gap analysis in
`docs/FINAL_GOAL_GAP_ANALYSIS_2026_08_12.md`.
