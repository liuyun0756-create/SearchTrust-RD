"""Synthetic saved job for pure session arithmetic and contract export."""
import json
from uuid import UUID

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.ga4_collection_storage import Ga4CollectionJob
from app.report_v22.ga4_comparison_models import Ga4ComparisonCollection, Ga4ExactSnapshotV2, qualify_ga4
from tests.support.ga4_comparison_fixtures import fixture, AT
from tests.test_v22_ga4_collection_storage import job_raw


def session_job(before=100, after=125, mutate=None):
    raw = fixture().model_dump(mode="json")
    s = raw["snapshot"]
    s["schema_version"] = "ga4_comparison_observation_v2"
    s["truncation_checks"] = [dict(period=p, view=v, reasons=[]) for p in ("previous", "current") for v in ("totals", "dates", "key_events")]
    for period, count in (("previous", before), ("current", after)):
        s[period]["totals"]["rows"][0]["sessions"] = count
    if mutate:
        mutate(s)
    raw["qualification"] = qualify_ga4(Ga4ExactSnapshotV2.model_validate_json(json.dumps(s))).model_dump(mode="json")
    raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
    collection = Ga4ComparisonCollection.model_validate_json(json.dumps(raw))
    j = job_raw()
    j.update(id=str(UUID(int=20301)), status="succeeded", lease_id=str(UUID(int=9001)), lease_expires_at="2026-12-15T08:05:00Z", completed_at=AT,
        collection=collection.model_dump(mode="json"))
    return Ga4CollectionJob.model_validate_json(json.dumps(j))
