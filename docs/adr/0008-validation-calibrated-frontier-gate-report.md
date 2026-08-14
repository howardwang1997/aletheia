# ADR 0008: Validation-calibrated, receipt-linked Frontier Gate decisions

- Status: Accepted
- Date: 2026-08-14
- Scope: F7-S6 / implementation issue 11

## Context

A held-out evaluation is not credible if its thresholds are chosen after seeing test outcomes, if
an operator can replace an aggregate with a hand-edited JSON file, or if missing tracks are treated
as zero failures and silently ignored. The final F7 decision must also distinguish a measured miss
from an evaluation that was never completed: the former is `FAIL`; the latter is `BLOCKED`.

F7 already had evaluator isolation, signed scorer receipts, append-only attempt ledgers, a paired
four-arm matrix, and one-time private-suite custody. It lacked the final contract that freezes a
claim and numerical decision rule before held-out access, replays the complete raw-evidence audit,
and joins public results to the private custody lifecycle.

The design assumes an evaluator owner and independent auditor can retain approval artifacts and
ledger heads outside the research process. It does not assume hashes authenticate those people or
that repository fixtures are real scientific evidence.

## Decision

### Two-stage freeze

Every track starts with a `SuiteCalibrationPlan`. Before validation execution it binds:

- the exact validation and test matrices and suites;
- one evaluator and the same four system-arm identities;
- paired analysis, confidence level, bootstrap seed, missingness policy, and Holm correction;
- direct/no-K2 superiority and generic-agent noninferiority rules;
- objective metric directions and absolute scientific boundaries;
- independently reviewed expert, author, or reference-implementation evidence;
- different calibration-owner and reviewer identities.

Reference evidence must already exist when the plan freezes, bind the validation suite, and cover
the required fraction of validation tasks. Test treatments, analysis, evaluator, and mismatch
disclosures must equal their validation parents.

After validation ends, `calibrate_suite_acceptance` re-audits the raw result, attempt ledger, and
signed scorer receipts. Thresholds are deterministic functions of the preregistered policy,
validation measurements, and reference evidence. The resulting `SuiteAcceptanceConfig` binds both
validation provenance and the exact held-out test identities.

An independently approved `FrontierGateAcceptanceConfig` then combines suite configs and freezes
the scientific claim before any held-out result starts. Formal `frontier_gate` configuration
requires exactly these tracks:

1. ScienceAgentBench;
2. COREBench;
3. DiscoveryWorld;
4. private prospective evaluation.

All tracks use the same evaluator and four system arms. Formal configurations also require
correctly directed false-discovery rate, calibration error, evidence-provenance completeness, and
reproduction-fidelity objectives. A pilot may contain fewer tracks, but cannot be represented as a
formal result.

### Threshold derivation and decision semantics

Full-K2 thresholds cover operational pass@1, scientific success conditional on valid verdicts,
scientific-valid and final-invalid fractions, infrastructure retries, complete trusted cost
observations, mean cost, zero human interventions, and zero contamination declarations.

Direct-model and no-K2 comparisons require a positive practical effect, a paired lower confidence
bound that clears the same effect, and a Holm-adjusted p-value below the frozen alpha. The generic
agent requires the paired lower confidence bound to clear a frozen noninferiority margin. Every
comparison also requires sufficient valid pairs, complete paired cost, a calibrated cost-increase
ceiling, and unconditional comparability. A disclosed resource mismatch therefore cannot yield an
unconditional formal pass.

Objective thresholds combine an absolute scientific boundary with a bounded validation
degradation. Missing objective values are themselves an endpoint. No test observation can alter a
threshold.

Verdicts have fixed meanings:

- `PASS`: every configured measurement and required private-custody criterion passes;
- `FAIL`: complete, authenticated evidence exists and at least one measured criterion misses;
- `BLOCKED`: a configured track, receipt bundle, or required private-custody artifact is absent.

`BLOCKED` takes precedence when the program evidence is incomplete. Scientific-claim output is
enabled only for an overall `PASS`.

### Raw evidence, not operator aggregates

`generate_frontier_gate_report` accepts matrices, suite manifests, result bundles, append-only
ledgers, and evaluator-owned receipt verification keys. It calls the same complete
`aggregate_baseline_matrix` audit used by the baseline layer. It rejects omitted or undeclared
attempts, changed ledger files, invalid retry lineages, altered manifests, forged scorer HMACs,
wrong run plans, and changed test analysis. It never accepts an operator-supplied aggregate report
as authoritative input.

Each suite decision embeds the audited aggregate and a receipt index containing every attempt,
attempt manifest, execution receipt, submission, scorer receipt, signed envelope, ledger head, and
whole-ledger file identity. The final evidence-bundle hash commits to every suite decision, custody
evidence, and program criterion.

### Private-suite closure

The private custody manifest binds the program acceptance-config hash after that config freezes;
the suite acceptance config itself binds only the decrypted evaluation suite and test matrix, so
there is no circular content hash.

A private result cannot pass until the report verifies unique registration, two-person
authorization, one-time open after acceptance freeze, verified materialization, execution within
the opened lifecycle, no materialization failure, zero contamination reports, post-run cleanup,
access closure, cleanup/materialization receipt integrity, and suite retirement. A statistical
pass without custody closure fails the program; custody closure without a measured statistical
pass also fails.

### Immutable views

The typed JSON report is authoritative. Markdown and SVG are deterministic views over that model;
they do not recompute or edit criteria. The operator CLI preflights and refuses all existing output
paths, reads receipt keys only from named base64 environment variables, re-audits raw evidence,
and emits JSON, Markdown, and SVG together.

This lifecycle follows NIST AI RMF's emphasis on governed, documented, repeatable TEVV and safe
decommissioning: [NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/). The
private-test motivation is consistent with the contamination risks discussed by
[TRUCE](https://arxiv.org/abs/2403.00393), without claiming TRUCE's confidential-computing or
cryptographic protocol.

## Consequences

- Test-dependent threshold editing changes a frozen hash and is rejected.
- A real F7 result requires four complete raw evidence bundles and a closed private custody chain.
- Reliability, invalidity, cost, intervention, and contamination cannot disappear behind a mean
  scientific score.
- Re-running report generation re-verifies signatures and ledger completeness instead of trusting
  a previous summary.
- Formal report engineering can be complete while the current program verdict remains `BLOCKED`
  because no real configurations, private commission, or model runs exist.
- Approval evidence, receipt-key custody, external ledger anchoring, and real task quality remain
  deployment responsibilities.

## Rejected alternatives

- **Hand-authored YAML thresholds after a pilot test:** permits result-dependent gate selection.
- **Use public benchmark leaderboard values directly:** does not calibrate this exact model,
  treatment, evaluator, budget, or task sample.
- **Trust a saved aggregate JSON:** permits attempt omission or score editing outside ledger and
  signature verification.
- **Treat missing tracks as failures:** confuses absence of evidence with a measured negative and
  obscures the operational blocker.
- **Treat missing tracks as passes:** converts an incomplete evaluation into a false scientific
  claim.
- **Let a private score pass before cleanup/retirement:** leaves one-time access and contamination
  obligations unresolved.
