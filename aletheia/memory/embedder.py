"""Pluggable text embedders for semantic recall.

Two backends behind one interface:
  - ``HashEmbedder``  — deterministic, offline, no deps beyond numpy. Used in
    dry-run and tests so recall is verifiable with zero spend and no network.
  - ``SentenceTransformerEmbedder`` — local sentence-transformers (all-MiniLM-L6-v2),
    the production default; runs offline on CPU, no per-call cost.

``get_embedder`` resolves the configured backend, degrading gracefully to the
hash backend when the model can't be loaded (e.g. sentence-transformers not yet
installed) so a missing optional dep never breaks the loop — EXCEPT when a caller
passes ``require_real=True`` (a real scientific metric depends on real semantics),
in which case it raises ``EmbedderUnavailableError`` rather than silently
substituting random vectors. Fail-closed beats reporting a meaningless number.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Protocol

import numpy as np

from aletheia.config import get_settings


class EmbedderUnavailableError(RuntimeError):
    """A real embedding backend was required but could not be loaded. Raised instead
    of silently degrading to the random hash stub, so a real run fails closed rather
    than reporting embedding-based metrics computed on meaningless vectors."""


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
        self._lock = threading.RLock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name)
            return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Keep model inference single-threaded at the Python-call boundary. Survey
        # librarians index concurrently, while the underlying torch model is not a
        # reliable re-entrant object on every CPU runtime.
        with self._lock:
            model = self._load()
            mat = np.asarray(model.encode(texts, normalize_embeddings=True), dtype=float)
        return mat.tolist()


_cached: Embedder | None = None
_cache_lock = threading.RLock()


def get_embedder(*, dry_run: bool = False, require_real: bool = False) -> Embedder:
    """Resolve the configured embedder (cached). dry_run → always the hash stub.
    A "local" backend that fails to load degrades to the hash stub so a missing
    optional dependency never breaks indexing/recall.

    ``require_real=True`` (a real scientific metric depends on real semantics, e.g.
    dense retrieval in a real run) disables the silent degrade: if the configured
    backend is the hash stub, or the real model cannot be loaded, it raises
    ``EmbedderUnavailableError`` instead of returning random vectors."""
    global _cached
    s = get_settings()
    if dry_run or s.embedding_backend == "hash":
        if require_real:
            backend = "dry-run" if dry_run else repr(s.embedding_backend)
            raise EmbedderUnavailableError(
                f"a real embedding backend is required but the active backend is {backend} "
                f"(random hash stub); refusing to compute embedding metrics on it"
            )
        # don't cache the dry-run/hash path under the same slot as production
        return HashEmbedder(s.embedding_dim)
    # reuse a cached REAL backend; never let a cached hash-fallback satisfy a
    # require_real caller (deps may have since been installed) — retry the load.
    with _cache_lock:
        if _cached is not None and not isinstance(_cached, HashEmbedder):
            return _cached
        try:
            emb: Embedder = SentenceTransformerEmbedder(s.embedding_model, s.embedding_dim)
            emb.embed(["warmup"])  # force the model load now so failures surface here
        except Exception as exc:
            if require_real:
                raise EmbedderUnavailableError(
                    f"the real embedding backend {s.embedding_model!r} could not be loaded "
                    f"({type(exc).__name__}: {exc}); refusing to fall back to the random hash stub"
                ) from exc
            emb = HashEmbedder(s.embedding_dim)
        _cached = emb
        return emb
