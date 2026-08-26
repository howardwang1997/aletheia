# Architecture decision 0064: Deterministic action-proposal RPC service

- Status: accepted for the PR-7g source composition
- Date: 2026-08-26
- Scope: `PROPOSE_ACTION`, `PROPOSE_REDESIGN`, and `PROPOSE_FOLLOWUP`

## Decision

The first concrete proposal provider is a conservative deterministic policy, not an unrestricted
model callback. It can select only an exact target and allowed action kind from the fresh audited
`ControllerActionProposalRequest`. For compiler redesign and continuation follow-up, the request's
required action kind is mandatory. For an initial action, a deployment-frozen unique preference
list is intersected with the target's allowed kinds. No eligible intersection returns one canonical
typed blocker.

The policy freezes purpose text, canonical candidate outcomes, requested authority class, cost and
risk screening-policy identities, provider principal, and exact implementation-source hash. The
action ID is content-derived from the request, target, kind, and policy. Proposal time is the only
clock-dependent field.

The cost and risk receipts are explicit non-authority statements: cost is unknown pending an
independent budget authority, and risk is unassessed pending an independent risk authority. Neither
contains approval or authorizes execution. Their canonical identities are reconstructable from the
request and frozen policy; the complete draft and materialized unsigned command remain in the
existing first-writer-wins spool.

The materialization service now requires a distinct draft-verification port. It verifies a new
provider result before publication and reconstructs the entire stored draft on every restart retry.
This prevents an opaque provider or durable-spool winner from becoming trusted merely because its
fields still pass the generic proposal schema.

`aletheia.research_controller_action_proposal_runtime` is the guarded-loader factory for exactly one
`materialize_action_proposal` operation. Its canonical duplicate-free config pins the database URL
hash and Alembic revision, read-only Kernel trust root/CAS custody, powerless authority binding,
policy, exact provider source path and SHA-256, private spool identity, controller/worker identity,
service pin, and preparation time. It loads no scientific signing key, execution port, cost/risk
authority, or model callback.

## Consequences

- The checked-in baseline is reproducible and fail-closed, but it is intentionally not
  knowledge-grounded novelty, SOTA, causal-design, or value-of-information intelligence.
- A future richer provider must remain behind the same exact request and independent deterministic
  verifier, or introduce an explicitly reviewed richer receipt contract; it cannot silently expand
  this policy.
- Proposal RPC and transport receipts remain powerless. Only an independent Kernel command
  authority may authorize, sign, and commit a proposal.
- This closes action-proposal provider/factory and source-level spool-custody composition. It does
  not commission service accounts, socket/database/filesystem ACLs, the Kernel command signer,
  supervisor/alerting, or a live Linux multi-process campaign. PR-7h, PR-7i and PR-7j subsequently
  close the frozen protocol-compilation, exact-template execution-authorization, and atomic
  registration factories; PR-7k subsequently closes verified raw-run loading. Five other PR-7e
  factories remain.
