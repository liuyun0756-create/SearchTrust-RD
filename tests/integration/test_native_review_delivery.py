"""Real isolated approval/pinned delivery SQL. No external clients or billing."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.review_report_persistence import ReviewCompletionService
from tests.integration.test_native_review_report_settlement import repo,state,choose
from tests.integration.test_native_review_input_storage import bound_setup,decisions,review_setup,prepared,native_database,freeze

pytestmark=[pytest.mark.native_integration,pytest.mark.anyio,pytest.mark.timeout(70)]


async def ready(p,zero=False):
    if zero:await choose(p,None)
    f=await freeze(p);r,_=await ReviewCompletionService(repo(p)).complete(frozen=f,run_generation=1)
    body=dict(schema_version="v22_review_delivery_revision_v1",report_id=str(r.id),user_id=str(p["input"].user_id),case_id=str(p["input"].case_id),
        expected_revision=0,source_checksum=verified_request_digest(r.model_dump(mode="json")),status="approved",title=r.title,
        summary="Reviewed client summary.",client_note=None,actions=[a.model_dump(mode="json") for a in r.actions])
    args=dict(p_user_id=str(p["input"].user_id),p_case_id=str(p["input"].case_id),p_report_id=str(r.id))
    return r,body,args


def command(body,**kw):return dict(id=str(uuid4()),review_confirmed=body["status"]=="approved",content=body)|kw
async def save(p,args,q):return await p["db"].rpc("save_v22_review_delivery",args|dict(p_request_text=json.dumps(q)))


@pytest.mark.parametrize("zero",[False,True])
async def test_native_delivery_idempotence_draft_approval_and_pinned_share(bound_setup,zero):
    p=bound_setup;r,body,args=await ready(p,zero);before=await state(p)
    q=command(body);items=await asyncio.gather(*(save(p,args,q) for _ in range(3)));first=items[0]
    assert all(x==first for x in items) and first["status"]=="approved"
    assert await p["db"].rpc("get_v22_review_delivery",args|dict(p_revision_id=first["id"]))==first
    old=await p["db"].query("select * from create_v22_capability_share(%s,%s,%s,%s,%s)",[*args.values(),first["id"],"a"*64],True)
    draft=deepcopy(body);draft.update(expected_revision=1,status="draft",summary="New draft must not replace the shared version.")
    second=await save(p,args,command(draft));assert second["revision"]==2
    with pytest.raises(Exception,match="CAPABILITY_APPROVAL_CHANGED"):
        await p["db"].query("select * from create_v22_capability_share(%s,%s,%s,%s,%s)",[*args.values(),first["id"],"b"*64],True)
    pinned=(await p["db"].query("select * from resolve_v22_capability_share(%s)",["a"*64],True))[0]
    assert str(pinned["revision_id"])==first["id"]
    assert (await p["db"].rpc("get_v22_review_delivery",args|dict(p_revision_id=first["id"])))["content"]["summary"]==body["summary"]
    assert len(await p["db"].rpc("list_v22_review_delivery_history",args))==2
    assert (await save(p,args,q))["id"]==first["id"] # recovery survives a newer revision
    await p["db"].query("select revoke_v22_capability_share(%s,%s,%s,%s)",[*args.values(),old[0]["id"]],True)
    assert await p["db"].query("select * from resolve_v22_capability_share(%s)",["a"*64],True)==[]
    assert await state(p)==before
    assert len((await p["db"].rpc("get_v22_review_delivery",args|dict(p_revision_id=first["id"])))["content"]["actions"])==(0 if zero else 1)


async def test_native_delivery_conflicts_identity_confirmation_and_protected_facts(bound_setup):
    p=bound_setup;r,body,args=await ready(p)
    for change in ("confirm","hash","facts","order","materials","steps","extra","owner","blank"):
        b=deepcopy(body);q=command(b)
        if change=="confirm":q["review_confirmed"]=False
        elif change=="hash":b["source_checksum"]="sha256:"+"0"*64
        elif change=="facts":b["observed_fact"]="edited"
        elif change=="order":b["actions"][0]["id"]="new-id"
        elif change=="materials":b["actions"][0]["materials"]=[{}]
        elif change=="steps":b["actions"][0]["steps"]=["x"*4001]
        elif change=="extra":q["extra"]=True
        elif change=="owner":b["user_id"]=str(uuid4())
        elif change=="blank":b["summary"]="\u2003\n"
        with pytest.raises(Exception,match="REVIEW_DELIVERY_INVALID"):await save(p,args,q)
    q=command(body);first=await save(p,args,q)
    with pytest.raises(Exception,match="REVIEW_DELIVERY_CONFLICT"):await save(p,args,command(body))
    changed=deepcopy(q);changed["content"]["summary"]="Different body with the same ID"
    with pytest.raises(Exception,match="REVIEW_DELIVERY_CONFLICT"):await save(p,args,changed)
    with pytest.raises(Exception,match="REVIEW_REPORT_UNAVAILABLE"):await save(p,args|dict(p_user_id=str(uuid4())),q)
    assert (await p["db"].rpc("get_v22_review_delivery",args))["id"]==first["id"]


async def test_native_delivery_unknown_response_recovery_permissions_and_expiry(bound_setup):
    p=bound_setup;r,body,args=await ready(p);q=command(body)
    first=await save(p,args,q) # simulate losing this receipt; replay exact command
    assert await save(p,args,q)==first
    rights=await p["db"].query("""select has_table_privilege('service_role','capability_review_delivery_requests','INSERT') a,
      has_table_privilege('authenticated','report_content_revisions','SELECT') b,
      has_function_privilege('anon','save_v22_review_delivery(uuid,uuid,uuid,text)','EXECUTE') c""")
    assert not any(rights[0].values())
    with pytest.raises(Exception,match="IMMUTABLE"):await p["db"].query("update capability_review_delivery_requests set request=request where id=%s",[q["id"]])
    await p["db"].query("insert into capability_report_shares(user_id,case_id,report_id,revision_id,token_hash,created_at,expires_at) values(%s,%s,%s,%s,%s,now()-interval '2 days',now()-interval '1 day')",[*args.values(),first["id"],"c"*64])
    assert await p["db"].query("select * from resolve_v22_capability_share(%s)",["c"*64],True)==[]
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[r.business.case_id])
    with pytest.raises(Exception,match="REVIEW_REPORT_UNAVAILABLE"):await save(p,args,q)
