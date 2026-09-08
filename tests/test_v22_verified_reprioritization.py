from copy import deepcopy
from datetime import timedelta

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.actions import canonical_public_findings
from app.report_v22.verified_reprioritization import build_verified_reprioritization
from app.report_v22.verified_reprioritization_errors import VerifiedReprioritizationError
from verified_reprioritization_helpers import PLANNING_DATE, verified_request


def test_builder_returns_exactly_three_actions_and_public_core_problem() -> None:
    result = build_verified_reprioritization(verified_request())
    assert [item.sequence for item in result.actions] == [1, 2, 3]
    assert result.core_problem_finding.origin_stage == "public_findings"
    assert [
        item.public_action.review_date if item.public_action else item.measurement_action.review_date
        for item in result.actions
    ] == [PLANNING_DATE + timedelta(days=30 * index) for index in (1, 2, 3)]
    assert len(result.selection_audit) >= 4


def test_tampered_upstream_result_is_rejected_even_when_resigned() -> None:
    value = verified_request()
    changed = deepcopy(value.public_findings_result)
    changed.findings[0].statement = "Structurally valid but modified."
    forged = value.model_copy(update={
        "public_findings_result": changed,
        "public_findings_result_checksum": request_digest(canonical_public_findings(changed)),
    })
    with pytest.raises(VerifiedReprioritizationError) as exc:
        build_verified_reprioritization(forged)
    assert exc.value.error_code == "V22_VERIFIED_REPRIORITIZATION_UPSTREAM_MISMATCH"


def test_wrong_parent_binding_is_rejected() -> None:
    value = verified_request()
    altered = value.model_copy(update={"parent_report_id": value.case_id})
    with pytest.raises(VerifiedReprioritizationError) as exc:
        build_verified_reprioritization(altered)
    assert exc.value.error_code == "V22_VERIFIED_REPRIORITIZATION_BINDING_INVALID"


def test_repeated_build_is_byte_deterministic_and_contains_no_raw_provider_payload() -> None:
    value = verified_request()
    first = build_verified_reprioritization(value)
    second = build_verified_reprioritization(value)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    encoded = canonical_json_bytes(first).decode()
    assert "raw_payload" not in encoded


def test_unhealthy_required_source_forces_measurement_repair_first() -> None:
    from test_v22_first_party_findings import gsc_value

    result = build_verified_reprioritization(verified_request(gsc=gsc_value(unhealthy=True)))
    assert result.actions[0].candidate_kind == "measurement_repair"
    assert result.actions[0].measurement_action.template_key == "restore_verified_measurement"
    assert all(item.candidate_kind == "existing_public" for item in result.actions[1:])
    assert result.core_problem_finding.origin_stage == "public_findings"


def test_checksum_mismatch_is_rejected_before_recomputation() -> None:
    value = verified_request().model_copy(update={
        "cross_source_result_checksum": f"sha256:{'0' * 64}",
    })
    with pytest.raises(VerifiedReprioritizationError) as exc:
        build_verified_reprioritization(value)
    assert exc.value.error_code == "V22_VERIFIED_REPRIORITIZATION_CHECKSUM_MISMATCH"


def test_nfkc_query_match_promotes_market_action_with_single_source_support() -> None:
    from test_v22_first_party_findings import gsc_value

    gsc = gsc_value()
    rows = list(gsc.current.queries.rows)
    rows[0] = rows[0].model_copy(update={"key": "  ＰＬＵＭＢＥＲ  "})
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "queries": gsc.current.queries.model_copy(update={"rows": rows}),
        }),
    })
    result = build_verified_reprioritization(verified_request(gsc=gsc))
    market = next(item for item in result.selection_audit if item.public_anchor and item.candidate_key.startswith("candidate-")
        and next((action.public_action for action in result.actions if action.action_id == item.action_id), None)
        and next(action.public_action for action in result.actions if action.action_id == item.action_id).template_key == "review_market_visibility")
    assert market.verification_level == "single_source_supported"
    assert any(item.reason_code == "STRICT_SINGLE_SOURCE_SUPPORT" and item.action_id == market.action_id
        for item in result.relations)


def test_cross_source_page_support_outranks_public_only_candidates() -> None:
    from app.google_connections_v22.gsc import MetricRow
    from test_v22_cross_source_findings import aligned_values

    gsc, ga4 = aligned_values()
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={"pages": gsc.current.pages.model_copy(update={
            "rows": [MetricRow(key="https://example.test/page-2", clicks=10, impressions=750, ctr=.013, position=8)],
        })}),
        "previous": gsc.previous.model_copy(update={"pages": gsc.previous.pages.model_copy(update={
            "rows": [MetricRow(key="https://example.test/page-2", clicks=20, impressions=200, ctr=.1, position=9)],
        })}),
    })
    current = ga4.current.landing_pages.rows[0].model_copy(update={"landing_page": "/page-2", "sessions": 20})
    previous = ga4.previous.landing_pages.rows[0].model_copy(update={"landing_page": "/page-2", "sessions": 50})
    ga4 = ga4.model_copy(update={
        "current": ga4.current.model_copy(update={"landing_pages": ga4.current.landing_pages.model_copy(update={"rows": [current]})}),
        "previous": ga4.previous.model_copy(update={"landing_pages": ga4.previous.landing_pages.model_copy(update={"rows": [previous]})}),
    })
    result = build_verified_reprioritization(verified_request(gsc=gsc, ga4=ga4))
    title = next(item for item in result.actions if item.public_action and item.public_action.template_key == "differentiate_page_titles")
    assert title.verification_level == "cross_source_supported"
    assert any(item.reason_code == "STRICT_CROSS_SOURCE_SUPPORT" and item.action_id == title.action_id
        for item in result.relations)


def test_growth_reduces_normal_action_but_not_noindex_or_http_repair() -> None:
    from app.google_connections_v22.gsc import MetricRow
    from test_v22_first_party_findings import gsc_value

    gsc = gsc_value()
    current = MetricRow(key="https://example.test/page-1", clicks=15, impressions=150, ctr=.1, position=2)
    previous = MetricRow(key="https://example.test/page-1", clicks=10, impressions=100, ctr=.1, position=2)
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={"pages": gsc.current.pages.model_copy(update={"rows": [current]})}),
        "previous": gsc.previous.model_copy(update={"pages": gsc.previous.pages.model_copy(update={"rows": [previous]})}),
    })
    result = build_verified_reprioritization(verified_request(gsc=gsc))
    restore = next(item for item in result.selection_audit if item.public_anchor and item.action_id == next(
        action.action_id for action in result.actions if action.public_action and action.public_action.template_key == "restore_site_access_indexing"
    ))
    title = next(item for item in result.selection_audit if item.verification_level == "reduced_by_growth")
    assert restore.verification_level != "reduced_by_growth"
    assert title.verification_rank == 0
    assert any(item.reason_code == "HARD_PUBLIC_FACT_PROTECTED" for item in result.relations)


def test_direction_conflict_is_ordinary_candidate_above_public_only() -> None:
    from test_v22_cross_source_findings import aligned_values

    gsc, ga4 = aligned_values(conflict=True)
    result = build_verified_reprioritization(verified_request(gsc=gsc, ga4=ga4))
    conflict = next(item for item in result.actions if item.candidate_kind == "measurement_consistency")
    assert conflict.verification_rank == 3
    assert conflict.measurement_action.template_key == "review_measurement_consistency"
    assert result.actions[0].candidate_kind == "measurement_consistency"


def test_missing_optional_gbp_never_forces_measurement_repair() -> None:
    result = build_verified_reprioritization(verified_request())
    assert all(item.candidate_kind != "measurement_repair" for item in result.actions)


def test_output_limit_and_date_overflow_are_safe_failures() -> None:
    value = verified_request()
    value.limits.max_bytes = 1
    with pytest.raises(VerifiedReprioritizationError) as exc:
        build_verified_reprioritization(value)
    assert exc.value.error_code == "V22_VERIFIED_REPRIORITIZATION_LIMIT_EXCEEDED"

    overflow = verified_request().model_copy(update={"planning_date": __import__("datetime").date.max})
    with pytest.raises(VerifiedReprioritizationError) as exc:
        build_verified_reprioritization(overflow)
    assert exc.value.error_code == "V22_VERIFIED_REPRIORITIZATION_INPUT_INVALID"
