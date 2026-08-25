"""The trusted scorer port preserves the frozen inspect_evals objective semantics."""

from __future__ import annotations

import json
import math

from aletheia.evals.adapters.corebench_scorer_entrypoint import (
    _write_terminal_receipt,
    evaluate_results,
)


def test_terminal_receipt_is_canonical_and_atomic(tmp_path):
    result = tmp_path / "result.json"
    _write_terminal_receipt(result, {"score": 1, "valid": True})

    assert json.loads(result.read_bytes()) == {"score": 1, "valid": True}
    assert result.read_bytes() == b'{"score":1,"valid":true}\n'
    assert list(tmp_path.iterdir()) == [result]


def test_numeric_prediction_interval_percent_and_punctuation_semantics():
    ground_truth = [
        {"numeric?": 10.0, "label.": "Hello!", "items:": [1, 2], "fig 1?": 5.0},
        {"numeric?": 11.0, "label.": "Hello!", "items:": [1, 2], "fig 1?": 5.0},
        {"numeric?": 9.0, "label.": "Hello!", "items:": [1, 2], "fig 1?": 5.0},
    ]
    result = evaluate_results(
        {
            "numeric!!!": "10%",
            "label???": "hello",
            "items...": [1, 2],
            "fig 1!!!": 5,
            "unknown": math.nan,
        },
        ground_truth,
    )
    assert result == {
        "correct_written_answers": 3,
        "correct_vision_answers": 1,
        "total_written_questions": 3,
        "total_vision_questions": 1,
    }


def test_missing_extra_and_wrong_answers_never_increase_correct_count():
    ground_truth = [{"a": 1.0, "b": "x"}] * 3
    assert evaluate_results({"a": 999, "c": "x"}, ground_truth) == {
        "correct_written_answers": 0,
        "correct_vision_answers": 0,
        "total_written_questions": 2,
        "total_vision_questions": 0,
    }
