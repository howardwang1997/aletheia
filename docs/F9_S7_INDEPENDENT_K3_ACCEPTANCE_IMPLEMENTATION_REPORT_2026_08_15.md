# F9-S7 independent K3 acceptance implementation report

- Date: 2026-08-15
- Status: Isolated engineering slice complete; F9 scientific exit not claimed
- Scope: Independent archive replay, evidence-spine scoring, claim/revision/persistence gates, and
  scheduler-facing single scoring path

## Outcome

F9-S7 now reconstructs a deterministic K3 engineering verdict from committed F9-S5 selections,
F9-S6 validations and belief updates, plus one content-addressed evidence ledger. The scorer is
frozen before selection, role-independent, has no ambient tools or raw-observation access, physically
reloads every artifact, and mechanically derives eleven complete checks and one disposition.

The slice closes three especially dangerous vacuity/gaming paths:

- `0 valid observations == 0 updates` preserves the spine but can never earn `accepted`;
- an unsupported mechanism claim cannot be issued merely because one nominal posterior is high; and
- a negative result cannot count as model change when only hypothesis prose changes while its
  falsifiable prediction remains identical.

`accepted` means the supplied committed evidence chain passes this engineering contract. It does not
mean that F9 has met its hidden-world/K2-relative or real-materials scientific exit.

## Related-work basis

The design uses:

- multiple competing hypotheses and discriminating experiments from strong inference:
  [Platt, 1964](https://pubmed.ncbi.nlm.nih.gov/17739513/);
- prediction-before-observation and sequential forecast assessment from the prequential approach:
  [Dawid, 1984](https://rss.onlinelibrary.wiley.com/doi/10.2307/2981683);
- explicit entity/activity/agent derivation and revision chains from
  [W3C PROV-O](https://www.w3.org/TR/prov-o/);
- the distinction between reproducibility and new-data replication, plus complete computational
  reporting, from
  [NASEM, 2019](https://nap.nationalacademies.org/catalog/25303/reproducibility-and-replicability-in-science); and
- lifecycle-wide preregistration/reporting/verification practices from the
  [TOP Guidelines](https://www.cos.io/initiatives/top-guidelines).

These sources motivate the process; F9-S7 does not claim conformance certification to PROV-O, TOP,
or a regulatory/repository standard.

## Implemented evidence boundary

```text
K3RoundEvidence[]
  - committed F9-S5 selection
  - exactly one committed F9-S6 validation attempt
  - zero/one committed F9-S6 update attempt

+ committed K3EvidenceLedger
  - all attempt receipts
  - source/child snapshots and belief states
  - source/revised hypothesis and prediction versions
  - revision directives/materializations
  - contradictions
  - mechanism-claim attempts
  - final terminal action and reasons

-> frozen independent deterministic scorer
-> physical reload of four archive classes
-> 11 mechanically derived checks
-> accepted / partial / rejected_integrity / blocked_execution
-> optional committed acceptance receipt
```

## Independent scorer

`K3AcceptanceScorerManifest` freezes scorer code/schema/principal, deterministic runtime,
zero-tool authority, and committed-artifact-only access. Policy and manifest must be frozen before
the first F9-S5 selection.

The scorer principal is checked against:

- persistence and terminal-decision principals;
- F9-S2 generator and duplicate reviewer;
- F9-S3 causal author and assumption reviewer;
- F9-S4 prediction author and calibration evaluator;
- F9-S5 assessor and selector harness;
- F9-S6 observation validator and validation harness; and
- F9-S6 update harness.

`aletheia.scheduler.k3_acceptance.score_k3` delegates directly to
`aletheia.epistemics.acceptance.run_k3_acceptance`. The scheduler has no alternative formula.

## Physical archive replay

Every round's selection, validation, and optional update campaign is reopened through its original
content-addressed ledger. The K3 evidence ledger is reopened separately. Canonical JSON, byte hash,
object identity, nested Pydantic derivation, and equality with the embedded wrapper are checked.

The first physical failure yields a stage-specific sanitized failure:

- `selection_archive_invalid`;
- `validation_archive_invalid`;
- `update_archive_invalid`; or
- `evidence_ledger_archive_invalid`.

Blocked campaigns contain no partial verification or acceptance check. This prevents a caller from
presenting trusted checks for the prefix before a corrupt later artifact.

## Eleven acceptance checks

### 1. Competing hypotheses

Every source snapshot must retain at least the frozen minimum active hypotheses, include null,
primary, and alternative roles, and have unique normalized statements and version identities. This
rechecks the active comparison surface rather than merely trusting a count.

### 2. Pre-observation chronology

Prediction and selection commitment must precede `observed_at`; observation must precede staging and
validation; validation commitment must precede update issue/generation/commitment. Exact receipts are
retained as evidence.

### 3. Valid-observation/update bijection

Every round contains exactly one validation attempt. `validated_confirmation` requires exactly one
update attempt; `rejected_scientific` and `blocked_execution` require none. Update request must embed
that exact validation receipt. Duplicate raw-observation receipt reuse across rounds is rejected.

The equality at zero remains a valid spine state, but the separate positive-update exit gate prevents
a vacuous full verdict.

### 4. High-belief discrimination

The scorer independently identifies hypotheses at or above the frozen prior floor and computes
pairwise total variation from the selected F9-S4 likelihood. Each round needs at least two high-
belief hypotheses and one pair at or above the discrimination floor. This is a scientific-exit gate,
not an integrity check: failure yields honest partial rather than corruption.

### 5. Belief lineage

Question and belief lineage are fixed. A successful update must be `version + 1`, bind the exact
parent belief, and preserve the source question/hypothesis/assumption/prediction objects. The next
round, when supplied, must start from the previous child snapshot.

### 6. Mechanism-claim gate

Every claim exact-binds its round, committed update, target hypothesis, requested causal ceiling,
evidence, and post-update time. Issued descriptive/association claims stay within the F9-S3 ceiling.

An issued mechanism claim additionally needs a robust update, stable world set, retained non-null
target, target posterior above policy under nominal and every likelihood-sensitivity scenario, every
alternative below exclusion policy under all those scenarios, and a requested ceiling no higher than
the causal audit. Withholding an unsupported claim is accepted; issuing it rejects integrity.

### 7. Negative-result revision

A primary-negative result must lead to `narrow` or `retire`, require a new version, and forbid
mutation. Every new-version directive needs one materialization.

Narrowing creates an exact-parent hypothesis child and an exact set of prediction children bound to
that hypothesis. Prediction stable IDs are preserved, versions increment, and at least one testable
field changes. A prose/rationale-only rewrite with an unchanged prediction is explicitly rejected.
Retirement creates the retired hypothesis child and cannot add predictions.

### 8. Contradiction retention

The persisted contradiction set must exactly equal all update queues. The scorer additionally checks
that primary-negative, fragile, all-model-low, and uninformative conditions retain their required
contradiction kinds.

### 9. Persistence completeness

The evidence ledger must contain exact sets of selection/validation/update receipts, source/child
snapshots and beliefs, hypothesis and prediction versions, revision directives, and contradictions.
Every new-version directive has exactly one chronologically valid materialization. Extra as well as
missing objects fail.

### 10. Terminal decision

One final decision persists action, reasons, evidence, principal, and time. A successful final update
also binds its receipt and world revision. Continue/fork/seek/stop actions are checked against the
F9-S6 directive; without a successful update only stop-and-archive is allowed.

### 11. Positive validated update

At least one committed update must have `updated_robust` or `updated_fragile`. A validated update
attempt blocked by an incomplete/zero-mass likelihood remains an honest partial chain, never
accepted.

## Verdict semantics

Integrity/spine checks are competing set, chronology, update bijection, lineage, claim gate,
negative-result revision, contradiction retention, persistence, and terminal decision. Any failure
produces `rejected_integrity`.

With an intact spine:

- high-belief discrimination plus at least one successful validated update yields `accepted`;
- absence of either yields `partial_no_scientific_exit`.

Physical inability to trust an archive yields `blocked_execution` before scoring.

## Canonical replay

`K3AcceptanceCampaign` validation reruns request/independence checks, validates every physical
verification receipt, recomputes all eleven checks and the disposition, and verifies chronology.
Caller-forged check status/reasons/verdicts are rejected.

Evidence and acceptance campaigns each have commit/load wrappers with canonical content-addressed
JSON and explicit non-backdated commit times. Both archive types have byte-tamper tests.

## Acceptance evidence

Focused F9-S7 acceptance:

```text
26 passed in 149.25 s
```

The suite covers:

- complete accepted chain and exact physical receipts;
- zero-update nonvacuity and valid-observation/update mismatch;
- high-belief discrimination partial semantics;
- likelihood-blocked update as honest partial;
- positive and primary-negative chains;
- missing negative revision and prose-only/no-new-prediction attacks;
- unauthorized mechanism issuance, safe withholding, bounded association claim, robustly authorized
  mechanism claim, and predated claim;
- terminal/world-action conflict;
- exact persistence sets;
- selection, validation, update, and evidence archive absence without partial scoring;
- scorer freeze and role independence;
- no raw-observation/tool path;
- scheduler/core scorer identity;
- derived-check forgery rejection; and
- evidence/acceptance archive round trips and byte tampering.

All F9 epistemics tests through F9-S7:

```text
202 passed in 295.32 s
```

Repository-wide acceptance:

```text
non-Docker: 1119 passed, 1 skipped, 29 deselected in 652.80 s
Docker:       29 passed, 1120 deselected in 26.78 s
```

Ruff lint, Ruff format check, compilation, the focused F9-S7 suite, all F9 epistemics tests, the
complete non-Docker repository, and the real-Docker matrix all passed. Unlike the preceding F9-S6
acceptance run, the final F9-S7 Docker matrix passed on its first execution.

## Files added or materially changed

- `aletheia/epistemics/acceptance.py`;
- `aletheia/epistemics/__init__.py`;
- `aletheia/scheduler/k3_acceptance.py`;
- `tests/epistemics/f9s7_fixtures.py`;
- `tests/epistemics/test_k3_acceptance.py`;
- `docs/adr/0022-f9-independent-k3-evidence-chain-acceptance.md`;
- `docs/epistemics/INDEPENDENT_K3_ACCEPTANCE.md`; and
- this report, README, docs index, F9-S6 guide/report, and F7–F12 master-plan status.

## Explicit non-guarantees

The first two integration gaps below described the F9-S7 delivery boundary. F9-S8 subsequently
closed them with an atomic PostgreSQL transition, typed event, and verified second-round F9-S3
consumer; see `F9_S8_TRANSACTIONAL_WORLD_MODEL_CONTINUATION_IMPLEMENTATION_REPORT_2026_08_15.md`.
F9-S9 subsequently implemented the hidden-world matrix, truth-relative endpoints, and gate, but no
live/private result has passed it.

- no frozen hidden-world K3-versus-K2 comparison, posterior calibration gate, false-mechanism rate,
  multi-repeat statistical acceptance, or `real_k3_hidden_world_e2e.py` implementation;
- no real materials alternatives → experiment → update chain;
- no authenticated laboratory/instrument/custody/measurement evidence;
- the F9-S7 evidence ledger itself is still content-addressed; F9-S8 supplies the bound database
  transition/event transaction;
- retirement still lacks an automatic F9-S2 hypothesis-set fork/replenishment path;
- no retry/authoritative-validation-attempt protocol beyond one validation attempt per round;
- no parser that compares actual manuscript mechanism text to claim records;
- no calibrated universal belief/discrimination/claim thresholds;
- no requirement that evidence-chain acceptance itself be robust rather than fragile; fragile updates
  cannot authorize mechanism claims, and stricter consumers may cap them;
- no execution of continue/fork/seek/stop actions;
- no F10 registered capability, experiment engine, replication workflow, publication gate, or F9/F10
  scientific exit; and
- no evidence that a real hypothesis, causal effect, mechanism, novelty, SOTA, or publication claim
  is true.

## Next work

F9-S8 completed items 1–3 below. Remaining highest-value work is the scientific-exit harness and
domain integration:

1. ~~materialize committed child snapshots and revision children into transactional F9 persistence~~;
2. ~~let the next F9 round consume that exact child snapshot~~;
3. ~~project typed receipts into scheduler events without a second verdict formula~~;
4. ~~implement and freeze the K3 hidden-world/K2 ablation suite with repeated calibration and
   false-mechanism endpoints~~ (F9-S9 engineering complete; live/private evidence still blocked); and
5. connect accepted K3 selections to F10 registered experiment capabilities, starting with the
   materials domain.

**Subsequent status (2026-08-15):** F9-S9 added the separate headline/K2/K3 matrix, evaluator-owned
truth-relative endpoints, signed raw-evidence aggregation, paired statistics, pre-validation
threshold policy, and custody-aware scientific-exit gate. It has not run the live/private matrix,
so the scientific exit remains open.
