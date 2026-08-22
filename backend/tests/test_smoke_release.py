import gzip

import mapbox_vector_tile
import pytest
from fastapi.testclient import TestClient

from app import api
from app.main import app
from app.releases import read_current_release, verify_artifact_checksum
from app.tiles import read_tile
from scripts.build_smoke_release import build_smoke_release


def test_smoke_release_exercises_runtime_database_and_real_vector_tile(tmp_path, monkeypatch) -> None:
    fixture = build_smoke_release(tmp_path)
    pointer = tmp_path / "data_manifest.json"
    release = read_current_release(pointer)

    assert release is not None
    assert release.dataset_version == fixture["version"]
    assert verify_artifact_checksum(release.database_path, release.database_sha256)
    assert verify_artifact_checksum(release.tileset_path, release.tileset_sha256)

    _, _, version, z, x, filename = str(fixture["tile_url"]).strip("/").split("/")
    y = filename.removesuffix(".pbf")
    archived = read_tile(release.tileset_path, int(z), int(x), int(y))
    assert archived is not None and archived.startswith(b"\x1f\x8b")
    decoded = mapbox_vector_tile.decode(gzip.decompress(archived))
    assert decoded["collection_streets"]["features"]

    monkeypatch.setattr(api, "DATA_MANIFEST_PATH", str(pointer))
    client = TestClient(app)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["map_available"] is True
    assert health.json()["artifact_integrity"] == "verified"
    assert health.json()["dataset_version"] == version

    config = client.get("/api/map-config")
    assert config.status_code == 200
    assert config.json()["available"] is True
    assert config.json()["tile_schema_revision"] == 3
    assert config.json()["unknown_source_layer"] == "collection_unknowns"
    assert config.json()["bounds"] == fixture["bounds"]

    tile = client.get(str(fixture["tile_url"]))
    assert tile.status_code == 200
    assert tile.headers["content-encoding"] == "gzip"
    assert tile.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert mapbox_vector_tile.decode(tile.content)["collection_streets"]["features"]

    cached = client.get(
        str(fixture["tile_url"]),
        headers={"If-None-Match": tile.headers["etag"]},
    )
    assert cached.status_code == 304


def test_smoke_release_refuses_to_replace_a_committed_fixture(tmp_path) -> None:
    build_smoke_release(tmp_path)

    with pytest.raises(FileExistsError, match="existing manifest"):
        build_smoke_release(tmp_path)
