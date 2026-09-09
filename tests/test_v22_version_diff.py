from copy import deepcopy

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.version_diff import build_version_diff
from app.report_v22.version_diff_errors import VersionDiffError
from app.report_v22.version_diff_identity import finding_fingerprint
from version_diff_helpers import version_diff_request


def test_builder_emits_only_changes_and_partitions_every_new_finding() -> None:
    request = version_diff_request()
    result = build_version_diff(request)
    parent_ids = {item.finding_id for item in request.parent_report.findings}
    changed_ids = {
        item.previous_finding.finding_id
        for item in result.version_diff.entries
        if item.previous_finding is not None
    }
    unchanged_ids = {item.finding_id for item in result.unchanged_previous_findings}
    assert changed_ids.isdisjoint(unchanged_ids)
    assert changed_ids | unchanged_ids == parent_ids
    assert all(item.change_type != "replaced" for item in result.version_diff.entries)

    expected_new = {
        (origin, stage.ruleset_version, item.finding_id)
        for origin, stage in (
            ("first_party_findings", request.verified_reprioritization_input.first_party_result),
            ("cross_source_findings", request.verified_reprioritization_input.cross_source_result),
        )
        for item in stage.findings
    }
    consumed = {(item.origin_stage, item.ruleset_version, item.finding_id) for item in result.consumed_new_findings}
    new = {(item.origin_stage, item.ruleset_version, item.finding_id) for item in result.new_findings}
    assert consumed.isdisjoint(new)
    assert consumed | new == expected_new


def test_previous_fingerprint_covers_the_complete_parent_finding() -> None:
    request = version_diff_request()
    result = build_version_diff(request)
    by_id = {item.finding_id: item for item in request.parent_report.findings}
    references = [
        *(item.previous_finding for item in result.version_diff.entries if item.previous_finding),
        *result.unchanged_previous_findings,
    ]
    assert all(item.fingerprint == finding_fingerprint(by_id[item.finding_id]) for item in references)


def test_tampered_parent_and_resigned_upstream_are_rejected() -> None:
    request = version_diff_request()
    changed_parent = deepcopy(request.parent_report)
    changed_parent.findings[0].statement = "Structurally valid but changed."
    forged_parent = request.model_copy(update={
        "parent_report": changed_parent,
        "parent_report_checksum": request_digest(changed_parent),
    })
    with pytest.raises(VersionDiffError) as exc:
        build_version_diff(forged_parent)
    assert exc.value.error_code == "V22_VERSION_DIFF_PARENT_MISMATCH"

    changed_result = deepcopy(request.verified_reprioritization_result)
    changed_result.relations[0].target_key = f"{changed_result.relations[0].target_key}-changed"
    forged_upstream = request.model_copy(update={
        "verified_reprioritization_result": changed_result,
        "verified_reprioritization_result_checksum": request_digest(changed_result),
    })
    with pytest.raises(VersionDiffError) as exc:
        build_version_diff(forged_upstream)
    assert exc.value.error_code == "V22_VERSION_DIFF_UPSTREAM_MISMATCH"


def test_repeated_build_is_byte_deterministic_and_has_no_raw_provider_payload() -> None:
    request = version_diff_request()
    first = build_version_diff(request)
    second = build_version_diff(request)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    encoded = canonical_json_bytes(first).decode()
    assert "raw_payload" not in encoded
    assert "keyword_rows" not in encoded


def test_limits_fail_without_partial_output() -> None:
    request = version_diff_request()
    request.limits.max_bytes = 1
    with pytest.raises(VersionDiffError) as exc:
        build_version_diff(request)
    assert exc.value.error_code == "V22_VERSION_DIFF_LIMIT_EXCEEDED"


def test_strict_query_support_confirms_one_old_finding_and_is_not_duplicated_as_new() -> None:
    from test_v22_first_party_findings import gsc_value

    gsc = gsc_value()
    rows = list(gsc.current.queries.rows)
    rows[0] = rows[0].model_copy(update={"key": "  ＰＬＵＭＢＥＲ  "})
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "queries": gsc.current.queries.model_copy(update={"rows": rows}),
        }),
    })
    result = build_version_diff(version_diff_request(gsc=gsc))
    confirmed = [item for item in result.version_diff.entries if item.change_type == "confirmed"]
    assert len(confirmed) == 1
    assert len(confirmed[0].current_finding_ids) == 2
    consumed_ids = {item.finding_id for item in result.consumed_new_findings}
    new_ids = {item.finding_id for item in result.new_findings}
    assert set(confirmed[0].current_finding_ids) - {confirmed[0].previous_finding.finding_id} <= consumed_ids
    assert consumed_ids.isdisjoint(new_ids)


def test_growth_refines_before_reprioritization_and_hard_facts_remain_protected() -> None:
    from app.google_connections_v22.gsc import MetricRow
    from test_v22_first_party_findings import gsc_value

    gsc = gsc_value()
    current = MetricRow(
        key="https://example.test/page-1", clicks=15, impressions=150, ctr=.1, position=2,
    )
    previous = MetricRow(
        key="https://example.test/page-1", clicks=10, impressions=100, ctr=.1, position=2,
    )
    gsc = gsc.model_copy(update={
        "current": gsc.current.model_copy(update={
            "pages": gsc.current.pages.model_copy(update={"rows": [current]}),
        }),
        "previous": gsc.previous.model_copy(update={
            "pages": gsc.previous.pages.model_copy(update={"rows": [previous]}),
        }),
    })
    result = build_version_diff(version_diff_request(gsc=gsc))
    refined = [item for item in result.version_diff.entries if item.change_type == "refined"]
    assert len(refined) == 1
    assert "lowers the urgency" in refined[0].reason
    hard_ids = {
        finding_id
        for audit in version_diff_request(gsc=gsc).verified_reprioritization_input.public_action_plan.selection_audit
        if audit.template_key == "restore_site_access_indexing"
        for finding_id in audit.finding_ids
    }
    assert not hard_ids.intersection(
        item.previous_finding.finding_id for item in refined if item.previous_finding
    )


def test_measurement_gate_reprioritizes_with_explicit_issue_codes() -> None:
    from test_v22_first_party_findings import gsc_value

    result = build_version_diff(version_diff_request(gsc=gsc_value(unhealthy=True)))
    audits = [item for item in result.audit if item.change_type == "reprioritized"]
    assert audits
    assert any(item.reason_code == "REPRIORITIZED_BY_MEASUREMENT_GATE" for item in audits)
    assert any(item.decision_issue_codes for item in audits)
