# Architecture decision 0051: Isolated legacy evaluation compatibility leaf

- Status: accepted for the PR-6 local source/test slice
- Date: 2026-08-25
- Scope: `DomainPlugin` evaluation reuse, raw-artifact validation, and legacy-surface labeling

## Decision

Only the isolatable `featurize` plus `train_evaluate` portion of a reviewed `DomainPlugin` may
enter the new execution system. It is exposed as one source-pinned, no-network,
`CapabilityManifestV2` atomic operation. The adapter lives outside every protected authority
package and receives an explicitly injected plugin; the Kernel and controller depend only on
domain-neutral contracts and their existing execution/step ports.

A plugin that overrides `run_experiment` owns a larger control flow and is ineligible. Therefore
the RAG path and the full `ExperimentDriver` stay behind the legacy `/runs` API. Neither may be
registered as a capability or imported transitively by the new authority graph.

Compatibility execution emits only raw eval/model/index artifacts. Process success never supplies
a scientific outcome. A separate domain-free validator fresh-rehashes the files, mechanically
checks the frozen metric projections and grouped protocol, and signs an evaluator-only eligibility
receipt. It cannot mutate the Research Kernel, fill a scientific slot, or promote a claim.

Legacy APIs remain available, but every run/session response and the dashboard identify their
surface as `legacy_protocol_executor`. This is an additive contract field, not a migration of
legacy Runs into the Research Kernel.

## Consequences

- Existing materials evaluation behavior can be exercised by the new controller without calling
  the legacy optimization loop.
- Source or transitive implementation drift invalidates the harness freeze; environment and
  qualification evidence remain separate deployment pins.
- The standard protocol compiler and controller need no materials, MatBench, MAE, molecule, or RAG
  special case.
- Opaque models are never loaded by the independent validator.
- Optional legacy plots are discarded rather than bypassing the exact WorkOrder artifact tree.
- Compatibility validation is not observation admission; the PR-5 bridge remains mandatory for
  scientific incorporation.
- Production readiness still depends on PR-4 host qualification and PR-5 process composition.

See [the PR-6 guide](../PR6_LEGACY_EVALUATION_COMPATIBILITY.md) for concrete contracts, local
evidence, and remaining gates.
