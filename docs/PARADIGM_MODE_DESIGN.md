# Paradigm Mode — design sketch

**Status:** design only (not implemented). Aligns the results gate with the refined north
star: *frontier research ≠ beating SOTA; it can be creating a new paradigm.*

## Problem

Aletheia today only does Kuhnian **normal science**. Every `DomainProfile`
(`aletheia/domains/base.py`) is built around `headline_metric` + `sota_reference` +
`sota_rows`, and the results-gate critics are instructed to reject results that trail the
benchmark. A genuine new paradigm (a new question / formulation / representation / metric)
**always trails the incumbent benchmark at birth** — so the current gate rejects it every
time. Observed live: the molecules e2e's attempted contribution was uncertainty / prediction-
interval calibration (a different *question* than RMSE), but the gate flattened it to
"didn't beat RMSE SOTA → reject."

Key fact that makes this fixable cheaply: **`gate_passed` is NOT mechanically tied to
SOTA.** It is purely the critic panel's consensus over verdicts
(`gateway._consensus`, `aletheia/critics/gateway.py:131`). The SOTA-anchoring lives in the
*framing*: the domain profile, the per-target critic instruction (`_instruction`), and
`_claim_strength`'s `has_sota` lever (`aletheia/scheduler/driver.py`). So paradigm mode is
mostly about **what standard the critics are told to apply** + **the claim taxonomy** —
the deterministic spine stays intact.

## The non-negotiable constraint

Paradigm claims are the **most fakeable** thing in science. The fix is NOT "relax the gate."
Even revolutionary science, to be science and not a manifesto, must cash out in a
**discriminating, reproducible demonstration**. No demonstration → it is a *proposal*, not a
supported paradigm claim. This is what keeps paradigm mode inside the fail-closed spine
(see `invariants-safety-spine`).

## Two contribution types, two gate modes

```
                        IDEATE / HYPOTHESIS
                               │
                 declare contribution_type
                ┌──────────────┴───────────────┐
        "performance"                       "paradigm"
   (beat/match a benchmark)        (change the question/representation/metric)
                │                               │
        ┌───────┴────────┐              ┌───────┴─────────┐
        │ EXISTING PATH  │              │  NEW PATH        │
        │ leakage-aware  │              │ build a          │
        │ eval vs SOTA   │              │ DISCRIMINATING   │
        │ + reproduction │              │ DEMONSTRATION    │
        └───────┬────────┘              └───────┬─────────┘
                │                               │
        RESULTS GATE (performance mode)   RESULTS GATE (paradigm mode)
        pass iff: match/beat SOTA,        pass iff: (a) novelty claim survived an
        leakage-aware, reproduced         adversarial literature attack, (b) the frame
                │                         is well-posed, (c) a reproducible discriminating
                │                         demonstration holds, (d) honest scope.
                │                         SOTA-delta is EXPLICITLY IRRELEVANT.
                └───────────────┬───────────────┘
                          gate_passed (same consensus mechanics)
                                │
                    FINALIZE CLAIMS · WRITE-UP (mode-labeled)
```

Both modes share the *mechanism* (`_consensus` over the final round, fail-closed,
reproduction). They differ only in the **standard** and the **claim type** that carries the
contribution.

## What grounds a paradigm claim (the evidence bar)

A `paradigm`/`formulation` claim reaches `supported` only with a **discriminating
demonstration** — one of:

| Form | The demonstrable, adversarially-checkable assertion |
|------|------|
| **Discriminating instance** | The standard metric/model rates A and B equivalent; the new frame separates them, + a concrete case proving A and B genuinely differ in a way that matters. |
| **Enablement** | The new representation makes a measurement/result possible that was previously unaskable — shown on real data. |
| **Unification** | Two previously-separate phenomena shown to be one under the new frame, with the mapping explicit and checked. |
| **Impossibility / blind-spot** | A constructed, reproducible case showing the SOTA framing has a structural hole. |

All four are concrete artifacts the harness can re-run — so they plug into the existing
`evidence` ledger and the reproduction pass, exactly like a metric does today.

## Concrete changes (grounded in the code)

1. **Claim taxonomy** — `aletheia/memory/ledger.py:67` `CLAIM_TYPES`: add `"formulation"`
   (a.k.a. paradigm). It is the load-bearing claim in paradigm mode, the way `metric`/`sota`
   are in performance mode.

2. **Contribution type at ideation** — the hypothesis/scorecard stage records
   `contribution_type ∈ {performance, paradigm}`. The hypothesis scorecard already gates on
   novelty/clear-eval; extend it: a `paradigm` hypothesis must name *which* discriminating-
   demonstration form it will produce, or it is sent back. This prevents "paradigm" from
   becoming a vague escape hatch.

3. **DomainProfile** — `aletheia/domains/base.py`: make the SOTA fields optional framing
   rather than the definition of success. Add `supports_paradigm: bool = True` and let a
   profile describe the *incumbent frame* (what the new work would discriminate against)
   instead of only a benchmark number.

4. **Results-gate instruction** — `gateway._instruction` for `target="results"`: branch on
   `contribution_type`. Performance mode = today's "is the number real + leakage-free +
   reproduced, does it match/beat SOTA". Paradigm mode = a DIFFERENT, *stricter* adversarial
   prompt: "Is the claimed novelty real or repackaged (attack via the literature)? Is the new
   frame well-posed? Does the discriminating demonstration actually hold, or is it word salad?
   What would falsify that this frame is useful? **Do NOT reward or penalize SOTA-delta.**"
   The payload (`_results_review_payload`, `aletheia/scheduler/driver.py`) carries the
   demonstration artifact + the novelty evidence so the critic attacks the real thing.

5. **Claim strength** — `_claim_strength` (`aletheia/scheduler/driver.py`): in paradigm mode,
   `has_sota` is irrelevant. A `formulation` claim reaches `strong` only when (a) novelty
   survived the adversarial literature attack AND (b) the discriminating demonstration was
   *reproduced* (reuse the existing reproduction pass on the demonstration, not on a metric).
   No demonstration → capped at `speculative`/`proposed` and reported as a proposal.

6. **`gate_passed` decoupling** — no mechanics change. In paradigm mode the critics simply are
   not told to reject on SOTA-delta, so a trailing-SOTA result with a real demonstrated frame
   passes *as a paradigm contribution*, honestly labeled in the write-up.

7. **Write-up** — label the contribution type, so a paradigm result is never dressed up as a
   performance win and vice-versa. The `limitation`/honesty machinery (and `not_evaluated`
   status) applies unchanged.

## Phasing

- **P1 — vocabulary + honesty (small, safe): DONE (PR #32).** `formulation` claim type +
  `contribution_type` threading + write-up labeling. No gate change; just stop *flattening*
  paradigm attempts into performance language.
- **P2 — paradigm-mode instruction + scorecard demand: DONE (PR #33).** The results-gate
  critic instruction branches on `mode` (`gateway._instruction`, threaded through
  `review`/`review_sync`): paradigm mode judges novelty / well-posedness / the discriminating
  demonstration and explicitly does NOT reward or penalize SOTA-delta. The driver passes
  `mode=contribution_type` and carries the demonstration in the results payload. The
  demonstration requirement is enforced deterministically — `_contribution_type()` returns
  `paradigm` ONLY when a concrete `demonstration` is named (else falls back to performance);
  no demonstration → no formulation claim (the fakeability guardrail).
- **P3 — demonstration-as-evidence + reproduction: GROUNDING DONE (PR #34).** The
  `formulation` claim is now grounded by a REPRODUCIBLE discriminating demonstration, never an
  LLM assertion and never SOTA-delta:
  - `_claim_strength("formulation", …, demonstration_holds=…)` — no demonstration executed →
    `speculative`; held but gate failed / didn't hold → `weak`; held + approved → `moderate`;
    held + clean approve + **reproduced** → `strong`.
  - `_reproduce` reproduces the DEMONSTRATION (not a metric): stable only if it `holds` on both
    runs and its `statistic` is within tolerance → `reproduction.demonstration_reproduced`.
  - `_finalize_claims` finalizes the formulation claim: `not_evaluated` (no demonstration),
    `supported` (held + approved), `refuted` (gate reject / didn't hold). Write-up records a
    `limitation` when a paradigm run produced no demonstration (the formulation stays a PROPOSAL).
  - Contract: execution surfaces `info["demonstration"] = {form, holds, statistic, detail}`.

- **P4 — the demonstration EXECUTOR: DONE (PR #35).** A `DomainPlugin.run_demonstration(spec,
  data_spec, workdir) -> {form, holds, statistic, detail} | None` hook (default `None`) computes
  the discriminating statistic DETERMINISTICALLY (harness-owned, no LLM "holds" assertion). The
  **molecules** domain implements it on real MoleculeNet ESOL: it fits the same model and measures
  RMSE under a random holdout (the incumbent metric) vs a scaffold-grouped split, and `holds` iff
  the scaffold RMSE is materially worse — proving the random-split metric is blind to the
  scaffold-generalization gap. (Measured live: random 1.18 vs scaffold 1.95, **ratio 1.65**.) The
  driver runs it in `_run_eval` for paradigm runs (dry-run-skipped; reproduction re-enters
  `_run_eval` so the demonstration is recomputed + checked stable → P3 grounding).

  Two real-run bugs surfaced + fixed by the first paradigm e2e: (a) the per-round `_claim_ids`
  reset wiped IDEATE's `formulation` id before `_finalize_claims` — moved the reset to the
  campaign-loop top; (b) a round where every critic provider errored returned empty critiques →
  `_consensus([])` silently rejected — now it falls back to the last non-empty round, else fails
  closed as a "peer review could not run" block (honest, not a fake reject).

  Live status: the executor computes + reproduces the demonstration on real data end-to-end. A
  full real e2e grounding the formulation to `moderate`/`strong` is gated on the (currently
  intermittently-erroring) OpenAI Codex critic being available — the fail-closed fix correctly
  PAUSES such a run rather than fabricating an approval.

- **P5 — the AI-AUTHORED demonstration EXECUTOR: DONE (PRs #59–#61).** P4's executor was limited
  to hand-coded registered capabilities (one or two per domain). P5 lets the AI AUTHOR the
  discriminating computation itself while the **harness still owns the verdict** — the frontier
  move toward general paradigm discovery. The AI writes code; it never decides `holds`.

  The full path (`aletheia/scheduler/driver.py` unless noted):

  ```text
  IDEATE demonstration spec
    -> optional frontier override (demonstration_prefer_authored / spec authoring="ai")
       — default is REGISTERED-FIRST: a registered capability pre-empts authoring (_prefer_authored_demonstration)
    -> _demonstration_code: the coder authors compute_demonstration(X, y, groups, meta)
       + a JSON PRE-REGISTRATION (statistic, supported_if, control_silent_if) — committed BEFORE results exist
    -> static gate: check_code (AST allowlist, required compute_demonstration) + smoke_test_demonstration (dummy data)
    -> the pre-registration is committed IMMUTABLY to the formulation claim's evidence ledger
    -> _compute_demonstration: the pre-registration is read BACK from the ledger (not memory),
       the domain harness stages data (load_data -> featurize -> X,y,groups) and runs the code in a
       resource-limited subprocess (aletheia/coder/demonstration_runner.py)
    -> harness applies the decision rule + NEGATIVE CONTROL + leakage/degeneracy probes
       (domains/base.py: holds = test_triggers AND control_silent AND probes_clean) — never an LLM 'holds'
    -> _audit_demonstration: an INDEPENDENT cross-vendor audit with the Opus AUTHOR EXCLUDED reviews the code;
       a refuting / non-independent audit FORCES holds=False (the author cannot override an adversarial finding)
    -> results gate (paradigm mode)
    -> _finalize_claims: harness-owned formulation strength + status
    -> a `claims` event surfaces the final ledger to the UI
  ```

  Anti-fakeability spine (four independent guards): (1) the pre-registration is committed before
  any result and read back immutably; (2) a mandatory negative control must stay silent; (3)
  harness-side leakage/degeneracy probes reject sham/empty controls and non-finite statistics; (4)
  an author-excluded cross-vendor audit can refute. The AI-authored capability is reachable ONLY by
  explicit id (`ai_authored_demonstration`), never keyword-matched, and a missing pre-registration
  or a sandbox failure fails closed to `not_evaluated` — never a silent pass.

  **Audit status is a distinct state, not a bare bool.** `_audit_demonstration` returns
  `(audit_passed, audit_error)` so the ledger never confuses two different things: an audit that
  RAN and refuted (`audit_passed is False` → forces `holds=False`, formulation `refuted`) versus an
  audit that COULD NOT run (`audit_error is True` → `holds` untouched, strength capped at `weak`,
  but NOT marked refuted — missing independent verification is not evidence against the claim). This
  mirrors the `not_evaluated` vs `refuted` distinction enforced elsewhere.

  **Domain generality.** The executor is domain-agnostic: any `DomainPlugin` that implements the
  `load_data` + `featurize` → `(X, y, groups)` contract inherits the AI-authored capability for
  free. Proven on **molecules** (real ESOL) and on **materials** (Magpie composition features,
  chemical-system groups — `tests/test_paradigm_p5_materials.py`; real e2e in
  `scripts/real_ai_demonstration_e2e_materials.py`). **RAG is intentionally out of scope:** its
  plugin owns a retrieve→answer→score pipeline and deliberately raises `NotImplementedError` in
  `featurize` (`aletheia/domains/rag/plugin.py`), so it cannot ground an AI-authored demonstration
  without a separate eval-only path — a known limitation, not a TODO to paper over.

  **Auditability.** A real run leaves a single machine-readable summary under `artifacts/`
  (`scripts/_e2e_common.py`) recording which route fired (AI-authored vs a registered fallback),
  the demonstration payload, the audit verdict, and the final claim ledger — so a reviewer can tell
  whether the frontier path actually fired without scraping the console.

## What this is NOT

Not a license to pass ungrounded grandiosity. The bar in paradigm mode is arguably *higher*
than performance mode (novelty must survive an adversarial literature attack AND the
demonstration must reproduce). The spine — claims↔evidence, adversarial critics, fail-closed,
harness-owned claim strength — is unchanged; it gains a second grounded standard, not a softer
one.
