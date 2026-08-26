# Architecture and operations

## Runtime data path

```text
official DSNY + LION sources
            |
   background refresh process
            |
   audited immutable release
   (SQLite + MBTiles + reports)
            |
       manifest pointer
            |
          FastAPI
            |
   versioned gzip PBF tiles
            |
    MapLibre viewport cache
```

FastAPI serves the compiled frontend and `/api/*` on port `8000`. It does not encode geometry during a request. The same standalone container runs the geospatial refresh scheduler in the background; completed release artifacts remain on the mounted data volume.

`GET /api/live` is process liveness and backs the container health check. `GET /api/health` is release readiness and returns `503` until a committed release is fully verified. Health reads the required manifest-v4 summary and database-v1 tables directly; it has no compatibility reconstruction for missing metadata or older schemas. Production disables `/docs`, `/redoc`, and `/openapi.json`; development retains them.

## Vector-tile contract

The tile-v4 archive is an MBTiles SQLite file covering zooms 11–16. `collection_streets` contains known schedule geometry; `collection_unknowns` contains schedule-free unresolved geometry at zooms 14–16. Source-coverage gaps and segments with insufficient address evidence both appear from zoom 14. The runtime accepts only manifest v4 releases and tile schema v4.

| Property | Meaning |
|---|---|
| `id` | Unique stored feature key |
| `origin_block_face_id` | Original LION block-face ID |
| `street_name` | Display street name |
| `borough`, `side` | Borough and `LEFT`/`RIGHT` side |
| `refuse_days` | Comma-separated weekday codes |
| `recycling_days` | Comma-separated weekday codes |
| `organics_days` | Comma-separated weekday codes |
| `bulk_days` | Comma-separated weekday codes |
| `source`, `retrieved_at` | Schedule provenance |
| `*_status`, `*_conflict` | Per-type evidence state and whether an explicit source schedule conflicts with the general policy relationship |

Blank day lists with `UNKNOWN_SOURCE_BLANK` mean the official source schedule is unavailable, never that service does not exist. Every known block face has exactly four state rows. Unknown-layer features contain only `street_name`, `side`, `reason_code`, and `reason`; detailed audit lineage remains in SQLite and release reports instead of being repeated in every tile placement.

A line crossing a tile boundary is clipped into each intersecting tile. `feature_count` is the unique source count; `tile_feature_count` includes these per-tile placements.

At maximum zoom, the builder first uses the normal simplified geometry and retries the exact source geometry if quantization would collapse it. A source line that still maps to a single vector-tile coordinate in every relevant buffered tile is classified as nonrenderable at the configured maximum zoom. It remains in GeoJSON, SQLite, and the audit trail, but does not block publication. The tile report records each such ID, its projected length, its reason, and a digest of the complete ID set. Release validation independently projects and tests the source geometry; an omitted line that could render, an unexplained missing ID, or a count/digest mismatch still fails the release.

MapLibre fetches the current contract from `/api/map-config`, then requests only visible PBF tiles. The API translates XYZ requests to MBTiles TMS rows and returns gzip content without decompressing it. Versioned URLs have `Cache-Control: public, max-age=31536000, immutable` and an `ETag`; empty valid coordinates return `204`.

## Tile size gates and metrics

Each build fails before publication if any tile exceeds either default ceiling:

- compressed PBF: 1.5 MiB;
- uncompressed PBF: 6 MiB.

The `TILE_MAX_COMPRESSED_BYTES` and `TILE_MAX_UNCOMPRESSED_BYTES` settings (and equivalent `scripts/build_tiles.py` flags) may lower these gates for stricter deployments. They cannot raise the hard release ceilings; increasing those requires a reviewed code change so an environment typo cannot silently trade away download/decode latency.

`tile_build_report.json`, MBTiles metadata, and release validation record overall and per-zoom tile counts; compressed and uncompressed totals, maxima, and p95 sizes; initial-zoom bytes; build limits; source feature/count bindings; per-zoom unique-feature coverage; and maximum-zoom rendered/nonrenderable reconciliation. This makes a performance regression or renderability exception a release artifact rather than a browser surprise.

## Immutable, atomic releases

A successful refresh installs this shape under the mounted data directory:

```text
data/
  data_manifest.json
  releases/
    <dataset-version>/
      app.sqlite3
      collection_streets.mbtiles
      citywide.geojson
      ingestion_audit.json
      ingestion_failures.jsonl
      tile_build_report.json
      source_report.json
      dsny_frequencies.geojson
      lion.zip
      pad.zip
      unknown_block_faces.geojson
      addresspoint_query_report.json
      cscl_alignment_report.json
      cscl_alignment_subset.geojson
      recovery_shadow_report.json
      recovery_diff.json
      release_manifest.json
```

The dataset version combines the processing timestamp and processed-data digest. Each artifact descriptor carries a SHA-256 checksum and relevant count/version bindings.

Every attempt downloads and hashes the authoritative source snapshots first. If those bytes and revisions, the processing code/runtime, and the relevant build configuration exactly match the committed release, the refresh reruns the regression floors and then skips extraction, processing, SQLite loading, and tile generation.

For a changed release, processed GeoJSON is parsed and normalized once; the same validated objects feed semantic hashing and SQLite loading. The final bundle gate reuses those in-process Stage 5-7 results while rechecking every artifact hash and cross-artifact binding. It then atomically renames the private directory into `releases/` on the same filesystem—without copying the multi-gigabyte bundle—and atomically replaces `data_manifest.json` last. That manifest is the sole commit pointer, so the app never intentionally combines a database from one refresh with tiles from another. Every present non-v4 manifest fails closed; loose database, tileset, and `.previous` files are never served.

The complete persisted contract is manifest v4, processed GeoJSON schema v3, ingestion audit v3, database schema v1, and tile schema v4. Missing `/app/data/data_manifest.json` is the only valid pre-release state in the container. Its location is fixed to the mounted data volume; loose database and tileset paths are not configurable. The database revision is bound in `dataset_metadata`, the manifest database summary, and the database artifact descriptor.

On first access to a new production-sized release, the app hashes the committed database and tileset once on a background worker. While this single-flight check is running, `/api/health` returns HTTP `503` with `Committed artifact checksums are verifying`, and `/api/map-config` reports `available: false`; the frontend retries both after 0.5 seconds and exponentially backs off to a 15-second cap. Verified results are cached against the artifact path, size, modification time, and expected digest. `HEALTH_SYNC_HASH_MAX_BYTES` is the maximum total artifact size verified inline (16 MiB by default). Lowering it moves more checks to the background; raising it can make a health request block on more I/O. Every artifact is still hashed.

By default the manifest authorizes the current release plus one previous release. It also authorizes that prior tile version so browser requests already in flight continue to work across a switch. `DATA_RELEASE_RETENTION` can increase retention but cannot be below two. Cleanup happens after the pointer switch and is best effort; only validated, recognized release directories are eligible, and an unreferenced directory is never live merely because it remains on disk.

### Inspect and roll back

```bash
python scripts/run_refresh.py --status
python scripts/activate_release.py <dataset-version>
```

Inside the standalone container:

```bash
docker exec nyc-sanitation-map python scripts/run_refresh.py --status
docker exec nyc-sanitation-map python scripts/activate_release.py <dataset-version>
```

Activation revalidates the installed bundle and then atomically switches the pointer. It works only while that release is retained. Back up the persistent volume before manual recovery work.

`scripts/promote_staging.py <bundle-directory>` is for a fully prebuilt bundle containing `release_manifest.json` and every checksummed artifact. It revalidates the same release and regression gates; it is not a way to promote loose database or tile files.

## Refresh gates

The background refresh will not change the manifest unless all of these pass:

1. Complete source download and stable DSNY snapshot checks.
2. Full LION row, LION-side, and DSNY-frequency reconciliation with zero fatal audit outcomes.
3. Processed GeoJSON byte digest, per-feature semantic digest, and count binding.
4. Exact database-schema-v1 and processed-to-SQLite identity/geometry/schedule/provenance reconciliation plus integrity and foreign-key checks.
5. Decoded all-zoom tile-property reconciliation to SQLite, maximum-zoom rendered/nonrenderable reconciliation with independent source-geometry verification, gzip, coordinate, and tile-size checks.
6. Cross-artifact SHA-256, version, and count checks for the whole release bundle.
7. Count floors and regression limits.

Default first-release floors are 200,000 raw LION rows, 500 DSNY polygons, and 100,000 output features. These deliberately sit below the observed 243,237/610/169,000-plus scale while still rejecting a severely truncated first snapshot. Against an existing manifest, the default 10% relative-drop limit covers raw LION rows, DSNY polygons, eligible LION rows, matched sides, used DSNY polygons, output features, and each collection type's schedule-row count. Configure the floors with `MIN_LION_SOURCE_ROWS`, `MIN_DSNY_SOURCE_ROWS`, and `MIN_OUTPUT_FEATURES`; configure the relative gate with `MAX_COUNT_DROP_PERCENT`. These are anomaly alarms, not expected-count substitutions—the observed source counts are still reconciled exactly and saved in every release.

## Running refreshes

The scheduler runs inside the standalone container on startup when `DATA_REFRESH_ON_STARTUP=true`, then waits `DATA_REFRESH_INTERVAL_DAYS` between successful attempts. A failure leaves the current manifest unchanged and retries after `DATA_REFRESH_FAILURE_RETRY_MINUTES` (30 minutes by default; minimum one minute).

For a one-time local run from the repository root:

```bash
python -m pip install -e ".[refresh]"
python scripts/run_refresh.py --allow-large-run
```

The `--allow-large-run` flag is required. Useful expert overrides include `--tile-minzoom`, `--tile-maxzoom`, `--side-offset-feet`, `--release-retention`, the three count-floor flags, and `--max-count-drop-percent`.

## Fresh v2 deployment and rollback

Deploy v2 with a new empty data directory. Before switching, record the current image digest, dataset version, health/counts, per-type schedule counts, and disk usage; set the three first-release floors to at least 90% of the recorded LION, DSNY, and output-feature counts. Stop v1, move its data directory to a timestamped backup, create an empty replacement, and start the immutable v2 image with startup refresh enabled.

During the first eight-stage refresh, `/api/live` and the frontend are available, `/api/health` is `503`, `/api/map-config` reports `available: false`, and the UI is basemap-only. After publication require health `200`, manifest v4, database schema v1, tile schema v4, verified integrity, acceptable counts, a real gzip tile response, and browser checks of every control.

If verification fails, stop v2, preserve its failed data directory for diagnosis, restore the untouched v1 backup, and redeploy the pinned v1 image. Keep the backup through at least one later successful v2 refresh and a tested activation rollback between retained v4 releases.

## Troubleshooting checklist

| Symptom | Check |
|---|---|
| Basemap only on a new install | The first refresh may still be running; watch the container log and `/api/map-config`. |
| `/api/health` returns `503` on a new volume | No valid release has been committed yet; monitor `/api/live`, `/api/map-config`, and the refresh log. |
| Refresh exits before promotion | Read the first validation error in the container log; the previous release remains live. |
| Refresh repeatedly starts after recreation | Set `DATA_REFRESH_ON_STARTUP=false` after initialization. |
| Health returns `503` with checksums `verifying` | Expected briefly after startup or a release switch; keep polling while the single background hash completes. |
| Health returns `503` with checksums `invalid` or another integrity error | The manifest, checksum, or database/tileset metadata is invalid; restore a backup or activate a retained valid release. |
| Tile request returns `404` | The version is not current/retained, the zoom is outside the archive range, or coordinates are invalid. |
| Refresh cannot publish | Verify the container's `/app/data` mount is writable and has free space. |
