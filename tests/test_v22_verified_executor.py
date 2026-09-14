from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import UUID

import fakeredis.aioredis
import httpx
import pytest

from app.api.v2.models import AnalyzeRequest
from app.competitors_v22.selection import AnalysisDiscoveryLink, AnalysisRequestEnvelope
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.verified_executor import VerifiedV22Executor, V22ExecutorRouter
from app.jobs_v22.verified_models import VerifiedRequestEnvelope
from app.jobs_v22.verified_report_pipeline import VerifiedReportPipeline
from app.jobs_v22.verified_result_persistence import SupabaseVerifiedResultPersister
from test_api_v2_jobs import prospect_analyze_payload
from verified_pipeline_helpers import JOB_ID, VERIFIED_AT, resolve_fixture


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def prospect_envelope() -> AnalysisRequestEnvelope:
    return AnalysisRequestEnvelope(
        schema_version="v22_analysis_request_envelope_v1",
        analyze_request=AnalyzeRequest.model_validate_json(
            json.dumps(prospect_analyze_payload())
        ),
        competitor_discovery=AnalysisDiscoveryLink(
            discovery_id=UUID("22222222-2222-4222-8222-222222222222"),
            candidate_digest="sha256:" + "a" * 64,
            market_snapshot_id=UUID("44444444-4444-4444-8444-444444444444"),
            market_snapshot_checksum="sha256:" + "b" * 64,
        ),
    )


@pytest.mark.anyio
async def test_router_sends_only_exact_prospect_or_verified_schema() -> None:
    resolved, verified_request = await resolve_fixture()
    prospect = AsyncMock()
    verified = AsyncMock()
    router = V22ExecutorRouter(prospect_executor=prospect, verified_executor=verified)
    checkpoints = JobCheckpoints(
        fakeredis.aioredis.FakeRedis(), prefix="test:router", ttl_seconds=3600
    )

    await router.execute(
        job_id=JOB_ID,
        request=prospect_envelope().model_dump(mode="json"),
        submitted_at=VERIFIED_AT,
        checkpoints=checkpoints,
    )
    prospect.execute.assert_awaited_once()
    verified.execute.assert_not_awaited()
    prospect.reset_mock()

    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=verified_request,
    )
    verified.execute.return_value = resolved.parent_report
    await router.execute(
        job_id=JOB_ID,
        request=envelope.model_dump(mode="json"),
        submitted_at=VERIFIED_AT,
        checkpoints=checkpoints,
    )
    verified.execute.assert_awaited_once()
    prospect.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "v22_unknown_envelope_v1"},
        {"schema_version": "v22_verified_request_envelope_v1"},
        {"schema_version": ["v22_verified_request_envelope_v1"]},
    ],
)
async def test_router_fails_closed_for_unknown_or_malformed_envelopes(payload) -> None:
    prospect = AsyncMock()
    verified = AsyncMock()
    router = V22ExecutorRouter(prospect_executor=prospect, verified_executor=verified)

    with pytest.raises(DeterministicJobError, match="V22_ANALYSIS_REQUEST_INVALID"):
        await router.execute(
            job_id=JOB_ID,
            request=payload,
            submitted_at=VERIFIED_AT,
            checkpoints=JobCheckpoints(
                fakeredis.aioredis.FakeRedis(),
                prefix="test:router:invalid",
                ttl_seconds=3600,
            ),
        )

    prospect.execute.assert_not_awaited()
    verified.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_verified_executor_preserves_trusted_capability_and_order() -> None:
    resolved, request = await resolve_fixture(run_generation=7)
    report = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID,
        request=request,
        resolved_input=resolved,
        checkpoints=JobCheckpoints(
            fakeredis.aioredis.FakeRedis(),
            prefix="test:verified:fixture",
            ttl_seconds=3600,
            run_generation=7,
        ),
    )
    calls: list[str] = []

    class Resolver:
        async def resolve(self, **kwargs):
            calls.append("resolve")
            assert kwargs == {
                "job_id": JOB_ID,
                "request": request,
                "run_generation": 7,
            }
            return resolved

    class Pipeline:
        async def build(self, **kwargs):
            calls.append("pipeline")
            assert kwargs["resolved_input"] is resolved
            assert kwargs["request"] == request
            return report

    class Persister:
        async def persist(self, **kwargs):
            calls.append("persist")
            assert kwargs["resolved_input"] is resolved
            assert kwargs["request"] == request
            assert kwargs["report"] is report
            assert kwargs["run_generation"] == 7

    executor = VerifiedV22Executor(
        resolver=Resolver(), pipeline=Pipeline(), persister=Persister()
    )
    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=request,
    )
    result = await executor.execute(
        job_id=JOB_ID,
        request=envelope.model_dump(mode="json"),
        submitted_at=VERIFIED_AT,
        checkpoints=JobCheckpoints(
            fakeredis.aioredis.FakeRedis(),
            prefix="test:verified:executor",
            ttl_seconds=3600,
            run_generation=7,
        ),
    )

    assert result is report
    assert calls == ["resolve", "pipeline", "persist"]


@pytest.mark.anyio
async def test_verified_executor_re_resolves_on_restart_instead_of_reconstructing_trust() -> None:
    first, request = await resolve_fixture(run_generation=2)
    second, _ = await resolve_fixture(run_generation=2)
    report = await VerifiedReportPipeline(clock=lambda: VERIFIED_AT).build(
        job_id=JOB_ID,
        request=request,
        resolved_input=first,
        checkpoints=JobCheckpoints(
            fakeredis.aioredis.FakeRedis(),
            prefix="test:verified:restart:fixture",
            ttl_seconds=3600,
            run_generation=2,
        ),
    )
    resolver = AsyncMock()
    resolver.resolve.side_effect = [first, second]
    pipeline = AsyncMock()
    pipeline.build.return_value = report
    persister = AsyncMock()
    executor = VerifiedV22Executor(
        resolver=resolver, pipeline=pipeline, persister=persister
    )
    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=request,
    ).model_dump(mode="json")
    checkpoints = JobCheckpoints(
        fakeredis.aioredis.FakeRedis(),
        prefix="test:verified:restart",
        ttl_seconds=3600,
        run_generation=2,
    )

    await executor.execute(
        job_id=JOB_ID,
        request=envelope,
        submitted_at=VERIFIED_AT,
        checkpoints=checkpoints,
    )
    await executor.execute(
        job_id=JOB_ID,
        request=envelope,
        submitted_at=VERIFIED_AT,
        checkpoints=checkpoints,
    )

    assert first is not second
    assert pipeline.build.await_args_list[0].kwargs["resolved_input"] is first
    assert pipeline.build.await_args_list[1].kwargs["resolved_input"] is second
    assert persister.persist.await_args_list[0].kwargs["resolved_input"] is first
    assert persister.persist.await_args_list[1].kwargs["resolved_input"] is second


@pytest.mark.anyio
async def test_verified_executor_rejects_a_capability_sealed_for_an_older_generation() -> None:
    resolved, request = await resolve_fixture(run_generation=1)
    pipeline = AsyncMock()
    persister = AsyncMock()
    executor = VerifiedV22Executor(
        resolver=AsyncMock(resolve=AsyncMock(return_value=resolved)),
        pipeline=pipeline,
        persister=persister,
    )
    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=request,
    ).model_dump(mode="json")

    with pytest.raises(DeterministicJobError, match="V22_VERIFIED_INPUT_INVALID"):
        await executor.execute(
            job_id=JOB_ID,
            request=envelope,
            submitted_at=VERIFIED_AT,
            checkpoints=JobCheckpoints(
                fakeredis.aioredis.FakeRedis(),
                prefix="test:verified:stale-capability",
                ttl_seconds=3600,
                run_generation=2,
            ),
        )

    pipeline.build.assert_not_awaited()
    persister.persist.assert_not_awaited()


@pytest.mark.anyio
async def test_verified_executor_retries_the_same_atomic_persist_after_response_loss() -> None:
    first, request = await resolve_fixture(run_generation=3)
    second, _ = await resolve_fixture(run_generation=3)
    resolver = AsyncMock()
    resolver.resolve.side_effect = [first, second]
    sent_payloads: list[dict] = []

    async def respond(http_request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(http_request.content))
        if len(sent_payloads) == 1:
            # The database transaction may already have committed; only its HTTP
            # acknowledgement was lost. Retrying must address that same paid job.
            raise httpx.ReadTimeout("response lost after commit", request=http_request)
        return httpx.Response(
            200,
            json=[{"report_id": str(JOB_ID), "idempotent": True}],
            request=http_request,
        )

    redis = fakeredis.aioredis.FakeRedis()
    checkpoints = JobCheckpoints(
        redis,
        prefix="test:verified:atomic-response-loss",
        ttl_seconds=3600,
        run_generation=3,
    )
    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=request,
    ).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = VerifiedV22Executor(
            resolver=resolver,
            pipeline=VerifiedReportPipeline(clock=lambda: VERIFIED_AT),
            persister=SupabaseVerifiedResultPersister(
                url="https://project.supabase.co",
                service_role_key="fake-service",
                http_client=client,
            ),
        )
        with pytest.raises(
            TransientJobError, match="V22_VERIFIED_RESULT_PERSISTENCE_UNAVAILABLE"
        ):
            await executor.execute(
                job_id=JOB_ID,
                request=envelope,
                submitted_at=VERIFIED_AT,
                checkpoints=checkpoints,
            )
        report = await executor.execute(
            job_id=JOB_ID,
            request=envelope,
            submitted_at=VERIFIED_AT,
            checkpoints=checkpoints,
        )

    assert report.report_version.report_type == "verified_execution"
    assert resolver.resolve.await_count == 2
    assert len(sent_payloads) == 2
    assert sent_payloads[0] == sent_payloads[1]
    assert sent_payloads[0]["p_job_id"] == str(JOB_ID)
    assert sent_payloads[0]["p_run_generation"] == 3


@pytest.mark.anyio
async def test_verified_executor_rejects_job_id_reused_as_frozen_source() -> None:
    _, request = await resolve_fixture()
    request = request.model_copy(update={"gsc_snapshot_id": JOB_ID})
    envelope = VerifiedRequestEnvelope(
        schema_version="v22_verified_request_envelope_v1",
        verified_request=request,
    )
    resolver = AsyncMock()
    executor = VerifiedV22Executor(
        resolver=resolver, pipeline=AsyncMock(), persister=AsyncMock()
    )

    with pytest.raises(DeterministicJobError, match="V22_ANALYSIS_REQUEST_INVALID"):
        await executor.execute(
            job_id=JOB_ID,
            request=envelope.model_dump(mode="json"),
            submitted_at=VERIFIED_AT,
            checkpoints=JobCheckpoints(
                fakeredis.aioredis.FakeRedis(),
                prefix="test:verified:identity",
                ttl_seconds=3600,
            ),
        )

    resolver.resolve.assert_not_awaited()
