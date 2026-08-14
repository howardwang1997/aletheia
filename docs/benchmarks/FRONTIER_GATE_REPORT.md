# Frontier Gate acceptance and report runbook

## What this command can and cannot conclude

F7 issue 11 turns calibrated validation evidence and raw held-out receipts into one typed
`PASS`, `FAIL`, or `BLOCKED` decision. The implementation lives in
[`aletheia/evals/frontier_gate.py`](../../aletheia/evals/frontier_gate.py); the operator entry point
is [`scripts/run_frontier_gate.py`](../../scripts/run_frontier_gate.py).

The repository currently supplies the mechanism and synthetic adversarial tests. It does not
contain production model/system manifests, commissioned private tasks, real four-track result
bundles, approval attestations, receipt keys, or an external custody ledger archive. Running a
readiness report without those inputs must produce `BLOCKED`; it is not a scientific failure and
is never a pass.

## Evidence flow

```text
frozen validation + test matrices
              + independent reference evidence
              + preregistered calibration policy
                              |
                              v
                 SuiteCalibrationPlan (pre-validation)
                              |
                      real validation run
                              |
       raw result + ledger + signed scorer receipts
                              |
                              v
                   SuiteAcceptanceConfig
                              |
                    two independent approvals
                              |
                              v
              FrontierGateAcceptanceConfig (pre-test)
                              |
        four raw held-out runs + private custody close
                              |
                              v
             JSON + Markdown + SVG gate artifacts
```

An aggregate JSON is not an input to the last step. The reporter recomputes it from every raw
attempt and verifies the append-only ledger and scorer HMACs.

## 1. Freeze validation and test before validation execution

For each track, prepare:

- a validation `BaselineMatrixPlan`;
- a test `BaselineMatrixPlan` naming the validation matrix as its parent;
- the exact frozen validation and test `EvaluationSuite` manifests;
- identical four-arm treatments, evaluator, paired analysis, and mismatch disclosures;
- an independently reviewed `ReferenceBaselineEvidence` set;
- a `GateCalibrationPolicy` with three comparison rules and every objective endpoint.

`SuiteCalibrationPlan.frozen_at` must precede validation execution. Reference evidence must
precede that freeze, bind the validation suite, and cover the policy's minimum task fraction.

Formal plans require these objective directions:

| Metric | Direction |
|---|---|
| `false_discovery_rate` | maximum |
| `calibration_error` | maximum |
| `evidence_provenance_completeness` | minimum |
| `reproduction_fidelity` | minimum |

Direct-model and no-K2 rules use superiority; generic-agent uses noninferiority. The policy also
sets absolute pass, validity, retry, invalidity, cost, missingness, and zero-intervention/
zero-contamination bounds. These are policy inputs, not values copied from the held-out test.

## 2. Run and calibrate validation

Run the four-arm validation matrix through
[`run_baseline_matrix.py`](../../scripts/run_baseline_matrix.py). Then expose the evaluator-owned
receipt key only through a base64 environment value:

```bash
export FRONTIER_SCORER_KEY_B64='...'

conda run -n aletheia python scripts/run_frontier_gate.py calibrate-suite \
  --plan /evaluator/gate/scienceagentbench.calibration-plan.v1.json \
  --validation-matrix /evaluator/gate/scienceagentbench.validation-matrix.v1.json \
  --validation-suite-bundle /evaluator/suites/scienceagentbench.validation.v1.json \
  --validation-result /evaluator/runs/scienceagentbench.validation-result.v1.json \
  --validation-ledger /evaluator/ledger/scienceagentbench-validation.jsonl \
  --test-matrix /evaluator/gate/scienceagentbench.test-matrix.v1.json \
  --test-suite-bundle /evaluator/suites/scienceagentbench.test.v1.json \
  --suite-config-id scienceagentbench-acceptance-v1 \
  --receipt-key-env evaluator-key-v1=FRONTIER_SCORER_KEY_B64 \
  --output /evaluator/gate/scienceagentbench.acceptance.v1.json
```

Calibration refuses incomplete cost receipts, insufficient reference coverage, invalid paired
samples, unsupported validation superiority/noninferiority, post-freeze reference evidence, and
any matrix/suite/treatment drift.

The derived full-K2 thresholds retain the stricter applicable absolute, validation, and reference
bound. Objective minimums use the larger absolute or validation-retention bound; maximums use the
smaller. Comparison effects and paired cost ceilings are likewise bounded by the preregistered
policy. Therefore a validation result can fail to justify opening a held-out test.

Repeat this step for all intended tracks. It is safe to repeat with the same inputs and explicit
timestamp: the config is deterministic. Output files are immutable and cannot be replaced.

## 3. Freeze the program contract

The freeze-request JSON contains non-secret program metadata:

```json
{
  "program_config_id": "aletheia-frontier-gate-v1",
  "version": "1.0.0",
  "tier": "frontier_gate",
  "acceptance_owner_principal_sha256": "...",
  "independent_auditor_principal_sha256": "...",
  "owner_approval_evidence_sha256": "...",
  "auditor_approval_evidence_sha256": "...",
  "scientific_claim": "Exact claim whose release requires a complete pass.",
  "frozen_at": "2026-08-14T12:00:00+00:00"
}
```

Approval identities and evidence hashes must differ. Production operators authenticate those
external artifacts before invoking the command; a SHA-256 string is not itself a signature.

```bash
conda run -n aletheia python scripts/run_frontier_gate.py freeze-config \
  --freeze-request /evaluator/gate/freeze-request.v1.json \
  --suite-config /evaluator/gate/scienceagentbench.acceptance.v1.json \
  --suite-config /evaluator/gate/corebench.acceptance.v1.json \
  --suite-config /evaluator/gate/discoveryworld.acceptance.v1.json \
  --suite-config /evaluator/gate/private-prospective.acceptance.v1.json \
  --output /evaluator/gate/frontier-gate.acceptance.v1.json
```

A formal freeze requires exactly the four listed tracks, one evaluator, and identical four-arm
system identities. Freeze must precede every test result's start time.

## 4. Bind and spend private access

Only after the program config exists should the custody owner freeze a `PrivateSuiteManifest` with
its `acceptance_config_sha256`. Register, authorize, materialize, run, and close it through
[`manage_private_suite.py`](../../scripts/manage_private_suite.py) and the procedure in
[`PRIVATE_SUITE_CUSTODY.md`](PRIVATE_SUITE_CUSTODY.md).

Do not issue a final report while plaintext access is open. A passing custody state requires:

- the exact acceptance config, evaluation suite, evaluator, and private test matrix;
- one unique registration and open event after acceptance freeze;
- at least one valid two-person authorization;
- one verified materialization receipt and no materialization failure;
- zero contamination reports;
- test execution after open;
- one close and verified cleanup receipt after execution;
- final suite retirement.

## 5. Build the non-secret evidence index

Paths may be absolute or relative to the index file. Environment-variable names are allowed; key
bytes are not.

```json
{
  "tracks": {
    "scienceagentbench": {
      "matrix": "matrices/scienceagentbench.test.json",
      "suite_bundle": "suites/scienceagentbench.test.json",
      "result": "runs/scienceagentbench.result.json",
      "ledger": "ledgers/scienceagentbench.jsonl",
      "receipt_key_env": ["evaluator-key-v1=FRONTIER_SCORER_KEY_B64"]
    },
    "corebench": {
      "matrix": "matrices/corebench.test.json",
      "suite_bundle": "suites/corebench.test.json",
      "result": "runs/corebench.result.json",
      "ledger": "ledgers/corebench.jsonl",
      "receipt_key_env": ["evaluator-key-v1=FRONTIER_SCORER_KEY_B64"]
    },
    "discoveryworld": {
      "matrix": "matrices/discoveryworld.test.json",
      "suite_bundle": "suites/discoveryworld.test.json",
      "result": "runs/discoveryworld.result.json",
      "ledger": "ledgers/discoveryworld.jsonl",
      "receipt_key_env": ["evaluator-key-v1=FRONTIER_SCORER_KEY_B64"]
    },
    "private_prospective": {
      "matrix": "matrices/private.test.json",
      "suite_bundle": "suites/private.test.json",
      "result": "runs/private.result.json",
      "ledger": "ledgers/private-evaluation.jsonl",
      "receipt_key_env": ["private-key-v1=PRIVATE_SCORER_KEY_B64"]
    }
  },
  "private": {
    "manifest": "custody/private-suite.manifest.json",
    "ledger": "custody/events.jsonl"
  }
}
```

Omitted configured tracks are permitted only so the same command can issue an honest readiness
artifact. They become `BLOCKED` and disable the scientific claim.

## 6. Generate immutable artifacts

```bash
conda run -n aletheia python scripts/run_frontier_gate.py report \
  --config /evaluator/gate/frontier-gate.acceptance.v1.json \
  --evidence-index /evaluator/gate/evidence-index.v1.json \
  --output-json /evaluator/report/frontier_gate_report.json \
  --output-markdown /evaluator/report/frontier_gate_report.md \
  --output-svg /evaluator/report/frontier_gate_report.svg
```

All three output paths are checked before evidence aggregation and are created with mode `0600`.
The command refuses an existing file or symlink. Archive the JSON, Markdown, SVG, acceptance
config, matrices, results, ledgers, custody manifest, approval evidence, receipt-key versions, and
ledger heads according to evaluator policy.

## Reading a report

The JSON is authoritative. It includes:

- the exact acceptance-config and evidence-bundle hashes;
- one decision and audited aggregate per configured track;
- every numerical criterion with expected relation, observed value, status, and evidence hash;
- a complete attempt/manifest/execution/submission/scorer receipt index;
- reliability, invalidity, retry, intervention, contamination, and cost decomposition;
- private custody state, receipt hashes, event times, and criteria;
- missing tracks and limitations;
- `scientific_claim_allowed`, which is true only for an overall `PASS`.

The Markdown is a review view, and the SVG plots observed pass@1, valid fraction, and mean cost
against their calibrated thresholds. Editing either view cannot change the JSON decision.

## Verdict matrix

| Evidence state | Verdict | Claim allowed |
|---|---:|---:|
| All configured raw evidence and custody complete; every criterion meets threshold | `PASS` | yes |
| Complete authenticated evidence; one or more measured criteria miss | `FAIL` | no |
| Any configured track or required custody evidence absent | `BLOCKED` | no |
| Supplied ledger, result, manifest, or scorer signature is inconsistent | command rejects evidence | no |

Synthetic fixtures, pilot tiers, disclosed non-comparability, or public-only runs cannot be cited
as a formal autonomous-frontier-scientist result.
