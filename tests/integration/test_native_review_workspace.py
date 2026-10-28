"""Owner directory and workspace with disposable PostgreSQL, no live account calls."""
import json
from uuid import uuid4

import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_workspace import ReviewWorkspace
from app.report_v22.review_workspace_models import command_adapter
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database
from tests.integration.test_native_page_check_storage import page_setup, request as page_request, execute as page_execute, revise
from tests.integration.test_native_ga4_collection_storage import ga4_setup, request as ga4_request, execute as ga4_execute

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(70)]


def cmd(p, operation, **kw):
    return command_adapter.validate_json(json.dumps(dict(operation=operation,
        user_id=str(p["identity"]["user_id"]),case_id=str(p["identity"]["case_id"]),report_id=p["record"]["report_id"],**kw)))


def target(p):
    r = p["record"]
    return dict(delivery_id=r["delivery_revision_id"],implementation_id=r["id"],implementation_revision=r["revision"])


async def test_native_context_preview_save_history_exact_read(review_setup):
    p=review_setup; service=ReviewWorkspace(p["reviews"])
    deliveries=await service.run(cmd(p,"versions",kind="deliveries"))
    assert deliveries.items[0].id == p["review_choice"].delivery.id
    implementations=await service.run(cmd(p,"versions",kind="implementations",delivery_id=str(deliveries.items[0].id)))
    assert implementations.items[0].id == p["review_choice"].implementation.id
    context=await service.run(cmd(p,"context",target=target(p)))
    assert context.selection == p["review_choice"]
    assert context.confirmation_fingerprint is None
    for kind in ("page","gsc","ga4"):
        directory=await service.run(cmd(p,"candidates",kind=kind,target=target(p),action_id=context.actions[0].action_id))
        assert directory.items == []
    preview=await service.run(cmd(p,"preview",selection=context.selection.model_dump(mode="json")))
    saved=await service.run(cmd(p,"save",selection=context.selection.model_dump(mode="json"),review_id=str(uuid4()),confirmation_fingerprint=preview.confirmation_fingerprint))
    history=await service.run(cmd(p,"history",kind="history"))
    assert history.items[0].id == saved.review_id
    assert await service.run(cmd(p,"read",review_id=str(saved.review_id))) == saved


async def test_native_confirmation_stales_after_case_change(review_setup):
    p=review_setup; service=ReviewWorkspace(p["reviews"]); selection=p["review_choice"].model_dump(mode="json")
    preview=await service.run(cmd(p,"preview",selection=selection))
    await p["db"].query("update client_cases set updated_at=clock_timestamp() where id=%s",[p["identity"]["case_id"]])
    with pytest.raises(DeterministicJobError,match="STALE"):
        await service.run(cmd(p,"save",selection=selection,review_id=str(uuid4()),confirmation_fingerprint=preview.confirmation_fingerprint))
    assert await p["db"].query("select id from capability_review_evidence_bundles") == []


async def test_native_directory_scope_and_archive(review_setup):
    p=review_setup; service=ReviewWorkspace(p["reviews"])
    for field in ("user_id","case_id","report_id"):
        raw=cmd(p,"versions",kind="deliveries").model_dump(mode="json");raw[field]=str(uuid4())
        with pytest.raises(DeterministicJobError): await service.run(command_adapter.validate_json(json.dumps(raw)))
    with pytest.raises(DeterministicJobError):
        await service.run(cmd(p,"candidates",kind="page",target=target(p),action_id="another-action"))
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[p["identity"]["case_id"]])
    with pytest.raises(DeterministicJobError): await service.run(cmd(p,"history",kind="history"))


async def test_native_snapshot_history_pagination_and_recovery(review_setup):
    p=review_setup; service=ReviewWorkspace(p["reviews"]); selection=p["review_choice"].model_dump(mode="json")
    preview=await service.run(cmd(p,"preview",selection=selection)); commands=[]
    for _ in range(22):
        c=cmd(p,"save",selection=selection,review_id=str(uuid4()),confirmation_fingerprint=preview.confirmation_fingerprint)
        commands.append(c);await service.run(c)
    first=await service.run(cmd(p,"history",kind="history"))
    second=await service.run(cmd(p,"history",kind="history",cursor=first.next_cursor.model_dump(mode="json")))
    assert len(first.items)==20 and len(second.items)==2 and second.next_cursor is None
    assert len({v.id for v in first.items+second.items})==22
    recovered=await service.run(commands[0]);assert recovered.review_id==commands[0].review_id
    assert len(await p["db"].query("select id from capability_review_evidence_bundles"))==22


async def test_native_directory_service_role_only(review_setup):
    p=review_setup
    for role in ("anon","authenticated"):
        result=await p["db"].query("select has_function_privilege(%s,'public.list_v22_review_workspace(uuid,uuid,uuid,text,uuid,uuid,integer,text,jsonb)','EXECUTE') as allowed",[role])
        assert result[0]["allowed"] is False


async def test_native_gsc_candidates_and_exact_historical_read(review_setup):
    p=review_setup;service=ReviewWorkspace(p["reviews"]); action=p["review_choice"].actions[0].action_id
    comparison=await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=p["selection"])
    directory=await service.run(cmd(p,"candidates",kind="gsc",target=target(p),action_id=action))
    assert [i.id for i in directory.items]==[comparison.id]
    source=await service.run(cmd(p,"source",kind="gsc",target=target(p),action_id=action,source_id=str(comparison.id)))
    assert source.actions[0].gsc is not None
    old=target(p);await revise(p)
    historical=await service.run(cmd(p,"source",kind="gsc",target=old,action_id=action,source_id=str(comparison.id)))
    assert historical.selection==source.selection
    assert historical.actions[0].gsc==source.actions[0].gsc


@pytest.mark.parametrize("unable",[False,True])
async def test_native_page_candidates_exclude_pending_keep_completed_limits(review_setup,page_setup,unable):
    import httpx
    p=review_setup;service=ReviewWorkspace(p["reviews"]);action=p["review_choice"].actions[0].action_id
    job=await page_request(p)
    command=cmd(p,"candidates",kind="page",target=target(p),action_id=action)
    assert (await service.run(command)).items==[]
    job=await page_execute(p,job,(lambda _:httpx.Response(404)) if unable else None)
    assert [i.id for i in (await service.run(command)).items]==[job.id]
    result=await service.run(cmd(p,"source",kind="page",target=target(p),action_id=action,source_id=str(job.id)))
    assert result.actions[0].page.status==('unable_to_check' if unable else 'checked')


@pytest.mark.parametrize("restricted",[False,True])
async def test_native_ga4_candidates_preserve_withheld(review_setup,ga4_setup,restricted):
    import httpx
    p=review_setup;service=ReviewWorkspace(p["reviews"]);action=p["review_choice"].actions[0].action_id
    job=await ga4_request(p);original=p["provider_handler"]
    command=cmd(p,"candidates",kind="ga4",target=target(p),action_id=action)
    assert (await service.run(command)).items==[]
    def handler(req):
        raw=original(req).json()
        if restricted and req.method=='POST':raw['metadata']['subjectToThresholding']=True
        return httpx.Response(200,json=raw)
    job=await ga4_execute(p,job,handler=handler)
    assert [i.id for i in (await service.run(command)).items]==[job.id]
    result=await service.run(cmd(p,"source",kind="ga4",target=target(p),action_id=action,source_id=str(job.id)))
    assert result.actions[0].ga4.state==('withheld' if restricted else 'available')
    if restricted:assert result.actions[0].ga4.before is result.actions[0].ga4.after is None


async def test_native_sources_beyond_first_directory_page_remain_selectable(review_setup):
    p=review_setup;service=ReviewWorkspace(p["reviews"]);action=p["review_choice"].actions[0].action_id
    ids=[]
    for _ in range(22):
        comparison=await p["repo"].freeze(**p["identity"],comparison_id=uuid4(),selection=p["selection"])
        ids.append(comparison.id)
    first=await service.run(cmd(p,"candidates",kind="gsc",target=target(p),action_id=action))
    second=await service.run(cmd(p,"candidates",kind="gsc",target=target(p),action_id=action,cursor=first.next_cursor.model_dump(mode='json')))
    assert len(first.items)==20 and len(second.items)==2
    assert {i.id for i in first.items+second.items}==set(ids)
    source=await service.run(cmd(p,"source",kind="gsc",target=target(p),action_id=action,source_id=str(ids[0])))
    assert source.selection.actions[0].gsc_id==ids[0]
