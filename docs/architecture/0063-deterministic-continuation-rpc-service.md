# Architecture decision 0063: Deterministic continuation RPC service

- Status: accepted for the PR-7f source composition
- Date: 2026-08-26
- Scope: graph-scoped F9-v2 `DERIVE_CONTINUATION`

## Decision

Continuation fit is no longer left to an unspecified production model provider. F9-v2 may commit an
exact outcome-bin prediction with `exact_outcome_bin_prediction_sha256()`. The identity binds the
observable, measurement protocol, analysis outcome space, and one bin from the already frozen
observation-admission policy. The admitted observation projection is version 2 and carries the
observed bin, the complete canonical admissible-bin set, and the admission-policy hash recovered
from signed validation authority.

The checked-in assessor recognizes only that identity scheme. One recognized prediction matching
the observed bin is `in_support`; one recognized different bin is `out_of_support`. A prediction
using an opaque/legacy identity or more than one exact-context prediction for the same active
hypothesis is `indeterminate`. No exact-context prediction remains missing. Consequently an unknown
format cannot be converted into an all-model miss or a hypothesis fork: both unknown and ambiguous
inputs produce the existing typed observable-redesign path.

Every assessment has a canonical reconstruction artifact. A service-owned content-addressed
archive publishes it once as `0400` regular data beneath an inode-, device-, UID-, GID-, and
`0700`-mode-pinned root. Registration and every restarted exact retry reopen and fresh-rehash the
bytes, reconstruct the fit from the audited context, and require the durable assessment to match.

`aletheia.research_controller_continuation_runtime` is the guarded-loader factory for exactly one
`derive_continuation` RPC operation. Its canonical duplicate-free config pins the database URL hash
and Alembic revision, read-only Research Kernel trust root/CAS custody, full powerless authority
binding, policy, exact assessor source path and SHA-256, artifact-root identity, controller/worker
identity, service pin, and preparation time. It loads no Kernel, observation, execution, or model
signing key and exposes no generic callback.

## Consequences

- A negative label alone still cannot trigger a fork; the observed bin must miss every active
  hypothesis under the same recognizable, preregistered outcome identity.
- Existing opaque `predicted_outcome_sha256` values fail safe to `redesign_observable`; producers
  must deliberately adopt the exact outcome-bin helper.
- Transport receipts remain operational provenance. The continuation receipt remains an
  operational projection and cannot mutate the Kernel or admit an observation.
- This closes the continuation provider/factory and source-level artifact-custody gap. It does not
  commission the service account, socket/database/filesystem ACLs, supervisor, alerting, or a live
  Linux multi-process campaign. PR-7g subsequently closes the conservative action-proposal source
  factory; nine other PR-7e service factories remain.
