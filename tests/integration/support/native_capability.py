"""Native SQL acceptance adapter and controlled sources; test code only."""
import json
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_canonical_json_bytes
from support.first_service_delivery_bridge import seed
from support.first_service_entry_bridge import now

DATABASE_NAME = re.compile(r"^searchtrust_v22_test_[0-9a-f]{32}$")
SCALAR_RPCS = frozenset({"save_v22_implementation_record","lookup_v22_review_generation", "start_v22_review_generation", "get_v22_review_generation_status",
    "find_v22_review_generation", "pending_v22_review_generations", "claim_v22_review_dispatch", "claim_v22_review_run",
    "heartbeat_v22_review_generation", "finalize_v22_review_generation", "reserve_v22_first_service_discovery", "record_v22_first_service_discovery",
    "claim_v22_first_service_discovery_dispatch", "claim_v22_first_service_discovery_run",
    "claim_v22_first_service_preparation", "heartbeat_v22_first_service_preparation",
    "begin_v22_first_service_collection", "freeze_v22_first_service_preparation",
    "get_v22_capability_dispatch", "resolve_v22_capability_analysis_input", "finalize_v22_capability_failure",
    "get_v22_comparison", "resolve_v22_comparison_input", "freeze_v22_comparison", "get_v22_comparison_context",
    "resolve_v22_collection_input", "get_v22_collection", "request_v22_collection", "claim_v22_collection",
    "finish_v22_collection", "fail_v22_collection", "get_v22_collection_context", "pending_v22_collections",
    "resolve_v22_ga4_collection_input", "get_v22_ga4_collection", "request_v22_ga4_collection", "claim_v22_ga4_collection",
    "finish_v22_ga4_collection", "fail_v22_ga4_collection", "pending_v22_ga4_collections", "get_v22_ga4_collection_context",
    "get_v22_ga4_evidence_reviews", "save_v22_ga4_evidence_review",
    "resolve_v22_page_check_input", "get_v22_page_check", "list_v22_page_checks", "request_v22_page_check",
    "claim_v22_page_check", "finish_v22_page_check", "fail_v22_page_check", "expire_v22_page_check",
    "get_v22_page_check_context", "prepare_v22_page_check_dispatch",
    "resolve_v22_review_evidence", "get_v22_review_evidence", "freeze_v22_review_evidence",
    "get_v22_review_workspace_basis", "list_v22_review_workspace",
    "save_v22_review_decision", "get_v22_review_decision", "list_v22_review_decisions",
    "freeze_v22_review_generation_input", "resolve_v22_review_generation_input",
    "complete_v22_review_analysis", "get_v22_completed_review_report", "get_v22_review_report_difference",
    "save_v22_review_delivery", "get_v22_review_delivery", "list_v22_review_delivery_history"})
ROW_RPCS = frozenset({"complete_v22_capability_analysis", "apply_analysis_job_event",
    "list_v22_first_service_preparation_work", "expire_v22_prepared_capability_jobs"})


def validate_database_url(url, environment):
    parsed = urlsplit(url)
    if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1" or not parsed.port
        or parsed.username != "searchtrust_test" or parsed.password or parsed.query or parsed.fragment
        or not DATABASE_NAME.fullmatch(parsed.path.removeprefix("/"))
        or environment.get("SEARCHTRUST_TESTING") != "1"
        or any(environment.get(k) for k in ("DATABASE_URL", "SUPABASE_DB_URL", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID"))):
        raise ValueError("Native tests require the disposable local PostgreSQL endpoint")
    return parsed


class NativeDatabase:
    def __init__(self, url):
        self.parsed = validate_database_url(url, os.environ)
        self.url = url
        self.calls = []

    async def query(self, statement, parameters=(), service_role=False):
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        async with await AsyncConnection.connect(self.url, row_factory=dict_row) as connection:
            if service_role:
                await connection.execute("set local role service_role")
            cursor = await connection.execute(statement, parameters)
            return await cursor.fetchall() if cursor.description else []

    async def rpc(self, name, args):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        if name not in SCALAR_RPCS | ROW_RPCS or any(not re.fullmatch(r"p_[a-z][a-z0-9_]*", key) for key in args):
            raise ValueError("Unsupported native test RPC")
        self.calls.append(name)
        call = sql.SQL("public.{}({})").format(sql.Identifier(name), sql.SQL(",").join(
            sql.SQL("{}=>%s").format(sql.Identifier(key)) for key in args))
        statement = sql.SQL("select {} as value").format(call) if name in SCALAR_RPCS else sql.SQL("select * from {}").format(call)
        rows = await self.query(statement, [Jsonb(value) if isinstance(value, (dict,list)) else value for value in args.values()], True)
        return rows[0]["value"] if name in SCALAR_RPCS else rows

    async def transport(self, request):
        if request.url.host != "local-test.invalid": raise AssertionError("External request blocked")
        try:
            value = await self.rpc(request.url.path.rsplit("/",1)[-1], json.loads(request.content))
            return httpx.Response(200, content=json.dumps(value, default=str), headers={"content-type":"application/json"})
        except Exception as error:
            # Native failures must surface through the production repository,
            # with SQL transactions rolled back before any retry.
            if not hasattr(error, "sqlstate"): raise
            if error.sqlstate == "PT429":
                return httpx.Response(429, json={"code":error.sqlstate,"message":error.diag.message_primary,"details":error.diag.message_detail})
            return httpx.Response(400, json={"code":error.sqlstate,"message":str(error)})


async def seed_first_service(database, redis, prefix, *, clean=False):
    """Only source providers are controlled. Reservation/confirmation use real SQL."""
    from app.api.v2.competitor_models import CompetitorDiscoveryRequest, CompetitorDiscoveryResult
    from app.competitors_v22.discovery_service import competitor_discovery_input_digest
    from app.competitors_v22.market_store import SharedMarketSnapshotStore
    from app.competitors_v22.store import CompetitorDiscoveryStore
    from test_v22_competitor_models import candidate
    m, value = seed(clean)
    owner, case, job, discovery, connection = (uuid4() for _ in range(5))
    identity = value.public_input.business_identity.model_dump(mode="json")
    context = value.public_input.evidence_input.context
    market = context.target_market.model_dump(mode="json")
    await database.query("insert into users(id,clerk_user_id,email) values(%s,%s,%s)",[owner,str(owner),f"{owner}@example.invalid"])
    await database.query("select register_v22_clerk_user(%s,decode(repeat('01',32),'hex'),%s,null)",[str(owner),f"{owner}@example.invalid"])
    await database.query("""insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
        values(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)""",[case,owner,identity["site_url"],identity["normalized_domain"],
            identity["business_name"],json.dumps(identity),identity["operating_model"],m.business.primary_service,json.dumps(market)])
    await database.query("""insert into google_connections(id,user_id,google_subject,granted_scopes,access_token_ciphertext,access_token_iv,access_token_auth_tag,refresh_token_ciphertext,refresh_token_iv,refresh_token_auth_tag,encryption_key_version,token_expires_at)
        values(%s,%s,%s,array['openid','email','profile','https://www.googleapis.com/auth/webmasters.readonly','https://www.googleapis.com/auth/analytics.readonly'],
        decode('01','hex'),decode('02','hex'),decode('03','hex'),decode('04','hex'),decode('05','hex'),decode('06','hex'),'test-key',now()+interval '1 hour')""",[connection,owner,str(owner)])
    private = []
    for saved in value.first_party_input.snapshots:
        s = saved.model_copy(update={"case_id":case,"snapshot_id":uuid4(),"binding_id":uuid4()})
        private.append(s)
        await database.query("""insert into case_source_bindings(id,case_id,connection_id,source_type,external_resource_id,external_resource_name,identity_match_status,health_status,confirmed_by_user_id,confirmed_at)
            values(%s,%s,%s,%s,%s,'Native resource','matched','healthy',%s,now())""",[s.binding_id,case,connection,s.source_type,s.external_resource_id,owner])
        await database.query("""insert into data_snapshots(id,case_id,binding_id,source_type,schema_version,fetched_at,expires_at,sync_trigger,health_status,normalized_payload,payload_checksum,coverage_start,coverage_end,provider_request_context,health_reasons)
            values(%s,%s,%s,%s,%s,%s,%s,'report_generation','healthy',%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            [s.snapshot_id,case,s.binding_id,s.source_type,s.schema_version,s.fetched_at,s.expires_at,canonical_json_bytes(s.normalized_payload).decode(),
                s.payload_checksum,s.coverage_start,s.coverage_end,json.dumps({"external_resource_id":s.external_resource_id}),json.dumps(s.health_reasons)])
    request = CompetitorDiscoveryRequest(case_id=case,business_identity=identity,primary_service=m.business.primary_service,target_market=market,queries=context.queries)
    await database.rpc("reserve_v22_first_service_discovery",dict(p_user_id=str(owner),p_case_id=str(case),p_workflow_id=str(job),
        p_discovery_job_id=str(discovery),p_idempotency_key=str(job),p_request=request.model_dump(mode="json"),
        p_gsc_snapshot_id=str(private[0].snapshot_id),p_ga4_snapshot_id=str(private[1].snapshot_id)))
    await database.rpc("claim_v22_first_service_discovery_dispatch",dict(p_workflow_id=str(job),p_dispatch_token=str(uuid4())))
    await database.rpc("claim_v22_first_service_discovery_run",dict(p_workflow_id=str(job),p_discovery_job_id=str(discovery),p_lease_token=str(uuid4())))
    market_store = SharedMarketSnapshotStore(redis,prefix=prefix,ttl_seconds=3600)
    market_payload = next(s.payload for s in value.public_input.evidence_input.sources if s.kind=="serp")
    shared = await market_store.save(input_digest=competitor_discovery_input_digest(request),snapshot=market_payload,now=now())
    candidates = [candidate(i) for i in range(1,4)]
    result = CompetitorDiscoveryResult(discovery_id=discovery,case_id=case,input_digest=shared.input_digest,
        candidate_digest=request_digest([c.model_dump(mode="json") for c in candidates]),market_snapshot_id=shared.snapshot_id,
        market_snapshot_checksum=shared.snapshot_checksum,candidates=candidates,ready_for_confirmation=True,created_at=now(),expires_at=shared.expires_at)
    found = CompetitorDiscoveryStore(redis,prefix=prefix,state_ttl_seconds=3600)
    await found.register_job(discovery_job_id=discovery,case_id=case,idempotency_key=str(discovery),request_payload=request.model_dump(mode="json"),now=now())
    await found.transition(discovery,status="running",stage="collecting_market",progress=1,message="Controlled provider",now=now())
    await found.transition(discovery,status="succeeded",stage="completed",progress=100,message="Controlled result",result=result,now=now())
    await database.rpc("record_v22_first_service_discovery",dict(p_user_id=str(owner),p_case_id=str(case),p_workflow_id=str(job),p_result=result.model_dump(mode="json")))
    from app.jobs_v22.first_service_collection_models import FirstServiceCollectionEnvelope
    envelope = FirstServiceCollectionEnvelope(schema_version="v22_first_service_collection_v1",collection_request=dict(case_id=case,
        business_identity=identity,primary_service=m.business.primary_service,target_market=market,queries=context.queries,
        competitors=[dict(competitor_id=c.competitor_id,business_name=c.business_name,website_url=c.website_url,public_gbp_url=c.public_gbp_url,
            confirmation_source="user") for c in candidates]),competitor_discovery=dict(discovery_id=discovery,candidate_digest=result.candidate_digest,
                market_snapshot_id=shared.snapshot_id,market_snapshot_checksum=shared.snapshot_checksum))
    confirm = [owner,case,job,discovery,private[0].snapshot_id,private[1].snapshot_id,verified_canonical_json_bytes(envelope.model_dump(mode="json")).decode()]
    return dict(owner=owner,case=case,job=job,confirm=confirm,clean=clean)


class DatabaseSynchronizer:
    def __init__(self, database, store): self.database,self.store=database,store

    async def sync(self, job):
        s = await self.store.require_state(job)
        await self.database.rpc("apply_analysis_job_event",dict(p_job_id=str(s.job_id),p_case_id=str(s.case_id),p_revision=s.revision,
            p_status=s.status,p_current_stage=s.stage,p_progress=s.progress,p_attempt_count=s.attempt_count,
            p_error_code=s.error.error_code if s.error else None,p_user_message=s.message,p_cost_counters=s.cost_counters,
            p_heartbeat_at=s.heartbeat_at.isoformat() if s.heartbeat_at else None,p_completed_at=s.completed_at.isoformat() if s.completed_at else None,
            p_run_generation=s.run_generation,p_deadline_at=s.deadline_at.isoformat()))
        await self.store.mark_callback_synced(job,s.revision)
        return True
