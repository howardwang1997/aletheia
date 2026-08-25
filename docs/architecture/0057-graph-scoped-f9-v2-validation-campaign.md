# Architecture decision 0057: Graph-scoped F9-v2 validation campaign

- Status: accepted for the external-validator source slice
- Date: 2026-08-26
- Scope: independent validation request, signature, write-once publication, and fresh verification

## Decision

The independent observation validator uses a new F9-v2 contract rather than importing or wrapping
the frozen F9-v1 control plane. Before analysis, it replays the exact Research Kernel action and
historical scientific-execution authorization, verifies PR-4 qualification and raw-run lineage,
and fresh-rehashes the preregistered observation artifact through the injected custody ports.

The validation request binds the exact Quest, action and authorization event, graph snapshot and
scope, branch and question, Protocol IR, world-model snapshot, hypotheses, predictions,
measurement method, outcome space, scientific slot, raw run, artifact receipt and bytes, validation
manifest/policy, observation namespace, and selection/prediction commitments. Only predictions for
the exact observable, measurement method, and outcome space are included.

A deployment-owned assessor returns a closed positive-validation, scientific-rejection, or
engineering-blocked assessment. The external validator—not the controller worker—owns the
Ed25519 key and signs the exact request/assessment pair. A successful engineering run must select
one campaign; an engineering failure creates no campaign and can only become
`blocked_execution`. A campaign signature grants neither observation admission nor a Kernel
transition.

Publication uses one immutable canonical file per raw-run identity. The first valid writer is the
winner; exact retries and concurrent variants reload that winner. Every validation-receipt request
reopens the file with no symlink following, checks its regular-file identity and mode, reparses
canonical bytes, rebuilds the request from the supplied raw run, and verifies the externally pinned
signature before returning the existing bridge projection.

## Consequences

- The protected observation/controller graph no longer needs the legacy F9-v1 compatibility
  adapter for new scientific observations.
- Invalid custody is rejected before assessor execution, and is checked again when a DB challenge
  authorizes validation-receipt issuance.
- Scientific rejection is retained as rejection and never converted into a negative observation.
- The archive is a write-once raw-run binding, not a mutable cache or scientific ledger.
- This slice supplies contracts, an external-service implementation, and local filesystem custody.
  It does not supply an RPC transport, hardware-backed key custody, production assessor,
  supervisor, target-host qualification, or live process-kill evidence.
