from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.jobs_v22.digest import request_digest
from app.report_v22.first_party_findings_models import (
    FirstPartyFindingsInput,
    TrustedFirstPartySnapshot,
)


CASE_ID = UUID("10000000-0000-4000-8000-000000000001")
PARENT_ID = UUID("10000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def snapshot(source: str, number: int) -> dict:
    payload = {"schema_version": f"{source}_sync_v1", "resource_id": f"resource-{source}"}
    raw = {"content": source} if source == "gbp" else None
    checksum = request_digest(raw if raw is not None else payload)
    return {
        "snapshot_id": UUID(f"10000000-0000-4000-8000-{number:012d}"),
        "case_id": CASE_ID,
        "binding_id": UUID(f"20000000-0000-4000-8000-{number:012d}"),
        "source_type": source,
        "schema_version": f"{source}_sync_v1",
        "fetched_at": NOW,
        "expires_at": NOW + timedelta(days=7 if source != "gbp" else 30),
        "identity_match_status": "matched",
        "health_status": "healthy",
        "health_reasons": [],
        "normalized_payload": payload,
        "raw_payload": raw,
        "payload_checksum": checksum,
        "external_resource_id": f"resource-{source}",
        "coverage_start": date(2026, 3, 13),
        "coverage_end": date(2026, 9, 8),
    }


def request_payload(*sources: str) -> dict:
    return {
        "case_id": CASE_ID,
        "parent_report_id": PARENT_ID,
        "evaluated_at": NOW,
        "snapshots": [snapshot(source, index + 10) for index, source in enumerate(sources)],
    }


def test_input_requires_unique_gsc_and_ga4_sources() -> None:
    value = FirstPartyFindingsInput.model_validate(request_payload("gsc", "ga4"))
    assert [item.source_type for item in value.snapshots] == ["gsc", "ga4"]

    with pytest.raises(ValidationError, match="GSC and GA4"):
        FirstPartyFindingsInput.model_validate(request_payload("gsc", "gbp"))

    duplicate = request_payload("gsc", "ga4", "gbp")
    duplicate["snapshots"][2] = deepcopy(duplicate["snapshots"][0])
    duplicate["snapshots"][2]["snapshot_id"] = UUID("10000000-0000-4000-8000-000000000099")
    with pytest.raises(ValidationError, match="GSC and GA4"):
        FirstPartyFindingsInput.model_validate(duplicate)


def test_snapshot_source_schema_and_temporary_content_are_strict() -> None:
    invalid = snapshot("gsc", 1)
    invalid["schema_version"] = "ga4_sync_v1"
    with pytest.raises(ValidationError, match="source schema mismatch"):
        TrustedFirstPartySnapshot.model_validate(invalid)

    invalid = snapshot("ga4", 2)
    invalid["raw_payload"] = {"forged": True}
    with pytest.raises(ValidationError, match="only GBP"):
        TrustedFirstPartySnapshot.model_validate(invalid)

    invalid = snapshot("gbp", 3)
    invalid["raw_payload"] = None
    with pytest.raises(ValidationError, match="GBP raw content"):
        TrustedFirstPartySnapshot.model_validate(invalid)


def test_input_rejects_cross_case_and_nonfinite_content() -> None:
    invalid = request_payload("gsc", "ga4")
    invalid["snapshots"][0]["case_id"] = UUID("90000000-0000-4000-8000-000000000009")
    with pytest.raises(ValidationError, match="another Case"):
        FirstPartyFindingsInput.model_validate(invalid)

    invalid = request_payload("gsc", "ga4")
    invalid["snapshots"][0]["normalized_payload"]["bad"] = float("nan")
    with pytest.raises(ValidationError, match="nonfinite"):
        FirstPartyFindingsInput.model_validate(invalid)


def test_input_rejects_duplicate_ids_and_pre_fetch_evaluation() -> None:
    invalid = request_payload("gsc", "ga4")
    invalid["snapshots"][1]["snapshot_id"] = invalid["snapshots"][0]["snapshot_id"]
    with pytest.raises(ValidationError, match="snapshot identities"):
        FirstPartyFindingsInput.model_validate(invalid)

    invalid = request_payload("gsc", "ga4")
    invalid["evaluated_at"] = NOW - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="before it was fetched"):
        FirstPartyFindingsInput.model_validate(invalid)
