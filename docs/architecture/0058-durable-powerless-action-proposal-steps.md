# Architecture decision 0058: Durable powerless action-proposal steps

- Status: accepted for the proposal-service source slice
- Date: 2026-08-26
- Scope: `PROPOSE_ACTION`, `PROPOSE_REDESIGN`, and `PROPOSE_FOLLOWUP`

## Decision

All three controller proposal steps use one closed proposal-only boundary. Before a provider sees a
request, the service locks and replays the exact Research Kernel Quest and requires the current
stream version, tail event, graph snapshot, recovery projection, and deterministic tick plan to
match. The request contains only nonterminal branch/question targets and the current charter and
scope.

`PROPOSE_REDESIGN` additionally reloads the source action from Kernel CAS and the append-only
compilation row, recomputes the canonical compiler result, proves the adjacent proposal and
authorization events, and carries the blocked compilation receipt as an objection. A follow-up
reloads the incorporated observation event and continuation row, then binds both the non-droppable
observation evidence and the typed continuation receipt. Its required action kind is derived from
the continuation disposition rather than chosen by the provider.

The external provider returns only a closed `ActionProposalDraft`: target, kind, purpose, outcome
space, cost/risk receipts, alternatives, requested authority class, and proposal time. It cannot
choose the Quest, charter, question, graph head, evidence, principal, command envelope, signer, or
database mutation. The service materializes a complete `ResearchActionProposal` and an unsigned
`ResearchCommandProposal` under the deployment-pinned proposal principal.

One private write-once spool entry is published per audited request hash. Publication is
process-serialized, first-writer-wins, atomic, fsynced, no-symlink, and canonical-byte revalidated;
exact retries and a restarted service reload the same winner. The spool is proposal custody, not a
Research Kernel ledger or authorization source.

The controller adapter returns `awaiting_authority` and the action, command-proposal, and submission
hashes. It always reports `kernel_command_signed=false` and `kernel_state_mutated=false`. A separate
Kernel command authority must fresh-audit the proposal, authorize it, and perform the transactional
Kernel commit.

## Consequences

- A provider cannot turn a compiler blocker or continuation result into a different action kind or
  omit its required evidence.
- Concurrent proposal variants for one exact request converge on one immutable operational winner.
- A stale tick, rebound receipt, changed CAS action, unsafe spool path/mode, or noncanonical bytes
  fail closed before a worker receipt is emitted.
- The controller worker still owns no Kernel signing key and has no direct Kernel mutation path.
- This slice does not commission a production proposal model/provider, RPC, Kernel command signer,
  worker runtime factory, target-host filesystem policy, or process-kill campaign. The compilation
  and continuation steps also still need their own production service/custody adapters.
