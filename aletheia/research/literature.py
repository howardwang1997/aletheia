"""Literature access — the lab's eyes on human knowledge.

Keyless clients for Semantic Scholar, arXiv, OpenAlex, and Crossref, merged + deduped into a
normalized ``Paper``. ``search`` is **best-effort**: any network/parse failure on a
source is logged and skipped (returns whatever the other source gave, or []), so
literature is an enrichment, never a dependency that can break the loop.
``ingest`` stores papers in the pgvector recall store (kind="literature") so the
designer can recall world knowledge, not just the lab's own past runs.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_API = "https://api.crossref.org/works"
_TIMEOUT = 20.0

# arXiv asks for <= 1 request / 3s and a descriptive User-Agent; bursting the survey's
# ~10 queries is exactly what triggered HTTP 429 + read timeouts. We pace arXiv calls
# (process-wide) and retry with backoff that honors Retry-After. OpenAlex joins the
# "polite pool" via a mailto, which gives it its own, more forgiving rate limits.
_UA = "Aletheia-AI-Scientist/1.0 (autonomous research agent; +https://github.com/howardwang1997/aletheia)"
_HEADERS = {"User-Agent": _UA}
_ARXIV_MIN_INTERVAL = 3.0
_arxiv_lock = threading.Lock()
_arxiv_last = 0.0
# Semantic Scholar's unauthenticated pool is ~1 req/s and 429s on bursts; pace it too.
_S2_MIN_INTERVAL = 1.1
_s2_lock = threading.Lock()
_s2_last = 0.0


def _pace_arxiv() -> None:
    """Block until >= _ARXIV_MIN_INTERVAL has elapsed since the last arXiv call
    (process-wide), so the survey's burst of queries does not trip arXiv's rate limit."""
    global _arxiv_last
    with _arxiv_lock:
        wait = _ARXIV_MIN_INTERVAL - (time.monotonic() - _arxiv_last)
        if wait > 0:
            time.sleep(wait)
        _arxiv_last = time.monotonic()


def _pace_s2() -> None:
    """Process-wide pacing for Semantic Scholar's unauthenticated rate limit."""
    global _s2_last
    with _s2_lock:
        wait = _S2_MIN_INTERVAL - (time.monotonic() - _s2_last)
        if wait > 0:
            time.sleep(wait)
        _s2_last = time.monotonic()


# --- per-source circuit breaker -------------------------------------------------------------
# Pacing + short retries protect ONE call, but a survey fans out many sub-queries: once a source
# is clearly throttled (429s/timeouts that survive the retries), paying the pace+retry cost again
# for EVERY remaining query both hammers the API and stalls the survey (seen live: dozens of
# consecutive arXiv/S2 429 lines). After _BREAKER_THRESHOLD consecutive cross-query failures a
# source's circuit OPENS and it is skipped until _BREAKER_COOLDOWN_S elapses; one success closes
# it. ``search`` stays best-effort either way — a skipped source is just an empty contribution,
# and retrieval health already surfaces weak coverage downstream.
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 180.0
_breaker_lock = threading.Lock()
_breaker_failures: dict[str, int] = {}
_breaker_open_until: dict[str, float] = {}


def _breaker_allows(source: str) -> bool:
    with _breaker_lock:
        return time.monotonic() >= _breaker_open_until.get(source, 0.0)


def _breaker_success(source: str) -> None:
    with _breaker_lock:
        _breaker_failures[source] = 0
        _breaker_open_until.pop(source, None)


def _breaker_failure(source: str) -> bool:
    """Record one cross-query failure; returns True iff this failure OPENED the circuit."""
    with _breaker_lock:
        n = _breaker_failures.get(source, 0) + 1
        if n >= _BREAKER_THRESHOLD:
            _breaker_open_until[source] = time.monotonic() + _BREAKER_COOLDOWN_S
            _breaker_failures[source] = 0
            return True
        _breaker_failures[source] = n
        return False


def _breaker_reset() -> None:
    """Test hook: clear circuit-breaker state (module-level, would leak across tests)."""
    with _breaker_lock:
        _breaker_failures.clear()
        _breaker_open_until.clear()


def _get_with_retry(client: Any, url: str, params: dict, *, retries: int = 2) -> Any:
    """GET with SHORT exponential backoff on 429 / 5xx / timeout, honoring a Retry-After
    header when present. Raises the last error if all attempts fail. Backoff is kept small
    (few attempts, capped) so a throttled source FAILS FAST and the survey proceeds
    best-effort with whatever the other sources returned, instead of stalling on minutes
    of 429 backoff across its many sub-queries.

    Exception classes are resolved defensively via ``getattr`` so a stubbed httpx
    (used in tests) without these attributes still works."""
    import httpx

    retry_excs = tuple(
        e for e in (getattr(httpx, "TimeoutException", None), getattr(httpx, "TransportError", None))
        if isinstance(e, type)
    )
    delay = 1.5
    last_exc: Exception | None = None
    for _attempt in range(retries):
        try:
            r = client.get(url, params=params)
            status = getattr(r, "status_code", 200)
            if status == 429 or status >= 500:
                retry_after = (getattr(r, "headers", {}) or {}).get("Retry-After")
                sleep_s = float(retry_after) if (retry_after or "").isdigit() else delay
                time.sleep(min(sleep_s, 8.0))  # capped: fail fast under throttling
                delay = min(delay * 2, 8.0)
                last_exc = RuntimeError(f"HTTP {status} from {url}")
                continue
            r.raise_for_status()
            return r
        except retry_excs as exc:  # network/timeout — retry with backoff
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed without an exception")  # pragma: no cover


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
    _pace_arxiv()  # respect arXiv's ~1-req/3s limit before issuing the call
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as c:
        r = _get_with_retry(c, ARXIV_API, params)
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

    # mailto puts us in OpenAlex's "polite pool" (its own, more forgiving rate limit).
    params = {"search": query, "per_page": k, "mailto": "aletheia-agent@users.noreply.github.com"}
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as c:
        r = _get_with_retry(c, OPENALEX_API, params)
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


def _semantic_scholar(query: str, k: int) -> list[Paper]:
    """Semantic Scholar Graph API — RELEVANCE-ranked search (its own algorithm +
    SPECTER2), the strongest keyless source for scientific relevance. Keyless (the
    shared pool is rate-limited; we pace + retry)."""
    import httpx

    params = {
        "query": query, "limit": min(max(k, 1), 100),
        "fields": "title,abstract,year,externalIds,venue,authors,citationCount,url",
    }
    # an optional S2 API key (env S2_API_KEY) lifts the harsh unauthenticated rate limit
    from aletheia.config import get_settings

    key = get_settings().semantic_scholar_api_key
    headers = {**_HEADERS, "x-api-key": key} if key else _HEADERS
    _pace_s2()
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as c:
        r = _get_with_retry(c, SEMANTIC_SCHOLAR_API, params)
        data = r.json()
    papers: list[Paper] = []
    for w in data.get("data", []) or []:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        ext = w.get("externalIds") or {}
        authors = [(a.get("name") or "").strip() for a in (w.get("authors") or [])]
        papers.append(
            Paper(
                title=title,
                authors=[a for a in authors if a],
                year=w.get("year"),
                doi=ext.get("DOI"),
                venue=w.get("venue"),
                abstract=(w.get("abstract") or "").strip(),
                url=w.get("url") or (f"https://doi.org/{ext.get('DOI')}" if ext.get("DOI") else None),
                citations=w.get("citationCount"),
                source="semanticscholar",
            )
        )
    return papers


def _crossref(query: str, k: int) -> list[Paper]:
    """Crossref REST API — broad, DOI-backed metadata fallback.

    Crossref is deliberately an additional source rather than a substitute for
    relevance-ranked Semantic Scholar/OpenAlex.  Its independent infrastructure
    keeps a temporary outage or shared-pool throttle at those services from turning
    a citable-literature gate into a single point of failure.
    """
    import html
    import re

    import httpx

    params = {
        "query.bibliographic": query,
        "rows": min(max(k, 1), 100),
        "mailto": "aletheia-agent@users.noreply.github.com",
        "select": (
            "DOI,title,author,published,container-title,URL,"
            "is-referenced-by-count,abstract"
        ),
    }
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as c:
        r = _get_with_retry(c, CROSSREF_API, params)
        data = r.json()

    papers: list[Paper] = []
    for w in ((data.get("message") or {}).get("items") or []):
        titles = w.get("title") or []
        title = str(titles[0] if titles else "").strip()
        if not title:
            continue
        date_parts = ((w.get("published") or {}).get("date-parts") or [[]])[0]
        year = int(date_parts[0]) if date_parts and str(date_parts[0]).isdigit() else None
        authors = []
        for author in w.get("author") or []:
            name = " ".join(
                x for x in (str(author.get("given") or "").strip(),
                            str(author.get("family") or "").strip()) if x
            )
            if name:
                authors.append(name)
        containers = w.get("container-title") or []
        raw_abstract = str(w.get("abstract") or "")
        abstract = " ".join(
            html.unescape(re.sub(r"<[^>]+>", " ", raw_abstract)).split()
        )[:2000]
        papers.append(
            Paper(
                title=title,
                authors=authors,
                year=year,
                doi=w.get("DOI"),
                venue=str(containers[0]).strip() if containers else None,
                abstract=abstract,
                url=w.get("URL") or (f"https://doi.org/{w['DOI']}" if w.get("DOI") else None),
                citations=w.get("is-referenced-by-count"),
                source="crossref",
            )
        )
    return papers


_search_cache: dict[tuple[str, int], list[Paper]] = {}
_search_cache_lock = threading.Lock()


def search(query: str, k: int = 8) -> list[Paper]:
    """Search Semantic Scholar, arXiv, OpenAlex, and Crossref; merge, dedupe, then rerank
    candidates by genuine query relevance (cross-encoder) and drop off-topic hits — so
    keyword recall is filtered to what actually matters. Best-effort: a failing source is
    skipped; if the reranker can't load, the merge order is kept. Returns up to ``k``.

    Results are cached process-wide by (query, k): the survey fans out many overlapping
    sub-queries across parallel librarians, so caching repeats avoids re-hitting (and
    re-throttling) the rate-limited APIs."""
    query = (query or "").strip()
    if not query:
        return []
    ckey = (query.lower(), k)
    with _search_cache_lock:
        if ckey in _search_cache:
            return list(_search_cache[ckey])
    # over-fetch per source so the reranker has a real pool to choose from
    per_source = max(k, 15)
    found: list[Paper] = []
    degraded = False
    for fn in (_semantic_scholar, _arxiv, _openalex, _crossref):  # S2 first: best relevance
        src = fn.__name__
        if not _breaker_allows(src):
            degraded = True
            continue  # circuit open: source was recently throttling/failing — skip quietly
        try:
            found.extend(fn(query, per_source))
            _breaker_success(src)
        except Exception as exc:  # pragma: no cover - network/parse, defensive
            degraded = True
            opened = _breaker_failure(src)
            suffix = (f" — circuit OPEN, skipping {src} for {int(_BREAKER_COOLDOWN_S)}s"
                      if opened else "")
            print(f"[literature] {src} failed: {exc}{suffix}", file=sys.stderr)
    seen: set[str] = set()
    merged: list[Paper] = []
    for p in found:
        kk = p.key()
        if not kk or kk in seen:
            continue
        seen.add(kk)
        merged.append(p)
    from aletheia.research.rerank import rerank_papers

    out = rerank_papers(query, merged, top_k=k)
    if not degraded:
        with _search_cache_lock:
            if len(_search_cache) < 1024:  # simple bound; a survey is far below this
                _search_cache[ckey] = list(out)
    return out


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
