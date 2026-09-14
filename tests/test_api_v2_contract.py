from __future__ import annotations

import json
import subprocess
import sys
from itertools import combinations
from pathlib import Path
from uuid import UUID

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


def verified_snapshots(*source_types: str) -> list[dict]:
    fixture_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "report_v22_evidence"
        / "inputs"
        / "verified.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    snapshots = {
        source["payload"]["source_type"]: source["payload"]
        for source in fixture["sources"]
        if source["kind"] == "first_party"
    }
    return [snapshots[source_type] for source_type in source_types]


def validate_analyze(payload: dict) -> AnalyzeRequest:
    return AnalyzeRequest.model_validate_json(json.dumps(payload))


def test_prospect_analyze_request_validates() -> None:
    request = validate_analyze(prospect_analyze_payload())
    assert request.report_type == "prospect"
    assert len(request.competitors) == 3


@pytest.mark.parametrize("competitor_count", [1, 2, 3])
def test_analyze_request_accepts_one_to_three_competitors(competitor_count: int) -> None:
    payload = prospect_analyze_payload()
    payload["competitors"] = payload["competitors"][:competitor_count]
    payload["generation_limits"]["competitor_count"] = competitor_count

    request = validate_analyze(payload)

    assert len(request.competitors) == competitor_count
    assert request.generation_limits.competitor_count == competitor_count


@pytest.mark.parametrize("competitor_count", [0, 4])
def test_analyze_request_rejects_competitor_counts_outside_one_to_three(
    competitor_count: int,
) -> None:
    payload = prospect_analyze_payload()
    competitor = payload["competitors"][0]
    payload["competitors"] = (
        []
        if competitor_count == 0
        else [
            {
                **competitor,
                "competitor_id": f"cp_boundary_{index}",
                "business_name": f"Boundary Competitor {index}",
                "website_url": f"https://boundary-{index}.example",
            }
            for index in range(competitor_count)
        ]
    )
    payload["generation_limits"]["competitor_count"] = max(1, competitor_count)

    with pytest.raises(ValidationError):
        validate_analyze(payload)


def test_analyze_request_requires_generation_limit_to_match_selected_competitors() -> None:
    payload = prospect_analyze_payload()
    payload["competitors"] = payload["competitors"][:1]

    with pytest.raises(ValidationError, match="competitor count must match"):
        validate_analyze(payload)


@pytest.mark.parametrize("token_field", ["access_token", "refresh_token", "google_token"])
def test_analyze_contract_rejects_google_tokens(token_field: str) -> None:
    payload = prospect_analyze_payload()
    payload[token_field] = "must-not-cross-the-service-boundary"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_analyze(payload)


def test_verified_analyze_requires_core_source_snapshots() -> None:
    payload = prospect_analyze_payload()
    payload["report_type"] = "verified_execution"
    payload["parent_report"] = prospect_report()
    payload["first_party_snapshots"] = verified_snapshots("gsc")
    with pytest.raises(ValidationError, match="requires GSC and GA4 snapshots"):
        validate_analyze(payload)


def test_verified_analyze_accepts_gsc_and_ga4_without_official_gbp() -> None:
    payload = prospect_analyze_payload()
    payload["report_type"] = "verified_execution"
    payload["parent_report"] = prospect_report()
    payload["first_party_snapshots"] = verified_snapshots("gsc", "ga4")

    request = validate_analyze(payload)

    assert [snapshot.source_type for snapshot in request.first_party_snapshots] == ["gsc", "ga4"]


def test_verified_analyze_accepts_optional_official_gbp() -> None:
    payload = prospect_analyze_payload()
    payload["report_type"] = "verified_execution"
    payload["parent_report"] = prospect_report()
    payload["first_party_snapshots"] = verified_snapshots("gsc", "gbp", "ga4")

    assert validate_analyze(payload).report_type == "verified_execution"


def test_preflight_contract_rejects_report_conclusions() -> None:
    payload = {"site_url": "https://example-plumbing.test", "finding": "not allowed"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PreflightRequest.model_validate_json(json.dumps(payload))


def test_succeeded_task_requires_completed_report() -> None:
    payload = {
        "job_id": "55555555-5555-4555-8555-555555555555",
        "revision": 3,
        "run_generation": 1,
        "status": "succeeded",
        "stage": "completed",
        "progress": 100,
        "message": "Complete",
        "report": None,
        "error": None,
        "created_at": "2026-08-26T08:00:00Z",
        "deadline_at": "2026-08-26T08:20:00Z",
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
        "VerifiedTaskRequest",
        "TaskCreateResponse",
        "TaskStatusResponse",
        "RetryTaskResponse",
    ):
        assert model_name in definitions
    serialized = json.dumps(schema)
    assert "refresh_token" not in serialized
    assert "access_token" not in serialized


def verified_task_payload() -> dict:
    return {
        "schema_version": "v22_verified_task_request_v1",
        "case_id": "11111111-1111-4111-8111-111111111111",
        "parent_report_id": "22222222-2222-4222-8222-222222222222",
        "gsc_snapshot_id": "33333333-3333-4333-8333-333333333333",
        "ga4_snapshot_id": "44444444-4444-4444-8444-444444444444",
        "public_gbp_snapshot_id": "66666666-6666-4666-8666-666666666666",
        "input_checksum": "sha256:" + "a" * 64,
    }


def verified_request_model():
    from app.api.v2 import models

    model = getattr(models, "VerifiedTaskRequest", None)
    assert model is not None, "VerifiedTaskRequest must be exported by API v2 models"
    return model


def test_verified_task_request_accepts_only_bound_source_references() -> None:
    model = verified_request_model()
    payload = verified_task_payload()
    request = model.model_validate_json(json.dumps(payload))

    assert request.model_dump(mode="json") == payload
    assert request.case_id == UUID(payload["case_id"])
    payload.pop("schema_version")
    assert model.model_validate_json(json.dumps(payload)).schema_version == "v22_verified_task_request_v1"
    with pytest.raises(ValidationError, match="instance of UUID"):
        model.model_validate(payload)


@pytest.mark.parametrize("left,right", list(combinations([
    "parent_report_id", "gsc_snapshot_id", "ga4_snapshot_id", "public_gbp_snapshot_id",
], 2)))
def test_verified_task_request_rejects_duplicate_source_identities(left: str, right: str) -> None:
    model = verified_request_model()
    payload = verified_task_payload()
    payload[right] = payload[left]
    with pytest.raises(ValidationError, match="distinct"):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("checksum", ["a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63, "md5:abcd", 123])
def test_verified_task_request_rejects_invalid_checksums(checksum) -> None:
    model = verified_request_model()
    payload = {**verified_task_payload(), "input_checksum": checksum}
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["job_id", "parent_report", "first_party_snapshots", "access_token"])
def test_verified_task_request_rejects_extra_fields(field: str) -> None:
    model = verified_request_model()
    payload = {**verified_task_payload(), field: "untrusted"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["case_id", "parent_report_id", "gsc_snapshot_id", "ga4_snapshot_id", "public_gbp_snapshot_id", "input_checksum"])
def test_verified_task_request_requires_all_bindings(field: str) -> None:
    model = verified_request_model()
    payload = verified_task_payload()
    payload.pop(field)
    with pytest.raises(ValidationError, match="Field required"):
        model.model_validate_json(json.dumps(payload))


def test_verified_task_request_rejects_wrong_schema_version() -> None:
    model = verified_request_model()
    payload = {**verified_task_payload(), "schema_version": "v21_verified_task_request_v1"}
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("first_import", ["app.api.v2.models", "app.jobs_v22.verified_models", "app.jobs_v22.models"])
def test_verified_models_import_without_package_cycles(first_import: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first_import}; "
         "from app.jobs_v22 import JobState, JobErrorState; "
         "from app.jobs_v22.models import JobState as DirectJobState, JobErrorState as DirectJobErrorState; "
         "assert JobState is DirectJobState; assert JobErrorState is DirectJobErrorState"],
        cwd=CONTRACT_DIR.parents[1], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
