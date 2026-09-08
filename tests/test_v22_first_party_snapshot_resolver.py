import json

import httpx
import pytest

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.first_party_snapshot_resolver import FirstPartySnapshotResolver
from test_v22_first_party_findings import CASE_ID, NOW, PARENT_ID, ga4_value, gsc_value, request, trusted


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_resolver_posts_ids_only_and_validates_trusted_response() -> None:
    expected = request(trusted("gsc", gsc_value(), 80), trusted("ga4", ga4_value(), 81))
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=expected.model_dump_json())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = FirstPartySnapshotResolver(url="https://storage.example", service_role_key="secret", http_client=client)
        result = await resolver.resolve(case_id=CASE_ID, parent_report_id=PARENT_ID,
            gsc_snapshot_id=expected.snapshots[0].snapshot_id, ga4_snapshot_id=expected.snapshots[1].snapshot_id,
            gbp_snapshot_id=None, evaluated_at=expected.evaluated_at)
    assert result == expected
    body = json.loads(calls[0].content)
    assert set(body) == {"p_case_id", "p_parent_report_id", "p_gsc_snapshot_id", "p_ga4_snapshot_id",
        "p_gbp_snapshot_id", "p_evaluated_at"}
    assert "normalized_payload" not in body and "raw_payload" not in body
    assert calls[0].url.path.endswith("/rpc/resolve_v22_first_party_findings_input")


@pytest.mark.anyio
@pytest.mark.parametrize("status,error", [(503, TransientJobError), (429, TransientJobError),
    (403, DeterministicJobError), (409, DeterministicJobError)])
async def test_resolver_classifies_storage_failures_without_echoing_body(status, error) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json={"secret": "must-not-escape"}))) as client:
        resolver = FirstPartySnapshotResolver(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(error) as captured:
            await resolver.resolve(case_id=CASE_ID, parent_report_id=PARENT_ID,
                gsc_snapshot_id=trusted("gsc", gsc_value(), 90).snapshot_id,
                ga4_snapshot_id=trusted("ga4", ga4_value(), 91).snapshot_id,
                gbp_snapshot_id=None, evaluated_at=NOW)
    assert "must-not-escape" not in captured.value.user_message


@pytest.mark.anyio
async def test_resolver_rejects_inconsistent_response_identity() -> None:
    expected = request(trusted("gsc", gsc_value(), 92), trusted("ga4", ga4_value(), 93))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=expected.model_dump_json()))) as client:
        resolver = FirstPartySnapshotResolver(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(DeterministicJobError, match="V22_FIRST_PARTY_INPUT_INVALID"):
            await resolver.resolve(case_id=CASE_ID, parent_report_id=PARENT_ID,
                gsc_snapshot_id=expected.snapshots[1].snapshot_id,
                ga4_snapshot_id=expected.snapshots[0].snapshot_id,
                gbp_snapshot_id=None, evaluated_at=expected.evaluated_at)


@pytest.mark.anyio
async def test_resolver_rejects_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr("app.report_v22.first_party_snapshot_resolver.MAX_RESPONSE_BYTES", 100)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * 101))) as client:
        resolver = FirstPartySnapshotResolver(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(DeterministicJobError, match="V22_FIRST_PARTY_INPUT_INVALID"):
            await resolver.resolve(case_id=CASE_ID, parent_report_id=PARENT_ID,
                gsc_snapshot_id=trusted("gsc", gsc_value(), 94).snapshot_id,
                ga4_snapshot_id=trusted("ga4", ga4_value(), 95).snapshot_id,
                gbp_snapshot_id=None, evaluated_at=NOW)
