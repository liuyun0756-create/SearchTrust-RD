import httpx
import pytest

from app.jobs_v22.result_persistence import SupabaseResultPersister
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.callbacks import SignedCallbackClient
from test_v22_job_callbacks import build_store
from test_v22_result_persistence import JOB_ID, inputs


pytestmark = pytest.mark.contract


@pytest.mark.anyio
async def test_supabase_ack_fixture_matches_result_delivery_contract(provider_fixture) -> None:
    fixture = provider_fixture("delivery/supabase_ack.json")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        persister = SupabaseResultPersister(
            url="https://supabase.example.test",
            service_role_key="fixture-key-value",
            http_client=client,
        )
        request, site, shared, competitor, report = inputs()
        await persister.persist(
            job_id=JOB_ID,
            request=request,
            site_inventory=site,
            shared_market=shared,
            competitor_collection=competitor,
            report=report,
        )

    assert requests[0].url.path == "/rest/v1/rpc/persist_v22_prospect_result"
    assert requests[0].headers["authorization"] == "Bearer fixture-key-value"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "error_type", "error_code"),
    [
        (httpx.Response(400, text="fixture-key-value"), DeterministicJobError, "V22_RESULT_PERSISTENCE_REJECTED"),
        (httpx.Response(429, text="fixture-key-value"), TransientJobError, "V22_RESULT_PERSISTENCE_UNAVAILABLE"),
        (httpx.Response(503, text="fixture-key-value"), TransientJobError, "V22_RESULT_PERSISTENCE_UNAVAILABLE"),
        (httpx.Response(200, text="not-json"), TransientJobError, "V22_RESULT_PERSISTENCE_INVALID_RESPONSE"),
        (httpx.Response(200, json=[{"report_id": str(JOB_ID), "padding": "x" * 65_536}]), TransientJobError, "V22_RESULT_PERSISTENCE_INVALID_RESPONSE"),
    ],
)
async def test_result_delivery_errors_are_stable_and_secret_safe(
    response, error_type, error_code,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        persister = SupabaseResultPersister(
            url="https://supabase.example.test",
            service_role_key="fixture-key-value",
            http_client=client,
        )
        request, site, shared, competitor, report = inputs()
        with pytest.raises(error_type) as raised:
            await persister.persist(
                job_id=JOB_ID,
                request=request,
                site_inventory=site,
                shared_market=shared,
                competitor_collection=competitor,
                report=report,
            )
    assert raised.value.error_code == error_code
    assert "fixture-key-value" not in str(raised.value)


@pytest.mark.anyio
async def test_callback_ack_fixture_matches_signed_delivery_contract(provider_fixture) -> None:
    fixture = provider_fixture("delivery/callback_ack.json")
    store = await build_store()
    state = await store.require_state(JOB_ID)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(fixture["status_code"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        delivered = await SignedCallbackClient(
            url="https://frontend.example.test/api/internal/v2/job-events",
            secret="fixture-callback-value",
            http_client=client,
            clock=lambda: 1787817600,
        ).send(state)

    assert delivered is True
    assert requests[0].headers["x-searchtrust-event-id"] == f"{JOB_ID}:1"
    assert requests[0].headers["x-searchtrust-signature"].startswith("sha256=")
    assert "fixture-callback-value" not in requests[0].content.decode()
