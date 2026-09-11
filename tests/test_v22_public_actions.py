from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import os
import subprocess
import sys

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.action_errors import PublicActionError
from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.findings import build_public_findings
from findings_helpers import collection, market, request, site


PLANNING_DATE = date(2026, 9, 2)


def actionable_findings(*, noindex_page: int = 1):
    records = [
        {"status_code": 503, "title": "Broken"},
        {
            "status_code": 200,
            "title": "Shared",
            "meta_robots": ["noindex"] if noindex_page == 1 else [],
        },
        {
            "status_code": 200,
            "title": "Shared",
            "meta_robots": ["noindex"] if noindex_page == 2 else [],
        },
    ]
    serp = market()
    return build_public_findings(request(site(records), serp, collection(serp)))


def insufficient_findings():
    serp = market()
    return build_public_findings(request(site(), serp, collection(serp)))


def action_input(result=None, *, planning_date=PLANNING_DATE):
    return PublicActionPlanInput(
        findings_result=result or actionable_findings(),
        planning_date=planning_date,
    )


def with_market_clones(result, count: int):
    copied = deepcopy(result)
    finding = next(item for item in copied.findings if item.rule_id.endswith("competitors_ahead"))
    evaluation = next(item for item in copied.rule_evaluations if item.finding_id == finding.finding_id)
    for index in range(count):
        finding_id = f"fn_market_clone_{index}"
        copied.findings.append(finding.model_copy(update={"finding_id": finding_id}))
        copied.rule_evaluations.append(
            evaluation.model_copy(update={"finding_id": finding_id})
        )
    copied.site_rollup.finding_ids = sorted(item.finding_id for item in copied.findings)
    return copied


def test_builder_selects_three_distinct_grouped_actions_with_dependencies() -> None:
    result = actionable_findings()
    plan = build_public_action_plan(action_input(result))

    assert [item.template_key for item in plan.actions] == [
        "restore_site_access_indexing",
        "review_market_visibility",
        "differentiate_page_titles",
    ]
    assert [item.sequence for item in plan.actions] == [1, 2, 3]
    assert [item.review_date for item in plan.actions] == [
        PLANNING_DATE + timedelta(days=30),
        PLANNING_DATE + timedelta(days=60),
        PLANNING_DATE + timedelta(days=90),
    ]
    recovery = plan.actions[0]
    findings = {item.finding_id: item for item in result.findings}
    assert {findings[key].rule_id for key in recovery.finding_ids} == {
        "v22_public.site_http_error",
        "v22_public.site_explicit_noindex",
    }
    assert plan.actions[1].dependencies == [recovery.action_id]
    assert plan.actions[2].dependencies == [recovery.action_id]
    assert len(set(item.action_id for item in plan.actions)) == 3
    assert any(item.selection_state == "unselected" for item in plan.selection_audit)


def test_input_order_and_repeated_build_do_not_change_output() -> None:
    result = actionable_findings()
    before = canonical_json_bytes(result)
    baseline = build_public_action_plan(action_input(result))
    shuffled = deepcopy(result)
    shuffled.findings.reverse()
    shuffled.rule_evaluations.reverse()
    shuffled.evidence_result.evidence_index.reverse()
    shuffled.evidence_result.source_traces.reverse()

    assert build_public_action_plan(action_input(shuffled)) == baseline
    assert build_public_action_plan(action_input(result)) == baseline
    assert canonical_json_bytes(result) == before


def test_planning_date_changes_dates_but_not_action_identity() -> None:
    result = actionable_findings()
    first = build_public_action_plan(action_input(result))
    later = build_public_action_plan(action_input(result, planning_date=PLANNING_DATE + timedelta(days=10)))

    assert [item.action_id for item in later.actions] == [item.action_id for item in first.actions]
    assert [item.review_date for item in later.actions] != [item.review_date for item in first.actions]


def test_target_change_changes_only_the_affected_action_identity() -> None:
    first = build_public_action_plan(action_input(actionable_findings(noindex_page=1)))
    second = build_public_action_plan(action_input(actionable_findings(noindex_page=2)))
    first_ids = {item.template_key: item.action_id for item in first.actions}
    second_ids = {item.template_key: item.action_id for item in second.actions}

    assert first_ids["restore_site_access_indexing"] != second_ids["restore_site_access_indexing"]
    assert first_ids["review_market_visibility"] == second_ids["review_market_visibility"]
    assert first_ids["differentiate_page_titles"] == second_ids["differentiate_page_titles"]


def test_low_confidence_page_gap_does_not_displace_medium_confidence_title_action() -> None:
    plan = build_public_action_plan(action_input())
    selected = {item.template_key for item in plan.actions}
    assert "differentiate_page_titles" in selected
    assert "close_page_type_gap" not in selected


def test_higher_confidence_candidate_wins_within_the_same_severity() -> None:
    result = actionable_findings()
    asset = next(item for item in result.findings if item.rule_id.endswith("sample_page_type_gap"))
    asset.severity = "medium"
    asset.confidence = "high"
    asset.classification = "fact"
    plan = build_public_action_plan(action_input(result))

    assert "close_page_type_gap" in {item.template_key for item in plan.actions}
    assert "differentiate_page_titles" not in {item.template_key for item in plan.actions}


def test_repeated_market_findings_have_a_capped_priority_and_stable_action_id() -> None:
    five = build_public_action_plan(action_input(with_market_clones(actionable_findings(), 2)))
    many = build_public_action_plan(action_input(with_market_clones(actionable_findings(), 12)))
    five_audit = next(item for item in five.selection_audit if item.template_key == "review_market_visibility")
    many_audit = next(item for item in many.selection_audit if item.template_key == "review_market_visibility")

    assert five_audit.priority.impact_scope == many_audit.priority.impact_scope == 8
    assert five_audit.action_id == many_audit.action_id


def test_fewer_than_three_actionable_work_packages_fails_without_partial_output() -> None:
    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(insufficient_findings()))
    assert exc.value.error_code == "V22_ACTIONS_INSUFFICIENT_ACTIONABLE_FINDINGS"
    assert exc.value.retryable is False


def test_unknown_rule_is_not_silently_ignored() -> None:
    result = actionable_findings()
    target = result.findings[0]
    target.rule_id = "v22_public.unknown_rule"
    evaluation = next(item for item in result.rule_evaluations if item.finding_id == target.finding_id)
    evaluation.rule_id = target.rule_id

    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(result))
    assert exc.value.error_code == "V22_ACTIONS_UNSUPPORTED_FINDING"


def test_unknown_rule_version_is_not_silently_accepted() -> None:
    result = actionable_findings()
    target = result.findings[0]
    target.rule_version = "9.9.9"
    evaluation = next(item for item in result.rule_evaluations if item.finding_id == target.finding_id)
    evaluation.rule_version = target.rule_version

    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(result))
    assert exc.value.error_code == "V22_ACTIONS_UNSUPPORTED_FINDING"


def test_target_not_present_in_referenced_evidence_is_rejected() -> None:
    result = actionable_findings()
    target = next(item for item in result.findings if item.rule_id.endswith("site_http_error"))
    target.affected_urls = ["https://example.test/invented"]
    evaluation = next(item for item in result.rule_evaluations if item.finding_id == target.finding_id)
    evaluation.target.urls = target.affected_urls

    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(result))
    assert exc.value.error_code == "V22_ACTIONS_REFERENCE_INVALID"


def test_unknown_evidence_reference_is_rejected() -> None:
    result = actionable_findings()
    target = result.findings[0]
    target.evidence_ids = ["ev_missing_reference"]
    evaluation = next(item for item in result.rule_evaluations if item.finding_id == target.finding_id)
    evaluation.evidence_ids = target.evidence_ids

    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(result))
    assert exc.value.error_code == "V22_ACTIONS_REFERENCE_INVALID"


def test_nonfinite_input_is_rejected_before_action_selection() -> None:
    result = actionable_findings()
    result.evidence_result.evidence_index[0].original_value = float("nan")
    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(result))
    assert exc.value.error_code == "V22_ACTIONS_INPUT_INVALID"


@pytest.mark.parametrize(
    "limit,maximum",
    [
        ("max_candidates", 3),
        ("max_findings_per_action", 1),
        ("max_targets_per_action", 1),
        ("max_steps_per_action", 1),
        ("max_assets_per_action", 0),
        ("max_audit_entries", 3),
        ("max_bytes", 1),
    ],
)
def test_resource_limits_cover_candidates_and_action_output(limit: str, maximum: int) -> None:
    value = action_input()
    setattr(value.limits, limit, maximum)
    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(value)
    assert exc.value.error_code == "V22_ACTIONS_LIMIT_EXCEEDED"


def test_review_date_overflow_is_a_safe_deterministic_error() -> None:
    with pytest.raises(PublicActionError) as exc:
        build_public_action_plan(action_input(planning_date=date.max))
    assert exc.value.error_code == "V22_ACTIONS_DATE_INVALID"


def test_output_is_stable_across_python_hash_seeds() -> None:
    value = action_input()
    program = (
        "from app.report_v22.action_models import PublicActionPlanInput; "
        "from app.report_v22.actions import build_public_action_plan; import sys; "
        "print(build_public_action_plan(PublicActionPlanInput.model_validate_json(sys.stdin.read())).model_dump_json())"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", program],
            input=value.model_dump_json().encode(),
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        for seed in (17, 91)
    ]
    assert outputs[0] == outputs[1]


def test_output_contains_only_input_findings_targets_and_sources() -> None:
    result = actionable_findings()
    plan = build_public_action_plan(action_input(result))
    known_findings = {item.finding_id for item in result.findings}
    known_urls = {str(url) for item in result.findings for url in item.affected_urls}
    known_queries = {query for item in result.findings for query in item.affected_queries}
    known_sources = {item.source_type for item in result.evidence_result.evidence_index}

    for action in plan.actions:
        assert not hasattr(action, "why_now")
        assert not hasattr(action, "client_facing_explanation")
        assert set(action.finding_ids) <= known_findings
        assert set(action.data_sources) <= known_sources
        for target in action.exact_targets:
            if target.url is not None:
                assert str(target.url) in known_urls or target.kind == "site"
            if target.query is not None:
                assert target.query in known_queries
