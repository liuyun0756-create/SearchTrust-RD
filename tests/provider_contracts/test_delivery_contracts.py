import httpx
import pytest
import json
from pathlib import Path

from app.jobs_v22.result_persistence import SupabaseResultPersister
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.callbacks import SignedCallbackClient
from test_v22_job_callbacks import build_store
from test_v22_result_persistence import JOB_ID, inputs


pytestmark = pytest.mark.contract


@pytest.mark.anyio
async def test_verified_service_role_rpc_delivery_contracts():
    from test_v22_verified_input_resolver import resolve_response, resolved_payload
    from test_v22_verified_result_persistence import persist_response, persistence_inputs

    payload = resolved_payload()
    _, resolve_calls = await resolve_response(httpx.Response(200, json=payload), payload=payload)
    values = persistence_inputs()
    persist_calls = await persist_response(httpx.Response(200, json=[{
        "report_id": str(values[1].job_id), "idempotent": False}]), values=values)
    manifest = json.loads((Path(__file__).parents[1] / "fixtures/provider_contracts/manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["operations"]}
    for operation, calls, fields, limit in [
        ("delivery.verified_input_rpc", resolve_calls, ["p_job_id", "p_case_id", "p_run_generation"], 25_000_000),
        ("delivery.verified_result_rpc", persist_calls, ["p_job_id", "p_case_id", "p_report_payload", "p_run_generation"], 65_536),
    ]:
        entry = entries[operation]
        assert entry["method"] == calls[0].method == "POST"
        assert entry["path"] == calls[0].url.path
        assert entry["auth"] == {"role": "service_role", "headers": ["authorization", "apikey"]}
        assert entry["request_fields"] == fields
        assert set(json.loads(calls[0].content)) == set(fields)
        assert entry["max_response_bytes"] == limit
        assert entry["oauth_token_fields"] == []
        assert entry["response_contract"]
        assert "access_token" not in calls[0].content.decode()
        assert "refresh_token" not in calls[0].content.decode()


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
