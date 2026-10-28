"""Real local PostgreSQL ownership/locks; no live object storage or customer files."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.test_native_comparison_storage import prepared, native_database, inspect
from tests.integration.test_native_page_check_storage import revise, unaffected

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(60)]


async def command(p, operation="reserve", attachment_id=None, **changes):
    r = p["record"]
    args = dict(user=r["user_id"], case=r["case_id"], report=r["report_id"], delivery=r["delivery_revision_id"],
        record=r["id"], revision=r["revision"], checksum=r["checksum"], action=r["entries"][0]["action_id"],
        operation=operation, id=str(attachment_id or uuid4()), name="work.pdf", mime="application/pdf", size=100, digest="a"*64)
    args.update(changes)
    rows = await p["db"].query("select public.v22_attachments(" + ",".join(["%s"]*14) + ") value", list(args.values()), True)
    return rows[0]["value"]


async def test_attachment_concurrency_idempotency_and_no_ordinary_mutation(prepared):
    p=prepared; before=await unaffected(p); aid=uuid4()
    results=await asyncio.gather(*(command(p,attachment_id=aid) for _ in range(6)))
    assert sum(v["created"] for v in results)==1
    assert all(v["attachment"]["id"]==str(aid) for v in results)
    assert len(await command(p,"list"))==1
    with pytest.raises(Exception,match="ATTACHMENT_CONFLICT"): await command(p,attachment_id=aid,digest="b"*64)
    a=await command(p,"finalize",aid); assert a["status"]=="ready"
    assert (await command(p,"finalize",aid))["updated_at"]==a["updated_at"]
    assert (await command(p,"remove",aid))["status"]=="removing"
    with pytest.raises(Exception,match="ATTACHMENT_EXPIRED"): await command(p,"finalize",aid)
    assert await unaffected(p)==before and await inspect(p["db"],p["scenario"])==p["baseline"]


@pytest.mark.parametrize("key",["user","case","report","delivery","record","revision","checksum","action"])
async def test_attachment_rejects_wrong_source(prepared,key):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid)
    value=2 if key=="revision" else "sha256:"+"b"*64 if key=="checksum" else str(uuid4())
    for operation in ("read","list","reserve","finalize","remove"):
        with pytest.raises(Exception,match="NOT_FOUND"): await command(p,operation,aid,**{key:value})
    assert (await command(p,"read",aid))["status"]=="uploading"


async def test_attachment_last_action_slot_serialized_and_pending_cleanup_counts(prepared):
    p=prepared
    ids=[uuid4() for _ in range(4)]
    for aid in ids: await command(p,attachment_id=aid)
    results=await asyncio.gather(command(p),command(p),return_exceptions=True)
    assert sum(isinstance(v,dict) for v in results)==1
    assert any("ATTACHMENT_LIMIT" in str(v) for v in results)
    await command(p,"remove",ids[0])
    with pytest.raises(Exception,match="ATTACHMENT_LIMIT"): await command(p)
    # Early cleanup acknowledgements cannot release an in-flight reservation.
    await p["db"].query("select finish_v22_attachment_cleanup(%s)",[ids[0]],True)
    assert (await command(p,"read",ids[0]))["status"]=="removing"


async def test_attachment_latest_revision_upload_and_old_history(prepared):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid); await command(p,"finalize",aid)
    pending=uuid4(); await command(p,attachment_id=pending); await revise(p)
    assert (await command(p,"read",aid))["status"]=="ready"
    assert not (await command(p,attachment_id=aid))["created"]
    with pytest.raises(Exception,match="ATTACHMENT_STALE"): await command(p)
    with pytest.raises(Exception,match="ATTACHMENT_STALE"): await command(p,"finalize",pending)
    new=(await p["db"].query("select payload from capability_implementation_records where id=%s and revision=2",[p["record"]["id"]]))[0]["payload"]
    assert await command(p,"list",revision=2,checksum=new["checksum"])==[]
    assert (await command(p,"remove",aid))["status"]=="removing"


async def test_attachment_read_does_not_expire_and_explicit_cleanup_is_replayable(prepared):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid)
    await p["db"].query("update implementation_attachments set expires_at=clock_timestamp()-interval '3 minutes' where id=%s",[aid])
    assert (await command(p,"read",aid))["status"]=="uploading"
    with pytest.raises(Exception,match="ATTACHMENT_EXPIRED"): await command(p,"finalize",aid)
    work=await p["db"].query("select * from prepare_v22_attachment_cleanup()",[],True)
    assert len(work)==1 and work[0]["status"]=="removing" and work[0]["cleaned_at"] is None
    await p["db"].query("select finish_v22_attachment_cleanup(%s)",[aid],True)
    a=await command(p,"read",aid); assert a["status"]=="removed" and a["cleaned_at"]
    await p["db"].query("select finish_v22_attachment_cleanup(%s)",[aid],True)
    events=await p["db"].query("select event from implementation_attachment_events where attachment_id=%s order by sequence",[aid])
    assert [e["event"] for e in events]==["reserved","removing","removed"]
    await p["db"].query("update implementation_attachments set cleanup_after=clock_timestamp()-interval '1 second' where id=%s",[aid])
    assert (await p["db"].query("select * from prepare_v22_attachment_cleanup()",[],True))[0]["status"]=="removed"


async def test_attachment_daily_case_limit_survives_removal_and_replay(prepared):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid)
    # Test-admin fixture simulates accepted, physically cleaned jobs, across older work.
    for _ in range(19):
        await p["db"].query("""insert into implementation_attachments select %s,user_id,case_id,report_id,delivery_revision_id,
          record_id,record_revision,record_checksum,action_id,file_name,mime,byte_size,digest,%s,'removed',created_at,expires_at,
          updated_at,clock_timestamp()+interval '24 hours',clock_timestamp() from implementation_attachments where id=%s""",
          [uuid4(),"files/"+str(uuid4()),aid])
    with pytest.raises(Exception,match="ATTACHMENT_LIMIT"): await command(p)
    assert not (await command(p,attachment_id=aid))["created"]
    await p["db"].query("update implementation_attachments set created_at=clock_timestamp()-interval '24 hours 1 second' where case_id=%s",[p["record"]["case_id"]])
    assert (await command(p))["created"]


async def test_attachment_case_capacity_includes_unconfirmed_deletions(prepared):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid,size=2097152)
    for _ in range(49):
        await p["db"].query("""insert into implementation_attachments select %s,user_id,case_id,report_id,delivery_revision_id,
          record_id,record_revision,record_checksum,'older-action',file_name,mime,byte_size,digest,%s,'removing',
          clock_timestamp()-interval '25 hours',expires_at,updated_at,clock_timestamp(),null from implementation_attachments where id=%s""",
          [uuid4(),"files/"+str(uuid4()),aid])
    with pytest.raises(Exception,match="ATTACHMENT_LIMIT"): await command(p,size=1)
    assert not (await command(p,attachment_id=aid,size=2097152))["created"]


@pytest.mark.parametrize("changes",[dict(size=0),dict(size=2097153),dict(mime="text/html"),dict(name="../file.pdf"),dict(digest="bad")])
async def test_attachment_invalid_metadata_never_reserves(prepared,changes):
    with pytest.raises(Exception,match="ATTACHMENT_INVALID"): await command(prepared,**changes)
    assert await command(prepared,"list")==[]


async def test_attachment_privileges_and_archived_case(prepared):
    p=prepared; aid=uuid4(); await command(p,attachment_id=aid)
    privileges=await p["db"].query("""select
      has_table_privilege('authenticated','implementation_attachments','SELECT') reader,
      has_table_privilege('service_role','implementation_attachments','INSERT') writer,
      has_function_privilege('authenticated','v22_attachments(uuid,uuid,uuid,uuid,uuid,integer,text,text,text,uuid,text,text,integer,text)','EXECUTE') caller,
      has_function_privilege('service_role','v22_attachment_source(uuid,uuid,uuid,uuid,uuid,integer,text,text)','EXECUTE') helper""")
    assert privileges==[dict(reader=False,writer=False,caller=False,helper=False)]
    with pytest.raises(Exception,match="permission denied"):
        await p["db"].query("delete from implementation_attachments where id=%s",[aid],True)
    with pytest.raises(Exception,match="IMMUTABLE"):
        await p["db"].query("delete from implementation_attachment_events where attachment_id=%s",[aid])
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s",[p["record"]["case_id"]])
    with pytest.raises(Exception,match="NOT_FOUND"): await command(p,"read",aid)


async def test_attachment_bucket_restricts_broad_legacy_storage_policies(native_database):
    """Exercise the migration's Storage branch against a local schema, not a hosted Storage service."""
    from psycopg import AsyncConnection
    db=native_database
    await db.query("""create schema storage;
      create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
      create table storage.objects(bucket_id text,name text);
      alter table storage.objects enable row level security;
      grant usage on schema storage to authenticated,anon;
      grant all on storage.objects to authenticated,anon;
      create policy old_broad_policy on storage.objects for all to authenticated,anon using(true) with check(true);
      insert into storage.objects values('implementation-files','private'),('other','ordinary');""")
    migration=Path(__file__).resolve().parents[2].parent/"search-trust/supabase/migrations/20261026100000_implementation_attachments.sql"
    block="do $$ begin"+migration.read_text().split("do $$ begin",1)[1].split("commit;",1)[0]
    await db.query(block)
    bucket=(await db.query("select * from storage.buckets"))[0]
    assert bucket["public"] is False and bucket["file_size_limit"]==2097152
    for role in ("anon","authenticated"):
        async with await AsyncConnection.connect(db.url) as conn:
            await conn.execute("set local role "+role)
            assert await (await conn.execute("select name from storage.objects")).fetchall()==[("ordinary",)]
        with pytest.raises(Exception,match="row-level security"):
            async with await AsyncConnection.connect(db.url) as conn:
                await conn.execute("set local role "+role)
                await conn.execute("insert into storage.objects values('implementation-files','unauthorized')")
