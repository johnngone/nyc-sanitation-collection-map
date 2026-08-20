"""Validate, aggregate, and load processed GeoJSON into SQLite."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry.base import BaseGeometry
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.database import initialize
from scripts.build_pilot import DAY_ORDER, SCHEDULE_FIELDS, combine_line_geometries

LOGGER = logging.getLogger("load_processed")
VALID_DAYS = set(DAY_ORDER)
COLLECTION_TYPES = tuple(SCHEDULE_FIELDS)


@dataclass(frozen=True)
class ValidatedFeature:
    block_face_id: str
    origin_block_face_id: str
    segment_ids: tuple[str, ...]
    street_name: str
    street_names: tuple[str, ...]
    borough: str
    side: str
    geometry: BaseGeometry
    schedules: dict[str, tuple[str, ...]]
    dsny_object_ids: tuple[str, ...]
    dsny_sources: tuple[dict[str, object], ...]
    lion_components: tuple[dict[str, object], ...]
    source: str
    retrieved_at: str


def prepare_features(payload: object) -> list[ValidatedFeature]:
    """Validate all input before writes and aggregate repeated block-face IDs."""

    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError("input must be a GeoJSON FeatureCollection")
    raw_features = payload.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("FeatureCollection features must be a list")

    groups: dict[str, list[ValidatedFeature]] = defaultdict(list)
    for feature_number, feature in enumerate(raw_features):
        validated = _validate_feature(feature, feature_number)
        groups[validated.block_face_id].append(validated)

    aggregated: list[ValidatedFeature] = []
    for block_face_id in sorted(groups):
        group = groups[block_face_id]
        conflicts = _conflicting_fields(group)
        if conflicts:
            raise ValueError(
                f"conflicting metadata/schedules for block_face_id {block_face_id}: "
                f"{', '.join(conflicts)}"
            )
        first = group[0]
        geometry = combine_line_geometries(item.geometry for item in group)
        segment_ids = tuple(sorted({segment_id for item in group for segment_id in item.segment_ids}))
        dsny_object_ids = tuple(sorted({object_id for item in group for object_id in item.dsny_object_ids}))
        street_names = tuple(sorted({name for item in group for name in item.street_names}))
        display_street_name = sorted({item.street_name for item in group})[0]
        dsny_sources = _merge_provenance_records(
            (record for item in group for record in item.dsny_sources),
            key="object_id",
            label="DSNY OBJECTID",
        )
        lion_components = _deduplicate_json_records(
            record for item in group for record in item.lion_components
        )
        aggregated.append(ValidatedFeature(
            block_face_id=block_face_id,
            origin_block_face_id=first.origin_block_face_id,
            segment_ids=segment_ids,
            street_name=display_street_name,
            street_names=street_names,
            borough=first.borough,
            side=first.side,
            geometry=geometry,
            schedules=first.schedules,
            dsny_object_ids=dsny_object_ids,
            dsny_sources=dsny_sources,
            lion_components=lion_components,
            source=first.source,
            retrieved_at=first.retrieved_at,
        ))
    return aggregated


def load_payload(payload: object, database: str | Path) -> int:
    """Atomically load a fully validated payload and return its feature count."""

    features = prepare_features(payload)
    if not features:
        raise ValueError("input contains no features")
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # SQLite cannot add a NOT NULL column without a table rebuild. Existing
        # databases receive a nullable migration column, then this full-snapshot
        # transaction replaces every row with a non-null audited origin.
        _ensure_columns(connection, "block_faces", {"origin_block_face_id": "TEXT"})
        connection.execute(
            """CREATE TABLE IF NOT EXISTS block_face_dsny_sources (
                block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id) ON DELETE CASCADE,
                dsny_object_id TEXT NOT NULL,
                frequency_row INTEGER,
                schedule_code TEXT,
                section TEXT,
                district TEXT,
                PRIMARY KEY (block_face_id, dsny_object_id)
            )"""
        )
        _ensure_columns(connection, "block_face_dsny_sources", {
            "frequency_row": "INTEGER",
            "schedule_code": "TEXT",
            "section": "TEXT",
            "district": "TEXT",
        })
        connection.execute(
            """CREATE TABLE IF NOT EXISTS block_face_lion_components (
                block_face_id TEXT NOT NULL REFERENCES block_faces(block_face_id) ON DELETE CASCADE,
                component_index INTEGER NOT NULL,
                segment_id TEXT NOT NULL,
                source_side TEXT NOT NULL CHECK (source_side IN ('LEFT', 'RIGHT')),
                source_rows_json TEXT NOT NULL,
                source_indices_json TEXT NOT NULL,
                street_names_json TEXT NOT NULL,
                source_records_json TEXT NOT NULL,
                dsny_object_ids_json TEXT NOT NULL,
                PRIMARY KEY (block_face_id, component_index)
            )"""
        )
        # This loader consumes a complete snapshot. Clear every spatial and
        # relational row in the same transaction so removed faces cannot
        # survive a successful refresh.
        connection.execute("DELETE FROM collection_schedules")
        connection.execute("DELETE FROM block_face_lion_components")
        connection.execute("DELETE FROM block_face_dsny_sources")
        connection.execute("DELETE FROM block_faces_rtree")
        connection.execute("DELETE FROM block_face_rtree_map")
        connection.execute("DELETE FROM block_faces")
        for feature in features:
            min_x, min_y, max_x, max_y = feature.geometry.bounds
            stored_segment_ids = "|".join(feature.segment_ids)
            connection.execute(
                """INSERT INTO block_faces
                (block_face_id, origin_block_face_id, segment_id, borough, street_name, side,
                 geometry_wkt, min_x, min_y, max_x, max_y)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feature.block_face_id,
                    feature.origin_block_face_id,
                    stored_segment_ids,
                    feature.borough,
                    feature.street_name,
                    feature.side,
                    _compatible_wkt(feature.geometry),
                    min_x,
                    min_y,
                    max_x,
                    max_y,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO block_face_rtree_map (block_face_id) VALUES (?)",
                (feature.block_face_id,),
            )
            rtree_id = connection.execute(
                "SELECT rtree_id FROM block_face_rtree_map WHERE block_face_id = ?",
                (feature.block_face_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT OR REPLACE INTO block_faces_rtree "
                "(rtree_id, min_x, max_x, min_y, max_y) VALUES (?, ?, ?, ?, ?)",
                (rtree_id, min_x, max_x, min_y, max_y),
            )
            schedule_rows = [
                (
                    feature.block_face_id,
                    collection_type,
                    day,
                    feature.source,
                    feature.retrieved_at,
                )
                for collection_type in COLLECTION_TYPES
                for day in feature.schedules[collection_type]
            ]
            connection.executemany(
                """INSERT INTO collection_schedules
                (block_face_id, collection_type, weekday, source, retrieved_at, validation_status)
                VALUES (?, ?, ?, ?, ?, 'AUDITED_SIDE_TRACE_V2')""",
                schedule_rows,
            )
            connection.executemany(
                """INSERT INTO block_face_dsny_sources
                (block_face_id, dsny_object_id, frequency_row, schedule_code, section, district)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        feature.block_face_id,
                        source["object_id"],
                        source.get("frequency_row"),
                        source.get("schedule_code"),
                        source.get("section"),
                        source.get("district"),
                    )
                    for source in feature.dsny_sources
                ],
            )
            connection.executemany(
                """INSERT INTO block_face_lion_components
                (block_face_id, component_index, segment_id, source_side, source_rows_json,
                 source_indices_json, street_names_json, source_records_json, dsny_object_ids_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        feature.block_face_id,
                        component_index,
                        component["segment_id"],
                        component["source_side"],
                        _canonical_json(component["source_rows"]),
                        _canonical_json(component["source_indices"]),
                        _canonical_json(component["street_names"]),
                        _canonical_json(component["source_records"]),
                        _canonical_json(component["dsny_object_ids"]),
                    )
                    for component_index, component in enumerate(feature.lion_components)
                ],
            )
    return len(features)


def _validate_feature(feature: object, feature_number: int) -> ValidatedFeature:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError(f"feature {feature_number} must be a GeoJSON Feature")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"feature {feature_number} properties must be an object")
    required = (
        "block_face_id",
        "origin_block_face_id",
        "segment_id",
        "street_name",
        "street_names",
        "borough",
        "side",
        "refuse_days",
        "schedules",
        "dsny_object_ids",
        "dsny_sources",
        "lion_components",
        "source",
        "retrieved_at",
    )
    missing = [field for field in required if not properties.get(field)]
    if missing:
        raise ValueError(f"feature {feature_number} missing required fields: {missing}")

    block_face_id = _required_text(properties["block_face_id"], "block_face_id", feature_number)
    feature_key = properties.get("feature_key", block_face_id)
    if _required_text(feature_key, "feature_key", feature_number) != block_face_id:
        raise ValueError(f"feature {feature_number} feature_key must equal block_face_id")
    origin_block_face_id = _required_text(
        properties["origin_block_face_id"],
        "origin_block_face_id",
        feature_number,
    )
    street_name = _required_text(properties["street_name"], "street_name", feature_number)
    street_names = _text_list(properties["street_names"], "street_names", feature_number)
    if street_name not in street_names:
        raise ValueError(f"feature {feature_number} street_names must include street_name")
    borough = _required_text(properties["borough"], "borough", feature_number)
    side = _required_text(properties["side"], "side", feature_number).upper()
    if side not in {"LEFT", "RIGHT"}:
        raise ValueError(f"feature {feature_number} has invalid side {side!r}")
    source = _required_text(properties["source"], "source", feature_number)
    retrieved_at = _required_text(properties["retrieved_at"], "retrieved_at", feature_number)
    segment_ids = _segment_ids(properties, feature_number)

    try:
        geometry = shape(feature["geometry"])
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"feature {feature_number} has invalid geometry: {error}") from None
    if (
        geometry.geom_type not in {"LineString", "MultiLineString"}
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.length <= 0
    ):
        raise ValueError(
            f"block face {block_face_id} must have a valid non-empty LineString or MultiLineString"
        )

    schedules_value = properties["schedules"]
    if not isinstance(schedules_value, dict):
        raise ValueError(f"feature {feature_number} schedules must be an object")
    missing_types = [kind for kind in COLLECTION_TYPES if kind not in schedules_value]
    unknown_types = sorted(set(schedules_value) - set(COLLECTION_TYPES))
    if missing_types or unknown_types:
        raise ValueError(
            f"feature {feature_number} schedules must contain exactly {', '.join(COLLECTION_TYPES)}; "
            f"missing={missing_types}, unknown={unknown_types}"
        )
    schedules = {
        collection_type: _validate_days(
            schedules_value[collection_type],
            f"schedules.{collection_type}",
            feature_number,
            allow_empty=collection_type != "REFUSE",
        )
        for collection_type in COLLECTION_TYPES
    }
    refuse_days = _validate_days(
        properties["refuse_days"],
        "refuse_days",
        feature_number,
        allow_empty=False,
    )
    if refuse_days != schedules["REFUSE"]:
        raise ValueError(
            f"feature {feature_number} refuse_days does not match schedules.REFUSE"
        )
    dsny_object_ids = _dsny_object_ids(properties["dsny_object_ids"], feature_number)
    dsny_sources = _dsny_sources(properties["dsny_sources"], feature_number)
    if tuple(source["object_id"] for source in dsny_sources) != dsny_object_ids:
        raise ValueError(
            f"feature {feature_number} dsny_sources OBJECTIDs must exactly match dsny_object_ids"
        )
    lion_components = _lion_components(
        properties["lion_components"],
        feature_number,
        segment_ids=segment_ids,
        dsny_object_ids=dsny_object_ids,
    )

    return ValidatedFeature(
        block_face_id=block_face_id,
        origin_block_face_id=origin_block_face_id,
        segment_ids=segment_ids,
        street_name=street_name,
        street_names=street_names,
        borough=borough,
        side=side,
        geometry=geometry,
        schedules=schedules,
        dsny_object_ids=dsny_object_ids,
        dsny_sources=dsny_sources,
        lion_components=lion_components,
        source=source,
        retrieved_at=retrieved_at,
    )


def _validate_days(
    value: object,
    field_name: str,
    feature_number: int,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"feature {feature_number} {field_name} must be a list")
    if not value and not allow_empty:
        raise ValueError(f"feature {feature_number} {field_name} must not be empty")
    if any(not isinstance(day, str) for day in value):
        raise ValueError(f"feature {feature_number} {field_name} contains a non-string day")
    unknown = sorted(set(value) - VALID_DAYS)
    if unknown:
        raise ValueError(
            f"feature {feature_number} {field_name} contains unknown weekday token(s): {unknown}"
        )
    if len(value) != len(set(value)):
        raise ValueError(f"feature {feature_number} {field_name} contains duplicate weekdays")
    return tuple(day for day in DAY_ORDER if day in value)


def _segment_ids(properties: dict[str, object], feature_number: int) -> tuple[str, ...]:
    raw_ids = properties.get("segment_ids", [properties["segment_id"]])
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError(f"feature {feature_number} segment_ids must be a non-empty list")
    segment_ids = {_required_text(value, "segment_ids", feature_number) for value in raw_ids}
    segment_ids.add(_required_text(properties["segment_id"], "segment_id", feature_number))
    return tuple(sorted(segment_ids))


def _dsny_object_ids(value: object, feature_number: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"feature {feature_number} dsny_object_ids must be a non-empty list")
    object_ids = tuple(_required_text(item, "dsny_object_ids", feature_number) for item in value)
    if len(object_ids) != len(set(object_ids)):
        raise ValueError(f"feature {feature_number} dsny_object_ids contains duplicates")
    return tuple(sorted(object_ids))


def _text_list(value: object, field_name: str, feature_number: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"feature {feature_number} {field_name} must be a non-empty list")
    normalized = tuple(_required_text(item, field_name, feature_number) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"feature {feature_number} {field_name} contains duplicates")
    return tuple(sorted(normalized))


def _dsny_sources(value: object, feature_number: int) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"feature {feature_number} dsny_sources must be a non-empty list")
    normalized: list[dict[str, object]] = []
    for source_number, raw_source in enumerate(value):
        if not isinstance(raw_source, dict):
            raise ValueError(
                f"feature {feature_number} dsny_sources[{source_number}] must be an object"
            )
        source: dict[str, object] = {
            "object_id": _required_text(
                raw_source.get("object_id"),
                f"dsny_sources[{source_number}].object_id",
                feature_number,
            ),
        }
        frequency_row = raw_source.get("frequency_row")
        if not isinstance(frequency_row, int) or isinstance(frequency_row, bool) or frequency_row < 0:
            raise ValueError(
                f"feature {feature_number} dsny_sources[{source_number}].frequency_row "
                "must be a non-negative integer"
            )
        source["frequency_row"] = frequency_row
        for field_name in ("schedule_code", "section", "district"):
            raw_value = raw_source.get(field_name)
            if raw_value is not None:
                source[field_name] = _required_text(
                    raw_value,
                    f"dsny_sources[{source_number}].{field_name}",
                    feature_number,
                )
        normalized.append(source)
    return _merge_provenance_records(normalized, key="object_id", label="DSNY OBJECTID")


def _lion_components(
    value: object,
    feature_number: int,
    *,
    segment_ids: tuple[str, ...],
    dsny_object_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"feature {feature_number} lion_components must be a non-empty list")
    normalized: list[dict[str, object]] = []
    for component_number, raw_component in enumerate(value):
        if not isinstance(raw_component, dict):
            raise ValueError(
                f"feature {feature_number} lion_components[{component_number}] must be an object"
            )
        segment_id = _required_text(
            raw_component.get("segment_id"),
            f"lion_components[{component_number}].segment_id",
            feature_number,
        )
        if segment_id not in segment_ids:
            raise ValueError(
                f"feature {feature_number} LION component segment_id is absent from segment_ids"
            )
        source_side = _required_text(
            raw_component.get("source_side"),
            f"lion_components[{component_number}].source_side",
            feature_number,
        ).upper()
        if source_side not in {"LEFT", "RIGHT"}:
            raise ValueError(
                f"feature {feature_number} LION component has invalid source_side {source_side!r}"
            )
        source_rows = raw_component.get("source_rows")
        if (
            not isinstance(source_rows, list)
            or not source_rows
            or any(not isinstance(row, int) or isinstance(row, bool) or row < 0 for row in source_rows)
            or len(source_rows) != len(set(source_rows))
        ):
            raise ValueError(
                f"feature {feature_number} lion_components[{component_number}].source_rows "
                "must contain unique non-negative integers"
            )
        if source_rows != sorted(source_rows):
            raise ValueError(
                f"feature {feature_number} lion_components[{component_number}].source_rows "
                "must be sorted so parallel provenance remains aligned"
            )
        source_indices = raw_component.get("source_indices")
        if not isinstance(source_indices, list) or len(source_indices) != len(source_rows):
            raise ValueError(
                f"feature {feature_number} lion_components[{component_number}].source_indices "
                "must align one-for-one with source_rows"
            )
        street_names = _text_list(
            raw_component.get("street_names"),
            f"lion_components[{component_number}].street_names",
            feature_number,
        )
        source_records = raw_component.get("source_records")
        if (
            not isinstance(source_records, list)
            or len(source_records) != len(source_rows)
            or any(not isinstance(record, dict) for record in source_records)
        ):
            raise ValueError(
                f"feature {feature_number} lion_components[{component_number}].source_records "
                "must align one-for-one with source_rows"
            )
        for record_number, (source_row, source_index, source_record) in enumerate(
            zip(source_rows, source_indices, source_records, strict=True)
        ):
            required_record_fields = {"source_row", "source_index", "segment_id"}
            missing_record_fields = required_record_fields - set(source_record)
            if missing_record_fields:
                raise ValueError(
                    f"feature {feature_number} lion_components[{component_number}]"
                    f".source_records[{record_number}] is missing required provenance fields: "
                    f"{sorted(missing_record_fields)}"
                )
            record_source_row = source_record["source_row"]
            if (
                not isinstance(record_source_row, int)
                or isinstance(record_source_row, bool)
                or record_source_row != source_row
            ):
                raise ValueError(
                    f"feature {feature_number} lion_components[{component_number}]"
                    f".source_records[{record_number}].source_row does not align with source_rows"
                )
            if _canonical_json(source_record["source_index"]) != _canonical_json(source_index):
                raise ValueError(
                    f"feature {feature_number} lion_components[{component_number}]"
                    f".source_records[{record_number}].source_index does not align with source_indices"
                )
            record_segment_id = _required_text(
                source_record["segment_id"],
                f"lion_components[{component_number}].source_records[{record_number}].segment_id",
                feature_number,
            )
            if record_segment_id != segment_id:
                raise ValueError(
                    f"feature {feature_number} lion_components[{component_number}]"
                    f".source_records[{record_number}].segment_id does not match the component"
                )
        component_object_ids = _dsny_object_ids(
            raw_component.get("dsny_object_ids"),
            feature_number,
        )
        if not set(component_object_ids).issubset(dsny_object_ids):
            raise ValueError(
                f"feature {feature_number} LION component DSNY IDs are absent from dsny_object_ids"
            )
        component = {
            "segment_id": segment_id,
            "source_side": source_side,
            "source_rows": source_rows,
            "source_indices": source_indices,
            "street_names": list(street_names),
            "source_records": source_records,
            "dsny_object_ids": list(component_object_ids),
        }
        _canonical_json(component)
        normalized.append(component)
    components = _deduplicate_json_records(normalized)
    component_segment_ids = {str(component["segment_id"]) for component in components}
    if component_segment_ids != set(segment_ids):
        raise ValueError(
            f"feature {feature_number} LION component segment IDs must exactly match segment_ids; "
            f"components={sorted(component_segment_ids)}, feature={list(segment_ids)}"
        )
    component_dsny_object_ids = {
        str(object_id)
        for component in components
        for object_id in component["dsny_object_ids"]
    }
    if component_dsny_object_ids != set(dsny_object_ids):
        raise ValueError(
            f"feature {feature_number} LION component DSNY IDs must exactly match dsny_object_ids; "
            f"components={sorted(component_dsny_object_ids)}, feature={list(dsny_object_ids)}"
        )
    return components


def _merge_provenance_records(
    records: object,
    *,
    key: str,
    label: str,
) -> tuple[dict[str, object], ...]:
    by_key: dict[str, dict[str, object]] = {}
    for record in records:
        record_key = str(record[key])
        existing = by_key.get(record_key)
        if existing is not None and _canonical_json(existing) != _canonical_json(record):
            raise ValueError(f"conflicting provenance for {label} {record_key}")
        by_key[record_key] = record
    return tuple(by_key[record_key] for record_key in sorted(by_key))


def _deduplicate_json_records(records: object) -> tuple[dict[str, object], ...]:
    unique: dict[str, dict[str, object]] = {}
    for record in records:
        unique[_canonical_json(record)] = record
    return tuple(unique[key] for key in sorted(unique))


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"provenance must be JSON serializable: {error}") from None


def _required_text(value: object, field_name: str, feature_number: int) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"feature {feature_number} {field_name} must be non-empty text")
    return " ".join(str(value).strip().split())


def _conflicting_fields(group: list[ValidatedFeature]) -> list[str]:
    conflicts = []
    for field_name in ("origin_block_face_id", "borough", "side", "source", "retrieved_at"):
        if len({getattr(item, field_name) for item in group}) > 1:
            conflicts.append(field_name)
    schedule_signatures = {
        tuple((kind, item.schedules[kind]) for kind in COLLECTION_TYPES)
        for item in group
    }
    if len(schedule_signatures) > 1:
        conflicts.append("schedules")
    return conflicts


def _compatible_wkt(geometry: BaseGeometry) -> str:
    # The current API's deliberately small WKT reader expects no whitespace
    # between MultiLineString components.
    return geometry.wkt.replace("), (", "),(")


def _ensure_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }
    for column_name, declaration in columns.items():
        if column_name not in existing:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/app.sqlite3"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    feature_count = load_payload(payload, args.database)
    LOGGER.info("Loaded %s aggregated features into %s", feature_count, args.database)


if __name__ == "__main__":
    main()
