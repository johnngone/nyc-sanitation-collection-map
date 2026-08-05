from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_routes_are_before_frontend_catch_all() -> None:
    routes = [getattr(route, "path", None) for route in app.routes]
    if "/" in routes:
        assert routes.index("/api/health") < routes.index("/")


def test_refuse_streets_reaches_api_router() -> None:
    response = TestClient(app).get("/api/refuse-streets?day=MON")
    assert response.status_code == 200
    assert response.json()["type"] == "FeatureCollection"

