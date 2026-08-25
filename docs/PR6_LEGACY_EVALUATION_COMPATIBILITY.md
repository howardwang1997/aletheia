# PR-6 legacy evaluation compatibility leaf

- Status: local source/test compatibility slice complete; production runtime unqualified
- Date: 2026-08-25
- Scope: isolate the reusable evaluation half of `DomainPlugin` without importing the legacy
  research control loop into the new authority graph

## What this slice closes

PR-6 exposes one narrow, atomic compatibility operation for source-pinned tabular evaluation. The
outer adapter accepts an ordinary `ExecutionIntent`, an exact invocation, and a verified local CSV
input. It invokes only `DomainPlugin.featurize` and `DomainPlugin.train_evaluate`. It does not call
the plugin loader, `run_experiment`, profile or demonstration hooks, the compute factory, or
`ExperimentDriver`.

The materials implementation is frozen by
`configs/capabilities/legacy-evaluation-materials-v1.json`. Its reviewed source closure includes
the base plugin contract, shared grouped-evaluation protocol, materials plugin, dataset-column
resolver, featurizer, compatibility contracts, and adapter. The Conda/container environment is a
separate `CapabilityManifestV2` runtime pin; changing either source bytes or environment requires
new qualification evidence. The committed JSON is a harness freeze, not a claim that a production
container or target host has already qualified.

Plugins that override `DomainPlugin.run_experiment` are rejected mechanically. This keeps the RAG
retrieve/answer/score loop and any other self-controlled domain path on the legacy `/runs` surface.
The complete `ExperimentDriver` remains a frozen legacy executor and is never registered as a
capability.

## Contracts and compiler path

`aletheia.legacy_evaluation.contracts` contains the closed, pure contracts:

- reviewed source and metric projections;
- a self-hashed WorkOrder-scoped invocation and exact input-table custody binding;
- an executor-produced raw result and opaque eval/model artifacts;
- a deployment-pinned validator key and signed evaluator-only validation receipt.

`LegacyEvaluationCapability` is deliberately an outer compatibility adapter because it imports a
legacy domain plugin. Protected Kernel, protocol, observation, execution, and controller packages
are forbidden from importing it. The pure contracts remain usable at typed boundaries.

`build_legacy_evaluation_protocol_manifest` projects the harness into the ordinary
`CapabilityManifestV2` catalog. The standard PR-3 compiler accepts a characterization protocol
containing a parser, this scientific-executor leaf, and an independent validator; no compiler or
Kernel branch names materials, MatBench, MAE, or RAG. The manifest is replay-safe, no-network,
single-attempt, source/environment pinned, and capped at exploratory descriptive claims that still
require independent validation.

## Raw artifact and validation boundary

The executor fresh-reads the exact CSV bytes, revalidates the complete intent/invocation scope,
checks the frozen source closure, fixes the random seed, and writes exactly:

- `eval.json`, the legacy evaluation record;
- `model.bin`, an opaque model artifact that the validator never deserializes;
- `raw-result.json`, the closed raw artifact index.

The legacy parity plot is best-effort in the old harness and is discarded rather than smuggled
through an undeclared optional WorkOrder output. Engineering success remains
`scientific_outcome=not_assessed`; the executor cannot admit an observation or promote a claim.

The independent validator imports no domain plugin. It safely reopens and rehashes every declared
file, rejects extra paths and artifact-contract drift, parses JSON with duplicate/non-finite values
disabled, mechanically projects each frozen metric from `eval.json`, and requires the grouped
protocol status. Its Ed25519 receipt grants only eligibility for the later independent scientific
validation path. It does not write research state, grant observation admission, choose a positive
or negative outcome, or approve a claim.

## Evidence from the local vertical

The PR-6 tests exercise the real materials Magpie/grouped-regression implementation over the
existing 20-composition fixture. The canonical metric tuple is frozen as
`7bb0323c36f700527c580d669ce51e0d52e19cd5e4224d9d71eaeedaa712197e`.
They also cover source drift, self-hash drift, authorization-window and input-receipt rebinding,
RAG rejection, artifact tampering, signature tampering, and exact output-tree enforcement.

A `ResearchControllerService` tick executes the leaf through its typed step port. A newly created
controller instance then reconstructs the next step from the retained projection and performs the
independent validation. The test installs an `ExperimentDriver._optimize` sentinel and observes
zero calls. This is local control-path evidence, not a PostgreSQL process-kill test or a deployed
controller step-handler service.

The old `/runs` and `/sessions` APIs remain additive-compatible and now return
`execution_surface=legacy_protocol_executor`; the dashboard renders that label next to dry/real
mode. The label is intentionally explicit: those endpoints still launch the legacy protocol
executor, not the Research Kernel controller.

## Remaining gates

PR-6 does not close the PR-4 target-host campaign or PR-5 production composition gates. Before this
leaf can run unattended, a deployment still needs:

- a real environment/image digest and independently issued capability audit receipts;
- a production step handler that materializes the exact invocation/table receipts and invokes the
  adapter inside the qualified PR-4 runtime;
- durable raw-result/validation receipt registration and the existing PR-5 scientific bridge for
  any later observation admission;
- deployment-owned validator key custody and an independent validator service;
- terminal dispatcher, worker, monitoring, and PostgreSQL/process-kill campaigns;
- the exact Linux/root/systemd/loop/ext4/rootful-Docker qualification campaign.

RAG and other full legacy control flows remain legacy. Expanding them requires separate, atomic,
typed capabilities and must not add domain branches to the Kernel or controller.

PR-7a now provides the pinned process boundary plus concrete Kernel-dispatcher and
delivery-reconciler composition. The PR-6 worker handler, terminal dispatcher, independent
validator deployment, monitoring, and live process-kill campaign in the list above remain open.

See [ADR 0051](architecture/0051-legacy-evaluation-compatibility-leaf.md) and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
