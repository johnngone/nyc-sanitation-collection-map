# NYC Sanitation Collection Map

Map NYC sanitation collection schedules by street. Pick a day and filter Refuse Recycling Organics or Bulk trash. The map also supports live browser location tracking. Run the whole app as one Docker container.

## Quick start

Pull the published GHCR image and mount a persistent data directory. The container listens on port `8000`. Set `HOST_PORT` to the port people will use in their browser.

```bash
HOST_PORT=8080
DATA_DIR=/opt/nyc-sanitation-map/data
IMAGE=ghcr.io/johnngone/nyc-sanitation-collection-map:latest

mkdir -p "$DATA_DIR"
docker pull "$IMAGE"
docker run -d \
  --name nyc-sanitation-map \
  --restart unless-stopped \
  -p "${HOST_PORT}:8000" \
  -v "${DATA_DIR}:/app/data" \
  -e DATA_REFRESH_ON_STARTUP=true \
  "$IMAGE"
```

Open `http://SERVER-IP:8080`. Replace `8080` with your `HOST_PORT` value.

The first refresh downloads the source data and builds the map release. The basemap loads before that work is done. Street schedules appear after the release passes validation.

Use a commit SHA tag instead of `latest` when you want a pinned deployment. Published images use the form `ghcr.io/johnngone/nyc-sanitation-collection-map:<commit-sha>`.

## HTTPS and location tracking

Live location tracking requires HTTPS. `http://localhost` also works for local use. Put a public or LAN deployment behind an HTTPS reverse proxy and allow the browser location permission.

The browser keeps location coordinates in memory. The app does not send them to its API or store them in the data volume.

## Deployment settings

The Docker image refreshes data on startup and every 14 days. Set these environment variables on `docker run` or in Docker Compose when you need different behavior.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATA_REFRESH_ON_STARTUP` | `true` | Start a refresh when the container starts |
| `DATA_REFRESH_ENABLED` | `true` | Run the background refresh scheduler |
| `DATA_REFRESH_INTERVAL_DAYS` | `14` | Days between scheduled refreshes |
| `DATA_RELEASE_RETENTION` | `2` | Validated releases kept on the data volume |
| `HEALTH_SYNC_HASH_MAX_BYTES` | `16777216` | Artifact bytes checked during a health request before validation continues in the background |
| `VITE_BASEMAP_TILEJSON_URL` | OpenFreeMap | Basemap TileJSON URL used while building the image |

`VITE_BASEMAP_TILEJSON_URL` is a build setting. Rebuild the image after changing it. The remaining settings apply when the container starts.

For a source build with Docker Compose copy `.env.example` to `.env`. Set `APP_HOST_PORT` in `.env` then run:

```bash
docker compose up -d --build
docker compose logs -f app
```

Compose mounts `./data` at `/app/data`.

## Documentation

- [Deployment and release operations](docs/operations.md) covers refreshes release checks rollback and tile limits.
- [Data sources and completeness](docs/data-sources.md) covers DSNY frequencies DCP LION validation and limits on map interpretation.
- [Build process and release checks](docs/operations.md#refresh-gates) lists the gates that run before a release becomes live.
- [Map architecture](docs/operations.md#runtime-data-path) shows the path from source downloads to vector tiles in the browser.
- [Backend architecture](docs/operations.md#runtime-data-path) covers the FastAPI service and its release data path.
- [Frontend map design](docs/operations.md#vector-tile-contract) covers MapLibre viewport requests and browser caching.
- [DSNY address lookup investigation](docs/dsny-lookup.md) documents the separate research utility. The production refresh does not call the address lookup page.

## API

| Endpoint | Use |
| --- | --- |
| `GET /api/health` | Release status record counts and artifact checks |
| `GET /api/map-config` | Current vector tile URL and map availability |
| `GET /api/tiles/{version}/{z}/{x}/{y}.pbf` | Gzip vector tiles used by the map |
| `GET /api/refuse-streets?day=MON&types=REFUSE` | Legacy GeoJSON query limited to 20000 features |

The frontend uses `/api/map-config` and vector tiles. Keep `/api/refuse-streets` for compatibility or diagnostics.

## Troubleshooting

| Problem | Check |
| --- | --- |
| Basemap loads but street schedules do not | Run `curl http://127.0.0.1:8080/api/health` and inspect `docker logs nyc-sanitation-map` |
| Health returns `503` while checksums are verifying | Keep polling. The app checks each committed database and tileset before serving it |
| Location button is unavailable | Use HTTPS or `localhost` and grant browser location permission |
| Docker reports that the port is allocated | Change `HOST_PORT` such as `9090` |
| The first refresh cannot publish data | Confirm the mounted data directory is writable and inspect the container log |

## Local development

Install the backend dependencies and start FastAPI from the repository root.

```bash
python -m pip install -e ".[refresh]"
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Start the frontend in another terminal.

```bash
cd frontend
npm ci
npm run dev
```

Use `python scripts/run_refresh.py --allow-large-run` to build a local dataset from the official sources. Run `python scripts/run_refresh.py --status` to inspect the current release.

## License

The project code is licensed under the [MIT License](LICENSE). Third-party data and assets retain their own terms.

This map derives street schedules from NYC Department of Sanitation frequency data and NYC Department of City Planning LION street centerlines. Use the official [DSNY collection schedule lookup](https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup) for an address-specific answer.

Basemap data comes from [OpenMapTiles](https://openmaptiles.org/) and [OpenStreetMap](https://www.openstreetmap.org/copyright). See [third-party notices](THIRD_PARTY_NOTICES.md) for licenses.
