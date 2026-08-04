"""Download official sources, build a staged citywide dataset, and promote it atomically."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

LOGGER = logging.getLogger("run_refresh")
DSNY_QUERY = "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0/query"
LION_URL = "https://data.cityofnewyork.us/download/2v4z-66xt/application/zip"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_dsny(output: Path) -> None:
    features = []
    offset = 0
    while True:
        params = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "resultOffset": offset, "resultRecordCount": 2000, "f": "geojson"}
        response = httpx.get(DSNY_QUERY, params=params, timeout=120, headers={"User-Agent": "nyc-sanitation-map/1.0"})
        response.raise_for_status()
        page = response.json().get("features", [])
        if not page:
            break
        features.extend(page)
        LOGGER.info("Downloaded DSNY features=%s", len(features))
        if len(page) < 2000:
            break
        offset += len(page)
    if not features:
        raise RuntimeError("DSNY download returned no features")
    output.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")


def download_file(url: str, output: Path) -> None:
    with httpx.stream("GET", url, timeout=180, follow_redirects=True, headers={"User-Agent": "nyc-sanitation-map/1.0"}) as response:
        response.raise_for_status()
        with output.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/app.sqlite3")))
    parser.add_argument("--manifest", type=Path, default=Path(os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        if args.manifest.exists():
            print(args.manifest.read_text(encoding="utf-8"))
        else:
            print("No dataset manifest exists")
        return
    if not args.allow_large_run:
        raise SystemExit("Refusing citywide processing without --allow-large-run")
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.processed_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nyc-refresh-", dir=str(args.processed_dir.parent)) as temporary:
        staging = Path(temporary)
        dsny = staging / "dsny_frequencies.geojson"
        lion_zip = staging / "lion.zip"
        LOGGER.info("Downloading complete DSNY frequency layer")
        download_dsny(dsny)
        LOGGER.info("Downloading complete LION archive")
        download_file(LION_URL, lion_zip)
        extracted = staging / "lion-extracted"
        shutil.unpack_archive(lion_zip, extracted)
        gdb = next(extracted.rglob("*.gdb"), None)
        if gdb is None:
            raise RuntimeError("LION archive did not contain an ESRI geodatabase")
        processed = staging / "citywide.geojson"
        failures = staging / "citywide_failures.jsonl"
        command = [sys.executable, "scripts/build_pilot.py", "--lion", str(gdb), "--lion-layer", "lion", "--frequencies", str(dsny), "--output", str(processed), "--failures", str(failures)]
        subprocess.run(command, check=True)
        staged_db = staging / "app.sqlite3"
        subprocess.run([sys.executable, "scripts/load_processed.py", str(processed), "--database", str(staged_db)], check=True)
        with sqlite3.connect(staged_db) as connection:
            block_faces = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
            schedules = dict(connection.execute("SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type"))
        processed_at = datetime.now(UTC).isoformat()
        manifest = {"manifest_version": 1, "processed_at": processed_at, "sources": {"dsny": DSNY_QUERY, "lion": LION_URL}, "input_sha256": {"dsny": sha256(dsny), "lion": sha256(lion_zip)}, "block_faces": block_faces, "schedule_counts": schedules, "failure_records": sum(1 for _ in failures.open(encoding="utf-8")) if failures.exists() else 0}
        manifest_stage = staging / "data_manifest.json"
        manifest_stage.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        args.database.parent.mkdir(parents=True, exist_ok=True)
        backup = args.database.with_suffix(args.database.suffix + ".previous")
        if args.database.exists():
            shutil.copy2(args.database, backup)
        os.replace(staged_db, args.database)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(manifest_stage, args.manifest)
        LOGGER.info("Promoted citywide database block_faces=%s schedules=%s", block_faces, schedules)


if __name__ == "__main__":
    main()
