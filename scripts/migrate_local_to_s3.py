#!/usr/bin/env python3
"""Copy persisted local review data to the configured S3 data prefix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow execution as `python scripts/migrate_local_to_s3.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR, get_settings
from src.storage.base import StorageError
from src.storage.s3_storage import S3Storage


def migration_files(data_dir: Path) -> list[Path]:
    candidates = list((data_dir / "raw").glob("*.md"))
    candidates += [data_dir / "processed" / "reviews.json", data_dir / "state" / "state.json"]
    return [path for path in candidates if path.is_file() and path.name != "sample_reviews.md"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload local review data to S3 without deleting local files.")
    parser.add_argument("--dry-run", action="store_true", help="Show upload targets without uploading.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.s3_bucket:
        print("Configuration error: S3_BUCKET must be set.", file=sys.stderr)
        return 2

    files = migration_files(DATA_DIR)
    if not files:
        print(f"No migration files found under {DATA_DIR}.")
        return 0

    print("Existing S3 objects with the same key are overwritten. Local files are never deleted.")
    for local_path in files:
        relative = local_path.relative_to(DATA_DIR).as_posix()
        print(f"{local_path} -> s3://{settings.s3_bucket}/{settings.s3_prefix.strip('/')}/{relative}")

    if args.dry_run:
        print(f"Dry run complete: {len(files)} file(s) would be uploaded; 0 failed.")
        return 0

    try:
        storage = S3Storage(settings.s3_bucket, settings.s3_prefix, settings.aws_region, settings.s3_endpoint_url)
    except StorageError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    succeeded = failed = 0
    for local_path in files:
        relative = local_path.relative_to(DATA_DIR).as_posix()
        try:
            storage.save_text(relative, local_path.read_text(encoding="utf-8"))
            succeeded += 1
        except (OSError, StorageError) as exc:
            failed += 1
            print(f"FAILED {local_path}: {exc}", file=sys.stderr)
    print(f"Migration complete: {succeeded} succeeded, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
