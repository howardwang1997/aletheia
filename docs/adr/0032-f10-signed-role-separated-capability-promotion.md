# ADR 0032: Signed, role-separated capability promotion

Date: 2026-08-16

## Status

Accepted for the F10-S7 engineering boundary. No current production materials capability is
promoted by this decision.

## Context

F10-S1 made capability manifests immutable and append-only, but a structurally valid registered
manifest could still be assembled from unauthenticated hash strings. F10-S2 through F10-S6 kept
the real materials capabilities provisional because their validators were AI-authored or otherwise
not independently reviewed. The missing boundary was not another boolean on the manifest. It was a
verifiable process proving who authored, tested, validated, reviewed, audited, and authorized an
exact registry change.

The principal threats are:

- an author writes a validator that accepts its own defect;
- generated tests are silently replaced after validation;
- controls or fixtures are rebound to easier cases;
- a valid signature is replayed for another artifact, capability, domain, or registry;
- an expired, revoked, out-of-scope, or wrong-role key authorizes promotion;
- a promoter edits a manifest or registry after the independent audit;
- two concurrent updates both claim the same stale source registry;
- a successful sandbox smoke test is misreported as scientific validation; and
- synthetic conformance identities are described as real independent reviewers.

The design follows the useful separation in the SLSA provenance and in-toto attestation models:
the artifact is content-addressed while an authenticated statement describes the process that
produced or assessed it. It also adopts the TUF ideas of externally trusted role keys, configurable
signature thresholds, scoped delegation, expiry, revocation, and an exact trusted source version.

## Decision

### 1. Promotion is a chain of immutable evidence, not a lifecycle edit

A provisional manifest is never rewritten. Promotion creates a higher, contract-compatible
semantic version whose `supersedes_manifest_sha256` is the exact latest provisional object. The
target snapshot contains every source manifest byte-for-byte plus exactly one registered successor.

### 2. Six permissions have separate trust roots

The frozen `CapabilityPromotionPolicy` defines public Ed25519 keys and thresholds for:

1. hard-sandbox controller attestation;
2. generated-test-suite attestation;
3. independent validation;
4. domain review;
5. promotion audit; and
6. registry promotion.

Keys are identified by SHA-256 of their raw public bytes, restricted to exact domains and
capability prefixes, and bounded by validity and optional revocation times. A principal cannot
occupy keys in two permission classes. Thresholds count distinct principals, not merely distinct
keys.

Private keys are never fields in a policy, request, receipt, audit, update, log, or CLI argument
value. The CLI accepts only `KEY_ID=/path` references to owner-only regular files and writes new
artifacts create-only with mode `0600`.

### 3. Every signature is context- and scope-bound

The signed message commits:

- the protocol context `aletheia.capability-promotion/v1`;
- artifact kind and content hash;
- promotion-policy hash;
- registry ID;
- capability ID and domain; and
- issuance time.

Verification recomputes the public-key ID, role permission, threshold, principal identity, scope,
key activity, and Ed25519 signature. An otherwise valid test-generator signature cannot be reused
as validation authorization.

### 4. AI-authored execution remains inside a hard boundary

`SandboxAuthoringReceipt` accepts only a successful, non-truncated execution with an immutable
`sha256:` image ID and an explicit sentinel. It commits the flat staged-file index, executable
AI-authored implementation hashes, source review, output hash, and no-network/read-only/no-repo/
no-secret/no-host-write claims. Those claims become authoritative only when a trusted sandbox
controller signs the receipt.

All executable AI-authored roles in the provisional manifest must use the existing hard-sandbox or
digest-pinned-container boundary. A registered successor must replace the validator with a frozen,
non-agent-authored, role-independent implementation.

### 5. Test generation and validation are different stages and people

The generated suite freezes reference, adversarial, positive-control, and negative-control fixture
hashes before validation. Validation must execute the same suite and fixture hashes, pass every
declared case, retain no failed case in a success receipt, satisfy the manifest's exact-reexecution
and independence requirements, and bind its own implementation identity.

The author, test generator, validator, and domain reviewer are pairwise distinct. Test generator,
validator, and reviewer are also independent of every source-manifest role. Sandbox controller,
promotion auditor, and registry promoter are checked separately against the occupied principals.

### 6. Audit and registry authorization are independent signatures

The promotion auditor re-verifies the exact current registry, latest source manifest, all four
stage attestations, allowed sandbox images, reproduction requirements, chronology, and role
separation. It signs either an approved or rejected audit; failures are retained as canonical
blockers.

Only an approved audit can reach a different registry-promoter role. The promotion receipt commits
the request, signed audit, policy, source/target registry hashes, source/registered manifest hashes,
promoter principals, and timestamp. Verification reconstructs the only permitted successor and
requires exact equality with the signed target snapshot.

### 7. Production truth is separate from conformance evidence

The test suite contains a complete synthetic v2.1 provisional-to-v2.2 registered upgrade to prove
the engineering state machine. Its deterministic test-only identities and keys do not represent
human reviewers, an institution, a production trust root, or materials-science evidence.

The real registry v4 is unchanged. Its readiness audit remains false because both latest materials
capabilities have agent-authored validators and lack a trusted policy, independent validation,
domain review, signed promotion audit, and authorized registry update.

## Consequences

- A registered capability update is portable and independently verifiable without private keys.
- Stale-source and post-signing mutations fail closed.
- Organizations can raise thresholds or rotate/revoke keys without changing receipt schemas.
- Registry consumers must verify `SignedCapabilityRegistryUpdate`; a bare registered manifest is a
  data structure, not proof of authorized promotion.
- Commissioning real keys, reviewers, validation runs, and custody is operational work that code
  cannot truthfully synthesize.
- Ed25519 support adds the `cryptography` Conda dependency.

## Rejected alternatives

### Shared HMAC keys

Rejected for registry authority because every verifier could forge a new update. HMAC remains
appropriate for some single-custody run receipts but not transferable promotion authorization.

### Let the manifest validate its own evidence hashes

Rejected because an attacker can invent internally consistent hashes. Authenticity and delegated
permission require an external trust policy and signatures.

### Let generated tests serve as the validator

Rejected because test generation demonstrates coverage intent, not independent adjudication. The
validator must run frozen tests and controls with a separate implementation and principal.

### Replace the provisional manifest in place

Rejected because it destroys audit history and makes concurrent or stale updates ambiguous.

### Commit deterministic private keys for reproducible examples

Rejected. Tests derive explicitly test-only ephemeral material in memory. Repository artifacts
contain no promotion private key.
