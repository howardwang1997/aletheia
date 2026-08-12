"""DIVERGENT-IDEATION probe — generate EXTREMELY NOVEL, 剑走偏锋 (oblique/contrarian) research
directions that are nonetheless FEASIBLE on the data we already have.

The lesson from 3 failed cuprate runs: every time novelty went up, the design needed MORE data
structure (replicates, crystal structure, high-D tight matching) → underpowered → rejected. So the
target here is the hard intersection: novelty that comes from the ANGLE, not from extra data.

This prompts the non-author vendors (zhipu, grok) as bold, contrarian scientists who are RUTHLESS
about feasibility, to propose unconventional hypotheses that are computable + adequately powered on
ONLY {composition/SMILES, target, a trained model's predictions/residuals}. Claude-free → safe.

    conda run -n aletheia python scripts/divergent_ideation_probe.py

Output: a ranked menu (novelty × feasibility) of off-beat candidates for me to cheap-triage (hold +
novelty-gate + groundability) before any live run.
"""

from __future__ import annotations

import json
import time

from aletheia.config import get_settings

_SYSTEM = (
    "You are a brilliant, contrarian research scientist (materials + chemistry + ML) who finds "
    "OBLIQUE, high-novelty research angles others miss — and you are RUTHLESS about feasibility: a "
    "beautiful idea that cannot be tested on the available data is worthless. You commit to specific, "
    "falsifiable, computable claims. Output STRICT JSON only."
)

_CONTEXT = (
    "GOAL. Propose EXTREMELY NOVEL, oblique ('剑走偏锋') research directions about composition/structure-"
    "free property-prediction MODELS — angles the field has NOT framed — where the novelty comes from "
    "the ANGLE, not from needing more data.\n\n"
    "AVAILABLE DATA (your design must need ONLY these — no exceptions):\n"
    "  - composition -> superconducting Tc: UCI SuperCon, ~21k formulas + critical_temp (K), Magpie "
    "features. NO replicate compositions, NO crystal structure.\n"
    "  - composition -> band gap: matbench_expt_gap, ~4.6k formulas + gap (eV), Magpie features.\n"
    "  - SMILES -> aqueous solubility: MoleculeNet ESOL, ~1.1k molecules, RDKit/Morgan features.\n"
    "  - For any of these: a trained model's predictions and signed residuals on a held-out split, "
    "and the raw feature matrix X.\n\n"
    "HARD FEASIBILITY BAR (this is what killed prior ideas — respect it): the discriminating statistic "
    "must be (i) computable from {features X, target y, model predictions, formula/SMILES element-set} "
    "ALONE; (ii) ADEQUATELY POWERED — hundreds+ of samples per arm, a SIMPLE statistic, a clean "
    "negative control that vanishes if the effect is null; (iii) NOT reliant on replicate samples, "
    "crystal structure, external datasets, oxidation-state proxies, or high-dimensional tight/caliper "
    "matching that shreds sample size.\n\n"
    "EXHAUSTED — do NOT propose any of these (all tried, rejected): applicability-domain / distance-to-"
    "training / support density; error-rises-with-rarity; activity/composition cliffs; "
    "epistemic-vs-aleatoric or Bayes-error-floor decompositions; the cuprate plane-doping story.\n\n"
    "WHAT 'OBLIQUE/CONTRARIAN' MEANS (aim here): a contrarian INVERSION (the field assumes X; test the "
    "opposite); a cross-domain method TRANSPLANT applied unexpectedly; 'the field optimizes/reports X "
    "but the load-bearing signal is Y'; an OVERLOOKED INVARIANCE or symmetry the model silently "
    "violates; a SURPRISING-IF-TRUE claim about what the model has actually learned vs what we assume. "
    "Surprise should come from the framing, not from exotic data.\n\n"
    "Return STRICT JSON: {\"candidates\": [{\"title\": str, \"insight\": str (the oblique thing everyone "
    "misses, 1-2 sentences), \"surprising_claim\": str (the falsifiable claim), \"test_statistic\": str "
    "(computable on the available data), \"negative_control\": str (vanishes if null), \"why_novel\": "
    "str (why it is oblique and NOT an exhausted category), \"why_feasible\": str (large-sample, simple "
    "stat, needs no special data), \"dataset\": \"Tc\"|\"bandgap\"|\"ESOL\"|\"any\", "
    "\"novelty\": <0-1>, \"feasibility\": <0-1>}]}. Propose 5, ranked by novelty * feasibility."
)


def _ideate(vendor_id: str, model: str, base_url: str, key: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, max_retries=1, timeout=200.0)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": _CONTEXT}],
        response_format={"type": "json_object"}, temperature=0.95,  # high for divergence
    )
    raw = (resp.choices[0].message.content or "{}").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
    return json.loads(raw)


def main() -> int:
    print("=" * 92)
    print("DIVERGENT-IDEATION PROBE  (剑走偏锋 + feasibility-first; zhipu + grok; Claude-free)")
    print("=" * 92)
    s = get_settings()
    vendors = [c for c in s.critics.active if c.id in ("zhipu", "grok") and c.transport == "api"]
    allc: list[dict] = []
    for c in vendors:
        key = s.vendor_key(c.id); base = s.vendor_base_url(c.id) or c.base_url
        if not key or not base:
            print(f"\n[{c.id}] skipped (no key)"); continue
        print(f"\n{'='*92}\n[{c.id} / {c.model}] ideating (divergent)...")
        t = time.time()
        try:
            out = _ideate(c.id, c.model, base, key)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED ({time.time()-t:.0f}s): {type(exc).__name__}: {str(exc)[:150]}"); continue
        cands = out.get("candidates", []) if isinstance(out, dict) else []
        print(f"  {len(cands)} candidates ({time.time()-t:.0f}s):")
        for k in cands:
            k["_vendor"] = c.id
            allc.append(k)
            nf = (float(k.get("novelty", 0) or 0)) * (float(k.get("feasibility", 0) or 0))
            print(f"\n  [{c.id}] {k.get('title','?')}  (nov={k.get('novelty')} feas={k.get('feasibility')} "
                  f"-> {nf:.2f}, data={k.get('dataset')})")
            print(f"      insight   : {str(k.get('insight',''))[:240]}")
            print(f"      claim     : {str(k.get('surprising_claim',''))[:240]}")
            print(f"      statistic : {str(k.get('test_statistic',''))[:220]}")
            print(f"      control   : {str(k.get('negative_control',''))[:180]}")
            print(f"      why novel : {str(k.get('why_novel',''))[:200]}")
            print(f"      why feas. : {str(k.get('why_feasible',''))[:200]}")
    allc.sort(key=lambda k: (float(k.get("novelty", 0) or 0)) * (float(k.get("feasibility", 0) or 0)), reverse=True)
    print("\n" + "=" * 92)
    print("TOP by novelty*feasibility:")
    for k in allc[:5]:
        nf = (float(k.get("novelty", 0) or 0)) * (float(k.get("feasibility", 0) or 0))
        print(f"  {nf:.2f}  [{k.get('_vendor')}] {k.get('title','?')}  (data={k.get('dataset')})")
    print(f"\ncollected {len(allc)} candidates. Next: cheap-triage the top few (hold-probe on the data "
          "+ direction novelty-gate + groundability) and bring the survivors to a live run.")
    print("IDEATION_JSON " + json.dumps({"candidates": allc}, default=str)[:8000])
    return 0 if allc else 1


if __name__ == "__main__":
    raise SystemExit(main())
