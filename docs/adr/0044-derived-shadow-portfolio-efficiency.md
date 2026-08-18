# ADR 0044: Derive endurance efficiency from the blind shadow portfolio

Date: 2026-08-18

Status: accepted

## Context

The endurance gate requires at least 10% efficiency improvement, but the generic receipt accepts
integer value and cost fields. Allowing an operator to hand-author those fields would make the final
threshold self-graded. Actual production costs and outcomes are not yet available before the run,
while the F11-S5 shadow epoch already provides a causally blinded human/planner comparison with
frozen candidate durations and question scope.

The metric must remain honest about the distinction between expected planning efficiency and
realized scientific efficiency. It must also stay replayable after later Campaign transitions and
must not expose planner output before the human baseline is frozen.

## Decision

Add a content-addressed efficiency work order prepared after the human blind plan and before gate
start. Require exactly one experimental baseline candidate (`replication` or `mechanism_test`) and
freeze the candidate-to-question mapping plus preregistered durations. Do not calculate scores or
selection during preparation.

Immediately after the in-window shadow epoch, reconstruct the append-only slate and epoch and derive
question coverage per estimated duration:

- baseline value: distinct questions covered by the one human candidate;
- baseline cost: that candidate's estimated duration in microseconds;
- endurance value: distinct questions covered by the planner shadow batch; and
- endurance cost: the batch's summed estimated durations in microseconds.

Pass these integers through `EnduranceEfficiencyReceipt`, which independently checks the cross-
product improvement formula. Bind the receipt to work-order, slate, epoch, decision, comparison,
question mapping, code, and source hashes. Use the epoch evaluation time as `assessed_at`; all
deterministic inputs existed then, and this makes exact retries time-stable.

Label the result explicitly as expected rather than realized. Reject an infeasible human baseline,
blocked/empty planner decision, non-shadow epoch, or any action enqueue. Preserve below-floor and
negative results without repair. Finalization remains a separate independent command after 72 hours.

## Consequences

- Operators cannot choose value/cost numbers or directly choose `improvement_ppm`.
- The human baseline remains genuinely blind because preparation does not derive planner output.
- A shadow plan can demonstrate expected planning efficiency but cannot claim realized scientific
  output, cost savings, or successful execution.
- The production improvement is unknown until the human plan and live epoch exist.
- A below-floor result blocks the gate rather than causing candidate, duration, or baseline edits.
- Stable database causal bounds tolerate small clock corrections without caller-supplied time or
  breaking content-addressed replay.

## Rejected alternatives

### Let finalization accept a hand-written efficiency JSON

Rejected for production because the same operator could choose both numerator and denominator.

### Compare against an empty or infeasible baseline

Rejected because it would manufacture improvement from no scientific alternative.

### Call expected planning efficiency realized research efficiency

Rejected because shadow actions are never enqueued and their outcomes/costs are not observed.

### Recompute with a new timestamp on every retry

Rejected because identical scientific inputs would then create multiple receipt identities.
