#!/usr/bin/env python3
"""Review identity migration: read-only by default; apply approved bindings explicitly."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.data_loader import _save_sync
from src.review_migration import plan_identity_migration, prepare_approved_bindings
from src.storage.local_storage import LocalStorage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DATA_DIR)
    parser.add_argument("--bindings", type=Path, help="Reviewed one-to-one bindings and expected input hashes")
    parser.add_argument("--apply", action="store_true", help="Apply validated existing-review bindings only; never restores candidates")
    args = parser.parse_args()
    storage = LocalStorage(args.data_root)
    if args.apply and not args.bindings:
        parser.error("--apply requires --bindings")
    if not args.bindings:
        print(json.dumps(plan_identity_migration(storage), ensure_ascii=False, indent=2))
        return
    decisions = json.loads(args.bindings.read_text(encoding="utf-8"))
    reviews, state = prepare_approved_bindings(storage, decisions)
    if args.apply:
        _save_sync(storage, [
            ("processed/reviews.json", json.dumps([
                r.model_dump(mode="json", exclude={"source_block_id"} if not r.source_block_id else set()) for r in reviews
            ], ensure_ascii=False, indent=2)),
            ("state/state.json", json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)),
        ])
    print(json.dumps({"review_count": len(reviews), "binding_count": len(decisions['bindings']), "applied": args.apply}))


if __name__ == "__main__":
    main()
