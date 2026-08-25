# ADR-0001: independent Frontier Discovery evaluation plane

- Status: accepted
- Date: 2026-08-13
- Scope: F7 Frontier Discovery Gate

## Decision

The research process and evaluator run in separate workspaces and security principals. Aletheia
receives only the public task view and may write only a content-addressed submission envelope to a
one-way inbox. It cannot read hidden assets, scorer code, gold labels, raw score internals, or prior
test attempts. The evaluator may read the submission and hidden assets, execute a pinned scorer,
and write an immutable scorer receipt; it cannot edit the research ledger or submission.

Every suite, task, system, submission, scorer, score, and receipt has a canonical SHA-256 identity.
Evaluator-issued receipts bind the exact task manifest, system manifest, submission, scorer, and
evaluator manifest.
Evaluation storage uses credentials distinct from the research ledger even when both are backed by
Postgres.

Retries are permitted only for evaluator-classified infrastructure failures. Scientific failures,
timeouts caused by the submitted system, protocol breaches, and invalid submissions remain visible
attempts and are never automatically replaced. Test access is counted and every attempt is reported.

## Threat model

| Threat | Required control | Terminal classification |
|---|---|---|
| Training/public benchmark contamination | corpus cutoff, overlap disclosure, private prospective tasks | contaminated or invalid |
| Hidden path guessing, traversal, symlink escape | disjoint roots, canonical-path checks, container mounts | invalid: hidden asset access |
| Prompt injection inside task or submission | scorer treats submission bytes as data; no evaluator tools exposed | invalid if boundary crossed |
| Scorer hacking or forged score JSON | pinned scorer hash and evaluator-signed/content-addressed receipt | invalid: forged receipt |
| Best-of-N / omitted attempts | pre-registered repeat indices and append-only attempt family | invalid: undeclared attempt |
| Repeated test viewing | test-access counter, one-time private tasks, retirement policy | invalid: contamination |
| Fake retry through abnormal exit | evaluator assigns infra failure independently; retry lineage retained | original result retained |
| Seed or clock manipulation | evaluator supplies seed/clock and records runtime receipt | invalid: protocol breach |
| Evaluator crash | no scientific verdict; explicit infra failure; bounded retry | infrastructure failure |
| Missing or unverifiable artifacts | expected-artifact contract plus byte/hash verification | invalid: missing artifact |

## Invalid versus scientifically false

`scientific_failure` means the protocol was valid and the submitted hypothesis/method did not meet
the pre-registered scientific criterion. It is useful negative evidence and is not retried.

`invalid` means no scientific inference is permitted because the attempt violated provenance,
isolation, contamination, resource, or artifact rules. An invalid attempt cannot simultaneously be
scored as scientifically true or false. `infra_failure` means the independent evaluator or platform
failed before a valid verdict; only this class permits an automatic retry, and both attempts remain
in the ledger.

## Consequences

- The research agent cannot optimize against hidden test feedback during development.
- Local all-in-one evaluation is allowed only as a non-release developer mode and cannot satisfy F7.
- Adapters must translate external benchmarks into the same public-task/submission/receipt contract.
- Operational complexity increases because evaluator credentials, storage, images, and deployments
  are separate; this is accepted because self-grading cannot establish frontier-science capability.
