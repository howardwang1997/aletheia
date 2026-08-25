# F7 implementation issue 7: Asta CORE-Bench-Hard reproduction adapter

Date: 2026-08-14

## Outcome

Implementation issue 7 is engineering-complete. Aletheia can freeze a license-audited public
Asta CORE-Bench-Hard validation mini-suite, safely disclose one sanitized scientific repository,
execute a submitted reproduction program twice without network access, score its numerical report
against evaluator-only official reference runs, require a tangible artifact tree, and issue exact
evidence receipts through the independent F7 runner.

This completes the second of three F7-S3 public adapters. DiscoveryWorld remains issue 8. This is
not a CORE-Bench/AstaBench leaderboard result and no model campaign was run in this implementation
slice.

## Frozen release and subset

- AstaBench v0.3.1 commit `5c844b7451e3a98cd0df71ea626bb217803d2bed`.
- Frozen `inspect_evals` submodule commit `c2bec9ebee7a5995512bf5ff67a2e82afe4d12e1`.
- CORE-Bench Hugging Face revision `18ac8edf2532d9edb9d13ae71f715410de6ee5a0`.
- Public `core_train.json`: 45 rows, SHA-256
  `3df47f1b3fa1cb60045018eb1a0f1ad4ecf6a53f72318c845a879ce0313b0730`.
- Development split: Asta `validation` / original CORE-Bench `train`; encrypted test never fetched
  or decrypted.
- Default IDs: `capsule-6460826` and `capsule-0940461`; both code MIT, data CC0-1.0, Python,
  non-GPU, with reviewed package contracts.

## Delivered

### Public scientific repository boundary

The generic F7 runner now accepts content-addressed evaluator-owned public tarballs. The research
view receives only asset ID/hash/counts/mount location. It never receives evaluator storage paths.
Safe extraction accepts normalized directories and regular files only and rejects traversal,
absolute paths, duplicate entries, links/devices, archive bombs, and count/size drift. Every
per-attempt staging action is hash-chained in the evaluator ledger.

### License-aware capsule preparation

The preparer verifies exact annotation and source archive identities, audits MIT and CC0 license
files, hashes upstream Dockerfiles, then creates deterministic public archives. `results/`,
`REPRODUCING.md`, `environment/`, `.DS_Store`, and any existing `reproduction_artifacts/` are
removed. Source capsules are not copied; public sanitized bytes are explicitly marked
non-redistributable pending upstream-asset policy.

The actual two-capsule preparation succeeded. The source manifest freezes the exact upstream
archive byte counts/hashes and the code-license, data-license, environment-Dockerfile, and package
contracts for both defaults; public archive hashes are
`0a19b20de69fa85231b022ffdb6d042fe91f31463feb921ae97bea0c14c8e118` and
`9c82053d9eaac562d54673e80074973fb4e8cdbecf6b6f3cc7746e405c83fc65`.

### Isolated numerical and artifact reproduction

- Candidate: immutable no-network container; one sanitized capsule; writable ephemeral root; no
  gold, scorer, benchmark annotation, other capsule, repository, credentials, or host environment.
- Scorer: separate immutable container; exact read-only candidate report and exact evaluator-only
  three-run gold list.
- Output: valid `report.json` plus at least one regular reproduction artifact.
- Reproducibility: two fresh runs; exact report hash, artifact tree hash/count/bytes, and objective
  score must agree; both receipts retained; no best-of-N.
- Official answer semantics: punctuation-normalized keys, percentages, exact lists,
  case-insensitive strings, written/vision question split, and 95% numerical prediction intervals.

### Reviewed runtime

`docker/corebench.Dockerfile` extends the evaluator-agent public scientific base with NetworkX,
Seaborn, Jupyter, and nbconvert. Local immutable image:

`sha256:e12444843ba4cc3d5b18a2fd1c7650e48090783290cb90ada855bb7c5eebfb7b`

The frozen probe recorded Python 3.11.15, NumPy 2.4.6, pandas 2.3.3, scikit-learn 1.8.0,
NetworkX 3.6.1, Seaborn 0.13.2, Jupyter 1.1.1, and nbconvert 7.17.0.

## Six acceptance classes

1. Correct report plus generated artifact: scientific true.
2. Runnable numerical error: valid scientific false with question accuracy 0.
3. Canary, result/annotation/scorer path reference, or declared overlap: invalid contamination;
   harness not called.
4. Missing submitted program: invalid missing artifact; missing generated report/artifact:
   scientific false.
5. Different report or artifact tree across the two runs: invalid non-reproducible; no selection.
6. Timeout, CPU/memory/oversized program or output: invalid resource limit.

Additional tests cover licenses, annotation/archive drift, traversal, symlink/device rejection,
public-view secrecy, safe expansion bounds, formal-runner staging, score-port semantics, authored
exit 125, evaluator mount separation, public-asset tampering, environment contracts, evaluator-only
probe closeout retry, and evidence hash binding.

## Verification

- Adapter/public-asset unit matrix: 25 passed.
- Real Docker adapter and CLI integration: 6 passed.
- Official two-capsule suite preparation: passed; two task manifests, two sanitized public archives,
  two hidden receipts, and immutable candidate/scorer image identities emitted.
- Final full-project non-Docker regression: 590 passed, 1 skipped, 24 Docker tests deselected in
  271.05 seconds. The focused adapter/public-asset unit matrix passed 25/25.
- Final all-Docker regression: 24 passed, 591 non-Docker tests deselected in 31.14 seconds. A prior
  aggregate run exposed a stopped-container CLI closeout incident; the six affected CORE-Bench
  Docker/CLI tests also passed alone in 6.00 seconds with an evaluator-only, one-retry closeout
  policy. Running-container timeouts and every candidate failure remain non-retryable.
- Ruff on every touched runtime/adapter/test file and `git diff --check`: passed.

Post-closeout shared-runner hardening from issue 8 changed the trusted objective scorer to commit
its evaluator-only result atomically with `fsync` and then exit without optional interpreter
teardown. The host watches only this hidden receipt and explicitly cleans up the one-shot scorer;
candidate reports cannot trigger that path. The final aggregate project matrix after this change
passed all 29 Docker tests and 622 non-Docker tests with 1 skip.

## Limitations and next issue

The two default tasks are both Computer Science/Python and their validation answers are public.
They establish machinery and a reproducibility diagnostic, not contamination-resistant evidence of
frontier scientific ability. No interactive shell agent or benchmark leaderboard run was included;
the submitted program interface is the F7 system-under-test artifact contract. Cross-domain/R
coverage can be added only after license and offline-runtime review.

Next is implementation issue 8: a DiscoveryWorld mini-adapter with hidden governing rules,
objective terminal success, and an evaluator-owned action trace/information-gain contract.
