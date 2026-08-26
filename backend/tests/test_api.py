import json

import pytest
from fastapi.testclient import TestClient

from app import api
from app.main import app


def test_runtime_tile_contract_accepts_only_revision_four() -> None:
    assert api._expected_tile_schema_revision(
        {"manifest_version": 4, "artifacts": {"tileset": {"tile_schema_revision": 4}}}
    ) == 4
    for revision in (None, 2, 3, 5):
        assert api._expected_tile_schema_revision(
            {"manifest_version": 4, "artifacts": {"tileset": {"tile_schema_revision": revision}}}
        ) is None


def test_first_start_contract_and_removed_routes(tmp_path, monkeypatch) -> None:
    pointer = tmp_path / "missing-manifest.json"
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))
    client = TestClient(app)

    assert client.get("/api/live").status_code == 200
    assert client.get("/api/live").json() == {"status": "ok"}
    assert client.get("/api/health").status_code == 503
    map_config = client.get("/api/map-config")
    assert map_config.status_code == 200
    assert map_config.json()["available"] is False
    assert "known_source_layer" not in map_config.json()
    assert client.get("/api/tiles/not-published/11/0/0.pbf").status_code == 404
    assert client.get("/api/refuse-streets?day=MON").status_code == 404
    assert client.get("/api/app-config").status_code == 404
    assert not pointer.parent.joinpath("app.sqlite3").exists()


@pytest.mark.parametrize("manifest_version", [1, 2, 3, 5])
def test_old_and_future_manifests_fail_closed(tmp_path, monkeypatch, manifest_version) -> None:
    pointer = tmp_path / "data_manifest.json"
    pointer.write_text(json.dumps({"manifest_version": manifest_version}), encoding="utf-8")
    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))

    client = TestClient(app)
    assert client.get("/api/health").status_code == 503
    assert client.get("/api/map-config").json()["available"] is False
    assert client.get("/api/tiles/rejected/11/0/0.pbf").status_code == 503
