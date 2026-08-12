# Autonomous-Discovery Co-Author Devlog — 2026-06-19 → 2026-06-22

A development + debugging log of bringing the **autonomous-discovery loop** to a working state where the
system both *proposes* and *vets* its own science, and a chronicle of the moving bottleneck that each fix
exposed. Branch: `feat/auto-discovery`. All live runs are OUTSIDE the Claude Code session (AUP).

---

## 0. Goal & starting point

The discovery loop (`aletheia/research/discovery.py`) has the system generate **bold candidate failure
modes** of a composition→property model, each carrying a runnable `compute_demonstration`, and
**self-screen** them through the full rigor filter:

```
prereg-valid → code-gate → runnable (smoke) → runs on REAL data → effect holds/separates
            → non-trivial → GROUNDED (≥3 papers) → cross-vendor NOVELTY gate (authors excluded)
```

Survivors are *novel-AND-feasible-AND-grounded*. It runs as an off-by-default driver STAGE
(`discovery_enabled`) inside the K2 campaign arc: **discover → demonstrate → audit → believe**.

Starting symptom: live arc runs reached `✗ FAIL` with **0 survivors**, and we were *blind to why* — the
driver published only a summary (`screened=N, n_survivors=0`), discarding the per-candidate detail.

---

## 1. The moving bottleneck (each fix exposed the next wall)

| # | Wall | Diagnosis | Fix |
|---|------|-----------|-----|
| 1 | **Blind to failures** | driver passed `log=lambda: None`; only the summary was published | publish `kill_tally` + per-candidate `breakdown` (stage died at, why, test/control, grounding, novelty) in the `discovery` event |
| 2 | **Smoke-probe false kills** | the smoke test ran code on a synthetic **8-col / integer-group** frame, but candidates parse `groups` as chemical-system **strings** (`group.split('-')`) → `AttributeError` / degenerate selection → killed *before* the real run (~60% of deaths were this artifact) | `smoke_test_demonstration(code, sample=(X,y,groups))` takes a **real-data slice**; `screen_deterministic` passes a 1024-row subsample (true dims + string groups). Synthetic frame kept as fallback |
| 3 | **Candidate code quality** | with the probe fixed, standalone breakdown showed grok writes buggy code (NaN stats, `ValueError: int()`, empty selections) and artifactual effects — *nothing* reached the novelty gate | **co-author**: grok proposes the ANGLE, **Claude writes the code** |
| 4 | **Co-author deadlock** | first co-author run HUNG at `state=discovering` (arc idle, 0% CPU, 28 min): `asyncio.run(run_worker())` inside `asyncio.to_thread(discover)` deadlocks the Claude SDK (its subprocess transport needs the **main-thread** loop; the httpx novelty gate tolerated a worker-thread loop, the SDK does not) | `make_claude_code_author(loop=)` submits authoring to the driver's main loop via `run_coroutine_threadsafe(coro, loop).result(timeout=600)`; standalone CLI keeps `asyncio.run`. Per-angle `[coauthor] i/N authored` progress added (kills the silent-stall false alarm) |
| 5 | **Effect validity** | co-author *worked* — runnability deaths **60% → 9%**, code-bug wall solved. New wall: **16/20** candidates die at `scored` — they run but the effect doesn't hold (clean control, but the test stat doesn't separate). grok's oblique angles are mostly **null** | mechanism-demand + survey-gap seeding in `materials_angle_context(gaps=)` + failure-mode-specific lessons (`_lesson_from`) |
| 6 | **Blind thresholds** | mechanism-grounding worked — effect sizes jumped **5–10×** (real physics: V-V dimers, lone-pairs, Mn³⁺/Mn⁴⁺ charge ordering). But effects with **clean vanishing controls** were killed for missing grok's `supported_if` threshold *guessed before seeing data* (e.g. `test=0.71 / ctrl=0.0007`) | decouple the screen from the blind threshold: judge on observed **separation** (`_DISCOVERY_SEP_RATIO=3.0`: control vanishes AND `|test| ≥ 3·max(|control|, control_bar)`). prereg still carried for the campaign's explore/confirm |
| 7 | **Novelty (the real wall)** | the candidates that reach the novelty gate **hold + are grounded** but are judged `major:novelty`. Pulling the critic's actual finding showed the gate is **fair**: grok keeps re-proposing the *one known meta-limitation* — "composition-average descriptors can't encode local/site structure" — which is *literally why GNNs (CGCNN) exist* | forbid that meta-framing in the angle prompt; demand a **named chemical family + quantified mechanism** (like the verified cuprate effect); feed the critic's **evidence** back as a lesson |

### kill_tally evolution (matbench_expt_gap, ~24 candidates/run)

```
grok writes code (pre-co-author):  runnability ~60%   scored ~40%   novelty 0
co-author round 1:                 runnability 9%      scored 87% (16/20 NULL)   novelty 1 (held+grounded, rejected known)
co-author round 2 (mechanism):     scored 17 (effects 5-10x bigger)   novelty 0
co-author round 3 (decoupled):     scored 16   novelty 1 (held+grounded, rejected known)
```

---

## 2. Current state

- **The autonomy pipeline is fully working and validated end-to-end** (offline + live): grok angle →
  Claude code → runs → separates from a vanishing control → grounded → reaches the cross-vendor novelty
  gate, every filter firing correctly.
- **On proxy egress** `critic_vendor_error ≈ 0` (zhipu responds), so the ≥2-vendor audit may NOT starve —
  a banked survivor could be **certified (FULL)**, not just an uncertified PARTIAL.
- **The remaining problem is pure novelty-targeting**, not engineering. On a heavily-studied band-gap
  benchmark, grok defaults to re-discovering the textbook limitation. The verified counter-example
  (cuprate plane-doping: multi-alkaline-earth cuprates, ~0.16 holes/Cu) shows the way out — **novelty is
  specificity**: a named family + a quantified mechanism, not a general principle.
- **Still `✗ FAIL`** every run, because 0 survivors → fallback to single-shot ideation → direction gate
  pauses, and the weak ideation-time belief updates trip "belief moved without a harness verdict."

---

## 3. Artifacts

**`aletheia/research/discovery.py`**
- `materials_angle_context(n, target_desc, gaps=)` — angles-only prompt; demands a physical MECHANISM,
  forbids the "composition misses structure" meta-framing, demands a named family + quantified mechanism,
  seeds the survey gaps.
- `CODE_AUTHOR_SYSTEM`, `code_author_prompt(angle, target_desc)` — Claude implements one angle on the
  exact screened contract.
- `make_claude_code_author(run_id, loop=, dry_run=, worker=, extract=)` — sync `author_fn`; routes the SDK
  call to the main loop via `run_coroutine_threadsafe` (the deadlock fix).
- `make_coauthor_ideator(angle_ideate_fn, author_fn, log=)` — grok angle + Claude code → candidate;
  per-angle progress.
- `screen_deterministic` — separation-based promote-or-not (`_DISCOVERY_SEP_RATIO`), decoupled from the
  blind threshold.
- `screen_novelty_grounding(..., exclude_vendors=)` — configurable author exclusion; captures the critic's
  novelty **evidence**.
- `_lesson_from(row)` — failure-mode-specific lessons (null vs confounded vs real-but-known).
- `discover(..., novelty_exclude=)` — surfaces per-candidate `breakdown`.

**`aletheia/coder/sandbox.py`** — `smoke_test_demonstration(code, sample=)` real-slice probe.

**`aletheia/config/settings.py`** — `discovery_coauthor` flag (novelty gate then excludes `{anthropic, grok}`).

**`aletheia/scheduler/driver.py`** — `_discover` builds the co-author ideator when `discovery_coauthor`,
passes the main loop + survey gaps, publishes `kill_tally` + `breakdown`.

**Scripts**
- `scripts/run_arc_proxy.sh` — run the arc THROUGH the proxy (recommended for co-author; heavy Claude
  authoring). Header prints `discovery_enabled=True discovery_coauthor=True`.
- `scripts/run_arc_direct.sh` — forced-direct arc launcher.
- `scripts/run_e2e_direct.sh` — now defaults to the ARC (`CUPRATE=1` for the cuprate campaign).
- `scripts/auto_discovery.py [--coauthor]` — standalone discovery breakdown (Claude-free without
  `--coauthor`; in-session safe).
- `scripts/real_discovery_campaign_e2e.py` — the autonomous arc (sets `discovery_enabled` +
  `discovery_coauthor`).

**Tests** — `tests/test_discovery_coauthor.py` (co-author merge/drop, worker+extract, loop-path,
end-to-end), `tests/test_discovery.py` (separation/decoupling), `tests/test_discovery_stage.py`.

---

## 4. Run & reproduce

```bash
# autonomous co-author arc (OUTSIDE the Claude Code session; FlClash ON / proxy):
bash scripts/run_arc_proxy.sh
#   header must read: discovery_enabled=True discovery_coauthor=True

# standalone discovery breakdown (Claude-free, in-session safe, ~90s):
conda run -n aletheia python scripts/auto_discovery.py

# tests:
conda run -n aletheia python -m pytest tests/test_discovery*.py -q
```

Diagnose a run from `artifacts/transcript_materials_<run_id>_*.jsonl`: find the `discovery … status=done`
event and read `kill_tally` + `breakdown` (per-candidate stage/why/test/control/grounded/novelty).

---

## 5. Open options (next)

1. **(active) Sharper novelty-targeting** — forbid the meta-framing + demand specificity + evidence-rich
   lessons (shipped 2026-06-22; needs a live run to validate).
2. **Richer data** — point discovery at the 21k UCI superconductor set (more samples/stratum, less
   benchmark-studied failure modes, and a verified novel effect provably exists there).
3. **Prove the full arc** — seed the verified cuprate effect to confirm bank → demonstrate → audit
   (certify on proxy) → believe end-to-end with a known-good survivor.
