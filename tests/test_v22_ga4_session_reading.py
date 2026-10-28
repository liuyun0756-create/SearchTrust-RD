from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_session_reading import Ga4SessionCommand, Ga4SessionReading, Ga4SessionService, read_sessions, relative_percent, MAX_COUNT
from tests.support.ga4_session_fixtures import session_job
from tests.test_v22_ga4_evidence_review import completed


@pytest.fixture
def anyio_backend(): return "asyncio"


def command(job, **changes):
    return Ga4SessionCommand(**(dict(user_id=job.user_id, case_id=job.case_id, report_id=job.record.report_id,
        delivery_revision_id=job.record.delivery_revision_id, collection_id=job.id) | changes))


@pytest.mark.parametrize("before,after,diff,pct", [(100,125,25,"25.00"), (125,100,-25,"-20.00"),
    (10,10,0,"0.00"), (0,0,0,None), (0,20,20,None), (3,1,-2,"-66.67"), (20000,20001,1,"0.01"),
    (20000,19999,-1,"-0.01"), (MAX_COUNT,MAX_COUNT-1,-1,"0.00"), (1,MAX_COUNT,MAX_COUNT-1,"900719925474099000.00")])
def test_arithmetic_and_old_evidence_unchanged(before, after, diff, pct):
    job = session_job(before, after); original = job.model_dump_json()
    result = read_sessions(job)
    assert (result.before, result.after, result.difference, result.relative_change_percent) == (before, after, diff, pct)
    assert result.state == "available" and result.reasons == []
    assert result.effect is None and not result.causal_attribution
    assert result.processing_completeness == "not_attested" and result.measurement_history == "unverified"
    assert result.observation_checksum == job.collection.qualification.observation_checksum
    assert job.model_dump_json() == original and all(m.state == "not_comparable" for m in job.collection.qualification.metrics)
    assert read_sessions(job) == result


@pytest.mark.parametrize("period", ["previous", "current"])
@pytest.mark.parametrize("view", ["totals", "dates"])
@pytest.mark.parametrize("kind,reason", [("sampling","SAMPLED"), ("subject_to_thresholding","THRESHOLDED"),
    ("data_loss_from_other_row","OTHER_ROW_LOSS"), ("empty_reason","EMPTY_REASON"),
    ("metric_restrictions","SESSIONS_RESTRICTED"), ("truncated","TRUNCATED"), ("truncation","TRUNCATED")])
def test_every_relevant_view_restriction(period, view, kind, reason):
    def mutate(s):
        v = s[period][view]
        if kind == "truncated": v["truncated"] = True
        elif kind == "truncation":
            next(c for c in s["truncation_checks"] if c["period"] == period and c["view"] == view)["reasons"] = [dict(type="FUTURE_UNKNOWN", before_date=None, date_ranges=[])]
        else:
            v["metadata"][kind] = {"sampling": [dict(samples_read_count=1, sampling_space_size=2)],
                "empty_reason": "provider restricted", "metric_restrictions": [dict(metric_name="sessions", restricted_metric_types=[])]}.get(kind, True)
    if kind == "truncated":
        # Totals/dates row limits are already a hard schema failure, not an eligible observation.
        with pytest.raises(ValueError): session_job(mutate=mutate)
        return
    result = read_sessions(session_job(mutate=mutate))
    assert result.state == "withheld" and reason in result.reasons
    assert result.before is result.after is result.difference is result.relative_change_percent is None


@pytest.mark.parametrize("period,view,reason", [("previous","totals","TOTAL_NOT_RETURNED"), ("current","totals","TOTAL_NOT_RETURNED"),
    ("previous","dates","DATES_NOT_RETURNED"), ("current","dates","DATES_NOT_RETURNED")])
def test_missing_rows_are_not_zero(period, view, reason):
    result = read_sessions(session_job(mutate=lambda s: s[period][view]["rows"].pop()))
    assert reason in result.reasons and result.before is None


def test_legacy_withheld_and_daily_counts_not_summed():
    assert read_sessions(completed()).reasons == ["TRUNCATION_NOT_CAPTURED"]
    j = session_job(); assert sum(r.sessions for r in j.collection.snapshot.previous.dates.rows) != 100
    assert read_sessions(j).before == 100


def test_event_only_restrictions_do_not_block_sessions():
    def mutate(s):
        s["current"]["key_events"]["metadata"]["subject_to_thresholding"] = True
        s["truncation_checks"][-1]["reasons"] = [dict(type="FUTURE", before_date=None, date_ranges=[])]
        s["configuration_after"]["events"] = []
        s["previous"]["totals"]["metadata"]["metric_restrictions"] = [dict(metric_name="keyEvents", restricted_metric_types=[])]
    assert read_sessions(session_job(mutate=mutate)).state == "available"


@pytest.mark.parametrize("kind", ["checksum", "promoted", "scope", "window", "dates", "truncation"])
def test_mutated_source_never_treated_as_clean(kind):
    j = session_job()
    if kind == "checksum": j.collection.snapshot.previous.totals.rows[0].sessions += 1
    if kind == "promoted": j.collection.qualification.causal_attribution = True
    if kind == "scope": j.collection.snapshot.scope.hosts = ["other.test"]
    if kind == "window": j.collection.snapshot.windows.after.end = j.collection.snapshot.windows.after.start
    if kind == "dates": j.collection.snapshot.current.dates.rows.append(j.collection.snapshot.current.dates.rows[0])
    if kind == "truncation": j.collection.snapshot.truncation_checks.pop()
    with pytest.raises(ValueError): read_sessions(j)


@pytest.mark.anyio
async def test_read_service_bound_and_only_reads():
    j = session_job(); calls = []
    class Repo:
        async def read_collection(self, **args): calls.append(args); return j
    service = Ga4SessionService(Repo())
    assert (await service.run(command(j))).state == "available"
    for field in ("user_id", "case_id", "report_id", "delivery_revision_id", "collection_id"):
        with pytest.raises(DeterministicJobError): await service.run(command(j, **{field: uuid4()}))
    assert len(calls) == 6


@pytest.mark.parametrize("change", [{"effect": "improved"}, {"causal_attribution": True}, {"before": -1},
    {"before": MAX_COUNT+1}, {"difference": 5}, {"relative_change_percent": "25.01"}, {"state": "withheld"}])
def test_response_cannot_promote_or_forge(change):
    raw = read_sessions(session_job()).model_dump(mode="json") | change
    with pytest.raises(ValueError): Ga4SessionReading.model_validate_json(__import__("json").dumps(raw))


@pytest.mark.anyio
async def test_api_auth_flags_bounds_and_readonly(monkeypatch):
    from app.api.v2.ga4_session_readings import session_service
    from app.core.config import settings
    from app.main import create_app
    j = session_job(); calls = []
    class Repo:
        async def read_collection(self, **kw): calls.append(kw); return j
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "session-test")
    flags = ("V22_GA4_SESSION_SNAPSHOT_READING_ENABLED", "V22_GA4_COMPARISON_COLLECTIONS_ENABLED", "V22_COMPARISONS_ENABLED", "V22_CAPABILITY_EXECUTION_ENABLED")
    for f in flags: monkeypatch.setattr(settings, f, True)
    app = create_app(); app.dependency_overrides[session_service] = lambda: Ga4SessionService(Repo())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v2/ga4-session-readings"; body = command(j).model_dump(mode="json"); headers = {"Authorization": "Bearer session-test"}
        assert (await client.post(url, json=body)).status_code == 401
        r = await client.post(url, json=body, headers=headers)
        assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
        assert (await client.post(url, json=body | {"before": 123}, headers=headers)).status_code == 400
        assert (await client.post(url, content="x"*4001, headers=headers | {"Content-Type": "application/json"})).status_code == 413
        for f in flags:
            monkeypatch.setattr(settings, f, False)
            assert (await client.post(url, json=body, headers=headers)).status_code == 404
            monkeypatch.setattr(settings, f, True)
        assert (await client.get(url, headers=headers)).status_code == 405
    assert len(calls) == 1
