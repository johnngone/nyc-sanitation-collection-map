# NYC Sanitation Collection Map

 🌐 [Open the live map: trashmap.nyc](https://trashmap.nyc)

Map NYC sanitation collection schedules by street. Pick a day and filter by refuse, recycling, organics, or bulk trash. The map also supports live browser location tracking. Hostable as a standalone Docker container.

<p align="center"><img width="600" alt="Screenshot of trashmap.nyc" src="https://github.com/user-attachments/assets/90b6505a-f896-452e-86ca-f929e23c0b62" /></p>

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

Version 2 uses a fresh-only persisted-data contract. Start it with an empty data directory; manifests and artifacts created by earlier versions are deliberately rejected. Do not point a v2 container at a v1 data volume.

Release images are published for both `linux/amd64` and `linux/arm64`. Docker automatically pulls the matching image for the host, including an Oracle Cloud Ampere A1 VM. Use a GitHub release tag instead of `latest` when you want a pinned deployment. Published images use the form `ghcr.io/johnngone/nyc-sanitation-collection-map:<release-tag>`.

Set the `APP_*` variables to customize visible branding and the metadata returned in the initial HTML response. Recreate the container after changing them; the image does not need to be rebuilt. Set `APP_PUBLIC_URL` to the deployment's public HTTPS origin without a trailing slash. It supplies the canonical URL, social image URL, sitemap, and structured-data URL.

The initial page response includes the configured title and description, Open Graph and Twitter cards, canonical metadata, and JSON-LD. The server also exposes `/robots.txt` and `/sitemap.xml`.

`APP_ROBOTS_TXT` defaults to a private, crawl-blocking policy. Use literal `\n` sequences between lines when setting a custom value.

Private default (leaving the variable empty produces the same file):

```env
APP_ROBOTS_TXT=User-agent: *\nDisallow: /
```

Public example:

```env
APP_ROBOTS_TXT=User-agent: *\nAllow: /\nSitemap: https://map.example.com/sitemap.xml
```

```bash
docker run -d \
  --name nyc-sanitation-map \
  --restart unless-stopped \
  -p "${HOST_PORT}:8000" \
  -v "${DATA_DIR}:/app/data" \
  -e APP_TITLE="My Collection Map" \
  -e APP_SUBTITLE="Find pickup schedules in your neighborhood." \
  -e APP_BROWSER_TITLE="Collection Schedule Map | Example Organization" \
  -e APP_META_DESCRIPTION="Explore local refuse and recycling schedules by street." \
  -e APP_PUBLIC_URL="https://map.example.com" \
  -e APP_ROBOTS_TXT="User-agent: *\\nAllow: /\\nSitemap: https://map.example.com/sitemap.xml" \
  "$IMAGE"
```

## HTTPS and location tracking

Live location tracking requires HTTPS. `http://localhost` also works for local use. Put a public or LAN deployment behind an HTTPS reverse proxy and allow the browser location permission.

The map starts with a citywide NYC overview centered on U Thant Island. If location access was already granted, or the user previously completed a manual locate in that browser, it automatically starts tracking and moves to the user's neighborhood. Otherwise, it waits for the location button so first-time visitors do not receive an unsolicited permission prompt. Locations outside the collection-data bounds leave the automatic startup view on the NYC overview.

The browser keeps location coordinates in memory and stores only the auto-location preference locally. The app does not send coordinates to its API or store them in the data volume. Firefox may ask again after a temporary location permission expires.

## Deployment settings

The Docker image refreshes data on startup and every 14 days. Set these environment variables on `docker run` or in Docker Compose when you need different behavior.

| Setting | Default | Purpose |
| --- | --- | --- |
| `APP_TITLE` | `NYC Sanitation – Collection Map` | Visible and accessible map heading |
| `APP_SUBTITLE` | `See collection schedules by street and day.` | Text beneath the map title |
| `APP_BROWSER_TITLE` | `NYC Sanitation Collection Map` | Browser-tab, search-result, Open Graph, and Twitter title |
| `APP_SHORT_NAME` | `Trash Map` | Home-screen and installed-app label |
| `APP_META_DESCRIPTION` | `Map NYC sanitation collection schedules by street and day.` | Search-result, Open Graph, and Twitter description |
| `APP_PUBLIC_URL` | empty | Public HTTPS origin used for canonical, Open Graph, JSON-LD, robots, and sitemap URLs |
| `APP_ROBOTS_TXT` | empty (`Disallow: /`) | Complete `/robots.txt` override using literal `\n` line separators; set an explicit allow policy to make the site crawlable |
| `DATA_REFRESH_ON_STARTUP` | `true` | Start a refresh when the container starts |
| `DATA_REFRESH_ENABLED` | `true` | Run the background refresh scheduler |
| `DATA_REFRESH_INTERVAL_DAYS` | `14` | Days between scheduled refreshes |
| `DATA_REFRESH_FAILURE_RETRY_MINUTES` | `30` | Minutes before retrying a failed refresh |
| `DATA_RELEASE_RETENTION` | `2` | Validated releases kept on the data volume |
| `HEALTH_SYNC_HASH_MAX_BYTES` | `16777216` | Artifact bytes checked during a health request before validation continues in the background |
| `MIN_LION_SOURCE_ROWS` | `200000` | Minimum raw LION rows required for a first release |
| `MIN_DSNY_SOURCE_ROWS` | `500` | Minimum DSNY polygons required for a first release |
| `MIN_OUTPUT_FEATURES` | `100000` | Minimum processed features required for a first release |
| `MAX_COUNT_DROP_PERCENT` | `10` | Maximum permitted count decline from the current release |
| `TILE_MIN_ZOOM` | `11` | Lowest generated vector-tile zoom |
| `TILE_MAX_ZOOM` | `16` | Highest generated vector-tile zoom |
| `TILE_MAX_COMPRESSED_BYTES` | `1572864` | Per-tile compressed-size gate; may only lower the hard ceiling |
| `TILE_MAX_UNCOMPRESSED_BYTES` | `6291456` | Per-tile decoded-size gate; may only lower the hard ceiling |
| `VITE_BASEMAP_TILEJSON_URL` | OpenFreeMap | Basemap TileJSON URL used while building the image |

`VITE_BASEMAP_TILEJSON_URL` is a build setting. Rebuild the image after changing it. The remaining settings apply when the container starts.

For a source build with Docker Compose copy `.env.example` to `.env`. Set `APP_HOST_PORT` in `.env` then run:

```bash
docker compose up -d --build
docker compose logs -f app
```

Compose mounts `./data` at `/app/data`. The container's manifest pointer is fixed at `/app/data/data_manifest.json`; there is no separate loose database or tileset path to configure.

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
| `GET /api/live` | Process liveness; always `200` while the server is running |
| `GET /api/map-config` | Current vector tile URL and map availability |
| `GET /api/tiles/{version}/{z}/{x}/{y}.pbf` | Gzip vector tiles used by the map |

Before the first release is committed, `/api/live` and the frontend return `200`, `/api/health` returns `503`, `/api/map-config` returns `available: false`, and tile URLs return `404`. The basemap remains usable while the frontend retries. After publication, health requires the committed v4 summary and database-v1 tables directly; it does not reconstruct missing release metadata or tolerate older table layouts. `/api/refuse-streets` and `/api/app-config` do not exist.

Interactive API documentation is available at `/docs`, `/redoc`, and `/openapi.json` in development. All three routes return `404` when `APP_ENV=production`.

Development keeps the complete HTTP request-access log. Production suppresses successful request lines but logs every `4xx` and `5xx` response, including `/api/health` readiness failures. Refresh progress and actionable application failures remain in the container log.

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
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

In PowerShell, use `$env:VITE_API_BASE_URL='http://127.0.0.1:8000'; npm run dev`. There is intentionally no Vite proxy; set this URL explicitly whenever the frontend and backend use different origins.

Use `python scripts/run_refresh.py --allow-large-run` to build a local dataset from the official sources. Run `python scripts/run_refresh.py --status` to inspect the current release.

## License

The project code is licensed under the [MIT License](LICENSE). Third-party data and assets retain their own terms.

This map derives street schedules from NYC Department of Sanitation frequency data and NYC Department of City Planning LION street centerlines. Use the official [DSNY collection schedule lookup](https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup) for an address-specific answer.

Basemap data comes from [OpenMapTiles](https://openmaptiles.org/) and [OpenStreetMap](https://www.openstreetmap.org/copyright). See [third-party notices](THIRD_PARTY_NOTICES.md) for licenses.
