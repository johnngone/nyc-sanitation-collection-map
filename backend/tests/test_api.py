import sqlite3

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
            (block_face_id, segment_id, borough, street_name, side, geometry_wkt,
             min_x, min_y, max_x, max_y)
            VALUES ('bf-1', 'seg-1', 'BROOKLYN', 'EXAMPLE STREET', 'LEFT',
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

