# Claude Hardening Review - 2026-06-02

## Purpose

This document is a neutral, strict review of the latest Claude-developed hardening work on
Aletheia, centered on the merge commit:

```text
afee4e4 Merge pull request #29 from howardwang1997/harden-first-real-run
```

The review asks whether the work moves Aletheia toward its ultimate goal: AI conducting
frontier scientific research end to end, with evidence, provenance, calibrated claims, and
fail-closed behavior.

## Executive Judgment

The development direction is broadly correct. The recent work strengthens the system's
epistemic discipline rather than merely adding more autonomy. That is the right priority for
Aletheia at this stage.

The most valuable changes are:

- real embedding requirements for scientific dense-retrieval metrics;
- more robust literature API access through rate limiting and retries;
- explicit distinction between `refuted` and `not_evaluated` claims;
- clearer run-level status when the results gate rejects an experiment;
- avoidance of extra optimization after a peer-review rejection.

However, the work is not yet a complete real-run hardening pass. The main remaining risks are
in real RAG execution, method fallback semantics, and insufficient test coverage for non-dry
scientific paths.

The correct classification is:

> Directionally strong hardening, with important remaining real-run correctness gaps.

## Verified Model Update

The repository now defaults to `claude-opus-4-8`. This is valid based on current official
Anthropic documentation.

Official sources reviewed:

- <https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-8>
- <https://platform.claude.com/docs/en/release-notes/overview>
- <https://platform.claude.com/docs/en/docs/about-claude/models>
- <https://www.anthropic.com/news/claude-opus-4-8>

The official docs state that Claude Opus 4.8 launched on May 28, 2026, and that the API model
ID is `claude-opus-4-8`. A quick scan of the repository did not find Claude-specific use of
non-default `temperature`, `top_p`, `top_k`, or old explicit thinking-budget parameters that
would obviously conflict with the Opus 4.8 migration guidance.

Conclusion: the model update should not be treated as a defect.

## Positive Findings

### Real Embedder Fail-Closed Behavior

`aletheia/memory/embedder.py` adds `EmbedderUnavailableError` and `get_embedder(require_real=True)`.
This is a necessary correction. A scientific dense-retrieval result must not be computed with
random hash vectors and reported as semantic retrieval.

The caching detail is also correct: a cached hash fallback cannot satisfy a later `require_real`
caller. That prevents a subtle false-positive path after optional dependencies are installed or
settings change.

### Literature API Robustness

`aletheia/research/literature.py` adds arXiv pacing, User-Agent headers, retry/backoff, and
OpenAlex polite-pool mailto. This is practical and aligned with real research operation.

This does not solve structured-literature quality by itself, but it reduces avoidable failures
in the survey stage. That matters because Aletheia's roadmap explicitly depends on real,
citable grounding rather than dry-run briefings.

### `not_evaluated` Claim Status

`aletheia/memory/ledger.py` adds `not_evaluated`, and `aletheia/scheduler/driver.py` uses it
when the hypothesis names one method family but another was actually executed.

This is an important scientific distinction:

- `refuted` means the claim was tested and contradicted;
- `not_evaluated` means the claimed mechanism was never instantiated.

That distinction directly supports calibrated reporting.

### Results-Rejected Run Status

The driver now distinguishes a clean completion from a run whose results gate rejected the
experiment. Setting run status to `results_rejected` is better than marking such runs simply
as `completed`.

This is aligned with the principle that a polished report must not create false confidence.

### Skipping Optimize After Rejection

Skipping optimization after a rejected results gate is reasonable. Metric tuning usually does
not address critique-level rejection, and continuing can waste budget while making the run look
more successful than it is.

## Issues And Risks

### High: Real RAG Runs Can Be Blocked By A Regression-Domain Artifact Assumption

`ExperimentDriver._post_execution_guards` requires real runs to produce both `eval` and `model`
artifacts. This is appropriate for fitted regression domains, but not for host-side RAG
evaluation.

The RAG plugin returns an `eval` artifact only. It evaluates a retrieval/generation
configuration; it does not train and persist a fitted model. Therefore a successful real RAG
execution can be paused before analysis because the guard expects a `model` artifact that the
domain should not produce.

Risk:

- the RAG domain may work in dry-run while failing in real mode;
- the test suite can pass while a core claimed capability is unusable;
- artifact completeness is enforced, but with the wrong domain contract.

Recommended fix:

- define required artifacts per domain/profile or per execution mode;
- require `eval` for RAG, plus any domain-specific provenance artifact such as retriever label,
  prompt, generated answers, and per-case scores;
- keep `model` required for domains that fit models.

### High: Dense Retrieval Fallback Still Does Not Fully Fail Closed

For normal RAG evaluation, a real run that requests dense retrieval falls back to lexical
retrieval when the real embedder is unavailable. The event stream labels the fallback, which is
more honest than using hash vectors, but it still permits the experiment to proceed with a
different method.

Risk:

- a hypothesis about dense retrieval can be evaluated with lexical retrieval;
- method drift may not be detected because the current method-family detector does not model
  dense vs lexical retrieval;
- the mechanism claim may not be forced to `not_evaluated`;
- the report may still look like an experiment was conducted, when the requested mechanism was
  unavailable.

Recommended fix:

- in real runs, if dense retrieval is the requested scientific method and no real embedder is
  available, pause the run or mark the mechanism claim `not_evaluated` before reporting;
- extend method provenance to structured RAG methods: `lexical`, `dense`, `hybrid`, `rerank`,
  `generator`, `answerer`;
- include fallback status in the claims/evidence package, not only in events.

### Medium: Method Drift Detection Is A Useful Prototype But Too Narrow

The current detector is keyword-based and covers a small set of classical model families:
random forest, gradient boosting, neural network, linear model, SVM, nearest neighbor, and
Gaussian process.

It does not reliably cover:

- dense vs lexical retrieval;
- reranking and hybrid retrieval;
- graph neural networks;
- foundation-model fine-tuning;
- kernel methods beyond Gaussian process wording;
- multi-method hypotheses such as ablations or method comparisons;
- hypotheses that mention both baseline and candidate methods.

Risk:

- reviewers may overestimate the coverage of the guardrail;
- real method drift can pass undetected;
- the system may still confuse "not tried" with "tried and failed" outside the narrow supported
  model families.

Recommended fix:

- store requested method and executed method as structured fields;
- have each domain plugin declare method family, method role, and fallback status;
- keep keyword detection only as a secondary fallback for free-text hypotheses.

### Medium: Method Drift Should Be Explicit In The Results Review Payload

The driver computes `method_drift` before the results review, and later uses it for claim
finalization and write-up instructions. But the review payload does not explicitly include
`method_drift` and `method_drift_msg`.

Risk:

- the critic panel may not directly evaluate this known issue;
- evidence review remains partly implicit;
- a reviewer must infer drift from design/code instead of receiving the harness-owned finding.

Recommended fix:

- add `method_drift`, `method_drift_msg`, requested method, executed implementation, and fallback
  state to the results review payload.

### Medium: `results_rejected` Is Still Presented Too Much Like Success In The Frontend

The backend now emits `results_rejected`, which is good. The frontend activity text currently
renders `run_finished` with a success-like checkmark regardless of status.

Risk:

- users can read a rejected scientific result as a completed success;
- the UI undermines the backend's improved claim calibration.

Recommended fix:

- render `results_rejected` as a warning/rejected outcome;
- distinguish `completed`, `results_rejected`, `paused`, and `failed` visually and textually;
- surface the results gate verdict near final metrics and the report.

### Medium: Documentation Now Makes Real Runs Sound Too Automatic

The README says that on a machine already logged into Claude Code, real runs fire automatically.
This matches the current auth design, but the risk boundary should be clearer.

Risk:

- users may unintentionally spend money or invoke external model calls;
- users may not understand when dry-run versus real mode is selected;
- real data and credentials are more exposed than the old wording implied.

Recommended fix:

- explicitly document the dry-run/real-run decision path;
- highlight budget caps and external model calls;
- make clear that real runs require deliberate launch action and appropriate budget settings.

## Test Review

Commands run:

```bash
conda run -n aletheia pytest tests/test_dense_retriever.py tests/test_method_drift.py -q
conda run -n aletheia pytest -q
```

Results:

```text
12 passed
167 passed, 1 skipped
```

The current suite passing is a good sign. It shows the new local behavior and dry-run paths do
not obviously regress the existing codebase.

However, the test suite does not yet prove the real scientific paths are safe.

Important missing tests:

- real RAG execution passing `_post_execution_guards` without a model artifact;
- real dense retrieval requested with unavailable embedder, verifying pause or `not_evaluated`;
- method drift flowing through results review payload, claim finalization, and report prompt;
- frontend rendering of `results_rejected`;
- Opus 4.8 config smoke test or documented official-ID validation in CI;
- domain-specific artifact-contract tests.

## Strategic Assessment

This work is on the right path because it strengthens truthfulness and provenance. It does not
just add another autonomous capability; it closes ways the system could produce misleading
scientific output.

The main weakness is that fail-closed behavior is still uneven. Some guards are strict, but not
yet domain-aware. Some fallbacks are labeled, but still allow experiments to proceed with a
different method. Some evidence rules exist, but not every known issue is fed directly into the
critic and claim machinery.

The next step should not be more breadth. It should be real-run correctness:

1. make artifact guards domain-aware;
2. make method provenance structured;
3. make fallback state part of claims and critic review;
4. add real-mode tests for RAG and dense retrieval;
5. improve UI outcome semantics for rejected results.

## Final Evaluation

Claude's recent development should be judged positively but not uncritically.

It is moving Aletheia toward the ultimate goal because it improves evidence discipline, makes
unsupported dense metrics harder to report, and introduces a better vocabulary for untested
claims. But it has not yet fully secured the real-run path. The strongest remaining concern is
that the system can still either block valid RAG work due to regression-domain assumptions or
continue after method fallback in ways that are not fully reflected in scientific claims.

The project should continue along this hardening direction before expanding autonomy or adding
more domains.
