# Architecture decision 0056: Independent observation controller steps

- Status: accepted for the production-boundary source slice
- Date: 2026-08-26
- Scope: `COMMIT_VALIDATION`, `COMMIT_ADMISSION`, and deterministic raw-run recovery

## Decision

The controller worker coordinates observation validation and admission through separate,
deployment-pinned ports. It never loads a database, validator, admission, or Research Kernel
private key.

`COMMIT_VALIDATION` reconstructs one deterministic `RawRunEnvelope` from the exact SEA
preregistration and a newly public PR-4 projection containing only historically verified terminal
contracts. It then asks an external validator for a campaign identity, obtains a DB-time signed
challenge from a separate database-attestation service, receives the independently signed
validation receipt, and accepts only the database-signed durable commit of that exact receipt.
Engineering failure cannot select a validation campaign; a successful process must select one.

`COMMIT_ADMISSION` reloads the canonical committed-validation bytes by Quest and scientific slot,
obtains a fresh database challenge, and asks a distinct admission authority for a signed decision.
A signed rejection becomes a typed controller blocker and never calls the Kernel coordinator. An
admitted decision completes only after the existing atomic coordinator returns proof that the
admission row and signed `observation_incorporated` Kernel event committed together.

Every injected service exposes the same frozen authority binding that appears in the step adapter
manifest. Returned challenges, receipts, decisions, and Kernel receipts are independently rebound
to those principal, key, policy, service-manifest, Quest, action, slot, and graph identities.

## Consequences

- Restart recovery no longer needs an in-memory raw-run or committed-validation object.
- A worker cannot self-sign validation, admission, DB attestation, or Kernel incorporation.
- A campaign result or signed decision alone does not confer scientific authority.
- This slice supplies the controller adapters and durable read seams, not an independently
  deployed F9-v2 validator/admitter RPC service or its key-custody evidence.
- PR-7d later supplies the complete checked-in worker composition. Target-host qualification,
  external service commissioning, and multi-process kill/restart remain deployment gates.
