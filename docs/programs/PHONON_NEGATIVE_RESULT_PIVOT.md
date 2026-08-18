# Phonon negative-result pivot

This is the contradiction-only structural-pivot producer for the first production phonon endurance
Quest. It never manufactures a negative result and never treats an inconclusive or confirmed replay
as a pivot trigger.

## Precommitted route

The work order freezes one conditional route before gate start:

~~~text
implementation-diverse replay/mechanism branch
    -- exact committed contradiction --> stopped

external-calculation branch
    -- after the same negative fact --> active for lineage/target qualification only
~~~

The successor activation authorizes only calculation-lineage, license/custody, target-definition,
and target-blind overlap audits. It does not create an `external_validation` data role, allocate a
dataset, inspect external target values, spend budget, or authorize an outward action.

The before fingerprint binds the same-source replay predictions, hypothesis pairs, source files,
estimator policy, and code. The after fingerprint binds the structurally different cross-workflow
transfer question and qualification plan. It changes predictions, capability/input readiness,
analysis, and discriminated pairs while preserving the honest unresolved hypothesis semantics.

## Freeze before start

Prepare only from the final mutually bound controller and replay protocol:

~~~bash
conda run -n aletheia python scripts/run_phonon_negative_pivot.py prepare \
  --controller artifacts/phonon-quest/endurance/controller-manifest-vN.json \
  --protocol artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  --commissioning artifacts/phonon-quest/commissioning-manifest.json \
  --output artifacts/phonon-quest/endurance/pivot/work-order-vN.json

conda run -n aletheia python scripts/run_phonon_negative_pivot.py preflight-start \
  artifacts/phonon-quest/endurance/pivot/work-order-vN.json
~~~

Preflight rehashes committed code and bound artifacts, reconstructs the exact initial graph, requires
both conditional source and successor Campaigns to remain planned, and rejects an already started
gate. It performs no graph mutation.

## Required in-window ordering

After the separate explicit gate start, preserve this order:

1. materialize the already staged portfolio shadow epoch while the graph is still initial;
2. activate the same-source reproduction Campaign;
3. run, physically replay, and commit the reproduction outcome to memory/controller spool; and
4. only if its committed conclusion is `contradicted`, execute this pivot.

The portfolio must come first because even reproduction activation is a graph transition. A
confirmed or inconclusive replay is retained as-is and makes this work order not applicable; an
operator must not edit the result, inject a negative fact, or use a different Campaign merely to
satisfy the endurance gate.

For a genuine contradiction:

~~~bash
conda run -n aletheia python scripts/run_phonon_negative_pivot.py execute \
  artifacts/phonon-quest/endurance/pivot/work-order-vN.json \
  artifacts/phonon-quest/endurance/reproduction/commit-receipt.json \
  --output artifacts/phonon-quest/endurance/pivot/execution-receipt.json
~~~

Execution verifies all of the following before changing the graph:

- the replay commit and envelope name the exact controller, gate, protocol, result, and Campaigns;
- the reproduction envelope still exists byte-for-byte in pending or committed controller spool;
- its conclusion and typed reproduction receipt are both `contradicted`;
- the authoritative `pivot-analysis` memory fact is a non-droppable negative result with the exact
  statement, detail, task bindings, artifact source, result ID, and SHA-256; and
- replay completion and fact creation occurred inside the live gate window.

It then commits two fixed idempotent graph commands: source `active→stopped`, followed by successor
`planned→active`. The transition timestamps come from PostgreSQL. Their IDs, the exact negative fact,
and the precommitted fingerprints form one `EnduranceStructuralPivotReceipt`, which enters the
controller spool. A crash between the two transitions or between graph commit and spool write is
resumed by exact command/envelope replay.

## Outcome matrix

| Replay conclusion | Graph mutation | Pivot evidence |
|---|---:|---:|
| `contradicted` with exact committed provenance | conditional route executes | one typed receipt |
| `confirmed` | none | none |
| `inconclusive` | none | none |
| missing, forged, drifted, or pre-window evidence | none | rejected |

This means the real endurance gate may honestly remain blocked if the production science produces
no negative result. The work order proves causal handling where warranted, not a guaranteed passing
outcome.

## Acceptance

~~~bash
conda run -n aletheia pytest -q \
  tests/domains/materials/test_phonon_endurance_portfolio.py \
  tests/programs/test_endurance_gate.py
~~~

The tests prove non-contradiction rejection, exact negative-fact closure, two-step durable graph
transition, replay after partial/full completion, zero data/outward authority, controller spool
submission, and gate-side causal validation. Production remains zero-result and unstarted.
