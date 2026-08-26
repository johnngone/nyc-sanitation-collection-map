import hashlib
import sqlite3
from pathlib import Path

import geopandas as gpd
import pytest
from shapely import wkt
from shapely.geometry import LineString, MultiLineString, box, mapping

from scripts.build_pilot import (
    LION_LOAD_FIELDS,
    _collect_frequency_parts,
    _map_offset_part_to_source,
    _read_lion_source,
    _side_offset_pairs,
    bind_processed_sha256,
    build_collection_features,
    serialize_processed_payload,
)
from scripts.load_processed import load_payload, prepare_features


X = 1_000_000.0
Y = 200_000.0


def lion_frame(rows: list[dict[str, object]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, crs="EPSG:2263")


def lion_row(
    *,
    y: float = Y,
    segment_id: str = "segment-1",
    left_id: object = "left-1",
    right_id: object = "right-1",
    street: str = "TEST STREET",
    left_borough: object = 1,
    right_borough: object = 1,
    segment_type: object = "B",
    feature_type: object = "0",
    status: object = "2",
    non_pedestrian: object = "",
    left_from: object = 0,
    left_to: object = 0,
    right_from: object = 0,
    right_to: object = 0,
    geometry: LineString | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "Street": street,
        "SegmentID": segment_id,
        "LBlockFaceID": left_id,
        "RBlockFaceID": right_id,
        "LBoro": left_borough,
        "RBoro": right_borough,
        "SegmentTyp": segment_type,
        "FeatureTyp": feature_type,
        "Status": status,
        "NonPed": non_pedestrian,
        "FromLeft": left_from,
        "ToLeft": left_to,
        "FromRight": right_from,
        "ToRight": right_to,
        "SpecAddr": None,
        "geometry": geometry if geometry is not None else LineString([(X, y), (X + 100, y)]),
        **extra,
    }


def frequency_frame(rows: list[dict[str, object]]) -> gpd.GeoDataFrame:
    defaults = {
        "FREQ_REFUSE": "Mon, Thu",
        "FREQ_RECYCLING": "Tue",
        "FREQ_ORGANICS": "Wed",
        "FREQ_BULK": "Fri",
    }
    return gpd.GeoDataFrame(
        [{"OBJECTID": index, **defaults, **row} for index, row in enumerate(rows, start=1)],
        crs="EPSG:2263",
    )


def schedules(refuse: list[str] | None = None) -> dict[str, list[str]]:
    return {
        "REFUSE": refuse or ["MON"],
        "RECYCLING": ["TUE"],
        "ORGANICS": [],
        "BULK": [],
    }


def geojson_feature(
    block_face_id: str,
    segment_id: str,
    coordinates: list[tuple[float, float]],
    *,
    feature_schedules: dict[str, list[str]] | None = None,
    street_name: str = "TEST STREET",
    dsny_object_ids: list[str] | None = None,
) -> dict[str, object]:
    normalized_schedules = feature_schedules or schedules()
    return {
        "type": "Feature",
        "geometry": mapping(LineString(coordinates)),
        "properties": {
            "block_face_id": block_face_id,
            "feature_key": block_face_id,
            "origin_block_face_id": block_face_id,
            "segment_id": segment_id,
            "segment_ids": [segment_id],
            "street_name": street_name,
            "street_names": [street_name],
            "borough": "MANHATTAN",
            "side": "LEFT",
            "refuse_days": normalized_schedules["REFUSE"],
            "schedules": normalized_schedules,
            "schedule_states": {
                collection_type: {
                    "state": "SOURCE_EXPLICIT" if days else "UNKNOWN_SOURCE_BLANK",
                    "source_field": f"FREQ_{collection_type}",
                    "raw_value": ",".join(days) if days else None,
                    "rule_id": None,
                    "source_policy_conflict": False,
                    "provenance": "DSNY test fixture",
                }
                for collection_type, days in normalized_schedules.items()
            },
            "dsny_object_ids": dsny_object_ids or ["101"],
            "dsny_sources": [
                {"frequency_row": 0, "object_id": object_id}
                for object_id in (dsny_object_ids or ["101"])
            ],
            "lion_components": [{
                "segment_id": segment_id,
                "source_side": "LEFT",
                "source_rows": [0],
                "source_indices": [0],
                "street_names": [street_name],
                "source_records": [{"source_row": 0, "source_index": 0, "segment_id": segment_id}],
                "dsny_object_ids": dsny_object_ids or ["101"],
            }],
            "source": "DSNY Frequencies",
            "retrieved_at": "2026-08-19",
        },
    }


@pytest.mark.parametrize("revision", [None, 2, 4])
def test_loader_rejects_noncurrent_processed_schema(revision) -> None:
    payload = {
        "type": "FeatureCollection",
        "schema_revision": revision,
        "features": [
            geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)])
        ],
    }

    with pytest.raises(ValueError, match="schema_revision must be 3"):
        prepare_features(payload)


def test_lion_source_read_projects_used_fields_and_applies_limit(monkeypatch) -> None:
    expected = lion_frame([lion_row()])
    observed: dict[str, object] = {}

    def fake_read_file(path: Path, **options: object) -> gpd.GeoDataFrame:
        observed["path"] = path
        observed.update(options)
        return expected

    monkeypatch.setattr(gpd, "read_file", fake_read_file)

    source = Path("official-lion.gdb")
    result = _read_lion_source(source, "lion", limit=25)

    assert result is expected
    assert observed == {
        "path": source,
        "columns": list(LION_LOAD_FIELDS),
        "layer": "lion",
        "rows": 25,
    }


def test_working_crs_inputs_are_not_reprojected(monkeypatch) -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([{
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    def unexpected_reprojection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("EPSG:2263 input must not be reprojected")

    monkeypatch.setattr(gpd.GeoDataFrame, "to_crs", unexpected_reprojection)

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["passed"] is True


def test_non_default_lion_index_is_preserved_in_provenance() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    lion.index = ["official-row-42"]
    frequencies = frequency_frame([{
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    component = payload["features"][0]["properties"]["lion_components"][0]
    assert component["source_indices"] == ["official-row-42"]
    assert component["source_records"][0]["source_index"] == "official-row-42"
    assert any(
        record["source_index"] == "official-row-42"
        for record in audit["records"]
        if record.get("segment_id") == "segment-1"
    )


def test_side_offset_join_resolves_left_and_right_independently() -> None:
    lion = lion_frame([lion_row()])
    frequencies = frequency_frame([
        {
            "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
            "FREQ_REFUSE": "Mon, Thu",
        },
        {
            "geometry": box(X - 10, Y - 100, X + 110, Y - 1),
            "FREQ_REFUSE": "Tue, Fri",
        },
    ])

    payload, audit = build_collection_features(
        lion,
        frequencies,
        side_offset_feet=25,
        retrieved_at="2026-08-19",
    )

    features = {feature["properties"]["side"]: feature for feature in payload["features"]}
    assert features["LEFT"]["properties"]["refuse_days"] == ["MON", "THU"]
    assert features["RIGHT"]["properties"]["refuse_days"] == ["TUE", "FRI"]
    assert features["LEFT"]["properties"]["dsny_object_ids"] == ["1"]
    assert features["RIGHT"]["properties"]["dsny_object_ids"] == ["2"]
    assert audit["working_crs"] == "EPSG:2263"
    assert audit["outcomes"]["matched"] == 2
    assert audit["classified_sides"] == audit["source_rows"] * 2 == 2
    assert audit["reconciliation"]["passed"] is True
    assert audit["passed"] is True


def test_full_side_trace_splits_line_spanning_adjacent_frequency_schedules() -> None:
    lion = lion_frame([{
        **lion_row(right_id=0),
        "geometry": LineString([(X, Y), (X + 200, Y)]),
    }])
    frequencies = frequency_frame([
        {
            "OBJECTID": 501,
            "geometry": box(X - 10, Y + 1, X + 100, Y + 100),
            "FREQ_REFUSE": "Mon",
        },
        {
            "OBJECTID": 502,
            "geometry": box(X + 100, Y + 1, X + 210, Y + 100),
            "FREQ_REFUSE": "Thu",
        },
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 2
    properties = [feature["properties"] for feature in payload["features"]]
    assert {tuple(item["refuse_days"]) for item in properties} == {("MON",), ("THU",)}
    assert {item["origin_block_face_id"] for item in properties} == {"left-1"}
    assert len({item["block_face_id"] for item in properties}) == 2
    assert {tuple(item["dsny_object_ids"]) for item in properties} == {("501",), ("502",)}
    assert audit["outcomes"]["matched"] == 1
    assert audit["outcomes"]["non_addressable"] == 1
    assert audit["used_valid_frequency_rows"] == 2
    assert audit["unused_valid_frequency_rows"] == 0
    assert audit["split_feature_groups"] == 1
    assert audit["split_output_features"] == 2
    assert audit["passed"] is True


def test_full_side_trace_merges_identical_schedules_and_provenance() -> None:
    lion = lion_frame([{
        **lion_row(right_id=0),
        "geometry": LineString([(X, Y), (X + 200, Y)]),
    }])
    frequencies = frequency_frame([
        {"OBJECTID": 601, "geometry": box(X - 10, Y + 1, X + 100, Y + 100)},
        {"OBJECTID": 602, "geometry": box(X + 100, Y + 1, X + 210, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"]["dsny_object_ids"] == ["601", "602"]
    assert [source["object_id"] for source in payload["features"][0]["properties"]["dsny_sources"]] == ["601", "602"]
    assert audit["outcomes"]["matched"] == 1
    assert audit["used_valid_frequency_rows"] == 2
    assert audit["passed"] is True


def test_partially_covered_side_emits_only_covered_geometry_and_audits_remainder() -> None:
    lion = lion_frame([
        lion_row(
            right_id=0,
            geometry=LineString([(X, Y), (X + 200, Y)]),
        ),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 603, "geometry": box(X - 10, Y + 1, X + 100, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert payload["features"][0]["geometry"]["type"] == "LineString"
    assert audit["outcomes"]["matched"] == 0
    assert audit["outcomes"]["partially_outside_schedule_area"] == 1
    record = next(
        record
        for record in audit["records"]
        if record["outcome"] == "partially_outside_schedule_area"
    )
    assert record["covered_trace_feet"] == pytest.approx(100)
    assert record["uncovered_trace_feet"] == pytest.approx(100)
    assert record["coverage_ratio"] == pytest.approx(0.5)
    assert audit["passed"] is True


def test_blank_bfid_with_address_range_remains_unknown_without_corroboration() -> None:
    lion = lion_frame([
        lion_row(left_id="", right_id=0, left_from=2, left_to=100),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 604, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    candidate = next(
        feature for feature in payload["unknown_features"]
        if feature["properties"]["technical_identity"] == "LION:segment-1:LEFT"
    )
    assert candidate["properties"]["reason_code"] == "INSUFFICIENT_ADDRESS_EVIDENCE"
    assert audit["fallback_block_face_id"] == 1
    assert audit["outcomes"]["matched"] == 0
    fallback_record = next(
        record for record in audit["records"] if "fallback_block_face_id" in record
    )
    assert fallback_record["raw_block_face_id"] == ""
    assert fallback_record["address_range"]["FromLeft"] == 2
    assert audit["passed"] is False


def test_unused_valid_frequency_row_is_detailed_and_fatal() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([
        {"OBJECTID": 701, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
        {"OBJECTID": 702, "geometry": box(X + 10_000, Y + 1, X + 10_100, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["frequency_outcomes"] == {"used_valid": 1, "unused_valid": 1, "invalid": 0}
    assert audit["frequency_reconciliation"]["passed"] is True
    assert audit["reconciled"] is True
    assert audit["fatal_frequency_count"] == 1
    assert audit["passed"] is False
    assert audit["unused_frequency_records"] == [{
        "record_type": "frequency",
        "outcome": "unused_frequency",
        "frequency_row": 1,
        "dsny_object_id": "702",
        "reason": "valid DSNY frequency row mapped to zero LION sides",
    }]
    assert audit["records"][-1] == audit["unused_frequency_records"][0]


def test_all_four_official_schedule_fields_are_required() -> None:
    lion = lion_frame([lion_row()])
    frequencies = frequency_frame([
        {"geometry": box(X - 10, Y - 100, X + 110, Y + 100)},
    ]).drop(columns=["FREQ_BULK"])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    assert audit["expected_sides"] == audit["classified_sides"] == 2
    assert audit["outcomes"]["invalid"] == 2
    assert audit["global_errors"][0]["kind"] == "missing_frequency_fields"
    assert audit["global_errors"][0]["fields"] == ["FREQ_BULK"]
    assert audit["passed"] is False


def test_dsny_objectid_field_is_required_and_frequency_rows_still_reconcile() -> None:
    lion = lion_frame([lion_row()])
    frequencies = frequency_frame([
        {"geometry": box(X - 10, Y - 100, X + 110, Y + 100)},
    ]).drop(columns=["OBJECTID"])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    assert audit["frequency_id_field"] is None
    assert audit["frequency_outcomes"] == {"used_valid": 0, "unused_valid": 0, "invalid": 1}
    assert audit["frequency_reconciliation"]["passed"] is True
    assert audit["reconciled"] is True
    assert audit["passed"] is False


def test_trace_tolerance_recovers_a_subfoot_topology_gap() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([
        {"OBJECTID": 801, "geometry": box(X - 10, Y + 25.5, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies, trace_tolerance_feet=1.0)

    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"]["dsny_object_ids"] == ["801"]
    assert audit["trace_tolerance_feet"] == 1.0
    assert audit["passed"] is True


def test_audit_classifies_ambiguous_and_unmatched_sides() -> None:
    lion = lion_frame([lion_row()])
    overlapping_left = box(X - 10, Y + 1, X + 110, Y + 60)
    frequencies = frequency_frame([
        {"geometry": overlapping_left},
        {"geometry": overlapping_left, "FREQ_REFUSE": "Tue"},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    assert audit["outcomes"]["ambiguous"] == 1
    assert audit["outcomes"]["outside_schedule_area"] == 1
    assert audit["classified_sides"] == audit["expected_sides"] == 2
    assert {record["outcome"] for record in audit["records"]} == {"ambiguous", "outside_schedule_area"}
    assert audit["passed"] is False


def test_non_addressable_side_is_detailed_but_not_fatal() -> None:
    lion = lion_frame([lion_row(left_id=0)])
    frequencies = frequency_frame([
        {"geometry": box(X - 10, Y - 100, X + 110, Y - 1)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["outcomes"]["matched"] == 1
    assert audit["outcomes"]["non_addressable"] == 1
    assert audit["fallback_block_face_id"] == 0
    assert audit["fatal_count"] == 0
    assert audit["records"][0]["outcome"] == "non_addressable"
    assert audit["passed"] is True


def test_unknown_schedule_token_marks_matching_side_invalid() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([
        {
            "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
            "FREQ_REFUSE": "Mon, Funday",
        },
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    assert audit["outcomes"]["invalid"] == 1
    assert audit["outcomes"]["non_addressable"] == 1
    assert audit["global_errors"][0]["kind"] == "invalid_frequency_row"
    assert "FUNDAY" in str(audit["global_errors"][0])
    assert audit["passed"] is False


def test_repeated_block_face_aggregates_all_geometries_and_segment_ids() -> None:
    lion = lion_frame([
        lion_row(y=Y, segment_id="segment-1", left_id="shared", right_id=0),
        lion_row(y=Y + 50, segment_id="segment-2", left_id="shared", right_id=0),
    ])
    frequencies = frequency_frame([
        {"geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["geometry"]["type"] == "MultiLineString"
    assert len(feature["geometry"]["coordinates"]) == 2
    assert feature["properties"]["segment_ids"] == ["segment-1", "segment-2"]
    assert audit["outcomes"]["matched"] == 2
    assert audit["outcomes"]["non_addressable"] == 2
    assert audit["passed"] is True


@pytest.mark.parametrize("variation", ["street_alias", "schedules"])
def test_repeated_block_face_variations_preserve_names_or_split_schedules(variation: str) -> None:
    second_street = "OTHER STREET" if variation == "street_alias" else "TEST STREET"
    lion = lion_frame([
        lion_row(y=Y, segment_id="segment-1", left_id="shared", right_id=0),
        lion_row(y=Y + 100, segment_id="segment-2", left_id="shared", right_id=0, street=second_street),
    ])
    frequencies = frequency_frame([
        {"geometry": box(X - 10, Y + 1, X + 110, Y + 49), "FREQ_REFUSE": "Mon"},
        {
            "geometry": box(X - 10, Y + 101, X + 110, Y + 149),
            "FREQ_REFUSE": "Tue" if variation == "schedules" else "Mon",
        },
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == (2 if variation == "schedules" else 1)
    if variation == "street_alias":
        assert payload["features"][0]["properties"]["street_names"] == ["OTHER STREET", "TEST STREET"]
    else:
        assert {tuple(feature["properties"]["refuse_days"]) for feature in payload["features"]} == {
            ("MON",),
            ("TUE",),
        }
    assert audit["outcomes"]["matched"] == 2
    assert audit["outcomes"]["conflicts"] == 0
    assert audit["outcomes"]["non_addressable"] == 2
    assert audit["conflict_groups"] == 0
    assert audit["split_feature_groups"] == (1 if variation == "schedules" else 0)
    assert audit["classified_sides"] == audit["source_rows"] * 2 == 4
    assert audit["passed"] is True


def test_repeated_block_face_crossing_boroughs_is_split_without_dropping_components() -> None:
    lion = lion_frame([
        lion_row(
            y=Y,
            segment_id="manhattan-segment",
            left_id="shared-boundary-face",
            right_id=0,
            left_borough=1,
        ),
        lion_row(
            y=Y + 100,
            segment_id="queens-segment",
            left_id="shared-boundary-face",
            right_id=0,
            left_borough=4,
        ),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 801, "geometry": box(X - 10, Y + 1, X + 110, Y + 49)},
        {"OBJECTID": 802, "geometry": box(X - 10, Y + 101, X + 110, Y + 149)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 2
    properties = [feature["properties"] for feature in payload["features"]]
    assert {item["borough"] for item in properties} == {"MANHATTAN", "QUEENS"}
    assert {item["origin_block_face_id"] for item in properties} == {"shared-boundary-face"}
    assert len({item["block_face_id"] for item in properties}) == 2
    assert {tuple(item["segment_ids"]) for item in properties} == {
        ("manhattan-segment",),
        ("queens-segment",),
    }
    assert audit["outcomes"]["matched"] == 2
    assert audit["outcomes"]["conflicts"] == 0
    assert audit["conflict_groups"] == 0
    assert audit["borough_split_feature_groups"] == 1
    assert audit["borough_split_output_features"] == 2
    assert audit["passed"] is True


def test_collapsed_nominal_offset_uses_audited_smaller_complete_trace() -> None:
    curved = LineString([
        (X, Y),
        (X + 10, Y + 10),
        (X + 20, Y),
    ])
    lion = lion_frame([
        lion_row(
            segment_id="tight-curve",
            left_id=0,
            right_id="inside-face",
            geometry=curved,
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 803,
        "geometry": box(X - 100, Y - 100, X + 200, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies, side_offset_feet=25)

    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"]["block_face_id"] == "inside-face"
    fallback_record = next(
        record
        for record in audit["records"]
        if record.get("block_face_id") == "inside-face"
    )
    assert fallback_record["side_offset_fallback"] is True
    assert fallback_record["requested_side_offset_feet"] == 25
    assert 0 < fallback_record["used_side_offset_feet"] < 25
    assert fallback_record["outcome"] == "matched"
    assert audit["outcomes"]["invalid"] == 0
    assert audit["passed"] is True


@pytest.mark.parametrize("leg_length", [75.0, 50.0, 30.0])
def test_short_normal_inner_corner_preserves_requested_offset_and_full_source_mapping(
    leg_length: float,
) -> None:
    corner = LineString([
        (X, Y),
        (X + leg_length, Y),
        (X + leg_length, Y + leg_length),
    ])

    pairs, used_distance, strategy = _side_offset_pairs(corner, "LEFT", 25)
    mapped_parts = [
        mapped_part
        for source_line, offset_line in pairs
        for mapped_part in _map_offset_part_to_source(source_line, offset_line, offset_line)
    ]

    assert used_distance == 25
    assert strategy == "continuous_line_offset"
    assert sum(offset_line.length for _, offset_line in pairs) == pytest.approx(
        corner.length - 50,
    )
    assert sum(mapped_part.length for mapped_part in mapped_parts) == pytest.approx(corner.length)


@pytest.mark.parametrize("include_complete_component", [False, True])
def test_nonempty_but_truncated_offset_preserves_every_source_segment(
    include_complete_component: bool,
) -> None:
    folded = LineString([
        (X, Y),
        (X + 100, Y),
        (X + 1, Y + 1),
        (X + 101, Y + 1),
    ])
    geometry = (
        MultiLineString([
            LineString([(X, Y - 50), (X + 100, Y - 50)]),
            folded,
        ])
        if include_complete_component
        else folded
    )
    lion = lion_frame([
        lion_row(
            segment_id="truncated-offset",
            left_id="truncated-face",
            right_id=0,
            geometry=geometry,
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 804,
        "geometry": box(X - 200, Y - 200, X + 300, Y + 200),
    }])

    payload, audit = build_collection_features(lion, frequencies, side_offset_feet=25)

    assert len(payload["features"]) == 1
    fallback_record = next(
        record
        for record in audit["records"]
        if record.get("block_face_id") == "truncated-face"
    )
    assert fallback_record["outcome"] == "matched"
    assert fallback_record["side_offset_segment_fallback"] is True
    assert fallback_record["side_offset_strategy"] == "per_source_segment_offset"
    assert audit["outcomes"]["invalid"] == 0
    assert audit["passed"] is True


def test_official_lion_scope_and_raw_row_reconciliation_are_explicit() -> None:
    lion = lion_frame([
        lion_row(right_id=0),
        lion_row(segment_id="rail-1", left_id="rail-left", right_id="rail-right", segment_type="R"),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 901, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["source_rows"] == audit["raw_lion_rows"] == 2
    assert audit["in_scope_lion_rows"] == 1
    assert audit["excluded_lion_rows"] == 1
    assert audit["source_row_outcomes"] == {
        "in_scope": 1,
        "out_of_scope": 1,
        "curbside_out_of_scope": 0,
        "deduplicated_alias": 0,
        "invalid": 0,
    }
    assert audit["source_row_reconciliation"]["passed"] is True
    assert audit["classified_sides"] == audit["raw_lion_rows"] * 2 == 4
    assert audit["outcomes"]["out_of_scope"] == 2
    assert audit["passed"] is True


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"status": "5"}, "not constructed"),
        ({"non_pedestrian": "V"}, "vehicle-only"),
    ],
)
def test_official_generic_but_non_curbside_rows_are_explicitly_excluded(
    overrides: dict[str, object],
    reason_fragment: str,
) -> None:
    lion = lion_frame([
        lion_row(segment_id="excluded", **overrides),
        lion_row(segment_id="normal", left_id="normal-left", right_id=0, y=Y + 100),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 907, "geometry": box(X - 10, Y + 101, X + 110, Y + 200)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["in_scope_lion_rows"] == 2
    assert audit["eligible_lion_rows"] == 1
    assert audit["curbside_excluded_lion_rows"] == 1
    excluded_records = [
        record for record in audit["records"] if record.get("segment_id") == "excluded"
    ]
    assert len(excluded_records) == 2
    assert all(reason_fragment in record["reason"] for record in excluded_records)
    assert audit["passed"] is True


def test_exact_special_address_aliases_are_sampled_once_with_full_provenance() -> None:
    geometry = LineString([(X, Y), (X + 100, Y)])
    lion = lion_frame([
        lion_row(right_id=0, geometry=geometry, OBJECTID=1001, GenericID=77),
        lion_row(
            right_id=0,
            geometry=geometry,
            street="TEST STREET",
            SpecAddr="N",
            SAFStreetName="ALIAS STREET",
            OBJECTID=1002,
            GenericID=77,
        ),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 902, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    properties = payload["features"][0]["properties"]
    assert properties["street_name"] == "TEST STREET"
    assert properties["street_names"] == ["ALIAS STREET", "TEST STREET"]
    assert properties["lion_components"][0]["source_rows"] == [0, 1]
    assert [record["object_id"] for record in properties["lion_components"][0]["source_records"]] == [1001, 1002]
    assert audit["in_scope_lion_rows"] == 2
    assert audit["processed_segment_rows"] == 1
    assert audit["deduplicated_alias_rows"] == 1
    assert audit["segment_alias_groups"] == 1
    assert audit["outcomes"]["deduplicated_alias"] == 2
    assert audit["passed"] is True


def test_alias_address_range_stays_unknown_and_loader_preserves_it(tmp_path) -> None:
    geometry = LineString([(X, Y), (X + 100, Y)])
    lion = lion_frame([
        lion_row(
            segment_id="alias-address",
            left_id=0,
            right_id=0,
            geometry=geometry,
            OBJECTID=1003,
        ),
        lion_row(
            segment_id="alias-address",
            left_id=0,
            right_id=0,
            geometry=geometry,
            left_from=101,
            left_to=199,
            SpecAddr="A",
            SAFStreetName="ALIAS ADDRESS",
            OBJECTID=1004,
        ),
        lion_row(
            segment_id="normal",
            left_id="normal-left",
            right_id=0,
            y=Y + 100,
            OBJECTID=1005,
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 908,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 200),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    assert {feature["properties"]["block_face_id"] for feature in payload["features"]} == {"normal-left"}
    assert any(
        feature["properties"]["technical_identity"] == "LION:alias-address:LEFT"
        for feature in payload["unknown_features"]
    )
    fallback = next(
        record
        for record in audit["records"]
        if record.get("fallback_block_face_id") == "LION:alias-address:LEFT"
    )
    assert fallback["address_range"]["source_row"] == 1
    assert fallback["address_range"]["FromLeft"] == 101
    assert fallback["address_ranges"] == [fallback["address_range"]]
    assert audit["passed"] is True
    database = tmp_path / "unknowns.sqlite3"
    load_payload(payload, database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM unknown_block_faces WHERE technical_identity = ?",
            ("LION:alias-address:LEFT",),
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT COUNT(*) FROM collection_schedules
               WHERE block_face_id LIKE 'UNKNOWN:%'"""
        ).fetchone()[0] == 0


def test_alias_name_is_used_when_canonical_base_street_is_blank() -> None:
    geometry = LineString([(X, Y), (X + 100, Y)])
    lion = lion_frame([
        lion_row(street="", right_id=0, geometry=geometry, OBJECTID=1006),
        lion_row(
            street="",
            right_id=0,
            geometry=geometry,
            SpecAddr="N",
            SAFStreetName="NAMED PLACE",
            OBJECTID=1007,
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 909,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"][0]["properties"]["street_name"] == "NAMED PLACE"
    assert audit["passed"] is True


def test_invalid_alias_borough_is_not_ignored_in_favor_of_valid_alias() -> None:
    geometry = LineString([(X, Y), (X + 100, Y)])
    lion = lion_frame([
        lion_row(right_id=0, geometry=geometry, left_borough=1),
        lion_row(
            right_id=0,
            geometry=geometry,
            left_borough=9,
            SpecAddr="N",
            SAFStreetName="ALIAS",
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 910,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    assert payload["features"] == []
    assert audit["identity_conflict_groups"] == 1
    assert audit["outcomes"]["invalid"] == 4
    assert audit["passed"] is False


def test_same_segment_id_with_distinct_geometries_is_not_collapsed() -> None:
    lion = lion_frame([
        lion_row(segment_id="shared-segment", left_id="shared-face", right_id=0, y=Y),
        lion_row(segment_id="shared-segment", left_id="shared-face", right_id=0, y=Y + 50),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 903, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert payload["features"][0]["geometry"]["type"] == "MultiLineString"
    assert len(payload["features"][0]["properties"]["lion_components"]) == 2
    assert audit["processed_segment_rows"] == 2
    assert audit["deduplicated_alias_rows"] == 0
    assert audit["multi_geometry_segment_id_count"] == 1
    assert audit["passed"] is True


def test_mixed_source_sides_are_oriented_to_one_display_side_and_preserved() -> None:
    lion = lion_frame([
        lion_row(segment_id="segment-a", left_id="shared", right_id=0, y=Y),
        lion_row(
            segment_id="segment-b",
            left_id=0,
            right_id="shared",
            geometry=LineString([(X + 100, Y + 50), (X, Y + 50)]),
        ),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 904, "geometry": box(X - 10, Y + 1, X + 110, Y + 100)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["properties"]["side"] == "LEFT"
    assert {component["source_side"] for component in feature["properties"]["lion_components"]} == {
        "LEFT",
        "RIGHT",
    }
    assert all(line[0][0] < line[-1][0] for line in feature["geometry"]["coordinates"])
    assert audit["outcomes"]["conflicts"] == 0
    assert audit["passed"] is True


def test_boundary_side_with_justified_null_borough_is_out_of_scope_not_invalid() -> None:
    lion = lion_frame([
        lion_row(
            segment_id="boundary",
            left_borough=None,
            right_id=0,
            segment_type="U",
            LocStatus=3,
            BoroBndry=1,
        ),
        lion_row(segment_id="normal", left_id="normal-left", right_id=0, y=Y + 100),
    ])
    frequencies = frequency_frame([
        {"OBJECTID": 905, "geometry": box(X - 10, Y + 101, X + 110, Y + 200)},
    ])

    payload, audit = build_collection_features(lion, frequencies)

    assert len(payload["features"]) == 1
    assert audit["outcomes"]["out_of_scope"] == 1
    assert audit["outcomes"]["invalid"] == 0
    boundary_record = next(
        record
        for record in audit["records"]
        if record.get("segment_id") == "boundary" and record["side"] == "LEFT"
    )
    assert boundary_record["location_status"] == "3"
    assert audit["passed"] is True


def test_aliases_must_consistently_justify_missing_boundary_borough() -> None:
    geometry = LineString([(X, Y), (X + 100, Y)])
    lion = lion_frame([
        lion_row(
            segment_id="boundary",
            left_borough=None,
            right_id=0,
            segment_type="U",
            geometry=geometry,
            LocStatus=3,
            BoroBndry=1,
        ),
        lion_row(
            segment_id="boundary",
            left_borough=None,
            right_id=0,
            segment_type="U",
            geometry=geometry,
            SpecAddr="N",
            SAFStreetName="BOUNDARY ALIAS",
            LocStatus=None,
            BoroBndry=0,
        ),
    ])
    frequencies = frequency_frame([{
        "OBJECTID": 911,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    _, audit = build_collection_features(lion, frequencies)

    assert audit["outcomes"]["out_of_scope"] == 0
    assert audit["outcomes"]["invalid"] == 1
    assert audit["passed"] is False


def test_frequency_intersection_errors_are_not_silently_skipped() -> None:
    class SpatialIndex:
        @staticmethod
        def query(_geometry: object, *, predicate: str) -> list[int]:
            assert predicate == "intersects"
            return [0]

    class PositionLookup:
        @staticmethod
        def __getitem__(_position: int) -> object:
            return object()

    class GeometryColumn:
        iloc = PositionLookup()

    class Frequencies:
        geometry = GeometryColumn()

    source_line = LineString([(X, Y), (X + 100, Y)])
    offset_line = LineString([(X, Y + 25), (X + 100, Y + 25)])

    with pytest.raises(ValueError, match="frequency row 0 could not be intersected"):
        _collect_frequency_parts(
            Frequencies(),
            SpatialIndex(),
            [(source_line, offset_line)],
            tolerance_feet=0,
        )


def test_empty_optional_schedules_and_dsny_business_keys_are_audited_and_preserved() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([{
        "OBJECTID": 906,
        "SCHEDULECODE": "MN-01",
        "SECTION": "MN0101",
        "DISTRICT": "01",
        "FREQ_RECYCLING": None,
        "FREQ_ORGANICS": None,
        "FREQ_BULK": None,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    properties = payload["features"][0]["properties"]
    assert properties["schedules"]["REFUSE"] == ["MON", "THU"]
    assert properties["schedules"]["RECYCLING"] == []
    assert properties["dsny_sources"] == [{
        "frequency_row": 0,
        "object_id": "906",
        "schedule_code": "MN-01",
        "section": "MN0101",
        "district": "01",
    }]
    assert audit["frequency_schedule_empty_rows"] == {
        "FREQ_REFUSE": 0,
        "FREQ_RECYCLING": 1,
        "FREQ_ORGANICS": 1,
        "FREQ_BULK": 1,
    }
    assert audit["passed"] is True


def test_blank_organics_derives_only_from_explicit_recycling() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([{
        "OBJECTID": 1201,
        "FREQ_RECYCLING": "Tue, Sat",
        "FREQ_ORGANICS": None,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    properties = payload["features"][0]["properties"]
    assert properties["schedules"]["ORGANICS"] == ["TUE", "SAT"]
    assert properties["schedule_states"]["ORGANICS"] == {
        "state": "POLICY_DERIVED",
        "source_field": "FREQ_ORGANICS",
        "raw_value": None,
        "rule_id": "dsny-organics-on-recycling-day-v1",
        "source_policy_conflict": False,
        "provenance": "NYC DSNY citywide curbside compost collection occurs on the recycling day",
    }
    assert audit["schedule_state_counts"]["ORGANICS"]["POLICY_DERIVED"] == 1


def test_blank_organics_and_recycling_remain_unknown_without_inference() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([{
        "OBJECTID": 1202,
        "FREQ_RECYCLING": " ",
        "FREQ_ORGANICS": None,
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, _ = build_collection_features(lion, frequencies)

    properties = payload["features"][0]["properties"]
    assert properties["schedules"]["RECYCLING"] == []
    assert properties["schedules"]["ORGANICS"] == []
    assert properties["schedule_states"]["RECYCLING"]["state"] == "UNKNOWN_SOURCE_BLANK"
    assert properties["schedule_states"]["ORGANICS"]["state"] == "UNKNOWN_SOURCE_BLANK"


def test_explicit_organics_conflict_is_preserved_and_flagged() -> None:
    lion = lion_frame([lion_row(right_id=0)])
    frequencies = frequency_frame([{
        "OBJECTID": 1203,
        "FREQ_RECYCLING": "Tue",
        "FREQ_ORGANICS": "Wed",
        "geometry": box(X - 10, Y + 1, X + 110, Y + 100),
    }])

    payload, audit = build_collection_features(lion, frequencies)

    properties = payload["features"][0]["properties"]
    assert properties["schedules"]["ORGANICS"] == ["WED"]
    organics_state = properties["schedule_states"]["ORGANICS"]
    assert organics_state["state"] == "SOURCE_EXPLICIT"
    assert organics_state["source_policy_conflict"] is True
    assert audit["policy_conflicts"] == 1


def test_processed_digest_binds_exact_deterministic_payload_bytes() -> None:
    payload = {
        "type": "FeatureCollection", "schema_revision": 3,
        "features": [geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)])],
    }
    differently_ordered = {"features": payload["features"], "type": "FeatureCollection", "schema_revision": 3}

    processed_bytes = serialize_processed_payload(payload)
    assert processed_bytes == serialize_processed_payload(differently_ordered)
    audit: dict[str, object] = {"output_features": 1}
    digest = bind_processed_sha256(audit, processed_bytes)
    assert audit["processed_sha256"] == digest == hashlib.sha256(processed_bytes).hexdigest()
    assert audit["processed_feature_count"] == 1
    assert hashlib.sha256(processed_bytes + b" ").hexdigest() != digest


def test_loader_aggregates_duplicate_ids_and_removes_stale_schedules(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    first_payload = {
        "type": "FeatureCollection", "schema_revision": 3,
        "features": [
            geojson_feature("shared", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)]),
            geojson_feature(
                "shared",
                "segment-2",
                [(-74.0, 40.71), (-73.99, 40.71)],
                dsny_object_ids=["102"],
            ),
            geojson_feature("removed", "segment-old", [(-74.0, 40.73), (-73.99, 40.73)]),
        ],
    }

    assert load_payload(first_payload, database) == 2
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT segment_id, geometry_wkt FROM block_faces WHERE block_face_id = 'shared'"
        ).fetchone()
        assert row[0] == "segment-1|segment-2"
        assert wkt.loads(row[1]).geom_type == "MultiLineString"
        assert len(wkt.loads(row[1]).geoms) == 2
        assert connection.execute(
            "SELECT dsny_object_id FROM block_face_dsny_sources "
            "WHERE block_face_id = 'shared' ORDER BY dsny_object_id"
        ).fetchall() == [("101",), ("102",)]
        assert connection.execute(
            "SELECT origin_block_face_id FROM block_faces WHERE block_face_id = 'shared'"
        ).fetchone() == ("shared",)
        assert connection.execute(
            "SELECT segment_id, source_side FROM block_face_lion_components "
            "WHERE block_face_id = 'shared' ORDER BY segment_id"
        ).fetchall() == [("segment-1", "LEFT"), ("segment-2", "LEFT")]

    replacement_schedules = {
        "REFUSE": ["TUE"],
        "RECYCLING": [],
        "ORGANICS": [],
        "BULK": [],
    }
    replacement = {
        "type": "FeatureCollection", "schema_revision": 3,
        "features": [
            geojson_feature(
                "shared",
                "segment-3",
                [(-74.0, 40.72), (-73.99, 40.72)],
                feature_schedules=replacement_schedules,
            ),
        ],
    }
    load_payload(replacement, database)
    with sqlite3.connect(database) as connection:
        expected_secondary_indexes = {
            "idx_schedule_day_type",
            "idx_schedule_type_day_face",
            "idx_collection_state_type",
            "idx_unknown_reason",
            "idx_block_face_borough",
            "idx_block_face_origin",
            "idx_dsny_source_object",
        }
        actual_indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert expected_secondary_indexes <= actual_indexes
        rows = connection.execute(
            "SELECT collection_type, weekday FROM collection_schedules "
            "WHERE block_face_id = 'shared' ORDER BY collection_type, weekday"
        ).fetchall()
        assert rows == [("REFUSE", "TUE")]
        assert connection.execute("SELECT COUNT(*) FROM block_faces").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM block_face_dsny_sources").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM block_faces WHERE origin_block_face_id IS NOT NULL"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM block_face_lion_components").fetchone()[0] == 1
        assert connection.execute(
            "SELECT DISTINCT validation_status FROM collection_schedules"
        ).fetchall() == [("AUDITED_SIDE_TRACE_V3",)]


def test_loader_persists_split_feature_keys_origins_and_source_provenance(tmp_path) -> None:
    lion = lion_frame([{
        **lion_row(right_id=0),
        "geometry": LineString([(X, Y), (X + 200, Y)]),
    }])
    frequencies = frequency_frame([
        {
            "OBJECTID": 1101,
            "SCHEDULECODE": "MN-A",
            "SECTION": "A",
            "DISTRICT": "01",
            "geometry": box(X - 10, Y + 1, X + 100, Y + 100),
            "FREQ_REFUSE": "Mon",
        },
        {
            "OBJECTID": 1102,
            "SCHEDULECODE": "MN-B",
            "SECTION": "B",
            "DISTRICT": "01",
            "geometry": box(X + 100, Y + 1, X + 210, Y + 100),
            "FREQ_REFUSE": "Thu",
        },
    ])
    payload, audit = build_collection_features(lion, frequencies)
    assert audit["passed"] is True
    database = tmp_path / "split.sqlite3"

    assert load_payload(payload, database) == 2

    with sqlite3.connect(database) as connection:
        faces = connection.execute(
            "SELECT block_face_id, origin_block_face_id FROM block_faces ORDER BY block_face_id"
        ).fetchall()
        assert len(faces) == 2
        assert len({row[0] for row in faces}) == 2
        assert {row[1] for row in faces} == {"left-1"}
        assert connection.execute(
            "SELECT dsny_object_id, schedule_code, section, district "
            "FROM block_face_dsny_sources ORDER BY dsny_object_id"
        ).fetchall() == [
            ("1101", "MN-A", "A", "01"),
            ("1102", "MN-B", "B", "01"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM block_face_lion_components"
        ).fetchone()[0] == 2


def test_loader_validates_before_writes_and_rejects_conflicting_duplicates(tmp_path) -> None:
    database = tmp_path / "app.sqlite3"
    conflicting = {
        "type": "FeatureCollection", "schema_revision": 3,
        "features": [
            geojson_feature("shared", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)]),
            geojson_feature(
                "shared",
                "segment-2",
                [(-74.0, 40.71), (-73.99, 40.71)],
                feature_schedules=schedules(["TUE"]),
            ),
        ],
    }

    with pytest.raises(ValueError, match="conflicting metadata/schedules.*schedules"):
        load_payload(conflicting, database)
    assert not database.exists()


def test_loader_requires_exactly_four_schedule_types() -> None:
    feature = geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)])
    del feature["properties"]["schedules"]["BULK"]

    with pytest.raises(ValueError, match="must contain exactly"):
        prepare_features({"type": "FeatureCollection", "schema_revision": 3, "features": [feature]})


def test_loader_requires_component_segment_union_to_match_feature() -> None:
    feature = geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)])
    feature["properties"]["segment_ids"].append("segment-2")

    with pytest.raises(ValueError, match="component segment IDs must exactly match"):
        prepare_features({"type": "FeatureCollection", "schema_revision": 3, "features": [feature]})


def test_loader_requires_component_dsny_union_to_match_feature() -> None:
    feature = geojson_feature(
        "face-1",
        "segment-1",
        [(-74.0, 40.7), (-73.99, 40.7)],
        dsny_object_ids=["101", "102"],
    )
    feature["properties"]["lion_components"][0]["dsny_object_ids"] = ["101"]

    with pytest.raises(ValueError, match="component DSNY IDs must exactly match"):
        prepare_features({"type": "FeatureCollection", "schema_revision": 3, "features": [feature]})


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda record: record.pop("source_index"), "missing required provenance fields"),
        (lambda record: record.update(source_row=4), "source_row does not align"),
        (lambda record: record.update(source_index=4), "source_index does not align"),
        (lambda record: record.update(segment_id="other"), "segment_id does not match"),
    ],
)
def test_loader_requires_aligned_source_record_provenance(mutation, message: str) -> None:
    feature = geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-73.99, 40.7)])
    source_record = feature["properties"]["lion_components"][0]["source_records"][0]
    mutation(source_record)

    with pytest.raises(ValueError, match=message):
        prepare_features({"type": "FeatureCollection", "schema_revision": 3, "features": [feature]})


def test_loader_rejects_zero_length_geometry() -> None:
    feature = geojson_feature("face-1", "segment-1", [(-74.0, 40.7), (-74.0, 40.7)])

    with pytest.raises(ValueError, match="valid non-empty"):
        prepare_features({"type": "FeatureCollection", "schema_revision": 3, "features": [feature]})
