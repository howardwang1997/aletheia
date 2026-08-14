# F7 issue 11 Frontier Gate acceptance/report implementation report

Date: 2026-08-14

## Outcome

F7 issue 11 is engineering-complete. Aletheia can now preregister a validation-calibration policy,
derive immutable test thresholds from audited validation and independent reference evidence,
freeze a multi-track scientific claim before held-out execution, re-audit every raw test receipt,
and emit a receipt-linked `PASS`, `FAIL`, or `BLOCKED` JSON/Markdown/SVG report.

This is not a scientific pass. The repository has no production acceptance configuration, no
commissioned 10–20-task private suite, no four real held-out model matrices, and no external
approval or ledger-head archive. With current repository evidence a formal readiness report is
correctly `BLOCKED`. Synthetic fixture outcomes are contract tests and cannot support claims that
K2 improves science or that Aletheia is an autonomous frontier scientist.

## Delivered

### Pre-validation calibration contract

[`aletheia/evals/frontier_gate.py`](../aletheia/evals/frontier_gate.py) adds immutable schemas for:

- independently reviewed expert/author/reference-implementation baselines;
- absolute and validation-retention rules for scientific objectives;
- direct/no-K2 superiority and generic-agent noninferiority;
- reliability, invalidity, retry, cost, intervention, contamination, and coverage policies;
- one suite calibration plan frozen before validation;
- one validation-derived suite acceptance config;
- one independently approved program acceptance config frozen before test.

The calibration plan binds exact validation/test matrices and suites, evaluator, four arms,
analysis policy, mismatch disclosures, reference identities, and different owner/reviewer roles.
Reference measurements must predate freeze and bind covered validation tasks. Validation and test
treatments cannot drift.

`calibrate_suite_acceptance` reruns complete result/ledger/HMAC aggregation. It refuses incomplete
costs, insufficient reference coverage, invalid paired samples, or validation evidence that does
not support the preregistered superiority/noninferiority claim. Given an explicit calibration
timestamp, identical evidence produces an identical acceptance config.

### Formal program gate

`FrontierGateAcceptanceConfig` binds the claim, suite configs, two different principals, and two
external approval-evidence identities. Formal tier requires exactly ScienceAgentBench, COREBench,
DiscoveryWorld, and private prospective tracks; one evaluator; identical four system arms; and
correctly directed false-discovery, calibration-error, evidence-provenance, and reproduction
objectives.

The config hash exists before private-suite freeze. The private manifest then binds that program
hash while the program's private suite config binds only the decrypted suite/test matrix, avoiding
a circular hash.

### Fail-closed measured decisions

For every supplied track, report generation validates frozen test identities and confirms execution
started after program freeze. It calls `aggregate_baseline_matrix` over raw inputs; saved operator
aggregates are not accepted.

Criteria cover:

- no-best-of-N ledger completeness;
- full-K2 pass@1 and scientific success;
- valid, final-invalid, and infrastructure-retry fractions;
- complete cost receipts and mean USD cost;
- zero human intervention and contamination declarations;
- valid-pair coverage and unconditional comparability;
- paired cost coverage and increase ceilings;
- effect, lower confidence bound, and Holm-adjusted alpha for superiority;
- lower confidence bound for noninferiority;
- objective means and missingness.

Complete measured evidence that misses any threshold is `FAIL`. A configured missing bundle is
`BLOCKED`. Missing evidence takes precedence over a partial measured failure because the program
conclusion is incomplete. `scientific_claim_allowed` is true only for overall `PASS`.

### Receipt and custody traceability

Every suite decision embeds the audited aggregate plus a content-addressed index of every attempt,
attempt manifest, execution receipt, submission, inner scorer receipt, signed scorer envelope,
ledger head, and whole-ledger file hash. Omitted attempts and forged signatures are rejected during
reaggregation.

Private evidence is a separate decision. It verifies the exact acceptance/suite/evaluator/matrix
bindings, tier, post-config manifest freeze, unique registration, authorization, unique open and
close, materialization, zero failure and contamination, test/open/cleanup ordering, cleanup and
materialization receipt contents, access closure, and retirement. A private statistical result
cannot compensate for incomplete custody, and custody success cannot compensate for a measured
threshold miss.

The final evidence-bundle hash commits to all suite-decision hashes, custody evidence, program
criteria, and acceptance config.

### Operator artifacts

[`scripts/run_frontier_gate.py`](../scripts/run_frontier_gate.py) provides:

- `calibrate-suite` — audit validation and derive one immutable suite config;
- `freeze-config` — combine independently reviewed suite configs into the pre-test program rule;
- `report` — load a non-secret evidence index, re-audit raw test/custody evidence, and emit JSON,
  Markdown, and SVG.

Receipt key bytes never enter argv or JSON; the CLI reads base64 values from named environment
variables. It preflights distinct output paths, refuses existing files/symlinks, stages complete
artifacts, and writes mode-`0600` files. Markdown includes per-track criteria, failure decomposition,
receipt heads, custody evidence, and limitations. SVG plots pass@1, scientific validity, and mean
cost against calibrated thresholds. Both are views over the typed JSON verdict.

The complete workflow and evidence-index schema are documented in
[`docs/benchmarks/FRONTIER_GATE_REPORT.md`](benchmarks/FRONTIER_GATE_REPORT.md). The decision and
rejected alternatives are in
[`docs/adr/0008-validation-calibrated-frontier-gate-report.md`](adr/0008-validation-calibrated-frontier-gate-report.md).

## Threat-model traceability

| Threat | Control | Adversarial evidence |
|---|---|---|
| Thresholds selected after test | plan before validation; program config before test | late plan and pre-freeze test rejected |
| Test treatment/analysis drift | exact matrix/suite/evaluator/arm/analysis hashes | changed bootstrap policy rejected |
| Reference evidence invented later | measurement time and suite/task coverage frozen | post-freeze and outside-suite evidence rejected |
| Operator edits aggregate | reporter accepts only raw result/ledger/keys | complete raw reaggregation required |
| Best attempt retained, failed one omitted | no-best-of-N reconciliation | omitted attempt rejected |
| Scorer result forged | evaluator-owned HMAC verification | changed signature rejected |
| Weak result called unavailable | complete miss maps to `FAIL` | two full-K2 misses produce fail, not block |
| Missing run called pass | one blocked decision per absent configured track | empty formal evidence maps to four missing tracks |
| Public-only result called formal | formal exact four-track schema | incomplete formal freeze rejected |
| Private score used before cleanup | custody close/cleanup/retirement criteria | integrated one-time lifecycle audited separately |
| Same person approves claim | owner and independent auditor must differ | self-approved freeze rejected |
| Views edit verdict | Markdown/SVG consume typed report only | render tests retain report hashes/verdict |
| Existing evidence overwritten | all output paths preflighted and immutable | CLI round trip refuses second report write |

## Verification

The 13 focused tests cover deterministic calibration, plan timing, treatment drift, reference
timing/coverage, a complete receipt-linked pass, a measured fail, missing-track blocking, omitted
attempts, forged scorer signatures, formal track/objective requirements, self-approval rejection,
private custody integration, Markdown/SVG rendering, and the complete CLI round trip with immutable
outputs.

Final verification after implementation:

- focused Frontier Gate tests: **13 passed**;
- all non-Docker evaluator tests: **169 passed, 22 deselected**;
- complete non-Docker project under controlled local PostgreSQL/data-source access:
  **667 passed, 1 skipped, 29 deselected** in **300.80 s**;
- complete real Docker isolation group: **29 passed, 668 deselected** in **26.53 s**;
- changed Python files pass Ruff and targeted Ruff format checks; all evaluator/CLI modules compile;
  `git diff --check` passes.

The first full-project attempt inside the restricted filesystem/network sandbox reached
**484 passed, 1 skipped** but could not connect to the project's existing local PostgreSQL service
and could not download one online fixture. Those environment-denied dependents were not treated as
code failures; the complete controlled rerun above is the acceptance result.

## Limits and operational F7 exit

Engineering issue 11 completes the planned F7 evaluation/report implementation slice. It does not
complete F7's scientific exit. Evaluator and custody operators still have to provide:

- frozen production model, prompt, tool, budget, image, and evaluator manifests;
- licensed public benchmark assets and a preregistered validation/test sampling decision;
- independent reference/expert evidence and authenticated approvals;
- a real unpublished multi-domain private suite, KMS/object store, and external ledger anchoring;
- paid four-arm validation and held-out runs with complete usage receipts;
- independent review of the resulting claim, limitations, and failure decomposition.

Repository development can now start issue 12, the F8 knowledge-schema ADR/fixture spike, without
pretending those external F7 operations have happened. Any later F8–F12 capability must continue to
regress against this frozen evaluation plane, and a real F7 result remains the scientific release
gate.
