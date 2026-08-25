# ADR 0006: Two-container hidden-rule evaluation for DiscoveryWorld

- Status: Accepted
- Date: 2026-08-14
- Scope: implementation issue 8 / third F7-S3 public adapter

## Decision

Aletheia adapts the official DiscoveryWorld `Combinatorial Chemistry / Easy` task at commit
`fd591323920be0d3786ef350955de1945aa571e5` as a four-instance public-validation mini-suite. The
task has an objective finite explanation space: exactly one of pure Substance A, B, C, or D removes
rust. Official world seeds 0–3 cover all four governing substances. These are public diagnostics,
not private Frontier Gate tasks, because the upstream source contains the parametric answers.

Each attempt uses two different immutable, offline Docker images. The candidate image contains
only Python's standard library and is rejected if either an installed DiscoveryWorld distribution
or an importable `discoveryworld` module is present. The environment image contains the official
package and assets. Before suite freeze, the evaluator checks the archive identity, retained source
hashes, installed runtime-code hashes, package version, and code/art license contract.

Candidate and environment communicate through a bounded file protocol:

```text
candidate container                         trusted environment container
  read-only /observations  <-------------   writes public observations
  writable  /actions       ------------->   reads structured actions + beliefs
  no source, seed, rule, scorecard           hidden contract + server + receipt only here
```

The environment owns the authoritative trace. Every step binds sequence, submitted action digest,
pre/post observation digests, official world-step counters, action validity, reported beliefs,
hypothesis-note digest, objective experiment outcome, and remaining finite hypothesis set. The
candidate cannot submit or edit a score or trace. Symlink-safe bounded reads, one-way mounts,
read-only roots, no network, resource limits, scoped container cleanup, and evaluator-only receipt
storage are mandatory.

An action envelope must report a normalized probability for every hypothesis. Pure-substance
tests are detected from successful official `USE` and `PUT` transitions, not from candidate prose.
The adapter computes objective finite-set entropy, information gain per world action, distinct and
redundant trials, belief revisions after falsification, and grounded versus ungrounded belief
updates. The official task's procedural and terminal values remain authoritative.

Scientific success requires all of the following in every retained run:

1. candidate reaches an evaluator-validated terminal `stop` within its execution budget;
2. official task reports `completedSuccessfully`;
3. candidate sends the explicit terminal `stop` envelope; and
4. its structured final hypothesis exactly equals the evaluator-only governing rule.

The policy runs at least twice from fresh identical worlds. Exact evaluator-owned trace identity
and terminal metrics must agree; no best-of-N result is selected.

## Consequences

- Merely manipulating the world, getting partial official score, or solving by luck without the
  right explanatory rule is scientific false.
- A correct terminal task plus an exact explicit rule is scientific true, with trajectory metrics
  retained separately from the Boolean verdict.
- Missing submission, contamination, malformed bridge protocol, resource exhaustion, and unequal
  repeated traces are invalid, so no scientific conclusion is drawn.
- The final terminal observation permits exactly one structured `stop`; further world actions are
  a protocol breach. This makes task completion and explicit explanation jointly representable.
- A trusted process atomically writes and fsyncs its terminal receipt in an evaluator-only mount.
  The host watches that path and explicitly removes the one-shot container, avoiding dependence on
  Docker client exit notification. A candidate cannot mount or forge the receipt. Its validated
  `stop` receipt also terminates the interactive candidate because no post-stop work is legal;
  all pre-terminal resource limits remain enforced.
- Upstream compressed whole-world history is disabled because only the optional knowledge scorer
  consumes it. Official dynamics and task scoring are retained, while the bounded structured
  action trace becomes the authoritative history for this adapter.
- Teleport actions remain available because they are part of the official public API. The adapter
  measures discovery and action cost but does not claim that this mini-suite tests realistic
  laboratory manipulation.
- DiscoveryWorld code is Apache-2.0, while bundled PixyMoon art has separate project-use,
  attribution, modification, and no-resale terms. Aletheia downloads the fixed upstream archive
  while building the evaluator image and does not vendor or redistribute those assets in a suite.
- Public source spoilers and likely model overlap prevent this suite from satisfying the private
  Frontier Scientist Gate. It is an engineering and behavioral validation instrument only.
