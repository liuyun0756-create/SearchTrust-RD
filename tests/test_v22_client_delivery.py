from __future__ import annotations

from pathlib import Path

import pytest

from app.report_v22.client_delivery import (
    ClientDeliveryError,
    assert_client_delivery_safe,
    build_client_delivery,
)
from app.report_v22.client_delivery_catalog import CLIENT_DELIVERY_TEMPLATES
from app.report_v22.execution_plan_catalog import (
    MEASUREMENT_PRESENTATION,
    PUBLIC_PRESENTATION,
)
from app.report_v22.models import ClientDelivery, ReportV22


ROOT = Path(__file__).resolve().parents[1]


def test_client_delivery_catalog_covers_every_supported_action_template() -> None:
    supported = set(PUBLIC_PRESENTATION) | set(MEASUREMENT_PRESENTATION)

    assert set(CLIENT_DELIVERY_TEMPLATES) == supported


def _prospect_report() -> ReportV22:
    return ReportV22.model_validate_json(
        (ROOT / "contracts/v2.2/fixtures/prospect.json").read_text(encoding="utf-8")
    )


def _build(report: ReportV22) -> ClientDelivery:
    return build_client_delivery(
        report_type=report.report_version.report_type,
        template_keys=[
            "close_page_type_gap",
            "align_public_gbp",
            "review_market_visibility",
        ],
        top_actions=report.top_actions,
        findings=report.findings,
        evidence_index=report.evidence_index,
        data_coverage=report.data_coverage,
    )


def test_build_client_delivery_is_deterministic_and_fully_structured() -> None:
    report = _prospect_report()

    first = _build(report)
    second = _build(report)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.priority_actions) == 3
    assert [action.sequence for action in first.priority_actions] == [1, 2, 3]
    assert len(first.evidence_cards) == 3
    assert [phase.period for phase in first.roadmap] == [
        "days_1_30",
        "days_31_60",
        "days_61_90",
    ]
    assert first.next_review_date == report.top_actions[0].review_date


def test_client_delivery_display_copy_excludes_urls_and_internal_ids() -> None:
    delivery = _build(_prospect_report())
    display_payload = {
        "decision": delivery.decision.model_dump(mode="json"),
        "actions": [
            {
                "title": action.title,
                "why_now": action.why_now,
                "expected_result": action.expected_result,
                "required_client_assets": action.required_client_assets,
            }
            for action in delivery.priority_actions
        ],
        "cards": [
            {
                "source_label": card.source_label,
                "subject_label": card.subject_label,
                "observation": card.observation,
                "decision_relevance": card.decision_relevance,
            }
            for card in delivery.evidence_cards
        ],
    }
    rendered = str(display_payload)

    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "fn_" not in rendered
    assert "ev_" not in rendered


def test_client_delivery_fails_closed_for_unknown_template() -> None:
    report = _prospect_report()

    with pytest.raises(ClientDeliveryError, match="unsupported client delivery template"):
        build_client_delivery(
            report_type="prospect",
            template_keys=["close_page_type_gap", "unknown", "review_market_visibility"],
            top_actions=report.top_actions,
            findings=report.findings,
            evidence_index=report.evidence_index,
            data_coverage=report.data_coverage,
        )


def test_client_delivery_fails_when_no_healthy_evidence_is_available() -> None:
    report = _prospect_report()
    unhealthy = [
        item.model_copy(update={"health_status": "error"})
        for item in report.evidence_index
    ]

    with pytest.raises(ClientDeliveryError, match="no healthy representative evidence"):
        build_client_delivery(
            report_type="prospect",
            template_keys=[
                "close_page_type_gap",
                "align_public_gbp",
                "review_market_visibility",
            ],
            top_actions=report.top_actions,
            findings=report.findings,
            evidence_index=unhealthy,
            data_coverage=report.data_coverage,
        )


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Read https://example.com/details",
        "Inspect fn_internal_rule",
        "Provider code SOURCE_RATE_LIMIT",
        "This will increase rankings",
    ],
)
def test_client_delivery_safety_scan_rejects_technical_leakage_and_promises(
    unsafe_text: str,
) -> None:
    delivery = _build(_prospect_report())
    unsafe = delivery.model_copy(
        update={
            "decision": delivery.decision.model_copy(
                update={"business_impact": unsafe_text}
            )
        }
    )

    with pytest.raises(ClientDeliveryError, match="unsafe display copy"):
        assert_client_delivery_safe(unsafe)
