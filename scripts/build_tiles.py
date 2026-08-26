"""Build an immutable MBTiles archive of DSNY collection block faces.

The web process does not import the vector-tile encoder.  This module is a
refresh-time data job: it validates the complete SQLite input, projects and
clips linework for each zoom, and atomically publishes a gzip-compressed PBF
archive.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Iterable

import mapbox_vector_tile
from pyproj import Transformer
from shapely import force_2d, wkt
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection, LineString, MultiLineString, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


LOGGER = logging.getLogger("build_tiles")
SOURCE_LAYER = "collection_streets"
UNKNOWN_SOURCE_LAYER = "collection_unknowns"
UNKNOWN_MIN_ZOOM = 14
TILE_SCHEMA_REVISION = 4
DEFAULT_MIN_ZOOM = 11
DEFAULT_MAX_ZOOM = 16
DEFAULT_BUFFER_PIXELS = 16.0
# The frontend's widest styled line is offset 9 px with a 5 px stroke.  Keep
# enough geometry outside each tile for that stroke (plus antialiasing/joins)
# to render across tile seams.
MIN_RENDER_BUFFER_PIXELS = 16.0
DEFAULT_MAX_COMPRESSED_TILE_BYTES = 1536 * 1024
DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES = 6 * 1024 * 1024
MVT_EXTENT = 4096
WEB_MERCATOR_HALF_WORLD = 20_037_508.342789244
WEB_MERCATOR_LATITUDE_LIMIT = 85.05112878
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT")
VALID_TYPES = ("REFUSE", "RECYCLING", "ORGANICS", "BULK")
TILE_PROGRESS_EVERY_SECONDS = 30.0
DAY_FIELDS = {
    "REFUSE": "refuse_days",
    "RECYCLING": "recycling_days",
    "ORGANICS": "organics_days",
    "BULK": "bulk_days",
}
SOURCE_ID_COLUMNS = ("origin_block_face_id",)
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass
class TileBlockFace:
    feature_id: str
    geometry: BaseGeometry
    properties: dict[str, str]
    mvt_id: int | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    block_faces: list[TileBlockFace]
    unknown_faces: list[TileBlockFace]
    bounds: tuple[float, float, float, float]
    content_digest: str
    data_updated: str | None
    source_block_face_count: int
    source_schedule_count: int
    source_schedule_group_count: int
    optional_source_id_fields: tuple[str, ...]
    schedule_state_counts: dict[str, dict[str, int]]


@dataclass(frozen=True)
class BuildReport:
    version: str
    tile_schema_revision: int
    source_database_sha256: str
    source_database_version: str
    source_block_face_count: int
    source_schedule_count: int
    source_schedule_group_count: int
    tile_count: int
    feature_count: int
    geometry_count: int
    maxzoom_feature_count: int
    maxzoom_nonrenderable_feature_count: int
    maxzoom_nonrenderable_feature_ids_sha256: str
    unknown_feature_count: int
    maxzoom_unknown_feature_count: int
    maxzoom_nonrenderable_unknown_feature_count: int
    maxzoom_nonrenderable_unknown_ids_sha256: str
    maxzoom_nonrenderable_features: list[dict[str, object]]
    maxzoom_nonrenderable_unknowns: list[dict[str, object]]
    tile_feature_count: int
    bounds: tuple[float, float, float, float]
    minzoom: int
    maxzoom: int
    data_updated: str | None
    simplify_pixels: float
    buffer_pixels: float
    tile_size_metrics: dict[str, object]
    tile_size_limits: dict[str, int]
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "tile_schema_revision": self.tile_schema_revision,
            "source_database_sha256": self.source_database_sha256,
            "source_database_version": self.source_database_version,
            "source_block_face_count": self.source_block_face_count,
            "source_schedule_count": self.source_schedule_count,
            "source_schedule_group_count": self.source_schedule_group_count,
            "tile_count": self.tile_count,
            "feature_count": self.feature_count,
            "geometry_count": self.geometry_count,
            "maxzoom_feature_count": self.maxzoom_feature_count,
            "maxzoom_nonrenderable_feature_count": self.maxzoom_nonrenderable_feature_count,
            "maxzoom_nonrenderable_feature_ids_sha256": self.maxzoom_nonrenderable_feature_ids_sha256,
            "unknown_feature_count": self.unknown_feature_count,
            "maxzoom_unknown_feature_count": self.maxzoom_unknown_feature_count,
            "maxzoom_nonrenderable_unknown_feature_count": self.maxzoom_nonrenderable_unknown_feature_count,
            "maxzoom_nonrenderable_unknown_ids_sha256": self.maxzoom_nonrenderable_unknown_ids_sha256,
            "maxzoom_nonrenderable_features": self.maxzoom_nonrenderable_features,
            "maxzoom_nonrenderable_unknowns": self.maxzoom_nonrenderable_unknowns,
            "tile_feature_count": self.tile_feature_count,
            "bounds": list(self.bounds),
            "minzoom": self.minzoom,
            "maxzoom": self.maxzoom,
            "data_updated": self.data_updated,
            "simplify_pixels": self.simplify_pixels,
            "buffer_pixels": self.buffer_pixels,
            "tile_size_metrics": self.tile_size_metrics,
            "tile_size_limits": self.tile_size_limits,
            "sha256": self.sha256,
        }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _validate_source(connection: sqlite3.Connection) -> tuple[int, int, int]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"source database integrity check failed: {integrity}")
    block_faces = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
    schedules = connection.execute("SELECT COUNT(*) FROM collection_schedules").fetchone()[0]
    if block_faces <= 0 or schedules <= 0:
        raise RuntimeError("source database must contain block faces and collection schedules")
    orphaned = connection.execute(
        """SELECT COUNT(*) FROM collection_schedules cs
           LEFT JOIN block_faces bf ON bf.block_face_id = cs.block_face_id
           WHERE bf.block_face_id IS NULL"""
    ).fetchone()[0]
    unscheduled = connection.execute(
        """SELECT COUNT(*) FROM block_faces bf
           LEFT JOIN collection_schedules cs ON cs.block_face_id = bf.block_face_id
           WHERE cs.block_face_id IS NULL"""
    ).fetchone()[0]
    if orphaned or unscheduled:
        raise RuntimeError(
            f"source database coverage is incomplete: orphaned_schedules={orphaned} "
            f"unscheduled_block_faces={unscheduled}"
        )
    schedule_groups = connection.execute(
        """SELECT COUNT(*) FROM (
               SELECT block_face_id, collection_type
               FROM collection_schedules
               GROUP BY block_face_id, collection_type
           )"""
    ).fetchone()[0]
    state_rows = connection.execute("SELECT COUNT(*) FROM block_face_collection_states").fetchone()[0]
    if state_rows != block_faces * len(VALID_TYPES):
        raise RuntimeError(
            "source database must contain exactly four collection states per block face: "
            f"expected={block_faces * len(VALID_TYPES)} actual={state_rows}"
        )
    invalid_state_faces = connection.execute(
        """SELECT COUNT(*) FROM (
               SELECT block_face_id, COUNT(*) AS count
               FROM block_face_collection_states
               GROUP BY block_face_id
               HAVING count != 4
           )"""
    ).fetchone()[0]
    if invalid_state_faces:
        raise RuntimeError(f"source database has block faces with incomplete state rows: {invalid_state_faces}")
    return block_faces, schedules, schedule_groups


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _source_database_version(
    connection: sqlite3.Connection,
    explicit_version: str | None,
    database_sha256: str,
) -> str:
    if explicit_version:
        return explicit_version
    del database_sha256
    metadata = dict(connection.execute("SELECT key, value FROM dataset_metadata"))
    value = str(metadata.get("dataset_version", "")).strip()
    if not value:
        raise RuntimeError("source database is missing dataset_metadata.dataset_version")
    return value


def _load_features(
    connection: sqlite3.Connection,
    minzoom: int,
    maxzoom: int,
    simplify_pixels: float,
    buffer_pixels: float,
    source_database_sha256: str,
    source_database_version: str,
) -> SourceSnapshot:
    source_block_face_count, source_schedule_count, source_schedule_group_count = _validate_source(connection)
    block_face_columns = _table_columns(connection, "block_faces")
    missing_source_id_fields = set(SOURCE_ID_COLUMNS) - block_face_columns
    if missing_source_id_fields:
        raise RuntimeError(f"source database is missing source ID columns: {sorted(missing_source_id_fields)}")
    optional_source_id_fields = SOURCE_ID_COLUMNS
    encoder_version = importlib.metadata.version("mapbox-vector-tile")
    content_hash = hashlib.sha256()
    content_hash.update(
        json.dumps(
            {
                "tile_schema_revision": TILE_SCHEMA_REVISION,
                "encoder": encoder_version,
                "minzoom": minzoom,
                "maxzoom": maxzoom,
                "unknown_minzoom": min(maxzoom, max(minzoom, UNKNOWN_MIN_ZOOM)),
                "extent": MVT_EXTENT,
                "simplify_pixels": simplify_pixels,
                "buffer_pixels": buffer_pixels,
                "source_database_sha256": source_database_sha256,
                "source_database_version": source_database_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    projector = Transformer.from_crs(4326, 3857, always_xy=True)
    day_order = {day: position for position, day in enumerate(VALID_DAYS)}
    block_faces_by_id: dict[str, TileBlockFace] = {}
    west = south = math.inf
    east = north = -math.inf

    optional_select = "".join(f", bf.{field}" for field in optional_source_id_fields)
    face_rows = connection.execute(
        f"""SELECT bf.block_face_id, bf.street_name, bf.borough, bf.side,
                   bf.geometry_wkt{optional_select}
            FROM block_faces bf
            ORDER BY bf.block_face_id"""
    )
    for row in face_rows:
        street_name = str(row["street_name"]).strip()
        block_face_id = str(row["block_face_id"]).strip()
        properties = {
            "id": block_face_id,
            "street_name": street_name,
            "borough": str(row["borough"]).strip(),
            "side": str(row["side"]).strip().upper(),
        }
        for field in optional_source_id_fields:
            value = "" if row[field] is None else str(row[field]).strip()
            if not value:
                raise RuntimeError(
                    f"block face {properties['id']!r} has blank source ID column {field}"
                )
            properties[field] = value
        blank = [key for key, value in properties.items() if not value]
        if blank:
            raise RuntimeError(f"tile feature has blank properties id={properties['id']!r}: {blank}")
        if properties["side"] not in {"LEFT", "RIGHT"}:
            raise RuntimeError(
                f"tile feature has invalid side id={properties['id']}: {properties['side']!r}"
            )
        if properties["id"] in block_faces_by_id:
            raise RuntimeError(f"duplicate block face ID in source database: {properties['id']}")
        try:
            geometry_wgs84 = force_2d(wkt.loads(row["geometry_wkt"]))
        except (ShapelyError, TypeError, ValueError) as error:
            raise RuntimeError(f"tile feature has invalid WKT id={properties['id']}") from error
        if (
            geometry_wgs84.is_empty
            or not geometry_wgs84.is_valid
            or geometry_wgs84.length <= 0
            or geometry_wgs84.geom_type not in {"LineString", "MultiLineString"}
        ):
            raise RuntimeError(
                f"tile feature must have valid non-empty line geometry id={properties['id']}: "
                f"{geometry_wgs84.geom_type}"
            )
        min_x, min_y, max_x, max_y = geometry_wgs84.bounds
        if (
            min_x < -180
            or max_x > 180
            or min_y < -WEB_MERCATOR_LATITUDE_LIMIT
            or max_y > WEB_MERCATOR_LATITUDE_LIMIT
        ):
            raise RuntimeError(f"tile feature is outside Web Mercator bounds id={properties['id']}")
        projected = transform(projector.transform, geometry_wgs84)
        if projected.is_empty or not all(math.isfinite(value) for value in projected.bounds):
            raise RuntimeError(f"tile feature projection failed id={properties['id']}")
        block_faces_by_id[properties["id"]] = TileBlockFace(
            feature_id=block_face_id,
            geometry=projected,
            properties=properties,
        )
        west, south = min(west, min_x), min(south, min_y)
        east, north = max(east, max_x), max(north, max_y)
        content_hash.update(geometry_wgs84.wkb)

    actual_schedule_count = 0
    finalized_schedule_faces = 0
    current_block_face_id: str | None = None
    current_days = {kind: set() for kind in VALID_TYPES}
    current_sources: set[str] = set()
    current_retrieval_times: set[str] = set()

    def finalize_current_schedule() -> None:
        nonlocal finalized_schedule_faces
        if current_block_face_id is None:
            return
        block_face = block_faces_by_id[current_block_face_id]
        if len(current_sources) != 1 or len(current_retrieval_times) != 1:
            raise RuntimeError(
                f"tile feature has conflicting provenance id={current_block_face_id}: "
                f"sources={len(current_sources)} retrieval_times={len(current_retrieval_times)}"
            )
        for collection_type, field in DAY_FIELDS.items():
            block_face.properties[field] = ",".join(
                sorted(current_days[collection_type], key=day_order.__getitem__)
            )
        block_face.properties["source"] = next(iter(current_sources))
        block_face.properties["retrieved_at"] = next(iter(current_retrieval_times))
        content_hash.update(
            json.dumps(block_face.properties, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        finalized_schedule_faces += 1

    schedule_rows = connection.execute(
        """SELECT block_face_id, collection_type, weekday, source, retrieved_at
           FROM collection_schedules
           ORDER BY block_face_id, collection_type, weekday"""
    )
    for row in schedule_rows:
        actual_schedule_count += 1
        block_face_id = str(row["block_face_id"]).strip()
        if block_face_id not in block_faces_by_id:
            raise RuntimeError(f"schedule references missing block face: {block_face_id!r}")
        if block_face_id != current_block_face_id:
            finalize_current_schedule()
            current_block_face_id = block_face_id
            current_days = {kind: set() for kind in VALID_TYPES}
            current_sources = set()
            current_retrieval_times = set()
        collection_type = str(row["collection_type"]).strip().upper()
        weekday = str(row["weekday"]).strip().upper()
        source = str(row["source"]).strip()
        retrieved_at = str(row["retrieved_at"]).strip()
        if collection_type not in VALID_TYPES:
            raise RuntimeError(
                f"tile feature has unsupported collection type id={block_face_id}: {collection_type!r}"
            )
        if weekday not in VALID_DAYS:
            raise RuntimeError(f"tile feature has invalid schedule id={block_face_id}: {weekday!r}")
        if not source or not retrieved_at:
            raise RuntimeError(f"tile feature has blank provenance id={block_face_id}")
        if weekday in current_days[collection_type]:
            raise RuntimeError(
                f"tile feature has duplicate schedule id={block_face_id}: {collection_type}/{weekday}"
            )
        current_days[collection_type].add(weekday)
        current_sources.add(source)
        current_retrieval_times.add(retrieved_at)

    finalize_current_schedule()

    if actual_schedule_count != source_schedule_count:
        raise RuntimeError(
            "schedule query lost rows: "
            f"expected={source_schedule_count} actual={actual_schedule_count}"
        )

    state_counts: dict[str, dict[str, int]] = {kind: {} for kind in VALID_TYPES}
    state_rows = connection.execute(
        """SELECT block_face_id, collection_type, effective_days_json, state, rule_id,
                  source_policy_conflict
           FROM block_face_collection_states
           ORDER BY block_face_id, collection_type"""
    )
    actual_state_rows = 0
    for row in state_rows:
        actual_state_rows += 1
        block_face_id = str(row["block_face_id"])
        collection_type = str(row["collection_type"])
        state = str(row["state"])
        if block_face_id not in block_faces_by_id or collection_type not in VALID_TYPES:
            raise RuntimeError("collection state references an invalid face or collection type")
        try:
            effective_days = json.loads(str(row["effective_days_json"]))
        except json.JSONDecodeError as error:
            raise RuntimeError("collection state has invalid effective_days_json") from error
        expected_days = block_faces_by_id[block_face_id].properties[DAY_FIELDS[collection_type]]
        if not isinstance(effective_days, list) or ",".join(effective_days) != expected_days:
            raise RuntimeError(f"collection state does not match effective schedule id={block_face_id}")
        if state == "UNKNOWN_SOURCE_BLANK" and effective_days:
            raise RuntimeError(f"unknown collection state has effective days id={block_face_id}")
        if state == "POLICY_DERIVED" and row["rule_id"] != "dsny-organics-on-recycling-day-v1":
            raise RuntimeError(f"derived collection state has an invalid rule id={block_face_id}")
        prefix = collection_type.lower()
        properties = block_faces_by_id[block_face_id].properties
        properties[f"{prefix}_status"] = state
        properties[f"{prefix}_conflict"] = "1" if int(row["source_policy_conflict"]) else "0"
        state_counts[collection_type][state] = state_counts[collection_type].get(state, 0) + 1
    if actual_state_rows != source_block_face_count * len(VALID_TYPES):
        raise RuntimeError("collection state query lost rows")
    for block_face in block_faces_by_id.values():
        content_hash.update(
            json.dumps(block_face.properties, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    block_faces = list(block_faces_by_id.values())
    if len(block_faces) != source_block_face_count or finalized_schedule_faces != source_block_face_count:
        raise RuntimeError(
            "tile feature build lost records: "
            f"expected_block_faces={source_block_face_count} actual_block_faces={len(block_faces)} "
            f"scheduled_block_faces={finalized_schedule_faces}"
        )
    data_updated_row = connection.execute("SELECT MAX(retrieved_at) FROM collection_schedules").fetchone()
    data_updated = str(data_updated_row[0]) if data_updated_row and data_updated_row[0] else None
    unknown_faces: list[TileBlockFace] = []
    unknown_ids: set[str] = set()
    unknown_mvt_ids: set[int] = set()
    unknown_rows = connection.execute(
        """SELECT unknown_id, street_name, side, reason_code, reason, geometry_wkt
           FROM unknown_block_faces ORDER BY unknown_id COLLATE BINARY"""
    )
    for mvt_id, row in enumerate(unknown_rows, start=1):
        unknown_id = str(row["unknown_id"])
        if unknown_id in unknown_ids:
            raise RuntimeError(f"duplicate unknown feature id={unknown_id}")
        if mvt_id in unknown_mvt_ids:
            raise RuntimeError(f"duplicate unknown MVT feature id={mvt_id}")
        unknown_ids.add(unknown_id)
        unknown_mvt_ids.add(mvt_id)
        geometry_wgs84 = force_2d(wkt.loads(row["geometry_wkt"]))
        if geometry_wgs84.is_empty or geometry_wgs84.geom_type not in {"LineString", "MultiLineString"}:
            raise RuntimeError(f"unknown feature has invalid geometry id={unknown_id}")
        projected = transform(projector.transform, geometry_wgs84)
        properties = {
            "street_name": str(row["street_name"]),
            "side": str(row["side"]),
            "reason_code": str(row["reason_code"]),
            "reason": str(row["reason"]),
        }
        if any(key.endswith("_days") or key.endswith("_status") for key in properties):
            raise RuntimeError("unknown tile feature contains schedule properties")
        unknown_faces.append(
            TileBlockFace(
                feature_id=unknown_id,
                geometry=projected,
                properties=properties,
                mvt_id=mvt_id,
            )
        )
        min_x, min_y, max_x, max_y = geometry_wgs84.bounds
        west, south = min(west, min_x), min(south, min_y)
        east, north = max(east, max_x), max(north, max_y)
        content_hash.update(geometry_wgs84.wkb)
        content_hash.update(json.dumps(properties, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if unknown_mvt_ids != set(range(1, len(unknown_faces) + 1)):
        raise RuntimeError("unknown MVT feature IDs must be compact, unique, and 1-based")
    return SourceSnapshot(
        block_faces=block_faces,
        unknown_faces=unknown_faces,
        bounds=(west, south, east, north),
        content_digest=content_hash.hexdigest(),
        data_updated=data_updated,
        source_block_face_count=source_block_face_count,
        source_schedule_count=source_schedule_count,
        source_schedule_group_count=source_schedule_group_count,
        optional_source_id_fields=optional_source_id_fields,
        schedule_state_counts=state_counts,
    )


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    span = (2 * WEB_MERCATOR_HALF_WORLD) / (1 << z)
    min_x = -WEB_MERCATOR_HALF_WORLD + x * span
    max_y = WEB_MERCATOR_HALF_WORLD - y * span
    return min_x, max_y - span, min_x + span, max_y


def _tile_range(bounds: tuple[float, float, float, float], z: int, padding: float) -> tuple[range, range]:
    min_x, min_y, max_x, max_y = bounds
    dimension = 1 << z
    span = (2 * WEB_MERCATOR_HALF_WORLD) / dimension
    first_x = math.floor((min_x - padding + WEB_MERCATOR_HALF_WORLD) / span)
    last_x = math.floor((max_x + padding + WEB_MERCATOR_HALF_WORLD) / span)
    first_y = math.floor((WEB_MERCATOR_HALF_WORLD - max_y - padding) / span)
    last_y = math.floor((WEB_MERCATOR_HALF_WORLD - min_y + padding) / span)
    first_x, last_x = max(0, first_x), min(dimension - 1, last_x)
    first_y, last_y = max(0, first_y), min(dimension - 1, last_y)
    return range(first_x, last_x + 1), range(first_y, last_y + 1)


def _linear_parts(geometry: BaseGeometry) -> BaseGeometry | None:
    if geometry.is_empty:
        return None
    if isinstance(geometry, LineString):
        return geometry if len(geometry.coords) >= 2 else None
    if isinstance(geometry, MultiLineString):
        lines = [line for line in geometry.geoms if len(line.coords) >= 2]
    elif isinstance(geometry, GeometryCollection):
        lines = []
        for part in geometry.geoms:
            linear = _linear_parts(part)
            if isinstance(linear, LineString):
                lines.append(linear)
            elif isinstance(linear, MultiLineString):
                lines.extend(linear.geoms)
    else:
        return None
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def _create_archive(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        CREATE TABLE metadata (name TEXT NOT NULL, value TEXT NOT NULL);
        CREATE UNIQUE INDEX metadata_name ON metadata(name);
        CREATE TABLE tiles (
            zoom_level INTEGER NOT NULL,
            tile_column INTEGER NOT NULL,
            tile_row INTEGER NOT NULL,
            tile_data BLOB NOT NULL
        );
        CREATE UNIQUE INDEX tile_index
            ON tiles (zoom_level, tile_column, tile_row);
        """
    )


def _metadata_rows(
    *,
    version: str,
    source_database_sha256: str,
    source_database_version: str,
    source_block_face_count: int,
    source_schedule_count: int,
    source_schedule_group_count: int,
    feature_count: int,
    unknown_feature_count: int,
    maxzoom_feature_count: int,
    maxzoom_nonrenderable_feature_count: int,
    maxzoom_nonrenderable_feature_ids_sha256: str,
    maxzoom_unknown_feature_count: int,
    maxzoom_nonrenderable_unknown_feature_count: int,
    maxzoom_nonrenderable_unknown_ids_sha256: str,
    bounds: tuple[float, float, float, float],
    minzoom: int,
    maxzoom: int,
    data_updated: str | None,
    simplify_pixels: float,
    buffer_pixels: float,
    optional_source_id_fields: tuple[str, ...],
    tile_size_metrics: dict[str, object],
    tile_size_limits: dict[str, int],
) -> Iterable[tuple[str, str]]:
    west, south, east, north = bounds
    vector_layers = {
        "vector_layers": [
            {
                "id": SOURCE_LAYER,
                "description": "NYC sanitation collection schedules, one feature per stored block face",
                "minzoom": minzoom,
                "maxzoom": maxzoom,
                "fields": {
                    "id": "String",
                    "street_name": "String",
                    "borough": "String",
                    "side": "String",
                    "refuse_days": "String",
                    "recycling_days": "String",
                    "organics_days": "String",
                    "bulk_days": "String",
                    "source": "String",
                    "retrieved_at": "String",
                    "refuse_status": "String",
                    "recycling_status": "String",
                    "organics_status": "String",
                    "bulk_status": "String",
                    "refuse_conflict": "String",
                    "recycling_conflict": "String",
                    "organics_conflict": "String",
                    "bulk_conflict": "String",
                    **{field: "String" for field in optional_source_id_fields},
                },
            },
            {
                "id": UNKNOWN_SOURCE_LAYER,
                "description": "Unresolved block-face evidence; contains no collection weekdays",
                "minzoom": min(maxzoom, max(minzoom, UNKNOWN_MIN_ZOOM)),
                "maxzoom": maxzoom,
                "fields": {
                    "street_name": "String",
                    "side": "String",
                    "reason_code": "String",
                    "reason": "String",
                },
            },
        ]
    }
    rows = [
        ("name", "NYC Sanitation Collection Streets"),
        ("description", "Pre-generated NYC sanitation collection block-face vector tiles"),
        ("type", "overlay"),
        ("format", "pbf"),
        ("version", version),
        ("source_database_sha256", source_database_sha256),
        ("source_database_version", source_database_version),
        ("source_block_face_count", str(source_block_face_count)),
        ("source_schedule_count", str(source_schedule_count)),
        ("source_schedule_group_count", str(source_schedule_group_count)),
        ("feature_count", str(feature_count)),
        ("unknown_feature_count", str(unknown_feature_count)),
        ("maxzoom_unknown_feature_count", str(maxzoom_unknown_feature_count)),
        ("maxzoom_nonrenderable_unknown_feature_count", str(maxzoom_nonrenderable_unknown_feature_count)),
        ("maxzoom_nonrenderable_unknown_ids_sha256", maxzoom_nonrenderable_unknown_ids_sha256),
        ("geometry_count", str(feature_count)),
        ("maxzoom_feature_count", str(maxzoom_feature_count)),
        ("maxzoom_nonrenderable_feature_count", str(maxzoom_nonrenderable_feature_count)),
        ("maxzoom_nonrenderable_feature_ids_sha256", maxzoom_nonrenderable_feature_ids_sha256),
        ("source_layer", SOURCE_LAYER),
        ("unknown_source_layer", UNKNOWN_SOURCE_LAYER),
        ("unknown_minzoom", str(min(maxzoom, max(minzoom, UNKNOWN_MIN_ZOOM)))),
        ("minzoom", str(minzoom)),
        ("maxzoom", str(maxzoom)),
        ("simplify_pixels", f"{simplify_pixels:.12g}"),
        ("buffer_pixels", f"{buffer_pixels:.12g}"),
        ("bounds", ",".join(f"{value:.7f}" for value in bounds)),
        ("center", f"{(west + east) / 2:.7f},{(south + north) / 2:.7f},{minzoom}"),
        ("json", json.dumps(vector_layers, sort_keys=True, separators=(",", ":"))),
        ("compression", "gzip"),
        ("tile_schema_revision", str(TILE_SCHEMA_REVISION)),
        ("tile_size_metrics", json.dumps(tile_size_metrics, sort_keys=True, separators=(",", ":"))),
        ("tile_size_limits", json.dumps(tile_size_limits, sort_keys=True, separators=(",", ":"))),
    ]
    if data_updated:
        rows.append(("data_updated", data_updated))
    return rows


def _nearest_rank_percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _tile_size_metrics(
    sizes_by_zoom: dict[int, list[tuple[int, int]]],
    minzoom: int,
) -> dict[str, object]:
    compressed_sizes = [compressed for sizes in sizes_by_zoom.values() for compressed, _ in sizes]
    uncompressed_sizes = [uncompressed for sizes in sizes_by_zoom.values() for _, uncompressed in sizes]
    by_zoom: dict[str, dict[str, int]] = {}
    for zoom in sorted(sizes_by_zoom):
        sizes = sizes_by_zoom[zoom]
        zoom_compressed = [compressed for compressed, _ in sizes]
        zoom_uncompressed = [uncompressed for _, uncompressed in sizes]
        by_zoom[str(zoom)] = {
            "tile_count": len(sizes),
            "compressed_bytes": sum(zoom_compressed),
            "uncompressed_bytes": sum(zoom_uncompressed),
            "max_compressed_tile_bytes": max(zoom_compressed, default=0),
            "p95_compressed_tile_bytes": _nearest_rank_percentile(zoom_compressed, 0.95),
            "max_uncompressed_tile_bytes": max(zoom_uncompressed, default=0),
            "p95_uncompressed_tile_bytes": _nearest_rank_percentile(zoom_uncompressed, 0.95),
        }
    initial_sizes = sizes_by_zoom.get(minzoom, [])
    return {
        "max_compressed_tile_bytes": max(compressed_sizes, default=0),
        "p95_compressed_tile_bytes": _nearest_rank_percentile(compressed_sizes, 0.95),
        "total_compressed_tile_bytes": sum(compressed_sizes),
        "max_uncompressed_tile_bytes": max(uncompressed_sizes, default=0),
        "p95_uncompressed_tile_bytes": _nearest_rank_percentile(uncompressed_sizes, 0.95),
        "total_uncompressed_tile_bytes": sum(uncompressed_sizes),
        "initial_zoom": minzoom,
        "initial_zoom_tile_count": len(initial_sizes),
        "initial_zoom_compressed_bytes": sum(compressed for compressed, _ in initial_sizes),
        "initial_zoom_uncompressed_bytes": sum(uncompressed for _, uncompressed in initial_sizes),
        "by_zoom": by_zoom,
    }


def _survives_quantization(
    geometry: BaseGeometry,
    tile_bounds: tuple[float, float, float, float],
) -> bool:
    min_x, min_y, max_x, max_y = tile_bounds

    def quantized(coordinate: tuple[float, ...]) -> tuple[int, int]:
        x, y = coordinate[:2]
        return (
            round(MVT_EXTENT * (x - min_x) / (max_x - min_x)),
            round(MVT_EXTENT * (y - min_y) / (max_y - min_y)),
        )

    lines = geometry.geoms if isinstance(geometry, MultiLineString) else (geometry,)
    for line in lines:
        coordinates = iter(line.coords)
        first = quantized(next(coordinates))
        if any(quantized(coordinate) != first for coordinate in coordinates):
            return True
    return False


def _tile_geometry_with_source_fallback(
    simplified: BaseGeometry,
    source: BaseGeometry,
    clipping_box: BaseGeometry,
    tile_bounds: tuple[float, float, float, float],
) -> tuple[BaseGeometry | None, bool]:
    """Return encodable linework, retrying exact source geometry when needed."""

    clipped = _linear_parts(simplified.intersection(clipping_box))
    if clipped is not None and _survives_quantization(clipped, tile_bounds):
        return clipped, False
    if source is simplified:
        return None, False
    source_clipped = _linear_parts(source.intersection(clipping_box))
    if source_clipped is not None and _survives_quantization(source_clipped, tile_bounds):
        return source_clipped, True
    return None, False


def _id_set_sha256(feature_ids: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for feature_id in sorted(feature_ids):
        digest.update(feature_id.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _nonrenderable_feature_records(
    features: list[TileBlockFace],
    indexes: set[int],
    *,
    unknown: bool,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for index in sorted(indexes, key=lambda item: features[item].feature_id):
        feature = features[index]
        record: dict[str, object] = {
            "id": feature.feature_id,
            "street_name": feature.properties["street_name"],
            "side": feature.properties["side"],
            "projected_length_meters": round(float(feature.geometry.length), 6),
            "reason": "COLLAPSES_AFTER_MVT_QUANTIZATION",
        }
        if unknown:
            record["reason_code"] = feature.properties["reason_code"]
        records.append(record)
    return records


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_tiles(
    database_path: Path,
    output_path: Path,
    *,
    minzoom: int = DEFAULT_MIN_ZOOM,
    maxzoom: int = DEFAULT_MAX_ZOOM,
    version: str | None = None,
    source_version: str | None = None,
    data_updated: str | None = None,
    simplify_pixels: float = 0.5,
    buffer_pixels: float = DEFAULT_BUFFER_PIXELS,
    max_compressed_tile_bytes: int = DEFAULT_MAX_COMPRESSED_TILE_BYTES,
    max_uncompressed_tile_bytes: int = DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES,
) -> BuildReport:
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    simplify_pixels = float(simplify_pixels)
    buffer_pixels = float(buffer_pixels)
    if not 0 <= minzoom <= maxzoom <= 22:
        raise ValueError("zoom range must satisfy 0 <= minzoom <= maxzoom <= 22")
    if simplify_pixels < 0:
        raise ValueError("simplify pixel value must be non-negative")
    if buffer_pixels < MIN_RENDER_BUFFER_PIXELS:
        raise ValueError(
            f"tile buffer must be at least {MIN_RENDER_BUFFER_PIXELS:g} pixels "
            "for the frontend's styled line offsets"
        )
    if max_compressed_tile_bytes <= 0 or max_uncompressed_tile_bytes <= 0:
        raise ValueError("tile byte limits must be positive")
    if (
        max_compressed_tile_bytes > DEFAULT_MAX_COMPRESSED_TILE_BYTES
        or max_uncompressed_tile_bytes > DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES
    ):
        raise ValueError(
            "tile byte limits may be lowered but cannot exceed the hard "
            f"release ceilings ({DEFAULT_MAX_COMPRESSED_TILE_BYTES} compressed, "
            f"{DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES} uncompressed)"
        )
    if version is not None and not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must be 1-128 URL-safe letters, numbers, dots, dashes, or underscores")
    if source_version is not None and not VERSION_PATTERN.fullmatch(source_version):
        raise ValueError("source version must be 1-128 URL-safe letters, numbers, dots, dashes, or underscores")

    LOGGER.info("Loading SQLite snapshot for vector tiles database=%s", database_path)
    snapshot_started = monotonic()
    source_database_sha256 = _file_sha256(database_path)
    with closing(_readonly_connection(database_path)) as source:
        source.execute("BEGIN")
        resolved_source_version = _source_database_version(
            source,
            source_version,
            source_database_sha256,
        )
        snapshot = _load_features(
            source,
            minzoom,
            maxzoom,
            simplify_pixels,
            buffer_pixels,
            source_database_sha256,
            resolved_source_version,
        )
    if _file_sha256(database_path) != source_database_sha256:
        raise RuntimeError("source database changed while the tile snapshot was being read")
    version = version or snapshot.content_digest[:20]
    data_updated = data_updated or snapshot.data_updated
    block_faces = snapshot.block_faces
    unknown_faces = snapshot.unknown_faces
    if unknown_faces and maxzoom < UNKNOWN_MIN_ZOOM:
        raise ValueError(f"maxzoom must be at least {UNKNOWN_MIN_ZOOM} when unknown features exist")
    feature_count = len(block_faces)
    bounds = snapshot.bounds
    tile_size_limits = {
        "max_compressed_tile_bytes": max_compressed_tile_bytes,
        "max_uncompressed_tile_bytes": max_uncompressed_tile_bytes,
    }
    LOGGER.info(
        "Loaded tile source features=%s schedules=%s zooms=%s-%s elapsed_s=%.1f",
        feature_count,
        snapshot.source_schedule_count,
        minzoom,
        maxzoom,
        monotonic() - snapshot_started,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    tile_count = 0
    tile_feature_count = 0
    represented_at_maxzoom: set[int] = set()
    represented_unknown_at_maxzoom: set[int] = set()
    source_fallback_at_maxzoom: set[int] = set()
    source_unknown_fallback_at_maxzoom: set[int] = set()
    nonrenderable_at_maxzoom: set[int] = set()
    nonrenderable_unknown_at_maxzoom: set[int] = set()
    maxzoom_nonrenderable_features: list[dict[str, object]] = []
    maxzoom_nonrenderable_unknowns: list[dict[str, object]] = []
    sizes_by_zoom: dict[int, list[tuple[int, int]]] = defaultdict(list)
    try:
        with closing(sqlite3.connect(temporary_path)) as archive:
            _create_archive(archive)
            for zoom in range(minzoom, maxzoom + 1):
                zoom_started = monotonic()
                span = (2 * WEB_MERCATOR_HALF_WORLD) / (1 << zoom)
                simplify_tolerance = span * simplify_pixels / 512
                buffer_distance = span * buffer_pixels / 512
                simplified = [
                    block_face.geometry.simplify(simplify_tolerance, preserve_topology=True)
                    if simplify_tolerance
                    else block_face.geometry
                    for block_face in block_faces
                ]
                simplified_unknowns = [
                    unknown.geometry.simplify(simplify_tolerance, preserve_topology=True)
                    if simplify_tolerance
                    else unknown.geometry
                    for unknown in unknown_faces
                ] if zoom >= UNKNOWN_MIN_ZOOM else []
                tile_members: dict[tuple[int, int], list[int]] = defaultdict(list)
                membership_geometries = (
                    [feature.geometry for feature in block_faces]
                    if zoom == maxzoom else simplified
                )
                for index, geometry in enumerate(membership_geometries):
                    x_values, y_values = _tile_range(geometry.bounds, zoom, buffer_distance)
                    for x in x_values:
                        for y in y_values:
                            tile_members[(x, y)].append(index)
                unknown_tile_members: dict[tuple[int, int], list[int]] = defaultdict(list)
                unknown_membership_geometries = (
                    [feature.geometry for feature in unknown_faces]
                    if zoom == maxzoom else simplified_unknowns
                )
                for index, geometry in enumerate(unknown_membership_geometries):
                    x_values, y_values = _tile_range(geometry.bounds, zoom, buffer_distance)
                    for x in x_values:
                        for y in y_values:
                            unknown_tile_members[(x, y)].append(index)

                LOGGER.info(
                    "Encoding vector tiles zoom=%s candidate_tiles=%s source_features=%s",
                    zoom,
                    len(set(tile_members) | set(unknown_tile_members)),
                    feature_count,
                )
                zoom_tiles = 0
                last_progress_at = monotonic()
                candidate_tiles = sorted(set(tile_members) | set(unknown_tile_members))
                for candidate_number, (x, y) in enumerate(candidate_tiles, start=1):
                    indexes = tile_members.get((x, y), [])
                    min_x, min_y, max_x, max_y = _tile_bounds(zoom, x, y)
                    clip_bounds = (
                        min_x - buffer_distance,
                        min_y - buffer_distance,
                        max_x + buffer_distance,
                        max_y + buffer_distance,
                    )
                    clipping_box = box(*clip_bounds)
                    encoded_features = []
                    for index in indexes:
                        clipped, used_source_fallback = _tile_geometry_with_source_fallback(
                            simplified[index],
                            block_faces[index].geometry if zoom == maxzoom else simplified[index],
                            clipping_box,
                            (min_x, min_y, max_x, max_y),
                        )
                        if clipped is None:
                            continue
                        if zoom == maxzoom:
                            represented_at_maxzoom.add(index)
                            if used_source_fallback:
                                source_fallback_at_maxzoom.add(index)
                        encoded_features.append(
                            {"geometry": clipped, "properties": block_faces[index].properties}
                        )
                    encoded_unknowns = []
                    for index in unknown_tile_members.get((x, y), []):
                        clipped, used_source_fallback = _tile_geometry_with_source_fallback(
                            simplified_unknowns[index],
                            unknown_faces[index].geometry if zoom == maxzoom else simplified_unknowns[index],
                            clipping_box,
                            (min_x, min_y, max_x, max_y),
                        )
                        if clipped is None:
                            continue
                        if zoom == maxzoom:
                            represented_unknown_at_maxzoom.add(index)
                            if used_source_fallback:
                                source_unknown_fallback_at_maxzoom.add(index)
                        encoded_unknowns.append(
                            {
                                "id": unknown_faces[index].mvt_id,
                                "geometry": clipped,
                                "properties": unknown_faces[index].properties,
                            }
                        )
                    if not encoded_features and not encoded_unknowns:
                        continue
                    layers = []
                    if encoded_features:
                        layers.append({"name": SOURCE_LAYER, "features": encoded_features})
                    if encoded_unknowns:
                        layers.append({"name": UNKNOWN_SOURCE_LAYER, "features": encoded_unknowns})
                    tile = mapbox_vector_tile.encode(
                        layers,
                        default_options={
                            "quantize_bounds": (min_x, min_y, max_x, max_y),
                            "extents": MVT_EXTENT,
                            "y_coord_down": False,
                        },
                    )
                    uncompressed_size = len(tile)
                    if uncompressed_size > max_uncompressed_tile_bytes:
                        raise RuntimeError(
                            "uncompressed vector tile exceeds build gate "
                            f"z={zoom} x={x} y={y} bytes={uncompressed_size} "
                            f"limit={max_uncompressed_tile_bytes}"
                        )
                    compressed = gzip.compress(tile, compresslevel=9, mtime=0)
                    compressed_size = len(compressed)
                    if compressed_size > max_compressed_tile_bytes:
                        raise RuntimeError(
                            "compressed vector tile exceeds build gate "
                            f"z={zoom} x={x} y={y} bytes={compressed_size} "
                            f"limit={max_compressed_tile_bytes}"
                        )
                    tms_y = (1 << zoom) - 1 - y
                    archive.execute(
                        """INSERT INTO tiles(zoom_level, tile_column, tile_row, tile_data)
                           VALUES (?, ?, ?, ?)""",
                        (zoom, x, tms_y, compressed),
                    )
                    zoom_tiles += 1
                    tile_count += 1
                    tile_feature_count += len(encoded_features) + len(encoded_unknowns)
                    sizes_by_zoom[zoom].append((compressed_size, uncompressed_size))
                    now = monotonic()
                    if now - last_progress_at >= TILE_PROGRESS_EVERY_SECONDS:
                        LOGGER.info(
                            "Tile encoding progress zoom=%s candidates=%s/%s emitted_tiles=%s elapsed_s=%.1f",
                            zoom,
                            candidate_number,
                            len(candidate_tiles),
                            zoom_tiles,
                            now - zoom_started,
                        )
                        last_progress_at = now
                archive.commit()
                LOGGER.info(
                    "Built zoom=%s tiles=%s source_features=%s simplify_m=%.3f elapsed_s=%.1f",
                    zoom,
                    zoom_tiles,
                    feature_count,
                    simplify_tolerance,
                    monotonic() - zoom_started,
                )
            LOGGER.info(
                "Verifying complete maximum-zoom representation expected_features=%s represented_features=%s",
                len(block_faces),
                len(represented_at_maxzoom),
            )
            if len(represented_at_maxzoom) != len(block_faces):
                nonrenderable_at_maxzoom = set(range(len(block_faces))) - represented_at_maxzoom
            if len(represented_unknown_at_maxzoom) != len(unknown_faces):
                nonrenderable_unknown_at_maxzoom = (
                    set(range(len(unknown_faces))) - represented_unknown_at_maxzoom
                )
            maxzoom_nonrenderable_features = _nonrenderable_feature_records(
                block_faces, nonrenderable_at_maxzoom, unknown=False
            )
            maxzoom_nonrenderable_unknowns = _nonrenderable_feature_records(
                unknown_faces, nonrenderable_unknown_at_maxzoom, unknown=True
            )
            LOGGER.info(
                "Maximum-zoom renderability scheduled=%s/%s nonrenderable=%s "
                "source_fallbacks=%s unknown=%s/%s unknown_nonrenderable=%s "
                "unknown_source_fallbacks=%s",
                len(represented_at_maxzoom),
                len(block_faces),
                len(nonrenderable_at_maxzoom),
                len(source_fallback_at_maxzoom),
                len(represented_unknown_at_maxzoom),
                len(unknown_faces),
                len(nonrenderable_unknown_at_maxzoom),
                len(source_unknown_fallback_at_maxzoom),
            )
            if maxzoom_nonrenderable_features:
                LOGGER.warning(
                    "Scheduled features below the maximum-zoom tile grid count=%s ids=%s",
                    len(maxzoom_nonrenderable_features),
                    [record["id"] for record in maxzoom_nonrenderable_features[:20]],
                )
            if maxzoom_nonrenderable_unknowns:
                LOGGER.warning(
                    "Unknown features below the maximum-zoom tile grid count=%s ids=%s",
                    len(maxzoom_nonrenderable_unknowns),
                    [record["id"] for record in maxzoom_nonrenderable_unknowns[:20]],
                )
            if tile_count == 0:
                raise RuntimeError("tile build produced no tiles")
            tile_size_metrics = _tile_size_metrics(sizes_by_zoom, minzoom)
            archive.executemany(
                "INSERT INTO metadata(name, value) VALUES (?, ?)",
                _metadata_rows(
                    version=version,
                    source_database_sha256=source_database_sha256,
                    source_database_version=resolved_source_version,
                    source_block_face_count=snapshot.source_block_face_count,
                    source_schedule_count=snapshot.source_schedule_count,
                    source_schedule_group_count=snapshot.source_schedule_group_count,
                    feature_count=feature_count,
                    unknown_feature_count=len(unknown_faces),
                    maxzoom_feature_count=len(represented_at_maxzoom),
                    maxzoom_nonrenderable_feature_count=len(nonrenderable_at_maxzoom),
                    maxzoom_nonrenderable_feature_ids_sha256=_id_set_sha256(
                        record["id"] for record in maxzoom_nonrenderable_features
                    ),
                    maxzoom_unknown_feature_count=len(represented_unknown_at_maxzoom),
                    maxzoom_nonrenderable_unknown_feature_count=len(nonrenderable_unknown_at_maxzoom),
                    maxzoom_nonrenderable_unknown_ids_sha256=_id_set_sha256(
                        record["id"] for record in maxzoom_nonrenderable_unknowns
                    ),
                    bounds=bounds,
                    minzoom=minzoom,
                    maxzoom=maxzoom,
                    data_updated=data_updated,
                    simplify_pixels=simplify_pixels,
                    buffer_pixels=buffer_pixels,
                    optional_source_id_fields=snapshot.optional_source_id_fields,
                    tile_size_metrics=tile_size_metrics,
                    tile_size_limits=tile_size_limits,
                ),
            )
            archive.execute("ANALYZE")
            archive.commit()
        if _file_sha256(database_path) != source_database_sha256:
            raise RuntimeError("source database changed while vector tiles were being built")
        os.replace(temporary_path, output_path)
        LOGGER.info("Published completed vector tile archive path=%s", output_path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return BuildReport(
        version=version,
        tile_schema_revision=TILE_SCHEMA_REVISION,
        source_database_sha256=source_database_sha256,
        source_database_version=resolved_source_version,
        source_block_face_count=snapshot.source_block_face_count,
        source_schedule_count=snapshot.source_schedule_count,
        source_schedule_group_count=snapshot.source_schedule_group_count,
        tile_count=tile_count,
        feature_count=feature_count,
        geometry_count=feature_count,
        maxzoom_feature_count=len(represented_at_maxzoom),
        maxzoom_nonrenderable_feature_count=len(nonrenderable_at_maxzoom),
        maxzoom_nonrenderable_feature_ids_sha256=_id_set_sha256(
            record["id"] for record in maxzoom_nonrenderable_features
        ),
        unknown_feature_count=len(unknown_faces),
        maxzoom_unknown_feature_count=len(represented_unknown_at_maxzoom),
        maxzoom_nonrenderable_unknown_feature_count=len(nonrenderable_unknown_at_maxzoom),
        maxzoom_nonrenderable_unknown_ids_sha256=_id_set_sha256(
            record["id"] for record in maxzoom_nonrenderable_unknowns
        ),
        maxzoom_nonrenderable_features=maxzoom_nonrenderable_features,
        maxzoom_nonrenderable_unknowns=maxzoom_nonrenderable_unknowns,
        tile_feature_count=tile_feature_count,
        bounds=bounds,
        minzoom=minzoom,
        maxzoom=maxzoom,
        data_updated=data_updated,
        simplify_pixels=simplify_pixels,
        buffer_pixels=buffer_pixels,
        tile_size_metrics=tile_size_metrics,
        tile_size_limits=tile_size_limits,
        sha256=_file_sha256(output_path),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/app.sqlite3"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/collection_streets.mbtiles"),
    )
    parser.add_argument("--minzoom", type=int, default=int(os.getenv("TILE_MIN_ZOOM", str(DEFAULT_MIN_ZOOM))))
    parser.add_argument("--maxzoom", type=int, default=int(os.getenv("TILE_MAX_ZOOM", str(DEFAULT_MAX_ZOOM))))
    parser.add_argument("--version", help="Optional URL-safe dataset version supplied by the refresh job")
    parser.add_argument("--source-version", help="Optional source database revision for release binding")
    parser.add_argument("--data-updated", help="Timestamp exposed by /api/map-config")
    parser.add_argument("--simplify-pixels", type=float, default=0.5)
    parser.add_argument(
        "--buffer-pixels",
        type=float,
        default=DEFAULT_BUFFER_PIXELS,
        help=(
            f"Tile-edge geometry buffer in pixels (minimum "
            f"{MIN_RENDER_BUFFER_PIXELS:g} for the frontend style)"
        ),
    )
    parser.add_argument(
        "--max-compressed-tile-bytes",
        type=int,
        default=int(
            os.getenv("TILE_MAX_COMPRESSED_BYTES", str(DEFAULT_MAX_COMPRESSED_TILE_BYTES))
        ),
        help=(
            "Fail before publication at this size; may be lowered but not raised above "
            f"{DEFAULT_MAX_COMPRESSED_TILE_BYTES}"
        ),
    )
    parser.add_argument(
        "--max-uncompressed-tile-bytes",
        type=int,
        default=int(
            os.getenv("TILE_MAX_UNCOMPRESSED_BYTES", str(DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES))
        ),
        help=(
            "Fail before publication at this size; may be lowered but not raised above "
            f"{DEFAULT_MAX_UNCOMPRESSED_TILE_BYTES}"
        ),
    )
    parser.add_argument("--metadata-output", type=Path, help="Optional JSON build-report sidecar")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = build_tiles(
        args.database,
        args.output,
        minzoom=args.minzoom,
        maxzoom=args.maxzoom,
        version=args.version,
        source_version=args.source_version,
        data_updated=args.data_updated,
        simplify_pixels=args.simplify_pixels,
        buffer_pixels=args.buffer_pixels,
        max_compressed_tile_bytes=args.max_compressed_tile_bytes,
        max_uncompressed_tile_bytes=args.max_uncompressed_tile_bytes,
    )
    payload = report.as_dict()
    if args.metadata_output:
        _atomic_json(args.metadata_output, payload)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
