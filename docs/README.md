# Aletheia Docs

This directory contains the architecture, project review, roadmap, and implementation RFCs for
Aletheia.

Current target-architecture RFC:

- `END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md` — the first-principles architecture for
  turning Aletheia into an end-to-end autonomous research system: one scientific authority, an
  event-derived research graph, executable protocol IR, capability composition, reality execution,
  independent validation, distributed compute, migration slices, and prospective discovery gates.

Current implementation slice:

- `migration/PR0_LEGACY_FREEZE.md` — operator/developer guide for the content-addressed legacy freeze,
  dependency boundary, write-owner inventory, golden projections, and immutable endurance evidence.
- `adr/0045-pr0-legacy-freeze-and-scientific-authority-boundary.md` — why legacy behavior remains
  engineering-only evidence and how PR-0 prevents a second scientific authority.
- `adr/0046-root-certified-research-command-event-store.md` — why root-certified signed commands,
  replay, CAS custody, and a cross-store Quest namespace form the PR-2 authority boundary.
- `migration/PR2_RESEARCH_EVENT_STORE.md` — deployment pins, migration, API cutover, audit/replay,
  emergency halt, failure recovery, and current scaling limits for the PR-2 event store.
- `architecture/0047-scientific-protocol-compiler.md` — the PR-3 decision to keep protocol
  compilation pure, graph-scoped, domain-independent, and separate from execution/admission, with
  mandatory pure intent/retry checks at the future launch boundary.
- `PR3_PROTOCOL_COMPILER.md` — PR-3 contract map, hard gates, canonical compilation workflow,
  typed input-receipt and retry bindings, heterogeneous fixtures, frozen v1 bindings, verification
  commands, and PR-4 boundary.

Supporting technology research (not the system architecture):

- `CODEX_HARNESS_LDM_BENCHMARK_STRATEGY_2026_08_21.md` — how to combine the Codex agent harness,
  Large Discovery Model search, Aletheia's scientific control plane, and a causal benchmark matrix
  without weakening the independent evidence boundary. Its named components are optional proposal/
  execution strategies; they do not define the target module graph or implementation order.

After the target-architecture RFC above, use this order for the current implementation history:

1. `ARCHITECTURE.md` — current system architecture and invariants.
2. `PROJECT_REVIEW.md` — neutral assessment of strengths, risks, and gaps.
3. `FRONTIER_SCIENTIST_F7_F12_DETAILED_PLAN_2026_08_13.md` — current executable master plan from
   the completed Real Campaign Gate baseline to a domain-level autonomous frontier scientist.
4. `PF1_PF2_F7S1_IMPLEMENTATION_REPORT_2026_08_13.md` — implementation and migration/restore
   evidence for the first Frontier Scientist foundation slice.
5. `F7S2_INDEPENDENT_RUNNER_IMPLEMENTATION_REPORT_2026_08_13.md` — independent evaluator runner,
   append-only attempt ledger, resource enforcement, submission validation, and signed receipts.
6. `F7_ISSUE_6_SCIENCEAGENTBENCH_IMPLEMENTATION_REPORT_2026_08_14.md` — frozen licensed mini-suite,
   two-plane official objective scorer, reproducibility, and adversarial acceptance evidence.
7. `benchmarks/SCIENCEAGENTBENCH_ADAPTER.md` — operator runbook for acquiring assets, building the
   reviewed runtime, preparing a suite, and interpreting verdicts.
8. `F7_ISSUE_7_COREBENCH_IMPLEMENTATION_REPORT_2026_08_14.md` — sanitized public-repository
   boundary, Asta CORE-Bench-Hard objective/artifact scorer, and acceptance evidence.
9. `benchmarks/COREBENCH_ADAPTER.md` — operator runbook for the frozen public validation subset,
   licensed capsules, offline image, suite preparation, and verdict semantics.
10. `F7_ISSUE_8_DISCOVERYWORLD_IMPLEMENTATION_REPORT_2026_08_14.md` — two-container hidden world,
    objective action trace, information gain, explicit rule discovery, and real Docker evidence.
11. `benchmarks/DISCOVERYWORLD_ADAPTER.md` — operator runbook for building both images, freezing the
    public suite, authoring a policy, and interpreting trajectory/verdict evidence.
12. `F7_ISSUE_9_BASELINE_MATRIX_IMPLEMENTATION_REPORT_2026_08_14.md` — same-model four-arm
    preregistration, paired execution/statistics, no-best-of-N ledger reconciliation, and evidence.
13. `benchmarks/BASELINE_MATRIX.md` — evaluator operator contract for materializing, running, and
    aggregating direct/generic/no-K2/full-K2 comparisons.
14. `F7_ISSUE_10_PRIVATE_SUITE_IMPLEMENTATION_REPORT_2026_08_14.md` — prospective custody,
    role-separated encryption, one-time unlock, contamination, verified cleanup, and retirement.
15. `benchmarks/PRIVATE_SUITE_CUSTODY.md` — evaluator-owner runbook for commissioning, sealing,
    authorizing, materializing, running, closing, contaminating, retiring, and auditing private
    suites.
16. `adr/0007-private-suite-custody-and-retirement.md` — threat model, custody decision, guarantees,
    and explicit non-guarantees.
17. `F7_ISSUE_11_FRONTIER_GATE_REPORT_IMPLEMENTATION_REPORT_2026_08_14.md` — validation-calibrated
    thresholds, frozen claims, raw receipt reaggregation, custody-linked verdicts, and evidence.
18. `benchmarks/FRONTIER_GATE_REPORT.md` — evaluator runbook for calibrating, freezing, indexing
    evidence, and issuing immutable JSON/Markdown/SVG PASS/FAIL/BLOCKED artifacts.
19. `adr/0008-validation-calibrated-frontier-gate-report.md` — two-stage freeze, decision semantics,
    evidence boundary, and explicit scientific non-claims.
20. `F8_ISSUE_12_KNOWLEDGE_SCHEMA_SPIKE_REPORT_2026_08_14.md` — immutable temporal corpus,
    source-span, search/coverage, claim/prior-art, novelty, and protocol-safe SOTA contracts.
21. `benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md` — developer guide to the issue-12 object boundary,
    synthetic temporal fixture, guarantees, tests, and explicit non-capabilities.
22. `adr/0009-f8-knowledge-boundary-schema-spike.md` — temporal false-novelty threat model,
    literature-as-untrusted-data decision, schema boundary, and rejected alternatives.
23. `F8_S1_CORPUS_PERSISTENCE_IMPLEMENTATION_REPORT_2026_08_14.md` — license-explicit provider
    receipts, immutable ordered PostgreSQL persistence, conflict/rollback, triggers, and CLI.
24. `knowledge/CORPUS_PERSISTENCE.md` — operator/developer guide for migration, bundle schema,
    access-right rules, validation, persistence, inspection, recovery, and current limits.
25. `adr/0010-f8-immutable-corpus-persistence-and-access-rights.md` — official rights/selector
    evidence, hash-only storage decision, relational layout, trust boundary, and alternatives.
26. `F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md` — deterministic query axes,
    metadata-only content-addressed responses, complete failure ledgers, replay, citation rounds,
    and fail-closed coverage derivation.
27. `knowledge/SEARCH_REPLAY_AND_CITATION.md` — developer guide for freezing plans/adapters,
    executing and committing, replaying, traversing citations, and building coverage evidence.
28. `adr/0011-f8-deterministic-search-replay-and-citation-traversal.md` — provider/API constraints,
    metadata retention boundary, mechanical frontier decision, and explicit non-capabilities.
29. `F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md` — licensed ephemeral span
    access, strict structured claims, confidence review, immutable graph closure, and replay.
30. `knowledge/CLAIM_EXTRACTION_AND_REVIEW.md` — developer guide for manifests, content resolvers,
    extraction, review resolution, graph construction, commitment, and replay.
31. `adr/0012-f8-licensed-atomic-claim-extraction-and-independent-review.md` — related scientific
    NLP evidence, authorization/trust decision, review semantics, and explicit non-capabilities.
32. `F8_S4_PRIOR_ART_MATCHING_IMPLEMENTATION_REPORT_2026_08_15.md` — four-channel complete-union
    recall, no-delete reranking, strict relation/component differences, review, and ledgers.
33. `knowledge/PRIOR_ART_MATCHING_AND_REVIEW.md` — developer guide for recall/matcher manifests,
    union semantics, execution, stage failures, relation review, commitment, and current limits.
34. `adr/0013-f8-auditable-multichannel-prior-art-matching.md` — related retrieval evidence,
    no-delete decision, relation/review semantics, consequences, and rejected alternatives.
35. `F8_S5_CALIBRATED_NOVELTY_IMPLEMENTATION_REPORT_2026_08_15.md` — evaluator-owned temporal
    calibration, confidence-bound acceptance, artifact-derived coverage, review, and direction gate.
36. `knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md` — developer/operator guide for suite sealing,
    receipts, metrics, live evidence derivation, authorship, independent review, and gate use.
37. `adr/0014-f8-calibrated-novelty-acceptance-and-direction-gate.md` — related benchmark evidence,
    statistical/coverage/review decisions, limitations, consequences, and rejected alternatives.
38. `F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md` — pre-sealed references,
    signed paired results, complete comparison matrix, Holm/practical superiority, and write-up gate.
39. `knowledge/PROTOCOL_SAFE_SOTA_EVALUATION.md` — developer/evaluator guide for registry sealing,
    exact protocols, receipt issuance, campaign interpretation, archive, and claim consumption.
40. `adr/0015-f8-presealed-protocol-safe-sota-evaluation.md` — benchmark evidence, threat model,
    selection/evaluation/statistical decisions, consequences, and rejected alternatives.
41. `F9_S1_WORLD_MODEL_VERSIONING_IMPLEMENTATION_REPORT_2026_08_15.md` — stable lineages,
    immutable versions, closed competing beliefs, PostgreSQL persistence, and K2 compatibility.
42. `epistemics/WORLD_MODEL_VERSIONING.md` — developer/operator guide for constructing, revising,
    storing, recovering, and reading F9 snapshots and legacy K2 projections.
43. `adr/0016-f9-immutable-competing-world-model-versioning.md` — provenance/model-workflow basis,
    version decisions, K2 non-migration boundary, consequences, and rejected alternatives.
44. `F9_S2_COMPETING_HYPOTHESIS_GENERATION_IMPLEMENTATION_REPORT_2026_08_15.md` — exact F8-bound
    request, independent complete semantic pair ledger, explicit duplicate provenance, pairwise
    prediction-discrimination proof, sanitized failures, and F9-S1 admission evidence.
45. `epistemics/F8_GROUNDED_HYPOTHESIS_GENERATION.md` — developer/operator guide for manifests,
    adapters, grounding and duplicate gates, campaign execution, archive, persistence, and limits.
46. `adr/0017-f9-f8-grounded-competing-hypothesis-admission.md` — related-work basis, proposer versus
    admission boundary, complete-pair and discrimination decisions, consequences, and alternatives.
47. `F9_S3_CAUSAL_CONTRACT_IDENTIFICATION_IMPLEMENTATION_REPORT_2026_08_15.md` — typed graphs,
    exact F8/F9 binding, structural and back-door witnesses, independent assumption review, bounded
    claim ceilings, failure/archive semantics, and acceptance evidence.
48. `epistemics/CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md` — developer/operator guide for causal
    objects, graph rules, back-door audit, assumption review, dispositions, archive, and limits.
49. `adr/0018-f9-explicit-causal-contract-and-bounded-identification.md` — causal-inference basis,
    exact implemented criterion, proposer/reviewer/harness split, consequences, and alternatives.
50. `F9_S4_PREOBSERVATION_PREDICTION_COMMITMENT_IMPLEMENTATION_REPORT_2026_08_15.md` — frozen
    experiment/outcome protocols, calibrated likelihood family, admission probes, immutable receipt,
    observation seal, mutation violation, and acceptance evidence.
51. `epistemics/PREOBSERVATION_PREDICTION_COMMITMENT.md` — developer/operator guide for outcome
    schemas, calibration, probability/ordinal semantics, execution, archive, and observation staging.
52. `adr/0019-f9-pre-observation-prediction-commitment-and-observation-seal.md` — preregistration and
    forecast-verification basis, exact trust boundary, consequences, and rejected alternatives.
53. `F9_S5_CONSTRAINED_EXPERIMENT_SELECTION_IMPLEMENTATION_REPORT_2026_08_15.md` — physical
    prediction-candidate verification, full EIG ledgers, hard constraints, fixed utility, complete
    ranking reasons, no-feasible semantics, and acceptance evidence.
54. `epistemics/CONSTRAINED_EXPERIMENT_SELECTION.md` — developer/operator guide for exact candidates,
    independent assessment, EIG, constraints, utility, ranking, failure, archive, and current limits.
55. `adr/0020-f9-observation-blind-constrained-experiment-selection.md` — Bayesian design,
    model-discrimination, multicriterion, measurement-validity, and no-fallback design decisions.
56. `F9_S6_VALIDATED_OBSERVATION_BELIEF_UPDATE_IMPLEMENTATION_REPORT_2026_08_15.md` — physical
    validation boundaries, scientific admission gates, exact posterior/sensitivity ledgers,
    immutable revision directives, contradiction queue, and acceptance evidence.
57. `epistemics/VALIDATED_OBSERVATION_BELIEF_UPDATE.md` — developer/operator guide for independent
    validation, raw-data isolation, Bayesian update, fragility, revisions, archive, and limitations.
58. `adr/0021-f9-validated-observation-bayesian-revision.md` — Bayesian workflow, robust-update,
    preregistration, protocol-deviation, truth-maintenance, and append-only design decisions.
59. `F9_S7_INDEPENDENT_K3_ACCEPTANCE_IMPLEMENTATION_REPORT_2026_08_15.md` — physical full-chain
    replay, eleven independent checks, nonvacuous verdicts, claim/revision/persistence gates, and
    acceptance evidence.
60. `epistemics/INDEPENDENT_K3_ACCEPTANCE.md` — developer/operator guide for round evidence,
    persistence ledger, scoring, dispositions, mechanism/negative-result gates, archive, and limits.
61. `adr/0022-f9-independent-k3-evidence-chain-acceptance.md` — strong-inference, prequential,
    provenance, reproducibility, independent-scoring, and prediction-changing revision decisions.
62. `F9_S8_TRANSACTIONAL_WORLD_MODEL_CONTINUATION_IMPLEMENTATION_REPORT_2026_08_15.md` — atomic
    posterior/revision persistence, typed scheduler event, K3-gated authorization, and verified
    second-round causal consumption.
63. `epistemics/TRANSACTIONAL_WORLD_MODEL_CONTINUATION.md` — developer/operator guide for building,
    committing, recovering, authorizing, and consuming F9 world-model transitions.
64. `adr/0023-f9-transactional-world-model-continuation.md` — revision-closure, transaction,
    event-projection, terminal-action, and fork decisions.
65. `F9_S9_K3_HIDDEN_WORLD_SCIENTIFIC_EXIT_HARNESS_IMPLEMENTATION_REPORT_2026_08_15.md` — matched
    headline/K2/K3 execution, truth-relative endpoints, paired statistics, threshold freeze, and
    honest private-evidence blocker.
66. `benchmarks/K3_HIDDEN_WORLD_SCIENTIFIC_EXIT.md` — evaluator runbook for protocol inspection,
    three-arm execution, signed aggregation, acceptance freeze, and final decision semantics.
67. `adr/0024-f9-frozen-k3-hidden-world-scientific-exit.md` — treatment isolation, hidden metrics,
    calibration/false-mechanism, pairing, preregistration, and custody decisions.
68. `F9_S10_REAL_MATERIALS_EVIDENCE_CHAIN_IMPLEMENTATION_REPORT_2026_08_15.md` — frozen real
    Matbench alternatives, EIG selection, signed measurement/recomputation, posterior sensitivity,
    retained unfavorable result, and explicit non-exit.
69. `benchmarks/K3_REAL_MATERIALS_EVIDENCE_CHAIN.md` — operator runbook for preregistration,
    separately keyed execution/validation, update, replay, and current evidence interpretation.
70. `adr/0025-f9-authenticated-real-materials-evidence-chain.md` — retrospective evidence scope,
    chemical-system control, all-scenarios revision, anti-best-of-N, and custody decisions.
71. `F10_S1_CAPABILITY_REGISTRY_AND_REPLICATION_IMPLEMENTATION_REPORT_2026_08_15.md` — immutable
    capability contracts, append-only schema correction, exact planning, full five-slot real matrix,
    and honest partition-sensitive result.
72. `benchmarks/F10_MATERIALS_CAPABILITY_REPLICATION.md` — operator runbook and frozen identities,
    commands, result matrix, verification, and interpretation boundary.
73. `adr/0026-f10-versioned-capability-registry-and-full-matrix-replication.md` — schema-version,
    provisional-evidence, all-slot, no-pseudo-replication, and promotion-boundary decisions.
74. `F10_S2_TYPED_OBSERVATION_PIPELINE_IMPLEMENTATION_REPORT_2026_08_15.md` — raw/parse/validate
    separation, units/uncertainty/conditions, negative preservation, real reexecution, and admission.
75. `capabilities/TYPED_OBSERVATION_PIPELINE.md` — state model, typed requirements, persistence,
    replay command, and current limits.
76. `adr/0027-f10-typed-observation-and-negative-result-boundary.md` — invalid-versus-negative,
    UCUM, independent validation, provenance, and anti-double-counting decisions.
77. `F10_S3_MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT_IMPLEMENTATION_REPORT_2026_08_15.md` —
    formula/structure/sample/batch identity, multi-level split leakage, measurement audit, gold
    fixtures, and real Matbench identity evidence.
78. `capabilities/MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT.md` — identity hierarchy, pooling rules,
    replay commands, unresolved composition collisions, and current limits.
79. `adr/0028-f10-material-identity-and-conservative-measurement-audit.md` — CIF/sample/metrology
    basis, fail-closed identity, conservative pooling, and licence-boundary decisions.
80. `F10_S4_STRUCTURE_AWARE_EXPERIMENT_IMPLEMENTATION_REPORT_2026_08_15.md` — structure quality,
    frozen three-arm matched control, real Matbench phonon result, replay, and claim boundary.
81. `capabilities/STRUCTURE_AWARE_MATERIALS_EXPERIMENT.md` — acquisition, immutable plan/run/replay
    commands, result interpretation, and current non-claims.
82. `adr/0029-f10-precommitted-matched-structure-discrimination.md` — target-blind preflight,
    chemical-system split, equal-capacity permutation, cluster bootstrap, and claim-ceiling decision.
83. `F10_S5_REPRODUCIBLE_SIMULATION_CAPABILITY_IMPLEMENTATION_REPORT_2026_08_15.md` — digest-pinned
    ASE/EMT execution, checkpoint/raw lineage, append-only infrastructure correction, reference
    calibration, physical replay, provisional registry, and bounded claim.
84. `capabilities/ASE_EMT_REFERENCE_SIMULATION.md` — frozen identities, execution/replay commands,
    validation/failure semantics, current reference result, and promotion gates.
85. `adr/0030-f10-digest-pinned-ase-emt-reference-simulation.md` — classical-before-DFT stack,
    sandbox, failure retention, exact-repetition, append-only, and provisional-boundary decisions.
86. `F10_S6_MECHANISTIC_CAMPAIGN_TEMPLATE_IMPLEMENTATION_REPORT_2026_08_16.md` — exact F8/F9/F10
    lineage, registry-bound multi-family slots, fresh independence, robust per-slot scoring,
    evidence bundle, current readiness blockers, and honest non-exit.
87. `capabilities/MECHANISTIC_CAMPAIGN_TEMPLATE.md` — state model, disposition/claim semantics,
    readiness CLI, real-protocol workflow, and current release work.
88. `adr/0031-f10-fail-closed-mechanistic-campaign-composition.md` — registry trust, separate
    execution/release gates, no-joint-posterior, fresh confirmation, and claim-ceiling decisions.
89. `F10_S7_CAPABILITY_AUTHORING_AND_PROMOTION_IMPLEMENTATION_REPORT_2026_08_16.md` — hard-sandbox
    authoring evidence, separate generated tests/validation, Ed25519 role policy, independent audit,
    signed append-only update, malicious-capability coverage, and honest production blockers.
90. `capabilities/CAPABILITY_AUTHORING_AND_PROMOTION.md` — trust objects, signature/permission
    rules, owner-only key handling, audit/promote/verify CLI, failure semantics, and commissioning.
91. `adr/0032-f10-signed-role-separated-capability-promotion.md` — SLSA/in-toto/TUF-inspired
    attestation, role-threshold, no-self-validation, rollback, and non-claim decisions.
92. `F11_S1_DURABLE_TASK_ORCHESTRATION_IMPLEMENTATION_REPORT_2026_08_16.md` — Postgres queue,
    content-bound delivery, lease/heartbeat/retry, process-kill recovery, task/event atomicity,
    independent worker integration, and honest F11-S2 boundary.
93. `jobs/DURABLE_TASK_ORCHESTRATION.md` — deployment, task states, API/CLI/worker operation,
    event-cursor resume, failure interpretation, recovery, and debugging.
94. `adr/0033-f11-postgres-durable-queue-and-event-cursor.md` — at-least-once delivery,
    `SKIP LOCKED`, hashed leases, durable SSE cursor, and rejected exactly-once/broker claims.
95. `F11_S2_TRANSACTIONAL_SCIENTIFIC_TRANSITIONS_IMPLEMENTATION_REPORT_2026_08_17.md` — exact
    scientific commands, phase-separated epistemic commits, one-time outward authorization,
    immutable receipts, reconciliation, and honest cross-system boundary.
96. `jobs/TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md` — command/action contracts, deployment,
    inspection, recovery, failure interpretation, and focused acceptance suite.
97. `adr/0034-f11-transactional-scientific-commands-and-external-action-receipts.md` — database
    outbox atomicity, content-bound replay, stable provider keys, and rejected global exactly-once
    claim.
98. `F11_S3_RESEARCH_PROGRAM_GRAPH_IMPLEMENTATION_REPORT_2026_08_17.md` — reconstructible hierarchy,
    cross-Campaign family disclosure, scientific DAG, typed allocations, controller-only UI, and
    acceptance evidence.
99. `programs/RESEARCH_PROGRAM_GRAPH.md` — deploy, creation/lifecycle/dependency operation,
    family attempts, allocations, API/dashboard projection, reconstruction, and incident handling.
100. `adr/0035-f11-reconstructible-scientific-program-graph.md` — hierarchy/task separation,
    append-only lifecycle, concurrent cycle prevention, portfolio command scope, and rejected reset
    paths.
101. `F11_S4_RECEIPT_BACKED_SCIENTIFIC_MEMORY_IMPLEMENTATION_REPORT_2026_08_17.md` — immutable
    task-bound facts, exact negative-state retention, complete compaction receipts, deterministic
    recovery, provider-neutral context delivery, and honest scientific boundary.
102. `programs/RECEIPT_BACKED_SCIENTIFIC_MEMORY.md` — deployment, fact/compaction/context workflow,
    API/CLI/worker integration, incident handling, and failure semantics.
103. `adr/0036-f11-receipt-backed-task-scoped-scientific-memory.md` — authoritative ledger,
    task-minimal ancestry, non-destructive compaction, exact protected facts, and provider-switch
    decisions.
104. `F11_S5_SHADOW_RESEARCH_PORTFOLIO_IMPLEMENTATION_REPORT_2026_08_18.md` — observation-blind
    proposal/assessment separation, harness-derived EIG/hard filters, constrained shadow batches,
    pre-result human comparison, persistence, and honest non-activation boundary.
105. `programs/SHADOW_RESEARCH_PORTFOLIO.md` — deployment, slate/human/epoch workflow,
    deterministic scoring, hard/batch gates, API/CLI, readiness audit, and incident handling.
106. `adr/0037-f11-observation-blind-shadow-research-portfolio.md` — no-self-score proposal,
    independent inputs, deterministic selection, shadow-only ledger, and rejected activation.
107. `F11_S6_DETERMINISTIC_FAULT_INJECTION_IMPLEMENTATION_REPORT_2026_08_18.md` — deterministic
    ten-boundary campaign, independent invariant regrading, real failure/recovery evidence,
    append-only persistence, and honest non-activation boundary.
108. `jobs/FAULT_INJECTION_CAMPAIGNS.md` — manifest/executor contract, six zero-loss invariants,
    CLI, persistence, recovery semantics, endurance-review audit, and incident handling.
109. `adr/0038-f11-deterministic-replayable-fault-campaigns.md` — seeded order, observation-only
    executors, full-transaction replay, remote reconciliation, failure retention, and rejected
    exactly-once/automatic-activation claims.
110. `F11_S7_RESEARCH_ENDURANCE_GATE_IMPLEMENTATION_REPORT_2026_08_18.md` — database-clock
    endurance lifecycle, resumable checkpoint chain, typed scientific/fault/pivot evidence,
    accelerated acceptance, and explicit pending real-72-hour boundary.
111. `programs/RESEARCH_ENDURANCE_GATE.md` — deployment, evidence classes, advisory-locked
    supervised run-once controller, write-once evidence spool, start/checkpoint/finalize boundary,
    crash recovery, audit, incident handling, and honest production-run status.
112. `adr/0039-f11-durable-real-time-research-endurance-gate.md` — durable clock, append-only
    lifecycle, frozen authority, structural-pivot semantics, cadence evidence, and rejected fake
    time/activation alternatives.
113. `F11_PRODUCTION_PHONON_QUEST_COMMISSIONING_REPORT_2026_08_18.md` — applied real-evidence Quest,
    exact identities, first-write/replay/audit results, scientific design, external-source research,
    and retained blockers.
114. `programs/PHONON_QUEST_COMMISSIONING.md` — real F10 evidence inspection, two competing world
    models, three bounded Campaigns, deterministic Run/DataAsset creation, frozen budgets,
    production apply/replay/audit identities, external-corpus triage, and explicit blockers.
115. `programs/PHONON_IMPLEMENTATION_REPRODUCTION.md` — zero-fit implementation-diverse protocol,
    independent matrix reconstruction, distinct estimator, gate-bound execution, exact outcome
    memory, endurance evidence submission, and same-source claim ceiling.
116. `adr/0040-gate-bound-implementation-diverse-phonon-reproduction.md` — frozen invariants,
    independent code/estimator variation, zero-fit gate binding, outcome retention, and rejected
    self-replay/external/premature-fit/automatic-pivot alternatives.
117. `programs/ENDURANCE_LAUNCHD_SUPERVISOR.md` — frozen Conda/runtime/controller/plist identity,
    pre-start waiting, loaded-job readiness, run-once operation, and incident/bootout workflow.
118. `adr/0041-frozen-launchd-endurance-supervision.md` — scheduler failure-domain separation,
    exact launchd deployment, explicit start/finalize boundaries, and rejected shell-loop options.
119. `programs/PHONON_ENDURANCE_PORTFOLIO.md` — production candidate freeze, pre-start slate and
    human-blind baseline, in-window shadow epoch, hard-filtered external data, and incident workflow.
120. `adr/0042-precommitted-in-window-phonon-shadow-portfolio.md` — causal ordering, exact source
    binding, genuine human comparison, zero action authority, and rejected hindsight alternatives.
121. `programs/PHONON_NEGATIVE_RESULT_PIVOT.md` — contradiction-only trigger closure, durable
    two-transition pivot, qualification-only successor authority, and crash recovery.
122. `adr/0043-conditional-phonon-negative-result-pivot.md` — exact negative causality,
    non-contradiction rejection, structural fingerprints, and rejected fabricated outcomes.
123. `programs/PHONON_PORTFOLIO_EFFICIENCY.md` — blind single-experiment baseline, derived expected
    question-coverage efficiency, raw finalization receipt, and non-realized claim boundary.
124. `adr/0044-derived-shadow-portfolio-efficiency.md` — non-self-graded value/cost derivation,
    causal epoch timing, below-floor retention, and rejected hand-authored metrics.
125. `AUTONOMOUS_RESEARCH_ROADMAP.md` — historical 3-month, 6-month, and long-term improvement plan.
126. `RFC_GUARDRAILS_AND_EVIDENCE.md` — historical engineering RFC for guardrails and evidence.
127. `architecture/0047-scientific-protocol-compiler.md` — graph-scoped Protocol IR, atomic
    capability resolution, static compilation, exact intent/retry bindings,
    engineering/scientific result separation, and deferred execution authority.
128. `PR3_PROTOCOL_COMPILER.md` — developer guide for constructing, checking, compiling, and
    independently verifying PR-3 work orders, input-receipt identities, and retry lineage without
    live execution or launch authority.
