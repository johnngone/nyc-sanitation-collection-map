"""Revalidate and atomically promote a prebuilt immutable release bundle."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_validation import (
    publish_release,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "staging",
        type=Path,
        help="Directory containing release_manifest.json and every bound artifact",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")),
    )
    parser.add_argument(
        "--release-retention",
        type=int,
        default=int(os.getenv("DATA_RELEASE_RETENTION", "2")),
    )
    parser.add_argument(
        "--min-lion-rows",
        type=int,
        default=int(os.getenv("MIN_LION_SOURCE_ROWS", "200000")),
    )
    parser.add_argument(
        "--min-dsny-rows",
        type=int,
        default=int(os.getenv("MIN_DSNY_SOURCE_ROWS", "500")),
    )
    parser.add_argument(
        "--min-output-features",
        type=int,
        default=int(os.getenv("MIN_OUTPUT_FEATURES", "100000")),
    )
    parser.add_argument(
        "--max-count-drop-percent",
        type=float,
        default=float(os.getenv("MAX_COUNT_DROP_PERCENT", "10")),
    )
    args = parser.parse_args()
    if args.release_retention < 2:
        raise SystemExit("--release-retention must be at least 2")
    if not 0 <= args.max_count_drop_percent < 100:
        raise SystemExit("--max-count-drop-percent must be at least 0 and below 100")

    # The staging manifest supplies all identities and checksums.  This command
    # deliberately does not invent missing trust metadata for loose files.
    manifest = publish_release(
        args.staging,
        args.manifest,
        retention=args.release_retention,
        regression_gate={
            "min_lion_rows": args.min_lion_rows,
            "min_dsny_rows": args.min_dsny_rows,
            "min_output_features": args.min_output_features,
            "max_drop_fraction": args.max_count_drop_percent / 100,
        },
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
