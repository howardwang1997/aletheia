# PR-7g deterministic action-proposal service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_action_proposal_runtime.build_action_proposal_rpc_service` for the
single `materialize_action_proposal` endpoint. The service fresh-audits the exact Kernel/CAS and
durable compiler or continuation context, chooses only a request-listed target and action kind,
materializes an unsigned Kernel command proposal, and retains the first canonical submission in a
private write-once spool.

The baseline provider is deliberately conservative and deterministic. Initial requests follow a
deployment-frozen action-kind preference; redesign and follow-up requests must use the kind already
derived from their typed source receipt. Action identity is content-derived from the audited
request, target, kind, and policy. Restart verification reconstructs every provider-owned draft
field rather than trusting stored output.

Cost and risk hashes identify typed receipts whose dispositions are respectively
`unknown_requires_independent_budget_authority` and
`unassessed_requires_independent_risk_authority`. They contain no approved amount, safety approval,
or execution permission. The proposal service has no Kernel signer, scientific decision key,
execution port, generic model callback, or direct database mutation authority.

The duplicate-free canonical deployment config pins the exact controller and worker, RPC service,
powerless authority binding, provider policy and source bytes, database URL hash and schema
revision, read-only Kernel CAS, and the submission-spool device/inode/UID/GID/`0700` mode. The
production spool rechecks that root and the service-owned parent/file chain during access.

## Local verification

Focused tests cover all three proposal steps, exact required-kind/evidence preservation,
deterministic action identity, reconstructable non-authority cost/risk identities, stored-draft
tamper and chronology rejection, canonical blockers, first-writer/restart behavior, root custody,
duplicate/rebound configuration, exact RPC operation partitioning, and the guarded PR-7e loader.

## Remaining release gates

This closes a safe baseline provider and source-level RPC factory, not knowledge-grounded proposal
intelligence or deployment evidence. A richer provider may later replace the selection policy only
behind the same closed request, verifier, receipt, and unsigned-command boundary. A separately
deployed Kernel command authority must still decide and sign any accepted command.

The exact Linux service account must still prove socket, PostgreSQL, CAS, spool, supervisor, alert,
and process-restart custody in the PR-4/PR-5 campaign. PR-7h, PR-7i and PR-7j subsequently supply
the frozen protocol-compilation, exact-template execution-authorization, and atomic registration
factories; PR-7k subsequently supplies the verified raw-run source and PR-7l database observation
attestation; PR-7m subsequently supplies independent F9-v2 validation. Three other PR-7e service
factories remain at that checkpoint; PR-7n subsequently closes committed-validation loading, so two
factories and their domain-key/ACL
commissioning remain incomplete.

See [ADR 0064](architecture/0064-deterministic-action-proposal-rpc-service.md),
[ADR 0058](architecture/0058-durable-powerless-action-proposal-steps.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-7d worker guide](PR7D_COMPLETE_CONTROLLER_WORKER.md).
