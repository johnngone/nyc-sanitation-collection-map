import json
import hashlib
import sqlite3
import time

from fastapi.testclient import TestClient

from app import api
from app.database import initialize
from app.main import app


def test_invalid_day_returns_422(tmp_path, monkeypatch) -> None:
    database = tmp_path / "app.sqlite3"
    initialize(database)
    monkeypatch.setattr(api, "DATABASE_PATH", str(database))
    response = TestClient(app).get("/api/refuse-streets?day=SUN")
    assert response.status_code == 422
    assert "day must be one of" in response.json()["detail"]


def test_refuse_geojson_filters_by_day_and_bbox(tmp_path, monkeypatch) -> None:
    database = tmp_path / "app.sqlite3"
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO block_faces
            (block_face_id, origin_block_face_id, segment_id, borough, street_name, side, geometry_wkt,
             min_x, min_y, max_x, max_y)
            VALUES ('bf-1', 'bf-1', 'seg-1', 'BROOKLYN', 'EXAMPLE STREET', 'LEFT',
                    'LINESTRING (-74.0 40.6, -73.99 40.61)', -74.0, 40.6, -73.99, 40.61)""")
        connection.execute("""INSERT INTO collection_schedules
            (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
            VALUES ('bf-1', 'REFUSE', 'MON', 'DSNY', '2026-08-03', 'VALIDATED')""")
    monkeypatch.setattr(api, "DATABASE_PATH", str(database))
    response = TestClient(app).get("/api/refuse-streets?day=MON&west=-74.1&south=40.5&east=-73.9&north=40.7")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"]["refuse_days"] == ["MON"]
    assert payload["features"][0]["properties"]["block_face_id"] == "bf-1"
    assert payload["features"][0]["properties"]["feature_id"] == "bf-1"


def test_legacy_geojson_endpoint_caps_large_responses(tmp_path, monkeypatch) -> None:
    database = tmp_path / "app.sqlite3"
    initialize(database)
    with sqlite3.connect(database) as connection:
        for index in (1, 2):
            connection.execute(
                """INSERT INTO block_faces
                   (block_face_id, origin_block_face_id, segment_id, borough, street_name,
                    side, geometry_wkt, min_x, min_y, max_x, max_y)
                   VALUES (?, ?, ?, 'QUEENS', 'TEST STREET', 'LEFT',
                           'LINESTRING (-73.9 40.7, -73.89 40.71)',
                           -73.9, 40.7, -73.89, 40.71)""",
                (f"feature-{index}", f"origin-{index}", f"segment-{index}"),
            )
            connection.execute(
                """INSERT INTO collection_schedules
                   (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                   VALUES (?, 'REFUSE', 'MON', 'DSNY', '2026-08-19', 'VALIDATED')""",
                (f"feature-{index}",),
            )
    monkeypatch.setattr(api, "DATABASE_PATH", str(database))
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(tmp_path / "missing-manifest.json"))
    monkeypatch.setattr(api, "LEGACY_FEATURE_LIMIT", 1)

    response = TestClient(app).get("/api/refuse-streets?day=MON")

    assert response.status_code == 413
    assert "smaller bounding box" in response.json()["detail"]


def test_legacy_geometry_parser_accepts_canonical_spaced_multilinestring() -> None:
    assert api._parse_geometry(
        "MULTILINESTRING ((-74 40.6, -73.99 40.61), (-73.98 40.62, -73.97 40.63))"
    ) == {
        "type": "MultiLineString",
        "coordinates": [
            [[-74.0, 40.6], [-73.99, 40.61]],
            [[-73.98, 40.62], [-73.97, 40.63]],
        ],
    }


def test_health_resolves_database_and_tiles_from_committed_manifest(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    release = data / "releases" / "health-v2"
    release.mkdir(parents=True)
    database = release / "app.sqlite3"
    initialize(database)
    tileset = release / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        connection.execute("CREATE TABLE metadata(name TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("format", "pbf"),
                ("version", "health-v2"),
                ("tile_schema_revision", "2"),
                ("source_layer", "collection_streets"),
                ("minzoom", "11"),
                ("maxzoom", "16"),
                ("bounds", "-74.3,40.4,-73.6,40.95"),
            ],
        )
    database_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    tileset_hash = hashlib.sha256(tileset.read_bytes()).hexdigest()
    pointer = data / "data_manifest.json"
    pointer.write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "dataset_version": "health-v2",
                "release_path": "releases/health-v2",
                "processed_at": "2026-08-19T12:00:00+00:00",
                "block_faces": 17,
                "schedule_counts": {"REFUSE": 23},
                "ingestion_audit": {"passed": True, "fatal_count": 0},
                "artifacts": {
                    "database": {"path": "app.sqlite3", "sha256": database_hash},
                    "tileset": {
                        "path": "collection_streets.mbtiles",
                        "sha256": tileset_hash,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))
    monkeypatch.setattr(api, "DATABASE_PATH", str(tmp_path / "wrong.sqlite3"))
    monkeypatch.setattr(api, "TILESET_PATH", str(tmp_path / "wrong.mbtiles"))

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["dataset_version"] == "health-v2"
    assert response.json()["processed_records"] == 17
    assert response.json()["map_available"] is True

    with tileset.open("ab") as handle:
        handle.write(b"corruption")
    corrupted = TestClient(app).get("/api/health")
    assert corrupted.status_code == 503
    assert "checksum" in corrupted.json()["detail"]
    assert TestClient(app).get("/api/map-config").json()["available"] is False
    assert TestClient(app).get("/api/refuse-streets?day=MON").status_code == 503


def test_cold_health_hashing_is_background_and_fail_closed(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    release_dir = data / "releases" / "background-checksum"
    release_dir.mkdir(parents=True)
    database = release_dir / "app.sqlite3"
    initialize(database)
    tileset = release_dir / "collection_streets.mbtiles"
    with sqlite3.connect(tileset) as connection:
        connection.execute("CREATE TABLE metadata(name TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("format", "pbf"),
                ("version", "background-checksum"),
                ("tile_schema_revision", "2"),
                ("source_layer", "collection_streets"),
                ("minzoom", "11"),
                ("maxzoom", "16"),
                ("bounds", "-74.3,40.4,-73.6,40.95"),
            ],
        )
    pointer = data / "data_manifest.json"
    pointer.write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "dataset_version": "background-checksum",
                "release_path": "releases/background-checksum",
                "block_faces": 0,
                "schedule_counts": {},
                "artifacts": {
                    "database": {
                        "path": "app.sqlite3",
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    },
                    "tileset": {
                        "path": "collection_streets.mbtiles",
                        "sha256": hashlib.sha256(tileset.read_bytes()).hexdigest(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))
    monkeypatch.setattr(api, "HEALTH_SYNC_HASH_MAX_BYTES", 0)
    client = TestClient(app)

    started = time.perf_counter()
    first = client.get("/api/health")
    assert time.perf_counter() - started < 1
    assert first.status_code == 503
    assert "verifying" in first.json()["detail"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get("/api/health")
        if response.status_code == 200:
            break
        time.sleep(0.01)
    assert response.status_code == 200
    assert response.json()["artifact_integrity"] == "verified"

