import json
from urllib.parse import parse_qs

import httpx
import pytest

from scripts.run_refresh import DSNY_LAYER, download_dsny


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
