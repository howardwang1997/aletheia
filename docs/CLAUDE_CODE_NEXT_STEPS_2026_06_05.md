# Claude Code Next Steps — 2026-06-05

Scope: follow-up plan after reviewing today's Claude Code development on the
AI-authored demonstration path and verification-spine UI.

## Executive Judgment

Today's development is on the right path. The work is not drifting into cosmetic polish; it
is strengthening the core epistemic contract:

- the AI may propose and author a discriminating computation;
- the harness owns the verdict through a committed pre-registration and negative control;
- independent audit and critic gates constrain claim strength;
- the UI now exposes the final claim ledger instead of only showing a headline metric.

The remaining work is mostly about hardening this path so it is reliable in real runs, not just
plausible in dry-run/unit tests.

## Distance From the North Star

The project is no longer a toy demo, but it is not yet a mature autonomous scientist. A fair
current classification is:

```text
credible early research operating system: yes
reliable autonomous frontier scientist: not yet
```

Rough progress estimate: Aletheia is around 35-45% of the way to the stated end state where a
human provides direction, data, budget, credentials, and approval while the system handles the
research loop end to end and produces a reproducible research bundle with calibrated claims.

The strongest parts are now the engineering skeleton and evidence discipline:

- explicit lifecycle from survey through write-up;
- structured claim/evidence ledger;
- fail-closed behavior when grounding is missing;
- critic gates and degraded-review handling;
- reproduction checks;
- paradigm/formulation claims separated from benchmark-performance claims;
- AI-authored demonstration path beginning to move beyond fixed registered templates;
- frontend visibility into claim status rather than only headline metrics.

The biggest remaining gap is not autonomy for its own sake. The hard gap is scientific creativity
plus generality: can the system reliably propose, implement, and defend a genuinely novel
discriminating demonstration, across domains, without overclaiming?

Current capability should be read as: the system is becoming good at framing, executing, auditing,
and refusing unsupported claims. It has not yet shown that it can repeatedly produce frontier
scientific contributions that survive independent scrutiny.

### What Is Close

The research lifecycle is mostly in place. A run can move through literature search, hypothesis
generation, experiment design, code authoring, execution, analysis, critic review, claim
finalization, and report generation.

The epistemic spine is also directionally right. Reports are increasingly downstream of claims,
metrics, artifacts, reviews, reproduction, and limitations. That is the correct architecture for
trustworthy research automation.

Today's AI-authored demonstration work is especially important because it attacks a core frontier
limitation: a paradigm claim should not be limited to hand-coded templates. Letting the AI author a
candidate computation while the harness owns pre-registration, controls, probes, audit, and verdict
is the right shape.

### What Is Still Far

The AI-authored demonstration path is newly viable, not yet reliable. It needs repeated real e2e
runs across different questions before it can be treated as a dependable research capability.

Domain coverage is narrow. Molecules has the strongest paradigm grounding. Materials and RAG have
useful infrastructure, but the general pattern "arbitrary domain, arbitrary proposed
demonstration, harness-verifiable claim" is not solved.

Novelty judgment remains weak. Literature retrieval and structured SOTA rows are improving, but
the system can still mistake a rephrased known concept for a new frame. The right behavior is to
detect this, downgrade it, and ask a sharper question.

Campaign planning is still shallow. A mature scientist chooses the next experiment based on failed
assumptions, critic objections, expected information gain, uncertainty reduction, and unresolved
claims. The current campaign loop has pieces of this, but not a full belief-updating research
planner.

Reviewer and audit independence still need hardening. Cross-vendor review is the correct bar, but
provider failures, degraded review, audit errors, and non-independent auditors need distinct,
first-class states so the system never confuses missing review with scientific refutation or
support.

The research bundle is not yet fully productized. The final output should be a reproducibility
package containing the question, literature, data card, plan, code, metrics, artifacts, claims,
limitations, reviews, reproduction results, and report. The pieces exist, but the bundle is still
assembled implicitly rather than delivered as a stable artifact.

### Practical Stage Assessment

```text
toy demo                                      surpassed
credible research operating-system MVP        current stage
multi-experiment autonomous research assistant next major stage
reliable autonomous frontier scientist         still several stages away
```

The next leap is not adding more UI or more prompt surface. It is making the AI-authored
demonstration path robust, making the claim ledger the single source of truth for write-up,
proving the system on several non-template paradigm runs, and building a planner that selects
follow-up experiments from evidence gaps rather than from generic optimization pressure.

## What Was Verified

The targeted backend and frontend checks pass in the intended environment:

```text
conda run -n aletheia PYTHONPATH=. pytest tests/test_paradigm_p5.py tests/test_worker.py
24 passed

conda run -n aletheia PYTHONPATH=. pytest tests/test_domain_molecules.py tests/test_domain_materials.py tests/test_method_drift.py tests/test_paradigm_p3.py
24 passed

npm run build
Compiled successfully
```

Bare `pytest` in the current shell fails because the active Python environment lacks project
dependencies such as `pgvector`. Use the `aletheia` conda environment for project validation.

## Highest-Priority Fixes

### 1. Add a real options test for text-only workers

File: `aletheia/orchestrator/worker.py`

Today's change correctly tries to force text-only Claude workers to return inline text by setting:

```python
opts["permission_mode"] = "bypassPermissions"
opts["allowed_tools"] = []
opts["disallowed_tools"] = list(_NO_TOOLS)
```

This addresses a real failure mode: the coder used a `Write` tool, returned prose, and the
downstream code extractor received no fenced Python block.

The gap: existing `tests/test_worker.py` only exercises dry-run behavior. It does not instantiate
the non-dry-run `ClaudeAgentOptions` path, so an SDK compatibility issue with empty
`allowed_tools` plus `disallowed_tools` would only appear during real runs.

Required test:

- monkeypatch `has_credentials()` to return `True`;
- monkeypatch `configure_auth()` to no-op;
- monkeypatch `claude_agent_sdk.ClaudeAgentOptions` or the imported SDK module with a fake object
  that captures constructor kwargs;
- monkeypatch `ClaudeSDKClient` with an async fake that yields one assistant text message;
- assert a text-only worker sets `permission_mode="bypassPermissions"`, `allowed_tools=[]`, and a
  disallow list containing at least `Write`, `Bash`, `Read`, `Edit`;
- add a second test for MCP/tool workers to ensure `allowed_tools` still passes through and the
  no-tools disallow list is not applied.

Acceptance criteria:

- `conda run -n aletheia PYTHONPATH=. pytest tests/test_worker.py` passes;
- the test fails if the text-only worker silently regains file or shell tools.

### 2. Clarify audit status semantics

File: `aletheia/scheduler/driver.py`

Current behavior is mostly correct:

- a real audit rejection or non-independent auditor forces `demo_result["holds"] = False`;
- an audit infrastructure exception does not change `holds`;
- audit infrastructure failure caps formulation strength by returning `False` as `audit_passed`.

The problem is semantic ambiguity. A return value of `False` currently means two different things:

- the audit ran and refuted or failed the demonstration;
- the audit did not run because the audit infrastructure errored.

That distinction matters because one is evidence against the demonstration, while the other is
missing independent verification.

Recommended change:

- return a structured audit state instead of a bare bool, for example:

```python
{"status": "passed" | "refuted" | "not_independent" | "error" | "skipped"}
```

or, if keeping the public shape small:

```python
audit_passed: bool | None
audit_error: bool
```

Then update `_claim_strength()` so:

- `audit_passed is True` allows normal strength escalation;
- `audit_passed is False` means audit ran and failed/refuted;
- `audit_error is True` caps strength but does not imply refutation;
- skipped non-AI demonstrations remain unaffected.

Required tests:

- audit rejection forces `holds=False` and `audit_refuted=True`;
- non-independent audit forces `holds=False`;
- audit infrastructure exception leaves `holds` unchanged, records audit evidence as error, and
  caps strength without labeling the formulation as refuted;
- final claim status for audit infrastructure failure is `supported` or `unverified` according to
  results gate and demonstration holds, but strength is no higher than `weak`.

Acceptance criteria:

- no code path can confuse "audit unavailable" with "audit refuted the result";
- the claim ledger and frontend can show the distinction later if needed.

### 3. Decide what to do with untracked documents

Current untracked files:

```text
docs/AUTONOMOUS_SCIENTIST_GAP_ANALYSIS_2026_06_04.md
docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md
docs/CLAUDE_CODE_DEVELOPMENT_REVIEW_2026_06_04.md
paper.md
```

These appear useful, but they are currently neither committed nor intentionally ignored.

Required decision:

- commit the docs if they are part of the project record;
- move generated research outputs such as `paper.md` under an artifact/output directory if they
  are run products rather than source documentation;
- remove or ignore only if they are scratch notes.

Acceptance criteria:

- `git status --short` has no ambiguous untracked research artifacts after the decision;
- future reviewers can tell whether `paper.md` is a project document, a generated report, or a
  disposable run artifact.

## Medium-Priority Hardening

### 4. Add a claims event regression test

Files:

- `aletheia/scheduler/driver.py`
- `frontend/lib/useSession.ts`
- `frontend/components/Activity.tsx`

The backend now emits a `claims` event after claim finalization, and the frontend renders a claim
ledger card from the latest such event. This is the right UX direction because it makes evidence
state visible.

The gap: there is no test that the emitted event has the shape the frontend expects.

Required backend test:

- run `_finalize_claims()` on a minimal run with metric/formulation/reproduction claims;
- collect or inspect the emitted `claims` event;
- assert each claim row includes `claim_type`, `status`, `strength`, `evidence_kinds`, and
  `claim_text`.

Required frontend test, if the project keeps frontend tests:

- feed a synthetic `claims` event into the session/event reducer path;
- assert `claims` returns the last claims payload;
- assert statuses `supported`, `refuted`, `unverified`, and `not_evaluated` render without crashing.

Acceptance criteria:

- backend/frontend event contract is pinned;
- future claim-status additions fail visibly if the UI cannot render them.

### 5. Make real e2e output easier to audit

File: `scripts/real_ai_demonstration_e2e.py`

The script is useful but still optimized for live console inspection. For repeated development,
it should leave a compact machine-readable summary.

Recommended additions:

- write `run_id`, final run status, metric list, claim ledger summary, demonstration payload, and
  audit payload to a timestamped JSON file under an artifacts directory;
- include whether the route used the AI-authored capability or a registered fallback;
- include the exact `demonstration_prefer_authored` setting used.

Acceptance criteria:

- after a real e2e run, a reviewer can inspect one JSON file without scraping terminal output;
- the summary makes it obvious whether the frontier path actually fired.

### 6. Tighten `demonstration_prefer_authored` operational safety

Files:

- `aletheia/config/settings.py`
- `aletheia/scheduler/driver.py`
- `scripts/real_ai_demonstration_e2e.py`

The override is correct for frontier testing, but it should remain visibly experimental.

Recommended additions:

- emit an event when registered-first routing is overridden;
- include the selected route in the `demonstration_code` or `demonstration` event;
- keep the default `False`;
- document that authoring failure falls back to registered capability and does not by itself
  support a formulation claim.

Acceptance criteria:

- a real run's event stream reveals whether the AI-authored path was forced;
- accidental production use of the override is visible in logs/UI.

## Lower-Priority Cleanup

### 7. Reduce overlong explanatory comments after behavior stabilizes

Several new comments are useful right now because the evidence semantics are subtle. Once tests
pin the behavior, trim comments that restate implementation details and keep the ones explaining
scientific semantics:

- why audit error is not refutation;
- why negative control is required;
- why reproduction must be seed-perturbed;
- why claim strength is capped under degraded review.

Acceptance criteria:

- code remains readable without losing the core epistemic rationale.

### 8. Add a short architecture note for AI-authored demonstrations

File suggestion: `docs/PARADIGM_MODE_DESIGN.md`

Add a concise section documenting the full path:

```text
IDEATE demonstration spec
-> optional frontier override
-> coder authors compute_demonstration + preregistration
-> static gate + smoke test
-> preregistration committed to ledger
-> domain harness executes test/control
-> independent author-excluded audit
-> results gate
-> claim finalization
-> claims event/UI
```

Acceptance criteria:

- a new contributor can understand where the verdict is derived;
- the doc explicitly says the AI-authored code never gets to decide `holds`.

## Suggested Work Order

1. Add the non-dry-run worker options tests.
2. Refactor audit status semantics and add regression tests.
3. Add backend `claims` event shape test.
4. Add route/audit/demo summary output to `real_ai_demonstration_e2e.py`.
5. Decide and clean up the untracked docs/artifacts.
6. Update `PARADIGM_MODE_DESIGN.md` with the finalized AI-authored demonstration flow.

This order prioritizes regressions that could silently break real runs before improving
documentation and observability.

## Definition of Done for the Next Development Pass

The next pass should be considered complete when:

- text-only workers are unit-tested against the real non-dry-run SDK option path;
- audit error, audit refutation, audit non-independence, and audit skip are distinct in code and
  tests;
- claim ledger event shape is pinned by a backend test;
- real AI-demonstration e2e runs leave an auditable summary artifact;
- untracked research documents are either committed, moved to an artifact location, or removed;
- the targeted backend tests and frontend build still pass in the `aletheia` conda environment.
