"""Build audited block-face collection features from official NYC sources.

The spatial join is intentionally side-aware. LION centerlines are projected
to EPSG:2263 and sampled a short distance to the left and right of the line so
the two block faces can resolve to different DSNY frequency polygons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from time import monotonic
from typing import Iterable

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring, transform, unary_union

LOGGER = logging.getLogger("build_pilot")
WORKING_CRS = "EPSG:2263"
DEFAULT_SIDE_OFFSET_FEET = 25.0
DEFAULT_TRACE_TOLERANCE_FEET = 1.0
# DSNY polygon boundaries and floating-point overlay can produce microscopic
# positive-length slivers.  Three inches is far below a usable street feature
# or MVT grid cell, but avoids emitting a separate schedule component that no
# renderer can faithfully display.  The resulting uncovered trace remains in
# the side audit rather than being silently accepted.
MIN_MAPPABLE_TRACE_FEET = 0.25
OFFSET_COVERAGE_TOLERANCE_FEET = 1e-5
MAX_INNER_JOIN_TRIM_FRACTION = 0.25
NEAR_REVERSAL_COSINE = -0.95
PROGRESS_EVERY_PREPARED_SEGMENTS = 5_000
PROGRESS_EVERY_SOURCE_ROWS = 25_000
DAY_ORDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT")
DAY_ALIASES = {
    "MON": "MON",
    "MONDAY": "MON",
    "TUE": "TUE",
    "TUES": "TUE",
    "TUESDAY": "TUE",
    "WED": "WED",
    "WEDNESDAY": "WED",
    "THU": "THU",
    "THUR": "THU",
    "THURS": "THU",
    "THURSDAY": "THU",
    "FRI": "FRI",
    "FRIDAY": "FRI",
    "SAT": "SAT",
    "SATURDAY": "SAT",
}
SCHEDULE_FIELDS = {
    "REFUSE": "FREQ_REFUSE",
    "RECYCLING": "FREQ_RECYCLING",
    "ORGANICS": "FREQ_ORGANICS",
    "BULK": "FREQ_BULK",
}
FREQUENCY_ID_FIELD_NAMES = ("OBJECTID", "ObjectID", "objectid")
BOROUGHS = {
    1: "MANHATTAN",
    2: "BRONX",
    3: "BROOKLYN",
    4: "QUEENS",
    5: "STATEN ISLAND",
}
SIDE_FIELDS = (
    ("LEFT", "LBlockFaceID", "LBoro"),
    ("RIGHT", "RBlockFaceID", "RBoro"),
)
SIDE_ADDRESS_FIELDS = {
    "LEFT": ("FromLeft", "ToLeft", "LLo_Hyphen", "LHi_Hyphen"),
    "RIGHT": ("FromRight", "ToRight", "RLo_Hyphen", "RHi_Hyphen"),
}
IN_SCOPE_SEGMENT_TYPES = {"B", "G", "E", "F", "U"}
IN_SCOPE_FEATURE_TYPES = {"0", "6", "C", "W", "A", "F"}
REQUIRED_LION_FIELDS = (
    "SegmentTyp",
    "FeatureTyp",
    "Status",
    "NonPed",
    "SegmentID",
    "LBlockFaceID",
    "RBlockFaceID",
    "LBoro",
    "RBoro",
    "Street",
    "FromLeft",
    "ToLeft",
    "FromRight",
    "ToRight",
)
OUTCOME_KEYS = (
    "matched",
    "out_of_scope",
    "deduplicated_alias",
    "non_addressable",
    "outside_schedule_area",
    "partially_outside_schedule_area",
    "ambiguous",
    "invalid",
    "conflicts",
)
FATAL_OUTCOMES = ("ambiguous", "invalid", "conflicts")


@dataclass
class SideResult:
    source_row: int
    source_index: object
    side: str
    segment_id: str
    block_face_id: str
    status: str = "pending"
    reason: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def audit_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "source_row": self.source_row,
            "source_index": _json_scalar(self.source_index),
            "side": self.side,
            "outcome": self.status,
            "reason": self.reason or self.status,
        }
        if self.segment_id:
            record["segment_id"] = self.segment_id
        if self.block_face_id:
            record["block_face_id"] = self.block_face_id
        record.update(self.details)
        return record


@dataclass
class SideCandidate:
    result: SideResult
    segment_id: str
    block_face_id: str
    street_name: str
    borough: str
    side: str
    geometry: BaseGeometry
    schedules: dict[str, tuple[str, ...]]
    frequency_rows: tuple[int, ...]
    dsny_object_ids: tuple[str, ...]
    source_rows: tuple[int, ...]
    source_indices: tuple[object, ...]
    street_names: tuple[str, ...]
    source_records: tuple[dict[str, object], ...]


@dataclass
class PreparedLionSegment:
    source_row: int
    source_index: object
    alias_source_rows: tuple[int, ...]
    source_indices: tuple[object, ...]
    row: pd.Series
    segment_id: str
    street_names: tuple[str, ...]
    source_records: tuple[dict[str, object], ...]
    boroughs_by_side: dict[str, str | None]
    address_ranges_by_side: dict[str, tuple[dict[str, object], ...]]
    boundary_out_of_scope_by_side: dict[str, bool]


@dataclass
class FrequencyTraceMatch:
    overlap_feet: float
    trace_geometry: BaseGeometry
    source_geometry: BaseGeometry
    used_tolerance: bool = False


def normalize_days(value: object) -> list[str]:
    """Normalize an explicit weekday string and reject every unknown token."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing explicit schedule")
    raw_tokens = re.split(r"[,/;]", value)
    tokens = [token.strip().upper() for token in raw_tokens]
    if any(not token for token in tokens):
        raise ValueError(f"invalid empty weekday token in schedule: {value!r}")
    unknown = sorted({token for token in tokens if token not in DAY_ALIASES})
    if unknown:
        raise ValueError(f"unknown weekday token(s) {', '.join(unknown)} in schedule: {value!r}")
    normalized = {DAY_ALIASES[token] for token in tokens}
    return [day for day in DAY_ORDER if day in normalized]


def side_offset_point(geometry: BaseGeometry, side: str, distance_feet: float) -> Point:
    """Return the midpoint of a complete side-offset trace."""

    trace = side_offset_trace(geometry, side, distance_feet)
    line = _sample_line(trace)
    return line.interpolate(line.length / 2)


def side_offset_trace(geometry: BaseGeometry, side: str, distance_feet: float) -> BaseGeometry:
    """Offset every line component to a block-face side in EPSG:2263."""

    offset_pairs, _, _ = _side_offset_pairs(geometry, side, distance_feet)
    offset_lines = [offset_line for _, offset_line in offset_pairs]
    return offset_lines[0] if len(offset_lines) == 1 else MultiLineString(offset_lines)


def build_collection_features(
    lion: gpd.GeoDataFrame,
    frequencies: gpd.GeoDataFrame,
    *,
    side_offset_feet: float = DEFAULT_SIDE_OFFSET_FEET,
    trace_tolerance_feet: float = DEFAULT_TRACE_TOLERANCE_FEET,
    retrieved_at: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build a GeoJSON payload and a complete, machine-checkable side audit."""

    if not math.isfinite(side_offset_feet) or side_offset_feet <= 0:
        raise ValueError("side_offset_feet must be a positive finite number")
    if not math.isfinite(trace_tolerance_feet) or trace_tolerance_feet < 0:
        raise ValueError("trace_tolerance_feet must be a finite non-negative number")
    if lion.crs is None or frequencies.crs is None:
        raise ValueError("both inputs must declare a coordinate reference system")

    LOGGER.info(
        "Preparing spatial inputs lion_rows=%s frequency_polygons=%s working_crs=%s",
        len(lion),
        len(frequencies),
        WORKING_CRS,
    )
    lion_working = lion.to_crs(WORKING_CRS).copy()
    frequencies_working = frequencies.to_crs(WORKING_CRS).copy()
    source_rows = len(lion_working)
    expected_sides = source_rows * 2
    source_indices = list(lion_working.index)
    lion_working = lion_working.reset_index(drop=True)
    frequencies_working = frequencies_working.reset_index(drop=True)
    side_results: list[SideResult] = []
    candidates: dict[str, list[SideCandidate]] = defaultdict(list)
    global_errors: list[dict[str, object]] = []
    prepared_segments, source_row_outcomes, lion_preparation = _prepare_lion_segments(
        lion_working,
        source_indices,
        side_results,
        global_errors,
    )
    LOGGER.info(
        "Prepared LION physical segments=%s raw_rows=%s in_scope_rows=%s aliases_deduplicated=%s",
        len(prepared_segments),
        source_rows,
        source_row_outcomes["in_scope"],
        source_row_outcomes["deduplicated_alias"],
    )

    missing_schedule_fields = [field for field in SCHEDULE_FIELDS.values() if field not in frequencies_working.columns]
    frequency_id_field = _frequency_id_field(frequencies_working.columns)
    missing_source_fields = [*missing_schedule_fields]
    if frequency_id_field is None:
        missing_source_fields.append("OBJECTID")
    parsed_schedules: dict[int, dict[str, tuple[str, ...]]] = {}
    frequency_object_ids: dict[int, str] = {}
    frequency_sources: dict[int, dict[str, object]] = {}
    invalid_frequency_rows: dict[int, list[str]] = {}
    frequency_schedule_empty_rows = _frequency_schedule_empty_rows(frequencies_working)
    if missing_source_fields:
        global_errors.append({
            "kind": "missing_frequency_fields",
            "fields": missing_source_fields,
            "message": "frequency input must contain OBJECTID and all four official schedule fields",
        })
        invalid_frequency_rows = {
            int(position): [f"missing required source fields: {', '.join(missing_source_fields)}"]
            for position in range(len(frequencies_working))
        }
    else:
        parsed_schedules, invalid_frequency_rows, frequency_object_ids = _parse_frequency_rows(
            frequencies_working,
            frequency_id_field,
        )
        frequency_sources = {
            int(position): _frequency_source_record(row, int(position), frequency_id_field)
            for position, row in frequencies_working.iterrows()
        }
        for frequency_row, errors in invalid_frequency_rows.items():
            global_errors.append({
                "kind": "invalid_frequency_row",
                "frequency_row": frequency_row,
                "dsny_object_id": frequency_object_ids.get(frequency_row),
                "errors": errors,
            })

    frequency_index = frequencies_working.sindex if not frequencies_working.empty else None
    encountered_frequency_rows: set[int] = set()
    spatial_join_started = monotonic()
    for prepared_number, prepared in enumerate(prepared_segments, start=1):
        source_row = prepared.source_row
        row = prepared.row
        source_index = prepared.source_index
        street_name = (
            _clean_text(_first_present(row, "Street", "SAFStreetName", "street_name"))
            or (prepared.street_names[0] if prepared.street_names else "")
        )
        segment_id = prepared.segment_id
        geometry = row.geometry
        geometry_error = _line_error(geometry)
        for side, id_field, borough_field in SIDE_FIELDS:
            raw_block_face_id = _clean_identifier(row.get(id_field))
            block_face_id = raw_block_face_id
            used_fallback_id = False
            address_ranges = prepared.address_ranges_by_side[side]
            if _is_non_addressable(row.get(id_field)) and address_ranges:
                block_face_id = f"LION:{segment_id}:{side}"
                used_fallback_id = True
            result = SideResult(
                source_row=int(source_row),
                source_index=source_index,
                side=side,
                segment_id=segment_id,
                block_face_id=block_face_id,
            )
            side_results.append(result)

            if used_fallback_id:
                result.details.update({
                    "fallback_block_face_id": block_face_id,
                    "raw_block_face_id": raw_block_face_id,
                    # Keep the legacy singular detail for operators while also
                    # retaining every exact LION alias that established the
                    # fallback side's addressability.
                    "address_range": dict(address_ranges[0]),
                    "address_ranges": [dict(address_range) for address_range in address_ranges],
                })
            elif _is_non_addressable(row.get(id_field)):
                result.status = "non_addressable"
                result.reason = f"{id_field} is missing/zero and the side has no usable address range"
                continue
            if missing_source_fields:
                result.status = "invalid"
                result.reason = "frequency input is missing required provenance or schedule fields"
                result.details["missing_fields"] = missing_source_fields
                continue
            if geometry_error:
                result.status = "invalid"
                result.reason = geometry_error
                continue
            if not street_name:
                result.status = "invalid"
                result.reason = "missing street name"
                continue
            if not segment_id:
                result.status = "invalid"
                result.reason = "missing segment ID"
                continue
            borough = prepared.boroughs_by_side[side]
            if borough is None:
                if prepared.boundary_out_of_scope_by_side[side]:
                    result.status = "out_of_scope"
                    result.reason = f"{borough_field} is intentionally absent on a boundary/outside side"
                    result.details.update({
                        "segment_type": _source_code(row.get("SegmentTyp")),
                        "location_status": _source_code(row.get("LocStatus")),
                        "borough_boundary": _json_scalar(row.get("BoroBndry")),
                    })
                else:
                    result.status = "invalid"
                    result.reason = f"missing or invalid {borough_field}"
                continue

            try:
                matches, trace_length, used_side_offset_feet, offset_strategy = _matching_frequency_parts(
                    frequencies_working,
                    frequency_index,
                    geometry,
                    side,
                    side_offset_feet,
                    trace_tolerance_feet,
                )
            except (TypeError, ValueError) as error:
                result.status = "invalid"
                result.reason = str(error)
                continue
            if used_side_offset_feet != side_offset_feet:
                result.details.update({
                    "requested_side_offset_feet": side_offset_feet,
                    "used_side_offset_feet": used_side_offset_feet,
                    "side_offset_fallback": True,
                })
            if offset_strategy != "continuous_line_offset":
                result.details.update({
                    "side_offset_strategy": offset_strategy,
                    "side_offset_segment_fallback": True,
                })
            match_rows = sorted(matches)
            encountered_frequency_rows.update(match_rows)
            if not match_rows:
                result.status = "outside_schedule_area"
                result.reason = "side-offset trace did not overlap a frequency polygon"
                result.details["trace_length_feet"] = trace_length
                continue
            invalid_matches = [frequency_row for frequency_row in match_rows if frequency_row in invalid_frequency_rows]
            if invalid_matches:
                result.status = "invalid"
                result.reason = "side-offset trace overlaps an invalid frequency row"
                result.details["frequency_rows"] = invalid_matches
                result.details["dsny_object_ids"] = [
                    frequency_object_ids.get(frequency_row)
                    for frequency_row in invalid_matches
                ]
                result.details["errors"] = {
                    str(frequency_row): invalid_frequency_rows[frequency_row]
                    for frequency_row in invalid_matches
                }
                continue
            covered_trace_feet = min(
                trace_length,
                float(unary_union([
                    matches[frequency_row].trace_geometry
                    for frequency_row in match_rows
                ]).length),
            )
            uncovered_trace_feet = max(0.0, trace_length - covered_trace_feet)
            if uncovered_trace_feet > max(0.01, trace_length * 1e-6):
                result.status = "partially_outside_schedule_area"
                result.reason = "only part of the side-offset trace overlaps frequency polygons"
                result.details.update({
                    "trace_length_feet": trace_length,
                    "covered_trace_feet": covered_trace_feet,
                    "uncovered_trace_feet": uncovered_trace_feet,
                    "coverage_ratio": covered_trace_feet / trace_length,
                })
            overlap_details = [
                {
                    "frequency_row": frequency_row,
                    "dsny_object_id": frequency_object_ids[frequency_row],
                    "overlap_feet": matches[frequency_row].overlap_feet,
                    "used_tolerance": matches[frequency_row].used_tolerance,
                    "schedules": {
                        kind: list(parsed_schedules[frequency_row][kind])
                        for kind in SCHEDULE_FIELDS
                    },
                }
                for frequency_row in match_rows
            ]
            conflicting_overlaps = _conflicting_frequency_overlaps(matches, parsed_schedules)
            if conflicting_overlaps:
                result.status = "ambiguous"
                result.reason = "conflicting frequency polygons overlap the same positive-length side trace"
                result.details["frequency_overlaps"] = overlap_details
                result.details["overlapping_frequency_pairs"] = [list(pair) for pair in conflicting_overlaps]
                result.details["dsny_object_ids"] = sorted({
                    frequency_object_ids[frequency_row]
                    for frequency_row in match_rows
                })
                continue
            rows_by_schedule: dict[tuple[tuple[str, tuple[str, ...]], ...], list[int]] = defaultdict(list)
            for frequency_row in match_rows:
                signature = tuple(
                    (kind, parsed_schedules[frequency_row][kind])
                    for kind in SCHEDULE_FIELDS
                )
                rows_by_schedule[signature].append(frequency_row)
            result.details["matched_schedule_components"] = len(rows_by_schedule)
            result.details["frequency_overlaps"] = overlap_details
            for signature in sorted(rows_by_schedule):
                schedule_rows = tuple(sorted(rows_by_schedule[signature]))
                try:
                    component_geometry = combine_line_geometries(
                        matches[frequency_row].source_geometry
                        for frequency_row in schedule_rows
                    )
                except ValueError as error:
                    result.status = "invalid"
                    result.reason = f"could not map frequency overlap to source geometry: {error}"
                    break
                matched_object_ids = tuple(sorted({
                    frequency_object_ids[frequency_row]
                    for frequency_row in schedule_rows
                }))
                candidates[block_face_id].append(SideCandidate(
                    result=result,
                    segment_id=segment_id,
                    block_face_id=block_face_id,
                    street_name=street_name,
                    borough=borough,
                    side=side,
                    geometry=component_geometry,
                    schedules={kind: days for kind, days in signature},
                    frequency_rows=schedule_rows,
                    dsny_object_ids=matched_object_ids,
                    source_rows=tuple(sorted((source_row, *prepared.alias_source_rows))),
                    source_indices=prepared.source_indices,
                    street_names=prepared.street_names,
                    source_records=prepared.source_records,
                ))

        if (
            prepared_number % PROGRESS_EVERY_PREPARED_SEGMENTS == 0
            or prepared_number == len(prepared_segments)
        ):
            LOGGER.info(
                "Spatial join progress segments=%s/%s sides=%s candidate_groups=%s dsny_polygons_touched=%s elapsed_s=%.1f",
                prepared_number,
                len(prepared_segments),
                len(side_results),
                len(candidates),
                len(encountered_frequency_rows),
                monotonic() - spatial_join_started,
            )

    output_features: list[dict[str, object]] = []
    conflict_groups = 0
    split_feature_groups = 0
    split_output_features = 0
    borough_split_feature_groups = 0
    borough_split_output_features = 0
    to_wgs84 = Transformer.from_crs(WORKING_CRS, "EPSG:4326", always_xy=True)
    retrieved = retrieved_at or date.today().isoformat()
    LOGGER.info(
        "Aggregating validated side matches candidate_groups=%s elapsed_s=%.1f",
        len(candidates),
        monotonic() - spatial_join_started,
    )
    for origin_block_face_id in sorted(candidates):
        origin_group = sorted(
            candidates[origin_block_face_id],
            key=lambda candidate: (candidate.segment_id, candidate.result.source_row, candidate.side),
        )
        groups_by_identity: dict[
            tuple[str, tuple[tuple[str, tuple[str, ...]], ...]],
            list[SideCandidate],
        ] = defaultdict(list)
        for candidate in origin_group:
            signature = tuple((kind, candidate.schedules[kind]) for kind in SCHEDULE_FIELDS)
            groups_by_identity[(candidate.borough, signature)].append(candidate)
        if len(groups_by_identity) > 1:
            split_feature_groups += 1
            split_output_features += len(groups_by_identity)
        boroughs = {candidate.borough for candidate in origin_group}
        if len(boroughs) > 1:
            borough_split_feature_groups += 1
            borough_split_output_features += len(groups_by_identity)

        for borough, signature in sorted(groups_by_identity):
            group = groups_by_identity[(borough, signature)]
            try:
                canonical_side = group[0].side
                combined_geometry = combine_line_geometries(
                    _orient_geometry_for_display(candidate.geometry, candidate.side, canonical_side)
                    for candidate in group
                )
            except ValueError as error:
                for candidate in group:
                    candidate.result.status = "invalid"
                    candidate.result.reason = f"could not aggregate repeated block-face geometry: {error}"
                continue
            for candidate in group:
                if candidate.result.status == "pending":
                    candidate.result.status = "matched"
            first = group[0]
            feature_key = (
                origin_block_face_id
                if len(groups_by_identity) == 1
                else _split_feature_key(origin_block_face_id, borough, signature)
            )
            segment_ids = sorted({candidate.segment_id for candidate in group})
            dsny_object_ids = sorted({
                object_id
                for candidate in group
                for object_id in candidate.dsny_object_ids
            })
            matched_frequency_rows = sorted({
                frequency_row
                for candidate in group
                for frequency_row in candidate.frequency_rows
            })
            dsny_sources = [frequency_sources[frequency_row] for frequency_row in matched_frequency_rows]
            street_names = sorted({name for candidate in group for name in candidate.street_names})
            display_street_name = sorted({candidate.street_name for candidate in group})[0]
            lion_components = [
                {
                    "segment_id": candidate.segment_id,
                    "source_side": candidate.side,
                    "source_rows": list(candidate.source_rows),
                    "source_indices": list(candidate.source_indices),
                    "street_names": list(candidate.street_names),
                    "source_records": list(candidate.source_records),
                    "dsny_object_ids": list(candidate.dsny_object_ids),
                }
                for candidate in group
            ]
            schedules = {kind: list(first.schedules[kind]) for kind in SCHEDULE_FIELDS}
            output_geometry = transform(to_wgs84.transform, combined_geometry)
            output_features.append({
                "type": "Feature",
                "geometry": mapping(output_geometry),
                "properties": {
                    "block_face_id": feature_key,
                    "feature_key": feature_key,
                    "origin_block_face_id": origin_block_face_id,
                    "segment_id": segment_ids[0],
                    "segment_ids": segment_ids,
                    "source_segment_count": len(segment_ids),
                    "source_component_count": len(group),
                    "street_name": display_street_name,
                    "street_names": street_names,
                    "borough": first.borough,
                    "side": canonical_side,
                    "lion_components": lion_components,
                    "refuse_days": schedules["REFUSE"],
                    "schedules": schedules,
                    "dsny_object_ids": dsny_object_ids,
                    "dsny_sources": dsny_sources,
                    "source": "DSNY Frequencies",
                    "retrieved_at": retrieved,
                },
            })

    payload = {"type": "FeatureCollection", "features": output_features}
    audit = _build_audit(
        source_rows=source_rows,
        frequency_rows=len(frequencies_working),
        expected_sides=expected_sides,
        side_results=side_results,
        output_features=len(output_features),
        conflict_groups=conflict_groups,
        side_offset_feet=side_offset_feet,
        trace_tolerance_feet=trace_tolerance_feet,
        global_errors=global_errors,
        valid_frequency_rows=set(parsed_schedules),
        invalid_frequency_rows=set(invalid_frequency_rows),
        encountered_frequency_rows=encountered_frequency_rows,
        frequency_object_ids=frequency_object_ids,
        frequency_id_field=frequency_id_field,
        source_row_outcomes=source_row_outcomes,
        lion_preparation=lion_preparation,
        frequency_schedule_empty_rows=frequency_schedule_empty_rows,
        split_feature_groups=split_feature_groups,
        split_output_features=split_output_features,
        borough_split_feature_groups=borough_split_feature_groups,
        borough_split_output_features=borough_split_output_features,
    )
    LOGGER.info(
        "Finished spatial audit output_features=%s classified_sides=%s expected_sides=%s elapsed_s=%.1f",
        len(output_features),
        audit["classified_sides"],
        audit["expected_sides"],
        monotonic() - spatial_join_started,
    )
    return payload, audit


def combine_line_geometries(geometries: Iterable[BaseGeometry]) -> BaseGeometry:
    """Combine every line component without silently replacing repeated IDs."""

    lines: list[LineString] = []
    seen_wkb: set[bytes] = set()
    for geometry in geometries:
        if isinstance(geometry, LineString):
            components = [_two_dimensional_line(geometry)]
        elif isinstance(geometry, MultiLineString):
            components = [_two_dimensional_line(line) for line in geometry.geoms]
        else:
            raise ValueError(f"unsupported geometry type {geometry.geom_type}")
        for component in components:
            if component.wkb in seen_wkb:
                continue
            seen_wkb.add(component.wkb)
            lines.append(component)
    if not lines:
        raise ValueError("no line geometry supplied")
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def _prepare_lion_segments(
    lion: gpd.GeoDataFrame,
    source_indices: list[object],
    side_results: list[SideResult],
    global_errors: list[dict[str, object]],
) -> tuple[list[PreparedLionSegment], dict[str, int], dict[str, object]]:
    """Apply the official Generic layer scope and deduplicate SegmentID aliases."""

    row_outcomes = {
        "in_scope": 0,
        "out_of_scope": 0,
        "curbside_out_of_scope": 0,
        "deduplicated_alias": 0,
        "invalid": 0,
    }
    missing_scope_fields = [field for field in REQUIRED_LION_FIELDS if field not in lion.columns]
    if missing_scope_fields:
        global_errors.append({
            "kind": "missing_lion_scope_fields",
            "fields": missing_scope_fields,
            "message": "LION input is missing fields required for scope and curbside reconciliation",
        })
        for source_row, row in lion.iterrows():
            row_outcomes["invalid"] += 1
            _append_preclassified_sides(
                side_results,
                row,
                int(source_row),
                source_indices[int(source_row)],
                "invalid",
                f"missing official LION scope fields: {', '.join(missing_scope_fields)}",
            )
        return [], row_outcomes, {
            "segment_alias_groups": 0,
            "multi_geometry_segment_ids": [],
            "identity_conflict_groups": 0,
        }

    LOGGER.info("Classifying official LION scope and curbside eligibility rows=%s", len(lion))
    grouped: dict[str, list[int]] = defaultdict(list)
    for source_number, (source_row, row) in enumerate(lion.iterrows(), start=1):
        if source_number % PROGRESS_EVERY_SOURCE_ROWS == 0 or source_number == len(lion):
            LOGGER.info(
                "LION scope progress rows=%s/%s physical_segment_ids=%s",
                source_number,
                len(lion),
                len(grouped),
            )
        source_row = int(source_row)
        segment_type = _source_code(row.get("SegmentTyp"))
        feature_type = _source_code(row.get("FeatureTyp"))
        if segment_type not in IN_SCOPE_SEGMENT_TYPES or feature_type not in IN_SCOPE_FEATURE_TYPES:
            row_outcomes["out_of_scope"] += 1
            _append_preclassified_sides(
                side_results,
                row,
                source_row,
                source_indices[source_row],
                "out_of_scope",
                "row is excluded by the official LION Streets - Generic layer query",
                details={"segment_type": segment_type, "feature_type": feature_type},
            )
            continue
        curbside_exclusion = _curbside_exclusion(row)
        if curbside_exclusion is not None:
            row_outcomes["curbside_out_of_scope"] += 1
            _append_preclassified_sides(
                side_results,
                row,
                source_row,
                source_indices[source_row],
                "out_of_scope",
                curbside_exclusion,
                details={
                    "official_generic_scope": True,
                    "status": _source_code(row.get("Status")),
                    "non_pedestrian": _source_code(row.get("NonPed")),
                },
            )
            continue
        segment_id = _clean_identifier(_first_present(row, "SegmentID", "segment_id"))
        if not segment_id:
            row_outcomes["invalid"] += 1
            _append_preclassified_sides(
                side_results,
                row,
                source_row,
                source_indices[source_row],
                "invalid",
                "in-scope LION row is missing SegmentID",
            )
            continue
        grouped[segment_id].append(source_row)

    prepared: list[PreparedLionSegment] = []
    alias_groups = 0
    identity_conflict_groups = 0
    multi_geometry_segment_ids: list[dict[str, object]] = []
    sorted_segment_ids = sorted(grouped)
    LOGGER.info("Normalizing LION aliases physical_segment_ids=%s", len(sorted_segment_ids))
    for segment_number, segment_id in enumerate(sorted_segment_ids, start=1):
        positions = sorted(grouped[segment_id])
        geometry_groups: dict[str, list[int]] = defaultdict(list)
        identity_groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        geometry_block_faces: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for position in positions:
            row = lion.iloc[position]
            geometry_key = _exact_geometry_key(row.geometry, position)
            left_id = _clean_identifier(row.get("LBlockFaceID"))
            right_id = _clean_identifier(row.get("RBlockFaceID"))
            geometry_groups[geometry_key].append(position)
            identity_groups[(geometry_key, left_id, right_id)].append(position)
            geometry_block_faces[geometry_key].add((left_id, right_id))

        if len(geometry_groups) > 1:
            multi_geometry_segment_ids.append({
                "segment_id": segment_id,
                "geometry_groups": len(geometry_groups),
                "source_rows": positions,
            })

        conflicting_positions: set[int] = set()
        for geometry_key, block_face_pairs in geometry_block_faces.items():
            if not geometry_key.startswith("invalid:") and len(block_face_pairs) > 1:
                conflict_positions = sorted(geometry_groups[geometry_key])
                conflicting_positions.update(conflict_positions)
                identity_conflict_groups += 1
                global_errors.append({
                    "kind": "segment_identity_conflict",
                    "segment_id": segment_id,
                    "source_rows": conflict_positions,
                    "conflicting_fields": ["LBlockFaceID", "RBlockFaceID"],
                    "block_face_pairs": [list(pair) for pair in sorted(block_face_pairs)],
                })
        for source_row in sorted(conflicting_positions):
            row_outcomes["invalid"] += 1
            _append_preclassified_sides(
                side_results,
                lion.iloc[source_row],
                source_row,
                source_indices[source_row],
                "invalid",
                "same SegmentID and geometry have conflicting block-face identity",
                details={"segment_id": segment_id},
            )

        for identity_key in sorted(identity_groups):
            identity_positions = sorted(identity_groups[identity_key])
            if any(position in conflicting_positions for position in identity_positions):
                continue
            boroughs_by_side, borough_conflicts = _alias_boroughs(lion, identity_positions)
            if borough_conflicts:
                identity_conflict_groups += 1
                global_errors.append({
                    "kind": "segment_borough_conflict",
                    "segment_id": segment_id,
                    "source_rows": identity_positions,
                    "conflicting_fields": borough_conflicts,
                })
                for source_row in identity_positions:
                    row_outcomes["invalid"] += 1
                    _append_preclassified_sides(
                        side_results,
                        lion.iloc[source_row],
                        source_row,
                        source_indices[source_row],
                        "invalid",
                        "exact LION aliases have conflicting borough identity",
                        details={"conflicting_fields": borough_conflicts},
                    )
                continue

            canonical_row = _canonical_alias_position(lion, identity_positions)
            alias_rows = tuple(position for position in identity_positions if position != canonical_row)
            if alias_rows:
                alias_groups += 1
            row_outcomes["in_scope"] += 1
            for alias_row in alias_rows:
                row_outcomes["deduplicated_alias"] += 1
                _append_preclassified_sides(
                    side_results,
                    lion.iloc[alias_row],
                    alias_row,
                    source_indices[alias_row],
                    "deduplicated_alias",
                    "exact SegmentID/geometry/block-face alias is sampled once",
                    details={
                        "canonical_source_row": canonical_row,
                        "identity_group_source_rows": identity_positions,
                        "segment_id": segment_id,
                    },
                )
            street_names = _lion_street_names(lion, identity_positions)
            source_records = tuple(
                _lion_source_record(lion.iloc[position], position, source_indices[position])
                for position in identity_positions
            )
            address_ranges_by_side = _alias_address_ranges(
                lion,
                identity_positions,
                source_indices,
            )
            boundary_out_of_scope_by_side = {
                side: all(
                    _missing_borough_is_out_of_scope(lion.iloc[position], side)
                    for position in identity_positions
                )
                for side, _, _ in SIDE_FIELDS
            }
            prepared.append(PreparedLionSegment(
                source_row=canonical_row,
                source_index=source_indices[canonical_row],
                alias_source_rows=alias_rows,
                source_indices=tuple(_json_scalar(source_indices[position]) for position in identity_positions),
                row=lion.iloc[canonical_row],
                segment_id=segment_id,
                street_names=street_names,
                source_records=source_records,
                boroughs_by_side=boroughs_by_side,
                address_ranges_by_side=address_ranges_by_side,
                boundary_out_of_scope_by_side=boundary_out_of_scope_by_side,
            ))
        if (
            segment_number % PROGRESS_EVERY_SOURCE_ROWS == 0
            or segment_number == len(sorted_segment_ids)
        ):
            LOGGER.info(
                "LION alias normalization progress segments=%s/%s prepared=%s aliases=%s",
                segment_number,
                len(sorted_segment_ids),
                len(prepared),
                row_outcomes["deduplicated_alias"],
            )
    return prepared, row_outcomes, {
        "segment_alias_groups": alias_groups,
        "multi_geometry_segment_ids": multi_geometry_segment_ids,
        "identity_conflict_groups": identity_conflict_groups,
    }


def _append_preclassified_sides(
    side_results: list[SideResult],
    row: pd.Series,
    source_row: int,
    source_index: object,
    status: str,
    reason: str,
    *,
    details: dict[str, object] | None = None,
) -> None:
    segment_id = _clean_identifier(_first_present(row, "SegmentID", "segment_id"))
    for side, id_field, _ in SIDE_FIELDS:
        side_results.append(SideResult(
            source_row=source_row,
            source_index=source_index,
            side=side,
            segment_id=segment_id,
            block_face_id=_clean_identifier(row.get(id_field)),
            status=status,
            reason=reason,
            details=dict(details or {}),
        ))


def _exact_geometry_key(geometry: BaseGeometry | None, source_row: int) -> str:
    if _line_error(geometry):
        return f"invalid:{source_row}"
    return geometry.wkb_hex


def _curbside_exclusion(row: pd.Series) -> str | None:
    status = _source_code(row.get("Status"))
    if status != "2":
        return f"official Generic segment is not constructed (Status={status or 'blank'})"
    non_pedestrian = _source_code(row.get("NonPed"))
    if non_pedestrian == "V":
        return "official Generic segment is vehicle-only (NonPed=V)"
    return None


def _canonical_alias_position(lion: gpd.GeoDataFrame, positions: list[int]) -> int:
    base_rows = [
        position
        for position in positions
        if not _clean_text(lion.iloc[position].get("SpecAddr"))
    ]
    candidates = base_rows or positions
    return min(
        candidates,
        key=lambda position: (
            _clean_text(_first_present(lion.iloc[position], "Street", "SAFStreetName", "street_name")),
            position,
        ),
    )


def _alias_boroughs(
    lion: gpd.GeoDataFrame,
    positions: list[int],
) -> tuple[dict[str, str | None], list[str]]:
    boroughs_by_side: dict[str, str | None] = {}
    conflicts: list[str] = []
    for side, _, borough_field in SIDE_FIELDS:
        present_values = [
            lion.iloc[position].get(borough_field)
            for position in positions
            if not _is_missing(lion.iloc[position].get(borough_field))
            and _clean_text(lion.iloc[position].get(borough_field))
        ]
        values = {
            borough
            for value in present_values
            if (borough := _borough_name(value)) is not None
        }
        if any(_borough_name(value) is None for value in present_values) or len(values) > 1:
            conflicts.append(borough_field)
            boroughs_by_side[side] = None
        else:
            boroughs_by_side[side] = next(iter(values), None)
    return boroughs_by_side, conflicts


def _alias_address_ranges(
    lion: gpd.GeoDataFrame,
    positions: list[int],
    source_indices: list[object],
) -> dict[str, tuple[dict[str, object], ...]]:
    """Retain every alias row that establishes a side's addressability."""

    ranges_by_side: dict[str, tuple[dict[str, object], ...]] = {}
    for side, _, _ in SIDE_FIELDS:
        ranges: list[dict[str, object]] = []
        for position in positions:
            row = lion.iloc[position]
            if not _usable_address_range(row, side):
                continue
            ranges.append({
                "source_row": position,
                "source_index": _json_scalar(source_indices[position]),
                **_address_range_detail(row, side),
            })
        ranges_by_side[side] = tuple(ranges)
    return ranges_by_side


def _lion_street_names(lion: gpd.GeoDataFrame, positions: list[int]) -> tuple[str, ...]:
    names = {
        cleaned
        for position in positions
        for field_name in ("Street", "SAFStreetName", "street_name")
        if (cleaned := _clean_text(lion.iloc[position].get(field_name)))
    }
    return tuple(sorted(names))


def _lion_source_record(
    row: pd.Series,
    source_row: int,
    source_index: object,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source_row": source_row,
        "source_index": _json_scalar(source_index),
    }
    fields = {
        "object_id": ("OBJECTID", "ObjectID", "objectid"),
        "segment_id": ("SegmentID", "segment_id"),
        "generic_id": ("GenericID",),
        "segment_count": ("SegCount",),
        "segment_type": ("SegmentTyp",),
        "feature_type": ("FeatureTyp",),
        "status": ("Status",),
        "non_pedestrian": ("NonPed",),
        "location_status": ("LocStatus",),
        "borough_boundary": ("BoroBndry",),
        "street_name": ("Street", "street_name"),
        "saf_street_name": ("SAFStreetName",),
        "special_address": ("SpecAddr",),
        "left_block_face_id": ("LBlockFaceID",),
        "right_block_face_id": ("RBlockFaceID",),
        "left_borough": ("LBoro",),
        "right_borough": ("RBoro",),
        "left_low_house_number": ("L_LOW_HN", "LLo_Hyphen"),
        "left_high_house_number": ("L_HIGH_HN", "LHi_Hyphen"),
        "right_low_house_number": ("R_LOW_HN", "RLo_Hyphen"),
        "right_high_house_number": ("R_HIGH_HN", "RHi_Hyphen"),
        "from_left": ("FromLeft",),
        "to_left": ("ToLeft",),
        "from_right": ("FromRight",),
        "to_right": ("ToRight",),
    }
    for output_field, source_fields in fields.items():
        value = _first_present(row, *source_fields)
        if not _is_missing(value) and str(value).strip():
            record[output_field] = _json_scalar(value)
    return record


def _parse_frequency_rows(
    frequencies: gpd.GeoDataFrame,
    frequency_id_field: str,
) -> tuple[
    dict[int, dict[str, tuple[str, ...]]],
    dict[int, list[str]],
    dict[int, str],
]:
    parsed: dict[int, dict[str, tuple[str, ...]]] = {}
    invalid: dict[int, list[str]] = {}
    object_ids: dict[int, str] = {}
    rows_by_object_id: dict[str, list[int]] = defaultdict(list)
    for position, row in frequencies.iterrows():
        errors: list[str] = []
        object_id = _clean_identifier(row.get(frequency_id_field))
        object_ids[int(position)] = object_id
        if not object_id:
            errors.append(f"{frequency_id_field}: missing DSNY source OBJECTID")
        else:
            rows_by_object_id[object_id].append(int(position))
        geometry = row.geometry
        if geometry is None or geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            errors.append("frequency geometry must be a valid non-empty Polygon or MultiPolygon")
        schedules: dict[str, tuple[str, ...]] = {}
        for collection_type, field_name in SCHEDULE_FIELDS.items():
            value = row.get(field_name)
            if _is_missing(value) or (isinstance(value, str) and not value.strip()):
                if collection_type == "REFUSE":
                    errors.append(f"{field_name}: missing explicit schedule")
                schedules[collection_type] = ()
                continue
            try:
                schedules[collection_type] = tuple(normalize_days(value))
            except ValueError as error:
                errors.append(f"{field_name}: {error}")
                schedules[collection_type] = ()
        if errors:
            invalid[int(position)] = errors
        else:
            parsed[int(position)] = schedules
    for object_id, positions in rows_by_object_id.items():
        if len(positions) <= 1:
            continue
        for position in positions:
            invalid.setdefault(position, []).append(
                f"duplicate DSNY source OBJECTID {object_id!r} appears in rows {positions}"
            )
            parsed.pop(position, None)
    return parsed, invalid, object_ids


def _frequency_schedule_empty_rows(frequencies: gpd.GeoDataFrame) -> dict[str, int]:
    return {
        field_name: sum(
            _is_missing(value) or (isinstance(value, str) and not value.strip())
            for value in frequencies[field_name]
        ) if field_name in frequencies.columns else len(frequencies)
        for field_name in SCHEDULE_FIELDS.values()
    }


def _frequency_source_record(
    row: pd.Series,
    frequency_row: int,
    frequency_id_field: str,
) -> dict[str, object]:
    record: dict[str, object] = {
        "frequency_row": frequency_row,
        "object_id": _clean_identifier(row.get(frequency_id_field)),
    }
    fields = {
        "schedule_code": ("SCHEDULECODE", "ScheduleCode", "schedulecode"),
        "section": ("SECTION", "Section", "section"),
        "district": ("DISTRICT", "District", "district"),
    }
    for output_field, source_fields in fields.items():
        value = _first_present(row, *source_fields)
        if not _is_missing(value) and str(value).strip():
            record[output_field] = _json_scalar(value)
    return record


def _matching_frequency_parts(
    frequencies: gpd.GeoDataFrame,
    spatial_index: object | None,
    source_geometry: BaseGeometry,
    side: str,
    side_offset_feet: float,
    tolerance_feet: float,
) -> tuple[dict[int, FrequencyTraceMatch], float, float, str]:
    offset_pairs, used_side_offset_feet, offset_strategy = _side_offset_pairs(
        source_geometry,
        side,
        side_offset_feet,
    )
    trace_length = sum(offset_line.length for _, offset_line in offset_pairs)
    if spatial_index is None:
        return {}, trace_length, used_side_offset_feet, offset_strategy
    direct = _collect_frequency_parts(
        frequencies,
        spatial_index,
        offset_pairs,
        tolerance_feet=0,
    )
    if direct or tolerance_feet == 0:
        return direct, trace_length, used_side_offset_feet, offset_strategy

    # Tolerance is a fallback only when no exact positive-length overlap exists.
    # That recovers small topology gaps without double-classifying normal shared
    # polygon boundaries.
    return _collect_frequency_parts(
        frequencies,
        spatial_index,
        offset_pairs,
        tolerance_feet=tolerance_feet,
    ), trace_length, used_side_offset_feet, offset_strategy


def _side_offset_pairs(
    geometry: BaseGeometry,
    side: str,
    distance_feet: float,
) -> tuple[list[tuple[LineString, LineString]], float, str]:
    if side not in {"LEFT", "RIGHT"}:
        raise ValueError(f"unknown side {side!r}")
    if not math.isfinite(distance_feet) or distance_feet <= 0:
        raise ValueError("side offset must be a positive finite distance")
    error = _line_error(geometry)
    if error:
        raise ValueError(error)
    source_lines = [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)
    source_lines = [_two_dimensional_line(source_line) for source_line in source_lines]
    # An inward parallel curve can legitimately collapse when a short/tightly
    # curved LION segment has a radius smaller than the nominal curb offset.
    # Retry the complete geometry at successively smaller offsets; accepting a
    # partial set of components would silently omit source geometry.
    candidate_distances = [distance_feet]
    while candidate_distances[-1] > 0.25:
        candidate_distances.append(max(0.25, candidate_distances[-1] / 2))
    for candidate_distance in candidate_distances:
        signed_distance = candidate_distance if side == "LEFT" else -candidate_distance
        pairs: list[tuple[LineString, LineString]] = []
        complete = True
        for source_line in source_lines:
            offset_parts = [
                _two_dimensional_line(offset_line)
                for offset_line in _line_parts(source_line.offset_curve(signed_distance))
                if offset_line.length > 0
            ]
            if not offset_parts or not _offset_trace_covers_source(
                source_line,
                offset_parts,
                signed_distance,
            ):
                complete = False
                break
            pairs.extend((source_line, offset_line) for offset_line in offset_parts)
        if complete and pairs:
            return pairs, candidate_distance, "continuous_line_offset"
    # A folded centerline can have no complete continuous parallel curve even at
    # a sub-foot offset.  Do not discard its geometry: offset every original
    # primitive independently at the requested distance.  Each returned pair
    # retains the exact source segment, so schedule clipping/provenance still
    # has to account for all of the source rather than accepting GEOS's partial
    # curve.  This is deliberately explicit in the side audit.
    primitive_pairs = _primitive_offset_pairs(source_lines, signed_distance)
    if primitive_pairs:
        return primitive_pairs, distance_feet, "per_source_segment_offset"
    raise ValueError(
        "line did not produce a complete side-offset trace for every source segment "
        "at or below the requested distance"
    )


def _primitive_offset_pairs(
    source_lines: list[LineString],
    signed_distance: float,
) -> list[tuple[LineString, LineString]]:
    """Offset each non-zero original primitive without losing folded geometry."""

    pairs: list[tuple[LineString, LineString]] = []
    for source_line in source_lines:
        coordinates = list(source_line.coords)
        for start, end in zip(coordinates, coordinates[1:]):
            primitive = LineString([start, end])
            if primitive.length <= 1e-9:
                continue
            parts = [
                _two_dimensional_line(offset_part)
                for offset_part in _line_parts(primitive.offset_curve(signed_distance))
                if offset_part.length > 1e-9
            ]
            if not parts:
                return []
            pairs.extend((primitive, offset_part) for offset_part in parts)
    return pairs


def _offset_trace_covers_source(
    source_line: LineString,
    offset_parts: list[LineString],
    signed_distance: float,
) -> bool:
    """Require complete source-parameter coverage, allowing normal inner joins.

    GEOS can return a valid, non-empty parallel curve after discarding a large
    portion of a tightly folded line. A non-empty check therefore is not a
    completeness check. First require the returned parts' endpoint mappings to
    span the complete source parameter range. Then compare each primitive with
    its analytic parallel, excluding only the distance/angle-derived portion
    legitimately consumed by an inner join. Join trimming is capped per source
    segment so a near reversal cannot erase most of a hairpin and still pass.
    """

    source_segments: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    coordinates = list(source_line.coords)
    for start, end in zip(coordinates, coordinates[1:]):
        normalized_start = (float(start[0]), float(start[1]))
        normalized_end = (float(end[0]), float(end[1]))
        segment_length = math.hypot(
            normalized_end[0] - normalized_start[0],
            normalized_end[1] - normalized_start[1],
        )
        if segment_length > 1e-9:
            source_segments.append((normalized_start, normalized_end, segment_length))
    if not source_segments:
        return False
    # A non-empty offset of one straight primitive cannot omit an interior
    # source segment, so avoid polygon overlay work for the common fast path.
    if len(source_segments) == 1:
        return True

    try:
        tolerance = max(
            OFFSET_COVERAGE_TOLERANCE_FEET,
            abs(signed_distance) * 1e-7,
            source_line.length * 1e-10,
        )
        if not _offset_parts_span_source_parameters(
            source_line,
            offset_parts,
            tolerance,
        ):
            return False

        start_trims = [0.0] * len(source_segments)
        end_trims = [0.0] * len(source_segments)
        for segment_number in range(len(source_segments) - 1):
            _, _, incoming_length = source_segments[segment_number]
            _, _, outgoing_length = source_segments[segment_number + 1]
            incoming_start, incoming_end, _ = source_segments[segment_number]
            outgoing_start, outgoing_end, _ = source_segments[segment_number + 1]
            incoming_x = (incoming_end[0] - incoming_start[0]) / incoming_length
            incoming_y = (incoming_end[1] - incoming_start[1]) / incoming_length
            outgoing_x = (outgoing_end[0] - outgoing_start[0]) / outgoing_length
            outgoing_y = (outgoing_end[1] - outgoing_start[1]) / outgoing_length
            cross = incoming_x * outgoing_y - incoming_y * outgoing_x
            dot = max(
                -1.0,
                min(1.0, incoming_x * outgoing_x + incoming_y * outgoing_y),
            )
            if dot <= -1.0 + 1e-12:
                # A true reversal has no finite parallel-join trim and cannot
                # provide reliable left/right source coverage.
                return False
            if signed_distance * cross <= 0:
                continue
            trim = abs(signed_distance) * abs(cross) / max(1e-12, 1.0 + dot)
            if dot <= NEAR_REVERSAL_COSINE:
                incoming_cap = incoming_length * MAX_INNER_JOIN_TRIM_FRACTION
                outgoing_cap = outgoing_length * MAX_INNER_JOIN_TRIM_FRACTION
                if trim > incoming_cap + tolerance or trim > outgoing_cap + tolerance:
                    return False
            end_trims[segment_number] = max(end_trims[segment_number], trim)
            start_trims[segment_number + 1] = max(
                start_trims[segment_number + 1],
                trim,
            )

        trace = unary_union(offset_parts)
        trace_area = trace.buffer(tolerance)
        for segment_number, (start, end, segment_length) in enumerate(source_segments):
            delta_x = end[0] - start[0]
            delta_y = end[1] - start[1]
            offset_x = -delta_y / segment_length * signed_distance
            offset_y = delta_x / segment_length * signed_distance
            expected = LineString([
                (start[0] + offset_x, start[1] + offset_y),
                (end[0] + offset_x, end[1] + offset_y),
            ])
            core_start = start_trims[segment_number]
            core_end = expected.length - end_trims[segment_number]
            if core_end - core_start <= tolerance:
                return False
            expected_core = substring(expected, core_start, core_end)
            uncovered_length = expected_core.difference(trace_area).length
            if uncovered_length > max(tolerance * 2, expected_core.length * 1e-8):
                return False
        return True
    except (GEOSException, TypeError, ValueError):
        return False


def _offset_parts_span_source_parameters(
    source_line: LineString,
    offset_parts: list[LineString],
    tolerance: float,
) -> bool:
    """Check that split offset parts collectively span the source arclength."""

    intervals: list[tuple[float, float]] = []
    for offset_part in offset_parts:
        coordinates = list(offset_part.coords)
        if len(coordinates) < 2:
            continue
        start = source_line.project(Point(coordinates[0]))
        end = source_line.project(Point(coordinates[-1]))
        low, high = sorted((float(start), float(end)))
        if high - low > tolerance:
            intervals.append((low, high))
    if not intervals:
        return False
    intervals.sort()
    covered_start, covered_end = intervals[0]
    if covered_start > tolerance:
        return False
    for interval_start, interval_end in intervals[1:]:
        if interval_start > covered_end + tolerance:
            return False
        covered_end = max(covered_end, interval_end)
    return covered_end >= source_line.length - tolerance


def _collect_frequency_parts(
    frequencies: gpd.GeoDataFrame,
    spatial_index: object,
    offset_pairs: list[tuple[LineString, LineString]],
    *,
    tolerance_feet: float,
) -> dict[int, FrequencyTraceMatch]:
    trace_parts: dict[int, list[LineString]] = defaultdict(list)
    source_parts: dict[int, list[LineString]] = defaultdict(list)
    for source_line, offset_line in offset_pairs:
        search_geometry = (
            offset_line
            if tolerance_feet == 0
            else offset_line.buffer(tolerance_feet, cap_style="flat")
        )
        positions = spatial_index.query(search_geometry, predicate="intersects")
        for position_value in positions:
            position = int(position_value)
            polygon = frequencies.geometry.iloc[position]
            try:
                target = polygon if tolerance_feet == 0 else polygon.buffer(tolerance_feet)
                intersection = offset_line.intersection(target)
            except (GEOSException, TypeError, AttributeError) as error:
                raise ValueError(
                    f"frequency row {position} could not be intersected with the side trace: {error}"
                ) from error
            for trace_part in _line_parts(intersection):
                if trace_part.length < MIN_MAPPABLE_TRACE_FEET:
                    continue
                mapped_parts = _map_offset_part_to_source(source_line, offset_line, trace_part)
                if not mapped_parts:
                    continue
                trace_parts[position].append(trace_part)
                source_parts[position].extend(mapped_parts)

    matches: dict[int, FrequencyTraceMatch] = {}
    for position in sorted(trace_parts):
        trace_geometry = combine_line_geometries(trace_parts[position])
        source_geometry = combine_line_geometries(source_parts[position])
        matches[position] = FrequencyTraceMatch(
            overlap_feet=float(unary_union(trace_parts[position]).length),
            trace_geometry=trace_geometry,
            source_geometry=source_geometry,
            used_tolerance=tolerance_feet > 0,
        )
    return matches


def _map_offset_part_to_source(
    source_line: LineString,
    offset_line: LineString,
    trace_part: LineString,
) -> list[LineString]:
    start = Point(trace_part.coords[0])
    end = Point(trace_part.coords[-1])
    start_distance = source_line.project(start)
    end_distance = source_line.project(end)
    low, high = sorted((start_distance, end_distance))
    if high - low <= 1e-6 and offset_line.length > 0:
        low, high = sorted((
            offset_line.project(start) / offset_line.length * source_line.length,
            offset_line.project(end) / offset_line.length * source_line.length,
        ))
    if high - low <= 1e-6:
        return []
    return [part for part in _line_parts(substring(source_line, low, high)) if part.length > 1e-6]


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [] if geometry.is_empty else [_two_dimensional_line(geometry)]
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        return [
            line
            for component in geometry.geoms
            for line in _line_parts(component)
        ]
    return []


def _conflicting_frequency_overlaps(
    matches: dict[int, FrequencyTraceMatch],
    schedules: dict[int, dict[str, tuple[str, ...]]],
) -> list[tuple[int, int]]:
    rows = sorted(matches)
    conflicting: list[tuple[int, int]] = []
    for offset, left_row in enumerate(rows):
        left_signature = tuple((kind, schedules[left_row][kind]) for kind in SCHEDULE_FIELDS)
        for right_row in rows[offset + 1:]:
            right_signature = tuple((kind, schedules[right_row][kind]) for kind in SCHEDULE_FIELDS)
            if left_signature == right_signature:
                continue
            try:
                overlap = matches[left_row].trace_geometry.intersection(
                    matches[right_row].trace_geometry
                ).length
            except (GEOSException, TypeError):
                overlap = math.inf
            if overlap > 1e-6:
                conflicting.append((left_row, right_row))
    return conflicting


def _frequency_id_field(columns: Iterable[object]) -> str | None:
    string_columns = [column for column in columns if isinstance(column, str)]
    for candidate in FREQUENCY_ID_FIELD_NAMES:
        if candidate in string_columns:
            return candidate
    return next((column for column in string_columns if column.casefold() == "objectid"), None)


def _split_feature_key(
    origin_block_face_id: str,
    borough: str,
    schedule_signature: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    encoded = json.dumps(
        {
            "borough": borough,
            "schedules": {kind: list(days) for kind, days in schedule_signature},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"{origin_block_face_id}~{digest}"


def _missing_borough_is_out_of_scope(row: pd.Series, side: str) -> bool:
    if _source_code(row.get("SegmentTyp")) != "U":
        return False
    location_status = _source_code(row.get("LocStatus"))
    boundary = _source_code(row.get("BoroBndry"))
    other_borough_field = "RBoro" if side == "LEFT" else "LBoro"
    has_paired_borough = _borough_name(row.get(other_borough_field)) is not None
    return has_paired_borough and (
        location_status in {"3", "4", "9"}
        or boundary not in {"", "0", "N", "NO"}
    )


def _orient_geometry_for_display(
    geometry: BaseGeometry,
    source_side: str,
    display_side: str,
) -> BaseGeometry:
    if source_side == display_side:
        return geometry
    if isinstance(geometry, LineString):
        return LineString(list(geometry.coords)[::-1])
    if isinstance(geometry, MultiLineString):
        return MultiLineString([list(line.coords)[::-1] for line in geometry.geoms])
    raise ValueError(f"unsupported geometry type {geometry.geom_type}")


def _candidate_conflict_values(
    group: list[SideCandidate],
    conflicts: list[str],
) -> dict[str, object]:
    values: dict[str, object] = {}
    for field_name in conflicts:
        if field_name == "schedules":
            schedules = {
                json.dumps(
                    {kind: list(candidate.schedules[kind]) for kind in SCHEDULE_FIELDS},
                    sort_keys=True,
                )
                for candidate in group
            }
            values[field_name] = [json.loads(schedule) for schedule in sorted(schedules)]
        else:
            values[field_name] = sorted({getattr(candidate, field_name) for candidate in group})
    return values


def _build_audit(
    *,
    source_rows: int,
    frequency_rows: int,
    expected_sides: int,
    side_results: list[SideResult],
    output_features: int,
    conflict_groups: int,
    side_offset_feet: float,
    trace_tolerance_feet: float,
    global_errors: list[dict[str, object]],
    valid_frequency_rows: set[int],
    invalid_frequency_rows: set[int],
    encountered_frequency_rows: set[int],
    frequency_object_ids: dict[int, str],
    frequency_id_field: str | None,
    source_row_outcomes: dict[str, int],
    lion_preparation: dict[str, object],
    frequency_schedule_empty_rows: dict[str, int],
    split_feature_groups: int,
    split_output_features: int,
    borough_split_feature_groups: int,
    borough_split_output_features: int,
) -> dict[str, object]:
    outcomes = {key: 0 for key in OUTCOME_KEYS}
    for result in side_results:
        key = "conflicts" if result.status == "conflict" else result.status
        if key not in outcomes:
            raise RuntimeError(f"unclassified source side: {result.audit_record()}")
        outcomes[key] += 1
    classified_sides = sum(outcomes.values())
    side_reconciliation_passed = classified_sides == expected_sides
    used_valid_rows = valid_frequency_rows & encountered_frequency_rows
    unused_valid_rows = valid_frequency_rows - encountered_frequency_rows
    frequency_outcomes = {
        "used_valid": len(used_valid_rows),
        "unused_valid": len(unused_valid_rows),
        "invalid": len(invalid_frequency_rows),
    }
    frequency_reconciliation_passed = sum(frequency_outcomes.values()) == frequency_rows
    classified_source_rows = sum(source_row_outcomes.values())
    source_row_reconciliation_passed = classified_source_rows == source_rows
    reconciliation_passed = (
        side_reconciliation_passed
        and frequency_reconciliation_passed
        and source_row_reconciliation_passed
    )
    fatal_side_count = sum(outcomes[key] for key in FATAL_OUTCOMES)
    fallback_block_face_ids = sum(
        "fallback_block_face_id" in result.details
        for result in side_results
    )
    unused_frequency_records = [
        {
            "record_type": "frequency",
            "outcome": "unused_frequency",
            "frequency_row": frequency_row,
            "dsny_object_id": frequency_object_ids[frequency_row],
            "reason": "valid DSNY frequency row mapped to zero LION sides",
        }
        for frequency_row in sorted(unused_valid_rows)
    ]
    fatal_frequency_count = len(unused_valid_rows) + len(invalid_frequency_rows)
    # Invalid rows are already detailed in global_errors; schema-level errors
    # add one further fatal finding when no per-row parse was possible.
    schema_error_count = sum(
        error.get("kind") in {"missing_frequency_fields", "missing_lion_scope_fields"}
        for error in global_errors
    )
    fatal_count = fatal_side_count + fatal_frequency_count + schema_error_count
    records = [
        result.audit_record()
        for result in side_results
        if (
            result.status != "matched"
            or "fallback_block_face_id" in result.details
            or result.details.get("side_offset_fallback") is True
            or result.details.get("side_offset_segment_fallback") is True
        )
    ]
    records.extend(unused_frequency_records)
    return {
        "audit_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "working_crs": WORKING_CRS,
        "side_offset_feet": side_offset_feet,
        "trace_tolerance_feet": trace_tolerance_feet,
        "required_schedule_fields": list(SCHEDULE_FIELDS.values()),
        "frequency_id_field": frequency_id_field,
        "source_rows": source_rows,
        "raw_source_rows": source_rows,
        "raw_lion_rows": source_rows,
        "source_row_outcomes": source_row_outcomes,
        "classified_source_rows": classified_source_rows,
        "in_scope_source_rows": (
            source_row_outcomes["in_scope"]
            + source_row_outcomes["deduplicated_alias"]
            + source_row_outcomes["curbside_out_of_scope"]
        ),
        "in_scope_lion_rows": (
            source_row_outcomes["in_scope"]
            + source_row_outcomes["deduplicated_alias"]
            + source_row_outcomes["curbside_out_of_scope"]
        ),
        "eligible_lion_rows": (
            source_row_outcomes["in_scope"] + source_row_outcomes["deduplicated_alias"]
        ),
        "processed_segment_rows": source_row_outcomes["in_scope"],
        "deduplicated_alias_rows": source_row_outcomes["deduplicated_alias"],
        "curbside_excluded_lion_rows": source_row_outcomes["curbside_out_of_scope"],
        "out_of_scope_source_rows": source_row_outcomes["out_of_scope"],
        "excluded_lion_rows": source_row_outcomes["out_of_scope"],
        "invalid_source_rows": source_row_outcomes["invalid"],
        "segment_alias_groups": lion_preparation["segment_alias_groups"],
        "multi_geometry_segment_id_count": len(lion_preparation["multi_geometry_segment_ids"]),
        "multi_geometry_segment_ids": lion_preparation["multi_geometry_segment_ids"],
        "identity_conflict_groups": lion_preparation["identity_conflict_groups"],
        "source_row_reconciliation": {
            "expected": source_rows,
            "classified": classified_source_rows,
            "difference": classified_source_rows - source_rows,
            "passed": source_row_reconciliation_passed,
        },
        "frequency_rows": frequency_rows,
        "expected_sides": expected_sides,
        "classified_sides": classified_sides,
        "outcomes": outcomes,
        "matched": outcomes["matched"],
        "unmatched": outcomes["outside_schedule_area"],
        "outside_schedule_area": outcomes["outside_schedule_area"],
        "partially_outside_schedule_area": outcomes["partially_outside_schedule_area"],
        "fallback_block_face_id": fallback_block_face_ids,
        "non_addressable": outcomes["non_addressable"],
        "ambiguous": outcomes["ambiguous"],
        "invalid": outcomes["invalid"],
        "conflicts": outcomes["conflicts"],
        "conflict_groups": conflict_groups,
        "output_features": output_features,
        "reconciliation": {
            "expected": expected_sides,
            "classified": classified_sides,
            "difference": classified_sides - expected_sides,
            "passed": side_reconciliation_passed,
        },
        "frequency_outcomes": frequency_outcomes,
        "frequency_schedule_empty_rows": frequency_schedule_empty_rows,
        "valid_frequency_rows": len(valid_frequency_rows),
        "used_valid_frequency_rows": len(used_valid_rows),
        "unused_valid_frequency_rows": len(unused_valid_rows),
        "invalid_frequency_rows": len(invalid_frequency_rows),
        "frequency_reconciliation": {
            "expected": frequency_rows,
            "classified": sum(frequency_outcomes.values()),
            "difference": sum(frequency_outcomes.values()) - frequency_rows,
            "passed": frequency_reconciliation_passed,
        },
        "reconciled": reconciliation_passed,
        "global_errors": global_errors,
        "fatal_side_count": fatal_side_count,
        "fatal_frequency_count": fatal_frequency_count,
        "fatal_count": fatal_count,
        "split_feature_groups": split_feature_groups,
        "split_output_features": split_output_features,
        "borough_split_feature_groups": borough_split_feature_groups,
        "borough_split_output_features": borough_split_output_features,
        "unused_frequency_records": unused_frequency_records,
        "records": records,
        "passed": reconciliation_passed and fatal_count == 0 and output_features > 0,
    }


def _line_error(geometry: BaseGeometry | None) -> str | None:
    if geometry is None or geometry.is_empty:
        return "missing or empty line geometry"
    if geometry.geom_type not in {"LineString", "MultiLineString"}:
        return f"unsupported LION geometry type {geometry.geom_type}"
    if not geometry.is_valid:
        return "invalid LION line geometry"
    if geometry.length <= 0:
        return "zero-length LION line geometry"
    return None


def _sample_line(geometry: BaseGeometry) -> LineString:
    error = _line_error(geometry)
    if error:
        raise ValueError(error)
    if isinstance(geometry, LineString):
        return geometry
    if isinstance(geometry, MultiLineString):
        return max(geometry.geoms, key=lambda line: line.length)
    raise ValueError(f"unsupported geometry type {geometry.geom_type}")


def _two_dimensional_line(line: LineString) -> LineString:
    return LineString([(float(coordinate[0]), float(coordinate[1])) for coordinate in line.coords])


def _borough_name(value: object) -> str | None:
    if _is_missing(value):
        return None
    try:
        return BOROUGHS.get(int(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _clean_text(value: object) -> str:
    return "" if _is_missing(value) else " ".join(str(value).strip().split())


def _source_code(value: object) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _clean_text(value).upper()


def _first_present(row: pd.Series, *field_names: str) -> object:
    for field_name in field_names:
        value = row.get(field_name)
        if not _is_missing(value) and str(value).strip():
            return value
    return None


def _clean_identifier(value: object) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_non_addressable(value: object) -> bool:
    if _is_missing(value):
        return True
    cleaned = _clean_identifier(value).upper()
    if cleaned in {"", "0", "0.0", "NONE", "NULL", "NAN", "<NA>"}:
        return True
    try:
        return float(cleaned) == 0
    except ValueError:
        return False


def _usable_address_range(row: pd.Series, side: str) -> bool:
    return any(
        _usable_address_value(row.get(field_name))
        for field_name in SIDE_ADDRESS_FIELDS[side]
    )


def _usable_address_value(value: object) -> bool:
    if _is_missing(value):
        return False
    cleaned = _clean_text(value).upper()
    if cleaned in {"", "0", "NONE", "NULL", "NAN", "<NA>"}:
        return False
    digits = re.sub(r"\D", "", cleaned)
    return bool(digits) and any(digit != "0" for digit in digits)


def _address_range_detail(row: pd.Series, side: str) -> dict[str, object]:
    return {
        field_name: _json_scalar(row.get(field_name))
        for field_name in SIDE_ADDRESS_FIELDS[side]
        if not _is_missing(row.get(field_name)) and _clean_text(row.get(field_name))
    }


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if missing is pd.NA:
        return True
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _json_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if isinstance(converted, (str, int, float, bool)) or converted is None:
            return converted
    return str(value)


def serialize_processed_payload(payload: dict[str, object]) -> bytes:
    """Serialize the promoted artifact canonically so its digest is stable."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def bind_processed_sha256(
    audit: dict[str, object],
    processed_bytes: bytes,
) -> str:
    """Bind an audit to the exact bytes that will be loaded and promoted."""

    digest = hashlib.sha256(processed_bytes).hexdigest()
    audit["processed_sha256"] = digest
    audit["processed_feature_count"] = audit["output_features"]
    return digest


def _atomic_write_many(files: list[tuple[Path, bytes]]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                staged.append((Path(temporary.name), destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lion", type=Path, required=True, help="LION GeoJSON or File Geodatabase")
    parser.add_argument("--lion-layer", default="lion", help="Layer name when --lion points to a File Geodatabase")
    parser.add_argument("--frequencies", type=Path, required=True, help="DSNY frequency polygons GeoJSON")
    parser.add_argument("--output", type=Path, default=Path("data/processed/pilot.geojson"))
    parser.add_argument("--audit", type=Path, default=Path("output/pilot_audit.json"), help="Structured ingestion audit JSON")
    parser.add_argument("--failures", type=Path, default=Path("output/pilot_failures.jsonl"), help="Legacy JSONL copy of non-success side records")
    parser.add_argument("--side-offset-feet", type=float, default=DEFAULT_SIDE_OFFSET_FEET)
    parser.add_argument(
        "--trace-tolerance-feet",
        type=float,
        default=DEFAULT_TRACE_TOLERANCE_FEET,
        help="Maximum fallback tolerance when exact side-trace overlay finds no polygon",
    )
    parser.add_argument(
        "--allow-audit-failures",
        action="store_true",
        help="Write diagnostic output and exit successfully even when the audit fails",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional pilot limit; omit for all LION features")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    LOGGER.info("Reading LION source path=%s layer=%s", args.lion, args.lion_layer)
    lion = gpd.read_file(args.lion, layer=args.lion_layer if args.lion.suffix.lower() == ".gdb" or args.lion.is_dir() else None)
    LOGGER.info("Read LION source rows=%s columns=%s", len(lion), len(lion.columns))
    LOGGER.info("Reading DSNY frequency polygons path=%s", args.frequencies)
    frequencies = gpd.read_file(args.frequencies)
    LOGGER.info("Read DSNY frequency polygons rows=%s", len(frequencies))
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("limit must be positive")
        lion = lion.head(args.limit).copy()
    if lion.empty:
        raise ValueError("LION input contains no rows")

    LOGGER.info("Starting complete side-aware LION-to-DSNY audit")
    payload, audit = build_collection_features(
        lion,
        frequencies,
        side_offset_feet=args.side_offset_feet,
        trace_tolerance_feet=args.trace_tolerance_feet,
    )
    LOGGER.info("Serializing audited GeoJSON features=%s", len(payload["features"]))
    processed_bytes = serialize_processed_payload(payload)
    bind_processed_sha256(audit, processed_bytes)
    audit_bytes = (
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    failure_bytes = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for record in audit["records"]
    ).encode("utf-8")
    LOGGER.info("Writing audited artifacts atomically")
    _atomic_write_many([
        (args.output, processed_bytes),
        (args.audit, audit_bytes),
        (args.failures, failure_bytes),
    ])

    log = LOGGER.info if audit["passed"] else LOGGER.error
    log(
        "Wrote features=%s classified_sides=%s expected_sides=%s fatal=%s passed=%s output=%s audit=%s",
        len(payload["features"]),
        audit["classified_sides"],
        audit["expected_sides"],
        audit["fatal_count"],
        audit["passed"],
        args.output,
        args.audit,
    )
    if not audit["passed"] and not args.allow_audit_failures:
        raise RuntimeError("ingestion audit failed; inspect the structured audit")


if __name__ == "__main__":
    main()
