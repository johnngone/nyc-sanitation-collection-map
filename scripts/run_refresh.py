"""Download official sources, build a staged citywide dataset, and promote it atomically."""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_validation import (
    atomic_json,
    file_sha256,
    publish_release,
    validate_database,
    validate_ingestion_audit,
    validate_processed_database_semantics,
    validate_processed_geojson,
    validate_tile_build_report,
    validate_tileset,
)

LOGGER = logging.getLogger("run_refresh")
DSNY_LAYER = "https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0"
DSNY_QUERY = f"{DSNY_LAYER}/query"
LION_URL = "https://data.cityofnewyork.us/download/2v4z-66xt/application/zip"
USER_AGENT = "nyc-sanitation-map/1.0"
DOWNLOAD_PROGRESS_BYTES = 16 * 1024 * 1024
DOWNLOAD_PROGRESS_SECONDS = 30.0


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
                        "LION download progress bytes=%s expected=%s percent=%s rate_mib_s=%.2f elapsed_s=%.0f",
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--manifest", type=Path, default=Path(os.getenv("DATA_MANIFEST_PATH", "data/data_manifest.json")))
    parser.add_argument("--tile-minzoom", type=int, default=int(os.getenv("TILE_MIN_ZOOM", "12")))
    parser.add_argument("--tile-maxzoom", type=int, default=int(os.getenv("TILE_MAX_ZOOM", "17")))
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
    manifest_path = args.manifest.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nyc-refresh-", dir=manifest_path.parent) as temporary:
        staging = Path(temporary)
        bundle = staging / "release"
        bundle.mkdir()
        dsny = bundle / "dsny_frequencies.geojson"
        lion_zip = bundle / "lion.zip"
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
        LOGGER.info("Stage 3/8: hashing and extracting LION archive")
        input_hashes = {"dsny": file_sha256(dsny), "lion": file_sha256(lion_zip)}
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
        LOGGER.info("Stage 5/8: validating audited GeoJSON and provenance")
        stage_started = monotonic()
        processed_summary = validate_processed_geojson(processed)
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
        subprocess.run([sys.executable, "scripts/load_processed.py", str(processed), "--database", str(staged_db)], check=True)
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
            "Stage 7/8 complete tiles=%s max_compressed_bytes=%s elapsed_s=%.1f",
            tileset_summary["tile_count"],
            tile_report["tile_size_metrics"]["max_compressed_tile_bytes"],
            monotonic() - stage_started,
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
                    **lion_source_audit,
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
        }
        manifest = {
            "manifest_version": 2,
            "dataset_version": dataset_version,
            "release_path": f"releases/{dataset_version}",
            "processed_at": processed_at,
            "sources": source_report["sources"],
            "input_sha256": input_hashes,
            "counts": counts,
            "block_faces": database_summary["block_faces"],
            "schedule_counts": database_summary["schedule_counts"],
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
            },
        }
        atomic_json(bundle / "release_manifest.json", manifest)
        LOGGER.info("Stage 8/8: validating release bundle and publishing atomically")
        stage_started = monotonic()
        published = publish_release(
            bundle,
            manifest_path,
            retention=args.release_retention,
            regression_gate={
                "min_lion_rows": args.min_lion_rows,
                "min_dsny_rows": args.min_dsny_rows,
                "min_output_features": args.min_output_features,
                "max_drop_fraction": args.max_count_drop_percent / 100,
            },
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
