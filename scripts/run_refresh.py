"""Download official sources, build a staged citywide dataset, and promote it atomically."""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.load_processed import load_prepared_payload
from scripts.release_validation import (
    EXPECTED_TILE_SCHEMA_REVISION,
    PrevalidatedReleaseComponents,
    RELEASE_FILENAMES,
    atomic_json,
    file_sha256,
    publish_release,
    read_json_object,
    validate_database,
    validate_ingestion_audit,
    validate_and_prepare_processed_geojson,
    validate_processed_database_semantics,
    validate_release_bundle,
    validate_regression_gates,
    validate_tile_build_report,
    validate_tileset,
)
from backend.app.database import DATABASE_SCHEMA_REVISION
from backend.app.releases import MANIFEST_VERSION, read_current_release
from scripts.recovery_shadow import (
    GEOMETRY_RULE_VERSION,
    identity_shadow_report,
    metadata_release_identifier,
    source_release_identifier,
)

LOGGER = logging.getLogger("run_refresh")
DSNY_LAYER = "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0"
DSNY_QUERY = f"{DSNY_LAYER}/query"
LION_URL = "https://data.cityofnewyork.us/download/2v4z-66xt/application/zip"
PAD_URL = "https://data.cityofnewyork.us/download/bc8t-ecyu/application/zip"
ADDRESSPOINT_METADATA_URL = "https://data.cityofnewyork.us/api/views/6xyb-j5pk"
LION_METADATA_URL = "https://data.cityofnewyork.us/api/views/2v4z-66xt"
PAD_METADATA_URL = "https://data.cityofnewyork.us/api/views/bc8t-ecyu"
CSCL_METADATA_URL = "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/cscl/FeatureServer/1"
USER_AGENT = "nyc-sanitation-map/2.0"
DOWNLOAD_PROGRESS_BYTES = 16 * 1024 * 1024
DOWNLOAD_PROGRESS_SECONDS = 30.0
REFRESH_FINGERPRINT_VERSION = 1
PROCESSING_CODE_PATHS = (
    "backend/app/database.py",
    "backend/app/releases.py",
    "scripts/build_pilot.py",
    "scripts/build_tiles.py",
    "scripts/load_processed.py",
    "scripts/recovery_shadow.py",
    "scripts/release_validation.py",
    "scripts/run_refresh.py",
)
PROCESSING_DISTRIBUTIONS = (
    "geopandas",
    "httpx",
    "mapbox-vector-tile",
    "numpy",
    "pandas",
    "pyogrio",
    "pyproj",
    "shapely",
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def processing_fingerprint(
    *,
    tile_minzoom: int,
    tile_maxzoom: int,
    side_offset_feet: float,
    tile_max_compressed_bytes: int | None = None,
    tile_max_uncompressed_bytes: int | None = None,
) -> dict[str, object]:
    """Bind a release to every local input that can change generated data."""

    repository = Path(__file__).resolve().parents[1]
    if tile_max_compressed_bytes is None:
        tile_max_compressed_bytes = int(
            os.getenv("TILE_MAX_COMPRESSED_BYTES", "1572864")
        )
    if tile_max_uncompressed_bytes is None:
        tile_max_uncompressed_bytes = int(
            os.getenv("TILE_MAX_UNCOMPRESSED_BYTES", "6291456")
        )
    code_sha256 = {
        relative: file_sha256(repository / relative)
        for relative in PROCESSING_CODE_PATHS
    }
    runtime_versions: dict[str, str] = {}
    for distribution in PROCESSING_DISTRIBUTIONS:
        try:
            runtime_versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            runtime_versions[distribution] = "unavailable"
    inputs: dict[str, object] = {
        "configuration": {
            "side_offset_feet": side_offset_feet,
            "tile_minzoom": tile_minzoom,
            "tile_maxzoom": tile_maxzoom,
            "tile_max_compressed_bytes": tile_max_compressed_bytes,
            "tile_max_uncompressed_bytes": tile_max_uncompressed_bytes,
        },
        "schemas": {
            "manifest_version": MANIFEST_VERSION,
            "processed_geojson_revision": 3,
            "ingestion_audit_version": 3,
            "database_schema_revision": DATABASE_SCHEMA_REVISION,
            "tile_schema_revision": EXPECTED_TILE_SCHEMA_REVISION,
        },
        "code_sha256": code_sha256,
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "sqlite_version": sqlite3.sqlite_version,
            "distributions": runtime_versions,
        },
    }
    return {
        "fingerprint_version": REFRESH_FINGERPRINT_VERSION,
        "sha256": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def source_fingerprint(
    *,
    input_sha256: dict[str, str],
    dsny_source_audit: dict[str, object],
    lion_source_audit: dict[str, object],
    pad_source_audit: dict[str, object],
    lion_release: str | None,
    pad_release: str | None,
    lion_catalog_release: str | None,
    pad_catalog_release: str | None,
    lion_metadata: dict[str, object],
    pad_metadata: dict[str, object],
    addresspoint_metadata_before: dict[str, object],
    addresspoint_metadata_after: dict[str, object],
    cscl_metadata_before: dict[str, object],
    cscl_metadata_after: dict[str, object],
) -> dict[str, object]:
    """Return the exact public-source content and revision identity."""

    inputs: dict[str, object] = {
        "input_sha256": dict(input_sha256),
        "dsny": dict(dsny_source_audit),
        "lion": {
            "transfer": {
                key: lion_source_audit.get(key)
                for key in ("bytes", "etag", "last_modified")
            },
            "archive_release": lion_release,
            "catalog_release": lion_catalog_release,
            "metadata": lion_metadata,
        },
        "pad": {
            "transfer": {
                key: pad_source_audit.get(key)
                for key in ("bytes", "etag", "last_modified")
            },
            "archive_release": pad_release,
            "catalog_release": pad_catalog_release,
            "metadata": pad_metadata,
        },
        "addresspoint": {
            "metadata_before": addresspoint_metadata_before,
            "metadata_after": addresspoint_metadata_after,
        },
        "cscl": {
            "metadata_before": cscl_metadata_before,
            "metadata_after": cscl_metadata_after,
        },
    }
    return {
        "fingerprint_version": REFRESH_FINGERPRINT_VERSION,
        "sha256": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def refresh_fingerprint(
    processing: dict[str, object],
    sources: dict[str, object],
) -> dict[str, object]:
    inputs = {"processing": processing, "sources": sources}
    return {
        "fingerprint_version": REFRESH_FINGERPRINT_VERSION,
        "sha256": _canonical_sha256(inputs),
        **inputs,
    }


def unchanged_release(
    manifest_path: Path,
    candidate_fingerprint: dict[str, object],
    *,
    regression_gate: dict[str, object],
) -> dict[str, object] | None:
    """Return the committed manifest only when it is safe to reuse exactly."""

    current_release = read_current_release(manifest_path)
    if current_release is None:
        return None
    current = current_release.manifest
    if current.get("refresh_fingerprint") != candidate_fingerprint:
        return None

    source_inputs = candidate_fingerprint.get("sources")
    if not isinstance(source_inputs, dict):
        return None
    raw_source_inputs = source_inputs.get("inputs")
    if not isinstance(raw_source_inputs, dict):
        return None
    input_sha256 = raw_source_inputs.get("input_sha256")
    if current.get("input_sha256") != input_sha256 or not isinstance(input_sha256, dict):
        return None
    artifacts = current.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    for source, artifact_name in (
        ("dsny", "source_dsny"),
        ("lion", "source_lion"),
        ("pad", "source_pad"),
    ):
        descriptor = artifacts.get(artifact_name)
        if not isinstance(descriptor, dict) or descriptor.get("sha256") != input_sha256.get(source):
            return None

    version = current.get("dataset_version")
    release_path = current.get("release_path")
    if not isinstance(version, str) or release_path != f"releases/{version}":
        return None
    release_candidate = manifest_path.parent / "releases" / version
    if release_candidate.is_symlink() or not release_candidate.is_dir():
        return None
    release_dir = release_candidate.resolve()
    if release_dir.parent != (manifest_path.parent / "releases").resolve():
        return None
    installed_manifest = release_dir / "release_manifest.json"
    if installed_manifest.is_symlink() or not installed_manifest.is_file():
        return None
    try:
        installed = read_json_object(installed_manifest, "installed release manifest")
    except RuntimeError:
        return None
    # Promotion adds release history only to the atomic pointer. Everything
    # else in the installed manifest is immutable and must still match it.
    installed_binding = dict(installed)
    installed_binding.pop("previous_releases", None)
    current_binding = dict(current)
    current_binding.pop("previous_releases", None)
    if installed_binding != current_binding:
        return None
    if set(artifacts) != set(RELEASE_FILENAMES):
        return None
    for name, filename in RELEASE_FILENAMES.items():
        descriptor = artifacts.get(name)
        artifact = release_dir / filename
        expected_sha256 = descriptor.get("sha256") if isinstance(descriptor, dict) else None
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("path") != filename
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or artifact.is_symlink()
            or not artifact.is_file()
        ):
            return None
        try:
            if file_sha256(artifact) != expected_sha256:
                return None
        except OSError:
            return None
    counts = current.get("counts")
    if not isinstance(counts, dict):
        return None
    validate_regression_gates(counts, current, **regression_gate)
    return current


def _format_bytes(value: int) -> str:
    """Return a compact, operator-friendly byte count for refresh logs."""

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _arcgis_json(client: httpx.Client, url: str, params: dict[str, object]) -> dict[str, object]:
    response = client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"ArcGIS returned a non-object response url={url}")
    if payload.get("error"):
        raise RuntimeError(f"ArcGIS returned an error url={url} error={payload['error']!r}")
    return payload


def _arcgis_post_json(client: httpx.Client, url: str, params: dict[str, object]) -> dict[str, object]:
    """Submit large ArcGIS queries without putting hundreds of IDs in the URL."""

    response = client.post(url, data=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"ArcGIS returned a non-object response url={url}")
    if payload.get("error"):
        raise RuntimeError(f"ArcGIS returned an error url={url} error={payload['error']!r}")
    return payload


def _feature_object_id(feature: dict[str, object], object_id_field: str) -> object:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("DSNY feature is missing a properties object")
    if object_id_field in properties:
        return properties[object_id_field]
    expected = object_id_field.casefold()
    for key, value in properties.items():
        if str(key).casefold() == expected:
            return value
    raise RuntimeError(f"DSNY feature is missing object ID field {object_id_field}")


def _object_id_field(metadata: dict[str, object]) -> str | None:
    value = metadata.get("objectIdField")
    if isinstance(value, str) and value:
        return value
    fields = metadata.get("fields")
    if isinstance(fields, list):
        inferred = next(
            (
                field.get("name")
                for field in fields
                if isinstance(field, dict) and field.get("type") == "esriFieldTypeOID"
            ),
            None,
        )
        return inferred if isinstance(inferred, str) and inferred else None
    return None


def download_dsny(output: Path, client: httpx.Client | None = None) -> dict[str, object]:
    """Download every advertised ArcGIS object ID and prove none were skipped.

    Offset pagination can stop early without an error when a service changes its
    transfer limit.  Fetching the authoritative ID set first makes omissions,
    duplicates, and unexpected records explicit refresh failures.
    """

    if client is None:
        with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}) as owned_client:
            return download_dsny(output, owned_client)

    metadata = _arcgis_json(client, DSNY_LAYER, {"f": "json"})
    object_id_field = _object_id_field(metadata)
    if not isinstance(object_id_field, str) or not object_id_field:
        raise RuntimeError("DSNY layer metadata does not declare an object ID field")

    count_payload = _arcgis_json(
        client,
        DSNY_QUERY,
        {"where": "1=1", "returnCountOnly": "true", "f": "json"},
    )
    id_payload = _arcgis_json(
        client,
        DSNY_QUERY,
        {"where": "1=1", "returnIdsOnly": "true", "f": "json"},
    )
    expected_count = count_payload.get("count")
    object_ids = id_payload.get("objectIds")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count <= 0:
        raise RuntimeError(f"DSNY source advertised an invalid count: {expected_count!r}")
    if not isinstance(object_ids, list) or not object_ids:
        raise RuntimeError("DSNY source returned no object IDs")
    normalized_ids = [str(value) for value in object_ids]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise RuntimeError("DSNY source returned duplicate object IDs")
    if expected_count != len(object_ids):
        raise RuntimeError(
            f"DSNY count/ID mismatch advertised_count={expected_count} ids={len(object_ids)}"
        )

    advertised_limit = metadata.get("maxRecordCount", 2000)
    batch_size = min(max(int(advertised_limit), 1), 2000)
    ordered_ids = sorted(normalized_ids)
    features_by_id: dict[str, dict[str, object]] = {}
    downloaded_ids: set[str] = set()
    for start in range(0, len(ordered_ids), batch_size):
        batch = ordered_ids[start : start + batch_size]
        payload = _arcgis_post_json(
            client,
            DSNY_QUERY,
            {
                "objectIds": ",".join(str(value) for value in batch),
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "geojson",
            },
        )
        page = payload.get("features")
        if not isinstance(page, list):
            raise RuntimeError("DSNY feature response is missing its features array")
        expected_batch = {str(value) for value in batch}
        received_batch: set[str] = set()
        for raw_feature in page:
            if not isinstance(raw_feature, dict):
                raise RuntimeError("DSNY feature response contains a non-object feature")
            object_id = str(_feature_object_id(raw_feature, object_id_field))
            if object_id in received_batch or object_id in downloaded_ids:
                raise RuntimeError(f"DSNY download returned duplicate object ID {object_id}")
            received_batch.add(object_id)
            features_by_id[object_id] = raw_feature
        missing = expected_batch - received_batch
        unexpected = received_batch - expected_batch
        if missing or unexpected:
            raise RuntimeError(
                "DSNY batch did not match requested IDs "
                f"missing={sorted(missing)[:10]} unexpected={sorted(unexpected)[:10]}"
            )
        downloaded_ids.update(received_batch)
        LOGGER.info("Downloaded verified DSNY features=%s/%s", len(features_by_id), expected_count)

    missing_ids = set(normalized_ids) - downloaded_ids
    if missing_ids or len(features_by_id) != expected_count:
        raise RuntimeError(
            f"DSNY download incomplete features={len(features_by_id)} expected={expected_count} "
            f"missing_ids={sorted(missing_ids)[:10]}"
        )

    # Prove the service did not change while its batches were being fetched.
    # A complete set assembled across two source revisions is not a coherent
    # snapshot even if every initially advertised ID happened to arrive.
    final_metadata = _arcgis_json(client, DSNY_LAYER, {"f": "json"})
    final_count_payload = _arcgis_json(
        client,
        DSNY_QUERY,
        {"where": "1=1", "returnCountOnly": "true", "f": "json"},
    )
    final_id_payload = _arcgis_json(
        client,
        DSNY_QUERY,
        {"where": "1=1", "returnIdsOnly": "true", "f": "json"},
    )
    final_count = final_count_payload.get("count")
    final_ids = final_id_payload.get("objectIds")
    initial_editing = metadata.get("editingInfo")
    final_editing = final_metadata.get("editingInfo")
    initial_edit_ms = (
        initial_editing.get("lastEditDate") if isinstance(initial_editing, dict) else None
    )
    final_edit_ms = final_editing.get("lastEditDate") if isinstance(final_editing, dict) else None
    if (
        not isinstance(initial_edit_ms, int)
        or isinstance(initial_edit_ms, bool)
        or initial_edit_ms < 0
        or not isinstance(final_edit_ms, int)
        or isinstance(final_edit_ms, bool)
        or final_edit_ms < 0
        or final_count != expected_count
        or not isinstance(final_ids, list)
        or {str(value) for value in final_ids} != set(normalized_ids)
        or len(final_ids) != len(normalized_ids)
        or final_edit_ms != initial_edit_ms
        or _object_id_field(final_metadata) != object_id_field
        or final_metadata.get("currentVersion") != metadata.get("currentVersion")
    ):
        raise RuntimeError(
            "DSNY source changed during snapshot "
            f"initial_count={expected_count} final_count={final_count} "
            f"initial_edit_ms={initial_edit_ms} final_edit_ms={final_edit_ms}"
        )
    # ArcGIS does not promise result ordering. Canonical OBJECTID order keeps
    # source bytes and downstream frequency-row provenance reproducible.
    features = [features_by_id[object_id] for object_id in ordered_ids]
    output.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "record_count": len(features),
        "object_id_field": object_id_field,
        "max_record_count": batch_size,
        "service_last_edit_ms": initial_edit_ms,
        "service_version": metadata.get("currentVersion"),
    }


def download_file(url: str, output: Path) -> dict[str, object]:
    with httpx.stream("GET", url, timeout=180, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        advertised_size = response.headers.get("content-length")
        expected_size = int(advertised_size) if advertised_size is not None else None
        LOGGER.info(
            "Downloading archive destination=%s expected_bytes=%s",
            output.name,
            _format_bytes(expected_size) if expected_size is not None else "unknown",
        )
        downloaded = 0
        started = monotonic()
        last_progress_at = started
        last_progress_bytes = 0
        with output.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                handle.write(chunk)
                downloaded += len(chunk)
                now = monotonic()
                if (
                    downloaded - last_progress_bytes >= DOWNLOAD_PROGRESS_BYTES
                    or now - last_progress_at >= DOWNLOAD_PROGRESS_SECONDS
                ):
                    percentage = (
                        f"{downloaded / expected_size:.1%}"
                        if expected_size
                        else "unknown"
                    )
                    elapsed = max(now - started, 0.001)
                    LOGGER.info(
                        "Archive download progress destination=%s bytes=%s expected=%s percent=%s rate_mib_s=%.2f elapsed_s=%.0f",
                        output.name,
                        _format_bytes(downloaded),
                        _format_bytes(expected_size) if expected_size is not None else "unknown",
                        percentage,
                        downloaded / elapsed / (1024 * 1024),
                        elapsed,
                    )
                    last_progress_at = now
                    last_progress_bytes = downloaded
        actual_size = output.stat().st_size
        if expected_size is not None and expected_size != actual_size:
            raise RuntimeError(
                f"download size mismatch url={url} expected={expected_size} actual={actual_size}"
            )
        LOGGER.info(
            "Completed archive download destination=%s bytes=%s elapsed_s=%.1f",
            output.name,
            _format_bytes(actual_size),
            monotonic() - started,
        )
        return {
            "bytes": actual_size,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
        }


def _release_version(processed_at: datetime, processed_sha256: str) -> str:
    return f"{processed_at:%Y%m%dT%H%M%S%fZ}-{processed_sha256[:12]}"


def _bind_database_metadata(
    database: Path,
    *,
    dataset_version: str,
    processed_sha256: str,
    processed_semantic_sha256: str,
    processed_features: int,
    audit_sha256: str,
) -> None:
    values = {
        "database_schema_revision": str(DATABASE_SCHEMA_REVISION),
        "dataset_version": dataset_version,
        "processed_sha256": processed_sha256,
        "processed_semantic_sha256": processed_semantic_sha256,
        "processed_feature_count": str(processed_features),
        "ingestion_audit_sha256": audit_sha256,
    }
    with closing(sqlite3.connect(database)) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO dataset_metadata(key, value) VALUES (?, ?)",
            values.items(),
        )
        connection.commit()


def _artifact(path: Path, **metadata: object) -> dict[str, object]:
    return {"path": path.name, "sha256": file_sha256(path), **metadata}


def _remote_metadata(url: str) -> dict[str, object]:
    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, params={"f": "json"} if "FeatureServer" in url else None)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"source metadata is not an object url={url}")
    return {
        key: payload.get(key)
        for key in (
            "id", "name", "description", "blobFilename", "blobFileSize",
            "rowsUpdatedAt", "dataUpdatedAt", "lastEditDate", "editingInfo",
        )
        if key in payload
    }


def download_cscl_subset(physical_ids: set[str], output: Path) -> dict[str, object]:
    """Fetch exactly the CSCL rows for shadow candidates and verify the OID set."""

    numeric_ids = sorted({int(value) for value in physical_ids if value.isdigit()})
    if not numeric_ids:
        atomic_json(output, {"type": "FeatureCollection", "features": []})
        return {
            "requested_physical_ids": 0,
            "returned_object_ids": 0,
            "returned_features": 0,
            "object_id_set_verified": True,
            "source_metadata_stable": True,
        }
    query_url = f"{CSCL_METADATA_URL}/query"
    with httpx.Client(timeout=180, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        before = _arcgis_json(client, CSCL_METADATA_URL, {"f": "json"})
        oid_field = _object_id_field(before)
        if not oid_field:
            raise RuntimeError("CSCL metadata does not declare an object ID field")
        object_id_set: set[int] = set()
        for physical_offset in range(0, len(numeric_ids), 500):
            physical_batch = numeric_ids[physical_offset:physical_offset + 500]
            where = f"PHYSICALID IN ({','.join(str(value) for value in physical_batch)})"
            ids_payload = _arcgis_post_json(
                client, query_url, {"where": where, "returnIdsOnly": "true", "f": "json"}
            )
            batch_ids = ids_payload.get("objectIds")
            if not isinstance(batch_ids, list) or any(not isinstance(value, int) for value in batch_ids):
                raise RuntimeError("CSCL object-ID query returned an invalid ID set")
            if object_id_set & set(batch_ids):
                raise RuntimeError("CSCL object-ID batches returned duplicate IDs")
            object_id_set.update(batch_ids)
        object_ids = sorted(object_id_set)
        expected_oids = set(object_ids)
        features: list[dict[str, object]] = []
        batch_size = int(before.get("maxRecordCount") or 1000)
        for offset in range(0, len(object_ids), batch_size):
            batch = object_ids[offset:offset + batch_size]
            payload = _arcgis_post_json(
                client,
                query_url,
                {
                    "objectIds": ",".join(str(value) for value in batch),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": 4326,
                    "f": "geojson",
                },
            )
            batch_features = payload.get("features")
            if not isinstance(batch_features, list):
                raise RuntimeError("CSCL feature query returned an invalid feature array")
            features.extend(batch_features)
        returned_oids = {
            int(feature["properties"][oid_field])
            for feature in features
            if isinstance(feature, dict)
            and isinstance(feature.get("properties"), dict)
            and feature["properties"].get(oid_field) is not None
        }
        if returned_oids != expected_oids or len(features) != len(returned_oids):
            raise RuntimeError(
                "CSCL subset lost, duplicated, or invented object IDs "
                f"expected={len(expected_oids)} returned={len(returned_oids)} features={len(features)}"
            )
        after = _arcgis_json(client, CSCL_METADATA_URL, {"f": "json"})
    before_editing = before.get("editingInfo")
    after_editing = after.get("editingInfo")
    if before_editing != after_editing:
        raise RuntimeError("CSCL source metadata changed during subset retrieval")
    returned_physical_ids = {
        int(feature["properties"]["PHYSICALID"])
        for feature in features
    }
    if not returned_physical_ids.issubset(set(numeric_ids)):
        raise RuntimeError("CSCL subset returned an unrequested PHYSICALID")
    atomic_json(output, {"type": "FeatureCollection", "features": features})
    return {
        "requested_physical_ids": len(numeric_ids),
        "returned_physical_ids": len(returned_physical_ids),
        "returned_object_ids": len(returned_oids),
        "returned_features": len(features),
        "object_id_field": oid_field,
        "object_id_set_verified": True,
        "source_metadata_stable": True,
        "missing_physical_ids": sorted(set(numeric_ids) - returned_physical_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--manifest", type=Path, default=Path(os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")))
    parser.add_argument("--tile-minzoom", type=int, default=int(os.getenv("TILE_MIN_ZOOM", "11")))
    parser.add_argument("--tile-maxzoom", type=int, default=int(os.getenv("TILE_MAX_ZOOM", "16")))
    parser.add_argument("--side-offset-feet", type=float, default=25.0)
    parser.add_argument(
        "--release-retention",
        type=int,
        default=int(os.getenv("DATA_RELEASE_RETENTION", "2")),
    )
    parser.add_argument(
        "--min-lion-rows",
        type=int,
        default=int(os.getenv("MIN_LION_SOURCE_ROWS", "200000")),
    )
    parser.add_argument(
        "--min-dsny-rows",
        type=int,
        default=int(os.getenv("MIN_DSNY_SOURCE_ROWS", "500")),
    )
    parser.add_argument(
        "--min-output-features",
        type=int,
        default=int(os.getenv("MIN_OUTPUT_FEATURES", "100000")),
    )
    parser.add_argument(
        "--max-count-drop-percent",
        type=float,
        default=float(os.getenv("MAX_COUNT_DROP_PERCENT", "10")),
    )
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
    if args.release_retention < 2:
        raise SystemExit("--release-retention must be at least 2")
    if not 0 <= args.max_count_drop_percent < 100:
        raise SystemExit("--max-count-drop-percent must be at least 0 and below 100")
    regression_gate = {
        "min_lion_rows": args.min_lion_rows,
        "min_dsny_rows": args.min_dsny_rows,
        "min_output_features": args.min_output_features,
        "max_drop_fraction": args.max_count_drop_percent / 100,
    }
    manifest_path = args.manifest.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nyc-refresh-", dir=manifest_path.parent) as temporary:
        staging = Path(temporary)
        bundle = staging / "release"
        bundle.mkdir()
        dsny = bundle / "dsny_frequencies.geojson"
        lion_zip = bundle / "lion.zip"
        pad_zip = bundle / "pad.zip"
        LOGGER.info("Stage 1/8: downloading complete DSNY frequency layer")
        dsny_source_audit = download_dsny(dsny)
        LOGGER.info(
            "Stage 1/8 complete: DSNY features=%s object_id_field=%s",
            dsny_source_audit["record_count"],
            dsny_source_audit["object_id_field"],
        )
        LOGGER.info("Stage 2/8: downloading complete LION archive")
        lion_source_audit = download_file(LION_URL, lion_zip)
        LOGGER.info("Stage 2/8 complete: LION archive bytes=%s", _format_bytes(int(lion_source_audit["bytes"])))
        LOGGER.info("Stage 2/8: downloading complete PAD archive for shadow identity audit")
        pad_source_audit = download_file(PAD_URL, pad_zip)
        LOGGER.info("Stage 2/8 complete: PAD archive bytes=%s", _format_bytes(int(pad_source_audit["bytes"])))
        LOGGER.info("Stage 3/8: hashing and extracting LION archive")
        input_hashes = {
            "dsny": file_sha256(dsny),
            "lion": file_sha256(lion_zip),
            "pad": file_sha256(pad_zip),
        }
        lion_metadata = _remote_metadata(LION_METADATA_URL)
        pad_metadata = _remote_metadata(PAD_METADATA_URL)
        with zipfile.ZipFile(lion_zip) as archive:
            lion_release = source_release_identifier(archive.namelist(), "lion")
        with zipfile.ZipFile(pad_zip) as archive:
            pad_release = source_release_identifier(archive.namelist(), "pad")
        lion_catalog_release = metadata_release_identifier(lion_metadata)
        pad_catalog_release = metadata_release_identifier(pad_metadata)
        addresspoint_before = _remote_metadata(ADDRESSPOINT_METADATA_URL)
        cscl_metadata = _remote_metadata(CSCL_METADATA_URL)
        processing_identity = processing_fingerprint(
            tile_minzoom=args.tile_minzoom,
            tile_maxzoom=args.tile_maxzoom,
            side_offset_feet=args.side_offset_feet,
        )
        # A fast refresh still observes the shadow-source freshness gates. Two
        # metadata reads prevent a changing AddressPoint/CSCL revision from
        # making an old release look current merely because its primary inputs
        # are unchanged.
        addresspoint_probe_after = _remote_metadata(ADDRESSPOINT_METADATA_URL)
        cscl_probe_after = _remote_metadata(CSCL_METADATA_URL)
        candidate_source_identity = source_fingerprint(
            input_sha256=input_hashes,
            dsny_source_audit=dsny_source_audit,
            lion_source_audit=lion_source_audit,
            pad_source_audit=pad_source_audit,
            lion_release=lion_release,
            pad_release=pad_release,
            lion_catalog_release=lion_catalog_release,
            pad_catalog_release=pad_catalog_release,
            lion_metadata=lion_metadata,
            pad_metadata=pad_metadata,
            addresspoint_metadata_before=addresspoint_before,
            addresspoint_metadata_after=addresspoint_probe_after,
            cscl_metadata_before=cscl_metadata,
            cscl_metadata_after=cscl_probe_after,
        )
        if (
            addresspoint_before == addresspoint_probe_after
            and cscl_metadata == cscl_probe_after
        ):
            reusable = unchanged_release(
                manifest_path,
                refresh_fingerprint(processing_identity, candidate_source_identity),
                regression_gate=regression_gate,
            )
            if reusable is not None:
                LOGGER.info(
                    "Sources and processing fingerprint unchanged; retaining dataset version=%s "
                    "and skipping extraction plus stages 4-8",
                    reusable["dataset_version"],
                )
                return
        else:
            LOGGER.info(
                "Shadow source metadata changed during freshness probe; full refresh required"
            )
        extracted = staging / "lion-extracted"
        shutil.unpack_archive(lion_zip, extracted)
        geodatabases = sorted(extracted.rglob("*.gdb"))
        if len(geodatabases) != 1:
            raise RuntimeError(
                "LION archive must contain exactly one ESRI geodatabase "
                f"found={len(geodatabases)}"
            )
        gdb = geodatabases[0]
        LOGGER.info("Stage 3/8 complete: discovered LION geodatabase=%s", gdb.name)
        processed = bundle / "citywide.geojson"
        failures = bundle / "ingestion_failures.jsonl"
        staged_audit = bundle / "ingestion_audit.json"
        command = [
            sys.executable,
            "scripts/build_pilot.py",
            "--lion",
            str(gdb),
            "--lion-layer",
            "lion",
            "--frequencies",
            str(dsny),
            "--output",
            str(processed),
            "--audit",
            str(staged_audit),
            "--failures",
            str(failures),
            "--side-offset-feet",
            str(args.side_offset_feet),
        ]
        LOGGER.info("Stage 4/8: auditing LION block faces against DSNY frequency polygons")
        stage_started = monotonic()
        subprocess.run(command, check=True)
        LOGGER.info("Stage 4/8 complete elapsed_s=%.1f", monotonic() - stage_started)
        addresspoint_after = _remote_metadata(ADDRESSPOINT_METADATA_URL)
        identity_report = identity_shadow_report(
            lion_release=lion_release,
            pad_release=pad_release,
            addresspoint_metadata_before=addresspoint_before,
            addresspoint_metadata_after=addresspoint_after,
        )
        addresspoint_report = {
            "report_version": 1,
            "mode": "shadow",
            "dataset": "6xyb-j5pk",
            "metadata_before": addresspoint_before,
            "metadata_after": addresspoint_after,
            "requested_candidate_count": 0,
            "returned_count": 0,
            "object_ids": [],
            "count_verified": True,
            "object_id_set_verified": True,
            "pagination_verified": True,
            "skipped_reason": identity_report["blocking_reasons"][0],
        }
        cscl_report = {
            "report_version": 1,
            "rule_version": GEOMETRY_RULE_VERSION,
            "mode": "shadow",
            "source_metadata": cscl_metadata,
            "candidate_count": 0,
            "evaluated_count": 0,
            "evaluation_status": "PENDING_STABILITY_AND_MANUAL_REVIEW",
            "promoted_count": 0,
            "promotion_enabled": False,
            "blocking_reasons": ["TWO_STABLE_SHADOW_BUILDS_REQUIRED", "BOROUGH_STRATIFIED_REVIEW_REQUIRED"],
        }
        recovery_report = {
            "report_version": 1,
            "identity": identity_report,
            "geometry": cscl_report,
            "fully_outside_promotion_enabled": False,
            "fully_outside_blocking_reason": "NO_DOCUMENTED_PUBLIC_DSNY_ADDRESS_LOOKUP_CONTRACT",
        }
        atomic_json(bundle / "addresspoint_query_report.json", addresspoint_report)
        atomic_json(bundle / "cscl_alignment_report.json", cscl_report)
        atomic_json(bundle / "recovery_shadow_report.json", recovery_report)
        LOGGER.info("Stage 5/8: validating audited GeoJSON and provenance")
        stage_started = monotonic()
        processed_validation = validate_and_prepare_processed_geojson(processed)
        processed_summary = processed_validation.summary
        unknown_records = processed_validation.unknown_features
        unknown_reason_counts: dict[str, int] = {}
        cscl_physical_ids: set[str] = set()
        for feature in unknown_records:
            properties = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(properties, dict):
                raise RuntimeError("processed unknown feature is malformed")
            reason_code = str(properties.get("reason_code", ""))
            unknown_reason_counts[reason_code] = unknown_reason_counts.get(reason_code, 0) + 1
            evidence = properties.get("evidence")
            if reason_code == "PARTIAL_GEOMETRY_GAP" and isinstance(evidence, dict):
                physical_id = str(evidence.get("physical_id") or "")
                if physical_id:
                    cscl_physical_ids.add(physical_id)
        identity_report["candidate_count"] = unknown_reason_counts.get(
            "INSUFFICIENT_ADDRESS_EVIDENCE", 0
        )
        cscl_report["candidate_count"] = unknown_reason_counts.get("PARTIAL_GEOMETRY_GAP", 0)
        cscl_subset_path = bundle / "cscl_alignment_subset.geojson"
        cscl_query_audit = download_cscl_subset(cscl_physical_ids, cscl_subset_path)
        cscl_report["query_audit"] = cscl_query_audit
        cscl_report["evaluated_count"] = 0
        cscl_report["evaluation_status"] = "SUBSET_VERIFIED_PROMOTION_BLOCKED_PENDING_REVIEW"
        addresspoint_report["shadow_input_candidate_count"] = identity_report["candidate_count"]
        recovery_report["unknown_reason_counts"] = unknown_reason_counts
        atomic_json(bundle / "addresspoint_query_report.json", addresspoint_report)
        atomic_json(bundle / "cscl_alignment_report.json", cscl_report)
        atomic_json(bundle / "recovery_shadow_report.json", recovery_report)
        atomic_json(
            bundle / "unknown_block_faces.geojson",
            {"type": "FeatureCollection", "features": unknown_records},
        )
        ingestion_audit = validate_ingestion_audit(
            staged_audit,
            expected_processed_sha256=processed_summary["sha256"],
            expected_processed_features=processed_summary["feature_count"],
        )
        LOGGER.info(
            "Stage 5/8 complete features=%s raw_lion_rows=%s dsny_rows=%s elapsed_s=%.1f",
            processed_summary["feature_count"],
            ingestion_audit["source_rows"],
            ingestion_audit["frequency_rows"],
            monotonic() - stage_started,
        )

        processed_time = datetime.now(UTC)
        processed_at = processed_time.isoformat()
        dataset_version = _release_version(processed_time, str(processed_summary["sha256"]))
        audit_sha256 = file_sha256(staged_audit)
        staged_db = bundle / "app.sqlite3"
        LOGGER.info("Stage 6/8: loading audited GeoJSON into SQLite")
        stage_started = monotonic()
        load_prepared_payload(processed_validation.prepared, staged_db)
        _bind_database_metadata(
            staged_db,
            dataset_version=dataset_version,
            processed_sha256=str(processed_summary["sha256"]),
            processed_semantic_sha256=str(processed_summary["semantic_sha256"]),
            processed_features=int(processed_summary["feature_count"]),
            audit_sha256=audit_sha256,
        )
        database_summary = validate_database(
            staged_db,
            expected_version=dataset_version,
            expected_processed_sha256=str(processed_summary["sha256"]),
            expected_processed_semantic_sha256=str(processed_summary["semantic_sha256"]),
            expected_processed_features=int(processed_summary["feature_count"]),
            expected_audit_sha256=audit_sha256,
        )
        database_summary.update(
            validate_processed_database_semantics(processed_summary, staged_db)
        )
        LOGGER.info(
            "Stage 6/8 complete block_faces=%s schedules=%s elapsed_s=%.1f",
            database_summary["block_faces"],
            database_summary["schedule_count"],
            monotonic() - stage_started,
        )
        # The semantic hash maps remain in processed_summary for the final
        # cross-artifact gate; the much larger geometry/provenance objects are
        # no longer needed after their independently reconciled SQLite load.
        del processed_validation, unknown_records

        staged_tileset = bundle / "collection_streets.mbtiles"
        tile_report_path = bundle / "tile_build_report.json"
        LOGGER.info(
            "Stage 7/8: building gzip vector tiles zooms=%s-%s",
            args.tile_minzoom,
            args.tile_maxzoom,
        )
        stage_started = monotonic()
        subprocess.run(
            [
                sys.executable,
                "scripts/build_tiles.py",
                "--database",
                str(staged_db),
                "--output",
                str(staged_tileset),
                "--minzoom",
                str(args.tile_minzoom),
                "--maxzoom",
                str(args.tile_maxzoom),
                "--version",
                dataset_version,
                "--source-version",
                dataset_version,
                "--data-updated",
                processed_at,
                "--metadata-output",
                str(tile_report_path),
            ],
            check=True,
        )
        tileset_summary = validate_tileset(
            staged_tileset,
            dataset_version,
            expected_database=database_summary,
            expected_database_path=staged_db,
        )
        tile_report = validate_tile_build_report(
            tile_report_path,
            expected_version=dataset_version,
            database=database_summary,
            tileset=tileset_summary,
        )
        LOGGER.info(
            "Stage 7/8 complete tiles=%s rendered_features=%s nonrenderable_features=%s "
            "rendered_unknowns=%s nonrenderable_unknowns=%s max_compressed_bytes=%s elapsed_s=%.1f",
            tileset_summary["tile_count"],
            tile_report["maxzoom_feature_count"],
            tile_report["maxzoom_nonrenderable_feature_count"],
            tile_report["maxzoom_unknown_feature_count"],
            tile_report["maxzoom_nonrenderable_unknown_feature_count"],
            tile_report["tile_size_metrics"]["max_compressed_tile_bytes"],
            monotonic() - stage_started,
        )

        cscl_after = _remote_metadata(CSCL_METADATA_URL)
        release_source_identity = source_fingerprint(
            input_sha256=input_hashes,
            dsny_source_audit=dsny_source_audit,
            lion_source_audit=lion_source_audit,
            pad_source_audit=pad_source_audit,
            lion_release=lion_release,
            pad_release=pad_release,
            lion_catalog_release=lion_catalog_release,
            pad_catalog_release=pad_catalog_release,
            lion_metadata=lion_metadata,
            pad_metadata=pad_metadata,
            addresspoint_metadata_before=addresspoint_before,
            addresspoint_metadata_after=addresspoint_after,
            cscl_metadata_before=cscl_metadata,
            cscl_metadata_after=cscl_after,
        )
        release_refresh_fingerprint = refresh_fingerprint(
            processing_identity,
            release_source_identity,
        )
        audit_summary = {key: value for key, value in ingestion_audit.items() if key != "records"}
        lion_source_audit["record_count"] = ingestion_audit["source_rows"]
        source_report = {
            "report_version": 1,
            "dataset_version": dataset_version,
            "snapshot_completed_at": processed_at,
            "sources": {
                "dsny": {
                    "url": DSNY_QUERY,
                    "sha256": input_hashes["dsny"],
                    **dsny_source_audit,
                },
                "lion": {
                    "url": LION_URL,
                    "sha256": input_hashes["lion"],
                    "release_identifier": lion_release,
                    "catalog_release_identifier": lion_catalog_release,
                    "metadata": lion_metadata,
                    **lion_source_audit,
                },
                "pad": {
                    "url": PAD_URL,
                    "sha256": input_hashes["pad"],
                    "release_identifier": pad_release,
                    "catalog_release_identifier": pad_catalog_release,
                    "metadata": pad_metadata,
                    **pad_source_audit,
                },
                "addresspoint": {
                    "url": ADDRESSPOINT_METADATA_URL,
                    "metadata": addresspoint_after,
                    "queried_records": 0,
                },
                "cscl": {
                    "url": CSCL_METADATA_URL,
                    "metadata": cscl_metadata,
                    "queried_records": 0,
                },
            },
        }
        source_report_path = bundle / "source_report.json"
        atomic_json(source_report_path, source_report)
        counts = {
            "raw_lion_rows": ingestion_audit["source_rows"],
            "dsny_frequency_rows": ingestion_audit["frequency_rows"],
            "eligible_lion_rows": ingestion_audit["eligible_lion_rows"],
            "matched_sides": ingestion_audit["matched"],
            "used_frequency_rows": ingestion_audit["used_valid_frequency_rows"],
            "output_features": processed_summary["feature_count"],
            "block_faces": database_summary["block_faces"],
            "schedule_rows": database_summary["schedule_count"],
            "schedule_groups": database_summary["schedule_group_count"],
            "schedule_rows_by_type": database_summary["schedule_counts"],
            "tile_features": tile_report["feature_count"],
            "rendered_tile_features": tile_report["maxzoom_feature_count"],
            "nonrenderable_tile_features": tile_report["maxzoom_nonrenderable_feature_count"],
            "unknown_features": database_summary["unknown_feature_count"],
            "rendered_unknown_features": tile_report["maxzoom_unknown_feature_count"],
            "nonrenderable_unknown_features": tile_report["maxzoom_nonrenderable_unknown_feature_count"],
        }
        recovery_counts = {
            "identity_candidates": identity_report["candidate_count"],
            "identity_promoted": 0,
            "geometry_candidates": cscl_report["candidate_count"],
            "geometry_promoted": 0,
        }
        previous_dataset_version = None
        previous_recovery_counts: dict[str, object] = {}
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous_manifest, dict):
                previous_dataset_version = previous_manifest.get("dataset_version")
                raw_previous_counts = previous_manifest.get("recovery_counts")
                if isinstance(raw_previous_counts, dict):
                    previous_recovery_counts = raw_previous_counts
        except FileNotFoundError:
            pass
        recovery_diff = {
            "report_version": 1,
            "dataset_version": dataset_version,
            "previous_dataset_version": previous_dataset_version,
            "previous": previous_recovery_counts,
            "current": recovery_counts,
            "promoted_identity_delta": -int(previous_recovery_counts.get("identity_promoted", 0)),
            "promoted_geometry_delta": -int(previous_recovery_counts.get("geometry_promoted", 0)),
            "publication_action": "SHADOW_ONLY_NO_PROMOTIONS",
        }
        recovery_diff_path = bundle / "recovery_diff.json"
        atomic_json(recovery_diff_path, recovery_diff)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "dataset_version": dataset_version,
            "release_path": f"releases/{dataset_version}",
            "processed_at": processed_at,
            "refresh_fingerprint": release_refresh_fingerprint,
            "sources": source_report["sources"],
            "input_sha256": input_hashes,
            "counts": counts,
            "block_faces": database_summary["block_faces"],
            "schedule_counts": database_summary["schedule_counts"],
            "source_versions": {
                "lion_artifact": lion_release,
                "pad_artifact": pad_release,
                "lion_catalog": lion_catalog_release,
                "pad_catalog": pad_catalog_release,
            },
            "recovery_counts": recovery_counts,
            "database": database_summary,
            "tileset": tileset_summary,
            "ingestion_audit": {
                **audit_summary,
                "sha256": audit_sha256,
                "artifact": staged_audit.name,
            },
            "failure_records": ingestion_audit.get("fatal_side_count", 0),
            "artifacts": {
                "database": _artifact(
                    staged_db,
                    database_schema_revision=DATABASE_SCHEMA_REVISION,
                    dataset_version=dataset_version,
                    block_faces=database_summary["block_faces"],
                    schedule_count=database_summary["schedule_count"],
                    processed_sha256=processed_summary["sha256"],
                    processed_semantic_sha256=processed_summary["semantic_sha256"],
                ),
                "tileset": _artifact(
                    staged_tileset,
                    version=dataset_version,
                    tile_schema_revision=tile_report["tile_schema_revision"],
                    feature_count=tile_report["feature_count"],
                    source_database_sha256=database_summary["sha256"],
                ),
                "ingestion_audit": _artifact(
                    staged_audit,
                    processed_sha256=processed_summary["sha256"],
                    output_features=ingestion_audit["output_features"],
                ),
                "processed_geojson": _artifact(
                    processed,
                    feature_count=processed_summary["feature_count"],
                    semantic_sha256=processed_summary["semantic_sha256"],
                ),
                "tile_build_report": _artifact(tile_report_path),
                "source_report": _artifact(source_report_path),
                "ingestion_failures": _artifact(failures),
                "source_dsny": _artifact(dsny, record_count=ingestion_audit["frequency_rows"]),
                "source_lion": _artifact(lion_zip, record_count=ingestion_audit["source_rows"]),
                "source_pad": _artifact(pad_zip, release_identifier=pad_release),
                "unknown_geojson": _artifact(bundle / "unknown_block_faces.geojson", feature_count=database_summary["unknown_feature_count"]),
                "addresspoint_query_report": _artifact(bundle / "addresspoint_query_report.json"),
                "cscl_alignment_report": _artifact(bundle / "cscl_alignment_report.json"),
                "cscl_alignment_subset": _artifact(cscl_subset_path, feature_count=cscl_query_audit["returned_features"]),
                "recovery_shadow_report": _artifact(bundle / "recovery_shadow_report.json"),
                "recovery_diff": _artifact(recovery_diff_path),
            },
        }
        atomic_json(bundle / "release_manifest.json", manifest)
        LOGGER.info("Stage 8/8: validating release bundle and publishing atomically")
        stage_started = monotonic()
        validated_bundle = validate_release_bundle(
            bundle,
            prevalidated_components=PrevalidatedReleaseComponents(
                processed=processed_summary,
                audit=ingestion_audit,
                database=database_summary,
                tileset=tileset_summary,
                tile_report=tile_report,
            ),
        )
        published = publish_release(
            bundle,
            manifest_path,
            retention=args.release_retention,
            regression_gate=regression_gate,
            validated_bundle=validated_bundle,
        )
        LOGGER.info(
            "Stage 8/8 complete: promoted audited dataset version=%s block_faces=%s tiles=%s elapsed_s=%.1f",
            published["dataset_version"],
            database_summary["block_faces"],
            tileset_summary["tile_count"],
            monotonic() - stage_started,
        )


if __name__ == "__main__":
    main()
