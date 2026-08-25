# F7 issue 10 private-suite custody implementation report

Date: 2026-08-14

## Outcome

F7 issue 10 is engineering-complete. Aletheia now has an evaluator-owned protocol for defining,
registering, authorizing, opening exactly once, running, contaminating, cleaning, closing, and
retiring a private prospective evaluation suite.

This is not a scientific pass. The repository contains synthetic contract fixtures, not 10–20
commissioned unpublished scientific tasks. No external KMS has been configured, no real model has
spent a private suite, no external approval signatures or ledger-head archive have been supplied,
and no acceptance threshold has been frozen. Those statements remain explicit because a private
evaluation mechanism is useful only when its evidence labels are honest.

## Delivered

### Prospective custody schema

`aletheia/evals/private_suite.py` adds a versioned, frozen custody model:

- `pilot` and formal `frontier_gate` suite tiers;
- formal enforcement of 10–20 tasks, at least two domains, and coverage of true effect, null
  effect, confounding, label error, distribution shift, and insufficient sample;
- three different principals for custody, independent audit, and research;
- source type, provenance, license/retention terms, human-subjects/ethics evidence, domain reviewer,
  conflict review, acceptable conclusion identity, and gold evidence identity;
- prospective creation cutoff, similarity audit, training-overlap state, contamination risk, and
  scheduled retirement;
- a content-distinct validation analog for every private task;
- rejection of critical/materially overlapping tasks at freeze.

The formal schema guarantees the declared structure; evaluator and domain reviewers still have to
commission and audit real task content.

### Role-separated encrypted assets

`EncryptedAssetEnvelope` stores opaque role-scoped `custody://` references, ciphertext/plaintext
byte counts and hashes, encryption scheme, hashed external key identity, and hashed access-policy
identity. It never accepts key bytes or private plaintext.

Evaluation-suite manifests, task manifests, hidden assets, and gold evidence occupy separate role
stores. Within each task, manifest, hidden asset, and gold evidence require three distinct key
identities; manifest and hidden asset also require separate policy identities. All storage refs are
normalized and globally unique. The custody registry therefore contains enough identity to verify
decryption without containing the protected values.

This design is informed by NIST's emphasis on protecting key metadata and managing access,
authentication, and inventory in
[SP 800-57 Part 1 Rev. 5](https://csrc.nist.gov/News/2020/NIST-Publishes-SP-800-57-Pt-1-Revision-5),
and by the separation-of-duties and least-privilege controls in
[SP 800-53 Rev. 5.1](https://csrc.nist.gov/CSRC/media/Projects/risk-management/800-53%20Downloads/800-53r5/SP_800-53_v5_1-derived-OSCAL.pdf).

### Frozen, bounded authorization

`PrivateSuiteAccessAuthorization` binds the private manifest, decrypted evaluation suite,
evaluator, test-phase baseline matrix, acceptance config, and allowed derived run plans. Formal
suites require exactly four run plans. The custody owner and independent auditor must both match
the frozen roles, neither can be the research principal, and each binds a different externally
authenticated approval artifact.

Authorization cannot precede suite freeze and is limited by the frozen policy: 72 hours by default,
seven days at most. Replacement authorization may renew the window but cannot change the test
configuration. The code binds approval evidence; it does not verify a digital signature or prove
that two humans acted. Production custody must authenticate those two artifacts externally.

### One-time, fail-closed materialization

`PrivateCustodyLedger` is a file-locked, sequenced, hash-chained, flushed and `fsync`ed JSONL audit
log. Registration is immutable per suite ID/version. Access opening validates the authorization,
retirement/contamination state, expiry, and scheduled retirement within one exclusive append lock,
so concurrent unlock calls have exactly one winner.

`materialize_private_suite` performs two phases:

1. verify every ciphertext length/hash before spending access;
2. atomically record the one-time open, decrypt through an external plugin, verify every plaintext
   and manifest binding, and stage evaluator-only files.

It requires a frozen test matrix and exact evaluator/run-plan identities. Decrypted tasks must
exactly cover the suite, retain `retire_after_access`, stay inside the suite hidden path, and respect
their per-plan access limit. Gold evidence is verified in memory but never staged. Suite/task
manifests and hidden files are created with no-overwrite semantics and mode `0400` below evaluator
mode-`0700` directories.

A ciphertext preflight failure does not open the suite. Any failure after open records a terminal
materialization failure and retires the version. Files successfully written before an interruption
are removed before that event is recorded.

### Verified cleanup, contamination, and retirement

Normal close inventories exact suite/task plaintext plus every file in the suite-scoped hidden
directory. It rejects symlinks, special files, unexpected content, and changed hashes. Only after
verified files are unlinked does the ledger record a content-addressed cleanup receipt and close
the one-time access. Missing files are tolerated for recovery when a process died after unlink but
before the close append. Repeating a completed close returns the existing receipt.

Candidate-declared contamination is terminal before hidden scoring. A trusted scorer may also
return a contamination canary. Both paths append a report, retire the whole suite version, dispose
plaintext, close access, and block the next attempt before it is claimed. A concurrent external
retirement between candidate submission and hidden scoring is detected by the second guard check;
the hidden scorer is skipped and the attempt becomes contamination-invalid.

Development disclosure can retire a suite before authorization. Explicit scheduled, post-use,
contamination, or operator retirement binds two frozen principals and two independent approval
artifacts.

### Runner and operator integration

`IndependentEvaluationRunner` now accepts an evaluator-owned `EvaluationCustodyGuard`. A task with
`retire_after_access=true` cannot run without it. Custody corruption is classified as evaluator
infrastructure failure; retired/contaminated access is a fail-closed invalid result, not a new
scientific sample or retry opportunity.

`scripts/manage_private_suite.py` exposes:

- `validate` — schema validation without ciphertext access;
- `register` — immutable custody registration;
- `authorize` — frozen two-person authorization;
- `materialize` — external store/KMS plugin plus one-time unlock;
- `recover-materialized` — verify staged plaintext and recover a lost post-commit receipt without
  decrypting again;
- `fail-materialization` — terminal recovery and verified cleanup after an opened materializer is
  killed;
- `report-contamination` — report, retire, and clean active plaintext;
- `close` — verified plaintext disposal and access closure;
- `retire` — explicit two-person task/suite retirement;
- `status` — state plus ledger head/file hash for independent anchoring.

The CLI never accepts key bytes. Its explicit `MODULE:CALLABLE` operator resolves workload identity
and KMS access outside the ledger. It exposes no clock override and refuses an existing receipt
path before loading the operator or spending access.

The complete operator procedure is
[`docs/benchmarks/PRIVATE_SUITE_CUSTODY.md`](benchmarks/PRIVATE_SUITE_CUSTODY.md); the security
decision and limitations are in
[`docs/adr/0007-private-suite-custody-and-retirement.md`](adr/0007-private-suite-custody-and-retirement.md).

## Threat-model traceability

| Threat | Control | Adversarial evidence |
|---|---|---|
| Research loop reads prompt/gold | envelopes contain hashes/opaque refs only; evaluator root absent from research boundary | serialized registry contains neither prompt nor answer |
| Key/policy role collapse | three task key identities; manifest/hidden policy separation | schema rejects shared key identity |
| Too-small or cherry-picked formal suite | 10–20, ≥2 domains, all six cases | nine-task formal manifest rejected |
| Config changes at unlock | authorization binds suite/evaluator/matrix/acceptance/plans | wrong acceptance and reordered plans rejected |
| Long-lived or pre-freeze authority | frozen TTL and freeze-time checks | 73-hour authorization rejected under default policy |
| Concurrent/repeated unlock | exclusive lock and immutable open event | two callers produce one winner |
| Corrupt ciphertext | pre-open byte/hash verification | access remains unopened |
| Wrong decrypt/plaintext | post-open verification and terminal failure | access spent and suite retired |
| Partial disk failure | track and unlink every completed new file | first file removed after simulated second-write failure |
| Hard kill after open/write | terminal failure recovery inventories frozen plaintext scope | all simulated orphan plaintext removed and access closed |
| Candidate declares overlap | no hidden scorer; contamination event and cleanup | invalid receipt, suite closed, next claim denied |
| Scorer detects canary | report/retire/cleanup in guard | hidden plaintext removed and access closed |
| External retirement during attempt | guard recheck immediately before scoring | scorer not invoked; contamination-invalid receipt |
| Ledger line edited | per-record hash and chain validation | status/events fail closed |
| Cleanup deletes unrelated data | exact identities and suite-scoped inventory | changed/unexpected/symlink content rejected |

The hash chain is tamper evident only relative to a head retained outside the ledger owner's control.
It is not a signature and cannot by itself stop a privileged owner from rewriting the full file.

## Research alignment

Public benchmark contamination can invalidate comparative conclusions when test data entered model
training or tuning. The [TRUCE paper](https://arxiv.org/abs/2403.00393) motivates keeping test data
private and auditing benchmark quality; Aletheia adopts that operational motivation but does not
claim TRUCE's confidential-computing or cryptographic security protocol.

NIST AI RMF calls for lifecycle governance, safe decommissioning, documented and repeatable TEVV,
uncertainty and benchmark reporting, and assessors independent of front-line developers. The
[AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/) informed the independent
custody roles, provenance/review records, retirement state, and fail-closed incident handling.

## Adversarial verification

The 19 focused tests cover:

- registry redaction and cryptographic-role separation;
- formal task count, domain breadth, and scientific-case coverage;
- bounded two-person authorization and immutable test configuration;
- concurrent one-time unlock;
- complete envelope verification and mode-`0400` staging;
- ciphertext preflight versus post-open decryption failure semantics;
- partial-write cleanup and idempotent normal close;
- preexisting plaintext-scope protection and hard-kill recovery after open/write;
- development disclosure and explicit two-person retirement before access;
- denial of private tasks without an active guard;
- exact plan authorization and normal guarded scoring;
- submission-declared and scorer-canary contamination;
- concurrent external retirement between submission and scoring;
- tamper-evident ledger rejection;
- CLI validation, registration, authorization, external-plugin materialization, status, and close.

Verification after implementation:

- focused private-suite policy: **19 passed**;
- all non-Docker evaluator tests: **156 passed, 22 deselected**;
- complete non-Docker project: **654 passed, 1 skipped, 29 deselected**. The reproducible final
  verification was partitioned into **628 passed, 1 skipped** inside the restricted sandbox plus
  **26 passed** for the existing ESOL/local-database file under controlled network/local-service
  access. An earlier monolithic run reached **646 passed, 1 skipped** and failed only six ESOL
  downloads through the dead macOS proxy at `127.0.0.1:7890`;
- complete real Docker group: **29 passed, 655 deselected**. The first controlled run had one
  transient CORE-Bench candidate timeout (**28 passed**); that exact test immediately passed alone
  in **0.61 s**, and the complete repeat then passed **29/29** in **26.03 s**;
- Ruff check on changed evaluator/CLI/test files: passed;
- Ruff format check on changed Python files, Python compilation, and `git diff --check`: passed.

## Limitations and next issue

The implementation deliberately leaves deployment facts outside source control:

- real unpublished tasks and independent reviewers;
- legal/ethics approval artifacts and actual KMS/object-store policy;
- authenticated approver signatures or identity-provider attestations;
- an independently signed/archived ledger head;
- physical secure deletion guarantees;
- real model runtimes and paid four-arm runs.

Issue 11 must calibrate metrics and numerical thresholds using validation and expert/reference
baselines, freeze a versioned acceptance config before private access, execute the real matrices,
reconcile all attempt and scorer receipts, and produce the immutable Frontier Gate JSON/Markdown
report. Until that happens, Aletheia has an engineering-complete evaluation plane but no evidence
that K2 improves science or that the system meets the autonomous frontier-scientist goal.
