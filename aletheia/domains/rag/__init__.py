"""RAG-evaluation domain — the first non-regression / AI-application domain.

Evaluates a retrieval-augmented-generation configuration on a QA set with
DETERMINISTIC, gold-anchored metrics (retrieval recall@k + answer token-F1 +
exact-match + measured latency/cost). The honest harness computes every metric;
a subjective faithfulness metric is added by the cross-vendor critic panel in the
driver (multi-model, never a single judge).
"""

from aletheia.domains.rag.plugin import RagEvalPlugin

__all__ = ["RagEvalPlugin"]
