# Autonomous Research Roadmap

## Objective

Aletheia's long-term goal is to conduct frontier AI research end to end. Humans should provide high
level direction, data, credentials, budgets, and final approval. The system should handle literature
review, hypothesis generation, experiment design, implementation, execution, evaluation, iteration,
write-up, and artifact packaging.

This roadmap is written from the end state backward. The goal is not to make the agent appear more
autonomous quickly. The goal is to keep Aletheia on a path where every increase in autonomy is matched
by stronger evidence, provenance, and scientific judgment.

## North Star

A mature Aletheia run should produce a research bundle containing:

- research question and hypothesis,
- structured literature review,
- novelty and SOTA analysis,
- data card,
- experiment plan,
- implementation code,
- evaluation artifacts,
- baseline and ablation results,
- reproduction results,
- critic reviews,
- claim-to-evidence map,
- report draft,
- GitHub PR or reproducibility package.

The bundle should make it clear what is supported, what is speculative, what failed, and what should be
done next.

## Non-Negotiable Principles

### Fail Closed

If real scientific grounding is missing, the system pauses. It does not continue with a polished but
unsupported report.

### Evidence Before Prose

Reports express evidence. They do not create evidence.

### Agent Generates, Harness Verifies

LLMs may propose hypotheses, code, analyses, and reports. Fixed system components must compute metrics,
enforce protocols, persist artifacts, and determine gate status.

### Claim Strength Must Match Evidence

Every claim must be categorized as speculative, weak, moderate, or strong based on reproducibility,
statistical support, literature support, and critic review.

## 3-Month Plan: Credible Research MVP

### Goal

Turn Aletheia from an end-to-end autonomous experiment demo into a credible research MVP that refuses
to produce strong conclusions when evidence is missing.

### Workstream 1: Fail-Closed Guardrails

Status: partially implemented.

Already implemented:

- Real critic gates reject when no real reviewer providers are available.
- Real runs reject unknown non-empty domains instead of falling back to materials.
- Real runs pause before ideation if survey produces no citable grounding.

Next guardrails:

- Pause when grouped evaluation falls back to plain KFold.
- Pause or downgrade when executed implementation differs from requested frontier method.
- Pause when SOTA comparison is unavailable but the report attempts a SOTA claim.
- Pause when result artifacts are missing.
- Add dashboard labels for dry-run, degraded, blocked, fallback, and unverified states.

Acceptance criteria:

- A real run cannot complete with missing critic review.
- A real run cannot complete with no literature grounding.
- A real run cannot silently execute the wrong domain.
- A real report cannot contain strong novelty or SOTA claims without evidence.

### Workstream 2: Evidence Ledger v1

Add a structured claim and evidence layer.

Minimum schema:

```python
class ClaimEvidence:
    claim_id: str
    run_id: str
    experiment_id: str | None
    claim_text: str
    claim_type: str
    strength: str
    status: str
    evidence_refs: list[str]
    limitations: list[str]
    created_by: str
```

Claim types:

- novelty,
- SOTA,
- metric,
- mechanism,
- limitation,
- reproducibility,
- cost,
- safety.

Acceptance criteria:

- Each report's major claims map to evidence records.
- Unsupported claims are marked unverified or speculative.
- Critic review can inspect evidence packages directly.

### Workstream 3: Structured Literature v1

Upgrade literature from prose briefing to structured research memory.

Store:

- query,
- source,
- paper id,
- title,
- authors,
- year,
- venue,
- DOI or URL,
- abstract,
- extracted methods,
- datasets,
- metrics,
- results,
- limitations,
- open gaps.

Acceptance criteria:

- Hypothesis novelty is checked against concrete prior work.
- SOTA comparison references structured rows, not hand-written profile text alone.
- Write-up citations can only use retrieved papers.

### Workstream 4: Hypothesis Scorecard

Every hypothesis should be scored before execution.

Score dimensions:

- novelty,
- feasibility,
- expected information gain,
- SOTA relevance,
- dataset fit,
- evaluation clarity,
- cost risk,
- interpretability of failure.

Acceptance criteria:

- Low novelty or unclear evaluation blocks execution.
- Campaign continuation decisions use expected information gain.
- The scorecard is persisted and visible to critics.

### Workstream 5: Evaluation Hardening

Improve `domains/protocol.py` and domain plugins.

Add:

- seed reruns,
- confidence intervals,
- baseline win/loss/tie summaries,
- explicit protocol degradation markers,
- grouped OOF error stratification,
- artifact completeness checks.

Acceptance criteria:

- Headline results use the strongest honest protocol available.
- Protocol fallback is never hidden.
- Strong claims require multiple seeds or reproduction.

## 6-Month Plan: Autonomous Research Campaigns

### Goal

Move from single-loop automation to multi-experiment research campaigns that ask better questions over
time.

### Workstream 1: Experiment Search Engine

Add a planner that proposes next experiments based on:

- open gaps,
- prior results,
- critic findings,
- expected information gain,
- budget remaining,
- failed assumptions,
- unresolved claims.

Experiment types:

- baseline establishment,
- ablation,
- method comparison,
- data scaling,
- robustness test,
- failure analysis,
- reproduction,
- SOTA attempt.

Acceptance criteria:

- Every new experiment answers a named open question.
- The system can stop when marginal value is low.
- Campaign summaries explain the research trajectory, not just final metrics.

### Workstream 2: Reproduction Pass

Strong claims should require independent reproduction.

Reproduction modes:

- different seed,
- regenerated code,
- locked code rerun,
- baseline-only sanity check,
- alternate implementation check.

Acceptance criteria:

- Reproduction failure downgrades claim strength.
- Reports distinguish original and reproduced metrics.
- Critic gate sees reproduction artifacts.

### Workstream 3: Domain Plugin v2

Upgrade plugins from model/evaluation adapters to research task definitions.

Add plugin methods for:

- task schema,
- data requirements,
- supported evaluation protocols,
- baseline suite,
- SOTA sources,
- claim rules,
- failure modes,
- artifact requirements.

Acceptance criteria:

- New domains can be added without changing the driver core.
- Unsupported claim types are blocked by domain policy.
- Domain-specific evaluation is explicit and testable.

### Workstream 4: AI Application Domains

Add non-regression AI application domains.

Priority domains:

- RAG and search systems,
- agent workflows,
- LLM evaluation and prompt optimization.

RAG metrics:

- answer quality,
- faithfulness,
- citation accuracy,
- retrieval recall,
- latency,
- cost.

Agent workflow metrics:

- task success,
- tool error rate,
- cost,
- latency,
- human intervention count,
- regression risk.

Acceptance criteria:

- At least one non-tabular AI application domain runs end to end.
- Evaluation does not rely on a single uncalibrated LLM judge.
- Reports show quality, cost, latency, and reliability tradeoffs.

### Workstream 5: Critic Panel v2

Critics should review structured evidence packages.

Gate targets:

- literature quality,
- hypothesis novelty,
- experiment design,
- implementation correctness,
- metric validity,
- claim support,
- report honesty.

Acceptance criteria:

- Critics cite specific claim ids and evidence refs.
- Disagreement is tracked by evidence conflict, not just verdict.
- Reviewer absence or low reviewer coverage blocks real runs.

## Long-Term Plan: Autonomous Research Lab

### Goal

Build a long-running autonomous lab that maintains a research agenda, accumulates memory, selects
projects, runs experiments, and prepares reproducible research outputs.

### Workstream 1: Research Knowledge Graph

Move beyond vector recall.

Core nodes:

- paper,
- method,
- dataset,
- benchmark,
- metric,
- claim,
- hypothesis,
- experiment,
- artifact,
- critique,
- research gap,
- SOTA result.

Core queries:

- Has this idea been tried?
- Which datasets support this claim?
- Which methods dominate this benchmark?
- Why did past attempts fail?
- Which open gaps remain?

### Workstream 2: Research Program Planner

Support multi-project planning.

Capabilities:

- rank candidate research directions,
- allocate budget across projects,
- balance exploration and exploitation,
- schedule reproductions,
- retire unpromising directions,
- write periodic research reviews.

### Workstream 3: Strong Validation

Add stronger validation according to domain.

Examples:

- hidden test sets,
- external benchmark submissions,
- human expert review,
- independent implementations,
- statistical significance tests,
- robustness stress tests,
- real-world deployment evaluations.

### Workstream 4: Multi-Agent Lab Roles

Separate responsibilities into explicit roles:

- Principal Investigator,
- Librarian,
- Methodologist,
- Engineer,
- Evaluator,
- Statistician,
- Reviewer,
- Writer,
- Archivist.

Each role should have separate context, permissions, and output schemas. Reviewers should see evidence
artifacts, not hidden author reasoning.

### Workstream 5: Publication Workflow

Support research outputs such as:

- paper draft,
- reproducibility bundle,
- GitHub PR,
- experiment card,
- model card,
- dataset card,
- reviewer response,
- revision experiments.

Public submission remains human-approved.

## Implementation Order

1. Finish fail-closed guardrails.
2. Add evidence ledger.
3. Add structured literature and SOTA tables.
4. Add hypothesis scorecards.
5. Add claim-aware write-up.
6. Add reproduction pass.
7. Add experiment search engine.
8. Add domain plugin v2.
9. Add AI application domains.
10. Add research knowledge graph and program planner.

## Success Criteria

### 3 Months

- Real runs pause instead of overclaiming when required evidence is missing.
- Major claims have evidence records.
- Literature and SOTA are structured enough for critic review.
- Materials and molecules still run end to end.

### 6 Months

- Multi-experiment campaigns are driven by information gain.
- At least one AI application domain runs end to end.
- Strong claims require reproduction.
- Critics review evidence packages.

### Long Term

- Aletheia can maintain a research agenda.
- It can accumulate cross-run knowledge.
- It can select high-value projects.
- It can produce reproducible, reviewable research bundles with limited human supervision.

