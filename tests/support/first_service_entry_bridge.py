"""Isolated entry-to-delivery acceptance bridge; never a production entrypoint.

Only discovery/provider outputs and physical queue transport are synthetic.
SQL leases, Redis stores, source-only orchestration, input freezing, runtime,
formal worker, report rules and settlement are the real application code.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.first_service_delivery_bridge import seed, emit
import httpx
from arq import Retry
from fakeredis.aioredis import FakeRedis
from app.api.v2.runtime import V22JobRuntime
from app.api.v2.competitor_models import CompetitorDiscoveryResult
from app.competitors_v22.market_store import SharedMarketSnapshotStore
from app.competitors_v22.discovery_service import competitor_discovery_input_digest
from app.competitors_v22.selection import RedisDiscoveryVerifier
from app.competitors_v22.store import CompetitorDiscoveryStore
from app.jobs_v22.capability_acquisition_worker import prepare_v22_first_service
from app.jobs_v22.capability_executor import CapabilityV22Executor
from app.jobs_v22.capability_first_service_preparation import CapabilityFirstServicePreparer
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.cost_models import PricingCatalog, empty_request_prices
from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.executor import ProspectV22Executor
from app.jobs_v22.errors import TransientJobError
from app.jobs_v22.first_service_discovery_repository import FirstServiceDiscoveryRepository
from app.jobs_v22.first_service_preparation_reconciler import reconcile_v22_first_service_preparation
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.worker import execute_v22_job
from test_v22_competitor_models import candidate


def now():
    return datetime.now(timezone.utc)


def freshly_collected(model, timestamp):
    """Instantiate new synthetic provider output, not refresh stored evidence."""
    def move(value, key=""):
        if isinstance(value, dict): return {k: move(v, k) for k, v in value.items()}
        if isinstance(value, list): return [move(v) for v in value]
        if isinstance(value, str) and len(value) > 19 and value[10:11] == "T":
            return (timestamp + (timedelta(days=1) if key == "expires_at" else timedelta())).isoformat()
        return value
    return type(model).model_validate_json(canonical_json_bytes(move(model.model_dump(mode="json"))))


class SourceStage:
    def __init__(self, produce, fail_once=False):
        self.produce, self.calls, self.fail_once = produce, 0, fail_once

    async def collect(self, **kwargs):
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise TransientJobError("SYNTHETIC_SOURCE_UNAVAILABLE", "Synthetic provider temporarily unavailable")
        return self.produce(kwargs)


class QueueTransport:
    def __init__(self, lose_ack):
        self.items, self.lose_ack = [], lose_ack

    async def enqueue(self, job, generation):
        self.items.append((job, generation))
        if self.lose_ack:
            self.lose_ack = False
            raise RuntimeError("Injected queue acknowledgement loss after acceptance")
        return True


async def main():
    command = json.loads(input())
    manifest, value = seed(command["clean"])
    # The older report-rule fixture uses UUID(int=903); browser ownership gates
    # deliberately require an RFC 4122 UUID. Only seed identity is reused here.
    owner = uuid4()
    manifest = manifest.model_copy(update={"user_id": owner,
        "business": manifest.business.model_copy(update={"user_id": owner})})
    emit(dict(kind="seed", manifest=manifest.model_dump(mode="json"), request=value.model_dump(mode="json"),
        private_payloads={str(s.snapshot_id): canonical_json_bytes(s.normalized_payload).decode()
            for s in value.first_party_input.snapshots}))
    calls = []
    async def transport(sent):
        if sent.url.host != "local-test.invalid": raise AssertionError("External request blocked")
        rpc = sent.url.path.rsplit("/", 1)[-1]
        calls.append(rpc)
        emit(dict(kind="rpc", rpc=rpc, args=json.loads(sent.content)))
        response = json.loads(input())
        return httpx.Response(response["status"], content=response["body_text"],
            headers={"content-type": "application/json"})

    redis = FakeRedis()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            options = dict(url="https://local-test.invalid", service_role_key="local-only", http_client=client)
            repo = SupabaseCapabilityInputRepository(**options)
            discovery_repo = FirstServiceDiscoveryRepository(**options)
            discovery_store = CompetitorDiscoveryStore(redis, prefix="test:entry", state_ttl_seconds=3600)
            market_store = SharedMarketSnapshotStore(redis, prefix="test:entry", ttl_seconds=3600)
            sources = {s.kind: s.payload for s in value.public_input.evidence_input.sources}

            command_discover = json.loads(input())
            assert command_discover["kind"] == "discover"
            workflow = UUID(command_discover["workflow"])
            work = await discovery_repo.claim(workflow, uuid4())
            work = await discovery_repo.claim(workflow, uuid4(), discovery_job_id=work.discovery_job_id)
            assert work.request.queries == sources["serp"].queries
            shared = await market_store.save(input_digest=competitor_discovery_input_digest(work.request), snapshot=sources["serp"], now=now())
            candidates = [candidate(i) for i in range(1, 4)]
            result = CompetitorDiscoveryResult(discovery_id=work.discovery_job_id, case_id=work.case_id,
                input_digest=shared.input_digest, candidate_digest=request_digest([c.model_dump(mode="json") for c in candidates]),
                market_snapshot_id=shared.snapshot_id, market_snapshot_checksum=shared.snapshot_checksum,
                candidates=candidates, ready_for_confirmation=True, created_at=now(), expires_at=shared.expires_at)
            await discovery_store.register_job(discovery_job_id=work.discovery_job_id, case_id=work.case_id,
                idempotency_key="local-discovery", request_payload=work.request.model_dump(mode="json"), now=now())
            await discovery_store.transition(work.discovery_job_id, status="running", stage="collecting_market",
                progress=1, message="Synthetic provider started", now=now())
            await discovery_store.transition(work.discovery_job_id, status="succeeded", stage="completed",
                progress=100, message="Synthetic provider completed", result=result, now=now())
            await discovery_repo.finish(work, result)
            emit(dict(kind="discovered"))

            assert json.loads(input()) == {"kind": "run"}
            site = SourceStage(lambda _: freshly_collected(sources["site"], now()), command["loss"] == "collection")
            def rivals_output(args):
                payload = freshly_collected(sources["competitor"], now())
                payload.job_id = args["job_id"]
                payload.discovery_id = result.discovery_id
                payload.candidate_digest = result.candidate_digest
                payload.market_snapshot_id = shared.snapshot_id
                payload.market_snapshot_checksum = shared.snapshot_checksum
                return payload
            rivals = SourceStage(rivals_output)
            def gbp_output(_):
                reference = freshly_collected(value.public_input.evidence_input.context.customer_public_gbp, now())
                snapshot = freshly_collected(sources["public_gbp"], now())
                snapshot.subject_reference_checksum = request_digest(reference)
                return CustomerPublicGbpCollection(reference=reference, snapshot=snapshot)
            gbp = SourceStage(gbp_output)
            async def forbid_prospect(**kwargs): raise AssertionError("Must not create an intermediate Prospect")
            collector = ProspectV22Executor(discovery_store=discovery_store, market_store=market_store,
                site_stage=site, competitor_stage=rivals, customer_public_gbp_stage=gbp,
                report_pipeline=SimpleNamespace(build=forbid_prospect))
            preparer = CapabilityFirstServicePreparer(repository=repo, collector=collector,
                discovery_verifier=RedisDiscoveryVerifier(store=discovery_store, market_store=market_store))
            store = DurableJobStore(redis, prefix="test:entry", state_ttl_seconds=3600)
            queue = QueueTransport(command["loss"] == "handoff")
            runtime = V22JobRuntime(store=store, queue=queue, redis=redis, capability_repository=repo)
            publications = []
            async def enqueue_job(*args, **kwargs): publications.append((args, kwargs))
            redis.enqueue_job = enqueue_job
            ctx = dict(redis=redis, store=store, max_attempts=3, state_ttl_seconds=3600, heartbeat_seconds=5,
                cost_pricing=PricingCatalog(revision=1, request_usd_micros=empty_request_prices()),
                capability_first_service_preparer=preparer, capability_submit_dispatch=runtime.submit_capability,
                first_service_preparation_recovery_enabled=True, first_service_preparation_queue_name="local-test",
                executor=CapabilityV22Executor(resolver=repo, persister=SupabaseCapabilityResultPersister(**options)))
            await reconcile_v22_first_service_preparation(ctx)
            assert len(publications) == 1
            name, job, generation = publications[0][0]
            assert name == "prepare_v22_first_service" and UUID(job) == workflow
            recovered = False
            try:
                await prepare_v22_first_service(ctx, job, generation)
            except Retry as error:
                if command["loss"] == "none":
                    import traceback
                    traceback.print_exception(error.__context__)
                    raise
                recovered = True
                if command["loss"] == "collection":
                    # Advance only the test lease, never the task deadline or
                    # evidence dates. No real-time sleep or new reservation.
                    emit(dict(kind="expire_test_lease", job=job))
                    assert json.loads(input()) == {"kind": "lease_expired"}
                await reconcile_v22_first_service_preparation(ctx)
                assert len(publications) == 2
                assert publications[1][0] == publications[0][0]
                await prepare_v22_first_service(ctx, job, generation)
            registered = await store.require_state(UUID(job))
            assert registered.status == "queued" and registered.attempt_count == (2 if command["loss"] == "collection" else 1)
            assert queue.items and all(item == (UUID(job), generation) for item in queue.items)
            for queued_job, queued_generation in queue.items:
                await execute_v22_job(ctx, str(queued_job), queued_generation)
            state = await store.require_state(UUID(job))
            assert state.status == "succeeded", state.model_dump_json()
            assert [site.calls, rivals.calls, gbp.calls] == [2 if command["loss"] == "collection" else 1, 1, 1]
            emit(dict(kind="result", report=state.report.model_dump(mode="json"), calls=calls,
                recovered=recovered, provider_calls=[site.calls,rivals.calls,gbp.calls],
                publications=len(publications), queue_deliveries=len(queue.items),
                created_at=state.created_at.isoformat(), deadline_at=state.deadline_at.isoformat()))
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
