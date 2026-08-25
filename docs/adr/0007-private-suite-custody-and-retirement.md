# ADR 0007: One-time custody and retirement for private prospective suites

- Status: Accepted
- Date: 2026-08-14
- Scope: F7-S4 / implementation issue 10

## Context

Public benchmarks are useful diagnostics but cannot establish a Frontier Gate result when their
tasks or answers may have entered model training, tuning, agent development, or prompt iteration.
A private suite is useful only if the research loop cannot inspect or repeatedly spend it, the
evaluator can prove which frozen configuration was tested, and any suspected disclosure stops
further scoring.

The protected actors are deliberately separate:

- the **research principal** develops Aletheia and may see validation analogs, but never private
  task manifests, hidden observations, gold evidence, keys, or custody controls;
- the **custody owner** controls suite registration and one approval artifact;
- an **independent auditor** supplies the second approval artifact and archives ledger heads;
- the **evaluator operator** runs the frozen matrix and scorer outside the research sandbox;
- independent **domain reviewers** approve gold evidence and acceptable conclusion boundaries;
- an external object store and KMS hold ciphertext and key material.

The local threat model includes accidental disclosure, curious or compromised research code,
path/symlink replacement, best-of-N expansion, altered acceptance settings, repeated unlocks,
concurrent operators, scorer canaries, declared contamination, interrupted materialization, and
ledger edits. It does not claim to withstand compromise of the evaluator OS, both approvers, the
KMS, or every external audit archive simultaneously.

## Decision

### Frozen prospective manifest

`PrivateSuiteManifest` is the non-secret evaluator custody index. It binds the encrypted evaluation
suite, evaluator identity, test-phase four-arm matrix, acceptance configuration, three distinct
organizational principals, and every task custody record.

A formal `frontier_gate` suite must contain 10–20 unpublished tasks, at least two domains, and all
six scientific cases: true effect, null effect, confounding, label error, distribution shift, and
insufficient sample. Each task binds provenance and license terms, a domain review, a prospective
cutoff and contamination assessment, a structurally related but content-distinct validation
analog, and a scheduled retirement time. Critical or materially overlapping tasks cannot freeze.

The suite schema is an engineering gate, not evidence that real tasks have been commissioned or
that Aletheia passes them.

### Encrypted assets and least privilege

The custody index contains content identities and opaque `custody://` storage references, never
keys or private plaintext. Suite manifests, task manifests, hidden assets, and gold evidence use
role-scoped stores. For each task, the task manifest, hidden asset, and gold evidence require three
different key identities; task and hidden assets also require distinct access-policy identities.
Key IDs themselves are hashed so provider account and key names do not enter the registry.

The external operator factory resolves ciphertext and KMS credentials at execution time. Aletheia
verifies ciphertext length/hash before spending access, then verifies every decrypted length/hash,
suite/task identity, hidden-asset identity, gold-review identity, run-plan binding, and per-plan
access limit. Gold evidence is checked in memory and never materialized for the runner. Evaluator
plaintext is written as new files only, mode `0400`, below mode-`0700` evaluator directories.

This maps to NIST guidance on protecting key metadata, access control, authentication, and key
inventory, and to least-privilege and separation-of-duties controls:

- [NIST SP 800-57 Part 1 Rev. 5](https://csrc.nist.gov/News/2020/NIST-Publishes-SP-800-57-Pt-1-Revision-5)
- [NIST SP 800-53 Rev. 5.1, AC-5 and AC-6](https://csrc.nist.gov/CSRC/media/Projects/risk-management/800-53%20Downloads/800-53r5/SP_800-53_v5_1-derived-OSCAL.pdf)

### Bounded two-person authorization and one-time open

An authorization binds exactly the private manifest, evaluation suite, evaluator, baseline matrix,
acceptance configuration, and allowed derived run plans. Its custody and independent principals
must match the frozen roles; neither may be the research principal. It also binds two different
external approval-evidence hashes. The authorization cannot predate suite freeze and cannot exceed
the manifest's TTL (72 hours by default, hard maximum seven days).

Approval-evidence hashes are not digital-signature verification. The custody deployment must
authenticate both external approval artifacts before creating the authorization. This ADR does not
pretend that writing two identity strings proves two humans acted.

`PrivateCustodyLedger.open_access` uses one exclusive file lock around validation and append. One
concurrent caller can win; every later caller fails. No timestamp override is exposed by the
operator CLI. Once open, a decryption, validation, or write failure records a terminal
`materialization_failed` event and retires the suite. A ciphertext preflight failure occurs before
open and therefore does not consume the one-time access.

### Independent run guard and contamination

Every task with `retire_after_access=true` is rejected by `IndependentEvaluationRunner` unless an
active `PrivateSuiteAccessGuard` is present. The guard checks ledger integrity and the exact
suite/evaluator/run-plan/task authorization before claiming an attempt and again immediately before
hidden scoring. A concurrent retirement therefore produces a contamination-invalid result without
calling the hidden scorer.

Candidate-declared overlap skips hidden scoring. A trusted scorer can also return a contamination
canary. Both paths append a content-addressed report, retire the whole suite version, verify and
remove all materialized private plaintext, and
append a cleanup receipt before closing the one-time access. Subsequent attempts fail before a new
ledger claim.

Development disclosure can be reported before authorization; it immediately makes the version
ineligible to open. Explicit retirement requires the frozen two principals and two independent
approval-evidence identities.

### Audit and recovery

Custody events are append-only, sequenced, hash chained, file locked, flushed, and `fsync`ed. The
status command emits the current head and whole-file hash for independent anchoring. The ledger
does not make a malicious storage owner unable to rewrite the entire chain; tamper evidence depends
on the independent auditor retaining earlier heads outside that owner's account.

Materialization tracks each file it creates. An interrupted write removes all completed plaintext
files before recording failure. Normal close and contamination cleanup first inventory the exact
suite/task identities plus the suite-scoped hidden directory, reject modified, unexpected, special,
or symlinked content, remove verified files, and then record a content-addressed cleanup receipt.
Cleanup tolerates already-absent files so an interruption after unlink but before ledger append can
be safely retried. If the materializer is killed after the open event, the operator records external
crash evidence through the dedicated failure-recovery command; it terminally marks materialization
failed, verifies and removes any files left in the previously absent suite scopes, and closes the
access. It never resumes or reopens that suite version.

If the crash occurs after the ledger commits `suite_materialized` but before the CLI writes its
external receipt, recovery takes the opposite path: verify every staged suite/task/hidden file
against custody, recover the committed receipt from the ledger, and continue through the original
open access. It performs no KMS call and cannot create a second materialization event.

The overall design follows NIST AI RMF's call for documented lifecycle governance, safe
decommissioning, rigorous repeatable TEVV, and assessors independent of front-line developers:
[NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/). The motivation for
keeping test data private follows the contamination problem and private-evaluation model described
by [TRUCE](https://arxiv.org/abs/2403.00393); Aletheia does not claim TRUCE's confidential-computing
or cryptographic protocol.

## Consequences

- Public validation and private test are structurally linked but content-distinct and have separate
  lifecycle states.
- Test configuration cannot change between authorization and scoring without changing a bound
  hash and failing closed.
- One failed post-open materialization spends and retires the suite; operational rehearsal must use
  a separate pilot suite and validation analogs.
- A contamination signal favors loss of a test suite over a potentially biased score.
- Plaintext cleanup is exact and auditable, but secure physical-media erasure remains the storage
  platform's responsibility; encrypted ciphertext follows the retention policy.
- The code supplies custody mechanics and synthetic contract tests. It does not supply real private
  tasks, an external KMS implementation, cryptographic approver signatures, an external ledger
  anchor, real model runs, or Frontier Gate acceptance thresholds.

## Rejected alternatives

- **A private JSON file beside the research repository:** too easy for development tools and model
  context to ingest and offers no one-time control.
- **Encrypt only answers:** task wording and labels can leak enough information to tune behavior;
  suite, task, hidden, and gold roles remain separate.
- **Authorize a mutable command or model alias:** cannot attribute a result to the frozen matrix.
- **Delete ledger history after use:** destroys attempt and contamination auditability; only verified
  plaintext is disposed.
- **Treat hash chaining as authorization or a signature:** a chain detects changes relative to an
  independently retained head but does not authenticate a malicious ledger owner by itself.
