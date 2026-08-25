# ScienceAgentBench adapter runbook

This adapter prepares and scores a frozen four-task mini-suite from the verified
ScienceAgentBench release. It is an evaluator-owned F7 benchmark path, not a training data loader
and not a leaderboard submission client.

## Frozen official inputs

- Repository: `OSU-NLP-Group/ScienceAgentBench`, commit
  `c26e151ed601ba109dc4d35e057ff8e73fec469d`.
- Annotation: Hugging Face dataset `osunlp/ScienceAgentBench`, verified split, revision
  `9c6e96c9e74572e979b0930ee735041cef528cb7`.
- Preferred annotation: verified CSV, 102 rows, SHA-256
  `7f490f17f721a9c7e9415d3608a1a37d1a5315a26862cf556e3096ac4062face`.
- Default mini-suite IDs: `16`, `21`, `29`, and `40`. They cover computational chemistry, GIS,
  physiological-signal analysis, and bioinformatics/ML. Their reviewed runtime requirements are
  respectively RDKit, GeoPandas, NeuroKit2, and scikit-learn.
- Default task license: CC-BY-4.0. IDs `3`, `32`, `46`, `53`, `54`, and `84` retain their original
  upstream licenses and are rejected without explicit adapter-level opt-in.

The upstream authors prohibit redistributing unzipped benchmark data. Keep
`benchmark_verified.zip` and its extracted `benchmark/` tree in evaluator-owned storage. The
preparation command records their hashes and file statistics but never copies them into the suite
bundle.

## Build the reviewed runtime

Build the evaluator research-plane image first, then the benchmark runtime:

```bash
docker build -f docker/evaluator-agent.Dockerfile -t aletheia-evaluator-agent:latest .
docker build -f docker/scienceagentbench.Dockerfile -t aletheia-scienceagentbench:latest .
```

The adapter probes the actual package versions and resolves both mutable tags to immutable image
IDs before it creates any suite. Suite construction fails if a selected task lacks a reviewed
package contract or its required package is absent.

## Prepare the frozen suite

Download the verified public annotation from the pinned dataset revision. Separately obtain the
official password-protected `benchmark_verified.zip` from the upstream repository instructions,
then extract it only in evaluator-owned storage. Run:

```bash
conda run -n aletheia python scripts/prepare_scienceagentbench_suite.py \
  --annotation /evaluator/source/scienceagentbench-verified.csv \
  --benchmark-root /evaluator/source/benchmark \
  --benchmark-archive /evaluator/source/benchmark_verified.zip \
  --output-root /evaluator/suites/scienceagentbench-v1
```

The command verifies the exact annotation hash/schema/row set, explicit licenses, archive hash,
per-task dataset tree hashes, evaluator script hashes, image IDs, and installed package versions.
It emits `scienceagentbench_suite.v1.json`, evaluator-private hidden receipts, task manifests, and a
suite manifest. It does not execute a research model and does not copy benchmark datasets or
evaluator scripts.

Custom source manifests are intended for fixture development or separately reviewed releases.
They must explicitly declare one package contract per selected ID, for example:

```text
--required-distribution 1=scikit-learn,numpy
```

## Scoring boundary

Each frozen run uses two separate no-network, immutable-image containers:

1. The candidate container sees only its public prompt, submitted program, and the dataset roots
   named by that task. It cannot see evaluator scripts, gold programs, other tasks' datasets, the
   evaluator ledger, or host environment variables.
2. The trusted scorer container sees only that task's exact hashed evaluator, exact hashed data,
   and the candidate output mounted read-only. Gold programs are never mounted.

The scorer executes every candidate twice. Matching outputs and objective scores are required;
there is no best-of-N selection. Official CodeBERT program similarity is deliberately excluded
from the scientific verdict because code resemblance is not output correctness and would require
exposing gold program material. Scientific success requires a valid output and official objective
`success_rate == 1` in every frozen reproduction run.

## Verdict semantics

- Correct objective output: scientific success.
- Runnable but numerically wrong output, missing output, syntax/process error: reproducible
  scientific false.
- Missing submitted program: invalid (`missing_artifact`).
- Gold/evaluator path reference, canary, or declared overlap: invalid (`contamination`) before any
  hidden test access.
- Different repeated output/score: invalid (`non_reproducible`); neither run is selected.
- Timeout, CPU/memory kill, or oversized output: invalid (`resource_limit`).
- Asset/hash/image/scorer failure: evaluator infrastructure failure; only this class can consume a
  pre-registered retry.

Harness receipts, submitted-program hashes, output hashes, evaluator log hashes, image IDs, wall
times, and complete result objects are bound into the common score and append-only evaluation
ledger.

## Current operational limitation

The adapter, specialized image, source annotation validation, synthetic official-evaluator fixture,
and real Docker isolation tests are complete. A real four-task official-data execution still
requires an evaluator operator to supply the upstream `benchmark_verified.zip`; the public
SharePoint link returned HTTP 401 to this non-interactive environment on 2026-08-14. This is an
asset-custody prerequisite, not an adapter implementation gap. Do not substitute the older
unverified benchmark archive.
