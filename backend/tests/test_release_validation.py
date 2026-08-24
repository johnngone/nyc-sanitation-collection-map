import hashlib
import gzip
import json
import shutil
import sqlite3
import copy
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import mapbox_vector_tile
import pytest
from shapely.geometry import LineString

from app.database import initialize
from app.releases import ReleaseManifestError, read_current_release, tileset_for_version
from scripts.build_tiles import build_tiles
from scripts.load_processed import load_prepared_payload
import scripts.release_validation as release_validation
from scripts.release_validation import (
    PrevalidatedReleaseComponents,
    activate_release,
    atomic_json,
    file_sha256,
    publish_release,
    validate_database,
    validate_ingestion_audit,
    validate_and_prepare_processed_geojson,
    validate_processed_database_semantics,
    validate_processed_geojson,
    validate_regression_gates,
    validate_release_bundle,
    validate_tile_build_report,
    validate_tileset,
)
from scripts.release_validation import _tile_size_metrics
from scripts.release_validation import _validated_nonrenderable_records


def _audit(processed_sha256: str) -> dict[str, object]:
    outcomes = {
        "matched": 1,
        "out_of_scope": 0,
        "deduplicated_alias": 0,
        "non_addressable": 1,
        "outside_schedule_area": 0,
        "partially_outside_schedule_area": 0,
        "ambiguous": 0,
        "invalid": 0,
        "conflicts": 0,
    }
    source_outcomes = {
        "in_scope": 1,
        "deduplicated_alias": 0,
        "out_of_scope": 0,
        "curbside_out_of_scope": 0,
        "invalid": 0,
    }
    frequency_outcomes = {"used_valid": 1, "unused_valid": 0, "invalid": 0}
    return {
        "audit_version": 2,
        "source_rows": 1,
        "raw_source_rows": 1,
        "raw_lion_rows": 1,
        "source_row_outcomes": source_outcomes,
        "classified_source_rows": 1,
        "in_scope_source_rows": 1,
        "in_scope_lion_rows": 1,
        "eligible_lion_rows": 1,
        "processed_segment_rows": 1,
        "deduplicated_alias_rows": 0,
        "curbside_excluded_lion_rows": 0,
        "out_of_scope_source_rows": 0,
        "excluded_lion_rows": 0,
        "invalid_source_rows": 0,
        "source_row_reconciliation": {
            "expected": 1,
            "classified": 1,
            "difference": 0,
            "passed": True,
        },
        "frequency_rows": 1,
        "expected_sides": 2,
        "classified_sides": 2,
        "outcomes": outcomes,
        "matched": 1,
        "unmatched": 0,
        "outside_schedule_area": 0,
        "partially_outside_schedule_area": 0,
        "non_addressable": 1,
        "ambiguous": 0,
        "invalid": 0,
        "conflicts": 0,
        "output_features": 1,
        "reconciliation": {
            "expected": 2,
            "classified": 2,
            "difference": 0,
            "passed": True,
        },
        "frequency_outcomes": frequency_outcomes,
        "valid_frequency_rows": 1,
        "used_valid_frequency_rows": 1,
        "unused_valid_frequency_rows": 0,
        "invalid_frequency_rows": 0,
        "frequency_reconciliation": {
            "expected": 1,
            "classified": 1,
            "difference": 0,
            "passed": True,
        },
        "reconciled": True,
        "global_errors": [],
        "fatal_side_count": 0,
        "fatal_frequency_count": 0,
        "fatal_count": 0,
        "records": [{"outcome": "non_addressable"}],
        "processed_sha256": processed_sha256,
        "processed_feature_count": 1,
        "passed": True,
    }


def _processed_payload() -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "schema_revision": 3,
        "unknown_features": [],
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-73.9, 40.7], [-73.89, 40.71]],
                },
                "properties": {
                    "block_face_id": "face-1",
                    "origin_block_face_id": "origin-1",
                    "segment_id": "segment-1",
                    "segment_ids": ["segment-1"],
                    "street_name": "TEST STREET",
                    "street_names": ["TEST STREET"],
                    "borough": "QUEENS",
                    "side": "LEFT",
                    "refuse_days": ["MON"],
                    "schedules": {
                        collection_type: ["MON"]
                        for collection_type in ("REFUSE", "RECYCLING", "ORGANICS", "BULK")
                    },
                    "schedule_states": {
                        collection_type: {
                            "state": "SOURCE_EXPLICIT",
                            "source_field": f"FREQ_{collection_type}",
                            "raw_value": "MON",
                            "rule_id": None,
                            "source_policy_conflict": False,
                            "provenance": "DSNY test fixture",
                        }
                        for collection_type in ("REFUSE", "RECYCLING", "ORGANICS", "BULK")
                    },
                    "dsny_object_ids": ["101"],
                    "dsny_sources": [{"object_id": "101", "frequency_row": 0}],
                    "lion_components": [
                        {
                            "segment_id": "segment-1",
                            "source_side": "LEFT",
                            "source_rows": [0],
                            "source_indices": [0],
                            "street_names": ["TEST STREET"],
                            "source_records": [
                                {
                                    "source_row": 0,
                                    "source_index": 0,
                                    "segment_id": "segment-1",
                                }
                            ],
                            "dsny_object_ids": ["101"],
                        }
                    ],
                    "source": "DSNY",
                    "retrieved_at": "2026-08-19",
                },
            }
        ],
    }


def _database(
    path,
    version: str,
    processed_hash: str,
    processed_semantic_hash: str,
    audit_hash: str,
) -> None:
    initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """INSERT INTO block_faces
               (block_face_id, origin_block_face_id, segment_id, borough, street_name,
                side, geometry_wkt, min_x, min_y, max_x, max_y)
               VALUES ('face-1', 'origin-1', 'segment-1', 'QUEENS', 'TEST STREET',
                       'LEFT', 'LINESTRING (-73.9 40.7, -73.89 40.71)',
                       -73.9, 40.7, -73.89, 40.71)"""
        )
        connection.execute("INSERT INTO block_face_rtree_map(block_face_id) VALUES ('face-1')")
        rtree_id = connection.execute(
            "SELECT rtree_id FROM block_face_rtree_map WHERE block_face_id = 'face-1'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO block_faces_rtree VALUES (?, ?, ?, ?, ?)",
            (rtree_id, -73.9, -73.89, 40.7, 40.71),
        )
        connection.execute(
            """INSERT INTO block_face_lion_components
               (block_face_id, component_index, segment_id, source_side,
                source_rows_json, source_indices_json, street_names_json,
                source_records_json, dsny_object_ids_json)
               VALUES ('face-1', 0, 'segment-1', 'LEFT', '[0]', '[0]',
                       '["TEST STREET"]',
                       '[{"segment_id":"segment-1","source_index":0,"source_row":0}]',
                       '["101"]')"""
        )
        connection.execute(
            """INSERT INTO block_face_dsny_sources
               (block_face_id, dsny_object_id, frequency_row)
               VALUES ('face-1', '101', 0)"""
        )
        for collection_type in ("REFUSE", "RECYCLING", "ORGANICS", "BULK"):
            connection.execute(
                """INSERT INTO collection_schedules
                   (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                   VALUES ('face-1', ?, 'MON', 'DSNY', '2026-08-19', 'AUDITED_SIDE_OFFSET')""",
                (collection_type,),
            )
            connection.execute(
                """INSERT INTO block_face_collection_states
                   (block_face_id, collection_type, effective_days_json, state,
                    source_field, raw_value, rule_id, source_policy_conflict, provenance)
                   VALUES ('face-1', ?, '["MON"]', 'SOURCE_EXPLICIT', ?,
                           'MON', NULL, 0, 'DSNY test fixture')""",
                (collection_type, f"FREQ_{collection_type}"),
            )
        connection.executemany(
            "INSERT INTO dataset_metadata(key, value) VALUES (?, ?)",
            {
                "dataset_version": version,
                "processed_sha256": processed_hash,
                "processed_semantic_sha256": processed_semantic_hash,
                "processed_feature_count": "1",
                "ingestion_audit_sha256": audit_hash,
            }.items(),
        )
        connection.commit()


def _artifact(path, **values) -> dict[str, object]:
    return {"path": path.name, "sha256": file_sha256(path), **values}


def _bundle(tmp_path, version: str):
    bundle = tmp_path / f"bundle-{version}"
    bundle.mkdir()
    processed = bundle / "citywide.geojson"
    processed.write_text(json.dumps(_processed_payload()), encoding="utf-8")
    processed_summary = validate_processed_geojson(processed)
    processed_hash = processed_summary["sha256"]
    audit_path = bundle / "ingestion_audit.json"
    atomic_json(audit_path, _audit(processed_hash))
    audit_hash = file_sha256(audit_path)
    database = bundle / "app.sqlite3"
    _database(
        database,
        version,
        processed_hash,
        processed_summary["semantic_sha256"],
        audit_hash,
    )
    database_summary = validate_database(
        database,
        expected_version=version,
        expected_processed_sha256=processed_hash,
        expected_processed_semantic_sha256=processed_summary["semantic_sha256"],
        expected_processed_features=1,
        expected_audit_sha256=audit_hash,
    )
    database_summary.update(
        validate_processed_database_semantics(processed_summary, database)
    )
    tileset = bundle / "collection_streets.mbtiles"
    report = build_tiles(
        database,
        tileset,
        minzoom=10,
        maxzoom=11,
        version=version,
        source_version=version,
    ).as_dict()
    report_path = bundle / "tile_build_report.json"
    atomic_json(report_path, report)
    dsny = bundle / "dsny_frequencies.geojson"
    dsny.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    lion = bundle / "lion.zip"
    lion.write_bytes(b"test-lion-snapshot")
    pad = bundle / "pad.zip"
    pad.write_bytes(b"test-pad-snapshot")
    unknown = bundle / "unknown_block_faces.geojson"
    atomic_json(unknown, {"type": "FeatureCollection", "features": []})
    addresspoint_report = bundle / "addresspoint_query_report.json"
    atomic_json(addresspoint_report, {"report_version": 1, "returned_count": 0})
    cscl_report = bundle / "cscl_alignment_report.json"
    atomic_json(cscl_report, {"report_version": 1, "promoted_count": 0})
    cscl_subset = bundle / "cscl_alignment_subset.geojson"
    atomic_json(cscl_subset, {"type": "FeatureCollection", "features": []})
    recovery_report = bundle / "recovery_shadow_report.json"
    atomic_json(recovery_report, {"report_version": 1, "mode": "shadow"})
    recovery_diff = bundle / "recovery_diff.json"
    atomic_json(recovery_diff, {"report_version": 1, "publication_action": "SHADOW_ONLY_NO_PROMOTIONS"})
    failures = bundle / "ingestion_failures.jsonl"
    failures.write_text("", encoding="utf-8")
    source_report_path = bundle / "source_report.json"
    source_report = {
        "report_version": 1,
        "dataset_version": version,
        "sources": {
            "dsny": {"sha256": file_sha256(dsny), "record_count": 1},
            "lion": {"sha256": file_sha256(lion), "record_count": 1},
            "pad": {"sha256": file_sha256(pad), "release_identifier": "26B"},
        },
    }
    atomic_json(source_report_path, source_report)
    tileset_summary = validate_tileset(
        tileset,
        version,
        expected_database=database_summary,
        expected_database_path=database,
    )
    manifest = {
        "manifest_version": 3,
        "dataset_version": version,
        "release_path": f"releases/{version}",
        "processed_at": "2026-08-19T12:00:00+00:00",
        "counts": {
            "raw_lion_rows": 1,
            "dsny_frequency_rows": 1,
            "eligible_lion_rows": 1,
            "matched_sides": 1,
            "used_frequency_rows": 1,
            "output_features": 1,
            "block_faces": 1,
            "schedule_rows": 4,
            "schedule_groups": 4,
            "schedule_rows_by_type": database_summary["schedule_counts"],
            "tile_features": 1,
            "rendered_tile_features": 1,
            "nonrenderable_tile_features": 0,
            "unknown_features": 0,
            "rendered_unknown_features": 0,
            "nonrenderable_unknown_features": 0,
        },
        "block_faces": 1,
        "schedule_counts": database_summary["schedule_counts"],
        "database": database_summary,
        "tileset": tileset_summary,
        "ingestion_audit": {
            **{
                key: value
                for key, value in _audit(processed_hash).items()
                if key != "records"
            },
            "sha256": audit_hash,
            "artifact": "ingestion_audit.json",
        },
        "artifacts": {
            "database": _artifact(
                database,
                dataset_version=version,
                block_faces=1,
                schedule_count=4,
                processed_sha256=processed_hash,
                processed_semantic_sha256=processed_summary["semantic_sha256"],
            ),
            "tileset": _artifact(
                tileset,
                version=version,
                tile_schema_revision=report["tile_schema_revision"],
                feature_count=1,
                source_database_sha256=database_summary["sha256"],
            ),
            "ingestion_audit": _artifact(
                audit_path,
                processed_sha256=processed_hash,
                output_features=1,
            ),
            "processed_geojson": _artifact(
                processed,
                feature_count=1,
                semantic_sha256=processed_summary["semantic_sha256"],
            ),
            "tile_build_report": _artifact(report_path),
            "source_report": _artifact(source_report_path),
            "ingestion_failures": _artifact(failures),
            "source_dsny": _artifact(dsny, record_count=1),
            "source_lion": _artifact(lion, record_count=1),
            "source_pad": _artifact(pad, release_identifier="26B"),
            "unknown_geojson": _artifact(unknown, feature_count=0),
            "addresspoint_query_report": _artifact(addresspoint_report),
            "cscl_alignment_report": _artifact(cscl_report),
            "cscl_alignment_subset": _artifact(cscl_subset, feature_count=0),
            "recovery_shadow_report": _artifact(recovery_report),
            "recovery_diff": _artifact(recovery_diff),
        },
    }
    atomic_json(bundle / "release_manifest.json", manifest)
    return bundle


def _rewrite_bundle_tileset_as_v3(bundle) -> None:
    """Turn the compact v4 fixture into a valid retained v3 release."""

    manifest_path = bundle / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest["dataset_version"]
    database = bundle / "app.sqlite3"
    tileset = bundle / "collection_streets.mbtiles"
    expected_properties = release_validation._expected_tile_properties(
        database,
        tile_schema_revision=3,
    )

    with closing(sqlite3.connect(tileset)) as connection:
        rows = connection.execute(
            "SELECT rowid, tile_data FROM tiles ORDER BY rowid"
        ).fetchall()
        for rowid, payload in rows:
            decoded = mapbox_vector_tile.decode(gzip.decompress(bytes(payload)))
            layer = decoded["collection_streets"]
            features = copy.deepcopy(layer["features"])
            for feature in features:
                feature_id = str(feature["properties"]["id"])
                feature["properties"] = expected_properties[feature_id]
            encoded = mapbox_vector_tile.encode(
                {"name": "collection_streets", "features": features},
                default_options={
                    "quantize_bounds": (0, 0, 4096, 4096),
                    "y_coord_down": True,
                },
            )
            connection.execute(
                "UPDATE tiles SET tile_data = ? WHERE rowid = ?",
                (gzip.compress(encoded, compresslevel=9, mtime=0), rowid),
            )

        tilejson = json.loads(
            connection.execute(
                "SELECT value FROM metadata WHERE name = 'json'"
            ).fetchone()[0]
        )
        tilejson["vector_layers"][0]["fields"] = {
            name: "String"
            for name in sorted(next(iter(expected_properties.values())))
        }
        connection.execute(
            "UPDATE metadata SET value = '3' WHERE name = 'tile_schema_revision'"
        )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE name = 'json'",
            (json.dumps(tilejson, sort_keys=True, separators=(",", ":")),),
        )
        sizes_by_zoom: dict[int, list[tuple[int, int]]] = {}
        for zoom, raw in connection.execute(
            "SELECT zoom_level, tile_data FROM tiles ORDER BY zoom_level"
        ):
            compressed = bytes(raw)
            sizes_by_zoom.setdefault(zoom, []).append(
                (len(compressed), len(gzip.decompress(compressed)))
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE name = 'tile_size_metrics'",
            (
                json.dumps(
                    _tile_size_metrics(sizes_by_zoom, 10),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        connection.commit()

    database_summary = manifest["database"]
    tileset_summary = validate_tileset(
        tileset,
        version,
        expected_database=database_summary,
        expected_database_path=database,
    )
    report_path = bundle / "tile_build_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "tile_count": tileset_summary["tile_count"],
            "tile_schema_revision": 3,
            "minzoom": tileset_summary["minzoom"],
            "maxzoom": tileset_summary["maxzoom"],
            "maxzoom_feature_count": tileset_summary["maxzoom_feature_count"],
            "maxzoom_nonrenderable_feature_count": tileset_summary[
                "maxzoom_nonrenderable_feature_count"
            ],
            "maxzoom_nonrenderable_feature_ids_sha256": tileset_summary[
                "maxzoom_nonrenderable_feature_ids_sha256"
            ],
            "maxzoom_unknown_feature_count": tileset_summary[
                "maxzoom_unknown_feature_count"
            ],
            "maxzoom_nonrenderable_unknown_feature_count": tileset_summary[
                "maxzoom_nonrenderable_unknown_feature_count"
            ],
            "maxzoom_nonrenderable_unknown_ids_sha256": tileset_summary[
                "maxzoom_nonrenderable_unknown_ids_sha256"
            ],
            "maxzoom_nonrenderable_features": tileset_summary[
                "maxzoom_nonrenderable_features"
            ],
            "maxzoom_nonrenderable_unknowns": tileset_summary[
                "maxzoom_nonrenderable_unknowns"
            ],
            "tile_feature_count": tileset_summary["decoded_tile_feature_count"],
            "simplify_pixels": tileset_summary["simplify_pixels"],
            "buffer_pixels": tileset_summary["buffer_pixels"],
            "tile_size_metrics": tileset_summary["tile_size_metrics"],
            "tile_size_limits": tileset_summary["tile_size_limits"],
            "bounds": tileset_summary["bounds"],
            "sha256": tileset_summary["sha256"],
            "bytes": tileset_summary["bytes"],
        }
    )
    atomic_json(report_path, report)

    manifest["tileset"] = tileset_summary
    manifest["artifacts"]["tileset"] = _artifact(
        tileset,
        version=version,
        tile_schema_revision=3,
        feature_count=1,
        source_database_sha256=database_summary["sha256"],
    )
    manifest["artifacts"]["tile_build_report"] = _artifact(report_path)
    atomic_json(manifest_path, manifest)


def test_release_artifacts_reconcile_and_are_cross_bound(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-1")

    validated = validate_release_bundle(bundle)

    assert validated["database"]["block_faces"] == 1
    assert validated["tileset"]["version"] == "release-1"
    assert validated["audit"]["classified_sides"] == 2


def test_processed_snapshot_loads_without_a_second_normalization(tmp_path, monkeypatch) -> None:
    processed = tmp_path / "citywide.geojson"
    processed.write_text(json.dumps(_processed_payload()), encoding="utf-8")
    calls = 0
    original = release_validation.prepare_payload

    def counted_prepare(payload):
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(release_validation, "prepare_payload", counted_prepare)
    snapshot = validate_and_prepare_processed_geojson(processed)
    database = tmp_path / "app.sqlite3"

    assert load_prepared_payload(snapshot.prepared, database) == 1
    assert calls == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0] == 1


def test_prevalidated_handoff_skips_repeated_component_scans_and_publication_validation(
    tmp_path,
    monkeypatch,
) -> None:
    bundle = _bundle(tmp_path, "prevalidated-handoff")
    first = validate_release_bundle(bundle)
    components = PrevalidatedReleaseComponents(
        processed=first["processed"],
        audit=first["audit"],
        database=first["database"],
        tileset=first["tileset"],
        tile_report=first["tile_report"],
    )

    def unexpected_repeat(*_args, **_kwargs):
        raise AssertionError("heavy component validation repeated")

    for name in (
        "validate_processed_geojson",
        "validate_ingestion_audit",
        "validate_database",
        "validate_processed_database_semantics",
        "validate_tileset",
    ):
        monkeypatch.setattr(release_validation, name, unexpected_repeat)
    final_gate = validate_release_bundle(
        bundle,
        prevalidated_components=components,
    )
    pointer = tmp_path / "data" / "data_manifest.json"
    monkeypatch.setattr(
        release_validation,
        "validate_release_bundle",
        unexpected_repeat,
    )

    published = publish_release(bundle, pointer, validated_bundle=final_gate)

    assert published["dataset_version"] == "prevalidated-handoff"
    assert not bundle.exists()
    assert (tmp_path / "data" / "releases" / "prevalidated-handoff").is_dir()


def test_release_gate_rejects_inconsistent_fatal_count(tmp_path) -> None:
    audit_path = tmp_path / "audit.json"
    processed_hash = "a" * 64
    audit = _audit(processed_hash)
    audit["fatal_count"] = 1
    atomic_json(audit_path, audit)

    with pytest.raises(RuntimeError, match="fatal_count is inconsistent"):
        validate_ingestion_audit(audit_path)


def test_mixed_valid_artifacts_are_rejected_by_database_tile_binding(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-mixed")
    database = bundle / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO dataset_metadata VALUES ('unrelated', 'change')")
    manifest_path = bundle / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database_hash = file_sha256(database)
    manifest["artifacts"]["database"]["sha256"] = database_hash
    manifest["database"]["sha256"] = database_hash
    manifest["database"]["bytes"] = database.stat().st_size
    atomic_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="MBTiles/database binding mismatch"):
        validate_release_bundle(bundle)


def test_tileset_validation_rejects_corrupt_gzip_payload(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-corrupt")
    tileset = bundle / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        connection.execute("UPDATE tiles SET tile_data = x'1f8b0000' WHERE rowid = 1")

    with pytest.raises(RuntimeError, match="invalid gzip"):
        validate_tileset(tileset)


def test_tileset_validation_rejects_wrong_vector_layer_schema(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-schema")
    tileset = bundle / "collection_streets.mbtiles"
    wrong_layer = mapbox_vector_tile.encode(
        {
            "name": "wrong_layer",
            "features": [
                {
                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                    "properties": {"id": "wrong"},
                }
            ],
        },
        default_options={"quantize_bounds": (0, 0, 1, 1)},
    )
    with sqlite3.connect(tileset) as connection:
        connection.execute(
            "UPDATE tiles SET tile_data = ? WHERE rowid = 1",
            (gzip.compress(wrong_layer, mtime=0),),
        )

    with pytest.raises(RuntimeError, match="unexpected vector layers"):
        validate_tileset(tileset)


def test_tileset_validation_rejects_maxzoom_count_mismatch(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-count")
    tileset = bundle / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        connection.execute(
            "UPDATE metadata SET value = '2' WHERE name = 'maxzoom_feature_count'"
        )

    with pytest.raises(RuntimeError, match="maxzoom renderability"):
        validate_tileset(tileset)


def test_tileset_validation_reconciles_subgrid_unknown_against_database(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-subgrid-unknown")
    database = bundle / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO unknown_block_faces
               (unknown_id, technical_identity, segment_id, borough, street_name, side,
                reason_code, reason, identity_method, geometry_method, geometry_wkt,
                min_x, min_y, max_x, max_y, evidence_json)
               VALUES ('unknown-tiny', 'LION:3:RIGHT', '3', 'QUEENS', 'TINY GAP', 'RIGHT',
                       'PARTIAL_GEOMETRY_GAP', 'Tiny uncovered source fragment',
                       'LION_BLOCK_FACE_ID', 'DIRECT_SIDE_TRACE_UNRESOLVED',
                       'LINESTRING (-74 40.6, -73.999999999 40.600000001)',
                       -74, 40.6, -73.999999999, 40.600000001, '{}')"""
        )
    database_summary = validate_database(database)
    tileset = tmp_path / "subgrid-unknown.mbtiles"
    report = build_tiles(
        database,
        tileset,
        minzoom=16,
        maxzoom=16,
        version="release-subgrid-unknown",
        source_version="release-subgrid-unknown",
        simplify_pixels=0,
    ).as_dict()

    summary = validate_tileset(
        tileset,
        "release-subgrid-unknown",
        expected_database=database_summary,
        expected_database_path=database,
    )

    assert summary["unknown_feature_count"] == 1
    assert summary["maxzoom_unknown_feature_count"] == 0
    assert summary["maxzoom_nonrenderable_unknown_feature_count"] == 1
    assert [item["id"] for item in summary["maxzoom_nonrenderable_unknowns"]] == [
        "unknown-tiny"
    ]
    report_path = tmp_path / "subgrid-tile-report.json"
    atomic_json(report_path, report)
    validated_report = validate_tile_build_report(
        report_path,
        expected_version="release-subgrid-unknown",
        database=database_summary,
        tileset=summary,
    )
    assert validated_report["maxzoom_nonrenderable_unknown_feature_count"] == 1

    with sqlite3.connect(tileset) as connection:
        connection.execute(
            "UPDATE metadata SET value = ? "
            "WHERE name = 'maxzoom_nonrenderable_unknown_ids_sha256'",
            ("0" * 64,),
        )
    with pytest.raises(RuntimeError, match="digest does not match missing IDs"):
        validate_tileset(
            tileset,
            "release-subgrid-unknown",
            expected_database=database_summary,
            expected_database_path=database,
        )


def test_nonrenderable_exception_rejects_ordinary_renderable_geometry() -> None:
    properties = {
        "face-1": {
            "id": "face-1",
            "street_name": "VISIBLE STREET",
            "side": "LEFT",
        }
    }
    geometries = {
        "face-1": LineString([(-74.0, 40.6), (-73.99, 40.61)])
    }

    with pytest.raises(RuntimeError, match="omitted renderable maxzoom geometry"):
        _validated_nonrenderable_records(
            properties,
            geometries,
            set(),
            declared_count=1,
            declared_sha256=hashlib.sha256(b"face-1\n").hexdigest(),
            maxzoom=17,
            buffer_pixels=16,
            unknown=False,
        )


def test_tileset_validation_rejects_runtime_invalid_bounds(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-invalid-bounds")
    tileset = bundle / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        connection.execute(
            "UPDATE metadata SET value = '-73.0,40.9,-74.0,40.5' WHERE name = 'bounds'"
        )

    with pytest.raises(RuntimeError, match="ordered geographic bounds"):
        validate_tileset(tileset)


def test_processed_database_semantics_reject_same_count_substitution(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-db-semantic")
    processed = validate_processed_geojson(bundle / "citywide.geojson")
    database = bundle / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE block_faces SET street_name = 'SUBSTITUTED STREET' WHERE block_face_id = 'face-1'"
        )

    with pytest.raises(RuntimeError, match="semantic reconciliation failed"):
        validate_processed_database_semantics(processed, database)


def test_processed_database_semantics_normalizes_sqlite_weekday_order(tmp_path) -> None:
    """SQLite's lexical ORDER BY must not change the calendar schedule contract."""

    bundle = _bundle(tmp_path, "release-weekday-order")
    processed_path = bundle / "citywide.geojson"
    payload = _processed_payload()
    properties = payload["features"][0]["properties"]
    properties["refuse_days"] = ["WED", "SAT"]
    properties["schedules"]["REFUSE"] = ["WED", "SAT"]
    processed_path.write_text(json.dumps(payload), encoding="utf-8")
    processed = validate_processed_geojson(processed_path)

    database = bundle / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM collection_schedules "
            "WHERE block_face_id = 'face-1' AND collection_type = 'REFUSE'"
        )
        connection.executemany(
            """INSERT INTO collection_schedules
               (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
               VALUES ('face-1', 'REFUSE', ?, 'DSNY', '2026-08-19', 'AUDITED_SIDE_OFFSET')""",
            [(day,) for day in ("WED", "SAT")],
        )

    assert validate_processed_database_semantics(processed, database)["semantic_feature_count"] == 1


def test_tileset_semantics_reject_weekday_tamper_with_matching_counts(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-tile-semantic")
    database = bundle / "app.sqlite3"
    tileset = bundle / "collection_streets.mbtiles"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE collection_schedules SET weekday = 'TUE'
               WHERE block_face_id = 'face-1' AND collection_type = 'REFUSE'"""
        )
    database_summary = validate_database(database)
    with sqlite3.connect(tileset) as connection:
        connection.execute(
            "UPDATE metadata SET value = ? WHERE name = 'source_database_sha256'",
            (database_summary["sha256"],),
        )

    with pytest.raises(RuntimeError, match="semantic content"):
        validate_tileset(
            tileset,
            "release-tile-semantic",
            expected_database=database_summary,
            expected_database_path=database,
        )


def test_tileset_semantics_reject_low_zoom_only_ghost(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-low-zoom-ghost")
    database = bundle / "app.sqlite3"
    tileset = bundle / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        rowid, payload = connection.execute(
            "SELECT rowid, tile_data FROM tiles WHERE zoom_level = 10 LIMIT 1"
        ).fetchone()
        decoded = mapbox_vector_tile.decode(gzip.decompress(bytes(payload)))
        features = decoded["collection_streets"]["features"]
        ghost = copy.deepcopy(features[0])
        ghost["properties"]["id"] = "low-zoom-ghost"
        ghost["properties"]["origin_block_face_id"] = "low-zoom-ghost"
        features.append(ghost)
        encoded = mapbox_vector_tile.encode(
            {"name": "collection_streets", "features": features},
            default_options={"quantize_bounds": (0, 0, 4096, 4096), "y_coord_down": True},
        )
        connection.execute(
            "UPDATE tiles SET tile_data = ? WHERE rowid = ?",
            (gzip.compress(encoded, mtime=0), rowid),
        )
        sizes_by_zoom: dict[int, list[tuple[int, int]]] = {}
        for zoom, raw in connection.execute(
            "SELECT zoom_level, tile_data FROM tiles ORDER BY zoom_level"
        ):
            compressed = bytes(raw)
            sizes_by_zoom.setdefault(zoom, []).append(
                (len(compressed), len(gzip.decompress(compressed)))
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE name = 'tile_size_metrics'",
            (json.dumps(_tile_size_metrics(sizes_by_zoom, 10), sort_keys=True),),
        )
    database_summary = validate_database(database)

    with pytest.raises(RuntimeError, match="unexpected_ids=.*low-zoom-ghost"):
        validate_tileset(
            tileset,
            "release-low-zoom-ghost",
            expected_database=database_summary,
            expected_database_path=database,
        )


def test_release_bundle_rejects_unexpected_files_and_audit_outcomes(tmp_path) -> None:
    bundle = _bundle(tmp_path, "release-extra")
    (bundle / "unexpected.txt").write_text("not part of the release", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected files"):
        validate_release_bundle(bundle)

    (bundle / "unexpected.txt").unlink()
    audit_path = bundle / "ingestion_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["outcomes"]["new_unreviewed_classification"] = 0
    atomic_json(audit_path, audit)
    with pytest.raises(RuntimeError, match="unexpected schema"):
        validate_ingestion_audit(audit_path)

    bundle = _bundle(tmp_path, "release-extra-descriptor")
    manifest_path = bundle / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["database"]["unreviewed_binding"] = "value"
    atomic_json(manifest_path, manifest)
    with pytest.raises(RuntimeError, match="artifact descriptor has an unexpected schema"):
        validate_release_bundle(bundle)


def test_atomic_pointer_selects_only_current_and_retained_previous(tmp_path) -> None:
    pointer = tmp_path / "data" / "data_manifest.json"
    first = _bundle(tmp_path, "release-1")
    second = _bundle(tmp_path, "release-2")
    uncommitted = _bundle(tmp_path, "release-3")
    publish_release(first, pointer, retention=2)
    publish_release(second, pointer, retention=2)
    shutil.copytree(uncommitted, pointer.parent / "releases" / "release-3")

    current = read_current_release(pointer)
    assert current is not None
    assert current.dataset_version == "release-2"
    assert tileset_for_version(current, "release-1") is not None
    assert tileset_for_version(current, "release-3") is None

    activate_release("release-1", pointer, retention=2)
    rolled_back = read_current_release(pointer)
    assert rolled_back is not None
    assert rolled_back.dataset_version == "release-1"
    assert tileset_for_version(rolled_back, "release-2") is not None


def test_atomic_pointer_can_roll_back_to_retained_v3_tiles(tmp_path) -> None:
    pointer = tmp_path / "data" / "data_manifest.json"
    previous = _bundle(tmp_path, "schema-v3")
    _rewrite_bundle_tileset_as_v3(previous)
    current = _bundle(tmp_path, "schema-v4")

    assert validate_release_bundle(previous)["tileset"]["tile_schema_revision"] == 3
    publish_release(previous, pointer, retention=2)
    publish_release(current, pointer, retention=2)
    activate_release("schema-v3", pointer, retention=2)

    rolled_back = read_current_release(pointer)
    assert rolled_back is not None
    assert rolled_back.dataset_version == "schema-v3"
    assert rolled_back.manifest["tileset"]["tile_schema_revision"] == 3
    assert tileset_for_version(rolled_back, "schema-v4") is not None


def test_concurrent_promotions_keep_a_nondangling_current_and_history(tmp_path) -> None:
    pointer = tmp_path / "data" / "data_manifest.json"
    first = _bundle(tmp_path, "concurrent-1")
    second = _bundle(tmp_path, "concurrent-2")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda bundle: publish_release(bundle, pointer, retention=2),
                (first, second),
            )
        )

    assert {result["dataset_version"] for result in results} == {"concurrent-1", "concurrent-2"}
    current = read_current_release(pointer)
    assert current is not None
    versions = {current.dataset_version, *(item.dataset_version for item in current.previous_tilesets)}
    assert versions == {"concurrent-1", "concurrent-2"}
    assert current.database_path.is_file()
    assert current.tileset_path.is_file()
    assert all(item.path.is_file() for item in current.previous_tilesets)


def test_manifest_path_traversal_fails_closed(tmp_path) -> None:
    pointer = tmp_path / "data_manifest.json"
    pointer.write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "dataset_version": "release-1",
                "release_path": "../release-1",
                "artifacts": {
                    "database": {"path": "app.sqlite3", "sha256": "a" * 64},
                    "tileset": {
                        "path": "collection_streets.mbtiles",
                        "sha256": "b" * 64,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManifestError, match="release_path must be"):
        read_current_release(pointer)


@pytest.mark.parametrize(
    "manifest",
    [
        {"manifest_version": 2, "dataset_version": "truncated-v2"},
        {"dataset_version": "missing-version"},
        {"manifest_version": 3, "dataset_version": "future-version"},
        {"manifest_version": 2.0, "dataset_version": "float-version"},
    ],
)
def test_truncated_or_unknown_manifest_never_falls_back(tmp_path, manifest) -> None:
    pointer = tmp_path / "data_manifest.json"
    pointer.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReleaseManifestError):
        read_current_release(pointer)


def test_explicit_v1_manifest_uses_legacy_paths(tmp_path) -> None:
    pointer = tmp_path / "data_manifest.json"
    pointer.write_text(json.dumps({"manifest_version": 1}), encoding="utf-8")

    assert read_current_release(pointer) is None


def test_regression_gate_rejects_a_self_consistent_large_drop() -> None:
    current = {
        "counts": {
            "raw_lion_rows": 200_000,
            "dsny_frequency_rows": 500,
            "eligible_lion_rows": 180_000,
            "matched_sides": 170_000,
            "used_frequency_rows": 490,
            "output_features": 100_000,
            "schedule_rows_by_type": {
                "REFUSE": 100_000,
                "RECYCLING": 90_000,
                "ORGANICS": 50_000,
                "BULK": 80_000,
            },
        }
    }
    candidate = {
        "raw_lion_rows": 150_000,
        "dsny_frequency_rows": 500,
        "eligible_lion_rows": 180_000,
        "matched_sides": 170_000,
        "used_frequency_rows": 490,
        "output_features": 95_000,
        "schedule_rows_by_type": {
            "REFUSE": 100_000,
            "RECYCLING": 90_000,
            "ORGANICS": 50_000,
            "BULK": 80_000,
        },
    }

    with pytest.raises(RuntimeError, match="raw_lion_rows dropped 25.00%"):
        validate_regression_gates(
            candidate,
            current,
            min_lion_rows=100_000,
            min_dsny_rows=100,
            min_output_features=50_000,
        )


def test_regression_gate_rejects_truncated_first_release_against_safe_floors() -> None:
    counts = {
        "raw_lion_rows": 199_999,
        "dsny_frequency_rows": 610,
        "eligible_lion_rows": 150_000,
        "matched_sides": 150_000,
        "used_frequency_rows": 610,
        "output_features": 120_000,
        "schedule_rows_by_type": {
            "REFUSE": 120_000,
            "RECYCLING": 100_000,
            "ORGANICS": 50_000,
            "BULK": 80_000,
        },
    }

    with pytest.raises(RuntimeError, match="raw_lion_rows=199999 floor=200000"):
        validate_regression_gates(counts, None)


def test_regression_gate_rejects_one_collection_type_disappearing() -> None:
    base_counts = {
        "raw_lion_rows": 200_000,
        "dsny_frequency_rows": 500,
        "eligible_lion_rows": 180_000,
        "matched_sides": 170_000,
        "used_frequency_rows": 490,
        "output_features": 100_000,
        "schedule_rows_by_type": {
            "REFUSE": 100_000,
            "RECYCLING": 90_000,
            "ORGANICS": 50_000,
            "BULK": 80_000,
        },
    }
    candidate = {**base_counts, "schedule_rows_by_type": {**base_counts["schedule_rows_by_type"], "ORGANICS": 1}}

    with pytest.raises(RuntimeError, match=r"schedule_rows_by_type\.ORGANICS dropped"):
        validate_regression_gates(candidate, {"counts": base_counts})
