"""PHYSICS-IDEATION probe (A + C) — use the non-author critic vendors (zhipu, grok) as
materials-physics + ML EXPERTS to PROPOSE genuinely novel, falsifiable, physics-grounded failure-mode
hypotheses for a composition->property regressor, then hold-test the best with the existing probes.

Why: ~6 hand-crafted error-STRUCTURE effects all turned out known/circular/tautological on two
datasets. Manual error statistics is the wrong tool for NOVELTY. The right novelty source is
literature/physics-grounded ideation -- which the LLM vendors can supply. This is Claude-free (zhipu +
grok only), so it runs in-process with no AUP risk. The vendors must AVOID the exhausted categories
(applicability-domain, target-tail regression-to-mean, activity-cliffs/local-non-smoothness) and give
each hypothesis a COMPUTABLE statistic + a negative control that vanishes if the effect is real.

    conda run -n aletheia python scripts/physics_ideation_probe.py

Output: a ranked menu of candidate hypotheses (per vendor) for me to implement as effect_hold_probe
statistics. Pick the most novel + likely-to-hold + computable, then HOLD-test before any FULL.
"""

from __future__ import annotations

import json
import time

from aletheia.config import get_settings

_IDEATION_SYSTEM = (
    "You are a rigorous materials-physics + machine-learning scientist. You PROPOSE genuinely novel, "
    "FALSIFIABLE hypotheses about SYSTEMATIC, PHYSICALLY-MEANINGFUL failure modes of a composition->"
    "property regressor. You do NOT hedge; you commit to specific, computable claims. Output STRICT "
    "JSON only."
)

_CONTEXT = (
    "SETUP. A Random Forest regressor on Magpie composition features (statistical aggregates of "
    "element properties: mean/range/std of atomic number, electronegativity, valence, radius, etc.) "
    "predicts two targets in separate studies:\n"
    "  (1) superconducting critical temperature Tc -- UCI dataset, ~21k formulas (e.g. Ba0.2La1.8Cu1O4), "
    "Tc in 0..185 K, with per-element atomic-fraction columns available; families include cuprates "
    "(Cu+O), iron-based (Fe pnictides/chalcogenides), MgB2-type, and conventional BCS.\n"
    "  (2) experimental band gap -- matbench_expt_gap, ~4.6k formulas.\n"
    "We can compute ANY statistic from: the composition/formula, the Magpie feature vector, the model's "
    "predictions and signed residuals on a held-out split, and per-element fractions. We can refit the "
    "model on subsets. We have a permutation/matched-removal control framework.\n\n"
    "GOAL. Propose discriminating hypotheses about a systematic failure mode that is GROUNDED IN "
    "SPECIFIC MATERIALS PHYSICS (electron count / valence balance / charge neutrality / electronegativity "
    "contrast / specific chemical rules / family membership), NOT generic ML behavior.\n\n"
    "HARD EXCLUSIONS (these are known and/or circular -- do NOT propose them):\n"
    "  - applicability domain / distance-to-training-data / feature-space sparsity;\n"
    "  - target-tail regression-to-the-mean (model under-predicts high values);\n"
    "  - activity cliffs / local non-smoothness / Lipschitz roughness (any statistic where the "
    "'difficulty' label is computed FROM the target);\n"
    "  - generic 'more data helps' / learning-curve effects.\n\n"
    "REQUIREMENTS per hypothesis: it must be (i) computable from the inputs above WITHOUT using the "
    "target to define the difficulty strata (avoid circularity), (ii) have a NEGATIVE CONTROL that "
    "vanishes (~0) if and only if the effect is real, (iii) be physically motivated, (iv) be genuinely "
    "distinct from the exclusions.\n\n"
    "Return STRICT JSON: {\"hypotheses\": [{\"name\": str, \"target\": \"Tc\"|\"bandgap\"|\"both\", "
    "\"mechanism\": str (the physics, 1-3 sentences), \"strata\": str (how difficulty/strata are defined "
    "WITHOUT the target), \"statistic\": str (the discriminating quantity), \"control\": str (what "
    "vanishes if real), \"novelty\": str (why distinct from AD/tail/cliff), \"expected_effect\": "
    "\"large\"|\"medium\"|\"small\"}]}. Propose 3, ranked best-first."
)


def _ideate(vendor_id: str, model: str, base_url: str, key: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, max_retries=1, timeout=180.0)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _IDEATION_SYSTEM},
                  {"role": "user", "content": _CONTEXT}],
        response_format={"type": "json_object"},
        temperature=0.8,  # ideation wants diversity, not the review default 0.3
    )
    raw = resp.choices[0].message.content or "{}"
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
    return json.loads(raw)


def main() -> int:
    print("=" * 90)
    print("PHYSICS-IDEATION PROBE  (zhipu + grok as physics+ML ideators; Claude-free)")
    print("=" * 90)
    settings = get_settings()
    vendors = [c for c in settings.critics.active if c.id in ("zhipu", "grok") and c.transport == "api"]
    all_hyps: list[dict] = []
    for c in vendors:
        key = settings.vendor_key(c.id)
        base_url = settings.vendor_base_url(c.id) or c.base_url
        if not key or not base_url:
            print(f"\n[{c.id}] skipped (no key/base_url)")
            continue
        print(f"\n{'='*90}\n[{c.id} / {c.model}] ideating...")
        t = time.time()
        try:
            out = _ideate(c.id, c.model, base_url, key)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED ({time.time()-t:.0f}s): {type(exc).__name__}: {str(exc)[:160]}")
            continue
        hyps = out.get("hypotheses", []) if isinstance(out, dict) else []
        print(f"  {len(hyps)} hypotheses ({time.time()-t:.0f}s):")
        for i, h in enumerate(hyps, 1):
            h["_vendor"] = c.id
            all_hyps.append(h)
            print(f"\n  [{c.id} #{i}] {h.get('name','?')}  (target={h.get('target','?')}, "
                  f"expect={h.get('expected_effect','?')})")
            print(f"      mechanism : {str(h.get('mechanism',''))[:260]}")
            print(f"      strata    : {str(h.get('strata',''))[:220]}")
            print(f"      statistic : {str(h.get('statistic',''))[:220]}")
            print(f"      control   : {str(h.get('control',''))[:200]}")
            print(f"      novelty   : {str(h.get('novelty',''))[:200]}")
    print("\n" + "=" * 90)
    print(f"collected {len(all_hyps)} candidate hypotheses. Next: pick the most novel + computable + "
          "likely-to-hold (strata NOT defined from the target!) and implement as an effect_hold_probe "
          "statistic; HOLD-test before any FULL.")
    print("IDEATION_JSON " + json.dumps({"hypotheses": all_hyps}, default=str)[:6000])
    return 0 if all_hyps else 1


if __name__ == "__main__":
    raise SystemExit(main())
