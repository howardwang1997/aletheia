"""Phase C: real cited paper output. The citation helpers build a numbered,
deduped reference list + BibTeX from surveyed papers; WRITE_UP assembles a
structured IMRAD paper that cites them and emits a references.bib artifact. All
dry-run (no network, no spend)."""

from __future__ import annotations

import asyncio
import types

from aletheia.db import create_all
from aletheia.memory.service import create_run, finalize_plan
from aletheia.paths import run_artifacts_dir
from aletheia.research.citations import numbered_references, to_bibtex
from aletheia.research.literature import Paper
from aletheia.scheduler.driver import ExperimentDriver


def _papers() -> list[Paper]:
    return [
        Paper(
            title="Magpie composition features for band-gap regression",
            authors=["Ada Lovelace", "Alan Turing"],
            year=2024, doi="10.1234/magpie", venue="npj Computational Materials",
            abstract="Composition-only descriptors predict band gaps.", source="arxiv",
        ),
        Paper(
            title="Leakage-aware evaluation for materials ML",
            authors=["Grace Hopper"],
            year=2023, citations=31, venue="Nature Materials",
            url="https://example.org/leakage", abstract="LCSO is the honest metric.", source="openalex",
        ),
    ]


# --- citation helpers -----------------------------------------------------
def test_numbered_references_formats_and_links():
    refs, md = numbered_references(_papers())
    assert len(refs) == 2
    assert md.startswith("## References")
    assert "[1]" in md and "[2]" in md
    assert "2024" in md and "2023" in md
    assert "doi.org/10.1234/magpie" in md  # DOI rendered as a link
    assert "https://example.org/leakage" in md  # url fallback when no DOI


def test_to_bibtex_emits_entries():
    bib = to_bibtex(_papers())
    assert bib.count("@article{") == 2
    assert "title = {" in bib and "year = {2024}" in bib
    assert "lovelace2024" in bib  # key = first-author surname + year


def test_references_dedupe_by_key():
    dup = _papers()[0]
    refs, _ = numbered_references([_papers()[0], dup, _papers()[1]])
    assert len(refs) == 2  # same DOI collapses


# --- WRITE_UP produces a structured cited paper ---------------------------
def test_write_up_dry_produces_cited_paper():
    create_all()
    run_id = create_run("write-up test", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    d = ExperimentDriver(run_id, dry_run=True)
    from aletheia.domains.registry import get_domain_plugin
    d.profile = get_domain_plugin("materials").profile()  # _run sets this before WRITE_UP
    d.survey_papers = _papers()
    d.hypothesis = {"statement": "GBM beats an LCSO baseline", "prediction": "lower LCSO MAE"}
    result = {
        "metrics": {
            "mae": 0.47, "r2": 0.6, "mae_lcso": 0.47, "r2_lcso": 0.6, "mae_cv_mean": 0.45,
            "mae_cv_std": 0.03, "mae_holdout": 0.40, "rmse_holdout": 0.7,
        },
        "info": {
            "eval_summary": "LCSO GroupKFold + RepeatedKFold 5x5 + baselines",
            "model_impl": "RandomForestRegressor",  # what actually ran
        },
    }
    rpanel = types.SimpleNamespace(consensus_verdict="approve", gate_passed=True)
    asyncio.run(d._write_up(
        {"objective": "predict band gap"}, {"model": "gradient_boosting"},
        result, "Hypothesis supported; LCSO MAE within baseline range.", rpanel, exp_id,
    ))

    adir = run_artifacts_dir(run_id)
    report = (adir / "report.md").read_text()
    for header in ("## Abstract", "## 1. Introduction", "## 2. Related Work",
                   "## 3. Method", "## 4. Results", "Limitations", "## References"):
        assert header in report, header
    assert "[1]" in report  # cites a real surveyed reference inline
    assert "figures/parity.png" in report
    assert "0.47" in report  # leads with the LCSO headline
    assert "RandomForestRegressor" in report  # states the model that ACTUALLY ran

    bib = (adir / "references.bib").read_text()
    assert "@article{" in bib
