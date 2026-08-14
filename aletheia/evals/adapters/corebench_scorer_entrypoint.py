"""Trusted objective scorer for the frozen Asta CORE-Bench-Hard adapter.

The comparison below is a dependency-light port of the MIT-licensed inspect_evals CORE-Bench
scorer frozen in ``CoreBenchSourceManifest``.  This file is mounted only into the scorer plane;
candidate reproduction programs receive neither this implementation nor the hidden answers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import string
import tempfile
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any


def _write_terminal_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Commit the trusted result before bypassing third-party interpreter teardown."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                (
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _strip_keys(value: dict[str, Any]) -> dict[str, Any]:
    return {key.rstrip(string.punctuation): item for key, item in value.items()}


def _clean_agent_results(value: object) -> dict[str, str | float]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str | float] = {}
    try:
        for key, item in value.items():
            try:
                if isinstance(item, str) and "%" in item:
                    item = item.replace("%", "")
                try:
                    cleaned[key] = float(item)
                except (ValueError, TypeError):
                    cleaned[key] = item
            except Exception:
                cleaned[key] = item
    except Exception:
        pass
    return cleaned


def _student_t_quantile_975(degrees_of_freedom: int) -> float:
    """Match scipy.stats.t.ppf(0.975, df) without giving the scorer a large dependency.

    CORE-Bench stores three reference runs, so df=2 is the normal path and has a closed form.
    The asymptotic expansion keeps this exact port well-defined for reviewed custom fixtures too.
    """

    if degrees_of_freedom <= 0:
        return math.nan
    if degrees_of_freedom == 1:
        return 12.706204736432095
    if degrees_of_freedom == 2:
        return 4.302652729696142
    z = NormalDist().inv_cdf(0.975)
    df = float(degrees_of_freedom)
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


def evaluate_results(agent_result: object, ground_truth: list[dict[str, Any]]) -> dict[str, int]:
    gt = [_strip_keys(record) for record in ground_truth]
    first = gt[0]
    numeric = [key for key, value in first.items() if isinstance(value, int | float)]
    lists = [key for key, value in first.items() if isinstance(value, list)]
    strings = [key for key, value in first.items() if isinstance(value, str)]
    total_written = sum(1 for key in numeric + lists + strings if "fig" not in key)
    total_vision = sum(1 for key in numeric + lists + strings if "fig" in key)
    cleaned = _strip_keys(_clean_agent_results(agent_result))

    intervals: dict[str, tuple[float, float]] = {}
    trials = len(gt)
    t_value = _student_t_quantile_975(trials - 1)
    for key in numeric:
        values = [float(trial[key]) for trial in gt]
        center = mean(values)
        deviation = stdev(values)
        margin = t_value * deviation * math.sqrt(1 + 1 / trials)
        intervals[key] = (center - margin, center + margin)

    correct_written = 0
    correct_vision = 0
    for key, candidate in cleaned.items():
        if key not in first:
            continue
        correct = False
        if key in numeric:
            if isinstance(candidate, str):
                try:
                    candidate = float(candidate)
                except ValueError:
                    candidate = math.nan
            if isinstance(candidate, int | float):
                lower, upper = intervals[key]
                correct = lower <= candidate <= upper
        elif key in lists:
            correct = candidate == first[key]
        elif key in strings:
            correct = str(candidate).lower().rstrip(string.punctuation) == str(
                first[key]
            ).lower().rstrip(string.punctuation)
        if correct:
            if "fig" in key:
                correct_vision += 1
            else:
                correct_written += 1
    return {
        "correct_written_answers": correct_written,
        "correct_vision_answers": correct_vision,
        "total_written_questions": total_written,
        "total_vision_questions": total_vision,
    }


def main() -> None:
    report_path = Path("/candidate/report.json")
    gold_path = Path(os.environ["COREBENCH_GOLD_PATH"]).resolve(strict=True)
    allowed_gold_root = Path("/gold").resolve(strict=True)
    if allowed_gold_root not in gold_path.parents:
        raise RuntimeError("CORE-Bench gold path escaped evaluator-only storage")

    report_sha256: str | None = None
    report_bytes = 0
    report_valid = False
    if report_path.is_file() and not report_path.is_symlink():
        raw_report = report_path.read_bytes()
        report_bytes = len(raw_report)
        report_sha256 = hashlib.sha256(raw_report).hexdigest()
        try:
            report = json.loads(raw_report)
            report_valid = isinstance(report, dict)
        except (UnicodeDecodeError, json.JSONDecodeError):
            report = {}
    else:
        report = {}

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    if not isinstance(gold, list) or not gold or not all(isinstance(row, dict) for row in gold):
        raise RuntimeError("CORE-Bench hidden answers are malformed")
    counts = (
        evaluate_results(report, gold)
        if report_valid
        else {
            "correct_written_answers": 0,
            "correct_vision_answers": 0,
            "total_written_questions": sum(1 for key in gold[0] if "fig" not in key),
            "total_vision_questions": sum(1 for key in gold[0] if "fig" in key),
        }
    )
    correct = (
        report_valid
        and counts["correct_written_answers"] == counts["total_written_questions"]
        and counts["correct_vision_answers"] == counts["total_vision_questions"]
    )
    receipt = {
        **counts,
        "correct": correct,
        "report_valid": report_valid,
        "report_sha256": report_sha256,
        "report_bytes": report_bytes,
    }
    _write_terminal_receipt(Path("/receipt/result.json"), receipt)

    # The fsynced receipt is this one-shot trusted process's terminal commit. Avoid waiting on
    # optional dependency teardown in a short-lived, no-network evaluator container.
    os._exit(0)


if __name__ == "__main__":
    main()
