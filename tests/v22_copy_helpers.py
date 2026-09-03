from __future__ import annotations

from datetime import date

from app.report_v22.action_models import PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan
from app.report_v22.copy_contract import COPY_OUTPUT_KEY, build_copy_request
from app.report_v22.findings import build_public_findings
from app.report_v22.copy_models import CopyRequestV1
from findings_helpers import collection, market, request, site


def public_findings():
    records = [
        {"status_code": 503, "title": "Broken"},
        {"status_code": 200, "title": "Shared", "meta_robots": ["noindex"]},
        {"status_code": 200, "title": "Shared"},
    ]
    serp = market()
    return build_public_findings(request(site(records), serp, collection(serp)))


def public_plan(findings=None):
    findings = findings or public_findings()
    return build_public_action_plan(
        PublicActionPlanInput(
            findings_result=findings,
            planning_date=date(2026, 9, 3),
        )
    )


def copy_request(findings=None, plan=None) -> CopyRequestV1:
    findings = findings or public_findings()
    plan = plan or public_plan(findings)
    return build_copy_request(findings, plan)


def valid_response(request_value: CopyRequestV1 | None = None) -> dict:
    request_value = request_value or copy_request()
    actions = []
    for action in request_value.actions:
        why_pattern = action.why_now_patterns[0]
        explanation_pattern = action.explanation_patterns[0]
        by_type: dict[str, list[str]] = {}
        for fact in action.facts:
            by_type.setdefault(fact.fact_type, []).append(fact.fact_id)

        why_bindings = {}
        for slot in why_pattern.slots:
            choices = [
                fact_id
                for fact_type in slot.allowed_fact_types
                for fact_id in by_type.get(fact_type, [])
            ]
            why_bindings[slot.slot_name] = choices[: slot.min_items]

        explanation_bindings = {}
        for slot in explanation_pattern.slots:
            choices = [
                fact_id
                for fact_type in slot.allowed_fact_types
                for fact_id in by_type.get(fact_type, [])
            ]
            explanation_bindings[slot.slot_name] = choices

        actions.append(
            {
                "action_id": action.action_id,
                "sequence": action.sequence,
                "why_now": {
                    "pattern_key": why_pattern.pattern_key,
                    "slot_bindings": why_bindings,
                },
                "client_facing_explanation": {
                    "pattern_key": explanation_pattern.pattern_key,
                    "slot_bindings": explanation_bindings,
                },
            }
        )
    return {
        COPY_OUTPUT_KEY: {
            "schema_version": "v22_copy_response_v1",
            "copy_contract_version": request_value.copy_contract_version,
            "copy_catalog_version": request_value.copy_catalog_version,
            "language": request_value.language,
            "action_plan_checksum": request_value.action_plan_checksum,
            "actions": actions,
        }
    }
