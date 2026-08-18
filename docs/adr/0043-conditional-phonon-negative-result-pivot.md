# ADR 0043: Make the phonon structural pivot conditional on an exact negative result

Date: 2026-08-18

Status: accepted

## Context

F11-S7 requires a real structural pivot caused by a negative result. The production replay outcome
is deliberately unknown before the gate. Automatically pivoting on every result, pre-writing a
negative fact, or selecting a convenient old failure would turn the acceptance criterion into a
scripted label rather than evidence of adaptive scientific behavior.

The commissioned Quest has one remaining planned external-calculation Campaign, but its candidate
corpora have not passed lineage and target audits. Activating that Campaign must not imply that an
external dataset is ready or that target access is authorized.

## Decision

Freeze a content-addressed, contradiction-only pivot work order before gate start. Its source is the
Campaign used by the implementation-diverse replay and its successor is the commissioned external-
calculation Campaign, limited to lineage/target qualification.

Execution requires the exact `PhononReplayCommitReceipt`, the byte-identical reproduction envelope
in controller spool, and the authoritative non-droppable `pivot-analysis` memory fact. The commit,
typed evidence, and fact must agree on protocol, result ID/SHA, Campaigns, conclusion, statement,
detail, task bindings, and source artifact. All causal timestamps come from PostgreSQL and must be
inside the live gate.

Only `contradicted` is applicable. On that trigger, commit a fixed source `active→stopped` command
and then a fixed successor `planned→active` command. Derive the pivot time from their durable
transition timestamps and submit a receipt whose before/after fingerprints change at least two
dimensions, including prediction pattern and discriminated pairs. Fixed idempotency keys make a
crash between either transition or evidence submission replay-safe.

The successor reason and receipt explicitly retain `data_allocation=false` and
`outward_action=false`. Qualification work needs a separate audited data workflow before it can
create an external-validation role.

## Consequences

- A confirmation or inconclusive result cannot be relabeled to pass the endurance gate.
- A negative memory label without the exact reproduction envelope and result provenance cannot
  cause graph mutation.
- The negative fact precedes both actual graph transitions, and the independent assessor principal
  differs from their principal.
- The route is structurally different but bounded to audit; it grants no dataset or external-action
  authority.
- The gate may honestly fail or remain blocked when nature supplies no qualifying negative result.
- The portfolio epoch must execute immediately after start and before reproduction activation,
  because the portfolio work order requires the initial graph.

## Rejected alternatives

### Always pivot after the replay

Rejected because a positive or inconclusive result is not a negative-result cause.

### Use the pre-start fault campaign or an older negative fact

Rejected because operational faults are not scientific contradictions and pre-window facts cannot
establish in-window adaptation.

### Generate a new negative-result fact inside the pivot module

Rejected because the evidence producer, not the transition mechanism, owns outcome classification.

### Activate and allocate an external corpus together

Rejected because candidate discovery is not lineage, license, target, leakage, or custody
qualification, and target access may be irreversible.
