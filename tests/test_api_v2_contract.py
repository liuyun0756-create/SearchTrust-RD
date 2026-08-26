from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v2.models import (
    AnalyzeRequest,
    ApiV2ContractBundle,
    PreflightRequest,
    TaskStatusResponse,
)


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "v2.2"


def prospect_report() -> dict:
    return json.loads((CONTRACT_DIR / "fixtures" / "prospect.json").read_text(encoding="utf-8"))


def prospect_analyze_payload() -> dict:
    report = prospect_report()
    competitors = report["competitor_analysis"]["competitors"]
    return {
        "case_id": report["identity"]["case_id"],
        "report_type": "prospect",
        "business_identity": report["identity"]["business"],
        "primary_service": report["case_context"]["primary_service"],
        "target_market": report["case_context"]["target_market"],
        "queries": report["case_context"]["queries"],
        "competitors": [
            {
                "competitor_id": competitor["competitor_id"],
                "business_name": competitor["business_name"],
                "website_url": competitor["website_url"],
                "public_gbp_url": competitor["public_gbp_url"],
                "confirmation_source": "user",
            }
            for competitor in competitors
        ],
        "first_party_snapshots": [],
        "parent_report": None,
        "generation_limits": {
            "max_site_urls": 500,
            "max_deep_pages": 50,
            "max_competitor_pages_each": 10,
            "competitor_count": 3,
            "max_pagespeed_pages": 5,
            "max_review_samples_each": 30,
        },
    }


def validate_analyze(payload: dict) -> AnalyzeRequest:
    return AnalyzeRequest.model_validate_json(json.dumps(payload))


def test_prospect_analyze_request_validates() -> None:
    request = validate_analyze(prospect_analyze_payload())
    assert request.report_type == "prospect"
    assert len(request.competitors) == 3


@pytest.mark.parametrize("token_field", ["access_token", "refresh_token", "google_token"])
def test_analyze_contract_rejects_google_tokens(token_field: str) -> None:
    payload = prospect_analyze_payload()
    payload[token_field] = "must-not-cross-the-service-boundary"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_analyze(payload)


def test_verified_analyze_requires_all_source_snapshots() -> None:
    payload = prospect_analyze_payload()
    payload["report_type"] = "verified_execution"
    payload["parent_report"] = prospect_report()
    with pytest.raises(ValidationError, match="requires GSC, GBP, and GA4 snapshots"):
        validate_analyze(payload)


def test_preflight_contract_rejects_report_conclusions() -> None:
    payload = {"site_url": "https://example-plumbing.test", "finding": "not allowed"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PreflightRequest.model_validate_json(json.dumps(payload))


def test_succeeded_task_requires_completed_report() -> None:
    payload = {
        "job_id": "55555555-5555-4555-8555-555555555555",
        "status": "succeeded",
        "stage": "completed",
        "progress": 100,
        "message": "Complete",
        "report": None,
        "error": None,
        "created_at": "2026-08-26T08:00:00Z",
        "updated_at": "2026-08-26T08:10:00Z",
    }
    with pytest.raises(ValidationError, match="succeeded jobs require a completed report"):
        TaskStatusResponse.model_validate_json(json.dumps(payload))


def test_api_schema_contains_all_endpoint_models_and_no_token_fields() -> None:
    schema = ApiV2ContractBundle.model_json_schema(mode="serialization")
    definitions = schema.get("$defs", {})
    for model_name in (
        "PreflightRequest",
        "PreflightResponse",
        "AnalyzeRequest",
        "TaskCreateResponse",
        "TaskStatusResponse",
        "RetryTaskResponse",
    ):
        assert model_name in definitions
    serialized = json.dumps(schema)
    assert "refresh_token" not in serialized
    assert "access_token" not in serialized
