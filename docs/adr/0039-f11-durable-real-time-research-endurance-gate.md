# ADR 0039: Durable, real-time research endurance evidence

- Status: Accepted for the F11-S7 engineering boundary; real 72-hour evidence pending
- Date: 2026-08-18

## Context

F11-S1 through F11-S6 provide durable work, exact scientific commands, a reconstructible research
graph, receipt-backed memory, a shadow portfolio, and deterministic fault campaigns. None of those
proves that one frozen research direction remains coherent through a 72-hour process lifetime.

A naive soak script has four dangerous ambiguities:

1. process-local elapsed time disappears when the process is killed;
2. caller-supplied timestamps can turn a short test into a false 72-hour claim;
3. a mutable `running` row can be repaired after the fact; and
4. counting a renamed prompt as a pivot lets a model manufacture progress without changing what
   would discriminate hypotheses.

The endurance boundary must preserve negative evidence and failure, survive controller restart,
and remain incapable of activating autonomous allocation.

## Decision

### 1. Separate engineering time from scientific time permanently

Every manifest chooses exactly one evidence class:

- `accelerated_engineering` exercises contracts, persistence, resume, and acceptance in seconds;
- `real_time_72h` requires at least 259,200 elapsed seconds and rejects every caller-supplied clock.

Only a passing `real_time_72h` report can set `real_72h_passed=true`. Database constraints repeat
that rule. Accelerated evidence can pass its own manifest but is never eligible for F11 scientific
exit.

Production timestamps use PostgreSQL `clock_timestamp()`, which returns the changing wall clock;
`now()`/`transaction_timestamp()` instead remain fixed at transaction start:
[PostgreSQL date/time functions](https://www.postgresql.org/docs/current/functions-datetime.html).
Python `time.monotonic()` remains useful for one live process, but its value has no cross-process
epoch and is therefore not the durable source of a restarted 72-hour window:
[Python clock documentation](https://docs.python.org/3/library/time.html).

### 2. Derive state from three append-only ledgers

`research_endurance_gates` stores the immutable start, `research_endurance_checkpoints` stores a
contiguous parent-hash chain, and `research_endurance_reports` stores one terminal report. There is
no mutable status projection. An unfinished gate is a start without a terminal report; its next
sequence and parent come from the database tail after a Quest/gate lock.

The start, each checkpoint, and the report are independently committed through
`research_endurance.mutation`, with their command result and keyed event in the same transaction.
Database triggers reject update/delete and inserts outside the applying command.

### 3. Freeze direction and authority, not scientific learning

The start manifest seals:

- the active Quest specification and initial reconstructed graph;
- at least two exact research-question versions;
- at least three Campaign identities;
- the complete budget-allocation and data-role manifests;
- the latest passing F11-S6 fault report; and
- harness/environment identities, duration, checkpoint cadence, and evidence floors.

Quest direction, question versions, budget authority, and data roles cannot change during a passing
gate. Campaign states may evolve and new branches may be added because a valid negative-result
pivot is part of the test. Initial branches can never disappear from the append-only graph.

### 4. Use typed evidence, not milestone booleans

Checkpoints add content-addressed receipts for:

- reproduction on a distinct Campaign branch;
- a confirmed API/worker process exit bound to a passing fault scenario;
- a confirmed provider transport interruption bound to a passing fault scenario; and
- a structural pivot caused by an exact negative-result fact.

The store resolves all Campaigns, facts, transitions, fault reports, portfolio epochs, budget
charges, one-time actions, and command/event receipts from their authoritative ledgers. Repeating a
receipt in another checkpoint conflicts rather than increasing a count.

### 5. Define a structural pivot mechanically

A pivot names the negative-result fact, stopped/paused/failed source transition, active successor
transition, independent assessor, and before/after strategy fingerprints. The negative result must
precede both graph changes. At least two fingerprint dimensions must change, and one must be the
prediction pattern or discriminated hypothesis pairs. Editing wording, seed, or a single analysis
label cannot satisfy the contract.

### 6. Treat soak coverage as evidence

Finalization measures the start-to-first, checkpoint-to-checkpoint, and last-to-final gaps. A late or
missing checkpoint remains in the chain but blocks the report. This follows the operational
principle that an experiment needs a measurable steady state, real failure events, and continuous
automation: [Principles of Chaos Engineering](https://principlesofchaos.org/).

Google SRE guidance also motivates representative duration and cadence: canaries must span long
work units, while synthetic load gives code coverage but limited state coverage:
[Google SRE canarying releases](https://sre.google/workbook/canarying-releases/). Long-running data
processing guidance recommends soak duration that covers worst-case runtime plus verification and
peak load: [Reliable Data Processing with Minimal Toil](https://sre.google/static/pdf/reliable_data_processing_with_minimal_toil.pdf).

### 7. Keep final scientific exit and activation separate

The terminal report derives duration, gaps, milestone counts, zero-loss invariants, an independent
efficiency comparison, Campaign states/reasons, budget state, and the complete final portfolio.
Passed, blocked, and failed reports are retained. Every report and audit contains
`autonomous_allocation_enabled=false`.

The accelerated acceptance suite proves only the engineering implementation. A real Quest must
still run for 72 wall-clock hours. F12 reality-linked independent replication and any signed/IAM
activation decision remain separate gates.

## Consequences

- A controller crash cannot reset elapsed time or checkpoint sequence.
- A short test cannot be relabelled as 72-hour evidence in Python or PostgreSQL.
- Missing cadence, evidence, portfolio output, efficiency improvement, or duration becomes an
  explicit blocker.
- Direction, question, budget, or data-role drift becomes an integrity failure.
- Evidence is larger because terminal reports retain a complete portfolio and checkpoint chain.
- Real production passage necessarily takes at least 72 hours; engineering completion cannot
  shorten that scientific requirement.

## Rejected alternatives

### Sleep in one process and write a final JSON file

Rejected because a process kill loses both elapsed-state custody and the unique checkpoint tail.

### Let tests override the production clock

Rejected because the same interface could mint false real-time evidence. Clock override exists only
for the permanently labelled accelerated class.

### Freeze the entire graph

Rejected because scientific learning must change Campaign state and may add a successor branch.
The Quest direction, exact questions, budgets, and data roles are the frozen authority boundary.

### Let the model state that it pivoted

Rejected because a self-report cannot establish causal chronology or a changed discriminating
strategy.

### Delete a premature or failed finalization

Rejected because doing so hides reliability/scientific failure history. Start a new content-addressed
gate after remediation.

### Treat an accelerated passing report as F11 exit

Rejected because contract coverage is not elapsed wall-clock endurance evidence.
