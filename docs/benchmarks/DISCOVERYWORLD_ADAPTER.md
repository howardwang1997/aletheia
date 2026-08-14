# DiscoveryWorld hidden-rule adapter runbook

## What this adapter is

This is a four-instance public-validation suite for interactive scientific discovery. A submitted
Python policy sees only official text observations, chooses official JSON actions, and reports an
explicit belief distribution over four candidate rust-removal rules. A separate trusted container
runs DiscoveryWorld, detects controlled trials, owns the complete action trace, and reads the
official task scorecard. Each policy is run twice from a fresh identical world.

The frozen task is `Combinatorial Chemistry / Easy` (`RustedKeyTaskEasy`). It tests whether a policy
can manipulate an apparatus, run informative pure-substance tests, revise a wrong hypothesis,
state the governing rule, derust the key, open the shed, and leave. It is not an official
DiscoveryWorld leaderboard result and is not a private Frontier Gate.

## Frozen upstream identity and licenses

- Repository: [allenai/discoveryworld](https://github.com/allenai/discoveryworld).
- Commit: `fd591323920be0d3786ef350955de1945aa571e5`.
- Package version: `0.0.2`.
- Source archive: 29,491,760 bytes; SHA-256
  `0ef5f45566807083754aa140e5653b9e8260434fc71d977591598b6625e619b1`.
- `DiscoveryWorldAPI.py`: SHA-256
  `c455e32ddb5e676a83b7b3e349dda8262473ca54fec217650497a73603d46dc8`.
- `ScenarioMaker.py`: SHA-256
  `1b1055e765b98e5a0dab94f4a31c9ac0f627eb9f52ca5a63f9c4140c3afdbd06`.
- `TaskScorer.py`: SHA-256
  `32755603cc4ce0a706943047e1f9ab0e031eb0d0992b9feaabb226b1b35fd79e`.
- `scenarios/storage_shed.py`: SHA-256
  `f88ac019b7867fac92dc4cf8d8fc61331bdd9df59577a463d54e1c3c75b35d2f`.
- Code license: Apache-2.0. The upstream README states that this does not cover the artwork.
- Art assets: separate PixyMoon project-use/attribution/modification/no-resale terms. The fixed
  archive is downloaded only while building the trusted image. Prepared suites contain neither
  upstream source nor art assets.

See the [frozen upstream README](https://github.com/allenai/discoveryworld/blob/fd591323920be0d3786ef350955de1945aa571e5/README.md)
and the [DiscoveryWorld paper](https://arxiv.org/abs/2406.06769) for upstream scope and design.

## Build the two images

Both Dockerfiles pin the same immutable Python base digest, but their contents and final image IDs
are deliberately different:

```bash
docker build -f docker/discoveryworld-candidate.Dockerfile \
  -t aletheia-discoveryworld-candidate:latest .

docker build -f docker/discoveryworld.Dockerfile \
  -t aletheia-discoveryworld:latest .
```

The candidate image must probe as Python present, DiscoveryWorld distribution absent,
DiscoveryWorld import path absent, and Aletheia source absent. The trusted probe verifies both the
retained source tree and the files imported from the installed package. Suite preparation resolves
mutable tags to immutable local image IDs.

## Freeze a suite

Use an output root visible to the Docker daemon:

```bash
PYTHONPATH=. conda run -n aletheia python scripts/prepare_discoveryworld_suite.py \
  --output-root workspaces/evaluator/discoveryworld-public-v1
```

The default selection is four opaque instance IDs bound evaluator-side to official world seeds
0–3. Custom entries use `--instance INSTANCE_ID=WORLD_SEED`; only official seeds 0–4 are accepted,
and IDs/seeds must be unique. Selection must be predeclared—do not run many seeds and retain the
easiest.

Preparation performs no model call. It emits:

```text
discoveryworld_suite.v1.json
hidden_assets/discoveryworld/<source-manifest-sha256>/<instance-id>.json
scratch/
```

The bundle includes public task views and hidden-asset hashes, but does not embed hidden receipts.
The receipt files contain seeds, exact governing-rule IDs, and official observation/action hashes;
keep the whole `hidden_assets/` tree evaluator-only.

## Candidate protocol

Submit artifact kind `agent_program`, media type `text/x-python`. The policy runs with only the
standard library and these environment variables:

```text
DISCOVERYWORLD_PROTOCOL
DISCOVERYWORLD_OBSERVATIONS_DIR
DISCOVERYWORLD_ACTIONS_DIR
ALETHEIA_EVAL_SEED=0
PYTHONHASHSEED=0
```

For `observation_0007.json`, atomically create `action_0007.json`. Every action reports beliefs
over exactly `substance_a`, `substance_b`, `substance_c`, and `substance_d`:

```json
{
  "schema_version": 1,
  "sequence": 7,
  "kind": "act",
  "world_action": {"action": "USE", "arg1": 123, "arg2": 456},
  "beliefs": {
    "substance_a": 0.1,
    "substance_b": 0.7,
    "substance_c": 0.1,
    "substance_d": 0.1
  },
  "hypothesis_note": "Test pure B in a clean jar."
}
```

Probabilities must be finite, within `[0,1]`, and sum to one. Extra envelope fields, nested action
arguments, mismatched sequences, oversized files, links, and non-finite JSON fail closed. After a
terminal observation, submit only:

```json
{
  "schema_version": 1,
  "sequence": 19,
  "kind": "stop",
  "final_hypothesis_id": "substance_b",
  "beliefs": {
    "substance_a": 0.0,
    "substance_b": 1.0,
    "substance_c": 0.0,
    "substance_d": 0.0
  },
  "hypothesis_note": "A controlled positive trial supports the final rule."
}
```

The example illustrates syntax only; policies must infer the instance rule from observations and
experiments rather than reuse an answer.

`stop` is the terminal protocol commit. Once the hidden environment validates it and atomically
writes the evaluator-only episode receipt, the host removes both one-shot containers; candidate
code cannot see or write that receipt, and no post-stop action or computation is accepted. Wall,
CPU, memory, and action-wait limits remain authoritative until the validated receipt exists.

## Scores and verdicts

The trusted environment retains official completion and normalized procedural score. Aletheia
also records:

- valid-action rate;
- pure trials, distinct hypotheses tested, and redundant trials;
- objective initial/final entropy and information gain;
- information gain per world action;
- reported belief entropy;
- hypothesis changes and successful revisions after a falsifying result; and
- grounded versus ungrounded belief updates.

Verdict mapping:

- scientific true: official successful completion, explicit stop, exact rule, and identical runs;
- scientific false: runnable but incomplete, wrong rule, no explicit stop, or lucky completion
  without the governing explanation;
- invalid contamination: declared overlap, canary, or recognized source/scorecard/oracle reference;
- invalid missing artifact: no submitted policy program;
- invalid non-reproducible: exact evaluator-owned trace or terminal result differs across runs;
- invalid protocol breach: malformed bridge envelope or action after a terminal observation;
- invalid resource limit: program size, wall time, CPU, or memory limit; and
- infrastructure failure: only evaluator-owned Docker, frozen-source, hidden-contract, or trusted
  environment failure.

All run receipts are retained. There is no best-of-two selection.

## Verification

```bash
conda run -n aletheia pytest -q \
  tests/evals/adapters/test_discoveryworld_contract.py \
  tests/evals/adapters/test_discoveryworld_entrypoint.py \
  tests/evals/adapters/test_discoveryworld_scoring.py

conda run -n aletheia pytest -q \
  tests/evals/adapters/test_discoveryworld_docker.py \
  tests/evals/adapters/test_prepare_discoveryworld_suite.py
```

The Docker group includes a neutral-policy isolation probe, a complete systematic scientist that
discovers the rule through controlled trials and exits the shed in two identical runs, a randomized
trace rejected as non-reproducible, and a sanitized suite-freeze check.

Closeout evidence on 2026-08-14: 28 focused non-Docker tests passed; the real DiscoveryWorld group
passed 4/4; the complete project passed 622 non-Docker tests with 1 skip and all 29 Docker tests.
The four-instance frozen bundle SHA-256 is
`bf0b74ed4bad8277e2a669b43b34e3a28433bf32c78d12de768bedb7dd81f5d6`.

## Interpretation limit

The upstream source publishes the seed-to-rule implementation, and public models may have seen it.
Static contamination checks and an empty candidate image prevent direct runtime access but cannot
prove absence from model weights. Report this suite as public validation only. F7 still needs the
private prospective suite, predeclared baseline matrix, and frozen acceptance report.
