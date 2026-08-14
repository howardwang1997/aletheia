# F7 implementation issue 8: DiscoveryWorld hidden-rule/action-trace adapter

Date: 2026-08-14

## Outcome

Implementation issue 8 is engineering-complete. Aletheia can freeze four official
DiscoveryWorld hidden-rule instances, run a submitted policy and the official world in distinct
immutable no-network containers, expose only actions and public observations, retain an
evaluator-owned causal action trace, score explicit rule discovery and task completion, quantify
information gain and hypothesis revision, and reject unequal repeated trajectories.

This completes all three engineering adapters in F7-S3: ScienceAgentBench scientific coding,
Asta CORE-Bench-Hard reproduction, and DiscoveryWorld interactive hidden-rule discovery. It does
not complete F7: the baseline matrix, private-suite custody, and final acceptance configuration and
report remain issues 9–11. No production model campaign or official leaderboard submission was run.

## Frozen release and subset

- Official repository commit: `fd591323920be0d3786ef350955de1945aa571e5`.
- Official package version: `0.0.2`.
- Source archive: 29,491,760 bytes; SHA-256
  `0ef5f45566807083754aa140e5653b9e8260434fc71d977591598b6625e619b1`.
- Scenario: `Combinatorial Chemistry`; difficulty: `Easy`; task: `RustedKeyTaskEasy`.
- Default public variations: seeds 0–3 under opaque instance IDs; together they cover pure
  Substance A, B, C, and D as the governing rust-removal rule.
- Code: Apache-2.0. Art: separate PixyMoon project-use/attribution/modification/no-resale terms;
  downloaded at image build and never copied into the prepared suite.
- Intended use: public validation only. The upstream source contains exact parametric rules and is
  explicitly classified as a spoiler source.

## Delivered

### Two-container hidden-world boundary

The candidate image is a pinned Python slim runtime with neither DiscoveryWorld nor Aletheia. The
trusted environment image downloads the exact archive, verifies its hash, installs only the
environment dependencies, and retains source for runtime auditing. A pre-freeze probe rejects a
candidate-side installed distribution or importable module and compares the environment's actual
installed API/scenario/scorer files against the frozen source tree.

Each episode has four disjoint mount classes:

- public protocol and observations: read-only to candidate;
- candidate actions: writable to candidate and read-only to environment;
- hidden seed/rule contract and trusted server: environment only; and
- full episode receipt and scorecard: environment/evaluator only.

Both containers have read-only roots, no network, dropped capabilities, `no-new-privileges`, PID,
file-descriptor, memory, CPU, and wall-time limits. The bridge rejects links, non-regular or
oversized JSON, undeclared fields, bad sequences, non-finite beliefs, and actions after terminal
state. Candidate-controlled exit paths are replaced without following links. Exception paths
signal and then forcibly close only the randomly named trusted container before scratch cleanup.

The one-shot trusted world commits its final JSON receipt atomically and with `fsync`. The shared
container runner watches only this evaluator-owned path and then removes the completed container;
it never applies this rule to candidate-authored files. For the interactive candidate, a validated
environment receipt following its explicit `stop` is the terminal protocol handshake, so there is
no legal post-stop work and the evaluator may clean up the candidate even if Docker loses its exit
notification. Before that handshake, candidate wall/CPU/memory limits and real process exit remain
authoritative. A dedicated real-container test writes a terminal receipt and then sleeps for 30
seconds; the runner observes the trusted commit, terminates it in under a second, and leaves no
container behind.

The adapter disables upstream `World.saveWorldHistory`, whose compressed full-world pickle is used
only by the optional natural-language knowledge scorer. Official world dynamics, actions,
`TaskScorer`, and JSON observations remain intact; the evaluator-owned structured trace is the
bounded authoritative replacement.

### Objective action trace and discovery metrics

The trusted server records every official transition with action, pre/post observation hashes,
world-step counters, validity, beliefs, hypothesis-note hash, controlled-trial outcome, and
objective remaining hypotheses. It identifies pure tests only from successful official dispenser,
jar, key, and cleaner transitions. The candidate cannot author the experiment receipt, task score,
governing rule, or trace.

The scorer retains official completion and procedural score and adds informative/distinct/redundant
trials, finite-set entropy, information gain per action, reported entropy, hypothesis revisions,
successful revisions after falsification, and grounded belief-update rate. Scientific success
requires a runnable program, explicit stop, official `completedSuccessfully`, and exact structured
rule discovery.

### Exact repeated execution

Every policy runs twice from a fresh world with fixed engine/thread/Python seeds. The scorer compares
the evaluator-owned trace digest plus terminal outcomes and retains both receipts. A mismatch is
invalid non-reproducible; it never selects the better run. Candidate stdout is bounded and hashed
but cannot replace world evidence.

### Real end-to-end scientist proof

The Docker acceptance policy contains no answer. It parses public observations, locates the jar,
key, dispensers, cleaner, and door, tests pure substances in a cleaned apparatus, updates its belief
distribution after a negative result, confirms a positive rule, derusts the key, opens the door,
leaves, and sends an explicit structured conclusion. On the seed-0 acceptance instance it excluded
Substance A, confirmed Substance B, reduced objective entropy from 2 bits to 0 in two trials, and
completed the task. Two complete traces matched exactly.

## Six acceptance classes

1. Official terminal success plus exact explicit governing rule: scientific true.
2. Only runs, partial progress, wrong rule, or lucky task completion without rule: scientific false.
3. Canary, declared answer overlap, official source/rule/scorecard reference, or hidden-path probe:
   invalid contamination before any world run.
4. Missing submitted policy: invalid missing artifact; authored runtime failure remains scientific
   false when it is reproducible and inside budget.
5. Different evaluator-owned action traces or terminal outcomes: invalid non-reproducible, with no
   best-of-two selection.
6. Wall/CPU/memory/program-size limit: invalid resource limit; malformed file protocol is separately
   invalid protocol breach.

Additional coverage verifies source/license hashes, exact four-rule contract, public-view secrecy,
unique subset selection, immutable image separation, installed-code drift, hidden asset staging,
belief normalization, symlink/size rejection, trace digest binding, terminal explanation handshake,
authored exit behavior, suite sanitization, and evidence identity.

## Verification

- DiscoveryWorld non-Docker contract/scoring/protocol/preparation tests: 28 passed.
- Real DiscoveryWorld Docker and preparation tests: 4 passed in 9.46 seconds.
- Real source/image probes: passed; candidate package and import path absent; installed environment
  code matches the fixed source hashes.
- Real systematic discovery: passed twice with exact trace reproduction, two informative trials,
  full 2-bit information gain, correct explicit rule, and official task completion.
- Shared terminal-receipt and affected-adapter unit matrix: 56 passed; the dedicated sleeping
  trusted-container test passed in 0.82 seconds.
- Final full-project non-Docker regression: 622 passed, 1 skipped, 29 Docker tests deselected in
  262.36 seconds.
- Final all-Docker regression: 29 passed, 623 non-Docker tests deselected in 26.98 seconds.
- Ruff, formatting checks on the new DiscoveryWorld files, Python compilation, and
  `git diff --check`: passed.

Final local image identities:

- neutral candidate: `sha256:171290fc2148f741c02487bf85c9a57625bfa43a495b90328cf463afd47e9051`;
- official environment: `sha256:709176f45ec9b03492a8b89a7fc4bbbb1dd85b4641915d0050e6357b90b511f6`.

The final four-instance suite is
`workspaces/evaluator/discoveryworld-public-v1/discoveryworld_suite.v1.json`.
Its SHA-256 is `bf0b74ed4bad8277e2a669b43b34e3a28433bf32c78d12de768bedb7dd81f5d6`;
the bound scorer SHA-256 is
`732b13068fd5f34241c6a1a42080c916f96ec34ab6ada97a5e31b08089e5a36a`, and the mounted trusted
server SHA-256 is `9f0c70bef4589ad3465a11b64a64d8e814bc66e25f3e8ea2137825a4dcc5dd3b`.
It contains four task/public-task manifests and four evaluator-only mode-`0400` receipts; the
receipts cover governing IDs B, D, C, and A exactly once, are not embedded in the bundle, and no
seed, rule, scorecard, source, or art asset is candidate-visible.

## Limitations and next issue

This task has only four known hypotheses, deterministic simulated chemistry, official teleport
actions, and public source answers. It validates experiment-selection and belief-update plumbing;
it does not establish open-ended novelty, real laboratory competence, or contamination-resistant
frontier discovery. Static checks cannot prove that a model never memorized public source code.

Next is issue 9: freeze and execute the F7 baseline matrix—direct model, generic agent, no-K2, and
full K2—under the same predeclared suite/task manifests, budgets, repeated-run policy, and complete
failure decomposition. Public adapters remain diagnostic strata; private tasks are required before
an acceptance threshold can be claimed.
