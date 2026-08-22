"""Fail-closed evidence gates for identity and geometry recovery.

The initial v3 rollout records candidates and source versions only.  Promotion
stays disabled until matching releases, deterministic shadow builds, and the
required manual samples have all been recorded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


IDENTITY_RULE_VERSION = "pad-addresspoint-corroboration-v1"
GEOMETRY_RULE_VERSION = "cscl-microgap-witness-v1"
RELEASE_PATTERN = re.compile(r"(?i)(?:lion|pad)[^0-9]{0,12}(\d{2}[a-z])")
GENERIC_RELEASE_PATTERN = re.compile(r"(?i)(?:current\s+version|release)\s*:?\s*(\d{2}[a-z])")


def source_release_identifier(names: Iterable[str], source: str) -> str | None:
    matches = {
        match.group(1).upper()
        for name in names
        for match in RELEASE_PATTERN.finditer(name)
        if source.casefold() in name.casefold()
    }
    return next(iter(matches)) if len(matches) == 1 else None


def metadata_release_identifier(metadata: dict[str, object]) -> str | None:
    matches = {
        match.group(1).upper()
        for value in metadata.values()
        if isinstance(value, str)
        for match in GENERIC_RELEASE_PATTERN.finditer(value)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def identity_shadow_report(
    *,
    lion_release: str | None,
    pad_release: str | None,
    addresspoint_metadata_before: dict[str, object],
    addresspoint_metadata_after: dict[str, object],
) -> dict[str, object]:
    releases_match = bool(lion_release and pad_release and lion_release == pad_release)
    metadata_stable = addresspoint_metadata_before == addresspoint_metadata_after
    reasons: list[str] = []
    if not releases_match:
        reasons.append("PAD_LION_RELEASE_MISMATCH")
    if not metadata_stable:
        reasons.append("ADDRESSPOINT_SOURCE_MUTATED_DURING_QUERY")
    reasons.extend(["TWO_STABLE_SHADOW_BUILDS_REQUIRED", "MANUAL_REVIEW_REQUIRED"])
    return {
        "rule_version": IDENTITY_RULE_VERSION,
        "mode": "shadow",
        "lion_release": lion_release,
        "pad_release": pad_release,
        "releases_match": releases_match,
        "source_metadata_stable": metadata_stable,
        "candidate_count": 0,
        "evaluated_count": 0,
        "evaluation_status": "BLOCKED_BEFORE_ADDRESSPOINT_QUERY",
        "promoted_count": 0,
        "promotion_enabled": False,
        "blocking_reasons": reasons,
        "prohibited_methods": [
            "nearest_polygon", "neighboring_street", "segment_id_only",
            "blank_source_value", "legacy_dsny_cscl_identity",
        ],
    }


def addresspoint_evidence_passes(evidence: dict[str, object]) -> bool:
    """Return true only when every accepted-plan AddressPoint gate is explicit."""

    component_length = float(evidence.get("component_length_feet", 0))
    endpoint_margin = min(20.0, component_length * 0.10)
    best_distance = float(evidence.get("best_distance_feet", float("inf")))
    runner_up = float(evidence.get("runner_up_distance_feet", float("inf")))
    return (
        evidence.get("borough_agrees") is True
        and evidence.get("actual_b7sc_agrees") is True
        and evidence.get("decoded_side_agrees") is True
        and evidence.get("geometric_side_agrees") is True
        and evidence.get("house_range_agrees") is True
        and evidence.get("parity_agrees") is True
        and best_distance <= 75.0
        and float(evidence.get("projection_from_start_feet", -1)) >= endpoint_margin
        and float(evidence.get("projection_from_end_feet", -1)) >= endpoint_margin
        and evidence.get("unique_nearest_same_b7sc_component") is True
        and runner_up - best_distance >= 25.0
        and runner_up >= best_distance * 1.5
        and evidence.get("contradictory_point_count") == 0
    )


def cscl_microgap_evidence_passes(evidence: dict[str, object]) -> bool:
    """Evaluate the tiny-gap witness without moving current LION geometry."""

    return (
        evidence.get("unique_physicalid_match") is True
        and evidence.get("current_single_unbranched_path") is True
        and evidence.get("cscl_single_unbranched_path") is True
        and float(evidence.get("endpoint_distance_feet", float("inf"))) <= 0.25
        and float(evidence.get("hausdorff_feet", float("inf"))) <= 0.25
        and 0.99 <= float(evidence.get("length_ratio", 0)) <= 1.01
        and 0.99 <= float(evidence.get("projected_span_ratio", 0)) <= 1.01
        and float(evidence.get("current_coverage_ratio", 0)) >= 0.99
        and float(evidence.get("uncovered_length_feet", float("inf"))) <= 3.0
        and float(evidence.get("cscl_exact_coverage_ratio", 0)) >= 0.995
        and evidence.get("schedule_signature_count") == 1
        and evidence.get("different_signature_within_3_feet") is False
        and evidence.get("covered_signature_agrees_all_four") is True
    )
