# F7 implementation issue 6: ScienceAgentBench adapter

Date: 2026-08-14

## Outcome

Implementation issue 6 is engineering-complete. Aletheia can validate the pinned verified
ScienceAgentBench release, freeze a four-domain CC-BY mini-suite, run submitted programs and
official objective evaluators across separate hard Docker planes, reproduce every result twice,
and emit content-addressed evidence into the independent F7 runner.

This completes the first of the three public adapters in F7-S3. It does not complete F7-S3 as a
whole: CORE/Asta reproduction and DiscoveryWorld remain issues 7 and 8. It is also not an official
ScienceAgentBench leaderboard result; no model campaign or official-data task was run in this
implementation slice.

## Frozen release and subset

- Official repository commit:
  `c26e151ed601ba109dc4d35e057ff8e73fec469d`.
- Verified Hugging Face revision:
  `9c6e96c9e74572e979b0930ee735041cef528cb7`.
- Verified CSV: 102 exact rows, SHA-256
  `7f490f17f721a9c7e9415d3608a1a37d1a5315a26862cf556e3096ac4062face`.
- Default CC-BY-4.0 IDs: `16`, `21`, `29`, `40`.
- Original-license IDs `3`, `32`, `46`, `53`, `54`, `84` fail closed without explicit opt-in.
- Unzipped data is marked non-redistributable and never copied by suite preparation.

## Delivered

### Source, license, and asset integrity

- Exact annotation hash, format, column order, row count, unique/contiguous ID set, safe paths, and
  explicit task licenses.
- Archive SHA-256, per-task dataset-root tree hashes, file/byte counts, and exact evaluator-script
  hashes frozen into evaluator-private receipts.
- Atomic hidden-receipt staging and a preparation CLI that emits tasks, suite, source/subset/harness
  manifests, and receipts without copying upstream assets or running a model.
- Custom releases require an explicit per-instance installed-distribution contract rather than an
  inferred generic environment.

### Independent two-plane objective scorer

- Candidate plane: immutable image, no network, no inherited host environment, resource limits,
  only task-local read-only dataset mounts, and a writable ephemeral output root.
- Scorer plane: a separate immutable container with only the exact task data, exact official
  evaluator, and read-only candidate output.
- Gold programs never mounted; CodeBERT similarity excluded from scientific correctness.
- Two mandatory runs; output and score must match; every attempt retained; no best-of-N.
- Submitted program and two complete harness result objects stored as verifiable score evidence and
  appended to the F7 ledger.

### Reviewed scientific runtime

`docker/scienceagentbench.Dockerfile` now pins all direct scientific dependencies. The locally
built arm64 image resolved to:

`sha256:eaf2aa2ef8a71464ff433ccc82810f85158031a9ed01d7e10e24b4152453b06d`

The runtime probe recorded Python 3.11.15, NumPy 2.4.6, pandas 2.3.3, scikit-learn 1.8.0, SciPy
1.17.1, Matplotlib 3.10.9, RDKit 2026.3.4, GeoPandas 1.1.4, and NeuroKit2 0.2.13. The four default
task package contracts all passed.

### Verdict and retry hardening

- Correct output is scientific true; runnable numerical error, missing output, syntax error, and
  authored process failure are scientific false.
- Missing artifact, contamination, non-reproducibility, and resource exhaustion are invalid with
  distinct reasons.
- Token/USD budget overage is rejected before hidden scorer access.
- Candidate exit code 125 cannot manufacture an infrastructure retry: a host-only CID file proves
  whether Docker launched the container.
- At a client deadline, exact-name container inspection distinguishes an authored wall-time overrun
  from a stopped-container Docker client hang; only the latter is retryable infrastructure failure.
- Fractional CPU budgets are normalized to Docker-supported deterministic precision.

## Six acceptance classes

The requested fixture matrix is covered at unit-contract and real-container levels:

1. Correct reference-like program: official objective success.
2. Runnable but numerically wrong program: valid scientific false.
3. Gold/evaluator path probe, canary, or declared overlap: contamination; hidden scorer not called.
4. Missing submitted program/output: respectively invalid missing artifact and scientific false.
5. Different repeated outputs: non-reproducible invalid; no run selection.
6. Timeout/resource/oversized output: resource-limit invalid; hidden budget overage is pre-scoring.

Additional adversarial checks cover annotation drift, exceptional-license opt-in, traversal,
symlinks, cross-task data access, evaluator/gold invisibility, asset tampering, receipt binding,
environment requirements, evidence hashes, evaluator failure, and authored exit 125.

## Verification

Final closing validation:

- Final full project non-Docker regression: 565 passed, 1 skipped, 18 Docker tests deselected in
  341.67 seconds.
- Final Docker coverage was split after diagnosing a Docker-client closeout incident: 13 independent
  runner/ScienceAgentBench/CLI/training-path tests passed in 9.99 seconds, and the remaining 5 hard
  sandbox adversarial tests passed in 15.19 seconds. Together these cover all 18 Docker-marked tests
  on the final code.
- Before the closeout classification fix, one aggregate Docker run saw an existing training
  container write `metrics.json` and log `ALETHEIA_JOB_OK` but the Docker client failed to return by
  its deadline. It left no Aletheia container running and passed alone in 2.44 seconds. The runner
  now classifies this stopped-container client hang as infrastructure failure rather than authored
  wall-time exhaustion.
- Dedicated image environment probe and all four default task package contracts: passed.
- Ruff on touched runtime/evaluation/test files and `git diff --check`: passed.

Post-closeout shared-runner hardening from issue 8 changed the trusted scorer's final write to an
atomic, fsynced evaluator-only receipt followed by immediate one-shot exit. The host now treats
only that candidate-inaccessible receipt as the terminal scorer handshake; candidate outputs do
not receive this privilege. The final aggregate project matrix after this change passed all 29
Docker tests and 622 non-Docker tests with 1 skip.

## Operational limitation and next issue

The official password-protected archive is not present in evaluator storage. On 2026-08-14 the
official SharePoint link redirected correctly but returned HTTP 401 to this non-interactive
environment. Therefore the adapter was proven with a synthetic task that uses the same official
evaluator interface and real two-container isolation, while the pinned official annotation was
validated in full. An operator must supply `benchmark_verified.zip` before a real four-task run;
using an older unverified archive is prohibited.

The next implementation issue is 7: a minimal CORE-Bench or Asta Core-Bench-Hard reproduction
adapter with numerical and artifact-level reproduction receipts.
