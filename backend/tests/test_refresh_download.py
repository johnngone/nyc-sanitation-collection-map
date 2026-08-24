import json
import copy
from urllib.parse import parse_qs

import httpx
import pytest

from scripts.release_validation import RELEASE_FILENAMES, atomic_json, file_sha256
from scripts.run_refresh import (
    DSNY_LAYER,
    download_dsny,
    processing_fingerprint,
    refresh_fingerprint,
    source_fingerprint,
    unchanged_release,
)


def _params(request: httpx.Request) -> dict[str, str]:
    if request.method == "POST":
        return {
            key: values[0]
            for key, values in parse_qs(request.content.decode("utf-8")).items()
        }
    return dict(request.url.params.multi_items())


def _feature(object_id: int) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": []},
        "properties": {"OBJECTID": object_id, "FREQ_REFUSE": "Mon, Thu"},
    }


def test_download_dsny_proves_every_advertised_id_was_downloaded(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = _params(request)
        if str(request.url).startswith(DSNY_LAYER) and request.url.path.endswith("/0"):
            return httpx.Response(
                200,
                json={
                    "objectIdField": "OBJECTID",
                    "maxRecordCount": 2,
                    "currentVersion": 12.0,
                    "editingInfo": {"lastEditDate": 1234},
                },
            )
        if params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 3})
        if params.get("returnIdsOnly") == "true":
            return httpx.Response(200, json={"objectIds": [30, 10, 20]})
        requested = [int(value) for value in params["objectIds"].split(",")]
        return httpx.Response(200, json={"features": [_feature(value) for value in reversed(requested)]})

    output = tmp_path / "dsny.geojson"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source_audit = download_dsny(output, client)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [item["properties"]["OBJECTID"] for item in payload["features"]] == [10, 20, 30]
    assert source_audit == {
        "record_count": 3,
        "object_id_field": "OBJECTID",
        "max_record_count": 2,
        "service_last_edit_ms": 1234,
        "service_version": 12.0,
    }


def test_download_dsny_fails_when_source_omits_a_requested_id(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = _params(request)
        if request.url.path.endswith("/0"):
            return httpx.Response(200, json={"objectIdField": "OBJECTID", "maxRecordCount": 10})
        if params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 2})
        if params.get("returnIdsOnly") == "true":
            return httpx.Response(200, json={"objectIds": [10, 20]})
        return httpx.Response(200, json={"features": [_feature(10)]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="missing=.*20"):
            download_dsny(tmp_path / "dsny.geojson", client)


def test_download_dsny_fails_on_count_id_disagreement(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = _params(request)
        if request.url.path.endswith("/0"):
            return httpx.Response(200, json={"objectIdField": "OBJECTID", "maxRecordCount": 10})
        if params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 3})
        return httpx.Response(200, json={"objectIds": [10, 20]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="count/ID mismatch"):
            download_dsny(tmp_path / "dsny.geojson", client)


def test_download_dsny_rejects_a_source_revision_change_mid_snapshot(tmp_path) -> None:
    metadata_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_reads
        params = _params(request)
        if request.url.path.endswith("/0"):
            metadata_reads += 1
            return httpx.Response(
                200,
                json={
                    "objectIdField": "OBJECTID",
                    "maxRecordCount": 10,
                    "editingInfo": {"lastEditDate": metadata_reads},
                },
            )
        if params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 1})
        if params.get("returnIdsOnly") == "true":
            return httpx.Response(200, json={"objectIds": [10]})
        return httpx.Response(200, json={"features": [_feature(10)]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="changed during snapshot"):
            download_dsny(tmp_path / "dsny.geojson", client)


def test_download_dsny_requires_a_snapshot_edit_timestamp(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = _params(request)
        if request.url.path.endswith("/0"):
            return httpx.Response(
                200,
                json={"objectIdField": "OBJECTID", "maxRecordCount": 10},
            )
        if params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 1})
        if params.get("returnIdsOnly") == "true":
            return httpx.Response(200, json={"objectIds": [10]})
        return httpx.Response(200, json={"features": [_feature(10)]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="changed during snapshot"):
            download_dsny(tmp_path / "dsny.geojson", client)


def _source_identity(
    *,
    cscl_revision: int = 1,
    input_sha256: dict[str, str] | None = None,
) -> dict[str, object]:
    return source_fingerprint(
        input_sha256=input_sha256
        or {"dsny": "d" * 64, "lion": "1" * 64, "pad": "a" * 64},
        dsny_source_audit={
            "record_count": 600,
            "object_id_field": "OBJECTID",
            "max_record_count": 2_000,
            "service_last_edit_ms": 1234,
            "service_version": 12.0,
        },
        lion_source_audit={"bytes": 10, "etag": "lion-etag", "last_modified": "today"},
        pad_source_audit={"bytes": 11, "etag": "pad-etag", "last_modified": "today"},
        lion_release="26B",
        pad_release="26B",
        lion_catalog_release="26B",
        pad_catalog_release="26B",
        lion_metadata={"rowsUpdatedAt": 10},
        pad_metadata={"rowsUpdatedAt": 11},
        addresspoint_metadata_before={"rowsUpdatedAt": 12},
        addresspoint_metadata_after={"rowsUpdatedAt": 12},
        cscl_metadata_before={"lastEditDate": cscl_revision},
        cscl_metadata_after={"lastEditDate": cscl_revision},
    )


def _committed_refresh(tmp_path, processing: dict[str, object]):
    pointer = tmp_path / "data" / "data_manifest.json"
    version = "release-fast-path"
    release_dir = pointer.parent / "releases" / version
    release_dir.mkdir(parents=True)
    artifacts = {}
    for name, filename in RELEASE_FILENAMES.items():
        artifact = release_dir / filename
        artifact.write_bytes(f"fixture:{name}".encode())
        artifacts[name] = {"path": filename, "sha256": file_sha256(artifact)}
    input_sha256 = {
        "dsny": artifacts["source_dsny"]["sha256"],
        "lion": artifacts["source_lion"]["sha256"],
        "pad": artifacts["source_pad"]["sha256"],
    }
    fingerprint = refresh_fingerprint(
        processing,
        _source_identity(input_sha256=input_sha256),
    )
    manifest = {
        "manifest_version": 3,
        "dataset_version": version,
        "release_path": f"releases/{version}",
        "refresh_fingerprint": fingerprint,
        "input_sha256": input_sha256,
        "artifacts": artifacts,
        "counts": {
            "raw_lion_rows": 200_000,
            "dsny_frequency_rows": 600,
            "eligible_lion_rows": 180_000,
            "matched_sides": 170_000,
            "used_frequency_rows": 590,
            "output_features": 150_000,
            "schedule_rows_by_type": {
                "REFUSE": 1,
                "RECYCLING": 1,
                "ORGANICS": 1,
                "BULK": 1,
            },
        },
    }
    atomic_json(release_dir / "release_manifest.json", manifest)
    pointer_manifest = {**manifest, "previous_releases": []}
    atomic_json(pointer, pointer_manifest)
    return pointer, pointer_manifest, fingerprint


def test_processing_fingerprint_changes_with_generation_configuration() -> None:
    zoom_16 = processing_fingerprint(
        tile_minzoom=12,
        tile_maxzoom=16,
        side_offset_feet=25.0,
    )
    zoom_17 = processing_fingerprint(
        tile_minzoom=12,
        tile_maxzoom=17,
        side_offset_feet=25.0,
    )

    assert zoom_16["sha256"] != zoom_17["sha256"]
    assert zoom_16["inputs"]["configuration"]["tile_maxzoom"] == 16


def test_source_fingerprint_changes_with_a_shadow_source_revision() -> None:
    assert _source_identity(cscl_revision=1)["sha256"] != _source_identity(
        cscl_revision=2
    )["sha256"]


def test_unchanged_release_requires_exact_fingerprint_and_complete_artifacts(tmp_path) -> None:
    pointer, manifest, candidate = _committed_refresh(
        tmp_path,
        {"fingerprint_version": 1, "sha256": "b" * 64, "inputs": {}},
    )
    gate = {
        "min_lion_rows": 200_000,
        "min_dsny_rows": 500,
        "min_output_features": 100_000,
        "max_drop_fraction": 0.10,
    }

    assert unchanged_release(pointer, candidate, regression_gate=gate) == manifest

    changed = copy.deepcopy(candidate)
    changed["processing"]["sha256"] = "c" * 64
    assert unchanged_release(pointer, changed, regression_gate=gate) is None

    (pointer.parent / manifest["release_path"] / "tile_build_report.json").unlink()
    assert unchanged_release(pointer, candidate, regression_gate=gate) is None


@pytest.mark.parametrize("corrupt_name", ["database", "tileset", "source_lion"])
def test_unchanged_release_rebuilds_when_an_installed_artifact_is_corrupt(
    tmp_path,
    corrupt_name,
) -> None:
    pointer, manifest, candidate = _committed_refresh(
        tmp_path,
        {"fingerprint_version": 1, "sha256": "b" * 64, "inputs": {}},
    )
    descriptor = manifest["artifacts"][corrupt_name]
    artifact = pointer.parent / manifest["release_path"] / descriptor["path"]
    artifact.write_bytes(b"corrupt")

    assert unchanged_release(
        pointer,
        candidate,
        regression_gate={
            "min_lion_rows": 200_000,
            "min_dsny_rows": 500,
            "min_output_features": 100_000,
            "max_drop_fraction": 0.10,
        },
    ) is None


def test_unchanged_release_rebuilds_when_installed_manifest_diverges(tmp_path) -> None:
    pointer, manifest, candidate = _committed_refresh(
        tmp_path,
        {"fingerprint_version": 1, "sha256": "b" * 64, "inputs": {}},
    )
    installed_manifest = (
        pointer.parent / manifest["release_path"] / "release_manifest.json"
    )
    installed = json.loads(installed_manifest.read_text(encoding="utf-8"))
    installed["processed_at"] = "tampered"
    atomic_json(installed_manifest, installed)

    assert unchanged_release(
        pointer,
        candidate,
        regression_gate={
            "min_lion_rows": 200_000,
            "min_dsny_rows": 500,
            "min_output_features": 100_000,
            "max_drop_fraction": 0.10,
        },
    ) is None


def test_unchanged_release_still_enforces_current_policy_floors(tmp_path) -> None:
    pointer, _, candidate = _committed_refresh(
        tmp_path,
        {"fingerprint_version": 1, "sha256": "b" * 64, "inputs": {}},
    )

    with pytest.raises(RuntimeError, match="raw_lion_rows=.*floor"):
        unchanged_release(
            pointer,
            candidate,
            regression_gate={
                "min_lion_rows": 200_001,
                "min_dsny_rows": 500,
                "min_output_features": 100_000,
                "max_drop_fraction": 0.10,
            },
        )
