"""Turn surveyed papers into a citable reference list + BibTeX.

Pure functions (no network) used by the WRITE_UP stage to assemble a real cited
paper. References come ONLY from the papers the SURVEY actually retrieved
(``research.literature.Paper``) — the writer cites these by their ``[n]`` key and
never invents a source. Dedupe is by ``Paper.key()`` (normalized DOI, else title).
"""

from __future__ import annotations

import re

from aletheia.research.literature import Paper


def _dedupe(papers: list[Paper]) -> list[Paper]:
    seen: set[str] = set()
    out: list[Paper] = []
    for p in papers:
        k = p.key()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _format_authors(authors: list[str], *, max_names: int = 3) -> str:
    names = [a.strip() for a in (authors or []) if a and a.strip()]
    if not names:
        return "Anon."
    if len(names) > max_names:
        return ", ".join(names[:max_names]) + ", et al."
    return ", ".join(names)


def _doi_or_url(p: Paper) -> str:
    if p.doi:
        d = p.doi.strip()
        if d.startswith("http"):
            return d
        return f"https://doi.org/{d.lstrip('doi:').strip()}"
    return (p.url or "").strip()


def numbered_references(papers: list[Paper]) -> tuple[list[Paper], str]:
    """Return ``(ordered_unique_papers, references_md)``.

    ``references_md`` is an IEEE-style numbered ``## References`` block; the
    writer cites entry *i* (1-based) inline as ``[i]``. Empty block → "" so the
    paper renders without a References section when no literature was retrieved.
    """
    refs = _dedupe(papers)
    if not refs:
        return [], ""
    lines = ["## References", ""]
    for i, p in enumerate(refs, start=1):
        authors = _format_authors(p.authors)
        title = (p.title or "").rstrip(".")
        venue = f" {p.venue.rstrip('.')}." if p.venue else ""
        year = f" {p.year}." if p.year else ""
        link = _doi_or_url(p)
        link = f" {link}" if link else ""
        lines.append(f'[{i}] {authors}, "{title}."{venue}{year}{link}'.rstrip())
    return refs, "\n".join(lines)


def _bibtex_key(p: Paper, used: set[str]) -> str:
    first = (p.authors[0].split()[-1] if p.authors and p.authors[0].split() else "anon")
    base = re.sub(r"[^A-Za-z0-9]", "", first).lower() or "anon"
    base = f"{base}{p.year}" if p.year else base
    key, n = base, 0
    while key in used:
        n += 1
        key = f"{base}{chr(ord('a') + n - 1)}"
    used.add(key)
    return key


def to_bibtex(papers: list[Paper]) -> str:
    """Emit deduped ``@article`` BibTeX entries (key = first-author+year)."""
    refs = _dedupe(papers)
    used: set[str] = set()
    blocks: list[str] = []
    for p in refs:
        key = _bibtex_key(p, used)
        fields = [f"  title = {{{p.title}}}"]
        if p.authors:
            fields.append(f"  author = {{{' and '.join(p.authors)}}}")
        if p.year:
            fields.append(f"  year = {{{p.year}}}")
        if p.venue:
            fields.append(f"  journal = {{{p.venue}}}")
        if p.doi:
            fields.append(f"  doi = {{{p.doi}}}")
        if p.url:
            fields.append(f"  url = {{{p.url}}}")
        blocks.append("@article{" + key + ",\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(blocks)
