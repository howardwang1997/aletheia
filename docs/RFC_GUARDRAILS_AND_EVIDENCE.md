# RFC: Guardrails and Evidence Layer

## Status

Draft. Phase A guardrails are partially implemented.

## Motivation

Aletheia's biggest near-term risk is not lack of autonomy. It is false confidence: the system may
produce a polished research artifact when required evidence is missing. This RFC defines the next
engineering phase: make real runs fail closed and add claim-to-evidence tracking.

## Goals

- Prevent real runs from completing when critic review, literature grounding, or supported domain
  setup is missing.
- Represent claims and evidence explicitly.
- Make critic review operate on evidence packages, not only natural-language summaries.
- Ensure reports cannot make strong claims without backing artifacts.

## Non-Goals

- Full knowledge graph.
- Full campaign planner.
- New AI application domains.
- Public submission workflow.
- Large UI redesign.

## Current Guardrails

Already implemented:

- `CriticGateway` rejects real review when no real critic providers are available.
- `get_domain_plugin(domain, strict=True)` rejects unknown non-empty domains.
- `ExperimentDriver` uses strict domain resolution for real runs.
- `ExperimentDriver` pauses real runs before ideation when survey produces no citable grounding.

Relevant files:

- `aletheia/critics/gateway.py`
- `aletheia/domains/registry.py`
- `aletheia/scheduler/driver.py`
- `tests/test_critics.py`
- `tests/test_domain_registry.py`
- `tests/test_research_frontend.py`

## Proposed Data Model

### Claim

Add a ledger table for claims.

Fields:

- `id`
- `run_id`
- `experiment_id`
- `claim_text`
- `claim_type`
- `strength`
- `status`
- `created_by`
- `stage`
- `created_at`

Allowed `claim_type` values:

- `novelty`
- `sota`
- `metric`
- `mechanism`
- `limitation`
- `reproducibility`
- `cost`
- `safety`

Allowed `strength` values:

- `speculative`
- `weak`
- `moderate`
- `strong`

Allowed `status` values:

- `proposed`
- `supported`
- `refuted`
- `unverified`

### ClaimEvidence

Add a join table from claims to evidence refs.

Fields:

- `id`
- `claim_id`
- `evidence_kind`
- `evidence_ref`
- `note`

Allowed `evidence_kind` values:

- `paper`
- `metric`
- `artifact`
- `critique_panel`
- `experiment`
- `dataset`
- `code`
- `reproduction`

### LiteratureFinding

Add a structured literature finding table or JSON ledger record.

Fields:

- `id`
- `run_id`
- `paper_id`
- `query`
- `method`
- `dataset`
- `metric`
- `result`
- `limitation`
- `gap`
- `relevance`
- `source`

### SOTAResult

Add a structured SOTA table.

Fields:

- `id`
- `domain`
- `task`
- `dataset`
- `metric`
- `score`
- `method`
- `paper_id`
- `split_policy`
- `notes`

## API and Service Changes

Add service helpers in `aletheia/memory/service.py`:

```python
def create_claim(...)
def attach_claim_evidence(...)
def list_claims(run_id: str, experiment_id: str | None = None) -> list[dict]
def record_literature_finding(...)
def list_literature_findings(run_id: str) -> list[dict]
def record_sota_result(...)
def list_sota_results(domain: str, task: str | None = None) -> list[dict]
```

Add API endpoints:

- `GET /runs/{run_id}/claims`
- `GET /runs/{run_id}/literature`
- `GET /runs/{run_id}/sota`

## Driver Changes

### Survey Stage

The survey stage should output:

- prose briefing,
- structured literature findings,
- SOTA rows,
- open gaps,
- citable paper refs.

Real run blocking rules:

- no citable papers -> pause,
- no structured findings -> pause or mark degraded,
- no SOTA rows -> allow only if report says no comparable SOTA,
- failed retrieval -> pause.

### Ideate Stage

Each hypothesis should create proposed claims:

- novelty claim,
- feasibility claim,
- expected contribution claim.

Each hypothesis should have evidence refs to:

- open gaps,
- prior work,
- dataset fit,
- SOTA table if relevant.

### Design Stage

The design should create or update claims about:

- method suitability,
- evaluation validity,
- baseline adequacy.

### Analysis Stage

The analysis stage should create metric and mechanism claims. Metric claims must reference eval
artifacts and exact metric keys.

### Write-Up Stage

The write-up prompt should receive a claim table. It may only make strong claims from supported claims.
Speculative or unverified claims must be labeled as such.

## Critic Changes

Critic input should include:

- target artifact,
- claim list,
- evidence refs,
- missing evidence markers,
- protocol status,
- degraded/fallback flags.

Critic output should reference claim ids when possible.

Example finding:

```json
{
  "severity": "major",
  "category": "novelty",
  "claim_id": "claim_123",
  "claim": "Novelty is overstated",
  "evidence": "SOTA table includes a similar 2024 method on the same dataset",
  "suggestion": "Downgrade claim strength to weak and cite the prior method"
}
```

## Report Rules

The report generator must obey:

- No strong novelty claim without literature evidence.
- No SOTA claim without comparable SOTA row.
- No method claim unless executed implementation matches or mismatch is stated.
- No headline result unless protocol status is valid.
- No unsupported causal mechanism claim.

## Testing Plan

### Guardrail Tests

- Real run without critic providers pauses or fails gate.
- Real run with unknown domain pauses.
- Real run without citable literature pauses.
- Evaluation protocol degradation prevents strong headline claims.

### Evidence Tests

- Major report claims have claim rows.
- Unsupported claims are downgraded.
- Metric claims reference eval artifacts.
- SOTA claims require SOTA rows.
- Critic findings can reference claim ids.

### Regression Tests

- Dry-run still completes with clear dry-run markers.
- Materials and molecules still pass existing e2e tests.
- Campaign tests still pass with claim records attached.

## Rollout Plan

1. Add schema and service helpers.
2. Add claim creation in analysis and write-up first.
3. Add literature findings and SOTA rows.
4. Feed claims into critic gateway.
5. Enforce report rules.
6. Surface claim/evidence state in API and dashboard.

## Acceptance Criteria

This RFC is complete when:

- Real runs cannot complete with missing critic, domain, or literature grounding.
- Every major report claim has evidence records.
- Critics can inspect and challenge specific claims.
- Strong claims are blocked or downgraded without sufficient evidence.
- Existing dry-run and campaign tests continue to pass.

