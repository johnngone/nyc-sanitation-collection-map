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

## Vector-tile contract

The default v3 archive is an MBTiles SQLite file covering zooms 12–17. `collection_streets` contains known schedule geometry; `collection_unknowns` contains schedule-free unresolved geometry at high zoom. The runtime continues to read retained v2 releases during rollout.

| Property | Meaning |
|---|---|
| `id` | Unique stored feature key |
| `origin_block_face_id` | Original LION block-face ID when that column is present |
| `name`, `street_name` | Display street name |
| `borough`, `side` | Borough and `LEFT`/`RIGHT` side |
| `refuse_days` | Comma-separated weekday codes |
| `recycling_days` | Comma-separated weekday codes |
| `organics_days` | Comma-separated weekday codes |
| `bulk_days` | Comma-separated weekday codes |
| `source`, `retrieved_at` | Schedule provenance |
| `*_status`, `*_rule`, `*_conflict`, `*_provenance` | Per-type evidence state and policy provenance |
| `identity_method`, `geometry_method` | How identity and geometry were validated |

Blank day lists with `UNKNOWN_SOURCE_BLANK` mean the official source schedule is unavailable, never that service does not exist. Every known block face has exactly four state rows. Unknown-layer features contain no weekday or status properties and must also survive maximum zoom exactly once.

A line crossing a tile boundary is clipped into each intersecting tile. `feature_count` is the unique source count; `tile_feature_count` includes these per-tile placements.

MapLibre fetches the current contract from `/api/map-config`, then requests only visible PBF tiles. The API translates XYZ requests to MBTiles TMS rows and returns gzip content without decompressing it. Versioned URLs have `Cache-Control: public, max-age=31536000, immutable` and an `ETag`; empty valid coordinates return `204`.

## Tile size gates and metrics

Each build fails before publication if any tile exceeds either default ceiling:

- compressed PBF: 500 KiB;
- uncompressed PBF: 5 MiB.

The `TILE_MAX_COMPRESSED_BYTES` and `TILE_MAX_UNCOMPRESSED_BYTES` settings (and equivalent `scripts/build_tiles.py` flags) may lower these gates for stricter deployments. They cannot raise the hard release ceilings; increasing those requires a reviewed code change so an environment typo cannot silently trade away download/decode latency.

`tile_build_report.json`, MBTiles metadata, and release validation record overall and per-zoom tile counts; compressed and uncompressed totals, maxima, and p95 sizes; initial-zoom bytes; build limits; source feature/count bindings; per-zoom unique-feature coverage; and complete maximum-zoom coverage. This makes a performance regression a release artifact rather than a browser surprise.

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
      recovery_shadow_report.json
      recovery_diff.json
      release_manifest.json
```

The dataset version combines the processing timestamp and processed-data digest. Each artifact descriptor carries a SHA-256 checksum and relevant count/version bindings.

The background refresh builds in a temporary directory on the same volume, validates the complete bundle, installs a new immutable release directory, and atomically replaces `data_manifest.json` last. That manifest is the sole commit pointer, so the app never intentionally combines a database from one refresh with tiles from another. Malformed committed v2 or v3 pointers fail closed instead of falling back to loose legacy files.

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
4. Exact processed-to-SQLite identity/geometry/schedule/provenance reconciliation plus integrity, foreign-key, and RTree checks.
5. Decoded all-zoom tile-property reconciliation to SQLite, full maximum-zoom feature coverage, gzip, coordinate, and tile-size checks.
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

## Legacy GeoJSON endpoint

`GET /api/refuse-streets` remains available for compatibility and diagnostics. It accepts `day`, comma-separated `types`, and an optional complete `west/south/east/north` bounding box. It is capped at 20,000 features and returns HTTP `413` rather than silently truncating an oversized query. The production frontend does not use this endpoint.

## Troubleshooting checklist

| Symptom | Check |
|---|---|
| Basemap only on a new install | The first refresh may still be running; watch the container log and `/api/map-config`. |
| `/api/health` shows `map_available: false` | No valid tileset has been committed yet, or the configured/shared volume is wrong. |
| Refresh exits before promotion | Read the first validation error in the container log; the previous release remains live. |
| Refresh repeatedly starts after recreation | Set `DATA_REFRESH_ON_STARTUP=false` after initialization. |
| Health returns `503` with checksums `verifying` | Expected briefly after startup or a release switch; keep polling while the single background hash completes. |
| Health returns `503` with checksums `invalid` or another integrity error | The manifest, checksum, or database/tileset metadata is invalid; restore a backup or activate a retained valid release. |
| Tile request returns `404` | The version is not current/retained, the zoom is outside the archive range, or coordinates are invalid. |
| Legacy request returns `413` | Use vector tiles or a smaller bounding box. |
| Refresh cannot publish | Verify the container's `/app/data` mount is writable and has free space. |
