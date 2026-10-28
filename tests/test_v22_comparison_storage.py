import json
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4

import httpx
import pytest

from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_storage import ComparisonRepository, ComparisonSelection
from support.comparison_fixtures import request_raw, parsed

pytestmark = pytest.mark.anyio


def test_comparison_source_timestamps_normalize_non_utc_database_zone():
    raw = request_raw()
    baseline = compare_implementation(parsed(raw))
    for field in ("before", "after"):
        for time in ("fetched_at", "expires_at"):
            raw[field][time] = datetime.fromisoformat(raw[field][time].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    result = compare_implementation(parsed(raw))
    assert result.before_source == baseline.before_source and result.after_source == baseline.after_source
    assert result.metrics == baseline.metrics


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock:
        @staticmethod
        def now(zone): return datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
        fromisoformat = datetime.fromisoformat
    monkeypatch.setattr("app.report_v22.comparison_storage.datetime", Clock)


def fixture():
    raw = request_raw()
    record = raw["implementation"]
    selection = ComparisonSelection.model_validate_json(json.dumps(dict(
        implementation={k: record[k] for k in ("id", "revision", "checksum")},
        action_id=raw["action_id"], days=28, scope=raw["scope"], concurrent_changes="unknown",
        before_snapshot_id=raw["before"]["snapshot_id"], after_snapshot_id=raw["after"]["snapshot_id"])))
    identity = dict(user_id=UUID(record["user_id"]), case_id=UUID(record["case_id"]), comparison_id=uuid4())
    return raw, selection, identity


def envelope(raw, selection, identity):
    request = parsed(raw)
    return dict(schema_version="v22_frozen_comparison_v1", id=str(identity["comparison_id"]),
        user_id=str(identity["user_id"]), case_id=str(identity["case_id"]),
        selection=selection.model_dump(mode="json"), request=request.model_dump(mode="json"),
        result=compare_implementation(request).model_dump(mode="json"))


async def test_resolve_freeze_replay_and_ids_only_boundary(monkeypatch):
    raw, selection, identity = fixture()
    calls = []
    stored = None
    def handler(req):
        nonlocal stored
        body = json.loads(req.content)
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append((name, body))
        assert req.headers["authorization"] == "Bearer secret"
        if name == "get_v22_comparison":
            return httpx.Response(200, content=json.dumps(stored))
        if name == "resolve_v22_comparison_input":
            assert set(body) == {"p_user_id", "p_case_id", "p_selection", "p_evaluated_at"}
            assert "normalized_payload" not in json.dumps(body)
            raw["evaluated_at"] = body["p_evaluated_at"].replace("+00:00", "Z")
            # Keep fixtures current without real data collection.
            raw["after"]["expires_at"] = "2099-01-01T00:00:00Z"
            return httpx.Response(200, json=raw)
        assert name == "freeze_v22_comparison"
        assert json.loads(body["p_canonical_request"]) == body["p_request"]
        assert json.loads(body["p_canonical_result"]) == {k:v for k,v in body["p_result"].items() if k != "checksum"}
        stored = envelope(body["p_request"], selection, identity)
        return httpx.Response(200, json=stored)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        first = await repo.freeze(**identity, selection=selection)
        assert await repo.freeze(**identity, selection=selection) == first
        assert [c[0] for c in calls] == ["get_v22_comparison", "resolve_v22_comparison_input", "freeze_v22_comparison", "get_v22_comparison"]
        assert first.result.effect_conclusion is None
        with pytest.raises(DeterministicJobError, match="V22_COMPARISON_CONFLICT"):
            await repo.freeze(**identity, selection=selection.model_copy(update={"days": 7}))


@pytest.mark.parametrize("change", ["metric", "window", "owner", "case", "id", "selection", "source", "checksum"])
async def test_read_rejects_tampered_or_re_signed_comparison(change):
    raw, selection, identity = fixture()
    value = envelope(raw, selection, identity)
    if change == "metric": value["result"]["metrics"][0]["after"] += 1
    elif change == "window": value["result"]["windows"]["timezone"] = "UTC"
    elif change == "owner": value["user_id"] = str(uuid4())
    elif change == "case": value["case_id"] = str(uuid4())
    elif change == "id": value["id"] = str(uuid4())
    elif change == "selection": value["selection"]["days"] = 7
    elif change == "source": value["request"]["before"]["normalized_payload"]["resource_id"] = "sc-domain:other.test"
    if change != "checksum":
        value["result"]["checksum"] = verified_request_digest({k:v for k,v in value["result"].items() if k != "checksum"})
    else: value["result"]["checksum"] = "sha256:"+"0"*64
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(DeterministicJobError, match="V22_COMPARISON_INVALID"):
            await repo.read(**identity)


@pytest.mark.parametrize("status,error", [(503, TransientJobError), (429, TransientJobError),
    (403, DeterministicJobError), (409, DeterministicJobError), (302, DeterministicJobError)])
async def test_storage_status_sanitized(status, error):
    _, _, identity = fixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="private provider secret"))) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(error) as caught: await repo.read(**identity)
        assert "private provider" not in str(caught.value)


async def test_response_budget_and_network_error(monkeypatch):
    _, _, identity = fixture()
    monkeypatch.setattr("app.report_v22.comparison_storage.MAX_RESPONSE_BYTES", 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"*11))) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(DeterministicJobError, match="V22_COMPARISON_INVALID"): await repo.read(**identity)
    def unavailable(request): raise httpx.ReadTimeout("secret endpoint", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(TransientJobError, match="V22_COMPARISON_UNAVAILABLE"): await repo.read(**identity)


@pytest.mark.parametrize("field", ["case_id", "before_snapshot_id", "evaluated_at"])
async def test_resolver_rejects_wrong_binding(field):
    raw, selection, identity = fixture()
    def response(req):
        value = deepcopy(raw)
        value["after"]["expires_at"] = "2099-01-01T00:00:00Z"
        value["evaluated_at"] = json.loads(req.content)["p_evaluated_at"].replace("+00:00", "Z")
        if field == "case_id": value["implementation"]["case_id"] = str(uuid4())
        elif field == "before_snapshot_id": value["before"]["snapshot_id"] = str(uuid4())
        else: value["evaluated_at"] = "2026-10-22T08:00:00Z"
        return httpx.Response(200, json=value)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        repo = ComparisonRepository(url="https://storage.example", service_role_key="secret", http_client=client)
        with pytest.raises(DeterministicJobError, match="V22_COMPARISON_INVALID"):
            await repo.resolve(user_id=identity["user_id"], case_id=identity["case_id"], selection=selection)
