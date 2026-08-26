# Architecture decision 0070: Independent F9-v2 validation RPC service

- Status: accepted for the source-composition slice
- Date: 2026-08-27
- Scope: isolated validator key, conservative assessment and write-once campaign RPC composition

## Context

ADR 0057 defined the graph-scoped F9-v2 campaign and independent validation service, while ADR 0062
defined the common external RPC process. The controller already called two typed validator
operations, but no checked-in guarded factory composed the Kernel/PR-4 readers, assessor, writable
campaign archive and validator key. The worker therefore could not reach this authority through the
production process boundary.

## Decision

- Add one two-operation guarded factory for campaign preparation and validation-receipt issuance.
  It loads only the observation-validator domain key; its transport key remains separate.
- Reuse the read-only Research Kernel audit and the complete PR-4 lineage/artifact adapter. The same
  concrete adapter proves qualification and raw-run custody without exposing allocator mutations.
- Give the validator a process-owned `0700` archive and retain one immutable canonical campaign per
  raw run. Every retry and receipt request reopens and verifies the winning bytes.
- Verify DB issuance challenges with the database public key only. The validator cannot create a DB
  challenge, commit validation, decide admission or mutate the Kernel.
- Supply a conservative exact-content assessor. A canonical frozen catalog binds the entire
  graph/protocol/slot/schema/content context to an allowed admission-policy outcome bin or explicit
  scientific rejection. Missing entries block; there is no model callback or inferred fallback.
- Pin and fresh-read the service source, assessor source and validator key before and after
  composition. Pin every filesystem root by canonical path and inode custody, and reject overlaps
  among code, CAS, archive, configuration, socket and keys.

## Consequences

- The worker can now use the existing receipt-authenticated F9-v2 validator client without sharing
  validator private key material.
- Exact known-answer campaigns have a runnable, deterministic and fail-closed assessment baseline.
- The exact-content catalog is not general scientific interpretation. Domain validators, target-host
  ACLs, supervision and live crash evidence remain commissioning gates.
- PR-7n subsequently closes the committed-validation source. Two concrete operation factories
  remained at that checkpoint; PR-7o closes independent admission, leaving only atomic
  admission/Kernel incorporation.
