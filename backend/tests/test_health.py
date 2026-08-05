from fastapi.testclient import TestClient

from app import api
from app.database import initialize
from app.main import app


def test_health(tmp_path, monkeypatch) -> None:
    database = tmp_path / "app.sqlite3"
    initialize(database)
    monkeypatch.setattr(api, "DATABASE_PATH", str(database))
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_routes_are_before_frontend_catch_all() -> None:
    routes = [getattr(route, "path", None) for route in app.routes]
    if "/" in routes:
        assert routes.index("/api/health") < routes.index("/")


def test_refuse_streets_reaches_api_router(tmp_path, monkeypatch) -> None:
    database = tmp_path / "app.sqlite3"
    initialize(database)
    monkeypatch.setattr(api, "DATABASE_PATH", str(database))
    response = TestClient(app).get("/api/refuse-streets?day=MON")
    assert response.status_code == 200
    assert response.json()["type"] == "FeatureCollection"

