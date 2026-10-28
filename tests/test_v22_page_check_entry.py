"""Owner entry, default-off routing, safe rate errors and dispatch boundaries."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.v2.page_checks import router, page_check_service
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.page_check_entry import PageCheckCommand, PageCheckEntry
from app.report_v22.page_check_limits import PageCheckLimits, PageCheckRateLimited
from app.report_v22.page_check_storage import PageCheckRepository
from app.report_v22.page_check_worker import reconcile_v22_page_checks, execute_v22_page_check
from test_v22_page_check_storage import job_payload, checked, resolved

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend(): return "asyncio"


def command(job, operation="context"):
    return PageCheckCommand(operation=operation, user_id=job.user_id, case_id=job.case_id,
        report_id=job.report_id, delivery_revision_id=job.delivery_revision_id,
        check_id=None if operation == "context" else job.id, selection=job.selection if operation == "request" else None)


def limits(**changes):
    return dict(used=0, active=False, server_time="2026-10-23T01:00:00Z", next_request_at="2026-10-23T01:00:00Z", reason=None) | changes


def setup():
    job = checked(job_payload())
    data = dict(record=resolved()["record"], actions=[dict(id=e["action_id"], title="Service page") for e in resolved()["record"]["entries"]],
        site_url=job.plan.binding.site_url, eligible=True, delivery_revision=job.plan.binding.delivery_revision,
        history=[{k: v for k, v in job.model_dump(mode="json").items() if k in ("id", "user_id", "case_id", "report_id", "delivery_revision_id", "status", "created_at", "completed_at", "error_code")}], limits=limits())
    repo = SimpleNamespace(_rpc=AsyncMock(return_value=json.dumps(data)), read_check=AsyncMock(return_value=job), request_check=AsyncMock(return_value=job))
    return job, data, repo


@pytest.mark.parametrize("change", ["valid", "stale", "no_record", "no_actions", "foreign", "duplicate_history", "duplicate_action", "extra", "bad_digest"])
async def test_context_is_bound_and_history_survives_disabled_creation(change):
    job, data, repo = setup()
    if change == "stale": data["eligible"] = False
    elif change == "no_record": data.update(record=None, actions=[], eligible=False)
    elif change == "no_actions":
        from test_v22_page_check_storage import resign
        data["record"]["entries"] = []; resign(data["record"]); data.update(actions=[], eligible=False)
    elif change == "foreign": data["history"][0]["report_id"] = str(uuid4())
    elif change == "duplicate_history": data["history"] *= 2
    elif change == "duplicate_action": data["actions"] *= 2
    elif change == "extra": data["lease_id"] = str(uuid4())
    elif change == "bad_digest": data["record"]["checksum"] = "sha256:" + "0" * 64
    repo._rpc.return_value = json.dumps(data)
    if change in ("valid", "stale", "no_record", "no_actions"):
        result = await PageCheckEntry(repo).run(command(job))
        assert len(result.history) == 1 and result.eligibility == {"valid": "eligible", "stale": "source_changed"}.get(change, change)
        assert "user_id" not in result.model_dump_json() and "entries" not in result.model_dump_json()
    else:
        with pytest.raises(DeterministicJobError): await PageCheckEntry(repo).run(command(job))
    repo.request_check.assert_not_called()


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "unable"])
async def test_detail_projection_has_no_source_body_lease_or_effect(status):
    job = checked(job_payload("completed" if status == "unable" else status, unable=status == "unable"))
    value = PageCheckEntry.view(job, command(job))
    assert value.selection == job.selection
    for forbidden in ("lease_id", "report_checksum", "business_checksum", "content_checksum", "causal_eligible", "user_id", "plan"):
        assert f'"{forbidden}"' not in value.model_dump_json()


@pytest.mark.parametrize("change", ["recover", "missing", "report", "delivery", "selection", "new", "new_wrong_record", "new_stale"])
async def test_recovery_and_binding_before_insert(change):
    job, data, repo = setup(); request = command(job, "read" if change == "missing" else "request")
    if change == "report": request = request.model_copy(update={"report_id": uuid4()})
    if change == "delivery": request = request.model_copy(update={"delivery_revision_id": uuid4()})
    if change == "selection": request = request.model_copy(update={"selection": job.selection.model_copy(update={"action_id": "other"})})
    if change.startswith("new") or change == "missing": repo.read_check.return_value = None
    if change == "new_wrong_record": request = request.model_copy(update={"selection": job.selection.model_copy(update={"implementation": job.selection.implementation.model_copy(update={"id": uuid4()})})})
    if change == "new_stale": data["eligible"] = False; repo._rpc.return_value = json.dumps(data)
    if change in ("recover", "new"):
        assert (await PageCheckEntry(repo).run(request)).id == job.id
    else:
        with pytest.raises(DeterministicJobError): await PageCheckEntry(repo).run(request)
    if change != "new": repo.request_check.assert_not_called()
    else: assert repo.request_check.call_args.kwargs["report_id"] == job.report_id


@pytest.mark.parametrize("body", ["valid", "unknown", "malformed", "inconsistent", "extra"])
async def test_only_verified_database_rate_details_cross_boundary(body):
    detail = limits(used=1, next_request_at="2026-10-23T01:05:00Z", reason="cooldown")
    if body == "inconsistent": detail["used"] = 0
    if body == "extra": detail["private"] = "secret"
    error = dict(code="PT429", message="PAGE_CHECK_RATE_LIMIT", details=json.dumps(detail))
    if body == "unknown": error["message"] = "private infrastructure error"
    if body == "malformed": error["details"] = "{"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(429, json=error))) as client:
        repo = PageCheckRepository(url="https://db.invalid", service_role_key="test", http_client=client)
        with pytest.raises(PageCheckRateLimited if body == "valid" else TransientJobError): await repo._rpc("request_v22_page_check", {})


@pytest.mark.parametrize("kind,status", [("valid", 200), ("no_auth", 401), ("disabled", 404), ("base_disabled", 404),
    ("wrong_type", 415), ("large", 413), ("malformed", 400), ("extra", 400), ("bad_shape", 400),
    ("missing", 404), ("conflict", 409), ("rate", 429), ("transient", 503), ("private", 503)])
async def test_api_auth_flags_bounds_errors(monkeypatch, kind, status):
    job, _, _ = setup(); request = command(job)
    monkeypatch.setattr(settings, "V22_PAGE_CHECKS_ENABLED", kind != "disabled")
    monkeypatch.setattr(settings, "V22_CAPABILITY_EXECUTION_ENABLED", kind != "base_disabled")
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", SecretStr("test-only"))
    service = SimpleNamespace(run=AsyncMock(return_value=PageCheckEntry.view(job, request)))
    if kind in ("missing", "conflict", "private"):
        service.run.side_effect = DeterministicJobError({"missing": "V22_PAGE_CHECK_NOT_FOUND", "conflict": "V22_PAGE_CHECK_CONFLICT", "private": "SECRET"}[kind], "private text")
    if kind == "rate": service.run.side_effect = PageCheckRateLimited(PageCheckLimits(**limits(used=1, active=True, reason="active")))
    if kind == "transient": service.run.side_effect = TransientJobError("SECRET", "private text")
    app = FastAPI(); app.include_router(router); app.dependency_overrides[page_check_service] = lambda: service
    data = request.model_dump(mode="json")
    if kind == "extra": data["token"] = "private"
    if kind == "bad_shape": data["check_id"] = str(job.id)
    raw = "x" * 24577 if kind == "large" else "{" if kind == "malformed" else json.dumps(data)
    headers = {"Content-Type": "text/plain" if kind == "wrong_type" else "application/json"}
    if kind != "no_auth": headers["Authorization"] = "Bearer test-only"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v2/page-checks", content=raw, headers=headers)
    assert response.status_code == status and "private text" not in response.text and "SECRET" not in response.text
    if kind == "rate": assert response.json()["limits"]["reason"] == "active"
    if status in (200, 429): assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("pending", [[], [str(uuid4())], ["invalid"], [str(uuid4())] * 2, [str(uuid4())] * 21, {}, None])
async def test_dispatch_only_bounded_distinct_persisted_ids(pending):
    rpc = AsyncMock(return_value=json.dumps(pending)); enqueue = AsyncMock()
    await reconcile_v22_page_checks(dict(page_check_repository=SimpleNamespace(_rpc=rpc),
        redis=SimpleNamespace(enqueue_job=enqueue), page_check_queue="test"))
    if isinstance(pending, list) and len(pending) == 1 and pending[0] != "invalid":
        enqueue.assert_awaited_once_with("execute_v22_page_check", pending[0], _job_id=f"page-check:{pending[0]}", _queue_name="test")
    else: enqueue.assert_not_called()


async def test_default_off_and_single_attempt_worker():
    from app.core.config import Settings
    from app.jobs_v22.worker import WorkerSettings
    assert Settings.model_fields["V22_PAGE_CHECKS_ENABLED"].default is False
    await reconcile_v22_page_checks({}); await execute_v22_page_check({}, str(uuid4()))
    registered = [f for f in WorkerSettings.functions if f.name == "execute_v22_page_check"]
    assert len(registered) == 1 and registered[0].max_tries == 1 and registered[0].timeout_s == 105 and registered[0].keep_result_s == 0


@pytest.mark.parametrize("disabled", [None, "V22_PAGE_CHECKS_ENABLED", "V22_CAPABILITY_EXECUTION_ENABLED"])
async def test_worker_lifecycle_independent_of_google_flags(monkeypatch, disabled):
    import fakeredis.aioredis
    from app.jobs_v22.worker import on_startup, on_shutdown
    for flag in ("V22_PAGE_CHECKS_ENABLED", "V22_CAPABILITY_EXECUTION_ENABLED"):
        monkeypatch.setattr(settings, flag, disabled != flag)
    monkeypatch.setattr(settings, "V22_COMPARISONS_ENABLED", False)
    monkeypatch.setattr(settings, "V22_SUPABASE_URL", "https://db.invalid")
    monkeypatch.setattr(settings, "V22_SUPABASE_SERVICE_ROLE_KEY", SecretStr("test-only"))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False); ctx = {"redis": redis}
    try:
        await on_startup(ctx)
        assert ("page_check_repository" in ctx) == (disabled is None)
    finally:
        await on_shutdown(ctx); await redis.aclose()
    if disabled is None: assert ctx["page_check_client"].is_closed
