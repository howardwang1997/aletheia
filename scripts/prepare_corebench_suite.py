"""Prepare the frozen Asta CORE-Bench-Hard public-validation mini-suite."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from aletheia.evals.adapters.corebench import (
    DEFAULT_COREBENCH_CAPSULE_IDS,
    OFFICIAL_CAPSULE_REQUIREMENTS,
    CoreBenchAdapter,
    CoreBenchScorer,
    CoreBenchSourceManifest,
    DockerCoreBenchHarness,
)
from aletheia.evals.schemas import ResourceBudget


DEFAULT_REQUIREMENTS = OFFICIAL_CAPSULE_REQUIREMENTS


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    from aletheia.config import get_settings

    default_image = get_settings().corebench_docker_image
    parser = argparse.ArgumentParser(
        description=(
            "Verify a pinned public CORE-Bench train annotation and locally supplied capsules; "
            "emit sanitized public assets plus evaluator-only answer receipts."
        )
    )
    parser.add_argument("--annotation", type=Path, required=True)
    parser.add_argument("--capsule-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--candidate-image", default=default_image)
    parser.add_argument("--scorer-image", default="aletheia-evaluator-agent:latest")
    parser.add_argument("--capsule-id", action="append", dest="capsule_ids")
    parser.add_argument("--wall-time-s", type=int, default=1800)
    parser.add_argument("--cpu-seconds", type=int, default=900)
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--token-cap", type=int)
    parser.add_argument("--usd-cap", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_root = args.output_root.expanduser().resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    capsule_root = args.capsule_root.expanduser().resolve(strict=True)
    if args.source_manifest:
        source = CoreBenchSourceManifest.model_validate_json(
            args.source_manifest.expanduser().resolve(strict=True).read_bytes()
        )
    else:
        source = CoreBenchSourceManifest.official_validation()
    adapter = CoreBenchAdapter(source)
    instances = adapter.load_instances(args.annotation.expanduser().resolve(strict=True))
    selected_ids = tuple(args.capsule_ids or DEFAULT_COREBENCH_CAPSULE_IDS)
    selected, subset = adapter.select_subset(instances, capsule_ids=selected_ids)
    unsupported = set(selected_ids) - set(DEFAULT_REQUIREMENTS)
    if unsupported:
        raise ValueError(
            "custom CORE-Bench capsules require a reviewed environment contract; unsupported: "
            f"{sorted(unsupported)}"
        )
    requirements = {capsule_id: DEFAULT_REQUIREMENTS[capsule_id] for capsule_id in selected_ids}
    receipts = [
        adapter.freeze_capsule(
            instance=instance,
            archive_path=capsule_root / f"{instance.capsule_id}.tar.gz",
            asset_root=output_root,
        )
        for instance in selected
    ]
    harness = DockerCoreBenchHarness.from_image_refs(
        candidate_image_ref=args.candidate_image,
        scorer_image_ref=args.scorer_image,
        source_manifest_sha256=source.manifest_sha256,
        public_asset_root=output_root / "public_assets",
        scratch_root=output_root / "scratch",
        supported_capsule_requirements=requirements,
    )
    scorer = CoreBenchScorer(harness=harness, source_manifest_sha256=source.manifest_sha256)
    budget = ResourceBudget(
        wall_time_s=args.wall_time_s,
        cpu_seconds=args.cpu_seconds,
        memory_mb=args.memory_mb,
        token_cap=args.token_cap,
        usd_cap=args.usd_cap,
    )
    tasks = [
        adapter.build_task(receipt=receipt, scorer=scorer, resource_budget=budget)
        for receipt in receipts
    ]
    suite = adapter.build_suite(tasks=tasks, subset_manifest=subset, scorer=scorer)
    hidden_paths = [
        adapter.stage_hidden_asset(evaluator_root=output_root, task=task, receipt=receipt)
        for task, receipt in zip(tasks, receipts, strict=True)
    ]
    bundle = {
        "schema_version": 1,
        "source_manifest": source.model_dump(mode="json"),
        "subset_manifest": subset.model_dump(mode="json"),
        "harness_manifest": harness.manifest.model_dump(mode="json"),
        "scorer_sha256": scorer.scorer_sha256,
        "suite": suite.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "asset_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "hidden_asset_paths": [str(path.relative_to(output_root)) for path in hidden_paths],
        "public_asset_paths": [
            str(
                Path("public_assets")
                / "corebench"
                / source.manifest_sha256
                / f"{receipt.instance.capsule_id}.tar.gz"
            )
            for receipt in receipts
        ],
        "upstream_test_downloaded_or_decrypted": False,
        "source_capsules_copied": False,
    }
    destination = output_root / "corebench_suite.v1.json"
    _atomic_json(destination, bundle)
    print(destination)


if __name__ == "__main__":
    main()
