#!/usr/bin/env python3
"""Read and verify the shadow-only research portfolio ledger."""

from __future__ import annotations

import argparse
import json
from typing import Any

from aletheia.schema_migrations import require_schema_exact
from aletheia.programs import (
    ResearchPortfolioError,
    ResearchPortfolioStore,
    PortfolioShadowAuditPolicy,
)


def _json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="verify and summarize a Quest's frozen slates")
    listing.add_argument("quest_id")
    slate = commands.add_parser("slate", help="reconstruct one proposal/assessment slate")
    slate.add_argument("slate_id")
    epoch = commands.add_parser("epoch", help="recompute and verify one shadow epoch")
    epoch.add_argument("epoch_id")
    audit = commands.add_parser("audit", help="aggregate human/planner shadow comparisons")
    audit.add_argument("quest_id")
    audit.add_argument("--minimum-epochs", type=int, default=20)
    audit.add_argument("--minimum-mean-jaccard-ppm", type=int, default=600_000)
    audit.add_argument("--maximum-human-hard-filter-violations", type=int, default=0)
    audit.add_argument("--maximum-planner-empty-epochs", type=int, default=0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    require_schema_exact()
    store = ResearchPortfolioStore()
    try:
        if args.command == "list":
            slates = store.list_slates(args.quest_id)
            _json(
                [
                    {
                        "slate_id": item.slate_id,
                        "graph_sha256": item.graph_snapshot.graph_sha256,
                        "candidate_count": len(item.spec.proposal.candidates),
                        "human_plan_id": item.human_plan_id,
                        "epoch_id": item.epoch_id,
                        "shadow_only": True,
                    }
                    for item in slates
                ]
            )
        elif args.command == "slate":
            _json(store.get_slate(args.slate_id).model_dump(mode="json"))
        elif args.command == "epoch":
            _json(store.get_epoch(args.epoch_id).model_dump(mode="json"))
        else:
            policy = PortfolioShadowAuditPolicy(
                minimum_epochs=args.minimum_epochs,
                minimum_mean_jaccard_ppm=args.minimum_mean_jaccard_ppm,
                maximum_human_hard_filter_violations=(args.maximum_human_hard_filter_violations),
                maximum_planner_empty_epochs=args.maximum_planner_empty_epochs,
            )
            _json(store.shadow_audit(quest_id=args.quest_id, policy=policy).model_dump(mode="json"))
        return 0
    except ResearchPortfolioError as exc:
        _json({"verified": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
