"""Contract tests for the file-backed campaign knowledge input (ARL-2 keystone B, PR B2)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aletheia.knowledge.ingestion import (
    ContentAccessClass,
    CorpusIngestionBundle,
    ProviderRetrievalMode,
)
from aletheia.research_controller.knowledge_input import (
    CorpusDirectoryError,
    build_file_backed_bundle,
    register_campaign_corpus,
)

PAPER_TEXTS = {
    "hamidieh-2018": (
        "A data-driven statistical model for predicting the critical temperature "
        "of superconductors. Using 82 features from superconducting materials, "
        "the model explains the variation in critical temperature."
    ),
    "second-2020": (
        "A second fixture record whose abstract mentions no numbers at all; it "
        "exists to exercise the zero-span closure path."
    ),
}

_SPAN_CLAIMS = ("82 features from superconducting materials",)


def _manifest() -> dict:
    first_text = PAPER_TEXTS["hamidieh-2018"]
    spans = []
    for index, claim in enumerate(_SPAN_CLAIMS):
        start = first_text.index(claim)
        spans.append(
            {
                "span_id": f"hamidieh-2018:claim-{index}",
                "text": claim,
                "char_start": start,
                "char_end": start + len(claim),
                "section": "abstract",
            }
        )
    return {
        "bundle_id": "arl2-fixture-corpus",
        "snapshot_id": "fixture-corpus-001",
        "snapshot_version": "1",
        "cutoff_time": "2026-06-16T00:00:00Z",
        "frozen_at": "2026-09-15T12:00:00Z",
        "policy_frozen_at": "2026-09-15T00:00:00Z",
        "policy_id": "arl2.manual-import.abstract.v1",
        "source": {
            "source_id": "operator-manual-import",
            "snapshot_id": "fixture-import-2026-09-15",
            "updated_through": "2026-06-16T00:00:00Z",
            "retrieved_at": "2026-09-15T10:00:00Z",
            "license_id": "operator-attested-per-paper",
            "terms": (
                "operator attestation: each record was supplied by the PI from a "
                "personal copy; no automated retrieval; no redistribution"
            ),
        },
        "papers": [
            {
                "paper_id": "hamidieh-2018",
                "canonical_id": "doi:10.1016/j.commatsci.2018.07.053",
                "version_id": "v1",
                "title": (
                    "A data-driven statistical model for predicting the critical "
                    "temperature of superconductors"
                ),
                "authors": ["K. Hamidieh"],
                "venue": "Computational Materials Science",
                "publication_type": "journal_article",
                "peer_review_status": "peer_reviewed",
                "doi": "10.1016/j.commatsci.2018.07.053",
                "first_public_at": "2018-07-01T00:00:00Z",
                "version_public_at": "2018-07-01T00:00:00Z",
                "observed_at": "2026-09-15T10:00:00Z",
                "source_urls": ["https://doi.org/10.1016/j.commatsci.2018.07.053"],
                "license_id": "publisher-terms",
                "license_terms": (
                    "publisher subscription terms; personal-use copy supplied by the PI"
                ),
                "text_file": "papers/hamidieh-2018.txt",
                "spans": spans,
            },
            {
                "paper_id": "second-2020",
                "canonical_id": "arxiv:2001.00002",
                "version_id": "v1",
                "title": "A second fixture record for the corpus closure tests",
                "authors": ["A. Author", "B. Author"],
                "publication_type": "preprint",
                "peer_review_status": "not_peer_reviewed",
                "first_public_at": "2020-01-02T00:00:00Z",
                "version_public_at": "2020-01-02T00:00:00Z",
                "observed_at": "2026-09-15T10:00:00Z",
                "source_urls": ["https://arxiv.org/abs/2001.00002"],
                "license_id": "arxiv-license",
                "license_terms": "arXiv non-exclusive license; abstract text public metadata",
                "license_evidence_status": "article_level_terms",
                "text_file": "papers/second-2020.txt",
            },
        ],
    }


def _write_corpus(root: Path, manifest: dict) -> Path:
    papers_dir = root / "papers"
    papers_dir.mkdir(parents=True)
    for entry in manifest["papers"]:
        text = PAPER_TEXTS[entry["paper_id"]]
        (papers_dir / f"{entry['paper_id']}.txt").write_text(text, encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root


def _corpus(tmp_path: Path) -> Path:
    return _write_corpus(tmp_path / "corpus", _manifest())


def _mutated_corpus(tmp_path: Path, mutate) -> Path:
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    return _write_corpus(tmp_path / "corpus", manifest)


def test_bundle_builds_and_rebuilds_identically(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    bundle = register_campaign_corpus(corpus)
    again = build_file_backed_bundle(corpus)
    assert again.bundle_sha256 == bundle.bundle_sha256
    assert bundle.corpus.temporal_mode.value == "reconstructed"
    assert {grant.access_class for grant in bundle.access_grants} == {
        ContentAccessClass.USER_PROVIDED
    }
    assert {receipt.retrieval_mode for receipt in bundle.provider_receipts} == {
        ProviderRetrievalMode.MANUAL_IMPORT
    }
    # Every post-cutoff observation carries as-of evidence, or the corpus contract refuses it.
    assert all(paper.as_of_evidence_sha256 for paper in bundle.corpus.papers)
    # One grant and one receipt per paper, and the zero-span paper is covered by an empty set.
    assert len(bundle.access_grants) == len(bundle.corpus.papers) == 2
    assert len(bundle.provider_receipts) == 2
    zero_span_paper = next(
        paper
        for paper in bundle.corpus.papers
        if paper.canonical_id == "arxiv:2001.00002"
    )
    zero_span_receipt = next(
        receipt
        for receipt in bundle.provider_receipts
        if receipt.paper_snapshot_sha256 == zero_span_paper.snapshot_sha256
    )
    assert zero_span_receipt.source_span_sha256s == ()
    assert all(grant.redistributable is False for grant in bundle.access_grants)
    assert all(grant.automated_retrieval_permitted is False for grant in bundle.access_grants)


def test_bundle_round_trips_through_its_contracts(tmp_path: Path) -> None:
    bundle = register_campaign_corpus(_corpus(tmp_path))
    restored = CorpusIngestionBundle.model_validate(json.loads(bundle.model_dump_json()))
    assert restored.bundle_sha256 == bundle.bundle_sha256


def test_span_text_drift_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][0]["spans"][0]["text"] += " tampered suffix"
    with pytest.raises(CorpusDirectoryError, match="differs from its file slice"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_span_out_of_bounds_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][0]["spans"][0]["char_end"] = 10_000
    with pytest.raises(CorpusDirectoryError, match="out of bounds"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_missing_text_file_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][0]["text_file"] = "papers/absent.txt"
    with pytest.raises(CorpusDirectoryError, match="text file missing"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CorpusDirectoryError, match="lacks manifest.json"):
        build_file_backed_bundle(tmp_path / "empty")


def test_naive_timestamp_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["cutoff_time"] = "2026-06-16T00:00:00"
    with pytest.raises(CorpusDirectoryError, match="timezone-aware UTC"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_non_utc_offset_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["frozen_at"] = "2026-09-15T08:00:00-04:00"
    with pytest.raises(CorpusDirectoryError, match="timezone-aware UTC"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_retrieval_before_publication_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][0]["version_public_at"] = "2026-09-15T11:00:00Z"
        manifest["papers"][0]["observed_at"] = "2026-09-15T11:30:00Z"
    with pytest.raises(CorpusDirectoryError, match="between publication and freeze"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_observed_after_freeze_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][0]["observed_at"] = "2026-09-15T12:30:00Z"
    with pytest.raises(CorpusDirectoryError, match="observed after the corpus freeze"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_empty_source_urls_fail_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][1]["source_urls"] = []
    with pytest.raises(CorpusDirectoryError, match="at least one source URL"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_post_cutoff_publication_is_rejected_by_the_contract(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["papers"][1]["version_public_at"] = "2026-06-20T00:00:00Z"
        manifest["papers"][1]["first_public_at"] = "2026-06-20T00:00:00Z"
    with pytest.raises(ValueError, match="after cutoff"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_incompatible_license_evidence_is_rejected(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        # USER_PROVIDED text with institutional-contract evidence violates the grant contract.
        manifest["papers"][0]["license_evidence_status"] = "institutional_contract"
    with pytest.raises(ValueError, match="user-provided text requires"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))


def test_freeze_before_cutoff_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["frozen_at"] = "2026-06-01T00:00:00Z"
    with pytest.raises(CorpusDirectoryError, match="freeze before its cutoff"):
        build_file_backed_bundle(_mutated_corpus(tmp_path, mutate))
