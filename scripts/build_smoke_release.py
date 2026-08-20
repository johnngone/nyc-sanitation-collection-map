"""Build a one-feature versioned dataset for cross-container smoke tests.

This deliberately creates only the runtime artifacts consumed by the app. It
is not a production release bundle and cannot be passed to promote_staging.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import initialize
from backend.app.releases import VERSION_PATTERN
from scripts.build_tiles import build_tiles
from scripts.release_validation import (
    atomic_json,
    validate_database,
    validate_tileset,
)


DEFAULT_VERSION = "ci-smoke-v2"
DATA_UPDATED = "2026-08-19T12:00:00+00:00"
WEST, SOUTH, EAST, NORTH = -73.99, 40.70, -73.98, 40.71


def _build_database(path: Path, version: str) -> None:
    initialize(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO block_faces
               (block_face_id, origin_block_face_id, segment_id, borough,
                street_name, side, geometry_wkt, min_x, min_y, max_x, max_y)
               VALUES ('smoke-face', 'smoke-origin', 'smoke-segment', 'QUEENS',
                       'SMOKE TEST STREET', 'LEFT',
                       'LINESTRING (-73.99 40.70, -73.98 40.71)',
                       ?, ?, ?, ?)""",
            (WEST, SOUTH, EAST, NORTH),
        )
        connection.execute(
            "INSERT INTO block_face_rtree_map(block_face_id) VALUES ('smoke-face')"
        )
        rtree_id = connection.execute(
            "SELECT rtree_id FROM block_face_rtree_map WHERE block_face_id = 'smoke-face'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO block_faces_rtree VALUES (?, ?, ?, ?, ?)",
            (rtree_id, WEST, EAST, SOUTH, NORTH),
        )
        connection.execute(
            """INSERT INTO block_face_lion_components
               (block_face_id, component_index, segment_id, source_side,
                source_rows_json, source_indices_json, street_names_json,
                source_records_json, dsny_object_ids_json)
               VALUES ('smoke-face', 0, 'smoke-segment', 'LEFT', '[0]', '[0]',
                       '["SMOKE TEST STREET"]', '[{}]', '["1"]')"""
        )
        connection.execute(
            """INSERT INTO block_face_dsny_sources
               (block_face_id, dsny_object_id, frequency_row)
               VALUES ('smoke-face', '1', 0)"""
        )
        connection.executemany(
            """INSERT INTO collection_schedules
               (block_face_id, collection_type, weekday, source, retrieved_at,
                validation_status)
               VALUES ('smoke-face', ?, 'MON', 'DSNY_SMOKE_FIXTURE',
                       '2026-08-19', 'AUDITED_SIDE_OFFSET')""",
            [(kind,) for kind in ("REFUSE", "RECYCLING", "ORGANICS", "BULK")],
        )
        connection.execute(
            "INSERT INTO dataset_metadata(key, value) VALUES ('dataset_version', ?)",
            (version,),
        )


def build_smoke_release(data_dir: str | Path, version: str = DEFAULT_VERSION) -> dict[str, object]:
    """Create a checked SQLite/MBTiles pair and commit its v2 pointer last."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must be a URL-safe release identifier")
    data_root = Path(data_dir).resolve()
    pointer = data_root / "data_manifest.json"
    release_dir = data_root / "releases" / version
    if pointer.exists():
        raise FileExistsError(f"refusing to replace an existing manifest: {pointer}")
    if release_dir.exists():
        raise FileExistsError(f"refusing to replace an existing release: {release_dir}")
    release_dir.mkdir(parents=True)

    database = release_dir / "app.sqlite3"
    _build_database(database, version)
    database_summary = validate_database(database, expected_version=version)

    tileset = release_dir / "collection_streets.mbtiles"
    tile_report = build_tiles(
        database,
        tileset,
        minzoom=11,
        maxzoom=11,
        version=version,
        source_version=version,
        data_updated=DATA_UPDATED,
    ).as_dict()
    tileset_summary = validate_tileset(
        tileset,
        version,
        expected_database=database_summary,
        expected_database_path=database,
    )

    with sqlite3.connect(tileset) as connection:
        zoom, x, tms_y = connection.execute(
            """SELECT zoom_level, tile_column, tile_row
               FROM tiles ORDER BY zoom_level, tile_column, tile_row LIMIT 1"""
        ).fetchone()
    y = (1 << zoom) - 1 - tms_y

    manifest = {
        "manifest_version": 2,
        "dataset_version": version,
        "release_path": f"releases/{version}",
        "processed_at": DATA_UPDATED,
        "block_faces": database_summary["block_faces"],
        "schedule_counts": database_summary["schedule_counts"],
        "database": database_summary,
        "tileset": tileset_summary,
        "artifacts": {
            "database": {
                "path": database.name,
                "sha256": database_summary["sha256"],
            },
            "tileset": {
                "path": tileset.name,
                "sha256": tileset_summary["sha256"],
            },
        },
        "previous_releases": [],
        "smoke_fixture": True,
    }
    atomic_json(pointer, manifest)
    return {
        "version": version,
        "tile_schema_revision": tile_report["tile_schema_revision"],
        "bounds": tile_report["bounds"],
        "tile_url": f"/api/tiles/{version}/{zoom}/{x}/{y}.pbf",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args()
    print(json.dumps(build_smoke_release(args.data_dir, args.version), sort_keys=True))


if __name__ == "__main__":
    main()
