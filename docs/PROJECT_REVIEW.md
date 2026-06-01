# Aletheia Project Review

## Purpose

This document is a neutral technical review of Aletheia as an autonomous AI scientist system. It is
intended for Claude, Codex, and human reviewers who need a grounded view of what the project already
does well, where it is fragile, and what must improve before it can credibly produce frontier research.

## Current Position

Aletheia is not a toy demo. It already has a serious autonomous research skeleton:

- A deterministic research lifecycle.
- Literature survey and hypothesis generation stages.
- Experiment design, code generation, execution, analysis, optimization, and write-up.
- Cross-model critic gates.
- A Postgres ledger and event stream.
- Domain plugins for materials and molecules.
- Leakage-aware evaluation protocols.
- Budget limits, RBAC, GitHub App integration, and sandboxed execution.

The correct current classification is:

> Aletheia is an early research operating system for AI-assisted scientific experimentation. It is
> not yet a mature AI Scientist capable of reliably producing frontier research from arbitrary ideas
> or datasets.

## Strengths

### Deterministic Lifecycle

The project uses an explicit lifecycle rather than free-form autonomous wandering:

```text
survey -> ideate -> experiment_design -> code -> execution -> analysis -> optimize -> write_up -> archive
```

This is the right shape for a scientific agent. It makes gates, provenance, and failure handling
possible.

### Honest Evaluation Direction

The shared regression protocol uses grouped cross-validation as the headline result, not random
holdout. Materials use chemical-system grouping; molecules use scaffold grouping. This is materially
better than ordinary benchmark automation because it directly addresses leakage and interpolation.

### Cross-Model Review

The critic gateway supports independent reviewers, adversarial stance, rebuttal rounds, and
deterministic consensus. This is healthier than self-grading by the same model that proposed the
experiment.

### Provenance and Guardrails

The system already treats ledger records, budget limits, sandboxing, and RBAC as core infrastructure,
not afterthoughts. That is essential for any long-running autonomous research lab.

### Test Coverage

The test suite covers auth, critic behavior, domain plugins, protocol behavior, sandboxing, campaign
logic, recall, and write-up generation. For an early project, the engineering discipline is strong.

## Main Weaknesses

### Research Contribution Is Still Underspecified

The system can run experiments and produce reports, but it does not yet robustly decide whether an
experiment produced a meaningful research contribution. Current success is still too close to:

- model beats a baseline,
- report has citations,
- critic gate passes.

Frontier research needs stronger checks:

- Is the hypothesis novel?
- Does the experiment answer an important open question?
- Does the result explain a mechanism or only improve a number?
- Is the comparison against published work fair?
- Is the result reproducible?
- Is the claim strength calibrated to the evidence?

### Literature Grounding Is Too Weak

The survey stage exists, but literature grounding must become a first-class structured subsystem.
Natural-language briefings are not enough to support novelty or SOTA claims.

The system needs structured records of:

- queries,
- papers,
- methods,
- datasets,
- metrics,
- reported numbers,
- limitations,
- open gaps,
- contradictions.

### Fail-Closed Behavior Must Be the Default

The recent guardrail implementation moved the project in the right direction: real runs now pause when
critic providers are unavailable, unknown domains are requested, or the survey produces no citable
grounding.

This principle must be expanded. A real research run should pause, not continue, when:

- literature retrieval fails,
- no SOTA table exists,
- the evaluation protocol degrades,
- the executed method differs from the claimed method,
- artifact provenance is incomplete,
- critic review is unavailable,
- reproduction fails.

### Campaign Search Is Still Shallow

The current campaign loop can run multiple experiments, but the search policy is not yet a true
research planner. It needs expected information gain, ablation planning, failure-driven iteration,
reproduction passes, and explicit stopping criteria.

### Reports Can Look More Certain Than They Are

A system that writes a polished paper can create false confidence. Aletheia must distinguish:

- supported claims,
- weak claims,
- speculative claims,
- unverified claims,
- refuted claims.

No generated report should imply stronger evidence than the ledger contains.

## Highest-Risk Failure Modes

1. **False novelty** — the system claims novelty because it failed to retrieve prior work.
2. **False SOTA comparison** — the system compares against incompatible metrics, splits, or datasets.
3. **Silent fallback** — the system runs a simpler method or different domain while reporting the
   requested one.
4. **Reviewer absence** — critic infrastructure is missing but the run still completes.
5. **Benchmark overfitting** — the system optimizes a familiar benchmark without producing generalizable
   knowledge.
6. **Report overclaiming** — generated prose is stronger than the evidence.

## Strategic Judgment

The project should continue. Its architecture is pointed in the right direction. The most important
next step is not more autonomy; it is more epistemic discipline.

Priority should be:

1. Fail-closed guardrails.
2. Evidence ledger.
3. Structured literature and SOTA tables.
4. Hypothesis scoring.
5. Reproducibility passes.
6. Experiment search.
7. More domains.

Autonomy should increase only when the system can prove what it knows, what it does not know, and why
it is safe to continue.

