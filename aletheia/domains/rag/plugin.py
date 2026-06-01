"""RagEvalPlugin — evaluates a RAG configuration (retriever + extractive answerer)
on a QA set with deterministic gold metrics. Overrides ``run_experiment`` (the RAG
flow is retrieve→answer→score, not the (X,y) regression shape), so it runs on the
LOCAL backend; ``featurize``/``train_evaluate`` are intentionally unsupported."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from aletheia.domains.base import DomainPlugin, DomainProfile, ExperimentResult
from aletheia.domains.rag.dataset import load_qa
from aletheia.domains.rag.retriever import (
    extractive_answerer,
    gold_in_topk,
    normalized_exact_match,
    retrieve,
    token_f1,
)


class RagEvalPlugin(DomainPlugin):
    name = "rag"

    def load_data(self, data_spec: dict[str, Any]) -> Any:
        corpus, cases = load_qa(data_spec)
        return {"corpus": corpus, "cases": cases}

    # The RAG flow does not fit the regression (X, y, groups) contract; the plugin
    # owns the whole pipeline via run_experiment (local backend). Be explicit.
    def featurize(self, df: Any, design: dict[str, Any]) -> tuple[Any, Any, list[str], Any]:
        raise NotImplementedError(
            "rag domain uses run_experiment (retrieve→answer→score), not featurize; "
            "run it on the local compute backend"
        )

    def train_evaluate(
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path, groups: Any = None
    ) -> ExperimentResult:
        raise NotImplementedError("rag domain uses run_experiment, not train_evaluate")

    def run_experiment(
        self, design: dict[str, Any], data_spec: dict[str, Any], workdir: Path
    ) -> ExperimentResult:
        """Compute-path / dry entry: evaluate with the OFFLINE extractive answerer.
        Host-side LLM generation is driven by the scheduler via ``evaluate`` instead."""
        data = self.load_data(data_spec)
        return self.evaluate(
            data["corpus"], data["cases"], int(design.get("k", 5)),
            answerer=extractive_answerer, workdir=workdir, design=design,
        )

    def evaluate(
        self,
        corpus: list[dict],
        cases: list[dict],
        k: int,
        *,
        answerer: Any,
        workdir: Path,
        design: dict[str, Any] | None = None,
        answerer_label: str = "extractive top-sentence",
        cost_per_answer: float = 0.0,
        latency_per_answer_ms: float | None = None,
    ) -> ExperimentResult:
        """Deterministic RAG scoring with an INJECTED ``answerer(question, contexts) ->
        str``. Retrieval + every metric is the fixed harness (the answerer never grades
        itself); only the answer text comes from the (offline or host-side LLM) answerer.
        ``contexts`` is the list of retrieved passage texts."""
        design = design or {}
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        rows: list[dict[str, Any]] = []
        faithfulness_cases: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        for case in cases:
            q = case.get("question", "")
            gold = case.get("gold_answer", "")
            retrieved = retrieve(corpus, q, k)
            contexts = [r["text"] for r in retrieved]
            answer = str(answerer(q, contexts) or "")
            rows.append({
                "question": q,
                "retrieved": [r["doc_id"] for r in retrieved],
                "recall_hit": gold_in_topk(retrieved, case.get("gold_doc_ids", [])),
                "answer": answer,
                "gold_answer": gold,
                "f1": token_f1(answer, gold),
                "exact_match": normalized_exact_match(answer, gold),
            })
            faithfulness_cases.append({"question": q, "context": " ".join(contexts), "answer": answer})
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        n = max(1, len(rows))
        recall_at_k = sum(1 for r in rows if r["recall_hit"]) / n
        answer_f1 = sum(r["f1"] for r in rows) / n
        exact_match = sum(r["exact_match"] for r in rows) / n
        latency_ms = latency_per_answer_ms if latency_per_answer_ms is not None else elapsed_ms / n
        cost_usd = cost_per_answer * len(rows)

        metrics = {
            "answer_f1": answer_f1,  # HEADLINE (goal=max)
            "recall_at_k": recall_at_k,
            "exact_match": exact_match,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
        }
        eval_payload = {
            "protocol": {"headline_metric": "answer_f1", "k": k, "n_eval": len(rows),
                         "retrieval": "lexical token-overlap", "answerer": answerer_label},
            "per_case": rows,
        }
        eval_path = workdir / "eval.json"
        eval_path.write_text(json.dumps(eval_payload, indent=2))
        eval_summary = (
            f"RAG eval on {len(rows)} QA cases (k={k}): answer-F1 {answer_f1:.3f} (HEADLINE), "
            f"recall@{k} {recall_at_k:.3f}, EM {exact_match:.3f}, "
            f"{latency_ms:.1f} ms/query, ${cost_usd:.4f} — lexical retrieval + {answerer_label}"
        )
        return ExperimentResult(
            metrics=metrics,
            artifacts=[{"kind": "eval", "uri": str(eval_path)}],
            info={
                "n_eval": len(rows),
                "model": design.get("model", f"lexical_k{k}"),
                "model_impl": f"lexical retrieval + {answerer_label}",
                "protocol_status": "eval_set",  # not a grouped-CV protocol; not degraded either
                "eval_summary": eval_summary,
                "faithfulness_cases": faithfulness_cases,  # the driver scores these via the critic panel
            },
        )

    def baselines(self) -> list[dict[str, Any]]:
        return [
            {"model": "lexical_k3", "k": 3},
            {"model": "lexical_k5", "k": 5},
        ]

    def profile(self) -> DomainProfile:
        from aletheia.research.literature import Paper

        return DomainProfile(
            task="retrieval-augmented QA evaluation",
            headline_metric="answer_f1",
            headline_goal="max",  # higher F1 is better — surfaces the lower-is-better assumptions
            quality_via_critics=True,  # add a cross-vendor faithfulness metric in the driver
            host_side_run=True,  # the answerer is a real host-side LLM (trusted); scoring stays deterministic
            units="",
            protocol_desc=(
                "held-out QA eval set: deterministic recall@k + answer token-F1 + exact-match + "
                "measured latency/cost, plus cross-vendor faithfulness"
            ),
            feature_desc="a question + a retrieved passage set (lexical retriever over a corpus)",
            sota_reference="mini-QA RAG baseline: answer-F1 ≈ 0.25 (BM25 + extractive); higher is better",
            sota_rows=[
                {"method": "BM25 + extractive", "dataset": "mini-QA", "metric": "answer_f1",
                 "score": 0.25, "split_policy": "held-out eval set", "source": "baseline"},
            ],
            dry_literature_findings=[
                {"paper_id": "10.0000/rag", "method": "retrieval-augmented generation", "dataset": "open-domain QA",
                 "metric": "answer_f1", "result": "retrieval grounds answers + raises F1",
                 "limitation": "retriever recall caps end-to-end quality",
                 "gap": "few report recall@k + F1 + cost together", "relevance": "core method"},
            ],
            dry_papers=[
                Paper(title="Retrieval-augmented generation for knowledge-intensive QA",
                      abstract="Grounding a generator in retrieved passages improves factual QA.",
                      year=2023, source="arxiv"),
                Paper(title="Evaluating retrieval and answer quality jointly",
                      abstract="Recall@k, answer F1, latency and cost together characterize a RAG system.",
                      year=2024, citations=18, source="openalex"),
            ],
            dry_gaps=[
                "Few RAG studies report recall@k, answer F1, latency and cost on the same eval set.",
                "Faithfulness is rarely scored by an independent multi-model panel.",
            ],
            dry_frontier_methods=[
                {"name": "dense retrieval (DPR / embeddings) + LLM generation",
                 "why": "dense retrieval + a strong generator set the QA-RAG state of the art",
                 "source": "RAG / DPR literature"},
                {"name": "lexical retrieval (BM25) + extractive answerer",
                 "why": "a cheap, deterministic, no-API baseline", "source": "IR baselines"},
            ],
            dry_hypothesis={
                "statement": "a larger retrieval depth (k) raises answer-F1 on the QA eval set",
                "rationale": "more retrieved passages raise the chance the gold passage is present",
                "prediction": "answer-F1 increases with k",
                "novelty_note": "few baselines report the recall/F1/cost trade-off together",
            },
            dry_next_hypothesis={
                "statement": "an extractive answerer over the top passage beats returning the whole passage",
                "rationale": "tighter spans raise token-F1",
                "prediction": "higher answer-F1 than the full-passage baseline",
                "novelty_note": "rarely ablated on the same eval set",
            },
            dry_metrics={
                "answer_f1": 0.33, "recall_at_k": 1.0, "exact_match": 0.0,
                "latency_ms": 3.0, "cost_usd": 0.0,
            },
            dry_eval_summary=(
                "RAG eval on 4 QA cases (k=5): answer-F1 0.330 (HEADLINE), recall@5 1.000, EM 0.000, "
                "3.0 ms/query, $0.0000 — lexical retriever + extractive answerer"
            ),
        )
