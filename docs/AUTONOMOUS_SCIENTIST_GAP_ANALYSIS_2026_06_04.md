# Autonomous Scientist Gap Analysis — 2026-06-04

## Executive Assessment

Aletheia is moving in the right direction, but the remaining gap is not mainly autonomy or
workflow coverage. The hard gap is scientific credibility: whether the system can produce,
calibrate, and defend claims with evidence that survives independent scrutiny.

The current system is best understood as an early autonomous research operating system. It can
chain together literature search, hypothesis generation, experiment execution, critic review,
and write-up. A credible autonomous scientist must be stricter than that: every important claim
must be grounded in structured evidence, fixed experimental harnesses, reproduction, and
independent review. Reports should express evidence; they should not create it.

## What A Credible Autonomous Scientist Requires

### 1. Claim-first science

The system should treat scientific conclusions as structured claims before it treats them as
prose. Each major claim needs a type, status, strength, evidence references, limitations, and
review state. Unsupported statements should remain speculative or unverified, even if they are
well written.

A trustworthy report is then generated from the claim/evidence ledger. The reverse direction is
unsafe: if the paper draft is the primary artifact, the system can sound more certain than the
evidence allows.

### 2. Harness verifies, agents propose

LLMs are useful for proposing hypotheses, experiments, code, interpretations, and drafts. They
should not be trusted to decide whether a scientific claim holds. That decision should belong to
domain harnesses with registered capabilities: what they can compute, which inputs they accept,
which statistic they produce, and which conditions support or refute the claim.

If no domain capability exists for a proposed demonstration, the honest state is unverified. The
system should not substitute a nearby demonstration just because it sounds related.

### 3. Fail-closed defaults

A credible autonomous scientist should pause, block, or downgrade when evidence is missing. This
includes missing literature grounding, unavailable baselines, degraded evaluation protocols,
missing artifacts, weak critic coverage, failed reproduction, or a report attempting stronger
claims than the evidence supports.

Fail-closed behavior is not a UX inconvenience; it is the core safety property for scientific
truthfulness.

### 4. Reproduction as a first-class gate

Strong claims require more than a single successful run. At minimum, the system should support
seed reruns, protocol reruns, and, where possible, cross-dataset or cross-model-family reruns.
The reproduction criterion should be defined per claim type and per demonstration capability,
not as a single global tolerance.

The system should distinguish qualitative reproduction, statistical confirmation, and exact
deterministic recomputation. These are different evidential states.

### 5. Independent criticism

Critic review must be genuinely independent enough to bear scientific weight. A gate that can
pass with one surviving reviewer, or with same-vendor self-review, is not a strong peer-review
substitute.

For high-strength claims, the system should require multiple successful reviewers and vendor
diversity. Reviewer failures, reviewer count, unresolved objections, and degraded review states
should be visible in the evidence bundle.

### 6. Information-gain driven experimentation

An autonomous scientist should not merely continue optimizing metrics. It should choose the next
experiment based on expected information gain: which test is most likely to change belief in the
claim, expose a confound, separate competing explanations, or narrow the claim's scope.

Negative results are useful if they reduce uncertainty. The system should learn from failed
assumptions, critic objections, and unstable reproductions.

### 7. Research bundle before paper

The paper should be a generated view over a research bundle, not the primary output. The bundle
should include the research question, structured literature review, novelty analysis, data card,
experiment plan, implementation code, evaluation artifacts, baselines, ablations, reproduction
results, critic reviews, claim-to-evidence map, limitations, and packaging instructions.

## Current Gap From That Standard

### 1. Workflow automation is ahead of scientific judgment

Aletheia can already run a substantial research workflow, but many judgments still depend on
weak proxies: whether the run completed, whether a critic passed, whether a report was produced,
or whether a metric improved. A mature system needs stronger epistemic state tracking: what is
supported, what is refuted, what is unverified, and why.

### 2. The claim/evidence ledger is not yet hard enough

The project has claim status and strength concepts, but it does not yet enforce that every major
report statement maps to structured evidence. Without that enforcement, write-ups can still make
claims that are more polished or more general than the underlying artifacts justify.

### 3. Demonstration capabilities are too narrow

The recent molecules work moves in the correct direction by registering harness-computed
demonstrations such as scaffold generalization, activity-cliff Lipschitz behavior, and the
leakage-slope law. But this is still a small, hand-built set in one domain. Other domains cannot
yet ground many paradigm claims, and arbitrary AI-proposed demonstrations remain out of reach.

The long-term interface should be explicit capability selection and capability-backed execution,
not free-text keyword matching.

### 4. Statistical rigor is uneven

The system increasingly fails closed, which is good. But several statistical definitions remain
too coarse for strong scientific claims: bootstrap semantics, counterfactual success criteria,
reproduction tolerances, multiple comparisons, cross-dataset replication, and uncertainty around
ranking or mechanism claims.

The leakage-slope law implementation is a useful example. It correctly turns an untested premise
into a computed harness result and can honestly return `holds=false`. However, its tests mostly
verify output shape and routing, not whether the statistical standard is strong enough. A
credible scientist needs tests that catch weak evidence definitions, not only missing fields.

### 5. Critic independence can degrade too far

Cross-model review is a strong architectural direction, but it must not silently degrade into
single-reviewer or same-vendor validation for important claims. If critic coverage is weak, the
system should mark the gate as degraded or blocked rather than treating the result as peer
reviewed.

### 6. Novelty detection remains fragile

Literature retrieval has improved, but "not found" is not evidence of novelty. A credible
system needs structured literature memory: methods, datasets, metrics, results, limitations,
open gaps, and known similar claims. Novelty and SOTA claims should stay speculative unless the
literature search reached an explicit quality bar.

### 7. Autonomous research campaigns are still immature

The current system is closer to single-loop or short campaign automation than to sustained
scientific investigation. A mature autonomous scientist should decide whether the next best
step is reproduction, ablation, failure analysis, counterexample search, dataset expansion,
protocol hardening, or claim narrowing.

## Priority Direction

The next step is not simply making Claude, Codex, or any one model smarter. The priority is to
make Aletheia less willing to believe itself.

The highest-leverage improvements are:

1. Enforce a structured claim/evidence ledger for every major report conclusion.
2. Expand registered domain demonstration capabilities, with explicit success and failure
   criteria.
3. Require stronger, per-capability reproduction before claims can become moderate or strong.
4. Harden critic gates so important claims need multiple independent reviewers.
5. Add retrieval-health and novelty-health signals before allowing novelty or SOTA claims.
6. Drive campaigns by expected information gain instead of metric optimization alone.
7. Keep papers as generated summaries of evidence bundles, not as the source of truth.

## Bottom Line

Aletheia is on the correct path because it is becoming more evidence-led and more fail-closed.
The remaining distance to a credible autonomous scientist is the distance between an automated
research workflow and a disciplined evidence-producing institution.

The central question for every future patch should be:

> Does this make the system better at refusing unsupported claims, producing reproducible
> evidence, and choosing experiments that genuinely change what we know?

If the answer is yes, it is probably moving toward the autonomous scientist goal. If the answer
is only that the system can complete more runs or write better reports, it is probably improving
the demo more than the science.
