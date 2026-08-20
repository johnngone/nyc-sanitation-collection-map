# Data sources and completeness contract

The production refresh has two authoritative inputs. AddressPoint, PAD, and the public address-lookup page are not used to build the map.

## Production sources

### DSNY collection-frequency polygons

- Exact layer: [DSNY Frequencies OFFICIAL, FeatureServer layer 0](https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0)
- Exact query endpoint: [FeatureServer/0/query](https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0/query)
- NYC OpenData catalog cross-reference: [DSNY Frequencies (`rv63-53db`)](https://data.cityofnewyork.us/d/rv63-53db)
- Publisher: NYC Department of Sanitation
- Geometry: polygon/multipolygon frequency boundaries; the service declares EPSG:2263 and the downloader requests GeoJSON in EPSG:4326.
- Required identity/schedule fields: the service-declared object ID plus `FREQ_REFUSE`, `FREQ_RECYCLING`, `FREQ_ORGANICS`, and `FREQ_BULK`.

`FREQUENCY` is a DCP-style letter code, not a weekday schedule. Only the four explicit `FREQ_*` fields are normalized. Refuse must have an explicit schedule. Empty values for the other three types mean no schedule for that type; unknown, malformed, or truncated weekday tokens are invalid.

The downloader first obtains the authoritative count and complete object-ID set. It requests exact ID batches, rejects missing, unexpected, or duplicate IDs, and requires the returned total to equal the advertised total. It then fetches the layer metadata, count, and ID set again; a count, ID, or `lastEditDate` change makes the snapshot fail rather than combine two source revisions.

### DCP LION street centerlines

- Exact ZIP used by the worker: [NYC OpenData LION File Geodatabase download](https://data.cityofnewyork.us/download/2v4z-66xt/application/zip)
- Catalog entry: [LION (`2v4z-66xt`)](https://data.cityofnewyork.us/d/2v4z-66xt)
- Publisher page: [NYC Department of City Planning LION](https://www.nyc.gov/content/planning/pages/resources/datasets/lion)
- Field definitions and limitations: [official LION metadata PDF](https://s-media.nyc.gov/agencies/dcp/assets/files/pdf/data-tools/bytes/lion_metadata.pdf)
- Publisher: NYC Department of City Planning
- Geometry: linework in EPSG:2263.
- Required scope/provenance fields include `SegmentTyp`, `FeatureTyp`, `Status`, `NonPed`, `SegmentID`, left/right block-face IDs, left/right boroughs, street name, and left/right address ranges.

The worker streams the archive, verifies the HTTP content length when the server provides one, requires exactly one File Geodatabase, and reads the complete `lion` layer. Its SHA-256 digest, byte size, response `ETag`/`Last-Modified` when present, and parsed row count are preserved with the release.

## Observed source snapshot

The complete download verified on 2026-08-19 contained the following records; the official metadata PDF at that time identified LION as Release 26C:

| Source | Observed records |
|---|---:|
| DSNY frequency polygons | 610 |
| Raw LION `lion` rows | 243,237 |

These are observations, not hard-coded expected totals. Every future refresh records its own counts and hashes. Hard floors reject implausibly small raw inputs/output. Once a release exists, the default relative gate also rejects a greater-than-10% decline in raw LION rows, DSNY polygons, eligible LION rows, matched sides, used DSNY polygons, output features, or schedule rows for any collection type.

## Row and side reconciliation

The ingestion audit accounts for three complete populations:

1. Every raw LION row receives exactly one source-row outcome.
2. Every raw LION row contributes two audited sides, even when the row or side is intentionally outside map scope.
3. Every DSNY polygon is classified as valid-and-used, valid-but-unused, or invalid.

The sums must exactly reconcile to `source_rows`, `source_rows * 2`, and `frequency_rows`. A checksum binds the audit to the exact canonical processed GeoJSON bytes, and the processed feature count must agree throughout the audit, database, MBTiles report, and release manifest.

### Explicit nonfatal outcomes

These outcomes may legitimately produce no map feature, but they are counted and written to the audit rather than discarded silently:

| Outcome | Meaning |
|---|---|
| `out_of_scope` | The LION row is outside the official generic-street/curbside scope, or a boundary side is intentionally outside a borough. |
| `deduplicated_alias` | An exact SegmentID/geometry/block-face alias is represented by its canonical row. |
| `non_addressable` | A side has no usable block-face ID or address range. A usable range can instead receive an explicit `LION:<segment>:<side>` fallback ID. |
| `outside_schedule_area` | The side-offset trace overlaps no DSNY frequency polygon. |
| `partially_outside_schedule_area` | Only part of the trace is covered; the condition remains visible in the audit. |
| `matched` | The side has an unambiguous validated schedule mapping. |

An absent line on the map therefore means “no validated map feature,” not proof that no collection occurs.

### Fatal outcomes

These stop publication:

- `ambiguous`: positive-length overlaps assign conflicting schedule signatures to a side;
- `invalid`: required schema/identity, geometry, borough, provenance, or weekday data is invalid;
- `conflicts`: repeated candidates have an unresolved identity or metadata disagreement that cannot be split deterministically;
- any valid DSNY polygon used by zero eligible LION sides;
- any invalid DSNY polygon, missing required source fields, count mismatch, or reconciliation failure.

There is a diagnostic `--allow-audit-failures` option on `scripts/build_pilot.py`, but the production refresh does not use it. A failed audit cannot be promoted.

## Side-aware spatial join

Processing reprojects both sources to EPSG:2263 and tests the complete line against separate traces offset 25 feet to the left and right of each oriented LION line. This avoids automatically assigning a polygon on one side of a street to both block faces. If GEOS collapses or truncates a tight inside curve, the builder retries a smaller offset only after proving that every LineString component and source-arclength interval remains represented; the requested and actual distances are recorded. If a folded line still has no complete continuous parallel curve, each original primitive is offset independently at the requested distance. This explicit `per_source_segment_offset` audit strategy preserves every source segment and its downstream provenance rather than accepting GEOS's partial curve.

Exact LION aliases are deduplicated with a recorded canonical row. A source side that crosses frequency polygons is clipped into schedule-specific components instead of being classified from one midpoint. Polygon overlaps shorter than 0.25 feet are treated as non-mappable boundary noise; any remaining coverage gap is still visible in the side audit. Repeated block-face IDs that cross a borough boundary are likewise split by `(borough, schedule)` under deterministic feature keys while retaining the original ID as `origin_block_face_id`. Street aliases, segment components, raw source rows, source indices, address evidence, and DSNY object IDs remain attached to the emitted components.

The processed output retains the original block-face identity, contributing LION SegmentIDs/source rows/source records, and every contributing DSNY object ID. SQLite materializes this lineage in `block_face_lion_components` and `block_face_dsny_sources`; release validation requires both provenance tables to cover every stored block face.

## Preserved release evidence

Every committed release contains:

- the exact raw DSNY GeoJSON and LION ZIP;
- `source_report.json` with URLs, hashes, counts, and available server metadata;
- `ingestion_audit.json` with reconciliation totals, outcomes, global errors, and diagnostic records;
- `ingestion_failures.jsonl`, a line-oriented diagnostic view of non-success/fallback records;
- the canonical processed GeoJSON, validated SQLite database, MBTiles archive, tile report, and checksummed release manifest.

The database validation also requires foreign-key integrity, complete schedule/provenance coverage, all four collection types, and one RTree entry per block face. Independent semantic hashes compare each processed feature's identity, geometry, schedules, and provenance with SQLite. Tile validation decodes every zoom, reconciles every tile property with the database, reports per-zoom unique-feature coverage, and requires every database feature to survive at maximum zoom.

## Interpretation limits

This is a spatial visualization derived from official frequency polygons and LION geometry, not an address-specific confirmation service. Boundary geometry and source publication errors can still exist even when the pipeline is internally complete. Use the official [DSNY collection schedule lookup](https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup) for an address-specific answer and consult the source agencies' disclaimers.
