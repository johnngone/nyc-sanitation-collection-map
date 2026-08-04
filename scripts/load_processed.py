"""Load validated processed GeoJSON into the development SQLite database."""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import initialize

LOGGER = logging.getLogger("load_processed")
VALID_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/app.sqlite3"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise ValueError("input must be a GeoJSON FeatureCollection")
    initialize(args.database)
    with sqlite3.connect(args.database) as connection:
        for feature in payload["features"]:
            properties = feature.get("properties", {})
            required = ("block_face_id", "segment_id", "street_name", "borough", "side", "refuse_days", "source", "retrieved_at")
            missing = [field for field in required if not properties.get(field)]
            if missing:
                raise ValueError(f"feature missing required fields: {missing}")
            geometry = shape(feature["geometry"])
            if geometry.geom_type not in ("LineString", "MultiLineString") or geometry.is_empty:
                raise ValueError(f"block face {properties['block_face_id']} is not a non-empty line geometry")
            min_x, min_y, max_x, max_y = geometry.bounds
            connection.execute(
                """INSERT INTO block_faces
                (block_face_id, segment_id, borough, street_name, side, geometry_wkt,
                 min_x, min_y, max_x, max_y)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(block_face_id) DO UPDATE SET
                 segment_id=excluded.segment_id, borough=excluded.borough,
                 street_name=excluded.street_name, side=excluded.side,
                 geometry_wkt=excluded.geometry_wkt, min_x=excluded.min_x,
                 min_y=excluded.min_y, max_x=excluded.max_x, max_y=excluded.max_y""",
                (properties["block_face_id"], properties["segment_id"], properties["borough"], properties["street_name"], properties["side"], geometry.wkt, min_x, min_y, max_x, max_y),
            )
            connection.execute("INSERT OR IGNORE INTO block_face_rtree_map (block_face_id) VALUES (?)", (properties["block_face_id"],))
            rtree_id = connection.execute("SELECT rtree_id FROM block_face_rtree_map WHERE block_face_id = ?", (properties["block_face_id"],)).fetchone()[0]
            connection.execute("INSERT OR REPLACE INTO block_faces_rtree (rtree_id, min_x, max_x, min_y, max_y) VALUES (?, ?, ?, ?, ?)", (rtree_id, min_x, max_x, min_y, max_y))
            days = properties["refuse_days"]
            if not isinstance(days, list) or not days or not set(days).issubset(VALID_DAYS):
                raise ValueError(f"invalid refuse_days for {properties['block_face_id']}: {days!r}")
            connection.execute("DELETE FROM collection_schedules WHERE block_face_id = ? AND collection_type = 'REFUSE'", (properties["block_face_id"],))
            connection.executemany(
                """INSERT INTO collection_schedules
                (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                VALUES (?, 'REFUSE', ?, ?, ?, 'PILOT_UNVALIDATED')""",
                [(properties["block_face_id"], day, properties["source"], properties["retrieved_at"]) for day in days],
            )
            schedules = properties.get("schedules", {"REFUSE": days})
            for collection_type, collection_days in schedules.items():
                if collection_type == "REFUSE":
                    continue
                if collection_type not in {"RECYCLING", "ORGANICS", "BULK"} or not isinstance(collection_days, list) or not set(collection_days).issubset(VALID_DAYS):
                    raise ValueError(f"invalid {collection_type} schedule for {properties['block_face_id']}: {collection_days!r}")
                connection.executemany(
                    """INSERT OR REPLACE INTO collection_schedules
                    (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                    VALUES (?, ?, ?, ?, ?, 'PILOT_UNVALIDATED')""",
                    [(properties["block_face_id"], collection_type, day, properties["source"], properties["retrieved_at"]) for day in collection_days],
                )
    LOGGER.info("Loaded %s features into %s", len(payload["features"]), args.database)


if __name__ == "__main__":
    main()
