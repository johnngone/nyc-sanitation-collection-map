"""Safely roll the current dataset pointer back to an installed release."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_validation import activate_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_version")
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
    args = parser.parse_args()
    manifest = activate_release(
        args.dataset_version,
        args.manifest,
        retention=args.release_retention,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
