# F10-S1 capability registry and replication implementation report

Date: 2026-08-15
Status: Core engineering and real matrix complete; capability remains provisional

## Outcome

Aletheia can now represent a scientific experiment as an immutable, discoverable, versioned
capability contract; plan against it without observation access; reject unsupported or over-strong
requests; and execute a fully preregistered stochastic matrix without early stopping or selecting
favorable runs.

The first real matrix completed successfully and produced an unfavorable-but-informative result:
`partition_sensitive`. This does not promote the capability and does not establish a mechanism.

## Implemented

- `aletheia/capabilities/schemas.py`: lifecycle, evidence/claim ceilings, exact schemas, four role
  bindings, controls, assumptions, failures, sampling, resources, stochastic policy, reproduction,
  safety, approval, license/egress, and promotion evidence.
- `aletheia/capabilities/registry.py`: immutable snapshots, physical archive commitments, exact
  lookup, semantic-version chains, lifecycle monotonicity, major-version schema compatibility, and
  fail-closed unsupported behavior.
- `aletheia/capabilities/planner.py`: observation-blind exact query, complete blocker audit, and no
  fuzzy fallback.
- `aletheia/domains/materials/capabilities/replication.py`: five-slot plan, two exact
  recomputations per slot, checkpoint-complete evidence, descriptive heterogeneity aggregation,
  signature/derivation replay, and optional full physical audit.
- `scripts/capability_registry.py` and `scripts/real_materials_replication_e2e.py`: create-only
  validation, freezing, inspection, execution, resumption, aggregation, and verification commands.

## Append-only contract correction

The frozen v1 output schema described a summary rather than the actual executor result. It was not
edited. Version 2.0.0 changes the input, output, and matrix-preregistration schemas, binds the exact
v1 manifest hash as its predecessor, and is stored with both versions in registry snapshot
`56c167d9...`. Unit tests use JSON Schema to validate the actual Pydantic objects and prove that the
old output schema rejects the real executor result.

## Capability gates

The exploratory query selects v2.0.0 exactly. A confirmatory query returns `unsupported` with both
`capability_not_registered` and `evidence_level_insufficient`. Provisional manifests cannot support
mechanism or experimental-causal claims. Registered manifests require positive and negative
controls, a non-agent-authored validator, distinct domain reviewer and promotion auditor, and all
promotion receipts.

## Real matrix

All five seeds were frozen in plan `55449225...` before measurement. Every slot completed one signed
measurement and two separately keyed physical recomputations. A third audit physically reran all
five slots and exactly matched every result; signatures, belief updates, and aggregation also
replayed.

| Outcome | Slots |
|---|---:|
| unseen-system-specific compression | 2 |
| generic model shrinkage | 2 |
| ambiguous | 1 |
| no material compression | 0 |

The five point deltas are all positive, but only two cluster-bootstrap intervals exclude zero. With
the frozen 4/5 rule there is no consensus. Same-dataset partitions were not combined as independent
Bayesian evidence.

## Verification

- Capability and replication focused tests: `11 passed` before the live matrix.
- Real matrix: five observations, ten validator recomputations, five derived updates, one complete
  aggregation, no retained failures.
- Final audit: signatures valid, all updates derived, aggregation derived, and all five slots
  physically recomputed.
- Targeted Ruff and formatting checks: pass.

## Remaining gates

F10-S1 core is present, but the broader F10 engineering exit is not. The capability stays
provisional because validation remains agent-authored/local, no independent domain reviewer has
signed it, the benchmark is public and retrospective, source-specific licensing has not received
independent legal review, and there is no external dataset/site replication.

F10-S2 has since implemented the general raw output → typed parser → candidate observation →
independent validator → validated observation pipeline; see
`F10_S2_TYPED_OBSERVATION_PIPELINE_IMPLEMENTATION_REPORT_2026_08_15.md`. The next slice is F10-S3
materials identity and measurement audit.
