# Phase 2: Official data sources

Research date: 2026-08-03.

## Recommended source set

### DSNY Frequencies — primary schedule boundary source

- Official landing page: https://data.cityofnewyork.us/d/rv63-53db
- Dataset identifier: `rv63-53db`
- Official Socrata API: `https://data.cityofnewyork.us/resource/rv63-53db.json`
- Small-sample query: `https://data.cityofnewyork.us/resource/rv63-53db.json?$limit=3`
- Official bulk/API metadata: `https://data.cityofnewyork.us/api/views/rv63-53db`
- Format: Socrata JSON/CSV; geometry is a `multipolygon` WKT field.
- Geometry: frequency boundary polygons.
- Important fields: `district`, `section`, `frequency`, `schedulecode`, `freq_refuse`, `freq_recycling`, `freq_organics`, `freq_bulk`, `multipolygon`.
- CRS: the Socrata metadata describes the geometry as WKT; the official ArcGIS service reports EPSG:2263 (NAD83 / New York Long Island feet). Confirm the Socrata response's geometry encoding before ingestion.
- Weekday status: `frequency` is explicitly documented as a DCP letter code. Do not use it as weekdays. The current DSNY ArcGIS layer exposes `FREQ_REFUSE` values such as `Mon, Thu` and `Mon, Wed, Fri`.
- Last-updated evidence: Socrata catalog metadata reports 2024-04-10; the current DSNY ArcGIS layer reports data/edit timestamps of 2026-04-08. These should be compared during ingestion.
- Attribution: NYC Department of Sanitation / NYC OpenData.

Current DSNY ArcGIS layer for cross-checking:

`https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0`

Its metadata reports polygon geometry, EPSG:2263, and the fields `FREQUENCY`, `FREQ_REFUSE`, `FREQ_RECYCLING`, `FREQ_ORGANICS`, and `FREQ_BULK`. Its renderer lists the observed refuse schedules as `Mon, Wed, Fri`, `Tue, Thu, Sat`, `Mon, Thu`, `Tue, Fri`, and `Wed, Sat`.

### LION — street centerline and block-face reference

- Official DCP resource page: https://www.nyc.gov/content/planning/pages/resources/datasets/lion
- NYC OpenData catalog entry: https://data.cityofnewyork.us/d/2v4z-66xt
- Download endpoint: `https://data.cityofnewyork.us/download/2v4z-66xt/application/zip`
- Metadata: https://s-media.nyc.gov/agencies/dcp/assets/files/pdf/data-tools/bytes/lion_metadata.pdf
- Format: ESRI File Geodatabase ZIP; metadata describes a polyline feature class.
- Geometry: single-line NYC street representation.
- CRS: EPSG:2263.
- Important fields: `Street`, `FaceCode`, `SeqNum`, `SegmentID`, `LBoro`, `RBoro`, `LBlockFaceID`, `RBlockFaceID`, and left/right address-range fields.
- Precision: metadata reports 243,237 features and a spatial index. `LBlockFaceID` and `RBlockFaceID` identify the two sides separately.
- Last-updated evidence: current metadata is release 26B, published May 19, 2026.
- Attribution/licensing: NYC Department of City Planning; metadata includes a City/DCP accuracy disclaimer.

### AddressPoint — representative address points

- Official NYC OpenData catalog entry: https://data.cityofnewyork.us/d/uf93-f8nk
- Dataset identifier: `uf93-f8nk`
- API: `https://data.cityofnewyork.us/resource/uf93-f8nk.json`
- Small-sample query: `https://data.cityofnewyork.us/resource/uf93-f8nk.json?$limit=3`
- Format: Socrata JSON/CSV with point geometry.
- Purpose: supplement the address information supplied by the city street centerline; candidate source for selecting representative addresses on a block face.
- Last-updated evidence: catalog reports June 30, 2026.
- Attribution: NYC OpenData.

### PAD — address/BIN/BBL support candidate

- Official DCP resource page: https://www.nyc.gov/content/planning/pages/resources/datasets/pad
- NYC OpenData catalog entry: https://data.cityofnewyork.us/d/bc8t-ecyu
- Download: `https://data.cityofnewyork.us/download/bc8t-ecyu/application/zip`
- Format: ZIP containing ASCII comma-delimited tax-lot and address files.
- Purpose: supplemental address, BIN, and BBL relationships; not a street geometry source.

## What is not established yet

The public DSNY frequency boundaries provide strong evidence of regular refuse schedules, but we have not yet validated that a boundary polygon can be joined directly to both sides of every LION segment. That validation belongs to Phases 4–7. We will not assume that one polygon schedule automatically proves both block faces have the same schedule.

