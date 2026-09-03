import logging
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pathlib import Path

from .config import (
    DATA_MANIFEST_PATH,
    HEALTH_SYNC_HASH_MAX_BYTES,
)
from .database import DATABASE_SCHEMA_REVISION
from .releases import (
    CurrentRelease,
    ReleaseManifestError,
    VERSION_PATTERN,
    artifact_checksum_status,
    read_current_release,
    release_checksum_status,
    tileset_for_version,
)
from .tiles import TILE_SCHEMA_REVISION, VECTOR_TILE_MEDIA_TYPE, read_metadata, read_tile

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


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
        "unknown_source_layer": None,
        "unknown_minzoom": None,
        "minzoom": None,
        "maxzoom": None,
        "bounds": None,
        "data_updated": None,
    }


def _expected_tile_schema_revision(manifest: dict[str, object]) -> int | None:
    """Resolve the release contract only when this runtime can consume it."""

    artifacts = manifest.get("artifacts")
    tileset_descriptor = (
        artifacts.get("tileset") if isinstance(artifacts, dict) else None
    )
    declared = (
        tileset_descriptor.get("tile_schema_revision")
        if isinstance(tileset_descriptor, dict)
        else None
    )
    if (
        not isinstance(declared, int)
        or isinstance(declared, bool)
        or declared != TILE_SCHEMA_REVISION
    ):
        return None
    return declared


@router.get("/map-config")
def map_config() -> JSONResponse:
    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    if release is None:
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    checksum_status = _current_release_integrity(release)
    if checksum_status != "verified":
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    tileset_path = release.tileset_path
    try:
        metadata = read_metadata(tileset_path)
    except FileNotFoundError:
        LOGGER.info("Vector tileset is not published yet path=%s", tileset_path)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.warning("Vector tileset is unavailable path=%s", tileset_path, exc_info=True)
        return JSONResponse(_unavailable_map_config(), headers={"Cache-Control": "no-cache"})
    expected_tile_revision = _expected_tile_schema_revision(release.manifest)
    if (
        metadata.version != release.dataset_version
        or expected_tile_revision is None
        or metadata.tile_schema_revision != expected_tile_revision
    ):
        LOGGER.error("Committed tileset does not match the runtime release contract")
        return JSONResponse(
            _unavailable_map_config(),
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse(
        {
            "available": True,
            "version": metadata.version,
            "tile_schema_revision": metadata.tile_schema_revision,
            "tiles_url": f"/api/tiles/{metadata.version}/{{z}}/{{x}}/{{y}}.pbf",
            "source_layer": metadata.source_layer,
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
        candidates = ()
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

    try:
        release = read_current_release(DATA_MANIFEST_PATH)
    except ReleaseManifestError:
        LOGGER.exception("Committed dataset manifest is invalid path=%s", DATA_MANIFEST_PATH)
        raise HTTPException(status_code=503, detail="Committed data release is invalid") from None
    if release is None:
        raise HTTPException(status_code=503, detail="No committed data release")
    metadata = release.manifest
    database_path = release.database_path
    tileset_path = release.tileset_path
    _require_verified_current_release(release)
    try:
        tile_metadata = read_metadata(tileset_path)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
        LOGGER.exception("Health check could not validate committed tileset metadata")
        raise HTTPException(status_code=503, detail="Committed tileset metadata is invalid") from None
    expected_tile_revision = _expected_tile_schema_revision(release.manifest)
    if (
        expected_tile_revision is None
        or tile_metadata.version != release.dataset_version
        or tile_metadata.tile_schema_revision != expected_tile_revision
    ):
        raise HTTPException(status_code=503, detail="Committed tileset release binding is invalid")
    try:
        with closing(_read_only_database(database_path)) as connection:
            connection.execute("SELECT 1 FROM block_faces LIMIT 1").fetchone()
            database_revision = connection.execute(
                "SELECT value FROM dataset_metadata WHERE key = 'database_schema_revision'"
            ).fetchone()
            if database_revision is None or database_revision[0] != str(DATABASE_SCHEMA_REVISION):
                raise HTTPException(status_code=503, detail="Database schema revision is invalid")
            count = metadata.get("block_faces")
            schedule_counts = metadata.get("schedule_counts")
            if type(count) is not int or not isinstance(schedule_counts, dict):
                raise HTTPException(status_code=503, detail="Committed release summary is invalid")
            schedule_state_counts: dict[str, dict[str, int]] = {}
            for collection_type, state, state_count in connection.execute(
                """SELECT collection_type, state, COUNT(*)
                   FROM block_face_collection_states
                   GROUP BY collection_type, state"""
            ):
                schedule_state_counts.setdefault(str(collection_type), {})[str(state)] = int(state_count)
            policy_conflicts = int(connection.execute(
                "SELECT COUNT(*) FROM block_face_collection_states WHERE source_policy_conflict = 1"
            ).fetchone()[0])
            unresolved_counts = {
                str(reason): int(reason_count)
                for reason, reason_count in connection.execute(
                    "SELECT reason_code, COUNT(*) FROM unknown_block_faces GROUP BY reason_code"
                )
            }
    except sqlite3.Error:
        LOGGER.exception("Health check could not inspect the local database")
        raise HTTPException(status_code=503, detail="Committed database is invalid") from None
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
        "quality_status": quality_record.get("quality_status", "verified"),
        "map_available": tileset_path.is_file(),
        "artifact_integrity": "verified",
    }


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}
