"""Owner checks, append-only history and CAS on real disposable PostgreSQL."""
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.report_v22.ga4_comparison_models import Ga4ExactSnapshot, qualify_ga4, Ga4ComparisonCollection
from app.report_v22.ga4_evidence_review import Ga4EvidenceReview, Ga4ReviewCommand, Ga4ReviewInput
from tests.integration.test_native_ga4_collection_storage import ga4_setup, prepared, native_database, request, execute, ordinary, inspect, material
from tests.test_v22_ga4_evidence_review import checks

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(60)]


def command(p, job, **changes):
    return Ga4ReviewCommand(**(dict(operation="read", **p["identity"], report_id=UUID(p["record"]["report_id"]),
        delivery_revision_id=UUID(p["record"]["delivery_revision_id"]), collection_id=job.id, review=None) | changes))


async def test_native_ga4_review_revisions_retry_conflict_and_no_side_effects(ga4_setup):
    p = ga4_setup; job = await execute(p, await request(p)); assert job.status == "succeeded"
    service = Ga4EvidenceReview(p["collections"])
    start = await service.run(command(p, job)); assert start.reviews == []
    assert all(v.truncation_capture == "captured" for v in start.audit.views)
    review = Ga4ReviewInput(request_id=uuid4(), expected_revision=0, observation_checksum=start.audit.observation_checksum, checks=checks())
    save = command(p, job, operation="save", review=review)
    results = await asyncio.gather(*(service.run(save) for _ in range(4)))
    assert all(r.saved == results[0].saved for r in results)
    assert results[0].saved.revision == 1
    different = review.model_copy(update={"request_id": uuid4()})
    with pytest.raises(DeterministicJobError): await service.run(command(p, job, operation="save", review=different))
    next_review = different.model_copy(update={"expected_revision": 1})
    second = await service.run(command(p, job, operation="save", review=next_review))
    replay = await service.run(save)
    assert replay.saved == results[0].saved and replay.reviews == second.reviews and replay.reviews[0].revision == 2
    assert replay.audit == start.audit and not replay.audit.comparisons_available
    for changes in (dict(user_id=uuid4()), dict(case_id=uuid4()), dict(report_id=uuid4()), dict(delivery_revision_id=uuid4()), dict(collection_id=uuid4())):
        with pytest.raises(DeterministicJobError): await service.run(command(p, job, **changes))
    changed = review.model_copy(deep=True); changed.checks.date_coverage.note = "different body"
    with pytest.raises(DeterministicJobError): await service.run(command(p, job, operation="save", review=changed))
    for sql in ("update capability_ga4_evidence_reviews set revision=99 where collection_id=%s", "delete from capability_ga4_evidence_reviews where collection_id=%s"):
        with pytest.raises(Exception): await p["db"].query(sql, [job.id])
    rows = await p["db"].query("select has_function_privilege('authenticated','save_v22_ga4_evidence_review(uuid,uuid,uuid,uuid,uuid,integer,jsonb,text)','EXECUTE') allowed, has_table_privilege('service_role','capability_ga4_evidence_reviews','INSERT') direct")
    assert not rows[0]["allowed"] and not rows[0]["direct"]
    assert len(p["calls"]) == 8 and await ordinary(p) == p["ordinary"] and await inspect(p["db"], p["scenario"]) == p["baseline"]
    assert (await p["collections"].read_collection(**p["identity"], collection_id=job.id)) == job


async def test_native_ga4_review_api_gate_and_persisted_truncation(ga4_setup, monkeypatch):
    p = ga4_setup
    original = p["provider_handler"]
    def provider(req):
        raw = original(req).json()
        if req.method == "POST": raw["metadata"]["dataTruncationReasons"] = [dict(dataTruncationType="DATA_TRUNCATION_TYPE_PROPERTY")]
        return httpx.Response(200, json=raw)
    job = await execute(p, await request(p), handler=provider)
    assert job.status == "succeeded"
    from app.api.v2.ga4_evidence_reviews import review_service
    from app.core.config import settings
    from app.main import create_app
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "native-review")
    for flag in ("V22_GA4_COMPARISON_COLLECTIONS_ENABLED", "V22_COMPARISONS_ENABLED", "V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings, flag, True)
    app = create_app(); app.dependency_overrides[review_service] = lambda: Ga4EvidenceReview(p["collections"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def call(c, token="native-review"):
            return await client.post("/api/v2/ga4-evidence-reviews", json=c.model_dump(mode="json"), headers={"Authorization": f"Bearer {token}"})
        c = command(p, job)
        assert (await call(c, "wrong")).status_code == 401
        response = await call(c); assert response.status_code == 200, response.text
        data = response.json()
        assert all(v["truncation_reasons"] for v in data["audit"]["views"])
        edit = Ga4ReviewInput(request_id=uuid4(), expected_revision=0, observation_checksum=data["audit"]["observation_checksum"], checks=checks())
        assert (await call(command(p, job, operation="save", review=edit))).status_code == 200
        monkeypatch.setattr(settings, "V22_GA4_COMPARISON_COLLECTIONS_ENABLED", False)
        assert (await call(c)).status_code == 404
    assert len(p["calls"]) == 8


async def test_native_legacy_observation_and_bounded_history_preserve_old_receipt(ga4_setup):
    p = ga4_setup; job = await request(p); job = await p["collections"].claim_collection(job.id)
    value = (await material(p, job)).model_dump(mode="json")
    value["snapshot"].pop("truncation_checks"); value["snapshot"]["schema_version"] = "ga4_comparison_observation_v1"
    snapshot = Ga4ExactSnapshot.model_validate_json(json.dumps(value["snapshot"]))
    value["qualification"] = qualify_ga4(snapshot).model_dump(mode="json")
    value["checksum"] = verified_request_digest({k:v for k,v in value.items() if k != "checksum"})
    old = Ga4ComparisonCollection.model_validate_json(json.dumps(value))
    job = await p["collections"].finish_collection(job, old)
    service = Ga4EvidenceReview(p["collections"]); view = await service.run(command(p, job))
    assert all(v.truncation_capture == "not_captured" for v in view.audit.views)
    first = None
    for i in range(21):
        review = Ga4ReviewInput(request_id=uuid4(), expected_revision=i, observation_checksum=view.audit.observation_checksum, checks=checks())
        c = command(p, job, operation="save", review=review)
        if first is None: first = c
        result = await service.run(c)
    assert len(result.reviews) == 20 and result.reviews[-1].revision == 2
    replay = await service.run(first)
    assert replay.saved.revision == 1 and replay.reviews[0].revision == 21
    assert (await p["collections"].read_collection(**p["identity"], collection_id=job.id)).collection == old
    assert len(p["calls"]) == 8


async def test_native_sql_refuses_forged_manual_record_and_pending_collection(ga4_setup):
    p = ga4_setup; job = await request(p); service = Ga4EvidenceReview(p["collections"])
    with pytest.raises(DeterministicJobError): await service.run(command(p, job))
    job = await execute(p, job); view = await service.run(command(p, job))
    review = Ga4ReviewInput(request_id=uuid4(), expected_revision=0, observation_checksum=view.audit.observation_checksum, checks=checks())
    saved = (await service.run(command(p, job, operation="save", review=review))).saved
    for change in ("actor", "evidence", "status", "basis", "promote", "digest", "source"):
        raw = saved.model_dump(mode="json", exclude={"checksum"}); raw.update(request_id=str(uuid4()), revision=2)
        if change == "actor": raw["user_id"] = str(uuid4())
        if change == "evidence": raw["observation_checksum"] = "sha256:"+"0"*64
        if change == "status": raw["checks"]["event_history"]["status"] = "verified"
        if change == "basis": raw["checks"]["event_history"]["status"] = "no_issue_found"
        if change == "promote": raw["comparisons_available"] = True
        if change == "source": raw["source"] = "system_verified"
        canonical = verified_canonical_json_bytes(raw).decode(); raw["checksum"] = verified_request_digest(raw)
        if change == "digest": raw["checksum"] = "sha256:"+"0"*64
        with pytest.raises(Exception, match="GA4_REVIEW_INVALID"):
            await p["db"].rpc("save_v22_ga4_evidence_review", dict(p_user_id=str(job.user_id), p_case_id=str(job.case_id),
                p_report_id=str(job.record.report_id), p_delivery_revision_id=str(job.record.delivery_revision_id), p_collection_id=str(job.id),
                p_expected_revision=1, p_payload=raw, p_canonical_content=canonical))
    assert len((await service.run(command(p, job))).reviews) == 1
