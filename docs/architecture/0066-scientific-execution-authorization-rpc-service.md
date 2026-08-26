# Architecture decision 0066: Scientific execution-authorization RPC service

- Status: accepted for the PR-7i source composition
- Date: 2026-08-26
- Scope: `ISSUE_EXECUTION_AUTHORIZATION`

## Decision

The first concrete scientific execution authorizer is a deployment-frozen exact-action catalog.
Each template contains all unsigned SEA material: the authorized Kernel action/event lineage,
accepted protocol compilation and WorkOrder, PR-4 qualification bundle/grant, scientific artifact
binding, validator/admission policy, and exact authorization/deadline times. The catalog has no
unlisted-action or dynamic-template fallback.

Before signing, the service requires the current controller projection to plan exactly
`REGISTER_EXECUTION`. It locks and audits the same Kernel head, resolves the exact accepted
compilation row, canonically re-verifies the compiler result, and uses public immutable authority
registries plus fresh artifact reads to recompute engineering qualification. It repeats the source
proof after signing and performs the existing online SEA verifier at the service clock.

Authorization times are frozen rather than sampled during each request. Ed25519 therefore returns
the same signature for the same exact template on retries. This avoids both retry identity drift
and a new issuer-owned scientific ledger. An expired template stops and requires a newly reviewed
deployment config; it is never silently extended.

The service process owns exactly one scientific domain key in addition to its independent RPC
receipt key. The domain key is an exact 32-byte, single-link, non-symlink `0400` file pinned by
owner, group, SHA-256, public key, and key id. The factory loads only public keys for qualification,
pricing, source budget, terminal verification, validation, admission, and Kernel audit. Their
principals, signing keys, and policies are role-separated.

## Consequences

- The signer can authorize only pre-reviewed exact material. It is not a resource allocator,
  execution scheduler, validator, admitter, or Kernel command authority.
- The read-only qualification custody component can prove pre-admission eligibility but is
  structurally unable to claim a later allocator admission.
- The subsequent `REGISTER_EXECUTION` service must verify this SEA and atomically couple its
  append-only preregistration to PR-4 admission/reservation. It must not load this domain key.
- Source composition closes one PR-7e factory; seven concrete factories plus target-host ACL,
  supervision, restart, and campaign evidence remain.
