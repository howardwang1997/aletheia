"""Literature access — the lab's eyes on human knowledge.

Keyless clients for arXiv (Atom API) + OpenAlex (JSON), merged + deduped into a
normalized ``Paper``. ``search`` is **best-effort**: any network/parse failure on a
source is logged and skipped (returns whatever the other source gave, or []), so
literature is an enrichment, never a dependency that can break the loop.
``ingest`` stores papers in the pgvector recall store (kind="literature") so the
designer can recall world knowledge, not just the lab's own past runs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
_TIMEOUT = 20.0


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    venue: str | None = None
    abstract: str = ""
    url: str | None = None
    citations: int | None = None
    source: str = ""

    def key(self) -> str:
        """Dedupe key: normalized DOI if present, else normalized title."""
        if self.doi:
            d = self.doi.lower().strip()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if d.startswith(prefix):
                    d = d[len(prefix):]
            return d
        return " ".join((self.title or "").lower().split())


def _arxiv(query: str, k: int) -> list[Paper]:
    import xml.etree.ElementTree as ET

    import httpx

    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    params = {"search_query": f"all:{query}", "start": 0, "max_results": k}
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
        r = c.get(ARXIV_API, params=params)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    papers: list[Paper] = []
    for e in root.findall("a:entry", ns):
        title = " ".join((e.findtext("a:title", default="", namespaces=ns) or "").split())
        if not title:
            continue
        published = e.findtext("a:published", default="", namespaces=ns) or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        authors = [
            (a.findtext("a:name", default="", namespaces=ns) or "").strip()
            for a in e.findall("a:author", ns)
        ]
        papers.append(
            Paper(
                title=title,
                authors=[a for a in authors if a],
                year=year,
                doi=e.findtext("arxiv:doi", default=None, namespaces=ns),
                abstract=" ".join((e.findtext("a:summary", default="", namespaces=ns) or "").split()),
                url=(e.findtext("a:id", default="", namespaces=ns) or "").strip() or None,
                source="arxiv",
            )
        )
    return papers


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex returns abstracts as an inverted index {word: [positions]}."""
    if not inverted:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:2000]


def _openalex(query: str, k: int) -> list[Paper]:
    import httpx

    params = {"search": query, "per_page": k}
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
        r = c.get(OPENALEX_API, params=params)
        r.raise_for_status()
        data = r.json()
    papers: list[Paper] = []
    for w in data.get("results", []):
        title = (w.get("display_name") or "").strip()
        if not title:
            continue
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        authors = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships", [])]
        papers.append(
            Paper(
                title=title,
                authors=[a for a in authors if a],
                year=w.get("publication_year"),
                doi=w.get("doi"),
                venue=venue,
                abstract=_reconstruct_abstract(w.get("abstract_inverted_index")),
                url=w.get("doi") or w.get("id"),
                citations=w.get("cited_by_count"),
                source="openalex",
            )
        )
    return papers


def search(query: str, k: int = 8) -> list[Paper]:
    """Search arXiv + OpenAlex, merge + dedupe. Best-effort: a failing source is
    skipped, not raised. Returns up to ``k`` papers."""
    query = (query or "").strip()
    if not query:
        return []
    found: list[Paper] = []
    for fn in (_arxiv, _openalex):
        try:
            found.extend(fn(query, k))
        except Exception as exc:  # pragma: no cover - network/parse, defensive
            print(f"[literature] {fn.__name__} failed: {exc}", file=sys.stderr)
    seen: set[str] = set()
    merged: list[Paper] = []
    for p in found:
        kk = p.key()
        if not kk or kk in seen:
            continue
        seen.add(kk)
        merged.append(p)
    return merged[:k] if k else merged


def ingest(papers: list[Paper], run_id: str | None = None, dry_run: bool = False) -> int:
    """Store papers in the recall index (kind="literature"). Returns the count indexed."""
    from aletheia.memory.vector import index_chunk

    n = 0
    for p in papers:
        text = f"{p.title}\n\n{p.abstract}".strip()
        meta: dict[str, Any] = {
            "doi": p.doi, "year": p.year, "url": p.url, "venue": p.venue,
            "citations": p.citations, "source": p.source, "authors": p.authors[:8],
        }
        if index_chunk("literature", text, run_id=run_id, meta=meta, dry_run=dry_run) is not None:
            n += 1
    return n


def briefing(papers: list[Paper], limit: int = 6) -> str:
    """A compact prior-work briefing for a prompt."""
    if not papers:
        return ""
    lines = ["LITERATURE (prior work — ground the design in it, don't repeat it):"]
    for p in papers[:limit]:
        yr = f", {p.year}" if p.year else ""
        cites = f", {p.citations} cites" if p.citations is not None else ""
        lines.append(f"- {p.title}{yr}{cites} [{p.source}] — {p.abstract[:160]}")
    return "\n".join(lines)
