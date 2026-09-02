from copy import deepcopy
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.report_v22.action_models import ActionTarget, PublicActionPlan


PLANNING_DATE = date(2026, 9, 2)


def action(index: int, *, dependencies: list[str] | None = None) -> dict:
    finding_id = f"fn_action_{index}"
    return {
        "action_id": f"ac_action_{index}",
        "sequence": index,
        "finding_ids": [finding_id],
        "template_key": "restore_site_access_indexing",
        "template_version": "1.0.0",
        "exact_targets": [
            {"kind": "site", "url": "https://example.test/", "finding_ids": [finding_id]}
        ],
        "implementation_steps": [
            {"sequence": 1, "title": "Inspect the target", "instruction": "Review the bound evidence."}
        ],
        "specification": {
            "content_requirements": [],
            "gbp_requirements": [],
            "technical_requirements": ["Correct only the confirmed condition."],
        },
        "required_client_assets": [],
        "dependencies": dependencies or [],
        "owner_suggestion": "Technical SEO lead",
        "effort_bucket": "small",
        "definition_of_done": ["A new bound snapshot no longer triggers the condition."],
        "validation_metrics": [
            {
                "metric_key": "condition_recheck",
                "baseline": "The condition is present in saved evidence.",
                "success_condition": "A new bound snapshot no longer triggers the condition.",
                "source_types": ["site"],
            }
        ],
        "data_sources": ["site"],
        "review_date": PLANNING_DATE + timedelta(days=30 * index),
        "copy_requirements": {
            "finding_ids": [finding_id],
            "allowed_fact_fields": ["finding.statement", "target.site"],
            "required_limitations": ["Do not claim a ranking outcome."],
        },
    }


def plan_payload() -> dict:
    actions = [action(1), action(2, dependencies=["ac_action_1"]), action(3)]
    return {
        "schema_version": "public_action_plan_v1",
        "action_catalog_version": "v22_public_actions_v1",
        "source_ruleset_version": "v22_public_findings_v1",
        "planning_date": PLANNING_DATE,
        "findings_checksum": f"sha256:{'0' * 64}",
        "actions": actions,
        "selection_audit": [
            {
                "candidate_key": f"candidate-{index}",
                "action_id": item["action_id"],
                "template_key": item["template_key"],
                "anchor_finding_id": item["finding_ids"][0],
                "finding_ids": item["finding_ids"],
                "priority": {
                    "severity_rank": 2,
                    "confidence_rank": 2,
                    "classification_rank": 3,
                    "impact_scope": 1,
                },
                "selection_state": "selected",
                "reason": "selected_top_three",
            }
            for index, item in enumerate(actions, 1)
        ],
        "unselected_finding_ids": [],
    }


def test_plan_contract_accepts_exactly_three_ordered_actions() -> None:
    plan = PublicActionPlan.model_validate(plan_payload())
    assert [item.sequence for item in plan.actions] == [1, 2, 3]
    assert plan.actions[1].dependencies == ["ac_action_1"]


@pytest.mark.parametrize("change", ["too_few", "sequence", "duplicate", "unknown_dependency", "review_date"])
def test_plan_contract_rejects_invalid_action_sets(change: str) -> None:
    payload = deepcopy(plan_payload())
    if change == "too_few":
        payload["actions"].pop()
    elif change == "sequence":
        payload["actions"][1]["sequence"] = 3
    elif change == "duplicate":
        payload["actions"][1]["action_id"] = payload["actions"][0]["action_id"]
    elif change == "unknown_dependency":
        payload["actions"][1]["dependencies"] = ["ac_missing_action"]
    else:
        payload["actions"][1]["review_date"] = PLANNING_DATE + timedelta(days=59)
    with pytest.raises(ValidationError):
        PublicActionPlan.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "url", "finding_ids": ["fn_one"]},
        {"kind": "query", "url": "https://example.test/", "finding_ids": ["fn_one"]},
        {"kind": "site", "query": "plumber", "finding_ids": ["fn_one"]},
        {"kind": "gbp_field", "gbp_field": "unknown", "finding_ids": ["fn_one"]},
    ],
)
def test_action_target_requires_only_its_matching_value(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ActionTarget.model_validate(payload)


def test_action_target_canonicalizes_finding_ids() -> None:
    target = ActionTarget.model_validate(
        {
            "kind": "url",
            "url": "https://example.test/page",
            "finding_ids": ["fn_two", "fn_one", "fn_two"],
        }
    )
    assert target.finding_ids == ["fn_one", "fn_two"]
