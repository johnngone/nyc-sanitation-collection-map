import gzip
import hashlib
import json
import sqlite3

import mapbox_vector_tile
import pytest
from shapely.geometry import LineString, box
from fastapi.testclient import TestClient

from app import api
from app.database import initialize
from app.main import app
from scripts.build_tiles import _tile_geometry_with_source_fallback, build_tiles


def _write_mbtiles(
    path,
    *,
    version: str = "dataset-v1",
    tile: bytes | None = b"encoded-pbf",
    tile_schema_revision: int = 2,
) -> None:
    layer_metadata = {
        "vector_layers": [
            {
                "id": "collection_streets",
                "fields": {"id": "String"},
                "minzoom": 1,
                "maxzoom": 2,
            }
        ]
    }
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        connection.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        connection.executemany(
            "INSERT INTO metadata(name, value) VALUES (?, ?)",
            [
                ("name", "test"),
                ("format", "pbf"),
                ("version", version),
                ("tile_schema_revision", str(tile_schema_revision)),
                ("source_layer", "collection_streets"),
                ("minzoom", "1"),
                ("maxzoom", "2"),
                ("bounds", "-74.3,40.4,-73.6,40.95"),
                ("data_updated", "2026-08-19T12:00:00Z"),
                ("json", json.dumps(layer_metadata)),
            ],
        )
        if tile is not None:
            # XYZ 1/0/0 maps to the MBTiles/TMS row 1.
            connection.execute(
                "INSERT INTO tiles VALUES (1, 0, 1, ?)",
                (gzip.compress(tile, mtime=0),),
            )


def _source_database(path, *, include_schedule: bool = True) -> None:
    initialize(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO block_faces
               (block_face_id, origin_block_face_id, segment_id, borough, street_name, side, geometry_wkt,
                min_x, min_y, max_x, max_y)
               VALUES ('bf-1', 'bf-1', 'seg-1', 'BROOKLYN', 'EXAMPLE STREET', 'LEFT',
                       'LINESTRING (-74.0 40.6, -73.99 40.61)',
                       -74.0, 40.6, -73.99, 40.61)"""
        )
        if include_schedule:
            connection.executemany(
                """INSERT INTO collection_schedules
                   (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                   VALUES ('bf-1', ?, ?, 'DSNY', '2026-08-19', 'VALIDATED')""",
                [("REFUSE", "THU"), ("REFUSE", "MON"), ("RECYCLING", "TUE")],
            )
            connection.executemany(
                """INSERT INTO block_face_collection_states
                   (block_face_id, collection_type, effective_days_json, state,
                    source_field, raw_value, rule_id, source_policy_conflict, provenance)
                   VALUES ('bf-1', ?, ?, ?, ?, ?, NULL, 0, 'DSNY test fixture')""",
                [
                    ("REFUSE", '["MON","THU"]', "SOURCE_EXPLICIT", "FREQ_REFUSE", "MON,THU"),
                    ("RECYCLING", '["TUE"]', "SOURCE_EXPLICIT", "FREQ_RECYCLING", "TUE"),
                    ("ORGANICS", '[]', "UNKNOWN_SOURCE_BLANK", "FREQ_ORGANICS", None),
                    ("BULK", '[]', "UNKNOWN_SOURCE_BLANK", "FREQ_BULK", None),
                ],
            )


def test_map_config_and_tile_response(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "streets.mbtiles"
    _write_mbtiles(archive)
    monkeypatch.setattr(api, "TILESET_PATH", str(archive))
    client = TestClient(app)

    config_response = client.get("/api/map-config")
    assert config_response.status_code == 200
    assert config_response.json() == {
        "available": True,
        "version": "dataset-v1",
        "tile_schema_revision": 2,
        "tiles_url": "/api/tiles/dataset-v1/{z}/{x}/{y}.pbf",
        "source_layer": "collection_streets",
        "known_source_layer": "collection_streets",
        "unknown_source_layer": None,
        "unknown_minzoom": None,
        "minzoom": 1,
        "maxzoom": 2,
        "bounds": [-74.3, 40.4, -73.6, 40.95],
        "data_updated": "2026-08-19T12:00:00Z",
    }
    assert config_response.headers["cache-control"] == "no-cache"

    tile_response = client.get("/api/tiles/dataset-v1/1/0/0.pbf")
    assert tile_response.status_code == 200
    assert tile_response.content == b"encoded-pbf"
    assert tile_response.headers["content-type"].startswith("application/vnd.mapbox-vector-tile")
    assert tile_response.headers["content-encoding"] == "gzip"
    assert tile_response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert tile_response.headers["etag"] == '"dataset-v1-1-0-0"'

    cached_response = client.get(
        "/api/tiles/dataset-v1/1/0/0.pbf",
        headers={"If-None-Match": tile_response.headers["etag"]},
    )
    assert cached_response.status_code == 304


def test_tile_route_validates_version_coordinates_and_missing_tiles(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "streets.mbtiles"
    _write_mbtiles(archive)
    monkeypatch.setattr(api, "TILESET_PATH", str(archive))
    client = TestClient(app)

    assert client.get("/api/tiles/old/1/0/0.pbf").status_code == 404
    assert client.get("/api/tiles/dataset-v1/0/0/0.pbf").status_code == 404
    assert client.get("/api/tiles/dataset-v1/1/2/0.pbf").status_code == 404
    assert client.get("/api/tiles/dataset-v1/1/0/1.pbf").status_code == 204


def test_manifest_serves_current_and_retained_previous_but_not_uncommitted(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    current_dir = data / "releases" / "current-v2"
    previous_dir = data / "releases" / "previous-v2"
    uncommitted_dir = data / "releases" / "uncommitted-v2"
    for directory, version, tile in (
        (current_dir, "current-v2", b"current"),
        (previous_dir, "previous-v2", b"previous"),
        (uncommitted_dir, "uncommitted-v2", b"uncommitted"),
    ):
        directory.mkdir(parents=True)
        _write_mbtiles(directory / "collection_streets.mbtiles", version=version, tile=tile)
    current_database = current_dir / "app.sqlite3"
    current_database.write_bytes(b"bound-current-database")
    current_tileset = current_dir / "collection_streets.mbtiles"
    previous_tileset = previous_dir / "collection_streets.mbtiles"
    pointer = data / "data_manifest.json"
    pointer.write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "dataset_version": "current-v2",
                "release_path": "releases/current-v2",
                "artifacts": {
                    "database": {
                        "path": "app.sqlite3",
                        "sha256": hashlib.sha256(current_database.read_bytes()).hexdigest(),
                    },
                    "tileset": {
                        "path": "collection_streets.mbtiles",
                        "sha256": hashlib.sha256(current_tileset.read_bytes()).hexdigest(),
                    },
                },
                "previous_releases": [
                    {
                        "dataset_version": "previous-v2",
                        "release_path": "releases/previous-v2",
                        "artifacts": {
                            "tileset": {
                                "path": "collection_streets.mbtiles",
                                "sha256": hashlib.sha256(previous_tileset.read_bytes()).hexdigest(),
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))
    monkeypatch.setattr(api, "TILESET_PATH", str(tmp_path / "legacy-must-not-be-used.mbtiles"))
    client = TestClient(app)

    assert client.get("/api/map-config").json()["version"] == "current-v2"
    assert client.get("/api/tiles/current-v2/1/0/0.pbf").content == b"current"
    assert client.get("/api/tiles/previous-v2/1/0/0.pbf").content == b"previous"
    assert client.get("/api/tiles/uncommitted-v2/1/0/0.pbf").status_code == 404


def test_map_config_reports_unavailable_archive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "TILESET_PATH", str(tmp_path / "missing.mbtiles"))
    response = TestClient(app).get("/api/map-config")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["tiles_url"] is None


def test_builder_writes_one_feature_per_geometry_with_size_and_source_binding(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)

    report = build_tiles(
        database,
        archive,
        minzoom=11,
        maxzoom=11,
        version="source-v7",
        source_version="database-v3",
        data_updated="2026-08-19T12:00:00Z",
    )

    assert report.version == "source-v7"
    assert report.tile_schema_revision == 3
    assert report.feature_count == 1
    assert report.geometry_count == 1
    assert report.maxzoom_feature_count == 1
    assert report.maxzoom_nonrenderable_feature_count == 0
    assert report.maxzoom_nonrenderable_features == []
    assert report.source_block_face_count == 1
    assert report.source_schedule_count == 3
    assert report.source_schedule_group_count == 2
    assert report.source_database_version == "database-v3"
    assert report.source_database_sha256 == hashlib.sha256(database.read_bytes()).hexdigest()
    assert report.tile_count > 0
    assert report.sha256
    with sqlite3.connect(archive) as connection:
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        tile_rows = connection.execute("SELECT tile_data FROM tiles").fetchall()
    assert metadata["format"] == "pbf"
    assert metadata["version"] == "source-v7"
    assert metadata["tile_schema_revision"] == "3"
    assert metadata["source_database_sha256"] == report.source_database_sha256
    assert metadata["source_database_version"] == "database-v3"
    assert metadata["source_block_face_count"] == "1"
    assert metadata["source_schedule_count"] == "3"
    assert metadata["source_schedule_group_count"] == "2"
    assert metadata["feature_count"] == "1"
    assert metadata["geometry_count"] == "1"
    assert metadata["maxzoom_feature_count"] == "1"
    assert metadata["maxzoom_nonrenderable_feature_count"] == "0"
    assert metadata["maxzoom_nonrenderable_unknown_feature_count"] == "0"
    assert metadata["source_layer"] == "collection_streets"
    assert metadata["data_updated"] == "2026-08-19T12:00:00Z"
    assert all(row[0].startswith(b"\x1f\x8b") for row in tile_rows)

    properties = []
    compressed_sizes = []
    uncompressed_sizes = []
    for (compressed,) in tile_rows:
        uncompressed = gzip.decompress(compressed)
        compressed_sizes.append(len(compressed))
        uncompressed_sizes.append(len(uncompressed))
        decoded = mapbox_vector_tile.decode(uncompressed)
        tile_properties = [
            feature["properties"] for feature in decoded["collection_streets"]["features"]
        ]
        assert len({item["id"] for item in tile_properties}) == len(tile_properties)
        properties.extend(tile_properties)
    assert properties
    assert all(item["refuse_days"] == "MON,THU" for item in properties)
    assert all(item["recycling_days"] == "TUE" for item in properties)
    assert all(item["organics_days"] == "" for item in properties)
    assert all(item["bulk_days"] == "" for item in properties)
    assert all("collection_type" not in item and "days" not in item for item in properties)
    assert all(item["source"] == "DSNY" for item in properties)
    assert all(item["retrieved_at"] == "2026-08-19" for item in properties)
    assert all(
        {
            "id",
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
        }.issubset(item)
        for item in properties
    )
    assert report.tile_feature_count == len(properties)
    assert report.tile_size_metrics["max_compressed_tile_bytes"] == max(compressed_sizes)
    assert report.tile_size_metrics["max_uncompressed_tile_bytes"] == max(uncompressed_sizes)
    assert report.tile_size_metrics["initial_zoom_compressed_bytes"] == sum(compressed_sizes)
    assert report.tile_size_metrics["initial_zoom_uncompressed_bytes"] == sum(uncompressed_sizes)
    assert report.tile_size_metrics["initial_zoom_tile_count"] == len(tile_rows)
    assert json.loads(metadata["tile_size_metrics"]) == report.tile_size_metrics
    assert json.loads(metadata["tile_size_limits"]) == report.tile_size_limits


def test_builder_rejects_unscheduled_block_faces(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    _source_database(database, include_schedule=False)
    with pytest.raises(RuntimeError, match="must contain block faces and collection schedules"):
        build_tiles(database, tmp_path / "collection.mbtiles", minzoom=11, maxzoom=11)


def test_builder_rejects_conflicting_schedule_provenance(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    _source_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE collection_schedules SET source = 'OTHER'
               WHERE block_face_id = 'bf-1' AND collection_type = 'REFUSE' AND weekday = 'THU'"""
        )
    with pytest.raises(RuntimeError, match="conflicting provenance"):
        build_tiles(database, tmp_path / "collection.mbtiles", minzoom=11, maxzoom=11)


def test_builder_exposes_optional_origin_id_without_replacing_internal_id(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE block_faces SET origin_block_face_id = 'lion-left-100' WHERE block_face_id = 'bf-1'"
        )

    build_tiles(database, archive, minzoom=11, maxzoom=11)

    with sqlite3.connect(archive) as connection:
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        compressed = connection.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()[0]
    feature = mapbox_vector_tile.decode(gzip.decompress(compressed))["collection_streets"]["features"][0]
    assert feature["properties"]["id"] == "bf-1"
    assert feature["properties"]["origin_block_face_id"] == "lion-left-100"
    fields = json.loads(metadata["json"])["vector_layers"][0]["fields"]
    assert fields["origin_block_face_id"] == "String"


@pytest.mark.parametrize(
    ("limit_arguments", "message"),
    [
        ({"max_uncompressed_tile_bytes": 1}, "uncompressed vector tile exceeds build gate"),
        ({"max_compressed_tile_bytes": 1}, "compressed vector tile exceeds build gate"),
    ],
)
def test_builder_fails_oversize_before_publication(tmp_path, limit_arguments, message) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)
    archive.write_bytes(b"previous-valid-release")

    with pytest.raises(RuntimeError, match=message):
        build_tiles(database, archive, minzoom=11, maxzoom=11, **limit_arguments)

    assert archive.read_bytes() == b"previous-valid-release"


def test_builder_gzip_tile_bytes_and_metrics_are_deterministic(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    first_archive = tmp_path / "first.mbtiles"
    second_archive = tmp_path / "second.mbtiles"
    _source_database(database)

    first = build_tiles(database, first_archive, minzoom=11, maxzoom=12)
    second = build_tiles(database, second_archive, minzoom=11, maxzoom=12)

    def tile_rows(path):
        with sqlite3.connect(path) as connection:
            return connection.execute(
                """SELECT zoom_level, tile_column, tile_row, tile_data
                   FROM tiles ORDER BY zoom_level, tile_column, tile_row"""
            ).fetchall()

    assert tile_rows(first_archive) == tile_rows(second_archive)
    assert first.version == second.version
    assert first.tile_size_metrics == second.tile_size_metrics


def test_builder_reconciles_subgrid_scheduled_face_without_blocking_release(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE block_faces
               SET geometry_wkt = 'LINESTRING (-74 40.6, -73.999999999 40.600000001)',
                   min_x = -74, min_y = 40.6,
                   max_x = -73.999999999, max_y = 40.600000001
               WHERE block_face_id = 'bf-1'"""
        )
        connection.execute(
            """INSERT INTO unknown_block_faces
               (unknown_id, technical_identity, segment_id, borough, street_name, side,
                reason_code, reason, identity_method, geometry_method, geometry_wkt,
                min_x, min_y, max_x, max_y, evidence_json)
               VALUES ('unknown-visible', 'LION:2:LEFT', '2', 'BROOKLYN', 'VISIBLE GAP', 'LEFT',
                       'OUTSIDE_DSNY_COVERAGE', 'No exact polygon coverage', 'UNRESOLVED',
                       'DIRECT_SIDE_TRACE_UNRESOLVED',
                       'LINESTRING (-74.0 40.6, -73.99 40.61)',
                       -74.0, 40.6, -73.99, 40.61, '{}')"""
        )

    report = build_tiles(database, archive, minzoom=16, maxzoom=16, simplify_pixels=0)

    assert report.feature_count == 1
    assert report.maxzoom_feature_count == 0
    assert report.maxzoom_nonrenderable_feature_count == 1
    assert [record["id"] for record in report.maxzoom_nonrenderable_features] == ["bf-1"]
    assert report.maxzoom_unknown_feature_count == 1


def test_builder_retries_exact_geometry_when_simplification_collapses_line() -> None:
    source = LineString([(0.1, 0.1), (1.1, 0.1), (0.2, 0.1)])
    simplified = source.simplify(2, preserve_topology=True)

    encoded, used_source_fallback = _tile_geometry_with_source_fallback(
        simplified,
        source,
        box(-10, -10, 4106, 4106),
        (0, 0, 4096, 4096),
    )

    assert encoded is not None
    assert used_source_fallback is True


def test_builder_reconciles_subgrid_unknown_without_fabricating_geometry(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)
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

    report = build_tiles(database, archive, minzoom=16, maxzoom=16, simplify_pixels=0)

    assert report.unknown_feature_count == 1
    assert report.maxzoom_unknown_feature_count == 0
    assert report.maxzoom_nonrenderable_unknown_feature_count == 1
    assert len(report.maxzoom_nonrenderable_unknowns) == 1
    nonrenderable = report.maxzoom_nonrenderable_unknowns[0]
    assert nonrenderable == {
        "id": "unknown-tiny",
        "street_name": "TINY GAP",
        "side": "RIGHT",
        "projected_length_meters": nonrenderable["projected_length_meters"],
        "reason": "COLLAPSES_AFTER_MVT_QUANTIZATION",
        "reason_code": "PARTIAL_GEOMETRY_GAP",
    }
    assert 0 < nonrenderable["projected_length_meters"] < 0.001
    with sqlite3.connect(archive) as connection:
        decoded_unknown_ids = {
            feature["properties"]["id"]
            for (tile_data,) in connection.execute("SELECT tile_data FROM tiles")
            for feature in mapbox_vector_tile.decode(gzip.decompress(tile_data))
            .get("collection_unknowns", {})
            .get("features", [])
        }
    assert "unknown-tiny" not in decoded_unknown_ids


def test_builder_rejects_buffer_smaller_than_frontend_style_reach(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    _source_database(database)

    with pytest.raises(ValueError, match="tile buffer must be at least 16 pixels"):
        build_tiles(
            database,
            tmp_path / "collection.mbtiles",
            minzoom=11,
            maxzoom=11,
            buffer_pixels=15.99,
        )


def test_builder_rejects_raising_hard_tile_size_ceilings(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    _source_database(database)

    with pytest.raises(ValueError, match="cannot exceed the hard release ceilings"):
        build_tiles(
            database,
            tmp_path / "collection.mbtiles",
            minzoom=11,
            maxzoom=11,
            max_compressed_tile_bytes=512_001,
        )


def test_v3_unknown_layer_has_no_schedule_properties_and_survives_maxzoom(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    archive = tmp_path / "collection.mbtiles"
    _source_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO unknown_block_faces
               (unknown_id, technical_identity, segment_id, borough, street_name, side,
                reason_code, reason, identity_method, geometry_method, geometry_wkt,
                min_x, min_y, max_x, max_y, evidence_json)
               VALUES ('unknown-1', 'LION:1:LEFT', '1', 'BROOKLYN', 'UNKNOWN STREET', 'LEFT',
                       'OUTSIDE_DSNY_COVERAGE', 'No exact polygon coverage', 'UNRESOLVED',
                       'DIRECT_SIDE_TRACE_UNRESOLVED',
                       'LINESTRING (-74.0 40.6, -73.99 40.61)',
                       -74.0, 40.6, -73.99, 40.61, '{}')"""
        )

    report = build_tiles(database, archive, minzoom=16, maxzoom=16)

    assert report.unknown_feature_count == report.maxzoom_unknown_feature_count == 1
    decoded_unknowns = []
    with sqlite3.connect(archive) as connection:
        for (tile_data,) in connection.execute("SELECT tile_data FROM tiles"):
            decoded = mapbox_vector_tile.decode(gzip.decompress(tile_data))
            decoded_unknowns.extend(decoded.get("collection_unknowns", {}).get("features", []))
    assert decoded_unknowns
    properties = decoded_unknowns[0]["properties"]
    assert properties["reason_code"] == "OUTSIDE_DSNY_COVERAGE"
    assert not any(key.endswith("_days") or key.endswith("_status") for key in properties)
