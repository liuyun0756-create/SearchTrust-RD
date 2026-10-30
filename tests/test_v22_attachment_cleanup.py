from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.jobs_v22.attachment_cleanup import AttachmentCleanup, BUCKET, CleanupUnavailable, MAX_BYTES, MIMES
from app.jobs_v22 import attachment_cleanup_worker as worker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def row(**changes):
    identity = str(uuid4())
    return {"id": identity, "object_key": f"files/{identity}", "status": "removing",
            "mime": "application/pdf", "byte_size": 10,
            "cleanup_after": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), **changes}


def bucket(**changes):
    return {"id": BUCKET, "public": False, "file_size_limit": MAX_BYTES,
            "allowed_mime_types": sorted(MIMES), **changes}


async def exercise(rows, *, bucket_body=None, absence=None, delete_status=200, finish_status=204):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.headers["apikey"] == "private-token"
        assert "/public/" not in str(request.url) and "/sign/" not in str(request.url)
        path = request.url.path
        if path.endswith("prepare_v22_attachment_cleanup"):
            return httpx.Response(200, content=json.dumps(rows))
        if path.endswith("finish_v22_attachment_cleanup"):
            return httpx.Response(finish_status, json=None)
        if "/bucket/" in path:
            return httpx.Response(200, json=bucket() if bucket_body is None else bucket_body)
        if request.method == "DELETE":
            return httpx.Response(delete_status, json=[])
        raise AssertionError("unexpected request")

    def dispatch(request):
        if request.method == "GET" and "/object/" in request.url.path:
            calls.append(request)
            status, body = absence or (404, {"code": "NoSuchKey", "message": "Object not found"})
            return httpx.Response(status, json=body)
        return handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as client:
        cleaner = AttachmentCleanup(url="https://storage.example", service_role_key="private-token", http_client=client)
        try:
            result = await cleaner.run()
        except CleanupUnavailable:
            result = "unavailable"
    return result, calls


async def test_empty_batch_never_contacts_storage():
    result, calls = await exercise([])
    assert result == {"processed": 0, "removed": 0, "failed": 0}
    assert len(calls) == 1


async def test_exact_object_delete_absence_then_finish():
    item = row()
    result, calls = await exercise([item])
    assert result == {"processed": 1, "removed": 1, "failed": 0}
    deletion = next(c for c in calls if c.method == "DELETE")
    assert json.loads(deletion.content) == {"prefixes": [item["object_key"]]}
    assert calls[-2].url.path == f"/storage/v1/object/{BUCKET}/{item['object_key']}"
    assert json.loads(calls[-1].content) == {"p_id": item["id"]}


@pytest.mark.parametrize("change", [
    {"object_key": "files/../other"}, {"id": "not-an-id"}, {"status": "ready"},
    {"cleanup_after": "2030-01-01T00:00:00Z"}, {"cleanup_after": "2020-01-01"},
    {"cleanup_after": None}, {"mime": "text/html"}, {"byte_size": True}, {"byte_size": MAX_BYTES+1},
])
async def test_invalid_batch_fails_before_storage(change):
    result, calls = await exercise([row(), row(**change)])
    assert result == "unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("rows", [None, {}, [row()] * 21, [row()] * 2])
async def test_invalid_count_type_or_duplicate_fails_closed(rows):
    result, calls = await exercise(rows)
    assert result == "unavailable" and len(calls) == 1


@pytest.mark.parametrize("change", [
    {"public": True}, {"file_size_limit": MAX_BYTES+1}, {"allowed_mime_types": ["application/pdf"]},
    {"id": "other-bucket"}, {"allowed_mime_types": None},
])
async def test_bucket_policy_mismatch_never_deletes(change):
    result, calls = await exercise([row()], bucket_body=bucket(**change))
    assert result == "unavailable"
    assert not any(c.method == "DELETE" for c in calls)


@pytest.mark.parametrize("absence", [
    (200, "still present"), (403, {"code": "NoSuchKey"}),
    (404, {"code": "NoSuchBucket"}), (404, {"message": "not found"}),
    (500, {"code": "InternalError"}), (302, {}),
])
async def test_not_proven_absent_never_acknowledges(absence):
    result, calls = await exercise([row()], absence=absence)
    assert result == {"processed": 1, "removed": 0, "failed": 1}
    assert not any(c.url.path.endswith("finish_v22_attachment_cleanup") for c in calls)


async def test_legacy_storage_missing_response_supported():
    result, _ = await exercise([row()], absence=(400, {"statusCode": "404", "message": "Object not found"}))
    assert result["removed"] == 1


@pytest.mark.parametrize("delete_status,finish_status", [(503, 204), (200, 503)])
async def test_delete_or_finish_failure_remains_retryable(delete_status, finish_status):
    result, _ = await exercise([row()], delete_status=delete_status, finish_status=finish_status)
    assert result == {"processed": 1, "removed": 0, "failed": 1}


async def test_partial_failure_does_not_block_other_objects():
    items = [row(), row()]
    calls = []
    def handle(req):
        calls.append(req)
        if "prepare_" in req.url.path:
            return httpx.Response(200, json=items)
        if "/bucket/" in req.url.path:
            return httpx.Response(200, json=bucket())
        if req.method == "GET":
            return httpx.Response(404, json={"code": "NoSuchKey"})
        if req.method == "DELETE" and items[0]["object_key"] in req.content.decode():
            return httpx.Response(503)
        return httpx.Response(200, json=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        cleaner = AttachmentCleanup(url="https://storage.example", service_role_key="secret", http_client=client)
        assert await cleaner.run() == {"processed": 2, "removed": 1, "failed": 1}
    finishes = [json.loads(c.content)["p_id"] for c in calls if "finish_" in c.url.path]
    assert finishes == [items[1]["id"]]


@pytest.mark.parametrize("kind", ["oversized", "encoded", "malformed", "redirect"])
async def test_storage_protocol_failure_never_deletes(kind):
    calls = []
    def handle(req):
        calls.append(req)
        if "prepare_" in req.url.path:
            return httpx.Response(200, json=[row()])
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * 131_073)
        if kind == "encoded":
            return httpx.Response(200, content=b"{}", headers={"content-encoding": "unknown"})
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example"})
        return httpx.Response(200, content=b"not json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CleanupUnavailable):
            await AttachmentCleanup(url="https://storage.example", service_role_key="secret", http_client=client).run()
    assert len(calls) == 2


async def test_batch_concurrency_is_bounded_and_all_items_acknowledged():
    active, peak = 0, 0
    async def handle(req):
        nonlocal active, peak
        if "prepare_" in req.url.path:
            return httpx.Response(200, json=[row() for _ in range(20)])
        if "/bucket/" in req.url.path:
            return httpx.Response(200, json=bucket())
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.002)
            if req.method == "GET":
                return httpx.Response(404, json={"code": "NoSuchKey"})
            return httpx.Response(204)
        finally:
            active -= 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await AttachmentCleanup(url="https://storage.example", service_role_key="secret", http_client=client).run()
    assert result == {"processed": 20, "removed": 20, "failed": 0}
    assert peak == 4


async def test_enabled_startup_owns_and_closes_http_client():
    ctx = {}
    configured = SimpleNamespace(V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED=True,
        V22_SUPABASE_URL="https://storage.example", V22_SUPABASE_SERVICE_ROLE_KEY=SecretStr("secret"),
        V22_REDIS_PREFIX="test-prefix")
    await worker.start_attachment_cleanup(ctx, configured)
    assert isinstance(ctx["attachment_cleanup"], AttachmentCleanup)
    assert not ctx["attachment_cleanup_client"].is_closed
    await worker.stop_attachment_cleanup(ctx)
    assert ctx["attachment_cleanup_client"].is_closed


async def test_disabled_does_no_maintenance_io():
    ctx = {}
    await worker.start_attachment_cleanup(ctx, SimpleNamespace(V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED=False))
    await worker.cleanup_v22_implementation_attachments(ctx)
    assert ctx == {}


async def test_invalid_configuration_does_not_break_worker(caplog):
    ctx = {}
    configured = SimpleNamespace(V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED=True,
        V22_SUPABASE_URL="http://unsafe", V22_SUPABASE_SERVICE_ROLE_KEY=SecretStr("private-token"))
    await worker.start_attachment_cleanup(ctx, configured)
    assert "attachment_cleanup" not in ctx
    assert "configuration" in caplog.text and "private-token" not in caplog.text


async def test_timeout_records_unknown_counts_and_safe_result(monkeypatch, caplog):
    monkeypatch.setattr(worker, "RUN_SECONDS", 0.01)
    cleaner = SimpleNamespace(run=AsyncMock(side_effect=lambda: None))
    async def slow():
        await asyncio.sleep(1)
    cleaner.run = slow
    redis = SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1))
    await worker.cleanup_v22_implementation_attachments({"redis": redis,
        "attachment_cleanup": cleaner, "attachment_cleanup_prefix": "test"})
    receipt = json.loads(redis.eval.call_args.args[5])
    assert receipt["status"] == "timeout" and receipt["removed"] is None


async def test_cancellation_records_and_releases_owned_lease():
    cleaner = SimpleNamespace(run=AsyncMock(side_effect=asyncio.CancelledError))
    redis = SimpleNamespace(set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1))
    with pytest.raises(asyncio.CancelledError):
        await worker.cleanup_v22_implementation_attachments({"redis": redis,
            "attachment_cleanup": cleaner, "attachment_cleanup_prefix": "test"})
    assert json.loads(redis.eval.call_args.args[5])["status"] == "cancelled"


async def test_cron_has_bounded_minute_schedule_and_flag_defaults_off():
    from app.core.config import Settings
    from app.jobs_v22.worker import WorkerSettings
    job = next(c for c in WorkerSettings.cron_jobs if c.name == "cleanup_v22_implementation_attachments")
    assert job.second == 28 and job.unique and job.max_tries == 1
    assert job.timeout_s == 110
    assert Settings.model_fields["V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED"].default is False
