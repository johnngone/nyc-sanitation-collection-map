# NYC Sanitation Collection Map

An interactive map of NYC refuse, recycling, organics, and bulk collection schedules. The browser now loads small vector tiles for the visible area instead of downloading a citywide GeoJSON response.

The production image is a standalone container: it serves the compiled React/MapLibre frontend and FastAPI API on port `8000`, while a background process in that same container downloads, audits, and publishes the data release to its persistent volume.

## Why the map loads quickly

- The MBTiles archive contains one vector feature per stored block face. Its four schedule fields are comma-separated weekday codes, so geometry is not repeated for every day or collection type.
- Geometry is clipped and simplified by zoom (`12` through `16` by default), and each PBF tile is gzip-compressed.
- MapLibre requests only the tiles needed for the current view and filters day/type in the browser.
- Tile URLs include the immutable dataset version. Successful responses use a one-year immutable cache policy and an `ETag`.

See [architecture and operations](docs/operations.md) for the tile schema, release layout, validation gates, and rollback procedure. See [data sources and completeness](docs/data-sources.md) for the source-to-map audit contract.

## Deploy with Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f app
```

Open `http://SERVER-IP:8080`. `APP_HOST_PORT` changes the browser-facing port only; the container remains on port `8000`.

Compose starts one `app` service, mounting `./data:/app/data`. It serves the site and API and performs a full refresh on first startup, then repeats every 14 days by default. The basemap appears before the first dataset is ready; the collection layer becomes available automatically once the background refresh has promoted its validated release. A citywide refresh is CPU-, memory-, disk-, and network-intensive, so follow the app log until it reports a promoted dataset version.

The bundled basemap style uses OpenMapTiles-compatible vector tiles from OpenFreeMap. `VITE_BASEMAP_TILEJSON_URL` can select another compatible TileJSON endpoint when building the frontend or container. This is a build-time setting, so changing it requires rebuilding the image. OpenFreeMap's public service is provided without an SLA; a basemap request failure does not prevent the locally served sanitation overlay from initializing.

Useful commands:

```bash
docker compose ps
docker compose logs -f app
curl http://127.0.0.1:8080/api/health
docker compose down
```

Set `DATA_REFRESH_ON_STARTUP=false` after the first run if recreating the container should wait until the next interval. Set `DATA_REFRESH_ENABLED=false` to stop scheduled refreshes entirely. Failed refreshes retain the current release and retry after `DATA_REFRESH_FAILURE_RETRY_MINUTES` (30 minutes by default), rather than waiting for the normal 14-day interval.

## Deploy the published GHCR images

The image does not contain a citywide dataset. Choose one published commit SHA and run one standalone container:

```bash
RELEASE_SHA=<published-commit-sha>
mkdir -p /opt/nyc-sanitation-map/data
docker pull ghcr.io/johnngone/nyc-sanitation-collection-map:${RELEASE_SHA}

docker run -d --name nyc-sanitation-map --restart unless-stopped \
  -p 8080:8000 \
  -v /opt/nyc-sanitation-map/data:/app/data \
  -e DATA_REFRESH_ON_STARTUP=true \
  -e DATA_REFRESH_INTERVAL_DAYS=14 \
  ghcr.io/johnngone/nyc-sanitation-collection-map:${RELEASE_SHA}
```

The publish workflow builds and smoke-tests the image before publishing its immutable `<sha>` tag, then advances the convenience `latest` tag in a serialized job. Prefer a commit SHA for deployments.

For unRAID, create one web container from `:<sha>` with container port `8000` and map a persistent host folder to `/app/data`. The container runs the web service and refresh process together. The moving `:latest` tag is available when commit pinning is impractical.

## Migrate an existing DB-only volume

Back up the existing data directory, leave its `app.sqlite3` in place, and deploy the new standalone container against that same directory. It can still inspect the legacy database while its background refresh builds the first complete release. When validation succeeds, it adds `releases/<dataset-version>/` and atomically writes `data_manifest.json`; subsequent requests use the paired database and tileset from that release. The old root-level database is not silently converted or overwritten and can remain until the new release has been verified.

Allow room for the temporary build and at least two retained releases. Confirm migration with:

```bash
curl http://SERVER-IP:8080/api/health
docker logs nyc-sanitation-map
```

`dataset_version` should be non-null and `map_available` should be `true`.

## Local development and refresh

From the repository root:

```bash
python -m pip install -e ".[refresh,test]"
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Local Vite commands read `VITE_BASEMAP_TILEJSON_URL` from the repository-root `.env` file when present. The default OpenFreeMap endpoint is used when it is omitted.

Build and publish a complete local dataset from the official sources:

```bash
python scripts/run_refresh.py --allow-large-run
python scripts/run_refresh.py --status
```

The guard flag makes a costly citywide run explicit. Nothing is promoted unless source, ingestion, database, tile, checksum, count-regression, and bundle checks all pass.

## Tests

```bash
python -m pytest
cd frontend && npm ci && npm run build
docker build -f Dockerfile -t nyc-sanitation-map:test .
```

## API and troubleshooting

- `/api/health` reports the committed version, record counts, quality summary, artifact-integrity state, and whether the map archive exists.
- `/api/map-config` reports the current versioned vector-tile URL. An unavailable response during first initialization is expected.
- `/api/tiles/{version}/{z}/{x}/{y}.pbf` serves cached gzip PBF tiles.
- `/api/refuse-streets` is retained for compatibility, but responses are capped at 20,000 features. A broad request returns HTTP `413`; use a smaller bounding box or vector tiles.

If the basemap loads but collection lines do not, inspect `/api/health` and the container log. A `503` whose detail says checksums are `verifying` is transient; keep polling. Any `invalid` result is a fail-closed integrity error. `HEALTH_SYNC_HASH_MAX_BYTES` controls which small releases are hashed inline (16 MiB by default); it never disables verification. A failed refresh leaves the current committed release live. Volume permission errors prevent the container from creating its temporary build or atomic manifest. Restore the volume backup or activate a retained release as described in [operations](docs/operations.md) if a manifest or committed artifact is invalid. For `port already allocated`, change only the host side, for example `9090:8000`.

Official source attribution: NYC Department of Sanitation and NYC Department of City Planning. This visualization is not a substitute for the official [DSNY collection schedule lookup](https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup).

Basemap attribution: [© OpenMapTiles](https://openmaptiles.org/) Data from [OpenStreetMap](https://www.openstreetmap.org/copyright). See [third-party notices](THIRD_PARTY_NOTICES.md) for the style, tile, data, and font licenses.
