"""Independent decisions; no providers or actual customer data."""
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from app.report_v22.review_decision_models import SaveDecision, DecisionRevision, MAX_INPUT
from app.report_v22.review_decision_validation import parse, normalize, validate_revision
from app.report_v22.review_decision_storage import ReviewDecisionRepository
from app.report_v22.review_decision_service import ReviewDecisionService
from tests.test_v22_review_freeze import fixture
from app.report_v22.review_freeze_models import SavedReviewEvidence
from app.report_v22.review_projection import build_review_projection
from tests.support.review_projection_fixtures import bundle_raw, seal
from tests.test_v22_review_projection_boundaries import multi_bundle


@pytest.fixture
def anyio_backend():
    return "asyncio"


def example():
    b, _, raw, ids = fixture()
    saved = ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)
    ids["report_id"] = saved.bundle.report.id
    return saved, ids


def item(saved, disposition="continue", action_id=None):
    action_id = action_id or saved.bundle.delivery.actions[0].id
    d = dict(source="seo_recorded", action_id=action_id, disposition=disposition, reason="Review the contact path.",
        references=[f"implementation:{saved.bundle.implementation.id}:{saved.bundle.delivery.actions[0].id}"],
        target=None, work=None, completion_statement=None, reconsider_when=None)
    if disposition in ("adjust", "new"): d.update(target="Service page", work="Clarify contact details.")
    if disposition == "observe": d["completion_statement"] = "I published the agreed changes."
    if disposition == "defer": d["reconsider_when"] = "Once the client confirms the service area."
    return d


def request(saved, decisions=None, **kw):
    return dict(id=str(uuid4()), review_id=str(saved.id), previous_id=None, previous_checksum=None,
        decisions=[item(saved)] if decisions is None else decisions) | kw


def receipt(saved, req, **kw):
    req = normalize(req, saved).model_dump(mode="json")
    raw = req | dict(schema_version="v22_review_decision_revision_v1", user_id=str(saved.user_id), case_id=str(saved.case_id),
        report_id=str(saved.bundle.report.id), evidence_checksum=saved.bundle.evidence_checksum,
        bundle_checksum=saved.bundle.checksum, revision=1, recorded_at="2026-12-17T08:00:00Z", request_checksum=verified_request_digest(req)) | kw
    raw["checksum"] = verified_request_digest(raw)
    return raw


@pytest.mark.parametrize("kind", ["continue", "adjust", "observe", "defer", "new"])
def test_decisions_after_freeze_keep_original_evidence(kind):
    saved, ids = example(); original = saved.model_dump_json()
    req = request(saved, [item(saved, kind, "new-work" if kind == "new" else None)])
    r = parse(DecisionRevision, receipt(saved, req), 131072)
    assert validate_revision(r, saved, **{k:v for k,v in ids.items() if k != "review_id"}) == r
    assert r.recorded_at > saved.frozen_at
    assert saved.model_dump_json() == original and saved.bundle.decisions == []


@pytest.mark.parametrize("change", ["blank", "control", "long", "unknown", "duplicate", "foreign", "type", "missing_target", "extra_work", "completion", "reconsider", "too_many", "previous", "timestamp", "source", "new_identity", "bad_id"])
def test_reject_bad_decision_requests(change):
    saved, _ = example(); req = request(saved); d = req["decisions"][0]
    if change == "blank": d["reason"] = "\u2003\n"
    if change == "control": d["reason"] = "private\x7f"
    if change == "long": d["reason"] = "x"*1201
    if change == "unknown": d["other"] = True
    if change == "duplicate": req["decisions"] *= 2
    if change == "foreign": d["references"] = ["page:foreign"]
    if change == "type": d["disposition"] = "auto_complete"
    if change == "missing_target": d["disposition"] = "adjust"
    if change == "extra_work": d["work"] = "unexpected"
    if change == "completion": d["disposition"] = "observe"
    if change == "reconsider": d["disposition"] = "defer"
    if change == "too_many": req["decisions"] = [item(saved,"new",f"new-{i}") for i in range(4)]
    if change == "previous": req["previous_id"] = str(uuid4())
    if change == "timestamp": req["recorded_at"] = "2026-12-17T08:00:00Z"
    if change == "source": d["source"] = "system"
    if change == "new_identity": d["disposition"] = "new"; d.update(target="page",work="work")
    if change == "bad_id": d["action_id"] = "missing"
    with pytest.raises(DeterministicJobError): normalize(req, saved)


def test_empty_normalization_limits_and_duplicate_json():
    saved, _ = example()
    assert normalize(request(saved, []), saved).decisions == []
    req = request(saved); raw = json.dumps(req)
    with pytest.raises(DeterministicJobError): parse(SaveDecision, raw[:-1]+',"decisions":[]}',MAX_INPUT)
    with pytest.raises(DeterministicJobError,match="TOO_LARGE"): parse(SaveDecision, b" "*65537,MAX_INPUT)
    d = item(saved,"new","new-work"); d["references"] += [f"page:{saved.bundle.selections[0].page.current.id}"]
    a = normalize(request(saved,[d],id=req["id"]),saved)
    d["references"].reverse()
    assert normalize(request(saved,[d],id=req["id"]),saved) == a


@pytest.mark.parametrize("change", ["checksum", "request_hash", "time", "basis", "owner", "revision", "extra", "id"])
def test_response_integrity(change):
    saved, ids = example(); raw = receipt(saved,request(saved))
    if change == "checksum": raw["checksum"] = "sha256:"+"0"*64
    if change == "request_hash": raw["request_checksum"] = "sha256:"+"0"*64
    if change == "time": raw["recorded_at"] = "2026-12-15T08:00:00Z"
    if change == "basis": raw["evidence_checksum"] = "sha256:"+"0"*64
    if change == "owner": raw["user_id"] = str(uuid4())
    if change == "revision": raw["revision"] = True
    if change == "extra": raw["private_payload"] = {}
    if change == "id": raw["id"] = str(uuid4())
    if change != "checksum": raw["checksum"] = verified_request_digest({k:v for k,v in raw.items() if k!="checksum"})
    with pytest.raises(DeterministicJobError):
        validate_revision(parse(DecisionRevision,raw,131072),saved,**{k:v for k,v in ids.items() if k!="review_id"})


@pytest.mark.anyio
@pytest.mark.parametrize("status,message,code", [(400,"REVIEW_DECISION_STALE","STALE"),(400,"REVIEW_DECISION_CONFLICT","CONFLICT"),
    (400,"REVIEW_DECISION_INVALID","INVALID"),(400,"private SQL detail","UNAVAILABLE"),(429,"private","STORAGE_UNAVAILABLE"),(503,"private","STORAGE_UNAVAILABLE")])
async def test_transport_safe_errors(status,message,code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(status,json={"message":message}))) as client:
        repo=ReviewDecisionRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        with pytest.raises((DeterministicJobError,TransientJobError),match=code) as e:
            await repo.rpc("get_v22_review_decision",{})
        assert "private" not in str(e.value)


@pytest.mark.anyio
async def test_transport_limits_timeout_and_no_redirects():
    for mode in ("huge","timeout","redirect"):
        calls=[]
        def handler(req):
            calls.append(req.url)
            if mode=="timeout": raise httpx.ReadTimeout("private token",request=req)
            if mode=="redirect": return httpx.Response(307,headers={"location":"https://elsewhere.invalid"})
            return httpx.Response(200,content=b"x"*131073)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            repo=ReviewDecisionRepository(url="https://local.invalid",service_role_key="local",http_client=client)
            with pytest.raises((DeterministicJobError,TransientJobError)) as e: await repo.rpc("get_v22_review_decision",{})
            assert "private" not in str(e.value) and len(calls)==1


@pytest.mark.anyio
async def test_service_binds_returned_request_and_identity():
    saved,ids=example(); req=request(saved)
    class Evidence:
        async def read(self,**_): return saved
    class Repository:
        async def rpc(self,name,args): return canonical_json_bytes(receipt(saved,req))
    service=ReviewDecisionService(Repository(),Evidence())
    owner={k:v for k,v in ids.items() if k!="review_id"}
    assert (await service.save(**owner,request=req)).id.hex==req["id"].replace("-", "")
    # An unbound success receipt does not prove that the write was rejected.
    with pytest.raises(TransientJobError): await service.save(**owner,request=request(saved))
    with pytest.raises(DeterministicJobError): await service.read(**ids,decision_id=uuid4())


@pytest.mark.anyio
async def test_oversized_save_receipt_requires_same_request_recovery():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"*131073))) as client:
        repo=ReviewDecisionRepository(url="https://local.invalid",service_role_key="local",http_client=client)
        with pytest.raises(TransientJobError, match="STORAGE_UNAVAILABLE"):
            await repo.rpc("save_v22_review_decision", {})


def evidence_for(b):
    _, _, raw, _ = fixture(False)
    raw.pop("bundle_text")
    raw["bundle"] = b
    raw["selection"].update(report=dict(id=b["report"]["id"],checksum=b["report_checksum"]),
        delivery=dict(id=b["delivery_id"],revision=b["delivery_revision"],checksum=b["delivery_checksum"]),
        implementation={k:b["implementation"][k] for k in ("id","revision","checksum")},
        actions=[dict(action_id=s["action_id"],page_current_id=None,page_previous_id=None,gsc_id=None,ga4_id=None) for s in b["selections"]])
    return SavedReviewEvidence.model_validate_json(canonical_json_bytes(raw))


def zero_evidence():
    b=bundle_raw(sources=False)
    b["report"].update(actions=[],action_state="insufficient_basis")
    b["report_checksum"]=verified_request_digest(b["report"])
    b["delivery"].update(actions=[],source_checksum=b["report_checksum"])
    b["delivery_checksum"]=verified_request_digest(b["delivery"])
    b["implementation"].update(entries=[],report_checksum=b["report_checksum"])
    b["implementation"]["checksum"]=verified_request_digest({k:v for k,v in b["implementation"].items() if k!="checksum"})
    b["selections"]=[]
    return evidence_for(seal(b))


def test_zero_action_keeps_diagnosis_and_no_fabricated_new_action():
    saved=zero_evidence(); req=request(saved,[])
    assert normalize(req,saved).decisions==[]
    r=parse(DecisionRevision,receipt(saved,req),131072)
    assert r.decisions==[] and build_review_projection(saved.bundle).prior_diagnosis.findings
    normal,_=example(); req["decisions"]=[item(normal,"new","new-work")]
    with pytest.raises(DeterministicJobError): normalize(req,saved)


def test_multiple_actions_order_maximum_and_reference_boundaries():
    saved=evidence_for(multi_bundle())
    original=[item(saved,"defer",a.id)|dict(references=[]) for a in saved.bundle.delivery.actions]
    new=[item(saved,"new",f"new-{i}") for i in range(3)]
    req=request(saved,new+list(reversed(original)))
    result=normalize(req,saved)
    assert [d.action_id for d in result.decisions]==["ac-1","ac-2","ac-3","new-0","new-1","new-2"]
    req["decisions"][-1].update(disposition="continue",reconsider_when=None)
    with pytest.raises(DeterministicJobError): normalize(req,saved)
    for kind in ("continue","observe"):
        with pytest.raises(DeterministicJobError): normalize(request(saved,[item(saved,kind,"ac-2")]),saved)
    d=item(saved,"observe","ac-2");d["references"]=[]
    with pytest.raises(DeterministicJobError): normalize(request(saved,[d]),saved)


@pytest.mark.parametrize("value", [float("nan"),float("inf"),float("-inf")])
def test_nonfinite_values_do_not_silently_turn_into_null(value):
    saved,_=example();req=request(saved);req["decisions"][0]["target"]=value
    for raw in (req,json.dumps(req)):
        with pytest.raises(DeterministicJobError): normalize(raw,saved)


def test_multibyte_request_limit_not_only_character_count():
    saved=evidence_for(multi_bundle())
    ds=[item(saved,"defer",a.id)|dict(references=[],reason="中"*1200,reconsider_when="中"*1200) for a in saved.bundle.delivery.actions]
    ds += [item(saved,"new",f"new-{i}")|dict(reason="中"*1200,target="中"*2000,work="中"*4000) for i in range(3)]
    with pytest.raises(DeterministicJobError,match="TOO_LARGE"): normalize(request(saved,ds),saved)


@pytest.mark.parametrize("kind", ["page","gsc","ga4"])
def test_exact_selected_source_reference_allowed(kind):
    saved,_=example(); s=saved.bundle.selections[0]
    identifier=s.page.current.id if kind=="page" else getattr(s,kind).id
    d=item(saved,"adjust");d["references"]=[f"{kind}:{identifier}"]
    assert normalize(request(saved,[d]),saved).decisions[0].references==d["references"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["id","report","cursor","duplicate","chain","truncated","extra","huge","malformed"])
async def test_service_rejects_malformed_history(mode):
    saved,ids=example();r=receipt(saved,request(saved))
    row={k:r[k] for k in ("id","revision","previous_id","recorded_at","checksum")}|dict(decision_count=1)
    body=dict(review_id=str(saved.id),report_id=str(saved.bundle.report.id),items=[row],next_cursor=None)
    if mode=="id": body["review_id"]=str(uuid4())
    if mode=="report": body["report_id"]=str(uuid4())
    if mode=="cursor": body["next_cursor"]=1
    if mode=="duplicate": body["items"]*=2
    if mode=="chain": body["items"].insert(0,row|dict(id=str(uuid4()),revision=3,previous_id=str(uuid4())))
    if mode=="truncated": body["items"]=[row|dict(revision=2,previous_id=str(uuid4()))]
    if mode=="extra": body["private_notes"]="unexpected"
    class Evidence:
        async def read(self,**_): return saved
    class Repository:
        async def rpc(self,*_):
            if mode=="huge": return b" "*32769
            if mode=="malformed": return b"null"
            return canonical_json_bytes(body)
    with pytest.raises(DeterministicJobError): await ReviewDecisionService(Repository(),Evidence()).history(**ids)
