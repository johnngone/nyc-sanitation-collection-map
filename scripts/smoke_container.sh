#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: smoke_container.sh IMAGE CONTAINER_NAME}"
container="${2:?usage: smoke_container.sh IMAGE CONTAINER_NAME}"
smoke_data="$(mktemp -d)"
fixture_json="${RUNNER_TEMP:-${smoke_data}}/smoke-fixture.json"
health_json="${RUNNER_TEMP:-${smoke_data}}/smoke-health.json"
map_json="${RUNNER_TEMP:-${smoke_data}}/smoke-map-config.json"
tile_headers="${smoke_data}/tile.headers"
tile_body="${smoke_data}/tile.pbf"

cleanup() {
  docker logs "${container}" 2>&1 || true
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_app() {
  docker run -d --name "${container}" \
    -e APP_ENV=production \
    -e DATA_REFRESH_ENABLED=false \
    -p 18080:8000 \
    -v "${smoke_data}:/app/data" \
    "${image}"
}

start_app
ready=0
for attempt in {1..30}; do
  if curl --fail --silent http://127.0.0.1:18080/api/live >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
test "${ready}" -eq 1
test "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18080/)" = "200"
test "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18080/api/live)" = "200"
test "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18080/api/health)" = "503"
test "$(curl --silent http://127.0.0.1:18080/api/map-config | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["available"]).lower())')" = "false"
for path in /docs /redoc /openapi.json /api/tiles/unpublished/11/0/0.pbf; do
  test "$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:18080${path}")" = "404"
done
test -z "$(find "${smoke_data}" -mindepth 1 -print -quit)"
docker rm -f "${container}" >/dev/null

docker run --rm --entrypoint python \
  -v "${smoke_data}:/smoke-data" \
  "${image}" \
  scripts/build_smoke_release.py --data-dir /smoke-data >"${fixture_json}"
docker run --rm --entrypoint python -v "${smoke_data}:/smoke-data:ro" "${image}" -c \
  'import json,sqlite3,pathlib; f=json.load(open("/smoke-data/data_manifest.json")); d=pathlib.Path("/smoke-data")/f["release_path"]/("app.sqlite3"); c=sqlite3.connect(d); cols={r[1] for r in c.execute("pragma table_info(block_faces)")}; assert cols=={"block_face_id","origin_block_face_id","segment_id","borough","street_name","side","geometry_wkt"}; assert dict(c.execute("select key,value from dataset_metadata"))["database_schema_revision"]=="1"'

start_app
ready=0
for attempt in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:18080/ >"${RUNNER_TEMP:-${smoke_data}}/frontend.html" \
    && curl --fail --silent --show-error http://127.0.0.1:18080/api/health >"${health_json}"; then
    ready=1
    break
  fi
  sleep 2
done
test "${ready}" -eq 1

curl --fail --silent --show-error http://127.0.0.1:18080/api/map-config >"${map_json}"
python3 - "${health_json}" "${map_json}" "${fixture_json}" <<'PY'
import json
import math
import sys

health = json.load(open(sys.argv[1], encoding="utf-8"))
config = json.load(open(sys.argv[2], encoding="utf-8"))
fixture = json.load(open(sys.argv[3], encoding="utf-8"))
assert health["status"] == "ok"
assert health["environment"] == "production"
assert health["data_manifest"] == 4
assert health["map_available"] is True
assert health["artifact_integrity"] == "verified"
assert health["dataset_version"] == fixture["version"]
assert health["verified_at"] == fixture["verified_at"]
assert config["available"] is True
assert config["version"] == fixture["version"]
assert config["verified_at"] == fixture["verified_at"]
assert config["tile_schema_revision"] == 4
assert config["source_layer"] == "collection_streets"
assert config["unknown_source_layer"] == "collection_unknowns"
bounds = config["bounds"]
assert isinstance(bounds, list) and len(bounds) == 4
assert all(isinstance(value, (int, float)) and math.isfinite(value) for value in bounds)
assert bounds[0] < bounds[2] and bounds[1] < bounds[3]
assert bounds == fixture["bounds"]
PY

tile_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tile_url"])' "${fixture_json}")"
curl --fail --silent --show-error \
  --dump-header "${tile_headers}" \
  --output "${tile_body}" \
  "http://127.0.0.1:18080${tile_url}"
etag="$(python3 - "${tile_headers}" "${tile_body}" <<'PY'
import pathlib
import sys

headers = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="iso-8859-1").splitlines()[1:]:
    if ":" in line:
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
body = pathlib.Path(sys.argv[2]).read_bytes()
assert body.startswith(b"\x1f\x8b")
assert headers["content-encoding"].lower() == "gzip"
assert headers["content-type"].startswith("application/vnd.mapbox-vector-tile")
assert headers["cache-control"] == "public, max-age=31536000, immutable"
assert headers["etag"]
print(headers["etag"])
PY
)"
docker run --rm --entrypoint python \
  -v "${smoke_data}:/smoke-data:ro" \
  "${image}" \
  -c 'import gzip, pathlib, mapbox_vector_tile as m; payload=gzip.decompress(pathlib.Path("/smoke-data/tile.pbf").read_bytes()); decoded=m.decode(payload); assert decoded["collection_streets"]["features"]'
cache_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -H "If-None-Match: ${etag}" "http://127.0.0.1:18080${tile_url}")"
test "${cache_status}" = "304"
