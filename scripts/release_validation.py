"""Release-gate checks and atomic publication for immutable dataset bundles."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zlib
from contextlib import closing, contextmanager
from pathlib import Path

import mapbox_vector_tile
from shapely import wkt
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from backend.app.releases import MANIFEST_VERSION, VERSION_PATTERN, read_current_release


LOGGER = logging.getLogger("release_validation")
VALID_COLLECTION_TYPES = {"REFUSE", "RECYCLING", "ORGANICS", "BULK"}
AUDIT_OUTCOMES = {
    "matched",
    "out_of_scope",
    "deduplicated_alias",
    "non_addressable",
    "outside_schedule_area",
    "partially_outside_schedule_area",
    "ambiguous",
    "invalid",
    "conflicts",
}
SOURCE_ROW_OUTCOMES = {
    "in_scope",
    "deduplicated_alias",
    "out_of_scope",
    "curbside_out_of_scope",
    "invalid",
}
FATAL_SIDE_OUTCOMES = {"ambiguous", "invalid", "conflicts"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_TILE_SCHEMA_REVISION = 2
MAX_RELEASE_COMPRESSED_TILE_BYTES = 512_000
MAX_RELEASE_UNCOMPRESSED_TILE_BYTES = 5_242_880
TILE_SOURCE_LAYER = "collection_streets"
TILE_REQUIRED_PROPERTIES = {
    "id",
    "origin_block_face_id",
    "name",
    "street_name",
    "borough",
    "side",
    "refuse_days",
    "recycling_days",
    "organics_days",
    "bulk_days",
    "source",
    "retrieved_at",
}
VALID_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT"}
DAY_ORDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT")
COLLECTION_DAY_FIELDS = {
    "REFUSE": "refuse_days",
    "RECYCLING": "recycling_days",
    "ORGANICS": "organics_days",
    "BULK": "bulk_days",
}
RELEASE_FILENAMES = {
    "database": "app.sqlite3",
    "tileset": "collection_streets.mbtiles",
    "ingestion_audit": "ingestion_audit.json",
    "processed_geojson": "citywide.geojson",
    "tile_build_report": "tile_build_report.json",
    "source_report": "source_report.json",
    "ingestion_failures": "ingestion_failures.jsonl",
    "source_dsny": "dsny_frequencies.geojson",
    "source_lion": "lion.zip",
}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_object(path: str | Path, label: str = "JSON artifact") -> dict[str, object]:
    artifact = Path(path)
    try:
        value = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{label} is invalid: {artifact}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {artifact}")
    return value


def atomic_json(path: str | Path, payload: dict[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".staged", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass
        raise


def validate_database(
    path: str | Path,
    *,
    known_sha256: str | None = None,
    expected_version: str | None = None,
    expected_processed_sha256: str | None = None,
    expected_processed_semantic_sha256: str | None = None,
    expected_processed_features: int | None = None,
    expected_audit_sha256: str | None = None,
) -> dict[str, object]:
    database = Path(path)
    if not database.is_file() or database.stat().st_size == 0:
        raise RuntimeError(f"staged database is missing or empty: {database}")
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"staged database integrity check failed: {integrity}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(f"staged database has foreign-key errors: {foreign_key_errors[:10]}")

        block_faces = connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0]
        if block_faces <= 0:
            raise RuntimeError("staged database contains no block faces")
        block_face_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(block_faces)")
        }
        if "origin_block_face_id" not in block_face_columns:
            raise RuntimeError("staged database is missing origin block-face provenance")
        blank_origins = connection.execute(
            "SELECT COUNT(*) FROM block_faces WHERE TRIM(origin_block_face_id) = ''"
        ).fetchone()[0]
        schedule_count = connection.execute("SELECT COUNT(*) FROM collection_schedules").fetchone()[0]
        schedule_group_count = connection.execute(
            """SELECT COUNT(*) FROM (
                   SELECT block_face_id, collection_type
                   FROM collection_schedules
                   GROUP BY block_face_id, collection_type
               )"""
        ).fetchone()[0]
        schedule_counts = dict(
            connection.execute(
                "SELECT collection_type, COUNT(*) FROM collection_schedules GROUP BY collection_type"
            )
        )
        unknown_types = set(schedule_counts) - VALID_COLLECTION_TYPES
        if unknown_types:
            raise RuntimeError(f"staged database contains unknown collection types: {sorted(unknown_types)}")
        missing_types = VALID_COLLECTION_TYPES - set(schedule_counts)
        if missing_types:
            raise RuntimeError(f"staged database is missing collection types: {sorted(missing_types)}")

        faces_without_schedules = connection.execute(
            """SELECT COUNT(*) FROM block_faces bf
               WHERE NOT EXISTS (
                   SELECT 1 FROM collection_schedules cs
                   WHERE cs.block_face_id = bf.block_face_id
               )"""
        ).fetchone()[0]
        rtree_rows = connection.execute("SELECT COUNT(*) FROM block_faces_rtree").fetchone()[0]
        rtree_map_rows = connection.execute("SELECT COUNT(*) FROM block_face_rtree_map").fetchone()[0]
        missing_rtree_rows = connection.execute(
            """SELECT COUNT(*) FROM block_faces bf
               LEFT JOIN block_face_rtree_map bm ON bm.block_face_id = bf.block_face_id
               LEFT JOIN block_faces_rtree br ON br.rtree_id = bm.rtree_id
               WHERE bm.rtree_id IS NULL OR br.rtree_id IS NULL"""
        ).fetchone()[0]
        validation_counts = dict(
            connection.execute(
                "SELECT validation_status, COUNT(*) FROM collection_schedules GROUP BY validation_status"
            )
        )
        metadata = dict(connection.execute("SELECT key, value FROM dataset_metadata"))
        provenance_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required_provenance = {"block_face_lion_components", "block_face_dsny_sources"}
        if not required_provenance.issubset(provenance_tables):
            raise RuntimeError("staged database is missing provenance tables")
        lion_provenance_faces = connection.execute(
            "SELECT COUNT(DISTINCT block_face_id) FROM block_face_lion_components"
        ).fetchone()[0]
        dsny_provenance_faces = connection.execute(
            "SELECT COUNT(DISTINCT block_face_id) FROM block_face_dsny_sources"
        ).fetchone()[0]

    if faces_without_schedules:
        raise RuntimeError(f"staged database has block faces without schedules: {faces_without_schedules}")
    if blank_origins:
        raise RuntimeError(f"staged database has blank origin block-face IDs: {blank_origins}")
    if lion_provenance_faces != block_faces or dsny_provenance_faces != block_faces:
        raise RuntimeError(
            "staged database provenance coverage is incomplete "
            f"block_faces={block_faces} lion={lion_provenance_faces} dsny={dsny_provenance_faces}"
        )
    if rtree_rows != block_faces or rtree_map_rows != block_faces or missing_rtree_rows:
        raise RuntimeError(
            "staged database spatial index is incomplete "
            f"block_faces={block_faces} map={rtree_map_rows} rtree={rtree_rows} "
            f"missing={missing_rtree_rows}"
        )
    expected_metadata = {
        "dataset_version": expected_version,
        "processed_sha256": expected_processed_sha256,
        "processed_semantic_sha256": expected_processed_semantic_sha256,
        "processed_feature_count": (
            str(expected_processed_features) if expected_processed_features is not None else None
        ),
        "ingestion_audit_sha256": expected_audit_sha256,
    }
    for key, expected in expected_metadata.items():
        if expected is not None and metadata.get(key) != expected:
            raise RuntimeError(
                f"staged database metadata mismatch key={key} "
                f"expected={expected!r} actual={metadata.get(key)!r}"
            )
    return {
        "integrity_check": integrity,
        "sha256": known_sha256 or file_sha256(database),
        "bytes": database.stat().st_size,
        "block_faces": block_faces,
        "schedule_count": schedule_count,
        "schedule_group_count": schedule_group_count,
        "schedule_counts": schedule_counts,
        "validation_counts": validation_counts,
        "faces_without_schedules": faces_without_schedules,
        "rtree_rows": rtree_rows,
        "lion_provenance_faces": lion_provenance_faces,
        "dsny_provenance_faces": dsny_provenance_faces,
        "metadata": metadata,
    }


def validate_tileset(
    path: str | Path,
    expected_version: str | None = None,
    *,
    known_sha256: str | None = None,
    expected_database: dict[str, object] | None = None,
    expected_database_path: str | Path | None = None,
) -> dict[str, object]:
    tileset = Path(path)
    if not tileset.is_file() or tileset.stat().st_size == 0:
        raise RuntimeError(f"staged tileset is missing or empty: {tileset}")
    with closing(sqlite3.connect(f"{tileset.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"staged tileset integrity check failed: {integrity}")
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        required = {
            "format",
            "version",
            "minzoom",
            "maxzoom",
            "source_layer",
            "tile_schema_revision",
            "source_database_sha256",
            "source_database_version",
            "source_block_face_count",
            "source_schedule_count",
            "source_schedule_group_count",
            "feature_count",
            "geometry_count",
            "maxzoom_feature_count",
            "tile_size_metrics",
            "tile_size_limits",
            "compression",
            "bounds",
        }
        missing = required - set(metadata)
        if missing:
            raise RuntimeError(f"staged tileset metadata is incomplete: {sorted(missing)}")
        if metadata["format"] != "pbf":
            raise RuntimeError(f"staged tileset has unexpected format: {metadata['format']}")
        if expected_version is not None and metadata["version"] != expected_version:
            raise RuntimeError(
                f"staged tileset version mismatch expected={expected_version} actual={metadata['version']}"
            )
        minzoom = _metadata_int(metadata, "minzoom")
        maxzoom = _metadata_int(metadata, "maxzoom")
        bounds = _validated_bounds(metadata.get("bounds"), "staged tileset metadata bounds")
        tile_count = connection.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        invalid_gzip = connection.execute(
            "SELECT COUNT(*) FROM tiles WHERE substr(tile_data, 1, 2) != x'1f8b'"
        ).fetchone()[0]
        invalid_coordinates = connection.execute(
            """SELECT COUNT(*) FROM tiles
               WHERE zoom_level < ? OR zoom_level > ?
                  OR tile_column < 0 OR tile_column >= (1 << zoom_level)
                  OR tile_row < 0 OR tile_row >= (1 << zoom_level)""",
            (minzoom, maxzoom),
        ).fetchone()[0]
        payload_validation = _validate_tile_payloads(
            connection,
            metadata,
            minzoom=minzoom,
            maxzoom=maxzoom,
        )
    if tile_count <= 0:
        raise RuntimeError("staged tileset contains no tiles")
    if invalid_gzip:
        raise RuntimeError(f"staged tileset contains non-gzip tile payloads: {invalid_gzip}")
    if invalid_coordinates:
        raise RuntimeError(f"staged tileset contains invalid coordinates: {invalid_coordinates}")

    binding_keys = (
        "tile_schema_revision",
        "source_database_sha256",
        "source_database_version",
        "source_block_face_count",
        "source_schedule_count",
        "source_schedule_group_count",
        "feature_count",
        "geometry_count",
        "maxzoom_feature_count",
    )
    binding: dict[str, object] = {}
    for key in binding_keys:
        binding[key] = (
            metadata[key]
            if key in {"source_database_sha256", "source_database_version"}
            else _metadata_int(metadata, key)
        )
    if binding["tile_schema_revision"] != EXPECTED_TILE_SCHEMA_REVISION:
        raise RuntimeError(
            "staged tileset schema revision mismatch "
            f"expected={EXPECTED_TILE_SCHEMA_REVISION} actual={binding['tile_schema_revision']}"
        )
    if binding["source_database_sha256"] is None or not SHA256_PATTERN.fullmatch(
        str(binding["source_database_sha256"])
    ):
        raise RuntimeError("staged tileset source database checksum is invalid")
    payload_expected = {
        "maxzoom_unique_id_count": binding["maxzoom_feature_count"],
        "decoded_tile_feature_count": payload_validation["decoded_tile_feature_count"],
    }
    if binding["maxzoom_feature_count"] != binding["feature_count"]:
        raise RuntimeError("staged tileset maxzoom coverage count does not match feature_count")
    if payload_validation["maxzoom_unique_id_count"] != binding["maxzoom_feature_count"]:
        raise RuntimeError(
            "staged tileset decoded maxzoom ID coverage does not match metadata "
            f"expected={binding['maxzoom_feature_count']} "
            f"actual={payload_validation['maxzoom_unique_id_count']}"
        )
    if expected_database is not None:
        if expected_database_path is None:
            raise RuntimeError("database path is required for semantic tile validation")
        expected_bindings = {
            "tile_schema_revision": EXPECTED_TILE_SCHEMA_REVISION,
            "source_database_sha256": expected_database["sha256"],
            "source_database_version": expected_database["metadata"]["dataset_version"],
            "source_block_face_count": expected_database["block_faces"],
            "source_schedule_count": expected_database["schedule_count"],
            "source_schedule_group_count": expected_database["schedule_group_count"],
            "feature_count": expected_database["block_faces"],
            "geometry_count": expected_database["block_faces"],
        }
        _require_equal_fields(binding, expected_bindings, "MBTiles/database binding")
        expected_properties = _expected_tile_properties(Path(expected_database_path))
        # Compare the union from every zoom, not only maxzoom.  A stale or
        # invented feature present solely in an initial-view tile must fail.
        actual_properties = payload_validation["properties_by_id"]
        if not _same_value(actual_properties, expected_properties):
            missing_ids = sorted(set(expected_properties) - set(actual_properties))[:10]
            unexpected_ids = sorted(set(actual_properties) - set(expected_properties))[:10]
            mismatched_ids = sorted(
                feature_id
                for feature_id in set(expected_properties) & set(actual_properties)
                if not _same_value(actual_properties[feature_id], expected_properties[feature_id])
            )[:10]
            raise RuntimeError(
                "staged tileset semantic content does not match source database "
                f"missing_ids={missing_ids} unexpected_ids={unexpected_ids} "
                f"mismatched_ids={mismatched_ids}"
            )
    return {
        "integrity_check": integrity,
        "sha256": known_sha256 or file_sha256(tileset),
        "version": metadata["version"],
        "source_layer": metadata["source_layer"],
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "bounds": bounds,
        "tile_count": tile_count,
        "bytes": tileset.stat().st_size,
        **binding,
        **payload_expected,
        "feature_coverage_by_zoom": payload_validation["feature_coverage_by_zoom"],
        "tile_size_metrics": payload_validation["tile_size_metrics"],
        "tile_size_limits": payload_validation["tile_size_limits"],
    }


def _validate_tile_payloads(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    minzoom: int,
    maxzoom: int,
) -> dict[str, object]:
    if metadata.get("source_layer") != TILE_SOURCE_LAYER:
        raise RuntimeError(f"staged tileset source layer must be {TILE_SOURCE_LAYER}")
    if metadata.get("compression") != "gzip":
        raise RuntimeError("staged tileset must declare gzip compression")
    limits = _metadata_json_object(metadata, "tile_size_limits")
    if set(limits) != {"max_compressed_tile_bytes", "max_uncompressed_tile_bytes"}:
        raise RuntimeError("staged tileset tile_size_limits has unexpected fields")
    compressed_limit = _nonnegative_int(
        limits.get("max_compressed_tile_bytes"),
        "staged tileset compressed size limit",
    )
    uncompressed_limit = _nonnegative_int(
        limits.get("max_uncompressed_tile_bytes"),
        "staged tileset uncompressed size limit",
    )
    if compressed_limit <= 0 or uncompressed_limit <= 0:
        raise RuntimeError("staged tileset size limits must be positive")
    if (
        compressed_limit > MAX_RELEASE_COMPRESSED_TILE_BYTES
        or uncompressed_limit > MAX_RELEASE_UNCOMPRESSED_TILE_BYTES
    ):
        raise RuntimeError("staged tileset declares size limits above the release safety budget")

    sizes_by_zoom: dict[int, list[tuple[int, int]]] = {}
    ids_by_zoom: dict[int, set[str]] = {}
    maxzoom_ids: set[str] = set()
    properties_by_id: dict[str, dict[str, object]] = {}
    decoded_feature_count = 0
    rows = connection.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
        "ORDER BY zoom_level, tile_column, tile_row"
    )
    for zoom, column, row, raw_tile in rows:
        tile_data = bytes(raw_tile)
        compressed_size = len(tile_data)
        if compressed_size > compressed_limit:
            raise RuntimeError(
                "staged tileset compressed tile exceeds its declared limit "
                f"z={zoom} x={column} row={row} bytes={compressed_size} limit={compressed_limit}"
            )
        decoded_bytes = _decompress_gzip_bounded(tile_data, uncompressed_limit)
        uncompressed_size = len(decoded_bytes)
        try:
            decoded = mapbox_vector_tile.decode(decoded_bytes)
        except Exception as error:
            raise RuntimeError(
                f"staged tileset contains an invalid PBF z={zoom} x={column} row={row}"
            ) from error
        if not isinstance(decoded, dict) or set(decoded) != {TILE_SOURCE_LAYER}:
            raise RuntimeError(
                f"staged tile has unexpected vector layers z={zoom} x={column} row={row}"
            )
        layer = decoded[TILE_SOURCE_LAYER]
        features = layer.get("features") if isinstance(layer, dict) else None
        if not isinstance(features, list) or not features:
            raise RuntimeError(f"staged tile has no collection features z={zoom} x={column} row={row}")
        tile_ids: set[str] = set()
        for feature in features:
            if not isinstance(feature, dict):
                raise RuntimeError("staged tile contains a non-object feature")
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                raise RuntimeError("staged tile feature is missing properties")
            missing_properties = TILE_REQUIRED_PROPERTIES - set(properties)
            if missing_properties:
                raise RuntimeError(
                    f"staged tile feature is missing properties: {sorted(missing_properties)}"
                )
            _validate_tile_properties(properties)
            feature_id = str(properties["id"])
            if feature_id in tile_ids:
                raise RuntimeError(f"staged tile contains duplicate feature ID {feature_id!r}")
            tile_ids.add(feature_id)
            ids_by_zoom.setdefault(int(zoom), set()).add(feature_id)
            previous_properties = properties_by_id.setdefault(feature_id, properties)
            if previous_properties != properties:
                raise RuntimeError(
                    f"staged tileset has inconsistent properties for feature ID {feature_id!r}"
                )
            geometry = feature.get("geometry")
            if (
                not isinstance(geometry, dict)
                or geometry.get("type") not in {"LineString", "MultiLineString"}
                or not geometry.get("coordinates")
            ):
                raise RuntimeError(f"staged tile feature {feature_id!r} has invalid line geometry")
            if zoom == maxzoom:
                maxzoom_ids.add(feature_id)
        decoded_feature_count += len(features)
        sizes_by_zoom.setdefault(int(zoom), []).append((compressed_size, uncompressed_size))

    expected_zooms = set(range(minzoom, maxzoom + 1))
    if set(sizes_by_zoom) != expected_zooms:
        raise RuntimeError(
            "staged tileset has missing zoom coverage "
            f"expected={sorted(expected_zooms)} actual={sorted(sizes_by_zoom)}"
        )
    calculated_metrics = _tile_size_metrics(sizes_by_zoom, minzoom)
    declared_metrics = _metadata_json_object(metadata, "tile_size_metrics")
    if not _same_value(declared_metrics, calculated_metrics):
        raise RuntimeError("staged tileset tile-size metrics do not match decoded payloads")
    return {
        "decoded_tile_feature_count": decoded_feature_count,
        "maxzoom_unique_id_count": len(maxzoom_ids),
        "properties_by_id": properties_by_id,
        "feature_coverage_by_zoom": {
            str(zoom): len(ids_by_zoom.get(zoom, set()))
            for zoom in range(minzoom, maxzoom + 1)
        },
        "tile_size_metrics": calculated_metrics,
        "tile_size_limits": limits,
    }


def _validate_tile_properties(properties: dict[str, object]) -> None:
    required_nonblank = TILE_REQUIRED_PROPERTIES - {
        "recycling_days",
        "organics_days",
        "bulk_days",
    }
    for key in TILE_REQUIRED_PROPERTIES:
        if not isinstance(properties.get(key), str):
            raise RuntimeError(f"staged tile property {key!r} must be a string")
        if key in required_nonblank and not str(properties[key]).strip():
            raise RuntimeError(f"staged tile property {key!r} must not be blank")
    if properties["side"] not in {"LEFT", "RIGHT"}:
        raise RuntimeError("staged tile feature has an invalid side")
    for key in ("refuse_days", "recycling_days", "organics_days", "bulk_days"):
        value = str(properties[key])
        days = value.split(",") if value else []
        if len(days) != len(set(days)) or not set(days).issubset(VALID_DAYS):
            raise RuntimeError(f"staged tile property {key!r} has invalid weekdays")


def _expected_tile_properties(database: Path) -> dict[str, dict[str, object]]:
    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(block_faces)")}
        optional_ids = tuple(
            field
            for field in ("origin_block_face_id", "source_block_face_id")
            if field in columns
        )
        optional_select = "".join(f", {field}" for field in optional_ids)
        expected: dict[str, dict[str, object]] = {}
        day_sets: dict[str, dict[str, set[str]]] = {}
        sources: dict[str, set[str]] = {}
        retrieval_times: dict[str, set[str]] = {}
        for row in connection.execute(
            "SELECT block_face_id, street_name, borough, side"
            f"{optional_select} FROM block_faces ORDER BY block_face_id"
        ):
            feature_id = str(row["block_face_id"]).strip()
            properties: dict[str, object] = {
                "id": feature_id,
                "name": str(row["street_name"]).strip(),
                "street_name": str(row["street_name"]).strip(),
                "borough": str(row["borough"]).strip(),
                "side": str(row["side"]).strip().upper(),
            }
            properties.update({field: str(row[field]).strip() for field in optional_ids})
            expected[feature_id] = properties
            day_sets[feature_id] = {kind: set() for kind in COLLECTION_DAY_FIELDS}
            sources[feature_id] = set()
            retrieval_times[feature_id] = set()
        for row in connection.execute(
            """SELECT block_face_id, collection_type, weekday, source, retrieved_at
               FROM collection_schedules
               ORDER BY block_face_id, collection_type, weekday"""
        ):
            feature_id = str(row["block_face_id"]).strip()
            collection_type = str(row["collection_type"]).strip().upper()
            weekday = str(row["weekday"]).strip().upper()
            if feature_id not in expected or collection_type not in COLLECTION_DAY_FIELDS:
                raise RuntimeError("source database has an invalid tile schedule binding")
            day_sets[feature_id][collection_type].add(weekday)
            sources[feature_id].add(str(row["source"]).strip())
            retrieval_times[feature_id].add(str(row["retrieved_at"]).strip())

    for feature_id, properties in expected.items():
        if len(sources[feature_id]) != 1 or len(retrieval_times[feature_id]) != 1:
            raise RuntimeError(
                f"source database has conflicting tile provenance id={feature_id!r}"
            )
        for collection_type, field in COLLECTION_DAY_FIELDS.items():
            properties[field] = ",".join(
                day for day in DAY_ORDER if day in day_sets[feature_id][collection_type]
            )
        properties["source"] = next(iter(sources[feature_id]))
        properties["retrieved_at"] = next(iter(retrieval_times[feature_id]))
    return expected


def _decompress_gzip_bounded(payload: bytes, limit: int) -> bytes:
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = inflater.decompress(payload, limit + 1)
        if len(decoded) > limit or inflater.unconsumed_tail:
            raise RuntimeError("staged tileset contains an oversized uncompressed tile")
        decoded += inflater.flush()
    except zlib.error as error:
        raise RuntimeError("staged tileset contains an invalid gzip tile payload") from error
    if len(decoded) > limit:
        raise RuntimeError("staged tileset contains an oversized uncompressed tile")
    if not inflater.eof or inflater.unused_data:
        raise RuntimeError("staged tileset contains an incomplete or concatenated gzip payload")
    return decoded


def _metadata_json_object(metadata: dict[str, str], key: str) -> dict[str, object]:
    try:
        value = json.loads(metadata[key])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"staged tileset metadata {key!r} must be valid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"staged tileset metadata {key!r} must be an object")
    return value


def _nearest_rank_percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


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


def validate_ingestion_audit(
    path: str | Path,
    *,
    expected_processed_sha256: str | None = None,
    expected_processed_features: int | None = None,
) -> dict[str, object]:
    audit = read_json_object(path, "ingestion audit")
    required = {
        "source_rows",
        "frequency_rows",
        "expected_sides",
        "classified_sides",
        "outcomes",
        "source_row_outcomes",
        "frequency_outcomes",
        "reconciliation",
        "source_row_reconciliation",
        "frequency_reconciliation",
        "reconciled",
        "fatal_side_count",
        "fatal_frequency_count",
        "fatal_count",
        "output_features",
        "processed_sha256",
        "processed_feature_count",
        "global_errors",
        "records",
        "passed",
    }
    missing = required - set(audit)
    if missing:
        raise RuntimeError(f"ingestion audit is missing fields: {sorted(missing)}")

    scalar_counts = (
        "source_rows",
        "frequency_rows",
        "expected_sides",
        "classified_sides",
        "fatal_side_count",
        "fatal_frequency_count",
        "fatal_count",
        "output_features",
        "processed_feature_count",
    )
    counts = {key: _nonnegative_int(audit.get(key), f"ingestion audit {key}") for key in scalar_counts}
    if counts["source_rows"] <= 0 or counts["frequency_rows"] <= 0:
        raise RuntimeError("ingestion audit source counts must be positive")
    if counts["expected_sides"] != counts["source_rows"] * 2:
        raise RuntimeError("ingestion audit expected_sides must equal source_rows * 2")

    outcomes = _count_map(audit["outcomes"], "ingestion audit outcomes", AUDIT_OUTCOMES)
    source_outcomes = _count_map(
        audit["source_row_outcomes"],
        "ingestion audit source_row_outcomes",
        SOURCE_ROW_OUTCOMES,
    )
    frequency_outcomes = _count_map(
        audit["frequency_outcomes"],
        "ingestion audit frequency_outcomes",
        {"used_valid", "unused_valid", "invalid"},
    )
    if sum(outcomes.values()) != counts["classified_sides"] or counts["classified_sides"] != counts["expected_sides"]:
        raise RuntimeError(
            "ingestion audit does not reconcile sides "
            f"expected={counts['expected_sides']} classified={counts['classified_sides']} "
            f"outcomes={sum(outcomes.values())}"
        )
    if sum(source_outcomes.values()) != counts["source_rows"]:
        raise RuntimeError("ingestion audit source-row outcomes do not reconcile")
    if sum(frequency_outcomes.values()) != counts["frequency_rows"]:
        raise RuntimeError("ingestion audit frequency outcomes do not reconcile")
    _reconciliation(audit["reconciliation"], counts["expected_sides"], counts["classified_sides"], "side")
    _reconciliation(audit["source_row_reconciliation"], counts["source_rows"], sum(source_outcomes.values()), "source-row")
    _reconciliation(audit["frequency_reconciliation"], counts["frequency_rows"], sum(frequency_outcomes.values()), "frequency")
    scalar_outcomes = {
        "matched": outcomes["matched"],
        "unmatched": outcomes["outside_schedule_area"],
        "outside_schedule_area": outcomes["outside_schedule_area"],
        "partially_outside_schedule_area": outcomes["partially_outside_schedule_area"],
        "non_addressable": outcomes["non_addressable"],
        "ambiguous": outcomes["ambiguous"],
        "invalid": outcomes["invalid"],
        "conflicts": outcomes["conflicts"],
        "raw_source_rows": counts["source_rows"],
        "raw_lion_rows": counts["source_rows"],
        "classified_source_rows": sum(source_outcomes.values()),
        "in_scope_source_rows": (
            source_outcomes["in_scope"]
            + source_outcomes["deduplicated_alias"]
            + source_outcomes["curbside_out_of_scope"]
        ),
        "in_scope_lion_rows": (
            source_outcomes["in_scope"]
            + source_outcomes["deduplicated_alias"]
            + source_outcomes["curbside_out_of_scope"]
        ),
        "eligible_lion_rows": source_outcomes["in_scope"] + source_outcomes["deduplicated_alias"],
        "processed_segment_rows": source_outcomes["in_scope"],
        "deduplicated_alias_rows": source_outcomes["deduplicated_alias"],
        "curbside_excluded_lion_rows": source_outcomes["curbside_out_of_scope"],
        "out_of_scope_source_rows": source_outcomes["out_of_scope"],
        "excluded_lion_rows": source_outcomes["out_of_scope"],
        "invalid_source_rows": source_outcomes["invalid"],
        "valid_frequency_rows": frequency_outcomes["used_valid"] + frequency_outcomes["unused_valid"],
        "used_valid_frequency_rows": frequency_outcomes["used_valid"],
        "unused_valid_frequency_rows": frequency_outcomes["unused_valid"],
        "invalid_frequency_rows": frequency_outcomes["invalid"],
    }
    _require_equal_fields(audit, scalar_outcomes, "ingestion audit count aliases")

    fatal_side = sum(outcomes[key] for key in FATAL_SIDE_OUTCOMES)
    fatal_frequency = frequency_outcomes["unused_valid"] + frequency_outcomes["invalid"]
    if counts["fatal_side_count"] != fatal_side:
        raise RuntimeError("ingestion audit fatal_side_count is inconsistent with outcomes")
    if counts["fatal_frequency_count"] != fatal_frequency:
        raise RuntimeError("ingestion audit fatal_frequency_count is inconsistent with outcomes")
    global_errors = audit["global_errors"]
    records = audit["records"]
    if not isinstance(global_errors, list) or any(not isinstance(item, dict) for item in global_errors):
        raise RuntimeError("ingestion audit global_errors must be an array of objects")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise RuntimeError("ingestion audit records must be an array of objects")
    schema_errors = sum(
        item.get("kind") in {"missing_frequency_fields", "missing_lion_scope_fields"}
        for item in global_errors
    )
    if counts["fatal_count"] != fatal_side + fatal_frequency + schema_errors:
        raise RuntimeError("ingestion audit fatal_count is inconsistent with component counts")

    processed_hash = audit["processed_sha256"]
    if not isinstance(processed_hash, str) or not SHA256_PATTERN.fullmatch(processed_hash):
        raise RuntimeError("ingestion audit processed_sha256 is invalid")
    if counts["processed_feature_count"] != counts["output_features"]:
        raise RuntimeError("ingestion audit processed feature counts do not agree")
    if expected_processed_sha256 is not None and processed_hash != expected_processed_sha256:
        raise RuntimeError("ingestion audit is bound to a different processed GeoJSON")
    if expected_processed_features is not None and counts["output_features"] != expected_processed_features:
        raise RuntimeError("ingestion audit output count does not match processed GeoJSON")

    reconciled = audit["reconciled"] is True
    expected_passed = reconciled and counts["fatal_count"] == 0 and counts["output_features"] > 0
    if not reconciled:
        raise RuntimeError("ingestion audit is not reconciled")
    if audit["passed"] is not expected_passed or not expected_passed:
        raise RuntimeError(f"ingestion audit quality gate failed: {counts['fatal_count']} fatal")
    return audit


def validate_processed_geojson(
    path: str | Path,
    *,
    known_sha256: str | None = None,
    expected_sha256: str | None = None,
    expected_semantic_sha256: str | None = None,
    expected_features: int | None = None,
) -> dict[str, object]:
    processed = Path(path)
    payload = read_json_object(processed, "processed GeoJSON")
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise RuntimeError("processed GeoJSON must be a FeatureCollection")
    feature_count = len(payload["features"])
    if feature_count <= 0:
        raise RuntimeError("processed GeoJSON contains no features")
    checksum = known_sha256 or file_sha256(processed)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise RuntimeError("processed GeoJSON checksum does not match its binding")
    if expected_features is not None and feature_count != expected_features:
        raise RuntimeError(
            f"processed GeoJSON feature count mismatch expected={expected_features} actual={feature_count}"
        )
    feature_hashes = _processed_feature_hashes(payload)
    if len(feature_hashes) != feature_count:
        raise RuntimeError(
            "processed GeoJSON contains repeated block-face IDs that would be collapsed by the loader"
        )
    semantic_sha256 = _aggregate_feature_hashes(feature_hashes)
    if expected_semantic_sha256 is not None and semantic_sha256 != expected_semantic_sha256:
        raise RuntimeError("processed GeoJSON semantic checksum does not match its binding")
    return {
        "sha256": checksum,
        "semantic_sha256": semantic_sha256,
        "feature_hashes": feature_hashes,
        "feature_count": feature_count,
        "bytes": processed.stat().st_size,
    }


def validate_processed_database_semantics(
    processed: dict[str, object],
    database_path: str | Path,
) -> dict[str, object]:
    """Prove independently that every processed feature survived the SQLite load."""

    expected_hashes = processed.get("feature_hashes")
    if not isinstance(expected_hashes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in expected_hashes.items()
    ):
        raise RuntimeError("processed semantic feature bindings are unavailable")
    actual_hashes = _database_feature_hashes(Path(database_path))
    expected_ids = set(expected_hashes)
    actual_ids = set(actual_hashes)
    mismatched = sorted(
        feature_id
        for feature_id in expected_ids & actual_ids
        if expected_hashes[feature_id] != actual_hashes[feature_id]
    )
    missing = sorted(expected_ids - actual_ids)
    unexpected = sorted(actual_ids - expected_ids)
    if missing or unexpected or mismatched:
        raise RuntimeError(
            "processed/database semantic reconciliation failed "
            f"missing={missing[:10]} unexpected={unexpected[:10]} "
            f"mismatched={mismatched[:10]}"
        )
    semantic_sha256 = _aggregate_feature_hashes(actual_hashes)
    if semantic_sha256 != processed.get("semantic_sha256"):
        raise RuntimeError("processed/database aggregate semantic checksum mismatch")
    return {
        "semantic_sha256": semantic_sha256,
        "semantic_feature_count": len(actual_hashes),
    }


def _processed_feature_hashes(payload: dict[str, object]) -> dict[str, str]:
    # Importing the loader's input validator ensures the expected side uses the
    # exact documented aggregation/normalization contract.  The actual side
    # below is reconstructed independently from normalized relational rows.
    from scripts.load_processed import prepare_features

    try:
        features = prepare_features(payload)
    except ValueError as error:
        raise RuntimeError(f"processed GeoJSON semantic validation failed: {error}") from error
    records: dict[str, dict[str, object]] = {}
    for feature in features:
        records[feature.block_face_id] = {
            "block_face_id": feature.block_face_id,
            "origin_block_face_id": feature.origin_block_face_id,
            "stored_segment_id": "|".join(feature.segment_ids),
            "segment_ids": list(feature.segment_ids),
            "street_name": feature.street_name,
            "borough": feature.borough,
            "side": feature.side,
            "geometry": _canonical_geometry(feature.geometry, feature.block_face_id),
            "schedules": {
                collection_type: list(feature.schedules[collection_type])
                for collection_type in sorted(VALID_COLLECTION_TYPES)
            },
            "source": feature.source,
            "retrieved_at": feature.retrieved_at,
            "dsny_sources": list(feature.dsny_sources),
            "lion_components": list(feature.lion_components),
        }
    return {
        feature_id: _canonical_record_sha256(record)
        for feature_id, record in records.items()
    }


def _database_feature_hashes(database: Path) -> dict[str, str]:
    if not database.is_file():
        raise RuntimeError(f"semantic source database is missing: {database}")
    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        records: dict[str, dict[str, object]] = {}
        for row in connection.execute(
            """SELECT block_face_id, origin_block_face_id, segment_id, street_name,
                      borough, side, geometry_wkt
               FROM block_faces ORDER BY block_face_id"""
        ):
            feature_id = str(row["block_face_id"])
            if feature_id in records:
                raise RuntimeError(f"source database repeats block-face ID {feature_id!r}")
            try:
                geometry = wkt.loads(str(row["geometry_wkt"]))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"source database has invalid geometry id={feature_id!r}"
                ) from error
            records[feature_id] = {
                "block_face_id": feature_id,
                "origin_block_face_id": str(row["origin_block_face_id"]),
                "stored_segment_id": str(row["segment_id"]),
                "segment_ids": [],
                "street_name": str(row["street_name"]),
                "borough": str(row["borough"]),
                "side": str(row["side"]),
                "geometry": _canonical_geometry(geometry, feature_id),
                "schedules": {
                    collection_type: []
                    for collection_type in sorted(VALID_COLLECTION_TYPES)
                },
                "source": None,
                "retrieved_at": None,
                "dsny_sources": [],
                "lion_components": [],
            }

        sources: dict[str, set[str]] = {feature_id: set() for feature_id in records}
        retrieved: dict[str, set[str]] = {feature_id: set() for feature_id in records}
        for row in connection.execute(
            """SELECT block_face_id, collection_type, weekday, source, retrieved_at
               FROM collection_schedules
               ORDER BY block_face_id, collection_type, weekday"""
        ):
            feature_id = str(row["block_face_id"])
            collection_type = str(row["collection_type"])
            if feature_id not in records or collection_type not in VALID_COLLECTION_TYPES:
                raise RuntimeError("source database has an invalid semantic schedule binding")
            records[feature_id]["schedules"][collection_type].append(str(row["weekday"]))
            sources[feature_id].add(str(row["source"]))
            retrieved[feature_id].add(str(row["retrieved_at"]))

        for row in connection.execute(
            """SELECT block_face_id, dsny_object_id, frequency_row, schedule_code,
                      section, district
               FROM block_face_dsny_sources
               ORDER BY block_face_id, dsny_object_id"""
        ):
            feature_id = str(row["block_face_id"])
            if feature_id not in records:
                raise RuntimeError("source database has orphaned DSNY semantic provenance")
            source: dict[str, object] = {
                "object_id": str(row["dsny_object_id"]),
                "frequency_row": row["frequency_row"],
            }
            for field_name in ("schedule_code", "section", "district"):
                if row[field_name] is not None:
                    source[field_name] = str(row[field_name])
            records[feature_id]["dsny_sources"].append(source)

        for row in connection.execute(
            """SELECT block_face_id, component_index, segment_id, source_side,
                      source_rows_json, source_indices_json, street_names_json,
                      source_records_json, dsny_object_ids_json
               FROM block_face_lion_components
               ORDER BY block_face_id, component_index"""
        ):
            feature_id = str(row["block_face_id"])
            if feature_id not in records:
                raise RuntimeError("source database has orphaned LION semantic provenance")
            component = {
                "segment_id": str(row["segment_id"]),
                "source_side": str(row["source_side"]),
                "source_rows": _json_array(row["source_rows_json"], "source_rows", feature_id),
                "source_indices": _json_array(
                    row["source_indices_json"], "source_indices", feature_id
                ),
                "street_names": _json_array(
                    row["street_names_json"], "street_names", feature_id
                ),
                "source_records": _json_array(
                    row["source_records_json"], "source_records", feature_id
                ),
                "dsny_object_ids": _json_array(
                    row["dsny_object_ids_json"], "dsny_object_ids", feature_id
                ),
            }
            records[feature_id]["lion_components"].append(component)
            records[feature_id]["segment_ids"].append(str(row["segment_id"]))

    for feature_id, record in records.items():
        if len(sources[feature_id]) != 1 or len(retrieved[feature_id]) != 1:
            raise RuntimeError(
                f"source database has conflicting semantic provenance id={feature_id!r}"
            )
        record["source"] = next(iter(sources[feature_id]))
        record["retrieved_at"] = next(iter(retrieved[feature_id]))
        record["segment_ids"] = sorted(set(record["segment_ids"]))
        # SQLite's lexical weekday order is not the source contract.  Rebuild
        # each list in the same Monday-to-Saturday order used by processed
        # GeoJSON, so semantically identical schedules hash identically.
        record["schedules"] = {
            collection_type: [
                day for day in DAY_ORDER if day in record["schedules"][collection_type]
            ]
            for collection_type in sorted(VALID_COLLECTION_TYPES)
        }
    return {
        feature_id: _canonical_record_sha256(record)
        for feature_id, record in records.items()
    }


def _json_array(raw: object, field: str, feature_id: str) -> list[object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"source database provenance {field} is invalid id={feature_id!r}"
        ) from error
    if not isinstance(value, list):
        raise RuntimeError(
            f"source database provenance {field} is not an array id={feature_id!r}"
        )
    return value


def _canonical_geometry(geometry: BaseGeometry, feature_id: str) -> str:
    if (
        geometry.geom_type not in {"LineString", "MultiLineString"}
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.length <= 0
    ):
        raise RuntimeError(f"semantic geometry is invalid id={feature_id!r}")
    return geometry.normalize().wkb_hex.lower()


def _canonical_record_sha256(record: dict[str, object]) -> str:
    encoded = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aggregate_feature_hashes(feature_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for feature_id in sorted(feature_hashes):
        digest.update(feature_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(feature_hashes[feature_id].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_tile_build_report(
    path: str | Path,
    *,
    expected_version: str,
    database: dict[str, object],
    tileset: dict[str, object],
) -> dict[str, object]:
    report = read_json_object(path, "tile build report")
    expected = {
        "version": expected_version,
        "source_database_sha256": database["sha256"],
        "source_database_version": expected_version,
        "source_block_face_count": database["block_faces"],
        "source_schedule_count": database["schedule_count"],
        "source_schedule_group_count": database["schedule_group_count"],
        "feature_count": database["block_faces"],
        "geometry_count": database["block_faces"],
        "tile_count": tileset["tile_count"],
        "tile_schema_revision": tileset["tile_schema_revision"],
        "minzoom": tileset["minzoom"],
        "maxzoom": tileset["maxzoom"],
        "maxzoom_feature_count": tileset["maxzoom_feature_count"],
        "tile_feature_count": tileset["decoded_tile_feature_count"],
        "tile_size_metrics": tileset["tile_size_metrics"],
        "tile_size_limits": tileset["tile_size_limits"],
        "sha256": tileset["sha256"],
    }
    _require_equal_fields(report, expected, "tile build report")
    report_bounds = _validated_bounds(report.get("bounds"), "tile build report bounds")
    if any(
        not math.isclose(actual, expected_value, rel_tol=0, abs_tol=1e-7)
        for actual, expected_value in zip(report_bounds, tileset["bounds"], strict=True)
    ):
        raise RuntimeError("tile build report bounds do not match MBTiles metadata")
    if report["maxzoom_feature_count"] < report["feature_count"]:
        raise RuntimeError("tile build report maxzoom coverage is below the source feature count")
    if report["tile_feature_count"] < report["maxzoom_feature_count"]:
        raise RuntimeError("tile build report total feature count is internally inconsistent")
    return report


def validate_release_bundle(release_dir: str | Path, manifest: dict[str, object] | None = None) -> dict[str, object]:
    supplied_bundle = Path(release_dir)
    if supplied_bundle.is_symlink():
        raise RuntimeError(f"release bundle must not be a symlink: {supplied_bundle}")
    bundle = supplied_bundle.resolve()
    if not bundle.is_dir():
        raise RuntimeError(f"release bundle is not a directory: {bundle}")
    manifest = manifest or read_json_object(bundle / "release_manifest.json", "release manifest")
    version = _manifest_version(manifest)
    expected_release_path = f"releases/{version}"
    if manifest.get("release_path") != expected_release_path:
        raise RuntimeError(f"release manifest release_path must be {expected_release_path!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("release manifest artifacts must be an object")
    if set(artifacts) != set(RELEASE_FILENAMES):
        raise RuntimeError(
            "release manifest has an unexpected artifact schema "
            f"missing={sorted(set(RELEASE_FILENAMES) - set(artifacts))} "
            f"unknown={sorted(set(artifacts) - set(RELEASE_FILENAMES))}"
        )
    expected_files = {"release_manifest.json", *RELEASE_FILENAMES.values()}
    actual_files = {child.name for child in bundle.iterdir()}
    if actual_files != expected_files or any(child.is_dir() for child in bundle.iterdir()):
        raise RuntimeError(
            "release bundle contains unexpected files or directories "
            f"missing={sorted(expected_files - actual_files)} "
            f"unknown={sorted(actual_files - expected_files)}"
        )
    paths: dict[str, Path] = {}
    verified_hashes: dict[str, str] = {}
    for name, filename in RELEASE_FILENAMES.items():
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict) or descriptor.get("path") != filename:
            raise RuntimeError(f"release artifact {name!r} must use path {filename!r}")
        checksum = descriptor.get("sha256")
        if not isinstance(checksum, str) or not SHA256_PATTERN.fullmatch(checksum):
            raise RuntimeError(f"release artifact {name!r} has an invalid sha256")
        supplied_candidate = bundle / filename
        if supplied_candidate.is_symlink():
            raise RuntimeError(f"release artifact {name!r} must not be a symlink")
        candidate = supplied_candidate.resolve()
        if bundle not in candidate.parents or not candidate.is_file():
            raise RuntimeError(f"release artifact {name!r} is missing or escapes its bundle")
        actual = file_sha256(candidate)
        if actual != checksum:
            raise RuntimeError(f"release artifact checksum mismatch: {name}")
        paths[name] = candidate
        verified_hashes[name] = actual

    processed_descriptor = artifacts["processed_geojson"]
    processed = validate_processed_geojson(
        paths["processed_geojson"],
        known_sha256=verified_hashes["processed_geojson"],
        expected_sha256=processed_descriptor["sha256"],
        expected_semantic_sha256=processed_descriptor.get("semantic_sha256"),
        expected_features=_nonnegative_int(processed_descriptor.get("feature_count"), "processed artifact feature_count"),
    )
    audit = validate_ingestion_audit(
        paths["ingestion_audit"],
        expected_processed_sha256=processed["sha256"],
        expected_processed_features=processed["feature_count"],
    )
    audit_hash = artifacts["ingestion_audit"]["sha256"]
    database = validate_database(
        paths["database"],
        known_sha256=verified_hashes["database"],
        expected_version=version,
        expected_processed_sha256=processed["sha256"],
        expected_processed_semantic_sha256=processed["semantic_sha256"],
        expected_processed_features=processed["feature_count"],
        expected_audit_sha256=audit_hash,
    )
    if database["block_faces"] != processed["feature_count"]:
        raise RuntimeError(
            "database/output feature count mismatch "
            f"database={database['block_faces']} processed={processed['feature_count']}"
        )
    database.update(validate_processed_database_semantics(processed, paths["database"]))
    tileset = validate_tileset(
        paths["tileset"],
        version,
        known_sha256=verified_hashes["tileset"],
        expected_database=database,
        expected_database_path=paths["database"],
    )
    tile_report = validate_tile_build_report(
        paths["tile_build_report"],
        expected_version=version,
        database=database,
        tileset=tileset,
    )
    source_report = read_json_object(paths["source_report"], "source report")
    _validate_source_report(source_report, version, artifacts, audit)
    _require_exact_fields(
        artifacts["database"],
        {
            "path": RELEASE_FILENAMES["database"],
            "sha256": database["sha256"],
            "dataset_version": version,
            "block_faces": database["block_faces"],
            "schedule_count": database["schedule_count"],
            "processed_sha256": processed["sha256"],
            "processed_semantic_sha256": processed["semantic_sha256"],
        },
        "database artifact descriptor",
    )
    _require_exact_fields(
        artifacts["tileset"],
        {
            "path": RELEASE_FILENAMES["tileset"],
            "sha256": tileset["sha256"],
            "version": version,
            "tile_schema_revision": tile_report["tile_schema_revision"],
            "feature_count": tile_report["feature_count"],
            "source_database_sha256": database["sha256"],
        },
        "tileset artifact descriptor",
    )
    _require_exact_fields(
        artifacts["ingestion_audit"],
        {
            "path": RELEASE_FILENAMES["ingestion_audit"],
            "sha256": audit_hash,
            "processed_sha256": processed["sha256"],
            "output_features": processed["feature_count"],
        },
        "ingestion audit artifact descriptor",
    )
    _require_exact_fields(
        artifacts["processed_geojson"],
        {
            "path": RELEASE_FILENAMES["processed_geojson"],
            "sha256": processed["sha256"],
            "semantic_sha256": processed["semantic_sha256"],
            "feature_count": processed["feature_count"],
        },
        "processed GeoJSON artifact descriptor",
    )
    _require_exact_fields(
        artifacts["source_dsny"],
        {
            "path": RELEASE_FILENAMES["source_dsny"],
            "sha256": artifacts["source_dsny"]["sha256"],
            "record_count": audit["frequency_rows"],
        },
        "DSNY source artifact descriptor",
    )
    _require_exact_fields(
        artifacts["source_lion"],
        {
            "path": RELEASE_FILENAMES["source_lion"],
            "sha256": artifacts["source_lion"]["sha256"],
            "record_count": audit["source_rows"],
        },
        "LION source artifact descriptor",
    )
    for artifact_name in ("tile_build_report", "source_report", "ingestion_failures"):
        _require_exact_fields(
            artifacts[artifact_name],
            {
                "path": RELEASE_FILENAMES[artifact_name],
                "sha256": artifacts[artifact_name]["sha256"],
            },
            f"{artifact_name} artifact descriptor",
        )

    counts = manifest.get("counts")
    expected_counts = {
        "raw_lion_rows": audit["source_rows"],
        "dsny_frequency_rows": audit["frequency_rows"],
        "eligible_lion_rows": audit["eligible_lion_rows"],
        "matched_sides": audit["matched"],
        "used_frequency_rows": audit["used_valid_frequency_rows"],
        "output_features": processed["feature_count"],
        "block_faces": database["block_faces"],
        "schedule_rows": database["schedule_count"],
        "schedule_groups": database["schedule_group_count"],
        "schedule_rows_by_type": database["schedule_counts"],
        "tile_features": tile_report["feature_count"],
    }
    if not isinstance(counts, dict):
        raise RuntimeError("release manifest counts must be an object")
    _require_equal_fields(counts, expected_counts, "release manifest counts")
    if manifest.get("block_faces") != database["block_faces"]:
        raise RuntimeError("release manifest block_faces does not match database")
    if manifest.get("schedule_counts") != database["schedule_counts"]:
        raise RuntimeError("release manifest schedule_counts does not match database")
    if manifest.get("database") != database:
        raise RuntimeError("release manifest database summary does not match its artifact")
    if manifest.get("tileset") != tileset:
        raise RuntimeError("release manifest tileset summary does not match its artifact")
    audit_summary = manifest.get("ingestion_audit")
    if not isinstance(audit_summary, dict):
        raise RuntimeError("release manifest ingestion_audit must be an object")
    expected_audit_summary = {
        key: value for key, value in audit.items() if key != "records"
    }
    expected_audit_summary.update(
        {
            "sha256": artifacts["ingestion_audit"]["sha256"],
            "artifact": RELEASE_FILENAMES["ingestion_audit"],
        }
    )
    _require_exact_fields(
        audit_summary,
        expected_audit_summary,
        "release manifest ingestion audit",
    )
    return {
        "manifest": manifest,
        "version": version,
        "paths": paths,
        "processed": processed,
        "audit": audit,
        "database": database,
        "tileset": tileset,
        "tile_report": tile_report,
        "source_report": source_report,
        "counts": expected_counts,
    }


def validate_regression_gates(
    counts: dict[str, object],
    current_manifest: dict[str, object] | None,
    *,
    min_lion_rows: int = 200_000,
    min_dsny_rows: int = 500,
    min_output_features: int = 100_000,
    max_drop_fraction: float = 0.10,
) -> None:
    if any(value < 0 for value in (min_lion_rows, min_dsny_rows, min_output_features)):
        raise ValueError("release count floors must be non-negative")
    if not 0 <= max_drop_fraction < 1:
        raise ValueError("max_drop_fraction must satisfy 0 <= value < 1")
    candidate = {
        "raw_lion_rows": _nonnegative_int(counts.get("raw_lion_rows"), "raw_lion_rows"),
        "dsny_frequency_rows": _nonnegative_int(counts.get("dsny_frequency_rows"), "dsny_frequency_rows"),
        "eligible_lion_rows": _nonnegative_int(counts.get("eligible_lion_rows"), "eligible_lion_rows"),
        "matched_sides": _nonnegative_int(counts.get("matched_sides"), "matched_sides"),
        "used_frequency_rows": _nonnegative_int(counts.get("used_frequency_rows"), "used_frequency_rows"),
        "output_features": _nonnegative_int(counts.get("output_features"), "output_features"),
    }
    raw_schedule_counts = counts.get("schedule_rows_by_type")
    if not isinstance(raw_schedule_counts, dict) or set(raw_schedule_counts) != VALID_COLLECTION_TYPES:
        raise RuntimeError("schedule_rows_by_type must contain every supported collection type")
    schedule_counts = {
        collection_type: _nonnegative_int(
            raw_schedule_counts[collection_type],
            f"schedule_rows_by_type.{collection_type}",
        )
        for collection_type in sorted(VALID_COLLECTION_TYPES)
    }
    floors = {
        "raw_lion_rows": min_lion_rows,
        "dsny_frequency_rows": min_dsny_rows,
        "output_features": min_output_features,
    }
    for key, floor in floors.items():
        if candidate[key] < floor:
            raise RuntimeError(f"release regression gate failed: {key}={candidate[key]} floor={floor}")
    if not isinstance(current_manifest, dict):
        return
    previous_counts = current_manifest.get("counts")
    if not isinstance(previous_counts, dict):
        return
    for key, value in candidate.items():
        previous = previous_counts.get(key)
        if not isinstance(previous, int) or isinstance(previous, bool) or previous <= 0:
            continue
        minimum = previous * (1 - max_drop_fraction)
        if value < minimum:
            drop_percent = (previous - value) * 100 / previous
            raise RuntimeError(
                f"release regression gate failed: {key} dropped {drop_percent:.2f}% "
                f"from {previous} to {value} (allowed {max_drop_fraction * 100:.2f}%)"
            )
    previous_schedule_counts = previous_counts.get("schedule_rows_by_type")
    if isinstance(previous_schedule_counts, dict):
        for collection_type, value in schedule_counts.items():
            previous = previous_schedule_counts.get(collection_type)
            if not isinstance(previous, int) or isinstance(previous, bool) or previous <= 0:
                continue
            minimum = previous * (1 - max_drop_fraction)
            if value < minimum:
                drop_percent = (previous - value) * 100 / previous
                raise RuntimeError(
                    "release regression gate failed: "
                    f"schedule_rows_by_type.{collection_type} dropped {drop_percent:.2f}% "
                    f"from {previous} to {value} (allowed {max_drop_fraction * 100:.2f}%)"
                )


@contextmanager
def _promotion_lock(data_root: Path):
    """Hold a crash-released, cross-process lock for pointer/history changes."""

    lock_path = data_root / ".release-promotion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_release(
    staged_release: str | Path,
    manifest_path: str | Path,
    *,
    retention: int = 2,
    regression_gate: dict[str, object] | None = None,
) -> dict[str, object]:
    """Install a validated immutable release, then atomically switch the pointer."""

    if retention < 2:
        raise ValueError("release retention must be at least 2")
    supplied_staging = Path(staged_release)
    if supplied_staging.is_symlink():
        raise RuntimeError(f"release bundle must not be a symlink: {supplied_staging}")
    staging = supplied_staging.resolve()
    release_manifest = read_json_object(
        staging / "release_manifest.json", "release manifest"
    )
    version = _manifest_version(release_manifest)
    pointer = Path(manifest_path).resolve()
    data_root = pointer.parent
    releases_root = (data_root / "releases").resolve()
    if releases_root != data_root and data_root not in releases_root.parents:
        raise RuntimeError("release directory escapes the manifest data root")
    releases_root.mkdir(parents=True, exist_ok=True)
    with _promotion_lock(data_root):
        final_release = (releases_root / version).resolve()
        if final_release.parent != releases_root or not VERSION_PATTERN.fullmatch(version):
            raise RuntimeError("release destination is unsafe")
        temporary: Path | None = None
        try:
            if final_release.exists():
                validated = validate_release_bundle(final_release)
                if validated["manifest"] != release_manifest:
                    raise RuntimeError(
                        f"immutable release already exists with different contents: {version}"
                    )
            else:
                temporary = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=releases_root))
                for child in staging.iterdir():
                    if child.is_symlink():
                        raise RuntimeError(f"release bundle contains a symlink: {child.name}")
                    destination = temporary / child.name
                    if child.is_dir():
                        shutil.copytree(child, destination)
                    else:
                        shutil.copy2(child, destination)
                validated = validate_release_bundle(temporary)
                if validated["manifest"] != release_manifest:
                    raise RuntimeError("staging release changed while it was being copied")

            current = _read_current_manifest(pointer)
            if regression_gate is not None:
                validate_regression_gates(
                    validated["counts"],
                    current,
                    **regression_gate,
                )
            if temporary is not None:
                os.replace(temporary, final_release)
                temporary = None
            release_manifest["previous_releases"] = _previous_release_entries(
                current,
                new_version=version,
                retention=retention,
            )
            atomic_json(pointer, release_manifest)
            try:
                _cleanup_old_releases(releases_root, release_manifest, retention)
            except OSError:
                # Cleanup is deliberately best-effort after commit.  A deletion
                # error must not make operators think the switched release failed.
                LOGGER.exception("Could not remove one or more expired releases")
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
    return release_manifest


def activate_release(
    version: str,
    manifest_path: str | Path,
    *,
    retention: int = 2,
) -> dict[str, object]:
    """Revalidate an installed release and atomically make it current."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("release version is invalid")
    pointer = Path(manifest_path).resolve()
    release_dir = (pointer.parent / "releases" / version).resolve()
    if release_dir.parent != (pointer.parent / "releases").resolve():
        raise RuntimeError("release activation path is unsafe")
    return publish_release(release_dir, pointer, retention=retention)


def _read_current_manifest(path: Path) -> dict[str, object] | None:
    current = read_current_release(path)
    return current.manifest if current is not None else None


def _previous_release_entries(
    current: dict[str, object] | None,
    *,
    new_version: str,
    retention: int,
) -> list[dict[str, object]]:
    if not current or current.get("dataset_version") == new_version:
        return [] if not current else list(current.get("previous_releases", []))[: retention - 1]
    candidates = [_release_history_entry(current)]
    raw_previous = current.get("previous_releases", [])
    if isinstance(raw_previous, list):
        candidates.extend(item for item in raw_previous if isinstance(item, dict))
    selected: list[dict[str, object]] = []
    seen = {new_version}
    for candidate in candidates:
        version = candidate.get("dataset_version")
        if not isinstance(version, str) or version in seen:
            continue
        seen.add(version)
        selected.append(candidate)
        if len(selected) >= retention - 1:
            break
    return selected


def _release_history_entry(manifest: dict[str, object]) -> dict[str, object]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("tileset"), dict):
        raise RuntimeError("current manifest cannot be retained safely")
    return {
        "dataset_version": manifest["dataset_version"],
        "release_path": manifest["release_path"],
        "artifacts": {"tileset": dict(artifacts["tileset"])},
    }


def _cleanup_old_releases(releases_root: Path, manifest: dict[str, object], retention: int) -> None:
    keep = {str(manifest["dataset_version"])}
    keep.update(
        str(item.get("dataset_version"))
        for item in manifest.get("previous_releases", [])
        if isinstance(item, dict)
    )
    if len(keep) > retention:
        raise RuntimeError("committed release history exceeds configured retention")
    for child in releases_root.iterdir():
        if not child.is_dir() or child.is_symlink() or child.name in keep:
            continue
        resolved = child.resolve()
        if resolved.parent != releases_root or not VERSION_PATTERN.fullmatch(child.name):
            LOGGER.warning("Skipping unsafe or unrecognized release directory path=%s", child)
            continue
        try:
            validate_release_bundle(resolved)
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("Skipping cleanup of an unvalidated release directory path=%s", child)
            continue
        shutil.rmtree(resolved)
        LOGGER.info("Removed expired validated release version=%s", child.name)


def _validate_source_report(
    report: dict[str, object],
    version: str,
    artifacts: dict[str, object],
    audit: dict[str, object],
) -> None:
    if report.get("report_version") != 1 or report.get("dataset_version") != version:
        raise RuntimeError("source report version binding is invalid")
    sources = report.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError("source report sources must be an object")
    for source, artifact_name, expected_count in (
        ("dsny", "source_dsny", audit["frequency_rows"]),
        ("lion", "source_lion", audit["source_rows"]),
    ):
        details = sources.get(source)
        if not isinstance(details, dict):
            raise RuntimeError(f"source report is missing {source}")
        if details.get("sha256") != artifacts[artifact_name]["sha256"]:
            raise RuntimeError(f"source report {source} checksum does not match its artifact")
        if details.get("record_count") != expected_count:
            raise RuntimeError(f"source report {source} count does not match ingestion audit")


def _manifest_version(manifest: dict[str, object]) -> str:
    if (
        type(manifest.get("manifest_version")) is not int
        or manifest.get("manifest_version") != MANIFEST_VERSION
    ):
        raise RuntimeError(f"release manifest version must be {MANIFEST_VERSION}")
    version = manifest.get("dataset_version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError("release manifest dataset_version is invalid")
    return version


def _metadata_int(metadata: dict[str, str], key: str) -> int:
    try:
        value = int(metadata[key])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"staged tileset metadata {key!r} must be an integer") from error
    return _nonnegative_int(value, f"staged tileset metadata {key}")


def _validated_bounds(value: object, label: str) -> list[float]:
    raw_values: object = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_values, (list, tuple)) or len(raw_values) != 4:
        raise RuntimeError(f"{label} must contain four coordinates")
    try:
        bounds = [float(item) for item in raw_values]
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} contains a non-numeric coordinate") from error
    west, south, east, north = bounds
    if (
        not all(math.isfinite(item) for item in bounds)
        or not -180 <= west < east <= 180
        or not -90 <= south < north <= 90
    ):
        raise RuntimeError(f"{label} is outside valid ordered geographic bounds")
    return bounds


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{label} must be a non-negative integer")
    return value


def _count_map(value: object, label: str, required_keys: set[str]) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    if set(value) != required_keys:
        raise RuntimeError(
            f"{label} has an unexpected schema "
            f"missing={sorted(required_keys - set(value))} "
            f"unknown={sorted(set(value) - required_keys)}"
        )
    return {key: _nonnegative_int(item, f"{label}.{key}") for key, item in value.items()}


def _reconciliation(value: object, expected: int, classified: int, label: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"ingestion audit {label} reconciliation must be an object")
    expected_value = {
        "expected": expected,
        "classified": classified,
        "difference": classified - expected,
        "passed": classified == expected,
    }
    _require_equal_fields(value, expected_value, f"ingestion audit {label} reconciliation")


def _require_equal_fields(actual: dict[str, object], expected: dict[str, object], label: str) -> None:
    for key, value in expected.items():
        if not _same_value(actual.get(key), value):
            raise RuntimeError(
                f"{label} mismatch key={key} expected={value!r} actual={actual.get(key)!r}"
            )


def _require_exact_fields(actual: dict[str, object], expected: dict[str, object], label: str) -> None:
    if set(actual) != set(expected):
        raise RuntimeError(
            f"{label} has an unexpected schema "
            f"missing={sorted(set(expected) - set(actual))} "
            f"unknown={sorted(set(actual) - set(expected))}"
        )
    _require_equal_fields(actual, expected, label)


def _same_value(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _same_value(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_value(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected)
        )
    return actual == expected
