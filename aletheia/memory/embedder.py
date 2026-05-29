"""Pluggable text embedders for semantic recall.

Two backends behind one interface:
  - ``HashEmbedder``  — deterministic, offline, no deps beyond numpy. Used in
    dry-run and tests so recall is verifiable with zero spend and no network.
  - ``SentenceTransformerEmbedder`` — local sentence-transformers (all-MiniLM-L6-v2),
    the production default; runs offline on CPU, no per-call cost.

``get_embedder`` resolves the configured backend, degrading gracefully to the
hash backend when the model can't be loaded (e.g. sentence-transformers not yet
installed) so a missing optional dep never breaks the loop.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from aletheia.config import get_settings


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-norm vector per input text."""
        ...


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class HashEmbedder:
    """Deterministic offline embedder: each text seeds a PRNG that fills a
    unit-norm vector. Same text → same vector; similar texts are NOT semantically
    close (this is a stub), but identical/near-duplicate text recalls reliably —
    enough to verify the index/recall/ordering machinery without spend."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _one(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "little")
        rng = np.random.default_rng(seed)
        return rng.standard_normal(self.dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        mat = np.vstack([self._one(t) for t in texts]) if texts else np.empty((0, self.dim))
        return _normalize(mat).astype(float).tolist()


class SentenceTransformerEmbedder:
    """Local sentence-transformers embedder (lazy-loaded, model cached)."""

    def __init__(self, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        mat = np.asarray(model.encode(texts, normalize_embeddings=True), dtype=float)
        return mat.tolist()


_cached: Embedder | None = None


def get_embedder(*, dry_run: bool = False) -> Embedder:
    """Resolve the configured embedder (cached). dry_run → always the hash stub.
    A "local" backend that fails to load degrades to the hash stub so a missing
    optional dependency never breaks indexing/recall."""
    global _cached
    s = get_settings()
    if dry_run or s.embedding_backend == "hash":
        # don't cache the dry-run/hash path under the same slot as production
        return HashEmbedder(s.embedding_dim)
    if _cached is not None:
        return _cached
    try:
        emb: Embedder = SentenceTransformerEmbedder(s.embedding_model, s.embedding_dim)
        emb.embed(["warmup"])  # force the model load now so failures degrade here
    except Exception:
        emb = HashEmbedder(s.embedding_dim)
    _cached = emb
    return emb
