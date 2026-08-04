"""Build a bounded block-face pilot from LION and explicit DSNY schedules.

Inputs must be official GeoJSON exports. The script never translates the
letter-valued FREQUENCY field. It only accepts explicit weekday text in
FREQ_REFUSE/refuse_days-like fields.
"""

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import geopandas as gpd
from shapely.geometry import mapping

LOGGER = logging.getLogger("build_pilot")
DAY_CODES = {"MON": "MON", "TUE": "TUE", "WED": "WED", "THU": "THU", "FRI": "FRI", "SAT": "SAT"}
BOROUGHS = {1: "MANHATTAN", 2: "BRONX", 3: "BROOKLYN", 4: "QUEENS", 5: "STATEN ISLAND"}


def normalize_days(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing explicit refuse schedule")
    tokens = [token.strip().upper()[:3] for token in value.replace("/", ",").split(",")]
    days = sorted({DAY_CODES[token] for token in tokens if token in DAY_CODES}, key=list(DAY_CODES).index)
    if not days or len(days) != len(set(tokens) & set(DAY_CODES)):
        raise ValueError(f"invalid or unknown refuse schedule: {value!r}")
    return days


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lion", type=Path, required=True, help="LION GeoJSON or File Geodatabase")
    parser.add_argument("--lion-layer", default="lion", help="Layer name when --lion points to a File Geodatabase")
    parser.add_argument("--frequencies", type=Path, required=True, help="DSNY frequency polygons GeoJSON")
    parser.add_argument("--output", type=Path, default=Path("data/processed/pilot.geojson"))
    parser.add_argument("--failures", type=Path, default=Path("output/pilot_failures.jsonl"))
    parser.add_argument("--limit", type=int, default=None, help="Optional pilot limit; omit for all LION features")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lion = gpd.read_file(args.lion, layer=args.lion_layer if args.lion.suffix.lower() == ".gdb" or args.lion.is_dir() else None)
    frequencies = gpd.read_file(args.frequencies)
    if lion.crs is None or frequencies.crs is None:
        raise ValueError("both inputs must declare a coordinate reference system")
    if lion.crs != frequencies.crs:
        frequencies = frequencies.to_crs(lion.crs)
    lion = lion[lion.geometry.notna() & ~lion.geometry.is_empty].copy()
    if args.limit is not None:
        lion = lion.head(args.limit).copy()
    if lion.empty:
        raise ValueError("LION pilot input contains no usable geometries")
    frequency_fields = {kind: next((field for field in candidates if field in frequencies.columns), None) for kind, candidates in {
        "REFUSE": ("FREQ_REFUSE", "freq_refuse", "refuse_days"),
        "RECYCLING": ("FREQ_RECYCLING", "freq_recycling", "recycling_days"),
        "ORGANICS": ("FREQ_ORGANICS", "freq_organics", "organics_days"),
        "BULK": ("FREQ_BULK", "freq_bulk", "bulk_days"),
    }.items()}
    frequency_field = frequency_fields["REFUSE"]
    if frequency_field is None:
        raise ValueError("frequency input lacks explicit FREQ_REFUSE/refuse_days field")
    pilot_points = lion.copy()
    pilot_points["line_geometry"] = pilot_points.geometry
    pilot_points["geometry"] = pilot_points.geometry.representative_point()
    join_fields = [field for field in frequency_fields.values() if field]
    joined = gpd.sjoin(pilot_points, frequencies[list(dict.fromkeys([*join_fields, "geometry"]))], how="left", predicate="within")
    # Spatial joins may suffix source fields when names overlap. Keep the
    # schedule-field lookup explicit so valid source values are not dropped.
    for kind, field in list(frequency_fields.items()):
        if field and field not in joined.columns and f"{field}_right" in joined.columns:
            frequency_fields[kind] = f"{field}_right"
    output_geometries = gpd.GeoSeries(joined["line_geometry"].tolist(), crs=lion.crs).to_crs(4326)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.failures.parent.mkdir(parents=True, exist_ok=True)
    features = []
    failures = 0
    with args.failures.open("w", encoding="utf-8") as failure_file:
        for output_geometry, (index, row) in zip(output_geometries, joined.iterrows(), strict=True):
            try:
                schedules = {kind: normalize_days(row.get(field)) for kind, field in frequency_fields.items() if field and isinstance(row.get(field), str) and row.get(field).strip()}
                days = schedules.get("REFUSE")
                if not days:
                    raise ValueError("missing explicit refuse schedule")
                street_name = str(row.get("Street") or row.get("street_name") or "").strip()
                segment_id = str(row.get("SegmentID") or row.get("segment_id") or "").strip()
                if not street_name or not segment_id:
                    raise ValueError("missing street name or segment ID")
                for side, id_field, boro_field in (("LEFT", "LBlockFaceID", "LBoro"), ("RIGHT", "RBlockFaceID", "RBoro")):
                    block_face_id = str(row.get(id_field) or "").strip()
                    if not block_face_id or block_face_id.lower() == "nan":
                        raise ValueError(f"missing {id_field}")
                    borough = BOROUGHS.get(int(row[boro_field])) if str(row.get(boro_field, "")).strip() else None
                    if not borough:
                        raise ValueError(f"missing or invalid {boro_field}")
                    geometry = output_geometry
                    features.append({
                        "type": "Feature",
                        "geometry": mapping(geometry),
                        "properties": {
                            "block_face_id": block_face_id,
                            "segment_id": segment_id,
                            "street_name": street_name,
                            "borough": borough,
                            "side": side,
                        "refuse_days": days,
                        "schedules": schedules,
                            "source": "DSNY Frequencies",
                            "retrieved_at": date.today().isoformat(),
                        },
                    })
            except (KeyError, TypeError, ValueError) as error:
                failures += 1
                failure_file.write(json.dumps({"row_index": int(index), "error": str(error)}, ensure_ascii=False) + "\n")
                LOGGER.error("Pilot row failed row_index=%s error=%s", index, error)
    if not features:
        raise RuntimeError("pilot produced no valid block faces; inspect failure file")
    args.output.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    LOGGER.info("Wrote features=%s failures=%s output=%s", len(features), failures, args.output)


if __name__ == "__main__":
    main()
