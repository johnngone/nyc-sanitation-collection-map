"""Validate and atomically promote a completed citywide staging directory."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("staging", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/app.sqlite3"))
    parser.add_argument("--manifest", type=Path, default=Path("data/data_manifest.json"))
    args = parser.parse_args()
    staged_db = args.staging / "app.sqlite3"
    processed = args.staging / "citywide.geojson"
    failures = args.staging / "citywide_failures.jsonl"
    if not staged_db.exists() or not processed.exists():
        raise FileNotFoundError("staging directory must contain app.sqlite3 and citywide.geojson")
    with sqlite3.connect(staged_db) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"staged database integrity check failed: {integrity}")
        block_faces = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
        schedules = dict(connection.execute("SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type"))
    if block_faces <= 0:
        raise RuntimeError("staged database contains no block faces")
    manifest = {
        "manifest_version": 1,
        "processed_at": datetime.now(UTC).isoformat(),
        "sources": {
            "dsny": "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0/query",
            "lion": "https://data.cityofnewyork.us/download/2v4z-66xt/application/zip",
        },
        "staged_geojson_sha256": sha256(processed),
        "block_faces": block_faces,
        "schedule_counts": schedules,
        "failure_records": sum(1 for _ in failures.open(encoding="utf-8")) if failures.exists() else 0,
    }
    args.database.parent.mkdir(parents=True, exist_ok=True)
    if args.database.exists():
        shutil.copy2(args.database, args.database.with_suffix(args.database.suffix + ".previous"))
    live_stage = args.database.with_suffix(args.database.suffix + ".staged")
    shutil.copy2(staged_db, live_stage)
    os.replace(live_stage, args.database)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
