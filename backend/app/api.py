import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pathlib import Path
import json

from .database import connect
from .config import DATABASE_PATH

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT")
VALID_TYPES = ("REFUSE", "RECYCLING", "ORGANICS", "BULK")


@router.get("/health")
def health() -> dict[str, object]:
    from .config import APP_ENV, DATA_MANIFEST_PATH

    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            count = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
            schedule_counts = {row[0]: row[1] for row in connection.execute("SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type")}
    except sqlite3.Error:
        LOGGER.exception("Health check could not inspect the local database")
        raise
    metadata = {}
    manifest = Path(DATA_MANIFEST_PATH)
    if manifest.exists():
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.exception("Could not read dataset manifest path=%s", DATA_MANIFEST_PATH)
    return {"status": "ok", "environment": APP_ENV, "processed_records": count, "schedule_counts": schedule_counts, "data_updated": metadata.get("processed_at"), "data_manifest": metadata.get("manifest_version")}


def _validate_bounds(west: float | None, south: float | None, east: float | None, north: float | None) -> None:
    supplied = [west, south, east, north]
    if any(value is not None for value in supplied) and any(value is None for value in supplied):
        raise HTTPException(status_code=422, detail="west, south, east, and north must be supplied together")
    if west is not None and east is not None and west >= east:
        raise HTTPException(status_code=422, detail="west must be less than east")
    if south is not None and north is not None and south >= north:
        raise HTTPException(status_code=422, detail="south must be less than north")
    if west is not None and not -180 <= west <= 180:
        raise HTTPException(status_code=422, detail="west is outside longitude range")
    if east is not None and not -180 <= east <= 180:
        raise HTTPException(status_code=422, detail="east is outside longitude range")
    if south is not None and not -90 <= south <= 90:
        raise HTTPException(status_code=422, detail="south is outside latitude range")
    if north is not None and not -90 <= north <= 90:
        raise HTTPException(status_code=422, detail="north is outside latitude range")


@router.get("/refuse-streets")
def refuse_streets(
    day: Annotated[str, Query(min_length=3, max_length=3)],
    types: str = "REFUSE",
    west: float | None = None,
    south: float | None = None,
    east: float | None = None,
    north: float | None = None,
) -> JSONResponse:
    day = day.upper()
    if day not in VALID_DAYS:
        raise HTTPException(status_code=422, detail=f"day must be one of {', '.join(VALID_DAYS)}")
    collection_types = [value.strip().upper() for value in types.split(",") if value.strip()]
    if not collection_types or not set(collection_types).issubset(VALID_TYPES):
        raise HTTPException(status_code=422, detail=f"types must contain only {', '.join(VALID_TYPES)}")
    _validate_bounds(west, south, east, north)

    query = """
        SELECT bf.*, GROUP_CONCAT(cs.weekday) AS collection_days,
               cs.collection_type,
               MAX(cs.source) AS source, MAX(cs.retrieved_at) AS retrieved_at
        FROM block_faces bf
        JOIN collection_schedules cs ON cs.block_face_id = bf.block_face_id
        WHERE cs.collection_type IN ({}) AND cs.weekday = ?
    """.format(",".join("?" for _ in collection_types))
    parameters: list[object] = [*collection_types, day]
    if west is not None:
        query += " AND bf.block_face_id IN (SELECT bm.block_face_id FROM block_face_rtree_map bm JOIN block_faces_rtree br ON br.rtree_id = bm.rtree_id WHERE br.max_x >= ? AND br.min_x <= ? AND br.max_y >= ? AND br.min_y <= ?)"
        parameters.extend([west, east, south, north])
    query += " GROUP BY bf.block_face_id, cs.collection_type ORDER BY bf.street_name, bf.side"

    database_path = DATABASE_PATH
    try:
        with connect(database_path) as connection:
            if west is not None and connection.execute("SELECT COUNT(*) FROM block_faces_rtree").fetchone()[0] == 0:
                query = query.replace("bf.block_face_id IN (SELECT bm.block_face_id FROM block_face_rtree_map bm JOIN block_faces_rtree br ON br.rtree_id = bm.rtree_id WHERE br.max_x >= ? AND br.min_x <= ? AND br.max_y >= ? AND br.min_y <= ?)", "bf.max_x >= ? AND bf.min_x <= ? AND bf.max_y >= ? AND bf.min_y <= ?")
            rows = list(connection.execute(query, tuple(parameters)))
    except sqlite3.Error:
        LOGGER.exception("Map query failed day=%s bounds=%s", day, parameters[1:])
        raise HTTPException(status_code=500, detail="Map data query failed") from None

    features = []
    for row in rows:
        geometry = _parse_geometry(row["geometry_wkt"])
        if geometry is None:
            LOGGER.error("Skipping invalid stored geometry block_face_id=%s", row["block_face_id"])
            continue
        properties = {
            "block_face_id": row["block_face_id"],
            "street_name": row["street_name"],
            "borough": row["borough"],
            "side": row["side"],
            "collection_type": row["collection_type"],
            "collection_days": sorted(set(row["collection_days"].split(","))),
            "source": row["source"] if "source" in row.keys() else "DSNY",
            "retrieved_at": row["retrieved_at"] if "retrieved_at" in row.keys() else "",
        }
        if row["collection_type"] == "REFUSE":
            properties["refuse_days"] = properties["collection_days"]
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": properties,
        })
    return JSONResponse({"type": "FeatureCollection", "features": features})


def _parse_geometry(wkt: str) -> dict[str, object] | None:
    prefix = "LINESTRING ("
    if wkt.startswith(prefix) and wkt.endswith(")"):
        coordinates = _parse_coordinate_pairs(wkt[len(prefix):-1])
        return {"type": "LineString", "coordinates": coordinates} if coordinates else None
    multi_prefix = "MULTILINESTRING (("
    if wkt.startswith(multi_prefix) and wkt.endswith("))"):
        parts = wkt[len("MULTILINESTRING ("):-1].split("),(")
        lines = [_parse_coordinate_pairs(part.strip("() ")) for part in parts]
        lines = [line for line in lines if line]
        return {"type": "MultiLineString", "coordinates": lines} if lines else None
    return None


def _parse_coordinate_pairs(value: str) -> list[list[float]] | None:
    coordinates = []
    for pair in value.split(","):
        values = pair.strip().split()
        if len(values) != 2:
            return None
        try:
            coordinates.append([float(values[0]), float(values[1])])
        except ValueError:
            return None
    return coordinates if len(coordinates) >= 2 else None
