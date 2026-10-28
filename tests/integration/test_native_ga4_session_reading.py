"""Real owner SQL and saved observations; no Google calls during session reads."""
from uuid import uuid4

import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_session_reading import Ga4SessionService
from tests.test_v22_ga4_session_reading import command
from tests.integration.test_native_ga4_collection_storage import ga4_setup, prepared, native_database, request, execute, revise, inspect, ordinary

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(60)]


async def test_native_session_reading_source_binding_immutable_and_no_effects(ga4_setup):
    p = ga4_setup; job = await execute(p, await request(p)); service = Ga4SessionService(p["collections"])
    first = await service.run(command(job))
    assert first.state == "available" and (first.before, first.after, first.difference) == (10,10,0)
    for field in ("user_id", "case_id", "report_id", "delivery_revision_id", "collection_id"):
        with pytest.raises(DeterministicJobError): await service.run(command(job, **{field: uuid4()}))
    # A later implementation record must not reinterpret this saved observation.
    await revise(p)
    assert await service.run(command(job)) == first
    assert (await p["collections"].read_collection(**p["identity"], collection_id=job.id)) == job
    assert len(p["calls"]) == 8 and await ordinary(p) == p["ordinary"]
    assert await inspect(p["db"],p["scenario"]) == p["baseline"]
    assert not first.causal_attribution and first.effect is None
    assert await p["db"].query("select collection_id from capability_ga4_evidence_reviews") == []


async def test_native_session_reading_access_revoked_and_pending(ga4_setup):
    p = ga4_setup; job = await request(p); service = Ga4SessionService(p["collections"])
    with pytest.raises(DeterministicJobError): await service.run(command(job))
    job = await execute(p, job)
    # Saved history requires current Case ownership, not another provider request.
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s", [p["connection"]])
    assert (await service.run(command(job))).state == "available"
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s", [job.case_id])
    with pytest.raises(DeterministicJobError): await service.run(command(job))
    assert len(p["calls"]) == 8


async def test_native_session_reading_restricted_source_is_not_promoted(ga4_setup):
    p = ga4_setup; original = p["provider_handler"]
    def provider(req):
        import httpx
        raw = original(req).json()
        if req.method == "POST": raw["metadata"]["subjectToThresholding"] = True
        return httpx.Response(200, json=raw)
    job = await execute(p, await request(p), handler=provider)
    r = await Ga4SessionService(p["collections"]).run(command(job))
    assert r.state == "withheld" and "THRESHOLDED" in r.reasons and r.before is r.after is r.difference is None
    assert all(m.state == "not_comparable" for m in job.collection.qualification.metrics)
    assert len(p["calls"]) == 8
