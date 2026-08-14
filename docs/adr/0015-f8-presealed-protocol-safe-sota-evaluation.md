# ADR 0015: Pre-sealed, protocol-safe SOTA evaluation and manuscript headline gating

- Status: Accepted
- Date: 2026-08-15
- Scope: F8-S6 / Knowledge Boundary Engine

## Context

The issue-12 schema spike can compare two exact `ProtocolSignature` objects, but that pairwise
contract is not sufficient for a defensible state-of-the-art claim. A system could still create a
false headline by:

- choosing one weak reference after seeing the candidate result;
- copying a published scalar whose data bytes, split, preprocessing, metric, or budget differ;
- letting the experiment author type candidate and reference scores into the comparison;
- omitting a failed reference reproduction;
- using different random seeds or evaluation partitions for candidate and references;
- reporting a favorable mean without paired uncertainty or multiple-comparison correction;
- calling a statistically detectable but scientifically negligible change an advance;
- reusing one execution or prediction artifact as if it came from two methods;
- editing the matrix verdict or manuscript claim after evaluation;
- falling back to the legacy “best structured row” shortcut when the audited path errors.

Related primary work supports a protocol-first boundary. “Show Your Work” argues that a test score
alone is insufficient and that model selection, resources, and evaluation choices must be exposed:
[Show Your Work](https://aclanthology.org/D19-1224/). Demšar describes paired tests and corrections
for comparing multiple classifiers rather than treating isolated means as conclusive:
[Statistical Comparisons of Classifiers over Multiple Data Sets](https://www.jmlr.org/papers/volume7/demsar06a/demsar06a.pdf).
MLCommons uses fixed benchmark rules, required scenarios, submission checking, and auditability:
[MLCommons Inference submission guide](https://docs.mlcommons.org/inference/submission/).
OpenML benchmark suites freeze tasks, data, splits, and evaluation procedures for reproducible
runs: [OpenML benchmarks](https://docs.openml.org/benchmark/). The Benchmark Lottery shows that
benchmark choice can materially change model rankings:
[The Benchmark Lottery](https://openreview.net/pdf?id=5Str2l1vmr-). Matbench provides a scientific
example of fixed data/split evaluation against reference algorithms:
[Matbench](https://www.nature.com/articles/s41524-020-00406-3).

These sources motivate the engineering controls. They do not establish that the three synthetic
references in Aletheia's tests are the real frontier of any domain.

## Decision

### Seal the reference set before the candidate protocol and result

`SOTAReferenceRegistry` is tied to one authorized F8-S5 `ResearchDirectionGate`, its exact
calibrated coverage object, reference-search session, and corpus snapshot. It includes:

- a frozen selection-protocol hash;
- an evidence cutoff and later seal time;
- the sorted candidate-author identities;
- at least two sorted, author-excluded selector identities;
- at least three canonical required reference entries;
- for each reference, its exact protocol, source-paper snapshot, result evidence spans, selection
  evidence, independent inclusion reviewer, and review receipt.

Reference IDs, entry identities, protocol identities, source papers, and review receipts are
unique. Candidate authors cannot select references or review their inclusion. A reference result
reported after the evidence cutoff or selected after sealing is invalid. The registry must be
sealed before the comparison policy and candidate protocol are frozen.

Registry construction and campaign revalidation also require every reference paper to be present
in the bound F8-S1 corpus and every result-evidence span to belong to that exact paper. Merely typing
an arbitrary 64-character paper or span hash cannot close the evidence chain.

Three references are an engineering anti-cherry-picking floor, not proof that a registry is
complete. Production selection must additionally follow a domain-specific preregistered search and
inclusion protocol.

### Compare exact protocols, not names

Every candidate and reference uses the existing `DatasetVersion`, `MetricDefinition`,
`ResourceBudgetSignature`, and `ProtocolSignature` contracts. Comparability covers:

- task definition;
- dataset version, content bytes, and schema;
- split policy and split bytes;
- grouping and leakage controls;
- preprocessing and exclusions;
- metric formula, aggregation, direction, unit, and valid range;
- uncertainty and statistical procedure;
- compute/data/hardware/cost budget;
- external resources and pretraining.

Evaluation-date differences remain visible temporal context but are not, by themselves, blocking.
Every other mismatch is blocking. A non-comparable row contains no delta and can never contribute
to a SOTA headline.

### Freeze an unprivileged evaluator and sign every result

`SOTAEvaluatorManifest` freezes evaluator code, score parser, aggregation policy, statistical
policy, repeat floor, and receipt-key identity. It has no tool authority. F8-S6 currently fixes
aggregation to the arithmetic mean of canonical frozen repeats and statistics to an exact
one-sided paired sign test followed by Holm step-down correction.

Every successful `SignedBenchmarkResultReceipt` binds:

- exact protocol and metric identities;
- exact evaluator identity;
- contiguous repeat ordinal and ID;
- evaluation-partition hash;
- finite score;
- unique execution-receipt and prediction-artifact hashes;
- exact aggregate and completion time.

The HMAC-SHA-256 key must contain at least 32 bytes. Failed runs emit an explicit error receipt with
the exception class and message hash, never raw error text or an invented score. Candidate and all
references must use identical ordered `(replicate_id, partition_hash)` pairs. Execution and
prediction artifacts cannot be shared across methods.

HMAC is the current local evaluator/reporter boundary. Production deployments should place the key
in evaluator-owned secret storage; a later public-verification deployment may replace it with a
KMS-backed asymmetric receipt without weakening the identity rules.

### Require both statistical and practical superiority over every reference

`SOTAComparisonPolicy` is frozen before the candidate protocol. The default engineering floor is:

- at least three required references;
- at least ten paired repeats;
- exact one-sided paired sign tests;
- Holm correction across all comparable reference rows at alpha 0.05;
- a positive, preregistered minimum practical improvement;
- every reference comparable, successful, and beaten.

For higher-is-better metrics, a positive paired difference is `candidate - reference`; for
lower-is-better metrics it is `reference - candidate`. Ties remain in the ledger but are excluded
from the sign-test trial count; the frozen policy defines an absolute tie tolerance of `1e-12`.
The binomial tail is accumulated as exact integers before its final floating representation so the
allowed 10,000-repeat boundary does not overflow. A row is `beats_reference` only when its
Holm-adjusted p-value is no greater than alpha and its mean favorable delta reaches the practical
margin.

The global `sota_confirmed` verdict requires every sealed row to beat its reference. One unbeaten
reference produces `sota_not_demonstrated`; any failed result or blocking protocol mismatch
produces `sota_blocked_evidence`. The corresponding claim ceilings are `moderate`,
`comparative_only`, and `none`. F8-S6 never emits a strong or unbounded headline.

The sign test is deliberately assumption-light and exact for the current repeat contract. It does
not claim optimal power. A domain protocol may justify a different preregistered paired test only
through a new versioned statistical-policy contract and validation suite.

### Re-derive the matrix and archive it immutably

`SOTAEvaluationCampaign` contains the direction gate, registry, policy, evaluator, candidate
protocol, every signed result, every matrix row, global verdict, blockers, headline bit, and claim
ceiling. Construction and model validation re-derive the complete matrix and decision. Missing,
reordered, duplicated, reused, or incorrectly paired results fail before a headline exists.

Commit/load uses the content-addressed knowledge ledger. Load checks canonical JSON, object
identity, all Pydantic derivations, and every result signature.

### Bind manuscript claims to the exact campaign

`screen_auditable_sota_campaign` revalidates campaign structure and signatures, then requires the
current manuscript result to match:

- exact candidate `ProtocolSignature` hash;
- metric ID, canonical name, or declared alias;
- signed candidate aggregate within an absolute tolerance of `1e-12`;
- performance contribution type.

An authorized claim is at most `moderate/supported`. An unbeaten reference becomes
`weak/refuted`; missing or non-comparable evidence becomes `weak/unverified`.

`ExperimentDriver` accepts an optional `auditable_sota_campaign_fn`. When configured, WRITE_UP
consumes the typed decision and campaign/receipt/row hashes. Provider exceptions, invalid return
types, wrong keys, identity mismatch, or missing protocol identity fail closed and do not fall back
to the legacy scalar-row path. The legacy path remains only for deployments that do not configure
F8-S6; it must not be described as audited SOTA acceptance.

## Consequences

- A candidate cannot choose its comparison set after seeing its score.
- Dataset aliases or equal-looking metric names cannot conceal different bytes or formulas.
- Failed reproductions remain visible and block evidence instead of disappearing.
- The matrix uses paired frozen partitions and rejects artifact reuse.
- Nominal per-reference significance cannot bypass Holm correction.
- Tiny consistent gains cannot bypass the practical-improvement threshold.
- One weak or non-comparable reference blocks the global headline.
- A manuscript cannot rebind a valid campaign to another protocol, metric, score, or contribution
  type.
- The synthetic fixture demonstrates invariant enforcement only. It does not establish a real
  state of the art, reproduce a published system, or show that an Aletheia method is superior.

## Rejected alternatives

- **Compare only the best published scalar:** it hides protocol incompatibility and selection.
- **Let the candidate author select references:** it makes the target adaptive and rewards
  cherry-picking.
- **Accept dataset and metric names as identity:** names and aliases do not identify bytes, split,
  formula, or resource regime.
- **Use independent unpaired repeats:** paired differences would no longer isolate method changes
  on the same frozen evaluation conditions.
- **Report a favorable mean as SOTA:** it ignores uncertainty and multiplicity.
- **Use p-value alone:** a negligible effect can be statistically detectable.
- **Let one beaten reference imply global SOTA:** the untested or unbeaten sealed references remain
  counterevidence.
- **Drop evaluator errors:** absence would be converted into favorable evidence.
- **Let WRITE_UP fall back after audited-gate failure:** a stricter path would become optional exactly
  when it matters.
- **Replace the legacy scheduler path immediately:** current deployments do not automatically
  materialize F8 protocols and receipts; an unconditional switch would break them or encourage
  placeholder identities.
