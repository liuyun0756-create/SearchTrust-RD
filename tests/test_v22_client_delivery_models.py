from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from app.report_v22.models import ClientDelivery


def valid_payload() -> dict:
    action_ids = ["ac_first_action", "ac_second_action", "ac_third_action"]
    periods = ["days_1_30", "days_31_60", "days_61_90"]
    review_dates = [date(2026, 10, 18), date(2026, 11, 17), date(2026, 12, 17)]
    return {
        "decision": {
            "headline": "Repair the visibility foundation before expanding content.",
            "business_impact": "Important service pages are not consistently available to searchers.",
            "opportunity": "Fixing the current foundation creates a clearer basis for future growth work.",
        },
        "evidence_cards": [
            {
                "source_label": "Website",
                "subject_label": "Priority service page",
                "observation": "A checked service page is currently excluded from search indexing.",
                "decision_relevance": "The page should be made eligible before new content is added.",
                "finding_ids": ["fn_supported_finding"],
                "evidence_ids": ["ev_supported_evidence"],
            }
        ],
        "priority_actions": [
            {
                "action_id": action_id,
                "sequence": index,
                "title": f"Priority action {index}",
                "why_now": "The checked evidence makes this the next practical step.",
                "expected_result": "A new check confirms the agreed condition has been corrected.",
                "effort_bucket": "medium",
                "review_date": review_dates[index - 1],
                "required_client_assets": ["Confirm the intended page purpose."],
            }
            for index, action_id in enumerate(action_ids, start=1)
        ],
        "roadmap": [
            {
                "period": period,
                "objective": f"Complete priority action {index}.",
                "expected_result": "The agreed outcome is confirmed with a new check.",
                "action_ids": [action_ids[index - 1]],
            }
            for index, period in enumerate(periods, start=1)
        ],
        "coverage_appendix": {
            "checked_sources": ["Public website", "Public search results"],
            "unavailable_sources": ["Authorized analytics"],
            "boundary_summary": "This report uses public evidence and does not claim private performance outcomes.",
        },
        "next_review_date": review_dates[0],
    }


def test_client_delivery_accepts_the_bounded_structure() -> None:
    delivery = ClientDelivery.model_validate(valid_payload())

    assert len(delivery.evidence_cards) == 1
    assert [item.sequence for item in delivery.priority_actions] == [1, 2, 3]
    assert [item.period for item in delivery.roadmap] == [
        "days_1_30",
        "days_31_60",
        "days_61_90",
    ]


@pytest.mark.parametrize("count", [0, 4])
def test_client_delivery_requires_one_to_three_evidence_cards(count: int) -> None:
    payload = valid_payload()
    payload["evidence_cards"] = [deepcopy(payload["evidence_cards"][0]) for _ in range(count)]
    for index, card in enumerate(payload["evidence_cards"]):
        card["finding_ids"] = [f"fn_supported_{index}"]
        card["evidence_ids"] = [f"ev_supported_{index}"]

    with pytest.raises(ValidationError):
        ClientDelivery.model_validate(payload)


def test_client_evidence_card_limits_references() -> None:
    payload = valid_payload()
    payload["evidence_cards"][0]["finding_ids"] = [
        "fn_supported_one",
        "fn_supported_two",
        "fn_supported_three",
    ]

    with pytest.raises(ValidationError, match="at most 2 items"):
        ClientDelivery.model_validate(payload)


def test_client_priority_actions_must_be_canonical() -> None:
    payload = valid_payload()
    payload["priority_actions"][1]["sequence"] = 3

    with pytest.raises(ValidationError, match="sequences 1, 2, 3"):
        ClientDelivery.model_validate(payload)


def test_client_roadmap_must_reference_actions_in_order() -> None:
    payload = valid_payload()
    payload["roadmap"][0]["action_ids"] = ["ac_second_action"]

    with pytest.raises(ValidationError, match="every priority action in order"):
        ClientDelivery.model_validate(payload)


def test_client_next_review_must_match_first_action() -> None:
    payload = valid_payload()
    payload["next_review_date"] = date(2026, 10, 19)

    with pytest.raises(ValidationError, match="first priority action"):
        ClientDelivery.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("decision", "headline"), "x" * 161),
        (("decision", "business_impact"), "x" * 321),
        (("evidence_cards", 0, "source_label"), "x" * 41),
        (("evidence_cards", 0, "observation"), "x" * 241),
        (("priority_actions", 0, "title"), "x" * 121),
        (("roadmap", 0, "objective"), "x" * 181),
        (("coverage_appendix", "boundary_summary"), "x" * 321),
    ],
)
def test_client_delivery_enforces_display_text_limits(path: tuple, value: str) -> None:
    payload = valid_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        ClientDelivery.model_validate(payload)
