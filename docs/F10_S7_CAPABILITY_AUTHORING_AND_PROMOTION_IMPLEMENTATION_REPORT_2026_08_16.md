# F10-S7 Capability Authoring and Promotion Implementation Report

Date: 2026-08-16

## Outcome

The F10-S7 engineering boundary is implemented: a provisional capability can pass through
hard-sandbox authoring evidence, separately frozen generated tests, independent validation,
independent domain review, a signed promotion audit, and a separately signed append-only registry
update. The complete transition is content-addressed, Ed25519-authenticated, role/threshold/scoped,
time-bounded, and reconstructable from the exact source snapshot.

The current materials registry was not promoted. Both latest real capabilities remain provisional,
and the new machine audit reports `production_promotion_ready=false` and zero registered
capabilities. The only complete provisional-to-registered case is a synthetic conformance fixture;
it establishes the engineering contract, not independent scientific review.

## Related-work basis

The design adapts three mature software-supply-chain patterns to scientific capability admission:

- [SLSA provenance](https://slsa.dev/spec/v1.2/provenance) separates artifact subjects from
  authenticated claims about how a trusted builder produced them.
- [in-toto attestations](https://github.com/in-toto/attestation) use signed, typed statements to
  bind actors and supply-chain steps to content identities.
- [The Update Framework](https://theupdateframework.github.io/specification/latest/) uses
  externally trusted role keys, signature thresholds, scoped delegation, expiry, versioning, and
  rollback-resistant update verification.

F10-S7 does not claim conformance certification with those projects. It implements the relevant
principles in Aletheia's existing frozen Pydantic/content-addressed object model.

## Implemented boundary

### Public-key policy and signatures

`aletheia/capabilities/promotion.py` adds:

- raw Ed25519 public keys with key IDs derived as SHA-256(public key);
- distinct sandbox, test, validation, domain-review, audit, and registry permissions;
- configurable thresholds counted by distinct principal;
- exact domain and capability-prefix scopes;
- validity windows and revocation times;
- a context-separated canonical signature message; and
- verification of permission, threshold, identity, scope, time, and signature bytes.

Private keys are function arguments to the signing operation only. They do not appear in any
persisted schema.

### Provisional hard-sandbox authoring

`build_sandbox_authoring_receipt` refuses:

- a non-provisional source manifest;
- a failed execution;
- local/mutable-image execution without an immutable `sha256:` image ID;
- truncated output;
- a missing trusted sentinel;
- an empty source bundle; or
- a manifest without executable AI-authored roles.

`run_provisional_capability_authoring` is the production integration entry point and hard-codes the
existing executor to `backend="docker"`; callers cannot silently select `local_dev` through it.

The receipt binds the exact source-file index and every executable agent-authored role
implementation. The request additionally requires each such role to use the hard-sandbox or
digest-pinned-container boundary.

### Test generation versus validation

The generated suite freezes reference/adversarial fixtures and both control fixtures before
validation. Validation must bind the same suite, counts, and fixture hashes, pass every reference
and adversarial case, pass both controls, retain no failure, bind a frozen non-agent-authored
validator implementation, and satisfy the capability's reproduction requirements.

The author, test generator, validator, and domain reviewer must differ. Test/review actors cannot
reuse a source-manifest role. An independent registered validator cannot reuse the executor adapter.

### Domain review and claim ceiling

The domain receipt signs an explicit approved claim set and maximum evidence level, along with
safety and review evidence. A promotion request cannot expand the provisional claim set or exceed
that independently approved evidence ceiling.

### Independent audit and append-only update

The audit re-verifies the source registry, latest manifest, signatures, sandbox images,
chronology, control/reproduction obligations, and role separation. It retains canonical blockers
and signs both approval and rejection outcomes.

An approved audit is necessary but insufficient: a different registry promoter must sign the
promotion receipt. The verifier reconstructs the successor manifest and target registry from the
request, audit, and promotion time. Any extra edit, missing source manifest, wrong successor,
stale source, or changed target hash fails closed.

## Operational CLI

`scripts/capability_promotion.py` provides:

- `readiness` — machine-readable gaps and a CI `--require-ready` exit;
- `audit` — threshold-sign an approved or rejected request;
- `promote` — append and sign one registered successor; and
- `verify` — independently reconstruct and verify the target registry.

Signing keys use `KEY_ID=/path`. The file must be non-symlink, regular, owned by the current user,
and mode `0600` or stricter. Audit/update outputs are create-only and atomically committed with mode
`0600`.

## Adversarial coverage

The focused suite exercises a complete synthetic upgrade and rejects:

1. test generator equal to validator;
2. agent-authored self-validator;
3. control-fixture rebinding;
4. attestation issued before its artifact;
5. forged test-suite signature;
6. signature reused under the wrong permission;
7. signing outside delegated domain/capability scope;
8. one principal controlling test and validation permissions;
9. revoked signing key;
10. local/mutable-image authoring receipt;
11. registry signature mutation;
12. registry rollback/source-hash mismatch;
13. a stale source policy attempting a second concurrent promotion;
14. an audit with a required check removed; and
15. group-readable private-key files; and
16. private-key symlinks or overwrite of an already frozen output.

The CLI audit→promote→verify sequence is also executed end to end with temporary owner-only keys and
create-only artifacts.

## Frozen current-state audit

Artifact:
`configs/capabilities/f10_promotion_readiness_audit_v1.json`

- audit object SHA-256:
  `b1017ae5e7cbb8ffb7628ec9b0ce12a11bd060d272518e69b6d3a3a6f0dad9c0`;
- file SHA-256:
  `6ce1e4819ad117978dd39a50cfb82f2481a5fdbb8cf4eac840a6f8fbb20f1bdc`;
- source registry SHA-256:
  `80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77`;
- registered capability count: 0; and
- production promotion ready: false.

The latest range-compression v2.1.0 and ASE/EMT reference v1.0.0 manifests each report:

- validator is agent-authored;
- trusted production promotion policy missing;
- independent validation missing;
- independent domain review missing;
- signed promotion audit missing; and
- authorized registry update missing.

These are operational dependencies and independent-authority requirements. The implementation does
not fabricate people, keys, or evidence to clear them.

## Validation

At initial implementation closeout:

- focused F10-S7 tests: 20 passed;
- capabilities + materials cross-regression: 97 passed;
- authoritative full non-Docker regression: 1234 passed, 1 skipped, 29 deselected in 794.56 s;
- warnings: 2611 existing spglib deprecation warnings only;
- Ruff: passed for the promotion module, CLI, public exports, and focused tests;
- exact readiness CLI regeneration: passed; and
- frozen F9/F10 implementation hashes: unchanged.

Final implementation identities:

- promotion module:
  `4c58ba796ecfc3f17c146fc91e1b801e2089b3d3d77c00dcbe054a4a0b5f496e`;
- promotion CLI:
  `4b7f5542e32c80ea52ddf23d0ad284397073c399f1f888f3586573b4493d42af`; and
- focused test source:
  `792591cb5d7cb887eeb2db1dd241d4912281657abb3ef53b8e70174d4a1c69aa`.

## Files

- `aletheia/capabilities/promotion.py`
- `aletheia/capabilities/__init__.py`
- `scripts/capability_promotion.py`
- `tests/capabilities/test_promotion.py`
- `configs/capabilities/f10_promotion_readiness_audit_v1.json`
- `docs/capabilities/CAPABILITY_AUTHORING_AND_PROMOTION.md`
- `docs/adr/0032-f10-signed-role-separated-capability-promotion.md`
- `environment.yml`

## Honest boundary and next work

This slice prevents a provisional capability from promoting itself. It does not provide the
independent organization required to approve a real capability, and promotion is not evidence that
the capability's scientific conclusions are correct.

The next production step is to commission one tightly bounded capability with real key custody,
an independently implemented validator, frozen adversarial/controls, domain and safety review, and
an authorized signed update. Only after that should the registered capability be used for fresh
confirmation. F10 scientific exit still requires a prospective quest, discriminating experiment,
fresh plus independent evidence, hypothesis-set change, domain audit, and private-baseline gain.
