"""Phase A-1: literature clients (arXiv + OpenAlex) — parsing, OpenAlex abstract
reconstruction, cross-source dedupe by DOI, best-effort degradation on error, and
ingest into the recall store. All offline (httpx is faked; no network)."""

from __future__ import annotations

import sys
import types

import pytest

from aletheia.research import literature
from aletheia.research.literature import Paper, search


@pytest.fixture(autouse=True)
def _passthrough_rerank(monkeypatch):
    """These tests cover SOURCE parsing/dedupe, not ranking — keep them fully offline by
    stubbing the cross-encoder rerank to a passthrough (no model download), and clear the
    process-wide search cache so per-test httpx stubs don't leak across tests."""
    import aletheia.research.rerank as rr

    monkeypatch.setattr(
        rr, "rerank_papers",
        lambda q, papers, **kw: list(papers)[: (kw.get("top_k") or len(papers))],
    )
    literature._search_cache.clear()

_ARXIV_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>Machine learning for band gap prediction</title>
    <summary>We predict band gaps from composition using ML.</summary>
    <published>2024-03-01T00:00:00Z</published>
    <id>http://arxiv.org/abs/2403.00001v1</id>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

_OPENALEX_JSON = {
    "results": [
        {
            "display_name": "Graph networks for materials",
            "publication_year": 2023,
            "doi": "https://doi.org/10.1/abc",
            "cited_by_count": 42,
            "primary_location": {"source": {"display_name": "Nature"}},
            "authorships": [{"author": {"display_name": "John Roe"}}],
            "abstract_inverted_index": {"Graph": [0], "networks": [1], "predict": [2]},
        }
    ]
}


def _fake_httpx(*, arxiv_text=_ARXIV_XML, openalex_json=_OPENALEX_JSON, s2_json=None, raise_on=None):
    class _Resp:
        def __init__(self, text="", data=None):
            self._text, self._data = text, data

        @property
        def text(self):
            return self._text

        def json(self):
            return self._data

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if raise_on and raise_on in url:
                raise RuntimeError("boom")
            if "arxiv" in url:
                return _Resp(text=arxiv_text)
            if "semanticscholar" in url:
                return _Resp(data=s2_json or {"data": []})  # default: S2 contributes nothing
            return _Resp(data=openalex_json)

    mod = types.ModuleType("httpx")
    mod.Client = _Client
    return mod


def test_search_parses_both_sources(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx())
    papers = search("band gap", k=8)
    assert len(papers) == 2
    by_src = {p.source: p for p in papers}
    assert by_src["arxiv"].title.startswith("Machine learning")
    assert by_src["arxiv"].year == 2024 and by_src["arxiv"].url.endswith("2403.00001v1")
    oa = by_src["openalex"]
    assert oa.year == 2023 and oa.citations == 42 and oa.venue == "Nature"
    assert oa.abstract == "Graph networks predict"  # inverted index reconstructed in order


def test_semantic_scholar_parsed(monkeypatch):
    s2 = {"data": [
        {"title": "Dense passage retrieval for QA", "abstract": "We propose DPR.",
         "year": 2020, "externalIds": {"DOI": "10.9/dpr"}, "venue": "EMNLP",
         "authors": [{"name": "V. Karpukhin"}], "citationCount": 1234,
         "url": "https://www.semanticscholar.org/paper/abc"},
        {"title": "", "abstract": "skip me"},  # titleless -> skipped
    ]}
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(s2_json=s2, arxiv_text="<feed/>"))
    papers = search("retrieval", k=8)
    s2p = [p for p in papers if p.source == "semanticscholar"]
    assert len(s2p) == 1
    assert s2p[0].year == 2020 and s2p[0].citations == 1234 and s2p[0].doi == "10.9/dpr"
    assert s2p[0].venue == "EMNLP"


def test_search_caches_repeated_query(monkeypatch):
    n = {"arxiv": 0}

    def fake_arxiv(q, k):
        n["arxiv"] += 1
        return [Paper(title="cached paper", source="arxiv")]

    monkeypatch.setattr(literature, "_arxiv", fake_arxiv)
    monkeypatch.setattr(literature, "_semantic_scholar", lambda q, k: [])
    monkeypatch.setattr(literature, "_openalex", lambda q, k: [])
    a = search("dup query", k=5)
    b = search("dup query", k=5)  # second call must hit the cache, not the sources
    assert n["arxiv"] == 1
    assert [p.title for p in a] == [p.title for p in b] == ["cached paper"]


def test_semantic_scholar_sends_api_key_header(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"data": []}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, headers=None, **k):
            captured["headers"] = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return _Resp()

    mod = types.ModuleType("httpx")
    mod.Client = _Client
    monkeypatch.setitem(sys.modules, "httpx", mod)
    import aletheia.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: types.SimpleNamespace(semantic_scholar_api_key="sk-2"))
    literature._semantic_scholar("q", 5)
    assert captured["headers"].get("x-api-key") == "sk-2"


def test_dedupe_across_sources_by_doi(monkeypatch):
    # arXiv entry carries the same DOI (raw) as OpenAlex (https://doi.org/...) -> one paper
    arxiv_with_doi = _ARXIV_XML.replace(
        "<author><name>Jane Doe</name></author>",
        "<arxiv:doi>10.1/abc</arxiv:doi><author><name>Jane Doe</name></author>",
    )
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(arxiv_text=arxiv_with_doi))
    papers = search("band gap", k=8)
    assert len(papers) == 1  # deduped despite differing DOI formats


def test_one_source_failing_still_returns_other(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(raise_on="openalex"))
    papers = search("band gap", k=8)
    assert len(papers) == 1 and papers[0].source == "arxiv"


def test_total_failure_degrades_to_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(raise_on="api"))  # both URLs contain "api"
    assert search("band gap", k=8) == []
    assert search("   ") == []  # empty query short-circuits


def test_ingest_into_recall():
    from aletheia.db import create_all
    from aletheia.memory.vector import recall

    create_all()
    n = literature.ingest(
        [Paper(title="LCSO is honest", abstract="leave chemical system out", year=2025,
               doi="10.5/zzz", citations=7, source="arxiv")],
        run_id="lit-run", dry_run=True,
    )
    assert n == 1
    # scope to this run (HashEmbedder only matches identical text; other tests' literature leaks in otherwise)
    hits = recall("LCSO is honest", kinds=["literature"], run_id="lit-run", dry_run=True)
    assert hits and hits[0]["kind"] == "literature" and (hits[0]["meta"] or {}).get("year") == 2025


def test_briefing_renders_year():
    out = literature.briefing([Paper(title="T", abstract="a", year=2022, citations=3, source="openalex")])
    assert "LITERATURE" in out and "2022" in out and "3 cites" in out
