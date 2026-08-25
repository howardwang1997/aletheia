# Capability authoring and promotion

F10-S7 provides the trust boundary for moving one exact provisional capability to a registered
successor. It does not automatically register the current materials capabilities. Registration
requires real independent people or services, real private-key custody, and evidence generated
under the frozen policy.

## State transition

~~~text
provisional manifest (immutable, exploratory only)
  |
  +-- hard-sandbox authoring receipt -------- sandbox-controller signature
  +-- generated tests frozen ---------------- test-generator signature
  +-- independent validation + controls ----- validator signature
  +-- domain/safety/claim-scope review ------- domain-reviewer signature
  |
  +-- complete promotion request
        |
        +-- independent audit (approve/reject) -- promotion-auditor signature
              |
              +-- registered successor + append-only snapshot
                    |
                    +-- promotion receipt ------- registry-promoter signature
~~~

The author, test generator, validator, and domain reviewer must be pairwise distinct. The sandbox
controller, promotion auditor, and registry promoter occupy separately delegated permissions. The
registered validator must be frozen before validation, must not be AI-authored, and cannot reuse the
executor adapter.

## Core objects

`CapabilityPromotionPolicy`

: An externally trusted policy for one exact source-registry hash. It contains only Ed25519 public
  keys, key-derived IDs, principal hashes, domain/capability scopes, role thresholds, validity,
  revocation, allowed immutable sandbox images, and policy expiry.

`SandboxAuthoringReceipt`

: Commits the provisional manifest, executable AI-authored implementation hashes, staged source
  index, source-review hash, immutable image, output, time interval, and hard-boundary properties.
  A local-dev result, mutable image, truncated output, failed process, or missing sentinel cannot
  create this receipt.

`GeneratedCapabilityTestSuiteReceipt`

: Freezes the exact test suite plus reference, adversarial, positive-control, and negative-control
  fixtures before validation. Test generation is evidence about proposed coverage; it is not the
  validation decision.

`IndependentCapabilityValidationReceipt`

: Binds the independent validator implementation/principal to the frozen suite, exact case counts,
  both control outputs, reexecution count, independent recomputation, and reproduction-policy
  evidence. Successful receipts cannot retain failures.

`DomainCapabilityReviewReceipt`

: Bounds the approved claim types and maximum evidence level and commits safety, measurement,
  protocol, and domain-review evidence.

`CapabilityPromotionRequest`

: Closes the four receipts and signatures, source registry/manifest, independent validator binding,
  compatible target version, target evidence ceiling, and chronology into one immutable request.

`SignedCapabilityPromotionAudit`

: Records all required checks and canonical blockers. A rejected audit is signed and retained but
  cannot update a registry.

`SignedCapabilityRegistryUpdate`

: Contains the append-only target snapshot, promotion receipt, and separate registry-promoter
  attestation. Verification reconstructs the exact successor rather than trusting serialized
  lifecycle fields.

## Signature and permission rules

Each signature covers a canonical message with:

- protocol context `aletheia.capability-promotion/v1`;
- artifact kind and SHA-256;
- policy SHA-256 and registry ID;
- capability ID and domain; and
- issuance time.

The verifier then checks:

1. key ID equals SHA-256 of the raw Ed25519 public key;
2. key belongs to the artifact's permission role;
3. enough distinct principals meet the role threshold;
4. domain and capability prefix are delegated;
5. issuance falls inside policy/key validity and before revocation;
6. signature bytes are valid; and
7. the envelope binds the exact embedded receipt.

Private keys are never serialized. Do not put a private key in YAML/JSON, an environment variable,
a command argument value, or the repository. The CLI reads only `KEY_ID=/path` references. The key
file must be a non-symlink regular file owned by the current user with no group/world permissions;
it may contain 32 raw bytes or 64 lowercase/uppercase hexadecimal characters.

## Authoring integration

Use `run_provisional_capability_authoring` for the normal integration path. It hard-codes the
existing `execute_python_files` boundary to `backend="docker"`; the caller supplies staged files,
script, timeout, source-review hash, and success sentinel. `build_sandbox_authoring_receipt` remains
the lower-level adapter for a controller that already owns a `SandboxExecution`. The result must
include an immutable image ID and trusted sentinel. A sandbox controller holding the
`sandbox_attest` key signs the resulting receipt with `sign_promotion_artifact`.

Constructors are not authority. Any process can create a syntactically valid receipt; only a
signature verified against the externally supplied policy makes the claim admissible.

The hard-sandbox receipt is also not scientific validation. It proves bounded execution and exact
artifact identity. Reference/adversarial behavior, controls, reproduction, scientific scope, and
registry authority are later independent stages.

## CLI workflow

All Python commands use the project Conda environment.

### 1. Inspect current readiness

~~~bash
conda run -n aletheia python scripts/capability_promotion.py readiness \
  --registry workspaces/evaluator/capabilities/materials_registry_v4.json \
  --audit-id f10-s7-materials-promotion-readiness-v1 \
  --audited-at 2026-08-16T06:00:00Z
~~~

Add `--require-ready` in CI; it returns exit 2 while blockers remain.

### 2. Independent auditor signs a frozen request

~~~bash
conda run -n aletheia python scripts/capability_promotion.py audit \
  --registry path/to/source-registry.json \
  --policy path/to/promotion-policy.json \
  --request path/to/promotion-request.json \
  --auditor-key '<key-id>=/secure/path/auditor.ed25519' \
  --audited-at 2026-08-20T02:00:00Z \
  --output path/to/signed-audit.json
~~~

Repeat `--auditor-key` when the policy threshold exceeds one. The output is create-only. A rejected
audit is still written and exits 3 so its blockers are not lost.

### 3. Registry promoter appends a successor

~~~bash
conda run -n aletheia python scripts/capability_promotion.py promote \
  --registry path/to/source-registry.json \
  --policy path/to/promotion-policy.json \
  --request path/to/promotion-request.json \
  --audit path/to/signed-audit.json \
  --promoter-key '<key-id>=/secure/path/promoter.ed25519' \
  --promoted-at 2026-08-20T03:00:00Z \
  --output path/to/signed-registry-update.json
~~~

The source snapshot must still match the policy and request. Once one update is accepted, another
promotion using the stale policy/source fails closed and needs a new policy epoch bound to the new
snapshot.

### 4. Consumers verify and reconstruct

~~~bash
conda run -n aletheia python scripts/capability_promotion.py verify \
  --registry path/to/source-registry.json \
  --policy path/to/promotion-policy.json \
  --request path/to/promotion-request.json \
  --audit path/to/signed-audit.json \
  --update path/to/signed-registry-update.json
~~~

Only the returned verified target snapshot should enter a registered-capability planner. Loading a
bare manifest or target JSON proves schema validity, not authorization.

## Current materials readiness

The frozen audit is
`configs/capabilities/f10_promotion_readiness_audit_v1.json`:

- registry: `materials-capabilities-v4`;
- registry SHA-256: `80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77`;
- latest candidates: range compression v2.1.0 and ASE/EMT EOS reference v1.0.0;
- registered capability count: 0; and
- production promotion ready: false.

Each latest candidate has the same honest blockers: agent-authored validator, no trusted production
policy, no independent validation, no independent domain review, no signed promotion audit, and no
authorized registry update. The audit object hash is
`b1017ae5e7cbb8ffb7628ec9b0ce12a11bd060d272518e69b6d3a3a6f0dad9c0`.

## Test-only full upgrade

`tests/capabilities/test_promotion.py` loads the real immutable range-compression v1→v2.0→v2.1
lineage, constructs a synthetic trust policy and independent roles, promotes v2.1 to a registered
v2.2 successor, and verifies the signed append. The keys are deterministic and explicitly
test-only; no private bytes are committed.

This satisfies the engineering conformance case. It does not claim that the real v2.1 capability
has an independent validator or that its results are confirmatory.

## Failure interpretation

Treat these as integrity failures, not negative scientific results:

- invalid, missing, expired, revoked, out-of-scope, or wrong-permission signature;
- receipt/fixture/control/implementation hash mismatch;
- role-principal reuse;
- AI-authored registered validator;
- policy or request bound to a different source registry;
- source manifest no longer latest;
- signature or review predating its artifact;
- reproduction requirement not met;
- rejected or incomplete promotion audit; or
- target snapshot different from the reconstructed append.

A failed positive/negative control or adversarial case prevents a successful validation receipt. It
does not become evidence against the scientific hypothesis being studied by the capability.

## Remaining production work

1. Commission offline/managed Ed25519 trust roots and a rotation/revocation procedure.
2. Obtain a genuinely independent validator implementation for one bounded capability.
3. Freeze reference, adversarial, positive, and negative fixtures before that validator runs.
4. Obtain separate domain/safety/measurement review with an explicit evidence/claim ceiling.
5. Execute the signed audit and registry promotion under distinct custody.
6. Verify the update at the planner boundary and retain the source plus signed transition.
7. Only then commission fresh confirmation; promotion itself is not scientific confirmation.
