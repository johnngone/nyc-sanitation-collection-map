from scripts.recovery_shadow import (
    addresspoint_evidence_passes,
    cscl_microgap_evidence_passes,
    identity_shadow_report,
)


def test_release_mismatch_keeps_identity_promotion_disabled() -> None:
    report = identity_shadow_report(
        lion_release="26C",
        pad_release="26B",
        addresspoint_metadata_before={"rowsUpdatedAt": 1},
        addresspoint_metadata_after={"rowsUpdatedAt": 1},
    )
    assert report["promotion_enabled"] is False
    assert report["promoted_count"] == 0
    assert "PAD_LION_RELEASE_MISMATCH" in report["blocking_reasons"]


def test_addresspoint_gate_rejects_endpoint_and_ambiguous_runner_up() -> None:
    evidence = {
        "borough_agrees": True,
        "actual_b7sc_agrees": True,
        "decoded_side_agrees": True,
        "geometric_side_agrees": True,
        "house_range_agrees": True,
        "parity_agrees": True,
        "best_distance_feet": 10,
        "runner_up_distance_feet": 40,
        "component_length_feet": 200,
        "projection_from_start_feet": 20,
        "projection_from_end_feet": 20,
        "unique_nearest_same_b7sc_component": True,
        "contradictory_point_count": 0,
    }
    assert addresspoint_evidence_passes(evidence)
    assert not addresspoint_evidence_passes({**evidence, "projection_from_start_feet": 19.9})
    assert not addresspoint_evidence_passes({**evidence, "runner_up_distance_feet": 34.9})
    assert not addresspoint_evidence_passes({**evidence, "contradictory_point_count": 1})


def test_cscl_microgap_gate_is_all_or_nothing() -> None:
    evidence = {
        "unique_physicalid_match": True,
        "current_single_unbranched_path": True,
        "cscl_single_unbranched_path": True,
        "endpoint_distance_feet": 0.25,
        "hausdorff_feet": 0.25,
        "length_ratio": 0.99,
        "projected_span_ratio": 1.01,
        "current_coverage_ratio": 0.99,
        "uncovered_length_feet": 3.0,
        "cscl_exact_coverage_ratio": 0.995,
        "schedule_signature_count": 1,
        "different_signature_within_3_feet": False,
        "covered_signature_agrees_all_four": True,
    }
    assert cscl_microgap_evidence_passes(evidence)
    assert not cscl_microgap_evidence_passes({**evidence, "uncovered_length_feet": 3.01})
    assert not cscl_microgap_evidence_passes({**evidence, "hausdorff_feet": 0.251})
    assert not cscl_microgap_evidence_passes({**evidence, "different_signature_within_3_feet": True})

