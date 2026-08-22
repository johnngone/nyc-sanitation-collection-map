import logging
import re
import sqlite3
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pathlib import Path
import json

from .config import (
    DATABASE_PATH,
    DATA_MANIFEST_PATH,
    HEALTH_SYNC_HASH_MAX_BYTES,
    TILESET_PATH,
)
from .releases import (
    CurrentRelease,
    ReleaseManifestError,
    VERSION_PATTERN,
    artifact_checksum_status,
    read_current_release,
    release_checksum_status,
    tileset_for_version,
)
from .tiles import VECTOR_TILE_MEDIA_TYPE, read_metadata, read_tile

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT")
VALID_TYPES = ("REFUSE", "RECYCLING", "ORGANICS", "BULK")
LEGACY_FEATURE_LIMIT = 20_000


def _read_only_database(path: str | Path) -> sqlite3.Connection:
    database = Path(path).resolve()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _current_release_integrity(release: CurrentRelease) -> str:
    return release_checksum_status(
        release,
        synchronous_max_bytes=HEALTH_SYNC_HASH_MAX_BYTES,
    )


def _require_verified_current_release(release: CurrentRelease) -> None:
    checksum_status = _current_release_integrity(release)
    if checksum_status != "verified":
        raise HTTPException(
            status_code=503,
            detail=f"Committed artifact checksums are {checksum_status}",
        )


def _unavailable_map_config() -> dict[str, object]:
    return {
        "available": False,
        "version": None,
        "tile_schema_revision": None,
        "tiles_url": None,
        "source_layer": "collection_streets",
        "known_source_layer": "collection_streets",
        "unknown_source_layer": None,
        "unknown_minzoom": None,
        "minzoom": None,
        "maxzoom": None,
        "bounds": None,
        "data_updated": None,
    }


@router.get("/map-config")
def map_config() -> JSONResponse:
    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    if release is not None:
        checksum_status = _current_release_integrity(release)
        if checksum_status != "verified":
            LOGGER.warning(
                "Committed map artifacts are not checksum-ready status=%s version=%s",
                checksum_status,
                release.dataset_version,
            )
            return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    tileset_path = release.tileset_path if release is not None else Path(TILESET_PATH)
    try:
        metadata = read_metadata(tileset_path)
    except FileNotFoundError:
        LOGGER.info("Vector tileset is not published yet path=%s", tileset_path)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.warning("Vector tileset is unavailable path=%s", tileset_path, exc_info=True)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    if release is not None and metadata.version != release.dataset_version:
        LOGGER.error(
            "Committed tileset version does not match manifest expected=%s actual=%s",
            release.dataset_version,
            metadata.version,
        )
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    return JSONResponse(
        {
            "available": True,
            "version": metadata.version,
            "tile_schema_revision": metadata.tile_schema_revision,
            "tiles_url": f"/api/tiles/{metadata.version}/{{z}}/{{x}}/{{y}}.pbf",
            "source_layer": metadata.source_layer,
            "known_source_layer": metadata.source_layer,
            "unknown_source_layer": metadata.unknown_source_layer,
            "unknown_minzoom": metadata.unknown_minzoom,
            "minzoom": metadata.minzoom,
            "maxzoom": metadata.maxzoom,
            "bounds": list(metadata.bounds),
            "data_updated": metadata.data_updated,
        },
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/tiles/{version}/{z}/{x}/{y}.pbf")
def vector_tile(version: str, z: int, x: int, y: int, request: Request) -> Response:
    if not VERSION_PATTERN.fullmatch(version):
        raise HTTPException(status_code=404, detail="Vector tileset is unavailable")
    tileset_path: Path | None = None
    metadata = None
    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        raise HTTPException(status_code=503, detail="Committed map release is invalid") from None
    if release is not None:
        selected = tileset_for_version(release, version)
        if selected is not None:
            checksum_status = (
                _current_release_integrity(release)
                if version == release.dataset_version
                else artifact_checksum_status(
                    selected.path,
                    selected.sha256,
                    synchronous_max_bytes=HEALTH_SYNC_HASH_MAX_BYTES,
                )
            )
            if checksum_status != "verified":
                raise HTTPException(
                    status_code=503,
                    detail=f"Vector tileset checksum is {checksum_status}",
                )
        candidates = (selected.path,) if selected is not None else ()
    else:
        configured_tileset = Path(TILESET_PATH)
        candidates = (
            configured_tileset,
            configured_tileset.with_suffix(configured_tileset.suffix + ".previous"),
        )
    for candidate in candidates:
        try:
            candidate_metadata = read_metadata(candidate)
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
            continue
        if candidate_metadata.version == version:
            tileset_path = candidate
            metadata = candidate_metadata
            break
    if tileset_path is None or metadata is None:
        raise HTTPException(status_code=404, detail="Vector tileset is unavailable") from None
    if z < metadata.minzoom or z > metadata.maxzoom:
        raise HTTPException(status_code=404, detail="Tile zoom is outside the tileset range")
    dimension = 1 << z
    if x < 0 or x >= dimension or y < 0 or y >= dimension:
        raise HTTPException(status_code=404, detail="Tile coordinate is outside the zoom range")

    etag = f'"{metadata.version}-{z}-{x}-{y}"'
    cache_headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)
    try:
        tile_data = read_tile(tileset_path, z, x, y)
    except (OSError, sqlite3.Error):
        LOGGER.exception("Could not read vector tile version=%s z=%s x=%s y=%s", version, z, x, y)
        raise HTTPException(status_code=500, detail="Vector tile could not be read") from None
    if tile_data is None:
        return Response(status_code=204, headers=cache_headers)
    if not tile_data.startswith(b"\x1f\x8b"):
        LOGGER.error("Vector tile is not gzip-compressed version=%s z=%s x=%s y=%s", version, z, x, y)
        raise HTTPException(status_code=500, detail="Vector tile archive is invalid")
    return Response(
        content=tile_data,
        media_type=VECTOR_TILE_MEDIA_TYPE,
        headers={**cache_headers, "Content-Encoding": "gzip"},
    )


@router.get("/health")
def health() -> dict[str, object]:
    from .config import APP_ENV

    metadata: dict[str, object] = {}
    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        raise HTTPException(status_code=503, detail="Committed data release is invalid") from None
    if release is not None:
        metadata = release.manifest
        database_path = release.database_path
        tileset_path = release.tileset_path
        _require_verified_current_release(release)
        try:
            tile_metadata = read_metadata(tileset_path)
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Health check could not validate committed tileset metadata")
            raise HTTPException(status_code=503, detail="Committed tileset metadata is invalid") from None
        manifest_version = release.manifest.get("manifest_version")
        expected_tile_revision = 3 if manifest_version == 3 else 2
        if (
            tile_metadata.version != release.dataset_version
            or tile_metadata.tile_schema_revision != expected_tile_revision
        ):
            raise HTTPException(status_code=503, detail="Committed tileset release binding is invalid")
    else:
        database_path = Path(DATABASE_PATH)
        tileset_path = Path(TILESET_PATH)
        manifest = Path(DATA_MANIFEST_PATH)
        try:
            parsed = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                metadata = parsed
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOGGER.exception("Could not read legacy dataset manifest path=%s", DATA_MANIFEST_PATH)
    try:
        with closing(_read_only_database(database_path)) as connection:
            connection.execute("SELECT 1 FROM block_faces LIMIT 1").fetchone()
            count = metadata.get("block_faces")
            schedule_counts = metadata.get("schedule_counts")
            if not isinstance(count, int) or not isinstance(schedule_counts, dict):
                count = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
                schedule_counts = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type"
                    )
                }
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            schedule_state_counts: dict[str, dict[str, int]] = {}
            unresolved_counts: dict[str, int] = {}
            policy_conflicts = 0
            if "block_face_collection_states" in tables:
                for collection_type, state, state_count in connection.execute(
                    """SELECT collection_type, state, COUNT(*)
                       FROM block_face_collection_states
                       GROUP BY collection_type, state"""
                ):
                    schedule_state_counts.setdefault(str(collection_type), {})[str(state)] = int(state_count)
                policy_conflicts = int(connection.execute(
                    "SELECT COUNT(*) FROM block_face_collection_states WHERE source_policy_conflict = 1"
                ).fetchone()[0])
            if "unknown_block_faces" in tables:
                unresolved_counts = {
                    str(reason): int(reason_count)
                    for reason, reason_count in connection.execute(
                        "SELECT reason_code, COUNT(*) FROM unknown_block_faces GROUP BY reason_code"
                    )
                }
    except sqlite3.Error:
        LOGGER.exception("Health check could not inspect the local database")
        raise
    quality = metadata.get("ingestion_audit")
    quality_record = quality if isinstance(quality, dict) else {}
    return {
        "status": "ok",
        "environment": APP_ENV,
        "processed_records": count,
        "schedule_counts": schedule_counts,
        "data_updated": metadata.get("processed_at"),
        "data_manifest": metadata.get("manifest_version"),
        "dataset_version": metadata.get("dataset_version"),
        "data_quality": quality if isinstance(quality, dict) else None,
        "schedule_state_counts": schedule_state_counts,
        "source_versions": metadata.get("source_versions", {}),
        "recovery_counts": metadata.get("recovery_counts", {"identity_promoted": 0, "geometry_promoted": 0}),
        "unresolved_counts": unresolved_counts,
        "policy_conflicts": policy_conflicts,
        "policy_rule_version": quality_record.get("policy_rule_version"),
        "quality_status": quality_record.get("quality_status", "legacy_verified"),
        "map_available": tileset_path.is_file(),
        "artifact_integrity": "verified" if release is not None else "legacy-unverified",
    }


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
    collection_types = list(dict.fromkeys(value.strip().upper() for value in types.split(",") if value.strip()))
    if not collection_types or not set(collection_types).issubset(VALID_TYPES):
        raise HTTPException(status_code=422, detail=f"types must contain only {', '.join(VALID_TYPES)}")
    _validate_bounds(west, south, east, north)

    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        raise HTTPException(status_code=503, detail="Committed data release is invalid") from None
    if release is not None:
        _require_verified_current_release(release)
    database_path = release.database_path if release is not None else Path(DATABASE_PATH)
    parameters: list[object] = [*collection_types, day]
    try:
        with closing(_read_only_database(database_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(block_faces)")
            }
            origin_expression = (
                "COALESCE(NULLIF(TRIM(bf.origin_block_face_id), ''), bf.block_face_id)"
                if "origin_block_face_id" in columns
                else "bf.block_face_id"
            )
            query = """
                SELECT {origin} AS origin_block_face_id,
                       bf.block_face_id AS feature_id,
                       bf.street_name, bf.borough, bf.side, bf.geometry_wkt,
                       cs.collection_type, cs.source, cs.retrieved_at,
                       (SELECT GROUP_CONCAT(all_cs.weekday)
                        FROM collection_schedules all_cs
                        WHERE all_cs.block_face_id = cs.block_face_id
                          AND all_cs.collection_type = cs.collection_type) AS collection_days
                FROM collection_schedules cs
                JOIN block_faces bf ON bf.block_face_id = cs.block_face_id
                WHERE cs.collection_type IN ({types}) AND cs.weekday = ?
            """.format(
                origin=origin_expression,
                types=",".join("?" for _ in collection_types),
            )
            if west is not None:
                query += " AND bf.block_face_id IN (SELECT bm.block_face_id FROM block_face_rtree_map bm JOIN block_faces_rtree br ON br.rtree_id = bm.rtree_id WHERE br.max_x >= ? AND br.min_x <= ? AND br.max_y >= ? AND br.min_y <= ?)"
                parameters.extend([west, east, south, north])
            if west is not None and connection.execute("SELECT 1 FROM block_faces_rtree LIMIT 1").fetchone() is None:
                query = query.replace("bf.block_face_id IN (SELECT bm.block_face_id FROM block_face_rtree_map bm JOIN block_faces_rtree br ON br.rtree_id = bm.rtree_id WHERE br.max_x >= ? AND br.min_x <= ? AND br.max_y >= ? AND br.min_y <= ?)", "bf.max_x >= ? AND bf.min_x <= ? AND bf.max_y >= ? AND bf.min_y <= ?")
            query += " LIMIT ?"
            parameters.append(LEGACY_FEATURE_LIMIT + 1)
            rows = list(connection.execute(query, tuple(parameters)))
    except sqlite3.Error:
        LOGGER.exception("Map query failed day=%s bounds=%s", day, parameters[1:])
        raise HTTPException(status_code=500, detail="Map data query failed") from None
    if len(rows) > LEGACY_FEATURE_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Legacy GeoJSON response exceeds {LEGACY_FEATURE_LIMIT} features; "
                "request a smaller bounding box or use vector tiles"
            ),
        )

    features = []
    for row in rows:
        geometry = _parse_geometry(row["geometry_wkt"])
        if geometry is None:
            LOGGER.error("Invalid stored geometry feature_id=%s", row["feature_id"])
            raise HTTPException(status_code=500, detail="Stored map geometry is invalid")
        properties = {
            "block_face_id": row["origin_block_face_id"],
            "feature_id": row["feature_id"],
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
    line_match = re.fullmatch(r"LINESTRING\s*\(\s*([^()]*)\s*\)", wkt, re.IGNORECASE)
    if line_match:
        coordinates = _parse_coordinate_pairs(line_match.group(1))
        return {"type": "LineString", "coordinates": coordinates} if coordinates else None
    multi_match = re.fullmatch(
        r"MULTILINESTRING\s*\(\s*(\([^()]*\)(?:\s*,\s*\([^()]*\))*)\s*\)",
        wkt,
        re.IGNORECASE,
    )
    if multi_match:
        parts = re.findall(r"\(([^()]*)\)", multi_match.group(1))
        lines = [_parse_coordinate_pairs(part) for part in parts]
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
