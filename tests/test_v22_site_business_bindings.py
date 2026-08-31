from datetime import timedelta
from uuid import UUID

import pytest

from app.jobs_v22.digest import request_digest
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.site_business_facts import build_site_business_facts
from site_business_helpers import request


def test_missing_source_has_no_invented_identity_or_evidence():
    value = request()
    value.source = None
    result = build_site_business_facts(value)
    assert result.source_status.state == "missing" and result.source_status.reason == "no_snapshot"
    assert result.source_snapshot_id is None
    assert result.pages == result.candidates == result.evidence_index == result.source_traces == []


@pytest.mark.parametrize("mutation", [
    lambda r: setattr(r.context, "report_type", "verified_execution"),
    lambda r: setattr(r.source.binding, "case_id", UUID(int=123)),
    lambda r: setattr(r.source.binding, "source_type", "gbp"),
    lambda r: setattr(r.source.binding, "schema_version", "forged"),
    lambda r: setattr(r.source.binding, "payload_checksum", "sha256:" + "0" * 64),
    lambda r: setattr(r.context, "site_url", "https://other.test/"),
    lambda r: setattr(r.source.binding, "fetched_at", r.source.binding.fetched_at + timedelta(seconds=1)),
    lambda r: setattr(r.context, "evaluated_at", r.source.binding.fetched_at - timedelta(seconds=1)),
])
def test_bad_bindings_fail_safely(mutation):
    value = request()
    mutation(value)
    with pytest.raises(SiteBusinessError) as exc:
        build_site_business_facts(value)
    assert exc.value.error_code.startswith("V22_SITE_FACTS_")
    assert "Fixture" not in str(exc.value) + exc.value.user_message


@pytest.mark.parametrize("field,value,reason", [("health_status", "expired", "source_expired"), ("health_status", "unavailable", "source_unavailable"), ("identity_match_status", "mismatch", "source_identity_mismatch")])
def test_source_ineligible_is_not_business_evidence(field, value, reason):
    inputs = request()
    setattr(inputs.source.binding, field, value)
    result = build_site_business_facts(inputs)
    assert result.source_status.reason == reason
    assert result.pages == result.candidates == result.evidence_index == []


@pytest.mark.parametrize("change,reason", [({"html": ""}, "html_missing"), ({"content_type": "application/pdf"}, "content_unsupported"), ({"status_code": 404}, "http_error"), ({"final_url": "https://other.test/"}, "out_of_scope_redirect")])
def test_unusable_deep_page_is_not_observed_absence(change, reason):
    value = request()
    page = value.source.payload.selected_pages[0].deep_snapshot
    for key, item in change.items():
        setattr(page, key, item)
    value.source.binding.payload_checksum = request_digest(value.source.payload)
    result = build_site_business_facts(value)
    assert result.candidates == []
    assert result.pages[0].status == "not_checked"
    assert result.pages[0].diagnostics[0].code == reason
    assert all(f.observation_status == "not_checked" for f in result.pages[0].fields.values())
