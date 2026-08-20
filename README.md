# NYC Sanitation Collection Map

An interactive map of NYC refuse, recycling, organics, and bulk collection schedules. The browser now loads small vector tiles for the visible area instead of downloading a citywide GeoJSON response.

The production image serves the compiled React/MapLibre frontend and FastAPI API on container port `8000`. A separate refresh worker downloads and audits the official sources, builds SQLite plus MBTiles, and publishes them through the same persistent data volume.

## Why the map loads quickly

- The MBTiles archive contains one vector feature per stored block face. Its four schedule fields are comma-separated weekday codes, so geometry is not repeated for every day or collection type.
- Geometry is clipped and simplified by zoom (`12` through `17` by default), and each PBF tile is gzip-compressed.
- MapLibre requests only the tiles needed for the current view and filters day/type in the browser.
- Tile URLs include the immutable dataset version. Successful responses use a one-year immutable cache policy and an `ETag`.

See [architecture and operations](docs/operations.md) for the tile schema, release layout, validation gates, and rollback procedure. See [data sources and completeness](docs/data-sources.md) for the source-to-map audit contract.

## Deploy with Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f refresh
```

Open `http://SERVER-IP:8080`. `APP_HOST_PORT` changes the browser-facing port only; the container remains on port `8000`.

Compose starts both services by default:

- `app` serves the site and API.
- `refresh` performs a full refresh on first startup, then repeats every 14 days by default.

Both mount `./data:/app/data`. The app can start before the first dataset is ready: the basemap appears, the collection layer reports that tiles are not available, and the browser retries `/api/health` and `/api/map-config` with a 0.5-second exponential backoff capped at 15 seconds. After a production-sized release is first discovered, checksum verification runs once in the background; `/api/health` may briefly return HTTP `503` with `verifying` and map config remains unavailable until it completes. No app restart or page reload is required. A citywide refresh is CPU-, memory-, disk-, and network-intensive, so follow the refresh log until it reports a promoted dataset version.

Useful commands:

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f refresh
curl http://127.0.0.1:8080/api/health
docker compose down
```

Set `DATA_REFRESH_ON_STARTUP=false` after the first run if recreating the refresh container should wait until the next interval. Set `DATA_REFRESH_ENABLED=false` to stop scheduled refreshes entirely.

## Deploy the published GHCR images

The web image does not contain a citywide dataset. Choose one published commit SHA and run that exact app/worker pair with the same host directory:

```bash
RELEASE_SHA=<published-commit-sha>
mkdir -p /opt/nyc-sanitation-map/data
docker pull ghcr.io/johnngone/nyc-sanitation-collection-map:${RELEASE_SHA}
docker pull ghcr.io/johnngone/nyc-sanitation-collection-map:refresh-${RELEASE_SHA}

docker run -d --name nyc-sanitation-map --restart unless-stopped \
  -p 8080:8000 \
  -v /opt/nyc-sanitation-map/data:/app/data \
  ghcr.io/johnngone/nyc-sanitation-collection-map:${RELEASE_SHA}

docker run -d --name nyc-sanitation-refresh --restart unless-stopped \
  -v /opt/nyc-sanitation-map/data:/app/data \
  -e DATA_REFRESH_ON_STARTUP=true \
  -e DATA_REFRESH_INTERVAL_DAYS=14 \
  ghcr.io/johnngone/nyc-sanitation-collection-map:refresh-${RELEASE_SHA}
```

The publish workflow builds and smoke-tests both containers before pushing either image. It publishes the paired immutable tags `<sha>` and `refresh-<same-sha>` first, then updates the convenience tags `latest` and `refresh` in a serialized job. Prefer the SHA pair for deployments because two registry tags cannot be advanced atomically.

For unRAID, create one web container from `:<sha>` with container port `8000`, plus one non-web worker from `:refresh-<same-sha>`. Give both the same host path at `/app/data`; publish no worker port. The moving `:latest` and `:refresh` tags are available when commit pinning is impractical, but must still be deployed together.

## Migrate an existing DB-only volume

Back up the existing data directory, leave its `app.sqlite3` in place, and deploy the new app and refresh worker against that same directory. The app can still inspect the legacy database while the worker builds the first complete release. When validation succeeds, the worker adds `releases/<dataset-version>/` and atomically writes `data_manifest.json`; subsequent requests use the paired database and tileset from that release. The old root-level database is not silently converted or overwritten and can remain until the new release has been verified.

Allow room for the temporary build and at least two retained releases. Confirm migration with:

```bash
curl http://SERVER-IP:8080/api/health
docker logs nyc-sanitation-refresh
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
docker build -f Dockerfile.refresh -t nyc-sanitation-map-refresh:test .
```

## API and troubleshooting

- `/api/health` reports the committed version, record counts, quality summary, artifact-integrity state, and whether the map archive exists.
- `/api/map-config` reports the current versioned vector-tile URL. An unavailable response during first initialization is expected.
- `/api/tiles/{version}/{z}/{x}/{y}.pbf` serves cached gzip PBF tiles.
- `/api/refuse-streets` is retained for compatibility, but responses are capped at 20,000 features. A broad request returns HTTP `413`; use a smaller bounding box or vector tiles.

If the basemap loads but collection lines do not, inspect `/api/health` and the refresh log. A `503` whose detail says checksums are `verifying` is transient; keep polling. Any `invalid` result is a fail-closed integrity error. `HEALTH_SYNC_HASH_MAX_BYTES` controls which small releases are hashed inline (16 MiB by default); it never disables verification. A failed refresh leaves the current committed release live. Volume permission errors prevent the worker from creating its temporary build or atomic manifest. Restore the volume backup or activate a retained release as described in [operations](docs/operations.md) if a manifest or committed artifact is invalid. For `port already allocated`, change only the host side, for example `9090:8000`.

Official source attribution: NYC Department of Sanitation and NYC Department of City Planning. This visualization is not a substitute for the official [DSNY collection schedule lookup](https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup).
