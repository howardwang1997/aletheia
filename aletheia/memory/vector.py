"""Semantic recall over the experiment ledger (pgvector).

``index_chunk`` embeds a fragment of research reasoning and stores it;
``recall`` finds the nearest prior fragments by cosine distance. Both are
**best-effort**: any failure (DB down, embedder missing) is swallowed and logged
to stderr so recall is an optimisation, never a dependency that can break the
autonomous loop.
"""

from __future__ import annotations

import sys
from typing import Any

from aletheia.db import session_scope
from aletheia.memory.embedder import get_embedder
from aletheia.memory.ledger import MemoryChunk


def index_chunk(
    kind: str,
    text: str,
    *,
    run_id: str | None = None,
    experiment_id: str | None = None,
    meta: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> int | None:
    """Embed and store one recall chunk. Returns its id, or None on any failure
    (best-effort — never raises into the caller)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        vec = get_embedder(dry_run=dry_run).embed([text])[0]
        with session_scope() as s:
            chunk = MemoryChunk(
                run_id=run_id,
                experiment_id=experiment_id,
                kind=kind,
                text=text,
                embedding=vec,
                meta_json=meta,
            )
            s.add(chunk)
            s.flush()
            return chunk.id
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[recall] index_chunk failed ({kind}): {exc}", file=sys.stderr)
        return None


def recall(
    query: str,
    k: int | None = None,
    *,
    run_id: str | None = None,
    kinds: list[str] | None = None,
    exclude_run_id: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Return up to ``k`` prior chunks nearest to ``query`` by cosine distance.

    Filters: ``run_id`` scopes to one run; ``exclude_run_id`` drops the current
    run (cross-run "what did we try before?"); ``kinds`` restricts content type.
    Best-effort: returns [] on any failure.
    """
    query = (query or "").strip()
    if not query:
        return []
    from aletheia.config import get_settings

    k = k or get_settings().recall_k
    try:
        qvec = get_embedder(dry_run=dry_run).embed([query])[0]
        with session_scope() as s:
            stmt = s.query(
                MemoryChunk,
                MemoryChunk.embedding.cosine_distance(qvec).label("distance"),
            )
            if run_id is not None:
                stmt = stmt.filter(MemoryChunk.run_id == run_id)
            if exclude_run_id is not None:
                stmt = stmt.filter(MemoryChunk.run_id != exclude_run_id)
            if kinds:
                stmt = stmt.filter(MemoryChunk.kind.in_(kinds))
            rows = stmt.order_by("distance").limit(k).all()
            return [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "text": c.text,
                    "run_id": c.run_id,
                    "experiment_id": c.experiment_id,
                    "meta": c.meta_json,
                    "distance": float(distance),
                    "similarity": 1.0 - float(distance),
                }
                for c, distance in rows
            ]
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[recall] recall failed: {exc}", file=sys.stderr)
        return []


def format_briefing(chunks: list[dict[str, Any]]) -> str:
    """Render recalled chunks into a compact 'prior work' briefing for a prompt."""
    if not chunks:
        return ""
    lines = ["PRIOR WORK (from past runs — avoid repeating dead ends):"]
    for c in chunks:
        tag = c["kind"].replace("_", " ")
        snippet = c["text"][:240].replace("\n", " ")
        lines.append(f"- [{tag}, sim={c['similarity']:.2f}] {snippet}")
    return "\n".join(lines)
