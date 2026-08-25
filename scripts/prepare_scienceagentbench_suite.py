"""Prepare the frozen, license-safe ScienceAgentBench mini-suite without running a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from aletheia.evals.adapters.scienceagentbench import (
    DEFAULT_CC_BY_SUBSET_IDS,
    TASK_REQUIRED_DISTRIBUTIONS,
    DockerScienceAgentBenchHarness,
    ScienceAgentBenchAdapter,
    ScienceAgentBenchScorer,
    ScienceAgentBenchSourceManifest,
    parse_custom_instance_requirements,
)
from aletheia.evals.schemas import ResourceBudget


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


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

    default_image = get_settings().scienceagentbench_docker_image
    parser = argparse.ArgumentParser(
        description=(
            "Verify locally supplied ScienceAgentBench verified assets and emit evaluator-only "
            "content-addressed task/suite manifests. Unzipped benchmark assets are never copied."
        )
    )
    parser.add_argument("--annotation", type=Path, required=True)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        help="Optional explicit source-manifest JSON; omitted means the pinned official verified CSV.",
    )
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--benchmark-archive", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--candidate-image", default=default_image)
    parser.add_argument("--scorer-image", default=default_image)
    parser.add_argument("--instance-id", action="append", dest="instance_ids")
    parser.add_argument(
        "--required-distribution",
        action="append",
        default=[],
        metavar="INSTANCE_ID=DISTRIBUTION[,DISTRIBUTION...]",
        help=(
            "Reviewed package contract for a custom source manifest; repeat once per selected "
            "instance. Official pinned sources use the built-in reviewed contracts."
        ),
    )
    parser.add_argument("--wall-time-s", type=int, default=1800)
    parser.add_argument("--cpu-seconds", type=int, default=900)
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--token-cap", type=int)
    parser.add_argument("--usd-cap", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_root = args.output_root.expanduser().resolve(strict=False)
    benchmark_root = args.benchmark_root.expanduser().resolve(strict=True)
    archive = args.benchmark_archive.expanduser().resolve(strict=True)
    source = None
    if args.source_manifest is not None:
        source = ScienceAgentBenchSourceManifest.model_validate_json(
            args.source_manifest.expanduser().resolve(strict=True).read_bytes()
        )
    adapter = ScienceAgentBenchAdapter(source)
    instances = adapter.load_instances(args.annotation.expanduser().resolve(strict=True))
    selected, subset = adapter.select_subset(
        instances,
        instance_ids=tuple(args.instance_ids or DEFAULT_CC_BY_SUBSET_IDS),
    )
    selected_ids = tuple(instance.instance_id for instance in selected)
    if args.source_manifest is None:
        if args.required_distribution:
            raise ValueError(
                "--required-distribution is only valid with an explicit custom source manifest"
            )
        supported_requirements = {
            instance_id: TASK_REQUIRED_DISTRIBUTIONS[instance_id]
            for instance_id in selected_ids
        }
    else:
        supported_requirements = parse_custom_instance_requirements(
            args.required_distribution, selected_ids=selected_ids
        )
    harness = DockerScienceAgentBenchHarness.from_image_refs(
        candidate_image_ref=args.candidate_image,
        scorer_image_ref=args.scorer_image,
        benchmark_root=benchmark_root,
        scratch_root=output_root / "scratch",
        supported_instance_requirements=supported_requirements,
    )
    scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=adapter.source.manifest_sha256
    )
    budget = ResourceBudget(
        wall_time_s=args.wall_time_s,
        cpu_seconds=args.cpu_seconds,
        memory_mb=args.memory_mb,
        token_cap=args.token_cap,
        usd_cap=args.usd_cap,
    )
    archive_sha256 = _sha256_file(archive)
    receipts = [
        adapter.freeze_assets(
            instance=instance,
            benchmark_root=benchmark_root,
            benchmark_archive_sha256=archive_sha256,
        )
        for instance in selected
    ]
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
        "source_manifest": adapter.source.model_dump(mode="json"),
        "subset_manifest": subset.model_dump(mode="json"),
        "harness_manifest": harness.manifest.model_dump(mode="json"),
        "scorer_sha256": scorer.scorer_sha256,
        "suite": suite.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "asset_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "hidden_asset_paths": [str(path.relative_to(output_root)) for path in hidden_paths],
        "benchmark_assets_copied": False,
    }
    _atomic_json(output_root / "scienceagentbench_suite.v1.json", bundle)
    print(output_root / "scienceagentbench_suite.v1.json")


if __name__ == "__main__":
    main()
