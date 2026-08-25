# Asta CORE-Bench-Hard adapter runbook

## What this adapter is

This is a frozen, two-capsule **public validation** suite for scientific repository reproduction.
It is based on AstaBench v0.3.1's `CORE-Bench-Hard` wrapper and the original CORE-Bench public train
annotation. It verifies numerical answers and an actual reproduction artifact across two fresh,
offline runs. It is not the encrypted test split and not an official leaderboard run.

Default tasks:

| Capsule | Work | Code | Data | Reviewed runtime |
|---|---|---|---|---|
| `capsule-6460826` | CULP classification | MIT | CC0-1.0 | NumPy, pandas, scikit-learn, NetworkX |
| `capsule-0940461` | fuzzy gradient boosting notebook | MIT | CC0-1.0 | NumPy, pandas, scikit-learn, Seaborn, Jupyter, nbconvert |

Upstream assets remain operator-supplied. The prepared bundle records
`capsule_assets_redistributable=false` and does not copy the original capsule archives.

## Frozen upstream identities

- AstaBench tag/commit: `v0.3.1` /
  `5c844b7451e3a98cd0df71ea626bb217803d2bed`.
- Asta CORE wrapper SHA-256:
  `4aed0cd36bd6c48bc352bc32bd5b720e6dccbae0438b386e71218015bc4229b3`.
- `inspect_evals` commit: `c2bec9ebee7a5995512bf5ff67a2e82afe4d12e1`.
- Original scorer SHA-256:
  `51652922721d62e9f333106f0c015fcd979b20e5cec92895dc364545ff770da3`.
- Hugging Face dataset revision:
  `18ac8edf2532d9edb9d13ae71f715410de6ee5a0`.
- `core_train.json` SHA-256:
  `3df47f1b3fa1cb60045018eb1a0f1ad4ecf6a53f72318c845a879ce0313b0730`.
- Capsule archive SHA-256s: `6460826` =
  `36e6fe89a288dc66a167c055bd21965cdbd053f87217d6fc9e00642af0445664`;
  `0940461` =
  `4d1ca989f9c597a7ec9f5f0545f7c26943c324426d2f98ff8e54787a696cab31`.

## Acquire assets

Download the public `core_train.json` at the exact dataset revision and these two official capsule
archives from `https://corebench.cs.princeton.edu/capsules/{capsule_id}.tar.gz`. Verify hashes
before preparation. Put the archives in one operator-owned directory with names:

```text
capsule-6460826.tar.gz
capsule-0940461.tar.gz
```

Do not acquire or decrypt `core_test.json.gpg` for adapter development.

## Build the reviewed offline image

```bash
docker build -t aletheia-evaluator-agent:latest -f docker/evaluator-agent.Dockerfile .
docker build -t aletheia-corebench:latest -f docker/corebench.Dockerfile .
```

The preparation command resolves both tags to immutable image IDs and probes installed versions.
The candidate image contains no benchmark assets or Aletheia/evaluator code.

## Prepare the suite

Use an output root visible to the Docker daemon (normally below the repository workspace):

```bash
PYTHONPATH=. conda run -n aletheia python scripts/prepare_corebench_suite.py \
  --annotation /operator/core-bench/core_train.json \
  --capsule-root /operator/core-bench/capsules \
  --output-root workspaces/evaluator/corebench-validation-v1
```

Preparation performs no model call and does not execute a capsule. It emits:

```text
corebench_suite.v1.json
public_assets/corebench/<source-hash>/<capsule>.tar.gz
hidden_assets/corebench/<source-hash>/<capsule>.json
scratch/
```

Public archives retain code/data/license files but remove upstream results, convenience
reproduction/environment instructions, and pre-existing reproduction output. Hidden receipts hold
exact gold values and must use evaluator-only permissions. Never expose the suite JSON's
`asset_receipts` or `hidden_assets/` directory to a research process; the runner passes only each
task's public view and sanitized capsule.

## Submission and verdicts

Submit artifact kind `reproduction_program`, media type `text/x-python`. When the scorer executes
it, the working directory contains `capsule/`. The program must create:

```text
capsule/report.json
capsule/reproduction_artifacts/<at least one regular file>
```

Verdict mapping:

- scientific true: every official question correct, tangible artifact present, both runs exact;
- scientific false: valid attempt but wrong numbers, missing generated report/artifact, or process
  error;
- invalid contamination: declared overlap or reference to hidden results/scorer/annotation assets;
- invalid non-reproducible: report, artifact tree, or objective result differs across runs;
- invalid resource limit: wall/CPU/memory/output budget exceeded;
- infrastructure failure: only evaluator-owned Docker, asset, or trusted-scorer failure.

The public validation answers may be present in model training data. Every campaign must disclose
overlap, and this suite cannot replace a private prospective test.

## Verification

```bash
conda run -n aletheia python -m pytest \
  tests/evals/test_public_asset_staging.py \
  tests/evals/adapters/test_corebench_contract.py \
  tests/evals/adapters/test_corebench_entrypoint.py \
  tests/evals/adapters/test_corebench_scoring.py

conda run -n aletheia python -m pytest -m docker \
  tests/evals/adapters/test_corebench_docker.py \
  tests/evals/adapters/test_prepare_corebench_suite.py
```
