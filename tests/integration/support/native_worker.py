"""ARQ process exercising production preparation/generation with native stores."""
import os
import traceback
from types import SimpleNamespace

import httpx
from arq.connections import RedisSettings
from arq.worker import func

from app.api.v2.runtime import V22JobRuntime
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.selection import RedisDiscoveryVerifier
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.capability_acquisition_worker import prepare_v22_first_service
from app.jobs_v22.capability_executor import CapabilityV22Executor
from app.jobs_v22.capability_first_service_preparation import CapabilityFirstServicePreparer
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.capability_terminal import CapabilityTerminalCoordinator
from app.jobs_v22.cost_models import PricingCatalog, empty_request_prices
from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
from app.jobs_v22.digest import request_digest
from app.jobs_v22.executor import ProspectV22Executor
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.queue import ArqJobQueue
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.worker import execute_v22_job
from support.network_guard import validate_redis_test_environment
from tests.integration.support.native_capability import NativeDatabase, DatabaseSynchronizer
from support.first_service_delivery_bridge import seed
from support.first_service_entry_bridge import freshly_collected, now


def queue_name(prefix): return f"{prefix}:native-queue"
def signal_key(prefix, phase): return f"{prefix}:native:{phase}"


async def make_context(database, redis, prefix, *, clean=False, crash=""):
    _, value = seed(clean)
    sources = {s.kind:s.payload for s in value.public_input.evidence_input.sources}
    found = CompetitorDiscoveryStore(redis,prefix=prefix,state_ttl_seconds=3600)
    markets = SharedMarketSnapshotStore(redis,prefix=prefix,ttl_seconds=3600)
    async def crash_after(phase):
        if crash == phase:
            await redis.set(signal_key(prefix,phase),b"ready",ex=120)
            os._exit(71)  # Intentionally no cleanup: this is the failure being tested.
    async def transport(request):
        name=request.url.path.rsplit("/",1)[-1]
        await redis.incr(f"{prefix}:rpc:{name}")
        response = await database.transport(request)
        if response.status_code==200 and name=="freeze_v22_first_service_preparation": await crash_after("freeze")
        if response.status_code==200 and name=="complete_v22_capability_analysis": await crash_after("commit")
        return response
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    options=dict(url="https://local-test.invalid",service_role_key="local-only",http_client=client)
    repository = SupabaseCapabilityInputRepository(**options)
    store = DurableJobStore(redis,prefix=prefix,state_ttl_seconds=3600)
    queue = ArqJobQueue(redis,queue_name=queue_name(prefix))
    runtime = V22JobRuntime(store=store,queue=queue,redis=redis,capability_repository=repository)

    class Stage:
        def __init__(self, kind): self.kind=kind
        async def collect(self, **args):
            await redis.incr(f"{prefix}:provider:{self.kind}")
            observed_at=now()
            payload=freshly_collected(sources[self.kind],observed_at)
            if self.kind=="competitor":
                payload.job_id=args["job_id"]
                payload.discovery_id=args["discovery"].discovery_id
                payload.candidate_digest=args["discovery"].candidate_digest
                payload.market_snapshot_id=args["shared_market"].snapshot_id
                payload.market_snapshot_checksum=args["shared_market"].snapshot_checksum
            if self.kind=="public_gbp":
                reference=freshly_collected(value.public_input.evidence_input.context.customer_public_gbp,observed_at)
                reference.case_id=args["request"].case_id
                payload.subject_reference_checksum=request_digest(reference)
                return CustomerPublicGbpCollection(reference=reference,snapshot=payload)
            return payload
    async def forbidden_prospect(**kwargs): raise AssertionError("Unexpected intermediate Prospect")
    collector=ProspectV22Executor(discovery_store=found,market_store=markets,site_stage=Stage("site"),
        competitor_stage=Stage("competitor"),customer_public_gbp_stage=Stage("public_gbp"),
        report_pipeline=SimpleNamespace(build=forbidden_prospect))
    preparer=CapabilityFirstServicePreparer(repository=repository,collector=collector,
        discovery_verifier=RedisDiscoveryVerifier(store=found,market_store=markets))
    executor=CapabilityV22Executor(resolver=repository,persister=SupabaseCapabilityResultPersister(**options))
    original_build=executor.service_stage.build
    async def build(**args):
        if crash=="failure":
            raise DeterministicJobError("SYNTHETIC_REPORT_FAILURE","Controlled report generation failure")
        result=await original_build(**args)
        await crash_after("checkpoint")
        return result
    executor.service_stage.build=build
    return dict(redis=redis,store=store,max_attempts=3,state_ttl_seconds=3600,heartbeat_seconds=1,
        lease_seconds=5,cost_pricing=PricingCatalog(revision=1,request_usd_micros=empty_request_prices()),
        capability_first_service_preparer=preparer,capability_submit_dispatch=runtime.submit_capability,
        first_service_preparation_recovery_enabled=True,first_service_preparation_queue_name=queue_name(prefix),
        executor=executor,capability_terminal_coordinator=CapabilityTerminalCoordinator(repository),
        callback_synchronizer=DatabaseSynchronizer(database,store),native_client=client,native_queue=queue)


async def startup(ctx):
    validate_redis_test_environment(os.environ)
    prefix=os.environ["V22_NATIVE_PREFIX"]
    if not prefix.startswith(os.environ["V22_TEST_REDIS_PREFIX"]): raise ValueError("Foreign test prefix")
    ctx.update(await make_context(NativeDatabase(os.environ["V22_NATIVE_DATABASE_URL"]),ctx["redis"],prefix,
        clean=os.environ.get("V22_NATIVE_CLEAN")=="1",crash=os.environ.get("V22_NATIVE_CRASH","")))


async def shutdown(ctx): await ctx["native_client"].aclose()


async def prepare_with_diagnostics(ctx,job,generation):
    try:return await prepare_v22_first_service(ctx,job,generation)
    except Exception as error:
        # This isolated fixture contains only synthetic data. Preserve the
        # wrapped failure location so native integration failures are actionable.
        traceback.print_exception(error.__context__ or error)
        raise


class NativeWorkerSettings:
    functions=[func(prepare_with_diagnostics,name="prepare_v22_first_service",max_tries=3,keep_result=0),func(execute_v22_job,max_tries=3,keep_result=0)]
    redis_settings=RedisSettings.from_dsn(os.environ.get("V22_TEST_REDIS_URL","redis://127.0.0.1:1/15"))
    queue_name=queue_name(os.environ.get("V22_NATIVE_PREFIX","disabled"))
    on_startup=startup
    on_shutdown=shutdown
    max_jobs=2
    job_timeout=120
    keep_result=0
    poll_delay=0.05
    health_check_key=f"{os.environ.get('V22_NATIVE_PREFIX','disabled')}:native-health"
    job_completion_wait=0
