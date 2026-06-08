"""The DEMONSTRATION worker: prompt + a constrained contract for AI-authoring a paradigm
contribution's *discriminating demonstration* (the frontier path), plus extraction of the
code block AND the structured pre-registration block from the worker's reply.

This is the autonomy step beyond ``coder/worker.py``: there the AI authors a MODEL and the
harness scores it; here the AI authors the DISCRIMINATING COMPUTATION itself. The trust
spine that keeps it from "grading its own homework": the AI returns only a TEST statistic
and a CONTROL statistic (never ``holds``); it pre-registers a STRUCTURED decision rule
BEFORE results exist; the harness applies that rule, runs leakage/degeneracy probes, and an
independent (different-vendor) auditor reviews the code. See base ``_compute_ai_authored_
demonstration`` and driver ``_demonstration_code`` / ``_audit_demonstration``.
"""

from __future__ import annotations

import json
from typing import Any

# reuse the solution extractor (same ```python fenced block``` convention)
from aletheia.coder.worker import extract_code  # noqa: F401 (re-exported for the driver)

DEMO_SYSTEM = (
    "You are a meticulous research scientist who authors DISCRIMINATING DEMONSTRATIONS: "
    "small, correct, leakage-free numerical code that measures whether a paradigm claim "
    "holds. You compute a TEST statistic and a CONTROL statistic where, if the claimed "
    "effect is real, the statistic should VANISH. You NEVER decide whether the effect holds "
    "— a fixed harness applies your PRE-REGISTERED decision rule. You never access data, "
    "files, the network, or the process; X/y/groups are passed to you. You return one python "
    "code block and one JSON pre-registration block.\n"
    "OUTPUT DISCIPLINE (hard): reply with ONLY the two fenced blocks and NOTHING else — no "
    "preamble, no explanation, no tool use. Do NOT write your answer to a file; return it "
    "INLINE. Use ASCII only: write operators as `<=` `>=` `!=` `*` and the word `in` — never "
    "Unicode math symbols (e.g. no perpendicular/bottom, <=, >=, !=, times, in, sqrt, sum)."
)

_CONTRACT_TEMPLATE = """\
Author a discriminating demonstration for the PARADIGM claim below. Write a Python module
that defines EXACTLY one function:

    def compute_demonstration(X, y, groups, meta):
        \"\"\"X: (n, d) float ndarray — {feature_desc} (host-featurized, trusted).
        y: (n,) float target. groups: (n,) object array (a grouping key, e.g. scaffold) or None.
        meta: {{"random_state": int, "preregistration": <your committed rule, read-only>}}.

        Compute the discriminating statistic on a TEST condition AND a CONTROL condition where,
        if the claimed effect is REAL, the statistic should VANISH (the negative control). Use
        meta["random_state"] for ANY subsample/split so an independent re-run is a genuine
        re-computation. Return EXACTLY this dict (NO 'holds' — the harness decides that):
            {{"test_statistic": float,     # the discriminating quantity on the TEST condition
              "control_statistic": float,  # the SAME quantity on the CONTROL (should vanish)
              "components": dict,          # any supporting numbers (for the audit)
              "detail": str,               # one-line human summary of what was measured
              "n_test": int,               # sample size the test statistic was computed on
              "n_control": int}}           # sample size the control statistic was computed on
        \"\"\"

The claim is DISCRIMINATING only if the statistic is large on TEST and small on CONTROL. A
control that is trivially silent (empty/degenerate) or a test driven by a handful of points
will be REJECTED by the harness probes and the independent auditor — make the control a real
counterfactual and use adequate sample sizes (>= {min_samples} on each side).

Hard rules (your code is statically checked and rejected if violated):
- Import ONLY from: sklearn, numpy, scipy, pandas, math, statistics (+ typing, dataclasses,
  functools, itertools, collections, warnings, random).
- No file/network/process access; no eval/exec/open/__import__/pickle/joblib; no dunder
  introspection (__globals__, __subclasses__, ...).

Then PRE-REGISTER your decision rule as a JSON object (committed BEFORE results are seen, so
it cannot be tuned afterward). Return EXACTLY these keys:
    {{"statistic_name": str,         # name of the test statistic
      "computation": str,            # how the test statistic is computed (one sentence)
      "control_description": str,    # what the CONTROL condition is
      "expected_control": str,       # why the statistic VANISHES on the control if the effect is real
      "supported_if": {{"op": ">=" | ">" | "<=" | "<", "threshold": <float>}},   # on test_statistic
      "control_silent_if": {{"op": "<" | "<=", "threshold": <float>}}}}          # on control_statistic

Return your reply as ONE ```python ... ``` code block followed by ONE ```json ... ``` block."""

# canned, gate-passing demonstration for dry-run + as a safe reference. A trivial, honest
# discriminating demo: the variance of y on a TEST subset vs a CONTROL subset.
CANNED_DEMO = (
    "def compute_demonstration(X, y, groups, meta):\n"
    "    import numpy as np\n"
    "    rng = np.random.default_rng(int(meta.get('random_state', 0)))\n"
    "    n = len(y)\n"
    "    idx = rng.permutation(n)\n"
    "    half = n // 2\n"
    "    test, ctrl = idx[:half], idx[half:]\n"
    "    return {\n"
    "        'test_statistic': float(np.std(y[test])),\n"
    "        'control_statistic': float(np.std(y[ctrl])),\n"
    "        'components': {},\n"
    "        'detail': 'std(y) on a test half vs a control half (placeholder demo)',\n"
    "        'n_test': int(len(test)),\n"
    "        'n_control': int(len(ctrl)),\n"
    "    }\n"
)

CANNED_PREREGISTRATION = {
    "statistic_name": "std(y) on the test half",
    "computation": "standard deviation of the target on a random test half",
    "control_description": "standard deviation of the target on the complementary half",
    "expected_control": "a placeholder; the halves are exchangeable so the control does NOT vanish",
    "supported_if": {"op": ">=", "threshold": 0.0},
    "control_silent_if": {"op": "<", "threshold": 0.0},
}

_JSON_BLOCK_LANGS = ("json", "JSON")

# --- K1: the EXPLORATION probe (explore->confirm seal) ---------------------------------
EXPLORE_SYSTEM = (
    "You are a meticulous research scientist running an EXPLORATORY analysis. You look at an "
    "EXPLORATION subset of the data and measure DESCRIPTIVE quantities (effect sizes, spreads, "
    "rates) so you can later PRE-REGISTER a decision threshold the data can actually support — "
    "instead of guessing it blind. You decide NOTHING here: you return only descriptive numbers. "
    "You never access data, files, the network, or the process; X/y/groups are passed to you. "
    "Return ONE python code block and NOTHING else.\n"
    "OUTPUT DISCIPLINE (hard): reply with ONLY the fenced code block — no preamble, no explanation, "
    "no tool use. Do NOT write to a file; return it INLINE. ASCII only."
)

_EXPLORE_CONTRACT = """\
EXPLORATION PHASE. Write a Python module that defines EXACTLY one function:

    def explore_demonstration(X, y, groups, meta):
        \"\"\"X: (n, d) float ndarray — {feature_desc} (host-featurized, trusted; the EXPLORATION
        subset only). y: (n,) float target. groups: (n,) object array (a grouping key) or None.
        meta: {{"random_state": int}}.

        Measure DESCRIPTIVE quantities that reveal how large the claimed effect is on this kind of
        data, and how a future TEST statistic and its negative CONTROL would behave — enough to set
        a sensible pre-registered threshold later. Return EXACTLY this dict:
            {{"observations": {{<name>: float, ...}},  # descriptive numbers (NOT a verdict)
              "detail": str,                            # one-line human summary
              "n": int}}                                # rows used
        STRONGLY RECOMMENDED keys in `observations` (the harness uses them to sanity-check your later
        threshold against this exploration, so you cannot pre-register a threshold this data already
        contradicts):
            "expected_test_statistic": float,     # your point estimate, ON THIS EXPLORE SUBSET, of the
                                                  # TEST statistic you will later compute
            "expected_control_statistic": float   # the same estimate for the negative CONTROL
        You are NOT deciding whether anything holds. Returning ANY verdict field — `holds`,
        `test_statistic`, `control_statistic`, `supported_if`, `control_silent_if`, `verdict` —
        (at top level OR inside `observations`) is REJECTED by the harness.
        \"\"\"

Hard rules (your code is statically checked and rejected if violated):
- Import ONLY from: sklearn, numpy, scipy, pandas, math, statistics (+ typing, dataclasses,
  functools, itertools, collections, warnings, random).
- No file/network/process access; no eval/exec/open/__import__/pickle/joblib; no dunder
  introspection (__globals__, __subclasses__, ...).

Return your reply as ONE ```python ... ``` code block."""

# canned, gate-passing exploration for dry-run + as a safe reference.
CANNED_EXPLORATION = (
    "def explore_demonstration(X, y, groups, meta):\n"
    "    import numpy as np\n"
    "    return {\n"
    "        'observations': {'y_std': float(np.std(y)), 'y_mean': float(np.mean(y)),\n"
    "                         'n_groups': float(len(set(groups.tolist())) if groups is not None else 0)},\n"
    "        'detail': 'placeholder exploration: target spread + group count',\n"
    "        'n': int(len(y)),\n"
    "    }\n"
)


def exploration_prompt(
    hypothesis: dict[str, Any],
    demonstration: dict[str, Any],
    data_spec: dict[str, Any],
    *,
    feature_desc: str = "a dense numeric feature matrix",
) -> str:
    return (
        "Author the EXPLORATION probe for this PARADIGM contribution.\n\n"
        f"HYPOTHESIS:\n{json.dumps(hypothesis, indent=2)}\n\n"
        f"DEMONSTRATION CLAIM (the concrete case the incumbent frame cannot handle):\n"
        f"{json.dumps(demonstration, indent=2)}\n\n"
        f"DATA:\n{json.dumps(data_spec, indent=2)}\n\n" + _EXPLORE_CONTRACT.format(feature_desc=feature_desc)
    )


def demonstration_prompt(
    hypothesis: dict[str, Any],
    demonstration: dict[str, Any],
    data_spec: dict[str, Any],
    *,
    feature_desc: str = "a dense numeric feature matrix",
    min_samples: int = 20,
    exploration: dict[str, Any] | None = None,
) -> str:
    contract = _CONTRACT_TEMPLATE.format(feature_desc=feature_desc, min_samples=min_samples)
    explore_block = ""
    if exploration:
        explore_block = (
            "EXPLORATION OBSERVATIONS (measured on a DISJOINT exploration subset — calibrate your "
            "pre-registered threshold to what these support; the harness will CONFIRM on a held-out "
            "subset your code has NOT seen, so a threshold tuned to noise will be refuted):\n"
            f"{json.dumps(exploration, indent=2)}\n\n"
        )
    return (
        "Author the discriminating demonstration for this PARADIGM contribution.\n\n"
        f"HYPOTHESIS:\n{json.dumps(hypothesis, indent=2)}\n\n"
        f"DEMONSTRATION CLAIM (the concrete case the incumbent frame cannot handle):\n"
        f"{json.dumps(demonstration, indent=2)}\n\n"
        f"DATA:\n{json.dumps(data_spec, indent=2)}\n\n" + explore_block + contract
    )


def extract_preregistration(text: str) -> dict[str, Any] | None:
    """Pull the JSON pre-registration block from the worker reply. Returns None if absent or
    unparseable (the driver then falls back to the registered-capability path — fail closed)."""
    import re

    for lang in _JSON_BLOCK_LANGS:
        m = re.search(rf"```{lang}\s*(.*?)```", text or "", re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1).strip())
                return obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return None
    # last resort: a bare {...} after the code block
    m = re.search(r"\{.*\}", (text or "").split("```")[-1], re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None
