"""Exact-window material is synthetic; these tests never contact Google."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import fakeredis.aioredis
import httpx
import pytest

from app.google_connections_v22.gsc import GscProvider, SyncError, evaluate_health
from app.jobs_v22.cost_ledger import JobCostLedger, CostLimitExceeded
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.digest import request_digest, verified_request_digest, verified_canonical_json_bytes
from app.report_v22.comparison_collection import prepare_gsc_comparison, collect_gsc_comparison, GscComparisonCollection, GscComparisonPlan
from app.report_v22.comparison import compare_implementation
from app.report_v22.implementation_records import ImplementationRecord
from support.comparison_fixtures import request_raw, parsed

AT = "2026-12-15T08:00:00Z"
RESOURCE = "sc-domain:example.test"
BINDING = UUID(int=20003)

@pytest.fixture
def anyio_backend(): return "asyncio"

def record(**entry_changes):
    raw = deepcopy(request_raw()["implementation"])
    raw["entries"][0].update(entry_changes)
    raw["checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "checksum"})
    return ImplementationRecord.model_validate_json(verified_canonical_json_bytes(raw))

def options(**changes):
    return dict(action_id="ac-1",days=28,binding_id=BINDING,resource_id=RESOURCE,at=AT) | changes

@pytest.fixture
async def ledger():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    value = JobCostLedger(redis, prefix="test:exact-window",job_id=uuid4(),ttl_seconds=604800,
        pricing=PricingCatalog(),job_created_at=datetime.now(timezone.utc))
    try: yield value
    finally: await redis.aclose()

def response(request):
    body=json.loads(request.content)
    start,end=(date.fromisoformat(body[k]) for k in ("startDate","endDate"))
    dimension=next(iter(body["dimensions"]),None)
    count=(end-start).days+1
    clicks=12 if start>=date(2026,9,2) else 10
    keys = [(start+timedelta(days=i)).isoformat() for i in range(count)] if dimension=="date" else [
        {None:None,"page":"https://example.test/service","query":"repair service","device":"MOBILE","country":"usa"}[dimension]]
    rows=[dict(keys=[key] if key is not None else [],clicks=clicks*(1 if dimension=="date" else count),
        impressions=200*(1 if dimension=="date" else count),ctr=clicks/200,position=3) for key in keys]
    return httpx.Response(200,json=dict(rows=rows,responseAggregationType="byPage" if dimension=="page" else "byProperty"))

async def collect(ledger, *, handler=response, entry=None, **changes):
    args=options(); args.update(changes)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await collect_gsc_comparison(entry or record(),**args,provider=GscProvider(client),token="private-test-token",cost_ledger=ledger)

@pytest.mark.anyio
@pytest.mark.parametrize("days",[7,14,28,90])
async def test_exact_request_windows_and_cost_caps(ledger,days):
    calls=[]
    def capture(req): calls.append(req); return response(req)
    result=await collect(ledger,handler=capture,days=days)
    plan=result.plan; snap=result.snapshot
    assert result.state=="collected" and plan.expected_requests==12 and len(calls)==12
    assert snap.previous.end_date==date(2026,8,31) and snap.current.start_date==date(2026,9,2)
    assert (snap.previous.end_date-snap.previous.start_date).days+1==days
    assert (snap.current.end_date-snap.current.start_date).days+1==days
    periods=[(p.start_date.isoformat(),p.end_date.isoformat()) for p in (snap.current,snap.previous)]
    for index,req in enumerate(calls):
        body=json.loads(req.content)
        assert (body["startDate"],body["endDate"])==periods[index//6]
        assert body["dataState"]=="final" and body["type"]=="web" and body["startRow"]==0
        assert "dimensionFilterGroups" not in body and len(body["dimensions"])<=1
        assert req.url.host=="www.googleapis.com"
        assert body["rowLimit"] in (2,4,91,251)
    assert "private-test-token" not in result.model_dump_json()
    assert GscComparisonCollection.model_validate_json(result.model_dump_json())==result
    counters=(await ledger.snapshot()).root
    assert counters["gsc_attempts"]==12 and counters["gsc_successes"]==12
    with pytest.raises(CostLimitExceeded): await collect(ledger,handler=capture,days=days)
    assert len(calls)==12

@pytest.mark.anyio
async def test_waiting_no_calls_or_cost_even_on_last_after_date(ledger):
    # Sept 2 through Nov 30 is 90 days; PT midnight determines eligibility.
    def forbidden(_): raise AssertionError("provider must not run")
    result=await collect(ledger,handler=forbidden,days=90,at="2026-12-01T07:59:59Z")
    assert result.state=="waiting" and result.snapshot is None
    assert result.plan.expected_requests==0 and result.payload_checksum is None and result.health_reasons==[]
    assert (await ledger.snapshot()).root["gsc_attempts"]==0
    result=await collect(ledger,days=90,at="2026-12-01T08:00:00Z")
    assert result.state=="collected"

@pytest.mark.parametrize("zone,day,excluded",[
    ("Asia/Shanghai","2026-09-01",("2026-08-31","2026-09-01")),
    ("America/Los_Angeles","2026-03-08",("2026-03-08","2026-03-08")),
    ("Pacific/Kiritimati","2026-09-01",("2026-08-31","2026-09-01")),
])
def test_calendar_exclusion_and_saved_reference(zone,day,excluded):
    saved=record(timezone=zone,implemented_on=day)
    plan=prepare_gsc_comparison(saved,**options())
    assert (plan.windows.excluded.start.isoformat(),plan.windows.excluded.end.isoformat())==excluded
    assert plan.implementation.checksum==saved.checksum and plan.case_id==saved.case_id
    assert plan.windows.before.end<plan.windows.excluded.start<=plan.windows.excluded.end<plan.windows.after.start
    assert GscComparisonPlan.model_validate_json(plan.model_dump_json())==plan

@pytest.mark.parametrize("changes",[{"days":True},{"days":30},{"action_id":"missing"},{"at":"2026-01-01T00:00:00Z"},
    {"resource_id":""},{"resource_id":"x"*2049},{"resource_id":"bad\u0000resource"},{"binding_id":"not-a-uuid"}])
def test_invalid_plan_rejected(changes):
    args=options(); args.update(changes)
    with pytest.raises((ValueError,TypeError)): prepare_gsc_comparison(record(),**args)

def test_mutated_record_is_not_trusted():
    saved=record().model_copy(update={"revision":2})
    with pytest.raises(ValueError,match="checksum"): prepare_gsc_comparison(saved,**options())

@pytest.mark.anyio
@pytest.mark.parametrize("previous,current",[
    ((date(2026,1,2),date(2026,1,1)),(date(2026,2,1),date(2026,2,7))),
    ((date(2026,1,1),date(2026,4,1)),(date(2026,5,1),date(2026,7,30))),
    ((date(2026,1,1),date(2026,1,7)),(date(2026,1,7),date(2026,1,13))),
    ((date(2026,1,1),date(2026,1,7)),(date(2026,2,1),date(2026,2,14))),
    (("2026-01-01","2026-01-07"),(date(2026,2,1),date(2026,2,7))),
    ((datetime(2026,1,1),datetime(2026,1,7)),(date(2026,2,1),date(2026,2,7))),
    ((date(2026,1,1),),(date(2026,2,1),date(2026,2,7))),
])
async def test_period_primitive_rejects_invalid_ranges_before_io(previous,current):
    def forbidden(_): raise AssertionError("provider must not run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(SyncError,match="SYNC_INVALID_WINDOWS"):
            await GscProvider(client).collect_periods(RESOURCE,"token",previous=previous,current=current)

@pytest.mark.anyio
@pytest.mark.parametrize("status,code",[(401,"SYNC_GOOGLE_TOKEN_EXPIRED"),(403,"SYNC_GOOGLE_ACCESS_DENIED"),
    (429,"SYNC_GOOGLE_UNAVAILABLE"),(500,"SYNC_GOOGLE_UNAVAILABLE"),(302,"SYNC_GOOGLE_REJECTED")])
async def test_errors_count_attempt_and_never_return_partial_output(ledger,status,code):
    calls=[]
    def fail(req):
        calls.append(req)
        return httpx.Response(status,json={"private":"secret"}) if len(calls)==5 else response(req)
    with pytest.raises(SyncError,match=code) as error: await collect(ledger,handler=fail)
    assert "secret" not in str(error.value)
    assert len(calls)==5
    counters=(await ledger.snapshot()).root
    assert counters["gsc_attempts"]==5 and counters["gsc_successes"]==4 and counters["gsc_failures"]==1

@pytest.mark.anyio
async def test_requires_cost_ledger_for_collection():
    with pytest.raises(SyncError,match="SYNC_COST_LEDGER_REQUIRED"): await collect(None)

def comparison_input(result):
    """Test-only trusted boundary. Production persistence is deliberately absent."""
    raw=request_raw(); raw["evaluated_at"]=AT; raw["days"]=result.plan.windows.days
    payload=result.snapshot.model_dump(mode="json")
    for name in ("before","after"):
        raw[name].update(normalized_payload=payload,payload_checksum=request_digest(payload),
            coverage_start=payload["previous"]["start_date"],coverage_end=payload["current"]["end_date"],
            fetched_at=AT,expires_at="2026-12-22T08:00:00Z",health_status=result.health_status,health_reasons=result.health_reasons)
    return raw

@pytest.mark.anyio
@pytest.mark.parametrize("dimension,key",[("property",None),("page","https://example.test/service"),("query","repair service"),("device","MOBILE"),("country","usa")])
async def test_exact_collection_supports_existing_descriptive_comparator(ledger,dimension,key):
    collection=await collect(ledger)
    raw=comparison_input(collection); raw["scope"].update(dimension=dimension,key=key)
    result=compare_implementation(parsed(raw))
    assert result.state=="comparable"
    assert result.metrics[0].before==280 and result.metrics[0].after==336
    assert result.effect_conclusion is None and result.causal_attribution is False
    assert result.effect_eligibility=="pending_metric_specific_calibration"

@pytest.mark.anyio
@pytest.mark.parametrize("kind,reason",[("gap","DAILY_COVERAGE_INCOMPLETE"),("truncated","DETAIL_TRUNCATED"),
    ("absent","TARGET_NOT_OBSERVED_IN_BOTH_WINDOWS"),("aggregation","AGGREGATION_NOT_SUPPORTED")])
async def test_collection_success_does_not_claim_comparable(ledger,kind,reason):
    def incomplete(req):
        base=response(req).json(); body=json.loads(req.content)
        if kind=="gap" and body["dimensions"]==["date"]: base["rows"].pop(0)
        if body["dimensions"]==["page"]:
            if kind=="truncated": base["rows"] += [{**base["rows"][0],"keys":[f"https://example.test/{i}"]} for i in range(250)]
            if kind=="absent": base["rows"][0]["keys"]=["https://example.test/other"]
            if kind=="aggregation": base["responseAggregationType"]="auto"
        return httpx.Response(200,json=base)
    collection=await collect(ledger,handler=incomplete)
    assert collection.state=="collected"
    raw=comparison_input(collection);raw["scope"].update(dimension="page",key="https://example.test/service")
    result=compare_implementation(parsed(raw))
    assert reason in result.reasons and result.metrics==[] and result.effect_conclusion is None

@pytest.mark.anyio
async def test_collection_envelope_tamper_and_cancellation(ledger):
    result=await collect(ledger)
    value=result.model_dump(mode="json");value["snapshot"]["resource_id"]="sc-domain:other.test"
    value["payload_checksum"]=request_digest(value["snapshot"])
    value["checksum"]=verified_request_digest({k:v for k,v in value.items() if k!="checksum"})
    with pytest.raises(ValueError,match="scope mismatch"): GscComparisonCollection.model_validate_json(json.dumps(value))
    class Cancelled:
        async def collect_periods(self,*args,**kwargs): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect_gsc_comparison(record(),**options(),provider=Cancelled(),token="secret",cost_ledger=ledger)
