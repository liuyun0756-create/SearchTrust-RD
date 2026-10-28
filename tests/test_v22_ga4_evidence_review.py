"""Read-only evidence distinction and strict independent SEO record contracts."""
from copy import deepcopy
from datetime import datetime
import json
from uuid import uuid4

import httpx
import pytest

from app.google_connections_v22.ga4 import SyncError
from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_collection_storage import Ga4CollectionJob
from app.report_v22.ga4_comparison_models import Ga4ComparisonCollection, Ga4ExactSnapshotV2, qualify_ga4
from app.report_v22.ga4_evidence_audit import audit_ga4
from app.report_v22.ga4_evidence_review import Ga4EvidenceReview, Ga4ReviewCommand, Ga4ReviewInput, SeoCheck
from app.report_v22.ga4_truncation import truncation_check
from tests.support.ga4_comparison_fixtures import fixture, google, AT
from tests.test_v22_ga4_collection_storage import job_raw
from tests.test_v22_ga4_comparison_collection import ledger, collect, anyio_backend


def checks():
    return {k: dict(status="not_checked", note="") for k in ("date_coverage", "event_history", "measurement_setup")}


def completed():
    raw = job_raw()
    raw.update(status="succeeded", lease_id=str(uuid4()), lease_expires_at="2026-12-15T08:05:00Z", completed_at=AT,
        collection=fixture().model_dump(mode="json"))
    return Ga4CollectionJob.model_validate_json(json.dumps(raw))


def command(job, **changes):
    return Ga4ReviewCommand(**(dict(operation="read", user_id=job.user_id, case_id=job.case_id,
        report_id=job.record.report_id, delivery_revision_id=job.record.delivery_revision_id, collection_id=job.id, review=None) | changes))


def test_legacy_hash_and_qualification_unchanged_and_unknown_not_clean():
    old = fixture(); content = old.model_dump_json()
    assert Ga4ComparisonCollection.model_validate_json(content).model_dump_json() == content
    audit = audit_ga4(uuid4(), old.snapshot)
    assert all(v.truncation_capture == "not_captured" and not v.truncation_reasons for v in audit.views)
    assert audit.observation_checksum == old.qualification.observation_checksum
    assert old.qualification.ruleset_version == "v22_ga4_qualification_foundation_v1"
    assert not audit.comparisons_available and audit.processing_completeness == "not_attested"


@pytest.mark.anyio
async def test_provider_truncation_retained_without_raw_messages_or_new_calls(ledger):
    def response(req):
        raw = google(req).json()
        if req.method == "POST":
            raw["metadata"]["dataTruncationReasons"] = [dict(dataTruncationType="DATA_TRUNCATION_TYPE_DATE_RANGE",
                dataTruncationMessage="private-provider-message", dataTruncationDate="2026-08-20",
                dataTruncationDateRanges=[dict(startDate="2026-08-01", endDate="2026-08-19")])]
        return httpx.Response(200, json=raw)
    value = await collect(ledger, response)
    assert isinstance(value.snapshot, Ga4ExactSnapshotV2)
    assert len(value.snapshot.truncation_checks) == 6
    assert "private-provider-message" not in value.model_dump_json()
    assert all("GA4_DATA_TRUNCATION_REPORTED" in m.reasons and m.state == "not_comparable" for m in value.qualification.metrics)
    audit = audit_ga4(uuid4(), value.snapshot)
    assert all(v.truncation_capture == "captured" and len(v.truncation_reasons) == 1 for v in audit.views)
    assert (await ledger.snapshot()).root["ga4_attempts"] == 8


@pytest.mark.parametrize("raw", [None, {}, [None], [dict(dataTruncationType="private bad")],
    [dict(dataTruncationDate="2026-02-30")], [dict(dataTruncationDate=123)],
    [dict(dataTruncationDateRanges=None)], [dict(dataTruncationDateRanges=[{}])],
    [dict(dataTruncationDateRanges=[dict(startDate="2026-02-20", endDate="2026-02-01")])], [{}]*21])
def test_bad_truncation_fails_closed(raw):
    with pytest.raises(ValueError): truncation_check(dict(dataTruncationReasons=raw), period="previous", view="dates")


@pytest.mark.anyio
async def test_bad_provider_metadata_rejected_not_recorded_as_clean(ledger):
    def response(req):
        raw = google(req).json()
        if req.method == "POST": raw["metadata"]["dataTruncationReasons"] = None
        return httpx.Response(200, json=raw)
    with pytest.raises(SyncError): await collect(ledger, response)


@pytest.mark.anyio
async def test_audit_missing_dates_configuration_diffs_and_tamper(ledger):
    value = await collect(ledger)
    raw = value.snapshot.model_dump(mode="json")
    missing = raw["current"]["dates"]["rows"].pop(0)["date"]
    raw["configuration_after"]["events"][0]["counting_method"] = "ONCE_PER_SESSION"
    snapshot = Ga4ExactSnapshotV2.model_validate_json(json.dumps(raw))
    audit = audit_ga4(uuid4(), snapshot)
    assert audit.coverage[1].missing_dates[0].isoformat() == missing
    assert audit.configuration_changes[0].fields == ["counting_method"]
    assert audit.configuration_change_count == 1 and audit.event_history == "unverified"
    raw["truncation_checks"].pop()
    with pytest.raises(ValueError): Ga4ExactSnapshotV2.model_validate_json(json.dumps(raw))
    collection = value.model_dump(mode="json")
    collection["snapshot"]["truncation_checks"][0]["reasons"] = [dict(type="FUTURE_TYPE", date_ranges=[], before_date=None)]
    collection["checksum"] = verified_request_digest({k:v for k,v in collection.items() if k != "checksum"})
    with pytest.raises(ValueError): Ga4ComparisonCollection.model_validate_json(json.dumps(collection))


@pytest.mark.parametrize("raw", [dict(status="verified", note="ok"), dict(status="no_issue_found", note=" "),
    dict(status="issue_found", note=""), dict(status="uncertain", note=""), dict(status="not_checked", note="x"*1201),
    dict(status="not_checked", note="x\x01"), dict(status="not_checked", note="", comparisons_available=True)])
def test_manual_check_cannot_claim_verification_or_omit_basis(raw):
    with pytest.raises(ValueError): SeoCheck.model_validate_json(json.dumps(raw))


@pytest.mark.anyio
async def test_review_service_replay_and_source_binding():
    job = completed(); history = []; calls = []
    class Repo:
        async def read_collection(self, **kw): return job
        async def _rpc(self, name, args):
            calls.append(name)
            if name == "save_v22_ga4_evidence_review":
                history.insert(0, args["p_payload"])
                return json.dumps(history[0])
            return json.dumps(history)
    service = Ga4EvidenceReview(Repo(), clock=lambda: datetime.fromisoformat(AT.replace("Z", "+00:00")))
    first = await service.run(command(job)); assert first.reviews == []
    data = checks(); data["event_history"] = dict(status="no_issue_found", note="Reviewed exported settings; earlier deployment logs not available.")
    review = Ga4ReviewInput(request_id=uuid4(), expected_revision=0, observation_checksum=first.audit.observation_checksum, checks=data)
    saved = await service.run(command(job, operation="save", review=review))
    assert saved.audit == first.audit and saved.saved.source == "seo_recorded" and saved.saved.revision == 1
    assert not saved.audit.comparisons_available and saved.audit.event_history == "unverified"
    for c in (command(job, report_id=uuid4()), command(job, operation="save", review=review.model_copy(update={"observation_checksum":"sha256:"+"0"*64}))):
        with pytest.raises(DeterministicJobError): await service.run(c)
    history[0]["checks"]["event_history"]["status"] = "issue_found"
    with pytest.raises(DeterministicJobError): await service.run(command(job))
    assert set(calls) == {"get_v22_ga4_evidence_reviews", "save_v22_ga4_evidence_review"}
